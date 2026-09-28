from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from trace2world_pipeline.state_kernel.contracts import EnvironmentStateError
from trace2world_pipeline.state_kernel.service import EnvironmentStateService


def make_environment_state_handler(
    *,
    result_root: str | Path,
    service: EnvironmentStateService,
    service_metadata: dict[str, Any] | None = None,
):
    root = Path(result_root).resolve()
    metadata = dict(service_metadata or {})

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json_body(self) -> dict[str, Any]:
            size = int(self.headers.get("Content-Length", "0") or 0)
            if not size:
                return {}
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            return payload if isinstance(payload, dict) else {}

        def _send_json(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, EnvironmentStateError):
                self._send_json(exc.http_status, {"error": {"code": exc.code, "message": str(exc), "details": exc.details}})
            elif isinstance(exc, KeyError):
                self._send_json(404, {"error": {"code": "not_found", "message": str(exc)}})
            else:
                self._send_json(500, {"error": {"code": "internal_error", "message": str(exc)}})

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
            try:
                if parsed.path == "/api/v1/health":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "runtime_profile": "state_kernel_sqlite",
                            "schema_hash": metadata.get("schema_hash"),
                            "world_hash": metadata.get("world_hash"),
                            "bundle_hash": metadata.get("bundle_hash"),
                        },
                    )
                    return
                if len(parts) == 7 and parts[:3] == ["api", "v1", "sessions"] and parts[4] == "collections":
                    self._send_json(200, service.read_collection(parts[3], parts[5] if parts[6] == "" else parts[5]))
                    return
                if len(parts) == 6 and parts[:3] == ["api", "v1", "sessions"] and parts[4] == "collections":
                    self._send_json(200, service.read_collection(parts[3], parts[5]))
                    return
                if len(parts) == 6 and parts[:3] == ["api", "v1", "sessions"] and parts[4] == "storage":
                    self._send_json(200, service.read_storage(parts[3], parts[5]))
                    return
                if len(parts) == 5 and parts[:3] == ["internal", "v1", "sessions"] and parts[4] == "events":
                    query = parse_qs(parsed.query)
                    self._send_json(200, service.dispatch("events", sid=parts[3], payload={"from_revision": query.get("from_revision", [0])[0]}))
                    return
                super().do_GET()
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
            try:
                payload = self._json_body()
                if parts == ["api", "v1", "bootstrap"]:
                    self._send_json(200, service.dispatch("bootstrap", payload=payload))
                    return
                if len(parts) == 6 and parts[:3] == ["api", "v1", "sessions"] and parts[4] == "transactions":
                    self._send_json(200, service.execute(parts[3], parts[5], payload))
                    return
                if parts == ["internal", "v1", "sessions"]:
                    self._send_json(201, service.dispatch("create", payload=payload))
                    return
                if len(parts) == 5 and parts[:3] == ["internal", "v1", "sessions"]:
                    operation = parts[4].lstrip(":").replace("-", "_")
                    self._send_json(200, service.dispatch(operation, sid=parts[3], payload=payload))
                    return
                self._send_json(404, {"error": {"code": "not_found", "message": parsed.path}})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def run_server(
    *,
    result_root: str | Path,
    runtime_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    state_db_path: str | Path | None = None,
) -> None:
    runtime_root = Path(runtime_root)
    service = EnvironmentStateService(runtime_root, state_db_path=state_db_path)
    service_metadata = {}
    manifest_path = runtime_root / "service_manifest.json"
    if manifest_path.is_file():
        service_metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    handler = make_environment_state_handler(
        result_root=result_root,
        service=service,
        service_metadata=service_metadata,
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("T2W_STATE_PORT", "8765")))
    parser.add_argument("--state-db-path", default=os.environ.get("T2W_STATE_DB_PATH"))
    args = parser.parse_args()
    run_server(
        result_root=args.result_root,
        runtime_root=args.runtime_root,
        host=args.host,
        port=args.port,
        state_db_path=args.state_db_path,
    )


if __name__ == "__main__":
    main()
