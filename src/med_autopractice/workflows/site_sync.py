"""Validate and synchronize a unified external Site Pack directory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from med_autopractice.platform.security import verify_private_directory
from med_autopractice.workflows.site_plan import SiteCatalog
from med_autopractice.workflows.site_playbook import Playbook, SitePlaybook
from med_autopractice.workflows.site_survey import SiteSurveyCatalog


@dataclass(frozen=True)
class SitePackReport:
    site_id: str
    directory: str
    playbooks_count: int
    verified_playbooks: tuple[str, ...]
    unverified_playbooks: tuple[str, ...]
    issues: tuple[str, ...]
    synced_dependencies: tuple[str, ...]
    valid: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def inspect_and_sync_site_pack(
    directory: Path,
    *,
    repo_root: Path | None = None,
    sync_observed: bool = False,
) -> SitePackReport:
    """Validate site-dependencies, site-survey, playbooks and deployment descriptor together."""

    root = verify_private_directory(directory, repo_root)
    issues: list[str] = []
    synced: list[str] = []

    deps_path = root / "dependencies.json"
    if not deps_path.is_file():
        raise ValueError(f"missing dependencies.json in {root}")
    deps_doc = _read_json(deps_path)
    catalog = SiteCatalog(deps_doc)
    site_id = str(catalog.profile_id or "")
    if not site_id:
        raise ValueError("dependencies.json must declare a non-empty profile_id")

    survey_path = root / "site-survey.json"
    if not survey_path.is_file():
        survey_path = root / "survey.json"
    survey: SiteSurveyCatalog | None = None
    if survey_path.is_file():
        survey = SiteSurveyCatalog.load(survey_path)
        if survey.profile_id != site_id:
            issues.append(f"survey profile_id ({survey.profile_id}) differs from site_id ({site_id})")
    else:
        issues.append("missing site-survey.json (or survey.json)")

    playbooks_dir = root / "playbooks"
    playbooks: tuple[Playbook, ...] = ()
    if playbooks_dir.is_dir():
        known_systems = set(survey.system_ids) if survey is not None else None
        playbooks = SitePlaybook.load_directory(playbooks_dir, known_systems)
        for book in playbooks:
            if book.site_id != site_id:
                issues.append(f"playbook {book.playbook_id} targets site {book.site_id}, expected {site_id}")
    else:
        issues.append("missing playbooks directory")

    deployment_path = root / "deployment.json"
    if deployment_path.is_file():
        dep_cfg = _read_json(deployment_path)
        if dep_cfg.get("site_id") != site_id:
            issues.append(f"deployment.json site_id differs from {site_id}")
        playbook_map = dep_cfg.get("playbooks")
        if isinstance(playbook_map, dict):
            known_book_ids = {b.playbook_id for b in playbooks}
            for role, pid in playbook_map.items():
                if isinstance(pid, str) and pid not in known_book_ids:
                    issues.append(f"deployment playbook mapping {role}->{pid} not found in playbooks/")

    if sync_observed and survey is not None and playbooks:
        # Never promote to qualified automatically; only sync unmapped -> observed when a
        # playbook with an evidence/notes reference exists for the matching system.
        book_by_system = {
            system: book for book in playbooks for system in book.systems
            if book.evidence_dir_ref or book.revision_ref or any(step.notes_ref for step in book.steps)
        }
        changed = False
        for dep_entry in deps_doc.get("dependencies", []):
            if dep_entry.get("owner") != "adapter" or dep_entry.get("state") != "unmapped":
                continue
            sys_id = dep_entry.get("system_id")
            if sys_id in book_by_system:
                book = book_by_system[sys_id]
                dep_entry["state"] = "observed"
                dep_entry["evidence_ref"] = (
                    book.evidence_dir_ref
                    or book.revision_ref
                    or next(step.notes_ref for step in book.steps if step.notes_ref)
                )
                synced.append(dep_entry["id"])
                changed = True
        if changed:
            deps_path.write_text(json.dumps(deps_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verified = tuple(b.playbook_id for b in playbooks if b.all_steps_verified)
    unverified = tuple(b.playbook_id for b in playbooks if not b.all_steps_verified)
    return SitePackReport(
        site_id=site_id,
        directory=str(root),
        playbooks_count=len(playbooks),
        verified_playbooks=verified,
        unverified_playbooks=unverified,
        issues=tuple(issues),
        synced_dependencies=tuple(synced),
        valid=not issues,
    )
