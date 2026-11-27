"""The native carrier must carry a runnable, truthful MAP package."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageManagementTests(unittest.TestCase):
    def materialize(self, target):
        lock = json.loads((ROOT / "contracts/package_content_lock.json").read_text())
        for relative in ["opl-package.json", "contracts/package_content_lock.json", *lock["paths"]]:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)

    def test_materialized_product_knowledge_matches_catalog_and_skill_links(self):
        with tempfile.TemporaryDirectory(prefix="map-knowledge-") as temporary:
            target = Path(temporary)
            self.materialize(target)
            catalog = json.loads((target / "contracts/durable_knowledge_catalog.json").read_text(encoding="utf-8"))
            items = {item["knowledge_id"]: item["body_ref"] for item in catalog["items"]}
            for skill, knowledge_id in (
                ("map-site-adaptation", "med-autopractice.site-adaptation"),
                ("med-autopractice", "med-autopractice.clinical-record-review"),
            ):
                body_ref = items[knowledge_id]
                body = (target / body_ref["ref"]).read_bytes()
                self.assertEqual(body_ref["sha256"], "sha256:" + hashlib.sha256(body).hexdigest())
                skill_text = (target / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn(f"](../../{body_ref['ref']})", skill_text)

    def test_materialized_package_runs_without_developer_checkout(self):
        with tempfile.TemporaryDirectory(prefix="map-package-") as temporary:
            target = Path(temporary)
            self.materialize(target)
            result = subprocess.run([sys.executable, str(target / "scripts/project_status.py"), "--json"],
                                    cwd=target.parent, capture_output=True, text=True, check=True)
            status = json.loads(result.stdout)
            self.assertEqual(status["package"]["content_integrity"], "verified")
            self.assertEqual(status["package"]["package_id"], "med-autopractice")
            self.assertFalse(status["clinical_execution_available"])
            self.assertEqual(status["callable_capabilities"], [])
            help_result = subprocess.run([sys.executable, str(target / "scripts/map_runtime.py"), "--help"],
                                         cwd=target.parent, capture_output=True, text=True, check=True)
            self.assertIn("synthetic", help_result.stdout)
            demo = subprocess.run(
                [sys.executable, str(target / "scripts/map_demo.py")],
                cwd=target.parent, capture_output=True, text=True, check=True,
            )
            view = json.loads(demo.stdout)
            self.assertEqual(view["mode"], "synthetic")
            self.assertEqual(view["statuses"], [
                "needs_pre_submit_review", "needs_clinician_acceptance", "accepted",
            ])
            self.assertEqual(view["readback"]["object_ref"], "demo://ui/object/1")
            with tempfile.TemporaryDirectory(prefix="map-deployment-") as directory:
                site = Path(directory) / "site"
                subprocess.run(
                    [sys.executable, str(target / "examples/synthetic/local_deployment.py"), str(site)],
                    cwd=directory, capture_output=True, check=True,
                )
                result = subprocess.run(
                    [sys.executable, str(target / "scripts/map_runtime.py"), "batch",
                     "--site", str(site), "--requests", str(site / "batch.json")],
                    cwd=directory, capture_output=True, text=True, check=True,
                )
                rows = json.loads(result.stdout)["requests"]
                self.assertEqual({item["proposal"]["payload"]["type"] for item in rows},
                                 {"history", "document_deadlines", "cohort_decision", "followup_schedule"})

    def test_materialized_package_rejects_changed_content(self):
        with tempfile.TemporaryDirectory(prefix="map-package-") as temporary:
            target = Path(temporary)
            self.materialize(target)
            capabilities = target / "contracts/capabilities.json"
            capabilities.write_text(capabilities.read_text() + "\n")
            result = subprocess.run([sys.executable, str(target / "scripts/project_status.py"), "--json"],
                                    cwd=target, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("content lock", result.stderr)

    def test_installed_site_skill_can_inspect_only_the_requested_operation(self):
        with tempfile.TemporaryDirectory(prefix="map-site-") as temporary:
            folder = Path(temporary)
            target = folder / "plugin"
            self.materialize(target)
            manifest = json.loads((target / "opl-package.json").read_text())
            skill_ids = [item["id"] for item in manifest["provides"]["skills"]]
            self.assertIn("map-site-adaptation", skill_ids)
            for skill in skill_ids:
                self.assertTrue((target / "skills" / skill / "SKILL.md").is_file())
            cli = [sys.executable, str(target / "scripts/site_plan.py")]
            env = os.environ.copy()
            env.pop("MAP_SITE_PROFILE", None)
            def inspect(*args):
                run = subprocess.run([*cli, *args], cwd=folder, env=env, text=True,
                                     capture_output=True, check=True)
                return json.loads(run.stdout)

            self.assertEqual(inspect("--inspect"), {"profile_source": "template", "profile_id": "generic-unconfigured"})
            profile = folder / "deployment.json"
            self.assertEqual(inspect("--init-profile", str(profile))["profile_source"], "external")
            changed = json.loads(profile.read_text())
            operation = next(d for d in changed["dependencies"] if d["id"] == "read_lab_results")
            operation.update(transport="middleware", state="observed", evidence_ref="demo://survey/reading")
            profile.write_text(json.dumps(changed))
            env["MAP_SITE_PROFILE"] = str(profile)
            one = inspect("--inspect", "--dependency", "read_lab_results")
            self.assertEqual(one["profile_source"], "external")
            self.assertEqual(one["dependency"]["transport"], "middleware")
            self.assertEqual(one["dependency"]["evidence_ref"], "demo://survey/reading")
            self.assertNotIn("plan", one)
            plan = inspect("--inspect", "write_inpatient_notes")["plan"]
            self.assertIn("read_lab_results", plan["blockers"])
            self.assertEqual(inspect("write_inpatient_notes"), [plan])
            profile.unlink()
            failure = subprocess.run([*cli, "--inspect"], cwd=folder, env=env, text=True, capture_output=True)
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn("does not exist", failure.stderr)


if __name__ == "__main__":
    unittest.main()
