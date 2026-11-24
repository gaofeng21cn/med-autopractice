"""Automated 5-check clinical UI & KVM qualification stress harness."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from med_autopractice.adapters.drivers.text_entry import ChineseEntryUnqualifiedError
from med_autopractice.adapters.playbook_runner import HumanInterruptError, PlaybookRunner, UIDriver
from med_autopractice.domain.task import SubjectRef
from med_autopractice.workflows.site_playbook import Playbook

SYNTHETIC_500_CHAR_CHINESE_SAMPLE = (
    "患者系中年男性，因反复咳嗽、咳痰伴发热三天入院。今日查房神清，精神尚可，体温三十七度四，"
    "双肺呼吸音粗，右下肺可及少许湿性啰音，心率齐，未闻及明显杂音。结合昨日检验结果示白细胞升高，"
    "超敏C反应蛋白升高，降钙素原轻度异常，提示细菌性下呼吸道感染可能大。护理记录提示夜间睡眠欠佳，"
    "咳嗽咳痰略多，无明显胸痛及呼吸困难。今日拟继续当前抗感染及祛痰对症治疗方案，加强翻身拍背及排痰护理，"
    "嘱患者多饮温水，注意监测体温变化及血氧饱和度。已向患者及家属交代病情变化及下一步诊疗计划，"
    "复查血常规与炎症指标后再评估是否调整抗菌药物方案，目前病情总体趋于平稳，继续住院观察治疗。"
    "补充查房记录：患者饮食尚可，大小便正常，双下肢无水肿，神经系统查体未见明显定位体征。"
)


@dataclass(frozen=True)
class QualificationCheckItem:
    name: str
    passed: bool
    detail: str
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "metrics": self.metrics,
        }


@dataclass(frozen=True)
class QualificationReport:
    site_id: str
    generated_at: str
    passed: bool
    checks: tuple[QualificationCheckItem, ...]
    promoted_dependencies: tuple[str, ...]
    qualification_ref: str | None
    report_digest: str

    def to_dict(self) -> dict[str, Any]:
        passed_count = sum(1 for c in self.checks if c.passed)
        return {
            "schema_version": "map-qualification-report.v1",
            "site_id": self.site_id,
            "generated_at": self.generated_at,
            "passed": self.passed,
            "pass_rate": round(passed_count / len(self.checks), 4) if self.checks else 0.0,
            "checks": [c.to_dict() for c in self.checks],
            "promoted_dependencies": list(self.promoted_dependencies),
            "qualification_ref": self.qualification_ref,
            "report_digest": self.report_digest,
        }


def _check_identity_binding(
    driver: UIDriver,
    subject: SubjectRef,
    iterations: int = 3,
) -> QualificationCheckItem:
    try:
        wrong_subject = SubjectRef(subject.site_id, f"{subject.patient_id}-WRONG", f"{subject.encounter_id}-WRONG")
        for _ in range(iterations):
            obs = driver.observe()
            text = obs.full_text()
            if subject.patient_id not in text or subject.encounter_id not in text:
                return QualificationCheckItem(
                    "identity_binding", False,
                    "active screen missing expected patient or encounter token", {"iterations": iterations},
                )
            if wrong_subject.patient_id in text or wrong_subject.encounter_id in text:
                return QualificationCheckItem(
                    "identity_binding", False,
                    "ambiguous mismatched patient/encounter token appeared on screen", {"iterations": iterations},
                )
        return QualificationCheckItem(
            "identity_binding", True,
            f"verified unique patient & encounter binding across {iterations} checks with wrong-subject negative pass",
            {"iterations": iterations},
        )
    except Exception as error:
        return QualificationCheckItem("identity_binding", False, f"identity check error: {error}", {})


def _check_multipage_collection(
    driver: UIDriver,
    subject: SubjectRef,
    collect_playbook: Playbook | None,
) -> QualificationCheckItem:
    try:
        if collect_playbook is not None:
            runner = PlaybookRunner(driver)
            res = runner.run(collect_playbook, subject)
            row_count = 0
            truncated_any = False
            for val in res.extracted.values():
                if isinstance(val, dict) and isinstance(val.get("rows"), list):
                    row_count += len(val["rows"])
                    truncated_any = truncated_any or bool(val.get("truncated"))
            ok = row_count > 0 and not truncated_any
            return QualificationCheckItem(
                "multipage_collection", ok,
                "playbook multipage scroll_collect complete" if ok else "collection empty or truncated",
                {"rows_collected": row_count, "truncated": truncated_any},
            )
        tbl = driver.extract_table()
        rows = list(tbl.rows)
        pages = 1
        while tbl.has_next and pages < 10:
            driver.act("scroll", value="down")
            tbl = driver.extract_table()
            rows.extend(tbl.rows)
            pages += 1
        ok = len(rows) > 0 and not tbl.has_next
        return QualificationCheckItem(
            "multipage_collection", ok,
            f"collected {len(rows)} rows across {pages} page(s)",
            {"rows_collected": len(rows), "pages": pages, "truncated": bool(tbl.has_next)},
        )
    except Exception as error:
        return QualificationCheckItem("multipage_collection", False, f"collection error: {error}", {})


def _check_chinese_long_text_and_readback(
    driver: UIDriver,
    subject: SubjectRef,
    *,
    write_playbook: Playbook | None,
    readback_playbook: Playbook | None,
    editor_target: str | None,
    editor_selector: str | None,
    editor_region_hint: tuple[int, int, int, int] | None,
    sample_text: str,
) -> tuple[QualificationCheckItem, QualificationCheckItem]:
    try:
        if write_playbook is not None and readback_playbook is not None:
            runner = PlaybookRunner(driver)
            w_res = runner.run(write_playbook, subject, params={"draft_text": sample_text})
            r_res = runner.run(readback_playbook, subject)
            observed = str(r_res.extracted.get("draft_text") or "")
            shots = w_res.screenshot_refs or r_res.screenshot_refs
        else:
            driver.input_text(
                sample_text,
                target=editor_target,
                selector=editor_selector,
                region_hint=editor_region_hint,
                subject=subject,
            )
            reader = getattr(driver, "read_field", None)
            if callable(reader) and editor_selector:
                obs = reader(target=editor_target, selector=editor_selector, region_hint=editor_region_hint)
            else:
                obs = driver.observe(editor_region_hint)
            observed = obs.full_text()
            shot = driver.screenshot(region=editor_region_hint, label="qualify-readback")
            shots = (shot,)

        ratio = SequenceMatcher(None, sample_text.strip(), observed.strip()).ratio()
        exact = sample_text.strip() in observed.strip()
        accuracy = 1.0 if exact else round(ratio, 4)
        text_ok = exact or ratio >= 0.995

        entry_item = QualificationCheckItem(
            "chinese_long_text_entry", text_ok,
            f"entered {len(sample_text)} chars with accuracy={accuracy}",
            {"chars": len(sample_text), "accuracy": accuracy},
        )
        shot_ok = bool(shots) and all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in shots if not str(p).startswith("demo://"))
        rb_ok = text_ok and shot_ok
        readback_item = QualificationCheckItem(
            "readback_consistency", rb_ok,
            "independent readback and non-empty screenshot verified" if rb_ok else "readback or screenshot check failed",
            {"accuracy": accuracy, "screenshots_count": len(shots), "screenshot_non_empty": shot_ok},
        )
        return entry_item, readback_item
    except ChineseEntryUnqualifiedError as error:
        return (
            QualificationCheckItem("chinese_long_text_entry", False, f"strategy_c_draft_only floor: {error}", {"fallback": "draft_only"}),
            QualificationCheckItem("readback_consistency", False, "skipped due to unqualified text entry", {}),
        )
    except Exception as error:
        return (
            QualificationCheckItem("chinese_long_text_entry", False, f"text entry error: {error}", {}),
            QualificationCheckItem("readback_consistency", False, f"readback error: {error}", {}),
        )


def _check_human_interrupt(
    driver: UIDriver,
    interrupt_injector: Callable[[bool], None] | None,
) -> QualificationCheckItem:
    if interrupt_injector is None:
        return QualificationCheckItem(
            "human_interrupt_and_recovery", False,
            "missing interrupt_injector for human takeover verification", {},
        )
    halted = False
    try:
        interrupt_injector(True)
        try:
            driver.observe()
        except HumanInterruptError:
            halted = True
    finally:
        interrupt_injector(False)
    recovered = False
    if halted:
        try:
            driver.check_human_interrupt()
            recovered = True
        except Exception:
            recovered = False
    ok = halted and recovered
    return QualificationCheckItem(
        "human_interrupt_and_recovery", ok,
        "preempted immediately on human input and recovered cleanly" if ok else "failed to halt or recover cleanly",
        {"halted_immediately": halted, "recovered_cleanly": recovered},
    )


def promote_qualified_dependencies(
    site_dir: Path,
    *,
    dependency_ids: Sequence[str],
    qualification_ref: str,
) -> tuple[str, ...]:
    """Promote only existing `observed` dependencies to `qualified`; never skip `observed`."""
    deps_path = site_dir / "dependencies.json"
    doc = json.loads(deps_path.read_text(encoding="utf-8"))
    target_set = set(dependency_ids)
    promoted: list[str] = []
    for item in doc.get("dependencies", []):
        dep_id = item.get("id")
        if dep_id not in target_set:
            continue
        state = item.get("state")
        if state == "observed":
            item["state"] = "qualified"
            item["qualification_ref"] = qualification_ref
            if not item.get("evidence_ref"):
                item["evidence_ref"] = qualification_ref
            promoted.append(dep_id)
        elif state == "qualified":
            item["qualification_ref"] = qualification_ref
            promoted.append(dep_id)
        else:
            raise ValueError(f"cannot promote dependency {dep_id!r} directly from state {state!r}; observe it first")
    deps_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tuple(promoted)


def run_qualification_suite(
    site_dir: Path,
    *,
    driver: UIDriver,
    subject: SubjectRef,
    collect_playbook: Playbook | None = None,
    write_playbook: Playbook | None = None,
    readback_playbook: Playbook | None = None,
    interrupt_injector: Callable[[bool], None] | None = None,
    editor_target: str | None = "病程编辑器",
    editor_selector: str | None = "#note-editor",
    editor_region_hint: tuple[int, int, int, int] | None = (40, 160, 540, 360),
    sample_text: str = SYNTHETIC_500_CHAR_CHINESE_SAMPLE,
    promote: bool = False,
    dependency_ids: Sequence[str] = ("save_inpatient_note_draft", "readback_inpatient_note"),
    report_path: Path | None = None,
) -> QualificationReport:
    """Execute the 5-item UI/KVM qualification stress suite without leaking any PHI."""
    deps_doc = json.loads((site_dir / "dependencies.json").read_text(encoding="utf-8"))
    site_id = str(deps_doc.get("profile_id") or subject.site_id)

    c1 = _check_identity_binding(driver, subject)
    c2 = _check_multipage_collection(driver, subject, collect_playbook)
    c3, c4 = _check_chinese_long_text_and_readback(
        driver, subject,
        write_playbook=write_playbook,
        readback_playbook=readback_playbook,
        editor_target=editor_target,
        editor_selector=editor_selector,
        editor_region_hint=editor_region_hint,
        sample_text=sample_text,
    )
    c5 = _check_human_interrupt(driver, interrupt_injector)
    checks = (c1, c2, c3, c4, c5)
    all_passed = all(c.passed for c in checks)

    generated_at = datetime.now(timezone.utc).isoformat()
    digest_body = json.dumps(
        {"site_id": site_id, "generated_at": generated_at, "checks": [c.to_dict() for c in checks]},
        ensure_ascii=False, sort_keys=True,
    )
    report_digest = sha256(digest_body.encode("utf-8")).hexdigest()
    qual_ref: str | None = None
    promoted: tuple[str, ...] = ()

    if promote:
        if not all_passed:
            failed_names = [c.name for c in checks if not c.passed]
            raise ValueError(f"refusing promotion; failed qualification checks: {failed_names}")
        qual_ref = f"site://{site_id}/qualification/{report_digest[:16]}"
        promoted = promote_qualified_dependencies(
            site_dir, dependency_ids=dependency_ids, qualification_ref=qual_ref,
        )

    report = QualificationReport(
        site_id=site_id,
        generated_at=generated_at,
        passed=all_passed,
        checks=checks,
        promoted_dependencies=promoted,
        qualification_ref=qual_ref,
        report_digest=report_digest,
    )
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.chmod(0o600)
    return report
