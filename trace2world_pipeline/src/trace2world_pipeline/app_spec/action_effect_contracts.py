
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .global_execution_schema import normalize_field_path


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _trace_step(action_id: str) -> tuple[str, str]:
    if ":step" not in action_id:
        return "", ""
    trace_id, raw_step = action_id.split(":step", 1)
    return trace_id, raw_step


ROUTE_BLOCKING_SKIP_REASONS = frozenset({
    "phone_home",
    "system_launcher",
    "app_switcher",
    "os_permission_or_settings",
    "non_target_app",
    "non_target_app_state",
    "external_system",
    "system_chrome",
    "unassigned_step",
})

ACTION_EFFECT_EXCLUDED_SKIP_REASONS = ROUTE_BLOCKING_SKIP_REASONS | frozenset({
    "loading_wait",
    "transient_wait",
    "noop_retry",
    "transient_launch_gate",
    "manual_takeover_noop",
    "failed_noop",
    "dropped_by_preprocess",
    "phone_keyboard_state",
    "runtime_state_bridge",
    "page_assignment_audit_skip",
})

DOM_HOST_OPERATIONS = frozenset({
    "click",
    "tap",
    "double_click",
    "dblclick",
    "submit",
    "press",
    "fill",
    "type",
    "input",
    "change",
    "select",
})

GESTURE_ONLY_OPERATIONS = frozenset({"swipe", "scroll", "drag", "wheel", "wheel_drag", "pan"})

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


def _skipped_steps_by_action_id(skipped_steps: Optional[Iterable[Mapping[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in skipped_steps or []:
        row = _as_dict(item)
        trace_id = _text(row.get("trace_id"))
        step_index = row.get("step_index")
        if not trace_id and row.get("segment_id"):
            segment_id = _text(row.get("segment_id"))
            if ":s_" in segment_id:
                trace_id, raw_step = segment_id.rsplit(":s_", 1)
                step_index = raw_step
        try:
            step_no = int(step_index)
        except (TypeError, ValueError):
            continue
        if not trace_id:
            continue
        result[f"{trace_id}:step{step_no}"] = row
    return result


def _skip_reason_blocks_target_runtime(reason: str) -> bool:
    return _text(reason).lower() in ROUTE_BLOCKING_SKIP_REASONS


def _skip_reason_excludes_action_effect(reason: str) -> bool:
    return _text(reason).lower() in ACTION_EFFECT_EXCLUDED_SKIP_REASONS


def _is_startup_or_splash_gate(step: Mapping[str, Any]) -> bool:
    source = _source(step).lower()
    target = _target(step).lower()
    intent = _operation_intent(step)
    reducer = _text(step.get("reducer")).lower()
    if any(token in source for token in ("splash", "launch", "startup", "ad")) and any(
        token in intent for token in ("跳过", "关闭", "开屏", "广告", "skip", "close")
    ):
        return True
    if reducer in {"close_overlay", "dismiss_overlay"} and target in {"home.html", "index.html"} and any(
        token in intent for token in ("开屏", "广告", "跳过", "关闭", "skip", "close")
    ):
        return True
    return False


def _step_number(action_id: str) -> int:
    _, raw = _trace_step(action_id)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _transaction(step: Mapping[str, Any]) -> Dict[str, Any]:
    return _as_dict(step.get("transaction"))


def _operation_intent(step: Mapping[str, Any]) -> str:
    txn = _transaction(step)
    return _text(
        txn.get("operation_intent")
        or step.get("operation_intent")
        or step.get("action")
        or step.get("target")
        or step.get("command")
    )


def _state_commit(step: Mapping[str, Any]) -> Dict[str, Any]:
    txn = _transaction(step)
    return _as_dict(txn.get("state_commit") or step.get("state_commit"))


def _expected_state_diff(step: Mapping[str, Any]) -> Dict[str, Any]:
    state_commit = _state_commit(step)
    diff = _as_dict(
        state_commit.get("expected_state_diff")
        or _as_dict(step.get("assertions")).get("expected_state_diff")
        or step.get("expected_state_diff")
    )
    return dict(diff)


def _same_page_mutation_semantic_diff(step: Mapping[str, Any], state_refs: Iterable[str]) -> Dict[str, Any]:
    text = _operation_intent(step)
    refs = [_text(ref) for ref in state_refs if _text(ref)]
    reducer = _text(step.get("reducer")).lower()
    if refs and reducer in {"open_action_sheet", "open_overlay", "open_runtime_overlay"}:
        return {f"{refs[0]}.visible": True}
    if refs and reducer in {"close_overlay", "dismiss_overlay"}:
        return {f"{refs[0]}.visible": False}
    if refs and reducer == "confirm_overlay":
        return {f"{refs[0]}.confirmed": True}
    if not text:
        return {}
    lowered = text.lower()
    refs_text = " ".join(_text(ref).lower() for ref in state_refs)
    value = _clean_filter_value(_extract_quoted_value(text))
    if not value:
        return {}

    field = ""
    if any(token in text for token in ("出发时间", "发车时间")) or "departure_time" in refs_text:
        field = "departure_time_filter"
    elif "到达时间" in text or "arrival_time" in refs_text:
        field = "arrival_time_filter"
    elif any(token in text for token in ("出发车站", "出发站")):
        field = "departure_station_filter"
    elif any(token in text for token in ("到达车站", "到达站")):
        field = "arrival_station_filter"
    elif any(token in text for token in ("车次类型", "高铁", "动车", "普通车")) or "train_type" in refs_text:
        field = "train_type_filter"
    elif any(token in text for token in ("席别", "硬卧", "软卧", "商务座", "一等座", "二等座")) or "seat" in refs_text:
        field = "seat_type_filter"
    elif any(token in text for token in ("乘客类型", "旅客类型", "学生", "成人", "儿童")) and "passenger" in refs_text:
        field = "identityForm.personType"
    elif any(token in text for token in ("排序", "耗时最短", "发时最早")) or "sort" in lowered:
        field = "sort_order"
    elif "action_sheet" in refs_text:
        field = "selected_option"
    elif "filter" in refs_text or "time_picker" in refs_text:
        field = "selected_filter"

    if not field:
        return {}
    return {field: value}


def _save_return_semantic_diff(step: Mapping[str, Any], state_refs: Iterable[str]) -> Dict[str, Any]:
    refs = [_text(ref) for ref in state_refs if _text(ref)]
    if not refs:
        return {}
    text = _operation_intent(step)
    value = _clean_filter_value(_extract_quoted_value(text))
    if not value:
        return {}
    refs_text = " ".join(ref.lower() for ref in refs)
    if "calendar" in refs_text or "schedule" in refs_text:
        return {"selected_record_status": value}
    if "passenger" in refs_text and any(token in text for token in ("确认", "完成", "保存")):
        return {"passenger_selection_status": value}
    return {"committed_option": value}


def _clean_filter_value(value: str) -> str:
    text = _clean_entity_value(value)
    text = text.replace("－", "-").replace("—", "-").replace("–", "-")
    text = re.sub(r"\s*-\s*", "-", text)
    return text.strip()


def _state_refs(step: Mapping[str, Any]) -> List[str]:
    commit = _state_commit(step)
    refs = [_text(ref) for ref in _as_list(commit.get("state_refs")) if _text(ref)]
    if refs:
        return refs
    refs = []
    for key in _expected_state_diff(step):
        if "." in key:
            refs.append(key.rsplit(".", 1)[0])
        else:
            refs.append(key)
    return sorted(dict.fromkeys(refs))


def _source(step: Mapping[str, Any]) -> str:
    txn = _transaction(step)
    nav = _as_dict(txn.get("navigation"))
    return _text(step.get("source_filename") or nav.get("source_filename"))


def _target(step: Mapping[str, Any]) -> str:
    txn = _transaction(step)
    nav = _as_dict(txn.get("navigation"))
    return _text(step.get("target_filename") or nav.get("target_filename") or nav.get("expected_filename"))


def _next_step_source(steps: List[Dict[str, Any]], index: int) -> str:
    if index + 1 >= len(steps):
        return ""
    return _source(steps[index + 1])


def _next_step_target(steps: List[Dict[str, Any]], index: int) -> str:
    if index + 1 >= len(steps):
        return ""
    return _target(steps[index + 1])


def _trace_step_sort_key(step: Mapping[str, Any]) -> int:
    return _step_number(_text(step.get("action_id")))


def _closed_target_from_next_step(
    effect_kind: str,
    source: str,
    target: str,
    next_source: str,
    next_target: str = "",
) -> str:
    if effect_kind not in {"route", "gesture_route", "stateful_submit", "select_result_return"}:
        return target
    if not source or not target or target != source:
        return target
    if not next_source or next_source == source:
        if next_target and next_target != source:
            return next_target
        return target
    return next_source


def _page_boundary_authority(
    step: Mapping[str, Any],
    action_id: str,
    page_boundary_authority: Mapping[str, Any],
) -> Dict[str, Any]:

    embedded = _as_dict(
        step.get("boundary_contract") or step.get("page_boundary_authority")
    )
    if embedded:
        return dict(embedded)
    by_action = _as_dict(page_boundary_authority.get("actions_by_id"))
    return dict(_as_dict(by_action.get(action_id)))


def _runtime_projection_authority(authority: Mapping[str, Any]) -> bool:
    decision = _text(
        authority.get("boundary_decision")
        or authority.get("transition_kind")
        or authority.get("disposition")
    ).lower()
    return decision in {
        "runtime_projection",
        "same_general_page_runtime_projection",
        "same_page_projection",
    }


def _canonical_projection_filename(authority: Mapping[str, Any]) -> str:
    return _text(
        authority.get("canonical_runtime_filename")
        or authority.get("canonical_filename")
        or authority.get("canonical_materialized_filename")
        or authority.get("canonical_materialized_page")
    )


def _authority_state_refs(authority: Mapping[str, Any]) -> List[str]:
    refs = [_text(ref) for ref in _as_list(authority.get("state_refs")) if _text(ref)]
    state_ref = _text(authority.get("state_ref"))
    if state_ref and state_ref not in refs:
        refs.append(state_ref)
    return refs


def _validate_runtime_projection_authority(
    action_id: str,
    authority: Mapping[str, Any],
) -> None:
    canonical_filename = _canonical_projection_filename(authority)
    source_general = _text(
        authority.get("source_general_page_id")
        or authority.get("general_page_id")
    )
    target_general = _text(
        authority.get("target_general_page_id")
        or authority.get("general_page_id")
    )
    state_refs = _authority_state_refs(authority)
    expected_state_diff = _as_dict(authority.get("expected_state_diff"))
    if not canonical_filename or not source_general or not target_general:
        raise ValueError(
            f"{action_id}: runtime_projection page-boundary authority requires "
            "canonical_filename and general_page_id "
            "(canonical_runtime_filename and source/target general page identity)"
        )
    if source_general != target_general:
        raise ValueError(
            f"{action_id}: runtime_projection page-boundary authority crosses general page identity"
        )
    if not state_refs or not expected_state_diff:
        raise ValueError(
            f"{action_id}: runtime_projection page-boundary authority requires "
            "state_refs and expected_state_diff"
        )


def _has_authoritative_same_page_navigation(step: Mapping[str, Any]) -> bool:
    nav = _as_dict(_transaction(step).get("navigation"))
    return nav.get("same_page") is True


def _extract_quoted_value(text: str) -> str:
    for pattern in (r"“([^”]+)”", r'"([^"]+)"', r"'([^']+)'"):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    if "中的" in text:
        return text.rsplit("中的", 1)[-1].strip(" ：:，,。")
    return text.strip()


def _extract_quoted_values(text: str) -> List[str]:
    values: List[str] = []
    for pattern in (r"“([^”]+)”", r'"([^"]+)"', r"'([^']+)'"):
        values.extend(match.strip() for match in re.findall(pattern, text) if match.strip())
    return _dedupe(values)


def _clean_entity_value(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(点击|选择|搜索结果|第一项|城市|车站)\s*", "", value)
    return value.strip(" ：:，,。")


def _is_more_results_intent(text: str) -> bool:
    return any(token in text for token in ("查看更多", "更多")) and any(
        token in text for token in ("火车站", "车站", "候选", "列表", "结果")
    )


def _is_pending_confirm_selection_intent(text: str) -> bool:
    return any(token in text for token in ("选择框", "勾选", "复选框", "checkbox"))


def _is_picker_input_activation_intent(text: str) -> bool:
    return any(token in text for token in ("输入框", "搜索框", "搜索输入框")) and any(
        token in text for token in ("顶部", "车站", "搜索")
    )


def _is_edit_surface_route(step: Mapping[str, Any], next_source: str, next_target: str = "") -> bool:
    source = _source(step)
    target = _target(step)
    operation = _text(step.get("operation")).lower()
    if operation not in {"click", "tap", "press", "double_click", "dblclick"}:
        return False
    if not source or not target or source != target:
        return False
    route_target = next_source if next_source and next_source != source else next_target
    if not route_target or route_target == source:
        return False
    next_lower = route_target.lower()
    if not any(token in next_lower for token in ("utility", "edit", "form", "input")):
        return False
    text = _operation_intent(step)
    return any(
        token in text
        for token in (
            "输入栏",
            "输入框",
            "字段",
            "手机号",
            "手机号码",
            "证件号码",
            "编辑",
            "修改",
            "填写",
            "表单",
        )
    )


def _field_role_from_text(text: str) -> str:
    lowered = text.lower()
    if any(token in text for token in ("日期", "日历", "时间", "提醒")) or "date" in lowered or "time" in lowered:
        return "date_or_time"
    if any(token in text for token in ("出发", "左侧", "起点", "始发")) or "origin" in lowered or "from" in lowered:
        return "origin"
    if any(token in text for token in ("目的", "到达", "右侧", "终点")) or "destination" in lowered or "to" in lowered:
        return "destination"
    return "selection"


def _caller_owned_state_ref(field_role: str, state_refs: Iterable[str]) -> str:
    role = _text(field_role)
    candidates = [_text(ref) for ref in state_refs if _text(ref)]

    def usable(ref: str) -> bool:
        lowered = ref.lower()
        if any(token in lowered for token in ("overlay", "selector", "runtime", "draft::")):
            return False
        if role and role not in lowered:
            return False
        return True

    for ref in candidates:
        if usable(ref):
            return ref
    return f"state::selection.{role}" if role else ""


def _is_terminal_step(step: Mapping[str, Any]) -> bool:
    operation = _text(step.get("operation")).lower()
    reducer = _text(step.get("reducer")).lower()
    return operation in {"terminate", "terminal", "task_done", "agent_execute"} or reducer in {
        "terminate",
        "task_done",
        "terminal",
        "agent_execute",
    }


def _is_non_interactive_agent_step(step: Mapping[str, Any]) -> bool:
    operation = _text(step.get("operation")).lower()
    reducer = _text(step.get("reducer")).lower()
    return operation == "agent_execute" or reducer == "agent_execute"


def _is_save_return(step: Mapping[str, Any]) -> bool:
    text = _operation_intent(step).lower()
    operation = _text(step.get("operation")).lower()
    source = _source(step)
    target = _target(step)
    has_save_text = any(token in text for token in ("完成", "保存", "确定", "确认", "done", "save", "ok"))
    executable_op = operation in {"click", "tap", "double_click", "dblclick", "submit", "press"}
    return executable_op and has_save_text and bool(source and target and source != target)


def _is_tab_or_mode_route(step: Mapping[str, Any]) -> bool:
    text = _operation_intent(step) or _text(_as_dict(step.get("source_evidence")).get("operation_intent"))
    target = (_target(step) or _text(step.get("expected_target_filename"))).lower()
    if not text or not target:
        return False
    if not any(token in text for token in ("标签", "切换", "tab")):
        return False
    if not any(token in text for token in ("汽车票", "飞机票", "火车票", "车票", "业务", "模式")):
        return False
    return not any(token in target for token in ("result", "results", "list", "search"))


def _is_stateful_submit(step: Mapping[str, Any], state_reads: Optional[Iterable[str]] = None) -> bool:
    source = _source(step)
    target = _target(step)
    text = _operation_intent(step).lower()
    reducer = _text(step.get("reducer")).lower()
    if not source or not target or source == target:
        return False
    if _is_tab_or_mode_route(step):
        return False
    if _activation_target(step) and any(
        token in _operation_intent(step)
        for token in ("出发", "到达", "左侧", "右侧", "日期", "日历", "时间")
    ):
        return False
    if reducer in {"submit_form", "form_commit", "terminal_submit", "terminal_commit"}:
        return True
    if any(token in text for token in ("查询", "搜索", "提交", "下单", "确认订单", "submit", "search")):
        return bool([ref for ref in (state_reads or []) if _text(ref)])
    return False


def _is_select_result_return(
    step: Mapping[str, Any],
    previous_activation: Optional[Mapping[str, Any]],
    next_source: str = "",
    next_target: str = "",
) -> bool:
    source = _source(step)
    target = _target(step)
    reducer = _text(step.get("reducer")).lower()
    text = _operation_intent(step)
    closed_target = target
    if source and target and target == source and next_source and next_source != source:
        closed_target = next_source
    elif source and target and target == source and next_target and next_target != source:
        closed_target = next_target
    if not source or not closed_target or source == closed_target:
        return False
    if not previous_activation:
        return False
    if (
        _is_more_results_intent(text)
        or _is_pending_confirm_selection_intent(text)
        or _is_picker_input_activation_intent(text)
    ):
        return False
    if reducer in {"commit_query", "select_record", "selected_record_hydration", "select_suggestion"}:
        return True
    return any(token in text for token in ("搜索结果", "选择", "第一项", "候选", "结果中的", "列表中的", "城市", "车站", "火车站"))


QUERY_PROJECTION_ACTION_TYPES = frozenset({
    "query_draft_updated",
    "query_autocomplete_project",
})

NON_QUERY_TEXT_ACTION_TYPES = frozenset({
    "form_fill",
    "form_focus",
    "form_commit",
})

QUERY_PROJECTION_REDUCERS = frozenset({
    "project_suggestions",
    "type_search",
    "update_query_draft",
})

NON_QUERY_TEXT_REDUCERS = frozenset({
    "fill_form",
    "form_focus",
    "submit_form",
    "form_commit",
    "terminal_submit",
    "terminal_commit",
})


def _typed_action(
    action_id: str,
    typed_action_contracts: Mapping[str, Any],
) -> Dict[str, Any]:
    return _as_dict(
        _as_dict(typed_action_contracts.get("typed_action_contracts_by_action_id")).get(action_id)
    )


def _query_projection_authority(
    step: Mapping[str, Any],
    action_id: str,
    typed_action_contracts: Mapping[str, Any],
) -> Optional[bool]:

    typed = _typed_action(action_id, typed_action_contracts)
    action_type = _text(typed.get("action_type")).lower()
    if action_type in QUERY_PROJECTION_ACTION_TYPES:
        return True
    if action_type in NON_QUERY_TEXT_ACTION_TYPES:
        return False

    reducer = _text(step.get("reducer")).lower()
    typed_reducer = _text(typed.get("reducer")).lower()
    reducers = {value for value in (typed_reducer, reducer) if value}
    if reducers & QUERY_PROJECTION_REDUCERS:
        return True
    if reducers & NON_QUERY_TEXT_REDUCERS:
        return False
    return None


def _is_query_projection(
    step: Mapping[str, Any],
    action_id: str,
    typed_action_contracts: Mapping[str, Any],
) -> bool:
    authority = _query_projection_authority(step, action_id, typed_action_contracts)
    if authority is not None:
        return authority

    operation = _text(step.get("operation")).lower()
    source = _source(step)
    target = _target(step)


    return operation in {"fill", "type", "input"} and (not target or target == source)


def _is_same_page_mutation(
    step: Mapping[str, Any],
    action_id: str = "",
    typed_action_contracts: Optional[Mapping[str, Any]] = None,
) -> bool:
    source = _source(step)
    target = _target(step)
    reducer = _text(step.get("reducer")).lower()
    if source and target and source != target:
        return False
    typed = _typed_action(action_id, typed_action_contracts or {})
    action_type = _text(typed.get("action_type")).lower()
    typed_reducer = _text(typed.get("reducer")).lower()
    if action_type in NON_QUERY_TEXT_ACTION_TYPES or typed_reducer in NON_QUERY_TEXT_REDUCERS:
        return True
    return reducer in {
        *NON_QUERY_TEXT_REDUCERS,
        "open_action_sheet",
        "open_overlay",
        "open_runtime_overlay",
        "close_overlay",
        "confirm_overlay",
        "select_filter_option",
        "apply_filter",
        "switch_tab",
        "sort_select",
        "cart_mutation",
        "add_to_cart",
        "remove_from_cart",
        "update_cart_quantity",
        "activate_inline_state",
    }


def _is_viewport_scroll(step: Mapping[str, Any]) -> bool:
    operation = _text(step.get("operation")).strip().lower()
    reducer = _text(step.get("reducer")).strip().lower()
    source = _source(step)
    target = _target(step)
    return (
        (operation in GESTURE_ONLY_OPERATIONS or reducer == "scroll_viewport")
        and bool(source)
        and (not target or target == source)
        and not _state_refs(step)
        and not _expected_state_diff(step)
    )


def _is_gesture_route(step: Mapping[str, Any]) -> bool:
    operation = _text(step.get("operation")).lower()
    source = _source(step)
    target = _target(step)
    text = _operation_intent(step)
    return (
        operation in {"drag", "scroll", "swipe"}
        and bool(source and target and source != target)
        and any(token in text for token in ("滑", "横滑", "翻页", "更多", "全部", "carousel", "launcher"))
    )


def _safe_ref_token(value: Any) -> str:
    text = _text(value).lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text)
    text = text.strip("._-")
    return text or "unknown"


def _gesture_direction(text: str) -> str:
    lowered = text.lower()
    if any(token in text for token in ("右向左", "向左", "左滑", "往左")) or any(
        token in lowered for token in ("left", "next", "forward")
    ):
        return "forward"
    if any(token in text for token in ("左向右", "向右", "右滑", "往右")) or any(
        token in lowered for token in ("right", "previous", "back")
    ):
        return "backward"
    return "forward"


def _gesture_component_role(text: str, next_step: Mapping[str, Any]) -> str:
    combined = f"{text} {_operation_intent(next_step)}".lower()
    if any(token in combined for token in ("分类", "category", "shortcut", "快捷", "频道", "宫格", "入口")):
        return "category_shortcuts"
    if any(token in combined for token in ("carousel", "轮播", "横滑", "横向", "卡片")):
        return "carousel_strip"
    return "gesture_surface"


def _stateful_component_gesture_scope(
    step: Mapping[str, Any],
    next_step: Mapping[str, Any],
) -> Dict[str, Any]:
    operation = _text(step.get("operation")).lower()
    if operation not in GESTURE_ONLY_OPERATIONS:
        return {}
    source = _source(step)
    target = _target(step)
    if source and target and source != target:
        return {}
    text = _operation_intent(step)
    lowered = text.lower()
    if not any(token in text for token in ("滑", "拖", "横", "翻页", "切换")) and not any(
        token in lowered for token in ("swipe", "drag", "pan", "carousel", "scroll")
    ):
        return {}
    component_role = _gesture_component_role(text, next_step)
    if component_role == "gesture_surface" and not any(
        token in lowered for token in ("component", "carousel", "shortcut", "category")
    ):
        return {}
    axis = "horizontal" if any(token in text for token in ("左", "右", "横")) or component_role in {
        "category_shortcuts",
        "carousel_strip",
    } else "vertical"
    direction = _gesture_direction(text)
    reveal_labels = _dedupe(
        list(_extract_quoted_values(_operation_intent(next_step)))
        + list(_unquoted_intent_labels(_operation_intent(next_step)))
    )
    source_token = _safe_ref_token(source)
    component_ref = f"component::{source_token}::{component_role}"
    state_ref = f"state::{source_token}.{component_role}.visible_window"
    expected_state_diff = {
        state_ref: {
            "effect": "advance_visible_window",
            "axis": axis,
            "direction": direction,
            "reveals": reveal_labels,
        }
    }
    return {
        "gesture_scope": "component_visible_window",
        "component_role": component_role,
        "component_ref": component_ref,
        "state_ref": state_ref,
        "axis": axis,
        "direction": direction,
        "reveals": reveal_labels,
        "expected_state_diff": expected_state_diff,
    }


def _state_reads(action_id: str, typed_action_contracts: Mapping[str, Any]) -> List[str]:
    typed = _as_dict(_as_dict(typed_action_contracts.get("typed_action_contracts_by_action_id")).get(action_id))
    return [_text(ref) for ref in _as_list(typed.get("state_reads")) if _text(ref)]


def _typed_state_writes(action_id: str, typed_action_contracts: Mapping[str, Any]) -> List[str]:
    typed = _as_dict(_as_dict(typed_action_contracts.get("typed_action_contracts_by_action_id")).get(action_id))
    return [_text(ref) for ref in _as_list(typed.get("state_writes")) if _text(ref)]


def _typed_expected_state_diff(action_id: str, typed_action_contracts: Mapping[str, Any]) -> Dict[str, Any]:
    typed = _as_dict(_as_dict(typed_action_contracts.get("typed_action_contracts_by_action_id")).get(action_id))
    expected: Dict[str, Any] = {}
    for oracle in _as_list(typed.get("state_oracles")):
        expected.update(_as_dict(_as_dict(oracle).get("diff")))
    return expected


def _commit_draft_state_reads(step: Mapping[str, Any]) -> List[str]:
    state_effect = _as_dict(step.get("state_effect"))
    if _text(state_effect.get("effect")).lower() != "commit_draft":
        return []
    identity_ref = _text(
        _as_dict(_as_dict(step.get("transition_identity_key")).get("state_identity")).get("state_ref")
    )
    return _dedupe([
        _text(state_effect.get("draft_ref")),
        identity_ref,
    ])


def _dedupe(values: Iterable[str]) -> List[str]:
    return sorted(dict.fromkeys(_text(value) for value in values if _text(value)))


def _append_unique(target: List[str], value: Any) -> None:
    text = _text(value)
    if text and text not in target:
        target.append(text)


def _schema_projection_alias_indexes(
    downstream_projection: Mapping[str, Any],
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    exact: Dict[str, Dict[str, Any]] = {}
    normalized: Dict[str, Dict[str, Any]] = {}
    if not isinstance(downstream_projection, Mapping):
        return exact, normalized
    for item in _as_list(downstream_projection.get("ref_aliases")):
        if not isinstance(item, Mapping):
            continue
        ref = _text(item.get("ref"))
        owner = _text(item.get("owner_field_path"))
        if not ref or not owner:
            continue
        row = dict(item)
        exact[ref] = row
        norm = normalize_field_path(ref)
        if norm:
            normalized.setdefault(norm, row)
    for field in _as_list(downstream_projection.get("projection_fields")):
        if not isinstance(field, Mapping):
            continue
        field_path = _text(field.get("field_path"))
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
        norm = normalize_field_path(field_path)
        if norm:
            normalized.setdefault(norm, row)
    return exact, normalized


def _schema_projection_owner_for_ref(
    ref: str,
    alias_index: Mapping[str, Mapping[str, Any]],
    alias_norm_index: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any] | None:
    text = _text(ref)
    if not text:
        return None
    if text.startswith("global_execution_schema:"):
        return {"owner_kind": "root_schema", "owner_field_path": text.split(":", 1)[1]}
    alias = alias_index.get(text) or alias_norm_index.get(normalize_field_path(text))
    if alias:
        return dict(alias)
    normalized = normalize_field_path(text)
    if normalized.startswith(ROOT_RUNTIME_FIELD_PREFIXES):
        return {"owner_kind": "root_schema", "owner_field_path": normalized}
    if normalized.startswith(DOWNSTREAM_RUNTIME_FIELD_PREFIXES):
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": normalized,
        }
    return None


def _default_runtime_field_owner(field: str) -> Dict[str, Any] | None:
    if _is_page_runtime_field(field):
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": "runtime.page",
        }
    return None


def _is_page_runtime_field(field: str) -> bool:
    text = _text(field)
    return text in PAGE_RUNTIME_FIELD_NAMES or text.endswith(".page")


def _append_projected_ref(
    owner: Mapping[str, Any] | None,
    canonical_refs: List[str],
    downstream_refs: List[str],
) -> None:
    if not owner:
        return
    owner_field_path = _text(owner.get("owner_field_path"))
    if not owner_field_path:
        return
    if owner.get("owner_kind") == "root_schema":
        _append_unique(canonical_refs, f"global_execution_schema:{owner_field_path}")
    elif owner.get("owner_kind") == "downstream_extension":
        _append_unique(downstream_refs, owner_field_path)


def _project_expected_state_diff_to_schema(
    expected_diff: Mapping[str, Any],
    legacy_refs: List[str],
    alias_index: Mapping[str, Mapping[str, Any]],
    alias_norm_index: Mapping[str, Mapping[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    canonical: Dict[str, Any] = {}
    downstream: Dict[str, Any] = {}
    owners = [
        _schema_projection_owner_for_ref(ref, alias_index, alias_norm_index)
        for ref in legacy_refs
    ]
    owners = [owner for owner in owners if owner]
    for field, value in expected_diff.items():
        field_text = _text(field)
        owner = _default_runtime_field_owner(field_text)
        if not owner:
            owner = _schema_projection_owner_for_ref(field_text, alias_index, alias_norm_index)
        if not owner:
            owner = _owner_from_field_name(field_text, owners)
        if not owner and len(owners) == 1 and len(expected_diff) == 1:
            owner = owners[0]
        if not owner:
            continue
        owner_path = _text(owner.get("owner_field_path"))
        if not owner_path:
            continue
        payload = {"field": field_text, "value": _raw_expected_value(value)}
        if owner.get("owner_kind") == "root_schema":
            canonical[f"global_execution_schema:{owner_path}"] = payload
        elif owner.get("owner_kind") == "downstream_extension":
            downstream[owner_path] = payload
    return canonical, downstream


def _owner_from_field_name(field_name: str, owners: List[Mapping[str, Any]]) -> Dict[str, Any] | None:
    normalized_field = normalize_field_path(field_name)
    for owner in owners:
        owner_path = _text(owner.get("owner_field_path"))
        normalized_owner = normalize_field_path(owner_path)
        if not normalized_owner:
            continue
        if normalized_owner.endswith(f".{normalized_field}") or normalized_owner == normalized_field:
            return dict(owner)
        if normalized_field and normalized_field in normalized_owner.split("."):
            return dict(owner)
    if len(owners) == 1 and field_name in {"selected_value", "value", "query", "draft"}:
        return dict(owners[0])
    return None


def _raw_expected_value(value: Any) -> Any:
    if isinstance(value, Mapping) and "value" in value:
        return value.get("value")
    if isinstance(value, Mapping) and "expected" in value:
        return value.get("expected")
    return value


def _projection_state_reads(step: Mapping[str, Any], action_id: str) -> List[str]:
    reads: List[str] = []
    for projection in _as_list(_transaction(step).get("affected_render_projections")):
        pd = _as_dict(projection)
        source_steps = {_text(item) for item in _as_list(pd.get("source_step_keys"))}
        l0_steps = {_text(item) for item in _as_list(pd.get("l0_transition_ids"))}
        if source_steps and action_id not in source_steps and action_id not in l0_steps:
            continue
        for key in ("reads_state", "state_ref"):
            ref = _text(pd.get(key))
            if ref:
                reads.append(ref)
        reads.extend(_text(ref) for ref in _as_list(pd.get("reads_state_refs")) if _text(ref))

    def keep(ref: str) -> bool:
        lowered = ref.lower()
        if "page_runtime" in lowered:
            return False
        if lowered.endswith("_page") or lowered.endswith("_list"):
            return False
        return any(
            token in lowered
            for token in (
                "route",
                "query",
                "context",
                "criteria",
                "form",
                "search",
                "filter",
                "date",
                "station",
                "passenger",
            )
        )

    return [ref for ref in _dedupe(reads) if keep(ref)]


def _pre_state_reads(step: Mapping[str, Any]) -> List[str]:
    pre_state = _as_dict(_transaction(step).get("pre_state"))
    reads: List[str] = []
    for bucket in ("selections", "drafts"):
        reads.extend(_text(key) for key in _as_dict(pre_state.get(bucket)).keys() if _text(key))
    query = _as_dict(pre_state.get("query"))
    if query and (query.get("default") or query.get("committed_values")):
        reads.append("state::query_context")
    return _dedupe(reads)


def _iter_query_contract_rows(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if _text(value.get("query_state_owner")):
            yield value
        for nested in value.values():
            yield from _iter_query_contract_rows(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_query_contract_rows(nested)


def _domain_query_state_reads(
    action_id: str,
    step: Mapping[str, Any],
    domain_contracts: Optional[Mapping[str, Any]],
) -> List[str]:
    if not isinstance(domain_contracts, Mapping):
        return []
    source = _source(step)
    if not source:
        return []
    trace_id, _ = _trace_step(action_id)
    step_no = _step_number(action_id)
    scored: List[tuple[int, str]] = []
    source_stem = source.rsplit(".", 1)[0]

    for row in _iter_query_contract_rows(domain_contracts):
        owner = _text(row.get("query_state_owner"))
        if not owner:
            continue
        page_id = _text(row.get("page_id") or row.get("page_filename") or row.get("filename"))
        input_action_id = _text(row.get("input_action_id") or row.get("source_action_id"))
        evidence_refs = [_text(ref) for ref in _as_list(row.get("source_evidence_refs")) if _text(ref)]

        score = 0
        if page_id == source:
            score += 100
        elif source_stem and source_stem in owner:
            score += 70
        elif page_id:
            continue

        related_action_ids = [input_action_id, *evidence_refs]
        if trace_id and any(ref.startswith(f"{trace_id}:") for ref in related_action_ids if ref):
            score += 30
        elif trace_id and trace_id in owner:
            score += 20

        producer_steps = [
            _step_number(ref)
            for ref in related_action_ids
            if ref and _step_number(ref) >= 0
        ]
        if producer_steps:
            if step_no >= 0 and any(producer_step < step_no for producer_step in producer_steps):
                score += 20
            elif step_no >= 0:
                continue

        if score >= 90:
            scored.append((score, owner))

    return _dedupe(owner for _, owner in sorted(scored, key=lambda item: (-item[0], item[1])))


def _stateful_submit_reads(
    action_id: str,
    step: Mapping[str, Any],
    typed_action_contracts: Mapping[str, Any],
    domain_contracts: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    typed_reads = _state_reads(action_id, typed_action_contracts)
    if typed_reads:
        return typed_reads
    projection_reads = _projection_state_reads(step, action_id)
    if projection_reads:
        return projection_reads
    domain_reads = _domain_query_state_reads(action_id, step, domain_contracts)
    if domain_reads:
        return domain_reads
    commit_draft_reads = _commit_draft_state_reads(step)
    if commit_draft_reads:
        return commit_draft_reads
    return _pre_state_reads(step)


def _activation_target(step: Mapping[str, Any]) -> bool:
    source = _source(step)
    target = _target(step)
    if not source or not target or source == target:
        return False
    text = _operation_intent(step)
    target_l = target.lower()
    resolver_target = any(
        token in target_l
        for token in ("selector", "select", "picker", "search", "query", "calendar", "date", "edit", "form")
    )
    field_activation_text = any(token in text for token in ("出发", "目的", "到达", "日期", "日历", "选择"))
    return resolver_target and field_activation_text


def _projection_from_step(step: Mapping[str, Any], target: str) -> Dict[str, Any]:
    projections = _as_list(_transaction(step).get("affected_render_projections"))
    for projection in projections:
        pd = _as_dict(projection)
        if pd:
            return {
                "page": target,
                "projection_id": _text(pd.get("projection_id")),
                "reads_state_refs": [_text(ref) for ref in _as_list(pd.get("reads_state_refs")) if _text(ref)],
                "expected_text": _extract_quoted_value(_operation_intent(step)),
            }
    return {
        "page": target,
        "expected_text": _extract_quoted_value(_operation_intent(step)),
    }


def _runtime_event_for(effect_kind: str) -> str:
    mapping = {
        "select_result_return": "picker_selection_committed",
        "stateful_submit": "stateful_submit_attempted",
        "save_return": "save_return_committed",
        "same_page_mutation": "same_page_state_mutated",
        "query_projection": "query_projection_updated",
        "query_commit": "query_committed",
        "gesture_route": "gesture_route_committed",
        "route": "route_navigated",
        "activation": "activation_opened",
        "close_or_back_return": "return_committed",
    }
    return mapping.get(effect_kind, f"{effect_kind}_committed")


def _dom_text_labels_for_action(action: Mapping[str, Any]) -> List[str]:
    labels: List[str] = []
    intent = _text(_as_dict(action.get("source_evidence")).get("operation_intent"))
    operation = _text(action.get("operation")).lower()
    label_contract = _dom_label_contract_from_intent(intent)
    host_label = _text(label_contract.get("host_display_label"))
    if host_label and operation not in {"fill", "type", "input"}:
        labels.append(host_label)
    elif operation not in {"fill", "type", "input"} and not label_contract.get("context_text_must_include"):
        labels.extend(_extract_quoted_values(intent))
        labels.extend(_unquoted_intent_labels(intent))
    control_label_rules = (
        (("返回", "左上角", "返回按钮", "back"), ("返回", "back")),
        (("删除键", "删除", "退格", "backspace"), ("删除", "backspace")),
        (("交换按钮", "交换", "互换", "swap"), ("交换", "swap")),
        (("更多菜单", "更多", "more"), ("更多", "more")),
        (("选择框", "勾选", "checkbox"), ("选择", "勾选", "checkbox")),
        (("搜索输入框", "搜索框"), ("搜索", "输入")),
        (("输入框",), ("输入",)),
        (("分类区域", "分类区", "分类", "category", "shortcut"), ("分类", "category")),
        (("轮播", "carousel"), ("轮播", "carousel")),
        (("预订", "订购"), ("预订",)),
        (("下一步",), ("下一步",)),
    )
    intent_lower = intent.lower()
    for triggers, additions in control_label_rules:
        if any(trigger.lower() in intent_lower for trigger in triggers):
            labels.extend(additions)
    selected_value = _text(action.get("selected_value"))
    if selected_value:
        labels.append(selected_value)
    effect_kind = _text(action.get("effect_kind"))
    target = _text(action.get("expected_target_filename"))
    opens_picker_or_calendar = (
        effect_kind == "activation"
        and any(token in target for token in ("selector", "picker", "calendar"))
    )
    if effect_kind in {"stateful_submit", "save_return", "route", "activation"}:
        command_tokens = ("查询车票", "查询", "搜索", "提交", "确认", "完成", "保存", "确定", "汽车票", "飞机票")
        if opens_picker_or_calendar:
            command_tokens = ()
        elif _is_tab_or_mode_route(action):
            command_tokens = ("汽车票", "飞机票", "火车票")
        for token in command_tokens:
            if token in intent:
                labels.append(token)
    if effect_kind == "query_projection":
        labels.extend(["搜索", "输入"])
    elif operation in {"fill", "type", "input"}:
        labels.append("输入")
    labels.extend(_as_list(action.get("gesture_reveals")))
    return _dedupe(label for label in labels if 0 < len(label) <= 32)


def _dom_label_contract_from_intent(intent: str) -> Dict[str, Any]:
    raw = _text(intent)
    if not raw:
        return {}
    contract: Dict[str, Any] = {}
    quoted = [
        value.strip()
        for value in re.findall(r"[“\"']([^”\"']{1,32})[”\"']", raw)
        if value.strip()
    ]
    context_labels: List[str] = []
    accessible_labels: List[str] = []
    host_label = ""
    if quoted:
        first = quoted[0]
        after = raw.split(first, 1)[1] if first in raw else ""
        if re.search(r"(?:区域|板块|栏目|分区|模块|列表|频道)的(?:第[一二三四五六七八九十\d]+|首个|第一)", after):
            context_labels.append(first)
        elif re.search(r"(?:右侧|左侧|旁边|后面|前面|同一行|所在行|该行|行内)的?", after):
            if len(quoted) > 1:
                context_labels.extend(quoted[:-1])
                host_label = quoted[-1]
            else:
                context_labels.append(first)
                control_match = re.search(
                    r"(删除|移除|编辑|修改|更多|查看|选择|勾选|取消|关闭|展开|收起|全部|预约|预订|购买|购票|抢票|trash|delete|edit|more|remove)",
                    after,
                    re.IGNORECASE,
                )
                if control_match:
                    host_label = control_match.group(1)
        else:
            host_label = first
    intent_lower = raw.lower()
    if any(token in intent_lower for token in ("删除", "移除", "退格", "backspace", "delete", "remove", "trash")):
        accessible_labels.extend(["删除", "backspace"])
        if not host_label:
            host_label = "删除"
    elif any(token in intent_lower for token in ("编辑", "修改", "edit")):
        accessible_labels.append("编辑")
        if not host_label:
            host_label = "编辑"
    elif any(token in intent_lower for token in ("更多", "more")):
        accessible_labels.extend(["更多", "more"])
        if not host_label:
            host_label = "更多"
    if host_label:
        contract["host_display_label"] = host_label[:24]
    context_labels = _dedupe(context_labels)[:8]
    accessible_labels = _dedupe(accessible_labels)[:8]
    if context_labels:
        contract["context_text_must_include"] = context_labels
        contract["context_match_scope"] = "ancestor_or_nearby_region"
    if accessible_labels:
        contract["accessible_label_must_include"] = accessible_labels
    return contract


def _unquoted_intent_labels(intent: str) -> List[str]:
    labels: List[str] = []
    text = _text(intent)
    if not text:
        return labels
    for year, month, day in re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text):
        labels.append(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
        labels.append(f"{int(year)}年{int(month)}月{int(day)}日")
    if "“" in text or "\"" in text or "'" in text:
        return labels
    match = re.search(r"(?:点击|选择|打开)(?P<object>.+)$", text)
    if not match:
        return labels
    obj = re.sub(r"^(?:日历中的|搜索结果中的|车站列表中的|常用车站中的|顶部|第一个|第一项)", "", match.group("object")).strip()
    obj = re.sub(r"(?:按钮|入口)$", "", obj).strip()
    if obj and "输入框" not in obj and "搜索框" not in obj:
        labels.append(obj)
    return labels


def build_action_effect_contracts(
    contract_replay_plan: Mapping[str, Any],
    typed_action_contracts: Optional[Mapping[str, Any]] = None,
    skipped_steps: Optional[Iterable[Mapping[str, Any]]] = None,
    domain_contracts: Optional[Mapping[str, Any]] = None,
    page_boundary_authority: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    typed_action_contracts = typed_action_contracts or {}
    page_boundary_authority = page_boundary_authority or {}
    skipped_by_action_id = _skipped_steps_by_action_id(skipped_steps)
    traces = _as_list(contract_replay_plan.get("traces"))
    actions: List[Dict[str, Any]] = []
    by_action: Dict[str, Dict[str, Any]] = {}
    excluded_actions: List[Dict[str, Any]] = []
    summary_counter: Counter[str] = Counter()

    for trace in traces:
        trace_dict = _as_dict(trace)
        trace_id = _text(trace_dict.get("trace_id") or trace_dict.get("id"))
        previous_activation: Optional[Dict[str, Any]] = None
        trace_steps = sorted(
            [_as_dict(step) for step in _as_list(trace_dict.get("steps"))],
            key=_trace_step_sort_key,
        )
        for step_index, step in enumerate(trace_steps):
            step = _as_dict(step)
            action_id = _text(step.get("action_id"))
            if not action_id:
                continue
            skipped = skipped_by_action_id.get(action_id)
            skip_reason = _text(_as_dict(skipped).get("skip_reason"))
            if skipped and _skip_reason_excludes_action_effect(skip_reason):
                excluded_actions.append(
                    {
                        "action_id": action_id,
                        "trace_id": _text(_as_dict(skipped).get("trace_id")) or _trace_step(action_id)[0],
                        "step_index": _step_number(action_id),
                        "skip_reason": skip_reason,
                        "exclusion_reason": "skipped_step_not_reward_action_effect",
                        "operation": _text(step.get("operation")),
                        "reducer": _text(step.get("reducer")),
                        "source_filename": _source(step),
                        "target_filename": _target(step),
                    }
                )
                summary_counter["excluded_skipped_step"] += 1
                continue
            if _is_startup_or_splash_gate(step):
                excluded_actions.append(
                    {
                        "action_id": action_id,
                        "trace_id": _trace_step(action_id)[0],
                        "step_index": _step_number(action_id),
                        "skip_reason": "transient_launch_gate",
                        "exclusion_reason": "startup_or_splash_gate_not_reward_action_effect",
                        "operation": _text(step.get("operation")),
                        "reducer": _text(step.get("reducer")),
                        "source_filename": _source(step),
                        "target_filename": _target(step),
                    }
                )
                summary_counter["excluded_startup_or_splash_gate"] += 1
                continue
            step_trace_id, step_no = _trace_step(action_id)
            trace_id = trace_id or step_trace_id
            boundary_authority = _page_boundary_authority(
                step,
                action_id,
                page_boundary_authority,
            )
            runtime_projection_boundary = _runtime_projection_authority(boundary_authority)
            source = _source(step)
            target = _target(step)
            if runtime_projection_boundary:
                _validate_runtime_projection_authority(action_id, boundary_authority)
                canonical_filename = _canonical_projection_filename(boundary_authority)
                source = canonical_filename
                target = canonical_filename
            next_source = _next_step_source(trace_steps, step_index)
            next_target = _next_step_target(trace_steps, step_index)
            operation = _text(step.get("operation"))
            state_refs = _state_refs(step) or _typed_state_writes(action_id, typed_action_contracts)
            if runtime_projection_boundary:
                state_refs = _authority_state_refs(boundary_authority) or state_refs
            state_reads = _stateful_submit_reads(action_id, step, typed_action_contracts, domain_contracts)
            next_step = trace_steps[step_index + 1] if step_index + 1 < len(trace_steps) else {}
            gesture_scope = _stateful_component_gesture_scope(step, next_step)
            previous_step = trace_steps[step_index - 1] if step_index > 0 else {}
            previous_action_id = _text(_as_dict(previous_step).get("action_id"))
            previous_gesture = _as_dict(by_action.get(previous_action_id))
            previous_gesture_ref = ""
            if previous_gesture and _text(previous_gesture.get("source_filename")) == source:
                refs = _as_list(previous_gesture.get("state_refs"))
                previous_gesture_ref = _text(refs[0]) if refs else ""
            effect_kind = "route"
            reward_relevant = True
            state_changing = bool(state_refs or _expected_state_diff(step))
            selected_value = ""
            field_role = _text(_typed_action(action_id, typed_action_contracts).get("field_role"))
            expected_state_diff = (
                _expected_state_diff(step)
                or _typed_expected_state_diff(action_id, typed_action_contracts)
            )
            if runtime_projection_boundary:
                expected_state_diff = (
                    _as_dict(boundary_authority.get("expected_state_diff"))
                    or expected_state_diff
                )

            if runtime_projection_boundary:
                effect_kind = "same_page_mutation"
                state_changing = True
            elif _is_terminal_step(step):
                effect_kind = "terminal_observation"
                reward_relevant = False
                state_changing = False
                if _is_non_interactive_agent_step(step):
                    target = source
            elif _is_save_return(step):
                effect_kind = "save_return"
                state_changing = True
                if not expected_state_diff:
                    expected_state_diff = _save_return_semantic_diff(step, state_refs)
            elif _is_select_result_return(step, previous_activation, next_source, next_target):
                effect_kind = "select_result_return"
                state_changing = True
                activation_source = _source(previous_activation or {})
                activation_target = _target(previous_activation or {})
                if (
                    activation_source
                    and activation_target
                    and source == activation_source
                    and target == activation_source
                ):
                    source = activation_target
                    target = activation_source
                selected_value = _clean_entity_value(_extract_quoted_value(_operation_intent(step)))
                field_role = _field_role_from_text(_operation_intent(previous_activation or {}))
                state_ref = _caller_owned_state_ref(field_role, state_refs)
                if state_ref:
                    state_refs = [state_ref]
                expected_state_diff = {
                    "selected_value": selected_value,
                    field_role: selected_value,
                }
            elif _is_edit_surface_route(step, next_source, next_target):
                effect_kind = "route"
                state_changing = True
            elif _is_stateful_submit(step, state_reads):
                effect_kind = "stateful_submit"
                state_changing = False
                state_refs = state_reads
            elif _is_query_projection(step, action_id, typed_action_contracts):
                effect_kind = "query_projection"
                state_changing = True
            elif _is_same_page_mutation(step, action_id, typed_action_contracts):
                effect_kind = "same_page_mutation"
                state_changing = True
                if not expected_state_diff:
                    expected_state_diff = _same_page_mutation_semantic_diff(step, state_refs)
            elif gesture_scope:
                effect_kind = "same_page_mutation"
                state_changing = True
                state_refs = [gesture_scope["state_ref"]]
                expected_state_diff = dict(gesture_scope.get("expected_state_diff") or {})
            elif _is_viewport_scroll(step):
                effect_kind = "viewport_scroll"
                reward_relevant = False
                state_changing = False
                target = source
                state_refs = []
                expected_state_diff = {}
            elif source and source == target and (state_refs or expected_state_diff):
                effect_kind = "same_page_mutation"
                state_changing = True
            elif _is_gesture_route(step):
                effect_kind = "gesture_route"
                state_changing = True
            elif _activation_target(step):
                effect_kind = "activation"
                state_changing = bool(state_refs)
                inferred_field_role = _field_role_from_text(_operation_intent(step))
                if inferred_field_role != "selection":
                    field_role = inferred_field_role

            if runtime_projection_boundary:
                pass
            elif not _has_authoritative_same_page_navigation(step):
                target = _closed_target_from_next_step(effect_kind, source, target, next_source, next_target)

            transaction_id = ""
            if effect_kind == "select_result_return" and previous_activation:
                transaction_id = (
                    f"txn::{trace_id}::step{_step_number(_as_dict(previous_activation).get('action_id', ''))}"
                    f"::step{_step_number(action_id)}"
                )
            elif effect_kind in {
                "query_projection",
                "stateful_submit",
                "save_return",
                "same_page_mutation",
                "gesture_route",
            }:
                transaction_id = f"txn::{trace_id}::step{step_no}::{effect_kind}"

            runtime_transaction = {
                "transaction_id": transaction_id,
                "effect_kind": effect_kind,
                "state_reads": state_reads if effect_kind == "stateful_submit" else [],
                "state_writes": state_refs if state_changing else [],
                "return_target": target if effect_kind in {"select_result_return", "save_return", "close_or_back_return"} else "",
                "field_role": field_role,
                "selected_value": selected_value,
            }
            visibility_precondition: Dict[str, Any] = {}
            if (
                previous_gesture_ref
                and not gesture_scope
                and _text(previous_gesture.get("gesture_scope"))
                and _text(previous_gesture.get("gesture_scope")) != "viewport_scroll"
            ):
                reveal_values = _as_list(previous_gesture.get("gesture_reveals"))
                required_value = _text(reveal_values[0]) if reveal_values else ""
                consumer_reads = _dedupe([*_as_list(state_reads), previous_gesture_ref])
                state_reads = consumer_reads
                visibility_precondition = {
                    "producer_action_id": previous_action_id,
                    "state_ref": previous_gesture_ref,
                    "required_visible_value": required_value,
                    "dependency_kind": "visibility_precondition",
                }
                runtime_transaction["state_reads"] = consumer_reads

            contract = {
                "action_id": action_id,
                "trace_id": trace_id,
                "step_index": _step_number(action_id),
                "source_filename": source,
                "target_filename": target,
                "expected_target_filename": target,
                "operation": operation,
                "reducer": (
                    _text(boundary_authority.get("reducer")) or "runtime_projection"
                    if runtime_projection_boundary
                    else "component_visible_window" if gesture_scope else _text(step.get("reducer"))
                ),
                "effect_kind": effect_kind,
                "reward_relevant": reward_relevant,
                "state_changing": state_changing,
                "transaction_id": transaction_id,
                "component_ref": _text(gesture_scope.get("component_ref")) if gesture_scope else "",
                "entity_ref": f"entity::{selected_value}" if selected_value else "",
                "state_refs": state_refs,
                "expected_state_diff": expected_state_diff,
                "required_runtime_events": [_runtime_event_for(effect_kind)] if reward_relevant else [],
                "runtime_transaction": runtime_transaction,
                "source_evidence": {
                    "operation_intent": _operation_intent(step),
                    "state_refs": _state_refs(step),
                    "state_reads": state_reads,
                },
            }
            if runtime_projection_boundary:
                contract["page_boundary_authority"] = {
                    "boundary_decision": "runtime_projection",
                    "general_page_id": _text(
                        boundary_authority.get("source_general_page_id")
                        or boundary_authority.get("general_page_id")
                    ),
                    "source_general_page_id": _text(
                        boundary_authority.get("source_general_page_id")
                        or boundary_authority.get("general_page_id")
                    ),
                    "target_general_page_id": _text(
                        boundary_authority.get("target_general_page_id")
                        or boundary_authority.get("general_page_id")
                    ),
                    "source_materialized_page_id": _text(
                        boundary_authority.get("source_materialized_page_id")
                    ),
                    "target_materialized_page_id": _text(
                        boundary_authority.get("target_materialized_page_id")
                    ),
                    "canonical_runtime_filename": source,
                    "source_observation_key": _text(
                        boundary_authority.get("source_observation_key")
                    ),
                    "post_observation_key": _text(
                        boundary_authority.get("post_observation_key")
                    ),
                    "state_delta": dict(_as_dict(boundary_authority.get("state_delta"))),
                    "state_refs": list(state_refs),
                    "expected_state_diff": dict(expected_state_diff),
                    "authority_ref": _text(boundary_authority.get("authority_ref")),
                }
            if gesture_scope:
                contract["gesture_scope"] = gesture_scope["gesture_scope"]
                contract["gesture_component_role"] = gesture_scope["component_role"]
                contract["gesture_axis"] = gesture_scope["axis"]
                contract["gesture_direction"] = gesture_scope["direction"]
                contract["gesture_reveals"] = gesture_scope["reveals"]
                contract["source_evidence"]["gesture_scope"] = {
                    key: gesture_scope[key]
                    for key in (
                        "gesture_scope",
                        "component_role",
                        "component_ref",
                        "state_ref",
                        "axis",
                        "direction",
                        "reveals",
                    )
                }
            if visibility_precondition:
                contract["visibility_precondition_action_id"] = visibility_precondition["producer_action_id"]
                contract["visibility_precondition"] = visibility_precondition
                contract["state_reads"] = state_reads
                if visibility_precondition.get("required_visible_value"):
                    contract["required_visible_value"] = visibility_precondition["required_visible_value"]
            contract["dom_binding_required"] = _requires_dom_binding(contract)
            contract["dom_binding_ref"] = f"binding::{action_id}" if contract["dom_binding_required"] else ""
            if selected_value:
                contract["selected_value"] = selected_value
            if field_role:
                contract["caller_context"] = {"field_role": field_role}

            actions.append(contract)
            by_action[action_id] = contract
            summary_counter[effect_kind] += 1

            if effect_kind == "select_result_return":
                previous_activation = None
            elif effect_kind == "activation":
                previous_activation = dict(step)


    represented_action_ids = {
        _text(item.get("action_id"))
        for item in [*actions, *excluded_actions]
        if _text(item.get("action_id"))
    }
    for action_id, skipped in sorted(skipped_by_action_id.items()):
        if action_id in represented_action_ids:
            continue
        skip_reason = _text(_as_dict(skipped).get("skip_reason"))
        if not _skip_reason_excludes_action_effect(skip_reason):
            continue
        excluded_actions.append(
            {
                "action_id": action_id,
                "trace_id": _text(_as_dict(skipped).get("trace_id")) or _trace_step(action_id)[0],
                "step_index": _step_number(action_id),
                "skip_reason": skip_reason,
                "exclusion_reason": "skipped_step_not_in_replay_plan_cleanup",
                "operation": "",
                "reducer": "",
                "source_filename": "",
                "target_filename": "",
            }
        )
        summary_counter["excluded_skipped_step_cleanup"] += 1

    return {
        "format": "action_effect_contracts",
        "schema_revision": 1,
        "summary": {
            "action_count": len(actions),
            "effect_kind_counts": dict(sorted(summary_counter.items())),
            "reward_relevant_count": sum(1 for action in actions if action.get("reward_relevant")),
            "dom_binding_required_count": sum(1 for action in actions if action.get("dom_binding_required")),
            "transaction_action_count": sum(1 for action in actions if action.get("transaction_id")),
            "excluded_action_count": len(excluded_actions),
        },
        "actions": actions,
        "actions_by_id": by_action,
        "excluded_actions": excluded_actions,
    }


def build_runtime_transaction_contracts(
    action_effect_contracts: Mapping[str, Any],
) -> Dict[str, Any]:
    transactions: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    actions = [_as_dict(action) for action in _as_list(action_effect_contracts.get("actions"))]
    actions_by_id = {_text(action.get("action_id")): action for action in actions}

    for action in actions:
        effect_kind = _text(action.get("effect_kind"))
        txn_id = _text(action.get("transaction_id"))
        if not txn_id:
            continue
        trace_id = _text(action.get("trace_id"))
        transaction: Dict[str, Any]
        if effect_kind == "select_result_return":
            action_id = _text(action.get("action_id"))
            activation_action_id = ""
            match = re.search(r"::step(\d+)::step(\d+)$", txn_id)
            if match:
                activation_action_id = f"{trace_id}:step{match.group(1)}"
            activation = actions_by_id.get(activation_action_id, {})
            field_role = _text(_as_dict(action.get("caller_context")).get("field_role") or _as_dict(action.get("runtime_transaction")).get("field_role"))
            selected_value = _text(action.get("selected_value") or _as_dict(action.get("runtime_transaction")).get("selected_value"))
            state_refs = [_text(ref) for ref in _as_list(action.get("state_refs")) if _text(ref)]
            state_write = _caller_owned_state_ref(field_role, state_refs)
            transaction = {
                "transaction_id": txn_id,
                "transaction_kind": "caller_aware_picker",
                "trace_id": trace_id,
                "open_action_id": activation_action_id,
                "commit_action_id": action_id,
                "caller": {
                    "source_filename": _text(activation.get("source_filename")),
                    "field_role": field_role,
                    "component_ref": _text(activation.get("component_ref")),
                },
                "resolver_surface": {
                    "filename": _text(action.get("source_filename")),
                },
                "commit": {
                    "operation": _text(action.get("operation")),
                    "selected_value": selected_value,
                    "selected_entity_ref": _text(action.get("entity_ref")),
                    "state_write": state_write,
                    "return_target": _text(action.get("expected_target_filename")),
                },
                "projection": _projection_from_action(action, state_write),
                "negative_probes": [
                    "wrong_selected_value",
                    "missing_caller_context",
                    "return_without_projection",
                ],
            }
        elif effect_kind == "stateful_submit":
            transaction = {
                "transaction_id": txn_id,
                "transaction_kind": "stateful_submit_route",
                "trace_id": trace_id,
                "source_action_id": _text(action.get("action_id")),
                "source_page": _text(action.get("source_filename")),
                "target_page": _text(action.get("expected_target_filename")),
                "state_reads": _as_list(_as_dict(action.get("runtime_transaction")).get("state_reads")),
                "route_resolver": "compiled_stateful_route_manifest",
                "negative_probes": ["missing_state_read", "wrong_state_tuple", "wrong_route_target"],
            }
        elif effect_kind == "save_return":
            transaction = {
                "transaction_id": txn_id,
                "transaction_kind": "edit_save_return",
                "trace_id": trace_id,
                "source_action_id": _text(action.get("action_id")),
                "source_page": _text(action.get("source_filename")),
                "return_target": _text(action.get("expected_target_filename")),
                "state_writes": _as_list(action.get("state_refs")),
                "expected_state_diff": _as_dict(action.get("expected_state_diff")),
                "negative_probes": ["missing_saved_state", "terminal_summary_only"],
            }
        else:
            transaction = {
                "transaction_id": txn_id,
                "transaction_kind": effect_kind,
                "trace_id": trace_id,
                "source_action_id": _text(action.get("action_id")),
                "source_page": _text(action.get("source_filename")),
                "target_page": _text(action.get("expected_target_filename")),
                "state_writes": _as_list(action.get("state_refs")),
            }
        transactions.append(transaction)
        by_id[txn_id] = transaction

    return {
        "format": "runtime_transaction_contracts",
        "schema_revision": 1,
        "summary": {
            "transaction_count": len(transactions),
            "by_transaction_kind": dict(sorted(Counter(_text(t.get("transaction_kind")) for t in transactions).items())),
        },
        "transactions": transactions,
        "transactions_by_id": by_id,
    }


def project_action_effect_contracts(
    action_effect_contracts: Mapping[str, Any],
    downstream_projection: Mapping[str, Any],
) -> Dict[str, Any]:

    if not isinstance(action_effect_contracts, Mapping):
        return {}
    alias_index, alias_norm_index = _schema_projection_alias_indexes(downstream_projection)
    projected = dict(action_effect_contracts)
    actions: List[Dict[str, Any]] = []
    projected_action_count = 0
    for action in _as_list(action_effect_contracts.get("actions")):
        action_row = dict(_as_dict(action))
        legacy_refs = [_text(ref) for ref in _as_list(action_row.get("state_refs")) if _text(ref)]
        canonical_refs: List[str] = []
        downstream_refs: List[str] = []
        for ref in legacy_refs:
            _append_projected_ref(
                _schema_projection_owner_for_ref(ref, alias_index, alias_norm_index),
                canonical_refs,
                downstream_refs,
            )
        canonical_diff, downstream_diff = _project_expected_state_diff_to_schema(
            _as_dict(action_row.get("expected_state_diff")),
            legacy_refs,
            alias_index,
            alias_norm_index,
        )
        for ref in canonical_diff:
            _append_unique(canonical_refs, ref)
        for ref in downstream_diff:
            _append_unique(downstream_refs, ref)
        action_row["legacy_state_refs"] = list(action_row.get("legacy_state_refs") or legacy_refs)
        action_row["canonical_state_refs"] = _dedupe([
            *[_text(ref) for ref in _as_list(action_row.get("canonical_state_refs")) if _text(ref)],
            *canonical_refs,
        ])
        action_row["downstream_extension_refs"] = _dedupe([
            *[_text(ref) for ref in _as_list(action_row.get("downstream_extension_refs")) if _text(ref)],
            *downstream_refs,
        ])
        if canonical_diff:
            action_row["canonical_expected_state_diff"] = {
                **_as_dict(action_row.get("canonical_expected_state_diff")),
                **canonical_diff,
            }
        if downstream_diff:
            action_row["downstream_expected_state_diff"] = {
                **_as_dict(action_row.get("downstream_expected_state_diff")),
                **downstream_diff,
            }
        if action_row.get("canonical_state_refs") or action_row.get("downstream_extension_refs"):
            projected_action_count += 1
        actions.append(action_row)
    projected["actions"] = actions
    projected["actions_by_id"] = {
        _text(action.get("action_id")): action
        for action in actions
        if _text(action.get("action_id"))
    }
    projected["schema_projection"] = {
        "source": "global_execution_schema_downstream_projection",
        "projection_status": _text(_as_dict(downstream_projection.get("summary")).get("status"))
        if isinstance(downstream_projection, Mapping)
        else "",
        "projected_action_count": projected_action_count,
    }
    return projected


def collect_action_effect_required_refs(action_effect_contracts: Mapping[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for action in _as_list(action_effect_contracts.get("actions")):
        action_row = _as_dict(action)
        action_id = _text(action_row.get("action_id"))
        for ref in _as_list(action_row.get("state_refs")):
            ref_text = _text(ref)
            if ref_text:
                refs.append({
                    "ref": ref_text,
                    "source": "action_effect_contract",
                    "action_id": action_id,
                })
        for key in _as_dict(action_row.get("expected_state_diff")):
            key_text = _text(key)
            if key_text.startswith(("state::", "draft::", "runtime.", "runtimeState.", "taskOutcome.", "ui.")):
                refs.append({
                    "ref": key_text,
                    "source": "action_effect_expected_state_diff",
                    "action_id": action_id,
                })
    return refs


def _projection_from_action(action: Mapping[str, Any], state_write: str) -> Dict[str, Any]:
    target = _text(action.get("expected_target_filename"))
    return {
        "page": target,
        "selector_or_binding": _text(action.get("dom_binding_ref")),
        "state_ref": state_write,
        "expected_text": _text(action.get("selected_value")),
    }


def _handler_for(effect_kind: str) -> str:
    mapping = {
        "select_result_return": "GhostwebRuntime.commitSelectionAndReturn",
        "stateful_submit": "GhostwebRuntime.submitFromState",
        "save_return": "GhostwebRuntime.saveAndReturn",
        "same_page_mutation": "GhostwebRuntime.commitSamePageMutation",
        "gesture_route": "GhostwebRuntime.commitGestureRoute",
        "activation": "GhostwebRuntime.openInteractionSurface",
        "route": "GhostwebRouteExecution.navigate",
    }
    return mapping.get(effect_kind, "GhostwebRuntime.dispatchActionEffect")


def _requires_dom_binding(action: Mapping[str, Any]) -> bool:
    if not action.get("reward_relevant"):
        return False
    operation = _text(action.get("operation")).lower()
    effect_kind = _text(action.get("effect_kind"))
    if operation in GESTURE_ONLY_OPERATIONS:
        gesture_scope = _text(action.get("gesture_scope"))
        if _text(action.get("component_ref")) or (gesture_scope and gesture_scope != "viewport_scroll"):
            return True
        return False
    if effect_kind == "gesture_route":
        return False
    if operation in DOM_HOST_OPERATIONS:
        return True
    return effect_kind in {
        "activation",
        "query_projection",
        "route",
        "same_page_mutation",
        "save_return",
        "select_result_return",
        "stateful_submit",
    }


def _component_action_keys(value: Any) -> List[str]:
    values = value if isinstance(value, list) else [value]
    keys: List[str] = []
    for item in values:
        if isinstance(item, Mapping):
            for field in ("action_id", "step_key"):
                candidate = _text(item.get(field))
                if candidate and candidate not in keys:
                    keys.append(candidate)
            continue
        candidate = _text(item)
        if candidate and candidate not in keys:
            keys.append(candidate)
    return keys


def _pageenv_component_refs_by_action(
    page_environment_specs: Optional[Mapping[str, Any]],
) -> Dict[str, List[str]]:
    specs = _as_dict(
        page_environment_specs.get("page_environment_specs_by_filename")
        if isinstance(page_environment_specs, Mapping)
        else {}
    )
    refs_by_action: Dict[str, List[str]] = {}

    def record(component_ref: str, source: Mapping[str, Any]) -> None:
        if not component_ref:
            return
        for field in (
            "source_step_keys",
            "l0_transition_ids",
            "evidence_steps",
            "action_ids",
            "action_id",
            "step_key",
        ):
            for action_key in _component_action_keys(source.get(field)):
                refs = refs_by_action.setdefault(action_key, [])
                if component_ref not in refs:
                    refs.append(component_ref)

    for spec in specs.values():
        spec = _as_dict(spec)
        for component in _as_list(spec.get("components")):
            component = _as_dict(component)
            component_ref = _text(component.get("component_id") or component.get("component_ref"))
            record(component_ref, component)
            record(component_ref, _as_dict(component.get("interaction_contract")))
        for projection in _as_list(spec.get("render_projections")):
            projection = _as_dict(projection)
            component_ref = _text(projection.get("component_ref") or projection.get("component_id"))
            record(component_ref, projection)
    return refs_by_action


def _pageenv_component_refs_by_action_and_filename(
    page_environment_specs: Optional[Mapping[str, Any]],
) -> Dict[Tuple[str, str], List[str]]:
    specs = _as_dict(
        page_environment_specs.get("page_environment_specs_by_filename")
        if isinstance(page_environment_specs, Mapping)
        else {}
    )
    refs_by_action_and_filename: Dict[Tuple[str, str], List[str]] = {}

    def record(component_ref: str, source: Mapping[str, Any], filename: str) -> None:
        if not component_ref or not filename:
            return
        for field in (
            "source_step_keys",
            "l0_transition_ids",
            "evidence_steps",
            "action_ids",
            "action_id",
            "step_key",
        ):
            for action_key in _component_action_keys(source.get(field)):
                refs = refs_by_action_and_filename.setdefault(
                    (action_key, filename), []
                )
                if component_ref not in refs:
                    refs.append(component_ref)

    for filename, spec in specs.items():
        filename = _text(filename)
        spec = _as_dict(spec)
        for component in _as_list(spec.get("components")):
            component = _as_dict(component)
            component_ref = _text(
                component.get("component_id") or component.get("component_ref")
            )
            record(component_ref, component, filename)
            record(component_ref, _as_dict(component.get("interaction_contract")), filename)
        for projection in _as_list(spec.get("render_projections")):
            projection = _as_dict(projection)
            component_ref = _text(
                projection.get("component_ref") or projection.get("component_id")
            )
            record(component_ref, projection, filename)
    return refs_by_action_and_filename


def build_dom_binding_contracts(
    action_effect_contracts: Mapping[str, Any],
    runtime_transaction_contracts: Optional[Mapping[str, Any]] = None,
    downstream_projection: Optional[Mapping[str, Any]] = None,
    page_environment_specs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_transaction_contracts = runtime_transaction_contracts or {}
    if downstream_projection:
        action_effect_contracts = project_action_effect_contracts(
            action_effect_contracts,
            downstream_projection,
        )
    tx_by_id = _as_dict(runtime_transaction_contracts.get("transactions_by_id"))
    component_refs_by_action = _pageenv_component_refs_by_action(page_environment_specs)
    component_refs_by_action_and_filename = (
        _pageenv_component_refs_by_action_and_filename(page_environment_specs)
    )
    component_resolution_counts: Counter[str] = Counter()
    bindings: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}

    for action in _as_list(action_effect_contracts.get("actions")):
        action = _as_dict(action)
        if not _requires_dom_binding(action):
            continue
        action_id = _text(action.get("action_id"))
        effect_kind = _text(action.get("effect_kind"))
        txn_id = _text(action.get("transaction_id"))
        tx = _as_dict(tx_by_id.get(txn_id))
        selected_value = _text(action.get("selected_value") or _as_dict(tx.get("commit")).get("selected_value"))
        field_role = _text(_as_dict(action.get("caller_context")).get("field_role") or _as_dict(tx.get("caller")).get("field_role"))
        binding_id = _text(action.get("dom_binding_ref")) or f"binding::{action_id}"
        explicit_component_ref = _text(action.get("component_ref"))
        pageenv_component_refs = component_refs_by_action.get(action_id, [])
        source_filename = _text(action.get("source_filename"))
        source_page_refs = component_refs_by_action_and_filename.get(
            (action_id, source_filename), []
        )
        source_l0_suffix = f"::l0::{action_id}::source"
        source_l0_refs = [
            ref for ref in source_page_refs if ref.endswith(source_l0_suffix)
        ]
        if explicit_component_ref:
            component_ref = explicit_component_ref
            component_resolution = {
                "status": "resolved_action_effect",
                "source": "action_effect_contract.component_ref",
                "candidates": [component_ref],
            }
        elif len(source_l0_refs) == 1:
            component_ref = source_l0_refs[0]
            component_resolution = {
                "status": "resolved_page_environment",
                "source": "page_environment_specs.source_page_l0_component",
                "candidates": [component_ref],
            }
        elif len(source_page_refs) == 1:
            component_ref = source_page_refs[0]
            component_resolution = {
                "status": "resolved_page_environment",
                "source": "page_environment_specs.source_page_component",
                "candidates": [component_ref],
            }
        elif len(pageenv_component_refs) == 1:
            component_ref = pageenv_component_refs[0]
            component_resolution = {
                "status": "resolved_page_environment",
                "source": "page_environment_specs.components_or_render_projections",
                "candidates": [component_ref],
            }
        elif len(pageenv_component_refs) > 1:
            component_ref = ""
            component_resolution = {
                "status": "ambiguous_page_environment",
                "source": "page_environment_specs.components_or_render_projections",
                "candidates": sorted(pageenv_component_refs),
            }
        else:
            component_ref = ""
            component_resolution = {
                "status": "missing_page_environment",
                "source": "page_environment_specs.components_or_render_projections",
                "candidates": [],
            }
        component_resolution_counts[component_resolution["status"]] += 1
        payload = {
            "action_id": action_id,
            "effect_kind": effect_kind,
            "transaction_id": txn_id,
        }
        if selected_value:
            payload["selected_value"] = selected_value
        if field_role:
            payload["field_role"] = field_role
        if action.get("state_refs"):
            payload["state_refs"] = _as_list(action.get("state_refs"))
        if action.get("state_reads"):
            payload["state_reads"] = _as_list(action.get("state_reads"))
        for key in (
            "gesture_scope",
            "gesture_component_role",
            "gesture_axis",
            "gesture_direction",
            "gesture_reveals",
            "component_ref",
            "visibility_precondition_action_id",
            "visibility_precondition",
            "required_visible_value",
        ):
            if key == "component_ref":
                value = component_ref
            else:
                value = action.get(key)
            if value not in (None, "", [], {}):
                payload[key] = value

        label_contract = _dom_label_contract_from_intent(
            _text(_as_dict(action.get("source_evidence")).get("operation_intent"))
        )
        operation_intent = _text(_as_dict(action.get("source_evidence")).get("operation_intent"))
        if operation_intent:
            payload["operation_intent"] = operation_intent
        element_identity = {
            "component_ref": component_ref,
            "entity_ref": _text(action.get("entity_ref")),
            "text_must_include": _dom_text_labels_for_action(action),
        }
        for source_key, target_key in (
            ("context_text_must_include", "context_text_must_include"),
            ("context_match_scope", "context_match_scope"),
            ("accessible_label_must_include", "accessible_label_must_include"),
        ):
            value = label_contract.get(source_key)
            if value not in (None, "", [], {}):
                element_identity[target_key] = value

        binding = {
            "dom_binding_id": binding_id,
            "action_id": action_id,
            "source_filename": _text(action.get("source_filename")),
            "element_identity": element_identity,
            "component_ref_resolution": component_resolution,
            "interaction": {
                "event": _text(action.get("operation")) or "click",
                "handler": _handler_for(effect_kind),
                "payload": payload,
            },
            "postcondition": {
                "target_filename": _text(action.get("expected_target_filename")),
                "runtime_event": (_as_list(action.get("required_runtime_events")) or [""])[0],
                "state_diff": _as_dict(action.get("expected_state_diff")),
                "legacy_state_refs": _as_list(action.get("legacy_state_refs")) or _as_list(action.get("state_refs")),
                "canonical_state_refs": _as_list(action.get("canonical_state_refs")),
                "downstream_extension_refs": _as_list(action.get("downstream_extension_refs")),
                "canonical_expected_state_diff": _as_dict(action.get("canonical_expected_state_diff")),
                "downstream_expected_state_diff": _as_dict(action.get("downstream_expected_state_diff")),
            },
            "forbidden": {
                "wrong_entity_ref": effect_kind == "select_result_return",
                "handler_without_payload": True,
                "coordinate_only_binding": True,
            },
        }
        bindings.append(binding)
        by_id[binding_id] = binding

    return {
        "format": "dom_binding_contracts",
        "schema_revision": 1,
        "summary": {
            "binding_count": len(bindings),
            "handler_count": len({b["interaction"]["handler"] for b in bindings}),
            "component_ref_resolution_counts": dict(sorted(component_resolution_counts.items())),
        },
        "bindings": bindings,
        "bindings_by_id": by_id,
    }


def _contract_replay_steps_by_action_id(contract_replay_plan: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    steps_by_action_id: Dict[str, Dict[str, Any]] = {}
    plan = _as_dict(contract_replay_plan)
    for trace in _as_list(plan.get("traces")):
        for step in _as_list(_as_dict(trace).get("steps")):
            step_dict = _as_dict(step)
            action_id = _text(step_dict.get("action_id"))
            if action_id:
                steps_by_action_id[action_id] = step_dict
    return steps_by_action_id


def _allows_replay_boundary_rewrite(effect_kind: str) -> bool:
    return effect_kind in {
        "select_result_return",
        "save_return",
        "close_or_back_return",
        "terminal_observation",
    }


def validate_action_effect_contracts(
    action_effect_contracts: Mapping[str, Any],
    runtime_transaction_contracts: Optional[Mapping[str, Any]] = None,
    dom_binding_contracts: Optional[Mapping[str, Any]] = None,
    skipped_steps: Optional[Iterable[Mapping[str, Any]]] = None,
    contract_replay_plan: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_transaction_contracts = runtime_transaction_contracts or {}
    dom_binding_contracts = dom_binding_contracts or {}
    tx_by_id = _as_dict(runtime_transaction_contracts.get("transactions_by_id"))
    binding_by_id = _as_dict(dom_binding_contracts.get("bindings_by_id"))
    skipped_by_action_id = _skipped_steps_by_action_id(skipped_steps)
    replay_steps_by_action_id = _contract_replay_steps_by_action_id(contract_replay_plan or {})
    issues: List[Dict[str, Any]] = []

    for action in _as_list(action_effect_contracts.get("actions")):
        action = _as_dict(action)
        action_id = _text(action.get("action_id"))
        effect_kind = _text(action.get("effect_kind"))
        skipped = skipped_by_action_id.get(action_id)
        skip_reason = _text(_as_dict(skipped).get("skip_reason"))
        if action.get("reward_relevant") and skipped and _skip_reason_excludes_action_effect(skip_reason):
            issues.append(
                _issue(
                    "skipped_step_action_effect_leak",
                    action_id,
                    f"reward-relevant action_effect references skipped {skip_reason} step",
                )
            )
        if action.get("reward_relevant") and not effect_kind:
            issues.append(_issue("missing_effect_kind", action_id, "reward-relevant action has no effect_kind"))
        replay_step = _as_dict(replay_steps_by_action_id.get(action_id))
        if replay_step and not _allows_replay_boundary_rewrite(effect_kind):
            replay_source = _text(replay_step.get("source_filename"))
            replay_target = _text(replay_step.get("target_filename"))
            action_source = _text(action.get("source_filename"))
            action_target = _text(action.get("target_filename") or action.get("expected_target_filename"))
            if replay_source and action_source and replay_source != action_source:
                issues.append(_issue(
                    "contract_replay_source_drift",
                    action_id,
                    (
                        "action_effect source_filename diverges from contract_replay_plan "
                        f"({action_source} != {replay_source})"
                    ),
                ))
            if replay_target and action_target and replay_target != action_target:
                issues.append(_issue(
                    "contract_replay_target_drift",
                    action_id,
                    (
                        "action_effect target_filename diverges from contract_replay_plan "
                        f"({action_target} != {replay_target})"
                    ),
                ))
            if (
                replay_source
                and replay_target
                and replay_source == replay_target
                and action_target
                and action_target != action_source
                and effect_kind == "route"
            ):
                issues.append(_issue(
                    "contract_replay_effect_boundary_drift",
                    action_id,
                    "same-page replay step was compiled as route action_effect",
                ))
        txn_id = _text(action.get("transaction_id"))
        tx = _as_dict(tx_by_id.get(txn_id))
        binding_id = _text(action.get("dom_binding_ref"))
        binding = _as_dict(binding_by_id.get(binding_id))

        if effect_kind == "select_result_return":
            caller = _as_dict(tx.get("caller"))
            commit = _as_dict(tx.get("commit"))
            projection = _as_dict(tx.get("projection"))
            if not txn_id or not tx:
                issues.append(_issue("missing_runtime_transaction", action_id, "select_result_return has no runtime transaction"))
            if not caller.get("field_role"):
                issues.append(_issue("missing_caller_context", action_id, "select_result_return has no caller field_role"))
            if not commit.get("selected_value"):
                issues.append(_issue("missing_selected_value", action_id, "select_result_return has no selected value"))
            if not commit.get("state_write"):
                issues.append(_issue("missing_state_write", action_id, "select_result_return has no caller-owned state write"))
            if not commit.get("return_target"):
                issues.append(_issue("missing_return_target", action_id, "select_result_return has no return target"))
            if not projection:
                issues.append(_issue("missing_projection_assertion", action_id, "select_result_return has no projection assertion"))

        if effect_kind == "stateful_submit":
            reads = _as_list(_as_dict(action.get("runtime_transaction")).get("state_reads"))
            if not reads:
                issues.append(_issue("missing_stateful_submit_state_reads", action_id, "stateful_submit has no state_reads"))

        if effect_kind == "save_return":
            if _text(action.get("operation")).lower() not in {"click", "tap", "double_click", "dblclick", "submit", "press"}:
                issues.append(_issue("save_return_not_executable", action_id, "save_return must come from executable action"))

        if action.get("reward_relevant") and _requires_dom_binding(action):
            if not binding:
                issues.append(_issue("missing_dom_binding", action_id, "reward-relevant action has no DOM binding contract"))
            else:
                interaction = _as_dict(binding.get("interaction"))
                postcondition = _as_dict(binding.get("postcondition"))
                if not interaction.get("handler"):
                    issues.append(_issue("missing_binding_handler", action_id, "DOM binding has no handler"))
                if not _as_dict(interaction.get("payload")).get("action_id"):
                    issues.append(_issue("missing_binding_payload", action_id, "DOM binding has no payload"))
                component_resolution = _as_dict(binding.get("component_ref_resolution"))
                component_resolution_status = _text(component_resolution.get("status"))
                component_ref = _text(_as_dict(binding.get("element_identity")).get("component_ref"))
                if not component_ref:
                    issue_code = (
                        "ambiguous_dom_component_ref"
                        if component_resolution_status == "ambiguous_page_environment"
                        else "missing_dom_component_ref"
                    )
                    issues.append(_issue(
                        issue_code,
                        action_id,
                        "DOM binding lacks one explicit component_ref from action or PageEnv authority",
                    ))
                if effect_kind == "select_result_return" and not _as_dict(binding.get("element_identity")).get("entity_ref"):
                    issues.append(_issue("missing_entity_ref", action_id, "select result DOM binding has no entity_ref"))
                if not postcondition.get("runtime_event"):
                    issues.append(_issue("missing_runtime_event", action_id, "DOM binding has no runtime event postcondition"))

    blocking = [issue for issue in issues if issue.get("severity") == "blocking"]
    issue_code_counts = dict(sorted(Counter(_text(issue.get("code")) for issue in issues).items()))
    return {
        "format": "effect_contract_validation",
        "schema_revision": 1,
        "summary": {
            "issue_count": len(issues),
            "blocking_issue_count": len(blocking),
            "issue_code_counts": issue_code_counts,
            "status": "pass" if not blocking else "fail",
        },
        "issues": issues,
    }


def _issue(code: str, action_id: str, message: str) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": "blocking",
        "action_id": action_id,
        "message": message,
    }
