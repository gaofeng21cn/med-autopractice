"""Read the installed glkvm-native shared-service state without connecting a device."""

import json
import os
from pathlib import Path
import sys


def main() -> int:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    receipt_path = codex_home / "glkvm-native" / "installation.json"
    if not receipt_path.is_file():
        print("glkvm-native is not installed", file=sys.stderr)
        return 1
    receipt = json.loads(receipt_path.read_text())
    runtime = Path(receipt["runtime"])
    python = runtime / ".venv" / "bin" / "python"
    if not python.is_file() or not (runtime / "gateway.py").is_file():
        print("glkvm-native installation is incomplete", file=sys.stderr)
        return 1
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(repo / "src"), str(runtime)))
    env["GLKVM_PROFILE"] = receipt["profile"]
    env["MAP_EXPECTED_KVM_REVISION"] = receipt["runtime_revision"]
    os.execve(str(python), [str(python), "-m", "med_autopractice.adapters.kvm"], env)


if __name__ == "__main__":
    raise SystemExit(main())
