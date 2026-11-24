"""Operation-level hybrid transport router across clinical adapters."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json

from med_autopractice.adapters.ports import ClinicalSystem
from med_autopractice.domain.task import (
    ExecutionReceipt, Fact, Proposal, Snapshot, SourceCoverage, SubjectRef,
    SystemReadback, Task,
)
from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS
from med_autopractice.workflows.site_plan import SiteCatalog

_DEPENDENCY_SCOPE_HINTS: dict[str, str] = {
    "bind_inpatient": "patient_context",
    "bind_outpatient": "patient_context",
    "read_inpatient_records": "patient_context",
    "read_outpatient_records": "current_encounter",
    "read_current_orders": "current_orders",
    "read_allergies": "allergies",
    "read_nursing_records": "nursing",
    "read_lab_results": "results",
    "read_imaging_reports": "results",
    "read_discharge_records": "discharge_record",
    "read_discharge_plan": "discharge_plan",
    "read_document_status": "document_status",
    "read_order_sets": "current_order_set",
    "read_local_catalog": "local_catalog",
    "read_cohort_facts": "cohort_facts",
    "read_followup_events": "followup_events",
}


class CompositeClinicalSystem:
    """Route each workflow dependency to its configured transport adapter."""

    def __init__(
        self,
        catalog: SiteCatalog,
        adapters_by_transport: Mapping[str, ClinicalSystem] | None = None,
        *,
        adapters_by_dependency: Mapping[str, ClinicalSystem] | None = None,
        partial_read_allowed: bool = False,
    ):
        self.catalog = catalog
        self.adapters_by_transport = dict(adapters_by_transport or {})
        self.adapters_by_dependency = dict(adapters_by_dependency or {})
        self.partial_read_allowed = partial_read_allowed

    def _adapter_for(self, dependency_id: str) -> ClinicalSystem | None:
        if dependency_id in self.adapters_by_dependency:
            return self.adapters_by_dependency[dependency_id]
        dependency = self.catalog.dependencies.get(dependency_id)
        if dependency is None or dependency.transport is None:
            return None
        return self.adapters_by_transport.get(dependency.transport)

    def read_context(self, subject: SubjectRef, capability_id: str) -> Snapshot:
        workflow = LOCAL_WORKFLOWS.get(capability_id)
        if workflow is None:
            raise ValueError(f"unknown capability: {capability_id}")

        facts_by_id: dict[str, Fact] = {}
        ordered_facts: list[Fact] = []
        trace_refs: list[str] = []
        coverages: list[SourceCoverage] = []
        revisions: list[str] = []
        blocked_scopes: set[str] = set()
        seen_adapters: dict[int, Snapshot] = {}

        for dependency_id in workflow.read_dependencies:
            dependency = self.catalog.dependencies.get(dependency_id)
            adapter = self._adapter_for(dependency_id)
            ready = dependency is not None and dependency.state == "qualified" and adapter is not None
            if not ready:
                if not self.partial_read_allowed:
                    raise PermissionError(f"read dependency is unqualified or unconfigured: {dependency_id}")
                scope = _DEPENDENCY_SCOPE_HINTS.get(dependency_id, dependency_id)
                blocked_scopes.add(scope)
                system_name = (dependency.transport or "unconfigured") if dependency else "unconfigured"
                coverages.append(SourceCoverage(system_name, scope, False, (f"unqualified:{dependency_id}",)))
                continue

            adapter_key = id(adapter)
            if adapter_key not in seen_adapters:
                snap = adapter.read_context(subject, capability_id)
                if snap.subject != subject:
                    raise ValueError("composite adapter returned snapshot for a different subject")
                seen_adapters[adapter_key] = snap
                revisions.append(f"{dependency.transport or dependency_id}:{snap.revision}")
                for ref in snap.trace_refs:
                    if ref not in trace_refs:
                        trace_refs.append(ref)
                for fact in snap.facts:
                    existing = facts_by_id.get(fact.fact_id)
                    if existing is None:
                        facts_by_id[fact.fact_id] = fact
                        ordered_facts.append(fact)
                    elif existing != fact:
                        raise ValueError(f"conflicting duplicate fact ID across adapters: {fact.fact_id}")
                for item in snap.coverage:
                    coverages.append(item)

        normalized_coverage: list[SourceCoverage] = []
        for item in coverages:
            if item.complete and item.scope in blocked_scopes:
                normalized_coverage.append(
                    SourceCoverage(item.source_system, item.scope, False, (*item.gaps, "unqualified_dependency"))
                )
            elif item not in normalized_coverage:
                normalized_coverage.append(item)

        digest_input = json.dumps(
            {"subject": vars(subject), "capability": capability_id, "revisions": sorted(revisions),
             "blocked": sorted(blocked_scopes)},
            sort_keys=True, ensure_ascii=False,
        )
        revision = "composite:" + sha256(digest_input.encode("utf-8")).hexdigest()[:16]
        if not trace_refs and self.partial_read_allowed:
            trace_refs.append("composite://partial-read")
        return Snapshot(
            subject=subject,
            revision=revision,
            facts=tuple(ordered_facts),
            trace_refs=tuple(trace_refs),
            coverage=tuple(normalized_coverage),
        )

    def _write_and_readback_adapters(self, capability_id: str) -> tuple[ClinicalSystem, ClinicalSystem]:
        workflow = LOCAL_WORKFLOWS.get(capability_id)
        if workflow is None or not workflow.write_dependency or not workflow.readback_dependency:
            raise PermissionError(f"capability is not writable: {capability_id}")
        if not self.catalog.qualified(workflow.write_dependency, workflow.readback_dependency):
            raise PermissionError("write or readback dependency is not qualified")
        writer = self._adapter_for(workflow.write_dependency)
        reader = self._adapter_for(workflow.readback_dependency) or writer
        if writer is None or reader is None:
            raise PermissionError("missing write or readback transport adapter")
        return writer, reader

    def can_apply(self, proposal: Proposal) -> bool:
        workflow = LOCAL_WORKFLOWS.get(proposal.capability_id)
        if workflow is None or not workflow.write_dependency or not workflow.readback_dependency:
            return False
        if not self.catalog.qualified(workflow.write_dependency, workflow.readback_dependency):
            return False
        writer = self._adapter_for(workflow.write_dependency)
        return writer is not None and bool(writer.can_apply(proposal))

    def apply(
        self, proposal: Proposal, idempotency_key: str, existing_object_ref: str | None
    ) -> ExecutionReceipt:
        writer, _reader = self._write_and_readback_adapters(proposal.capability_id)
        if not writer.can_apply(proposal):
            raise PermissionError("target write adapter rejected proposal")
        return writer.apply(proposal, idempotency_key, existing_object_ref)

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback:
        _writer, reader = self._write_and_readback_adapters(task.request.capability_id)
        return reader.read_back(receipt, task)

    def find_submission(self, task: Task) -> ExecutionReceipt | None:
        workflow = LOCAL_WORKFLOWS.get(task.request.capability_id)
        if workflow is None or not workflow.write_dependency:
            return None
        reader = self._adapter_for(workflow.readback_dependency or "") or self._adapter_for(workflow.write_dependency)
        if reader is None:
            return None
        return reader.find_submission(task)
