"""Check tracked text and reachable Git history for common disclosure risks."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REVIEWED_IMAGES = {
    "assets/branding/medautopractice-logo.png",
    "assets/branding/medautopractice-overview.png",
}
RULES = {
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
PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]+)?|credentials|secrets|patients?|captures|logs|"
    r"site-local|site-profiles-local|private-site|site-evidence|ui-qualification|"
    r"site-profile[^/]*\.json|[^/]+-site-profile\.json)(?:/|$)|"
    r"\.(?:local\.json|sqlite3?|db|p12|pfx|key)$",
    re.I,
)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout


def check_file(path: str, data: bytes) -> list[str]:
    if PRIVATE_PATH.search(path):
        return [f"{path}: private file path"]
    if path in REVIEWED_IMAGES and data.startswith(b"\x89PNG\r\n\x1a\n"):
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
        for rule, pattern in RULES.items()
        if pattern.search(line)
    ]


def main() -> int:
    findings = set()
    paths = git("ls-files", "-z").decode().split("\0")
    for relative in filter(None, paths):
        path = ROOT / relative
        if path.is_symlink() or not path.is_file():
            findings.add(f"{relative}: expected a regular source file")
        else:
            findings.update(check_file(relative, path.read_bytes()))

    seen = set()
    for commit in git("rev-list", "--all").decode().splitlines():
        entries = git("ls-tree", "-rz", commit).decode().split("\0")
        for entry in filter(None, entries):
            meta, path = entry.split("\t", 1)
            mode, _, object_id = meta.split()
            if (object_id, path) in seen:
                continue
            seen.add((object_id, path))
            if mode in {"100644", "100755"}:
                errors = check_file(path, git("cat-file", "blob", object_id))
            else:
                errors = [f"{path}: expected a regular source file"]
            findings.update(f"{commit[:12]} {error}" for error in errors)

    if findings:
        for finding in sorted(findings):
            print(finding, file=sys.stderr)
        return 1
    print("Disclosure pattern check passed for tracked files and reachable Git history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
