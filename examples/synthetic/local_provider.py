"""Synthetic site provider for the regular local deployment entrypoint."""

from dataclasses import asdict
from datetime import datetime, timezone
import json
import sqlite3

from med_autopractice.domain.task import ExecutionReceipt, Fact, Snapshot, SourceCoverage, SubjectRef, SystemReadback
from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS


class DemoProvider:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "demo-target.sqlite"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS written (operation TEXT PRIMARY KEY, "
                "object_ref TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL, "
                "digest TEXT NOT NULL, state TEXT NOT NULL)"
            )

    def connect(self):
        return sqlite3.connect(self.path)

    def authorize(self, action, request, decision):
        subject = request.subject
        if (subject.site_id != "demo-local" or not subject.patient_id.startswith("synthetic-")
                or not subject.encounter_id.startswith("synthetic-") or request.authority_ref != "demo://grant"):
            return False
        return action in {"read", "reconcile"} or bool(decision and decision.clinician_id == "demo-clinician")

    def read_context(self, subject, capability):
        def fact(label, **details):
            return Fact(label, f"合成 {label}", f"demo://facts/{label}", source_object_ref=f"demo://objects/{label}",
                        details=details)

        facts = [
            fact("observation", section="观察"),
            fact("assessment", section="评估"),
            fact("catalog", kind="catalog_item", code="DEMO-RX", active=True, item_type="medication"),
            fact("current", kind="current_order", code="DEMO-RX"),
            fact("set", kind="order_set_item", code="DEMO-RX"),
            fact("document", kind="document_status", document="演练记录", state="draft", trigger_on="2026-01-01"),
            fact("cohort", kind="cohort_field", field="program", value="demo"),
            fact("followup", kind="followup_event", event="演练复查", state="trigger", on="2026-01-01"),
            fact("history", kind="history_collection",
                 collection=json.loads((self.directory / "history.json").read_text())),
        ]
        scopes = LOCAL_WORKFLOWS[capability].required_scopes
        revision_path = self.directory / "demo-revision"
        revision = revision_path.read_text().strip() if revision_path.exists() else "demo-revision-1"
        return Snapshot(subject, revision, tuple(facts), ("demo://trace/read",),
                        tuple(SourceCoverage("demo", scope, True) for scope in scopes))

    def can_apply(self, proposal):
        return LOCAL_WORKFLOWS[proposal.capability_id].write_dependency is not None

    def apply_bound(self, request, proposal, revision, key, existing_ref, approval):
        if not self.authorize("submit", request, approval):
            raise PermissionError("synthetic clinician authorization required")
        body = json.dumps(proposal.payload, ensure_ascii=False, sort_keys=True)
        subject = json.dumps(asdict(request.subject), sort_keys=True)
        state = "saved_draft" if proposal.payload["type"] == "note_draft" else "recorded"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.read_context(request.subject, request.capability_id).revision != revision:
                raise ValueError("synthetic source has changed")
            reference = existing_ref or f"demo://written/{proposal.digest}"
            db.execute("INSERT INTO written VALUES (?, ?, ?, ?, ?, ?)",
                       (key, reference, subject, body, proposal.digest, state))
        if (self.directory / "simulate-lost-response").exists():
            raise ConnectionError("synthetic response lost")
        return ExecutionReceipt(reference, state, ("demo://trace/write",))

    def find_submission(self, task):
        with self.connect() as db:
            row = db.execute("SELECT object_ref, state FROM written WHERE operation = ?",
                             (task.idempotency_key,)).fetchone()
        return ExecutionReceipt(*row, ("demo://trace/lookup",)) if row else None

    def read_back(self, receipt, task):
        with self.connect() as db:
            row = db.execute(
                "SELECT subject, payload, digest, state FROM written WHERE operation = ? AND object_ref = ?",
                (task.idempotency_key, receipt.object_ref),
            ).fetchone()
        subject, payload, digest, state = row
        return SystemReadback(
            SubjectRef(**json.loads(subject)), receipt.object_ref, digest, state, None,
            ("demo://trace/readback",), json.loads(payload), datetime.now(timezone.utc).isoformat(),
            f"{receipt.object_ref}/versions/{digest}",
        )


def build_provider(directory):
    return DemoProvider(directory)
