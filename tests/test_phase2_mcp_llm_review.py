"""Phase 2 tests: MCP Server, dual-track LLM clinical proposer with PHI guard, and OPL MiniApp review UI."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.agent.task_engine import JsonlAuditSink  # noqa: E402
from med_autopractice.agent.task_store import SyntheticSqliteTaskStore  # noqa: E402
from med_autopractice.domain.task import Fact, Snapshot, SubjectRef, TaskRequest  # noqa: E402
from med_autopractice.mcp_server import MAPMCPServer, REVIEW_UI_URI  # noqa: E402
from med_autopractice.medicine.llm_proposer import LLMClinicalProposer  # noqa: E402
from med_autopractice.medicine.phi_guard import PHIGuard  # noqa: E402
from med_autopractice.review_ui.renderer import render_review_app_html  # noqa: E402
from med_autopractice.review_ui.server import LocalReviewServer  # noqa: E402
from med_autopractice.runtime import build_synthetic_runtime  # noqa: E402


def _synthetic_runtime(store_path: Path):
    rt, _ui = build_synthetic_runtime(
        SyntheticSqliteTaskStore(store_path),
        JsonlAuditSink(store_path.with_suffix(".audit.jsonl")),
    )
    return rt


class _MockLLMClient:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, *, response_format=None) -> str:
        self.calls.append(messages)
        return self._responses.pop(0)


class Phase2MCPLLMReviewTests(unittest.TestCase):
    def test_mcp_stdio_and_http_full_clinical_and_record_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "tasks.sqlite"
            collection_path = Path(tmp) / "synthetic-collection.json"
            fixture = ROOT / "tests/fixtures/synthetic-history-collection.json"
            collection_path.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

            # Verify stdio JSON-RPC invocation via scripts/map_mcp.py subprocess
            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "test"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": REVIEW_UI_URI}},
                {
                    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {
                        "name": "map_structure_record",
                        "arguments": {"collection_path": str(collection_path), "record_id": "SYNTH-REC-1"},
                    },
                },
                {
                    "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {
                        "name": "map_start_task",
                        "arguments": {
                            "request_id": "mcp-req-1",
                            "capability_id": "write_inpatient_notes",
                            "intent": "起草今日病程",
                            "site_id": "demo-site",
                            "patient_id": "synthetic-p1",
                            "encounter_id": "synthetic-e1",
                            "authority_ref": "demo://grant",
                            "inputs": {"sections": {"观察": "患者今日体温平稳"}},
                        },
                    },
                },
            ]
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts/map_mcp.py"), "--mode", "synthetic", "--store", str(store)],
                input="\n".join(json.dumps(r, ensure_ascii=False) for r in reqs) + "\n",
                capture_output=True, text=True, check=True,
            )
            responses = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
            self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4, 5])
            tool_names = {t["name"] for t in responses[1]["result"]["tools"]}
            self.assertEqual(len(tool_names), 10)
            self.assertIn("map_submit_task", tool_names)
            self.assertIn("map_cancel_task", tool_names)
            self.assertIn("text/html;profile=mcp-app", responses[2]["result"]["contents"][0]["mimeType"])
            self.assertEqual(responses[3]["result"]["structuredContent"]["record_id"], "SYNTH-REC-1")
            started = responses[4]["result"]["structuredContent"]
            self.assertEqual(started["status"], "ready_to_submit")
            self.assertIn("ui", responses[4]["result"]["_meta"])

            # Verify loopback HTTP MCP server shares persistent runtime state
            runtime = _synthetic_runtime(store)
            server = MAPMCPServer(runtime, repo_root=ROOT)
            with self.assertRaises(ValueError):
                server.start_http(host="0.0.0.0", port=0)
            url = server.start_http(host="127.0.0.1", port=0)
            try:
                def post_rpc(payload: dict) -> dict:
                    req = Request(
                        url,
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(req, timeout=5.0) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                listed = post_rpc({
                    "jsonrpc": "2.0", "id": 10, "method": "tools/call",
                    "params": {"name": "map_list_tasks", "arguments": {"patient_id": "synthetic-p1"}},
                })
                self.assertEqual(listed["result"]["structuredContent"]["count"], 1)

                submitted = post_rpc({
                    "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                    "params": {
                        "name": "map_submit_task",
                        "arguments": {
                            "task_id": started["task_id"],
                            "clinician_id": "demo-clinician",
                            "proposal_digest": started["proposal"]["digest"],
                            "snapshot_revision": started["snapshot"]["revision"],
                        },
                    },
                })["result"]["structuredContent"]
                self.assertEqual(submitted["status"], "needs_clinician_acceptance")

                accepted = post_rpc({
                    "jsonrpc": "2.0", "id": 12, "method": "tools/call",
                    "params": {
                        "name": "map_review_task",
                        "arguments": {
                            "task_id": started["task_id"],
                            "clinician_id": "demo-clinician",
                            "accepted": True,
                            "digest": started["proposal"]["digest"],
                        },
                    },
                })["result"]["structuredContent"]
                self.assertEqual(accepted["status"], "accepted")
            finally:
                server.stop_http()

    def test_phi_guard_dual_track_and_llm_clinical_proposer_validation(self):
        local_guard = PHIGuard("local_trusted")
        self.assertEqual(local_guard.validate_endpoint("http://127.0.0.1:11434/v1/"), "http://127.0.0.1:11434/v1")
        with self.assertRaises(ValueError):
            local_guard.validate_endpoint("https://api.cloud-llm.example.invalid/v1")

        cloud_guard = PHIGuard("pseudonymize_outbound")
        self.assertEqual(
            cloud_guard.validate_endpoint("https://api.cloud-llm.example.invalid/v1"),
            "https://api.cloud-llm.example.invalid/v1",
        )
        with self.assertRaises(ValueError):
            cloud_guard.validate_endpoint("http://api.cloud-llm.example.invalid/v1")

        subject = SubjectRef("demo-hospital", "SYNTH-PAT-9988", "SYNTH-ENC-7766")
        snapshot = Snapshot(
            subject=subject,
            revision="rev-1",
            facts=(
                Fact(
                    "lab-wbc", "患者: 张合成 身份证110101199001011234 手机13800138000 白细胞 11.4 10^9/L",
                    "demo://lis/wbc", details={"section": "观察", "kind": "observation"},
                ),
                Fact(
                    "cat-cef", "头孢曲松", "demo://catalog/CEF-1",
                    source_object_ref="demo://cat/CEF-1",
                    details={"kind": "catalog_item", "code": "CEF-1", "active": True, "item_type": "medication"},
                ),
                Fact(
                    "ord-1", "旧医嘱", "demo://orders/old-1",
                    source_object_ref="demo://ord/old-1",
                    details={"kind": "current_order", "code": "CEF-1"},
                ),
            ),
            trace_refs=("demo://trace/1",),
        )

        # Outbound mode scrubs all direct identifiers in prompt and restores them in proposal.
        note_json = json.dumps({
            "sections": {"观察": "针对 <PATIENT_01>（联系 <PHONE_01>）检验提示白细胞升高，拟继续观察。"},
            "cited_fact_ids": ["lab-wbc"],
            "knowledge_refs": ["demo://guide/resp-1"],
        }, ensure_ascii=False)
        client = _MockLLMClient([note_json])
        proposer = LLMClinicalProposer(
            client,
            model="cloud-clinical-demo",
            endpoint="https://api.cloud-llm.example.invalid/v1",
            privacy_mode="pseudonymize_outbound",
            rules_by_capability={"write_inpatient_notes": {"approved_ref": "demo://rules/note", "sections": ["观察"]}},
        )
        req = TaskRequest(
            "llm-note-1", "write_inpatient_notes", "合成查房记录",
            subject, "demo://grant", inputs={"known_identifiers": ["张合成"]},
        )
        proposal = proposer.propose(req, snapshot)
        sent_user_prompt = client.calls[0][1]["content"]
        for raw_sensitive in ("张合成", "110101199001011234", "13800138000", "SYNTH-PAT-9988", "SYNTH-ENC-7766"):
            self.assertNotIn(raw_sensitive, sent_user_prompt)
        entries = proposal.payload["sections"][0]["entries"]
        self.assertIn("SYNTH-PAT-9988", entries[-1]["text"])
        self.assertIn("13800138000", entries[-1]["text"])
        self.assertEqual(proposal.generator_ref, "llm://pseudonymize_outbound/cloud-clinical-demo")

        # Hallucinated fact_id must be rejected deterministically.
        bad_client = _MockLLMClient([json.dumps({
            "sections": {"观察": "捏造结果"},
            "cited_fact_ids": ["nonexistent-fact-999"],
        }, ensure_ascii=False)])
        bad_proposer = LLMClinicalProposer(
            bad_client, model="local-vllm", endpoint="http://127.0.0.1:8000/v1",
            privacy_mode="local_trusted",
            rules_by_capability={"write_inpatient_notes": {"approved_ref": "demo://rules/note", "sections": ["观察"]}},
        )
        with self.assertRaises(ValueError):
            bad_proposer.propose(req, snapshot)

        # Order draft via local_trusted must pass through proposals.make_orders catalog gating.
        order_json = json.dumps({
            "changes": [{
                "action": "change",
                "code": "CEF-1",
                "target_ref": "demo://ord/old-1",
                "dose": "2.0",
                "unit": "g",
                "route": "IV",
                "frequency": "qd",
                "reason": "结合白细胞升高调整抗感染方案",
                "fact_ids": ["lab-wbc"],
            }],
            "cited_fact_ids": ["lab-wbc"],
        }, ensure_ascii=False)
        order_client = _MockLLMClient([order_json])
        order_proposer = LLMClinicalProposer(
            order_client, model="local-vllm", endpoint="http://127.0.0.1:8000/v1",
            privacy_mode="local_trusted",
            rules_by_capability={"revise_orders": {"approved_ref": "demo://rules/ord"}},
        )
        order_prop = order_proposer.propose(
            TaskRequest("llm-ord-1", "revise_orders", "调整头孢曲松", subject, "demo://grant"),
            snapshot,
        )
        self.assertEqual(order_prop.payload["type"], "order_changes")
        self.assertEqual(order_prop.generator_ref, "llm://local_trusted/local-vllm")

    def test_local_review_server_token_auth_and_end_to_end_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _synthetic_runtime(Path(tmp) / "tasks.sqlite")
            started = runtime.start(TaskRequest(
                "ui-req-1", "revise_orders", "调整降压医嘱",
                SubjectRef("demo-site", "synthetic-p2", "synthetic-e2"),
                "demo://grant",
            ))
            # Check XSS-safe escaping in HTML rendering
            escaped_html = render_review_app_html(initial_state={**started, "task_id": "<script>evil()</script>"})
            self.assertNotIn("<script>evil()</script>", escaped_html)

            with self.assertRaises(ValueError):
                LocalReviewServer(runtime, host="0.0.0.0")

            with LocalReviewServer(runtime, host="127.0.0.1", port=0) as srv:
                # Missing token is rejected with HTTP 401.
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(f"{srv.url}/api/tasks", timeout=5.0)
                self.assertEqual(ctx.exception.code, 401)

                headers = {"X-MAP-Token": srv.token, "Content-Type": "application/json"}
                req_sub = Request(
                    f"{srv.url}/api/tasks/{started['task_id']}/submit",
                    data=json.dumps({
                        "clinician_id": "demo-clinician",
                        "proposal_digest": started["proposal"]["digest"],
                        "snapshot_revision": started["snapshot"]["revision"],
                    }).encode("utf-8"),
                    headers=headers, method="POST",
                )
                with urlopen(req_sub, timeout=5.0) as resp:
                    sub_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(sub_data["task"]["status"], "needs_clinician_acceptance")

                req_rev = Request(
                    f"{srv.url}/api/tasks/{started['task_id']}/review",
                    data=json.dumps({
                        "clinician_id": "demo-clinician",
                        "accepted": True,
                        "digest": started["proposal"]["digest"],
                    }).encode("utf-8"),
                    headers=headers, method="POST",
                )
                with urlopen(req_rev, timeout=5.0) as resp:
                    rev_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(rev_data["task"]["status"], "accepted")
                self.assertEqual(runtime.show(started["task_id"])["status"], "accepted")
