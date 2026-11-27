"""Verify or refresh the reviewed OPL Package content lock, without external I/O."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def content_digest(root: Path, paths: list[str]) -> str:
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Package content paths must be nonempty and unique")
    resolved_root = root.resolve()
    for relative in paths:
        file = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or file.is_symlink():
            raise ValueError("Package content must be ordinary relative files")
        if not file.resolve().is_relative_to(resolved_root):
            raise ValueError("Package content escaped its owner root")
    digest = hashlib.sha256()
    for relative in paths:
        for data in (relative.encode("utf-8"), (root / relative).read_bytes()):
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return "sha256:" + digest.hexdigest()


def verify_package(root: Path = ROOT) -> dict:
    package = json.loads((root / "opl-package.json").read_text(encoding="utf-8"))
    identity = package["identity"]
    agent = json.loads((root / "agent/manifest.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "contracts/package_content_lock.json").read_text(encoding="utf-8"))
    if identity["id"] != agent["agent_id"]:
        raise ValueError("Package and agent identities differ")
    if identity["version"] != agent["version"]:
        raise ValueError("Package and agent versions differ")
    distribution = package.get("distribution") or {}
    for field in ("access_policy_ref", "credential_provider"):
        if field in distribution and not (isinstance(distribution[field], str) and distribution[field].strip()):
            raise ValueError(f"Package {field} must be a nonempty string when declared")
    if lock["algorithm"] != "sha256" or lock["canonicalization"] != "ordered_path_length_file_length_bytes" or lock["digest"] != content_digest(root, lock["paths"]):
        raise ValueError("Package content lock does not match the installed files")
    return {"package_id": identity["id"], "display_name": identity["name"]["en-US"],
            "description": identity["description"]["en-US"], "version": identity["version"],
            "content_integrity": "verified", "lifecycle_owner": "opl_pack"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-lock", action="store_true", help="refresh the digest for the already reviewed path list")
    args = parser.parse_args()
    if args.write_lock:
        file = ROOT / "contracts/package_content_lock.json"
        lock = json.loads(file.read_text(encoding="utf-8"))
        lock["digest"] = content_digest(ROOT, lock["paths"])
        file.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verify_package(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
