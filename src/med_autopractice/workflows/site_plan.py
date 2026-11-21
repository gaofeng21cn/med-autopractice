"""Read-only site readiness and workflow steps; never executes clinical input."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from med_autopractice.workflows.catalog import PHASE1_WORKFLOWS


_OWNERS = {"adapter", "rules", "medicine"}
_STATES = {"unmapped", "observed", "qualified"}


@dataclass(frozen=True)
class Dependency:
    dependency_id: str
    owner: str
    state: str
    evidence_ref: str | None
    qualification_ref: str | None
    transport: str | None = None


@dataclass(frozen=True)
class PlanStep:
    phase: str
    dependency_id: str | None
    owner: str
    state: str
    transport: str | None = None


@dataclass(frozen=True)
class SitePlan:
    capability_id: str
    steps: tuple[PlanStep, ...]
    blockers: tuple[str, ...]
    dependencies_qualified: bool


class SiteCatalog:
    def __init__(self, document: dict):
        if not isinstance(document, dict) or document.get("schema_version") != "map-site-dependencies.v1":
            raise ValueError("unsupported site dependency catalog")
        self.profile_id = document.get("profile_id")
        systems = document.get("systems")
        entries = document.get("dependencies")
        if not isinstance(systems, list) or not isinstance(entries, list):
            raise ValueError("site systems and dependencies must be lists")
        if not all(isinstance(item, dict) for item in (*systems, *entries)):
            raise ValueError("site systems and dependencies must be objects")
        system_ids = [item.get("id") for item in systems]
        if any(not isinstance(value, str) or not value for value in system_ids) or len(system_ids) != len(set(system_ids)):
            raise ValueError("site system IDs must be unique")
        catalog = Path(__file__).resolve().parents[3] / "contracts/capabilities.json"
        transports = json.loads(catalog.read_text(encoding="utf-8"))["site_transports"]
        self.dependencies = {}
        for item in entries:
            dependency_id = item.get("id")
            owner = item.get("owner")
            state = item.get("state")
            system_id = item.get("system_id")
            if (not isinstance(dependency_id, str) or not dependency_id
                    or dependency_id in self.dependencies or owner not in _OWNERS
                    or state not in _STATES
                    or (owner == "adapter" and system_id not in system_ids)
                    or (owner != "adapter" and system_id is not None)
                    or (state != "unmapped" and not item.get("evidence_ref"))
                    or (state == "qualified" and not item.get("qualification_ref"))):
                raise ValueError("invalid or duplicate site dependency")
            transport = item.get("transport")
            if transport is not None and (owner != "adapter" or transport not in transports):
                raise ValueError(f"invalid transport for {dependency_id}")
            self.dependencies[dependency_id] = Dependency(
                dependency_id, owner, state, item.get("evidence_ref"),
                item.get("qualification_ref"), transport,
            )
        required = set()
        for workflow in PHASE1_WORKFLOWS.values():
            required.update(workflow.read_dependencies)
            required.update(workflow.rule_dependencies)
            if workflow.medical_proposal:
                required.add("clinical_proposal")
            if workflow.write_dependency:
                required.add(workflow.write_dependency)
                required.add(workflow.readback_dependency)
        if required - self.dependencies.keys():
            raise ValueError("workflow dependency is absent from the site catalog")

    @classmethod
    def load(cls, path: Path) -> SiteCatalog:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def plan(self, capability_id: str) -> SitePlan:
        workflow = PHASE1_WORKFLOWS[capability_id]
        steps = []
        blockers = []

        def add(phase: str, dependency_id: str):
            dependency = self.dependencies[dependency_id]
            steps.append(PlanStep(phase, dependency_id, dependency.owner, dependency.state, dependency.transport))
            if dependency.state != "qualified":
                blockers.append(dependency_id)

        for dependency_id in workflow.read_dependencies:
            add("collect", dependency_id)
        for dependency_id in workflow.rule_dependencies:
            add("load_rules", dependency_id)
        if workflow.medical_proposal:
            add("medical_proposal", "clinical_proposal")
        if workflow.requires_pre_submit_review:
            steps.append(PlanStep("clinician_approval", None, "clinician", "required"))
        if workflow.write_dependency:
            add("apply", workflow.write_dependency)
            add("system_readback", workflow.readback_dependency)
        steps.append(PlanStep("clinician_acceptance", None, "clinician", "required"))
        return SitePlan(capability_id, tuple(steps), tuple(dict.fromkeys(blockers)), not blockers)
