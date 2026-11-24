"""Common clinical checks for Computer Use and KVM site adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from med_autopractice.domain.task import (
    ExecutionReceipt, Proposal, Snapshot, SubjectRef, SystemReadback, Task,
)


class UIStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisibleObject:
    subject: SubjectRef
    object_ref: str
    state: str
    payload: dict[str, Any]
    screenshot_ref: str
    trace_refs: tuple[str, ...]
    observed_at: str


@dataclass(frozen=True)
class UIProfile:
    site_id: str
    product: str
    version: str
    role: str
    display_size: tuple[int, int]
    qualification_ref: str
    write_capabilities: frozenset[str] = frozenset()
    long_text_qualified: bool = False


class HospitalUI(Protocol):
    """One tested hospital UI profile, implemented with an authorized desktop or browser session."""

    profile: UIProfile

    def collect(self, subject: SubjectRef, capability_id: str) -> Snapshot: ...

    def write(self, proposal: Proposal, existing_object_ref: str | None) -> ExecutionReceipt: ...

    def open_object(self, subject: SubjectRef, capability_id: str, object_ref: str) -> VisibleObject: ...

    def find_candidates(self, task: Task) -> tuple[VisibleObject, ...]: ...


class UIClinicalSystem:
    """Validate site UI results; an internal key is never sent to the GUI."""

    def __init__(self, ui: HospitalUI):
        profile = ui.profile
        if (not all((profile.site_id, profile.product, profile.version, profile.role,
                     profile.qualification_ref)) or min(profile.display_size) < 1):
            raise ValueError("hospital UI profile is not identified and qualified")
        self.ui = ui

    def read_context(self, subject: SubjectRef, capability_id: str) -> Snapshot:
        snapshot = self.ui.collect(subject, capability_id)
        if (subject.site_id != self.ui.profile.site_id
                or snapshot.subject != subject or not snapshot.revision
                or not snapshot.trace_refs or not snapshot.coverage):
            raise UIStateError("UI collection lacks identity, revision, trace, or coverage")
        return snapshot

    def can_apply(self, proposal: Proposal) -> bool:
        profile = self.ui.profile
        if proposal.subject.site_id != profile.site_id or proposal.capability_id not in profile.write_capabilities:
            return False
        return (proposal.capability_id not in {"write_inpatient_notes", "write_outpatient_notes"}
                or profile.long_text_qualified)

    def apply(
        self, proposal: Proposal, idempotency_key: str, existing_object_ref: str | None
    ) -> ExecutionReceipt:
        if not self.can_apply(proposal):
            raise UIStateError("this UI write or long-text entry has not been qualified")
        return self.ui.write(proposal, existing_object_ref)

    @staticmethod
    def _reconstruct_from_draft_text(actual: str, expected_payload: dict) -> dict | None:
        """Strictly parse raw GUI editor text and accept only an exact line-by-line match."""
        stripped = actual.strip()
        if not stripped:
            return None
        if expected_payload.get("type") == "note_draft":
            parsed_sections: list[tuple[str, list[str]]] = []
            current_name: str | None = None
            current_lines: list[str] = []
            for raw_line in stripped.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("[") and line.endswith("]") and len(line) > 2:
                    if current_name is not None:
                        parsed_sections.append((current_name, current_lines))
                    current_name = line[1:-1]
                    current_lines = []
                elif current_name is None:
                    return None
                else:
                    current_lines.append(line)
            if current_name is not None:
                parsed_sections.append((current_name, current_lines))

            expected_sections = expected_payload.get("sections", [])
            if len(parsed_sections) != len(expected_sections):
                return None
            reconstructed_sections = []
            for (sec_name, sec_lines), exp_sec in zip(parsed_sections, expected_sections):
                if sec_name != exp_sec.get("section"):
                    return None
                exp_entries = [e for e in exp_sec.get("entries", []) if e.get("text")]
                if len(sec_lines) != len(exp_entries):
                    return None
                reconstructed_entries = []
                for obs_line, exp_entry in zip(sec_lines, exp_entries):
                    if obs_line != str(exp_entry.get("text", "")).strip():
                        return None
                    reconstructed_entries.append(dict(exp_entry))
                reconstructed_sections.append({"section": sec_name, "entries": reconstructed_entries})
            return {"type": "note_draft", "sections": reconstructed_sections}
        if "draft" in expected_payload:
            if stripped == str(expected_payload["draft"]).strip():
                return {"draft": stripped}
        return None

    @staticmethod
    def _reconstruct_order_changes(visible_payload: dict, expected_payload: dict) -> dict | None:
        """Verify an observed HIS order table row-by-row against expected order changes."""
        exp_type = expected_payload.get("type")
        expected_changes = expected_payload.get("changes")
        if exp_type not in {"order_changes", "order_set"} or not isinstance(expected_changes, list) or not expected_changes:
            return None
        rows = visible_payload.get("rows")
        if not isinstance(rows, list):
            return None

        active_states = {"entered", "active", "pending", "verified"}
        stopped_states = {"stopped", "discontinued", "cancelled", "inactive"}
        order_fields = ("dose", "unit", "route", "frequency", "instruction", "duration", "quantity")
        used_indices: set[int] = set()
        reconstructed_changes: list[dict[str, Any]] = []

        for exp in expected_changes:
            if not isinstance(exp, dict):
                return None
            action = exp.get("action")
            code = str(exp.get("code") or "").strip()
            target_ref = exp.get("target_ref")
            if not code or action not in {"add", "change", "stop"}:
                return None

            matches: list[int] = []
            for idx, row in enumerate(rows):
                if idx in used_indices or not isinstance(row, dict):
                    continue
                row_code = str(row.get("code") or "").strip()
                row_ref = str(row.get("object_ref") or "").strip()
                row_status = str(row.get("status") or "").strip().lower()
                if row_code != code:
                    continue
                if action == "stop":
                    if target_ref and row_ref != str(target_ref):
                        continue
                    if row_status not in stopped_states:
                        continue
                    matches.append(idx)
                    continue
                if action == "change":
                    if not target_ref or row_ref != str(target_ref):
                        continue
                elif target_ref and row_ref != str(target_ref):
                    continue
                if row_status not in active_states:
                    continue
                if str(row.get("action") or "").strip() == "stop":
                    continue
                fields_ok = True
                for field in order_fields:
                    if field in exp and exp[field] is not None:
                        if str(row.get(field) or "").strip() != str(exp[field]).strip():
                            fields_ok = False
                            break
                if fields_ok:
                    matches.append(idx)

            if not matches:
                return None
            if action in {"change", "stop"} and len(matches) != 1:
                return None
            used_indices.add(matches[0])
            reconstructed_changes.append(dict(exp))

        reconstructed: dict[str, Any] = {"type": exp_type, "changes": reconstructed_changes}
        if exp_type == "order_set":
            for meta_key in ("name", "version", "effective_on"):
                if meta_key in expected_payload:
                    if visible_payload.get(meta_key) != expected_payload[meta_key]:
                        return None
                    reconstructed[meta_key] = visible_payload[meta_key]
        return reconstructed

    @classmethod
    def _match_visible_payload(cls, raw_payload: dict, expected_payload: dict) -> dict | None:
        if raw_payload == expected_payload:
            return raw_payload
        if set(raw_payload) == {"draft_text"}:
            return cls._reconstruct_from_draft_text(
                str(raw_payload.get("draft_text", "")),
                expected_payload,
            )
        if raw_payload.get("type") == "observed_order_table" or isinstance(raw_payload.get("rows"), list):
            return cls._reconstruct_order_changes(raw_payload, expected_payload)
        return None

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback:
        visible = self.ui.open_object(
            task.request.subject, task.request.capability_id, receipt.object_ref,
        )
        digest = ""
        visible_payload = visible.payload
        if (
            task.proposal
            and visible.subject == task.request.subject
            and visible.object_ref == receipt.object_ref
            and visible.state
        ):
            matched = self._match_visible_payload(visible.payload, task.proposal.payload)
            if matched is not None and matched == task.proposal.payload:
                digest = task.proposal.digest
                visible_payload = matched
        return SystemReadback(
            visible.subject, visible.object_ref, digest, visible.state,
            visible.screenshot_ref, visible.trace_refs, visible_payload,
            visible.observed_at,
        )

    def find_submission(self, task: Task) -> ExecutionReceipt | None:
        if task.proposal is None:
            return None
        exact: list[VisibleObject] = []
        for item in self.ui.find_candidates(task):
            if (item.subject != task.request.subject
                    or not item.object_ref or not item.trace_refs or not item.screenshot_ref):
                continue
            matched = self._match_visible_payload(item.payload, task.proposal.payload)
            if matched is not None and matched == task.proposal.payload:
                exact.append(item)
        if len(exact) != 1:
            return None
        item = exact[0]
        return ExecutionReceipt(item.object_ref, item.state, item.trace_refs)
