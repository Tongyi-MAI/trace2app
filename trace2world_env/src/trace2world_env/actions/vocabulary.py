"""The action vocabulary accepted by the host and the pure normalisation of action fields.

Actions are JSON objects with an ``action_type`` and type-specific fields. Coordinates are
device pixels of the screenshot (divided by the device scale factor to get CSS pixels) unless
both lie in ``[0, 1]``, in which case they are fractions of the viewport.

=====================  ==========================================================
``action_type``        fields
=====================  ==========================================================
click, double_tap      x, y
long_press             x, y
swipe                  direction, or start_x/start_y/end_x/end_y, or
                       start_coordinate/end_coordinate (+ optional gesture_kind)
scroll                 direction (default ``down``), optional x, y
drag                   start_x, start_y, end_x, end_y
type, input_text       text (typed into the focused editable element)
key, keyboard_enter    key
navigate_home          -
navigate_back          - (aliases: system_back, system_button with button=back)
wait                   waiting_time | duration | seconds (default 1 s, capped at 30 s)
open_app, awake        app_name | app | value
ask_user               -
answer, terminate      text | answer (aliases: finished, status); ends the episode
=====================  ==========================================================
"""

from __future__ import annotations

import math
from typing import Any

#: CSS pixels moved by one ``scroll``/``swipe`` step unless the environment overrides it.
SCROLL_DELTA = 200
POST_CLICK_SETTLE_MS = 50
POST_GESTURE_SETTLE_MS = 50
PREFLIGHT_READY_TIMEOUT_MS = 10000
DEFAULT_WAIT_SECONDS = 1.0
MAX_WAIT_SECONDS = 30.0
#: Screen point (in CSS pixels, before scaling) a ``scroll`` without x/y starts from.
DEFAULT_SCROLL_ORIGIN = (187, 406)

TERMINAL_ACTIONS = frozenset({"answer", "terminate", "finished", "status"})
TAP_ACTIONS = frozenset({"click", "double_tap"})
TEXT_ACTIONS = frozenset({"type", "input_text"})
KEY_ACTIONS = frozenset({"key", "keyboard_enter"})
OPEN_APP_ACTIONS = frozenset({"open_app", "awake"})

#: ``action_observation.source`` values reported for the three observation points.
PREFLIGHT_SOURCE = "env_playwright_preflight"
POST_GESTURE_SOURCE = "env_playwright_post_gesture"
FOCUSED_INPUT_SOURCE = "env_playwright_focused_input"


def action_type(action: dict[str, Any]) -> str:
    """The action's ``action_type`` as a string (empty when absent)."""
    return str(action.get("action_type") or "")


def is_terminal(action: dict[str, Any]) -> bool:
    """Whether the action ends the episode (``answer``/``terminate`` and aliases)."""
    return action_type(action) in TERMINAL_ACTIONS


def is_back_action(action: dict[str, Any]) -> bool:
    """Whether the action is a system/in-app back navigation."""
    kind = action_type(action)
    if kind in {"navigate_back", "system_back"}:
        return True
    return kind == "system_button" and action.get("button") == "back"


def to_css(val: int | float, scale: float) -> int:
    """Convert one device-pixel coordinate to CSS pixels."""
    return int(val / scale)


def point_to_css(
    viewport: tuple[int, int] | None,
    x: int | float,
    y: int | float,
    scale: float,
) -> tuple[int, int]:
    """Convert a screenshot point to CSS pixels.

    Points with both coordinates in ``[0, 1]`` are viewport fractions; others are device
    pixels divided by ``scale``.
    """
    fx, fy = float(x), float(y)
    if viewport is not None and 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
        width, height = viewport
        return int(fx * width), int(fy * height)
    return to_css(fx, scale), to_css(fy, scale)


def gesture_axis(dx: int, dy: int) -> str:
    """``horizontal``/``vertical`` by the dominant displacement, ``""`` on a tie."""
    if abs(dx) > abs(dy):
        return "horizontal"
    if abs(dy) > abs(dx):
        return "vertical"
    return ""


def has_swipe_endpoints(action: dict[str, Any]) -> bool:
    """Whether a ``swipe`` carries explicit start/end points instead of a direction."""
    return ("start_x" in action and "start_y" in action) or (
        "start_coordinate" in action and "end_coordinate" in action
    )


def swipe_endpoints(action: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """Raw ``(start_x, start_y, end_x, end_y)`` of an endpoint swipe."""
    if "start_coordinate" in action and "end_coordinate" in action:
        start = action.get("start_coordinate") or [0, 0]
        end = action.get("end_coordinate") or start
        return start[0], start[1], end[0], end[1]
    return drag_endpoints(action)


def drag_endpoints(action: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """Raw ``(start_x, start_y, end_x, end_y)``; a missing end defaults to the start."""
    start_x = action.get("start_x", action.get("x", 0))
    start_y = action.get("start_y", action.get("y", 0))
    return start_x, start_y, action.get("end_x", start_x), action.get("end_y", start_y)


def wheel_offsets(kind: str, direction: Any, delta: int) -> tuple[int, int]:
    """Mouse-wheel ``(dx, dy)`` for a directional ``scroll`` or ``swipe``.

    ``scroll`` moves the content view in ``direction``; ``swipe`` moves the finger, so the
    content scrolls the opposite way.
    """
    sign = 1 if kind == "scroll" else -1
    offsets = {
        "down": (0, delta),
        "up": (0, -delta),
        "right": (delta, 0),
        "left": (-delta, 0),
    }
    dx, dy = offsets.get(direction, (0, 0)) if isinstance(direction, str) else (0, 0)
    return sign * dx, sign * dy


def wait_duration_seconds(action: dict[str, Any]) -> float:
    """Wait length in seconds, capped at :data:`MAX_WAIT_SECONDS`.

    Raises ``ValueError`` for a boolean, non-numeric, negative, or non-finite duration.
    """
    raw_duration = next(
        (
            action[key]
            for key in ("waiting_time", "duration", "seconds")
            if key in action
        ),
        DEFAULT_WAIT_SECONDS,
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
    return min(duration, MAX_WAIT_SECONDS)


def terminal_answer_text(action: dict[str, Any]) -> str:
    """The stripped answer text of a terminal action (``text`` then ``answer``)."""
    for key in ("text", "answer"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
