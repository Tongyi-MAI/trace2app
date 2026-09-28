
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


BLOCKING = "blocking"
WARNING = "warning"


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_state_ref(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if text.startswith("state::"):
        namespace, _, rest = text.partition("::")
        if "." in rest:
            return f"{namespace}::{rest.replace('.', '_')}"
        return text
    if text.startswith(("draft::", "query::", "collection::", "record::", "declared::", "missing::")):
        return text
    if text.startswith("c_") and "::" in text:
        return f"state::{text.replace('::', '_').replace('.', '_')}"
    if text.startswith("c_") and "." in text:
        return f"state::{text.replace('.', '_')}"
    if "::" in text:
        return text
    return f"state::{text}"


def _trace_id_for_step(step_key: str) -> str:
    if ":step" in step_key:
        return step_key.split(":step", 1)[0]
    if ":" in step_key:
        return step_key.split(":", 1)[0]
    return step_key or "unknown_trace"


def _action_sort_key(action_id: Any) -> Tuple[str, int, str]:
    text = _text(action_id)
    match = re.match(r"^(.+):step(\d+)$", text)
    if match:
        return match.group(1), int(match.group(2)), text
    return _trace_id_for_step(text), 10**9, text


def _state_refs_from_schema(schema: Mapping[str, Any]) -> Set[str]:
    refs: Set[str] = set()
    runtime_refs = schema.get("runtime_state_refs")
    if isinstance(runtime_refs, list):
        for item in runtime_refs:
            if isinstance(item, dict):
                ref = _canonical_state_ref(item.get("state_ref") or item.get("state_scope") or item.get("name"))
                if ref:
                    refs.add(ref)
            elif _text(item):
                refs.add(_canonical_state_ref(item))
    fields = schema.get("fields")
    if isinstance(fields, dict):
        refs.update(_text(key) for key in fields if _text(key))
    if isinstance(fields, list):
        for item in fields:
            if isinstance(item, dict):
                ref = _text(item.get("state_ref") or item.get("name") or item.get("field"))
                if ref:
                    refs.add(ref)
            elif _text(item):
                refs.add(_text(item))
    return refs


def _state_refs_from_value(value: Any) -> Set[str]:
    refs: Set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"state_ref", "target_state_ref", "source_state_ref"} and _text(item):
                refs.add(_canonical_state_ref(item))
            refs.update(_state_refs_from_value(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_state_refs_from_value(item))
    return refs


def _contains_key(value: Any, key_name: str) -> bool:
    if isinstance(value, dict):
        if key_name in value:
            return True
        return any(_contains_key(item, key_name) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key_name) for item in value)
    return False


def _contains_selected_record_hydration(transition: Mapping[str, Any]) -> bool:
    reducer = _text(transition.get("reducer") or transition.get("action_intent"))
    if reducer == "selected_record_hydration":
        return True
    state_effect = _as_dict(transition.get("state_effect"))
    effect = _text(state_effect.get("effect"))
    return effect == "selected_record_hydration" or _contains_key(transition, "selected_record_hydration")


def _transition_status(transition: Mapping[str, Any]) -> str:
    state_effect = _as_dict(transition.get("state_effect"))
    return _text(
        transition.get("effect_admission_status")
        or state_effect.get("effect_admission_status")
        or transition.get("admission_status")
    )


def _weak_candidates(transition: Mapping[str, Any]) -> List[str]:
    state_effect = _as_dict(transition.get("state_effect"))
    candidates: List[str] = []
    for value in (transition.get("weak_effect_candidates"), state_effect.get("weak_effect_candidates")):
        if isinstance(value, list):
            candidates.extend(_text(item) for item in value if _text(item))
    return list(dict.fromkeys(candidates))


def _effect_families(transition: Mapping[str, Any]) -> Set[str]:
    state_effect = _as_dict(transition.get("state_effect"))
    values = {
        _text(transition.get("reducer")),
        _text(transition.get("action_intent")),
        _text(state_effect.get("effect")),
        _text(state_effect.get("reducer")),
    }
    values.update(_weak_candidates(transition))
    return {item for item in values if item}


def _pure_empty_diff_reducer(reducer: str) -> bool:
    return reducer in {
        "agent_execute",
        "navigate_route",
        "open_app",
        "open_overlay",
        "open_runtime_overlay",
        "open_search_entry",
        "scroll_viewport",
    }


def _is_stateful_l0_action_without_diff(transition: Mapping[str, Any], expected_state_diff: Mapping[str, Any]) -> bool:
    if expected_state_diff:
        return False
    if _text(transition.get("l0_role")) != "execution_anchor":
        return False
    operation = _text(transition.get("operation"))
    if operation not in {"click", "fill", "type"}:
        return False
    reducer = _text(transition.get("reducer") or transition.get("action_intent"))
    if _pure_empty_diff_reducer(reducer):
        return False
    families = _effect_families(transition)
    return (
        _transition_status(transition) == "unresolved"
        or reducer == "activate_inline_state"
        or bool(
            families
            & {
                "cart_mutation",
                "commit_query",
                "filter_selection",
                "option_overlay",
                "payment_selection",
                "record_selection",
                "review_or_tab_selection",
                "select_address_record",
                "select_payment_method",
                "select_record",
                "select_sku_option",
                "select_vehicle_option",
            }
        )
    )


_RETURN_REDUCERS = frozenset({
    "navigate_back",
    "return_to_parent",
    "go_back",
    "close_modal",
    "close_overlay",
    "close_sheet",
    "close_drawer",
    "pop_history",
    "finish_activity",
})

_RETURN_OPERATION_TOKENS = frozenset({
    "返回", "关闭", "back", "close", "dismiss", "cancel_overlay",
})

_COMMIT_REPLACE_REDUCERS = frozenset({
    "commit_query",
    "submit_form",
    "commit_transaction",
    "terminal_submit",
    "terminal_commit",
    "commit_order",
    "commit_booking",
    "commit_draft",
})


def _is_return_navigation(transition: Mapping[str, Any]) -> bool:
    reducer = _text(transition.get("reducer") or transition.get("action_intent")).lower()
    if reducer in _RETURN_REDUCERS:
        return True
    operation = _text(transition.get("operation")).lower()
    operation_intent = _text(transition.get("operation_intent")).lower()
    for token in _RETURN_OPERATION_TOKENS:
        if token in operation or token in operation_intent:
            return True
    state_effect = _as_dict(transition.get("state_effect"))
    effect = _text(state_effect.get("effect")).lower()
    return effect in _RETURN_REDUCERS


def _navigation_kind(transition: Mapping[str, Any]) -> str:
    if _is_return_navigation(transition):
        return "return"
    return "forward"


def _history_mode(transition: Mapping[str, Any]) -> str:
    reducer = _text(transition.get("reducer") or transition.get("action_intent")).lower()
    if reducer in _COMMIT_REPLACE_REDUCERS:
        return "commit_replace"
    source = _text(transition.get("source_filename"))
    target = _text(transition.get("target_filename"))
    if source and target and source == target:
        return "replace"
    return "push"


def _return_operation_mode(transition: Mapping[str, Any]) -> str:
    reducer = _text(transition.get("reducer") or transition.get("action_intent")).lower()
    if reducer in {"close_modal"}:
        return "close_modal"
    if reducer in {"close_overlay", "close_sheet", "close_drawer"}:
        return "close_overlay"
    if reducer in {"pop_history", "go_back", "navigate_back"}:
        return "pop_history"
    if reducer in {"return_to_parent"}:
        return "pop_to_parent"
    if reducer in {"finish_activity"}:
        return "finish_activity"
    return "pop_history"


def _state_ref_from_diff_key(key: str) -> str:
    text = _text(key)
    if text.startswith("state::") and "." in text:
        return text.split(".", 1)[0]
    if text.startswith("draft::") and "." in text:
        return text.split(".", 1)[0]
    return _canonical_state_ref(text)


def _state_refs_for_transaction(transition: Mapping[str, Any], expected_state_diff: Mapping[str, Any]) -> Set[str]:
    state_effect = _as_dict(transition.get("state_effect"))
    refs = _state_refs_from_value(transition)
    refs.update(_state_ref_from_diff_key(key) for key in expected_state_diff if _text(key))
    state_ref = _text(state_effect.get("state_ref"))
    if state_ref:
        refs.add(state_ref)
    return {ref for ref in refs if ref.startswith("state::")}


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def _render_projection_reads_any_state_ref(render_projections: Sequence[Any], refs: Set[str]) -> bool:
    if not refs:
        return False
    for projection in render_projections:
        if not isinstance(projection, dict):
            continue
        values = list(_string_values(projection))
        if any(ref in value for ref in refs for value in values):
            return True
    return False


def _oracle_covers_action_or_state(oracles: Sequence[Any], action_id: str, refs: Set[str]) -> bool:
    for oracle in oracles:
        if not isinstance(oracle, dict):
            continue
        action_fields = (
            oracle.get("action_id"),
            oracle.get("step_key"),
            oracle.get("source_step_key"),
            oracle.get("transition_id"),
        )
        if any(_text(value) == action_id for value in action_fields):
            return True
        for key in ("source_step_keys", "l0_transition_ids", "transition_ids", "action_ids"):
            values = oracle.get(key)
            if isinstance(values, list) and action_id in {_text(item) for item in values}:
                return True
        values = list(_string_values(oracle))
        if refs and any(ref in value for ref in refs for value in values):
            return True
    return False


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
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in ("历史记录", "操作记录", "审批记录")) or "history" in lowered:
        return False
    strong_markers = (
        "地址簿",
        "收件地址",
        "新华小区",
        "东进站口",
        "航站楼",
        "目的地",
        "候选",
        "条目",
        "条记录",
        "record",
        "address book",
    )
    selection_markers = ("点击", "选择", "select", "choose")
    return any(marker in text for marker in strong_markers) and any(marker in text for marker in selection_markers)


def _is_query_projection(transition: Mapping[str, Any]) -> bool:
    state_effect = _as_dict(transition.get("state_effect"))
    reducer = _text(transition.get("reducer") or transition.get("action_intent"))
    return (
        reducer == "project_suggestions"
        or _text(state_effect.get("query_transition_kind")) == "suggestion_projection"
        or _text(transition.get("query_transition_kind")) == "suggestion_projection"
    )


def _query_projection_matches_action(query_projections: Sequence[Any], action_id: str) -> bool:
    if not query_projections:
        return False
    saw_step_scoped_projection = False
    for item in query_projections:
        if not isinstance(item, dict):
            continue
        step_key = _text(item.get("step_key") or item.get("source_step_key") or item.get("transition_step_key"))
        if step_key:
            saw_step_scoped_projection = True
            if step_key == action_id:
                return True
        for key in ("step_keys", "source_step_keys", "l0_transition_ids", "transition_ids", "action_ids"):
            values = item.get(key)
            if not isinstance(values, list):
                continue
            scoped_values = {_text(value) for value in values if _text(value)}
            if scoped_values:
                saw_step_scoped_projection = True
            if action_id in scoped_values:
                return True
    return not saw_step_scoped_projection


def _expected_filename(transition: Mapping[str, Any]) -> str:
    route_oracle = _as_dict(transition.get("route_oracle"))
    oracle_expected = _text(
        route_oracle.get("expected_filename")
        or route_oracle.get("target_filename")
        or route_oracle.get("filename")
    )
    return oracle_expected or _text(transition.get("expected_filename") or transition.get("target_filename"))


def _expected_state_diff(transition: Mapping[str, Any]) -> Dict[str, Any]:
    state_effect = _as_dict(transition.get("state_effect"))
    for candidate in (
        transition.get("expected_state_diff"),
        transition.get("state_diff"),
        state_effect.get("expected_state_diff"),
        state_effect.get("state_diff"),
    ):
        if isinstance(candidate, dict):
            return candidate
    return {}


def _assertions_by_action(derived_contract: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    assertions: Dict[str, Dict[str, Any]] = {}
    for item in _as_list(derived_contract.get("trace_replay_assertions")):
        if not isinstance(item, dict):
            continue
        action_id = _text(item.get("action_id"))
        if action_id:
            assertions[action_id] = {
                "expected_filename": _text(item.get("expected_filename")),
                "expected_state_diff": _as_dict(item.get("expected_state_diff")),
            }
    return assertions


def _runtime_binding_for_action(action: Mapping[str, Any], assertions: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    action_id = _text(action.get("action_id"))
    assertion = assertions.get(action_id, {})
    return {
        "executor": "step(action_id)",
        "action_id": action_id,
        "reducer": _text(action.get("reducer")),
        "source_filename": _text(action.get("source_filename")),
        "target_filename": _text(action.get("target_filename")),
        "state_diff": _as_dict(assertion.get("expected_state_diff")),
    }


def _record_items_from_value(value: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        record_id = _text(
            value.get("record_id")
            or value.get("record_ref")
            or value.get("record_identity")
            or value.get("identity_key")
        )
        if record_id:
            item = dict(value)
            item["record_id"] = record_id
            records.append(item)
        for item in value.values():
            records.extend(_record_items_from_value(item))
    elif isinstance(value, list):
        for item in value:
            records.extend(_record_items_from_value(item))
    return records


def _render_projections_for_transaction(
    render_projections: Sequence[Any],
    *,
    action_id: str,
    state_refs: Set[str],
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for projection in render_projections:
        if not isinstance(projection, dict):
            continue
        values = list(_string_values(projection))
        if action_id and action_id in {_text(value) for value in values}:
            selected.append(dict(projection))
            continue
        if state_refs and any(ref in value for ref in state_refs for value in values):
            selected.append(dict(projection))
    return selected


def _oracle_for_transaction(
    oracles: Sequence[Any],
    *,
    action_id: str,
    state_refs: Set[str],
    expected_filename: str,
    expected_state_diff: Mapping[str, Any],
) -> Dict[str, Any]:
    for oracle in oracles:
        if not isinstance(oracle, dict):
            continue
        if _oracle_covers_action_or_state([oracle], action_id, state_refs):
            return dict(oracle)
    return {
        "source": "spec_executability_eval.derived_transaction",
        "action_id": action_id,
        "expected_filename": expected_filename,
        "expected_state_diff": dict(expected_state_diff),
    }


def _page_specs(payload: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    by_filename = payload.get("page_environment_specs_by_filename")
    if isinstance(by_filename, dict):
        return {
            _text(filename): spec
            for filename, spec in by_filename.items()
            if _text(filename) and isinstance(spec, dict)
        }
    specs = payload.get("specs")
    if isinstance(specs, list):
        result: Dict[str, Dict[str, Any]] = {}
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            filename = _text(spec.get("filename") or spec.get("page_id"))
            if filename:
                result[filename] = spec
        return result
    return {}


def _append_issue(
    issues: List[Dict[str, Any]],
    *,
    code: str,
    severity: str,
    page_filename: str,
    action_id: str = "",
    message: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    issue: Dict[str, Any] = {
        "code": code,
        "severity": severity,
        "page_filename": page_filename,
        "message": message,
    }
    if action_id:
        issue["action_id"] = action_id
        issue["trace_id"] = _trace_id_for_step(action_id)
    if details:
        issue["details"] = dict(details)
    issues.append(issue)


def evaluate_spec_executability(
    page_environment_specs: Mapping[str, Any],
    route_execution_contracts_by_filename: Mapping[str, Any] | None = None,
    step_boundary_contracts: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:

    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = _page_specs(payload)
    rec_by_filename = route_execution_contracts_by_filename if isinstance(route_execution_contracts_by_filename, dict) else {}
    issues: List[Dict[str, Any]] = []
    sessions_by_trace_id: Dict[str, Dict[str, Any]] = {}
    initial_state_by_trace_id: Dict[str, Dict[str, Any]] = {}
    actions_by_id: Dict[str, Dict[str, Any]] = {}
    transactions_by_action_id: Dict[str, Dict[str, Any]] = {}
    record_identity_schema: Dict[str, Any] = {
        "format": "record_identity_schema",
        "schema_revision": 1,
        "records_by_id": {},
        "record_refs_by_action_id": {},
    }
    reducers: Set[str] = set()
    pages: Dict[str, Dict[str, Any]] = {}
    trace_replay_assertions: List[Dict[str, Any]] = []
    seen_assertion_actions: Set[str] = set()
    occurrences_by_action: Dict[str, List[Tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    state_store_schema: Dict[str, Any] = {"pages": {}, "state_refs": []}
    declared_refs_by_name: Dict[str, Dict[str, Any]] = {}
    referenced_refs_by_name: Dict[str, Dict[str, Any]] = {}
    forward_edges_by_action_id: Dict[str, Dict[str, Any]] = {}
    return_operations_by_action_id: Dict[str, Dict[str, Any]] = {}
    boundary_by_action_id = _as_dict(
        _as_dict(step_boundary_contracts).get("actions_by_id")
    )

    for page_filename, spec in specs_by_filename.items():
        filename = _text(spec.get("filename") or page_filename)
        transitions = [item for item in _as_list(spec.get("transitions")) if isinstance(item, dict)]
        state_schema = _as_dict(spec.get("state_schema"))
        query_projections = [item for item in _as_list(spec.get("query_projections")) if isinstance(item, dict)]
        render_projections = [item for item in _as_list(spec.get("render_projections")) if isinstance(item, dict)]
        oracles = [item for item in _as_list(spec.get("oracles")) if isinstance(item, dict)]
        initial_state = deepcopy(_as_dict(spec.get("initial_state")))
        declared_state_refs = _state_refs_from_schema(state_schema)
        for ref in declared_state_refs:
            entry = declared_refs_by_name.setdefault(
                ref,
                {
                    "state_ref": ref,
                    "declared_by_pages": [],
                },
            )
            if filename not in entry["declared_by_pages"]:
                entry["declared_by_pages"].append(filename)
        state_store_schema["pages"][filename] = {
            "state_refs": sorted(declared_state_refs),
            "query_projection_count": len(query_projections),
            "transition_count": len(transitions),
        }
        pages[filename] = {
            "filename": filename,
            "transition_count": len(transitions),
            "query_projection_count": len(query_projections),
        }

        for index, transition in enumerate(transitions):
            action_id = _text(transition.get("step_key") or transition.get("action_id"))
            if not action_id:
                action_id = f"{filename}:transition{index}"
            trace_id = _trace_id_for_step(action_id)
            source_filename = _text(transition.get("source_filename") or filename)
            target_filename = _text(transition.get("target_filename"))
            boundary_contract = _as_dict(
                transition.get("boundary_contract")
                or transition.get("page_boundary_authority")
                or boundary_by_action_id.get(action_id)
            )
            boundary_decision = _text(
                boundary_contract.get("boundary_decision")
            ).lower()
            if boundary_decision == "runtime_projection":
                canonical_filename = _text(
                    boundary_contract.get("canonical_runtime_filename")
                )
                state_refs = [
                    _text(ref)
                    for ref in _as_list(boundary_contract.get("state_refs"))
                    if _text(ref)
                ]
                boundary_state_diff = _as_dict(
                    boundary_contract.get("expected_state_diff")
                )
                if not canonical_filename:
                    _append_issue(
                        issues,
                        code="runtime_projection_canonical_filename_mismatch",
                        severity=BLOCKING,
                        page_filename=filename,
                        action_id=action_id,
                        message="runtime projection has no canonical runtime filename",
                    )
                if not state_refs or not boundary_state_diff:
                    _append_issue(
                        issues,
                        code="runtime_projection_missing_state_contract",
                        severity=BLOCKING,
                        page_filename=filename,
                        action_id=action_id,
                        message="runtime projection has no state refs or expected state diff",
                    )
                if canonical_filename:
                    source_filename = canonical_filename
                    target_filename = canonical_filename
            reducer = _text(transition.get("reducer") or transition.get("action_intent"))
            initial_state_by_trace_id.setdefault(trace_id, deepcopy(initial_state))
            if reducer:
                reducers.add(reducer)
            session = sessions_by_trace_id.setdefault(trace_id, {"trace_id": trace_id, "action_ids": []})
            if action_id not in session["action_ids"]:
                session["action_ids"].append(action_id)

            action = actions_by_id.setdefault(
                action_id,
                {
                    "action_id": action_id,
                    "trace_id": trace_id,
                    "source_filename": source_filename,
                    "target_filename": target_filename,
                    "operation": _text(transition.get("operation")),
                    "operation_intent": _text(transition.get("operation_intent")),
                    "reducer": reducer,
                    "effect_admission_status": _transition_status(transition),
                    "boundary_contract": deepcopy(boundary_contract),
                    "page_filenames": [],
                },
            )
            if filename not in action["page_filenames"]:
                action["page_filenames"].append(filename)
            if not action.get("target_filename") and target_filename:
                action["target_filename"] = target_filename
            if not action.get("reducer") and reducer:
                action["reducer"] = reducer

            occurrences_by_action[action_id].append((filename, transition))

            status = _transition_status(transition)
            l0_role = _text(transition.get("l0_role"))
            if l0_role == "execution_anchor" and status == "unresolved":
                _append_issue(
                    issues,
                    code="unresolved_execution_effect",
                    severity=BLOCKING,
                    page_filename=filename,
                    action_id=action_id,
                    message="execution_anchor transition still has unresolved effect admission",
                    details={
                        "operation": _text(transition.get("operation")),
                        "operation_intent": _text(transition.get("operation_intent")),
                        "reducer": reducer,
                        "weak_effect_candidates": _weak_candidates(transition),
                    },
                )

            if _is_query_projection(transition):
                expected_filename = _expected_filename(transition)
                route_target = target_filename or expected_filename
                if route_target and source_filename and route_target != source_filename:
                    _append_issue(
                        issues,
                        code="query_projection_route_conflict",
                        severity=BLOCKING,
                        page_filename=filename,
                        action_id=action_id,
                        message="query suggestion projection is also compiled as a route transition",
                        details={
                            "source_filename": source_filename,
                            "target_filename": target_filename,
                            "expected_filename": expected_filename,
                            "reducer": reducer,
                        },
                    )
                if not _query_projection_matches_action(query_projections, action_id):
                    _append_issue(
                        issues,
                        code="missing_query_projection_spec",
                        severity=BLOCKING,
                        page_filename=filename,
                        action_id=action_id,
                        message="project_suggestions transition has no matching query_projections entry in the spec",
                        details={"query_projection_count": len(query_projections)},
                     )

            expected_filename = _expected_filename(transition)
            expected_state_diff = (
                deepcopy(_as_dict(boundary_contract.get("expected_state_diff")))
                if boundary_decision == "runtime_projection"
                else _expected_state_diff(transition)
            )
            effect_families = _effect_families(transition)
            transaction_state_refs = _state_refs_for_transaction(transition, expected_state_diff)
            affected_render_projections = _render_projections_for_transaction(
                render_projections,
                action_id=action_id,
                state_refs=transaction_state_refs,
            )
            post_action_oracle = _oracle_for_transaction(
                oracles,
                action_id=action_id,
                state_refs=transaction_state_refs,
                expected_filename=expected_filename,
                expected_state_diff=expected_state_diff,
            )
            records_for_action = _record_items_from_value(transition)
            record_ids_for_action: List[str] = []
            for record in records_for_action:
                record_id = _text(record.get("record_id"))
                if not record_id:
                    continue
                record_ids_for_action.append(record_id)
                existing = _as_dict(record_identity_schema["records_by_id"].get(record_id))
                merged = dict(existing)
                merged.update(record)
                merged["record_id"] = record_id
                record_identity_schema["records_by_id"][record_id] = merged
            if record_ids_for_action:
                record_identity_schema["record_refs_by_action_id"][action_id] = list(dict.fromkeys(record_ids_for_action))

            navigation_target_filename = target_filename or expected_filename or source_filename
            if boundary_decision == "runtime_projection":
                navigation_target_filename = source_filename
            target_hydration = _as_dict(
                transition.get("target_hydration")
                or _as_dict(transition.get("state_effect")).get("target_hydration")
                or transition.get("selected_record_hydration")
                or _as_dict(transition.get("state_effect")).get("selected_record_hydration")
            )
            transactions_by_action_id[action_id] = {
                "action_id": action_id,
                "trace_id": trace_id,
                "source_filename": source_filename,
                "target_filename": navigation_target_filename,
                "operation": _text(transition.get("operation")),
                "operation_intent": _text(transition.get("operation_intent")),
                "effect_admission_status": status,
                "pre_state": deepcopy(initial_state_by_trace_id.get(trace_id, {})),
                "reducer": reducer,
                "state_commit": {
                    "state_refs": sorted(transaction_state_refs),
                    "expected_state_diff": expected_state_diff,
                },
                "navigation": {
                    "source_filename": source_filename,
                    "target_filename": navigation_target_filename,
                    "expected_filename": expected_filename,
                    "same_page": bool(source_filename == navigation_target_filename),
                    "kind": _navigation_kind(transition),
                    "history_mode": _history_mode(transition),
                },
                "target_hydration": target_hydration,
                "post_action_oracle": post_action_oracle,
                "affected_render_projections": affected_render_projections,
                "boundary_contract": deepcopy(boundary_contract),
            }
            nav_kind = _navigation_kind(transition)
            is_cross_page = source_filename != navigation_target_filename
            if is_cross_page and nav_kind == "return":
                return_operations_by_action_id[action_id] = {
                    "action_id": action_id,
                    "kind": "return_operation",
                    "source_page_id": source_filename,
                    "target_resolution": {
                        "type": "previous_stack_frame",
                        "expected_page_id": navigation_target_filename,
                    },
                    "mode": _return_operation_mode(transition),
                    "stack_owner": "GhostwebRouteExecution",
                    "monotonicity": {
                        "decreases": _return_operation_mode(transition).split("_")[0],
                        "does_not_push": True,
                    },
                    "evidence": {
                        "trace_steps": [action_id],
                        "source": "trace",
                    },
                }
            elif is_cross_page:
                forward_edges_by_action_id[action_id] = {
                    "action_id": action_id,
                    "kind": "forward",
                    "source_filename": source_filename,
                    "target_filename": navigation_target_filename,
                    "history_mode": _history_mode(transition),
                }
            if boundary_decision == "runtime_projection" and action_id in forward_edges_by_action_id:
                forward_edges_by_action_id.pop(action_id, None)
                _append_issue(
                    issues,
                    code="runtime_projection_navigation_edge_leak",
                    severity=BLOCKING,
                    page_filename=filename,
                    action_id=action_id,
                    message="runtime projection produced a forward navigation edge",
                )
            if _is_stateful_l0_action_without_diff(transition, expected_state_diff):
                _append_issue(
                    issues,
                    code="stateful_action_has_empty_expected_state_diff",
                    severity=BLOCKING,
                    page_filename=filename,
                    action_id=action_id,
                    message="stateful L0 action has no expected state diff for contract replay",
                    details={
                        "operation": _text(transition.get("operation")),
                        "operation_intent": _text(transition.get("operation_intent")),
                        "reducer": reducer,
                        "effect_admission_status": status,
                        "effect_families": sorted(effect_families),
                    },
                )

            if "filter_selection" in effect_families:
                if not expected_state_diff:
                    _append_issue(
                        issues,
                        code="filter_selection_not_materialized",
                        severity=BLOCKING,
                        page_filename=filename,
                        action_id=action_id,
                        message="filter/tab action is not materialized as filter state transaction",
                        details={
                            "operation_intent": _text(transition.get("operation_intent")),
                            "reducer": reducer,
                            "effect_admission_status": status,
                            "weak_effect_candidates": _weak_candidates(transition),
                        },
                    )
                else:
                    filter_refs = _state_refs_for_transaction(transition, expected_state_diff)
                    if not _render_projection_reads_any_state_ref(render_projections, filter_refs):
                        _append_issue(
                            issues,
                            code="typed_reducer_render_projection_missing",
                            severity=BLOCKING,
                            page_filename=filename,
                            action_id=action_id,
                            message="typed filter reducer writes state that no render projection consumes",
                            details={
                                "reducer": reducer,
                                "state_refs": sorted(filter_refs),
                                "render_projection_count": len(render_projections),
                            },
                        )
                    if not _oracle_covers_action_or_state(oracles, action_id, filter_refs):
                        _append_issue(
                            issues,
                            code="typed_reducer_oracle_missing",
                            severity=BLOCKING,
                            page_filename=filename,
                            action_id=action_id,
                            message="typed filter reducer has no post-action oracle",
                            details={
                                "reducer": reducer,
                                "state_refs": sorted(filter_refs),
                                "oracle_count": len(oracles),
                            },
                        )

            if (
                reducer == "navigate_route"
                and source_filename
                and target_filename
                and source_filename != target_filename
                and not expected_state_diff
                and not _contains_selected_record_hydration(transition)
                and _looks_like_record_selection(transition)
            ):
                _append_issue(
                    issues,
                    code="route_only_record_selection_missing_state_commit",
                    severity=BLOCKING,
                    page_filename=filename,
                    action_id=action_id,
                    message="record-selection route transition has no state commit for target hydration",
                    details={
                        "source_filename": source_filename,
                        "target_filename": target_filename,
                        "operation_intent": _text(transition.get("operation_intent")),
                        "reducer": reducer,
                    },
                )

            used_state_refs = _state_refs_from_value(transition)
            for ref in used_state_refs:
                entry = referenced_refs_by_name.setdefault(
                    ref,
                    {
                        "state_ref": ref,
                        "referenced_by_actions": [],
                        "referenced_by_pages": [],
                    },
                )
                if action_id not in entry["referenced_by_actions"]:
                    entry["referenced_by_actions"].append(action_id)
                if filename not in entry["referenced_by_pages"]:
                    entry["referenced_by_pages"].append(filename)
            missing_state_refs = sorted(ref for ref in used_state_refs if ref not in declared_state_refs)
            if missing_state_refs:
                _append_issue(
                    issues,
                    code="state_ref_missing_from_schema",
                    severity=WARNING,
                    page_filename=filename,
                    action_id=action_id,
                    message="transition references state keys not declared by the page state_schema",
                    details={"missing_state_refs": missing_state_refs},
                )

            if action_id not in seen_assertion_actions and (expected_filename or expected_state_diff):
                trace_replay_assertions.append(
                    {
                        "action_id": action_id,
                        "trace_id": trace_id,
                        "expected_filename": expected_filename,
                        "expected_state_diff": expected_state_diff,
                    }
                )
                seen_assertion_actions.add(action_id)

    for action_id, occurrences in occurrences_by_action.items():
        if len(occurrences) < 2:
            continue
        hydration_pages = [
            filename
            for filename, transition in occurrences
            if _contains_selected_record_hydration(transition)
        ]
        if hydration_pages and len(hydration_pages) != len(occurrences):
            _append_issue(
                issues,
                code="selected_record_hydration_drift",
                severity=BLOCKING,
                page_filename=",".join(filename for filename, _ in occurrences),
                action_id=action_id,
                message="same trace action has selected_record_hydration in only part of its page projections",
                details={
                    "all_pages": [filename for filename, _ in occurrences],
                    "hydration_pages": hydration_pages,
                },
            )

    declared_state_refs_all = set(declared_refs_by_name)
    referenced_state_refs_all = set(referenced_refs_by_name)
    missing_state_refs_all = referenced_state_refs_all - declared_state_refs_all
    all_state_refs = declared_state_refs_all | referenced_state_refs_all
    refs_by_name: Dict[str, Dict[str, Any]] = {}
    for ref in sorted(all_state_refs):
        declared = declared_refs_by_name.get(ref, {})
        referenced = referenced_refs_by_name.get(ref, {})
        refs_by_name[ref] = {
            "state_ref": ref,
            "declaration_status": "declared" if ref in declared_state_refs_all else "referenced_missing",
            "declared_by_pages": sorted(declared.get("declared_by_pages", [])),
            "referenced_by_pages": sorted(referenced.get("referenced_by_pages", [])),
            "referenced_by_actions": sorted(referenced.get("referenced_by_actions", []), key=_action_sort_key),
        }
    state_store_schema["state_refs"] = sorted(all_state_refs)
    state_store_schema["declared_state_refs"] = sorted(declared_state_refs_all)
    state_store_schema["referenced_state_refs"] = sorted(referenced_state_refs_all)
    state_store_schema["missing_state_refs"] = sorted(missing_state_refs_all)
    state_store_schema["refs_by_name"] = refs_by_name
    state_store_schema["closure_status"] = "incomplete" if missing_state_refs_all else "closed"
    fallback_back_actions_by_page_id: Dict[str, Dict[str, Any]] = {}
    for page_filename in specs_by_filename:
        rec = _as_dict(rec_by_filename.get(page_filename))
        fallback = _as_dict(rec.get("fallback_back_action") or rec.get("default_back_action"))
        if fallback:
            target_fn = _text(fallback.get("target_filename"))
            if target_fn:
                fallback_back_actions_by_page_id[page_filename] = {
                    "action_id": _text(fallback.get("action_id") or "fallback_back"),
                    "kind": "return_operation",
                    "source_page_id": page_filename,
                    "mode": "cold_start_fallback",
                    "stack_owner": "GhostwebRouteExecution",
                    "target_resolution": {
                        "type": "fallback",
                        "expected_page_id": target_fn,
                        "fallback_page_id": target_fn,
                    },
                    "monotonicity": {
                        "decreases": "route_hierarchy_depth",
                        "does_not_push": True,
                    },
                    "evidence": {
                        "source": _text(fallback.get("target_source") or "route_hierarchy"),
                    },
                }

    navigation_contract: Dict[str, Any] = {
        "stack_schema": {
            "route_stack": "array<NavigationFrame>",
            "overlay_stack": "array<LayerFrame>",
            "modal_stack": "array<LayerFrame>",
            "active_page_id": "string",
            "active_trace_id": "string|null",
        },
        "forward_edges_by_action_id": forward_edges_by_action_id,
        "return_operations_by_action_id": return_operations_by_action_id,
        "fallback_back_actions_by_page_id": fallback_back_actions_by_page_id,
        "monotonicity_rules": {
            "rank_priority": [
                "modal_depth",
                "overlay_depth",
                "route_stack_depth",
                "activity_depth",
                "route_hierarchy_depth",
            ],
            "return_must_decrease_at_least_one": True,
            "return_must_not_push": True,
        },
    }

    severity_counts = Counter(issue.get("severity") for issue in issues)
    code_counts = Counter(issue.get("code") for issue in issues)
    blocking_issue_count = int(severity_counts.get(BLOCKING, 0))
    warning_issue_count = int(severity_counts.get(WARNING, 0))
    status = "fail" if blocking_issue_count else "warn" if warning_issue_count else "pass"

    return {
        "format": "spec_executability_eval",
        "schema_revision": 1,
        "summary": {
            "status": status,
            "page_count": len(specs_by_filename),
            "action_count": len(actions_by_id),
            "issue_count": len(issues),
            "blocking_issue_count": blocking_issue_count,
            "warning_issue_count": warning_issue_count,
            "issue_code_counts": dict(sorted(code_counts.items())),
        },
        "issues": issues,
        "derived_contract": {
            "format": "spec_execution_contract_projection",
            "schema_revision": 1,
            "pages": pages,
            "sessions_by_trace_id": sessions_by_trace_id,
            "initial_state_by_trace_id": initial_state_by_trace_id,
            "actions_by_id": actions_by_id,
            "transactions_by_action_id": transactions_by_action_id,
            "record_identity_schema": record_identity_schema,
            "reducers": sorted(reducers),
            "state_store_schema": state_store_schema,
            "trace_replay_assertions": trace_replay_assertions,
            "navigation_contract": navigation_contract,
            "step_boundary_contracts": {
                action_id: deepcopy(contract)
                for action_id, contract in boundary_by_action_id.items()
                if isinstance(contract, dict)
            },
            "reward_contract": {
                "reward_initial_expected": 0,
                "reward_final_expected": 1,
            },
        },
    }


def _recompute_summary_with_issues(report: Dict[str, Any], issues: List[Dict[str, Any]]) -> None:
    severity_counts = Counter(issue.get("severity") for issue in issues)
    code_counts = Counter(issue.get("code") for issue in issues)
    blocking_issue_count = int(severity_counts.get(BLOCKING, 0))
    warning_issue_count = int(severity_counts.get(WARNING, 0))
    summary = report.setdefault("summary", {})
    if not isinstance(summary, dict):
        summary = {}
        report["summary"] = summary
    summary["status"] = "fail" if blocking_issue_count else "warn" if warning_issue_count else "pass"
    summary["issue_count"] = len(issues)
    summary["blocking_issue_count"] = blocking_issue_count
    summary["warning_issue_count"] = warning_issue_count
    summary["issue_code_counts"] = dict(sorted(code_counts.items()))


def merge_domain_contract_issues_into_spec_eval(
    spec_executability_eval: Mapping[str, Any],
    domain_contracts: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:

    report = deepcopy(spec_executability_eval if isinstance(spec_executability_eval, dict) else {})
    base_issues = [
        item for item in _as_list(report.get("issues"))
        if isinstance(item, dict)
    ]
    domain_issues = [
        item for item in _as_list(_as_dict(domain_contracts).get("issues"))
        if isinstance(item, dict)
    ]
    merged_issues: List[Dict[str, Any]] = []
    seen = set()
    for source, issues in (
        ("spec_executability_eval", base_issues),
        ("domain_contracts", domain_issues),
    ):
        for issue in issues:
            normalized = dict(issue)
            normalized.setdefault("severity", BLOCKING)
            normalized.setdefault("source", source)
            key = (
                normalized.get("code"),
                normalized.get("page_filename"),
                normalized.get("action_id"),
                normalized.get("control_id"),
                normalized.get("message"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged_issues.append(normalized)
    report["issues"] = merged_issues
    _recompute_summary_with_issues(report, merged_issues)
    return report


def merge_state_delta_coverage_into_spec_eval(
    spec_executability_eval: Mapping[str, Any],
    state_delta_coverage_report: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:

    report = deepcopy(spec_executability_eval if isinstance(spec_executability_eval, dict) else {})
    base_issues = [
        item for item in _as_list(report.get("issues"))
        if isinstance(item, dict)
    ]
    coverage_issues = [
        item for item in _as_list(_as_dict(state_delta_coverage_report).get("issues"))
        if isinstance(item, dict)
    ]
    merged_issues: List[Dict[str, Any]] = []
    seen = set()
    for source, issues in (
        ("spec_executability_eval", base_issues),
        ("state_delta_spec_coverage", coverage_issues),
    ):
        for issue in issues:
            normalized = dict(issue)
            normalized.setdefault("severity", BLOCKING)
            normalized.setdefault("source", source)
            key = (
                normalized.get("code"),
                normalized.get("obs_step"),
                normalized.get("action_id"),
                normalized.get("signature"),
                normalized.get("state_scope"),
                normalized.get("message"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged_issues.append(normalized)
    report["issues"] = merged_issues
    _recompute_summary_with_issues(report, merged_issues)
    return report


def _state_ref_missing_repair_class(issue_counts: Mapping[str, Any]) -> str:
    missing_count = int(issue_counts.get("state_ref_missing_from_schema", 0) or 0)
    return "pipeline_defect" if missing_count >= 5 else "deterministic_normalizer"


def _route_issue(
    issue: Mapping[str, Any],
    *,
    issue_counts: Mapping[str, Any],
) -> Dict[str, Any]:
    code = _text(issue.get("code"))
    action_id = _text(issue.get("action_id"))
    base = {
        "code": code,
        "issue_code": code,
        "severity": _text(issue.get("severity")),
        "page_filename": _text(issue.get("page_filename")),
        "action_id": action_id,
        "trace_id": _trace_id_for_step(action_id) if action_id else "",
        "message": _text(issue.get("message")),
        "source_issue": dict(issue),
    }
    if code == "state_ref_missing_from_schema":
        repair_class = _state_ref_missing_repair_class(issue_counts)
        if repair_class == "pipeline_defect":
            return {
                **base,
                "repair_class": "pipeline_defect",
                "repair_reason": "state refs are referenced outside the declared state schema often enough to indicate missing global state-store closure",
                "allowed_patch_scope": ["state_store_schema", "page.state_schema", "state_store_closure_rule"],
                "forbidden_patch_scope": ["html", "compiled_views", "delete_transition", "drop_state_ref"],
                "promotion_rule": "state_ref_missing_from_schema_count>=5",
            }
        return {
            **base,
            "repair_class": "deterministic_normalizer",
            "repair_reason": "isolated missing state refs can be closed mechanically by declaring referenced refs in the state store",
            "allowed_patch_scope": ["state_store_schema", "page.state_schema"],
            "forbidden_patch_scope": ["html", "compiled_views", "delete_transition", "drop_state_ref"],
            "promotion_rule": "if repeated across pages/actions, promote to pipeline_defect",
        }
    if code == "query_projection_route_conflict":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "query suggestion projection and route commit must be separated by a deterministic query lifecycle rule",
            "allowed_patch_scope": ["query_lifecycle_rule", "transition.reducer", "transition.state_effect"],
            "forbidden_patch_scope": ["html", "compiled_views", "page_local_route_guess"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "selected_record_hydration_drift":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "cross-page selected-record state commit requires trace-wise transaction authority, not page-local repair",
            "allowed_patch_scope": ["trace_action_transaction_contract", "transition.state_effect"],
            "forbidden_patch_scope": ["html", "compiled_views", "target_page_only_hydration"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "route_only_record_selection_missing_state_commit":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "record-selection navigation must compile into source state_commit plus target hydration before PageGen",
            "allowed_patch_scope": ["trace_action_transaction_contract", "transition.state_effect", "target_hydration"],
            "forbidden_patch_scope": ["html", "compiled_views", "url_param_only_state_transfer"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "typed_reducer_render_projection_missing":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "typed state reducers must have render projections that consume the written state",
            "allowed_patch_scope": ["render_projection_closure_rule", "page.render_projections", "transition.state_effect"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_runtime_fallback"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "typed_reducer_oracle_missing":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "typed state reducers must have post-action oracles before PageGen",
            "allowed_patch_scope": ["oracle_closure_rule", "page.oracles", "transition.state_effect"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_runtime_fallback"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "filter_selection_not_materialized":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "filter/tab actions need a reducer-level filter state transaction and filtered collection projection",
            "allowed_patch_scope": ["reducer_ontology_rule", "transition.reducer", "transition.state_effect", "page.render_projections"],
            "forbidden_patch_scope": ["html", "compiled_views", "css_only_tab_switch"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "stateful_action_has_empty_expected_state_diff":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "isolated stateful actions can be repaired by adding a bounded reducer state_diff before PageGen",
            "allowed_patch_scope": ["transition.reducer", "transition.state_effect", "state_schema"],
            "forbidden_patch_scope": ["html", "compiled_views", "delete_transition", "downgrade_l0_role"],
            "promotion_rule": "if repeated by action family, promote to reducer ontology pipeline rule",
        }
    if code == "unresolved_execution_effect":
        weak_candidates = _as_list(_as_dict(issue.get("details")).get("weak_effect_candidates"))
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "the existing spec fields can express a concrete reducer/effect; use bounded spec-level repair before PageGen",
            "allowed_patch_scope": ["transition.reducer", "transition.state_effect", "state_schema"],
            "forbidden_patch_scope": ["html", "compiled_views", "delete_transition", "downgrade_l0_role"],
            "promotion_rule": "if same weak_effect_candidate/action family repeats across pages, promote to reducer ontology pipeline rule",
            "repair_hints": {
                "weak_effect_candidates": weak_candidates,
            },
        }
    if code in ("autocomplete_contract_missing", "autocomplete_candidate_rows_missing"):
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "autocomplete contracts must be compiled from query states and suggestion contracts at spec level",
            "allowed_patch_scope": ["query_states", "suggestion_contract", "render_projections"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_fallback_js"],
            "promotion_rule": "always pipeline_defect",
        }
    if code in ("autocomplete_candidate_evidence_missing", "autocomplete_materialization_incomplete"):
        return {
            **base,
            "repair_class": "evidence_blocked",
            "repair_reason": "autocomplete candidate rows are missing or below min-visible evidence; must be fixed by evidence/PES materialization, not by lowering counts",
            "allowed_patch_scope": ["query_states", "suggestion_contract", "render_projections", "trace_evidence"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_fallback_js", "schema_token_padding"],
            "promotion_rule": "always evidence_blocked until real trace evidence is provided",
        }
    if code in ("autocomplete_stale_identity_hint_projection", "autocomplete_duplicate_source_conflict"):
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "autocomplete compiler saw stale legacy projection/source conflict; producer/compiler must suppress identity-hint rows before PageGen",
            "allowed_patch_scope": ["page_environment_spec.render_projections", "query_states.suggestion_contract", "autocomplete_contracts"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_fallback_js"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "autocomplete_candidate_count_mismatch":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "autocomplete candidate rows exist but count is below expected; can be patched at spec level",
            "allowed_patch_scope": ["query_states.suggestions", "suggestion_contract.suggestions"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_fallback_js"],
            "promotion_rule": "if source evidence insufficient, promote to evidence_blocked",
        }
    if code == "autocomplete_category_coverage_missing":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "autocomplete category coverage incomplete; can be patched at spec level if evidence exists",
            "allowed_patch_scope": ["query_states.suggestions", "suggestion_contract.suggestions"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_fallback_js"],
            "promotion_rule": "if source evidence insufficient, promote to evidence_blocked",
        }
    if code == "autocomplete_initial_visibility_wrong":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "autocomplete visibility lifecycle can be patched at spec level",
            "allowed_patch_scope": ["suggestion_contract.projection_lifecycle", "render_projections"],
            "forbidden_patch_scope": ["html", "compiled_views"],
            "promotion_rule": "if lifecycle requires PageGen structural change, promote to pipeline_defect",
        }
    if code == "initial_control_state_contract_missing":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "initial control state must have explicit expected_checked/expected_value from evidence",
            "allowed_patch_scope": ["initial_state.controls", "content_seed"],
            "forbidden_patch_scope": ["html", "compiled_views", "infer_from_count"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "initial_control_state_wrong":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "initial control state value is incorrect; can be patched at spec level",
            "allowed_patch_scope": ["initial_state.controls"],
            "forbidden_patch_scope": ["html", "compiled_views", "infer_from_count"],
            "promotion_rule": "if source evidence contradicts, promote to evidence_blocked",
        }
    if code == "initial_control_state_inferred_from_count":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "group checkbox and individual items must use separate state_refs",
            "allowed_patch_scope": ["initial_state.controls.state_ref", "state_schema"],
            "forbidden_patch_scope": ["html", "compiled_views", "selectedCount_inference"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "materialized_identity_missing":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "committed query results with task-critical records must be promoted to materialized page identity",
            "allowed_patch_scope": ["instance_plan", "materialized_bindings", "content_lifecycle_plan"],
            "forbidden_patch_scope": ["html", "compiled_views", "pagegen_fallback_js"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "query_lifecycle_collapsed":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "type_search must not directly produce committed_result; query lifecycle must distinguish draft/preview/commit stages",
            "allowed_patch_scope": ["transition.reducer", "transition.state_effect", "query_lifecycle_state"],
            "forbidden_patch_scope": ["html", "compiled_views", "delete_transition"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "same_page_autocomplete_wrongly_materialized":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "same-page autocomplete/typeahead must remain query runtime state; should not create a materialized result page",
            "allowed_patch_scope": ["instance_plan", "materialized_bindings", "query_lifecycle_state"],
            "forbidden_patch_scope": ["html", "compiled_views"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "global_runtime_transaction_missing":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "cross-page state writeback must be compiled as a global runtime transaction with source write and target read",
            "allowed_patch_scope": ["transition.state_effect", "state_schema", "runtime_transactions"],
            "forbidden_patch_scope": ["html", "compiled_views", "initial_state_patch"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "global_runtime_transaction_read_missing":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "target page must read the global state written by the source transaction",
            "allowed_patch_scope": ["target_page.state_reads", "runtime_transactions", "render_projections"],
            "forbidden_patch_scope": ["html", "compiled_views", "initial_state_patch"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "initial_dom_contract_missing":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "pages with first-load reference evidence must have non-empty initial_dom_contract",
            "allowed_patch_scope": ["initial_state", "initial_dom_contract", "content_seed"],
            "forbidden_patch_scope": ["html", "compiled_views"],
            "promotion_rule": "if evidence is screenshot-only without structured data, promote to evidence_blocked",
        }
    if code == "initial_collection_oracle_missing":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "first-load list/collection must have count and item identity oracle",
            "allowed_patch_scope": ["initial_state", "initial_dom_contract", "content_seed", "render_projections"],
            "forbidden_patch_scope": ["html", "compiled_views"],
            "promotion_rule": "if list content is dynamic-only, promote to evidence_blocked",
        }
    if code == "initial_control_oracle_missing":
        return {
            **base,
            "repair_class": "spec_fixloop_candidate",
            "repair_reason": "first-load control must have explicit checked/value/visible oracle",
            "allowed_patch_scope": ["initial_state.controls", "initial_dom_contract"],
            "forbidden_patch_scope": ["html", "compiled_views", "infer_from_count"],
            "promotion_rule": "if source evidence contradicts, promote to evidence_blocked",
        }
    if code == "state_delta_missing_spec_coverage":
        return {
            **base,
            "repair_class": "pipeline_defect",
            "repair_reason": "excluded state delta has no reducer/state_write/oracle — spec must cover runtime state changes not shown in L0 screenshots",
            "allowed_patch_scope": ["transition.reducer", "transition.state_effect", "state_schema", "dom_oracles"],
            "forbidden_patch_scope": ["html", "compiled_views", "l0_screenshot_admission"],
            "promotion_rule": "always pipeline_defect",
        }
    if code == "pagegen_contract_violation":
        return {
            **base,
            "repair_class": "pagegen_materialization_defect",
            "repair_reason": "PageGen HTML does not conform to the compiled contract",
            "allowed_patch_scope": ["pagegen_prompt", "pagegen_fixloop"],
            "forbidden_patch_scope": ["spec", "compiled_views", "contract_modification"],
            "promotion_rule": "always pagegen_materialization_defect",
        }
    return {
        **base,
        "repair_class": "evidence_blocked",
        "repair_reason": "no deterministic or bounded spec repair policy is registered for this issue code",
        "allowed_patch_scope": [],
        "forbidden_patch_scope": ["html", "compiled_views", "silent_fallback"],
        "promotion_rule": "register explicit router policy before repair",
    }


def build_spec_issue_router_report(
    spec_executability_eval: Mapping[str, Any],
    *,
    state_delta_coverage_report: Optional[Mapping[str, Any]] = None,
    route_execution_blockers: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:

    report = spec_executability_eval if isinstance(spec_executability_eval, dict) else {}
    summary = _as_dict(report.get("summary"))
    issue_counts = _as_dict(summary.get("issue_code_counts"))
    issues = [item for item in _as_list(report.get("issues")) if isinstance(item, dict)]

    if isinstance(state_delta_coverage_report, dict):
        for issue in _as_list(state_delta_coverage_report.get("issues")):
            if isinstance(issue, dict):
                issues.append(issue)
                code = _text(issue.get("code"))
                issue_counts[code] = int(issue_counts.get(code, 0) or 0) + 1

    routes = [_route_issue(issue, issue_counts=issue_counts) for issue in issues]
    by_repair_class = Counter(route.get("repair_class") for route in routes)
    systemic_findings: List[Dict[str, Any]] = []
    missing_count = int(issue_counts.get("state_ref_missing_from_schema", 0) or 0)
    if missing_count >= 5:
        state_store = _as_dict(_as_dict(report.get("derived_contract")).get("state_store_schema"))
        systemic_findings.append(
            {
                "code": "systemic_state_store_closure_missing",
                "severity": "pipeline_defect",
                "issue_count": missing_count,
                "missing_state_ref_count": len(_as_list(state_store.get("missing_state_refs"))),
                "reason": "missing state refs are frequent enough to show that spec generation lacks a global state-store closure pass",
                "required_pipeline_optimization": "build global state_store_schema before compiling PageGen/runtime/replay views",
            }
        )
    if int(issue_counts.get("query_projection_route_conflict", 0) or 0) > 0:
        systemic_findings.append(
            {
                "code": "query_lifecycle_not_hard_split",
                "severity": "pipeline_defect",
                "issue_count": int(issue_counts.get("query_projection_route_conflict", 0) or 0),
                "reason": "suggestion projection actions are being allowed to carry route targets",
                "required_pipeline_optimization": "enforce query FSM: project_suggestions stays same-page; commit_query/select_suggestion owns navigation",
            }
        )
    if int(issue_counts.get("selected_record_hydration_drift", 0) or 0) > 0:
        systemic_findings.append(
            {
                "code": "cross_page_state_transaction_not_closed",
                "severity": "pipeline_defect",
                "issue_count": int(issue_counts.get("selected_record_hydration_drift", 0) or 0),
                "reason": "same action_id is projected differently across source/target pages",
                "required_pipeline_optimization": "introduce trace-wise state_commit/state_hydration transaction contract",
            }
        )
    route_only_record_count = int(issue_counts.get("route_only_record_selection_missing_state_commit", 0) or 0)
    if route_only_record_count > 0:
        systemic_findings.append(
            {
                "code": "route_only_record_selection_not_transactional",
                "severity": "pipeline_defect",
                "issue_count": route_only_record_count,
                "reason": "record selection is compiled as navigation without state_commit/target_hydration",
                "required_pipeline_optimization": "compile record-selection routes into atomic state transactions before PageGen",
            }
        )
    filter_closure_count = int(issue_counts.get("filter_selection_not_materialized", 0) or 0) + int(
        issue_counts.get("typed_reducer_render_projection_missing", 0) or 0
    ) + int(
        issue_counts.get("typed_reducer_oracle_missing", 0) or 0
    )
    if filter_closure_count > 0:
        systemic_findings.append(
            {
                "code": "stateful_reducer_render_closure_missing",
                "severity": "pipeline_defect",
                "issue_count": filter_closure_count,
                "reason": "stateful reducers are not closed through state_diff and render projections",
                "required_pipeline_optimization": "enforce reducer -> state_diff -> render_projection/oracle closure in spec compilation",
            }
        )
    autocomplete_missing_count = (
        int(issue_counts.get("autocomplete_contract_missing", 0) or 0)
        + int(issue_counts.get("autocomplete_candidate_rows_missing", 0) or 0)
        + int(issue_counts.get("autocomplete_materialization_incomplete", 0) or 0)
        + int(issue_counts.get("autocomplete_stale_identity_hint_projection", 0) or 0)
        + int(issue_counts.get("autocomplete_duplicate_source_conflict", 0) or 0)
    )
    if autocomplete_missing_count > 0:
        systemic_findings.append(
            {
                "code": "autocomplete_contracts_incomplete",
                "severity": "pipeline_defect",
                "issue_count": autocomplete_missing_count,
                "reason": "autocomplete contracts missing or have empty candidate rows",
                "required_pipeline_optimization": "compile autocomplete contracts with candidate rows from query_states/suggestion_contracts",
            }
        )
    initial_control_missing_count = int(issue_counts.get("initial_control_state_contract_missing", 0) or 0)
    if initial_control_missing_count > 0:
        systemic_findings.append(
            {
                "code": "initial_control_state_contracts_incomplete",
                "severity": "pipeline_defect",
                "issue_count": initial_control_missing_count,
                "reason": "initial control state contracts missing expected_checked/expected_value",
                "required_pipeline_optimization": "compile initial control state contracts with explicit oracle from evidence",
            }
        )
    _BLOCKER_REPAIR_CLASS = {
        "runtime_bootstrap_missing": "pipeline_defect:runtime_bootstrap_missing",
        "route_manifest_unloaded": "pipeline_defect:route_manifest_unloaded",
        "executor_not_defined": "pipeline_defect:executor_not_defined",
        "target_resolution_failed": "pipeline_defect:target_resolution_failed",
        "click_handler_no_effect": "pagegen_materialization_defect:click_handler_no_effect",
        "navigation_to_about_blank": "pipeline_defect:navigation_to_about_blank",
    }
    if isinstance(route_execution_blockers, dict):
        blocker_counts = route_execution_blockers.get("route_execution_blockers", {})
        if not blocker_counts:
            blocker_counts = route_execution_blockers.get("summary", {}).get("route_execution_blockers", {})
        for blocker_type, count in blocker_counts.items():
            if count > 0:
                repair_class = _BLOCKER_REPAIR_CLASS.get(blocker_type, f"pipeline_defect:{blocker_type}")
                systemic_findings.append({
                    "code": f"route_execution_blocker:{blocker_type}",
                    "severity": "pipeline_blocker",
                    "repair_class": repair_class,
                    "issue_count": count,
                    "reason": f"route execution substrate failure: {blocker_type} prevents causal replay",
                    "required_pipeline_optimization": (
                        "fix route execution closure (materializer, runtime, URL resolution) before full replay"
                    ),
                })
    return {
        "format": "spec_issue_router_report",
        "schema_revision": 2,
        "source": "spec_executability_eval",
        "summary": {
            "issue_count": len(routes),
            "by_repair_class": dict(sorted(by_repair_class.items())),
            "systemic_finding_count": len(systemic_findings),
        },
        "systemic_findings": systemic_findings,
        "issue_routes": routes,
    }


def build_pagegen_spec_view_by_page(spec_executability_eval: Mapping[str, Any]) -> Dict[str, Any]:

    report = spec_executability_eval if isinstance(spec_executability_eval, dict) else {}
    derived_contract = _as_dict(report.get("derived_contract"))
    actions_by_id = _as_dict(derived_contract.get("actions_by_id"))
    assertions = _assertions_by_action(derived_contract)
    views_by_filename: Dict[str, Dict[str, Any]] = {}

    for action_id, action in actions_by_id.items():
        if not isinstance(action, dict):
            continue
        source_filename = _text(action.get("source_filename"))
        if not source_filename:
            continue
        view = views_by_filename.setdefault(
            source_filename,
            {
                "format": "pagegen_spec_execution_view",
                "schema_revision": 1,
                "filename": source_filename,
                "authority": "page_environment_specs",
                "source": "spec_executability_eval.derived_contract",
                "actions": [],
            },
        )
        assertion = assertions.get(_text(action_id), {})
        view["actions"].append(
            {
                "action_id": _text(action.get("action_id") or action_id),
                "trace_id": _text(action.get("trace_id")),
                "operation": _text(action.get("operation")),
                "operation_intent": _text(action.get("operation_intent")),
                "reducer": _text(action.get("reducer")),
                "source_filename": source_filename,
                "target_filename": _text(action.get("target_filename")),
                "effect_admission_status": _text(action.get("effect_admission_status")),
                "assertions": {
                    "expected_filename": _text(assertion.get("expected_filename")),
                    "expected_state_diff": _as_dict(assertion.get("expected_state_diff")),
                },
                "runtime_binding": _runtime_binding_for_action(action, assertions),
            }
        )

    for view in views_by_filename.values():
        view["actions"].sort(key=lambda item: _action_sort_key(item.get("action_id")))
        view["action_count"] = len(view["actions"])

    return {
        "format": "pagegen_spec_view_by_page",
        "schema_revision": 1,
        "source": "spec_executability_eval.derived_contract",
        "views_by_filename": views_by_filename,
        "summary": {
            "page_count": len(views_by_filename),
            "action_count": sum(len(view.get("actions", [])) for view in views_by_filename.values()),
        },
    }


def build_pagegen_execution_view_by_page(
    spec_executability_eval: Mapping[str, Any],
    typed_action_contracts: Mapping[str, Any] | None = None,
    domain_contracts: Mapping[str, Any] | None = None,
    action_effect_contracts: Mapping[str, Any] | None = None,
    runtime_transaction_contracts: Mapping[str, Any] | None = None,
    dom_binding_contracts: Mapping[str, Any] | None = None,
    global_execution_schema: Mapping[str, Any] | None = None,
    page_environment_specs: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:

    report = spec_executability_eval if isinstance(spec_executability_eval, dict) else {}
    derived_contract = _as_dict(report.get("derived_contract"))
    navigation_contract = _as_dict(derived_contract.get("navigation_contract"))
    state_store_schema = _as_dict(derived_contract.get("state_store_schema"))
    actions_by_id = _as_dict(derived_contract.get("actions_by_id"))
    transactions_by_action_id = _as_dict(derived_contract.get("transactions_by_action_id"))
    initial_state_by_trace_id = _as_dict(derived_contract.get("initial_state_by_trace_id"))
    assertions = _assertions_by_action(derived_contract)
    typed_contracts = _as_dict(
        typed_action_contracts.get("typed_action_contracts_by_action_id")
        if isinstance(typed_action_contracts, dict) else {}
    )
    domain = domain_contracts if isinstance(domain_contracts, dict) else {}
    effect_actions = [
        action for action in _as_list((action_effect_contracts or {}).get("actions"))
        if isinstance(action, dict)
    ]
    effect_by_action_id = {
        _text(action.get("action_id")): action
        for action in effect_actions
        if _text(action.get("action_id"))
    }
    runtime_transactions = [
        tx for tx in _as_list((runtime_transaction_contracts or {}).get("transactions"))
        if isinstance(tx, dict)
    ]
    dom_bindings = [
        binding for binding in _as_list((dom_binding_contracts or {}).get("bindings"))
        if isinstance(binding, dict)
    ]
    page_specs_by_filename = _as_dict(
        (page_environment_specs or {}).get("page_environment_specs_by_filename")
        if isinstance(page_environment_specs, Mapping)
        else {}
    )

    views_by_filename: Dict[str, Dict[str, Any]] = {}

    for action_id, action in actions_by_id.items():
        if not isinstance(action, dict):
            continue
        source_filename = _text(action.get("source_filename"))
        if not source_filename:
            continue
        view = views_by_filename.setdefault(
            source_filename,
            {
                "format": "pagegen_execution_view",
                "schema_revision": 1,
                "page_id": source_filename,
                "authority": "spec_executability_eval + typed_action_contracts",
                "typed_actions": [],
                "surface_contracts": [],
                "navigation_contract_slice": {},
                "state_store_slice": {},
                "post_action_oracles": [],
                "allowed_sdk_interfaces": [
                    "GhostwebRouteExecution.navigate",
                    "GhostwebRouteExecution.returnBack",
                    "GhostwebRouteExecution.step",
                    "GhostwebRouteExecution.inspectNavigationStack",
                    "GhostwebRouteExecution.inspectState",
                ],
                "provenance_refs": [],
            },
        )
        typed_action = typed_contracts.get(_text(action_id), {})
        assertion = assertions.get(_text(action_id), {})
        runtime_binding = _runtime_binding_for_action(action, assertions)
        effect_action = _as_dict(effect_by_action_id.get(_text(action_id)))
        if effect_action:
            effect_state_diff = _as_dict(effect_action.get("expected_state_diff"))
            runtime_binding.update(
                {
                    "reducer": _text(effect_action.get("reducer")) or runtime_binding.get("reducer", ""),
                    "effect_kind": _text(effect_action.get("effect_kind")),
                    "state_refs": _as_list(effect_action.get("state_refs")),
                    "state_reads": _as_list(effect_action.get("state_reads")),
                    "component_ref": _text(effect_action.get("component_ref")),
                }
            )
            if effect_state_diff:
                runtime_binding["state_diff"] = effect_state_diff
            for key in (
                "gesture_scope",
                "gesture_axis",
                "gesture_direction",
                "gesture_reveals",
                "visibility_precondition_action_id",
                "required_visible_value",
            ):
                if effect_action.get(key) not in (None, "", [], {}):
                    runtime_binding[key] = effect_action.get(key)
        typed_row = {
            "action_id": _text(action.get("action_id") or action_id),
            "action_type": _text(typed_action.get("action_type")) or "activate_control",
            "reducer": _text(effect_action.get("reducer")) if effect_action else _text(action.get("reducer")),
            "source_filename": source_filename,
            "target_filename": _text(action.get("target_filename")),
            "runtime_binding": runtime_binding,
            "state_oracles": typed_action.get("state_oracles", []) if typed_action else [],
        }
        if effect_action:
            for key in (
                "effect_kind",
                "component_ref",
                "state_refs",
                "state_reads",
                "expected_state_diff",
                "transaction_id",
                "required_runtime_events",
                "dom_binding_required",
                "dom_binding_ref",
                "gesture_scope",
                "gesture_axis",
                "gesture_direction",
                "gesture_reveals",
                "visibility_precondition_action_id",
                "visibility_precondition",
                "required_visible_value",
            ):
                if effect_action.get(key) not in (None, "", [], {}):
                    typed_row[key] = effect_action.get(key)
        view["typed_actions"].append({
            key: value for key, value in typed_row.items() if value not in (None, "", [], {})
        })

    for page_filename, view in views_by_filename.items():
        page_spec = _as_dict(page_specs_by_filename.get(page_filename))
        forward_edges = _as_dict(navigation_contract.get("forward_edges_by_action_id"))
        return_ops = _as_dict(navigation_contract.get("return_operations_by_action_id"))
        fallback = _as_dict(navigation_contract.get("fallback_back_actions_by_page_id")).get(page_filename, {})
        page_forward = {k: v for k, v in forward_edges.items() if isinstance(v, dict) and _text(v.get("source_filename")) == page_filename}
        page_return = {k: v for k, v in return_ops.items() if isinstance(v, dict) and _text(v.get("source_page_id")) == page_filename}
        view["navigation_contract_slice"] = {
            "forward_edges": page_forward,
            "return_operations": page_return,
            "fallback_back_action": fallback,
        }
        page_refs = set(
            _as_list(
                _as_dict(_as_dict(state_store_schema.get("pages")).get(page_filename)).get("state_refs")
            )
        )
        for typed_action in view["typed_actions"]:
            if isinstance(typed_action, dict):
                ta = typed_contracts.get(_text(typed_action.get("action_id")), {})
                if isinstance(ta, dict):
                    page_refs.update(ta.get("state_reads", []))
                    page_refs.update(ta.get("state_writes", []))
                page_refs.update(_as_list(typed_action.get("state_refs")))
                page_refs.update(_as_list(typed_action.get("state_reads")))
        view["state_store_slice"] = {
            "state_refs": sorted(page_refs),
            "schema": _as_dict(_as_dict(state_store_schema.get("pages")).get(page_filename)),
        }
        view["typed_actions"].sort(key=lambda item: _action_sort_key(item.get("action_id") if isinstance(item, dict) else ""))
        view["action_count"] = len(view["typed_actions"])

        autocomplete_contracts = _as_list(
            _as_dict(domain.get("autocomplete_contracts_by_page_id")).get(page_filename)
        )
        initial_control_contracts = _as_list(
            _as_dict(domain.get("initial_control_state_contracts_by_page_id")).get(page_filename)
        )
        if autocomplete_contracts:
            view["autocomplete_contracts"] = autocomplete_contracts
            for ac in autocomplete_contracts:
                if isinstance(ac, dict) and ac.get("min_visible_count"):
                    view["post_action_oracles"].append({
                        "type": "autocomplete_after_input_render",
                        "action_id": ac.get("input_action_id", ""),
                        "min_visible_count": ac["min_visible_count"],
                        "source": "autocomplete_contract",
                    })
        if initial_control_contracts:
            view["initial_control_state_contracts"] = initial_control_contracts

        if page_spec:
            view["initial_state"] = _as_dict(page_spec.get("initial_state"))
            view["state_schema"] = _as_dict(page_spec.get("state_schema"))
            action_ids = {
                _text(item.get("action_id"))
                for item in view["typed_actions"]
                if isinstance(item, dict) and _text(item.get("action_id"))
            }
            trace_ids = {
                action_id.rsplit(":step", 1)[0]
                for action_id in action_ids
                if ":step" in action_id
            }
            view["initial_state_by_trace_id"] = {
                trace_id: initial_state_by_trace_id[trace_id]
                for trace_id in sorted(trace_ids)
                if trace_id in initial_state_by_trace_id
            }
            view["transition_edges"] = [
                {
                    key: transition.get(key)
                    for key in (
                        "step_key",
                        "direction",
                        "operation",
                        "operation_intent",
                        "reducer",
                        "source_filename",
                        "target_filename",
                        "state_effect",
                        "transition_identity_key",
                    )
                    if transition.get(key) not in (None, "", [], {})
                }
                for transition in _as_list(page_spec.get("transitions"))
                if isinstance(transition, dict)
            ]
            view["render_projections"] = [
                {
                    key: projection.get(key)
                    for key in (
                        "projection_id",
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
                        "l0_transition_ids",
                        "dom_contract",
                        "render_contract",
                    )
                    if projection.get(key) not in (None, "", [], {})
                }
                for projection in _as_list(page_spec.get("render_projections"))
                if isinstance(projection, dict)
            ]
            view["surface_contracts"] = [
                {
                    key: component.get(key)
                    for key in (
                        "component_id",
                        "component_kind",
                        "component_role",
                        "render_phase",
                        "initial_dom_eligible",
                        "initial_visibility_policy",
                        "runtime_visibility_policy",
                        "state_surface_contract",
                        "value_lifecycle",
                        "source_step_keys",
                        "l0_transition_ids",
                        "interaction_contract",
                        "dom_contract",
                    )
                    if component.get(key) not in (None, "", [], {})
                }
                for component in _as_list(page_spec.get("components"))
                if isinstance(component, dict)
            ]
            view["post_action_oracles"] = [
                dict(oracle)
                for oracle in _as_list(page_spec.get("oracles"))
                if isinstance(oracle, dict)
            ]
            view["transaction_edges"] = [
                dict(transactions_by_action_id[action_id])
                for action_id in sorted(action_ids)
                if isinstance(transactions_by_action_id.get(action_id), dict)
            ]

        dom_obligations = _as_list(
            _as_dict(domain.get("initial_dom_contracts_by_page_id")).get(page_filename)
        )
        control_dom_entries = [
            {
                "control_id": c.get("control_id", ""),
                "label": c.get("label", ""),
                "control_type": c.get("control_type", ""),
                "expected_checked": c.get("expected_checked"),
                "expected_value": c.get("expected_value"),
                "expected_visible": c.get("expected_visible", True),
                "dom_oracle": c.get("dom_oracle", ""),
            }
            for c in initial_control_contracts if isinstance(c, dict)
        ]
        combined_dom = dom_obligations + control_dom_entries
        if combined_dom:
            view["initial_dom_contract"] = combined_dom

        page_transactions = [
            t for t in _as_list(domain.get("global_runtime_transactions"))
            if isinstance(t, dict) and (
                _text(t.get("source_page")) == page_filename
                or _text(t.get("target_page")) == page_filename
            )
        ]
        if page_transactions:
            view["global_runtime_transaction_contracts"] = page_transactions

        effect_slice = [
            _compact_action_effect_for_pagegen(action)
            for action in effect_actions
            if _text(action.get("source_filename")) == page_filename
        ]
        if effect_slice:
            view["action_effect_contracts"] = effect_slice

        runtime_slice = [
            _compact_runtime_transaction_for_pagegen(tx)
            for tx in runtime_transactions
            if _runtime_transaction_mentions_page(tx, page_filename)
        ]
        if runtime_slice:
            view["runtime_transaction_contracts"] = runtime_slice

        dom_slice = [
            _compact_dom_binding_for_pagegen(binding)
            for binding in dom_bindings
            if _text(binding.get("source_filename")) == page_filename
        ]
        if dom_slice:
            view["dom_binding_contracts"] = dom_slice

        schema_projection = _compact_global_schema_projection_for_pagegen(
            global_execution_schema or {},
            page_filename,
            state_refs=_pagegen_view_state_refs(view),
        )
        if schema_projection.get("fields"):
            view["global_schema_projection"] = schema_projection

        if effect_slice or runtime_slice or dom_slice:
            view["action_effect_authority_paths"] = {
                "action_effect_contracts": "semantic_api/08_spec_compiled_execution_views/action_effect_contracts.json",
                "runtime_transaction_contracts": "semantic_api/08_spec_compiled_execution_views/runtime_transaction_contracts.json",
                "dom_binding_contracts": "semantic_api/08_spec_compiled_execution_views/dom_binding_contracts.json",
            }
        if view.get("global_schema_projection"):
            view.setdefault("action_effect_authority_paths", {})
            view["action_effect_authority_paths"]["global_execution_schema"] = (
                "phase_consolidation/schema_registry/global_execution_schema.json"
            )

        stateful_actions = [
            action
            for action in view["typed_actions"]
            if isinstance(action, dict)
            and (
                _text(action.get("effect_kind")) == "same_page_mutation"
                or bool(_as_list(action.get("state_refs")))
                or bool(_as_dict(action.get("expected_state_diff")))
                or _text(action.get("reducer"))
                in {
                    "activate_inline_state",
                    "switch_tab",
                    "select_filter_option",
                    "update_cart_quantity",
                    "open_overlay",
                    "close_overlay",
                }
            )
        ]
        view["stateful_execution_required"] = bool(stateful_actions)
        semantic_digest_payload = {
            key: view.get(key)
            for key in (
                "initial_state",
                "state_schema",
                "state_store_slice",
                "typed_actions",
                "transition_edges",
                "transaction_edges",
                "render_projections",
                "surface_contracts",
                "post_action_oracles",
                "action_effect_contracts",
                "runtime_transaction_contracts",
                "dom_binding_contracts",
            )
        }
        view["semantic_contract_digest"] = _stable_schema_hash(semantic_digest_payload)

    return {
        "format": "pagegen_execution_view_by_page",
        "schema_revision": 5,
        "source": "spec_executability_eval + typed_action_contracts + domain_contracts + action_effect_contracts",
        "views_by_filename": views_by_filename,
        "summary": {
            "page_count": len(views_by_filename),
            "action_count": sum(v.get("action_count", 0) for v in views_by_filename.values()),
            "global_schema_projection_page_count": sum(
                1 for v in views_by_filename.values() if v.get("global_schema_projection")
            ),
        },
    }


def _compact_global_schema_projection_for_pagegen(
    global_schema: Mapping[str, Any],
    page_filename: str,
    *,
    state_refs: Iterable[str] = (),
) -> Dict[str, Any]:
    fields = [
        field for field in _as_list(global_schema.get("global_fields"))
        if isinstance(field, dict) and _schema_field_relevant_to_page(field, page_filename, state_refs)
    ]
    compact_fields = [_compact_global_schema_field_for_pagegen(field) for field in fields]
    autocomplete_contracts = _schema_autocomplete_contracts_for_page(compact_fields, page_filename)
    field_paths = [_text(field.get("field_path")) for field in compact_fields if _text(field.get("field_path"))]
    return {
        key: value
        for key, value in {
            "format": "pagegen_global_schema_projection_v1",
            "schema_id": _text(global_schema.get("schema_id") or "global_execution_schema"),
            "source": "phase_consolidation/schema_registry/global_execution_schema.json",
            "canonical_schema_hash": _stable_schema_hash(global_schema),
            "canonical_schema_source": _text(
                (global_schema.get("source_provenance") or {}).get("stage")
            ) if isinstance(global_schema.get("source_provenance"), Mapping) else "phase_consolidation/schema_registry/global_execution_schema.json",
            "page_id": page_filename,
            "fields": compact_fields,
            "autocomplete_contracts": autocomplete_contracts,
            "field_count": len(compact_fields),
            "schema_projection_validation": {
                "status": "pass",
                "field_count": len(compact_fields),
                "field_paths": field_paths,
                "source": "compact_global_schema_projection_for_pagegen",
            },
        }.items()
        if value not in (None, "", [], {})
    }


def _stable_schema_hash(global_schema: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(global_schema, ensure_ascii=False, sort_keys=True)
    except TypeError:
        payload = str(global_schema)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _schema_field_relevant_to_page(
    field: Mapping[str, Any],
    page_filename: str,
    state_refs: Iterable[str],
) -> bool:
    page_refs = {
        _text(item)
        for item in [
            *_as_list(field.get("projection_pages")),
            *_as_list(field.get("consumers")),
            *_as_list(field.get("producers")),
        ]
    }
    if any(_page_ref_matches(page_filename, page_ref) for page_ref in page_refs):
        return True
    required_refs = {_text(ref) for ref in state_refs if _text(ref)}
    if _text(field.get("field_path")) in required_refs:
        return True
    aliases = {_text(alias) for alias in _as_list(field.get("aliases"))}
    return bool(aliases & required_refs)


_PAGE_REF_STOP_TOKENS = {
    "html",
    "page",
    "screen",
    "component",
    "route",
    "12306",
}

_PAGE_REF_TOKEN_ALIASES = {
    "selected": "select",
    "selecting": "select",
    "selection": "select",
    "selector": "select",
    "passengers": "passenger",
    "tickets": "ticket",
    "orders": "order",
    "details": "detail",
    "results": "result",
    "profiles": "profile",
}


def _page_ref_matches(page_filename: str, raw_ref: str) -> bool:
    page_text = _text(page_filename)
    ref_text = _text(raw_ref)
    if not page_text or not ref_text:
        return False
    if page_text == ref_text:
        return True
    page_tokens = _page_ref_tokens(page_text)
    ref_tokens = _page_ref_tokens(ref_text)
    if not page_tokens or not ref_tokens:
        return False
    if page_tokens == ref_tokens:
        return True
    if len(page_tokens) == 1 and page_tokens[0] in ref_tokens:
        return True
    if len(ref_tokens) == 1 and ref_tokens[0] in page_tokens:
        return True
    page_set = set(page_tokens)
    ref_set = set(ref_tokens)
    return page_set.issubset(ref_set) or ref_set.issubset(page_set)


def _page_ref_tokens(value: str) -> List[str]:
    text = _text(value).lower()
    if text.startswith("c_"):
        text = text[2:]
    text = re.sub(r"\.html?$", "", text)
    raw_tokens = re.findall(r"[a-z0-9]+", text.replace("_", " ").replace("-", " "))
    tokens: List[str] = []
    for token in raw_tokens:
        token = _PAGE_REF_TOKEN_ALIASES.get(token, token)
        if token in _PAGE_REF_STOP_TOKENS:
            continue
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _compact_global_schema_field_for_pagegen(field: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in {
            "field_path": _text(field.get("field_path")),
            "field_type": _text(field.get("field_type")),
            "lifecycle": _text(field.get("lifecycle")),
            "producers": _as_list(field.get("producers"))[:8],
            "consumers": _as_list(field.get("consumers"))[:8],
            "aliases": _as_list(field.get("aliases"))[:8],
            "source_collection": _text(field.get("source_collection")),
            "candidate_projection": _as_list(field.get("candidate_projection"))[:8],
            "commit_target": _text(field.get("commit_target")),
        }.items()
        if value not in (None, "", [], {})
    }


def _schema_autocomplete_contracts_for_page(
    compact_fields: List[Dict[str, Any]],
    page_filename: str,
) -> List[Dict[str, Any]]:
    by_path = {_text(field.get("field_path")): field for field in compact_fields}
    contracts: List[Dict[str, Any]] = []
    if "stationPicker.query" in by_path or "stationPicker.suggestions" in by_path:
        contracts.append({
            "query_ref": "stationPicker.query",
            "source_collection": "Station",
            "candidate_projection": ["name", "fullName", "pinyin", "code"],
            "suggestions_ref": "stationPicker.suggestions",
            "commit_target": "searchForm[stationPicker.targetField]",
            "page_id": page_filename,
            "source": "global_execution_schema",
        })
    return contracts


def _pagegen_view_state_refs(view: Mapping[str, Any]) -> List[str]:
    refs: List[str] = []

    def add(raw: Any) -> None:
        text = _text(raw)
        if text and text not in refs:
            refs.append(text)

    state_store = _as_dict(view.get("state_store_slice"))
    for ref in _as_list(state_store.get("state_refs")):
        add(ref)
    for tx in _as_list(view.get("runtime_transaction_contracts")):
        if not isinstance(tx, dict):
            continue
        for ref in _as_list(tx.get("state_reads")) + _as_list(tx.get("state_writes")):
            add(ref)
    for action in _as_list(view.get("action_effect_contracts")):
        if not isinstance(action, dict):
            continue
        for ref in _as_list(action.get("state_refs")):
            add(ref)
        for key in _as_dict(action.get("expected_state_diff")).keys():
            add(key)
    return refs


def _compact_action_effect_for_pagegen(action: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: action.get(key)
        for key in (
            "action_id",
            "operation",
            "reducer",
            "effect_kind",
            "source_filename",
            "expected_target_filename",
            "transaction_id",
            "dom_binding_ref",
            "state_refs",
            "expected_state_diff",
            "required_runtime_events",
            "runtime_transaction",
            "caller_context",
            "selected_value",
            "component_ref",
            "dom_binding_required",
            "gesture_scope",
            "gesture_component_role",
            "gesture_axis",
            "gesture_direction",
            "gesture_reveals",
            "state_reads",
            "visibility_precondition_action_id",
            "visibility_precondition",
            "required_visible_value",
        )
        if action.get(key) not in (None, "", [], {})
    }


def _runtime_transaction_mentions_page(tx: Mapping[str, Any], page_filename: str) -> bool:
    candidates = {
        _text(tx.get("source_page")),
        _text(tx.get("target_page")),
        _text(tx.get("return_target")),
        _text(_as_dict(tx.get("caller")).get("source_filename")),
        _text(_as_dict(tx.get("resolver_surface")).get("filename")),
        _text(_as_dict(tx.get("commit")).get("return_target")),
        _text(_as_dict(tx.get("projection")).get("page")),
    }
    return page_filename in candidates


def _compact_runtime_transaction_for_pagegen(tx: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: tx.get(key)
        for key in (
            "transaction_id",
            "transaction_kind",
            "trace_id",
            "open_action_id",
            "commit_action_id",
            "source_action_id",
            "source_page",
            "target_page",
            "state_reads",
            "state_writes",
            "caller",
            "resolver_surface",
            "commit",
            "projection",
            "return_target",
            "negative_probes",
        )
        if tx.get(key) not in (None, "", [], {})
    }


def _compact_dom_binding_for_pagegen(binding: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: binding.get(key)
        for key in (
            "dom_binding_id",
            "action_id",
            "source_filename",
            "element_identity",
            "interaction",
            "postcondition",
            "forbidden",
        )
        if binding.get(key) not in (None, "", [], {})
    }


def build_runtime_binding_manifest(spec_executability_eval: Mapping[str, Any]) -> Dict[str, Any]:

    report = spec_executability_eval if isinstance(spec_executability_eval, dict) else {}
    derived_contract = _as_dict(report.get("derived_contract"))
    actions_by_id = _as_dict(derived_contract.get("actions_by_id"))
    transactions_by_action_id = _as_dict(derived_contract.get("transactions_by_action_id"))
    assertions = _assertions_by_action(derived_contract)
    runtime_actions: Dict[str, Dict[str, Any]] = {}
    for action_id, action in actions_by_id.items():
        if isinstance(action, dict):
            runtime_actions[_text(action.get("action_id") or action_id)] = _runtime_binding_for_action(action, assertions)

    return {
        "format": "spec_runtime_binding_manifest",
        "schema_revision": 1,
        "source": "spec_executability_eval.derived_contract",
        "interfaces": {
            "reset_state": {
                "signature": "reset_state(trace_id=None)",
                "source": "page_environment_specs.initial_state",
            },
            "step": {
                "signature": "step(action_id)",
                "source": "spec_executability_eval.derived_contract.transactions_by_action_id",
            },
            "navigate": {
                "signature": "navigate(action_id)",
                "source": "spec_executability_eval.derived_contract.navigation_contract.forward_edges_by_action_id",
            },
            "returnBack": {
                "signature": "returnBack(action_id)",
                "source": "spec_executability_eval.derived_contract.navigation_contract.return_operations_by_action_id",
            },
            "inspectNavigationStack": {
                "signature": "inspectNavigationStack()",
                "source": "runtime_navigation_stack",
            },
            "inspect_state": {
                "signature": "inspect_state()",
                "source": "runtime_state_store",
            },
            "state_diff": {
                "signature": "state_diff(before, after)",
                "source": "spec_executability_eval.derived_contract.trace_replay_assertions",
            },
        },
        "actions_by_id": runtime_actions,
        "initial_state_by_trace_id": _as_dict(derived_contract.get("initial_state_by_trace_id")),
        "record_identity_schema": _as_dict(derived_contract.get("record_identity_schema")),
        "transactions_by_action_id": transactions_by_action_id,
        "navigation_contract": _as_dict(derived_contract.get("navigation_contract")),
        "reducers": list(_as_list(derived_contract.get("reducers"))),
        "state_store_schema": _as_dict(derived_contract.get("state_store_schema")),
        "reward_contract": _as_dict(derived_contract.get("reward_contract")),
        "summary": {
            "action_count": len(runtime_actions),
            "transaction_count": len(transactions_by_action_id),
            "reducer_count": len(_as_list(derived_contract.get("reducers"))),
        },
    }


def build_contract_replay_plan(
    spec_executability_eval: Mapping[str, Any],
    *,
    skipped_steps: Iterable[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:

    report = spec_executability_eval if isinstance(spec_executability_eval, dict) else {}
    derived_contract = _as_dict(report.get("derived_contract"))
    actions_by_id = _as_dict(derived_contract.get("actions_by_id"))
    transactions_by_action_id = _as_dict(derived_contract.get("transactions_by_action_id"))
    initial_state_by_trace_id = _as_dict(derived_contract.get("initial_state_by_trace_id"))
    sessions_by_trace_id = _as_dict(derived_contract.get("sessions_by_trace_id"))
    navigation_contract = _as_dict(derived_contract.get("navigation_contract"))
    boundary_by_action_id = _as_dict(
        derived_contract.get("step_boundary_contracts")
    )
    forward_edges = _as_dict(navigation_contract.get("forward_edges_by_action_id"))
    return_ops = _as_dict(navigation_contract.get("return_operations_by_action_id"))
    assertions = _assertions_by_action(derived_contract)
    traces: List[Dict[str, Any]] = []
    skipped_by_action_id: Dict[str, Dict[str, Any]] = {}
    for raw in skipped_steps or []:
        if not isinstance(raw, Mapping):
            continue
        trace_id = _text(raw.get("trace_id"))
        try:
            step_index = int(raw.get("step_index"))
        except (TypeError, ValueError):
            continue
        if not trace_id or step_index < 0:
            continue
        action_id = f"{trace_id}:step{step_index}"
        skipped_by_action_id.setdefault(
            action_id,
            {
                "action_id": action_id,
                "trace_id": trace_id,
                "step_index": step_index,
                "skip_reason": _text(raw.get("skip_reason") or raw.get("reason")),
                "authority": _text(raw.get("authority") or raw.get("source")),
            },
        )

    for trace_id in sorted(sessions_by_trace_id):
        session = _as_dict(sessions_by_trace_id.get(trace_id))
        steps: List[Dict[str, Any]] = []
        stack_depth = 0
        for action_id in sorted(_as_list(session.get("action_ids")), key=_action_sort_key):
            action_key = _text(action_id)
            if action_key in skipped_by_action_id:
                continue
            action = _as_dict(actions_by_id.get(action_key))
            if not action:
                continue
            assertion = assertions.get(action_key, {})
            transaction = _as_dict(transactions_by_action_id.get(action_key))
            boundary_contract = _as_dict(
                action.get("boundary_contract")
                or transaction.get("boundary_contract")
                or boundary_by_action_id.get(action_key)
            )
            nav_stack_diff: Dict[str, Any] = {}
            if action_key in return_ops:
                stack_depth = max(0, stack_depth - 1)
                nav_stack_diff = {"direction": "pop", "depth_after": stack_depth}
            elif action_key in forward_edges:
                edge = forward_edges[action_key]
                mode = _text(edge.get("history_mode"))
                if mode == "push":
                    stack_depth += 1
                nav_stack_diff = {"direction": mode, "depth_after": stack_depth}
            steps.append(
                {
                    "action_id": action_key,
                    "operation": _text(action.get("operation")),
                    "source_filename": _text(action.get("source_filename")),
                    "target_filename": _text(action.get("target_filename")),
                    "reducer": _text(action.get("reducer")),
                    "assertions": {
                        "expected_filename": _text(assertion.get("expected_filename")),
                        "expected_state_diff": _as_dict(assertion.get("expected_state_diff")),
                    },
                    "transaction": transaction,
                    "expected_navigation_stack_diff": nav_stack_diff,
                    "boundary_contract": deepcopy(boundary_contract),
                    "page_boundary_authority": deepcopy(boundary_contract),
                }
            )
        traces.append(
            {
                "trace_id": _text(trace_id),
                "initial_state": deepcopy(_as_dict(initial_state_by_trace_id.get(trace_id))),
                "initial_navigation_stack": [],
                "steps": steps,
            }
        )

    issue_summary = _as_dict(report.get("summary"))
    blocking_issue_count = int(issue_summary.get("blocking_issue_count", 0) or 0)
    reward_contract = _as_dict(derived_contract.get("reward_contract")) or {
        "reward_initial_expected": 0,
        "reward_final_expected": 1,
    }
    return {
        "format": "spec_contract_replay_plan",
        "schema_revision": 1,
        "source": "spec_executability_eval.derived_contract",
        "reward_initial_expected": int(reward_contract.get("reward_initial_expected", 0) or 0),
        "reward_final_expected": int(reward_contract.get("reward_final_expected", 1) or 1),
        "readiness": {
            "ready": blocking_issue_count == 0,
            "blocking_issue_count": blocking_issue_count,
            "spec_status": _text(issue_summary.get("status")),
        },
        "reward_contract": reward_contract,
        "excluded_reference_skipped_actions": [
            skipped_by_action_id[action_id]
            for action_id in sorted(skipped_by_action_id, key=_action_sort_key)
            if action_id in actions_by_id
        ],
        "traces": traces,
        "summary": {
            "trace_count": len(traces),
            "step_count": sum(len(trace.get("steps", [])) for trace in traces),
            "transaction_count": sum(len(trace.get("steps", [])) for trace in traces),
            "excluded_reference_skipped_action_count": sum(
                1 for action_id in skipped_by_action_id if action_id in actions_by_id
            ),
        },
    }


def build_returnability_eval(spec_executability_eval: Mapping[str, Any]) -> Dict[str, Any]:

    report = spec_executability_eval if isinstance(spec_executability_eval, dict) else {}
    derived_contract = _as_dict(report.get("derived_contract"))
    navigation_contract = _as_dict(derived_contract.get("navigation_contract"))
    return_ops = _as_dict(navigation_contract.get("return_operations_by_action_id"))
    fallback_backs = _as_dict(navigation_contract.get("fallback_back_actions_by_page_id"))
    pages = _as_dict(derived_contract.get("pages"))
    issues: List[Dict[str, Any]] = []

    pages_by_status: Dict[str, List[str]] = {
        "required": [],
        "optional": [],
        "forbidden": [],
        "unknown": [],
    }

    returnability_by_page: Dict[str, Dict[str, Any]] = {}
    for page_filename in sorted(pages):
        return_action_ids = [
            aid for aid, op in return_ops.items()
            if _text(op.get("source_page_id")) == page_filename
        ]
        has_fallback = page_filename in fallback_backs
        return_op_count = len(return_action_ids)

        if return_op_count > 0 or has_fallback:
            status = "required"
        else:
            status = "unknown"

        fallback_info = fallback_backs.get(page_filename, {})
        fallback_target = _text(_as_dict(fallback_info.get("target_resolution")).get("fallback_page_id")) if fallback_info else ""

        if fallback_target == page_filename:
            issues.append({
                "code": "fallback_back_self_loop",
                "severity": BLOCKING,
                "page_filename": page_filename,
                "message": "fallback_back_action targets the current page",
            })

        if fallback_target and fallback_target in fallback_backs:
            other_target = _text(
                _as_dict(fallback_backs[fallback_target].get("target_resolution")).get("fallback_page_id")
            )
            if other_target == page_filename:
                issues.append({
                    "code": "fallback_back_two_page_loop",
                    "severity": BLOCKING,
                    "page_filename": page_filename,
                    "message": f"fallback_back forms a two-page loop with {fallback_target}",
                })

        for aid in return_action_ids:
            op = return_ops[aid]
            if _text(op.get("stack_owner")) != "GhostwebRouteExecution":
                issues.append({
                    "code": "return_operation_without_stack_owner",
                    "severity": BLOCKING,
                    "page_filename": page_filename,
                    "action_id": aid,
                    "message": "return operation has no stack owner",
                })
            monotonicity = _as_dict(op.get("monotonicity"))
            if not monotonicity.get("does_not_push"):
                issues.append({
                    "code": "return_operation_without_monotonicity",
                    "severity": BLOCKING,
                    "page_filename": page_filename,
                    "action_id": aid,
                    "message": "return operation lacks monotonicity proof",
                })

        returnability_by_page[page_filename] = {
            "returnability_status": status,
            "surface_count": return_op_count + (1 if has_fallback else 0),
            "return_operation_count": return_op_count,
            "cold_start_fallback_status": "present" if has_fallback else "absent",
            "monotonicity_status": "valid" if not any(
                i["page_filename"] == page_filename
                for i in issues
                if i["code"] in ("return_operation_without_monotonicity", "return_operation_without_stack_owner")
            ) else "invalid",
        }
        pages_by_status[status].append(page_filename)

    blocking_count = sum(1 for i in issues if i.get("severity") == BLOCKING)
    return {
        "format": "returnability_eval",
        "schema_revision": 1,
        "source": "spec_executability_eval.derived_contract.navigation_contract",
        "returnability_summary": {
            "page_count": len(pages),
            "by_status": {k: len(v) for k, v in pages_by_status.items()},
            "blocking_issue_count": blocking_count,
            "issue_count": len(issues),
        },
        "returnability_by_page": returnability_by_page,
        "issues": issues,
    }


def build_state_delta_spec_coverage_report(
    state_delta_signatures: Mapping[str, Any],
    typed_action_contracts: Mapping[str, Any],
    domain_contracts: Mapping[str, Any],
    *,
    skipped_steps: Iterable[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    delta_by_step = (
        state_delta_signatures.get("state_delta_signatures_by_step", {})
        if isinstance(state_delta_signatures, dict) else {}
    )
    contracts_by_id = (
        typed_action_contracts.get("typed_action_contracts_by_action_id", {})
        if isinstance(typed_action_contracts, dict) else {}
    )

    coverage_entries: List[Dict[str, Any]] = []
    excluded_entries: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    covered_count = 0
    excluded_count = 0
    skipped_by_action_id: Dict[str, Dict[str, Any]] = {}
    for raw_skip in skipped_steps or []:
        skip = _as_dict(raw_skip)
        trace_id = _text(skip.get("trace_id"))
        step_index = skip.get("step_index")
        try:
            step_no = int(step_index)
        except (TypeError, ValueError):
            continue
        if trace_id and step_no >= 0:
            skipped_by_action_id[f"{trace_id}:step{step_no}"] = skip

    for obs_step, deltas in delta_by_step.items():
        if not isinstance(deltas, list):
            continue
        for delta in deltas:
            if not isinstance(delta, dict):
                continue
            action_id = _text(delta.get("action_id"))
            signature = _text(delta.get("signature"))
            state_scope = _text(delta.get("state_scope"))
            mutation = _text(delta.get("mutation"))
            coverage_class = _text(delta.get("coverage_class"))
            if coverage_class and coverage_class != "requires_spec_contract":
                excluded_count += 1
                continue

            skipped = skipped_by_action_id.get(action_id)
            if skipped is not None:
                excluded_count += 1
                excluded_entries.append({
                    "obs_step": obs_step,
                    "action_id": action_id,
                    "signature": signature,
                    "state_scope": state_scope,
                    "mutation": mutation,
                    "coverage_class": "excluded_reference_skipped_action",
                    "exclusion_reason": "reference step_disposition excludes this action from admitted execution",
                    "skip_reason": _text(skipped.get("skip_reason")),
                    "skip_authority": _text(skipped.get("authority") or skipped.get("source")),
                })
                continue

            contract = _as_dict(contracts_by_id.get(action_id))
            reducer = _text(contract.get("reducer"))
            state_writes = _as_list(contract.get("state_writes"))
            state_oracles = _as_list(contract.get("state_oracles"))
            dom_oracles = _as_list(contract.get("dom_oracles"))
            navigation_effect = _as_dict(contract.get("navigation_effect"))

            matched_contract_id = ""
            matched_field = ""
            match_reason = ""
            match_type = ""

            scope_lower = state_scope.lower()
            scope_tokens = set(scope_lower.replace(".", "_").split("_"))
            scope_tokens.discard("")

            for write_ref in state_writes:
                if scope_lower in str(write_ref).lower():
                    matched_contract_id = action_id
                    matched_field = f"state_writes[{write_ref}]"
                    match_reason = f"state_write ref contains scope '{state_scope}'"
                    match_type = "exact_scope_match"
                    break

            if not matched_contract_id:
                for oracle in state_oracles:
                    if isinstance(oracle, dict):
                        diff = oracle.get("diff", {})
                        if any(scope_lower in str(k).lower() for k in diff):
                            matched_contract_id = action_id
                            matched_field = "state_oracle.diff"
                            match_reason = f"state_oracle diff key matches scope '{state_scope}'"
                            match_type = "exact_scope_match"
                            break

            if not matched_contract_id and reducer:
                if scope_lower in reducer.lower() or mutation.lower() in reducer.lower():
                    matched_contract_id = action_id
                    matched_field = f"reducer[{reducer}]"
                    match_reason = f"reducer '{reducer}' matches scope/mutation"
                    match_type = "exact_scope_match"

            if not matched_contract_id:
                for oracle in dom_oracles:
                    if isinstance(oracle, dict):
                        oracle_type = _text(oracle.get("type"))
                        if scope_lower in oracle_type or mutation.lower() in oracle_type:
                            matched_contract_id = action_id
                            matched_field = f"dom_oracle[{oracle_type}]"
                            match_reason = f"dom_oracle type matches scope/mutation"
                            match_type = "exact_scope_match"
                            break

            if not matched_contract_id and navigation_effect:
                matched_contract_id = action_id
                matched_field = "navigation_effect"
                match_reason = (
                    f"action has route/navigation effect from "
                    f"'{_text(navigation_effect.get('source_filename'))}' to "
                    f"'{_text(navigation_effect.get('target_filename'))}'"
                )
                match_type = "navigation_effect_match"

            if not matched_contract_id and (reducer or state_writes or state_oracles or dom_oracles):
                matched_contract_id = action_id
                matched_field = "broad_contract_presence"
                match_type = "broad_match"

                _HIGH_RISK_SCOPE_TOKENS = frozenset({
                    "cart", "collection", "picker", "modal", "confirm",
                    "payment", "coupon", "form", "address", "hydration",
                    "cross_page", "commit", "filter", "category", "sort",
                    "checkout", "order",
                })
                risk_tokens = scope_tokens & _HIGH_RISK_SCOPE_TOKENS
                broad_risk = "high" if risk_tokens else "low"

                match_reason = (
                    f"action has contract with reducer='{reducer}', "
                    f"writes={len(state_writes)}, oracles={len(state_oracles)+len(dom_oracles)} "
                    f"but no exact scope match for '{state_scope}' (risk={broad_risk})"
                )

            covered = bool(matched_contract_id)
            if covered:
                covered_count += 1

            entry: Dict[str, Any] = {
                "obs_step": obs_step,
                "action_id": action_id,
                "signature": signature,
                "state_scope": state_scope,
                "mutation": mutation,
                "covered": covered,
            }
            if covered:
                entry["matched_contract_id"] = matched_contract_id
                entry["matched_contract_type"] = match_type
                entry["matched_field"] = matched_field
                entry["match_reason"] = match_reason
                if match_type == "broad_match":
                    entry["broad_match_risk_level"] = broad_risk
                    entry["missing_exact_scope"] = state_scope
            coverage_entries.append(entry)

            if not covered:
                has_any_contract = bool(contract)
                if not has_any_contract and action_id:
                    entry["evidence_status"] = "no_typed_contract_for_action"
                    issues.append({
                        "code": "state_delta_missing_spec_coverage",
                        "severity": BLOCKING,
                        "obs_step": obs_step,
                        "action_id": action_id,
                        "signature": signature,
                        "state_scope": state_scope,
                        "mutation": mutation,
                        "evidence_status": "no_typed_contract_for_action",
                        "message": (
                            f"state delta '{signature}' at {obs_step}: action {action_id} "
                            f"has no typed contract — PES producer gap"
                        ),
                    })
                elif action_id:
                    issues.append({
                        "code": "state_delta_missing_spec_coverage",
                        "severity": BLOCKING,
                        "obs_step": obs_step,
                        "action_id": action_id,
                        "signature": signature,
                        "state_scope": state_scope,
                        "mutation": mutation,
                        "evidence_status": "contract_exists_but_no_scope_match",
                        "message": (
                            f"state delta '{signature}' at {obs_step} (action {action_id}) "
                            f"has typed contract but no matching field for scope '{state_scope}'"
                        ),
                    })

    total = len(coverage_entries)
    return {
        "format": "state_delta_spec_coverage_report",
        "schema_revision": 3,
        "coverage_entries": coverage_entries,
        "excluded_entries": excluded_entries,
        "issues": issues,
        "summary": {
            "total_state_deltas": total,
            "excluded_from_denominator": excluded_count,
            "excluded_reference_skipped_action_count": len(excluded_entries),
            "covered_count": covered_count,
            "uncovered_count": total - covered_count,
            "issue_count": len(issues),
        },
    }


_CANONICAL_SEARCH_FORM_REFS = (
    "state::railway_search_form.origin_station",
    "state::railway_search_form.destination_station",
    "state::railway_search_form.travel_date",
)


def validate_stateful_submit_state_reads(
    contract_replay_plan: Mapping[str, Any],
    *,
    transaction_matrix: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    traces = _as_list(contract_replay_plan.get("traces"))
    issues: List[Dict[str, Any]] = []
    checked = 0
    passing = 0

    tx_traces = {}
    if isinstance(transaction_matrix, dict):
        for t in _as_list(transaction_matrix.get("traces")):
            if isinstance(t, dict):
                tx_traces[t.get("trace_id", "")] = t

    for trace in traces:
        if not isinstance(trace, dict):
            continue
        trace_id = _text(trace.get("trace_id"))
        steps = _as_list(trace.get("steps"))
        tx_trace = tx_traces.get(trace_id, {})
        tx_submit = _as_dict(tx_trace.get("submit"))

        for step in steps:
            if not isinstance(step, dict):
                continue
            transaction = _as_dict(step.get("transaction"))
            operation = _text(transaction.get("operation") or step.get("operation"))
            source = _text(transaction.get("source_filename") or step.get("source_filename"))
            target = _text(transaction.get("target_filename") or step.get("target_filename"))
            reducer = _text(transaction.get("reducer") or step.get("reducer"))

            is_submit = (
                operation == "click"
                and source == "home-train.html"
                and target.startswith("train-list-")
            )
            if not is_submit and "submit" not in reducer.lower() and "commit_query" not in reducer.lower():
                continue
            if not target.startswith("train-list-"):
                continue

            checked += 1
            step_index = transaction.get("step_index") or step.get("step_index", -1)

            state_reads = _as_list(transaction.get("state_reads") or step.get("state_reads"))
            canonical_reads_present = [
                ref for ref in _CANONICAL_SEARCH_FORM_REFS
                if any(ref in str(r) for r in state_reads)
            ]
            tx_state_reads = _as_list(tx_submit.get("state_reads"))
            tx_canonical = [
                ref for ref in _CANONICAL_SEARCH_FORM_REFS
                if any(ref in str(r) for r in tx_state_reads)
            ]

            all_canonical = set(canonical_reads_present) | set(tx_canonical)
            missing = [r for r in _CANONICAL_SEARCH_FORM_REFS if r not in all_canonical]

            if not missing:
                passing += 1
                continue

            issues.append({
                "trace_id": trace_id,
                "step_index": step_index,
                "source_filename": source,
                "expected_target": target,
                "code": "missing_submit_state_reads",
                "severity": BLOCKING,
                "missing_refs": missing,
                "declared_state_reads": state_reads[:10],
                "message": (
                    f"submit from {source} to {target} does not declare canonical "
                    f"railway_search_form state reads: {missing}"
                ),
            })

    return {
        "format": "stateful_submit_state_reads_validation",
        "schema_revision": 1,
        "summary": {
            "submit_steps_checked": checked,
            "submit_steps_passing": passing,
            "submit_steps_failing": checked - passing,
            "issue_count": len(issues),
        },
        "issues": issues,
    }
