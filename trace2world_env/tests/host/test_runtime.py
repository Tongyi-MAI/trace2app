"""Pure helpers of the episode loop, the page runtime protocol, and the browser evaluator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from trace2world_env.episodes import (
    next_context_step,
    observed_trace_step,
    should_republish_context,
)
from trace2world_env.page_runtime import launch_context_script, task_url, url_with_sid
from trace2world_env.scoring.browser import build_harness_script, evaluate_browser_checkpoints
from trace2world_env.scoring.browser_plan import BrowserPlanEntry, load_plan_entry
from trace2world_env.server import evaluation_response

BASE = "http://localhost:17100/app/demo"


def test_context_step_bookkeeping() -> None:
    assert next_context_step(None, 0) == 0
    assert next_context_step(4, 2) == 5
    assert next_context_step(1, 3) == 3
    with pytest.raises(ValueError):
        next_context_step(None, -1)
    assert observed_trace_step({"action_id": "trace_7:step12"}) == 12
    assert observed_trace_step({"action_id": "trace_7:done"}) is None
    assert observed_trace_step(None) is None
    assert should_republish_context({"action_type": "navigate_back"}, "a", "a")
    assert should_republish_context({"action_type": "click"}, "a", "b")
    assert not should_republish_context({"action_type": "click"}, "a", "a")
    assert not should_republish_context({"action_type": "click"}, "", "b")


def test_task_urls_carry_sid() -> None:
    assert task_url(BASE, "/pages/a.html", "s1") == f"{BASE}/pages/a.html?sid=s1"
    assert task_url(BASE, "a.html", None) == f"{BASE}/a.html"
    assert url_with_sid(f"{BASE}/b.html?x=1", "s1", BASE) == f"{BASE}/b.html?x=1&sid=s1"
    assert url_with_sid(f"{BASE}/b.html?sid=s1", "s1", BASE) == f"{BASE}/b.html?sid=s1"
    assert url_with_sid("http://localhost:17100/other/b.html", "s1", BASE).endswith("b.html")


def test_launch_context_uses_sim_protocol() -> None:
    script = launch_context_script(
        "task_1",
        start_filename="index.html",
        initial_state={"collections": {}},
        source_reference_task_id="ref_9",
        sid="s1",
    )
    context = json.loads(script.split("const context = ", 1)[1].split(";\n", 1)[0])
    assert context["task_context_protocol"] == "sim.task_launch_context.v1"
    assert context["trace_id"] == context["reference_trace_id"] == "ref_9"
    assert context["source"] == "env_server"
    assert (context["response_mode"], context["actionability_preflight"]) == ("env_text", True)
    assert "window.__simExecutionContext = context;" in script
    assert "sessionStorage.setItem('sim.execution_context', text)" in script


def test_harness_template_is_fully_filled(tmp_path: Path) -> None:
    plan = {
        "evaluators": {
            "t1": {
                "evaluator_file": "t1.js",
                "required_checkpoints": ["a", "b"],
                "checkpoint_weights": {"a": 0.4, "b": 0.6},
                "source_reference_task_id": "ref",
            }
        }
    }
    (tmp_path / "browser_evaluator_plan.json").write_text(json.dumps(plan))
    entry = BrowserPlanEntry.from_entry(load_plan_entry(tmp_path, "t1"))
    assert entry.required_checkpoints == ["a", "b"]
    assert entry.checkpoint_weights == {"a": 0.4, "b": 0.6}
    script = build_harness_script("return 1; // @@NOT_A_SLOT@@", "t1", entry)
    assert script.startswith("(() => {\n") and script.endswith("})()")
    assert "@@" not in script.replace("@@NOT_A_SLOT@@", "")
    assert '  const configuredRequired = ["a", "b"];\n' in script
    assert '  const configuredSourceReferenceTaskId = "ref";\n' in script
    assert "window.SimCheckpointRuntime" in script
    assert BrowserPlanEntry.from_entry("not an object").required_checkpoints == []


class _Page:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.scripts: list[str] = []

    async def evaluate(self, script: str) -> Any:
        self.scripts.append(script)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_browser_evaluation_payload(tmp_path: Path) -> None:
    (tmp_path / "browser_evaluator_plan.json").write_text(
        json.dumps(
            {"evaluators": {"t1": {"evaluator_file": "t1.js", "required_checkpoints": ["a"]}}}
        )
    )
    (tmp_path / "t1.js").write_text("return 1;")
    page = _Page(
        {
            "raw_result": 0.5,
            "details": {
                "checkpoint_reward_vector": [
                    {"checkpoint_id": "a", "reached": True, "reward_contribution": 0.5}
                ]
            },
        }
    )
    result = asyncio.run(evaluate_browser_checkpoints(page, task_id="t1", evaluators_dir=tmp_path))
    payload = result.to_payload()
    assert list(payload)[:3] == ["score", "reason", "raw_reward"]
    assert payload["raw_reward"] == 0.5 and payload["terminal_success"] is False
    metadata = payload["evaluation_metadata"]
    assert metadata["schema"] == "trace2world.evaluation_metadata.v1"
    assert metadata["seen_checkpoints"] == ["a"] and metadata["missing_checkpoints"] == []
    assert payload["evaluation"]["task_id"] == "t1"
    response = evaluation_response(payload)
    assert (response["reward"], response["success"], response["reason"]) == (0.5, False, "")

    missing = asyncio.run(evaluate_browser_checkpoints(page, task_id="t2", evaluators_dir=tmp_path))
    assert missing.to_payload()["reason"] == "missing_evaluator_file"
    crashed = asyncio.run(
        evaluate_browser_checkpoints(
            _Page(RuntimeError("boom")), task_id="t1", evaluators_dir=tmp_path
        )
    )
    assert crashed.to_payload()["evaluation"] == {
        "error": "boom",
        "raw_reward": 0.0,
        "reason": "evaluator_execution_error",
        "terminal_success": False,
        "checkpoint_reward_vector": [],
        "ability_reward_vector": {},
        "nonzero_checkpoint_reached": None,
        "env_valid": False,
        "format_valid": False,
    }
