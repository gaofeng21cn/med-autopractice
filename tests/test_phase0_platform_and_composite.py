"""Phase 0 tests: cross-platform primitives, composite routing, partial readiness, and task lifecycle."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from med_autopractice.adapters.composite import CompositeClinicalSystem
from med_autopractice.agent.task_engine import JsonlAuditSink, TaskEngine
from med_autopractice.agent.task_store import InMemoryTaskStore, SqliteTaskStore
from med_autopractice.domain.task import (
    ClinicianApproval, ExecutionReceipt, Fact, Proposal, Snapshot,
    SourceCoverage, SubjectRef, SystemReadback, TaskPolicy, TaskRequest,
)
from med_autopractice.platform.locking import exclusive_file_lock
from med_autopractice.platform.security import open_private_rw, verify_private_directory, verify_private_file
from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS
from med_autopractice.workflows.site_plan import SiteCatalog

ROOT = Path(__file__).resolve().parents[1]
SUBJECT = SubjectRef("demo-local", "synthetic-p01", "synthetic-e01")


class _StubAdapter:
    def __init__(self, name: str, facts: tuple[Fact, ...], coverage: tuple[SourceCoverage, ...],
                 *, subject_override: SubjectRef | None = None, can_write: bool = True,
                 screenshot: bool = True):
        self.name = name
        self.facts = facts
        self.coverage = coverage
        self.subject_override = subject_override
        self.can_write = can_write
        self.screenshot = screenshot
        self.revision = f"{name}-rev-1"
        self.reads = 0
        self.writes = 0
        self.written: dict[str, Proposal] = {}

    def read_context(self, subject: SubjectRef, capability_id: str) -> Snapshot:
        self.reads += 1
        return Snapshot(
            self.subject_override or subject,
            self.revision,
            self.facts,
            (f"demo://{self.name}/read",),
            self.coverage,
        )

    def can_apply(self, proposal: Proposal) -> bool:
        return self.can_write

    def apply(self, proposal: Proposal, idempotency_key: str, existing_object_ref: str | None) -> ExecutionReceipt:
        self.writes += 1
        ref = existing_object_ref or f"demo://{self.name}/obj/{self.writes}"
        self.written[ref] = proposal
        return ExecutionReceipt(ref, "saved_draft", (f"demo://{self.name}/write",))

    def read_back(self, receipt: ExecutionReceipt, task) -> SystemReadback:
        proposal = self.written[receipt.object_ref]
        return SystemReadback(
            proposal.subject,
            receipt.object_ref,
            proposal.digest,
            "saved_draft",
            f"demo://{self.name}/shot" if self.screenshot else "",
            (f"demo://{self.name}/readback",),
            dict(proposal.payload),
            "2026-01-01T12:00:00Z",
        )

    def find_submission(self, task):
        return None


def _build_catalog(transports: dict[str, tuple[str, str]]) -> SiteCatalog:
    doc = json.loads((ROOT / "contracts/site-dependencies.json").read_text(encoding="utf-8"))
    doc["profile_id"] = "demo-local"
    for item in doc["dependencies"]:
        dep_id = item["id"]
        if dep_id in transports:
            transport, state = transports[dep_id]
            item["state"] = state
            item["evidence_ref"] = f"demo://ev/{dep_id}"
            if state == "qualified":
                item["qualification_ref"] = f"demo://qual/{dep_id}"
            if item["owner"] == "adapter":
                item["transport"] = transport
        elif item["owner"] != "adapter":
            item["state"] = "qualified"
            item["evidence_ref"] = f"demo://ev/{dep_id}"
            item["qualification_ref"] = f"demo://qual/{dep_id}"
    return SiteCatalog(doc)


class PlatformPrimitivesTests(unittest.TestCase):
    def test_exclusive_file_lock_and_windows_security_bypass_only_posix_uid_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "locks" / "task.lock"
            counter = 0

            def bump(_):
                nonlocal counter
                with exclusive_file_lock(lock_path):
                    current = counter
                    counter = current + 1

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(bump, range(12)))
            self.assertEqual(counter, 12)

            cfg = root / "site.json"
            cfg.write_text('{"ok": true}', encoding="utf-8")
            with patch("med_autopractice.platform.security.is_windows", return_value=True):
                self.assertEqual(verify_private_directory(root, ROOT), root.resolve())
                self.assertEqual(json.loads(verify_private_file(cfg).decode("utf-8")), {"ok": True})
                open_private_rw(root / "db.sqlite")
            with self.assertRaises(PermissionError):
                verify_private_directory(ROOT / "docs", ROOT)


class CompositeClinicalSystemTests(unittest.TestCase):
    def test_routes_reads_across_native_api_and_computer_use_and_writes_to_ui(self):
        deps = {
            "bind_inpatient": ("computer_use", "qualified"),
            "read_inpatient_records": ("computer_use", "qualified"),
            "read_current_orders": ("computer_use", "qualified"),
            "read_nursing_records": ("computer_use", "qualified"),
            "read_lab_results": ("native_api", "qualified"),
            "read_imaging_reports": ("native_api", "qualified"),
            "save_inpatient_note_draft": ("computer_use", "qualified"),
            "readback_inpatient_note": ("computer_use", "qualified"),
        }
        catalog = _build_catalog(deps)
        ui_adapter = _StubAdapter(
            "cu",
            (Fact("ord-1", "阿司匹林", "demo://cu/ord-1"), Fact("nur-1", "生命体征平稳", "demo://cu/nur-1")),
            (
                SourceCoverage("HIS", "patient_context", True),
                SourceCoverage("HIS", "current_orders", True),
                SourceCoverage("NIS", "nursing", True),
            ),
        )
        api_adapter = _StubAdapter(
            "fhir",
            (Fact("lab-1", "WBC 6.5", "demo://fhir/lab-1"),),
            (SourceCoverage("LIS", "results", True),),
        )
        composite = CompositeClinicalSystem(
            catalog, {"computer_use": ui_adapter, "native_api": api_adapter},
        )
        snapshot = composite.read_context(SUBJECT, "write_inpatient_notes")
        self.assertEqual(ui_adapter.reads, 1)
        self.assertEqual(api_adapter.reads, 1)
        self.assertEqual([f.fact_id for f in snapshot.facts], ["ord-1", "nur-1", "lab-1"])
        self.assertTrue(snapshot.revision.startswith("composite:"))

        proposal = Proposal(SUBJECT, "write_inpatient_notes", {"draft": "合成病程"}, ("ord-1", "lab-1"), "demo://gen")
        self.assertTrue(composite.can_apply(proposal))
        receipt = composite.apply(proposal, "idem-1", None)
        self.assertEqual(ui_adapter.writes, 1)
        self.assertEqual(api_adapter.writes, 0)
        readback = composite.read_back(receipt, type("T", (), {"request": TaskRequest("r", "write_inpatient_notes", "i", SUBJECT, "a")})())
        self.assertEqual(readback.proposal_digest, proposal.digest)

    def test_rejects_mismatched_subject_and_conflicting_fact_ids(self):
        deps = {
            "bind_inpatient": ("computer_use", "qualified"),
            "read_inpatient_records": ("computer_use", "qualified"),
            "read_current_orders": ("computer_use", "qualified"),
            "read_nursing_records": ("computer_use", "qualified"),
            "read_lab_results": ("native_api", "qualified"),
            "read_imaging_reports": ("native_api", "qualified"),
        }
        catalog = _build_catalog(deps)
        bad_subject = _StubAdapter(
            "cu", (Fact("f1", "x", "demo://1"),), (SourceCoverage("HIS", "patient_context", True),),
            subject_override=SubjectRef("demo-local", "synthetic-other", "synthetic-e01"),
        )
        good_api = _StubAdapter("fhir", (Fact("f2", "y", "demo://2"),), (SourceCoverage("LIS", "results", True),))
        composite = CompositeClinicalSystem(catalog, {"computer_use": bad_subject, "native_api": good_api})
        with self.assertRaisesRegex(ValueError, "different subject"):
            composite.read_context(SUBJECT, "write_inpatient_notes")

        conflict_ui = _StubAdapter("cu", (Fact("shared", "A", "demo://1"),), (SourceCoverage("HIS", "patient_context", True),))
        conflict_api = _StubAdapter("fhir", (Fact("shared", "B", "demo://2"),), (SourceCoverage("LIS", "results", True),))
        composite2 = CompositeClinicalSystem(catalog, {"computer_use": conflict_ui, "native_api": conflict_api})
        with self.assertRaisesRegex(ValueError, "conflicting duplicate fact ID"):
            composite2.read_context(SUBJECT, "write_inpatient_notes")

    def test_partial_read_allowed_preserves_facts_and_blocks_write(self):
        deps = {
            "bind_inpatient": ("computer_use", "qualified"),
            "read_inpatient_records": ("computer_use", "qualified"),
            "read_current_orders": ("computer_use", "qualified"),
            "read_nursing_records": ("computer_use", "observed"),  # not qualified yet
            "read_lab_results": ("native_api", "qualified"),
            "read_imaging_reports": ("native_api", "qualified"),
            "save_inpatient_note_draft": ("computer_use", "observed"),
            "readback_inpatient_note": ("computer_use", "observed"),
        }
        catalog = _build_catalog(deps)
        ui_adapter = _StubAdapter("cu", (Fact("ord-1", "头孢", "demo://cu/1"),), (
            SourceCoverage("HIS", "patient_context", True),
            SourceCoverage("HIS", "current_orders", True),
        ))
        api_adapter = _StubAdapter("fhir", (Fact("lab-1", "CRP 12", "demo://fhir/1"),), (
            SourceCoverage("LIS", "results", True),
        ))
        strict_sys = CompositeClinicalSystem(catalog, {"computer_use": ui_adapter, "native_api": api_adapter}, partial_read_allowed=False)
        with self.assertRaises(PermissionError):
            strict_sys.read_context(SUBJECT, "write_inpatient_notes")

        partial_sys = CompositeClinicalSystem(catalog, {"computer_use": ui_adapter, "native_api": api_adapter}, partial_read_allowed=True)
        snap = partial_sys.read_context(SUBJECT, "write_inpatient_notes")
        self.assertEqual([f.fact_id for f in snap.facts], ["ord-1", "lab-1"])
        nursing_cov = [c for c in snap.coverage if c.scope == "nursing"][0]
        self.assertFalse(nursing_cov.complete)
        self.assertIn("unqualified:read_nursing_records", nursing_cov.gaps)
        proposal = Proposal(SUBJECT, "write_inpatient_notes", {"draft": "x"}, ("ord-1",), "demo://gen")
        self.assertFalse(partial_sys.can_apply(proposal))


class PartialDeploymentAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / "site"
        subprocess.run([sys.executable, str(ROOT / "examples/synthetic/local_deployment.py"), str(self.site)], check=True, capture_output=True)

    def test_partial_draft_allowed_deployment_and_cli_list_cancel(self):
        deps_path = self.site / "dependencies.json"
        deps = json.loads(deps_path.read_text(encoding="utf-8"))
        next(d for d in deps["dependencies"] if d["id"] == "read_nursing_records").update(
            state="observed", qualification_ref=None,
        )
        deps_path.write_text(json.dumps(deps, ensure_ascii=False, indent=2), encoding="utf-8")

        # Strict mode blocks start.
        strict_run = subprocess.run(
            [sys.executable, str(ROOT / "scripts/map_runtime.py"), "start", "--site", str(self.site),
             "--request", str(self.site / "write_inpatient_notes.json")],
            capture_output=True, text=True,
        )
        self.assertEqual(strict_run.returncode, 0)
        self.assertEqual(json.loads(strict_run.stdout)["status"], "blocked")

        # Enable partial_draft_allowed -> preserves partial snapshot and waits in needs_information.
        deploy_path = self.site / "deployment.json"
        deploy = json.loads(deploy_path.read_text(encoding="utf-8"))
        deploy["read_mode"] = "partial_draft_allowed"
        deploy_path.write_text(json.dumps(deploy, ensure_ascii=False, indent=2), encoding="utf-8")

        req_path = self.site / "write_inpatient_notes_partial.json"
        req = json.loads((self.site / "write_inpatient_notes.json").read_text(encoding="utf-8"))
        req["request_id"] = "demo-partial-note"
        req_path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")

        partial_run = subprocess.run(
            [sys.executable, str(ROOT / "scripts/map_runtime.py"), "start", "--site", str(self.site),
             "--request", str(req_path)],
            capture_output=True, text=True, check=True,
        )
        view = json.loads(partial_run.stdout)
        self.assertEqual(view["status"], "needs_information")
        self.assertIn("nursing", view["missing_information"])
        self.assertTrue(view["snapshot"]["facts"])
        self.assertIn(":partial:", view["snapshot"]["revision"])

        listed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/map_runtime.py"), "list", "--site", str(self.site),
             "--patient-id", "synthetic-p01"],
            capture_output=True, text=True, check=True,
        )
        tasks = json.loads(listed.stdout)["tasks"]
        self.assertEqual(len(tasks), 2)

        cancelled = subprocess.run(
            [sys.executable, str(ROOT / "scripts/map_runtime.py"), "cancel", "--site", str(self.site),
             "--task-id", view["task_id"], "--clinician", "demo-clinician", "--reason", "患者转科取消"],
            capture_output=True, text=True, check=True,
        )
        cview = json.loads(cancelled.stdout)
        self.assertEqual(cview["status"], "cancelled")
        self.assertEqual(cview["next_action"], "closed")

    def test_engine_cancel_guards_uncertain_and_accepted(self):
        store = InMemoryTaskStore()
        policy = LOCAL_WORKFLOWS["write_inpatient_notes"].policy("saved_draft")
        adapter = _StubAdapter("cu", (Fact("f1", "obs", "demo://1"),), (
            SourceCoverage("HIS", "patient_context", True),
            SourceCoverage("HIS", "current_orders", True),
            SourceCoverage("NIS", "nursing", True),
            SourceCoverage("LIS", "results", True),
        ))
        knowledge = type("K", (), {
            "propose": lambda self, req, snap: Proposal(SUBJECT, req.capability_id, {"draft": "ok"}, ("f1",), "demo://g")
        })()
        engine = TaskEngine(knowledge, adapter, JsonlAuditSink(Path(self.tmp.name) / "audit.jsonl"),
                            {"write_inpatient_notes": policy}, store=store)
        t = engine.start(TaskRequest("r-1", "write_inpatient_notes", "i", SUBJECT, "demo://a"), auto_submit=True)
        engine.review(t.task_id, "doc", accepted=True)
        with self.assertRaisesRegex(ValueError, "cannot cancel"):
            engine.cancel(t.task_id, "too late")
        uncertain_adapter = _StubAdapter("cu2", (Fact("f1", "obs", "demo://1"),), adapter.coverage, screenshot=False)
        uncertain_engine = TaskEngine(
            knowledge, uncertain_adapter, JsonlAuditSink(Path(self.tmp.name) / "audit2.jsonl"),
            {"write_inpatient_notes": policy}, store=store,
        )
        t_uncertain = uncertain_engine.start(
            TaskRequest("r-uncertain", "write_inpatient_notes", "i", SUBJECT, "demo://a"),
            auto_submit=True,
        )
        self.assertEqual(t_uncertain.status, "execution_uncertain")
        with self.assertRaisesRegex(ValueError, "cannot cancel"):
            uncertain_engine.cancel(t_uncertain.task_id, "must reconcile first")
        t2 = engine.start(TaskRequest("r-2", "write_inpatient_notes", "i", SUBJECT, "demo://a"))
        c2 = engine.cancel(t2.task_id, "不再需要")
        self.assertEqual(c2.status, "cancelled")
        self.assertEqual(len(engine.list_tasks(status="cancelled")), 1)


if __name__ == "__main__":
    unittest.main()
