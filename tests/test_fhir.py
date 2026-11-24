from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from med_autopractice.adapters.fhir import FHIRClinicalSystem, FHIRObservationSource
from med_autopractice.domain.task import Proposal, SubjectRef, Task, TaskRequest


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


class FHIRClinicalSystemTests(unittest.TestCase):
    def _start_server(self, state: dict):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _send(self, code: int, obj: dict):
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/fhir+json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)
                if path == "/fhir/Encounter/enc-1":
                    self._send(200, {
                        "resourceType": "Encounter", "id": "enc-1", "status": "in-progress",
                        "subject": {"reference": "Patient/pat-1"}, "meta": {"versionId": "1"},
                    })
                elif path == "/fhir/Observation":
                    page = qs.get("page", ["1"])[0]
                    if state.get("evil_next"):
                        self._send(200, {
                            "resourceType": "Bundle",
                            "link": [{"relation": "next", "url": "https://evil.example.invalid/fhir/Observation?page=2"}],
                            "entry": [],
                        })
                        return
                    if page == "1":
                        base = state["base_url"]
                        self._send(200, {
                            "resourceType": "Bundle",
                            "link": [{"relation": "next", "url": f"{base}/Observation?patient=pat-1&encounter=enc-1&page=2"}],
                            "entry": [{"resource": {
                                "resourceType": "Observation", "id": "obs-1", "status": "final",
                                "subject": {"reference": "Patient/pat-1"}, "encounter": {"reference": "Encounter/enc-1"},
                                "code": {"text": "WBC"}, "valueQuantity": {"value": 11.5, "unit": "10^9/L"},
                                "meta": {"versionId": "1"},
                            }}],
                        })
                    elif page == "2":
                        nxt = f"{state['base_url']}/Observation?patient=pat-1&encounter=enc-1&page=3" if state.get("infinite_pages") else None
                        links = [{"relation": "next", "url": nxt}] if nxt else []
                        self._send(200, {
                            "resourceType": "Bundle", "link": links,
                            "entry": [{"resource": {
                                "resourceType": "Observation", "id": "obs-2", "status": "final",
                                "subject": {"reference": "Patient/pat-1"}, "encounter": {"reference": "Encounter/enc-1"},
                                "code": {"text": "CRP"}, "valueQuantity": {"value": 24, "unit": "mg/L"},
                                "meta": {"versionId": "1"},
                            }}],
                        })
                    else:
                        self._send(200, {
                            "resourceType": "Bundle",
                            "link": [{"relation": "next", "url": f"{state['base_url']}/Observation?patient=pat-1&encounter=enc-1&page={int(page) + 1}"}],
                            "entry": [],
                        })
                elif path == "/fhir/MedicationRequest":
                    if "identifier" in qs:
                        token = qs["identifier"][0].split("|", 1)[-1]
                        hits = [
                            {"resource": r}
                            for r in state["meds"].values()
                            if any(i.get("value") == token for i in r.get("identifier", []))
                        ]
                        self._send(200, {"resourceType": "Bundle", "entry": hits})
                    else:
                        # Includes one matching-encounter order and one cross-encounter order that MUST be filtered out
                        self._send(200, {"resourceType": "Bundle", "entry": [
                            {"resource": state["meds"]["ord-1"]},
                            {"resource": {
                                "resourceType": "MedicationRequest", "id": "ord-foreign-enc", "status": "active",
                                "subject": {"reference": "Patient/pat-1"},
                                "encounter": {"reference": "Encounter/enc-other"},
                                "medicationCodeableConcept": {"coding": [{"code": "FOREIGN-MED"}], "text": "FOREIGN-MED"},
                                "meta": {"versionId": "1"},
                            }},
                        ]})
                elif path.startswith("/fhir/MedicationRequest/"):
                    rid = path.rsplit("/", 1)[-1]
                    if rid in state["meds"]:
                        self._send(200, state["meds"][rid])
                    else:
                        self._send(404, {"error": "not found"})
                elif path == "/fhir/DocumentReference":
                    # Includes one matching-encounter document and one missing-encounter document
                    self._send(200, {"resourceType": "Bundle", "entry": [
                        {"resource": {
                            "resourceType": "DocumentReference", "id": "doc-ok", "status": "current",
                            "subject": {"reference": "Patient/pat-1"},
                            "context": {"encounter": [{"reference": "Encounter/enc-1"}]},
                            "description": "护理交班记录", "meta": {"versionId": "1"},
                        }},
                        {"resource": {
                            "resourceType": "DocumentReference", "id": "doc-wrong-enc", "status": "current",
                            "subject": {"reference": "Patient/pat-1"},
                            "context": {"encounter": [{"reference": "Encounter/enc-999"}]},
                            "description": "外院区旧记录", "meta": {"versionId": "1"},
                        }},
                    ]})
                else:
                    self._send(404, {"error": f"unknown {path}"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/fhir/MedicationRequest":
                    rid = f"ord-new-{len(state['meds']) + 1}"
                    saved = {
                        **body,
                        "id": rid,
                        "meta": {"versionId": "1", "lastUpdated": "2026-01-01T12:00:00Z"},
                    }
                    state["meds"][rid] = saved
                    state["writes"].append(("POST", rid, body, self.headers.get("If-Match")))
                    self._send(201, saved)
                else:
                    self._send(404, {"error": "unknown post"})

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path.startswith("/fhir/MedicationRequest/"):
                    rid = self.path.rsplit("/", 1)[-1]
                    prev = state["meds"].get(rid, {})
                    prev_vid = int((prev.get("meta") or {}).get("versionId") or 1)
                    saved = {
                        **body,
                        "id": rid,
                        "meta": {"versionId": str(prev_vid + 1), "lastUpdated": "2026-01-01T12:05:00Z"},
                    }
                    state["meds"][rid] = saved
                    state["writes"].append(("PUT", rid, body, self.headers.get("If-Match")))
                    self._send(200, saved)
                else:
                    self._send(404, {"error": "unknown put"})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        state["base_url"] = f"http://127.0.0.1:{srv.server_address[1]}/fhir"
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return state["base_url"]

    def test_read_pagination_encounter_filtering_and_honest_coverage(self):
        state = {
            "meds": {
                "ord-1": {
                    "resourceType": "MedicationRequest", "id": "ord-1", "status": "active",
                    "subject": {"reference": "Patient/pat-1"},
                    "encounter": {"reference": "Encounter/enc-1"},
                    "medicationCodeableConcept": {"coding": [{"code": "MED-A"}], "text": "MED-A"},
                    "meta": {"versionId": "2"},
                }
            },
            "writes": [],
        }
        base_url = self._start_server(state)
        subject = SubjectRef("site-fhir", "pat-1", "enc-1")

        # Read-only by default
        fhir_ro = FHIRClinicalSystem(base_url, "site-fhir")
        snap = fhir_ro.read_context(subject, "revise_orders")
        fact_ids = {f.fact_id for f in snap.facts}
        self.assertEqual(fact_ids, {"Observation/obs-1", "Observation/obs-2", "MedicationRequest/ord-1", "DocumentReference/doc-ok"})
        cov_map = {c.scope: c for c in snap.coverage}
        self.assertTrue(cov_map["results"].complete)
        self.assertNotIn("nursing", cov_map)

        # Exceeding max_pages marks results coverage incomplete with pagination_truncated
        state["infinite_pages"] = True
        fhir_capped = FHIRClinicalSystem(base_url, "site-fhir", max_pages=2)
        capped_snap = fhir_capped.read_context(subject, "revise_orders")
        res_cov = next(c for c in capped_snap.coverage if c.scope == "results")
        self.assertFalse(res_cov.complete)
        self.assertIn("pagination_truncated", res_cov.gaps)
        state["infinite_pages"] = False

        # Cross-origin next link is strictly rejected
        state["evil_next"] = True
        with self.assertRaises(ValueError):
            fhir_ro.read_context(subject, "revise_orders")

    def test_standard_r4_medication_request_write_and_honest_draft_vs_active_readback(self):
        state = {
            "meds": {
                "ord-1": {
                    "resourceType": "MedicationRequest", "id": "ord-1", "status": "active",
                    "subject": {"reference": "Patient/pat-1"},
                    "encounter": {"reference": "Encounter/enc-1"},
                    "medicationCodeableConcept": {"coding": [{"code": "DEMO-MED"}], "text": "DEMO-MED"},
                    "meta": {"versionId": "3"},
                },
                "ord-2": {
                    "resourceType": "MedicationRequest", "id": "ord-2", "status": "active",
                    "subject": {"reference": "Patient/pat-1"},
                    "encounter": {"reference": "Encounter/enc-1"},
                    "medicationCodeableConcept": {"coding": [{"code": "STOP-MED"}], "text": "STOP-MED"},
                    "meta": {"versionId": "4"},
                },
            },
            "writes": [],
        }
        base_url = self._start_server(state)
        subject = SubjectRef("site-fhir", "pat-1", "enc-1")
        req = TaskRequest("r-ord", "revise_orders", "调整医嘱", subject, "demo://grant")
        changes = [
            {
                "action": "add", "code": "NEW-MED", "dose": "250", "unit": "mg",
                "route": "iv", "frequency": "q12h", "reason": "CRP升高",
                "fact_ids": ["Observation/obs-2"], "catalog_ref": "demo://cat/NEW-MED",
            },
            {
                "action": "change", "code": "DEMO-MED", "target_ref": f"{base_url}/MedicationRequest/ord-1",
                "dose": "750", "unit": "mg", "route": "po", "frequency": "bid", "reason": "加量",
                "fact_ids": ["Observation/obs-1"], "catalog_ref": "demo://cat/DEMO-MED",
            },
            {
                "action": "stop", "code": "STOP-MED", "target_ref": f"{base_url}/MedicationRequest/ord-2",
                "reason": "停用", "fact_ids": ["Observation/obs-1"], "catalog_ref": "demo://cat/STOP-MED",
            },
        ]
        prop = Proposal(subject, "revise_orders", {"type": "order_changes", "changes": changes}, ("Observation/obs-1", "Observation/obs-2"), "demo://gen")

        # Draft status maps honestly to saved_draft (NEVER entered)
        fhir_draft = FHIRClinicalSystem(
            base_url, "site-fhir", write_capabilities=("revise_orders",), order_write_status="draft",
        )
        single_add_prop = Proposal(subject, "revise_orders", {"type": "order_changes", "changes": [changes[0]]}, ("Observation/obs-2",), "demo://gen")
        draft_receipt = fhir_draft.apply(single_add_prop, "idem-draft-1", None)
        self.assertEqual(draft_receipt.state, "saved_draft")
        draft_posted = state["writes"][-1][2]
        self.assertEqual(draft_posted["medicationCodeableConcept"]["coding"][0]["code"], "NEW-MED")
        self.assertNotIn("extension", draft_posted)
        draft_rb = fhir_draft.read_back(
            draft_receipt, Task("t-d", req, "execution_uncertain", proposal=single_add_prop, idempotency_key="idem-draft-1", receipt=draft_receipt),
        )
        self.assertEqual(draft_rb.state, "saved_draft")
        self.assertEqual(draft_rb.proposal_digest, single_add_prop.digest)

        # Active multi-item batch (add + change + stop) writes standard R4 with If-Match and reconciles
        state["writes"].clear()
        fhir_active = FHIRClinicalSystem(
            base_url, "site-fhir", write_capabilities=("revise_orders",), order_write_status="active",
        )
        receipt = fhir_active.apply(prop, "idem-batch-1", None)
        self.assertEqual(receipt.state, "entered")
        self.assertEqual(len(receipt.trace_refs), 3)
        self.assertEqual(state["writes"][0][0], "POST")
        self.assertEqual((state["writes"][1][0], state["writes"][1][3]), ("PUT", 'W/"3"'))
        self.assertEqual((state["writes"][2][0], state["writes"][2][3], state["writes"][2][2]["status"]), ("PUT", 'W/"4"', "stopped"))

        task_obj = Task("t-batch", req, "execution_uncertain", proposal=prop, idempotency_key="idem-batch-1", receipt=receipt)
        found = fhir_active.find_submission(task_obj)
        self.assertIsNotNone(found)
        rb = fhir_active.read_back(found, task_obj)
        self.assertEqual(rb.state, "entered")
        self.assertEqual(rb.proposal_digest, prop.digest)
        self.assertEqual(rb.visible_payload, prop.payload)
