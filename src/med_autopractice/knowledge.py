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

_OPERATION_COMMANDS: dict[str, tuple[str, ...]] = {
    "sources": ("workspace", "sources", "list"),
    "sources-add": ("workspace", "sources", "add"),
    "sources-remove": ("workspace", "sources", "remove"),
    "snapshot": ("workspace", "snapshot"),
    "writeback-propose": ("workspace", "writeback", "propose"),
    "writeback-commit": ("workspace", "writeback", "commit"),
}
OPERATIONS = tuple(_OPERATION_COMMANDS)
_SCOPE_OPTIONS = {"--workspace", "--json"}


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
        """Forward one owner operation through Framework v2 workspace commands."""
        cmd_prefix = _OPERATION_COMMANDS.get(operation)
        if cmd_prefix is None:
            raise ValueError(f"unsupported workspace knowledge operation: {operation}")
        if any(arg.split("=", 1)[0] in _SCOPE_OPTIONS for arg in arguments):
            raise ValueError("workspace arguments cannot override MAP scope or output format")
        command = [self.opl_bin, *cmd_prefix]
        if operation != "writeback-commit":
            command += ["--workspace", self.workspace]
        command += [*arguments, "--json"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KnowledgeCommandError(f"workspace {operation} did not complete") from error
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise KnowledgeCommandError(f"workspace {operation} returned invalid JSON") from error
        if result.returncode or not isinstance(envelope, dict) or envelope.get("error"):
            raise KnowledgeCommandError(f"workspace {operation} was rejected by Framework")
        return envelope
