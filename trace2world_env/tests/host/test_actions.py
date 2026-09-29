"""Action normalisation and the agent-visible feedback payloads."""

from __future__ import annotations

import pytest

from trace2world_env.actions.feedback import (
    BACK_BOUNDARY_MESSAGE,
    back_boundary_payload,
    feedback_text,
    focused_editable_rejection_payload,
    open_app_feedback,
    rejected_actionability_payload,
    resolve_app_aliases,
    unknown_action_payload,
)
from trace2world_env.actions.vocabulary import (
    drag_endpoints,
    gesture_axis,
    has_swipe_endpoints,
    is_back_action,
    is_terminal,
    point_to_css,
    swipe_endpoints,
    terminal_answer_text,
    wait_duration_seconds,
    wheel_offsets,
)

NOT_INTERACTIVE = "ERROR[NOT_INTERACTIVE]\nNo state change. Choose another target."
NO_EDITABLE_FOCUS = (
    "ERROR[NO_EDITABLE_FOCUS]\nNo state change. Click an editable field before typing."
)
INVALID_ACTION = "ERROR[INVALID_ACTION]\nNo state change. Choose a supported action."
FORMAT_INVALID = (
    "ERROR[FORMAT_INVALID]\nYour action could not be parsed. Emit exactly one valid tool call."
)


def test_point_to_css_device_pixels_and_fractions() -> None:
    assert point_to_css((430, 884), 500, 1001, 2.0) == (250, 500)
    assert point_to_css((430, 884), 0.5, 0.25, 2.0) == (215, 221)
    # fractions need a known viewport; otherwise they are device pixels
    assert point_to_css(None, 0.5, 0.25, 2.0) == (0, 0)
    assert point_to_css((430, 884), 1, 2, 2.0) == (0, 1)


def test_wheel_offsets_scroll_versus_swipe() -> None:
    assert wheel_offsets("scroll", "down", 200) == (0, 200)
    assert wheel_offsets("scroll", "left", 200) == (-200, 0)
    assert wheel_offsets("swipe", "down", 200) == (0, -200)
    assert wheel_offsets("swipe", "right", 150) == (-150, 0)
    assert wheel_offsets("scroll", "diagonal", 200) == (0, 0)
    assert wheel_offsets("scroll", ["down"], 200) == (0, 0)


def test_gesture_axis() -> None:
    assert gesture_axis(10, -3) == "horizontal"
    assert gesture_axis(0, -30) == "vertical"
    assert gesture_axis(5, -5) == ""


def test_swipe_and_drag_endpoints() -> None:
    swipe = {"action_type": "swipe", "start_coordinate": [10, 20], "end_coordinate": [30, 40]}
    assert has_swipe_endpoints(swipe)
    assert swipe_endpoints(swipe) == (10, 20, 30, 40)
    assert swipe_endpoints({"start_coordinate": [1, 2], "end_coordinate": None}) == (1, 2, 1, 2)
    assert swipe_endpoints({"start_x": 1, "start_y": 2, "end_y": 9}) == (1, 2, 1, 9)
    assert not has_swipe_endpoints({"action_type": "swipe", "direction": "up"})
    assert drag_endpoints({"x": 5, "y": 6}) == (5, 6, 5, 6)


def test_wait_duration() -> None:
    assert wait_duration_seconds({}) == 1.0
    assert wait_duration_seconds({"waiting_time": 2}) == 2.0
    assert wait_duration_seconds({"seconds": 90}) == 30.0
    for bad in ({"duration": True}, {"duration": "soon"}, {"duration": -1}, {"duration": "nan"}):
        with pytest.raises(ValueError, match="finite non-negative"):
            wait_duration_seconds(bad)


def test_terminal_and_back_detection() -> None:
    for kind in ("answer", "terminate", "finished", "status"):
        assert is_terminal({"action_type": kind})
    assert not is_terminal({"action_type": "click"})
    assert terminal_answer_text({"text": "  ", "answer": " 42 "}) == "42"
    assert is_back_action({"action_type": "system_back"})
    assert is_back_action({"action_type": "system_button", "button": "back"})
    assert not is_back_action({"action_type": "system_button", "button": "home"})


def test_unknown_action_messages() -> None:
    parse_error = unknown_action_payload({"action_type": "unknown", "error": "bad json"})
    assert parse_error["env_text"] == FORMAT_INVALID
    assert parse_error["actionability_rejected"] is True
    assert parse_error["last_action_result"]["kind"] == "parser_format"
    unsupported = unknown_action_payload({"action_type": "fly"})
    assert unsupported["env_text"] == INVALID_ACTION
    assert unsupported["actionability_feedback"]["result"]["error_code"] == "INVALID_ACTION"
    explicit = unknown_action_payload(
        {"action_type": "unknown", "error": "unsupported action_type: x"}
    )
    assert explicit["env_text"] == INVALID_ACTION
    assert unsupported["actionability_feedback"]["schema"] == "trace2world.action_rejected.v1"


def test_open_app_feedback() -> None:
    aliases = resolve_app_aliases(None, "demo")
    assert aliases == ["demo"]
    satisfied = open_app_feedback(" DEMO ", aliases)
    assert satisfied["actionability_rejected"] is False
    assert satisfied["env_text"] == "Already in demo; open_app was satisfied without navigation."
    missing = open_app_feedback("", aliases)
    assert missing["env_text"] == "No app name was supplied; the current app was left unchanged."
    other = open_app_feedback("Maps", resolve_app_aliases(["钉钉", " DingTalk "], "dingtalk"))
    assert other["env_text"] == (
        "App 'Maps' is not available in this environment. Available app: 钉钉, DingTalk."
    )
    assert other["actionability_feedback"]["result"]["status"] == "unsupported"


def test_back_boundary_and_focus_rejections() -> None:
    assert back_boundary_payload()["env_text"] == BACK_BOUNDARY_MESSAGE
    assert BACK_BOUNDARY_MESSAGE == (
        "System back would leave the current app. The current page was kept. "
        "Use a visible in-app back control instead."
    )
    no_focus = focused_editable_rejection_payload(
        {"editable": False, "observed_element_tag": "div"}
    )
    assert no_focus["env_text"] == NO_EDITABLE_FOCUS
    assert no_focus["last_action_result"]["observed_element_tag"] == "div"
    assert focused_editable_rejection_payload(None)["actionability_feedback"]["result"][
        "reason"
    ] == "focused_editable_probe_unavailable"


def test_rejected_actionability_payload() -> None:
    assert rejected_actionability_payload(None) is None
    assert rejected_actionability_payload({"status": "interactive"}) is None
    rejected = rejected_actionability_payload(
        {
            "status": "noninteractive",
            "reason": "no_host",
            "schema": "x",
            "label": "dropped",
            "action_host_diagnostics": {"selected_tag": "button", "click_host_action_id": ""},
        }
    )
    assert rejected is not None
    assert rejected["env_text"] == NOT_INTERACTIVE
    assert rejected["environment_suspect"] is True
    assert rejected["actionability_feedback"]["schema"] == "sim.actionability.env_step.v1"
    assert "label" not in rejected["actionability_feedback"]["result"]
    gesture = rejected_actionability_payload(
        {"status": "noninteractive", "reason": "gesture_host_requires_gesture"}
    )
    assert gesture is not None and gesture["environment_suspect"] is False


def test_feedback_text_priority() -> None:
    assert feedback_text({}) is None
    assert feedback_text({"env_text": "a", "actionability_feedback": {"message": "b"}}) == "a"
    assert feedback_text({"actionability_feedback": {"message": "b"}}) == "b"
    assert feedback_text({"actionability_feedback": {"result": {"message": "c"}}}) == "c"
    assert feedback_text({"action_observation": {"action_id": "x"}}) is None
