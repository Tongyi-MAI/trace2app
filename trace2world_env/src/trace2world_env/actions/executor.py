"""Execute one agent action in a page.

:func:`execute_action` returns a result dict. Rejected actions carry ``actionability_rejected``
and agent-visible feedback (see :mod:`trace2world_env.actions.feedback`) and leave the page
unchanged; accepted actions may carry an ``action_observation`` naming the declared page action
the page attributed the input to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from trace2world_env.actions.feedback import (
    ASK_USER_UNDECLARED_MESSAGE,
    ask_user_rejected_payload,
    back_boundary_payload,
    focused_editable_rejection_payload,
    open_app_feedback,
    rejected_actionability_payload,
    unknown_action_payload,
)
from trace2world_env.actions.probes import (
    inspect_actionability,
    inspect_focused_actionability,
    inspect_focused_editable,
    inspect_gesture_actionability,
    is_actionability_preflight_enabled,
    read_satisfied_viewport_gesture_action_ids,
    wait_for_action_navigation,
)
from trace2world_env.actions.vocabulary import (
    DEFAULT_SCROLL_ORIGIN,
    FOCUSED_INPUT_SOURCE,
    KEY_ACTIONS,
    OPEN_APP_ACTIONS,
    POST_CLICK_SETTLE_MS,
    POST_GESTURE_SETTLE_MS,
    POST_GESTURE_SOURCE,
    PREFLIGHT_SOURCE,
    TAP_ACTIONS,
    TERMINAL_ACTIONS,
    TEXT_ACTIONS,
    action_type,
    drag_endpoints,
    gesture_axis,
    has_swipe_endpoints,
    is_back_action,
    point_to_css,
    swipe_endpoints,
    terminal_answer_text,
    wait_duration_seconds,
    wheel_offsets,
)

logger = logging.getLogger(__name__)


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


def _is_in_app_url(url: Any, app_base_url: str) -> bool:
    """Whether ``url`` is under the environment's static app root."""
    value = str(url or "")
    base = str(app_base_url or "").rstrip("/")
    return bool(base) and (
        value == base or value.startswith(base + "/") or value.startswith(base + "?")
    )


async def _navigate_back_with_app_boundary(page: Page, app_base_url: str) -> dict[str, Any] | None:
    """Go back in history; if that leaves the app, restore the page and reject the action."""
    previous_url = str(page.url or "")
    try:
        await page.go_back(wait_until="load")
    except PlaywrightTimeoutError:
        committed_url = str(page.url or "")
        if not committed_url or committed_url == previous_url:
            raise
        logger.warning(
            "navigate_back load wait timed out after URL commit "
            "from=%s to=%s; continuing with boundary validation",
            previous_url,
            committed_url,
        )
    await page.wait_for_timeout(POST_CLICK_SETTLE_MS)
    destination_url = str(page.url or "")
    if _is_in_app_url(destination_url, app_base_url):
        return None

    restored = False
    try:
        await page.go_forward(wait_until="load")
        await page.wait_for_timeout(POST_CLICK_SETTLE_MS)
        restored = _is_in_app_url(page.url, app_base_url)
    except Exception as exc:
        logger.warning("forward restore after back-boundary failed: %s", exc)
    if not restored and _is_in_app_url(previous_url, app_base_url):
        await page.goto(previous_url, wait_until="load")
        await page.wait_for_timeout(POST_CLICK_SETTLE_MS)
        restored = _is_in_app_url(page.url, app_base_url)
    logger.warning(
        "rejected navigate_back across app boundary from=%s to=%s restored=%s",
        previous_url,
        destination_url,
        restored,
    )
    return back_boundary_payload()


async def _execute_ask_user(page: Page) -> dict[str, Any]:
    """Click the page's single visible ask_user host and relay its feedback."""
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
                document.querySelectorAll('[data-sim-external-action]')
              ).filter(visible);
              if (hosts.length === 0) {
                return {
                  schema: 'trace2world.external_action.v1',
                  status: 'rejected',
                  reason: 'action_host_not_declared'
                };
              }
              if (hosts.length !== 1) {
                return {
                  schema: 'trace2world.external_action.v1',
                  status: 'rejected',
                  reason: 'ambiguous_action_host'
                };
              }
              const host = hosts[0];
              let feedback = null;
              const observe = (event) => { feedback = event && event.detail || null; };
              document.addEventListener(
                'sim:external-action-feedback', observe, { once: true }
              );
              host.click();
              document.removeEventListener('sim:external-action-feedback', observe);
              const described = String(host.getAttribute('aria-describedby') || '')
                .split(/\s+/).filter(Boolean)
                .map((id) => document.getElementById(id))
                .filter(Boolean)
                .map((element) => String(element.textContent || '').trim())
                .find(Boolean) || '';
              return {
                schema: 'trace2world.external_action.v1',
                status: String(feedback && feedback.status || 'accepted'),
                reason: String(feedback && (
                  feedback.error_code || feedback.reason
                ) || ''),
                message: String(feedback && feedback.message || described || '')
              };
            }"""
        )
    except Exception as exc:
        logger.warning("ask_user host execution failed: %s", exc)
        return ask_user_rejected_payload(
            "The page-owned ask_user action host could not be executed. "
            "No user request was sent and no page state was changed.",
            "action_host_execution_failed",
        )

    if not isinstance(result, dict):
        return ask_user_rejected_payload(
            "The page-owned ask_user action host returned no structured feedback. "
            "No user request was confirmed.",
            "action_host_feedback_missing",
        )
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or "")
    if status != "accepted":
        if reason == "action_host_not_declared":
            message = ASK_USER_UNDECLARED_MESSAGE
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
        return ask_user_rejected_payload(message, reason or "action_host_rejected")

    await page.wait_for_timeout(POST_CLICK_SETTLE_MS)
    message = str(result.get("message") or (
        "The page accepted the user request without returning additional text."
    ))
    feedback = {
        "schema": "trace2world.external_action_feedback.v1",
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


async def _execute_terminal_action(
    page: Page,
    action: dict[str, Any],
) -> dict[str, Any]:
    """Submit a terminal answer to ``window.SimAgentTerminalSubmission`` when the page has one."""
    answer = terminal_answer_text(action)
    if not answer:
        return {}
    action_type = str(action.get("action_type") or "answer")
    raw_result = await page.evaluate(
        """async ({text, actionType}) => {
          const api = window.SimAgentTerminalSubmission;
          if (!api || typeof api.submit !== 'function') {
            return {
              schema: 'sim.agent_terminal_submission_result.v1',
              status: 'unavailable',
              reason: 'page_terminal_submission_api_unavailable'
            };
          }
          const result = await api.submit({
            schema: 'sim.agent_terminal_submission.v1',
            text,
            action_type: actionType
          });
          if (!result || typeof result !== 'object' || Array.isArray(result)) {
            return {
              schema: 'sim.agent_terminal_submission_result.v1',
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
            "schema": "sim.agent_terminal_submission_result.v1",
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
    result.setdefault("schema", "sim.agent_terminal_submission_result.v1")
    result.setdefault("status", "rejected")
    if result["status"] == "recorded":
        await page.wait_for_timeout(POST_CLICK_SETTLE_MS)
    return {
        "terminal_submission_result": result,
        "last_action_result": {
            "kind": "terminal_answer_submission",
            "status": result["status"],
            "reason": str(result.get("reason") or ""),
            "source": str(result.get("source") or ""),
        },
    }


@dataclass(frozen=True)
class ActionContext:
    """Where an action runs: the environment's app root and browser profile."""

    app_base_url: str
    entry_page: str
    device_scale_factor: float
    scroll_delta: int
    app_aliases: list[str]


@dataclass
class _Observation:
    """The page action an input was attributed to, and which probe observed it."""

    inspected: dict[str, Any] | None = None
    source: str = PREFLIGHT_SOURCE


def _viewport(page: Page) -> tuple[int, int] | None:
    viewport = getattr(page, "viewport_size", None)
    if isinstance(viewport, dict):
        width = viewport.get("width")
        height = viewport.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
    return None


def _to_css(page: Page, x: Any, y: Any, scale: float) -> tuple[int, int]:
    return point_to_css(_viewport(page), x, y, scale)


async def _tap(
    page: Page, action: dict[str, Any], kind: str, scale: float, observed: _Observation
) -> dict[str, Any] | None:
    x, y = _to_css(page, action["x"], action["y"], scale)
    if await is_actionability_preflight_enabled(page):
        inspected = await inspect_actionability(page, x, y, event_type=kind)
        rejected = rejected_actionability_payload(inspected)
        if rejected:
            if kind in TAP_ACTIONS:
                details = inspected if isinstance(inspected, dict) else {}
                logger.warning(
                    "rejected click point=(%s,%s) viewport=%s "
                    "inspected_viewport=%s reason=%s label=%r element_id=%r "
                    "action_id=%r action_kind=%r",
                    x,
                    y,
                    getattr(page, "viewport_size", None),
                    details.get("viewport"),
                    details.get("reason"),
                    details.get("label"),
                    details.get("element_id"),
                    details.get("action_id"),
                    details.get("action_kind"),
                )
            return rejected
        observed.inspected = inspected
    if kind == "long_press":
        await page.mouse.click(x, y, delay=500)
    else:
        await page.mouse.click(x, y, click_count=2 if kind == "double_tap" else 1)
    await wait_for_action_navigation(page, observed.inspected)
    await page.wait_for_timeout(POST_CLICK_SETTLE_MS)
    return None


async def _gesture(
    page: Page,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    wheel: tuple[int, int] | None,
    observed: _Observation,
) -> None:
    """Perform a wheel (``wheel`` offsets) or pointer drag from ``start`` to ``end`` and
    record which gesture host the page attributes it to."""
    (start_x, start_y), (end_x, end_y) = start, end
    previously_satisfied = await read_satisfied_viewport_gesture_action_ids(
        page, gesture_axis=gesture_axis(end_x - start_x, end_y - start_y)
    )
    await page.mouse.move(start_x, start_y)
    if wheel is not None:
        await page.mouse.wheel(wheel[0], wheel[1])
        event_type = "wheel"
    else:
        await page.mouse.down()
        await page.mouse.move(end_x, end_y, steps=10)
        await page.mouse.up()
        event_type = "pointerup"
    await page.wait_for_timeout(POST_GESTURE_SETTLE_MS)
    observed.inspected = await inspect_gesture_actionability(
        page,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        event_type=event_type,
        previously_satisfied_viewport_action_ids=previously_satisfied,
    )
    observed.source = POST_GESTURE_SOURCE


async def _scroll_or_swipe(
    page: Page, action: dict[str, Any], kind: str, context: ActionContext, observed: _Observation
) -> None:
    scale = context.device_scale_factor
    if kind == "swipe" and has_swipe_endpoints(action):
        raw_start_x, raw_start_y, raw_end_x, raw_end_y = swipe_endpoints(action)
        start = _to_css(page, raw_start_x, raw_start_y, scale)
        end = _to_css(page, raw_end_x, raw_end_y, scale)
        # gesture_kind=scroll replays the swipe as a wheel that moves content with the finger.
        wheel = (
            (start[0] - end[0], start[1] - end[1])
            if action.get("gesture_kind") == "scroll"
            else None
        )
        await _gesture(page, start, end, wheel=wheel, observed=observed)
        return
    dx, dy = wheel_offsets(kind, action.get("direction", "down"), context.scroll_delta)
    origin_x, origin_y = DEFAULT_SCROLL_ORIGIN
    x, y = _to_css(
        page, action.get("x", int(origin_x * scale)), action.get("y", int(origin_y * scale)), scale
    )
    await _gesture(page, (x, y), (x + dx, y + dy), wheel=(dx, dy), observed=observed)


async def _type_text(
    page: Page, action: dict[str, Any], kind: str, observed: _Observation
) -> dict[str, Any] | None:
    text = action.get("text", "")
    focused_editable = await inspect_focused_editable(page)
    if not focused_editable or focused_editable.get("editable") is not True:
        return focused_editable_rejection_payload(focused_editable)
    # keyboard.type emits per-key events; non-ASCII text (IME input) is inserted at once.
    if text.isascii():
        await page.keyboard.type(text, delay=50)
    else:
        await page.keyboard.insert_text(text)
    observed.inspected = await inspect_focused_actionability(page, event_type=kind)
    observed.source = FOCUSED_INPUT_SOURCE
    return None


async def _press_key(
    page: Page, action: dict[str, Any], kind: str, observed: _Observation
) -> None:
    key = "Enter" if kind == "keyboard_enter" else action.get("key", "")
    if not key:
        return
    await page.keyboard.press(key)
    await wait_for_action_navigation(page, observed.inspected)
    await page.wait_for_timeout(POST_CLICK_SETTLE_MS)
    if await is_actionability_preflight_enabled(page):
        observed.inspected = await inspect_focused_actionability(
            page, event_type="keyboard_enter" if kind == "keyboard_enter" else "key"
        )
        observed.source = FOCUSED_INPUT_SOURCE


async def execute_action(
    page: Page,
    action: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    """Execute ``action`` in ``page`` and return the step result.

    Raises for malformed coordinates or wait durations and for navigation failures.
    """
    kind = action_type(action)
    scroll_delta = context.scroll_delta
    if isinstance(scroll_delta, bool) or not isinstance(scroll_delta, int) or scroll_delta <= 0:
        raise ValueError("scroll_delta must be a positive integer CSS-pixel distance")
    observed = _Observation()

    if kind in TERMINAL_ACTIONS:
        return await _execute_terminal_action(page, action)
    if kind == "ask_user":
        return await _execute_ask_user(page)
    if kind in OPEN_APP_ACTIONS:
        return open_app_feedback(
            action.get("app_name") or action.get("app") or action.get("value"),
            context.app_aliases,
        )

    if kind in TAP_ACTIONS or kind == "long_press":
        rejected = await _tap(page, action, kind, context.device_scale_factor, observed)
        if rejected:
            return rejected
    elif kind in {"scroll", "swipe"}:
        await _scroll_or_swipe(page, action, kind, context, observed)
    elif kind in TEXT_ACTIONS:
        rejected = await _type_text(page, action, kind, observed)
        if rejected:
            return rejected
    elif kind == "navigate_home":
        await page.goto(f"{context.app_base_url}/{context.entry_page}", wait_until="load")
    elif is_back_action(action):
        boundary_rejection = await _navigate_back_with_app_boundary(page, context.app_base_url)
        if boundary_rejection is not None:
            return boundary_rejection
    elif kind in KEY_ACTIONS:
        await _press_key(page, action, kind, observed)
    elif kind == "wait":
        await page.wait_for_timeout(round(wait_duration_seconds(action) * 1000))
    elif kind == "drag":
        raw_start_x, raw_start_y, raw_end_x, raw_end_y = drag_endpoints(action)
        start = _to_css(page, raw_start_x, raw_start_y, context.device_scale_factor)
        end = _to_css(page, raw_end_x, raw_end_y, context.device_scale_factor)
        await _gesture(page, start, end, wheel=None, observed=observed)
    else:
        logger.warning("Unknown action_type: %s", kind)
        return unknown_action_payload(action)

    inspected = observed.inspected
    if not (inspected and inspected.get("action_id")):
        return {}
    observation = {
        "action_id": inspected.get("action_id"),
        "action_kind": inspected.get("action_kind"),
        "target": inspected.get("target"),
        "source": observed.source,
    }
    if observed.source == FOCUSED_INPUT_SOURCE:
        observation.update(
            {
                "event_type": kind,
                "observed_value": inspected.get("observed_value"),
                "observed_element_tag": inspected.get("observed_element_tag"),
            }
        )
    return {"action_observation": observation}
