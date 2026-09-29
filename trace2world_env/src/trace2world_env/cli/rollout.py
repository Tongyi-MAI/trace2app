"""``t2w-rollout``: run the bundled feedback agent on one task and write its trajectory.

After each action the agent sees the environment feedback as a text turn when there is any,
otherwise a fresh screenshot. The episode stops on ``answer`` / ``terminate`` or after
``--max-steps`` actions and is then evaluated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from trace2world_env.agent import (
    TERMINAL_ACTIONS,
    AgentInput,
    ChatClient,
    Feedback,
    FeedbackAgent,
    Screenshot,
)
from trace2world_env.client import EnvironmentClient

TRAJECTORY_SCHEMA = "trace2world.trajectory.v1"


def build_agent(args: argparse.Namespace) -> FeedbackAgent:
    """Create the agent and its model client from command-line options."""
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    chat = ChatClient(
        args.llm_base_url,
        args.model,
        api_key=api_key,
        max_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
    )
    return FeedbackAgent(chat, log_dir=args.messages_dir)


async def rollout(
    env: EnvironmentClient,
    agent: FeedbackAgent,
    task_id: str,
    *,
    max_steps: int,
) -> dict[str, Any]:
    """Run one episode of ``task_id`` and return its ``trace2world.trajectory.v1`` record."""
    known = {str(task.get("task_id") or "") for task in await env.tasks()}
    if task_id not in known:
        raise ValueError(f"unknown task id on host: {task_id}")
    episode = await env.start_episode(task_id)
    try:
        if not episode.instruction:
            raise ValueError(f"task has no instruction: {task_id}")
        agent.reset(episode.instruction)
        steps: list[dict[str, Any]] = []
        observation: AgentInput = Screenshot((await env.observe(episode.episode_id)).screenshot)
        for index in range(1, max_steps + 1):
            step = await agent.act(observation)
            outcome = await env.act(episode.episode_id, step.action)
            steps.append(
                {
                    "step": index,
                    "action": step.action,
                    "response": step.response,
                    "feedback": outcome.feedback,
                    "url": outcome.url,
                }
            )
            if outcome.done or step.action.get("action_type") in TERMINAL_ACTIONS:
                break
            if outcome.feedback:
                observation = Feedback(outcome.feedback)
            else:
                observation = Screenshot((await env.observe(episode.episode_id)).screenshot)
        evaluation = await env.evaluate(episode.episode_id, final=True)
    finally:
        await env.close_episode(episode.episode_id)
    return {
        "schema": TRAJECTORY_SCHEMA,
        "task_id": task_id,
        "model": agent.chat.model,
        "instruction": episode.instruction,
        "steps": steps,
        "reward": float(evaluation.get("reward") or 0.0),
        "evaluation": evaluation,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run the rollout described by ``args``."""
    agent = build_agent(args)
    try:
        async with EnvironmentClient(args.server, timeout=args.timeout) as env:
            return await rollout(env, agent, args.task_id, max_steps=args.max_steps)
    finally:
        await agent.chat.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(prog="t2w-rollout", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--server", default="http://127.0.0.1:6800", help="environment host URL")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--llm-base-url", required=True, help="OpenAI-compatible base URL")
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--api-key-env",
        default="MODEL_API_KEY",
        help="environment variable holding the model API key (falls back to OPENAI_API_KEY)",
    )
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=600.0, help="environment request timeout (s)")
    parser.add_argument("--messages-dir", type=Path, help="write each model step's prompt here")
    parser.add_argument("--output", type=Path, help="write the trajectory JSON here")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point of ``t2w-rollout``."""
    args = parse_args(argv)
    result = asyncio.run(run(args))
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
