"""Sourced work products for history, quality control, cohorts and follow-up."""

from datetime import date, timedelta

from med_autopractice.medicine.proposals import facts_of, text
from med_autopractice.records.clinical_record import build_record, validate_record


def history(request, snapshot) -> dict:
    collected = facts_of(snapshot.facts, "history_collection")
    if not collected:
        raise ValueError("history requires a sourced collection pack")
    encounters = {}
    gaps = []
    base = collected[0].details["collection"]
    for fact in collected:
        collection = fact.details["collection"]
        if collection.get("patient") != base.get("patient") or collection.get("site_id") != base.get("site_id"):
            raise ValueError("history pages span different patient identities")
        for gap in collection.get("gaps", []):
            if gap not in gaps:
                gaps.append(gap)
        for row in collection["encounters"]:
            if not isinstance(row, dict):
                raise ValueError("invalid source encounter")
            key = row.get("encounter_id", row.get("seq"))
            if type(key) not in {str, int} or not key:
                raise ValueError("source encounter needs a stable identity")
            if key in encounters and row != encounters[key]:
                raise ValueError("conflicting duplicate encounter")
            encounters[key] = row
    record = build_record({**base, "encounters": list(encounters.values()), "gaps": gaps},
                          record_id=request.request_id)
    if issues := validate_record(record):
        raise ValueError(f"invalid structured history: {len(issues)} validation issues")
    return {"type": "history", "record": record}


def document_work(request, snapshot, rule) -> dict:
    deadline_mode = request.capability_id == "track_document_deadlines"
    days = rule.get("deadline_days", {})
    required = rule.get("required_documents")
    if (not isinstance(required, list) or not required or
            any(not isinstance(name, str) or not name.strip() for name in required)):
        raise ValueError("document rule must specify required documents")
    as_of = date.fromisoformat(text(request.inputs.get("as_of"), "as_of date")) if deadline_mode else None
    documents = facts_of(snapshot.facts, "document_status")
    triggers = facts_of(snapshot.facts, "document_trigger") if deadline_mode else []
    if deadline_mode and not isinstance(days, dict):
        raise ValueError("deadline rules must map names to intervals")
    rows = []
    for name in dict.fromkeys(required):
        matches = [fact for fact in documents if fact.details.get("document") == name]
        if len(matches) > 1:
            status, refs = "needs_reconciliation", [f.source_ref for f in matches]
        elif not matches:
            status, refs = "not_found", []
        else:
            status, refs = matches[0].details.get("state"), [matches[0].source_ref]
            if status not in {"draft", "signed", "missing", "not_applicable"}:
                status = "needs_verification"
        row = {"document": name, "status": status, "source_refs": refs}
        if deadline_mode and status not in {"signed", "not_applicable"}:
            duration = days.get(name)
            candidates = [f for f in (*matches, *triggers) if f.details.get("document") == name
                          and f.details.get("trigger_on")]
            anchors = {date.fromisoformat(f.details["trigger_on"]) for f in candidates}
            if type(duration) is int and duration >= 0 and len(anchors) == 1:
                due = next(iter(anchors)) + timedelta(days=duration)
                row["source_refs"] = list(dict.fromkeys((*refs, *(f.source_ref for f in candidates))))
                row.update(due_on=due.isoformat(), timing="overdue" if due < as_of else "due" if due == as_of else "upcoming")
            else:
                row["timing"] = "needs_evidence"
        rows.append(row)
    return {"type": "document_deadlines" if deadline_mode else "discharge_review", "documents": rows}


def cohort(snapshot, rule) -> dict:
    criteria = rule.get("include")
    exclusions = rule.get("exclude", {})
    if not isinstance(criteria, dict) or not criteria or not isinstance(exclusions, dict):
        raise ValueError("cohort rule requires explicit inclusion criteria")
    observations = facts_of(snapshot.facts, "cohort_field")

    def match(field, expected):
        values = [f.details.get("value") for f in observations if f.details.get("field") == field
                  and f.details.get("value") is not None]
        if not values or any(value != values[0] for value in values):
            return None
        return values[0] == expected

    required = [match(field, value) for field, value in criteria.items()]
    excluded = [match(field, value) for field, value in exclusions.items()]
    status = "excluded" if False in required or True in excluded else "included"
    if status == "included" and (None in required or None in excluded):
        status = "needs_verification"
    return {"type": "cohort_decision", "status": status, "source_refs": [f.source_ref for f in observations]}


def followup(request, snapshot, rule) -> dict:
    as_of = date.fromisoformat(text(request.inputs.get("as_of"), "as_of date"))
    intervals = rule.get("intervals")
    if not isinstance(intervals, dict) or not intervals:
        raise ValueError("follow-up rule requires source events and intervals")
    events = facts_of(snapshot.facts, "followup_event")
    rows = []
    for name, days in intervals.items():
        if type(days) is not int or days < 0:
            raise ValueError("invalid follow-up interval")
        matches = [f for f in events if f.details.get("event") == name and f.details.get("on")]
        anchors = [f for f in matches if f.details.get("state") == "trigger"]
        completions = [f for f in matches if f.details.get("state") == "completed"]
        observed = lambda fact: date.fromisoformat(fact.details["on"])
        anchors = [f for f in anchors if observed(f) <= as_of]
        if not anchors:
            rows.append({"event": name, "status": "needs_evidence", "source_refs": []})
            continue
        anchor = max(anchors, key=observed)
        anchor_date = observed(anchor)
        due = anchor_date + timedelta(days=days)
        completions = [f for f in completions if anchor_date <= observed(f) <= as_of]
        refs = [anchor.source_ref]
        if completions:
            refs.append(max(completions, key=observed).source_ref)
        status = "completed" if completions else "overdue" if due < as_of else "due" if due == as_of else "upcoming"
        rows.append({"event": name, "status": status, "due_on": due.isoformat(), "source_refs": refs})
    return {"type": "followup_schedule", "events": rows}
