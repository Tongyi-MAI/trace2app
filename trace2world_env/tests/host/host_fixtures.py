"""Builders for a tiny synthetic Trace2World environment on disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EVALUATOR_JS = "return {score: 1, checkpoint_reward_vector: []};\n"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_environment(root: Path, *, task_ids: tuple[str, ...] = ("t1", "t2")) -> Path:
    """Write a minimal ``browser_checkpoint_events`` environment under ``root``."""
    (root / "app" / "phase_pagegen" / "pages").mkdir(parents=True)
    (root / "app" / "index.html").write_text("<html><body>home</body></html>")
    (root / "app" / "phase_pagegen" / "pages" / "detail.html").write_text("<p>detail</p>")
    write_json(
        root / "resource_manifest.json",
        {
            "format": "trace2world_environment_resource.v2",
            "runtime_profile": "browser_checkpoint_events",
            "app_root": "app",
            "tasks_file": "tasks.json",
            "entry_page": "index.html",
            "browser_viewport": {"width": 430, "height": 884},
            "evaluator": {"enabled": True, "plan": "evaluators/browser_evaluator_plan.json"},
        },
    )
    write_json(root / "package_validation_report.json", {"status": "pass"})
    tasks = [
        {
            "task_id": task_ids[0],
            "instruction": "Open the detail page",
            "start_page": "detail.html",
        },
        {
            "task_id": task_ids[1],
            "description": "Say hello",
            "start_page": "",
            "initial_state": {"collections": {}},
            "available_apps": [" Demo ", "demo-app"],
            "source_reference_task_id": "ref_1",
        },
    ]
    write_json(root / "tasks.json", {"tasks": tasks})
    for task_id in task_ids:
        (root / "evaluators").mkdir(exist_ok=True)
        (root / "evaluators" / f"{task_id}.js").write_text(EVALUATOR_JS)
    write_json(
        root / "evaluators" / "browser_evaluator_plan.json",
        {"evaluators": {task_id: {"evaluator_file": f"{task_id}.js"} for task_id in task_ids}},
    )
    return root
