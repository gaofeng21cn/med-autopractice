"""Declarative REST/JSON hospital middleware ClinicalSystem adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from med_autopractice.domain.task import (
    ExecutionReceipt, Fact, Proposal, Snapshot, SourceCoverage, SubjectRef, SystemReadback, Task,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _extract_dotted(obj: Any, path: str, default: Any = None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, Mapping):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
        if cur is None:
            return default
    return cur


class RestMiddlewareAdapter:
    """ClinicalSystem adapter configured by endpoint mappings for hospital ESB / REST gateways."""

    def __init__(
        self,
        base_url: str,
        site_id: str,
        endpoints: Mapping[str, Any],
        *,
        token_provider: Callable[[], str] | None = None,
        write_capabilities: tuple[str, ...] | frozenset[str] = (
            "write_inpatient_notes", "revise_orders", "prepare_admission_orders",
        ),
        required_scopes: tuple[str, ...] = ("patient_context", "results", "current_orders", "nursing"),
        timeout: float = 10.0,
    ):
        parsed = urlsplit(base_url)
        loopback = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not site_id or not parsed.hostname or parsed.username or parsed.password or not (parsed.scheme == "https" or loopback):
            raise ValueError("RestMiddlewareAdapter requires site_id and HTTPS (or loopback HTTP) base URL")
        self.base_url = base_url.rstrip("/")
        self.site_id = site_id
        self.endpoints = dict(endpoints)
        self.token_provider = token_provider
        self.write_capabilities = frozenset(write_capabilities)
        self.required_scopes = tuple(required_scopes)
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _request_json(self, method: str, path: str, *, body: dict | None = None, headers: dict[str, str] | None = None) -> dict:
        url = path if path.startswith(self.base_url) else f"{self.base_url}/{path.lstrip('/')}"
        req_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if self.token_provider:
            req_headers["Authorization"] = "Bearer " + self.token_provider()
        req = Request(url, data=data, headers=req_headers, method=method)
        with self._opener.open(req, timeout=self.timeout) as resp:
            obj = json.load(resp)
        if not isinstance(obj, dict):
            raise ValueError("middleware JSON response must be an object")
        return obj

    def _render_path(self, template: str, **values: str) -> str:
        safe_vals = {k: quote(str(v), safe="") for k, v in values.items()}
        return template.format(**safe_vals)

    def read_context(self, subject: SubjectRef, capability_id: str) -> Snapshot:
        if subject.site_id != self.site_id:
            raise ValueError("subject belongs to another site")
        cfg = self.endpoints.get("context") or {}
        path = self._render_path(
            str(cfg.get("path", "/api/context/{patient_id}/{encounter_id}")),
            patient_id=subject.patient_id,
            encounter_id=subject.encounter_id,
            capability_id=capability_id,
        )
        raw = self._request_json(str(cfg.get("method", "GET")), path)
        if raw.get("patient_id") != subject.patient_id or raw.get("encounter_id") != subject.encounter_id:
            raise ValueError("middleware context patient or encounter identity differs")
        items = _extract_dotted(raw, str(cfg.get("facts_path", "facts")), [])
        if not isinstance(items, list):
            raise ValueError("middleware facts must be a JSON list")

        facts: list[Fact] = []
        for idx, row in enumerate(items):
            if not isinstance(row, dict):
                continue
            facts.append(Fact(
                fact_id=str(row.get("fact_id") or f"rest-{idx}"),
                value=str(row.get("value") or ""),
                source_ref=str(row.get("source_ref") or f"{self.base_url}{path}#{idx}"),
                source_system=str(row.get("source_system") or "hospital-rest"),
                source_object_ref=str(row.get("object_ref") or ""),
                effective_at=row.get("effective_at"),
                recorded_at=row.get("recorded_at"),
                unit=row.get("unit"),
                status=str(row.get("status") or "observed"),
                details=dict(row.get("details") or {}),
            ))
        rev_raw = str(raw.get("revision") or sha256(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()[:16])
        cov = tuple(SourceCoverage("hospital-rest", scope, True) for scope in self.required_scopes)
        return Snapshot(subject, rev_raw, tuple(facts), (f"{self.base_url}{path}",), cov)

    def can_apply(self, proposal: Proposal) -> bool:
        return proposal.subject.site_id == self.site_id and proposal.capability_id in self.write_capabilities

    def apply(self, proposal: Proposal, idempotency_key: str, existing_object_ref: str | None) -> ExecutionReceipt:
        if not self.can_apply(proposal):
            raise PermissionError("RestMiddlewareAdapter rejected foreign site or disabled capability")
        cfg = self.endpoints.get("apply") or {}
        path = self._render_path(
            str(cfg.get("path", "/api/submissions")),
            patient_id=proposal.subject.patient_id,
            encounter_id=proposal.subject.encounter_id,
            capability_id=proposal.capability_id,
        )
        body = {
            "site_id": self.site_id,
            "patient_id": proposal.subject.patient_id,
            "encounter_id": proposal.subject.encounter_id,
            "capability_id": proposal.capability_id,
            "idempotency_key": idempotency_key,
            "existing_object_ref": existing_object_ref,
            "proposal_digest": proposal.digest,
            "payload": proposal.payload,
        }
        resp = self._request_json(
            str(cfg.get("method", "POST")),
            path,
            body=body,
            headers={"Idempotency-Key": idempotency_key},
        )
        obj_ref = str(resp.get("object_ref") or "")
        state = str(resp.get("state") or "")
        version_ref = str(resp.get("version_ref") or obj_ref)
        if not obj_ref or not state:
            raise ValueError("middleware apply response missing object_ref or state")
        return ExecutionReceipt(obj_ref, state, (version_ref,))

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback:
        if task.proposal is None:
            raise ValueError("read_back requires a task proposal")
        cfg = self.endpoints.get("readback") or {}
        path = self._render_path(
            str(cfg.get("path", "/api/submissions/by-ref?object_ref={object_ref}")),
            object_ref=receipt.object_ref,
        )
        raw = self._request_json("GET", path)
        pat_id = str(raw.get("patient_id") or "")
        enc_id = str(raw.get("encounter_id") or "")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else None
        digest = ""
        if (pat_id == task.request.subject.patient_id and enc_id == task.request.subject.encounter_id
                and payload is not None
                and json.dumps(payload, ensure_ascii=False, sort_keys=True)
                == json.dumps(task.proposal.payload, ensure_ascii=False, sort_keys=True)):
            digest = task.proposal.digest
        version_ref = str(raw.get("version_ref") or receipt.object_ref)
        observed_at = str(raw.get("observed_at") or datetime.now(timezone.utc).isoformat())
        obs_subj = task.request.subject if (pat_id == task.request.subject.patient_id and enc_id == task.request.subject.encounter_id) else SubjectRef(self.site_id, pat_id, enc_id)
        return SystemReadback(
            subject=obs_subj,
            object_ref=str(raw.get("object_ref") or receipt.object_ref),
            proposal_digest=digest,
            state=str(raw.get("state") or ""),
            screenshot_ref=None,
            trace_refs=(version_ref,),
            visible_payload=payload,
            observed_at=observed_at,
            resource_version_ref=version_ref,
        )

    def find_submission(self, task: Task) -> ExecutionReceipt | None:
        if not task.idempotency_key:
            return None
        cfg = self.endpoints.get("find") or {}
        path = self._render_path(
            str(cfg.get("path", "/api/submissions/by-key/{idempotency_key}")),
            idempotency_key=task.idempotency_key,
        )
        try:
            raw = self._request_json("GET", path)
        except Exception:
            return None
        if not raw.get("object_ref") or not raw.get("state"):
            return None
        version_ref = str(raw.get("version_ref") or raw["object_ref"])
        return ExecutionReceipt(str(raw["object_ref"]), str(raw["state"]), (version_ref,))
