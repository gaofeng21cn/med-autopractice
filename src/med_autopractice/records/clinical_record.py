"""Clean a raw hospital-document collection into one structured clinical record.

This module owns the *cleaning* step, not the reading step. Input is the JSON a
deployment stored while reading documents through a qualified interface; output
is one normalized record with per-section provenance, explicit gaps and
explicit conflicts.

Three guarantees matter to a reviewing clinician:

* nothing is invented - a field the collected documents did not contain is
  reported under ``missing`` instead of being reconstructed from general
  knowledge;
* nothing is silently overwritten - when two observations disagree, both values
  survive under ``conflicts`` and the record keeps the documented one;
* every section points back to the document it came from.

Deployment identity (hospital, product, page paths) and patient content stay in
the deployment profile outside this repository; the rules here are generic.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any, Mapping, Sequence

from med_autopractice.records import research_dataset as research


SCHEMA_VERSION = "map-structured-clinical-record.v1"

#: Review order, not an overwrite rule. The first document present wins for a
#: section, and the record states that basis explicitly. The runner-up value is
#: still reachable through the collection the record was built from.
DOCUMENT_PRECEDENCE = ("出院记录", "疾病证明书")

#: ``key, heading`` in the order a discharge record is normally read.
SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("chief_complaint", "主诉"),
    ("admission_situation", "入院情况"),
    ("admission_diagnoses", "入院诊断"),
    ("positive_findings", "阳性辅助检查"),
    ("hospital_course", "住院经过"),
    ("discharge_diagnoses", "出院诊断"),
    ("discharge_status", "出院情况"),
    ("discharge_orders", "出院医嘱"),
    ("discharge_medications", "出院带药"),
)

_TEXT_SECTIONS = ("chief_complaint", "admission_situation", "hospital_course",
                  "discharge_status")
_LIST_SECTIONS = ("admission_diagnoses", "discharge_diagnoses", "discharge_orders",
                  "positive_findings")
_REQUIRED_SECTIONS = _TEXT_SECTIONS + _LIST_SECTIONS + ("discharge_medications",)

#: Field key -> label used in the review copy. The JSON keeps stable keys; the
#: Markdown a clinician reads must not expose internal field names.
FIELD_LABELS: dict[str, str] = {
    **{key: heading for key, heading in SECTION_ORDER},
    "admission.datetime": "入院时间",
    "discharge.datetime": "出院时间",
}

#: Section key -> key used by the collection packs that feed this module. The
#: indirection keeps the record vocabulary (what a clinician reads) separate
#: from the collection vocabulary (what a reader stored).
_TEXT_FIELDS = {
    "chief_complaint": ("chief_complaint",),
    "admission_situation": ("admission_situation", "admission_status"),
    "hospital_course": ("hospital_course", "course_summary"),
    "discharge_status": ("discharge_status",),
}
_LIST_FIELDS = {
    "admission_diagnoses": ("admission_diagnoses",),
    "discharge_diagnoses": ("discharge_diagnoses",),
    "discharge_orders": ("discharge_orders", "discharge_advice"),
    "positive_findings": ("positive_findings",),
}

_WHITESPACE = re.compile(r"[ \t\u3000]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_NUMBERED = re.compile(r"(?<!\d)(\d{1,2})\s*[.、)）]")
_DATE = re.compile(
    r"(?P<year>\d{4})\s*[-/年]\s*(?P<month>\d{1,2})\s*[-/月]\s*(?P<day>\d{1,2})\s*日?"
    r"(?:\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?))?"
)
_LEADING_MARKER = re.compile(r"^[\s※*·•\-—]*(?:\d{1,2}\s*[.、)）])?\s*")
_PACK_SIZE = re.compile(r"^\s*\d+\s*(?:片|粒|袋|支|盒)\s*装")
_DOSE = re.compile(
    # The amount must not start inside a product name: "碳酸钙D3片" is a name,
    # not a 3-tablet dose, so require a non-alphanumeric character before it.
    r"(?<![0-9A-Za-z])(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>mg|g|ml|mL|μg|ug|IU|U|片|粒|袋|支|丸|滴)"
)
_EXPLICIT_DOSE = re.compile(r"每次量\s*[:：]\s*([^,，;；。]+)")
_TOTAL = re.compile(r"(?:总量|共|合计)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(片|粒|袋|支|盒|瓶)")
_ROUTES = ("静脉滴注", "静脉注射", "静滴", "静推", "皮下注射", "肌肉注射", "肌注",
           "雾化吸入", "舌下含服", "口服", "含服", "外用", "鼻饲")
_FREQUENCY = re.compile(
    r"(每(?:日|天)[一二三四五六七八九十两\d]+次|每晚(?:[一二三四五六七八九十]次)?"
    r"|每[一二三四五六七八九十两\d]+小时(?:一次)?|QD|BID|TID|QID|Q\d+H)",
    re.IGNORECASE,
)
_DEDUPE_STRIP = re.compile(r"[\s、，,。；;：:（）()\[\]【】\-—_·]")


def normalize_text(value: str) -> str:
    """Collapse display whitespace without changing the wording."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def normalize_datetime(value: str | None) -> str | None:
    """Return local wall-clock ``YYYY-MM-DDTHH:MM[:SS]`` for the first date found.

    The record never converts time zones: hospital documents are written in
    local wall-clock time and a converted stamp would be unverifiable.
    ``None`` means "no usable date", never "unknown, assume today".
    """

    if not value:
        return None
    match = _DATE.search(value)
    if not match:
        return None
    year, month, day = (int(match.group("year")), int(match.group("month")),
                        int(match.group("day")))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    stamp = f"{year:04d}-{month:02d}-{day:02d}"
    clock = match.group("time")
    if clock:
        hour, minute = clock.split(":")[0], clock.split(":")[1]
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            return None
        stamp = f"{stamp}T{int(hour):02d}:{minute}"
        if clock.count(":") == 2:
            stamp = f"{stamp}:{clock.split(':')[2]}"
    return stamp


def date_part(stamp: str | None) -> str | None:
    """Return the calendar date of a normalized stamp, or ``None``."""

    return stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else None


def split_numbered_list(value: str) -> list[str]:
    """Split ``1.甲2.乙`` / ``1、甲；2、乙`` into ordered items.

    A delimiter only counts when it is plausible: it must sit at the start of
    the text or after a non-ASCII boundary, its number must advance (a gap of
    at most one is tolerated because hand-typed lists skip numbers), and a
    delimiter followed by digits must lead into a Chinese word. That keeps
    identifiers and measurements - ``X12.345x010``, ``2型糖尿病``, ``直径5.5cm``
    - inside one item instead of cutting them apart. Text with no plausible
    delimiter is returned whole rather than guessed at.
    """

    text = normalize_text(value)
    if not text:
        return []
    accepted: list[re.Match[str]] = []
    last: int | None = None
    for match in _NUMBERED.finditer(text):
        number = int(match.group(1))
        if last is None:
            if match.start() != 0 and number != 1:
                continue
        elif not last < number <= last + 2:
            continue
        if not _delimiter_boundary(text, match):
            continue
        accepted.append(match)
        last = number
    if not accepted:
        return [text]
    items: list[str] = []
    prefix = text[:accepted[0].start()].strip(" \n;；,，。")
    if prefix and not prefix.endswith(("：", ":")):
        items.append(prefix)
    for index, match in enumerate(accepted):
        end = accepted[index + 1].start() if index + 1 < len(accepted) else len(text)
        item = normalize_text(text[match.end():end]).strip(" \n;；,，、")
        if item:
            items.append(item)
    return items


def _delimiter_boundary(text: str, match: re.Match[str]) -> bool:
    """Reject delimiters that belong to an identifier or a measurement."""

    if match.start():
        before = text[match.start() - 1]
        if before.isascii() and before.isalnum():
            return False
    rest = text[match.end():]
    digits = re.match(r"\d+", rest)
    if digits:
        following = rest[digits.end():digits.end() + 1]
        if not following or following.isascii():
            return False
    return True


def dedupe_preserving_order(items: Sequence[str]) -> list[str]:
    """Drop repeats while keeping the first wording and the original order."""

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = _DEDUPE_STRIP.sub("", item).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def parse_medication(line: str) -> dict[str, Any]:
    """Parse one discharge-medication line without guessing missing parts.

    Recognized parts are returned as separate fields; anything not recognized
    stays in ``raw`` and is flagged through ``parsed``/``uncertain`` so a
    reviewer re-reads the source instead of trusting a partial parse.
    """

    raw = normalize_text(line)
    text = _PACK_SIZE.sub("", _LEADING_MARKER.sub("", raw)).strip()
    record: dict[str, Any] = {
        "name": None, "dose": None, "route": None, "frequency": None,
        "total": None, "raw": raw, "parsed": False, "uncertain": True,
        "notes": [],
    }

    total_match = _TOTAL.search(text)
    if total_match:
        record["total"] = f"{total_match.group(1)}{total_match.group(2)}"

    explicit = _EXPLICIT_DOSE.search(text)
    cut = len(text)
    if explicit:
        record["dose"] = normalize_text(explicit.group(1))
        cut = min(cut, explicit.start())
    else:
        for candidate in _DOSE.finditer(text):
            if total_match and candidate.start() >= total_match.start():
                break
            record["dose"] = f"{candidate.group('amount')}{candidate.group('unit')}"
            cut = min(cut, candidate.start())
            break

    for route in _ROUTES:
        position = text.find(route)
        if position >= 0:
            record["route"] = route
            cut = min(cut, position)
            break

    frequency = _FREQUENCY.search(text)
    if frequency:
        record["frequency"] = normalize_text(frequency.group(1))
        cut = min(cut, frequency.start())

    name = normalize_text(text[:cut]).strip(" ,，;；:：")
    if not name and text:
        name = normalize_text(text.split("，")[0])
    record["name"] = name or None

    record["parsed"] = bool(record["name"]) and bool(record["dose"] or record["total"])
    record["uncertain"] = not record["parsed"]
    if record["uncertain"]:
        record["notes"].append("未识别到剂量或总量，按原文保留待人工核对")
    return record


def build_record(collection: Mapping[str, Any], *, record_id: str) -> dict[str, Any]:
    """Build a structured clinical record from one deployment collection pack."""

    encounters = collection.get("encounters") or []
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "profile_id": collection.get("site_id"),
        "built_from": {
            "collection_schema": collection.get("schema_version"),
            "collected_at": collection.get("collected_at"),
            "collection_mode": collection.get("collection_mode"),
            "document_precedence": list(DOCUMENT_PRECEDENCE),
        },
        "patient": dict(collection.get("patient") or {}),
        "source_system": dict(collection.get("source") or {}),
        "document_semantics": dict(collection.get("document_semantics") or {}),
        "prior_history_outside_this_hospital": list(
            collection.get("prior_history_outside_this_hospital") or []
        ),
        "encounters": [_build_encounter(encounter) for encounter in encounters],
        "gaps": list(collection.get("gaps") or []),
        "verification": dict(collection.get("verification") or {}),
    }


def _build_encounter(encounter: Mapping[str, Any]) -> dict[str, Any]:
    documents = dict(encounter.get("documents") or {})
    readable = [name for name in documents if _document_read(documents.get(name))]
    primary = next((name for name in DOCUMENT_PRECEDENCE if name in readable), None)
    if primary is None:
        primary = readable[0] if readable else None
    basis = ("only_read_document" if len(readable) == 1
             else "document_precedence" if primary in DOCUMENT_PRECEDENCE
             else "unverified")
    provenance = {
        "document": primary,
        "document_created_at": _document_field(documents, primary, "created_at"),
        "author": _document_field(documents, primary, "author"),
        "basis": basis,
    }
    if primary is None:
        provenance["notes"] = ["采集包未标记任何已读文书，字段来源不可核实"]

    admission = normalize_datetime(
        encounter.get("admission_datetime") or _compose(
            encounter.get("admission_date"), encounter.get("admission_time")
        )
    )
    discharge = normalize_datetime(
        encounter.get("discharge_datetime") or _compose(
            encounter.get("discharge_date"), encounter.get("discharge_time")
        )
    )
    stay = encounter.get("actual_los_days", encounter.get("length_of_stay_days"))

    sections: dict[str, Any] = {}
    for key in _TEXT_SECTIONS:
        sections[key] = _text_section(_first(encounter, _TEXT_FIELDS[key]), provenance)
    for key in _LIST_SECTIONS:
        sections[key] = _list_section(
            _first(encounter, _LIST_FIELDS[key]), provenance,
            fallback=encounter.get(f"{key}_text"),
        )
    sections["discharge_medications"] = _medication_section(
        encounter.get("discharge_medications"), provenance
    )

    missing = _missing_fields(sections, admission, discharge)
    document_label = provenance.get("document") or "collection_primary_document"
    conflicts = _conflicts(encounter, admission, discharge, document_label)
    return {
        "encounter_id": f"{encounter.get('department', 'unknown')}/"
                        f"{date_part(admission) or 'unknown-date'}",
        "department": encounter.get("department"),
        "bed_no": encounter.get("bed_no"),
        "admission": {"datetime": admission},
        "discharge": {
            "datetime": discharge,
            "calendar_date": date_part(discharge),
            "observations": _discharge_observations(encounter, discharge, document_label),
        },
        "length_of_stay_days": stay,
        "sections": sections,
        "provenance": provenance,
        "missing": missing,
        "conflicts": conflicts,
    }


def _compose(date_value: Any, time_value: Any) -> str | None:
    if not date_value:
        return None
    return f"{date_value} {time_value}".strip() if time_value else str(date_value)


def _first(source: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = source.get(key)
        if value:
            return value
    return None


def _document_read(document: Any) -> bool:
    return isinstance(document, Mapping) and bool(document.get("content_read"))


def _document_field(documents: Mapping[str, Any], name: str | None, field: str) -> Any:
    if name is None:
        return None
    document = documents.get(name)
    return document.get(field) if isinstance(document, Mapping) else None


def _text_section(value: Any, provenance: Mapping[str, Any]) -> dict[str, Any]:
    text = normalize_text(str(value)) if value else ""
    return {"text": text or None, "provenance": dict(provenance)}


def _list_section(value: Any, provenance: Mapping[str, Any],
                  fallback: Any = None) -> dict[str, Any]:
    items: list[str] = []
    for entry in _as_sequence(value) or _as_sequence(fallback):
        items.extend(split_numbered_list(entry))
    return {"items": dedupe_preserving_order(items), "provenance": dict(provenance)}


def _medication_section(value: Any, provenance: Mapping[str, Any]) -> dict[str, Any]:
    medications = [parse_medication(line) for line in _as_sequence(value)]
    return {
        "items": medications,
        "unparsed": [item["raw"] for item in medications if not item["parsed"]],
        "provenance": dict(provenance),
    }


def _as_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        return [f"{key} {entry}" for key, entry in value.items() if entry]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [str(value)]


def _missing_fields(sections: Mapping[str, Any], admission: str | None,
                    discharge: str | None) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for key in _REQUIRED_SECTIONS:
        section = sections.get(key) or {}
        filled = section.get("text") if key in _TEXT_SECTIONS else section.get("items")
        if not filled:
            missing.append({
                "field": key,
                "reason": "采集到的文书中没有该字段，未用其他来源补造",
            })
    if not admission:
        missing.append({"field": "admission.datetime",
                        "reason": "采集到的文书中没有可解析的入院时间"})
    if not discharge:
        missing.append({"field": "discharge.datetime",
                        "reason": "采集到的文书中没有可解析的出院时间"})
    return missing


def _discharge_observations(encounter: Mapping[str, Any],
                            discharge: str | None,
                            document_label: str) -> list[dict[str, Any]]:
    documented = {
        "source": document_label,
        "datetime": discharge,
        "calendar_date": date_part(discharge),
    }
    observations = [documented]
    for entry in encounter.get("external_observations") or []:
        if not isinstance(entry, Mapping):
            continue
        stamp = normalize_datetime(
            entry.get("discharge_datetime") or _compose(
                entry.get("discharge_date"), entry.get("discharge_time")
            )
        )
        observations.append({
            "source": entry.get("source"),
            "datetime": stamp,
            "calendar_date": date_part(stamp),
        })
    return observations


def _conflicts(encounter: Mapping[str, Any], admission: str | None,
               discharge: str | None,
               document_label: str) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    documented = {"document": document_label, "datetime": discharge}
    for entry in encounter.get("external_observations") or []:
        if not isinstance(entry, Mapping):
            continue
        other = normalize_datetime(
            entry.get("discharge_datetime") or _compose(
                entry.get("discharge_date"), entry.get("discharge_time")
            )
        )
        if documented["datetime"] and other and _differs(
                documented["datetime"], other):
            conflicts.append({
                "field": "discharge.datetime",
                "values": [
                    {"source": documented["document"], "value": documented["datetime"]},
                    {"source": entry.get("source"), "value": other},
                ],
                "same_calendar_date": date_part(other) == date_part(documented["datetime"]),
                "resolution": "kept_both_review_required",
                "note": "两份记录的时刻不同；日期相同不代表可以直接替换，"
                        "不得据此推算医嘱停用或结算时间。",
            })
        other_admission = normalize_datetime(
            entry.get("admission_datetime") or _compose(
                entry.get("admission_date"), entry.get("admission_time")
            )
        )
        if admission and other_admission and _differs(admission, other_admission):
            conflicts.append({
                "field": "admission.datetime",
                "values": [
                    {"source": document_label, "value": admission},
                    {"source": entry.get("source"), "value": other_admission},
                ],
                "same_calendar_date": date_part(other_admission) == date_part(admission),
                "resolution": "kept_both_review_required",
            })
    return conflicts


def _differs(left: str, right: str) -> bool:
    """Compare two stamps at the coarsest precision they share.

    ``2026-09-16T09:41`` and ``2026-09-16T09:41:25`` describe the same minute;
    one source simply records seconds. Reporting that as a conflict would train
    reviewers to ignore the conflict list, so only a real disagreement at the
    shared precision counts. A genuine difference (``14:28`` vs ``15:41``) is
    still reported.
    """

    return not (left.startswith(right) or right.startswith(left))


def validate_record(record: Mapping[str, Any]) -> list[str]:
    """Return the list of contract violations; empty means the record is usable."""

    errors: list[str] = []
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(record.get("record_id"), str) or not record.get("record_id"):
        errors.append("record_id must be a non-empty string")
    if not isinstance(record.get("patient"), Mapping):
        errors.append("patient must be an object")
    encounters = record.get("encounters")
    if not isinstance(encounters, list) or not encounters:
        errors.append("encounters must be a non-empty array")
        return sorted(errors)
    for index, encounter in enumerate(encounters):
        errors.extend(_validate_encounter(index, encounter))
    return sorted(errors)


def _validate_encounter(index: int, encounter: Any) -> list[str]:
    prefix = f"encounters[{index}]"
    errors: list[str] = []
    if not isinstance(encounter, Mapping):
        return [f"{prefix} must be an object"]
    if not (encounter.get("admission") or {}).get("datetime"):
        errors.append(f"{prefix}.admission.datetime is required")
    if not (encounter.get("discharge") or {}).get("datetime"):
        errors.append(f"{prefix}.discharge.datetime is required")
    provenance = encounter.get("provenance") or {}
    if not isinstance(provenance, Mapping) or "document" not in provenance:
        errors.append(f"{prefix}.provenance.document is required")
    sections = encounter.get("sections")
    if not isinstance(sections, Mapping):
        return errors + [f"{prefix}.sections must be an object"]
    for key in _REQUIRED_SECTIONS:
        section = sections.get(key)
        if not isinstance(section, Mapping):
            errors.append(f"{prefix}.sections.{key} is required")
            continue
        if not isinstance(section.get("provenance"), Mapping):
            errors.append(f"{prefix}.sections.{key}.provenance is required")
        if key in _TEXT_SECTIONS and section.get("text") == "":
            errors.append(f"{prefix}.sections.{key}.text must not be empty")
        if key in _LIST_SECTIONS and not isinstance(section.get("items"), list):
            errors.append(f"{prefix}.sections.{key}.items must be an array")
        if key == "discharge_medications":
            items = section.get("items")
            if not isinstance(items, list):
                errors.append(f"{prefix}.sections.{key}.items must be an array")
                continue
            for position, medication in enumerate(items):
                if not isinstance(medication, Mapping) or not medication.get("name"):
                    errors.append(
                        f"{prefix}.sections.{key}.items[{position}].name is required")
                elif not medication.get("raw"):
                    errors.append(
                        f"{prefix}.sections.{key}.items[{position}].raw is required")
    for position, conflict in enumerate(encounter.get("conflicts") or []):
        if not isinstance(conflict, Mapping) or len(conflict.get("values") or []) < 2:
            errors.append(f"{prefix}.conflicts[{position}] must keep both values")
        elif not conflict.get("resolution"):
            errors.append(f"{prefix}.conflicts[{position}].resolution is required")
    return errors


def render_markdown(record: Mapping[str, Any]) -> str:
    """Render one record as a reviewable Chinese structured record."""

    patient = record.get("patient") or {}
    lines: list[str] = [
        "# 结构化病历（清洗结果）",
        "",
        f"> 记录 ID：{record.get('record_id')}｜来源系统："
        f"{(record.get('source_system') or {}).get('system_id') or '未标注'}"
        f"｜角色：{(record.get('source_system') or {}).get('role') or '未标注'}",
        f"> 生成自采集包：{(record.get('built_from') or {}).get('collection_schema')}"
        f" @ {(record.get('built_from') or {}).get('collected_at')}"
        f"｜采集方式：{(record.get('built_from') or {}).get('collection_mode')}",
        f"> 患者：{patient.get('name')}｜{patient.get('gender')}｜出生 "
        f"{patient.get('birth_date')}｜住院号 {patient.get('inpatient_no')}",
        "",
        "本记录只整理已采集到的文书内容，不补造缺失项；每节标注来源文书。"
        "缺失与冲突集中在文末。",
        "",
        "## 就诊时间线",
        "",
        "| # | 科室 | 入院 | 出院 | 住院天数 | 字段缺口 | 冲突 |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, encounter in enumerate(record.get("encounters") or [], start=1):
        lines.append(
            f"| {index} | {encounter.get('department') or '—'} | "
            f"{_stamp(encounter.get('admission'))} | {_stamp(encounter.get('discharge'))} | "
            f"{encounter.get('length_of_stay_days') or '—'} | "
            f"{len(encounter.get('missing') or [])} | {len(encounter.get('conflicts') or [])} |"
        )
    lines.append("")

    for index, encounter in enumerate(record.get("encounters") or [], start=1):
        lines.extend(_render_encounter(index, encounter))

    prior = record.get("prior_history_outside_this_hospital") or []
    if prior:
        lines.extend(["## 院外既往诊疗（由本次文书带出，未回原文核验）", ""])
        lines.extend(f"- {normalize_text(item)}" for item in prior)
        lines.append("")

    gaps = record.get("gaps") or []
    if gaps:
        lines.extend(["## 采集层面的缺口", ""])
        lines.extend(f"- {normalize_text(str(item))}" for item in gaps)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_encounter(index: int, encounter: Mapping[str, Any]) -> list[str]:
    sections = encounter.get("sections") or {}
    provenance = encounter.get("provenance") or {}
    lines = [
        f"## 就诊 {index}：{_stamp(encounter.get('admission'))} → "
        f"{_stamp(encounter.get('discharge'))}",
        "",
    ]
    if encounter.get("bed_no"):
        lines.append(f"- 床位号：{encounter['bed_no']}")
    if encounter.get("length_of_stay_days"):
        lines.append(f"- 住院天数：{encounter['length_of_stay_days']}")
    lines.append("")
    for key, heading in SECTION_ORDER:
        section = sections.get(key) or {}
        lines.append(f"### {heading}")
        lines.append("")
        if key in _TEXT_SECTIONS:
            lines.append(section.get("text") or "_（本次采集未取得，见缺口）_")
        elif key == "discharge_medications":
            lines.extend(_render_medications(section))
        else:
            items = section.get("items") or []
            if items:
                lines.extend(f"{position}. {item}"
                             for position, item in enumerate(items, start=1))
            else:
                lines.append("_（本次采集未取得，见缺口）_")
        lines.append("")
    lines.extend([
        "### 来源",
        "",
        f"- {provenance.get('document') or '未标注文书'}"
        f"（创建 {provenance.get('document_created_at') or '未标注'}，"
        f"作者 {provenance.get('author') or '未标注'}）"
        f"｜依据：{provenance.get('basis')}",
        "",
    ])
    observations = (encounter.get("discharge") or {}).get("observations") or []
    if len(observations) > 1:
        lines.extend(["### 出院时间的多来源记录", ""])
        lines.extend(
            f"- {item.get('source')}：{item.get('datetime') or '未取得'}"
            for item in observations
        )
        lines.append("")
    missing = encounter.get("missing") or []
    if missing:
        lines.extend(["### 缺口", ""])
        lines.extend(f"- {_label(item.get('field'))}：{item.get('reason')}" for item in missing)
        lines.append("")
    conflicts = encounter.get("conflicts") or []
    if conflicts:
        lines.extend(["### 冲突（保留双方，待人工判定）", ""])
        for conflict in conflicts:
            values = "；".join(f"{item.get('source')}={item.get('value')}"
                               for item in conflict.get("values") or [])
            lines.append(f"- {_label(conflict.get('field'))}：{values}"
                         f"（同日：{conflict.get('same_calendar_date')}，"
                         f"处置：{conflict.get('resolution')}）")
        lines.append("")
    return lines


def _label(field: Any) -> str:
    """Return the clinician-facing label for a field key, falling back to the key."""

    return FIELD_LABELS.get(field, str(field)) if field else "未标注"


def _render_medications(section: Mapping[str, Any]) -> list[str]:
    items = section.get("items") or []
    if not items:
        return ["_（本次采集未取得，见缺口）_"]
    lines = ["| 药品 | 单次量 | 途径 | 频次 | 总量 | 解析状态 |",
             "|---|---|---|---|---|---|"]
    for item in items:
        status = "已解析" if item.get("parsed") else "待人工核对"
        lines.append(
            f"| {item.get('name') or '—'} | {item.get('dose') or '—'} | "
            f"{item.get('route') or '—'} | {item.get('frequency') or '—'} | "
            f"{item.get('total') or '—'} | {status} |"
        )
    return lines


def _stamp(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("datetime")
    return str(value).replace("T", " ") if value else "—"


# --------------------------------------------------------------------------
# Human-facing output
#
# The functions below reorganize what the documents already say. They group,
# order, diff and flag; they do not interpret. Every item keeps the encounter
# it came from so a reader can go back to the source document.
# --------------------------------------------------------------------------

#: Similarity above which two diagnosis labels are surfaced as "possibly the
#: same problem, wording differs". Deliberately conservative: merging is a
#: clinical decision, so near-matches are reported instead of collapsed.
_SAME_PROBLEM_THRESHOLD = 0.6


def build_problem_list(record: Mapping[str, Any]) -> dict[str, Any]:
    """Group documented diagnoses across encounters without merging by guess.

    Grouping is exact on normalized wording. Labels that look like the same
    problem but differ (``示例肿物术后复发`` vs ``示例肿物 单发囊肿``) are
    returned under ``needs_confirmation`` instead of being merged, because
    deciding they are one problem is a clinical judgement.
    """

    encounters = record.get("encounters") or []
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, encounter in enumerate(encounters, start=1):
        sections = encounter.get("sections") or {}
        for field, phase in (("admission_diagnoses", "admission"),
                             ("discharge_diagnoses", "discharge")):
            for raw in (sections.get(field) or {}).get("items") or []:
                label = normalize_text(str(raw))
                if not label:
                    continue
                key = _diagnosis_key(label)
                entry = grouped.get(key)
                if entry is None:
                    entry = {"label": label, "occurrences": [], "encounters": []}
                    grouped[key] = entry
                    order.append(key)
                if index not in entry["encounters"]:
                    entry["encounters"].append(index)
                entry["occurrences"].append({"encounter": index, "phase": phase})

    problems = [grouped[key] for key in order]
    for problem in problems:
        phases = {item["phase"] for item in problem["occurrences"]}
        problem["encounter_count"] = len(problem["encounters"])
        problem["phases"] = sorted(phases)
        problem["in_latest_encounter"] = bool(encounters) and (
            encounters and len(encounters) in problem["encounters"]
        )
        problem["added_during_stay"] = [
            index for index in problem["encounters"]
            if {"encounter": index, "phase": "discharge"} in problem["occurrences"]
            and {"encounter": index, "phase": "admission"} not in problem["occurrences"]
        ]

    return {
        "problems": problems,
        "needs_confirmation": _similar_pairs(problems),
        "per_encounter_delta": _diagnosis_delta(encounters),
    }


def _diagnosis_key(label: str) -> str:
    return _DEDUPE_STRIP.sub("", label).lower()


def _similar_pairs(problems: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(problems):
        for right in problems[index + 1:]:
            first, second = str(left["label"]), str(right["label"])
            if not _looks_similar(first, second):
                continue
            pairs.append({
                "a": first,
                "b": second,
                "a_encounters": list(left["encounters"]),
                "b_encounters": list(right["encounters"]),
                "reason": "表述相近但不是同一字符串，是否为同一问题需人工确认",
            })
    return pairs


def _looks_similar(left: str, right: str) -> bool:
    first = _DEDUPE_STRIP.sub("", left)
    second = _DEDUPE_STRIP.sub("", right)
    if not first or not second:
        return False
    if first in second or second in first:
        return min(len(first), len(second)) >= 4
    return SequenceMatcher(None, first, second).ratio() >= _SAME_PROBLEM_THRESHOLD


def _diagnosis_delta(encounters: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Report additions and removals between admission and discharge wording."""

    deltas: list[dict[str, Any]] = []
    for index, encounter in enumerate(encounters, start=1):
        sections = encounter.get("sections") or {}
        admission = (sections.get("admission_diagnoses") or {}).get("items") or []
        discharge = (sections.get("discharge_diagnoses") or {}).get("items") or []
        admission_keys = {_diagnosis_key(item): item for item in admission}
        discharge_keys = {_diagnosis_key(item): item for item in discharge}
        added = [item for key, item in discharge_keys.items() if key not in admission_keys]
        removed = [item for key, item in admission_keys.items() if key not in discharge_keys]
        if added or removed:
            deltas.append({
                "encounter": index,
                "added_at_discharge": added,
                "only_in_admission": removed,
                "note": "同一住院内入院诊断与出院诊断的差异；文书未说明原因。"
                        "可能是新发、补充或表述变更，需人工判定。",
            })
    return deltas


def normalize_review_notes(notes: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize reviewer-supplied items; clinical wording stays outside this repo.

    Each note may carry an ``anchor`` — a short string copied from the rendered
    record. When present, the note's number is also marked at that line so a
    reader meets the uncertainty where the text is, not only in a list at the
    end.
    """

    normalized: list[dict[str, Any]] = []
    for item in notes or ():
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        anchor = item.get("anchor")
        normalized.append({
            "title": title,
            "level": str(item.get("level") or "").strip(),
            "detail": str(item.get("detail") or "").strip(),
            "verify": str(item.get("verify") or "").strip(),
            "anchor": str(anchor) if anchor else None,
        })
    return normalized


def render_clinical_summary(record: Mapping[str, Any],
                            review_notes: Sequence[Mapping[str, Any]] | None = None) -> str:
    """Render the record as one human-readable clinical summary.

    The compiled record comes first; differences that need a human decision are
    listed after it, and repeated as numbered marks next to the text they
    qualify.
    """

    patient = record.get("patient") or {}
    source = record.get("source_system") or {}
    built = record.get("built_from") or {}
    encounters = record.get("encounters") or []
    latest = encounters[-1] if encounters else {}
    analysis = build_problem_list(record)
    notes = normalize_review_notes(review_notes)

    header: list[str] = [
        "# 结构化病历 · 临床阅读版",
        "",
        f"**患者**　{patient.get('name') or '—'}　{patient.get('gender') or '—'}　"
        f"出生 {patient.get('birth_date') or '—'}　年龄 {patient.get('age_years') or '—'}　"
        f"住院号 {patient.get('inpatient_no') or '—'}",
        "",
        f"**资料来源**　{source.get('system_id') or '—'}"
        f"（{source.get('role') or '未标注角色'}，{source.get('department_scope') or '未标注科室'}）",
        f"**采集**　{built.get('collected_at') or '—'}　方式 {built.get('collection_mode') or '—'}",
        f"**文书依据**　每次就诊以"
        f"{'、'.join((record.get('built_from') or {}).get('document_precedence') or []) or '—'}"
        f"为复核顺序",
        "",
        "> 本页只整理已采集文书的原文内容：不补造缺失项，不合并需要临床判断的诊断，"
        "冲突一律保留双方。"
        + ("正文与表格中标有 **※需核实 N** 之处，与「需人工核实」一节逐条对应。"
           if notes else ""),
        "",
    ]

    timeline: list[str] = [
        "| # | 科室 | 入院 | 出院 | 住院天数 | 床位 |",
        "|---|---|---|---|---|---|",
    ]
    for index, encounter in enumerate(encounters, start=1):
        timeline.append(
            f"| {index} | {encounter.get('department') or '—'} | "
            f"{_stamp(encounter.get('admission'))} | {_stamp(encounter.get('discharge'))} | "
            f"{encounter.get('length_of_stay_days') or '—'} | {encounter.get('bed_no') or '—'} |"
        )
    timeline.extend(["", f"共 {len(encounters)} 次住院记录。", ""])

    highlights: list[str] = []
    for index, encounter in enumerate(encounters, start=1):
        highlights.extend(_render_encounter_highlights(index, encounter))

    sections: list[tuple[str, list[str]]] = [
        # 1) What a researcher needs first: fields extracted into fixed columns
        #    with normalized codes next to the original wording.
        ("结构化病历（研究数据集）", research.render_research_record(record)),
        # 2) What still needs a decision, so the tables can be trusted.
        ("问题清单与需核实项", _render_problem_list(analysis)
         + _render_data_issues(record) + _render_open_questions(analysis, record)
         + _render_review_notes(notes)),
        # 3) The source wording last, for checking any extracted field.
        ("病历原文", timeline + highlights),
        ("出院带药原文（最近一次）", _render_latest_medications(latest)),
        ("院外既往诊疗原文（由本次文书带出，未回原文核验）",
         _render_prior_history(record)),
    ]

    lines = list(header)
    numeral = "一二三四五六七八九十"
    rendered = 0
    for title, body in sections:
        if not body:
            continue
        rendered += 1
        marker = numeral[rendered - 1] if rendered <= len(numeral) else str(rendered)
        lines.append(f"## {marker}、{title}")
        lines.append("")
        lines.extend(body)

    rendered += 1
    marker = numeral[rendered - 1] if rendered <= len(numeral) else str(rendered)
    lines.extend([
        f"## {marker}、这份文件是什么",
        "",
        "结构化工作副本，供快速阅读与核对使用。它不写回院内系统，不替代正式病历，"
        "也不构成任何诊疗建议。所有诊断、医嘱与带药内容均照抄自文书记载；"
        "文中标注的差异与冲突尚未判定，需回原文或由你确认。",
        "",
    ])
    return "\n".join(_mark_review_notes(lines, notes)).rstrip() + "\n"


def _render_review_notes(notes: Sequence[Mapping[str, Any]]) -> list[str]:
    if not notes:
        return []
    lines = [
        "### 需人工核实（详细情况，与正文/表格 ※ 对应）",
        "",
        f"共 {len(notes)} 条。按临床紧迫度排列，每条给出需要确认的具体问题。",
        "",
    ]
    for number, note in enumerate(notes, start=1):
        suffix = f"（{note['level']}）" if note["level"] else ""
        lines.extend([f"### {number}. {note['title']}{suffix}", ""])
        if note["detail"]:
            lines.extend([note["detail"], ""])
        if note["verify"]:
            lines.extend([f"→ 核实：{note['verify']}", ""])
    return lines


def _mark_review_notes(lines: Sequence[str],
                       notes: Sequence[Mapping[str, Any]]) -> list[str]:
    """Append ``※需核实 N`` after the first line carrying that note's anchor.

    One mark per note keeps the mark next to the text a reader meets first;
    repeating it on every later mention would turn the record into a list of
    warnings.
    """

    anchored = {number: note["anchor"]
                for number, note in enumerate(notes, start=1) if note["anchor"]}
    if not anchored:
        return list(lines)
    marked: list[str] = []
    pending: list[str] = []  # marks for table rows, held until the table ends

    def flush_pending() -> None:
        if pending:
            # Keep one blank line before the first mark and between marks so
            # each renders as its own quoted line.
            marked.append("")
            for index, mark in enumerate(pending):
                if index:
                    marked.append("")
                marked.append(mark)
            pending.clear()

    for line in lines:
        if pending and not line.startswith("|"):
            flush_pending()
        marked.append(line)
        hits = [number for number, anchor in list(anchored.items()) if anchor in line]
        if not hits:
            continue
        labels = "、".join(str(number) for number in hits)
        if line.startswith("|"):
            # A blockquote inside a table would split it into two tables, so
            # name the row by the matched anchor and emit the mark afterwards.
            row = next((anchor for number, anchor in anchored.items()
                        if number in hits), "").strip()
            quoted = f"「{row}」" if row else "命中的一行"
            pending.append(f"> ※需核实 {labels}（对应上表中 {quoted} 所在行）")
        else:
            marked.append(f"> ※需核实 {labels}")
        for number in hits:
            anchored.pop(number)
    flush_pending()
    return marked


def _render_problem_list(analysis: Mapping[str, Any]) -> list[str]:
    problems = analysis.get("problems") or []
    lines = [
        f"共 {len(problems)} 条。归并依据是诊断文字完全相同；表述不同的未合并，"
        "列在下方「待确认是否同一问题」。",
        "",
        "| 诊断（文书记载） | 涉及就诊 | 最近一次就诊仍记载 | 本次住院期间新增 |",
        "|---|---|---|---|",
    ]
    for problem in problems:
        visits = "、".join(str(index) for index in problem["encounters"])
        added = "、".join(str(index) for index in problem.get("added_during_stay") or [])
        lines.append(
            f"| {problem['label']} | 第 {visits} 次 | "
            f"{'是' if problem.get('in_latest_encounter') else '否'} | {added or '—'} |"
        )
    lines.append("")
    lines.extend([
        "「本次住院期间新增」指该诊断在本次住院的**入院诊断**中没有、**出院诊断**中"
        "出现。文书未说明原因，可能是新发、补充或表述变更。",
        "",
        "「最近一次就诊仍记载」只表示该诊断文字出现在最近一次就诊的文书中。显示"
        "「否」可能是已缓解，也可能是文书中未逐项列出，不能据此判断病情变化。",
        "",
    ])

    pairs = analysis.get("needs_confirmation") or []
    if pairs:
        lines.extend(["### 待确认是否同一问题", ""])
        for pair in pairs:
            lines.append(
                f"- 「{pair['a']}」（第 "
                f"{'、'.join(str(i) for i in pair['a_encounters'])} 次）与"
                f"「{pair['b']}」（第 "
                f"{'、'.join(str(i) for i in pair['b_encounters'])} 次）："
                f"{pair['reason']}"
            )
        lines.append("")
    return lines


def _render_prior_history(record: Mapping[str, Any]) -> list[str]:
    prior = record.get("prior_history_outside_this_hospital") or []
    if not prior:
        return []
    lines: list[str] = []
    lines.extend(f"- {normalize_text(str(item))}" for item in prior)
    lines.extend([
        "",
        "以上来自本次住院文书的「入院情况」叙述，尚未调取外院记录核对。",
        "",
    ])
    return lines


def _render_encounter_highlights(index: int, encounter: Mapping[str, Any]) -> list[str]:
    sections = encounter.get("sections") or {}
    provenance = encounter.get("provenance") or {}
    lines = [
        f"### 第 {index} 次：{_stamp(encounter.get('admission'))} → "
        f"{_stamp(encounter.get('discharge'))}"
        f"（{encounter.get('length_of_stay_days') or '—'} 天）",
        "",
    ]
    admission_diagnoses = (sections.get("admission_diagnoses") or {}).get("items") or []
    if admission_diagnoses:
        lines.extend(["**入院诊断**", ""])
        lines.extend(f"{position}. {item}"
                     for position, item in enumerate(admission_diagnoses, start=1))
        lines.append("")
    discharge_diagnoses = (sections.get("discharge_diagnoses") or {}).get("items") or []
    if discharge_diagnoses:
        lines.extend(["**出院诊断**", ""])
        lines.extend(f"{position}. {item}"
                     for position, item in enumerate(discharge_diagnoses, start=1))
        lines.append("")
    complaint = (sections.get("chief_complaint") or {}).get("text")
    if complaint:
        lines.extend([f"**主诉**　{complaint}", ""])
    situation = (sections.get("admission_situation") or {}).get("text")
    if situation:
        lines.extend([f"**入院情况**　{situation}", ""])
    course = (sections.get("hospital_course") or {}).get("text")
    if course:
        lines.extend([f"**住院经过**　{course}", ""])
    status = (sections.get("discharge_status") or {}).get("text")
    if status:
        lines.extend([f"**出院情况**　{status}", ""])

    findings = (sections.get("positive_findings") or {}).get("items") or []
    if findings:
        lines.extend(["**关键检查**", ""])
        lines.extend(f"- {item}" for item in findings)
        lines.append("")

    orders = (sections.get("discharge_orders") or {}).get("items") or []
    if orders:
        lines.extend(["**出院医嘱**", ""])
        lines.extend(f"{position}. {item}"
                     for position, item in enumerate(orders, start=1))
        lines.append("")

    lines.extend([
        f"> 依据文书：{provenance.get('document') or '未标注'}"
        f"（创建 {provenance.get('document_created_at') or '未标注'}，"
        f"作者 {provenance.get('author') or '未标注'}）",
        "",
    ])
    return lines


def _render_latest_medications(latest: Mapping[str, Any]) -> list[str]:
    section = (latest.get("sections") or {}).get("discharge_medications") or {}
    items = section.get("items") or []
    lines: list[str] = []
    if not items:
        lines.extend([
            "_本次采集的最近一次文书中没有带药清单，见文末缺口；"
            "此处不引用其他就诊的用药。_",
            "",
        ])
        return lines
    lines.extend([
        "| 药品 | 单次量 | 途径 | 频次 | 总量 | 解析 |",
        "|---|---|---|---|---|---|",
    ])
    for item in items:
        lines.append(
            f"| {item.get('name') or '—'} | {item.get('dose') or '—'} | "
            f"{item.get('route') or '—'} | {item.get('frequency') or '—'} | "
            f"{item.get('total') or '—'} | "
            f"{'已解析' if item.get('parsed') else '**待核对**'} |"
        )
    lines.append("")
    uncertain = section.get("unparsed") or []
    if uncertain:
        lines.extend(["标为「待核对」的行未能自动拆出剂量或总量，原文如下：", ""])
        lines.extend(f"- {line}" for line in uncertain)
        lines.append("")
    return lines


def _render_data_issues(record: Mapping[str, Any]) -> list[str]:
    lines: list[str] = ["### 采集与记录问题", ""]
    items: list[str] = []
    for index, encounter in enumerate(record.get("encounters") or [], start=1):
        for conflict in encounter.get("conflicts") or []:
            values = "；".join(
                f"{item.get('source')}={item.get('value')}"
                for item in conflict.get("values") or []
            )
            same_day = conflict.get("same_calendar_date")
            items.append(
                f"**冲突（第 {index} 次就诊）**{_label(conflict.get('field'))}：{values}"
                f"　{'日期相同、时刻不同' if same_day else '连日期也不同'}，"
                f"两处均已保留，未替你选定。"
            )
        for missing in encounter.get("missing") or []:
            items.append(
                f"**缺口（第 {index} 次就诊）**{_label(missing.get('field'))}："
                f"{missing.get('reason')}"
            )
    gaps = record.get("gaps") or []
    for gap in gaps:
        items.append(f"**采集范围**{normalize_text(str(gap))}")
    if not items:
        items.append("本次清洗未发现缺口或冲突。")
    lines.extend(f"- {item}" for item in items)
    lines.append("")
    return lines


def _render_open_questions(analysis: Mapping[str, Any],
                           record: Mapping[str, Any]) -> list[str]:
    lines: list[str] = ["### 需你判断的差异", ""]
    questions: list[str] = []
    dropped = _labels_missing_from_latest(analysis, record)
    if dropped:
        labels, earlier_count, latest_count = dropped
        questions.append(
            "以下诊断出现在较早的就诊文书中、最近一次就诊未再出现："
            f"{'、'.join(labels)}。最近一次出院诊断列了 {latest_count} 条，"
            f"较早一次列了 {earlier_count} 条——是已缓解、已治愈，"
            "还是最近一次文书未逐项列全？"
        )
    for pair in analysis.get("needs_confirmation") or []:
        questions.append(
            f"「{pair['a']}」与「{pair['b']}」是否同一问题？"
            f"两者在不同就诊的文书里出现，归并会改变问题清单的条数。"
        )
    for index, delta in enumerate(analysis.get("per_encounter_delta") or [], start=1):
        added = "、".join(delta.get("added_at_discharge") or []) or "—"
        removed = "、".join(delta.get("only_in_admission") or []) or "—"
        questions.append(
            f"第 {delta['encounter']} 次就诊的入院→出院诊断差异：出院新增「{added}」，"
            f"仅入院有「{removed}」。{delta.get('note')}"
        )
    for index, encounter in enumerate(record.get("encounters") or [], start=1):
        for conflict in encounter.get("conflicts") or []:
            questions.append(
                f"第 {index} 次就诊的{_label(conflict.get('field'))}以哪个来源为准？"
                f"出院记录与页面记录的时刻不同。"
            )
    if record.get("prior_history_outside_this_hospital"):
        questions.append("是否需要调取院外记录，核对本次文书里转述的外院手术与复查经过？")
    if not questions:
        questions.append("未发现需要判定的差异。")
    lines.extend(f"{position}. {question}"
                 for position, question in enumerate(questions, start=1))
    lines.append("")
    return lines


def _labels_missing_from_latest(analysis: Mapping[str, Any],
                                record: Mapping[str, Any]) -> tuple[list[str], int, int] | None:
    """Return earlier diagnoses absent from the latest encounter, with counts.

    Only reported when the latest encounter lists fewer diagnoses than an
    earlier one, so a record whose diagnoses are stable stays quiet.
    """

    encounters = record.get("encounters") or []
    if len(encounters) < 2:
        return None
    latest_index = len(encounters)
    latest_section = (encounters[-1].get("sections") or {}).get("discharge_diagnoses") or {}
    latest_labels = latest_section.get("items") or []
    earlier_max = 0
    earlier_labels: list[str] = []
    for encounter in encounters[:-1]:
        section = (encounter.get("sections") or {}).get("discharge_diagnoses") or {}
        items = section.get("items") or []
        earlier_max = max(earlier_max, len(items))
        earlier_labels.extend(items)
    if not earlier_labels or len(latest_labels) >= earlier_max:
        return None
    latest_keys = {_diagnosis_key(item) for item in latest_labels}
    dropped = dedupe_preserving_order(
        [item for item in earlier_labels if _diagnosis_key(item) not in latest_keys]
    )
    if not dropped:
        return None
    return dropped, earlier_max, len(latest_labels)
