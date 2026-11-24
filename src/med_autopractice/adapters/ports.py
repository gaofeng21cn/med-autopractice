"""Hospital-system and task-state ports."""

from typing import Protocol

from med_autopractice.domain.task import (
    AuditEvent, ClinicianApproval, ExecutionReceipt, Proposal, Snapshot, SystemReadback, TaskRequest,
    SubjectRef, Task,
)


class ClinicalSystem(Protocol):
    def read_context(self, subject: SubjectRef, capability_id: str) -> Snapshot: ...

    def can_apply(self, proposal: Proposal) -> bool: ...

    def apply(
        self, proposal: Proposal, idempotency_key: str, existing_object_ref: str | None
    ) -> ExecutionReceipt: ...

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback: ...

    def find_submission(self, task: Task) -> ExecutionReceipt | None: ...


class SiteProvider(Protocol):
    """A deployment-owned connector with an authenticated clinical authority."""

    def authorize(self, action: str, request: TaskRequest, decision: ClinicianApproval | None) -> bool: ...

    def read_context(self, subject: SubjectRef, capability_id: str) -> Snapshot: ...

    def can_apply(self, proposal: Proposal) -> bool: ...

    def apply_bound(
        self, request: TaskRequest, proposal: Proposal, revision: str,
        idempotency_key: str, existing_object_ref: str | None, approval: ClinicianApproval,
    ) -> ExecutionReceipt: ...

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback: ...

    def find_submission(self, task: Task) -> ExecutionReceipt | None: ...


class AuditSink(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class TaskStore(Protocol):
    def validate_request(self, request: TaskRequest) -> None: ...

    def get(self, task_id: str) -> Task | None: ...

    def get_by_request(self, request_id: str) -> Task | None: ...

    def list_tasks(
        self, *, status: str | None = None, patient_id: str | None = None, limit: int = 50
    ) -> list[Task]: ...

    def save(self, task: Task) -> None: ...
