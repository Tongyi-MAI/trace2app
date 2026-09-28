
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple


BUTTON_LABEL_FIELDS = (
    "name",
    "title",
    "label",
    "text",
    "display_name",
    "displayText",
    "display_text",
    "channelName",
    "channel_name",
    "serviceName",
    "service_name",
    "categoryName",
    "category_name",
    "tabName",
    "tab_name",
)

BUTTON_ENTITY_MARKERS = (
    "shortcut",
    "channel",
    "category",
    "navigation",
    "nav",
    "tab",
    "menu",
    "entry",
    "service",
    "suggestion",
    "search",
)


def _base_filename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    return text.rsplit("/", 1)[-1]


def _dedupe(values: Iterable[str], limit: int = 64) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalize_label(text: Any) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"[\s“”\"'（）()\[\]【】,，。:：;；、/\\|_\-·]+", "", value)
    for suffix in ("按钮", "入口", "链接", "页签", "标签", "图标", "tab"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value


def _entity_looks_button_like(entity_name: str) -> bool:
    lowered = str(entity_name or "").lower()
    return any(marker in lowered for marker in BUTTON_ENTITY_MARKERS)


def _component_ref_for_entity(content_data: Dict[str, Any], entity_name: str) -> str:
    mapping = content_data.get("entity_component_mapping", []) if isinstance(content_data, dict) else []
    for item in mapping if isinstance(mapping, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("entity") or "").strip() == entity_name:
            return str(item.get("source_component") or item.get("component_ref") or "").strip()
    return ""


def _surface_for_item(entity_name: str, item: Dict[str, Any]) -> str:
    text = " ".join(
        str(value or "")
        for value in (
            entity_name,
            item.get("section_id"),
            item.get("section"),
            item.get("section_kind"),
            item.get("component"),
            item.get("component_kind"),
            item.get("component_role"),
            item.get("surface"),
        )
    ).lower()
    if "search" in text or "suggestion" in text:
        return "search_surface"
    if "bottom" in text or "footer" in text:
        return "bottom_navigation"
    if "tab" in text:
        return "tab_bar"
    if "category" in text or "channel" in text or "shortcut" in text:
        return "category_grid"
    return "visible_button"


def collect_source_controls(page: Any, *, limit: int = 64) -> List[Dict[str, Any]]:
    content_data = getattr(page, "content_data", {}) if isinstance(getattr(page, "content_data", {}), dict) else {}
    extracted_items = content_data.get("extracted_items", {}) if isinstance(content_data, dict) else {}
    if not isinstance(extracted_items, dict):
        return []
    controls: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for entity_name, items in extracted_items.items():
        if not isinstance(items, list) or not _entity_looks_button_like(str(entity_name)):
            continue
        component_ref = _component_ref_for_entity(content_data, str(entity_name))
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            label = ""
            for field in BUTTON_LABEL_FIELDS:
                value = str(item.get(field) or "").strip()
                if value:
                    label = value
                    break
            label_key = _normalize_label(label)
            if not label_key:
                continue
            surface = str(item.get("surface") or "").strip() or _surface_for_item(str(entity_name), item)
            key = (label_key, surface)
            if key in seen:
                continue
            seen.add(key)
            controls.append({
                "control_id": f"{getattr(page, 'page_id', '')}::{entity_name}::{index}",
                "component_key": f"{component_ref or entity_name}::{label}",
                "label": label,
                "label_key": label_key,
                "surface": surface,
                "component_ref": component_ref,
                "source_entity": str(entity_name),
            })
            if len(controls) >= limit:
                return controls
    return controls


def _action_text(step: Dict[str, Any]) -> str:
    action = step.get("action", {}) if isinstance(step.get("action"), dict) else {}
    return " ".join(
        str(action.get(key) or "")
        for key in ("intent", "action_text", "raw_action", "operation")
        if str(action.get(key) or "").strip()
    )


def _quoted_labels(text: str) -> List[str]:
    labels: List[str] = []
    for pattern in (r"[“\"']([^”\"']{1,32})[”\"']", r"【([^】]{1,32})】"):
        labels.extend(match.strip() for match in re.findall(pattern, text or "") if match.strip())
    return _dedupe(labels, limit=8)


def _best_control_for_step(step: Dict[str, Any], controls: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not controls:
        return None
    text = _action_text(step)
    text_key = _normalize_label(text)
    quoted_keys = {_normalize_label(label) for label in _quoted_labels(text)}
    best_rank: Tuple[int, int] = (-1, -1)
    best_control: Dict[str, Any] | None = None
    for control in controls:
        label = str(control.get("label") or "")
        label_key = str(control.get("label_key") or _normalize_label(label))
        if not label_key:
            continue
        score = 0
        if label_key in quoted_keys:
            score = 100
        elif label_key and label_key in text_key:
            score = 80
        elif text_key and text_key in label_key:
            score = 40
        if score <= 0:
            continue
        candidate_rank = (score, len(label_key))
        if candidate_rank > best_rank:
            best_rank = candidate_rank
            best_control = control
    return best_control


def _page_maps(site_model: Any) -> Tuple[Dict[str, Any], Dict[str, Dict[str, str]]]:
    structure = getattr(site_model, "structure", None)
    pages = list(getattr(structure, "page_prototypes", []) or []) if structure is not None else []
    by_filename: Dict[str, Any] = {}
    identity_by_filename: Dict[str, Dict[str, str]] = {}
    for page in pages:
        filename = _base_filename(getattr(page, "filename", ""))
        if not filename:
            continue
        by_filename[filename] = page
        structure_data = getattr(page, "structure_data", {}) if isinstance(getattr(page, "structure_data", {}), dict) else {}
        general_page_id = str(
            structure_data.get("cluster_id")
            or structure_data.get("source_cluster_id")
            or structure_data.get("general_page_id")
            or getattr(page, "general_page_id", "")
            or ""
        )
        instance_key = str(structure_data.get("instance_key") or structure_data.get("materialized_page_key") or "")
        materialized_page_id = str(getattr(page, "page_id", "") or "")
        if general_page_id and instance_key:
            materialized_page_id = f"{general_page_id}::{instance_key}"
        identity_by_filename[filename] = {
            "page_id": str(getattr(page, "page_id", "") or ""),
            "general_page_id": general_page_id,
            "materialized_page_id": materialized_page_id,
        }
    return by_filename, identity_by_filename


def build_canonical_route_bindings(l0_execution_pack: Dict[str, Any], site_model: Any = None) -> Dict[str, Any]:
    pages_by_filename, identity_by_filename = _page_maps(site_model)
    bindings: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    action_packs = l0_execution_pack.get("action_packs", []) if isinstance(l0_execution_pack, dict) else []
    for pack in action_packs if isinstance(action_packs, list) else []:
        if not isinstance(pack, dict):
            continue
        trace_id = str(pack.get("trace_id") or "")
        steps = pack.get("steps", []) if isinstance(pack.get("steps"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                continue
            action = step.get("action", {}) if isinstance(step.get("action"), dict) else {}
            operation = str(action.get("operation") or "").strip().lower()
            if operation in {"scroll", "swipe", "open_app"}:
                continue
            pre_state = step.get("pre_state", {}) if isinstance(step.get("pre_state"), dict) else {}
            post_state = step.get("expected_post_state", {}) if isinstance(step.get("expected_post_state"), dict) else {}
            source_filename = _base_filename(pre_state.get("filename"))
            target_filename = _base_filename(post_state.get("filename"))
            if not source_filename or not target_filename or source_filename == target_filename:
                continue
            source_page = pages_by_filename.get(source_filename)
            controls = collect_source_controls(source_page) if source_page is not None else []
            control = _best_control_for_step(step, controls)
            visible_label = str((control or {}).get("label") or "")
            if not visible_label:
                quoted = _quoted_labels(_action_text(step))
                visible_label = quoted[0] if quoted else ""
            label_key = _normalize_label(visible_label)
            if not label_key:
                rejected.append({
                    "code": "l0_route_binding_label_missing",
                    "trace_id": trace_id,
                    "step_ref": f"{trace_id}:step{step.get('step_index')}",
                    "source_filename": source_filename,
                    "target_filename": target_filename,
                })
                continue
            key = (source_filename, str((control or {}).get("surface") or ""), label_key)
            if key in seen:
                continue
            seen.add(key)
            source_identity = identity_by_filename.get(source_filename, {})
            target_identity = identity_by_filename.get(target_filename, {})
            missing = [
                name
                for name, value in (
                    ("source_materialized_page_id", source_identity.get("materialized_page_id")),
                    ("target_materialized_page_id", target_identity.get("materialized_page_id")),
                )
                if not value
            ]
            if missing:
                rejected.append({
                    "code": "l0_route_binding_identity_missing",
                    "trace_id": trace_id,
                    "step_ref": f"{trace_id}:step{step.get('step_index')}",
                    "source_filename": source_filename,
                    "target_filename": target_filename,
                    "missing": missing,
                })
                continue
            bindings.append({
                "binding_id": f"route.{source_filename}.{label_key}.{trace_id}.step{step.get('step_index')}",
                "source_filename": source_filename,
                "source_page_id": source_identity.get("page_id", ""),
                "source_general_page_id": source_identity.get("general_page_id", ""),
                "source_materialized_page_id": source_identity.get("materialized_page_id", ""),
                "source_component_key": str((control or {}).get("component_key") or visible_label),
                "source_component_ref": str((control or {}).get("component_ref") or ""),
                "visible_label": visible_label,
                "surface": str((control or {}).get("surface") or "visible_button"),
                "binding_kind": "category_entry" if str((control or {}).get("surface") or "") == "category_grid" else "visible_control",
                "target_filename": target_filename,
                "target_page_id": target_identity.get("page_id", ""),
                "target_general_page_id": target_identity.get("general_page_id", ""),
                "target_materialized_page_id": target_identity.get("materialized_page_id", ""),
                "binding_source": "l0_observed_reference_trace",
                "trace_id": trace_id,
                "step_ref": f"{trace_id}:step{step.get('step_index')}",
                "priority": "hard",
                "evidence": {
                    "operation": operation,
                    "intent": str(action.get("intent") or action.get("action_text") or ""),
                    "source_observation_key": str(step.get("source_observation_key") or ""),
                    "target_observation_key": str(post_state.get("observation_key") or ""),
                },
            })
    by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for binding in bindings:
        by_source[str(binding.get("source_filename") or "")].append(binding)
    return {
        "format": "canonical_route_bindings",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.page_environment.canonical_route_bindings",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "logical_owner": "canonical_route_bindings",
            "semantic_role": "l0_route_binding_projection",
            "producer": "site_model.route_binding_projection.build_canonical_route_bindings",
            "allowed_consumers": [
                "alternative_route_access_planner",
                "page_environment_compiler",
                "validator",
            ],
        },
        "source": "projections/l0_execution/l0_execution_pack.json",
        "bindings": bindings,
        "bindings_by_source_filename": dict(by_source),
        "rejected": rejected,
        "summary": {
            "binding_count": len(bindings),
            "rejected_count": len(rejected),
        },
    }
