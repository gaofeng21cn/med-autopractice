"""Read a single quantitative FHIR R4 Observation for a bound encounter."""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
import json
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from med_autopractice.domain.task import Fact, SubjectRef


def _resource_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", value) or value in {".", ".."}:
        raise ValueError("expected a FHIR resource ID")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FHIRObservationSource:
    """The deployment supplies the connection, site ID and optional access token provider."""

    def __init__(self, base_url: str, site_id: str, *,
                 access_token: Callable[[], str] | None = None, timeout: float = 10):
        parsed = urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not site_id or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or not (parsed.scheme == "https" or local_http)):
            raise ValueError("expected a site ID and a FHIR HTTPS base (loopback HTTP for a local demo)")
        self.base_url = base_url.rstrip("/")
        self.site_id = site_id
        self.access_token = access_token
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _get(self, kind: str, resource_id: str) -> dict:
        url = f"{self.base_url}/{kind}/{_resource_id(resource_id)}"
        headers = {"Accept": "application/fhir+json"}
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token()
        request = Request(url, headers=headers, method="GET")
        with self._opener.open(request, timeout=self.timeout) as response:
            resource = json.load(response, parse_float=Decimal)
        if not isinstance(resource, dict) or resource.get("resourceType") != kind or resource.get("id") != resource_id:
            raise ValueError("FHIR resource identity differs from the request")
        return resource

    def _bound(self, reference: dict, expected: str) -> bool:
        if not isinstance(reference, dict):
            return False
        return reference.get("reference") in (expected, f"{self.base_url}/{expected}")

    def read_quantity(self, subject: SubjectRef, observation_id: str) -> Fact:
        """Read a specified scalar result; IDs use this endpoint's FHIR namespace."""
        if subject.site_id != self.site_id:
            raise ValueError("subject belongs to another site")
        patient = f"Patient/{_resource_id(subject.patient_id)}"
        encounter = self._get("Encounter", subject.encounter_id)
        if not self._bound(encounter.get("subject"), patient) or encounter.get("status") == "entered-in-error":
            raise ValueError("encounter is invalid or belongs to another patient")
        result = self._get("Observation", observation_id)
        if (not self._bound(result.get("subject"), patient)
                or not self._bound(result.get("encounter"), f"Encounter/{subject.encounter_id}")):
            raise ValueError("observation patient or encounter differs")
        if result.get("status") not in {"final", "amended", "corrected", "preliminary"}:
            raise ValueError("observation has no usable result status")
        quantity = result.get("valueQuantity") or {}
        value = quantity.get("value")
        code = result.get("code") or {}
        codings = code.get("coding") or []
        label = code.get("text") or next((c.get("display") or c["code"] for c in codings if c.get("code")), None)
        if type(value) not in {Decimal, int} or not label or result.get("component") or result.get("modifierExtension"):
            raise ValueError("expected a named scalar valueQuantity observation")
        object_ref = f"{self.base_url}/Observation/{observation_id}"
        meta = result.get("meta") or {}
        version = meta.get("versionId")
        source_ref = f"{object_ref}/_history/{_resource_id(version)}" if version else object_ref
        if quantity.get("comparator") not in {None, "<", "<=", ">=", ">"}:
            raise ValueError("unknown quantity comparator")
        return Fact(
            fact_id=f"Observation/{observation_id}", value=f"{label} {quantity.get('comparator', '')}{value}",
            unit=quantity.get("unit") or quantity.get("code"), source_ref=source_ref,
            source_object_ref=object_ref, source_system="FHIR R4",
            effective_at=result.get("effectiveDateTime") or result.get("effectiveInstant"),
            recorded_at=result.get("issued"), status="uncertain" if result["status"] == "preliminary" else "observed",
        )


_MAP_PAYLOAD_EXT_URL = "https://med-auto-practice.local/fhir/StructureDefinition/map-payload"
_IDEMPOTENCY_SYSTEM = "https://med-auto-practice.local/idempotency-key"


def _encode_b64_json(payload: dict) -> str:
    import base64

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _decode_b64_json(encoded: str) -> dict | None:
    import base64

    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


class FHIRClinicalSystem:
    """Full FHIR R4 ClinicalSystem implementation (Encounter/Observation/MedicationRequest/DocumentReference)."""

    def __init__(
        self,
        base_url: str,
        site_id: str,
        *,
        access_token: Callable[[], str] | None = None,
        timeout: float = 10.0,
        write_capabilities: tuple[str, ...] | frozenset[str] = (
            "write_inpatient_notes",
            "write_outpatient_notes",
            "prepare_admission_orders",
            "revise_orders",
            "prepare_discharge_medication",
        ),
        required_scopes: tuple[str, ...] = ("patient_context", "results", "current_orders", "nursing"),
    ):
        parsed = urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not site_id or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or not (parsed.scheme == "https" or local_http)):
            raise ValueError("expected a site ID and a FHIR HTTPS base (loopback HTTP for a local demo)")
        self.base_url = base_url.rstrip("/")
        self.site_id = site_id
        self.access_token = access_token
        self.timeout = timeout
        self.write_capabilities = frozenset(write_capabilities)
        self.required_scopes = tuple(required_scopes)
        self._opener = build_opener(_NoRedirect())

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/fhir+json"}
        if write:
            headers["Content-Type"] = "application/fhir+json; charset=utf-8"
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token()
        return headers

    def _request_json(self, method: str, path_or_url: str, *, body: dict | None = None) -> dict:
        url = path_or_url if path_or_url.startswith(self.base_url) else f"{self.base_url}/{path_or_url.lstrip('/')}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(url, data=data, headers=self._headers(write=body is not None), method=method)
        with self._opener.open(req, timeout=self.timeout) as resp:
            return json.load(resp, parse_float=Decimal)

    def _bound(self, reference: dict | None, expected: str) -> bool:
        if not isinstance(reference, dict):
            return False
        return reference.get("reference") in (expected, f"{self.base_url}/{expected}")

    @staticmethod
    def _version_ref(base: str, rtype: str, rid: str, resource: dict) -> str:
        meta = resource.get("meta") or {}
        vid = meta.get("versionId")
        obj = f"{base}/{rtype}/{rid}"
        return f"{obj}/_history/{_resource_id(str(vid))}" if vid else obj

    def _bundle_entries(self, bundle: dict) -> list[dict]:
        entries = bundle.get("entry")
        if not isinstance(entries, list):
            return []
        out = []
        for item in entries:
            res = item.get("resource") if isinstance(item, dict) else None
            if isinstance(res, dict):
                out.append(res)
        return out

    def read_context(self, subject: SubjectRef, capability_id: str):
        from hashlib import sha256
        from med_autopractice.domain.task import Snapshot, SourceCoverage

        if subject.site_id != self.site_id:
            raise ValueError("subject belongs to another site")
        pat_ref = f"Patient/{_resource_id(subject.patient_id)}"
        enc_id = _resource_id(subject.encounter_id)
        enc_ref = f"Encounter/{enc_id}"

        encounter = self._request_json("GET", f"Encounter/{enc_id}")
        if (encounter.get("resourceType") != "Encounter" or encounter.get("id") != enc_id
                or not self._bound(encounter.get("subject"), pat_ref)
                or encounter.get("status") == "entered-in-error"):
            raise ValueError("encounter is invalid or belongs to another patient")

        facts: list[Fact] = []
        traces: list[str] = [self._version_ref(self.base_url, "Encounter", enc_id, encounter)]

        # 1. Observations (labs / vitals)
        obs_bundle = self._request_json("GET", f"Observation?patient={subject.patient_id}&encounter={enc_id}")
        for obs in self._bundle_entries(obs_bundle):
            if obs.get("resourceType") != "Observation" or not obs.get("id"):
                continue
            if not self._bound(obs.get("subject"), pat_ref) or not self._bound(obs.get("encounter"), enc_ref):
                continue
            status = obs.get("status")
            if status not in {"final", "amended", "corrected", "preliminary"}:
                continue
            oid = _resource_id(str(obs["id"]))
            obj_ref = f"{self.base_url}/Observation/{oid}"
            src_ref = self._version_ref(self.base_url, "Observation", oid, obs)
            code = obs.get("code") or {}
            label = code.get("text") or next(
                (c.get("display") or c.get("code") for c in (code.get("coding") or []) if c.get("code")),
                oid,
            )
            qty = obs.get("valueQuantity") or {}
            if qty.get("value") is not None:
                val = f"{label} {qty.get('comparator', '')}{qty.get('value')}"
                unit = qty.get("unit") or qty.get("code")
            else:
                val = f"{label}: {obs.get('valueString', '')}".strip()
                unit = None
            facts.append(Fact(
                fact_id=f"Observation/{oid}",
                value=val,
                source_ref=src_ref,
                source_system="FHIR R4",
                source_object_ref=obj_ref,
                effective_at=obs.get("effectiveDateTime") or obs.get("effectiveInstant"),
                recorded_at=obs.get("issued"),
                unit=unit,
                status="uncertain" if status == "preliminary" else "observed",
                details={"section": "观察", "kind": "observation"},
            ))
        traces.append(f"{self.base_url}/Observation?encounter={enc_id}")

        # 2. MedicationRequests (active orders)
        med_bundle = self._request_json("GET", f"MedicationRequest?patient={subject.patient_id}&encounter={enc_id}")
        for med in self._bundle_entries(med_bundle):
            if med.get("resourceType") != "MedicationRequest" or not med.get("id"):
                continue
            if not self._bound(med.get("subject"), pat_ref):
                continue
            mid = _resource_id(str(med["id"]))
            obj_ref = f"{self.base_url}/MedicationRequest/{mid}"
            src_ref = self._version_ref(self.base_url, "MedicationRequest", mid, med)
            med_code = ((med.get("medicationCodeableConcept") or {}).get("coding") or [{}])[0].get("code") or ""
            med_text = (med.get("medicationCodeableConcept") or {}).get("text") or med_code or mid
            facts.append(Fact(
                fact_id=f"MedicationRequest/{mid}",
                value=str(med_text),
                source_ref=src_ref,
                source_system="FHIR R4",
                source_object_ref=obj_ref,
                recorded_at=med.get("authoredOn"),
                status="observed" if med.get("status") in {"active", "completed"} else "uncertain",
                details={"kind": "current_order", "code": str(med_code or med_text)},
            ))
        traces.append(f"{self.base_url}/MedicationRequest?encounter={enc_id}")

        # 3. DocumentReferences (existing notes / nursing context)
        doc_bundle = self._request_json("GET", f"DocumentReference?patient={subject.patient_id}&encounter={enc_id}")
        for doc in self._bundle_entries(doc_bundle):
            if doc.get("resourceType") != "DocumentReference" or not doc.get("id"):
                continue
            if not self._bound(doc.get("subject"), pat_ref):
                continue
            did = _resource_id(str(doc["id"]))
            obj_ref = f"{self.base_url}/DocumentReference/{did}"
            src_ref = self._version_ref(self.base_url, "DocumentReference", did, doc)
            facts.append(Fact(
                fact_id=f"DocumentReference/{did}",
                value=str(doc.get("description") or f"FHIR Document {did}"),
                source_ref=src_ref,
                source_system="FHIR R4",
                source_object_ref=obj_ref,
                recorded_at=doc.get("date"),
                status="observed",
                details={"section": "观察", "kind": "document"},
            ))
        traces.append(f"{self.base_url}/DocumentReference?encounter={enc_id}")

        cov = tuple(SourceCoverage("FHIR R4", scope, True) for scope in self.required_scopes)
        rev_src = json.dumps([(f.fact_id, f.source_ref, f.value) for f in facts], ensure_ascii=False, sort_keys=True)
        revision = "fhir-" + sha256(f"{enc_id}:{rev_src}".encode("utf-8")).hexdigest()[:16]
        return Snapshot(subject, revision, tuple(facts), tuple(traces), cov)

    def can_apply(self, proposal) -> bool:
        return proposal.subject.site_id == self.site_id and proposal.capability_id in self.write_capabilities

    @staticmethod
    def _resource_type_for(capability_id: str, payload: dict) -> str:
        if payload.get("type") == "note_draft" or capability_id in {"write_inpatient_notes", "write_outpatient_notes"}:
            return "DocumentReference"
        return "MedicationRequest"

    def apply(self, proposal, idempotency_key: str, existing_object_ref: str | None):
        from med_autopractice.domain.task import ExecutionReceipt

        if not self.can_apply(proposal):
            raise PermissionError("FHIRClinicalSystem rejected unconfigured capability or foreign site")
        pat_ref = f"Patient/{_resource_id(proposal.subject.patient_id)}"
        enc_ref = f"Encounter/{_resource_id(proposal.subject.encounter_id)}"
        rtype = self._resource_type_for(proposal.capability_id, proposal.payload)

        if rtype == "DocumentReference":
            resource = {
                "resourceType": "DocumentReference",
                "status": "current",
                "docStatus": "preliminary",
                "subject": {"reference": pat_ref},
                "context": {"encounter": [{"reference": enc_ref}]},
                "identifier": [{"system": _IDEMPOTENCY_SYSTEM, "value": idempotency_key}],
                "description": f"MAP {proposal.capability_id} ({proposal.digest[:12]})",
                "content": [{
                    "attachment": {
                        "contentType": "application/json",
                        "data": _encode_b64_json(proposal.payload),
                    }
                }],
            }
            expected_state = "saved_draft"
        else:
            resource = {
                "resourceType": "MedicationRequest",
                "status": "draft",
                "intent": "proposal",
                "subject": {"reference": pat_ref},
                "encounter": {"reference": enc_ref},
                "identifier": [{"system": _IDEMPOTENCY_SYSTEM, "value": idempotency_key}],
                "extension": [{"url": _MAP_PAYLOAD_EXT_URL, "valueString": json.dumps(proposal.payload, ensure_ascii=False, sort_keys=True)}],
            }
            expected_state = "entered"

        method = "PUT" if existing_object_ref else "POST"
        target = existing_object_ref if existing_object_ref else rtype
        saved = self._request_json(method, target, body=resource)
        if saved.get("resourceType") != rtype or not saved.get("id"):
            raise ValueError("FHIR server returned unexpected resource payload on write")
        rid = _resource_id(str(saved["id"]))
        object_ref = f"{self.base_url}/{rtype}/{rid}"
        version_ref = self._version_ref(self.base_url, rtype, rid, saved)
        return ExecutionReceipt(object_ref, expected_state, (version_ref,))

    def _extract_visible_payload(self, rtype: str, res: dict) -> tuple[dict | None, str]:
        if rtype == "DocumentReference":
            contents = res.get("content") or []
            payload = None
            if contents and isinstance(contents[0], dict):
                att = contents[0].get("attachment") or {}
                if isinstance(att.get("data"), str):
                    payload = _decode_b64_json(att["data"])
            doc_status = res.get("docStatus") or res.get("status")
            state = "saved_draft" if doc_status == "preliminary" else str(doc_status or "")
            return payload, state

        payload = None
        for ext in res.get("extension") or []:
            if isinstance(ext, dict) and ext.get("url") == _MAP_PAYLOAD_EXT_URL and isinstance(ext.get("valueString"), str):
                parsed = json.loads(ext["valueString"])
                if isinstance(parsed, dict):
                    payload = parsed
                    break
        status = res.get("status")
        state = "entered" if status == "draft" else str(status or "")
        return payload, state

    def read_back(self, receipt, task):
        from datetime import datetime, timezone
        from med_autopractice.domain.task import SystemReadback

        if task.proposal is None:
            raise ValueError("read_back requires a task proposal")
        pat_ref = f"Patient/{_resource_id(task.request.subject.patient_id)}"
        enc_ref = f"Encounter/{_resource_id(task.request.subject.encounter_id)}"
        res = self._request_json("GET", receipt.object_ref)
        rtype = str(res.get("resourceType") or "")
        rid = _resource_id(str(res.get("id") or ""))

        subject_ok = self._bound(res.get("subject"), pat_ref)
        if rtype == "DocumentReference":
            encs = (res.get("context") or {}).get("encounter") or []
            enc_ok = any(self._bound(e, enc_ref) for e in encs)
        else:
            enc_ok = self._bound(res.get("encounter"), enc_ref)

        visible_payload, mapped_state = self._extract_visible_payload(rtype, res)
        digest = ""
        if (subject_ok and enc_ok and visible_payload is not None
                and json.dumps(visible_payload, ensure_ascii=False, sort_keys=True)
                == json.dumps(task.proposal.payload, ensure_ascii=False, sort_keys=True)):
            digest = task.proposal.digest

        version_ref = self._version_ref(self.base_url, rtype, rid, res)
        observed_at = str((res.get("meta") or {}).get("lastUpdated") or datetime.now(timezone.utc).isoformat())
        observed_subject = task.request.subject if (subject_ok and enc_ok) else SubjectRef(self.site_id, "mismatch", "mismatch")
        return SystemReadback(
            subject=observed_subject,
            object_ref=f"{self.base_url}/{rtype}/{rid}",
            proposal_digest=digest,
            state=mapped_state,
            screenshot_ref=None,
            trace_refs=(version_ref,),
            visible_payload=visible_payload,
            observed_at=observed_at,
            resource_version_ref=version_ref,
        )

    def find_submission(self, task):
        from urllib.parse import quote
        from med_autopractice.domain.task import ExecutionReceipt

        if task.proposal is None or not task.idempotency_key:
            return None
        rtype = self._resource_type_for(task.request.capability_id, task.proposal.payload)
        token_param = quote(f"{_IDEMPOTENCY_SYSTEM}|{task.idempotency_key}", safe="")
        bundle = self._request_json("GET", f"{rtype}?identifier={token_param}")
        entries = self._bundle_entries(bundle)
        if len(entries) != 1 or not entries[0].get("id"):
            return None
        res = entries[0]
        rid = _resource_id(str(res["id"]))
        _, state = self._extract_visible_payload(rtype, res)
        obj_ref = f"{self.base_url}/{rtype}/{rid}"
        version_ref = self._version_ref(self.base_url, rtype, rid, res)
        return ExecutionReceipt(obj_ref, state, (version_ref,))
