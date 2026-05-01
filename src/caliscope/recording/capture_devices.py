"""Enumerate host video capture devices via OpenCV."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

# Typical USB webcam / capture modes to probe via OpenCV (width, height).
# Sorted by pixel count descending for presentation after probing.
_CANDIDATE_CAPTURE_RESOLUTIONS: tuple[tuple[int, int], ...] = (
    (3840, 2160),
    (2560, 1440),
    (1920, 1080),
    (1600, 1200),
    (1280, 1024),
    (1280, 720),
    (1024, 768),
    (960, 540),
    (800, 600),
    (640, 480),
    (640, 360),
    (352, 288),
    (320, 240),
)


@dataclass(frozen=True, slots=True)
class DetectedCaptureDevice:
    """An OpenCV index that opened successfully and produced a frame."""

    index: int
    size: tuple[int, int]


def count_attached_capture_devices_without_opening() -> int | None:
    """Best-effort count of attached cameras using OS metadata only.

    Does not call :class:`cv2.VideoCapture` or otherwise open a stream.
    The result may differ from OpenCV device indices (which require a scan).
    Returns ``None`` if the host cannot be queried on this platform.
    """
    try:
        if sys.platform == "win32":
            return _windows_pnp_camera_count()
        if sys.platform == "darwin":
            return _macos_system_profiler_camera_count()
        if sys.platform.startswith("linux"):
            return _linux_v4l_camera_count()
    except Exception:
        logger.debug("count_attached_capture_devices_without_opening failed", exc_info=True)
    return None


def _parse_powershell_int_stdout(stdout: str | bytes | None) -> int | None:
    """Parse a nonnegative integer printed by PowerShell (handles BOM / stray text)."""
    if stdout is None:
        return None
    if isinstance(stdout, bytes):
        text = stdout.decode("utf-8-sig", errors="replace")
    else:
        text = str(stdout).lstrip("\ufeff")
    text_stripped = text.strip()
    if not text_stripped:
        return None
    match = re.search(r"\d+", text_stripped)
    if not match:
        return None
    return int(match.group())


def _windows_pnp_camera_count() -> int | None:
    """Count PnP devices in the ``Camera`` class with status OK (PowerShell)."""
    # Write-Output: a bare `( ... ).Count` -Command expression often exits 0
    # without writing stdout, which made hosts look like "could not detect count".
    script = (
        "$devices = @(Get-PnpDevice -Class 'Camera' -PresentOnly | "
        "Where-Object { $_.Status -eq 'OK' }); Write-Output $devices.Count"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        logger.debug(
            "PowerShell PnP camera query failed (rc=%s): %s",
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return None
    n = _parse_powershell_int_stdout(proc.stdout)
    if n is not None:
        return n

    stderr = (proc.stderr or "").strip()
    logger.debug(
        "PowerShell PnP camera query returned unexpected stdout: %r stderr=%r",
        proc.stdout,
        stderr,
    )
    return None


def _macos_system_profiler_camera_count() -> int | None:
    """Parse ``system_profiler SPCameraDataType`` for installed camera entries."""
    proc = subprocess.run(
        ["system_profiler", "SPCameraDataType", "-json"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode == 0 and (proc.stdout or "").strip():
        try:
            data = json.loads(proc.stdout)
            items = data.get("SPCameraDataType")
            if isinstance(items, list):
                return len(items)
            if isinstance(items, dict):
                inner = items.get("_items")
                if isinstance(inner, list):
                    return len(inner)
        except json.JSONDecodeError:
            pass

    return _macos_camera_count_fallback_text()


def _macos_camera_count_fallback_text() -> int | None:
    proc = subprocess.run(
        ["system_profiler", "SPCameraDataType"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return _macos_camera_count_fallback_from_string(proc.stdout or "")


def _macos_camera_count_fallback_from_string(text: str) -> int | None:
    lowered = text.lower()
    if "no camera" in lowered or "no relevant" in lowered:
        return 0
    # Each built-in / USB camera block usually includes a model id line
    return len(re.findall(r"^\s+Model ID:\s", text, flags=re.MULTILINE))


def _linux_v4l_camera_count() -> int | None:
    """Count distinct V4L devices via ``/dev/v4l/by-id`` when present."""
    by_id = Path("/dev/v4l/by-id")
    if not by_id.is_dir():
        return None
    entries = [p for p in by_id.iterdir() if not p.name.startswith(".")]
    return len(entries)


def enumerate_capture_devices(*, max_index: int = 10) -> list[DetectedCaptureDevice]:
    """Probe integer device indices [0, max_index) with VideoCapture.

    Tries each index briefly; devices that do not open or fail to read a frame
    are omitted. This can take noticeable time if many indices are probed.
    """
    found: list[DetectedCaptureDevice] = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        try:
            if not cap.isOpened():
                continue
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            h, w = frame.shape[:2]
            found.append(DetectedCaptureDevice(index=i, size=(int(w), int(h))))
        except Exception as e:
            logger.debug("enumerate_capture_devices: index %s: %s", i, e)
        finally:
            cap.release()
    return found


def _probe_one_capture_resolution(
    device_index: int,
    width: int,
    height: int,
    *,
    match_tolerance: int = 16,
    max_reads: int = 3,
) -> tuple[int, int] | None:
    """Open the device once, request ``width``×``height``, and read until a valid frame.

    Uses a **fresh** :class:`cv2.VideoCapture` for each call so backends (notably
    Windows MSMF) are less likely to return a corrupt buffer after repeated
    property changes on a single handle. Catches :class:`cv2.error` from
    ``read()`` — some drivers trigger OpenCV Mat assertions instead of
    returning ``ok=False``.

    Returns:
        Actual ``(width, height)`` when a frame matches the request within
        ``match_tolerance``, else ``None``.
    """
    cap = cv2.VideoCapture(device_index)
    if not cap.isOpened():
        return None
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        for _ in range(max(1, max_reads)):
            try:
                ok, frame = cap.read()
            except cv2.error:
                logger.debug(
                    "VideoCapture.read raised cv2.error for device %s at %sx%s",
                    device_index,
                    width,
                    height,
                    exc_info=True,
                )
                return None
            if not ok or frame is None:
                continue
            if frame.ndim != 3 or frame.shape[0] < 1 or frame.shape[1] < 1:
                continue
            aw = int(frame.shape[1])
            ah = int(frame.shape[0])
            if abs(aw - width) > match_tolerance or abs(ah - height) > match_tolerance:
                continue
            return (aw, ah)
        return None
    finally:
        cap.release()


def enumerate_supported_capture_resolutions(
    device_index: int,
    *,
    extra_sizes: tuple[tuple[int, int], ...] = (),
) -> list[tuple[int, int]]:
    """Return distinct resolutions the device accepts via CAP_PROP_FRAME_*.

    Probes each candidate size with :func:`_probe_one_capture_resolution`
    (separate capture session per candidate, safe ``read()`` handling).

    Args:
        device_index: OpenCV ``VideoCapture`` index.
        extra_sizes: Optional (width, height) pairs to probe in addition to
            the built-in candidate list (e.g. current workspace size).
    """
    seen: set[tuple[int, int]] = set()
    ordered: list[tuple[int, int]] = []

    candidates: list[tuple[int, int]] = list(dict.fromkeys((*extra_sizes, *_CANDIDATE_CAPTURE_RESOLUTIONS)))

    for w, h in candidates:
        got = _probe_one_capture_resolution(device_index, w, h)
        if got is None:
            continue
        if got not in seen:
            seen.add(got)
            ordered.append(got)

    ordered.sort(key=lambda wh: wh[0] * wh[1], reverse=True)
    return ordered
