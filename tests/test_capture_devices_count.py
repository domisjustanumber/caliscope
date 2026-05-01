"""Tests for host camera counting without VideoCapture probes."""

from __future__ import annotations

import subprocess

from caliscope.recording import capture_devices as cd


def test_count_without_opening_never_calls_videocapture(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("VideoCapture must not run for host count")

    monkeypatch.setattr(cd.cv2, "VideoCapture", boom)
    monkeypatch.setattr(cd.sys, "platform", "unsupported_os_xyz")
    assert cd.count_attached_capture_devices_without_opening() is None


def test_windows_powershell_parses_count(monkeypatch) -> None:
    monkeypatch.setattr(cd.sys, "platform", "win32")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "powershell"
        return subprocess.CompletedProcess(cmd, 0, stdout="4\r\n", stderr="")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd.count_attached_capture_devices_without_opening() == 4


def test_windows_nonzero_exit_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(cd.sys, "platform", "win32")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd.count_attached_capture_devices_without_opening() is None


def test_windows_non_numeric_stdout_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(cd.sys, "platform", "win32")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="unexpected", stderr="")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd.count_attached_capture_devices_without_opening() is None


def test_windows_parses_stdout_with_bom(monkeypatch) -> None:
    monkeypatch.setattr(cd.sys, "platform", "win32")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="\ufeff2\n", stderr="")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd.count_attached_capture_devices_without_opening() == 2
