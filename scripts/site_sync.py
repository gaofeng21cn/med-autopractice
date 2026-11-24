#!/usr/bin/env python3
"""Inspect and safely synchronize an external MAP Site Pack directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.workflows.site_sync import inspect_and_sync_site_pack


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True, help="External private Site Pack directory")
    parser.add_argument(
        "--sync-observed",
        action="store_true",
        help="Promote unmapped dependencies with documented playbooks to observed (never qualified)",
    )
    args = parser.parse_args()
    try:
        report = inspect_and_sync_site_pack(args.site, repo_root=ROOT, sync_observed=args.sync_observed)
    except (OSError, PermissionError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
