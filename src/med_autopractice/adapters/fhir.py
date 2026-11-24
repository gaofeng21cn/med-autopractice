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
_BATCH_IDEMPOTENCY_SYSTEM = "https://med-auto-practice.local/batch-idempotency-key"
_DEFAULT_MED_SYSTEM = "https://med-auto-practice.local/fhir/CodeSystem/local-catalog"
_DEFAULT_ROUTE_SYSTEM = "https://med-auto-practice.local/fhir/CodeSystem/route"


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


def _numeric_or_none(value: Any) -> int | float | None:
    try:
        d = Decimal(str(value).strip())
    except Exception:
        return None
    if not d.is_finite():
        return None
    if d == d.to_integral_value():
        return int(d)
    return float(d)


def _format_num(value: Any) -> str:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            return str(int(normalized))
        return format(normalized, "f")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class FHIRClinicalSystem:
    """FHIR R4 ClinicalSystem implementation (read-only by default; explicit write capabilities opt-in)."""

    def __init__(
        self,
        base_url: str,
        site_id: str,
        *,
        access_token: Callable[[], str] | None = None,
        timeout: float = 10.0,
        write_capabilities: tuple[str, ...] | frozenset[str] = (),
        required_scopes: tuple[str, ...] = ("patient_context", "results", "current_orders", "documents"),
        max_pages: int = 20,
        medication_system: str = _DEFAULT_MED_SYSTEM,
        route_system: str = _DEFAULT_ROUTE_SYSTEM,
        order_write_status: str = "draft",
        nursing_category: str | None = None,
    ):
        parsed = urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not site_id or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or not (parsed.scheme == "https" or local_http)):
            raise ValueError("expected a site ID and a FHIR HTTPS base (loopback HTTP for a local demo)")
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if order_write_status not in {"draft", "active"}:
            raise ValueError("order_write_status must be 'draft' or 'active'")
        self.base_url = base_url.rstrip("/")
        self._base_parsed = urlsplit(self.base_url)
        self.site_id = site_id
        self.access_token = access_token
        self.timeout = timeout
        self.write_capabilities = frozenset(write_capabilities)
        self.required_scopes = tuple(required_scopes)
        self.max_pages = max_pages
        self.medication_system = medication_system
        self.route_system = route_system
        self.order_write_status = order_write_status
        self.nursing_category = nursing_category
        self._opener = build_opener(_NoRedirect())

    def _headers(self, *, write: bool = False, if_match: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/fhir+json"}
        if write:
            headers["Content-Type"] = "application/fhir+json; charset=utf-8"
        if if_match:
            headers["If-Match"] = if_match
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token()
        return headers

    def _validate_same_origin(self, url: str) -> str:
        parsed = urlsplit(url)
        base = self._base_parsed
        base_prefix = base.path.rstrip("/")
        path_ok = parsed.path == base_prefix or parsed.path.startswith(base_prefix + "/")
        if (
            parsed.scheme != base.scheme
            or parsed.netloc != base.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not path_ok
        ):
            raise ValueError(f"FHIR link escapes configured base_url origin: {url}")
        return url

    def _request_json(
        self, method: str, path_or_url: str, *, body: dict | None = None, if_match: str | None = None,
    ) -> dict:
        if "://" in path_or_url:
            url = self._validate_same_origin(path_or_url)
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(url, data=data, headers=self._headers(write=body is not None, if_match=if_match), method=method)
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

    def _fetch_bundle_pages(self, path_or_url: str) -> tuple[list[dict], bool, tuple[str, ...]]:
        from urllib.parse import urljoin

        next_url: str | None = (
            self._validate_same_origin(path_or_url)
            if "://" in path_or_url
            else f"{self.base_url}/{path_or_url.lstrip('/')}"
        )
        seen: set[str] = set()
        pages = 0
        resources: list[dict] = []
        traces: list[str] = []
        truncated = False

        while next_url:
            if pages >= self.max_pages:
                truncated = True
                break
            self._validate_same_origin(next_url)
            if next_url in seen:
                raise ValueError("FHIR pagination loop detected")
            seen.add(next_url)
            bundle = self._request_json("GET", next_url)
            if bundle.get("resourceType") != "Bundle":
                raise ValueError("expected FHIR Bundle response")
            traces.append(next_url)
            pages += 1
            resources.extend(self._bundle_entries(bundle))

            raw_next = next(
                (
                    lnk.get("url")
                    for lnk in (bundle.get("link") or [])
                    if isinstance(lnk, dict) and lnk.get("relation") == "next" and lnk.get("url")
                ),
                None,
            )
            if raw_next:
                resolved = urljoin(next_url + ("/" if next_url.endswith("/") else ""), str(raw_next)) if "://" not in str(raw_next) else str(raw_next)
                next_url = self._validate_same_origin(resolved)
            else:
                next_url = None

        return resources, truncated, tuple(traces)

    def read_context(self, subject: SubjectRef, capability_id: str):
        from hashlib import sha256
        from urllib.parse import quote
        from med_autopractice.domain.task import Snapshot, SourceCoverage

        if subject.site_id != self.site_id:
            raise ValueError("subject belongs to another site")
        pat_id = _resource_id(subject.patient_id)
        enc_id = _resource_id(subject.encounter_id)
        pat_ref = f"Patient/{pat_id}"
        enc_ref = f"Encounter/{enc_id}"

        encounter = self._request_json("GET", f"Encounter/{enc_id}")
        if (encounter.get("resourceType") != "Encounter" or encounter.get("id") != enc_id
                or not self._bound(encounter.get("subject"), pat_ref)
                or encounter.get("status") == "entered-in-error"):
            raise ValueError("encounter is invalid or belongs to another patient")

        facts: list[Fact] = []
        traces: list[str] = [self._version_ref(self.base_url, "Encounter", enc_id, encounter)]
        coverages: list[SourceCoverage] = [SourceCoverage("FHIR R4", "patient_context", True)]

        # 1. Observations (labs / vitals) with pagination
        obs_items, obs_truncated, obs_traces = self._fetch_bundle_pages(
            f"Observation?patient={quote(pat_id)}&encounter={quote(enc_id)}"
        )
        for obs in obs_items:
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
                val = f"{label} {qty.get('comparator', '')}{_format_num(qty.get('value'))}"
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
        traces.extend(obs_traces)
        coverages.append(
            SourceCoverage("FHIR R4", "results", not obs_truncated, ("pagination_truncated",) if obs_truncated else ())
        )

        # 2. MedicationRequests (active orders) with strict encounter binding & pagination
        med_items, med_truncated, med_traces = self._fetch_bundle_pages(
            f"MedicationRequest?patient={quote(pat_id)}&encounter={quote(enc_id)}"
        )
        for med in med_items:
            if med.get("resourceType") != "MedicationRequest" or not med.get("id"):
                continue
            if med.get("status") == "entered-in-error":
                continue
            if not self._bound(med.get("subject"), pat_ref) or not self._bound(med.get("encounter"), enc_ref):
                continue
            mid = _resource_id(str(med["id"]))
            obj_ref = f"{self.base_url}/MedicationRequest/{mid}"
            src_ref = self._version_ref(self.base_url, "MedicationRequest", mid, med)
            med_cc = med.get("medicationCodeableConcept") or {}
            med_code = next((c.get("code") for c in (med_cc.get("coding") or []) if c.get("code")), None) or med_cc.get("text") or ""
            med_text = med_cc.get("text") or med_code or mid
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
        traces.extend(med_traces)
        coverages.append(
            SourceCoverage(
                "FHIR R4", "current_orders", not med_truncated,
                ("pagination_truncated",) if med_truncated else (),
            )
        )

        # 3. DocumentReferences (existing notes / optional configured nursing category)
        doc_items, doc_truncated, doc_traces = self._fetch_bundle_pages(
            f"DocumentReference?patient={quote(pat_id)}&encounter={quote(enc_id)}"
        )
        for doc in doc_items:
            if doc.get("resourceType") != "DocumentReference" or not doc.get("id"):
                continue
            if doc.get("status") == "entered-in-error":
                continue
            encs = (doc.get("context") or {}).get("encounter") or []
            if not self._bound(doc.get("subject"), pat_ref) or not any(self._bound(e, enc_ref) for e in encs):
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
        traces.extend(doc_traces)
        coverages.append(
            SourceCoverage(
                "FHIR R4", "documents", not doc_truncated,
                ("pagination_truncated",) if doc_truncated else (),
            )
        )

        if self.nursing_category:
            nurse_items, nurse_truncated, nurse_traces = self._fetch_bundle_pages(
                f"DocumentReference?patient={quote(pat_id)}&encounter={quote(enc_id)}&category={quote(self.nursing_category)}"
            )
            for doc in nurse_items:
                if doc.get("resourceType") != "DocumentReference" or not doc.get("id"):
                    continue
                if doc.get("status") == "entered-in-error":
                    continue
                encs = (doc.get("context") or {}).get("encounter") or []
                if not self._bound(doc.get("subject"), pat_ref) or not any(self._bound(e, enc_ref) for e in encs):
                    continue
                did = _resource_id(str(doc["id"]))
                fid = f"DocumentReference/{did}"
                if any(f.fact_id == fid for f in facts):
                    continue
                obj_ref = f"{self.base_url}/DocumentReference/{did}"
                src_ref = self._version_ref(self.base_url, "DocumentReference", did, doc)
                facts.append(Fact(
                    fact_id=fid,
                    value=str(doc.get("description") or f"Nursing Document {did}"),
                    source_ref=src_ref,
                    source_system="FHIR R4",
                    source_object_ref=obj_ref,
                    recorded_at=doc.get("date"),
                    status="observed",
                    details={"section": "观察", "kind": "nursing"},
                ))
            traces.extend(nurse_traces)
            coverages.append(
                SourceCoverage(
                    "FHIR R4", "nursing", not nurse_truncated,
                    ("pagination_truncated",) if nurse_truncated else (),
                )
            )

        rev_src = json.dumps([(f.fact_id, f.source_ref, f.value) for f in facts], ensure_ascii=False, sort_keys=True)
        revision = "fhir-" + sha256(f"{enc_id}:{rev_src}".encode("utf-8")).hexdigest()[:16]
        return Snapshot(subject, revision, tuple(facts), tuple(traces), tuple(coverages))

    def can_apply(self, proposal) -> bool:
        return proposal.subject.site_id == self.site_id and proposal.capability_id in self.write_capabilities

    @staticmethod
    def _resource_type_for(capability_id: str, payload: dict) -> str:
        if payload.get("type") == "note_draft" or capability_id in {"write_inpatient_notes", "write_outpatient_notes"}:
            return "DocumentReference"
        return "MedicationRequest"

    def _resolve_resource_path(self, ref: str, expected_rtype: str) -> tuple[str, str]:
        prefix = f"{self.base_url}/{expected_rtype}/"
        if ref.startswith(prefix):
            rid = _resource_id(ref[len(prefix):].split("/_history/", 1)[0])
        elif ref.startswith(f"{expected_rtype}/"):
            rid = _resource_id(ref[len(expected_rtype) + 1:].split("/_history/", 1)[0])
        else:
            raise ValueError(f"target reference {ref!r} does not identify a {expected_rtype}")
        return f"{expected_rtype}/{rid}", rid

    def _build_r4_medication_request(
        self,
        change: dict,
        *,
        pat_ref: str,
        enc_ref: str,
        item_key: str,
        batch_key: str,
        existing_id: str | None = None,
    ) -> dict:
        action = change.get("action")
        code = str(change.get("code") or "").strip()
        if not code:
            raise ValueError("MedicationRequest requires a catalog code")
        if action not in {"add", "change", "stop"}:
            raise ValueError(f"unsupported order change action: {action!r}")

        target_status = "stopped" if action == "stop" else self.order_write_status
        intent = "proposal" if target_status == "draft" else "order"
        resource: dict = {
            "resourceType": "MedicationRequest",
            "status": target_status,
            "intent": intent,
            "medicationCodeableConcept": {
                "coding": [{"system": self.medication_system, "code": code}],
                "text": code,
            },
            "subject": {"reference": pat_ref},
            "encounter": {"reference": enc_ref},
            "identifier": [
                {"system": _IDEMPOTENCY_SYSTEM, "value": item_key},
                {"system": _BATCH_IDEMPOTENCY_SYSTEM, "value": batch_key},
            ],
        }
        if existing_id:
            resource["id"] = existing_id
        if change.get("reason"):
            resource["reasonCode"] = [{"text": str(change["reason"])}]
        fact_ids = [str(fid) for fid in (change.get("fact_ids") or ()) if str(fid).strip()]
        if fact_ids:
            resource["supportingInformation"] = [{"reference": fid} for fid in fact_ids]
        if action != "stop":
            dosage: dict = {}
            summary_parts = [
                f"{change.get('dose', '')}{change.get('unit', '')}".strip(),
                str(change.get("route") or "").strip(),
                str(change.get("frequency") or "").strip(),
            ]
            dosage["text"] = " ".join(p for p in summary_parts if p)
            if change.get("instruction"):
                dosage["patientInstruction"] = str(change["instruction"])
            if change.get("route"):
                route = str(change["route"])
                dosage["route"] = {"coding": [{"system": self.route_system, "code": route}], "text": route}
            if change.get("frequency"):
                dosage["timing"] = {"code": {"text": str(change["frequency"])}}
            if change.get("dose") is not None or change.get("unit") is not None:
                dose_val = _numeric_or_none(change.get("dose"))
                qty: dict = {}
                if dose_val is not None:
                    qty["value"] = dose_val
                if change.get("unit") is not None:
                    qty["unit"] = str(change["unit"])
                if qty:
                    dosage["doseAndRate"] = [{"doseQuantity": qty}]
            if change.get("duration"):
                dosage.setdefault("additionalInstruction", []).append({"text": f"duration:{change['duration']}"})
            if change.get("quantity") is not None:
                qty_val = _numeric_or_none(change.get("quantity"))
                disp_qty = {"value": qty_val} if qty_val is not None else {"value": str(change["quantity"])}
                if change.get("unit") is not None:
                    disp_qty["unit"] = str(change["unit"])
                resource["dispenseRequest"] = {"quantity": disp_qty}
            resource["dosageInstruction"] = [dosage]
        return resource

    @staticmethod
    def _map_fhir_order_status(status: str | None) -> str:
        if status == "draft":
            return "saved_draft"
        if status == "active":
            return "entered"
        if status == "stopped":
            return "stopped"
        return str(status or "")

    def apply(self, proposal, idempotency_key: str, existing_object_ref: str | None):
        from urllib.parse import quote
        from med_autopractice.domain.task import ExecutionReceipt

        if not self.can_apply(proposal):
            raise PermissionError("FHIRClinicalSystem rejected unconfigured capability or foreign site")
        pat_ref = f"Patient/{_resource_id(proposal.subject.patient_id)}"
        enc_ref = f"Encounter/{_resource_id(proposal.subject.encounter_id)}"
        rtype = self._resource_type_for(proposal.capability_id, proposal.payload)

        if rtype == "DocumentReference":
            if_match: str | None = None
            target = "DocumentReference"
            method = "POST"
            resource: dict = {
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
            if existing_object_ref:
                rel_path, rid = self._resolve_resource_path(existing_object_ref, "DocumentReference")
                existing = self._request_json("GET", rel_path)
                encs = (existing.get("context") or {}).get("encounter") or []
                if not self._bound(existing.get("subject"), pat_ref) or not any(self._bound(e, enc_ref) for e in encs):
                    raise ValueError("existing DocumentReference belongs to another patient or encounter")
                vid = (existing.get("meta") or {}).get("versionId")
                if vid:
                    if_match = f'W/"{vid}"'
                resource["id"] = rid
                target = rel_path
                method = "PUT"
            saved = self._request_json(method, target, body=resource, if_match=if_match)
            if saved.get("resourceType") != "DocumentReference" or not saved.get("id"):
                raise ValueError("FHIR server returned unexpected DocumentReference payload on write")
            rid = _resource_id(str(saved["id"]))
            object_ref = f"{self.base_url}/DocumentReference/{rid}"
            version_ref = self._version_ref(self.base_url, "DocumentReference", rid, saved)
            return ExecutionReceipt(object_ref, "saved_draft", (version_ref,))

        changes = proposal.payload.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError("MedicationRequest write requires non-empty proposal.payload['changes']")

        version_refs: list[str] = []
        created_refs: list[str] = []
        observed_states: list[str] = []
        for idx, ch in enumerate(changes):
            if not isinstance(ch, dict):
                raise ValueError("each order change must be a JSON object")
            action = ch.get("action")
            item_key = f"{idempotency_key}:{idx}"
            if action == "add":
                body = self._build_r4_medication_request(
                    ch, pat_ref=pat_ref, enc_ref=enc_ref, item_key=item_key, batch_key=idempotency_key,
                )
                saved = self._request_json("POST", "MedicationRequest", body=body)
            else:
                target_ref = str(ch.get("target_ref") or "").strip()
                if not target_ref:
                    raise ValueError(f"order {action} requires target_ref")
                rel_path, existing_rid = self._resolve_resource_path(target_ref, "MedicationRequest")
                existing = self._request_json("GET", rel_path)
                if not self._bound(existing.get("subject"), pat_ref) or not self._bound(existing.get("encounter"), enc_ref):
                    raise ValueError("target MedicationRequest belongs to another patient or encounter")
                med_cc = existing.get("medicationCodeableConcept") or {}
                existing_code = next((c.get("code") for c in (med_cc.get("coding") or []) if c.get("code")), None) or med_cc.get("text")
                if existing_code and str(existing_code) != str(ch.get("code")):
                    raise ValueError("target MedicationRequest medication code differs from proposal")
                vid = (existing.get("meta") or {}).get("versionId")
                if_match = f'W/"{vid}"' if vid else None
                body = self._build_r4_medication_request(
                    ch, pat_ref=pat_ref, enc_ref=enc_ref, item_key=item_key, batch_key=idempotency_key,
                    existing_id=existing_rid,
                )
                saved = self._request_json("PUT", rel_path, body=body, if_match=if_match)

            if saved.get("resourceType") != "MedicationRequest" or not saved.get("id"):
                raise ValueError("FHIR server returned unexpected MedicationRequest payload on write")
            rid = _resource_id(str(saved["id"]))
            created_refs.append(f"{self.base_url}/MedicationRequest/{rid}")
            version_refs.append(self._version_ref(self.base_url, "MedicationRequest", rid, saved))
            observed_states.append(self._map_fhir_order_status(saved.get("status")))

        if len(created_refs) == 1:
            batch_ref = created_refs[0]
            receipt_state = observed_states[0]
        else:
            token_param = quote(f"{_BATCH_IDEMPOTENCY_SYSTEM}|{idempotency_key}", safe="")
            batch_ref = f"{self.base_url}/MedicationRequest?identifier={token_param}"
            receipt_state = "saved_draft" if self.order_write_status == "draft" else "entered"
        return ExecutionReceipt(batch_ref, receipt_state, tuple(version_refs))

    def _extract_document_payload(self, res: dict) -> tuple[dict | None, str]:
        contents = res.get("content") or []
        payload = None
        if contents and isinstance(contents[0], dict):
            att = contents[0].get("attachment") or {}
            if isinstance(att.get("data"), str):
                payload = _decode_b64_json(att["data"])
        doc_status = res.get("docStatus") or res.get("status")
        state = "saved_draft" if doc_status == "preliminary" else str(doc_status or "")
        return payload, state

    @staticmethod
    def _extract_r4_medication_fields(res: dict) -> dict[str, Any]:
        med_cc = res.get("medicationCodeableConcept") or {}
        code = next((c.get("code") for c in (med_cc.get("coding") or []) if c.get("code")), None) or med_cc.get("text") or ""
        dosages = res.get("dosageInstruction") or []
        dosage = dosages[0] if dosages and isinstance(dosages[0], dict) else {}
        dose_rates = dosage.get("doseAndRate") or []
        dose_qty = dose_rates[0].get("doseQuantity") if dose_rates and isinstance(dose_rates[0], dict) else {}
        route_cc = dosage.get("route") or {}
        route = next((c.get("code") for c in (route_cc.get("coding") or []) if c.get("code")), None) or route_cc.get("text") or ""
        freq = ((dosage.get("timing") or {}).get("code") or {}).get("text") or ""
        duration = ""
        for inst in dosage.get("additionalInstruction") or []:
            if isinstance(inst, dict) and str(inst.get("text", "")).startswith("duration:"):
                duration = str(inst["text"])[len("duration:"):]
                break
        reasons = res.get("reasonCode") or []
        reason = reasons[0].get("text", "") if reasons and isinstance(reasons[0], dict) else ""
        fact_ids = [
            str(s.get("reference"))
            for s in (res.get("supportingInformation") or [])
            if isinstance(s, dict) and s.get("reference")
        ]
        disp_qty = ((res.get("dispenseRequest") or {}).get("quantity") or {})
        item_key = next(
            (
                str(ident.get("value"))
                for ident in (res.get("identifier") or [])
                if isinstance(ident, dict) and ident.get("system") == _IDEMPOTENCY_SYSTEM and ident.get("value")
            ),
            "",
        )
        return {
            "id": str(res.get("id") or ""),
            "status": str(res.get("status") or ""),
            "code": str(code),
            "dose": _format_num(dose_qty["value"]) if dose_qty and dose_qty.get("value") is not None else "",
            "unit": str(dose_qty.get("unit") or "") if dose_qty else "",
            "route": str(route),
            "frequency": str(freq),
            "instruction": str(dosage.get("patientInstruction") or ""),
            "duration": duration,
            "quantity": _format_num(disp_qty["value"]) if disp_qty.get("value") is not None else "",
            "reason": str(reason),
            "fact_ids": fact_ids,
            "item_key": item_key,
        }

    def _reconstruct_r4_orders_readback(
        self,
        resources: list[dict],
        task,
        *,
        pat_ref: str,
        enc_ref: str,
    ) -> tuple[dict | None, str, bool]:
        """Reconstruct and verify standard FHIR R4 MedicationRequest resources against proposal changes."""
        expected_changes = (task.proposal.payload or {}).get("changes")
        if not isinstance(expected_changes, list) or not expected_changes or not resources:
            return None, "", False

        parsed_items: list[tuple[dict, dict[str, Any]]] = []
        subject_enc_ok = True
        for res in resources:
            if res.get("resourceType") != "MedicationRequest" or not res.get("id"):
                return None, "", False
            if not self._bound(res.get("subject"), pat_ref) or not self._bound(res.get("encounter"), enc_ref):
                subject_enc_ok = False
            parsed_items.append((res, self._extract_r4_medication_fields(res)))

        if not subject_enc_ok:
            return {"resources": [item for _, item in parsed_items]}, "", False

        expected_active_fhir = self.order_write_status
        used: set[int] = set()
        reconstructed_changes: list[dict[str, Any]] = []
        observed_mapped_states: list[str] = []

        for idx, exp in enumerate(expected_changes):
            if not isinstance(exp, dict):
                return {"resources": [item for _, item in parsed_items]}, "", True
            action = exp.get("action")
            code = str(exp.get("code") or "").strip()
            exp_key = f"{task.idempotency_key}:{idx}" if task.idempotency_key else ""
            match_idx: int | None = None

            for r_idx, (raw_res, fields) in enumerate(parsed_items):
                if r_idx in used:
                    continue
                if exp_key and fields["item_key"] and fields["item_key"] != exp_key:
                    continue
                if fields["code"] != code:
                    continue
                if action in {"change", "stop"} and exp.get("target_ref"):
                    _, exp_rid = self._resolve_resource_path(str(exp["target_ref"]), "MedicationRequest")
                    if fields["id"] != exp_rid:
                        continue
                if action == "stop":
                    if fields["status"] != "stopped":
                        continue
                else:
                    if fields["status"] != expected_active_fhir:
                        continue
                    fields_match = True
                    for fname in ("dose", "unit", "route", "frequency", "instruction", "duration", "quantity"):
                        if fname in exp and exp[fname] is not None:
                            if str(fields.get(fname, "")).strip() != str(exp[fname]).strip():
                                fields_match = False
                                break
                    if not fields_match:
                        continue
                if exp.get("reason") and fields["reason"] != str(exp["reason"]):
                    continue
                if exp.get("fact_ids") and fields["fact_ids"] != [str(x) for x in exp["fact_ids"]]:
                    continue
                match_idx = r_idx
                break

            if match_idx is None:
                return {"resources": [item for _, item in parsed_items]}, self._map_fhir_order_status(parsed_items[0][1]["status"]), True
            used.add(match_idx)
            reconstructed_changes.append(dict(exp))
            observed_mapped_states.append(self._map_fhir_order_status(parsed_items[match_idx][1]["status"]))

        if len(observed_mapped_states) == 1:
            batch_state = observed_mapped_states[0]
        else:
            batch_state = "saved_draft" if expected_active_fhir == "draft" else "entered"
        reconstructed = {"type": task.proposal.payload.get("type", "order_changes"), "changes": reconstructed_changes}
        return reconstructed, batch_state, True

    def read_back(self, receipt, task):
        from datetime import datetime, timezone
        from med_autopractice.domain.task import SystemReadback

        if task.proposal is None:
            raise ValueError("read_back requires a task proposal")
        pat_ref = f"Patient/{_resource_id(task.request.subject.patient_id)}"
        enc_ref = f"Encounter/{_resource_id(task.request.subject.encounter_id)}"
        rtype = self._resource_type_for(task.request.capability_id, task.proposal.payload)

        if rtype == "DocumentReference":
            res = self._request_json("GET", receipt.object_ref)
            actual_type = str(res.get("resourceType") or "")
            rid = _resource_id(str(res.get("id") or ""))
            subject_ok = self._bound(res.get("subject"), pat_ref)
            encs = (res.get("context") or {}).get("encounter") or []
            enc_ok = any(self._bound(e, enc_ref) for e in encs)
            visible_payload, mapped_state = self._extract_document_payload(res)
            digest = ""
            if (actual_type == "DocumentReference" and subject_ok and enc_ok and visible_payload is not None
                    and json.dumps(visible_payload, ensure_ascii=False, sort_keys=True)
                    == json.dumps(task.proposal.payload, ensure_ascii=False, sort_keys=True)):
                digest = task.proposal.digest
            version_ref = self._version_ref(self.base_url, actual_type or "DocumentReference", rid, res)
            observed_at = str((res.get("meta") or {}).get("lastUpdated") or datetime.now(timezone.utc).isoformat())
            observed_subject = task.request.subject if (subject_ok and enc_ok) else SubjectRef(self.site_id, "mismatch", "mismatch")
            return SystemReadback(
                subject=observed_subject,
                object_ref=f"{self.base_url}/{actual_type}/{rid}",
                proposal_digest=digest,
                state=mapped_state,
                screenshot_ref=None,
                trace_refs=(version_ref,),
                visible_payload=visible_payload,
                observed_at=observed_at,
                resource_version_ref=version_ref,
            )

        if "?" in receipt.object_ref:
            resources, truncated, _ = self._fetch_bundle_pages(receipt.object_ref)
            if truncated:
                resources = []
        else:
            single = self._request_json("GET", receipt.object_ref)
            resources = [single] if isinstance(single, dict) and single.get("id") else []

        visible_payload, mapped_state, subject_enc_ok = self._reconstruct_r4_orders_readback(
            resources, task, pat_ref=pat_ref, enc_ref=enc_ref,
        )
        version_refs = tuple(
            self._version_ref(self.base_url, "MedicationRequest", _resource_id(str(r["id"])), r)
            for r in resources if r.get("resourceType") == "MedicationRequest" and r.get("id")
        )
        digest = ""
        if (subject_enc_ok and visible_payload is not None
                and json.dumps(visible_payload, ensure_ascii=False, sort_keys=True)
                == json.dumps(task.proposal.payload, ensure_ascii=False, sort_keys=True)):
            digest = task.proposal.digest
        observed_at = str(
            ((resources[0].get("meta") or {}).get("lastUpdated") if resources else None)
            or datetime.now(timezone.utc).isoformat()
        )
        observed_subject = task.request.subject if subject_enc_ok else SubjectRef(self.site_id, "mismatch", "mismatch")
        primary_ver = (
            version_refs[0]
            if len(version_refs) == 1
            else (f"{receipt.object_ref}#versions={len(version_refs)}" if version_refs else None)
        )
        return SystemReadback(
            subject=observed_subject,
            object_ref=receipt.object_ref,
            proposal_digest=digest,
            state=mapped_state,
            screenshot_ref=None,
            trace_refs=version_refs,
            visible_payload=visible_payload,
            observed_at=observed_at,
            resource_version_ref=primary_ver,
        )

    def find_submission(self, task):
        from urllib.parse import quote
        from med_autopractice.domain.task import ExecutionReceipt

        if task.proposal is None or not task.idempotency_key:
            return None
        pat_ref = f"Patient/{_resource_id(task.request.subject.patient_id)}"
        enc_ref = f"Encounter/{_resource_id(task.request.subject.encounter_id)}"
        rtype = self._resource_type_for(task.request.capability_id, task.proposal.payload)

        if rtype == "DocumentReference":
            token_param = quote(f"{_IDEMPOTENCY_SYSTEM}|{task.idempotency_key}", safe="")
            entries, truncated, _ = self._fetch_bundle_pages(f"DocumentReference?identifier={token_param}")
            if truncated or len(entries) != 1 or not entries[0].get("id"):
                return None
            res = entries[0]
            encs = (res.get("context") or {}).get("encounter") or []
            if not self._bound(res.get("subject"), pat_ref) or not any(self._bound(e, enc_ref) for e in encs):
                return None
            rid = _resource_id(str(res["id"]))
            _, state = self._extract_document_payload(res)
            obj_ref = f"{self.base_url}/DocumentReference/{rid}"
            version_ref = self._version_ref(self.base_url, "DocumentReference", rid, res)
            return ExecutionReceipt(obj_ref, state, (version_ref,))

        token_param = quote(f"{_BATCH_IDEMPOTENCY_SYSTEM}|{task.idempotency_key}", safe="")
        query_path = f"MedicationRequest?identifier={token_param}"
        entries, truncated, _ = self._fetch_bundle_pages(query_path)
        if truncated or not entries:
            return None
        valid_entries = [
            res for res in entries
            if res.get("resourceType") == "MedicationRequest" and res.get("id")
            and self._bound(res.get("subject"), pat_ref) and self._bound(res.get("encounter"), enc_ref)
        ]
        if len(valid_entries) != len(entries):
            return None
        version_refs = tuple(
            self._version_ref(self.base_url, "MedicationRequest", _resource_id(str(r["id"])), r)
            for r in valid_entries
        )
        if len(valid_entries) == 1:
            rid = _resource_id(str(valid_entries[0]["id"]))
            obj_ref = f"{self.base_url}/MedicationRequest/{rid}"
            state = self._map_fhir_order_status(valid_entries[0].get("status"))
        else:
            obj_ref = f"{self.base_url}/{query_path}"
            state = "saved_draft" if self.order_write_status == "draft" else "entered"
        return ExecutionReceipt(obj_ref, state, version_refs)
