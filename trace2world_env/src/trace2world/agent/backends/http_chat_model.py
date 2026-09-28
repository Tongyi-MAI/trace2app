
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from trace2world.agent._types import GenerateResult
from trace2world.agent.errors import AgentAPIError
from trace2world.agent.tool_parser import ToolParser

logger = logging.getLogger(__name__)

_MAX_PROVIDER_ERROR_BODY_CHARS = 4000
_PROCESS_GATE_LOCK = threading.Lock()
_PROCESS_MODEL_GATES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_TELEMETRY_WRITE_LOCK = threading.Lock()


def _process_model_gate(
    model: str, max_concurrent: int
) -> threading.BoundedSemaphore | None:

    limit = int(max_concurrent)
    if limit <= 0:
        return None
    key = (model, limit)
    with _PROCESS_GATE_LOCK:
        gate = _PROCESS_MODEL_GATES.get(key)
        if gate is None:
            gate = threading.BoundedSemaphore(limit)
            _PROCESS_MODEL_GATES[key] = gate
        return gate


async def _acquire_process_gate(gate: threading.BoundedSemaphore) -> None:

    while not gate.acquire(blocking=False):
        await asyncio.sleep(0.05)


def _provider_error_body(response: httpx.Response) -> str:
    try:
        value = response.json()
    except Exception:
        value = response.text
    if isinstance(value, str):
        text = value.strip()
    else:
        import json

        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) > _MAX_PROVIDER_ERROR_BODY_CHARS:
        text = text[:_MAX_PROVIDER_ERROR_BODY_CHARS] + "…"
    return text or "<empty>"



class _PassthroughDecoder:

    def decode(self, value: Any, skip_special_tokens: bool = True) -> str:
        return value if isinstance(value, str) else str(value)


class HttpChatModel:

    def __init__(
        self,
        url: str,
        model: str = "default",
        *,
        tool_parser_name: str = "mobile",
        api_key: str = "EMPTY",
        timeout: float = 600.0,
        preserve_thinking: bool = False,
        enable_thinking: bool | None = None,
        max_pixels: int | None = None,
        min_pixels: int | None = None,
        max_concurrent: int = 0,
        provider_telemetry_path: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:


        base = url.rstrip("/")
        self.url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        self.model = model
        self.tool_parser_name = tool_parser_name
        normalized_api_key = str(api_key or "").strip()
        self.api_key_source = "explicit"
        if normalized_api_key in {"", "EMPTY"}:
            normalized_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            self.api_key_source = "environment" if normalized_api_key else "sentinel"
        self.api_key = normalized_api_key


        self.preserve_thinking = preserve_thinking


        self.enable_thinking = enable_thinking
        self.max_pixels = max_pixels
        self.min_pixels = min_pixels
        self.max_concurrent = int(max_concurrent)
        self._gate = _process_model_gate(self.model, self.max_concurrent)
        self.provider_telemetry_path = (
            Path(provider_telemetry_path).expanduser()
            if provider_telemetry_path
            else None
        )

        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    def reset(self) -> None:
        ...

    async def generate(
        self,
        messages: list[dict[str, Any]],
        sampling_params: dict[str, Any],
        *,
        tools: list[dict] | None = None,
        original_image_width: int | None = None,
        original_image_height: int | None = None,
        timing: Any | None = None,
    ) -> GenerateResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai_multimodal(messages),
        }
        payload.update(self._map_sampling(sampling_params))
        if tools:
            payload["tools"] = tools


        ctk: dict[str, Any] = {}
        if self.preserve_thinking:
            ctk["preserve_thinking"] = True
        if self.enable_thinking is not None:
            ctk["enable_thinking"] = self.enable_thinking
            payload["enable_thinking"] = self.enable_thinking
        if ctk:
            payload["chat_template_kwargs"] = ctk
        if self.max_pixels is not None:
            payload["max_pixels"] = self.max_pixels
        if self.min_pixels is not None:
            payload["min_pixels"] = self.min_pixels

        headers: dict[str, str] = {}
        api_key = self.api_key.strip()
        if api_key and api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {api_key}"
        request_started_at = time.time()
        if self._gate is not None:
            await _acquire_process_gate(self._gate)
        try:
            resp = await self._client.post(self.url, json=payload, headers=headers)
        except Exception as exc:
            self._emit_provider_telemetry(
                request_started_at=request_started_at,
                status_code=None,
                error_kind=f"transport:{type(exc).__name__}",
            )
            raise
        finally:
            if self._gate is not None:
                self._gate.release()
        status_code = getattr(resp, "status_code", None)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._emit_provider_telemetry(
                request_started_at=request_started_at,
                status_code=status_code,
                error_kind="http_status",
            )
            raise AgentAPIError(
                "model provider HTTP failure "
                f"status={status_code} body={_provider_error_body(resp)}"
            ) from exc
        try:
            data = resp.json()
        except Exception as exc:
            self._emit_provider_telemetry(
                request_started_at=request_started_at,
                status_code=status_code,
                error_kind="response_json_parse",
            )
            raise AgentAPIError(
                "model provider JSON response parse failure "
                f"status={status_code} body={_provider_error_body(resp)}"
            ) from exc
        self._emit_provider_telemetry(
            request_started_at=request_started_at,
            status_code=status_code,
            error_kind=None,
        )

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""


        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning and "<think>" not in content:
            content = f"<think>{reasoning}</think>{content}"
        finish_reason = choice.get("finish_reason") or ""

        parser = ToolParser.get_tool_parser(
            self.tool_parser_name,
            _PassthroughDecoder(),
            original_image_width=original_image_width,
            original_image_height=original_image_height,
        )


        info, function_calls = await parser.extract_tool_calls(content)

        return GenerateResult(
            content=content,
            function_calls=function_calls,
            info=info,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _map_sampling(sp: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if sp.get("max_new_tokens") is not None:
            out["max_tokens"] = sp["max_new_tokens"]
        elif sp.get("max_tokens") is not None:
            out["max_tokens"] = sp["max_tokens"]
        for key in ("temperature", "top_p", "stop"):
            if sp.get(key) is not None:
                out[key] = sp[key]


        if sp.get("no_stop_trim") is not None:
            out["no_stop_trim"] = sp["no_stop_trim"]
        return out

    @staticmethod
    def _to_openai_multimodal(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "image_url"
                        and isinstance(part.get("image_url"), str)
                    ):
                        part = {**part, "image_url": {"url": part["image_url"]}}
                    parts.append(part)
                m = {**m, "content": parts}
            out.append(m)
        return out

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _emit_provider_telemetry(
        self,
        *,
        request_started_at: float,
        status_code: int | None,
        error_kind: str | None,
    ) -> None:

        path = self.provider_telemetry_path
        if path is None:
            return
        finished_at = time.time()
        row = {
            "schema": "trace2world.provider-request-telemetry.v1",
            "started_at_unix": request_started_at,
            "finished_at_unix": finished_at,
            "elapsed_seconds": finished_at - request_started_at,
            "provider_host": urlparse(self.url).hostname,
            "model": self.model,
            "status_code": status_code,
            "success": error_kind is None,
            "error_kind": error_kind,
        }
        payload = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
        with _TELEMETRY_WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o666)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
