"""SQLite task snapshots and their audit events commit in one transaction."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from med_autopractice.domain.task import (
    AuditEvent, ExecutionReceipt, Fact, Proposal, ReviewDecision, Snapshot,
    SourceCoverage, SubjectRef, SystemReadback, Task, TaskPolicy, TaskRequest,
)
from med_autopractice.platform.locking import exclusive_file_lock
from med_autopractice.platform.security import open_private_rw


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

    def list_tasks(
        self, *, status: str | None = None, patient_id: str | None = None, limit: int = 50
    ) -> list[Task]:
        results: list[Task] = []
        for task in reversed(list(self.tasks.values())):
            if status is not None and task.status != status:
                continue
            if patient_id is not None and task.request.subject.patient_id != patient_id:
                continue
            results.append(task)
            if len(results) >= limit:
                break
        return results

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
            tuple(SourceCoverage(
                coverage["source_system"], coverage["scope"], coverage["complete"], tuple(coverage.get("gaps", ())),
            ) for coverage in snapshot.get("coverage", [])),
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
            readback.get("resource_version_ref"),
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
            resource_version_ref=event.get("resource_version_ref"),
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
        events=events, input_override=data.get("input_override"),
        required_input=tuple(data.get("required_input", ())),
    )


class SqliteTaskStore:
    def __init__(self, path: Path, *, site_id: str | None = None, serialize_operations: bool = False):
        self.site_id = site_id
        self.path = path
        self.serialize_operations = serialize_operations
        open_private_rw(path)
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "task_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
                "revision INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS deployment (id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "site_id TEXT NOT NULL)"
                )
                connection.execute("BEGIN IMMEDIATE")
                site = connection.execute("SELECT site_id FROM deployment WHERE id = 1").fetchone()
                expected = site_id or ""
                if site is None:
                    if site_id and connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
                        raise PermissionError("existing unbound task database")
                    connection.execute("INSERT INTO deployment VALUES (1, ?)", (expected,))
                elif site[0] != expected:
                    raise PermissionError("task database belongs to another deployment")
        finally:
            connection.close()

    @contextmanager
    def operation(self, task_id: str):
        if not self.site_id:
            yield
            return
        # Declarative tasks share a terminal. SQLite CAS still protects each task commit.
        lock_dir = self.path.parent / ".operations"
        name = "site-terminal" if self.serialize_operations else sha256(task_id.encode()).hexdigest()
        with exclusive_file_lock(lock_dir / name):
            yield

    def _read(self, column: str, value: str) -> Task | None:
        query = {
            "task_id": "SELECT body FROM tasks WHERE task_id = ?",
            "request_id": "SELECT body FROM tasks WHERE request_id = ?",
        }[column]
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute(query, (value,)).fetchone()
            if row is None:
                return None
            task = _task_from_json(row[0])
            self.validate_request(task.request)
            return task
        finally:
            connection.close()

    def get(self, task_id: str) -> Task | None:
        return self._read("task_id", task_id)

    def get_by_request(self, request_id: str) -> Task | None:
        return self._read("request_id", request_id)

    def list_tasks(
        self, *, status: str | None = None, patient_id: str | None = None, limit: int = 50
    ) -> list[Task]:
        if limit < 1:
            return []
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute("SELECT body FROM tasks ORDER BY rowid DESC").fetchall()
        finally:
            connection.close()
        results: list[Task] = []
        for (body,) in rows:
            task = _task_from_json(body)
            self.validate_request(task.request)
            if status is not None and task.status != status:
                continue
            if patient_id is not None and task.request.subject.patient_id != patient_id:
                continue
            results.append(task)
            if len(results) >= limit:
                break
        return results

    def validate_request(self, request: TaskRequest) -> None:
        subject = request.subject
        if self.site_id:
            if subject.site_id != self.site_id:
                raise PermissionError("task belongs to another deployment")
            return
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
                    try:
                        connection.execute(
                            "INSERT INTO tasks (task_id, request_id, revision, body) VALUES (?, ?, ?, ?)",
                            (task.task_id, task.request.request_id, next_revision, body),
                        )
                    except sqlite3.IntegrityError as error:
                        raise ConcurrentTaskUpdate("request was created in another process") from error
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


class SyntheticSqliteTaskStore(SqliteTaskStore):
    """Preserve the isolated synthetic CLI and existing database format."""

    def __init__(self, path: Path):
        super().__init__(path)
