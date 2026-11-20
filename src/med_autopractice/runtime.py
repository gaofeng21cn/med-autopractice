"""Stable local entry the Codex primary Skill calls.

The runtime composes the medical proposal router, the workflow policy catalog
and the task engine behind one JSON-serializable boundary, so a host never
assembles policies by hand. Clinical facts, write permission and final
acceptance keep their existing owners; this module only routes and projects
task state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict

from med_autopractice.adapters.ports import AuditSink, ClinicalSystem, TaskStore
from med_autopractice.adapters.synthetic import (
    NOTE_CAPABILITIES, SyntheticHospitalUI, SyntheticKnowledge, SyntheticScenario,
    build_synthetic_system,
)
from med_autopractice.agent.task_engine import TaskEngine
from med_autopractice.domain.task import (
    ClinicianApproval, Proposal, Task, TaskPolicy, TaskRequest,
)
from med_autopractice.medicine.ports import ClinicalKnowledge
from med_autopractice.workflows.catalog import PHASE1_WORKFLOWS, WorkflowDefinition


NEXT_ACTION = {
    "blocked": "resolve_error",
    "needs_information": "provide_missing_sources",
    "draft_only": "review_draft_with_clinician",
    "needs_pre_submit_review": "obtain_pre_submit_approval",
    "ready_to_submit": "submit",
    "needs_refresh": "refresh_facts_and_reapprove",
    "needs_policy_review": "review_policy_change",
    "execution_uncertain": "reconcile_visible_object",
    "needs_clinician_acceptance": "obtain_clinician_acceptance",
    "changes_requested": "revise_proposal",
    "accepted": "complete",
}


def build_policies(expected_states: Mapping[str, str]) -> dict[str, TaskPolicy]:
    """Build every writable workflow policy from the catalog in one place."""

    policies: dict[str, TaskPolicy] = {}
    for capability_id, workflow in PHASE1_WORKFLOWS.items():
        if workflow.write_dependency is None:
            continue
        try:
            expected_state = expected_states[capability_id]
        except KeyError as error:
            raise ValueError(f"missing expected system state for {capability_id}") from error
        policies[capability_id] = workflow.policy(expected_state)
    return policies


def build_synthetic_runtime(
    store: TaskStore,
    audit: AuditSink,
    scenario: SyntheticScenario | None = None,
) -> tuple["TaskRuntime", SyntheticHospitalUI]:
    """Compose the synthetic-only runtime used for local acceptance checks."""

    system, ui = build_synthetic_system(scenario)
    expected_states = {
        workflow.capability_id: (
            "saved_draft" if workflow.capability_id in NOTE_CAPABILITIES else "entered"
        )
        for workflow in PHASE1_WORKFLOWS.values() if workflow.write_dependency
    }
    engine = TaskEngine(SyntheticKnowledge(), system, audit,
                        build_policies(expected_states), store)
    return TaskRuntime(engine), ui


class TaskRuntime:
    """Host-facing commands returning a stable, JSON-serializable task view."""

    def __init__(
        self,
        engine: TaskEngine,
        workflows: Mapping[str, WorkflowDefinition] = PHASE1_WORKFLOWS,
    ):
        self.engine = engine
        self.workflows = workflows

    def start(self, request: TaskRequest, *, auto_submit: bool = False) -> dict:
        definition = self.workflows.get(request.capability_id)
        if definition is None:
            raise ValueError(f"unknown capability: {request.capability_id}")
        if definition.write_dependency is None:
            raise ValueError(
                f"{request.capability_id} is a read-only workflow and has no "
                "execution policy yet"
            )
        return self.view(self.engine.start(request, auto_submit=auto_submit))

    def show(self, task_id: str) -> dict:
        return self.view(self.engine.get(task_id))

    def submit(self, task_id: str, approval: ClinicianApproval | None = None) -> dict:
        return self.view(self.engine.submit(task_id, approval))

    def review(self, task_id: str, clinician_id: str, *, accepted: bool,
               feedback: str = "") -> dict:
        return self.view(self.engine.review(task_id, clinician_id, accepted=accepted,
                                            feedback=feedback))

    def revise(self, task_id: str, payload: Mapping, *,
               generator_ref: str | None = None,
               knowledge_refs: Sequence[str] | None = None) -> dict:
        """Bind an externally produced corrected payload to the task.

        The payload must come from the medical component or an explicit
        clinician correction; the runtime only validates and version-binds it.
        """

        task = self.engine.get(task_id)
        if task.snapshot is None:
            raise ValueError("task has no observed snapshot to bind a revision to")
        reference = generator_ref or (
            f"{task.proposal.generator_ref}+revision" if task.proposal
            else "host://clinician-correction"
        )
        if knowledge_refs is None:
            refs = task.proposal.knowledge_refs if task.proposal else ()
        else:
            if (isinstance(knowledge_refs, (str, bytes))
                    or any(not isinstance(ref, str) or not ref.strip()
                           for ref in knowledge_refs)):
                raise ValueError("knowledge references must be non-empty strings")
            refs = tuple(knowledge_refs)
        proposal = Proposal(
            task.request.subject, task.request.capability_id, dict(payload),
            tuple(fact.fact_id for fact in task.snapshot.facts), reference,
            knowledge_refs=refs,
        )
        return self.view(self.engine.revise(task_id, proposal))

    def reconcile(self, task_id: str) -> dict:
        return self.view(self.engine.reconcile(task_id))

    def view(self, task: Task) -> dict:
        definition = self.workflows[task.request.capability_id]
        coverage = task.snapshot.coverage if task.snapshot else ()
        covered = {item.scope for item in coverage if item.complete}
        missing = [scope for scope in definition.required_scopes if scope not in covered]
        return {
            "task_id": task.task_id,
            "capability_id": task.request.capability_id,
            "intent": task.request.intent,
            "status": task.status,
            "next_action": NEXT_ACTION.get(task.status, "inspect_task"),
            "subject": asdict(task.request.subject),
            "authority_ref": task.request.authority_ref,
            "policy": {
                "requires_pre_submit_review": task.policy.requires_pre_submit_review,
                "expected_system_state": task.policy.expected_system_state,
                "required_scopes": list(task.policy.required_scopes),
            },
            "snapshot": None if task.snapshot is None else {
                "revision": task.snapshot.revision,
                "trace_refs": list(task.snapshot.trace_refs),
                "facts": [
                    {
                        "fact_id": fact.fact_id,
                        "value": fact.value,
                        "source_ref": fact.source_ref,
                        "source_system": fact.source_system,
                        "effective_at": fact.effective_at,
                        "recorded_at": fact.recorded_at,
                        "unit": fact.unit,
                        "status": fact.status,
                    }
                    for fact in task.snapshot.facts
                ],
                "coverage": [
                    {
                        "source_system": item.source_system,
                        "scope": item.scope,
                        "complete": item.complete,
                        "gaps": list(item.gaps),
                    }
                    for item in coverage
                ],
            },
            "missing_information": missing,
            "proposal": None if task.proposal is None else {
                "schema_version": task.proposal.schema_version,
                "digest": task.proposal.digest,
                "generator_ref": task.proposal.generator_ref,
                "knowledge_refs": list(task.proposal.knowledge_refs),
                "cited_fact_ids": list(task.proposal.cited_fact_ids),
                "payload": task.proposal.payload,
            },
            "execution": {
                "idempotency_key": task.idempotency_key,
                "receipt": None if task.receipt is None else asdict(task.receipt),
                "readback": None if task.readback is None else {
                    "object_ref": task.readback.object_ref,
                    "state": task.readback.state,
                    "proposal_digest": task.readback.proposal_digest,
                    "screenshot_ref": task.readback.screenshot_ref,
                    "trace_refs": list(task.readback.trace_refs),
                    "observed_at": task.readback.observed_at,
                },
            },
            "review": {
                "feedback": task.review_feedback,
                "decisions": [asdict(decision) for decision in task.reviews],
            },
            "events": [
                {
                    "sequence": event.sequence,
                    "action": event.action,
                    "outcome": event.outcome,
                    "at_utc": event.at_utc,
                }
                for event in task.events
            ],
        }


def build_runtime(
    knowledge: ClinicalKnowledge,
    system: ClinicalSystem,
    audit: AuditSink,
    store: TaskStore,
    expected_states: Mapping[str, str],
) -> TaskRuntime:
    """Compose a runtime for any clinical system implementation."""

    engine = TaskEngine(knowledge, system, audit, build_policies(expected_states), store)
    return TaskRuntime(engine)
