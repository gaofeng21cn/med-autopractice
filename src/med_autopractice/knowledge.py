"""Thin client for Framework-owned knowledge sources and reviewed writeback.

MAP owns no registry, private knowledge body, cache, search index or writer.
Hospital profiles, operating playbooks and patient records keep their existing
domain owners; this interface is for explicitly bound reusable Markdown sources.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import shutil
import subprocess


DOMAIN_ID = "med-autopractice"
OPERATIONS = (
    "sources", "search", "discover", "resolve-target", "read-source",
    "inspect-source", "register", "propose", "review", "apply",
    "review-source", "verify-dependencies",
)
_SCOPE_OPTIONS = {"--workspace", "--project", "--domain", "--json"}


class KnowledgeCommandError(RuntimeError):
    """The Framework owner did not complete a requested operation."""


class KnowledgeSources:
    def __init__(self, workspace: Path, *, project: Path | None = None,
                 opl_bin: str | None = None):
        self.workspace = str(workspace.expanduser().resolve())
        self.project = str(project.expanduser().resolve()) if project else None
        self.opl_bin = opl_bin or os.environ.get("OPL_BIN") or shutil.which("opl")
        if not self.opl_bin:
            raise KnowledgeCommandError("OPL CLI is unavailable; configure OPL_BIN")

    def run(self, operation: str, arguments: Sequence[str] = ()) -> dict:
        """Forward one owner operation; no local fallback or data mutation."""
        if operation not in OPERATIONS:
            raise ValueError(f"unsupported knowledge operation: {operation}")
        if any(arg.split("=", 1)[0] in _SCOPE_OPTIONS for arg in arguments):
            raise ValueError("knowledge arguments cannot override MAP scope or output format")
        command = [self.opl_bin, "knowledge", operation,
                   "--workspace", self.workspace, "--domain", DOMAIN_ID]
        if self.project:
            command += ["--project", self.project]
        command += [*arguments, "--json"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KnowledgeCommandError(f"knowledge {operation} did not complete") from error
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise KnowledgeCommandError(f"knowledge {operation} returned invalid JSON") from error
        if result.returncode or not isinstance(envelope, dict) or envelope.get("error"):
            raise KnowledgeCommandError(f"knowledge {operation} was rejected by Framework")
        if envelope.get("version") != "g2":
            raise KnowledgeCommandError("unsupported Framework knowledge response")
        return envelope
