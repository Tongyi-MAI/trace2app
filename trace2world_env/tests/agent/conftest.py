"""Shared fixtures for the agent tests: import path and a scripted chat-completions server."""

from __future__ import annotations

import json
import socket
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PORT_RANGE = range(17200, 17300)


@dataclass
class RecordedRequest:
    """One request received by the scripted server."""

    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


@dataclass
class ScriptedChatServer:
    """OpenAI-compatible server that answers from a fixed list of replies, in order.

    Each reply is a dict with ``content`` and optional ``reasoning_content``, ``reasoning``,
    ``finish_reason``. Every request body is recorded verbatim.
    """

    port: int
    script: list[dict[str, Any]] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)
    _cursor: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def load(self, script: list[dict[str, Any]]) -> None:
        """Replace the script and forget previous requests."""
        with self._lock:
            self.script = list(script)
            self.requests = []
            self._cursor = 0

    def next_reply(self, request: RecordedRequest) -> dict[str, Any]:
        with self._lock:
            self.requests.append(request)
            if self._cursor >= len(self.script):
                raise AssertionError(f"script exhausted after {self._cursor} replies")
            reply = self.script[self._cursor]
            self._cursor += 1
        message: dict[str, Any] = {"role": "assistant", "content": reply.get("content")}
        for key in ("reasoning_content", "reasoning"):
            if key in reply:
                message[key] = reply[key]
        return {
            "id": f"chatcmpl-{self._cursor}",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": message, "finish_reason": reply.get("finish_reason", "stop")}
            ],
        }


def _free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 17200-17299")


@pytest.fixture(scope="session")
def chat_server() -> Iterator[ScriptedChatServer]:
    """A running scripted chat server on a port in 17200-17299."""
    server_state = ScriptedChatServer(port=_free_port())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            request = RecordedRequest(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            try:
                payload = json.dumps(server_state.next_reply(request)).encode()
                status = 200
            except AssertionError as exc:
                payload = json.dumps({"error": str(exc)}).encode()
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", server_state.port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server_state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
