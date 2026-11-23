"""Create a private synthetic deployment and request examples outside Git."""

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS


def initialize(directory: Path):
    directory = directory.expanduser().resolve()
    if directory.is_relative_to(ROOT) or any((p / ".git").exists() for p in directory.parents):
        raise ValueError("choose a new directory outside Git")
    directory.mkdir(mode=0o700)
    for file, source in (
        ("provider.py", ROOT / "examples/synthetic/local_provider.py"),
        ("history.json", ROOT / "tests/fixtures/synthetic-history-collection.json"),
    ):
        shutil.copyfile(source, directory / file)
    profile = json.loads((ROOT / "contracts/site-dependencies.json").read_text())
    profile["profile_id"] = "demo-local"
    for dependency in profile["dependencies"]:
        dependency.update(
            state="qualified", evidence_ref="demo://evidence/" + dependency["id"],
            qualification_ref="demo://qualification/" + dependency["id"],
        )
    rules = {}
    capabilities = {}
    requests = []
    for key, workflow in LOCAL_WORKFLOWS.items():
        inputs = {}
        rules[key] = {"approved_ref": f"demo://rules/{key}/1"}
        if key in {"write_inpatient_notes", "write_outpatient_notes"}:
            rules[key]["sections"] = ["观察", "评估"]
            state = "saved_draft"
        else:
            state = "recorded" if workflow.write_dependency else None
        if key in {"prepare_admission_orders", "revise_orders", "prepare_discharge_medication", "maintain_order_sets"}:
            inputs["changes"] = [{
                "action": "add", "code": "DEMO-RX", "dose": "演练", "unit": "演练",
                "route": "演练", "frequency": "演练", "fact_ids": ["observation"], "reason": "演练计划",
            }]
        if key == "maintain_order_sets":
            inputs.update(name="演练组套", version="1", effective_on="2026-01-01")
        if key in {"track_document_deadlines", "audit_discharge_records"}:
            rules[key].update(required_documents=["演练记录", "演练签收"], deadline_days={"演练记录": 1})
            inputs["as_of"] = "2026-01-03"
        if key == "build_disease_cohort":
            rules[key]["include"] = {"program": "demo"}
        if key == "manage_followup":
            rules[key]["intervals"] = {"演练复查": 1}
            inputs["as_of"] = "2026-01-03"
        capabilities[key] = state
        requests.append({
            "request_id": f"demo-{key}", "capability_id": key, "intent": "合成本地演练",
            "subject": {"site_id": "demo-local", "patient_id": "synthetic-p01", "encounter_id": "synthetic-e01"},
            "authority_ref": "demo://grant", "inputs": inputs,
        })
    documents = {
        "dependencies.json": profile,
        "deployment.json": {"schema_version": "map-local-deployment.v1", "site_id": "demo-local",
                            "capabilities": capabilities, "rules": rules},
        "batch.json": [row for row in requests if capabilities[row["capability_id"]] is None],
        **{f"{row['capability_id']}.json": row for row in requests},
    }
    for name, content in documents.items():
        (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    initialize(args.directory)
