#!/usr/bin/env python3
"""Validate a per-trace obligation checklist set produced by the build-obligations skill."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


SET_SCHEMA = "trace2world.reference_stepwise_checklist_set.v1"
INDEX_SCHEMA = "trace2world.reference_stepwise_checklist_index.v1"
CHECKLIST_SCHEMA = "trace2world.reference_stepwise_checklist.v1"
EVIDENCE_SCHEMA = "trace2world.reference_stepwise_checklist_evidence.v1"
SPRITE_SCHEMA = "trace2world.reference_trace_sprites.v1"
FORBIDDEN_ROOT_CHECKLISTS = {
    "package_wide_checklist.json",
    "stepwise_checklist.json",
    "closure_checklist.json",
}
REQUIRED_ROW_FIELDS = {
    "step_index",
    "disposition",
    "raw_action_snapshot",
    "semantic_action_snapshot",
    "current_observation",
    "marked_screenshot",
    "pre_action_expected_visible_state",
    "pre_action_forbidden_future_state",
    "source_host",
    "operation_intent",
    "post_action_expected_effect",
    "next_state_anchor",
    "progressive_lifecycle",
    "input_or_autocomplete",
    "overlay_or_parent_projection",
    "viewport_containment",
    "terminal_or_supervision",
    "reference_annotation_checks",
    "env_execution_checks",
    "evidence",
    "blockers",
}
REQUIRED_REFERENCE_CHECKS = {
    "pre_action_source_host",
    "marked_target_alignment",
    "source_host_not_destination",
    "next_state_leak_absent",
    "post_action_effect",
    "instruction_trace_alignment",
    "terminal_dependency_explicit",
}
REQUIRED_ENV_CHECKS = {
    "whole_viewport_chrome_scan",
    "l0_related_surface_parity_check",
    "progressive_surface_lifecycle_check",
    "parent_projection_contamination_check",
    "viewport_containment",
}
ALLOWED_PROJECTION_DISPOSITIONS = {
    "retained",
    "rewritten",
    "projected_terminal_handoff",
    "dropped_with_reason",
}


def load_json(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing file: {path}")
        return None
    except Exception as exc:
        errors.append(f"invalid json: {path}: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"expected object json: {path}")
        return None
    return data


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def disposition(step: dict[str, Any]) -> str:
    return str(
        (step.get("step_disposition") or {}).get("verdict")
        or (step.get("effective_step") or {}).get("verdict")
        or ("skip" if step.get("drop") else "keep")
    )


def expected_kept_steps(trajectory_path: Path, errors: list[str]) -> list[int]:
    trajectory = load_json(trajectory_path, errors)
    if not trajectory:
        return []
    steps = trajectory.get("trajectory") or trajectory.get("steps") or []
    if not isinstance(steps, list):
        errors.append(f"{trajectory_path}: trajectory/steps must be list")
        return []
    return [
        index
        for index, step in enumerate(steps)
        if isinstance(step, dict) and disposition(step) == "keep"
    ]


def require_string(row: dict[str, Any], field: str, trace_id: str, step_index: Any, errors: list[str]) -> None:
    if not isinstance(row.get(field), str) or not row.get(field).strip():
        errors.append(f"{trace_id}:{step_index} {field} must be nonempty string")


def require_nonempty_list(
    row: dict[str, Any], field: str, trace_id: str, step_index: Any, errors: list[str]
) -> None:
    value = row.get(field)
    if not isinstance(value, list) or not value:
        errors.append(f"{trace_id}:{step_index} {field} must be nonempty list")


def validate_obligation(
    value: Any,
    *,
    trace_id: str,
    step_index: Any,
    name: str,
    required_when_applies: tuple[str, ...],
    errors: list[str],
) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("applies"), bool):
        errors.append(f"{trace_id}:{step_index} {name} must be object with boolean applies")
        return
    if value["applies"]:
        for field in required_when_applies:
            item = value.get(field)
            if isinstance(item, list):
                if not item:
                    errors.append(f"{trace_id}:{step_index} {name}.{field} must be nonempty when applies")
            elif not isinstance(item, str) or not item.strip():
                errors.append(f"{trace_id}:{step_index} {name}.{field} must be nonempty when applies")


def validate_row(row: dict[str, Any], trace_id: str, source_trace_dir: Path, errors: list[str]) -> None:
    missing = sorted(REQUIRED_ROW_FIELDS - set(row))
    step_index = row.get("step_index")
    if missing:
        errors.append(f"{trace_id}:{step_index} missing row fields: {missing}")
    if not isinstance(step_index, int):
        errors.append(f"{trace_id}: row step_index must be int")
    if row.get("disposition") != "keep":
        errors.append(f"{trace_id}:{step_index} checklist row disposition must be keep")
    require_string(row, "current_observation", trace_id, step_index, errors)
    require_string(row, "marked_screenshot", trace_id, step_index, errors)
    for field in ("current_observation", "marked_screenshot"):
        raw_path = row.get(field)
        if isinstance(raw_path, str) and raw_path:
            path = Path(raw_path)
            if not path.is_absolute():
                path = source_trace_dir / path
            if not path.is_file():
                errors.append(f"{trace_id}:{step_index} {field} does not exist: {path}")
    require_nonempty_list(row, "pre_action_expected_visible_state", trace_id, step_index, errors)
    require_string(row, "source_host", trace_id, step_index, errors)
    require_string(row, "operation_intent", trace_id, step_index, errors)
    require_nonempty_list(row, "post_action_expected_effect", trace_id, step_index, errors)
    require_string(row, "next_state_anchor", trace_id, step_index, errors)
    if not isinstance(row.get("pre_action_forbidden_future_state"), list):
        errors.append(f"{trace_id}:{step_index} pre_action_forbidden_future_state must be list")
    validate_obligation(
        row.get("progressive_lifecycle"),
        trace_id=trace_id,
        step_index=step_index,
        name="progressive_lifecycle",
        required_when_applies=("expected_before", "expected_after", "forbidden_before_action"),
        errors=errors,
    )
    validate_obligation(
        row.get("input_or_autocomplete"),
        trace_id=trace_id,
        step_index=step_index,
        name="input_or_autocomplete",
        required_when_applies=("input_value_after", "expected_suggestions", "forbidden_preselection"),
        errors=errors,
    )
    validate_obligation(
        row.get("overlay_or_parent_projection"),
        trace_id=trace_id,
        step_index=step_index,
        name="overlay_or_parent_projection",
        required_when_applies=(
            "expected_current_surface",
            "expected_parent_projection",
            "forbidden_sibling_or_future_projection",
        ),
        errors=errors,
    )
    validate_obligation(
        row.get("terminal_or_supervision"),
        trace_id=trace_id,
        step_index=step_index,
        name="terminal_or_supervision",
        required_when_applies=("required_values", "success_relation", "must_not_pass_without"),
        errors=errors,
    )
    terminal_handoff = row.get("terminal_synthetic_projection_handoff")
    if terminal_handoff is not None:
        if not isinstance(terminal_handoff, dict) or not isinstance(terminal_handoff.get("applies"), bool):
            errors.append(
                f"{trace_id}:{step_index} terminal_synthetic_projection_handoff must be object with boolean applies"
            )
        elif terminal_handoff["applies"]:
            if terminal_handoff.get("eligibility") != "eligible":
                errors.append(f"{trace_id}:{step_index} terminal handoff must be eligible when applies")
            for field in ("pre_action_visible_state", "expected_terminal_delta", "rationale"):
                if not isinstance(terminal_handoff.get(field), str) or not terminal_handoff[field].strip():
                    errors.append(f"{trace_id}:{step_index} terminal handoff {field} must be nonempty")
            for field in (
                "source_host_and_target_unambiguous",
                "local_deterministic_delta",
                "no_later_reference_dependency",
                "no_reference_contradiction",
                "safe_reversible_or_low_risk",
                "requires_generated_visible_projection",
                "requires_app_owned_state_and_evaluator_probe",
            ):
                if terminal_handoff.get(field) is not True:
                    errors.append(f"{trace_id}:{step_index} terminal handoff {field} must be true")
            if not isinstance(terminal_handoff.get("forbidden_terminal_outcomes"), list) or not terminal_handoff.get("forbidden_terminal_outcomes"):
                errors.append(f"{trace_id}:{step_index} terminal handoff forbidden_terminal_outcomes must be nonempty")
    action_commit = row.get("terminal_action_commit_handoff")
    if action_commit is not None:
        if not isinstance(action_commit, dict) or not isinstance(action_commit.get("applies"), bool):
            errors.append(
                f"{trace_id}:{step_index} terminal_action_commit_handoff must be object with boolean applies"
            )
        elif action_commit["applies"]:
            if action_commit.get("eligibility") != "eligible":
                errors.append(f"{trace_id}:{step_index} action commit handoff must be eligible when applies")
            for field in ("visible_source_host", "marked_target", "committed_action", "rationale"):
                if not isinstance(action_commit.get(field), str) or not action_commit[field].strip():
                    errors.append(f"{trace_id}:{step_index} action commit handoff {field} must be nonempty")
            if action_commit.get("success_boundary") != "action_commit_only":
                errors.append(f"{trace_id}:{step_index} action commit handoff success_boundary must be action_commit_only")
            if action_commit.get("destination_content_claimed") is not False:
                errors.append(f"{trace_id}:{step_index} action commit handoff cannot claim destination content")
            if action_commit.get("reference_post_action_effect_claimed") is not False:
                errors.append(f"{trace_id}:{step_index} action commit handoff cannot claim the source trace proved the post-action effect")
            if action_commit.get("effect_class") not in {
                "navigation_or_open",
                "immediate_ui_transition",
                "business_state_mutation",
                "scroll_or_gesture",
                "other",
            }:
                errors.append(f"{trace_id}:{step_index} action commit handoff effect_class is invalid")
            for field in (
                "source_host_and_target_unambiguous",
                "action_target_and_commit_unambiguous",
                "no_later_reference_dependency",
                "reference_proves_action_was_issued",
                "post_action_effect_out_of_scope",
            ):
                if action_commit.get(field) is not True:
                    errors.append(f"{trace_id}:{step_index} action commit handoff {field} must be true")
            for field in ("required_gui_evidence", "forbidden_success_inference"):
                if not isinstance(action_commit.get(field), list) or not action_commit.get(field):
                    errors.append(f"{trace_id}:{step_index} action commit handoff {field} must be nonempty")
    if (
        isinstance(terminal_handoff, dict)
        and terminal_handoff.get("applies")
        and isinstance(action_commit, dict)
        and action_commit.get("applies")
    ):
        errors.append(f"{trace_id}:{step_index} terminal handoff modes are mutually exclusive")
    viewport = row.get("viewport_containment")
    if not isinstance(viewport, dict) or not viewport.get("expected_shell"):
        errors.append(f"{trace_id}:{step_index} viewport_containment.expected_shell is required")
    elif not isinstance(viewport.get("must_be_inside_shell"), list):
        errors.append(f"{trace_id}:{step_index} viewport_containment.must_be_inside_shell must be list")
    if not REQUIRED_REFERENCE_CHECKS.issubset(set(row.get("reference_annotation_checks") or [])):
        errors.append(f"{trace_id}:{step_index} reference_annotation_checks incomplete")
    if not REQUIRED_ENV_CHECKS.issubset(set(row.get("env_execution_checks") or [])):
        errors.append(f"{trace_id}:{step_index} env_execution_checks incomplete")
    if not isinstance(row.get("evidence"), list) or not row.get("evidence"):
        errors.append(f"{trace_id}:{step_index} evidence must be nonempty list")
    if not isinstance(row.get("blockers"), list):
        errors.append(f"{trace_id}:{step_index} blockers must be list")


def validate_trace(
    trace_id: str,
    entry: dict[str, Any],
    trace_root: Path,
    checklist_dir: Path,
    require_ready: bool,
    errors: list[str],
) -> None:
    trace_dir = checklist_dir / "traces" / trace_id
    trajectory_path = trace_root / trace_id / "trajectory.json"
    checklist_path = trace_dir / "stepwise_checklist.json"
    evidence_path = trace_dir / "evidence.json"
    local_sprite_manifest_path = trace_dir / "reference_sprites" / "manifest.json"
    local_sprite_path = trace_dir / "reference_sprites" / "sprites" / f"{trace_id}_reference_sprite.jpg"
    checklist = load_json(checklist_path, errors)
    evidence = load_json(evidence_path, errors)
    sprite_manifest = load_json(local_sprite_manifest_path, errors)
    if not checklist or not evidence or not sprite_manifest:
        return

    if checklist.get("schema") != CHECKLIST_SCHEMA:
        errors.append(f"{trace_id}: checklist schema mismatch")
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        errors.append(f"{trace_id}: evidence schema mismatch")
    if sprite_manifest.get("schema") != SPRITE_SCHEMA:
        errors.append(f"{trace_id}: sprite manifest schema mismatch")
    if checklist.get("trace_id") != trace_id or evidence.get("trace_id") != trace_id:
        errors.append(f"{trace_id}: trace_id mismatch")
    if Path(str(entry.get("checklist_path") or "")) != checklist_path:
        errors.append(f"{trace_id}: trace_index checklist_path must point inside this checklist set")
    if Path(str(entry.get("evidence_path") or "")) != evidence_path:
        errors.append(f"{trace_id}: trace_index evidence_path must point inside this checklist set")
    if Path(str(checklist.get("sprite_manifest_path") or "")) != local_sprite_manifest_path:
        errors.append(f"{trace_id}: checklist sprite_manifest_path must point to the trace-local manifest")
    if Path(str(checklist.get("sprite_path") or "")) != local_sprite_path:
        errors.append(f"{trace_id}: checklist sprite_path must point to the trace-local sprite")
    if Path(str(evidence.get("sprite_manifest_path") or "")) != local_sprite_manifest_path:
        errors.append(f"{trace_id}: evidence sprite_manifest_path must point to the trace-local manifest")
    if Path(str(evidence.get("sprite_path") or "")) != local_sprite_path:
        errors.append(f"{trace_id}: evidence sprite_path must point to the trace-local sprite")
    if Path(str(evidence.get("checklist_path") or "")) != checklist_path:
        errors.append(f"{trace_id}: evidence checklist_path must point to the trace-local checklist")
    if checklist.get("construction_stage") != "source_evidence":
        errors.append(f"{trace_id}: construction_stage must be source_evidence")
    if checklist.get("annotation_text_is_candidate_only") is not True:
        errors.append(f"{trace_id}: annotation_text_is_candidate_only must be true")
    if checklist.get("sprite_opened_and_read") is not True or evidence.get("sprite_opened_and_read") is not True:
        errors.append(f"{trace_id}: sprite_opened_and_read must be true in checklist and evidence")
    if not trajectory_path.is_file():
        errors.append(f"{trace_id}: missing source trajectory {trajectory_path}")
        return
    digest = sha256(trajectory_path)
    baseline_digests = {
        str(value or "")
        for _owner, value in (
        ("index", entry.get("trajectory_sha256")),
        ("checklist", checklist.get("trajectory_sha256")),
        ("evidence", evidence.get("trajectory_sha256")),
        )
    }
    if "" in baseline_digests:
        errors.append(f"{trace_id}: trajectory_sha256 must be nonempty in index/checklist/evidence")
    if len(baseline_digests) != 1:
        errors.append(f"{trace_id}: index/checklist/evidence baseline trajectory digests disagree")

    if not local_sprite_path.is_file():
        errors.append(f"{trace_id}: missing sprite_path {local_sprite_path}")
    sprite_traces = sprite_manifest.get("traces")
    if not isinstance(sprite_traces, list) or len(sprite_traces) != 1:
        errors.append(f"{trace_id}: reference sprite manifest must contain exactly one trace")
    elif sprite_traces[0].get("trace_id") != trace_id:
        errors.append(f"{trace_id}: reference sprite manifest contains another trace")

    expected = expected_kept_steps(trajectory_path, errors)
    rows = checklist.get("rows")
    if not isinstance(rows, list):
        errors.append(f"{trace_id}: rows must be list")
        return
    observed = [row.get("step_index") for row in rows if isinstance(row, dict)]
    baseline_digest = next(iter(baseline_digests), "")
    if baseline_digest == digest:
        if observed != expected:
            errors.append(f"{trace_id}: checklist steps {observed} do not match retained steps {expected}")
    elif len(baseline_digests) == 1 and baseline_digest:
        errors.append(
            f"{trace_id}: source trajectory changed since the checklist was built "
            f"({baseline_digest[:12]} -> {digest[:12]}); rebuild the checklist from the current trace"
        )
    for row in rows:
        if isinstance(row, dict):
            validate_row(row, trace_id, trace_root / trace_id, errors)
    row_blockers = [
        row.get("step_index")
        for row in rows
        if isinstance(row, dict) and row.get("blockers")
    ]
    blockers = checklist.get("blockers")
    if not isinstance(blockers, list):
        errors.append(f"{trace_id}: checklist blockers must be list")
    if require_ready:
        if checklist.get("status") != "ready":
            errors.append(f"{trace_id}: checklist status must be ready")
        if entry.get("status") != "ready":
            errors.append(f"{trace_id}: trace_index status must be ready")
        if blockers:
            errors.append(f"{trace_id}: ready checklist cannot have blockers")
        if row_blockers:
            errors.append(f"{trace_id}: ready checklist rows have blockers at {row_blockers}")
        if evidence.get("blockers"):
            errors.append(f"{trace_id}: ready evidence cannot have blockers")
    commands = "\n".join(str(command) for command in evidence.get("commands") or [])
    if "render_marked_trace.py" not in commands or f"--traces {trace_id}" not in commands:
        errors.append(f"{trace_id}: evidence must contain per-trace sprite command")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--checklist-dir", required=True, type=Path)
    parser.add_argument("--traces", default="")
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument(
        "--allow-indexed-superset",
        action="store_true",
        help="validate only --traces while allowing the checklist set index to contain additional trace-local entries",
    )
    args = parser.parse_args()

    errors: list[str] = []
    for name in FORBIDDEN_ROOT_CHECKLISTS:
        if (args.checklist_dir / name).exists():
            errors.append(f"forbidden root-level aggregate checklist: {args.checklist_dir / name}")
    manifest = load_json(args.checklist_dir / "manifest.json", errors)
    index = load_json(args.checklist_dir / "trace_index.json", errors)
    if not manifest or not index:
        print(json.dumps({"ok": False, "problems": errors}, ensure_ascii=False, indent=2))
        return 1
    if manifest.get("schema") != SET_SCHEMA:
        errors.append("manifest schema mismatch")
    if index.get("schema") != INDEX_SCHEMA:
        errors.append("trace_index schema mismatch")
    if manifest.get("source_trace_root") != str(args.trace_root):
        errors.append("manifest source_trace_root mismatch")
    if manifest.get("aggregate_stepwise_checklist_forbidden") is not True:
        errors.append("manifest must set aggregate_stepwise_checklist_forbidden=true")

    entries = index.get("traces")
    if not isinstance(entries, list):
        errors.append("trace_index.traces must be list")
        entries = []
    selected = {item.strip() for item in args.traces.split(",") if item.strip()}
    indexed = {str(entry.get("trace_id")): entry for entry in entries if isinstance(entry, dict)}
    expected_ids = selected or {
        path.name
        for path in args.trace_root.iterdir()
        if path.is_dir() and (path / "trajectory.json").is_file()
    }
    indexed_ids = set(indexed)
    if args.allow_indexed_superset:
        missing_selected = expected_ids - indexed_ids
        if missing_selected:
            errors.append(f"trace_index is missing selected ids {sorted(missing_selected)}")
        if manifest.get("trace_ids") != sorted(indexed_ids):
            errors.append("manifest.trace_ids must match the complete indexed trace set")
        if manifest.get("trace_count") != len(indexed_ids):
            errors.append("manifest.trace_count must match the complete indexed trace set")
    else:
        if indexed_ids != expected_ids:
            errors.append(f"trace_index ids {sorted(indexed)} do not match selected ids {sorted(expected_ids)}")
        if manifest.get("trace_ids") != sorted(expected_ids):
            errors.append("manifest.trace_ids must be the sorted selected trace ids")
        if manifest.get("trace_count") != len(expected_ids):
            errors.append("manifest.trace_count mismatch")
    for trace_id in sorted(expected_ids):
        entry = indexed.get(trace_id)
        if entry:
            validate_trace(trace_id, entry, args.trace_root, args.checklist_dir, args.require_ready, errors)
    if args.require_ready and manifest.get("status") != "ready":
        errors.append("manifest.status must be ready")

    print(
        json.dumps(
            {"ok": not errors, "checklist_dir": str(args.checklist_dir), "problems": errors},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
