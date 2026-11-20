"""Hospital-system and task-state ports."""

from typing import Protocol

from med_autopractice.domain.task import (
    AuditEvent, ExecutionReceipt, Proposal, Snapshot, SystemReadback, TaskRequest,
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


class AuditSink(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class TaskStore(Protocol):
    def validate_request(self, request: TaskRequest) -> None: ...

    def get(self, task_id: str) -> Task | None: ...

    def get_by_request(self, request_id: str) -> Task | None: ...

    def save(self, task: Task) -> None: ...
