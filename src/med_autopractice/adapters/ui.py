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

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback:
        visible = self.ui.open_object(
            task.request.subject, task.request.capability_id, receipt.object_ref,
        )
        digest = task.proposal.digest if task.proposal and visible.payload == task.proposal.payload else ""
        return SystemReadback(
            visible.subject, visible.object_ref, digest, visible.state,
            visible.screenshot_ref, visible.trace_refs, visible.payload,
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
