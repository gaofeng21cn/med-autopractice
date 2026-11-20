"""List or show private site playbooks; read-only and outside the repository."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.workflows.site_playbook import SitePlaybook, write_plan
from med_autopractice.workflows.site_survey import SiteSurveyCatalog


def _external(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("playbooks are deployment data and must stay outside the source repository")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playbooks", required=True, type=Path,
                        help="private playbook directory outside this repository")
    parser.add_argument("--survey", type=Path,
                        help="private site survey pack used to validate system references")
    parser.add_argument("--playbook", help="show one playbook instead of listing all")
    args = parser.parse_args()
    try:
        directory = _external(args.playbooks)
        known = None
        if args.survey:
            survey = SiteSurveyCatalog(
                json.loads(_external(args.survey).read_text(encoding="utf-8")))
            known = set(survey.system_ids)
        books = SitePlaybook.load_directory(directory, known)
        if args.playbook:
            matches = [book for book in books if book.playbook_id == args.playbook]
            if len(matches) != 1:
                raise ValueError(f"unknown or ambiguous playbook: {args.playbook}")
            payload = write_plan(matches[0])
        else:
            payload = [
                {
                    "playbook_id": book.playbook_id,
                    "title": book.title,
                    "systems": list(book.systems),
                    "steps": len(book.steps),
                    "verified_steps": book.verified_steps,
                    "all_steps_verified": book.all_steps_verified,
                    "contains_write": book.contains_write,
                }
                for book in books
            ]
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
