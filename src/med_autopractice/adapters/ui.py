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

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback:
        visible = self.ui.open_object(
            task.request.subject, task.request.capability_id, receipt.object_ref,
        )
        digest = ""
        visible_payload = visible.payload
        if task.proposal:
            if visible.payload == task.proposal.payload:
                digest = task.proposal.digest
            elif set(visible.payload) == {"draft_text"}:
                matched = self._reconstruct_from_draft_text(
                    str(visible.payload.get("draft_text", "")),
                    task.proposal.payload,
                )
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
        exact = [item for item in self.ui.find_candidates(task)
                 if item.subject == task.request.subject
                 and item.payload == task.proposal.payload
                 and item.object_ref and item.trace_refs and item.screenshot_ref]
        if len(exact) != 1:
            return None
        item = exact[0]
        return ExecutionReceipt(item.object_ref, item.state, item.trace_refs)
