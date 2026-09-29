"""Async HTTP client for the environment host (used by rollout and smoke)."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx


class HostError(RuntimeError):
    """The host answered with an error payload ``{"error": {"code", "message"}}``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Episode:
    """A started episode."""

    episode_id: str
    task_id: str
    environment: str
    instruction: str


@dataclass(frozen=True)
class Observation:
    """A screenshot (base64 PNG) and where it was taken."""

    screenshot: str
    url: str
    page: str
    viewport: tuple[int, int]
    device_scale_factor: float


@dataclass(frozen=True)
class ActionOutcome:
    """The host's answer to one action."""

    accepted: bool
    feedback: str | None
    done: bool
    result: dict[str, Any]
    url: str
    page: str


class EnvironmentClient:
    """Client for one host; use as ``async with EnvironmentClient(url) as client``."""

    def __init__(self, base_url: str, *, timeout: float = 600.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def __aenter__(self) -> EnvironmentClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, path, **kwargs)
        if response.is_error:
            try:
                error = response.json().get("error") or {}
            except (ValueError, AttributeError):
                error = {}
            raise HostError(
                response.status_code,
                str(error.get("code") or "http_error"),
                str(error.get("message") or response.text),
            )
        return response.json()

    async def health(self) -> dict[str, Any]:
        """``GET /health``."""
        return await self._call("GET", "/health")

    async def tasks(self) -> list[dict[str, Any]]:
        """All hosted tasks (``GET /tasks``)."""
        return (await self._call("GET", "/tasks"))["tasks"]

    async def start_episode(self, task_id: str) -> Episode:
        """Start an episode of ``task_id`` in a fresh browser context."""
        payload = await self._call("POST", "/episodes", json={"task_id": task_id})
        return Episode(
            episode_id=payload["episode_id"],
            task_id=payload["task_id"],
            environment=payload["environment"],
            instruction=payload["instruction"],
        )

    async def observe(self, episode_id: str) -> Observation:
        """Screenshot the episode's page."""
        payload = await self._call("GET", f"/episodes/{episode_id}/observation")
        viewport = payload.get("viewport") or {}
        return Observation(
            screenshot=payload["screenshot"],
            url=payload["url"],
            page=payload["page"],
            viewport=(viewport.get("width"), viewport.get("height")),
            device_scale_factor=float(payload["device_scale_factor"]),
        )

    async def act(self, episode_id: str, action: dict[str, Any]) -> ActionOutcome:
        """Execute one action."""
        payload = await self._call(
            "POST", f"/episodes/{episode_id}/actions", json={"action": action}
        )
        return ActionOutcome(
            accepted=payload["accepted"],
            feedback=payload["feedback"],
            done=payload["done"],
            result=payload["result"],
            url=payload["url"],
            page=payload["page"],
        )

    async def evaluate(self, episode_id: str, *, final: bool = True) -> dict[str, Any]:
        """Evaluate the episode: ``{"reward", "success", "reason", "details"}``."""
        return await self._call(
            "POST", f"/episodes/{episode_id}/evaluate", json={"final": final}
        )

    async def close_episode(self, episode_id: str) -> None:
        """Close the episode and release its browser."""
        await self._call("DELETE", f"/episodes/{episode_id}")

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()
