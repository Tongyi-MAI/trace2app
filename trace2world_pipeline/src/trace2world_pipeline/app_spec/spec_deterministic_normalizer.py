
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Dict, List, Mapping, MutableMapping, Tuple


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return text or "state"


def _page_specs(payload: MutableMapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    specs = payload.get("page_environment_specs_by_filename")
    return specs if isinstance(specs, dict) else {}


def _base_filename(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return text.split("?")[0].split("#")[0].rsplit("/", 1)[-1]


def _transition_action_id(transition: Mapping[str, Any]) -> str:
    return _text(transition.get("step_key") or transition.get("action_id") or transition.get("transition_id"))


def _transition_for_action(page_spec: Mapping[str, Any], action_id: str) -> Dict[str, Any] | None:
    for transition in _as_list(page_spec.get("transitions")):
        if isinstance(transition, dict) and _transition_action_id(transition) == action_id:
            return transition
    return None


def _append_unique_runtime_state_ref(page_spec: Dict[str, Any], state_ref: str) -> None:
    if not state_ref:
        return
    state_schema = page_spec.setdefault("state_schema", {})
    if not isinstance(state_schema, dict):
        state_schema = {}
        page_spec["state_schema"] = state_schema
    runtime_refs = state_schema.setdefault("runtime_state_refs", [])
    if not isinstance(runtime_refs, list):
        runtime_refs = []
        state_schema["runtime_state_refs"] = runtime_refs
    normalized_refs: List[Dict[str, Any]] = []
    for item in runtime_refs:
        if isinstance(item, dict):
            item_ref = _text(item.get("state_ref") or item.get("state_scope"))
            if not item_ref:
                continue
            if not item_ref.startswith(("state::", "query::", "draft::")):
                item_ref = f"state::{_slug(item_ref)}"
            normalized = dict(item)
            normalized["state_ref"] = item_ref
            normalized.setdefault("state_scope", item_ref.replace("state::", ""))
            normalized.setdefault("source", "legacy_runtime_state_ref_dict")
            if not any(existing.get("state_ref") == item_ref for existing in normalized_refs):
                normalized_refs.append(normalized)
            continue
        item_ref = _text(item)
        if not item_ref:
            continue
        if not item_ref.startswith(("state::", "query::", "draft::")):
            item_ref = f"state::{_slug(item_ref)}"
        if not any(existing.get("state_ref") == item_ref for existing in normalized_refs):
            normalized_refs.append(
                {
                    "state_ref": item_ref,
                    "state_scope": item_ref.replace("state::", ""),
                    "source": "legacy_runtime_state_ref_string",
                }
            )
    if not any(item.get("state_ref") == state_ref for item in normalized_refs):
        normalized_refs.append(
            {
                "state_ref": state_ref,
                "state_scope": state_ref.replace("state::", ""),
                "source": "spec_deterministic_normalizer",
            }
        )
    state_schema["runtime_state_refs"] = normalized_refs


def _state_effect(transition: Dict[str, Any]) -> Dict[str, Any]:
    state_effect = transition.setdefault("state_effect", {})
    if not isinstance(state_effect, dict):
        state_effect = {}
        transition["state_effect"] = state_effect
    return state_effect


def _weak_candidates(transition: Mapping[str, Any]) -> set[str]:
    state_effect = _as_dict(transition.get("state_effect"))
    candidates: set[str] = set()
    for value in (transition.get("weak_effect_candidates"), state_effect.get("weak_effect_candidates")):
        if isinstance(value, list):
            candidates.update(_text(item) for item in value if _text(item))
    return candidates


def _operation_value(transition: Mapping[str, Any]) -> str:
    state_effect = _as_dict(transition.get("state_effect"))
    return (
        _text(state_effect.get("value"))
        or _text(transition.get("input_text"))
        or _text(transition.get("operation_intent"))
        or _text(transition.get("command"))
        or "active"
    )


def _dedupe_text(values: List[str], *, limit: int = 8) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _query_projection_kind(transition: Mapping[str, Any]) -> str:
    state_effect = _as_dict(transition.get("state_effect"))
    kind = _text(transition.get("query_transition_kind") or state_effect.get("query_transition_kind"))
    if kind:
        return kind
    reducer = _text(transition.get("reducer"))
    effect = _text(state_effect.get("effect"))
    if reducer in {"project_suggestions", "type_search"} or effect == "project_suggestions":
        return "suggestion_projection"
    return ""


def _query_projection_values(transition: Mapping[str, Any]) -> List[str]:
    state_effect = _as_dict(transition.get("state_effect"))
    values = [
        _text(state_effect.get("value")),
        _text(transition.get("input_text")),
        _text(transition.get("query_value")),
        _text(state_effect.get("query_value")),
    ]
    for field in _as_list(state_effect.get("committed_fields")):
        if isinstance(field, Mapping):
            values.append(_text(field.get("value")))
    return _dedupe_text(values)


def _ensure_query_projection_for_transition(
    page_spec: Dict[str, Any],
    transition: Dict[str, Any],
    *,
    action_id: str,
) -> bool:
    kind = _query_projection_kind(transition)
    if kind != "suggestion_projection":
        return False
    values = _query_projection_values(transition)
    if not values:
        return False

    state_effect = _state_effect(transition)
    source = _base_filename(transition.get("source_filename") or page_spec.get("filename") or page_spec.get("page_id"))
    target = _base_filename(transition.get("target_filename") or source)
    state_ref = _text(
        transition.get("query_state_owner")
        or state_effect.get("query_state_owner")
        or state_effect.get("state_ref")
        or state_effect.get("draft_ref")
    )
    if not state_ref:
        state_ref = f"draft::{_slug(source)}_{_slug(action_id)}"
        state_effect["draft_ref"] = state_ref
        state_effect["state_ref"] = state_ref

    projections = page_spec.setdefault("query_projections", [])
    if not isinstance(projections, list):
        projections = []
        page_spec["query_projections"] = projections

    changed = False
    for query in values:
        matched = False
        for projection in projections:
            if not isinstance(projection, dict):
                continue
            if action_id not in _projection_action_ids(projection):
                continue
            matched = True
            for list_key in ("step_keys", "source_step_keys", "action_ids"):
                ids = projection.setdefault(list_key, [])
                if not isinstance(ids, list):
                    ids = []
                    projection[list_key] = ids
                if action_id not in {_text(item) for item in ids}:
                    ids.append(action_id)
                    changed = True
            if not projection.get("query_value"):
                projection["query_value"] = query
                changed = True
        if matched:
            continue
        projections.append(
            {
                "query_identity": f"query::{_slug(query)}",
                "query_value": query,
                "field_role": _text(state_effect.get("field_role") or transition.get("field_role") or "business_query"),
                "source_filename": source,
                "target_filename": target,
                "query_transition_kind": kind,
                "query_state_owner": state_ref,
                "projection_policy": "project_runtime_suggestions_by_query",
                "source_kind": "transition",
                "step_keys": [action_id],
                "source_step_keys": [action_id],
                "action_ids": [action_id],
                "visible_initial_prefill_allowed": False,
                "source": "spec_deterministic_normalizer",
            }
        )
        changed = True

    if changed:
        _append_unique_runtime_state_ref(page_spec, state_ref)
    return changed


def _ensure_render_projection(page_spec: Dict[str, Any], action_id: str, state_ref: str, kind: str) -> None:
    render_projections = page_spec.setdefault("render_projections", [])
    if not isinstance(render_projections, list):
        render_projections = []
        page_spec["render_projections"] = render_projections
    needle = f"{action_id}::{kind}_render_projection"
    for item in render_projections:
        if not isinstance(item, dict) or item.get("projection_id") != needle:
            continue
        item["reads_state"] = state_ref
        refs = [_text(ref) for ref in _as_list(item.get("reads_state_refs")) if _text(ref)]
        if state_ref not in refs:
            refs.append(state_ref)
        item["reads_state_refs"] = refs
        return
    render_projections.append(
        {
            "projection_id": needle,
            "source": "spec_deterministic_normalizer",
            "action_id": action_id,
            "source_step_keys": [action_id],
            "l0_transition_ids": [action_id],
            "reads_state": state_ref,
            "reads_state_refs": [state_ref],
            "interaction_required": True,
            "dom_contract": {"required_behavior": "visible_actionable_control"},
            "render_contract": f"{kind} render projection must read the transaction state",
        }
    )


def _ensure_global_transaction_read_projection(
    page_spec: Dict[str, Any],
    *,
    action_id: str,
    state_refs: List[str],
) -> bool:
    clean_refs = [_text(ref) for ref in state_refs if _text(ref)]
    if not clean_refs:
        return False
    for ref in clean_refs:
        _append_unique_runtime_state_ref(page_spec, ref)
    render_projections = page_spec.setdefault("render_projections", [])
    if not isinstance(render_projections, list):
        render_projections = []
        page_spec["render_projections"] = render_projections
    projection_id = f"{action_id}::global_runtime_transaction_read_projection"
    for projection in render_projections:
        if isinstance(projection, dict) and projection.get("projection_id") == projection_id:
            existing_refs = [
                _text(ref)
                for ref in _as_list(projection.get("reads_state_refs"))
                if _text(ref)
            ]
            single_ref = _text(projection.get("reads_state"))
            if single_ref and single_ref not in existing_refs:
                existing_refs.append(single_ref)
            changed = False
            for ref in clean_refs:
                if ref not in existing_refs:
                    existing_refs.append(ref)
                    changed = True
            if changed:
                projection["source"] = "spec_deterministic_normalizer"
                projection["reads_state"] = existing_refs[0]
                projection["reads_state_refs"] = existing_refs
                projection.setdefault("action_id", action_id)
                projection.setdefault("source_step_keys", [action_id] if action_id else [])
                projection.setdefault("l0_transition_ids", [action_id] if action_id else [])
                projection["render_phase"] = "after_cross_page_transaction"
                projection["render_contract"] = "target page must read global runtime transaction state written by source page"
            return changed
    render_projections.append(
        {
            "projection_id": projection_id,
            "source": "spec_deterministic_normalizer",
            "action_id": action_id,
            "source_step_keys": [action_id] if action_id else [],
            "l0_transition_ids": [action_id] if action_id else [],
            "reads_state_refs": clean_refs,
            "render_phase": "after_cross_page_transaction",
            "render_contract": "target page must read global runtime transaction state written by source page",
        }
    )
    return True


def _oracle_types_for_kind(kind: str) -> List[str]:
    if kind == "record_selection":
        return ["state_mutation", "selected_record_hydration"]
    if kind in {"filter_selection", "filter"}:
        return ["state_mutation", "stateful_component_projection"]
    if kind == "query_suggestion":
        return ["state_mutation", "query_suggestion_projection"]
    return ["state_mutation"]


def _ensure_oracle(page_spec: Dict[str, Any], action_id: str, state_ref: str, expected_state_diff: Mapping[str, Any], kind: str) -> None:
    oracles = page_spec.setdefault("oracles", [])
    if not isinstance(oracles, list):
        oracles = []
        page_spec["oracles"] = oracles
    existing_ids = {
        str(item.get("oracle_id") or "")
        for item in oracles
        if isinstance(item, dict)
    }
    for oracle_type in _oracle_types_for_kind(kind):
        oracle_id = f"{action_id}::{kind}_{oracle_type}_oracle"
        if oracle_id in existing_ids:
            continue
        oracles.append(
            {
                "oracle_id": oracle_id,
                "source": "spec_deterministic_normalizer",
                "action_id": action_id,
                "step_key": action_id,
                "oracle_type": oracle_type,
                "state_refs": [state_ref],
                "expected_state_diff": dict(expected_state_diff),
            }
        )


def _primary_state_delta_value(delta: Mapping[str, Any]) -> str:
    for value in _as_list(delta.get("value_hints")):
        text = _text(value)
        if text:
            return text
    for key in ("value", "primary_value", "signature"):
        text = _text(delta.get(key))
        if text:
            return text
    return "observed"


def _state_delta_field(delta: Mapping[str, Any]) -> str:
    mutation = _text(delta.get("mutation"))
    if mutation:
        return _slug(mutation)
    lifecycle = _text(delta.get("lifecycle"))
    if lifecycle:
        return _slug(lifecycle)
    return "value"


def _state_delta_state_ref(page_filename: str, state_scope: str) -> str:
    page_slug = _slug(_base_filename(page_filename) or page_filename)
    scope_slug = _slug(state_scope)
    return f"state::{page_slug}_{scope_slug}"


def _typed_state_delta_semantics(deltas: List[Dict[str, Any]]) -> Dict[str, Any]:
    mutations = {
        _text(delta.get("mutation")).lower()
        for delta in deltas
        if _text(delta.get("mutation"))
    }
    if len(mutations) != 1:
        return {}
    mutation = next(iter(mutations))
    if mutation in {"add_to_cart", "increment_cart_quantity"}:
        return {
            "reducer": "update_cart_quantity",
            "effect": "update_cart_quantity",
            "effect_family": "cart_mutation",
            "delta": 1,
        }
    if mutation in {"remove_from_cart", "decrement_cart_quantity"}:
        return {
            "reducer": "update_cart_quantity",
            "effect": "update_cart_quantity",
            "effect_family": "cart_mutation",
            "delta": -1,
        }
    if mutation in {"select_category", "switch_category"}:
        return {
            "reducer": "switch_tab",
            "effect": "update_selection",
            "effect_family": "selection_mutation",
            "selection_kind": "category",
        }
    return {}


def _required_state_deltas_by_action(
    state_delta_signatures: Mapping[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    by_action: Dict[str, List[Dict[str, Any]]] = {}
    signatures_by_step = (
        state_delta_signatures.get("state_delta_signatures_by_step", {})
        if isinstance(state_delta_signatures, Mapping)
        else {}
    )
    if not isinstance(signatures_by_step, Mapping):
        return by_action
    seen: set[tuple[str, str]] = set()
    for deltas in signatures_by_step.values():
        if not isinstance(deltas, list):
            continue
        for raw_delta in deltas:
            if not isinstance(raw_delta, Mapping):
                continue
            if _text(raw_delta.get("coverage_class")) != "requires_spec_contract":
                continue
            action_id = _text(raw_delta.get("action_id"))
            state_scope = _text(raw_delta.get("state_scope"))
            if not action_id or not state_scope:
                continue
            signature = _text(raw_delta.get("signature")) or f"{action_id}:{state_scope}:{_text(raw_delta.get('mutation'))}"
            dedupe_key = (action_id, signature)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            by_action.setdefault(action_id, []).append(dict(raw_delta))
    return by_action


def apply_state_delta_signatures_to_page_environment_specs(
    page_environment_specs: Mapping[str, Any],
    state_delta_signatures: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:

    updated = deepcopy(page_environment_specs)
    specs_by_filename = _page_specs(updated)
    deltas_by_action = _required_state_deltas_by_action(state_delta_signatures)
    operations: List[Dict[str, Any]] = []

    for page_filename, page_spec in specs_by_filename.items():
        if not isinstance(page_spec, dict):
            continue
        canonical_filename = _base_filename(page_spec.get("filename") or page_filename) or page_filename
        for transition in _as_list(page_spec.get("transitions")):
            if not isinstance(transition, dict):
                continue
            action_id = _transition_action_id(transition)
            deltas = deltas_by_action.get(action_id)
            if not deltas:
                continue

            state_effect = _state_effect(transition)
            existing_diff = _as_dict(state_effect.get("expected_state_diff") or state_effect.get("state_diff"))
            expected_state_diff: Dict[str, Any] = dict(existing_diff)
            state_refs: List[str] = []
            materialized_deltas: List[Dict[str, Any]] = []
            typed_semantics = _typed_state_delta_semantics(deltas)
            for delta in deltas:
                state_scope = _text(delta.get("state_scope"))
                if not state_scope:
                    continue
                state_ref = _text(state_effect.get("state_ref"))
                if typed_semantics:


                    state_ref = _state_delta_state_ref(canonical_filename, state_scope)
                if not state_ref or not state_ref.startswith(("state::", "draft::", "query::")):
                    state_ref = _state_delta_state_ref(canonical_filename, state_scope)
                field = _state_delta_field(delta)
                diff_key = f"{state_ref}.{field}"
                expected_state_diff.setdefault(diff_key, _primary_state_delta_value(delta))
                if state_ref not in state_refs:
                    state_refs.append(state_ref)
                materialized_deltas.append(
                    {
                        "signature": _text(delta.get("signature")),
                        "state_scope": state_scope,
                        "mutation": _text(delta.get("mutation")),
                        "effect_obs_key": _text(delta.get("effect_obs_key")),
                        "value_hints": _as_list(delta.get("value_hints")),
                    }
                )

            if not materialized_deltas or not expected_state_diff:
                continue
            primary_state_ref = state_refs[0]
            reducer = _text(transition.get("reducer"))
            if typed_semantics:
                reducer = _text(typed_semantics.get("reducer"))
                transition["reducer"] = reducer
            elif not reducer or reducer == "scroll_viewport":
                reducer = "activate_inline_state"
                transition["reducer"] = reducer
            transition["l0_role"] = "execution_anchor"
            transition["effect_admission_status"] = "accepted"
            existing_effect = _text(state_effect.get("effect"))
            existing_effect_family = _text(state_effect.get("effect_family"))
            typed_effect = _text(typed_semantics.get("effect"))
            typed_effect_family = _text(typed_semantics.get("effect_family"))
            weak_effects = {"", "activate_inline_state", "state_delta_signature"}
            if typed_effect:
                state_effect["effect"] = typed_effect
                state_effect["effect_family"] = typed_effect_family or typed_effect
            elif existing_effect in weak_effects:
                state_effect["effect"] = "state_delta_signature"
                state_effect["effect_family"] = "state_delta_signature"
            else:


                state_effect["effect"] = existing_effect
                if existing_effect_family:
                    state_effect["effect_family"] = existing_effect_family
            state_effect["effect_admission_status"] = "accepted"
            state_effect["state_ref"] = primary_state_ref
            for key in ("delta", "selection_kind"):
                if typed_semantics.get(key) not in (None, "", [], {}):
                    state_effect[key] = typed_semantics[key]
            state_effect["expected_state_diff"] = expected_state_diff
            state_effect["state_diff"] = dict(expected_state_diff)
            existing_materialized = _as_list(state_effect.get("state_delta_signatures"))
            signatures_seen = {
                _text(item.get("signature"))
                for item in existing_materialized
                if isinstance(item, Mapping)
            }
            merged_materialized = [
                dict(item)
                for item in existing_materialized
                if isinstance(item, Mapping)
            ]
            for item in materialized_deltas:
                sig = _text(item.get("signature"))
                if sig and sig in signatures_seen:
                    continue
                merged_materialized.append(item)
                if sig:
                    signatures_seen.add(sig)
            state_effect["state_delta_signatures"] = merged_materialized
            for state_ref in state_refs:
                _append_unique_runtime_state_ref(page_spec, state_ref)
                _ensure_render_projection(page_spec, action_id, state_ref, "state_delta_signature")
            _ensure_oracle(page_spec, action_id, primary_state_ref, expected_state_diff, "state_delta_signature")
            operations.append(
                {
                    "operation": "materialize_state_delta_signature_contract",
                    "page_filename": canonical_filename,
                    "action_id": action_id,
                    "state_refs": state_refs,
                    "state_delta_signature_count": len(materialized_deltas),
                    "reducer": reducer,
                }
            )

    return updated, {
        "format": "state_delta_spec_injection_report",
        "schema_revision": 1,
        "operations": operations,
        "summary": {
            "applied_operation_count": len(operations),
            "required_action_count": len(deltas_by_action),
            "materialized_action_count": len({item["action_id"] for item in operations}),
            "materialized_signature_count": sum(
                int(item.get("state_delta_signature_count", 0) or 0)
                for item in operations
            ),
        },
    }


def _materialize_stateful_transaction(
    page_spec: Dict[str, Any],
    transition: Dict[str, Any],
    *,
    action_id: str,
    page_filename: str,
    kind: str = "stateful_action",
    force_reducer: str = "",
) -> bool:
    state_effect = _state_effect(transition)
    reducer = force_reducer or _text(transition.get("reducer") or state_effect.get("effect") or "activate_inline_state")
    state_ref = _text(state_effect.get("state_ref"))
    if not state_ref.startswith(("state::", "draft::", "query::")):
        state_ref = f"state::{_slug(page_filename)}_{_slug(reducer)}"
    diff_key = f"{state_ref}.active"
    expected_state_diff = _as_dict(state_effect.get("expected_state_diff") or state_effect.get("state_diff"))
    if not expected_state_diff:
        expected_state_diff = {diff_key: _operation_value(transition)}

    transition["reducer"] = reducer
    transition["effect_admission_status"] = "accepted"
    state_effect["effect"] = reducer
    state_effect["effect_admission_status"] = "accepted"
    state_effect["state_ref"] = state_ref
    state_effect["expected_state_diff"] = expected_state_diff
    state_effect["state_diff"] = dict(expected_state_diff)
    _append_unique_runtime_state_ref(page_spec, state_ref)
    _ensure_render_projection(page_spec, action_id, state_ref, kind)
    _ensure_oracle(page_spec, action_id, state_ref, expected_state_diff, kind)
    return True


def _split_query_projection_from_route(page_spec: Dict[str, Any], transition: Dict[str, Any], *, action_id: str) -> bool:
    source_filename = _text(transition.get("source_filename"))
    if not source_filename:
        return False
    transition["target_filename"] = source_filename
    route_oracle = transition.setdefault("route_oracle", {})
    if not isinstance(route_oracle, dict):
        route_oracle = {}
        transition["route_oracle"] = route_oracle
    route_oracle["expected_filename"] = source_filename
    state_effect = _state_effect(transition)
    state_effect["effect"] = "project_suggestions"
    state_effect["effect_admission_status"] = "accepted"
    state_effect["query_transition_kind"] = "suggestion_projection"
    state_effect.setdefault("mutation_policy", "update_draft_and_project_suggestions")
    state_ref = _text(state_effect.get("draft_ref") or state_effect.get("state_ref"))
    if not state_ref:
        state_ref = f"draft::{_slug(source_filename)}_{_slug(action_id)}"
        state_effect["draft_ref"] = state_ref
    value = _operation_value(transition)
    expected_state_diff = _as_dict(state_effect.get("expected_state_diff") or state_effect.get("state_diff"))
    if not expected_state_diff:
        expected_state_diff = {f"{state_ref}.business_query": value}
    state_effect["expected_state_diff"] = expected_state_diff
    state_effect["state_diff"] = dict(expected_state_diff)
    _append_unique_runtime_state_ref(page_spec, state_ref)
    _ensure_render_projection(page_spec, action_id, state_ref, "query_suggestion")
    _ensure_oracle(page_spec, action_id, state_ref, expected_state_diff, "query_suggestion")
    return True


def _projection_action_ids(projection: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("action_id", "step_key"):
        value = _text(projection.get(key))
        if value:
            ids.add(value)
    for key in ("step_keys", "source_step_keys", "l0_transition_ids", "transition_ids", "action_ids"):
        for value in _as_list(projection.get(key)):
            text = _text(value)
            if text:
                ids.add(text)
    return ids


def _suggestion_contract_has_rows(suggestion_contract: Mapping[str, Any]) -> bool:
    return bool(
        _as_list(suggestion_contract.get("suggestions"))
        or _as_list(suggestion_contract.get("expected_titles"))
        or _as_list(suggestion_contract.get("projected_items"))
    )


def _downgrade_empty_autocomplete_projection_to_draft(
    page_spec: Dict[str, Any],
    transition: Dict[str, Any],
    *,
    action_id: str,
) -> bool:

    state_effect = _state_effect(transition)
    source_filename = _text(transition.get("source_filename") or page_spec.get("filename"))
    value = _operation_value(transition)
    draft_ref = _text(state_effect.get("draft_ref") or state_effect.get("state_ref"))
    if not draft_ref:
        draft_ref = f"draft::{_slug(source_filename)}_{_slug(action_id)}"
    transition["reducer"] = "type_search"
    transition["effect_admission_status"] = "accepted"
    if source_filename:
        transition["target_filename"] = source_filename
        route_oracle = transition.setdefault("route_oracle", {})
        if not isinstance(route_oracle, dict):
            route_oracle = {}
            transition["route_oracle"] = route_oracle
        route_oracle["expected_filename"] = source_filename
    state_effect["effect"] = "update_draft"
    state_effect["effect_admission_status"] = "accepted"
    state_effect["draft_ref"] = draft_ref
    state_effect["state_ref"] = draft_ref
    state_effect["query_transition_kind"] = "query_entry"
    state_effect["mutation_policy"] = "update_draft_only"
    state_effect["value"] = value
    expected_state_diff = _as_dict(state_effect.get("expected_state_diff") or state_effect.get("state_diff"))
    if not expected_state_diff:
        expected_state_diff = {f"{draft_ref}.business_query": value}
    state_effect["expected_state_diff"] = expected_state_diff
    state_effect["state_diff"] = dict(expected_state_diff)
    _append_unique_runtime_state_ref(page_spec, draft_ref)

    render_projections = page_spec.get("render_projections")
    if isinstance(render_projections, list):
        for projection in render_projections:
            if not isinstance(projection, dict):
                continue
            if action_id not in _projection_action_ids(projection):
                continue
            suggestion_contract = _as_dict(projection.get("suggestion_contract"))
            if suggestion_contract and not _suggestion_contract_has_rows(suggestion_contract):
                projection.pop("suggestion_contract", None)
                projection["projection_kind"] = "query_draft"
                projection["render_phase"] = "after_input"
                projection["reads_state"] = draft_ref
                projection["reads_state_refs"] = [draft_ref]
                projection["render_contract"] = "query draft is updated; no autocomplete candidates were observed"
                projection["source"] = "spec_deterministic_normalizer"
    _ensure_oracle(page_spec, action_id, draft_ref, expected_state_diff, "query_draft")
    return True


def _canonicalize_typed_input_route_preview_targets(
    specs_by_filename: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    operations: List[Dict[str, Any]] = []
    for page_filename, page_spec in specs_by_filename.items():
        current = _base_filename(page_filename or page_spec.get("filename") or page_spec.get("page_id"))
        if not current:
            continue
        for transition in _as_list(page_spec.get("transitions")):
            if not isinstance(transition, dict):
                continue
            state_effect = _as_dict(transition.get("state_effect"))
            if _text(state_effect.get("commit_kind")) != "typed_input_route_preview":
                continue
            preview_target = _base_filename(state_effect.get("preview_target_filename"))
            source_filename = _base_filename(
                transition.get("source_filename")
                or state_effect.get("source_filename")
                or page_spec.get("filename")
            )
            direction = _text(transition.get("direction") or "source")
            old_target = _base_filename(transition.get("target_filename"))
            desired_target = ""
            if direction == "target" and preview_target and current == preview_target:
                desired_target = preview_target
            elif direction == "source" and source_filename:
                desired_target = source_filename
            if not desired_target:
                continue

            changed = False
            if old_target != desired_target:
                transition["target_filename"] = desired_target
                route_oracle = transition.get("route_oracle")
                if isinstance(route_oracle, dict):
                    route_oracle["expected_filename"] = desired_target
                changed = True

            effect_kind = _text(state_effect.get("query_transition_kind")) or "query_entry"
            if effect_kind and _text(transition.get("query_transition_kind")) != effect_kind:
                transition["query_transition_kind"] = effect_kind
                changed = True

            if _text(state_effect.get("effect")) == "update_draft":
                if _text(transition.get("reducer")) != "type_search":
                    transition["reducer"] = "type_search"
                    changed = True
                if _text(state_effect.get("mutation_policy")) != "update_draft_only":
                    state_effect["mutation_policy"] = "update_draft_only"
                    changed = True

            if changed:
                operations.append(
                    {
                        "operation": "canonicalize_typed_input_route_preview_target",
                        "page_filename": current,
                        "action_id": _transition_action_id(transition),
                        "direction": direction or "source",
                        "old_target_filename": old_target,
                        "target_filename": desired_target,
                    }
                )
    return operations


def _looks_like_record_selection(transition: Mapping[str, Any]) -> bool:
    text = " ".join(
        item
        for item in (
            _text(transition.get("operation_intent")),
            _text(transition.get("command")),
            _text(transition.get("input_text")),
        )
        if item
    )
    lowered = text.lower()
    if any(marker in text for marker in ("历史记录", "操作记录", "审批记录")) or "history" in lowered:
        return False
    return any(marker in text for marker in ("地址簿", "收件地址", "新华小区", "目的地", "候选", "条目", "条记录", "record"))


def _record_selection_payload(transition: Mapping[str, Any], target_filename: str) -> Dict[str, Any]:
    state_effect = _as_dict(transition.get("state_effect"))
    hydration = _as_dict(state_effect.get("selected_record_hydration") or transition.get("selected_record_hydration"))
    value = (
        _text(hydration.get("recipient_title"))
        or _text(hydration.get("title"))
        or _text(hydration.get("label"))
        or _operation_value(transition)
    )
    state_ref = _text(state_effect.get("state_ref")) or f"state::{_slug(target_filename)}_selected_record"
    expected_state_diff = _as_dict(state_effect.get("expected_state_diff") or state_effect.get("state_diff"))
    if not expected_state_diff:
        expected_state_diff = {f"{state_ref}.title": value}
    if not hydration:
        hydration = {"title": value, "recipient_title": value}
    else:
        if "title" not in hydration and value:
            hydration["title"] = value
        if "recipient_title" not in hydration and value:
            hydration["recipient_title"] = value
    return {
        "state_ref": state_ref,
        "expected_state_diff": expected_state_diff,
        "selected_record_hydration": hydration,
    }


def _materialize_record_selection(
    page_spec: Dict[str, Any],
    transition: Dict[str, Any],
    *,
    action_id: str,
) -> bool:
    target_filename = _text(transition.get("target_filename")) or _text(transition.get("source_filename"))
    if not target_filename:
        return False
    payload = _record_selection_payload(transition, target_filename)
    state_effect = _state_effect(transition)
    state_effect["effect_admission_status"] = "accepted"
    state_effect.setdefault("effect", "navigate_route")
    state_effect["state_ref"] = payload["state_ref"]
    state_effect["expected_state_diff"] = payload["expected_state_diff"]
    state_effect["state_diff"] = dict(payload["expected_state_diff"])
    state_effect["selected_record_hydration"] = payload["selected_record_hydration"]
    transition["effect_admission_status"] = "accepted"
    transition["target_hydration"] = payload["selected_record_hydration"]
    _append_unique_runtime_state_ref(page_spec, payload["state_ref"])
    _ensure_render_projection(page_spec, action_id, payload["state_ref"], "record_selection")
    _ensure_oracle(page_spec, action_id, payload["state_ref"], payload["expected_state_diff"], "record_selection")
    return True


def _propagate_selected_record_hydration(
    specs_by_filename: Dict[str, Dict[str, Any]],
    action_id: str,
) -> List[Dict[str, Any]]:
    matches: List[tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    template: Dict[str, Any] | None = None
    for filename, spec in specs_by_filename.items():
        transition = _transition_for_action(spec, action_id)
        if not transition:
            continue
        matches.append((filename, spec, transition))
        state_effect = _as_dict(transition.get("state_effect"))
        if state_effect.get("selected_record_hydration") or transition.get("selected_record_hydration"):
            target_filename = _text(transition.get("target_filename")) or filename
            template = _record_selection_payload(transition, target_filename)
    if not template:
        return []

    operations: List[Dict[str, Any]] = []
    for filename, spec, transition in matches:
        state_effect = _state_effect(transition)
        if state_effect.get("selected_record_hydration") and state_effect.get("expected_state_diff"):
            continue
        state_effect["effect_admission_status"] = "accepted"
        state_effect["state_ref"] = template["state_ref"]
        state_effect["expected_state_diff"] = dict(template["expected_state_diff"])
        state_effect["state_diff"] = dict(template["expected_state_diff"])
        state_effect["selected_record_hydration"] = dict(template["selected_record_hydration"])
        transition["effect_admission_status"] = "accepted"
        transition["target_hydration"] = dict(template["selected_record_hydration"])
        _append_unique_runtime_state_ref(spec, template["state_ref"])
        _ensure_render_projection(spec, action_id, template["state_ref"], "record_selection")
        _ensure_oracle(spec, action_id, template["state_ref"], template["expected_state_diff"], "record_selection")
        operations.append(
            {
                "operation": "propagate_selected_record_hydration",
                "page_filename": filename,
                "action_id": action_id,
                "state_ref": template["state_ref"],
            }
        )
    return operations


def normalize_spec_deterministically(
    page_environment_specs: Mapping[str, Any],
    routed_issues: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:

    updated = deepcopy(page_environment_specs)
    specs_by_filename = _page_specs(updated)
    operations: List[Dict[str, Any]] = []
    operations.extend(_canonicalize_typed_input_route_preview_targets(specs_by_filename))

    for route in _as_list(_as_dict(routed_issues).get("issue_routes")):
        if not isinstance(route, dict):
            continue
        code = _text(route.get("code") or route.get("issue_code"))
        page_filename = _text(route.get("page_filename") or route.get("filename"))
        action_id = _text(route.get("action_id"))
        if code == "global_runtime_transaction_read_missing":
            source_issue = _as_dict(route.get("source_issue"))
            target_page = _text(source_issue.get("target_page") or source_issue.get("target_filename"))
            state_writes = [_text(item) for item in _as_list(source_issue.get("state_writes")) if _text(item)]
            target_spec = specs_by_filename.get(target_page)
            if target_spec is not None and _ensure_global_transaction_read_projection(
                target_spec,
                action_id=action_id,
                state_refs=state_writes,
            ):
                operations.append(
                    {
                        "operation": "close_global_runtime_transaction_target_read",
                        "repair_class": _text(route.get("repair_class")),
                        "target_page": target_page,
                        "action_id": action_id,
                        "state_refs": state_writes,
                    }
                )
            continue
        if code == "selected_record_hydration_drift" and action_id:
            operations.extend(_propagate_selected_record_hydration(specs_by_filename, action_id))
            continue
        if not page_filename or "," in page_filename or page_filename not in specs_by_filename:
            continue
        spec = specs_by_filename[page_filename]
        transition = _transition_for_action(spec, action_id) if action_id else None
        if code in {
            "filter_selection_not_materialized",
            "stateful_action_has_empty_expected_state_diff",
            "unresolved_execution_effect",
        } and transition is not None:
            force_reducer = "filter_selection" if (
                code == "filter_selection_not_materialized" or "filter_selection" in _weak_candidates(transition)
            ) else ""
            reducer = force_reducer or _text(transition.get("reducer") or _as_dict(transition.get("state_effect")).get("effect"))
            if reducer and reducer not in {"agent_execute", "navigate_route", "scroll_viewport"}:
                if _materialize_stateful_transaction(
                    spec,
                    transition,
                    action_id=action_id,
                    page_filename=page_filename,
                    kind=force_reducer or reducer,
                    force_reducer=force_reducer,
                ):
                    operations.append(
                        {
                            "operation": "materialize_stateful_transaction",
                            "repair_class": _text(route.get("repair_class")),
                            "page_filename": page_filename,
                            "action_id": action_id,
                            "issue_code": code,
                        }
                    )
            continue
        if code == "query_projection_route_conflict" and transition is not None:
            state_effect = _as_dict(transition.get("state_effect"))
            reducer = _text(transition.get("reducer"))
            if reducer == "type_search" or _text(state_effect.get("effect")) == "update_draft":
                changed = _downgrade_empty_autocomplete_projection_to_draft(
                    spec,
                    transition,
                    action_id=action_id,
                )
                operation = "downgrade_query_projection_route_to_draft"
            else:
                changed = _split_query_projection_from_route(spec, transition, action_id=action_id)
                operation = "split_query_projection_from_route"
            if changed:
                operations.append(
                    {
                        "operation": operation,
                        "repair_class": _text(route.get("repair_class")),
                        "page_filename": page_filename,
                        "action_id": action_id,
                    }
                )
            continue
        if code == "autocomplete_candidate_rows_missing" and transition is not None:
            if _downgrade_empty_autocomplete_projection_to_draft(
                spec,
                transition,
                action_id=action_id,
            ):
                operations.append(
                    {
                        "operation": "downgrade_empty_autocomplete_projection_to_draft",
                        "repair_class": _text(route.get("repair_class")),
                        "page_filename": page_filename,
                        "action_id": action_id,
                    }
                )
            continue
        if code == "autocomplete_candidate_count_mismatch":
            query_states = spec.get("query_states") if isinstance(spec.get("query_states"), list) else []
            adjusted = False
            for qs in query_states:
                if not isinstance(qs, dict):
                    continue
                sc = qs.get("suggestion_contract") if isinstance(qs.get("suggestion_contract"), dict) else {}
                expected = int(sc.get("expected_candidate_count", 0) or 0)
                suggestions = sc.get("suggestions") if isinstance(sc.get("suggestions"), list) else []
                actual = len(suggestions)
                if expected > actual and actual >= 0:
                    sc["expected_candidate_count"] = actual
                    sc["_count_adjusted_by_normalizer"] = True
                    adjusted = True
            if adjusted:
                operations.append(
                    {
                        "operation": "adjust_autocomplete_expected_candidate_count",
                        "repair_class": _text(route.get("repair_class")),
                        "page_filename": page_filename,
                        "action_id": action_id,
                    }
                )
            continue
        if code == "missing_query_projection_spec" and transition is not None:
            if _ensure_query_projection_for_transition(spec, transition, action_id=action_id):
                operations.append(
                    {
                        "operation": "materialize_missing_query_projection_spec",
                        "repair_class": _text(route.get("repair_class")),
                        "page_filename": page_filename,
                        "action_id": action_id,
                    }
                )
            continue
        if code == "query_lifecycle_collapsed" and transition is not None:
            if _split_query_projection_from_route(spec, transition, action_id=action_id):
                operations.append(
                    {
                        "operation": "split_collapsed_query_lifecycle",
                        "repair_class": _text(route.get("repair_class")),
                        "page_filename": page_filename,
                        "action_id": action_id,
                    }
                )
            continue
        if code == "route_only_record_selection_missing_state_commit" and transition is not None:
            if _looks_like_record_selection(transition) and _materialize_record_selection(
                spec,
                transition,
                action_id=action_id,
            ):
                operations.append(
                    {
                        "operation": "materialize_record_selection_transaction",
                        "repair_class": _text(route.get("repair_class")),
                        "page_filename": page_filename,
                        "action_id": action_id,
                    }
                )
            continue
        if code != "state_ref_missing_from_schema":
            continue
        source_issue = _as_dict(route.get("source_issue"))
        missing_refs = [
            _text(ref)
            for ref in _as_list(_as_dict(source_issue.get("details")).get("missing_state_refs"))
            if _text(ref)
        ]
        if not missing_refs:
            continue
        state_schema = spec.setdefault("state_schema", {})
        if not isinstance(state_schema, dict):
            state_schema = {}
            spec["state_schema"] = state_schema
        runtime_refs = state_schema.setdefault("runtime_state_refs", [])
        if not isinstance(runtime_refs, list):
            runtime_refs = []
            state_schema["runtime_state_refs"] = runtime_refs
        added: List[str] = []
        for ref in missing_refs:
            if ref not in runtime_refs:
                runtime_refs.append(ref)
                added.append(ref)
        if added:
            operations.append(
                {
                    "operation": "declare_missing_state_refs",
                    "repair_class": _text(route.get("repair_class")),
                    "page_filename": page_filename,
                    "action_id": _text(route.get("action_id")),
                    "added_state_refs": added,
                }
            )

    return updated, {
        "format": "spec_deterministic_normalizer_report",
        "schema_revision": 1,
        "operations": operations,
        "summary": {
            "applied_operation_count": len(operations),
            "added_state_ref_count": sum(len(item.get("added_state_refs", [])) for item in operations),
        },
    }
