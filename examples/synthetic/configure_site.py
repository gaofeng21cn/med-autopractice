"""Create a synthetic mixed-transport deployment profile outside the source tree."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTES = {
    "read_lab_results": "native_api",
    "read_current_orders": "middleware",
    "read_nursing_records": "computer_use",
    "read_imaging_reports": "ip_kvm",
}


def write_example(target: Path):
    target = target.expanduser().resolve()
    if target.is_relative_to(ROOT):
        raise ValueError("choose a deployment directory outside the source checkout")
    document = json.loads((ROOT / "contracts/site-dependencies.json").read_text(encoding="utf-8"))
    document["profile_id"] = "demo-mixed-transports"
    for dependency in document["dependencies"]:
        name = dependency["id"]
        if name in ROUTES:
            dependency.update(transport=ROUTES[name], state="observed", evidence_ref=f"demo://survey/{name}")
    with target.open("x", encoding="utf-8") as output:
        json.dump(document, output, ensure_ascii=False, indent=2)
        output.write("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new file in an existing external directory")
    args = parser.parse_args()
    try:
        write_example(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
