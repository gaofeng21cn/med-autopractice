"""Shared task objects. Hospital systems remain authoritative for clinical records."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from typing import Any

try:
    from opl_framework.controlled_apply import compute_proposal_digest as _framework_proposal_digest
except ImportError:
    _framework_proposal_digest = None


@dataclass(frozen=True)
class SubjectRef:
    site_id: str
    patient_id: str
    encounter_id: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip()
                   for value in (self.site_id, self.patient_id, self.encounter_id)):
            raise ValueError("site, patient, and encounter IDs are required")


@dataclass(frozen=True)
class Fact:
    fact_id: str
    value: str
    source_ref: str
    source_system: str = ""
    source_object_ref: str = ""
    effective_at: str | None = None
    recorded_at: str | None = None
    unit: str | None = None
    status: str = "observed"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {
            "observed", "verified", "not_recorded", "not_found", "denied",
            "not_applicable", "unreadable", "conflicting", "uncertain",
        }:
            raise ValueError("invalid clinical fact status")


@dataclass(frozen=True)
class SourceCoverage:
    source_system: str
    scope: str
    complete: bool
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    subject: SubjectRef
    revision: str
    facts: tuple[Fact, ...]
    trace_refs: tuple[str, ...]
    coverage: tuple[SourceCoverage, ...] = ()


class WorkflowInputRequired(ValueError):
    """An input the clinician can supply after reviewing the sourced context."""

    def __init__(self, *fields: str):
        super().__init__("workflow needs clinician input")
        self.fields = fields


@dataclass(frozen=True)
class TaskRequest:
    request_id: str
    capability_id: str
    intent: str
    subject: SubjectRef
    authority_ref: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Proposal:
    subject: SubjectRef
    capability_id: str
    payload: dict[str, Any]
    cited_fact_ids: tuple[str, ...]
    generator_ref: str
    knowledge_refs: tuple[str, ...] = ()
    schema_version: str = "map-proposal.v1"

    @property
    def digest(self) -> str:
        body = {
            "subject": vars(self.subject),
            "capability_id": self.capability_id,
            "payload": self.payload,
            "cited_fact_ids": self.cited_fact_ids,
            "generator_ref": self.generator_ref,
            "knowledge_refs": self.knowledge_refs,
            "schema_version": self.schema_version,
        }
        if _framework_proposal_digest is not None:
            return _framework_proposal_digest(body)
        return sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class TaskPolicy:
    capability_id: str
    requires_pre_submit_review: bool
    expected_system_state: str | None
    required_scopes: tuple[str, ...] = ()
    deployment_revision: str = ""
    allow_partial_draft: bool = False

    def __post_init__(self) -> None:
        order_changes = {
            "maintain_order_sets", "prepare_admission_orders", "revise_orders",
            "prepare_discharge_medication",
        }
        if self.capability_id in order_changes and not self.requires_pre_submit_review:
            raise ValueError("order changes require clinician approval before submission")
        if self.capability_id in order_changes and self.allow_partial_draft:
            raise ValueError("order workflows cannot enable partial draft mode")


@dataclass(frozen=True)
class ClinicianApproval:
    task_id: str
    clinician_id: str
    proposal_digest: str
    facts_revision: str


@dataclass(frozen=True)
class ExecutionReceipt:
    object_ref: str
    state: str
    trace_refs: tuple[str, ...]


@dataclass(frozen=True)
class SystemReadback:
    subject: SubjectRef
    object_ref: str
    proposal_digest: str
    state: str
    screenshot_ref: str | None
    trace_refs: tuple[str, ...]
    visible_payload: dict[str, Any] | None = None
    observed_at: str | None = None
    # Versioned target-system evidence from an independent structured read.
    resource_version_ref: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    task_id: str
    sequence: int
    action: str
    outcome: str
    at_utc: str
    evidence_refs: tuple[str, ...] = ()
    screenshot_ref: str | None = None
    resource_version_ref: str | None = None


@dataclass(frozen=True)
class ReviewDecision:
    clinician_id: str
    accepted: bool
    proposal_digest: str
    screenshot_ref: str | None
    feedback: str
    at_utc: str
    resource_version_ref: str | None = None


@dataclass
class Task:
    task_id: str
    request: TaskRequest
    policy: TaskPolicy
    revision: int = 0
    status: str = "collecting"
    snapshot: Snapshot | None = None
    proposal: Proposal | None = None
    prepared_digest: str | None = None
    receipt: ExecutionReceipt | None = None
    readback: SystemReadback | None = None
    review_feedback: str | None = None
    idempotency_key: str | None = None
    reviews: list[ReviewDecision] = field(default_factory=list)
    events: list[AuditEvent] = field(default_factory=list)
    input_override: dict[str, Any] | None = None
    required_input: tuple[str, ...] = ()

    @property
    def effective_request(self) -> TaskRequest:
        if self.input_override is None:
            return self.request
        return replace(self.request, inputs=self.input_override)
