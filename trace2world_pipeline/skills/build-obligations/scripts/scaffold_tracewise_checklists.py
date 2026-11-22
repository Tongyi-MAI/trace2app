#!/usr/bin/env python3
"""Scaffold one pending Trace2World reference stepwise checklist per trace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SET_SCHEMA = "trace2world.reference_stepwise_checklist_set.v1"
INDEX_SCHEMA = "trace2world.reference_stepwise_checklist_index.v1"
CHECKLIST_SCHEMA = "trace2world.reference_stepwise_checklist.v1"
EVIDENCE_SCHEMA = "trace2world.reference_stepwise_checklist_evidence.v1"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON: {path}")
    return data


def write_json(path: Path, data: dict[str, Any], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_prediction(value: Any) -> str:
    text = str(value or "").strip()
    if "<action>" in text and "</action>" in text:
        return text.split("<action>", 1)[1].split("</action>", 1)[0].strip()
    return text


def disposition(step: dict[str, Any]) -> str:
    return str(
        (step.get("step_disposition") or {}).get("verdict")
        or (step.get("effective_step") or {}).get("verdict")
        or ("skip" if step.get("drop") else "keep")
    )


def discover(trace_root: Path, selected: set[str] | None) -> list[Path]:
    paths = sorted(
        path
        for path in trace_root.iterdir()
        if path.is_dir() and (path / "trajectory.json").is_file() and (not selected or path.name in selected)
    )
    if selected:
        found = {path.name for path in paths}
        missing = sorted(selected - found)
        if missing:
            raise FileNotFoundError(f"missing selected traces under {trace_root}: {', '.join(missing)}")
    if not paths:
        raise FileNotFoundError(f"no trace directories with trajectory.json under {trace_root}")
    return paths


def pending_row(index: int, step: dict[str, Any], marked_dir: Path) -> dict[str, Any]:
    return {
        "step_index": index,
        "disposition": disposition(step),
        "raw_action_snapshot": str(step.get("action") or ""),
        "semantic_action_snapshot": parse_prediction(step.get("prediction")),
        "current_observation": str(step.get("screenshot") or ""),
        "marked_screenshot": str(step.get("marked_screenshot") or (marked_dir / f"step_{index:04d}.jpg")),
        "pre_action_expected_visible_state": [],
        "pre_action_forbidden_future_state": [],
        "source_host": "",
        "operation_intent": "",
        "post_action_expected_effect": [],
        "next_state_anchor": "",
        "progressive_lifecycle": {
            "applies": False,
            "expected_before": [],
            "expected_after": [],
            "forbidden_before_action": [],
        },
        "input_or_autocomplete": {
            "applies": False,
            "input_value_before": "",
            "input_value_after": "",
            "expected_suggestions": [],
            "forbidden_preselection": [],
        },
        "overlay_or_parent_projection": {
            "applies": False,
            "expected_current_surface": [],
            "expected_parent_projection": [],
            "forbidden_sibling_or_future_projection": [],
        },
        "viewport_containment": {
            "expected_shell": "mobile_content_viewport",
            "must_be_inside_shell": [],
            "allowed_outside_shell": [],
        },
        "terminal_or_supervision": {
            "applies": False,
            "required_values": [],
            "success_relation": "",
            "must_not_pass_without": [],
        },
        "terminal_synthetic_projection_handoff": {
            "applies": False,
            "eligibility": "not_applicable",
            "pre_action_visible_state": "",
            "expected_terminal_delta": "",
            "forbidden_terminal_outcomes": [],
            "source_host_and_target_unambiguous": None,
            "local_deterministic_delta": None,
            "no_later_reference_dependency": None,
            "no_reference_contradiction": None,
            "safe_reversible_or_low_risk": None,
            "requires_generated_visible_projection": None,
            "requires_app_owned_state_and_evaluator_probe": None,
            "rationale": "",
        },
        "terminal_action_commit_handoff": {
            "applies": False,
            "eligibility": "not_applicable",
            "visible_source_host": "",
            "marked_target": "",
            "committed_action": "",
            "success_boundary": "",
            "destination_content_claimed": False,
            "reference_post_action_effect_claimed": False,
            "effect_class": "",
            "post_action_effect_out_of_scope": None,
            "required_gui_evidence": [],
            "forbidden_success_inference": [],
            "source_host_and_target_unambiguous": None,
            "action_target_and_commit_unambiguous": None,
            "no_later_reference_dependency": None,
            "reference_proves_action_was_issued": None,
            "rationale": "",
        },
        "annotation_text_disagreement": "",
        "reference_annotation_checks": [
            "pre_action_source_host",
            "marked_target_alignment",
            "source_host_not_destination",
            "next_state_leak_absent",
            "post_action_effect",
            "instruction_trace_alignment",
            "terminal_dependency_explicit",
        ],
        "env_execution_checks": [
            "whole_viewport_chrome_scan",
            "l0_related_surface_parity_check",
            "progressive_surface_lifecycle_check",
            "parent_projection_contamination_check",
            "viewport_containment",
        ],
        "evidence": [],
        "blockers": ["pending_visual_oracle_annotation"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--checklist-dir", required=True, type=Path)
    parser.add_argument("--traces", default="")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    selected = {item.strip() for item in args.traces.split(",") if item.strip()} or None
    trace_dirs = discover(args.trace_root, selected)
    now = datetime.now(timezone.utc).isoformat()
    index_entries: list[dict[str, Any]] = []

    for trace_dir in trace_dirs:
        trace_id = trace_dir.name
        trajectory_path = trace_dir / "trajectory.json"
        trajectory = load_json(trajectory_path)
        steps = trajectory.get("trajectory") or trajectory.get("steps") or []
        if not isinstance(steps, list):
            raise ValueError(f"{trajectory_path}: trajectory/steps must be a list")
        trace_output = args.checklist_dir / "traces" / trace_id
        marked_dir = trace_output / "reference_sprites" / "marked"
        kept_rows = [
            pending_row(index, step, marked_dir)
            for index, step in enumerate(steps)
            if isinstance(step, dict) and disposition(step) == "keep"
        ]
        digest = sha256(trajectory_path)
        checklist_path = trace_output / "stepwise_checklist.json"
        evidence_path = trace_output / "evidence.json"
        checklist = {
            "schema": CHECKLIST_SCHEMA,
            "trace_id": trace_id,
            "source_trace_root": str(args.trace_root),
            "trajectory_path": str(trajectory_path),
            "trajectory_sha256": digest,
            "construction_stage": "source_evidence",
            "annotation_text_is_candidate_only": True,
            "instruction": str(trajectory.get("instruction") or ""),
            "sprite_manifest_path": str(trace_output / "reference_sprites" / "manifest.json"),
            "sprite_path": str(trace_output / "reference_sprites" / "sprites" / f"{trace_id}_reference_sprite.jpg"),
            "sprite_opened_and_read": False,
            "rows": kept_rows,
            "status": "pending",
            "blockers": ["pending_visual_oracle_annotation"],
        }
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "trace_id": trace_id,
            "source_trace_root": str(args.trace_root),
            "trajectory_path": str(trajectory_path),
            "trajectory_sha256": digest,
            "commands": [],
            "sprite_manifest_path": checklist["sprite_manifest_path"],
            "sprite_path": checklist["sprite_path"],
            "sprite_opened_and_read": False,
            "checklist_path": str(checklist_path),
            "blockers": ["pending_visual_oracle_annotation"],
        }
        write_json(checklist_path, checklist, args.overwrite)
        write_json(evidence_path, evidence, args.overwrite)
        index_entries.append(
            {
                "trace_id": trace_id,
                "trajectory_path": str(trajectory_path),
                "trajectory_sha256": digest,
                "steps_total": len(steps),
                "kept_steps": len(kept_rows),
                "checklist_path": str(checklist_path),
                "evidence_path": str(evidence_path),
                "status": "pending",
                "blockers": ["pending_visual_oracle_annotation"],
            }
        )

    manifest = {
        "schema": SET_SCHEMA,
        "source_trace_root": str(args.trace_root),
        "construction_stage": "source_evidence",
        "trace_count": len(index_entries),
        "trace_ids": [entry["trace_id"] for entry in index_entries],
        "aggregate_stepwise_checklist_forbidden": True,
        "created_at": now,
        "updated_at": now,
        "status": "pending",
    }
    index = {
        "schema": INDEX_SCHEMA,
        "source_trace_root": str(args.trace_root),
        "traces": index_entries,
    }
    write_json(args.checklist_dir / "manifest.json", manifest, args.overwrite)
    write_json(args.checklist_dir / "trace_index.json", index, args.overwrite)
    print(json.dumps({"checklist_dir": str(args.checklist_dir), "trace_count": len(index_entries)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
