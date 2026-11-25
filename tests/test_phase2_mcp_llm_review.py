"""Phase 2 tests: MCP Server, dual-track LLM clinical proposer with PHI guard, and OPL MiniApp review UI."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.agent.task_engine import JsonlAuditSink  # noqa: E402
from med_autopractice.agent.task_store import SyntheticSqliteTaskStore  # noqa: E402
from med_autopractice.domain.task import Fact, Snapshot, SubjectRef, TaskRequest  # noqa: E402
from med_autopractice.mcp_server import MAPMCPServer, REVIEW_UI_URI  # noqa: E402
from med_autopractice.medicine.llm_proposer import LLMClinicalProposer  # noqa: E402
from med_autopractice.medicine.phi_guard import PHIGuard  # noqa: E402
from med_autopractice.review_ui.renderer import build_review_card_model, render_review_app_html  # noqa: E402
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
            self.assertIn("io.modelcontextprotocol/ui", responses[0]["result"]["capabilities"]["extensions"])
            tools = responses[1]["result"]["tools"]
            tool_names = {t["name"] for t in tools}
            self.assertEqual(len(tool_names), 10)
            self.assertIn("map_submit_task", tool_names)
            self.assertIn("map_cancel_task", tool_names)
            submit_tool = next(t for t in tools if t["name"] == "map_submit_task")
            self.assertEqual(submit_tool["_meta"]["ui"]["resourceUri"], REVIEW_UI_URI)
            self.assertEqual(submit_tool["_meta"]["ui"]["visibility"], ["model", "app"])
            self.assertIn("text/html;profile=mcp-app", responses[2]["result"]["contents"][0]["mimeType"])
            self.assertEqual(responses[3]["result"]["structuredContent"]["record_id"], "SYNTH-REC-1")
            started = responses[4]["result"]["structuredContent"]
            self.assertEqual(started["status"], "ready_to_submit")
            self.assertEqual(responses[4]["result"]["_meta"]["ui"]["resourceUri"], REVIEW_UI_URI)
            self.assertEqual(responses[4]["result"]["_meta"]["ui"]["card"]["subject"]["patient_id"], "synthetic-p1")
            self.assertEqual(responses[4]["result"]["_meta"]["ui"]["card"]["capability_id"], "write_inpatient_notes")

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
                # Card projection preserves readback subject & visible_payload
                accepted_card = build_review_card_model(rev_data["task"])
                self.assertEqual(accepted_card["subject"]["patient_id"], "synthetic-p2")
                self.assertEqual(accepted_card["execution"]["readback_subject"]["encounter_id"], "synthetic-e2")
                self.assertIsNotNone(accepted_card["execution"]["readback_visible_payload"])

    def test_mcp_app_host_handshake_and_tool_roundtrip(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node binary not installed for simulated MCP App host test")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _synthetic_runtime(Path(tmp) / "tasks.sqlite")
            mcp = MAPMCPServer(runtime, repo_root=ROOT)
            res = mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": REVIEW_UI_URI}})
            html = res["result"]["contents"][0]["text"]
            m = re.search(r"<script>([\s\S]*)</script>", html)
            self.assertIsNotNone(m)
            app_js = m.group(1)

            start_res = mcp.call_tool("map_start_task", {
                "request_id": "mcp-host-1",
                "capability_id": "revise_orders",
                "intent": "调整医嘱",
                "site_id": "demo-site",
                "patient_id": "synthetic-p3",
                "encounter_id": "synthetic-e3",
                "authority_ref": "demo://grant",
            })
            submit_res = mcp.call_tool("map_submit_task", {
                "task_id": start_res["structuredContent"]["task_id"],
                "clinician_id": "demo-clinician",
                "proposal_digest": start_res["structuredContent"]["proposal"]["digest"],
                "snapshot_revision": start_res["structuredContent"]["snapshot"]["revision"],
            })
            review_res = mcp.call_tool("map_review_task", {
                "task_id": start_res["structuredContent"]["task_id"],
                "clinician_id": "demo-clinician",
                "accepted": True,
                "digest": start_res["structuredContent"]["proposal"]["digest"],
            })
            uncertain_res = {
                "isError": False,
                "structuredContent": {
                    **start_res["structuredContent"],
                    "status": "execution_uncertain",
                    "execution": {"receipt": {"object_ref": "demo://ui/obj/x", "state": "entered"}, "readback": None},
                },
            }

            harness = Path(tmp) / "host_sim.js"
            harness.write_text(f"""
const assert = require('assert');
const elements = new Map();
function makeEl(id) {{
  const obj = {{
    id, textContent: '', value: id === 'clinician-id' ? 'demo-clinician' : '',
    disabled: false, className: '', style: {{}}, listeners: {{}},
    children: [],
    addEventListener(ev, fn) {{ this.listeners[ev] = fn; }},
    appendChild(ch) {{ this.children.push(ch); }},
    getBoundingClientRect() {{ return {{ width: 640, height: 480 }}; }}
  }};
  elements.set(id, obj);
  return obj;
}}
for (const id of [
  'app-root','hdr-title','hdr-subject','hdr-status','hdr-task','missing-banner',
  'prop-meta','prop-body','facts-body','readback-summary','readback-body',
  'clinician-id','action-note','btn-submit','btn-reconcile','btn-accept',
  'btn-reject','btn-cancel','action-feedback'
]) makeEl(id);

const postedToHost = [];
const listeners = [];
const hostOrigin = 'https://mcp-host.example';
function deliverHostMessage(data) {{
  listeners.forEach(fn => fn({{ source: window.parent, origin: hostOrigin, data }}));
}}
const startRes = {json.dumps(start_res, ensure_ascii=False)};
const submitRes = {json.dumps(submit_res, ensure_ascii=False)};
const reviewRes = {json.dumps(review_res, ensure_ascii=False)};
const uncertainRes = {json.dumps(uncertain_res, ensure_ascii=False)};
let submitMode = 'normal';

global.document = {{
  documentElement: {{ dataset: {{}}, style: {{ vars: {{}}, setProperty(k,v) {{ this.vars[k]=v; }} }} }},
  getElementById(id) {{ return elements.get(id); }},
  createElement(tag) {{ return {{ tag, textContent: '', children: [], appendChild(c) {{ this.children.push(c); }} }}; }}
}};
global.window = {{
  addEventListener(ev, fn) {{ if (ev === 'message') listeners.push(fn); }},
  parent: {{
    postMessage(msg, targetOrigin) {{
      postedToHost.push(msg);
      if (msg.method === 'ui/initialize') {{
        assert.strictEqual(msg.params.protocolVersion, '2026-01-26');
        queueMicrotask(() => deliverHostMessage({{
            jsonrpc: '2.0', id: msg.id,
            result: {{
              protocolVersion: '2026-01-26',
              hostCapabilities: {{ serverTools: {{}} }},
              hostContext: {{ theme: 'dark', styles: {{ variables: {{ '--color-background-primary': '#111' }} }} }}
            }}
        }}));
      }} else if (msg.method === 'tools/call') {{
        assert.strictEqual(targetOrigin, hostOrigin);
        let result;
        if (msg.params.name === 'map_submit_task') {{
          result = submitMode === 'uncertain' ? uncertainRes : submitRes;
        }} else if (msg.params.name === 'map_review_task') {{
          result = reviewRes;
        }}
        queueMicrotask(() => deliverHostMessage({{ jsonrpc: '2.0', id: msg.id, result }}));
      }}
    }}
  }}
}};

(async () => {{
  eval({json.dumps(app_js)});
  await new Promise(r => setTimeout(r, 30));
  assert.ok(postedToHost.some(m => m.method === 'ui/initialize'));
  assert.ok(postedToHost.some(m => m.method === 'ui/notifications/initialized'));
  assert.strictEqual(document.documentElement.dataset.theme, 'dark');
  assert.strictEqual(document.documentElement.style.vars['--color-background-primary'], '#111');

  // Deliver tool-result from host
  deliverHostMessage({{ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: startRes }});
  assert.ok(elements.get('hdr-subject').textContent.includes('synthetic-p3'));
  assert.strictEqual(elements.get('hdr-status').textContent, 'needs_pre_submit_review');
  assert.strictEqual(elements.get('btn-submit').disabled, false);

  // Uncertain submit must reflect execution_uncertain instead of faking success
  submitMode = 'uncertain';
  await elements.get('btn-submit').listeners.click();
  assert.strictEqual(elements.get('hdr-status').textContent, 'execution_uncertain');
  assert.ok(elements.get('action-feedback').textContent.includes('execution_uncertain'));
  assert.strictEqual(elements.get('btn-reconcile').disabled, false);

  // Re-deliver normal start and submit -> readback evidence rendered -> accept
  deliverHostMessage({{ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: startRes }});
  submitMode = 'normal';
  await elements.get('btn-submit').listeners.click();
  assert.strictEqual(elements.get('hdr-status').textContent, 'needs_clinician_acceptance');
  assert.ok(elements.get('readback-summary').textContent.includes('一致'));
  assert.ok(elements.get('readback-body').textContent.includes('readback_visible_payload'));

  await elements.get('btn-accept').listeners.click();
  assert.strictEqual(elements.get('hdr-status').textContent, 'accepted');
  console.log('HOST_SIM_OK');
}})().catch(err => {{ console.error(err); process.exit(1); }});
""", encoding="utf-8")
            proc = subprocess.run([node_bin, str(harness)], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")
            self.assertIn("HOST_SIM_OK", proc.stdout)
