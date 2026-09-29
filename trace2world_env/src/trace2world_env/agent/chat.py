"""Minimal OpenAI-compatible chat-completions client used by the agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .images import RELEASE_PIXEL_BUDGET, PixelBudget
from .messages import Message

_MAX_ERROR_BODY_CHARS = 4000


class ChatRequestError(RuntimeError):
    """The model server returned an HTTP error or a body that is not JSON."""


@dataclass(frozen=True)
class ChatReply:
    """Text of the first choice and its finish reason."""

    content: str
    finish_reason: str


class ChatClient:
    """Send chat-completion requests to an OpenAI-compatible server.

    Every request enables Qwen thinking (``enable_thinking`` and
    ``chat_template_kwargs.preserve_thinking``) as the released policy expects, and carries
    ``max_pixels`` / ``min_pixels`` from ``pixel_budget`` so the server resizes screenshots.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.0,
        top_p: float = 1.0,
        pixel_budget: PixelBudget = RELEASE_PIXEL_BUDGET,
        timeout: float = 600.0,
    ) -> None:
        base = base_url.rstrip("/")
        self.url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        self.model = model
        key = api_key.strip()
        if key in {"", "EMPTY"}:
            key = os.environ.get("OPENAI_API_KEY", "").strip()
        self._api_key = "" if key == "EMPTY" else key
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.pixel_budget = pixel_budget
        self._http = httpx.AsyncClient(timeout=timeout)

    def request_body(self, messages: list[Message]) -> dict[str, Any]:
        """Return the JSON body sent for ``messages``."""
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "enable_thinking": True,
            "chat_template_kwargs": {"preserve_thinking": True, "enable_thinking": True},
            "max_pixels": self.pixel_budget.max_pixels,
            "min_pixels": self.pixel_budget.min_pixels,
        }

    async def complete(self, messages: list[Message]) -> ChatReply:
        """Request one completion; reasoning returned separately is folded into ``<think>``."""
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        response = await self._http.post(
            self.url, json=self.request_body(messages), headers=headers
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ChatRequestError(
                "model provider HTTP failure "
                f"status={response.status_code} body={_error_body(response)}"
            ) from exc
        try:
            data = response.json()
        except Exception as exc:
            raise ChatRequestError(
                "model provider JSON response parse failure "
                f"status={response.status_code} body={_error_body(response)}"
            ) from exc
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning and "<think>" not in content:
            content = f"<think>{reasoning}</think>{content}"
        return ChatReply(content=content, finish_reason=choice.get("finish_reason") or "")

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()


def _error_body(response: httpx.Response) -> str:
    try:
        value = response.json()
    except Exception:
        value = response.text
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) > _MAX_ERROR_BODY_CHARS:
        text = text[:_MAX_ERROR_BODY_CHARS] + "…"
    return text or "<empty>"
