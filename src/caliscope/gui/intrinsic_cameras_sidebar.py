"""Sidebar for intrinsic calibration: active project cameras and detected capture devices."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QPoint, QMimeData, QTimer, Qt, Signal
from PySide6.QtGui import QDrag, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from caliscope.gui.camera_list_widget import CAPTURE_DEVICE_MIME, ActiveCamerasListWidget
from caliscope.recording.capture_devices import (
    DetectedCaptureDevice,
    count_attached_capture_devices_without_opening,
    enumerate_capture_devices,
)

if TYPE_CHECKING:
    from caliscope.workspace_coordinator import WorkspaceCoordinator

logger = logging.getLogger(__name__)


class _DetectedCameraListWidget(QListWidget):
    """Lists enumerated VideoCapture indices; drag them into the active list."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag_start_pos: QPoint | None = None
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setStyleSheet("""
            QListWidget::item {
                padding: 6px 10px;
                min-height: 22px;
            }
            QListWidget::item:selected {
                background-color: #3a5f8a;
            }
        """)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start_pos is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < 12:
            super().mouseMoveEvent(event)
            return
        item = self.currentItem()
        if item is None:
            self._drag_start_pos = None
            super().mouseMoveEvent(event)
            return
        device_index = item.data(Qt.ItemDataRole.UserRole)
        if device_index is None:
            self._drag_start_pos = None
            super().mouseMoveEvent(event)
            return

        mime = QMimeData()
        mime.setData(CAPTURE_DEVICE_MIME, QByteArray(str(int(device_index)).encode("utf-8")))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)
        self._drag_start_pos = None


class IntrinsicCamerasSidebar(QWidget):
    """Active cameras (project) and host-detected capture devices."""

    camera_selected = Signal(int)
    live_device_drop_requested = Signal(int)

    def __init__(self, coordinator: WorkspaceCoordinator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._coordinator = coordinator

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        active_label = QLabel("Active cameras")
        active_label.setStyleSheet("color: #ccc; font-size: 11px; font-weight: bold;")
        layout.addWidget(active_label)

        self._active = ActiveCamerasListWidget(coordinator.camera_array)
        self._active.setMinimumHeight(120)
        self._active.camera_selected.connect(self.camera_selected.emit)
        self._active.live_device_drop_requested.connect(self.live_device_drop_requested.emit)
        layout.addWidget(self._active, stretch=1)

        detected_label = QLabel("Detected cameras")
        detected_label.setStyleSheet("color: #ccc; font-size: 11px; font-weight: bold;")
        layout.addWidget(detected_label)

        self._attached_count_label = QLabel()
        self._attached_count_label.setStyleSheet("color: #888; font-size: 10px;")
        self._attached_count_label.setWordWrap(True)
        layout.addWidget(self._attached_count_label)
        QTimer.singleShot(0, self._set_attached_camera_count_startup)

        hint = QLabel("Use Scan — then drag a row into Active cameras for live calibration.")
        hint.setStyleSheet("color: #888; font-size: 10px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        scan_btn = QPushButton("Scan for cameras…")
        scan_btn.setToolTip(
            "Briefly probes capture device indices.\nHardware is accessed only during this scan and when "
            "a device is dragged into Active cameras."
        )
        scan_btn.clicked.connect(self._refresh_detected_list)
        layout.addWidget(scan_btn)

        self._detected = _DetectedCameraListWidget()
        self._detected.setMinimumHeight(80)
        layout.addWidget(self._detected, stretch=1)

    @property
    def active_list(self) -> ActiveCamerasListWidget:
        return self._active

    def refresh_active_list(self) -> None:
        """Update Active cameras from the coordinator without probing capture hardware."""
        self._active.refresh(self._coordinator.camera_array)

    def _set_attached_camera_count_startup(self) -> None:
        """Host-reported attached camera count; never opens capture devices."""
        n = count_attached_capture_devices_without_opening()
        if n is None:
            self._attached_count_label.setText("Attached cameras (this system): could not detect count")
        elif n == 0:
            self._attached_count_label.setText("Attached cameras (this system): none reported")
        elif n == 1:
            self._attached_count_label.setText("Attached cameras (this system): 1")
        else:
            self._attached_count_label.setText(f"Attached cameras (this system): {n}")

    def remove_detected_device_row(self, device_index: int) -> None:
        """Remove one row from Detected cameras after assignment (no probe)."""
        for row in range(self._detected.count() - 1, -1, -1):
            item = self._detected.item(row)
            if item is None:
                continue
            if item.data(Qt.ItemDataRole.UserRole) == device_index:
                self._detected.takeItem(row)

    def _refresh_detected_list(self) -> None:
        try:
            devices = enumerate_capture_devices()
        except Exception as e:
            logger.debug("enumerate_capture_devices failed: %s", e)
            return

        used = {
            cam.live_device_index
            for cam in self._coordinator.camera_array.cameras.values()
            if cam.live_device_index is not None
        }
        available: list[DetectedCaptureDevice] = [d for d in devices if d.index not in used]

        self._detected.clear()
        for dev in available:
            item = QListWidgetItem(f"Device {dev.index} ({dev.size[0]}×{dev.size[1]})")
            item.setData(Qt.ItemDataRole.UserRole, dev.index)
            self._detected.addItem(item)
