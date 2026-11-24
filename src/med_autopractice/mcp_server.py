"""Pure-stdlib JSON-RPC 2.0 MCP Server & OPL MiniApp resource provider for MAP."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from typing import Any, IO
from urllib.parse import parse_qs, urlsplit

from med_autopractice.domain.task import ClinicianApproval, SubjectRef, TaskRequest
from med_autopractice.records.clinical_record import build_record, validate_record
from med_autopractice.review_ui.renderer import build_review_card_model, render_review_app_html
from med_autopractice.runtime import TaskRuntime
from med_autopractice.workflows.site_plan import SiteCatalog
from med_autopractice.workflows.site_sync import inspect_and_sync_site_pack

ROOT = Path(__file__).resolve().parents[2]
REVIEW_UI_URI = "ui://med-autopractice/review-card"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _read_server_version() -> str:
    manifest = ROOT / "agent/manifest.json"
    if manifest.is_file():
        try:
            return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "0.1.5")
        except (OSError, ValueError):
            pass
    return "0.1.5"


def _tool_spec(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }
    if meta is not None:
        spec["_meta"] = meta
    return spec


class MAPMCPServer:
    """In-process, stdio, and loopback HTTP MCP Server backed by TaskRuntime."""

    def __init__(
        self,
        runtime: TaskRuntime,
        *,
        site_dir: Path | None = None,
        repo_root: Path = ROOT,
    ):
        self.runtime = runtime
        self.site_dir = site_dir.resolve() if site_dir is not None else None
        self.repo_root = repo_root.resolve()
        self.version = _read_server_version()
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "map_site_status": self._tool_site_status,
            "map_structure_record": self._tool_structure_record,
            "map_start_task": self._tool_start_task,
            "map_show_task": self._tool_show_task,
            "map_list_tasks": self._tool_list_tasks,
            "map_revise_task": self._tool_revise_task,
            "map_submit_task": self._tool_submit_task,
            "map_review_task": self._tool_review_task,
            "map_reconcile_task": self._tool_reconcile_task,
            "map_cancel_task": self._tool_cancel_task,
        }

    def list_tools(self) -> list[dict[str, Any]]:
        card_meta = {"ui": {"resourceUri": REVIEW_UI_URI, "visibility": ["model", "app"]}}
        app_visible = {"ui": {"visibility": ["model", "app"]}}
        return [
            _tool_spec("map_site_status", "Inspect site deployment pack readiness and dependency plan.", {
                "site_dir": {"type": "string"},
                "capability_id": {"type": "string"},
            }),
            _tool_spec("map_structure_record", "Build and validate a structured clinical record with provenance.", {
                "collection_path": {"type": "string"},
                "collection": {"type": "object"},
                "record_id": {"type": "string"},
                "include_record": {"type": "boolean"},
            }),
            _tool_spec("map_start_task", "Start a new patient-bound clinical workflow task.", {
                "request_id": {"type": "string"},
                "capability_id": {"type": "string"},
                "intent": {"type": "string"},
                "goal": {"type": "string"},
                "site_id": {"type": "string"},
                "patient_id": {"type": "string"},
                "encounter_id": {"type": "string"},
                "authority_ref": {"type": "string"},
                "inputs": {"type": "object"},
                "auto_submit": {"type": "boolean"},
            }, ["request_id", "capability_id", "site_id", "patient_id", "encounter_id", "authority_ref"], meta=card_meta),
            _tool_spec("map_show_task", "Show full status, proposal, evidence, and readback for one task.", {
                "task_id": {"type": "string"},
            }, ["task_id"], meta=card_meta),
            _tool_spec("map_list_tasks", "List tasks filtered optionally by status or patient_id.", {
                "status": {"type": "string"},
                "patient_id": {"type": "string"},
                "limit": {"type": "integer"},
            }, meta=app_visible),
            _tool_spec("map_revise_task", "Revise proposal payload or inputs for an open task.", {
                "task_id": {"type": "string"},
                "payload": {"type": "object"},
                "inputs": {"type": "object"},
                "generator_ref": {"type": "string"},
                "knowledge_refs": {"type": "array", "items": {"type": "string"}},
            }, ["task_id"], meta=card_meta),
            _tool_spec("map_submit_task", "Submit clinician-approved proposal digest + snapshot revision.", {
                "task_id": {"type": "string"},
                "clinician_id": {"type": "string"},
                "proposal_digest": {"type": "string"},
                "snapshot_revision": {"type": "string"},
                "notes": {"type": "string"},
            }, ["task_id", "clinician_id", "proposal_digest", "snapshot_revision"], meta=card_meta),
            _tool_spec("map_review_task", "Record clinician acceptance or rejection after readback.", {
                "task_id": {"type": "string"},
                "clinician_id": {"type": "string"},
                "accepted": {"type": "boolean"},
                "feedback": {"type": "string"},
                "digest": {"type": "string"},
            }, ["task_id", "clinician_id", "accepted"], meta=card_meta),
            _tool_spec("map_reconcile_task", "Reconcile uncertain execution against the target clinical system.", {
                "task_id": {"type": "string"},
            }, ["task_id"], meta=card_meta),
            _tool_spec("map_cancel_task", "Cancel an active task and record audit reason.", {
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
                "clinician_id": {"type": "string"},
            }, ["task_id", "reason"], meta=card_meta),
        ]

    def list_resources(self) -> list[dict[str, Any]]:
        return [{
            "uri": REVIEW_UI_URI,
            "name": "MAP Clinical Review Card",
            "description": "OPL MiniApp / MCP App interactive clinical proposal and readback review UI.",
            "mimeType": "text/html;profile=mcp-app",
            "_meta": {
                "ui": {
                    "prefersBorder": True,
                    "csp": {"connectDomains": [], "resourceDomains": []},
                }
            },
        }]

    def read_resource(self, uri: str) -> dict[str, Any]:
        parsed = urlsplit(uri)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if base != REVIEW_UI_URI:
            raise KeyError(f"unknown MCP resource URI: {uri}")
        qs = parse_qs(parsed.query)
        task_id = (qs.get("task_id") or [None])[0]
        initial = self.runtime.show(task_id) if task_id else None
        html = render_review_app_html(initial_state=initial)
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "text/html;profile=mcp-app",
                "text": html,
            }]
        }

    # --- Tool Implementations ---
    def _tool_site_status(self, args: dict[str, Any]) -> dict[str, Any]:
        target_dir = Path(args["site_dir"]).expanduser().resolve() if args.get("site_dir") else self.site_dir
        if target_dir is None:
            return {"mode": "synthetic_or_unbound", "site_configured": False}
        report = inspect_and_sync_site_pack(target_dir, repo_root=self.repo_root, sync_observed=False)
        result = report.to_dict()
        capability_id = args.get("capability_id")
        if capability_id and (target_dir / "dependencies.json").is_file():
            catalog = SiteCatalog.load(target_dir / "dependencies.json")
            plan = catalog.plan(str(capability_id))
            result["capability_plan"] = {
                "capability_id": plan.capability_id,
                "dependencies_qualified": plan.dependencies_qualified,
                "blockers": list(plan.blockers),
            }
        return result

    def _tool_structure_record(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("collection_path"):
            path = Path(str(args["collection_path"])).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"collection file does not exist: {path}")
            collection = json.loads(path.read_text(encoding="utf-8"))
            default_id = path.stem
        elif isinstance(args.get("collection"), dict):
            collection = args["collection"]
            default_id = "inline-record"
        else:
            raise ValueError("collection_path or collection object is required")
        record_id = str(args.get("record_id") or default_id)
        record = build_record(collection, record_id=record_id)
        errors = validate_record(record)
        summary: dict[str, Any] = {
            "record_id": record_id,
            "valid": not errors,
            "errors": errors,
            "missing": record.get("missing", []),
            "conflicts_count": len(record.get("conflicts", [])),
            "sections_present": sorted(k for k, v in (record.get("sections") or {}).items() if v),
        }
        if args.get("include_record"):
            summary["record"] = record
        return summary

    def _tool_start_task(self, args: dict[str, Any]) -> dict[str, Any]:
        subject = SubjectRef(str(args["site_id"]), str(args["patient_id"]), str(args["encounter_id"]))
        intent = str(args.get("intent") or args.get("goal") or "").strip()
        if not intent:
            raise ValueError("map_start_task requires intent (or goal)")
        req = TaskRequest(
            request_id=str(args["request_id"]),
            capability_id=str(args["capability_id"]),
            intent=intent,
            subject=subject,
            authority_ref=str(args["authority_ref"]),
            inputs=dict(args.get("inputs") or {}),
        )
        return self.runtime.start(req, auto_submit=bool(args.get("auto_submit", False)))

    def _tool_show_task(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.show(str(args["task_id"]))

    def _tool_list_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        rows = self.runtime.list_tasks(
            status=args.get("status"),
            patient_id=args.get("patient_id"),
            limit=int(args.get("limit", 50)),
        )
        return {"tasks": rows, "count": len(rows)}

    def _tool_revise_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args["task_id"])
        if isinstance(args.get("payload"), dict):
            return self.runtime.revise(
                task_id,
                args["payload"],
                generator_ref=args.get("generator_ref"),
                knowledge_refs=args.get("knowledge_refs"),
            )
        if isinstance(args.get("inputs"), dict):
            return self.runtime.revise_inputs(task_id, args["inputs"])
        raise ValueError("map_revise_task requires either payload or inputs")

    def _tool_submit_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args["task_id"])
        facts_rev = str(args.get("facts_revision") or args["snapshot_revision"])
        approval = ClinicianApproval(
            task_id=task_id,
            clinician_id=str(args["clinician_id"]),
            proposal_digest=str(args["proposal_digest"]),
            facts_revision=facts_rev,
        )
        return self.runtime.submit(task_id, approval)

    def _tool_review_task(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.review(
            str(args["task_id"]),
            str(args["clinician_id"]),
            accepted=bool(args["accepted"]),
            feedback=str(args.get("feedback", "")),
            digest=args.get("digest"),
        )

    def _tool_reconcile_task(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.reconcile(str(args["task_id"]))

    def _tool_cancel_task(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.cancel(
            str(args["task_id"]),
            str(args["reason"]),
            clinician_id=args.get("clinician_id"),
        )

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {
                "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True,
            }
        try:
            data = handler(dict(arguments or {}))
            response: dict[str, Any] = {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data,
                "isError": False,
            }
            if isinstance(data, dict) and "task_id" in data:
                response["_meta"] = {
                    "ui": {
                        "resourceUri": REVIEW_UI_URI,
                        "card": build_review_card_model(data),
                    }
                }
            return response
        except Exception as error:
            return {
                "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                "isError": True,
            }

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        msg_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": "Invalid Request"}}
        if method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                        "extensions": {
                            "io.modelcontextprotocol/ui": {
                                "mimeTypes": ["text/html;profile=mcp-app"],
                            }
                        },
                    },
                    "serverInfo": {"name": "med-autopractice", "version": self.version},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                result = self.call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            elif method == "resources/list":
                result = {"resources": self.list_resources()}
            elif method == "resources/read":
                result = self.read_resource(str(params.get("uri") or ""))
            else:
                if msg_id is None:
                    return None
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except KeyError as error:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(error)}}
        except Exception as error:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(error)}}

        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve_stdio(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        in_stream = stdin or sys.stdin
        out_stream = stdout or sys.stdout
        for raw_line in in_stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.handle_rpc(req) if isinstance(req, dict) else {
                    "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"},
                }
            except json.JSONDecodeError as error:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {error}"}}
            if resp is not None:
                out_stream.write(json.dumps(resp, ensure_ascii=False) + "\n")
                out_stream.flush()

    def start_http(self, *, host: str = "127.0.0.1", port: int = 0) -> str:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("MCP HTTP transport must bind strictly to loopback (127.0.0.1 / localhost / ::1)")
        if self._http_server is not None:
            return self.http_url
        parent = self

        class RPCHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                if self.path not in {"/", "/mcp"}:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8"))
                    resp = parent.handle_rpc(req)
                except Exception as error:
                    resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(error)}}
                if resp is None:
                    self.send_response(HTTPStatus.ACCEPTED)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._http_server = ThreadingHTTPServer((host, port), RPCHandler)
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        return self.http_url

    @property
    def http_url(self) -> str:
        if self._http_server is None:
            raise RuntimeError("MCP HTTP server is not started")
        host, port = self._http_server.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def stop_http(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=2.0)
            self._http_thread = None
