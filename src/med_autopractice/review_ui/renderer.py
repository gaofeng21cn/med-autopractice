"""Render structured clinical review cards and MCP App / MiniApp HTML payloads."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
import json
from pathlib import Path
from typing import Any

_ASSET_PATH = Path(__file__).resolve().parent / "assets" / "review_app.html"


def build_review_card_model(task_view: Mapping[str, Any]) -> dict[str, Any]:
    """Extract normalized card view-model from a TaskRuntime view dictionary."""
    req = task_view.get("request") or {}
    subject = req.get("subject") or {}
    snapshot = task_view.get("snapshot") or {}
    proposal = task_view.get("proposal") or {}
    execution = task_view.get("execution") or {}
    readback = execution.get("readback") or {}
    receipt = execution.get("receipt") or {}
    status = str(task_view.get("status") or "")

    facts = []
    for item in snapshot.get("facts", []):
        if not isinstance(item, dict):
            continue
        facts.append({
            "fact_id": item.get("fact_id"),
            "value": item.get("value"),
            "unit": item.get("unit"),
            "status": item.get("status"),
            "source_ref": item.get("source_ref"),
            "effective_at": item.get("effective_at"),
        })

    return {
        "task_id": task_view.get("task_id"),
        "status": status,
        "next_action": task_view.get("next_action"),
        "capability_id": req.get("capability_id"),
        "intent": req.get("intent") or req.get("goal"),
        "subject": {
            "site_id": subject.get("site_id"),
            "patient_id": subject.get("patient_id"),
            "encounter_id": subject.get("encounter_id"),
        },
        "snapshot_revision": snapshot.get("revision"),
        "coverage": snapshot.get("coverage", []),
        "facts": facts,
        "proposal": {
            "digest": proposal.get("digest"),
            "generator_ref": proposal.get("generator_ref"),
            "cited_fact_ids": proposal.get("cited_fact_ids") or proposal.get("fact_refs") or [],
            "knowledge_refs": proposal.get("knowledge_refs", []),
            "payload": proposal.get("payload"),
        } if proposal else None,
        "execution": {
            "object_ref": receipt.get("object_ref") or readback.get("object_ref"),
            "receipt_state": receipt.get("state"),
            "readback_state": readback.get("state"),
            "readback_digest": readback.get("proposal_digest"),
            "screenshot_ref": readback.get("screenshot_ref"),
            "resource_version_ref": readback.get("resource_version_ref"),
            "observed_at": readback.get("observed_at"),
        },
        "available_actions": {
            "can_submit": status in {"needs_pre_submit_review", "ready_to_submit"} and bool(proposal.get("digest")),
            "can_accept": status == "needs_clinician_acceptance",
            "can_revise": status in {"needs_pre_submit_review", "ready_to_submit", "needs_clinician_acceptance", "draft_only"},
            "can_cancel": status not in {"accepted", "cancelled", "rejected"},
        },
    }


def _safe_json_for_script(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_review_app_html(
    *,
    initial_state: Mapping[str, Any] | None = None,
    api_base: str = "",
    auth_token: str = "",
) -> str:
    """Render the self-contained OPL MiniApp / loopback review HTML with XSS-safe injection."""
    template = _ASSET_PATH.read_text(encoding="utf-8")
    bootstrap = {
        "apiBase": api_base,
        "authToken": auth_token,
        "initialCard": build_review_card_model(initial_state) if initial_state else None,
    }
    safe_json = _safe_json_for_script(bootstrap)
    title_suffix = ""
    if initial_state and (initial_state.get("task_id") or (initial_state.get("request") or {}).get("capability_id")):
        tid = escape(str(initial_state.get("task_id") or ""))
        cap = escape(str((initial_state.get("request") or {}).get("capability_id") or ""))
        title_suffix = f" · {cap} ({tid})"
    rendered = template.replace("__MAP_TITLE_SUFFIX__", title_suffix)
    rendered = rendered.replace("__MAP_BOOTSTRAP_JSON__", safe_json)
    return rendered
