"""Synthetic HIS sandbox simulating Windows UIA, Browser CDP and IP KVM (pure HID/OCR) simultaneously."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from med_autopractice.adapters.playbook_runner import ScreenObservation, TableData


class SyntheticHISSandbox:
    """In-memory synthetic hospital UI supporting Desktop UIA, Browser DOM, and IP KVM targets."""

    def __init__(
        self,
        *,
        site_id: str = "demo-local",
        patient_id: str = "synthetic-p01",
        encounter_id: str = "synthetic-e01",
        inject_hid_typo_on_segment: int | None = None,
        allow_kvm_clipboard: bool = False,
    ):
        self.site_id = site_id
        self.patient_id = patient_id
        self.encounter_id = encounter_id
        self.page_index = 0
        self.dialog_open = True
        self.human_interrupted = False
        self.allow_kvm_clipboard = allow_kvm_clipboard
        self.clipboard_value = "医生原有剪贴板内容"
        self.saved_notes: dict[str, str] = {}
        self.last_object_ref: str | None = None
        self.editor_text = ""
        self.inject_hid_typo_on_segment = inject_hid_typo_on_segment
        self.hid_calls = 0
        self.shot_count = 0
        self.lab_pages = [
            [
                {"fact_id": "lab-1", "test": "白细胞", "value": "WBC 11.2 10^9/L", "source_ref": "demo://lis/1"},
                {"fact_id": "lab-2", "test": "CRP", "value": "CRP 28.4 mg/L", "source_ref": "demo://lis/2"},
            ],
            [
                {"fact_id": "lab-3", "test": "降钙素原", "value": "PCT 0.45 ng/mL", "source_ref": "demo://lis/3"},
            ],
        ]

    def _banner(self) -> str:
        dialog = " 提示弹窗 确定" if self.dialog_open else ""
        return f"住院病历工作站 患者:{self.patient_id} 就诊:{self.encounter_id} 保存草稿 下一页{dialog}"

    def _words(self) -> list[dict[str, Any]]:
        words = [
            {"text": self.patient_id, "confidence": 99, "box": [10, 10, 120, 20]},
            {"text": self.encounter_id, "confidence": 99, "box": [140, 10, 120, 20]},
            {"text": "住院病历工作站", "confidence": 99, "box": [10, 40, 160, 24]},
            {"text": "检验结果", "confidence": 99, "box": [200, 40, 90, 24]},
            {"text": "提示弹窗", "confidence": 98, "box": [200, 200, 90, 24]} if self.dialog_open else None,
            {"text": "确定", "confidence": 98, "box": [300, 200, 60, 24]} if self.dialog_open else None,
            {"text": "病程编辑器", "confidence": 98, "box": [40, 120, 120, 24]},
            {"text": "保存草稿", "confidence": 99, "box": [200, 420, 90, 28]},
            {"text": "下一页", "confidence": 97, "box": [500, 380, 60, 24]} if self.page_index == 0 else None,
            {"text": self.editor_text, "confidence": 96, "box": [40, 160, 500, 200]} if self.editor_text else None,
            {"text": "已保存", "confidence": 98, "box": [320, 420, 70, 28]} if self.last_object_ref else None,
        ]
        return [w for w in words if w is not None]

    def _words_in_region(self, region: tuple[int, int, int, int]) -> list[dict[str, Any]]:
        rx1, ry1, rx2, ry2 = region
        return [
            w for w in self._words()
            if rx1 <= w["box"][0] < rx2 and ry1 <= w["box"][1] < ry2
        ]

    # --- DesktopWinBackend ---
    def snapshot_window(self, region=None) -> ScreenObservation:
        if region is not None:
            words = self._words_in_region(region)
            return ScreenObservation(
                frame_id="sandbox-uia-region",
                text=self.editor_text,
                captured_at=datetime.now(timezone.utc).isoformat(),
                words=tuple(words),
            )
        return ScreenObservation(
            frame_id="sandbox-uia-1",
            text=f"{self._banner()}\n{self.editor_text}\n{'已保存' if self.last_object_ref else ''}",
            captured_at=datetime.now(timezone.utc).isoformat(),
            words=tuple(self._words()),
        )

    def locate_control(self, target, *, selector=None, region_hint=None) -> str:
        return selector or target or ""

    def perform_action(self, kind, *, target=None, selector=None, region_hint=None, value=None) -> ScreenObservation:
        if target == "确定" or selector == "#dialog-ok":
            self.dialog_open = False
        elif target == "检验结果" or selector == "#tab-labs":
            self.page_index = 0
        elif target == "下一页" or selector == "#next-page":
            self.page_index = min(1, self.page_index + 1)
        elif target == "保存草稿" or selector == "#save-draft":
            self.last_object_ref = f"demo://his/note/{len(self.saved_notes) + 1}"
            self.saved_notes[self.last_object_ref] = self.editor_text
        return self.snapshot_window(region_hint)

    def set_value(self, text: str, *, target=None, selector=None) -> bool:
        return False  # Exercise atomic clipboard backup/paste/readback/restore path!

    def get_control_text(self, *, target=None, selector=None) -> str:
        return self.editor_text

    def get_clipboard(self) -> str:
        return self.clipboard_value

    def set_clipboard(self, text: str) -> bool:
        self.clipboard_value = text
        return self.allow_kvm_clipboard

    def paste_hotkey(self, *, target=None, selector=None) -> None:
        self.editor_text = self.clipboard_value

    def read_table(self, *, target=None, selector=None, region_hint=None) -> TableData:
        rows = tuple(self.lab_pages[self.page_index])
        has_next = self.page_index < len(self.lab_pages) - 1
        return TableData(
            headers=("fact_id", "test", "value"),
            rows=rows,
            has_next=has_next,
            next_selector="#next-page" if has_next else None,
            next_target="下一页" if has_next else None,
        )

    def capture_screenshot(self, destination, *, region=None) -> None:
        self.shot_count += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")

    def user_input_detected(self) -> bool:
        return self.human_interrupted

    # --- BrowserCDPBackend ---
    def read_dom_text(self, selector=None) -> ScreenObservation:
        if selector in {"#note-editor", "textarea#note"}:
            return ScreenObservation(
                frame_id="sandbox-dom-editor",
                text=self.editor_text,
                captured_at=datetime.now(timezone.utc).isoformat(),
            )
        return self.snapshot_window()

    def locate_element(self, selector=None, *, text=None) -> str:
        return selector or str(text or "")

    def perform(self, kind, *, selector=None, text=None, value=None) -> ScreenObservation:
        return self.perform_action(kind, target=text, selector=selector, value=value)

    def fill_and_readback(self, selector: str, value: str) -> str:
        self.editor_text = value
        return self.editor_text

    def read_dom_table(self, selector: str) -> TableData:
        return self.read_table(selector=selector)

    def captured_xhr(self) -> tuple[dict[str, Any], ...]:
        return ({"url": "/api/labs", "status": 200, "json": {"items": self.lab_pages[self.page_index]}},)

    def screenshot_page(self, destination) -> None:
        self.capture_screenshot(destination)

    def user_interrupted(self) -> bool:
        return self.human_interrupted

    # --- KVMTransport ---
    def status(self) -> dict[str, Any]:
        return {"connected": True, "control_mode": "human_takeover" if self.human_interrupted else "agent"}

    def grab_frame(self, *, region=None, ocr=True, include_image=False) -> dict[str, Any]:
        if region is not None:
            words = self._words_in_region(region)
            return {
                "frame_id": "kvm-frame-region",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "original_size": [1280, 720],
                "image": b"synthetic-frame" if include_image else None,
                "ocr": {"words": words},
                "text": self.editor_text,
            }
        rows = self.lab_pages[self.page_index]
        has_next = self.page_index < len(self.lab_pages) - 1
        return {
            "frame_id": "kvm-frame-1",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "original_size": [1280, 720],
            "image": b"synthetic-frame" if include_image else None,
            "ocr": {"words": self._words()},
            "table": {"headers": ["fact_id", "test", "value"], "rows": rows, "has_next": has_next, "next_target": "下一页" if has_next else None},
        }

    def click(self, x: int, y: int, *, frame_id=None) -> dict[str, Any]:
        for word in self._words():
            bx, by, bw, bh = word["box"]
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.perform_action("click", target=word["text"])
                break
        return self.grab_frame()

    def double_click(self, x: int, y: int, *, frame_id=None) -> dict[str, Any]:
        return self.click(x, y, frame_id=frame_id)

    def send_keys(self, keys: str, *, frame_id=None) -> dict[str, Any]:
        if keys == "Ctrl+v":
            self.editor_text = self.clipboard_value
        return self.grab_frame()

    def send_text_hid(self, text: str, *, frame_id=None) -> dict[str, Any]:
        self.hid_calls += 1
        if self.inject_hid_typo_on_segment is not None and self.hid_calls == self.inject_hid_typo_on_segment:
            # Simulate an IME candidate typo that Strategy B must detect via OCR and backspace-correct.
            self.editor_text += text[:-1] + "错"
        else:
            self.editor_text += text
        return self.grab_frame()

    def backspace(self, count: int, *, frame_id=None) -> dict[str, Any]:
        if count > 0:
            self.editor_text = self.editor_text[:-count]
        return self.grab_frame()

    def scroll(self, delta: int, *, x=None, y=None, frame_id=None) -> dict[str, Any]:
        if delta < 0:
            self.page_index = min(1, self.page_index + 1)
        return self.grab_frame()

    def save_shot(self, destination, *, region=None, label=None) -> str:
        self.capture_screenshot(destination, region=region)
        return str(destination)
