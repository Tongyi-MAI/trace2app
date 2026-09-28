
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from trace2world.agent import create_policy
from trace2world.schema.config import AgentConfig


TERMINAL_ACTIONS = frozenset({"answer", "terminate", "finished", "status"})


def _checked(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


async def _screenshot(client: httpx.AsyncClient, display_id: int) -> dict[str, Any]:
    payload = _checked(
        await client.get(
            "/screenshot",
            params={"display_id": display_id, "return_b64": "true"},
        )
    )
    return {
        "observation_kind": "image",
        "image": str(payload.get("b64_png") or ""),
        "current_url": payload.get("current_url"),
        "page_filename": payload.get("page_filename"),
        "viewport": payload.get("viewport"),
    }


def _feedback_observation(step: dict[str, Any]) -> dict[str, Any] | None:
    feedback = step.get("env_text")
    structured = step.get("actionability_feedback")
    if not feedback and isinstance(structured, dict):
        feedback = structured.get("message")
        if not feedback and isinstance(structured.get("result"), dict):
            feedback = structured["result"].get("message")
    if not feedback:
        return None
    return {
        "observation_kind": "text",
        "env_text": str(feedback),
        "actionability_feedback": structured,
        "current_url": step.get("current_url"),
        "page_filename": step.get("page_filename"),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get(args.api_key_env, "EMPTY") if args.api_key_env else "EMPTY"
    config = AgentConfig(
        type="trace2world_env_text",
        model_name=args.model,
        llm_base_url=args.llm_base_url,
        llm_backend="http",
        api_key=api_key,
        max_steps=args.max_steps,
        runtime_conf={
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_new_tokens": args.max_new_tokens,
            "messages_log_dir": str(args.messages_dir) if args.messages_dir else None,
        },
    )
    policy = create_policy(config)
    display_id: int | None = None
    trajectory: dict[str, Any] = {
        "schema": "trace2world.direct_text_feedback_trajectory.v1",
        "task_id": args.task_id,
        "agent": config.type,
        "model": args.model,
        "steps": [],
    }
    async with httpx.AsyncClient(base_url=args.server.rstrip("/"), timeout=args.timeout) as client:
        tasks = _checked(await client.get("/tasks")).get("tasks", [])
        task = next(
            (
                row
                for row in tasks
                if str(row.get("task_id") or row.get("id") or "") == args.task_id
            ),
            None,
        )
        if task is None:
            raise ValueError(f"unknown task id on host: {args.task_id}")
        acquired = _checked(await client.post("/display/acquire"))
        display_id = int(acquired["logical_id"])
        try:
            _checked(
                await client.post(
                    "/task/init",
                    json={"task_name": args.task_id, "display_id": display_id},
                )
            )
            goal = _checked(
                await client.get(
                    "/task/goal",
                    params={"task_name": args.task_id, "display_id": display_id},
                )
            )
            goal = str(goal or task.get("instruction") or "")
            if not goal:
                raise ValueError(f"task has no instruction: {args.task_id}")
            trajectory["goal"] = goal
            policy.initialize(goal)
            observation = await _screenshot(client, display_id)

            for step_index in range(1, args.max_steps + 1):
                action = await policy.select_action(observation)
                step_result = _checked(
                    await client.post(
                        "/step",
                        json={"display_id": display_id, "action": action},
                    )
                )
                trajectory["steps"].append(
                    {
                        "step": step_index,
                        "action": action,
                        "model_response": getattr(policy, "last_prediction", None),
                        "environment_feedback": step_result.get("env_text"),
                        "actionability_feedback": step_result.get(
                            "actionability_feedback"
                        ),
                        "current_url": step_result.get("current_url"),
                    }
                )
                if str(action.get("action_type") or "") in TERMINAL_ACTIONS:
                    break
                feedback = _feedback_observation(step_result)
                observation = feedback or await _screenshot(client, display_id)

            evaluation = _checked(
                await client.get(
                    "/task/eval",
                    params={
                        "task_name": args.task_id,
                        "display_id": display_id,
                        "preview": "false",
                    },
                )
            )
            trajectory["reward"] = float(
                evaluation.get("raw_reward", evaluation.get("score", 0.0)) or 0.0
            )
            trajectory["evaluation"] = evaluation
            policy.done()
        finally:
            if display_id is not None:
                try:
                    await client.post(
                        "/task/tear_down",
                        json={"task_name": args.task_id, "display_id": display_id},
                    )
                finally:
                    await client.post(
                        "/display/release", params={"display_id": display_id}
                    )
    chat_model = getattr(policy, "chat_model", None)
    if chat_model is not None and hasattr(chat_model, "aclose"):
        await chat_model.aclose()
    return trajectory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:6800")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--llm-base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", default="MODEL_API_KEY")
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--messages-dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = asyncio.run(run(args))
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
