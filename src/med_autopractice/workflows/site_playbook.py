"""Load and validate site-specific playbooks kept outside the source repository.

Playbooks carry the exact operating knowledge of one hospital deployment, so
they are deployment data: they live in the private site directory, are never
committed, and hold references to credentials rather than credential values.
This module only reads and validates them; it never touches a device.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


_PRIMITIVES = {
    "kvm_observe", "kvm_wait", "kvm_click", "kvm_double_click", "kvm_type_ascii",
    "kvm_keys", "kvm_scroll", "kvm_drag", "browser_autofill_select",
    "human_action", "human_handoff",
}
_EFFECTS = {"read_only", "draft_write", "reviewed_write"}
_FAILURES = {"stop", "retry_once", "handoff", "reobserve"}
_EVIDENCE = {"none", "ref", "screenshot_ref"}
_STATES = {"unverified", "verified", "revoked"}
_FORBIDDEN_KEYS = {
    "password", "password_value", "passwd", "token", "cookie", "secret",
    "credential_value", "private_key", "patient", "patient_id", "patient_name",
    "encounter_id", "medical_record", "diagnosis", "order_text", "note_text",
    "screenshot_data", "image_bytes",
}


@dataclass(frozen=True)
class PlaybookStep:
    id: str
    intent: str
    primitive: str
    target: str | None
    region_hint: tuple[int, int, int, int] | None
    expect_text: str
    effect: str
    on_failure: str
    state: str
    evidence: str
    notes_ref: str | None

    @property
    def is_write(self) -> bool:
        return self.effect != "read_only"

    @property
    def is_verified(self) -> bool:
        return self.state == "verified"


@dataclass(frozen=True)
class Playbook:
    playbook_id: str
    site_id: str
    title: str
    purpose: str
    systems: tuple[str, ...]
    preconditions: tuple[str, ...]
    account_ref: str | None
    evidence_dir_ref: str | None
    revision_ref: str | None
    steps: tuple[PlaybookStep, ...]

    @property
    def contains_write(self) -> bool:
        return any(step.is_write for step in self.steps)

    @property
    def verified_steps(self) -> int:
        return sum(1 for step in self.steps if step.is_verified)

    @property
    def all_steps_verified(self) -> bool:
        return self.verified_steps == len(self.steps)


def _reject_forbidden(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"playbook contains forbidden field: {path}{normalized}")
            _reject_forbidden(child, f"{path}{normalized}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{path}{index}.")


def _parse_step(document: dict[str, Any]) -> PlaybookStep:
    step_id = document.get("id")
    region = document.get("region_hint")
    if region is not None and (
        not isinstance(region, list) or len(region) != 4
        or not all(type(value) is int for value in region)
        or not (region[0] < region[2] and region[1] < region[3])
    ):
        raise ValueError(f"invalid region hint in step: {step_id!r}")
    if (not isinstance(step_id, str) or not step_id
            or document.get("primitive") not in _PRIMITIVES
            or document.get("effect") not in _EFFECTS
            or document.get("on_failure") not in _FAILURES
            or document.get("evidence", "none") not in _EVIDENCE
            or document.get("state", "unverified") not in _STATES
            or not isinstance(document.get("intent"), str) or not document["intent"]
            or not isinstance(document.get("expect_text"), str) or not document["expect_text"]):
        raise ValueError(f"invalid playbook step: {step_id!r}")
    return PlaybookStep(
        step_id, document["intent"], document["primitive"], document.get("target"),
        tuple(region) if region else None, document["expect_text"],
        document["effect"], document["on_failure"], document.get("state", "unverified"),
        document.get("evidence", "none"), document.get("notes_ref"),
    )


class SitePlaybook:
    def __init__(self, document: dict[str, Any], known_systems: set[str] | None = None):
        _reject_forbidden(document)
        if document.get("schema_version") != "map-site-playbook.v1":
            raise ValueError("unsupported site playbook")
        systems = document.get("systems")
        steps = document.get("steps")
        if (not isinstance(document.get("playbook_id"), str) or not document["playbook_id"]
                or not isinstance(document.get("site_id"), str) or not document["site_id"]
                or not isinstance(document.get("title"), str) or not document["title"]
                or not isinstance(systems, list) or not systems
                or any(not isinstance(item, str) or not item for item in systems)
                or not isinstance(steps, list) or not steps):
            raise ValueError("incomplete site playbook")
        if known_systems is not None and not set(systems) <= known_systems:
            raise ValueError("playbook references a system absent from the site survey")
        parsed = tuple(_parse_step(item) for item in steps)
        if len({step.id for step in parsed}) != len(parsed):
            raise ValueError("playbook step IDs must be unique")
        self.playbook = Playbook(
            document["playbook_id"], document["site_id"], document["title"],
            document.get("purpose", ""), tuple(systems),
            tuple(document.get("preconditions", ())), document.get("account_ref"),
            document.get("evidence_dir_ref"), document.get("revision_ref"), parsed,
        )

    @classmethod
    def load(cls, path: Path, known_systems: set[str] | None = None) -> Playbook:
        return cls(json.loads(path.read_text(encoding="utf-8")), known_systems).playbook

    @classmethod
    def load_directory(cls, directory: Path,
                       known_systems: set[str] | None = None) -> tuple[Playbook, ...]:
        if not directory.is_dir():
            raise ValueError(f"playbook directory does not exist: {directory}")
        loaded = [cls.load(path, known_systems) for path in sorted(directory.glob("*.json"))]
        ids = [book.playbook_id for book in loaded]
        if len(set(ids)) != len(ids):
            raise ValueError("playbook IDs must be unique")
        return tuple(loaded)


def write_plan(playbook: Playbook) -> dict:
    """Project a playbook as the ordered plan a host or agent will execute."""

    return {
        "playbook_id": playbook.playbook_id,
        "site_id": playbook.site_id,
        "title": playbook.title,
        "purpose": playbook.purpose,
        "systems": list(playbook.systems),
        "preconditions": list(playbook.preconditions),
        "account_ref": playbook.account_ref,
        "evidence_dir_ref": playbook.evidence_dir_ref,
        "revision_ref": playbook.revision_ref,
        "contains_write": playbook.contains_write,
        "verified_steps": playbook.verified_steps,
        "steps_total": len(playbook.steps),
        "all_steps_verified": playbook.all_steps_verified,
        "steps": [
            {
                "id": step.id,
                "intent": step.intent,
                "primitive": step.primitive,
                "target": step.target,
                "region_hint": list(step.region_hint) if step.region_hint else None,
                "expect_text": step.expect_text,
                "effect": step.effect,
                "on_failure": step.on_failure,
                "state": step.state,
                "evidence": step.evidence,
                "notes_ref": step.notes_ref,
            }
            for step in playbook.steps
        ],
    }
