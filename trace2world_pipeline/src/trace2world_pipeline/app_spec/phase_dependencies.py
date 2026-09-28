
from __future__ import annotations

from typing import Any

from trace2world_pipeline.core.phase_output import PHASE_ARTIFACTS, PHASE_ORDER, phase_index


PHASE_DEPENDENCIES: dict[str, list[str]] = {
    "trace_semantic_extraction": ["global_reasoning"],
    "instance_splitting": ["global_reasoning", "trace_semantic_extraction"],
    "l0_fidelity": ["instance_splitting"],
    "content_extraction": ["instance_splitting", "l0_fidelity"],
    "consolidation": ["content_extraction"],
    "page_environment": ["consolidation"],
    "interface_design": ["page_environment"],
    "sdk_generation": ["page_environment", "interface_design"],
    "pagegen_input_scope": ["page_environment", "sdk_generation"],
    "pagegen": ["pagegen_input_scope"],
    "post_processing": ["pagegen"],
}


def direct_dependents(phase: str) -> list[str]:
    phase_index(phase)
    return sorted(
        candidate
        for candidate, deps in PHASE_DEPENDENCIES.items()
        if phase in deps
    )


def transitive_dependents(phase: str) -> list[str]:
    phase_index(phase)
    seen: set[str] = set()
    pending = list(direct_dependents(phase))
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        pending.extend(direct_dependents(current))
    return sorted(seen, key=phase_index)


def earliest_stale_phase(changed_phase: str) -> str:
    phase_index(changed_phase)
    dependents = transitive_dependents(changed_phase)
    if not dependents:
        return changed_phase
    return min(dependents, key=phase_index)


def cleanup_plan_for_changed_phase(changed_phase: str) -> dict[str, Any]:
    earliest = earliest_stale_phase(changed_phase)
    start = phase_index(earliest)
    stale_phases = list(PHASE_ORDER[start:])
    patterns: list[str] = []
    for phase in stale_phases:
        patterns.extend(PHASE_ARTIFACTS.get(phase, ()))
    patterns.append("artifact_index.json")
    return {
        "format": "ghostweb_phase_dependency_cleanup_plan",
        "changed_phase": changed_phase,
        "earliest_stale_phase": earliest,
        "stale_phases": stale_phases,
        "artifact_patterns": patterns,
    }
