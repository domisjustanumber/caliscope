"""Tests for probing capture resolution support."""

from __future__ import annotations

import numpy as np

import caliscope.recording.capture_devices as cd


def test_enumerate_supported_capture_resolutions_collects_distinct_sizes(monkeypatch) -> None:
    """Fake VideoCapture returns frames matching requested width/height."""

    class FakeCap:
        def __init__(self, _index: int) -> None:
            self._w = 640
            self._h = 480
            self._opened = True

        def isOpened(self) -> bool:
            return self._opened

        def set(self, prop, value) -> bool:
            if prop == cd.cv2.CAP_PROP_FRAME_WIDTH:
                self._w = int(value)
            elif prop == cd.cv2.CAP_PROP_FRAME_HEIGHT:
                self._h = int(value)
            return True

        def read(self):
            frame = np.zeros((self._h, self._w, 3), dtype=np.uint8)
            return True, frame

        def release(self) -> None:
            self._opened = False

    monkeypatch.setattr(cd.cv2, "VideoCapture", lambda _i: FakeCap(_i))

    r = cd.enumerate_supported_capture_resolutions(0)
    assert (1920, 1080) in r
    assert (640, 480) in r
    assert r == sorted(r, key=lambda wh: wh[0] * wh[1], reverse=True)


def test_enumerate_supported_capture_resolutions_extra_sizes_first(monkeypatch) -> None:
    class FakeCap:
        def __init__(self, _index: int) -> None:
            self._w = 800
            self._h = 600
            self._opened = True

        def isOpened(self) -> bool:
            return self._opened

        def set(self, prop, value) -> bool:
            if prop == cd.cv2.CAP_PROP_FRAME_WIDTH:
                self._w = int(value)
            elif prop == cd.cv2.CAP_PROP_FRAME_HEIGHT:
                self._h = int(value)
            return True

        def read(self):
            frame = np.zeros((self._h, self._w, 3), dtype=np.uint8)
            return True, frame

        def release(self) -> None:
            self._opened = False

    monkeypatch.setattr(cd.cv2, "VideoCapture", lambda _i: FakeCap(_i))

    r = cd.enumerate_supported_capture_resolutions(0, extra_sizes=((1024, 576),))
    assert (1024, 576) in r


def test_enumerate_skips_resolution_when_read_raises_cv2_error(monkeypatch) -> None:
    """Bad driver frames can raise cv2.error inside VideoCapture.read; probe must not crash."""

    class SelectivelyBrokenCap:
        def __init__(self, _index: int) -> None:
            self._w = 640
            self._h = 480
            self._opened = True

        def isOpened(self) -> bool:
            return self._opened

        def set(self, prop, value) -> bool:
            if prop == cd.cv2.CAP_PROP_FRAME_WIDTH:
                self._w = int(value)
            elif prop == cd.cv2.CAP_PROP_FRAME_HEIGHT:
                self._h = int(value)
            return True

        def read(self):
            if self._w == 1920 and self._h == 1080:
                raise cd.cv2.error("OpenCV(-215) simulated Mat step failure")
            frame = np.zeros((self._h, self._w, 3), dtype=np.uint8)
            return True, frame

        def release(self) -> None:
            self._opened = False

    monkeypatch.setattr(cd.cv2, "VideoCapture", lambda _i: SelectivelyBrokenCap(_i))

    r = cd.enumerate_supported_capture_resolutions(0)
    assert (1920, 1080) not in r
    assert (640, 480) in r
