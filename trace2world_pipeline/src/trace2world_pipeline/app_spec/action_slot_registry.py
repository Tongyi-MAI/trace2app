
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from trace2world_pipeline.core.artifact_reuse import FALLBACK_BINDING_SOURCES


_ROUTE_ACTION_KIND = "navigate_to_target"
_INPUT_OPERATIONS = {"fill", "input", "type"}
_VARIANT_FIELDS = (
    "source_state_key",
    "state_key",
    "state_variant",
    "query_variant",
    "lifecycle",
    "lifecycle_variant",
)
_OPERATION_SKIP_SET = {"open_app", "launch_app", "scroll", "swipe"}

def _base_filename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    return Path(text).name


def _step_key(trace_id: str, step_index: Any) -> str:
    try:
        idx = int(step_index)
    except (TypeError, ValueError):
        idx = 0
    return f"{trace_id}:step{idx}" if trace_id else f"step{idx}"


def _stable_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _collect_variants(*objects: Dict[str, Any]) -> List[Tuple[str, str]]:
    variants: List[Tuple[str, str]] = []
    seen = set()
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for field in _VARIANT_FIELDS:
            if field not in obj:
                continue
            value = obj.get(field)
            if value in (None, "", [], {}):
                continue
            key = "state_variant" if field in {"source_state_key", "state_key"} else field
            pair = (key, _stable_value(value))
            if pair not in seen:
                seen.add(pair)
                variants.append(pair)
    return variants


def _source_state_key(
    step: Dict[str, Any],
    target: Dict[str, Any],
    pre_state: Dict[str, Any],
) -> str:
    variants = _collect_variants(target, step, pre_state)
    if not variants:
        return "default"
    return "|".join(f"{key}={value}" for key, value in variants)


def _operation(step: Dict[str, Any]) -> str:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    return str(action.get("operation") or "").lower().strip()


def _input_text(step: Dict[str, Any]) -> str:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    return str(action.get("input_text") or step.get("input_text") or "").strip()


def _is_route_step(step: Dict[str, Any]) -> tuple[bool, str, str]:
    if _operation(step) in _OPERATION_SKIP_SET:
        return False, "", ""
    pre_state = step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
    post_state = (
        step.get("expected_post_state")
        if isinstance(step.get("expected_post_state"), dict)
        else {}
    )
    source_filename = _base_filename(pre_state.get("filename"))
    target_filename = _base_filename(post_state.get("filename"))
    if not source_filename or not target_filename or source_filename == target_filename:
        return False, source_filename, target_filename
    return True, source_filename, target_filename


def _evidence(trace_id: str, step: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trace_id": trace_id,
        "step_index": step.get("step_index"),
        "step_key": _step_key(trace_id, step.get("step_index")),
    }


def _slot_id(
    source_filename: str,
    component_id: str,
    source_state_key: str,
    target_filename: str,
) -> str:
    return (
        f"slot::{source_filename}::{component_id}::"
        f"{source_state_key}::{_ROUTE_ACTION_KIND}::{target_filename}"
    )


def _filename_slug(filename: str) -> str:
    return _base_filename(filename).replace(".html", "").replace("-", "_")


def _slot_slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.split("::")[-1].strip() if "::" in text else text
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text, flags=re.UNICODE)
    return text.strip("_")


def _shell_slot_key(raw: Dict[str, Any]) -> str:
    for field in ("shell_slot_key", "normalized_label", "slot_id", "evidence_action_id", "control_label"):
        value = _slot_slug(raw.get(field))
        if value:
            return value
    return ""


def _admit_shell_broadcast_route(raw: Dict[str, Any]) -> tuple[bool, str, str]:
    if str(raw.get("source") or "").strip() != "shell_interaction_broadcast":
        return False, "", "not_shell_interaction_broadcast"
    shell_family = str(raw.get("shell_family_id") or raw.get("surface") or "").strip()
    if not shell_family:
        return False, "", "missing_shell_family"
    source_filename = _base_filename(raw.get("source_filename"))
    target_filename = _base_filename(raw.get("target_filename"))
    if not source_filename or not target_filename:
        return False, "", "missing_source_or_target"
    if source_filename == target_filename:
        return False, "", "self_target_shell_route"
    interaction_kind = str(raw.get("interaction_kind") or "navigate").strip().lower()
    if interaction_kind not in {"navigate", "route", "navigation"}:
        return False, "", "non_navigation_shell_interaction"
    slot_key = _shell_slot_key(raw)
    if not slot_key:
        slot_key = _filename_slug(target_filename)
    return True, slot_key, ""


def _shell_route_component_id(source_filename: str, slot_key: str) -> str:
    return f"cmp::{_filename_slug(source_filename)}_html::shell::bottom_tab_bar::{slot_key}"


def _operation_intent(step: Dict[str, Any]) -> str:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    for field in ("intent", "intent_normalized", "description", "text"):
        value = action.get(field)
        if value not in (None, ""):
            return str(value)
    return ""


def _query_source_state_key(query: str) -> str:
    return f"query_text={query}"


def _query_precondition(
    *,
    query: str,
    source_step_key: str,
    target_filename: str,
) -> Dict[str, Any]:
    return {
        "input_text": query,
        "source_step_key": source_step_key,
        "target_filename": target_filename,
    }


def _canonical_route_authority(
    canonical_route_bindings: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Set[Tuple[str, ...]]]]:
    if canonical_route_bindings is None:
        return None
    bindings = (
        canonical_route_bindings.get("bindings")
        if isinstance(canonical_route_bindings, dict)
        else None
    )
    if not isinstance(bindings, list):
        return {"step_refs": set(), "route_keys": set()}
    step_refs: Set[Tuple[str, ...]] = set()
    route_keys: Set[Tuple[str, ...]] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        binding_source = str(binding.get("binding_source") or "")
        if binding_source in FALLBACK_BINDING_SOURCES:
            continue
        source_filename = _base_filename(binding.get("source_filename"))
        target_filename = _base_filename(binding.get("target_filename"))
        if not source_filename or not target_filename:
            continue
        step_ref = str(binding.get("step_ref") or "").strip()
        if step_ref:
            step_refs.add((step_ref,))
        else:
            route_keys.add((source_filename, target_filename))
    return {"step_refs": step_refs, "route_keys": route_keys}


def _canonical_route_admission(
    *,
    authority: Optional[Dict[str, Set[Tuple[str, ...]]]],
    step_key: str,
    source_filename: str,
    target_filename: str,
) -> bool:
    if authority is None:
        return True
    if (step_key,) in authority.get("step_refs", set()):
        return True
    return (source_filename, target_filename) in authority.get("route_keys", set())


def _route_rejection(
    *,
    trace_id: str,
    step: Dict[str, Any],
    source_filename: str,
    target_filename: str,
    reason: str,
    component_id: str = "",
    status: str = "unresolved",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "trace_id": trace_id,
        "step_index": step.get("step_index"),
        "step_key": _step_key(trace_id, step.get("step_index")),
        "source_filename": source_filename,
        "target_filename": target_filename,
        "status": status,
        "reason": reason,
    }
    if component_id:
        payload["component_id"] = component_id
    if extra:
        payload.update(
            {
                key: value
                for key, value in extra.items()
                if value not in (None, "", [], {})
            }
        )
    return payload


def build_action_slot_registry(
    l0_execution_pack: Dict[str, Any],
    *,
    shell_interaction_broadcast: Optional[Dict[str, Any]] = None,
    query_domain_filenames: Optional[set] = None,
    canonical_route_bindings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    action_packs = (
        l0_execution_pack.get("action_packs")
        if isinstance(l0_execution_pack, dict)
        else None
    )
    if not isinstance(action_packs, list):
        action_packs = []

    slots_by_identity: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    unresolved_route_steps: List[Dict[str, Any]] = []
    action_slot_rejections: List[Dict[str, Any]] = []
    rejected_shell_routes: List[Dict[str, Any]] = []
    route_step_count = 0
    shell_route_slot_count = 0
    active_query_by_trace_source: Dict[Tuple[str, str], Dict[str, Any]] = {}
    route_authority = _canonical_route_authority(canonical_route_bindings)

    for pack in action_packs:
        if not isinstance(pack, dict):
            continue
        pack_trace_id = str(pack.get("trace_id") or "")
        steps = pack.get("steps") if isinstance(pack.get("steps"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                continue
            trace_id = str(step.get("trace_id") or pack_trace_id)
            pre_state = (
                step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
            )
            source_for_state = _base_filename(pre_state.get("filename"))
            operation = _operation(step)
            input_text = _input_text(step)
            _qdf = query_domain_filenames or set()
            is_route, source_filename, target_filename = _is_route_step(step)
            if (
                operation in _INPUT_OPERATIONS
                and input_text
                and source_for_state
                and source_for_state in _qdf
            ):
                active_query_by_trace_source[(trace_id, source_for_state)] = {
                    "input_text": input_text,
                    "step_key": _step_key(trace_id, step.get("step_index")),
                    "field_role": "business_query",
                }
            if operation in _INPUT_OPERATIONS:
                if is_route:
                    route_step_count += 1
                    target = step.get("target") if isinstance(step.get("target"), dict) else {}
                    rejection = _route_rejection(
                        trace_id=trace_id,
                        step=step,
                        source_filename=source_filename,
                        target_filename=target_filename,
                        reason="query_lifecycle_not_route_slot",
                        component_id=str(target.get("component_id") or "").strip(),
                        extra={
                            "operation": operation,
                            "input_text": input_text,
                            "binding_source": target.get("binding_source"),
                        },
                    )
                    unresolved_route_steps.append(rejection)
                    action_slot_rejections.append(rejection)
                continue
            if not is_route:
                continue
            route_step_count += 1
            target = step.get("target") if isinstance(step.get("target"), dict) else {}
            status = str(target.get("status") or "")
            component_id = str(target.get("component_id") or "").strip()
            if status != "resolved" or not component_id:
                unresolved_route_steps.append(
                    {
                        "trace_id": trace_id,
                        "step_index": step.get("step_index"),
                        "step_key": _step_key(trace_id, step.get("step_index")),
                        "source_filename": source_filename,
                        "target_filename": target_filename,
                        "status": status or "missing",
                        "reason": "missing_resolved_component_id",
                    }
                )
                continue
            binding_source = str(target.get("binding_source") or "")
            if binding_source in FALLBACK_BINDING_SOURCES:
                rejection = {
                    "trace_id": trace_id,
                    "step_index": step.get("step_index"),
                    "step_key": _step_key(trace_id, step.get("step_index")),
                    "source_filename": source_filename,
                    "target_filename": target_filename,
                    "status": "unresolved",
                    "reason": "fallback_binding_not_formal",
                    "rejected_binding_source": binding_source,
                    "component_id": component_id,
                }
                unresolved_route_steps.append(rejection)
                action_slot_rejections.append(rejection)
                continue
            step_key = _step_key(trace_id, step.get("step_index"))
            if not _canonical_route_admission(
                authority=route_authority,
                step_key=step_key,
                source_filename=source_filename,
                target_filename=target_filename,
            ):
                rejection = _route_rejection(
                    trace_id=trace_id,
                    step=step,
                    source_filename=source_filename,
                    target_filename=target_filename,
                    reason="noncanonical_route_step",
                    component_id=component_id,
                    extra={"binding_source": binding_source},
                )
                unresolved_route_steps.append(rejection)
                action_slot_rejections.append(rejection)
                continue

            active_query = active_query_by_trace_source.get((trace_id, source_filename), {})
            query_text = str(active_query.get("input_text") or "").strip()
            source_state_key = _source_state_key(step, target, pre_state)
            if source_state_key == "default" and query_text:
                source_state_key = _query_source_state_key(query_text)
            identity = (
                source_filename,
                component_id,
                source_state_key,
                _ROUTE_ACTION_KIND,
                target_filename,
            )
            slot = slots_by_identity.get(identity)
            evidence = _evidence(trace_id, step)
            if slot is None:
                slot = {
                    "action_slot_id": _slot_id(
                        source_filename,
                        component_id,
                        source_state_key,
                        target_filename,
                    ),
                    "source_filename": source_filename,
                    "component_id": component_id,
                    "source_state_key": source_state_key,
                    "action_kind": _ROUTE_ACTION_KIND,
                    "target_filename": target_filename,
                    "executor": "GhostwebRouteExecution.navigate",
                    "sdk_side_effect": "WebsiteSDK.navigateRoute",
                    "history_mode": "push",
                    "operation": _operation(step) or "click",
                    "operation_intent": _operation_intent(step),
                    "binding_source": binding_source,
                    "evidence_steps": [],
                }
                if query_text:
                    slot["input_text"] = query_text
                    slot["query_precondition"] = _query_precondition(
                        query=query_text,
                        source_step_key=str(active_query.get("step_key") or ""),
                        target_filename=target_filename,
                    )
                    slot["precondition"] = {
                        "kind": "query_text_equals",
                        "value": query_text,
                    }
                    slot["state_binding"] = {
                        "kind": "search_draft",
                        "value": query_text,
                    }
                slots_by_identity[identity] = slot
            if evidence not in slot["evidence_steps"]:
                slot["evidence_steps"].append(evidence)

    propagated_routes = (
        shell_interaction_broadcast.get("propagated_routes")
        if isinstance(shell_interaction_broadcast, dict)
        else None
    )
    if isinstance(propagated_routes, list):
        for raw in propagated_routes:
            if not isinstance(raw, dict):
                continue
            admitted, slot_key, rejection_reason = _admit_shell_broadcast_route(raw)
            source_filename = _base_filename(raw.get("source_filename"))
            target_filename = _base_filename(raw.get("target_filename"))
            if not admitted:
                rejected_shell_routes.append(
                    {
                        "source_filename": source_filename,
                        "target_filename": target_filename,
                        "control_label": raw.get("control_label"),
                        "shell_family_id": raw.get("shell_family_id"),
                        "evidence_action_id": raw.get("evidence_action_id"),
                        "reason": rejection_reason or "invalid_shell_broadcast_route",
                    }
                )
                continue
            component_id = str(raw.get("component_id") or "").strip()
            if not component_id:
                component_id = _shell_route_component_id(source_filename, slot_key)
            source_state_key = "default"
            identity = (
                source_filename,
                component_id,
                source_state_key,
                _ROUTE_ACTION_KIND,
                target_filename,
            )
            slot = slots_by_identity.get(identity)
            evidence = {
                "source": "shell_interaction_broadcast",
                "evidence_action_id": raw.get("evidence_action_id"),
                "evidence_source_filename": raw.get("evidence_source_filename"),
                "control_label": raw.get("control_label"),
            }
            if slot is None:
                slot = {
                    "action_slot_id": _slot_id(
                        source_filename,
                        component_id,
                        source_state_key,
                        target_filename,
                    ),
                    "source_filename": source_filename,
                    "component_id": component_id,
                    "source_state_key": source_state_key,
                    "action_kind": _ROUTE_ACTION_KIND,
                    "target_filename": target_filename,
                    "executor": "GhostwebRouteExecution.navigate",
                    "sdk_side_effect": "WebsiteSDK.navigateRoute",
                    "history_mode": "push",
                    "operation": "click",
                    "operation_intent": f"点击底部导航“{raw.get('control_label') or slot_key}”",
                    "control_label": raw.get("control_label"),
                    "shell_family_id": raw.get("shell_family_id") or raw.get("surface") or "",
                    "shell_slot_key": slot_key,
                    "binding_source": "shell_interaction_broadcast",
                    "binding_sources": ["shell_interaction_broadcast"],
                    "evidence_steps": [],
                }
                slots_by_identity[identity] = slot
                shell_route_slot_count += 1
            if evidence not in slot["evidence_steps"]:
                slot["evidence_steps"].append(evidence)

    slots = sorted(
        slots_by_identity.values(),
        key=lambda item: (
            item["source_filename"],
            item["component_id"],
            item["source_state_key"],
            item["target_filename"],
        ),
    )
    slots_by_source_filename: Dict[str, List[Dict[str, Any]]] = {}
    for slot in slots:
        slots_by_source_filename.setdefault(slot["source_filename"], []).append(slot)

    conflict_groups: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    for slot in slots:
        key = (
            slot["source_filename"],
            slot["component_id"],
            slot["source_state_key"],
            slot["action_kind"],
        )
        conflict_groups.setdefault(key, []).append(slot)

    conflicts: List[Dict[str, Any]] = []
    for (source_filename, component_id, source_state_key, action_kind), group in sorted(
        conflict_groups.items()
    ):
        target_filenames = sorted({slot["target_filename"] for slot in group})
        if len(target_filenames) <= 1:
            continue
        evidence_steps: List[Dict[str, Any]] = []
        slot_ids: List[str] = []
        for slot in group:
            slot_ids.append(slot["action_slot_id"])
            for evidence in slot.get("evidence_steps", []):
                if evidence not in evidence_steps:
                    evidence_steps.append(evidence)
        conflicts.append(
            {
                "source_filename": source_filename,
                "component_id": component_id,
                "source_state_key": source_state_key,
                "action_kind": action_kind,
                "target_filenames": target_filenames,
                "action_slot_ids": sorted(slot_ids),
                "evidence_steps": evidence_steps,
                "blocking_reason": "same_component_source_state_multi_target_route",
            }
        )

    component_keys = {
        (
            slot["source_filename"],
            slot["component_id"],
            slot["source_state_key"],
            slot["action_kind"],
        )
        for slot in slots
    }
    return {
        "format": "action_slot_registry",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.action_slots.registry",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": True,
            "logical_owner": "action_slot_registry",
            "semantic_role": "route_action_slot_registry",
            "producer": "site_model.action_slot_registry.build_action_slot_registry",
            "allowed_consumers": [
                "pagegen",
                "runtime",
                "playwright_replay_executor",
                "pagegen_dom_contract_validator",
                "developer",
            ],
            "debug_only": False,
        },
        "source_selection": {
            "derived_from": [
                "projections/l0_execution/l0_execution_pack.json",
                "phase_consolidation/canonical_routes/canonical_route_bindings.json",
                "phase_consolidation/site_model/step_component_bindings.json",
                "phase_page_environment/projections/page_environment/shell_interaction_broadcast.json",
            ],
            "requires_resolved_component_binding": True,
        },
        "summary": {
            "slot_count": len(slots),
            "route_slot_count": len(slots),
            "shell_route_slot_count": shell_route_slot_count,
            "route_step_count": route_step_count,
            "component_instance_count": len(component_keys),
            "conflict_count": len(conflicts),
            "unresolved_route_step_count": len(unresolved_route_steps),
            "rejected_shell_route_count": len(rejected_shell_routes),
            "action_slot_rejection_count": len(action_slot_rejections),
        },
        "blocking_policy": {
            "gate": "component_id_source_state_multi_target_route",
            "config_flag": "action_slot_route_hard_gate_enabled",
            "hard_gate_when_conflict_count_gt_zero": True,
            "allowed_variant_fields": [
                "state_variant",
                "query_variant",
                "lifecycle",
                "lifecycle_variant",
            ],
        },
        "slots": slots,
        "slots_by_source_filename": slots_by_source_filename,
        "conflicts": conflicts,
        "unresolved_route_steps": unresolved_route_steps,
        "action_slot_rejections": action_slot_rejections,
        "rejected_shell_routes": rejected_shell_routes,
    }


def action_slot_conflict_count(registry: Dict[str, Any]) -> int:
    summary = registry.get("summary") if isinstance(registry, dict) else {}
    if not isinstance(summary, dict):
        return 0
    try:
        return int(summary.get("conflict_count") or 0)
    except (TypeError, ValueError):
        return 0


def prompt_action_slots_for_filename(
    action_slot_registry: Dict[str, Any],
    filename: str,
    *,
    limit: int = 80,
) -> List[Dict[str, Any]]:
    slots_by_source = (
        action_slot_registry.get("slots_by_source_filename")
        if isinstance(action_slot_registry, dict)
        else None
    )
    if not isinstance(slots_by_source, dict):
        return []
    slots = slots_by_source.get(filename) or []
    if not isinstance(slots, list):
        return []
    prompt_items: List[Dict[str, Any]] = []
    for slot in slots[:limit]:
        if not isinstance(slot, dict):
            continue
        item = {
            "action_slot_id": slot.get("action_slot_id"),
            "component_id": slot.get("component_id"),
            "source_state_key": slot.get("source_state_key", "default"),
            "action_kind": slot.get("action_kind", _ROUTE_ACTION_KIND),
            "target_filename": slot.get("target_filename"),
            "executor": slot.get("executor", "GhostwebRouteExecution.navigate"),
            "sdk_side_effect": slot.get("sdk_side_effect", "WebsiteSDK.navigateRoute"),
            "history_mode": slot.get("history_mode", "push"),
            "operation": slot.get("operation"),
            "operation_intent": slot.get("operation_intent"),
            "control_label": slot.get("control_label"),
            "shell_family_id": slot.get("shell_family_id"),
            "shell_slot_key": slot.get("shell_slot_key"),
            "binding_source": slot.get("binding_source"),
            "binding_sources": slot.get("binding_sources"),
            "query_precondition": slot.get("query_precondition"),
            "precondition": slot.get("precondition"),
            "state_binding": slot.get("state_binding"),
            "input_text": slot.get("input_text"),
            "evidence_steps": slot.get("evidence_steps"),
        }
        prompt_items.append(
            {key: value for key, value in item.items() if value not in (None, "", [], {})}
        )
    if len(slots) > limit:
        prompt_items.append(
            {
                "omitted_count": len(slots) - limit,
                "omitted_reason": "action_slot_prompt_limit",
            }
        )
    return prompt_items


def write_action_slot_registry(path: str | Path, artifact: Dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)
