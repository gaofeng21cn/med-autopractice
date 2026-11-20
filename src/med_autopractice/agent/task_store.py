"""Task state storage. The SQLite implementation is for synthetic cases only."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import stat

from med_autopractice.domain.task import (
    AuditEvent, ExecutionReceipt, Fact, Proposal, ReviewDecision, Snapshot,
    SourceCoverage, SubjectRef, SystemReadback, Task, TaskPolicy, TaskRequest,
)


class ConcurrentTaskUpdate(RuntimeError):
    pass


class InMemoryTaskStore:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.requests: dict[str, str] = {}

    def validate_request(self, request: TaskRequest) -> None:
        pass

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def get_by_request(self, request_id: str) -> Task | None:
        task_id = self.requests.get(request_id)
        return self.tasks.get(task_id) if task_id else None

    def save(self, task: Task) -> None:
        existing = self.requests.get(task.request.request_id)
        if existing is not None and existing != task.task_id:
            raise ValueError("request_id already belongs to another task")
        task.revision += 1
        self.tasks[task.task_id] = task
        self.requests[task.request.request_id] = task.task_id


def _subject(data: dict) -> SubjectRef:
    return SubjectRef(**data)


def _task_from_json(body: str) -> Task:
    data = json.loads(body)
    request = data["request"]
    request["subject"] = _subject(request["subject"])
    snapshot = data.get("snapshot")
    if snapshot is not None:
        snapshot = Snapshot(
            _subject(snapshot["subject"]), snapshot["revision"],
            tuple(Fact(**fact) for fact in snapshot["facts"]),
            tuple(snapshot["trace_refs"]),
            tuple(SourceCoverage(**coverage) for coverage in snapshot.get("coverage", [])),
        )
    proposal = data.get("proposal")
    if proposal is not None:
        proposal = Proposal(
            _subject(proposal["subject"]), proposal["capability_id"],
            proposal["payload"], tuple(proposal["cited_fact_ids"]),
            proposal["generator_ref"],
            tuple(proposal["knowledge_refs"]),
            proposal["schema_version"],
        )
    readback = data.get("readback")
    if readback is not None:
        readback = SystemReadback(
            _subject(readback["subject"]), readback["object_ref"],
            readback["proposal_digest"], readback["state"],
            readback["screenshot_ref"], tuple(readback["trace_refs"]),
            readback.get("visible_payload"), readback.get("observed_at"),
        )
    receipt = data.get("receipt")
    if receipt is not None:
        receipt = ExecutionReceipt(
            receipt["object_ref"], receipt["state"], tuple(receipt["trace_refs"]),
        )
    events = [
        AuditEvent(
            task_id=event["task_id"], sequence=event["sequence"],
            action=event["action"], outcome=event["outcome"],
            at_utc=event["at_utc"], evidence_refs=tuple(event["evidence_refs"]),
            screenshot_ref=event["screenshot_ref"],
        )
        for event in data["events"]
    ]
    policy = data["policy"]
    policy["required_scopes"] = tuple(policy.get("required_scopes", ()))
    return Task(
        task_id=data["task_id"], request=TaskRequest(**request),
        policy=TaskPolicy(**policy),
        revision=data["revision"], status=data["status"], snapshot=snapshot,
        proposal=proposal, prepared_digest=data["prepared_digest"],
        receipt=receipt, readback=readback, review_feedback=data["review_feedback"],
        idempotency_key=data["idempotency_key"],
        reviews=[ReviewDecision(**review) for review in data["reviews"]],
        events=events,
    )


class SyntheticSqliteTaskStore:
    """Crash-recovery test store; never use for patient information."""

    def __init__(self, path: Path):
        self.path = path
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
                raise PermissionError("task database must be private to its owner")
        finally:
            os.close(fd)
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "task_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
                "revision INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()

    def _read(self, column: str, value: str) -> Task | None:
        query = {
            "task_id": "SELECT body FROM tasks WHERE task_id = ?",
            "request_id": "SELECT body FROM tasks WHERE request_id = ?",
        }[column]
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute(query, (value,)).fetchone()
            return _task_from_json(row[0]) if row else None
        finally:
            connection.close()

    def get(self, task_id: str) -> Task | None:
        return self._read("task_id", task_id)

    def get_by_request(self, request_id: str) -> Task | None:
        return self._read("request_id", request_id)

    def validate_request(self, request: TaskRequest) -> None:
        subject = request.subject
        if (
            not subject.site_id.startswith("demo-")
            or not subject.patient_id.startswith("synthetic-")
            or not request.authority_ref.startswith("demo://")
        ):
            raise ValueError("synthetic task store rejects non-demo identities")

    def save(self, task: Task) -> None:
        self.validate_request(task.request)
        next_revision = task.revision + 1
        data = asdict(task)
        data["revision"] = next_revision
        body = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                if task.revision == 0:
                    connection.execute(
                        "INSERT INTO tasks (task_id, request_id, revision, body) "
                        "VALUES (?, ?, ?, ?)",
                        (task.task_id, task.request.request_id, next_revision, body),
                    )
                else:
                    result = connection.execute(
                        "UPDATE tasks SET revision = ?, body = ? "
                        "WHERE task_id = ? AND revision = ?",
                        (next_revision, body, task.task_id, task.revision),
                    )
                    if result.rowcount != 1:
                        raise ConcurrentTaskUpdate("task changed in another process")
        finally:
            connection.close()
        task.revision = next_revision
