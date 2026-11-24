"""Record & strip raw UIA/CDP/KVM events into credential-free, PHI-free Playbook v2."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import re
from typing import Any

from med_autopractice.workflows.site_playbook import Playbook, SitePlaybook

_RE_ID_CARD = re.compile(r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_LABELED_NAME = re.compile(r"(?:患者|姓名|病人)\s*[:：]\s*([^\s,，。；;()\n]{2,8})")

_ACTION_TO_PRIMITIVE = {
    "click": "click",
    "dblclick": "double_click",
    "double_click": "double_click",
    "input": "input_text",
    "fill": "input_text",
    "type": "input_text",
    "input_text": "input_text",
    "select": "select_option",
    "select_option": "select_option",
    "keys": "keys",
    "hotkey": "keys",
    "read": "read_text",
    "read_text": "read_text",
    "table": "read_table",
    "read_table": "read_table",
    "scroll_collect": "scroll_collect",
    "dialog": "handle_dialog",
    "handle_dialog": "handle_dialog",
    "wait": "wait_state",
    "wait_state": "wait_state",
    "verify_subject": "verify_subject",
    "capture_evidence": "capture_evidence",
}


@dataclass(frozen=True)
class RecordedEvent:
    """Single UI interaction event captured from desktop UIA, browser CDP, or KVM session."""

    action: str
    intent: str = ""
    target: str | None = None
    selector: str | None = None
    region_hint: tuple[int, int, int, int] | None = None
    text_value: str | None = None
    text_param: str | None = None
    extract_key: str | None = None
    scope: str | None = None
    max_pages: int | None = None
    expect_text: str = "*"
    effect: str = "read_only"
    on_failure: str = "stop"
    evidence: str = "none"
    dialog_rules: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RecordedEvent":
        reg = raw.get("region_hint")
        region_tuple = tuple(int(v) for v in reg) if isinstance(reg, (list, tuple)) and len(reg) == 4 else None
        return cls(
            action=str(raw.get("action") or raw.get("primitive") or ""),
            intent=str(raw.get("intent") or ""),
            target=raw.get("target"),
            selector=raw.get("selector"),
            region_hint=region_tuple,
            text_value=raw.get("text_value") or raw.get("value"),
            text_param=raw.get("text_param"),
            extract_key=raw.get("extract_key"),
            scope=raw.get("scope"),
            max_pages=raw.get("max_pages"),
            expect_text=str(raw.get("expect_text") or "*"),
            effect=str(raw.get("effect") or "read_only"),
            on_failure=str(raw.get("on_failure") or "stop"),
            evidence=str(raw.get("evidence") or "none"),
            dialog_rules=tuple(raw.get("dialog_rules") or ()),
        )


def strip_phi_from_text(
    text: str,
    *,
    sensitive_tokens: Sequence[str] = (),
) -> str:
    """Remove direct patient identifiers/names/IDs before committing or storing templates."""
    clean = text
    for tok in sorted((t for t in sensitive_tokens if t and len(t) >= 2), key=len, reverse=True):
        clean = clean.replace(tok, "<REDACTED>")
    clean = _RE_ID_CARD.sub("<REDACTED_ID>", clean)
    clean = _RE_PHONE.sub("<REDACTED_PHONE>", clean)
    clean = _RE_LABELED_NAME.sub("患者:<REDACTED_NAME>", clean)
    return clean


def build_kvm_anchor_step(
    step_id: str,
    intent: str,
    anchor_word: str,
    *,
    region_hint: tuple[int, int, int, int] | None = None,
    expect_text: str = "*",
    effect: str = "read_only",
    on_failure: str = "reobserve",
    evidence: str = "screenshot_ref",
) -> dict[str, Any]:
    """Build a single-step unverified KVM OCR anchor step without coordinates hardcoding."""
    step: dict[str, Any] = {
        "id": step_id,
        "intent": intent,
        "primitive": "click",
        "target": anchor_word,
        "expect_text": expect_text,
        "effect": effect,
        "on_failure": on_failure,
        "evidence": evidence,
        "state": "unverified",
    }
    if region_hint is not None:
        step["region_hint"] = list(region_hint)
    return step


class PlaybookRecorder:
    """Convert raw recorded UI traces into sanitized, validated map-site-playbook.v2 JSON."""

    def __init__(
        self,
        *,
        site_id: str,
        playbook_id: str,
        title: str,
        systems: Sequence[str] = ("his_ui",),
        purpose: str = "Recorded clinical UI workflow",
        sensitive_tokens: Sequence[str] = (),
    ):
        self.site_id = site_id
        self.playbook_id = playbook_id
        self.title = title
        self.systems = tuple(systems)
        self.purpose = purpose
        self.sensitive_tokens = tuple(sensitive_tokens)

    def build_document(self, events: Iterable[RecordedEvent | Mapping[str, Any]]) -> dict[str, Any]:
        steps: list[dict[str, Any]] = [{
            "id": "verify_subject_before_workflow",
            "intent": "核对当前激活窗口患者与就诊身份",
            "primitive": "verify_subject",
            "expect_text": "*",
            "effect": "read_only",
            "on_failure": "stop",
            "evidence": "none",
            "state": "unverified",
        }]

        param_index = 0
        for idx, raw_ev in enumerate(events, start=1):
            ev = raw_ev if isinstance(raw_ev, RecordedEvent) else RecordedEvent.from_dict(raw_ev)
            primitive = _ACTION_TO_PRIMITIVE.get(ev.action)
            if not primitive:
                raise ValueError(f"unsupported recorded action: {ev.action!r}")

            intent = strip_phi_from_text(
                ev.intent or f"录制步骤 {idx}: {primitive}",
                sensitive_tokens=self.sensitive_tokens,
            )
            expect_text = strip_phi_from_text(ev.expect_text, sensitive_tokens=self.sensitive_tokens) or "*"
            target = strip_phi_from_text(ev.target, sensitive_tokens=self.sensitive_tokens) if ev.target else None

            step: dict[str, Any] = {
                "id": f"step_{idx}_{primitive}",
                "intent": intent,
                "primitive": primitive,
                "expect_text": expect_text,
                "effect": ev.effect if primitive != "input_text" or ev.effect != "read_only" else "draft_write",
                "on_failure": ev.on_failure or "stop",
                "evidence": ev.evidence or ("screenshot_ref" if ev.effect != "read_only" else "none"),
                # Recorded playbooks never self-promote to verified.
                "state": "unverified",
            }
            if target:
                step["target"] = target
            if ev.selector:
                step["selector"] = ev.selector
            if ev.region_hint is not None:
                step["region_hint"] = list(ev.region_hint)

            if primitive == "input_text":
                # Strip concrete clinical prose / raw text; emit placeholder text_param only.
                param_index += 1
                step["text_param"] = ev.text_param or ("draft_text" if param_index == 1 else f"param_{param_index}")
                step["effect"] = "draft_write" if ev.effect == "read_only" else ev.effect
                if step["expect_text"] == "*":
                    step["expect_text"] = "*"
            elif ev.text_param:
                step["text_param"] = ev.text_param

            if ev.extract_key:
                step["extract_key"] = ev.extract_key
            if ev.scope:
                step["scope"] = ev.scope
            if ev.max_pages is not None:
                step["max_pages"] = int(ev.max_pages)
            if ev.dialog_rules:
                cleaned_rules = []
                for rule in ev.dialog_rules:
                    cleaned_rules.append({
                        "trigger_text": strip_phi_from_text(str(rule["trigger_text"]), sensitive_tokens=self.sensitive_tokens),
                        "action": str(rule["action"]),
                        **({"target": strip_phi_from_text(str(rule["target"]), sensitive_tokens=self.sensitive_tokens)} if rule.get("target") else {}),
                    })
                step["dialog_rules"] = cleaned_rules

            steps.append(step)

        document: dict[str, Any] = {
            "schema_version": "map-site-playbook.v2",
            "playbook_id": self.playbook_id,
            "site_id": self.site_id,
            "title": strip_phi_from_text(self.title, sensitive_tokens=self.sensitive_tokens),
            "purpose": strip_phi_from_text(self.purpose, sensitive_tokens=self.sensitive_tokens),
            "systems": list(self.systems),
            "steps": steps,
        }
        # Validate against official SitePlaybook parser before returning.
        SitePlaybook(document)
        return document

    def build_playbook(self, events: Iterable[RecordedEvent | Mapping[str, Any]]) -> Playbook:
        return SitePlaybook(self.build_document(events)).playbook
