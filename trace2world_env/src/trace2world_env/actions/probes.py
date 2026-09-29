"""Read-only page probes run around an action.

The generated apps expose ``window.SimActionability`` and ``data-sim-*`` action hosts. Before a
tap the host asks the page whether the target is interactive; after a gesture or text entry it
asks which declared action the page observed, so the state kernel can record the reward
authority bound to that action. Every probe is best effort: a failing probe returns ``None``.
"""

from __future__ import annotations

import logging
from typing import Any

from playwright.async_api import Page

from trace2world_env.actions.feedback import NONINTERACTIVE_MESSAGE
from trace2world_env.actions.vocabulary import PREFLIGHT_READY_TIMEOUT_MS

logger = logging.getLogger(__name__)


async def wait_for_preflight_ready(page: Page) -> None:
    """Wait (bounded) for the page's ``load`` event before inspecting it."""
    try:
        await page.wait_for_load_state("load", timeout=PREFLIGHT_READY_TIMEOUT_MS)
    except Exception as exc:
        logger.debug("preflight readiness wait skipped: %s", exc)


async def inspect_actionability(
    page: Page,
    x: int,
    y: int,
    *,
    event_type: str,
) -> dict[str, Any] | None:
    """Ask the page what a tap at ``(x, y)`` would hit.

    A tap that lands on a gesture-only host (and on no distinct click host) is reported
    ``noninteractive`` with reason ``gesture_host_requires_gesture``.
    """
    await wait_for_preflight_ready(page)
    try:
        result = await page.evaluate(
            """({ x, y, eventType }) => {
              const api = window.SimActionability;
              if (!api || !api.inspectPoint) return null;
              const inspected = api.inspectPoint(x, y, {
                event_type: eventType,
                source: 'env_playwright_preflight'
              });
              if (!inspected) return null;
              const selected = document.elementFromPoint(x, y);
              const gestureHost = selected && selected.closest
                ? selected.closest('[data-gesture-host], [data-sim-gesture-action]')
                : null;
              const clickHost = selected && selected.closest
                ? selected.closest(
                    '[data-sim-action], [data-sim-route-action], '
                    + '[data-sim-target], [data-sim-effect-kind], '
                    + '[data-sim-action-id], [data-action-id]'
                  )
                : null;
              const clickHostKind = clickHost
                ? ((clickHost.getAttribute('data-sim-action') || '') + ' '
                   + (clickHost.getAttribute('data-sim-effect-kind') || '') + ' '
                   + (clickHost.getAttribute('data-sim-gesture-action') || ''))
                : '';
              const hostActionId = (host) => host
                ? (host.getAttribute('data-sim-action-id') ||
                   host.getAttribute('data-action-id') || '')
                : '';
              const hostActionKind = (host) => host
                ? (host.getAttribute('data-sim-action') ||
                   host.getAttribute('data-sim-effect-kind') ||
                   host.getAttribute('data-sim-gesture-action') ||
                   host.getAttribute('data-sim-route-action') || '')
                : '';
              const distinctClickHost = Boolean(
                clickHost
                && clickHost !== gestureHost
                && !clickHost.hasAttribute('data-gesture-host')
                && !/(?:gesture|swipe)/i.test(clickHostKind)
              );
              return Object.assign({}, inspected, {
                __env_gesture_host_action_id: hostActionId(gestureHost),
                __env_distinct_click_host: distinctClickHost,
                __env_action_host_diagnostics: {
                  schema: 'trace2world.action_host_diagnostics.v1',
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
        logger.debug("actionability inspect failed: %s", exc)
        return None
    if not isinstance(result, dict):
        return None
    gesture_action_id = str(result.pop("__env_gesture_host_action_id", "") or "")
    distinct_click_host = result.pop("__env_distinct_click_host", False) is True
    raw_diagnostics = result.pop("__env_action_host_diagnostics", None)
    if isinstance(raw_diagnostics, dict):
        action_host_diagnostics = {
            "schema": "trace2world.action_host_diagnostics.v1",
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
            "message": NONINTERACTIVE_MESSAGE,
            "action_id": "",
            "action_kind": "",
            "target": "",
            "reason": "gesture_host_requires_gesture",
        }
    return result


async def inspect_focused_actionability(
    page: Page,
    *,
    event_type: str,
) -> dict[str, Any] | None:
    """Inspect the focused element after text entry, with its observed value."""
    try:
        result = await page.evaluate(
            """({ eventType }) => {
              const api = window.SimActionability;
              const focused = document.activeElement;
              if (!api || !api.inspectElement || !focused) return null;
              const inspected = api.inspectElement(focused, {
                event_type: eventType,
                source: 'env_playwright_focused_input'
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
        logger.debug("focused actionability inspect failed: %s", exc)
        return None
    return result if isinstance(result, dict) else None


async def inspect_focused_editable(page: Page) -> dict[str, Any] | None:
    """Whether the focused element accepts text (enabled, writable text input/area/editable)."""
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
        logger.debug("focused editable inspect failed: %s", exc)
        return None
    return result if isinstance(result, dict) else None


async def read_satisfied_viewport_gesture_action_ids(
    page: Page,
    *,
    gesture_axis: str,
) -> list[str]:
    """Action ids of viewport-staged gesture hosts already satisfied before the gesture."""
    try:
        result = await page.evaluate(
            """({ gestureAxis }) => {
              const selector = [
                '[data-gesture-host][data-sim-viewport-target][data-action-id]',
                '[data-gesture-host][data-sim-viewport-target][data-action-aliases]',
                '[data-gesture-host][data-sim-viewport-target][data-sim-action-id]',
                '[data-sim-gesture-action][data-sim-viewport-target][data-action-id]',
                '[data-sim-gesture-action][data-sim-viewport-target][data-action-aliases]',
                '[data-sim-gesture-action][data-sim-viewport-target][data-sim-action-id]'
              ].join(',');
              function stageSatisfied(candidate) {
                const targetSelector = String(
                  candidate.getAttribute('data-sim-viewport-target') || ''
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
                  candidate.getAttribute('data-sim-bottom-occlusion-selector') || ''
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
                  candidate.getAttribute('data-sim-viewport-stage') || 'entered'
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
                const canonical = String(candidate.getAttribute('data-sim-action-id') || '');
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
        logger.debug("pre-gesture viewport stage read failed: %s", exc)
        return []
    if not isinstance(result, list):
        return []
    return sorted({str(value) for value in result if isinstance(value, str) and value})


async def inspect_gesture_actionability(
    page: Page,
    *,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    event_type: str,
    previously_satisfied_viewport_action_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """Find the gesture host the page attributes a completed swipe/scroll/drag to.

    Prefers the host of the current execution-context action, then the spatial host under the
    end/start point when it belongs to the current trace, then any visible host of the trace.
    Viewport-staged hosts already satisfied before the gesture are not reported again.
    """
    delta_x = abs(end_x - start_x)
    delta_y = abs(end_y - start_y)
    gesture_axis = "horizontal" if delta_x > delta_y else "vertical" if delta_y > delta_x else ""
    try:
        result = await page.evaluate(
            """({ startX, startY, endX, endY, eventType, gestureAxis,
                    previouslySatisfiedViewportActionIds }) => {
              const api = window.SimActionability;
              if (!api || !api.inspectElement) return null;
              const selector = [
                '[data-gesture-host][data-action-id]',
                '[data-gesture-host][data-action-aliases]',
                '[data-gesture-host][data-sim-action-id]',
                '[data-sim-gesture-action][data-action-id]',
                '[data-sim-gesture-action][data-action-aliases]',
                '[data-sim-gesture-action][data-sim-action-id]'
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
                  candidate.getAttribute('data-sim-viewport-target') || ''
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
                  candidate.getAttribute('data-sim-bottom-occlusion-selector') || ''
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
                  candidate.getAttribute('data-sim-viewport-stage') || 'entered'
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
                  candidate.getAttribute('data-sim-action-id') || '',
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
              let context = window.__simExecutionContext || null;
              if (!context) {
                try {
                  context = JSON.parse(
                    (window.sessionStorage &&
                      window.sessionStorage.getItem('sim.execution_context')) ||
                    (window.localStorage &&
                      window.localStorage.getItem('sim.execution_context')) ||
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
                source: 'env_playwright_post_gesture'
              });
              const inspectedActionId = String(
                inspected && inspected.action_id || ''
              );
              if (host.getAttribute('data-sim-viewport-target') &&
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
        logger.debug("gesture actionability inspect failed: %s", exc)
        return None
    return result if isinstance(result, dict) else None


async def wait_for_action_navigation(
    page: Page,
    action_observation: dict[str, Any] | None,
) -> None:
    """After a declared ``navigate`` action, wait (bounded) for the target URL."""
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
        logger.debug("navigation settle wait skipped: %s", exc)


async def is_actionability_preflight_enabled(page: Page) -> bool:
    """Whether the page opted into tap preflight (default: enabled)."""
    try:
        enabled = await page.evaluate(
            """() => {
              const values = [
                window.__SimActionabilityPreflight,
                window.localStorage &&
                  window.localStorage.getItem('sim.actionability_preflight'),
                window.sessionStorage &&
                  window.sessionStorage.getItem('sim.actionability_preflight')
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
        logger.debug("actionability preflight flag read failed: %s", exc)
        return True
    return bool(enabled)
