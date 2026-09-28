
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from trace2world.env.evidence_contract import (
    EvidenceContractError,
    normalize_state_kernel_evaluator,
)

_EMPTY_CHECKPOINT_LIST_RE = re.compile(
    r"\b(?:const|let|var)\s+(?:EXPECTED|REQUIRED)\s*=\s*\[\s*\]\s*;"
)
_TASK_SPECIFIC_ORACLE_RE = re.compile(
    r"\bconst\s+TASK_SPECIFIC_ORACLE\s*=\s*true\s*;"
)
_NAMED_TASK_SPECIFIC_ORACLE_RE = re.compile(r"\boracle\s*:\s*['\"][^'\"]+['\"]")


def validate_browser_evaluator_source(
    source: str,
    *,
    evaluator_path: str | Path = "<inline>",
) -> None:
    if not _EMPTY_CHECKPOINT_LIST_RE.search(source):
        return
    has_task_specific_oracle = bool(_TASK_SPECIFIC_ORACLE_RE.search(source))
    has_named_oracle = bool(_NAMED_TASK_SPECIFIC_ORACLE_RE.search(source))
    if not (has_task_specific_oracle and has_named_oracle):
        raise ValueError(
            "Browser evaluator declares an empty EXPECTED/REQUIRED checkpoint list "
            "without an explicit named task-specific oracle: "
            f"{evaluator_path}"
        )


def validate_browser_evaluator_directory(evaluators_dir: str | Path) -> None:
    root = Path(evaluators_dir)
    for evaluator_path in sorted(root.glob("*.js")):
        validate_browser_evaluator_source(
            evaluator_path.read_text(encoding="utf-8"),
            evaluator_path=evaluator_path,
        )


def validate_browser_evaluator_plan(
    evaluator_plan: str | Path,
    evaluators_dir: str | Path,
    tasks: list[dict[str, Any]],
) -> None:
    plan_path = Path(evaluator_plan)
    root = Path(evaluators_dir)
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Browser evaluator plan is not readable JSON: {plan_path}"
        ) from error
    evaluators = payload.get("evaluators") if isinstance(payload, dict) else None
    if not isinstance(evaluators, dict):
        raise ValueError(
            "Browser evaluator plan must contain an evaluators object: "
            f"{plan_path}"
        )

    missing_entries: list[str] = []
    missing_files: list[str] = []
    invalid_entries: list[str] = []
    for task in tasks:
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id:
            continue
        entry = evaluators.get(task_id)
        if not isinstance(entry, dict):
            missing_entries.append(task_id)
            continue
        evaluator_file = str(entry.get("evaluator_file") or "").strip()
        if not evaluator_file or Path(evaluator_file).name != evaluator_file:
            invalid_entries.append(task_id)
            continue
        if not (root / evaluator_file).is_file():
            missing_files.append(f"{task_id}:{evaluator_file}")

    problems: list[str] = []
    if missing_entries:
        problems.append("missing plan entries=" + ",".join(missing_entries))
    if invalid_entries:
        problems.append("invalid evaluator_file entries=" + ",".join(invalid_entries))
    if missing_files:
        problems.append("missing evaluator files=" + ",".join(missing_files))
    if problems:
        raise ValueError(
            "Browser evaluator coverage is incomplete: " + "; ".join(problems)
        )


def validate_state_kernel_evaluator_plan(
    evaluator_plan: str | Path,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    plan_path = Path(evaluator_plan)
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"State-kernel evaluator plan is not readable JSON: {plan_path}"
        ) from error
    evaluators = payload.get("evaluators") if isinstance(payload, dict) else None
    if not isinstance(evaluators, dict):
        raise ValueError(
            "State-kernel evaluator plan must contain an evaluators object: "
            f"{plan_path}"
        )
    task_ids = [
        str(task.get("task_id") or task.get("id") or "").strip()
        for task in tasks
    ]
    task_ids = [task_id for task_id in task_ids if task_id]
    missing = [
        task_id for task_id in task_ids
        if not isinstance(evaluators.get(task_id), dict)
    ]
    if missing:
        raise ValueError(
            "State-kernel evaluator coverage is incomplete: missing plan entries="
            + ",".join(missing)
        )
    strict_task_ids: list[str] = []
    for task_id in task_ids:
        evaluator = evaluators[task_id]
        if evaluator.get("state_kernel_evidence_contract") is None:
            continue
        try:
            normalized = normalize_state_kernel_evaluator(evaluator)
        except EvidenceContractError as error:
            raise ValueError(
                f"Invalid strict state-kernel evaluator contract for {task_id}: {error}"
            ) from error
        if normalized is None:
            raise ValueError(
                f"Strict state-kernel evaluator contract did not normalize: {task_id}"
            )
        strict_task_ids.append(task_id)
    strict_set = set(strict_task_ids)
    gaps = [task_id for task_id in task_ids if task_id not in strict_set]
    return {
        "task_count": len(task_ids),
        "strict_task_ids": strict_task_ids,
        "strict_evaluation_gaps": gaps,
        "portfolio_release_ready": bool(task_ids) and not gaps,
    }
