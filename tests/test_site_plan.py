import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from med_autopractice.workflows.catalog import PHASE1_WORKFLOWS, PILOT_WORKFLOWS
from med_autopractice.workflows.site_plan import SiteCatalog
from scripts.site_plan import DEFAULT_PROFILE, initialize_profile, resolve_profile_path


ROOT = Path(__file__).resolve().parents[1]


class SitePlanTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((ROOT / "contracts/site-dependencies.json").read_text())
        self.catalog = SiteCatalog(self.document)

    def test_all_phase_one_capabilities_have_plans_without_false_qualification(self):
        capabilities = json.loads((ROOT / "contracts/capabilities.json").read_text())
        phase_one = {item["id"] for item in capabilities["capabilities"] if item["phase"] == "phase1"}
        self.assertEqual(set(PHASE1_WORKFLOWS), phase_one)
        self.assertEqual(set(PILOT_WORKFLOWS), set(capabilities["first_pilot"]))
        for capability_id in phase_one:
            plan = self.catalog.plan(capability_id)
            self.assertFalse(plan.dependencies_qualified)
            self.assertTrue(plan.blockers)
            self.assertEqual(plan.steps[-1].phase, "clinician_acceptance")

    def test_order_plan_keeps_medical_proposal_and_two_clinician_checkpoints(self):
        plan = self.catalog.plan("revise_orders")
        phases = [step.phase for step in plan.steps]
        self.assertLess(phases.index("medical_proposal"), phases.index("clinician_approval"))
        self.assertLess(phases.index("clinician_approval"), phases.index("apply"))
        self.assertLess(phases.index("system_readback"), phases.index("clinician_acceptance"))
        self.assertIn("read_nursing_records", plan.blockers)
        self.assertIn("enter_inpatient_orders", plan.blockers)

    def test_read_only_workflow_has_no_submission_policy(self):
        with self.assertRaises(ValueError):
            PHASE1_WORKFLOWS["collect_history"].policy("entered")

    def test_observed_ui_is_not_qualified_execution(self):
        plan = self.catalog.plan("collect_history")
        self.assertIn("read_inpatient_records", plan.blockers)
        self.assertIn("read_lab_results", plan.blockers)
        self.assertIn("read_imaging_reports", plan.blockers)

    def test_qualification_requires_explicit_evidence(self):
        for item in self.document["dependencies"]:
            if item["id"] == "enter_inpatient_orders":
                item["state"] = "qualified"
        with self.assertRaises(ValueError):
            SiteCatalog(self.document)

    def test_catalog_rejects_missing_workflow_dependency(self):
        self.document["dependencies"] = [item for item in self.document["dependencies"]
                                          if item["id"] != "read_current_orders"]
        with self.assertRaises(ValueError):
            SiteCatalog(self.document)


class SiteProfilePathTests(unittest.TestCase):
    def test_default_uses_generic_repository_contract(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_profile_path(None), DEFAULT_PROFILE)

    def test_explicit_path_overrides_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "site-profile.json"
            with patch.dict(os.environ, {"MAP_SITE_PROFILE": "/ignored/profile.json"}):
                self.assertEqual(resolve_profile_path(selected), selected.resolve())

    def test_private_profiles_inside_repository_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the source repository"):
            resolve_profile_path(ROOT / "site-local" / "profile.json")

    def test_initialization_preserves_existing_site_and_rejects_git_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            target = folder / "site-profile.json"
            initialize_profile(target)
            self.assertEqual(target.read_bytes(), DEFAULT_PROFILE.read_bytes())
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            target.write_text("existing private configuration")
            with self.assertRaises(FileExistsError):
                initialize_profile(target)
            self.assertEqual(target.read_text(), "existing private configuration")
            (folder / ".git").touch()  # A linked worktree has a .git file.
            with self.assertRaisesRegex(ValueError, "outside Git"):
                initialize_profile(folder / "another.json")
            self.assertEqual({p.name for p in folder.iterdir()}, {".git", target.name})

    def test_external_profile_drives_read_only_plan_state(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "site-profile.json"
            profile = json.loads(DEFAULT_PROFILE.read_text())
            profile["profile_id"] = "synthetic-test-deployment"
            dependency = next(item for item in profile["dependencies"]
                              if item["id"] == "read_current_orders")
            dependency["state"] = "observed"
            dependency["evidence_ref"] = "synthetic://evidence/orders-list"
            selected.write_text(json.dumps(profile))
            catalog = SiteCatalog.load(resolve_profile_path(selected))
            self.assertEqual(catalog.plan("revise_orders").steps[1].state, "observed")
            self.assertIn("read_current_orders", catalog.plan("revise_orders").blockers)


if __name__ == "__main__":
    unittest.main()
