"""Construct reviewable notes and order changes from sourced facts and clinician input."""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from med_autopractice.domain.task import Fact, WorkflowInputRequired

NOTES = frozenset({"write_inpatient_notes", "write_outpatient_notes"})
ORDERS = frozenset({"prepare_admission_orders", "revise_orders", "prepare_discharge_medication"})
OBSERVED = frozenset({"observed", "verified"})


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {field}")
    return value.strip()


def objects(value: Any, field: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{field} must be a list of objects")
    return value


def facts_of(facts: tuple[Fact, ...], kind: str) -> list[Fact]:
    return [fact for fact in facts if fact.status in OBSERVED and fact.details.get("kind") == kind]


def make_note(
    inputs: dict,
    facts: tuple[Fact, ...],
    rule: Mapping,
    authority: str,
    *,
    missing_scopes: Sequence[str] = (),
) -> dict:
    sections = rule.get("sections")
    if not isinstance(sections, list) or not sections or any(not isinstance(n, str) or not n.strip() for n in sections):
        raise ValueError("document rule needs ordered section names")
    if len(set(sections)) != len(sections):
        raise ValueError("document rule contains duplicate sections")
    entries = inputs.get("sections", {})
    if not isinstance(entries, dict) or set(entries) - set(sections):
        raise ValueError("clinician input contains undefined note sections")
    missing_list = [str(s) for s in missing_scopes if str(s).strip()]
    result = []
    for section in sections:
        evidence = [fact for fact in facts if fact.details.get("section") == section and fact.status in OBSERVED]
        lines = [{"text": f.value, "source_ref": f.source_ref, "fact_id": f.fact_id} for f in evidence]
        if section in entries:
            lines.append({"text": text(entries[section], "section text"), "source_ref": authority})
        if not lines:
            if missing_list:
                lines.append({
                    "text": f"[资料缺失：未获取 {', '.join(missing_list)}，待医生核实补充]",
                    "source_ref": "map://missing-information",
                    "status": "missing_source",
                })
            else:
                raise WorkflowInputRequired(f"sections.{section}")
        result.append({"section": section, "entries": lines})
    payload: dict[str, Any] = {"type": "note_draft", "sections": result}
    if missing_list:
        payload["partial_draft"] = True
        payload["missing_scopes"] = missing_list
    return payload


def make_orders(inputs: dict, facts: tuple[Fact, ...], rule: Mapping, *, group: bool = False) -> dict:
    if inputs.get("changes") in (None, []):
        raise WorkflowInputRequired("changes")
    changes = objects(inputs["changes"], "changes")
    catalog_items = facts_of(facts, "catalog_item")
    catalog = {text(f.details.get("code"), "source catalog code"): f for f in catalog_items}
    current_kind = "order_set_item" if group else "current_order"
    current_items = facts_of(facts, current_kind)
    current = {text(f.source_object_ref, "current order reference"): f for f in current_items}
    if len(catalog) != len(catalog_items) or len(current) != len(current_items):
        raise ValueError("order context contains ambiguous catalog codes or order identities")
    evidence = {f.fact_id: f for f in facts if f.status in OBSERVED}
    seen = set()
    result = []
    for row in changes:
        action = row.get("action")
        if action not in {"add", "change", "stop"}:
            raise ValueError("plan action must be add, change, or stop")
        code = text(row.get("code"), "catalog code")
        ref = row.get("target_ref")
        if code not in catalog or (catalog[code].details.get("active") is not True and action != "stop"):
            raise ValueError("catalog item is missing or inactive")
        if action != "add" and (ref not in current or current[ref].details.get("code") != code):
            raise ValueError("order change needs a matching current target")
        if action == "add" and ref is not None:
            raise ValueError("new order must reference the catalog, not an existing order")
        identity = ref or code
        if identity in seen:
            raise ValueError("duplicate target in clinician plan")
        seen.add(identity)
        fact_ids = row.get("fact_ids")
        if not isinstance(fact_ids, list) or not fact_ids or any(fid not in evidence for fid in fact_ids):
            raise ValueError("each planned change needs supporting facts in the current snapshot")
        entry = {
            "action": action, "code": code, "target_ref": ref,
            "reason": text(row.get("reason"), "clinical rationale"), "fact_ids": fact_ids,
            "catalog_ref": catalog[code].source_ref,
        }
        if action != "stop":
            fields = ("dose", "unit", "route", "frequency")
            if catalog[code].details.get("item_type") not in {"medication", "service"}:
                raise ValueError("catalog item must identify its type")
            if catalog[code].details["item_type"] == "service":
                fields = ("instruction",)
            extra = rule.get("required_fields", [])
            if not isinstance(extra, list) or any(
                not isinstance(f, str) or f not in {"duration", "quantity", "instruction"} for f in extra
            ):
                raise ValueError("invalid additional order fields")
            for field in dict.fromkeys((*fields, *extra)):
                entry[field] = text(row.get(field), field)
        result.append(entry)
    payload = {"type": "order_set" if group else "order_changes", "changes": result}
    if group:
        payload["name"] = text(inputs.get("name"), "order set name")
        payload["version"] = text(inputs.get("version"), "order set version")
        payload["effective_on"] = date.fromisoformat(text(inputs.get("effective_on"), "effective date")).isoformat()
    return payload
