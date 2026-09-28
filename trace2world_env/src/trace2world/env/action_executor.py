
import json
import logging
import math
import os
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)


_SCROLL_DELTA = 200


_POST_CLICK_SETTLE_MS = 50


_POST_GESTURE_SETTLE_MS = 50


_PREFLIGHT_READY_TIMEOUT_MS = 10000


_DEFAULT_WAIT_SECONDS = 1.0
_MAX_WAIT_SECONDS = 30.0


_TERMINAL_ACTIONS = frozenset({"answer", "terminate", "finished", "status"})
_REJECT_STATUSES = frozenset({"noninteractive", "disabled", "passive_static"})
_GENERIC_NONINTERACTIVE_MESSAGE = "The selected component is not interactive."
_GENERIC_ASK_USER_UNDECLARED_MESSAGE = (
    "The current page does not declare an ask_user action host. "
    "No user request was sent and no page state was changed."
)
_GENERIC_BACK_BOUNDARY_MESSAGE = (
    "System back would leave the current WebEnv app. The current page was kept. "
    "Use a visible in-app back control instead."
)
_REJECTION_MESSAGES = {
    "NOT_INTERACTIVE": "ERROR[NOT_INTERACTIVE]\nNo state change. Choose another target.",
    "NO_EDITABLE_FOCUS": (
        "ERROR[NO_EDITABLE_FOCUS]\n"
        "No state change. Click an editable field before typing."
    ),
    "INVALID_ACTION": "ERROR[INVALID_ACTION]\nNo state change. Choose a supported action.",
    "FORMAT_INVALID": (
        "ERROR[FORMAT_INVALID]\n"
        "Your action could not be parsed. Emit exactly one valid tool call."
    ),
}
_TERMINAL_SUBMISSION_RESULT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "reason",
        "source",
        "submitted_at",
        "evidence_bound",
        "evidence_observation_count",
        "interaction_count",
        "page",
    }
)


def _normalise_app_alias(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _configured_app_aliases(
    page: Page,
    app_base_url: str,
    explicit_aliases: list[str] | None = None,
) -> list[str]:
    if explicit_aliases is not None:
        parsed = explicit_aliases
    else:
        raw = os.environ.get("T2W_APP_ALIASES", "")
        try:
            parsed = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            parsed = []
    aliases = [value.strip() for value in parsed if isinstance(value, str) and value.strip()]
    slug = str(app_base_url).rstrip("/").split("/")[-1].strip()


    if not aliases and slug:
        aliases.append(slug)
    return aliases


def _open_app_feedback(
    page: Page,
    app_name: Any,
    app_base_url: str,
    app_aliases: list[str] | None = None,
) -> dict[str, Any]:
    requested = str(app_name or "").strip()
    aliases = _configured_app_aliases(page, app_base_url, app_aliases)
    alias_set = {_normalise_app_alias(value) for value in aliases}
    if not requested:
        message = "No app name was supplied; the current WebEnv app was left unchanged."
        reason = "missing_app_name"
        status = "invalid"
        error_class = "parse_or_schema_error"
    elif _normalise_app_alias(requested) in alias_set:
        current = aliases[0] if aliases else "the current app"
        message = f"Already in {current}; open_app was satisfied without navigation."
        reason = "already_in_requested_app"
        status = "satisfied"
        error_class = "none"
    else:
        shown = ", ".join(aliases) if aliases else "the current app"
        message = f"App '{requested}' is not available in this WebEnv. Available app: {shown}."
        reason = "app_unavailable"
        status = "unsupported"
        error_class = "action_unavailable"
    return {
        "actionability_rejected": status != "satisfied",
        "env_text": message,
        "actionability_feedback": {
            "schema": "agentforge.webenv.open_app_result.v1",
            "message": message,
            "result": {
                "schema": "agentforge.webenv.open_app_result.v1",
                "status": status,
                "response_mode": "env_text",
                "reason": reason,
                "requested_app": requested,
                "available_apps": aliases,
                "error_class": error_class,
                "business_failure": False,
            },
        },
        "last_action_result": {
            "kind": "open_app",
            "status": status,
            "reason": reason,
            "requested_app": requested,
            "available_apps": aliases,
            "error_class": error_class,
            "business_failure": False,
        },
    }


def _unknown_action_payload(action: dict[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("action_type") or "").strip() or "<missing>"
    error_detail = str(action.get("error") or "").strip().casefold()
    unsupported_action = (
        error_detail.startswith("unsupported action_type:")
        or error_detail.startswith("unsupported action")
    )
    parser_invalid = (
        not unsupported_action
        and (
            action_type in {"<missing>", "unknown"}
            or action.get("format_valid") is False
            or str(action.get("error_class") or "") == "parse_or_schema_error"
        )
    )
    error_code = "FORMAT_INVALID" if parser_invalid else "INVALID_ACTION"
    message = _REJECTION_MESSAGES[error_code]
    reason = "parser_format_invalid" if parser_invalid else "unsupported_action"
    return {
        "actionability_rejected": True,
        "environment_suspect": False,
        "environment_suspect_reason": "model_format_or_unsupported_action",
        "env_text": message,
        "actionability_feedback": {
            "schema": "agentforge.webenv.action_rejected.v1",
            "message": message,
            "result": {
                "schema": "agentforge.webenv.action_rejected.v1",
                "status": "invalid",
                "response_mode": "env_text",
                "reason": reason,
                "action_type": action_type,
                "error_code": error_code,
                "state_changed": False,
                "error_class": (
                    "parse_or_schema_error" if parser_invalid else "unsupported_action"
                ),
                "format_valid": not parser_invalid,
                "business_failure": False,
            },
        },
        "last_action_result": {
            "kind": "parser_format" if parser_invalid else "unsupported_action",
            "status": "invalid",
            "action_type": action_type,
            "reason": reason,
            "error_code": error_code,
            "state_changed": False,
            "error_class": "parse_or_schema_error" if parser_invalid else "unsupported_action",
            "format_valid": not parser_invalid,
            "business_failure": False,
        },
    }


def _is_in_app_url(url: Any, app_base_url: str) -> bool:
    value = str(url or "")
    base = str(app_base_url or "").rstrip("/")
    return bool(base) and (
        value == base or value.startswith(base + "/") or value.startswith(base + "?")
    )


def _back_boundary_payload() -> dict[str, Any]:
    message = _GENERIC_BACK_BOUNDARY_MESSAGE
    return {
        "actionability_rejected": True,
        "env_text": message,
        "actionability_feedback": {
            "schema": "agentforge.webenv.action_rejected.v1",
            "message": message,
            "result": {
                "schema": "agentforge.webenv.action_rejected.v1",
                "status": "blocked",
                "response_mode": "env_text",
                "reason": "no_in_app_back_history",
                "error_class": "navigation_boundary",
                "business_failure": False,
                "message": message,
            },
        },
        "last_action_result": {
            "kind": "navigation_boundary",
            "rejected": True,
            "status": "blocked",
            "reason": "no_in_app_back_history",
            "error_class": "navigation_boundary",
            "business_failure": False,
            "message": message,
        },
    }


async def _navigate_back_with_app_boundary(page: Page, app_base_url: str) -> dict[str, Any] | None:
    previous_url = str(page.url or "")
    try:
        await page.go_back(wait_until="load")
    except PlaywrightTimeoutError:


        committed_url = str(page.url or "")
        if not committed_url or committed_url == previous_url:
            raise
        logger.warning(
            "[ActionExecutor] navigate_back load wait timed out after URL commit "
            "from=%s to=%s; continuing with boundary validation",
            previous_url,
            committed_url,
        )
    await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)
    destination_url = str(page.url or "")
    if _is_in_app_url(destination_url, app_base_url):
        return None

    restored = False
    try:
        await page.go_forward(wait_until="load")
        await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)
        restored = _is_in_app_url(page.url, app_base_url)
    except Exception as exc:
        logger.warning("[ActionExecutor] forward restore after back-boundary failed: %s", exc)
    if not restored and _is_in_app_url(previous_url, app_base_url):
        await page.goto(previous_url, wait_until="load")
        await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)
        restored = _is_in_app_url(page.url, app_base_url)
    logger.warning(
        "[ActionExecutor] rejected navigate_back across app boundary from=%s to=%s restored=%s",
        previous_url,
        destination_url,
        restored,
    )
    return _back_boundary_payload()


def _to_css(val: int | float, scale: float) -> int:
    return int(val / scale)


def _wait_duration_seconds(action: dict[str, Any]) -> float:
    raw_duration = next(
        (
            action[key]
            for key in ("waiting_time", "duration", "seconds")
            if key in action
        ),
        _DEFAULT_WAIT_SECONDS,
    )
    if isinstance(raw_duration, bool):
        raise ValueError("wait duration must be a finite non-negative number of seconds")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "wait duration must be a finite non-negative number of seconds"
        ) from exc
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("wait duration must be a finite non-negative number of seconds")
    return min(duration, _MAX_WAIT_SECONDS)


def _viewport_size(page: Page) -> tuple[int, int] | None:
    viewport = getattr(page, "viewport_size", None)
    if isinstance(viewport, dict):
        width = viewport.get("width")
        height = viewport.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
    return None


def _point_to_css(
    page: Page,
    x: int | float,
    y: int | float,
    scale: float,
) -> tuple[int, int]:
    fx, fy = float(x), float(y)
    viewport = _viewport_size(page)
    if viewport is not None and 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
        width, height = viewport
        return int(fx * width), int(fy * height)
    return _to_css(fx, scale), _to_css(fy, scale)


async def _wait_for_preflight_ready(page: Page) -> None:
    try:
        await page.wait_for_load_state("load", timeout=_PREFLIGHT_READY_TIMEOUT_MS)
    except Exception as exc:
        logger.debug("[ActionExecutor] preflight readiness wait skipped: %s", exc)


async def _inspect_actionability(
    page: Page,
    x: int,
    y: int,
    *,
    event_type: str,
) -> dict[str, Any] | None:
    await _wait_for_preflight_ready(page)
    try:
        result = await page.evaluate(
            """({ x, y, eventType }) => {
              const api = window.GhostwebActionability;
              if (!api || !api.inspectPoint) return null;
              const inspected = api.inspectPoint(x, y, {
                event_type: eventType,
                source: 'webenv_playwright_preflight'
              });
              if (!inspected) return null;
              const selected = document.elementFromPoint(x, y);
              const gestureHost = selected && selected.closest
                ? selected.closest('[data-gesture-host], [data-ghostweb-gesture-action]')
                : null;
              const clickHost = selected && selected.closest
                ? selected.closest(
                    '[data-ghostweb-action], [data-ghostweb-route-action], '
                    + '[data-ghostweb-target], [data-ghostweb-effect-kind], '
                    + '[data-ghostweb-action-id], [data-action-id]'
                  )
                : null;
              const clickHostKind = clickHost
                ? ((clickHost.getAttribute('data-ghostweb-action') || '') + ' '
                   + (clickHost.getAttribute('data-ghostweb-effect-kind') || '') + ' '
                   + (clickHost.getAttribute('data-ghostweb-gesture-action') || ''))
                : '';
              const hostActionId = (host) => host
                ? (host.getAttribute('data-ghostweb-action-id') ||
                   host.getAttribute('data-action-id') || '')
                : '';
              const hostActionKind = (host) => host
                ? (host.getAttribute('data-ghostweb-action') ||
                   host.getAttribute('data-ghostweb-effect-kind') ||
                   host.getAttribute('data-ghostweb-gesture-action') ||
                   host.getAttribute('data-ghostweb-route-action') || '')
                : '';
              const distinctClickHost = Boolean(
                clickHost
                && clickHost !== gestureHost
                && !clickHost.hasAttribute('data-gesture-host')
                && !/(?:gesture|swipe)/i.test(clickHostKind)
              );
              return Object.assign({}, inspected, {
                __webenv_gesture_host_action_id: hostActionId(gestureHost),
                __webenv_distinct_click_host: distinctClickHost,
                __webenv_action_host_diagnostics: {
                  schema: 'agentforge.webenv.action_host_diagnostics.v1',
                  selected_tag: selected ? String(selected.tagName || '').toLowerCase() : '',
                  selected_id: selected ? String(selected.id || '') : '',
                  click_host_action_id: hostActionId(clickHost),
                  click_host_action_kind: hostActionKind(clickHost),
                  gesture_host_action_id: hostActionId(gestureHost),
                  gesture_host_action_kind: hostActionKind(gestureHost),
                  distinct_click_host: distinctClickHost
                }
              });
            }""",
            {"x": x, "y": y, "eventType": event_type},
        )
    except Exception as exc:
        logger.debug("[ActionExecutor] actionability inspect failed: %s", exc)
        return None
    if not isinstance(result, dict):
        return None
    gesture_action_id = str(result.pop("__webenv_gesture_host_action_id", "") or "")
    distinct_click_host = result.pop("__webenv_distinct_click_host", False) is True
    raw_diagnostics = result.pop("__webenv_action_host_diagnostics", None)
    if isinstance(raw_diagnostics, dict):
        action_host_diagnostics = {
            "schema": "agentforge.webenv.action_host_diagnostics.v1",
            **{
                key: str(raw_diagnostics.get(key) or "")[:256]
                for key in (
                    "selected_tag",
                    "selected_id",
                    "click_host_action_id",
                    "click_host_action_kind",
                    "gesture_host_action_id",
                    "gesture_host_action_kind",
                )
            },
            "distinct_click_host": raw_diagnostics.get("distinct_click_host") is True,
        }
    else:
        action_host_diagnostics = None
    if action_host_diagnostics is not None:
        result["action_host_diagnostics"] = action_host_diagnostics
    if (
        event_type in {"click", "double_tap", "long_press"}
        and gesture_action_id
        and gesture_action_id == str(result.get("action_id") or "")
        and not distinct_click_host
    ):
        return {
            **result,
            "status": "noninteractive",
            "message": _GENERIC_NONINTERACTIVE_MESSAGE,
            "action_id": "",
            "action_kind": "",
            "target": "",
            "reason": "gesture_host_requires_gesture",
        }
    return result


async def _inspect_focused_actionability(
    page: Page,
    *,
    event_type: str,
) -> dict[str, Any] | None:
    try:
        result = await page.evaluate(
            """({ eventType }) => {
              const api = window.GhostwebActionability;
              const focused = document.activeElement;
              if (!api || !api.inspectElement || !focused) return null;
              const inspected = api.inspectElement(focused, {
                event_type: eventType,
                source: 'webenv_playwright_focused_input'
              });
              if (!inspected) return null;
              const observedValue = 'value' in focused
                ? focused.value
                : (focused.textContent || '');
              return Object.assign({}, inspected, {
                observed_value: String(observedValue == null ? '' : observedValue),
                observed_element_tag: String(focused.tagName || '').toLowerCase()
              });
            }""",
            {"eventType": event_type},
        )
    except Exception as exc:
        logger.debug("[ActionExecutor] focused actionability inspect failed: %s", exc)
        return None
    return result if isinstance(result, dict) else None


async def _inspect_focused_editable(page: Page) -> dict[str, Any] | None:
    try:
        result = await page.evaluate(
            """() => {
              const focused = document.activeElement;
              if (!focused) return { editable: false, observed_element_tag: '' };
              const tag = String(focused.tagName || '').toLowerCase();
              const inputType = tag === 'input'
                ? String(focused.getAttribute('type') || 'text').toLowerCase()
                : '';
              const nonTextInputTypes = new Set([
                'button', 'checkbox', 'color', 'file', 'hidden', 'image',
                'radio', 'range', 'reset', 'submit'
              ]);
              const editable = !focused.disabled && !focused.readOnly && (
                tag === 'textarea'
                || (tag === 'input' && !nonTextInputTypes.has(inputType))
                || focused.isContentEditable === true
              );
              return {
                editable,
                observed_element_tag: tag,
                observed_input_type: inputType
              };
            }"""
        )
    except Exception as exc:
        logger.debug("[ActionExecutor] focused editable inspect failed: %s", exc)
        return None
    return result if isinstance(result, dict) else None


def _focused_editable_rejection_payload(
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    probe_unavailable = result is None
    reason = "focused_editable_probe_unavailable" if probe_unavailable else "no_focused_editable"
    message = _REJECTION_MESSAGES["NO_EDITABLE_FOCUS"]
    observed_element_tag = str((result or {}).get("observed_element_tag") or "")[:32]
    observed_input_type = str((result or {}).get("observed_input_type") or "")[:32]
    feedback_result = {
        "schema": "agentforge.webenv.action_rejected.v1",
        "status": "noninteractive",
        "response_mode": "env_text",
        "reason": reason,
        "error_class": "action_context_missing",
        "error_code": "NO_EDITABLE_FOCUS",
        "state_changed": False,
        "business_failure": False,
        "observed_element_tag": observed_element_tag,
        "observed_input_type": observed_input_type,
    }
    return {
        "actionability_rejected": True,
        "environment_suspect": False,
        "environment_suspect_reason": "no_editable_focus",
        "env_text": message,
        "actionability_feedback": {
            "schema": "agentforge.webenv.action_rejected.v1",
            "message": message,
            "result": feedback_result,
        },
        "last_action_result": {
            "kind": "input_text",
            "rejected": True,
            "status": "noninteractive",
            "reason": reason,
            "error_class": "action_context_missing",
            "error_code": "NO_EDITABLE_FOCUS",
            "state_changed": False,
            "business_failure": False,
            "observed_element_tag": observed_element_tag,
            "observed_input_type": observed_input_type,
        },
    }


async def _read_satisfied_viewport_gesture_action_ids(
    page: Page,
    *,
    gesture_axis: str,
) -> list[str]:
    try:
        result = await page.evaluate(
            """({ gestureAxis }) => {
              const selector = [
                '[data-gesture-host][data-ghostweb-viewport-target][data-action-id]',
                '[data-gesture-host][data-ghostweb-viewport-target][data-action-aliases]',
                '[data-gesture-host][data-ghostweb-viewport-target][data-ghostweb-action-id]',
                '[data-ghostweb-gesture-action][data-ghostweb-viewport-target][data-action-id]',
                '[data-ghostweb-gesture-action][data-ghostweb-viewport-target][data-action-aliases]',
                '[data-ghostweb-gesture-action][data-ghostweb-viewport-target][data-ghostweb-action-id]'
              ].join(',');
              function stageSatisfied(candidate) {
                const targetSelector = String(
                  candidate.getAttribute('data-ghostweb-viewport-target') || ''
                );
                if (!targetSelector) return false;
                let target = null;
                try { target = document.querySelector(targetSelector); }
                catch (error) { return false; }
                if (!target || !target.getBoundingClientRect) return false;
                const rect = target.getBoundingClientRect();
                const viewportTop = 0;
                let viewportBottom = window.innerHeight ||
                  (document.documentElement && document.documentElement.clientHeight) || 0;
                const occlusionSelector = String(
                  candidate.getAttribute('data-ghostweb-bottom-occlusion-selector') || ''
                );
                if (occlusionSelector) {
                  try {
                    const occlusion = document.querySelector(occlusionSelector);
                    if (occlusion && occlusion.getBoundingClientRect) {
                      const occlusionRect = occlusion.getBoundingClientRect();
                      if (occlusionRect.height > 0 && occlusionRect.top > viewportTop) {
                        viewportBottom = Math.min(viewportBottom, occlusionRect.top);
                      }
                    }
                  } catch (error) {}
                }
                const stage = String(
                  candidate.getAttribute('data-ghostweb-viewport-stage') || 'entered'
                );
                if (stage === 'fully_visible') {
                  return rect.width > 0 && rect.height > 0 &&
                    rect.top >= viewportTop && rect.bottom <= viewportBottom;
                }
                return rect.width > 0 && rect.height > 0 &&
                  rect.bottom > viewportTop && rect.top < viewportBottom;
              }
              const ids = new Set();
              document.querySelectorAll(selector).forEach(function(candidate) {
                if (gestureAxis &&
                    candidate.getAttribute('data-gesture-axis') !== gestureAxis) return;
                if (!stageSatisfied(candidate)) return;
                const canonical = String(candidate.getAttribute('data-ghostweb-action-id') || '');
                const primary = String(candidate.getAttribute('data-action-id') || '');
                if (canonical) ids.add(canonical);
                if (primary) ids.add(primary);
                String(candidate.getAttribute('data-action-aliases') || '')
                  .split(/\\s+/).filter(Boolean).forEach(function(id) { ids.add(id); });
              });
              return Array.from(ids).sort();
            }""",
            {"gestureAxis": gesture_axis},
        )
    except Exception as exc:
        logger.debug("[ActionExecutor] pre-gesture viewport stage read failed: %s", exc)
        return []
    if not isinstance(result, list):
        return []
    return sorted({str(value) for value in result if isinstance(value, str) and value})


async def _inspect_gesture_actionability(
    page: Page,
    *,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    event_type: str,
    previously_satisfied_viewport_action_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    delta_x = abs(end_x - start_x)
    delta_y = abs(end_y - start_y)
    gesture_axis = "horizontal" if delta_x > delta_y else "vertical" if delta_y > delta_x else ""
    try:
        result = await page.evaluate(
            """({ startX, startY, endX, endY, eventType, gestureAxis,
                    previouslySatisfiedViewportActionIds }) => {
              const api = window.GhostwebActionability;
              if (!api || !api.inspectElement) return null;
              const selector = [
                '[data-gesture-host][data-action-id]',
                '[data-gesture-host][data-action-aliases]',
                '[data-gesture-host][data-ghostweb-action-id]',
                '[data-ghostweb-gesture-action][data-action-id]',
                '[data-ghostweb-gesture-action][data-action-aliases]',
                '[data-ghostweb-gesture-action][data-ghostweb-action-id]'
              ].join(',');
              const points = [[endX, endY], [startX, startY]];
              const allHosts = Array.from(document.querySelectorAll(selector));
              function isVisible(candidate) {
                if (!candidate || !candidate.getBoundingClientRect) return false;
                const rect = candidate.getBoundingClientRect();
                const style = window.getComputedStyle
                  ? window.getComputedStyle(candidate)
                  : null;
                const viewportWidth = window.innerWidth ||
                  (document.documentElement && document.documentElement.clientWidth) || 0;
                const viewportHeight = window.innerHeight ||
                  (document.documentElement && document.documentElement.clientHeight) || 0;
                return rect.width > 0 && rect.height > 0 &&
                  rect.bottom > 0 && rect.right > 0 &&
                  rect.top < viewportHeight && rect.left < viewportWidth &&
                  (!style || (style.display !== 'none' && style.visibility !== 'hidden'));
              }
              function axisMatches(candidate) {
                return !gestureAxis ||
                  candidate.getAttribute('data-gesture-axis') === gestureAxis;
              }
              function viewportStageSatisfied(candidate) {
                if (!candidate || !candidate.getAttribute) return true;
                const targetSelector = String(
                  candidate.getAttribute('data-ghostweb-viewport-target') || ''
                );
                if (!targetSelector) return true;
                let target = null;
                try { target = document.querySelector(targetSelector); }
                catch (error) { return false; }
                if (!target || !target.getBoundingClientRect) return false;
                const rect = target.getBoundingClientRect();
                const viewportTop = 0;
                let viewportBottom = window.innerHeight ||
                  (document.documentElement && document.documentElement.clientHeight) || 0;
                const occlusionSelector = String(
                  candidate.getAttribute('data-ghostweb-bottom-occlusion-selector') || ''
                );
                if (occlusionSelector) {
                  try {
                    const occlusion = document.querySelector(occlusionSelector);
                    if (occlusion && occlusion.getBoundingClientRect) {
                      const occlusionRect = occlusion.getBoundingClientRect();
                      if (occlusionRect.height > 0 && occlusionRect.top > viewportTop) {
                        viewportBottom = Math.min(viewportBottom, occlusionRect.top);
                      }
                    }
                  } catch (error) {}
                }
                const stage = String(
                  candidate.getAttribute('data-ghostweb-viewport-stage') || 'entered'
                );
                if (stage === 'fully_visible') {
                  return rect.width > 0 && rect.height > 0 &&
                    rect.top >= viewportTop && rect.bottom <= viewportBottom;
                }
                return rect.width > 0 && rect.height > 0 &&
                  rect.bottom > viewportTop && rect.top < viewportBottom;
              }
              function actionIds(candidate) {
                return [
                  candidate.getAttribute('data-ghostweb-action-id') || '',
                  candidate.getAttribute('data-action-id') || ''
                ].concat(
                  String(candidate.getAttribute('data-action-aliases') || '')
                    .split(/\\s+/)
                    .filter(Boolean)
                ).filter(Boolean);
              }
              function belongsToContext(actionId) {
                return !!contextTraceId && (
                  actionId === contextTraceId ||
                  actionId.indexOf(contextTraceId + ':') === 0
                );
              }
              let context = window.__ghostwebExecutionContext || null;
              if (!context) {
                try {
                  context = JSON.parse(
                    (window.sessionStorage &&
                      window.sessionStorage.getItem('ghostweb.execution_context')) ||
                    (window.localStorage &&
                      window.localStorage.getItem('ghostweb.execution_context')) ||
                    '{}'
                  );
                } catch (error) {
                  context = {};
                }
              }
              const contextActionId = String((context && context.action_id) || '');
              const contextTraceId = String(
                (context && (
                  context.reference_trace_id || context.trace_id || context.task_id
                )) || ''
              );
              let spatialHost = null;
              let fallbackHost = null;
              for (const point of points) {
                const selected = document.elementFromPoint(point[0], point[1]);
                let candidate = selected;
                while (candidate) {
                  if (candidate.matches && candidate.matches(selector)) {
                    if (!fallbackHost && viewportStageSatisfied(candidate)) {
                      fallbackHost = candidate;
                    }
                    if (axisMatches(candidate) && viewportStageSatisfied(candidate)) {
                      spatialHost = candidate;
                      break;
                    }
                  }
                  candidate = candidate.parentElement;
                }
                if (spatialHost) break;
              }
              let host = null;
              if (contextActionId) {
                host = allHosts.find(function(candidate) {
                  return isVisible(candidate) && axisMatches(candidate) &&
                    viewportStageSatisfied(candidate) &&
                    actionIds(candidate).includes(contextActionId);
                }) || null;
              }
              if (!host && contextTraceId && spatialHost &&
                  actionIds(spatialHost).some(belongsToContext)) {
                host = spatialHost;
              }
              if (!host && contextTraceId) {
                host = allHosts.find(function(candidate) {
                  return isVisible(candidate) && axisMatches(candidate) &&
                    viewportStageSatisfied(candidate) &&
                    actionIds(candidate).some(belongsToContext);
                }) || null;
              }
              if (!host && !contextTraceId) host = spatialHost || fallbackHost;
              if (!host) return null;
              const inspected = api.inspectElement(host, {
                event_type: eventType,
                source: 'webenv_playwright_post_gesture'
              });
              const inspectedActionId = String(
                inspected && inspected.action_id || ''
              );
              if (host.getAttribute('data-ghostweb-viewport-target') &&
                  inspectedActionId &&
                  previouslySatisfiedViewportActionIds.includes(inspectedActionId)) {
                return null;
              }
              return inspected || null;
            }""",
            {
                "startX": start_x,
                "startY": start_y,
                "endX": end_x,
                "endY": end_y,
                "eventType": event_type,
                "gestureAxis": gesture_axis,
                "previouslySatisfiedViewportActionIds": (
                    previously_satisfied_viewport_action_ids or []
                ),
            },
        )
    except Exception as exc:
        logger.debug("[ActionExecutor] gesture actionability inspect failed: %s", exc)
        return None
    return result if isinstance(result, dict) else None


async def _wait_for_action_navigation(
    page: Page,
    action_observation: dict[str, Any] | None,
) -> None:
    if not isinstance(action_observation, dict):
        return
    if str(action_observation.get("action_kind") or "") != "navigate":
        return
    target = str(action_observation.get("target") or "").strip()
    if not target:
        return
    try:
        await page.wait_for_url(f"**/{target}*", timeout=5000)
    except Exception as exc:


        logger.debug("[ActionExecutor] navigation settle wait skipped: %s", exc)


async def _is_actionability_preflight_enabled(page: Page) -> bool:
    try:
        enabled = await page.evaluate(
            """() => {
              const values = [
                window.__GhostwebActionabilityPreflight,
                window.localStorage &&
                  window.localStorage.getItem('ghostweb.actionability_preflight'),
                window.sessionStorage &&
                  window.sessionStorage.getItem('ghostweb.actionability_preflight')
              ];
              for (const value of values) {
                if (value === false) return false;
                if (value === true) return true;
                if (typeof value === 'string') {
                  const normalized = value.trim().toLowerCase();
                  if (['false', '0', 'no', 'off'].includes(normalized)) {
                    return false;
                  }
                  if (['true', '1', 'yes', 'on'].includes(normalized)) {
                    return true;
                  }
                }
              }
              return true;
            }"""
        )
    except Exception as exc:
        logger.debug("[ActionExecutor] actionability preflight flag read failed: %s", exc)
        return True
    return bool(enabled)


def _rejected_actionability_payload(
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or "")
    if status not in _REJECT_STATUSES:
        return None


    message = _REJECTION_MESSAGES["NOT_INTERACTIVE"]


    sanitized_result = {
        key: result[key] for key in ("schema", "status", "response_mode", "reason") if key in result
    }
    sanitized_result["message"] = message
    sanitized_result["error_code"] = "NOT_INTERACTIVE"
    sanitized_result["state_changed"] = False
    diagnostics = result.get("action_host_diagnostics")
    diagnostic_click_kind = ""
    diagnostic_click_id = ""
    if isinstance(diagnostics, dict):
        diagnostic_click_kind = str(diagnostics.get("click_host_action_kind") or "")
        diagnostic_click_id = str(diagnostics.get("click_host_action_id") or "")
        diagnostic_selected_tag = str(diagnostics.get("selected_tag") or "").casefold()
    else:
        diagnostic_selected_tag = ""
    gesture_mismatch = str(result.get("reason") or "") in {
        "gesture_host_requires_gesture",
        "gesture_action_required",
    } or (
        "gesture" in diagnostic_click_kind.casefold()
        or "swipe" in diagnostic_click_kind.casefold()
    )
    environment_suspect = (
        bool(diagnostic_click_id)
        or diagnostic_selected_tag in {
            "a", "button", "input", "textarea", "select", "option", "summary",
        }
    ) and not gesture_mismatch
    payload = {
        "actionability_rejected": True,
        "environment_suspect": environment_suspect,
        "environment_suspect_reason": (
            "declared_visual_semantic_host_rejected"
            if environment_suspect
            else "noninteractive_or_gesture_mismatch"
        ),
        "env_text": message,
        "actionability_feedback": {
            "schema": "ghostweb.actionability.webenv_step.v1",
            "message": message,
            "result": sanitized_result,
        },
        "last_action_result": {
            "kind": "actionability_preflight",
            "rejected": True,
            "status": status,
            "message": message,
            "reason": result.get("reason"),
            "error_code": "NOT_INTERACTIVE",
            "state_changed": False,
        },
    }
    if isinstance(diagnostics, dict):
        payload["actionability_diagnostics"] = diagnostics
    return payload


def _ask_user_rejected_payload(message: str, reason: str) -> dict[str, Any]:
    return {
        "actionability_rejected": True,
        "env_text": message,
        "actionability_feedback": {
            "schema": "agentforge.webenv.action_rejected.v1",
            "message": message,
            "result": {
                "schema": "agentforge.webenv.action_rejected.v1",
                "status": "rejected",
                "response_mode": "env_text",
                "reason": reason,
                "message": message,
            },
        },
        "last_action_result": {
            "kind": "external_action_host",
            "rejected": True,
            "status": "rejected",
            "message": message,
            "reason": reason,
        },
    }


async def _execute_ask_user(page: Page, action: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            r"""() => {
              const visible = (element) => {
                if (!element || element.hidden) return false;
                const style = window.getComputedStyle(element);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  style.opacity !== '0' && element.getClientRects().length > 0;
              };
              const hosts = Array.from(
                document.querySelectorAll('[data-ghostweb-external-action]')
              ).filter(visible);
              if (hosts.length === 0) {
                return {
                  schema: 'agentforge.webenv.external_action.v1',
                  status: 'rejected',
                  reason: 'action_host_not_declared'
                };
              }
              if (hosts.length !== 1) {
                return {
                  schema: 'agentforge.webenv.external_action.v1',
                  status: 'rejected',
                  reason: 'ambiguous_action_host'
                };
              }
              const host = hosts[0];
              let feedback = null;
              const observe = (event) => { feedback = event && event.detail || null; };
              document.addEventListener(
                'ghostweb:external-action-feedback', observe, { once: true }
              );
              host.click();
              document.removeEventListener('ghostweb:external-action-feedback', observe);
              const described = String(host.getAttribute('aria-describedby') || '')
                .split(/\s+/).filter(Boolean)
                .map((id) => document.getElementById(id))
                .filter(Boolean)
                .map((element) => String(element.textContent || '').trim())
                .find(Boolean) || '';
              return {
                schema: 'agentforge.webenv.external_action.v1',
                status: String(feedback && feedback.status || 'accepted'),
                reason: String(feedback && (
                  feedback.error_code || feedback.reason
                ) || ''),
                message: String(feedback && feedback.message || described || '')
              };
            }"""
        )
    except Exception as exc:
        logger.warning("[ActionExecutor] ask_user host execution failed: %s", exc)
        return _ask_user_rejected_payload(
            "The page-owned ask_user action host could not be executed. "
            "No user request was sent and no page state was changed.",
            "action_host_execution_failed",
        )

    if not isinstance(result, dict):
        return _ask_user_rejected_payload(
            "The page-owned ask_user action host returned no structured feedback. "
            "No user request was confirmed.",
            "action_host_feedback_missing",
        )
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or "")
    if status != "accepted":
        if reason == "action_host_not_declared":
            message = _GENERIC_ASK_USER_UNDECLARED_MESSAGE
        elif reason == "ambiguous_action_host":
            message = (
                "The current page declares multiple ask_user action hosts, so no "
                "user request was sent. Use a visible page control to disambiguate."
            )
        else:
            message = str(result.get("message") or (
                "The page-owned ask_user action host rejected the request. "
                "No user request was confirmed."
            ))
        return _ask_user_rejected_payload(message, reason or "action_host_rejected")

    await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)
    message = str(result.get("message") or (
        "The page accepted the user request without returning additional text."
    ))
    feedback = {
        "schema": "agentforge.webenv.external_action_feedback.v1",
        "status": "accepted",
        "response_mode": "env_text",
        "message": message,
    }
    return {
        "env_text": message,
        "ask_user_response": message,
        "external_action_feedback": feedback,
        "last_action_result": {
            "kind": "external_action_host",
            "rejected": False,
            "status": "accepted",
            "message": message,
        },
    }


def _terminal_answer_text(action: dict[str, Any]) -> str:
    for key in ("text", "answer"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _execute_terminal_action(
    page: Page,
    action: dict[str, Any],
) -> dict[str, Any]:
    answer = _terminal_answer_text(action)
    if not answer:
        return {}
    action_type = str(action.get("action_type") or "answer")
    raw_result = await page.evaluate(
        """async ({text, actionType}) => {
          const api = window.GhostwebAgentTerminalSubmission;
          if (!api || typeof api.submit !== 'function') {
            return {
              schema: 'ghostweb.agent_terminal_submission_result.v1',
              status: 'unavailable',
              reason: 'page_terminal_submission_api_unavailable'
            };
          }
          const result = await api.submit({
            schema: 'ghostweb.agent_terminal_submission.v1',
            text,
            action_type: actionType
          });
          if (!result || typeof result !== 'object' || Array.isArray(result)) {
            return {
              schema: 'ghostweb.agent_terminal_submission_result.v1',
              status: 'rejected',
              reason: 'page_terminal_submission_result_invalid'
            };
          }
          return result;
        }""",
        {"text": answer, "actionType": action_type},
    )
    if not isinstance(raw_result, dict):
        raw_result = {
            "schema": "ghostweb.agent_terminal_submission_result.v1",
            "status": "rejected",
            "reason": "page_terminal_submission_result_invalid",
        }
    result = {
        key: value
        for key, value in raw_result.items()
        if key in _TERMINAL_SUBMISSION_RESULT_FIELDS
        and isinstance(value, (str, int, float, bool))
        and not isinstance(value, (dict, list))
    }
    result.setdefault("schema", "ghostweb.agent_terminal_submission_result.v1")
    result.setdefault("status", "rejected")
    if result["status"] == "recorded":
        await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)
    return {
        "terminal_submission_result": result,
        "last_action_result": {
            "kind": "terminal_answer_submission",
            "status": result["status"],
            "reason": str(result.get("reason") or ""),
            "source": str(result.get("source") or ""),
        },
    }


async def execute_action(
    page: Page,
    action: dict[str, Any],
    app_base_url: str,
    entry_page: str,
    *,
    device_scale_factor: float = 2.0,
    scroll_delta: int = _SCROLL_DELTA,
    app_aliases: list[str] | None = None,
) -> dict[str, Any]:
    action_type = action.get("action_type", "")
    sf = device_scale_factor
    if isinstance(scroll_delta, bool) or not isinstance(scroll_delta, int) or scroll_delta <= 0:
        raise ValueError("scroll_delta must be a positive integer CSS-pixel distance")
    action_observation: dict[str, Any] | None = None
    action_observation_source = "webenv_playwright_preflight"

    if action_type in _TERMINAL_ACTIONS:
        return await _execute_terminal_action(page, action)

    if action_type == "ask_user":
        return await _execute_ask_user(page, action)

    if action_type in {"open_app", "awake"}:
        return _open_app_feedback(
            page,
            action.get("app_name") or action.get("app") or action.get("value"),
            app_base_url,
            app_aliases,
        )

    if action_type in ("click", "double_tap"):
        x, y = _point_to_css(page, action["x"], action["y"], sf)
        if await _is_actionability_preflight_enabled(page):
            inspected = await _inspect_actionability(page, x, y, event_type=action_type)
            rejected = _rejected_actionability_payload(inspected)
            if rejected:
                logger.warning(
                    "[ActionExecutor] rejected click point=(%s,%s) viewport=%s "
                    "inspected_viewport=%s reason=%s label=%r element_id=%r "
                    "action_id=%r action_kind=%r",
                    x,
                    y,
                    getattr(page, "viewport_size", None),
                    (inspected or {}).get("viewport") if isinstance(inspected, dict) else None,
                    (inspected or {}).get("reason") if isinstance(inspected, dict) else None,
                    (inspected or {}).get("label") if isinstance(inspected, dict) else None,
                    (inspected or {}).get("element_id") if isinstance(inspected, dict) else None,
                    (inspected or {}).get("action_id") if isinstance(inspected, dict) else None,
                    (inspected or {}).get("action_kind") if isinstance(inspected, dict) else None,
                )
                return rejected
            action_observation = inspected
        click_count = 2 if action_type == "double_tap" else 1
        await page.mouse.click(x, y, click_count=click_count)
        await _wait_for_action_navigation(page, action_observation)
        await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)

    elif action_type == "long_press":
        x, y = _point_to_css(page, action["x"], action["y"], sf)
        if await _is_actionability_preflight_enabled(page):
            inspected = await _inspect_actionability(page, x, y, event_type=action_type)
            rejected = _rejected_actionability_payload(inspected)
            if rejected:
                return rejected
            action_observation = inspected
        await page.mouse.click(x, y, delay=500)
        await _wait_for_action_navigation(page, action_observation)
        await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)

    elif action_type in ("scroll", "swipe"):
        if action_type == "swipe" and (
            ("start_x" in action and "start_y" in action)
            or ("start_coordinate" in action and "end_coordinate" in action)
        ):
            if "start_coordinate" in action and "end_coordinate" in action:
                start = action.get("start_coordinate") or [0, 0]
                end = action.get("end_coordinate") or start
                raw_start_x, raw_start_y = start[0], start[1]
                raw_end_x, raw_end_y = end[0], end[1]
            else:
                raw_start_x = action.get("start_x", action.get("x", 0))
                raw_start_y = action.get("start_y", action.get("y", 0))
                raw_end_x = action.get("end_x", raw_start_x)
                raw_end_y = action.get("end_y", raw_start_y)
            start_x, start_y = _point_to_css(page, raw_start_x, raw_start_y, sf)
            end_x, end_y = _point_to_css(page, raw_end_x, raw_end_y, sf)
            gesture_axis = (
                "horizontal"
                if abs(end_x - start_x) > abs(end_y - start_y)
                else "vertical"
                if abs(end_y - start_y) > abs(end_x - start_x)
                else ""
            )
            previously_satisfied = await _read_satisfied_viewport_gesture_action_ids(
                page, gesture_axis=gesture_axis
            )
            if action.get("gesture_kind") == "scroll":
                await page.mouse.move(start_x, start_y)
                await page.mouse.wheel(start_x - end_x, start_y - end_y)
                gesture_event_type = "wheel"
            else:
                await page.mouse.move(start_x, start_y)
                await page.mouse.down()
                await page.mouse.move(end_x, end_y, steps=10)
                await page.mouse.up()
                gesture_event_type = "pointerup"
            await page.wait_for_timeout(_POST_GESTURE_SETTLE_MS)
            action_observation = await _inspect_gesture_actionability(
                page,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                event_type=gesture_event_type,
                previously_satisfied_viewport_action_ids=previously_satisfied,
            )
            action_observation_source = "webenv_playwright_post_gesture"

        else:


            direction = action.get("direction", "down")
            dx, dy = 0, 0
            if action_type == "scroll":

                if direction == "down":
                    dy = scroll_delta
                elif direction == "up":
                    dy = -scroll_delta
                elif direction == "right":
                    dx = scroll_delta
                elif direction == "left":
                    dx = -scroll_delta
            else:


                if direction == "down":
                    dy = -scroll_delta
                elif direction == "up":
                    dy = scroll_delta
                elif direction == "right":
                    dx = -scroll_delta
                elif direction == "left":
                    dx = scroll_delta
            x, y = _point_to_css(
                page,
                action.get("x", int(187 * sf)),
                action.get("y", int(406 * sf)),
                sf,
            )
            gesture_axis = (
                "horizontal" if abs(dx) > abs(dy) else "vertical" if abs(dy) > abs(dx) else ""
            )
            previously_satisfied = await _read_satisfied_viewport_gesture_action_ids(
                page, gesture_axis=gesture_axis
            )
            await page.mouse.move(x, y)
            await page.mouse.wheel(dx, dy)
            await page.wait_for_timeout(_POST_GESTURE_SETTLE_MS)
            action_observation = await _inspect_gesture_actionability(
                page,
                start_x=x,
                start_y=y,
                end_x=x + dx,
                end_y=y + dy,
                event_type="wheel",
                previously_satisfied_viewport_action_ids=previously_satisfied,
            )
            action_observation_source = "webenv_playwright_post_gesture"

    elif action_type in ("type", "input_text"):
        text = action.get("text", "")
        focused_editable = await _inspect_focused_editable(page)
        if not focused_editable or focused_editable.get("editable") is not True:
            return _focused_editable_rejection_payload(focused_editable)


        if text.isascii():
            await page.keyboard.type(text, delay=50)
        else:
            await page.keyboard.insert_text(text)
        action_observation = await _inspect_focused_actionability(page, event_type=action_type)
        action_observation_source = "webenv_playwright_focused_input"

    elif action_type == "navigate_home":
        entry_url = f"{app_base_url}/{entry_page}"
        await page.goto(entry_url, wait_until="load")

    elif action_type in ("navigate_back", "system_back") or (
        action_type == "system_button" and action.get("button") == "back"
    ):


        boundary_rejection = await _navigate_back_with_app_boundary(page, app_base_url)
        if boundary_rejection is not None:
            return boundary_rejection

    elif action_type in {"key", "keyboard_enter"}:
        key = "Enter" if action_type == "keyboard_enter" else action.get("key", "")
        if key:
            await page.keyboard.press(key)
            await _wait_for_action_navigation(page, action_observation)
            await page.wait_for_timeout(_POST_CLICK_SETTLE_MS)
            if await _is_actionability_preflight_enabled(page):
                action_observation = await _inspect_focused_actionability(
                    page, event_type="keyboard_enter" if action_type == "keyboard_enter" else "key"
                )
                action_observation_source = "webenv_playwright_focused_input"

    elif action_type == "wait":
        duration_seconds = _wait_duration_seconds(action)
        await page.wait_for_timeout(round(duration_seconds * 1000))

    elif action_type == "drag":

        raw_start_x = action.get("start_x", action.get("x", 0))
        raw_start_y = action.get("start_y", action.get("y", 0))
        raw_end_x = action.get("end_x", raw_start_x)
        raw_end_y = action.get("end_y", raw_start_y)
        start_x, start_y = _point_to_css(page, raw_start_x, raw_start_y, sf)
        end_x, end_y = _point_to_css(page, raw_end_x, raw_end_y, sf)
        gesture_axis = (
            "horizontal"
            if abs(end_x - start_x) > abs(end_y - start_y)
            else "vertical"
            if abs(end_y - start_y) > abs(end_x - start_x)
            else ""
        )
        previously_satisfied = await _read_satisfied_viewport_gesture_action_ids(
            page, gesture_axis=gesture_axis
        )
        await page.mouse.move(start_x, start_y)
        await page.mouse.down()
        await page.mouse.move(end_x, end_y, steps=10)
        await page.mouse.up()
        await page.wait_for_timeout(_POST_GESTURE_SETTLE_MS)
        action_observation = await _inspect_gesture_actionability(
            page,
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
            event_type="pointerup",
            previously_satisfied_viewport_action_ids=previously_satisfied,
        )
        action_observation_source = "webenv_playwright_post_gesture"

    else:
        logger.warning("[ActionExecutor] Unknown action_type: %s", action_type)
        return _unknown_action_payload(action)
    if action_observation and action_observation.get("action_id"):
        observation_payload = {
            "action_id": action_observation.get("action_id"),
            "action_kind": action_observation.get("action_kind"),
            "target": action_observation.get("target"),
            "source": action_observation_source,
        }
        if action_observation_source == "webenv_playwright_focused_input":
            observation_payload.update(
                {
                    "event_type": action_type,
                    "observed_value": action_observation.get("observed_value"),
                    "observed_element_tag": action_observation.get("observed_element_tag"),
                }
            )
        return {"action_observation": observation_payload}
    return {}
