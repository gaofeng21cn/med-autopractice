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
from med_autopractice.adapters.playbook_runner import HumanInterruptError
from med_autopractice.adapters.ui import UIClinicalSystem, UIProfile
from med_autopractice.deployment import build_site_runtime
from med_autopractice.domain.task import ClinicianApproval, SubjectRef, TaskRequest
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
                text_strategy=PureHIDSegmentedStrategy(max_segment_chars=30, max_retries=2),
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
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

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


if __name__ == "__main__":
    unittest.main()
