"""Adapter-independent task lifecycle for the Codex primary Skill."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from med_autopractice.adapters.ports import AuditSink, ClinicalSystem, TaskStore
from med_autopractice.agent.task_store import InMemoryTaskStore
from med_autopractice.domain.task import (
    AuditEvent, ClinicianApproval, Proposal, ReviewDecision, Task, TaskPolicy,
    TaskRequest,
)
from med_autopractice.medicine.ports import ClinicalKnowledge


class JsonlAuditSink:
    """Store metadata/refs only; callers choose an approved private location."""

    def __init__(self, path: Path):
        self.path = path

    def append(self, event: AuditEvent) -> None:
        data = json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd >= 0:
                os.close(fd)


class TaskEngine:
    def __init__(
        self,
        knowledge: ClinicalKnowledge,
        system: ClinicalSystem,
        audit: AuditSink,
        policies: dict[str, TaskPolicy],
        store: TaskStore | None = None,
    ):
        self.knowledge = knowledge
        self.system = system
        self.audit = audit
        self.policies = policies
        self.store = store if store is not None else InMemoryTaskStore()

    def get(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _event(
        self, task: Task, action: str, outcome: str,
        refs: tuple[str, ...] = (), screenshot_ref: str | None = None,
        resource_version_ref: str | None = None,
    ) -> None:
        event = AuditEvent(
            task_id=task.task_id, sequence=len(task.events) + 1,
            action=action, outcome=outcome,
            at_utc=datetime.now(timezone.utc).isoformat(),
            evidence_refs=refs, screenshot_ref=screenshot_ref, resource_version_ref=resource_version_ref,
        )
        self.audit.append(event)
        task.events.append(event)
        self.store.save(task)

    @staticmethod
    def _check_proposal(task: Task, proposal: Proposal) -> None:
        if task.snapshot is None or proposal.subject != task.request.subject:
            raise ValueError("proposal subject does not match the task")
        if proposal.capability_id != task.request.capability_id:
            raise ValueError("proposal capability does not match the task")
        fact_ids = {fact.fact_id for fact in task.snapshot.facts}
        if not set(proposal.cited_fact_ids) <= fact_ids:
            raise ValueError("proposal cites facts outside the observed snapshot")
        if not proposal.payload:
            raise ValueError("proposal payload is empty")
        if proposal.schema_version != "map-proposal.v1" or not proposal.generator_ref:
            raise ValueError("proposal needs a supported schema and generator reference")
        proposal.digest  # Reject payloads that cannot be bound to exact bytes.

    def start(self, request: TaskRequest, *, auto_submit: bool = False) -> Task:
        if not request.request_id or not request.authority_ref:
            raise ValueError("request and authority references are required")
        if request.capability_id not in self.policies:
            raise ValueError("capability has no execution policy")
        self.store.validate_request(request)
        previous = self.store.get_by_request(request.request_id)
        if previous is not None:
            if previous.request != request:
                raise ValueError("request_id already refers to a different task")
            return previous

        task = Task(task_id=uuid4().hex, request=request, policy=self.policies[request.capability_id])
        self._event(task, "request", "accepted", (request.authority_ref,))
        try:
            snapshot = self.system.read_context(request.subject, request.capability_id)
            if snapshot.subject != request.subject or not snapshot.revision or not snapshot.trace_refs:
                raise ValueError("system returned an unbound patient/encounter snapshot")
            if len({fact.fact_id for fact in snapshot.facts}) != len(snapshot.facts):
                raise ValueError("system returned duplicate fact IDs")
            if any(not fact.fact_id or not fact.source_ref for fact in snapshot.facts):
                raise ValueError("system returned facts without source references")
            task.snapshot = snapshot
            self._event(task, "read_context", "observed", snapshot.trace_refs)
            proposal = self.knowledge.propose(request, snapshot)
            self._check_proposal(task, proposal)
            task.proposal = proposal
            task.prepared_digest = proposal.digest
            policy = self.policies[request.capability_id]
            covered = {item.scope for item in snapshot.coverage if item.complete}
            if not set(policy.required_scopes) <= covered:
                task.status = "needs_information"
            elif not self.system.can_apply(proposal):
                task.status = "draft_only"
            else:
                task.status = "needs_pre_submit_review" if policy.requires_pre_submit_review else "ready_to_submit"
            self._event(task, "prepare_proposal", task.status, (proposal.digest,))
        except Exception:
            task.status = "blocked"
            self._event(task, "prepare", "failed")
            raise

        if auto_submit and task.status == "ready_to_submit":
            return self.submit(task.task_id)
        return task

    def submit(self, task_id: str, approval: ClinicianApproval | None = None) -> Task:
        task = self.get(task_id)
        if task.status not in {"ready_to_submit", "needs_pre_submit_review"}:
            self._event(task, "system_submit", "rejected_state")
            raise ValueError("task is not ready to submit")
        if self.policies.get(task.request.capability_id) != task.policy:
            task.status = "needs_policy_review"
            self._event(task, "pre_submit_check", "policy_changed")
            return task
        assert task.snapshot is not None and task.proposal is not None
        if task.proposal.digest != task.prepared_digest:
            self._event(task, "system_submit", "proposal_changed")
            raise ValueError("proposal changed after preparation")
        policy = task.policy
        if policy.requires_pre_submit_review and (
            approval is None or approval.task_id != task_id
            or not approval.clinician_id
            or approval.proposal_digest != task.proposal.digest
            or approval.facts_revision != task.snapshot.revision
        ):
            self._event(task, "clinician_authorization", "rejected")
            raise ValueError("exact clinician approval is required")
        try:
            current = self.system.read_context(task.request.subject, task.request.capability_id)
        except Exception:
            self._event(task, "pre_submit_refresh", "failed")
            raise
        if not current.trace_refs:
            self._event(task, "pre_submit_refresh", "missing_trace")
            raise ValueError("system refresh has no operation trace")
        self._event(task, "pre_submit_refresh", "observed", current.trace_refs)
        covered = {item.scope for item in current.coverage if item.complete}
        if not set(policy.required_scopes) <= covered:
            task.status = "needs_information"
            self._event(task, "pre_submit_check", "incomplete_sources")
            return task
        if current.subject != task.request.subject or current.revision != task.snapshot.revision:
            task.status = "needs_refresh"
            self._event(task, "pre_submit_check", "stale_facts")
            return task
        if not self.system.can_apply(task.proposal):
            task.status = "draft_only"
            self._event(task, "pre_submit_check", "write_unqualified")
            return task
        if approval is not None:
            self._event(
                task, "clinician_authorization", "approved",
                (approval.clinician_id, approval.proposal_digest, approval.facts_revision),
            )

        prior_ref = task.receipt.object_ref if task.receipt else None
        key = f"{task.request.request_id}:{task.proposal.digest}:{task.snapshot.revision}"
        task.idempotency_key = key
        task.status = "execution_uncertain"
        self._event(task, "system_submit", "attempted", (key,))
        try:
            receipt = self.system.apply(task.proposal, key, prior_ref)
            task.receipt = receipt
            if not receipt.object_ref or not receipt.trace_refs:
                raise ValueError("system submission has no object or operation trace")
            self._event(task, "system_submit", receipt.state, receipt.trace_refs)
            readback = self.system.read_back(receipt, task)
            task.readback = readback
            valid = self._readback_matches(task, receipt, readback, policy)
            next_status = "needs_clinician_acceptance" if valid else "execution_uncertain"
            task.status = next_status
            self._event(task, "system_readback", next_status, readback.trace_refs,
                        readback.screenshot_ref, readback.resource_version_ref)
        except Exception:
            task.status = "execution_uncertain"
            self._event(task, "system_execution", "uncertain")
            raise
        return task

    @staticmethod
    def _readback_matches(task: Task, receipt, readback, policy: TaskPolicy) -> bool:
        return (
            task.proposal is not None
            and readback.subject == task.request.subject
            and readback.object_ref == receipt.object_ref
            and readback.proposal_digest == task.proposal.digest
            and readback.visible_payload == task.proposal.payload
            and readback.state == policy.expected_system_state
            and bool(readback.screenshot_ref or (readback.resource_version_ref and readback.observed_at))
            and bool(readback.trace_refs)
        )

    def reconcile(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status != "execution_uncertain" or not task.idempotency_key:
            raise ValueError("task has no uncertain submission to reconcile")
        try:
            receipt = self.system.find_submission(task)
        except Exception:
            self._event(task, "reconcile", "lookup_failed")
            raise
        if receipt is None:
            self._event(task, "reconcile", "not_found")
            return task
        if not receipt.object_ref or not receipt.trace_refs:
            self._event(task, "reconcile", "invalid_receipt")
            return task
        task.receipt = receipt
        self._event(task, "reconcile", "found", receipt.trace_refs)
        try:
            readback = self.system.read_back(receipt, task)
        except Exception:
            self._event(task, "reconcile_readback", "failed")
            raise
        task.readback = readback
        policy = task.policy
        task.status = (
            "needs_clinician_acceptance"
            if self._readback_matches(task, receipt, readback, policy)
            else "execution_uncertain"
        )
        self._event(task, "reconcile_readback", task.status, readback.trace_refs,
                    readback.screenshot_ref, readback.resource_version_ref)
        return task

    def review(self, task_id: str, clinician_id: str, *, accepted: bool, feedback: str = "") -> Task:
        task = self.get(task_id)
        if task.status != "needs_clinician_acceptance" or not clinician_id:
            self._event(task, "clinician_review", "rejected_state")
            raise ValueError("verified system readback and clinician identity are required")
        if (
            task.proposal is None or task.readback is None or task.receipt is None
            or task.prepared_digest != task.proposal.digest
            or not self._readback_matches(task, task.receipt, task.readback, task.policy)
        ):
            self._event(task, "clinician_review", "stale_evidence")
            raise ValueError("proposal or system evidence changed before review")
        if not accepted and not feedback.strip():
            self._event(task, "clinician_review", "missing_feedback")
            raise ValueError("correction feedback is required")
        next_status = "accepted" if accepted else "changes_requested"
        previous_status = task.status
        task.status = next_status
        task.review_feedback = None if accepted else feedback
        task.reviews.append(ReviewDecision(
            clinician_id=clinician_id, accepted=accepted,
            proposal_digest=task.prepared_digest,
            screenshot_ref=task.readback.screenshot_ref,
            resource_version_ref=task.readback.resource_version_ref,
            feedback=feedback, at_utc=datetime.now(timezone.utc).isoformat(),
        ))
        refs = (clinician_id,) if accepted else (clinician_id, sha256(feedback.encode()).hexdigest())
        try:
            self._event(task, "clinician_review", next_status, refs)
        except Exception:
            task.reviews.pop()
            task.status = previous_status
            task.review_feedback = None
            raise
        return task

    def revise(self, task_id: str, proposal: Proposal) -> Task:
        task = self.get(task_id)
        if task.status != "changes_requested" or task.proposal is None:
            self._event(task, "revise_proposal", "rejected_state")
            raise ValueError("task has no requested correction")
        if self.policies.get(task.request.capability_id) != task.policy:
            task.status = "needs_policy_review"
            self._event(task, "revise_proposal", "policy_changed")
            return task
        try:
            self._check_proposal(task, proposal)
        except ValueError:
            self._event(task, "revise_proposal", "invalid_proposal")
            raise
        if proposal.digest == task.proposal.digest:
            self._event(task, "revise_proposal", "unchanged")
            raise ValueError("correction must change the proposal")
        policy = task.policy
        next_status = "needs_pre_submit_review" if policy.requires_pre_submit_review else "ready_to_submit"
        previous = (task.proposal, task.prepared_digest, task.readback, task.review_feedback, task.status)
        task.proposal = proposal
        task.prepared_digest = proposal.digest
        task.readback = None
        task.review_feedback = None
        task.status = next_status
        try:
            self._event(task, "revise_proposal", next_status, (proposal.digest,))
        except Exception:
            task.proposal, task.prepared_digest, task.readback, task.review_feedback, task.status = previous
            raise
        return task
