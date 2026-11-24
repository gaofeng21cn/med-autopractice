"""Track A (Ideal Form): Windows UI Automation desktop driver for local HIS/EMR clients."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from med_autopractice.adapters.playbook_runner import (
    HumanInterruptError, ScreenObservation, TableData,
)
from med_autopractice.adapters.ui import UIStateError
from med_autopractice.domain.task import SubjectRef


class DesktopWinBackend(Protocol):
    """Abstracted Win32/UIA backend (lazy-loaded on Windows or injected for tests)."""

    def snapshot_window(self, region: tuple[int, int, int, int] | None = None) -> ScreenObservation: ...

    def locate_control(
        self,
        target: str | None,
        *,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> str: ...

    def perform_action(
        self,
        kind: str,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
        value: Any = None,
    ) -> ScreenObservation: ...

    def set_value(
        self, text: str, *, target: str | None = None, selector: str | None = None
    ) -> bool: ...

    def get_control_text(self, *, target: str | None = None, selector: str | None = None) -> str: ...

    def get_clipboard(self) -> str: ...

    def set_clipboard(self, text: str) -> None: ...

    def paste_hotkey(self, *, target: str | None = None, selector: str | None = None) -> None: ...

    def read_table(
        self, *, target: str | None = None, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> TableData: ...

    def capture_screenshot(
        self, destination: Path, *, region: tuple[int, int, int, int] | None = None
    ) -> None: ...

    def user_input_detected(self) -> bool: ...


class _LazyWin32UIABackend:
    """Native Windows UIA + Win32 clipboard/input hook backend (imported only on demand)."""

    def __init__(self, window_title_hint: str | None = None):
        if os.name != "nt":
            raise RuntimeError("WindowsUIADriver native Win32 backend requires Windows")
        try:
            import uiautomation as auto  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError(
                "install optional dependency 'win-desktop' (pip install med-autopractice[win-desktop])"
            ) from error
        self.auto = auto
        self.window_title_hint = window_title_hint
        self._last_input_tick = self._current_input_tick()

    @staticmethod
    def _current_input_tick() -> int:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
        return 0

    def user_input_detected(self) -> bool:
        current = self._current_input_tick()
        return current > self._last_input_tick

    def _mark_automation_input(self) -> None:
        self._last_input_tick = self._current_input_tick()

    def _window(self):
        if self.window_title_hint:
            win = self.auto.WindowControl(searchDepth=1, SubName=self.window_title_hint)
            if win.Exists(1, 0.2):
                return win
        return self.auto.GetForegroundControl()

    def _find(self, target: str | None, selector: str | None):
        win = self._window()
        if selector and selector.startswith("id="):
            ctrl = win.Control(AutomationId=selector[3:])
        else:
            name = selector or target or ""
            ctrl = win.Control(Name=name)
        if not ctrl.Exists(1, 0.2):
            raise UIStateError(f"UIA control not found: {selector or target!r}")
        return ctrl

    def snapshot_window(self, region: tuple[int, int, int, int] | None = None) -> ScreenObservation:
        win = self._window()
        texts: list[str] = []
        for ctrl, _depth in self.auto.WalkControl(win, maxDepth=6):
            val = getattr(ctrl, "Name", "") or ""
            if val:
                texts.append(val)
        return ScreenObservation(
            frame_id="uia-" + uuid4().hex[:8],
            text="\n".join(texts),
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    def locate_control(
        self, target: str | None, *, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> str:
        ctrl = self._find(target, selector)
        return ctrl.AutomationId or ctrl.Name or (selector or target or "")

    def perform_action(
        self, kind: str, *, target: str | None = None, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None, value: Any = None,
    ) -> ScreenObservation:
        ctrl = self._find(target, selector)
        if kind == "double_click":
            ctrl.DoubleClick()
        elif kind == "select_option":
            ctrl.Select(str(value or ""))
        elif kind == "keys":
            self.auto.SendKeys(str(value or ""))
        else:
            ctrl.Click()
        self._mark_automation_input()
        return self.snapshot_window(region_hint)

    def set_value(self, text: str, *, target: str | None = None, selector: str | None = None) -> bool:
        ctrl = self._find(target, selector)
        pattern = ctrl.GetValuePattern()
        if pattern is None:
            return False
        pattern.SetValue(text)
        self._mark_automation_input()
        return True

    def get_control_text(self, *, target: str | None = None, selector: str | None = None) -> str:
        ctrl = self._find(target, selector)
        pattern = ctrl.GetValuePattern()
        if pattern is not None:
            return str(pattern.Value or "")
        return str(ctrl.Name or "")

    def get_clipboard(self) -> str:
        return str(self.auto.GetClipboardText() or "")

    def set_clipboard(self, text: str) -> None:
        self.auto.SetClipboardText(text)

    def paste_hotkey(self, *, target: str | None = None, selector: str | None = None) -> None:
        ctrl = self._find(target, selector)
        ctrl.Click()
        self.auto.SendKeys("{Ctrl}a{Ctrl}v")
        self._mark_automation_input()

    def read_table(
        self, *, target: str | None = None, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> TableData:
        ctrl = self._find(target, selector)
        rows = []
        for child in ctrl.GetChildren():
            cells = [c.Name for c in child.GetChildren() if c.Name]
            if cells:
                rows.append({"cells": cells, "summary": " | ".join(cells)})
        return TableData(headers=(), rows=tuple(rows), has_next=False)

    def capture_screenshot(
        self, destination: Path, *, region: tuple[int, int, int, int] | None = None
    ) -> None:
        win = self._window()
        win.CaptureToImage(str(destination))


class WindowsUIADriver:
    """UIDriver for direct execution on clinician Windows workstations."""

    def __init__(
        self,
        *,
        backend: DesktopWinBackend | None = None,
        screenshot_dir: Path | None = None,
        window_title_hint: str | None = None,
    ):
        self._backend = backend
        self._window_title_hint = window_title_hint
        self.screenshot_dir = screenshot_dir

    def _get_backend(self) -> DesktopWinBackend:
        if self._backend is None:
            self._backend = _LazyWin32UIABackend(self._window_title_hint)
        return self._backend

    def check_human_interrupt(self) -> None:
        if self._get_backend().user_input_detected():
            raise HumanInterruptError("clinician input detected; desktop automation lease revoked")

    def observe(self, region: tuple[int, int, int, int] | None = None) -> ScreenObservation:
        self.check_human_interrupt()
        return self._get_backend().snapshot_window(region)

    def read_field(
        self,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> ScreenObservation:
        self.check_human_interrupt()
        backend = self._get_backend()
        value = backend.get_control_text(target=target, selector=selector)
        return ScreenObservation(
            frame_id="uia-field-" + uuid4().hex[:8],
            text=value,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    def locate(
        self, target: str | None, *, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> str:
        self.check_human_interrupt()
        return self._get_backend().locate_control(target, selector=selector, region_hint=region_hint)

    def act(
        self, kind: str, *, target: str | None = None, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None, value: Any = None,
    ) -> ScreenObservation:
        self.check_human_interrupt()
        return self._get_backend().perform_action(
            kind, target=target, selector=selector, region_hint=region_hint, value=value,
        )

    def input_text(
        self,
        text: str,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
        subject: SubjectRef | None = None,
    ) -> ScreenObservation:
        self.check_human_interrupt()
        backend = self._get_backend()
        if subject is not None:
            obs = backend.snapshot_window(None)
            blob = obs.full_text()
            if subject.patient_id not in blob or subject.encounter_id not in blob:
                raise UIStateError("unique patient and encounter identifiers are not both visible")
        # Try ValuePattern first; fall back to atomic clipboard backup/paste/readback/restore.
        used_direct = backend.set_value(text, target=target, selector=selector)
        if not used_direct:
            saved_clipboard = backend.get_clipboard()
            try:
                backend.set_clipboard(text)
                backend.paste_hotkey(target=target, selector=selector)
            finally:
                backend.set_clipboard(saved_clipboard)
        self.check_human_interrupt()
        readback = backend.get_control_text(target=target, selector=selector)
        if readback != text:
            raise UIStateError("UIA readback differs from entered clinical text")
        return backend.snapshot_window(region_hint)

    def extract_table(
        self, *, target: str | None = None, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> TableData:
        self.check_human_interrupt()
        return self._get_backend().read_table(target=target, selector=selector, region_hint=region_hint)

    def screenshot(
        self, *, region: tuple[int, int, int, int] | None = None, label: str | None = None
    ) -> str:
        self.check_human_interrupt()
        directory = self.screenshot_dir or Path.cwd()
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (label or "shot"))
        dest = directory / f"uia-{safe_label}-{uuid4().hex[:8]}.png"
        self._get_backend().capture_screenshot(dest, region=region)
        return str(dest)
