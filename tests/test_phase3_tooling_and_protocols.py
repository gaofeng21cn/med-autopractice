"""Phase 3 tests: site recorder, 5-point qualification harness, FHIR/REST/HL7 adapters, UI patterns."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from examples.sandbox_his.sandbox_engine import SyntheticHISSandbox  # noqa: E402
from med_autopractice.adapters.drivers.desktop_win import WindowsUIADriver  # noqa: E402
from med_autopractice.adapters.drivers.kvm_driver import KVMUIDriver  # noqa: E402
from med_autopractice.adapters.drivers.text_entry import PureHIDSegmentedStrategy  # noqa: E402
from med_autopractice.adapters.fhir import FHIRClinicalSystem  # noqa: E402
from med_autopractice.adapters.hl7v2 import HL7ObservationSource  # noqa: E402
from med_autopractice.adapters.middleware_rest import RestMiddlewareAdapter  # noqa: E402
from med_autopractice.domain.task import Proposal, SubjectRef, Task, TaskPolicy, TaskRequest  # noqa: E402
from med_autopractice.workflows.site_playbook import SitePlaybook  # noqa: E402
from med_autopractice.workflows.site_qualify import run_qualification_suite  # noqa: E402
from med_autopractice.workflows.site_recorder import PlaybookRecorder, build_kvm_anchor_step  # noqa: E402

SUBJECT = SubjectRef("demo-local", "synthetic-p01", "synthetic-e01")


def _prepare_observed_site(root: Path, *, long_text_qualified: bool = True) -> tuple[Path, dict]:
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True, mode=0o700)
    (site / "playbooks").mkdir(mode=0o700)

    deps = json.loads((ROOT / "contracts/site-dependencies.json").read_text(encoding="utf-8"))
    deps["profile_id"] = "demo-local"
    for item in deps["dependencies"]:
        item["state"] = "observed"
        item["evidence_ref"] = f"demo://observed/{item['id']}"
        item["qualification_ref"] = None
        if item["owner"] == "adapter":
            item["transport"] = "computer_use"
    (site / "dependencies.json").write_text(json.dumps(deps, ensure_ascii=False, indent=2), encoding="utf-8")

    collect_doc = {
        "schema_version": "map-site-playbook.v2",
        "playbook_id": "collect-labs",
        "site_id": "demo-local",
        "title": "采集实验室检验",
        "systems": ["inpatient_ehr"],
        "steps": [
            {"id": "dismiss", "intent": "关闭弹窗", "primitive": "handle_dialog",
             "dialog_rules": [{"trigger_text": "提示弹窗", "action": "dismiss", "target": "确定"}],
             "expect_text": "住院病历工作站", "effect": "read_only", "on_failure": "stop", "state": "verified"},
            {"id": "labs", "intent": "采集检验表格", "primitive": "scroll_collect",
             "selector": "table.labs", "extract_key": "results", "scope": "results",
             "max_pages": 5, "expect_text": "住院病历工作站", "effect": "read_only", "on_failure": "stop", "state": "verified"},
        ],
    }
    write_doc = {
        "schema_version": "map-site-playbook.v2",
        "playbook_id": "write-note",
        "site_id": "demo-local",
        "title": "写入病程草稿",
        "systems": ["inpatient_ehr"],
        "steps": [
            {"id": "enter", "intent": "输入病程正文", "primitive": "input_text",
             "selector": "#note-editor", "target": "病程编辑器", "region_hint": [40, 160, 540, 360],
             "text_param": "draft_text", "expect_text": "患者", "effect": "draft_write", "on_failure": "stop", "state": "verified"},
            {"id": "save", "intent": "点击保存草稿", "primitive": "click",
             "selector": "#save-draft", "target": "保存草稿", "expect_text": "已保存",
             "effect": "draft_write", "on_failure": "stop", "evidence": "screenshot_ref", "state": "verified"},
        ],
    }
    readback_doc = {
        "schema_version": "map-site-playbook.v2",
        "playbook_id": "readback-note",
        "site_id": "demo-local",
        "title": "回读病程正文",
        "systems": ["inpatient_ehr"],
        "steps": [
            {"id": "read", "intent": "读取编辑器", "primitive": "read_text",
             "selector": "#note-editor", "region_hint": [40, 160, 540, 360],
             "extract_key": "draft_text", "expect_text": "患者", "effect": "read_only", "on_failure": "stop",
             "evidence": "screenshot_ref", "state": "verified"},
        ],
    }
    books = {}
    for name, doc in (("collect.json", collect_doc), ("write.json", write_doc), ("readback.json", readback_doc)):
        path = site / "playbooks" / name
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        pb = SitePlaybook.load(path)
        books[pb.playbook_id] = pb
    return site, books


class Phase3ToolingAndProtocolsTests(unittest.TestCase):
    def test_playbook_recorder_strips_phi_and_builds_valid_v2_and_kvm_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            events = [
                {
                    "action": "click",
                    "intent": "选择患者: 张合成 (手机13800138000) 病程页签",
                    "target": "张合成-病程页签",
                    "selector": "#tab-progress",
                    "expect_text": "住院医生站",
                },
                {
                    "action": "input",
                    "intent": "输入病程正文 (身份证 110101199001011234)",
                    "selector": "#note-editor",
                    "text_value": "患者张合成咳嗽三天伴发热...",
                    "region_hint": [40, 160, 540, 360],
                },
                {
                    "action": "click",
                    "intent": "保存草稿",
                    "selector": "#save-btn",
                    "effect": "draft_write",
                    "evidence": "screenshot_ref",
                    "expect_text": "已保存",
                },
            ]
            events_file = tmp_dir / "raw_events.json"
            events_file.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
            out_file = tmp_dir / "sanitized_playbook.json"

            subprocess.run([
                sys.executable, str(ROOT / "scripts/site_recorder.py"),
                "--events", str(events_file), "--output", str(out_file),
                "--site-id", "demo-local", "--playbook-id", "recorded-note",
                "--title", "患者: 张合成 病程录入录制", "--redact", "张合成",
            ], capture_output=True, text=True, check=True)

            raw_text = out_file.read_text(encoding="utf-8")
            for leaked in ("张合成", "13800138000", "110101199001011234", "咳嗽三天"):
                self.assertNotIn(leaked, raw_text)
            pb = SitePlaybook.load(out_file)
            self.assertEqual(pb.steps[0].primitive, "verify_subject")
            self.assertTrue(all(s.state == "unverified" for s in pb.steps))
            self.assertEqual(pb.steps[2].text_param, "draft_text")

            kvm_step = build_kvm_anchor_step("kvm_save", "点击保存草稿", "保存草稿", region_hint=(100, 400, 350, 460))
            rec = PlaybookRecorder(site_id="demo-local", playbook_id="kvm-anchor", title="KVM")
            self.assertEqual(kvm_step["state"], "unverified")
            self.assertEqual(len(rec.build_playbook([events[0]]).steps), 2)

    def test_qualification_harness_passes_and_promotes_and_blocks_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. WindowsUIA 5-point qualification & promotion
            uia_site, books = _prepare_observed_site(Path(tmp) / "uia")
            sb_uia = SyntheticHISSandbox()
            uia_driver = WindowsUIADriver(backend=sb_uia, screenshot_dir=uia_site / "evidence")
            report = run_qualification_suite(
                uia_site,
                driver=uia_driver,
                subject=SUBJECT,
                collect_playbook=books["collect-labs"],
                write_playbook=books["write-note"],
                readback_playbook=books["readback-note"],
                interrupt_injector=lambda flag: setattr(sb_uia, "human_interrupted", flag),
                promote=True,
                dependency_ids=("save_inpatient_note_draft", "readback_inpatient_note"),
                report_path=uia_site / "qualification_report.json",
            )
            self.assertTrue(report.passed)
            self.assertEqual(len(report.checks), 5)
            self.assertTrue(all(c.passed for c in report.checks))
            self.assertEqual(set(report.promoted_dependencies), {"save_inpatient_note_draft", "readback_inpatient_note"})
            updated_deps = json.loads((uia_site / "dependencies.json").read_text(encoding="utf-8"))
            self.assertEqual(
                next(d for d in updated_deps["dependencies"] if d["id"] == "save_inpatient_note_draft")["state"],
                "qualified",
            )

            # 2. KVM Strategy B pure-HID typo auto-recovery passes 5-point suite
            kvm_site, kvm_books = _prepare_observed_site(Path(tmp) / "kvm")
            sb_kvm = SyntheticHISSandbox(inject_hid_typo_on_segment=4)
            kvm_driver = KVMUIDriver(
                sb_kvm,
                screenshot_dir=kvm_site / "evidence",
                expected_size=(1280, 720),
                long_text_qualified=True,
                text_strategy=PureHIDSegmentedStrategy(max_segment_chars=30, max_retries=2),
            )
            kvm_report = run_qualification_suite(
                kvm_site,
                driver=kvm_driver,
                subject=SUBJECT,
                collect_playbook=kvm_books["collect-labs"],
                write_playbook=kvm_books["write-note"],
                readback_playbook=kvm_books["readback-note"],
                interrupt_injector=lambda flag: setattr(sb_kvm, "human_interrupted", flag),
                promote=True,
                dependency_ids=("save_inpatient_note_draft", "readback_inpatient_note"),
            )
            self.assertTrue(kvm_report.passed)
            self.assertEqual(set(kvm_report.promoted_dependencies), {"save_inpatient_note_draft", "readback_inpatient_note"})

            # 3. Unqualified Strategy C fails suite and strictly refuses promotion
            fail_site, fail_books = _prepare_observed_site(Path(tmp) / "fail")
            sb_fail = SyntheticHISSandbox()
            fail_driver = KVMUIDriver(sb_fail, screenshot_dir=fail_site / "evidence", expected_size=(1280, 720), long_text_qualified=False)
            with self.assertRaises(ValueError):
                run_qualification_suite(
                    fail_site,
                    driver=fail_driver,
                    subject=SUBJECT,
                    collect_playbook=fail_books["collect-labs"],
                    write_playbook=fail_books["write-note"],
                    readback_playbook=fail_books["readback-note"],
                    interrupt_injector=lambda flag: setattr(sb_fail, "human_interrupted", flag),
                    promote=True,
                    dependency_ids=("save_inpatient_note_draft",),
                )
            still_observed = json.loads((fail_site / "dependencies.json").read_text(encoding="utf-8"))
            self.assertEqual(
                next(d for d in still_observed["dependencies"] if d["id"] == "save_inpatient_note_draft")["state"],
                "observed",
            )

    def test_full_fhir_and_rest_middleware_roundtrips_via_loopback_servers(self):
        stored_fhir: dict[str, dict] = {}
        stored_rest: dict[str, dict] = {}

        class MockProtocolHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _send(self, code: int, obj: dict):
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)
                if path == "/fhir/Encounter/synthetic-e01":
                    self._send(200, {
                        "resourceType": "Encounter", "id": "synthetic-e01", "status": "in-progress",
                        "subject": {"reference": "Patient/synthetic-p01"}, "meta": {"versionId": "1"},
                    })
                elif path == "/fhir/Observation":
                    self._send(200, {"resourceType": "Bundle", "entry": [{"resource": {
                        "resourceType": "Observation", "id": "obs-1", "status": "final",
                        "subject": {"reference": "Patient/synthetic-p01"},
                        "encounter": {"reference": "Encounter/synthetic-e01"},
                        "code": {"text": "白细胞"}, "valueQuantity": {"value": 10.8, "unit": "10^9/L"},
                        "meta": {"versionId": "3"},
                    }}]})
                elif path == "/fhir/MedicationRequest":
                    if "identifier" in qs:
                        token = qs["identifier"][0].split("|", 1)[-1]
                        hits = [{"resource": r} for r in stored_fhir.values()
                                if r.get("resourceType") == "MedicationRequest"
                                and any(i.get("value") == token for i in r.get("identifier", []))]
                        self._send(200, {"resourceType": "Bundle", "entry": hits})
                    else:
                        self._send(200, {"resourceType": "Bundle", "entry": [{"resource": {
                            "resourceType": "MedicationRequest", "id": "ord-1", "status": "active",
                            "subject": {"reference": "Patient/synthetic-p01"},
                            "medicationCodeableConcept": {"text": "DEMO-MED", "coding": [{"code": "DEMO-MED"}]},
                            "meta": {"versionId": "1"},
                        }}]})
                elif path == "/fhir/DocumentReference":
                    if "identifier" in qs:
                        token = qs["identifier"][0].split("|", 1)[-1]
                        hits = [{"resource": r} for r in stored_fhir.values()
                                if r.get("resourceType") == "DocumentReference"
                                and any(i.get("value") == token for i in r.get("identifier", []))]
                        self._send(200, {"resourceType": "Bundle", "entry": hits})
                    else:
                        self._send(200, {"resourceType": "Bundle", "entry": []})
                elif path.startswith("/fhir/DocumentReference/") or path.startswith("/fhir/MedicationRequest/"):
                    rid = path.rsplit("/", 1)[-1]
                    if rid in stored_fhir:
                        self._send(200, stored_fhir[rid])
                    else:
                        self._send(404, {"error": "not found"})
                elif path == "/mw/api/context/synthetic-p01/synthetic-e01":
                    self._send(200, {
                        "patient_id": "synthetic-p01", "encounter_id": "synthetic-e01", "revision": "mw-rev-1",
                        "facts": [{"fact_id": "mw-lab-1", "value": "CRP 18.2 mg/L", "details": {"section": "观察"}}],
                    })
                elif path == "/mw/api/submissions/by-ref":
                    ref = (qs.get("object_ref") or [""])[0]
                    if ref in stored_rest:
                        self._send(200, stored_rest[ref])
                    else:
                        self._send(404, {"error": "missing"})
                elif path.startswith("/mw/api/submissions/by-key/"):
                    key = path.rsplit("/", 1)[-1]
                    hit = next((v for v in stored_rest.values() if v.get("idempotency_key") == key), None)
                    self._send(200 if hit else 404, hit or {"error": "missing"})
                else:
                    self._send(404, {"error": f"unknown path {path}"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path in {"/fhir/DocumentReference", "/fhir/MedicationRequest"}:
                    rid = f"res-{len(stored_fhir) + 1}"
                    saved = {
                        **body, "id": rid,
                        "meta": {"versionId": "1", "lastUpdated": "2026-01-01T10:00:00Z"},
                    }
                    stored_fhir[rid] = saved
                    self._send(201, saved)
                elif self.path == "/mw/api/submissions":
                    ref = f"mw://doc/{len(stored_rest) + 1}"
                    rec = {
                        **body, "object_ref": ref, "state": "saved_draft",
                        "version_ref": f"{ref}/v1", "observed_at": "2026-01-01T10:00:00Z",
                    }
                    stored_rest[ref] = rec
                    self._send(201, rec)
                else:
                    self._send(404, {"error": "unknown post"})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockProtocolHandler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            fhir = FHIRClinicalSystem(f"http://127.0.0.1:{port}/fhir", "demo-local", access_token=lambda: "demo-tok")
            snap = fhir.read_context(SUBJECT, "write_inpatient_notes")
            self.assertEqual(len(snap.facts), 2)
            req_note = TaskRequest("r-fhir-1", "write_inpatient_notes", "写病程", SUBJECT, "demo://grant")
            prop = Proposal(SUBJECT, "write_inpatient_notes", {"type": "note_draft", "sections": [{"section": "观察", "entries": [{"text": "今日平稳"}]}]}, ("Observation/obs-1",), "demo://gen")
            receipt = fhir.apply(prop, "idem-fhir-1", None)
            task_obj = Task("t-fhir-1", req_note, "execution_uncertain", snapshot=snap, proposal=prop, idempotency_key="idem-fhir-1", receipt=receipt)
            rb = fhir.read_back(receipt, task_obj)
            self.assertEqual(rb.proposal_digest, prop.digest)
            self.assertEqual(rb.state, "saved_draft")
            self.assertTrue(rb.resource_version_ref and "/_history/" in rb.resource_version_ref)
            found = fhir.find_submission(task_obj)
            self.assertIsNotNone(found)
            self.assertEqual(found.object_ref, receipt.object_ref)

            rest = RestMiddlewareAdapter(f"http://127.0.0.1:{port}/mw", "demo-local", {})
            mw_snap = rest.read_context(SUBJECT, "write_inpatient_notes")
            self.assertEqual(mw_snap.facts[0].fact_id, "mw-lab-1")
            mw_receipt = rest.apply(prop, "idem-mw-1", None)
            mw_task = Task("t-mw-1", req_note, "execution_uncertain", snapshot=mw_snap, proposal=prop, idempotency_key="idem-mw-1", receipt=mw_receipt)
            mw_rb = rest.read_back(mw_receipt, mw_task)
            self.assertEqual(mw_rb.proposal_digest, prop.digest)
            self.assertEqual(mw_rb.state, "saved_draft")
            self.assertEqual(rest.find_submission(mw_task).object_ref, mw_receipt.object_ref)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_hl7v2_parser_and_four_ui_pattern_templates(self):
        hl7 = (
            "MSH|^~\\&|LIS|HOSP|MAP|HOSP|20260101120000||ORU^R01|MSG001|P|2.5\r"
            "PID|1||synthetic-p01^^^HOSP||SYNTHETIC^TEST||19800101|M\r"
            "PV1|1|I|WARD^101^1||||||||||||||||synthetic-e01\r"
            "OBR|1|ORD1||LAB|||20260101115500\r"
            "OBX|1|NM|WBC^白细胞计数||11.8|10^9/L|3.5-9.5|H|||F|||20260101115800\r"
            "OBX|2|NM|PCT^降钙素原||0.42|ng/mL|<0.05|H|||P\r"
            "OBX|3|NM|ERR^作废项||99.9|U/L|||||D\r"
        )
        src = HL7ObservationSource("demo-local")
        facts = src.extract_facts(SUBJECT, hl7)
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0].status, "observed")
        self.assertEqual(facts[1].status, "uncertain")
        with self.assertRaises(ValueError):
            src.extract_facts(SubjectRef("demo-local", "other-pat", "synthetic-e01"), hl7)

        pattern_files = sorted((ROOT / "examples/patterns").glob("*_playbook.json"))
        self.assertEqual(len(pattern_files), 4)
        for pfile in pattern_files:
            pb = SitePlaybook.load(pfile)
            self.assertGreaterEqual(len(pb.steps), 4)
            self.assertTrue(all(s.state == "unverified" for s in pb.steps))
