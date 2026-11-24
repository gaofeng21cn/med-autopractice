"""Phase 1 tests: declarative Playbook v2 runner, parallel Windows UIA/Browser CDP and IP KVM pure-HID drivers."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.sandbox_his.sandbox_engine import SyntheticHISSandbox
from med_autopractice.adapters.declarative_ui import DeclarativeHospitalUI
from med_autopractice.adapters.drivers.browser_cdp import BrowserCDPDriver
from med_autopractice.adapters.drivers.desktop_win import WindowsUIADriver
from med_autopractice.adapters.drivers.kvm_driver import (
    GLKVMNativeTransport, KVMUIDriver, PiKVMHttpTransport, VNCHidTransport,
)
from med_autopractice.adapters.drivers.ocr import CallableOCREngine, OCRWord
from med_autopractice.adapters.drivers.text_entry import (
    ChineseEntryUnqualifiedError, ClipboardRelayStrategy, DraftOnlyFallbackStrategy,
    PureHIDSegmentedStrategy,
)
from med_autopractice.adapters.playbook_runner import HumanInterruptError, PlaybookRunner, ScreenObservation
from med_autopractice.adapters.ui import UIClinicalSystem, UIProfile, UIStateError
from med_autopractice.deployment import build_site_runtime
from med_autopractice.domain.task import ClinicianApproval, Proposal, SubjectRef, Task, TaskPolicy, TaskRequest
from med_autopractice.workflows.site_playbook import SitePlaybook
from med_autopractice.workflows.site_sync import inspect_and_sync_site_pack

SUBJECT = SubjectRef("demo-local", "synthetic-p01", "synthetic-e01")

CHINESE_NOTE_500 = (
    "患者系中年男性，因反复咳嗽、咳痰伴发热三天入院。今日查房神清，精神尚可，体温三十七度四，"
    "双肺呼吸音粗，右下肺可及少许湿性啰音，心率齐，未闻及明显杂音。结合昨日检验结果示白细胞升高，"
    "超敏C反应蛋白升高，降钙素原轻度异常，提示细菌性下呼吸道感染可能大。护理记录提示夜间睡眠欠佳，"
    "咳嗽咳痰略多，无明显胸痛及呼吸困难。今日拟继续当前抗感染及祛痰对症治疗方案，加强翻身拍背及排痰护理，"
    "嘱患者多饮温水，注意监测体温变化及血氧饱和度。已向患者及家属交代病情变化及下一步诊疗计划，"
    "复查血常规与炎症指标后再评估是否调整抗菌药物方案，目前病情总体趋于平稳，继续住院观察治疗。"
    "补充查房记录：患者饮食尚可，大小便正常，双下肢无水肿，神经系统查体未见明显定位体征。"
)


def _create_declarative_site(root: Path, *, long_text_qualified: bool = True) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    site = root / "site"
    site.mkdir(mode=0o700)
    (site / "playbooks").mkdir(mode=0o700)

    deps = json.loads((ROOT / "contracts/site-dependencies.json").read_text(encoding="utf-8"))
    deps["profile_id"] = "demo-local"
    for item in deps["dependencies"]:
        item["state"] = "qualified"
        item["evidence_ref"] = f"demo://ev/{item['id']}"
        item["qualification_ref"] = f"demo://qual/{item['id']}"
        if item["owner"] == "adapter":
            item["transport"] = "computer_use"
    (site / "dependencies.json").write_text(json.dumps(deps, ensure_ascii=False, indent=2), encoding="utf-8")

    survey = {
        "schema_version": "map-site-survey.v1",
        "profile_id": "demo-local",
        "systems": [{"id": "inpatient_ehr", "role": "inpatient_his_emr"}],
        "operations": [
            {
                "dependency_id": "bind_inpatient",
                "system_id": "inpatient_ehr",
                "semantic_action": "bind_patient_and_encounter",
                "mode": "read",
                "state": "qualified",
                "evidence_refs": ["demo://survey/bind"],
                "qualification_ref": "demo://qual/bind",
            }
        ],
    }
    (site / "site-survey.json").write_text(json.dumps(survey, ensure_ascii=False, indent=2), encoding="utf-8")

    collect_book = {
        "schema_version": "map-site-playbook.v2",
        "playbook_id": "collect-note-context",
        "site_id": "demo-local",
        "title": "采集住院病程所需多页事实",
        "systems": ["inpatient_ehr"],
        "evidence_dir_ref": "demo://evidence",
        "steps": [
            {
                "id": "dismiss_notice",
                "intent": "关闭初始提示弹窗",
                "primitive": "handle_dialog",
                "dialog_rules": [{"trigger_text": "提示弹窗", "action": "dismiss", "target": "确定"}],
                "expect_text": "住院病历工作站",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "verify_identity",
                "intent": "核对患者与本次住院号",
                "primitive": "verify_subject",
                "expect_text": "住院病历工作站",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "open_labs_tab",
                "intent": "切换到检验结果页签并回到首页",
                "primitive": "click",
                "selector": "#tab-labs",
                "target": "检验结果",
                "expect_text": "住院病历工作站",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "collect_labs",
                "intent": "翻页采集多页检验结果",
                "primitive": "scroll_collect",
                "selector": "table.labs",
                "extract_key": "results",
                "scope": "results",
                "max_pages": 5,
                "expect_text": "住院病历工作站",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
        ],
    }
    write_book = {
        "schema_version": "map-site-playbook.v2",
        "playbook_id": "write-note-draft",
        "site_id": "demo-local",
        "title": "写入中文住院病程草稿",
        "systems": ["inpatient_ehr"],
        "steps": [
            {
                "id": "verify_before_write",
                "intent": "写前再次核对患者身份",
                "primitive": "verify_subject",
                "expect_text": "住院病历工作站",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "enter_chinese_note",
                "intent": "录入中文病程正文",
                "primitive": "input_text",
                "selector": "#note-editor",
                "target": "病程编辑器",
                "region_hint": [40, 160, 540, 360],
                "text_param": "draft_text",
                "expect_text": "患者",
                "effect": "draft_write",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "click_save",
                "intent": "点击保存草稿",
                "primitive": "click",
                "selector": "#save-draft",
                "target": "保存草稿",
                "expect_text": "已保存",
                "effect": "draft_write",
                "on_failure": "stop",
                "evidence": "screenshot_ref",
                "state": "verified",
            },
            {
                "id": "read_saved_ref",
                "intent": "读取系统生成的草稿对象引用",
                "primitive": "read_text",
                "selector": "#object-ref",
                "region_hint": [315, 448, 560, 480],
                "extract_key": "object_ref",
                "expect_text": "demo://",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "read_saved_state",
                "intent": "读取草稿保存状态",
                "primitive": "read_text",
                "selector": "#object-state",
                "region_hint": [315, 415, 410, 448],
                "extract_key": "state",
                "expect_text": "已保存",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
        ],
    }
    readback_book = {
        "schema_version": "map-site-playbook.v2",
        "playbook_id": "readback-note-draft",
        "site_id": "demo-local",
        "title": "回读已保存的住院病程正文",
        "systems": ["inpatient_ehr"],
        "steps": [
            {
                "id": "verify_before_readback",
                "intent": "回读前核对患者身份",
                "primitive": "verify_subject",
                "expect_text": "住院病历工作站",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "read_object_ref",
                "intent": "回读对象唯一引用",
                "primitive": "read_text",
                "selector": "#object-ref",
                "region_hint": [315, 448, 560, 480],
                "extract_key": "object_ref",
                "expect_text": "demo://",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "read_object_state",
                "intent": "回读对象状态",
                "primitive": "read_text",
                "selector": "#object-state",
                "region_hint": [315, 415, 410, 448],
                "extract_key": "state",
                "expect_text": "已保存",
                "effect": "read_only",
                "on_failure": "stop",
                "state": "verified",
            },
            {
                "id": "read_editor",
                "intent": "回读病程编辑器全文",
                "primitive": "read_text",
                "selector": "#note-editor",
                "region_hint": [40, 160, 540, 360],
                "extract_key": "draft_text",
                "expect_text": "患者",
                "effect": "read_only",
                "on_failure": "stop",
                "evidence": "screenshot_ref",
                "state": "verified",
            },
        ],
    }
    for name, body in (
        ("collect.json", collect_book),
        ("write.json", write_book),
        ("readback.json", readback_book),
    ):
        (site / "playbooks" / name).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    deployment = {
        "schema_version": "map-local-deployment.v1",
        "site_id": "demo-local",
        "authority_ref": "demo://grant",
        "authorized_clinicians": ["demo-clinician"],
        "driver": {"kind": "windows_uia"},
        "ui_profile": {
            "product": "synthetic-his",
            "version": "2.0",
            "role": "clinician",
            "display_size": [1280, 720],
            "qualification_ref": "demo://qual/ui",
            "write_capabilities": ["write_inpatient_notes"],
            "long_text_qualified": long_text_qualified,
        },
        "playbooks": {
            "write_inpatient_notes": {
                "collect": ["collect-note-context"],
                "write": "write-note-draft",
                "readback": "readback-note-draft",
                "default_section": "观察",
                "extra_complete_scopes": ["patient_context", "current_orders", "nursing"],
                "state_map": {"已保存": "saved_draft", "已录入": "entered"},
            }
        },
        "capabilities": {"write_inpatient_notes": "saved_draft"},
        "rules": {
            "write_inpatient_notes": {"approved_ref": "demo://rules/note/1", "sections": ["观察"]}
        },
    }
    (site / "deployment.json").write_text(json.dumps(deployment, ensure_ascii=False, indent=2), encoding="utf-8")
    return site


class DeclarativeSiteAndDriversTests(unittest.TestCase):
    def test_site_sync_validates_and_syncs_observed_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = _create_declarative_site(Path(tmp))
            deps_path = site / "dependencies.json"
            deps = json.loads(deps_path.read_text(encoding="utf-8"))
            next(d for d in deps["dependencies"] if d["id"] == "bind_inpatient").update(
                state="unmapped", evidence_ref=None, qualification_ref=None,
            )
            deps_path.write_text(json.dumps(deps, ensure_ascii=False, indent=2), encoding="utf-8")

            report = inspect_and_sync_site_pack(site, repo_root=ROOT, sync_observed=True)
            self.assertTrue(report.valid)
            self.assertIn("bind_inpatient", report.synced_dependencies)
            updated = json.loads(deps_path.read_text(encoding="utf-8"))
            synced = next(d for d in updated["dependencies"] if d["id"] == "bind_inpatient")
            self.assertEqual(synced["state"], "observed")
            self.assertIsNone(synced["qualification_ref"])

            cli = subprocess.run(
                [sys.executable, str(ROOT / "scripts/site_sync.py"), "--site", str(site)],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(cli.stdout)["playbooks_count"], 3)

    def test_windows_uia_declarative_runtime_without_provider_py(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = _create_declarative_site(Path(tmp))
            sandbox = SyntheticHISSandbox()
            driver = WindowsUIADriver(backend=sandbox, screenshot_dir=site / "evidence")
            runtime = build_site_runtime(site, driver=driver)

            req = TaskRequest(
                "req-uia-500", "write_inpatient_notes", "写今日病程",
                SUBJECT, "demo://grant", inputs={"sections": {"观察": CHINESE_NOTE_500}},
            )
            started = runtime.start(req)
            self.assertEqual(started["status"], "needs_pre_submit_review")
            self.assertEqual(len(started["snapshot"]["facts"]), 3)
            self.assertFalse(sandbox.dialog_open)

            approval = ClinicianApproval(
                started["task_id"], "demo-clinician",
                started["proposal"]["digest"], started["snapshot"]["revision"],
            )
            submitted = runtime.submit(started["task_id"], approval)
            self.assertEqual(submitted["status"], "needs_clinician_acceptance")
            self.assertTrue(submitted["execution"]["readback"]["screenshot_ref"])
            self.assertEqual(sandbox.clipboard_value, "医生原有剪贴板内容")

            accepted = runtime.review(
                started["task_id"], "demo-clinician",
                accepted=True, digest=started["proposal"]["digest"],
            )
            self.assertEqual(accepted["status"], "accepted")

            # Clinician mouse/keyboard preemption halts immediately.
            sandbox.human_interrupted = True
            with self.assertRaises(HumanInterruptError):
                driver.observe()

    def test_browser_cdp_driver_collects_fills_and_captures_xhr(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = _create_declarative_site(Path(tmp))
            sandbox = SyntheticHISSandbox()
            driver = BrowserCDPDriver(backend=sandbox, screenshot_dir=site / "evidence")
            books = {b.playbook_id: b for b in SitePlaybook.load_directory(site / "playbooks")}
            profile = UIProfile("demo-local", "web-his", "1.0", "clinician", (1280, 720), "demo://q", frozenset({"write_inpatient_notes"}), True)
            ui = DeclarativeHospitalUI(profile, driver, books, {
                "write_inpatient_notes": {
                    "collect": ["collect-note-context"],
                    "write": "write-note-draft",
                    "readback": "readback-note-draft",
                    "extra_complete_scopes": ["patient_context", "current_orders", "nursing"],
                }
            })
            system = UIClinicalSystem(ui)
            snap = system.read_context(SUBJECT, "write_inpatient_notes")
            self.assertEqual(len(snap.facts), 3)
            self.assertTrue(driver.captured_xhr())
            driver.input_text(CHINESE_NOTE_500, selector="#note-editor", subject=SUBJECT)
            self.assertEqual(sandbox.editor_text, CHINESE_NOTE_500)

    def test_kvm_pure_hid_segmented_chinese_typo_recovery_and_strategies(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = _create_declarative_site(Path(tmp))
            # Inject an IME typo on HID segment 3; PureHIDSegmentedStrategy must auto-backspace & correct.
            sandbox = SyntheticHISSandbox(inject_hid_typo_on_segment=3)
            driver = KVMUIDriver(
                sandbox,
                screenshot_dir=site / "evidence",
                expected_size=(1280, 720),
                long_text_qualified=True,
                text_strategy=PureHIDSegmentedStrategy(max_segment_chars=30, max_retries=2, allow_unicode_hid=True),
            )
            runtime = build_site_runtime(site, driver=driver)
            req = TaskRequest(
                "req-kvm-500", "write_inpatient_notes", "KVM纯HID写病程",
                SUBJECT, "demo://grant", inputs={"sections": {"观察": CHINESE_NOTE_500}},
            )
            started = runtime.start(req)
            self.assertEqual(started["status"], "needs_pre_submit_review")
            approval = ClinicianApproval(
                started["task_id"], "demo-clinician",
                started["proposal"]["digest"], started["snapshot"]["revision"],
            )
            submitted = runtime.submit(started["task_id"], approval)
            self.assertEqual(submitted["status"], "needs_clinician_acceptance")
            self.assertIn(CHINESE_NOTE_500, sandbox.editor_text)

            # Strategy C: unqualified KVM refuses Chinese entry & runtime goes draft_only.
            site_unqual = _create_declarative_site(Path(tmp) / "unqual", long_text_qualified=False)
            unqual_driver = KVMUIDriver(SyntheticHISSandbox(), long_text_qualified=False)
            with self.assertRaises(ChineseEntryUnqualifiedError):
                unqual_driver.input_text("中文测试正文", region_hint=(40, 160, 540, 360), subject=SUBJECT)
            rt_unqual = build_site_runtime(site_unqual, driver=unqual_driver)
            draft_only = rt_unqual.start(TaskRequest(
                "req-kvm-draft", "write_inpatient_notes", "未通过中文资格",
                SUBJECT, "demo://grant", inputs={"sections": {"观察": "测试内容"}},
            ))
            self.assertEqual(draft_only["status"], "draft_only")

            # Strategy A: disabled unless explicitly permitted by hospital policy.
            with self.assertRaises(Exception):
                ClipboardRelayStrategy(allow_clipboard_relay=False).enter_text(
                    "测试", hid_sender=lambda _: None, backspace_sender=lambda _: None, ocr_reader=lambda _: "",
                )
            sandbox_clip = SyntheticHISSandbox(allow_kvm_clipboard=True)
            clip_driver = KVMUIDriver(
                sandbox_clip, long_text_qualified=True, allow_clipboard_relay=True,
            )
            clip_driver.input_text("剪贴板策略测试文本", region_hint=(40, 160, 540, 360), subject=SUBJECT)
            self.assertEqual(sandbox_clip.editor_text, "剪贴板策略测试文本")

    def test_kvm_transports_pikvm_vnc_and_glkvm_discovery(self):
        # 1. PiKVMHttpTransport against loopback HTTP server
        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                if self.path == "/api/info":
                    body = json.dumps({"ok": True, "result": {"hw": "pikvm"}}).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/streamer/snapshot":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"\x89PNG\r\n\x1a\npikvm")
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length) if length else b""
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        pikvm = PiKVMHttpTransport(f"http://127.0.0.1:{server.server_address[1]}")
        self.assertTrue(pikvm.status()["connected"])
        ocr = CallableOCREngine(lambda img, reg: (OCRWord("住院号", 99.0, (10, 10, 60, 20)),))
        kvm_driver = KVMUIDriver(pikvm, ocr_engine=ocr)
        obs = kvm_driver.observe()
        self.assertIn("住院号", obs.full_text())

        # 2. VNCHidTransport
        class _FakeRFB:
            def is_connected(self): return True
            def capture_screen(self, region=None): return {"words": [{"text": "VNC窗口", "confidence": 99, "box": [0, 0, 50, 20]}]}
            def mouse_click(self, x, y, button=1): pass
            def key_press(self, keys): pass
            def type_text(self, text): pass
            def mouse_wheel(self, delta, x=None, y=None): pass

        vnc = VNCHidTransport(_FakeRFB())
        self.assertTrue(vnc.status()["connected"])

        # 3. GLKVMNativeTransport discovery via GLKVM_INSTALLATION
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "installation.json"
            receipt.write_text('{"runtime": ".", "profile": "demo"}', encoding="utf-8")
            with patch.dict(os.environ, {"GLKVM_INSTALLATION": str(receipt)}):
                self.assertEqual(GLKVMNativeTransport.discover_receipt(), receipt)

    def test_declarative_fail_closed_subject_ref_state_and_order_table_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            site = _create_declarative_site(tmp_dir / "failclosed")
            books = {b.playbook_id: b for b in SitePlaybook.load_directory(site / "playbooks")}

            # 1. verify_subject rejects ambiguous patient tokens and substring collisions
            sandbox = SyntheticHISSandbox()
            driver = WindowsUIADriver(backend=sandbox, screenshot_dir=site / "evidence")
            ambiguous_book = SitePlaybook({
                "schema_version": "map-site-playbook.v2",
                "playbook_id": "verify-only",
                "site_id": "demo-local",
                "title": "核验身份",
                "systems": ["inpatient_ehr"],
                "steps": [{
                    "id": "v", "intent": "verify", "primitive": "verify_subject",
                    "expect_text": "*", "effect": "read_only", "on_failure": "stop", "state": "verified",
                }],
            }).playbook
            with patch.object(
                driver, "observe",
                return_value=ScreenObservation("f1", "患者:synthetic-p01 患者:synthetic-p02 就诊:synthetic-e01", "2026-01-01T00:00:00Z"),
            ):
                with self.assertRaises(UIStateError):
                    PlaybookRunner(driver).run(ambiguous_book, SUBJECT)
            with patch.object(
                driver, "observe",
                return_value=ScreenObservation("f2", "synthetic-p01-WRONG synthetic-e01", "2026-01-01T00:00:00Z"),
            ):
                with self.assertRaises(UIStateError):
                    PlaybookRunner(driver).run(ambiguous_book, SUBJECT)

            # 2. Missing object_ref or state in write/readback playbooks fails closed (no fake fallback)
            write_no_ref = SitePlaybook({
                "schema_version": "map-site-playbook.v2",
                "playbook_id": "write-no-ref",
                "site_id": "demo-local",
                "title": "无对象引用写入",
                "systems": ["inpatient_ehr"],
                "steps": [
                    {"id": "v", "intent": "verify", "primitive": "verify_subject",
                     "expect_text": "*", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                    {"id": "s", "intent": "save", "primitive": "click", "selector": "#save-draft",
                     "expect_text": "已保存", "effect": "draft_write", "on_failure": "stop", "state": "verified"},
                ],
            }).playbook
            readback_no_state = SitePlaybook({
                "schema_version": "map-site-playbook.v2",
                "playbook_id": "readback-no-state",
                "site_id": "demo-local",
                "title": "缺状态回读",
                "systems": ["inpatient_ehr"],
                "steps": [
                    {"id": "ref", "intent": "read ref", "primitive": "read_text", "selector": "#object-ref",
                     "extract_key": "object_ref", "expect_text": "demo://", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                    {"id": "txt", "intent": "read txt", "primitive": "read_text", "selector": "#note-editor",
                     "extract_key": "draft_text", "expect_text": "*", "effect": "read_only", "on_failure": "stop",
                     "evidence": "screenshot_ref", "state": "verified"},
                ],
            }).playbook
            books_fc = {**books, "write-no-ref": write_no_ref, "readback-no-state": readback_no_state}
            profile = UIProfile("demo-local", "synthetic-his", "2.0", "clinician", (1280, 720), "demo://q", frozenset({"write_inpatient_notes", "revise_orders"}), True)
            ui_fc = DeclarativeHospitalUI(profile, driver, books_fc, {
                "write_inpatient_notes": {
                    "write": "write-no-ref",
                    "readback": "readback-no-state",
                }
            })
            prop_note = Proposal(SUBJECT, "write_inpatient_notes", {"draft": "患者平稳"}, ("lab-1",), "demo://gen")
            with self.assertRaises(UIStateError):
                ui_fc.write(prop_note, None)

            # Even open_object never synthesizes missing subject/state
            sandbox.last_object_ref = "demo://his/note/99"
            sandbox.editor_text = "患者平稳"
            vis = ui_fc.open_object(SUBJECT, "write_inpatient_notes", "demo://his/note/99")
            self.assertEqual(vis.state, "")
            self.assertEqual(vis.subject.patient_id, "__unverified__")
            rb_fc = UIClinicalSystem(ui_fc).read_back(
                UIClinicalSystem(DeclarativeHospitalUI(profile, driver, books, {
                    "write_inpatient_notes": {
                        "write": "write-note-draft", "readback": "readback-note-draft",
                        "state_map": {"已保存": "saved_draft"},
                    }
                })).apply(prop_note, "idem-fc", None),
                Task("t-fc", TaskRequest("r-fc", "write_inpatient_notes", "note", SUBJECT, "demo://grant"), "execution_uncertain", proposal=prop_note),
            )
            self.assertEqual(rb_fc.proposal_digest, "")

            # 3. Declarative order changes table reconstruction: full match vs partial failure
            order_write_book = SitePlaybook({
                "schema_version": "map-site-playbook.v2",
                "playbook_id": "write-orders",
                "site_id": "demo-local",
                "title": "录入医嘱变更",
                "systems": ["inpatient_ehr"],
                "steps": [
                    {"id": "v", "intent": "verify", "primitive": "verify_subject", "selector": "#patient-header",
                     "expect_text": "患者", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                    {"id": "in", "intent": "input orders", "primitive": "input_text", "selector": "#orders-editor",
                     "target": "医嘱编辑器", "text_param": "changes_json", "expect_text": "*",
                     "effect": "reviewed_write", "on_failure": "stop", "state": "verified"},
                    {"id": "sub", "intent": "submit orders", "primitive": "click", "selector": "#submit-orders",
                     "target": "提交医嘱", "expect_text": "已录入", "effect": "reviewed_write",
                     "on_failure": "stop", "evidence": "screenshot_ref", "state": "verified"},
                    {"id": "ref", "intent": "read ref", "primitive": "read_text", "selector": "#object-ref",
                     "extract_key": "object_ref", "expect_text": "demo://", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                    {"id": "st", "intent": "read state", "primitive": "read_text", "selector": "#object-state",
                     "extract_key": "state", "expect_text": "已录入", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                ],
            }).playbook
            order_readback_book = SitePlaybook({
                "schema_version": "map-site-playbook.v2",
                "playbook_id": "readback-orders",
                "site_id": "demo-local",
                "title": "回读医嘱表格",
                "systems": ["inpatient_ehr"],
                "steps": [
                    {"id": "v", "intent": "verify", "primitive": "verify_subject", "selector": "#patient-header",
                     "expect_text": "患者", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                    {"id": "ref", "intent": "read ref", "primitive": "read_text", "selector": "#object-ref",
                     "extract_key": "object_ref", "expect_text": "demo://", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                    {"id": "st", "intent": "read state", "primitive": "read_text", "selector": "#object-state",
                     "extract_key": "state", "expect_text": "已录入", "effect": "read_only", "on_failure": "stop", "state": "verified"},
                    {"id": "tbl", "intent": "read orders table", "primitive": "read_table", "selector": "#orders-table",
                     "extract_key": "orders_table", "expect_text": "*", "effect": "read_only", "on_failure": "stop",
                     "evidence": "screenshot_ref", "state": "verified"},
                ],
            }).playbook
            order_payload = {
                "type": "order_changes",
                "changes": [
                    {"action": "add", "code": "NEW-MED", "dose": "250", "unit": "mg", "route": "iv", "frequency": "q12h",
                     "reason": "CRP升高", "fact_ids": ["lab-2"], "catalog_ref": "demo://catalog/NEW-MED"},
                    {"action": "change", "code": "DEMO-MED", "target_ref": "demo://his/order/ord-1",
                     "dose": "750", "unit": "mg", "route": "po", "frequency": "bid",
                     "reason": "加量", "fact_ids": ["lab-1"], "catalog_ref": "demo://catalog/DEMO-MED"},
                    {"action": "stop", "code": "STOP-MED", "target_ref": "demo://his/order/ord-2",
                     "reason": "停用", "fact_ids": ["lab-1"], "catalog_ref": "demo://catalog/STOP-MED"},
                ],
            }
            prop_ord = Proposal(SUBJECT, "revise_orders", order_payload, ("lab-1", "lab-2"), "demo://gen")
            order_bindings = {
                "revise_orders": {
                    "write": "write-orders",
                    "readback": "readback-orders",
                    "state_map": {"已录入": "entered"},
                    "order_table_key": "orders_table",
                }
            }
            order_books = {"write-orders": order_write_book, "readback-orders": order_readback_book}

            # Full success -> row-by-row reconstruction matches proposal digest
            sb_ok = SyntheticHISSandbox()
            sys_ok = UIClinicalSystem(DeclarativeHospitalUI(
                profile, WindowsUIADriver(backend=sb_ok, screenshot_dir=site / "evidence"), order_books, order_bindings,
            ))
            receipt_ok = sys_ok.apply(prop_ord, "idem-ord-ok", None)
            rb_ok = sys_ok.read_back(
                receipt_ok,
                Task("t-ord-ok", TaskRequest("r-ord-ok", "revise_orders", "调医嘱", SUBJECT, "demo://grant"), "execution_uncertain", proposal=prop_ord),
            )
            self.assertEqual(rb_ok.proposal_digest, prop_ord.digest)
            self.assertEqual(rb_ok.state, "entered")
            self.assertEqual(rb_ok.visible_payload, order_payload)

            # Partial failure (index 1 skipped on target UI) -> readback refuses match!
            sb_partial = SyntheticHISSandbox(simulate_partial_order_failure_index=1)
            sys_partial = UIClinicalSystem(DeclarativeHospitalUI(
                profile, WindowsUIADriver(backend=sb_partial, screenshot_dir=site / "evidence"), order_books, order_bindings,
            ))
            receipt_partial = sys_partial.apply(prop_ord, "idem-ord-part", None)
            rb_partial = sys_partial.read_back(
                receipt_partial,
                Task("t-ord-part", TaskRequest("r-ord-part", "revise_orders", "调医嘱", SUBJECT, "demo://grant"), "execution_uncertain", proposal=prop_ord),
            )
            self.assertEqual(rb_partial.proposal_digest, "")
            self.assertEqual(rb_partial.visible_payload["type"], "observed_order_table")

    def test_glkvm_real_contract_honest_chinese_fallback_and_pikvm_ocr_wiring(self):
        # 1. PureHIDSegmentedStrategy refuses raw non-ASCII by default; ime_composer sends ASCII only
        with self.assertRaises(ChineseEntryUnqualifiedError):
            PureHIDSegmentedStrategy().enter_text(
                "中文病程", hid_sender=lambda _: None, backspace_sender=lambda _: None, ocr_reader=lambda _: "",
            )
        sent_hid: list[str] = []
        res = PureHIDSegmentedStrategy(ime_composer=lambda s: "zhongwenbingcheng ").enter_text(
            "中文病程",
            hid_sender=lambda chunk: sent_hid.append(chunk),
            backspace_sender=lambda _: None,
            ocr_reader=lambda _: "中文病程",
        )
        self.assertEqual(sent_hid, ["zhongwenbingcheng "])
        self.assertTrue(all(x.isascii() for x in sent_hid))
        self.assertEqual(res.text_readback, "中文病程")

        # 2. GLKVMNativeTransport aligns with gateway.RemoteClient contract and refuses non-ASCII HID
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            shot_file = tmp_dir / "gw-shot.jpg"
            shot_file.write_bytes(b"jpg-bytes")
            calls: list[tuple[str, tuple, dict]] = []
            seq = {"n": 0, "control_mode": "agent"}

            class FakeRemoteClient:
                async def status(self):
                    return {"connected": True, "control_mode": seq["control_mode"], "control_owner": seq["control_mode"]}

                async def observe(self, region=None, ocr=True, include_image=False):
                    seq["n"] += 1
                    return (
                        {
                            "frame_id": f"f-{seq['n']}",
                            "captured_at": "2026-01-01T00:00:00Z",
                            "original_size": [1920, 1080],
                            "ocr": {"words": [{"text": "synthetic-p01", "confidence": 99, "box": [10, 10, 50, 20]}]},
                        },
                        None,
                    )

                async def action(self, frame_id, kind, **kwargs):
                    calls.append((kind, (frame_id,), kwargs))
                    seq["n"] += 1
                    return ({"frame_id": f"f-{seq['n']}", "captured_at": "2026-01-01T00:00:01Z", "original_size": [1920, 1080], "ocr": {"words": []}}, None)

                async def shot(self, region=None, label=None):
                    return {"shot_path": str(shot_file)}

                async def release(self):
                    return {"released": True}

            transport = GLKVMNativeTransport()
            transport._client = FakeRemoteClient()
            transport.grab_frame()
            transport.double_click(100, 200)
            self.assertEqual(calls[-1][0], "click")
            self.assertEqual(calls[-1][2]["count"], 2)
            transport.scroll(-3)
            self.assertEqual(calls[-1][0], "scroll")
            self.assertEqual((calls[-1][2]["dy"], calls[-1][2]["x"], calls[-1][2]["y"]), (-3, 960, 540))
            transport.backspace(2)
            self.assertNotEqual(calls[-2][1][0], calls[-1][1][0])  # frame_id advances per step
            with self.assertRaises(ChineseEntryUnqualifiedError):
                transport.send_text_hid("中文")
            copied_shot = transport.save_shot(tmp_dir / "evidence" / "cap.png", label="test")
            self.assertTrue(Path(copied_shot).is_file())
            self.assertEqual(transport.release_lease(), {"released": True})

            # KVMUIDriver recognizes real glkvm control_mode values ("manual", "paused", "handoff")
            drv = KVMUIDriver(transport, expected_size=(1920, 1080), long_text_qualified=True)
            self.assertIsInstance(drv.text_strategy, DraftOnlyFallbackStrategy)
            for mode in ("manual", "paused", "handoff"):
                seq["control_mode"] = mode
                with self.assertRaises(HumanInterruptError):
                    drv.check_human_interrupt()
            seq["control_mode"] = "agent"
            drv.check_human_interrupt()

            # from_config defaults to DraftOnlyFallbackStrategy on glkvm without clipboard/ime, and wires RapidOCR for pikvm
            from_cfg = KVMUIDriver.from_config({"kvm_backend": "glkvm", "long_text_qualified": True})
            self.assertIsInstance(from_cfg.text_strategy, DraftOnlyFallbackStrategy)
            sentinel_ocr = CallableOCREngine(lambda img, reg: ())
            with patch("med_autopractice.adapters.drivers.ocr.RapidOCRBackend", return_value=sentinel_ocr) as mock_ocr:
                pikvm_drv = KVMUIDriver.from_config({"kvm_backend": "pikvm", "base_url": "http://127.0.0.1:8080"})
                mock_ocr.assert_called_once()
                self.assertIs(pikvm_drv.ocr_engine, sentinel_ocr)


if __name__ == "__main__":
    unittest.main()
