"""Task requirements, independent of medical proposals and UI navigation."""

from dataclasses import dataclass
from types import MappingProxyType

from med_autopractice.domain.task import TaskPolicy


@dataclass(frozen=True)
class WorkflowDefinition:
    capability_id: str
    required_scopes: tuple[str, ...]
    requires_pre_submit_review: bool
    read_dependencies: tuple[str, ...] = ()
    rule_dependencies: tuple[str, ...] = ()
    write_dependency: str | None = None
    readback_dependency: str | None = None
    medical_proposal: bool = True

    def __post_init__(self) -> None:
        if bool(self.write_dependency) != bool(self.readback_dependency):
            raise ValueError("a write workflow needs a paired system readback")
        if self.requires_pre_submit_review and self.write_dependency is None:
            raise ValueError("read-only workflows cannot request a submit approval")

    def policy(self, expected_system_state: str) -> TaskPolicy:
        if self.write_dependency is None:
            raise ValueError("read-only workflow has no submission policy")
        if not expected_system_state:
            raise ValueError("a qualified system readback state is required")
        return TaskPolicy(
            self.capability_id,
            self.requires_pre_submit_review,
            expected_system_state,
            self.required_scopes,
        )


PHASE1_WORKFLOWS = MappingProxyType({
    "collect_history": WorkflowDefinition(
        "collect_history", ("patient_context", "encounter_history"), False,
        ("bind_inpatient", "bind_outpatient", "read_inpatient_records",
         "read_outpatient_records", "read_lab_results", "read_imaging_reports"),
        medical_proposal=False,
    ),
    "maintain_order_sets": WorkflowDefinition(
        "maintain_order_sets", ("approved_protocol", "local_catalog", "current_order_set"), True,
        ("read_order_sets", "read_local_catalog"), ("approved_order_set_rules",),
        "write_order_set", "readback_order_set",
    ),
    "prepare_admission_orders": WorkflowDefinition(
        "prepare_admission_orders", ("patient_context", "allergies", "current_orders", "admission_plan"), True,
        ("bind_inpatient", "read_inpatient_records", "read_allergies", "read_current_orders",
         "read_local_catalog"), ("local_medicine_dictionary",),
        "enter_inpatient_orders", "readback_inpatient_orders",
    ),
    "write_inpatient_notes": WorkflowDefinition(
        "write_inpatient_notes",
        ("patient_context", "current_orders", "results", "nursing"),
        False,
        ("bind_inpatient", "read_inpatient_records", "read_current_orders",
         "read_lab_results", "read_imaging_reports", "read_nursing_records"),
        ("institutional_document_rules",),
        "save_inpatient_note_draft", "readback_inpatient_note",
    ),
    "revise_orders": WorkflowDefinition(
        "revise_orders",
        ("patient_context", "current_orders", "results", "nursing"),
        True,
        ("bind_inpatient", "read_current_orders", "read_lab_results",
         "read_imaging_reports", "read_nursing_records", "read_local_catalog"),
        ("local_medicine_dictionary",),
        "enter_inpatient_orders", "readback_inpatient_orders",
    ),
    "prepare_discharge_medication": WorkflowDefinition(
        "prepare_discharge_medication", ("patient_context", "current_orders", "allergies", "discharge_plan"), True,
        ("bind_inpatient", "read_current_orders", "read_allergies",
         "read_discharge_plan", "read_local_catalog"), ("local_medicine_dictionary",),
        "enter_inpatient_orders", "readback_inpatient_orders",
    ),
    "write_outpatient_notes": WorkflowDefinition(
        "write_outpatient_notes", ("patient_context", "current_encounter", "results"), False,
        ("bind_outpatient", "read_outpatient_records", "read_lab_results",
         "read_imaging_reports"), ("institutional_document_rules",),
        "save_outpatient_note_draft", "readback_outpatient_note",
    ),
    "audit_discharge_records": WorkflowDefinition(
        "audit_discharge_records", ("patient_context", "discharge_record", "document_status"), True,
        ("bind_inpatient", "read_discharge_records", "read_document_status"),
        ("discharge_audit_rules",), "save_discharge_review", "readback_discharge_review",
    ),
    "track_document_deadlines": WorkflowDefinition(
        "track_document_deadlines", ("patient_context", "document_status"), False,
        ("bind_inpatient", "read_document_status"), ("institutional_document_rules",),
        medical_proposal=False,
    ),
})

PILOT_WORKFLOWS = MappingProxyType({
    capability_id: PHASE1_WORKFLOWS[capability_id]
    for capability_id in ("write_inpatient_notes", "revise_orders")
})
