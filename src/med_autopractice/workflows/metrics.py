"""Zero-PHI operational metrics aggregated from a site SQLite TaskStore."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_site_metrics(store_path: Path) -> dict[str, Any]:
    """Compute strictly PHI-free workflow execution and quality metrics from TaskStore SQLite."""
    db_path = store_path.expanduser().resolve()
    if not db_path.is_file():
        raise ValueError(f"TaskStore SQLite file does not exist: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT body FROM tasks").fetchall()
    finally:
        conn.close()

    total = len(rows)
    status_counts: Counter[str] = Counter()
    capability_counts: Counter[str] = Counter()
    generator_counts: Counter[str] = Counter()
    refreshed_tasks = 0
    accepted_total = 0
    first_pass_accepted = 0
    durations: list[float] = []

    for (body_str,) in rows:
        doc = json.loads(body_str)
        status = str(doc.get("status") or "unknown")
        status_counts[status] += 1

        req = doc.get("request") or {}
        capability_counts[str(req.get("capability_id") or "unknown")] += 1

        proposal = doc.get("proposal") or {}
        if proposal.get("generator_ref"):
            gen = str(proposal["generator_ref"]).split("://", 1)[0]
            generator_counts[gen] += 1

        events = doc.get("events") or []
        actions = {str(e.get("action") or "") for e in events}
        outcomes = {str(e.get("outcome") or "") for e in events}
        if "refresh" in actions or "needs_refresh" in outcomes:
            refreshed_tasks += 1

        if status == "accepted":
            accepted_total += 1
            reviews = doc.get("reviews") or []
            had_reject = any(not bool(r.get("accepted")) for r in reviews)
            had_revise = "revise_proposal" in actions or "revise_inputs" in actions
            if not had_reject and not had_revise:
                first_pass_accepted += 1

        times = [dt for dt in (_parse_iso(e.get("at_utc")) for e in events) if dt is not None]
        if len(times) >= 2:
            durations.append(max(0.0, (max(times) - min(times)).total_seconds()))

    draft_only_count = status_counts.get("draft_only", 0)
    cancelled_count = status_counts.get("cancelled", 0)
    return {
        "schema_version": "map-site-metrics.v1",
        "total_tasks": total,
        "status_counts": dict(sorted(status_counts.items())),
        "capability_counts": dict(sorted(capability_counts.items())),
        "generator_scheme_counts": dict(sorted(generator_counts.items())),
        "draft_only_ratio": round(draft_only_count / total, 4) if total else 0.0,
        "first_pass_acceptance_rate": round(first_pass_accepted / accepted_total, 4) if accepted_total else 0.0,
        "refresh_rate": round(refreshed_tasks / total, 4) if total else 0.0,
        "cancel_rate": round(cancelled_count / total, 4) if total else 0.0,
        "median_duration_seconds": round(float(median(durations)), 3) if durations else 0.0,
    }
