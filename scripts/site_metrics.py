"""Print zero-PHI operational metrics for a private MAP site TaskStore SQLite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from med_autopractice.workflows.metrics import compute_site_metrics  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path, help="Private TaskStore SQLite path outside repo")
    args = parser.parse_args(argv)

    store_path = args.store.expanduser().resolve()
    if store_path.is_relative_to(ROOT):
        parser.error("--store must live outside the repository")
    metrics = compute_site_metrics(store_path)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
