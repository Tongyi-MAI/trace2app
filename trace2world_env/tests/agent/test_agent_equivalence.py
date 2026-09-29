"""Old-vs-new agent equivalence against a scripted chat server.

The agent implementation of the previous release and ``trace2world_env.agent.FeedbackAgent``
are fed the same observation sequence and the same scripted model replies. Every chat request
body must be byte-identical and every produced action identical.

The previous agent is driven by a helper outside this repository (``T2W_PREVIOUS_AGENT_DRIVER``,
a Python file exposing ``release_policy``, ``run`` and ``SYSTEM_PROMPT``); the test is skipped
when it is not available.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from PIL import Image, ImageDraw

from trace2world_env.agent import ChatClient, Feedback, FeedbackAgent, Screenshot

DRIVER = Path(os.environ.get("T2W_PREVIOUS_AGENT_DRIVER", ""))
pytestmark = pytest.mark.skipif(
    not DRIVER.is_file(),
    reason="set T2W_PREVIOUS_AGENT_DRIVER to the previous-agent driver to run this test",
)

INSTRUCTION = "Book the cheapest flight to 北京 and report the \"total\" price."
MODEL = "scripted-model"


@pytest.fixture(scope="module")
def previous() -> ModuleType:
    """Load the previous-agent driver."""
    spec = importlib.util.spec_from_file_location("previous_agent_driver", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png(width: int, height: int, seed: int) -> str:
    """A distinct, cheaply compressible PNG screenshot as base64."""
    image = Image.new("RGB", (width, height), ((seed * 40) % 256, (seed * 90) % 256, 200))
    draw = ImageDraw.Draw(image)
    for i in range(0, max(width, height), 37):
        draw.line([(i, 0), (0, i)], fill=((i + seed) % 256, 30, 90), width=2)
    draw.rectangle([width // 4, height // 4, width // 2, height // 2], fill=(255, 255, 255))
    draw.text((10, 10), f"screen {seed} {width}x{height}", fill=(0, 0, 0))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def call(arguments: dict[str, Any], think: str | None = "step") -> str:
    body = json.dumps({"name": "mobile_use", "arguments": arguments}, ensure_ascii=False)
    prefix = f"<think>\n{think}\n</think>\n" if think is not None else ""
    return f"{prefix}<tool_call>\n{body}\n</tool_call>"


def raw_call(body: str, think: str = "step") -> str:
    return f"<think>\n{think}\n</think>\n<tool_call>\n{body}\n</tool_call>"


LONG_FEEDBACK = "  ERROR[NOT_INTERACTIVE] " + "x" * 5000 + "  "

# (observation, replies consumed by that observation). Observation: ("image", b64) or
# ("text", feedback).
MAIN_SEQUENCE: list[tuple[tuple[str, str], list[dict[str, Any]]]] = [
    (("image", png(1280, 720, 1)), [{"content": call({"action_type": "click", "coordinate": [500, 250]})}]),
    (
        ("text", "ERROR[NOT_INTERACTIVE] The clicked element is not interactive."),
        [{"content": call({"action_type": "double_click", "coordinate": [999, 0]})}],
    ),
    (("image", png(1920, 1080, 2)), [{"content": call({"action_type": "long_press", "coordinate": [1, 998]}, think=None)}]),
    (
        ("image", png(390, 844, 3)),
        [{
            "content": call({"action_type": "type", "text": "北京 café \"quoted\""}, think=None),
            "reasoning_content": "The search box is focused, so type.",
        }],
    ),
    (("text", LONG_FEEDBACK), [{"content": call({"action_type": "drag", "start_coordinate": [100, 900], "end_coordinate": [100, 100]})}]),
    (("image", png(1024, 768, 4)), [{"content": call({"action_type": "swipe", "start_coordinate": [500, 800], "end_coordinate": [500, 200]})}]),
    (("image", png(800, 600, 5)), [{"content": call({"action": "scroll", "start_coordinate": [10, 20], "end_coordinate": [30, 40]})}]),
    (("image", png(1280, 720, 6)), [{"content": call({"action_type": "wait", "waiting_time": "2"})}]),
    (
        ("image", png(1366, 768, 7)),
        [
            {"content": raw_call('{"name": "mobile_use", "arguments": {action_type: "click", "coordinate": [1, 2]}}')},
            {"content": call({"action_type": "click", "coordinate": [321, 654]}, think="retry ok")},
        ],
    ),
    (
        ("text", "ERROR[FORMAT_INVALID] The previous response was not a valid tool call."),
        [
            {"content": "I think I should click the button.", "finish_reason": "length"},
            {"content": "<think>still no call</think>Clicking now."},
        ],
    ),
    (("image", ""), [{"content": raw_call('{"name": "mobile_use", "arguments": {"action_type": "click", "coordinate": [10, 20]}}}')}]),
    (
        ("text", "OK"),
        [{"content": raw_call('{"name": "mobile_use", "arguments": {"action_type": "terminate", "answer": "He said "hi" today", "status": "success"}}')}],
    ),
    (("image", png(1440, 900, 8)), [{"content": call({"action_type": "click", "coordinate": [5, 5]}).replace("mobile_use", "browser")}]),
    (("image", png(1440, 900, 9)), [{"content": call({"action_type": "open", "text": "Maps"})}]),
    (
        ("text", "  ERROR[NO_EDITABLE_FOCUS] No editable element is focused.  "),
        [
            {"content": raw_call('{"name": "mobile_use", "arguments": {"action_type": "wait"}} {"name": "mobile_use"}')},
            {"content": call({"action_type": "terminate", "status": "failed"}), "finish_reason": "length"},
        ],
    ),
    (("image", png(600, 1200, 10)), [{"content": raw_call('{"action_type": "wait", "duration": 3}')}]),
    (("image", png(640, 480, 11)), [{"content": raw_call('{"name": "mobile_use", "arguments": ["click"]}')}]),
    (("image", png(640, 480, 12)), [{"content": call({"action_type": "click", "coordinate": [5]})}]),
    (("image", png(640, 480, 13)), [{"content": call({"action_type": ["click"], "coordinate": [5, 5]})}]),
    (
        ("image", png(1600, 1000, 14)),
        [{"content": "<think>inline</think>" + call({"action_type": "click", "coordinate": [700, 300]}, think=None), "reasoning": "ignored"}],
    ),
    (("image", png(1600, 1000, 15)), [{"content": raw_call("[1, 2]")}]),
    (("image", png(1600, 1000, 16)), [{"content": raw_call('{"arguments": {"action_type": "click"}}')}]),
    (("image", png(1600, 1000, 17)), [{"content": raw_call('{"name": "mobile_use", "arguments": {"action": "terminate", "status": "failed"')}]),
    (("image", png(1600, 1000, 18)), [{"content": "", "finish_reason": "length"}, {"content": call({"action_type": "type", "text": ""})}]),
    (("image", png(1280, 720, 19)), [{"content": call({"action_type": "terminate", "answer": "The total is $412.", "status": "success"})}]),
]

# No screenshot yet, feedback before the first screenshot, an undecodable image.
EDGE_SEQUENCE: list[tuple[tuple[str, str], list[dict[str, Any]]]] = [
    (("image", ""), []),
    (("text", "ERROR[INVALID_ACTION] Unsupported action."), []),
    (("image", base64.b64encode(b"not an image").decode()), [{"content": call({"action_type": "click", "coordinate": [1, 1]})}]),
    (("image", png(1280, 720, 20)), [{"content": call({"action_type": "click", "coordinate": [1, 1]})}]),
    (("text", "true"), [{"content": call({"action_type": "wait"})}]),
]


def new_observation(kind: str, value: str) -> Screenshot | Feedback:
    return Screenshot(value) if kind == "image" else Feedback(value)


def script_of(sequence: list[tuple[tuple[str, str], list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    return [reply for _, replies in sequence for reply in replies]


async def run_new(agent: FeedbackAgent, sequence: list, instruction: str) -> list[dict[str, Any]]:
    agent.reset(instruction)
    actions = [(await agent.act(new_observation(*obs))).action for obs, _ in sequence]
    await agent.chat.aclose()
    return actions


def drive(
    chat_server: Any, previous: ModuleType, sequence: list, old_policy: Any, new_agent: Any
) -> tuple[list, list, list, list]:
    """Run the previous and the new agent over ``sequence``; return actions and requests."""
    observations = [obs for obs, _ in sequence]
    chat_server.load(script_of(sequence))
    old_actions = asyncio.run(previous.run(old_policy(), observations, INSTRUCTION))
    old_requests = list(chat_server.requests)
    chat_server.load(script_of(sequence))
    new_actions = asyncio.run(run_new(new_agent(), sequence, INSTRUCTION))
    new_requests = list(chat_server.requests)
    return old_actions, new_actions, old_requests, new_requests


def assert_same_requests(old_requests: list, new_requests: list, expected: int) -> None:
    assert len(old_requests) == expected
    assert len(new_requests) == expected
    for index, (old, new) in enumerate(zip(old_requests, new_requests, strict=True)):
        assert old.path == new.path, index
        assert old.headers.get("authorization") == new.headers.get("authorization"), index
        canonical_old = json.dumps(old.json, sort_keys=True, ensure_ascii=False)
        canonical_new = json.dumps(new.json, sort_keys=True, ensure_ascii=False)
        assert canonical_old == canonical_new, f"request {index} differs"
        assert old.body == new.body, f"request {index} raw bytes differ"


@pytest.mark.parametrize(
    ("temperature", "top_p", "max_new_tokens", "api_key"),
    [(0.0, 1.0, 2048, ""), (0.7, 0.9, 512, "sk-test")],
)
def test_release_agent_matches_previous_agent(
    chat_server: Any,
    previous: ModuleType,
    temperature: float,
    top_p: float,
    max_new_tokens: int,
    api_key: str,
) -> None:
    def old_policy() -> Any:
        return previous.release_policy(
            chat_server.base_url,
            MODEL,
            api_key=api_key or "EMPTY",
            temperature=temperature,
            top_p=top_p,
            max_new_tokens=max_new_tokens,
        )

    def new_agent() -> FeedbackAgent:
        chat = ChatClient(
            chat_server.base_url,
            MODEL,
            api_key=api_key,
            max_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        return FeedbackAgent(chat)

    for sequence in (MAIN_SEQUENCE, EDGE_SEQUENCE):
        old_actions, new_actions, old_requests, new_requests = drive(
            chat_server, previous, sequence, old_policy, new_agent
        )
        assert new_actions == old_actions
        assert_same_requests(old_requests, new_requests, len(script_of(sequence)))

    # The scripted conversation exercises every behaviour it is meant to cover.
    chat_server.load(script_of(MAIN_SEQUENCE))
    actions = asyncio.run(run_new(new_agent(), MAIN_SEQUENCE, INSTRUCTION))
    types = [action["action_type"] for action in actions]
    assert {"click", "double_tap", "long_press", "input_text", "drag", "wait", "answer", "terminate"} <= set(types)
    assert any(action.get("error") == "format_repair_failed" for action in actions)
    assert sum(1 for action in actions if action["action_type"] == "unknown") >= 6
    bodies = [request.json for request in chat_server.requests]
    assert all(body["messages"][0]["content"] == previous.SYSTEM_PROMPT for body in bodies)
    assert max(
        sum(1 for m in body["messages"] if isinstance(m["content"], list) and m["content"][0]["type"] == "image_url")
        for body in bodies
    ) == 3
