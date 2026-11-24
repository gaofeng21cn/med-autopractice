import json
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from med_autopractice.adapters.synthetic import SYNTHETIC_SCOPES, SYNTHETIC_SITE_ID, SyntheticScenario
from med_autopractice.agent.task_engine import JsonlAuditSink
from med_autopractice.agent.task_store import InMemoryTaskStore, SyntheticSqliteTaskStore
from med_autopractice.domain.task import ClinicianApproval, SubjectRef, TaskRequest
from med_autopractice.platform.security import is_windows
from med_autopractice.runtime import build_policies, build_synthetic_runtime
from med_autopractice.workflows.catalog import PHASE1_WORKFLOWS


ROOT = Path(__file__).resolve().parents[1]
SUBJECT = SubjectRef(SYNTHETIC_SITE_ID, "synthetic-patient", "synthetic-encounter")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audit_path = Path(self.directory.name) / "audit.jsonl"
        self.runtime, self.ui = self.build_runtime()

    def build_runtime(self, scenario=None):
        return build_synthetic_runtime(
            InMemoryTaskStore(), JsonlAuditSink(self.audit_path), scenario,
        )

    def request(self, capability_id, subject=SUBJECT, request_id=None):
        return TaskRequest(
            request_id or f"req-{capability_id}", capability_id,
            "合成任务", subject, "demo://authority/synthetic",
        )

    def test_synthetic_coverage_covers_every_required_scope(self):
        required = {scope for workflow in PHASE1_WORKFLOWS.values()
                    for scope in workflow.required_scopes}
        self.assertLessEqual(required, set(SYNTHETIC_SCOPES))

    def test_policies_come_from_the_catalog_and_need_expected_states(self):
        writable = {capability_id for capability_id, workflow in PHASE1_WORKFLOWS.items()
                    if workflow.write_dependency}
        states = {capability_id: "saved_draft" for capability_id in writable}
        self.assertEqual(set(build_policies(states)), writable)
        with self.assertRaisesRegex(ValueError, "missing expected system state"):
            build_policies({})

    def test_read_only_and_unknown_capabilities_are_not_executable(self):
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.runtime.start(self.request("collect_history"))
        with self.assertRaisesRegex(ValueError, "unknown capability"):
            self.runtime.start(self.request("not_a_capability"))

    def test_note_flow_reaches_clinician_acceptance(self):
        view = self.runtime.start(self.request("write_inpatient_notes"))
        self.assertEqual(view["status"], "ready_to_submit")
        self.assertEqual(view["next_action"], "submit")
        self.assertEqual(view["missing_information"], [])
        view = self.runtime.submit(view["task_id"])
        self.assertEqual(view["status"], "needs_clinician_acceptance")
        self.assertEqual(view["next_action"], "obtain_clinician_acceptance")
        self.assertTrue(view["execution"]["readback"]["screenshot_ref"])
        view = self.runtime.review(view["task_id"], "demo-doctor", accepted=True)
        self.assertEqual(view["status"], "accepted")
        self.assertEqual(view["next_action"], "complete")
        json.dumps(view, ensure_ascii=False)

    def test_order_flow_requires_exact_clinician_approval(self):
        view = self.runtime.start(self.request("revise_orders"))
        self.assertEqual(view["status"], "needs_pre_submit_review")
        with self.assertRaises(ValueError):
            self.runtime.submit(view["task_id"])
        self.assertEqual(self.ui.writes, 0)
        approval = ClinicianApproval(
            view["task_id"], "demo-doctor",
            view["proposal"]["digest"], view["snapshot"]["revision"],
        )
        view = self.runtime.submit(view["task_id"], approval)
        self.assertEqual(view["status"], "needs_clinician_acceptance")

    def test_incomplete_sources_block_before_any_write(self):
        runtime, ui = self.build_runtime(SyntheticScenario(incomplete_scopes=frozenset({"nursing"})))
        view = runtime.start(self.request("revise_orders"))
        self.assertEqual(view["status"], "needs_information")
        self.assertEqual(view["next_action"], "provide_missing_sources")
        self.assertIn("nursing", view["missing_information"])
        self.assertEqual(ui.writes, 0)

    def test_lost_response_reconciles_without_replay(self):
        runtime, ui = self.build_runtime(SyntheticScenario(lose_write_response=True))
        view = runtime.start(self.request("revise_orders"))
        approval = ClinicianApproval(
            view["task_id"], "demo-doctor",
            view["proposal"]["digest"], view["snapshot"]["revision"],
        )
        with self.assertRaises(ConnectionError):
            runtime.submit(view["task_id"], approval)
        self.assertEqual(runtime.show(view["task_id"])["status"], "execution_uncertain")
        view = runtime.reconcile(view["task_id"])
        self.assertEqual(view["status"], "needs_clinician_acceptance")
        self.assertEqual(ui.writes, 1)

    def test_ambiguous_visible_candidates_keep_execution_uncertain(self):
        runtime, ui = self.build_runtime(SyntheticScenario(
            lose_write_response=True, duplicate_visible_object=True,
        ))
        view = runtime.start(self.request("revise_orders"))
        approval = ClinicianApproval(
            view["task_id"], "demo-doctor",
            view["proposal"]["digest"], view["snapshot"]["revision"],
        )
        with self.assertRaises(ConnectionError):
            runtime.submit(view["task_id"], approval)
        self.assertEqual(runtime.reconcile(view["task_id"])["status"], "execution_uncertain")
        self.assertEqual(ui.writes, 1)

    def test_revision_rebinds_an_external_payload(self):
        runtime, _ui = self.build_runtime()
        original = runtime.engine.knowledge.propose
        runtime.engine.knowledge.propose = lambda request, snapshot: replace(
            original(request, snapshot), knowledge_refs=("synthetic://rule/v1",))
        view = runtime.start(self.request("write_inpatient_notes"))
        runtime.submit(view["task_id"])
        view = runtime.review(view["task_id"], "demo-doctor", accepted=False,
                              feedback="合成修订意见")
        self.assertEqual(view["status"], "changes_requested")
        view = runtime.revise(view["task_id"], {"document_type": "progress_note",
                                                "draft": "修订后的合成草稿"})
        self.assertEqual(view["status"], "ready_to_submit")
        self.assertIn("+revision", view["proposal"]["generator_ref"])
        self.assertEqual(view["proposal"]["knowledge_refs"], ["synthetic://rule/v1"])

    def test_revision_can_explicitly_replace_reviewed_knowledge(self):
        runtime, _ui = self.build_runtime()
        view = runtime.start(self.request("write_inpatient_notes"))
        runtime.submit(view["task_id"])
        runtime.review(view["task_id"], "demo-doctor", accepted=False,
                       feedback="合成修订意见")
        payload = {"document_type": "progress_note", "draft": "修订后的合成草稿"}
        with self.assertRaisesRegex(ValueError, "knowledge references"):
            runtime.revise(view["task_id"], payload, knowledge_refs="not-a-list")
        revised = runtime.revise(view["task_id"], payload,
                                 knowledge_refs=["synthetic://rule/v2"])
        self.assertEqual(revised["proposal"]["knowledge_refs"], ["synthetic://rule/v2"])
        self.assertNotEqual(view["proposal"]["digest"], revised["proposal"]["digest"])

    def test_synthetic_runtime_rejects_real_identities(self):
        subjects = {
            "req-site": SubjectRef("hospital-a", "synthetic-patient", "synthetic-encounter"),
            "req-patient": SubjectRef(SYNTHETIC_SITE_ID, "P-123456", "synthetic-encounter"),
        }
        for request_id, subject in subjects.items():
            with self.assertRaisesRegex(ValueError, "demo/synthetic"):
                self.runtime.start(self.request("write_inpatient_notes", subject=subject,
                                                request_id=request_id))


class RuntimeCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Path(self.directory.name) / "tasks.sqlite"
        self.request_path = Path(self.directory.name) / "request.json"
        self.request_path.write_text(json.dumps({
            "request_id": "cli-request",
            "capability_id": "write_inpatient_notes",
            "intent": "合成任务",
            "subject": {
                "site_id": SYNTHETIC_SITE_ID,
                "patient_id": "synthetic-patient",
                "encounter_id": "synthetic-encounter",
            },
            "authority_ref": "demo://authority/synthetic",
        }, ensure_ascii=False), encoding="utf-8")

    def run_cli(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/map_runtime.py"), *arguments],
            capture_output=True, text=True, cwd=ROOT,
        )
        return result

    def test_cli_creates_and_resumes_a_task_across_processes(self):
        first = self.run_cli("start", "--store", str(self.store),
                             "--request", str(self.request_path))
        self.assertEqual(first.returncode, 0, first.stderr)
        created = json.loads(first.stdout)
        self.assertEqual(created["status"], "ready_to_submit")
        second = self.run_cli("start", "--store", str(self.store),
                              "--request", str(self.request_path))
        self.assertEqual(json.loads(second.stdout)["task_id"], created["task_id"])
        shown = self.run_cli("show", "--store", str(self.store),
                             "--task-id", created["task_id"])
        view = json.loads(shown.stdout)
        self.assertEqual(view["events"][0]["action"], "request")
        self.assertTrue(SyntheticSqliteTaskStore(self.store).get(created["task_id"]))

    def test_cli_submits_then_records_clinician_review(self):
        created = json.loads(self.run_cli(
            "start", "--store", str(self.store), "--request", str(self.request_path),
        ).stdout)
        submitted = json.loads(self.run_cli(
            "submit", "--store", str(self.store), "--task-id", created["task_id"],
        ).stdout)
        self.assertEqual(submitted["status"], "needs_clinician_acceptance")
        reviewed = json.loads(self.run_cli(
            "review", "--store", str(self.store), "--task-id", created["task_id"],
            "--clinician", "demo-doctor", "--accept",
        ).stdout)
        self.assertEqual(reviewed["status"], "accepted")

    def test_cli_refuses_real_identity_and_keeps_store_private(self):
        self.request_path.write_text(json.dumps({
            "request_id": "cli-real",
            "capability_id": "write_inpatient_notes",
            "intent": "不应执行",
            "subject": {
                "site_id": "demo-site",
                "patient_id": "P-123456",
                "encounter_id": "synthetic-encounter",
            },
            "authority_ref": "demo://authority/synthetic",
        }, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("start", "--store", str(self.store),
                              "--request", str(self.request_path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("rejects non-demo identities", json.loads(result.stderr)["error"])
        self.assertTrue(self.store.is_file())
        if not is_windows():
            self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
