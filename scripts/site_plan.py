"""Inspect site readiness, or explicitly initialize a new external profile."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(ROOT))

from med_autopractice.workflows.catalog import PHASE1_WORKFLOWS
from med_autopractice.workflows.site_plan import SiteCatalog


DEFAULT_PROFILE = ROOT / "contracts/site-dependencies.json"


def resolve_profile_path(argument: Path | None) -> Path:
    configured = argument or os.environ.get("MAP_SITE_PROFILE")
    if not configured:
        return DEFAULT_PROFILE
    path = Path(configured).expanduser().resolve()
    default_path = DEFAULT_PROFILE.resolve()
    if path.is_relative_to(ROOT.resolve()) and path != default_path:
        raise ValueError("deployment profiles must be stored outside the source repository")
    return path


def initialize_profile(destination: Path) -> Path:
    path = destination.expanduser()
    target = path.parent.resolve() / path.name
    if target.is_relative_to(ROOT) or any((parent / ".git").exists() for parent in target.parents):
        raise ValueError("create deployment profiles outside Git worktrees and the MAP package")
    # Publish the complete, owner-only file without overwriting a concurrent creator.
    draft = NamedTemporaryFile(dir=target.parent, delete=False)
    try:
        with draft:
            draft.write(DEFAULT_PROFILE.read_bytes())
        os.link(draft.name, target)
    finally:
        Path(draft.name).unlink(missing_ok=True)
    return target


def inspect_profile(path: Path, catalog: SiteCatalog, capability_id: str | None,
                    dependency_id: str | None) -> dict:
    result = {"profile_source": "template" if path == DEFAULT_PROFILE else "external",
              "profile_id": catalog.profile_id}
    if dependency_id:
        if dependency_id not in catalog.dependencies:
            raise ValueError(f"unknown site dependency: {dependency_id}")
        result["dependency"] = asdict(catalog.dependencies[dependency_id])
    elif capability_id:
        result["plan"] = asdict(catalog.plan(capability_id))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capability_id", nargs="?", choices=tuple(PHASE1_WORKFLOWS))
    parser.add_argument("--profile", type=Path,
                        help="private deployment profile outside this repository; defaults to MAP_SITE_PROFILE or the generic template")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true", help="show the selected profile and the requested readiness only")
    mode.add_argument("--init-profile", type=Path, metavar="FILE",
                      help="create an unconfigured profile in an existing private directory")
    parser.add_argument("--dependency", metavar="ID", help="inspect one operation, including its evidence references")
    args = parser.parse_args()
    if args.init_profile and (args.profile or args.capability_id or args.dependency):
        parser.error("--init-profile takes only the new file path")
    if args.dependency and args.capability_id:
        parser.error("choose a workflow or a single dependency")
    try:
        path = initialize_profile(args.init_profile) if args.init_profile else resolve_profile_path(args.profile)
        if not path.is_file():
            raise ValueError(f"site profile does not exist or is not a file: {path}")
        catalog = SiteCatalog.load(path)
        if args.inspect or args.dependency or args.init_profile:
            result = inspect_profile(path, catalog, args.capability_id, args.dependency)
        else:
            ids = (args.capability_id,) if args.capability_id else PHASE1_WORKFLOWS
            result = [asdict(catalog.plan(capability_id)) for capability_id in ids]
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
