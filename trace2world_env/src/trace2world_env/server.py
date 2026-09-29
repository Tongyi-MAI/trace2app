"""The HTTP host: agent-facing episode API, static app pages, and the state-kernel proxy.

Agent-facing routes (errors are ``{"error": {"code", "message"}}``)::

    GET    /health
    GET    /tasks
    POST   /episodes                    {"task_id"}
    GET    /episodes/{id}/observation
    POST   /episodes/{id}/actions       {"action": {...}}
    POST   /episodes/{id}/evaluate      {"final": true}
    DELETE /episodes/{id}
    GET    /episodes/{id}/page | /runtime | /state?kind=audit

Page-facing routes (part of the frozen environment payload): ``/app/<environment>/...`` serves
the static app and ``/api/v1/...`` proxies to the episode's state kernel, resolved by ``sid``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from trace2world_env.actions import feedback_text, is_terminal
from trace2world_env.browser import BrowserLauncher
from trace2world_env.catalog import Catalog
from trace2world_env.episodes import (
    EpisodeManager,
    StateKernelNotConfiguredError,
    UnknownEpisodeError,
    UnknownTaskError,
)

logger = logging.getLogger(__name__)


class StartEpisodeBody(BaseModel):
    """``POST /episodes`` body."""

    task_id: str


class ActionBody(BaseModel):
    """``POST /episodes/{id}/actions`` body."""

    action: dict[str, Any]


class EvaluateBody(BaseModel):
    """``POST /episodes/{id}/evaluate`` body; ``final=false`` previews."""

    final: bool = True


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _identity_fields(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": identity["current_url"],
        "page": identity["page_filename"],
        "viewport": identity["viewport"],
        "device_scale_factor": identity["device_scale_factor"],
    }


def evaluation_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap an evaluator payload as ``{reward, success, reason, details}``."""
    reward = float(payload.get("raw_reward", payload.get("score", 0.0)) or 0.0)
    success = payload.get("terminal_success")
    return {
        "reward": reward,
        "success": success if isinstance(success, bool) else None,
        "reason": str(payload.get("reason") or ""),
        "details": payload,
    }


def action_response(
    action: dict[str, Any], result: dict[str, Any], identity: dict[str, Any]
) -> dict[str, Any]:
    """Wrap a step result as ``{accepted, feedback, done, result, url, page, ...}``."""
    return {
        "accepted": not result.get("actionability_rejected"),
        "feedback": feedback_text(result),
        "done": is_terminal(action),
        "result": result,
        **_identity_fields(identity),
    }


def _sid_of(state_path: str, query_sid: str | None, body: bytes) -> str | None:
    """Resolve the episode session of a page's state-kernel request: ``?sid=``, then a
    ``sessions/<sid>`` path segment, then a JSON body ``sid``."""
    sid = query_sid
    if not sid:
        match = re.search(r"(?:^|/)sessions/([^/]+)", state_path)
        if match:
            sid = match.group(1)
    if not sid and body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and payload.get("sid"):
                sid = str(payload["sid"])
        except (TypeError, ValueError):
            pass
    return sid


def create_app(
    catalog: Catalog,
    *,
    port: int,
    state_root: str | Path | None = None,
    browser_executable: str | None = None,
    manager: EpisodeManager | None = None,
) -> FastAPI:
    """Build the host application for ``catalog`` served on ``port``.

    ``manager`` replaces the default :class:`EpisodeManager` (tests use a stub).
    """
    episodes = manager or EpisodeManager(
        catalog,
        BrowserLauncher(browser_executable),
        app_root_url=f"http://localhost:{port}/app",
        state_root=state_root,
    )
    instance_id = uuid.uuid4().hex

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await episodes.start()
        logger.info(
            "Host ready: %d environments, %d tasks",
            len(catalog.environments),
            len(catalog.tasks),
        )
        try:
            yield
        finally:
            await episodes.stop()
            logger.info("Host stopped")

    app = FastAPI(title="Trace2World environment host", lifespan=lifespan)
    app.state.episodes = episodes

    @app.exception_handler(UnknownTaskError)
    async def unknown_task(_request: Request, exc: UnknownTaskError) -> JSONResponse:
        return _error(404, "unknown_task", f"unknown task: {exc.args[0]}")

    @app.exception_handler(UnknownEpisodeError)
    async def unknown_episode(_request: Request, exc: UnknownEpisodeError) -> JSONResponse:
        return _error(404, "unknown_episode", f"unknown episode: {exc.args[0]}")

    @app.exception_handler(StateKernelNotConfiguredError)
    async def no_state_kernel(
        _request: Request, exc: StateKernelNotConfiguredError
    ) -> JSONResponse:
        return _error(
            404,
            "state_kernel_not_configured",
            f"environment {exc.args[0]} has no state kernel",
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", json.dumps(exc.errors(), default=str))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/app/"):
            return await http_exception_handler(request, exc)
        code = "not_found" if exc.status_code == 404 else "http_error"
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Request failed")
        return _error(500, "internal_error", f"{type(exc).__name__}: {exc}")

    for environment_id, environment in catalog.environments.items():
        app.mount(
            f"/app/{environment_id}",
            StaticFiles(directory=environment.app_dir, html=True),
            name=f"app-{environment_id}",
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "instance_id": instance_id,
            "environments": len(catalog.environments),
            "tasks": len(catalog.tasks),
            "active_episodes": len(episodes.episodes),
        }

    @app.get("/tasks")
    async def list_tasks() -> dict[str, Any]:
        return {"tasks": [task.summary() for task in catalog.tasks.values()]}

    @app.post("/episodes")
    async def start_episode(body: StartEpisodeBody) -> dict[str, Any]:
        episode = await episodes.start_episode(body.task_id)
        return {
            "episode_id": episode.episode_id,
            "task_id": episode.task.task_id,
            "environment": episode.environment.environment_id,
            "instruction": episode.task.instruction,
        }

    @app.get("/episodes/{episode_id}/observation")
    async def observe(episode_id: str) -> dict[str, Any]:
        png, identity = await episodes.observe(episode_id)
        return {"screenshot": base64.b64encode(png).decode("ascii"), **_identity_fields(identity)}

    @app.post("/episodes/{episode_id}/actions")
    async def act(episode_id: str, body: ActionBody) -> dict[str, Any]:
        result, identity = await episodes.act(episode_id, body.action)
        return action_response(body.action, result, identity)

    @app.post("/episodes/{episode_id}/evaluate")
    async def evaluate(episode_id: str, body: EvaluateBody | None = None) -> dict[str, Any]:
        final = True if body is None else body.final
        return evaluation_response(await episodes.evaluate(episode_id, final=final))

    @app.delete("/episodes/{episode_id}")
    async def close_episode(episode_id: str) -> dict[str, Any]:
        await episodes.close(episode_id)
        return {"closed": True}

    @app.get("/episodes/{episode_id}/page")
    async def page_state(episode_id: str) -> dict[str, Any]:
        episode = episodes.get(episode_id)
        return {"episode_id": episode_id, **_identity_fields(episode.identity())}

    @app.get("/episodes/{episode_id}/runtime")
    async def runtime_state(episode_id: str) -> dict[str, Any]:
        return {"episode_id": episode_id, "snapshots": await episodes.runtime_audit(episode_id)}

    @app.get("/episodes/{episode_id}/state")
    async def state_snapshot(episode_id: str, kind: str = "audit") -> dict[str, Any]:
        episode = episodes.get(episode_id)
        snapshot = await episodes.state_snapshot(episode_id, kind=kind)
        return {
            "episode_id": episode_id,
            "task_id": episode.task.task_id,
            "sid": episode.sid,
            "snapshot": snapshot,
        }

    @app.api_route("/api/v1/{state_path:path}", methods=["GET", "POST"])
    async def state_kernel_proxy(state_path: str, request: Request) -> Response:
        if not episodes.state_kernels:
            return _error(
                404, "state_kernel_not_configured", "no hosted environment has a state kernel"
            )
        body = await request.body()
        sid = _sid_of(state_path, request.query_params.get("sid"), body)
        target = episodes.kernel_for_sid(sid)
        if target is None:
            return _error(
                404,
                "state_kernel_episode_not_found",
                f"no active state-kernel episode for sid: {sid or ''}",
            )
        kernel, lock = target
        query = f"?{request.url.query}" if request.url.query else ""
        async with lock:
            response = await kernel.request(
                request.method,
                f"/api/v1/{state_path}{query}",
                content=body,
                headers=dict(request.headers),
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

    return app
