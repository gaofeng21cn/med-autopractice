"""Run a synthetic order-review task through the public MAP CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


def run_demo(store: Path) -> dict:
    def run(command: str, expected_status: str, *arguments: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/map_runtime.py"), command,
             "--store", str(store), *arguments],
            check=True, stdout=subprocess.PIPE, encoding="utf-8",
        )
        view = json.loads(result.stdout)
        if view["status"] != expected_status:
            raise ValueError(f'{command} returned {view["status"]}, expected {expected_status}')
        return view

    request = ROOT / "examples/synthetic/revise-orders.json"
    draft = run("start", "needs_pre_submit_review", "--request", str(request))
    task = ("--task-id", draft["task_id"])
    clinician = ("--clinician", "demo-doctor")
    submitted = run(
        "submit", "needs_clinician_acceptance", *task, *clinician,
        "--digest", draft["proposal"]["digest"],
        "--revision", draft["snapshot"]["revision"],
    )
    run("review", "accepted", *task, *clinician, "--accept")
    result = run("show", "accepted", *task)
    return {
        "mode": "synthetic",
        "task_id": result["task_id"],
        "capability_id": result["capability_id"],
        "statuses": [draft["status"], submitted["status"], result["status"]],
        "readback": result["execution"]["readback"],
        "next_action": result["next_action"],
    }


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        with TemporaryDirectory(prefix="map-demo-") as directory:
            result = run_demo(Path(directory) / "tasks.sqlite")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"MAP demo failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
