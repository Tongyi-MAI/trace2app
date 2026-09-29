"""Browser checkpoint evaluation.

Environments with ``runtime_profile: browser_checkpoint_events`` ship one JavaScript evaluator
per task. The host runs it inside ``checkpoint_harness.js``, which reads the page's checkpoint
event log and app-owned state snapshot before and after the evaluator, rejects results whose
evidence changed while evaluating, and recomputes the reward from the grounded checkpoints
declared in ``browser_evaluator_plan.json`` (see :mod:`trace2world_env.scoring.browser_plan`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from trace2world_env.scoring.browser_plan import (
    BrowserPlanEntry,
    evaluator_script_path,
    load_plan_entry,
)
from trace2world_env.scoring.plans import validate_browser_evaluator_source
from trace2world_env.scoring.result import EvaluationResult

logger = logging.getLogger(__name__)

_SLOT = re.compile(r"@@([A-Z_]+)@@")


@cache
def _harness_template() -> str:
    return (
        resources.files("trace2world_env.scoring")
        .joinpath("checkpoint_harness.js")
        .read_text(encoding="utf-8")
    )


def build_harness_script(evaluator_source: str, task_id: str, plan: BrowserPlanEntry) -> str:
    """Fill the harness template with the plan declarations and the evaluator source."""

    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    slots = {
        "RUNTIME_EVENT_CONTRACT_JSON": dumps(plan.runtime_event_contract),
        "CHECKPOINT_EVIDENCE_CONTRACT_JSON": dumps(plan.checkpoint_evidence_contract),
        "JS_CODE": evaluator_source,
        "REQUIRED_JSON": dumps(plan.required_checkpoints),
        "DIAGNOSTIC_JSON": dumps(plan.diagnostic_checkpoints),
        "TERMINAL_JSON": dumps(plan.terminal_checkpoints),
        "DEPENDENCIES_JSON": dumps(plan.checkpoint_dependencies),
        "WEIGHTS_JSON": dumps(plan.checkpoint_weights),
        "ZERO_WEIGHT_ASSERTION_IDS_JSON": dumps(plan.zero_weight_assertion_ids),
        "SOLUTION_POLICY_JSON": dumps(plan.terminal_solution_policy),
        "TASK_ID_JSON": dumps(task_id or ""),
        "SOURCE_REFERENCE_TASK_ID_JSON": dumps(plan.source_reference_task_id or ""),
    }
    return _SLOT.sub(lambda match: slots[match.group(1)], _harness_template())


async def evaluate_browser_checkpoints(
    page: Page,
    *,
    task_id: str,
    evaluators_dir: Path,
) -> EvaluationResult:
    """Run the task's evaluator in ``page`` and return the grounded result."""
    entry = load_plan_entry(evaluators_dir, task_id)
    try:
        eval_path = evaluator_script_path(evaluators_dir, task_id, entry)
    except ValueError as exc:
        logger.warning("Invalid evaluator mapping for task %s: %s", task_id, exc)
        return EvaluationResult.failure("invalid_evaluator_mapping")
    if not eval_path.exists():
        logger.warning("No evaluator file for task %s at %s", task_id, eval_path)
        return EvaluationResult.failure("missing_evaluator_file")
    js_code = eval_path.read_text(encoding="utf-8")

    try:
        validate_browser_evaluator_source(js_code, evaluator_path=eval_path)
        plan = BrowserPlanEntry.from_entry(entry)
        result = await page.evaluate(build_harness_script(js_code, task_id, plan))
        return _attach_evaluation_metadata(
            _normalize_browser_result(result),
            task_id=task_id,
            evaluator_path=eval_path,
            evaluator_source=js_code,
            plan=plan,
        )
    except Exception as exc:
        logger.warning("Browser evaluation failed: %s", exc)
        return EvaluationResult.failure("evaluator_execution_error", {"error": str(exc)})


def _normalize_browser_result(result: Any) -> EvaluationResult:
    """Clamp the harness output to a reward in [0, 1] and fill the default detail fields."""
    raw_result = result
    details: dict[str, Any] = {}
    if isinstance(result, dict) and "raw_result" in result:
        raw_result = result.get("raw_result")
        if isinstance(result.get("details"), dict):
            details = dict(result["details"])
    elif isinstance(result, dict):
        details = dict(result)

    if isinstance(raw_result, dict):
        details = {**raw_result, **details}
        raw_result = details.get("raw_reward", details.get("score"))

    try:
        score = float(raw_result) if isinstance(raw_result, (int, float)) else 0.0
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    details["raw_reward"] = score
    details.setdefault("terminal_success", score >= 1.0)
    details.setdefault("nonzero_checkpoint_reached", score > 0.0)
    details.setdefault("checkpoint_reward_vector", [])
    if (
        "ability_reward_vector" not in details
        and isinstance(details.get("reward_vector"), dict)
    ):
        details["ability_reward_vector"] = {
            str(key): float(value)
            for key, value in details["reward_vector"].items()
            if key not in {"raw_reward", "terminal_success", "safety_violation"}
            and isinstance(value, (int, float))
        }
    details.setdefault("ability_reward_vector", {})
    details.setdefault("env_valid", True)
    details.setdefault("format_valid", None)
    reason = details.get("evaluation_invalid_reason")
    if not isinstance(reason, str):
        reason = ""
    return EvaluationResult.from_details(score, reason, details)


def _attach_evaluation_metadata(
    result: EvaluationResult,
    *,
    task_id: str,
    evaluator_path: Path,
    evaluator_source: str,
    plan: BrowserPlanEntry,
) -> EvaluationResult:
    """Add ``evaluation_metadata``: evaluator identity, checkpoint denominator and attribution."""
    if not task_id:
        return result
    vector = (
        result.checkpoint_reward_vector
        if isinstance(result.checkpoint_reward_vector, list)
        else []
    )
    vector_ids = [
        str(row.get("checkpoint_id") or "")
        for row in vector
        if isinstance(row, dict) and row.get("checkpoint_id")
    ]
    configured_required = plan.required_checkpoints
    required = list(configured_required or dict.fromkeys(vector_ids))
    seen = [
        checkpoint_id
        for checkpoint_id in required
        if any(
            isinstance(row, dict)
            and str(row.get("checkpoint_id") or "") == checkpoint_id
            and row.get("reached") is True
            for row in vector
        )
    ]
    missing = [checkpoint_id for checkpoint_id in required if checkpoint_id not in seen]
    attribution = []
    for checkpoint_id in required:
        row = next(
            (
                candidate
                for candidate in vector
                if isinstance(candidate, dict)
                and str(candidate.get("checkpoint_id") or "") == checkpoint_id
            ),
            {},
        )
        attribution.append(
            {
                "checkpoint_id": checkpoint_id,
                "reached": checkpoint_id in seen,
                "first_event_index": row.get("first_event_index"),
                "first_event_index_source": row.get("first_event_index_source"),
                "reward_contribution": row.get("reward_contribution", 0),
                "checkpoint_dependencies": row.get("checkpoint_dependencies", []),
                "credit_assignment_mode": row.get("credit_assignment_mode"),
            }
        )
    metadata = {
        "schema": "trace2world.evaluation_metadata.v1",
        "status": "complete",
        "task_id": task_id,
        "evaluator_path": str(evaluator_path),
        "evaluator_sha256": "sha256:"
        + hashlib.sha256(evaluator_source.encode("utf-8")).hexdigest(),
        "required_checkpoints": required,
        "required_checkpoints_source": (
            "evaluator_plan"
            if configured_required
            else "evaluator_reward_vector"
            if vector_ids
            else "none_configured"
        ),
        "terminal_checkpoints": list(plan.terminal_checkpoints),
        "diagnostic_checkpoints": list(plan.diagnostic_checkpoints),
        "terminal_solution_policy": dict(plan.terminal_solution_policy),
        "runtime_event_contract": dict(plan.runtime_event_contract),
        "checkpoint_evidence_contract": dict(plan.checkpoint_evidence_contract),
        "checkpoint_dependencies": plan.checkpoint_dependencies,
        "seen_checkpoints": seen,
        "missing_checkpoints": missing,
        "checkpoint_attribution": attribution,
        "checkpoint_contract": (
            "configured_checkpoint_denominator"
            if required
            else "no_configured_checkpoints"
        ),
        "grounded_reducer_contract": "browser_checkpoint_grounded_v3e",
    }
    details = dict(result.details)
    details.setdefault("task_id", task_id)
    details["evaluation_metadata"] = metadata
    return result.with_details(details)
