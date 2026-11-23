"""Drive a synthetic task or an explicitly selected local site deployment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.agent.task_engine import JsonlAuditSink
from med_autopractice.agent.task_store import ConcurrentTaskUpdate, SyntheticSqliteTaskStore
from med_autopractice.deployment import build_site_runtime
from med_autopractice.domain.task import ClinicianApproval, SubjectRef, TaskRequest
from med_autopractice.runtime import TaskRuntime, build_synthetic_runtime
from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS


def _private_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _runtime(args: argparse.Namespace) -> TaskRuntime:
    if args.site:
        if args.audit:
            raise ValueError("site audit events are stored with each task")
        return build_site_runtime(args.site)
    store_path = Path(args.store).expanduser()
    _private_directory(store_path)
    audit_path = (Path(args.audit).expanduser() if args.audit
                  else store_path.with_suffix(".audit.jsonl"))
    _private_directory(audit_path)
    runtime, _ui = build_synthetic_runtime(
        SyntheticSqliteTaskStore(store_path), JsonlAuditSink(audit_path),
    )
    return runtime


def _request_from(path: Path) -> TaskRequest:
    return _request(json.loads(path.expanduser().read_text(encoding="utf-8")))


def _request(body: dict) -> TaskRequest:
    if not isinstance(body, dict) or not isinstance(body.get("inputs", {}), dict):
        raise ValueError("request and inputs must be JSON objects")
    subject = SubjectRef(**body["subject"])
    return TaskRequest(
        request_id=body["request_id"],
        capability_id=body["capability_id"],
        intent=body["intent"],
        subject=subject,
        authority_ref=body["authority_ref"],
        inputs=body.get("inputs", {}),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    target = common.add_mutually_exclusive_group(required=True)
    target.add_argument("--store", help="synthetic task store path")
    target.add_argument("--site", type=Path, help="explicit private deployment directory")
    common.add_argument("--audit", help="audit JSONL path; defaults next to the store")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", parents=[common],
                                help="create or resume a task from a request file")
    start.add_argument("--request", required=True, type=Path)
    start.add_argument("--auto-submit", action="store_true")

    show = commands.add_parser("show", parents=[common], help="show one task view")
    show.add_argument("--task-id", required=True)
    refresh = commands.add_parser("refresh", parents=[common],
                                  help="re-read sources and prepare the same unsubmitted task")
    refresh.add_argument("--task-id", required=True)
    batch = commands.add_parser("batch", parents=[common],
                                help="start or resume an explicit list of read-only requests")
    batch.add_argument("--requests", type=Path, required=True)

    submit = commands.add_parser("submit", parents=[common],
                                 help="submit the exact approved proposal")
    submit.add_argument("--task-id", required=True)
    submit.add_argument("--clinician")
    submit.add_argument("--digest", help="exact proposal digest the clinician approved")
    submit.add_argument("--revision", help="exact facts revision the clinician approved")

    review = commands.add_parser("review", parents=[common],
                                 help="record the clinician acceptance or correction")
    review.add_argument("--task-id", required=True)
    review.add_argument("--clinician", required=True)
    review.add_argument("--accept", action="store_true")
    review.add_argument("--digest", help="exact result digest for a site review")
    review.add_argument("--feedback", default="")

    revise = commands.add_parser("revise", parents=[common],
                                 help="bind an externally produced corrected payload")
    revise.add_argument("--task-id", required=True)
    revise.add_argument("--payload", required=True, type=Path)
    revise.add_argument("--generator-ref")
    revise.add_argument("--knowledge-refs", type=Path,
                        help="JSON list of exact reviewed knowledge references; omitted preserves existing references")

    reconcile = commands.add_parser("reconcile", parents=[common],
                                    help="look for an uncertain submission without replay")
    reconcile.add_argument("--task-id", required=True)
    return parser


def run_batch(runtime: TaskRuntime, path: Path) -> dict:
    raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("batch requires a nonempty JSON array")
    requests = [_request(row) for row in raw]
    ids = {request.request_id for request in requests}
    if len(ids) != len(requests):
        raise ValueError("batch request IDs must be unique")
    for request in requests:
        workflow = LOCAL_WORKFLOWS.get(request.capability_id)
        if workflow is None or workflow.write_dependency is not None:
            raise ValueError("batch currently accepts only read-only workflows")
    rows = []
    for request in requests:
        try:
            view = runtime.start(request)
        except (OSError, ValueError, TypeError, KeyError, ConcurrentTaskUpdate):
            rows.append({"request_id": request.request_id, "status": "blocked"})
            continue
        rows.append({key: view[key] for key in ("task_id", "subject", "status", "next_action", "proposal")})
        rows[-1]["request_id"] = request.request_id
    return {"requests": rows}


def main() -> int:
    args = build_parser().parse_args()
    try:
        runtime = _runtime(args)
        if args.command == "start":
            view = runtime.start(_request_from(args.request), auto_submit=args.auto_submit)
        elif args.command == "show":
            view = runtime.show(args.task_id)
        elif args.command == "refresh":
            view = runtime.refresh(args.task_id)
        elif args.command == "batch":
            if not args.site:
                raise ValueError("batch requires a deployment")
            view = run_batch(runtime, args.requests)
        elif args.command == "submit":
            approval = None
            if args.clinician:
                if not args.digest or not args.revision:
                    raise ValueError(
                        "clinician approval needs --digest and --revision from the reviewed proposal"
                    )
                approval = ClinicianApproval(
                    args.task_id, args.clinician, args.digest, args.revision,
                )
            view = runtime.submit(args.task_id, approval)
        elif args.command == "review":
            view = runtime.review(args.task_id, args.clinician,
                                  accepted=args.accept, feedback=args.feedback, digest=args.digest)
        elif args.command == "revise":
            payload = json.loads(args.payload.expanduser().read_text(encoding="utf-8"))
            knowledge_refs = None
            if args.knowledge_refs:
                knowledge_refs = json.loads(args.knowledge_refs.expanduser().read_text(encoding="utf-8"))
                if not isinstance(knowledge_refs, list):
                    raise ValueError("knowledge references file must contain a JSON list")
            if args.site:
                if args.generator_ref or args.knowledge_refs:
                    raise ValueError("site corrections use the workflow's input and rule validation")
                if not isinstance(payload, dict):
                    raise ValueError("correction inputs must be a JSON object")
                view = runtime.revise_inputs(args.task_id, payload)
            else:
                view = runtime.revise(args.task_id, payload, generator_ref=args.generator_ref,
                                      knowledge_refs=knowledge_refs)
        else:
            view = runtime.reconcile(args.task_id)
    except (ValueError, KeyError, OSError, TypeError, ConcurrentTaskUpdate) as error:
        # Provider errors can contain source-system data. Site task events retain the operational outcome.
        message = type(error).__name__ if args.site else str(error)
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(view, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
