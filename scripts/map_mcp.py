"""Start the MAP MCP Server (stdio or 127.0.0.1 HTTP) and optional OPL MiniApp bridge."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from med_autopractice.agent.task_engine import JsonlAuditSink  # noqa: E402
from med_autopractice.agent.task_store import SyntheticSqliteTaskStore  # noqa: E402
from med_autopractice.deployment import build_site_runtime  # noqa: E402
from med_autopractice.mcp_server import MAPMCPServer  # noqa: E402
from med_autopractice.runtime import build_synthetic_runtime  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("synthetic", "site"), default="synthetic")
    parser.add_argument("--site", type=Path, default=None, help="Private site directory when --mode site")
    parser.add_argument("--store", type=Path, default=None, help="SQLite TaskStore path for synthetic mode")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.mode == "site":
            if not args.site:
                parser.error("--site is required when --mode site")
            if args.transport != "stdio":
                parser.error("private site mode requires the local stdio transport")
            site_dir = args.site.expanduser().resolve()
            runtime = build_site_runtime(site_dir)
        else:
            site_dir = None
            if args.store:
                store_path = args.store.expanduser().resolve()
                if store_path.is_relative_to(ROOT):
                    parser.error("--store must live outside this repository")
            else:
                temp_dir = tempfile.TemporaryDirectory(prefix="map-mcp-")
                store_path = Path(temp_dir.name) / "tasks.sqlite"
            audit_path = store_path.with_suffix(".audit.jsonl")
            store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            runtime, _ui = build_synthetic_runtime(
                SyntheticSqliteTaskStore(store_path), JsonlAuditSink(audit_path),
            )

        server = MAPMCPServer(runtime, site_dir=site_dir, repo_root=ROOT)
        if args.transport == "stdio":
            server.serve_stdio()
            return 0
        url = server.start_http(host=args.host, port=args.port)
        print(f"MAP MCP HTTP listening on {url}", flush=True)
        try:
            while True:
                import time
                time.sleep(1.0)
        except KeyboardInterrupt:
            server.stop_http()
        return 0
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
