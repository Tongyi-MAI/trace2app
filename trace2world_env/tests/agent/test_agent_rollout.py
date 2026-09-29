"""Tests of the ``t2w-rollout`` loop with a fake environment and the scripted chat server."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

pytest.importorskip("trace2world_env.client")

from trace2world_env.agent import RELEASE_PIXEL_BUDGET, ChatClient, FeedbackAgent  # noqa: E402
from trace2world_env.cli import rollout as rollout_cli  # noqa: E402
from trace2world_env.client import ActionOutcome, Episode, Observation  # noqa: E402


def png(width: int, height: int) -> str:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def call(arguments: dict[str, Any]) -> str:
    return "<think>ok</think><tool_call>" + json.dumps({"name": "mobile_use", "arguments": arguments}) + "</tool_call>"


@dataclass
class FakeEnvironment:
    """Stands in for ``EnvironmentClient``; answers actions from a list of feedback strings."""

    feedback: list[str | None]
    actions: list[dict[str, Any]] = field(default_factory=list)
    observed: int = 0
    closed: bool = False

    async def tasks(self) -> list[dict[str, Any]]:
        return [{"task_id": "demo-1", "instruction": "Do the thing."}]

    async def start_episode(self, task_id: str) -> Episode:
        return Episode(episode_id="ep-1", task_id=task_id, environment="demo", instruction="Do the thing.")

    async def observe(self, episode_id: str) -> Observation:
        self.observed += 1
        return Observation(
            screenshot=png(1000, 500), url="http://h/app/demo/", page="index.html",
            viewport=(1000, 500), device_scale_factor=1.0,
        )

    async def act(self, episode_id: str, action: dict[str, Any]) -> ActionOutcome:
        self.actions.append(action)
        done = action["action_type"] in {"answer", "terminate"}
        feedback = None if done else self.feedback[len(self.actions) - 1]
        return ActionOutcome(
            accepted=feedback is None, feedback=feedback, done=done, result={},
            url=f"http://h/app/demo/?step={len(self.actions)}", page="index.html",
        )

    async def evaluate(self, episode_id: str, *, final: bool = True) -> dict[str, Any]:
        return {"reward": 1.0, "success": True, "reason": "ok", "details": {}}

    async def close_episode(self, episode_id: str) -> None:
        self.closed = True


def test_rollout_feedback_turns_and_trajectory(chat_server: Any) -> None:
    chat_server.load([
        {"content": call({"action_type": "click", "coordinate": [100, 100]})},
        {"content": call({"action_type": "click", "coordinate": [200, 200]})},
        {"content": call({"action_type": "terminate", "answer": "done"})},
    ])
    env = FakeEnvironment(feedback=["ERROR[NOT_INTERACTIVE] nope.", None])
    agent = FeedbackAgent(ChatClient(chat_server.base_url, "m"))

    async def go() -> dict[str, Any]:
        try:
            return await rollout_cli.rollout(env, agent, "demo-1", max_steps=10)
        finally:
            await agent.chat.aclose()

    trajectory = asyncio.run(go())
    assert trajectory["schema"] == "trace2world.trajectory.v1"
    assert [s["action"]["action_type"] for s in trajectory["steps"]] == ["click", "click", "answer"]
    assert trajectory["steps"][0] == {
        "step": 1,
        "action": {"action_type": "click", "x": 100, "y": 50},
        "response": call({"action_type": "click", "coordinate": [100, 100]}),
        "feedback": "ERROR[NOT_INTERACTIVE] nope.",
        "url": "http://h/app/demo/?step=1",
    }
    assert trajectory["reward"] == 1.0 and trajectory["instruction"] == "Do the thing."
    assert env.closed and env.observed == 2  # initial screenshot + after the accepted click

    second = chat_server.requests[1].json["messages"][-1]
    assert second == {
        "role": "user",
        "content": [{"type": "text", "text": "Environment feedback after the previous action:\nERROR[NOT_INTERACTIVE] nope."}],
    }
    third = chat_server.requests[2].json["messages"][-1]
    assert third["content"][0]["type"] == "image_url"


def test_rollout_stops_at_max_steps(chat_server: Any) -> None:
    chat_server.load([{"content": call({"action_type": "wait"})}] * 2)
    env = FakeEnvironment(feedback=[None, None])
    agent = FeedbackAgent(ChatClient(chat_server.base_url, "m"))

    async def go() -> dict[str, Any]:
        try:
            return await rollout_cli.rollout(env, agent, "demo-1", max_steps=2)
        finally:
            await agent.chat.aclose()

    trajectory = asyncio.run(go())
    assert len(trajectory["steps"]) == 2 and env.closed


def test_unknown_task_is_rejected(chat_server: Any) -> None:
    agent = FeedbackAgent(ChatClient(chat_server.base_url, "m"))
    with pytest.raises(ValueError, match="unknown task id"):
        asyncio.run(rollout_cli.rollout(FakeEnvironment(feedback=[]), agent, "nope", max_steps=1))


def test_cli_options_and_messages_dir(tmp_path: Path) -> None:
    args = rollout_cli.parse_args([
        "--task-id", "demo-1", "--llm-base-url", "http://m/v1", "--model", "m",
        "--messages-dir", str(tmp_path / "msgs"),
    ])
    assert (args.server, args.max_steps, args.max_new_tokens, args.temperature, args.top_p) == (
        "http://127.0.0.1:6800", 50, 2048, 0.0, 1.0,
    )
    agent = rollout_cli.build_agent(args)
    assert agent.chat.pixel_budget == RELEASE_PIXEL_BUDGET
    assert agent.log_dir == tmp_path / "msgs"
    asyncio.run(agent.chat.aclose())
