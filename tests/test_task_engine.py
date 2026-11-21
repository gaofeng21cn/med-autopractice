import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from med_autopractice.agent.task_engine import JsonlAuditSink, TaskEngine
from med_autopractice.agent.task_store import (
    ConcurrentTaskUpdate, SyntheticSqliteTaskStore,
)
from med_autopractice.domain.task import (
    ClinicianApproval, ExecutionReceipt, Fact, Proposal, Snapshot,
    SubjectRef, SystemReadback, TaskPolicy, TaskRequest,
)


SUBJECT = SubjectRef("demo-hospital", "synthetic-patient", "synthetic-encounter")


class SyntheticKnowledge:
    def propose(self, request, snapshot):
        return Proposal(
            subject=snapshot.subject,
            capability_id=request.capability_id,
            payload={"draft": "Synthetic note based on the reviewed encounter."},
            cited_fact_ids=("fact-1",),
            generator_ref="demo://proposer/synthetic-v1",
            knowledge_refs=("synthetic-rule-v1",),
        )


class SyntheticSystem:
    def __init__(self, transport, screenshot=True):
        self.transport = transport
        self.revision = "revision-1"
        self.screenshot = screenshot
        self.applies = 0
        self.objects = {}
        self.keys = {}

    def read_context(self, subject, capability_id):
        return Snapshot(
            subject, self.revision,
            (Fact("fact-1", "Synthetic observation", "demo://source/1"),),
            (f"demo://{self.transport}/read",),
        )

    def can_apply(self, proposal):
        return True

    def apply(self, proposal, idempotency_key, existing_object_ref):
        if idempotency_key in self.keys:
            object_ref = self.keys[idempotency_key]
        else:
            self.applies += 1
            object_ref = existing_object_ref or "demo://record/1"
            self.objects[object_ref] = proposal
            self.keys[idempotency_key] = object_ref
        return ExecutionReceipt(object_ref, "saved", (f"demo://{self.transport}/submit",))

    def read_back(self, receipt, task):
        proposal = self.objects[receipt.object_ref]
        return SystemReadback(
            proposal.subject, receipt.object_ref, proposal.digest, "saved_draft",
            f"demo://{self.transport}/screenshot" if self.screenshot else "",
            (f"demo://{self.transport}/readback",),
            dict(proposal.payload),
        )

    def find_submission(self, task):
        object_ref = self.keys.get(task.idempotency_key)
        if object_ref is None:
            return None
        return ExecutionReceipt(object_ref, "saved", (f"demo://{self.transport}/lookup",))


class AppliedThenLostSystem(SyntheticSystem):
    def apply(self, proposal, idempotency_key, existing_object_ref):
        receipt = super().apply(proposal, idempotency_key, existing_object_ref)
        if self.applies == 1:
            raise ConnectionError("synthetic connection lost after commit")
        return receipt


class MissingTraceSystem(SyntheticSystem):
    def read_context(self, subject, capability_id):
        snapshot = super().read_context(subject, capability_id)
        return Snapshot(snapshot.subject, snapshot.revision, snapshot.facts, ())


class MissingRefreshTraceSystem(SyntheticSystem):
    def __init__(self, transport):
        super().__init__(transport)
        self.reads = 0

    def read_context(self, subject, capability_id):
        self.reads += 1
        snapshot = super().read_context(subject, capability_id)
        if self.reads == 2:
            return Snapshot(snapshot.subject, snapshot.revision, snapshot.facts, ())
        return snapshot


class TaskEngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audit_path = Path(self.directory.name) / "audit.jsonl"

    def engine(self, system, capability="write_inpatient_notes", pre_review=False, store=None):
        policy = TaskPolicy(capability, pre_review, "saved_draft")
        return TaskEngine(
            SyntheticKnowledge(), system, JsonlAuditSink(self.audit_path),
            {capability: policy}, store=store,
        )

    def request(self, capability="write_inpatient_notes"):
        return TaskRequest("request-1", capability, "Prepare a synthetic draft", SUBJECT, "demo://authority")

    def test_synthetic_kvm_workflow_and_readback(self):
        system = SyntheticSystem("kvm")
        engine = self.engine(system)
        task = engine.start(self.request(), auto_submit=True)
        self.assertEqual(task.status, "needs_clinician_acceptance")
        self.assertEqual(task.readback.screenshot_ref, "demo://kvm/screenshot")
        self.assertEqual(system.applies, 1)
        self.assertIs(engine.start(self.request()), task)
        engine.review(task.task_id, "demo-clinician", accepted=True)
        self.assertEqual(task.status, "accepted")
        events = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        self.assertEqual(len(events), len(task.events))
        self.assertEqual(events[-2]["action"], "system_readback")
        self.assertEqual(events[-2]["screenshot_ref"], task.readback.screenshot_ref)

    def test_versioned_middleware_readback_survives_restart_and_clinician_review(self):
        class NativeSystem(SyntheticSystem):
            def read_back(self, receipt, task):
                return replace(super().read_back(receipt, task),
                               screenshot_ref=None, observed_at="2026-01-01T10:00:00Z",
                               resource_version_ref=receipt.object_ref + "/versions/2")

        system = NativeSystem("middleware")
        store = SyntheticSqliteTaskStore(Path(self.directory.name) / "tasks.sqlite")
        task = self.engine(system, store=store).start(self.request(), auto_submit=True)
        self.assertEqual(task.status, "needs_clinician_acceptance")
        reopened = self.engine(system, store=store)
        reviewed = reopened.review(task.task_id, "demo-clinician", accepted=True)
        evidence = "demo://record/1/versions/2"
        self.assertEqual(reviewed.reviews[-1].resource_version_ref, evidence)
        self.assertEqual(reopened.get(task.task_id).events[-2].resource_version_ref, evidence)
        self.assertIsNone(reviewed.readback.screenshot_ref)

    def test_order_submission_requires_exact_clinician_review(self):
        with self.assertRaises(ValueError):
            TaskPolicy("prepare_admission_orders", False, "saved_draft")
        system = SyntheticSystem("kvm")
        engine = self.engine(system, "prepare_admission_orders", pre_review=True)
        task = engine.start(self.request("prepare_admission_orders"))
        self.assertEqual(task.status, "needs_pre_submit_review")
        with self.assertRaises(ValueError):
            engine.submit(task.task_id)
        with self.assertRaises(ValueError):
            engine.submit(task.task_id, ClinicianApproval(task.task_id, "doctor", "wrong", "revision-1"))
        self.assertEqual(system.applies, 0)
        approval = ClinicianApproval(task.task_id, "doctor", task.proposal.digest, task.snapshot.revision)
        engine.submit(task.task_id, approval)
        self.assertEqual(task.status, "needs_clinician_acceptance")

    def test_changed_facts_stop_submission(self):
        system = SyntheticSystem("kvm")
        engine = self.engine(system)
        task = engine.start(self.request())
        system.revision = "revision-2"
        engine.submit(task.task_id)
        self.assertEqual(task.status, "needs_refresh")
        self.assertEqual(system.applies, 0)

    def test_missing_system_screenshot_prevents_acceptance(self):
        engine = self.engine(SyntheticSystem("kvm", screenshot=False))
        task = engine.start(self.request(), auto_submit=True)
        self.assertEqual(task.status, "execution_uncertain")
        with self.assertRaises(ValueError):
            engine.review(task.task_id, "doctor", accepted=True)

    def test_proposal_requires_traceable_generator_and_schema(self):
        engine = self.engine(SyntheticSystem("kvm"))
        original = engine.knowledge.propose
        engine.knowledge.propose = lambda request, snapshot: replace(
            original(request, snapshot), generator_ref=""
        )
        with self.assertRaises(ValueError):
            engine.start(self.request())
        engine = self.engine(SyntheticSystem("kvm"))
        original = engine.knowledge.propose
        engine.knowledge.propose = lambda request, snapshot: replace(
            original(request, snapshot), schema_version="unsupported"
        )
        with self.assertRaises(ValueError):
            engine.start(self.request())

    def test_missing_read_trace_blocks_preparation(self):
        engine = self.engine(MissingTraceSystem("kvm"))
        with self.assertRaises(ValueError):
            engine.start(self.request())
        events = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        self.assertEqual([event["action"] for event in events], ["request", "prepare"])

    def test_missing_refresh_trace_blocks_submission(self):
        system = MissingRefreshTraceSystem("kvm")
        engine = self.engine(system)
        task = engine.start(self.request())
        with self.assertRaises(ValueError):
            engine.submit(task.task_id)
        self.assertEqual(system.applies, 0)
        self.assertEqual(engine.get(task.task_id).status, "ready_to_submit")

    def test_correction_updates_existing_object_and_requires_new_review(self):
        system = SyntheticSystem("kvm")
        engine = self.engine(system, "prepare_admission_orders", pre_review=True)
        task = engine.start(self.request("prepare_admission_orders"))
        approval = ClinicianApproval(task.task_id, "doctor", task.proposal.digest, task.snapshot.revision)
        engine.submit(task.task_id, approval)
        original_ref = task.receipt.object_ref
        engine.review(task.task_id, "doctor", accepted=False, feedback="Correct the synthetic draft")
        self.assertEqual(task.review_feedback, "Correct the synthetic draft")
        self.assertEqual(task.reviews[0].screenshot_ref, "demo://kvm/screenshot")
        corrected = Proposal(SUBJECT, task.request.capability_id, {"draft": "Corrected synthetic draft"},
                             ("fact-1",), "demo://proposer/synthetic-v2")
        engine.revise(task.task_id, corrected)
        self.assertIsNone(task.review_feedback)
        self.assertEqual(task.status, "needs_pre_submit_review")
        with self.assertRaises(ValueError):
            engine.submit(task.task_id, approval)
        new_approval = ClinicianApproval(task.task_id, "doctor", corrected.digest, task.snapshot.revision)
        engine.submit(task.task_id, new_approval)
        self.assertEqual(task.receipt.object_ref, original_ref)
        self.assertEqual(system.applies, 2)
        engine.review(task.task_id, "doctor", accepted=True)
        self.assertEqual(task.status, "accepted")
        self.assertEqual(len(task.reviews), 2)

    def test_mutated_proposal_cannot_be_accepted(self):
        engine = self.engine(SyntheticSystem("kvm"))
        task = engine.start(self.request(), auto_submit=True)
        task.proposal.payload["draft"] = "Changed after the system readback"
        with self.assertRaises(ValueError):
            engine.review(task.task_id, "doctor", accepted=True)
        self.assertEqual(task.status, "needs_clinician_acceptance")

    def test_mutated_readback_cannot_be_accepted(self):
        engine = self.engine(SyntheticSystem("kvm"))
        task = engine.start(self.request(), auto_submit=True)
        task.readback.visible_payload["draft"] = "different text"
        with self.assertRaises(ValueError):
            engine.review(task.task_id, "doctor", accepted=True)

    def test_synthetic_task_resumes_across_engine_instances(self):
        path = Path(self.directory.name) / "tasks.sqlite"
        system = SyntheticSystem("kvm")
        first = self.engine(system, store=SyntheticSqliteTaskStore(path))
        task = first.start(self.request())
        second = self.engine(system, store=SyntheticSqliteTaskStore(path))
        resumed = second.start(self.request())
        self.assertEqual(resumed.task_id, task.task_id)
        self.assertEqual(resumed.proposal.digest, task.proposal.digest)
        self.assertEqual(resumed.proposal.generator_ref, "demo://proposer/synthetic-v1")
        self.assertEqual(resumed.proposal.schema_version, "map-proposal.v1")
        second.submit(task.task_id)
        third = self.engine(system, store=SyntheticSqliteTaskStore(path))
        self.assertEqual(third.get(task.task_id).status, "needs_clinician_acceptance")
        third.review(task.task_id, "doctor", accepted=True)
        fourth = self.engine(system, store=SyntheticSqliteTaskStore(path))
        self.assertEqual(fourth.get(task.task_id).status, "accepted")
        self.assertEqual(len(fourth.get(task.task_id).reviews), 1)
        self.assertEqual(system.applies, 1)

    def test_synthetic_task_is_readable_in_a_new_process(self):
        path = Path(self.directory.name) / "tasks.sqlite"
        engine = self.engine(SyntheticSystem("kvm"), store=SyntheticSqliteTaskStore(path))
        task = engine.start(self.request())
        source_root = Path(__file__).resolve().parents[1]
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from med_autopractice.agent.task_store import SyntheticSqliteTaskStore\n"
            "task = SyntheticSqliteTaskStore(Path(sys.argv[1])).get(sys.argv[2])\n"
            "assert task.status == 'ready_to_submit'\n"
            "assert task.proposal.digest == sys.argv[3]\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(source_root / "src")
        child = subprocess.run(
            [sys.executable, "-c", script, str(path), task.task_id, task.proposal.digest],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_lost_submit_response_reconciles_without_replay(self):
        path = Path(self.directory.name) / "tasks.sqlite"
        system = AppliedThenLostSystem("kvm")
        first = self.engine(system, store=SyntheticSqliteTaskStore(path))
        task = first.start(self.request())
        with self.assertRaises(ConnectionError):
            first.submit(task.task_id)
        second = self.engine(system, store=SyntheticSqliteTaskStore(path))
        self.assertEqual(second.get(task.task_id).status, "execution_uncertain")
        reconciled = second.reconcile(task.task_id)
        self.assertEqual(reconciled.status, "needs_clinician_acceptance")
        self.assertEqual(system.applies, 1)
        second.review(task.task_id, "doctor", accepted=True)
        self.assertEqual(second.get(task.task_id).status, "accepted")

    def test_changed_policy_blocks_resumed_submission(self):
        path = Path(self.directory.name) / "tasks.sqlite"
        system = SyntheticSystem("kvm")
        first = self.engine(system, store=SyntheticSqliteTaskStore(path))
        task = first.start(self.request())
        second = self.engine(system, pre_review=True, store=SyntheticSqliteTaskStore(path))
        resumed = second.submit(task.task_id)
        self.assertEqual(resumed.status, "needs_policy_review")
        self.assertEqual(system.applies, 0)

    def test_missing_lookup_does_not_resubmit(self):
        path = Path(self.directory.name) / "tasks.sqlite"
        system = AppliedThenLostSystem("kvm")
        engine = self.engine(system, store=SyntheticSqliteTaskStore(path))
        task = engine.start(self.request())
        with self.assertRaises(ConnectionError):
            engine.submit(task.task_id)
        system.keys.clear()
        self.assertEqual(engine.reconcile(task.task_id).status, "execution_uncertain")
        self.assertEqual(system.applies, 1)

    def test_synthetic_store_rejects_real_identity_and_stale_writer(self):
        path = Path(self.directory.name) / "tasks.sqlite"
        system = SyntheticSystem("kvm")
        store = SyntheticSqliteTaskStore(path)
        engine = self.engine(system, store=store)
        real_request = TaskRequest(
            "real-request", "write_inpatient_notes", "Do not persist",
            SubjectRef("hospital", "patient", "encounter"), "real-authority",
        )
        with self.assertRaises(ValueError):
            engine.start(real_request)
        self.assertFalse(self.audit_path.exists())
        task = engine.start(self.request())
        first = store.get(task.task_id)
        stale = store.get(task.task_id)
        first.status = "changed"
        store.save(first)
        with self.assertRaises(ConcurrentTaskUpdate):
            store.save(stale)


if __name__ == "__main__":
    unittest.main()
