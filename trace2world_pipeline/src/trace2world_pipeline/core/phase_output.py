
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


PHASE_ORDER: list[str] = [
    "trace_preparation",
    "frame_analysis",
    "global_reasoning",
    "trace_semantic_extraction",
    "instance_splitting",
    "l0_fidelity",
    "content_extraction",
    "consolidation",
    "page_environment",
    "interface_design",
    "sdk_generation",
    "pagegen_input_scope",
    "pagegen",
    "post_processing",
]


PHASE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "frame_analysis": (
        "phase_frame_analysis/**",
    ),
    "global_reasoning": (
        "phase_global_reasoning/**",
    ),
    "trace_semantic_extraction": (
        "phase_trace_semantic_extraction/**",
    ),
    "instance_splitting": (
        "phase_instance_splitting/**",
    ),
    "l0_fidelity": (
        "phase_l0_fidelity/**",
    ),
    "content_extraction": (
        "phase_content_extraction/**",
    ),
    "consolidation": (
        "phase_consolidation/**",
    ),
    "page_environment": (
        "phase_page_environment/**",
    ),
    "interface_design": (
        "phase_interface_design/**",
    ),
    "sdk_generation": (
        "phase_sdk_generation/**",
    ),
    "pagegen_input_scope": (
        "phase_pagegen_input_scope/**",
    ),
    "pagegen": (
        "phase_pagegen/**",
    ),
    "post_processing": (
        "phase_post_processing/**",
    ),
}


def phase_index(phase: str) -> int:
    try:
        return PHASE_ORDER.index(phase)
    except ValueError as exc:
        raise ValueError(f"unknown phase: {phase}; expected one of: {', '.join(PHASE_ORDER)}") from exc


class PhaseOutputManager:

    def __init__(self, result_dir: str | Path):
        self.result_dir = Path(result_dir)

    def phase_dir(self, phase: str) -> Path:
        return self.result_dir / f"phase_{phase}"

    def formal(self, phase: str, *parts: str) -> Path:
        return self.phase_dir(phase) / Path(*parts) if parts else self.phase_dir(phase)

    def debug(self, phase: str, *parts: str) -> Path:
        return self.phase_dir(phase) / "debug" / Path(*parts) if parts else self.phase_dir(phase) / "debug"

    def site_model_dir(self) -> Path:
        return self.phase_dir("consolidation") / "site_model"

    def write_json(
        self,
        phase: str,
        kind: str,
        rel_path: str,
        payload: Any,
        *,
        artifact_id: str = "",
        artifact_kind: str = "",
        producer: str = "",
        register: bool = True,
    ) -> Path:
        if kind == "formal":
            target = self.formal(phase, rel_path)
        elif kind == "debug":
            target = self.debug(phase, rel_path)
        else:
            raise ValueError(f"kind must be 'formal' or 'debug', got {kind!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if register:
            if not artifact_id:
                artifact_id = f"{phase}.{rel_path.replace('/', '.').removesuffix('.json')}"
            if not artifact_kind:
                artifact_kind = "debug_export" if kind == "debug" else "det_projection"
            try:
                from trace2world_pipeline.app_spec.artifact_index import register_artifact as _register
                _register(
                    self.result_dir,
                    target,
                    artifact_id=artifact_id,
                    phase=phase,
                    artifact_kind=artifact_kind,
                    api_or_det="det",
                    canonical_source=(kind == "formal"),
                    logical_owner=phase,
                    producer=producer or f"phase_output.{phase}",
                    debug_only=(kind == "debug"),
                    refresh_filesystem_summary=False,
                )
            except Exception:
                pass
        return target

    def ensure_dir(self, phase: str, kind: str, *parts: str) -> Path:
        d = self.formal(phase, *parts) if kind == "formal" else self.debug(phase, *parts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def resolve_read_path(self, phase: str, kind: str, rel_path: str) -> Optional[Path]:
        phase_path = self.formal(phase, rel_path) if kind == "formal" else self.debug(phase, rel_path)
        return phase_path if phase_path.exists() else None
