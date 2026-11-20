"""Show non-patient, read-only readiness for one or all phase-one workflows."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(ROOT))

from med_autopractice.workflows.catalog import PHASE1_WORKFLOWS
from med_autopractice.workflows.site_plan import SiteCatalog


DEFAULT_PROFILE = ROOT / "contracts/site-dependencies.json"


def resolve_profile_path(argument: Path | None) -> Path:
    configured = argument or os.environ.get("MAP_SITE_PROFILE")
    if configured is None:
        return DEFAULT_PROFILE
    path = Path(configured).expanduser().resolve()
    default_path = DEFAULT_PROFILE.resolve()
    if path.is_relative_to(ROOT.resolve()) and path != default_path:
        raise ValueError("deployment profiles must be stored outside the source repository")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capability_id", nargs="?", choices=tuple(PHASE1_WORKFLOWS))
    parser.add_argument("--profile", type=Path,
                        help="private deployment profile outside this repository; defaults to MAP_SITE_PROFILE or the generic template")
    args = parser.parse_args()
    try:
        profile_path = resolve_profile_path(args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    if not profile_path.is_file():
        parser.error(f"site profile does not exist or is not a file: {profile_path}")
    catalog = SiteCatalog.load(profile_path)
    ids = (args.capability_id,) if args.capability_id else PHASE1_WORKFLOWS
    print(json.dumps([asdict(catalog.plan(capability_id)) for capability_id in ids],
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
