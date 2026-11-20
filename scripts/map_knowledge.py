"""Use Framework's shared source registry from the MAP direct Skill.

Place --workspace and optional --project before the operation. Remaining flags
are passed unchanged to `opl knowledge <operation>`; Framework validates source
authorization, accepted versions, review evidence and writes to the source owner.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.knowledge import KnowledgeCommandError, KnowledgeSources, OPERATIONS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        envelope = KnowledgeSources(args.workspace, project=args.project).run(
            args.operation, args.arguments)
    except (KnowledgeCommandError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
