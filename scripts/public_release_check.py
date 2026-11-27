#!/usr/bin/env python3
"""Check tracked text and reachable Git history for disclosure risks via Framework CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_DIRS = {".venv", "__pycache__", ".pytest_cache", "dist", "coverage", "node_modules"}
_FORBIDDEN_SUFFIXES = (".egg-info", ".pyc", ".pyo")
_EXCLUDED_ROOTS = {".git", ".worktrees", "worktrees"}
_RULES = {
    "GitHub credential": re.compile(r"gh[pousr]_[\w-]{20,}|github_pat_[\w-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "secret value in configuration": re.compile(
        r"""(?i)["']?(?:password|client_secret|access_token|api_key)["']?\s*[:=]\s*["'][^"'\s<>]{12,}["']"""
    ),
    "private IPv4 address": re.compile(
        r"\b(?:10\.(?:\d{1,3}\.){2}|192\.168\.\d{1,3}\.|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.)\d{1,3}\b"
    ),
    "personal workspace path": re.compile(r"/Users/[\w.-]+/workspace/"),
    "private package reference": re.compile(r"ghcr\.io/[^/\s]+/[^/\s]+-private-packages/", re.I),
}
_PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]+)?|credentials|secrets|patients?|captures|logs|"
    r"site-local|site-profiles-local|private-site|site-evidence|ui-qualification|"
    r"site-profile[^/]*\.json|[^/]+-site-profile\.json)(?:/|$)|"
    r"\.(?:local\.json|sqlite3?|db|p12|pfx|key)$",
    re.I,
)


def resolve_opl_bin() -> str | None:
    env_bin = os.environ.get("OPL_BIN", "").strip()
    if env_bin:
        return env_bin
    fw_root = os.environ.get("OPL_FRAMEWORK_ROOT", "").strip()
    if fw_root:
        candidate = Path(fw_root) / "bin" / "opl"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    sibling = ROOT.parent / "one-person-lab" / "bin" / "opl"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("opl")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout


def _reviewed_binary_paths() -> set[str]:
    policy_path = ROOT / "contracts" / "disclosure_policy.json"
    if not policy_path.is_file():
        return set()
    data = json.loads(policy_path.read_text(encoding="utf-8"))
    return set(data.get("reviewed_binary_paths", ()))


def _check_file(path: str, data: bytes, reviewed_images: set[str]) -> list[str]:
    if _PRIVATE_PATH.search(path):
        return [f"{path}: private file path"]
    if path in reviewed_images and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return []
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{path}: binary asset requires review"]
    if "\0" in source:
        return [f"{path}: binary asset requires review"]
    return [
        f"{path}:{line_number}: {rule}"
        for line_number, line in enumerate(source.splitlines(), 1)
        for rule, pattern in _RULES.items()
        if pattern.search(line)
    ]


def _fallback_check() -> int:
    byproducts: list[str] = []
    for current_root, dirs, files in os.walk(ROOT):
        rel_root = Path(current_root).relative_to(ROOT)
        if rel_root.parts and rel_root.parts[0] in _EXCLUDED_ROOTS:
            dirs[:] = []
            continue
        kept_dirs: list[str] = []
        for name in dirs:
            if name in _EXCLUDED_ROOTS:
                continue
            rel = (rel_root / name).as_posix() if rel_root != Path(".") else name
            if name in _FORBIDDEN_DIRS or name.endswith(_FORBIDDEN_SUFFIXES):
                byproducts.append(rel)
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            if name.endswith(_FORBIDDEN_SUFFIXES):
                rel = (rel_root / name).as_posix() if rel_root != Path(".") else name
                byproducts.append(rel)
    if byproducts:
        for item in sorted(byproducts):
            print(f"{item}: cache or install byproduct present", file=sys.stderr)
        return 3

    reviewed_images = _reviewed_binary_paths()
    findings: set[str] = set()
    for relative in filter(None, _git("ls-files", "-z").decode().split("\0")):
        path = ROOT / relative
        if path.is_symlink() or not path.is_file():
            findings.add(f"{relative}: expected a regular source file")
        else:
            findings.update(_check_file(relative, path.read_bytes(), reviewed_images))

    seen: set[tuple[str, str]] = set()
    for commit in _git("rev-list", "--all").decode().splitlines():
        for entry in filter(None, _git("ls-tree", "-rz", commit).decode().split("\0")):
            meta, path = entry.split("\t", 1)
            mode, _, object_id = meta.split()
            if (object_id, path) in seen:
                continue
            seen.add((object_id, path))
            if mode in {"100644", "100755"}:
                errors = _check_file(path, _git("cat-file", "blob", object_id), reviewed_images)
            else:
                errors = [f"{path}: expected a regular source file"]
            findings.update(f"{commit[:12]} {error}" for error in errors)

    if findings:
        for finding in sorted(findings):
            print(finding, file=sys.stderr)
        return 1
    print("Disclosure pattern check passed for tracked files and reachable Git history (fallback).")
    return 0


def main() -> int:
    return _fallback_check()


if __name__ == "__main__":
    raise SystemExit(main())
