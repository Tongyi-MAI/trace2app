"""The page-facing runtime protocol: scripts the host injects into and reads from app pages.

The generated apps read their launch context from ``window.__simExecutionContext`` and the
``sim.*`` storage keys, publish runtime state through ``window.SimRuntimeState`` and checkpoint
events through ``window.SimCheckpointRuntime`` / ``__sim_checkpoint_events_v1__``. These names
are part of the frozen environment payload.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from playwright.async_api import Page

#: Pages run with text feedback and tap preflight enabled; the released policy was trained so.
RESPONSE_MODE = "env_text"
ACTIONABILITY_PREFLIGHT = True
TASK_CONTEXT_PROTOCOL = "sim.task_launch_context.v1"


def url_with_sid(url: str, sid: str, app_base_url: str) -> str:
    """Add ``?sid=`` to an app URL that lacks it (other URLs are returned unchanged)."""
    if not sid:
        return url
    target = urlsplit(url)
    app = urlsplit(app_base_url)
    app_prefix = app.path.rstrip("/") + "/"
    if (target.scheme, target.netloc) != (app.scheme, app.netloc):
        return url
    if not target.path.startswith(app_prefix):
        return url
    query = dict(parse_qsl(target.query, keep_blank_values=True))
    if query.get("sid") == sid:
        return url
    query["sid"] = sid
    return urlunsplit(
        (target.scheme, target.netloc, target.path, urlencode(query), target.fragment)
    )


def task_url(app_base_url: str, start_page: str, sid: str | None) -> str:
    """The URL a task starts at: ``<app_base_url>/<start_page>?sid=<sid>``."""
    base = app_base_url.rstrip("/")
    path = str(start_page or "").lstrip("/")
    query = urlencode({"sid": sid}) if sid else ""
    return f"{base}/{path}" + (f"?{query}" if query else "")


def launch_context_script(
    task_id: str,
    *,
    start_filename: str,
    initial_state: dict[str, Any] | None,
    source_reference_task_id: str | None,
    sid: str | None,
) -> str:
    """Init script that publishes the task launch context to every document of the episode.

    ``trace_id`` is the reference trace the task derives from (its own id for native tasks);
    pages use it to namespace checkpoint events and action ids.
    """
    source_trace_id = source_reference_task_id or task_id
    context = {
        "task_context_protocol": TASK_CONTEXT_PROTOCOL,
        "task_id": task_id,
        "taskId": task_id,
        "source_reference_task_id": source_reference_task_id,
        "reference_trace_id": source_trace_id,
        "trace_id": source_trace_id,
        "trainable": True,
        "source": "env_server",
        "start_filename": start_filename,
        "response_mode": RESPONSE_MODE,
        "actionability_preflight": ACTIONABILITY_PREFLIGHT,
        "initial_state": initial_state or {},
        "sid": sid,
    }
    context_json = json.dumps(context, ensure_ascii=False)
    return f"""
    (() => {{
      const context = {context_json};
      try {{
        const text = JSON.stringify(context);
        window.SimActiveTraceId = context.trace_id;
        window.__simActiveTraceId = context.trace_id;
        window.__simExecutionContext = context;
        window.localStorage.setItem('sim.execution_context', text);
        window.localStorage.setItem(
          'sim.task_initial_state',
          JSON.stringify(context.initial_state || {{}})
        );
        window.localStorage.setItem('sim.current_task_id', context.task_id);
        window.localStorage.setItem('sim.trainable_trace_id', context.task_id);
        window.localStorage.setItem('sim.response_mode', context.response_mode || 'env_text');
        window.localStorage.setItem(
          'sim.actionability_preflight',
          String(!!context.actionability_preflight)
        );
        window.sessionStorage.setItem('sim.execution_context', text);
        window.sessionStorage.setItem(
          'sim.task_initial_state',
          JSON.stringify(context.initial_state || {{}})
        );
        window.sessionStorage.setItem('sim.current_task_id', context.task_id);
        window.sessionStorage.setItem('sim.trainable_trace_id', context.task_id);
        window.sessionStorage.setItem(
          'sim.response_mode',
          context.response_mode || 'env_text'
        );
        window.sessionStorage.setItem(
          'sim.actionability_preflight',
          String(!!context.actionability_preflight)
        );
        window.__SimResponseMode = context.response_mode || 'env_text';
        window.__SimActionabilityPreflight = !!context.actionability_preflight;
        window.SimTaskInitialState = context.initial_state || {{}};
        window.__simTaskInitialState = context.initial_state || {{}};
        window.__simTaskContextBootstrapped = true;
      }} catch (e) {{
        console.error('Failed to initialize task context', e);
      }}
    }})()
    """


FRAME_STRIP_JS = """
(() => {
    const style = document.createElement('style');
    style.textContent = `
        html {
            background: #fff !important;
            overflow: hidden !important;
            height: 100% !important;
        }
        body {
            max-width: none !important;
            margin: 0 !important;
            border-radius: 0 !important;
            box-shadow: none !important;
            width: 100vw !important;
            height: 100vh !important;
            overflow: hidden !important;
        }
        body::before, body::after {
            display: none !important;
        }
    `;
    if (document.head) {
        document.head.appendChild(style);
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            document.head.appendChild(style);
        });
    }
})();
"""
_CONTEXT_STEP_UPDATE_JS = """(nextStepIndex) => {
                  let context = window.__simExecutionContext;
                  if (!context || typeof context !== 'object') {
                    try {
                      context = JSON.parse(
                        window.sessionStorage.getItem('sim.execution_context') ||
                        window.localStorage.getItem('sim.execution_context') ||
                        '{}'
                      );
                    } catch (error) {
                      context = {};
                    }
                  }
                  context = Object.assign({}, context);
                  const traceId = String(
                    context.reference_trace_id || context.trace_id || context.task_id || ''
                  );
                  if (nextStepIndex === null) {
                    delete context.step_index;
                    delete context.raw_step_index;
                    delete context.action_id;
                  } else {
                    context.step_index = nextStepIndex;
                    context.raw_step_index = nextStepIndex;
                    context.action_id = traceId ? traceId + ':step' + nextStepIndex : '';
                  }
                  const serialized = JSON.stringify(context);
                  window.__simExecutionContext = context;
                  window.SimActiveTraceId = traceId;
                  window.__simActiveTraceId = window.SimActiveTraceId;
                  try { window.localStorage.setItem('sim.execution_context', serialized); } catch (error) {}
                  try { window.sessionStorage.setItem('sim.execution_context', serialized); } catch (error) {}
                  window.dispatchEvent(new CustomEvent('sim:task-context-update', {
                    detail: {
                      step_index: nextStepIndex,
                      action_id: context.action_id || '',
                      context
                    }
                  }));
                }"""


async def set_context_step(page: Page, step_index: int | None) -> None:
    """Publish the reference-trace step the agent is at (``<trace_id>:step<N>`` action id).

    Pages use it to attribute the next input to a declared trace action; ``None`` clears it.
    Retries while a navigation replaces the execution context.
    """
    for attempt in range(5):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        try:
            await page.evaluate(_CONTEXT_STEP_UPDATE_JS, step_index)
            return
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc) or attempt == 4:
                raise
            await asyncio.sleep(0.05)


_AUDIT_EVENT_DEDUPE_JS = r"""
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


def page_identity(
    page: Page,
    *,
    device_scale_factor: float,
) -> dict[str, Any]:
    """Current URL, page file name, viewport, and device scale factor of ``page``."""
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


async def read_runtime_current_state(page: Page) -> dict[str, Any]:
    """Read ``SimRuntimeState.snapshot().current_state`` (retrying across navigations).

    Returns ``{available: true, current_state}`` or ``{available: false, reason}``.
    """
    snapshot_script = """() => {
          try {
            const runtime = window.SimRuntimeState;
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


async def read_runtime_audit_state(page: Page) -> dict[str, Any]:

    """Read-only audit of the page runtime: execution context, runtime snapshot,
    de-duplicated checkpoint events and their sources, and app-owned state diagnostics.
    """
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
          const executionContext = clone(window.__simExecutionContext || null) ||
            readJsonStorage('sim.execution_context', null);
          const taskId = String(
            executionContext && (executionContext.task_id || executionContext.taskId) || ''
          ).trim();
          const sid = String(executionContext && executionContext.sid || '').trim();
          const checkpointEventKey = '__sim_checkpoint_events_v1__';
          const checkpointCarrier = taskId && sid
            ? checkpointEventKey + '.carrier.' + encodeURIComponent(taskId + ':' + sid) : null;
""" + _AUDIT_EVENT_DEDUPE_JS + """
          const checkpointAudit = (() => {
            const rows = [];
            const sources = [];
            const append = (source, value, key) => {
              if (!Array.isArray(value)) return;
              rows.push(...value);
              sources.push({ source, key, count: value.length });
            };
            try {
              const runtime = window.SimCheckpointRuntime;
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
            runtime_state: snapshot(window.SimRuntimeState),
            checkpoint_events: checkpointAudit.events,
            checkpoint_event_sources: checkpointAudit.sources,
            app_owned_state_diagnostics: {
              bridge_revision: String(
                window.SimAppOwnedStateBridge &&
                window.SimAppOwnedStateBridge.revision || ''
              ),
              task_world_instance_registered: Boolean(
                taskId && window.SimEnhancedTaskWorldInstances &&
                window.SimEnhancedTaskWorldInstances.instances &&
                window.SimEnhancedTaskWorldInstances.instances[taskId]
              ),
              checkpoint_event_carrier: checkpointCarrier,
              checkpoint_event_carrier_present: Boolean(
                checkpointCarrier && readJson(window.localStorage, checkpointCarrier)
              ),
              persist_failure: clone(window.__simAppOwnedStatePersistFailure || null),
              carrier_deferred: clone(window.__simAppOwnedStateCarrierDeferred || null),
              contract_deferred: clone(window.__simAppOwnedStateContractDeferred || null),
            },
            runtime_events: readJsonStorage('sim_runtime_events_v1', []),
            enhanced_world: snapshot(window.SimEnhancedTaskWorld),
            enhanced_behavior: snapshot(window.SimEnhancedTaskBehavior),
            enhanced_world_enabled: root
              ? root.getAttribute('data-sim-enhanced-world-enabled') : null,
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


async def flush_before_screenshot(page: Page) -> None:
    """Wait for fonts and two animation frames so the screenshot shows settled layout."""
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


async def capture_screenshot(page: Page) -> bytes:
    """PNG screenshot with animations disabled and the caret hidden (4 attempts)."""
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
