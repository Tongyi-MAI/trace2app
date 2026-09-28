
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx


def _checked(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


def _is_png(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return base64.b64decode(value, validate=True).startswith(b"\x89PNG\r\n\x1a\n")
    except Exception:
        return False


async def smoke_one(
    client: httpx.AsyncClient,
    app: str,
    task_id: str,
) -> dict[str, Any]:
    started = time.monotonic()
    display_id: int | None = None
    try:
        acquired = _checked(await client.post("/display/acquire"))
        display_id = int(acquired["logical_id"])
        initialized = _checked(
            await client.post(
                "/task/init",
                json={"task_name": task_id, "display_id": display_id},
            )
        )
        goal = _checked(
            await client.get(
                "/task/goal",
                params={"task_name": task_id, "display_id": display_id},
            )
        )
        screenshot = _checked(
            await client.get("/screenshot", params={"display_id": display_id})
        )
        step = _checked(
            await client.post(
                "/step",
                json={
                    "display_id": display_id,
                    "action": {
                        "action_type": "unknown",
                        "error": "standalone_runtime_smoke",
                    },
                },
            )
        )
        evaluation = _checked(
            await client.get(
                "/task/eval",
                params={
                    "task_name": task_id,
                    "display_id": display_id,
                    "preview": "false",
                },
            )
        )
        reward = evaluation.get("raw_reward", evaluation.get("score"))
        route = str(screenshot.get("current_url") or "")
        feedback = step.get("env_text") or (
            step.get("actionability_feedback", {}).get("message")
            if isinstance(step.get("actionability_feedback"), dict)
            else None
        )
        checks = {
            "initialized": initialized.get("ok") is True,
            "goal_nonempty": isinstance(goal, str) and bool(goal.strip()),
            "screenshot_png": _is_png(screenshot.get("b64_png")),
            "app_route_selected": f"/app/{app}/" in route,
            "text_feedback_returned": isinstance(feedback, str) and bool(feedback),
            "reward_numeric": isinstance(reward, (int, float)) and not isinstance(reward, bool),
        }
        return {
            "app": app,
            "task_id": task_id,
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "reward": reward,
            "feedback": feedback,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "app": app,
            "task_id": task_id,
            "status": "fail",
            "error": repr(exc),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        if display_id is not None:
            try:
                await client.post(
                    "/task/tear_down",
                    json={"task_name": task_id, "display_id": display_id},
                )
            finally:
                await client.post("/display/release", params={"display_id": display_id})


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    rows = manifest.get("environments") or manifest.get("apps")
    if not isinstance(rows, list) or not rows:
        raise ValueError("environment manifest must bind at least one environment")
    async with httpx.AsyncClient(base_url=args.server.rstrip("/"), timeout=args.timeout) as client:
        health = _checked(await client.get("/health"))
        tasks = _checked(await client.get("/tasks")).get("tasks", [])
        task_ids = {
            str(row.get("task_id") or row.get("id") or "")
            for row in tasks
            if isinstance(row, dict)
        }
        results = []
        for row in rows:
            app = str(row.get("id") or row["app"])
            declared = row.get("task_ids")
            if not isinstance(declared, list) or not declared:
                raise ValueError(f"environment has no task ids: {app}")
            task_id = str(declared[0])
            if task_id not in task_ids:
                raise ValueError(f"host omitted manifest task: {task_id}")
            result = await smoke_one(client, app, task_id)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            results.append(result)
    pass_count = sum(row["status"] == "pass" for row in results)
    return {
        "schema": "trace2world.environment_host_smoke.v1",
        "status": "pass" if pass_count == len(results) else "fail",
        "server": args.server,
        "health": health,
        "environment_count": len(results),
        "pass_count": pass_count,
        "task_count": len(task_ids),
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one browser lifecycle per Trace2World environment against a running host."
    )
    parser.add_argument("--server", default="http://127.0.0.1:6800")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("environments_manifest.json"),
        help="Path to environments_manifest.json of the hosted release.",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
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
    raise SystemExit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
