"""Unit tests for the response parser."""

from __future__ import annotations

import json

import pytest

from trace2world_env.agent.parser import decode_tool_call, parse_response

W, H = 1998, 999


def respond(arguments: object, name: str = "mobile_use") -> str:
    body = json.dumps({"name": name, "arguments": arguments})
    return f"<think>\nreason\n</think>\n<tool_call>\n{body}\n</tool_call>"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"action_type": "click", "coordinate": [500, 999]}, {"action_type": "click", "x": 1000, "y": 999}),
        ({"action_type": "long_press", "coordinate": [0, 0]}, {"action_type": "long_press", "x": 0, "y": 0}),
        ({"action_type": "double_click", "coordinate": [999, 1]}, {"action_type": "double_tap", "x": 1998, "y": 1}),
        (
            {"action_type": "drag", "start_coordinate": [1, 2], "end_coordinate": [3, 4]},
            {"action_type": "drag", "start_x": 2, "start_y": 2, "end_x": 6, "end_y": 4},
        ),
        (
            {"action_type": "swipe", "start_coordinate": [1, 2], "end_coordinate": [3, 4]},
            {"action_type": "drag", "start_x": 2, "start_y": 2, "end_x": 6, "end_y": 4},
        ),
        (
            {"action": "scroll", "start_coordinate": [1, 2], "end_coordinate": [3, 4]},
            {"action_type": "drag", "start_x": 2, "start_y": 2, "end_x": 6, "end_y": 4},
        ),
        ({"action_type": "type", "text": "héllo"}, {"action_type": "input_text", "text": "héllo"}),
        ({"action_type": "type"}, {"action_type": "input_text", "text": ""}),
        ({"action_type": "wait", "duration": 2, "seconds": 9}, {"action_type": "wait", "duration": 2}),
        ({"action_type": "wait"}, {"action_type": "wait"}),
        ({"action_type": "terminate", "status": "failed"}, {"action_type": "terminate", "status": "failed"}),
        ({"action_type": "terminate"}, {"action_type": "terminate", "status": "success"}),
        ({"action_type": "terminate", "answer": 42}, {"action_type": "answer", "text": "42"}),
    ],
)
def test_actions_map_to_environment_vocabulary(arguments: dict, expected: dict) -> None:
    parsed = parse_response(respond(arguments), W, H)
    assert parsed.action == expected
    assert parsed.valid
    assert parsed.repair_trigger is None


def test_call_without_think_and_top_level_arguments() -> None:
    parsed = parse_response('<tool_call>{"action_type": "click", "coordinate": [0, 999]}</tool_call>', W, H)
    assert parsed.action == {"action_type": "click", "x": 0, "y": 999}


@pytest.mark.parametrize(
    ("text", "error", "trigger"),
    [
        ("no call here", "No <tool_call> block found", "missing_tool_call_block"),
        ("<tool_call>{bad}</tool_call>", "Expecting property name enclosed in double quotes", "malformed_tool_call_json"),
        ('<tool_call>{"a": 1} {"b": 2}</tool_call>', "Extra data", "malformed_tool_call_json"),
        ("<tool_call>[1]</tool_call>", "Expected JSON object, got <class 'list'>", None),
        ('<tool_call>{"arguments": {}}</tool_call>', "tool_call missing required name", None),
        (respond({"action_type": "click"}, name="browser"), "unsupported tool name: 'browser'", None),
        (respond(["click"]), "Invalid arguments", None),
        (respond({"action_type": "open", "text": "Maps"}), "unsupported action_type: 'open'", None),
        (respond({"action_type": "answer", "text": "x"}), "unsupported action_type: 'answer'", None),
        (respond({"action_type": "click", "coordinate": [1]}), "list index out of range", None),
        (respond({"action_type": ["click"]}), "unhashable type: 'list'", None),
    ],
)
def test_failures_become_unknown_actions(text: str, error: str, trigger: str | None) -> None:
    parsed = parse_response(text, W, H)
    assert parsed.action["action_type"] == "unknown"
    assert parsed.action["error"].startswith(error)
    assert not parsed.valid
    assert parsed.repair_trigger == trigger


def test_unknown_screenshot_size_is_reported() -> None:
    parsed = parse_response(respond({"action_type": "click", "coordinate": [1, 1]}), None, None)
    assert parsed.action == {
        "action_type": "unknown",
        "error": "QwenToolParser needs original_image_width/height",
    }


def test_single_extra_closing_brace_is_repaired() -> None:
    body = '{"name": "mobile_use", "arguments": {"action_type": "wait"}}}'
    assert decode_tool_call(body) == {"name": "mobile_use", "arguments": {"action_type": "wait"}}


def test_terminate_with_unescaped_quotes_is_repaired() -> None:
    body = '{"name": "mobile_use", "arguments": {"action_type": "terminate", "answer": "He said "hi"", "status": "success"}}'
    assert decode_tool_call(body) == {
        "name": "mobile_use",
        "arguments": {"action_type": "terminate", "status": "success", "answer": 'He said "hi"'},
    }
    text = f"<tool_call>{body}</tool_call>"
    assert parse_response(text, W, H).action == {"action_type": "answer", "text": 'He said "hi"'}


def test_truncated_terminate_with_action_key_is_repaired() -> None:
    body = '{"name": "mobile_use", "arguments": {"action": "terminate", "status": "failed"'
    assert decode_tool_call(body) == {
        "name": "mobile_use",
        "arguments": {"action": "terminate", "status": "failed"},
    }


def test_unrepairable_json_raises_decoder_message() -> None:
    with pytest.raises(ValueError, match="^Expecting value"):
        decode_tool_call('{"name": }')
