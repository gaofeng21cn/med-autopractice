"""Pure-stdlib HL7 v2.x ORU/MDM parser and read-only observation extractor."""

from __future__ import annotations

from dataclasses import dataclass

from med_autopractice.domain.task import Fact, SubjectRef


@dataclass(frozen=True)
class HL7Segment:
    name: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class HL7Message:
    message_type: str
    control_id: str
    patient_id: str
    encounter_id: str
    segments: tuple[HL7Segment, ...]


def _field(seg: HL7Segment, index: int) -> str:
    """Return 1-based HL7 field text (empty string if missing)."""
    return seg.fields[index] if index < len(seg.fields) else ""


def _comp(field_val: str, index: int = 0) -> str:
    parts = field_val.split("^")
    return parts[index] if index < len(parts) else ""


def parse_hl7_message(raw_message: str) -> HL7Message:
    """Parse an ER7-delimited HL7 v2.x message into bound patient/encounter/segments."""
    if not isinstance(raw_message, str) or not raw_message.startswith("MSH"):
        raise ValueError("HL7 v2 message must begin with MSH segment")
    field_sep = raw_message[3]
    lines = [line.strip() for line in raw_message.replace("\r\n", "\r").replace("\n", "\r").split("\r") if line.strip()]
    segments: list[HL7Segment] = []
    for line in lines:
        if line.startswith("MSH"):
            # MSH-1 is the separator itself; split after MSH| so MSH-9 etc match 1-based indices.
            parts = ["MSH", field_sep, *line[4:].split(field_sep)]
        else:
            parts = line.split(field_sep)
        segments.append(HL7Segment(parts[0], tuple(parts)))

    msh = segments[0]
    msg_type = _field(msh, 9)
    control_id = _field(msh, 10)
    pid = next((s for s in segments if s.name == "PID"), None)
    pv1 = next((s for s in segments if s.name == "PV1"), None)
    if pid is None or pv1 is None:
        raise ValueError("HL7 message missing PID or PV1 binding segment")
    patient_id = _comp(_field(pid, 3), 0).strip()
    encounter_id = _comp(_field(pv1, 19), 0).strip()
    if not patient_id or not encounter_id:
        raise ValueError("HL7 message PID-3 or PV1-19 identifier is empty")
    return HL7Message(
        message_type=msg_type,
        control_id=control_id,
        patient_id=patient_id,
        encounter_id=encounter_id,
        segments=tuple(segments),
    )


class HL7ObservationSource:
    """Extract read-only clinical Facts from HL7 v2 ORU^R01 observations for a bound subject."""

    def __init__(self, site_id: str, *, source_system: str = "HL7v2-ORU"):
        if not site_id:
            raise ValueError("site_id is required")
        self.site_id = site_id
        self.source_system = source_system

    def extract_facts(self, subject: SubjectRef, raw_message: str) -> tuple[Fact, ...]:
        if subject.site_id != self.site_id:
            raise ValueError("subject belongs to another site")
        msg = parse_hl7_message(raw_message)
        if msg.patient_id != subject.patient_id or msg.encounter_id != subject.encounter_id:
            raise ValueError("HL7 PID-3 or PV1-19 differs from bound patient/encounter")

        facts: list[Fact] = []
        current_obr_time: str | None = None
        for seg in msg.segments:
            if seg.name == "OBR":
                current_obr_time = _field(seg, 7) or None
            elif seg.name == "OBX":
                obs_status = _field(seg, 11).strip().upper()
                if obs_status in {"D", "X", "I"}:
                    continue
                if obs_status not in {"F", "C", "P"}:
                    raise ValueError(f"unsupported OBX-11 result status: {obs_status!r}")
                obx_id = _field(seg, 1).strip() or str(len(facts) + 1)
                obs_code = _comp(_field(seg, 3), 0).strip()
                obs_label = _comp(_field(seg, 3), 1).strip() or obs_code or f"OBX-{obx_id}"
                obs_val = _field(seg, 5).strip()
                if not obs_val:
                    raise ValueError(f"OBX-{obx_id} missing observation value")
                unit = _comp(_field(seg, 6), 0).strip() or None
                ref_range = _field(seg, 7).strip() or None
                abnormal = _field(seg, 8).strip() or None
                obs_time = _field(seg, 14).strip() or current_obr_time
                mapped_status = "uncertain" if obs_status == "P" else "observed"
                src_ref = f"hl7://{self.site_id}/{msg.control_id or 'msg'}/OBX/{obx_id}"
                facts.append(Fact(
                    fact_id=f"hl7-obx-{obx_id}",
                    value=f"{obs_label}: {obs_val}",
                    source_ref=src_ref,
                    source_system=self.source_system,
                    source_object_ref=f"OBX/{obs_code or obx_id}",
                    effective_at=obs_time,
                    unit=unit,
                    status=mapped_status,
                    details={
                        "section": "观察",
                        "kind": "observation",
                        "code": obs_code,
                        "reference_range": ref_range,
                        "abnormal_flag": abnormal,
                    },
                ))
        return tuple(facts)
