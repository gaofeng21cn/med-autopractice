"""Read the installed glkvm-native shared-service state without connecting a device."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _locate_glkvm_receipt(codex_home: Path) -> Path | None:
    opl_bin = os.environ.get("OPL_BIN") or shutil.which("opl")
    if opl_bin:
        try:
            located = subprocess.run(
                [opl_bin, "packages", "locate", "glkvm-native", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if located.returncode == 0:
                location = json.loads(located.stdout).get("opl_package_location", {})
                package_root = location.get("package_root") if location.get("found") else None
                if isinstance(package_root, str) and package_root:
                    candidate = Path(package_root) / "installation.json"
                    if candidate.is_file():
                        return candidate
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
            pass
    fallback = codex_home / "glkvm-native" / "installation.json"
    return fallback if fallback.is_file() else None


def main() -> int:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    receipt_path = _locate_glkvm_receipt(codex_home)
    if receipt_path is None:
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
