import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from med_autopractice.workflows.site_playbook import SitePlaybook, write_plan
from scripts.site_playbook import _external


ROOT = Path(__file__).resolve().parents[1]


def step(step_id="open", **overrides):
    body = {
        "id": step_id,
        "intent": "navigation",
        "primitive": "kvm_click",
        "target": "portal tile",
        "region_hint": [10, 20, 110, 90],
        "expect_text": "门户",
        "effect": "read_only",
        "on_failure": "reobserve",
    }
    body.update(overrides)
    return body


def playbook(**overrides):
    body = {
        "schema_version": "map-site-playbook.v1",
        "playbook_id": "demo",
        "site_id": "demo-site",
        "title": "demo playbook",
        "systems": ["portal"],
        "steps": [step()],
    }
    body.update(overrides)
    return body


class SitePlaybookTests(unittest.TestCase):
    def test_public_site_examples_run_through_read_only_cli(self):
        examples = ROOT / "examples/synthetic"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copyfile(examples / "survey.json", target / "survey.json")
            shutil.copytree(examples / "playbooks", target / "playbooks")
            survey = subprocess.run(
                [sys.executable, str(ROOT / "scripts/site_survey.py"), "--profile",
                 str(target / "survey.json"), "--dependency", "bind_inpatient"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(survey.stdout)["blockers"], ["bind_inpatient"])
            book = subprocess.run(
                [sys.executable, str(ROOT / "scripts/site_playbook.py"), "--survey",
                 str(target / "survey.json"), "--playbooks", str(target / "playbooks"),
                 "--playbook", "demo-open-overview"],
                capture_output=True, text=True, check=True,
            )
            plan = json.loads(book.stdout)
            self.assertFalse(plan["contains_write"])
            self.assertEqual(plan["verified_steps"], 0)

    def test_valid_playbook_projects_an_ordered_plan(self):
        book = SitePlaybook(playbook(steps=[
            step("first"), step("second", primitive="kvm_wait", effect="reviewed_write"),
        ])).playbook
        plan = write_plan(book)
        self.assertEqual([item["id"] for item in plan["steps"]], ["first", "second"])
        self.assertTrue(book.contains_write)
        self.assertTrue(plan["contains_write"])
        json.dumps(plan, ensure_ascii=False)

    def test_read_only_playbook_is_not_flagged_as_write(self):
        self.assertFalse(SitePlaybook(playbook()).playbook.contains_write)

    def test_steps_default_to_unverified_and_can_be_marked_verified(self):
        book = SitePlaybook(playbook(steps=[
            step("documented"), step("reproduced", state="verified"),
        ])).playbook
        self.assertEqual(book.steps[0].state, "unverified")
        self.assertFalse(book.steps[0].is_verified)
        self.assertEqual(book.verified_steps, 1)
        self.assertFalse(book.all_steps_verified)
        plan = write_plan(book)
        self.assertEqual(plan["verified_steps"], 1)
        self.assertEqual(plan["steps_total"], 2)
        self.assertEqual([item["state"] for item in plan["steps"]],
                         ["unverified", "verified"])

    def test_all_steps_verified_requires_every_step(self):
        book = SitePlaybook(playbook(steps=[step("a", state="verified")])).playbook
        self.assertTrue(book.all_steps_verified)
        self.assertTrue(write_plan(book)["all_steps_verified"])

    def test_unknown_step_state_is_rejected(self):
        with self.assertRaises(ValueError):
            SitePlaybook(playbook(steps=[step(state="probably")]))

    def test_credential_and_patient_fields_are_rejected(self):
        for field in ("password", "token", "patient_id", "screenshot_data"):
            with self.assertRaisesRegex(ValueError, "forbidden field"):
                SitePlaybook(playbook(**{field: "must-not-be-stored"}))
            with self.assertRaisesRegex(ValueError, "forbidden field"):
                SitePlaybook(playbook(steps=[step(**{field: "must-not-be-stored"})]))

    def test_invalid_step_contract_is_rejected(self):
        with self.assertRaises(ValueError):
            SitePlaybook(playbook(steps=[step(effect="silent_write")]))
        with self.assertRaises(ValueError):
            SitePlaybook(playbook(steps=[step(region_hint=[10, 20, 5, 90])]))
        with self.assertRaises(ValueError):
            SitePlaybook(playbook(steps=[step("dup"), step("dup")]))
        with self.assertRaises(ValueError):
            SitePlaybook(playbook(schema_version="map-site-playbook.v99"))

    def test_v2_transport_agnostic_playbook_is_supported(self):
        v2 = playbook(
            schema_version="map-site-playbook.v2",
            steps=[
                step(
                    "bind",
                    primitive="verify_subject",
                    selector="#patient-banner",
                    expect_text="住院号",
                ),
                step(
                    "collect_labs",
                    primitive="scroll_collect",
                    selector="table.labs",
                    extract_key="labs",
                    scope="results",
                    max_pages=5,
                    expect_text="检验结果",
                ),
                step(
                    "write_note",
                    primitive="input_text",
                    selector="textarea#note",
                    text_param="draft_text",
                    effect="draft_write",
                    expect_text="已保存",
                ),
            ],
        )
        parsed = SitePlaybook(v2, {"portal"})
        self.assertEqual(parsed.schema_version, "map-site-playbook.v2")
        self.assertEqual(parsed.playbook.steps[1].max_pages, 5)
        self.assertEqual(parsed.playbook.steps[2].text_param, "draft_text")
        self.assertTrue(parsed.playbook.contains_write)
        plan = write_plan(parsed.playbook)
        self.assertEqual(plan["steps"][1]["extract_key"], "labs")

    def test_playbook_must_reference_surveyed_systems(self):
        with self.assertRaisesRegex(ValueError, "absent from the site survey"):
            SitePlaybook(playbook(systems=["unmapped-system"]), {"portal"})

    def test_directory_load_requires_unique_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.json").write_text(json.dumps(playbook()), encoding="utf-8")
            self.assertEqual(len(SitePlaybook.load_directory(root, {"portal"})), 1)
            (root / "b.json").write_text(json.dumps(playbook()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unique"):
                SitePlaybook.load_directory(root, {"portal"})

    def test_playbooks_inside_the_source_repository_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the source repository"):
            _external(ROOT / "site-local" / "playbook.json")
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "playbook.json"
            self.assertEqual(_external(selected), selected.resolve())


if __name__ == "__main__":
    unittest.main()
