
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from trace2world_pipeline.core.step_keys import canonical_step_key, normalize_step_keyed_mapping


def _selector_for_component_id(component_id: str) -> str:
    cid = str(component_id or "")
    return f'[data-element-id="{cid}"], [data-element-aliases~="{cid}"]' if cid else ""


def _step_key(trace_id: str, step_index: Any) -> str:
    try:
        idx = int(step_index)
    except (TypeError, ValueError):
        idx = 0
    return f"{trace_id}:step{idx}" if trace_id else f"step{idx}"


def _action_kind(step: Dict[str, Any]) -> str:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    operation = str(action.get("operation") or "").lower()
    if operation in {"fill", "input", "type"}:
        return "fill_input"
    if operation in {"tap", "click", "agent_execute"}:
        return "click"
    if operation in {"swipe", "scroll"}:
        return "scroll"
    return operation or "interaction"


def _expected_effect(step: Dict[str, Any]) -> Dict[str, Any]:
    pre = step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
    post = (
        step.get("expected_post_state")
        if isinstance(step.get("expected_post_state"), dict)
        else {}
    )
    source_filename = str(pre.get("filename") or "")
    target_filename = str(post.get("filename") or "")
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    effect: Dict[str, Any] = {}
    if target_filename and target_filename != source_filename:
        effect = {"type": "route", "target_filename": target_filename}
    else:
        effect = {"type": "same_page_state"}
    if action.get("input_text") not in (None, ""):
        effect["input_text"] = action.get("input_text")
    return effect


def _trace_hit_point(step: Dict[str, Any], step_key: str) -> Dict[str, Any]:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    viewport_coordinates = action.get("viewport_coordinates")
    raw_coordinates = action.get("coordinates")
    coords = viewport_coordinates if isinstance(viewport_coordinates, list) else raw_coordinates
    if not isinstance(coords, list) or len(coords) < 2:
        return {}
    try:
        x = float(coords[0])
        y = float(coords[1])
    except (TypeError, ValueError):
        return {}
    result: Dict[str, Any] = {
        "step_key": step_key,
        "x": int(x) if x.is_integer() else x,
        "y": int(y) if y.is_integer() else y,
        "source": (
            "l0_execution_pack.viewport_coordinates"
            if isinstance(viewport_coordinates, list)
            else "l0_execution_pack.coordinates"
        ),
    }
    if isinstance(raw_coordinates, list) and len(raw_coordinates) >= 2:
        result["raw_coordinates"] = raw_coordinates[:2]
    coordinate_space = str(action.get("coordinate_space") or "").strip()
    if coordinate_space:
        result["coordinate_space"] = coordinate_space
    return result


def _upsert_trace_hit_point(surface: Dict[str, Any], hit_point: Dict[str, Any]) -> None:
    if not hit_point:
        return
    points = surface.setdefault("trace_hit_points", [])
    if isinstance(points, list) and hit_point not in points:
        points.append(hit_point)


def _initial_value_binding(step: Dict[str, Any]) -> Dict[str, Any]:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    operation = str(action.get("operation") or "").lower()
    input_text = str(action.get("input_text") or "")
    if operation not in {"fill", "input", "type"} or not input_text:
        return {}
    return {
        "host_availability_phase": "initial",
        "initial_value_source": "spec_initial_state_or_empty",
        "post_action_value": input_text,
        "state_value_phase": "post_action",
        "forbid_initial_value_from_input_text": True,
    }


def _page_environment_specs_by_filename(payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("page_environment_specs_by_filename")
    if isinstance(nested, dict):
        return {
            str(filename): spec
            for filename, spec in nested.items()
            if isinstance(spec, dict)
        }
    return {
        str(filename): spec
        for filename, spec in payload.items()
        if isinstance(spec, dict)
    }


def _operation_action_kind(operation: Any) -> str:
    op = str(operation or "").strip().lower()
    if op in {"fill", "input", "type"}:
        return "fill_input"
    if op in {"tap", "click"}:
        return "click"
    if op in {"swipe", "scroll"}:
        return "scroll"
    return op or "interaction"


def _component_lifecycle_fields(component: Dict[str, Any]) -> Dict[str, Any]:
    render_phase = str(component.get("render_phase") or "")
    initial_dom_eligible = component.get("initial_dom_eligible")
    if initial_dom_eligible is None:
        initial_dom_eligible = render_phase in ("", "initial")
    initial_visibility_policy = str(component.get("initial_visibility_policy") or "")
    if not initial_visibility_policy:
        initial_visibility_policy = (
            "visible_initial" if bool(initial_dom_eligible) else "hidden_until_transition"
        )
    runtime_visibility_policy = str(component.get("runtime_visibility_policy") or "")
    if not runtime_visibility_policy:
        runtime_visibility_policy = (
            "visible_initial"
            if initial_visibility_policy == "visible_initial"
            else "visible_when_state_active"
        )
    return {
        "render_phase": render_phase,
        "host_availability_phase": str(component.get("host_availability_phase") or ""),
        "required_in_state": str(component.get("reads_state") or ""),
        "initial_dom_eligible": bool(initial_dom_eligible),
        "initial_visibility_policy": initial_visibility_policy,
        "runtime_visibility_policy": runtime_visibility_policy,
    }


def _merge_lifecycle(current: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    if not current:
        return dict(incoming)
    result = dict(current)
    if incoming.get("initial_visibility_policy") == "scroll_reachable_initial":
        result.update(incoming)
        return result
    if incoming.get("initial_dom_eligible") is False:
        result.update(incoming)
        result["initial_dom_eligible"] = False
        return result
    for key, value in incoming.items():
        if key not in result or result.get(key) in (None, ""):
            result[key] = value
    return result


def _component_lifecycle_by_step(page_environment_specs: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    specs_by_filename = _page_environment_specs_by_filename(page_environment_specs)
    result: Dict[str, Dict[str, Any]] = {}
    for spec in specs_by_filename.values():
        components = spec.get("components") if isinstance(spec, dict) else []
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, dict):
                continue
            if str(component.get("component_role") or "").lower() != "l0_execution_anchor":
                continue
            if not component.get("interaction_required"):
                continue
            lifecycle = _component_lifecycle_fields(component)
            source_steps = component.get("source_step_keys")
            if not isinstance(source_steps, list):
                source_steps = []
            for step_key in source_steps:
                canonical = canonical_step_key(step_key)
                if canonical:
                    result[canonical] = _merge_lifecycle(result.get(canonical, {}), lifecycle)
    return result


def _apply_step_lifecycle(surface: Dict[str, Any], lifecycle: Dict[str, Any]) -> None:
    if not lifecycle:
        return
    if lifecycle.get("initial_visibility_policy") == "scroll_reachable_initial":
        surface["render_phase"] = lifecycle.get("render_phase") or "initial"
        surface["host_availability_phase"] = (
            lifecycle.get("host_availability_phase") or "initial_dom_scroll_reachable"
        )
        surface["initial_dom_eligible"] = True
        surface["initial_visibility_policy"] = "scroll_reachable_initial"
        surface["runtime_visibility_policy"] = (
            lifecycle.get("runtime_visibility_policy") or "visible_after_scroll"
        )
        if lifecycle.get("required_in_state"):
            surface["required_in_state"] = lifecycle["required_in_state"]
        return
    if lifecycle.get("initial_dom_eligible") is False:
        surface["render_phase"] = lifecycle.get("render_phase") or "runtime"
        surface["host_availability_phase"] = lifecycle.get("host_availability_phase") or "runtime"
        surface["initial_dom_eligible"] = False
        surface["initial_visibility_policy"] = lifecycle.get("initial_visibility_policy") or "hidden_until_transition"
        surface["runtime_visibility_policy"] = lifecycle.get("runtime_visibility_policy") or "visible_when_state_active"
        if lifecycle.get("required_in_state"):
            surface["required_in_state"] = lifecycle["required_in_state"]
        return
    surface.setdefault("render_phase", lifecycle.get("render_phase") or "initial")
    surface.setdefault("host_availability_phase", lifecycle.get("host_availability_phase") or "initial")
    surface.setdefault("initial_dom_eligible", lifecycle.get("initial_dom_eligible", True))
    surface.setdefault("initial_visibility_policy", lifecycle.get("initial_visibility_policy") or "visible_initial")
    surface.setdefault("runtime_visibility_policy", lifecycle.get("runtime_visibility_policy") or "visible_initial")
    if lifecycle.get("required_in_state"):
        surface.setdefault("required_in_state", lifecycle["required_in_state"])


_CONTAINER_SUFFIXES = (
    "_section", "_form", "_wrapper", "_container", "_region",
    "_area", "_panel", "_group", "_block", "_box", "_layout",
    "_list", "_counter", "_summary", "_stats", "_status",
)

_ACTIONABLE_SUFFIXES = (
    "_button", "_btn", "_link", "_input", "_checkbox", "_radio",
    "_select", "_toggle", "_switch", "_tab", "_anchor", "_cta",
    "_submit", "_search", "_picker",
)


def _is_container_component(component_id: str) -> bool:
    cid = component_id.lower().strip()
    if not cid:
        return False
    if any(cid.endswith(suffix) for suffix in _ACTIONABLE_SUFFIXES):
        return False
    if any(cid.endswith(suffix) for suffix in _CONTAINER_SUFFIXES):
        return True
    return False


def _apply_surface_interactivity(surface: Dict[str, Any], component_id: str) -> None:
    if not _is_container_component(component_id):
        return
    surface["surface_role"] = "semantic_region"
    surface["interactive_required"] = False
    surface["topmost_required"] = False
    surface["action_host_status"] = "action_host_unresolved"


def _apply_render_role_lifecycle(
    surface: Dict[str, Any],
    source_steps: Iterable[Any],
    render_roles_by_step: Dict[str, str],
) -> None:
    if surface.get("initial_visibility_policy") == "scroll_reachable_initial":
        return
    for step_key in source_steps:
        role = str(render_roles_by_step.get(canonical_step_key(step_key)) or "").strip()
        if role in {"interaction_state_change", "post_interaction_overlay"}:
            surface["render_phase"] = "runtime"
            surface["initial_dom_eligible"] = False
            surface["initial_visibility_policy"] = "hidden_until_transition"
            surface["runtime_visibility_policy"] = "visible_when_state_active"
            surface["required_in_state"] = "state::page_runtime"
            return


def _slug_part(value: Any, *, fallback: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    chars: List[str] = []
    for char in text:
        if char.isalnum():
            chars.append(char)
        else:
            chars.append("_")
    slug = "_".join(part for part in "".join(chars).split("_") if part)
    return slug or fallback


def _dedupe_values(values: Iterable[Any], *, limit: int = 64) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _compact_label(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _label_authority(component: Dict[str, Any], interaction: Dict[str, Any]) -> str:
    return _compact_label(
        component.get("label_authority")
        or interaction.get("label_authority")
        or _as_dict(component.get("visible_text_contract")).get("authority")
        or _as_dict(interaction.get("visible_text_contract")).get("authority")
    )


def _visible_text_contract(component: Dict[str, Any], interaction: Dict[str, Any]) -> Dict[str, Any]:
    for source in (
        component.get("visible_text_contract"),
        interaction.get("visible_text_contract"),
    ):
        if isinstance(source, dict) and source:
            return dict(source)
    authority = _label_authority(component, interaction)
    if authority:
        return {"authority": authority, "exact_text": ""}
    return {}


def _visible_display_label(component: Dict[str, Any], interaction: Dict[str, Any]) -> str:
    contract = _visible_text_contract(component, interaction)
    authority = _compact_label(contract.get("authority") or _label_authority(component, interaction))
    exact = _compact_label(contract.get("exact_text"))
    if authority in {"observed_host_text", "visual_slot_observed", "exact_visible_label_required"}:
        return exact or _compact_label(
            component.get("required_display_label")
            or component.get("display_label")
            or interaction.get("required_display_label")
            or interaction.get("display_label")
        )
    if authority in {"semantic_role_only", "context_only", "unresolved"}:
        return ""
    return _compact_label(
        component.get("required_display_label")
        or component.get("display_label")
        or interaction.get("required_display_label")
        or interaction.get("display_label")
    )


def _opened_surface_family(
    *,
    label: Any,
    operation_intent: Any,
    expected_filename: Any,
    component_kind: Any,
) -> str:
    text = " ".join(
        _compact_label(item)
        for item in (label, operation_intent, expected_filename, component_kind)
        if _compact_label(item)
    ).lower()
    if (
        "publish" in text
        or "compose" in text
        or "发布" in text
        or "创作" in text
        or "写文字" in text
        or "相册" in text
        or "相机" in text
        or "拍摄" in text
        or "拍视频" in text
    ):
        return "create_entry_sheet"
    if "回复" in text or "reply" in text or "comment" in text:
        return "reply_sheet"
    if any(term in text for term in ("删除", "置顶", "标为未读", "清空", "delete", "pin", "unread")):
        return "list_item_context_actions"
    if "筛选" in text or "filter" in text:
        return "filter_sheet"
    return "opened_overlay"


def _opened_option_key(
    *,
    surface_family: str,
    label: Any,
    operation_intent: Any,
    expected_filename: Any,
) -> str:
    label_text = _compact_label(label)
    text = f"{label_text} {_compact_label(operation_intent)} {_compact_label(expected_filename)}".lower()
    target = _compact_label(expected_filename)
    if surface_family == "create_entry_sheet":
        if "相册" in text or "album" in text or "media-picker" in target.lower():
            return "album_picker"
        if "写文字" in text or "compose-text" in target.lower() or "text" in text:
            return "compose_text"
        if "相机" in text or "拍摄" in text or "camera" in text:
            return "camera"
        if "取消" in text or "cancel" in text:
            return "cancel"
    if surface_family == "reply_sheet" and ("回复" in text or "reply" in text):
        return "reply"
    if target:
        return "route_" + _slug_part(target, fallback="target")
    return _slug_part(label_text or operation_intent, fallback="option")


def _canonical_option_label(option_key: str, labels: Iterable[Any]) -> str:
    label_values = _dedupe_values(labels, limit=8)
    if option_key == "album_picker":
        return "从相册选择"
    if option_key == "compose_text":
        return "写文字"
    if option_key == "camera":
        return "相机"
    if option_key == "cancel":
        return "取消"
    return label_values[0] if label_values else option_key


def _shell_broadcast_surface_id(filename: str, item: Dict[str, Any]) -> str:
    page_slug = _slug_part(filename.replace(".", "_"), fallback="page")
    shell_slug = _slug_part(
        item.get("shell_family_id") or item.get("surface"),
        fallback="shell",
    )
    action_slug = _slug_part(
        item.get("evidence_action_id")
        or item.get("interaction_kind")
        or item.get("control_label"),
        fallback="interaction",
    )
    return f"cmp::{page_slug}::shell::{shell_slug}::{action_slug}"


def _state_surface_root_id(filename: str, item: Dict[str, Any], open_state_id: str) -> str:
    page_slug = _slug_part(filename.replace(".", "_"), fallback="page")
    shell_slug = _slug_part(
        item.get("shell_family_id") or item.get("surface"),
        fallback="shell",
    )
    state_slug = _slug_part(open_state_id, fallback="state_surface")
    return f"cmp::{page_slug}::shell::{shell_slug}::{state_slug}::state_surface"


def _state_surface_option_id(filename: str, item: Dict[str, Any], open_state_id: str, label: Any) -> str:
    page_slug = _slug_part(filename.replace(".", "_"), fallback="page")
    shell_slug = _slug_part(
        item.get("shell_family_id") or item.get("surface"),
        fallback="shell",
    )
    state_slug = _slug_part(open_state_id, fallback="state_surface")
    label_slug = _slug_part(label, fallback="option")
    return f"cmp::{page_slug}::shell::{shell_slug}::{state_slug}::option::{label_slug}"


def _shell_broadcast_expected_effect(item: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(item.get("interaction_kind") or "same_page_state").strip() or "same_page_state"
    target_filename = str(item.get("target_filename") or "").strip()
    if target_filename:
        return {"type": "route", "target_filename": target_filename}
    if kind == "overlay":
        overlay_id = str(item.get("evidence_action_id") or item.get("control_label") or "").strip()
        effect: Dict[str, Any] = {"type": "overlay"}
        if overlay_id:
            effect["overlay_id"] = overlay_id
        return effect
    return {"type": "same_page_state", "interaction_kind": kind}


def _upsert_surface_step(surface: Dict[str, Any], evidence: Dict[str, Any]) -> None:
    if evidence not in surface["evidence_steps"]:
        surface["evidence_steps"].append(evidence)


def _collect_opened_surface_candidate(
    *,
    filename: str,
    component: Dict[str, Any],
    interaction: Dict[str, Any],
    action_kind: str,
    source_steps: List[Any],
    render_roles_by_step: Dict[str, str],
) -> Dict[str, Any] | None:
    component_id = str(component.get("component_id") or "").strip()
    if not component_id:
        return None
    label = _compact_label(
        component.get("required_display_label")
        or component.get("display_label")
        or interaction.get("display_label")
    )
    operation_intent = _compact_label(
        component.get("operation_intent")
        or interaction.get("operation_intent")
        or interaction.get("action_text")
    )
    expected_filename = _compact_label(interaction.get("expected_filename"))
    component_kind = _compact_label(component.get("component_kind"))
    surface_family = _opened_surface_family(
        label=label,
        operation_intent=operation_intent,
        expected_filename=expected_filename,
        component_kind=component_kind,
    )
    roles = {
        str(render_roles_by_step.get(canonical_step_key(step_key)) or "").strip()
        for step_key in source_steps
    }
    has_opened_role = "post_interaction_overlay" in roles or (
        "interaction_state_change" in roles
        and surface_family in {"list_item_context_actions", "filter_sheet"}
    )
    if not has_opened_role:
        return None
    option_key = _opened_option_key(
        surface_family=surface_family,
        label=label,
        operation_intent=operation_intent,
        expected_filename=expected_filename,
    )
    expected_effect = {"type": "same_page_state"}
    if expected_filename and expected_filename != filename:
        expected_effect = {"type": "route", "target_filename": expected_filename}
    return {
        "filename": filename,
        "component_id": component_id,
        "label": label,
        "operation_intent": operation_intent,
        "expected_filename": expected_filename,
        "component_kind": component_kind,
        "action_kind": action_kind,
        "source_steps": [str(step) for step in source_steps if str(step or "").strip()],
        "surface_family": surface_family,
        "option_key": option_key,
        "expected_effect": expected_effect,
    }


def _attach_opened_interaction_surfaces(
    surfaces_by_key: Dict[tuple[str, str], Dict[str, Any]],
    candidates: List[Dict[str, Any]],
) -> None:
    grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for candidate in candidates:
        filename = str(candidate.get("filename") or "").strip()
        family = str(candidate.get("surface_family") or "").strip()
        if filename and family:
            grouped.setdefault((filename, family), []).append(candidate)

    for (filename, family), group in grouped.items():
        if not group:
            continue
        page_slug = _slug_part(filename.replace(".", "_"), fallback="page")
        family_slug = _slug_part(family, fallback="opened_surface")
        root_id = f"cmp::{page_slug}::opened_surface::{family_slug}"
        root_key = (filename, root_id)
        options_by_key: Dict[str, List[Dict[str, Any]]] = {}
        for candidate in group:
            option_key = str(candidate.get("option_key") or "option")
            options_by_key.setdefault(option_key, []).append(candidate)

        option_contracts: List[Dict[str, Any]] = []
        for option_index, (option_key, option_group) in enumerate(sorted(options_by_key.items())):
            labels = _dedupe_values((item.get("label") for item in option_group), limit=12)
            component_ids = _dedupe_values((item.get("component_id") for item in option_group), limit=32)
            source_steps = _dedupe_values(
                step
                for item in option_group
                for step in item.get("source_steps", [])
                if str(step or "").strip()
            )
            effects: List[Dict[str, Any]] = []
            for item in option_group:
                effect = item.get("expected_effect")
                if isinstance(effect, dict) and effect not in effects:
                    effects.append(effect)
            option_id = f"{root_id}::option::{_slug_part(option_key, fallback='option')}"
            canonical_label = _canonical_option_label(option_key, labels)
            option_contract = {
                "option_id": option_id,
                "option_key": option_key,
                "label": canonical_label,
                "aliases": labels,
                "component_ids": component_ids,
                "source_step_keys": source_steps,
                "expected_effect": effects[0] if effects else {"type": "same_page_state"},
                "all_expected_effects": effects,
                "action_kind": next(
                    (
                        str(item.get("action_kind") or "")
                        for item in option_group
                        if str(item.get("action_kind") or "")
                    ),
                    "click",
                ),
                "interactive_required": True,
                "sort_order": option_index,
            }
            option_contracts.append(option_contract)

            option_surface = {
                "surface_id": option_id,
                "component_ref": option_id,
                "filename": filename,
                "source": "opened_interaction_surface_inference",
                "surface_role": "opened_interaction_surface_option",
                "surface_family": family,
                "opened_surface_id": root_id,
                "opened_surface_option_id": option_id,
                "required_in_state": f"state::{root_id}",
                "render_phase": "runtime",
                "host_availability_phase": "runtime",
                "initial_dom_eligible": False,
                "initial_visibility_policy": "hidden_until_transition",
                "runtime_visibility_policy": "visible_after_trigger",
                "action_kinds": [option_contract["action_kind"]],
                "required_dom_selector": _selector_for_component_id(option_id),
                "required_data_element_id": option_id,
                "required_data_element_aliases": component_ids,
                "required_display_label": canonical_label,
                "option_aliases": labels,
                "surface": "opened_interaction_surface",
                "interaction_kind": "opened_surface_option",
                "visibility_required": True,
                "topmost_required": True,
                "interactive_required": True,
                "dom_contract": {
                    "required_behavior": "opened_interaction_surface_option",
                    "forbid_noninteractive_host": True,
                    "click_surface": "real_visible_component",
                },
                "expected_effects": effects,
                "evidence_steps": [
                    {"step_key": step, "source": "opened_interaction_surface_inference"}
                    for step in source_steps
                ],
                "binding_sources": ["opened_interaction_surface_inference"],
                "contract_status": "complete",
                "sort_order": 3030 + option_index,
            }
            surfaces_by_key[(filename, option_id)] = option_surface

            for component_id in component_ids:
                source_surface = surfaces_by_key.get((filename, component_id))
                if not source_surface:
                    continue
                source_surface["opened_surface_id"] = root_id
                source_surface["opened_surface_option_id"] = option_id
                source_surface["surface_family"] = family
                source_surface["option_aliases"] = labels
                source_surface["required_data_element_aliases"] = component_ids
                if "opened_interaction_surface_inference" not in source_surface.setdefault("binding_sources", []):
                    source_surface["binding_sources"].append("opened_interaction_surface_inference")

        all_source_steps = _dedupe_values(
            step
            for item in group
            for step in item.get("source_steps", [])
            if str(step or "").strip()
        )
        root_surface = {
            "surface_id": root_id,
            "component_ref": root_id,
            "filename": filename,
            "source": "opened_interaction_surface_inference",
            "surface_role": "opened_interaction_surface",
            "surface_family": family,
            "required_in_state": f"state::{root_id}",
            "render_phase": "runtime",
            "host_availability_phase": "runtime",
            "initial_dom_eligible": False,
            "initial_visibility_policy": "hidden_until_transition",
            "runtime_visibility_policy": "visible_after_trigger",
            "action_kinds": [],
            "required_dom_selector": _selector_for_component_id(root_id),
            "required_data_element_id": root_id,
            "required_display_label": "",
            "surface": "opened_interaction_surface",
            "interaction_kind": "opened_interaction_surface",
            "visibility_required": True,
            "topmost_required": True,
            "interactive_required": False,
            "dom_contract": {
                "required_behavior": "opened_interaction_surface",
                "forbid_initial_visible": True,
            },
            "expected_effects": [],
            "evidence_steps": [
                {"step_key": step, "source": "opened_interaction_surface_inference"}
                for step in all_source_steps
            ],
            "binding_sources": ["opened_interaction_surface_inference"],
            "state_surface_contract": {
                "surface_id": root_id,
                "open_state_id": root_id,
                "surface_family": family,
                "initial_visibility_policy": "hidden_until_transition",
                "runtime_visibility_policy": "visible_after_trigger",
                "options": option_contracts,
            },
            "contract_status": "complete",
            "sort_order": 3025,
        }
        existing_root = surfaces_by_key.get(root_key)
        if existing_root:
            existing_root.update(root_surface)
        else:
            surfaces_by_key[root_key] = root_surface


def build_interaction_surface_contract(
    l0_execution_pack: Dict[str, Any],
    *,
    page_environment_specs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    action_packs = (
        l0_execution_pack.get("action_packs")
        if isinstance(l0_execution_pack, dict)
        else None
    )
    if not isinstance(action_packs, list):
        action_packs = []
    render_roles_by_step = normalize_step_keyed_mapping(
        page_environment_specs.get("screenshot_render_roles")
        if isinstance(page_environment_specs, dict)
        else {}
    )
    specs_by_filename = _page_environment_specs_by_filename(page_environment_specs)
    lifecycle_by_step = _component_lifecycle_by_step(page_environment_specs)

    surfaces_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    opened_surface_candidates: List[Dict[str, Any]] = []
    skipped_steps = 0
    unresolved_steps = 0
    manual_steps = 0
    executable_steps = 0

    for pack in action_packs:
        if not isinstance(pack, dict):
            continue
        trace_id = str(pack.get("trace_id") or "")
        steps = pack.get("steps") if isinstance(pack.get("steps"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                continue
            target = step.get("target") if isinstance(step.get("target"), dict) else {}
            status = str(target.get("status") or "")
            if status == "not_applicable":
                skipped_steps += 1
                continue
            if status == "manual":
                manual_steps += 1
                continue
            if status != "resolved":
                unresolved_steps += 1
                continue
            component_id = str(target.get("component_id") or "")
            if not component_id:
                unresolved_steps += 1
                continue
            pre = step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
            source_filename = str(pre.get("filename") or "")
            if not source_filename:
                unresolved_steps += 1
                continue
            executable_steps += 1
            key = (source_filename, component_id)
            surface = surfaces_by_key.get(key)
            if not surface:
                is_container = _is_container_component(component_id)
                surface = {
                    "surface_id": component_id,
                    "component_ref": component_id,
                    "filename": source_filename,
                    "source": "l0_execution_pack",
                    "surface_role": "semantic_region" if is_container else "source_host",
                    "action_kinds": [],
                    "required_dom_selector": _selector_for_component_id(component_id),
                    "required_data_element_id": component_id,
                    "visibility_required": True,
                    "topmost_required": not is_container,
                    "interactive_required": not is_container,
                    "expected_effects": [],
                    "evidence_steps": [],
                    "binding_sources": [],
                    "value_lifecycle": {},
                }
                _apply_surface_interactivity(surface, component_id)
                surfaces_by_key[key] = surface
            action_kind = _action_kind(step)
            if action_kind not in surface["action_kinds"]:
                surface["action_kinds"].append(action_kind)
            effect = _expected_effect(step)
            if effect not in surface["expected_effects"]:
                surface["expected_effects"].append(effect)
            value_lifecycle = _initial_value_binding(step)
            if value_lifecycle:
                surface["value_lifecycle"] = value_lifecycle
            evidence = {
                "trace_id": trace_id,
                "step_index": step.get("step_index"),
                "step_key": _step_key(trace_id, step.get("step_index")),
            }
            if evidence not in surface["evidence_steps"]:
                surface["evidence_steps"].append(evidence)
            step_key = evidence["step_key"]
            _upsert_trace_hit_point(surface, _trace_hit_point(step, step_key))
            _apply_step_lifecycle(surface, lifecycle_by_step.get(canonical_step_key(step_key), {}))
            _apply_render_role_lifecycle(surface, [step_key], render_roles_by_step)
            binding_source = str(target.get("binding_source") or "")
            if binding_source and binding_source not in surface["binding_sources"]:
                surface["binding_sources"].append(binding_source)

    for filename, spec in specs_by_filename.items():
        components = spec.get("components") if isinstance(spec, dict) else []
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, dict):
                continue
            if str(component.get("component_role") or "").lower() != "l0_execution_anchor":
                continue
            if not component.get("interaction_required"):
                continue
            component_id = str(component.get("component_id") or "").strip()
            if not component_id:
                continue
            interaction = (
                component.get("interaction_contract")
                if isinstance(component.get("interaction_contract"), dict)
                else {}
            )
            display_label = _visible_display_label(component, interaction)
            label_authority = _label_authority(component, interaction)
            visible_text_contract = _visible_text_contract(component, interaction)
            context_text_must_include = [
                _compact_label(item)
                for item in (
                    component.get("context_text_must_include")
                    if isinstance(component.get("context_text_must_include"), list)
                    else interaction.get("context_text_must_include")
                    if isinstance(interaction.get("context_text_must_include"), list)
                    else []
                )
                if _compact_label(item)
            ]
            accessible_label_must_include = [
                _compact_label(item)
                for item in (
                    component.get("accessible_label_must_include")
                    if isinstance(component.get("accessible_label_must_include"), list)
                    else interaction.get("accessible_label_must_include")
                    if isinstance(interaction.get("accessible_label_must_include"), list)
                    else []
                )
                if _compact_label(item)
            ]
            operation_intent = _compact_label(
                component.get("operation_intent")
                or interaction.get("operation_intent")
                or interaction.get("action_text")
                or interaction.get("command")
            )
            operation = interaction.get("operation") or component.get("operation")
            action_kind = _operation_action_kind(operation)
            key = (filename, component_id)
            surface = surfaces_by_key.get(key)
            if not surface:
                dom_contract = (
                    component.get("dom_contract")
                    if isinstance(component.get("dom_contract"), dict)
                    else interaction.get("dom_contract")
                    if isinstance(interaction.get("dom_contract"), dict)
                    else {}
                )
                lifecycle = _component_lifecycle_fields(component)
                is_container = _is_container_component(component_id)
                surface = {
                    "surface_id": component_id,
                    "component_ref": component_id,
                    "filename": filename,
                    "source": "page_environment_spec",
                    "surface_role": "semantic_region" if is_container else "source_host",
                    "required_in_state": str(
                        component.get("reads_state")
                        or component.get("host_availability_phase")
                        or component.get("render_phase")
                        or "page_initial"
                    ),
                    "render_phase": component.get("render_phase"),
                    "host_availability_phase": component.get("host_availability_phase"),
                    "initial_dom_eligible": lifecycle["initial_dom_eligible"],
                    "initial_visibility_policy": lifecycle["initial_visibility_policy"],
                    "runtime_visibility_policy": lifecycle["runtime_visibility_policy"],
                    "action_kinds": [],
                    "required_dom_selector": _selector_for_component_id(component_id),
                    "required_data_element_id": component_id,
                    "required_display_label": display_label,
                    "label_authority": label_authority,
                    "visible_text_contract": visible_text_contract,
                    "visual_slot_key": component.get("visual_slot_key") or interaction.get("visual_slot_key"),
                    "context_text_must_include": context_text_must_include,
                    "context_match_scope": (
                        component.get("context_match_scope")
                        or interaction.get("context_match_scope")
                        or "ancestor_or_nearby_region"
                    ),
                    "accessible_label_must_include": accessible_label_must_include,
                    "operation_intent": operation_intent,
                    "visibility_required": True,
                    "topmost_required": not is_container,
                    "interactive_required": not is_container,
                    "dom_contract": dom_contract,
                    "expected_effects": [],
                    "evidence_steps": [],
                    "binding_sources": ["page_environment_l0_source_host"],
                    "value_lifecycle": {},
                }
                if is_container:
                    surface["action_host_status"] = "action_host_unresolved"
                surfaces_by_key[key] = surface
            else:
                surface.setdefault("surface_role", "source_host")
                surface.setdefault(
                    "required_in_state",
                    str(
                        component.get("reads_state")
                        or component.get("host_availability_phase")
                        or component.get("render_phase")
                        or "page_initial"
                    ),
                )
                if component.get("render_phase") not in (None, ""):
                    surface["render_phase"] = component.get("render_phase")
                if component.get("host_availability_phase") not in (None, ""):
                    surface["host_availability_phase"] = component.get(
                        "host_availability_phase"
                    )
                lifecycle = _component_lifecycle_fields(component)
                surface["initial_dom_eligible"] = lifecycle["initial_dom_eligible"]
                surface["initial_visibility_policy"] = lifecycle["initial_visibility_policy"]
                surface["runtime_visibility_policy"] = lifecycle["runtime_visibility_policy"]
                dom_contract = (
                    component.get("dom_contract")
                    if isinstance(component.get("dom_contract"), dict)
                    else interaction.get("dom_contract")
                    if isinstance(interaction.get("dom_contract"), dict)
                    else {}
                )
                if dom_contract:
                    surface["dom_contract"] = dom_contract
            if display_label and not surface.get("required_display_label"):
                surface["required_display_label"] = display_label
            if label_authority and not surface.get("label_authority"):
                surface["label_authority"] = label_authority
            if visible_text_contract and not surface.get("visible_text_contract"):
                surface["visible_text_contract"] = visible_text_contract
            if (component.get("visual_slot_key") or interaction.get("visual_slot_key")) and not surface.get("visual_slot_key"):
                surface["visual_slot_key"] = component.get("visual_slot_key") or interaction.get("visual_slot_key")
            if context_text_must_include:
                existing_context = [
                    _compact_label(item)
                    for item in (
                        surface.get("context_text_must_include")
                        if isinstance(surface.get("context_text_must_include"), list)
                        else []
                    )
                    if _compact_label(item)
                ]
                for label in context_text_must_include:
                    if label not in existing_context:
                        existing_context.append(label)
                surface["context_text_must_include"] = existing_context
                surface.setdefault("context_match_scope", "ancestor_or_nearby_region")
            if accessible_label_must_include:
                existing_accessible = [
                    _compact_label(item)
                    for item in (
                        surface.get("accessible_label_must_include")
                        if isinstance(surface.get("accessible_label_must_include"), list)
                        else []
                    )
                    if _compact_label(item)
                ]
                for label in accessible_label_must_include:
                    if label not in existing_accessible:
                        existing_accessible.append(label)
                surface["accessible_label_must_include"] = existing_accessible
            if operation_intent and not surface.get("operation_intent"):
                surface["operation_intent"] = operation_intent
            if display_label and not surface.get("control_label"):
                surface["control_label"] = display_label
            surface.setdefault("action_kinds", [])
            if action_kind not in surface["action_kinds"]:
                surface["action_kinds"].append(action_kind)
            source_steps = component.get("source_step_keys")
            if not isinstance(source_steps, list):
                source_steps = []
            trace_hit_points = component.get("trace_hit_points")
            if isinstance(trace_hit_points, list):
                for hit_point in trace_hit_points:
                    if isinstance(hit_point, dict):
                        _upsert_trace_hit_point(surface, hit_point)
            opened_candidate = _collect_opened_surface_candidate(
                filename=filename,
                component=component,
                interaction=interaction,
                action_kind=action_kind,
                source_steps=source_steps,
                render_roles_by_step=render_roles_by_step,
            )
            if opened_candidate:
                opened_surface_candidates.append(opened_candidate)
            for step_key in source_steps:
                step_text = str(step_key or "").strip()
                if not step_text:
                    continue
                _upsert_surface_step(
                    surface,
                    {"step_key": step_text, "source": "page_environment_spec"},
                )
            if not source_steps:
                _upsert_surface_step(
                    surface,
                    {
                        "step_key": str(component.get("component_id") or ""),
                        "source": "page_environment_spec",
                    },
                )
            _apply_render_role_lifecycle(surface, source_steps, render_roles_by_step)
            effect_type = "same_page_state"
            expected_filename = str(interaction.get("expected_filename") or filename)
            if expected_filename and expected_filename != filename:
                effect = {"type": "route", "target_filename": expected_filename}
            else:
                effect = {"type": effect_type}
            if effect not in surface["expected_effects"]:
                surface["expected_effects"].append(effect)
            if "page_environment_l0_source_host" not in surface["binding_sources"]:
                surface["binding_sources"].append("page_environment_l0_source_host")

    shell_broadcast = (
        page_environment_specs.get("shell_interaction_broadcast")
        if isinstance(page_environment_specs, dict)
        else None
    )
    propagated_interactions = (
        shell_broadcast.get("propagated_interactions")
        if isinstance(shell_broadcast, dict)
        else None
    )
    if isinstance(propagated_interactions, list):
        for item in propagated_interactions:
            if not isinstance(item, dict):
                continue
            source_filename = str(item.get("source_filename") or "").strip()
            if not source_filename:
                continue
            component_id = _shell_broadcast_surface_id(source_filename, item)
            key = (source_filename, component_id)
            surface = surfaces_by_key.get(key)
            if not surface:
                surface = {
                    "surface_id": component_id,
                    "component_ref": component_id,
                    "filename": source_filename,
                    "source": "shell_interaction_broadcast",
                    "surface_role": "shell_broadcast_interaction",
                    "required_in_state": "state::page_runtime",
                    "render_phase": "initial",
                    "host_availability_phase": "initial",
                    "initial_dom_eligible": True,
                    "initial_visibility_policy": "visible_initial",
                    "runtime_visibility_policy": "visible_initial",
                    "action_kinds": ["click"],
                    "required_dom_selector": _selector_for_component_id(component_id),
                    "required_data_element_id": component_id,
                    "required_display_label": str(item.get("control_label") or ""),
                    "control_label": str(item.get("control_label") or ""),
                    "surface": str(item.get("surface") or ""),
                    "shell_family_id": str(item.get("shell_family_id") or ""),
                    "interaction_kind": str(item.get("interaction_kind") or ""),
                    "visibility_required": True,
                    "topmost_required": True,
                    "interactive_required": True,
                    "dom_contract": {
                        "required_behavior": "visible_actionable_control",
                        "forbid_noninteractive_host": True,
                        "click_surface": "real_visible_component",
                    },
                    "expected_effects": [],
                    "evidence_steps": [],
                    "binding_sources": ["shell_interaction_broadcast"],
                    "value_lifecycle": {},
                    "sort_order": 3000,
                }
                surfaces_by_key[key] = surface
            effect = _shell_broadcast_expected_effect(item)
            if effect not in surface["expected_effects"]:
                surface["expected_effects"].append(effect)
            evidence = {
                "source": "shell_interaction_broadcast",
                "evidence_action_id": str(item.get("evidence_action_id") or ""),
                "evidence_source_filename": str(item.get("evidence_source_filename") or ""),
            }
            _upsert_surface_step(surface, evidence)
            if "shell_interaction_broadcast" not in surface["binding_sources"]:
                surface["binding_sources"].append("shell_interaction_broadcast")
            interaction_contract = (
                item.get("interaction_contract")
                if isinstance(item.get("interaction_contract"), dict)
                else {}
            )
            state_surface_contract = (
                interaction_contract.get("state_surface_contract")
                if isinstance(interaction_contract.get("state_surface_contract"), dict)
                else {}
            )
            open_state_id = str(
                state_surface_contract.get("open_state_id")
                or item.get("evidence_action_id")
                or item.get("control_label")
                or ""
            ).strip()
            if state_surface_contract and open_state_id:
                root_id = _state_surface_root_id(source_filename, item, open_state_id)
                root_key = (source_filename, root_id)
                if root_key not in surfaces_by_key:
                    surfaces_by_key[root_key] = {
                        "surface_id": root_id,
                        "component_ref": root_id,
                        "filename": source_filename,
                        "source": "shell_interaction_broadcast",
                        "surface_role": "shell_broadcast_state_surface",
                        "required_in_state": f"state::{open_state_id}",
                        "render_phase": "runtime",
                        "host_availability_phase": "runtime",
                        "initial_dom_eligible": False,
                        "initial_visibility_policy": "hidden_until_transition",
                        "runtime_visibility_policy": "visible_after_trigger",
                        "action_kinds": [],
                        "required_dom_selector": _selector_for_component_id(root_id),
                        "required_data_element_id": root_id,
                        "required_display_label": "",
                        "control_label": str(item.get("control_label") or ""),
                        "surface": str(item.get("surface") or ""),
                        "shell_family_id": str(item.get("shell_family_id") or ""),
                        "interaction_kind": str(item.get("interaction_kind") or ""),
                        "visibility_required": True,
                        "topmost_required": True,
                        "interactive_required": False,
                        "dom_contract": {
                            "required_behavior": "opened_state_surface",
                            "forbid_initial_visible": True,
                        },
                        "expected_effects": [],
                        "evidence_steps": [evidence],
                        "binding_sources": ["shell_interaction_broadcast"],
                        "state_surface_contract": state_surface_contract,
                        "contract_status": item.get("contract_status", "incomplete"),
                        "broadcast_generation_authority": item.get("broadcast_generation_authority", False),
                        "sort_order": 3010,
                    }
                options = state_surface_contract.get("options")
                if isinstance(options, list):
                    for option_index, option in enumerate(options):
                        if not isinstance(option, dict):
                            continue
                        label = str(option.get("label") or "").strip()
                        if not label:
                            continue
                        option_id = _state_surface_option_id(source_filename, item, open_state_id, label)
                        option_key = (source_filename, option_id)
                        option_effect = option.get("expected_effect") if isinstance(option.get("expected_effect"), dict) else {"type": "same_page_state"}
                        if option_key not in surfaces_by_key:
                            surfaces_by_key[option_key] = {
                                "surface_id": option_id,
                                "component_ref": option_id,
                                "filename": source_filename,
                                "source": "shell_interaction_broadcast",
                                "surface_role": "shell_broadcast_state_surface_option",
                                "required_in_state": f"state::{open_state_id}",
                                "render_phase": "runtime",
                                "host_availability_phase": "runtime",
                                "initial_dom_eligible": False,
                                "initial_visibility_policy": "hidden_until_transition",
                                "runtime_visibility_policy": "visible_after_trigger",
                                "action_kinds": [str(option.get("action_kind") or "click")],
                                "required_dom_selector": _selector_for_component_id(option_id),
                                "required_data_element_id": option_id,
                                "required_display_label": label,
                                "control_label": label,
                                "surface": str(item.get("surface") or ""),
                                "shell_family_id": str(item.get("shell_family_id") or ""),
                                "interaction_kind": "state_surface_option",
                                "visibility_required": True,
                                "topmost_required": True,
                                "interactive_required": bool(option.get("interactive_required", True)),
                                "dom_contract": {
                                    "required_behavior": "opened_state_surface_option",
                                    "forbid_noninteractive_host": True,
                                    "click_surface": "real_visible_component",
                                },
                                "expected_effects": [option_effect],
                                "evidence_steps": [evidence],
                                "binding_sources": ["shell_interaction_broadcast"],
                                "state_surface_contract": state_surface_contract,
                                "contract_status": item.get("contract_status", "incomplete"),
                                "broadcast_generation_authority": item.get("broadcast_generation_authority", False),
                                "sort_order": 3020 + option_index,
                             }

    _attach_opened_interaction_surfaces(surfaces_by_key, opened_surface_candidates)

    surfaces_by_filename: Dict[str, List[Dict[str, Any]]] = {}
    for (filename, _component_id), surface in sorted(
        surfaces_by_key.items(),
        key=lambda item: (
            item[0][0],
            item[1].get("sort_order", 1000),
            item[0][1],
        ),
    ):
        surfaces_by_filename.setdefault(filename, []).append(surface)

    surface_count = sum(len(items) for items in surfaces_by_filename.values())
    return {
        "format": "interaction_surface_contract",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.interaction_surfaces.contract",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "logical_owner": "interaction_surface_contract",
            "semantic_role": "dom_interaction_surface_requirements",
            "producer": "site_model.interaction_surface_contract.build_interaction_surface_contract",
            "allowed_consumers": [
                "pagegen",
                "pagegen_dom_contract_validator",
                "playwright_replay_executor",
                "developer",
            ],
            "debug_only": False,
        },
        "source_selection": {
            "derived_from": [
                "projections/l0_execution/l0_execution_pack.json",
                "phase_page_environment/semantic_api/06_page_environment_specs/page_environment_specs.json",
                "phase_page_environment/projections/page_environment/shell_interaction_broadcast.json",
            ],
        },
        "summary": {
            "page_count": len(surfaces_by_filename),
            "surface_count": surface_count,
            "opened_interaction_surface_count": sum(
                1
                for surfaces in surfaces_by_filename.values()
                for surface in surfaces
                if surface.get("surface_role") == "opened_interaction_surface"
            ),
            "executable_step_count": executable_steps,
            "skipped_step_count": skipped_steps,
            "manual_step_count": manual_steps,
            "unresolved_step_count": unresolved_steps,
        },
        "surfaces_by_filename": surfaces_by_filename,
    }


def required_component_ids_by_filename(
    interaction_surface_contract: Dict[str, Any],
) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    surfaces_by_filename = (
        interaction_surface_contract.get("surfaces_by_filename")
        if isinstance(interaction_surface_contract, dict)
        else None
    )
    if not isinstance(surfaces_by_filename, dict):
        return result
    for filename, surfaces in surfaces_by_filename.items():
        ids: List[str] = []
        if isinstance(surfaces, list):
            for surface in surfaces:
                if not isinstance(surface, dict):
                    continue
                if (
                    surface.get("initial_dom_eligible") is False
                    or str(surface.get("initial_visibility_policy") or "")
                    in {"hidden_until_transition", "runtime_only", "blocked_unknown"}
                ):
                    continue
                cid = str(
                    surface.get("required_data_element_id")
                    or surface.get("surface_id")
                    or ""
                )
                if cid and cid not in ids:
                    if surface.get("interactive_required") is False:
                        continue
                    ids.append(cid)
        if ids:
            result[str(filename)] = ids
    return result


def prompt_surfaces_for_filename(
    interaction_surface_contract: Dict[str, Any],
    filename: str,
    *,
    limit: int = 80,
) -> List[Dict[str, Any]]:
    surfaces_by_filename = (
        interaction_surface_contract.get("surfaces_by_filename")
        if isinstance(interaction_surface_contract, dict)
        else None
    )
    if not isinstance(surfaces_by_filename, dict):
        return []
    surfaces = surfaces_by_filename.get(filename) or []
    if not isinstance(surfaces, list):
        return []
    prompt_items: List[Dict[str, Any]] = []
    for surface in surfaces[:limit]:
        if not isinstance(surface, dict):
            continue
        item = {
            "surface_id": surface.get("surface_id"),
            "surface_role": surface.get("surface_role"),
            "required_data_element_id": surface.get("required_data_element_id"),
            "required_dom_selector": surface.get("required_dom_selector"),
            "required_display_label": surface.get("required_display_label"),
            "control_label": surface.get("control_label"),
            "label_authority": surface.get("label_authority"),
            "visible_text_contract": surface.get("visible_text_contract"),
            "visual_slot_key": surface.get("visual_slot_key"),
            "accessible_label_must_include": surface.get("accessible_label_must_include"),
            "context_text_must_include": surface.get("context_text_must_include"),
            "context_match_scope": surface.get("context_match_scope"),
            "operation_intent": surface.get("operation_intent"),
            "surface": surface.get("surface"),
            "surface_family": surface.get("surface_family"),
            "shell_family_id": surface.get("shell_family_id"),
            "interaction_kind": surface.get("interaction_kind"),
            "opened_surface_id": surface.get("opened_surface_id"),
            "opened_surface_option_id": surface.get("opened_surface_option_id"),
            "required_in_state": surface.get("required_in_state"),
            "render_phase": surface.get("render_phase"),
            "host_availability_phase": surface.get("host_availability_phase"),
            "initial_dom_eligible": surface.get("initial_dom_eligible"),
            "initial_visibility_policy": surface.get("initial_visibility_policy"),
            "runtime_visibility_policy": surface.get("runtime_visibility_policy"),
            "action_kinds": surface.get("action_kinds"),
            "expected_effects": surface.get("expected_effects"),
            "value_lifecycle": surface.get("value_lifecycle"),
            "dom_contract": surface.get("dom_contract"),
            "state_surface_contract": surface.get("state_surface_contract"),
            "required_data_element_aliases": surface.get("required_data_element_aliases"),
            "option_aliases": surface.get("option_aliases"),
            "contract_status": surface.get("contract_status"),
            "broadcast_generation_authority": surface.get("broadcast_generation_authority"),
            "evidence_steps": surface.get("evidence_steps"),
            "trace_hit_points": surface.get("trace_hit_points"),
            "binding_sources": surface.get("binding_sources"),
            "visibility_required": surface.get("visibility_required", True),
            "topmost_required": surface.get("topmost_required", True),
            "interactive_required": surface.get("interactive_required", True),
        }
        prompt_items.append({k: v for k, v in item.items() if v not in (None, "", [], {})})
    if len(surfaces) > limit:
        prompt_items.append(
            {
                "omitted_count": len(surfaces) - limit,
                "omitted_reason": "interaction_surface_prompt_limit",
            }
        )
    return prompt_items


def attach_interaction_surfaces_to_scopes(
    scopes: Dict[str, Dict[str, Any]],
    interaction_surface_contract: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(scopes, dict) or not isinstance(interaction_surface_contract, dict):
        return scopes
    updated: Dict[str, Dict[str, Any]] = {}
    for filename, scope in scopes.items():
        page_scope = dict(scope or {})
        surfaces = prompt_surfaces_for_filename(interaction_surface_contract, filename)
        if surfaces:
            page_scope["required_interaction_surfaces"] = surfaces
        updated[filename] = page_scope
    return updated


def write_interaction_surface_contract(path: str | Path, artifact: Dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)
