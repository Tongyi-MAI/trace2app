
import os
import sys
import json
import re
import time
import shutil
import asyncio
import hashlib
import subprocess
import socket
from dataclasses import asdict
from html import escape as _html_escape
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Dict, Any, Mapping, Optional, Tuple

from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.data_manager import Trace2WorldDataManager, Trace2WorldGenerationResult
from trace2world_pipeline.core.config_manager import Trace2WorldConfigManager, resolve_page_environment_planning_mode
from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.core.post_processing_paths import first_existing, task_file_candidates
from trace2world_pipeline.core.step_keys import normalize_step_keyed_mapping
from trace2world_pipeline.core.artifact_reuse import ArtifactReuseDecision, validate_no_fallback_binding_source
from trace2world_pipeline.app_spec.trace_loader import TraceLoader
from trace2world_pipeline.app_spec.model import AppSpec
from trace2world_pipeline.app_spec.environment_profile import (
    build_render_context,
    device_context_mirror,
    normalize_environment_profile,
    resolve_environment_profile,
)
from trace2world_pipeline.app_spec.environment_prompt_policy import (
    POLICY_REVISION,
    prompt_policy_manifest,
)
from trace2world_pipeline.app_spec.reconcile.core import (
    AppSpecBuilder,
    GlobalReasoningStopAfterReached,
    InstanceSplittingStopAfterReached,
    SelectedInstanceCompileStopAfterReached,
)
from trace2world_pipeline.app_spec.entity_seed import entity_seed_to_init_data, materialize_entity_seed
from trace2world_pipeline.app_spec.artifact_index import rebuild_artifact_index, register_artifact
from trace2world_pipeline.app_spec.artifact_migration import (
    assert_output_artifacts_registered,
)
from trace2world_pipeline.app_spec.schema_pageenv_projection import project_global_schema_into_page_environment_specs
from trace2world_pipeline.app_spec.schema_generation_context import build_schema_generation_context
from trace2world_pipeline.app_spec.schema_obligations import (
    canonicalize_runtime_state_obligations_payload,
    collect_schema_obligations,
)


from trace2world_pipeline.task_generation.task_generator import Trace2WorldTaskGenerator
from trace2world_pipeline.task_generation.interface_designer import Trace2WorldInterfaceDesigner
from trace2world_pipeline.runtime_backend.sdk_generator import Trace2WorldSdkGenerator
from trace2world_pipeline.runtime_backend.runtime_contract import build_runtime_contract
from trace2world_pipeline.page_realizer.html_generator import Trace2WorldHtmlGenerator
from trace2world_pipeline.page_realizer.pagegen_input_budget import normalize_pagegen_input_token_budget
from trace2world_pipeline.task_generation.schema_enricher import SchemaEnricher
from trace2world_pipeline.resources.resource_replacer import Trace2WorldResourceReplacer
from trace2world_pipeline.verification.post_processor import Trace2WorldInstrumentationPostProcessor
from trace2world_pipeline.core.token_tracker import get_token_tracker
from trace2world_pipeline.core.llm_caller import (
    configure_load_balancing,
    configure_global_concurrency,
    configure_model_concurrency,
    configure_stage_model_routing,
    configure_api_log,
)
from trace2world_pipeline.orchestration.run_context import LogicalArtifactDecision, RunContext

GLOBAL_REASONING_PAGE_ASSIGNMENT_AUDITED_FILENAME = "global_reasoning_page_assignment_audited.json"
INSTANCE_SPLITTING_PAGE_SPLIT_AUDIT_FILENAME = "reference_page_split_audit_after_instance_splitting.json"
INSTANCE_SPLITTING_PAGE_SPLIT_AUDIT_MD_FILENAME = "reference_page_split_audit_after_instance_splitting.md"


def load_completed_semantic_design_for_downstream_resume(
    output_dir: str | Path,
    *,
    require_environment_state_artifacts: bool = False,
) -> Dict[str, Any]:
    root = Path(output_dir)
    semantic_design_path = root / "phase_consolidation" / "site_model" / "semantic_design.json"
    interfaces_path = root / "phase_consolidation" / "site_model" / "interfaces.json"
    compiled_dir = root / "phase_page_environment" / "semantic_api" / "08_spec_compiled_execution_views"
    required_compiled = (
        "action_effect_contracts", "action_effect_downstream_schema_projection",
        "contract_replay_plan", "dom_binding_contracts", "domain_contracts",
        "effect_contract_validation", "pagegen_execution_view_by_page",
        "pagegen_spec_view_by_page", "route_execution_contracts",
        "runtime_binding_manifest", "runtime_transaction_contracts",
        "trace_coverage_closure_report", "typed_action_contracts",
    )
    state_kernel_compiled = (
        "storage_plan", "runtime_state_registry", "transaction_manifest",
        "seed_manifest", "evaluator_state_view", "state_api_contract",
    )
    missing = [
        str(path)
        for path in (semantic_design_path, interfaces_path, compiled_dir)
        if not path.exists()
    ]
    missing.extend(
        str(compiled_dir / f"{name}.json")
        for name in required_compiled
        if not (compiled_dir / f"{name}.json").is_file()
    )
    state_kernel_paths = {
        name: compiled_dir / f"{name}.json"
        for name in state_kernel_compiled
    }
    present_state_kernel = {
        name for name, path in state_kernel_paths.items() if path.is_file()
    }
    if require_environment_state_artifacts or present_state_kernel:
        missing.extend(
            str(path)
            for name, path in state_kernel_paths.items()
            if name not in present_state_kernel
        )
    if missing:
        raise RuntimeError(
            "Completed semantic-design downstream resume is missing canonical artifacts: "
            + ", ".join(missing)
        )
    payload = json.loads(semantic_design_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError(f"Invalid completed semantic design: {semantic_design_path}")
    compiled_views = payload.get("spec_compiled_execution_views")
    if not isinstance(compiled_views, dict):
        raise RuntimeError(
            "Completed semantic design has no spec_compiled_execution_views: "
            f"{semantic_design_path}"
        )
    for name in required_compiled:
        compiled_views[name] = json.loads(
            (compiled_dir / f"{name}.json").read_text(encoding="utf-8")
        )
    compiled_views["environment_state_artifacts"] = {
        name: json.loads(state_kernel_paths[name].read_text(encoding="utf-8"))
        for name in state_kernel_compiled
        if name in present_state_kernel
    }
    payload["spec_compiled_execution_views"] = compiled_views
    payload["downstream_resume"] = {
        "mode": "completed_semantic_design",
        "environment_state_artifact_policy": (
            "required"
            if require_environment_state_artifacts
            else "optional_complete_bundle"
        ),
        "preserved_phases": [
            "phase_global_reasoning", "phase_instance_splitting",
            "phase_page_environment/semantic_api/06_page_environment_specs",
            "phase_page_environment/semantic_api/07_spec_executability_eval",
        ],
        "first_added_contracts": (
            "phase_page_environment/semantic_api/08_spec_compiled_execution_views/"
            "{storage_plan,runtime_state_registry,transaction_manifest,seed_manifest,"
            "evaluator_state_view,state_api_contract}.json"
        ),
    }
    return payload


def inject_state_delta_spec_contracts_from_path(
    page_environment_specs: Mapping[str, Any],
    state_delta_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    if not state_delta_path.is_file():
        return dict(page_environment_specs), {}, {}
    try:
        with open(state_delta_path, encoding="utf-8") as f:
            state_delta_signatures = json.load(f)
        from trace2world_pipeline.app_spec.spec_deterministic_normalizer import (
            apply_state_delta_signatures_to_page_environment_specs,
        )

        updated_specs, report = apply_state_delta_signatures_to_page_environment_specs(
            page_environment_specs,
            state_delta_signatures,
        )
    except Exception as exc:
        raise RuntimeError(
            f"State-delta spec contract injection failed for formal artifact {state_delta_path}: {exc}"
        ) from exc
    return updated_specs, report, state_delta_signatures


def _merged_component_config(config_manager: Any, component_name: str) -> Dict[str, Any]:
    component_config = dict(config_manager.get_component_config(component_name) or {})
    nested_config = config_manager.get(component_name, {})
    if isinstance(nested_config, dict):
        component_config.update(nested_config)
    return component_config


def _instrumentation_artifacts_only_config(instrumentation_config: Mapping[str, Any]) -> bool:
    scope = (
        instrumentation_config.get("output_scope")
        or instrumentation_config.get("artifact_scope")
    )
    return str(scope or "").strip().lower() in {
        "instrumentation",
        "instrumentation_only",
        "instrumentation_artifacts",
        "instrumentation_artifacts_only",
    }


def _instrumentation_generate_test_logic(config_manager: Any) -> bool:
    instrumentation_config = _merged_component_config(config_manager, "instrumentation")
    if _instrumentation_artifacts_only_config(instrumentation_config):
        return False
    default = not bool(config_manager.get("skip_instrumentation", False))
    return bool(instrumentation_config.get("generate_test_logic", default))


_INSTRUMENTATION_GENERATION_RESULT_KEYS = (
    "mode",
    "generated_at",
    "terminal_policy_artifact",
    "checkpoint_matrix_artifact",
    "runtime_state_schema_artifact",
    "checkpoint_matrix_validation_report",
    "runtime_event_bindings_artifact",
    "evaluators_artifact",
    "test_logic_artifact",
    "source_env_provenance_artifact",
    "terminal_policy_summary",
    "checkpoint_matrix_summary",
    "evaluators_count",
    "test_logic_summary",
    "optional_artifacts",
    "runtime_bridge_status",
    "source_env_hash_count",
)


def validate_pagegen_environment_profile_resume(
    output_dir: Path | str,
    environment_profile: Dict[str, Any],
) -> Dict[str, Any]:
    root = Path(output_dir)
    marker_path = root / "run_management/pagegen_environment_profile.json"
    pages_dir = root / "phase_pagegen/pages"
    existing_pages = sorted(pages_dir.glob("*.html")) if pages_dir.is_dir() else []
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker_hash = str(marker.get("environment_profile_hash") or "")
        if marker_hash and marker_hash != environment_profile["profile_hash"]:
            raise RuntimeError(
                "Existing PageGen artifacts were produced for a different environment profile. "
                f"cached={marker_hash} resolved={environment_profile['profile_hash']}. "
                "Move the stale output to deprecated or use a fresh output_dir before rerunning."
            )
    elif environment_profile["platform"] == "desktop" and existing_pages:
        raise RuntimeError(
            "Existing PageGen HTML has no environment profile provenance and cannot be reused "
            "for desktop. Use a fresh output_dir or deprecate the stale pages."
        )
    return {
        "marker_path": str(marker_path),
        "marker_present": marker_path.is_file(),
        "existing_page_count": len(existing_pages),
        "environment_profile_hash": environment_profile["profile_hash"],
        "platform": environment_profile["platform"],
        "legacy_mobile_reuse_allowed": (
            environment_profile["platform"] == "mobile" and not marker_path.is_file()
        ),
    }


def _merge_instrumentation_generation_result_metadata(
    output_dir: str,
    contract_generation_result: Mapping[str, Any] | None,
) -> bool:
    if not contract_generation_result:
        return False
    result_path = (
        Path(output_dir)
        / "phase_post_processing"
        / "debug"
        / "data_manager"
        / "generation_result.json"
    )
    if not result_path.is_file():
        return False
    try:
        with result_path.open(encoding="utf-8") as f:
            final_result = json.load(f)
    except Exception:
        return False
    if not isinstance(final_result, dict):
        return False
    changed = False
    for key in _INSTRUMENTATION_GENERATION_RESULT_KEYS:
        if (
            key in contract_generation_result
            and (key not in final_result or final_result.get(key) != contract_generation_result.get(key))
        ):
            final_result[key] = contract_generation_result.get(key)
            changed = True
    metadata = final_result.get("metadata") if isinstance(final_result.get("metadata"), dict) else {}
    mode = str(contract_generation_result.get("mode") or "").strip()
    if mode and metadata.get("instrumentation_artifact_mode") != mode:
        metadata["instrumentation_artifact_mode"] = mode
        final_result["metadata"] = metadata
        changed = True
    if changed:
        with result_path.open("w", encoding="utf-8") as f:
            json.dump(final_result, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return changed


async def _run_pagegen_resource_streaming_jobs(
    items: List[Any],
    process_item: Callable[[Any], Awaitable[Any]],
) -> List[Any]:
    tasks = [asyncio.create_task(process_item(item)) for item in items]
    results: List[Any] = []
    try:
        for task in asyncio.as_completed(tasks):
            results.append(await task)
    except Exception:
        for task in tasks:
            if not task.done():
                task.cancel()
        raise
    return results


def _parse_static_resource_pipeline_stdout(stdout: str) -> Dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        return {}
    for match in reversed(list(re.finditer(r"(?m)^\s*\{", text))):
        candidate = text[match.start():].strip()
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {"stdout_tail": text[-4000:]}


def _static_resource_pipeline_log_lines(result: Dict[str, Any]) -> List[str]:
    if not isinstance(result, dict) or not result:
        return []
    trace_results = result.get("trace_results", [])
    if not isinstance(trace_results, list):
        trace_results = []
    trace_count = int(result.get("trace_count") or len(trace_results) or 0)
    existing_or_reused = int(
        result.get("existing_or_reused_count")
        or sum(
            1
            for item in trace_results
            if isinstance(item, dict) and item.get("reused_or_existing") is True
        )
        or 0
    )
    error_count = int(
        result.get("error_count")
        or sum(1 for item in trace_results if isinstance(item, dict) and item.get("error"))
        or 0
    )
    newly_extracted = int(
        result.get("newly_extracted_count")
        or sum(
            1
            for item in trace_results
            if isinstance(item, dict)
            and item.get("reused_or_existing") is not True
            and not item.get("error")
        )
        or 0
    )
    resource_count = int(
        result.get("total_resources")
        or sum(
            int(item.get("resources") or 0)
            for item in trace_results
            if isinstance(item, dict)
        )
        or 0
    )
    final_count = int(result.get("final_count") or 0)
    reused_count = int(result.get("reused_count") or 0)
    scope_summary = result.get("screenshot_scope_summary") or {}
    if not isinstance(scope_summary, dict):
        scope_summary = {}
    selected_screenshots = int(scope_summary.get("selected_screenshot_count") or 0)
    scoped_traces = int(scope_summary.get("trace_count") or 0)

    lines = [
        "Static resource pipeline summary: "
        f"traces={trace_count}, existing_or_reused={existing_or_reused}, "
        f"newly_extracted={newly_extracted}, reuse_root_copies={reused_count}, "
        f"errors={error_count}, resources={resource_count}, final_resources={final_count}"
    ]
    if selected_screenshots or scoped_traces:
        lines.append(
            "Static resource pipeline L0 scope: "
            f"selected_screenshots={selected_screenshots}, scoped_traces={scoped_traces}"
        )
    if trace_count and newly_extracted == 0 and error_count == 0:
        lines.append(
            "Static resource pipeline reused existing scoped e2e_results; "
            "no new MinerU extraction jobs were needed"
        )
    elif newly_extracted:
        lines.append(f"Static resource pipeline submitted MinerU extraction for {newly_extracted} trace(s)")
    return lines


def _build_schema_context_trace_payloads(reference_traces: Iterable[Any]) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for trace in reference_traces or []:
        trace_id = str(getattr(trace, "trace_id", "") or "").strip()
        steps_payload: List[Dict[str, Any]] = []
        for idx, step in enumerate(getattr(trace, "steps", []) or []):
            steps_payload.append({
                "step": getattr(step, "step_index", idx),
                "action": str(getattr(step, "action", "") or "")[:240],
                "observation": str(getattr(step, "observation", "") or "")[:240],
            })
        payloads.append({
            "trace_id": trace_id or f"trace_{len(payloads)}",
            "task": str(getattr(trace, "task", "") or "")[:800],
            "level": str(getattr(trace, "level", "") or ""),
            "steps": steps_payload,
        })
    return payloads


def _load_screenshot_render_roles(output_dir: str) -> Dict[str, str]:
    candidates = [
        PhaseOutputManager(output_dir).formal(
            "l0_fidelity",
            "screenshot_render_classification.json",
        ),
        Path(output_dir) / "phase_l0_fidelity" / "screenshot_render_classification.json",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        roles = payload.get("render_role_by_step") if isinstance(payload, dict) else {}
        lifecycle_roles = payload.get("lifecycle_role_by_step") if isinstance(payload, dict) else {}
        if isinstance(roles, dict):
            normalized_roles = normalize_step_keyed_mapping(roles)
            normalized_lifecycle = normalize_step_keyed_mapping(lifecycle_roles)
            return {
                str(step_key): str(
                    normalized_lifecycle.get(step_key)
                    if normalized_lifecycle.get(step_key) in {
                        "runtime_pre_action_state",
                        "interaction_state_change",
                        "post_interaction_overlay",
                    }
                    else render_role
                )
                for step_key, render_role in normalized_roles.items()
                if str(step_key or "").strip() and str(render_role or "").strip()
            }
    return {}


def _phase_pagegen_sprites_dir(output_dir: str, filename: str) -> str:
    return str(PhaseOutputManager(output_dir).formal("pagegen", "sprites", filename.replace(".html", "")))


def _assert_no_formal_fallback_binding_source(artifact_name: str, payload: Dict[str, Any]) -> None:
    issues = validate_no_fallback_binding_source(payload)
    if issues:
        first = issues[0]
        raise RuntimeError(
            f"{artifact_name} contains fallback binding source in formal artifact: "
            f"{first.get('value')} at {first.get('path')}"
        )


def _write_consolidation_recompute_decision(
    output_dir: str,
    *,
    subphase: str,
    artifact_id: str,
    artifact_rel_path: str,
    input_artifacts: Optional[List[str]] = None,
) -> None:
    decision = ArtifactReuseDecision(
        artifact_id=artifact_id,
        artifact_path=artifact_rel_path,
        reused=False,
        reason="recomputed",
        input_artifacts=input_artifacts or [],
    )
    PhaseOutputManager(output_dir).write_json(
        "consolidation",
        "debug",
        f"{subphase}/reuse_decision.json",
        decision.to_json(),
        artifact_id=f"consolidation.{subphase}.reuse_decision",
        artifact_kind="debug_export",
        producer="trace_generator._write_consolidation_recompute_decision",
    )


def _write_consolidation_phase_manifest(
    output_dir: str,
    *,
    required_rel_paths: List[str],
    blocking_issues: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    result_dir = Path(output_dir)
    issues = list(blocking_issues or [])
    artifacts: List[Dict[str, Any]] = []
    for rel_path in required_rel_paths:
        path = result_dir / rel_path
        exists = path.exists()
        artifacts.append({"path": rel_path, "exists": exists})
        if not exists:
            issues.append(
                {
                    "code": "missing_required_artifact",
                    "path": rel_path,
                }
            )
    manifest = {
        "format": "phase_manifest",
        "schema_revision": 1,
        "phase": "consolidation",
        "status": "complete" if not issues else "blocked",
        "required_artifact_count": len(artifacts),
        "required_artifacts": artifacts,
        "blocking_issue_count": len(issues),
        "blocking_issues": issues,
    }
    PhaseOutputManager(output_dir).write_json(
        "consolidation",
        "formal",
        "manifest.json",
        manifest,
        artifact_id="consolidation.manifest",
        artifact_kind="validation_report",
        producer="trace_generator._write_consolidation_phase_manifest",
    )
    return manifest


def _write_pagegen_resource_projection_artifact(
    *,
    output_dir: str,
    filename: str,
    page_scope: Dict[str, Any],
    resources: List[Dict[str, Any]],
    semantic_cache: Optional[Dict[str, Any]] = None,
    producer: str,
) -> tuple[str, Dict[str, Any]]:
    from trace2world_pipeline.page_realizer.pagegen_resource_projection import build_pagegen_resource_projection

    projection_dir = PhaseOutputManager(output_dir).formal("pagegen", "projections", "pagegen_resources")
    projection_dir.mkdir(parents=True, exist_ok=True)
    slug = filename.replace(".html", "")
    projection = build_pagegen_resource_projection(
        page_id=filename,
        prompt_screenshot_refs=(
            page_scope.get("prompt_screenshot_refs")
            or page_scope.get("page_screenshot_refs")
            or []
        ),
        resource_screenshot_refs=(
            page_scope.get("resource_screenshot_refs")
            or page_scope.get("prompt_screenshot_refs")
            or page_scope.get("page_screenshot_refs")
            or []
        ),
        screenshot_render_roles=page_scope.get("screenshot_render_roles", {}),
        resources=resources or [],
        semantic_cache=semantic_cache or {},
    )
    projection_path = projection_dir / f"{slug}.json"
    projection_path.write_text(
        json.dumps(projection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    register_artifact(
        Path(output_dir),
        projection_path,
        artifact_id=f"projections.pagegen.resources.{filename}",
        phase="pagegen_input_assembly",
        artifact_kind="det_projection",
        api_or_det="det",
        canonical_source=False,
        logical_owner="frontend.pagegen_resource_projection",
        producer=producer,
        allowed_consumers=("pagegen", "debug_visualization"),
        semantic_role="pagegen_resource_projection",
        debug_only=False,
        canonical_semantic_source=False,
        canonical_prompt_boundary=True,
        artifact_role="projection",
        source_selection={"derived_from": "page_wise_resource_semantic_labeling_and_prompt_scope"},
        notes="Canonical PageGen resource projection; debug/cache keys are excluded.",
        refresh_filesystem_summary=False,
    )
    return str(projection_path), projection


def _resource_list_text_from_projection(projection: Dict[str, Any]) -> str:
    from trace2world_pipeline.page_realizer.pagegen_resource_projection import format_pagegen_resource_list_from_projection

    selection_policy = projection.get("selection_policy") if isinstance(projection, dict) else {}
    has_visible_resources = bool(projection.get("resources")) if isinstance(projection, dict) else False
    has_omitted_resources = (
        isinstance(selection_policy, dict)
        and bool(selection_policy.get("omitted_resource_count"))
    )
    if not has_visible_resources and not has_omitted_resources:
        return ""
    return format_pagegen_resource_list_from_projection(projection, scope="page")


def _write_resource_list_from_projection_artifact(
    resource_list_path: str,
    projection: Dict[str, Any],
) -> str:
    resource_text = _resource_list_text_from_projection(projection)
    os.makedirs(os.path.dirname(resource_list_path), exist_ok=True)
    with open(resource_list_path, "w", encoding="utf-8") as f:
        f.write(resource_text)
    return resource_text


class _StopAfterReached(Exception):
    pass


def _resource_cache_key(page_resources: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for resource in page_resources:
        result.append({
            "asset_name": str(resource.get("asset_name") or ""),
            "source_step_key": str(resource.get("source_step_key") or ""),
            "path": str(resource.get("path") or ""),
        })
    return result


def _resource_sprite_cache_is_valid(
    cache_path: str,
    manifest_path: str,
    expected_dedup_metadata: Dict[str, Any],
    expected_cache_key: List[Dict[str, str]],
    expected_resource_text_version: str = "",
) -> bool:
    if not os.path.exists(cache_path) or not os.path.exists(manifest_path):
        return False
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception:
        return False
    return (
        manifest.get("resource_dedup") == expected_dedup_metadata
        and manifest.get("resource_cache_key") == expected_cache_key
        and (not expected_resource_text_version or manifest.get("resource_text_version") == expected_resource_text_version)
        and "dedup_resource_count" in manifest
    )


def _resource_semantic_labeling_path(sprite_dir: str) -> str:
    return os.path.join(sprite_dir, "resource_semantic_labeling.json")


def _resource_semantic_labeling_status_path(sprite_dir: str) -> str:
    return os.path.join(sprite_dir, "resource_semantic_labeling_status.json")


def _file_sha256_or_empty(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return ""


def _resource_semantic_labeling_reuse_key(
    page_scope: Dict[str, Any],
    *,
    sprite_paths: List[str] | None = None,
    model: str | None = None,
) -> Dict[str, Any]:
    sprite_entries: List[Dict[str, str]] = []
    for path in sorted(str(item) for item in (sprite_paths or []) if item):
        sprite_entries.append({
            "path": os.path.basename(path),
            "sha256": _file_sha256_or_empty(path),
        })
    return {
        "schema_revision": 1,
        "resource_text_version": page_scope.get("resource_text_version", ""),
        "pagegen_execution_flow": page_scope.get("pagegen_execution_flow", ""),
        "dedup_resource_cache_key": page_scope.get("dedup_resource_cache_key", []),
        "sprite_sheets": sprite_entries,
        "model": str(model or ""),
    }


def _semantic_labeling_artifact_is_valid(
    payload: Dict[str, Any],
    *,
    expected_reuse_key: Dict[str, Any],
) -> bool:
    dedup_page_resources = payload.get("dedup_page_resources")
    return (
        isinstance(payload, dict)
        and payload.get("format") == "pagegen_resource_semantic_labeling"
        and payload.get("schema_revision") == 1
        and payload.get("reuse_key") == expected_reuse_key
        and isinstance(dedup_page_resources, list)
    )


def _load_resource_semantic_labeling_artifact(
    artifact_path: str,
    *,
    expected_reuse_key: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not artifact_path or not os.path.exists(artifact_path):
        return None
    try:
        with open(artifact_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    if _semantic_labeling_artifact_is_valid(payload, expected_reuse_key=expected_reuse_key):
        return payload
    return None


def _write_resource_semantic_labeling_artifact(
    artifact_path: str,
    *,
    filename: str,
    page_scope: Dict[str, Any],
    labeled_resources: List[Dict[str, Any]],
    semantic_labeling_report: Dict[str, Any],
    reuse_key: Dict[str, Any],
    resource_list_text: Optional[str] = None,
) -> None:
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    payload = {
        "format": "pagegen_resource_semantic_labeling",
        "schema_revision": 1,
        "filename": filename,
        "reuse_key": reuse_key,
        "resource_text_version": page_scope.get("resource_text_version", ""),
        "pagegen_execution_flow": page_scope.get("pagegen_execution_flow", ""),
        "resource_dedup": page_scope.get("resource_dedup", {}),
        "dedup_resource_cache_key": page_scope.get("dedup_resource_cache_key", []),
        "dedup_resource_count": len(labeled_resources),
        "resource_semantic_labeling": semantic_labeling_report,
        "dedup_page_resources": labeled_resources,
        "artifact_metadata": {
            "artifact_kind": "det_projection",
            "api_or_det": "mixed",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "artifact_role": "formal_artifact",
            "semantic_role": "pagegen_resource_semantic_labeling",
        },
    }
    if resource_list_text is not None:
        payload["resource_list_text"] = resource_list_text
    with open(artifact_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _write_resource_semantic_labeling_status(
    status_path: str,
    *,
    filename: str,
    page_scope: Dict[str, Any],
    semantic_labeling_report: Dict[str, Any],
    reason: str,
    label_artifact_expected: bool = False,
) -> None:
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    payload = {
        "format": "pagegen_resource_semantic_labeling_status",
        "schema_revision": 1,
        "filename": filename,
        "status": "skipped",
        "reason": reason,
        "label_artifact_expected": bool(label_artifact_expected),
        "resource_text_version": page_scope.get("resource_text_version", ""),
        "pagegen_execution_flow": page_scope.get("pagegen_execution_flow", ""),
        "resource_dedup": page_scope.get("resource_dedup", {}),
        "dedup_resource_cache_key": page_scope.get("dedup_resource_cache_key", []),
        "dedup_resource_count": int(page_scope.get("dedup_resource_count") or 0),
        "resource_semantic_labeling": semantic_labeling_report,
        "artifact_metadata": {
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "artifact_role": "formal_artifact_status",
            "semantic_role": "pagegen_resource_semantic_labeling_status",
        },
    }
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _resolve_pagegen_resource_semantic_execution_flow(
    *,
    pagegen_logic_flow: str,
    prompt_screenshot_count: int = 0,
    dedup_resource_count: int = 0,
    adaptive_cascade_enabled: bool,
    adaptive_cascade_min_prompt_screenshots: int = 12,
    adaptive_cascade_min_resources: int = 48,
    cascade_available: bool = True,
) -> str:
    if not adaptive_cascade_enabled:
        return "single_pass"
    _ = (pagegen_logic_flow, dedup_resource_count, adaptive_cascade_min_resources)
    over_budget = int(prompt_screenshot_count or 0) > int(adaptive_cascade_min_prompt_screenshots or 12)
    if over_budget and cascade_available:
        return "cascade"
    return "single_pass"


def _spec_executability_hard_gate_message(
    spec_executability_eval: Dict[str, Any],
    *,
    hard_gate_enabled: bool,
) -> str:
    if not hard_gate_enabled or not isinstance(spec_executability_eval, dict):
        return ""
    summary = spec_executability_eval.get("summary")
    if not isinstance(summary, dict):
        return ""
    try:
        blocking_issue_count = int(summary.get("blocking_issue_count") or 0)
    except (TypeError, ValueError):
        blocking_issue_count = 0
    if blocking_issue_count <= 0:
        return ""
    issue_code_counts = summary.get("issue_code_counts")
    code_summary = ""
    if isinstance(issue_code_counts, dict) and issue_code_counts:
        code_summary = "; issue_code_counts=" + json.dumps(
            issue_code_counts,
            ensure_ascii=False,
            sort_keys=True,
        )
    return (
        "Spec executability hard gate found "
        f"{blocking_issue_count} blocking issue(s); "
        "see phase_page_environment/semantic_api/07_spec_executability_eval/"
        "spec_executability_eval.json and "
        "phase_page_environment/semantic_api/07_spec_executability_eval/"
        "spec_issue_router_report.json"
        f"{code_summary}"
    )


def _effect_contract_hard_gate_message(
    effect_contract_validation: Dict[str, Any],
    *,
    hard_gate_enabled: bool,
) -> str:
    if not hard_gate_enabled or not isinstance(effect_contract_validation, dict):
        return ""
    summary = effect_contract_validation.get("summary")
    if not isinstance(summary, dict):
        return ""
    try:
        blocking_issue_count = int(summary.get("blocking_issue_count") or 0)
    except (TypeError, ValueError):
        blocking_issue_count = 0
    if blocking_issue_count <= 0:
        return ""
    issue_code_counts = summary.get("issue_code_counts")
    code_summary = ""
    if isinstance(issue_code_counts, dict) and issue_code_counts:
        code_summary = "; issue_code_counts=" + json.dumps(
            issue_code_counts,
            ensure_ascii=False,
            sort_keys=True,
        )
    return (
        "Effect contract hard gate found "
        f"{blocking_issue_count} blocking issue(s); "
        "see phase_page_environment/semantic_api/08_spec_compiled_execution_views/"
        "effect_contract_validation.json"
        f"{code_summary}"
    )


def _pagegen_dom_contract_hard_gate_message(
    audit: Dict[str, Any],
    *,
    runtime: bool = False,
) -> str:
    if not isinstance(audit, dict):
        return ""
    summary = audit.get("summary")
    if not isinstance(summary, dict):
        return ""
    if runtime:
        fields = (
            "failing_pages",
            "missing_total",
            "ambiguous_total",
            "noninteractive_total",
            "hidden_or_tiny_total",
            "not_topmost_total",
            "initial_visibility_violation_total",
            "state_surface_violation_total",
            "runtime_probe_error_total",
        )
    else:
        fields = (
            "ambiguous_total",
            "noninteractive_total",
            "hidden_or_tiny_total",
        )
    counts: Dict[str, int] = {}
    for field in fields:
        try:
            value = int(summary.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            counts[field] = value
    if not counts:
        return ""
    label = "PageGen runtime DOM contract" if runtime else "PageGen DOM contract"
    path = (
        "phase_pagegen/validation/dom_contract/pagegen_runtime_dom_contract_audit.json"
        if runtime
        else "phase_pagegen/validation/dom_contract/pagegen_dom_contract_audit.json"
    )
    return (
        f"{label} hard gate failed; "
        f"counts={json.dumps(counts, ensure_ascii=False, sort_keys=True)}; "
        f"see {path}"
    )


def _action_host_manifest_hard_gate_message(
    validation_report: Dict[str, Any],
) -> str:
    if not isinstance(validation_report, dict):
        return ""
    try:
        missing = int(validation_report.get("missing_host_count") or 0)
    except (TypeError, ValueError):
        missing = 0
    try:
        duplicate = int(validation_report.get("duplicate_host_count") or 0)
    except (TypeError, ValueError):
        duplicate = 0
    try:
        semantic = int(validation_report.get("semantic_mismatch_count") or 0)
    except (TypeError, ValueError):
        semantic = 0
    if missing <= 0 and duplicate <= 0 and semantic <= 0:
        return ""
    return (
        "Action host manifest hard gate failed: "
        f"missing={missing} duplicate={duplicate} semantic={semantic}; "
        "see phase_pagegen/action_host_manifest_validation.json"
    )


def _looks_like_trace_id(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.endswith(".html") and (
        text.startswith("task_") or text.startswith("trace_")
    )


def _trace_id_from_step_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for marker in (":step", "::step", "/step", "|step"):
        if marker in text:
            return text.split(marker, 1)[0].strip()
    if ":" in text:
        left, right = text.split(":", 1)
        if left and (right.isdigit() or right.startswith("step")):
            return left.strip()
    return text if _looks_like_trace_id(text) else ""


def _collect_trace_ids_from_payload(payload: Any) -> set[str]:
    trace_ids: set[str] = set()
    trace_id_keys = {
        "trace_id",
        "source_trace_id",
        "task_trace_id",
        "source_task_id",
        "task_id",
    }
    trace_ids_keys = {
        "trace_ids",
        "source_trace_ids",
        "source_task_ids",
        "covered_trace_ids",
        "task_trace_ids",
    }
    step_key_fields = {
        "action_id",
        "obs_key",
        "observation_key",
        "step_key",
        "source_step_key",
        "trace_step_key",
        "segment_id",
    }

    def visit(node: Any, key_hint: str = "") -> None:
        if isinstance(node, Mapping):
            for raw_key, value in node.items():
                key = str(raw_key or "")
                parsed_key_trace_id = _trace_id_from_step_key(key)
                if parsed_key_trace_id:
                    trace_ids.add(parsed_key_trace_id)
                if key in trace_id_keys:
                    if isinstance(value, str):
                        trace_id = str(value).strip()
                        if trace_id:
                            trace_ids.add(trace_id)
                    elif isinstance(value, (int, float)):
                        trace_ids.add(str(value))
                elif key in trace_ids_keys and isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            trace_ids.add(item.strip())
                elif key in step_key_fields:
                    parsed_value_trace_id = _trace_id_from_step_key(value)
                    if parsed_value_trace_id:
                        trace_ids.add(parsed_value_trace_id)
                visit(value, key)
            return
        if isinstance(node, list):
            for item in node:
                visit(item, key_hint)
            return
        if isinstance(node, str) and key_hint in step_key_fields:
            parsed_trace_id = _trace_id_from_step_key(node)
            if parsed_trace_id:
                trace_ids.add(parsed_trace_id)

    visit(payload)
    return {trace_id for trace_id in trace_ids if trace_id}


def _build_trace_coverage_closure_report(
    *,
    accepted_trace_ids: Iterable[str],
    global_reasoning: Optional[Mapping[str, Any]] = None,
    cluster_assignments: Optional[Mapping[str, Any]] = None,
    materialized_bindings: Optional[Mapping[str, Any]] = None,
    page_environment_specs: Optional[Mapping[str, Any]] = None,
    contract_replay_plan: Optional[Mapping[str, Any]] = None,
    action_effect_contracts: Optional[Mapping[str, Any]] = None,
    action_host_manifest: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    accepted = sorted({str(trace_id).strip() for trace_id in accepted_trace_ids if str(trace_id).strip()})
    accepted_set = set(accepted)
    layer_payloads: List[Tuple[str, Any]] = []
    if global_reasoning is not None:
        layer_payloads.append(("step_labels", global_reasoning.get("step_labels") if isinstance(global_reasoning, Mapping) else global_reasoning))
    if cluster_assignments is not None:
        layer_payloads.append(("cluster_assignments", cluster_assignments))
    if materialized_bindings is not None:
        layer_payloads.append(("materialized_bindings", materialized_bindings))
    if page_environment_specs is not None:
        layer_payloads.append(("page_environment_specs", page_environment_specs))
    if contract_replay_plan is not None:
        layer_payloads.append(("contract_replay_plan", contract_replay_plan))
    if action_effect_contracts is not None:
        layer_payloads.append(("action_effect_contracts", {"actions": action_effect_contracts.get("actions", [])} if isinstance(action_effect_contracts, Mapping) else action_effect_contracts))
    if action_host_manifest is not None:
        layer_payloads.append(("action_host_manifest", action_host_manifest))

    layers: Dict[str, Any] = {}
    blocking_issues: List[Dict[str, Any]] = []
    for layer_name, payload in layer_payloads:
        observed_all = _collect_trace_ids_from_payload(payload)
        observed = sorted(observed_all & accepted_set) if accepted_set else sorted(observed_all)
        missing = sorted(accepted_set - set(observed))
        extra = sorted(observed_all - accepted_set)
        layers[layer_name] = {
            "observed_trace_count": len(observed),
            "observed_trace_ids": observed,
            "missing_trace_ids": missing,
            "extra_trace_ids": extra,
        }
        if missing:
            blocking_issues.append(
                {
                    "layer": layer_name,
                    "issue_code": "missing_accepted_trace_coverage",
                    "missing_trace_ids": missing,
                }
            )

    return {
        "format": "trace_coverage_closure_report",
        "schema_revision": 1,
        "accepted_trace_ids": accepted,
        "layers": layers,
        "blocking_issues": blocking_issues,
        "summary": {
            "status": "fail" if blocking_issues else "pass",
            "accepted_trace_count": len(accepted),
            "blocking_issue_count": len(blocking_issues),
        },
    }


def _load_global_reasoning_for_trace_coverage(
    output_dir: str | Path,
    current: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:

    if isinstance(current, Mapping) and current.get("step_labels"):
        return dict(current)
    base = Path(output_dir) if output_dir else Path()
    if not str(base):
        return dict(current) if isinstance(current, Mapping) else {}
    candidates = [
        base / "phase_global_reasoning" / "debug" / "global_reasoning_feedback_fixed.json",
        base / "phase_global_reasoning" / "global_reasoning_final.json",
        base / "phase_global_reasoning" / "debug" / "global_reasoning_final.json",
        base / "phase_global_reasoning" / "debug" / "global_reasoning.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            continue
        if isinstance(payload, Mapping) and payload.get("step_labels"):
            return dict(payload)
    return dict(current) if isinstance(current, Mapping) else {}


def _trace_coverage_hard_gate_message(
    trace_coverage_report: Dict[str, Any],
) -> str:
    if not isinstance(trace_coverage_report, dict):
        return ""
    summary = trace_coverage_report.get("summary")
    if not isinstance(summary, dict):
        return ""
    try:
        blocking_issue_count = int(summary.get("blocking_issue_count") or 0)
    except (TypeError, ValueError):
        blocking_issue_count = 0
    if blocking_issue_count <= 0:
        return ""
    missing_by_layer: Dict[str, List[str]] = {}
    for issue in trace_coverage_report.get("blocking_issues", []):
        if not isinstance(issue, Mapping):
            continue
        layer = str(issue.get("layer") or "unknown_layer")
        missing = [
            str(trace_id)
            for trace_id in issue.get("missing_trace_ids", [])
            if str(trace_id).strip()
        ]
        if missing:
            missing_by_layer[layer] = missing
    layer_summary = json.dumps(missing_by_layer, ensure_ascii=False, sort_keys=True)
    return (
        "Trace coverage closure hard gate failed: "
        f"{blocking_issue_count} layer(s) missing accepted trace coverage; "
        f"missing_by_layer={layer_summary}; "
        "see phase_page_environment/semantic_api/08_spec_compiled_execution_views/"
        "trace_coverage_closure_report.json and phase_pagegen/action_host_trace_coverage_closure_report.json"
    )


def _attach_page_environment_projections_to_extra_page_data(
    *,
    output_dir: str,
    extra_page_data: Optional[Dict[str, Dict[str, Any]]],
    page_environment_projections: Dict[str, Dict[str, Any]],
    logger: Optional[Trace2WorldLogger] = None,
) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = dict(extra_page_data or {})
    for filename, projection in (page_environment_projections or {}).items():
        if not filename or not isinstance(projection, dict):
            continue
        page_data = dict(merged.get(filename, {}))


        for legacy_semantic_key in (
            "page_execution_contract",
            "page_execution_obligations",
            "trace_execution_obligations",
        ):
            page_data.pop(legacy_semantic_key, None)
        semantic_authority = {
            "mode": "page_environment_spec",
            "legacy_execution_contracts_in_prompt": False,
            "legacy_execution_obligations_in_prompt": False,
        }
        page_data["page_environment_projection"] = projection
        page_data["pagegen_semantic_authority"] = semantic_authority
        page_scope = dict(page_data.get("pagegen_input_scope") or {})
        if page_scope:
            page_scope["page_environment_projection"] = projection
            page_scope["pagegen_semantic_authority"] = semantic_authority
            page_data["pagegen_input_scope"] = page_scope
            sprite_dir = _phase_pagegen_sprites_dir(output_dir, filename)
            debug_manifest_path = os.path.join(sprite_dir, "pagegen_scope_debug.json")
            prompt_manifest_paths = [
                os.path.join(sprite_dir, "pagegen_prompt_manifest.json"),
                os.path.join(sprite_dir, "pagegen_input_scope.json"),
            ]
            if os.path.exists(debug_manifest_path):
                try:
                    with open(debug_manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f) or {}
                    if isinstance(manifest, dict):
                        manifest["page_environment_projection"] = projection
                        manifest["pagegen_semantic_authority"] = semantic_authority
                        with open(debug_manifest_path, "w", encoding="utf-8") as f:
                            json.dump(manifest, f, ensure_ascii=False, indent=2)
                        try:
                            from trace2world_pipeline.page_realizer.pagegen_input_scope import (
                                build_actual_pagegen_input_manifest,
                                build_pagegen_prompt_manifest,
                            )

                            prompt_manifest = build_pagegen_prompt_manifest(manifest)
                            for manifest_path in prompt_manifest_paths:
                                with open(manifest_path, "w", encoding="utf-8") as f:
                                    json.dump(prompt_manifest, f, ensure_ascii=False, indent=2)
                            actual_dir = PhaseOutputManager(output_dir).formal(
                                "pagegen", "page_inputs", filename.replace(".html", "")
                            )
                            actual_dir.mkdir(parents=True, exist_ok=True)
                            actual_path = actual_dir / "actual_pagegen_input_manifest.json"
                            actual_path.write_text(
                                json.dumps(
                                    build_actual_pagegen_input_manifest(manifest),
                                    ensure_ascii=False,
                                    indent=2,
                                ),
                                encoding="utf-8",
                            )
                        except Exception:
                            pass
                except Exception as exc:
                    if logger:
                        logger.log_warning(f"Failed to update page environment manifest for {filename}: {exc}")
        merged[filename] = page_data
    return merged


def _rewrite_pagegen_scope_artifacts(
    *,
    output_dir: str,
    filename: str,
    page_scope: Dict[str, Any],
) -> None:
    from trace2world_pipeline.page_realizer.pagegen_input_scope import (
        build_actual_pagegen_input_manifest,
        build_pagegen_prompt_manifest,
        build_pagegen_scope_debug,
    )

    sprite_dir = _phase_pagegen_sprites_dir(output_dir, filename)
    os.makedirs(sprite_dir, exist_ok=True)
    prompt_manifest = build_pagegen_prompt_manifest(page_scope)
    debug_scope = build_pagegen_scope_debug(page_scope)
    actual_dir = PhaseOutputManager(output_dir).formal("pagegen", "page_inputs", filename.replace(".html", ""))
    actual_dir.mkdir(parents=True, exist_ok=True)
    actual_manifest = build_actual_pagegen_input_manifest(page_scope)
    for name, payload in (
        ("pagegen_prompt_manifest.json", prompt_manifest),
        ("pagegen_input_scope.json", prompt_manifest),
        ("pagegen_scope_debug.json", debug_scope),
    ):
        with open(os.path.join(sprite_dir, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    (actual_dir / "actual_pagegen_input_manifest.json").write_text(
        json.dumps(actual_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _merge_canonical_pagegen_scope(
    base_scope: Dict[str, Any],
    overlay_scope: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged = dict(base_scope or {})
    if isinstance(overlay_scope, dict):
        merged.update(overlay_scope)
    for key in (
        "page_screenshot_refs",
        "prompt_screenshot_refs",
        "unique_prompt_screenshot_refs",
        "task_extra_screenshot_refs",
        "non_prompt_page_screenshot_refs",
        "excluded_page_screenshot_refs",
        "full_page_screenshot_refs",
        "render_scope_contract",
        "pagegen_evidence_alignment",
        "canonical_step_evidence_refs",
    ):
        value = (base_scope or {}).get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _attach_pagegen_input_scopes_to_extra_page_data(
    extra_page_data: Optional[Dict[str, Dict[str, Any]]],
    pagegen_input_scopes: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = dict(extra_page_data or {})
    for filename, scope in (pagegen_input_scopes or {}).items():
        if not filename or not isinstance(scope, dict):
            continue
        page_data = dict(merged.get(filename, {}))
        existing_scope = (
            page_data.get("pagegen_input_scope")
            if isinstance(page_data.get("pagegen_input_scope"), dict)
            else None
        )
        page_data["pagegen_input_scope"] = _merge_canonical_pagegen_scope(
            dict(scope),
            existing_scope,
        )
        merged[filename] = page_data
    return merged


def _merge_cascade_round_resource_semantics(
    *,
    output_dir: str,
    generated_pages: List[Any],
    extra_page_data: Dict[str, Dict[str, Any]],
    semantic_enabled: bool,
    logger: Optional[Trace2WorldLogger] = None,
) -> None:
    if not generated_pages or not isinstance(extra_page_data, dict):
        return
    from trace2world_pipeline.page_realizer.resource_semantics import merge_resource_semantic_annotations

    updated_count = 0
    for page in generated_pages:
        filename = str(getattr(page, "filename", "") or "")
        if not filename:
            continue
        metadata = getattr(page, "metadata", {}) or {}
        cascade_report = metadata.get("cascade_report") if isinstance(metadata, dict) else {}
        if not isinstance(cascade_report, dict) or cascade_report.get("logic_flow") != "cascade":
            continue
        round_labeled = cascade_report.get("round_labeled_resources") or []
        page_data = extra_page_data.get(filename)
        if not isinstance(page_data, dict):
            continue
        resources = page_data.get("dedup_page_resources") or page_data.get("page_resources") or []
        if not isinstance(resources, list):
            continue
        merged_resources, report = merge_resource_semantic_annotations(resources, round_labeled)
        if semantic_enabled:
            report.setdefault("enabled", True)
        report["pagegen_execution_flow"] = "cascade"
        report["source"] = report.get("source") or "cascade_round_aggregate"
        page_scope = dict(page_data.get("pagegen_input_scope") or {})
        if page_scope:
            page_scope["dedup_page_resources"] = merged_resources
            page_scope["dedup_resource_count"] = len(merged_resources)
            page_scope["resource_semantic_labeling"] = report
            page_scope["pagegen_execution_flow"] = "cascade"
            projection_path, projection = _write_pagegen_resource_projection_artifact(
                output_dir=output_dir,
                filename=filename,
                page_scope=page_scope,
                resources=merged_resources,
                producer="trace_generator._merge_cascade_round_resource_semantics",
            )
            page_scope["pagegen_resource_projection_path"] = projection_path
            resource_text = _resource_list_text_from_projection(projection)
        else:
            projection_path = ""
            projection = {}
            resource_text = ""
        page_data["dedup_page_resources"] = merged_resources
        page_data["dedup_resource_count"] = len(merged_resources)
        page_data["resource_list_text"] = resource_text
        page_data["dedup_resource_list_text"] = resource_text
        page_data["resource_semantic_labeling"] = report
        page_data["pagegen_resource_projection"] = projection
        page_data["pagegen_resource_projection_path"] = projection_path
        if page_scope:
            page_data["pagegen_input_scope"] = page_scope
            _rewrite_pagegen_scope_artifacts(
                output_dir=output_dir,
                filename=filename,
                page_scope=page_scope,
            )
            sprite_dir_for_labeling = _phase_pagegen_sprites_dir(output_dir, filename)
            page_sprite_paths = [
                os.path.join(sprite_dir_for_labeling, name)
                for name in ("icon_sprite.jpg", "photo_sprite.jpg", "asset_sprite.jpg")
                if os.path.exists(os.path.join(sprite_dir_for_labeling, name))
            ]
            _write_resource_semantic_labeling_artifact(
                _resource_semantic_labeling_path(sprite_dir_for_labeling),
                filename=filename,
                page_scope=page_scope,
                labeled_resources=merged_resources,
                semantic_labeling_report=report,
                reuse_key=_resource_semantic_labeling_reuse_key(
                    page_scope,
                    sprite_paths=page_sprite_paths,
                    model=str((report or {}).get("model") or ""),
                ),
                resource_list_text=resource_text,
            )
        sprite_dir = _phase_pagegen_sprites_dir(output_dir, filename)
        os.makedirs(sprite_dir, exist_ok=True)
        _write_resource_list_from_projection_artifact(
            os.path.join(sprite_dir, "resource_list.txt"),
            projection,
        )
        updated_count += 1
    if updated_count and logger:
        logger.log_info(
            f"Aggregated cascade round resource semantic labels into {updated_count} page-level resource artifacts"
        )


_INLINE_SCRIPT_RE = re.compile(
    r"<script\b(?P<attrs>(?![^>]*\bsrc\s*=)[^>]*)>(?P<content>[\s\S]*?)</script\s*>",
    re.IGNORECASE,
)


def _inline_script_is_javascript(attrs: str) -> bool:
    type_match = re.search(r"\btype\s*=\s*(['\"])(.*?)\1", attrs or "", re.IGNORECASE | re.DOTALL)
    if not type_match:
        return True
    script_type = type_match.group(2).strip().lower()
    return script_type in {
        "",
        "text/javascript",
        "application/javascript",
        "application/ecmascript",
        "text/ecmascript",
        "module",
    }


def _validate_inline_javascript_syntax(
    *,
    output_dir: str,
    pages_dir: str,
    logger: Optional[Trace2WorldLogger] = None,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "format": "frontend_inline_javascript_syntax_validation",
        "schema_revision": 1,
        "mode": "validation_only",
        "write_modified": False,
        "repair_policy": "fail_without_html_mutation",
        "ok": True,
        "node_available": bool(shutil.which("node")),
        "checked_scripts": 0,
        "failed_scripts": 0,
        "pages": {},
    }
    validation_dir = str(PhaseOutputManager(output_dir).formal("pagegen", "validation", "frontend_js_syntax"))
    os.makedirs(validation_dir, exist_ok=True)

    def _debug_filename(filename: str) -> str:
        name = (filename or "unknown_page").replace("\\", "/").split("/")[-1]
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".json"

    def _record_syntax_issues(filename: str, failed_scripts: list[dict[str, Any]]) -> None:
        if not failed_scripts:
            return
        pom = PhaseOutputManager(output_dir)
        rel_issue_path = f"pagegen_contract_issues/{_debug_filename(filename)}"
        issue_path = pom.debug("pagegen", rel_issue_path)
        existing: dict[str, Any] = {}
        if issue_path.exists():
            try:
                loaded = json.loads(issue_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        issues = list(existing.get("issues", [])) if isinstance(existing.get("issues"), list) else []
        seen = {
            (
                str(issue.get("code") or ""),
                str(issue.get("artifact") or ""),
                str(issue.get("selector_hint") or ""),
            )
            for issue in issues
            if isinstance(issue, dict)
        }
        for failed in failed_scripts:
            artifact = str(failed.get("artifact") or "")
            selector_hint = f"script[{failed.get('script_index')}]"
            issue = {
                "code": "frontend_js_syntax.syntax_error",
                "message": (
                    f"Inline JavaScript syntax validation failed for {filename} "
                    f"script {failed.get('script_index')}; reject the PageGen output and "
                    "fix the producer source or prompt."
                ),
                "selector_hint": selector_hint,
                "artifact": artifact,
                "evidence": json.dumps(
                    {
                        "returncode": failed.get("returncode"),
                        "stderr": failed.get("stderr", ""),
                    },
                    ensure_ascii=False,
                ),
                "expected_contract": (
                    "PageGen must return complete HTML with closed script/body/html tags; "
                    "all inline JavaScript must parse under node --check."
                ),
            }
            identity = (issue["code"], issue["artifact"], issue["selector_hint"])
            if identity not in seen:
                issues.append(issue)
                seen.add(identity)
        html_path = Path(pages_dir) / filename
        payload = {
            "format": "pagegen_contract_issues",
            "schema_revision": 1,
            "status": "rejected_pagegen_output",
            "mode": "source_fix_required",
            "filename": filename,
            "attempt": existing.get("attempt"),
            "remaining_issue_count": len(issues),
            "issues": issues,
            "last_error": existing.get("last_error", ""),
            "saved_html_path": os.path.relpath(str(html_path), output_dir) if html_path.exists() else "",
            "candidate_saved": html_path.exists(),
            "html_saved": html_path.exists(),
        }
        pom.write_json(
            "pagegen",
            "debug",
            rel_issue_path,
            payload,
            producer="trace_generator.inline_javascript_syntax_validation",
        )

    if not report["node_available"]:
        report["ok"] = False
        report["error"] = "node executable is required for frontend inline JavaScript syntax gate"
    else:
        for html_path in sorted(Path(pages_dir).glob("*.html")):
            try:
                html = html_path.read_text(encoding="utf-8")
            except OSError as exc:
                report["ok"] = False
                report["pages"][html_path.name] = {"status": "read_error", "error": repr(exc)}
                continue
            page_record = {"status": "ok", "scripts": []}
            for index, match in enumerate(_INLINE_SCRIPT_RE.finditer(html), start=1):
                if not _inline_script_is_javascript(match.group("attrs") or ""):
                    continue
                script = match.group("content").strip()
                if not script:
                    continue
                report["checked_scripts"] += 1
                js_path = os.path.join(validation_dir, f"{html_path.stem}.script_{index}.js")
                with open(js_path, "w", encoding="utf-8") as f:
                    f.write(script)
                    f.write("\n")
                proc = subprocess.run(
                    ["node", "--check", js_path],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                )
                item = {
                    "script_index": index,
                    "artifact": os.path.relpath(js_path, output_dir),
                    "returncode": proc.returncode,
                }
                if proc.returncode != 0:
                    report["ok"] = False
                    report["failed_scripts"] += 1
                    page_record["status"] = "syntax_error"
                    item["stdout"] = proc.stdout[-2000:]
                    item["stderr"] = proc.stderr[-4000:]
                page_record["scripts"].append(item)
            failed_scripts = [
                item
                for item in page_record["scripts"]
                if isinstance(item, dict) and int(item.get("returncode", 0) or 0) != 0
            ]
            _record_syntax_issues(html_path.name, failed_scripts)
            if page_record["scripts"] or page_record["status"] != "ok":
                report["pages"][html_path.name] = page_record
    report_path = os.path.join(validation_dir, "inline_js_syntax_validation.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    if logger:
        logger.log_info(
            "Inline JS syntax gate: checked=%s failed=%s ok=%s"
            % (report.get("checked_scripts", 0), report.get("failed_scripts", 0), report.get("ok", False))
        )
    return report


def _move_to_deprecated(path: str, *, reason: str = "") -> str:
    if not path or not os.path.exists(path):
        return ""
    deprecated_dir = os.path.join(os.path.dirname(path), "deprecated")
    os.makedirs(deprecated_dir, exist_ok=True)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    target = os.path.join(deprecated_dir, base)
    if os.path.exists(target):
        suffix_seed = hashlib.sha256(f"{path}:{reason}:{time.time()}".encode("utf-8")).hexdigest()[:8]
        target = os.path.join(deprecated_dir, f"{stem}.{suffix_seed}{ext}")
    shutil.move(path, target)
    return target


def _load_json_artifact(path: str) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _publish_semantic_design_aggregate(site_model: AppSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    site_model.semantic_design = payload
    return payload


def _page_environment_spec_reuse_decision(
    *,
    cached_spec: Optional[Dict[str, Any]],
    cached_executability_eval: Optional[Dict[str, Any]] = None,
    canonical_schema: Optional[Dict[str, Any]] = None,
    schema_source_path: str = "",
    schema_mtime: Optional[float] = None,
    current_page_filenames: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    canonical_schema_hash = _stable_json_hash(canonical_schema) if isinstance(canonical_schema, dict) and canonical_schema else ""
    decision: Dict[str, Any] = {
        "format": "page_environment_spec_reuse_decision",
        "schema_revision": 3,
        "reused": False,
        "reason": "missing_or_invalid_cached_spec",
        "would_call_llm": True,
        "manual_invalidation_policy": "delete_or_deprecate_canonical_artifact_to_force_recompute",
        "canonical_global_schema_hash": canonical_schema_hash,
        "schema_source_path": schema_source_path,
        "schema_mtime": schema_mtime,
    }
    if not _looks_like_page_environment_specs(cached_spec):
        return decision
    current_filename_set = {
        str(filename or "").strip()
        for filename in (current_page_filenames or [])
        if str(filename or "").strip()
    }
    cached_specs_by_filename = cached_spec.get("page_environment_specs_by_filename")
    cached_filename_set = (
        {
            str(filename or "").strip()
            for filename in cached_specs_by_filename
            if str(filename or "").strip()
        }
        if isinstance(cached_specs_by_filename, dict)
        else set()
    )
    decision.update({
        "current_page_count": len(current_filename_set),
        "cached_page_count": len(cached_filename_set),
    })
    if current_filename_set and cached_filename_set != current_filename_set:
        decision.update({
            "reason": "site_model_page_identity_changed",
            "manual_invalidation_policy": (
                "regenerate_page_environment_after_site_model_page_identity_change"
            ),
            "missing_cached_page_filenames": sorted(
                current_filename_set - cached_filename_set
            ),
            "stale_cached_page_filenames": sorted(
                cached_filename_set - current_filename_set
            ),
        })
        return decision
    if isinstance(cached_executability_eval, dict):
        evaluation_summary = (
            cached_executability_eval.get("summary")
            if isinstance(cached_executability_eval.get("summary"), dict)
            else {}
        )
        evaluation_status = str(evaluation_summary.get("status") or "").strip().lower()
        blocking_issues = int(evaluation_summary.get("blocking_issue_count") or 0)
        decision["cached_executability_status"] = evaluation_status
        decision["cached_blocking_issue_count"] = blocking_issues
        if evaluation_status != "pass" or blocking_issues:
            decision.update({
                "reason": "cached_spec_executability_not_pass",
                "manual_invalidation_policy": "regenerate_page_environment_after_failed_executability_gate",
            })
            return decision
    cached_projection = cached_spec.get("global_schema_projection")
    cached_schema_hash = (
        cached_projection.get("canonical_schema_hash")
        if isinstance(cached_projection, dict)
        else ""
    )
    decision["cached_global_schema_hash"] = cached_schema_hash or ""
    if canonical_schema_hash and not cached_schema_hash:
        decision.update({
            "reason": "cached_spec_missing_canonical_schema_hash",
            "manual_invalidation_policy": "regenerate_page_environment_after_schema_hash_gate",
        })
        return decision
    if canonical_schema_hash and cached_schema_hash != canonical_schema_hash:
        decision.update({
            "reason": "canonical_schema_hash_changed",
            "manual_invalidation_policy": "regenerate_page_environment_after_schema_hash_change",
        })
        return decision
    decision.update({
        "reused": True,
        "reason": "canonical_artifact_present",
        "would_call_llm": False,
    })
    return decision


def _stable_json_hash(payload: Any) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_page_environment_spec_reuse_decision(
    output_dir: str,
    decision: Dict[str, Any],
) -> None:
    from trace2world_pipeline.core.phase_output import PhaseOutputManager
    pom = PhaseOutputManager(output_dir)
    pom.write_json("page_environment", "debug", "reuse_decision.json", dict(decision))


def _looks_like_page_environment_specs(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    specs = payload.get("page_environment_specs_by_filename")
    return (
        payload.get("format") == "page_environment_specs"
        and isinstance(specs, dict)
        and bool(specs)
    )


def _spec_execution_views_by_page_from_semantic_design(
    semantic_design_payload: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(semantic_design_payload, dict):
        return {}
    compiled_views = semantic_design_payload.get("spec_compiled_execution_views")
    compiled_pagegen_view = (
        compiled_views.get("pagegen_spec_view_by_page")
        if isinstance(compiled_views, dict)
        else None
    )
    direct_pagegen_view = semantic_design_payload.get("pagegen_spec_view_by_page")
    candidates = [
        compiled_pagegen_view.get("views_by_filename")
        if isinstance(compiled_pagegen_view, dict)
        else None,
        direct_pagegen_view.get("views_by_filename")
        if isinstance(direct_pagegen_view, dict)
        else None,
        semantic_design_payload.get("spec_execution_views_by_page"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        return {
            str(filename): view
            for filename, view in candidate.items()
            if filename and isinstance(view, dict)
        }
    return {}


def _domain_contracts_from_semantic_design_or_artifact(
    semantic_design_payload: Optional[Mapping[str, Any]],
    *,
    output_dir: str,
) -> Dict[str, Any]:
    payload = semantic_design_payload if isinstance(semantic_design_payload, Mapping) else {}
    compiled_views = payload.get("spec_compiled_execution_views")
    if isinstance(compiled_views, Mapping):
        domain_contracts = compiled_views.get("domain_contracts")
        if isinstance(domain_contracts, dict):
            return domain_contracts

    domain_contracts = payload.get("domain_contracts")
    if isinstance(domain_contracts, dict):
        return domain_contracts

    artifact_path = os.path.join(
        output_dir or "",
        "phase_page_environment",
        "semantic_api",
        "08_spec_compiled_execution_views",
        "domain_contracts.json",
    )
    artifact_payload = _load_json_artifact(artifact_path)
    return artifact_payload if isinstance(artifact_payload, dict) else {}


def _page_environment_spec_authority_shape(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "ok": True,
        "error_count": 0,
        "errors": [],
        "page_count": 0,
    }
    if not isinstance(payload, dict):
        report["errors"].append({"code": "payload_not_object"})
    elif payload.get("format") != "page_environment_specs":
        report["errors"].append({"code": "invalid_format", "format": payload.get("format")})
    specs = payload.get("page_environment_specs_by_filename") if isinstance(payload, dict) else None
    if not isinstance(specs, dict) or not specs:
        report["errors"].append({"code": "missing_page_environment_specs_by_filename"})
    elif isinstance(specs, dict):
        report["page_count"] = len(specs)
        for filename, page_spec in specs.items():
            if not isinstance(page_spec, dict):
                report["errors"].append({
                    "code": "page_spec_not_object",
                    "filename": str(filename),
                })
    report["error_count"] = len(report["errors"])
    report["ok"] = report["error_count"] == 0
    return report


def _looks_like_interface_requirement_projection(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("format") == "interface_requirement_projection"
        and isinstance(payload.get("requirements_by_page"), dict)
        and isinstance(payload.get("requirements_by_id"), dict)
        and isinstance(payload.get("summary"), dict)
    )


def _interface_requirement_projection_cache_path(output_dir: str) -> str:
    return str(
        PhaseOutputManager(output_dir).formal(
            "interface_design",
            "interface_requirements",
            "interface_requirement_projection.json",
        )
    )


def _load_reusable_interface_requirement_projection(
    output_dir: str,
) -> tuple[Optional[Dict[str, Any]], str]:
    cache_path = _interface_requirement_projection_cache_path(output_dir)
    cached = _load_json_artifact(cache_path)
    if _looks_like_interface_requirement_projection(cached):
        return cached, cache_path
    return None, cache_path


def _load_trace_context(trace_resource_dir: str, output_dir: str) -> tuple:
    trace_descriptions = {}
    trace_actions = {}
    trace_index_path = os.path.join(trace_resource_dir, "index.json")
    if os.path.exists(trace_index_path):
        with open(trace_index_path, 'r', encoding='utf-8') as f:
            root = json.load(f)
        for tr in root.get("traces", []):
            tid = tr.get("id", "")
            if tid:
                trace_descriptions[tid] = tr.get("description_en", "")
            trace_dir = os.path.join(trace_resource_dir, tr.get("path", tr.get("id", "")))
            steps_idx = os.path.join(trace_dir, "index.json")
            if os.path.exists(steps_idx):
                with open(steps_idx, 'r', encoding='utf-8') as f:
                    trace_data = json.load(f)
                if isinstance(trace_data, dict):
                    steps = trace_data.get("steps", [])
                elif isinstance(trace_data, list):
                    steps = trace_data
                else:
                    steps = []
                trace_actions[tid] = [
                    {"step": s.get("step"), "action": s.get("action", ""),
                     "page": s.get("page", ""), "description": s.get("description", "")}
                    for s in steps if s.get("action", "") != "初始状态"
                ]


    from trace2world_pipeline.core.phase_output import PhaseOutputManager
    _pom = PhaseOutputManager(output_dir)
    gr_path = str(_pom.debug("global_reasoning", "global_reasoning.json"))
    canonical_pages = []
    canonical_routes = []
    capture_actions = {}
    task_related_elements = {}
    if os.path.exists(gr_path):
        with open(gr_path, 'r', encoding='utf-8') as f:
            gr = json.load(f)
        canonical_pages = gr.get("canonical_pages", [])
        canonical_routes = gr.get("canonical_routes", [])
        capture_actions = gr.get("capture_actions", {})
        task_related_elements = gr.get("task_related_elements", {})

    trace_context = {
        "trace_descriptions": trace_descriptions,
        "trace_actions": trace_actions,
        "canonical_pages": canonical_pages,
        "canonical_routes": canonical_routes,
        "capture_actions": capture_actions,
        "task_related_elements": task_related_elements,
    }


    sdk_lines = []
    for tid, desc in trace_descriptions.items():
        actions = trace_actions.get(tid, [])
        sdk_lines.append(
            f'Trace "{tid}": {desc} ({len(actions)} steps: '
            f'{" -> ".join(a["action"] for a in actions)})'
        )
    sdk_context = "\n".join(sdk_lines) if sdk_lines else ""

    return trace_context, sdk_context


def _build_interface_design_prompt_context(
    trace_context: Dict[str, Any],
    tasks: List[Dict[str, Any]],
    website_type: str,
    page_environment_projections: Optional[Dict[str, Dict[str, Any]]] = None,
    page_environment_evaluation: Optional[Dict[str, Any]] = None,
    interface_requirement_projection: Optional[Dict[str, Any]] = None,
) -> str:
    sections = []
    sections.append(
        "## External/System UI Exclusion\n"
        "Some trace screenshots may contain OS or non-target-app surfaces such as "
        "verification SMS previews, OS notification banners, incoming call bars, "
        "system permission sheets, app switcher surfaces, keyboard/IME chrome, or "
        "status-bar-only system UI. Do NOT design interfaces, task elements, page "
        "functions, SDK data fields, or navigation behavior for those external/system "
        "regions. Only model target-app business UI and user actions."
    )
    sections.append(
        "## Search Entry Dynamic State\n"
        "For search-entry or suggestion-entry pages within the same stable search "
        "scope, the search input value, search history, recommendation chips, "
        "recommended queries, and autocomplete / typeahead rows are dynamic state "
        "within the same page instance. Do NOT treat different query text, history "
        "text, recommendation text, or autocomplete text as separate pages or "
        "separate page instances. However, if the screenshot exposes a different "
        "visible scope anchor that changes how the query is interpreted or where "
        "results/actions apply, that is not input text state and should remain a "
        "separate page instance. Interface design should use query-driven "
        "operations, such as getSearchSuggestions(scope, query), "
        "getSearchHistory(scope), and submitSearch(scope, query), instead of "
        "per-query interfaces."
    )

    spec_section = _build_interface_design_spec_context(
        page_environment_projections or {},
        page_environment_evaluation or {},
        interface_requirement_projection or {},
    )
    spec_authoritative = bool(spec_section)


    if spec_authoritative:
        sections.append(
            "## Upstream Evidence Boundary\n"
            "Raw trace actions, route catalogs, extracted content, and runtime "
            "observations have already been compiled into the authoritative page "
            "environment spec below. Use them only through this spec projection."
        )
        sections.append(spec_section)
        return "\n\n".join(sections)


    trace_descriptions = trace_context.get("trace_descriptions", {})
    if trace_descriptions:
        sections.append(
            "## Trace Descriptions (business goals — REAL user intents, do not rewrite)"
        )
        for tid, desc in trace_descriptions.items():
            sections.append(f'- **{tid}**: {desc}')


    canonical_pages = trace_context.get("canonical_pages", [])
    if canonical_pages:
        sections.append("## Canonical Pages (unified page catalog across all traces)")
        for cp in canonical_pages:
            sections.append(
                f'- **{cp["page_cluster_id"]}** ({cp.get("page_type_hint", "unknown")}): '
                f'{cp.get("page_name", "")} — {cp.get("description", "")}'
            )


    canonical_routes = trace_context.get("canonical_routes", [])
    if canonical_routes:
        sections.append("## Canonical Routes (page navigation graph)")
        for cr in canonical_routes:
            sections.append(
                f'- {cr.get("from_page_cluster_id", "")} '
                f'--[{cr.get("trigger", "")}]--> '
                f'{cr.get("to_page_cluster_id", "")}'
            )


    capture_actions = trace_context.get("capture_actions", {})
    if capture_actions:
        sections.append(
            "## Capture Actions (structured user action sequences — REAL facts)"
        )
        for tid, actions in capture_actions.items():
            sections.append(f'### Trace: {tid}')
            for a in actions:
                target = f' → {a.get("target_element", "")}' if a.get("target_element") else ""
                result = f' [{a.get("result_page", "")}]' if a.get("result_page") else ""
                sections.append(
                    f'  Step {a.get("step", "?")}: {a.get("action", "")}'
                    f'{target}{result} — {a.get("description", "")}'
                )


    task_related = trace_context.get("task_related_elements", {})
    if task_related:
        sections.append("## Task-Related UI Elements (per page cluster)")
        for cluster_id, elements in task_related.items():
            sections.append(f'### {cluster_id}')
            for el in elements[:10]:
                step_refs = el.get("task_step_refs", [])
                refs = ", ".join(
                    f'{r.get("trace_id")}:{r.get("step")}' for r in step_refs[:3]
                )
                sections.append(
                    f'  - {el.get("element_name", "")} '
                    f'({el.get("element_type", "")}) [{refs}]'
                )

    return "\n\n".join(sections)


def _build_interface_design_spec_context(
    page_environment_projections: Dict[str, Dict[str, Any]],
    page_environment_evaluation: Dict[str, Any],
    interface_requirement_projection: Optional[Dict[str, Any]] = None,
) -> str:
    if not page_environment_projections:
        return ""

    def _dedupe_values(items: Any, limit: int) -> List[Any]:
        values: List[Any] = []
        seen = set()
        for item in items if isinstance(items, list) else []:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
            if key in seen:
                continue
            seen.add(key)
            values.append(item)
            if len(values) >= limit:
                break
        return values

    def _compact_list(items: Any, limit: int) -> List[Any]:
        return list(items[:limit]) if isinstance(items, list) else []

    def _effect_summary(item: Dict[str, Any]) -> Dict[str, Any]:
        effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
        return {
            key: effect.get(key)
            for key in (
                "effect",
                "field_role",
                "query_transition_kind",
                "mutation_policy",
                "target_filename",
                "sdk_method",
            )
            if effect.get(key) not in (None, "", [], {})
        }

    def _compact_component(component: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: component.get(key)
            for key in (
                "component_id",
                "component_kind",
                "semantic_role",
                "display_label",
                "layout_policy",
                "interaction_role",
                "target_filename",
            )
            if component.get(key) not in (None, "", [], {})
        }

    def _compact_transition(transition: Dict[str, Any]) -> Dict[str, Any]:
        compact = {
            key: transition.get(key)
            for key in (
                "action_intent",
                "reducer",
                "target_filename",
                "query_transition_kind",
            )
            if transition.get(key) not in (None, "", [], {})
        }
        effect = _effect_summary(transition)
        if effect:
            compact["effect"] = effect
        return compact

    def _role_list(items: Any, key: str, limit: int) -> List[str]:
        values: List[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            value = str(item.get(key) or "").strip()
            if value:
                values.append(value)
        return [str(item) for item in _dedupe_values(values, limit=limit)]

    reducer_inventory: Dict[str, Dict[str, Any]] = {}
    parameter_inventory: Dict[str, Dict[str, Any]] = {}

    pages: Dict[str, Any] = {}
    for filename in sorted(page_environment_projections.keys())[:120]:
        spec = page_environment_projections.get(filename) or {}
        if not isinstance(spec, dict):
            continue
        transitions = [item for item in _compact_list(spec.get("transitions"), 10) if isinstance(item, dict)]
        parameters = [item for item in _compact_list(spec.get("parameterized_inputs"), 8) if isinstance(item, dict)]
        oracles = [item for item in _compact_list(spec.get("oracles"), 6) if isinstance(item, dict)]
        components = [item for item in _compact_list(spec.get("components"), 12) if isinstance(item, dict)]
        compact_transitions = [_compact_transition(item) for item in transitions]
        parameter_roles = _role_list(parameters, "field_role", 8) or _role_list(parameters, "parameter_id", 8)
        component_roles = _role_list(components, "component_role", 10) or _role_list(components, "semantic_role", 10)
        oracle_types = _role_list(oracles, "oracle_type", 6)
        if compact_transitions or parameter_roles or oracle_types:
            pages[filename] = {
                "component_roles": component_roles,
                "parameter_roles": parameter_roles,
                "transitions": compact_transitions,
                "oracle_types": oracle_types,
            }
        for transition in transitions:
            reducer = str(transition.get("reducer") or "").strip()
            if reducer:
                entry = reducer_inventory.setdefault(reducer, {"count": 0, "sample_pages": []})
                entry["count"] += 1
                entry["sample_pages"] = _dedupe_values(list(entry.get("sample_pages", [])) + [filename], limit=8)
        for parameter in parameters:
            role = str(parameter.get("field_role") or parameter.get("parameter_id") or "").strip()
            if role:
                entry = parameter_inventory.setdefault(role, {"count": 0, "sample_pages": []})
                entry["count"] += 1
                entry["sample_pages"] = _dedupe_values(list(entry.get("sample_pages", [])) + [filename], limit=8)

    evaluation_summary = {}
    if isinstance(page_environment_evaluation, dict):
        evaluation_summary = page_environment_evaluation.get("summary", {}) or {}

    payload = {
        "authority": "page_environment_spec",
        "interface_design_role": (
            "Project the authoritative environment spec into SDK/interface methods. "
            "Do not reinterpret page content, route targets, initial state, runtime "
            "reducers, query behavior, or fixed-slot choices from raw trace evidence."
        ),
        "rules": [
            "Expose APIs only for reducers, transitions, parameterized inputs, and oracles present in the spec.",
            "Search/autocomplete, form draft/commit, cart quantity, filter selection, and overlays must remain stateful projections, not prefilled initial content.",
            "Alternative route affordances are business navigation shortcuts to materialized pages, separate from L0 task-action broadcast obligations.",
            "If the spec does not require a component interaction, do not invent a task-specific SDK method for it.",
        ],
        "evaluation_summary": evaluation_summary,
        "interface_requirement_projection": {
            "role": "deterministic requirements derived from page_environment_spec; every non-frontend-only method must satisfy requirement ids instead of guessing semantics from method names",
            "summary": (interface_requirement_projection or {}).get("summary", {}),
            "requirements_by_page": (interface_requirement_projection or {}).get("requirements_by_page", {}),
            "requirements_by_id": (interface_requirement_projection or {}).get("requirements_by_id", {}),
        } if interface_requirement_projection else {},
        "capability_inventory": {
            "reducers": reducer_inventory,
            "parameterized_inputs": parameter_inventory,
        },
        "pages": pages,
    }
    return (
        "## Authoritative Page Environment Spec (Interface Design Input)\n"
        "The following compact projection is the semantic source of truth for "
        "interface design. Upstream trace/context sections above are evidence only; "
        "this spec decides state, transitions, route targets, and render obligations.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


def _build_sdk_generation_prompt_context(
    sdk_context_str: str,
    trace_context: Dict[str, Any],
) -> str:
    if not sdk_context_str:
        return ""

    sections = []
    sections.append(
        "Trace Action Context (REAL user action flows from trace screenshots):"
    )
    sections.append(sdk_context_str)


    canonical_routes = trace_context.get("canonical_routes", [])
    if canonical_routes:
        sections.append(
            "\nPage Navigation Triggers (inform interface design for page transitions):"
        )
        for cr in canonical_routes:
            trigger = cr.get("trigger", "")
            sections.append(f'  - "{trigger}" → navigates to a new page')

    return "\n".join(sections)


def _extract_query_domain_filenames(page_environment_specs: Dict[str, Any]) -> set[str]:
    specs_by_filename = (
        page_environment_specs.get("page_environment_specs_by_filename")
        if isinstance(page_environment_specs, dict)
        else {}
    )
    if not isinstance(specs_by_filename, dict):
        return set()

    query_transition_kinds = {
        "query_entry",
        "suggestion_projection",
        "committed_result",
        "selected_suggestion_commit",
        "search_within_collection",
    }
    query_reducers = {"type_search", "project_suggestions"}
    query_effects = {"project_suggestions"}

    def _filename(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return Path(text.split("#", 1)[0].split("?", 1)[0]).name

    def _is_query_parameter(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        return (
            str(item.get("field_role") or item.get("parameter_id") or "").strip()
            == "business_query"
        )

    def _is_query_projection(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        kind = str(item.get("query_transition_kind") or "").strip()
        policy = str(item.get("projection_policy") or "").strip()
        return kind in query_transition_kinds or "suggestion" in policy or "query" in policy

    def _is_query_transition(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
        kind = str(
            item.get("query_transition_kind")
            or effect.get("query_transition_kind")
            or ""
        ).strip()
        reducer = str(item.get("reducer") or "").strip()
        effect_name = str(effect.get("effect") or "").strip()
        mutation_policy = str(effect.get("mutation_policy") or "").strip()
        if item.get("query_state_owner") or effect.get("query_state_owner"):
            return True
        if kind in query_transition_kinds:
            return True
        if reducer in query_reducers:
            return True
        if effect_name in query_effects:
            return True
        return "project_suggestions" in mutation_policy

    query_domain_filenames: set[str] = set()
    for raw_filename, page_spec in specs_by_filename.items():
        if not isinstance(page_spec, dict):
            continue
        filename = _filename(raw_filename)
        if not filename:
            continue
        parameters = (
            page_spec.get("parameterized_inputs")
            if isinstance(page_spec.get("parameterized_inputs"), list)
            else []
        )
        projections = (
            page_spec.get("query_projections")
            if isinstance(page_spec.get("query_projections"), list)
            else []
        )
        transitions = (
            page_spec.get("transitions")
            if isinstance(page_spec.get("transitions"), list)
            else []
        )
        if any(_is_query_parameter(item) for item in parameters):
            query_domain_filenames.add(filename)
            continue
        if any(_is_query_projection(item) for item in projections):
            query_domain_filenames.add(filename)
            continue
        if any(_is_query_transition(item) for item in transitions):
            query_domain_filenames.add(filename)
    return query_domain_filenames


def _score_entity_key(storage_key: str, page_stem_tokens: set) -> int:
    sk_tokens = set(storage_key.lower().replace('_', ' ').replace('-', ' ').split())
    return len(page_stem_tokens & sk_tokens)


def _best_candidate(candidates: list, page_fn: str):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    stem = page_fn.replace('.html', '').lower()
    page_tokens = set(stem.replace('-', ' ').replace('_', ' ').split())
    scored = [(c, _score_entity_key(c, page_tokens)) for c in candidates]
    scored.sort(key=lambda x: (-x[1], -len(x[0])))
    return scored[0][0]


def _detect_page_type(filename: str) -> str:
    fn = filename.lower()
    if fn == "index.html" or "homepage" in fn:
        return "homepage"
    elif "-detail-" in fn or "_detail" in fn:
        return "detail"
    elif "-search" in fn or "search" in fn:
        return "search"
    elif "-listing-" in fn or "listing" in fn:
        return "listing"
    elif "-form-" in fn or "form" in fn:
        return "form"
    return ""


def _resolve_site_model_rebuild_decision(
    *,
    stop_after: Optional[str],
    feedback_global_reasoning_path: str,
    explicit_global_reasoning_path: str,
    site_model_path: Optional[str],
) -> Tuple[bool, str, Dict[str, Any]]:
    stop_after_value = str(stop_after or "").strip()
    explicit_site_model_path = bool(str(site_model_path or "").strip())
    implicit_rebuild_reasons: List[str] = []
    if stop_after_value in {"global_reasoning", "instance_splitting", "site_model"}:
        implicit_rebuild_reasons.append(f"stop_after_{stop_after_value}")
    if feedback_global_reasoning_path and not explicit_site_model_path:
        implicit_rebuild_reasons.append("feedback_global_reasoning_without_reusable_site_model")
    if explicit_global_reasoning_path and not explicit_site_model_path:
        implicit_rebuild_reasons.append("explicit_global_reasoning_without_reusable_site_model")

    implicit_should_rebuild = bool(implicit_rebuild_reasons)
    base_reason = implicit_rebuild_reasons[0] if implicit_rebuild_reasons else "reusable_site_model_allowed"
    details = {
        "implicit_should_rebuild_site_model": implicit_should_rebuild,
        "implicit_rebuild_reasons": implicit_rebuild_reasons,
        "stop_after": stop_after_value,
        "explicit_site_model_path": explicit_site_model_path,
        "feedback_global_reasoning_path": feedback_global_reasoning_path,
        "explicit_global_reasoning_path": explicit_global_reasoning_path,
    }
    return implicit_should_rebuild, base_reason, details


def _ensure_site_model_entity_seed(
    site_model: Any,
    output_dir: str | Path,
) -> Dict[str, Any]:
    existing_seed = (
        getattr(site_model, "entity_seed", {})
        if isinstance(getattr(site_model, "entity_seed", {}), dict)
        else {}
    )
    if existing_seed.get("format") == "entity_seed" and existing_seed.get("entities"):
        return {
            "seed": existing_seed,
            "validation": existing_seed.get("validation", {}),
            "materialized": False,
        }

    root = Path(output_dir)
    runtime_state_observations: Dict[str, Any] = {}
    runtime_state_path = root / "phase_instance_splitting" / "runtime_state_observations.json"
    if runtime_state_path.is_file():
        try:
            loaded_runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Failed to read canonical runtime state observations: {runtime_state_path}: {exc}"
            ) from exc
        if not isinstance(loaded_runtime_state, dict):
            raise RuntimeError(
                f"Canonical runtime state observations must be an object: {runtime_state_path}"
            )
        runtime_state_observations = loaded_runtime_state

    content_lifecycle_path = (
        root / "phase_trace_semantic_extraction" / "content_lifecycle_plan.json"
    )
    if content_lifecycle_path.is_file():
        try:
            content_lifecycle_plan = json.loads(
                content_lifecycle_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to read canonical content lifecycle plan: {content_lifecycle_path}: {exc}"
            ) from exc
        if not isinstance(content_lifecycle_plan, dict):
            raise RuntimeError(
                f"Canonical content lifecycle plan must be an object: {content_lifecycle_path}"
            )
        derived = runtime_state_observations.setdefault("derived", {})
        if not isinstance(derived, dict):
            raise RuntimeError(
                "runtime_state_observations.derived must be an object before entity seed materialization"
            )
        derived["content_lifecycle_plan"] = content_lifecycle_plan

    content = getattr(site_model, "content", None)
    entity_schemas = getattr(content, "entity_schemas", []) if content is not None else []
    content_samples = getattr(content, "content_samples", {}) if content is not None else {}
    artifact = materialize_entity_seed(
        entity_schemas=entity_schemas,
        content_samples=dict(content_samples or {}),
        runtime_state_observations=runtime_state_observations,
        output_dir=str(root),
    )
    seed = artifact.get("seed", {})
    if seed.get("format") != "entity_seed" or not seed.get("entities"):
        raise RuntimeError(
            "Entity seed materialization produced no canonical entities; "
            "SQLite state compilation cannot publish an empty world authority"
        )
    site_model.entity_seed = seed
    return {**artifact, "materialized": True}


class Trace2WorldTraceGenerator:

    def __init__(self, output_dir: Optional[str] = None, config_path: Optional[str] = None,
                 stop_after: Optional[str] = None,
                 hybrid_site_model: Optional[bool] = None,
                 site_model_parallel_enabled: Optional[bool] = None,
                 site_model_consistency_mode: Optional[str] = None,
                 site_model_frame_quality_refinement_enabled: Optional[bool] = None,
                 site_model_random_seed: Optional[int] = None,
                 site_model_unknown_url_decision_threshold: Optional[float] = None,
                 site_model_use_url_signals: Optional[bool] = None,
                 site_model_global_reasoning_enabled: Optional[bool] = None,
                 site_model_global_reasoning_overflow_strategy: Optional[str] = None,
                 site_model_global_reasoning_token_budget: Optional[int] = None,
                 site_model_global_reasoning_chunk_size: Optional[int] = None,
                 site_model_global_reasoning_merge_batch_size: Optional[int] = None,
                 site_model_global_reasoning_compact_chunk_retry_attempts: Optional[int] = None,
                 site_model_page_name_soft_limit: Optional[int] = None,
                 site_model_global_reasoning_logic_flow: Optional[str] = None,
                 site_model_global_reasoning_input_path: Optional[str] = None,
                 site_model_schema_boundary_slice_token_budget: Optional[int] = None,
                 site_model_schema_boundary_slice_candidate_budget: Optional[int] = None,
                 site_model_skip_full_extraction_enabled: Optional[bool] = None,
                 site_model_task_interaction_contract_logic_flow: Optional[str] = None,
                 vision_max_width: Optional[int] = None,
                 vision_max_height: Optional[int] = None,
                 vision_jpeg_quality: Optional[int] = None,
                 filtered_resources_path: Optional[str] = None,
                 pagegen_logic_flow: Optional[str] = None,
                 pagegen_cascade_round1_image_limit: Optional[int] = None,
                 pagegen_cascade_max_images_per_round: Optional[int] = None,
                 pagegen_cascade_patching_enabled: Optional[bool] = None,
                 pagegen_cascade_use_unique_steps_enabled: Optional[bool] = None,
                 pagegen_cascade_patch_max_tokens: Optional[int] = None,
                 pagegen_cascade_prompt_mode: Optional[str] = None,
                 platform: Optional[str] = None,
                 viewport: Optional[str] = None):

        self.config_manager = Trace2WorldConfigManager(config_path)
        if output_dir:
            self.config_manager.set("output_dir", output_dir)
        if hybrid_site_model is not None:
            self.config_manager.set("hybrid_site_model", hybrid_site_model)
        if site_model_parallel_enabled is not None:
            self.config_manager.set("site_model_parallel_enabled", site_model_parallel_enabled)
        if site_model_consistency_mode is not None:
            self.config_manager.set("site_model_consistency_mode", site_model_consistency_mode)
        if site_model_frame_quality_refinement_enabled is not None:
            self.config_manager.set("site_model_frame_quality_refinement_enabled", site_model_frame_quality_refinement_enabled)
        if site_model_random_seed is not None:
            self.config_manager.set("site_model_random_seed", site_model_random_seed)
        if site_model_unknown_url_decision_threshold is not None:
            self.config_manager.set(
                "site_model_unknown_url_decision_threshold",
                site_model_unknown_url_decision_threshold
            )
        if site_model_use_url_signals is not None:
            self.config_manager.set("site_model_use_url_signals", site_model_use_url_signals)
        if site_model_global_reasoning_enabled is not None:
            self.config_manager.set("site_model_global_reasoning_enabled", site_model_global_reasoning_enabled)
        if site_model_global_reasoning_overflow_strategy is not None:
            self.config_manager.set(
                "site_model_global_reasoning_overflow_strategy",
                site_model_global_reasoning_overflow_strategy
            )
        if site_model_global_reasoning_token_budget is not None:
            self.config_manager.set(
                "site_model_global_reasoning_token_budget",
                site_model_global_reasoning_token_budget
            )
        if site_model_global_reasoning_chunk_size is not None:
            self.config_manager.set(
                "site_model_global_reasoning_chunk_size",
                site_model_global_reasoning_chunk_size
            )
        if site_model_global_reasoning_merge_batch_size is not None:
            self.config_manager.set(
                "site_model_global_reasoning_merge_batch_size",
                site_model_global_reasoning_merge_batch_size
            )
        if site_model_global_reasoning_compact_chunk_retry_attempts is not None:
            self.config_manager.set(
                "site_model_global_reasoning_compact_chunk_retry_attempts",
                site_model_global_reasoning_compact_chunk_retry_attempts
            )
        if site_model_page_name_soft_limit is not None:
            self.config_manager.set("site_model_page_name_soft_limit", site_model_page_name_soft_limit)
        if site_model_global_reasoning_logic_flow is not None:
            self.config_manager.set(
                "site_model_global_reasoning_logic_flow",
                site_model_global_reasoning_logic_flow
            )
        if site_model_global_reasoning_input_path is not None:
            self.config_manager.set(
                "site_model_global_reasoning_input_path",
                site_model_global_reasoning_input_path
            )
        if site_model_schema_boundary_slice_token_budget is not None:
            self.config_manager.set(
                "site_model_schema_boundary_slice_token_budget",
                site_model_schema_boundary_slice_token_budget
            )
        if site_model_schema_boundary_slice_candidate_budget is not None:
            self.config_manager.set(
                "site_model_schema_boundary_slice_candidate_budget",
                site_model_schema_boundary_slice_candidate_budget
            )
        if site_model_skip_full_extraction_enabled is not None:
            self.config_manager.set(
                "site_model_skip_full_extraction_enabled",
                site_model_skip_full_extraction_enabled
            )
        if site_model_task_interaction_contract_logic_flow is not None:
            self.config_manager.set(
                "site_model_task_interaction_contract_logic_flow",
                site_model_task_interaction_contract_logic_flow,
            )
        if vision_max_width is not None:
            self.config_manager.set("vision_max_width", vision_max_width)
        if vision_max_height is not None:
            self.config_manager.set("vision_max_height", vision_max_height)
        if vision_jpeg_quality is not None:
            self.config_manager.set("vision_jpeg_quality", vision_jpeg_quality)
        if pagegen_logic_flow is not None:
            self.config_manager.set("pagegen_logic_flow", pagegen_logic_flow)
        if pagegen_cascade_round1_image_limit is not None:
            self.config_manager.set("pagegen_cascade_round1_image_limit", pagegen_cascade_round1_image_limit)
        if pagegen_cascade_max_images_per_round is not None:
            self.config_manager.set("pagegen_cascade_max_images_per_round", pagegen_cascade_max_images_per_round)
        if pagegen_cascade_patching_enabled is not None:
            self.config_manager.set("pagegen_cascade_patching_enabled", pagegen_cascade_patching_enabled)
        if pagegen_cascade_use_unique_steps_enabled is not None:
            self.config_manager.set("pagegen_cascade_use_unique_steps_enabled", pagegen_cascade_use_unique_steps_enabled)
        if pagegen_cascade_patch_max_tokens is not None:
            self.config_manager.set("pagegen_cascade_patch_max_tokens", pagegen_cascade_patch_max_tokens)
        if pagegen_cascade_prompt_mode is not None:
            self.config_manager.set("pagegen_cascade_prompt_mode", pagegen_cascade_prompt_mode)
        self._filtered_resources_path = filtered_resources_path or self.config_manager.get("filtered_resources_path", "")
        self._trace_resource_dir = self.config_manager.get("trace_resource_dir", "")
        self.output_dir = self.config_manager.get("output_dir")
        assert_output_artifacts_registered(self.output_dir)
        self.environment_profile = resolve_environment_profile(
            self.config_manager.get("environment_profile", {}),
            cli_platform=platform,
            cli_viewport=viewport,
        )
        self.config_manager.set("resolved_environment_profile", self.environment_profile)


        configure_load_balancing(
            endpoints=self.config_manager.get("endpoints"),
            strategy=self.config_manager.get("load_balance_strategy"),
            deployment=self.config_manager.get("deployment"),
        )
        model_concurrency = self.config_manager.get("model_concurrency", {})
        model_initial_concurrency = self.config_manager.get("model_initial_concurrency", {})
        if isinstance(model_concurrency, dict):
            for model_name, limit in model_concurrency.items():
                initial_limit = (
                    model_initial_concurrency.get(model_name)
                    if isinstance(model_initial_concurrency, dict)
                    else None
                )
                configure_model_concurrency(
                    str(model_name),
                    int(limit),
                    initial_concurrent=int(initial_limit) if initial_limit is not None else None,
                )
        configure_stage_model_routing(self.config_manager.get_stage_model_routing())
        configure_global_concurrency(
            self.config_manager.get("max_concurrent", 18),
            initial_concurrent=self.config_manager.get("initial_concurrent", None),
        )


        logs_dir = os.path.join(self.output_dir, "run_management", "logs")
        self.logger = Trace2WorldLogger(
            output_dir=logs_dir,
            log_level=self.config_manager.get("log_level")
        )
        configure_api_log(os.path.join(logs_dir, "llm_api.log"))
        profile_resolution_path = Path(self.output_dir) / "run_management" / "environment_profile_resolution.json"
        profile_resolution_path.parent.mkdir(parents=True, exist_ok=True)
        profile_resolution_path.write_text(
            json.dumps(self.environment_profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        prompt_inventory = {
            "format": "platform_prompt_inventory",
            "schema_revision": 1,
            "environment_profile_hash": self.environment_profile["profile_hash"],
            "platform": self.environment_profile["platform"],
            "policy_revision": POLICY_REVISION,
            "surfaces": [
                {"surface": "frontend.html_generator.pagegen_generation", "owner": "trace2world_pipeline/page_realizer/html_generator.py", "caller": "TraceToWebGenerator._run_frontend_pipeline", "family": "pagegen_generation", "classification": "parameterized", "evidence": "phase_pagegen/page_inputs/*/actual_pagegen_input_manifest.json"},
                {"surface": "frontend.html_generator.pagegen_fixloop", "owner": "trace2world_pipeline/page_realizer/html_generator.py", "caller": "Trace2WorldHtmlGenerator._audit_or_repair_page_contracts", "family": "pagegen_repair", "classification": "parameterized", "evidence": "phase_pagegen/debug/pagegen_contract_issues"},
                {"surface": "frontend.cascade_pagegen.round1", "owner": "trace2world_pipeline/page_realizer/html_generator.py", "caller": "Trace2WorldHtmlGenerator._generate_single_page_cascade_async", "family": "pagegen_generation", "classification": "parameterized", "evidence": "phase_pagegen/page_inputs/*/actual_pagegen_input_manifest.json"},
                {"surface": "frontend.cascade_pagegen.patch", "owner": "trace2world_pipeline/page_realizer/cascade_pagegen.py", "caller": "generate_page_cascade_async", "family": "pagegen_repair", "classification": "parameterized", "evidence": "phase_pagegen/debug/cascade"},
                {"surface": "site_model.global_reasoning", "owner": "trace2world_pipeline/app_spec/reconcile/", "caller": "TraceToWebGenerator", "classification": "platform_neutral", "reason": "world/schema semantics do not select viewport or shell"},
                {"surface": "site_model.page_environment_spec", "owner": "trace2world_pipeline/app_spec/page_environment_spec.py", "caller": "TraceToWebGenerator", "classification": "platform_neutral", "reason": "state/transition semantics consume compiled render context separately"},
                {"surface": "resources.resource_judge", "owner": "trace2world_pipeline/resources/resource_judge.py", "caller": "run_static_resource_pipeline.py", "family": "resource_analysis", "classification": "parameterized", "evidence": "resource judgment metadata"},
                {"surface": "resources.resource_replacer", "owner": "trace2world_pipeline/page_realizer/pagegen_resource_projection.py", "caller": "TraceToWebGenerator._prepare_static_resources", "classification": "platform_neutral", "reason": "matching consumes resource geometry and does not render platform prose"},
            ],
        }
        prompt_inventory_path = Path(self.output_dir) / "run_management" / "platform_prompt_inventory.json"
        prompt_inventory_path.write_text(
            json.dumps(prompt_inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        compatibility_execution_snapshot = {
            "format": "pipeline_execution_contract_snapshot",
            "schema_revision": 1,
            "pagegen_logic_flow": self.config_manager.get("pagegen_logic_flow"),
            "skip_flags": {
                key: bool(self.config_manager.get(key, False))
                for key in (
                    "skip_task_generation",
                    "skip_enrichment",
                    "skip_domain_enhancement",
                    "skip_data_generation",
                    "skip_instrumentation",
                    "skip_page_design",
                    "skip_layout_design",
                    "skip_page_framework",
                    "skip_react_optimizer",
                    "skip_page_validation",
                    "skip_syntax_fix",
                    "skip_tdd_validation",
                )
            },
            "request_parameters": {
                key: self.config_manager.get(key)
                for key in (
                    "deployment",
                    "max_retries",
                    "retry_delay",
                    "max_tokens",
                    "max_concurrent",
                    "initial_concurrent",
                    "pagegen_standard_max_prompt_screenshots",
                    "pagegen_cascade_round1_image_limit",
                    "pagegen_cascade_max_images_per_round",
                    "pagegen_adaptive_cascade_min_prompt_screenshots",
                    "pagegen_adaptive_cascade_min_resources",
                )
            },
            "platform_is_orthogonal": True,
            "environment_profile_hash": self.environment_profile["profile_hash"],
        }
        execution_snapshot_path = (
            Path(self.output_dir) / "run_management/pipeline_execution_contract_snapshot.json"
        )
        execution_snapshot_path.write_text(
            json.dumps(
                compatibility_execution_snapshot,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )


        self.data_manager = Trace2WorldDataManager(self.output_dir)
        self._active_trace_ids: set[str] = set()
        self._resume_plan_decisions: Dict[str, LogicalArtifactDecision] = {}


        self.trace_loader = TraceLoader()

        site_model_builder_config = self.config_manager.get_component_config("site_model_builder")
        frame_observation_config = self.config_manager.get_component_config("frame_observation")
        task_interaction_contract_planner_config = self.config_manager.get_component_config(
            "task_interaction_contract_planner"
        )
        global_max_concurrent = int(self.config_manager.get("max_concurrent", 16) or 16)
        self.site_model_builder = AppSpecBuilder(
            logger=self.logger,
            model=site_model_builder_config["model"],
            reasoning_effort=site_model_builder_config["reasoning_effort"],
            parallel_enabled=self.config_manager.get("site_model_parallel_enabled", True),
            max_concurrent=global_max_concurrent,
            consistency_mode=self.config_manager.get("site_model_consistency_mode", "deterministic"),
            frame_quality_refinement_enabled=self.config_manager.get("site_model_frame_quality_refinement_enabled", False),
            site_model_random_seed=self.config_manager.get("site_model_random_seed", 42),
            unknown_url_decision_threshold=self.config_manager.get(
                "site_model_unknown_url_decision_threshold",
                0.75
            ),
            use_url_signals=self.config_manager.get("site_model_use_url_signals", False),
            global_reasoning_enabled=self.config_manager.get("site_model_global_reasoning_enabled", True),
            global_reasoning_overflow_strategy=self.config_manager.get(
                "site_model_global_reasoning_overflow_strategy",
                "chunk_reduce"
            ),
            global_reasoning_token_budget=self.config_manager.get(
                "site_model_global_reasoning_token_budget",
                60000
            ),
            global_reasoning_chunk_size=self.config_manager.get(
                "site_model_global_reasoning_chunk_size",
                4
            ),
            global_reasoning_merge_batch_size=self.config_manager.get(
                "site_model_global_reasoning_merge_batch_size",
                4
            ),
            global_reasoning_compact_chunk_retry_attempts=self.config_manager.get(
                "site_model_global_reasoning_compact_chunk_retry_attempts",
                1
            ),
            page_name_soft_limit=self.config_manager.get("site_model_page_name_soft_limit", 256),
            global_reasoning_logic_flow=self.config_manager.get(
                "site_model_global_reasoning_logic_flow",
                "compact_transition_resolver"
            ),
            global_reasoning_input_path=self.config_manager.get(
                "site_model_global_reasoning_input_path",
                ""
            ),
            stop_after_global_reasoning=stop_after == "global_reasoning",
            stop_after_instance_splitting=stop_after == "instance_splitting",
            selected_instance_compile_manifest_path=self.config_manager.get(
                "site_model_selected_instance_compile_manifest_path", ""
            ),
            schema_boundary_slice_token_budget=self.config_manager.get(
                "site_model_schema_boundary_slice_token_budget",
                12000
            ),
            schema_boundary_slice_candidate_budget=self.config_manager.get(
                "site_model_schema_boundary_slice_candidate_budget",
                24
            ),
            skip_full_extraction_enabled=self.config_manager.get(
                "site_model_skip_full_extraction_enabled", False
            ),
            task_interaction_contract_logic_flow=self.config_manager.get(
                "site_model_task_interaction_contract_logic_flow",
                "l0_schema_registry",
            ),
            task_interaction_contract_planner_model=task_interaction_contract_planner_config["model"],
            task_interaction_contract_planner_reasoning_effort=task_interaction_contract_planner_config[
                "reasoning_effort"
            ],
            frame_observation_reasoning_effort=frame_observation_config["reasoning_effort"],
        )


        self.site_model_builder.config_manager = self.config_manager


        if stop_after == "site_model":
            self._init_interface_designer()

        elif stop_after not in {"global_reasoning", "instance_splitting"}:
            self._init_downstream_components()

    def _refresh_l0_schema_registry(self, site_model: AppSpec) -> bool:
        builder = getattr(self, "site_model_builder", None)
        if not builder or not callable(getattr(builder, "_l0_schema_registry_flow_enabled", None)):
            return False
        if not builder._l0_schema_registry_flow_enabled():
            return False
        content = getattr(site_model, "content", None)
        if content is None or not callable(getattr(builder, "_build_l0_schema_registry", None)):
            return False

        output_dir = getattr(self, "output_dir", "")
        if callable(getattr(builder, "_materialize_l0_schema_registry", None)):
            builder._materialize_l0_schema_registry(content, output_dir)
        else:
            content.schema_registry = builder._build_l0_schema_registry(content)
        logger = getattr(self, "logger", None)
        if logger:
            logger.log_info(
                "Refreshed l0 schema_registry "
                f"({content.schema_registry.get('catalog_entity_count', 0)} catalog entities, "
                f"{content.schema_registry.get('runtime_state_count', 0)} runtime states)"
            )
        if callable(getattr(builder, "_canonicalize_l0_task_interaction_state_identity", None)):
            canonicalization = builder._canonicalize_l0_task_interaction_state_identity(site_model)
            if output_dir:
                from trace2world_pipeline.core.phase_output import PhaseOutputManager
                _pom = PhaseOutputManager(output_dir)
                _pom.write_json("consolidation", "debug", "task_interaction_state_identity_canonicalization.json", canonicalization)
                if canonicalization.get("created_runtime_state_count", 0) and callable(getattr(builder, "_save_schema_registry_artifact", None)):
                    builder._save_schema_registry_artifact(content.schema_registry, output_dir)
            if logger:
                logger.log_info(
                    "Canonicalized l0 task interaction state identity "
                    f"({canonicalization.get('canonicalized_count', 0)} filled, "
                    f"{canonicalization.get('created_runtime_state_count', 0)} runtime states created, "
                    f"{canonicalization.get('unresolved_count', 0)} unresolved)"
                )
        if callable(getattr(builder, "_validate_task_interaction_contracts", None)):
            task_contracts = getattr(site_model, "task_interaction_contracts", {})
            contracts = task_contracts.get("contracts", []) if isinstance(task_contracts, dict) else []
            validation = builder._validate_task_interaction_contracts(contracts, content)
            output_dir = getattr(self, "output_dir", "")
            if output_dir:
                from trace2world_pipeline.core.phase_output import PhaseOutputManager
                _pom = PhaseOutputManager(output_dir)
                _pom.write_json("consolidation", "debug", "task_interaction_contract_validation.json", validation)
            if not validation.get("valid", False):
                raise RuntimeError(
                    "task_interaction_contract validation failed with {} error(s)".format(
                        validation.get("error_count", 0)
                    )
                )
        return True

    def _init_interface_designer(self):
        interface_config = self.config_manager.get_component_config("interface_designer")
        self.interface_designer = Trace2WorldInterfaceDesigner(
            self.logger,
            model=interface_config["model"],
            reasoning_effort=interface_config["reasoning_effort"],
        )

    def _init_downstream_components(self):
        task_gen_config = self.config_manager.get_component_config("task_generator")
        self.task_generator = Trace2WorldTaskGenerator(
            self.logger,
            model=task_gen_config["model"],
            reasoning_effort=task_gen_config["reasoning_effort"],
            min_steps=task_gen_config.get("min_steps", 6),
            max_steps=task_gen_config.get("max_steps", 10)
        )

        self._init_interface_designer()

        parallel_config = self.config_manager.get_component_config("parallel_generator")
        self.parallel_generator = Trace2WorldSdkGenerator(
            self.logger,
            model=parallel_config["model"],
            reasoning_effort=parallel_config["reasoning_effort"],
            max_concurrent=self.config_manager.get("max_concurrent", 64),
        )

        page_gen_config = self.config_manager.get_component_config("page_generator")
        self.page_generator = Trace2WorldHtmlGenerator(
            self.logger,
            max_concurrent=self.config_manager.get("max_concurrent", 3),
            model=page_gen_config["model"],
            reasoning_effort=page_gen_config["reasoning_effort"],
            max_prompt_screenshots=self.config_manager.get("pagegen_standard_max_prompt_screenshots", 16),
            page_fixloop_enabled=bool(self.config_manager.get("pagegen_fixloop_enabled", False)),
            page_fix_max_rounds=int(self.config_manager.get("pagegen_max_repair_rounds", 0) or 0),
            environment_profile=self.environment_profile,
        )

        enricher_config = self.config_manager.get_component_config("schema_enricher")
        self.schema_enricher = SchemaEnricher(
            self.logger,
            model=enricher_config["model"],
            reasoning_effort=enricher_config["reasoning_effort"],
        )

        resource_config = self.config_manager.get_component_config("resource_replacer")
        self.resource_replacer = Trace2WorldResourceReplacer(
            self.logger,
            image_mode=self.config_manager.get("image_mode"),
            output_dir=self.config_manager.get("output_dir", "results/generated"),
            model=resource_config["model"],
            reasoning_effort=resource_config["reasoning_effort"],
        )

    def generate(self, trace_dirs: List[str] = None,
                 task_metadata_path: str = None,
                 website_filter: str = None,
                 site_model_path: str = None,
                 stop_after: str = None) -> Trace2WorldGenerationResult:


        start_time = time.time()
        self.config_manager.print_summary()

        resume_dir = self.output_dir
        self._resume_dir = resume_dir

        self.logger.log_info("=" * 60)
        self.logger.log_info("Starting Trace-Based TDD Generation")
        self.logger.log_info("=" * 60)

        try:


            self.site_model_builder.stop_after_global_reasoning = stop_after == "global_reasoning"
            self.site_model_builder.stop_after_instance_splitting = stop_after == "instance_splitting"
            feedback_global_reasoning_path = str(
                self.config_manager.get("site_model_global_reasoning_input_path", "") or ""
            ).strip()
            if feedback_global_reasoning_path:
                self.site_model_builder.global_reasoning_input_path = feedback_global_reasoning_path
                self.logger.log_info(
                    f"Replaying global reasoning from {feedback_global_reasoning_path}"
                )

            effective_site_model_path = site_model_path
            explicit_global_reasoning_path = str(
                self.config_manager.get("site_model_global_reasoning_input_path", "") or ""
            ).strip()
            (
                should_rebuild_site_model,
                site_model_rebuild_reason,
                site_model_rebuild_details,
            ) = _resolve_site_model_rebuild_decision(
                stop_after=stop_after,
                feedback_global_reasoning_path=feedback_global_reasoning_path,
                explicit_global_reasoning_path=explicit_global_reasoning_path,
                site_model_path=site_model_path,
            )
            if not effective_site_model_path and resume_dir and not should_rebuild_site_model:
                from trace2world_pipeline.core.phase_output import PhaseOutputManager
                _resume_pom = PhaseOutputManager(resume_dir)
                candidate = str(_resume_pom.site_model_dir())
                if os.path.exists(candidate):
                    effective_site_model_path = candidate
            elif should_rebuild_site_model and resume_dir and self.logger:
                self.logger.log_info(
                    "Rebuilding AppSpec because "
                    f"{site_model_rebuild_reason}; "
                    "existing debug artifacts may still be reused by the AppSpecBuilder"
                )

            site_model_cache_exists = bool(
                effective_site_model_path and os.path.exists(effective_site_model_path)
            )
            site_model_manifest_path = (
                os.path.join(effective_site_model_path, "manifest.json")
                if effective_site_model_path
                else ""
            )
            site_model_cache_complete = bool(
                site_model_cache_exists
                and site_model_manifest_path
                and os.path.exists(site_model_manifest_path)
            )
            site_model_cache_reason = (
                "complete_site_model_manifest_found"
                if site_model_cache_complete
                else "site_model_manifest_missing"
                if site_model_cache_exists
                else "missing_cached_site_model"
            )
            self._record_resume_plan_decision(
                logical_artifact="site_model",
                source="reused_disk" if site_model_cache_complete else "recomputed",
                phase="PIPE-P1 Build AppSpec",
                artifact_path=effective_site_model_path or "",
                canonical_semantic_source=True,
                canonical_prompt_boundary=False,
                consumers=("site_model_semantic_design", "pagegen", "runtime_backend", "validator"),
                reason=(
                    "prebuilt_site_model_selected"
                    if site_model_cache_complete
                    else site_model_rebuild_reason
                    if should_rebuild_site_model
                    else site_model_cache_reason
                ),
                details={
                    "explicit_site_model_path": bool(site_model_path),
                    "should_rebuild_site_model": should_rebuild_site_model,
                    "site_model_rebuild_reason": site_model_rebuild_reason,
                    "site_model_cache_complete": site_model_cache_complete,
                    "site_model_cache_reason": site_model_cache_reason,
                    "feedback_global_reasoning_path": feedback_global_reasoning_path,
                    "explicit_global_reasoning_path": explicit_global_reasoning_path,
                    **site_model_rebuild_details,
                },
            )

            if effective_site_model_path and os.path.exists(effective_site_model_path):


                finalizer_marker = os.path.join(effective_site_model_path, "manifest.json")
                if not os.path.exists(finalizer_marker):
                    raise ValueError(
                        f"--site-model points to an incomplete AppSpec directory: "
                        f"{effective_site_model_path}. Missing AppSpec "
                        f"manifest.json. Either delete the directory "
                        f"to rebuild from scratch, or rerun the original pipeline "
                        f"to Pass 2 completion before resuming."
                    )
                self.logger.log_info(f"Loading pre-built AppSpec from {effective_site_model_path}")
                site_model = AppSpec.load(effective_site_model_path)
                website_type = site_model.website_type
            else:
                site_model, website_type = self._build_site_model(
                    trace_dirs, task_metadata_path, website_filter
                )
            if not self._active_trace_ids:
                self._active_trace_ids = self._discover_active_trace_ids_from_config()

            self._refresh_l0_schema_registry(site_model)


            existing_profile = (
                site_model.environment_profile
                if isinstance(site_model.environment_profile, dict)
                else {}
            )
            if existing_profile:
                existing_profile = normalize_environment_profile(existing_profile)
                if existing_profile["profile_hash"] != self.environment_profile["profile_hash"]:
                    raise ValueError(
                        "Resolved environment profile conflicts with the prebuilt AppSpec: "
                        f"resolved={self.environment_profile['profile_hash']} "
                        f"site_model={existing_profile['profile_hash']}"
                    )


                site_model.environment_profile = dict(self.environment_profile)
            else:
                if site_model_cache_exists and self.environment_profile.get("platform") == "desktop":
                    raise ValueError(
                        "A prebuilt AppSpec without environment_profile.json cannot be resumed "
                        "as desktop. Rebuild the AppSpec with --platform desktop so platform "
                        "authority is materialized upstream."
                    )
                site_model.environment_profile = dict(self.environment_profile)

            inferred_device_profile = site_model.infer_device_profile()
            site_model.metadata["environment_profile_diagnostics"] = {
                "screenshot_inference": inferred_device_profile,
                "authoritative_profile_hash": self.environment_profile["profile_hash"],
                "inference_is_authoritative": False,
            }
            site_model.visual_style["device_context"] = device_context_mirror(
                site_model.environment_profile
            )
            self.logger.log_info(
                "Environment profile: "
                f"platform={self.environment_profile['platform']} "
                f"viewport={self.environment_profile['viewport']['width']}x"
                f"{self.environment_profile['viewport']['height']} "
                f"source={self.environment_profile['resolution_source']} "
                f"hash={self.environment_profile['profile_hash']}"
            )


            self.data_manager.set_site_model(site_model)

            self.logger.log_info(
                f"AppSpec ready: {len(site_model.structure.page_prototypes)} pages, "
                f"{len(site_model.content.entity_schemas)} entities"
            )


            source_inventory_profile = (
                str(self.config_manager.get("pagegen_logic_flow", "standard") or "standard")
                == "l0_fidelity"
            )
            skip_task_generation = bool(
                self.config_manager.get("skip_task_generation", source_inventory_profile)
            )
            if source_inventory_profile and not skip_task_generation:
                raise ValueError(
                    "L0 visible structure inventory requires source reference tasks; "
                    "set skip_task_generation=true instead of generating unobserved functionality"
                )
            resumed_tasks = self._load_resume_tasks(resume_dir)
            if source_inventory_profile and resumed_tasks:
                ungrounded = [
                    str(task.get("id") or task.get("task_id") or "<unknown>")
                    for task in resumed_tasks.get("tasks", [])
                    if not isinstance(task, dict) or task.get("evidence_based") is not True
                ]
                if ungrounded:
                    raise ValueError(
                        "L0 resumed construction tasks lack source-evidence admission: "
                        + ", ".join(ungrounded[:10])
                    )
            if resumed_tasks:
                tasks, task_filter_report = self._filter_resume_tasks_to_active_traces(
                    resumed_tasks.get("tasks", [])
                )
                if task_filter_report.get("filtered"):
                    self._save_stage_debug("resume_task_trace_filter", task_filter_report, phase="interface_design")
                    self.logger.log_info(
                        "Filtered resumed tasks to current trace set: "
                        f"{task_filter_report.get('original_task_count')} -> "
                        f"{task_filter_report.get('kept_task_count')}; removed "
                        f"{task_filter_report.get('removed_task_ids', [])}"
                    )
                website_type = resumed_tasks.get("website_type", website_type)
                self.data_manager.set_tasks(tasks, website_type)
                self.logger.log_info(f"Resumed {len(tasks)} tasks from {resume_dir}")
            elif skip_task_generation:

                ref_tasks = site_model.metadata.get("reference_tasks", [])
                tasks = []
                for i, rt in enumerate(ref_tasks):
                    tasks.append({
                        "id": rt.get("id", f"task_{i+1}"),
                        "name": rt.get("name", f"Task {i+1}"),
                        "description": rt.get("name", ""),
                        "evidence_based": True,
                        "steps": rt.get("steps", [])
                    })
                if source_inventory_profile and not tasks:
                    raise ValueError("L0 source-frozen construction has no admitted reference tasks")
                self.data_manager.set_tasks(tasks, website_type)
                self.logger.log_info(f"Skipped task generation — using {len(tasks)} reference tasks from AppSpec")
            else:
                self.logger.log_step_start("Generate Tasks", "PREPARE")
                self.logger.start_stage("Generate Tasks", "prepare")

                task_count_range = self.config_manager.get("task_count_range", "8-10")
                tasks = self.task_generator.generate_tasks(
                    website_type,
                    task_count_range,
                    site_model=site_model
                )
                self.data_manager.set_tasks(tasks, website_type)

                self.logger.log_info(f"Generated {len(tasks)} tasks")
                self.logger.end_stage("Generate Tasks")
                self.logger.log_step_end("Generate Tasks", "PREPARE")


            self._save_stage_debug("tasks", {"tasks": tasks, "website_type": website_type}, phase="interface_design")

            self._check_stop_gate("tasks", stop_after)


            skip_enrichment = bool(
                self.config_manager.get("skip_enrichment", source_inventory_profile)
            )
            if source_inventory_profile and not skip_enrichment:
                raise ValueError(
                    "L0 visible structure inventory cannot consume task-inferred AppSpec enrichment; "
                    "set skip_enrichment=true"
                )

            if not skip_enrichment:
                skip_tasks = self.config_manager.get("skip_task_generation", False)
                skip_data = self.config_manager.get("skip_data_generation", False)
                if skip_tasks and skip_data:
                    raise ValueError(
                        "skip_enrichment=False but skip_task_generation=True and "
                        "skip_data_generation=True. Enrichment requires tasks/data "
                        "generation — either enable them or set skip_enrichment=True."
                    )

            if skip_enrichment:
                self.logger.log_info("Skipped AppSpec enrichment (skip_enrichment=True)")
            else:
                self.logger.log_step_start("Enrich AppSpec", "PREPARE")
                self.logger.start_stage("Enrich AppSpec", "prepare")

                from trace2world_pipeline.task_generation.app_spec_enricher import AppSpecEnricher
                enricher_config = self.config_manager.get_component_config("schema_enricher")
                app_spec_enricher = AppSpecEnricher(
                    schema_enricher=self.schema_enricher,
                    logger=self.logger,
                    model=enricher_config["model"],
                    reasoning_effort=enricher_config["reasoning_effort"],
                )

                site_model = asyncio.run(
                    app_spec_enricher.enrich(site_model, tasks, website_type)
                )

                self.logger.log_info(
                    f"Enriched AppSpec: {len(site_model.structure.page_prototypes)} pages, "
                    f"{len(site_model.content.entity_schemas)} entities, "
                    f"{len(site_model.structure.site_graph.edges)} edges"
                )

                self.logger.end_stage("Enrich AppSpec")
                self.logger.log_step_end("Enrich AppSpec", "PREPARE")

            self.data_manager.set_site_model(site_model)


            reuse_completed_semantic_design = bool(
                self.config_manager.get("reuse_completed_semantic_design", False)
            )
            if reuse_completed_semantic_design:
                semantic_design = load_completed_semantic_design_for_downstream_resume(
                    self.output_dir,
                    require_environment_state_artifacts=(
                        str(self.config_manager.get("runtime_profile", "") or "").strip()
                        == "state_kernel_sqlite"
                    ),
                )
                setattr(site_model, "semantic_design", semantic_design)
                self.logger.log_info(
                    "Reused completed semantic design for downstream-only rerun; "
                    "GR, instance splitting, PageEnv 06/07, and existing 08 contracts "
                    "remain producer-preserved"
                )
            else:
                semantic_design = asyncio.run(
                    self._prepare_site_model_semantic_design(
                        site_model=site_model,
                        logger=self.logger,
                    )
                )
                self.logger.log_info(
                    "AppSpec semantic design canonical artifacts materialized"
                )


            from trace2world_pipeline.core.phase_output import PhaseOutputManager as _POM
            interfaces_resume_path = _POM(resume_dir).debug("interface_design", "interfaces.json")
            resumed_interfaces = None
            resumed_interfaces_source = str(interfaces_resume_path)
            canonical_interfaces_path = (
                Path(self.output_dir) / "phase_consolidation" / "site_model" / "interfaces.json"
            )
            if reuse_completed_semantic_design:
                resumed_interfaces = json.loads(
                    canonical_interfaces_path.read_text(encoding="utf-8")
                )
                resumed_interfaces_source = str(canonical_interfaces_path)
            if resumed_interfaces is None and interfaces_resume_path.exists():
                with interfaces_resume_path.open("r", encoding="utf-8") as f:
                    resumed_interfaces = json.load(f)
                self.logger.log_info(f"Loaded resume artifact: {interfaces_resume_path}")
            if resumed_interfaces:
                interfaces = resumed_interfaces
                self.data_manager.set_interfaces(interfaces)
                self.logger.log_info(
                    f"Resumed {len(interfaces.get('interfaces', []))} interfaces from {resumed_interfaces_source}"
                )
            else:
                interfaces = asyncio.run(
                    self._design_app_spec_interfaces(
                        tasks=tasks,
                        website_type=website_type,
                        site_model=site_model,
                        semantic_design=semantic_design,
                        logger=self.logger,
                    )
                )


            if not reuse_completed_semantic_design:
                from trace2world_pipeline.core.phase_output import PhaseOutputManager
                _pom = PhaseOutputManager(self.output_dir)
                _pom.write_json("consolidation", "formal", "site_model/interfaces.json", interfaces)

            self._check_stop_gate("site_model", stop_after)


            self.logger.log_info("Starting deterministic execution pipeline...")
            frontend_logger = Trace2WorldLogger(
                output_dir=os.path.join(self.output_dir, "run_management", "logs"),
                log_level=self.logger.log_level.value
            )
            frontend_logger.timing_file = self.logger.timing_file

            frontend_result = asyncio.run(
                self._run_frontend_pipeline(tasks, interfaces, website_type,
                                            frontend_logger, site_model,
                                            semantic_design=semantic_design)
            )


            skip_instrumentation = self.config_manager.get("skip_instrumentation", False)
            instrumentation_generation_result_contract: Optional[Dict[str, Any]] = None
            if skip_instrumentation:
                self.logger.log_info("Skipping instrumentation (skip_instrumentation=True)")
            else:
                self.logger.log_step_start("Instrumentation", "frontend")
                self.logger.start_stage("Instrumentation", "frontend")

                try:
                    instrumentation_config = _merged_component_config(self.config_manager, "instrumentation")
                    strict_instrumentation_validation = instrumentation_config.get("strict_validation", True)
                    if _instrumentation_artifacts_only_config(instrumentation_config):
                        terminal_contract_root = (
                            instrumentation_config.get("terminal_contract_root")
                            or self.config_manager.get("terminal_contract_root")
                        )
                        if not terminal_contract_root:
                            raise RuntimeError(
                                "instrumentation.output_scope=instrumentation_only requires terminal_contract_root"
                            )
                        from trace2world_pipeline.verification.contract_instrumentation import run_contract_instrumentation

                        contract_result = run_contract_instrumentation(
                            self.output_dir,
                            source_env_dir=instrumentation_config.get("source_env_dir") or self.output_dir,
                            terminal_contract_root=terminal_contract_root,
                            clear_existing=bool(
                                instrumentation_config.get(
                                    "clear_existing_instrumentation",
                                    self.config_manager.get("clear_existing_instrumentation", True),
                                )
                            ),
                            semantic_checkpoint_planner=instrumentation_config.get("semantic_checkpoint_planner"),
                            ignore_semantic_obligation_matrix=bool(
                                instrumentation_config.get("ignore_semantic_obligation_matrix", False)
                            ),
                            generate_evaluators=True,
                            generate_test_logic=False,
                            generate_instrumented_business_logic=(
                                str(self.config_manager.get("runtime_profile", "") or "").strip()
                                != "state_kernel_sqlite"
                            ),
                        )
                        from trace2world_pipeline.verification.executable_evaluator import compile_executable_evaluator

                        executable_manifest = compile_executable_evaluator(Path(self.output_dir))
                        self.logger.log_info(
                            "Generated executable evaluator manifest: "
                            f"traces={len(executable_manifest.get('evaluators') or {})}"
                        )
                        self.logger.log_info(
                            "Generated instrumentation artifacts only: "
                            f"checkpoints={contract_result.checkpoint_count}, "
                            f"test_flows={contract_result.test_flow_count}, "
                            f"evaluators={contract_result.evaluator_count}"
                        )
                        generation_result_path = (
                            Path(self.output_dir)
                            / "phase_post_processing"
                            / "debug"
                            / "data_manager"
                            / "generation_result.json"
                        )
                        if generation_result_path.is_file():
                            try:
                                with generation_result_path.open(encoding="utf-8") as f:
                                    loaded_generation_result = json.load(f)
                                if isinstance(loaded_generation_result, dict):
                                    instrumentation_generation_result_contract = loaded_generation_result
                            except Exception as exc:
                                self.logger.log_warning(
                                    f"Failed to preserve instrumentation generation_result metadata: {exc}"
                                )
                        evaluators = []
                    else:
                        post_config = {
                            "reasoning_effort": instrumentation_config["reasoning_effort"],
                            "model": instrumentation_config["model"],
                            "strict_validation": strict_instrumentation_validation,
                            "task_rewriting": {"include_full_data": True}
                        }
                        processor = Trace2WorldInstrumentationPostProcessor(self.output_dir, post_config)
                        post_result = asyncio.run(processor.process())

                        if post_result["success"]:
                            evaluators = post_result.get("evaluators", [])
                        else:
                            if strict_instrumentation_validation:
                                raise RuntimeError(post_result.get("error") or "Instrumentation post-processing failed")
                            self.logger.log_warning(f"Instrumentation post-processing issue (non-blocking due to strict_validation=False): {post_result.get('error')}")
                            evaluators = []
                    self.data_manager.evaluators = evaluators
                except Exception as e:
                    if _merged_component_config(self.config_manager, "instrumentation").get("strict_validation", True):
                        raise
                    self.logger.log_warning(f"Instrumentation failed (non-blocking): {e}")

                self.logger.end_stage("Instrumentation")
                self.logger.log_step_end("Instrumentation", "frontend")


            generation_time = time.time() - start_time
            result = self.data_manager.create_generation_result(
                success=True,
                generation_time=generation_time,
                iterations_used=0
            )
            if instrumentation_generation_result_contract:
                if _merge_instrumentation_generation_result_metadata(
                    self.output_dir,
                    instrumentation_generation_result_contract,
                ):
                    self.logger.log_info(
                        "Preserved instrumentation-only generation_result contract metadata"
                    )

            self.logger.save_summary()
            self.data_manager.print_summary()


            try:
                tracker = get_token_tracker()
                tracker.save_report(self.output_dir, "token_usage_report")
            except Exception as e:
                self.logger.log_warning(f"Failed to save token report: {e}")
            try:
                self._organize_run_level_artifacts(self.logger)
                artifact_index_path = rebuild_artifact_index(Path(self.output_dir))
                self.logger.log_info(f"Updated artifact index after run-level finalization: {artifact_index_path}")
            except Exception as e:
                self.logger.log_warning(f"Failed to organize run-level artifacts: {e}")

            self.logger.log_info(f"Generation complete in {generation_time:.2f}s")
            return result

        except _StopAfterReached as e:
            generation_time = time.time() - start_time
            self.logger.log_info(f"Stopped after '{e}' stage in {generation_time:.2f}s")
            self.logger.save_summary()
            return self.data_manager.create_generation_result(
                success=True,
                generation_time=generation_time,
                iterations_used=0
            )

        except Exception as e:
            self.logger.log_exception(e, "Trace-Based Generation")
            result = self.data_manager.create_generation_result(
                success=False,
                generation_time=time.time() - start_time,
                iterations_used=0
            )
            result.test_results = {"error": str(e)}
            self.data_manager.save_debug_data("debug_error_state.json")
            return result

    def _check_stop_gate(self, current_stage: str, stop_after: Optional[str]):
        if stop_after and current_stage == stop_after:
            raise _StopAfterReached(current_stage)

    def _save_stage_debug(self, stage_name: str, data: Any, *, phase: str = ""):
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        pom = PhaseOutputManager(self.output_dir)
        effective_phase = phase or stage_name
        try:

            if hasattr(data, 'to_dict'):
                serializable = data.to_dict()
            elif hasattr(data, '__dataclass_fields__'):
                from dataclasses import asdict as _asdict
                serializable = _asdict(data)
            elif hasattr(data, '__dict__') and not isinstance(data, (dict, list)):
                serializable = data.__dict__
            else:
                serializable = data
            filepath = pom.write_json(effective_phase, "debug", f"{stage_name}.json", serializable)
            self.logger.log_info(f"Debug data saved: {filepath}")
        except Exception as e:
            self.logger.log_warning(f"Failed to save debug data for {stage_name}: {e}")

    def _resume_plan_path(self) -> str:
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        pom = PhaseOutputManager(self.output_dir)
        return str(pom.debug("trace_preparation", "resume_plan.json"))

    def _record_resume_plan_decision(
        self,
        *,
        logical_artifact: str,
        source: str,
        phase: str,
        canonical_semantic_source: bool,
        canonical_prompt_boundary: bool,
        consumers: Tuple[str, ...] = (),
        reason: str = "",
        artifact_path: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not logical_artifact:
            return
        decision = LogicalArtifactDecision(
            logical_artifact=logical_artifact,
            source=source,
            phase=phase,
            artifact_path=artifact_path,
            canonical_semantic_source=canonical_semantic_source,
            canonical_prompt_boundary=canonical_prompt_boundary,
            consumers=consumers,
            reason=reason,
            details=details or {},
        )
        self._resume_plan_decisions[logical_artifact] = decision
        plan = RunContext.from_decisions(self._resume_plan_decisions.values()).to_resume_plan()
        plan["output_dir"] = self.output_dir
        plan["environment_profile_hash"] = self.environment_profile["profile_hash"]
        plan["platform"] = self.environment_profile["platform"]
        plan["note"] = (
            "Metadata-only run context: records observed resume/rebuild decisions "
            "but does not control execution order."
        )
        path = self._resume_plan_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        compatibility_path = Path(self.output_dir) / "run_management/resume_plan.json"
        compatibility_path.parent.mkdir(parents=True, exist_ok=True)
        compatibility_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


    def _resolve_global_reasoning_page_assignment_audited_path(self) -> str:
        output_dir = str(getattr(self, "output_dir", "") or "").strip()
        if not output_dir:
            return ""
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        pom = PhaseOutputManager(output_dir)
        path = pom.formal("global_reasoning", GLOBAL_REASONING_PAGE_ASSIGNMENT_AUDITED_FILENAME)
        if path.exists():
            return str(path)
        return ""

    def _global_reasoning_page_assignment_audit_auto_accept_enabled(self) -> bool:
        raw = self.config_manager.get("global_reasoning_page_assignment_audit_auto_accept", False)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(raw)

    @staticmethod
    def _find_available_port(start_port: int = 9101, host: str = "127.0.0.1", limit: int = 200) -> int:
        for port in range(int(start_port), int(start_port) + int(limit)):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind((host, port))
                except OSError:
                    continue
                return port
        raise RuntimeError(f"No available audit port found from {start_port} to {start_port + limit - 1}")

    @staticmethod
    def _wait_for_local_port(host: str, port: int, timeout_s: float = 5.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with socket.create_connection((host, int(port)), timeout=0.4):
                    return True
            except OSError:
                time.sleep(0.2)
        return False


    def _load_resume_artifact(self, resume_dir: str, relative_path: str) -> Optional[Any]:
        filepath = os.path.join(resume_dir, relative_path)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.logger.log_info(f"Loaded resume artifact: {filepath}")
            return data
        return None

    def _load_resume_tasks(self, resume_dir: str) -> Optional[Any]:
        if not resume_dir:
            return None
        path = first_existing(task_file_candidates(resume_dir, prefer_rewritten=False))
        if path is None:
            return None
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.logger.log_info(f"Loaded resume tasks: {path}")
        return data

    @staticmethod
    def _task_trace_id(task: Any) -> str:
        if not isinstance(task, dict):
            return ""
        return str(
            task.get("trace_id")
            or task.get("id")
            or task.get("task_id")
            or task.get("reference_trace_id")
            or ""
        ).strip()

    def _filter_resume_tasks_to_active_traces(self, tasks: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        task_list = [task for task in (tasks or []) if isinstance(task, dict)]
        allowed = set(getattr(self, "_active_trace_ids", set()) or set())
        if not allowed:
            return task_list, {"filtered": False, "original_task_count": len(task_list), "kept_task_count": len(task_list)}
        kept = []
        removed = []
        for task in task_list:
            trace_id = self._task_trace_id(task)
            if trace_id and trace_id not in allowed:
                removed.append(trace_id)
                continue
            kept.append(task)
        return kept, {
            "filtered": len(kept) != len(task_list),
            "allowed_trace_count": len(allowed),
            "original_task_count": len(task_list),
            "kept_task_count": len(kept),
            "removed_task_ids": sorted(set(removed)),
        }

    def _discover_active_trace_ids_from_config(self) -> set[str]:
        trace_input_dir = str(self.config_manager.get("trace_input_dir", "") or "").strip()
        if not trace_input_dir:
            return set()
        trace_dirs = self.trace_loader.discover_trace_dirs(trace_input_dir)
        return {
            os.path.basename(os.path.normpath(path))
            for path in trace_dirs
            if os.path.exists(os.path.join(path, "traj.json"))
        }

    def _load_resume_text_artifact(self, resume_dir: str, relative_path: str) -> Optional[str]:
        filepath = os.path.join(resume_dir, relative_path)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            if content.strip():
                self.logger.log_info(f"Loaded resume text artifact: {filepath} ({len(content)} chars)")
                return content
        return None

    def _move_generated_artifact(self, src: str, dst: str, *, reason: str) -> str:
        if not src or not os.path.exists(src):
            return ""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.abspath(src) == os.path.abspath(dst):
            return dst
        if os.path.exists(dst):
            _move_to_deprecated(dst, reason=reason)
        shutil.move(src, dst)
        return dst

    @staticmethod
    def _rewrite_page_paths_for_frontend_pages(html: str) -> str:
        if not html:
            return html

        html = re.sub(r'((?:src|href)=["\'])images/', r'\1../images/', html)
        html = re.sub(r'((?:src|href)=["\'])\./images/', r'\1../images/', html)
        html = re.sub(r'(?P<quote>["\'])images/', r'\g<quote>../images/', html)
        html = re.sub(r'(?P<quote>["\'])\./images/', r'\g<quote>../images/', html)
        for js_file in ("environment_state_client", "business_logic", "data_layer", "seed_data", "route_manifest", "route_runtime"):
            html = re.sub(
                r'((?:src|href)=["\'])(?:\./)?(?:\.\./\.\./phase_sdk_generation/runtime/)?' + js_file + r'\.js(?P<query>\?[^"\']*)?(["\'])',
                r'\1../../phase_sdk_generation/runtime/' + js_file + r'.js\g<query>\3',
                html,
            )
        html = html.replace("url('images/", "url('../images/")
        html = html.replace('url("images/', 'url("../images/')
        html = html.replace("url(images/", "url(../images/")
        return html

    @staticmethod
    def _normalize_state_kernel_runtime_script_order(html: str) -> str:
        if not html:
            return html
        runtime_names = "(?:seed_data|data_layer|business_logic)"
        pattern = re.compile(
            r"(?P<create>\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*"
            r"document\.createElement\(\s*['\"]script['\"]\s*\)\s*;\s*)"
            r"(?P<src>(?P=var)\.src\s*=\s*['\"][^'\"]*"
            + runtime_names
            + r"\.js[^'\"]*['\"]\s*;)"
        )

        def _ordered(match: re.Match[str]) -> str:
            create = match.group("create")
            variable = match.group("var")
            if re.search(rf"\b{re.escape(variable)}\.async\s*=", create):
                return match.group(0)
            return f"{create}{variable}.async = false;\n        {match.group('src')}"

        return pattern.sub(_ordered, html)

    @staticmethod
    def _rewrite_css_paths_for_frontend_pages(css: str) -> str:
        if not css:
            return css
        css = css.replace("url('images/", "url('../images/")
        css = css.replace('url("images/', 'url("../images/')
        css = css.replace("url(images/", "url(../images/")
        return css

    def _organize_final_result_layout(self, logger: Trace2WorldLogger) -> None:
        pages_dir = self._frontend_pages_dir()
        runtime_dir = self._backend_runtime_dir()
        projection_dir = self._runtime_projection_dir()
        test_flow_dir = self._validation_test_flows_dir()
        moved_pages = 0
        os.makedirs(pages_dir, exist_ok=True)
        for name in list(os.listdir(self.output_dir)):
            src = os.path.join(self.output_dir, name)
            if not os.path.isfile(src):
                continue
            if name.endswith(".html"):
                with open(src, "r", encoding="utf-8") as handle:
                    html = self._rewrite_page_paths_for_frontend_pages(handle.read())
                with open(src, "w", encoding="utf-8") as handle:
                    handle.write(html)
                self._move_generated_artifact(src, os.path.join(pages_dir, name), reason="stage_organized_frontend_pages")
                moved_pages += 1
            elif name.endswith(".css"):
                with open(src, "r", encoding="utf-8") as handle:
                    css = self._rewrite_css_paths_for_frontend_pages(handle.read())
                with open(src, "w", encoding="utf-8") as handle:
                    handle.write(css)
                self._move_generated_artifact(src, os.path.join(pages_dir, name), reason="stage_organized_frontend_pages")
            elif name == "business_logic.js":
                self._move_generated_artifact(src, os.path.join(runtime_dir, name), reason="stage_organized_backend_runtime")
            elif name == "test_flows.js":
                self._move_generated_artifact(src, os.path.join(test_flow_dir, name), reason="stage_organized_validation_test_flows")
            elif name == "seed_data.json":
                self._move_generated_artifact(src, os.path.join(projection_dir, name), reason="stage_organized_runtime_projection")
        if moved_pages:
            logger.log_info(f"Organized {moved_pages} root HTML artifact(s) under phase_pagegen/pages/")

    def _frontend_pages_dir(self) -> str:
        return str(PhaseOutputManager(self.output_dir).formal("pagegen", "pages"))

    def _backend_runtime_dir(self) -> str:
        return str(PhaseOutputManager(self.output_dir).formal("sdk_generation", "runtime"))

    def _validation_test_flows_dir(self) -> str:
        return str(PhaseOutputManager(self.output_dir).formal("sdk_generation", "validation", "test_flows"))

    def _runtime_projection_dir(self) -> str:
        return str(PhaseOutputManager(self.output_dir).formal("sdk_generation", "projections", "runtime"))

    def _frontend_page_path(self, filename: str) -> str:
        return os.path.join(self._frontend_pages_dir(), filename)

    def _frontend_css_path(self, filename: str) -> str:
        return os.path.join(self._frontend_pages_dir(), filename.replace(".html", ".css"))

    def _runtime_business_logic_path(self) -> str:
        return os.path.join(self._backend_runtime_dir(), "business_logic.js")

    def _test_flows_path(self) -> str:
        return os.path.join(self._validation_test_flows_dir(), "test_flows.js")

    def _seed_data_path(self) -> str:
        return os.path.join(self._runtime_projection_dir(), "seed_data.json")

    def _ensure_final_output_dirs(self) -> None:
        for path in (
            self._frontend_pages_dir(),
            self._backend_runtime_dir(),
            self._validation_test_flows_dir(),
            self._runtime_projection_dir(),
        ):
            os.makedirs(path, exist_ok=True)

    def _write_frontend_page_html(self, filename: str, html: str) -> str:
        path = self._frontend_page_path(filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rewritten = self._rewrite_page_paths_for_frontend_pages(html)
        if str(self.config_manager.get("runtime_profile", "") or "").strip() == "state_kernel_sqlite":
            rewritten = self._normalize_state_kernel_runtime_script_order(rewritten)
            state_client_tag = '<script src="../../phase_sdk_generation/runtime/environment_state_client.js"></script>'
            if "environment_state_client.js" not in rewritten:
                seed_match = re.search(
                    r'<script\b[^>]*\bsrc=["\'][^"\']*seed_data\.js[^"\']*["\'][^>]*>\s*</script>',
                    rewritten,
                    flags=re.IGNORECASE,
                )
                if seed_match:
                    rewritten = rewritten[:seed_match.start()] + state_client_tag + "\n" + rewritten[seed_match.start():]
                elif re.search(r"</head\s*>", rewritten, flags=re.IGNORECASE):
                    rewritten = re.sub(r"</head\s*>", state_client_tag + "\n</head>", rewritten, count=1, flags=re.IGNORECASE)
                else:
                    rewritten = state_client_tag + "\n" + rewritten
        with open(path, "w", encoding="utf-8") as f:
            f.write(rewritten)
        return path

    def _organize_run_level_artifacts(self, logger: Trace2WorldLogger) -> None:
        token_dir = os.path.join(self.output_dir, "run_management", "token_usage")
        reference_trace_semantic_dir = os.path.join(self.output_dir, "semantic_api", "00_reference_trace_rewrite")
        validation_spec_dir = os.path.join(self.output_dir, "validation", "spec")
        config_dir = os.path.join(self.output_dir, "run_management", "configs")
        run_summary_dir = os.path.join(self.output_dir, "run_management", "run_summary")
        moved = 0
        for name in ("token_usage_report.json", "token_usage_report.txt"):
            src = os.path.join(self.output_dir, name)
            if os.path.exists(src):
                self._move_generated_artifact(src, os.path.join(token_dir, name), reason="stage_organized_token_usage_report")
                moved += 1
        rewritten_trace_config = os.path.join(self.output_dir, "config_rewritten_reference_traces.json")
        if os.path.exists(rewritten_trace_config):
            self._move_generated_artifact(
                rewritten_trace_config,
                os.path.join(reference_trace_semantic_dir, "config_rewritten_reference_traces.json"),
                reason="stage_organized_reference_trace_rewrite",
            )
            moved += 1
        for name in list(os.listdir(self.output_dir)):
            src = os.path.join(self.output_dir, name)
            if not os.path.isfile(src):
                continue
            if name.startswith("config_") and name.endswith(".json"):
                self._move_generated_artifact(src, os.path.join(config_dir, name), reason="stage_organized_run_config")
                moved += 1
            elif name.endswith("_input_summary.json"):
                self._move_generated_artifact(src, os.path.join(run_summary_dir, name), reason="stage_organized_run_summary")
                moved += 1
        for name in ("evaluation.json", "evaluation.md"):
            misplaced = os.path.join(self.output_dir, "semantic_api", "06_page_environment_specs", name)
            canonical_validation = os.path.join(validation_spec_dir, name)
            if os.path.exists(misplaced):
                if os.path.exists(canonical_validation):
                    _move_to_deprecated(misplaced, reason="misplaced_spec_evaluation")
                else:
                    self._move_generated_artifact(
                        misplaced,
                        canonical_validation,
                        reason="stage_organized_spec_evaluation",
                    )
                moved += 1
        if moved:
            logger.log_info(f"Organized {moved} run-level artifact(s) under stage directories")

    @staticmethod
    def _runtime_websitesdk_method_names() -> set:
        return {"navigateRoute", "getRouteEvents"}

    @staticmethod
    def _business_logic_method_names(js_code: str) -> set:
        methods = set(re.findall(r"(?:^|[;\{\n]\s*)(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", js_code or "", flags=re.M))
        methods.update(re.findall(r"BusinessLogic\.prototype\.([A-Za-z_$][\w$]*)\s*=", js_code or ""))
        methods = methods - {"if", "for", "while", "switch", "catch", "function", "constructor"}
        return methods | Trace2WorldTraceGenerator._runtime_websitesdk_method_names()

    @staticmethod
    def _html_websitesdk_calls(html_content: str) -> set:
        return set(re.findall(r"\b(?:window\.)?WebsiteSDK\.([A-Za-z_$][\w$]*)\s*\(", html_content or ""))

    @staticmethod
    def _html_bare_websitesdk_calls(html_content: str) -> set:
        return set(
            re.findall(
                r"(?<![\w.$])WebsiteSDK\.([A-Za-z_$][\w$]*)\s*\(",
                html_content or "",
            )
        )

    @staticmethod
    def _attach_business_logic_cache_buster(html_content: str, sdk_code: str) -> str:
        digest = hashlib.sha256((sdk_code or "").encode("utf-8")).hexdigest()[:12]
        if not digest:
            return html_content or ""
        html = re.sub(
            r'(<script\b[^>]*\bsrc=["\'])(?:\./)?(?:\.\./\.\./phase_sdk_generation/runtime/)?business_logic\.js(?:\?v=[A-Za-z0-9_.-]+)?(["\'][^>]*>\s*</script>)',
            r"\1../../phase_sdk_generation/runtime/business_logic.js?v={}\2".format(digest),
            html_content or "",
        )
        html = re.sub(
            r'(<script\b[^>]*\bsrc=["\'])(?:\./)?(?:\.\./\.\./phase_sdk_generation/runtime/)?data_layer\.js(?:\?v=[A-Za-z0-9_.-]+)?(["\'][^>]*>\s*</script>)',
            r"\1../../phase_sdk_generation/runtime/data_layer.js?v={}\2".format(digest),
            html,
        )
        html = re.sub(
            r'(<script\b[^>]*\bsrc=["\'])(?:\./)?(?:\.\./\.\./phase_sdk_generation/runtime/)?seed_data\.js(?:\?v=[A-Za-z0-9_.-]+)?(["\'][^>]*>\s*</script>)',
            r"\1../../phase_sdk_generation/runtime/seed_data.js?v={}\2".format(digest),
            html,
        )
        return html

    def _business_logic_syntax_status(self, path: str) -> Dict[str, Any]:
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "missing"}
        if not shutil.which("node"):
            return {"ok": True, "skipped": True, "reason": "node_not_found"}
        proc = subprocess.run(
            ["node", "--check", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }

    def _validate_html_sdk_call_surface(self, html_pages: Dict[str, str], sdk_code: str) -> Dict[str, Any]:
        sdk_methods = self._business_logic_method_names(sdk_code)
        missing_by_page = {}
        bare_by_page = {}
        all_calls = set()
        for filename, html in (html_pages or {}).items():
            calls = self._html_websitesdk_calls(html)
            all_calls.update(calls)
            missing = sorted(calls - sdk_methods)
            if missing:
                missing_by_page[filename] = missing
            bare = sorted(self._html_bare_websitesdk_calls(html))
            if bare:
                bare_by_page[filename] = bare
        report = {
            "version": "html_sdk_call_surface_validation",
            "mode": "validation_only",
            "write_modified": False,
            "repair_policy": "fail_without_html_mutation",
            "html_file_count": len(html_pages or {}),
            "sdk_method_count": len(sdk_methods),
            "unique_html_sdk_call_count": len(all_calls),
            "missing_call_count": sum(len(items) for items in missing_by_page.values()),
            "missing_by_page": missing_by_page,
            "bare_call_count": sum(len(items) for items in bare_by_page.values()),
            "bare_by_page": bare_by_page,
            "valid": not missing_by_page and not bare_by_page,
        }
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        _pom = PhaseOutputManager(self.output_dir)
        _pom.write_json("pagegen", "debug", "html_sdk_call_surface_validation.json", report)
        if missing_by_page or bare_by_page:
            for filename in sorted(set(missing_by_page) | set(bare_by_page)):
                missing_methods = missing_by_page.get(filename, [])
                bare_methods = bare_by_page.get(filename, [])
                rel_issue_path = (
                    "pagegen_contract_issues/"
                    + re.sub(r"[^A-Za-z0-9_.-]+", "_", (filename or "unknown_page").split("/")[-1])
                    + ".json"
                )
                issue_path = _pom.debug("pagegen", rel_issue_path)
                existing: Dict[str, Any] = {}
                if issue_path.exists():
                    try:
                        loaded = json.loads(issue_path.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            existing = loaded
                    except Exception:
                        existing = {}
                issues = list(existing.get("issues", [])) if isinstance(existing.get("issues"), list) else []
                seen = {
                    (
                        str(issue.get("code") or ""),
                        str(issue.get("artifact") or ""),
                        str(issue.get("selector_hint") or ""),
                    )
                    for issue in issues
                    if isinstance(issue, dict)
                }
                for method in missing_methods:
                    issue = {
                        "code": "sdk_call_surface.undeclared_website_sdk_call",
                        "message": (
                            f"{filename} calls WebsiteSDK.{method}, but business_logic.js "
                            "does not declare that method. Reject the PageGen output and "
                            "fix the producer source or prompt."
                        ),
                        "selector_hint": f"WebsiteSDK.{method}",
                        "artifact": filename,
                        "evidence": json.dumps(
                            {
                                "missing_method": method,
                                "declared_sdk_method_count": len(sdk_methods),
                            },
                            ensure_ascii=False,
                        ),
                        "expected_contract": (
                            "PageGen must call only WebsiteSDK methods declared by the generated SDK, "
                            "or the SDK producer must expose the required formal action method."
                        ),
                    }
                    identity = (issue["code"], issue["artifact"], issue["selector_hint"])
                    if identity not in seen:
                        issues.append(issue)
                        seen.add(identity)
                for method in bare_methods:
                    issue = {
                        "code": "sdk_call_surface.unqualified_website_sdk_call",
                        "message": (
                            f"{filename} calls bare WebsiteSDK.{method}; the runtime instance is "
                            "window.WebsiteSDK. Reject the PageGen output and fix the producer source or prompt."
                        ),
                        "selector_hint": f"WebsiteSDK.{method}",
                        "artifact": filename,
                        "evidence": json.dumps(
                            {"method": method, "required_receiver": "window.WebsiteSDK"},
                            ensure_ascii=False,
                        ),
                        "expected_contract": (
                            "PageGen must call window.WebsiteSDK methods explicitly; the pipeline must not "
                            "rewrite bare WebsiteSDK calls after generation."
                        ),
                    }
                    identity = (issue["code"], issue["artifact"], issue["selector_hint"])
                    if identity not in seen:
                        issues.append(issue)
                        seen.add(identity)
                html_path = Path(self._frontend_pages_dir()) / filename
                payload = {
                    "format": "pagegen_contract_issues",
                    "schema_revision": 1,
                    "status": "rejected_pagegen_output",
                    "mode": "source_fix_required",
                    "filename": filename,
                    "attempt": existing.get("attempt"),
                    "remaining_issue_count": len(issues),
                    "issues": issues,
                    "last_error": existing.get("last_error", ""),
                    "saved_html_path": os.path.relpath(str(html_path), self.output_dir) if html_path.exists() else "",
                    "candidate_saved": html_path.exists(),
                    "html_saved": html_path.exists(),
                }
                _pom.write_json(
                    "pagegen",
                    "debug",
                    rel_issue_path,
                    payload,
                    producer="trace_generator.html_sdk_call_surface_validation",
                )
        return report

    def _build_site_model(self, trace_dirs, task_metadata_path, website_filter):
        self.logger.log_step_start("Load Traces", "PREPARE")
        self.logger.start_stage("Load Traces", "prepare")


        trace_resource_dir = self.config_manager.get("trace_resource_dir", "")
        if trace_resource_dir and os.path.exists(os.path.join(trace_resource_dir, "index.json")):
            traces = self.trace_loader.load_mt_resource(trace_resource_dir)
            self.logger.log_info(f"Loaded {len(traces)} traces from mt_resource: {trace_resource_dir}")
        else:

            if not trace_dirs:
                trace_input_dir = self.config_manager.get("trace_input_dir", "")
                if trace_input_dir:
                    trace_dirs = self.trace_loader.discover_trace_dirs(trace_input_dir)
                else:
                    raise ValueError("No trace_dirs provided and trace_input_dir not configured")

            if not task_metadata_path:
                task_metadata_path = self.config_manager.get("task_metadata_path", "")

            traces = self.trace_loader.load_traces_for_website(
                trace_dirs,
                task_metadata_path,
                website_filter,
            )

        if not traces:
            raise ValueError("No traces loaded")

        website_type_config = self.config_manager.get("website_type", "")
        self._active_trace_ids = {
            str(getattr(trace, "trace_id", "") or "")
            for trace in traces
            if str(getattr(trace, "trace_id", "") or "")
        }
        self.logger.log_info(f"Loaded {len(traces)} traces")
        self.logger.end_stage("Load Traces")
        self.logger.log_step_end("Load Traces", "PREPARE")


        self.logger.log_step_start("Build AppSpec", "PREPARE")
        self.logger.start_stage("Build AppSpec", "prepare")

        try:
            site_model = asyncio.run(self.site_model_builder.build_from_traces(traces, output_dir=self.output_dir))
        except GlobalReasoningStopAfterReached:
            self.logger.end_stage("Build AppSpec")
            self.logger.log_step_end("Build AppSpec", "PREPARE")
            raise _StopAfterReached("global_reasoning")
        except InstanceSplittingStopAfterReached:
            self.logger.end_stage("Build AppSpec")
            self.logger.log_step_end("Build AppSpec", "PREPARE")
            raise _StopAfterReached("instance_splitting")
        except SelectedInstanceCompileStopAfterReached:
            self.logger.end_stage("Build AppSpec")
            self.logger.log_step_end("Build AppSpec", "PREPARE")
            raise _StopAfterReached("selected_instance_content_extraction")


        if site_model.website_type and site_model.website_type != "website":
            website_type = site_model.website_type
            self.logger.log_info(f"website_type inferred from traces: {website_type}")
        elif website_type_config:
            website_type = website_type_config
            site_model.website_type = website_type
            self.logger.log_info(f"website_type from config: {website_type}")
        else:
            website_type = "website"
            site_model.website_type = website_type

        self.logger.log_info(
            f"AppSpec built: {len(site_model.structure.page_prototypes)} pages, "
            f"{len(site_model.structure.site_graph.edges)} edges"
        )
        self.logger.end_stage("Build AppSpec")
        self.logger.log_step_end("Build AppSpec", "PREPARE")

        return site_model, website_type

    def _load_reference_traces_for_downstream(self) -> List[Any]:
        trace_resource_dir = self.config_manager.get("trace_resource_dir", "")
        if trace_resource_dir and os.path.exists(os.path.join(trace_resource_dir, "index.json")):
            return self.trace_loader.load_mt_resource(trace_resource_dir)

        trace_input_dir = self.config_manager.get("trace_input_dir", "")
        if not trace_input_dir:
            return []

        if self.trace_loader.is_meituan_sessions_root(trace_input_dir):
            trace_dirs = self.trace_loader.discover_trace_dirs(trace_input_dir)
        elif self.trace_loader.is_mt_resource(trace_input_dir):
            return self.trace_loader.load_mt_resource(trace_input_dir)
        elif os.path.isdir(trace_input_dir):
            trace_dirs = self.trace_loader.discover_trace_dirs(trace_input_dir) or [trace_input_dir]
        else:
            return []

        task_metadata_path = self.config_manager.get("task_metadata_path", "")
        website_filter = self.config_manager.get("website_filter", "")
        return self.trace_loader.load_traces_for_website(
            trace_dirs,
            task_metadata_path=task_metadata_path,
            website_filter=website_filter,
        )


    def _merge_inferred_routes_into_site_model(
        self,
        *,
        site_model: Any,
        page_environment_specs: Dict[str, Any],
        logger: Trace2WorldLogger,
    ) -> Dict[str, Any]:
        from trace2world_pipeline.app_spec.model import GraphEdge

        structure = getattr(site_model, "structure", None)
        site_graph = getattr(structure, "site_graph", None) if structure else None
        if site_graph is None:
            return {
                "format": "inferred_route_merge_audit_v1",
                "schema_revision": 1,
                "trace_edge_count": 0,
                "inferred_admitted_count": 0,
                "inferred_overridden_count": 0,
                "skipped_reason": "site_model_has_no_structure_site_graph",
                "inferred_admitted": [],
                "inferred_overridden_by_trace": [],
            }

        trace_edges = list(getattr(site_graph, "edges", []) or [])
        trace_keys: set[Tuple[str, str]] = set()
        trace_edge_index: Dict[Tuple[str, str], List[Any]] = {}
        for edge in trace_edges:
            key = (str(getattr(edge, "from_page_id", "")), str(getattr(edge, "trigger", "")))
            trace_keys.add(key)
            trace_edge_index.setdefault(key, []).append(edge)

        page_envs = (
            page_environment_specs.get("page_environment_specs_by_filename", {})
            if isinstance(page_environment_specs, dict)
            else {}
        )
        if not isinstance(page_envs, dict):
            page_envs = {}

        inferred_admitted: List[Dict[str, Any]] = []
        inferred_overridden: List[Dict[str, Any]] = []
        existing_inferred_keys: set[Tuple[str, str, str]] = set()

        for filename, page_env in page_envs.items():
            if not isinstance(page_env, dict):
                continue
            source_mid = str(
                page_env.get("materialized_page_id")
                or page_env.get("site_model_page_id")
                or page_env.get("page_id")
                or ""
            ).strip()
            if not source_mid:
                continue
            for raw in page_env.get("route_affordances", []) or []:
                if not isinstance(raw, dict):
                    continue
                authority = str(raw.get("route_authority") or raw.get("authority") or "").strip()
                if authority == "reference_observed":
                    continue
                target_mid = str(
                    raw.get("target_materialized_page_id")
                    or raw.get("target_page_id")
                    or ""
                ).strip()
                if not target_mid or target_mid == source_mid:
                    continue
                trigger = str(
                    raw.get("label")
                    or raw.get("trigger")
                    or raw.get("display_label")
                    or raw.get("component_id")
                    or ""
                ).strip()
                if not trigger:
                    continue
                if (source_mid, trigger) in trace_keys:
                    inferred_overridden.append({
                        "source_materialized_page_id": source_mid,
                        "trigger": trigger,
                        "inferred_target_materialized_page_id": target_mid,
                        "route_authority": authority,
                        "trace_targets": sorted({
                            str(getattr(e, "to_page_id", ""))
                            for e in trace_edge_index.get((source_mid, trigger), [])
                        }),
                        "reason": "trace_edge_takes_precedence",
                    })
                    continue
                dedup_key = (source_mid, trigger, target_mid)
                if dedup_key in existing_inferred_keys:
                    continue
                existing_inferred_keys.add(dedup_key)
                edge = GraphEdge(
                    from_page_id=source_mid,
                    to_page_id=target_mid,
                    trigger=trigger,
                    params={},
                    observed_in_trace="inferred_route_affordance",
                    step_index=-1,
                    evidence_count=1,
                    evidence_steps=[],
                    confidence=float(raw.get("confidence") or 0.5),
                )
                site_graph.edges.append(edge)
                inferred_admitted.append({
                    "source_materialized_page_id": source_mid,
                    "trigger": trigger,
                    "target_materialized_page_id": target_mid,
                    "route_authority": authority,
                    "source_filename": str(filename),
                })

        return {
            "format": "inferred_route_merge_audit_v1",
            "schema_revision": 1,
            "trace_edge_count": len(trace_edges),
            "inferred_admitted_count": len(inferred_admitted),
            "inferred_overridden_count": len(inferred_overridden),
            "total_edges_after_merge": len(site_graph.edges),
            "inferred_admitted": inferred_admitted,
            "inferred_overridden_by_trace": inferred_overridden,
            "precedence_policy": "trace_evidence_wins_over_inferred",
        }

    def _save_inferred_route_merge_audit(self, audit: Dict[str, Any]) -> None:
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        pom = PhaseOutputManager(self.output_dir)
        audit_path = pom.write_json("consolidation", "debug", "inferred_route_merge_audit.json", audit)
        if hasattr(self, "logger") and self.logger:
            self.logger.log_info(
                f"Wrote inferred route merge audit: {os.path.relpath(str(audit_path), self.output_dir)}"
            )


    async def _prepare_site_model_semantic_design(
        self,
        *,
        site_model,
        logger,
    ) -> Dict[str, Any]:
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        pom = PhaseOutputManager(self.output_dir)

        extra_page_data: Optional[Dict[str, Dict[str, Any]]] = None
        page_execution_contracts: Dict[str, Any] = {}
        materialized_observation_bindings: Dict[str, Any] = {}
        step_boundary_contracts: Dict[str, Any] = {}
        page_execution_obligations_by_filename: Dict[str, List[Dict[str, Any]]] = {}
        trace_execution_obligations: List[Dict[str, Any]] = []
        l0_execution_pack: Dict[str, Any] = {}
        page_environment_projections: Dict[str, Dict[str, Any]] = {}
        page_environment_runtime_specs: Dict[str, Dict[str, Any]] = {}
        page_environment_specs: Dict[str, Any] = {}
        page_environment_evaluation: Dict[str, Any] = {}
        interface_requirement_projection: Dict[str, Any] = {}
        interface_requirement_evaluation: Dict[str, Any] = {}
        existing_page_execution_contracts_for_route_gate: Dict[str, Any] = {}
        l0_transition_blocking_conflict_count = 0
        l0_transition_conflict_output_path = ""
        l0_transition_conflict_gate_mode = str(self.config_manager.get("l0_transition_conflict_gate_mode", "blocking") or "blocking").strip().lower()
        l0_transition_conflict_gate_allows_continue = l0_transition_conflict_gate_mode in {"warning", "warn", "allow", "continue"}
        page_environment_blocking_error_count = 0
        page_environment_eval_output_path = ""
        page_environment_planning_mode = resolve_page_environment_planning_mode(self.config_manager)
        unified_planning_enabled = page_environment_planning_mode == "unified"

        def _publish_to_site_model(field_name: str, payload: Any) -> None:
            setattr(site_model, field_name, payload)


        try:
            if (
                self.config_manager.get("alternative_route_access_planner_enabled", False)
                and not unified_planning_enabled
            ):
                from trace2world_pipeline.app_spec.alternative_route_access import (
                    apply_alternative_route_access_plan,
                    build_alternative_route_access_plan_async,
                )

                planner_config = self.config_manager.get_component_config("alternative_route_access_planner")
                site_model_dir = str(pom.site_model_dir())
                debug_alt_dir = str(pom.debug("consolidation", "alternative_route_access_plan"))
                semantic_alt_dir = os.path.join(self.output_dir, "semantic_api", "05_alternative_route_access_plan")
                cached_alt = _load_json_artifact(os.path.join(site_model_dir, "alternative_route_access_plan.json"))
                if not isinstance(cached_alt, dict) or not cached_alt:
                    cached_alt = _load_json_artifact(os.path.join(semantic_alt_dir, "alternative_route_access_plan.json"))
                if isinstance(cached_alt, dict) and cached_alt:
                    alternative_route_access_plan = cached_alt
                    logger.log_info(
                        "Reused alternative_route_access_plan.json "
                        f"({alternative_route_access_plan.get('route_count', 0)} routes)"
                    )
                else:
                    alternative_route_access_plan = await build_alternative_route_access_plan_async(
                        site_model,
                        Path(self.output_dir),
                        model=planner_config.get("model"),
                        reasoning_effort=planner_config.get("reasoning_effort", "medium"),
                        max_pages_per_call=self.config_manager.get(
                            "alternative_route_access_planner_max_pages_per_call",
                            4,
                        ),
                        max_concurrent=self.config_manager.get("max_concurrent", 8),
                        max_controls_per_page=self.config_manager.get(
                            "alternative_route_access_planner_max_controls_per_page",
                            48,
                        ),
                        max_candidates_per_page=self.config_manager.get(
                            "alternative_route_access_planner_max_candidates_per_page",
                            64,
                        ),
                        max_tokens=self.config_manager.get(
                            "alternative_route_access_planner_max_tokens",
                            12000,
                        ),
                    )
                applied_count = apply_alternative_route_access_plan(site_model, alternative_route_access_plan)
                alternative_route_access_plan["applied_navigation_out_count"] = applied_count
                site_model.page_behaviors = site_model.structure.page_behaviors
                pom.write_json("consolidation", "formal",
                               "semantic_api/05_alternative_route_access_plan/alternative_route_access_plan.json",
                               alternative_route_access_plan,
                               )
                pom.write_json("consolidation", "debug",
                               "alternative_route_access_plan/alternative_route_access_plan.json",
                               alternative_route_access_plan,
                               )
                _publish_to_site_model("alternative_route_access_plan", alternative_route_access_plan)
                logger.log_info(
                    "Materialized alternative_route_access_plan.json as a AppSpec semantic view "
                    f"({alternative_route_access_plan.get('route_count', 0)} routes, "
                    f"{applied_count} applied to page_behaviors.navigation_out)"
                )
        except Exception as exc:
            logger.log_warning(f"Failed to build alternative route access plan: {exc}")

        try:
            from trace2world_pipeline.app_spec.page_execution_contracts import (
                build_dynamic_surface_contracts,
                build_input_surface_contracts,
                build_page_execution_contracts,
                build_route_execution_contracts,
            )
            from trace2world_pipeline.app_spec.page_environment_spec import build_route_affordance_candidates

            site_model_dir = str(pom.site_model_dir())
            page_execution_contracts_path = os.path.join(site_model_dir, "page_execution_contracts.json")
            existing_page_execution_contracts_for_route_gate = _load_json_artifact(page_execution_contracts_path)
            if not isinstance(existing_page_execution_contracts_for_route_gate, dict):
                existing_page_execution_contracts_for_route_gate = {}
            page_execution_contracts = build_page_execution_contracts(site_model)
            dynamic_surface_contracts = build_dynamic_surface_contracts(page_execution_contracts)
            input_surface_contracts = build_input_surface_contracts(page_execution_contracts)
            route_execution_contracts = build_route_execution_contracts(page_execution_contracts)
            route_affordance_candidates = build_route_affordance_candidates(page_execution_contracts)
            os.makedirs(site_model_dir, exist_ok=True)
            _publish_to_site_model("page_execution_contracts", page_execution_contracts)
            _publish_to_site_model("input_surface_contracts", input_surface_contracts)
            _publish_to_site_model("dynamic_surface_contracts", dynamic_surface_contracts)
            _publish_to_site_model("route_execution_contracts", route_execution_contracts)
            _publish_to_site_model("route_affordance_candidates", route_affordance_candidates)
            route_affordance_candidates_path = pom.write_json(
                "consolidation", "formal",
                "projections/page_environment/route_affordance_candidates.json",
                route_affordance_candidates,
                register=False,
            )
            register_artifact(
                Path(self.output_dir),
                route_affordance_candidates_path,
                artifact_id="projections.page_environment.route_affordance_candidates",
                phase="site_model_semantic_design",
                artifact_kind="det_projection",
                api_or_det="det",
                canonical_source=False,
                logical_owner="page_environment_spec",
                producer="site_model.page_environment_spec.build_route_affordance_candidates",
                allowed_consumers=("site_model_semantic_design", "validator"),
                semantic_role="route_affordance_candidates",
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="candidate_projection",
                source_selection={
                    "derived_from": "phase_consolidation/site_model/page_execution_contracts.json",
                    "selected_fields": [
                        "label",
                        "display_labels",
                        "target_filename",
                        "surface",
                        "binding_kind",
                        "component_ref",
                        "action_id",
                        "source",
                    ],
                },
            )
            legacy_transition_contract_path = os.path.join(site_model_dir, "execution_transition_contracts.json")
            if os.path.exists(legacy_transition_contract_path):
                _move_to_deprecated(legacy_transition_contract_path, reason="legacy_transition_contract")
            task_runner_path = os.path.join(self.output_dir, "ghostweb-task-runner.html")
            if os.path.exists(task_runner_path):
                moved_path = _move_to_deprecated(task_runner_path, reason="legacy_task_runner")
                logger.log_info(f"Moved legacy ghostweb-task-runner.html to {moved_path}")
            logger.log_info(
                "Materialized page_execution_contracts.json as a AppSpec semantic view "
                f"({len(page_execution_contracts.get('pages', []))} pages)"
            )
            logger.log_info(
                "Materialized route_affordance_candidates "
                f"({route_affordance_candidates.get('affordance_count', 0)} candidates)"
            )
        except Exception as exc:
            logger.log_warning(f"Failed to build page execution contracts: {exc}")

        try:
            from trace2world_pipeline.app_spec.materialized_observation_bindings import build_materialized_observation_bindings

            obs_bindings = build_materialized_observation_bindings(site_model)
            materialized_observation_bindings = obs_bindings
            bindings_path = pom.write_json(
                "consolidation", "formal",
                "materialized_bindings/materialized_observation_bindings.json",
                obs_bindings,
                artifact_id="consolidation.materialized_bindings.observation_bindings",
                artifact_kind="det_projection",
                producer="site_model.materialized_observation_bindings.build_materialized_observation_bindings",
            )
            _publish_to_site_model("materialized_observation_bindings", obs_bindings)
            _write_consolidation_recompute_decision(
                self.output_dir,
                subphase="materialized_bindings",
                artifact_id="consolidation.materialized_bindings.observation_bindings",
                artifact_rel_path="phase_consolidation/materialized_bindings/materialized_observation_bindings.json",
                input_artifacts=["phase_consolidation/site_model/page_prototypes/*_content.json"],
            )
            logger.log_info(f"Materialized observation bindings for AppSpec semantic design: {bindings_path}")
        except Exception as exc:
            logger.log_warning(f"Failed to build materialized observation bindings: {exc}")

        try:
            from trace2world_pipeline.app_spec.page_execution_obligations import (
                build_l0_transition_conflicts,
                build_l0_execution_pack_from_obligations,
                build_page_execution_obligations_by_page_from_site_model,
            )

            reference_traces = self._load_reference_traces_for_downstream()
            if reference_traces:
                site_model_dir = str(pom.site_model_dir())
                os.makedirs(site_model_dir, exist_ok=True)
                page_execution_obligations = build_page_execution_obligations_by_page_from_site_model(
                    site_model,
                    reference_traces,
                    output_dir=self.output_dir,
                )
                page_execution_obligations_by_filename = (
                    page_execution_obligations.get("pages", {})
                    if isinstance(page_execution_obligations, dict)
                    else {}
                )
                trace_execution_obligations = (
                    page_execution_obligations.get("obligations", [])
                    if isinstance(page_execution_obligations, dict)
                    else []
                )
                l0_execution_pack = build_l0_execution_pack_from_obligations(page_execution_obligations)
                _publish_to_site_model("l0_execution_pack", l0_execution_pack)
                from trace2world_pipeline.app_spec.step_boundary_contracts import build_step_boundary_contracts

                step_boundary_contracts = build_step_boundary_contracts(
                    l0_execution_pack=l0_execution_pack,
                    materialized_observation_bindings=materialized_observation_bindings,
                    reviewed_boundary_decisions=(
                        getattr(site_model, "step_boundary_contracts", {}) or {}
                    ),
                )
                if int(step_boundary_contracts.get("summary", {}).get("blocking_issue_count", 0) or 0):
                    raise RuntimeError(
                        "Step boundary contract gate failed: "
                        f"{step_boundary_contracts.get('summary', {}).get('blocking_issue_count', 0)} "
                        "blocking issue(s)"
                    )
                _publish_to_site_model("step_boundary_contracts", step_boundary_contracts)
                pom.write_json(
                    "consolidation",
                    "formal",
                    "materialized_bindings/step_boundary_contracts.json",
                    step_boundary_contracts,
                    register=False,
                )
                l0_execution_pack_path = pom.write_json(
                    "consolidation", "formal",
                    "l0_execution/l0_execution_pack.json",
                    l0_execution_pack,
                    register=False,
                )
                pom.write_json(
                    "consolidation", "formal",
                    "projections/l0_execution/l0_execution_pack.json",
                    l0_execution_pack,
                    register=False,
                )
                register_artifact(
                    Path(self.output_dir),
                    l0_execution_pack_path,
                    artifact_id="consolidation.l0_execution.pack",
                    phase="site_model_semantic_design",
                    artifact_kind="det_projection",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="l0_execution",
                    producer="site_model.page_execution_obligations.build_l0_execution_pack",
                    allowed_consumers=("site_model_semantic_design", "page_execution_validator", "repair"),
                    semantic_role="l0_execution_consumption_projection",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    artifact_role="projection",
                    source_selection={
                        "derived_from": [
                            "reference_traces",
                            "capture_actions",
                            "page_execution_obligations_by_page",
                        ],
                    },
                )
                l0_transition_conflicts = build_l0_transition_conflicts(l0_execution_pack)
                l0_transition_conflict_output_path = pom.write_json(
                    "consolidation", "formal",
                    "l0_execution/l0_transition_conflicts.json",
                    l0_transition_conflicts,
                    register=False,
                )
                pom.write_json(
                    "consolidation", "formal",
                    "projections/l0_execution/l0_transition_conflicts.json",
                    l0_transition_conflicts,
                    register=False,
                )
                l0_transition_blocking_conflict_count = int(
                    l0_transition_conflicts.get("blocking_conflict_count", 0) or 0
                )
                register_artifact(
                    Path(self.output_dir),
                    l0_transition_conflict_output_path,
                    artifact_id="consolidation.l0_execution.transition_conflicts",
                    phase="site_model_semantic_design",
                    artifact_kind="validation_report",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="l0_execution",
                    producer="site_model.page_execution_obligations.build_l0_transition_conflicts",
                    allowed_consumers=("trace_generator", "developer"),
                    semantic_role="l0_transition_conflict_gate",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    artifact_role="validation_gate",
                    source_selection={
                        "derived_from": ["phase_consolidation/l0_execution/l0_execution_pack.json"],
                    },
                )
                _write_consolidation_recompute_decision(
                    self.output_dir,
                    subphase="l0_execution",
                    artifact_id="consolidation.l0_execution.pack",
                    artifact_rel_path="phase_consolidation/l0_execution/l0_execution_pack.json",
                    input_artifacts=[
                        "reference_traces",
                        "phase_consolidation/site_model/page_execution_obligations_by_page.json",
                    ],
                )
                if l0_transition_blocking_conflict_count:
                    logger.log_warning(
                        "Detected incompatible L0 transition conflict(s): "
                        f"{l0_transition_blocking_conflict_count}. "
                        f"See {l0_transition_conflict_output_path}"
                    )
                _publish_to_site_model("page_execution_obligations_by_page", page_execution_obligations)
                pom.write_json("consolidation", "debug",
                               "pagegen_execution_obligations.json",
                               page_execution_obligations,
                               )
                try:
                    from trace2world_pipeline.app_spec.route_binding_projection import build_canonical_route_bindings

                    canonical_route_bindings = build_canonical_route_bindings(l0_execution_pack, site_model)
                    canonical_route_bindings_path = pom.write_json(
                        "consolidation", "formal",
                        "canonical_routes/canonical_route_bindings.json",
                        canonical_route_bindings,
                        register=False,
                    )
                    pom.write_json(
                        "consolidation", "formal",
                        "projections/page_environment/canonical_route_bindings.json",
                        canonical_route_bindings,
                        register=False,
                    )
                    register_artifact(
                        Path(self.output_dir),
                        canonical_route_bindings_path,
                        artifact_id="consolidation.canonical_routes.bindings",
                        phase="site_model_semantic_design",
                        artifact_kind="det_projection",
                        api_or_det="det",
                        canonical_source=False,
                        logical_owner="canonical_route_bindings",
                        producer="site_model.route_binding_projection.build_canonical_route_bindings",
                        allowed_consumers=("alternative_route_access_planner", "site_model_semantic_design", "validator"),
                        semantic_role="l0_route_binding_projection",
                        canonical_semantic_source=False,
                        canonical_prompt_boundary=False,
                        artifact_role="projection",
                        source_selection={
                            "derived_from": [
                                "phase_consolidation/l0_execution/l0_execution_pack.json",
                                "phase_consolidation/site_model/page_prototypes/*_content.json",
                            ],
                        },
                    )
                    _write_consolidation_recompute_decision(
                        self.output_dir,
                        subphase="canonical_routes",
                        artifact_id="consolidation.canonical_routes.bindings",
                        artifact_rel_path="phase_consolidation/canonical_routes/canonical_route_bindings.json",
                        input_artifacts=[
                            "phase_consolidation/l0_execution/l0_execution_pack.json",
                            "phase_consolidation/site_model/page_prototypes/*_content.json",
                        ],
                    )
                    logger.log_info(
                        "Materialized canonical_route_bindings.json "
                        f"({canonical_route_bindings.get('summary', {}).get('binding_count', 0)} bindings, "
                        f"{canonical_route_bindings.get('summary', {}).get('rejected_count', 0)} rejected)"
                    )
                except Exception as exc:
                    logger.log_warning(f"Failed to build canonical route bindings: {exc}")
                logger.log_info(
                    "Materialized l0_execution_pack.json for AppSpec semantic design "
                    f"({page_execution_obligations.get('total_obligations', 0)} obligations, "
                    f"{page_execution_obligations.get('page_count', 0)} pages)"
                )
        except Exception as exc:
            logger.log_warning(f"Failed to build page execution obligations: {exc}")

        if l0_transition_blocking_conflict_count and not l0_transition_conflict_gate_allows_continue:
            raise RuntimeError(
                "L0 transition conflict gate failed during AppSpec semantic design: "
                f"{l0_transition_blocking_conflict_count} incompatible L0 transition conflict(s). "
                f"See {l0_transition_conflict_output_path or 'phase_consolidation/l0_execution/l0_transition_conflicts.json'}. "
                "Drop one conflicting trace or split the dataset before continuing."
            )
        if l0_transition_blocking_conflict_count and l0_transition_conflict_gate_allows_continue:
            logger.log_warning(
                "Continuing despite incompatible L0 transition conflict(s) because "
                f"l0_transition_conflict_gate_mode={l0_transition_conflict_gate_mode!r}: "
                f"{l0_transition_blocking_conflict_count}. "
                f"See {l0_transition_conflict_output_path or 'phase_consolidation/l0_execution/l0_transition_conflicts.json'}"
            )

        if page_execution_contracts:
            from trace2world_pipeline.app_spec.page_environment_spec import build_component_route_outedge_conflicts

            component_route_outedge_conflicts = build_component_route_outedge_conflicts(
                page_execution_contracts,
                l0_execution_pack,
            )
            component_route_conflict_source = "current_build"
            if existing_page_execution_contracts_for_route_gate:
                existing_component_route_outedge_conflicts = build_component_route_outedge_conflicts(
                    existing_page_execution_contracts_for_route_gate,
                    l0_execution_pack,
                )
                current_transition_count = int(
                    component_route_outedge_conflicts.get("summary", {})
                    .get("navigation_transition_count", 0)
                    or 0
                )
                existing_transition_count = int(
                    existing_component_route_outedge_conflicts.get("summary", {})
                    .get("navigation_transition_count", 0)
                    or 0
                )
                if existing_transition_count > current_transition_count:
                    component_route_outedge_conflicts = existing_component_route_outedge_conflicts
                    component_route_conflict_source = "existing_disk_before_page_execution_contract_refresh"
            component_route_outedge_conflicts["source_selection"] = {
                "page_execution_contracts": component_route_conflict_source,
                "l0_execution_pack": "current_build",
            }
            component_route_conflict_count = int(
                component_route_outedge_conflicts.get("conflict_count", 0) or 0
            )
            component_route_conflict_output_path = pom.write_json(
                "consolidation", "debug",
                "component_route_outedge_conflicts.json",
                component_route_outedge_conflicts,
                register=False,
            )
            register_artifact(
                Path(self.output_dir),
                component_route_conflict_output_path,
                artifact_id="debug.component_route_outedge_conflicts",
                phase="site_model_semantic_design",
                artifact_kind="validation_report",
                api_or_det="det",
                canonical_source=False,
                logical_owner="page_execution_contracts",
                producer="site_model.page_environment_spec.build_component_route_outedge_conflicts",
                allowed_consumers=("trace_generator", "developer"),
                semantic_role="component_instance_multi_target_route_gate",
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="validation_gate",
                source_selection={
                    "derived_from": [
                        "phase_consolidation/site_model/page_execution_contracts.json",
                        "phase_consolidation/l0_execution/l0_execution_pack.json",
                    ],
                },
            )
            if component_route_conflict_count:
                logger.log_warning(
                    "Detected component-instance multi-target route conflict(s): "
                    f"{component_route_conflict_count}. "
                    f"See {component_route_conflict_output_path}"
                )
                if self.config_manager.get("component_route_outedge_hard_gate_enabled", True):
                    raise RuntimeError(
                        "component-instance multi-target route hard gate failed during "
                        "AppSpec semantic design: "
                        f"{component_route_conflict_count} conflict(s). "
                        f"See {component_route_conflict_output_path}."
                    )

        trace_execution_obligation_payload: Dict[str, Any] = {}
        if trace_execution_obligations:
            trace_execution_obligation_payload = {
                "format": "trace_execution_obligations",
                "schema_revision": 1,
                "obligations": trace_execution_obligations,
                "obligation_count": len(trace_execution_obligations),
            }
        runtime_state_obligation_payload: Dict[str, Any] = {}
        try:
            runtime_state_observations = (
                getattr(site_model, "runtime_state_observations", {})
                if site_model is not None
                else {}
            )
            if isinstance(runtime_state_observations, dict):
                candidate_payload = (
                    runtime_state_observations.get("derived", {})
                    .get("runtime_state_obligations", {})
                )
                if isinstance(candidate_payload, dict):
                    runtime_state_obligation_payload = candidate_payload
        except Exception:
            runtime_state_obligation_payload = {}

        pre_pageenv_global_execution_schema: Dict[str, Any] = {}
        pre_pageenv_global_execution_schema_source = (
            "phase_consolidation/schema_registry/global_execution_schema.json"
        )
        canonical_pre_pageenv_schema = _load_json_artifact(
            os.path.join(
                self.output_dir,
                "phase_consolidation",
                "schema_registry",
                "global_execution_schema.json",
            )
        )
        if isinstance(canonical_pre_pageenv_schema, dict) and canonical_pre_pageenv_schema.get("global_fields"):
            pre_pageenv_global_execution_schema = canonical_pre_pageenv_schema
            logger.log_info(
                "Using canonical pre-PageEnv global_execution_schema from schema generation "
                "(phase_consolidation/schema_registry/global_execution_schema.json)"
            )
        elif trace_execution_obligation_payload or runtime_state_obligation_payload:
            raise RuntimeError(
                "Missing canonical pre-PageEnv global_execution_schema from schema generation; "
                "refusing to use AppSpec.content.global_execution_schema as a fallback or to "
                "synthesize it in trace_generator from downstream obligations. Fix consolidation "
                "schema inference before PageEnv/spec generation."
            )
        if pre_pageenv_global_execution_schema:
            logger.log_info(
                "Resolved pre-PageEnv global_execution_schema "
                f"(source={pre_pageenv_global_execution_schema_source}, "
                f"fields={len(pre_pageenv_global_execution_schema.get('global_fields', []))}, "
                f"rejected={len(pre_pageenv_global_execution_schema.get('rejected_obligations', []))})"
            )

        action_slot_route_hard_gate_error: Optional[RuntimeError] = None
        try:
            from trace2world_pipeline.app_spec.page_environment_spec import (
                build_page_environment_specs,
                build_route_affordances_final,
                close_page_environment_contracts,
                compact_page_environment_spec_for_prompt,
                enrich_page_environment_specs_async,
                evaluate_page_environment_specs,
                materialize_autocomplete_suggestion_contracts,
                resolve_page_environment_conflicts_async,
                write_full_spec_route_audit,
            )

            site_model_dir = str(pom.site_model_dir())
            spec_authority_relpath = (
                "phase_page_environment/semantic_api/06_page_environment_specs/"
                "page_environment_specs.json"
            )
            spec_authority_path = os.path.join(self.output_dir, spec_authority_relpath)
            reused_page_environment_specs = False
            cached_spec = _load_json_artifact(spec_authority_path)
            cached_executability_eval = _load_json_artifact(
                os.path.join(
                    self.output_dir,
                    "phase_page_environment",
                    "semantic_api",
                    "07_spec_executability_eval",
                    "spec_executability_eval.json",
                )
            )
            schema_source_abs_path = ""
            schema_source_mtime: Optional[float] = None
            if pre_pageenv_global_execution_schema_source.startswith("phase_"):
                schema_source_abs_path = os.path.join(
                    self.output_dir,
                    pre_pageenv_global_execution_schema_source,
                )
                if os.path.exists(schema_source_abs_path):
                    schema_source_mtime = os.path.getmtime(schema_source_abs_path)
            reuse_decision = _page_environment_spec_reuse_decision(
                cached_spec=cached_spec,
                cached_executability_eval=cached_executability_eval,
                canonical_schema=pre_pageenv_global_execution_schema or None,
                schema_source_path=pre_pageenv_global_execution_schema_source,
                schema_mtime=schema_source_mtime,
                current_page_filenames=[
                    str(getattr(page, "filename", "") or "").strip()
                    for page in (site_model.structure.page_prototypes or [])
                    if str(getattr(page, "filename", "") or "").strip()
                ],
            )
            reuse_decision["artifact_path"] = spec_authority_relpath
            if reuse_decision.get("reused"):
                page_environment_specs = cached_spec or {}
                reused_page_environment_specs = True
                if pre_pageenv_global_execution_schema:
                    page_environment_specs = project_global_schema_into_page_environment_specs(
                        page_environment_specs,
                        pre_pageenv_global_execution_schema,
                        source=pre_pageenv_global_execution_schema_source,
                    )
                logger.log_info(
                    "Reused page_environment_specs from canonical output artifact "
                    f"({len(page_environment_specs.get('page_environment_specs_by_filename', {}))} pages, "
                    f"reason={reuse_decision.get('reason')})"
                )
            else:
                logger.log_info(
                    "PageEnvironmentSpec reuse miss "
                    f"(reason={reuse_decision.get('reason')}, artifact={spec_authority_relpath})"
                )
            pom.write_json("page_environment", "debug",
                           "page_environment_spec/reuse_decision.json",
                           dict(reuse_decision),
                           )
            self._record_resume_plan_decision(
                logical_artifact="page_environment_specs",
                source="reused_disk" if reused_page_environment_specs else "recomputed",
                phase="PIPE-P5 AppSpec Semantic Design",
                artifact_path=spec_authority_path,
                canonical_semantic_source=True,
                canonical_prompt_boundary=False,
                consumers=("site_model_interface_design", "pagegen", "runtime_backend", "validator"),
                reason=str(reuse_decision.get("reason") or "missing_or_invalid_cached_spec"),
                details={
                    "reuse_policy": "canonical_output_artifact_present",
                    "reused": reused_page_environment_specs,
                    "would_call_llm": bool(reuse_decision.get("would_call_llm", not reused_page_environment_specs)),
                    "manual_invalidation_policy": reuse_decision.get("manual_invalidation_policy"),
                    "artifact_path": spec_authority_relpath,
                },
            )

            if not reused_page_environment_specs:
                page_environment_specs = build_page_environment_specs(
                    site_model,
                    Path(self.output_dir),
                    include_route_affordance_candidates=self.config_manager.get(
                        "environment_action_route_planner_include_route_affordance_candidates",
                        False,
                    ),
                )
                if pre_pageenv_global_execution_schema:
                    page_environment_specs = project_global_schema_into_page_environment_specs(
                        page_environment_specs,
                        pre_pageenv_global_execution_schema,
                        source=pre_pageenv_global_execution_schema_source,
                    )


                logger.log_info(
                    "Page environment planning mode: "
                    f"{page_environment_planning_mode} "
                    f"(unified_planning_enabled={unified_planning_enabled})"
                )

                if unified_planning_enabled:
                    page_environment_specs = await enrich_page_environment_specs_async(
                        site_model,
                        Path(self.output_dir),
                        page_environment_specs,
                        page_behaviors=site_model.structure.page_behaviors if site_model else {},
                        model=self.config_manager.get(
                            "page_environment_spec_enrichment_model",
                            self.config_manager.get("deployment", None),
                        ),
                        reasoning_effort=self.config_manager.get(
                            "page_environment_spec_enrichment_reasoning_effort",
                            "medium",
                        ),
                        max_query_groups_per_call=self.config_manager.get(
                            "page_environment_spec_enrichment_max_query_groups_per_call",
                            12,
                        ),
                        max_component_pages_per_call=self.config_manager.get(
                            "page_environment_spec_enrichment_max_component_pages_per_call",
                            6,
                        ),
                        component_projection_input_budget_tokens=int(self.config_manager.get(
                            "page_environment_spec_enrichment_component_projection_input_budget_tokens",
                            16000,
                        ) or 16000),
                        component_projection_repair_max_rounds=int(self.config_manager.get(
                            "page_environment_spec_enrichment_component_projection_repair_max_rounds",
                            3,
                        ) or 3),
                        max_concurrent=self.config_manager.get("max_concurrent", 8),
                        max_tokens=self.config_manager.get(
                            "page_environment_spec_enrichment_max_tokens",
                            16000,
                        ),
                        run_phase_3_cross_page_decisions=True,
                        phase_3_logicflow=self.config_manager.get(
                            "page_environment_phase_3_logicflow",
                            self.config_manager.get(
                                "enrich_phase_3_logicflow",
                                "closure_default_review_only",
                            ),
                        ),
                        phase_3_max_tokens=self.config_manager.get(
                            "enrich_phase_3_max_tokens", 12000
                        ),
                        logger=logger,
                    )
                else:

                    if self.config_manager.get("page_environment_conflict_resolution_enabled", False):
                        page_environment_specs = await resolve_page_environment_conflicts_async(
                            site_model,
                            Path(self.output_dir),
                            page_environment_specs,
                            model=self.config_manager.get(
                                "page_environment_conflict_resolver_model",
                                self.config_manager.get("deployment", None),
                            ),
                            reasoning_effort=self.config_manager.get(
                                "page_environment_conflict_resolver_reasoning_effort",
                                "medium",
                            ),
                            max_groups_per_call=self.config_manager.get(
                                "page_environment_conflict_resolver_max_groups_per_call",
                                8,
                            ),
                            max_concurrent=self.config_manager.get("max_concurrent", 8),
                            max_tokens=self.config_manager.get(
                                "page_environment_conflict_resolver_max_tokens",
                                12000,
                            ),
                        )
                    if self.config_manager.get("page_environment_spec_enrichment_enabled", False):
                        page_environment_specs = await enrich_page_environment_specs_async(
                            site_model,
                            Path(self.output_dir),
                            page_environment_specs,
                            model=self.config_manager.get(
                                "page_environment_spec_enrichment_model",
                                self.config_manager.get("deployment", None),
                            ),
                            reasoning_effort=self.config_manager.get(
                                "page_environment_spec_enrichment_reasoning_effort",
                                "medium",
                            ),
                            max_query_groups_per_call=self.config_manager.get(
                                "page_environment_spec_enrichment_max_query_groups_per_call",
                                12,
                            ),
                            max_component_pages_per_call=self.config_manager.get(
                                "page_environment_spec_enrichment_max_component_pages_per_call",
                                6,
                            ),
                            component_projection_input_budget_tokens=int(self.config_manager.get(
                                "page_environment_spec_enrichment_component_projection_input_budget_tokens",
                                16000,
                            ) or 16000),
                            component_projection_repair_max_rounds=int(self.config_manager.get(
                                "page_environment_spec_enrichment_component_projection_repair_max_rounds",
                                3,
                            ) or 3),
                            max_concurrent=self.config_manager.get("max_concurrent", 8),
                            max_tokens=self.config_manager.get(
                                "page_environment_spec_enrichment_max_tokens",
                                16000,
                            ),
                            run_phase_3_cross_page_decisions=False,
                        )
            screenshot_render_roles = _load_screenshot_render_roles(self.output_dir)
            if screenshot_render_roles and isinstance(page_environment_specs, dict):
                page_environment_specs["screenshot_render_roles"] = screenshot_render_roles
            close_page_environment_contracts(
                page_environment_specs,
                render_role_by_step=screenshot_render_roles,
            )
            runtime_state_observations_for_autocomplete = (
                getattr(site_model, "runtime_state_observations", {})
                if site_model is not None
                else {}
            )
            if not isinstance(runtime_state_observations_for_autocomplete, dict):
                runtime_state_observations_for_autocomplete = {}
            if not runtime_state_observations_for_autocomplete:
                runtime_state_path = os.path.join(
                    self.output_dir,
                    "phase_instance_splitting",
                    "runtime_state_observations.json",
                )
                if os.path.exists(runtime_state_path):
                    try:
                        with open(runtime_state_path, "r", encoding="utf-8") as f:
                            runtime_state_observations_for_autocomplete = json.load(f) or {}
                    except Exception:
                        runtime_state_observations_for_autocomplete = {}
            autocomplete_materialization_report = materialize_autocomplete_suggestion_contracts(
                page_environment_specs,
                runtime_state_observations=runtime_state_observations_for_autocomplete,
            )
            pom.write_json(
                "page_environment",
                "debug",
                "page_environment_spec/autocomplete_suggestion_materialization.json",
                autocomplete_materialization_report,
            )


            try:
                from trace2world_pipeline.app_spec.autocomplete_evidence_extraction import (
                    extract_autocomplete_evidence_async,
                    hydrate_pes_from_autocomplete_evidence,
                )
                _ac_frame_dir = Path(self.output_dir) / "phase_frame_analysis" / "frame_observations"
                if _ac_frame_dir.is_dir():
                    _ac_evidence = await extract_autocomplete_evidence_async(
                        page_environment_specs,
                        _ac_frame_dir,
                        model=self.config_manager.get("deployment"),
                        max_concurrent=int(self.config_manager.get("max_concurrent", 8) or 8),
                    )
                    pom.write_json(
                        "content_extraction",
                        "formal",
                        "autocomplete_evidence/autocomplete_evidence_by_page.json",
                        _ac_evidence,
                    )
                    _ac_hydration = hydrate_pes_from_autocomplete_evidence(
                        page_environment_specs, _ac_evidence,
                    )
                    logger.log_info(
                        f"Autocomplete evidence: {_ac_evidence['summary'].get('pages_with_evidence', 0)} pages "
                        f"with {_ac_evidence['summary'].get('total_visible_rows', 0)} rows, "
                        f"hydrated {_ac_hydration.get('hydrated_count', 0)} PES query_states"
                    )
            except Exception as exc:
                logger.log_warning(f"Autocomplete evidence extraction failed (non-blocking): {exc}")

            spec_self_healing_report = {
                "format": "spec_self_healing_fixloop",
                "schema_revision": 1,
                "summary": {
                    "enabled": False,
                    "reason": "spec_self_healing_enabled=false",
                },
            }
            if self.config_manager.get("spec_self_healing_enabled", True):
                from trace2world_pipeline.app_spec.spec_fixloop import run_spec_self_healing_loop
                from trace2world_pipeline.app_spec.spec_executability_eval import (
                    evaluate_spec_executability as _evaluate_spec_executability_for_fixloop,
                    merge_domain_contract_issues_into_spec_eval as _merge_domain_contract_issues_for_fixloop,
                )
                from trace2world_pipeline.app_spec.typed_action_contracts import (
                    compile_domain_contracts as _compile_domain_contracts_for_fixloop,
                    compile_typed_action_contracts as _compile_typed_action_contracts_for_fixloop,
                )

                def _evaluate_spec_hard_gate_for_fixloop(spec_payload: Mapping[str, Any]) -> Dict[str, Any]:
                    base_eval = _evaluate_spec_executability_for_fixloop(spec_payload)
                    typed_contracts = _compile_typed_action_contracts_for_fixloop(spec_payload)
                    domain_contracts = _compile_domain_contracts_for_fixloop(
                        typed_contracts,
                        page_environment_specs=spec_payload,
                    )
                    return _merge_domain_contract_issues_for_fixloop(base_eval, domain_contracts)

                page_environment_specs, spec_self_healing_report = await run_spec_self_healing_loop(
                    page_environment_specs,
                    max_rounds=int(self.config_manager.get("spec_self_healing_max_rounds", 1) or 1),
                    llm_repair_enabled=bool(self.config_manager.get("spec_self_healing_llm_enabled", False)),
                    model=self.config_manager.get(
                        "spec_self_healing_model",
                        self.config_manager.get("deployment", None),
                    ),
                    reasoning_effort=self.config_manager.get(
                        "spec_self_healing_reasoning_effort",
                        "medium",
                    ),
                    max_tokens=int(self.config_manager.get("spec_self_healing_max_tokens", 4000) or 4000),
                    executability_evaluator=_evaluate_spec_hard_gate_for_fixloop,
                )
                healing_summary = (
                    spec_self_healing_report.get("summary", {})
                    if isinstance(spec_self_healing_report, dict)
                    else {}
                )
                logger.log_info(
                    "Ran spec self-healing fixloop "
                    f"(initial_blocking={healing_summary.get('initial_blocking_issue_count', 0)}, "
                    f"final_blocking={healing_summary.get('final_blocking_issue_count', 0)}, "
                    f"initial_warnings={healing_summary.get('initial_warning_issue_count', 0)}, "
                    f"final_warnings={healing_summary.get('final_warning_issue_count', 0)})"
                )
            _state_delta_signatures: Dict[str, Any] = {}
            _state_delta_path = Path(self.output_dir) / "phase_l0_fidelity" / "formal" / "trace_state_delta_signatures.json"
            if _state_delta_path.is_file():
                page_environment_specs, state_delta_spec_injection_report, _state_delta_signatures = (
                    inject_state_delta_spec_contracts_from_path(
                        page_environment_specs,
                        _state_delta_path,
                    )
                )
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/state_delta_spec_injection.json",
                    state_delta_spec_injection_report,
                )
                injection_summary = (
                    state_delta_spec_injection_report.get("summary", {})
                    if isinstance(state_delta_spec_injection_report, dict)
                    else {}
                )
                logger.log_info(
                    "State-delta spec injection "
                    f"materialized={injection_summary.get('materialized_action_count', 0)} actions, "
                    f"signatures={injection_summary.get('materialized_signature_count', 0)}"
                )
            page_environment_by_filename = (
                page_environment_specs.get("page_environment_specs_by_filename", {})
                if isinstance(page_environment_specs, dict)
                else {}
            )
            spec_authority_shape = _page_environment_spec_authority_shape(page_environment_specs)
            if not spec_authority_shape.get("ok"):
                raise RuntimeError(
                    "PageEnvironmentSpec authority shape gate failed: "
                    f"{spec_authority_shape.get('error_count', 0)} error(s): "
                    f"{spec_authority_shape.get('errors', [])[:5]}"
                )
            from trace2world_pipeline.app_spec.page_execution_contracts import (
                build_route_execution_contracts_from_page_environment_specs,
            )

            route_execution_contracts = build_route_execution_contracts_from_page_environment_specs(
                page_environment_specs
            )
            _publish_to_site_model("route_execution_contracts", route_execution_contracts)
            route_execution_contracts_path = pom.write_json(
                "page_environment",
                "formal",
                "semantic_api/08_spec_compiled_execution_views/route_execution_contracts.json",
                route_execution_contracts,
                register=False,
            )
            register_artifact(
                Path(self.output_dir),
                route_execution_contracts_path,
                artifact_id="semantic_api.spec_compiled_execution_views.route_execution_contracts",
                phase="site_model_semantic_design",
                artifact_kind="det_projection",
                api_or_det="det",
                canonical_source=False,
                logical_owner="site_model.page_execution_contracts",
                producer="site_model.page_execution_contracts.build_route_execution_contracts_from_page_environment_specs",
                allowed_consumers=("runtime_backend", "replay", "validator"),
                semantic_role="spec_authoritative_route_execution_contracts",
                debug_only=False,
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="projection",
                source_selection={"derived_from": [spec_authority_relpath]},
                refresh_filesystem_summary=False,
            )
            if isinstance(page_environment_by_filename, dict):
                page_environment_runtime_specs = {
                    str(filename): spec
                    for filename, spec in page_environment_by_filename.items()
                    if filename and isinstance(spec, dict)
                }
                page_environment_projections = {
                    str(filename): compact_page_environment_spec_for_prompt(spec)
                    for filename, spec in page_environment_by_filename.items()
                    if filename and isinstance(spec, dict)
                }

            debug_page_env_dir = str(pom.debug("page_environment"))
            semantic_page_env_dir = str(
                pom.formal("page_environment", "semantic_api/06_page_environment_specs")
            )
            page_environment_evaluation = evaluate_page_environment_specs(page_environment_specs)
            from trace2world_pipeline.app_spec.spec_executability_eval import (
                build_contract_replay_plan,
                build_pagegen_execution_view_by_page,
                build_pagegen_spec_view_by_page,
                build_returnability_eval,
                build_runtime_binding_manifest,
                build_spec_issue_router_report,
                build_state_delta_spec_coverage_report,
                evaluate_spec_executability,
                merge_domain_contract_issues_into_spec_eval,
                merge_state_delta_coverage_into_spec_eval,
            )

            rec_by_filename: Dict[str, Any] = {}
            if isinstance(site_model.page_execution_contracts, dict):
                for _pec_page in site_model.page_execution_contracts.get("pages", []):
                    if isinstance(_pec_page, dict):
                        _rec = _pec_page.get("route_execution_contract")
                        _fn = str(_pec_page.get("filename") or "").strip()
                        if isinstance(_rec, dict) and _fn:
                            rec_by_filename[_fn] = _rec
            spec_executability_eval = evaluate_spec_executability(
                page_environment_specs,
                route_execution_contracts_by_filename=rec_by_filename,
                step_boundary_contracts=step_boundary_contracts,
            )
            from trace2world_pipeline.app_spec.typed_action_contracts import (
                compile_typed_action_contracts,
                compile_domain_contracts,
            )
            from trace2world_pipeline.app_spec.action_effect_contracts import (
                build_action_effect_contracts,
                build_dom_binding_contracts,
                build_runtime_transaction_contracts,
                collect_action_effect_required_refs,
                project_action_effect_contracts,
                validate_action_effect_contracts,
            )
            from trace2world_pipeline.app_spec.global_schema_downstream_projection import (
                build_downstream_required_ref_payload,
                build_downstream_schema_projection,
            )
            typed_action_contracts = compile_typed_action_contracts(page_environment_specs)
            domain_contracts = compile_domain_contracts(typed_action_contracts, page_environment_specs=page_environment_specs)
            action_effect_skipped_steps: List[Dict[str, Any]] = []
            skipped_step_keys: set[Tuple[str, int]] = set()
            for _reference_trace in self._load_reference_traces_for_downstream():
                for _skipped_step in getattr(_reference_trace, "skipped_steps", []) or []:
                    if not isinstance(_skipped_step, dict):
                        continue
                    _trace_id = str(_skipped_step.get("trace_id") or getattr(_reference_trace, "trace_id", "") or "").strip()
                    try:
                        _step_index = int(_skipped_step.get("step_index", -1))
                    except (TypeError, ValueError):
                        continue
                    if not _trace_id or _step_index < 0:
                        continue
                    _row = dict(_skipped_step)
                    _row["trace_id"] = _trace_id
                    _row["step_index"] = _step_index
                    action_effect_skipped_steps.append(_row)
                    skipped_step_keys.add((_trace_id, _step_index))
            try:
                _global_reasoning_for_effects = locals().get("global_reasoning") or {}
                if not isinstance(_global_reasoning_for_effects, dict) or not _global_reasoning_for_effects:
                    _global_reasoning_path = Path(self.output_dir) / "phase_global_reasoning" / "global_reasoning_final.json"
                    if _global_reasoning_path.is_file():
                        with open(_global_reasoning_path, encoding="utf-8") as _f:
                            _global_reasoning_for_effects = json.load(_f)
                if isinstance(_global_reasoning_for_effects, dict):
                    for _skipped_step in _global_reasoning_for_effects.get("skipped_steps", []):
                        if not isinstance(_skipped_step, dict):
                            continue
                        _trace_id = str(_skipped_step.get("trace_id") or "").strip()
                        try:
                            _step_index = int(_skipped_step.get("step_index", -1))
                        except (TypeError, ValueError):
                            continue
                        if _trace_id and _step_index >= 0 and (_trace_id, _step_index) not in skipped_step_keys:
                            action_effect_skipped_steps.append(dict(_skipped_step))
                            skipped_step_keys.add((_trace_id, _step_index))
            except Exception as exc:
                raise RuntimeError(f"Failed to load skipped-step authority for downstream contracts: {exc}") from exc
            state_delta_spec_coverage_report: Dict[str, Any] = {}
            if _state_delta_signatures:
                try:
                    state_delta_spec_coverage_report = build_state_delta_spec_coverage_report(
                        _state_delta_signatures,
                        typed_action_contracts,
                        domain_contracts,
                        skipped_steps=action_effect_skipped_steps,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"State-delta spec coverage failed for formal artifact {_state_delta_path}: {exc}"
                    ) from exc
            spec_executability_eval = merge_domain_contract_issues_into_spec_eval(
                spec_executability_eval,
                domain_contracts,
            )
            spec_executability_eval = merge_state_delta_coverage_into_spec_eval(
                spec_executability_eval,
                state_delta_spec_coverage_report,
            )
            spec_issue_router_report = build_spec_issue_router_report(
                spec_executability_eval,
                state_delta_coverage_report=state_delta_spec_coverage_report,
            )
            if int(spec_issue_router_report.get("summary", {}).get("issue_count", 0) or 0) > 0:
                from trace2world_pipeline.app_spec.spec_deterministic_normalizer import (
                    normalize_spec_deterministically as _normalize_spec_deterministically,
                )

                page_environment_specs, post_contract_normalizer_report = _normalize_spec_deterministically(
                    page_environment_specs,
                    spec_issue_router_report,
                )
                normalizer_summary = (
                    post_contract_normalizer_report.get("summary", {})
                    if isinstance(post_contract_normalizer_report, dict)
                    else {}
                )
                applied_operation_count = int(normalizer_summary.get("applied_operation_count", 0) or 0)
                if applied_operation_count > 0:
                    pom.write_json(
                        "page_environment",
                        "debug",
                        "page_environment_spec/post_contract_deterministic_normalizer.json",
                        post_contract_normalizer_report,
                    )
                    logger.log_info(
                        "Post-contract deterministic spec normalization "
                        f"applied={applied_operation_count} operations"
                    )
                    spec_executability_eval = evaluate_spec_executability(
                        page_environment_specs,
                        route_execution_contracts_by_filename=rec_by_filename,
                        step_boundary_contracts=step_boundary_contracts,
                    )
                    typed_action_contracts = compile_typed_action_contracts(page_environment_specs)
                    domain_contracts = compile_domain_contracts(
                        typed_action_contracts,
                        page_environment_specs=page_environment_specs,
                    )
                    state_delta_spec_coverage_report = {}
                    if _state_delta_signatures:
                        try:
                            state_delta_spec_coverage_report = build_state_delta_spec_coverage_report(
                                _state_delta_signatures,
                                typed_action_contracts,
                                domain_contracts,
                                skipped_steps=action_effect_skipped_steps,
                            )
                        except Exception as exc:
                            raise RuntimeError(
                                "State-delta spec coverage failed after deterministic normalization "
                                f"for formal artifact {_state_delta_path}: {exc}"
                            ) from exc
                    spec_executability_eval = merge_domain_contract_issues_into_spec_eval(
                        spec_executability_eval,
                        domain_contracts,
                    )
                    spec_executability_eval = merge_state_delta_coverage_into_spec_eval(
                        spec_executability_eval,
                        state_delta_spec_coverage_report,
                    )
                    spec_issue_router_report = build_spec_issue_router_report(
                        spec_executability_eval,
                        state_delta_coverage_report=state_delta_spec_coverage_report,
                    )
            pagegen_spec_view_by_page = build_pagegen_spec_view_by_page(spec_executability_eval)
            spec_runtime_binding_manifest = build_runtime_binding_manifest(spec_executability_eval)
            spec_contract_replay_plan = build_contract_replay_plan(
                spec_executability_eval,
                skipped_steps=action_effect_skipped_steps,
            )
            action_effect_contracts = build_action_effect_contracts(
                spec_contract_replay_plan,
                typed_action_contracts,
                skipped_steps=action_effect_skipped_steps,
                domain_contracts=domain_contracts,
                page_boundary_authority=step_boundary_contracts,
            )
            runtime_transaction_contracts = build_runtime_transaction_contracts(
                action_effect_contracts,
            )
            environment_state_artifacts: Dict[str, Any] = {}
            if str(self.config_manager.get("runtime_profile", "") or "").strip() == "state_kernel_sqlite":
                from trace2world_pipeline.state_kernel.compiler import compile_environment_state_artifacts

                seed_artifact = _ensure_site_model_entity_seed(site_model, self.output_dir)
                if seed_artifact.get("materialized"):
                    seed_summary = seed_artifact.get("validation", {}).get("summary", {})
                    logger.log_info(
                        "Materialized canonical entity seed before SQLite state compilation: "
                        f"{seed_summary.get('record_count', 0)} records across "
                        f"{seed_summary.get('entity_count', 0)} entities"
                    )
                state_seed = entity_seed_to_init_data(seed_artifact.get("seed", {}))
                environment_state_artifacts = compile_environment_state_artifacts(
                    global_execution_schema=pre_pageenv_global_execution_schema or {},
                    entity_schemas=getattr(site_model.content, "entity_schemas", []),
                    runtime_transaction_contracts=runtime_transaction_contracts,
                    seed_data=state_seed,
                    source_provenance={
                        "global_schema": "phase_consolidation/schema_registry/global_execution_schema.json",
                        "runtime_transactions": "phase_page_environment/semantic_api/08_spec_compiled_execution_views/runtime_transaction_contracts.json",
                        "entity_seed": "phase_consolidation/site_model/entity_seed.json",
                    },
                )
                logger.log_info(
                    "Compiled SQLite Environment State artifacts "
                    f"(transactions={len(environment_state_artifacts['transaction_manifest']['transactions'])}, "
                    f"fields={len(environment_state_artifacts['runtime_state_registry']['fields'])})"
                )
            global_execution_schema = pre_pageenv_global_execution_schema or {}
            schema_obligations = collect_schema_obligations(
                trace_execution_obligations=trace_execution_obligation_payload,
                runtime_state_obligations=runtime_state_obligation_payload,
                page_environment_specs=page_environment_specs,
                runtime_transactions=runtime_transaction_contracts,
            )
            action_effect_downstream_projection = build_downstream_schema_projection(
                global_execution_schema,
                [
                    *build_downstream_required_ref_payload(
                        page_environment_specs=page_environment_specs,
                        runtime_transactions=runtime_transaction_contracts,
                        schema_obligations_payload=schema_obligations,
                    ).get("required_refs", []),
                    *collect_action_effect_required_refs(action_effect_contracts),
                ],
                source="phase_page_environment.action_effect_contracts",
            )
            action_effect_contracts = project_action_effect_contracts(
                action_effect_contracts,
                action_effect_downstream_projection,
            )
            canonicalized_runtime_state_obligations = (
                canonicalize_runtime_state_obligations_payload(runtime_state_obligation_payload)
                if runtime_state_obligation_payload
                else {}
            )
            schema_candidate_ir_for_context = _load_json_artifact(
                os.path.join(
                    self.output_dir,
                    "phase_consolidation",
                    "debug",
                    "schema_candidates.json",
                )
            )
            schema_context_reference_traces = locals().get("reference_traces") or []
            schema_generation_context = build_schema_generation_context(
                schema_candidate_ir=schema_candidate_ir_for_context
                if isinstance(schema_candidate_ir_for_context, dict)
                else {},
                schema_obligations=schema_obligations,
                traces_context=_build_schema_context_trace_payloads(schema_context_reference_traces),
                page_identity_context={
                    "page_names": sorted(
                        page_environment_specs.get("page_environment_specs_by_filename", {}).keys()
                    )
                    if isinstance(page_environment_specs, dict)
                    and isinstance(page_environment_specs.get("page_environment_specs_by_filename"), dict)
                    else [],
                    "page_identity_summary": "Page identity locates schema projections; stateful search/form/autocomplete values remain global execution fields.",
                },
            )
            schema_obligation_paths = [
                pom.write_json(
                    "consolidation",
                    "debug",
                    "schema_registry/schema_obligations_post_pageenv.json",
                    schema_obligations,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/schema_obligations_post_pageenv.json",
                    schema_obligations,
                    register=False,
                ),
            ]
            if canonicalized_runtime_state_obligations:
                pom.write_json(
                    "consolidation",
                    "debug",
                    "schema_registry/runtime_state_obligations_canonicalized.json",
                    canonicalized_runtime_state_obligations,
                    register=False,
                )
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/runtime_state_obligations_canonicalized.json",
                    canonicalized_runtime_state_obligations,
                    register=False,
                )
            schema_generation_context_paths = [
                pom.write_json(
                    "consolidation",
                    "debug",
                    "schema_registry/schema_generation_context_post_pageenv.json",
                    schema_generation_context,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/schema_generation_context_post_pageenv.json",
                    schema_generation_context,
                    register=False,
                ),
            ]
            global_schema_paths: List[Path] = []
            if not global_execution_schema and schema_obligations.get("obligation_count", 0):
                raise RuntimeError(
                    "Post-PageEnv schema obligations exist but no pre-PageEnv canonical "
                    "global_execution_schema is available; refusing downstream projection."
                )
            if global_execution_schema:
                page_environment_specs = project_global_schema_into_page_environment_specs(
                    page_environment_specs,
                    global_execution_schema,
                    source=pre_pageenv_global_execution_schema_source,
                )
            projected_page_env_paths = [
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/page_environment_specs.json",
                    page_environment_specs,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/06_page_environment_specs/page_environment_specs.json",
                    page_environment_specs,
                    register=False,
                ),
            ]
            logger.log_info(
                "Collected post-PageEnv schema obligations without publishing root schema "
                f"(obligations={schema_obligations.get('obligation_count', 0)}, "
                f"pre_pageenv_schema_fields={len(global_execution_schema.get('global_fields', []))})"
            )
            for path in schema_obligation_paths:
                logger.log_info(f"Writing schema obligations artifact: {path}")
            for path in schema_generation_context_paths:
                logger.log_info(f"Writing schema generation context artifact: {path}")
            for path in global_schema_paths:
                logger.log_info(f"Writing global execution schema artifact: {path}")
            for path in projected_page_env_paths:
                logger.log_info(f"Writing page_environment_specs global schema projection: {path}")
            dom_binding_contracts = build_dom_binding_contracts(
                action_effect_contracts,
                runtime_transaction_contracts,
                downstream_projection=action_effect_downstream_projection,
                page_environment_specs=page_environment_specs,
            )
            effect_contract_validation = validate_action_effect_contracts(
                action_effect_contracts,
                runtime_transaction_contracts,
                dom_binding_contracts,
                skipped_steps=action_effect_skipped_steps,
                contract_replay_plan=spec_contract_replay_plan,
            )
            spec_returnability_eval = build_returnability_eval(spec_executability_eval)
            pagegen_execution_view_by_page = build_pagegen_execution_view_by_page(
                spec_executability_eval,
                typed_action_contracts=typed_action_contracts,
                domain_contracts=domain_contracts,
                action_effect_contracts=action_effect_contracts,
                runtime_transaction_contracts=runtime_transaction_contracts,
                dom_binding_contracts=dom_binding_contracts,
                global_execution_schema=global_execution_schema,
                page_environment_specs=page_environment_specs,
            )
            spec_execution_views_by_page = pagegen_execution_view_by_page.get("views_by_filename", {})
            spec_executability_status = (
                spec_executability_eval.get("summary", {}).get("status", "unknown")
                if isinstance(spec_executability_eval, dict)
                else "unknown"
            )
            page_environment_blocking_error_count = int(
                page_environment_evaluation.get("summary", {})
                .get("acceptance", {})
                .get("blocking_error_count", 0)
                or 0
            )
            logger.log_info("Evaluated page_environment_specs for AppSpec semantic design")
            logger.log_info(
                "Evaluated page_environment_specs executability "
                f"(status={spec_executability_status}, "
                f"blocking_issues={spec_executability_eval.get('summary', {}).get('blocking_issue_count', 0)})"
            )
            for target_dir in (semantic_page_env_dir, debug_page_env_dir):
                spec_output_path = os.path.join(target_dir, "page_environment_specs.json")
                rel_spec_output_path = os.path.relpath(spec_output_path, self.output_dir)
                if reused_page_environment_specs and os.path.exists(spec_output_path):
                    logger.log_info(
                        "Preserved reused page_environment_specs mirror: "
                        f"{rel_spec_output_path}"
                    )
                else:
                    is_debug_write = os.path.abspath(target_dir) == os.path.abspath(debug_page_env_dir)
                    if is_debug_write:
                        pom.write_json("page_environment", "debug",
                                       "page_environment_spec/page_environment_specs.json",
                                       page_environment_specs,
                                       )
                    else:
                        pom.write_json("page_environment", "formal",
                                       "semantic_api/06_page_environment_specs/page_environment_specs.json",
                                       page_environment_specs,
                                       )
                    logger.log_info(f"Writing page_environment_specs artifact: {rel_spec_output_path}")
                is_debug_mirror = os.path.abspath(target_dir) == os.path.abspath(debug_page_env_dir)
                is_canonical_semantic = not is_debug_mirror
                logger.log_info(f"Registering page_environment_specs artifact: {rel_spec_output_path}")
                register_artifact(
                    Path(self.output_dir),
                    Path(spec_output_path),
                    artifact_id=(
                        "debug.page_environment_specs"
                        if is_debug_mirror
                        else "semantic_api.page_environment_specs"
                    ),
                    phase="site_model_semantic_design",
                    artifact_kind=(
                        "debug_export"
                        if is_debug_mirror
                        else "final_consumer_artifact"
                    ),
                    api_or_det="mixed",
                    canonical_source=is_canonical_semantic,
                    logical_owner="site_model.page_environment_spec",
                    producer="trace_generator.generate.page_environment_specs",
                    allowed_consumers=(
                        ("developer", "cache_resume")
                        if is_debug_mirror
                        else ("site_model_interface_design", "pagegen", "runtime_backend", "validator")
                    ),
                    debug_only=is_debug_mirror,
                    semantic_role="authoritative_environment_spec",
                    canonical_semantic_source=is_canonical_semantic,
                    canonical_prompt_boundary=False,
                    artifact_role="debug_cache" if is_debug_mirror else "authority",
                    source_selection=(
                        {"canonical_path": spec_authority_relpath}
                        if is_debug_mirror
                        else None
                    ),
                    refresh_filesystem_summary=False,
                )
            logger.log_info("Finished page_environment_specs artifact registration")
            eval_output_path = pom.write_json(
                "page_environment", "formal",
                "validation/spec/evaluation.json",
                page_environment_evaluation,
                register=False,
            )
            debug_eval_output_path = pom.write_json(
                "page_environment", "debug",
                "page_environment_spec/evaluation.json",
                page_environment_evaluation,
                register=False,
            )
            page_environment_eval_output_path = str(eval_output_path)
            for path in (eval_output_path, debug_eval_output_path):
                logger.log_info(f"Writing page_environment_specs evaluation: {path}")
            spec_executability_output_path = pom.write_json(
                "page_environment",
                "formal",
                "semantic_api/07_spec_executability_eval/spec_executability_eval.json",
                spec_executability_eval,
                register=False,
            )
            debug_spec_executability_output_path = pom.write_json(
                "page_environment",
                "debug",
                "page_environment_spec/spec_executability_eval.json",
                spec_executability_eval,
                register=False,
            )
            for path in (spec_executability_output_path, debug_spec_executability_output_path):
                logger.log_info(f"Writing page_environment_specs executability evaluation: {path}")
            if state_delta_spec_coverage_report:
                state_delta_coverage_paths = [
                    pom.write_json(
                        "page_environment",
                        "formal",
                        "semantic_api/07_spec_executability_eval/state_delta_spec_coverage_report.json",
                        state_delta_spec_coverage_report,
                        register=False,
                    ),
                    pom.write_json(
                        "page_environment",
                        "debug",
                        "page_environment_spec/state_delta_spec_coverage_report.json",
                        state_delta_spec_coverage_report,
                        register=False,
                    ),
                ]
                for path in state_delta_coverage_paths:
                    logger.log_info(f"Writing page_environment_specs state-delta coverage report: {path}")
            spec_issue_router_paths = [
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/07_spec_executability_eval/spec_issue_router_report.json",
                    spec_issue_router_report,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/spec_issue_router_report.json",
                    spec_issue_router_report,
                    register=False,
                ),
            ]
            for path in spec_issue_router_paths:
                logger.log_info(f"Writing page_environment_specs issue router report: {path}")
            spec_self_healing_paths = [
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/07_spec_executability_eval/spec_self_healing_fixloop.json",
                    spec_self_healing_report,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/spec_self_healing_fixloop.json",
                    spec_self_healing_report,
                    register=False,
                ),
            ]
            for path in spec_self_healing_paths:
                logger.log_info(f"Writing page_environment_specs self-healing fixloop report: {path}")
            render_context = build_render_context(site_model.environment_profile)
            render_context_by_page = {
                str(proto.filename): {
                    "format": "page_render_context_ref",
                    "schema_revision": 1,
                    "authority_ref": (
                        "phase_page_environment/semantic_api/08_spec_compiled_execution_views/"
                        "render_context.json"
                    ),
                    "profile_hash": render_context["profile_hash"],
                    "platform": render_context["platform"],
                    "viewport": render_context["viewport"],
                }
                for proto in site_model.structure.page_prototypes
                if str(getattr(proto, "filename", "") or "")
            }
            spec_compiled_views = {
                "format": "spec_compiled_execution_views",
                "schema_revision": 1,
                "source": "spec_executability_eval",
                "spec_self_healing_fixloop": spec_self_healing_report,
                "spec_issue_router_report": spec_issue_router_report,
                "pagegen_spec_view_by_page": pagegen_spec_view_by_page,
                "pagegen_execution_view_by_page": pagegen_execution_view_by_page,
                "typed_action_contracts": typed_action_contracts,
                "domain_contracts": domain_contracts,
                "step_boundary_contracts": step_boundary_contracts,
                "action_effect_contracts": action_effect_contracts,
                "runtime_transaction_contracts": runtime_transaction_contracts,
                "environment_state_artifacts": environment_state_artifacts,
                "action_effect_downstream_schema_projection": action_effect_downstream_projection,
                "schema_obligations": schema_obligations,
                "schema_generation_context": schema_generation_context,
                "global_execution_schema": global_execution_schema,
                "dom_binding_contracts": dom_binding_contracts,
                "effect_contract_validation": effect_contract_validation,
                "runtime_binding_manifest": spec_runtime_binding_manifest,
                "contract_replay_plan": spec_contract_replay_plan,
                "render_context": render_context,
                "render_context_by_page": render_context_by_page,
            }
            compiled_view_paths = [
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/step_boundary_contracts.json",
                    step_boundary_contracts,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/render_context.json",
                    render_context,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/render_context_by_page.json",
                    render_context_by_page,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/pagegen_spec_view_by_page.json",
                    pagegen_spec_view_by_page,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/runtime_binding_manifest.json",
                    spec_runtime_binding_manifest,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/contract_replay_plan.json",
                    spec_contract_replay_plan,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/typed_action_contracts.json",
                    typed_action_contracts,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/domain_contracts.json",
                    domain_contracts,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/action_effect_contracts.json",
                    action_effect_contracts,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/runtime_transaction_contracts.json",
                    runtime_transaction_contracts,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/action_effect_downstream_schema_projection.json",
                    action_effect_downstream_projection,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/dom_binding_contracts.json",
                    dom_binding_contracts,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/effect_contract_validation.json",
                    effect_contract_validation,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/pagegen_execution_view_by_page.json",
                    pagegen_execution_view_by_page,
                    register=False,
                ),
                pom.write_json(
                    "page_environment",
                    "debug",
                    "page_environment_spec/spec_compiled_execution_views.json",
                    spec_compiled_views,
                    register=False,
                ),
            ]
            if environment_state_artifacts:
                for artifact_name, artifact_payload in environment_state_artifacts.items():
                    compiled_view_paths.append(
                        pom.write_json(
                            "page_environment",
                            "formal",
                            f"semantic_api/08_spec_compiled_execution_views/{artifact_name}.json",
                            artifact_payload,
                            register=False,
                        )
                    )
            for path in compiled_view_paths:
                logger.log_info(f"Writing spec-compiled execution view: {path}")
            accepted_trace_ids_for_coverage = sorted(
                getattr(self, "_active_trace_ids", set()) or self._discover_active_trace_ids_from_config()
            )
            if accepted_trace_ids_for_coverage:
                materialized_bindings_for_coverage = locals().get("obs_bindings") or {}
                if not isinstance(materialized_bindings_for_coverage, dict):
                    materialized_bindings_for_coverage = {}
                global_reasoning_for_coverage = _load_global_reasoning_for_trace_coverage(
                    self.output_dir,
                    locals().get("global_reasoning") or locals().get("_global_reasoning_for_effects") or {},
                )
                trace_coverage_report = _build_trace_coverage_closure_report(
                    accepted_trace_ids=accepted_trace_ids_for_coverage,
                    global_reasoning=global_reasoning_for_coverage,
                    cluster_assignments=getattr(site_model, "cluster_assignments", {}) or {},
                    materialized_bindings=materialized_bindings_for_coverage,
                    page_environment_specs=page_environment_specs,
                    contract_replay_plan=spec_contract_replay_plan,
                    action_effect_contracts=action_effect_contracts,
                )
                trace_coverage_path = pom.write_json(
                    "page_environment",
                    "formal",
                    "semantic_api/08_spec_compiled_execution_views/trace_coverage_closure_report.json",
                    trace_coverage_report,
                    register=False,
                )
                logger.log_info(f"Writing trace coverage closure report: {trace_coverage_path}")
                trace_coverage_hard_gate_message = _trace_coverage_hard_gate_message(
                    trace_coverage_report,
                )
                if trace_coverage_hard_gate_message:
                    logger.log_warning(trace_coverage_hard_gate_message)
                    raise RuntimeError(trace_coverage_hard_gate_message)
            spec_hard_gate_message = _spec_executability_hard_gate_message(
                spec_executability_eval,
                hard_gate_enabled=bool(
                    self.config_manager.get("spec_executability_hard_gate_enabled", True)
                ),
            )
            if spec_hard_gate_message:
                logger.log_warning(spec_hard_gate_message)
                raise RuntimeError(spec_hard_gate_message)
            effect_hard_gate_message = _effect_contract_hard_gate_message(
                effect_contract_validation,
                hard_gate_enabled=bool(
                    self.config_manager.get("action_effect_contract_hard_gate_enabled", True)
                ),
            )
            if effect_hard_gate_message:
                logger.log_warning(effect_hard_gate_message)
                raise RuntimeError(effect_hard_gate_message)
            logger.log_info("Writing full spec route audit")
            spec_audit_dir = pom.debug("page_environment", "spec_audit")
            write_full_spec_route_audit(
                Path(self.output_dir), page_environment_specs, page_environment_evaluation,
                audit_dir=spec_audit_dir,
            )
            logger.log_info("Finished full spec route audit")
            route_affordances_final = build_route_affordances_final(page_environment_specs)
            route_affordances_final_path = pom.write_json(
                "page_environment", "formal",
                "projections/page_environment/route_affordances_final.json",
                route_affordances_final,
                register=False,
            )
            register_artifact(
                Path(self.output_dir),
                route_affordances_final_path,
                artifact_id="projections.page_environment.route_affordances_final",
                phase="site_model_semantic_design",
                artifact_kind="det_projection",
                api_or_det="det",
                canonical_source=False,
                logical_owner="page_environment_spec",
                producer="site_model.page_environment_spec.build_route_affordances_final",
                allowed_consumers=("runtime_backend", "replay", "validator"),
                semantic_role="route_affordances_final",
                debug_only=False,
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="projection",
                source_selection={"derived_from": spec_authority_relpath},
                notes="Final route projection derived from PES; candidate affordances are not consumed downstream.",
                refresh_filesystem_summary=False,
            )
            logger.log_info(
                "Materialized route_affordances_final.json "
                f"({route_affordances_final.get('route_count', 0)} routes)"
            )
            shell_interaction_broadcast = (
                page_environment_specs.get("shell_interaction_broadcast", {})
                if isinstance(page_environment_specs, dict)
                else {}
            )
            if not isinstance(shell_interaction_broadcast, dict) or not shell_interaction_broadcast:
                shell_interaction_broadcast = {
                    "format": "shell_interaction_broadcast",
                    "schema_revision": 1,
                    "source": "validated_route_action_or_route_affordance",
                    "propagation_count": 0,
                    "propagated_routes": [],
                    "propagated_interactions": [],
                    "rejected_controls": [],
                }
            shell_interaction_broadcast_path = pom.write_json(
                "page_environment", "formal",
                "projections/page_environment/shell_interaction_broadcast.json",
                shell_interaction_broadcast,
                register=False,
            )
            shell_navigation_contract = (
                shell_interaction_broadcast.get("shell_navigation_contract", {})
                if isinstance(shell_interaction_broadcast, dict)
                else {}
            )
            if not isinstance(shell_navigation_contract, dict) or not shell_navigation_contract:
                shell_navigation_contract = {
                    "format": "shell_navigation_contract",
                    "schema_revision": 1,
                    "authority": "observed_shell_controls",
                    "slots": [],
                    "families": [],
                    "summary": {"slot_count": 0},
                }
            shell_navigation_contract_path = pom.write_json(
                "page_environment", "formal",
                "projections/page_environment/shell_navigation_contract.json",
                shell_navigation_contract,
                register=False,
            )
            register_artifact(
                Path(self.output_dir),
                shell_interaction_broadcast_path,
                artifact_id="projections.page_environment.shell_interaction_broadcast",
                phase="page_environment",
                artifact_kind="det_projection",
                api_or_det="det",
                canonical_source=False,
                logical_owner="page_environment_spec",
                producer="site_model.page_environment_spec.apply_shell_interaction_broadcast",
                allowed_consumers=("pagegen", "debug_visualization", "validator"),
                semantic_role="shell_interaction_broadcast",
                debug_only=False,
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="projection",
                source_selection={"derived_from": spec_authority_relpath},
                notes="Audit of shell route and in-page interaction bindings broadcast from validated PES evidence.",
                refresh_filesystem_summary=False,
            )
            register_artifact(
                Path(self.output_dir),
                shell_navigation_contract_path,
                artifact_id="projections.page_environment.shell_navigation_contract",
                phase="page_environment",
                artifact_kind="det_projection",
                api_or_det="det",
                canonical_source=False,
                logical_owner="page_environment_spec",
                producer="site_model.page_environment_spec.apply_shell_interaction_broadcast",
                allowed_consumers=("pagegen", "debug_visualization", "validator"),
                semantic_role="shell_navigation_contract",
                debug_only=False,
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="projection",
                source_selection={"derived_from": spec_authority_relpath},
                notes="Current-run shell slot target authority consumed by shell_interaction_broadcast.",
                refresh_filesystem_summary=False,
            )
            logger.log_info(
                "Materialized shell_interaction_broadcast.json "
                f"({shell_interaction_broadcast.get('propagation_count', 0)} broadcast bindings)"
            )
            logger.log_info(
                "Materialized shell_navigation_contract.json "
                f"({shell_navigation_contract.get('summary', {}).get('slot_count', 0)} slots)"
            )
            try:
                from trace2world_pipeline.app_spec.interface_requirement_projection import (
                    build_interface_requirement_projection,
                    evaluate_interface_requirement_projection,
                )

                interface_resume_cache_path = _interface_requirement_projection_cache_path(self.output_dir)
                reused_interface_requirement_projection = False
                interface_reuse_source_path = ""
                cached_interface, interface_reuse_source_path = (
                    _load_reusable_interface_requirement_projection(
                        self.output_dir,
                    )
                )
                if cached_interface is not None:
                    interface_requirement_projection = cached_interface or {}
                    reused_interface_requirement_projection = True
                    logger.log_info(
                        "Reused interface_requirement_projection.json from output artifact "
                        f"({interface_requirement_projection.get('summary', {}).get('requirement_count', 0)} requirements)"
                        f" source={os.path.relpath(interface_reuse_source_path, self.output_dir)}"
                    )

                if not reused_interface_requirement_projection:
                    interface_requirement_projection = build_interface_requirement_projection(page_environment_specs)
                if not (
                    reused_interface_requirement_projection
                    and os.path.exists(interface_resume_cache_path)
                ):
                    interface_cache_path = pom.write_json(
                        "interface_design",
                        "formal",
                        "interface_requirements/interface_requirement_projection.json",
                        interface_requirement_projection,
                        register=False,
                    )
                    register_artifact(
                        Path(self.output_dir),
                        interface_cache_path,
                        artifact_id="projections.interface_requirement_projection",
                        phase="site_model_semantic_design",
                        artifact_kind="det_projection",
                        api_or_det="det",
                        canonical_source=False,
                        logical_owner="site_model.interface_requirement_projection",
                        producer="site_model.interface_requirement_projection.build_interface_requirement_projection",
                        allowed_consumers=("site_model_interface_design", "runtime_backend", "pagegen", "validator"),
                        semantic_role="spec_to_interface_requirements",
                        canonical_semantic_source=False,
                        canonical_prompt_boundary=False,
                        debug_only=False,
                        artifact_role="projection",
                        source_selection={
                            "derived_from": spec_authority_relpath,
                            "canonical_path": "phase_interface_design/interface_requirements/interface_requirement_projection.json",
                        },
                        refresh_filesystem_summary=False,
                    )
                self._record_resume_plan_decision(
                    logical_artifact="interface_requirement_projection",
                    source="reused_disk" if reused_interface_requirement_projection else "recomputed",
                    phase="PIPE-P5 AppSpec Semantic Design",
                    artifact_path=interface_resume_cache_path,
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    consumers=("site_model_interface_design", "runtime_backend", "pagegen", "validator"),
                    reason=(
                        "canonical_artifact_present"
                        if reused_interface_requirement_projection
                        else "missing_or_stale_projection"
                    ),
                    details={
                        "reuse_policy": "output_artifact_present",
                        "reused": reused_interface_requirement_projection,
                        "manual_invalidation_policy": "delete_or_deprecate_interface_projection_artifact_to_force_recompute",
                        "reuse_source_path": interface_reuse_source_path,
                    },
                )
                logger.log_info("Evaluating interface_requirement_projection")
                interface_requirement_evaluation = evaluate_interface_requirement_projection(interface_requirement_projection)
                requirements_by_id = interface_requirement_projection.get("requirements_by_id", {})
                requirements_by_page = interface_requirement_projection.get("requirements_by_page", {})
                logger.log_info("Injecting interface requirements into page environment projections")
                if isinstance(requirements_by_id, dict) and isinstance(requirements_by_page, dict):
                    for filename, requirement_ids in requirements_by_page.items():
                        if not isinstance(requirement_ids, list):
                            continue
                        requirements = [
                            requirements_by_id[req_id]
                            for req_id in requirement_ids
                            if req_id in requirements_by_id
                        ]
                        if not requirements:
                            continue
                        if filename in page_environment_runtime_specs:
                            page_environment_runtime_specs[filename]["interface_requirements"] = requirements
                        if filename in page_environment_projections:
                            page_environment_projections[filename]["interface_requirements"] = requirements
                semantic_interface_req_dir = os.path.join(self.output_dir, "semantic_api", "07_interface_requirements")
                site_model_interface_req_dir = site_model_dir
                debug_interface_req_dir = str(pom.debug("interface_design", "interface_requirements"))
                for target_dir, artifact_id, canonical in (
                    (site_model_interface_req_dir, "exports.site_model.interface_requirement_projection", True),
                    (semantic_interface_req_dir, "semantic_api.interface_requirement_projection", False),
                    (debug_interface_req_dir, "debug.interface_requirement_projection", False),
                ):
                    projection_path = os.path.join(target_dir, "interface_requirement_projection.json")
                    if reused_interface_requirement_projection and os.path.exists(projection_path):
                        logger.log_info(
                            "Preserved reused interface_requirement_projection mirror: "
                            f"{os.path.relpath(projection_path, self.output_dir)}"
                        )
                    elif canonical:
                        _publish_to_site_model("interface_requirement_projection", interface_requirement_projection)
                    else:
                        is_debug_interface_write = os.path.abspath(target_dir) == os.path.abspath(debug_interface_req_dir)
                        if is_debug_interface_write:
                            pom.write_json("interface_design", "debug",
                                           "interface_requirements/interface_requirement_projection.json",
                                           interface_requirement_projection,
                                           )
                        else:
                            pom.write_json("interface_design", "formal",
                                           "semantic_api/07_interface_requirements/interface_requirement_projection.json",
                                           interface_requirement_projection,
                                           )
                    is_debug_interface_projection = os.path.abspath(target_dir) == os.path.abspath(debug_interface_req_dir)
                    register_artifact(
                        Path(self.output_dir),
                        Path(projection_path),
                        artifact_id=artifact_id,
                        phase="site_model_semantic_design",
                        artifact_kind=(
                            "det_projection"
                            if canonical
                            else "debug_export"
                            if is_debug_interface_projection
                            else "final_consumer_artifact"
                        ),
                        api_or_det="det",
                        canonical_source=False,
                        logical_owner="site_model.interface_requirement_projection",
                        producer="site_model.interface_requirement_projection.build_interface_requirement_projection",
                        allowed_consumers=("site_model_interface_design", "runtime_backend", "pagegen", "validator"),
                        semantic_role="spec_to_interface_requirements",
                        canonical_semantic_source=False,
                        canonical_prompt_boundary=False,
                        debug_only=is_debug_interface_projection,
                        artifact_role=(
                            "projection"
                            if canonical
                            else "debug_export"
                            if is_debug_interface_projection
                            else "final_consumer_artifact"
                        ),
                        source_selection={
                            "derived_from": spec_authority_relpath,
                            "canonical_path": "phase_interface_design/interface_requirements/interface_requirement_projection.json",
                        },
                        refresh_filesystem_summary=False,
                    )
                interface_eval_path = pom.write_json(
                    "interface_design", "formal",
                    "validation/interface_requirements/evaluation.json",
                    interface_requirement_evaluation,
                    register=False,
                )
                register_artifact(
                    Path(self.output_dir),
                    interface_eval_path,
                    artifact_id="validation.interface_requirements.evaluation",
                    phase="site_model_semantic_design",
                    artifact_kind="validation_report",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="site_model.interface_requirement_projection",
                    producer="site_model.interface_requirement_projection.evaluate_interface_requirement_projection",
                    allowed_consumers=("developer", "artifact_inventory"),
                    semantic_role="interface_requirement_projection_evaluation",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    artifact_role="validation",
                    refresh_filesystem_summary=False,
                )
                logger.log_info(
                    "Materialized interface_requirement_projection.json "
                    f"({interface_requirement_projection.get('summary', {}).get('requirement_count', 0)} requirements)"
                )
            except Exception as exc:
                logger.log_warning(f"Failed to build interface requirement projection: {exc}")
            register_artifact(
                Path(self.output_dir),
                Path(eval_output_path),
                artifact_id="validation.spec.evaluation",
                phase="site_model_semantic_design",
                artifact_kind="validation_report",
                api_or_det="det",
                canonical_source=False,
                logical_owner="site_model.page_environment_spec",
                producer="site_model.page_environment_spec.evaluate_page_environment_specs",
                allowed_consumers=("developer", "artifact_inventory"),
                debug_only=False,
                semantic_role="spec_acceptance_evaluation",
                refresh_filesystem_summary=False,
            )
            register_artifact(
                Path(self.output_dir),
                Path(debug_eval_output_path),
                artifact_id="debug.spec.evaluation",
                phase="site_model_semantic_design",
                artifact_kind="debug_export",
                api_or_det="det",
                canonical_source=False,
                logical_owner="site_model.page_environment_spec",
                producer="site_model.page_environment_spec.evaluate_page_environment_specs",
                allowed_consumers=("developer", "cache_resume"),
                debug_only=True,
                semantic_role="spec_acceptance_evaluation",
                source_selection={"canonical_path": "validation/spec/evaluation.json"},
                refresh_filesystem_summary=False,
            )

            extra_page_data = _attach_page_environment_projections_to_extra_page_data(
                output_dir=self.output_dir,
                extra_page_data=extra_page_data,
                page_environment_projections=page_environment_projections,
                logger=logger,
            )
            logger.log_info(
                "Materialized page_environment_specs.json as a AppSpec semantic view "
                f"({len(page_environment_projections)} page projections, "
                f"{page_environment_evaluation.get('summary', {}).get('error_count', 0)} errors)"
            )


            try:
                from trace2world_pipeline.app_spec.step_component_binding import (
                    apply_step_component_bindings_to_pack,
                    build_step_component_bindings,
                )

                specs_by_filename = (
                    page_environment_specs.get("page_environment_specs_by_filename", {})
                    if isinstance(page_environment_specs, dict)
                    else {}
                )
                step_component_bindings_artifact = build_step_component_bindings(
                    l0_execution_pack,
                    specs_by_filename,
                )
                apply_step_component_bindings_to_pack(
                    l0_execution_pack, step_component_bindings_artifact
                )
                pom.write_json(
                    "consolidation",
                    "formal",
                    "l0_execution/l0_execution_pack.json",
                    l0_execution_pack,
                )
                pom.write_json(
                    "consolidation",
                    "formal",
                    "projections/l0_execution/l0_execution_pack.json",
                    l0_execution_pack,
                    register=False,
                )

                from trace2world_pipeline.app_spec.action_slot_registry import (
                    action_slot_conflict_count,
                    build_action_slot_registry,
                )

                _query_domain_fns = _extract_query_domain_filenames(page_environment_specs)

                action_slot_registry = build_action_slot_registry(
                    l0_execution_pack,
                    shell_interaction_broadcast=shell_interaction_broadcast,
                    query_domain_filenames=_query_domain_fns,
                    canonical_route_bindings=locals().get("canonical_route_bindings"),
                )
                _assert_no_formal_fallback_binding_source(
                    "action_slot_registry",
                    action_slot_registry,
                )
                action_slot_registry_path = pom.write_json(
                    "consolidation", "formal",
                    "action_slots/action_slot_registry.json",
                    action_slot_registry,
                    register=False,
                )
                pom.write_json(
                    "consolidation", "formal",
                    "action_slots/unresolved_route_steps.json",
                    action_slot_registry.get("unresolved_route_steps", []),
                    register=False,
                )
                pom.write_json(
                    "consolidation", "debug",
                    "action_slots/action_slot_rejections.json",
                    action_slot_registry.get("action_slot_rejections", []),
                    register=False,
                )
                pom.write_json(
                    "consolidation", "formal",
                    "projections/action_slots/action_slot_registry.json",
                    action_slot_registry,
                    register=False,
                )
                register_artifact(
                    Path(self.output_dir),
                    action_slot_registry_path,
                    artifact_id="consolidation.action_slots.registry",
                    phase="site_model_semantic_design",
                    artifact_kind="det_projection",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="action_slot_registry",
                    producer="site_model.action_slot_registry.build_action_slot_registry",
                    allowed_consumers=(
                        "pagegen",
                        "runtime",
                        "playwright_replay_executor",
                        "pagegen_dom_contract_validator",
                        "developer",
                    ),
                    semantic_role="route_action_slot_registry",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=True,
                    artifact_role="projection",
                    source_selection={
                        "derived_from": [
                            "phase_consolidation/l0_execution/l0_execution_pack.json",
                            "phase_consolidation/site_model/step_component_bindings.json",
                        ],
                        "requires_resolved_component_binding": True,
                    },
                )
                _write_consolidation_recompute_decision(
                    self.output_dir,
                    subphase="action_slots",
                    artifact_id="consolidation.action_slots.registry",
                    artifact_rel_path="phase_consolidation/action_slots/action_slot_registry.json",
                    input_artifacts=[
                        "phase_consolidation/l0_execution/l0_execution_pack.json",
                        "phase_consolidation/canonical_routes/canonical_route_bindings.json",
                        "phase_consolidation/site_model/step_component_bindings.json",
                    ],
                )
                action_slot_conflicts = action_slot_conflict_count(action_slot_registry)
                if action_slot_conflicts:
                    logger.log_warning(
                        "Detected action-slot multi-target route conflict(s): "
                        f"{action_slot_conflicts}. See {action_slot_registry_path}"
                    )
                    if self.config_manager.get("action_slot_route_hard_gate_enabled", True):
                        action_slot_route_hard_gate_error = RuntimeError(
                            "action-slot route hard gate failed during AppSpec "
                            "semantic design: "
                            f"{action_slot_conflicts} conflict(s). "
                            f"See {action_slot_registry_path}."
                        )

                from trace2world_pipeline.app_spec.interaction_surface_contract import (
                    build_interaction_surface_contract,
                )

                interaction_surface_contract = build_interaction_surface_contract(
                    l0_execution_pack,
                    page_environment_specs=page_environment_specs,
                )
                interaction_surface_contract_path = pom.write_json(
                    "consolidation", "formal",
                    "interaction_surfaces/interaction_surface_contract.json",
                    interaction_surface_contract,
                    register=False,
                )
                pom.write_json(
                    "consolidation", "formal",
                    "projections/interaction_surfaces/interaction_surface_contract.json",
                    interaction_surface_contract,
                    register=False,
                )
                register_artifact(
                    Path(self.output_dir),
                    interaction_surface_contract_path,
                    artifact_id="consolidation.interaction_surfaces.contract",
                    phase="site_model_semantic_design",
                    artifact_kind="det_projection",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="interaction_surface_contract",
                    producer="site_model.interaction_surface_contract.build_interaction_surface_contract",
                    allowed_consumers=(
                        "pagegen",
                        "pagegen_dom_contract_validator",
                        "playwright_replay_executor",
                        "developer",
                    ),
                    semantic_role="dom_interaction_surface_requirements",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    artifact_role="projection",
                )
                _write_consolidation_recompute_decision(
                    self.output_dir,
                    subphase="interaction_surfaces",
                    artifact_id="consolidation.interaction_surfaces.contract",
                    artifact_rel_path="phase_consolidation/interaction_surfaces/interaction_surface_contract.json",
                    input_artifacts=[
                        "phase_consolidation/l0_execution/l0_execution_pack.json",
                        spec_authority_relpath,
                    ],
                )

                step_binding_output_path = str(
                    pom.site_model_dir() / "step_component_bindings.json"
                )
                _publish_to_site_model("step_component_bindings", step_component_bindings_artifact)
                register_artifact(
                    Path(self.output_dir),
                    Path(step_binding_output_path),
                    artifact_id="projections.step_component_bindings",
                    phase="site_model_semantic_design",
                    artifact_kind="det_projection",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="step_component_binding",
                    producer="site_model.step_component_binding.build_step_component_bindings",
                    allowed_consumers=(
                        "pagegen_dom_contract_validator",
                        "replay_executor",
                        "developer",
                    ),
                    semantic_role="step_to_component_projection",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    artifact_role="projection",
                )
                s = step_component_bindings_artifact["summary"]
                surf_s = interaction_surface_contract.get("summary", {})
                slot_s = action_slot_registry.get("summary", {})
                logger.log_info(
                    "step_component_bindings: total=%d resolved=%d unresolved=%d not_applicable=%d"
                    % (s["total"], s["resolved"], s["unresolved"], s["not_applicable"])
                )
                logger.log_info(
                    "action_slot_registry: slots=%d components=%d conflicts=%d unresolved_route_steps=%d"
                    % (
                        slot_s.get("route_slot_count", 0),
                        slot_s.get("component_instance_count", 0),
                        slot_s.get("conflict_count", 0),
                        slot_s.get("unresolved_route_step_count", 0),
                    )
                )
                logger.log_info(
                    "interaction_surface_contract: pages=%d surfaces=%d executable_steps=%d"
                    % (
                        surf_s.get("page_count", 0),
                        surf_s.get("surface_count", 0),
                        surf_s.get("executable_step_count", 0),
                    )
                )
            except Exception as exc:
                logger.log_warning(f"Failed to project step component bindings: {exc}")
        except Exception as exc:
            import traceback
            failure_message = (
                "Failed to build page_environment_specs during AppSpec semantic design: "
                f"{exc}. page_environment_spec is the canonical environment authority; "
                "downstream PageGen/runtime/replay must not continue without a closed spec."
            )
            logger.log_warning(f"{failure_message}\n{traceback.format_exc()}")
            raise RuntimeError(failure_message) from exc

        if action_slot_route_hard_gate_error is not None:
            raise action_slot_route_hard_gate_error

        if page_environment_blocking_error_count:
            raise RuntimeError(
                "page_environment_spec blocking gate failed during AppSpec semantic design: "
                f"{page_environment_blocking_error_count} blocking error(s). "
                f"See {page_environment_eval_output_path or 'validation/spec/evaluation.json'}."
            )

        try:


            inferred_merge_audit = self._merge_inferred_routes_into_site_model(
                site_model=site_model,
                page_environment_specs=page_environment_specs,
                logger=logger,
            )
            sm_dir = str(pom.site_model_dir())
            semantic_design_payload = _publish_semantic_design_aggregate(
                site_model,
                {
                    "format": "site_model_semantic_design",
                    "schema_revision": 1,
                    "extra_page_data": extra_page_data,
                    "page_execution_contracts": page_execution_contracts,
                    "page_execution_obligations_by_filename": page_execution_obligations_by_filename,
                    "trace_execution_obligations": trace_execution_obligations,
                    "trace_execution_obligation_payload": trace_execution_obligation_payload,
                    "l0_execution_pack": l0_execution_pack,
                    "step_boundary_contracts": step_boundary_contracts,
                    "canonical_route_bindings": locals().get("canonical_route_bindings", {}),
                    "action_slot_registry": locals().get("action_slot_registry", {}),
                    "interaction_surface_contract": locals().get("interaction_surface_contract", {}),
                    "spec_compiled_execution_views": spec_compiled_views,
                    "page_environment_projections": page_environment_projections,
                    "page_environment_runtime_specs": page_environment_runtime_specs,
                    "page_environment_specs": page_environment_specs,
                    "page_environment_evaluation": page_environment_evaluation,
                    "page_environment_eval_output_path": page_environment_eval_output_path,
                    "interface_requirement_projection": interface_requirement_projection,
                    "interface_requirement_evaluation": interface_requirement_evaluation,
                    "inferred_route_merge_audit": inferred_merge_audit,
                },
            )
            site_model.save(sm_dir)
            required_consolidation_artifacts = [
                "phase_consolidation/site_content/site_content.json",
                "phase_consolidation/task_state_contract/task_state_contract.json",
                "phase_consolidation/interaction_contract/interaction_contract.json",
                "phase_consolidation/task_state_contract/success_evidence_hints.json",
                "phase_consolidation/materialized_bindings/materialized_observation_bindings.json",
                "phase_consolidation/schema_registry/schema_obligations.json",
                "phase_consolidation/schema_registry/schema_generation_context.json",
                "phase_consolidation/schema_registry/global_execution_schema.json",
            ]
            site_content = getattr(site_model, "content", None)
            if isinstance(getattr(site_content, "schema_registry", None), dict) and site_content.schema_registry:
                required_consolidation_artifacts.append(
                    "phase_consolidation/schema_registry/schema_registry.json"
                )
            if l0_execution_pack:
                required_consolidation_artifacts.extend(
                    [
                        "phase_consolidation/l0_execution/l0_execution_pack.json",
                        "phase_consolidation/l0_execution/l0_transition_conflicts.json",
                    ]
                )
            if locals().get("canonical_route_bindings"):
                required_consolidation_artifacts.append(
                    "phase_consolidation/canonical_routes/canonical_route_bindings.json"
                )
            if locals().get("action_slot_registry"):
                required_consolidation_artifacts.extend(
                    [
                        "phase_consolidation/action_slots/action_slot_registry.json",
                        "phase_consolidation/action_slots/unresolved_route_steps.json",
                    ]
                )
            if locals().get("interaction_surface_contract"):
                required_consolidation_artifacts.append(
                    "phase_consolidation/interaction_surfaces/interaction_surface_contract.json"
                )
            consolidation_manifest = _write_consolidation_phase_manifest(
                self.output_dir,
                required_rel_paths=required_consolidation_artifacts,
            )
            if consolidation_manifest.get("status") != "complete":
                raise RuntimeError(
                    "phase_consolidation manifest blocked: "
                    f"{consolidation_manifest.get('blocking_issue_count', 0)} issue(s). "
                    "See phase_consolidation/manifest.json."
                )
            logger.log_info(
                f"AppSpec finalized to {sm_dir} "
                f"(trace_edges={inferred_merge_audit.get('trace_edge_count', 0)}, "
                f"inferred_admitted={inferred_merge_audit.get('inferred_admitted_count', 0)}, "
                f"inferred_overridden_by_trace={inferred_merge_audit.get('inferred_overridden_count', 0)})"
            )
            pom.write_json("consolidation", "debug",
                           "inferred_route_merge_audit.json",
                           inferred_merge_audit,
                           )

            return semantic_design_payload
        finally:
            pass

    async def _design_app_spec_interfaces(
        self,
        *,
        tasks,
        website_type,
        site_model,
        semantic_design: Dict[str, Any],
        logger,
    ) -> Dict[str, Any]:
        trace_context, _sdk_context = _load_trace_context(
            self._trace_resource_dir,
            self.output_dir,
        )
        page_environment_projections = semantic_design.get("page_environment_projections") or {}
        page_environment_evaluation = semantic_design.get("page_environment_evaluation") or {}
        interface_requirement_projection = semantic_design.get("interface_requirement_projection") or {}

        logger.log_step_start("Design Interfaces", "site_model")
        logger.start_stage("Design Interfaces", "site_model")

        trace_prompt_context = _build_interface_design_prompt_context(
            trace_context,
            tasks,
            website_type,
            page_environment_projections=page_environment_projections,
            page_environment_evaluation=page_environment_evaluation,
            interface_requirement_projection=interface_requirement_projection,
        )

        page_count = len([p for p in site_model.structure.page_prototypes if p.confidence >= 0.2])
        max_concurrent = self.config_manager.get("max_concurrent", 8)
        if page_count > 3:
            logger.log_info(
                f"Using parallel AppSpec interface design ({page_count} pages, max_concurrent={max_concurrent})"
            )
            interfaces = await self.interface_designer.design_interfaces_parallel(
                tasks,
                {},
                website_type,
                site_model=site_model,
                max_concurrent=max_concurrent,
                trace_context=trace_context,
                trace_prompt_context=trace_prompt_context,
            )
        else:
            logger.log_info(f"Using single-call AppSpec interface design ({page_count} pages)")
            interfaces = self.interface_designer.design_interfaces(
                tasks,
                {},
                website_type,
                site_model=site_model,
                trace_context=trace_context,
                trace_prompt_context=trace_prompt_context,
            )

        interface_capability_registry = (
            interfaces.pop("interface_capability_registry", None)
            if isinstance(interfaces, dict)
            else None
        )
        if interface_capability_registry:
            self._save_stage_debug("interface_capability_registry", interface_capability_registry, phase="interface_design")
            logger.log_info(
                "Wrote interface_capability_registry.json "
                f"({len(interface_capability_registry.get('semantic_shards', []))} semantic shards)"
            )

        if isinstance(interfaces, dict):
            interfaces.setdefault("semantic_design_source", {
                "format": "site_model_interface_view",
                "source": "site_model.semantic_design",
                "page_environment_specs": "phase_page_environment/semantic_api/06_page_environment_specs/page_environment_specs.json",
                "interface_requirement_projection": "phase_interface_design/interface_requirements/interface_requirement_projection.json",
            })
        self.data_manager.set_interfaces(interfaces)
        self._save_stage_debug("interfaces", interfaces, phase="interface_design")

        logger.end_stage("Design Interfaces")
        logger.log_step_end("Design Interfaces", "site_model")
        return interfaces

    async def _run_frontend_pipeline(self, tasks, interfaces, website_type, logger, site_model, *, semantic_design=None):

        from trace2world_pipeline.utils.page_gen_utils import dedup_resources, build_sprite_sheet
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        pom = PhaseOutputManager(self.output_dir)


        trace_context, sdk_context = _load_trace_context(
            self._trace_resource_dir, self.output_dir
        )

        architecture_pages = {}
        hash_to_asset: Dict[str, Any] = {}
        spec_prep = semantic_design or getattr(site_model, "semantic_design", {}) or {}
        extra_page_data = spec_prep.get("extra_page_data")
        page_execution_contracts = spec_prep.get("page_execution_contracts") or {}
        page_execution_obligations_by_filename = (
            spec_prep.get("page_execution_obligations_by_filename") or {}
        )
        l0_execution_pack = spec_prep.get("l0_execution_pack") or {}
        action_slot_registry = spec_prep.get("action_slot_registry") or {}
        if not action_slot_registry and l0_execution_pack:
            try:
                from trace2world_pipeline.app_spec.action_slot_registry import build_action_slot_registry

                page_environment_specs_for_slots = (
                    spec_prep.get("page_environment_specs")
                    if isinstance(spec_prep.get("page_environment_specs"), dict)
                    else {}
                )
                _qdf2 = _extract_query_domain_filenames(page_environment_specs_for_slots)
                action_slot_registry = build_action_slot_registry(
                    l0_execution_pack,
                    shell_interaction_broadcast=page_environment_specs_for_slots.get(
                        "shell_interaction_broadcast", {}
                    ),
                    query_domain_filenames=_qdf2,
                    canonical_route_bindings=spec_prep.get("canonical_route_bindings"),
                )
                _assert_no_formal_fallback_binding_source(
                    "action_slot_registry",
                    action_slot_registry,
                )
            except RuntimeError as exc:
                if "contains fallback binding source in formal artifact" in str(exc):
                    raise
                logger.log_warning(
                    f"Failed to derive action_slot_registry from l0_execution_pack: {exc}"
                )
            except Exception as exc:
                logger.log_warning(
                    f"Failed to derive action_slot_registry from l0_execution_pack: {exc}"
                )
        interaction_surface_contract = spec_prep.get("interaction_surface_contract") or {}
        if not interaction_surface_contract and l0_execution_pack:
            try:
                from trace2world_pipeline.app_spec.interaction_surface_contract import (
                    build_interaction_surface_contract,
                )

                interaction_surface_contract = build_interaction_surface_contract(
                    l0_execution_pack,
                    page_environment_specs=spec_prep.get("page_environment_specs") or {},
                )
            except Exception as exc:
                logger.log_warning(
                    f"Failed to derive interaction_surface_contract from l0_execution_pack: {exc}"
                )
        page_environment_projections = spec_prep.get("page_environment_projections") or {}
        page_environment_runtime_specs = (
            spec_prep.get("page_environment_runtime_specs") or {}
        )
        page_environment_specs = spec_prep.get("page_environment_specs") or {}
        step_transition_contracts = spec_prep.get("step_transition_contracts") or {}
        page_environment_evaluation = spec_prep.get("page_environment_evaluation") or {}
        interface_requirement_projection = spec_prep.get("interface_requirement_projection") or {}
        site_model_page_count = len(getattr(getattr(site_model, "structure", None), "page_prototypes", []) or [])
        final_page_inputs_preview = site_model.get_final_page_inputs() if site_model else []
        if site_model_page_count and not final_page_inputs_preview:
            raise RuntimeError(
                "PageGen input gate failed: AppSpec has "
                f"{site_model_page_count} page prototype(s), but final_page_inputs is empty. "
                "Check excluded_from_pagegen flags and page prototype screenshot refs before L0 resource extraction."
            )

        pagegen_logic_flow = str(self.config_manager.get("pagegen_logic_flow", "standard") or "standard")
        selection_payload = None
        if bool(self.config_manager.get("reuse_completed_semantic_design", False)):
            if pagegen_logic_flow == "l0_fidelity":
                inventory_path = Path(self.output_dir) / "phase_l0_fidelity/visible_structure_inventory.json"
                if not inventory_path.is_file():
                    raise FileNotFoundError(
                        "Downstream L0 resume requires the preserved visible structure inventory: "
                        f"{inventory_path}"
                    )
                selection_path = Path(self.output_dir) / "phase_l0_fidelity/l0_visual_evidence_selection.json"
                if not selection_path.is_file():
                    raise FileNotFoundError(f"Downstream L0 resume requires its selection manifest: {selection_path}")
                from trace2world_pipeline.compiler.delivery_contract import sha256_file
                inventory_payload = json.loads(inventory_path.read_text(encoding="utf-8"))
                if inventory_payload.get("source_l0_selection_sha256") != sha256_file(selection_path):
                    raise ValueError("Downstream L0 resume inventory is stale against visual selection")
            existing_resources = str(
                self.config_manager.get("filtered_resources_path", "") or ""
            ).strip()
            if not existing_resources or not Path(existing_resources).is_file():
                raise RuntimeError(
                    "Completed semantic-design downstream resume requires the preserved "
                    f"filtered_resources_path, got: {existing_resources!r}"
                )
            self._filtered_resources_path = existing_resources
            logger.log_info(
                "Reused preserved L0 prompt resources for downstream-only rerun; "
                "L0 visual selection and static-resource producers were not invoked"
            )
        else:
            if pagegen_logic_flow == "l0_fidelity":
                selection_payload = self._materialize_l0_visual_evidence_selection(
                    site_model,
                    logger,
                    page_environment_projections=page_environment_projections,
                    interaction_surface_contract=interaction_surface_contract,
                    action_slot_registry=action_slot_registry,
                )
                if not selection_payload:
                    raise RuntimeError("L0 visual selection did not produce an instance-page manifest")
                from trace2world_pipeline.app_spec.visible_structure_inventory import (
                    build_visible_structure_inventory,
                )

                inventory_config = self.config_manager.get_component_config("visible_structure_inventory")
                trace_root = Path(str(self.config_manager.get("trace_input_dir", "") or self._trace_resource_dir))
                inventory_path = asyncio.run(build_visible_structure_inventory(
                    selection_path=Path(self.output_dir) / "phase_l0_fidelity/l0_visual_evidence_selection.json",
                    site_model=site_model,
                    output_dir=Path(self.output_dir),
                    trace_root=trace_root,
                    model=str(inventory_config["model"]),
                    reasoning_effort=str(inventory_config["reasoning_effort"]),
                    max_concurrent=int(inventory_config.get("max_concurrent") or self.config_manager.get("max_concurrent", 8)),
                ))
                logger.log_info(f"L0 visible structure inventory ready: {inventory_path}")
            self._prepare_l0_prompt_static_resources(
                site_model,
                logger,
                page_environment_projections=page_environment_projections,
                interaction_surface_contract=interaction_surface_contract,
                action_slot_registry=action_slot_registry,
                selection_payload=selection_payload,
            )

        def _page_execution_obligation_payload(fn: str) -> Dict[str, Any]:
            page_obligations = page_execution_obligations_by_filename.get(fn, [])
            if not page_obligations:
                return {}
            return {
                "format": "page_execution_obligations",
                "schema_revision": 1,
                "filename": fn,
                "obligations": page_obligations,
                "obligation_count": len(page_obligations),
            }

        self.data_manager.set_interfaces(interfaces)


        page_assignments = interfaces.get("page_assignments", {})
        page_designs = []
        for proto in site_model.structure.page_prototypes:
            structure_data = proto.structure_data if isinstance(proto.structure_data, dict) else {}
            if structure_data.get("excluded_from_pagegen") is True:
                continue
            assigned = page_assignments.get(proto.filename, [])
            page_designs.append({
                'filename': proto.filename,
                'title': proto.page_name,
                'description': proto.description,
                'assigned_interfaces': assigned,
                'components': [],
                'layout': None,
            })
        self.data_manager.set_page_designs(page_designs)
        logger.log_info(f"Built {len(page_designs)} page_designs from AppSpec prototypes")
        logger.log_info(f"Synthesized {len(page_designs)} page designs from architecture")


        entity_schema = {
            "entities": site_model.content.entity_schemas,
            "relationships": site_model.content.entity_relationships,
        }
        semantic_design_payload = spec_prep if isinstance(spec_prep, dict) else {}
        if not semantic_design_payload:
            semantic_design_payload = getattr(site_model, "semantic_design", {}) or {}
        if not isinstance(semantic_design_payload, dict):
            semantic_design_payload = {}
        state_kernel_enabled = str(self.config_manager.get("runtime_profile", "") or "").strip() == "state_kernel_sqlite"
        compiled_execution_views = (
            semantic_design_payload.get("spec_compiled_execution_views", {})
            if isinstance(semantic_design_payload.get("spec_compiled_execution_views", {}), dict)
            else {}
        )
        environment_state_artifacts = (
            compiled_execution_views.get("environment_state_artifacts", {})
            if isinstance(compiled_execution_views.get("environment_state_artifacts", {}), dict)
            else {}
        )
        if state_kernel_enabled and not environment_state_artifacts:
            compiled_dir = Path(self.output_dir) / "phase_page_environment" / "semantic_api" / "08_spec_compiled_execution_views"
            artifact_names = (
                "storage_plan", "runtime_state_registry", "transaction_manifest",
                "seed_manifest", "evaluator_state_view", "state_api_contract",
            )
            for artifact_name in artifact_names:
                artifact_path = compiled_dir / f"{artifact_name}.json"
                if not artifact_path.is_file():
                    raise RuntimeError(f"Missing SQLite Environment State artifact: {artifact_path}")
                environment_state_artifacts[artifact_name] = json.loads(artifact_path.read_text(encoding="utf-8"))
        spec_execution_views_by_page = _spec_execution_views_by_page_from_semantic_design(semantic_design_payload)
        domain_contracts = _domain_contracts_from_semantic_design_or_artifact(
            semantic_design_payload,
            output_dir=self.output_dir,
        )
        if domain_contracts:
            logger.log_info(
                "Loaded domain_contracts for frontend runtime validation: "
                f"format={domain_contracts.get('format', '')}, "
                f"issue_count={domain_contracts.get('summary', {}).get('issue_count', 0)}"
            )
        else:
            logger.log_warning(
                "domain_contracts unavailable for frontend runtime validation; "
                "runtime DOM audit will run without domain-level contracts"
            )
        spec_authoritative_runtime = _looks_like_page_environment_specs(
            getattr(site_model, "page_environment_specs", None)
            or semantic_design_payload.get("page_environment_specs")
        )
        runtime_state_signature = None if spec_authoritative_runtime else getattr(site_model, "state_signature", None)
        runtime_contract = build_runtime_contract(
            entity_schema,
            interfaces,
            task_interaction_contracts=None if spec_authoritative_runtime else getattr(site_model, "task_interaction_contracts", None),
            render_context=build_render_context(site_model.environment_profile),
        )
        if spec_authoritative_runtime:
            runtime_contract.setdefault("authority", {})["state_signature_source"] = "page_environment_spec"
        elif runtime_state_signature:
            runtime_contract.setdefault("authority", {})["state_signature_source"] = "legacy_state_signature"
        runtime_contract_path = pom.formal("sdk_generation", "runtime", "runtime_contract.json")
        runtime_contract_path.parent.mkdir(parents=True, exist_ok=True)
        with runtime_contract_path.open("w", encoding="utf-8") as f:
            json.dump(runtime_contract, f, ensure_ascii=False, indent=2)
        logger.log_info(
            "Materialized runtime_contract.json "
            f"({len(runtime_contract.get('entities', {}))} entities)"
        )


        all_interfaces = interfaces.get('interfaces', [])
        page_interfaces = {}
        for pd in page_designs:
            fn = pd.get('filename', '')
            names = pd.get('assigned_interfaces', [])
            page_interfaces[fn] = [i for i in all_interfaces if i.get('name') in names]

        page_execution_contracts_by_filename = (
            page_execution_contracts.get("pages_by_filename", {})
            if isinstance(page_execution_contracts, dict)
            else {}
        )

        if page_execution_contracts:
            try:
                from trace2world_pipeline.app_spec.page_execution_contracts import (
                    compact_page_execution_contract,
                    page_execution_contract_for_filename,
                )

                if extra_page_data is None:
                    extra_page_data = {}
                for pd in page_designs:
                    fn = pd.get("filename", "") if isinstance(pd, dict) else getattr(pd, "filename", "")
                    if not fn or fn in page_environment_projections:
                        continue
                    page_data = dict(extra_page_data.get(fn, {}))
                    contract = page_execution_contract_for_filename(page_execution_contracts, fn)
                    if contract:
                        page_data["page_execution_contract"] = compact_page_execution_contract(contract)
                    obligation_payload = _page_execution_obligation_payload(fn)
                    if obligation_payload:
                        page_data["page_execution_obligations"] = obligation_payload
                    if page_data:
                        extra_page_data[fn] = page_data
            except Exception as exc:
                raise RuntimeError(f"Failed to attach PageGen execution contracts: {exc}") from exc


        resource_result = await self._build_resource_extra_page_data(
            page_designs,
            site_model,
            logger,
            page_environment_projections=page_environment_projections,
            spec_execution_views_by_page=spec_execution_views_by_page,
            interaction_surface_contract=interaction_surface_contract,
            action_slot_registry=action_slot_registry,
            page_execution_contracts_by_filename=page_execution_contracts_by_filename,
        )
        if resource_result is None:
            resource_extra_page_data, hash_to_asset = {}, {}
        elif isinstance(resource_result, tuple):
            resource_extra_page_data, hash_to_asset = resource_result
        else:
            resource_extra_page_data, hash_to_asset = resource_result, {}
        if resource_extra_page_data:
            merged_extra_page_data: Dict[str, Dict[str, Any]] = dict(extra_page_data or {})
            for filename, resource_page_data in resource_extra_page_data.items():
                if not filename or not isinstance(resource_page_data, dict):
                    continue
                page_data = dict(merged_extra_page_data.get(filename, {}))
                page_data.update(resource_page_data)
                merged_extra_page_data[filename] = page_data
            extra_page_data = merged_extra_page_data
        elif extra_page_data is None:
            extra_page_data = {}
        if page_environment_projections:
            extra_page_data = _attach_page_environment_projections_to_extra_page_data(
                output_dir=self.output_dir,
                extra_page_data=extra_page_data,
                page_environment_projections=page_environment_projections,
                logger=logger,
            )


        task_elements = None
        if site_model and hasattr(site_model, 'task_elements') and site_model.task_elements:
            raw_te = (
                site_model.task_elements.get("effective_elements_by_page", {})
                or site_model.task_elements.get("elements_by_page", {})
            )
            if raw_te:

                id_to_fn = {}
                for proto in site_model.structure.page_prototypes:
                    if proto.page_id and proto.filename:
                        id_to_fn[proto.page_id] = proto.filename
                        sd = proto.structure_data if isinstance(proto.structure_data, dict) else {}
                        cid = sd.get("cluster_id", "")
                        if cid:
                            id_to_fn[cid] = proto.filename

                task_elements = {}
                for page_id, elements in raw_te.items():
                    fn = id_to_fn.get(page_id, "")
                    if fn:
                        task_elements[fn] = elements
                    else:
                        task_elements[page_id] = elements
                logger.log_info(f"  task_elements mapped: {len(task_elements)} pages with elements")

        pagegen_logic_flow = str(self.config_manager.get("pagegen_logic_flow", "standard") or "standard")
        if pagegen_logic_flow == "cascade":
            pagegen_logic_flow = "full"
        cascade_page_inputs: Dict[str, Dict[str, Any]] = {}
        from trace2world_pipeline.page_realizer.pagegen_input_scope import build_pagegen_input_scopes
        from trace2world_pipeline.app_spec.evidence_resolver import EvidenceResolver

        final_page_inputs = site_model.get_final_page_inputs() if site_model else []
        fidelity_mode = {"l0_fidelity": "l0", "full": "full"}.get(pagegen_logic_flow, "standard")
        evidence_resolver = EvidenceResolver.from_result_dir(
            Path(self.output_dir),
            trace_input_dir=str(self.config_manager.get("trace_input_dir", "") or ""),
            trace_resource_dir=str(getattr(self, "_trace_resource_dir", "") or ""),
        )

        visual_surface_sigs = None
        state_delta_sigs = None
        if fidelity_mode == "l0":
            try:
                from trace2world_pipeline.app_spec.l0_visual_surface_signatures import compile_visual_surface_signatures
                from trace2world_pipeline.app_spec.trace_state_delta_signatures import compile_state_delta_signatures
                from trace2world_pipeline.app_spec.trace_action_state_alignment import compile_trace_action_state_alignment

                _frame_obs_dir = Path(self.output_dir) / "phase_frame_analysis" / "frame_observations"
                _runtime_obs_path = Path(self.output_dir) / "phase_instance_splitting" / "runtime_state_observations.json"
                _runtime_obs_list = []
                if _runtime_obs_path.is_file():
                    try:
                        _runtime_obs_list = json.load(open(_runtime_obs_path, encoding="utf-8")).get("observations", [])
                    except Exception:
                        pass
                _trace_ids = sorted({str(o.get("trace_id", "")) for o in _runtime_obs_list if o.get("trace_id")})

                if _frame_obs_dir.is_dir() and _trace_ids:
                    _alignment = compile_trace_action_state_alignment(
                        _frame_obs_dir, _trace_ids, runtime_state_observations=_runtime_obs_list,
                    )
                    _state_deltas = compile_state_delta_signatures(_runtime_obs_list, _alignment)
                    state_delta_sigs = _state_deltas.get("state_delta_signatures_by_step")

                    _formal_dir = Path(self.output_dir) / "phase_l0_fidelity" / "formal"
                    _formal_dir.mkdir(parents=True, exist_ok=True)
                    with open(_formal_dir / "trace_action_state_alignment.json", "w", encoding="utf-8") as _f:
                        json.dump(_alignment, _f, ensure_ascii=False, indent=2)
                    with open(_formal_dir / "trace_state_delta_signatures.json", "w", encoding="utf-8") as _f:
                        json.dump(_state_deltas, _f, ensure_ascii=False, indent=2)
                    logger.log_info(
                        f"Formal producers: alignment={_alignment['summary'].get('total_steps', 0)} steps, "
                        f"state_deltas={_state_deltas['summary'].get('requires_spec_contract', 0)} requires_spec"
                    )

                if page_environment_specs:
                    _visual = compile_visual_surface_signatures(page_environment_specs)
                    visual_surface_sigs = _visual.get("visual_surface_signatures_by_step")
                    _formal_dir = Path(self.output_dir) / "phase_l0_fidelity" / "formal"
                    _formal_dir.mkdir(parents=True, exist_ok=True)
                    with open(_formal_dir / "l0_visual_surface_signatures.json", "w", encoding="utf-8") as _f:
                        json.dump(_visual, _f, ensure_ascii=False, indent=2)
                    logger.log_info(
                        f"Visual surface signatures: {_visual['summary'].get('total_signatures', 0)} across "
                        f"{_visual['summary'].get('page_count', 0)} pages"
                    )
            except Exception as exc:
                logger.log_warning(f"Formal L0 producers failed (non-blocking): {exc}")

        e2e_resource_root = None
        frp = str(getattr(self, "_filtered_resources_path", "") or "")
        if frp and os.path.isfile(frp):
            e2e_resource_root = str(Path(frp).parent)

        pagegen_input_scopes = build_pagegen_input_scopes(
            final_page_inputs,
            [],
            fidelity_mode=fidelity_mode,
            page_environment_projections=page_environment_projections,
            spec_execution_views_by_page=spec_execution_views_by_page,
            interaction_surface_contract=interaction_surface_contract,
            action_slot_registry=action_slot_registry,
            step_transition_contracts=step_transition_contracts,
            evidence_resolver=evidence_resolver,
            visual_surface_signatures=visual_surface_sigs,
            state_delta_signatures=state_delta_sigs,
            e2e_resource_root=e2e_resource_root,
            render_context=build_render_context(self.environment_profile),
        )
        for filename, page_input in pagegen_input_scopes.items():
            if not filename:
                continue
            scoped_extra = (extra_page_data or {}).get(filename, {}).get("pagegen_input_scope")
            cascade_page_inputs[filename] = _merge_canonical_pagegen_scope(
                dict(page_input),
                scoped_extra if isinstance(scoped_extra, dict) else None,
            )
        extra_page_data = _attach_pagegen_input_scopes_to_extra_page_data(
            extra_page_data,
            cascade_page_inputs,
        )
        logger.log_info(
            f"{pagegen_logic_flow} pagegen prepared cascade-capable inputs for {len(cascade_page_inputs)} pages"
        )
        pagegen_runtime_config = {
            "round1_image_limit": int(self.config_manager.get("pagegen_cascade_round1_image_limit", 8) or 8),
            "max_images_per_round": int(self.config_manager.get("pagegen_cascade_max_images_per_round", 8) or 8),
            "patching_enabled": bool(self.config_manager.get("pagegen_cascade_patching_enabled", True)),
            "use_unique_steps_enabled": bool(self.config_manager.get("pagegen_cascade_use_unique_steps_enabled", True)),
            "patch_max_tokens": int(self.config_manager.get("pagegen_cascade_patch_max_tokens", 8000) or 8000),
            "prompt_mode": str(self.config_manager.get("pagegen_cascade_prompt_mode", "current_pipeline") or "current_pipeline"),
            "adaptive_cascade_enabled": bool(self.config_manager.get("pagegen_adaptive_cascade_enabled", True)),
            "adaptive_cascade_min_prompt_screenshots": int(self.config_manager.get("pagegen_adaptive_cascade_min_prompt_screenshots", 12) or 12),
            "adaptive_cascade_min_resources": int(self.config_manager.get("pagegen_adaptive_cascade_min_resources", 48) or 48),
            "resource_semantic_labeling_enabled": bool(self.config_manager.get("pagegen_resource_semantic_labeling_enabled", False)),
            "scoped_content_enabled": bool(self.config_manager.get("pagegen_scoped_content_enabled", True)),
            "pagegen_max_estimated_input_tokens": normalize_pagegen_input_token_budget(
                self.config_manager.get("pagegen_max_estimated_input_tokens", 40000)
            ),
            "pagegen_input_image_token_proxy": int(
                self.config_manager.get("pagegen_input_image_token_proxy", 2200)
                or 2200
            ),
            "pagegen_input_overhead_token_proxy": int(
                self.config_manager.get("pagegen_input_overhead_token_proxy", 500)
                or 500
            ),
        }
        self._ensure_final_output_dirs()
        pagegen_profile_marker_path = (
            Path(self.output_dir) / "run_management" / "pagegen_environment_profile.json"
        )
        validate_pagegen_environment_profile_resume(
            self.output_dir,
            self.environment_profile,
        )

        from trace2world_pipeline.page_realizer.html_generator import GeneratedPage
        existing_pages = []
        pages_to_generate = []
        for pd in page_designs:
            fn = pd.get('filename', '')
            html_path = self._frontend_page_path(fn)
            if os.path.exists(html_path) and os.path.getsize(html_path) > 100:

                with open(html_path, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                if page_execution_contracts:
                    from trace2world_pipeline.app_spec.page_execution_contracts import detect_page_execution_html_artifacts

                    html_artifacts = detect_page_execution_html_artifacts(
                        html_content,
                        page_execution_contracts_by_filename.get(fn, {}),
                    )
                    if html_artifacts:
                        try:
                            _move_to_deprecated(html_path, reason="page_execution_contract_artifact")
                        except OSError as exc:
                            logger.log_warning(f"Failed to deprecate stale HTML {fn}: {exc}")
                        logger.log_warning(
                            f"Invalidating stale generated HTML {fn}; found "
                            f"{len(html_artifacts)} page execution contract artifacts"
                        )
                        pages_to_generate.append(pd)
                        continue
                    logger.log_info(
                        f"Reusing {fn}; L0 page execution contract will be validated after pagegen"
                    )
                css_content = ''
                css_path = self._frontend_css_path(fn)
                if os.path.exists(css_path):
                    with open(css_path, 'r', encoding='utf-8') as f:
                        css_content = f.read()

                import re as _re
                reused_entity_keys = _re.findall(r"localStorage\.setItem\(['\"](\w+)['\"]", html_content)
                reused_entity_keys = [k for k in reused_entity_keys if not k.startswith('_')]
                existing_pages.append(GeneratedPage(
                    filename=fn, html_content=html_content,
                    css_content=css_content, entity_keys=reused_entity_keys
                ))
                logger.log_info(f"Reusing existing page: {fn}")
            else:
                pages_to_generate.append(pd)

        if existing_pages:
            logger.log_info(f"Reusing {len(existing_pages)} existing pages, generating {len(pages_to_generate)} new pages")
        if existing_pages and pages_to_generate:
            pagegen_resume_source = "partial"
        elif existing_pages:
            pagegen_resume_source = "reused_disk"
        else:
            pagegen_resume_source = "recomputed"
        self._record_resume_plan_decision(
            logical_artifact="pagegen_pages",
            source=pagegen_resume_source,
            phase="PIPE-P5 Frontend Generation",
            artifact_path=self._frontend_pages_dir(),
            canonical_semantic_source=False,
            canonical_prompt_boundary=True,
            consumers=("browser_runtime", "validator", "replay_executor"),
            reason=(
                "all_pages_reused"
                if pagegen_resume_source == "reused_disk"
                else "some_pages_reused"
                if pagegen_resume_source == "partial"
                else "no_reusable_pages"
            ),
            details={
                "existing_page_count": len(existing_pages),
                "pages_to_generate_count": len(pages_to_generate),
                "total_page_design_count": len(page_designs),
                "manual_invalidation_policy": "delete_or_deprecate_frontend_page_to_force_regenerate",
            },
        )


        bl_path = self._runtime_business_logic_path()
        resumed_bl = None
        if os.path.exists(bl_path) and os.path.getsize(bl_path) > 100:
            with open(bl_path, 'r', encoding='utf-8') as f:
                resumed_bl = f.read()
            syntax_status = self._business_logic_syntax_status(bl_path)
            if syntax_status.get("ok"):
                logger.log_info(f"Reusing existing business_logic.js ({len(resumed_bl)} chars)")
            else:
                repaired_bl = self.parallel_generator._normalize_generated_javascript_syntax(resumed_bl)
                if repaired_bl != resumed_bl:
                    with open(bl_path, 'w', encoding='utf-8') as f:
                        f.write(repaired_bl)
                    repaired_status = self._business_logic_syntax_status(bl_path)
                    if repaired_status.get("ok"):
                        resumed_bl = repaired_bl
                        logger.log_warning(
                            "Repaired existing business_logic.js syntax and will reuse it ({len_chars} chars)".format(
                                len_chars=len(resumed_bl)
                            )
                        )
                    else:
                        resumed_bl = None
                else:
                    resumed_bl = None
                if not resumed_bl:
                    logger.log_warning(
                        "Existing business_logic.js failed syntax check; regenerating. {}".format(
                            (syntax_status.get("stderr") or syntax_status.get("error") or "").splitlines()[0:2]
                        )
                    )
        self._record_resume_plan_decision(
            logical_artifact="runtime_backend",
            source="reused_disk" if resumed_bl else "recomputed",
            phase="PIPE-P5 Backend Runtime Generation",
            artifact_path=bl_path,
            canonical_semantic_source=False,
            canonical_prompt_boundary=True,
            consumers=("browser_runtime", "validator", "replay_executor"),
            reason="existing_business_logic_reused" if resumed_bl else "missing_or_invalid_business_logic",
            details={
                "business_logic_exists": os.path.exists(bl_path),
                "business_logic_reused": bool(resumed_bl),
                "manual_invalidation_policy": "delete_or_deprecate_business_logic_to_force_regenerate",
            },
        )


        content_samples = {}
        if site_model and site_model.content.content_samples:
            content_samples = dict(site_model.content.content_samples)
        sdk_init_data = content_samples
        entity_seed = {}
        if site_model:
            seed_artifact = _ensure_site_model_entity_seed(site_model, self.output_dir)
            entity_seed = seed_artifact.get("seed", {})
            if seed_artifact.get("materialized"):
                logger.log_info(
                    "Materialized entity_seed.json: {} records across {} entities".format(
                        seed_artifact.get("validation", {}).get("summary", {}).get("record_count", 0),
                        seed_artifact.get("validation", {}).get("summary", {}).get("entity_count", 0),
                    )
                )
            seed_init_data = entity_seed_to_init_data(entity_seed)
            if seed_init_data:
                sdk_init_data = seed_init_data
        if state_kernel_enabled:
            from trace2world_pipeline.state_kernel.canonical_json import canonical_hash
            from trace2world_pipeline.state_kernel.materializer import materialize_environment_state_runtime

            entity_types = environment_state_artifacts["storage_plan"].get("entity_types", [])
            world_collections: Dict[str, List[Any]] = {}
            for entity_type in entity_types:
                entity_name = str(entity_type.get("entity_type") or "")
                storage_key = str(entity_type.get("storage_key") or entity_name)
                candidate = sdk_init_data.get(entity_name, sdk_init_data.get(storage_key, []))
                if isinstance(candidate, list):
                    world_collections[storage_key] = candidate
            expected_world_hash = environment_state_artifacts["storage_plan"]["world_hash"]
            actual_world_hash = canonical_hash(world_collections)
            if actual_world_hash != expected_world_hash:
                raise RuntimeError(
                    "SQLite world seed hash mismatch between compiled view and SDK materialization: "
                    f"expected={expected_world_hash}, actual={actual_world_hash}"
                )
            state_materialization = materialize_environment_state_runtime(
                output_dir=self.output_dir,
                artifacts=environment_state_artifacts,
                world=world_collections,
            )
            logger.log_info(
                "Materialized SQLite Environment State runtime: "
                f"{state_materialization.get('runtime_dir')}"
            )
        test_flows_path = self._test_flows_path()
        generate_tests = _instrumentation_generate_test_logic(self.config_manager)


        sdk_trace_prompt = _build_sdk_generation_prompt_context(sdk_context, trace_context)

        def _apply_page_execution_runtimes(fn: str, html_content: str) -> str:
            from trace2world_pipeline.app_spec.page_execution_contracts import (
                inject_page_execution_runtimes,
            )

            page_payload = dict(page_execution_contracts_by_filename.get(fn, {}))
            page_payload["page_execution_obligation_action_surface_runtime_enabled"] = bool(
                self.config_manager.get(
                    "page_execution_obligation_action_surface_runtime_enabled",
                    True,
                )
            )
            page_environment_projection = (extra_page_data or {}).get(fn, {}).get("page_environment_projection")
            if isinstance(page_environment_projection, dict) and page_environment_projection:
                page_payload["page_environment_projection"] = page_environment_projection
            page_environment_spec = page_environment_runtime_specs.get(fn, {})
            if isinstance(page_environment_spec, dict) and page_environment_spec:
                page_payload["page_environment_spec"] = page_environment_spec
            obligation_payload = _page_execution_obligation_payload(fn)
            if isinstance(obligation_payload, dict) and obligation_payload.get("obligations"):
                page_payload["page_execution_obligations"] = obligation_payload
            updated, _changed = inject_page_execution_runtimes(html_content, page_payload)
            return updated

        def _apply_incremental_pagegen_contracts(fn: str, html_content: str) -> str:
            return _apply_page_execution_runtimes(fn, html_content)

        async def _generate_sdk():
            runtime_dir = self._backend_runtime_dir()
            os.makedirs(runtime_dir, exist_ok=True)

            if resumed_bl:
                if generate_tests and not os.path.exists(test_flows_path):
                    logger.log_info("business_logic.js resumed but test_flows.js missing — generating tests")
                    logger.start_stage("Generate Tests", "backend")
                    tests = await self.parallel_generator._generate_tests(
                        tasks, entity_schema, interfaces, website_type, sdk_init_data,
                        trace_context=sdk_trace_prompt
                    )
                    os.makedirs(os.path.dirname(test_flows_path), exist_ok=True)
                    with open(test_flows_path, 'w', encoding='utf-8') as f:
                        f.write(tests)
                    logger.end_stage("Generate Tests")
                    logger.log_info(f"Generated test_flows.js ({len(tests)} chars)")
                return resumed_bl
            logger.log_step_start("Generate SDK", "backend")
            logger.start_stage("Generate SDK", "backend")
            bundle_task = self.parallel_generator._generate_implementation_bundle(
                entity_schema, interfaces, website_type, init_data=sdk_init_data,
                trace_context=sdk_trace_prompt,
                task_interaction_contracts=None if spec_authoritative_runtime else getattr(site_model, "task_interaction_contracts", None),
                state_kernel_enabled=state_kernel_enabled,
            )
            if generate_tests:
                test_task = self.parallel_generator._generate_tests(
                    tasks, entity_schema, interfaces, website_type, sdk_init_data,
                    trace_context=sdk_trace_prompt
                )
                bundle, tests = await asyncio.gather(bundle_task, test_task)
                os.makedirs(os.path.dirname(test_flows_path), exist_ok=True)
                with open(test_flows_path, 'w', encoding='utf-8') as f:
                    f.write(tests)
                logger.log_info(f"Generated test_flows.js ({len(tests)} chars)")
            else:
                bundle = await bundle_task


            data_layer_path = os.path.join(runtime_dir, "data_layer.js")
            seed_data_path = os.path.join(runtime_dir, "seed_data.js")
            with open(data_layer_path, 'w', encoding='utf-8') as f:
                f.write(bundle["data_layer"])
            with open(seed_data_path, 'w', encoding='utf-8') as f:
                f.write(bundle["seed_data"])
            logger.log_info(
                "SDK bundle: data_layer.js ({dl} chars) + seed_data.js ({sd} chars)".format(
                    dl=len(bundle["data_layer"]),
                    sd=len(bundle["seed_data"]),
                )
            )


            implementation = bundle["business_logic"]
            with open(bl_path, 'w', encoding='utf-8') as f:
                f.write(implementation)
            syntax_status = self._business_logic_syntax_status(bl_path)
            if not syntax_status.get("ok"):
                repaired = self.parallel_generator._normalize_generated_javascript_syntax(implementation)
                if repaired != implementation:
                    with open(bl_path, 'w', encoding='utf-8') as f:
                        f.write(repaired)
                    implementation = repaired
                    syntax_status = self._business_logic_syntax_status(bl_path)
                    if syntax_status.get("ok"):
                        logger.log_warning("Repaired generated business_logic.js syntax after assembly")
                if not syntax_status.get("ok"):
                    raise RuntimeError(
                        "Generated business_logic.js failed syntax check: {}".format(
                            syntax_status.get("stderr") or syntax_status.get("error") or "unknown"
                        )
                    )
            logger.end_stage("Generate SDK")
            logger.log_step_end("Generate SDK", "backend")
            logger.log_info(f"Generated business_logic.js ({len(implementation)} chars)")
            return implementation

        async def _generate_pages():
            if not pages_to_generate:
                logger.log_info("All pages already generated, skipping page generation")
                return existing_pages
            logger.log_step_start("Generate Pages", "frontend")
            logger.start_stage("Generate Pages", "frontend")
            new_pages = await self.page_generator.generate_pages_async(
                pages_to_generate, website_type, entity_schema,
                page_interfaces, {},
                None,
                architecture_pages=architecture_pages,
                site_model=site_model,
                extra_page_data=extra_page_data,
                output_dir=self.output_dir,
                task_elements=task_elements,
                content_samples=content_samples if content_samples else None,
                pagegen_logic_flow=pagegen_logic_flow,
                cascade_page_inputs=cascade_page_inputs,
                pagegen_runtime_config=pagegen_runtime_config,
                page_postprocess=_apply_incremental_pagegen_contracts,
            )
            logger.end_stage("Generate Pages")
            logger.log_step_end("Generate Pages", "frontend")
            return existing_pages + new_pages


        logger.log_info("Starting scheduled frontend producers: backend SDK gen, then frontend page gen")
        sdk_result = await _generate_sdk()
        generated_pages = await _generate_pages()
        logger.log_info(f"Scheduled frontend producers complete: SDK={len(sdk_result)} chars, pages={len(generated_pages)}")
        refreshed_runtime_pages = 0
        for page in generated_pages:
            fn = getattr(page, "filename", "")
            html_content = getattr(page, "html_content", "")
            if not fn or not html_content:
                continue
            updated_html = _apply_page_execution_runtimes(fn, html_content)
            if updated_html == html_content:
                continue
            page.html_content = updated_html
            html_path = self._frontend_page_path(fn)
            try:
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(updated_html)
                refreshed_runtime_pages += 1
            except OSError as exc:
                logger.log_warning(f"Failed to refresh page execution runtime for {fn}: {exc}")
                raise
        if refreshed_runtime_pages:
            logger.log_info(
                f"Refreshed page execution runtimes for {refreshed_runtime_pages} generated/reused pages"
            )
        _merge_cascade_round_resource_semantics(
            output_dir=self.output_dir,
            generated_pages=generated_pages,
            extra_page_data=extra_page_data,
            semantic_enabled=bool(self.config_manager.get("pagegen_resource_semantic_labeling_enabled", False)),
            logger=logger,
        )
        if page_execution_contracts:
            try:
                from trace2world_pipeline.app_spec.page_execution_contracts import validate_page_execution_contracts
                html_by_filename = {
                    page.filename: page.html_content
                    for page in generated_pages
                    if page and getattr(page, "filename", "") and getattr(page, "html_content", "")
                }
                page_execution_validation = validate_page_execution_contracts(
                    page_execution_contracts,
                    html_by_filename,
                    page_environment_specs=page_environment_specs,
                )
                _pagegen_debug = str(pom.debug("pagegen"))
                os.makedirs(_pagegen_debug, exist_ok=True)
                with open(os.path.join(_pagegen_debug, "page_execution_contract_validation.json"), "w", encoding="utf-8") as f:
                    json.dump(page_execution_validation, f, ensure_ascii=False, indent=2)
                missing_pages = [
                    filename for filename, report in page_execution_validation.get("pages", {}).items()
                    if isinstance(report, dict) and not report.get("ok", True)
                ]
                if missing_pages:
                    logger.log_warning(
                        "Page execution contract validation found missing L0 text in "
                        f"{len(missing_pages)} pages; see phase_page_environment/debug/page_execution_contract_validation.json"
                    )
            except Exception as exc:
                logger.log_warning(f"Failed to validate page execution contracts: {exc}")


        if isinstance(page_environment_specs, dict):
            from trace2world_pipeline.page_realizer.pagegen_dom_contract_validator import (
                validate_action_slot_route_contracts,
                validate_pagegen_dom_contracts,
                validate_pagegen_runtime_dom_contracts_async,
                write_audit,
            )
            from trace2world_pipeline.app_spec.interaction_surface_contract import (
                required_component_ids_by_filename,
            )

            specs_by_filename = page_environment_specs.get(
                "page_environment_specs_by_filename", {}
            )
            if specs_by_filename:
                pagegen_contract_errors: list[str] = []
                pages_dir = PhaseOutputManager(self.output_dir).formal("pagegen", "pages")
                required_surface_ids = required_component_ids_by_filename(
                    interaction_surface_contract
                )
                audit = validate_pagegen_dom_contracts(
                    page_environment_specs_by_filename=specs_by_filename,
                    generated_pages_dir=pages_dir,
                    required_component_ids_by_filename=required_surface_ids or None,
                )
                audit_path = PhaseOutputManager(self.output_dir).formal(
                    "pagegen", "validation", "dom_contract", "pagegen_dom_contract_audit.json"
                )
                write_audit(audit_path, audit)
                register_artifact(
                    Path(self.output_dir),
                    audit_path,
                    artifact_id="audits.pagegen_dom_contract",
                    phase="pagegen",
                    artifact_kind="validation_report",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="pagegen_dom_contract_validator",
                    producer="frontend.pagegen_dom_contract_validator.validate_pagegen_dom_contracts",
                    allowed_consumers=("developer", "replay_executor"),
                    semantic_role="dom_contract_gate",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    artifact_role="validation_gate",
                )
                s = audit.get("summary", {})
                logger.log_info(
                    "pagegen_dom_contract_audit: pages=%d passing=%d failing=%d "
                    "missing=%d ambiguous=%d required=%d present=%d"
                    % (
                        s.get("page_count", 0),
                        s.get("passing_pages", 0),
                        s.get("failing_pages", 0),
                        s.get("missing_total", 0),
                        s.get("ambiguous_total", 0),
                        s.get("required_total", 0),
                        s.get("present_total", 0),
                    )
                )
                dom_hard_gate_message = _pagegen_dom_contract_hard_gate_message(
                    audit,
                    runtime=False,
                )
                if dom_hard_gate_message:
                    logger.log_warning(dom_hard_gate_message)
                    pagegen_contract_errors.append(dom_hard_gate_message)

                runtime_audit = await validate_pagegen_runtime_dom_contracts_async(
                    page_environment_specs_by_filename=specs_by_filename,
                    generated_pages_dir=pages_dir,
                    result_dir=Path(self.output_dir),
                    required_component_ids_by_filename=required_surface_ids or None,
                    interaction_surface_contract=interaction_surface_contract,
                    domain_contracts=domain_contracts,
                    viewport=dict(self.environment_profile["viewport"]),
                )
                runtime_audit_path = PhaseOutputManager(self.output_dir).formal(
                    "pagegen", "validation", "dom_contract", "pagegen_runtime_dom_contract_audit.json"
                )
                write_audit(runtime_audit_path, runtime_audit)
                register_artifact(
                    Path(self.output_dir),
                    runtime_audit_path,
                    artifact_id="audits.pagegen_runtime_dom_contract",
                    phase="pagegen",
                    artifact_kind="validation_report",
                    api_or_det="det",
                    canonical_source=False,
                    logical_owner="pagegen_dom_contract_validator",
                    producer="frontend.pagegen_dom_contract_validator.validate_pagegen_runtime_dom_contracts",
                    allowed_consumers=("developer", "replay_executor"),
                    semantic_role="runtime_dom_contract_gate",
                    canonical_semantic_source=False,
                    canonical_prompt_boundary=False,
                    artifact_role="validation_gate",
                )
                runtime_s = runtime_audit.get("summary", {})
                logger.log_info(
                    "pagegen_runtime_dom_contract_audit: pages=%d passing=%d failing=%d "
                    "missing=%d noninteractive=%d initial_visibility=%d not_topmost=%d warnings=%d"
                    % (
                        runtime_s.get("page_count", 0),
                        runtime_s.get("passing_pages", 0),
                        runtime_s.get("failing_pages", 0),
                        runtime_s.get("missing_total", 0),
                        runtime_s.get("noninteractive_total", 0),
                        runtime_s.get("initial_visibility_violation_total", 0),
                        runtime_s.get("not_topmost_total", 0),
                        runtime_s.get("runtime_visibility_warning_total", 0),
                    )
                )
                runtime_dom_hard_gate_message = _pagegen_dom_contract_hard_gate_message(
                    runtime_audit,
                    runtime=True,
                )
                if runtime_dom_hard_gate_message:
                    logger.log_warning(runtime_dom_hard_gate_message)
                    pagegen_contract_errors.append(runtime_dom_hard_gate_message)

                if action_slot_registry:
                    action_slot_route_audit = validate_action_slot_route_contracts(
                        action_slot_registry=action_slot_registry,
                        generated_pages_dir=pages_dir,
                    )
                    from trace2world_pipeline.core.phase_output import PhaseOutputManager as _POM2
                    action_slot_audit_path = _POM2(self.output_dir).debug(
                        "consolidation", "action_slot_route_contract_audit.json"
                    )
                    write_audit(action_slot_audit_path, action_slot_route_audit)
                    register_artifact(
                        Path(self.output_dir),
                        action_slot_audit_path,
                        artifact_id="audits.action_slot_route_contract",
                        phase="frontend_page_generation",
                        artifact_kind="validation_report",
                        api_or_det="det",
                        canonical_source=False,
                        logical_owner="pagegen_dom_contract_validator",
                        producer="frontend.pagegen_dom_contract_validator.validate_action_slot_route_contracts",
                        allowed_consumers=("developer", "replay_executor"),
                        semantic_role="action_slot_route_contract_gate",
                        canonical_semantic_source=False,
                        canonical_prompt_boundary=False,
                        artifact_role="validation_gate",
                    )
                    route_s = action_slot_route_audit.get("summary", {})
                    logger.log_info(
                        "action_slot_route_contract_audit: pages=%d slots=%d failing=%d "
                        "missing_component=%d missing_executor=%d sdk_only=%d"
                        % (
                            route_s.get("page_count", 0),
                            route_s.get("slot_count", 0),
                            route_s.get("failing_pages", 0),
                            route_s.get("missing_component_total", 0),
                            route_s.get("missing_executor_total", 0),
                            route_s.get("sdk_only_route_total", 0),
                        )
                    )
                    if (
                        route_s.get("missing_component_total")
                        or route_s.get("missing_executor_total")
                        or route_s.get("sdk_only_route_total")
                    ):
                        pagegen_contract_errors.append(
                            "action slot route contract gate failed; "
                            "see phase_consolidation/site_model/action_slot_route_contract_audit.json. "
                            "Every route_action_slot must bind its component host to "
                            "GhostwebRouteExecution.navigate(target_filename); "
                            "WebsiteSDK.navigateRoute is side-effect only."
                        )

                if pagegen_contract_errors:
                    raise RuntimeError("\n".join(pagegen_contract_errors))


                try:
                    from trace2world_pipeline.app_spec.step_component_binding import (
                        finalize_selectors_in_pack,
                    )
                    from trace2world_pipeline.app_spec.page_execution_obligations import (
                        build_playwright_replay_plan,
                        build_replay_plan_readiness,
                        write_l0_execution_pack,
                        write_playwright_replay_plan,
                        write_replay_plan_readiness,
                    )

                    l0_pack_path = (
                        Path(self.output_dir)
                        / "projections"
                        / "l0_execution"
                        / "l0_execution_pack.json"
                    )
                    if l0_pack_path.is_file():
                        with l0_pack_path.open(encoding="utf-8") as f:
                            l0_pack = json.load(f)
                        finalize_selectors_in_pack(l0_pack)
                        write_l0_execution_pack(str(l0_pack_path), l0_pack)
                        replay_plan = build_playwright_replay_plan(l0_pack)
                        replay_plan_path = (
                            Path(self.output_dir)
                            / "projections"
                            / "replay"
                            / "playwright_replay_plan.json"
                        )
                        write_playwright_replay_plan(str(replay_plan_path), replay_plan)
                        register_artifact(
                            Path(self.output_dir),
                            replay_plan_path,
                            artifact_id="projections.replay.playwright_replay_plan",
                            phase="frontend_backend_generation",
                            artifact_kind="det_projection",
                            api_or_det="det",
                            canonical_source=False,
                            logical_owner="l0_execution",
                            producer="site_model.page_execution_obligations.build_playwright_replay_plan",
                            allowed_consumers=("playwright_replay_executor", "developer"),
                            debug_only=False,
                            semantic_role="playwright_replay_plan",
                            source_selection={
                                "derived_from": [
                                    "phase_consolidation/l0_execution/l0_execution_pack.json"
                                ]
                            },
                            refresh_filesystem_summary=False,
                        )
                        readiness = build_replay_plan_readiness(replay_plan)
                        readiness_path = (
                            Path(self.output_dir)
                            / "projections"
                            / "replay"
                            / "playwright_replay_plan_readiness.json"
                        )
                        write_replay_plan_readiness(str(readiness_path), readiness)
                        register_artifact(
                            Path(self.output_dir),
                            readiness_path,
                            artifact_id="projections.replay.playwright_replay_plan_readiness",
                            phase="frontend_backend_generation",
                            artifact_kind="validation_report",
                            api_or_det="det",
                            canonical_source=False,
                            logical_owner="l0_execution",
                            producer="site_model.page_execution_obligations.build_replay_plan_readiness",
                            allowed_consumers=("pipeline_gate", "playwright_replay_executor", "developer"),
                            debug_only=False,
                            semantic_role="playwright_replay_plan_readiness_gate",
                            source_selection={
                                "derived_from": [
                                    "projections/replay/playwright_replay_plan.json"
                                ]
                            },
                            refresh_filesystem_summary=False,
                        )
                        readiness_summary = readiness.get("summary", {})
                        if readiness_summary.get("blocking_count"):
                            raise RuntimeError(
                                "playwright replay plan readiness gate failed: "
                                f"{readiness_summary.get('blocking_count')} unresolved executable step(s); "
                                "see projections/replay/playwright_replay_plan_readiness.json"
                            )
                        logger.log_info(
                            "l0_execution_pack target.selector finalized and playwright_replay_plan.json written"
                        )
                except Exception as exc:
                    if "playwright replay plan readiness gate failed" in str(exc):
                        raise
                    logger.log_warning(
                        f"Failed to finalize l0_execution_pack selectors: {exc}"
                    )

        import re as _re

        def _map_image_refs_to_asset(html_content: str) -> str:
            if not hash_to_asset:
                return html_content
            def _replace(m):
                ref_name = m.group(1)
                asset = hash_to_asset.get(ref_name)
                if asset:
                    return f'images/{asset}'
                return m.group(0)
            return _re.sub(r'images/([a-f0-9]{16,}\.\w+)', _replace, html_content)

        page_entity_map = {}
        page_fix_debug = {}
        for page in generated_pages:

            page_entity_map[page.filename] = page.entity_keys
            if getattr(page, 'fix_rounds', 0) or getattr(page, 'fix_issues', None):
                page_fix_debug[page.filename] = {
                    "fix_rounds": getattr(page, 'fix_rounds', 0),
                    "remaining_issues": getattr(page, 'fix_issues', []),
                }

            html = _map_image_refs_to_asset(page.html_content)
            html = self._attach_business_logic_cache_buster(html, sdk_result)
            html = _apply_page_execution_runtimes(page.filename, html)

            self._write_frontend_page_html(page.filename, html)


        enable_image_replacement = self.config_manager.get("enable_image_replacement", True)
        if enable_image_replacement:
            logger.log_step_start("Replace Page Resources", "frontend")

            html_pages = {}
            for page in generated_pages:
                fn = getattr(page, "filename", "")
                if not fn:
                    continue
                html_file = self._frontend_page_path(fn)
                if os.path.exists(html_file):
                    with open(html_file, 'r', encoding='utf-8') as f:
                        html_pages[fn] = f.read()

            page_resource_result = await self.resource_replacer.replace_page_resources_async(
                html_pages, website_type
            )
            for fn, html in page_resource_result.updated_pages.items():
                html = self._attach_business_logic_cache_buster(html, sdk_result)
                html = _apply_page_execution_runtimes(fn, html)
                page_resource_result.updated_pages[fn] = html
                self._write_frontend_page_html(fn, html)

            logger.log_step_end("Replace Page Resources", "frontend")
            final_pages = page_resource_result.updated_pages
        else:
            logger.log_info("Skipped page resource replacement (enable_image_replacement=false)")

            final_pages = {}
            for page in generated_pages:
                fn = getattr(page, "filename", "")
                if not fn:
                    continue
                page_path = self._frontend_page_path(fn)
                if os.path.exists(page_path):
                    with open(page_path, 'r', encoding='utf-8') as f:
                        html = f.read()
                        html = self._attach_business_logic_cache_buster(html, sdk_result)
                        html = _apply_page_execution_runtimes(fn, html)
                    self._write_frontend_page_html(fn, html)
                    final_pages[fn] = html

        async def _validate_page_execution_obligations(final_pages: Dict[str, str]) -> Dict[str, str]:
            if not page_execution_obligations_by_filename:
                return final_pages
            try:
                from trace2world_pipeline.validation.page_execution_obligation_validator import validate_page_execution_obligations
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to load page execution obligation validator: {exc}"
                ) from exc

            if not self._load_reference_traces_for_downstream():
                return final_pages

            debug_dir = str(pom.debug("pagegen"))
            os.makedirs(debug_dir, exist_ok=True)
            validation_path = os.path.join(
                debug_dir,
                "page_execution_obligation_validation.json",
            )
            validation = await validate_page_execution_obligations(
                self.output_dir,
                l0_execution_pack,
                output_path=validation_path,
                max_failures=32,
            )
            if validation.get("ok", False):
                logger.log_info("Page execution obligation validation passed")
                return final_pages
            failed = int(validation.get("failed_obligations") or 0)
            validated = int(validation.get("validated_obligations") or 0)
            raise RuntimeError(
                "Page execution obligation validation failed: "
                f"{failed}/{validated} obligations failed; "
                f"see {validation_path} and repair the visible PageGen control"
            )

        final_pages = await _validate_page_execution_obligations(final_pages)

        js_syntax_report = _validate_inline_javascript_syntax(
            output_dir=self.output_dir,
            pages_dir=self._frontend_pages_dir(),
            logger=logger,
        )
        if not js_syntax_report.get("ok", False):
            raise RuntimeError(
                "Inline JavaScript syntax gate failed; see "
                "phase_pagegen/validation/frontend_js_syntax/inline_js_syntax_validation.json"
            )

        if page_environment_specs:
            try:
                from trace2world_pipeline.app_spec.page_environment_spec import (
                    validate_page_environment_artifacts,
                )

                artifact_validation = validate_page_environment_artifacts(
                    page_environment_specs,
                    final_pages,
                    sdk_result,
                )
                debug_page_env_dir = str(pom.debug("page_environment"))
                os.makedirs(debug_page_env_dir, exist_ok=True)
                with open(os.path.join(debug_page_env_dir, "artifact_validation.json"), "w", encoding="utf-8") as f:
                    json.dump(artifact_validation, f, ensure_ascii=False, indent=2)
                if artifact_validation.get("ok"):
                    logger.log_info("Page environment artifact validation valid")
                else:
                    summary = artifact_validation.get("summary", {})
                    message = (
                        "Page environment artifact validation found "
                        f"{summary.get('error_count', 0)} errors and "
                        f"{summary.get('warning_count', 0)} warnings; "
                        "see phase_page_environment/debug/artifact_validation.json"
                    )
                    raise RuntimeError(message)
            except Exception as exc:
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(f"Failed to validate page environment artifacts: {exc}") from exc

        dom_binding_contracts = {}
        if isinstance(locals().get("spec_compiled_views"), dict):
            dom_binding_contracts = locals().get("spec_compiled_views", {}).get("dom_binding_contracts") or {}
        if not dom_binding_contracts:
            dom_binding_contracts_path = os.path.join(
                self.output_dir,
                "phase_page_environment",
                "semantic_api",
                "08_spec_compiled_execution_views",
                "dom_binding_contracts.json",
            )
            if os.path.exists(dom_binding_contracts_path):
                try:
                    with open(dom_binding_contracts_path, "r", encoding="utf-8") as f:
                        loaded_dom_binding_contracts = json.load(f)
                    if isinstance(loaded_dom_binding_contracts, dict):
                        dom_binding_contracts = loaded_dom_binding_contracts
                except Exception as exc:
                    logger.log_warning(f"Failed to load dom_binding_contracts artifact: {exc}")
        visible_return_affordance_report: Dict[str, Any] = {
            "format": "ghostweb_visible_return_affordance_materialization",
            "schema_revision": 1,
            "summary": {
                "declared_fallback_count": 0,
                "materialized_count": 0,
                "existing_declared_return_host_count": 0,
                "missing_page_count": 0,
            },
            "bindings": [],
        }
        if page_execution_contracts:
            try:
                from trace2world_pipeline.page_realizer.route_materializer import materialize_visible_return_affordances

                visible_return_manifest = {"pages": {}}
                for _filename, _page_contract in page_execution_contracts_by_filename.items():
                    if not isinstance(_page_contract, dict):
                        continue
                    _route_contract = _page_contract.get("route_execution_contract", {})
                    if not isinstance(_route_contract, dict):
                        continue
                    _fallback_action = (
                        _route_contract.get("fallback_back_action")
                        or _route_contract.get("default_back_action")
                        or {}
                    )
                    _target = (
                        str(_fallback_action.get("target_filename") or "").strip()
                        if isinstance(_fallback_action, dict)
                        else ""
                    )
                    if _target:
                        visible_return_manifest["pages"][str(_filename)] = {
                            "back_target": _target,
                        }
                if visible_return_manifest["pages"]:
                    visible_return_affordance_report = materialize_visible_return_affordances(
                        route_manifest=visible_return_manifest,
                        generated_pages_dir=Path(self._frontend_pages_dir()),
                        write_modified=True,
                    )
                    visible_return_report_path = os.path.join(
                        self.output_dir,
                        "phase_pagegen",
                        "validation",
                        "visible_return_affordance_materialization.json",
                    )
                    os.makedirs(os.path.dirname(visible_return_report_path), exist_ok=True)
                    with open(visible_return_report_path, "w", encoding="utf-8") as f:
                        json.dump(visible_return_affordance_report, f, ensure_ascii=False, indent=2)
                    if visible_return_affordance_report["summary"]["missing_page_count"]:
                        raise RuntimeError(
                            "Visible return affordance materialization has missing route pages; "
                            "see phase_pagegen/validation/visible_return_affordance_materialization.json"
                        )
            except Exception as exc:
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(
                    f"Failed to materialize contract-owned visible return affordances: {exc}"
                ) from exc

        if dom_binding_contracts or visible_return_affordance_report["bindings"]:
            try:
                from trace2world_pipeline.verification.action_authority import (
                    action_host_exclusion_reason,
                    build_action_host_manifest_for_page,
                    validate_action_host_coverage,
                    validate_action_host_semantics,
                )

                pages_dir = Path(self._frontend_pages_dir())
                action_contracts = {}
                if isinstance(locals().get("spec_compiled_views"), dict):
                    action_contracts = locals().get("spec_compiled_views", {}).get("action_effect_contracts") or {}
                if not action_contracts:
                    action_contracts_path = os.path.join(
                        self.output_dir,
                        "phase_page_environment",
                        "semantic_api",
                        "08_spec_compiled_execution_views",
                        "action_effect_contracts.json",
                    )
                    if os.path.exists(action_contracts_path):
                        with open(action_contracts_path, "r", encoding="utf-8") as f:
                            loaded_action_contracts = json.load(f)
                        if isinstance(loaded_action_contracts, dict):
                            action_contracts = loaded_action_contracts
                actions_by_id = action_contracts.get("actions_by_id") if isinstance(action_contracts, dict) else {}
                if not isinstance(actions_by_id, dict):
                    actions_by_id = {
                        str(item.get("action_id") or ""): item
                        for item in action_contracts.get("actions", [])
                        if isinstance(item, dict) and item.get("action_id")
                    }
                expected_actions_by_page: Dict[str, set[str]] = {}
                expected_action_semantics_by_page: Dict[str, Dict[str, Dict[str, Any]]] = {}
                excluded_non_dom_actions: List[Dict[str, str]] = []
                for binding in dom_binding_contracts.get("bindings", []):
                    if not isinstance(binding, dict):
                        continue
                    action_id = str(binding.get("action_id") or "").strip()
                    action_contract = actions_by_id.get(action_id) if action_id else None
                    page = str(
                        (action_contract or {}).get("source_filename")
                        or binding.get("source_filename")
                        or ""
                    ).strip()
                    exclusion_reason = action_host_exclusion_reason(binding)
                    if exclusion_reason:
                        if page and action_id:
                            excluded_non_dom_actions.append(
                                {"page": page, "action_id": action_id, "reason": exclusion_reason}
                            )
                        continue
                    if page and action_id:
                        expected_actions_by_page.setdefault(page, set()).add(action_id)
                        action_binding = (
                            action_contract.get("binding")
                            if isinstance(action_contract, dict)
                            and isinstance(action_contract.get("binding"), dict)
                            else {}
                        )
                        expected_action_semantics_by_page.setdefault(page, {})[action_id] = {
                            "labels": action_binding.get("labels", []),
                            "confidence": action_binding.get("confidence", ""),
                            "target_filename": str(
                                (action_contract or {}).get("target_filename")
                                or (action_contract or {}).get("target_page")
                                or ""
                            ),
                        }
                for binding in visible_return_affordance_report["bindings"]:
                    if not isinstance(binding, dict):
                        continue
                    if binding.get("status") not in {
                        "materialized",
                        "existing_declared_return_host",
                    }:
                        continue
                    page = str(binding.get("source_filename") or "").strip()
                    if page:
                        expected_actions_by_page.setdefault(page, set()).add("fallback_back")
                action_host_manifest: Dict[str, Any] = {
                    "format": "ghostweb_action_host_manifest",
                    "schema_revision": 1,
                    "source": "trace_generator.pagegen_action_host_validator",
                    "result_dir": self.output_dir,
                    "hosts": [],
                }
                action_host_validation: Dict[str, Any] = {
                    "format": "ghostweb_action_host_manifest_validation",
                    "schema_revision": 1,
                    "source": "trace_generator.pagegen_action_host_validator",
                    "missing_hosts": [],
                    "duplicate_hosts": [],
                    "semantic_mismatches": [],
                    "excluded_non_dom_actions": excluded_non_dom_actions,
                    "pages": {},
                }
                for page_name, expected_action_ids in sorted(expected_actions_by_page.items()):
                    page_path = pages_dir / page_name
                    html_text = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
                    host_rows = build_action_host_manifest_for_page(
                        page_name,
                        html_text,
                        expected_action_ids,
                        expected_action_semantics_by_page.get(page_name, {}),
                    )
                    host_coverage = validate_action_host_coverage(expected_action_ids, host_rows)
                    host_semantics = validate_action_host_semantics(
                        expected_action_semantics_by_page.get(page_name, {}),
                        host_rows,
                    )
                    host_coverage.update(host_semantics)
                    action_host_manifest["hosts"].extend(host_rows)
                    action_host_validation["pages"][page_name] = host_coverage
                    action_host_validation["missing_hosts"].extend(
                        {"page": page_name, "action_id": action_id}
                        for action_id in host_coverage.get("missing_hosts", [])
                    )
                    action_host_validation["duplicate_hosts"].extend(
                        {"page": page_name, "action_id": action_id}
                        for action_id in host_coverage.get("duplicate_hosts", [])
                    )
                    action_host_validation["semantic_mismatches"].extend(
                        {"page": page_name, **row}
                        for row in host_semantics.get("semantic_mismatches", [])
                    )
                action_host_manifest["host_count"] = len(action_host_manifest["hosts"])
                action_host_validation["missing_host_count"] = len(action_host_validation["missing_hosts"])
                action_host_validation["duplicate_host_count"] = len(action_host_validation["duplicate_hosts"])
                action_host_validation["semantic_mismatch_count"] = len(
                    action_host_validation["semantic_mismatches"]
                )
                action_host_manifest_path = os.path.join(self.output_dir, "phase_pagegen", "action_host_manifest.json")
                action_host_validation_path = os.path.join(self.output_dir, "phase_pagegen", "action_host_manifest_validation.json")
                with open(action_host_manifest_path, "w", encoding="utf-8") as f:
                    json.dump(action_host_manifest, f, ensure_ascii=False, indent=2)
                with open(action_host_validation_path, "w", encoding="utf-8") as f:
                    json.dump(action_host_validation, f, ensure_ascii=False, indent=2)
                accepted_trace_ids_for_host_coverage = sorted(
                    getattr(self, "_active_trace_ids", set()) or self._discover_active_trace_ids_from_config()
                )
                if accepted_trace_ids_for_host_coverage:
                    action_host_trace_coverage_report = _build_trace_coverage_closure_report(
                        accepted_trace_ids=accepted_trace_ids_for_host_coverage,
                        action_effect_contracts=action_contracts,
                        action_host_manifest=action_host_manifest,
                    )
                    action_host_trace_coverage_path = os.path.join(
                        self.output_dir,
                        "phase_pagegen",
                        "action_host_trace_coverage_closure_report.json",
                    )
                    with open(action_host_trace_coverage_path, "w", encoding="utf-8") as f:
                        json.dump(action_host_trace_coverage_report, f, ensure_ascii=False, indent=2)
                    action_host_trace_coverage_message = _trace_coverage_hard_gate_message(
                        action_host_trace_coverage_report,
                    )
                    if action_host_trace_coverage_message:
                        raise RuntimeError(action_host_trace_coverage_message)
                action_host_hard_gate_message = _action_host_manifest_hard_gate_message(
                    action_host_validation,
                )
                if action_host_hard_gate_message:
                    raise RuntimeError(action_host_hard_gate_message)
            except Exception as exc:
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(f"Failed to validate PageGen action host manifest: {exc}") from exc

        if page_execution_contracts:
            try:
                from trace2world_pipeline.app_spec.page_execution_contracts import validate_page_execution_contracts

                debug_dir = str(pom.debug("pagegen"))
                os.makedirs(debug_dir, exist_ok=True)
                final_validation = validate_page_execution_contracts(
                    page_execution_contracts,
                    final_pages,
                    page_environment_specs=page_environment_specs,
                )
                with open(os.path.join(debug_dir, "page_execution_contract_validation.json"), "w", encoding="utf-8") as f:
                    json.dump(final_validation, f, ensure_ascii=False, indent=2)
                invalid_pages = [
                    filename for filename, report in final_validation.get("pages", {}).items()
                    if isinstance(report, dict) and not report.get("ok", True)
                ]
                if invalid_pages:
                    message = (
                        "Page execution contract validation found contract violations in "
                        f"{len(invalid_pages)} pages: {', '.join(invalid_pages[:12])}; "
                        "see phase_page_environment/debug/page_execution_contract_validation.json"
                    )
                    raise RuntimeError(message)
                else:
                    logger.log_info("Page execution contract validation passed")
            except Exception as exc:
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(f"Failed to validate page execution contracts: {exc}") from exc

        if page_fix_debug:
            debug_dir = str(pom.debug("pagegen"))
            os.makedirs(debug_dir, exist_ok=True)
            page_fix_debug_path = os.path.join(debug_dir, "page_fix_debug.json")
            with open(page_fix_debug_path, 'w', encoding='utf-8') as f:
                json.dump(page_fix_debug, f, ensure_ascii=False, indent=2)
            logger.log_info(f"Saved page fix debug for {len(page_fix_debug)} pages")

        final_html_for_validation = {}
        for fn in sorted(final_pages):
            html_file = self._frontend_page_path(fn)
            if os.path.exists(html_file):
                with open(html_file, "r", encoding="utf-8") as f:
                    final_html_for_validation[fn] = f.read()
        validation_report = self._validate_html_sdk_call_surface(final_html_for_validation, sdk_result)
        if validation_report.get("valid", True):
            logger.log_info(
                "HTML/SDK call surface valid: {} unique WebsiteSDK calls across {} HTML files".format(
                    validation_report.get("unique_html_sdk_call_count", 0),
                    validation_report.get("html_file_count", 0),
                )
            )
        else:
            raise RuntimeError(
                "HTML/SDK call surface validation failed: missing_calls={} bare_calls={} across {} HTML files; "
                "repair the PageGen source or generated visible page before continuing".format(
                    validation_report.get("missing_call_count", 0),
                    validation_report.get("bare_call_count", 0),
                    validation_report.get("html_file_count", 0),
                )
            )


        data_file = self._seed_data_path()
        if not os.path.exists(data_file):
            static_data = dict(sdk_init_data or {})
            if not static_data and site_model and site_model.content.content_samples:
                static_data = dict(site_model.content.content_samples)

                relationships = site_model.content.entity_relationships
                if relationships:
                    from trace2world_pipeline.core.content_normalizer import assign_ids_and_resolve_fks
                    static_data = assign_ids_and_resolve_fks(static_data, relationships)
                    logger.log_info(f"  Assigned IDs and resolved FKs for {len([v for v in static_data.values() if isinstance(v, list) and v and isinstance(v[0], dict)])} collections")
            static_data["_metadata"] = {
                "website_type": website_type,
                "source": "entity_seed" if entity_seed else "content_samples",
            }
            os.makedirs(os.path.dirname(data_file), exist_ok=True)
            with open(data_file, 'w', encoding='utf-8') as wf:
                json.dump(static_data, wf, ensure_ascii=False, indent=2)
            logger.log_info(f"Saved seed_data.json ({len(static_data)} keys) for reference")

        pagegen_profile_marker_path.parent.mkdir(parents=True, exist_ok=True)
        pagegen_profile_marker_path.write_text(
            json.dumps(
                {
                    "format": "pagegen_environment_profile_provenance",
                    "schema_revision": 1,
                    "environment_profile_hash": self.environment_profile["profile_hash"],
                    "platform": self.environment_profile["platform"],
                    "page_count": len(final_pages),
                    "policy_revision": POLICY_REVISION,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        PhaseOutputManager(self.output_dir).write_json(
            "pagegen",
            "formal",
            "validation/environment_profile_validation.json",
            {
                "format": "pagegen_environment_profile_validation",
                "schema_revision": 1,
                "status": "pass",
                "platform": self.environment_profile["platform"],
                "environment_profile_hash": self.environment_profile["profile_hash"],
                "interaction_viewport": self.environment_profile["interaction_viewport"],
                "browser_viewport": self.environment_profile["browser_viewport"],
                "app_content_rect": self.environment_profile["app_content_rect"],
                "device_scale_factor": self.environment_profile["device_scale_factor"],
                "consumer_reports": [
                    "phase_pagegen/validation/dom_contract/pagegen_dom_contract_audit.json",
                    "phase_pagegen/validation/dom_contract/pagegen_runtime_dom_contract_audit.json",
                ],
            },
            artifact_kind="validation_report",
            producer="trace_generator._run_frontend_pipeline",
        )

        self._organize_final_result_layout(logger)
        try:
            artifact_index_path = rebuild_artifact_index(Path(self.output_dir))
            logger.log_info(f"Updated artifact index: {artifact_index_path}")
        except Exception as exc:
            logger.log_warning(f"Failed to rebuild artifact index: {exc}")

        return {
            'page_designs': page_designs,
            'generated_pages': generated_pages,
            'page_entity_map': page_entity_map,
        }

    def _find_design_screenshot(self) -> str:
        site_model = self.data_manager.get_site_model()
        if site_model:
            for proto in site_model.structure.page_prototypes:
                for ref in proto.screenshot_refs:
                    if os.path.exists(ref):
                        return ref
        return ""

    def _materialize_l0_visual_evidence_selection(
        self,
        site_model: AppSpec,
        logger: Trace2WorldLogger,
        *,
        page_environment_projections: Optional[Dict[str, Any]] = None,
        interaction_surface_contract: Optional[Dict[str, Any]] = None,
        action_slot_registry: Optional[Dict[str, Any]] = None,
        trace_root: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        pagegen_logic_flow = str(self.config_manager.get("pagegen_logic_flow", "standard") or "standard")
        if pagegen_logic_flow != "l0_fidelity":
            return None
        if not site_model:
            return None
        if not (page_environment_projections or interaction_surface_contract):
            logger.log_info(
                "L0 visual evidence selection skipped: page environment projections are not ready"
            )
            return None

        from trace2world_pipeline.page_realizer.pagegen_input_scope import build_pagegen_input_scopes, step_key_from_screenshot_ref
        from trace2world_pipeline.app_spec.evidence_resolver import EvidenceResolver

        evidence_resolver = EvidenceResolver.from_result_dir(
            Path(self.output_dir),
            trace_input_dir=str(trace_root or self.config_manager.get("trace_input_dir", "") or ""),
            trace_resource_dir=str(getattr(self, "_trace_resource_dir", "") or ""),
        )
        final_page_inputs = site_model.get_final_page_inputs()

        _vs_sigs = None
        _sd_sigs = None
        _formal_align_path = Path(self.output_dir) / "phase_l0_fidelity" / "formal" / "l0_visual_surface_signatures.json"
        _formal_delta_path = Path(self.output_dir) / "phase_l0_fidelity" / "formal" / "trace_state_delta_signatures.json"
        if _formal_align_path.is_file():
            try:
                _vs_sigs = json.load(open(_formal_align_path, encoding="utf-8")).get("visual_surface_signatures_by_step")
            except Exception:
                pass
        if _formal_delta_path.is_file():
            try:
                _sd_sigs = json.load(open(_formal_delta_path, encoding="utf-8")).get("state_delta_signatures_by_step")
            except Exception:
                pass

        pagegen_scopes = build_pagegen_input_scopes(
            final_page_inputs,
            [],
            fidelity_mode="l0",
            page_environment_projections=page_environment_projections or {},
            interaction_surface_contract=interaction_surface_contract or {},
            action_slot_registry=action_slot_registry or {},
            step_transition_contracts=(
                getattr(site_model, "semantic_design", {}) or {}
            ).get("step_transition_contracts", {}),
            evidence_resolver=evidence_resolver,
            visual_surface_signatures=_vs_sigs,
            state_delta_signatures=_sd_sigs,
            render_context=build_render_context(self.environment_profile),
        )

        pages: Dict[str, Any] = {}
        total_full = 0
        total_selected = 0
        total_obligations = 0
        selected_step_keys: Dict[str, List[str]] = {}
        for filename, scope in sorted(pagegen_scopes.items()):
            if not isinstance(scope, dict):
                continue
            render_scope_contract = scope.get("render_scope_contract", {}) or {}
            selected_refs = (
                render_scope_contract.get("selected_screenshot_refs")
                or scope.get("prompt_screenshot_refs")
                or []
            )
            full_refs = (
                scope.get("full_page_screenshot_refs")
                or scope.get("page_screenshot_refs")
                or []
            )
            excluded_refs = scope.get("excluded_page_screenshot_refs") or []
            surface_selection = render_scope_contract.get("surface_coverage_selection") or {}
            total_full += len(full_refs)
            total_selected += len(selected_refs)
            total_obligations += int(surface_selection.get("obligation_signature_count", 0) or 0)
            step_keys = [
                str(step_key_from_screenshot_ref(str(ref)) or "")
                for ref in selected_refs
            ]
            step_keys = [item for item in step_keys if item]
            selected_step_keys[filename] = step_keys
            pages[filename] = {
                "fidelity_mode": scope.get("fidelity_mode", "l0"),
                "full_page_screenshot_count": len(full_refs),
                "selected_screenshot_count": len(selected_refs),
                "excluded_screenshot_count": len(excluded_refs),
                "selected_screenshot_refs": selected_refs,
                "selected_step_keys": step_keys,
                "prompt_screenshot_refs": scope.get("prompt_screenshot_refs", []),
                "excluded_page_screenshot_refs": excluded_refs,
                "screenshot_render_roles": scope.get("screenshot_render_roles", {}),
                "render_scope_contract": render_scope_contract,
            }

        payload = {
            "format": "l0_visual_evidence_selection",
            "schema_revision": 1,
            "selection_policy": "l0_obligation_surface_set_cover",
            "producer": "trace_generator._materialize_l0_visual_evidence_selection",
            "inputs": {
                "page_environment_projections": bool(page_environment_projections),
                "interaction_surface_contract": bool(interaction_surface_contract),
                "action_slot_registry": bool(action_slot_registry),
            },
            "summary": {
                "page_count": len(pages),
                "full_screenshot_count": total_full,
                "selected_screenshot_count": total_selected,
                "excluded_screenshot_count": max(total_full - total_selected, 0),
                "obligation_signature_count": total_obligations,
            },
            "selected_step_keys_by_filename": selected_step_keys,
            "pages": pages,
        }
        path = PhaseOutputManager(self.output_dir).write_json(
            "l0_fidelity",
            "formal",
            "l0_visual_evidence_selection.json",
            payload,
            artifact_id="l0_fidelity.visual_evidence_selection",
            artifact_kind="det_projection",
            producer="trace_generator._materialize_l0_visual_evidence_selection",
        )
        logger.log_info(
            "Materialized l0_visual_evidence_selection.json "
            f"({len(pages)} pages, {total_selected}/{total_full} screenshots selected): {path}"
        )
        return payload

    def _filtered_resources_match_l0_selection(
        self,
        filtered_resources_path: Path,
        selection_payload: Optional[Dict[str, Any]],
        trace_root: Path,
    ) -> bool:
        if not selection_payload or not filtered_resources_path.is_file():
            return False
        try:
            payload = json.loads(filtered_resources_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        resource_scope = payload.get("resource_scope") if isinstance(payload, dict) else {}
        if not isinstance(resource_scope, dict):
            return False
        if resource_scope.get("mode") != "l0_selected_screenshots":
            return False
        from trace2world_pipeline.resources.static_resource_pipeline import build_l0_screenshot_scope
        try:
            selected_scope = build_l0_screenshot_scope(trace_root, Path(self.output_dir))
        except (OSError, ValueError, KeyError):
            return False
        if selected_scope.get("mode") != "l0_visual_evidence_selection":
            return False
        expected_refs = selected_scope.get("trace_screenshots") or {}
        observed_refs = resource_scope.get("trace_screenshots") or {}
        if not isinstance(expected_refs, dict) or not isinstance(observed_refs, dict):
            return False
        normalize = lambda mapping: {
            str(trace_id): sorted(str(ref) for ref in refs)
            for trace_id, refs in mapping.items() if isinstance(refs, list)
        }
        expected = normalize(expected_refs)
        observed = normalize(observed_refs)
        return bool(expected) and expected == observed

    def _prepare_l0_prompt_static_resources(
        self,
        site_model: AppSpec,
        logger: Trace2WorldLogger,
        *,
        page_environment_projections: Optional[Dict[str, Any]] = None,
        interaction_surface_contract: Optional[Dict[str, Any]] = None,
        action_slot_registry: Optional[Dict[str, Any]] = None,
        selection_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.config_manager.get("pagegen_l0_prompt_resource_extraction_enabled", True):
            logger.log_info("Prompt resource extraction disabled by config")
            return None

        trace_root_raw = str(self.config_manager.get("trace_input_dir", "") or "").strip()
        if not trace_root_raw:
            logger.log_warning("L0 prompt resource extraction skipped: trace_input_dir is not configured")
            return None
        trace_root = Path(trace_root_raw).expanduser().resolve()
        if not (trace_root / "index.json").exists():
            logger.log_warning(
                "L0 prompt resource extraction skipped: trace_input_dir is not a reusable reference trace root "
                f"with index.json: {trace_root}"
            )
            return None

        filtered_resources_path = trace_root / "filtered_resources.json"
        if filtered_resources_path.is_file():
            if selection_payload and not self._filtered_resources_match_l0_selection(
                filtered_resources_path,
                selection_payload,
                trace_root,
            ):
                try:
                    filtered_resources_path.unlink()
                except Exception as exc:
                    raise RuntimeError(
                        "Failed to remove stale L0 filtered_resources.json before scoped extraction: "
                        f"{filtered_resources_path}"
                    ) from exc
                logger.log_info(
                    "Removed stale filtered_resources.json because its resource_scope does not match "
                    "l0_visual_evidence_selection"
                )
            else:
                self._filtered_resources_path = str(filtered_resources_path)
                self.config_manager.set("filtered_resources_path", str(filtered_resources_path))
                logger.log_info(
                    f"Prompt resource extraction skipped: reusing {filtered_resources_path} from trace root"
                )
                return {"filtered_resources_path": str(filtered_resources_path), "reused": True}

        existing_resources = str(self._filtered_resources_path or "").strip()
        if existing_resources and os.path.isfile(existing_resources):
            existing_path = Path(existing_resources).expanduser().resolve()
            if existing_path != filtered_resources_path:
                if selection_payload and not self._filtered_resources_match_l0_selection(
                    existing_path,
                    selection_payload,
                    trace_root,
                ):
                    logger.log_info(
                        "Ignoring pre-existing filtered_resources because its resource_scope does not match "
                        f"l0_visual_evidence_selection: {existing_path}"
                    )
                else:
                    logger.log_info(
                        f"Prompt resource extraction skipped: reusing pre-existing filtered_resources at {existing_path}"
                    )
                    return {"filtered_resources_path": str(existing_path), "reused": True}

        cmd = [
            sys.executable,
            "-m",
            "trace2world_pipeline.resources.static_resource_pipeline",
            "--mt-resource",
            str(trace_root),
            "--config",
            str(self.config_manager.config_path or ""),
            "--l0-scope-from-result",
            str(Path(self.output_dir).resolve()),
        ]
        reuse_roots = self.config_manager.get("pagegen_l0_prompt_resource_reuse_roots", []) or []
        if isinstance(reuse_roots, str):
            reuse_roots = [item.strip() for item in reuse_roots.split(",") if item.strip()]
        for reuse_root in reuse_roots if isinstance(reuse_roots, list) else []:
            cmd.extend(["--reuse-resource-root", str(Path(str(reuse_root)).expanduser().resolve())])

        logger.log_step_start("L0 Prompt Resource Extraction", "PREPARE")
        logger.start_stage("L0 Prompt Resource Extraction", "prepare")
        logger.log_info(
            "Preparing L0 prompt resources from selected screenshots; invoking static resource pipeline "
            "only for missing scoped e2e_results: "
            f"{trace_root}"
        )
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.log_error("L0 prompt resource extraction failed")
            if exc.stdout:
                logger.log_error(exc.stdout[-4000:])
            if exc.stderr:
                logger.log_error(exc.stderr[-4000:])
            raise
        finally:
            logger.end_stage("L0 Prompt Resource Extraction")
            logger.log_step_end("L0 Prompt Resource Extraction", "PREPARE")

        result: Dict[str, Any] = {}
        stdout = (proc.stdout or "").strip()
        if stdout:
            result = _parse_static_resource_pipeline_stdout(stdout)
        for line in _static_resource_pipeline_log_lines(result):
            logger.log_info(line)
        self._filtered_resources_path = str(filtered_resources_path)
        self.config_manager.set("filtered_resources_path", str(filtered_resources_path))
        logger.log_info(f"L0 prompt resources ready: {filtered_resources_path}")
        try:
            register_artifact(
                Path(self.output_dir),
                filtered_resources_path,
                artifact_id="reference_trace.resources.filtered",
                phase="PIPE-P1 Build AppSpec",
                artifact_kind="det_projection",
                api_or_det="mixed",
                canonical_source=False,
                logical_owner="scripts.generation.run_static_resource_pipeline",
                producer="trace_generator._prepare_l0_prompt_static_resources",
                allowed_consumers=("pagegen_input_scope", "resource_visualization"),
                semantic_role="pagegen_static_resource_pool",
                debug_only=False,
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="cache",
                source_selection={
                    "scope": "l0_prompt_screenshots",
                    "trace_root": str(trace_root),
                    "result_dir": str(Path(self.output_dir).resolve()),
                },
                refresh_filesystem_summary=False,
            )
        except Exception as exc:
            logger.log_warning(f"Failed to register L0 prompt resource artifact: {exc}")
        return {
            "filtered_resources_path": str(filtered_resources_path),
            "trace_root": str(trace_root),
            "command": cmd,
            "result": result,
        }

    def _load_or_build_resource_asset_mapping(
        self,
        resources: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        trace_input_dir = str(self.config_manager.get("trace_input_dir", "") or "").strip()
        mapping_path = os.path.join(trace_input_dir, "resource_asset_mapping.json") if trace_input_dir else ""
        mapping: Dict[str, str] = {}
        if mapping_path and os.path.exists(mapping_path):
            try:
                with open(mapping_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                mapping = data.get("entries", {}) if isinstance(data, dict) else {}
            except Exception:
                mapping = {}
        next_index = max((int(v.split("_")[1].split(".")[0]) for v in mapping.values() if v.startswith("asset_")), default=0) + 1
        added = 0
        for resource in resources:
            src_path = str(resource.get("path", ""))
            if not src_path:
                continue
            content_hash = os.path.splitext(os.path.basename(src_path))[0]
            if content_hash in mapping:
                continue
            ext = os.path.splitext(src_path)[1] or ".jpg"
            mapping[content_hash] = f"asset_{next_index:04d}{ext}"
            next_index += 1
            added += 1
        if added and mapping_path:
            os.makedirs(os.path.dirname(mapping_path), exist_ok=True)
            with open(mapping_path, "w", encoding="utf-8") as f:
                json.dump({"format": "resource_asset_mapping", "version": 1, "next_index": next_index, "entries": mapping}, f, indent=2, ensure_ascii=False)
        return mapping

    async def _build_resource_extra_page_data(
        self,
        page_designs,
        site_model,
        logger,
        *,
        page_environment_projections: Optional[Dict[str, Dict[str, Any]]] = None,
        spec_execution_views_by_page: Optional[Dict[str, Dict[str, Any]]] = None,
        interaction_surface_contract: Optional[Dict[str, Any]] = None,
        action_slot_registry: Optional[Dict[str, Any]] = None,
        page_execution_contracts_by_filename: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Dict]]:
        if not self._filtered_resources_path or not os.path.exists(self._filtered_resources_path):
            return None

        logger.log_info(f"Loading filtered resources from {self._filtered_resources_path}")
        with open(self._filtered_resources_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        all_resources = raw.get("resources", raw) if isinstance(raw, dict) else raw

        from trace2world_pipeline.utils.page_gen_utils import dedup_resources, build_sprite_sheet, get_resource_dedup_metadata
        from trace2world_pipeline.page_realizer.pagegen_input_scope import (
            build_actual_pagegen_input_manifest,
            build_pagegen_input_scopes,
            build_pagegen_prompt_manifest,
            build_pagegen_scope_debug,
        )
        from trace2world_pipeline.page_realizer.pagegen_resource_projection import (
            make_pagegen_asset_name,
        )
        from trace2world_pipeline.page_realizer.resource_semantics import (
            apply_pagegen_prompt_resource_policy,
            label_resources_for_pagegen,
            resource_text_version,
            validate_page_wise_resource_labeling_calls,
        )
        from trace2world_pipeline.app_spec.evidence_resolver import EvidenceResolver


        pagegen_logic_flow = str(self.config_manager.get("pagegen_logic_flow", "standard") or "standard")
        if pagegen_logic_flow == "cascade":
            pagegen_logic_flow = "full"
        is_cascade_capable_pagegen = True
        resource_semantic_labeling_enabled = bool(
            self.config_manager.get("pagegen_resource_semantic_labeling_enabled", False)
        )
        resource_text_version_value = resource_text_version(resource_semantic_labeling_enabled)
        stage_model_routing = self.config_manager.get_stage_model_routing()
        semantic_labeling_model = (
            stage_model_routing.get("pagegen_resource_semantic_labeling")
            or self.config_manager.get("model", "")
            or ""
        )
        adaptive_cascade_enabled = bool(self.config_manager.get("pagegen_adaptive_cascade_enabled", True))
        adaptive_cascade_min_prompt_screenshots = int(
            self.config_manager.get("pagegen_adaptive_cascade_min_prompt_screenshots", 12) or 12
        )
        adaptive_cascade_min_resources = int(
            self.config_manager.get("pagegen_adaptive_cascade_min_resources", 48) or 48
        )

        deduped = dedup_resources(all_resources)
        global_dedup_metadata = get_resource_dedup_metadata(len(all_resources), len(deduped))
        logger.log_info(f"  Deduped resources: {len(all_resources)} -> {len(deduped)}")


        images_dir = str(PhaseOutputManager(self.output_dir).formal("pagegen", "images"))
        os.makedirs(images_dir, exist_ok=True)

        resource_asset_mapping = self._load_or_build_resource_asset_mapping(all_resources)

        def _copy_resource_assets(resources: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
            copied: List[Dict[str, Any]] = []
            asset_map: Dict[str, str] = {}
            for resource in resources:
                r = dict(resource)
                src_path = r.get("path", "")
                if not os.path.exists(src_path):
                    continue
                ext = os.path.splitext(src_path)[1] or ".jpg"
                content_hash = os.path.splitext(os.path.basename(src_path))[0]
                asset_name = resource_asset_mapping.get(content_hash)
                if not asset_name:
                    asset_name = f"asset_{len(resource_asset_mapping) + 1:04d}{ext}"
                    resource_asset_mapping[content_hash] = asset_name
                dst_path = os.path.join(images_dir, asset_name)
                if not os.path.exists(dst_path):
                    shutil.copy2(src_path, dst_path)
                asset_map[os.path.basename(src_path)] = asset_name
                r["asset_name"] = asset_name
                copied.append(r)
            return copied, asset_map

        if is_cascade_capable_pagegen:
            resource_pool, hash_to_asset = _copy_resource_assets(all_resources)
            dedup_metadata = dict(global_dedup_metadata)
            dedup_metadata.update({
                "resource_scope_mode": "cascade_raw_page_pool_round_dedup",
                "global_page_dedup_applied": False,
                "page_resource_input_count": len(all_resources),
                "page_resource_kept_count": len(resource_pool),
            })
            logger.log_info(
                f"  {pagegen_logic_flow} resource scope: using raw page resources; "
                "round-wise dedup will run after screenshot batching"
            )
        else:
            resource_pool, hash_to_asset = _copy_resource_assets(deduped)
            dedup_metadata = dict(global_dedup_metadata)
            dedup_metadata.update({
                "resource_scope_mode": "standard_page_dedup",
                "global_page_dedup_applied": True,
                "page_resource_input_count": len(all_resources),
                "page_resource_kept_count": len(resource_pool),
            })

        from trace2world_pipeline.page_realizer.route_manifest_generator import generate_route_manifest_from_site_model
        generate_route_manifest_from_site_model(site_model, self.output_dir)
        _route_runtime_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "templates", "route_runtime.js")
        _route_runtime_dst = os.path.join(self._backend_runtime_dir(), "route_runtime.js")
        if os.path.exists(_route_runtime_src):
            os.makedirs(os.path.dirname(_route_runtime_dst), exist_ok=True)
            shutil.copy2(_route_runtime_src, _route_runtime_dst)

        final_page_inputs = site_model.get_final_page_inputs() if site_model else []
        fidelity_mode = {"l0_fidelity": "l0", "full": "full"}.get(pagegen_logic_flow, "standard")
        evidence_resolver = EvidenceResolver.from_result_dir(
            Path(self.output_dir),
            trace_input_dir=str(self.config_manager.get("trace_input_dir", "") or ""),
            trace_resource_dir=str(getattr(self, "_trace_resource_dir", "") or ""),
        )
        _vs_sigs_res = None
        _sd_sigs_res = None
        _formal_vs_path = Path(self.output_dir) / "phase_l0_fidelity" / "formal" / "l0_visual_surface_signatures.json"
        _formal_sd_path = Path(self.output_dir) / "phase_l0_fidelity" / "formal" / "trace_state_delta_signatures.json"
        if _formal_vs_path.is_file():
            try:
                _vs_sigs_res = json.load(open(_formal_vs_path, encoding="utf-8")).get("visual_surface_signatures_by_step")
            except Exception:
                pass
        if _formal_sd_path.is_file():
            try:
                _sd_sigs_res = json.load(open(_formal_sd_path, encoding="utf-8")).get("state_delta_signatures_by_step")
            except Exception:
                pass

        pagegen_scopes = build_pagegen_input_scopes(
            final_page_inputs,
            resource_pool,
            fidelity_mode=fidelity_mode,
            page_environment_projections=page_environment_projections,
            spec_execution_views_by_page=spec_execution_views_by_page,
            interaction_surface_contract=interaction_surface_contract,
            action_slot_registry=action_slot_registry,
            step_transition_contracts=(
                getattr(site_model, "semantic_design", {}) or {}
            ).get("step_transition_contracts", {}),
            evidence_resolver=evidence_resolver,
            visual_surface_signatures=_vs_sigs_res,
            state_delta_signatures=_sd_sigs_res,
            render_context=build_render_context(self.environment_profile),
        )
        alignment_items = {
            filename: scope.get("pagegen_evidence_alignment", {})
            for filename, scope in pagegen_scopes.items()
            if isinstance(scope, dict) and scope.get("pagegen_evidence_alignment")
        }
        blocking_alignment_issues = [
            issue
            for item in alignment_items.values()
            for issue in item.get("issues", [])
            if isinstance(issue, dict) and issue.get("severity") == "blocking"
        ]
        alignment_audit = {
            "format": "pagegen_evidence_alignment_audit",
            "schema_revision": 1,
            "summary": {
                "page_count": len(pagegen_scopes),
                "audited_page_count": len(alignment_items),
                "blocking_issue_count": len(blocking_alignment_issues),
                "warning_issue_count": sum(
                    1
                    for item in alignment_items.values()
                    for issue in item.get("issues", [])
                    if isinstance(issue, dict) and issue.get("severity") == "warning"
                ),
            },
            "pages": alignment_items,
        }
        from trace2world_pipeline.core.phase_output import PhaseOutputManager as _POM
        _pagegen_pom = _POM(self.output_dir)
        pagegen_reports_dir = _pagegen_pom.debug("pagegen")
        pagegen_reports_dir.mkdir(parents=True, exist_ok=True)
        alignment_audit_path = pagegen_reports_dir / "pagegen_evidence_alignment_audit.json"
        alignment_audit_path.write_text(
            json.dumps(alignment_audit, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        rows: List[str] = []
        for filename, item in sorted(alignment_items.items()):
            issues = item.get("issues", []) if isinstance(item, dict) else []
            first_refs = item.get("evidence_refs", [])[:3] if isinstance(item, dict) else []
            rows.append(
                "<tr>"
                f"<td>{_html_escape(str(filename))}</td>"
                f"<td>{int(item.get('blocking_issue_count', 0) if isinstance(item, dict) else 0)}</td>"
                f"<td>{int(item.get('warning_issue_count', 0) if isinstance(item, dict) else 0)}</td>"
                f"<td><pre>{_html_escape(json.dumps(first_refs, ensure_ascii=False, indent=2))}</pre></td>"
                f"<td><pre>{_html_escape(json.dumps(issues[:5], ensure_ascii=False, indent=2))}</pre></td>"
                "</tr>"
            )
        alignment_html_path = pagegen_reports_dir / "pagegen_evidence_alignment_audit.html"
        alignment_html_path.write_text(
            "\n".join([
                "<!doctype html><meta charset='utf-8'>",
                "<title>PageGen Evidence Alignment Audit</title>",
                "<style>body{font-family:Arial,sans-serif;margin:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;vertical-align:top}th{background:#f5f5f5}pre{white-space:pre-wrap;margin:0;font-size:12px}</style>",
                "<h1>PageGen Evidence Alignment Audit</h1>",
                f"<p>Pages: {len(pagegen_scopes)}; blocking: {len(blocking_alignment_issues)}; warnings: {alignment_audit['summary']['warning_issue_count']}</p>",
                "<table><thead><tr><th>Filename</th><th>Blocking</th><th>Warnings</th><th>Evidence sample</th><th>Issue sample</th></tr></thead><tbody>",
                *rows,
                "</tbody></table>",
            ]),
            encoding="utf-8",
        )
        if blocking_alignment_issues and self.config_manager.get("pagegen_evidence_alignment_blocking_enabled", True):
            sample = blocking_alignment_issues[:3]
            raise ValueError(
                "PageGen evidence alignment failed before resource/PageGen assembly: "
                f"{len(blocking_alignment_issues)} blocking issue(s); sample={sample}"
            )
        matched_resource_paths = {
            str(resource.get("path", ""))
            for scope in pagegen_scopes.values()
            for resource in scope.get("page_resources", [])
        }
        unmatched = sum(1 for r in resource_pool if str(r.get("path", "")) not in matched_resource_paths)

        logger.log_info(
            f"  Resource→page mapping: {sum(len(s.get('page_resources', [])) for s in pagegen_scopes.values())} mapped, "
            f"{unmatched} unmatched, {len(pagegen_scopes)} pages"
        )

        _scope_pom = PhaseOutputManager(self.output_dir)
        scope_selection_summary = {
            "format": "pagegen_scope_selection",
            "schema_revision": 1,
            "page_count": len(pagegen_scopes),
            "pages": {},
        }
        for _fn, _sc in sorted(pagegen_scopes.items()):
            if not isinstance(_sc, dict):
                continue
            scope_selection_summary["pages"][_fn] = {
                "fidelity_mode": _sc.get("fidelity_mode", ""),
                "prompt_screenshot_count": len(_sc.get("prompt_screenshot_refs", [])),
                "excluded_screenshot_count": len(_sc.get("excluded_page_screenshot_refs", [])),
                "task_extra_screenshot_count": len(_sc.get("task_extra_screenshot_refs", [])),
                "resource_count": len(_sc.get("page_resources", [])),
                "render_scope_contract": _sc.get("render_scope_contract", {}),
                "prompt_screenshot_refs": _sc.get("prompt_screenshot_refs", []),
                "excluded_page_screenshot_refs": _sc.get("excluded_page_screenshot_refs", []),
                "screenshot_render_roles": _sc.get("screenshot_render_roles", {}),
            }
        _scope_pom.write_json(
            "pagegen_input_scope", "formal",
            "pagegen_scope_selection.json",
            scope_selection_summary,
            artifact_id="pagegen_input_scope.pagegen_scope_selection",
            artifact_kind="det_projection",
            producer="trace_generator._run_frontend_pipeline",
        )
        logger.log_info(
            f"Materialized pagegen_scope_selection.json ({len(scope_selection_summary['pages'])} pages)"
        )


        extra_page_data: Dict[str, Dict] = {}

        def _annotate_pagegen_scope_manifest(filename: str, scope: Dict[str, Any]) -> None:
            scope["environment_prompt_policy"] = prompt_policy_manifest(
                self.environment_profile,
                "pagegen_generation",
            )
            scope["artifact_metadata"] = {
                "artifact_id": f"projections.pagegen.prompt_manifest.{filename}",
                "artifact_kind": "det_projection",
                "api_or_det": "mixed" if resource_semantic_labeling_enabled else "det",
                "canonical_source": False,
                "canonical_semantic_source": False,
                "canonical_prompt_boundary": True,
                "artifact_role": "projection",
                "logical_owner": "frontend.pagegen_input_scope",
                "semantic_role": "pagegen_input_boundary_manifest",
                "producer": "trace_generator._prepare_static_resources",
                "allowed_consumers": ["pagegen", "debug_visualization"],
                "debug_only": False,
                "derived_from": "semantic_api.page_environment_specs",
            }

        def _register_pagegen_scope_manifest(
            filename: str,
            manifest_path: str,
            *,
            artifact_id_prefix: str = "projections.pagegen.prompt_manifest",
            artifact_kind: str = "det_projection",
            semantic_role: str = "pagegen_input_boundary_manifest",
            debug_only: bool = False,
            canonical_prompt_boundary: bool = True,
            artifact_role: str = "projection",
        ) -> None:
            register_artifact(
                Path(self.output_dir),
                Path(manifest_path),
                artifact_id=f"{artifact_id_prefix}.{filename}",
                phase="pagegen_input_assembly",
                artifact_kind=artifact_kind,
                api_or_det="mixed" if resource_semantic_labeling_enabled else "det",
                canonical_source=False,
                logical_owner="frontend.pagegen_input_scope",
                producer="trace_generator._prepare_static_resources",
                allowed_consumers=("pagegen", "debug_visualization") if not debug_only else ("developer", "cache_resume"),
                semantic_role=semantic_role,
                debug_only=debug_only,
                canonical_semantic_source=False,
                canonical_prompt_boundary=canonical_prompt_boundary,
                artifact_role=artifact_role,
                source_selection={"derived_from": "semantic_api.page_environment_specs"} if not debug_only else None,
                notes=(
                    "Lean PageGen prompt manifest; semantic authority comes from page_environment_spec projection."
                    if not debug_only
                    else "Full PageGen scope debug/cache export; not a direct prompt input."
                ),
                refresh_filesystem_summary=False,
            )

        def _write_pagegen_resource_projection(
            filename: str,
            page_scope: Dict[str, Any],
            resources: List[Dict[str, Any]],
            semantic_cache: Optional[Dict[str, Any]] = None,
        ) -> tuple[str, Dict[str, Any]]:
            return _write_pagegen_resource_projection_artifact(
                output_dir=self.output_dir,
                filename=filename,
                page_scope=page_scope,
                resources=resources,
                semantic_cache=semantic_cache,
                producer="trace_generator._prepare_static_resources",
            )

        def _write_resource_list_from_projection(
            resource_list_path: str,
            projection: Dict[str, Any],
        ) -> str:
            return _write_resource_list_from_projection_artifact(resource_list_path, projection)

        def _sync_actual_pagegen_input_fields(
            page_scope: Dict[str, Any],
            *,
            resource_list_path: str,
            semantic_labeling_path: str,
            projection_path: str = "",
            sprite_result: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            page_scope["resource_list_path"] = resource_list_path
            page_scope["resource_semantic_labeling_path"] = semantic_labeling_path
            if projection_path:
                page_scope["pagegen_resource_projection_path"] = projection_path
            if isinstance(sprite_result, dict):
                for key in ("icon_sprite_path", "photo_sprite_path", "asset_sprite_path"):
                    if sprite_result.get(key):
                        page_scope[key] = sprite_result[key]
            return page_scope

        def _write_pagegen_actual_input_manifest(
            filename: str,
            page_scope: Dict[str, Any],
        ) -> str:
            slug = filename.replace(".html", "")
            actual_dir = PhaseOutputManager(self.output_dir).formal("pagegen", "page_inputs", slug)
            actual_dir.mkdir(parents=True, exist_ok=True)
            actual_manifest_path = actual_dir / "actual_pagegen_input_manifest.json"
            actual_manifest = build_actual_pagegen_input_manifest(page_scope)
            actual_manifest_path.write_text(
                json.dumps(actual_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            register_artifact(
                Path(self.output_dir),
                actual_manifest_path,
                artifact_id=f"pagegen.actual_inputs.{filename}",
                phase="pagegen_input_assembly",
                artifact_kind="det_projection",
                api_or_det="det",
                canonical_source=False,
                logical_owner="frontend.pagegen_input_scope",
                producer="trace_generator._prepare_static_resources",
                allowed_consumers=("debug_visualization", "audit"),
                semantic_role="actual_pagegen_input_manifest",
                debug_only=False,
                canonical_semantic_source=False,
                canonical_prompt_boundary=False,
                artifact_role="audit_manifest",
                refresh_filesystem_summary=False,
            )
            return str(actual_manifest_path)

        def _write_pagegen_scope_artifacts(
            filename: str,
            page_scope: Dict[str, Any],
            sprite_dir: str,
        ) -> tuple[str, str, str]:
            os.makedirs(sprite_dir, exist_ok=True)
            prompt_manifest_path = os.path.join(sprite_dir, "pagegen_prompt_manifest.json")
            legacy_prompt_manifest_path = os.path.join(sprite_dir, "pagegen_input_scope.json")
            debug_manifest_path = os.path.join(sprite_dir, "pagegen_scope_debug.json")
            prompt_manifest = build_pagegen_prompt_manifest(page_scope)
            debug_scope = build_pagegen_scope_debug(page_scope)
            for path, payload in (
                (prompt_manifest_path, prompt_manifest),
                (legacy_prompt_manifest_path, prompt_manifest),
                (debug_manifest_path, debug_scope),
            ):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            _register_pagegen_scope_manifest(filename, prompt_manifest_path)
            _register_pagegen_scope_manifest(
                filename,
                legacy_prompt_manifest_path,
                artifact_id_prefix="projections.pagegen.input_scope_legacy",
                artifact_kind="debug_export",
                semantic_role="legacy_pagegen_input_scope_mirror",
                debug_only=True,
                canonical_prompt_boundary=False,
                artifact_role="debug_cache",
            )
            _register_pagegen_scope_manifest(
                filename,
                debug_manifest_path,
                artifact_id_prefix="debug.pagegen.scope",
                artifact_kind="debug_export",
                semantic_role="pagegen_scope_debug_cache_manifest",
                debug_only=True,
                canonical_prompt_boundary=False,
                artifact_role="debug_cache",
            )
            blocking_issues = (
                prompt_manifest.get("manifest_blocking_issues", [])
                if isinstance(prompt_manifest, dict)
                else []
            )
            if blocking_issues:
                raise RuntimeError(
                    "PageGen prompt manifest blocking gate failed for "
                    f"{filename}: {len(blocking_issues)} blocking issue(s). "
                    f"See {prompt_manifest_path}."
                )
            return prompt_manifest_path, legacy_prompt_manifest_path, debug_manifest_path

        def _page_resource_data(
            page_scope: Dict[str, Any],
            page_res: List[Dict[str, Any]],
            dedup_page_res: List[Dict[str, Any]],
            resource_list_text: str,
            semantic_labeling_report: Dict[str, Any],
            pagegen_execution_flow: str,
        ) -> Dict[str, Any]:
            return {
                "resource_list_text": resource_list_text,
                "page_resources": page_res,
                "raw_resource_count": len(page_res),
                "dedup_resource_count": len(dedup_page_res),
                "dedup_page_resources": dedup_page_res,
                "dedup_resource_list_text": resource_list_text,
                "resource_semantic_labeling": semantic_labeling_report,
                "pagegen_execution_flow": pagegen_execution_flow,
                "pagegen_input_scope": page_scope,
                "page_screenshot_refs": page_scope.get("page_screenshot_refs", []),
                "prompt_screenshot_refs": page_scope.get("prompt_screenshot_refs", []),
                "non_prompt_page_screenshot_refs": page_scope.get("non_prompt_page_screenshot_refs", []),
                "unique_prompt_screenshot_refs": page_scope.get("unique_prompt_screenshot_refs", []),
                "fidelity_mode": page_scope.get("fidelity_mode", "standard"),
                "render_scope_contract": page_scope.get("render_scope_contract", {}),
                "full_page_screenshot_refs": page_scope.get("full_page_screenshot_refs", []),
                "task_extra_screenshot_refs": page_scope.get("task_extra_screenshot_refs", []),
                "excluded_page_screenshot_refs": page_scope.get("excluded_page_screenshot_refs", []),
            }

        async def _label_standard_page_resources(
            *,
            filename: str,
            page_scope: Dict[str, Any],
            resources: List[Dict[str, Any]],
            sprite_paths: List[str],
            semantic_labeling_path: str,
            page_context: Dict[str, Any],
        ) -> tuple[List[Dict[str, Any]], Dict[str, Any], bool, Dict[str, Any]]:
            if page_scope.get("pagegen_execution_flow") == "cascade":
                return resources, {
                    "enabled": resource_semantic_labeling_enabled,
                    "skipped_reason": "cascade_round_labels_resources",
                }, False, {}
            if not resource_semantic_labeling_enabled:
                return resources, {"enabled": False}, False, {}
            reuse_key = _resource_semantic_labeling_reuse_key(
                page_scope,
                sprite_paths=sprite_paths,
                model=semantic_labeling_model,
            )
            try:
                labeled_resources, semantic_labeling_report = await label_resources_for_pagegen(
                    resources,
                    sprite_paths=sprite_paths,
                    page_context=page_context,
                    enabled=True,
                    model=semantic_labeling_model,
                )
            except RuntimeError as exc:
                labeled_resources = resources
                semantic_labeling_report = {
                    "enabled": True,
                    "resource_count": len(resources),
                    "annotated_count": 0,
                    "skipped_reason": f"event_loop_error:{type(exc).__name__}",
                }
            return labeled_resources, semantic_labeling_report, True, reuse_key

        async def _process_page_resource_input(pd: Any) -> Optional[Dict[str, Any]]:
            fn = pd.filename if hasattr(pd, 'filename') else pd.get('filename', '')
            if not fn:
                return None

            page_scope = pagegen_scopes.get(fn, {})
            page_res = page_scope.get("page_resources", [])
            page_scope = dict(page_scope)
            page_scope["resource_dedup"] = dedup_metadata
            page_sprite_dir = _phase_pagegen_sprites_dir(self.output_dir, fn)
            cache_path = os.path.join(page_sprite_dir, "resource_list.txt")
            semantic_labeling_path = _resource_semantic_labeling_path(page_sprite_dir)
            semantic_labeling_status_path = _resource_semantic_labeling_status_path(page_sprite_dir)
            prompt_manifest_path = os.path.join(page_sprite_dir, "pagegen_prompt_manifest.json")
            legacy_prompt_manifest_path = os.path.join(page_sprite_dir, "pagegen_input_scope.json")
            debug_manifest_path = os.path.join(page_sprite_dir, "pagegen_scope_debug.json")
            if not page_res:
                page_scope["raw_resource_count"] = 0
                page_scope["dedup_resource_count"] = 0
                page_scope["dedup_page_resources"] = []
                page_scope["resource_semantic_labeling"] = {
                    "enabled": resource_semantic_labeling_enabled,
                    "skipped_reason": "no_page_resources",
                }
                page_scope["resource_text_version"] = resource_text_version_value
                page_scope["pagegen_execution_flow"] = "single_pass"
                _annotate_pagegen_scope_manifest(fn, page_scope)
                os.makedirs(page_sprite_dir, exist_ok=True)
                projection_path, projection = _write_pagegen_resource_projection(fn, page_scope, [])
                resource_list_text = _write_resource_list_from_projection(cache_path, projection)
                _sync_actual_pagegen_input_fields(
                    page_scope,
                    resource_list_path=cache_path,
                    semantic_labeling_path=semantic_labeling_path,
                    projection_path=projection_path,
                )
                _write_pagegen_scope_artifacts(fn, page_scope, page_sprite_dir)
                _write_pagegen_actual_input_manifest(fn, page_scope)
                _write_resource_semantic_labeling_status(
                    semantic_labeling_status_path,
                    filename=fn,
                    page_scope=page_scope,
                    semantic_labeling_report=page_scope["resource_semantic_labeling"],
                    reason="no_page_resources",
                )
                page_data = _page_resource_data(
                    page_scope,
                    [],
                    [],
                    resource_list_text,
                    page_scope["resource_semantic_labeling"],
                    "single_pass",
                )
                page_data.update({
                    "pagegen_resource_projection": projection,
                    "pagegen_resource_projection_path": projection_path,
                    "resource_list_path": cache_path,
                    "resource_semantic_labeling_path": semantic_labeling_path,
                })
                return {
                    "filename": fn,
                    "page_data": page_data,
                    "api_submitted": False,
                    "semantic_cache_hit": False,
                    "no_resources": True,
                }
            dedup_page_res = dedup_resources(page_res)
            page_scope["raw_resource_count"] = len(page_res)
            page_scope["dedup_resource_count"] = len(dedup_page_res)
            page_scope["dedup_resource_cache_key"] = _resource_cache_key(dedup_page_res)
            resource_cache_key = _resource_cache_key(page_res)
            page_scope["resource_cache_key"] = resource_cache_key
            page_scope["resource_text_version"] = resource_text_version_value
            pagegen_execution_flow = _resolve_pagegen_resource_semantic_execution_flow(
                pagegen_logic_flow=pagegen_logic_flow,
                prompt_screenshot_count=len(
                    page_scope.get("prompt_screenshot_refs")
                    or page_scope.get("page_screenshot_refs")
                    or []
                ),
                dedup_resource_count=len(dedup_page_res),
                adaptive_cascade_enabled=adaptive_cascade_enabled,
                adaptive_cascade_min_prompt_screenshots=adaptive_cascade_min_prompt_screenshots,
                adaptive_cascade_min_resources=adaptive_cascade_min_resources,
                cascade_available=bool(page_scope),
            )
            page_scope["pagegen_execution_flow"] = pagegen_execution_flow


            _annotate_pagegen_scope_manifest(fn, page_scope)
            semantic_cache_hit = False
            api_submitted = False
            semantic_reuse_key: Dict[str, Any] = {}
            standard_sprite_paths: List[str] = []
            sprite_result: Dict[str, Any] = {}
            if _resource_sprite_cache_is_valid(
                cache_path,
                debug_manifest_path,
                dedup_metadata,
                resource_cache_key,
                resource_text_version_value,
            ):
                cached = open(cache_path, 'r', encoding='utf-8').read()
                cached_manifest: Dict[str, Any] = {}
                try:
                    with open(debug_manifest_path, 'r', encoding='utf-8') as f:
                        cached_manifest = json.load(f)
                except Exception:
                    cached_manifest = {}
                if pagegen_execution_flow == "single_pass":
                    sprite_result = build_sprite_sheet(dedup_page_res, page_sprite_dir)
                    standard_sprite_paths = [
                        path for path in (
                            sprite_result.get("icon_sprite_path"),
                            sprite_result.get("photo_sprite_path"),
                            sprite_result.get("asset_sprite_path"),
                        )
                        if path
                    ]
                reuse_key = _resource_semantic_labeling_reuse_key(
                    page_scope,
                    sprite_paths=standard_sprite_paths,
                    model=semantic_labeling_model,
                )
                semantic_labeling_artifact = _load_resource_semantic_labeling_artifact(
                    semantic_labeling_path,
                    expected_reuse_key=reuse_key,
                )
                if resource_semantic_labeling_enabled and semantic_labeling_artifact:
                    semantic_cache_hit = True
                    cached_resources = semantic_labeling_artifact.get("dedup_page_resources") or []
                    dedup_page_res = [
                        apply_pagegen_prompt_resource_policy(item) if isinstance(item, dict) else item
                        for item in cached_resources
                        if isinstance(item, dict)
                    ]
                    semantic_labeling_report = semantic_labeling_artifact.get(
                        "resource_semantic_labeling",
                        {"enabled": True, "source": "resource_semantic_labeling_artifact"},
                    )
                else:
                    dedup_page_res, semantic_labeling_report, api_submitted, semantic_reuse_key = await _label_standard_page_resources(
                        filename=fn,
                        page_scope=page_scope,
                        resources=dedup_page_res,
                        sprite_paths=standard_sprite_paths,
                        semantic_labeling_path=semantic_labeling_path,
                        page_context={
                            "filename": fn,
                            "page_name": getattr(pd, "page_name", None) or (pd.get("page_name", "") if isinstance(pd, dict) else ""),
                            "logic_flow": pagegen_logic_flow,
                            "pagegen_execution_flow": pagegen_execution_flow,
                            "resource_scope": "single_pass_dedup_page",
                        },
                    )
                page_scope["dedup_page_resources"] = dedup_page_res
                page_scope["dedup_resource_count"] = len(dedup_page_res)
                page_scope["resource_semantic_labeling"] = semantic_labeling_report
                if pagegen_execution_flow == "single_pass":
                    sprite_result = build_sprite_sheet(dedup_page_res, page_sprite_dir)
                projection_path, projection = _write_pagegen_resource_projection(fn, page_scope, dedup_page_res)
                resource_list_text = _write_resource_list_from_projection(cache_path, projection)
                page_data = _page_resource_data(
                    page_scope,
                    page_res,
                    dedup_page_res,
                    resource_list_text,
                    semantic_labeling_report,
                    pagegen_execution_flow,
                )
                if pagegen_execution_flow == "single_pass":
                    if sprite_result.get("icon_sprite_path"):
                        page_data["icon_sprite_path"] = sprite_result["icon_sprite_path"]
                    if sprite_result.get("photo_sprite_path"):
                        page_data["photo_sprite_path"] = sprite_result["photo_sprite_path"]
                    if sprite_result.get("asset_sprite_path"):
                        page_data["asset_sprite_path"] = sprite_result["asset_sprite_path"]
                _sync_actual_pagegen_input_fields(
                    page_scope,
                    resource_list_path=cache_path,
                    semantic_labeling_path=semantic_labeling_path,
                    projection_path=projection_path,
                    sprite_result=sprite_result,
                )
                _write_pagegen_scope_artifacts(fn, page_scope, page_sprite_dir)
                _write_pagegen_actual_input_manifest(fn, page_scope)
                page_data["pagegen_resource_projection"] = projection
                page_data["pagegen_resource_projection_path"] = projection_path
                page_data["resource_list_path"] = cache_path
                page_data["resource_semantic_labeling_path"] = semantic_labeling_path
                if api_submitted:
                    _write_resource_semantic_labeling_artifact(
                        semantic_labeling_path,
                        filename=fn,
                        page_scope=page_scope,
                        labeled_resources=dedup_page_res,
                        semantic_labeling_report=semantic_labeling_report,
                        reuse_key=semantic_reuse_key or _resource_semantic_labeling_reuse_key(
                            page_scope,
                            sprite_paths=standard_sprite_paths,
                            model=semantic_labeling_model,
                        ),
                        resource_list_text=resource_list_text,
                    )
                elif not semantic_cache_hit:
                    _write_resource_semantic_labeling_status(
                        semantic_labeling_status_path,
                        filename=fn,
                        page_scope=page_scope,
                        semantic_labeling_report=semantic_labeling_report,
                        reason=str(semantic_labeling_report.get("skipped_reason") or "labeling_not_submitted"),
                    )
                return {
                    "filename": fn,
                    "page_data": page_data,
                    "api_submitted": api_submitted,
                    "semantic_cache_hit": semantic_cache_hit,
                    "no_resources": False,
                }


            sprite_result = {} if pagegen_execution_flow == "cascade" else build_sprite_sheet(dedup_page_res, page_sprite_dir)
            standard_sprite_paths = [
                path for path in (
                    sprite_result.get("icon_sprite_path"),
                    sprite_result.get("photo_sprite_path"),
                    sprite_result.get("asset_sprite_path"),
                )
                if path
            ]
            dedup_page_res, semantic_labeling_report, api_submitted, semantic_reuse_key = await _label_standard_page_resources(
                filename=fn,
                page_scope=page_scope,
                resources=dedup_page_res,
                sprite_paths=standard_sprite_paths,
                semantic_labeling_path=semantic_labeling_path,
                page_context={
                    "filename": fn,
                    "page_name": getattr(pd, "page_name", None) or (pd.get("page_name", "") if isinstance(pd, dict) else ""),
                    "logic_flow": pagegen_logic_flow,
                    "pagegen_execution_flow": pagegen_execution_flow,
                    "resource_scope": "single_pass_dedup_page",
                },
            )
            page_scope["dedup_page_resources"] = dedup_page_res
            page_scope["dedup_resource_count"] = len(dedup_page_res)
            page_scope["resource_semantic_labeling"] = semantic_labeling_report
            if pagegen_execution_flow == "single_pass" and api_submitted:
                sprite_result = build_sprite_sheet(dedup_page_res, page_sprite_dir)
            projection_path, projection = _write_pagegen_resource_projection(fn, page_scope, dedup_page_res)
            resource_text = _write_resource_list_from_projection(cache_path, projection)

            page_data = _page_resource_data(
                page_scope,
                page_res,
                dedup_page_res,
                resource_text,
                semantic_labeling_report,
                pagegen_execution_flow,
            )
            if sprite_result.get("icon_sprite_path"):
                page_data["icon_sprite_path"] = sprite_result["icon_sprite_path"]
            if sprite_result.get("photo_sprite_path"):
                page_data["photo_sprite_path"] = sprite_result["photo_sprite_path"]
            if sprite_result.get("asset_sprite_path"):
                page_data["asset_sprite_path"] = sprite_result["asset_sprite_path"]
            _sync_actual_pagegen_input_fields(
                page_scope,
                resource_list_path=cache_path,
                semantic_labeling_path=semantic_labeling_path,
                projection_path=projection_path,
                sprite_result=sprite_result,
            )
            _write_pagegen_scope_artifacts(fn, page_scope, page_sprite_dir)
            _write_pagegen_actual_input_manifest(fn, page_scope)
            page_data["pagegen_resource_projection"] = projection
            page_data["pagegen_resource_projection_path"] = projection_path
            page_data["resource_list_path"] = cache_path
            page_data["resource_semantic_labeling_path"] = semantic_labeling_path
            if api_submitted:
                _write_resource_semantic_labeling_artifact(
                    semantic_labeling_path,
                    filename=fn,
                    page_scope=page_scope,
                    labeled_resources=dedup_page_res,
                    semantic_labeling_report=semantic_labeling_report,
                    reuse_key=semantic_reuse_key or _resource_semantic_labeling_reuse_key(
                        page_scope,
                        sprite_paths=standard_sprite_paths,
                        model=semantic_labeling_model,
                    ),
                    resource_list_text=resource_text,
                )
            elif not semantic_cache_hit:
                _write_resource_semantic_labeling_status(
                    semantic_labeling_status_path,
                    filename=fn,
                    page_scope=page_scope,
                    semantic_labeling_report=semantic_labeling_report,
                    reason=str(semantic_labeling_report.get("skipped_reason") or "labeling_not_submitted"),
                )
            return {
                "filename": fn,
                "page_data": page_data,
                "api_submitted": api_submitted,
                "semantic_cache_hit": False,
                "no_resources": False,
            }

        logger.log_info(
            f"  Page resource assembly streaming: pages={len(page_designs)}, "
            f"semantic_labeling_enabled={resource_semantic_labeling_enabled}; "
            "API concurrency is delegated to core.llm_caller"
        )
        page_resource_results = await _run_pagegen_resource_streaming_jobs(
            list(page_designs),
            _process_page_resource_input,
        )
        streaming_summary = {
            "finalized": 0,
            "api_submitted": 0,
            "semantic_cache_hits": 0,
            "no_resource_pages": 0,
        }
        for result in page_resource_results:
            if not isinstance(result, dict):
                continue
            fn = str(result.get("filename") or "")
            page_data = result.get("page_data")
            if fn and isinstance(page_data, dict):
                extra_page_data[fn] = page_data
                streaming_summary["finalized"] += 1
            if result.get("api_submitted"):
                streaming_summary["api_submitted"] += 1
            if result.get("semantic_cache_hit"):
                streaming_summary["semantic_cache_hits"] += 1
            if result.get("no_resources"):
                streaming_summary["no_resource_pages"] += 1
        logger.log_info(
            "  Page resource assembly streaming complete: "
            f"finalized={streaming_summary['finalized']}/{len(page_designs)}, "
            f"api_submitted={streaming_summary['api_submitted']}, "
            f"semantic_cache_hits={streaming_summary['semantic_cache_hits']}, "
            f"no_resource_pages={streaming_summary['no_resource_pages']}"
        )

        semantic_call_records: List[Dict[str, Any]] = []
        for page_data in extra_page_data.values():
            report = page_data.get("resource_semantic_labeling") if isinstance(page_data, dict) else {}
            if not isinstance(report, dict):
                continue
            records = report.get("labeling_call_records")
            if isinstance(records, list):
                semantic_call_records.extend(record for record in records if isinstance(record, dict))
                continue
            usage = report.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("labeling_call_records"), list):
                semantic_call_records.extend(
                    record for record in usage.get("labeling_call_records", []) if isinstance(record, dict)
                )
        semantic_validation = validate_page_wise_resource_labeling_calls(semantic_call_records)
        from trace2world_pipeline.core.phase_output import PhaseOutputManager as _POM
        _res_pom = _POM(self.output_dir)
        validation_path = str(_res_pom.debug("content_extraction", "resource_semantic_labeling_validation.json"))
        try:
            os.makedirs(os.path.dirname(validation_path), exist_ok=True)
            with open(validation_path, "w", encoding="utf-8") as f:
                json.dump(semantic_validation, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.log_warning(f"  Failed to write resource semantic labeling validation: {exc}")
        if semantic_validation.get("blocking_error_count"):
            raise RuntimeError(
                "Resource semantic labeling validation failed: "
                f"{semantic_validation.get('blocking_error_count')} screenshot-wise violation(s)"
            )

        logger.log_info(f"  Resource preprocessing complete: {len(extra_page_data)} pages")
        return extra_page_data, hash_to_asset

    def _extract_domain_constraints_from_schema(self, schemas):
        if not schemas or not isinstance(schemas, list):
            return {}

        result = {}
        for entity in schemas:
            if not isinstance(entity, dict):
                continue
            entity_name = entity.get("name", "")
            fields = entity.get("fields", [])
            if not isinstance(fields, list):
                continue
            for field in fields:
                if not isinstance(field, dict):
                    continue
                domain_type = field.get("domain_type")
                domain_constraints = field.get("domain_constraints")
                if domain_type is None and domain_constraints is None:
                    continue
                field_name = field.get("name", "")
                key = f"{entity_name}.{field_name}"
                result[key] = {
                    "domain_type": domain_type,
                    "domain_constraints": domain_constraints,
                    "type": field.get("type"),
                }
        return result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Trace2World environment constructor")
    parser.add_argument("--config", type=str, required=True, help="Config file path")
    parser.add_argument("--trace-dirs", type=str, nargs='+', help="Trace directory paths")
    parser.add_argument("--trace-base-dir", type=str, help="Base dir to auto-discover traces")
    parser.add_argument("--task-metadata", type=str, help="Path to Online_Mind2Web.json")
    parser.add_argument("--website-filter", type=str, help="Website URL filter (e.g. example.com)")
    parser.add_argument("--website-type", type=str, help="Website type override")
    parser.add_argument("--output-dir", type=str, help="Output directory")
    parser.add_argument(
        "--platform",
        choices=["mobile", "desktop", "web"],
        help="Optional environment platform. Missing preserves the legacy mobile path; web aliases desktop.",
    )
    parser.add_argument(
        "--viewport",
        type=str,
        help="Optional WIDTHxHEIGHT viewport override (desktop only; default 1920x1080).",
    )
    parser.add_argument("--site-model", type=str, help="Path to pre-built AppSpec (directory or legacy JSON file)")
    parser.add_argument("--stop-after",
                        choices=["global_reasoning", "instance_splitting", "tasks", "site_model"],
                        help="Run pipeline up to (and including) this stage, then stop. "
                             "instance_splitting = after materialized page assignment; "
                             "site_model = after semantic design + interfaces (complete phase_consolidation/site_model/)")
    parser.add_argument("--task-count-range", type=str,
                        help="Override task count range (e.g. '10-20'), overrides config task_count_range")
    parser.add_argument("--task-min-steps", type=int,
                        help="Minimum steps per task (default 6)")
    parser.add_argument("--task-max-steps", type=int,
                        help="Maximum steps per task (default 10)")
    hybrid_group = parser.add_mutually_exclusive_group()
    hybrid_group.add_argument("--hybrid-site-model", dest="hybrid_site_model", action="store_true",
                              help="Enable hybrid copy-real + synthetic downstream pipeline")
    hybrid_group.add_argument("--no-hybrid-site-model", dest="hybrid_site_model", action="store_false",
                              help="Disable hybrid mode and use legacy downstream pipeline behavior")
    parser.set_defaults(hybrid_site_model=None)
    site_model_parallel_group = parser.add_mutually_exclusive_group()
    site_model_parallel_group.add_argument("--site-model-parallel-enabled", dest="site_model_parallel_enabled", action="store_true",
                                           help="Enable parallel site model builder pipeline")
    site_model_parallel_group.add_argument("--site-model-parallel-disabled", dest="site_model_parallel_enabled", action="store_false",
                                           help="Disable parallel site model builder pipeline")
    parser.set_defaults(site_model_parallel_enabled=None)
    parser.add_argument("--site-model-consistency-mode", type=str, choices=["deterministic", "throughput"],
                        help="Reducer consistency mode for site model building")
    site_model_refine_group = parser.add_mutually_exclusive_group()
    site_model_refine_group.add_argument("--site-model-frame-quality-refinement-enabled", dest="site_model_frame_quality_refinement_enabled", action="store_true",
                                         help="Enable two-pass frame analysis: first pass lightweight, second pass detailed for complex frames")
    site_model_refine_group.add_argument("--site-model-frame-quality-refinement-disabled", dest="site_model_frame_quality_refinement_enabled", action="store_false",
                                         help="Disable two-pass frame analysis (single-pass only)")
    parser.set_defaults(site_model_frame_quality_refinement_enabled=None)
    parser.add_argument("--site-model-random-seed", type=int, help="Random seed for page-level base observation selection")
    parser.add_argument("--site-model-unknown-url-decision-threshold", type=float,
                        help="Confidence threshold for unknown-url page merge decision")
    site_model_url_signal_group = parser.add_mutually_exclusive_group()
    site_model_url_signal_group.add_argument(
        "--site-model-use-url-signals",
        dest="site_model_use_url_signals",
        action="store_true",
        help="Enable URL signals as auxiliary evidence during site model extraction"
    )
    site_model_url_signal_group.add_argument(
        "--site-model-disable-url-signals",
        dest="site_model_use_url_signals",
        action="store_false",
        help="Disable URL signals (default)"
    )
    parser.set_defaults(site_model_use_url_signals=None)
    site_model_global_reasoning_group = parser.add_mutually_exclusive_group()
    site_model_global_reasoning_group.add_argument(
        "--site-model-global-reasoning-enabled",
        dest="site_model_global_reasoning_enabled",
        action="store_true",
        help="Enable global step-labeling and canonical route reasoning"
    )
    site_model_global_reasoning_group.add_argument(
        "--site-model-global-reasoning-disabled",
        dest="site_model_global_reasoning_enabled",
        action="store_false",
        help="Disable global reasoning and fallback to deterministic clustering"
    )
    parser.set_defaults(site_model_global_reasoning_enabled=None)
    parser.add_argument(
        "--site-model-global-reasoning-overflow-strategy",
        type=str,
        choices=["chunk_reduce", "fail_fast"],
        help="Overflow strategy when global reasoning input exceeds token budget"
    )
    parser.add_argument(
        "--site-model-global-reasoning-token-budget",
        type=int,
        help="Approximate input token budget for global reasoning"
    )
    parser.add_argument(
        "--site-model-global-reasoning-chunk-size",
        type=int,
        help="Trace count per compact global reasoning chunk"
    )
    parser.add_argument(
        "--site-model-global-reasoning-merge-batch-size",
        type=int,
        help="Max partial count per compact hierarchical merge call"
    )
    parser.add_argument(
        "--site-model-global-reasoning-compact-chunk-retry-attempts",
        type=int,
        help="Extra strict-schema retry waves for failed compact global reasoning chunks"
    )
    parser.add_argument(
        "--site-model-page-name-soft-limit",
        type=int,
        help="Soft limit for page_name display text; full text is kept in page_name_full"
    )
    parser.add_argument(
        "--site-model-global-reasoning-logic-flow",
        choices=[
            "compact_transition_resolver",
            "compact_authority_namespace_budget",
            "compact_phase1_semantic_balanced",
        ],
        help="Select the complete global reasoning flow. This is the only GR flow selector."
    )
    parser.add_argument(
        "--site-model-global-reasoning-input-path",
        type=str,
        help="Explicit global reasoning JSON to replay during AppSpec construction."
    )
    parser.add_argument(
        "--site-model-schema-boundary-slice-token-budget",
        type=int,
        help="Approximate token budget per boundary planner slice."
    )
    parser.add_argument(
        "--site-model-schema-boundary-slice-candidate-budget",
        type=int,
        help="Maximum decision candidates per boundary planner slice before recursive splitting."
    )
    parser.add_argument(
        "--site-model-selected-instance-compile-manifest",
        type=str,
        default="",
        help=(
            "Manifest-driven exact instance subset for content compilation; "
            "the manifest binds the canonical plan and hard-fail denominator."
        ),
    )
    skip_full_extraction_group = parser.add_mutually_exclusive_group()
    skip_full_extraction_group.add_argument(
        "--site-model-skip-full-extraction-enabled",
        dest="site_model_skip_full_extraction_enabled",
        action="store_true",
        help="Skip full SM-P4 content extraction and consolidate from lightweight state descriptions"
    )
    skip_full_extraction_group.add_argument(
        "--site-model-skip-full-extraction-disabled",
        dest="site_model_skip_full_extraction_enabled",
        action="store_false",
        help="Run the normal full SM-P4 content extraction pipeline"
    )
    skip_full_extraction_group.add_argument(
        "--site-model-state-only-routing-enabled",
        dest="site_model_skip_full_extraction_enabled",
        action="store_true",
        help=argparse.SUPPRESS
    )
    skip_full_extraction_group.add_argument(
        "--site-model-state-only-routing-disabled",
        dest="site_model_skip_full_extraction_enabled",
        action="store_false",
        help=argparse.SUPPRESS
    )
    parser.set_defaults(site_model_skip_full_extraction_enabled=None)
    parser.add_argument("--vision-max-width", type=int, help="Max width after downsampling (e.g., 1280)")
    parser.add_argument("--vision-max-height", type=int, help="Max height after downsampling (e.g., 720)")
    parser.add_argument("--vision-jpeg-quality", type=int, help="JPEG quality for downsampled images (1-95)")
    parser.add_argument("--filtered-resources", type=str, help="Path to filtered_resources.json")
    parser.add_argument(
        "--pagegen-logic-flow",
        choices=["standard", "full", "l0_fidelity"],
        help="Screenshot selection strategy: 'standard' uses unique-labeling screenshots only; 'full' uses all screenshots; 'l0_fidelity' uses unique + task-related screenshots. Cascade execution is automatic based on budget thresholds."
    )
    parser.add_argument("--pagegen-cascade-round1-image-limit", type=int, help="Maximum screenshot count for the first cascade round.")
    parser.add_argument("--pagegen-cascade-max-images-per-round", type=int, help="Maximum screenshot count for each cascade round.")
    pagegen_patch_group = parser.add_mutually_exclusive_group()
    pagegen_patch_group.add_argument(
        "--pagegen-cascade-patching-enabled",
        dest="pagegen_cascade_patching_enabled",
        action="store_true",
        help="Enable cascade patch rounds after round1 HTML generation."
    )
    pagegen_patch_group.add_argument(
        "--pagegen-cascade-patching-disabled",
        dest="pagegen_cascade_patching_enabled",
        action="store_false",
        help="Disable cascade patch rounds and keep only round1 HTML generation."
    )
    parser.set_defaults(pagegen_cascade_patching_enabled=None)
    pagegen_unique_group = parser.add_mutually_exclusive_group()
    pagegen_unique_group.add_argument(
        "--pagegen-cascade-use-unique-steps-enabled",
        dest="pagegen_cascade_use_unique_steps_enabled",
        action="store_true",
        help="Prioritize unique screenshot steps in cascade round1 batching."
    )
    pagegen_unique_group.add_argument(
        "--pagegen-cascade-use-unique-steps-disabled",
        dest="pagegen_cascade_use_unique_steps_enabled",
        action="store_false",
        help="Disable unique-step prioritization in cascade batching."
    )
    parser.set_defaults(pagegen_cascade_use_unique_steps_enabled=None)
    parser.add_argument("--pagegen-cascade-patch-max-tokens", type=int, help="Maximum completion tokens for each cascade patch round.")
    parser.add_argument(
        "--pagegen-cascade-prompt-mode",
        choices=["current_pipeline"],
        help="Cascade round1 prompt source. Only current_pipeline is supported in the integrated flow."
    )
    args = parser.parse_args()


    if not args.stop_after:
        for old_flag, stage in [
            ("site_model_only", "site_model"),
            ("tasks_only", "tasks"),
            ("interfaces_only", "interfaces"),
        ]:
            if getattr(args, old_flag, False):
                args.stop_after = stage
                break

    generator = Trace2WorldTraceGenerator(
        output_dir=args.output_dir,
        config_path=args.config,
        stop_after=args.stop_after,
        hybrid_site_model=args.hybrid_site_model,
        site_model_parallel_enabled=args.site_model_parallel_enabled,
        site_model_consistency_mode=args.site_model_consistency_mode,
        site_model_frame_quality_refinement_enabled=args.site_model_frame_quality_refinement_enabled,
        site_model_random_seed=args.site_model_random_seed,
        site_model_unknown_url_decision_threshold=args.site_model_unknown_url_decision_threshold,
        site_model_use_url_signals=args.site_model_use_url_signals,
        site_model_global_reasoning_enabled=args.site_model_global_reasoning_enabled,
        site_model_global_reasoning_overflow_strategy=args.site_model_global_reasoning_overflow_strategy,
        site_model_global_reasoning_token_budget=args.site_model_global_reasoning_token_budget,
        site_model_global_reasoning_chunk_size=args.site_model_global_reasoning_chunk_size,
        site_model_global_reasoning_merge_batch_size=args.site_model_global_reasoning_merge_batch_size,
        site_model_global_reasoning_compact_chunk_retry_attempts=(
            args.site_model_global_reasoning_compact_chunk_retry_attempts
        ),
        site_model_page_name_soft_limit=args.site_model_page_name_soft_limit,
        site_model_global_reasoning_logic_flow=args.site_model_global_reasoning_logic_flow,
        site_model_global_reasoning_input_path=args.site_model_global_reasoning_input_path,
        site_model_schema_boundary_slice_token_budget=args.site_model_schema_boundary_slice_token_budget,
        site_model_schema_boundary_slice_candidate_budget=args.site_model_schema_boundary_slice_candidate_budget,
        site_model_skip_full_extraction_enabled=args.site_model_skip_full_extraction_enabled,
        vision_max_width=args.vision_max_width,
        vision_max_height=args.vision_max_height,
        vision_jpeg_quality=args.vision_jpeg_quality,
        filtered_resources_path=args.filtered_resources,
        pagegen_logic_flow=args.pagegen_logic_flow,
        pagegen_cascade_round1_image_limit=args.pagegen_cascade_round1_image_limit,
        pagegen_cascade_max_images_per_round=args.pagegen_cascade_max_images_per_round,
        pagegen_cascade_patching_enabled=args.pagegen_cascade_patching_enabled,
        pagegen_cascade_use_unique_steps_enabled=args.pagegen_cascade_use_unique_steps_enabled,
        pagegen_cascade_patch_max_tokens=args.pagegen_cascade_patch_max_tokens,
        pagegen_cascade_prompt_mode=args.pagegen_cascade_prompt_mode,
        platform=args.platform,
        viewport=args.viewport,
    )

    if args.website_type:
        generator.config_manager.set("website_type", args.website_type)
    if args.site_model_selected_instance_compile_manifest:
        selected_manifest = os.path.abspath(
            args.site_model_selected_instance_compile_manifest
        )
        generator.config_manager.set(
            "site_model_selected_instance_compile_manifest_path",
            selected_manifest,
        )
        generator.site_model_builder.selected_instance_compile_manifest_path = (
            selected_manifest
        )
    if args.task_count_range:
        generator.config_manager.set("task_count_range", args.task_count_range)
    if args.task_min_steps is not None:
        generator.task_generator.min_steps = args.task_min_steps
    if args.task_max_steps is not None:
        generator.task_generator.max_steps = args.task_max_steps


    trace_dirs = args.trace_dirs
    if not trace_dirs and args.trace_base_dir:
        trace_dirs = generator.trace_loader.discover_trace_dirs(args.trace_base_dir)

    result = generator.generate(
        trace_dirs=trace_dirs,
        task_metadata_path=args.task_metadata,
        website_filter=args.website_filter,
        site_model_path=args.site_model,
        stop_after=args.stop_after
    )

    if result.success:
        print("\nConstruction successful.")
    else:
        print(f"\nGeneration failed: {result.test_results.get('error', 'Unknown')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
