"""Declarative HospitalUI implementation powered by PlaybookRunner and UIDriver."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from typing import Any

from med_autopractice.adapters.playbook_runner import PlaybookRunner, UIDriver
from med_autopractice.adapters.ui import HospitalUI, UIProfile, UIStateError, VisibleObject
from med_autopractice.domain.task import (
    ExecutionReceipt, Fact, Proposal, Snapshot, SourceCoverage, SubjectRef, Task,
)
from med_autopractice.medicine.proposals import NOTES
from med_autopractice.workflows.site_playbook import Playbook


def _flatten_proposal_params(proposal: Proposal, existing_object_ref: str | None) -> dict[str, Any]:
    payload = proposal.payload
    params: dict[str, Any] = {
        "proposal_digest": proposal.digest,
        "capability_id": proposal.capability_id,
        "existing_object_ref": existing_object_ref or "",
    }
    if payload.get("type") == "note_draft":
        parts: list[str] = []
        for section in payload.get("sections", []):
            name = section.get("section", "")
            lines = [str(entry.get("text", "")) for entry in section.get("entries", []) if entry.get("text")]
            joined = "\n".join(lines)
            params[f"section_{name}"] = joined
            parts.append(f"[{name}]\n{joined}")
        params["draft_text"] = "\n\n".join(parts)
    elif "draft" in payload:
        params["draft_text"] = str(payload["draft"])
    elif "changes" in payload:
        params["changes_json"] = json.dumps(payload["changes"], ensure_ascii=False)
        params["changes_text"] = "\n".join(
            f"{c.get('action')}:{c.get('code')} {c.get('dose', '')}{c.get('unit', '')} {c.get('route', '')} {c.get('frequency', '')}".strip()
            for c in payload.get("changes", [])
        )
    return params


class DeclarativeHospitalUI:
    """Implements HospitalUI purely from Playbook v1/v2 bindings without custom Python code."""

    def __init__(
        self,
        profile: UIProfile,
        driver: UIDriver,
        playbooks: Mapping[str, Playbook],
        bindings: Mapping[str, Mapping[str, Any]],
        *,
        runner: PlaybookRunner | None = None,
    ):
        self.profile = profile
        self.driver = driver
        self.runner = runner or PlaybookRunner(driver)
        self.playbooks = dict(playbooks)
        self.bindings = {k: dict(v) for k, v in bindings.items()}

    def _playbook(self, playbook_id: str) -> Playbook:
        try:
            return self.playbooks[playbook_id]
        except KeyError as error:
            raise UIStateError(f"missing declarative playbook: {playbook_id}") from error

    def collect(self, subject: SubjectRef, capability_id: str) -> Snapshot:
        binding = self.bindings.get(capability_id) or {}
        collect_ids = binding.get("collect", ())
        if isinstance(collect_ids, str):
            collect_ids = (collect_ids,)
        if not collect_ids:
            raise UIStateError(f"no collect playbook configured for {capability_id}")

        facts: list[Fact] = []
        trace_refs: list[str] = []
        coverages: list[SourceCoverage] = []
        seen_fact_ids: set[str] = set()

        for pid in collect_ids:
            res = self.runner.run(self._playbook(pid), subject)
            for trace in res.traces:
                trace_refs.append(trace.trace_ref)
            for key, raw_value in res.extracted.items():
                scope = res.extracted_scopes.get(key, key)
                if isinstance(raw_value, dict) and "rows" in raw_value:
                    truncated = bool(raw_value.get("truncated"))
                    gaps = ("pagination_truncated",) if truncated else ()
                    coverages.append(SourceCoverage(self.profile.product, scope, not truncated, gaps))
                    for idx, row in enumerate(raw_value["rows"]):
                        fact_id = str(row.get("fact_id") or f"{key}:{idx}")
                        if fact_id in seen_fact_ids:
                            continue
                        seen_fact_ids.add(fact_id)
                        summary = str(
                            row.get("value")
                            or row.get("summary")
                            or json.dumps(row, ensure_ascii=False, sort_keys=True)
                        )
                        details = dict(row)
                        if "section" not in details and binding.get("default_section"):
                            details["section"] = binding["default_section"]
                        facts.append(Fact(
                            fact_id=fact_id,
                            value=summary,
                            source_ref=str(row.get("source_ref") or f"ui://{self.profile.product}/{key}/{idx}"),
                            source_system=self.profile.product,
                            source_object_ref=str(row.get("object_ref") or ""),
                            effective_at=row.get("effective_at"),
                            recorded_at=row.get("recorded_at"),
                            unit=row.get("unit"),
                            status=str(row.get("status") or "observed"),
                            details=details,
                        ))
                else:
                    text_val = raw_value if isinstance(raw_value, str) else json.dumps(raw_value, ensure_ascii=False)
                    fact_id = f"{key}"
                    if fact_id not in seen_fact_ids:
                        seen_fact_ids.add(fact_id)
                        details = {"section": binding.get("default_section", scope)} if isinstance(raw_value, str) else dict(raw_value)
                        facts.append(Fact(
                            fact_id=fact_id,
                            value=text_val,
                            source_ref=f"ui://{self.profile.product}/{key}",
                            source_system=self.profile.product,
                            details=details,
                        ))
                    coverages.append(SourceCoverage(self.profile.product, scope, True))

        for extra_scope in binding.get("extra_complete_scopes", ()):
            if not any(c.scope == extra_scope for c in coverages):
                coverages.append(SourceCoverage(self.profile.product, extra_scope, True))

        revision_body = json.dumps(
            [{"id": f.fact_id, "val": f.value, "status": f.status} for f in facts],
            ensure_ascii=False, sort_keys=True,
        )
        revision = "ui-" + sha256(revision_body.encode("utf-8")).hexdigest()[:16]
        return Snapshot(subject, revision, tuple(facts), tuple(trace_refs), tuple(coverages))

    def write(self, proposal: Proposal, existing_object_ref: str | None) -> ExecutionReceipt:
        binding = self.bindings.get(proposal.capability_id) or {}
        write_id = binding.get("write")
        if not write_id:
            raise UIStateError(f"no write playbook configured for {proposal.capability_id}")
        params = _flatten_proposal_params(proposal, existing_object_ref)
        res = self.runner.run(self._playbook(write_id), proposal.subject, params=params)
        object_ref = str(
            res.extracted.get("object_ref")
            or existing_object_ref
            or f"ui://{self.profile.site_id}/{proposal.capability_id}/{proposal.digest[:12]}"
        )
        default_state = "saved_draft" if proposal.capability_id in NOTES else "entered"
        state = str(res.extracted.get("state") or default_state)
        return ExecutionReceipt(object_ref, state, tuple(t.trace_ref for t in res.traces))

    def open_object(self, subject: SubjectRef, capability_id: str, object_ref: str) -> VisibleObject:
        binding = self.bindings.get(capability_id) or {}
        readback_id = binding.get("readback")
        if not readback_id:
            raise UIStateError(f"no readback playbook configured for {capability_id}")
        res = self.runner.run(self._playbook(readback_id), subject, params={"object_ref": object_ref})
        shot_ref = (
            res.screenshot_refs[-1]
            if res.screenshot_refs
            else self.driver.screenshot(label=f"readback-{capability_id}")
        )
        payload = res.extracted.get("visible_payload")
        if not isinstance(payload, dict):
            payload = {"draft_text": res.extracted.get("draft_text", "")}
        default_state = "saved_draft" if capability_id in NOTES else "entered"
        state = str(res.extracted.get("state") or default_state)
        return VisibleObject(
            subject=subject,
            object_ref=str(res.extracted.get("object_ref") or object_ref),
            state=state,
            payload=payload,
            screenshot_ref=shot_ref,
            trace_refs=tuple(t.trace_ref for t in res.traces),
            observed_at=res.observed_at,
        )

    def find_candidates(self, task: Task) -> tuple[VisibleObject, ...]:
        binding = self.bindings.get(task.request.capability_id) or {}
        find_id = binding.get("find")
        if not find_id or task.proposal is None:
            return ()
        res = self.runner.run(
            self._playbook(find_id),
            task.request.subject,
            params={"proposal_digest": task.proposal.digest},
        )
        raw_candidates = res.extracted.get("candidates")
        if not isinstance(raw_candidates, list):
            return ()
        shot_ref = res.screenshot_refs[-1] if res.screenshot_refs else self.driver.screenshot(label="reconcile")
        traces = tuple(t.trace_ref for t in res.traces)
        items: list[VisibleObject] = []
        for cand in raw_candidates:
            if not isinstance(cand, dict) or not cand.get("object_ref"):
                continue
            items.append(VisibleObject(
                subject=task.request.subject,
                object_ref=str(cand["object_ref"]),
                state=str(cand.get("state") or "saved_draft"),
                payload=dict(cand.get("payload") or {}),
                screenshot_ref=str(cand.get("screenshot_ref") or shot_ref),
                trace_refs=traces,
                observed_at=res.observed_at,
            ))
        return tuple(items)
