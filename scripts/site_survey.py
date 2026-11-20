"""Show a read-only plan for qualifying private hospital UI operations."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.workflows.site_survey import SiteSurveyCatalog


def resolve_profile_path(argument: Path) -> Path:
    path = argument.expanduser().resolve()
    if path.is_relative_to(ROOT.resolve()):
        raise ValueError("survey packs must be stored outside the source repository")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path,
                        help="private map-site-survey.v1 JSON pack outside this repository")
    parser.add_argument("--dependency", action="append", dest="dependencies",
                        help="limit output to one or more dependency IDs")
    args = parser.parse_args()
    try:
        path = resolve_profile_path(args.profile)
        if not path.is_file():
            parser.error(f"survey pack does not exist or is not a file: {path}")
        catalog = SiteSurveyCatalog.load(path)
        plan = catalog.plan(dependency_ids=tuple(args.dependencies) if args.dependencies else None)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
