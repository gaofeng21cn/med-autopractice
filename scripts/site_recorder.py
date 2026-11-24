"""Convert private recorded UI event traces into sanitized Playbook v2 JSON outside the repo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from med_autopractice.workflows.site_recorder import PlaybookRecorder  # noqa: E402


def _ensure_outside_repo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"recorded event traces and site playbooks must live outside the repository: {resolved}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, type=Path, help="Private JSON list of recorded events")
    parser.add_argument("--output", required=True, type=Path, help="Output sanitized Playbook v2 path (outside repo)")
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--playbook-id", required=True)
    parser.add_argument("--title", default="Recorded Site Playbook")
    parser.add_argument("--redact", action="append", default=[], help="Extra sensitive string tokens to strip")
    args = parser.parse_args(argv)

    events_path = _ensure_outside_repo(args.events)
    output_path = _ensure_outside_repo(args.output)
    raw = json.loads(events_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        parser.error("--events JSON must be a list of event objects")

    recorder = PlaybookRecorder(
        site_id=args.site_id,
        playbook_id=args.playbook_id,
        title=args.title,
        sensitive_tokens=args.redact,
    )
    doc = recorder.build_document(raw)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_path.chmod(0o600)
    print(json.dumps({
        "playbook_id": doc["playbook_id"],
        "steps_count": len(doc["steps"]),
        "output": str(output_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
