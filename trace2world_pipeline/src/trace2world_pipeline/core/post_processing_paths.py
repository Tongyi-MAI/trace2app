
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from trace2world_pipeline.core.phase_output import PhaseOutputManager


def post_processing_path(result_dir: str | Path, *parts: str) -> Path:
    return PhaseOutputManager(result_dir).formal("post_processing", *parts)


def post_processing_debug_path(result_dir: str | Path, *parts: str) -> Path:
    return PhaseOutputManager(result_dir).debug("post_processing", *parts)


def first_existing(candidates: Iterable[str | Path]) -> Path | None:
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def task_file_candidates(result_dir: str | Path, *, prefer_rewritten: bool = True) -> list[Path]:
    root = Path(result_dir)
    rewritten = [
        post_processing_path(root, "tasks", "rewritten_tasks.json"),
    ]
    original = [
        post_processing_path(root, "tasks", "tasks.json"),
    ]
    return rewritten + original if prefer_rewritten else original + rewritten


def evaluator_file_candidates(result_dir: str | Path) -> list[Path]:
    root = Path(result_dir)
    return [
        post_processing_path(root, "evaluators", "evaluators.json"),
    ]
