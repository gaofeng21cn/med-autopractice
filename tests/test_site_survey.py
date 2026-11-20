import json
from pathlib import Path
import tempfile
import unittest

from med_autopractice.workflows.site_survey import SiteSurveyCatalog
from scripts.site_survey import resolve_profile_path


class SiteSurveyTests(unittest.TestCase):
    def setUp(self):
        self.document = {
            "schema_version": "map-site-survey.v1",
            "profile_id": "synthetic-deployment",
            "systems": [{"id": "ehr", "role": "inpatient_ehr"}],
            "operations": [{
                "dependency_id": "bind_inpatient",
                "system_id": "ehr",
                "semantic_action": "bind the selected inpatient encounter",
                "mode": "read",
                "state": "observed",
                "evidence_refs": ["private://survey/run-1"],
                "qualification_ref": None,
                "notes_ref": "private://survey/notes/bind",
            }],
        }

    def test_observed_operation_blocks_plan(self):
        plan = SiteSurveyCatalog(self.document).plan("write_inpatient_notes")
        self.assertFalse(plan.qualified)
        self.assertEqual(plan.blockers, ("bind_inpatient",))

    def test_qualified_operation_requires_evidence_and_qualification(self):
        self.document["operations"][0]["state"] = "qualified"
        with self.assertRaises(ValueError):
            SiteSurveyCatalog(self.document)
        self.document["operations"][0]["qualification_ref"] = "private://qualification/1"
        self.assertTrue(SiteSurveyCatalog(self.document).plan().qualified)

    def test_patient_and_secret_fields_are_rejected(self):
        self.document["operations"][0]["patient_id"] = "must-not-enter"
        with self.assertRaisesRegex(ValueError, "forbidden field"):
            SiteSurveyCatalog(self.document)

    def test_profile_must_be_external(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "survey.json"
            path.write_text(json.dumps(self.document))
            self.assertEqual(resolve_profile_path(path), path.resolve())


if __name__ == "__main__":
    unittest.main()
