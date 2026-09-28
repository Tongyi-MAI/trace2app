
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from trace2world_pipeline.core.step_keys import canonical_step_key


_DISALLOWED_KEYS = {
    "pagegen_scope_debug",
    "raw_extracted_content",
    "raw_extracted_items",
    "raw_page_execution_contract",
    "route_input_dynamic_contracts",
    "dynamic_surface_contract",
    "stateful_continuation_contexts",
    "runtime_contract",
    "resource_cache_key",
    "dedup_resource_cache_key",
}

_RENDER_VIEW_LEGACY_PARALLEL_KEYS = {
    "page_environment_spec",
    "spec_execution_view",
    "required_interaction_surfaces",
    "pagegen_dom_hard_gate_checklist",
    "runtime_api_surface",
    "route_action_slots",
    "action_slot_route_contract",
    "route_affordances_final",
}


def validate_pagegen_prompt_boundary(payload: Dict[str, Any]) -> Dict[str, Any]:

    violations: List[Dict[str, Any]] = []

    def base_filename(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return text.rsplit("/", 1)[-1]

    def add_authority(
        pairs: Set[Tuple[str, str]],
        targets: Set[str],
        source: Any,
        target: Any,
    ) -> None:
        source_filename = base_filename(source)
        target_filename = base_filename(target)
        if not target_filename.endswith(".html"):
            return
        targets.add(target_filename)
        if source_filename:
            pairs.add((source_filename, target_filename))

    def expected_filename_from_assertions(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        return str(value.get("expected_filename") or value.get("target_filename") or "").strip()

    def collect_page_environment_authority(
        spec: Any,
        pairs: Set[Tuple[str, str]],
        targets: Set[str],
        default_source: str = "",
    ) -> None:
        if not isinstance(spec, dict):
            return
        source = spec.get("filename") or default_source
        for key in ("transitions", "routes", "route_affordances"):
            items = spec.get(key, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    add_authority(
                        pairs,
                        targets,
                        item.get("source_filename") or source,
                        item.get("target_filename")
                        or item.get("to_filename")
                        or item.get("expected_filename"),
                    )
        components = spec.get("components", [])
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, dict):
                    continue
                if str(component.get("component_role") or "") != "route_navigation_affordance":
                    continue
                add_authority(
                    pairs,
                    targets,
                    component.get("source_filename") or source,
                    component.get("target_filename"),
                )

        by_filename = spec.get("page_environment_specs_by_filename")
        if isinstance(by_filename, dict):
            for filename, page_spec in by_filename.items():
                collect_page_environment_authority(page_spec, pairs, targets, str(filename))

    def collect_route_authority() -> Tuple[Set[Tuple[str, str]], Set[str]]:
        pairs: Set[Tuple[str, str]] = set()
        targets: Set[str] = set()

        render_view = payload.get("pagegen_render_view") if isinstance(payload, dict) else {}
        if isinstance(render_view, dict):
            route_slots = render_view.get("route_slots")
            if isinstance(route_slots, list):
                for slot in route_slots:
                    if isinstance(slot, dict):
                        add_authority(
                            pairs,
                            targets,
                            slot.get("source_filename") or render_view.get("filename"),
                            slot.get("target_filename"),
                        )
            action_obligations = render_view.get("action_obligations")
            if isinstance(action_obligations, list):
                for action in action_obligations:
                    if isinstance(action, dict):
                        add_authority(
                            pairs,
                            targets,
                            action.get("source_filename") or render_view.get("filename"),
                            action.get("target_filename"),
                        )

        route_slots = payload.get("route_action_slots") if isinstance(payload, dict) else []
        if isinstance(route_slots, list):
            for slot in route_slots:
                if isinstance(slot, dict):
                    add_authority(
                        pairs,
                        targets,
                        slot.get("source_filename"),
                        slot.get("target_filename"),
                    )

        route_contract = payload.get("action_slot_route_contract") if isinstance(payload, dict) else {}
        if isinstance(route_contract, dict):
            for key in ("route_action_slots", "routes", "navigation_actions", "actions"):
                items = route_contract.get(key, [])
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        add_authority(
                            pairs,
                            targets,
                            item.get("source_filename"),
                            item.get("target_filename")
                            or item.get("to_filename")
                            or item.get("expected_filename"),
                        )

        route_affordances = payload.get("route_affordances_final") if isinstance(payload, dict) else {}
        if isinstance(route_affordances, dict):
            routes = route_affordances.get("routes", [])
            if isinstance(routes, list):
                for route in routes:
                    if isinstance(route, dict):
                        add_authority(
                            pairs,
                            targets,
                            route.get("source_filename"),
                            route.get("target_filename"),
                        )
            by_source = route_affordances.get("routes_by_source_filename", {})
            if isinstance(by_source, dict):
                for source_filename, routes_for_source in by_source.items():
                    if not isinstance(routes_for_source, list):
                        continue
                    for route in routes_for_source:
                        if isinstance(route, dict):
                            add_authority(
                                pairs,
                                targets,
                                route.get("source_filename") or source_filename,
                                route.get("target_filename"),
                            )

        collect_page_environment_authority(payload.get("page_environment_spec"), pairs, targets)
        spec_execution_view = payload.get("spec_execution_view") if isinstance(payload, dict) else {}
        if isinstance(spec_execution_view, dict):
            actions = spec_execution_view.get("actions", [])
            if isinstance(actions, list):
                for action in actions:
                    if isinstance(action, dict):
                        add_authority(
                            pairs,
                            targets,
                            action.get("source_filename") or spec_execution_view.get("filename"),
                            action.get("target_filename")
                            or expected_filename_from_assertions(action.get("assertions")),
                        )
        return pairs, targets

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text in _DISALLOWED_KEYS:
                    violations.append(
                        {
                            "code": "debug_semantic_input_leak",
                            "severity": "blocking",
                            "path": child_path,
                            "key": key_text,
                        }
                    )
                visit(child, child_path)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                visit(item, f"{path}[{idx}]")

    visit(payload, "")

    render_view = payload.get("pagegen_render_view") if isinstance(payload, dict) else {}
    if isinstance(render_view, dict) and render_view:
        for key in sorted(_RENDER_VIEW_LEGACY_PARALLEL_KEYS):
            if key in payload:
                violations.append(
                    {
                        "code": "pagegen_render_view.legacy_parallel_authority_key",
                        "severity": "blocking",
                        "path": key,
                        "key": key,
                        "expected": "folded into pagegen_render_view",
                    }
                )
        conflict_report = {}
        validation_contract = render_view.get("validation_contract")
        if isinstance(validation_contract, dict):
            conflict_report = validation_contract.get("conflict_report")
        if isinstance(conflict_report, dict) and int(conflict_report.get("conflict_count") or 0) > 0:
            violations.append(
                {
                    "code": "pagegen_render_view.conflict_report_blocked",
                    "severity": "blocking",
                    "path": "pagegen_render_view.validation_contract.conflict_report",
                    "key": "conflict_count",
                    "actual": conflict_report.get("conflict_count"),
                }
            )
        fold_coverage = {}
        if isinstance(validation_contract, dict):
            fold_coverage = validation_contract.get("fold_coverage")
        if isinstance(fold_coverage, dict):
            missing_required = fold_coverage.get("missing_required_families")
            missing_count = int(fold_coverage.get("missing_required_count") or 0)
            if (
                str(fold_coverage.get("status") or "").strip() not in {"", "pass"}
                or missing_count > 0
                or (isinstance(missing_required, list) and missing_required)
            ):
                violations.append(
                    {
                        "code": "pagegen_render_view.fold_coverage_blocked",
                        "severity": "blocking",
                        "path": "pagegen_render_view.validation_contract.fold_coverage",
                        "key": "fold_coverage",
                        "actual": fold_coverage.get("status"),
                        "missing_required_count": missing_count,
                    }
                )

    lifecycle_inputs = (
        payload.get("screenshot_lifecycle_inputs")
        if isinstance(payload, dict)
        else {}
    )
    if isinstance(lifecycle_inputs, dict):
        non_initial_roles_by_step: Dict[str, str] = {}
        authority = lifecycle_inputs.get("initial_visual_authority")
        if isinstance(authority, dict) and str(authority.get("status") or "") == "blocked":
            violations.append(
                {
                    "code": "initial_visual_authority.blocked",
                    "severity": "blocking",
                    "path": "screenshot_lifecycle_inputs.initial_visual_authority.status",
                    "key": "status",
                    "expected": "ok or spec_only_initial",
                    "actual": "blocked",
                }
            )
        unknown = lifecycle_inputs.get("unknown_render_role_screenshots")
        if isinstance(unknown, list) and unknown:
            violations.append(
                {
                    "code": "initial_visual_authority.unknown_render_role",
                    "severity": "blocking",
                    "path": "screenshot_lifecycle_inputs.unknown_render_role_screenshots",
                    "key": "unknown_render_role_screenshots",
                    "count": len(unknown),
                }
            )
        initial_items = lifecycle_inputs.get("initial_layout_screenshots")
        if isinstance(initial_items, list):
            for idx, item in enumerate(initial_items):
                if not isinstance(item, dict):
                    continue
                if item.get("render_role") != "canonical_layout" or item.get("initial_dom_eligible") is not True:
                    violations.append(
                        {
                            "code": "initial_visual_authority.invalid_initial_layout_screenshot",
                            "severity": "blocking",
                            "path": f"screenshot_lifecycle_inputs.initial_layout_screenshots[{idx}]",
                            "key": "initial_layout_screenshots",
                        }
                    )
        for field in ("state_transition_screenshots", "runtime_status_screenshots"):
            items = lifecycle_inputs.get(field)
            if not isinstance(items, list):
                continue
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                step_key = canonical_step_key(item.get("step_key") or "")
                role = str(item.get("render_role") or "").strip()
                if step_key and role in {
                    "interaction_state_change",
                    "post_interaction_overlay",
                }:
                    non_initial_roles_by_step[step_key] = role or field
                if item.get("initial_dom_eligible") is not False:
                    violations.append(
                        {
                            "code": "initial_visual_authority.noncanonical_initial_eligible",
                            "severity": "blocking",
                            "path": f"screenshot_lifecycle_inputs.{field}[{idx}].initial_dom_eligible",
                            "key": "initial_dom_eligible",
                        }
                    )
        required_surfaces = payload.get("required_interaction_surfaces")
        if isinstance(required_surfaces, list):
            for idx, surface in enumerate(required_surfaces):
                if not isinstance(surface, dict):
                    continue
                if surface.get("initial_dom_eligible") is not True:
                    continue
                if str(surface.get("initial_visibility_policy") or "") != "visible_initial":
                    continue
                evidence_steps = surface.get("evidence_steps")
                if not isinstance(evidence_steps, list):
                    continue
                for evidence in evidence_steps:
                    if not isinstance(evidence, dict):
                        continue
                    step_key = canonical_step_key(evidence.get("step_key") or "")
                    if not step_key or step_key not in non_initial_roles_by_step:
                        continue
                    violations.append(
                        {
                            "code": "initial_visual_authority.surface_lifecycle_conflict",
                            "severity": "blocking",
                            "path": f"required_interaction_surfaces[{idx}]",
                            "key": "initial_visibility_policy",
                            "step_key": step_key,
                            "render_role": non_initial_roles_by_step[step_key],
                            "expected": "hidden_until_transition or runtime-only surface",
                            "actual": "visible_initial",
                        }
                    )
                    break

    prompt_annotations = payload.get("prompt_screenshot_annotations") if isinstance(payload, dict) else []
    if isinstance(prompt_annotations, list):
        for idx, ann in enumerate(prompt_annotations):
            if not isinstance(ann, dict):
                continue
            ann_step_key = canonical_step_key(ann.get("step_key") or "")
            ann_path = f"prompt_screenshot_annotations[{idx}]"
            prev = str(ann.get("previous_step") or "").strip()
            if prev and ann_step_key:
                prev_trace = prev.split(":")[0] if ":" in prev else ""
                cur_trace = ann_step_key.split(":")[0] if ":" in ann_step_key else ""
                if prev_trace and cur_trace and prev_trace != cur_trace:
                    violations.append(
                        {
                            "code": "screenshot_annotation.cross_trace_previous_step",
                            "severity": "blocking",
                            "path": f"{ann_path}.previous_step",
                            "key": "previous_step",
                            "previous_step": prev,
                            "step_key": ann_step_key,
                        }
                    )
            if ann.get("source_step_action") and ann.get("transition_trigger_action"):
                violations.append(
                    {
                        "code": "screenshot_annotation.conflicting_action_semantics",
                        "severity": "blocking",
                        "path": ann_path,
                        "key": "source_step_action+transition_trigger_action",
                    }
                )
    if isinstance(lifecycle_inputs, dict):
        state_items = lifecycle_inputs.get("state_transition_screenshots")
        if isinstance(state_items, list):
            for idx, item in enumerate(state_items):
                if not isinstance(item, dict):
                    continue
                sk = canonical_step_key(item.get("step_key") or "")
                if not sk:
                    continue
                iak_map = payload.get("initial_authority_kind_by_step") if isinstance(payload, dict) else {}
                if isinstance(iak_map, dict) and iak_map.get(sk) == "l0_source":
                    violations.append(
                        {
                            "code": "screenshot_lifecycle.l0_source_in_state_transition",
                            "severity": "blocking",
                            "path": f"screenshot_lifecycle_inputs.state_transition_screenshots[{idx}]",
                            "key": "step_key",
                            "step_key": sk,
                            "expected": "initial_layout_screenshots",
                        }
                    )

    route_slots = payload.get("route_action_slots") if isinstance(payload, dict) else []
    if isinstance(route_slots, list):
        for idx, slot in enumerate(route_slots):
            if not isinstance(slot, dict):
                continue
            path = f"route_action_slots[{idx}]"
            executor = str(slot.get("executor") or "")
            component_id = str(slot.get("component_id") or "")
            target_filename = str(slot.get("target_filename") or "")
            binding_source = str(slot.get("binding_source") or "")
            shell_family_id = str(slot.get("shell_family_id") or "")
            shell_slot_key = str(slot.get("shell_slot_key") or "")
            if executor and executor != "GhostwebRouteExecution.navigate":
                violations.append(
                    {
                        "code": "route_action_slot.invalid_executor",
                        "severity": "blocking",
                        "path": f"{path}.executor",
                        "key": "executor",
                        "expected": "GhostwebRouteExecution.navigate",
                        "actual": executor,
                    }
                )
            if not component_id:
                violations.append(
                    {
                        "code": "route_action_slot.missing_component_id",
                        "severity": "blocking",
                        "path": f"{path}.component_id",
                        "key": "component_id",
                    }
                )
            if not target_filename:
                violations.append(
                    {
                        "code": "route_action_slot.missing_target_filename",
                        "severity": "blocking",
                        "path": f"{path}.target_filename",
                        "key": "target_filename",
                    }
                )
            if binding_source == "shell_interaction_broadcast" or shell_family_id:
                if not shell_slot_key:
                    violations.append(
                        {
                            "code": "route_action_slot.missing_shell_slot_key",
                            "severity": "blocking",
                            "path": f"{path}.shell_slot_key",
                            "key": "shell_slot_key",
                            "expected": "stable shell slot identity for broadcast route DOM binding",
                        }
                    )
                if not shell_family_id:
                    violations.append(
                        {
                            "code": "route_action_slot.missing_shell_family_id",
                            "severity": "blocking",
                            "path": f"{path}.shell_family_id",
                            "key": "shell_family_id",
                            "expected": "shell family identity for broadcast route DOM binding",
                        }
                    )
    generated_targets = payload.get("generated_navigation_targets") if isinstance(payload, dict) else []
    if isinstance(generated_targets, list) and generated_targets:
        authorized_pairs, authorized_targets = collect_route_authority()
        for idx, item in enumerate(generated_targets):
            if not isinstance(item, dict):
                continue
            path = f"generated_navigation_targets[{idx}]"
            target_filename = base_filename(item.get("target_filename") or item.get("href"))
            if not target_filename.endswith(".html"):
                continue
            source_filename = base_filename(item.get("source_filename"))
            if target_filename == source_filename:
                continue
            authorized = (
                (
                    (source_filename, target_filename) in authorized_pairs
                    or target_filename in authorized_targets
                )
                if source_filename
                else target_filename in authorized_targets
            )
            if not authorized:
                violations.append(
                    {
                        "code": "navigation_target.missing_route_authority",
                        "severity": "blocking",
                        "path": f"{path}.target_filename",
                        "key": "target_filename",
                        "target_filename": target_filename,
                        "source_filename": source_filename,
                        "label": str(item.get("label") or ""),
                        "expected": (
                            "route_action_slots, action_slot_route_contract, "
                            "route_affordances_final, or page_environment_spec route authority"
                        ),
                    }
                )
    stc_items = payload.get("step_transition_contracts") if isinstance(payload, dict) else None
    if isinstance(stc_items, list) and stc_items:
        submit_targets = set()
        submit_contracts = []
        for item in stc_items:
            if not isinstance(item, dict):
                continue
            sem = str(item.get("action_semantic", "")).strip()
            if sem in {"submit", "query_commit"}:
                target = str(item.get("expected_target_filename", "")).strip()
                if target:
                    submit_targets.add(target)
                    submit_contracts.append(item)
        if len(submit_targets) > 1:
            for item in submit_contracts:
                state_read = item.get("state_read", {})
                postcondition = item.get("postcondition", {})
                has_guard = bool(
                    (isinstance(state_read, dict) and state_read.get("kind"))
                    or (isinstance(postcondition, dict) and postcondition.get("kind") in {"state_driven_navigate", "committed_query_navigate"})
                )
                if not has_guard:
                    violations.append({
                        "code": "submit_control.missing_state_to_target_guard",
                        "severity": "blocking",
                        "path": "step_transition_contracts",
                        "key": "state_read",
                        "contract_id": str(item.get("contract_id", "")),
                        "expected_target": str(item.get("expected_target_filename", "")),
                        "label": (
                            "multi-target submit control must have state_read or "
                            "state_driven_navigate postcondition to resolve target from committed state"
                        ),
                    })
    prompt_policy = payload.get("environment_prompt_policy") if isinstance(payload, dict) else None
    if isinstance(prompt_policy, dict) and prompt_policy:
        render_context_ref = payload.get("render_context_ref")
        if not isinstance(render_context_ref, dict) or not render_context_ref:
            violations.append({
                "code": "environment_prompt_policy.missing_render_context_ref",
                "severity": "blocking",
                "path": "render_context_ref",
                "key": "render_context_ref",
            })
        elif (
            prompt_policy.get("environment_profile_hash")
            != render_context_ref.get("profile_hash")
        ):
            violations.append({
                "code": "environment_prompt_policy.profile_hash_mismatch",
                "severity": "blocking",
                "path": "environment_prompt_policy.environment_profile_hash",
                "key": "environment_profile_hash",
                "prompt_hash": prompt_policy.get("environment_profile_hash"),
                "render_context_hash": render_context_ref.get("profile_hash"),
            })
    return {
        "format": "pagegen_prompt_boundary_validation",
        "schema_revision": 1,
        "page_count": 1 if payload else 0,
        "blocking_error_count": len(violations),
        "violations": violations,
    }
