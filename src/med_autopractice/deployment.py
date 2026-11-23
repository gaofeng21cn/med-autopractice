"""Compose a site-owned connector with the installed MAP task runtime."""

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
from types import ModuleType

from med_autopractice.adapters.ports import SiteProvider
from med_autopractice.agent.task_engine import TaskEngine
from med_autopractice.agent.task_store import SqliteTaskStore
from med_autopractice.domain.task import Proposal, TaskPolicy
from med_autopractice.medicine.proposals import NOTES, ORDERS, make_note, make_orders, text
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

    def __init__(self, provider: SiteProvider, catalog: SiteCatalog):
        self.provider = provider
        self.catalog = catalog

    def read_context(self, subject, capability_id):
        workflow = LOCAL_WORKFLOWS[capability_id]
        dependencies = (*workflow.read_dependencies, *workflow.rule_dependencies)
        if workflow.medical_proposal:
            dependencies += ("clinical_proposal",)
        if not self.catalog.qualified(*dependencies):
            raise PermissionError("deployment has unresolved read or rule dependencies")
        return self.provider.read_context(subject, capability_id)

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
    path = path.expanduser().resolve(strict=True)
    if path.is_relative_to(ROOT) or any((p / ".git").exists() for p in (path, *path.parents)):
        raise PermissionError("use a private deployment directory outside Git")
    identity = path.stat()
    if not path.is_dir() or identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) & 0o077:
        raise PermissionError("deployment directory must be private to its owner")
    return path


def _private_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PermissionError("deployment input must be a regular file")
    identity = path.stat()
    if identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) & 0o022:
        raise PermissionError("deployment input must be owned and writable only by its owner")
    return path.read_bytes()


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
    system = QualifiedSystem(provider, catalog)
    store = SqliteTaskStore(directory / "tasks.sqlite", site_id=site_id)
    engine = TaskEngine(
        SiteKnowledge(rules), system, None, policies, store,
        authorize=provider.authorize, apply_bound=system.apply_bound,
    )
    return TaskRuntime(engine, LOCAL_WORKFLOWS)
