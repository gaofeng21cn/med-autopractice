"""Synthetic HIS sandbox simulating Windows UIA, Browser CDP and IP KVM (pure HID/OCR) simultaneously."""

from __future__ import annotations

from datetime import datetime, timezone
import json
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
        simulate_save_timeout: bool = False,
        simulate_partial_order_failure_index: int | None = None,
        session_locked: bool = False,
        wrong_readback_encounter: str | None = None,
        initial_orders: list[dict[str, Any]] | None = None,
    ):
        self.site_id = site_id
        self.patient_id = patient_id
        self.encounter_id = encounter_id
        self.page_index = 0
        self.dialog_open = True
        self.human_interrupted = False
        self.allow_kvm_clipboard = allow_kvm_clipboard
        self.simulate_save_timeout = simulate_save_timeout
        self.simulate_partial_order_failure_index = simulate_partial_order_failure_index
        self.session_locked = session_locked
        self.wrong_readback_encounter = wrong_readback_encounter
        self.clipboard_value = "医生原有剪贴板内容"
        self.saved_notes: dict[str, dict[str, Any]] = {}
        self.saved_order_batches: dict[str, dict[str, Any]] = {}
        self.last_object_ref: str | None = None
        self.order_batch_ref: str | None = None
        self.editor_text = ""
        self.pending_orders_json = ""
        self.inject_hid_typo_on_segment = inject_hid_typo_on_segment
        self.hid_calls = 0
        self.shot_count = 0
        self.orders: list[dict[str, Any]] = list(
            initial_orders
            if initial_orders is not None
            else [
                {
                    "object_ref": "demo://his/order/ord-1",
                    "code": "DEMO-MED",
                    "dose": "500",
                    "unit": "mg",
                    "route": "po",
                    "frequency": "bid",
                    "status": "active",
                },
                {
                    "object_ref": "demo://his/order/ord-2",
                    "code": "STOP-MED",
                    "dose": "10",
                    "unit": "mg",
                    "route": "po",
                    "frequency": "qd",
                    "status": "active",
                },
            ]
        )
        self.catalog_items: list[dict[str, Any]] = [
            {"fact_id": "cat-1", "code": "DEMO-MED", "value": "DEMO-MED 合成常用药", "item_type": "medication", "active": True, "source_ref": "demo://catalog/DEMO-MED"},
            {"fact_id": "cat-2", "code": "STOP-MED", "value": "STOP-MED 合成停用药", "item_type": "medication", "active": True, "source_ref": "demo://catalog/STOP-MED"},
            {"fact_id": "cat-3", "code": "NEW-MED", "value": "NEW-MED 合成新增药", "item_type": "medication", "active": True, "source_ref": "demo://catalog/NEW-MED"},
        ]
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
        if self.session_locked:
            return "提示：会话已过期，请重新登录"
        dialog = " 提示弹窗 确定" if self.dialog_open else ""
        return f"住院病历工作站 患者:{self.patient_id} 就诊:{self.encounter_id} 保存草稿 提交医嘱 下一页{dialog}"

    def _words(self) -> list[dict[str, Any]]:
        if self.session_locked:
            return [
                {"text": "会话已过期", "confidence": 99, "box": [200, 200, 120, 24]},
                {"text": "请重新登录", "confidence": 99, "box": [200, 240, 120, 24]},
            ]
        active_ref = self.order_batch_ref or self.last_object_ref
        active_state = "已录入" if self.order_batch_ref else ("已保存" if self.last_object_ref else None)
        words = [
            {"text": self.patient_id, "confidence": 99, "box": [10, 10, 120, 20]},
            {"text": self.encounter_id, "confidence": 99, "box": [140, 10, 120, 20]},
            {"text": "住院病历工作站", "confidence": 99, "box": [10, 40, 160, 24]},
            {"text": "检验结果", "confidence": 99, "box": [200, 40, 90, 24]},
            {"text": "提示弹窗", "confidence": 98, "box": [200, 200, 90, 24]} if self.dialog_open else None,
            {"text": "确定", "confidence": 98, "box": [300, 200, 60, 24]} if self.dialog_open else None,
            {"text": "病程编辑器", "confidence": 98, "box": [40, 120, 120, 24]},
            {"text": "保存草稿", "confidence": 99, "box": [200, 420, 90, 28]},
            {"text": "提交医嘱", "confidence": 99, "box": [200, 455, 90, 28]},
            {"text": "下一页", "confidence": 97, "box": [500, 380, 60, 24]} if self.page_index == 0 else None,
            {"text": self.editor_text, "confidence": 96, "box": [40, 160, 500, 200]} if self.editor_text else None,
            {"text": active_state, "confidence": 98, "box": [320, 420, 70, 24]} if active_state else None,
            {"text": active_ref, "confidence": 99, "box": [320, 452, 220, 20]} if active_ref else None,
        ]
        return [w for w in words if w is not None]

    def _words_in_region(self, region: tuple[int, int, int, int]) -> list[dict[str, Any]]:
        rx1, ry1, rx2, ry2 = region
        return [
            w for w in self._words()
            if rx1 <= w["box"][0] < rx2 and ry1 <= w["box"][1] < ry2
        ]

    def _region_text(self, region: tuple[int, int, int, int], words: list[dict[str, Any]]) -> str:
        if self.session_locked:
            return "提示：会话已过期，请重新登录"
        if region == (40, 160, 540, 360):
            return self.editor_text
        return " ".join(str(w.get("text", "")).strip() for w in words if w.get("text"))

    def _apply_pending_orders(self) -> str:
        changes = json.loads(self.pending_orders_json or "[]")
        for idx, ch in enumerate(changes):
            if self.simulate_partial_order_failure_index is not None and idx == self.simulate_partial_order_failure_index:
                continue
            action = ch.get("action")
            code = ch.get("code")
            if action == "add":
                new_ref = f"demo://his/order/ord-{len(self.orders) + 1}"
                self.orders.append({
                    "object_ref": new_ref,
                    "code": code,
                    "dose": ch.get("dose", ""),
                    "unit": ch.get("unit", ""),
                    "route": ch.get("route", ""),
                    "frequency": ch.get("frequency", ""),
                    "instruction": ch.get("instruction", ""),
                    "status": "entered",
                })
            elif action == "change":
                for row in self.orders:
                    if row.get("object_ref") == ch.get("target_ref") and row.get("code") == code:
                        for field in ("dose", "unit", "route", "frequency", "instruction"):
                            if field in ch and ch[field] is not None:
                                row[field] = ch[field]
                        row["status"] = "entered"
                        break
            elif action == "stop":
                for row in self.orders:
                    if row.get("object_ref") == ch.get("target_ref") and row.get("code") == code:
                        row["status"] = "stopped"
                        break
        ref = f"demo://his/orders/batch/{len(self.saved_order_batches) + 1}"
        self.order_batch_ref = ref
        self.saved_order_batches[ref] = {
            "object_ref": ref,
            "patient_id": self.patient_id,
            "encounter_id": self.encounter_id,
            "state": "entered",
            "rows": [dict(r) for r in self.orders],
        }
        return ref

    # --- DesktopWinBackend ---
    def snapshot_window(self, region=None) -> ScreenObservation:
        if self.session_locked:
            return ScreenObservation(
                frame_id="sandbox-uia-locked",
                text="提示：会话已过期，请重新登录",
                captured_at=datetime.now(timezone.utc).isoformat(),
                words=tuple(self._words()),
            )
        if region is not None:
            words = self._words_in_region(region)
            return ScreenObservation(
                frame_id="sandbox-uia-region",
                text=self._region_text(region, words),
                captured_at=datetime.now(timezone.utc).isoformat(),
                words=tuple(words),
            )
        state_text = "已录入" if self.order_batch_ref else ("已保存" if self.last_object_ref else "")
        return ScreenObservation(
            frame_id="sandbox-uia-1",
            text=f"{self._banner()}\n{self.editor_text}\n{state_text}",
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
            ref = f"demo://his/note/{len(self.saved_notes) + 1}"
            self.last_object_ref = ref
            self.saved_notes[ref] = {
                "object_ref": ref,
                "patient_id": self.patient_id,
                "encounter_id": self.encounter_id,
                "state": "saved_draft",
                "draft_text": self.editor_text,
            }
            if self.wrong_readback_encounter:
                self.encounter_id = self.wrong_readback_encounter
            if self.simulate_save_timeout:
                raise TimeoutError("HIS save response timed out after commit")
        elif target == "提交医嘱" or selector == "#submit-orders":
            self._apply_pending_orders()
            if self.wrong_readback_encounter:
                self.encounter_id = self.wrong_readback_encounter
            if self.simulate_save_timeout:
                raise TimeoutError("HIS order submit response timed out after commit")
        return self.snapshot_window(region_hint)

    def set_value(self, text: str, *, target=None, selector=None) -> bool:
        if selector in {"#orders-editor", "textarea#orders"} or target == "医嘱编辑器":
            self.pending_orders_json = text
            return True
        return False  # Exercise atomic clipboard backup/paste/readback/restore path for notes!

    def get_control_text(self, *, target=None, selector=None) -> str:
        if self.session_locked:
            return "提示：会话已过期，请重新登录"
        if selector == "#patient-header":
            return f"住院病历工作站 患者:{self.patient_id} 就诊:{self.encounter_id}"
        if selector == "#object-ref":
            return self.order_batch_ref or self.last_object_ref or ""
        if selector == "#object-state":
            if self.order_batch_ref:
                return "已录入"
            if self.last_object_ref:
                return "已保存"
            return ""
        if selector in {"#orders-editor", "textarea#orders"} or target == "医嘱编辑器":
            return self.pending_orders_json
        return self.editor_text

    def get_clipboard(self) -> str:
        return self.clipboard_value

    def set_clipboard(self, text: str) -> bool:
        self.clipboard_value = text
        return self.allow_kvm_clipboard

    def paste_hotkey(self, *, target=None, selector=None) -> None:
        if selector in {"#orders-editor", "textarea#orders"} or target == "医嘱编辑器":
            self.pending_orders_json = self.clipboard_value
        else:
            self.editor_text = self.clipboard_value

    def read_table(self, *, target=None, selector=None, region_hint=None) -> TableData:
        if selector == "#orders-table" or target == "医嘱列表":
            return TableData(
                headers=("object_ref", "code", "dose", "unit", "route", "frequency", "status"),
                rows=tuple(dict(r) for r in self.orders),
                has_next=False,
            )
        if selector == "#current-orders-table":
            rows = tuple(
                {
                    "fact_id": f"ord-{idx + 1}",
                    "object_ref": r["object_ref"],
                    "code": r["code"],
                    "value": f"{r['code']} {r.get('dose', '')}{r.get('unit', '')} {r.get('route', '')} {r.get('frequency', '')}".strip(),
                    "kind": "current_order",
                    "source_ref": r["object_ref"],
                    "status": "observed",
                }
                for idx, r in enumerate(self.orders)
            )
            return TableData(headers=("fact_id", "object_ref", "code", "value"), rows=rows, has_next=False)
        if selector == "#catalog-table":
            return TableData(
                headers=("fact_id", "code", "value", "item_type", "active"),
                rows=tuple(
                    {**c, "kind": "catalog_item", "status": "observed"}
                    for c in self.catalog_items
                ),
                has_next=False,
            )
        if selector == "#saved-notes-table":
            return TableData(
                headers=("object_ref", "patient_id", "encounter_id", "state", "draft_text"),
                rows=tuple(dict(v) for v in self.saved_notes.values()),
                has_next=False,
            )
        if selector == "#order-batches-table":
            return TableData(
                headers=("object_ref", "patient_id", "encounter_id", "state", "rows"),
                rows=tuple(dict(v) for v in self.saved_order_batches.values()),
                has_next=False,
            )
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
        if selector in {"#patient-header", "#object-ref", "#object-state", "#note-editor", "textarea#note", "#orders-editor", "textarea#orders"}:
            return ScreenObservation(
                frame_id=f"sandbox-dom-{selector.strip('#')}",
                text=self.get_control_text(selector=selector),
                captured_at=datetime.now(timezone.utc).isoformat(),
            )
        return self.snapshot_window()

    def locate_element(self, selector=None, *, text=None) -> str:
        return selector or str(text or "")

    def perform(self, kind, *, selector=None, text=None, value=None) -> ScreenObservation:
        return self.perform_action(kind, target=text, selector=selector, value=value)

    def fill_and_readback(self, selector: str, value: str) -> str:
        if selector in {"#orders-editor", "textarea#orders"}:
            self.pending_orders_json = value
            return self.pending_orders_json
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
                "text": self._region_text(region, words),
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
