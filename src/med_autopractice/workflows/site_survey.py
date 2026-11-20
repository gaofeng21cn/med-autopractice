"""Validate private, non-patient UI survey packs and produce read-only plans."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


_STATES = {"unmapped", "observed", "qualified", "revoked"}
_MODES = {"read", "draft_write", "reviewed_write"}
_FORBIDDEN_KEYS = {
    "patient", "patient_id", "patient_name", "encounter_id", "medical_record",
    "diagnosis", "order_text", "note_text", "screenshot_data", "image_bytes",
    "password", "token", "cookie", "secret",
}


@dataclass(frozen=True)
class SurveyOperation:
    dependency_id: str
    system_id: str
    semantic_action: str
    mode: str
    state: str
    evidence_refs: tuple[str, ...]
    qualification_ref: str | None
    notes_ref: str | None


@dataclass(frozen=True)
class SurveyPlan:
    profile_id: str
    capability_id: str | None
    operations: tuple[SurveyOperation, ...]
    blockers: tuple[str, ...]
    qualified: bool


def _walk_forbidden(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"private survey pack contains forbidden field: {path + normalized}")
            _walk_forbidden(child, f"{path}{normalized}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{path}{index}.")


class SiteSurveyCatalog:
    """A read-only index of how a deployment will qualify UI operations.

    The pack contains references to private evidence, never the evidence body.
    It is intentionally separate from the generic dependency catalog so that
    clinical readiness and UI survey observations cannot be confused.
    """

    def __init__(self, document: dict[str, Any]):
        _walk_forbidden(document)
        if document.get("schema_version") != "map-site-survey.v1":
            raise ValueError("unsupported site survey pack")
        profile_id = document.get("profile_id")
        systems = document.get("systems")
        operations = document.get("operations")
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("survey profile_id is required")
        if not isinstance(systems, list) or not isinstance(operations, list):
            raise ValueError("survey systems and operations must be lists")
        system_ids = [item.get("id") for item in systems]
        if (any(not isinstance(value, str) or not value for value in system_ids)
                or len(system_ids) != len(set(system_ids))):
            raise ValueError("survey system IDs must be unique")
        self.profile_id = profile_id
        self.system_ids = frozenset(system_ids)
        self.operations: dict[str, SurveyOperation] = {}
        for item in operations:
            dependency_id = item.get("dependency_id")
            system_id = item.get("system_id")
            mode = item.get("mode")
            state = item.get("state")
            evidence_refs = item.get("evidence_refs", [])
            if (not isinstance(dependency_id, str) or not dependency_id
                    or dependency_id in self.operations
                    or system_id not in system_ids
                    or not isinstance(item.get("semantic_action"), str)
                    or not item["semantic_action"]
                    or mode not in _MODES or state not in _STATES
                    or not isinstance(evidence_refs, list)
                    or any(not isinstance(ref, str) or not ref for ref in evidence_refs)
                    or (state in {"observed", "qualified"} and not evidence_refs)
                    or (state == "qualified" and not item.get("qualification_ref"))):
                raise ValueError(f"invalid site survey operation: {dependency_id!r}")
            self.operations[dependency_id] = SurveyOperation(
                dependency_id, system_id, item["semantic_action"], mode, state,
                tuple(evidence_refs), item.get("qualification_ref"), item.get("notes_ref"),
            )

    @classmethod
    def load(cls, path: Path) -> "SiteSurveyCatalog":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def plan(self, capability_id: str | None = None, dependency_ids: tuple[str, ...] | None = None) -> SurveyPlan:
        selected = dependency_ids or tuple(self.operations)
        operations = []
        blockers = []
        for dependency_id in selected:
            try:
                operation = self.operations[dependency_id]
            except KeyError as error:
                raise ValueError(f"survey operation is missing: {dependency_id}") from error
            operations.append(operation)
            if operation.state != "qualified":
                blockers.append(dependency_id)
        return SurveyPlan(self.profile_id, capability_id, tuple(operations), tuple(blockers), not blockers)
