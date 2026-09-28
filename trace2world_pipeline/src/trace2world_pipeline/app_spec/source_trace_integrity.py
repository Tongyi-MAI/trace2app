
from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping


class QueryDraftActionClass(str, Enum):

    QUERY_DRAFT = "query_draft"
    NOT_QUERY_DRAFT = "not_query_draft"
    UNKNOWN = "unknown"


_QUERY_DRAFT_ACTIONS = {
    "type",
    "fill",
    "input",
    "input_text",
}

_NAVIGATION_ACTIONS = {
    "click",
    "tap",
    "press",
    "double_click",
    "dblclick",
    "select",
    "back",
    "return_back",
    "close",
    "close_overlay",
    "submit",
    "open",
}

_TERMINAL_ACTIONS = {
    "init",
    "terminal",
    "task_done",
    "terminate",
    "finish",
    "finished",
    "done",
}

_KNOWN_NON_QUERY_ACTIONS = (
    _NAVIGATION_ACTIONS
    | _TERMINAL_ACTIONS
    | {
        "scroll",
        "swipe",
        "drag",
        "open",
        "press",
        "system_button",
        "terminate",
    }
)

_TEXT_WRITE_PAYLOAD_KEYS = (
    "text",
    "value",
    "query",
    "input_text",
)

_QUERY_RESULT_TOKENS = (
    "result",
    "results",
    "search-results",
    "搜索结果",
    "结果页",
)

DEFAULT_SOURCE_TRACE_ACTION_CHAIN_ADMISSION_MODE = "hard"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return _clean(value).lower()


def _step_index(action: Mapping[str, Any]) -> int:
    for key in ("step_index", "step"):
        try:
            value = int(action.get(key))
        except Exception:
            continue
        if key == "step" and value > 0 and "step_index" not in action:
            return value - 1
        return value
    return -1


def _trace_actions(actions: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    result = [dict(item) for item in actions if isinstance(item, Mapping)]
    result.sort(key=lambda item: (_step_index(item), int(item.get("step", 0) or 0)))
    return result


def _is_page_jump(current: Mapping[str, Any], following: Mapping[str, Any]) -> bool:
    current_page = _clean(current.get("page") or current.get("source_page"))
    next_page = _clean(following.get("page") or following.get("source_page"))
    return bool(current_page and next_page and current_page != next_page)


def _transition_claims_navigation(action: Mapping[str, Any]) -> bool:
    values = [
        action.get("transition_kind"),
        action.get("next_transition_kind"),
    ]
    return any(_norm(value) in {"navigation", "route", "page_change"} for value in values)


def _action_explains_page_jump(action: Mapping[str, Any]) -> bool:
    action_type = _norm(action.get("action") or action.get("operation"))
    if action_type in _NAVIGATION_ACTIONS:
        return True
    description = _norm(action.get("description") or action.get("target_element"))
    if action_type == "system_button":
        system_target = _norm(action.get("target_element"))
        return system_target in {
            "back",
            "browser_back",
            "home",
            "recent_apps",
            "app_switcher",
        } or bool(
            re.search(
                r"\b(?:target|button)\s*=\s*(?:back|home|recent_apps|app_switcher)\b",
                description,
            )
        )
    if any(token in description for token in ("点击", "tap", "click", "返回", "关闭", "提交", "确认")):
        return True
    return False


def _is_terminal_summary(action: Mapping[str, Any]) -> bool:
    action_type = _norm(action.get("action") or action.get("operation"))
    if action_type in _TERMINAL_ACTIONS:
        return True
    description = _clean(action.get("description"))
    return bool(re.search(r"(结束任务|任务完成|已完成|terminate|task done)", description, flags=re.I))


def _has_meaningful_text_write_value(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return True
    return False


def _has_text_write_payload(action: Mapping[str, Any]) -> bool:
    payloads: List[Mapping[str, Any]] = [action]
    nested_payload = action.get("payload")
    if isinstance(nested_payload, Mapping):
        payloads.append(nested_payload)
    return any(
        _has_meaningful_text_write_value(payload.get(key))
        for payload in payloads
        for key in _TEXT_WRITE_PAYLOAD_KEYS
    )


def _description_expresses_text_entry(description: str) -> bool:
    text = _norm(description)
    if not text:
        return False

    chinese_text_entry = re.search(
        r"(?:输入|填写|键入|录入|填入|写入)"
        r"(?!框|栏|法|控件|区域|字段|按钮|组件|页面)",
        text,
    )
    english_text_entry = (
        re.search(r"\btype(?:d|s|ing)?\b", text)
        or re.search(r"\bfill(?:ed|s|ing)?(?:\s+(?:in|out))?\b", text)
        or re.search(
            r"\benter(?:ed|s|ing)?\b.{0,48}"
            r"\b(?:text|query|keyword|value|name|address|comment|message|search term)\b",
            text,
        )
        or re.search(
            r"\binput(?:ted|ting)?\b(?!\s+(?:field|box|control|area|component))",
            text,
        )
    )
    return bool(chinese_text_entry or english_text_entry)


def _description_expresses_known_non_query_action(description: str) -> bool:
    text = _norm(description)
    if not text:
        return False
    return bool(
        re.search(r"(点击|轻触|点按|滑动|滚动|拖动|返回|打开|关闭|提交|确认|选择|终止)", text)
        or re.search(
            r"\b(?:click|tap|scroll|swipe|drag|back|open|close|submit|select|terminate)\b",
            text,
        )
    )


def classify_query_draft_action(action: Mapping[str, Any]) -> QueryDraftActionClass:
    action_type = _norm(action.get("action") or action.get("operation"))
    if action_type in _QUERY_DRAFT_ACTIONS:
        return QueryDraftActionClass.QUERY_DRAFT
    if action_type in _KNOWN_NON_QUERY_ACTIONS:
        return QueryDraftActionClass.NOT_QUERY_DRAFT
    if _has_text_write_payload(action):
        return QueryDraftActionClass.QUERY_DRAFT

    description = _norm(action.get("description"))
    if _description_expresses_text_entry(description):
        return QueryDraftActionClass.QUERY_DRAFT
    if _description_expresses_known_non_query_action(description):
        return QueryDraftActionClass.NOT_QUERY_DRAFT
    return QueryDraftActionClass.UNKNOWN


def _is_query_draft_action(action: Mapping[str, Any]) -> bool:
    return classify_query_draft_action(action) == QueryDraftActionClass.QUERY_DRAFT


def _has_query_draft_state_evidence(
    current: Mapping[str, Any],
    following: Mapping[str, Any],
) -> bool:
    structured_state_text = _norm(
        _joined_text(
            current.get("state_effect"),
            current.get("effect_kind"),
            current.get("post_action_state_effect"),
            current.get("expected_state_delta"),
            current.get("runtime_state"),
        )
    )
    if _contains_any(
        structured_state_text,
        ("query_draft", "update_query_draft", "query_projection", "autocomplete"),
    ):
        return True

    after_text = _norm(
        _joined_text(
            current.get("next_description"),
            following.get("current_description"),
        )
    )
    search_field_visible = _contains_any(
        after_text,
        (
            "搜索框",
            "搜索输入",
            "search box",
            "search field",
            "search bar",
            "search input",
        ),
    )
    query_value_visible = _contains_any(
        after_text,
        (
            "已输入",
            "输入值",
            "搜索词",
            "查询词",
            "包含查询",
            "containing the query",
            "contains the query",
            "pre-filled",
            "prefilled",
            "filled with",
            "query value",
        ),
    )
    active_projection_visible = _contains_any(
        after_text,
        (
            "autocomplete",
            "suggestion",
            "suggestions",
            "联想",
            "搜索建议",
            "候选",
            "匹配结果",
            "search results",
            "result list",
        ),
    )
    return bool(search_field_visible and (query_value_visible or active_projection_visible))


def _is_query_draft_action_with_state_evidence(
    current: Mapping[str, Any],
    following: Mapping[str, Any],
) -> bool:
    classification = classify_query_draft_action(current)
    if classification == QueryDraftActionClass.QUERY_DRAFT:
        return True
    if classification == QueryDraftActionClass.NOT_QUERY_DRAFT:
        return False
    return _has_query_draft_state_evidence(current, following)


def _is_scroll_action(action: Mapping[str, Any]) -> bool:
    action_type = _norm(action.get("action") or action.get("operation"))
    if action_type in {"scroll", "swipe", "drag"}:
        return True
    description = _norm(action.get("description"))
    return any(token in description for token in ("滑动", "滚动", "scroll", "swipe", "drag"))


def _is_same_general_page_scroll_projection(
    current: Mapping[str, Any],
    following: Mapping[str, Any],
) -> bool:
    current_cluster = _norm(current.get("page_cluster_id"))
    following_cluster = _norm(following.get("page_cluster_id"))
    if not current_cluster or current_cluster != following_cluster:
        return False
    transition_kind = _norm(
        current.get("transition_kind") or current.get("next_transition_kind")
    )
    if transition_kind not in {"same_page_scroll", "same_page_reveal"}:
        return False
    return _is_scroll_action(current)


def _looks_like_query_result_page(value: Any) -> bool:
    text = _norm(value)
    return any(token in text for token in _QUERY_RESULT_TOKENS)


def _joined_text(*values: Any) -> str:
    return " ".join(_clean(value) for value in values if _clean(value))


def _contains_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def _is_gesture_route_action(current: Mapping[str, Any], following: Mapping[str, Any]) -> bool:
    if not _is_scroll_action(current):
        return False
    action_text = _norm(
        _joined_text(
            current.get("description"),
            current.get("target_element"),
            current.get("action"),
        )
    )
    combined_text = _norm(
        _joined_text(
            current.get("description"),
            current.get("target_element"),
            current.get("current_description"),
            current.get("next_description"),
            following.get("description"),
            following.get("current_description"),
            following.get("page"),
        )
    )
    horizontal_evidence = _contains_any(
        action_text,
        ("向左", "向右", "左滑", "右滑", "swipe left", "swipe right", "horizontal"),
    )
    carousel_or_launcher_evidence = _contains_any(
        combined_text,
        (
            "功能宫格",
            "应用宫格",
            "快捷入口",
            "shortcuts",
            "carousel",
            "pager",
            "分页",
            "page indicator",
            "全部应用",
            "all apps",
            "launcher",
            "精选应用",
            "出行服务",
            "出行向导",
        ),
    )
    route_surface_evidence = _contains_any(
        combined_text,
        ("全部应用", "all apps", "standalone", "独立", "返回箭头", "back arrow", "页面标题"),
    )
    return bool(horizontal_evidence and carousel_or_launcher_evidence and route_surface_evidence)


def _is_sheet_detail_expansion_gesture(current: Mapping[str, Any], following: Mapping[str, Any]) -> bool:
    if not _is_scroll_action(current):
        return False
    action_text = _norm(
        _joined_text(
            current.get("description"),
            current.get("target_element"),
            current.get("action"),
            current.get("operation"),
        )
    )
    current_text = _norm(
        _joined_text(
            current.get("description"),
            current.get("target_element"),
            current.get("current_description"),
            current.get("next_description"),
        )
    )
    next_text = _norm(
        _joined_text(
            current.get("next_description"),
            following.get("description"),
            following.get("current_description"),
            following.get("page"),
        )
    )

    vertical_evidence = _contains_any(
        action_text,
        ("向上", "上滑", "上拉", "swipe up", "drag up", "slide up", "vertical"),
    )
    sheet_evidence = _contains_any(
        current_text,
        (
            "弹窗",
            "模态",
            "浮层",
            "半屏",
            "抽屉",
            "底部弹出",
            "暗化",
            "遮罩",
            "modal",
            "overlay",
            "popup",
            "bottom sheet",
            "sheet",
            "drawer",
        ),
    )
    current_detail_evidence = _contains_any(
        current_text,
        (
            "详情弹窗",
            "详情页",
            "商品详情",
            "product detail",
            "detail sheet",
            "价格",
            "price",
            "商品标题",
            "product title",
        ),
    )
    next_detail_evidence = _contains_any(
        next_text,
        (
            "详情页",
            "商品详情",
            "product detail page",
            "detail page",
            "product title",
            "商品标题",
        ),
    )
    standalone_detail_evidence = _contains_any(
        next_text,
        (
            "tab bar",
            "标签",
            "返回箭头",
            "back arrow",
            "bottom action bar",
            "操作栏",
            "立即购买",
            "standalone",
        ),
    )
    return bool(
        vertical_evidence
        and sheet_evidence
        and current_detail_evidence
        and next_detail_evidence
        and standalone_detail_evidence
    )


def _is_active_query_projection(current: Mapping[str, Any], following: Mapping[str, Any]) -> bool:
    if not _is_query_draft_action_with_state_evidence(current, following):
        return False
    combined_text = _norm(
        _joined_text(
            current.get("description"),
            current.get("target_element"),
            current.get("current_description"),
            current.get("next_description"),
            following.get("description"),
            following.get("current_description"),
            following.get("page"),
        )
    )
    search_surface_evidence = _contains_any(
        combined_text,
        (
            "搜索框",
            "搜索输入",
            "search box",
            "search field",
            "search bar",
            "输入框",
        ),
    ) and _contains_any(
        combined_text,
        ("取消", "清除", "clear", "已聚焦", "active search", "包含"),
    )
    projection_evidence = _contains_any(
        combined_text,
        (
            "搜索结果",
            "结果列表",
            "filtered results",
            "query-result",
            "search result",
            "候选",
            "匹配",
            "查看更多",
            "城市",
            "火车站",
            "station matches",
        ),
    )
    return bool(search_surface_evidence and projection_evidence)


def _is_interactive_query_results_projection(current: Mapping[str, Any], following: Mapping[str, Any]) -> bool:
    if not _is_query_draft_action_with_state_evidence(current, following):
        return False
    if _norm(following.get("action") or following.get("operation")) != "click":
        return False
    current_projection_text = _norm(
        _joined_text(
            current.get("next_description"),
            following.get("page"),
            following.get("current_description"),
        )
    )
    following_action_text = _norm(
        _joined_text(
            following.get("description"),
            following.get("target_element"),
        )
    )
    active_search_surface = _contains_any(
        current_projection_text,
        (
            "搜索框",
            "搜索输入",
            "search input",
            "search field",
            "search header",
            "search bar",
        ),
    )
    result_projection = _contains_any(
        current_projection_text,
        (
            "搜索结果",
            "search result interface",
            "search results",
            "result sections",
            "结果列表",
            "list of location matches",
            "location matches",
            "applications",
            "service windows",
            "category tab",
            "category navigation",
            "contacts",
            "documents",
            "departments",
        ),
    )
    selects_result_item = _contains_any(following_action_text, ("点击", "click")) and _contains_any(
        following_action_text,
        (
            "搜索结果",
            "search result",
            "第一个",
            "第一条",
            "first",
            "result",
            "application",
            "service",
            "contact",
            "item",
            "条目",
        ),
    )
    return bool(active_search_surface and result_projection and selects_result_item)


def _is_search_suggestion_projection(current: Mapping[str, Any], following: Mapping[str, Any]) -> bool:
    if not _is_query_draft_action_with_state_evidence(current, following):
        return False
    combined_text = _norm(
        _joined_text(
            current.get("description"),
            current.get("target_element"),
            current.get("current_description"),
            current.get("next_description"),
            following.get("description"),
            following.get("current_description"),
            following.get("page"),
        )
    )
    search_surface_evidence = _contains_any(
        combined_text,
        (
            "搜索框",
            "搜索输入",
            "search box",
            "search field",
            "search bar",
            "输入框",
        ),
    ) and _contains_any(
        combined_text,
        ("清除", "clear", "search button", "搜索按钮", "已聚焦", "focused"),
    )
    suggestion_evidence = _contains_any(
        combined_text,
        (
            "autocomplete",
            "suggestion",
            "suggestions",
            "联想",
            "推荐搜索",
            "猜你想搜",
            "历史记录",
            "热搜",
            "候选",
        ),
    )
    return bool(search_surface_evidence and suggestion_evidence)


def _missing_action_hint(current: Mapping[str, Any], following: Mapping[str, Any]) -> str:
    if _is_query_draft_action(current):
        return "commit query before entering search results"
    text = " ".join(
        _clean(value)
        for value in (
            current.get("description"),
            current.get("next_description"),
            following.get("description"),
            following.get("current_description"),
        )
        if _clean(value)
    )
    if "更多" in text:
        return "click bottom tab 更多"
    if "搜索" in text or "结果" in text:
        return "commit query before entering search results"
    if "返回" in text:
        return "execute back/return action before next page"
    if "完成" in text or "保存" in text:
        return "execute save/done action before terminal or next page"
    return "add or correct the missing state-changing action in source trace"


def _issue(
    *,
    issue_code: str,
    trace_id: str,
    current: Mapping[str, Any],
    following: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "issue_code": issue_code,
        "severity": "blocking",
        "trace_id": trace_id,
        "from_step": _step_index(current),
        "to_step": _step_index(following),
        "from_page": _clean(current.get("page") or current.get("source_page")),
        "to_page": _clean(following.get("page") or following.get("source_page")),
        "observed_action": _clean(current.get("action") or current.get("operation")),
        "description": _clean(current.get("description")),
        "next_description": _clean(current.get("next_description")),
        "missing_action_hint": _missing_action_hint(current, following),
        "admission": "blocked",
    }


def _classify_pair(trace_id: str, current: Mapping[str, Any], following: Mapping[str, Any]) -> Dict[str, Any] | None:
    if not _is_page_jump(current, following):
        return None
    if _is_terminal_summary(current):
        return _issue(
            issue_code="terminal_summary_carries_unexecuted_action",
            trace_id=trace_id,
            current=current,
            following=following,
        )
    if _is_same_general_page_scroll_projection(current, following):
        return None
    if _is_gesture_route_action(current, following):
        return None
    if _is_sheet_detail_expansion_gesture(current, following):
        return None
    if _is_search_suggestion_projection(current, following):
        return None
    if _is_query_draft_action_with_state_evidence(current, following) and (
        _looks_like_query_result_page(following.get("page"))
        or _looks_like_query_result_page(following.get("current_description"))
    ):
        if _is_active_query_projection(current, following) or _is_interactive_query_results_projection(current, following):
            return None
        return _issue(
            issue_code="query_commit_missing",
            trace_id=trace_id,
            current=current,
            following=following,
        )
    if _action_explains_page_jump(current):
        return None
    if _transition_claims_navigation(current) and not _is_scroll_action(current):
        return _issue(
            issue_code="action_lowering_missing",
            trace_id=trace_id,
            current=current,
            following=following,
        )
    return _issue(
        issue_code="source_trace_action_gap",
        trace_id=trace_id,
        current=current,
        following=following,
    )


def build_source_trace_integrity_report(
    capture_actions: Mapping[str, Iterable[Mapping[str, Any]]],
) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    traces: Dict[str, Dict[str, Any]] = {}

    for trace_id, raw_actions in sorted((capture_actions or {}).items()):
        trace_key = _clean(trace_id)
        if not trace_key:
            continue
        actions = _trace_actions(raw_actions)
        trace_issues: List[Dict[str, Any]] = []
        for index, action in enumerate(actions[:-1]):
            issue = _classify_pair(trace_key, action, actions[index + 1])
            if issue:
                trace_issues.append(issue)
                issues.append(issue)
        admission = "blocked" if trace_issues else "passed"
        traces[trace_key] = {
            "trace_id": trace_key,
            "admission": admission,
            "issue_count": len(trace_issues),
            "blocking_issue_count": len(trace_issues),
            "issues": trace_issues,
        }

    blocked = [item for item in traces.values() if item.get("admission") == "blocked"]
    admission = {
        "format": "source_trace_action_chain_admission",
        "schema_revision": 1,
        "summary": {
            "trace_count": len(traces),
            "passed_trace_count": len(traces) - len(blocked),
            "blocked_trace_count": len(blocked),
            "blocking_issue_count": len(issues),
        },
        "traces": traces,
    }
    invalid_manifest = {
        "format": "invalid_trace_manifest",
        "schema_revision": 1,
        "invalid_trace_count": len(blocked),
        "invalid_traces": blocked,
    }
    return {
        "format": "source_trace_integrity_report",
        "schema_revision": 1,
        "summary": {
            "trace_count": len(traces),
            "passed_trace_count": len(traces) - len(blocked),
            "blocked_trace_count": len(blocked),
            "blocking_issue_count": len(issues),
        },
        "issues": issues,
        "admission": admission,
        "invalid_trace_manifest": invalid_manifest,
    }


def source_trace_admission_hard_gate_enabled(mode: Any) -> bool:
    normalized = _norm(mode)
    if not normalized:
        normalized = DEFAULT_SOURCE_TRACE_ACTION_CHAIN_ADMISSION_MODE
    return normalized in {"hard", "strict", "block", "blocking", "true", "1", "yes"}


def should_block_source_trace_admission(mode: Any, report: Mapping[str, Any]) -> bool:
    if not source_trace_admission_hard_gate_enabled(mode):
        return False
    try:
        blocked_trace_count = int(report.get("summary", {}).get("blocked_trace_count", 0))
    except Exception:
        blocked_trace_count = 0
    return blocked_trace_count > 0


def write_source_trace_integrity_artifacts(
    output_dir: str | Path,
    report: Mapping[str, Any],
    *,
    phase: str = "global_reasoning",
    debug_subdir: str = "source_trace_integrity",
) -> Dict[str, str]:
    base = Path(output_dir) / f"phase_{phase}" / "debug" / debug_subdir
    base.mkdir(parents=True, exist_ok=True)
    payloads: Dict[str, Any] = {
        "source_trace_integrity_report.json": dict(report),
        "action_chain_admission.json": report.get("admission", {}),
        "invalid_trace_manifest.json": report.get("invalid_trace_manifest", {}),
    }
    written: Dict[str, str] = {}
    for filename, payload in payloads.items():
        path = base / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written[filename] = str(path)
    return written
