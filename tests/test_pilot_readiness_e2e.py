"""End-to-end pilot readiness test executed from a verified offline bundle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PilotReadinessOfflineBundleTests(unittest.TestCase):
    def test_single_offline_bundle_covers_inpatient_notes_orders_and_fault_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bundle_dir = tmp_dir / "map-offline-bundle"

            subprocess.run([
                sys.executable, str(ROOT / "scripts/build_offline_bundle.py"),
                "--output", str(bundle_dir),
            ], capture_output=True, text=True, check=True)
            verify = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_offline_bundle.py"),
                "--verify", str(bundle_dir),
            ], capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(verify.stdout)["valid"])

            proj = subprocess.run(
                [sys.executable, str(bundle_dir / "scripts/project_status.py"), "--json"],
                cwd=tmp_dir, capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(proj.stdout)["package"]["content_integrity"], "verified")

            # Verify bundled MCP stdio handshake & UI declarations
            mcp_reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "pilot-host"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            mcp_proc = subprocess.run(
                [sys.executable, str(bundle_dir / "scripts/map_mcp.py"), "--mode", "synthetic", "--store", str(tmp_dir / "mcp.sqlite")],
                input="\n".join(json.dumps(r) for r in mcp_reqs) + "\n",
                cwd=tmp_dir, capture_output=True, text=True, check=True,
            )
            mcp_resps = [json.loads(line) for line in mcp_proc.stdout.strip().splitlines() if line.strip()]
            self.assertIn("io.modelcontextprotocol/ui", mcp_resps[0]["result"]["capabilities"]["extensions"])
            self.assertEqual(len(mcp_resps[1]["result"]["tools"]), 10)

            # Execute dual-pilot workflows strictly from bundle_dir/src
            runner_script = tmp_dir / "pilot_runner.py"
            runner_script.write_text(textwrap.dedent(f"""
                from datetime import datetime, timezone
                import json
                import os
                from pathlib import Path
                import subprocess
                import sys
                import med_autopractice
                from examples.sandbox_his.sandbox_engine import SyntheticHISSandbox
                from med_autopractice.adapters.drivers.desktop_win import WindowsUIADriver
                from med_autopractice.adapters.session_guard import SessionLockedError
                from med_autopractice.adapters.ui import UIStateError
                from med_autopractice.deployment import build_site_runtime
                from med_autopractice.domain.task import ClinicianApproval, SubjectRef, TaskRequest

                bundle_src = Path({json.dumps(str((bundle_dir / "src").resolve()))})
                assert Path(med_autopractice.__file__).resolve().is_relative_to(bundle_src), med_autopractice.__file__

                tmp_dir = Path({json.dumps(str(tmp_dir.resolve()))})
                site = tmp_dir / "pilot-site"
                site.mkdir(mode=0o700)
                (site / "playbooks").mkdir(mode=0o700)
                key_file = tmp_dir / "pilot.key"
                key_file.write_bytes(b"pilot-32-byte-aes-master-key-000")
                key_file.chmod(0o600)

                deps = json.loads((bundle_src.parent / "contracts/site-dependencies.json").read_text(encoding="utf-8"))
                deps["profile_id"] = "pilot-site"
                for item in deps["dependencies"]:
                    item["state"] = "qualified"
                    item["evidence_ref"] = f"demo://ev/{{item['id']}}"
                    item["qualification_ref"] = f"demo://qual/{{item['id']}}"
                    if item["owner"] == "adapter":
                        item["transport"] = "computer_use"
                (site / "dependencies.json").write_text(json.dumps(deps, ensure_ascii=False, indent=2), encoding="utf-8")

                playbooks = {{
                    "collect.json": {{
                        "schema_version": "map-site-playbook.v2",
                        "playbook_id": "collect-pilot",
                        "site_id": "pilot-site",
                        "title": "采集实验室检验、医嘱与目录",
                        "systems": ["inpatient_ehr"],
                        "steps": [
                            {{"id": "dismiss", "intent": "关闭弹窗", "primitive": "handle_dialog",
                              "dialog_rules": [{{"trigger_text": "提示弹窗", "action": "dismiss", "target": "确定"}}],
                              "expect_text": "住院病历工作站", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "verify", "intent": "核对抬头身份", "primitive": "verify_subject",
                              "selector": "#patient-header", "expect_text": "患者",
                              "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "open_labs", "intent": "回到检验结果首页", "primitive": "click",
                              "selector": "#tab-labs", "target": "检验结果",
                              "expect_text": "住院病历工作站", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "labs", "intent": "翻页采集检验", "primitive": "scroll_collect",
                              "selector": "table.labs", "extract_key": "results", "scope": "results", "max_pages": 5,
                              "expect_text": "住院病历工作站", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "orders", "intent": "采集当前医嘱", "primitive": "read_table",
                              "selector": "#current-orders-table", "extract_key": "current_orders", "scope": "current_orders",
                              "expect_text": "住院病历工作站", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "catalog", "intent": "采集本地医嘱目录", "primitive": "read_table",
                              "selector": "#catalog-table", "extract_key": "local_catalog", "scope": "local_catalog",
                              "expect_text": "住院病历工作站", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                        ],
                    }},
                    "write_note.json": {{
                        "schema_version": "map-site-playbook.v2",
                        "playbook_id": "write-note",
                        "site_id": "pilot-site",
                        "title": "写入住院病程草稿",
                        "systems": ["inpatient_ehr"],
                        "steps": [
                            {{"id": "verify", "intent": "核对身份", "primitive": "verify_subject", "selector": "#patient-header",
                              "expect_text": "患者", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "enter", "intent": "录入病程", "primitive": "input_text", "selector": "#note-editor",
                              "target": "病程编辑器", "text_param": "draft_text", "expect_text": "患者",
                              "effect": "draft_write", "on_failure": "stop", "state": "verified"}},
                            {{"id": "save", "intent": "保存草稿", "primitive": "click", "selector": "#save-draft",
                              "target": "保存草稿", "expect_text": "已保存", "effect": "draft_write",
                              "on_failure": "stop", "evidence": "screenshot_ref", "state": "verified"}},
                            {{"id": "ref", "intent": "读引用", "primitive": "read_text", "selector": "#object-ref",
                              "extract_key": "object_ref", "expect_text": "demo://", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "st", "intent": "读状态", "primitive": "read_text", "selector": "#object-state",
                              "extract_key": "state", "expect_text": "已保存", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                        ],
                    }},
                    "readback_note.json": {{
                        "schema_version": "map-site-playbook.v2",
                        "playbook_id": "readback-note",
                        "site_id": "pilot-site",
                        "title": "重开回读病程草稿",
                        "systems": ["inpatient_ehr"],
                        "steps": [
                            {{"id": "verify", "intent": "回读身份", "primitive": "verify_subject", "selector": "#patient-header",
                              "expect_text": "患者", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "ref", "intent": "读引用", "primitive": "read_text", "selector": "#object-ref",
                              "extract_key": "object_ref", "expect_text": "demo://", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "st", "intent": "读状态", "primitive": "read_text", "selector": "#object-state",
                              "extract_key": "state", "expect_text": "已保存", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "txt", "intent": "读正文", "primitive": "read_text", "selector": "#note-editor",
                              "extract_key": "draft_text", "expect_text": "*", "effect": "read_only",
                              "on_failure": "stop", "evidence": "screenshot_ref", "state": "verified"}},
                        ],
                    }},
                    "find_note.json": {{
                        "schema_version": "map-site-playbook.v2",
                        "playbook_id": "find-note",
                        "site_id": "pilot-site",
                        "title": "查找已保存病程",
                        "systems": ["inpatient_ehr"],
                        "steps": [
                            {{"id": "tbl", "intent": "读草稿列表", "primitive": "read_table", "selector": "#saved-notes-table",
                              "extract_key": "candidates", "expect_text": "*", "effect": "read_only",
                              "on_failure": "stop", "evidence": "screenshot_ref", "state": "verified"}},
                        ],
                    }},
                    "write_orders.json": {{
                        "schema_version": "map-site-playbook.v2",
                        "playbook_id": "write-orders",
                        "site_id": "pilot-site",
                        "title": "录入医嘱调整",
                        "systems": ["inpatient_ehr"],
                        "steps": [
                            {{"id": "verify", "intent": "核对身份", "primitive": "verify_subject", "selector": "#patient-header",
                              "expect_text": "患者", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "enter", "intent": "输入医嘱JSON", "primitive": "input_text", "selector": "#orders-editor",
                              "target": "医嘱编辑器", "text_param": "changes_json", "expect_text": "*",
                              "effect": "reviewed_write", "on_failure": "stop", "state": "verified"}},
                            {{"id": "sub", "intent": "提交医嘱", "primitive": "click", "selector": "#submit-orders",
                              "target": "提交医嘱", "expect_text": "已录入", "effect": "reviewed_write",
                              "on_failure": "stop", "evidence": "screenshot_ref", "state": "verified"}},
                            {{"id": "ref", "intent": "读批次引用", "primitive": "read_text", "selector": "#object-ref",
                              "extract_key": "object_ref", "expect_text": "demo://", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "st", "intent": "读状态", "primitive": "read_text", "selector": "#object-state",
                              "extract_key": "state", "expect_text": "已录入", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                        ],
                    }},
                    "readback_orders.json": {{
                        "schema_version": "map-site-playbook.v2",
                        "playbook_id": "readback-orders",
                        "site_id": "pilot-site",
                        "title": "重开回读医嘱表",
                        "systems": ["inpatient_ehr"],
                        "steps": [
                            {{"id": "verify", "intent": "回读身份", "primitive": "verify_subject", "selector": "#patient-header",
                              "expect_text": "患者", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "ref", "intent": "读批次引用", "primitive": "read_text", "selector": "#object-ref",
                              "extract_key": "object_ref", "expect_text": "demo://", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "st", "intent": "读状态", "primitive": "read_text", "selector": "#object-state",
                              "extract_key": "state", "expect_text": "已录入", "effect": "read_only", "on_failure": "stop", "state": "verified"}},
                            {{"id": "tbl", "intent": "读医嘱表格", "primitive": "read_table", "selector": "#orders-table",
                              "extract_key": "orders_table", "expect_text": "*", "effect": "read_only",
                              "on_failure": "stop", "evidence": "screenshot_ref", "state": "verified"}},
                        ],
                    }},
                }}
                for fname, doc in playbooks.items():
                    (site / "playbooks" / fname).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

                deployment = {{
                    "schema_version": "map-local-deployment.v1",
                    "site_id": "pilot-site",
                    "authority_ref": "demo://grant",
                    "authorized_clinicians": ["doc-1"],
                    "driver": {{"kind": "windows_uia"}},
                    "evidence_security": {{
                        "encryption": {{"mode": "required", "key_file": str(key_file), "prefer_aesgcm": False}},
                        "retention": {{"screenshot_max_age_days": 7, "task_log_max_age_days": 30}},
                    }},
                    "ui_profile": {{
                        "product": "pilot-sandbox-his", "version": "1.0", "role": "clinician",
                        "display_size": [1280, 720], "qualification_ref": "demo://qual/ui",
                        "write_capabilities": ["write_inpatient_notes", "revise_orders"],
                        "long_text_qualified": True,
                    }},
                    "playbooks": {{
                        "write_inpatient_notes": {{
                            "collect": ["collect-pilot"], "write": "write-note", "readback": "readback-note", "find": "find-note",
                            "default_section": "观察",
                            "extra_complete_scopes": ["patient_context", "current_orders", "nursing"],
                            "state_map": {{"已保存": "saved_draft"}},
                        }},
                        "revise_orders": {{
                            "collect": ["collect-pilot"], "write": "write-orders", "readback": "readback-orders",
                            "extra_complete_scopes": ["patient_context", "nursing"],
                            "state_map": {{"已录入": "entered"}},
                            "order_table_key": "orders_table",
                        }},
                    }},
                    "capabilities": {{"write_inpatient_notes": "saved_draft", "revise_orders": "entered"}},
                    "rules": {{
                        "write_inpatient_notes": {{"approved_ref": "demo://rules/note", "sections": ["观察"]}},
                        "revise_orders": {{"approved_ref": "demo://rules/ord"}},
                    }},
                }}
                (site / "deployment.json").write_text(json.dumps(deployment, ensure_ascii=False, indent=2), encoding="utf-8")

                subj = SubjectRef("pilot-site", "synthetic-p01", "synthetic-e01")
                wrong_subj = SubjectRef("pilot-site", "synthetic-p01", "wrong-e99")

                # A. Wrong encounter is blocked during start/read
                sb = SyntheticHISSandbox(site_id="pilot-site")
                drv = WindowsUIADriver(backend=sb, screenshot_dir=site / "evidence")
                rt = build_site_runtime(site, driver=drv)
                blocked_wrong = rt.start(TaskRequest(
                    "pilot-wrong-enc", "write_inpatient_notes", "错就诊", wrong_subj, "demo://grant",
                    inputs={{"sections": {{"观察": "今日平稳"}}}},
                ))
                assert blocked_wrong["status"] == "blocked", blocked_wrong

                # B. Save timeout during note draft -> execution_uncertain -> reconcile -> reopen readback -> accepted
                sb.simulate_save_timeout = True
                note_task = rt.start(TaskRequest(
                    "pilot-note-timeout", "write_inpatient_notes", "超时恢复写病程", subj, "demo://grant",
                    inputs={{"sections": {{"观察": "患者今日体温降至正常，继续观察。"}}}},
                ))
                assert note_task["status"] == "needs_pre_submit_review"
                try:
                    rt.submit(note_task["task_id"], ClinicianApproval(
                        note_task["task_id"], "doc-1", note_task["proposal"]["digest"], note_task["snapshot"]["revision"],
                    ))
                    raise AssertionError("expected TimeoutError")
                except TimeoutError:
                    pass
                assert rt.show(note_task["task_id"])["status"] == "execution_uncertain"

                sb.simulate_save_timeout = False
                reconciled = rt.reconcile(note_task["task_id"])
                assert reconciled["status"] == "needs_clinician_acceptance", reconciled
                assert reconciled["execution"]["readback"]["subject"]["encounter_id"] == "synthetic-e01"
                assert reconciled["execution"]["readback"]["screenshot_ref"].endswith(".png.enc")
                accepted_note = rt.review(
                    note_task["task_id"], "doc-1", accepted=True, digest=note_task["proposal"]["digest"],
                )
                assert accepted_note["status"] == "accepted"

                # C. Readback on wrong encounter catches mismatch and stays execution_uncertain
                sb_wrong_rb = SyntheticHISSandbox(site_id="pilot-site", wrong_readback_encounter="wrong-e-after")
                rt_wrong_rb = build_site_runtime(site, driver=WindowsUIADriver(backend=sb_wrong_rb, screenshot_dir=site / "evidence"))
                t_wrb = rt_wrong_rb.start(TaskRequest(
                    "pilot-wrong-rb", "write_inpatient_notes", "写后跳错就诊", subj, "demo://grant",
                    inputs={{"sections": {{"观察": "写后跳错就诊校验"}}}},
                ))
                try:
                    rt_wrong_rb.submit(t_wrb["task_id"], ClinicianApproval(
                        t_wrb["task_id"], "doc-1", t_wrb["proposal"]["digest"], t_wrb["snapshot"]["revision"],
                    ))
                    raise AssertionError("expected UIStateError on wrong readback encounter")
                except UIStateError:
                    pass
                assert rt_wrong_rb.show(t_wrb["task_id"])["status"] == "execution_uncertain"

                # D. Partial order failure stops at execution_uncertain; full order roundtrip succeeds
                order_changes = [
                    {{"action": "add", "code": "NEW-MED", "dose": "250", "unit": "mg", "route": "iv", "frequency": "q12h",
                      "reason": "CRP升高", "fact_ids": ["lab-2"]}},
                    {{"action": "change", "code": "DEMO-MED", "target_ref": "demo://his/order/ord-1",
                      "dose": "750", "unit": "mg", "route": "po", "frequency": "bid",
                      "reason": "加量", "fact_ids": ["lab-1"]}},
                    {{"action": "stop", "code": "STOP-MED", "target_ref": "demo://his/order/ord-2",
                      "reason": "停用", "fact_ids": ["lab-1"]}},
                ]
                sb_part = SyntheticHISSandbox(site_id="pilot-site", simulate_partial_order_failure_index=1)
                rt_part = build_site_runtime(site, driver=WindowsUIADriver(backend=sb_part, screenshot_dir=site / "evidence"))
                t_part = rt_part.start(TaskRequest(
                    "pilot-ord-partial", "revise_orders", "部分失败医嘱", subj, "demo://grant",
                    inputs={{"changes": order_changes}},
                ))
                sub_part = rt_part.submit(t_part["task_id"], ClinicianApproval(
                    t_part["task_id"], "doc-1", t_part["proposal"]["digest"], t_part["snapshot"]["revision"],
                ))
                assert sub_part["status"] == "execution_uncertain", sub_part

                sb_ord = SyntheticHISSandbox(site_id="pilot-site")
                rt_ord = build_site_runtime(site, driver=WindowsUIADriver(backend=sb_ord, screenshot_dir=site / "evidence"))
                t_ord = rt_ord.start(TaskRequest(
                    "pilot-ord-ok", "revise_orders", "完整医嘱调整", subj, "demo://grant",
                    inputs={{"changes": order_changes}},
                ))
                sub_ord = rt_ord.submit(t_ord["task_id"], ClinicianApproval(
                    t_ord["task_id"], "doc-1", t_ord["proposal"]["digest"], t_ord["snapshot"]["revision"],
                ))
                assert sub_ord["status"] == "needs_clinician_acceptance", sub_ord
                assert sub_ord["execution"]["readback"]["visible_payload"]["type"] == "order_changes"
                acc_ord = rt_ord.review(t_ord["task_id"], "doc-1", accepted=True, digest=t_ord["proposal"]["digest"])
                assert acc_ord["status"] == "accepted"

                # E. Encrypted screenshots only, retention cleanup via bundled CLI, and session lock guard
                assert not list((site / "evidence").glob("*.png"))
                enc_files = list((site / "evidence").glob("*.png.enc"))
                assert enc_files
                stale_ts = datetime.now(timezone.utc).timestamp() - 10 * 86400
                for p in enc_files:
                    os.utime(p, (stale_ts, stale_ts))
                ret_out = subprocess.run([
                    sys.executable, str(bundle_src.parent / "scripts/map_runtime.py"),
                    "retention", "--site", str(site),
                ], capture_output=True, text=True, check=True)
                assert json.loads(ret_out.stdout)["deleted"] >= len(enc_files)

                t_lock = rt_ord.start(TaskRequest(
                    "pilot-lock-test", "write_inpatient_notes", "锁屏测试", subj, "demo://grant",
                    inputs={{"sections": {{"观察": "锁屏前内容"}}}},
                ))
                sb_ord.session_locked = True
                try:
                    rt_ord.submit(t_lock["task_id"], ClinicianApproval(
                        t_lock["task_id"], "doc-1", t_lock["proposal"]["digest"], t_lock["snapshot"]["revision"],
                    ))
                    raise AssertionError("expected SessionLockedError")
                except SessionLockedError:
                    pass
                print("PILOT_E2E_OK")
            """), encoding="utf-8")

            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = f"{(bundle_dir / 'src').resolve()}{os.pathsep}{ROOT}"
            run_pilot = subprocess.run(
                [sys.executable, str(runner_script)],
                cwd=tmp_dir, env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(
                run_pilot.returncode, 0,
                msg=f"stderr={run_pilot.stderr}\nstdout={run_pilot.stdout}",
            )
            self.assertIn("PILOT_E2E_OK", run_pilot.stdout)


if __name__ == "__main__":
    unittest.main()
