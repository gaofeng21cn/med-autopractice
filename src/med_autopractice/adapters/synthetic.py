"""Synthetic-only clinical UI used to exercise the host entry end to end.

This adapter exists so the runtime boundary, task states and recovery paths can
be driven without a hospital system. It refuses every non-demo identity and is
never a deployment adapter; real UI behaviour belongs in a private site profile
outside this repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from med_autopractice.adapters.ui import UIClinicalSystem, UIProfile, VisibleObject
from med_autopractice.adapters.ports import ClinicalSystem
from med_autopractice.domain.task import (
    ExecutionReceipt, Fact, Proposal, Snapshot, SourceCoverage, SubjectRef, Task,
)


SYNTHETIC_SITE_ID = "demo-site"
NOTE_CAPABILITIES = frozenset({"write_inpatient_notes", "write_outpatient_notes"})
WRITE_CAPABILITIES = frozenset({
    "maintain_order_sets",
    "prepare_admission_orders",
    "revise_orders",
    "prepare_discharge_medication",
    "write_inpatient_notes",
    "write_outpatient_notes",
    "audit_discharge_records",
})
# A test asserts this covers every scope required by the workflow catalog so the
# synthetic snapshot cannot silently stop satisfying a new workflow.
SYNTHETIC_SCOPES = (
    "admission_plan",
    "allergies",
    "approved_protocol",
    "current_encounter",
    "current_order_set",
    "current_orders",
    "discharge_plan",
    "discharge_record",
    "document_status",
    "encounter_history",
    "local_catalog",
    "nursing",
    "patient_context",
    "results",
)


def require_synthetic_identity(subject: SubjectRef) -> None:
    if (not subject.site_id.startswith("demo-")
            or not subject.patient_id.startswith("synthetic-")
            or not subject.encounter_id.startswith("synthetic-")):
        raise ValueError(
            "the synthetic runtime only accepts demo/synthetic identities; "
            "real patient work needs a qualified site adapter"
        )


@dataclass
class SyntheticScenario:
    """Fault injection for local recovery checks; never used with real data."""

    revision: str = "synthetic-revision-1"
    incomplete_scopes: frozenset[str] = frozenset()
    lose_write_response: bool = False
    duplicate_visible_object: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyntheticHospitalUI:
    """In-memory stand-in for one qualified hospital UI profile."""

    def __init__(self, scenario: SyntheticScenario | None = None):
        self.profile = UIProfile(
            SYNTHETIC_SITE_ID, "synthetic-inpatient-ui", "synthetic-1", "demo-doctor",
            (640, 480), "demo://qualification/synthetic-ui",
            WRITE_CAPABILITIES, True,
        )
        self.scenario = scenario or SyntheticScenario()
        self.objects: dict[str, VisibleObject] = {}
        self.writes = 0

    def collect(self, subject: SubjectRef, capability_id: str) -> Snapshot:
        require_synthetic_identity(subject)
        facts = (
            Fact("fact-orders", "合成当前医嘱", "demo://orders/1", "HIS", "orders/1",
                 "2026-01-01"),
            Fact("fact-lab", "合成检验结果", "demo://lab/1", "LIS", "lab/1", "2026-01-01"),
            Fact("fact-nursing", "合成护理记录", "demo://nursing/1", "NIS", "nursing/1",
                 "2026-01-01"),
        )
        coverage = tuple(
            SourceCoverage(
                "synthetic-source", scope, scope not in self.scenario.incomplete_scopes,
                ("synthetic gap",) if scope in self.scenario.incomplete_scopes else (),
            )
            for scope in SYNTHETIC_SCOPES
        )
        return Snapshot(subject, self.scenario.revision, facts,
                        ("demo://kvm/read-context",), coverage)

    def _store(self, proposal: Proposal, object_ref: str) -> VisibleObject:
        state = "saved_draft" if proposal.capability_id in NOTE_CAPABILITIES else "entered"
        visible = VisibleObject(
            proposal.subject, object_ref, state, dict(proposal.payload),
            f"demo://kvm/screenshot/{object_ref.rsplit('/', 1)[-1]}",
            ("demo://kvm/action/synthetic",), _now(),
        )
        self.objects[object_ref] = visible
        return visible

    def write(self, proposal: Proposal, existing_object_ref: str | None) -> ExecutionReceipt:
        require_synthetic_identity(proposal.subject)
        self.writes += 1
        object_ref = existing_object_ref or f"demo://ui/object/{self.writes}"
        visible = self._store(proposal, object_ref)
        if self.scenario.duplicate_visible_object:
            self._store(proposal, f"{object_ref}-copy")
        if self.scenario.lose_write_response:
            raise ConnectionError("synthetic response lost after the GUI write")
        return ExecutionReceipt(object_ref, visible.state, visible.trace_refs)

    def open_object(self, subject: SubjectRef, capability_id: str, object_ref: str) -> VisibleObject:
        require_synthetic_identity(subject)
        try:
            return self.objects[object_ref]
        except KeyError as error:
            raise ValueError("synthetic object is not visible") from error

    def find_candidates(self, task: Task) -> tuple[VisibleObject, ...]:
        if task.proposal is None:
            return ()
        return tuple(
            item for item in self.objects.values()
            if item.subject == task.request.subject and item.payload == task.proposal.payload
        )


class SyntheticKnowledge:
    """Deterministic placeholder proposer; it never makes clinical claims."""

    def __init__(self, generator_ref: str = "demo://proposer/synthetic-v1"):
        self.generator_ref = generator_ref

    def propose(self, request, snapshot) -> Proposal:
        payload: dict[str, Any]
        if request.capability_id in NOTE_CAPABILITIES:
            payload = {
                "document_type": "progress_note",
                "draft": "合成病程记录草稿，待医生补充实际查房内容。",
            }
        else:
            payload = {
                "changes": [
                    {"action": "review", "item": "合成医嘱变更", "dose": "待医生确认"},
                ],
            }
        return Proposal(snapshot.subject, request.capability_id, payload,
                        tuple(fact.fact_id for fact in snapshot.facts), self.generator_ref)


def build_synthetic_system(
    scenario: SyntheticScenario | None = None,
) -> tuple[ClinicalSystem, SyntheticHospitalUI]:
    ui = SyntheticHospitalUI(scenario)
    return UIClinicalSystem(ui), ui
