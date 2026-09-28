
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Mapping, Set


def _text(value: Any) -> str:
    return str(value or "").strip()


def _base_filename(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return text.split("?")[0].split("#")[0].rsplit("/", 1)[-1]


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _terminal_contracts_by_trace_id(
    page_environment_specs: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    result: Dict[str, Dict[str, Any]] = {}
    explicit = payload.get("terminal_contracts_by_trace_id")
    if isinstance(explicit, dict):
        for trace_id, contract in explicit.items():
            if _text(trace_id) and isinstance(contract, dict):
                result[_text(trace_id)] = dict(contract)
    for spec in _as_list(payload.get("specs")):
        if not isinstance(spec, dict):
            continue
        trace_id = _text(spec.get("trace_id"))
        contract = spec.get("terminal_contract")
        if trace_id and isinstance(contract, dict) and contract:
            result.setdefault(trace_id, dict(contract))
    return result


def _terminal_overlay_rationale(
    action_id: str,
    trace_id: str,
    action_type: str,
    terminal_contracts: Mapping[str, Dict[str, Any]],
) -> Dict[str, Any]:
    if action_type != "open_overlay" or not trace_id:
        return {}
    contract = terminal_contracts.get(trace_id)
    if not isinstance(contract, dict) or not contract:
        return {}
    declared_trace_id = _text(contract.get("trace_id"))
    if declared_trace_id and declared_trace_id != trace_id:
        return {}
    try:
        last_step = int(contract.get("last_task_relevant_step"))
    except (TypeError, ValueError):
        return {}
    if action_id != f"{trace_id}:step{last_step}":
        return {}
    terminal_policy = _text(contract.get("terminal_policy_candidate"))
    allowed_final_state = _text(contract.get("allowed_final_state"))
    boundary_description = _text(contract.get("terminal_boundary_description"))
    if (
        terminal_policy
        not in {"precommit_ready", "simulated_commit", "observed_terminal_state"}
        or not allowed_final_state
        or not boundary_description
    ):
        return {}
    return {
        "terminal": True,
        "reason": "terminal_contract_allows_open_overlay",
        "terminal_policy_candidate": terminal_policy,
        "allowed_final_state": allowed_final_state,
        "last_task_relevant_step": last_step,
        "terminal_boundary_description": boundary_description,
        "source": _text(contract.get("source")) or "reference_trace_terminal_contract",
    }


_REDUCER_TO_ACTION_TYPE: Dict[str, str] = {
    "navigate_route": "navigate_route",
    "navigate_back": "return_back",
    "go_back": "return_back",
    "close_modal": "close_overlay",
    "close_overlay": "close_overlay",
    "close_sheet": "close_overlay",
    "close_drawer": "close_overlay",
    "open_overlay": "open_overlay",
    "open_option_modal": "open_overlay",
    "open_runtime_overlay": "open_overlay",
    "select_filter_option": "filter_select",
    "apply_filter": "filter_select",
    "switch_tab": "filter_select",
    "sort_select": "sort_select",
    "select_overlay_option": "select_overlay_option",
    "confirm_overlay": "confirm_overlay",
    "form_focus": "form_focus",
    "fill_form": "form_fill",
    "type_search": "query_draft_updated",
    "update_query_draft": "query_draft_updated",
    "submit_form": "form_commit",
    "commit_query": "query_committed",
    "commit_transaction": "form_commit",
    "terminal_submit": "form_commit",
    "terminal_commit": "form_commit",
    "select_record": "record_select",
    "selected_record_hydration": "record_select",
    "cart_mutation": "collection_mutation",
    "add_to_cart": "collection_mutation",
    "remove_from_cart": "collection_mutation",
    "update_cart_quantity": "collection_mutation",
    "project_suggestions": "query_autocomplete_project",
    "select_suggestion": "query_suggestion_select",
    "commit_draft": "cross_page_state_commit",
}

_OPERATION_HINT_TO_ACTION_TYPE: Dict[str, str] = {
    "filter": "filter_select",
    "sort": "sort_select",
    "筛选": "filter_select",
    "排序": "sort_select",
    "tab": "filter_select",
    "overlay": "open_overlay",
    "modal": "open_overlay",
    "sheet": "open_overlay",
    "drawer": "open_overlay",
    "选择": "record_select",
    "select": "record_select",
    "submit": "form_commit",
    "提交": "form_commit",
    "confirm": "form_commit",
    "确认": "form_commit",
    "pay": "form_commit",
    "支付": "form_commit",
}


def _overlay_text(transition: Mapping[str, Any]) -> str:
    state_effect = _as_dict(transition.get("state_effect"))
    return " ".join(
        item
        for item in (
            _text(transition.get("operation_intent")),
            _text(transition.get("command")),
            _text(state_effect.get("selected_label")),
            _text(state_effect.get("overlay_kind")),
            _text(state_effect.get("template_id")),
            _text(state_effect.get("domain_intent")),
        )
        if item
    )


def _has_overlay_surface_evidence(transition: Mapping[str, Any]) -> bool:
    state_effect = _as_dict(transition.get("state_effect"))
    reducer = _text(transition.get("reducer") or transition.get("action_intent")).lower()
    effect = _text(state_effect.get("effect")).lower()
    text = _overlay_text(transition).lower()
    if reducer in {"open_overlay", "open_option_modal", "open_runtime_overlay", "select_overlay_option", "confirm_overlay", "close_overlay"}:
        return True
    if effect in {"open_runtime_overlay", "open_overlay", "select_overlay_option", "confirm_overlay", "close_overlay"}:
        return True
    if _text(state_effect.get("overlay_kind") or state_effect.get("template_id")):
        return True
    return any(token in text for token in ("弹窗", "浮层", "面板", "overlay", "sheet", "drawer", "picker"))


def _overlay_confirm_action_type(transition: Mapping[str, Any]) -> str:
    if not _has_overlay_surface_evidence(transition):
        return ""
    text = _overlay_text(transition).lower()
    if any(token in text for token in ("取消", "关闭", "收起", "cancel", "close", "dismiss")):
        return "close_overlay"
    if any(token in text for token in ("确定", "确认", "完成", "done", "confirm", "ok")):
        return "confirm_overlay"
    return ""


def _is_overlay_option_consumer(transition: Mapping[str, Any]) -> bool:
    state_effect = _as_dict(transition.get("state_effect"))
    reducer = _text(transition.get("reducer") or transition.get("action_intent")).lower()
    operation = _text(transition.get("operation")).lower()
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    surface_contract = _as_dict(transition.get("surface_contract"))
    surface_kind = _text(surface_contract.get("surface_kind")).lower()
    surface_lifecycle = _text(surface_contract.get("lifecycle")).lower()
    evidenced_cross_page_overlay_exit = (
        operation in {"click", "tap"}
        and bool(source and target and source != target)
        and surface_kind in {
            "overlay",
            "runtime_overlay",
            "modal",
            "sheet",
            "drawer",
        }
        and surface_lifecycle in {"open", "opened", "active", "visible"}
    )
    if evidenced_cross_page_overlay_exit:
        return True
    if reducer not in {"navigate_route", "select_overlay_option"}:
        return False
    hydration = _as_dict(state_effect.get("selected_record_hydration") or transition.get("selected_record_hydration"))
    policy = _text(hydration.get("policy")).lower()
    mutation_policy = _text(state_effect.get("mutation_policy")).lower()
    effect = _text(state_effect.get("effect")).lower()
    state_ref = _text(state_effect.get("state_ref")).lower()
    text = _overlay_text(transition).lower()
    has_overlay_commit = (
        "overlay" in policy
        or "overlay" in mutation_policy
        or "runtime_overlay" in effect
        or "selected_runtime_overlay" in state_ref
        or any(token in text for token in ("弹窗", "浮层", "action_sheet", "action sheet", "面板", "overlay", "sheet"))
    )
    return bool(hydration and has_overlay_commit)


def _is_inline_overlay_option_consumer_candidate(transition: Mapping[str, Any]) -> bool:
    state_effect = _as_dict(transition.get("state_effect"))
    reducer = _text(transition.get("reducer") or transition.get("action_intent")).lower()
    if reducer not in {"activate_inline_state", "activate_control", "open_overlay", "open_option_modal", "open_runtime_overlay"}:
        return False
    operation = _text(transition.get("operation")).lower()
    if operation and operation not in {"click", "tap"}:
        return False
    if not _extract_state_writes(transition):
        return False
    text = _overlay_text(transition)
    lowered = text.lower()
    if any(token in text for token in ("展开更多", "更多支付方式", "展开")):
        return False
    in_overlay_surface = any(
        token in lowered or token in text
        for token in ("面板中", "浮层中", "弹窗中", "sheet", "overlay", "modal", "drawer")
    )
    explicit_open = any(token in lowered or token in text for token in ("打开", "展开", "open", "expand"))
    if reducer in {"open_overlay", "open_option_modal", "open_runtime_overlay"} and explicit_open and not in_overlay_surface:
        return False
    option_markers = (
        "选项",
        "选择按钮",
        "右侧的选择",
        "表情",
        "好友",
        "发送",
        "send",
        "submit",
        "done",
        "confirm",
        "payment option",
        "radio",
        "checkbox",
    )
    return in_overlay_surface or any(token in lowered or token in text for token in option_markers)


def _apply_overlay_consumer_action_type(contract: Dict[str, Any]) -> None:
    contract["action_type"] = "select_overlay_option"
    contract["dom_oracles"] = [
        oracle
        for oracle in _as_list(contract.get("dom_oracles"))
        if _text(_as_dict(oracle).get("type")) != "submit_feedback_visible"
    ]
    if not any(
        _text(_as_dict(oracle).get("type")) == "overlay_consumed_after_action"
        for oracle in _as_list(contract.get("dom_oracles"))
    ):
        contract.setdefault("dom_oracles", []).append({
            "type": "overlay_consumed_after_action",
            "description": "overlay/sheet must be consumed or hidden after selection/confirm action",
        })


def _infer_action_type(transition: Mapping[str, Any]) -> str:
    reducer = _text(transition.get("reducer") or transition.get("action_intent")).lower()
    overlay_confirm_type = _overlay_confirm_action_type(transition)
    if overlay_confirm_type and reducer in {"open_overlay", "open_option_modal", "open_runtime_overlay", "confirm_overlay", "close_overlay"}:
        return overlay_confirm_type
    if _is_overlay_option_consumer(transition):
        return "select_overlay_option"
    if reducer in _REDUCER_TO_ACTION_TYPE:
        return _REDUCER_TO_ACTION_TYPE[reducer]
    operation_intent = _text(transition.get("operation_intent")).lower()
    for token, action_type in _OPERATION_HINT_TO_ACTION_TYPE.items():
        if token in operation_intent:
            return action_type
    operation = _text(transition.get("operation")).lower()
    if operation in ("fill", "type"):
        return "form_fill"
    source = _text(transition.get("source_filename"))
    target = _text(transition.get("target_filename"))
    if source and target and source != target:
        return "navigate_route"
    return "activate_control"


def _extract_state_reads(transition: Mapping[str, Any]) -> List[str]:
    reads: Set[str] = set()
    state_effect = _as_dict(transition.get("state_effect"))
    for key in ("source_state_ref", "state_ref", "read_refs"):
        val = state_effect.get(key)
        if isinstance(val, str) and val:
            reads.add(val)
        elif isinstance(val, list):
            reads.update(_text(item) for item in val if _text(item))
    if _text(state_effect.get("effect")).lower() == "commit_draft":
        draft_ref = _text(state_effect.get("draft_ref"))
        identity_ref = _text(
            _as_dict(_as_dict(transition.get("transition_identity_key")).get("state_identity")).get("state_ref")
        )
        for ref in (draft_ref, identity_ref):
            if ref:
                reads.add(ref)
    return sorted(reads)


def _expected_state_diff(transition: Mapping[str, Any]) -> Dict[str, Any]:
    state_effect = _as_dict(transition.get("state_effect"))
    explicit = _as_dict(
        state_effect.get("expected_state_diff")
        or transition.get("expected_state_diff")
    )
    if explicit:
        return dict(explicit)


    if _text(state_effect.get("effect")).lower() != "update_draft":
        return {}
    draft_ref = _text(state_effect.get("draft_ref"))
    field_role = _text(state_effect.get("field_role"))
    if not draft_ref or not field_role or "value" not in state_effect:
        return {}
    return {f"{draft_ref}.{field_role}": state_effect.get("value")}


def _extract_state_writes(transition: Mapping[str, Any]) -> List[str]:
    writes: Set[str] = set()
    state_effect = _as_dict(transition.get("state_effect"))
    diff = _expected_state_diff(transition)
    for key in diff:
        ref = _text(key).split(".")[0] if "." in _text(key) else _text(key)
        if ref:
            writes.add(ref)
    target_ref = _text(state_effect.get("target_state_ref") or state_effect.get("state_ref"))
    if target_ref:
        writes.add(target_ref)
    if _text(state_effect.get("effect")).lower() == "update_draft":
        draft_ref = _text(state_effect.get("draft_ref"))
        if draft_ref:
            writes.add(draft_ref)
    return sorted(writes)


def _extract_navigation_effect(transition: Mapping[str, Any]) -> Dict[str, Any]:
    source = _text(transition.get("source_filename"))
    target = _text(transition.get("target_filename"))
    if not source or not target or source == target:
        return {}
    return {
        "source_filename": source,
        "target_filename": target,
        "cross_page": True,
    }


def compile_typed_action_contract(
    action_id: str,
    transition: Mapping[str, Any],
    page_filename: str,
) -> Dict[str, Any]:
    action_type = _infer_action_type(transition)
    reducer = _text(transition.get("reducer") or transition.get("action_intent"))
    state_effect = _as_dict(transition.get("state_effect"))
    expected_diff = _expected_state_diff(transition)

    contract: Dict[str, Any] = {
        "action_id": action_id,
        "trace_id": _text(transition.get("step_key", "")).split(":step")[0] if ":step" in _text(transition.get("step_key")) else "",
        "page_id": page_filename,
        "action_type": action_type,
        "surface_id": _text(transition.get("component_id") or transition.get("element_id") or ""),
        "reducer": reducer,
        "state_reads": _extract_state_reads(transition),
        "state_writes": _extract_state_writes(transition),
        "preconditions": [],
        "postconditions": [],
        "dom_oracles": [],
        "state_oracles": [],
        "navigation_effect": _extract_navigation_effect(transition),
        "evidence_refs": [action_id],
    }
    field_role = _text(state_effect.get("field_role"))
    if field_role:
        contract["field_role"] = field_role

    if expected_diff:
        contract["state_oracles"].append({
            "type": "expected_state_diff",
            "diff": expected_diff,
        })

    if action_type == "filter_select":
        contract["dom_oracles"].append({
            "type": "collection_re_render",
            "description": "collection/list must re-render after filter/tab selection",
        })
    elif action_type == "open_overlay":
        contract["dom_oracles"].append({
            "type": "overlay_visible_after_action",
            "description": "overlay/sheet must become visible after open action",
        })
    elif action_type in ("select_overlay_option", "confirm_overlay", "close_overlay"):
        if (
            action_type == "select_overlay_option"
            and state_effect.get("overlay_persists_after_action") is True
        ):
            contract["dom_oracles"].append({
                "type": "overlay_progressed_after_selection",
                "description": "overlay/sheet must preserve its surface and render the selected next lifecycle",
            })
        else:
            contract["dom_oracles"].append({
                "type": "overlay_consumed_after_action",
                "description": "overlay/sheet must be consumed or hidden after selection/confirm action",
            })
    elif action_type == "form_commit":
        nav = contract.get("navigation_effect", {})
        if nav.get("cross_page"):
            contract["postconditions"].append({
                "type": "navigate_to_target",
                "target_filename": nav.get("target_filename", ""),
            })
        else:
            contract["dom_oracles"].append({
                "type": "submit_feedback_visible",
                "description": "submit confirmation or button state change after form commit",
            })
    elif action_type in ("query_draft_updated", "query_autocomplete_project"):
        contract["dom_oracles"].append({
            "type": "autocomplete_candidates_visible",
            "description": "autocomplete suggestions must appear after query input",
        })
    elif action_type == "query_suggestion_select":
        contract["dom_oracles"].append({
            "type": "input_filled_after_select",
            "description": "search input must be filled with selected suggestion text",
        })

    return contract


def _contract_owner_rank(transition: Mapping[str, Any], page_filename: str) -> int:

    direction = _text(transition.get("direction") or "")
    source = _base_filename(transition.get("source_filename"))
    current = _base_filename(page_filename)
    rank = 0
    if direction == "source":
        rank += 10
    elif direction == "target":
        rank -= 10
    if source and current == source:
        rank += 5
    if _text(transition.get("l0_role")) == "execution_anchor":
        rank += 1
    return rank


def compile_typed_action_contracts(
    page_environment_specs: Mapping[str, Any],
) -> Dict[str, Any]:

    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = payload.get("page_environment_specs_by_filename", {})
    if not isinstance(specs_by_filename, dict):
        specs_by_filename = {}

    contracts_by_id: Dict[str, Dict[str, Any]] = {}
    owner_ranks_by_id: Dict[str, int] = {}
    l0_roles_by_id: Dict[str, str] = {}
    issues: List[Dict[str, Any]] = []
    terminal_contracts = _terminal_contracts_by_trace_id(payload)

    for page_filename, spec in specs_by_filename.items():
        if not isinstance(spec, dict):
            continue
        filename = _text(spec.get("filename") or page_filename)
        transitions = _as_list(spec.get("transitions"))
        active_overlay_by_trace: Set[str] = set()
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            action_id = _text(transition.get("step_key") or transition.get("action_id"))
            if not action_id:
                continue
            l0_role = _text(transition.get("l0_role"))
            if l0_role not in ("execution_anchor", "normalization", ""):
                continue
            contract = compile_typed_action_contract(action_id, transition, filename)
            trace_id = _text(contract.get("trace_id"))
            if trace_id in active_overlay_by_trace and _is_inline_overlay_option_consumer_candidate(transition):
                _apply_overlay_consumer_action_type(contract)
            terminal_overlay = _terminal_overlay_rationale(
                action_id,
                trace_id,
                _text(contract.get("action_type")),
                terminal_contracts,
            )
            if terminal_overlay:
                contract["terminal_overlay"] = terminal_overlay
            owner_rank = _contract_owner_rank(transition, filename)
            if action_id in contracts_by_id and owner_rank <= owner_ranks_by_id.get(action_id, -999):
                continue
            contracts_by_id[action_id] = contract
            owner_ranks_by_id[action_id] = owner_rank
            l0_roles_by_id[action_id] = l0_role
            action_type = _text(contract.get("action_type"))
            if trace_id:
                if action_type == "open_overlay":
                    active_overlay_by_trace.add(trace_id)
                elif action_type in {"select_overlay_option", "confirm_overlay", "close_overlay"}:
                    active_overlay_by_trace.discard(trace_id)

    for action_id, contract in contracts_by_id.items():
        if l0_roles_by_id.get(action_id) == "execution_anchor" and contract["action_type"] == "activate_control":
            if not contract["state_writes"] and not contract["navigation_effect"]:
                issues.append({
                    "code": "typed_action_missing_contract",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": _text(contract.get("page_id")),
                    "message": "L0-relevant action could not be compiled to a typed contract with state/navigation effect",
                })

    type_counts: Dict[str, int] = defaultdict(int)
    for contract in contracts_by_id.values():
        type_counts[_text(contract.get("action_type"))] += 1

    return {
        "format": "typed_action_contracts",
        "schema_revision": 1,
        "typed_action_contracts_by_action_id": contracts_by_id,
        "issues": issues,
        "summary": {
            "total_actions": len(contracts_by_id),
            "typed_count": sum(1 for c in contracts_by_id.values() if c["action_type"] != "activate_control"),
            "untyped_count": sum(1 for c in contracts_by_id.values() if c["action_type"] == "activate_control"),
            "issue_count": len(issues),
            "by_action_type": dict(sorted(type_counts.items())),
        },
    }


def compile_domain_contracts(
    typed_action_contracts: Mapping[str, Any],
    page_environment_specs: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    contracts_by_id = _as_dict(
        typed_action_contracts.get("typed_action_contracts_by_action_id")
        if isinstance(typed_action_contracts, dict) else {}
    )

    filter_contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    overlay_contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    form_contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    record_contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    collection_contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    cross_page_transactions: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []

    for action_id, contract in contracts_by_id.items():
        if not isinstance(contract, dict):
            continue
        page = _text(contract.get("page_id"))
        action_type = _text(contract.get("action_type"))

        if action_type == "filter_select":
            filter_contracts_by_page[page].append(contract)
            if not contract.get("state_writes"):
                issues.append({
                    "code": "filter_projection_missing",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "filter/tab action has no state_writes for collection re-render",
                })

        elif action_type in ("open_overlay", "select_overlay_option", "confirm_overlay", "close_overlay"):
            overlay_contracts_by_page[page].append(contract)

        elif action_type in ("form_fill", "form_commit", "form_focus"):
            form_contracts_by_page[page].append(contract)
            if action_type == "form_commit" and not contract.get("state_writes") and not contract.get("navigation_effect"):
                issues.append({
                    "code": "form_commit_missing",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "form commit action has no state writes or navigation effect",
                })

        elif action_type == "record_select":
            record_contracts_by_page[page].append(contract)
            if not contract.get("state_writes") and not contract.get("navigation_effect"):
                issues.append({
                    "code": "record_hydration_missing",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "record selection has no state commit or target hydration",
                })

        elif action_type == "collection_mutation":
            collection_contracts_by_page[page].append(contract)
            if not contract.get("state_writes"):
                issues.append({
                    "code": "collection_mutation_missing_projection",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "collection mutation action has no state writes for re-render",
                })

        elif action_type == "cross_page_state_commit":
            cross_page_transactions.append(contract)
            nav = contract.get("navigation_effect", {})
            if not nav.get("cross_page") or not contract.get("state_writes"):
                issues.append({
                    "code": "cross_page_transaction_unclosed",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "cross-page state commit lacks navigation or state writes",
                })

        elif action_type == "query_suggestion_select":
            form_contracts_by_page[page].append(contract)
            if not contract.get("state_writes"):
                issues.append({
                    "code": "query_suggestion_contract_missing",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "query suggestion select has no state writes",
                })

        elif action_type == "query_draft_updated":
            form_contracts_by_page[page].append(contract)
            if contract.get("navigation_effect", {}).get("cross_page"):
                issues.append({
                    "code": "query_lifecycle_collapsed",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "query draft update must not produce cross-page navigation to committed results",
                })

        elif action_type == "query_autocomplete_project":
            form_contracts_by_page[page].append(contract)
            if contract.get("navigation_effect", {}).get("cross_page"):
                issues.append({
                    "code": "query_lifecycle_collapsed",
                    "severity": "blocking",
                    "action_id": action_id,
                    "page_filename": page,
                    "message": "autocomplete projection must stay same-page; cannot navigate to a results page",
                })

        elif action_type == "query_committed":
            form_contracts_by_page[page].append(contract)

    for page, overlays in overlay_contracts_by_page.items():
        types = {_text(c.get("action_type")) for c in overlays}
        nonterminal_open_overlays = [
            contract
            for contract in overlays
            if _text(contract.get("action_type")) == "open_overlay"
            and _as_dict(contract.get("terminal_overlay")).get("terminal") is not True
        ]
        has_consuming_option = any(
            _text(c.get("action_type")) == "select_overlay_option"
            and (
                _as_dict(c.get("navigation_effect")).get("cross_page")
                or bool(_as_list(c.get("state_writes")))
            )
            for c in overlays
        )
        if (
            nonterminal_open_overlays
            and "close_overlay" not in types
            and "confirm_overlay" not in types
            and not has_consuming_option
        ):
            issues.append({
                "code": "overlay_lifecycle_missing",
                "severity": "blocking",
                "action_id": nonterminal_open_overlays[0].get("action_id", ""),
                "page_filename": page,
                "message": "overlay has open action but no close/confirm action",
            })

    autocomplete_result: Dict[str, Any] = {"autocomplete_contracts_by_page_id": {}, "issues": [], "summary": {}}
    initial_control_result: Dict[str, Any] = {"initial_control_state_contracts_by_page_id": {}, "issues": [], "summary": {}}
    initial_dom_result: Dict[str, Any] = {"initial_dom_contracts_by_page_id": {}, "issues": [], "summary": {}}
    global_txn_result: Dict[str, Any] = {"transactions": [], "issues": [], "summary": {}}

    if isinstance(page_environment_specs, dict) and page_environment_specs:
        from .autocomplete_contracts import compile_autocomplete_contracts
        from .initial_control_state_contracts import compile_initial_control_state_contracts
        from .initial_dom_contracts import compile_initial_dom_contracts
        from .global_runtime_transactions import compile_global_runtime_transactions

        autocomplete_result = compile_autocomplete_contracts(
            page_environment_specs, typed_action_contracts=typed_action_contracts,
        )
        initial_control_result = compile_initial_control_state_contracts(
            page_environment_specs,
        )
        initial_dom_result = compile_initial_dom_contracts(page_environment_specs)
        global_txn_result = compile_global_runtime_transactions(page_environment_specs)
        issues.extend(autocomplete_result.get("issues", []))
        issues.extend(initial_control_result.get("issues", []))
        issues.extend(initial_dom_result.get("issues", []))
        issues.extend(global_txn_result.get("issues", []))

    ac_summary = _as_dict(autocomplete_result.get("summary"))
    ic_summary = _as_dict(initial_control_result.get("summary"))
    dom_summary = _as_dict(initial_dom_result.get("summary"))
    txn_summary = _as_dict(global_txn_result.get("summary"))

    return {
        "format": "domain_contracts",
        "schema_revision": 3,
        "filter_contracts_by_page_id": dict(filter_contracts_by_page),
        "overlay_contracts_by_page_id": dict(overlay_contracts_by_page),
        "terminal_overlay_contracts_by_page_id": {
            page: [
                contract
                for contract in contracts
                if _as_dict(contract.get("terminal_overlay")).get("terminal") is True
            ]
            for page, contracts in overlay_contracts_by_page.items()
            if any(
                _as_dict(contract.get("terminal_overlay")).get("terminal") is True
                for contract in contracts
            )
        },
        "form_contracts_by_page_id": dict(form_contracts_by_page),
        "record_contracts_by_page_id": dict(record_contracts_by_page),
        "collection_projection_contracts": dict(collection_contracts_by_page),
        "cross_page_transaction_contracts": cross_page_transactions,
        "autocomplete_contracts_by_page_id": autocomplete_result.get("autocomplete_contracts_by_page_id", {}),
        "initial_control_state_contracts_by_page_id": initial_control_result.get("initial_control_state_contracts_by_page_id", {}),
        "initial_dom_contracts_by_page_id": initial_dom_result.get("initial_dom_contracts_by_page_id", {}),
        "global_runtime_transactions": global_txn_result.get("transactions", []),
        "issues": issues,
        "summary": {
            "filter_page_count": len(filter_contracts_by_page),
            "overlay_page_count": len(overlay_contracts_by_page),
            "terminal_overlay_count": sum(
                1
                for contracts in overlay_contracts_by_page.values()
                for contract in contracts
                if _as_dict(contract.get("terminal_overlay")).get("terminal") is True
            ),
            "form_page_count": len(form_contracts_by_page),
            "record_page_count": len(record_contracts_by_page),
            "collection_page_count": len(collection_contracts_by_page),
            "cross_page_transaction_count": len(cross_page_transactions),
            "autocomplete_page_count": int(ac_summary.get("autocomplete_page_count", 0) or 0),
            "autocomplete_candidate_count": int(ac_summary.get("autocomplete_candidate_count", 0) or 0),
            "initial_control_page_count": int(ic_summary.get("initial_control_page_count", 0) or 0),
            "initial_control_count": int(ic_summary.get("initial_control_count", 0) or 0),
            "initial_dom_page_count": int(dom_summary.get("page_count", 0) or 0),
            "initial_dom_obligation_count": int(dom_summary.get("obligation_count", 0) or 0),
            "global_txn_count": int(txn_summary.get("transaction_count", 0) or 0),
            "global_txn_closed_count": int(txn_summary.get("closed_count", 0) or 0),
            "issue_count": len(issues),
        },
    }


_CATEGORY_TO_SPEC_CLASSIFICATION: Dict[str, str] = {
    "missing_filter_sort_ui": "pipeline_defect",
    "missing_modal_overlay": "pipeline_defect",
    "missing_form_interaction": "pipeline_defect",
    "missing_form_input": "pipeline_defect",
    "missing_interactive_widget": "pipeline_defect",
    "missing_coupon_selector": "pipeline_defect",
    "missing_cta_button": "spec_fixloop_candidate",
    "content_data_mismatch": "spec_fixloop_candidate",
    "initial_state_wrong": "spec_fixloop_candidate",
    "missing_selection_state": "pipeline_defect",
    "navigation_wrong_target": "pipeline_defect",
    "missing_navigation_link": "pipeline_defect",
    "missing_back_button": "pipeline_defect",
}


def classify_trace_review_issues(
    trace_review_report: Mapping[str, Any],
) -> Dict[str, Any]:

    report = trace_review_report if isinstance(trace_review_report, dict) else {}
    categories = _as_dict(report.get("failure_categories"))
    classified: List[Dict[str, Any]] = []

    for category, info in categories.items():
        if not isinstance(info, dict):
            continue
        count = int(info.get("count", 0) or 0)
        failures = _as_list(info.get("failures"))
        spec_class = _CATEGORY_TO_SPEC_CLASSIFICATION.get(category, "evidence_blocked")
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            classified.append({
                "trace_id": _text(failure.get("trace_id")),
                "step_index": failure.get("step_index"),
                "page_filename": _text(failure.get("filename")),
                "target": _text(failure.get("target")),
                "category": category,
                "spec_classification": spec_class,
            })

    by_class: Dict[str, int] = defaultdict(int)
    for item in classified:
        by_class[item["spec_classification"]] += 1

    return {
        "format": "trace_review_spec_classification",
        "schema_revision": 1,
        "classified_issues": classified,
        "summary": {
            "total": len(classified),
            "by_spec_classification": dict(sorted(by_class.items())),
        },
    }


def build_materialization_report(
    pagegen_issues_by_page: Mapping[str, List[Dict[str, Any]]],
    typed_action_contracts: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    typed = _as_dict(
        typed_action_contracts.get("typed_action_contracts_by_action_id")
        if isinstance(typed_action_contracts, dict) else {}
    )
    all_issues: List[Dict[str, Any]] = []
    by_class: Dict[str, int] = defaultdict(int)

    pages_input = pagegen_issues_by_page if isinstance(pagegen_issues_by_page, dict) else {}
    for page_filename, issues in pages_input.items():
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            code = _text(issue.get("code"))
            has_contract = bool(typed.get(issue.get("action_id") or ""))
            if "missing_contract" in code or "missing_ghostweb" in code:
                failure_class = "spec_defect"
            elif has_contract:
                failure_class = "pagegen_materialization_defect"
            else:
                failure_class = "pagegen_materialization_defect"
            enriched = {
                **issue,
                "page_filename": page_filename,
                "failure_class": failure_class,
            }
            all_issues.append(enriched)
            by_class[failure_class] += 1

    return {
        "format": "pagegen_contract_materialization_report",
        "schema_revision": 1,
        "issues": all_issues,
        "summary": {
            "total_issues": len(all_issues),
            "page_count": len(pages_input),
            "by_failure_class": dict(sorted(by_class.items())),
        },
    }


def validate_autocomplete_materialization(
    page_filename: str,
    autocomplete_contracts: List[Dict[str, Any]],
    rendered_candidates: List[str] | None = None,
    rendered_candidate_count: int | None = None,
    rendered_categories: List[str] | None = None,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not autocomplete_contracts:
        issues.append({
            "code": "autocomplete_contract_missing_for_validation",
            "severity": "spec_defect",
            "page_filename": page_filename,
            "message": "no autocomplete contract available for materialization validation — return to spec/compiler",
        })
        return issues

    for contract in autocomplete_contracts:
        if not isinstance(contract, dict):
            continue
        expected_count = int(contract.get("expected_candidate_count", 0) or 0)
        min_visible = int(contract.get("min_visible_count", 0) or 0)
        contract_rows = _as_list(contract.get("candidate_rows"))
        required_titles = [_text(r.get("title")) for r in contract_rows if isinstance(r, dict) and _text(r.get("title"))]
        required_categories = contract.get("candidate_categories", {})

        if rendered_candidate_count is not None and rendered_candidate_count < min_visible:
            issues.append({
                "code": "autocomplete_min_visible_not_met",
                "severity": "pagegen_materialization_defect",
                "page_filename": page_filename,
                "action_id": contract.get("input_action_id", ""),
                "message": (
                    f"rendered {rendered_candidate_count} candidates but "
                    f"min_visible_count is {min_visible}"
                ),
            })

        if rendered_candidates is not None and required_titles:
            missing_titles = [t for t in required_titles if t not in rendered_candidates]
            if missing_titles:
                issues.append({
                    "code": "autocomplete_candidate_rows_not_rendered",
                    "severity": "pagegen_materialization_defect",
                    "page_filename": page_filename,
                    "action_id": contract.get("input_action_id", ""),
                    "message": f"missing {len(missing_titles)} required candidate titles in rendered HTML",
                    "missing_count": len(missing_titles),
                })

        if rendered_categories is not None and required_categories:
            missing_cats = [c for c in required_categories if c not in rendered_categories]
            if missing_cats:
                issues.append({
                    "code": "autocomplete_category_not_rendered",
                    "severity": "pagegen_materialization_defect",
                    "page_filename": page_filename,
                    "action_id": contract.get("input_action_id", ""),
                    "message": f"missing {len(missing_cats)} required categories in rendered HTML",
                })

    return issues


def validate_initial_control_materialization(
    page_filename: str,
    initial_control_contracts: List[Dict[str, Any]],
    rendered_controls: Dict[str, Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not initial_control_contracts:
        issues.append({
            "code": "initial_control_contract_missing_for_validation",
            "severity": "spec_defect",
            "page_filename": page_filename,
            "message": "no initial control state contract available — return to spec/compiler",
        })
        return issues

    if rendered_controls is None:
        return issues

    for contract in initial_control_contracts:
        if not isinstance(contract, dict):
            continue
        control_id = _text(contract.get("control_id"))
        rendered = rendered_controls.get(control_id, {})
        if not rendered:
            if contract.get("expected_visible", True):
                issues.append({
                    "code": "initial_control_not_found_in_dom",
                    "severity": "pagegen_materialization_defect",
                    "page_filename": page_filename,
                    "control_id": control_id,
                    "message": f"control '{control_id}' expected visible but not found in DOM",
                })
            continue

        expected_checked = contract.get("expected_checked")
        if expected_checked is not None and rendered.get("checked") != expected_checked:
            issues.append({
                "code": "initial_control_checked_wrong",
                "severity": "pagegen_materialization_defect",
                "page_filename": page_filename,
                "control_id": control_id,
                "message": (
                    f"control '{control_id}' expected_checked={expected_checked} "
                    f"but rendered checked={rendered.get('checked')}"
                ),
            })

        expected_value = contract.get("expected_value")
        if expected_value is not None and str(rendered.get("value", "")) != str(expected_value):
            issues.append({
                "code": "initial_control_value_wrong",
                "severity": "pagegen_materialization_defect",
                "page_filename": page_filename,
                "control_id": control_id,
                "message": (
                    f"control '{control_id}' expected_value={expected_value} "
                    f"but rendered value={rendered.get('value')}"
                ),
            })

    return issues


def build_domain_contract_materialization_report(
    domain_contracts: Mapping[str, Any],
    *,
    rendered_autocomplete_by_page: Mapping[str, Mapping[str, Any]] | None = None,
    rendered_initial_controls_by_page: Mapping[str, Mapping[str, Dict[str, Any]]] | None = None,
) -> Dict[str, Any]:

    domain = domain_contracts if isinstance(domain_contracts, dict) else {}
    rendered_autocomplete = rendered_autocomplete_by_page if isinstance(rendered_autocomplete_by_page, dict) else {}
    rendered_initial_controls = rendered_initial_controls_by_page if isinstance(rendered_initial_controls_by_page, dict) else {}
    issues: List[Dict[str, Any]] = []
    by_failure_class: Dict[str, int] = defaultdict(int)

    autocomplete_by_page = _as_dict(domain.get("autocomplete_contracts_by_page_id"))
    for page_filename, contracts in autocomplete_by_page.items():
        sample = _as_dict(rendered_autocomplete.get(page_filename))
        if not sample:
            continue
        issues.extend(
            validate_autocomplete_materialization(
                str(page_filename),
                [item for item in _as_list(contracts) if isinstance(item, dict)],
                rendered_candidates=[
                    _text(item) for item in _as_list(sample.get("rendered_candidates"))
                    if _text(item)
                ],
                rendered_candidate_count=sample.get("rendered_candidate_count"),
                rendered_categories=[
                    _text(item) for item in _as_list(sample.get("rendered_categories"))
                    if _text(item)
                ],
            )
        )
    for page_filename, sample in rendered_autocomplete.items():
        if page_filename in autocomplete_by_page:
            continue
        issues.extend(validate_autocomplete_materialization(str(page_filename), []))

    initial_controls_by_page = _as_dict(domain.get("initial_control_state_contracts_by_page_id"))
    for page_filename, contracts in initial_controls_by_page.items():
        sample = rendered_initial_controls.get(page_filename)
        if sample is None:
            continue
        issues.extend(
            validate_initial_control_materialization(
                str(page_filename),
                [item for item in _as_list(contracts) if isinstance(item, dict)],
                rendered_controls=dict(sample),
            )
        )
    for page_filename, sample in rendered_initial_controls.items():
        if page_filename in initial_controls_by_page:
            continue
        issues.extend(validate_initial_control_materialization(str(page_filename), [], rendered_controls=dict(sample)))

    _VALID_FAILURE_CLASSES = frozenset({
        "spec_defect",
        "pagegen_materialization_defect",
        "validator_semantics_gap",
        "runtime_state_defect",
        "evidence_blocked",
    })
    for issue in issues:
        severity = _text(issue.get("severity"))
        code = _text(issue.get("code"))
        if severity in _VALID_FAILURE_CLASSES:
            failure_class = severity
            classification_reason = f"severity field directly maps to {failure_class}"
        elif "contract_missing" in code or "evidence_missing" in code:
            failure_class = "spec_defect"
            classification_reason = f"issue code '{code}' indicates upstream contract gap"
        else:
            failure_class = "pagegen_materialization_defect"
            classification_reason = "contract exists but HTML does not conform"
        issue["failure_class"] = failure_class
        issue["classification_reason"] = classification_reason
        by_failure_class[failure_class] += 1

    return {
        "format": "pagegen_contract_materialization_report",
        "schema_revision": 2,
        "issues": issues,
        "summary": {
            "total_issues": len(issues),
            "by_failure_class": dict(sorted(by_failure_class.items())),
        },
    }


def check_stale_artifact_guard(
    result_dir: str,
    required_artifacts: List[str] | None = None,
) -> Dict[str, Any]:
    import os

    if required_artifacts is None:
        required_artifacts = [
            "semantic_api/07_spec_executability_eval/spec_executability_eval.json",
            "semantic_api/08_spec_compiled_execution_views/runtime_binding_manifest.json",
            "semantic_api/08_spec_compiled_execution_views/route_execution_contracts.json",
        ]

    missing: List[str] = []
    present: List[str] = []
    for artifact in required_artifacts:
        path = os.path.join(result_dir, artifact)
        if os.path.isfile(path):
            present.append(artifact)
        else:
            missing.append(artifact)

    is_stale = len(missing) > 0
    return {
        "result_dir": result_dir,
        "is_stale": is_stale,
        "missing_artifacts": missing,
        "present_artifacts": present,
        "guard_status": "blocked" if is_stale else "passed",
        "message": (
            f"Result dir is stale: {len(missing)} required EnvContract artifacts missing. "
            "Do not use stale HTML conclusions to determine new pipeline status."
            if is_stale else "Result dir has all required artifacts."
        ),
    }
