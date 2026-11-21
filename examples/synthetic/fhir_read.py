"""Read one synthetic result over HTTP from an in-process FHIR R4 test endpoint."""
from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
from threading import Thread

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.adapters.fhir import FHIRObservationSource
from med_autopractice.domain.task import SubjectRef


SUBJECT = SubjectRef("demo-site", "synthetic-patient", "synthetic-encounter")
ENCOUNTER = {
    "resourceType": "Encounter", "id": SUBJECT.encounter_id, "status": "finished",
    "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
    "subject": {"reference": f"Patient/{SUBJECT.patient_id}"},
}
OBSERVATION = {
    "resourceType": "Observation", "id": "synthetic-result", "meta": {"versionId": "3"},
    "subject": ENCOUNTER["subject"], "encounter": {"reference": f"Encounter/{SUBJECT.encounter_id}"},
    "status": "final", "code": {"text": "合成检验项目"},
    "valueQuantity": {"value": 5.1, "unit": "mmol/L"},
    "effectiveDateTime": "2026-01-01T09:00:00Z", "issued": "2026-01-01T09:20:00Z",
}


def run_demo():
    resources = {f"/r4/{r['resourceType']}/{r['id']}": r for r in (ENCOUNTER, OBSERVATION)}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in resources:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/fhir+json")
            self.end_headers()
            self.wfile.write(json.dumps(resources[self.path]).encode())

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            source = FHIRObservationSource(f"http://127.0.0.1:{server.server_port}/r4", SUBJECT.site_id)
            return asdict(source.read_quantity(SUBJECT, OBSERVATION["id"]))
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    print(json.dumps({"mode": "synthetic", "fact": run_demo()}, ensure_ascii=False, indent=2))
