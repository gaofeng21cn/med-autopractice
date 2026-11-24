"""Dual-track LLM clinical proposer strictly post-validated by deterministic proposal rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
from typing import Any, Protocol
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from med_autopractice.domain.task import Proposal, Snapshot, TaskRequest
from med_autopractice.medicine.phi_guard import PHIGuard, PrivacyMode
from med_autopractice.medicine.proposals import NOTES, OBSERVED, ORDERS, make_note, make_orders


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LLMClient(Protocol):
    """Protocol for hospital-local or outbound OpenAI-compatible LLM clients."""

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> str: ...


class OpenAICompatibleClient:
    """Zero-dependency OpenAI-compatible HTTP client (Ollama / vLLM / cloud HTTPS)."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        privacy_mode: PrivacyMode = "local_trusted",
        trusted_hosts: Sequence[str] = (),
        api_key_provider: Callable[[], str] | None = None,
        timeout: float = 60.0,
    ):
        self.guard = PHIGuard(privacy_mode=privacy_mode, trusted_hosts=trusted_hosts)
        self.endpoint = self.guard.validate_endpoint(endpoint)
        self.model = model
        self.api_key_provider = api_key_provider
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
        }
        if response_format is not None:
            body["response_format"] = response_format
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key_provider is not None:
            key = self.api_key_provider()
            if key:
                headers["Authorization"] = f"Bearer {key}"
        url = self.endpoint if self.endpoint.endswith("/chat/completions") else f"{self.endpoint}/chat/completions"
        req = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except URLError as error:
            raise ConnectionError(f"LLM endpoint unreachable: {error}") from error
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ValueError("LLM response missing choices")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LLM response missing assistant content")
        return content


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM did not return a JSON object")
    obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("LLM JSON payload must be an object")
    return obj


class LLMClinicalProposer:
    """Implements ClinicalKnowledge using local or pseudonymized LLMs under strict rule gating."""

    def __init__(
        self,
        client: LLMClient,
        *,
        model: str,
        endpoint: str = "http://127.0.0.1:11434/v1",
        privacy_mode: PrivacyMode = "local_trusted",
        trusted_hosts: Sequence[str] = (),
        rules_by_capability: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self.guard = PHIGuard(privacy_mode=privacy_mode, trusted_hosts=trusted_hosts)
        self.endpoint = self.guard.validate_endpoint(endpoint)
        self.client = client
        self.model = model
        self.privacy_mode = privacy_mode
        self.rules_by_capability = {k: dict(v) for k, v in (rules_by_capability or {}).items()}

    def _rule_for(self, request: TaskRequest) -> Mapping[str, Any]:
        rule = self.rules_by_capability.get(request.capability_id) or request.inputs.get("rule")
        if not isinstance(rule, Mapping) or not rule.get("approved_ref"):
            raise ValueError(f"missing approved site rule for capability {request.capability_id}")
        return rule

    def propose(self, request: TaskRequest, snapshot: Snapshot) -> Proposal:
        rule = self._rule_for(request)
        valid_facts = {f.fact_id: f for f in snapshot.facts if f.status in OBSERVED}
        if not valid_facts:
            raise ValueError("cannot propose without observed clinical facts")

        known_ids: list[str] = []
        raw_known = request.inputs.get("known_identifiers")
        if isinstance(raw_known, (list, tuple)):
            known_ids.extend(str(item) for item in raw_known if isinstance(item, str))

        with self.guard.new_session(snapshot.subject, known_identifiers=known_ids) as session:
            fact_rows = [
                {
                    "fact_id": f.fact_id,
                    "value": session.scrub(f.value),
                    "unit": f.unit,
                    "effective_at": f.effective_at,
                    "section": f.details.get("section"),
                    "kind": f.details.get("kind"),
                    "code": f.details.get("code"),
                    "object_ref": session.scrub(f.source_object_ref),
                }
                for f in snapshot.facts
                if f.status in OBSERVED
            ]
            clean_inputs = {
                k: v for k, v in request.inputs.items() if k not in {"known_identifiers", "rule"}
            }
            scrubbed_inputs = session.scrub_payload(clean_inputs)
            prompt_payload = {
                "capability_id": request.capability_id,
                "intent": session.scrub(request.intent),
                "patient_pseudonym": session.scrub(snapshot.subject.patient_id),
                "encounter_pseudonym": session.scrub(snapshot.subject.encounter_id),
                "facts": fact_rows,
                "clinician_inputs": scrubbed_inputs,
                "rule_sections": rule.get("sections", []),
            }
            user_prompt = json.dumps(prompt_payload, ensure_ascii=False)
            session.assert_no_phi_leaked(user_prompt)

            if request.capability_id in NOTES:
                sys_prompt = (
                    "你是循证临床文书助手。仅基于给定 facts（不可编造任何检查或病史）生成严格 JSON："
                    '{"sections": {"<section>": "<prose>"}, "cited_fact_ids": ["<fact_id>", ...], '
                    '"knowledge_refs": []}. 所有 cited_fact_ids 必须来自 facts。'
                )
            elif request.capability_id in ORDERS:
                sys_prompt = (
                    "你是循证临床医嘱助手。仅基于给定 facts 生成严格 JSON："
                    '{"changes": [{"action": "add|change|stop", "code": "...", "target_ref": null_or_ref, '
                    '"dose": "...", "unit": "...", "route": "...", "frequency": "...", "reason": "...", '
                    '"fact_ids": ["<fact_id>"]}], "cited_fact_ids": ["<fact_id>"], "knowledge_refs": []}. '
                    "所有 fact_ids 必须来自 facts。"
                )
            else:
                raise ValueError(f"LLMClinicalProposer currently supports note and order capabilities, got {request.capability_id!r}")

            raw_output = self.client.complete(
                [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                response_format={"type": "json_object"},
            )
            parsed = session.restore_payload(_extract_json_object(raw_output))

        # Deterministic post-validation: forbid hallucinated fact IDs and route through proposals.py.
        cited = parsed.get("cited_fact_ids")
        if not isinstance(cited, list) or not cited:
            raise ValueError("LLM proposal must cite at least one fact_id from current snapshot")
        unknown_cited = [str(fid) for fid in cited if str(fid) not in valid_facts]
        if unknown_cited:
            raise ValueError(f"LLM cited non-existent or unverified fact_ids: {unknown_cited}")

        k_refs = tuple(
            str(item) for item in parsed.get("knowledge_refs", []) if isinstance(item, str) and item.strip()
        )
        generator_ref = f"llm://{self.privacy_mode}/{self.model}"

        if request.capability_id in NOTES:
            sections_out = parsed.get("sections")
            if not isinstance(sections_out, dict) or not sections_out:
                raise ValueError("LLM note output must provide a non-empty sections object")
            merged_inputs = dict(request.inputs)
            merged_sections = dict(request.inputs.get("sections") or {})
            for sec_name, sec_text in sections_out.items():
                if not isinstance(sec_text, str) or not sec_text.strip():
                    raise ValueError(f"empty generated text for section {sec_name!r}")
                merged_sections[sec_name] = sec_text.strip()
            merged_inputs["sections"] = merged_sections
            payload = make_note(merged_inputs, snapshot.facts, rule, request.authority_ref)
            evidence_ids = [
                entry["fact_id"]
                for sec in payload["sections"]
                for entry in sec["entries"]
                if "fact_id" in entry
            ]
            fact_refs = tuple(dict.fromkeys([*cited, *evidence_ids]))
            if not fact_refs:
                raise ValueError("note proposal lacks traceable clinical facts")
            return Proposal(
                subject=snapshot.subject,
                capability_id=request.capability_id,
                payload=payload,
                cited_fact_ids=fact_refs,
                generator_ref=generator_ref,
                knowledge_refs=k_refs,
            )

        # ORDERS
        changes_out = parsed.get("changes")
        if not isinstance(changes_out, list) or not changes_out:
            raise ValueError("LLM order output must provide a non-empty changes list")
        for row in changes_out:
            if not isinstance(row, dict):
                raise ValueError("each LLM order change must be an object")
            row_facts = row.get("fact_ids") or cited
            unknown_row = [str(fid) for fid in row_facts if str(fid) not in valid_facts]
            if unknown_row:
                raise ValueError(f"LLM order change cited non-existent fact_ids: {unknown_row}")
            row["fact_ids"] = [str(fid) for fid in row_facts]
        merged_inputs = dict(request.inputs)
        merged_inputs["changes"] = changes_out
        payload = make_orders(
            merged_inputs,
            snapshot.facts,
            rule,
            group=(request.capability_id == "maintain_order_sets"),
        )
        evidence_ids = [fid for row in payload["changes"] for fid in row.get("fact_ids", ())]
        fact_refs = tuple(dict.fromkeys([*cited, *evidence_ids]))
        return Proposal(
            subject=snapshot.subject,
            capability_id=request.capability_id,
            payload=payload,
            cited_fact_ids=fact_refs,
            generator_ref=generator_ref,
            knowledge_refs=k_refs,
        )
