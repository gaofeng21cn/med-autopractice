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
    digest = hashlib.sha256()
    for relative in paths:
        file = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or file.is_symlink():
            raise ValueError("Package content must be ordinary relative files")
        if not file.resolve().is_relative_to(root.resolve()):
            raise ValueError("Package content escaped its owner root")
        for data in (relative.encode("utf-8"), file.read_bytes()):
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return "sha256:" + digest.hexdigest()


def verify_package(root: Path = ROOT) -> dict:
    package = json.loads((root / "opl-package.json").read_text())
    agent = json.loads((root / "agent/manifest.json").read_text())
    plugin = json.loads((root / ".codex-plugin/plugin.json").read_text())
    standard_plugin = json.loads((root / "plugin.json").read_text())
    marketplace = json.loads((root / ".agents/plugins/marketplace.json").read_text())
    if package["package_id"] != agent["agent_id"] or plugin["name"] != package["package_id"]:
        raise ValueError("Package and plugin identities differ")
    if not package["version"] == agent["version"] == plugin["version"]:
        raise ValueError("Package and plugin versions differ")
    if standard_plugin["name"] != plugin["name"] or standard_plugin["version"] != plugin["version"] or standard_plugin["extensions"]["com.openai"]["interface"] != plugin["interface"]:
        raise ValueError("Agent Plugins and Codex compatibility metadata differ")
    entry = marketplace["plugins"]
    if marketplace["name"] != package["package_id"] or len(entry) != 1 or entry[0]["name"] != plugin["name"] or entry[0]["source"] != {"source": "local", "path": "."}:
        raise ValueError("Native marketplace does not select this package root")
    lock = package["content_lock"]
    if lock["algorithm"] != "sha256" or lock["canonicalization"] != "ordered_path_length_file_length_bytes" or lock["digest"] != content_digest(root, lock["paths"]):
        raise ValueError("Package content lock does not match the installed files")
    return {"package_id": package["package_id"], "display_name": package["display_name"],
            "description": package["description"], "version": package["version"],
            "channel": package["publication_channel_admission"],
            "content_integrity": "verified", "lifecycle_owner": "codex_plugin_manager",
            "plugin_selector": package["codex_surface"]["configured_codex_plugin_carrier"]["plugin_selector"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-lock", action="store_true", help="refresh the digest for the already reviewed path list")
    args = parser.parse_args()
    if args.write_lock:
        file = ROOT / "opl-package.json"
        package = json.loads(file.read_text())
        package["content_lock"]["digest"] = content_digest(ROOT, package["content_lock"]["paths"])
        file.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(verify_package(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
