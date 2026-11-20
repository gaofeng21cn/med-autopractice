"""Replace the note proposer while using MAP's synthetic system."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.adapters.synthetic import SYNTHETIC_SITE_ID
from med_autopractice.agent.task_engine import JsonlAuditSink
from med_autopractice.agent.task_store import InMemoryTaskStore
from med_autopractice.domain.task import Proposal, Snapshot, SubjectRef, TaskRequest
from med_autopractice.medicine.router import CapabilityKnowledgeRouter
from med_autopractice.runtime import build_synthetic_runtime


class SourceIndexProposer:
    """Build a note outline from the observed synthetic facts."""

    def propose(self, request: TaskRequest, snapshot: Snapshot) -> Proposal:
        return Proposal(
            subject=snapshot.subject,
            capability_id=request.capability_id,
            payload={
                "document_type": "progress_note",
                "draft": "\n".join(f"{fact.source_system}: {fact.value}" for fact in snapshot.facts),
            },
            cited_fact_ids=tuple(fact.fact_id for fact in snapshot.facts),
            generator_ref="demo://proposer/source-index/v1",
        )


def main() -> None:
    capability = "write_inpatient_notes"
    router = CapabilityKnowledgeRouter({capability: SourceIndexProposer()})
    request = TaskRequest(
        "demo-source-index", capability, "示例：按来源列出合成记录",
        SubjectRef(SYNTHETIC_SITE_ID, "synthetic-demo-patient", "synthetic-demo-encounter"),
        "demo://authority/synthetic",
    )
    with TemporaryDirectory(prefix="map-custom-proposer-") as directory:
        runtime, _ = build_synthetic_runtime(
            InMemoryTaskStore(), JsonlAuditSink(Path(directory) / "audit.jsonl"),
            knowledge=router,
        )
        view = runtime.start(request)
    if view["status"] != "ready_to_submit":
        raise SystemExit(f'Expected a synthetic draft, got {view["status"]}')
    print(json.dumps(view["proposal"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
