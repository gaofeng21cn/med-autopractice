"""Run the explicit site entry in fresh processes against a synthetic connector."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS


ROOT = Path(__file__).resolve().parents[1]


class SiteRuntimeTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.site = Path(root.name) / "deployment"
        subprocess.run([sys.executable, str(ROOT / "examples/synthetic/local_deployment.py"), str(self.site)],
                       check=True, capture_output=True)

    def cli(self, action, *args, ok=True):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/map_runtime.py"), action, "--site", str(self.site), *args],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0 if ok else 2, completed.stderr)
        return json.loads(completed.stdout if ok else completed.stderr)

    def start(self, capability):
        return self.cli("start", "--request", str(self.site / f"{capability}.json"))

    def submit(self, view, clinician="demo-clinician", ok=True):
        return self.cli(
            "submit", "--task-id", view["task_id"], "--clinician", clinician,
            "--digest", view["proposal"]["digest"], "--revision", view["snapshot"]["revision"], ok=ok,
        )

    def written_count(self):
        with closing(sqlite3.connect(self.site / "demo-target.sqlite")) as db:
            return db.execute("SELECT count(*) FROM written").fetchone()[0]

    def test_every_workflow_through_public_entry_and_exact_review(self):
        for key, workflow in LOCAL_WORKFLOWS.items():
            with self.subTest(key=key):
                view = self.start(key)
                if workflow.write_dependency:
                    self.assertEqual(view["status"], "needs_pre_submit_review")
                    view = self.submit(view)
                    self.assertEqual(view["status"], "needs_clinician_acceptance")
                    self.assertTrue(view["execution"]["readback"]["resource_version_ref"])
                else:
                    self.assertEqual(view["status"], "review_result")
                    self.assertIsNone(view["execution"]["readback"])
                reviewed = self.cli("review", "--task-id", view["task_id"], "--clinician", "demo-clinician",
                                    "--accept", "--digest", view["proposal"]["digest"])
                self.assertEqual(reviewed["status"], "accepted")
        self.assertEqual(self.written_count(), 7)

    def test_rejects_actor_and_stale_facts_then_refreshes_same_task(self):
        first = self.start("revise_orders")
        self.assertEqual(self.submit(first, clinician="demo-unauthorized", ok=False)["error"], "PermissionError")
        (self.site / "demo-revision").write_text("demo-revision-2")
        self.assertEqual(self.submit(first)["status"], "needs_refresh")
        self.assertEqual(self.written_count(), 0)
        second = self.cli("refresh", "--task-id", first["task_id"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(second["snapshot"]["revision"], "demo-revision-2")
        self.assertEqual(self.submit(first, ok=False)["error"], "ValueError")
        self.assertEqual(self.submit(second)["status"], "needs_clinician_acceptance")
        self.assertEqual(self.written_count(), 1)

    def test_response_loss_never_replays_and_reconciles_after_restart(self):
        first = self.start("revise_orders")
        (self.site / "simulate-lost-response").touch()
        self.assertEqual(self.submit(first, ok=False)["error"], "ConnectionError")
        viewed = self.cli("show", "--task-id", first["task_id"])
        self.assertEqual(viewed["status"], "execution_uncertain")
        self.submit(first, ok=False)
        final = self.cli("reconcile", "--task-id", first["task_id"])
        self.assertEqual(final["status"], "needs_clinician_acceptance")
        self.assertEqual(self.written_count(), 1)

    def test_batch_resumes_and_projections_distinguish_read_only_results(self):
        first = self.cli("batch", "--requests", str(self.site / "batch.json"))
        second = self.cli("batch", "--requests", str(self.site / "batch.json"))
        self.assertEqual(first, second)
        payloads = {row["proposal"]["payload"]["type"]: row["proposal"]["payload"] for row in first["requests"]}
        self.assertEqual(len(payloads["history"]["record"]["encounters"]), 2)
        self.assertEqual(payloads["document_deadlines"]["documents"][0]["timing"], "overdue")
        self.assertEqual(payloads["cohort_decision"]["status"], "included")
        self.assertEqual(payloads["followup_schedule"]["events"][0]["status"], "overdue")
        self.assertEqual(self.written_count(), 0)

    def test_revoked_write_qualification_leaves_reviewable_draft(self):
        profile = self.site / "dependencies.json"
        data = json.loads(profile.read_text())
        next(dep for dep in data["dependencies"] if dep["id"] == "enter_inpatient_orders")["state"] = "revoked"
        profile.write_text(json.dumps(data))
        view = self.start("revise_orders")
        self.assertEqual(view["status"], "draft_only")
        self.submit(view, ok=False)
        self.assertEqual(self.written_count(), 0)

    def test_concurrent_submission_has_one_external_write(self):
        task = self.start("write_inpatient_notes")
        (self.site / "simulate-write-delay").touch()

        def submit_once(_):
            return subprocess.run(
                [sys.executable, str(ROOT / "scripts/map_runtime.py"), "submit",
                 "--site", str(self.site), "--task-id", task["task_id"],
                 "--clinician", "demo-clinician", "--digest", task["proposal"]["digest"],
                 "--revision", task["snapshot"]["revision"]],
                capture_output=True, timeout=15,
            ).returncode

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(submit_once, range(2))), [0, 2])
        self.assertEqual(self.written_count(), 1)

    def test_readback_change_after_submission_requires_reconciliation(self):
        task = self.submit(self.start("write_inpatient_notes"))
        with closing(sqlite3.connect(self.site / "demo-target.sqlite")) as db, db:
            db.execute("UPDATE written SET payload = ?", ('{"type": "different"}',))
        result = self.cli("review", "--task-id", task["task_id"], "--clinician", "demo-clinician",
                          "--accept", "--digest", task["proposal"]["digest"])
        self.assertEqual(result["status"], "execution_uncertain")
        self.assertEqual(result["review"]["decisions"], [])

    def test_clinician_can_provide_missing_plan_without_starting_a_new_task(self):
        request = self.site / "revise_orders.json"
        body = json.loads(request.read_text())
        plan = body.pop("inputs")
        request.write_text(json.dumps(body))
        first = self.start("revise_orders")
        self.assertEqual(first["status"], "needs_input")
        self.assertEqual(first["required_input"], ["changes"])
        self.assertTrue(first["snapshot"]["facts"])
        inputs = self.site / "plan.json"
        inputs.write_text(json.dumps(plan))
        prepared = self.cli("refresh", "--task-id", first["task_id"], "--inputs", str(inputs))
        self.assertEqual(prepared["task_id"], first["task_id"])
        self.assertEqual(prepared["status"], "needs_pre_submit_review")
        self.assertEqual(self.start("revise_orders"), prepared)
        self.assertEqual(self.submit(prepared)["status"], "needs_clinician_acceptance")
