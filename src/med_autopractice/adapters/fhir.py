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
