"""Compose a site-owned connector with the installed MAP task runtime."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import ModuleType

from med_autopractice.adapters.composite import _DEPENDENCY_SCOPE_HINTS
from med_autopractice.adapters.ports import SiteProvider
from med_autopractice.agent.task_engine import TaskEngine
from med_autopractice.agent.task_store import SqliteTaskStore
from med_autopractice.domain.task import Proposal, Snapshot, SourceCoverage, TaskPolicy
from med_autopractice.medicine.proposals import NOTES, ORDERS, make_note, make_orders, text
from med_autopractice.platform.security import verify_private_directory, verify_private_file
from med_autopractice.runtime import TaskRuntime
from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS
from med_autopractice.workflows import results
from med_autopractice.workflows.site_plan import SiteCatalog

ROOT = Path(__file__).resolve().parents[2]


class SiteKnowledge:
    def __init__(self, rules):
        self.rules = rules

    def propose(self, request, snapshot):
        capability = request.capability_id
        rule = self.rules.get(capability, {})
        if capability in NOTES:
            body = make_note(request.inputs, snapshot.facts, rule, request.authority_ref)
        elif capability in ORDERS or capability == "maintain_order_sets":
            body = make_orders(request.inputs, snapshot.facts, rule, group=capability == "maintain_order_sets")
        elif capability == "collect_history":
            body = results.history(request, snapshot)
        elif capability in {"audit_discharge_records", "track_document_deadlines"}:
            body = results.document_work(request, snapshot, rule)
        elif capability == "build_disease_cohort":
            body = results.cohort(snapshot, rule)
        else:
            body = results.followup(request, snapshot, rule)
        rule_ref = rule.get("approved_ref")
        return Proposal(
            request.subject, capability, body, tuple(f.fact_id for f in snapshot.facts),
            "map://site-workflow/v1", (rule_ref,) if rule_ref else (),
        )


class QualifiedSystem:
    """Gate the write/readback pair on the private dependency catalog."""

    def __init__(self, provider: SiteProvider, catalog: SiteCatalog, *, read_mode: str = "strict"):
        if read_mode not in {"strict", "partial_draft_allowed"}:
            raise ValueError(f"unsupported read_mode: {read_mode}")
        self.provider = provider
        self.catalog = catalog
        self.read_mode = read_mode

    def read_context(self, subject, capability_id):
        workflow = LOCAL_WORKFLOWS[capability_id]
        non_read_dependencies = list(workflow.rule_dependencies)
        if workflow.medical_proposal:
            non_read_dependencies.append("clinical_proposal")
        if non_read_dependencies and not self.catalog.qualified(*non_read_dependencies):
            raise PermissionError("deployment has unresolved read or rule dependencies")
        unqualified = [dep for dep in workflow.read_dependencies if not self.catalog.qualified(dep)]
        if unqualified and self.read_mode == "strict":
            raise PermissionError("deployment has unresolved read or rule dependencies")
        snapshot = self.provider.read_context(subject, capability_id)
        if not unqualified:
            return snapshot
        blocked_scopes = {_DEPENDENCY_SCOPE_HINTS.get(dep, dep) for dep in unqualified}
        updated_coverage = []
        seen_scopes: set[str] = set()
        for item in snapshot.coverage:
            seen_scopes.add(item.scope)
            if item.complete and item.scope in blocked_scopes:
                updated_coverage.append(
                    SourceCoverage(item.source_system, item.scope, False, (*item.gaps, "unqualified_dependency"))
                )
            else:
                updated_coverage.append(item)
        for dep in unqualified:
            scope = _DEPENDENCY_SCOPE_HINTS.get(dep, dep)
            if scope not in seen_scopes:
                updated_coverage.append(
                    SourceCoverage("site-readiness", scope, False, (f"unqualified:{dep}",))
                )
                seen_scopes.add(scope)
        suffix = sha256(",".join(sorted(unqualified)).encode("utf-8")).hexdigest()[:8]
        return Snapshot(
            snapshot.subject,
            f"{snapshot.revision}:partial:{suffix}",
            snapshot.facts,
            snapshot.trace_refs,
            tuple(updated_coverage),
        )

    def can_apply(self, proposal):
        workflow = LOCAL_WORKFLOWS[proposal.capability_id]
        return bool(workflow.write_dependency and self.catalog.qualified(
            workflow.write_dependency, workflow.readback_dependency,
        ) and self.provider.can_apply(proposal) is True)

    def apply_bound(self, task, approval, existing_object_ref):
        if approval is None or not self.can_apply(task.proposal):
            raise PermissionError("deployment has unresolved write dependencies")
        return self.provider.apply_bound(
            task.effective_request, task.proposal, task.snapshot.revision, task.idempotency_key,
            existing_object_ref, approval,
        )

    def read_back(self, receipt, task):
        return self.provider.read_back(receipt, task)

    def find_submission(self, task):
        return self.provider.find_submission(task)


def _private_directory(path: Path) -> Path:
    return verify_private_directory(path, ROOT)


def _private_file(path: Path) -> bytes:
    return verify_private_file(path)


def build_site_runtime(directory: Path) -> TaskRuntime:
    """Load a trusted provider from a user-selected deployment, never from request data."""
    directory = _private_directory(directory)
    config_bytes = _private_file(directory / "deployment.json")
    profile_bytes = _private_file(directory / "dependencies.json")
    provider_path = directory / "provider.py"
    provider_bytes = _private_file(provider_path)
    config = json.loads(config_bytes)
    if not isinstance(config, dict) or config.get("schema_version") != "map-local-deployment.v1":
        raise ValueError("unsupported local deployment descriptor")
    site_id = text(config.get("site_id"), "site ID")
    catalog = SiteCatalog(json.loads(profile_bytes))
    if catalog.profile_id != site_id:
        raise ValueError("site ID differs from the dependency catalog")
    capabilities = config.get("capabilities")
    rules = config.get("rules", {})
    if not isinstance(capabilities, dict) or not capabilities or not isinstance(rules, dict):
        raise ValueError("deployment needs explicit capabilities and rules")
    revision = sha256(b"\x00".join((config_bytes, profile_bytes, provider_bytes))).hexdigest()
    policies = {}
    for capability, state in capabilities.items():
        workflow = LOCAL_WORKFLOWS.get(capability)
        if workflow is None:
            raise ValueError("unknown deployment capability")
        if workflow.rule_dependencies:
            rule = rules.get(capability)
            if not isinstance(rule, dict) or not rule.get("approved_ref"):
                raise ValueError(f"{capability} requires approved site rules")
            text(rule["approved_ref"], "approved rule reference")
        if workflow.write_dependency:
            expected = text(state, "expected system state")
            if capability in NOTES and expected != "saved_draft":
                raise ValueError("note workflows may only save drafts")
            policy = workflow.policy(expected)
        else:
            if state is not None:
                raise ValueError("read-only capabilities cannot configure a write state")
            policy = TaskPolicy(capability, False, None, workflow.required_scopes)
        if workflow.write_dependency:
            policy = replace(policy, requires_pre_submit_review=True)
        policies[capability] = replace(policy, deployment_revision=revision)
    module_name = f"_map_site_provider_{revision}"
    module = ModuleType(module_name)
    module.__file__ = str(provider_path)
    sys.modules[module_name] = module
    try:
        exec(compile(provider_bytes, str(provider_path), "exec"), module.__dict__)
        factory = getattr(module, "build_provider", None)
        if not callable(factory):
            raise ValueError("deployment provider needs build_provider(directory)")
        provider = factory(directory)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    for method in ("authorize", "read_context", "can_apply", "apply_bound", "read_back", "find_submission"):
        if not callable(getattr(provider, method, None)):
            raise ValueError(f"deployment provider must implement {method}")
    read_mode = config.get("read_mode", "strict")
    system = QualifiedSystem(provider, catalog, read_mode=read_mode)
    store = SqliteTaskStore(directory / "tasks.sqlite", site_id=site_id)
    engine = TaskEngine(
        SiteKnowledge(rules), system, None, policies, store,
        authorize=provider.authorize, apply_bound=system.apply_bound,
    )
    return TaskRuntime(engine, LOCAL_WORKFLOWS)
