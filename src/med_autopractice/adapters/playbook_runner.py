"""Transport-agnostic declarative Playbook v1/v2 execution engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import time
from typing import Any, Protocol

from med_autopractice.adapters.ui import UIStateError
from med_autopractice.domain.task import SubjectRef
from med_autopractice.workflows.site_playbook import Playbook, PlaybookStep


class HumanInterruptError(UIStateError):
    """Raised immediately when clinician input preempts automation."""


@dataclass(frozen=True)
class ScreenObservation:
    frame_id: str
    text: str
    captured_at: str
    words: tuple[dict[str, Any], ...] = ()
    size: tuple[int, int] = (1280, 720)
    metadata: dict[str, Any] = field(default_factory=dict)

    def full_text(self) -> str:
        word_text = " ".join(str(w.get("text", "")).strip() for w in self.words if w.get("text"))
        if not word_text:
            return self.text
        if not self.text:
            return word_text
        if word_text == self.text.strip() or word_text in self.text:
            return self.text
        return f"{self.text}\n{word_text}"


@dataclass(frozen=True)
class TableData:
    headers: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    has_next: bool = False
    next_selector: str | None = None
    next_target: str | None = None


class UIDriver(Protocol):
    """Unified channel driver contract shared by Computer Use and IP KVM."""

    def observe(self, region: tuple[int, int, int, int] | None = None) -> ScreenObservation: ...

    def locate(
        self,
        target: str | None,
        *,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int, int, int] | str: ...

    def act(
        self,
        kind: str,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
        value: Any = None,
    ) -> ScreenObservation: ...

    def input_text(
        self,
        text: str,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
        subject: SubjectRef | None = None,
    ) -> ScreenObservation: ...

    def extract_table(
        self,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> TableData: ...

    def screenshot(
        self,
        *,
        region: tuple[int, int, int, int] | None = None,
        label: str | None = None,
    ) -> str: ...

    def check_human_interrupt(self) -> None: ...


@dataclass(frozen=True)
class StepTrace:
    step_id: str
    primitive: str
    outcome: str
    trace_ref: str
    observed_at: str
    screenshot_ref: str | None = None


@dataclass(frozen=True)
class PlaybookExecutionResult:
    playbook_id: str
    subject: SubjectRef
    extracted: dict[str, Any]
    extracted_scopes: dict[str, str]
    traces: tuple[StepTrace, ...]
    screenshot_refs: tuple[str, ...]
    observed_at: str
    observed_subject: SubjectRef | None = None


_PRIMITIVE_ALIASES: dict[str, str] = {
    "kvm_observe": "observe",
    "kvm_wait": "wait_state",
    "kvm_click": "click",
    "kvm_double_click": "double_click",
    "kvm_type_ascii": "input_text",
    "kvm_keys": "keys",
    "kvm_scroll": "scroll",
    "kvm_drag": "drag",
    "browser_autofill_select": "select_option",
    "human_action": "human_handoff",
}


def _token_present(text: str, token: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])", text))


def _verify_scoped_subject(obs: ScreenObservation, subject: SubjectRef) -> SubjectRef:
    meta = obs.metadata or {}
    if meta.get("patient_id") is not None and str(meta["patient_id"]).strip() != subject.patient_id:
        raise UIStateError("screen metadata patient_id does not match subject")
    if meta.get("encounter_id") is not None and str(meta["encounter_id"]).strip() != subject.encounter_id:
        raise UIStateError("screen metadata encounter_id does not match subject")

    text_blob = obs.full_text()
    if not _token_present(text_blob, subject.patient_id) or not _token_present(text_blob, subject.encounter_id):
        raise UIStateError("unique patient and encounter identifiers are not both visible")

    patient_mentions = {
        m.strip()
        for m in re.findall(r"(?:患者|病人|patient(?:_id)?)[：:\s=]+([A-Za-z0-9_-]+)", text_blob, re.IGNORECASE)
        if m.strip()
    }
    if len(patient_mentions) > 1 or (patient_mentions and patient_mentions != {subject.patient_id}):
        raise UIStateError("ambiguous or mismatched patient identifier visible on screen")

    encounter_mentions = {
        m.strip()
        for m in re.findall(r"(?:就诊|住院号|encounter(?:_id)?)[：:\s=]+([A-Za-z0-9_-]+)", text_blob, re.IGNORECASE)
        if m.strip()
    }
    if len(encounter_mentions) > 1 or (encounter_mentions and encounter_mentions != {subject.encounter_id}):
        raise UIStateError("ambiguous or mismatched encounter identifier visible on screen")

    return SubjectRef(subject.site_id, subject.patient_id, subject.encounter_id)


class PlaybookRunner:
    """Execute declarative Playbook v1/v2 steps safely against any UIDriver."""

    def __init__(
        self,
        driver: UIDriver,
        *,
        wait_timeout_sec: float = 2.0,
        poll_interval_sec: float = 0.05,
        handoff_handler: Callable[[PlaybookStep, ScreenObservation], ScreenObservation] | None = None,
        session_guard: Any | None = None,
    ):
        self.driver = driver
        self.wait_timeout_sec = wait_timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.handoff_handler = handoff_handler
        self.session_guard = session_guard

    def _check_session_observation(self, obs: ScreenObservation) -> None:
        if self.session_guard is None:
            return
        event = self.session_guard.inspect_observation(obs)
        if event is not None:
            from med_autopractice.adapters.session_guard import SessionLockedError

            raise SessionLockedError(
                f"hospital session locked or expired ({event.matched_keyword}); manual relogin required"
            )

    def run(
        self,
        playbook: Playbook,
        subject: SubjectRef,
        params: Mapping[str, Any] | None = None,
    ) -> PlaybookExecutionResult:
        param_map = dict(params or {})
        extracted: dict[str, Any] = {}
        extracted_scopes: dict[str, str] = {}
        traces: list[StepTrace] = []
        screenshots: list[str] = []
        subject_state: dict[str, SubjectRef | None] = {"observed": None}

        for step in playbook.steps:
            self.driver.check_human_interrupt()
            obs, step_shot = self._run_step_with_policy(
                step, subject, param_map, extracted, extracted_scopes, subject_state,
            )
            if step_shot:
                screenshots.append(step_shot)
            elif step.evidence == "screenshot_ref":
                step_shot = self.driver.screenshot(region=step.region_hint, label=step.id)
                screenshots.append(step_shot)
            traces.append(StepTrace(
                step_id=step.id,
                primitive=step.primitive,
                outcome="ok",
                trace_ref=f"ui://step/{playbook.playbook_id}/{step.id}/{obs.frame_id}",
                observed_at=obs.captured_at,
                screenshot_ref=step_shot,
            ))

        now_iso = datetime.now(timezone.utc).isoformat()
        return PlaybookExecutionResult(
            playbook_id=playbook.playbook_id,
            subject=subject,
            extracted=extracted,
            extracted_scopes=extracted_scopes,
            traces=tuple(traces),
            screenshot_refs=tuple(screenshots),
            observed_at=now_iso,
            observed_subject=subject_state["observed"],
        )

    def _run_step_with_policy(
        self,
        step: PlaybookStep,
        subject: SubjectRef,
        params: Mapping[str, Any],
        extracted: dict[str, Any],
        extracted_scopes: dict[str, str],
        subject_state: dict[str, SubjectRef | None],
    ) -> tuple[ScreenObservation, str | None]:
        from med_autopractice.adapters.session_guard import SessionLockedError

        attempts = 2 if step.on_failure == "retry_once" else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            self.driver.check_human_interrupt()
            if step.is_write and self.session_guard is not None:
                self.session_guard.assert_can_write()
            try:
                obs, shot = self._execute_single_step(
                    step, subject, params, extracted, extracted_scopes, subject_state,
                )
                self._check_session_observation(obs)
                if not self._matches_expectation(obs, step.expect_text):
                    if step.on_failure == "reobserve":
                        obs = self.driver.observe(step.region_hint)
                        self._check_session_observation(obs)
                    if not self._matches_expectation(obs, step.expect_text):
                        raise UIStateError(
                            f"step {step.id} expected text {step.expect_text!r} not observed"
                        )
                return obs, shot
            except (HumanInterruptError, SessionLockedError):
                raise
            except Exception as error:
                last_error = error
                if attempt + 1 < attempts:
                    continue
                if step.on_failure == "handoff":
                    raise HumanInterruptError(f"step {step.id} requires manual handoff: {error}") from error
                raise
        assert last_error is not None
        raise last_error

    @staticmethod
    def _matches_expectation(obs: ScreenObservation, expect_text: str) -> bool:
        if expect_text == "*":
            return True
        return expect_text in obs.full_text()

    def _execute_single_step(
        self,
        step: PlaybookStep,
        subject: SubjectRef,
        params: Mapping[str, Any],
        extracted: dict[str, Any],
        extracted_scopes: dict[str, str],
        subject_state: dict[str, SubjectRef | None],
    ) -> tuple[ScreenObservation, str | None]:
        op = _PRIMITIVE_ALIASES.get(step.primitive, step.primitive)
        if op == "observe":
            return self.driver.observe(step.region_hint), None

        if op == "locate":
            self.driver.locate(step.target, selector=step.selector, region_hint=step.region_hint)
            return self.driver.observe(step.region_hint), None

        if op in {"click", "double_click", "keys", "scroll", "drag", "select_option"}:
            val = params.get(step.text_param) if step.text_param else step.target
            obs = self.driver.act(
                op, target=step.target, selector=step.selector,
                region_hint=step.region_hint, value=val,
            )
            return obs, None

        if op == "wait_state":
            deadline = time.monotonic() + self.wait_timeout_sec
            obs = self.driver.observe(step.region_hint)
            self._check_session_observation(obs)
            while not self._matches_expectation(obs, step.expect_text):
                self.driver.check_human_interrupt()
                if time.monotonic() >= deadline:
                    raise UIStateError(f"wait_state timed out for {step.expect_text!r}")
                time.sleep(self.poll_interval_sec)
                obs = self.driver.observe(step.region_hint)
                self._check_session_observation(obs)
            return obs, None

        if op == "input_text":
            key = step.text_param or step.target or step.id
            raw_value = params.get(key, "")
            if not isinstance(raw_value, str) or not raw_value:
                raise UIStateError(f"missing text input parameter {key!r} for step {step.id}")
            obs = self.driver.input_text(
                raw_value, target=step.target, selector=step.selector,
                region_hint=step.region_hint, subject=subject,
            )
            return obs, None

        if op == "verify_subject":
            reader = getattr(self.driver, "read_field", None)
            if step.selector and callable(reader):
                obs = reader(target=step.target, selector=step.selector, region_hint=step.region_hint)
            else:
                obs = self.driver.observe(step.region_hint)
            self._check_session_observation(obs)
            verified = _verify_scoped_subject(obs, subject)
            subject_state["observed"] = verified
            return obs, None

        if op == "read_text":
            reader = getattr(self.driver, "read_field", None)
            if step.selector and callable(reader):
                obs = reader(target=step.target, selector=step.selector, region_hint=step.region_hint)
            else:
                obs = self.driver.observe(step.region_hint)
            key = step.extract_key or step.id
            extracted[key] = obs.full_text()
            if step.scope:
                extracted_scopes[key] = step.scope
            return obs, None

        if op == "read_table":
            table = self.driver.extract_table(
                target=step.target, selector=step.selector, region_hint=step.region_hint,
            )
            key = step.extract_key or step.id
            extracted[key] = {
                "headers": list(table.headers),
                "rows": [dict(r) for r in table.rows],
                "pages_read": 1,
                "truncated": bool(table.has_next),
            }
            if step.scope:
                extracted_scopes[key] = step.scope
            return self.driver.observe(step.region_hint), None

        if op == "scroll_collect":
            max_pages = step.max_pages or 10
            seen_rows: set[str] = set()
            collected_rows: list[dict[str, Any]] = []
            headers: tuple[str, ...] = ()
            pages_read = 0
            truncated = False
            obs = self.driver.observe(step.region_hint)
            for page in range(1, max_pages + 1):
                self.driver.check_human_interrupt()
                table = self.driver.extract_table(
                    target=step.target, selector=step.selector, region_hint=step.region_hint,
                )
                pages_read = page
                if not headers and table.headers:
                    headers = table.headers
                new_count = 0
                for row in table.rows:
                    sig = json.dumps(row, sort_keys=True, ensure_ascii=False)
                    if sig not in seen_rows:
                        seen_rows.add(sig)
                        collected_rows.append(dict(row))
                        new_count += 1
                if not table.has_next:
                    truncated = False
                    break
                if page == max_pages:
                    truncated = True
                    break
                next_sel = table.next_selector or step.selector
                next_tgt = table.next_target or step.target or "下一页"
                obs = self.driver.act(
                    "click" if (table.next_selector or table.next_target) else "scroll",
                    target=next_tgt, selector=next_sel, region_hint=step.region_hint,
                    value="down",
                )
                if new_count == 0 and not table.has_next:
                    break
            key = step.extract_key or step.id
            extracted[key] = {
                "headers": list(headers),
                "rows": collected_rows,
                "pages_read": pages_read,
                "truncated": truncated,
            }
            if step.scope:
                extracted_scopes[key] = step.scope
            return obs, None

        if op == "handle_dialog":
            obs = self.driver.observe(step.region_hint)
            full = obs.full_text()
            for rule in step.dialog_rules:
                trigger = rule.get("trigger_text", "")
                if trigger and trigger in full:
                    action = rule.get("action", "dismiss")
                    if action == "handoff":
                        raise HumanInterruptError(f"dialog {trigger!r} requires clinician handoff")
                    obs = self.driver.act(
                        "click", target=rule.get("target") or trigger, region_hint=step.region_hint,
                    )
                    full = obs.full_text()
            return obs, None

        if op == "capture_evidence":
            shot_ref = self.driver.screenshot(region=step.region_hint, label=step.id)
            obs = self.driver.observe(step.region_hint)
            key = step.extract_key or f"{step.id}_screenshot_ref"
            extracted[key] = shot_ref
            return obs, shot_ref

        if op == "human_handoff":
            obs = self.driver.observe(step.region_hint)
            if self.handoff_handler is not None:
                return self.handoff_handler(step, obs), None
            raise HumanInterruptError(f"step {step.id} requires clinician handoff")

        raise UIStateError(f"unsupported playbook primitive: {step.primitive}")
