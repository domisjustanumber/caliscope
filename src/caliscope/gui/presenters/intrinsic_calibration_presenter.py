"""Presenter for intrinsic camera calibration workflow.

Coordinates the collection of calibration board corner observations and calibration
via the domain's pure functions. Emits raw FramePackets for the View to
handle display transforms (undistortion, rotation, padding).

This is a "scratchpad" presenter - accumulated data and calibration results
are transient until emitted to the Coordinator for persistence.
"""

import logging
from enum import Enum, auto
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any

import cv2
from PySide6.QtCore import QObject, Qt, Signal

from caliscope.cameras.camera_array import CameraData
from caliscope.core.calibrate_intrinsics import (
    IntrinsicCalibrationOutput,
    IntrinsicCalibrationReport,
    run_intrinsic_calibration,
)
from caliscope.core.frame_selector import IntrinsicCoverageReport, select_calibration_frames
from caliscope.core.point_data import ImagePoints
from caliscope.packets import FramePacket, PointPacket
from caliscope.recording.capture_devices import enumerate_supported_capture_resolutions
from caliscope.recording.frame_packet_streamer import FramePacketStreamer, create_streamer
from caliscope.recording.frame_source import FrameSource
from caliscope.task_manager.cancellation import CancellationToken
from caliscope.task_manager.task_handle import TaskHandle
from caliscope.task_manager.task_manager import TaskManager
from caliscope.task_manager.task_state import TaskState
from caliscope.tracker import Tracker

logger = logging.getLogger(__name__)


class IntrinsicCalibrationState(Enum):
    """Workflow states for intrinsic calibration.

    States are computed from internal reality, not stored separately.
    This prevents state/reality divergence.
    """

    READY = auto()  # Initial state, can start collection
    COLLECTING = auto()  # Batch loop running, accumulating points
    CALIBRATING = auto()  # calibrate_intrinsics() running via TaskManager
    CALIBRATED = auto()  # Result available, can toggle undistortion


class IntrinsicCalibrationPresenter(QObject):
    """Presenter for single-camera intrinsic calibration workflow.

    Manages the collection of calibration board observations from recorded video and
    submission of calibration to TaskManager. Exposes a display_queue for
    the View's processing thread to consume directly (avoids GUI thread hop).

    Collection uses a batch-seek pattern: a background thread seeks directly to
    every Nth frame via FrameSource.get_frame(), tracks it, and emits for display.
    This skips expensive tracking on frames that won't be used.

    A FramePacketStreamer handles interactive scrubbing in READY/CALIBRATED states.

    Signals:
        state_changed: Emitted when computed state changes. View updates UI.
        calibration_complete: Emitted when calibration succeeds. Contains
            a new CameraData with calibration results applied.
        calibration_failed: Emitted when calibration fails. Contains error message.
        frame_position_changed: Emitted when current frame changes (background thread,
            Qt.AutoConnection queues to main thread).

    Queue:
        display_queue: View's processing thread reads FramePackets from here.
            Keeps heavy frame data off the GUI thread until processed into QPixmap.
    """

    state_changed = Signal(IntrinsicCalibrationState)
    calibration_complete = Signal(object)  # IntrinsicCalibrationOutput
    calibration_failed = Signal(str)
    frame_position_changed = Signal(int)  # Current frame index
    live_resolution_changed = Signal(int, int)  # width, height (actual capture size)

    def __init__(
        self,
        camera: CameraData,
        video_path: Path | None,
        tracker: Tracker,
        task_manager: TaskManager,
        parent: QObject | None = None,
        restored_report: IntrinsicCalibrationReport | None = None,
        restored_points: list[tuple[int, PointPacket]] | None = None,
        frame_skip: int = 1,
    ) -> None:
        """Initialize the presenter.

        Args:
            camera: CameraData with cam_id, size, rotation_count (and optionally live_device_index).
            video_path: Path to intrinsic video, or None when :pyattr:`CameraData.live_device_index` is set.
            tracker: Tracker for calibration board point detection
            task_manager: TaskManager for background calibration
            parent: Optional Qt parent
            restored_report: Optional report from previous calibration for overlay restoration
            restored_points: Optional collected points from previous calibration (session-only)
            frame_skip: Process every Nth frame during collection
        """
        super().__init__(parent)

        self._camera = camera
        self._video_path = video_path
        self._tracker = tracker
        self._task_manager = task_manager
        self._frame_skip = frame_skip

        self._is_live = camera.live_device_index is not None
        self._tracker_lock = Lock()
        self._live_sample_counter = 0

        # Derived properties for convenience
        self._cam_id = camera.cam_id
        self._image_size = camera.size

        # Scratchpad state - may be restored from previous calibration
        self._collected_points: list[tuple[int, PointPacket]] = []
        self._output: IntrinsicCalibrationOutput | None = None
        self._calibration_task: TaskHandle | None = None
        self._selection_result: IntrinsicCoverageReport | None = None

        # Restore previous calibration state if available
        if restored_report is not None and camera.matrix is not None:
            self._output = IntrinsicCalibrationOutput(camera=camera, report=restored_report)
            logger.info(f"Restored calibration for cam_id {self._cam_id}")

        if restored_points is not None:
            self._collected_points = list(restored_points)

        # Display queue for View consumption
        self._display_queue: Queue[FramePacket | None] = Queue()

        # Collection state
        self._is_collecting = False
        self._stop_collection = Event()
        self._collection_thread: Thread | None = None

        self._streamer: FramePacketStreamer | None = None
        self._frame_queue: Queue[FramePacket] = Queue()
        self._stream_handle: TaskHandle | None = None
        self._stop_event = Event()
        self._consumer_thread: Thread | None = None
        self._live_stop = Event()
        self._live_thread: Thread | None = None
        self._live_device_index: int | None = camera.live_device_index
        self._live_resolution_lock = Lock()
        self._pending_live_resolution: tuple[int, int] | None = None
        self._live_supported_resolutions: list[tuple[int, int]] = []

        if self._is_live:
            if self._live_device_index is None:
                raise ValueError("live_device_index required for live intrinsic calibration")
            self._live_supported_resolutions = enumerate_supported_capture_resolutions(
                self._live_device_index,
                extra_sizes=(self._camera.size,),
            )
            if not self._live_supported_resolutions:
                self._live_supported_resolutions = [self._camera.size]
            self._current_frame_index = 0
            self._live_stop.clear()
            self._live_thread = Thread(target=self._live_capture_loop, daemon=True)
        else:
            if self._video_path is None:
                raise ValueError("video_path is required for file-based intrinsic calibration")
            self._streamer = create_streamer(
                video_directory=self._video_path.parent,
                cam_id=self._camera.cam_id,
                rotation_count=self._camera.rotation_count,
                tracker=self._tracker,
                end_behavior="pause",  # Pause at end for interactive scrubbing
            )
            self._streamer.subscribe(self._frame_queue)

            # Start streamer worker (will read first frame, then we pause)
            self._stream_handle = self._task_manager.submit(
                self._streamer.play_worker,
                name=f"Streamer cam_id {self._cam_id}",
                auto_start=False,
            )
            self._task_manager.start_task(self._stream_handle.task_id)
            self._streamer.pause()  # Immediately pause for scrubbing mode

            # Position tracking (must be set before _load_initial_frame)
            self._current_frame_index = int(self._streamer.start_frame_index)

            # Guaranteed initial frame display (don't rely on thread timing)
            self._load_initial_frame()

            # Consumer thread for streamer frames (scrubbing display only)
            self._consumer_thread = Thread(target=self._consume_frames, daemon=True)
            self._consumer_thread.start()

    @property
    def state(self) -> IntrinsicCalibrationState:
        """Compute current state from internal reality - never stale."""
        if self._output is not None:
            return IntrinsicCalibrationState.CALIBRATED

        if self._calibration_task is not None and self._calibration_task.state == TaskState.RUNNING:
            return IntrinsicCalibrationState.CALIBRATING

        if self._is_collecting:
            return IntrinsicCalibrationState.COLLECTING

        return IntrinsicCalibrationState.READY

    @property
    def is_live_stream(self) -> bool:
        """True when frames come from a capture device instead of a file."""
        return self._is_live

    @property
    def live_supported_resolutions(self) -> list[tuple[int, int]]:
        """Distinct resolutions accepted by the capture device (live cameras only)."""
        return list(self._live_supported_resolutions)

    def bind_workspace_camera(self, camera: CameraData) -> None:
        """Keep presenter aligned with the workspace camera row after persistence."""
        self._camera = camera
        self._image_size = camera.size

    def start_live_capture_if_needed(self) -> None:
        """Start the live capture thread once signals are connected (Idempotent)."""
        if not self._is_live or self._live_thread is None:
            return
        if self._live_thread.is_alive():
            return
        self._live_stop.clear()
        self._live_thread.start()

    @property
    def display_queue(self) -> Queue[FramePacket | None]:
        """Queue for View's processing thread to consume frames from.

        None sentinel signals end of current sequence (e.g., after stop).
        """
        return self._display_queue

    @property
    def calibrated_camera(self) -> CameraData | None:
        """Access calibrated camera for View's undistortion setup."""
        return self._output.camera if self._output is not None else None

    @property
    def calibration_report(self) -> IntrinsicCalibrationReport | None:
        """Access calibration quality report for display."""
        return self._output.report if self._output is not None else None

    @property
    def camera(self) -> CameraData:
        """Access original camera data for View's display setup."""
        return self._camera

    @property
    def frame_count(self) -> int:
        """Total frames in video, or running count for live capture."""
        if self._is_live:
            return max(1, self._current_frame_index + 1)
        assert self._streamer is not None
        return self._streamer.last_frame_index + 1

    @property
    def current_frame_index(self) -> int:
        """Current frame position."""
        return self._current_frame_index

    @property
    def collected_points(self) -> list[tuple[int, PointPacket]]:
        """Accumulated points for overlay rendering. Returns a copy."""
        return list(self._collected_points)

    @property
    def selected_frame_indices(self) -> list[int] | None:
        """Selected frame indices for overlay rendering. Returns a copy.

        Checks both the selection result (from current calibration) and the
        output report (from restored calibration) for the frame list.
        """
        if self._selection_result is not None:
            return list(self._selection_result.selected_frames)
        if self._output is not None:
            return list(self._output.report.selected_frames)
        return None

    @property
    def board_connectivity(self) -> set[tuple[int, int]]:
        """Point ID pairs that should be connected to form grid."""
        return self._tracker.get_connected_points()

    def refresh_display(self) -> None:
        """Put a fresh frame on the display queue.

        Call this when display settings change (e.g., undistort toggle)
        and the View needs to re-render with new settings.
        """
        if self._is_live:
            return
        self._load_initial_frame()

    def seek_to(self, frame_index: int) -> None:
        """Seek to frame. Works in READY/CALIBRATED states via streamer's seek_to."""
        if self._is_live:
            return
        if self.state not in (IntrinsicCalibrationState.READY, IntrinsicCalibrationState.CALIBRATED):
            return

        assert self._streamer is not None
        frame_index = max(0, min(frame_index, self.frame_count - 1))
        self._streamer.seek_to(
            frame_index, precise=True
        )  # precise=False would cause Fast seek, skipping between keyframes

    def _load_initial_frame(self) -> None:
        """Read current frame from video with tracking and put on display queue.

        Uses current_frame_index to preserve user's position (not always start_frame_index).
        """
        assert self._streamer is not None
        # Use current position (default to start if not yet set)
        target_index = self._current_frame_index if self._current_frame_index > 0 else self._streamer.start_frame_index

        packet = self._streamer.peek_tracked_frame(target_index)

        if packet is not None:
            self._display_queue.put(packet)
        else:
            logger.warning(f"Failed to load frame {target_index} from {self._video_path!s}")

    # -------------------------------------------------------------------------
    # Collection: batch-seek pattern
    # -------------------------------------------------------------------------

    def start_calibration(self) -> None:
        """Start collecting calibration frames via batch-seek loop.

        Pauses the streamer (keeps it alive for later scrubbing) and spawns
        a collection thread that seeks directly to every Nth frame.
        """
        if self.state not in (IntrinsicCalibrationState.READY, IntrinsicCalibrationState.CALIBRATED):
            logger.warning(f"Cannot start calibration in state {self.state}")
            return

        logger.info(f"Starting calibration collection for cam_id {self._cam_id}")

        # Clear previous calibration data BEFORE setting collecting flag
        # (state is computed: CALIBRATED check comes before COLLECTING check)
        self._collected_points.clear()
        self._selection_result = None
        self._output = None
        self._calibration_task = None

        if self._is_live:
            self._live_sample_counter = 0
            self._is_collecting = True
            self._stop_collection.clear()
            self._emit_state_changed()
            return

        # Pause streamer — collection uses its own FrameSource
        assert self._streamer is not None
        self._streamer.pause()

        # Now set collecting and emit state change
        self._is_collecting = True
        self._stop_collection.clear()
        self._emit_state_changed()

        # Spawn batch collection thread
        self._collection_thread = Thread(target=self._run_collection, daemon=True)
        self._collection_thread.start()

    def stop_calibration(self) -> None:
        """Stop collection and return to READY state.

        Signals the collection thread to stop and clears accumulated data.
        """
        if self.state != IntrinsicCalibrationState.COLLECTING:
            logger.warning(f"Cannot stop calibration in state {self.state}")
            return

        logger.info(f"Stopping calibration collection for cam_id {self._cam_id}")

        if self._is_live:
            self._is_collecting = False
            if len(self._collected_points) > 0:
                self._on_collection_complete()
            else:
                self._collected_points.clear()
                self._emit_state_changed()
            return

        self._stop_collection.set()
        if self._collection_thread is not None:
            self._collection_thread.join(timeout=5.0)
            self._collection_thread = None

        self._collected_points.clear()
        self._is_collecting = False
        self._emit_state_changed()

    def _run_collection(self) -> None:
        """Batch collection loop — seeks directly to every Nth frame.

        Creates a temporary FrameSource, iterates through subsampled frame
        indices, tracks each frame, accumulates points, and emits for display.
        Matches the pattern in process_synchronized_recording().
        """
        assert self._video_path is not None
        assert self._streamer is not None
        frame_source = FrameSource(self._video_path.parent, self._cam_id)
        last_index = self._streamer.last_frame_index
        frame_skip = max(1, self._frame_skip)

        indices = list(range(0, last_index + 1, frame_skip))
        total = len(indices)

        logger.info(f"Collection batch: {total} frames (skip={frame_skip}, total available={last_index + 1})")

        try:
            for i, frame_idx in enumerate(indices):
                if self._stop_collection.is_set():
                    logger.info(f"Collection cancelled at frame {frame_idx}")
                    break

                frame = frame_source.read_frame_at(frame_idx)
                if frame is None:
                    continue

                # Track the frame
                with self._tracker_lock:
                    tracker = self._tracker
                points = tracker.get_points(frame, self._cam_id, self._camera.rotation_count) if tracker else None

                # Accumulate if board detected
                if points is not None and len(points.point_id) > 0:
                    self._collected_points.append((frame_idx, points))

                # Emit for display (subsampled frames only — preferred)
                packet = FramePacket(
                    cam_id=self._cam_id,
                    frame_index=frame_idx,
                    frame_time=0.0,
                    frame=frame,
                    points=points,
                )
                self._display_queue.put(packet)
                self._current_frame_index = frame_idx
                self.frame_position_changed.emit(frame_idx)

        finally:
            frame_source.close()

        # If not cancelled, proceed to calibration
        if not self._stop_collection.is_set():
            self._on_collection_complete()

    # -------------------------------------------------------------------------
    # Scrubbing consumer (READY/CALIBRATED states only)
    # -------------------------------------------------------------------------

    def _consume_frames(self) -> None:
        """Pull frames from streamer queue and emit for display.

        Handles scrubbing in READY/CALIBRATED states. During COLLECTING state,
        the batch loop writes directly to the display queue instead.
        """
        logger.debug(f"Consumer thread started for cam_id {self._cam_id}")

        while not self._stop_event.is_set():
            # Exit if streamer was cancelled externally
            if self._stream_handle is not None and self._stream_handle.state == TaskState.CANCELLED:
                break

            try:
                packet: FramePacket = self._frame_queue.get(timeout=0.1)
            except Empty:
                continue

            # Skip end-of-stream markers
            if packet.frame_index == -1:
                continue

            # Emit for display (scrubbing only — collection uses its own path)
            self._display_queue.put(packet)
            self._current_frame_index = packet.frame_index
            self.frame_position_changed.emit(packet.frame_index)

        logger.debug(f"Consumer thread exiting for cam_id {self._cam_id}")

    # -------------------------------------------------------------------------
    # Post-collection: calibration pipeline
    # -------------------------------------------------------------------------

    def _on_collection_complete(self) -> None:
        """Called when batch loop finishes. Submits calibration task."""
        self._is_collecting = False

        if len(self._collected_points) == 0:
            logger.warning(f"No points collected for cam_id {self._cam_id}")
            self.calibration_failed.emit("No calibration boards detected in video")
            self._emit_state_changed()
            return

        logger.info(f"Collection complete for cam_id {self._cam_id}: {len(self._collected_points)} frames with points")

        # Build ImagePoints from collected data
        try:
            image_points = self._build_image_points()
        except Exception as e:
            logger.error(f"Failed to build ImagePoints: {e}")
            self.calibration_failed.emit(str(e))
            self._emit_state_changed()
            return

        # Select calibration frames
        selection_result = select_calibration_frames(image_points, self._cam_id, self._image_size)

        if not selection_result.selected_frames:
            logger.warning(f"No frames selected for calibration at cam_id {self._cam_id}")
            self.calibration_failed.emit("Frame selection found no suitable frames")
            self._emit_state_changed()
            return

        logger.info(f"Selected {len(selection_result.selected_frames)} frames for calibration")

        # Store selection result for overlay rendering
        self._selection_result = selection_result

        # Capture camera for closure (avoid stale reference)
        camera = self._camera

        # Submit calibration to TaskManager using the orchestrator
        def calibration_worker(token: CancellationToken, handle: TaskHandle) -> IntrinsicCalibrationOutput:
            return run_intrinsic_calibration(
                camera,
                image_points,
                selection_result,
            )

        self._calibration_task = self._task_manager.submit(
            calibration_worker,
            name=f"Intrinsic calibration cam_id {self._cam_id}",
            auto_start=False,
        )
        # Use QueuedConnection - TaskHandle signals emitted from worker threads
        self._calibration_task.completed.connect(
            self._on_calibration_complete,
            Qt.ConnectionType.QueuedConnection,
        )
        self._calibration_task.failed.connect(
            self._on_calibration_failed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._task_manager.start_task(self._calibration_task.task_id)

        self._emit_state_changed()

    def _build_image_points(self) -> ImagePoints:
        """Convert accumulated (frame_index, PointPacket) to ImagePoints."""
        import pandas as pd

        rows = []
        for frame_index, points in self._collected_points:
            # For single-camera calibration, sync_index == frame_index
            point_count = len(points.point_id)

            # Build row data matching ImagePoints schema
            row_data = {
                "sync_index": [frame_index] * point_count,
                "cam_id": [self._cam_id] * point_count,
                "frame_index": [frame_index] * point_count,
                "frame_time": [0.0] * point_count,  # Not used for calibration
                "point_id": points.point_id.tolist(),
                "img_loc_x": points.img_loc[:, 0].tolist(),
                "img_loc_y": points.img_loc[:, 1].tolist(),
                "obj_loc_x": points.obj_loc[:, 0].tolist() if points.obj_loc is not None else [None] * point_count,
                "obj_loc_y": points.obj_loc[:, 1].tolist() if points.obj_loc is not None else [None] * point_count,
                "obj_loc_z": points.obj_loc[:, 2].tolist() if points.obj_loc is not None else [None] * point_count,
            }
            rows.append(pd.DataFrame(row_data))

        df = pd.concat(rows, ignore_index=True)
        return ImagePoints(df)

    def _on_calibration_complete(self, output: IntrinsicCalibrationOutput) -> None:
        """Handle successful calibration. Stores output and emits signal."""
        report = output.report
        logger.info(
            f"Calibration complete for cam_id {self._cam_id}: rmse={report.rmse:.3f}px, frames={report.frames_used}"
        )

        # Store complete output (camera + report)
        self._output = output

        self.calibration_complete.emit(output)
        self._emit_state_changed()

        if self._streamer is not None:
            self._streamer.seek_to(0, precise=True)

    def _on_calibration_failed(self, exc_type: str, message: str) -> None:
        """Handle calibration failure."""
        logger.error(f"Calibration failed for cam_id {self._cam_id}: {exc_type}: {message}")
        self.calibration_failed.emit(f"{exc_type}: {message}")
        self._emit_state_changed()

    def _emit_state_changed(self) -> None:
        """Emit state_changed signal with current computed state."""
        current_state = self.state
        logger.debug(f"State changed to {current_state} for cam_id {self._cam_id}")
        self.state_changed.emit(current_state)

    def update_tracker(self, tracker: Tracker) -> None:
        """Update tracker for next calibration run.

        Hot-swaps the tracker reference in both the presenter and the streamer.
        Clears any collected points (old tracker's point IDs are stale) and
        resets calibration state.

        Args:
            tracker: New tracker to use for subsequent calibrations.
        """
        with self._tracker_lock:
            self._tracker = tracker
        if self._streamer is not None:
            self._streamer.update_tracker(tracker)
        self._collected_points.clear()
        self._selection_result = None
        self._output = None
        logger.info(f"Tracker updated for cam_id {self._cam_id}, cleared collected points")
        self._emit_state_changed()
        self.refresh_display()

    @property
    def frame_skip(self) -> int:
        """Current frame skip interval for collection."""
        return self._frame_skip

    def set_frame_skip(self, skip: int) -> None:
        """Set frame skip interval for collection.

        Takes effect on the next collection run. Changing during collection
        has no effect (batch loop captures frame_skip at start).
        """
        self._frame_skip = max(1, skip)

    def request_live_resolution(self, width: int, height: int) -> None:
        """Request a new capture resolution (live cameras only). Applied on the capture thread."""
        if not self._is_live:
            return
        with self._live_resolution_lock:
            self._pending_live_resolution = (int(width), int(height))

    def _clear_calibration_scratchpad(self) -> None:
        self._collected_points.clear()
        self._selection_result = None
        self._output = None
        self._calibration_task = None

    def _invalidate_live_session_after_resolution_change(self) -> None:
        self._is_collecting = False
        self._live_sample_counter = 0
        self._clear_calibration_scratchpad()
        self._emit_state_changed()

    def _safe_cap_read(self, cap: cv2.VideoCapture) -> tuple[bool, Any]:
        """Read one BGR frame; never raises cv2.error (MSMF can throw on bad buffers)."""
        try:
            ok, frame = cap.read()
        except cv2.error:
            logger.warning(
                "VideoCapture.read raised cv2.error for cam_id %s",
                self._cam_id,
                exc_info=True,
            )
            return False, None
        if not ok or frame is None:
            return False, None
        if frame.ndim != 3 or frame.shape[0] < 1 or frame.shape[1] < 1:
            return False, None
        return True, frame

    def _try_apply_resolution_on_capture(
        self,
        cap: cv2.VideoCapture,
        width: int,
        height: int,
        *,
        max_reads: int = 4,
    ) -> tuple[int, int] | None:
        """Set frame size on an open capture and return actual (w, h) from a valid frame."""
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        for _ in range(max(1, max_reads)):
            ok, frame = self._safe_cap_read(cap)
            if not ok:
                continue
            return (int(frame.shape[1]), int(frame.shape[0]))
        return None

    def _apply_capture_resolution(
        self,
        device: int,
        cap: cv2.VideoCapture,
        width: int,
        height: int,
    ) -> tuple[cv2.VideoCapture, tuple[int, int]]:
        """Request resolution; may reopen the device if the backend returns corrupt frames.

        Returns:
            ``(cap, (actual_w, actual_h))``. On total failure, returns the last opened
            ``cap`` (may be closed) and restores :attr:`_camera.size` to ``prev_size``.
        """
        prev_size = self._camera.size

        got = self._try_apply_resolution_on_capture(cap, width, height)
        if got is None:
            logger.warning(
                "Reopening capture device %s for cam_id %s after failed resolution %sx%s",
                device,
                self._cam_id,
                width,
                height,
            )
            cap.release()
            cap = cv2.VideoCapture(device)
            if not cap.isOpened():
                logger.error("Could not reopen capture device %s for cam_id %s", device, self._cam_id)
                self._camera.size = prev_size
                self._image_size = prev_size
                return cap, prev_size
            got = self._try_apply_resolution_on_capture(cap, width, height)

        if got is None:
            logger.error(
                "Failed to set resolution %sx%s on device %s for cam_id %s; restoring %sx%s",
                width,
                height,
                device,
                self._cam_id,
                prev_size[0],
                prev_size[1],
            )
            restored = self._try_apply_resolution_on_capture(cap, prev_size[0], prev_size[1])
            final = restored if restored is not None else prev_size
            self._camera.size = final
            self._image_size = final
            return cap, final

        self._camera.size = got
        self._image_size = got
        return cap, got

    def _live_capture_loop(self) -> None:
        """Continuous read from VideoCapture; optional point collection when calibrating."""
        device = self._live_device_index
        if device is None:
            return
        cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            logger.error("Could not open live capture device %s for cam_id %s", device, self._cam_id)
            return

        try:
            initial_size = self._camera.size
            cap, actual = self._apply_capture_resolution(device, cap, initial_size[0], initial_size[1])
            if not cap.isOpened():
                return

            if actual != initial_size:
                logger.info(
                    "Live capture cam_id %s adjusted resolution %s -> %s",
                    self._cam_id,
                    initial_size,
                    actual,
                )
                self._invalidate_live_session_after_resolution_change()
                self.live_resolution_changed.emit(actual[0], actual[1])

            frame_idx = 0
            while not self._live_stop.is_set():
                with self._live_resolution_lock:
                    pending = self._pending_live_resolution
                    self._pending_live_resolution = None
                if pending is not None:
                    prev = self._camera.size
                    cap, new_actual = self._apply_capture_resolution(device, cap, pending[0], pending[1])
                    if not cap.isOpened():
                        return
                    if new_actual != prev:
                        logger.info(
                            "Live capture cam_id %s resolution %s -> %s",
                            self._cam_id,
                            prev,
                            new_actual,
                        )
                        self._invalidate_live_session_after_resolution_change()
                    if new_actual != prev:
                        self.live_resolution_changed.emit(new_actual[0], new_actual[1])

                ok, frame = self._safe_cap_read(cap)
                if not ok:
                    continue

                with self._tracker_lock:
                    tracker = self._tracker

                points_for_display = None
                draw_instructions = None
                if tracker is not None:
                    points_for_display = tracker.get_points(frame, self._cam_id, self._camera.rotation_count)
                    draw_instructions = tracker.scatter_draw_instructions

                if self._is_collecting:
                    self._live_sample_counter += 1
                    if self._live_sample_counter >= max(1, self._frame_skip):
                        self._live_sample_counter = 0
                        if points_for_display is not None and len(points_for_display.point_id) > 0:
                            self._collected_points.append((frame_idx, points_for_display))

                packet = FramePacket(
                    cam_id=self._cam_id,
                    frame_index=frame_idx,
                    frame_time=0.0,
                    frame=frame,
                    points=points_for_display,
                    draw_instructions=draw_instructions,
                )
                self._display_queue.put(packet)
                self._current_frame_index = frame_idx
                self.frame_position_changed.emit(frame_idx)
                frame_idx += 1
        finally:
            cap.release()
            logger.info("Live capture ended for cam_id %s", self._cam_id)

    def cleanup(self) -> None:
        """Clean up resources. Call before discarding presenter."""
        # Stop collection if running
        self._stop_collection.set()
        if self._collection_thread is not None:
            self._collection_thread.join(timeout=2.0)

        if self._is_live:
            self._live_stop.set()
            if self._live_thread is not None:
                self._live_thread.join(timeout=2.0)
            return

        # Stop consumer thread
        self._stop_event.set()
        if self._consumer_thread is not None:
            self._consumer_thread.join(timeout=2.0)

        # Cancel streamer worker
        if self._stream_handle is not None:
            self._stream_handle.cancel()

        # Clean up streamer
        if self._streamer is not None:
            self._streamer.unsubscribe(self._frame_queue)
            self._streamer.close()
