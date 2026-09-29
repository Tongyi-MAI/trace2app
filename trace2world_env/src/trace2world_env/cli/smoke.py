"""``t2w-smoke``: run one short episode per environment against a running host.

For the first task of every environment (or every task with ``--all-tasks``) it starts an
episode, checks the screenshot is a PNG under ``/app/<environment>/``, sends an unsupported
action and expects text feedback, evaluates, and closes the episode.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from trace2world_env.client import EnvironmentClient

SMOKE_ACTION = {"action_type": "unknown", "error": "standalone_runtime_smoke"}


def _is_png(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return base64.b64decode(value, validate=True).startswith(b"\x89PNG\r\n\x1a\n")
    except ValueError:
        return False


async def smoke_task(
    client: EnvironmentClient, environment: str, task_id: str
) -> dict[str, Any]:
    """Run the smoke lifecycle for one task and report per-check results."""
    started = time.monotonic()
    episode_id: str | None = None
    try:
        episode = await client.start_episode(task_id)
        episode_id = episode.episode_id
        observation = await client.observe(episode_id)
        outcome = await client.act(episode_id, SMOKE_ACTION)
        evaluation = await client.evaluate(episode_id, final=True)
        reward = evaluation.get("reward")
        checks = {
            "environment_selected": episode.environment == environment,
            "instruction_nonempty": bool(episode.instruction.strip()),
            "screenshot_png": _is_png(observation.screenshot),
            "app_route_selected": f"/app/{environment}/" in observation.url,
            "text_feedback_returned": bool(outcome.feedback) and not outcome.accepted,
            "reward_numeric": isinstance(reward, (int, float)) and not isinstance(reward, bool),
        }
        return {
            "environment": environment,
            "task_id": task_id,
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "reward": reward,
            "feedback": outcome.feedback,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "environment": environment,
            "task_id": task_id,
            "status": "fail",
            "error": repr(exc),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        if episode_id is not None:
            try:
                await client.close_episode(episode_id)
            except Exception as exc:
                print(json.dumps({"close_error": repr(exc), "task_id": task_id}), flush=True)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Smoke every environment the host serves and summarise."""
    async with EnvironmentClient(args.server, timeout=args.timeout) as client:
        health = await client.health()
        tasks = await client.tasks()
        by_environment: dict[str, list[str]] = {}
        for task in tasks:
            by_environment.setdefault(task["environment"], []).append(task["task_id"])
        results = []
        for environment, task_ids in by_environment.items():
            for task_id in task_ids if args.all_tasks else task_ids[:1]:
                result = await smoke_task(client, environment, task_id)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                results.append(result)
    pass_count = sum(row["status"] == "pass" for row in results)
    return {
        "schema": "trace2world.environment_host_smoke.v1",
        "status": "pass" if results and pass_count == len(results) else "fail",
        "server": args.server,
        "health": health,
        "environment_count": len(by_environment),
        "episode_count": len(results),
        "pass_count": pass_count,
        "task_count": len(tasks),
        "results": results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``t2w-smoke`` arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:6800")
    parser.add_argument(
        "--all-tasks", action="store_true", help="Smoke every task, not one per environment."
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the smoke test; exit status 0 only when every episode passes."""
    args = parse_args(argv)
    result = asyncio.run(run(args))
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    raise SystemExit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
