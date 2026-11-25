"""Adapter-independent task lifecycle for the Codex primary Skill."""

from __future__ import annotations

from collections.abc import Callable
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
    AuditEvent, ClinicianApproval, ExecutionReceipt, Proposal, ReviewDecision, Snapshot, Task, TaskPolicy,
    TaskRequest, WorkflowInputRequired,
)
from med_autopractice.medicine.ports import ClinicalKnowledge

try:
    from opl_framework.controlled_apply import (
        append_jsonl_audit_record as _framework_append_jsonl_audit_record,
        evaluate_pre_submit_refresh as _framework_evaluate_pre_submit_refresh,
        make_idempotency_key as _framework_make_idempotency_key,
        verify_approval_binding as _framework_verify_approval_binding,
        verify_independent_readback as _framework_verify_independent_readback,
    )
    _FRAMEWORK_CONTROLLED_APPLY_AVAILABLE = True
except ImportError:
    _FRAMEWORK_CONTROLLED_APPLY_AVAILABLE = False


class JsonlAuditSink:
    """Store metadata/refs only; callers choose an approved private location."""

    def __init__(self, path: Path):
        self.path = path

    def append(self, event: AuditEvent) -> None:
        if _FRAMEWORK_CONTROLLED_APPLY_AVAILABLE:
            _framework_append_jsonl_audit_record(self.path, asdict(event))
            return
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
        audit: AuditSink | None,
        policies: dict[str, TaskPolicy],
        store: TaskStore | None = None,
        *,
        authorize: Callable[[str, TaskRequest, ClinicianApproval | None], bool] | None = None,
        apply_bound: Callable[[Task, ClinicianApproval | None, str | None], ExecutionReceipt] | None = None,
    ):
        self.knowledge = knowledge
        self.system = system
        self.audit = audit
        self.policies = policies
        self.authorize = authorize
        self.apply_bound = apply_bound
        self.store = store if store is not None else InMemoryTaskStore()

    def _require_access(self, action: str, request: TaskRequest,
                        decision: ClinicianApproval | None = None) -> None:
        if self.authorize is not None and self.authorize(action, request, decision) is not True:
            raise PermissionError("deployment refused the clinical action")

    def get(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        self._require_access("read", task.effective_request)
        return task

    def list_tasks(
        self, *, status: str | None = None, patient_id: str | None = None, limit: int = 50
    ) -> list[Task]:
        tasks = self.store.list_tasks(status=status, patient_id=patient_id, limit=limit)
        if self.authorize is None:
            return tasks
        return [
            task for task in tasks
            if self.authorize("read", task.effective_request, None) is True
        ]

    def cancel(self, task_id: str, reason: str, *, clinician_id: str | None = None) -> Task:
        task = self.get(task_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason is required")
        if task.status in {"execution_uncertain", "accepted", "cancelled"}:
            raise ValueError(f"cannot cancel task in {task.status} state")
        decision = None
        if clinician_id is not None:
            decision = ClinicianApproval(
                task_id,
                clinician_id,
                task.prepared_digest or "cancelled",
                task.snapshot.revision if task.snapshot else "none",
            )
        self._require_access("cancel", task.effective_request, decision)
        previous_status = task.status
        previous_feedback = task.review_feedback
        cleaned_reason = reason.strip()
        task.status = "cancelled"
        task.review_feedback = cleaned_reason
        refs = (sha256(cleaned_reason.encode("utf-8")).hexdigest(),)
        if clinician_id is not None:
            refs = (clinician_id, *refs)
        try:
            self._event(task, "cancel", "cancelled", refs)
        except Exception:
            task.status = previous_status
            task.review_feedback = previous_feedback
            raise
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
        task.events.append(event)
        try:
            self.store.save(task)
        except Exception:
            task.events.pop()
            raise
        if self.audit is not None:
            self.audit.append(event)

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
        self._require_access("read", request)
        previous = self.store.get_by_request(request.request_id)
        if previous is not None:
            if previous.request != request:
                raise ValueError("request_id already refers to a different task")
            return previous

        task = Task(task_id=uuid4().hex, request=request, policy=self.policies[request.capability_id])
        self._event(task, "request", "accepted", (request.authority_ref,))
        return self._prepare(task, auto_submit=auto_submit)

    @staticmethod
    def _check_snapshot(task: Task, snapshot: Snapshot) -> None:
        if snapshot.subject != task.request.subject or not snapshot.revision or not snapshot.trace_refs:
            raise ValueError("system returned an unbound patient/encounter snapshot")
        if len({fact.fact_id for fact in snapshot.facts}) != len(snapshot.facts):
            raise ValueError("system returned duplicate fact IDs")
        if any(not fact.fact_id or not fact.source_ref for fact in snapshot.facts):
            raise ValueError("system returned facts without source references")

    def refresh(self, task_id: str, *, inputs: dict | None = None) -> Task:
        task = self.get(task_id)
        allowed = {"collecting", "blocked", "needs_input", "needs_information", "needs_refresh",
                   "needs_policy_review", "draft_only", "review_result",
                   "needs_pre_submit_review", "ready_to_submit"}
        if task.policy.expected_system_state is None:
            allowed.add("changes_requested")
        if task.status not in allowed:
            raise ValueError("task cannot be refreshed from this state")
        if task.idempotency_key:
            raise ValueError("a submitted task must use reconciliation or a new request")
        current = self.policies.get(task.request.capability_id)
        if current is None:
            raise PermissionError("workflow is unavailable in this deployment")
        task.policy = current
        if inputs is not None:
            task.input_override = inputs
            self._require_access("read", task.effective_request)
        task.required_input = ()
        task.proposal = None
        task.prepared_digest = None
        task.snapshot = None
        task.status = "collecting"
        self._event(task, "refresh", "started")
        return self._prepare(task)

    def _prepare(self, task: Task, *, auto_submit: bool = False) -> Task:
        request = task.effective_request
        policy = task.policy
        try:
            snapshot = self.system.read_context(request.subject, request.capability_id)
            self._check_snapshot(task, snapshot)
            task.snapshot = snapshot
            self._event(task, "read_context", "observed", snapshot.trace_refs)
            covered = {item.scope for item in snapshot.coverage if item.complete}
            missing_scopes = set(policy.required_scopes) - covered
            if missing_scopes and not policy.allow_partial_draft and self.authorize is not None:
                task.status = "needs_information"
                self._event(task, "prepare_proposal", task.status)
                return task
            proposal = self.knowledge.propose(request, snapshot)
            self._check_proposal(task, proposal)
            task.proposal = proposal
            task.prepared_digest = proposal.digest
            if missing_scopes:
                if policy.allow_partial_draft:
                    task.status = "draft_only"
                    self._event(task, "prepare_proposal", "draft_only", (proposal.digest, "partial_sources"))
                else:
                    task.status = "needs_information"
                    self._event(task, "prepare_proposal", task.status, (proposal.digest,))
            elif policy.expected_system_state is None:
                task.status = "review_result"
                self._event(task, "prepare_proposal", task.status, (proposal.digest,))
            elif not self.system.can_apply(proposal):
                task.status = "draft_only"
                self._event(task, "prepare_proposal", task.status, (proposal.digest,))
            else:
                task.status = "needs_pre_submit_review" if policy.requires_pre_submit_review else "ready_to_submit"
                self._event(task, "prepare_proposal", task.status, (proposal.digest,))
        except WorkflowInputRequired as error:
            task.status = "needs_input"
            task.required_input = error.fields
            self._event(task, "prepare_proposal", task.status)
            return task
        except Exception as error:
            task.status = "blocked"
            self._event(task, "prepare", "failed", (type(error).__name__,))
            if self.authorize is not None:
                return task
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
            approval is None
            or not (
                _framework_verify_approval_binding(
                    task_id=approval.task_id,
                    actor_id=approval.clinician_id,
                    proposal_digest=approval.proposal_digest,
                    facts_revision=approval.facts_revision,
                    expected_task_id=task_id,
                    expected_proposal_digest=task.proposal.digest,
                    expected_facts_revision=task.snapshot.revision,
                )
                if _FRAMEWORK_CONTROLLED_APPLY_AVAILABLE
                else (
                    approval.task_id == task_id
                    and bool(approval.clinician_id)
                    and approval.proposal_digest == task.proposal.digest
                    and approval.facts_revision == task.snapshot.revision
                )
            )
        ):
            self._event(task, "clinician_authorization", "rejected")
            raise ValueError("exact clinician approval is required")
        self._require_access("submit", task.effective_request, approval)
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
        if _FRAMEWORK_CONTROLLED_APPLY_AVAILABLE:
            verdict = _framework_evaluate_pre_submit_refresh(
                expected_subject=task.request.subject,
                expected_revision=task.snapshot.revision,
                expected_facts=task.snapshot.facts,
                expected_coverage=task.snapshot.coverage,
                current_subject=current.subject,
                current_revision=current.revision,
                current_facts=current.facts,
                current_coverage=current.coverage,
                required_scopes=policy.required_scopes,
                complete_scopes=covered,
                strict_fact_equality=self.authorize is not None,
                can_apply=lambda: self.system.can_apply(task.proposal),
            )
            if not verdict.ok:
                task.status = verdict.status
                self._event(task, "pre_submit_check", verdict.reason or verdict.status)
                return task
        else:
            if not set(policy.required_scopes) <= covered:
                task.status = "needs_information"
                self._event(task, "pre_submit_check", "incomplete_sources")
                return task
            if current.subject != task.request.subject or current.revision != task.snapshot.revision or (
                self.authorize is not None and (
                    current.facts != task.snapshot.facts or current.coverage != task.snapshot.coverage
                )
            ):
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
        key = (
            _framework_make_idempotency_key(task.request.request_id, task.proposal.digest, task.snapshot.revision)
            if _FRAMEWORK_CONTROLLED_APPLY_AVAILABLE
            else f"{task.request.request_id}:{task.proposal.digest}:{task.snapshot.revision}"
        )
        task.idempotency_key = key
        task.status = "execution_uncertain"
        self._event(task, "system_submit", "attempted", (key,))
        try:
            receipt = (self.apply_bound(task, approval, prior_ref) if self.apply_bound is not None
                       else self.system.apply(task.proposal, key, prior_ref))
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
        if task.proposal is None:
            return False
        if _FRAMEWORK_CONTROLLED_APPLY_AVAILABLE:
            return _framework_verify_independent_readback(
                expected_subject=task.request.subject,
                expected_object_ref=receipt.object_ref,
                expected_proposal_digest=task.proposal.digest,
                expected_payload=task.proposal.payload,
                expected_state=policy.expected_system_state,
                readback_subject=readback.subject,
                readback_object_ref=readback.object_ref,
                readback_proposal_digest=readback.proposal_digest,
                readback_visible_payload=readback.visible_payload,
                readback_state=readback.state,
                readback_screenshot_ref=readback.screenshot_ref,
                readback_resource_version_ref=readback.resource_version_ref,
                readback_observed_at=readback.observed_at,
                readback_trace_refs=readback.trace_refs,
            )
        return (
            task.proposal is not None
            and readback.subject == task.request.subject
            and readback.object_ref == receipt.object_ref
            and readback.proposal_digest == task.proposal.digest
            and json.dumps(readback.visible_payload, ensure_ascii=False, sort_keys=True)
            == json.dumps(task.proposal.payload, ensure_ascii=False, sort_keys=True)
            and readback.state == policy.expected_system_state
            and bool(readback.screenshot_ref or (readback.resource_version_ref and readback.observed_at))
            and bool(readback.trace_refs)
        )

    def reconcile(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status != "execution_uncertain" or not task.idempotency_key:
            raise ValueError("task has no uncertain submission to reconcile")
        self._require_access("reconcile", task.effective_request)
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

    def review(self, task_id: str, clinician_id: str, *, accepted: bool, feedback: str = "",
               digest: str | None = None) -> Task:
        task = self.get(task_id)
        if self.authorize is not None:
            if task.snapshot is None or not digest or digest != task.prepared_digest:
                raise ValueError("review needs the exact result digest")
            self._require_access("review", task.effective_request, ClinicianApproval(
                task_id, clinician_id, digest, task.snapshot.revision,
            ))
        read_only = task.policy.expected_system_state is None
        if task.status not in {"needs_clinician_acceptance", "review_result"} or not clinician_id:
            self._event(task, "clinician_review", "rejected_state")
            raise ValueError("verified system readback and clinician identity are required")
        if (
            task.proposal is None or task.prepared_digest != task.proposal.digest
            or (not read_only and (task.readback is None or task.receipt is None
                                   or not self._readback_matches(task, task.receipt, task.readback, task.policy)))
        ):
            self._event(task, "clinician_review", "stale_evidence")
            raise ValueError("proposal or system evidence changed before review")
        if not accepted and not feedback.strip():
            self._event(task, "clinician_review", "missing_feedback")
            raise ValueError("correction feedback is required")
        if accepted and self.authorize is not None and not read_only:
            latest = self.system.read_back(task.receipt, task)
            if not self._readback_matches(task, task.receipt, latest, task.policy):
                task.status = "execution_uncertain"
                self._event(task, "clinician_review", "target_changed", latest.trace_refs)
                return task
        next_status = "accepted" if accepted else "changes_requested"
        previous_status = task.status
        task.status = next_status
        task.review_feedback = None if accepted else feedback
        task.reviews.append(ReviewDecision(
            clinician_id=clinician_id, accepted=accepted,
            proposal_digest=task.prepared_digest,
            screenshot_ref=task.readback.screenshot_ref if task.readback else None,
            resource_version_ref=task.readback.resource_version_ref if task.readback else None,
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
        if policy.expected_system_state is None:
            raise ValueError("read-only results must be regenerated from their sources")
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
