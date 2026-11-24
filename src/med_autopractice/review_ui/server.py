"""Loopback token-authenticated HTTP clinical review server sharing TaskRuntime."""

from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from med_autopractice.domain.task import ClinicianApproval
from med_autopractice.review_ui.renderer import build_review_card_model, render_review_app_html
from med_autopractice.runtime import TaskRuntime

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LocalReviewServer:
    """Fallback local browser review server bound strictly to 127.0.0.1 with X-MAP-Token."""

    def __init__(
        self,
        runtime: TaskRuntime,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
    ):
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("LocalReviewServer may only bind to a loopback host (127.0.0.1 / localhost / ::1)")
        self.runtime = runtime
        self.host = host
        self.requested_port = port
        self.token = token or secrets.token_hex(24)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _authorized(self, header_token: str | None) -> bool:
        if not header_token:
            return False
        return hmac.compare_digest(header_token, self.token)

    def _make_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _send_json(self, status: int, body: Any) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("request body must be a JSON object")
                return data

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path == "/":
                    qs = parse_qs(parsed.query)
                    task_id = (qs.get("task_id") or [None])[0]
                    provided_token = (qs.get("token") or [None])[0] or self.headers.get("X-MAP-Token")
                    authorized = parent._authorized(provided_token)
                    initial = parent.runtime.show(task_id) if (task_id and authorized) else None
                    html = render_review_app_html(
                        initial_state=initial,
                        api_base="",
                        auth_token=parent.token if authorized else "",
                    ).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return

                if not parent._authorized(self.headers.get("X-MAP-Token")):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "valid X-MAP-Token required"})
                    return

                if parsed.path == "/api/tasks":
                    tasks = parent.runtime.list_tasks()
                    self._send_json(HTTPStatus.OK, {"tasks": tasks})
                    return

                if parsed.path.startswith("/api/tasks/"):
                    task_id = unquote(parsed.path[len("/api/tasks/"):])
                    try:
                        view = parent.runtime.show(task_id)
                        self._send_json(HTTPStatus.OK, {"task": view, "card": build_review_card_model(view)})
                    except KeyError:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"task not found: {task_id}"})
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})

            def do_POST(self):
                if not parent._authorized(self.headers.get("X-MAP-Token")):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "valid X-MAP-Token required"})
                    return
                parsed = urlsplit(self.path)
                parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
                if len(parts) != 4 or parts[0] != "api" or parts[1] != "tasks":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
                    return
                task_id, action = parts[2], parts[3]
                try:
                    body = self._read_json()
                    if action == "submit":
                        facts_rev = str(body.get("facts_revision") or body["snapshot_revision"])
                        approval = ClinicianApproval(
                            task_id=task_id,
                            clinician_id=str(body["clinician_id"]),
                            proposal_digest=str(body["proposal_digest"]),
                            facts_revision=facts_rev,
                        )
                        view = parent.runtime.submit(task_id, approval)
                    elif action == "review":
                        view = parent.runtime.review(
                            task_id,
                            str(body["clinician_id"]),
                            accepted=bool(body["accepted"]),
                            feedback=str(body.get("feedback", "")),
                            digest=body.get("digest"),
                        )
                    elif action == "revise":
                        view = parent.runtime.revise(
                            task_id,
                            body["payload"],
                            generator_ref=body.get("generator_ref"),
                            knowledge_refs=body.get("knowledge_refs"),
                        )
                    elif action == "reconcile":
                        view = parent.runtime.reconcile(task_id)
                    elif action == "cancel":
                        view = parent.runtime.cancel(
                            task_id,
                            str(body.get("reason", "clinician_cancelled")),
                            clinician_id=body.get("clinician_id"),
                        )
                    else:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown action: {action}"})
                        return
                    self._send_json(HTTPStatus.OK, {"task": view, "card": build_review_card_model(view)})
                except (KeyError, ValueError, TypeError, PermissionError) as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

        return Handler

    def start(self) -> str:
        if self._server is not None:
            return self.url
        self._server = ThreadingHTTPServer((self.host, self.requested_port), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("LocalReviewServer is not started")
        return int(self._server.server_port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __enter__(self) -> "LocalReviewServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
