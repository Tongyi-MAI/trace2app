
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trace2world_pipeline.app_spec.global_schema_downstream_projection import (
    build_downstream_required_ref_payload,
    build_downstream_schema_projection,
)
from trace2world_pipeline.app_spec.global_execution_schema import normalize_field_path


REQUIRED_SOURCE_ARTIFACTS = [
    "phase_post_processing/tasks/tasks.json",
    "phase_global_reasoning/debug/capture_actions.json",
    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/contract_replay_plan.json",
    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/runtime_binding_manifest.json",
    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/domain_contracts.json",
    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/action_effect_contracts.json",
    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/runtime_transaction_contracts.json",
    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/dom_binding_contracts.json",
    "phase_sdk_generation/runtime/business_logic.js",
    "phase_sdk_generation/runtime/route_manifest.js",
]

STATE_KERNEL_REQUIRED_SOURCE_ARTIFACTS = [
    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/reward_authority_bundle.json",
]


@dataclass(frozen=True)
class InstrumentationOnlyResult:
    success: bool
    output_dir: str
    trace_count: int
    checkpoint_count: int
    evaluator_count: int
    test_flow_count: int
    artifacts: dict[str, str]


def run_contract_instrumentation(
    output_dir: str | Path,
    *,
    source_env_dir: str | Path | None = None,
    terminal_contract_root: str | Path | None = None,
    clear_existing: bool = True,
    semantic_checkpoint_planner: dict[str, Any] | None = None,
    ignore_semantic_obligation_matrix: bool = False,
    generate_evaluators: bool = True,
    generate_test_logic: bool = True,
    generate_instrumented_business_logic: bool = True,
) -> InstrumentationOnlyResult:

    output = Path(output_dir)
    source = Path(source_env_dir) if source_env_dir else output
    if not output.exists():
        raise FileNotFoundError(f"output_dir does not exist: {output}")

    _validate_required_artifacts(output)
    semantic_obligation_matrix = None
    semantic_obligation_path = output / "phase_post_processing/instrumentation/task_semantic_obligation_matrix.json"
    if not ignore_semantic_obligation_matrix and semantic_obligation_path.is_file():
        semantic_obligation_matrix = _load_json(semantic_obligation_path)
    if clear_existing:
        _clear_generated_artifacts(output)
    if not generate_instrumented_business_logic:
        _switch_page_runtime_sdk(output, "business_logic.js")

    tasks = _load_json(output / "phase_post_processing/tasks/tasks.json")["tasks"]
    capture_actions = _load_json(output / "phase_global_reasoning/debug/capture_actions.json")
    replay_plan = _load_json(
        output
        / "phase_page_environment/semantic_api/08_spec_compiled_execution_views/contract_replay_plan.json"
    )
    runtime_manifest = _load_json(
        output
        / "phase_page_environment/semantic_api/08_spec_compiled_execution_views/runtime_binding_manifest.json"
    )
    compiled_dir = output / "phase_page_environment/semantic_api/08_spec_compiled_execution_views"
    action_effect_contracts = _load_json(compiled_dir / "action_effect_contracts.json")
    runtime_transaction_contracts = _load_json(compiled_dir / "runtime_transaction_contracts.json")
    dom_binding_contracts = _load_json(compiled_dir / "dom_binding_contracts.json")
    domain_contracts = _load_json(compiled_dir / "domain_contracts.json")
    evaluator_state_view = _load_optional_json(compiled_dir / "evaluator_state_view.json")
    reward_authority_bundle = _load_optional_json(compiled_dir / "reward_authority_bundle.json") or None
    runtime_state_obligations = _load_optional_json(
        output / "phase_instance_splitting/debug/runtime_state_obligations.json"
    )
    page_execution_obligations = _load_optional_json(
        output / "phase_consolidation/site_model/page_execution_obligations_by_page.json"
    )
    if terminal_contract_root:
        terminal_candidates = load_terminal_policy_candidates_from_rewritten_root(terminal_contract_root)
        terminal_candidates_artifact = "phase_post_processing/terminal_policy/terminal_policy_candidates.from_rewritten_trace.json"
    else:
        raise ValueError(
            "terminal_contract_root is required for contract instrumentation; "
            "preprocess-equivalent terminal fallback is not allowed."
        )
    terminal_verified = verify_terminal_policies(
        terminal_candidates,
        replay_plan,
        action_effect_contracts=action_effect_contracts,
    )
    from trace2world_pipeline.verification.instrumentation_contract_bundle import build_instrumentation_contract_bundle

    instrumentation_bundle, checkpoint_matrix_source, instrumentation_bundle_report = (
        build_instrumentation_contract_bundle(
            action_effect_contracts=action_effect_contracts,
            runtime_transaction_contracts=runtime_transaction_contracts,
            dom_binding_contracts=dom_binding_contracts,
            contract_replay_plan=replay_plan,
            runtime_binding_manifest=runtime_manifest,
            domain_contracts=domain_contracts,
            runtime_state_obligations=runtime_state_obligations,
            page_execution_obligations=page_execution_obligations,
            terminal_verified=terminal_verified,
            terminal_candidates=terminal_candidates,
            reward_authority_bundle=reward_authority_bundle,
        )
    )
    semantic_plan = build_semantic_checkpoint_plan_stub(
        tasks,
        capture_actions,
        replay_plan,
        runtime_manifest,
        terminal_verified,
        semantic_checkpoint_planner or {},
    )
    matrix, matrix_report = compile_checkpoint_matrix_from_bundle(
        instrumentation_bundle,
        runtime_manifest,
    )
    runtime_state_schema = compile_runtime_state_schema(matrix)
    downstream_schema_projection = merge_task_supervision_into_global_execution_schema(
        output,
        matrix,
        runtime_state_schema,
    )
    runtime_state_schema = compile_runtime_state_schema(matrix)
    if downstream_schema_projection:
        _apply_schema_projection_to_runtime_state_schema(runtime_state_schema, downstream_schema_projection)
    post_projection_failures = _post_projection_matrix_quality_failures(matrix)
    if post_projection_failures:
        matrix_report.setdefault("quality_gate_failures", []).extend(post_projection_failures)
        matrix_report["status"] = "failed"
        for failure in post_projection_failures:
            trace_id = str(failure.get("trace_id") or "")
            if not trace_id:
                continue
            trace_report = matrix_report.setdefault("trace_reports", {}).setdefault(trace_id, {
                "trace_id": trace_id,
                "source": "post_projection_matrix_quality",
                "checkpoint_count": 0,
                "quality_gate_failures": [],
                "contract_gaps": [],
                "status": "passed",
            })
            trace_report.setdefault("quality_gate_failures", []).append(failure)
            trace_report["status"] = "failed"
    root_schema = _load_optional_json(output / "phase_consolidation/schema_registry/global_execution_schema.json")
    if isinstance(root_schema, dict):
        runtime_state_schema["global_schema_hash"] = _stable_payload_hash(root_schema)
    runtime_state_schema["global_schema_source"] = "phase_consolidation/schema_registry/global_execution_schema.json"
    runtime_state_schema["downstream_projection_source"] = (
        "phase_consolidation/schema_registry/global_execution_schema_downstream_projection.json"
    )
    bindings = compile_runtime_event_bindings(matrix, runtime_manifest)
    from trace2world_pipeline.verification.action_authority import (
        build_action_execution_contracts_from_replay_plan,
    )

    action_execution_contracts = build_action_execution_contracts_from_replay_plan(
        replay_plan,
        matrix,
        action_effect_contracts=action_effect_contracts,
    )
    missing_source_actions = action_execution_contracts.get("missing_source_actions") or []
    duplicate_action_ids = action_execution_contracts.get("duplicate_action_ids") or []
    if missing_source_actions or duplicate_action_ids:
        raise RuntimeError(
            "ActionExecutionContract compilation failed: "
            f"missing_source_actions={len(missing_source_actions)}, "
            f"duplicate_action_ids={len(duplicate_action_ids)}"
        )
    evaluators = compile_evaluators(tasks, matrix, terminal_verified) if generate_evaluators else None
    environment_state_binding = None
    if isinstance(evaluator_state_view, dict) and evaluator_state_view.get("format") == "ghostweb_evaluator_state_view":
        environment_state_binding = {
            "format": "ghostweb_environment_state_evaluator_binding",
            "schema_revision": 1,
            "state_source": "finalized_environment_state_snapshot",
            "evaluation_handle_fields": ["sid", "episode_epoch", "revision", "snapshot_id", "state_hash"],
            "evaluator_state_view_ref": "phase_page_environment/semantic_api/08_spec_compiled_execution_views/evaluator_state_view.json",
            "transaction_manifest_ref": "phase_page_environment/semantic_api/08_spec_compiled_execution_views/transaction_manifest.json",
            "runtime_state_registry_ref": "phase_page_environment/semantic_api/08_spec_compiled_execution_views/runtime_state_registry.json",
            "schema_hash": evaluator_state_view.get("schema_hash"),
            "world_hash": evaluator_state_view.get("world_hash"),
            "bundle_hash": evaluator_state_view.get("bundle_hash"),
            "dom_evidence_role": "visible_projection_only",
            "instrumentation_mutation_allowed": False,
        }
        runtime_state_schema["canonical_state_source"] = "finalized_environment_state_snapshot"
        runtime_state_schema["environment_state_binding_ref"] = (
            "phase_post_processing/instrumentation/instrumentation_state_bindings.json"
        )
        bindings["canonical_state_source"] = "environment_state_kernel_events"
        bindings["environment_state_binding_ref"] = (
            "phase_post_processing/instrumentation/instrumentation_state_bindings.json"
        )
        if isinstance(evaluators, dict):
            evaluators["state_source"] = "finalized_environment_state_snapshot"
            evaluators["environment_state_binding_ref"] = (
                "phase_post_processing/evaluators/evaluator_state_bindings.json"
            )
            evaluator_rows = evaluators.get("evaluators", [])
            if isinstance(evaluator_rows, list):
                for evaluator in evaluator_rows:
                    if isinstance(evaluator, dict):
                        evaluator["state_source"] = "finalized_environment_state_snapshot"
                        evaluator["requires_evaluation_handle"] = True
    test_logic = compile_test_logic(matrix, terminal_verified) if generate_test_logic else None
    provenance = build_source_env_provenance(output, source)
    instrumented_code = (
        build_instrumented_business_logic(output, bindings, matrix)
        if generate_instrumented_business_logic
        else None
    )
    generation_result = build_generation_result(
        output,
        terminal_verified,
        matrix,
        evaluators,
        test_logic,
        provenance,
        generate_instrumented_business_logic=generate_instrumented_business_logic,
    )

    artifacts = {
        "terminal_candidates": terminal_candidates_artifact,
        "terminal_verified": "phase_post_processing/terminal_policy/terminal_policy_verified.json",
        "instrumentation_contract_bundle": "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/instrumentation_contract_bundle.json",
        "checkpoint_contract_matrix_source": "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/checkpoint_contract_matrix_source.json",
        "instrumentation_contract_validation_report": "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/instrumentation_contract_validation_report.json",
        "checkpoint_matrix": "phase_post_processing/instrumentation/checkpoint_matrix.json",
        "effect_checkpoint_matrix": "phase_post_processing/instrumentation/effect_checkpoint_matrix.json",
        "runtime_state_schema": "phase_post_processing/instrumentation/runtime_state_schema.json",
        "global_execution_schema_downstream_projection": "phase_consolidation/schema_registry/global_execution_schema_downstream_projection.json",
        "semantic_checkpoint_plan": "phase_post_processing/instrumentation/semantic_checkpoint_plan.json",
        "checkpoint_matrix_validation_report": "phase_post_processing/instrumentation/checkpoint_matrix_validation_report.json",
        "runtime_event_bindings": "phase_post_processing/instrumentation/runtime_event_bindings.json",
        "action_execution_contracts": "phase_post_processing/action_authority/action_execution_contracts.json",
        "source_env_provenance": "phase_post_processing/instrumentation/source_env_provenance.json",
        "generation_result": "phase_post_processing/debug/data_manager/generation_result.json",
    }
    if semantic_obligation_matrix:
        artifacts["task_semantic_obligation_matrix"] = (
            "phase_post_processing/instrumentation/task_semantic_obligation_matrix.json"
        )
    if environment_state_binding:
        artifacts["instrumentation_state_bindings"] = "phase_post_processing/instrumentation/instrumentation_state_bindings.json"
        artifacts["evaluator_state_bindings"] = "phase_post_processing/evaluators/evaluator_state_bindings.json"
    if generate_instrumented_business_logic:
        artifacts["instrumented_business_logic"] = "phase_sdk_generation/runtime/instrumented_business_logic.js"
    if generate_evaluators:
        artifacts["evaluators"] = "phase_post_processing/evaluators/evaluators.json"
    if generate_test_logic:
        artifacts["test_logic"] = "phase_post_processing/test_logic/test_flows.json"
        artifacts["compiled_test_flows"] = "phase_sdk_generation/validation/test_flows/test_flows.js"

    _write_json(output / artifacts["terminal_candidates"], terminal_candidates)
    _write_json(output / artifacts["terminal_verified"], terminal_verified)
    _write_json(output / artifacts["instrumentation_contract_bundle"], instrumentation_bundle)
    _write_json(output / artifacts["checkpoint_contract_matrix_source"], checkpoint_matrix_source)
    _write_json(output / artifacts["instrumentation_contract_validation_report"], instrumentation_bundle_report)
    if semantic_obligation_matrix:
        _write_json(output / artifacts["task_semantic_obligation_matrix"], semantic_obligation_matrix)
    _write_json(output / artifacts["semantic_checkpoint_plan"], semantic_plan)
    _write_json(output / artifacts["checkpoint_matrix"], matrix)
    _write_json(output / artifacts["effect_checkpoint_matrix"], matrix)
    _write_json(output / artifacts["runtime_state_schema"], runtime_state_schema)
    if downstream_schema_projection:
        _write_json(output / artifacts["global_execution_schema_downstream_projection"], downstream_schema_projection)
        _write_json(
            output / "phase_consolidation/projections/schema/global_execution_schema_downstream_projection.json",
            downstream_schema_projection,
        )
    _write_json(output / artifacts["checkpoint_matrix_validation_report"], matrix_report)
    _write_json(output / artifacts["runtime_event_bindings"], bindings)
    _write_json(output / artifacts["action_execution_contracts"], action_execution_contracts)
    if environment_state_binding:
        _write_json(output / artifacts["instrumentation_state_bindings"], environment_state_binding)
        _write_json(output / artifacts["evaluator_state_bindings"], environment_state_binding)
    _write_json(output / artifacts["source_env_provenance"], provenance)
    if instrumented_code is not None:
        _write_text(output / artifacts["instrumented_business_logic"], instrumented_code)
        _switch_page_runtime_sdk(output, "instrumented_business_logic.js")
    if evaluators is not None:
        _write_json(output / artifacts["evaluators"], evaluators)
    if test_logic is not None:
        _write_json(output / artifacts["test_logic"], test_logic)
        _write_playwright_flow_files(output, test_logic)
        _write_text(output / artifacts["compiled_test_flows"], _compile_test_flows_js(test_logic))
    _write_json(output / artifacts["generation_result"], generation_result)

    return InstrumentationOnlyResult(
        success=True,
        output_dir=str(output),
        trace_count=len(tasks),
        checkpoint_count=len(matrix["checkpoints"]),
        evaluator_count=len(evaluators["evaluators"]) if evaluators is not None else 0,
        test_flow_count=len(test_logic["trace_flows"]) if test_logic is not None else 0,
        artifacts=artifacts,
    )


def build_semantic_checkpoint_plan_stub(
    tasks: list[dict[str, Any]],
    capture_actions: dict[str, Any],
    replay_plan: dict[str, Any],
    runtime_manifest: dict[str, Any],
    terminal_verified: dict[str, Any],
    planner_config: dict[str, Any],
) -> dict[str, Any]:

    replay_by_trace = {trace.get("trace_id"): trace for trace in replay_plan.get("traces", []) if isinstance(trace, dict)}
    enabled = bool(planner_config.get("enabled"))
    if enabled:
        draft_path = (
            planner_config.get("planner_draft_path")
            or planner_config.get("draft_artifact")
            or planner_config.get("llm_plan_path")
        )
        if not draft_path:
            raise RuntimeError(
                "semantic_checkpoint_planner.enabled requires planner_draft_path/draft_artifact/llm_plan_path; "
                "refusing to emit llm_requested_stub as semantic planner output"
            )
        return _semantic_checkpoint_plan_from_draft(draft_path, tasks, replay_plan, runtime_manifest, terminal_verified)

    traces = []
    for task in tasks:
        task_id = task["id"]
        replay_trace = replay_by_trace.get(task_id, {})
        steps = replay_trace.get("steps", [])
        traces.append({
            "trace_id": task_id,
            "planner_status": "llm_planner_not_run" if not enabled else "llm_planner_requested_not_executed_by_stub",
            "task_instruction": task.get("name") or task.get("description", ""),
            "terminal_policy": terminal_verified["trace_policies"].get(task_id, {}),
            "context_summary": {
                "capture_action_count": len(capture_actions.get(task_id, [])),
                "replay_action_count": len(steps),
                "transaction_count": sum(1 for step in steps if step.get("transaction")),
                "state_ref_count": len(_dedupe([ref for step in steps for ref in _state_refs(step)])),
                "runtime_bridge_status": _runtime_bridge_status(runtime_manifest),
            },
        })
    return {
        "format": "ghostweb_semantic_checkpoint_plan",
        "schema_revision": 1,
        "source": "contract_instrumentation.semantic_planner_boundary",
        "planner_mode": "llm_disabled_stub" if not enabled else "llm_requested_stub",
        "llm_required_for_final_semantic_quality": True,
        "traces": traces,
        "summary": {
            "trace_count": len(traces),
            "planner_executed_count": 0,
            "planner_not_run_count": len(traces),
        },
    }


def merge_task_supervision_into_global_execution_schema(
    output_dir: Path,
    checkpoint_matrix: dict[str, Any],
    runtime_state_schema: dict[str, Any],
) -> dict[str, Any]:

    schema_path = output_dir / "phase_consolidation/schema_registry/global_execution_schema.json"
    page_env_path = output_dir / "phase_page_environment/debug/page_environment_spec/page_environment_specs.json"
    runtime_transactions_path = (
        output_dir
        / "phase_page_environment/semantic_api/08_spec_compiled_execution_views/runtime_transaction_contracts.json"
    )
    existing_schema = _load_optional_json(schema_path)
    if not isinstance(existing_schema, dict):
        existing_schema = {}
    page_environment_specs = _load_optional_json(page_env_path)
    runtime_transactions = _load_optional_json(runtime_transactions_path)
    ref_payload = build_downstream_required_ref_payload(
        page_environment_specs=page_environment_specs if isinstance(page_environment_specs, dict) else None,
        runtime_transactions=runtime_transactions if isinstance(runtime_transactions, dict) else None,
        checkpoint_matrix=checkpoint_matrix,
        runtime_state_schema=runtime_state_schema,
    )
    projection = build_downstream_schema_projection(
        existing_schema,
        ref_payload["required_refs"],
        source="contract_instrumentation.full_downstream_projection",
    )
    _apply_schema_projection_to_checkpoint_matrix(checkpoint_matrix, projection)
    _apply_schema_projection_to_runtime_state_schema(runtime_state_schema, projection)
    projection["source_artifacts"] = [
        "phase_consolidation/schema_registry/global_execution_schema.json",
        "phase_page_environment/debug/page_environment_spec/page_environment_specs.json",
        "phase_page_environment/semantic_api/08_spec_compiled_execution_views/runtime_transaction_contracts.json",
        "phase_post_processing/instrumentation/checkpoint_matrix.json",
        "phase_post_processing/instrumentation/runtime_state_schema.json",
    ]
    projection["input_presence"] = {
        "global_execution_schema": bool(existing_schema),
        "page_environment_specs": isinstance(page_environment_specs, dict),
        "runtime_transaction_contracts": isinstance(runtime_transactions, dict),
        "checkpoint_matrix": isinstance(checkpoint_matrix, dict),
        "runtime_state_schema": isinstance(runtime_state_schema, dict),
    }
    if existing_schema:
        projection["root_schema_hash"] = _stable_payload_hash(existing_schema)
    projection["downstream_required_ref_summary"] = ref_payload["summary"]
    projection["task_supervision_obligation_count"] = ref_payload["summary"].get("schema_obligation_count", 0)
    return projection


ROOT_RUNTIME_FIELD_PREFIXES = (
    "searchForm.",
    "stationPicker.",
    "queryState.",
    "selection.",
    "identityForm.",
    "identityProfile.",
    "personalIdentity.",
)

DOWNSTREAM_RUNTIME_FIELD_PREFIXES = (
    "taskOutcome.",
    "checkpointState.",
    "draft.",
    "runtimeState.",
    "runtime.",
    "ui.",
)

PAGE_RUNTIME_FIELD_NAMES = frozenset({
    "page",
    "current_page",
    "terminal_page",
    "target_filename",
    "runtime.page",
})


def _apply_schema_projection_to_checkpoint_matrix(
    checkpoint_matrix: dict[str, Any],
    projection: dict[str, Any],
) -> None:
    alias_index, alias_norm_index = _projection_alias_indexes(projection)
    for row in checkpoint_matrix.get("checkpoints", []) if isinstance(checkpoint_matrix, dict) else []:
        if not isinstance(row, dict):
            continue
        state = row.get("global_runtime_state")
        if not isinstance(state, dict):
            continue
        legacy_refs = [str(ref) for ref in state.get("state_refs", []) if ref]
        state["legacy_state_refs"] = list(state.get("legacy_state_refs") or legacy_refs)
        canonical_refs: list[str] = []
        downstream_refs: list[str] = []
        for ref in legacy_refs:
            owner = _projection_owner_for_ref(ref, alias_index, alias_norm_index)
            _append_projected_ref(owner, canonical_refs, downstream_refs)
        canonical_diff, downstream_diff = _project_expected_state_diff(
            state.get("expected_state_diff") or {},
            legacy_refs,
            alias_index,
            alias_norm_index,
        )
        if canonical_diff:
            state["canonical_expected_state_diff"] = canonical_diff
            for ref in canonical_diff:
                _append_unique(canonical_refs, ref)
        if downstream_diff:
            state["downstream_expected_state_diff"] = downstream_diff
            for ref in downstream_diff:
                _append_unique(downstream_refs, ref)
        state["canonical_state_refs"] = _dedupe([*state.get("canonical_state_refs", []), *canonical_refs])
        state["downstream_extension_refs"] = _dedupe([*state.get("downstream_extension_refs", []), *downstream_refs])


def _apply_schema_projection_to_runtime_state_schema(
    runtime_state_schema: dict[str, Any],
    projection: dict[str, Any],
) -> None:
    alias_index, alias_norm_index = _projection_alias_indexes(projection)
    for checkpoint in runtime_state_schema.get("checkpoints", []) if isinstance(runtime_state_schema, dict) else []:
        if not isinstance(checkpoint, dict):
            continue
        legacy_refs = [str(ref) for ref in checkpoint.get("state_refs", []) if ref]
        checkpoint["legacy_state_refs"] = list(checkpoint.get("legacy_state_refs") or legacy_refs)
        canonical_refs: list[str] = []
        downstream_refs: list[str] = []
        for ref in legacy_refs:
            owner = _projection_owner_for_ref(ref, alias_index, alias_norm_index)
            _append_projected_ref(owner, canonical_refs, downstream_refs)
        checkpoint["canonical_state_refs"] = _dedupe([*checkpoint.get("canonical_state_refs", []), *canonical_refs])
        checkpoint["downstream_extension_refs"] = _dedupe([*checkpoint.get("downstream_extension_refs", []), *downstream_refs])
        for field in checkpoint.get("fields", []) if isinstance(checkpoint.get("fields"), list) else []:
            if not isinstance(field, dict):
                continue
            runtime_ref = str(field.get("runtime_ref") or "")
            owner = _projection_owner_for_ref(runtime_ref, alias_index, alias_norm_index) if runtime_ref else None
            if not owner:
                owner = _owner_from_field_name_and_refs(str(field.get("field") or ""), legacy_refs, alias_index, alias_norm_index)
            _apply_owner_to_runtime_field(field, owner)
            if field.get("owner_kind") == "root_schema":
                _append_unique(canonical_refs, field.get("canonical_schema_ref"))
            elif field.get("owner_kind") == "downstream_extension":
                _append_unique(downstream_refs, field.get("extension_ref"))
        checkpoint["canonical_state_refs"] = _dedupe([*checkpoint.get("canonical_state_refs", []), *canonical_refs])
        checkpoint["downstream_extension_refs"] = _dedupe([*checkpoint.get("downstream_extension_refs", []), *downstream_refs])
    runtime_state_schema["downstream_projection_applied"] = True
    _refresh_runtime_state_schema_owner_summary(runtime_state_schema, enforce=True)


def _projection_alias_indexes(
    projection: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    exact: dict[str, dict[str, Any]] = {}
    norm: dict[str, dict[str, Any]] = {}
    for item in projection.get("ref_aliases", []) if isinstance(projection, dict) else []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        exact[ref] = item
        normalized = normalize_field_path(ref)
        if normalized and normalized not in norm:
            norm[normalized] = item
    for field in projection.get("projection_fields", []) if isinstance(projection, dict) else []:
        if not isinstance(field, dict):
            continue
        field_path = str(field.get("field_path") or "").strip()
        if not field_path:
            continue
        row = {
            "ref": field_path,
            "owner_field_path": field_path,
            "owner_kind": field.get("owner_kind", "downstream_extension"),
            "not_root_reason": field.get("not_root_reason", ""),
            "reason": "projection_field_self_owner",
        }
        exact.setdefault(field_path, row)
        normalized = normalize_field_path(field_path)
        if normalized:
            norm.setdefault(normalized, row)
    return exact, norm


def _projection_owner_for_ref(
    ref: str,
    alias_index: dict[str, dict[str, Any]],
    alias_norm_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    text = str(ref or "").strip()
    if not text:
        return None
    if text.startswith("global_execution_schema:"):
        field_path = text.split(":", 1)[1]
        return {"owner_kind": "root_schema", "owner_field_path": field_path}
    alias = alias_index.get(text) or alias_norm_index.get(normalize_field_path(text))
    if alias:
        return alias
    normalized = normalize_field_path(text)
    if normalized.startswith(ROOT_RUNTIME_FIELD_PREFIXES):
        return {"owner_kind": "root_schema", "owner_field_path": normalized}
    if normalized.startswith(DOWNSTREAM_RUNTIME_FIELD_PREFIXES):
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": normalized,
            "not_root_reason": _not_root_reason_for_runtime_path(normalized),
        }
    return None


def _append_projected_ref(
    owner: dict[str, Any] | None,
    canonical_refs: list[str],
    downstream_refs: list[str],
) -> None:
    if not owner:
        return
    owner_field_path = str(owner.get("owner_field_path") or "").strip()
    if not owner_field_path:
        return
    if owner.get("owner_kind") == "root_schema":
        _append_unique(canonical_refs, f"global_execution_schema:{owner_field_path}")
    elif owner.get("owner_kind") == "downstream_extension":
        _append_unique(downstream_refs, owner_field_path)


def _append_unique(target: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in target:
        target.append(text)


def _project_expected_state_diff(
    expected_diff: dict[str, Any],
    legacy_refs: list[str],
    alias_index: dict[str, dict[str, Any]],
    alias_norm_index: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical: dict[str, Any] = {}
    downstream: dict[str, Any] = {}
    if not isinstance(expected_diff, dict):
        return canonical, downstream
    owners = [
        _projection_owner_for_ref(ref, alias_index, alias_norm_index)
        for ref in legacy_refs
    ]
    owners = [owner for owner in owners if owner]
    for field, value in expected_diff.items():
        field_text = str(field or "").strip()
        projection_field = field_text
        projection_value = _raw_expected_value(value)


        if isinstance(projection_value, dict) and len(projection_value) == 1:
            nested_field, nested_value = next(iter(projection_value.items()))
            if str(nested_field or "").strip():
                projection_field = str(nested_field).strip()
                projection_value = _raw_expected_value(nested_value)
        owner = _default_runtime_field_owner(projection_field)
        if not owner:
            owner = _projection_owner_for_ref(projection_field, alias_index, alias_norm_index)
        if not owner:
            owner = _owner_from_field_name(projection_field, owners)
        if not owner and len(owners) == 1 and len(expected_diff) == 1:
            owner = owners[0]
        if not owner:
            owner = _default_runtime_field_owner(projection_field)
        if not owner:
            continue
        owner_path = str(owner.get("owner_field_path") or owner.get("extension_ref") or "").strip()
        if not owner_path:
            continue
        payload = {"field": projection_field, "value": projection_value}
        if owner.get("owner_kind") == "root_schema":
            canonical[f"global_execution_schema:{owner_path}"] = payload
        elif owner.get("owner_kind") == "downstream_extension":
            downstream[owner_path] = payload
    return canonical, downstream


def _owner_from_field_name_and_refs(
    field_name: str,
    legacy_refs: list[str],
    alias_index: dict[str, dict[str, Any]],
    alias_norm_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    owner = _projection_owner_for_ref(field_name, alias_index, alias_norm_index)
    if owner:
        return owner
    state_owner = _runtime_state_owner_from_state_field(field_name, legacy_refs)
    if state_owner:
        return state_owner
    owners = [
        _projection_owner_for_ref(ref, alias_index, alias_norm_index)
        for ref in legacy_refs
    ]
    owners = [owner for owner in owners if owner]
    return _owner_from_field_name(field_name, owners)


def _runtime_state_owner_from_state_field(field_name: str, legacy_refs: list[str]) -> dict[str, str] | None:
    field_text = str(field_name or "").strip()
    if not field_text.startswith("state::"):
        return None
    for ref in legacy_refs:
        ref_text = str(ref or "").strip()
        if not ref_text.startswith("state::"):
            continue
        if field_text == ref_text:
            suffix = ""
        elif field_text.startswith(f"{ref_text}."):
            suffix = field_text[len(ref_text) + 1 :]
        else:
            continue
        runtime_ref = ref_text.replace("state::", "runtimeState.", 1)
        if suffix:
            runtime_ref = f"{runtime_ref}.{suffix}"
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": runtime_ref,
            "not_root_reason": _not_root_reason_for_runtime_path(runtime_ref),
        }
    return None


def _owner_from_field_name(field_name: str, owners: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized_field = normalize_field_path(field_name)
    for owner in owners:
        owner_path = str(owner.get("owner_field_path") or "")
        normalized_owner = normalize_field_path(owner_path)
        if not normalized_owner:
            continue
        if normalized_owner.endswith(f".{normalized_field}") or normalized_owner == normalized_field:
            return owner
        if normalized_field and normalized_field in normalized_owner.split("."):
            return owner
    if len(owners) == 1 and field_name in {"selected_value", "value", "query", "draft"}:
        return owners[0]
    return None


def _apply_owner_to_runtime_field(field: dict[str, Any], owner: dict[str, Any] | None) -> None:
    if not owner:
        if field.get("structured_required"):
            field["owner_kind"] = field.get("owner_kind") or "unresolved"
        return
    owner_path = str(owner.get("owner_field_path") or "").strip()
    owner_kind = str(owner.get("owner_kind") or "").strip()
    if owner_kind == "root_schema" and owner_path:
        field["owner_kind"] = "root_schema"
        field["canonical_schema_ref"] = f"global_execution_schema:{owner_path}"
        field["extension_ref"] = ""
        field["not_root_reason"] = ""
    elif owner_kind == "downstream_extension" and owner_path:
        field["owner_kind"] = "downstream_extension"
        field["canonical_schema_ref"] = ""
        field["extension_ref"] = owner_path
        field["not_root_reason"] = str(owner.get("not_root_reason") or _not_root_reason_for_runtime_path(owner_path))


def _explicit_canonical_state_refs(state_refs: list[str]) -> list[str]:
    refs: list[str] = []
    for ref in state_refs:
        text = str(ref or "").strip()
        if text.startswith("global_execution_schema:"):
            _append_unique(refs, text)
    return refs


def _runtime_ref_for_field(field_name: str, state_refs: list[str]) -> str:
    if not state_refs:
        return ""
    normalized_field = normalize_field_path(field_name)
    for ref in state_refs:
        normalized_ref = normalize_field_path(ref)
        if normalized_ref == normalized_field or normalized_ref.endswith(f".{normalized_field}"):
            return ref
    if len(state_refs) == 1:
        return state_refs[0]
    return ""


def _not_root_reason_for_runtime_path(path: str) -> str:
    if path.startswith("taskOutcome."):
        return "terminal_outcome_downstream_projection"
    if path.startswith("checkpointState."):
        return "checkpoint_local_state"
    if path.startswith("draft."):
        return "task_scoped_draft_runtime_state"
    if path.startswith("ui."):
        return "ui_runtime_state_not_business_state"
    if path.startswith("runtime."):
        return "route_runtime_state_not_business_state"
    if path.startswith("runtimeState."):
        return "runtime_checkpoint_local_state"
    return "downstream_extension_state"


def _refresh_runtime_state_schema_owner_summary(
    runtime_state_schema: dict[str, Any],
    *,
    enforce: bool = False,
) -> None:
    root_owned = 0
    downstream_owned = 0
    unresolved_required: list[dict[str, Any]] = []
    legacy_alias_only = 0
    for checkpoint in runtime_state_schema.get("checkpoints", []) if isinstance(runtime_state_schema, dict) else []:
        if not isinstance(checkpoint, dict):
            continue
        for field in checkpoint.get("fields", []) if isinstance(checkpoint.get("fields"), list) else []:
            if not isinstance(field, dict):
                continue
            owner_kind = str(field.get("owner_kind") or "")
            if owner_kind == "root_schema":
                root_owned += 1
            elif owner_kind == "downstream_extension":
                downstream_owned += 1
            elif field.get("runtime_ref"):
                legacy_alias_only += 1
            if enforce and field.get("structured_required") and owner_kind not in {"root_schema", "downstream_extension"}:
                unresolved_required.append({
                    "trace_id": checkpoint.get("trace_id", ""),
                    "checkpoint_id": checkpoint.get("checkpoint_id", ""),
                    "field": field.get("field", ""),
                    "runtime_ref": field.get("runtime_ref", ""),
                })
    summary = runtime_state_schema.setdefault("summary", {})
    summary["root_owned_field_count"] = root_owned
    summary["downstream_extension_field_count"] = downstream_owned
    summary["legacy_alias_only_field_count"] = legacy_alias_only
    summary["unresolved_required_field_count"] = len(unresolved_required)
    validation = runtime_state_schema.setdefault("schema_validation", {})
    validation["unresolved_required_fields"] = unresolved_required
    if enforce and unresolved_required:
        validation["status"] = "failed"


def _semantic_checkpoint_plan_from_draft(
    draft_path: str | Path,
    tasks: list[dict[str, Any]],
    replay_plan: dict[str, Any],
    runtime_manifest: dict[str, Any],
    terminal_verified: dict[str, Any],
) -> dict[str, Any]:

    path = Path(draft_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"semantic checkpoint planner draft not found: {path}")
    draft = _load_json(path)
    draft_traces = draft.get("traces", []) if isinstance(draft, dict) else []
    if not isinstance(draft_traces, list):
        raise RuntimeError(f"semantic checkpoint planner draft has invalid traces: {path}")

    replay_by_trace = {trace.get("trace_id"): trace for trace in replay_plan.get("traces", []) if isinstance(trace, dict)}
    task_ids = {task["id"] for task in tasks}
    traces: list[dict[str, Any]] = []
    draft_by_trace = {
        str(item.get("trace_id")): item
        for item in draft_traces
        if isinstance(item, dict) and item.get("trace_id")
    }
    missing = sorted(task_ids - set(draft_by_trace))
    if missing:
        raise RuntimeError(
            "semantic checkpoint planner draft missing trace plans: "
            + json.dumps({"draft_path": str(path), "missing": missing[:20]}, ensure_ascii=False)
        )

    for task in tasks:
        task_id = task["id"]
        replay_trace = replay_by_trace.get(task_id, {})
        draft_row = draft_by_trace[task_id]
        traces.append({
            "trace_id": task_id,
            "planner_status": "llm_draft_loaded",
            "task_instruction": task.get("name") or task.get("description", ""),
            "terminal_policy": terminal_verified["trace_policies"].get(task_id, {}),
            "semantic_subtasks": draft_row.get("semantic_subtasks")
            or draft_row.get("checkpoints")
            or [],
            "planner_rationale": draft_row.get("rationale") or draft_row.get("planner_rationale") or "",
            "context_summary": {
                "replay_action_count": len(replay_trace.get("steps", [])),
                "transaction_count": sum(1 for step in replay_trace.get("steps", []) if step.get("transaction")),
                "state_ref_count": len(_dedupe([ref for step in replay_trace.get("steps", []) for ref in _state_refs(step)])),
                "runtime_bridge_status": _runtime_bridge_status(runtime_manifest),
            },
        })

    return {
        "format": "ghostweb_semantic_checkpoint_plan",
        "schema_revision": 1,
        "source": "contract_instrumentation.semantic_planner_draft_ingestion",
        "planner_mode": "llm_draft_artifact",
        "planner_draft_path": str(path),
        "llm_required_for_final_semantic_quality": True,
        "traces": traces,
        "summary": {
            "trace_count": len(traces),
            "planner_executed_count": len(traces),
            "planner_not_run_count": 0,
        },
    }


def load_terminal_policy_candidates_from_rewritten_root(terminal_contract_root: str | Path) -> dict[str, Any]:
    root = Path(terminal_contract_root).expanduser().resolve()
    index_path = root / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"terminal_contract_root missing index.json: {index_path}")
    index = _load_json(index_path)
    records = index.get("traces")
    if not isinstance(records, list) or not records:
        raise RuntimeError(f"terminal_contract_root index has no traces: {index_path}")

    trace_policies: dict[str, Any] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        task_id = str(record.get("id") or record.get("task_id") or record.get("path") or "").strip()
        rel_path = str(record.get("path") or task_id).strip()
        if not task_id or not rel_path:
            invalid.append(f"invalid_record:{record}")
            continue
        trajectory_path = root / rel_path / "trajectory.json"
        if not trajectory_path.is_file():
            missing.append(f"{task_id}:trajectory.json")
            continue
        payload = _load_json(trajectory_path)
        contract = payload.get("terminal_contract")
        if not isinstance(contract, dict):
            missing.append(f"{task_id}:terminal_contract")
            continue
        policy = contract.get("terminal_policy_candidate")
        base_policies = {"precommit_ready", "simulated_commit", "observed_terminal_state"}
        projected_policy = ""
        if policy not in base_policies:
            projected_policy = str(policy or "").strip()
            base_policy = str(contract.get("llm_terminal_policy_raw") or "").strip()
            projected_allowed_state = str(contract.get("allowed_final_state") or "").strip()
            projected_outcome_fields = contract.get("outcome_fields")
            projected_boundary = str(contract.get("terminal_boundary_description") or "").strip()
            is_agentic_projection = (
                contract.get("source") == "reference_agentic_rewrite"
                and bool(projected_policy)
                and base_policy in base_policies
                and projected_allowed_state == projected_policy
                and isinstance(projected_outcome_fields, list)
                and bool(projected_outcome_fields)
                and bool(projected_boundary)
            )
            if not is_agentic_projection:
                invalid.append(f"{task_id}:terminal_policy_candidate={policy!r}")
                continue
            policy = base_policy
        if policy not in base_policies:
            invalid.append(f"{task_id}:terminal_policy_candidate={policy!r}")
            continue
        normalized = dict(contract)
        if projected_policy:
            normalized["projected_terminal_policy_candidate"] = projected_policy
            normalized["terminal_policy_candidate"] = policy
        normalized.setdefault("schema_version", 1)
        normalized["source"] = str(contract.get("source") or "trace_instruction_rewriter")
        normalized["real_preprocess_rerun"] = True
        normalized["trace_id"] = task_id
        normalized["rewritten_trace_path"] = str(trajectory_path)
        trace_policies[task_id] = normalized

    if missing or invalid:
        raise RuntimeError(
            "invalid terminal contracts in rewritten trace root: "
            + json.dumps({"root": str(root), "missing": missing[:20], "invalid": invalid[:20]}, ensure_ascii=False)
        )

    return {
        "format": "ghostweb_terminal_policy_candidates",
        "schema_revision": 1,
        "source": "rewritten_trace_terminal_contract",
        "real_preprocess_rerun": True,
        "terminal_contract_root": str(root),
        "trace_policies": trace_policies,
        "summary": _policy_summary(trace_policies, "terminal_policy_candidate") | {
            "trace_count": len(trace_policies),
        },
    }


def verify_terminal_policies(
    candidates: dict[str, Any],
    replay_plan: dict[str, Any],
    action_effect_contracts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replay_by_trace = {trace["trace_id"]: trace for trace in replay_plan.get("traces", [])}
    action_effects_by_id = _terminal_action_effects_by_id(action_effect_contracts)
    excluded_action_effects_by_id = _terminal_excluded_action_effects_by_id(action_effect_contracts)
    verified: dict[str, Any] = {}
    for task_id, candidate in candidates["trace_policies"].items():
        replay_trace = replay_by_trace.get(task_id, {})
        policy = candidate["terminal_policy_candidate"]
        contract_actions = []
        evidence_step_idx = candidate.get("last_task_relevant_step")
        evidence_action_id = f"{task_id}:step{evidence_step_idx}" if evidence_step_idx is not None else ""
        issues: list[dict[str, Any]] = []
        if evidence_action_id in excluded_action_effects_by_id:
            override = _nearest_prior_terminal_authority(
                task_id,
                evidence_step_idx,
                action_effects_by_id,
                replay_trace,
            )
            if override:
                excluded = excluded_action_effects_by_id[evidence_action_id]
                issues.append({
                    "code": "candidate_terminal_boundary_excluded",
                    "severity": "warning",
                    "candidate_action_id": evidence_action_id,
                    "exclusion_reason": excluded.get("exclusion_reason") or excluded.get("skip_reason"),
                    "replacement_action_id": override.get("action_id"),
                    "terminal_policy_override": "simulated_commit",
                })
                policy = "simulated_commit"
                evidence_action_id = str(override.get("action_id") or evidence_action_id)
                evidence_step_idx = override.get("step_index", evidence_step_idx)
                candidate = _terminal_candidate_with_mock_commit(candidate)
            else:
                issues.append({
                    "code": "candidate_terminal_boundary_excluded_without_replacement",
                    "severity": "hard_failure",
                    "candidate_action_id": evidence_action_id,
                })
        terminal_source = ""
        terminal_target = ""
        terminal_reducer = ""
        for step in replay_trace.get("steps", []):
            action_id = step.get("action_id", "")
            if action_id == evidence_action_id or action_id.endswith(f"step{evidence_step_idx}"):
                summary = _action_contract_summary(step)
                contract_actions.append(summary)
                evidence_action_id = action_id
                terminal_source = summary.get("source_filename") or ""
                terminal_target = summary.get("target_filename") or ""
                terminal_reducer = summary.get("reducer") or ""
        if not contract_actions and evidence_action_id in action_effects_by_id:
            summary = _action_effect_contract_summary(action_effects_by_id[evidence_action_id])
            contract_actions.append(summary)
            terminal_source = summary.get("source_filename") or ""
            terminal_target = summary.get("target_filename") or ""
            terminal_reducer = summary.get("reducer") or ""
        observable_scope = _terminal_observable_scope(policy, terminal_target)
        required_mock_state = (
            _required_mock_state_for_terminal_candidate(policy, candidate)
            if policy == "simulated_commit"
            else list(candidate.get("required_mock_state", []))
        )
        forbidden_terminal_events = _dedupe(
            list(candidate.get("forbidden_terminal_events", [])) + _forbidden_events(policy)
        )
        verified_row = {
            "candidate_policy": candidate["terminal_policy_candidate"],
            "projected_terminal_policy_candidate": candidate.get("projected_terminal_policy_candidate"),
            "verified_policy": policy,
            "status": "verified",
            "confidence": candidate.get("confidence", "medium"),
            "evidence_steps": [f"{task_id}:step{evidence_step_idx}"],
            "contract_actions": contract_actions,
            "terminal_boundary": {
                "policy": policy,
                "source_action_id": evidence_action_id,
                "source_filename": terminal_source,
                "target_filename": terminal_target,
                "terminal_reducer": terminal_reducer,
                "observable_scope": observable_scope,
                "process_prerequisite_scope": "depends_on_process_checkpoints",
                "commit_payload_scope": required_mock_state,
                "mock_side_effect_scope": required_mock_state,
                "forbidden_side_effect_scope": forbidden_terminal_events,
            },
            "allowed_final_state": candidate.get("allowed_final_state") or _allowed_final_state(policy),
            "forbidden_terminal_events": forbidden_terminal_events,
            "required_mock_state": required_mock_state,
            "issues": issues,
        }
        outcome_fields = _normalize_terminal_outcome_fields(candidate.get("outcome_fields"))
        if outcome_fields:
            terminal_values = {
                field["field"]: field["expected"]
                for field in outcome_fields
                if field.get("field") and "expected" in field
            }
            verified_row["outcome_fields"] = outcome_fields
            verified_row["outcome_expected_state_diff"] = terminal_values
            verified_row["terminal_outcome_values"] = terminal_values
        if candidate.get("rewritten_trace_path"):
            verified_row["reference_terminal_contract_path"] = candidate.get("rewritten_trace_path")
        refresh = candidate.get("reference_oracle_admission_refresh")
        if isinstance(refresh, dict) and refresh:
            verified_row["reference_oracle_admission_refresh"] = refresh
        verified[task_id] = verified_row
    return {
        "format": "ghostweb_terminal_policy_verified",
        "schema_revision": 1,
        "source": "terminal_policy_verifier.contract_instrumentation",
        "trace_policies": verified,
        "summary": _policy_summary(verified, "verified_policy") | {
            "trace_count": len(verified),
            "verified_count": sum(1 for row in verified.values() if row["status"] == "verified"),
            "unresolved_count": sum(1 for row in verified.values() if row["status"] == "unresolved"),
        },
    }


def _normalize_terminal_outcome_fields(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        fields: list[dict[str, Any]] = []
        for field, expected in value.items():
            if isinstance(expected, dict):
                expected = expected.get("expected", expected.get("value", expected))
            fields.append({"field": str(field), "expected": expected})
        return [field for field in fields if field.get("field")]
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            field = item.get("field") or item.get("field_name") or item.get("name") or item.get("key")
            if not field:
                continue
            row = dict(item)
            row["field"] = str(field)
            if "expected" not in row and "value" in row:
                row["expected"] = row["value"]
            normalized.append(row)
        elif item:
            text = str(item)
            normalized.append({"field": text, "expected": text})
    return normalized


def _terminal_action_effects_by_id(action_effect_contracts: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(action_effect_contracts, dict):
        return {}
    by_id = action_effect_contracts.get("actions_by_id")
    if isinstance(by_id, dict):
        return {str(action_id): row for action_id, row in by_id.items() if isinstance(row, dict)}
    rows = action_effect_contracts.get("actions", [])
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("action_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("action_id")
    }


def _terminal_excluded_action_effects_by_id(action_effect_contracts: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(action_effect_contracts, dict):
        return {}
    rows = action_effect_contracts.get("excluded_actions", [])
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("action_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("action_id")
    }


def _nearest_prior_terminal_authority(
    trace_id: str,
    evidence_step_idx: Any,
    action_effects_by_id: dict[str, dict[str, Any]],
    replay_trace: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        step_idx = int(evidence_step_idx)
    except (TypeError, ValueError):
        step_idx = 10**9
    candidates = [
        row
        for row in action_effects_by_id.values()
        if row.get("trace_id") == trace_id
        and _coerce_int(row.get("step_index"), -1) < step_idx
        and row.get("reward_relevant") is not False
    ]
    if not candidates:
        return _nearest_prior_replay_step(trace_id, step_idx, replay_trace)
    candidates.sort(key=lambda row: _terminal_authority_rank(row), reverse=True)
    return candidates[0]


def _terminal_authority_rank(row: dict[str, Any]) -> tuple[int, int, int]:
    effect_kind = str(row.get("effect_kind") or "")
    strong_effect = {
        "stateful_submit",
        "save_return",
        "select_result_return",
        "same_page_mutation",
        "gesture_route",
        "route",
    }
    weak_effect = {"query_projection", "activation"}
    return (
        1 if row.get("state_changing") else 0,
        1 if effect_kind in strong_effect else -1 if effect_kind in weak_effect else 0,
        _coerce_int(row.get("step_index"), -1),
    )


def _nearest_prior_replay_step(trace_id: str, step_idx: int, replay_trace: dict[str, Any]) -> dict[str, Any] | None:
    candidates = []
    for step in replay_trace.get("steps", []):
        action_id = str(step.get("action_id") or "")
        if not action_id.startswith(f"{trace_id}:step"):
            continue
        current_idx = _coerce_int(action_id.rsplit("step", 1)[-1], -1)
        if current_idx < step_idx:
            row = dict(step)
            row["trace_id"] = trace_id
            row["step_index"] = current_idx
            candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda row: _coerce_int(row.get("step_index"), -1))


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _terminal_candidate_with_mock_commit(candidate: dict[str, Any]) -> dict[str, Any]:
    updated = dict(candidate)
    updated["terminal_policy_candidate"] = candidate.get("terminal_policy_candidate", "observed_terminal_state")
    updated["allowed_final_state"] = _allowed_final_state("simulated_commit")
    updated["required_mock_state"] = _required_mock_state_for_terminal_candidate("simulated_commit", candidate)
    updated["forbidden_terminal_events"] = _dedupe(
        list(candidate.get("forbidden_terminal_events", [])) + _forbidden_events("simulated_commit")
    )
    return updated


def _required_mock_state_for_terminal_candidate(policy: str, candidate: dict[str, Any]) -> list[str]:
    if policy != "simulated_commit":
        return list(candidate.get("required_mock_state", []))
    existing = [str(item) for item in candidate.get("required_mock_state", []) if item]
    text = _terminal_candidate_semantic_text(candidate)
    inferred = ["mock_external_side_effect_attempted"]
    if any(token in text for token in ("提醒", "reminder", "日程", "calendar")):
        inferred.append("mock_external_side_effect_pending")
    elif any(token in text for token in ("支付", "付款", "payment", "订单", "order")):
        inferred.extend(["commit_attempted", "pending_payment_or_confirmation"])
    else:
        inferred.extend(_required_mock_state("simulated_commit"))
    return _dedupe(existing + inferred)


def _terminal_candidate_semantic_text(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("task_instruction", "terminal_boundary_description", "irreversible_action"):
        value = candidate.get(key)
        if value:
            parts.append(str(value))
    fields = candidate.get("outcome_fields")
    if isinstance(fields, dict):
        for key, value in fields.items():
            parts.append(str(key))
            parts.append(str(value))
    elif isinstance(fields, list):
        parts.extend(str(item) for item in fields)
    return "\n".join(parts)


def compile_checkpoint_matrix(
    tasks: list[dict[str, Any]],
    capture_actions: dict[str, Any],
    replay_plan: dict[str, Any],
    runtime_manifest: dict[str, Any],
    terminal_verified: dict[str, Any],
    semantic_obligation_matrix: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    replay_by_trace = {trace["trace_id"]: trace for trace in replay_plan.get("traces", [])}
    sto_by_trace: dict[str, list[dict[str, Any]]] = {}
    semantic_obligation_schema_shape = "missing"
    if semantic_obligation_matrix:
        semantic_obligation_schema_shape = _semantic_obligation_schema_shape(semantic_obligation_matrix)
        for tid, obligations in _iter_semantic_obligation_traces(semantic_obligation_matrix):
            if tid:
                sto_by_trace[tid] = obligations
    checkpoints: list[dict[str, Any]] = []
    trace_reports: dict[str, Any] = {}
    for task in tasks:
        task_id = task["id"]
        policy = terminal_verified["trace_policies"][task_id]
        sto_obligations = sto_by_trace.get(task_id, [])
        if sto_obligations:
            rows, report = _compile_from_semantic_obligations(
                task, sto_obligations, capture_actions.get(task_id, []),
                replay_by_trace.get(task_id, {}), terminal_verified,
            )
            if not any(row.get("blocking") and row.get("supervision") == "process" for row in rows):
                report.setdefault("quality_gate_failures", []).append({
                    "trace_id": task_id,
                    "code": "semantic_obligation_compilation_empty",
                    "message": "semantic obligations were present but no blocking process checkpoint was compiled",
                })
        else:
            rows, report = _compile_generic_trace_matrix(task, capture_actions.get(task_id, []), replay_by_trace.get(task_id, {}), terminal_verified)
        rows = _dedupe_row_negative_probes(rows)
        _apply_terminal_policy_to_outcome_rows(rows, policy)
        quality = _validate_checkpoint_rows(rows, policy, runtime_manifest)
        for row in rows:
            row["quality_gates"] = quality["row_gates"].get(row["checkpoint_id"], _quality_gates(row))
        report["quality_gate_failures"] = quality["failures"]
        report["contract_gaps"] = _dedupe(report.get("contract_gaps", []) + quality["contract_gaps"])
        report["status"] = "failed" if quality["failures"] else "passed"
        checkpoints.extend(rows)
        trace_reports[task_id] = report

    matrix = {
        "format": "ghostweb_checkpoint_matrix",
        "schema_revision": 1,
        "source": "contract_instrumentation.checkpoint_matrix_compiler",
        "stable_key": "trace_id|checkpoint_id|checkpoint_kind|expected_state_id",
        "compiler_inputs": REQUIRED_SOURCE_ARTIFACTS,
        "runtime_bridge_status": _runtime_bridge_status(runtime_manifest),
        "checkpoints": checkpoints,
        "summary": {
            "trace_count": len(tasks),
            "checkpoint_count": len(checkpoints),
            "outcome_checkpoint_count": sum(1 for row in checkpoints if row["supervision"] == "outcome"),
            "process_checkpoint_count": sum(1 for row in checkpoints if row["supervision"] == "process"),
            "blocking_checkpoint_count": sum(1 for row in checkpoints if row.get("blocking")),
        },
    }
    report = {
        "format": "ghostweb_checkpoint_matrix_validation_report",
        "schema_revision": 1,
        "source": "contract_instrumentation.checkpoint_matrix_compiler",
        "trace_count": len(tasks),
        "row_count": len(checkpoints),
        "runtime_bridge_status": _runtime_bridge_status(runtime_manifest),
        "semantic_obligation_schema_shape": semantic_obligation_schema_shape,
        "semantic_obligation_trace_count": len(sto_by_trace),
        "status": "failed" if any(
            report_row.get("quality_gate_failures") for report_row in trace_reports.values()
        ) else "passed_static_contract",
        "trace_reports": trace_reports,
        "quality_gate_failures": [
            failure
            for report_row in trace_reports.values()
            for failure in report_row.get("quality_gate_failures", [])
        ],
        "notes": [
            "Runtime JS does not expose step/inspect_state/state_diff; event replay remains a runtime bridge follow-up.",
            "Static checkpoint/evaluator/test_logic artifacts are generated from formal contracts and capture evidence.",
        ],
    }
    return matrix, report


def compile_checkpoint_matrix_from_bundle(
    instrumentation_bundle: dict[str, Any],
    runtime_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    trace_reports: dict[str, Any] = {}
    for trace in instrumentation_bundle.get("trace_contracts", []):
        if not isinstance(trace, dict):
            continue
        trace_id = str(trace.get("trace_id") or "")
        rows = [_bundle_checkpoint_to_matrix_row(trace_id, cp) for cp in trace.get("checkpoints", []) if isinstance(cp, dict)]
        rows = _dedupe_row_negative_probes(rows)
        rows, absorbed_duplicates = _absorb_adjacent_duplicate_process_rows(rows)
        policy = {
            "verified_policy": trace.get("terminal_policy", "observed_terminal_state"),
            "forbidden_terminal_events": (trace.get("outcome_contract") or {}).get("forbidden_real_side_effects", []),
            "required_mock_state": (trace.get("outcome_contract") or {}).get("required_mock_side_effects", []),
        }
        quality = _validate_checkpoint_rows(rows, policy, _runtime_manifest_with_bundle_state_refs(runtime_manifest, rows))
        for row in rows:
            row["quality_gates"] = quality["row_gates"].get(row["checkpoint_id"], _quality_gates(row))
        checkpoints.extend(rows)
        trace_reports[trace_id] = {
            "trace_id": trace_id,
            "source": "instrumentation_contract_bundle",
            "checkpoint_count": len(rows),
            "absorbed_duplicate_checkpoints": absorbed_duplicates,
            "quality_gate_failures": quality["failures"],
            "contract_gaps": quality["contract_gaps"],
            "status": "failed" if quality["failures"] else "passed",
        }

    matrix = {
        "format": "ghostweb_checkpoint_matrix",
        "schema_revision": 2,
        "source": "instrumentation_contract_bundle.materialized_checkpoint_matrix",
        "authority_bundle_ref": "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/instrumentation_contract_bundle.json",
        "stable_key": "trace_id|checkpoint_id|checkpoint_kind|expected_state_id",
        "compiler_inputs": [
            "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/instrumentation_contract_bundle.json",
            "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/checkpoint_contract_matrix_source.json",
        ],
        "runtime_bridge_status": _runtime_bridge_status(runtime_manifest),
        "checkpoints": checkpoints,
        "summary": {
            "trace_count": len(trace_reports),
            "checkpoint_count": len(checkpoints),
            "outcome_checkpoint_count": sum(1 for row in checkpoints if row["supervision"] == "outcome"),
            "process_checkpoint_count": sum(1 for row in checkpoints if row["supervision"] == "process"),
            "blocking_checkpoint_count": sum(1 for row in checkpoints if row.get("blocking")),
        },
    }
    report = {
        "format": "ghostweb_checkpoint_matrix_validation_report",
        "schema_revision": 2,
        "source": "instrumentation_contract_bundle.materialized_checkpoint_matrix",
        "authority_bundle_ref": matrix["authority_bundle_ref"],
        "trace_count": len(trace_reports),
        "row_count": len(checkpoints),
        "runtime_bridge_status": matrix["runtime_bridge_status"],
        "status": "failed" if any(
            report_row.get("quality_gate_failures") for report_row in trace_reports.values()
        ) else "passed_static_contract",
        "trace_reports": trace_reports,
        "quality_gate_failures": [
            failure
            for report_row in trace_reports.values()
            for failure in report_row.get("quality_gate_failures", [])
        ],
        "contract_gaps": [
            gap
            for report_row in trace_reports.values()
            for gap in report_row.get("contract_gaps", [])
        ],
        "notes": [
            "Checkpoint matrix materialized from instrumentation_contract_bundle only.",
        ],
    }
    return matrix, report


def _runtime_manifest_with_bundle_state_refs(
    runtime_manifest: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest = dict(runtime_manifest or {})
    schema = dict(manifest.get("state_store_schema") or {})
    declared = set()
    existing = schema.get("declared_state_refs", [])
    if isinstance(existing, list):
        declared.update(str(ref) for ref in existing if ref)
    elif isinstance(existing, dict):
        declared.update(str(ref) for ref in existing.keys())
    for row in rows:
        declared.update(
            str(ref)
            for ref in (row.get("global_runtime_state") or {}).get("state_refs", [])
            if ref
        )
    schema["declared_state_refs"] = sorted(declared)
    manifest["state_store_schema"] = schema
    return manifest


def _bundle_checkpoint_to_matrix_row(trace_id: str, cp: dict[str, Any]) -> dict[str, Any]:
    cp, compiler_repairs = _normalize_bundle_page_runtime_placeholders(cp)
    supervision = str(cp.get("supervision_type") or cp.get("supervision") or "process")
    expected_state = dict(cp.get("expected_state") or {})
    expected_diff = dict(cp.get("expected_state_diff") or {})
    if supervision == "outcome" and not expected_diff:
        expected_diff = _expected_diff_from_outcome_fields(expected_state.get("outcome_fields"))
    if supervision == "process" and not expected_state:
        expected_state = dict(expected_diff)
    state_refs = [str(ref) for ref in cp.get("state_refs", []) if ref]
    if supervision == "outcome" and not state_refs:
        state_refs = _state_refs_from_outcome_fields(expected_state.get("outcome_fields"))
    legacy_state_refs = list(state_refs)
    canonical_state_refs = _explicit_canonical_state_refs(state_refs)
    action_ids = [str(action_id) for action_id in cp.get("action_ids", []) if action_id]
    projection_checks = list(cp.get("visible_projection_assertions") or cp.get("projection_assertions") or [])
    payload_fields = _payload_fields_from_bundle_assertions(cp, expected_state, expected_diff)
    row = {
        "trace_id": trace_id or str(cp.get("trace_id") or ""),
        "checkpoint_id": str(cp.get("checkpoint_id") or ""),
        "checkpoint_kind": str(cp.get("checkpoint_kind") or "process"),
        "supervision": supervision,
        "candidate_types": [str(cp.get("checkpoint_kind") or "process")],
        "source_steps": list(cp.get("source_steps") or []),
        "expected_state_id": _bundle_expected_state_id(cp),
        "expected_state": expected_state,
        "global_runtime_state": {
            "state_refs": state_refs,
            "legacy_state_refs": legacy_state_refs,
            "canonical_state_refs": canonical_state_refs,
            "downstream_extension_refs": [],
            "canonical_expected_state_diff": {},
            "downstream_expected_state_diff": {},
            "expected_state_diff": expected_diff,
            "source_action_ids": action_ids,
            "write_read_closed": bool(state_refs or expected_diff or action_ids),
            "transaction_id": str(cp.get("transaction_id") or ""),
            "dom_binding_id": str(cp.get("dom_binding_id") or ""),
        },
        "projection_checks": projection_checks,
        "projection_assertions": projection_checks,
        "event": _bundle_event(cp),
        "payload_fields": payload_fields,
        "negative_probes": [_normalize_bundle_probe(probe, str(cp.get("checkpoint_id") or "")) for probe in cp.get("negative_probe_specs", [])],
        "reward_weight": 0.3 if supervision == "outcome" else 0.1,
        "blocking": bool(cp.get("blocking", True)),
        "authority": "instrumentation_contract_bundle",
        "authority_bundle_ref": "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/instrumentation_contract_bundle.json",
        "transaction_id": str(cp.get("transaction_id") or ""),
        "dom_binding_id": str(cp.get("dom_binding_id") or ""),
        "runtime_event_assertions": list(cp.get("runtime_event_assertions") or []),
    }
    canonical_assertions = cp.get("canonical_state_assertions")
    if isinstance(canonical_assertions, dict) and canonical_assertions:
        row["global_runtime_state"]["canonical_expected_state_diff"] = {
            str(path): dict(assertion).get("value")
            for path, assertion in canonical_assertions.items()
            if isinstance(assertion, dict)
        }
        row["global_runtime_state"]["canonical_state_refs"] = sorted(
            row["global_runtime_state"]["canonical_expected_state_diff"]
        )
    if cp.get("route_only_justification"):
        row["route_only_justification"] = str(cp.get("route_only_justification"))
    if compiler_repairs:
        row["compiler_repairs"] = compiler_repairs
    if supervision == "outcome":
        row["terminal_policy"] = str(cp.get("terminal_policy") or "")
        row["forbidden_events"] = list(cp.get("forbidden_terminal_events") or [])
        row["required_mock_state"] = list(cp.get("required_mock_side_effects") or [])
        row["terminal_boundary"] = dict(cp.get("terminal_boundary") or {})
    return row


def _normalize_bundle_page_runtime_placeholders(
    checkpoint: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:

    if not isinstance(checkpoint, dict):
        return checkpoint, []
    terminal_boundary = checkpoint.get("terminal_boundary")
    if not isinstance(terminal_boundary, dict):
        return checkpoint, []
    target = str(
        terminal_boundary.get("target_filename")
        or terminal_boundary.get("source_filename")
        or ""
    ).strip()
    if not target.lower().endswith(".html"):
        return checkpoint, []

    normalized = copy.deepcopy(checkpoint)
    changed_paths: list[str] = []

    def replace(mapping: Any, key: str, path: str, placeholder: str | None = None) -> None:
        expected_placeholder = placeholder or key
        if (
            isinstance(mapping, dict)
            and str(mapping.get(key) or "").strip() == expected_placeholder
        ):
            mapping[key] = target
            changed_paths.append(path)

    expected_state = normalized.get("expected_state")
    if isinstance(expected_state, dict):
        current_page = expected_state.get("current_page")
        if isinstance(current_page, dict):
            replace(
                current_page,
                "value",
                "expected_state.current_page.value",
                "current_page",
            )
        replace(expected_state, "current_page", "expected_state.current_page")
        for index, field in enumerate(expected_state.get("outcome_fields") or []):
            if isinstance(field, dict) and str(field.get("field") or "").strip() == "current_page":
                replace(
                    field,
                    "expected",
                    f"expected_state.outcome_fields[{index}].expected",
                    "current_page",
                )

    replace(normalized.get("expected_state_diff"), "current_page", "expected_state_diff.current_page")
    for index, assertion in enumerate(normalized.get("runtime_event_assertions") or []):
        if isinstance(assertion, dict):
            replace(
                assertion.get("payload_required"),
                "current_page",
                f"runtime_event_assertions[{index}].payload_required.current_page",
            )
    for key in ("visible_projection_assertions", "projection_assertions"):
        for index, assertion in enumerate(normalized.get(key) or []):
            if (
                isinstance(assertion, dict)
                and str(assertion.get("field") or "").strip() == "current_page"
                and str(assertion.get("value") or "").strip() == "current_page"
            ):
                assertion["value"] = target
                changed_paths.append(f"{key}[{index}].value")

    repairs = []
    if changed_paths:
        repairs.append({
            "kind": "page_runtime_self_placeholder_resolved",
            "field": "current_page",
            "authority": "terminal_boundary.target_filename",
            "value": target,
            "changed_paths": changed_paths,
        })
    return normalized, repairs


def _expected_diff_from_outcome_fields(outcome_fields: Any) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    if not isinstance(outcome_fields, list):
        return diff
    for item in outcome_fields:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("name") or item.get("key") or "").strip()
        if not field:
            continue
        value = item.get("expected", item.get("value", item.get("text", "")))
        if value not in (None, ""):
            diff[field] = value
    return diff


def _state_refs_from_outcome_fields(outcome_fields: Any) -> list[str]:
    refs: list[str] = []
    if not isinstance(outcome_fields, list):
        return refs
    for item in outcome_fields:
        if not isinstance(item, dict):
            continue
        explicit = str(item.get("state_ref") or "").strip()
        if explicit:
            refs.append(explicit)
            continue
        field = str(item.get("field") or item.get("name") or item.get("key") or "").strip()
        if field:
            refs.append(f"taskOutcome.{_state_ref_slug(field)}")
    return _dedupe(refs)


def _state_ref_slug(value: Any) -> str:
    text = re.sub(r"[^\w]+", "_", str(value or "").strip(), flags=re.UNICODE)
    return text.strip("_") or "field"


def _bundle_expected_state_id(cp: dict[str, Any]) -> str:
    checkpoint_id = str(cp.get("checkpoint_id") or "")
    transaction_id = str(cp.get("transaction_id") or "")
    if transaction_id:
        return f"transaction={transaction_id}|checkpoint={checkpoint_id}"
    policy = str(cp.get("terminal_policy") or "")
    if policy:
        return f"terminal_policy={policy}|checkpoint={checkpoint_id}"
    return checkpoint_id


def _bundle_event(cp: dict[str, Any]) -> str:
    assertions = cp.get("runtime_event_assertions") or []
    if assertions and isinstance(assertions[0], dict) and assertions[0].get("event"):
        return str(assertions[0]["event"])
    return f"ghostweb:checkpoint:{cp.get('checkpoint_id', '')}"


def _payload_fields_from_bundle_assertions(
    cp: dict[str, Any],
    expected_state: dict[str, Any],
    expected_diff: dict[str, Any],
) -> list[str]:
    fields = ["trace_id", "checkpoint_id", "source_steps"]
    for assertion in cp.get("runtime_event_assertions", []) or []:
        payload = assertion.get("payload_required") if isinstance(assertion, dict) else {}
        if isinstance(payload, dict):
            fields.extend(str(key) for key in payload.keys())
    fields.extend(str(key) for key in expected_state.keys())
    fields.extend(str(key) for key in expected_diff.keys())
    return _dedupe(fields)


def _normalize_bundle_probe(probe: Any, checkpoint_id: str) -> dict[str, Any]:
    if isinstance(probe, dict):
        row = dict(probe)
        row.setdefault("probe_id", checkpoint_id)
        row.setdefault("must_fail", [checkpoint_id])
        return row
    probe_id = str(probe or "").strip() or f"probe_{checkpoint_id}"
    row: dict[str, Any] = {"probe_id": probe_id, "must_fail": [checkpoint_id]}
    if probe_id in {"forbidden_real_side_effect", "real_side_effect_success", "real_success_spoof"}:
        row["forbidden_events"] = ["real_payment_success", "real_external_side_effect"]
    elif "wrong" in probe_id:
        row["mutate"] = {"__probe__": probe_id}
    elif "missing" in probe_id:
        row["mutate"] = {"__remove__": probe_id}
    else:
        row["mutate"] = {"__probe__": probe_id}
    return row


def compile_runtime_state_schema(matrix: dict[str, Any]) -> dict[str, Any]:
    checkpoint_schemas: list[dict[str, Any]] = []
    blocking_without_canonical_fields: list[dict[str, str]] = []
    text_only_blocking_checkpoints: list[dict[str, str]] = []
    field_type_counts: dict[str, int] = {}

    for cp in matrix.get("checkpoints", []):
        if not isinstance(cp, dict):
            continue
        expected_state = cp.get("expected_state") or {}
        runtime_state = cp.get("global_runtime_state") or {}
        expected_diff = runtime_state.get("expected_state_diff") or {}
        canonical_expected_diff = runtime_state.get("canonical_expected_state_diff") or {}
        downstream_expected_diff = runtime_state.get("downstream_expected_state_diff") or {}
        runtime_event_payload_diff = _runtime_event_payload_state_fields(cp)
        projection_assertions = cp.get("projection_assertions") or cp.get("projection_checks") or []
        state_refs = list(runtime_state.get("state_refs") or [])
        fields = _runtime_schema_fields(
            expected_state,
            expected_diff,
            state_refs,
            canonical_expected_diff=canonical_expected_diff,
            downstream_expected_diff=downstream_expected_diff,
            runtime_event_payload_diff=runtime_event_payload_diff,
        )
        for field in fields:
            field_type_counts[field["type"]] = field_type_counts.get(field["type"], 0) + 1
        structured_required = _checkpoint_requires_structured_state(cp, fields)
        if cp.get("blocking") and structured_required and not fields:
            blocking_without_canonical_fields.append({
                "trace_id": str(cp.get("trace_id", "")),
                "checkpoint_id": str(cp.get("checkpoint_id", "")),
            })
        if cp.get("blocking") and not fields and projection_assertions:
            text_only_blocking_checkpoints.append({
                "trace_id": str(cp.get("trace_id", "")),
                "checkpoint_id": str(cp.get("checkpoint_id", "")),
            })
        checkpoint_schemas.append({
            "trace_id": cp.get("trace_id", ""),
            "checkpoint_id": cp.get("checkpoint_id", ""),
            "checkpoint_kind": cp.get("checkpoint_kind", ""),
            "supervision": cp.get("supervision", ""),
            "blocking": bool(cp.get("blocking")),
            "source_steps": cp.get("source_steps", []),
            "owner_pages": _runtime_schema_owner_pages(cp),
            "state_refs": state_refs,
            "legacy_state_refs": list((cp.get("global_runtime_state") or {}).get("legacy_state_refs") or state_refs),
            "canonical_state_refs": list((cp.get("global_runtime_state") or {}).get("canonical_state_refs") or []),
            "downstream_extension_refs": list((cp.get("global_runtime_state") or {}).get("downstream_extension_refs") or []),
            "structured_required": structured_required,
            "fields": fields,
            "projection_assertion_count": len(projection_assertions) if isinstance(projection_assertions, list) else 0,
        })

    schema = {
        "format": "ghostweb_runtime_state_schema",
        "schema_revision": 1,
        "source": "contract_instrumentation.runtime_state_schema_compiler",
        "checkpoint_matrix_ref": "phase_post_processing/instrumentation/checkpoint_matrix.json",
        "global_execution_schema_ref": "phase_consolidation/schema_registry/global_execution_schema.json",
        "runtime_bridge_status": matrix.get("runtime_bridge_status", "unknown"),
        "summary": {
            "checkpoint_count": len(checkpoint_schemas),
            "structured_required_count": sum(1 for item in checkpoint_schemas if item["structured_required"]),
            "field_count": sum(len(item["fields"]) for item in checkpoint_schemas),
            "field_type_counts": dict(sorted(field_type_counts.items())),
            "blocking_without_canonical_fields": len(blocking_without_canonical_fields),
            "text_only_blocking_checkpoints": len(text_only_blocking_checkpoints),
        },
        "schema_validation": {
            "status": "failed" if blocking_without_canonical_fields else "passed_static_contract",
            "blocking_without_canonical_fields": blocking_without_canonical_fields,
            "text_only_blocking_checkpoints": text_only_blocking_checkpoints,
        },
        "checkpoints": checkpoint_schemas,
    }
    _refresh_runtime_state_schema_owner_summary(schema)
    return schema


def _runtime_schema_fields(
    expected_state: dict[str, Any],
    expected_diff: dict[str, Any],
    state_refs: list[str] | None = None,
    canonical_expected_diff: dict[str, Any] | None = None,
    downstream_expected_diff: dict[str, Any] | None = None,
    runtime_event_payload_diff: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    refs = [str(ref) for ref in (state_refs or []) if ref]
    for source, data in (
        ("expected_state", expected_state),
        ("expected_state_diff", expected_diff),
        ("canonical_expected_state_diff", canonical_expected_diff or {}),
        ("downstream_expected_state_diff", downstream_expected_diff or {}),
        ("runtime_event_payload_required", runtime_event_payload_diff or {}),
    ):
        if not isinstance(data, dict):
            continue
        for field, value in data.items():
            name = str(field)
            if not name:
                continue
            payload_field = _expected_payload_field_name(name, value)
            owner_name = payload_field if _is_page_runtime_field(payload_field) else name
            if name in {"terminal_policy", "allowed_final_state", "outcome_fields"}:
                continue
            raw = _raw_expected_value(value)
            if _is_semantic_operation_field(name, raw, data):
                continue
            default_owner = _default_runtime_field_owner(owner_name)
            if source == "canonical_expected_state_diff" and not _is_page_runtime_field(payload_field):
                default_owner = {
                    "owner_kind": "root_schema",
                    "canonical_schema_ref": name,
                }
            elif source == "downstream_expected_state_diff":
                default_owner = {
                    "owner_kind": "downstream_extension",
                    "extension_ref": name,
                    "not_root_reason": _not_root_reason_for_runtime_path(name),
                }
            entry_name = owner_name if _is_page_runtime_field(owner_name) else name
            runtime_ref = (
                entry_name
                if _is_page_runtime_field(entry_name)
                or source in {"canonical_expected_state_diff", "downstream_expected_state_diff", "runtime_event_payload_required"}
                else _runtime_ref_for_field(entry_name, refs)
            )
            entry = merged.setdefault(entry_name, {
                "field": entry_name,
                "type": _runtime_state_field_type(entry_name, raw),
                "authority": "runtime_event_payload" if source == "runtime_event_payload_required" else "canonical_state",
                "sources": [],
                "canonical_value": raw,
                "structured_required": _runtime_schema_field_structured_required(entry_name),
                "runtime_ref": runtime_ref,
                "owner_kind": default_owner.get("owner_kind", "unresolved"),
                "canonical_schema_ref": default_owner.get("canonical_schema_ref", ""),
                "extension_ref": default_owner.get("extension_ref", ""),
                "not_root_reason": default_owner.get("not_root_reason", ""),
            })
            entry["sources"].append(source)
            if isinstance(value, dict):
                scope = value.get("field_scope") or value.get("assertion_channel")
                if scope:
                    entry["field_scope"] = scope
    return list(merged.values())


def _runtime_event_payload_state_fields(cp: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for assertion in cp.get("runtime_event_assertions", []) or []:
        if not isinstance(assertion, dict):
            continue
        payload = assertion.get("payload_required")
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            name = str(key or "").strip()
            if not name or not _is_page_runtime_field(name):
                continue
            fields[name] = value
    return fields


def _expected_payload_field_name(default: str, value: Any) -> str:
    if isinstance(value, dict):
        field = str(value.get("field") or "").strip()
        if field:
            return field
    return str(default or "").strip()


def _runtime_schema_field_structured_required(name: str) -> bool:
    if _is_page_runtime_field(name):
        return False
    return True


def _runtime_state_field_type(field: str, value: Any) -> str:
    if _is_page_runtime_field(field):
        return "page"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return "string_set"
        if all(isinstance(item, dict) for item in value):
            return "record_list"
        return "list"
    if isinstance(value, dict):
        if {"min", "max"} <= set(value):
            return "numeric_range"
        return "record"
    return "string"


def _default_runtime_field_owner(field: str) -> dict[str, str]:
    if _is_page_runtime_field(field):
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": "runtime.page",
            "extension_ref": "runtime.page",
            "not_root_reason": "route_runtime_state_not_business_state",
        }
    if field == "state_reads":
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": "checkpointState.state_reads",
            "extension_ref": "checkpointState.state_reads",
            "not_root_reason": "checkpoint_local_state",
        }
    return {}


def _is_page_runtime_field(field: str) -> bool:
    text = str(field or "").strip()
    return text in PAGE_RUNTIME_FIELD_NAMES or text.endswith(".page")


def _checkpoint_requires_structured_state(cp: dict[str, Any], fields: list[dict[str, Any]]) -> bool:
    if not cp.get("blocking"):
        return False
    kind = str(cp.get("checkpoint_kind", ""))
    if kind in {"selection", "mutation", "collection", "draft", "projection", "outcome"}:
        return any(field.get("structured_required") for field in fields)
    return False


def _runtime_schema_owner_pages(cp: dict[str, Any]) -> list[str]:
    pages: list[str] = []
    for field in ("page", "current_page", "terminal_page"):
        value = _raw_expected_value((cp.get("expected_state") or {}).get(field))
        if isinstance(value, str) and value:
            pages.append(value)
    target = cp.get("terminal_boundary_target")
    if isinstance(target, str) and target:
        pages.append(target)
    return _dedupe(pages)


def _semantic_obligation_schema_shape(matrix: dict[str, Any]) -> str:
    traces = matrix.get("traces")
    if isinstance(traces, dict):
        return "keyed_traces"
    if isinstance(traces, list):
        return "list_traces"
    return "unknown"


def _iter_semantic_obligation_traces(matrix: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    traces = matrix.get("traces", [])
    rows: list[tuple[str, list[dict[str, Any]]]] = []
    if isinstance(traces, dict):
        iterable = traces.items()
    elif isinstance(traces, list):
        iterable = (
            (entry.get("trace_id"), entry)
            for entry in traces
            if isinstance(entry, dict)
        )
    else:
        return rows
    for tid, entry in iterable:
        if not tid or not isinstance(entry, dict):
            continue
        obligations = [
            obligation
            for obligation in entry.get("obligations", [])
            if isinstance(obligation, dict)
        ]
        rows.append((str(tid), obligations))
    return rows


def compile_runtime_event_bindings(matrix: dict[str, Any], runtime_manifest: dict[str, Any]) -> dict[str, Any]:
    bindings = []
    actions_by_id = runtime_manifest.get("actions_by_id", {})
    transactions = runtime_manifest.get("transactions_by_action_id", {})
    for row in matrix["checkpoints"]:
        global_runtime_state = row.get("global_runtime_state", {}) or {}
        action_ids = row.get("global_runtime_state", {}).get("source_action_ids") or [
            f"{row['trace_id']}:step{step}" for step in row.get("source_steps", [])
        ]
        action_aliases = [
            str(alias)
            for alias in (global_runtime_state.get("action_aliases") or [])
            if str(alias).strip()
        ]
        state_refs = global_runtime_state.get("state_refs", [])
        legacy_state_refs = global_runtime_state.get("legacy_state_refs") or state_refs
        canonical_state_refs = global_runtime_state.get("canonical_state_refs") or _explicit_canonical_state_refs(state_refs)
        downstream_extension_refs = global_runtime_state.get("downstream_extension_refs") or []
        expected_state = row.get("expected_state") or {}
        expected_state_diff = global_runtime_state.get("expected_state_diff") or {}
        canonical_expected_state_diff = global_runtime_state.get("canonical_expected_state_diff") or {}
        downstream_expected_state_diff = global_runtime_state.get("downstream_expected_state_diff") or {}
        payload_required = _runtime_binding_payload_required(row, expected_state, expected_state_diff)
        route_only = bool(row.get("route_only_justification"))
        bindings.append({
            "trace_id": row["trace_id"],
            "checkpoint_id": row["checkpoint_id"],
            "checkpoint_kind": row["checkpoint_kind"],
            "supervision": row["supervision"],
            "source_steps": list(row.get("source_steps") or []),
            "event": row.get("event", f"ghostweb:checkpoint:{row['checkpoint_id']}"),
            "source_action_ids": action_ids,
            "action_aliases": action_aliases,
            "payload_fields": row.get("payload_fields") or _default_payload_fields(row),
            "payload_required": payload_required,
            "expected_state": expected_state,
            "expected_state_diff": expected_state_diff,
            "state_refs": state_refs,
            "legacy_state_refs": legacy_state_refs,
            "canonical_state_refs": canonical_state_refs,
            "downstream_extension_refs": downstream_extension_refs,
            "canonical_expected_state_diff": canonical_expected_state_diff,
            "downstream_expected_state_diff": downstream_expected_state_diff,
            "runtime_interface": {
                "manifest_actions_bound": [action_id for action_id in action_ids if action_id in actions_by_id],
                "manifest_transactions_bound": [action_id for action_id in action_ids if action_id in transactions],
                "legacy_state_refs": legacy_state_refs,
                "canonical_state_refs": canonical_state_refs,
                "downstream_extension_refs": downstream_extension_refs,
                "canonical_expected_state_diff": canonical_expected_state_diff,
                "downstream_expected_state_diff": downstream_expected_state_diff,
                "requires_runtime_state": bool(
                    row.get("blocking")
                    and (canonical_state_refs or downstream_extension_refs or state_refs)
                    and not route_only
                ),
                "route_only_justification": row.get("route_only_justification", ""),
            },
            "binding_status": "static_contract_bound",
        })
    return {
        "format": "ghostweb_runtime_event_bindings",
        "schema_revision": 1,
        "source": "contract_instrumentation.runtime_event_binding",
        "runtime_bridge_status": _runtime_bridge_status(runtime_manifest),
        "bindings": bindings,
        "summary": {
            "binding_count": len(bindings),
            "blocking_binding_count": sum(1 for row in matrix["checkpoints"] if row.get("blocking")),
        },
    }


def _runtime_binding_payload_required(
    row: dict[str, Any],
    expected_state: dict[str, Any],
    expected_state_diff: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for assertion in row.get("runtime_event_assertions", []) or []:
        if not isinstance(assertion, dict):
            continue
        required = assertion.get("payload_required")
        if isinstance(required, dict):
            payload.update(required)
    payload.update(expected_state)
    payload.update(expected_state_diff)
    payload.setdefault("trace_id", row.get("trace_id", ""))
    payload.setdefault("checkpoint_id", row.get("checkpoint_id", ""))
    return payload


def _canonical_assertion_fields(row: dict[str, Any]) -> list[str]:
    excluded = {"terminal_policy", "allowed_final_state", "outcome_fields"}
    fields: set[str] = set()
    for key in (row.get("expected_state", {}) or {}):
        if str(key) not in excluded:
            fields.add(str(key))
    state = row.get("global_runtime_state", {}) or {}
    for diff_key in ("canonical_expected_state_diff", "downstream_expected_state_diff", "expected_state_diff"):
        for key in (state.get(diff_key, {}) or {}):
            if str(key) not in excluded:
                fields.add(str(key))
    return sorted(fields)


def _canonical_state_assertion(row: dict[str, Any]) -> dict[str, Any]:
    state = row.get("global_runtime_state", {}) or {}
    canonical_refs = list(state.get("canonical_state_refs") or [])
    downstream_refs = list(state.get("downstream_extension_refs") or [])
    legacy_refs = list(state.get("legacy_state_refs") or state.get("state_refs") or [])
    canonical_diff = dict(state.get("canonical_expected_state_diff") or {})
    downstream_diff = dict(state.get("downstream_expected_state_diff") or {})
    legacy_diff = dict(state.get("expected_state_diff") or {})
    return {
        "checkpoint_id": row["checkpoint_id"],
        "state_refs": canonical_refs or downstream_refs or legacy_refs,
        "legacy_state_refs": legacy_refs,
        "canonical_state_refs": canonical_refs,
        "downstream_extension_refs": downstream_refs,
        "expected_state": row.get("expected_state", {}),
        "expected_state_diff": canonical_diff or downstream_diff or legacy_diff,
        "legacy_expected_state_diff": legacy_diff,
        "canonical_expected_state_diff": canonical_diff,
        "downstream_expected_state_diff": downstream_diff,
        "fields": _canonical_assertion_fields(row),
    }


def compile_evaluators(tasks: list[dict[str, Any]], matrix: dict[str, Any], terminal_verified: dict[str, Any]) -> dict[str, Any]:
    rows_by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in matrix["checkpoints"]:
        rows_by_trace.setdefault(row["trace_id"], []).append(row)

    evaluators = []
    for task in tasks:
        task_id = task["id"]
        rows = rows_by_trace.get(task_id, [])
        outcome = next((row for row in rows if row["supervision"] == "outcome"), None)
        policy = terminal_verified["trace_policies"][task_id]
        process_checkpoint_ids = [row["checkpoint_id"] for row in rows if row["supervision"] == "process"]
        blocking_checkpoint_ids = [row["checkpoint_id"] for row in rows if row.get("blocking")]
        outcome_checkpoint_id = outcome["checkpoint_id"] if outcome else None
        expected_change_refs = _expected_change_refs(rows)
        evaluators.append({
            "task_id": task_id,
            "name": f"Contract evaluator for {task_id}",
            "description": task.get("name") or task.get("description", ""),
            "schema_version": 2,
            "source": "contract_instrumentation.checkpoint_evaluator",
            "localStorage_variables": [],
            "evaluation_logic": "checkpoint_matrix_v1",
            "checkpoint_matrix_ref": "phase_post_processing/instrumentation/checkpoint_matrix.json",
            "terminal_policy_ref": "phase_post_processing/terminal_policy/terminal_policy_verified.json",
            "terminal_policy": policy["verified_policy"],
            "process_checkpoints": process_checkpoint_ids,
            "outcome_checkpoint": outcome_checkpoint_id,
            "evaluation_contract": {
                "contract_style": "appwise_semantic_state",
                "result_fields": ["success", "clean", "progress", "passed", "issues", "warnings"],
                "pass_rule": "passed = success && clean",
                "success_contract": {
                    "required_checkpoints": blocking_checkpoint_ids,
                    "outcome_checkpoint": outcome_checkpoint_id,
                    "forbidden_terminal_events": policy.get("forbidden_terminal_events", []),
                    "required_mock_state": policy.get("required_mock_state", []),
                },
                "progress_contract": {
                    "process_checkpoints": process_checkpoint_ids,
                    "denominator": len(process_checkpoint_ids),
                    "metric": "passed_process_checkpoints / process_checkpoint_count",
                },
                "clean_contract": {
                    "diff_source": "init_current_appwise_state",
                    "expected_change_refs": expected_change_refs,
                    "ignored_state_namespaces": ["transient_ui", "_temp", "os.keyboard", "loading", "focus"],
                    "failure_codes": [
                        "unexpected_state_diff",
                        "forbidden_terminal_event",
                        "real_side_effect_for_simulated_commit",
                        "stale_event_or_route",
                        "wrong_entity_payload",
                    ],
                },
            },
            "reward_contract": {
                "pass_rule": "success && clean",
                "requires": blocking_checkpoint_ids,
                "forbids": policy.get("forbidden_terminal_events", []),
                "required_mock_state": policy.get("required_mock_state", []),
                "expected_change_refs": expected_change_refs,
                "canonical_state_assertions": [
                    _canonical_state_assertion(row)
                    for row in rows
                    if row.get("global_runtime_state")
                ],
                "projection_assertions": [
                    {
                        "checkpoint_id": row["checkpoint_id"],
                        "projection_checks": row.get("projection_checks", []),
                    }
                    for row in rows
                    if row.get("projection_checks")
                ],
            },
            "negative_conditions": _negative_conditions(rows, policy),
        })
    return {
        "format": "ghostweb_contract_evaluators",
        "schema_revision": 1,
        "source": "contract_instrumentation.checkpoint_evaluator",
        "evaluator_count": len(evaluators),
        "evaluators": evaluators,
    }


def compile_test_logic(matrix: dict[str, Any], terminal_verified: dict[str, Any]) -> dict[str, Any]:
    rows_by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in matrix["checkpoints"]:
        rows_by_trace.setdefault(row["trace_id"], []).append(row)

    flows = {}
    for task_id, rows in rows_by_trace.items():
        policy = terminal_verified["trace_policies"][task_id]
        source_actions = []
        for row in rows:
            source_actions.extend(f"{task_id}:step{step}" for step in row.get("source_steps", []))
        source_actions = _dedupe(source_actions)
        negative_probes = []
        for row in rows:
            negative_probes.extend(row.get("negative_probes", []))
        if policy["verified_policy"] == "precommit_ready":
            negative_probes.append({
                "probe_id": "forbidden_submit_or_payment",
                "forbidden_events": policy.get("forbidden_terminal_events", []),
                "must_fail": [row["checkpoint_id"] for row in rows if row["supervision"] == "outcome"],
            })
        if policy["verified_policy"] == "simulated_commit":
            negative_probes.append({
                "probe_id": "real_success_spoof",
                "forbidden_events": policy.get("forbidden_terminal_events", []),
                "must_fail": [row["checkpoint_id"] for row in rows if row["supervision"] == "outcome"],
            })
        elif "real_payment_success" in set(policy.get("forbidden_terminal_events", []) or []):
            negative_probes.append({
                "probe_id": "real_success_spoof",
                "forbidden_events": policy.get("forbidden_terminal_events", []),
                "must_fail": [row["checkpoint_id"] for row in rows if row["supervision"] == "outcome"],
            })
        negative_probes = _dedupe_probes(negative_probes)
        flows[task_id] = {
            "trace_id": task_id,
            "positive_flow": {
                "source_action_ids": source_actions,
                "expected_checkpoints": [row["checkpoint_id"] for row in rows],
            },
            "negative_probes": negative_probes,
            "runtime_bridge_status": matrix.get("runtime_bridge_status", "unknown"),
        }
    return {
        "format": "ghostweb_instrumentation_test_logic",
        "schema_revision": 1,
        "source": "contract_instrumentation.test_logic_generator",
        "trace_flows": flows,
        "summary": {
            "trace_count": len(flows),
            "negative_probe_count": sum(len(flow["negative_probes"]) for flow in flows.values()),
        },
    }


def build_source_env_provenance(output_dir: Path, source_env_dir: Path) -> dict[str, Any]:
    hashes = []
    for rel in _required_source_artifacts(output_dir):
        path = output_dir / rel
        hashes.append({
            "path": rel,
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        })
    return {
        "format": "ghostweb_instrumentation_source_env_provenance",
        "schema_revision": 1,
        "source_env_dir": str(source_env_dir),
        "output_dir": str(output_dir),
        "source_env_readonly": str(source_env_dir) != str(output_dir),
        "generated_at": _now(),
        "required_artifacts": hashes,
        "deprecated_dir_count": sum(1 for _ in output_dir.rglob("deprecated")),
    }


def build_instrumented_business_logic(output_dir: Path, bindings: dict[str, Any], matrix: dict[str, Any]) -> str:
    original = (output_dir / "phase_sdk_generation/runtime/business_logic.js").read_text()
    contract_json = json.dumps(
        {
            "format": "ghostweb_static_instrumentation_contract",
            "runtime_bridge_status": matrix.get("runtime_bridge_status"),
            "binding_count": bindings["summary"]["binding_count"],
            "checkpoint_count": matrix["summary"]["checkpoint_count"],
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        original
        + "\n\n"
        + "/* Ghostweb static checkpoint contract generated by contract_instrumentation. */\n"
        + "(function(){\n"
        + "  var root = typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : {});\n"
        + f"  root.GhostwebStaticInstrumentationContract = {contract_json};\n"
        + "})();\n"
    )


def build_generation_result(
    output_dir: Path,
    terminal_verified: dict[str, Any],
    matrix: dict[str, Any],
    evaluators: dict[str, Any] | None,
    test_logic: dict[str, Any] | None,
    provenance: dict[str, Any],
    *,
    generate_instrumented_business_logic: bool = True,
) -> dict[str, Any]:
    existing_path = output_dir / "phase_post_processing/debug/data_manager/generation_result.json"
    existing = _load_json(existing_path) if existing_path.exists() else {}
    evaluator_generated = evaluators is not None
    test_logic_generated = test_logic is not None
    mode = (
        "instrumentation_only_contract_first"
        if evaluator_generated or test_logic_generated
        else "instrumentation_artifacts_only_contract_first"
    )
    optional_artifacts = {
        "instrumented_business_logic": {
            "generated": bool(generate_instrumented_business_logic),
            "path": (
                "phase_sdk_generation/runtime/instrumented_business_logic.js"
                if generate_instrumented_business_logic
                else None
            ),
        },
        "evaluators": {
            "generated": evaluator_generated,
            "path": "phase_post_processing/evaluators/evaluators.json" if evaluator_generated else None,
        },
        "test_logic": {
            "generated": test_logic_generated,
            "path": "phase_post_processing/test_logic/test_flows.json" if test_logic_generated else None,
        },
        "compiled_test_flows": {
            "generated": test_logic_generated,
            "path": (
                "phase_sdk_generation/validation/test_flows/test_flows.js"
                if test_logic_generated
                else None
            ),
        },
    }
    existing.update({
        "success": True,
        "mode": mode,
        "generated_at": _now(),
        "terminal_policy_artifact": "phase_post_processing/terminal_policy/terminal_policy_verified.json",
        "checkpoint_matrix_artifact": "phase_post_processing/instrumentation/checkpoint_matrix.json",
        "runtime_state_schema_artifact": "phase_post_processing/instrumentation/runtime_state_schema.json",
        "checkpoint_matrix_validation_report": "phase_post_processing/instrumentation/checkpoint_matrix_validation_report.json",
        "runtime_event_bindings_artifact": "phase_post_processing/instrumentation/runtime_event_bindings.json",
        "evaluators_artifact": optional_artifacts["evaluators"]["path"],
        "test_logic_artifact": optional_artifacts["test_logic"]["path"],
        "source_env_provenance_artifact": "phase_post_processing/instrumentation/source_env_provenance.json",
        "terminal_policy_summary": terminal_verified["summary"],
        "checkpoint_matrix_summary": matrix["summary"],
        "evaluators_count": evaluators["evaluator_count"] if evaluator_generated else 0,
        "test_logic_summary": test_logic["summary"] if test_logic_generated else {
            "trace_count": 0,
            "negative_probe_count": 0,
            "generated": False,
        },
        "evaluators": evaluators["evaluators"] if evaluator_generated else [],
        "optional_artifacts": optional_artifacts,
        "runtime_bridge_status": matrix.get("runtime_bridge_status"),
        "source_env_hash_count": len(provenance["required_artifacts"]),
    })
    return existing


def copy_env_without_deprecated(source: Path, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        return
    if output.exists():
        output.rmdir()
    shutil.copytree(source, output, ignore=shutil.ignore_patterns("deprecated"), ignore_dangling_symlinks=True)


def _compile_from_semantic_obligations(
    task: dict[str, Any],
    sto_obligations: list[dict[str, Any]],
    capture_steps: list[dict[str, Any]],
    replay_trace: dict[str, Any],
    terminal_verified: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    task_id = task["id"]
    rows: list[dict[str, Any]] = []
    for sto in sto_obligations:
        if not isinstance(sto, dict):
            continue
        if not _semantic_obligation_has_state(sto):
            continue
        supervision = str(sto.get("supervision", "process"))
        kind = _semantic_checkpoint_kind(sto, supervision)
        if kind == "outcome":
            supervision = "outcome"
        obligation_id = str(sto.get("obligation_id", "") or "")
        checkpoint_id = str(sto.get("checkpoint_id") or obligation_id or f"cp_sto_{len(rows):02d}")
        if checkpoint_id.startswith(f"sto:{task_id}:"):
            checkpoint_id = checkpoint_id.replace(f"sto:{task_id}:", "cp_")
        source_steps = sto.get("source_steps", [])
        if not isinstance(source_steps, list):
            source_steps = []
        state_refs = _semantic_state_refs(sto)
        expected_state = _semantic_expected_state(sto, supervision)
        expected_diff = _semantic_expected_state_diff(sto, expected_state)
        projections = _semantic_projection_assertions(sto)
        negative_probes = _semantic_negative_probes(sto, checkpoint_id, expected_state, expected_diff)
        payload_fields = _dedupe(
            [
                *[str(field) for field in (sto.get("payload_fields") or []) if field],
                "trace_id",
                "checkpoint_id",
                *expected_state.keys(),
                *expected_diff.keys(),
            ]
        )
        row: dict[str, Any] = {
            "trace_id": task_id,
            "checkpoint_id": checkpoint_id,
            "checkpoint_kind": kind,
            "supervision": supervision,
            "candidate_types": [kind],
            "source_steps": source_steps,
            "expected_state_id": f"{kind}={checkpoint_id}",
            "expected_state": expected_state,
            "global_runtime_state": {
                "state_refs": state_refs,
                "expected_state_diff": expected_diff,
                "source_action_ids": [f"{task_id}:step{s}" for s in source_steps],
                "write_read_closed": bool(state_refs),
            },
            "event": f"ghostweb:checkpoint:{checkpoint_id}",
            "payload_fields": payload_fields,
            "reward_weight": 0.3 if supervision == "outcome" else 0.1,
            "blocking": bool(sto.get("blocking", True)),
            "projection_assertions": projections,
            "projection_checks": projections,
            "negative_probes": negative_probes,
            "obligation_id": obligation_id or checkpoint_id,
            "source": "semantic_task_obligation_matrix",
        }
        if supervision == "outcome":
            row["terminal_policy"] = str(sto.get("terminal_policy", "observed_terminal_state"))
            row["forbidden_events"] = sto.get("forbidden_events", [])
            row["required_mock_state"] = sto.get("required_mock_state", [])
            row["depends_on"] = sto.get("depends_on", [])
            terminal_boundary_target = _semantic_terminal_boundary_target(sto, expected_state)
            row["terminal_boundary_target"] = terminal_boundary_target
            if terminal_boundary_target:
                row["terminal_boundary_override_source"] = "semantic_task_obligation_matrix"
        rows.append(row)
    report = {
        "trace_id": task_id,
        "checkpoint_count": len(rows),
        "source": "semantic_task_obligation_matrix",
        "contract_gaps": [],
    }
    return rows, report


def _semantic_checkpoint_kind(sto: dict[str, Any], supervision: str) -> str:
    raw = str(sto.get("checkpoint_kind") or "").strip()
    if supervision == "outcome" or raw in {"outcome", "observed_terminal_state"}:
        return "outcome"
    if raw and raw != "process":
        return raw
    checkpoint_id = str(sto.get("checkpoint_id") or sto.get("obligation_id") or "")
    if "draft" in checkpoint_id or "query" in checkpoint_id or "input" in checkpoint_id:
        return "draft"
    if "cart" in checkpoint_id or "added" in checkpoint_id or "saved" in checkpoint_id:
        return "collection"
    if "selected" in checkpoint_id or "selection" in checkpoint_id or "checked" in checkpoint_id:
        return "selection"
    if "committed" in checkpoint_id or "claim" in checkpoint_id or "like" in checkpoint_id:
        return "mutation"
    if "route" in checkpoint_id:
        return "route"
    return "selection"


def _semantic_state_refs(sto: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    state_ref = sto.get("state_ref")
    if isinstance(state_ref, str) and state_ref:
        refs.append(state_ref)
    state_refs = sto.get("state_refs", [])
    if isinstance(state_refs, list):
        refs.extend(str(ref) for ref in state_refs if ref)
    global_state_refs = (sto.get("global_runtime_state") or {}).get("state_refs", [])
    if isinstance(global_state_refs, list):
        refs.extend(str(ref) for ref in global_state_refs if ref)
    if not refs and (sto.get("source_page") or sto.get("target_page")):
        refs.append("state::page_runtime")
    return _dedupe(refs)


def _semantic_expected_state(sto: dict[str, Any], supervision: str) -> dict[str, Any]:
    expected = sto.get("expected_state")
    if isinstance(expected, dict) and expected:
        return {
            key: _with_field_scope(value, "terminal_observable" if supervision == "outcome" else "process_prerequisite")
            for key, value in expected.items()
            if not _is_semantic_operation_field(key, value, expected)
        }
    expected_state: dict[str, Any] = {}
    canonical = sto.get("canonical_entity")
    if isinstance(canonical, dict):
        for key, value in canonical.items():
            if value is not None and value != "":
                expected_state[str(key)] = value
    target_page = sto.get("target_page") or sto.get("page")
    if target_page and "page" not in expected_state and "current_page" not in expected_state:
        expected_state["page" if supervision == "process" else "current_page"] = str(target_page)
    delta = sto.get("expected_state_delta")
    if isinstance(delta, dict) and delta:
        field = str(delta.get("field") or "state")
        evidence = delta.get("evidence")
        if evidence is not None and evidence != "":
            expected_state.setdefault(field, evidence)
    if not expected_state:
        for key, value in _semantic_expected_state_diff(sto, {}).items():
            if value is not None and value != "":
                expected_state[str(key)] = value
    return {
        key: _with_field_scope(value, "terminal_observable" if supervision == "outcome" else "process_prerequisite")
        for key, value in expected_state.items()
        if not _is_semantic_operation_field(key, value, expected_state)
    }


def _with_field_scope(value: Any, field_scope: str) -> Any:
    if isinstance(value, dict) and "field_scope" in value:
        return value
    if isinstance(value, dict) and "value" in value:
        out = dict(value)
        out.setdefault("field_scope", field_scope)
        return out
    return {"field_scope": field_scope, "value": value}


def _semantic_expected_state_diff(sto: dict[str, Any], expected_state: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    global_diff = (sto.get("global_runtime_state") or {}).get("expected_state_diff")
    if isinstance(global_diff, dict):
        diff.update(global_diff)
    direct_diff = sto.get("expected_state_diff")
    if isinstance(direct_diff, dict):
        diff.update(direct_diff)
    delta = sto.get("expected_state_delta")
    if isinstance(delta, dict) and delta:
        field = str(delta.get("field") or "state")
        evidence = delta.get("evidence")
        if evidence is not None and evidence != "":
            diff.setdefault(field, evidence)
    if not diff:
        for key, value in expected_state.items():
            raw = value.get("value") if isinstance(value, dict) and "value" in value else value
            diff[key] = raw
    return {
        str(key): _normalize_expected_diff_value(value)
        for key, value in diff.items()
        if not _is_semantic_operation_field(key, value, diff)
    }


def _semantic_obligation_has_state(sto: dict[str, Any]) -> bool:
    state_sources = (
        sto.get("checkpoint_id"),
        sto.get("obligation_id"),
        sto.get("expected_state"),
        sto.get("expected_state_diff"),
        sto.get("expected_state_delta"),
        (sto.get("global_runtime_state") or {}).get("expected_state_diff") if isinstance(sto.get("global_runtime_state"), dict) else None,
        sto.get("projection_assertions"),
        sto.get("projection_checks"),
        sto.get("canonical_entity"),
    )
    for value in state_sources:
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, list) and value:
            return True
    return False


def _semantic_terminal_boundary_target(sto: dict[str, Any], expected_state: dict[str, Any]) -> str:
    for raw in (
        sto.get("terminal_boundary_target"),
        (sto.get("canonical_entity") or {}).get("terminal_page") if isinstance(sto.get("canonical_entity"), dict) else None,
        sto.get("target_page"),
        sto.get("terminal_page"),
    ):
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for field in ("current_page", "terminal_page", "page"):
        value = expected_state.get(field)
        raw = _raw_expected_value(value)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _is_semantic_operation_field(key: Any, value: Any, container: dict[str, Any]) -> bool:
    field = str(key)
    if field in {"terminal_policy", "allowed_final_state", "outcome_fields"}:
        return True
    if field == "commit_action":
        return False
    if field in {"operation_intent", "interaction_type", "target_area"}:
        return True
    if field in {"action", "entry_point"} and len(container) > 1:
        return True
    if field.endswith("_action") and len(container) > 1:
        return True
    if isinstance(value, str) and (field.startswith("state::") or field.endswith(".active")):
        return any(token in value for token in ("点击", "输入", "选择", "打开", "返回", "滑动", "搜索按钮"))
    return False


def _semantic_projection_assertions(sto: dict[str, Any]) -> list[dict[str, Any]]:
    raw = sto.get("projection_assertions") or sto.get("projection_checks") or []
    if not isinstance(raw, list):
        return []
    assertions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out = dict(item)
        if "contains" in out and "value" not in out:
            contains = out.get("contains")
            if isinstance(contains, list) and contains:
                out["value"] = contains[0]
            elif contains:
                out["value"] = contains
        out.setdefault("op", "contains_visible")
        assertions.append(out)
    return assertions


def _semantic_negative_probes(
    sto: dict[str, Any],
    checkpoint_id: str,
    expected_state: dict[str, Any],
    expected_diff: dict[str, Any],
) -> list[dict[str, Any]]:
    probes = sto.get("negative_probes") or []
    if not isinstance(probes, list):
        return []
    normalized: list[dict[str, Any]] = []
    for probe in probes:
        if isinstance(probe, str):
            normalized.append(_probe_from_id(probe, checkpoint_id, expected_state, expected_diff))
        elif isinstance(probe, dict):
            row = dict(probe)
            row.setdefault("must_fail", [checkpoint_id])
            mutate = row.get("mutate")
            if (not mutate) and not row.get("forbidden_events"):
                inferred = _probe_from_id(str(row.get("probe_id", "")), checkpoint_id, expected_state, expected_diff)
                row.update(inferred)
                if inferred.get("mutate"):
                    row.pop("probe_unbound_to_assertion", None)
            normalized.append(row)
    return _dedupe_probes(normalized)


def _probe_from_id(
    probe_id: str,
    checkpoint_id: str,
    expected_state: dict[str, Any],
    expected_diff: dict[str, Any],
) -> dict[str, Any]:
    if probe_id in {"real_payment", "order_completed", "paid", "real_success", "real_success_spoof"}:
        return {
            "probe_id": probe_id,
            "forbidden_events": ["order_completed", "paid", "real_payment_success", "real_external_side_effect"],
            "must_fail": [checkpoint_id],
        }
    mutate: dict[str, Any] = {}
    field_candidates = _probe_field_candidates(probe_id, expected_state, expected_diff)
    for field in field_candidates:
        value = _raw_expected_value(expected_state.get(field, expected_diff.get(field)))
        if "too_long" in probe_id:
            mutate[field] = "x" * 512
        elif "wrong_position" in probe_id or probe_id.startswith("wrong_"):
            mutate[field] = _wrong_value(value, probe_id)
        elif (
            probe_id.startswith(("missing_", "no_", "empty_", "not_"))
            or probe_id.endswith((
                "_empty",
                "_incomplete",
                "_unavailable",
                "_not_selected",
                "_not_shown",
                "_not_loaded",
                "_not_set",
                "_not_found",
                "_not_confirmed",
                "_not_ready",
                "_not_saved",
                "_not_deleted",
                "_not_updated",
                "_not_sent",
                "_not_visible",
                "_not_in_results",
            ))
        ):
            mutate[field] = _empty_value_for_probe(value)
        elif probe_id.startswith("wrong_"):
            mutate[field] = _wrong_value(value, probe_id)
        elif "too_many" in probe_id:
            mutate[field] = [value, f"extra::{probe_id}"] if not isinstance(value, list) else [*value, f"extra::{probe_id}"]
        elif "not_deselected" in probe_id:
            mutate[field] = [f"not_deselected::{probe_id}"]
    return {
        "probe_id": probe_id,
        "mutate": mutate,
        "must_fail": [checkpoint_id],
        "probe_unbound_to_assertion": not bool(mutate),
    }


def _probe_field_candidates(probe_id: str, expected_state: dict[str, Any], expected_diff: dict[str, Any]) -> list[str]:
    fields = list(dict.fromkeys([*expected_state.keys(), *expected_diff.keys()]))
    probe_tokens = [token for token in re.split(r"[_\W]+", probe_id) if token and token not in {"wrong", "missing", "empty", "no", "not", "selected", "loaded", "shown", "too", "many"}]
    if probe_tokens:
        matched = [field for field in fields if any(token.lower() in field.lower() for token in probe_tokens)]
        if matched:
            return matched
    semantic_tokens = {
        "destination": ["destination", "draft", "query"],
        "address": ["address", "delivery"],
        "payment": ["payment", "method"],
        "coupon": ["coupon", "selected", "checked"],
        "vehicle": ["vehicle", "checked", "selected", "deselected"],
        "merchant": ["merchant", "store"],
        "store": ["store", "merchant"],
        "item": ["item", "product", "cart"],
        "product": ["product", "item", "cart"],
        "query": ["query", "draft", "search"],
        "message": ["message", "chat"],
        "favorite": ["favorite", "favorited", "collected"],
        "favorited": ["favorite", "favorited", "collected"],
        "time": ["time", "departure", "schedule"],
        "ticket": ["ticket", "seat", "booking"],
        "ready": ["ready", "cta", "checkout", "form"],
        "form": ["form", "ready", "address", "contact"],
        "remark": ["remark", "note"],
        "poi": ["poi", "address", "destination"],
    }
    for token, aliases in semantic_tokens.items():
        if token in probe_id:
            matched = [field for field in fields if any(alias in field.lower() for alias in aliases)]
            if matched:
                return matched
    non_page = [field for field in fields if field not in {"page", "current_page", "route"}]
    return non_page[:1] or fields[:1]


def _raw_expected_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _wrong_value(value: Any, probe_id: str) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 999
    if isinstance(value, list):
        return [f"__wrong__:{item}" for item in value] or [f"__wrong__:{probe_id}"]
    if value in (None, ""):
        return f"__wrong__:{probe_id}"
    return f"__wrong__:{value}"


def _empty_value_for_probe(value: Any) -> Any:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return ""


def _compile_generic_trace_matrix(
    task: dict[str, Any],
    capture_entries: list[dict[str, Any]],
    replay_trace: dict[str, Any],
    terminal_verified: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    task_id = task["id"]
    steps = replay_trace.get("steps", [])
    policy = terminal_verified["trace_policies"][task_id]
    terminal_boundary = policy.get("terminal_boundary", {})
    terminal_target = terminal_boundary.get("target_filename", "")
    process_rows: list[dict[str, Any]] = []
    route_rows: list[dict[str, Any]] = []
    skipped = []
    candidate_steps: list[tuple[str, dict[str, Any]]] = []
    route_steps: list[dict[str, Any]] = []
    for step in steps:
        step_index = _step_index(step.get("action_id", ""))
        kind = _candidate_kind(step)
        if policy["verified_policy"] in {"precommit_ready", "simulated_commit"} and _is_terminal_side_effect_step(step):
            skipped.append({"step_index": step_index, "reason": "terminal side-effect action absorbed by terminal policy"})
            continue
        if kind == "skip":
            skipped.append({"step_index": step_index, "reason": _skip_reason(step)})
            continue
        if kind == "route":
            route_steps.append(step)
            continue
        if kind in {"draft", "selection", "mutation", "collection"}:
            candidate_steps.append((kind, step))

    route_idx = 0
    for rstep in route_steps:
        r_target = rstep.get("target_filename") or rstep.get("navigation", {}).get("target_filename", "")
        if not r_target:
            continue
        if route_rows and route_rows[-1].get("expected_state", {}).get("page") == r_target:
            continue
        route_idx += 1
        r_step_index = _step_index(rstep.get("action_id", ""))
        cp_id = f"cp_route_{route_idx:02d}"
        route_row: dict[str, Any] = {
            "trace_id": task_id,
            "checkpoint_id": cp_id,
            "checkpoint_kind": "route",
            "supervision": "process",
            "candidate_types": ["route"],
            "source_steps": [r_step_index],
            "expected_state_id": f"route={r_target}",
            "expected_state": {"page": {"field_scope": "process_prerequisite", "value": r_target}},
            "global_runtime_state": {
                "state_refs": ["state::page_runtime"],
                "expected_state_diff": {"page": r_target},
                "source_action_ids": [rstep.get("action_id", "")],
                "write_read_closed": True,
            },
            "event": f"ghostweb:checkpoint:{cp_id}",
            "payload_fields": ["trace_id", "checkpoint_id", "page", "source_steps"],
            "reward_weight": 0.05,
            "blocking": False,
        }
        route_row["quality_gates"] = _quality_gates(route_row)
        route_rows.append(route_row)

    for idx, group in enumerate(_group_candidate_steps(candidate_steps), start=1):
        kind, group_steps = group
        source_steps = [_step_index(step.get("action_id", "")) for step in group_steps]
        expected_text = "; ".join(_operation_intent(step) for step in group_steps if _operation_intent(step))
        expected_state = _expected_state_for_group(kind, group_steps, expected_text)
        for k in expected_state:
            if isinstance(expected_state[k], (list, str)) and k not in {"field_scope"}:
                expected_state[k] = {"field_scope": "process_prerequisite", "value": expected_state[k]}
        state_refs = _dedupe([
            ref
            for step in group_steps
            for ref in _state_refs(step)
            if ref and ref != "state::page_runtime"
        ])
        checkpoint_id = f"cp_{kind}_{idx:02d}"
        row = {
            "trace_id": task_id,
            "checkpoint_id": checkpoint_id,
            "checkpoint_kind": kind,
            "supervision": "process",
            "candidate_types": [kind],
            "source_steps": source_steps,
            "expected_state_id": f"{kind}={idx:02d}",
            "expected_state": expected_state,
            "global_runtime_state": {
                "state_refs": state_refs,
                "expected_state_diff": _expected_state_diff_for_group(kind, group_steps, expected_text),
                "source_action_ids": [step["action_id"] for step in group_steps],
                "write_read_closed": bool(state_refs),
            },
            "event": f"ghostweb:checkpoint:{checkpoint_id}",
            "payload_fields": ["trace_id", "checkpoint_id", "operation_intent", "source_steps"],
            "reward_weight": 0.1,
            "blocking": True,
        }
        if kind == "selection" and not state_refs and any((step.get("reducer") or "") == "navigate_route" for step in group_steps):
            row["route_only_justification"] = "navigation route selection has no canonical state commit in upstream env contract"
        row["quality_gates"] = _quality_gates(row)
        process_rows.append(row)
        if len(process_rows) >= 6:
            break

    all_process_rows = route_rows + process_rows
    if not all_process_rows:
        final_route_step = next((step for step in reversed(steps) if _candidate_kind(step) == "route"), None)
        if final_route_step:
            all_process_rows.append(_route_checkpoint(task_id, final_route_step))

    last_step = _last_task_relevant_step(capture_entries, task.get("steps", []))
    outcome_id = _outcome_id(policy["verified_policy"])
    outcome_state, outcome_diff, outcome_state_refs = _aggregate_outcome_state(
        [row for row in all_process_rows if row.get("blocking")]
    )
    if outcome_state:
        outcome_state = _rescope_expected_state(outcome_state, "terminal_observable")
    outcome_page = terminal_target
    if not outcome_state:
        raise RuntimeError(
            f"outcome checkpoint for {task_id} has no expected_state from process checkpoints or terminal contract"
        )
    if not outcome_page:
        for field in ("current_page", "page", "page_id", "terminal_page"):
            raw_page = _raw_expected_value(outcome_state.get(field))
            if isinstance(raw_page, str) and raw_page:
                outcome_page = raw_page
                break
    if not outcome_page:
        raise RuntimeError(f"outcome checkpoint for {task_id} has no terminal boundary target")
    if outcome_page:
        outcome_state.setdefault("current_page", {"field_scope": "terminal_observable", "value": outcome_page})
        outcome_diff.setdefault("current_page", outcome_page)
    outcome_state_refs = _dedupe(["state::page_runtime", *outcome_state_refs])
    blocking_ids = [row["checkpoint_id"] for row in all_process_rows if row.get("blocking")]
    outcome_row = {
        "trace_id": task_id,
        "checkpoint_id": outcome_id,
        "checkpoint_kind": "outcome",
        "supervision": "outcome",
        "candidate_types": ["outcome"],
        "source_steps": [last_step] if last_step is not None else [],
        "excluded_steps": [{"step_index": _terminal_step(task.get("steps", [])), "reason": "terminal summary is not outcome authority"}],
        "terminal_policy": policy["verified_policy"],
        "terminal_boundary_target": outcome_page,
        "expected_state_id": f"{task_id}.{policy['allowed_final_state']}",
        "depends_on": blocking_ids,
        "expected_state": outcome_state,
        "global_runtime_state": {
            "state_refs": outcome_state_refs,
            "expected_state_diff": outcome_diff,
            "source_action_ids": [],
            "write_read_closed": True,
        },
        "forbidden_events": policy.get("forbidden_terminal_events", []),
        "required_mock_state": policy.get("required_mock_state", []),
        "event": f"ghostweb:checkpoint:{outcome_id}",
        "payload_fields": ["trace_id", "checkpoint_id", "terminal_policy", "source_steps"],
        "reward_weight": 0.3,
        "blocking": True,
    }
    outcome_row["quality_gates"] = _quality_gates(outcome_row)
    rows = all_process_rows + [outcome_row]
    report = {
        "status": "passed",
        "kept_checkpoints": [row["checkpoint_id"] for row in rows],
        "absorbed_steps": [],
        "skipped_steps": skipped[:20],
        "contract_gaps": [],
        "quality_gate_failures": [],
    }
    return rows, report


def _aggregate_outcome_state(process_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    expected_state: dict[str, Any] = {}
    expected_diff: dict[str, Any] = {}
    state_refs: list[str] = []
    for row in process_rows:
        for key, value in (row.get("expected_state") or {}).items():
            if key not in {"allowed_final_state", "task", "operation_intent"}:
                expected_state[key] = value
        state = row.get("global_runtime_state", {}) or {}
        for key, value in (state.get("expected_state_diff") or {}).items():
            if key != "operation_intent":
                expected_diff[key] = value
                expected_state.setdefault(key, value)
        state_refs.extend(state.get("state_refs", []) or [])
    return expected_state, expected_diff or expected_state.copy(), _dedupe(state_refs)


def _rescope_expected_state(expected_state: dict[str, Any], field_scope: str) -> dict[str, Any]:
    scoped: dict[str, Any] = {}
    for key, value in expected_state.items():
        if value in (None, ""):
            continue
        if isinstance(value, dict) and "value" in value:
            updated = dict(value)
            updated["field_scope"] = field_scope
            scoped[key] = updated
        else:
            scoped[key] = _with_field_scope(value, field_scope)
    return scoped


def _route_checkpoint(task_id: str, step: dict[str, Any]) -> dict[str, Any]:
    step_index = _step_index(step.get("action_id", ""))
    target = step.get("target_filename") or step.get("navigation", {}).get("target_filename")
    row = {
        "trace_id": task_id,
        "checkpoint_id": "cp_route_ready",
        "checkpoint_kind": "route",
        "supervision": "process",
        "candidate_types": ["route"],
        "source_steps": [step_index],
        "expected_state_id": f"route={target}",
        "expected_state": {"page": target},
        "global_runtime_state": {
            "state_refs": ["state::page_runtime"],
            "expected_state_diff": {"page": target},
            "source_action_ids": [step.get("action_id", "")],
            "write_read_closed": True,
        },
        "event": "ghostweb:checkpoint:cp_route_ready",
        "payload_fields": ["trace_id", "checkpoint_id", "page", "source_steps"],
        "reward_weight": 0.1,
        "blocking": True,
    }
    row["quality_gates"] = _quality_gates(row)
    return row


def _group_candidate_steps(candidate_steps: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for kind, step in candidate_steps:
        refs = set(_state_refs(step))
        if groups:
            prev_kind, prev_steps = groups[-1]
            prev_refs = set(ref for s in prev_steps for ref in _state_refs(s))
            if kind == prev_kind and refs and refs & prev_refs:
                prev_steps.append(step)
                continue
        groups.append((kind, [step]))
    return groups


def _candidate_kind(step: dict[str, Any]) -> str:
    reducer = step.get("reducer") or step.get("transaction", {}).get("reducer", "")
    op = step.get("operation") or step.get("transaction", {}).get("operation", "")
    intent = _operation_intent(step)
    if reducer in {"open_app", "scroll_viewport", "agent_execute"}:
        return "skip"
    if op == "swipe":
        return "skip"
    if reducer == "navigate_route":
        if any(token in intent for token in ("选择", "点击常用地址", "提交", "支付", "确认")):
            return "selection"
        return "route"
    if reducer in {"fill_form", "project_suggestions"}:
        return "draft"
    if reducer == "commit_query":
        return "selection"
    if reducer == "activate_inline_state":
        if any(token in intent for token in ("搜索结果", "标签", "勾选", "选择", "确认", "提交", "支付", "加入", "领取", "收藏")):
            return "selection"
        if any(token in intent for token in ("输入框", "入口", "打开", "填写价格")):
            return "skip"
        return "activation"
    if any(token in intent for token in ("提交", "支付", "下单", "领取", "收藏", "加入购物车")):
        return "mutation"
    return "skip"


def _skip_reason(step: dict[str, Any]) -> str:
    reducer = step.get("reducer") or step.get("transaction", {}).get("reducer", "")
    if reducer == "open_app":
        return "open_app"
    if reducer == "scroll_viewport":
        return "scroll-only"
    if reducer == "agent_execute":
        return "terminal summary"
    if reducer == "activate_inline_state":
        return "activation/focus-only absorbed"
    return "not reward-relevant"


def _expected_state_for_group(kind: str, group_steps: list[dict[str, Any]], expected_text: str) -> dict[str, Any]:
    text = _sanitize_operation_intent(expected_text)
    if kind == "draft":
        typed_values = _extract_typed_values(text)
        if typed_values:
            return {"draft_values": typed_values}
        return {"draft_semantic": text or "draft"}
    if kind == "selection":
        labels = _extract_quoted_values(text)
        if labels:
            return {"selected_values": labels}
        return {"selected_semantic": text or "selection"}
    if kind == "mutation":
        labels = _extract_quoted_values(text)
        if labels:
            return {"mutated_values": labels}
        return {"mutation_semantic": text or "mutation"}
    return {"semantic_action": text or kind}


def _expected_state_diff_for_group(kind: str, group_steps: list[dict[str, Any]], expected_text: str) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for step in group_steps:
        diff.update(step.get("assertions", {}).get("expected_state_diff", {}) or {})
        diff.update(step.get("transaction", {}).get("state_commit", {}).get("expected_state_diff", {}) or {})
    diff = {
        key: _normalize_expected_diff_value(value)
        for key, value in diff.items()
        if key != "operation_intent"
    }
    if not diff:
        diff.update(_semantic_diff_from_text(kind, expected_text))
    return diff


def _semantic_diff_from_text(kind: str, expected_text: str) -> dict[str, Any]:
    text = _sanitize_operation_intent(expected_text)
    typed_values = _extract_typed_values(text)
    if kind == "draft" and typed_values:
        return {"draft_values": typed_values}
    labels = _extract_quoted_values(text)
    if labels:
        return {f"{kind}_values": labels}
    return {"semantic_action": text or kind}


def _state_refs(step: dict[str, Any]) -> list[str]:
    refs = list(step.get("transaction", {}).get("state_commit", {}).get("state_refs", []) or [])
    for proj in step.get("transaction", {}).get("affected_render_projections", []) or []:
        reads = proj.get("reads_state")
        if reads:
            refs.append(reads)
        refs.extend(proj.get("reads_state_refs", []) or [])
    return _dedupe(refs)


def _operation_intent(step: dict[str, Any]) -> str:
    return _sanitize_operation_intent(step.get("transaction", {}).get("operation_intent") or step.get("operation_intent") or "")


def _sanitize_operation_intent(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("text"):
                return str(payload["text"]).strip()
            action = str(payload.get("action") or payload.get("type") or "").strip()
            if action:
                return action
    contamination_markers = (
        "】【step_index",
        "\\\"step_index\\\"",
        '"step_index"',
        "'step_index'",
        "\\\"thinking\\\"",
        '"thinking"',
        "\\\"action\\\"",
        '"action"',
    )
    for marker in contamination_markers:
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip("】},，, ")
            break
    return text


def _has_operation_intent_contamination(value: Any) -> bool:
    text = str(value or "")
    return any(
        marker in text
        for marker in (
            "】【step_index",
            "\\\"step_index\\\"",
            '"step_index"',
            "'step_index'",
            "\\\"thinking\\\"",
            '"thinking"',
            "\\\"action\\\"",
            '"action"',
        )
    )


def _normalize_expected_diff_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("text"):
                return payload["text"]
            action = payload.get("action") or payload.get("type")
            if action:
                return f"{action}_event"
    return _sanitize_operation_intent(text)


def _extract_typed_values(text: str) -> list[str]:
    values: list[str] = []
    for pattern in (r"输入[“\"]([^”\"]+)[”\"]", r"输入框输入[“\"]([^”\"]+)[”\"]", r"在搜索框输入[“\"]([^”\"]+)[”\"]"):
        values.extend(re.findall(pattern, text))
    return _dedupe(values)


def _extract_quoted_values(text: str) -> list[str]:
    return _dedupe(re.findall(r"[“\"]([^”\"]{1,80})[”\"]", text or ""))


def _is_terminal_side_effect_step(step: dict[str, Any]) -> bool:
    intent = _operation_intent(step)
    reducer = step.get("reducer") or step.get("transaction", {}).get("reducer", "")
    if reducer == "agent_execute":
        return False
    return any(token in intent for token in ("提交订单", "确认并提交订单", "立即支付", "确认支付", "呼叫司机", "确认下单"))


def _last_task_relevant_step(capture_entries: list[dict[str, Any]], task_steps: list[str]) -> int | None:
    for entry in reversed(capture_entries):
        idx = entry.get("step_index")
        desc = entry.get("description", "")
        if idx is not None and "结束任务" not in desc:
            return idx
    terminal = _terminal_step(task_steps)
    return terminal - 1 if terminal and terminal > 0 else terminal


def _terminal_step(task_steps: list[str]) -> int | None:
    for idx, step in enumerate(task_steps):
        if "结束任务" in step or "完成" in step and idx == len(task_steps) - 1:
            return idx
    return len(task_steps) - 1 if task_steps else None


def _allowed_final_state(policy: str) -> str:
    return {
        "precommit_ready": "ready_to_submit",
        "simulated_commit": "mock_commit_attempted_or_pending",
        "observed_terminal_state": "observed_terminal_state",
    }[policy]


def _forbidden_events(policy: str) -> list[str]:
    if policy == "precommit_ready":
        return ["submit_order", "payment_attempted", "order_completed", "real_payment_success"]
    if policy == "simulated_commit":
        return ["paid=true", "order_completed", "real_payment_success"]
    return ["real_external_side_effect"]


def _required_mock_state(policy: str) -> list[str]:
    if policy == "simulated_commit":
        return ["commit_attempted", "pending_payment_or_confirmation"]
    return []


def _terminal_observable_scope(policy: str, target_filename: str) -> dict[str, Any]:
    scope: dict[str, Any] = {"terminal_page": target_filename}
    if policy == "observed_terminal_state":
        scope["assertion_channel"] = "terminal_observable"
        scope["asserts"] = {"current_page": {"op": "eq", "value": target_filename}}
    elif policy == "simulated_commit":
        scope["assertion_channel"] = "terminal_observable"
        scope["asserts"] = {"current_page": {"op": "eq", "value": target_filename}}
    elif policy == "precommit_ready":
        scope["assertion_channel"] = "terminal_observable"
        scope["asserts"] = {"current_page": {"op": "eq", "value": target_filename}}
    return scope


def _policy_summary(trace_policies: dict[str, Any], policy_key: str) -> dict[str, int]:
    counts = {"precommit_ready": 0, "simulated_commit": 0, "observed_terminal_state": 0}
    for row in trace_policies.values():
        policy = row.get(policy_key)
        if policy in counts:
            counts[policy] += 1
    return counts


def _action_contract_summary(step: dict[str, Any]) -> dict[str, Any]:
    tx = step.get("transaction", {})
    nav = tx.get("navigation", {})
    return {
        "action_id": step.get("action_id"),
        "reducer": step.get("reducer"),
        "operation": step.get("operation"),
        "source_filename": step.get("source_filename") or nav.get("source_filename"),
        "target_filename": step.get("target_filename") or nav.get("target_filename"),
        "expected_state_diff": step.get("assertions", {}).get("expected_state_diff", {}),
    }


def _action_effect_contract_summary(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": action.get("action_id"),
        "reducer": action.get("reducer"),
        "operation": action.get("operation"),
        "source_filename": action.get("source_filename"),
        "target_filename": action.get("target_filename") or action.get("expected_target_filename"),
        "expected_state_diff": action.get("expected_state_diff", {}),
    }


def _runtime_bridge_status(runtime_manifest: dict[str, Any]) -> str:
    interfaces = runtime_manifest.get("interfaces", [])
    required = {"step(action_id)", "inspect_state()", "state_diff(before, after)"}
    if isinstance(interfaces, dict):
        iterable = interfaces.values()
    else:
        iterable = interfaces
    available = {item.get("signature") or item.get("name") for item in iterable if isinstance(item, dict)}
    if required <= available:
        return "manifest_declared_runtime_bridge"
    return "static_contract_only"


def _quality_gates(row: dict[str, Any]) -> list[str]:
    gates = ["source_steps_non_empty", "has_expected_state_or_diff", "runtime_payload_bound"]
    if row.get("global_runtime_state", {}).get("state_refs"):
        gates.append("global_state_ref_bound")
    if row["supervision"] == "outcome":
        gates.append("terminal_policy_bound")
    gates.append("not_duplicate_adjacent_state_field")
    return gates


def _validate_checkpoint_rows(
    rows: list[dict[str, Any]],
    policy: dict[str, Any],
    runtime_manifest: dict[str, Any],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    contract_gaps: list[dict[str, Any]] = []
    row_gates: dict[str, list[str]] = {}
    declared_refs = _declared_state_refs(runtime_manifest)

    for row in rows:
        checkpoint_id = row["checkpoint_id"]
        gates = _quality_gates(row)
        source_steps = row.get("source_steps") or []
        if not source_steps:
            failures.append(_failure(row, "source_steps_empty"))
        if source_steps and all(step is None or int(step) < 0 for step in source_steps):
            failures.append(_failure(row, "source_steps_invalid_or_summary_only"))

        expected_diff = row.get("global_runtime_state", {}).get("expected_state_diff", {}) or {}
        expected_state = row.get("expected_state", {}) or {}
        for failure in _runtime_page_value_failures(row):
            failures.append(failure)
        if not expected_diff and not expected_state:
            failures.append(_failure(row, "missing_expected_state_or_diff"))

        if row.get("supervision") == "outcome":
            if row.get("terminal_policy") != policy.get("verified_policy"):
                failures.append(_failure(row, "outcome_terminal_policy_mismatch"))
            if set(row.get("forbidden_events", [])) != set(policy.get("forbidden_terminal_events", [])):
                failures.append(_failure(row, "outcome_forbidden_events_mismatch"))
            if row.get("required_mock_state", []) != policy.get("required_mock_state", []):
                failures.append(_failure(row, "outcome_required_mock_state_mismatch"))

        if row.get("blocking") and row.get("supervision") == "process":
            state_refs = row.get("global_runtime_state", {}).get("state_refs", []) or []
            has_projection = bool(row.get("projection_checks"))
            route_only = bool(row.get("route_only_justification"))
            if not state_refs and not has_projection and not route_only:
                contract_gaps.append(_gap(row, "blocking_process_missing_global_state_ref"))
            if _requires_meaningful_state_diff(row) and not _has_meaningful_state_diff(row):
                failures.append(_failure(row, "missing_meaningful_state_diff", {
                    "checkpoint_kind": row.get("checkpoint_kind"),
                    "expected_state_fields": sorted(str(key) for key in (expected_state or {}).keys()),
                    "expected_diff_fields": sorted(str(key) for key in (expected_diff or {}).keys()),
                    "technical_fields": sorted(PAGE_RUNTIME_FIELD_NAMES | {"state_reads"}),
                }))
            if set(expected_diff.keys()) == {"operation_intent"}:
                failures.append(_failure(row, "unstructured_operation_intent_only_diff"))
            for value in [*expected_state.values(), *expected_diff.values()]:
                if _has_operation_intent_contamination(value):
                    failures.append(_failure(row, "operation_intent_json_contamination"))
            missing_refs = [ref for ref in state_refs if ref not in declared_refs and ref != "state::page_runtime"]
            if missing_refs:
                contract_gaps.append(_gap(row, "state_refs_not_declared_in_global_runtime_state", {"state_refs": missing_refs}))

        row_gates[checkpoint_id] = gates

    duplicate_fields = _adjacent_duplicate_state_fields(rows)
    for item in duplicate_fields:
        failures.append(item)

    return {"failures": failures, "contract_gaps": contract_gaps, "row_gates": row_gates}


def _post_projection_matrix_quality_failures(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in matrix.get("checkpoints", []) if isinstance(matrix, dict) else []:
        if isinstance(row, dict):
            failures.extend(_runtime_page_value_failures(row))
    return failures


def _runtime_page_value_failures(row: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    state = row.get("global_runtime_state", {}) or {}
    for bucket_name, bucket in (
        ("expected_state", row.get("expected_state", {}) or {}),
        ("expected_state_diff", state.get("expected_state_diff", {}) or {}),
        ("canonical_expected_state_diff", state.get("canonical_expected_state_diff", {}) or {}),
        ("downstream_expected_state_diff", state.get("downstream_expected_state_diff", {}) or {}),
    ):
        if not isinstance(bucket, dict):
            continue
        for field, raw_value in bucket.items():
            field_text = str(field or "").strip()
            payload_field = _expected_payload_field_name(field_text, raw_value)
            if not _is_page_runtime_field(field_text) and not _is_page_runtime_field(payload_field):
                continue
            value = _raw_expected_value(raw_value)
            if not _is_runtime_page_target_value(value):
                failures.append(_failure(row, "runtime_page_expected_value_not_page_target", {
                    "bucket": bucket_name,
                    "field": field_text,
                    "payload_field": payload_field,
                    "value": value,
                }))
    return failures


def _is_runtime_page_target_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    return bool(re.search(r"(?:^|[/#?&=])[^/#?&=]+\.html(?:$|[?#&])", text) or text.endswith(".html"))


def _apply_terminal_policy_to_outcome_rows(rows: list[dict[str, Any]], policy: dict[str, Any]) -> None:
    for row in rows:
        if row.get("supervision") != "outcome":
            continue
        row["terminal_policy"] = policy.get("verified_policy")
        row["forbidden_events"] = list(policy.get("forbidden_terminal_events", []) or [])
        row["required_mock_state"] = list(policy.get("required_mock_state", []) or [])
        row.setdefault("allowed_final_state", policy.get("allowed_final_state"))


def _dedupe_row_negative_probes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        if row.get("negative_probes"):
            row["negative_probes"] = _dedupe_probes(row["negative_probes"])
    return rows


def _absorb_adjacent_duplicate_process_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized: list[dict[str, Any]] = []
    absorbed: list[dict[str, Any]] = []
    previous_key: tuple[str, str] | None = None
    previous_row: dict[str, Any] | None = None
    for row in rows:
        key = _business_state_diff_key(row)
        if key and previous_key and key == previous_key:
            absorbed.append({
                "checkpoint_id": row.get("checkpoint_id"),
                "source_steps": row.get("source_steps") or [],
                "absorbed_into": (previous_row or {}).get("checkpoint_id"),
                "reason": "adjacent_duplicate_business_state_diff",
            })
            continue
        normalized.append(row)
        previous_key = key
        previous_row = row
    return normalized, absorbed


def _dedupe_probes(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        key_payload = {
            "probe_id": probe.get("probe_id"),
            "mutate": probe.get("mutate"),
            "events": probe.get("events") or probe.get("forbidden_events"),
            "must_fail": probe.get("must_fail"),
        }
        key = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(probe)
    return normalized


def _declared_state_refs(runtime_manifest: dict[str, Any]) -> set[str]:
    schema = runtime_manifest.get("state_store_schema", {}) or {}
    refs: set[str] = set()
    for key in ("declared_state_refs", "referenced_state_refs", "state_refs"):
        values = schema.get(key, []) if isinstance(schema, dict) else []
        if isinstance(values, dict):
            refs.update(str(k) for k in values.keys())
        elif isinstance(values, list):
            for item in values:
                if isinstance(item, str):
                    refs.add(item)
                elif isinstance(item, dict):
                    ref = item.get("state_ref") or item.get("ref") or item.get("name")
                    if ref:
                        refs.add(str(ref))
    refs.update(runtime_manifest.get("transactions_by_action_id", {}).keys())
    return refs


def _failure(row: dict[str, Any], code: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "trace_id": row.get("trace_id"),
        "checkpoint_id": row.get("checkpoint_id"),
        "severity": "hard_failure",
        "code": code,
        "details": details or {},
    }


def _gap(row: dict[str, Any], code: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    gap_type = {
        "blocking_process_missing_global_state_ref": "runtime_binding_gap",
        "state_refs_not_declared_in_global_runtime_state": "state_ref_missing_from_schema",
    }.get(code, "contract_gap")
    remediation = {
        "runtime_binding_gap": "bind checkpoint to appwise/global semantic state or mark it projection-only",
        "state_ref_missing_from_schema": "add canonical state ref to state_store_schema or normalize alias before instrumentation",
    }.get(gap_type, "inspect upstream contract compiler")
    return {
        "trace_id": row.get("trace_id"),
        "checkpoint_id": row.get("checkpoint_id"),
        "severity": "contract_gap",
        "code": code,
        "gap_type": gap_type,
        "supervision_strength": "weak_until_runtime_state_bound",
        "blocks_strong_reward": bool(row.get("blocking")),
        "remediation": remediation,
        "details": details or {},
    }


def _adjacent_duplicate_state_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    previous: tuple[str, str, tuple[str, ...]] | None = None
    previous_row: dict[str, Any] | None = None
    for row in rows:
        if row.get("supervision") == "outcome":
            continue
        if row.get("checkpoint_kind") == "route":
            previous = None
            previous_row = row
            continue
        state_refs = row.get("global_runtime_state", {}).get("state_refs", []) or []
        diff = row.get("global_runtime_state", {}).get("expected_state_diff", {}) or {}
        current = None
        if state_refs and diff:
            business_diff = {
                str(key): diff[key]
                for key in diff.keys()
                if not _is_technical_runtime_diff_field(str(key))
            }
            if business_diff:
                current = (
                    state_refs[0],
                    json.dumps(business_diff, ensure_ascii=False, sort_keys=True),
                    tuple(sorted(business_diff.keys())),
                )
        if current and previous and current[0] == previous[0] and current[1] == previous[1]:
            failures.append(_failure(row, "duplicate_adjacent_state_field", {
                "previous_checkpoint_id": (previous_row or {}).get("checkpoint_id"),
                "state_ref": current[0],
                "field": ",".join(current[2]),
            }))
        previous = current
        previous_row = row
    return failures


def _business_state_diff_key(row: dict[str, Any]) -> tuple[str, str] | None:
    if row.get("supervision") == "outcome":
        return None
    if row.get("checkpoint_kind") == "route":
        return None
    state_refs = row.get("global_runtime_state", {}).get("state_refs", []) or []
    diff = row.get("global_runtime_state", {}).get("expected_state_diff", {}) or {}
    if not state_refs or not diff:
        return None
    business_diff = {
        str(key): diff[key]
        for key in diff.keys()
        if not _is_technical_runtime_diff_field(str(key))
    }
    if not business_diff:
        return None
    return state_refs[0], json.dumps(business_diff, ensure_ascii=False, sort_keys=True)


def _requires_meaningful_state_diff(row: dict[str, Any]) -> bool:
    if row.get("supervision") != "process" or not row.get("blocking"):
        return False
    if row.get("route_only_justification"):
        return False
    return str(row.get("checkpoint_kind") or "") in {"mutation", "selection", "collection"}


def _has_meaningful_state_diff(row: dict[str, Any]) -> bool:
    state = row.get("global_runtime_state", {}) or {}
    field_names: list[str] = []
    for bucket in (
        row.get("expected_state", {}) or {},
        state.get("expected_state_diff", {}) or {},
        state.get("canonical_expected_state_diff", {}) or {},
        state.get("downstream_expected_state_diff", {}) or {},
    ):
        if not isinstance(bucket, dict):
            continue
        for key, value in bucket.items():
            field_names.append(str(key))
            if isinstance(value, dict):
                field_names.append(str(value.get("field") or ""))
    return any(
        field and not _is_technical_runtime_diff_field(field)
        for field in field_names
    )


def _is_technical_runtime_diff_field(field: str) -> bool:
    text = str(field or "").strip()
    if not text:
        return True
    if _is_page_runtime_field(text):
        return True
    return text in {"state_reads"}


def _default_payload_fields(row: dict[str, Any]) -> list[str]:
    return ["trace_id", "checkpoint_id", "source_steps", *list(row.get("expected_state", {}).keys())[:4]]


def _expected_change_refs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for row in rows:
        state = row.get("global_runtime_state", {}) or {}
        legacy_refs = list(state.get("legacy_state_refs") or state.get("state_refs") or [])
        canonical_refs = list(state.get("canonical_state_refs") or [])
        downstream_refs = list(state.get("downstream_extension_refs") or [])
        legacy_diff = state.get("expected_state_diff", {}) or {}
        canonical_diff = state.get("canonical_expected_state_diff", {}) or {}
        downstream_diff = state.get("downstream_expected_state_diff", {}) or {}
        if legacy_refs or canonical_refs or downstream_refs or legacy_diff or canonical_diff or downstream_diff:
            refs.append({
                "checkpoint_id": row.get("checkpoint_id"),
                "state_refs": canonical_refs or downstream_refs or legacy_refs,
                "legacy_state_refs": legacy_refs,
                "canonical_state_refs": canonical_refs,
                "downstream_extension_refs": downstream_refs,
                "fields": sorted(
                    str(key)
                    for diff in (canonical_diff, downstream_diff, legacy_diff)
                    for key in diff.keys()
                ),
                "supervision": row.get("supervision"),
            })
        for projection in row.get("projection_checks", []) or []:
            refs.append({
                "checkpoint_id": row.get("checkpoint_id"),
                "projection": projection,
                "supervision": row.get("supervision"),
            })
    return refs


def _negative_conditions(rows: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    conditions = []
    for row in rows:
        for probe in row.get("negative_probes", []):
            conditions.append(probe)
    if policy.get("forbidden_terminal_events"):
        conditions.append({"probe_id": "forbidden_terminal_events", "events": policy["forbidden_terminal_events"]})
    return _dedupe_probes(conditions)


def _outcome_id(policy: str) -> str:
    return {
        "precommit_ready": "outcome_ready_to_submit",
        "simulated_commit": "outcome_mock_commit_attempted",
        "observed_terminal_state": "outcome_observed_terminal_state",
    }[policy]


def _step_index(action_id: str) -> int:
    match = re.search(r":step(\d+)$", action_id or "")
    return int(match.group(1)) if match else -1


def _dedupe(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _validate_required_artifacts(output: Path) -> None:
    missing = [rel for rel in _required_source_artifacts(output) if not (output / rel).exists()]
    if missing:
        raise FileNotFoundError(f"missing required artifacts in {output}: {missing}")


def _required_source_artifacts(output: Path) -> list[str]:
    required = list(REQUIRED_SOURCE_ARTIFACTS)
    state_kernel_markers = [
        output / "phase_sdk_generation/runtime/backend/service_manifest.json",
        output
        / "phase_page_environment/semantic_api/08_spec_compiled_execution_views/evaluator_state_view.json",
    ]
    if any(path.is_file() for path in state_kernel_markers):
        required.extend(STATE_KERNEL_REQUIRED_SOURCE_ARTIFACTS)
    return required


def _clear_generated_artifacts(output: Path) -> None:
    for rel in [
        "phase_post_processing/terminal_policy",
        "phase_post_processing/instrumentation",
        "phase_post_processing/evaluators",
        "phase_post_processing/test_logic",
        "phase_sdk_generation/validation/test_flows",
    ]:
        path = output / rel
        if path.exists():
            shutil.rmtree(path)
    generated_code = output / "phase_sdk_generation/runtime/instrumented_business_logic.js"
    if generated_code.exists():
        generated_code.unlink()


def _switch_page_runtime_sdk(output: Path, runtime_filename: str) -> None:
    pages_dir = output / "phase_pagegen" / "pages"
    if not pages_dir.is_dir():
        return
    replacements = {
        'src="instrumented_business_logic.js"': f'src="{runtime_filename}"',
        "src='instrumented_business_logic.js'": f"src='{runtime_filename}'",
        'src="../../phase_sdk_generation/runtime/instrumented_business_logic.js"': (
            f'src="../../phase_sdk_generation/runtime/{runtime_filename}"'
        ),
        "src='../../phase_sdk_generation/runtime/instrumented_business_logic.js'": (
            f"src='../../phase_sdk_generation/runtime/{runtime_filename}'"
        ),
        '../../phase_sdk_generation/runtime/instrumented_business_logic.js': (
            f'../../phase_sdk_generation/runtime/{runtime_filename}'
        ),
        'src="business_logic.js"': f'src="{runtime_filename}"',
        "src='business_logic.js'": f"src='{runtime_filename}'",
        'src="../../phase_sdk_generation/runtime/business_logic.js"': (
            f'src="../../phase_sdk_generation/runtime/{runtime_filename}"'
        ),
        "src='../../phase_sdk_generation/runtime/business_logic.js'": (
            f"src='../../phase_sdk_generation/runtime/{runtime_filename}'"
        ),
        '../../phase_sdk_generation/runtime/business_logic.js': (
            f'../../phase_sdk_generation/runtime/{runtime_filename}'
        ),
    }
    for html_path in pages_dir.glob("*.html"):
        content = html_path.read_text(encoding="utf-8")
        updated = content
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != content:
            html_path.write_text(updated, encoding="utf-8")


def _write_playwright_flow_files(output: Path, test_logic: dict[str, Any]) -> None:
    flow_dir = output / "phase_post_processing/test_logic/playwright_flows"
    flow_dir.mkdir(parents=True, exist_ok=True)
    for trace_id, flow in test_logic["trace_flows"].items():
        _write_json(flow_dir / f"{trace_id}.json", flow)


def _compile_test_flows_js(test_logic: dict[str, Any]) -> str:
    payload = json.dumps(test_logic, ensure_ascii=False, indent=2)
    return (
        '"use strict";\n'
        "// Generated static test-flow contract. Playwright runner consumes the JSON artifact.\n"
        f"window.GhostwebGeneratedTestFlows = {payload};\n"
        "if (typeof module !== 'undefined' && module.exports) module.exports = window.GhostwebGeneratedTestFlows;\n"
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _load_optional_json(path: Path) -> Any:
    if not path.is_file():
        return {}
    return _load_json(path)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def _stable_payload_hash(payload: Any) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
