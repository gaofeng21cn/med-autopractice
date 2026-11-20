"""Turn one compiled record into research-ready tables.

The clinical record keeps every value as the document wrote it. This module
adds the research view on top: it extracts fields into fixed columns, adds
normalized codes next to the original wording, and emits long tables keyed by
``subject_id`` so records from different patients can be concatenated without
renegotiating the schema.

Two rules keep the result trustworthy:

* A normalized code never replaces the source wording. Both stay in the row,
  so a reviewer can always see what the document actually said.
* A value that is not present is empty. Nothing is inferred from another visit,
  another field, or a plausible default.
"""

from __future__ import annotations

import csv
import hashlib
from io import StringIO
import re
from typing import Any, Mapping, Sequence

DATASET_VERSION = "map-research-dataset.v1"

#: Column order is part of the contract: a table file appends rows from other
#: patients by writing the same columns in the same order.
TABLES: dict[str, tuple[str, ...]] = {
    "subject": ("subject_id", "record_id", "site_id", "gender_code",
                "birth_date", "age_at_last_visit", "ethnicity", "nationality",
                "marital_status", "encounter_count"),
    "encounter": ("subject_id", "visit_seq", "encounter_id", "department",
                  "admission_datetime", "discharge_datetime", "los_days_recorded",
                  "los_days_computed", "bed_no", "source_document"),
    "condition": ("subject_id", "visit_seq", "phase", "seq_in_phase",
                  "term_raw", "term_norm", "first_discharge_diagnosis",
                  "added_during_stay", "distinct_visits"),
    "medication": ("subject_id", "visit_seq", "seq_in_visit", "drug_name",
                   "dose_value", "dose_unit", "route_raw", "route_code",
                   "frequency_raw", "frequency_code", "doses_per_day",
                   "total_quantity", "total_unit", "parse_status", "raw"),
    "measurement": ("subject_id", "visit_seq", "seq_in_visit", "performed_date",
                    "test_name", "modality_code", "result_text", "raw"),
    "procedure": ("subject_id", "visit_seq", "procedure_date",
                  "procedure_name", "anesthesia", "extraction", "source_text"),
    "qc_issue": ("subject_id", "visit_seq", "kind", "field", "detail",
                 "resolution"),
    "problem_list": ("subject_id", "term_norm", "term_raw", "visit_count",
                     "first_visit_seq", "last_visit_seq", "in_latest_visit",
                     "added_during_stay_visits"),
    "quality_summary": ("subject_id", "visits", "conditions", "medications",
                        "measurements", "procedures", "qc_issues",
                        "conditions_added_during_stay", "unparsed_medications",
                        "problems_needing_confirmation"),
}

_GENDER_CODES = {"男": "M", "男性": "M", "女": "F", "女性": "F"}

_ROUTE_CODES = {
    "口服": "PO", "静脉滴注": "IV_DRIP", "静滴": "IV_DRIP",
    "静脉注射": "IV_PUSH", "静推": "IV_PUSH", "皮下注射": "SC",
    "肌肉注射": "IM", "肌注": "IM", "雾化吸入": "INH", "舌下含服": "SL",
    "含服": "BUC", "外用": "TOP", "鼻饲": "NG",
}

_FREQUENCY_CODES = (
    (re.compile(r"^(每(?:日|天|1日)一?次|QD)$", re.I), "QD", 1.0),
    (re.compile(r"^(每(?:日|天)两?次|(?:每日|每天)2次|BID)$", re.I), "BID", 2.0),
    (re.compile(r"^(每(?:日|天)三?次|(?:每日|每天)3次|TID)$", re.I), "TID", 3.0),
    (re.compile(r"^(每(?:日|天)四?次|(?:每日|每天)4次|QID)$", re.I), "QID", 4.0),
    (re.compile(r"^(每晚(?:一次)?|QN)$", re.I), "QN", 1.0),
    (re.compile(r"^隔(?:日|天)一次$"), "QOD", 0.5),
    (re.compile(r"^按需$|^必要时$|^PRN$", re.I), "PRN", None),
)
_EVERY_HOURS = re.compile(r"^每\s*(\d+(?:\.\d+)?)\s*小时(?:一次)?$")

_DOSE_PARTS = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-zμ]+|片|粒|袋|支|丸|滴)$")
_UNIT_CANON = {"MG": "mg", "G": "g", "ML": "ml", "UG": "ug", "ΜG": "ug",
                "IU": "IU", "U": "U"}

_FINDING = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)\s+(?P<test>\S+?)\s+(?P<result>.+)$",
    re.S,
)

#: Modality is derived only from an explicit token in the test name; anything
#: else stays OTHER instead of guessing an examination type.
_MODALITY_TOKENS = (
    ("MR", ("MR", "磁共振")),
    ("CT", ("CT",)),
    ("ECG", ("心电图",)),
    ("US", ("彩超", "超声")),
    ("PFT", ("肺通气功能", "肺功能")),
    ("XR", ("X线", "胸片", "平片")),
    ("PATH", ("病理",)),
    ("LAB", ("化验", "检验", "血常规", "生化", "血气")),
)

_PROCEDURE_PATTERNS = (
    re.compile(r"(?:(?P<date>\d{4}-\d{1,2}-\d{1,2})\s*)?"
               r"(?P<anes>全麻|局麻)?下?\s*行\s*(?P<name>[^，。；]{2,60}?术)"),
    re.compile(r"(?:(?P<date>\d{4}-\d{1,2}-\d{1,2})\s*)?"
               r"(?P<anes>全麻|局麻)?下?\s*行\s*(?P<name>[^，。；]{2,40}?拔除)"),
)


def pseudonym(site_id: Any, inpatient_no: Any) -> str | None:
    """Return a stable per-site study identifier instead of a direct identifier.

    The digest is deterministic, so re-running the export keeps the same
    ``subject_id`` for the same hospital number while the tables themselves
    carry no name, identity-card number or phone.
    """

    if not site_id or not inpatient_no:
        return None
    digest = hashlib.sha256(f"{site_id}|{inpatient_no}".encode("utf-8")).hexdigest()
    return "S" + digest[:12]


def _date_part(value: Any) -> str | None:
    """Calendar date of a normalized stamp.

    Imported lazily because ``clinical_record`` renders this module's tables;
    a module-level import would make the two files circular.
    """

    from med_autopractice.records.clinical_record import date_part

    return date_part(_text(value) or None)


def _text(value: Any) -> str:
    """Plain text of a datetime field, which may be a ``{"datetime": ...}`` map."""

    if isinstance(value, Mapping):
        value = value.get("datetime") or value.get("calendar_date")
    return _normalize_text(value) if value else ""


def _normalize_text(value: Any) -> str:
    from med_autopractice.records.clinical_record import normalize_text

    return normalize_text(str(value))


def normalize_gender(value: Any) -> str | None:
    text = _normalize_text(str(value)) if value else ""
    return _GENDER_CODES.get(text, "U" if text else None)


def normalize_route(value: Any) -> str | None:
    text = _normalize_text(str(value)) if value else ""
    if not text:
        return None
    for name, code in _ROUTE_CODES.items():
        if name in text:
            return code
    return "OTHER"


def normalize_frequency(value: Any) -> tuple[str | None, float | None]:
    """Return ``(code, doses_per_day)``; unknown wording keeps its own code."""

    text = _normalize_text(str(value)) if value else ""
    if not text:
        return None, None
    compact = text.replace(" ", "")
    for pattern, code, per_day in _FREQUENCY_CODES:
        if pattern.match(compact):
            return code, per_day
    hours = _EVERY_HOURS.match(compact)
    if hours and float(hours.group(1)):
        interval = float(hours.group(1))
        return f"Q{int(interval) if interval.is_integer() else interval}H", 24 / interval
    return "OTHER", None


def split_dose(value: Any) -> tuple[float | None, str | None]:
    text = _normalize_text(str(value)) if value else ""
    match = _DOSE_PARTS.match(text) if text else None
    if not match:
        return None, None
    unit = match.group("unit")
    return float(match.group("value")), _UNIT_CANON.get(unit.upper(), unit)


def split_quantity(value: Any) -> tuple[float | None, str | None]:
    return split_dose(value)


def modality_code(test_name: Any) -> str:
    text = _normalize_text(str(test_name)) if test_name else ""
    for code, tokens in _MODALITY_TOKENS:
        if any(token in text for token in tokens):
            return code
    return "OTHER"


def age_at(birth_date: Any, on: Any) -> int | None:
    """Whole years between the birth date and the visit date, or ``None``."""

    born, moment = _date_part(birth_date), _date_part(on)
    if not born or not moment:
        return None
    try:
        born_year, born_month, born_day = (int(part) for part in born.split("-"))
        year, month, day = (int(part) for part in moment.split("-"))
    except ValueError:
        return None
    years = year - born_year - ((month, day) < (born_month, born_day))
    return years if years >= 0 else None


def compute_los_days(admission: Any, discharge: Any) -> float | None:
    """Calendar days between admission and discharge, rounded to 1 decimal."""

    import datetime

    start, end = _date_part(admission), _date_part(discharge)
    if not start or not end:
        return None
    try:
        first = datetime.date.fromisoformat(start)
        last = datetime.date.fromisoformat(end)
    except ValueError:
        return None
    days = (last - first).days
    return float(days) if days >= 0 else None


def build_dataset(record: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Build every research table for one record.

    Returns one list of rows per table; each row already carries the study
    identifier so callers can concatenate across patients.
    """

    patient = record.get("patient") or {}
    source = record.get("source_system") or {}
    encounters = record.get("encounters") or []
    site_id = record.get("profile_id") or source.get("system_id")
    subject_id = pseudonym(site_id, patient.get("inpatient_no"))

    tables: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLES}
    tables["subject"].append({
        "subject_id": subject_id,
        "record_id": record.get("record_id"),
        "site_id": site_id,
        "gender_code": normalize_gender(patient.get("gender")),
        "birth_date": _date_part(str(patient.get("birth_date"))) if patient.get("birth_date") else None,
        "age_at_last_visit": age_at(patient.get("birth_date"),
                                    encounters[-1].get("admission") if encounters else None),
        "ethnicity": patient.get("ethnicity"),
        "nationality": patient.get("nationality"),
        "marital_status": patient.get("marital_status"),
        "encounter_count": len(encounters),
    })

    for visit_seq, encounter in enumerate(encounters, start=1):
        sections = encounter.get("sections") or {}
        provenance = encounter.get("provenance") or {}
        admission, discharge = encounter.get("admission"), encounter.get("discharge")
        tables["encounter"].append({
            "subject_id": subject_id,
            "visit_seq": visit_seq,
            "encounter_id": encounter.get("encounter_id"),
            "department": encounter.get("department"),
            "admission_datetime": _stamp(admission),
            "discharge_datetime": _stamp(discharge),
            "los_days_recorded": encounter.get("length_of_stay_days"),
            "los_days_computed": compute_los_days(admission, discharge),
            "bed_no": encounter.get("bed_no"),
            "source_document": provenance.get("document"),
        })
        _condition_rows(tables, subject_id, visit_seq, sections)
        _medication_rows(tables, subject_id, visit_seq, sections)
        _measurement_rows(tables, subject_id, visit_seq, sections)
        _procedure_rows(tables, subject_id, visit_seq, encounter, sections)
        _qc_rows(tables, subject_id, visit_seq, encounter)

    prior = record.get("prior_history_outside_this_hospital") or []
    for item in prior:
        tables["qc_issue"].append({
            "subject_id": subject_id, "visit_seq": None, "kind": "prior_history",
            "field": "prior_history_outside_this_hospital",
            "detail": _normalize_text(str(item)),
            "resolution": "external records not retrieved",
        })
    for gap in record.get("gaps") or []:
        tables["qc_issue"].append({
            "subject_id": subject_id, "visit_seq": None, "kind": "collection_gap",
            "field": "collection_scope", "detail": _normalize_text(str(gap)),
            "resolution": "not collected in this round",
        })

    _problem_rows(tables, subject_id, record)
    _quality_row(tables, subject_id, record)
    return tables


def _stamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = value.get("datetime") or value.get("calendar_date")
    text = _normalize_text(str(value))
    return text.replace("T", " ") if text else None


def _condition_rows(tables: dict[str, list[dict[str, Any]]], subject_id: str | None,
                    visit_seq: int, sections: Mapping[str, Any]) -> None:
    admission = [(item, "admission") for item in
                 (sections.get("admission_diagnoses") or {}).get("items") or []]
    discharge = [(item, "discharge") for item in
                 (sections.get("discharge_diagnoses") or {}).get("items") or []]
    admission_keys = {_term_key(item) for item, _ in admission}
    discharge_keys = {_term_key(item) for item, _ in discharge}
    for phase, entries in (("admission", admission), ("discharge", discharge)):
        for seq_in_phase, (term, _) in enumerate(entries, start=1):
            key = _term_key(term)
            tables["condition"].append({
                "subject_id": subject_id,
                "visit_seq": visit_seq,
                "phase": phase,
                "seq_in_phase": seq_in_phase,
                "term_raw": term,
                "term_norm": key,
                "first_discharge_diagnosis": phase == "discharge" and seq_in_phase == 1,
                "added_during_stay": phase == "discharge" and key not in admission_keys,
            })


def _medication_rows(tables: dict[str, list[dict[str, Any]]], subject_id: str | None,
                     visit_seq: int, sections: Mapping[str, Any]) -> None:
    section = sections.get("discharge_medications") or {}
    for seq_in_visit, item in enumerate(section.get("items") or [], start=1):
        dose_value, dose_unit = split_dose(item.get("dose"))
        total_value, total_unit = split_quantity(item.get("total"))
        frequency_code, doses_per_day = normalize_frequency(item.get("frequency"))
        tables["medication"].append({
            "subject_id": subject_id,
            "visit_seq": visit_seq,
            "seq_in_visit": seq_in_visit,
            "drug_name": item.get("name"),
            "dose_value": dose_value,
            "dose_unit": dose_unit,
            "route_raw": item.get("route"),
            "route_code": normalize_route(item.get("route")),
            "frequency_raw": item.get("frequency"),
            "frequency_code": frequency_code,
            "doses_per_day": doses_per_day,
            "total_quantity": total_value,
            "total_unit": total_unit,
            "parse_status": "parsed" if item.get("parsed") else "unparsed",
            "raw": item.get("raw"),
        })


def _measurement_rows(tables: dict[str, list[dict[str, Any]]], subject_id: str | None,
                      visit_seq: int, sections: Mapping[str, Any]) -> None:
    for seq_in_visit, raw in enumerate(
            (sections.get("positive_findings") or {}).get("items") or [], start=1):
        match = _FINDING.match(raw)
        performed = match.group("date") if match else None
        test = match.group("test") if match else None
        result = match.group("result") if match else None
        tables["measurement"].append({
            "subject_id": subject_id,
            "visit_seq": visit_seq,
            "seq_in_visit": seq_in_visit,
            "performed_date": _date_part(performed) if performed else None,
            "test_name": test,
            "modality_code": modality_code(test or raw),
            "result_text": result if match else raw,
            "raw": raw,
        })


def _procedure_rows(tables: dict[str, list[dict[str, Any]]], subject_id: str | None,
                    visit_seq: int, encounter: Mapping[str, Any],
                    sections: Mapping[str, Any]) -> None:
    course = (sections.get("hospital_course") or {}).get("text") or ""
    seen: set[str] = set()
    for pattern in _PROCEDURE_PATTERNS:
        for match in pattern.finditer(course):
            name = _normalize_text(match.group("name"))
            if not name or name in seen:
                continue
            seen.add(name)
            date = match.group("date")
            tables["procedure"].append({
                "subject_id": subject_id,
                "visit_seq": visit_seq,
                "procedure_date": _date_part(_pad_date(date)) if date else None,
                "procedure_name": name,
                "anesthesia": match.group("anes"),
                "extraction": "regex_from_hospital_course",
                "source_text": _normalize_text(course),
            })


def _pad_date(value: str) -> str:
    parts = value.split("-")
    if len(parts) == 3:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return value


def _qc_rows(tables: dict[str, list[dict[str, Any]]], subject_id: str | None,
             visit_seq: int, encounter: Mapping[str, Any]) -> None:
    for conflict in encounter.get("conflicts") or []:
        values = "; ".join(
            f"{item.get('source')}={item.get('value')}"
            for item in conflict.get("values") or []
        )
        tables["qc_issue"].append({
            "subject_id": subject_id, "visit_seq": visit_seq, "kind": "conflict",
            "field": conflict.get("field"), "detail": values,
            "resolution": conflict.get("resolution"),
        })
    for missing in encounter.get("missing") or []:
        tables["qc_issue"].append({
            "subject_id": subject_id, "visit_seq": visit_seq, "kind": "missing",
            "field": missing.get("field"), "detail": missing.get("reason"),
            "resolution": "not fabricated from another source",
        })


def _problem_rows(tables: dict[str, list[dict[str, Any]]], subject_id: str | None,
                  record: Mapping[str, Any]) -> None:
    from med_autopractice.records.clinical_record import build_problem_list

    analysis = build_problem_list(record)
    for problem in analysis.get("problems") or []:
        visits = list(problem.get("encounters") or [])
        tables["problem_list"].append({
            "subject_id": subject_id,
            "term_norm": _term_key(problem.get("label")),
            "term_raw": problem.get("label"),
            "visit_count": len(visits),
            "first_visit_seq": min(visits) if visits else None,
            "last_visit_seq": max(visits) if visits else None,
            "in_latest_visit": problem.get("in_latest_encounter"),
            "added_during_stay_visits": ",".join(
                str(index) for index in problem.get("added_during_stay") or []),
        })


def _quality_row(tables: dict[str, list[dict[str, Any]]], subject_id: str | None,
                 record: Mapping[str, Any]) -> None:
    """One row per subject summarizing what a reviewer must weigh."""

    from med_autopractice.records.clinical_record import build_problem_list

    analysis = build_problem_list(record)
    medications = tables["medication"]
    tables["quality_summary"].append({
        "subject_id": subject_id,
        "visits": len(tables["encounter"]),
        "conditions": len(tables["condition"]),
        "medications": len(medications),
        "measurements": len(tables["measurement"]),
        "procedures": len(tables["procedure"]),
        "qc_issues": len(tables["qc_issue"]),
        "conditions_added_during_stay": sum(
            1 for row in tables["condition"] if row.get("added_during_stay")),
        "unparsed_medications": sum(
            1 for row in medications if row.get("parse_status") != "parsed"),
        "problems_needing_confirmation": len(
            analysis.get("needs_confirmation") or []),
    })


def _term_key(value: Any) -> str:
    text = _normalize_text(str(value or ""))
    return re.sub(r"[\s、，,。；;：:（）()\[\]【】\-—_·]+", "", text).lower()


def render_table_csv(table: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """Render one table as CSV using the contract column order.

    Extra keys are ignored rather than appended, so two subjects always produce
    the same header even if one record carried an extra field.
    """

    columns = TABLES.get(table)
    if columns is None:
        raise ValueError(f"unknown table: {table}")
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: _cell(row.get(name)) for name in columns})
    return buffer.getvalue()


def _cell(value: Any) -> Any:
    """Keep booleans and numbers machine-readable; empty means "not present"."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def table_summary(dataset: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, int]:
    return {name: len(rows) for name, rows in dataset.items()}


def render_research_record(record: Mapping[str, Any]) -> list[str]:
    """Render the research tables as readable markdown.

    The same tables are also written as CSV for concatenation across subjects;
    this view exists so a clinician can check the extracted fields directly.
    """

    dataset = build_dataset(record)
    subject = (dataset["subject"] or [{}])[0]
    lines: list[str] = [
        "字段已按固定列抽取，并补上与原文并列的归一化编码；"
        "归一化不会替换原文，缺失一律留空、不推断。"
        "同一份数据同时导出为 CSV，多例可直接纵向合并。",
        "",
        f"**研究编号**　{subject.get('subject_id') or '—'}"
        f"（由站点与住院号派生，不含姓名/证件号/电话）",
        f"**例数**　1 例　**就诊次数**　{subject.get('encounter_count') or 0}",
        "",
    ]
    lines.extend(_table_block("受试者（subject）", ("subject_id", "gender_code",
        "birth_date", "age_at_last_visit", "ethnicity", "encounter_count"),
        dataset["subject"], {
            "subject_id": "研究编号", "gender_code": "性别码", "birth_date": "出生日期",
            "age_at_last_visit": "末次就诊年龄", "ethnicity": "民族",
            "encounter_count": "就诊次数"}))
    lines.extend(_table_block("就诊（encounter）", ("visit_seq", "encounter_id",
        "department", "admission_datetime", "discharge_datetime",
        "los_days_recorded", "los_days_computed", "bed_no"),
        dataset["encounter"], {
            "visit_seq": "次序", "encounter_id": "就诊标识", "department": "科室",
            "admission_datetime": "入院", "discharge_datetime": "出院",
            "los_days_recorded": "住院天数(记录)", "los_days_computed": "住院天数(按日期算)",
            "bed_no": "床位"}))
    lines.extend(_table_block("诊断（condition）", ("visit_seq", "phase",
        "seq_in_phase", "term_raw", "added_during_stay", "first_discharge_diagnosis"),
        dataset["condition"], {
            "visit_seq": "次序", "phase": "阶段", "seq_in_phase": "序",
            "term_raw": "诊断（原文）", "added_during_stay": "住院期间新增",
            "first_discharge_diagnosis": "首位出院诊断"}, {
            "phase": {"admission": "入院", "discharge": "出院"}}))
    lines.extend(_table_block("用药（medication）", ("visit_seq", "seq_in_visit",
        "drug_name", "dose_value", "dose_unit", "route_code", "frequency_code",
        "doses_per_day", "total_quantity", "total_unit", "parse_status"),
        dataset["medication"], {
            "visit_seq": "次序", "seq_in_visit": "序", "drug_name": "药品",
            "dose_value": "单次量", "dose_unit": "单位", "route_code": "途径码",
            "frequency_code": "频次码", "doses_per_day": "次/日",
            "total_quantity": "总量", "total_unit": "总量单位",
            "parse_status": "解析"}))
    lines.extend(_table_block("检查检验（measurement）", ("visit_seq", "seq_in_visit",
        "performed_date", "test_name", "modality_code", "result_text"),
        dataset["measurement"], {
            "visit_seq": "次序", "seq_in_visit": "序", "performed_date": "检查日期",
            "test_name": "检查项目", "modality_code": "类别码",
            "result_text": "结果（原文）"}))
    lines.extend(_table_block("手术与操作（procedure）", ("visit_seq",
        "procedure_date", "procedure_name", "anesthesia", "extraction"),
        dataset["procedure"], {
            "visit_seq": "次序", "procedure_date": "日期", "procedure_name": "名称",
            "anesthesia": "麻醉", "extraction": "提取方式"}))
    return lines


def _table_block(title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]],
                 headers: Mapping[str, str],
                 value_maps: Mapping[str, Mapping[str, str]] | None = None
                 ) -> list[str]:
    lines = [f"### {title}", ""]
    if not rows:
        lines.extend(["_本次采集没有该类数据。_", ""])
        return lines
    lines.append("| " + " | ".join(headers.get(name, name) for name in columns) + " |")
    lines.append("|" + "---|" * len(columns))
    for row in rows:
        cells = []
        for name in columns:
            raw = row.get(name)
            mapping = (value_maps or {}).get(name) or {}
            if isinstance(raw, bool):
                # The readable view spells these out; the CSV keeps true/false
                # so downstream code can parse them.
                cells.append("是" if raw else "否")
                continue
            value = _cell(raw)
            cells.append(mapping.get(value, value) if isinstance(value, str) else value)
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    lines.append("")
    return lines
