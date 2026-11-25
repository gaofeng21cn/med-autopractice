"""Declarative HospitalUI implementation powered by PlaybookRunner and UIDriver."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from typing import Any

from med_autopractice.adapters.playbook_runner import PlaybookExecutionResult, PlaybookRunner, UIDriver
from med_autopractice.adapters.ui import HospitalUI, UIProfile, UIStateError, VisibleObject
from med_autopractice.domain.task import (
    ExecutionReceipt, Fact, Proposal, Snapshot, SourceCoverage, SubjectRef, Task,
)
from med_autopractice.medicine.proposals import ORDERS
from med_autopractice.workflows.site_playbook import Playbook

_CANONICAL_STATES = frozenset({"saved_draft", "entered", "active", "stopped", "cancelled", "draft"})


def _normalize_state(raw: Any, state_map: Mapping[str, str] | None = None) -> str:
    if raw is None:
        return ""
    text_val = str(raw).strip()
    if not text_val:
        return ""
    if state_map:
        if text_val in state_map:
            return str(state_map[text_val]).strip()
        for key, mapped in sorted(state_map.items(), key=lambda kv: len(str(kv[0])), reverse=True):
            if key and str(key) in text_val:
                return str(mapped).strip()
    if text_val in _CANONICAL_STATES:
        return text_val
    return ""


def _resolve_observed_subject(
    res: PlaybookExecutionResult,
    expected: SubjectRef,
    *,
    strict: bool,
) -> SubjectRef:
    obs_pat = res.extracted.get("observed_patient_id")
    obs_enc = res.extracted.get("observed_encounter_id")
    if isinstance(obs_pat, str) and obs_pat.strip() and isinstance(obs_enc, str) and obs_enc.strip():
        observed = SubjectRef(expected.site_id, obs_pat.strip(), obs_enc.strip())
    elif res.observed_subject is not None:
        observed = res.observed_subject
    else:
        if strict:
            raise UIStateError("playbook did not verify observed patient and encounter identity")
        return SubjectRef(expected.site_id, "__unverified__", "__unverified__")
    if strict and observed != expected:
        raise UIStateError(f"observed subject {observed} differs from expected {expected}")
    return observed


def _normalize_order_rows(raw_table: Mapping[str, Any], binding: Mapping[str, Any]) -> list[dict[str, Any]]:
    col_map = dict(binding.get("order_column_map") or {})
    status_map = dict(binding.get("order_status_map") or {})
    normalized_rows: list[dict[str, Any]] = []
    for raw_row in raw_table.get("rows", []):
        if not isinstance(raw_row, Mapping):
            continue
        row: dict[str, Any] = {}
        for key, val in raw_row.items():
            mapped_key = str(col_map.get(key, key))
            row[mapped_key] = val
        if not row.get("object_ref") and row.get("target_ref"):
            row["object_ref"] = str(row["target_ref"])
        if "status" in row and row["status"] is not None:
            raw_status = str(row["status"]).strip()
            mapped_status = status_map.get(raw_status)
            if mapped_status is None:
                for key, val in sorted(status_map.items(), key=lambda kv: len(str(kv[0])), reverse=True):
                    if key and str(key) in raw_status:
                        mapped_status = str(val)
                        break
            row["status"] = (mapped_status or raw_status).strip().lower()
        normalized_rows.append(row)
    return normalized_rows


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

    def _playbook(self, playbook_id: str, *, read_only: bool = False) -> Playbook:
        try:
            playbook = self.playbooks[playbook_id]
        except KeyError as error:
            raise UIStateError(f"missing declarative playbook: {playbook_id}") from error
        if read_only and playbook.contains_write:
            raise UIStateError(f"read-only operation is bound to a write playbook: {playbook_id}")
        return playbook

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
            res = self.runner.run(self._playbook(pid, read_only=True), subject)
            _resolve_observed_subject(res, subject, strict=True)
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
        _resolve_observed_subject(res, proposal.subject, strict=True)
        object_ref = str(res.extracted.get("object_ref") or "").strip()
        if not object_ref:
            raise UIStateError("write playbook did not observe target object_ref")
        state = _normalize_state(res.extracted.get("state"), binding.get("state_map"))
        if not state:
            raise UIStateError("write playbook did not observe target object state")
        return ExecutionReceipt(object_ref, state, tuple(t.trace_ref for t in res.traces))

    def open_object(self, subject: SubjectRef, capability_id: str, object_ref: str) -> VisibleObject:
        binding = self.bindings.get(capability_id) or {}
        readback_id = binding.get("readback")
        if not readback_id:
            raise UIStateError(f"no readback playbook configured for {capability_id}")
        res = self.runner.run(self._playbook(readback_id, read_only=True), subject, params={"object_ref": object_ref})
        shot_ref = (
            res.screenshot_refs[-1]
            if res.screenshot_refs
            else self.driver.screenshot(label=f"readback-{capability_id}")
        )
        observed_subject = _resolve_observed_subject(res, subject, strict=False)
        observed_ref = str(res.extracted.get("object_ref") or "").strip()
        observed_state = _normalize_state(res.extracted.get("state"), binding.get("state_map"))

        payload = res.extracted.get("visible_payload")
        if isinstance(payload, dict):
            normalized_payload: dict[str, Any] = dict(payload)
        elif capability_id in ORDERS or capability_id == "maintain_order_sets":
            table_key = str(binding.get("order_table_key") or "orders_table")
            raw_table = res.extracted.get(table_key)
            if not (isinstance(raw_table, Mapping) and isinstance(raw_table.get("rows"), list)):
                raw_table = next(
                    (val for val in res.extracted.values() if isinstance(val, Mapping) and isinstance(val.get("rows"), list)),
                    None,
                )
            if isinstance(raw_table, Mapping):
                normalized_payload = {
                    "type": "observed_order_table",
                    "rows": _normalize_order_rows(raw_table, binding),
                }
            else:
                normalized_payload = {}
        elif "draft_text" in res.extracted:
            normalized_payload = {"draft_text": str(res.extracted.get("draft_text", ""))}
        else:
            normalized_payload = {}

        return VisibleObject(
            subject=observed_subject,
            object_ref=observed_ref,
            state=observed_state,
            payload=normalized_payload,
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
            self._playbook(find_id, read_only=True),
            task.request.subject,
            params={"proposal_digest": task.proposal.digest},
        )
        raw_candidates = res.extracted.get("candidates")
        if isinstance(raw_candidates, Mapping) and isinstance(raw_candidates.get("rows"), list):
            cand_rows = raw_candidates["rows"]
        elif isinstance(raw_candidates, list):
            cand_rows = raw_candidates
        else:
            return ()
        shot_ref = res.screenshot_refs[-1] if res.screenshot_refs else self.driver.screenshot(label="reconcile")
        traces = tuple(t.trace_ref for t in res.traces)
        default_obs_subject = _resolve_observed_subject(res, task.request.subject, strict=False)
        items: list[VisibleObject] = []
        for cand in cand_rows:
            if not isinstance(cand, dict) or not str(cand.get("object_ref") or "").strip():
                continue
            cand_pat = cand.get("patient_id") or (cand.get("subject") or {}).get("patient_id")
            cand_enc = cand.get("encounter_id") or (cand.get("subject") or {}).get("encounter_id")
            if isinstance(cand_pat, str) and cand_pat.strip() and isinstance(cand_enc, str) and cand_enc.strip():
                cand_subj = SubjectRef(task.request.subject.site_id, cand_pat.strip(), cand_enc.strip())
            else:
                cand_subj = default_obs_subject
            cand_state = _normalize_state(cand.get("state"), binding.get("state_map"))
            if not cand_state:
                continue
            raw_payload = cand.get("payload")
            if isinstance(raw_payload, dict):
                cand_payload = dict(raw_payload)
            elif "rows" in cand and isinstance(cand.get("rows"), list):
                cand_payload = {
                    "type": "observed_order_table",
                    "rows": _normalize_order_rows(cand, binding),
                }
            elif "draft_text" in cand:
                cand_payload = {"draft_text": str(cand.get("draft_text", ""))}
            else:
                cand_payload = {}
            items.append(VisibleObject(
                subject=cand_subj,
                object_ref=str(cand["object_ref"]).strip(),
                state=cand_state,
                payload=cand_payload,
                screenshot_ref=str(cand.get("screenshot_ref") or shot_ref),
                trace_refs=traces,
                observed_at=res.observed_at,
            ))
        return tuple(items)
