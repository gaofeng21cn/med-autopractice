from dataclasses import replace
from decimal import Decimal
import unittest
from unittest.mock import patch

from med_autopractice.adapters.fhir import FHIRObservationSource
from med_autopractice.domain.task import SubjectRef


class FHIRObservationTests(unittest.TestCase):
    def setUp(self):
        self.source = FHIRObservationSource("https://fhir.example/r4", "demo-site")
        self.subject = SubjectRef("demo-site", "synthetic-patient", "synthetic-encounter")
        self.encounter = {"subject": {"reference": "Patient/synthetic-patient"}, "status": "finished"}
        self.observation = {
            "subject": self.encounter["subject"], "encounter": {"reference": "Encounter/synthetic-encounter"},
            "status": "preliminary", "code": {"text": "合成检验"}, "meta": {"versionId": "2"},
            "valueQuantity": {"value": Decimal("0.050"), "comparator": "<", "unit": "mg/L"},
        }

    def read(self, **changes):
        with patch.object(self.source, "_get", side_effect=[self.encounter, {**self.observation, **changes}]):
            return self.source.read_quantity(self.subject, "synthetic-result")

    def test_quantity_keeps_precision_comparator_version_and_preliminary_status(self):
        fact = self.read()
        self.assertEqual((fact.value, fact.unit, fact.status), ("合成检验 <0.050", "mg/L", "uncertain"))
        self.assertTrue(fact.source_ref.endswith("Observation/synthetic-result/_history/2"))

    def test_mismatched_identity_and_unusable_results(self):
        for changes in ({"subject": {"reference": "Patient/other"}},
                        {"encounter": {"reference": "Encounter/other"}},
                        {"status": "entered-in-error"}, {"valueQuantity": {}},
                        {"component": [{"code": {"text": "another component"}}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(**changes)
        with patch.object(self.source, "_get") as read_resource:
            with self.assertRaises(ValueError):
                self.source.read_quantity(replace(self.subject, site_id="other"), "synthetic-result")
            read_resource.assert_not_called()

    def test_rejects_unsafe_endpoint_and_resource_ids(self):
        for endpoint in ("http://fhir.example/r4", "https://user@fhir.example"):
            with self.assertRaises(ValueError):
                FHIRObservationSource(endpoint, "demo-site")
        with self.assertRaises(ValueError):
            self.source.read_quantity(replace(self.subject, encounter_id=".."), "synthetic-result")
