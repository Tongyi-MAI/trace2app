
import asyncio
import base64
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

from trace2world.env.action_executor import _SCROLL_DELTA, execute_action
from trace2world.env.context_manager import (
    _DEVICE_SCALE_FACTOR,
    _VIEWPORT_HEIGHT,
    _VIEWPORT_WIDTH,
    ContextManager,
    DisplayIdRequiredError,
    UnknownDisplayError,
)
from trace2world.env.evaluator import run_evaluator_result
from trace2world.env.layout import WebEnvPortfolioLayout, load_and_normalize_tasks
from trace2world.env.state_kernel import StateKernelManager

logger = logging.getLogger(__name__)

DEVICE_ID = "web-env-0"


_RUNTIME_AUDIT_EVENT_DEDUPE_JS = r"""
          const stableCanonicalJson = (value) => {
            if (value === null) return 'null';
            if (Array.isArray(value)) {
              return '[' + value.map(stableCanonicalJson).join(',') + ']';
            }
            const kind = typeof value;
            if (kind === 'object') {
              const fields = Object.keys(value).sort().map((key) => (
                JSON.stringify(key) + ':' + stableCanonicalJson(value[key])
              ));
              return '{' + fields.join(',') + '}';
            }
            if (kind === 'string' || kind === 'boolean') {
              return JSON.stringify(value);
            }
            if (kind === 'number' && Number.isFinite(value)) {
              return JSON.stringify(value);
            }
            throw new TypeError('checkpoint_event_is_not_json_canonicalizable');
          };
          const hasLegacyCheckpointIdentitySignal = (value) => {
            if (Array.isArray(value)) {
              return value.some(hasLegacyCheckpointIdentitySignal);
            }
            if (!value || typeof value !== 'object') return false;
            return Object.entries(value).some(([key, fieldValue]) => {
              const meaningful = fieldValue != null && !(
                typeof fieldValue === 'string' && fieldValue.trim() === ''
              ) && !(
                typeof fieldValue === 'object' &&
                Object.keys(fieldValue).length === 0
              );
              const identityField = [
                'trace_id', 'checkpoint_id', 'action_id', 'sequence', 'timestamp',
              ].includes(key) || key === 'page' || key.startsWith('page_') ||
                /(^|_)(page|state)_(hash|sha256)$/.test(key);
              return (identityField && meaningful) ||
                hasLegacyCheckpointIdentitySignal(fieldValue);
            });
          };
          const checkpointEventCopyIdentity = (row) => {
            if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
            const eventId = row.event_id == null ? '' : String(row.event_id);
            if (eventId) return 'event_id:' + JSON.stringify(eventId);
            if (!hasLegacyCheckpointIdentitySignal(row)) return null;
            try {
              return 'canonical:' + stableCanonicalJson(row);
            } catch (_) {
              return null;
            }
          };
          const deduplicateCheckpointEvents = (rows) => {
            const seen = new Set();
            return rows.filter((row) => {
              if (!row || typeof row !== 'object' || Array.isArray(row)) return false;
              const identity = checkpointEventCopyIdentity(row);
              if (identity == null) return true;
              if (seen.has(identity)) return false;
              seen.add(identity);
              return true;
            });
          };
"""


def _observed_trace_step_index(action_observation: object) -> int | None:
    if not isinstance(action_observation, dict):
        return None
    action_id = action_observation.get("action_id")
    if not isinstance(action_id, str):
        return None
    match = re.search(r":step(\d+)$", action_id)
    return int(match.group(1)) if match else None


def _next_execution_context_step(
    last_context_step: int | None,
    execution_action_index: int,
) -> int:
    if isinstance(execution_action_index, bool) or execution_action_index < 0:
        raise ValueError("execution_action_index must be a non-negative integer")
    if isinstance(last_context_step, bool) or (
        last_context_step is not None and last_context_step < 0
    ):
        raise ValueError("last_context_step must be non-negative or None")
    if last_context_step is None:
        return execution_action_index
    return max(execution_action_index, last_context_step + 1)


def _should_reproject_task_context_after_action(
    action: dict[str, Any],
    before_url: str,
    after_url: str,
) -> bool:
    action_type = str(action.get("action_type") or "")
    if action_type in {"navigate_back", "system_back"}:
        return True
    if action_type == "system_button" and action.get("button") == "back":
        return True
    return bool(before_url and after_url and before_url != after_url)


def _resolve_browser_profile(
    resource_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    default = {
        "viewport": {"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
        "device_scale_factor": float(_DEVICE_SCALE_FACTOR),
        "viewport_source": "webenv_default",
        "device_scale_factor_source": "webenv_default",
        "scroll_delta": _SCROLL_DELTA,
        "scroll_delta_source": "webenv_default",
    }
    if resource_manifest is None:
        return default

    profile = dict(default)
    if "browser_viewport" in resource_manifest:
        viewport = resource_manifest["browser_viewport"]
        if not isinstance(viewport, dict):
            raise ValueError("resource_manifest.browser_viewport must be an object")
        width = viewport.get("width")
        height = viewport.get("height")
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ValueError(
                "resource_manifest.browser_viewport must contain positive integer width and height"
            )
        profile["viewport"] = {"width": width, "height": height}
        profile["viewport_source"] = "resource_manifest.browser_viewport"

    if "browser_device_scale_factor" in resource_manifest:
        scale = resource_manifest["browser_device_scale_factor"]
        if isinstance(scale, bool):
            raise ValueError("resource_manifest.browser_device_scale_factor must be positive")
        try:
            scale_value = float(scale)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "resource_manifest.browser_device_scale_factor must be positive"
            ) from exc
        if scale_value <= 0:
            raise ValueError("resource_manifest.browser_device_scale_factor must be positive")
        profile["device_scale_factor"] = scale_value
        profile["device_scale_factor_source"] = (
            "resource_manifest.browser_device_scale_factor"
        )

    if "browser_scroll_delta" in resource_manifest:
        scroll_delta = resource_manifest["browser_scroll_delta"]
        if (
            isinstance(scroll_delta, bool)
            or not isinstance(scroll_delta, int)
            or scroll_delta <= 0
        ):
            raise ValueError(
                "resource_manifest.browser_scroll_delta must be a positive integer"
            )
        profile["scroll_delta"] = scroll_delta
        profile["scroll_delta_source"] = "resource_manifest.browser_scroll_delta"

    return profile


def _page_identity_payload(
    page: Any,
    *,
    device_scale_factor: float,
) -> dict[str, Any]:
    current_url = str(getattr(page, "url", "") or "")
    page_filename = Path(urlparse(current_url).path).name if current_url else ""
    viewport = getattr(page, "viewport_size", None)
    if not isinstance(viewport, dict):
        viewport = {}
    return {
        "current_url": current_url,
        "page_filename": page_filename,
        "viewport": {
            "width": viewport.get("width"),
            "height": viewport.get("height"),
        },
        "device_scale_factor": device_scale_factor,
    }


async def _read_runtime_current_state(page: Any) -> dict[str, Any]:


    snapshot_script = """() => {
          try {
            const runtime = window.GhostwebRuntimeState;
            if (!runtime || typeof runtime.snapshot !== 'function') {
              return {available: false, reason: 'runtime_snapshot_unavailable'};
            }
            const snapshot = runtime.snapshot();
            if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
              return {available: false, reason: 'runtime_snapshot_invalid'};
            }
            const currentState = snapshot.current_state;
            if (!currentState || typeof currentState !== 'object' || Array.isArray(currentState)) {
              return {available: false, reason: 'runtime_current_state_unavailable'};
            }
            return {available: true, current_state: currentState};
          } catch (error) {
            return {available: false, reason: 'runtime_snapshot_read_failed'};
          }
        }"""
    for attempt in range(5):


        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:

            pass
        try:
            return await page.evaluate(snapshot_script)
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc) or attempt == 4:
                return {"available": False, "reason": "runtime_snapshot_read_failed"}
            await asyncio.sleep(0.05)

    raise AssertionError("runtime snapshot retry loop exhausted")


async def _read_runtime_audit_state(page: Any) -> dict[str, Any]:

    audit_script = """() => {
          const clone = (value) => {
            if (value == null) return null;
            try { return JSON.parse(JSON.stringify(value)); } catch (_) { return null; }
          };
          const snapshot = (runtime) => {
            try {
              return runtime && typeof runtime.snapshot === 'function'
                ? clone(runtime.snapshot()) : null;
            } catch (_) { return null; }
          };
          const readJson = (storage, key) => {
            try {
              const value = storage.getItem(key);
              return value == null ? null : clone(JSON.parse(value));
            } catch (_) { return null; }
          };
          const readJsonStorage = (key, fallback) => {
            try {
              const sessionValue = readJson(window.sessionStorage, key);
              if (sessionValue != null) return sessionValue;
            } catch (_) {}
            try {
              const localValue = readJson(window.localStorage, key);
              if (localValue != null) return localValue;
            } catch (_) {}
            return fallback;
          };
          const executionContext = clone(window.__ghostwebExecutionContext || null) ||
            readJsonStorage('ghostweb.execution_context', null);
          const taskId = String(
            executionContext && (executionContext.task_id || executionContext.taskId) || ''
          ).trim();
          const sid = String(executionContext && executionContext.sid || '').trim();
          const checkpointEventKey = '__ghostweb_checkpoint_events_v1__';
          const checkpointCarrier = taskId && sid
            ? checkpointEventKey + '.carrier.' + encodeURIComponent(taskId + ':' + sid) : null;
""" + _RUNTIME_AUDIT_EVENT_DEDUPE_JS + """
          const checkpointAudit = (() => {
            const rows = [];
            const sources = [];
            const append = (source, value, key) => {
              if (!Array.isArray(value)) return;
              rows.push(...value);
              sources.push({ source, key, count: value.length });
            };
            try {
              const runtime = window.GhostwebCheckpointRuntime;
              if (runtime && typeof runtime.eventLog === 'function') {
                append('runtime_event_log', clone(runtime.eventLog()), null);
              }
            } catch (_) {}
            append(
              'session_storage',
              readJson(window.sessionStorage, checkpointEventKey),
              checkpointEventKey
            );
            append(
              'local_storage',
              readJson(window.localStorage, checkpointEventKey),
              checkpointEventKey
            );
            if (checkpointCarrier) {
              append(
                'local_storage_carrier',
                readJson(window.localStorage, checkpointCarrier),
                checkpointCarrier
              );
            }
            const deduplicated = deduplicateCheckpointEvents(rows);
            return { events: deduplicated, sources };
          })();
          const root = document.documentElement;
          const body = document.body;
          return {
            available: Boolean(root && body),
            reason: root && body ? null : 'document_dom_not_ready',
            execution_context: executionContext,
            runtime_state: snapshot(window.GhostwebRuntimeState),
            checkpoint_events: checkpointAudit.events,
            checkpoint_event_sources: checkpointAudit.sources,
            app_owned_state_diagnostics: {
              bridge_revision: String(
                window.GhostwebAppOwnedStateBridge &&
                window.GhostwebAppOwnedStateBridge.revision || ''
              ),
              task_world_instance_registered: Boolean(
                taskId && window.GhostwebEnhancedTaskWorldInstances &&
                window.GhostwebEnhancedTaskWorldInstances.instances &&
                window.GhostwebEnhancedTaskWorldInstances.instances[taskId]
              ),
              checkpoint_event_carrier: checkpointCarrier,
              checkpoint_event_carrier_present: Boolean(
                checkpointCarrier && readJson(window.localStorage, checkpointCarrier)
              ),
              persist_failure: clone(window.__ghostwebAppOwnedStatePersistFailure || null),
              carrier_deferred: clone(window.__ghostwebAppOwnedStateCarrierDeferred || null),
              contract_deferred: clone(window.__ghostwebAppOwnedStateContractDeferred || null),
            },
            runtime_events: readJsonStorage('ghostweb_runtime_events_v1', []),
            enhanced_world: snapshot(window.GhostwebEnhancedTaskWorld),
            enhanced_behavior: snapshot(window.GhostwebEnhancedTaskBehavior),
            enhanced_world_enabled: root
              ? root.getAttribute('data-ghostweb-enhanced-world-enabled') : null,
            enhanced_filter_signature: body
              ? body.getAttribute('data-enhanced-filter-signature') : null,
            favorite_committed: body ? (body.dataset.favoriteCommitted || '') : '',
            favorite_item: body ? (body.dataset.favoriteItem || '') : ''
          };
        }"""
    for attempt in range(5):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        try:
            return await page.evaluate(audit_script)
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc) or attempt == 4:
                return {
                    "available": False,
                    "reason": "runtime_audit_read_failed",
                }
            await asyncio.sleep(0.05)

    raise AssertionError("runtime audit retry loop exhausted")


async def _project_page_runtime_state(
    state_mgr: StateKernelManager,
    page: Any,
    sid: str,
    *,
    idempotency_key: str,
    runtime_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:

    if not state_mgr.runtime_snapshot_projection_enabled:
        return {"projected": False, "reason": "runtime_snapshot_projection_not_declared"}
    snapshot = runtime_snapshot
    if snapshot is None:
        snapshot = await _read_runtime_current_state(page)
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return {
            "projected": False,
            "reason": (
                snapshot.get("reason", "runtime_snapshot_read_failed")
                if isinstance(snapshot, dict)
                else "runtime_snapshot_read_failed"
            ),
        }
    return await state_mgr.project_runtime_snapshot(
        sid,
        snapshot.get("current_state"),
        idempotency_key=idempotency_key,
    )


async def _flush_page_before_screenshot(page: Any) -> None:
    try:
        await page.evaluate(
            """() => new Promise((resolve) => {
              const flush = () => {
                if (document.fonts && document.fonts.ready) {
                  document.fonts.ready.then(() => requestAnimationFrame(() => resolve(true)));
                } else {
                  requestAnimationFrame(() => resolve(true));
                }
              };
              requestAnimationFrame(flush);
            })"""
        )
    except Exception:


        pass


async def _capture_page_screenshot(page: Any) -> bytes:
    last_error: BaseException | None = None
    for attempt in range(4):
        try:
            return await page.screenshot(type="png", animations="disabled", caret="hide")
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                raise
            await asyncio.sleep(0.35 * (attempt + 1))
    assert last_error is not None
    raise last_error


class StepBody(BaseModel):


    device: str | None = None
    action: dict[str, Any]
    display_id: int | None = None


class TaskBody(BaseModel):
    task_name: str
    req_device: str | None = None
    display_id: int | None = None


def _load_tasks(tasks_file: str | None, static_dir: str) -> list[dict[str, Any]]:
    if tasks_file is None:
        return []
    p = Path(tasks_file)
    if not p.exists():
        logger.warning("[trace2world] Tasks file not found: %s", tasks_file)
        return []
    return load_and_normalize_tasks(p, Path(static_dir).resolve())


def create_webenv_app(
    static_dir: str,
    *,
    evaluators_dir: str | None = None,
    tasks_file: str | None = None,
    entry_page: str = "homepage.html",
    server_port: int = 6800,
    response_mode: str = "env_text",
    actionability_preflight: bool = True,
    resource_dir: str | None = None,
    resource_manifest: dict[str, Any] | None = None,
    evaluator_plan: str | None = None,
    state_root: str | None = None,
    browser_executable_path: str | None = None,
    portfolio_layout: WebEnvPortfolioLayout | None = None,
) -> FastAPI:
    ctx_mgr: ContextManager | None = None
    state_mgr: StateKernelManager | None = None
    state_mgrs: dict[str, StateKernelManager] = {}

    # Every application owns its own StateKernelManager and its own SQLite
    # sidecar process, so serialisation is only required per manager.  A single
    # shared lock here would also serialise unrelated applications against each
    # other and cap concurrent-episode throughput on the whole portfolio.
    state_io_locks: dict[int, asyncio.Lock] = {}

    def state_io_lock_for(manager: StateKernelManager) -> asyncio.Lock:
        return state_io_locks.setdefault(id(manager), asyncio.Lock())

    episode_by_display: dict[int, tuple[str, str, str]] = {}
    sid_to_app: dict[str, str] = {}
    step_count_by_display: dict[int, int] = {}
    context_step_by_display: dict[int, int | None] = {}
    instance_id = uuid.uuid4().hex
    app_base_url = f"http://localhost:{server_port}/app"
    tasks_data = (
        list(portfolio_layout.tasks)
        if portfolio_layout is not None
        else _load_tasks(tasks_file, static_dir)
    )
    task_to_app = portfolio_layout.task_to_app if portfolio_layout is not None else {}
    for task in tasks_data:
        task_id = str(task.get("task_id") or "")
        app_key = task_to_app.get(task_id, "__default__")
        selected_manifest = (
            portfolio_layout.apps[app_key].resource_manifest
            if portfolio_layout is not None and app_key in portfolio_layout.apps
            else resource_manifest
        ) or {}
        aliases = task.get("available_apps") or task.get("apps") or selected_manifest.get("available_apps")
        if aliases is not None:
            if (
                not isinstance(aliases, list)
                or not aliases
                or not all(isinstance(alias, str) and alias.strip() for alias in aliases)
            ):
                raise ValueError(
                    f"task {task_id!r} available app aliases must be a non-empty string list"
                )
            task["available_apps"] = [alias.strip() for alias in aliases]
            task["apps"] = list(task["available_apps"])
    task_by_id = {t["task_id"]: t for t in tasks_data if t.get("task_id")}
    browser_profile = _resolve_browser_profile(resource_manifest)
    portfolio_profiles = (
        {
            slug: _resolve_browser_profile(layout.resource_manifest)
            for slug, layout in portfolio_layout.apps.items()
        }
        if portfolio_layout is not None
        else {}
    )

    def app_key_for_task(task_id: str) -> str:
        if portfolio_layout is None:
            return "__default__"
        try:
            return task_to_app[task_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown task: {task_id}") from exc

    def layout_for_app(app_key: str):
        if portfolio_layout is None:
            return None
        return portfolio_layout.apps[app_key]

    def profile_for_app(app_key: str) -> dict[str, Any]:
        return portfolio_profiles.get(app_key, browser_profile)

    def state_mgr_for_app(app_key: str) -> StateKernelManager | None:
        return state_mgrs.get(app_key)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal ctx_mgr, state_mgr
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()

        async def browser_factory():
            launch_options: dict[str, Any] = {"headless": True}
            if browser_executable_path:
                launch_options["executable_path"] = browser_executable_path
            return await pw.chromium.launch(**launch_options)

        ctx_mgr = ContextManager(
            None,
            app_base_url,
            entry_page=entry_page,
            response_mode=response_mode,
            actionability_preflight=actionability_preflight,
            browser_factory=browser_factory,
            viewport_size=(
                browser_profile["viewport"]["width"],
                browser_profile["viewport"]["height"],
            ),
            device_scale_factor=browser_profile["device_scale_factor"],
        )
        if portfolio_layout is not None:
            for slug, layout in portfolio_layout.apps.items():
                manifest = layout.resource_manifest or {}
                if manifest.get("runtime_profile") != "state_kernel_sqlite":
                    continue
                evaluator = (
                    manifest.get("evaluator")
                    if isinstance(manifest.get("evaluator"), dict)
                    else {}
                )
                evaluation_enabled = bool(evaluator.get("enabled", True))
                if evaluation_enabled and not layout.evaluator_plan:
                    raise ValueError(
                        f"portfolio resource {slug} requires evaluator_plan"
                    )
                manager = StateKernelManager(
                    layout.resource_dir,
                    manifest,
                    layout.evaluator_plan,
                    writable_root=(Path(state_root) / slug if state_root else None),
                )
                await manager.start()
                state_mgrs[slug] = manager
        elif resource_manifest and resource_manifest.get("runtime_profile") == "state_kernel_sqlite":
            evaluator = (
                resource_manifest.get("evaluator")
                if isinstance(resource_manifest.get("evaluator"), dict)
                else {}
            )
            evaluation_enabled = bool(evaluator.get("enabled", True))
            if not resource_dir:
                raise ValueError("state_kernel_sqlite resource requires resource_dir")
            if evaluation_enabled and not evaluator_plan:
                raise ValueError(
                    "evaluation-enabled state_kernel_sqlite resource requires "
                    "evaluator_plan"
                )
            state_mgr = StateKernelManager(
                resource_dir,
                resource_manifest,
                evaluator_plan,
                writable_root=state_root,
            )
            await state_mgr.start()
            state_mgrs["__default__"] = state_mgr
        logger.info("[trace2world] Chromium ready — serving %s", static_dir)
        yield
        for manager in state_mgrs.values():
            await manager.stop()
        state_mgrs.clear()
        if ctx_mgr is not None:
            await ctx_mgr.close_all()
        try:
            await pw.stop()
        except Exception as exc:
            logger.warning("[trace2world] Playwright stop during shutdown: %s", exc)
        logger.info("[trace2world] Chromium shut down")

    app = FastAPI(title="Trace2World Environment Server", lifespan=lifespan)

    @app.exception_handler(UnknownDisplayError)
    async def unknown_display_handler(
        _request: Request, exc: UnknownDisplayError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "detail": {
                    "code": "invalid_display",
                    "display_id": exc.display_id,
                    "instance_id": instance_id,
                }
            },
        )

    @app.exception_handler(DisplayIdRequiredError)
    async def display_required_handler(
        _request: Request, _exc: DisplayIdRequiredError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": {"code": "display_id_required"}},
        )


    if portfolio_layout is not None:
        for slug, layout in portfolio_layout.apps.items():
            app.mount(
                f"/app/{slug}",
                StaticFiles(directory=layout.static_dir, html=True),
                name=f"webapp-{slug}",
            )
    else:
        app.mount("/app", StaticFiles(directory=static_dir, html=True), name="webapp")


    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "instance_id": instance_id,
            "capabilities": [
                "display_binding_v1",
                "page_identity_v1",
                *(["task_selected_portfolio_v1"] if portfolio_layout is not None else []),
                *(["state_kernel_sqlite_v1"] if state_mgrs else []),
                *(
                    ["evaluation_disabled_env_only_v1"]
                    if state_mgr is not None and not state_mgr.evaluation_enabled
                    else []
                ),
            ],
            "active_display_count": (
                ctx_mgr.active_display_count if ctx_mgr is not None else 0
            ),
            "active_browser_count": (
                ctx_mgr.active_browser_count if ctx_mgr is not None else 0
            ),
            "browser_profile": browser_profile,
            **(
                {
                    "portfolio": {
                        "environment_count": len(portfolio_layout.apps),
                        "task_count": len(tasks_data),
                    }
                }
                if portfolio_layout is not None
                else {}
            ),
        }

    @app.get("/tasks")
    async def list_tasks() -> dict[str, Any]:
        return {"tasks": tasks_data}

    @app.post("/display/acquire")
    async def acquire_display(
        device: str = Query(DEVICE_ID),
    ) -> dict[str, Any]:
        assert ctx_mgr is not None


        display_id = await ctx_mgr.acquire(navigate_entry=False)
        logger.info("[trace2world] Acquired display %d", display_id)
        return {
            "logical_id": display_id,
            "sf_display_id": display_id,
            "instance_id": instance_id,
        }

    @app.post("/display/release")
    async def release_display(
        device: str = Query(DEVICE_ID),
        display_id: int = Query(...),
    ) -> dict[str, Any]:
        assert ctx_mgr is not None
        await ctx_mgr.release(display_id)
        episode = episode_by_display.pop(display_id, None)
        step_count_by_display.pop(display_id, None)
        context_step_by_display.pop(display_id, None)
        if episode is not None:
            sid_to_app.pop(episode[1], None)
        logger.info("[trace2world] Released display %d", display_id)
        return {"ok": True}

    @app.get("/display/status")
    async def display_status(display_id: int = Query(...)) -> dict[str, Any]:
        assert ctx_mgr is not None
        return {
            "display_id": display_id,
            "valid": ctx_mgr.has_display(display_id),
            "instance_id": instance_id,
        }

    @app.get("/display/page_state")
    async def display_page_state(display_id: int = Query(...)) -> dict[str, Any]:
        assert ctx_mgr is not None
        page = ctx_mgr.get_page(display_id)
        episode = episode_by_display.get(display_id)
        profile = profile_for_app(episode[2]) if episode else browser_profile
        return {
            "display_id": display_id,
            "instance_id": instance_id,
            **_page_identity_payload(
                page,
                device_scale_factor=profile["device_scale_factor"],
            ),
        }

    @app.get("/display/runtime_state")
    async def display_runtime_state(display_id: int = Query(...)) -> dict[str, Any]:
        assert ctx_mgr is not None
        page = ctx_mgr.get_page(display_id)
        snapshots = await _read_runtime_audit_state(page)
        return {
            "display_id": display_id,
            "instance_id": instance_id,
            "snapshots": snapshots,
        }

    @app.get("/screenshot")
    async def screenshot(
        device: str = Query(DEVICE_ID),
        return_b64: str = Query("true"),
        display_id: int | None = Query(None),
    ) -> dict[str, Any]:
        assert ctx_mgr is not None
        resolved_display_id = ctx_mgr.resolve_display_id(display_id)
        page = ctx_mgr.get_page(resolved_display_id)
        episode = episode_by_display.get(resolved_display_id)
        profile = profile_for_app(episode[2]) if episode else browser_profile


        await _flush_page_before_screenshot(page)


        buf = await _capture_page_screenshot(page)
        b64 = base64.b64encode(buf).decode("ascii")
        return {
            "b64_png": b64,
            **_page_identity_payload(
                page,
                device_scale_factor=profile["device_scale_factor"],
            ),
        }

    @app.post("/step")
    async def step(body: StepBody) -> dict[str, Any]:
        assert ctx_mgr is not None
        page = ctx_mgr.get_page(body.display_id)
        page_url_before_action = str(page.url or "")
        resolved_display_id = ctx_mgr.resolve_display_id(body.display_id)
        episode = episode_by_display.get(resolved_display_id)
        app_key = episode[2] if episode else "__default__"
        profile = profile_for_app(app_key)
        selected_layout = layout_for_app(app_key)
        selected_base_url = (
            f"http://localhost:{server_port}/app/{app_key}"
            if selected_layout is not None
            else ctx_mgr.app_base_url
        )
        selected_entry_page = (
            selected_layout.entry_page if selected_layout is not None else ctx_mgr.entry_page
        )
        selected_task = task_by_id.get(episode[0]) if episode is not None else None
        selected_app_aliases = (
            selected_task.get("available_apps")
            if isinstance(selected_task, dict)
            else None
        )
        action_type = str(body.action.get("action_type") or "")
        has_task_episode = episode is not None
        is_terminal_action = action_type in {"answer", "terminate", "finished", "status"}
        previous_context_step = context_step_by_display.get(resolved_display_id)
        execution_action_index = step_count_by_display.get(resolved_display_id, 0)
        execution_step_count = execution_action_index + 1
        provisional_context_step: int | None = None
        if has_task_episode and not is_terminal_action:
            provisional_context_step = _next_execution_context_step(
                previous_context_step,
                execution_action_index,
            )


            await ctx_mgr.update_task_context_step(
                resolved_display_id,
                provisional_context_step,
            )
            context_step_by_display[resolved_display_id] = provisional_context_step
        try:
            result = await execute_action(
                page, body.action, selected_base_url, selected_entry_page,
                device_scale_factor=profile["device_scale_factor"],
                scroll_delta=profile["scroll_delta"],
                app_aliases=selected_app_aliases,
            )
        except Exception:
            if has_task_episode and not is_terminal_action:
                await ctx_mgr.update_task_context_step(
                    resolved_display_id,
                    previous_context_step,
                )
                context_step_by_display[resolved_display_id] = previous_context_step
            raise
        observation = result.get("action_observation") if isinstance(result, dict) else None
        observed_step_index = _observed_trace_step_index(observation)
        action_rejected = bool(
            isinstance(result, dict) and result.get("actionability_rejected")
        )
        if action_rejected and has_task_episode and not is_terminal_action:


            await ctx_mgr.update_task_context_step(
                resolved_display_id,
                previous_context_step,
            )
            context_step_by_display[resolved_display_id] = previous_context_step
        elif has_task_episode and not is_terminal_action:
            step_count_by_display[resolved_display_id] = execution_step_count
            if observed_step_index is not None:


                await ctx_mgr.update_task_context_step(
                    resolved_display_id,
                    observed_step_index,
                )
                context_step_by_display[resolved_display_id] = observed_step_index
            elif provisional_context_step is not None:
                if _should_reproject_task_context_after_action(
                    body.action,
                    page_url_before_action,
                    str(page.url or ""),
                ):
                    await ctx_mgr.update_task_context_step(
                        resolved_display_id,
                        provisional_context_step,
                    )
                context_step_by_display[resolved_display_id] = provisional_context_step
        elif observed_step_index is not None:


            await ctx_mgr.update_task_context_step(
                resolved_display_id,
                observed_step_index,
            )
        if isinstance(result, dict) and has_task_episode and not is_terminal_action:
            result = {
                **result,
                "execution_context_step": context_step_by_display.get(
                    resolved_display_id,
                    previous_context_step,
                ),
                "execution_context_step_source": (
                    "observed_action_id"
                    if observed_step_index is not None
                    else "execution_step_fallback_pre_action"
                ),
            }
        selected_state_mgr = state_mgr_for_app(app_key)
        if selected_state_mgr is not None:
            episode = episode_by_display.get(resolved_display_id)
            if episode is not None:
                execution_step_index = step_count_by_display.get(
                    resolved_display_id,
                    0,
                )
                authority_result = None
                terminal_boundary_result = {
                    "recorded": False,
                    "reason": "action_observation_unavailable",
                }
                if isinstance(observation, dict):
                    async with state_io_lock_for(selected_state_mgr):
                        authority_result = await selected_state_mgr.record_action(
                            episode[0],
                            episode[1],
                            observation,
                            idempotency_key=f"host-step:{step_count_by_display[resolved_display_id]}",
                        )
                        terminal_boundary_result = await selected_state_mgr.record_terminal_action_boundary(
                            episode[0],
                            episode[1],
                            execution_step_index,
                            observation,
                            idempotency_key=f"host-terminal-boundary:{execution_step_index}",
                        )


                runtime_snapshot = await _read_runtime_current_state(page)
                async with state_io_lock_for(selected_state_mgr):
                    projection_result = await _project_page_runtime_state(
                        selected_state_mgr,
                        page,
                        episode[1],
                        idempotency_key=(
                            "host-runtime-projection:step:"
                            f"{step_count_by_display[resolved_display_id]}"
                        ),
                        runtime_snapshot=runtime_snapshot,
                    )
                result = {
                    **(result or {}),
                    **(
                        {"state_authority_result": authority_result}
                        if authority_result is not None
                        else {}
                    ),
                    "terminal_action_boundary_result": terminal_boundary_result,
                    "runtime_snapshot_projection": projection_result,
                }
        return {
            "ok": True,
            **(result or {}),
            **_page_identity_payload(
                page,
                device_scale_factor=profile["device_scale_factor"],
            ),
        }

    @app.post("/task/init")
    async def task_init(body: TaskBody) -> dict[str, Any]:
        assert ctx_mgr is not None
        display_id = ctx_mgr.resolve_display_id(body.display_id)
        task = task_by_id.get(body.task_name)
        if task is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {body.task_name}")
        app_key = app_key_for_task(body.task_name)
        selected_layout = layout_for_app(app_key)
        selected_profile = profile_for_app(app_key)
        start_page = task.get("start_page") or (
            selected_layout.entry_page if selected_layout is not None else entry_page
        )
        initial_state = (
            task.get("initial_state")
            if isinstance(task.get("initial_state"), dict)
            else None
        )
        source_reference_task_id = task.get("source_reference_task_id")
        if not isinstance(source_reference_task_id, str) or not source_reference_task_id:
            source_reference_task_id = None


        sid = uuid.uuid4().hex
        selected_state_mgr = state_mgr_for_app(app_key)
        if selected_state_mgr is not None:
            async with state_io_lock_for(selected_state_mgr):
                sid = await selected_state_mgr.initialize_episode(
                    body.task_name,
                    display_id,
                    initial_state=initial_state,
                )
        previous_episode = episode_by_display.get(display_id)
        if previous_episode is not None:
            sid_to_app.pop(previous_episode[1], None)
        episode_by_display[display_id] = (body.task_name, sid, app_key)
        sid_to_app[sid] = app_key
        step_count_by_display[display_id] = 0
        context_step_by_display[display_id] = None
        await ctx_mgr.open_task(
            display_id,
            start_page,
            body.task_name,
            initial_state=initial_state,
            source_reference_task_id=source_reference_task_id,
            sid=sid,
            app_base_url=(
                f"http://localhost:{server_port}/app/{app_key}"
                if selected_layout is not None
                else None
            ),
            viewport_size=(
                selected_profile["viewport"]["width"],
                selected_profile["viewport"]["height"],
            ),
            device_scale_factor=selected_profile["device_scale_factor"],
        )
        runtime_snapshot_projection = None
        if selected_state_mgr is not None and sid is not None:
            page = ctx_mgr.get_page(display_id)
            runtime_snapshot = await asyncio.wait_for(
                _read_runtime_current_state(page),
                timeout=float(
                    os.environ.get("T2W_TASK_INIT_SNAPSHOT_TIMEOUT_S", "60")
                ),
            )
            async with state_io_lock_for(selected_state_mgr):
                runtime_snapshot_projection = await _project_page_runtime_state(
                    selected_state_mgr,
                    page,
                    sid,
                    idempotency_key="host-runtime-projection:task-init",
                    runtime_snapshot=runtime_snapshot,
                )
        return {
            "ok": True,
            "sid": sid,
            **(
                {"runtime_snapshot_projection": runtime_snapshot_projection}
                if runtime_snapshot_projection is not None
                else {}
            ),
        }

    @app.get("/task/goal")
    async def task_goal(
        task_name: str = Query(...),
        display_id: int | None = Query(None),
    ) -> str:
        task = task_by_id.get(task_name)
        if task is None:
            logger.warning("[trace2world] task_goal: unknown task %s", task_name)
            return ""
        return task.get("instruction") or task.get("name") or task.get("description") or ""

    @app.get("/task/eval")
    async def task_eval(
        task_name: str = Query(...),
        display_id: int | None = Query(None),
        preview: bool = Query(False),
    ) -> dict[str, Any]:
        assert ctx_mgr is not None
        resolved_display_id = ctx_mgr.resolve_display_id(display_id)
        episode = episode_by_display.get(resolved_display_id)
        app_key = app_key_for_task(task_name)
        selected_state_mgr = state_mgr_for_app(app_key)
        if selected_state_mgr is not None:
            if episode is None or episode[0] != task_name:
                return {"score": 0.0, "reason": "missing_state_kernel_episode"}
            async with state_io_lock_for(selected_state_mgr):
                if preview:
                    result = await selected_state_mgr.preview_and_evaluate(task_name, episode[1])
                else:
                    result = await selected_state_mgr.finalize_and_evaluate(task_name, episode[1])
            return result
        page = ctx_mgr.get_page(resolved_display_id)
        selected_layout = layout_for_app(app_key)
        result = await run_evaluator_result(
            page,
            task_id=task_name,
            evaluators_dir=(
                str(selected_layout.evaluators_dir)
                if selected_layout is not None and selected_layout.evaluators_dir
                else evaluators_dir
            ),
        )
        return {
            "score": result.raw_reward or 0.0,
            "reason": result.reason or "",
            **result.to_payload(),
        }

    @app.get("/task/state_snapshot")
    async def task_state_snapshot(
        task_name: str = Query(...),
        display_id: int = Query(...),
        kind: str = Query("audit"),
    ) -> dict[str, Any]:
        assert ctx_mgr is not None
        resolved_display_id = ctx_mgr.resolve_display_id(display_id)
        episode = episode_by_display.get(resolved_display_id)
        if episode is None or episode[0] != task_name:
            raise HTTPException(status_code=404, detail="missing_state_kernel_episode")
        selected_state_mgr = state_mgr_for_app(episode[2])
        if selected_state_mgr is None:
            raise HTTPException(status_code=404, detail="state_kernel_not_configured")
        async with state_io_lock_for(selected_state_mgr):
            snapshot = await selected_state_mgr.snapshot_episode(
                episode[1], kind=kind,
            )
        return {
            "task_name": task_name,
            "display_id": resolved_display_id,
            "sid": episode[1],
            "snapshot": snapshot,
        }

    @app.post("/task/tear_down")
    async def task_tear_down(body: TaskBody) -> dict[str, Any]:
        if ctx_mgr is not None:
            display_id = ctx_mgr.resolve_display_id(body.display_id)
            episode = episode_by_display.pop(display_id, None)
            step_count_by_display.pop(display_id, None)
            context_step_by_display.pop(display_id, None)
            if episode is not None:
                sid_to_app.pop(episode[1], None)
            selected_state_mgr = (
                state_mgr_for_app(episode[2]) if episode is not None else None
            )
            if episode is not None and selected_state_mgr is not None:
                async with state_io_lock_for(selected_state_mgr):
                    await selected_state_mgr.teardown_episode(episode[1])


            await ctx_mgr.deactivate(display_id)
        return {"ok": True}

    @app.api_route("/api/v1/{state_path:path}", methods=["GET", "POST"])
    async def state_kernel_proxy(state_path: str, request: Request) -> Response:
        if not state_mgrs:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "state_kernel_not_configured"}},
            )
        body = await request.body()
        sid = request.query_params.get("sid")
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
        app_key = sid_to_app.get(sid or "")
        selected_state_mgr = state_mgr_for_app(app_key or "__default__")
        if selected_state_mgr is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "state_kernel_episode_not_found"}},
            )
        query = f"?{request.url.query}" if request.url.query else ""
        async with state_io_lock_for(selected_state_mgr):
            response = await selected_state_mgr.request(
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
