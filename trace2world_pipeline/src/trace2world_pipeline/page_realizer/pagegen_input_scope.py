
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image

from trace2world_pipeline.app_spec.evidence_resolver import (
    step_key_from_screenshot_ref as evidence_step_key_from_screenshot_ref,
)
from trace2world_pipeline.app_spec.viewport_defaults import MOBILE_VIEWPORT_HEIGHT, MOBILE_VIEWPORT_WIDTH


def _dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def step_key_from_screenshot_ref(ref: str) -> Optional[str]:
    evidence_step_key = evidence_step_key_from_screenshot_ref(ref)
    if evidence_step_key:
        return evidence_step_key
    ref_norm = str(ref or "").replace("\\", "/")
    match = re.search(
        r"/([^/]+)/screenshots/[^/]+-\d+-(\d+)\.(?:jpg|jpeg|png|webp)$",
        ref_norm,
        re.IGNORECASE,
    )
    if match:


        return f"{match.group(1)}:step{max(int(match.group(2)) - 1, 0)}"
    match = re.search(r"/([^/]+)/screenshots/action_(\d+)\.(?:jpg|jpeg|png|webp)$", ref_norm, re.IGNORECASE)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    match = re.search(r"/([^/]+)/step[_-]?(\d+)[^/]*\.(?:jpg|jpeg|png|webp)$", ref_norm, re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1)}:step{int(match.group(2))}"


def normalize_step_key(value: str) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(.+):step(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    match = re.match(r"^(.+):(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    return text


def _normalize_step_keyed_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for key, item in value.items():
        normalized[normalize_step_key(str(key))] = item
    return normalized


def _render_roles_from_prompt_annotations(value: Any) -> Dict[str, str]:
    if not isinstance(value, list):
        return {}
    roles: Dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        step_key = normalize_step_key(str(item.get("step_key") or ""))
        role = str(item.get("render_role") or "").strip()
        if step_key and role:
            roles[step_key] = role
    return roles


def _render_annotations_from_prompt_annotations(value: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    annotations: Dict[str, Dict[str, str]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        step_key = normalize_step_key(str(item.get("step_key") or ""))
        if not step_key:
            continue
        ann: Dict[str, Any] = {}
        if item.get("trigger_action") not in (None, ""):
            ann["action"] = str(item.get("trigger_action"))
        if item.get("visual_diff") not in (None, ""):
            ann["reason"] = str(item.get("visual_diff"))
        for field in (
            "structural_role",
            "lifecycle_role",
            "initial_dom_eligible",
            "action_host_authority",
            "state_preconditions",
        ):
            if field in item:
                ann[field] = item[field]
        if ann:
            annotations[step_key] = ann
    return annotations


def _render_roles_for_scope(scope: Dict[str, Any]) -> Dict[str, str]:
    roles = _normalize_step_keyed_mapping(scope.get("screenshot_render_roles", {}))
    if roles:
        return roles
    return _render_roles_from_prompt_annotations(scope.get("prompt_screenshot_annotations"))


def _render_annotations_for_scope(scope: Dict[str, Any]) -> Dict[str, Any]:
    annotations = _normalize_step_keyed_mapping(scope.get("screenshot_render_annotations", {}))
    if annotations:
        return annotations
    return _render_annotations_from_prompt_annotations(scope.get("prompt_screenshot_annotations"))


def _render_lifecycle_for_scope(scope: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    lifecycle = _normalize_step_keyed_mapping(scope.get("screenshot_render_lifecycle", {}))
    if lifecycle:
        return {
            key: dict(value)
            for key, value in lifecycle.items()
            if isinstance(value, dict)
        }
    annotations = _render_annotations_for_scope(scope)
    result: Dict[str, Dict[str, Any]] = {}
    for key, ann in annotations.items():
        if not isinstance(ann, dict):
            continue
        entry = {
            field: ann[field]
            for field in (
                "structural_role",
                "lifecycle_role",
                "initial_dom_eligible",
                "action_host_authority",
                "state_preconditions",
            )
            if field in ann
        }
        if entry:
            result[key] = entry
    return result


_RENDER_ROLE_POLICIES: Dict[str, Dict[str, Any]] = {
    "canonical_layout": {
        "initial_dom_eligible": True,
        "initial_visibility_policy": "visible_initial",
        "runtime_visibility_policy": "visible_initial",
        "bucket": "initial_layout_screenshots",
    },
    "interaction_state_change": {
        "initial_dom_eligible": False,
        "initial_visibility_policy": "hidden_until_transition",
        "runtime_visibility_policy": "visible_when_state_active",
        "bucket": "state_transition_screenshots",
    },
    "post_interaction_overlay": {
        "initial_dom_eligible": False,
        "initial_visibility_policy": "hidden_until_transition",
        "runtime_visibility_policy": "visible_when_state_active",
        "bucket": "state_transition_screenshots",
    },
    "runtime_status": {
        "initial_dom_eligible": False,
        "initial_visibility_policy": "runtime_only",
        "runtime_visibility_policy": "visible_when_runtime_event_active",
        "bucket": "runtime_status_screenshots",
    },
    "unknown_render_role": {
        "initial_dom_eligible": False,
        "initial_visibility_policy": "blocked_unknown",
        "runtime_visibility_policy": "blocked_unknown",
        "bucket": "unknown_render_role_screenshots",
    },
}


def _render_role_policy(role: str) -> Dict[str, Any]:
    return _RENDER_ROLE_POLICIES.get(str(role or ""), _RENDER_ROLE_POLICIES["unknown_render_role"])


def _screenshot_lifecycle_bucket(role: str, lifecycle: Dict[str, Any]) -> str:
    lifecycle_role = str(lifecycle.get("lifecycle_role") or "").strip()
    initial = lifecycle.get("initial_dom_eligible")
    if role == "canonical_layout" and initial is True:
        return "initial_layout_screenshots"
    if lifecycle_role == "runtime_status" or role == "runtime_status":
        return "runtime_status_screenshots"
    if lifecycle_role in {
        "runtime_pre_action_state",
        "post_interaction_overlay",
        "interaction_state_change",
    }:
        return "state_transition_screenshots"
    if role in {"interaction_state_change", "post_interaction_overlay"}:
        return "state_transition_screenshots"
    if isinstance(initial, bool):
        if role == "canonical_layout":
            return "initial_layout_screenshots" if initial else "state_transition_screenshots"
        return _render_role_policy(role)["bucket"]
    return _render_role_policy(role)["bucket"]


def _screenshot_lifecycle_entry(ref: str, step_key: str, role: str, lifecycle: Dict[str, Any] | None = None) -> Dict[str, Any]:
    policy = _render_role_policy(role)
    lifecycle = lifecycle or {}
    initial_dom_eligible = lifecycle.get("initial_dom_eligible")
    if not isinstance(initial_dom_eligible, bool):
        initial_dom_eligible = bool(policy["initial_dom_eligible"])
    if role != "canonical_layout":
        initial_dom_eligible = False
    entry = {
        "screenshot_ref": ref,
        "step_key": step_key,
        "render_role": role,
        "initial_dom_eligible": bool(initial_dom_eligible),
    }
    for field in (
        "structural_role",
        "lifecycle_role",
        "action_host_authority",
        "state_preconditions",
    ):
        if field in lifecycle:
            entry[field] = lifecycle[field]
    return entry


def _initial_state_scope_for_lifecycle(role: str, lifecycle: Dict[str, Any], initial_dom_eligible: bool) -> str:
    if role != "canonical_layout" or not initial_dom_eligible:
        return ""
    lifecycle_role = str(lifecycle.get("lifecycle_role") or "").strip()
    if lifecycle_role == "trace_entry_initial":
        return "trace_entry_initial_state"
    if lifecycle_role in {"page_initial", "runtime_page_initial"}:
        return "runtime_page_initial_state"
    state_preconditions = lifecycle.get("state_preconditions")
    if isinstance(state_preconditions, list) and state_preconditions:
        return "trace_entry_initial_state"
    return "runtime_page_initial_state"


def _runtime_page_initial_visual_authority(
    *,
    runtime_count: int,
    trace_entry_count: int,
    selected_count: int,
    initial_render_mode: str,
) -> Dict[str, Any]:
    if runtime_count > 0:
        status = "ok"
    elif initial_render_mode == "spec_only_initial":
        status = "spec_only_initial"
    else:
        status = "blocked"
    return {
        "status": status,
        "runtime_page_initial_count": runtime_count,
        "trace_entry_initial_count": trace_entry_count,
        "selected_screenshot_count": selected_count,
        "initial_render_mode": initial_render_mode,
    }


def _build_screenshot_lifecycle_projection(scope: Dict[str, Any]) -> Dict[str, Any]:
    prompt_refs = list(scope.get("prompt_screenshot_refs") or [])
    render_roles = _render_roles_for_scope(scope)
    lifecycle_by_step = _render_lifecycle_for_scope(scope)
    initial_render_mode = str(scope.get("initial_render_mode") or "screenshot_initial")
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "initial_layout_screenshots": [],
        "runtime_page_initial_state_screenshots": [],
        "trace_entry_initial_state_screenshots": [],
        "state_transition_screenshots": [],
        "runtime_status_screenshots": [],
        "unknown_render_role_screenshots": [],
    }
    blocking_issues: List[Dict[str, Any]] = []

    for ref in prompt_refs:
        sk = step_key_from_screenshot_ref(str(ref)) or ""
        canonical_sk = normalize_step_key(sk) if sk else ""
        role = str(render_roles.get(canonical_sk) or "").strip()
        if role not in _RENDER_ROLE_POLICIES or role == "unknown_render_role":
            role = "unknown_render_role"
            blocking_issues.append(
                {
                    "code": "unknown_render_role",
                    "screenshot_ref": ref,
                    "step_key": canonical_sk,
                }
            )
        lifecycle = lifecycle_by_step.get(canonical_sk, {})
        entry = _screenshot_lifecycle_entry(str(ref), canonical_sk, role, lifecycle)
        buckets[_screenshot_lifecycle_bucket(role, lifecycle)].append(entry)
        initial_scope = _initial_state_scope_for_lifecycle(role, lifecycle, bool(entry.get("initial_dom_eligible")))
        if initial_scope:
            scoped_entry = dict(entry)
            scoped_entry["initial_state_scope"] = initial_scope
            buckets[f"{initial_scope}_screenshots"].append(scoped_entry)

    canonical_count = len(buckets["initial_layout_screenshots"])
    if canonical_count == 0 and prompt_refs and initial_render_mode != "spec_only_initial":
        blocking_issues.append(
            {
                "code": "missing_canonical_layout",
                "filename": str(scope.get("filename") or ""),
                "selected_screenshot_count": len(prompt_refs),
            }
        )
    if initial_render_mode == "spec_only_initial":
        authority_status = "spec_only_initial"
    elif canonical_count > 0:
        authority_status = "ok"
    else:
        authority_status = "blocked"

    result: Dict[str, Any] = dict(buckets)
    result["initial_visual_authority"] = {
        "status": authority_status,
        "canonical_layout_count": canonical_count,
        "initial_render_mode": initial_render_mode,
        "blocking_issue_count": len(blocking_issues),
    }
    result["runtime_page_initial_visual_authority"] = _runtime_page_initial_visual_authority(
        runtime_count=len(buckets["runtime_page_initial_state_screenshots"]),
        trace_entry_count=len(buckets["trace_entry_initial_state_screenshots"]),
        selected_count=len(prompt_refs),
        initial_render_mode=initial_render_mode,
    )
    result["pagegen_scope_blocking_issues"] = blocking_issues
    return result


def _attach_screenshot_lifecycle_projection(scope: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(scope or {})
    updated.update(_build_screenshot_lifecycle_projection(updated))
    return updated


def _build_source_host_visual_authority_screenshots(
    *,
    refs_by_step: Dict[str, str],
    screenshot_lifecycle: Dict[str, Any],
    required_interaction_surfaces: Optional[Sequence[Dict[str, Any]]],
    selected_step_keys: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    if not required_interaction_surfaces:
        return []
    lifecycle_by_step = _normalize_step_keyed_mapping(screenshot_lifecycle or {})
    selected_steps = {
        normalize_step_key(str(step))
        for step in (selected_step_keys or [])
        if str(step or "").strip()
    }
    entries: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for surface in required_interaction_surfaces:
        if not isinstance(surface, dict):
            continue
        if str(surface.get("surface_role") or "").strip() != "source_host":
            continue
        component_id = str(surface.get("required_data_element_id") or surface.get("surface_id") or "").strip()
        if not component_id:
            continue
        surface_initial_visible = (
            surface.get("initial_dom_eligible") is True
            or str(surface.get("initial_visibility_policy") or "").strip() == "visible_initial"
        )
        if not surface_initial_visible:
            continue
        for raw_step_key in _collect_step_keys_from_value(surface):
            step_key = normalize_step_key(str(raw_step_key))
            ref = refs_by_step.get(step_key)
            if not step_key or not ref:
                continue
            lifecycle = lifecycle_by_step.get(step_key, {})
            if not isinstance(lifecycle, dict):
                lifecycle = {}
            if lifecycle.get("action_host_authority") is not True:
                continue
            initial_dom_eligible = lifecycle.get("initial_dom_eligible")
            if (
                isinstance(initial_dom_eligible, bool)
                and initial_dom_eligible
                and step_key in selected_steps
            ):
                continue
            key = (step_key, component_id)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                {
                    "screenshot_ref": ref,
                    "step_key": step_key,
                    "surface_id": str(surface.get("surface_id") or component_id),
                    "required_data_element_id": component_id,
                    "required_display_label": str(surface.get("required_display_label") or ""),
                    "control_label": str(surface.get("control_label") or ""),
                    "authority_scope": "source_host_label_and_affordance_only",
                    "initial_scope": "trace_page_l0_initial",
                    "page_global_initial_dom_eligible": False,
                    "trace_page_l0_initial_eligible": True,
                    "lifecycle_role": str(lifecycle.get("lifecycle_role") or ""),
                    "action_host_authority": True,
                    "state_preconditions": list(lifecycle.get("state_preconditions") or []),
                    "pagegen_instruction": (
                        "Use this screenshot as the trace/page L0 initial state for the "
                        "specified source host label and affordance; do not use it as "
                        "page-global initial DOM authority."
                    ),
                }
            )
    return entries


def selected_step_keys_from_scope(scope: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    if isinstance(scope, dict):
        refs.extend(scope.get("page_screenshot_refs") or [])
        refs.extend(scope.get("prompt_screenshot_refs") or [])
        contract = scope.get("render_scope_contract")
        if isinstance(contract, dict):
            refs.extend(contract.get("selected_screenshot_refs") or [])

    keys = []
    for ref in refs:
        key = step_key_from_screenshot_ref(str(ref))
        if key:
            keys.append(key)
    return _dedupe_keep_order(keys)


def _step_key_from_trace_step(value: Dict[str, Any]) -> Optional[str]:
    trace_id = str(value.get("trace_id", "") or "").strip()
    if not trace_id:
        return None
    step_index = value.get("step_index")
    if step_index is None:
        step_index = value.get("step")
    try:
        return f"{trace_id}:step{int(step_index)}"
    except Exception:
        return None


def _looks_like_step_key(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.match(r"^.+:(?:step)?\d+$", text))


def _collect_step_keys_from_value(value: Any) -> List[str]:
    list_keys = {
        "source_steps",
        "evidence_steps",
        "obs_keys",
        "source_obs_keys",
        "source_step_keys",
        "selected_steps",
    }
    single_keys = {
        "source_step",
        "evidence_step",
        "obs_key",
        "source_obs_key",
        "step_key",
        "step",
    }
    nested_keys = {
        "provenance",
        "evidence",
        "operation_binding",
        "interaction_contract",
        "source",
        "sources",
    }

    collected: List[str] = []
    if isinstance(value, dict):
        direct = _step_key_from_trace_step(value)
        if direct:
            collected.append(direct)
        for key, child in value.items():
            if key in list_keys and isinstance(child, list):
                for item in child:
                    if isinstance(item, dict):
                        collected.extend(_collect_step_keys_from_value(item))
                    elif _looks_like_step_key(str(item)):
                        collected.append(normalize_step_key(str(item)))
            elif key in single_keys:
                if isinstance(child, dict):
                    collected.extend(_collect_step_keys_from_value(child))
                elif _looks_like_step_key(str(child)):
                    collected.append(normalize_step_key(str(child)))
            elif key in nested_keys:
                collected.extend(_collect_step_keys_from_value(child))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                collected.extend(_collect_step_keys_from_value(item))
            elif _looks_like_step_key(str(item)):
                collected.append(normalize_step_key(str(item)))
    elif _looks_like_step_key(str(value)):
        collected.append(normalize_step_key(str(value)))
    return _dedupe_keep_order(collected)


def content_item_step_keys(item: Any) -> List[str]:
    return _collect_step_keys_from_value(item)


def filter_extracted_items_for_steps(
    extracted_items: Dict[str, Any],
    selected_steps: Iterable[str],
    *,
    keep_unproven: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    selected = {normalize_step_key(str(step)) for step in selected_steps if str(step)}
    if not isinstance(extracted_items, dict) or not selected:
        return extracted_items if isinstance(extracted_items, dict) else {}, {
            "selected": 0,
            "omitted": 0,
            "unproven_kept": 0,
            "unproven_omitted": 0,
        }

    scoped: Dict[str, Any] = {}
    stats = {
        "selected": 0,
        "omitted": 0,
        "unproven_kept": 0,
        "unproven_omitted": 0,
    }
    for entity_name, items in extracted_items.items():
        if entity_name == "_metadata":
            scoped[entity_name] = items
            continue
        if not isinstance(items, list):
            scoped[entity_name] = items
            continue
        kept = []
        for item in items:
            item_steps = set(content_item_step_keys(item))
            if item_steps:
                if item_steps & selected:
                    kept.append(item)
                    stats["selected"] += 1
                else:
                    stats["omitted"] += 1
            elif keep_unproven:
                kept.append(item)
                stats["unproven_kept"] += 1
            else:
                stats["unproven_omitted"] += 1
        scoped[entity_name] = kept
    return scoped, stats


def filter_task_continuations_for_steps(
    task_continuations: Sequence[Any],
    selected_steps: Iterable[str],
    *,
    page_id: str = "",
    keep_unproven_for_page: bool = True,
) -> Tuple[List[Any], Dict[str, Any]]:
    selected = {normalize_step_key(str(step)) for step in selected_steps if str(step)}
    stats = {
        "selected": 0,
        "omitted": 0,
        "unproven_kept": 0,
        "unproven_omitted": 0,
    }
    if not isinstance(task_continuations, list) or not selected:
        return list(task_continuations or []), stats

    kept: List[Any] = []
    for item in task_continuations:
        item_steps = set(content_item_step_keys(item))
        if item_steps:
            if item_steps & selected:
                kept.append(item)
                stats["selected"] += 1
            else:
                stats["omitted"] += 1
            continue
        source_page_id = ""
        if isinstance(item, dict):
            source_page_id = str(item.get("source_page_id", "") or "").strip()
        if keep_unproven_for_page and (not page_id or not source_page_id or source_page_id == page_id):
            kept.append(item)
            stats["unproven_kept"] += 1
        else:
            stats["unproven_omitted"] += 1
    return kept, stats


def scope_page_evidence_for_steps(
    page_evidence: Dict[str, Any],
    selected_steps: Iterable[str],
    *,
    keep_unproven_items: bool = True,
    keep_unproven_continuations: bool = True,
) -> Dict[str, Any]:
    if not isinstance(page_evidence, dict):
        return {}
    scoped = dict(page_evidence)
    selected = _dedupe_keep_order(normalize_step_key(str(step)) for step in selected_steps if str(step))
    page_id = str(scoped.get("page_id", "") or "").strip()
    report: Dict[str, Any] = {
        "enabled": True,
        "selected_steps": selected,
    }
    extracted = scoped.get("extracted_items")
    if isinstance(extracted, dict):
        filtered, stats = filter_extracted_items_for_steps(
            extracted,
            selected,
            keep_unproven=keep_unproven_items,
        )
        scoped["extracted_items"] = filtered
        report["extracted_items"] = stats
    continuations = scoped.get("task_continuations")
    if isinstance(continuations, list):
        filtered_continuations, stats = filter_task_continuations_for_steps(
            continuations,
            selected,
            page_id=page_id,
            keep_unproven_for_page=keep_unproven_continuations,
        )
        scoped["task_continuations"] = filtered_continuations
        report["task_continuations"] = stats
    scoped["scoped_content"] = report
    return scoped


def resource_path_keys_from_screenshot_ref(ref: str) -> List[str]:
    step_key = step_key_from_screenshot_ref(ref)
    match = re.match(r"^(.+):step(\d+)$", step_key or "")
    if not match:
        return []
    trace_id = match.group(1)
    try:
        step_index = int(match.group(2))
    except Exception:
        return []
    return [
        f"{trace_id}/resources/step_{step_index:04d}/",
        f"{trace_id}/resources/step{step_index:02d}/",
        f"{trace_id}/resources/step{step_index}/",
    ]


def normalize_page_input_scope(page_input: Dict[str, Any]) -> Dict[str, Any]:
    filename = str(page_input.get("filename", "") or "")
    prompt_refs = _dedupe_keep_order(
        page_input.get("prompt_screenshot_refs")
        or page_input.get("screenshot_refs")
        or []
    )
    page_refs = _dedupe_keep_order(
        page_input.get("page_screenshot_refs")
        or page_input.get("resource_screenshot_refs")
        or prompt_refs
    )
    resource_refs = _dedupe_keep_order(
        page_input.get("resource_screenshot_refs")
        or page_refs
    )
    unique_refs = _dedupe_keep_order(
        page_input.get("unique_prompt_screenshot_refs")
        or page_input.get("unique_screenshot_refs")
        or prompt_refs
    )
    prompt_set = set(prompt_refs)
    return {
        "page_id": page_input.get("page_id", ""),
        "filename": filename,
        "page_name": page_input.get("page_name", ""),
        "page_type": page_input.get("page_type", ""),
        "description": page_input.get("description", ""),
        "initial_render_mode": page_input.get("initial_render_mode", ""),
        "page_screenshot_refs": page_refs,
        "resource_screenshot_refs": resource_refs,
        "prompt_screenshot_refs": prompt_refs,
        "non_prompt_page_screenshot_refs": [ref for ref in page_refs if ref not in prompt_set],
        "unique_prompt_screenshot_refs": unique_refs,
        "screenshot_render_roles": _normalize_step_keyed_mapping(page_input.get("screenshot_render_roles", {})),
        "screenshot_render_annotations": _normalize_step_keyed_mapping(
            page_input.get("screenshot_render_annotations", {})
        ),
        "screenshot_render_lifecycle": _normalize_step_keyed_mapping(
            page_input.get("screenshot_render_lifecycle", {})
        ),
        "initial_authority_kind_by_step": _normalize_step_keyed_mapping(page_input.get("initial_authority_kind_by_step", {})),
    }


def _canonicalize_page_input_with_resolver(
    page_input: Dict[str, Any],
    evidence_resolver: Any,
) -> Dict[str, Any]:
    if not evidence_resolver:
        return page_input
    updated = dict(page_input or {})
    filename = str(updated.get("filename") or "")
    evidence_refs: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    field_roles = (
        ("prompt_screenshot_refs", "primary_prompt"),
        ("screenshot_refs", "primary_prompt"),
        ("unique_prompt_screenshot_refs", "layout_template"),
        ("unique_screenshot_refs", "layout_template"),
        ("page_screenshot_refs", "page_scope"),
        ("resource_screenshot_refs", "resource_scope"),
        ("non_prompt_page_screenshot_refs", "page_scope"),
        ("full_page_screenshot_refs", "audit_full_page"),
        ("excluded_page_screenshot_refs", "audit_excluded"),
    )
    for field, role in field_roles:
        refs = updated.get(field)
        if not isinstance(refs, list) or not refs:
            continue
        canonical_refs, field_evidence, field_issues = evidence_resolver.canonicalize_screenshot_refs(
            refs,
            filename=filename,
            role=role,
        )
        updated[field] = canonical_refs
        evidence_refs.extend(field_evidence)
        issues.extend(field_issues)

    blocking = [issue for issue in issues if issue.get("severity") == "blocking"]
    warnings = [issue for issue in issues if issue.get("severity") == "warning"]
    updated["pagegen_evidence_alignment"] = {
        "format": "pagegen_evidence_alignment",
        "source": "site_model.evidence_resolver",
        "filename": filename,
        "canonicalized": True,
        "evidence_ref_count": len(evidence_refs),
        "blocking_issue_count": len(blocking),
        "warning_issue_count": len(warnings),
        "evidence_refs": evidence_refs,
        "issues": issues,
    }
    return updated


def _collect_task_related_evidence_steps(page_input: Dict[str, Any]) -> List[str]:
    evidence: List[str] = []
    relevance = page_input.get("entity_task_relevance", {})
    if not isinstance(relevance, dict):
        return evidence
    for _entity_name, item in relevance.items():
        if not isinstance(item, dict) or item.get("task_related") is not True:
            continue
        for step in item.get("evidence_steps", []) or []:
            normalized = normalize_step_key(str(step))
            if normalized:
                evidence.append(normalized)
    return _dedupe_keep_order(evidence)


def _build_entity_projection_contract(page_input: Dict[str, Any]) -> Dict[str, Any]:
    relevance = page_input.get("entity_task_relevance", {})
    if not isinstance(relevance, dict):
        return {}
    entities: List[Dict[str, Any]] = []
    for entity_name, item in sorted(relevance.items(), key=lambda kv: str(kv[0])):
        if not isinstance(item, dict) or item.get("task_related") is not True:
            continue
        evidence_steps = _dedupe_keep_order(
            normalize_step_key(str(step))
            for step in (item.get("evidence_steps") or [])
            if str(step or "").strip()
        )
        if not evidence_steps:
            continue
        entities.append(
            {
                "entity_type": str(entity_name),
                "evidence_steps": evidence_steps,
                "task_instruction_refs": _dedupe_keep_order(
                    str(ref)
                    for ref in (item.get("task_instruction_refs") or [])
                    if str(ref or "").strip()
                ),
                "projection_role": "task_critical_content",
            }
        )
    if not entities:
        return {}
    return {
        "format": "entity_projection_contract",
        "source": "content_extraction.entity_task_relevance",
        "projection_policy": "task_related_entities_must_remain_visible_or_runtime_projectable",
        "entities": entities,
    }


def _clean_compact_dict(value: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    return {
        key: value.get(key)
        for key in keys
        if value.get(key) not in (None, "", [], {})
    }


def _append_unique_dict(items: List[Dict[str, Any]], item: Dict[str, Any], key: str) -> None:
    if not item:
        return
    key_value = str(item.get(key) or "").strip()
    if key_value and any(str(existing.get(key) or "").strip() == key_value for existing in items):
        return
    items.append(item)


def _visibility_from_component(component: Dict[str, Any]) -> str:
    for key in ("initial_visibility_policy", "visibility_policy", "runtime_visibility_policy"):
        value = str(component.get(key) or "").strip()
        if value:
            return value
    if component.get("interaction_required") is True:
        return "visible_initial"
    return ""


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _label_authority_for_dom(source: Dict[str, Any], contract: Dict[str, Any] | None = None) -> str:
    contract = contract if isinstance(contract, dict) else {}
    visible_contract = _as_dict(source.get("visible_text_contract")) or _as_dict(contract.get("visible_text_contract"))
    return _simple_text(
        source.get("label_authority")
        or contract.get("label_authority")
        or visible_contract.get("authority")
    )


def _visible_text_contract_for_dom(source: Dict[str, Any], contract: Dict[str, Any] | None = None) -> Dict[str, Any]:
    contract = contract if isinstance(contract, dict) else {}
    for candidate in (source.get("visible_text_contract"), contract.get("visible_text_contract")):
        if isinstance(candidate, dict) and candidate:
            return dict(candidate)
    authority = _label_authority_for_dom(source, contract)
    if authority:
        return {"authority": authority, "exact_text": ""}
    return {}


def _visible_display_label_for_dom(source: Dict[str, Any], contract: Dict[str, Any] | None = None) -> str:
    contract = contract if isinstance(contract, dict) else {}
    visible_contract = _visible_text_contract_for_dom(source, contract)
    authority = _simple_text(visible_contract.get("authority") or _label_authority_for_dom(source, contract))
    exact = _simple_text(visible_contract.get("exact_text"))
    if authority in {"observed_host_text", "visual_slot_observed", "exact_visible_label_required"}:
        return exact or _simple_text(
            source.get("required_display_label")
            or source.get("display_label")
            or contract.get("required_display_label")
            or contract.get("display_label")
            or contract.get("action_text")
        )
    if authority in {"semantic_role_only", "context_only", "unresolved"}:
        return ""
    return _simple_text(
        source.get("required_display_label")
        or source.get("display_label")
        or contract.get("required_display_label")
        or contract.get("display_label")
        or contract.get("action_text")
    )


def _semantic_labels_for_dom(source: Dict[str, Any], contract: Dict[str, Any] | None = None) -> List[str]:
    contract = contract if isinstance(contract, dict) else {}
    labels: List[str] = []
    for field in ("accessible_label_must_include", "semantic_labels"):
        source_values = source.get(field) if isinstance(source.get(field), list) else []
        for item in source_values:
            text = _simple_text(item)
            if text:
                labels.append(text)
        contract_values = contract.get(field) if isinstance(contract.get(field), list) else []
        for item in contract_values:
            text = _simple_text(item)
            if text:
                labels.append(text)
    authority = _label_authority_for_dom(source, contract)
    label = _simple_text(source.get("required_display_label") or contract.get("required_display_label"))
    if authority == "semantic_role_only" and label:
        labels.append(label)
    return _dedupe_keep_order(labels)


def _source_step_keys_for_dom(source: Dict[str, Any], *fields: str) -> List[str]:
    values: List[str] = []
    for field in fields:
        items = source.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                text = _simple_text(item.get("step_key") or item.get("source_step_key"))
            else:
                text = _simple_text(item)
            if text:
                values.append(normalize_step_key(text))
    return _dedupe_keep_order(values)


def _dom_obligation_merge_key(obligation: Dict[str, Any]) -> str:
    visual_slot_key = _simple_text(obligation.get("visual_slot_key"))
    if visual_slot_key:
        return f"visual_slot::{visual_slot_key}"
    return _simple_text(obligation.get("obligation_id"))


def _component_dom_obligation(component: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(component, dict):
        return {}
    component_id = str(component.get("component_id") or component.get("id") or "").strip()
    if not component_id:
        return {}
    contract = component.get("interaction_contract") if isinstance(component.get("interaction_contract"), dict) else {}
    if (
        component.get("interaction_required") is not True
        and not contract
        and component.get("target_filename") in (None, "", [], {})
        and component.get("reducer") in (None, "", [], {})
        and component.get("operation_intent") in (None, "", [], {})
        and component.get("dom_contract") in (None, "", [], {})
        and component.get("action_host") in (None, "", [], {})
    ):
        return {}
    display_label = _visible_display_label_for_dom(component, contract)
    visible_text_contract = _visible_text_contract_for_dom(component, contract)
    label_authority = _label_authority_for_dom(component, contract)
    obligation = {
        "obligation_id": component_id,
        "aliases": [component_id],
        "visual_slot_key": component.get("visual_slot_key") or contract.get("visual_slot_key"),
        "component_kind": component.get("component_kind") or component.get("component_type"),
        "display_label": display_label,
        "visible_text_contract": visible_text_contract,
        "label_authority": label_authority,
        "semantic_labels": _semantic_labels_for_dom(component, contract),
        "source_step_keys": _source_step_keys_for_dom(component, "source_step_keys", "l0_transition_ids"),
        "operation_intent": _simple_text(
            component.get("operation_intent")
            or contract.get("operation_intent")
            or contract.get("action_text")
        ),
        "visibility_policy": _visibility_from_component(component),
        "target_filename": contract.get("expected_filename") or component.get("target_filename"),
        "reducer": contract.get("reducer") or component.get("reducer"),
    }
    return {k: v for k, v in obligation.items() if v not in (None, "", [], {})}


def _surface_dom_obligation(surface: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(surface, dict):
        return {}
    host_id = str(surface.get("required_data_element_id") or surface.get("surface_id") or "").strip()
    if not host_id:
        return {}
    aliases = surface.get("required_data_element_aliases")
    if not isinstance(aliases, list):
        aliases = []
    visible_text_contract = _visible_text_contract_for_dom(surface, {})
    label_authority = _label_authority_for_dom(surface, {})
    obligation = {
        "obligation_id": host_id,
        "aliases": _dedupe_keep_order([host_id] + [str(alias) for alias in aliases]),
        "visual_slot_key": surface.get("visual_slot_key"),
        "display_label": _visible_display_label_for_dom(surface, {}),
        "visible_text_contract": visible_text_contract,
        "label_authority": label_authority,
        "semantic_labels": _semantic_labels_for_dom(surface, {}),
        "source_step_keys": _source_step_keys_for_dom(surface, "source_step_keys", "evidence_steps"),
        "trace_hit_points": surface.get("trace_hit_points"),
        "visibility_policy": surface.get("initial_visibility_policy") or surface.get("runtime_visibility_policy"),
        "initial_dom_eligible": surface.get("initial_dom_eligible"),
        "state_surface_contract": surface.get("state_surface_contract"),
        "value_lifecycle": surface.get("value_lifecycle"),
    }
    return {k: v for k, v in obligation.items() if v not in (None, "", [], {})}


def _merge_dom_obligation(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    merged = dict(existing)
    conflicts: List[Dict[str, Any]] = []
    for field in ("display_label", "visibility_policy", "target_filename", "reducer"):
        old = _simple_text(merged.get(field))
        new = _simple_text(incoming.get(field))
        if old and new and old != new:
            conflicts.append(
                {
                    "code": "dom_obligation_field_conflict",
                    "obligation_id": merged.get("obligation_id"),
                    "field": field,
                    "values": [old, new],
                    "sources": [
                        merged.get("source_authority_refs", []),
                        incoming.get("source_authority_refs", []),
                    ],
                }
            )
            continue
        if not old and new:
            merged[field] = incoming.get(field)
    for field in ("aliases", "source_step_keys", "source_authority_refs", "semantic_labels"):
        merged[field] = _dedupe_keep_order(
            list(merged.get(field) or []) + list(incoming.get(field) or [])
        )
    trace_hit_points: List[Dict[str, Any]] = []
    for item in list(merged.get("trace_hit_points") or []) + list(incoming.get("trace_hit_points") or []):
        if isinstance(item, dict) and item not in trace_hit_points:
            trace_hit_points.append(item)
    if trace_hit_points:
        merged["trace_hit_points"] = trace_hit_points
    for field in ("state_surface_contract", "value_lifecycle", "initial_dom_eligible", "surface_role", "component_kind", "visual_slot_key", "visible_text_contract", "label_authority"):
        if merged.get(field) in (None, "", [], {}) and incoming.get(field) not in (None, "", [], {}):
            merged[field] = incoming.get(field)
    return {k: v for k, v in merged.items() if v not in (None, "", [], {})}, conflicts


def _action_obligation_from_spec_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    runtime_binding = action.get("runtime_binding") if isinstance(action.get("runtime_binding"), dict) else {}
    action_id = str(action.get("action_id") or "").strip()
    if not action_id:
        return {}
    return _clean_compact_dict(
        {
            "action_id": action_id,
            "action_type": action.get("action_type") or action.get("operation"),
            "reducer": action.get("reducer") or runtime_binding.get("reducer"),
            "target_filename": action.get("target_filename"),
            "component_ref": action.get("component_ref") or runtime_binding.get("component_ref"),
            "effect_kind": action.get("effect_kind") or runtime_binding.get("effect_kind"),
            "state_refs": action.get("state_refs") or runtime_binding.get("state_refs"),
            "state_reads": action.get("state_reads") or runtime_binding.get("state_reads"),
            "state_writes": action.get("state_writes") or runtime_binding.get("state_writes"),
            "expected_state_diff": action.get("expected_state_diff") or runtime_binding.get("state_diff"),
            "transaction_id": action.get("transaction_id") or runtime_binding.get("transaction_id"),
            "dom_binding_ref": action.get("dom_binding_ref") or runtime_binding.get("dom_binding_ref"),
            "visibility_precondition_action_id": action.get("visibility_precondition_action_id") or runtime_binding.get("visibility_precondition_action_id"),
            "visibility_precondition": action.get("visibility_precondition") or runtime_binding.get("visibility_precondition"),
            "required_visible_value": action.get("required_visible_value") or runtime_binding.get("required_visible_value"),
            "required_runtime_events": action.get("required_runtime_events") or runtime_binding.get("required_runtime_events"),
        },
        (
            "action_id",
            "action_type",
            "reducer",
            "target_filename",
            "component_ref",
            "effect_kind",
            "state_refs",
            "state_reads",
            "state_writes",
            "expected_state_diff",
            "transaction_id",
            "dom_binding_ref",
            "visibility_precondition_action_id",
            "visibility_precondition",
            "required_visible_value",
            "required_runtime_events",
        ),
    )


def _compact_state_effect(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _clean_compact_dict(
        value,
        (
            "effect",
            "state_ref",
            "state_refs",
            "state_reads",
            "state_writes",
            "delta",
            "value",
            "selected_value",
            "item_ref",
            "record_ref",
            "overlay_ref",
        ),
    )


def _compact_transition_edge_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    compact = _clean_compact_dict(
        item,
        (
            "step_key",
            "reducer",
        ),
    )
    state_effect = _compact_state_effect(item.get("state_effect"))
    if state_effect:
        compact["state_effect"] = state_effect
    return compact


def _compact_transaction_edge_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    compact = _clean_compact_dict(item, ("action_id", "reducer"))
    state_commit = item.get("state_commit") if isinstance(item.get("state_commit"), dict) else {}
    compact_commit = _clean_compact_dict(
        state_commit,
        ("state_refs", "state_reads", "state_writes"),
    )

    for key in ("state_refs", "state_reads", "state_writes"):
        if compact_commit.get(key) in (None, "", [], {}) and item.get(key) not in (None, "", [], {}):
            compact_commit[key] = item.get(key)
    if compact_commit:
        compact["state_commit"] = compact_commit
        for key in ("state_reads", "state_writes"):
            if compact_commit.get(key):
                compact[key] = compact_commit[key]
    return compact


def _compact_render_projection_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    return _clean_compact_dict(
        item,
        (
            "component_ref",
            "action_id",
            "reads_state",
            "reads_state_refs",
            "render_phase",
            "host_availability_phase",
            "state_value_phase",
            "content_projection_phase",
            "layout_policy",
            "source_step_keys",
        ),
    )


def _group_render_projections_for_render(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for raw in items:
        item = _compact_render_projection_for_render(raw)
        source_steps = [normalize_step_key(str(step)) for step in item.get("source_step_keys", []) if step]
        action_id = normalize_step_key(str(item.get("action_id") or (source_steps[0] if source_steps else "")))
        reads = _dedupe_keep_order(
            [str(item.get("reads_state") or "")] + [str(ref) for ref in item.get("reads_state_refs", [])]
        )
        key_payload = {
            "action_id": action_id,
            "render_phase": item.get("render_phase"),
        }
        key = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
        group = grouped.setdefault(
            key,
            {
                **key_payload,
                "reads_state_refs": [],
                "layout_policies": [],
                "lifecycle_phases": [],
            },
        )
        group["reads_state_refs"] = _dedupe_keep_order(group["reads_state_refs"] + reads)
        layout_policy = str(item.get("layout_policy") or "")
        if layout_policy and layout_policy not in group["layout_policies"]:
            group["layout_policies"].append(layout_policy)
        for field in ("host_availability_phase", "state_value_phase", "content_projection_phase"):
            value = str(item.get(field) or "")
            if value and value not in group["lifecycle_phases"]:
                group["lifecycle_phases"].append(value)
    return [
        {key: value for key, value in group.items() if value not in (None, "", [], {})}
        for group in grouped.values()
    ]


def _compact_surface_contract_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    compact = _clean_compact_dict(
        item,
        (
            "component_id",
            "render_phase",
            "initial_dom_eligible",
            "initial_visibility_policy",
            "runtime_visibility_policy",
        ),
    )
    lifecycle = item.get("value_lifecycle") if isinstance(item.get("value_lifecycle"), dict) else {}
    compact_lifecycle = _clean_compact_dict(
        lifecycle,
        ("state_ref", "state_refs", "initial_value_source", "post_action_value", "projection_phase"),
    )
    if compact_lifecycle:
        compact["value_lifecycle"] = compact_lifecycle
    surface = item.get("state_surface_contract") if isinstance(item.get("state_surface_contract"), dict) else {}
    compact_surface = _clean_compact_dict(
        surface,
        ("surface_id", "open_state_id", "surface_family", "initial_visibility_policy", "runtime_visibility_policy"),
    )
    if compact_surface:
        compact["state_surface_contract"] = compact_surface
    return compact


def _compact_post_action_oracle_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    compact = _clean_compact_dict(
        item,
        (
            "oracle_id",
            "oracle_type",
            "type",
            "action_id",
            "step_key",
            "component_ref",
            "state_ref",
            "expected",
            "expected_filename",
            "target_filename",
            "display_label",
            "min_visible_count",
        ),
    )
    for key in ("expected_state_diff", "diff", "dom_contract"):
        if isinstance(item.get(key), dict) and item.get(key):
            compact[key] = item[key]
    return compact


def _group_post_action_oracles_for_render(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(items):
        item = _compact_post_action_oracle_for_render(raw)
        action_key = normalize_step_key(str(item.get("action_id") or item.get("step_key") or ""))
        key = action_key or f"unscoped::{index}"
        group = grouped.setdefault(
            key,
            {
                "action_id": action_key,
                "oracle_types": [],
                "dom_requirements": [],
            },
        )
        for field, target in (
            ("oracle_type", "oracle_types"),
            ("type", "oracle_types"),
        ):
            value = item.get(field)
            if value not in (None, "", [], {}) and value not in group[target]:
                group[target].append(value)
        dom_contract = item.get("dom_contract") if isinstance(item.get("dom_contract"), dict) else {}
        requirement = str(dom_contract.get("required_behavior") or "")
        if requirement and requirement not in group["dom_requirements"]:
            group["dom_requirements"].append(requirement)
        if item.get("min_visible_count") not in (None, ""):
            group["min_visible_count"] = item["min_visible_count"]

    result: List[Dict[str, Any]] = []
    for group in grouped.values():
        compact = {key: value for key, value in group.items() if value not in (None, "", [], {})}

        raw_oracle_ids = [
            str(item.get("oracle_id") or "")
            for item in items
            if isinstance(item, dict)
            and normalize_step_key(str(item.get("action_id") or item.get("step_key") or "")) == group.get("action_id")
            and item.get("oracle_id")
        ]
        if len(raw_oracle_ids) == 1:
            compact["oracle_id"] = raw_oracle_ids[0]
        if len(group["oracle_types"]) == 1:
            compact["oracle_type"] = group["oracle_types"][0]
        result.append(compact)
    return result


def _compact_action_effect_binding_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    return _clean_compact_dict(item, ("action_id",))


def _compact_runtime_transaction_binding_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    return _clean_compact_dict(
        item,
        (
            "transaction_id",
            "transaction_kind",
            "open_action_id",
            "commit_action_id",
            "source_action_id",
        ),
    )


def _compact_dom_binding_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    compact = _clean_compact_dict(item, ("dom_binding_id", "binding_id", "action_id"))
    interaction = item.get("interaction") if isinstance(item.get("interaction"), dict) else {}
    compact_interaction = _clean_compact_dict(interaction, ("event", "handler"))
    if compact_interaction:
        compact["interaction"] = compact_interaction
    return compact


def _runtime_binding_reference_view(
    action_effects: Sequence[Dict[str, Any]],
    transactions: Sequence[Dict[str, Any]],
    dom_bindings: Sequence[Dict[str, Any]],
    global_schema_projection: Any,
) -> Dict[str, Any]:
    transaction_refs: Dict[str, str] = {}
    for item in transactions:
        action_id = str(
            item.get("source_action_id")
            or item.get("commit_action_id")
            or item.get("open_action_id")
            or ""
        )
        transaction_id = str(item.get("transaction_id") or "")
        if action_id and transaction_id:
            transaction_refs[action_id] = transaction_id
    dom_refs: Dict[str, Dict[str, Any]] = {}
    for item in dom_bindings:
        action_id = str(item.get("action_id") or "")
        binding_id = str(item.get("dom_binding_id") or item.get("binding_id") or "")
        interaction = item.get("interaction") if isinstance(item.get("interaction"), dict) else {}
        if action_id:
            dom_refs[action_id] = {
                key: value
                for key, value in {
                    "binding_id": binding_id,
                    "event": interaction.get("event"),
                    "handler": interaction.get("handler"),
                }.items()
                if value not in (None, "", [], {})
            }
    return {
        "global_schema_projection": global_schema_projection if global_schema_projection else {},
        "action_effect_contracts": [
            str(item.get("action_id") or "") for item in action_effects if item.get("action_id")
        ],
        "runtime_transaction_contracts": transaction_refs,
        "dom_binding_contracts": dom_refs,
    }


def _compact_dom_obligation_for_render(item: Dict[str, Any]) -> Dict[str, Any]:
    compact = _clean_compact_dict(
        item,
        (
            "obligation_id",
            "component_kind",
            "display_label",
            "visible_text_contract",
            "visual_slot_key",
            "semantic_labels",
            "accessible_label_must_include",
            "source_step_keys",
            "operation_intent",
            "visibility_policy",
            "target_filename",
            "initial_dom_eligible",
        ),
    )
    raw_aliases = _dedupe_keep_order(str(alias) for alias in item.get("aliases", []) if alias)
    aliases = (
        raw_aliases
        if len(raw_aliases) > 1
        else [alias for alias in raw_aliases if alias != str(item.get("obligation_id") or "")]
    )
    if aliases:
        compact["aliases"] = _dedupe_keep_order(aliases)
    surface = item.get("state_surface_contract") if isinstance(item.get("state_surface_contract"), dict) else {}
    if surface:
        compact_surface = _clean_compact_dict(
            surface,
            ("surface_id", "surface_family", "initial_visibility_policy", "runtime_visibility_policy"),
        )
        options = []
        for option in surface.get("options", []) if isinstance(surface.get("options"), list) else []:
            if not isinstance(option, dict):
                continue
            expected_effect = option.get("expected_effect") if isinstance(option.get("expected_effect"), dict) else {}
            options.append(
                {
                    key: value
                    for key, value in {
                        "option_id": option.get("option_id"),
                        "label": option.get("label"),
                        "effect": _clean_compact_dict(expected_effect, ("type", "target_filename")),
                    }.items()
                    if value not in (None, "", [], {})
                }
            )
        if options:
            compact_surface["options"] = options
        compact["state_surface_contract"] = compact_surface
    return compact


def _asset_slot_from_resource(resource: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(resource, dict):
        return {}
    if str(resource.get("prompt_visibility") or "").strip() != "prompt":
        return {}
    asset_name = str(resource.get("asset_name") or "").strip()
    if not asset_name:
        return {}
    return _clean_compact_dict(
        {
            "asset_name": asset_name,
            "path": f"images/{asset_name}",
        },
        (
            "asset_name",
            "path",
        ),
    )


def _compact_state_schema_for_render(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact = _clean_compact_dict(value, ("collections", "drafts", "parameters"))
    runtime_refs = []
    for item in value.get("runtime_state_refs", []) if isinstance(value.get("runtime_state_refs"), list) else []:
        if isinstance(item, dict):
            ref = str(item.get("state_ref") or "")
        else:
            ref = str(item or "")
        if ref:
            runtime_refs.append(ref)
    if runtime_refs:
        compact["runtime_state_refs"] = _dedupe_keep_order(runtime_refs)
    return compact


def _compact_state_store_slice_for_render(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    schema = value.get("schema") if isinstance(value.get("schema"), dict) else {}
    return {
        key: item
        for key, item in {
            "state_refs": _dedupe_keep_order(value.get("state_refs", [])),
            "transition_count": schema.get("transition_count"),
            "query_projection_count": schema.get("query_projection_count"),
        }.items()
        if item not in (None, "", [], {})
    }


def build_pagegen_render_view(scope: Dict[str, Any]) -> Dict[str, Any]:

    if not isinstance(scope, dict):
        return {}
    projection = scope.get("page_environment_projection") if isinstance(scope.get("page_environment_projection"), dict) else {}
    spec_view = scope.get("spec_execution_view") if isinstance(scope.get("spec_execution_view"), dict) else {}
    dom_by_id: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    source_family_counts: Dict[str, int] = {
        "page_environment_components": 0,
        "required_interaction_surfaces": 0,
        "spec_actions": 0,
        "autocomplete_contracts": 0,
        "initial_control_state_contracts": 0,
        "initial_dom_contract": 0,
        "route_action_slots": 0,
        "asset_resources": 0,
        "global_schema_projection": 0,
        "action_effect_authority_paths": 0,
        "state_store_slice": 0,
        "transition_edges": 0,
        "render_projections": 0,
        "surface_contracts": 0,
        "post_action_oracles": 0,
        "action_effect_contracts": 0,
        "runtime_transaction_contracts": 0,
        "dom_binding_contracts": 0,
    }

    for component in projection.get("components", []) if isinstance(projection.get("components"), list) else []:
        obligation = _component_dom_obligation(component)
        oid = _dom_obligation_merge_key(obligation)
        if not oid:
            continue
        source_family_counts["page_environment_components"] += 1
        if oid in dom_by_id:
            dom_by_id[oid], new_conflicts = _merge_dom_obligation(dom_by_id[oid], obligation)
            conflicts.extend(new_conflicts)
        else:
            dom_by_id[oid] = obligation

    for surface in scope.get("required_interaction_surfaces", []) if isinstance(scope.get("required_interaction_surfaces"), list) else []:
        obligation = _surface_dom_obligation(surface)
        oid = _dom_obligation_merge_key(obligation)
        if not oid:
            continue
        source_family_counts["required_interaction_surfaces"] += 1
        if oid in dom_by_id:
            dom_by_id[oid], new_conflicts = _merge_dom_obligation(dom_by_id[oid], obligation)
            conflicts.extend(new_conflicts)
        else:
            dom_by_id[oid] = obligation

    action_candidates: List[Dict[str, Any]] = []
    action_source_ids = set()
    action_source_missing_identity_count = 0
    for key in ("typed_actions", "actions"):
        actions = spec_view.get(key)
        if not isinstance(actions, list):
            continue
        for action in actions:
            row = _action_obligation_from_spec_action(action)
            if row:
                action_source_ids.add(str(row.get("action_id") or ""))
                _append_unique_dict(action_candidates, row, "action_id")
            elif isinstance(action, dict):
                action_source_missing_identity_count += 1
    source_family_counts["spec_actions"] = len([item for item in action_source_ids if item]) + action_source_missing_identity_count

    for field in (
        "autocomplete_contracts",
        "initial_control_state_contracts",
        "initial_dom_contract",
    ):
        value = spec_view.get(field)
        if isinstance(value, list):
            source_family_counts[field] = len(value)
        elif isinstance(value, dict):
            source_family_counts[field] = 1

    state_obligations = _clean_compact_dict(
        {
            "autocomplete_contracts": spec_view.get("autocomplete_contracts"),
            "initial_control_state_contracts": spec_view.get("initial_control_state_contracts"),
            "initial_dom_contract": spec_view.get("initial_dom_contract"),
            "initial_state": spec_view.get("initial_state"),
            "initial_state_by_trace_id": spec_view.get("initial_state_by_trace_id"),
            "state_schema": _compact_state_schema_for_render(spec_view.get("state_schema")),
            "state_store_slice": _compact_state_store_slice_for_render(spec_view.get("state_store_slice")),
        },
        (
            "autocomplete_contracts",
            "initial_control_state_contracts",
            "initial_dom_contract",
            "initial_state",
            "initial_state_by_trace_id",
            "state_schema",
            "state_store_slice",
        ),
    )

    transition_edges = [
        _compact_transition_edge_for_render(item) for item in spec_view.get("transition_edges", [])
        if isinstance(item, dict)
    ] if isinstance(spec_view.get("transition_edges"), list) else []
    raw_render_projections = [
        item for item in spec_view.get("render_projections", []) if isinstance(item, dict)
    ] if isinstance(spec_view.get("render_projections"), list) else []
    render_projections = _group_render_projections_for_render(raw_render_projections)
    surface_contracts = [
        _compact_surface_contract_for_render(item) for item in spec_view.get("surface_contracts", [])
        if isinstance(item, dict)
    ] if isinstance(spec_view.get("surface_contracts"), list) else []
    raw_post_action_oracles = [
        item for item in spec_view.get("post_action_oracles", []) if isinstance(item, dict)
    ] if isinstance(spec_view.get("post_action_oracles"), list) else []
    post_action_oracles = _group_post_action_oracles_for_render(raw_post_action_oracles)
    transaction_edges = [
        _compact_transaction_edge_for_render(item) for item in spec_view.get("transaction_edges", [])
        if isinstance(item, dict)
    ] if isinstance(spec_view.get("transaction_edges"), list) else []

    action_effect_bindings = [
        _compact_action_effect_binding_for_render(item)
        for item in spec_view.get("action_effect_contracts", [])
        if isinstance(item, dict)
    ] if isinstance(spec_view.get("action_effect_contracts"), list) else []
    runtime_transaction_bindings = [
        _compact_runtime_transaction_binding_for_render(item)
        for item in spec_view.get("runtime_transaction_contracts", [])
        if isinstance(item, dict)
    ] if isinstance(spec_view.get("runtime_transaction_contracts"), list) else []
    dom_binding_bindings = [
        _compact_dom_binding_for_render(item)
        for item in spec_view.get("dom_binding_contracts", [])
        if isinstance(item, dict)
    ] if isinstance(spec_view.get("dom_binding_contracts"), list) else []

    for field, value in (
        ("state_store_slice", spec_view.get("state_store_slice")),
        ("transition_edges", transition_edges),
        ("render_projections", raw_render_projections),
        ("surface_contracts", surface_contracts),
        ("post_action_oracles", raw_post_action_oracles),
        ("action_effect_contracts", spec_view.get("action_effect_contracts")),
        ("runtime_transaction_contracts", spec_view.get("runtime_transaction_contracts")),
        ("dom_binding_contracts", spec_view.get("dom_binding_contracts")),
    ):
        if isinstance(value, list):
            source_family_counts[field] = len(value)
        elif isinstance(value, dict) and value:
            source_family_counts[field] = 1

    route_slots = []
    for slot in scope.get("route_action_slots", []) if isinstance(scope.get("route_action_slots"), list) else []:
        if not isinstance(slot, dict):
            continue
        source_family_counts["route_action_slots"] += 1
        route_slots.append(
            _clean_compact_dict(
                slot,
                (
                    "component_id",
                    "required_data_element_id",
                    "action_slot_id",
                    "source_filename",
                    "target_filename",
                    "executor",
                    "label",
                    "control_label",
                    "source_state_key",
                    "shell_family_id",
                    "shell_slot_key",
                    "binding_source",
                    "binding_sources",
                    "query_precondition",
                    "state_binding",
                ),
            )
        )

    asset_slots = []
    for resource in scope.get("page_resources", []) if isinstance(scope.get("page_resources"), list) else []:
        slot = _asset_slot_from_resource(resource)
        if slot:
            source_family_counts["asset_resources"] += 1
            asset_slots.append(slot)

    if scope.get("global_schema_projection") not in (None, "", [], {}):
        source_family_counts["global_schema_projection"] = 1
    if scope.get("action_effect_authority_paths") not in (None, "", [], {}):
        source_family_counts["action_effect_authority_paths"] = 1

    render_family_counts = {
        "dom_obligations": len(dom_by_id),
        "action_obligations": len(action_candidates),
        "autocomplete_contracts": len(state_obligations.get("autocomplete_contracts", []) or []),
        "initial_control_state_contracts": len(state_obligations.get("initial_control_state_contracts", []) or []),
        "initial_dom_contract": len(state_obligations.get("initial_dom_contract", []) or []),
        "route_slots": len([slot for slot in route_slots if slot]),
        "asset_slots": len(asset_slots),
        "global_schema_projection": 1 if scope.get("global_schema_projection") not in (None, "", [], {}) else 0,
        "action_effect_authority_paths": 1 if scope.get("action_effect_authority_paths") not in (None, "", [], {}) else 0,
        "state_store_slice": 1 if state_obligations.get("state_store_slice") else 0,
        "transition_edges": len(transition_edges),
        "render_projections": len(render_projections),
        "surface_contracts": len(surface_contracts),
        "post_action_oracles": len(post_action_oracles),
        "action_effect_contracts": len(action_effect_bindings),
        "runtime_transaction_contracts": len(runtime_transaction_bindings),
        "dom_binding_contracts": len(dom_binding_bindings),
    }
    source_families = [
        {
            "family": "dom_obligations",
            "sources": {
                "page_environment_components": source_family_counts["page_environment_components"],
                "required_interaction_surfaces": source_family_counts["required_interaction_surfaces"],
            },
            "source_count": source_family_counts["page_environment_components"]
            + source_family_counts["required_interaction_surfaces"],
            "render_count": render_family_counts["dom_obligations"],
            "coalescing_allowed": True,
        },
        {
            "family": "action_obligations",
            "sources": {"spec_actions": source_family_counts["spec_actions"]},
            "source_count": source_family_counts["spec_actions"],
            "render_count": render_family_counts["action_obligations"],
            "coalescing_allowed": False,
        },
        {
            "family": "autocomplete_contracts",
            "sources": {"spec_execution_view": source_family_counts["autocomplete_contracts"]},
            "source_count": source_family_counts["autocomplete_contracts"],
            "render_count": render_family_counts["autocomplete_contracts"],
            "coalescing_allowed": False,
        },
        {
            "family": "initial_control_state_contracts",
            "sources": {"spec_execution_view": source_family_counts["initial_control_state_contracts"]},
            "source_count": source_family_counts["initial_control_state_contracts"],
            "render_count": render_family_counts["initial_control_state_contracts"],
            "coalescing_allowed": False,
        },
        {
            "family": "initial_dom_contract",
            "sources": {"spec_execution_view": source_family_counts["initial_dom_contract"]},
            "source_count": source_family_counts["initial_dom_contract"],
            "render_count": render_family_counts["initial_dom_contract"],
            "coalescing_allowed": False,
        },
        {
            "family": "route_slots",
            "sources": {"route_action_slots": source_family_counts["route_action_slots"]},
            "source_count": source_family_counts["route_action_slots"],
            "render_count": render_family_counts["route_slots"],
            "coalescing_allowed": False,
        },
        {
            "family": "asset_slots",
            "sources": {"page_resources": source_family_counts["asset_resources"]},
            "source_count": source_family_counts["asset_resources"],
            "render_count": render_family_counts["asset_slots"],
            "coalescing_allowed": False,
        },
        {
            "family": "runtime_bindings.global_schema_projection",
            "sources": {"spec_execution_view": source_family_counts["global_schema_projection"]},
            "source_count": source_family_counts["global_schema_projection"],
            "render_count": render_family_counts["global_schema_projection"],
            "coalescing_allowed": False,
        },
        {
            "family": "runtime_bindings.action_effect_authority_paths",
            "sources": {"spec_execution_view": source_family_counts["action_effect_authority_paths"]},
            "source_count": source_family_counts["action_effect_authority_paths"],
            "render_count": render_family_counts["action_effect_authority_paths"],
            "coalescing_allowed": False,
        },
        {
            "family": "state_obligations.state_store_slice",
            "sources": {"spec_execution_view": source_family_counts["state_store_slice"]},
            "source_count": source_family_counts["state_store_slice"],
            "render_count": render_family_counts["state_store_slice"],
            "coalescing_allowed": False,
            "required_when_stateful": True,
        },
        {
            "family": "transition_edges",
            "sources": {"spec_execution_view": source_family_counts["transition_edges"]},
            "source_count": source_family_counts["transition_edges"],
            "render_count": render_family_counts["transition_edges"],
            "coalescing_allowed": False,
            "required_when_stateful": True,
        },
        {
            "family": "render_projections",
            "sources": {"spec_execution_view": source_family_counts["render_projections"]},
            "source_count": source_family_counts["render_projections"],
            "render_count": render_family_counts["render_projections"],
            "coalescing_allowed": True,
            "required_when_stateful": True,
        },
        {
            "family": "surface_contracts",
            "sources": {"spec_execution_view": source_family_counts["surface_contracts"]},
            "source_count": source_family_counts["surface_contracts"],
            "render_count": render_family_counts["surface_contracts"],
            "coalescing_allowed": False,
            "required_when_stateful": True,
        },
        {
            "family": "post_action_oracles",
            "sources": {"spec_execution_view": source_family_counts["post_action_oracles"]},
            "source_count": source_family_counts["post_action_oracles"],
            "render_count": render_family_counts["post_action_oracles"],
            "coalescing_allowed": True,
            "required_when_stateful": True,
        },
        {
            "family": "runtime_bindings.action_effect_contracts",
            "sources": {"spec_execution_view": source_family_counts["action_effect_contracts"]},
            "source_count": source_family_counts["action_effect_contracts"],
            "render_count": render_family_counts["action_effect_contracts"],
            "coalescing_allowed": False,
            "required_when_stateful": True,
        },
        {
            "family": "runtime_bindings.runtime_transaction_contracts",
            "sources": {"spec_execution_view": source_family_counts["runtime_transaction_contracts"]},
            "source_count": source_family_counts["runtime_transaction_contracts"],
            "render_count": render_family_counts["runtime_transaction_contracts"],
            "coalescing_allowed": False,
            "required_when_stateful": True,
        },
        {
            "family": "runtime_bindings.dom_binding_contracts",
            "sources": {"spec_execution_view": source_family_counts["dom_binding_contracts"]},
            "source_count": source_family_counts["dom_binding_contracts"],
            "render_count": render_family_counts["dom_binding_contracts"],
            "coalescing_allowed": False,
            "required_when_stateful": True,
        },
    ]
    stateful_execution_required = bool(spec_view.get("stateful_execution_required"))
    missing_required_families = []
    for item in source_families:
        source_count = int(item.get("source_count") or 0)
        render_count = int(item.get("render_count") or 0)
        if item.get("required_when_stateful") and stateful_execution_required:
            if source_count <= 0 or render_count <= 0:
                missing_required_families.append(
                    {
                        "family": item.get("family"),
                        "source_count": source_count,
                        "render_count": render_count,
                    }
                )
            continue
        if source_count <= 0:
            continue
        missing = render_count == 0 if item.get("coalescing_allowed") else render_count < source_count
        if missing:
            missing_required_families.append(
                {
                    "family": item.get("family"),
                    "source_count": source_count,
                    "render_count": render_count,
                }
            )
    fold_coverage = {
        "status": "pass" if not missing_required_families else "blocked",
        "coverage_counts_by_family": {
            str(item.get("family") or ""): [
                int(item.get("source_count") or 0),
                int(item.get("render_count") or 0),
            ]
            for item in source_families
            if item.get("family")
        },
        "missing_required_families": missing_required_families,
        "missing_required_count": len(missing_required_families),
        "policy_omitted_families": [],
    }

    render_view = {
        "format": "pagegen_render_view",
        "schema_revision": 2,
        "filename": scope.get("filename", ""),
        "page_identity": {
            "page_id": scope.get("page_id", ""),
            "page_name": scope.get("page_name", ""),
            "page_type": scope.get("page_type", ""),
            "description": scope.get("description", ""),
        },
        "authority": {
            "source": "pagegen_input_scope.compiler",
            "policy": "single_authority_render_obligations",
            "legacy_parallel_sections_suppressed": True,
            "relational_host_join": "action_id/step_key -> dom_obligations.source_step_keys",
        },
        "dom_obligations": [
            _compact_dom_obligation_for_render(item)
            for item in sorted(dom_by_id.values(), key=lambda item: str(item.get("obligation_id") or ""))
        ],
        "action_obligations": sorted(action_candidates, key=lambda item: str(item.get("action_id") or "")),
        "state_obligations": state_obligations,
        "transition_edges": transition_edges,
        "transaction_edges": transaction_edges,
        "render_projections": render_projections,
        "surface_contracts": surface_contracts,
        "post_action_oracles": post_action_oracles,
        "route_slots": [slot for slot in route_slots if slot],
        "asset_slots": asset_slots,
        "runtime_bindings": _runtime_binding_reference_view(
            action_effect_bindings,
            runtime_transaction_bindings,
            dom_binding_bindings,
            scope.get("global_schema_projection", {}),
        ),
        "semantic_contract_digest": spec_view.get("semantic_contract_digest", ""),
        "stateful_execution_required": stateful_execution_required,
        "validation_contract": {
            "dom_obligation_count": len(dom_by_id),
            "action_obligation_count": len(action_candidates),
            "state_obligation_count": len(state_obligations),
            "asset_slot_count": len(asset_slots),
            "fold_coverage": fold_coverage,
            "conflict_report": {
                "status": "pass" if not conflicts else "blocked",
                "conflict_count": len(conflicts),
                "conflicts": conflicts,
            },
        },
    }
    return {k: v for k, v in render_view.items() if v not in (None, "", [], {})}


def _step_sort_index(step_key: str) -> int:
    match = re.search(r":step(\d+)$", str(step_key or ""))
    if not match:
        return 10**9
    return int(match.group(1))


def _simple_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _component_source_steps(component: Dict[str, Any]) -> List[str]:
    steps = component.get("source_step_keys")
    if not isinstance(steps, list):
        steps = component.get("l0_transition_ids")
    if not isinstance(steps, list):
        return []
    return _dedupe_keep_order(normalize_step_key(str(step)) for step in steps if str(step or "").strip())


def _source_render_role(source_steps: Sequence[str], render_roles: Dict[str, Any]) -> str:
    roles = _normalize_step_keyed_mapping(render_roles)
    for step_key in source_steps:
        role = str(roles.get(normalize_step_key(str(step_key))) or "").strip()
        if role:
            return role
    return ""


def _opened_scope_family(label: str, operation_intent: str, expected_filename: str, component_kind: str) -> str:
    text = f"{label} {operation_intent} {expected_filename} {component_kind}".lower()
    if any(term in text for term in ("publish", "compose", "发布", "创作", "写文字", "相册", "相机", "拍摄", "拍视频")):
        return "create_entry_sheet"
    if any(term in text for term in ("回复", "reply", "comment")):
        return "reply_sheet"
    if any(term in text for term in ("删除", "置顶", "标为未读", "清空", "delete", "pin", "unread")):
        return "list_item_context_actions"
    if "筛选" in text or "filter" in text:
        return "filter_sheet"
    if expected_filename:
        return f"route_options.{expected_filename}"
    return "generic_overlay"


def _surface_group_for_l0_scope(
    *,
    label: str,
    operation_intent: str,
    expected_filename: str,
    component_kind: str,
) -> Tuple[str, str]:
    text = f"{label} {operation_intent} {expected_filename} {component_kind}".lower()
    label_key = re.sub(r"\s+", "_", label.strip().lower())
    target_key = re.sub(r"[^a-z0-9_.-]+", "_", expected_filename.strip().lower()).strip("_")
    if label.strip() == "+" and any(term in text for term in ("发布", "创作", "publish", "create", "add_to_cart")):
        return "shell_create_entry", "shell_navigation.create_entry"
    if any(term in text for term in ("底部导航", "bottom tab", "tab bar")) or label in {"首页", "消息", "我", "Home", "Messages", "Me"}:
        return "shell_navigation", f"shell_navigation.{target_key or label_key or 'slot'}"
    if "搜索" in text or "search" in text:
        return "query_entry_surface", "top_bar.query_entry"
    if any(term in text for term in ("定位", "当前位置", "位置", "location")):
        return "location_selector_surface", "top_bar.location_selector"
    if any(term in text for term in ("扫码", "scan")):
        return "utility_surface", f"top_bar.{label_key or 'utility'}"
    if "卡片" in text or "card" in text:
        return "content_action_surface", ""
    if any(term in text for term in ("入口", "entry", "navigation_control")) and not expected_filename:
        return "repeated_navigation_surface", ""
    return "current_page_surface", ""


def _l0_surface_signature(
    *,
    surface_group: str,
    shell_slot: str,
    source_render_role: str,
    label: str,
    operation_intent: str,
    expected_filename: str,
    component_kind: str,
) -> str:
    def signature_token(value: str, fallback: str) -> str:
        token = _simple_text(value).replace("|", "/")
        return token or fallback

    def task_action_family() -> str:
        text = f"{operation_intent} {component_kind}".lower()
        if any(term in text for term in ("加购", "加入购物车", "add_to_cart", "cart_quantity")):
            return "add_to_cart"
        if any(term in text for term in ("去结算", "checkout", "submit")):
            return "submit"
        if any(term in text for term in ("分类", "category")):
            return "category_select"
        if any(term in text for term in ("选规格", "规格", "option", "select")):
            return "option_select"
        return signature_token(operation_intent, "action")

    if surface_group == "shell_create_entry":
        return "shell_create_entry|shell_navigation.create_entry"
    if shell_slot:
        return "|".join([surface_group, shell_slot, expected_filename or "<same-page-or-no-target>"])
    if source_render_role == "post_interaction_overlay":
        family = _opened_scope_family(label, operation_intent, expected_filename, component_kind)
        return "|".join(["opened_interaction_surface", family])
    if (
        source_render_role
        and source_render_role != "canonical_layout"
        and surface_group in {"content_action_surface", "current_page_surface"}
        and (label or operation_intent)
    ):
        return "|".join(
            [
                "task_action_surface",
                signature_token(component_kind, "component"),
                task_action_family(),
                signature_token(label, "unlabeled"),
            ]
        )
    if surface_group in {"repeated_navigation_surface", "content_action_surface", "current_page_surface"}:
        return "|".join([surface_group, "initial"])
    return "|".join([surface_group or "current_page_surface", expected_filename or "<same-page-or-no-target>"])


def _task_carousel_part_key(annotation: Dict[str, Any], *, fallback: str) -> str:
    text = f"{annotation.get('action') or ''} {annotation.get('reason') or ''}"
    if not any(term in text for term in ("横向", "滑动", "宫格", "分类列表", "carousel", "grid")):
        return ""
    match = re.search(r"第\s*([一二三四五六七八九十0-9]+)\s*页", text)
    if not match:
        return fallback
    raw = match.group(1)
    cn_digits = {
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "十": "10",
    }
    page = cn_digits.get(raw, raw)
    return f"grid_page_{page}"


def _trace_id_from_step_key(step_key: str) -> str:
    return str(step_key or "").split(":step", 1)[0]


def _candidate_step_keys_by_trace(candidate_refs: Sequence[str]) -> Dict[str, List[str]]:
    by_trace: Dict[str, List[str]] = {}
    for ref in candidate_refs:
        step_key = normalize_step_key(step_key_from_screenshot_ref(str(ref)) or "")
        trace_id = _trace_id_from_step_key(step_key)
        if not step_key or not trace_id:
            continue
        by_trace.setdefault(trace_id, []).append(step_key)
    for trace_steps in by_trace.values():
        trace_steps.sort(key=_step_sort_index)
    return by_trace


_SCROLL_DEDUPE_HASH_SIZE = 16
_SCROLL_DUPLICATE_AHASH_DISTANCE = 128


def _is_vertical_scroll_layout_annotation(annotation: Dict[str, Any]) -> bool:
    text = f"{annotation.get('action') or ''} {annotation.get('reason') or ''}".lower()
    if not text.strip():
        return False
    if any(term in text for term in ("横向", "横滑", "水平", "左右", "horizontal")):
        return False
    return any(term in text for term in ("向上滑动", "继续向上", "上滑", "滚动", "scroll", "swipe up"))


def _is_task_carousel_selected_state_annotation(annotation: Dict[str, Any]) -> bool:
    text = f"{annotation.get('action') or ''} {annotation.get('reason') or ''}".lower()
    if not text.strip():
        return False
    if not any(term in text for term in ("横向", "标签", "tab", "carousel", "分类")):
        return False
    if any(term in text for term in ("向上滑动", "上滑", "纵向", "列表继续", "scroll list")):
        return False
    return any(term in text for term in ("激活", "选中", "高亮", "active", "selected", "点击"))


def _task_carousel_signature_is_step_keyed(signature: str, step_key: str) -> bool:
    return signature == f"task_carousel_surface|{normalize_step_key(step_key)}"


def _scroll_visual_crop(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width <= 0 or height <= 0:
        return image
    top = int(height * 0.24)
    bottom = int(height * 0.88)
    if bottom <= top:
        top = 0
        bottom = height
    return image.crop((0, top, width, bottom))


def _average_hash_hex(image: Image.Image, *, size: int = _SCROLL_DEDUPE_HASH_SIZE) -> str:
    grayscale = _scroll_visual_crop(image.convert("RGB")).convert("L")
    resized = grayscale.resize((size, size), Image.Resampling.LANCZOS)
    values = list(resized.tobytes())
    average = sum(values) / len(values) if values else 0
    bits = 0
    for value in values:
        bits = (bits << 1) | (1 if value >= average else 0)
    return f"{bits:0{(size * size + 3) // 4}x}"


def _hash_distance(left: str, right: str) -> Optional[int]:
    if not left or not right:
        return None
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


def _scroll_visual_fingerprint(ref: str) -> Dict[str, Any]:
    path = Path(str(ref or ""))
    if not path.is_file():
        return {
            "dedupe_crop_policy": "phone_content_region_top24_bottom88",
            "visual_fingerprint_status": "missing_screenshot",
        }
    try:
        with Image.open(path) as image:
            return {
                "dedupe_crop_policy": "phone_content_region_top24_bottom88",
                "visual_fingerprint": _average_hash_hex(image),
                "visual_fingerprint_algo": f"average_hash_{_SCROLL_DEDUPE_HASH_SIZE}x{_SCROLL_DEDUPE_HASH_SIZE}",
                "visual_fingerprint_status": "ok",
                "source_image_size": {"width": image.width, "height": image.height},
            }
    except Exception as exc:
        return {
            "dedupe_crop_policy": "phone_content_region_top24_bottom88",
            "visual_fingerprint_status": "read_error",
            "visual_fingerprint_error": str(exc),
        }


def _build_scroll_visual_dedupe_index(
    obligations: Sequence[Dict[str, Any]],
    *,
    refs_by_step: Dict[str, str],
    render_roles: Dict[str, Any],
    screenshot_render_annotations: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    annotations = _normalize_step_keyed_mapping(screenshot_render_annotations or {})
    candidates_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for obligation in obligations:
        signature = str(obligation.get("signature") or "")
        if not signature.startswith("task_carousel_surface|"):
            continue
        source_steps = [
            normalize_step_key(str(step))
            for step in obligation.get("source_step_keys", [])
            if str(step or "").strip()
        ]
        if len(source_steps) != 1:
            continue
        step_key = source_steps[0]
        if not _task_carousel_signature_is_step_keyed(signature, step_key):
            continue
        if str(render_roles.get(step_key) or "").strip() != "interaction_state_change":
            continue
        ref = refs_by_step.get(step_key, "")
        trace_id = _trace_id_from_step_key(step_key)
        if not ref or not trace_id:
            continue
        fingerprint = _scroll_visual_fingerprint(ref)
        candidates_by_trace.setdefault(trace_id, []).append(
            {
                "step_key": step_key,
                "raw_prompt_signature": signature,
                "ref": ref,
                "scroll_layout_annotation_match": _is_vertical_scroll_layout_annotation(
                    annotations.get(step_key, {}) if isinstance(annotations.get(step_key, {}), dict) else {}
                ),
                **fingerprint,
            }
        )

    dedupe_by_step: Dict[str, Dict[str, Any]] = {}
    for trace_id, rows in candidates_by_trace.items():
        rows.sort(key=lambda item: _step_sort_index(str(item.get("step_key") or "")))
        representatives: List[Dict[str, Any]] = []
        for row in rows:
            fingerprint = str(row.get("visual_fingerprint") or "")
            matched: Optional[Dict[str, Any]] = None
            matched_distance: Optional[int] = None
            if fingerprint:
                for representative in representatives:
                    distance = _hash_distance(fingerprint, str(representative.get("visual_fingerprint") or ""))
                    if distance is None:
                        continue
                    if distance <= _SCROLL_DUPLICATE_AHASH_DISTANCE and (
                        matched_distance is None or distance < matched_distance
                    ):
                        matched = representative
                        matched_distance = distance
            if matched is None:
                cluster_index = len(representatives) + 1
                cluster_id = f"scroll_cluster:{trace_id}:{cluster_index}"
                prompt_signature = f"task_carousel_surface|{cluster_id}"
                representative = {
                    **row,
                    "scroll_cluster_id": cluster_id,
                    "prompt_signature": prompt_signature,
                    "dedupe_status": "scroll_cluster_representative",
                    "duplicate_of_step_key": "",
                    "visual_distance_to_representative": 0,
                    "scroll_duplicate_threshold": _SCROLL_DUPLICATE_AHASH_DISTANCE,
                }
                representatives.append(representative)
                dedupe_by_step[str(row["step_key"])] = representative
            else:
                dedupe_by_step[str(row["step_key"])] = {
                    **row,
                    "scroll_cluster_id": str(matched.get("scroll_cluster_id") or ""),
                    "prompt_signature": str(matched.get("prompt_signature") or row.get("raw_prompt_signature") or ""),
                    "dedupe_status": "scroll_cluster_duplicate",
                    "duplicate_of_step_key": str(matched.get("step_key") or ""),
                    "visual_distance_to_representative": matched_distance,
                    "scroll_duplicate_threshold": _SCROLL_DUPLICATE_AHASH_DISTANCE,
                }
        visual_representatives = [
            item
            for item in (dedupe_by_step.get(str(row.get("step_key") or "")) for row in rows)
            if isinstance(item, dict) and item.get("dedupe_status") == "scroll_cluster_representative"
        ]
        if len(visual_representatives) > 1 and all(bool(row.get("scroll_layout_annotation_match")) for row in rows):
            layout_cluster_id = f"scroll_layout_cluster:{trace_id}:1"
            layout_prompt_signature = f"task_carousel_surface|{layout_cluster_id}"
            representative_step_key = str(rows[0].get("step_key") or "")
            for row in rows:
                step_key = str(row.get("step_key") or "")
                existing = dict(dedupe_by_step.get(step_key) or row)
                is_representative = step_key == representative_step_key
                dedupe_by_step[step_key] = {
                    **existing,
                    "visual_scroll_cluster_id": str(existing.get("scroll_cluster_id") or ""),
                    "visual_scroll_prompt_signature": str(existing.get("prompt_signature") or ""),
                    "scroll_cluster_id": layout_cluster_id,
                    "prompt_signature": layout_prompt_signature,
                    "scroll_layout_cluster_id": layout_cluster_id,
                    "scroll_layout_dedupe_status": (
                        "scroll_layout_pattern_representative"
                        if is_representative
                        else "scroll_layout_pattern_duplicate"
                    ),
                    "dedupe_status": (
                        "scroll_layout_pattern_representative"
                        if is_representative
                        else "scroll_layout_pattern_duplicate"
                    ),
                    "duplicate_of_step_key": "" if is_representative else representative_step_key,
                    "scroll_layout_pattern_source": "continuous_vertical_scroll_annotation",
                }
    return dedupe_by_step


_OVERLAY_ROUTE_OPTION_PROMPT_SIGNATURE = "opened_interaction_surface|route_options"


def _overlay_route_option_signature_target(signature: str) -> str:
    prefix = "opened_interaction_surface|route_options."
    if not str(signature or "").startswith(prefix):
        return ""
    return str(signature)[len(prefix):]


def _build_overlay_route_option_dedupe_index(
    obligations: Sequence[Dict[str, Any]],
    *,
    render_roles: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for obligation in obligations:
        raw_signature = str(obligation.get("signature") or "")
        target = _overlay_route_option_signature_target(raw_signature)
        if not target:
            continue
        source_steps = [
            normalize_step_key(str(step))
            for step in obligation.get("source_step_keys", [])
            if str(step or "").strip()
        ]
        if len(source_steps) != 1:
            continue
        step_key = source_steps[0]
        if str(render_roles.get(step_key) or "").strip() != "post_interaction_overlay":
            continue
        rows.append(
            {
                "step_key": step_key,
                "raw_prompt_signature": raw_signature,
                "route_option_target": target,
            }
        )
    if len(rows) <= 1:
        return {}

    rows.sort(key=lambda item: (_step_sort_index(str(item.get("step_key") or "")), str(item.get("step_key") or "")))
    representative_step_key = str(rows[0].get("step_key") or "")
    route_signatures_by_step: Dict[str, List[str]] = {}
    route_targets_by_step: Dict[str, List[str]] = {}
    for row in rows:
        step_key = str(row.get("step_key") or "")
        signature = str(row.get("raw_prompt_signature") or "")
        target = str(row.get("route_option_target") or "")
        if not step_key or not signature:
            continue
        route_signatures_by_step.setdefault(step_key, [])
        if signature not in route_signatures_by_step[step_key]:
            route_signatures_by_step[step_key].append(signature)
        route_targets_by_step.setdefault(step_key, [])
        if target and target not in route_targets_by_step[step_key]:
            route_targets_by_step[step_key].append(target)

    dedupe_by_step: Dict[str, Dict[str, Any]] = {}
    for step_key in route_signatures_by_step:
        is_representative = step_key == representative_step_key
        dedupe_by_step[step_key] = {
            "overlay_route_option_cluster_id": "overlay_route_options:1",
            "prompt_signature": _OVERLAY_ROUTE_OPTION_PROMPT_SIGNATURE,
            "overlay_route_option_dedupe_status": (
                "overlay_route_option_representative"
                if is_representative
                else "overlay_route_option_duplicate"
            ),
            "duplicate_of_step_key": "" if is_representative else representative_step_key,
            "structured_route_option_signatures": sorted(route_signatures_by_step[step_key]),
            "structured_route_option_targets": sorted(route_targets_by_step.get(step_key, [])),
        }
    return dedupe_by_step


def _build_task_carousel_selected_state_dedupe_index(
    obligations: Sequence[Dict[str, Any]],
    *,
    render_roles: Dict[str, Any],
    screenshot_render_annotations: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    annotations = _normalize_step_keyed_mapping(screenshot_render_annotations or {})
    canonical_by_trace: Dict[str, Dict[str, str]] = {}
    selected_rows: List[Dict[str, Any]] = []
    for obligation in obligations:
        signature = str(obligation.get("signature") or "")
        if not signature.startswith("task_carousel_surface|"):
            continue
        source_steps = [
            normalize_step_key(str(step))
            for step in obligation.get("source_step_keys", [])
            if str(step or "").strip()
        ]
        if len(source_steps) != 1:
            continue
        step_key = source_steps[0]
        trace_id = _trace_id_from_step_key(step_key)
        if not trace_id:
            continue
        role = str(render_roles.get(step_key) or "").strip()
        if role == "canonical_layout":
            canonical_by_trace.setdefault(
                trace_id,
                {"step_key": step_key, "prompt_signature": signature},
            )
            continue
        if role != "interaction_state_change":
            continue
        if not _task_carousel_signature_is_step_keyed(signature, step_key):
            continue
        annotation = annotations.get(step_key, {}) if isinstance(annotations.get(step_key, {}), dict) else {}
        if not _is_task_carousel_selected_state_annotation(annotation):
            continue
        selected_rows.append(
            {
                "step_key": step_key,
                "trace_id": trace_id,
                "raw_prompt_signature": signature,
                "structured_task_carousel_state_action": _simple_text(annotation.get("action")),
                "structured_task_carousel_state_reason": _simple_text(annotation.get("reason")),
            }
        )

    dedupe_by_step: Dict[str, Dict[str, Any]] = {}
    for row in selected_rows:
        canonical = canonical_by_trace.get(str(row.get("trace_id") or ""))
        if not canonical:
            continue
        prompt_signature = str(canonical.get("prompt_signature") or "")
        if not prompt_signature:
            continue
        step_key = str(row.get("step_key") or "")
        dedupe_by_step[step_key] = {
            **row,
            "prompt_signature": prompt_signature,
            "task_carousel_selected_state_cluster_id": prompt_signature.split("|", 1)[-1],
            "task_carousel_selected_state_dedupe_status": "task_carousel_selected_state_duplicate",
            "duplicate_of_step_key": str(canonical.get("step_key") or ""),
        }
    return dedupe_by_step


def _collect_l0_scope_obligations(
    page_environment_projection: Optional[Dict[str, Any]],
    render_roles: Dict[str, Any],
    required_interaction_surfaces: Optional[Sequence[Dict[str, Any]]] = None,
    task_evidence_steps: Optional[Sequence[str]] = None,
    screenshot_render_annotations: Optional[Dict[str, Any]] = None,
    candidate_refs: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    def add_unique(item: Dict[str, Any]) -> None:
        key = (
            str(item.get("signature") or ""),
            tuple(item.get("source_step_keys") or []),
            str(item.get("component_id") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        obligations.append(item)

    seen: set = set()
    obligations: List[Dict[str, Any]] = []
    if not isinstance(page_environment_projection, dict):
        components = []
    else:
        components = page_environment_projection.get("components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            if component.get("interaction_required") is not True:
                continue
            role = str(component.get("component_role") or "")
            if "l0_execution_anchor" not in role and not component.get("l0_transition_ids"):
                continue
            source_steps = _component_source_steps(component)
            if not source_steps:
                continue
            contract = component.get("interaction_contract") if isinstance(component.get("interaction_contract"), dict) else {}
            label = _simple_text(component.get("required_display_label") or component.get("display_label") or contract.get("display_label"))
            operation_intent = _simple_text(component.get("operation_intent") or contract.get("operation_intent") or contract.get("action_text"))
            expected_filename = _simple_text(contract.get("expected_filename") or component.get("target_filename"))
            component_kind = _simple_text(component.get("component_kind"))
            surface_group, shell_slot = _surface_group_for_l0_scope(
                label=label,
                operation_intent=operation_intent,
                expected_filename=expected_filename,
                component_kind=component_kind,
            )
            source_render_role = _source_render_role(source_steps, render_roles)
            signature = _l0_surface_signature(
                surface_group=surface_group,
                shell_slot=shell_slot,
                source_render_role=source_render_role,
                label=label,
                operation_intent=operation_intent,
                expected_filename=expected_filename,
                component_kind=component_kind,
            )
            add_unique(
                {
                    "signature": signature,
                    "surface_group": surface_group,
                    "shell_slot": shell_slot,
                    "source_render_role": source_render_role,
                    "source_step_keys": source_steps,
                    "label": label,
                    "operation_intent": operation_intent,
                    "expected_filename": expected_filename,
                    "component_kind": component_kind,
                    "component_id": _simple_text(component.get("component_id")),
                }
            )
    annotations = _normalize_step_keyed_mapping(screenshot_render_annotations or {})
    candidate_steps_by_trace = _candidate_step_keys_by_trace(candidate_refs or [])
    carousel_runtime_traces: Dict[str, int] = {}
    for raw_step_key in task_evidence_steps or []:
        step_key = normalize_step_key(str(raw_step_key))
        if not step_key:
            continue
        source_render_role = _source_render_role([step_key], render_roles)
        if source_render_role != "interaction_state_change":
            continue
        annotation = annotations.get(step_key, {}) if isinstance(annotations.get(step_key, {}), dict) else {}
        carousel_part = _task_carousel_part_key(annotation, fallback=step_key)
        if not carousel_part:
            continue
        trace_id = _trace_id_from_step_key(step_key)
        carousel_runtime_traces[trace_id] = min(
            carousel_runtime_traces.get(trace_id, _step_sort_index(step_key)),
            _step_sort_index(step_key),
        )
        add_unique(
            {
                "signature": f"task_carousel_surface|{carousel_part}",
                "surface_group": "task_carousel_surface",
                "shell_slot": "",
                "source_render_role": source_render_role,
                "source_step_keys": [step_key],
                "label": carousel_part,
                "operation_intent": _simple_text(annotation.get("action")),
                "expected_filename": "",
                "component_kind": "task_carousel_surface",
                "component_id": f"task_carousel_surface::{carousel_part}::{step_key}",
            }
        )
    for trace_id, first_runtime_index in carousel_runtime_traces.items():
        for step_key in candidate_steps_by_trace.get(trace_id, []):
            if _step_sort_index(step_key) >= first_runtime_index:
                break
            source_render_role = _source_render_role([step_key], render_roles)
            if source_render_role != "canonical_layout":
                continue
            annotation = annotations.get(step_key, {}) if isinstance(annotations.get(step_key, {}), dict) else {}
            carousel_part = _task_carousel_part_key(annotation, fallback="grid_page_1")
            if not carousel_part:
                continue
            add_unique(
                {
                    "signature": f"task_carousel_surface|{carousel_part}",
                    "surface_group": "task_carousel_surface",
                    "shell_slot": "",
                    "source_render_role": source_render_role,
                    "source_step_keys": [step_key],
                    "label": carousel_part,
                    "operation_intent": _simple_text(annotation.get("action")),
                    "expected_filename": "",
                    "component_kind": "task_carousel_surface",
                    "component_id": f"task_carousel_surface::{carousel_part}::{step_key}",
                }
            )
            break
    if isinstance(required_interaction_surfaces, list):
        for surface in required_interaction_surfaces:
            if not isinstance(surface, dict):
                continue
            surface_role = str(surface.get("surface_role") or "")
            if surface_role not in {"opened_interaction_surface", "opened_interaction_surface_option"}:
                continue
            source_steps = _collect_step_keys_from_value(surface)
            if not source_steps:
                continue
            family = _simple_text(surface.get("surface_family") or surface.get("opened_surface_id") or "opened_surface")
            add_unique(
                {
                    "signature": f"opened_interaction_surface|{family}",
                    "surface_group": "opened_interaction_surface",
                    "shell_slot": "",
                    "source_render_role": _source_render_role(source_steps, render_roles),
                    "source_step_keys": source_steps,
                    "label": _simple_text(surface.get("required_display_label")),
                    "operation_intent": "",
                    "expected_filename": "",
                    "component_kind": "opened_interaction_surface",
                    "component_id": _simple_text(surface.get("surface_id") or surface.get("component_ref")),
                }
            )
    return obligations


def _candidate_covers_l0_signature(candidate_role: str, candidate_exact_signatures: set, signature: str) -> bool:
    if signature in candidate_exact_signatures:
        return True
    if signature == "visual_surface|query_input_surface":
        return candidate_role == "canonical_layout" and any(
            str(exact).startswith("query_entry_surface|")
            for exact in candidate_exact_signatures
        )
    if signature.startswith((
        "shell_navigation|",
        "shell_create_entry|",
        "query_entry_surface|",
        "location_selector_surface|",
        "utility_surface|",
        "repeated_navigation_surface|",
    )):
        return candidate_role == "canonical_layout"
    return False


def _candidate_selection_reason(
    candidate: Dict[str, Any],
    *,
    selected_refs: set[str],
    selected_covered: set[str],
) -> str:
    override = str(candidate.get("selection_reason_override") or "").strip()
    ref = str(candidate.get("ref") or "")
    covered = set(candidate.get("covered_signatures") or [])
    if ref in selected_refs and override:
        return override
    if ref in selected_refs:
        if not covered:
            return "selected_by_fallback_no_prompt_admission_signatures"
        return "selected_for_prompt_signature_coverage"
    if str(candidate.get("dedupe_status") or "") == "scroll_cluster_duplicate":
        return "rejected_duplicate_scroll_visual_prompt_cluster"
    if str(candidate.get("dedupe_status") or "") == "scroll_layout_pattern_duplicate":
        return "rejected_duplicate_scroll_layout_prompt_cluster"
    if str(candidate.get("overlay_route_option_dedupe_status") or "") == "overlay_route_option_duplicate":
        return "rejected_duplicate_overlay_route_option_prompt_cluster"
    if str(candidate.get("task_carousel_selected_state_dedupe_status") or "") == "task_carousel_selected_state_duplicate":
        return "rejected_duplicate_task_carousel_selected_state_prompt_cluster"
    if not covered:
        return "rejected_no_prompt_admission_signature"
    if covered <= selected_covered:
        return "rejected_covered_by_selected_prompt_candidates"
    return "rejected_outside_minimum_visual_basis"


def _materialize_l0_candidate_universe(
    candidates: Sequence[Dict[str, Any]],
    selected: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    selected_refs = {str(item.get("ref") or "") for item in selected if str(item.get("ref") or "")}
    selected_covered: set[str] = set()
    for item in selected:
        selected_covered.update(str(sig) for sig in item.get("covered_signatures", []) if str(sig or ""))
    universe: List[Dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        ref = str(item.get("ref") or "")
        item["selection_status"] = "selected" if ref in selected_refs else "rejected"
        item["selection_reason"] = _candidate_selection_reason(
            item,
            selected_refs=selected_refs,
            selected_covered=selected_covered,
        )
        item["prompt_admission_signature_count"] = len(item.get("covered_signatures") or [])
        universe.append(item)
    return universe


def _l0_axis_kind(raw_signature: str, normalized_signature: str) -> str:
    raw = str(raw_signature or "")
    normalized = str(normalized_signature or raw)
    if raw.startswith("initial_visual_authority|"):
        return "initial_visual_authority"
    if raw.startswith("opened_interaction_surface|route_options."):
        return "route_target"
    if raw.startswith("task_carousel_surface|") and raw != normalized:
        if "scroll_layout_cluster:" in normalized or "scroll_cluster:" in normalized:
            return "scroll_offset"
        return "selected_state"
    if raw.startswith("visual_surface|query_input_surface") or raw.startswith("query_entry_surface|"):
        return "query_filter_value"
    if raw.startswith("location_selector_surface|"):
        return "location_value"
    if raw.startswith("opened_interaction_surface|"):
        return "overlay_topology"
    if raw.startswith("task_carousel_surface|"):
        return "visual_topology"
    if raw.startswith(("shell_navigation|", "shell_create_entry|", "utility_surface|")):
        return "shell_topology"
    return "visual_topology"


def _l0_structured_payload_for_signature(raw_signature: str, axis_kind: str) -> Dict[str, Any]:
    raw = str(raw_signature or "")
    if axis_kind == "route_target":
        target = _overlay_route_option_signature_target(raw)
        return {"route_option_target": target} if target else {}
    if axis_kind in {"scroll_offset", "selected_state", "query_filter_value", "location_value"}:
        return {"raw_signature": raw}
    return {}


def _build_normalized_l0_obligations(
    *,
    obligations: Sequence[Dict[str, Any]],
    visual_sigs: Dict[str, List[Dict[str, Any]]],
    candidate_step_keys: set[str],
    layout_shell_prefixes: Sequence[str],
    rewrite_prompt_signature: Any,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    rows: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, Tuple[str, ...]]] = set()

    def add(raw_signature: str, source_steps: Sequence[str], source: str) -> None:
        raw = str(raw_signature or "")
        if not raw:
            return
        normalized = str(rewrite_prompt_signature(raw) or raw)
        steps = tuple(_dedupe_keep_order(normalize_step_key(str(step)) for step in source_steps if str(step or "").strip()))
        key = (raw, normalized, steps)
        if key in seen:
            return
        seen.add(key)
        axis_kind = _l0_axis_kind(raw, normalized)
        obligation_id = f"l0_obligation:{len(rows) + 1}"
        rows.append(
            {
                "obligation_id": obligation_id,
                "raw_signature": raw,
                "normalized_signature": normalized,
                "axis_kind": axis_kind,
                "source_step_keys": list(steps),
                "structured_payload": _l0_structured_payload_for_signature(raw, axis_kind),
                "visual_authority_required": axis_kind in {
                    "initial_visual_authority",
                    "visual_topology",
                    "shell_topology",
                    "overlay_topology",
                    "query_filter_value",
                    "location_value",
                },
                "structured_payload_required": axis_kind in {
                    "route_target",
                    "scroll_offset",
                    "selected_state",
                    "query_filter_value",
                    "location_value",
                },
                "source": source,
                "compression_status": (
                    "normalized_to_visual_basis" if raw != normalized else "visual_basis_signature"
                ),
            }
        )

    for obligation in obligations:
        sig = str(obligation.get("signature") or "")
        if not sig:
            continue
        if visual_sigs and not any(sig.startswith(prefix) for prefix in layout_shell_prefixes):
            continue
        add(sig, obligation.get("source_step_keys") or [], "l0_obligation")

    if visual_sigs:
        for step_key, sigs in visual_sigs.items():
            normalized_step_key = normalize_step_key(str(step_key))
            if normalized_step_key not in candidate_step_keys:
                continue
            for sig in sigs or []:
                if isinstance(sig, dict):
                    add(str(sig.get("signature") or ""), [normalized_step_key], "l0_visual_surface_signature")

    ids_by_normalized_signature: Dict[str, List[str]] = {}
    for row in rows:
        normalized = str(row.get("normalized_signature") or "")
        if normalized:
            ids_by_normalized_signature.setdefault(normalized, []).append(str(row["obligation_id"]))
    return rows, ids_by_normalized_signature


def _l0_reduction_metadata_for_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    raw_signatures = [str(sig) for sig in candidate.get("raw_exact_signatures") or [] if str(sig or "")]
    normalized_signatures = [str(sig) for sig in candidate.get("exact_signatures") or [] if str(sig or "")]
    axis_kinds = sorted(
        {
            _l0_axis_kind(raw, str(candidate.get("prompt_signature") or "") or raw)
            for raw in raw_signatures
        }
    )
    status = "not_reduced"
    if any(
        candidate.get(key)
        for key in (
            "dedupe_status",
            "overlay_route_option_dedupe_status",
            "task_carousel_selected_state_dedupe_status",
        )
    ):
        status = "representative_or_duplicate"
    if raw_signatures and set(raw_signatures) != set(normalized_signatures):
        status = "normalized_to_visual_basis"
    return {
        "reduction_status": status,
        "axis_kinds": axis_kinds or sorted({_l0_axis_kind(sig, sig) for sig in normalized_signatures}),
        "raw_signatures": raw_signatures,
        "normalized_signatures": normalized_signatures,
        "representative_step_key": str(candidate.get("duplicate_of_step_key") or candidate.get("step_key") or ""),
    }


def _candidate_exact_cover_priority(candidate: Dict[str, Any]) -> Tuple[Any, ...]:
    covered = set(candidate.get("covered_signatures") or [])
    return (
        -len(covered),
        0 if candidate.get("render_role") == "canonical_layout" else 1,
        0 if candidate.get("dedupe_status") not in {"scroll_cluster_duplicate", "scroll_layout_pattern_duplicate"} else 1,
        0 if candidate.get("overlay_route_option_dedupe_status") != "overlay_route_option_duplicate" else 1,
        0 if candidate.get("task_carousel_selected_state_dedupe_status") != "task_carousel_selected_state_duplicate" else 1,
        0 if any(str(sig).startswith(("shell_navigation|", "shell_create_entry|")) for sig in covered) else 1,
        _step_sort_index(str(candidate.get("step_key") or "")),
        str(candidate.get("ref") or ""),
    )


def _select_minimum_l0_visual_basis(
    candidates: Sequence[Dict[str, Any]],
    all_signatures: set[str],
) -> Tuple[List[Dict[str, Any]], set[str], Dict[str, Any]]:
    if not all_signatures:
        return [], set(), {"solver": "exact_minimum_cover", "exact_cover_applied": True}
    signature_list = sorted(str(sig) for sig in all_signatures if str(sig or ""))
    signature_index = {signature: index for index, signature in enumerate(signature_list)}
    full_mask = (1 << len(signature_list)) - 1
    ranked = sorted(
        [candidate for candidate in candidates if candidate.get("covered_signatures")],
        key=_candidate_exact_cover_priority,
    )
    candidate_masks: List[int] = []
    for candidate in ranked:
        mask = 0
        for signature in candidate.get("covered_signatures") or []:
            index = signature_index.get(str(signature))
            if index is not None:
                mask |= 1 << index
        candidate_masks.append(mask)

    union_mask = 0
    for mask in candidate_masks:
        union_mask |= mask
    if union_mask != full_mask:
        uncovered = {
            signature
            for signature, index in signature_index.items()
            if not (union_mask & (1 << index))
        }
        return [], uncovered, {
            "solver": "exact_minimum_cover",
            "exact_cover_applied": False,
            "reason": "candidate_union_does_not_cover_all_signatures",
        }

    dp: Dict[int, Tuple[int, ...]] = {0: ()}
    for index, mask in enumerate(candidate_masks):
        if mask == 0:
            continue
        updates: Dict[int, Tuple[int, ...]] = {}
        for existing_mask, solution in list(dp.items()):
            new_mask = existing_mask | mask
            new_solution = solution + (index,)
            old_solution = dp.get(new_mask)
            pending_solution = updates.get(new_mask)
            current_best = pending_solution if pending_solution is not None else old_solution
            if current_best is None or (len(new_solution), new_solution) < (len(current_best), current_best):
                updates[new_mask] = new_solution
        dp.update(updates)

    solution_indices = dp.get(full_mask)
    if solution_indices is None:
        return [], set(all_signatures), {
            "solver": "exact_minimum_cover",
            "exact_cover_applied": False,
            "reason": "no_exact_solution_found",
        }
    selected = [ranked[index] for index in solution_indices]
    return selected, set(), {
        "solver": "exact_minimum_cover",
        "exact_cover_applied": True,
        "candidate_count_considered": len(ranked),
        "signature_count": len(signature_list),
        "minimum_selected_count": len(selected),
    }


def _build_l0_related_runtime_evidence(
    *,
    task_evidence_steps: Sequence[str],
    refs_by_step: Dict[str, str],
    render_roles: Dict[str, Any],
    screenshot_render_annotations: Dict[str, Any],
    visual_surface_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    state_delta_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    selected_step_keys: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    annotations = _normalize_step_keyed_mapping(screenshot_render_annotations or {})
    visual_sigs = visual_surface_signatures if isinstance(visual_surface_signatures, dict) else {}
    state_sigs = state_delta_signatures if isinstance(state_delta_signatures, dict) else {}
    selected_steps = {
        normalize_step_key(str(step))
        for step in (selected_step_keys or [])
        if str(step or "").strip()
    }
    evidence: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for raw_step_key in task_evidence_steps:
        step_key = normalize_step_key(str(raw_step_key))
        if not step_key:
            continue
        role = str(render_roles.get(step_key) or "").strip()
        annotation = annotations.get(step_key, {}) if isinstance(annotations.get(step_key, {}), dict) else {}
        families: List[Tuple[str, str]] = []
        carousel_part = _task_carousel_part_key(annotation, fallback=step_key)
        if carousel_part:
            families.append(("task_carousel_surface", carousel_part))
        if state_sigs.get(step_key):
            families.append(("state_delta_surface", "trace_state_delta"))
        if visual_sigs.get(step_key):
            families.append(("visual_surface", "trace_visual_surface"))
        if role and role != "canonical_layout" and not families:
            families.append(("runtime_state_surface", role))
        for family, label in families:
            key = (step_key, family)
            if key in seen:
                continue
            seen.add(key)
            item: Dict[str, Any] = {
                "step_key": step_key,
                "screenshot_ref": refs_by_step.get(step_key, ""),
                "render_role": role,
                "evidence_family": family,
                "evidence_label": label,
                "prompt_admitted": step_key in selected_steps,
            }
            if annotation.get("action"):
                item["action"] = _simple_text(annotation.get("action"))
            if annotation.get("reason"):
                item["reason"] = _simple_text(annotation.get("reason"))
            evidence.append({k: v for k, v in item.items() if v not in (None, "", [], {})})
    return evidence


def _ensure_l0_initial_layout_authority(
    *,
    selected_refs: Sequence[str],
    candidate_refs: Sequence[str],
    render_roles: Dict[str, Any],
    render_lifecycle: Optional[Dict[str, Dict[str, Any]]] = None,
    initial_render_mode: str,
) -> Tuple[List[str], Dict[str, Any]]:
    selected = _dedupe_keep_order(str(ref) for ref in selected_refs if str(ref or ""))
    if not selected or initial_render_mode == "spec_only_initial":
        return selected, {
            "enabled": False,
            "reason": "no_prompt_screenshots_or_spec_only_initial",
        }

    def role_for_ref(ref: str) -> str:
        step_key = step_key_from_screenshot_ref(str(ref)) or ""
        if not step_key:
            return ""
        return str(render_roles.get(normalize_step_key(step_key)) or "").strip()

    lifecycle_by_step = render_lifecycle or {}

    def initial_eligible_for_ref(ref: str) -> bool:
        step_key = normalize_step_key(step_key_from_screenshot_ref(str(ref)) or "")
        lifecycle = lifecycle_by_step.get(step_key) if step_key else None
        if isinstance(lifecycle, dict) and lifecycle.get("initial_dom_eligible") is False:
            return False
        return True

    for ref in selected:
        if role_for_ref(ref) == "canonical_layout" and initial_eligible_for_ref(ref):
            return selected, {
                "enabled": True,
                "action": "already_selected",
                "canonical_layout_ref": ref,
                "canonical_step_key": normalize_step_key(step_key_from_screenshot_ref(ref) or ""),
            }

    for ref in _dedupe_keep_order(str(ref) for ref in candidate_refs if str(ref or "")):
        if role_for_ref(ref) != "canonical_layout" or not initial_eligible_for_ref(ref):
            continue
        return _dedupe_keep_order([ref, *selected]), {
            "enabled": True,
            "action": "prepended_canonical_layout",
            "canonical_layout_ref": ref,
            "canonical_step_key": normalize_step_key(step_key_from_screenshot_ref(ref) or ""),
        }

    return selected, {
        "enabled": True,
        "action": "missing_canonical_layout",
        "candidate_count": len(_dedupe_keep_order(str(ref) for ref in candidate_refs if str(ref or ""))),
    }


def _projection_has_semantic_initial_authority(projection: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(projection, dict):
        return False
    components = projection.get("components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            render_phase = str(component.get("render_phase") or "").strip()
            host_phase = str(component.get("host_availability_phase") or "").strip()
            if render_phase == "initial" or host_phase == "initial":
                return True
            if component.get("initial_dom_eligible") is True:
                return True
    initial_state = projection.get("initial_state")
    if isinstance(initial_state, dict):
        for value in initial_state.values():
            if value not in (None, "", [], {}):
                return True
    return False


def _select_l0_scope_refs_by_surface_coverage(
    *,
    base_scope: Dict[str, Any],
    page_environment_projection: Optional[Dict[str, Any]],
    required_interaction_surfaces: Optional[Sequence[Dict[str, Any]]] = None,
    task_evidence_steps: Optional[Sequence[str]] = None,
    visual_surface_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    state_delta_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    render_lifecycle: Optional[Dict[str, Dict[str, Any]]] = None,
    initial_render_mode: str = "screenshot_initial",
) -> Tuple[List[str], Dict[str, Any]]:
    full_refs = _dedupe_keep_order(list(base_scope.get("page_screenshot_refs") or []))
    unique_refs = _dedupe_keep_order(list(base_scope.get("unique_prompt_screenshot_refs") or []) or list(base_scope.get("prompt_screenshot_refs") or []))
    all_refs = _dedupe_keep_order(full_refs + unique_refs + list(base_scope.get("prompt_screenshot_refs") or []))
    render_roles = _normalize_step_keyed_mapping(base_scope.get("screenshot_render_roles", {}))
    obligations = _collect_l0_scope_obligations(
        page_environment_projection,
        render_roles,
        required_interaction_surfaces,
        task_evidence_steps=task_evidence_steps,
        screenshot_render_annotations=base_scope.get("screenshot_render_annotations", {}),
        candidate_refs=all_refs,
    )
    if not obligations or not all_refs:
        selected = _dedupe_keep_order(unique_refs or full_refs)
        return selected, {
            "enabled": False,
            "reason": "missing_l0_obligation_projection_or_refs",
            "selected_screenshot_refs": selected,
        }

    obligations_by_step: Dict[str, List[Dict[str, Any]]] = {}
    for obligation in obligations:
        for step_key in obligation.get("source_step_keys", []):
            obligations_by_step.setdefault(normalize_step_key(str(step_key)), []).append(obligation)
    visual_sigs = visual_surface_signatures if isinstance(visual_surface_signatures, dict) else {}
    state_sigs = state_delta_signatures if isinstance(state_delta_signatures, dict) else {}
    refs_by_step = {
        normalize_step_key(step_key_from_screenshot_ref(str(ref)) or ""): str(ref)
        for ref in all_refs
        if step_key_from_screenshot_ref(str(ref))
    }
    lifecycle_by_step = render_lifecycle or {}

    def initial_eligible_step(step_key: str, role: str) -> bool:
        if role != "canonical_layout":
            return False
        lifecycle = lifecycle_by_step.get(normalize_step_key(step_key))
        if isinstance(lifecycle, dict) and lifecycle.get("initial_dom_eligible") is False:
            return False
        return True

    initial_authority_signature = ""
    if initial_render_mode != "spec_only_initial":
        for step_key in refs_by_step:
            role = str(render_roles.get(step_key) or "").strip()
            if initial_eligible_step(step_key, role):
                initial_authority_signature = "initial_visual_authority|runtime_page_initial"
                break
    scroll_dedupe_by_step = _build_scroll_visual_dedupe_index(
        obligations,
        refs_by_step=refs_by_step,
        render_roles=render_roles,
        screenshot_render_annotations=base_scope.get("screenshot_render_annotations", {}),
    )
    overlay_route_option_dedupe_by_step = _build_overlay_route_option_dedupe_index(
        obligations,
        render_roles=render_roles,
    )
    task_carousel_selected_state_dedupe_by_step = _build_task_carousel_selected_state_dedupe_index(
        obligations,
        render_roles=render_roles,
        screenshot_render_annotations=base_scope.get("screenshot_render_annotations", {}),
    )


    signature_rewrites = {
        str(item.get("raw_prompt_signature") or ""): str(item.get("prompt_signature") or "")
        for item in scroll_dedupe_by_step.values()
        if str(item.get("raw_prompt_signature") or "") and str(item.get("prompt_signature") or "")
    }
    signature_rewrites.update(
        {
            str(item.get("raw_prompt_signature") or ""): str(item.get("prompt_signature") or "")
            for item in task_carousel_selected_state_dedupe_by_step.values()
            if str(item.get("raw_prompt_signature") or "") and str(item.get("prompt_signature") or "")
        }
    )
    for step_key, selected_state in task_carousel_selected_state_dedupe_by_step.items():
        replacement_signature = str(selected_state.get("prompt_signature") or "")
        scroll_meta = scroll_dedupe_by_step.get(step_key)
        replaced_scroll_signature = str(scroll_meta.get("prompt_signature") or "") if scroll_meta else ""
        if not replacement_signature or not replaced_scroll_signature:
            continue
        signature_rewrites[replaced_scroll_signature] = replacement_signature
        for item in scroll_dedupe_by_step.values():
            if str(item.get("prompt_signature") or "") != replaced_scroll_signature:
                continue
            raw_signature = str(item.get("raw_prompt_signature") or "")
            if raw_signature:
                signature_rewrites[raw_signature] = replacement_signature
            item["visual_scroll_cluster_id"] = str(item.get("scroll_cluster_id") or "")
            item["visual_scroll_prompt_signature"] = replaced_scroll_signature
            item["scroll_cluster_id"] = str(selected_state.get("task_carousel_selected_state_cluster_id") or "")
            item["prompt_signature"] = replacement_signature
            item["task_carousel_selected_state_cluster_id"] = str(
                selected_state.get("task_carousel_selected_state_cluster_id") or ""
            )
            item["task_carousel_selected_state_dedupe_status"] = (
                "task_carousel_selected_state_duplicate"
            )
            item["duplicate_of_step_key"] = str(selected_state.get("duplicate_of_step_key") or "")
    for item in overlay_route_option_dedupe_by_step.values():
        prompt_signature = str(item.get("prompt_signature") or "")
        if not prompt_signature:
            continue
        for raw_signature in item.get("structured_route_option_signatures", []) or []:
            raw = str(raw_signature or "")
            if raw:
                signature_rewrites[raw] = prompt_signature

    def rewrite_prompt_signature(signature: str) -> str:
        return signature_rewrites.get(signature, signature)

    _LAYOUT_SHELL_PREFIXES = (
        "shell_navigation|", "shell_create_entry|", "query_entry_surface|",
        "location_selector_surface|", "utility_surface|",
        "opened_interaction_surface|", "task_carousel_surface|",
    )
    candidate_step_keys = {
        normalize_step_key(step_key_from_screenshot_ref(str(ref)) or "")
        for ref in all_refs
        if step_key_from_screenshot_ref(str(ref))
    }
    normalized_l0_obligations, obligation_ids_by_signature = _build_normalized_l0_obligations(
        obligations=obligations,
        visual_sigs=visual_sigs,
        candidate_step_keys=candidate_step_keys,
        layout_shell_prefixes=_LAYOUT_SHELL_PREFIXES,
        rewrite_prompt_signature=rewrite_prompt_signature,
    )
    if initial_authority_signature:
        obligation_id = f"l0_obligation:{len(normalized_l0_obligations) + 1}"
        normalized_l0_obligations.append(
            {
                "obligation_id": obligation_id,
                "raw_signature": initial_authority_signature,
                "normalized_signature": initial_authority_signature,
                "axis_kind": "initial_visual_authority",
                "source_step_keys": [],
                "structured_payload": {},
                "visual_authority_required": True,
                "structured_payload_required": False,
                "source": "initial_visual_authority",
                "compression_status": "visual_basis_signature",
            }
        )
        obligation_ids_by_signature.setdefault(initial_authority_signature, []).append(obligation_id)

    if visual_sigs:
        all_signatures: set[str] = set()
        for item in obligations:
            sig = str(item.get("signature") or "")
            if sig and any(sig.startswith(p) for p in _LAYOUT_SHELL_PREFIXES):
                all_signatures.add(rewrite_prompt_signature(sig))
        for step_key, sigs in visual_sigs.items():
            if normalize_step_key(str(step_key)) not in candidate_step_keys:
                continue
            for sig in sigs:
                s = str(sig.get("signature") or "")
                if s:
                    all_signatures.add(rewrite_prompt_signature(s))
    else:
        all_signatures = {
            rewrite_prompt_signature(str(item.get("signature") or ""))
            for item in obligations
            if item.get("signature")
        }
    if initial_authority_signature:
        all_signatures.add(initial_authority_signature)

    candidates: List[Dict[str, Any]] = []
    for ref in all_refs:
        step_key = step_key_from_screenshot_ref(str(ref)) or ""
        step_key = normalize_step_key(step_key) if step_key else ""
        role = str(render_roles.get(step_key) or "").strip()
        exact_obligations = obligations_by_step.get(step_key, [])
        raw_exact_signatures: set[str] = set()
        if visual_sigs:
            exact_signatures: set[str] = set()
            for item in exact_obligations:
                sig = str(item.get("signature") or "")
                if sig and any(sig.startswith(p) for p in _LAYOUT_SHELL_PREFIXES):
                    raw_exact_signatures.add(sig)
                    exact_signatures.add(rewrite_prompt_signature(sig))
            for sig in visual_sigs.get(step_key, []):
                s = str(sig.get("signature") or "")
                if s:
                    raw_exact_signatures.add(s)
                    exact_signatures.add(rewrite_prompt_signature(s))
        else:
            raw_exact_signatures = {
                str(item.get("signature") or "")
                for item in exact_obligations
                if item.get("signature")
            }
            exact_signatures = {rewrite_prompt_signature(sig) for sig in raw_exact_signatures}
        if initial_authority_signature and initial_eligible_step(step_key, role):
            raw_exact_signatures.add(initial_authority_signature)
            exact_signatures.add(initial_authority_signature)
        covered = {
            signature
            for signature in all_signatures
            if _candidate_covers_l0_signature(role, exact_signatures, signature)
        }
        has_state_delta = bool(state_sigs.get(step_key))
        has_visual = bool(visual_sigs.get(step_key)) if visual_sigs else None
        candidate_entry: Dict[str, Any] = {
            "ref": ref,
            "step_key": step_key,
            "render_role": role,
            "exact_signatures": sorted(exact_signatures),
            "covered_signatures": sorted(covered),
            "exact_obligation_count": len(exact_obligations),
        }
        if raw_exact_signatures and raw_exact_signatures != exact_signatures:
            candidate_entry["raw_exact_signatures"] = sorted(raw_exact_signatures)
        scroll_meta = scroll_dedupe_by_step.get(step_key)
        if scroll_meta:
            candidate_entry.update(
                {
                    key: value
                    for key, value in scroll_meta.items()
                    if key not in {"ref"} and value not in (None, "", [], {})
                }
            )
        overlay_route_option_meta = overlay_route_option_dedupe_by_step.get(step_key)
        if overlay_route_option_meta:
            candidate_entry.update(
                {
                    key: value
                    for key, value in overlay_route_option_meta.items()
                    if value not in (None, "", [], {})
                }
            )
        task_carousel_selected_state_meta = task_carousel_selected_state_dedupe_by_step.get(step_key)
        if task_carousel_selected_state_meta:
            candidate_entry.update(
                {
                    key: value
                    for key, value in task_carousel_selected_state_meta.items()
                    if value not in (None, "", [], {})
                }
            )
        if has_state_delta:
            candidate_entry["has_state_delta"] = True
        if has_visual is not None:
            candidate_entry["has_visual_surface"] = has_visual
        covered_obligation_ids: List[str] = []
        for signature in sorted(covered):
            covered_obligation_ids.extend(obligation_ids_by_signature.get(signature, []))
        if covered_obligation_ids:
            candidate_entry["covered_l0_obligation_ids"] = _dedupe_keep_order(covered_obligation_ids)
        candidate_entry["l0_visual_basis_reduction"] = _l0_reduction_metadata_for_candidate(candidate_entry)
        candidates.append(candidate_entry)

    selected, uncovered, solver_summary = _select_minimum_l0_visual_basis(candidates, set(all_signatures))

    selected_refs = [str(item.get("ref") or "") for item in selected if str(item.get("ref") or "")]
    if not selected_refs:
        selected_refs = _dedupe_keep_order(unique_refs or full_refs)
        selected_ref_set = set(selected_refs)
        selected = [
            {
                **candidate,
                "selection_reason_override": "selected_by_fallback_no_prompt_admission_signatures",
            }
            for candidate in candidates
            if str(candidate.get("ref") or "") in selected_ref_set
        ]
    excluded_state_delta_only = [
        c for c in candidates
        if c["ref"] not in {s["ref"] for s in selected}
        and c.get("has_state_delta")
        and not c.get("has_visual_surface")
    ]
    candidate_universe = _materialize_l0_candidate_universe(candidates, selected)
    selected_candidates = [
        item
        for item in candidate_universe
        if item.get("selection_status") == "selected"
    ]

    return selected_refs, {
        "enabled": True,
        "selection_policy": "l0_visual_surface_set_cover" if visual_sigs else "l0_obligation_surface_set_cover",
        "obligation_count": len(obligations),
        "obligation_signature_count": len(all_signatures),
        "covered_signature_count": len(all_signatures - uncovered),
        "uncovered_signature_count": len(uncovered),
        "uncovered_signatures": sorted(uncovered),
        "normalized_l0_obligations": normalized_l0_obligations,
        "normalized_l0_obligation_count": len(normalized_l0_obligations),
        "coverage_solver": solver_summary,
        "selected_candidates": selected_candidates,
        "candidate_count": len(candidates),
        "candidate_universe": candidate_universe,
        "candidate_universe_count": len(candidate_universe),
        "excluded_state_delta_only_count": len(excluded_state_delta_only),
        "visual_surface_input_provided": bool(visual_sigs),
        "state_delta_input_provided": bool(state_sigs),
    }


def build_standard_unique_render_scope(page_input: Dict[str, Any]) -> Dict[str, Any]:
    base_scope = normalize_page_input_scope(page_input)
    full_refs = list(base_scope.get("page_screenshot_refs", []))
    unique_refs = list(base_scope.get("unique_prompt_screenshot_refs", []))
    if not unique_refs or set(unique_refs) == set(full_refs):
        selected_refs = list(full_refs)
    else:
        selected_refs = _dedupe_keep_order(unique_refs)
    selected_set = set(selected_refs)
    scope = dict(base_scope)
    scope["fidelity_mode"] = "standard"
    scope["full_page_screenshot_refs"] = _dedupe_keep_order(full_refs)
    scope["page_screenshot_refs"] = selected_refs
    scope["prompt_screenshot_refs"] = selected_refs
    scope["unique_prompt_screenshot_refs"] = _dedupe_keep_order(unique_refs)
    scope["non_prompt_page_screenshot_refs"] = []
    scope["excluded_page_screenshot_refs"] = [ref for ref in full_refs if ref not in selected_set]
    scope["render_scope_contract"] = {
        "format": "render_scope_contract",
        "fidelity_mode": "standard",
        "selected_screenshot_refs": selected_refs,
        "full_page_screenshot_count": len(_dedupe_keep_order(full_refs)),
        "selected_screenshot_count": len(selected_refs),
        "excluded_screenshot_count": len([ref for ref in full_refs if ref not in selected_set]),
    }
    return _attach_screenshot_lifecycle_projection(scope)


def build_l0_render_scope(
    page_input: Dict[str, Any],
    *,
    page_environment_projection: Optional[Dict[str, Any]] = None,
    required_interaction_surfaces: Optional[Sequence[Dict[str, Any]]] = None,
    visual_surface_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    state_delta_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    base_scope = normalize_page_input_scope(page_input)
    full_refs = list(base_scope.get("page_screenshot_refs", []))
    unique_refs = list(base_scope.get("unique_prompt_screenshot_refs", []) or base_scope.get("prompt_screenshot_refs", []))

    task_evidence_steps = _collect_task_related_evidence_steps(page_input)
    initial_render_mode = str(base_scope.get("initial_render_mode") or "screenshot_initial")
    render_lifecycle = _render_lifecycle_for_scope(base_scope)

    selected_refs, surface_coverage_selection = _select_l0_scope_refs_by_surface_coverage(
        base_scope=base_scope,
        page_environment_projection=page_environment_projection,
        required_interaction_surfaces=required_interaction_surfaces,
        task_evidence_steps=task_evidence_steps,
        visual_surface_signatures=visual_surface_signatures,
        state_delta_signatures=state_delta_signatures,
        render_lifecycle=render_lifecycle,
        initial_render_mode=initial_render_mode,
    )
    selected_refs, initial_authority_selection = _ensure_l0_initial_layout_authority(
        selected_refs=selected_refs,
        candidate_refs=_dedupe_keep_order(full_refs + unique_refs + list(base_scope.get("prompt_screenshot_refs") or [])),
        render_roles=_normalize_step_keyed_mapping(base_scope.get("screenshot_render_roles", {})),
        render_lifecycle=render_lifecycle,
        initial_render_mode=initial_render_mode,
    )
    if (
        initial_authority_selection.get("action") == "missing_canonical_layout"
        and _projection_has_semantic_initial_authority(page_environment_projection)
    ):
        initial_render_mode = "spec_only_initial"
        initial_authority_selection = {
            **initial_authority_selection,
            "action": "spec_only_initial_from_semantic_projection",
            "semantic_projection_available": True,
        }
    refs_by_step: Dict[str, str] = {}
    for ref in _dedupe_keep_order(full_refs + unique_refs + list(base_scope.get("prompt_screenshot_refs") or [])):
        step_key = normalize_step_key(step_key_from_screenshot_ref(str(ref)) or "")
        if step_key:
            refs_by_step.setdefault(step_key, str(ref))
    selected_step_keys = [
        normalize_step_key(step_key_from_screenshot_ref(str(ref)) or "")
        for ref in selected_refs
        if step_key_from_screenshot_ref(str(ref))
    ]
    l0_related_runtime_evidence = _build_l0_related_runtime_evidence(
        task_evidence_steps=task_evidence_steps,
        refs_by_step=refs_by_step,
        render_roles=_normalize_step_keyed_mapping(base_scope.get("screenshot_render_roles", {})),
        screenshot_render_annotations=base_scope.get("screenshot_render_annotations", {}),
        visual_surface_signatures=visual_surface_signatures,
        state_delta_signatures=state_delta_signatures,
        selected_step_keys=selected_step_keys,
    )
    task_critical_refs: List[str] = []
    for step_key in task_evidence_steps:
        ref = refs_by_step.get(normalize_step_key(step_key))
        if ref:
            task_critical_refs.append(ref)
    selected_set = set(selected_refs)
    task_extra_refs = [
        ref
        for ref in _dedupe_keep_order(task_critical_refs)
        if ref not in selected_set
    ]

    l0_scope = dict(base_scope)
    l0_scope["fidelity_mode"] = "l0"
    l0_scope["initial_render_mode"] = initial_render_mode
    l0_scope["full_page_screenshot_refs"] = full_refs


    l0_scope["resource_screenshot_refs"] = _dedupe_keep_order(
        base_scope.get("resource_screenshot_refs") or full_refs
    )
    l0_scope["page_screenshot_refs"] = selected_refs
    l0_scope["prompt_screenshot_refs"] = selected_refs
    l0_scope["unique_prompt_screenshot_refs"] = selected_refs
    l0_scope["task_extra_screenshot_refs"] = task_extra_refs
    l0_scope["task_critical_content_steps"] = task_evidence_steps
    l0_scope["l0_related_runtime_evidence"] = l0_related_runtime_evidence
    l0_scope["l0_visual_prompt_evidence"] = [
        item
        for item in surface_coverage_selection.get("candidate_universe", [])
        if item.get("selection_status") == "selected"
    ]
    entity_projection_contract = _build_entity_projection_contract(page_input)
    if entity_projection_contract:
        l0_scope["entity_projection_contract"] = entity_projection_contract
    l0_scope["non_prompt_page_screenshot_refs"] = []
    l0_scope["excluded_page_screenshot_refs"] = [ref for ref in full_refs if ref not in selected_set]
    l0_scope["screenshot_render_roles"] = page_input.get("screenshot_render_roles", {})
    l0_scope["screenshot_render_annotations"] = page_input.get("screenshot_render_annotations", {})
    l0_scope["screenshot_render_lifecycle"] = page_input.get("screenshot_render_lifecycle", {})
    l0_scope["initial_authority_kind_by_step"] = page_input.get("initial_authority_kind_by_step", {})
    source_host_visual_authority = _build_source_host_visual_authority_screenshots(
        refs_by_step=refs_by_step,
        screenshot_lifecycle=_normalize_step_keyed_mapping(page_input.get("screenshot_render_lifecycle", {})),
        required_interaction_surfaces=required_interaction_surfaces,
        selected_step_keys=selected_step_keys,
    )
    l0_scope["render_scope_contract"] = {
        "format": "render_scope_contract",
        "fidelity_mode": "l0",
        "selected_screenshot_refs": selected_refs,
        "full_page_screenshot_count": len(_dedupe_keep_order(full_refs)),
        "selected_screenshot_count": len(selected_refs),
        "excluded_screenshot_count": len([ref for ref in full_refs if ref not in selected_set]),
        "task_evidence_steps": task_evidence_steps,
        "task_critical_visual_evidence_refs": task_extra_refs,
        "task_critical_visual_admission_policy": "textual_entity_contract_only_not_prompt_screenshot",
        "l0_related_runtime_evidence_count": len(l0_related_runtime_evidence),
        "l0_visual_prompt_evidence_count": len(l0_scope["l0_visual_prompt_evidence"]),
        "surface_coverage_selection": surface_coverage_selection,
        "initial_authority_selection": initial_authority_selection,
        "rules": [
            "L0 screenshots are selected by obligation surface coverage, not action-level or structure-only uniqueness.",
            "Task-critical entity evidence steps remain textual/entity obligations unless they also cover an L0 surface; they cannot bypass L0 screenshot set-cover.",
            "A screenshot selected for runtime or transition coverage cannot replace canonical initial layout authority.",
            "Resources may come from producer-owned resource_screenshot_refs for this materialized page even when their redundant source screenshot is excluded from the visual prompt.",
            "Non-prompt resources retain source-step lifecycle and may enter initial DOM only from canonical_layout evidence.",
            "Do not synthesize fallback records or append text-only rows when no selected screenshot/resource supports them.",
        ],
    }
    attached_scope = _attach_screenshot_lifecycle_projection(l0_scope)
    if source_host_visual_authority:
        attached_scope["source_host_visual_authority_screenshots"] = source_host_visual_authority
        initial_layout_step_keys = [
            str(item.get("step_key") or "")
            for item in attached_scope.get("initial_layout_screenshots", [])
            if isinstance(item, dict) and str(item.get("step_key") or "").strip()
        ]
        attached_scope["source_host_visual_authority_issues"] = [
            {
                "code": "page_global_initial_vs_trace_page_l0_initial_conflict",
                "surface_id": str(item.get("surface_id") or ""),
                "step_key": str(item.get("step_key") or ""),
                "required_display_label": str(item.get("required_display_label") or ""),
                "initial_layout_step_keys": initial_layout_step_keys,
            }
            for item in source_host_visual_authority
        ]
    return attached_scope


def _build_ref_lookup(refs: Sequence[str]) -> List[Tuple[str, str, str]]:
    lookup: List[Tuple[str, str, str]] = []
    for ref in refs:
        step_key = step_key_from_screenshot_ref(ref)
        if not step_key:
            continue
        for path_key in resource_path_keys_from_screenshot_ref(ref):
            lookup.append((path_key, ref, step_key))
    return lookup


def _find_resource_source(resource_path: str, lookup: Sequence[Tuple[str, str, str]]) -> Optional[Tuple[str, str]]:
    path_norm = str(resource_path or "").replace("\\", "/")
    for path_key, ref, step_key in lookup:
        if path_key in path_norm:
            return ref, step_key
    return None


def _image_size(path: str, cache: Dict[str, Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    text = str(path or "")
    if not text:
        return None
    if text in cache:
        return cache[text]
    try:
        with Image.open(text) as img:
            size = (int(img.width), int(img.height))
    except Exception:
        return None
    cache[text] = size
    return size


def _trace_dir_from_screenshot_ref(ref: str) -> Optional[Path]:
    path = Path(str(ref or ""))
    if path.parent.name == "screenshots":
        return path.parent.parent
    if path.parent.is_dir():
        return path.parent
    return None


def _load_e2e_resource_index(trace_dir: Path, cache: Dict[str, Dict[str, Any]], e2e_resource_root: Optional[str] = None) -> Dict[str, Any]:
    key = str(trace_dir)
    if key in cache:
        return cache[key]
    result = {"by_path": {}, "by_basename": {}}
    e2e_path = trace_dir / "e2e_results.json"
    if not e2e_path.is_file() and e2e_resource_root:
        trace_id = trace_dir.name
        alt_path = Path(e2e_resource_root) / trace_id / "e2e_results.json"
        if alt_path.is_file():
            e2e_path = alt_path
    try:
        payload = json.loads(e2e_path.read_text(encoding="utf-8"))
    except Exception:
        cache[key] = result
        return result
    resources = payload.get("resources", []) if isinstance(payload, dict) else []
    for resource in resources if isinstance(resources, list) else []:
        if not isinstance(resource, dict):
            continue
        resource_path = str(resource.get("path") or "")
        if not resource_path:
            continue
        norm = resource_path.replace("\\", "/")
        result["by_path"][norm] = resource
        result["by_basename"].setdefault(os.path.basename(norm), resource)
    cache[key] = result
    return result


def _lookup_e2e_resource_metadata(
    resource_path: str,
    source_ref: str,
    e2e_cache: Dict[str, Dict[str, Any]],
    e2e_resource_root: Optional[str] = None,
) -> Dict[str, Any]:
    trace_dir = _trace_dir_from_screenshot_ref(source_ref)
    if not trace_dir:
        return {}
    index = _load_e2e_resource_index(trace_dir, e2e_cache, e2e_resource_root)
    norm = str(resource_path or "").replace("\\", "/")
    resource = index.get("by_path", {}).get(norm)
    if not resource:
        resource = index.get("by_basename", {}).get(os.path.basename(norm))
    metadata = resource.get("metadata", {}) if isinstance(resource, dict) else {}
    return metadata if isinstance(metadata, dict) else {}


def _numeric_bbox(value: Any) -> Optional[Dict[str, float]]:
    if not isinstance(value, dict):
        return None
    try:
        x = float(value.get("x"))
        y = float(value.get("y"))
        width = float(value.get("width"))
        height = float(value.get("height"))
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _resource_pixel_size(
    resource: Dict[str, Any],
    image_size_cache: Dict[str, Tuple[int, int]],
) -> Optional[Tuple[int, int]]:
    try:
        width = int(resource.get("width") or 0)
        height = int(resource.get("height") or 0)
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass
    return _image_size(str(resource.get("path") or ""), image_size_cache)


def _round_px(value: float) -> int:
    return max(1, int(round(value)))


def _attach_viewport_resource_geometry(
    resource: Dict[str, Any],
    *,
    source_ref: str,
    image_size_cache: Dict[str, Tuple[int, int]],
    e2e_cache: Dict[str, Dict[str, Any]],
    e2e_resource_root: Optional[str] = None,
    target_viewport: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item = dict(resource or {})
    source_size = _image_size(source_ref, image_size_cache)
    pixel_size = _resource_pixel_size(item, image_size_cache)
    if pixel_size:
        item["source_pixel_size"] = {"width": pixel_size[0], "height": pixel_size[1]}

    if not source_size:
        return item

    source_width, source_height = source_size
    item["source_screenshot_size"] = {"width": source_width, "height": source_height}
    viewport_width = int((target_viewport or {}).get("width") or MOBILE_VIEWPORT_WIDTH)
    viewport_height = int((target_viewport or {}).get("height") or MOBILE_VIEWPORT_HEIGHT)
    item["target_viewport_size"] = {
        "width": viewport_width,
        "height": viewport_height,
    }
    scale_x = viewport_width / source_width if source_width else 1.0
    scale_y = viewport_height / source_height if source_height else 1.0
    item["render_scale"] = {"x": round(scale_x, 6), "y": round(scale_y, 6)}
    item["resource_geometry_version"] = "viewport_normalized_v1"

    metadata = _lookup_e2e_resource_metadata(str(item.get("path") or ""), source_ref, e2e_cache, e2e_resource_root)
    if pixel_size:
        item["render_width"] = _round_px(pixel_size[0] * scale_x)
        item["render_height"] = _round_px(pixel_size[1] * scale_y)

    bbox = _numeric_bbox(metadata.get("bbox"))
    if bbox:
        item["source_pixel_bbox"] = {
            "x": _round_px(bbox["x"]),
            "y": _round_px(bbox["y"]),
            "width": _round_px(bbox["width"]),
            "height": _round_px(bbox["height"]),
        }
        render_bbox = {
            "x": _round_px(bbox["x"] * scale_x),
            "y": _round_px(bbox["y"] * scale_y),
            "width": _round_px(bbox["width"] * scale_x),
            "height": _round_px(bbox["height"] * scale_y),
        }
        item["render_bbox"] = render_bbox
        if not pixel_size:
            item["render_width"] = render_bbox["width"]
            item["render_height"] = render_bbox["height"]

    nearby_text = metadata.get("nearby_text")
    if nearby_text:
        item["source_nearby_text"] = str(nearby_text)[:240]
    return item


def build_pagegen_input_scopes(
    final_page_inputs: Sequence[Dict[str, Any]],
    resources: Sequence[Dict[str, Any]],
    fidelity_mode: str = "standard",
    page_environment_projections: Optional[Dict[str, Dict[str, Any]]] = None,
    spec_execution_views_by_page: Optional[Dict[str, Dict[str, Any]]] = None,
    interaction_surface_contract: Optional[Dict[str, Any]] = None,
    action_slot_registry: Optional[Dict[str, Any]] = None,
    evidence_resolver: Any = None,
    visual_surface_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    state_delta_signatures: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    e2e_resource_root: Optional[str] = None,
    step_transition_contracts: Optional[Dict[str, Any]] = None,
    render_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    scopes: Dict[str, Dict[str, Any]] = {}
    image_size_cache: Dict[str, Tuple[int, int]] = {}
    e2e_cache: Dict[str, Dict[str, Any]] = {}
    for page_input in final_page_inputs:
        page_input = _canonicalize_page_input_with_resolver(page_input, evidence_resolver)
        filename_hint = str(page_input.get("filename") or "") if isinstance(page_input, dict) else ""
        page_id_hint = str(page_input.get("page_id") or "") if isinstance(page_input, dict) else ""
        projection = None
        if page_environment_projections:
            projection = page_environment_projections.get(filename_hint)
            if not projection and page_id_hint:
                projection = page_environment_projections.get(page_id_hint)
        early_required_surfaces: List[Dict[str, Any]] = []
        if interaction_surface_contract and filename_hint:
            try:
                from trace2world_pipeline.app_spec.interaction_surface_contract import (
                    prompt_surfaces_for_filename,
                )

                early_required_surfaces = prompt_surfaces_for_filename(
                    interaction_surface_contract, filename_hint
                )
            except Exception:
                early_required_surfaces = []
        if fidelity_mode == "l0":
            scope = build_l0_render_scope(
                page_input,
                page_environment_projection=projection,
                required_interaction_surfaces=early_required_surfaces,
                visual_surface_signatures=visual_surface_signatures,
                state_delta_signatures=state_delta_signatures,
            )
        elif fidelity_mode == "full":
            scope = _attach_screenshot_lifecycle_projection(
                normalize_page_input_scope(page_input)
            )
        else:
            scope = build_standard_unique_render_scope(page_input)
        filename = str(scope.get("filename") or "")
        if not filename:
            continue

        prompt_ref_set = set(scope["prompt_screenshot_refs"])
        resource_match_refs = (
            scope.get("resource_screenshot_refs")
            or scope["prompt_screenshot_refs"]
            if fidelity_mode == "l0"
            else scope["page_screenshot_refs"]
        )
        ref_lookup = _build_ref_lookup(resource_match_refs)
        page_resources: List[Dict[str, Any]] = []
        for resource in resources:
            source = _find_resource_source(str(resource.get("path", "")), ref_lookup)
            if not source:
                continue
            source_ref, source_step_key = source
            item = dict(resource)
            item["asset_name"] = str(item.get("asset_name") or os.path.basename(str(item.get("path", ""))))
            item["source_screenshot_ref"] = source_ref
            item["source_step_key"] = source_step_key
            item["prompt_visibility"] = "prompt" if source_ref in prompt_ref_set else "non_prompt_page"
            item = _attach_viewport_resource_geometry(
                item,
                source_ref=source_ref,
                image_size_cache=image_size_cache,
                e2e_cache=e2e_cache,
                e2e_resource_root=e2e_resource_root,
                target_viewport=(render_context or {}).get("viewport"),
            )
            page_resources.append(item)

        page_scope = dict(scope)
        if isinstance(render_context, dict) and render_context:
            page_scope["render_context_ref"] = {
                "authority_ref": (
                    "phase_page_environment/semantic_api/08_spec_compiled_execution_views/"
                    "render_context.json"
                ),
                "profile_hash": render_context.get("profile_hash"),
                "platform": render_context.get("platform"),
                "viewport": render_context.get("viewport"),
                "prompt_policy": render_context.get("prompt_policy"),
            }
        if isinstance(page_input, dict) and page_input.get("pagegen_evidence_alignment"):
            page_scope["pagegen_evidence_alignment"] = page_input["pagegen_evidence_alignment"]
            page_scope["canonical_step_evidence_refs"] = page_input["pagegen_evidence_alignment"].get("evidence_refs", [])
        page_scope["page_resources"] = page_resources
        if page_environment_projections:
            projection = projection or page_environment_projections.get(filename)
            if not projection:
                page_id = str(page_scope.get("page_id") or "")
                projection = page_environment_projections.get(page_id)
            if projection:
                page_scope["page_environment_projection"] = projection
        if spec_execution_views_by_page:
            spec_execution_view = spec_execution_views_by_page.get(filename)
            if not spec_execution_view:
                page_id = str(page_scope.get("page_id") or "")
                spec_execution_view = spec_execution_views_by_page.get(page_id)
            if spec_execution_view:
                page_scope["spec_execution_view"] = spec_execution_view
                if spec_execution_view.get("autocomplete_contracts"):
                    page_scope["autocomplete_contracts"] = spec_execution_view["autocomplete_contracts"]
                    page_scope["autocomplete_contract_authority_path"] = "spec_compiled_execution_views/domain_contracts.json"
                if spec_execution_view.get("initial_control_state_contracts"):
                    page_scope["initial_control_state_contracts"] = spec_execution_view["initial_control_state_contracts"]
                if spec_execution_view.get("initial_dom_contract"):
                    page_scope["initial_dom_contract"] = spec_execution_view["initial_dom_contract"]
                if spec_execution_view.get("global_runtime_transaction_contracts"):
                    page_scope["global_runtime_transaction_contracts"] = spec_execution_view["global_runtime_transaction_contracts"]
                if spec_execution_view.get("action_effect_contracts"):
                    page_scope["action_effect_contracts"] = spec_execution_view["action_effect_contracts"]
                if spec_execution_view.get("runtime_transaction_contracts"):
                    page_scope["runtime_transaction_contracts"] = spec_execution_view["runtime_transaction_contracts"]
                if spec_execution_view.get("dom_binding_contracts"):
                    page_scope["dom_binding_contracts"] = spec_execution_view["dom_binding_contracts"]
                if spec_execution_view.get("global_schema_projection"):
                    page_scope["global_schema_projection"] = spec_execution_view["global_schema_projection"]
                if spec_execution_view.get("action_effect_authority_paths"):
                    page_scope["action_effect_authority_paths"] = spec_execution_view["action_effect_authority_paths"]
        if state_delta_signatures:
            page_scope["state_delta_authority"] = {
                "source": "trace_state_delta_signatures",
                "role": "spec_runtime_coverage_only",
                "note": "state deltas are NOT visual screenshot authority — they must be covered by typed action contracts and domain contracts, not by L0 screenshot admission",
            }
        if interaction_surface_contract:
            try:
                from trace2world_pipeline.app_spec.interaction_surface_contract import (
                    prompt_surfaces_for_filename,
                )

                surfaces = early_required_surfaces or prompt_surfaces_for_filename(
                    interaction_surface_contract, filename
                )
                if surfaces:
                    page_scope["required_interaction_surfaces"] = surfaces
            except Exception:


                pass
        if action_slot_registry:
            try:
                from trace2world_pipeline.app_spec.action_slot_registry import (
                    prompt_action_slots_for_filename,
                )

                route_slots = prompt_action_slots_for_filename(
                    action_slot_registry, filename
                )
                if route_slots:
                    page_scope["route_action_slots"] = route_slots
                    page_scope["action_slot_route_contract"] = {
                        "source": "action_slot_registry",
                        "executor": "GhostwebRouteExecution.navigate",
                        "sdk_side_effect": "WebsiteSDK.navigateRoute",
                        "sdk_side_effect_only": True,
                    }
            except Exception:
                pass
        if step_transition_contracts:
            try:
                stc_by_source: Dict[str, list] = {}
                for contract in step_transition_contracts.get("contracts", []):
                    src = str(contract.get("source_filename", "")).strip()
                    if src:
                        stc_by_source.setdefault(src, []).append({
                            "contract_id": contract.get("contract_id", ""),
                            "step_id": contract.get("step_id", ""),
                            "boundary_decision": contract.get("boundary_decision", ""),
                            "boundary_contract": contract.get("boundary_contract", {}),
                            "state_delta": contract.get("state_delta", {}),
                            "canonical_runtime_filename": (
                                contract.get("boundary_contract", {}).get(
                                    "canonical_runtime_filename", ""
                                )
                                if isinstance(contract.get("boundary_contract"), dict)
                                else ""
                            ),
                            "action_semantic": contract.get("action_semantic", ""),
                            "operation": contract.get("operation", ""),
                            "expected_target_filename": contract.get("expected_target_filename", ""),
                            "executor": contract.get("executor", ""),
                            "state_read": contract.get("state_read", {}),
                            "state_write": contract.get("state_write", {}),
                            "postcondition": contract.get("postcondition", {}),
                        })
                page_stc = stc_by_source.get(filename, [])
                if page_stc:
                    page_scope["step_transition_contracts"] = page_stc
            except Exception:
                pass
        page_scope["pagegen_render_view"] = build_pagegen_render_view(page_scope)
        scopes[filename] = page_scope
    return scopes


def attach_page_environment_projection(
    scopes: Dict[str, Dict[str, Any]],
    page_environment_projections: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(scopes, dict) or not isinstance(page_environment_projections, dict):
        return scopes
    updated: Dict[str, Dict[str, Any]] = {}
    for filename, scope in scopes.items():
        page_scope = dict(scope or {})
        projection = page_environment_projections.get(filename)
        if not projection:
            page_id = str(page_scope.get("page_id") or "")
            projection = page_environment_projections.get(page_id)
        if projection:
            page_scope["page_environment_projection"] = projection
        updated[filename] = page_scope
    return updated


def _compact_manifest_value(value: Any, depth: int = 0, parent_key: str = "") -> Any:
    def compact_component(item: Dict[str, Any]) -> Dict[str, Any]:
        contract = item.get("interaction_contract") if isinstance(item.get("interaction_contract"), dict) else {}
        compact = {
            "component_id": item.get("component_id"),
            "component_kind": item.get("component_kind"),
            "component_role": item.get("component_role"),
            "reads_state": item.get("reads_state"),
            "render_phase": item.get("render_phase"),
            "layout_policy": item.get("layout_policy"),
            "interaction_required": item.get("interaction_required"),
            "display_label": item.get("display_label"),
            "display_labels": item.get("display_labels"),
            "target_filename": item.get("target_filename"),
            "dom_contract": item.get("dom_contract"),
        }
        if contract:
            compact["interaction"] = {
                key: contract.get(key)
                for key in (
                    "operation",
                    "expected_filename",
                    "input_text",
                    "display_label",
                    "display_labels",
                    "reducer",
                    "state_effect",
                    "dom_contract",
                )
                if contract.get(key) not in (None, "", [], {})
            }
            intent = contract.get("operation_intent")
            if intent:
                compact["interaction"]["operation_intent"] = str(intent)[:220]
        steps = item.get("source_step_keys")
        if isinstance(steps, list) and steps:
            compact["source_step_keys"] = steps[:4]
        return {k: v for k, v in compact.items() if v not in (None, "", [], {})}

    def compact_projection(item: Dict[str, Any]) -> Dict[str, Any]:
        expected = item.get("expected_interaction") if isinstance(item.get("expected_interaction"), dict) else {}
        compact = {
            "projection_id": item.get("projection_id"),
            "component_ref": item.get("component_ref"),
            "reads_state": item.get("reads_state"),
            "render_phase": item.get("render_phase"),
            "layout_policy": item.get("layout_policy"),
            "interaction_required": item.get("interaction_required"),
            "lifecycle": item.get("lifecycle"),
            "dom_contract": item.get("dom_contract"),
            "suggestion_contract": item.get("suggestion_contract"),
            "overlay_contract": item.get("overlay_contract"),
            "selection_contract": item.get("selection_contract"),
        }
        if expected:
            compact["expected_interaction"] = {
                key: expected.get(key)
                for key in (
                    "operation",
                    "expected_filename",
                    "input_text",
                    "display_label",
                    "display_labels",
                    "reducer",
                    "state_effect",
                    "dom_contract",
                )
                if expected.get(key) not in (None, "", [], {})
            }
            intent = expected.get("operation_intent")
            if intent:
                compact["expected_interaction"]["operation_intent"] = str(intent)[:220]
        steps = item.get("source_step_keys")
        if isinstance(steps, list) and steps:
            compact["source_step_keys"] = steps[:4]
        return {k: v for k, v in compact.items() if v not in (None, "", [], {})}

    def compact_transition(item: Dict[str, Any]) -> Dict[str, Any]:
        compact = {
            key: item.get(key)
            for key in (
                "transition_id",
                "step_key",
                "source_filename",
                "target_filename",
                "operation",
                "reducer",
                "input_text",
                "direction",
                "l0_role",
                "state_effect",
            )
            if item.get(key) not in (None, "", [], {})
        }
        intent = item.get("operation_intent")
        if intent:
            compact["operation_intent"] = str(intent)[:220]
        return compact

    def compact_oracle(item: Dict[str, Any]) -> Dict[str, Any]:
        compact = {
            key: item.get(key)
            for key in ("oracle_type", "step_key", "component_ref", "target_filename", "expected_filename", "state_ref", "dom_contract", "display_label")
            if item.get(key) not in (None, "", [], {})
        }
        expected = item.get("expected")
        if expected:
            compact["expected"] = str(expected)[:220]
        return compact

    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        preserve_empty_list_keys = {"submit_targets", "related_items", "suggestions"}
        for key, child in value.items():
            key_text = str(key)
            if any(token in key_text for token in ("debug", "cache", "artifact", "raw_", "screenshot", "resource")):
                continue
            if depth >= 5 and isinstance(child, (dict, list)):
                continue
            compacted = _compact_manifest_value(child, depth + 1, key_text)
            if compacted not in (None, "", [], {}) or (
                compacted == [] and key_text in preserve_empty_list_keys
            ):
                result[key_text] = compacted
        return result
    if isinstance(value, list):
        if parent_key == "components":
            items = [compact_component(item) for item in value[:48] if isinstance(item, dict)]
            if len(value) > 48:
                items.append({"omitted_count": len(value) - 48, "omitted_reason": "prompt_manifest_size_limit"})
            return items
        if parent_key == "render_projections":
            items = [compact_projection(item) for item in value[:48] if isinstance(item, dict)]
            if len(value) > 48:
                items.append({"omitted_count": len(value) - 48, "omitted_reason": "prompt_manifest_size_limit"})
            return items
        if parent_key == "transitions":
            items = [compact_transition(item) for item in value[:64] if isinstance(item, dict)]
            if len(value) > 64:
                items.append({"omitted_count": len(value) - 64, "omitted_reason": "prompt_manifest_size_limit"})
            return items
        if parent_key == "oracles":
            items = [compact_oracle(item) for item in value[:64] if isinstance(item, dict)]
            if len(value) > 64:
                items.append({"omitted_count": len(value) - 64, "omitted_reason": "prompt_manifest_size_limit"})
            return items
        limits = {
            "components": 48,
            "render_projections": 64,
            "transitions": 80,
            "parameterized_inputs": 48,
            "oracles": 80,
            "content_seed": 24,
            "records": 16,
            "items": 16,
        }
        limit = limits.get(parent_key, 24 if depth >= 3 else 64)
        items = [_compact_manifest_value(item, depth + 1, parent_key) for item in value[:limit]]
        items = [item for item in items if item not in (None, "", [], {})]
        if len(value) > limit:
            items.append({"omitted_count": len(value) - limit, "omitted_reason": "prompt_manifest_size_limit"})
        return items
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value.strip())
        return text[:600] + ("..." if len(text) > 600 else "")
    return value


def compact_page_environment_projection_for_manifest(projection: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(projection, dict):
        return {}
    compact: Dict[str, Any] = {}
    for key in (
        "page_id",
        "filename",
        "initial_state",
        "state_schema",
        "content_seed",
        "components",
        "render_projections",
        "transitions",
        "query_projections",
        "parameterized_inputs",
        "interface_requirements",
        "oracles",
        "conflict_resolution",
        "conflict_resolutions",
        "semantic_enrichment",
    ):
        if key in projection:
            value = _compact_manifest_value(projection.get(key), 0, key)
            if key == "initial_state":
                compact[key] = projection.get(key) if value == {} else value
            elif value not in (None, "", [], {}):
                compact[key] = value
    if compact:
        compact["prompt_projection"] = {
            "source": "page_environment_spec",
            "mode": "compact_manifest_projection",
        }
    return compact


def build_pagegen_prompt_manifest(scope: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(scope, dict):
        return {}
    render_scope = scope.get("render_scope_contract")
    compact_render_scope = {}
    if isinstance(render_scope, dict):
        compact_render_scope = {
            "format": render_scope.get("format", "render_scope_contract"),
            "fidelity_mode": render_scope.get("fidelity_mode", scope.get("fidelity_mode", "")),
            "full_page_screenshot_count": render_scope.get("full_page_screenshot_count", 0),
            "selected_screenshot_count": render_scope.get("selected_screenshot_count", 0),
            "excluded_screenshot_count": render_scope.get("excluded_screenshot_count", 0),
            "task_evidence_steps": render_scope.get("task_evidence_steps", []),
            "ignored_task_evidence_steps": render_scope.get("ignored_task_evidence_steps", []),
            "task_critical_visual_evidence_refs": render_scope.get("task_critical_visual_evidence_refs", []),
            "task_critical_visual_admission_policy": render_scope.get("task_critical_visual_admission_policy", ""),
            "surface_coverage_selection": render_scope.get("surface_coverage_selection", {}),
            "initial_authority_selection": render_scope.get("initial_authority_selection", {}),
            "rules": render_scope.get("rules", []),
        }
    render_roles = _render_roles_for_scope(scope)
    render_annotations = _render_annotations_for_scope(scope)
    render_lifecycle = _render_lifecycle_for_scope(scope)
    prompt_refs = scope.get("prompt_screenshot_refs", [])
    prompt_screenshot_annotations = []
    manifest_warnings: List[Dict[str, Any]] = []
    render_role_instructions = {
        "canonical_layout": "Build initial DOM from this screenshot.",
        "post_interaction_overlay": "Render this overlay/panel initially hidden. Show via JS when the transition fires.",
        "interaction_state_change": "This shows a post-interaction state. Implement the visual diff as a JS state transition.",
        "runtime_status": "Do not render in initial DOM. Show only after the async event.",
    }
    prev_sk = ""
    initial_authority_kinds = scope.get("initial_authority_kind_by_step", {}) or {}
    for ref in prompt_refs:
        sk = step_key_from_screenshot_ref(ref) or ""
        canonical_sk = normalize_step_key(sk) if sk else ""
        role = render_roles.get(canonical_sk, "")
        if role not in _RENDER_ROLE_POLICIES or role == "unknown_render_role":
            role = "unknown_render_role"
        ann = render_annotations.get(canonical_sk, {})
        lifecycle = render_lifecycle.get(canonical_sk, {})
        policy = _render_role_policy(role)
        initial_dom_eligible = lifecycle.get("initial_dom_eligible")
        if not isinstance(initial_dom_eligible, bool):
            initial_dom_eligible = bool(policy["initial_dom_eligible"])
        initial_visibility_policy = policy["initial_visibility_policy"]
        runtime_visibility_policy = policy["runtime_visibility_policy"]
        lifecycle_role = str(lifecycle.get("lifecycle_role") or "").strip()
        if initial_dom_eligible is False and role == "canonical_layout":
            initial_visibility_policy = "hidden_until_transition"
            runtime_visibility_policy = "visible_when_state_active"
        entry: Dict[str, Any] = {
            "screenshot_ref": ref,
            "step_key": canonical_sk,
            "render_role": role,
            "initial_dom_eligible": bool(initial_dom_eligible),
            "initial_visibility_policy": initial_visibility_policy,
            "runtime_visibility_policy": runtime_visibility_policy,
            "pagegen_instruction": render_role_instructions.get(role, ""),
        }
        for field in (
            "structural_role",
            "lifecycle_role",
            "action_host_authority",
            "state_preconditions",
        ):
            if field in lifecycle:
                entry[field] = lifecycle[field]
        if lifecycle_role == "runtime_pre_action_state":
            entry["pagegen_instruction"] = (
                "Use this screenshot only after matching runtime state preconditions. "
                "Do not build initial visible DOM from it."
            )
        iak = initial_authority_kinds.get(canonical_sk, "")
        if iak:
            entry["initial_authority_kind"] = iak
        if role == "canonical_layout" and ann:
            action = ann.get("action", "")
            if action:
                entry["source_step_action"] = action
        elif role != "canonical_layout" and ann:
            action = ann.get("action", "")
            reason = ann.get("reason", "")
            if action:
                entry["transition_trigger_action"] = action
                entry["trigger_action"] = action
            if reason:
                entry["visual_diff"] = reason
            if prev_sk:
                prev_trace = prev_sk.split(":")[0] if ":" in prev_sk else ""
                cur_trace = canonical_sk.split(":")[0] if ":" in canonical_sk else ""
                if prev_trace and cur_trace and prev_trace == cur_trace:
                    entry["previous_step"] = prev_sk
        prev_sk = canonical_sk
        prompt_screenshot_annotations.append(entry)
    lifecycle_projection = _build_screenshot_lifecycle_projection(
        {
            **scope,
            "prompt_screenshot_refs": prompt_refs,
        }
    )
    resource_input_refs = {
        "resource_list_path": scope.get("resource_list_path", ""),
        "resource_semantic_labeling_path": scope.get("resource_semantic_labeling_path", ""),
        "pagegen_resource_projection_path": scope.get("pagegen_resource_projection_path", ""),
        "dedup_resource_count": scope.get("dedup_resource_count", 0),
        "raw_resource_count": scope.get("raw_resource_count", 0),
        "true_sprite_inputs": [
            key
            for key in ("icon_sprite_path", "photo_sprite_path")
            if scope.get(key)
        ],
        "auxiliary_sprite_inputs": [
            key
            for key in ("asset_sprite_path",)
            if scope.get(key)
        ],
    }
    resource_input_refs = {k: v for k, v in resource_input_refs.items() if v not in (None, "", [], {})}
    manifest = {
        "page_id": scope.get("page_id", ""),
        "filename": scope.get("filename", ""),
        "page_name": scope.get("page_name", ""),
        "page_type": scope.get("page_type", ""),
        "description": scope.get("description", ""),
        "fidelity_mode": scope.get("fidelity_mode", "standard"),
        "pagegen_execution_flow": scope.get("pagegen_execution_flow", "single_pass"),
        "render_context_ref": scope.get("render_context_ref", {}),
        "environment_prompt_policy": scope.get("environment_prompt_policy", {}),
        "prompt_input_tokens": scope.get("prompt_input_tokens"),
        "estimated_input_tokens": scope.get("estimated_input_tokens"),
        "prompt_screenshot_refs": prompt_refs,
        "initial_layout_screenshots": lifecycle_projection.get("initial_layout_screenshots", []),
        "runtime_page_initial_state_screenshots": lifecycle_projection.get("runtime_page_initial_state_screenshots", []),
        "trace_entry_initial_state_screenshots": lifecycle_projection.get("trace_entry_initial_state_screenshots", []),
        "state_transition_screenshots": lifecycle_projection.get("state_transition_screenshots", []),
        "runtime_status_screenshots": lifecycle_projection.get("runtime_status_screenshots", []),
        "unknown_render_role_screenshots": lifecycle_projection.get("unknown_render_role_screenshots", []),
        "source_host_visual_authority_screenshots": scope.get("source_host_visual_authority_screenshots", []),
        "source_host_visual_authority_issues": scope.get("source_host_visual_authority_issues", []),
        "initial_visual_authority": lifecycle_projection.get("initial_visual_authority", {}),
        "runtime_page_initial_visual_authority": lifecycle_projection.get("runtime_page_initial_visual_authority", {}),
        "prompt_screenshot_annotations": prompt_screenshot_annotations,
        "unique_prompt_screenshot_refs": scope.get("unique_prompt_screenshot_refs", []),
        "task_extra_screenshot_refs": scope.get("task_extra_screenshot_refs", []),
        "canonical_step_evidence_refs": scope.get("canonical_step_evidence_refs", []),
        "pagegen_evidence_alignment": scope.get("pagegen_evidence_alignment", {}),
        "render_scope_contract": compact_render_scope,
        "l0_related_runtime_evidence": scope.get("l0_related_runtime_evidence", []),
        "l0_visual_prompt_evidence": scope.get("l0_visual_prompt_evidence", []),
        "required_interaction_surfaces": scope.get("required_interaction_surfaces", []),
        "route_action_slots": scope.get("route_action_slots", []),
        "action_slot_route_contract": scope.get("action_slot_route_contract", {}),
        "pagegen_render_view": scope.get("pagegen_render_view", {}),
        "spec_execution_view": scope.get("spec_execution_view", {}),
        "entity_projection_contract": scope.get("entity_projection_contract", {}),
        "task_critical_content_steps": scope.get("task_critical_content_steps", []),
        "action_effect_contracts": scope.get("action_effect_contracts", []),
        "runtime_transaction_contracts": scope.get("runtime_transaction_contracts", []),
        "dom_binding_contracts": scope.get("dom_binding_contracts", []),
        "global_schema_projection": scope.get("global_schema_projection", {}),
        "action_effect_authority_paths": scope.get("action_effect_authority_paths", {}),
        "dedup_resource_count": scope.get("dedup_resource_count", 0),
        "resource_text_version": scope.get("resource_text_version", ""),
        "resource_input_refs": resource_input_refs,
        "resource_semantic_labeling": scope.get("resource_semantic_labeling", {}),
        "page_environment_projection": compact_page_environment_projection_for_manifest(
            scope.get("page_environment_projection", {})
        ),
        "pagegen_semantic_authority": scope.get("pagegen_semantic_authority", {}),
        "manifest_warnings": manifest_warnings,
        "manifest_blocking_issues": lifecycle_projection.get("pagegen_scope_blocking_issues", []),
        "artifact_metadata": {
            "artifact_kind": "pagegen_prompt_manifest",
            "api_or_det": "det",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": True,
            "artifact_role": "projection",
            "semantic_role": "lean_pagegen_input_boundary_manifest",
        },
    }
    if manifest.get("pagegen_render_view"):
        manifest["source_scope"] = {
            "page_environment_projection_available": bool(scope.get("page_environment_projection")),
            "spec_execution_view_available": bool(scope.get("spec_execution_view")),
            "required_interaction_surfaces_available": bool(scope.get("required_interaction_surfaces")),
            "route_action_slots_available": bool(scope.get("route_action_slots")),
            "action_slot_route_contract_available": bool(scope.get("action_slot_route_contract")),
            "action_effect_contracts_available": bool(scope.get("action_effect_contracts")),
            "runtime_transaction_contracts_available": bool(scope.get("runtime_transaction_contracts")),
            "dom_binding_contracts_available": bool(scope.get("dom_binding_contracts")),
            "global_schema_projection_available": bool(scope.get("global_schema_projection")),
        }
        for key in (
            "required_interaction_surfaces",
            "route_action_slots",
            "action_slot_route_contract",
            "spec_execution_view",
            "action_effect_contracts",
            "runtime_transaction_contracts",
            "dom_binding_contracts",
            "global_schema_projection",
            "action_effect_authority_paths",
            "page_environment_projection",
        ):
            manifest.pop(key, None)
    return manifest


def build_actual_pagegen_input_manifest(scope: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(scope, dict):
        return {}
    sprite_inputs = []
    for key, role in (
        ("icon_sprite_path", "true_sprite_input"),
        ("photo_sprite_path", "true_sprite_input"),
        ("asset_sprite_path", "auxiliary_resource_sheet"),
    ):
        path = scope.get(key)
        if path:
            sprite_inputs.append(
                {
                    "kind": key.replace("_path", ""),
                    "path": path,
                    "pagegen_input_role": role,
                }
            )
    return {
        "format": "actual_pagegen_input_manifest",
        "schema_revision": 2,
        "page_id": scope.get("page_id", ""),
        "filename": scope.get("filename", ""),
        "page_name": scope.get("page_name", ""),
        "page_type": scope.get("page_type", ""),
        "pagegen_execution_flow": scope.get("pagegen_execution_flow", "single_pass"),
        "text_inputs": {
            "pagegen_render_view": bool(scope.get("pagegen_render_view")),
            "semantic_contract_digest": (
                scope.get("pagegen_render_view", {}).get("semantic_contract_digest", "")
                if isinstance(scope.get("pagegen_render_view"), dict)
                else ""
            ),
            "resource_list_text": bool(scope.get("resource_list_text") or scope.get("dedup_resource_list_text")),
            "resource_text_version": scope.get("resource_text_version", ""),
        },
        "source_scope": {
            "page_environment_projection_available": bool(scope.get("page_environment_projection")),
            "action_effect_contracts_available": bool(scope.get("action_effect_contracts")),
            "runtime_transaction_contracts_available": bool(scope.get("runtime_transaction_contracts")),
            "dom_binding_contracts_available": bool(scope.get("dom_binding_contracts")),
            "global_schema_projection_available": bool(scope.get("global_schema_projection")),
            "stateful_execution_required": bool(
                scope.get("pagegen_render_view", {}).get("stateful_execution_required")
                if isinstance(scope.get("pagegen_render_view"), dict)
                else False
            ),
            "entity_projection_contract_available": bool(scope.get("entity_projection_contract")),
            "task_critical_content_steps_available": bool(scope.get("task_critical_content_steps")),
        },
        "image_inputs": {
            "prompt_screenshot_refs": scope.get("prompt_screenshot_refs", []),
            "initial_layout_screenshots": scope.get("initial_layout_screenshots", []),
            "runtime_page_initial_state_screenshots": scope.get("runtime_page_initial_state_screenshots", []),
            "trace_entry_initial_state_screenshots": scope.get("trace_entry_initial_state_screenshots", []),
            "state_transition_screenshots": scope.get("state_transition_screenshots", []),
            "runtime_status_screenshots": scope.get("runtime_status_screenshots", []),
            "unknown_render_role_screenshots": scope.get("unknown_render_role_screenshots", []),
            "source_host_visual_authority_screenshots": scope.get("source_host_visual_authority_screenshots", []),
            "task_extra_screenshot_refs": scope.get("task_extra_screenshot_refs", []),
        },
        "source_host_visual_authority_issues": scope.get("source_host_visual_authority_issues", []),
        "initial_visual_authority": scope.get("initial_visual_authority", {}),
        "runtime_page_initial_visual_authority": scope.get("runtime_page_initial_visual_authority", {}),
        "l0_related_runtime_evidence": scope.get("l0_related_runtime_evidence", []),
        "l0_visual_prompt_evidence": scope.get("l0_visual_prompt_evidence", []),
        "entity_projection_contract": scope.get("entity_projection_contract", {}),
        "task_critical_content_steps": scope.get("task_critical_content_steps", []),
        "sprite_inputs": sprite_inputs,
        "resource_inputs": {
            "raw_resource_count": scope.get("raw_resource_count", 0),
            "dedup_resource_count": scope.get("dedup_resource_count", 0),
            "resource_list_path": scope.get("resource_list_path", ""),
            "resource_semantic_labeling_path": scope.get("resource_semantic_labeling_path", ""),
            "pagegen_resource_projection_path": scope.get("pagegen_resource_projection_path", ""),
        },
        "audit": {
            "prompt_manifest_is_lean": True,
            "asset_sprite_is_auxiliary": any(item["kind"] == "asset_sprite" for item in sprite_inputs),
        },
        "artifact_metadata": {
            "artifact_kind": "actual_pagegen_input_manifest",
            "api_or_det": "det",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "artifact_role": "audit_manifest",
            "semantic_role": "actual_pagegen_input_audit",
        },
    }


def build_pagegen_scope_debug(scope: Dict[str, Any]) -> Dict[str, Any]:
    debug_scope = dict(scope or {})
    metadata = dict(debug_scope.get("artifact_metadata") or {})
    metadata.update({
        "artifact_kind": "pagegen_scope_debug",
        "api_or_det": metadata.get("api_or_det", "mixed"),
        "canonical_source": False,
        "canonical_semantic_source": False,
        "canonical_prompt_boundary": False,
        "artifact_role": "debug_cache",
        "debug_only": True,
        "semantic_role": "full_pagegen_scope_debug_export",
    })
    debug_scope["artifact_metadata"] = metadata
    return debug_scope
