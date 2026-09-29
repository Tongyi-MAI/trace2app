"""Parse one model response into an environment action.

The model answers with ``<think>...</think><tool_call>{"name": "mobile_use", "arguments":
{...}}</tool_call>``. Coordinates are on a 0-999 grid and are mapped back to screenshot pixels.
Two common generation slips are repaired silently (one stray closing brace; a ``terminate``
call whose answer contains unescaped quotes). Any other failure yields an ``unknown`` action
whose ``error`` tells the agent whether a format-repair retry is worthwhile.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

TOOL_NAME = "mobile_use"
COORDINATE_SCALE = 999

MODEL_ACTIONS = frozenset({
    "click",
    "double_click",
    "long_press",
    "type",
    "drag",
    "swipe",
    "scroll",
    "wait",
    "terminate",
})
"""Action names the model may emit (the Action Space of the system prompt)."""

TERMINAL_ACTIONS = frozenset({"answer", "terminate"})
"""Environment action types that end an episode."""

_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_NO_TOOL_CALL = "No <tool_call> block found"
_MALFORMED_JSON_PREFIXES = (
    "Expecting property name enclosed in double quotes",
    "Expecting ',' delimiter",
    "Expecting ':' delimiter",
    "Expecting value",
    "Unterminated string",
    "Invalid \\escape",
    "Extra data",
)
_WAIT_KEYS = ("waiting_time", "duration", "seconds")
_POINTER_GESTURES = frozenset({"drag", "swipe", "scroll"})


@dataclass(frozen=True)
class ParsedAction:
    """Result of parsing one response.

    ``action`` is always a dict sent to the environment as-is; on failure it is
    ``{"action_type": "unknown", "error": <reason>}``.
    """

    action: dict[str, Any]

    @property
    def valid(self) -> bool:
        """True when the response contained one well-formed, supported tool call."""
        return self.action.get("action_type") != "unknown"

    @property
    def repair_trigger(self) -> str | None:
        """Why a format-repair retry should be attempted, or None when it should not.

        Only a missing tool call or undecodable tool-call JSON is retried; well-formed calls
        with an unsupported tool or action are passed on to the environment unchanged.
        """
        if self.valid:
            return None
        error = str(self.action.get("error") or "")
        if error == _NO_TOOL_CALL:
            return "missing_tool_call_block"
        if error.startswith(_MALFORMED_JSON_PREFIXES):
            return "malformed_tool_call_json"
        return None


def parse_response(text: str, width: int | None, height: int | None) -> ParsedAction:
    """Parse ``text`` into an action for a screenshot of ``width`` x ``height`` pixels."""
    try:
        arguments = _tool_call_arguments(text)
    except Exception as exc:
        return _failure(str(exc))
    if not isinstance(arguments, dict):
        return _failure("Invalid arguments")
    try:
        return ParsedAction(_to_action(arguments, width, height))
    except Exception as exc:
        return _failure(str(exc))


def _failure(error: str) -> ParsedAction:
    return ParsedAction({"action_type": "unknown", "error": error})


def _tool_call_arguments(text: str) -> Any:
    match = _TOOL_CALL_RE.search(text.strip())
    if not match:
        raise ValueError(_NO_TOOL_CALL)
    call = decode_tool_call(match.group(1).strip())
    if "name" not in call and "action_type" not in call:
        raise ValueError("tool_call missing required name")
    if "name" in call and call.get("name") != TOOL_NAME:
        raise ValueError(f"unsupported tool name: {call.get('name')!r}")
    arguments = call.get("arguments")
    if arguments is None and "action_type" in call:
        arguments = call
    return arguments if arguments is not None else {}


def decode_tool_call(body: str) -> dict[str, Any]:
    """Decode the JSON body of a ``<tool_call>`` block, repairing known slips.

    Raises ``ValueError`` carrying the JSON decoder message when the body cannot be repaired.
    """
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        repaired = _drop_extra_closing_brace(body, exc)
        if repaired is None:
            repaired = _recover_terminate_call(body)
        if repaired is None:
            raise ValueError(str(exc)) from exc
        return repaired
    if not isinstance(decoded, dict):
        raise ValueError(f"Expected JSON object, got {type(decoded)}")
    return decoded


def _drop_extra_closing_brace(body: str, error: json.JSONDecodeError) -> dict[str, Any] | None:
    """Accept ``{...}}``: a complete object followed by exactly one stray ``}``."""
    if error.msg != "Extra data":
        return None
    if body[error.pos :].strip() != "}":
        return None
    try:
        decoded = json.loads(body[: error.pos].rstrip())
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _recover_terminate_call(body: str) -> dict[str, Any] | None:
    """Rebuild a ``terminate`` call whose JSON is broken, typically by quotes in the answer."""
    if re.search(r'"action_type"\s*:\s*"terminate"', body):
        action_key = "action_type"
    elif '"arguments"' in body and re.search(r'"action"\s*:\s*"terminate"', body):
        action_key = "action"
    else:
        return None
    status_match = re.search(r'"status"\s*:\s*"([^"]*)"', body)
    status = (status_match.group(1) if status_match else "success").strip() or "success"
    arguments: dict[str, Any] = {action_key: "terminate", "status": status}
    answer_match = re.search(r'"answer"\s*:\s*"', body)
    if answer_match:
        tail = body[answer_match.end() :]
        answer_end = re.search(r'"\s*,\s*"status"\s*:', tail)
        if answer_end is None:
            answer_end = re.search(r'"\s*}\s*}\s*$', tail)
        if answer_end is not None:
            arguments["answer"] = tail[: answer_end.start()].replace(r"\"", '"')
    return {"name": TOOL_NAME, "arguments": arguments}


def _to_action(arguments: dict[str, Any], width: int | None, height: int | None) -> dict[str, Any]:
    """Map validated tool arguments to the environment action vocabulary."""
    name = arguments.get("action_type", arguments.get("action", ""))
    # Probe set membership before anything else: an unhashable name fails here with a
    # TypeError, and that message is the one reported in the unknown action.
    _ = name in _POINTER_GESTURES
    if width is None or height is None:
        raise ValueError("QwenToolParser needs original_image_width/height")
    if not isinstance(name, str) or name not in MODEL_ACTIONS:
        raise ValueError(f"unsupported action_type: {name!r}")

    def point(coordinate: Any) -> tuple[int, int]:
        return (
            int(float(coordinate[0]) / COORDINATE_SCALE * width),
            int(float(coordinate[1]) / COORDINATE_SCALE * height),
        )

    if name in ("click", "long_press", "double_click"):
        x, y = point(arguments.get("coordinate", [0, 0]))
        return {"action_type": "double_tap" if name == "double_click" else name, "x": x, "y": y}
    if name in _POINTER_GESTURES:
        start_x, start_y = point(arguments.get("start_coordinate", [0, 0]))
        end_x, end_y = point(arguments.get("end_coordinate", [0, 0]))
        return {
            "action_type": "drag",
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
        }
    if name == "type":
        return {"action_type": "input_text", "text": arguments.get("text", "")}
    if name == "terminate":
        answer = arguments.get("answer")
        if answer is not None:
            return {"action_type": "answer", "text": str(answer)}
        return {"action_type": "terminate", "status": arguments.get("status", "success")}
    action: dict[str, Any] = {"action_type": "wait"}
    for key in _WAIT_KEYS:
        if key in arguments:
            action[key] = arguments[key]
            break
    return action
