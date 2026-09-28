
from __future__ import annotations

import html
import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from trace2world_pipeline.app_spec.provenance import (
    PROVENANCE_KEY,
    Provenance,
    attach_provenance,
)


OPEN_ENDED_COMPONENT_MARKERS = (
    "feed",
    "list",
    "results",
    "result",
    "menu",
    "collection",
    "timeline",
    "stream",
)

FIXED_CAPACITY_COMPONENT_MARKERS = (
    "grid",
    "banner",
    "card_slot",
    "slot",
    "tab",
    "nav",
    "navigation",
    "rank",
    "carousel",
)

TEXT_FIELD_HINTS = (
    "name",
    "title",
    "display_text",
    "place_name",
    "full_address",
    "address",
    "address_detail",
    "merchant",
    "merchant_name",
    "store_name",
    "brand_name",
    "keyword",
    "search_query",
    "search_placeholder",
    "city_name",
    "active_quick_filter",
    "selected_filter",
    "filter",
    "label",
    "query_text",
    "quick_filters",
    "quick_filters_visible",
    "visible_filter_chips",
    "sub_tab_selected",
)

TEXT_FIELD_PREFIX_HINTS = (
    "filter_",
)

NON_VISUAL_EVIDENCE_SOURCES = {
    "action",
    "agent_action",
    "trace_action",
    "task_action",
    "task_intent",
    "intent",
    "instruction",
    "thought",
    "agent_thought",
    "reasoning",
}

NOISE_TEXT_VALUES = {
    "true",
    "false",
    "是",
    "否",
    "搜索",
}

CONTROL_ENTITY_MARKERS = (
    "filter",
    "sort",
    "tab",
    "select",
)

CONTROL_ROLES = (
    "filter_condition",
    "selectable_task_state",
)

EXECUTION_ANCHOR_ROLES = (
    "execution_anchor",
    "action_anchor",
    "trace_anchor",
    "interactive_anchor",
    "click_target",
    "state_display_required",
)

QUERY_STATE_MARKERS = (
    "search",
    "query",
)

QUERY_PARAM_ALIASES = ("q", "query", "keyword", "search", "kw")

CONTENT_DATA_ITEM_SECTIONS = ("extracted_items", "runtime_items")

CONTENT_DATA_METADATA_KEYS = {
    "_metadata",
    "description",
    "entity_component_mapping",
    "entity_task_relevance",
    "excluded_from_initial",
    "item_lifecycle",
    "runtime_items",
    "runtime_state_obligations",
}

STATEFUL_DEPENDENCY_FIELDS = {
    "city_name": "current_city",
    "current_city": "current_city",
    "selected_city": "current_city",
    "search_scope": "search_scope",
    "scope": "search_scope",
}


STATE_REF_DOMAIN_MARKERS = {
    "city": ("city", "城市", "市"),
    "address": ("address", "地址", "址"),
    "delivery": ("address", "地址", "收货"),
    "filter": ("filter", "筛选", "过滤"),
    "invoice": ("invoice", "发票", "抬头"),
    "cart": ("cart", "购物车", "数量", "商品"),
}


def _state_ref_domain_markers_enabled() -> bool:

    raw = os.environ.get("T2W_STATE_REF_DOMAIN_MARKERS_ENABLED", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}

STATEFUL_SURFACE_ACTIONS = {
    "select_candidate",
    "select_option",
    "select_existing_option",
    "select_record",
    "apply_filter",
    "update_filter",
    "set_filter",
    "change_state",
    "commit_state",
}

STATEFUL_SDK_EFFECTS = {
    "select",
    "apply",
    "set",
    "update",
    "filter",
    "choose",
}


def _dedupe(values: Iterable[str], limit: int = 64) -> List[str]:
    seen = set()
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


def _is_required_text_candidate(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or text in NOISE_TEXT_VALUES:
        return False
    if len(text) <= 1:
        return False
    if text.startswith("不同截图"):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def _extract_quoted_values(text: str) -> List[str]:
    source = str(text or "")
    values = []

    def clean(value: str) -> str:
        cleaned = str(value or "").strip()
        cleaned = re.sub(r"[（(][^）)]{0,24}[）)]", "", cleaned).strip()
        cleaned = re.sub(
            r"^(?:(?:我现在要|现在要|之后|点击|右上角|黄色|按钮|执行|进行|操作|获取|查找|找到|进入|并进入|对应的|相关的|以|搜索)+)",
            "",
            cleaned,
        ).strip()
        cleaned = re.sub(r"(相关.*|类商家.*|商家.*|品牌馆.*|结果.*|内容.*|的操作.*|搜索页.*|搜索结果.*|搜索)$", "", cleaned).strip()
        if any(marker in cleaned for marker in ("搜索词", "关键词", "输入态", "按钮", "提交", "导航", "跳转", "入口", "页面", "搜索框")):
            return ""
        return cleaned

    for value in re.findall(r"[“\"']([^”\"']{1,48})[”\"']", source):
        cleaned = clean(value)
        if _is_required_text_candidate(cleaned):
            values.append(cleaned)
    for value in re.findall(
        r"(?:query|keyword|关键词|搜索词)\s*[=:：]\s*([A-Za-z0-9\u4e00-\u9fff]{2,48})",
        source,
        flags=re.IGNORECASE,
    ):
        cleaned = clean(value)
        if _is_required_text_candidate(cleaned):
            values.append(cleaned)
    for value in re.findall(
        r"(?:搜索(?!框|按钮|页|词|输入|操作|结果)|输入(?!框|页|态|的))\s*([A-Za-z0-9\u4e00-\u9fff]{2,48})",
        source,
        flags=re.IGNORECASE,
    ):
        cleaned = clean(value)
        if _is_required_text_candidate(cleaned):
            values.append(cleaned)
    for value in re.findall(
        r"(?:获取|查找|搜索|输入|进入|并进入)?([A-Za-z0-9\u4e00-\u9fff]{2,24})(?:相关|品牌馆|搜索结果|结果页)",
        source,
        flags=re.IGNORECASE,
    ):
        cleaned = clean(value)
        if _is_required_text_candidate(cleaned):
            values.append(cleaned)
    return _dedupe(values, limit=8)


def _text_mentions_query_state(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("search", "query", "keyword", "搜索", "输入", "关键词"))


def _contract_mentions_query_state(contract: Any) -> bool:
    if not isinstance(contract, dict):
        return False
    fields = [
        contract.get("action_semantic"),
        contract.get("interaction_outcome"),
        contract.get("state_ref"),
        contract.get("entity_ref"),
        contract.get("rationale"),
    ]
    for nested_key in ("route_binding", "hydration", "sdk_effect"):
        nested = contract.get(nested_key)
        if isinstance(nested, dict):
            fields.extend(nested.values())
    return any(_text_mentions_query_state(str(field or "")) for field in fields)


def _query_values_from_contract_texts(*texts: Any) -> List[str]:
    values: List[str] = []
    for text in texts:
        source = str(text or "")
        if not source or not _text_mentions_query_state(source):
            continue
        values.extend(_extract_quoted_values(source))
    return _dedupe(values, limit=8)


def _query_values_from_continuation(continuation: Dict[str, Any]) -> List[str]:
    if not isinstance(continuation, dict):
        return []
    contract = continuation.get("interaction_contract")
    texts: List[Any] = [
        continuation.get("element_name"),
        continuation.get("display_text"),
        continuation.get("action"),
        continuation.get("reason"),
    ]
    if isinstance(contract, dict):
        if _contract_mentions_query_state(contract):
            texts.extend([
                contract.get("action_semantic"),
                contract.get("interaction_outcome"),
                contract.get("state_ref"),
                contract.get("entity_ref"),
            ])
        field_contract = contract.get("field_contract")
        if isinstance(field_contract, dict):
            for field in field_contract.get("typed_fields", []) or []:
                if isinstance(field, dict):
                    for key in ("value", "text", "query", "keyword"):
                        value = field.get(key)
                        if isinstance(value, str) and _is_required_text_candidate(value):
                            texts.append(f"query={value}")
    return _query_values_from_contract_texts(*texts)


def _has_href_target(html_text: str, target: str) -> bool:
    if not target:
        return False
    escaped = re.escape(target)
    return bool(
        re.search(
            rf"""(?is)(?:\bhref\s*=\s*["'][^"']*{escaped}(?:[#?][^"']*)?["']|\bdata-ghostweb-action\s*=\s*["']navigate["'][^>]*\bdata-ghostweb-target\s*=\s*["'][^"']*{escaped}(?:[#?][^"']*)?["']|\bdata-ghostweb-target\s*=\s*["'][^"']*{escaped}(?:[#?][^"']*)?["'][^>]*\bdata-ghostweb-action\s*=\s*["']navigate["'])""",
            html_text or "",
        )
    )


BACK_NAV_MARKERS = (
    "返回",
    "回到上一级",
    "上一页",
    "返回箭头",
    "关闭",
    "back",
    "close",
)


def _filename_without_query(filename: str) -> str:
    text = str(filename or "").strip()
    text = text.rsplit("/", 1)[-1]
    text = text.split("?", 1)[0].split("#", 1)[0]
    return text


def _filename_family(filename: str) -> str:
    base = _filename_without_query(filename)
    if not base:
        return ""
    stem = base.rsplit(".", 1)[0]
    return stem.split("-", 1)[0] if "-" in stem else stem


def _normalize_route_path(route_path: str) -> str:
    path = str(route_path or "").strip()
    if not path:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"/+", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _route_segments(route_path: str) -> List[str]:
    return [part for part in _normalize_route_path(route_path).split("/") if part]


def _route_namespace(route_meta: Dict[str, Any] | None, filename: str = "") -> str:
    meta = route_meta if isinstance(route_meta, dict) else {}
    segments = _route_segments(str(meta.get("route_path") or ""))
    if segments:
        return segments[0]
    return _filename_family(filename)


def _route_level(route_path: str, value: Any = None) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        level = 0
    if level > 0:
        return level
    return max(0, len(_route_segments(route_path)))


def _is_contextual_back_text(text: str) -> bool:
    raw = str(text or "").strip()
    lowered = str(text or "").lower()
    labels = _extract_quoted_values(text)
    if labels:
        return any(str(label or "").strip().lower() in {"返回", "关闭", "back", "close"} for label in labels)
    if raw in {"返回", "点击返回", "关闭", "点击关闭"}:
        return True
    if re.search(r"^点击[^，,。；;]{0,12}(返回|关闭)(按钮|箭头|图标)?$", raw):
        return True
    return bool(
        re.search(r"(返回箭头|返回按钮|关闭按钮|左上角.*返回|右上角.*关闭|上一页|\\bback\\b|\\bclose\\b)", lowered)
    )


def _is_contextual_back_route_action(action: Dict[str, Any]) -> bool:
    if not isinstance(action, dict):
        return False
    route_binding = action.get("route_binding") if isinstance(action.get("route_binding"), dict) else {}
    sdk_effect = action.get("sdk_effect") if isinstance(action.get("sdk_effect"), dict) else {}
    trigger_text = str(action.get("trigger_text") or "").strip()
    return bool(
        str(route_binding.get("source") or "").lower() == "back_navigation"
        or str(sdk_effect.get("method") or "").lower() == "back"
        or _is_contextual_back_text(trigger_text)
    )


def _filter_contextual_back_namespace_actions(
    source_filename: str,
    source_route_meta: Dict[str, Any],
    actions: List[Dict[str, Any]],
    route_meta_by_filename: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not actions:
        return []
    source_namespace = _route_namespace(source_route_meta, source_filename)
    if not source_namespace:
        return actions
    filtered: List[Dict[str, Any]] = []
    for action in actions:
        target_filename = _filename_without_query(str(action.get("target_filename") or ""))
        target_meta = route_meta_by_filename.get(target_filename, {}) if isinstance(route_meta_by_filename, dict) else {}
        target_namespace = _route_namespace(target_meta, target_filename)
        if (
            target_filename
            and target_namespace
            and target_namespace != source_namespace
            and _is_contextual_back_route_action(action)
        ):
            continue
        filtered.append(action)
    return filtered


def _same_family_fallback_from_filenames(filename: str, filenames: Iterable[str], original_target: str = "") -> str:
    family = _filename_family(filename)
    original = _filename_without_query(original_target)
    current = _filename_without_query(filename)

    if original and original == current:
        return ""

    def is_home_like(candidate: str) -> bool:
        stem = candidate.rsplit(".", 1)[0]
        return (
            candidate == f"{family}.html"
            or candidate == "home.html"
            or candidate == "index.html"
            or stem == f"{family}-home"
            or stem.startswith(f"{family}-{family}_home")
            or stem.startswith(f"{family}-{family}-home")
        )

    if original and family and _filename_family(original) == family and is_home_like(original):
        return original

    same_family = [
        _filename_without_query(candidate)
        for candidate in filenames
        if _filename_without_query(candidate)
        and _filename_without_query(candidate) != current
        and _filename_family(candidate) == family
    ]
    if not same_family:
        return original
    home_like = [candidate for candidate in same_family if is_home_like(candidate)]
    return sorted(home_like or same_family)[0]


def _as_plain_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            result = value.to_dict()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    return {}


def _route_metadata_for_proto(proto: Any) -> Dict[str, Any]:
    identity = _page_identity(proto)
    sd = getattr(proto, "structure_data", {}) if proto is not None else {}
    if not isinstance(sd, dict):
        sd = {}
    route_path = _normalize_route_path(sd.get("route_path", ""))
    parent_route_path = _normalize_route_path(sd.get("parent_route_path", ""))
    if route_path and not parent_route_path:
        segments = _route_segments(route_path)
        parent_route_path = "/" + "/".join(segments[:-1]) if len(segments) > 1 else "/"
    return {
        "page_id": identity["page_id"],
        "filename": identity["filename"],
        "page_name": identity["page_name"],
        "page_type": identity["page_type"],
        "general_page_id": identity["general_page_id"],
        "materialized_page_id": identity["materialized_page_id"],
        "route_path": route_path,
        "parent_route_path": parent_route_path,
        "route_level": _route_level(route_path, sd.get("route_level")),
    }


def _route_metadata_from_site_graph(structure: Any) -> Dict[str, Dict[str, Any]]:
    site_graph = getattr(structure, "site_graph", None)
    nodes = getattr(site_graph, "nodes", []) if site_graph is not None else []
    if not isinstance(nodes, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        page_id = str(node.get("page_id") or "").strip()
        if not page_id:
            continue
        route_path = _normalize_route_path(node.get("route_path", ""))
        result[page_id] = {
            "page_id": page_id,
            "filename": str(node.get("filename") or ""),
            "page_name": str(node.get("page_name") or ""),
            "page_type": str(node.get("page_type") or ""),
            "route_path": route_path,
            "parent_route_path": _normalize_route_path(node.get("parent_route_path", "")),
            "route_level": _route_level(route_path, node.get("route_level")),
        }
    return result


def _merge_route_metadata(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key in ("route_path", "parent_route_path", "route_level", "page_type", "page_name", "filename"):
        value = overlay.get(key)
        if value not in ("", None, 0):
            merged[key] = value
    if merged.get("route_path") and not merged.get("parent_route_path"):
        segments = _route_segments(str(merged.get("route_path") or ""))
        merged["parent_route_path"] = "/" + "/".join(segments[:-1]) if len(segments) > 1 else "/"
    return merged


def _first_route_segment(meta: Dict[str, Any]) -> str:
    segments = _route_segments(str(meta.get("route_path") or ""))
    return segments[0] if segments else ""


def _is_global_home_route(meta: Dict[str, Any]) -> bool:
    route_path = str(meta.get("route_path") or "")
    filename = _filename_without_query(str(meta.get("filename") or ""))
    page_type = str(meta.get("page_type") or "").lower()
    return route_path in {"/home", "/"} or filename == "home.html" or (
        page_type == "homepage" and _route_level(route_path, meta.get("route_level")) <= 1
    )


def _is_section_landing(meta: Dict[str, Any], section: str) -> bool:
    route_path = str(meta.get("route_path") or "")
    segments = _route_segments(route_path)
    if not section or not segments or segments[0] != section:
        return False
    if len(segments) >= 2 and segments[1] == "home":
        return True
    page_type = str(meta.get("page_type") or "").lower()
    text = " ".join([
        str(meta.get("filename") or "").lower(),
        str(meta.get("page_name") or "").lower(),
        str(meta.get("general_page_id") or "").lower(),
        str(meta.get("materialized_page_id") or "").lower(),
    ])
    return page_type == "homepage" and ("home" in text or "首页" in text)


def _section_landing_rank(meta: Dict[str, Any], section: str, source_is_section_landing: bool) -> int:
    if source_is_section_landing:
        return 0 if _is_global_home_route(meta) else 10
    filename = _filename_without_query(str(meta.get("filename") or "")).lower()
    route_path = str(meta.get("route_path") or "").lower()
    materialized = str(meta.get("materialized_page_id") or "").lower()
    instance_key = materialized.split("::", 1)[1] if "::" in materialized else ""
    page_name = str(meta.get("page_name") or "").lower()
    primary_markers = (
        f"{section}_home",
        f"{section}-home",
        f"{section}_{section}_home",
        f"{section}-{section}_home",
    )
    if any(marker in route_path or marker in filename or marker in instance_key for marker in primary_markers):
        return 0
    if "首页" in page_name and not any(marker in route_path or marker in filename for marker in ("nearby", "search", "result", "list")):
        return 1
    if any(marker in route_path or marker in filename for marker in ("nearby", "search", "result", "list")):
        return 5
    return 3


def _back_proof_for_route_fallback(source: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    if not source or not target:
        return {}
    source_route = str(source.get("route_path") or "")
    target_route = str(target.get("route_path") or "")
    source_segments = _route_segments(source_route)
    target_segments = _route_segments(target_route)
    source_level = _route_level(source_route, source.get("route_level"))
    target_level = _route_level(target_route, target.get("route_level"))
    section = _first_route_segment(source)
    source_is_section_landing = _is_section_landing(source, section)
    if source_is_section_landing and _is_global_home_route(target):
        proof_kind = "global_home"
    elif _is_section_landing(target, section):
        proof_kind = "section_landing"
    elif target_level < source_level and target_segments and source_segments[:len(target_segments)] == target_segments:
        proof_kind = "route_parent"
    else:
        return {}
    return {
        "proof_kind": proof_kind,
        "source_route_path": source_route,
        "target_route_path": target_route,
    }


def _stack_navigation_fallback_policy(
    identity: Dict[str, str],
    route_meta_by_filename: Dict[str, Dict[str, Any]],
    fallback_by_filename: Dict[str, str],
) -> Dict[str, Any]:
    filename = _filename_without_query(identity.get("filename", ""))
    source = route_meta_by_filename.get(filename, {})
    source_route = str(source.get("route_path") or "")
    if not filename or not source_route:
        return {}

    section = _first_route_segment(source)
    source_is_section_landing = _is_section_landing(source, section)

    candidates: List[Dict[str, Any]] = []
    if source_is_section_landing:
        candidates = [
            meta for candidate_filename, meta in route_meta_by_filename.items()
            if candidate_filename != filename and _is_global_home_route(meta)
        ]
    elif section:
        candidates = [
            meta for candidate_filename, meta in route_meta_by_filename.items()
            if candidate_filename != filename and _is_section_landing(meta, section)
        ]

    if not candidates:
        return {}

    def sort_key(meta: Dict[str, Any]) -> Tuple[int, int, str]:
        route_path = str(meta.get("route_path") or "")
        route_level = _route_level(route_path, meta.get("route_level"))
        filename_text = _filename_without_query(str(meta.get("filename") or ""))
        preferred = _section_landing_rank(meta, section, source_is_section_landing)
        if source_is_section_landing and filename_text == "home.html":
            preferred = -1
        return (preferred, route_level, filename_text)

    target = sorted(candidates, key=sort_key)[0]
    target_filename = _filename_without_query(str(target.get("filename") or ""))
    if not target_filename:
        return {}
    target_route = str(target.get("route_path") or "")
    back_proof = _back_proof_for_route_fallback(source, target)
    if not back_proof:
        return {}
    fallback_by_filename[filename] = target_filename
    return {
        "strategy": "history_first",
        "fallback_target_filename": target_filename,
        "fallback_target_page_id": str(target.get("page_id") or ""),
        "fallback_route_path": target_route,
        "source_route_path": source_route,
        "fallback_source": "route_hierarchy",
        "monotonic": True,
        "back_proof": back_proof,
    }


def _navigation_policy_for_page(
    identity: Dict[str, str],
    behavior: Any,
    filename_by_page_id: Dict[str, str],
    all_filenames: Iterable[str],
    route_meta_by_filename: Dict[str, Dict[str, Any]],
    fallback_by_filename: Dict[str, str],
) -> Dict[str, Any]:
    behavior_dict = _as_plain_dict(behavior)
    if not behavior_dict:
        return {}

    generic_targets: List[Dict[str, Any]] = []

    def add_target(trigger: str, target_filename: str, source: str) -> None:
        trigger_text = str(trigger or "").strip()
        target = _filename_without_query(target_filename)
        if not trigger_text or not target or not _is_contextual_back_text(trigger_text):
            return
        generic_targets.append(
            {
                "trigger": trigger_text,
                "target_filename": target,
                "source": source,
                "contextual": True,
            }
        )

    for nav in behavior_dict.get("navigation_out", []) if isinstance(behavior_dict.get("navigation_out", []), list) else []:
        if not isinstance(nav, dict):
            continue
        target = nav.get("to_filename") or filename_by_page_id.get(str(nav.get("to_page_id") or ""))
        add_target(str(nav.get("trigger") or ""), str(target or ""), "navigation_out")

    for continuation in behavior_dict.get("task_continuations", []) if isinstance(behavior_dict.get("task_continuations", []), list) else []:
        if not isinstance(continuation, dict):
            continue
        trigger = continuation.get("element_name") or continuation.get("display_text") or continuation.get("action")
        target = continuation.get("target_filename") or filename_by_page_id.get(str(continuation.get("target_page_id") or ""))
        add_target(str(trigger or ""), str(target or ""), "task_continuation")

    deduped = []
    seen = set()
    for item in generic_targets:
        key = (item.get("trigger"), item.get("target_filename"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    if not deduped:
        return {}
    stack_policy = _stack_navigation_fallback_policy(identity, route_meta_by_filename, fallback_by_filename)
    fallback = str(stack_policy.get("fallback_target_filename") or "").strip()
    if not fallback:
        fallback = _same_family_fallback_from_filenames(
            identity.get("filename", ""),
            all_filenames,
            deduped[0].get("target_filename", ""),
        )
        source_filename = _filename_without_query(str(identity.get("filename") or ""))
        source_meta = route_meta_by_filename.get(source_filename, {})
        target_meta = route_meta_by_filename.get(fallback, {})
        back_proof = _back_proof_for_route_fallback(source_meta, target_meta)
        if fallback and back_proof:
            stack_policy = {
                "strategy": "history_first",
                "fallback_target_filename": fallback,
                "fallback_target_page_id": str(target_meta.get("page_id") or ""),
                "fallback_route_path": str(target_meta.get("route_path") or ""),
                "source_route_path": str(source_meta.get("route_path") or ""),
                "fallback_source": "route_hierarchy",
                "monotonic": True,
                "back_proof": back_proof,
            }
        else:
            fallback = ""
    history_hint = deduped[0] if not fallback and deduped else {}
    return {
        "prefer_history_for_generic_back": True,
        "generic_back_targets": deduped,
        "safe_same_family_fallback": fallback,
        **(
            {
                "backtrack_mode": "history_only_hint",
                "history_only_hint": history_hint,
            }
            if history_hint else {}
        ),
        **({"stack_navigation_policy": stack_policy} if stack_policy else {}),
    }


def _route_execution_contract_for_page(
    identity: Dict[str, str],
    route_meta: Dict[str, Any],
    navigation_policy: Dict[str, Any],
    behavior: Any,
    filename_by_page_id: Dict[str, str],
    page_name_by_page_id: Dict[str, str],
    route_meta_by_filename: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    policy = navigation_policy if isinstance(navigation_policy, dict) else {}
    stack_policy = policy.get("stack_navigation_policy") if isinstance(policy.get("stack_navigation_policy"), dict) else {}
    target = str(
        (stack_policy or {}).get("fallback_target_filename")
        or policy.get("safe_same_family_fallback")
        or ""
    ).strip()
    commit_actions = _route_commit_actions_for_behavior(behavior, filename_by_page_id, page_name_by_page_id)
    navigation_actions = _filter_contextual_back_namespace_actions(
        identity.get("filename", ""),
        route_meta,
        _route_navigation_actions_for_behavior(behavior, filename_by_page_id, page_name_by_page_id),
        route_meta_by_filename or {},
    )
    stateful_actions = _route_stateful_actions_for_behavior(behavior)
    contract: Dict[str, Any] = {
        "format": "route_execution_contract",
        "schema_revision": 1,
        "page_id": identity.get("page_id", ""),
        "filename": identity.get("filename", ""),
        "route_path": route_meta.get("route_path", ""),
        "route_level": route_meta.get("route_level", 0),
        "actions": [],
        "constraints": {
            "raw_history_back_allowed": not bool(target),
            "raw_commit_navigation_allowed": not bool(commit_actions),
            "raw_task_navigation_allowed": not bool(navigation_actions),
            "raw_stateful_surface_allowed": not bool(stateful_actions),
            "runtime_stack_required": bool(target),
        },
    }
    if target:
        target_source = str((stack_policy or {}).get("fallback_source") or "navigation_policy")
        fallback_back = {
            "action_id": "fallback_back",
            "action_kind": "history_back",
            "navigation_kind": "history_back",
            "history_policy": "stack_then_fallback",
            "target_filename": _filename_without_query(target),
            "fallback_target": _filename_without_query(target),
            "target_source": target_source,
            "emits_runtime_event": True,
            "monotonic": bool((stack_policy or {}).get("monotonic", True)),
        }
        back_proof = (stack_policy or {}).get("back_proof")
        if isinstance(back_proof, dict) and back_proof:
            fallback_back["back_proof"] = dict(back_proof)
        contract["fallback_back_action"] = fallback_back
        contract["default_back_action"] = fallback_back
        contract["actions"].append(fallback_back)
    if commit_actions:
        contract["commit_actions"] = commit_actions
        contract["actions"].extend(commit_actions)
    if navigation_actions:
        contract["navigation_actions"] = navigation_actions
        contract["actions"].extend(navigation_actions)
    if stateful_actions:
        contract["stateful_actions"] = stateful_actions
    return contract


def _stable_route_action_id(prefix: str, seed: str) -> str:
    text = str(seed or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    if safe:
        return f"{prefix}_{safe[:48]}"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _is_commit_interaction_contract(contract: Any) -> bool:
    if not isinstance(contract, dict):
        return False
    action_semantic = str(contract.get("action_semantic") or "").lower()
    if action_semantic in {"commit_transaction", "terminal_submit", "terminal_commit"}:
        return True
    if action_semantic.startswith("commit_"):
        return True
    sdk_effect = contract.get("sdk_effect")
    if isinstance(sdk_effect, dict):
        effect = str(sdk_effect.get("effect") or "").lower()
        method = str(sdk_effect.get("method") or "").lower()
        if "submit" in method or method.endswith("_commit"):
            return True
        if effect in {"submit", "commit"} and not any(marker in method for marker in ("navigate", "open_", "draft_then_navigate")):
            return True
    return False


def _is_stateful_surface_interaction_contract(contract: Any) -> bool:
    if not isinstance(contract, dict):
        return False
    if _is_commit_interaction_contract(contract):
        return False
    action_semantic = str(contract.get("action_semantic") or "").lower()
    if action_semantic in STATEFUL_SURFACE_ACTIONS:
        return True
    sdk_effect = contract.get("sdk_effect")
    if isinstance(sdk_effect, dict):
        effect = str(sdk_effect.get("effect") or "").lower()
        method = str(sdk_effect.get("method") or "").lower()
        if effect in STATEFUL_SDK_EFFECTS and not any(
            marker in method for marker in ("navigate", "open_", "submit", "create")
        ):
            return True
    return False


def _route_stateful_actions_for_behavior(behavior: Any) -> List[Dict[str, Any]]:
    behavior_dict = _as_plain_dict(behavior)
    if not behavior_dict:
        return []
    actions: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    continuations = behavior_dict.get("task_continuations", [])
    if not isinstance(continuations, list):
        return actions

    for continuation in continuations:
        if not isinstance(continuation, dict):
            continue
        interaction_contract = continuation.get("interaction_contract")
        if not _is_stateful_surface_interaction_contract(interaction_contract):
            continue
        trigger_text = str(
            continuation.get("element_name")
            or continuation.get("display_text")
            or continuation.get("action")
            or ""
        ).strip()
        contract_id = ""
        action_semantic = ""
        sdk_effect: Dict[str, Any] = {}
        entity_ref = ""
        state_ref = ""
        state_provenance = ""
        source_lifecycle = ""
        if isinstance(interaction_contract, dict):
            contract_id = str(interaction_contract.get("contract_id") or "").strip()
            action_semantic = str(interaction_contract.get("action_semantic") or "").strip()
            entity_ref = str(interaction_contract.get("entity_ref") or "").strip()
            state_ref = str(interaction_contract.get("state_ref") or "").strip()
            state_provenance = str(interaction_contract.get("state_provenance") or "").strip()
            source_lifecycle = str(interaction_contract.get("source_lifecycle") or "").strip()
            if isinstance(interaction_contract.get("sdk_effect"), dict):
                sdk_effect = dict(interaction_contract.get("sdk_effect") or {})
        key = (contract_id, trigger_text)
        if key in seen:
            continue
        seen.add(key)
        action = {
            "action_id": _stable_route_action_id("stateful", contract_id or trigger_text),
            "interaction_kind": "stateful_surface_action",
            "trigger_text": trigger_text,
            "contract_id": contract_id,
            "action_semantic": action_semantic,
            "state_ref": state_ref,
            "entity_ref": entity_ref,
            "state_provenance": state_provenance,
            "source_lifecycle": source_lifecycle,
            "sdk_effect": sdk_effect,
            "query_values": _query_values_from_continuation(continuation),
        }
        actions.append({
            key: value
            for key, value in action.items()
            if value not in ("", [], {})
        })
    return actions


def _route_commit_actions_for_behavior(
    behavior: Any,
    filename_by_page_id: Dict[str, str],
    page_name_by_page_id: Dict[str, str],
) -> List[Dict[str, Any]]:
    behavior_dict = _as_plain_dict(behavior)
    if not behavior_dict:
        return []
    actions: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()

    continuations = behavior_dict.get("task_continuations", [])
    if not isinstance(continuations, list):
        return actions
    for continuation in continuations:
        if not isinstance(continuation, dict):
            continue
        interaction_contract = continuation.get("interaction_contract")
        if not _is_commit_interaction_contract(interaction_contract):
            continue
        target = continuation.get("target_filename") or filename_by_page_id.get(str(continuation.get("target_page_id") or ""))
        target_filename = _filename_without_query(str(target or ""))
        if not target_filename:
            continue
        target_page_name = str(
            continuation.get("target_page_name")
            or continuation.get("target_page")
            or page_name_by_page_id.get(str(continuation.get("target_page_id") or ""))
            or ""
        ).strip()
        trigger_text = str(
            continuation.get("element_name")
            or continuation.get("display_text")
            or continuation.get("action")
            or ""
        ).strip()
        contract_id = ""
        sdk_effect: Dict[str, Any] = {}
        entity_ref = ""
        state_ref = ""
        source_lifecycle = ""
        state_provenance = ""
        route_binding: Dict[str, Any] = {}
        if isinstance(interaction_contract, dict):
            contract_id = str(interaction_contract.get("contract_id") or "").strip()
            entity_ref = str(interaction_contract.get("entity_ref") or "").strip()
            state_ref = str(interaction_contract.get("state_ref") or "").strip()
            source_lifecycle = str(interaction_contract.get("source_lifecycle") or "").strip()
            state_provenance = str(interaction_contract.get("state_provenance") or "").strip()
            if isinstance(interaction_contract.get("sdk_effect"), dict):
                sdk_effect = dict(interaction_contract.get("sdk_effect") or {})
            if isinstance(interaction_contract.get("route_binding"), dict):
                route_binding = dict(interaction_contract.get("route_binding") or {})
        key = (contract_id, trigger_text, target_filename)
        if key in seen:
            continue
        seen.add(key)
        action_id = _stable_route_action_id("commit", contract_id or f"{trigger_text}:{target_filename}")
        action = {
            "action_id": action_id,
            "navigation_kind": "commit_to_target",
            "target_filename": target_filename,
            "target_source": "task_interaction_contract",
            "trigger_text": trigger_text,
            "history_mode": "replace",
            "monotonic": True,
        }
        if target_page_name:
            action["_target_page_name"] = target_page_name
        if contract_id:
            action["contract_id"] = contract_id
        if entity_ref:
            action["entity_ref"] = entity_ref
        if state_ref:
            action["state_ref"] = state_ref
        if source_lifecycle:
            action["source_lifecycle"] = source_lifecycle
        if state_provenance:
            action["state_provenance"] = state_provenance
        if route_binding:
            action["route_binding"] = route_binding
        if sdk_effect:
            action["sdk_effect"] = sdk_effect
        query_values = _query_values_from_continuation(continuation)
        if query_values:
            action["query_values"] = query_values
        actions.append(action)
    return _filter_ambiguous_route_actions(actions)


def _route_navigation_actions_for_behavior(
    behavior: Any,
    filename_by_page_id: Dict[str, str],
    page_name_by_page_id: Dict[str, str],
) -> List[Dict[str, Any]]:
    behavior_dict = _as_plain_dict(behavior)
    if not behavior_dict:
        return []
    actions: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()

    continuations = behavior_dict.get("task_continuations", [])
    if not isinstance(continuations, list):
        return actions
    for continuation in continuations:
        if not isinstance(continuation, dict):
            continue
        interaction_contract = continuation.get("interaction_contract")
        if _is_commit_interaction_contract(interaction_contract):
            continue
        if str(continuation.get("continuation_kind") or "") != "page_navigation":
            continue
        target = continuation.get("target_filename") or filename_by_page_id.get(str(continuation.get("target_page_id") or ""))
        target_filename = _filename_without_query(str(target or ""))
        if not target_filename:
            continue
        trigger_text = str(
            continuation.get("element_name")
            or continuation.get("display_text")
            or continuation.get("action")
            or ""
        ).strip()
        contract_id = ""
        sdk_effect: Dict[str, Any] = {}
        entity_ref = ""
        state_ref = ""
        source_lifecycle = ""
        state_provenance = ""
        route_binding: Dict[str, Any] = {}
        if isinstance(interaction_contract, dict):
            contract_id = str(interaction_contract.get("contract_id") or "").strip()
            entity_ref = str(interaction_contract.get("entity_ref") or "").strip()
            state_ref = str(interaction_contract.get("state_ref") or "").strip()
            source_lifecycle = str(interaction_contract.get("source_lifecycle") or "").strip()
            state_provenance = str(interaction_contract.get("state_provenance") or "").strip()
            if isinstance(interaction_contract.get("sdk_effect"), dict):
                sdk_effect = dict(interaction_contract.get("sdk_effect") or {})
            if isinstance(interaction_contract.get("route_binding"), dict):
                route_binding = dict(interaction_contract.get("route_binding") or {})
        key = (contract_id, trigger_text, target_filename)
        if key in seen:
            continue
        seen.add(key)
        action_id = _stable_route_action_id("navigate", contract_id or f"{trigger_text}:{target_filename}")
        action = {
            "action_id": action_id,
            "navigation_kind": "navigate_to_target",
            "target_filename": target_filename,
            "target_source": "task_interaction_contract",
            "trigger_text": trigger_text,
            "history_mode": "push",
            "monotonic": True,
        }
        target_page_name = str(
            continuation.get("target_page_name")
            or continuation.get("target_page")
            or page_name_by_page_id.get(str(continuation.get("target_page_id") or ""))
            or ""
        ).strip()
        if target_page_name:
            action["_target_page_name"] = target_page_name
        if contract_id:
            action["contract_id"] = contract_id
        if entity_ref:
            action["entity_ref"] = entity_ref
        if state_ref:
            action["state_ref"] = state_ref
        if source_lifecycle:
            action["source_lifecycle"] = source_lifecycle
        if state_provenance:
            action["state_provenance"] = state_provenance
        if route_binding:
            action["route_binding"] = route_binding
        if sdk_effect:
            action["sdk_effect"] = sdk_effect
        query_values = _query_values_from_continuation(continuation)
        if query_values:
            action["query_values"] = query_values
        actions.append(action)
    return _filter_ambiguous_route_actions(actions)


def _route_action_target_score(action: Dict[str, Any]) -> int:
    trigger_text = str(action.get("trigger_text") or "")
    target_text = " ".join([
        str(action.get("target_filename") or ""),
        str(action.get("target_page_name") or action.get("_target_page_name") or ""),
    ])
    tokens = _dedupe(
        list(action.get("query_values", []) if isinstance(action.get("query_values"), list) else [])
        + _query_values_from_contract_texts(trigger_text)
        + _interaction_tokens(trigger_text),
        limit=16,
    )
    score = 0
    for token in tokens:
        if not token:
            continue
        if token in target_text:
            score += 3
        elif token.lower() in target_text.lower():
            score += 1
    return score


def _filter_ambiguous_route_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for action in actions:
        sdk_effect = action.get("sdk_effect") if isinstance(action.get("sdk_effect"), dict) else {}
        route_binding = action.get("route_binding") if isinstance(action.get("route_binding"), dict) else {}
        query_values = action.get("query_values") if isinstance(action.get("query_values"), list) else []
        scoped_identity = (
            str(action.get("state_ref") or ""),
            str(action.get("entity_ref") or ""),
            str(action.get("source_lifecycle") or ""),
            str(action.get("state_provenance") or ""),
            str(route_binding.get("state_ref") or route_binding.get("source") or ""),
            "|".join(str(value or "") for value in query_values),
        )
        has_scoped_identity = any(scoped_identity)
        key = (
            str(action.get("navigation_kind") or ""),
            str(sdk_effect.get("method") or ""),
            str(action.get("trigger_text") or ""),
            "" if has_scoped_identity else str(action.get("contract_id") or ""),
            *scoped_identity,
        )
        grouped.setdefault(key, []).append(action)

    filtered: List[Dict[str, Any]] = []
    for group in grouped.values():
        targets = {_filename_without_query(str(item.get("target_filename") or "")) for item in group}
        if len(group) <= 1 or len(targets) <= 1:
            filtered.extend(_public_route_action(item) for item in group)
            continue
        scored = [(item, _route_action_target_score(item)) for item in group]
        best = max(score for _, score in scored)
        if best <= 0:
            filtered.extend(_public_route_action(item) for item in group)
            continue
        for item, score in scored:
            if score != best:
                continue
            item = dict(item)
            item["route_target_discriminator"] = {
                "source": "trigger_target_semantic_overlap",
                "score": score,
            }
            filtered.append(_public_route_action(item))
    return filtered


def _public_route_action(action: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in dict(action).items()
        if not str(key).startswith("_")
    }


def _query_values_for_behavior(behavior: Any) -> List[str]:
    behavior_dict = _as_plain_dict(behavior)
    if not behavior_dict:
        return []
    values: List[str] = []
    for action in behavior_dict.get("page_actions", []) if isinstance(behavior_dict.get("page_actions", []), list) else []:
        if not isinstance(action, dict):
            continue
        values.extend(_query_values_from_contract_texts(action.get("description"), action.get("target_element")))
    for nav in behavior_dict.get("navigation_out", []) if isinstance(behavior_dict.get("navigation_out", []), list) else []:
        if not isinstance(nav, dict):
            continue
        values.extend(_query_values_from_contract_texts(nav.get("trigger")))
    for continuation in behavior_dict.get("task_continuations", []) if isinstance(behavior_dict.get("task_continuations", []), list) else []:
        if isinstance(continuation, dict):
            values.extend(_query_values_from_continuation(continuation))
    return _dedupe(values, limit=12)


def _item_text_blob(item: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key, value in item.items():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float)):
            parts.append(str(value))
        elif isinstance(value, list):
            parts.extend(str(v) for v in value if isinstance(v, (str, int, float)))
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, str):
                    parts.append(nested)
                elif isinstance(nested, (int, float)):
                    parts.append(str(nested))
    return " ".join(parts)


def _merge_content_samples(*samples_list: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    merged: Dict[str, List[Dict[str, Any]]] = {}
    seen: Dict[str, set[str]] = {}
    for samples in samples_list:
        if not isinstance(samples, dict):
            continue
        for entity_name, items in samples.items():
            entity = str(entity_name or "").strip()
            if not entity or not isinstance(items, list):
                continue
            bucket = merged.setdefault(entity, [])
            entity_seen = seen.setdefault(entity, set())
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if key in entity_seen:
                    continue
                entity_seen.add(key)
                bucket.append(dict(item))
    return merged


def _content_samples_from_page_content(content_data: Any) -> Dict[str, List[Dict[str, Any]]]:
    if not isinstance(content_data, dict):
        return {}
    samples: Dict[str, List[Dict[str, Any]]] = {}

    def add_items(entity_name: str, items: Any) -> None:
        entity = str(entity_name or "").strip()
        if not entity or not isinstance(items, list):
            return
        records = [dict(item) for item in items if isinstance(item, dict)]
        if records:
            samples.setdefault(entity, []).extend(records)

    for section in CONTENT_DATA_ITEM_SECTIONS:
        section_items = content_data.get(section)
        if not isinstance(section_items, dict):
            continue
        for entity_name, items in section_items.items():
            add_items(str(entity_name or ""), items)

    for key, value in content_data.items():
        if key in CONTENT_DATA_METADATA_KEYS:
            continue
        add_items(str(key or ""), value)
    return _merge_content_samples(samples)


def _state_dependency_ref(identity: Dict[str, str], field_name: str) -> str:
    normalized = STATEFUL_DEPENDENCY_FIELDS.get(str(field_name or "").strip(), "")
    if not normalized:
        return ""
    surface = _surface_intent_for_query_page(identity, {"states": []})
    if surface == "address_lookup" and normalized == "current_city":
        return "address_selection.current_city"
    if surface == "search" and normalized == "search_scope":
        return "search_context.scope"
    return f"context.{normalized}"


def _query_state_dependencies_from_content(
    identity: Dict[str, str],
    content_samples: Dict[str, List[Dict[str, Any]]],
    query: str,
) -> List[Dict[str, Any]]:
    query_text = str(query or "").strip()
    if not query_text or not isinstance(content_samples, dict):
        return []
    dependencies: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    for entity_name, items in content_samples.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if query_text not in _item_text_blob(item):
                continue
            for field, state_name in STATEFUL_DEPENDENCY_FIELDS.items():
                value = item.get(field)
                if not isinstance(value, (str, int, float)) or not str(value).strip():
                    continue
                state_ref = _state_dependency_ref(identity, field)
                key = (state_ref, str(value), str(entity_name))
                if not state_ref or key in seen:
                    continue
                seen.add(key)
                dependencies.append({
                    "state_ref": state_ref,
                    "state_field": state_name,
                    "value": str(value),
                    "source_entity": str(entity_name),
                    "source_field": field,
                })
    return dependencies[:12]


def _preview_text_from_compact_item(item: Any) -> str:
    if not isinstance(item, dict):
        return _item_text_blob(item if isinstance(item, dict) else {})
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    preferred_fields = (
        "place_name",
        "name",
        "title",
        "keyword",
        "merchant_name",
        "store_name",
        "full_address",
        "address",
        "address_detail",
    )
    values = [
        str(fields.get(field) or "").strip()
        for field in preferred_fields
        if str(fields.get(field) or "").strip()
    ]
    if values:
        return " ".join(_dedupe(values, limit=3))
    return _item_text_blob(item)


def _preview_record(kind: str, item: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "kind": kind,
        "text": _preview_text_from_compact_item(item),
    }
    if isinstance(item, dict):
        entity = str(item.get("entity") or "").strip()
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        if entity:
            record["entity"] = entity
        if fields:
            record["fields"] = {
                str(key): value
                for key, value in fields.items()
                if isinstance(value, (str, int, float)) and str(value).strip()
            }
    return record


def _content_items_for_query(
    content_samples: Dict[str, List[Dict[str, Any]]],
    query: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    suggestions: List[Dict[str, Any]] = []
    related: List[Dict[str, Any]] = []
    query_text = str(query or "").strip()
    if not query_text or not isinstance(content_samples, dict):
        return suggestions, related
    for entity_name, items in content_samples.items():
        if not isinstance(items, list):
            continue
        entity_text = str(entity_name or "")
        for item in items:
            if not isinstance(item, dict):
                continue
            blob = _item_text_blob(item)
            if query_text not in blob:
                continue
            compact = {
                "entity": entity_text,
                "fields": {
                    str(key): value
                    for key, value in item.items()
                    if isinstance(value, (str, int, float)) and str(value).strip()
                },
            }
            if _is_query_state_entity(entity_text):
                suggestions.append(compact)
            else:
                related.append(compact)
    return suggestions[:12], related[:12]


def _build_submit_targets_by_query(
    page_behaviors: Dict[str, Any],
    filename_by_page_id: Dict[str, str],
    page_name_by_page_id: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    targets_by_query: Dict[str, List[Dict[str, Any]]] = {}
    for page_id, behavior in page_behaviors.items():
        behavior_dict = _as_plain_dict(behavior)
        if not behavior_dict:
            continue
        for continuation in behavior_dict.get("task_continuations", []) if isinstance(behavior_dict.get("task_continuations", []), list) else []:
            if not isinstance(continuation, dict):
                continue
            interaction_contract = continuation.get("interaction_contract")
            if not _is_commit_interaction_contract(interaction_contract):
                continue
            target = continuation.get("target_filename") or filename_by_page_id.get(str(continuation.get("target_page_id") or ""))
            target_filename = _filename_without_query(str(target or ""))
            if not target_filename:
                continue
            target_page_name = str(
                continuation.get("target_page_name")
                or continuation.get("target_page")
                or page_name_by_page_id.get(str(continuation.get("target_page_id") or ""))
                or ""
            ).strip()
            for query in _query_values_from_continuation(continuation):
                targets_by_query.setdefault(query, []).append({
                    "query": query,
                    "target_filename": target_filename,
                    "target_page_name": target_page_name,
                    "source_page_id": str(page_id or ""),
                    "trigger_text": str(
                        continuation.get("element_name")
                        or continuation.get("display_text")
                        or continuation.get("action")
                        or ""
                    ).strip(),
                    "contract_id": (
                        str(interaction_contract.get("contract_id") or "").strip()
                        if isinstance(interaction_contract, dict)
                        else ""
                    ),
                })

    ranked: Dict[str, List[Dict[str, Any]]] = {}
    for query, targets in targets_by_query.items():
        deduped: Dict[str, Dict[str, Any]] = {}
        for target in targets:
            filename = str(target.get("target_filename") or "")
            if not filename:
                continue
            existing = deduped.get(filename)
            if existing is None:
                deduped[filename] = target
                continue
            if _route_action_target_score(target) > _route_action_target_score(existing):
                deduped[filename] = target
        scored = sorted(
            deduped.values(),
            key=lambda item: _route_action_target_score({
                "trigger_text": str(item.get("trigger_text") or query),
                "target_filename": item.get("target_filename"),
                "target_page_name": item.get("target_page_name"),
            }),
            reverse=True,
        )
        best_score = 0
        if scored:
            best_score = _route_action_target_score({
                "trigger_text": str(scored[0].get("trigger_text") or query),
                "target_filename": scored[0].get("target_filename"),
                "target_page_name": scored[0].get("target_page_name"),
            })
        if best_score > 0:
            scored = [
                item for item in scored
                if _route_action_target_score({
                    "trigger_text": str(item.get("trigger_text") or query),
                    "target_filename": item.get("target_filename"),
                    "target_page_name": item.get("target_page_name"),
                }) == best_score
            ]
        ranked[query] = scored[:4]
    return ranked


def _query_state_contract_for_page(
    identity: Dict[str, str],
    behavior: Any,
    content_samples: Dict[str, List[Dict[str, Any]]],
    submit_targets_by_query: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    queries = _query_values_for_behavior(behavior)
    if not queries:
        return {}
    query_states: List[Dict[str, Any]] = []
    for query in queries:
        suggestions, related_items = _content_items_for_query(content_samples, query)
        submit_targets = submit_targets_by_query.get(query, [])
        state = {
            "query": query,
            "suggestions": suggestions,
            "related_items": related_items,
            "submit_targets": submit_targets,
        }
        dependencies = _query_state_dependencies_from_content(identity, content_samples, query)
        if dependencies:
            state["state_dependencies"] = dependencies
        query_states.append(state)
    return {
        "format": "query_state_contract",
        "schema_revision": 1,
        "page_id": identity.get("page_id", ""),
        "filename": identity.get("filename", ""),
        "url_query_aliases": list(QUERY_PARAM_ALIASES),
        "hydration": {
            "read_url_query": True,
            "write_visible_search_input": True,
            "dispatch_input_event": True,
            "render_preview_panel": False,
        },
        "states": query_states,
    }


def _page_environment_query_state_contract(
    page_environment: Dict[str, Any],
    *,
    filename: str = "",
    page_id: str = "",
) -> Dict[str, Any]:
    if not isinstance(page_environment, dict) or not page_environment:
        return {}
    current_filename = _filename_without_query(
        filename
        or str(page_environment.get("filename") or page_environment.get("page_id") or "")
    )
    _fn_lower = current_filename.lower()
    _is_search_page = any(m in _fn_lower for m in ("search", "query"))
    _has_business_query_input = any(
        isinstance(pi, dict)
        and str(pi.get("field_role") or pi.get("parameter_id") or "") == "business_query"
        for pi in page_environment.get("parameterized_inputs", [])
        if isinstance(page_environment.get("parameterized_inputs"), list)
    )
    _has_query_state_owner = any(
        isinstance(tr, dict)
        and (tr.get("query_state_owner") or (tr.get("state_effect", {}) if isinstance(tr.get("state_effect"), dict) else {}).get("query_state_owner"))
        for tr in page_environment.get("transitions", [])
        if isinstance(page_environment.get("transitions"), list)
    )
    if not (_is_search_page or _has_business_query_input or _has_query_state_owner):
        return {}
    queries: List[str] = []

    def add_values(values: Any) -> int:
        before = len(queries)
        if isinstance(values, list):
            iterable = values
        else:
            iterable = [values]
        for value in iterable:
            text = str(value or "").strip()
            if text:
                queries.append(text)
        return len(queries) - before

    def values_for_keys(source: Any, keys: Iterable[str]) -> List[str]:
        if not isinstance(source, dict):
            return []
        values: List[str] = []
        for key in keys:
            value = source.get(key)
            if isinstance(value, list):
                values.extend(str(item or "").strip() for item in value if str(item or "").strip())
            elif str(value or "").strip():
                values.append(str(value or "").strip())
        return _dedupe(values)

    def effect_for(item: Dict[str, Any]) -> Dict[str, Any]:
        effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
        return effect if isinstance(effect, dict) else {}

    def query_kind(item: Dict[str, Any], effect: Dict[str, Any]) -> str:
        return str(item.get("query_transition_kind") or effect.get("query_transition_kind") or "").strip()

    def current_role(item: Dict[str, Any], effect: Dict[str, Any]) -> str:
        source = _filename_without_query(str(item.get("source_filename") or effect.get("source_filename") or ""))
        target = _filename_without_query(str(item.get("target_filename") or effect.get("target_filename") or ""))
        if source and source == current_filename and target and target != current_filename:
            return "source"
        if target and target == current_filename:
            return "target"
        if source and source == current_filename:
            return "source"
        return ""

    def committed_values(item: Dict[str, Any], effect: Dict[str, Any]) -> List[str]:
        return _dedupe(
            values_for_keys(item, ("committed_query_values", "applied_query_values", "committed_values"))
            + values_for_keys(effect, ("committed_query_values", "applied_query_values", "committed_values"))
        )

    def draft_values(item: Dict[str, Any], effect: Dict[str, Any]) -> List[str]:
        return _dedupe(
            values_for_keys(item, ("draft_observed_values", "observed_values", "input_text", "value"))
            + values_for_keys(effect, ("draft_observed_values", "observed_values", "input_text", "value"))
        )

    def add_role_scoped_query_values(item: Dict[str, Any], effect: Dict[str, Any]) -> int:
        role = current_role(item, effect)
        kind = query_kind(item, effect)
        if role == "source":
            return add_values(draft_values(item, effect))
        if role == "target":
            if kind in {"selected_suggestion_commit", "committed_result"}:
                added = add_values(committed_values(item, effect))
                return added or add_values(draft_values(item, effect))
            added = add_values(draft_values(item, effect))
            return added or add_values(committed_values(item, effect))
        if not role:
            added = add_values(committed_values(item, effect))
            return added or add_values(draft_values(item, effect))
        return 0

    suggestions_by_query: Dict[str, List[Dict[str, Any]]] = {}
    for projection in page_environment.get("render_projections", []):
        if not isinstance(projection, dict):
            continue
        contract = projection.get("suggestion_contract") if isinstance(projection.get("suggestion_contract"), dict) else {}
        if not contract:
            continue
        query_value = str(contract.get("query_value") or "").strip()
        if not query_value:
            continue
        titles = [
            str(item or "").strip()
            for item in contract.get("expected_titles", [])
            if str(item or "").strip()
        ] if isinstance(contract.get("expected_titles"), list) else []
        subtitles = [
            str(item or "").strip()
            for item in contract.get("expected_subtitles", [])
            if str(item or "").strip()
        ] if isinstance(contract.get("expected_subtitles"), list) else []
        rows: List[Dict[str, Any]] = []
        for idx, title in enumerate(titles):
            rows.append({
                "title": title,
                "subtitle": subtitles[idx] if idx < len(subtitles) else "",
                "source": "page_environment_spec.suggestion_contract",
            })
        if rows:
            suggestions_by_query[query_value] = rows


    for query_state in page_environment.get("query_states", []):
        if not isinstance(query_state, dict):
            continue
        contract = (
            query_state.get("suggestion_contract")
            if isinstance(query_state.get("suggestion_contract"), dict)
            else {}
        )
        query_value = str(
            query_state.get("query_value") or contract.get("query_value") or ""
        ).strip()
        if not query_value or not isinstance(contract.get("suggestions"), list):
            continue
        rows: List[Dict[str, Any]] = []
        for item in contract.get("suggestions", []):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            subtitle = str(item.get("subtitle") or "").strip()
            if not title:
                continue
            rows.append({
                "entity": "SearchSuggestion",
                "fields": {
                    "title": title,
                    "subtitle": subtitle,
                },
                "source": str(item.get("source") or "page_environment_spec.query_states"),
            })
        if rows:
            suggestions_by_query[query_value] = rows

    role_scoped_count = 0
    for item in page_environment.get("parameterized_inputs", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("field_role") or item.get("parameter_id") or "") != "business_query":
            continue
        role_scoped_count += add_role_scoped_query_values(item, effect_for(item))

    for transition in page_environment.get("transitions", []):
        if not isinstance(transition, dict):
            continue
        effect = effect_for(transition)
        mentions_query = (
            str(effect.get("field_role") or "") == "business_query"
            or bool(query_kind(transition, effect))
            or bool(transition.get("query_state_owner") or effect.get("query_state_owner"))
        )
        if not mentions_query:
            continue
        role_scoped_count += add_role_scoped_query_values(transition, effect)

    initial_query = page_environment.get("initial_state", {}).get("query", {})
    if isinstance(initial_query, dict) and role_scoped_count == 0:
        add_values(initial_query.get("committed_values"))
        add_values(initial_query.get("default"))
    queries = _dedupe(queries)
    if not queries:
        return {}
    return {
        "format": "query_state_contract",
        "schema_revision": 1,
        "page_id": page_id or str(page_environment.get("page_id") or ""),
        "filename": filename or str(page_environment.get("filename") or page_environment.get("page_id") or ""),
        "url_query_aliases": list(QUERY_PARAM_ALIASES),
        "hydration": {
            "read_url_query": True,
            "write_visible_search_input": True,
            "dispatch_input_event": True,
            "render_preview_panel": bool(suggestions_by_query),
        },
        "states": [
            {
                "query": query,
                "suggestions": suggestions_by_query.get(query, []),
                "related_items": [],
                "submit_targets": [],
                "source": "page_environment_spec",
            }
            for query in queries
        ],
        "source": "page_environment_spec",
    }


def _page_environment_draft_state_contract(
    page_environment: Dict[str, Any],
    *,
    filename: str = "",
    page_id: str = "",
) -> Dict[str, Any]:
    if not isinstance(page_environment, dict) or not page_environment:
        return {}
    current_filename = _filename_without_query(
        filename
        or str(page_environment.get("filename") or page_environment.get("page_id") or "")
    )
    draft_refs: List[str] = []
    submit_targets: List[Dict[str, Any]] = []
    for pi in page_environment.get("parameterized_inputs", []) if isinstance(page_environment.get("parameterized_inputs"), list) else []:
        if not isinstance(pi, dict):
            continue
        fr = str(pi.get("field_role") or pi.get("parameter_id") or "")
        if fr in ("draft_ref", "form_value", "draft_field"):
            ref = str(pi.get("state_ref") or pi.get("value") or "").strip()
            if ref:
                draft_refs.append(ref)
    for tr in page_environment.get("transitions", []) if isinstance(page_environment.get("transitions"), list) else []:
        if not isinstance(tr, dict):
            continue
        state_ref = str(tr.get("state_ref") or "").strip()
        if state_ref and state_ref.startswith("draft::"):
            if state_ref not in draft_refs:
                draft_refs.append(state_ref)
        effect = tr.get("state_effect") if isinstance(tr.get("state_effect"), dict) else {}
        sr = str(effect.get("state_ref") or "").strip()
        if sr and sr.startswith("draft::") and sr not in draft_refs:
            draft_refs.append(sr)
        target_fn = _filename_without_query(str(tr.get("target_filename") or ""))
        if target_fn and target_fn != current_filename and target_fn.endswith(".html"):
            if not any(st.get("target_filename") == target_fn for st in submit_targets):
                submit_targets.append({"target_filename": target_fn})
    init_state = page_environment.get("initial_state", {})
    if isinstance(init_state, dict):
        for key in ("draft_ref", "state_ref"):
            val = str(init_state.get(key) or "").strip()
            if val and val.startswith("draft::") and val not in draft_refs:
                draft_refs.append(val)
    if not draft_refs:
        return {}
    return {
        "format": "draft_state_contract",
        "schema_revision": 1,
        "page_id": page_id or str(page_environment.get("page_id") or ""),
        "filename": current_filename,
        "draft_ref": draft_refs[0],
        "additional_draft_refs": draft_refs[1:] if len(draft_refs) > 1 else [],
        "fields": [{"field_role": "form_value", "schema": {"type": "string"}}],
        "submit_targets": submit_targets,
    }


def _surface_intent_for_query_page(identity: Dict[str, str], query_state_contract: Dict[str, Any]) -> str:
    text = " ".join([
        str(identity.get("page_type") or ""),
        str(identity.get("page_name") or ""),
        str(identity.get("filename") or ""),
    ]).lower()
    if any(marker in text for marker in ("address", "地址", "poi", "地点")):
        return "address_lookup"
    if any(marker in text for marker in ("city", "城市")):
        return "city_lookup"
    contract_texts: List[str] = []
    for state in query_state_contract.get("states", []) if isinstance(query_state_contract, dict) else []:
        if not isinstance(state, dict):
            continue
        contract_texts.append(str(state.get("query") or ""))
        for target in state.get("submit_targets", []) if isinstance(state.get("submit_targets", []), list) else []:
            if isinstance(target, dict):
                contract_texts.extend([
                    str(target.get("target_filename") or ""),
                    str(target.get("target_page_name") or ""),
                    str(target.get("trigger_text") or ""),
                ])
    contract_text = " ".join(contract_texts).lower()
    if any(marker in f"{text} {contract_text}" for marker in ("search", "搜索", "query")):
        return "search"
    return "typed_input"


def _input_surface_contract_for_page(
    identity: Dict[str, str],
    query_state_contract: Dict[str, Any],
) -> Dict[str, Any]:
    states = query_state_contract.get("states", []) if isinstance(query_state_contract, dict) else []
    if not isinstance(states, list) or not states:
        return {}
    observed_values = _dedupe(
        [str(state.get("query") or "").strip() for state in states if isinstance(state, dict)],
        limit=16,
    )
    if not observed_values:
        return {}
    transitions: List[Dict[str, Any]] = [
        {
            "event": "activate",
            "trigger": "click_input_surface",
            "from_state": "inactive",
            "to_state": "active_empty",
        }
    ]
    preview_items: List[Dict[str, Any]] = []
    seen_preview_text: set[str] = set()
    state_dependencies: List[Dict[str, Any]] = []
    seen_dependency_keys: set[Tuple[str, str, str]] = set()

    def add_preview_item(kind: str, item: Any) -> None:
        record = _preview_record(kind, item)
        text = str(record.get("text") or "").strip()
        if not text or text in seen_preview_text:
            return
        seen_preview_text.add(text)
        preview_items.append(record)

    def add_state_dependency(item: Any) -> None:
        if not isinstance(item, dict):
            return
        key = (
            str(item.get("state_ref") or ""),
            str(item.get("value") or ""),
            str(item.get("source_entity") or ""),
        )
        if not key[0] or not key[1] or key in seen_dependency_keys:
            return
        seen_dependency_keys.add(key)
        state_dependencies.append(dict(item))

    for state in states:
        if not isinstance(state, dict):
            continue
        query = str(state.get("query") or "").strip()
        if not query:
            continue
        suggestions = state.get("suggestions", []) if isinstance(state.get("suggestions", []), list) else []
        related_items = state.get("related_items", []) if isinstance(state.get("related_items", []), list) else []
        for dependency in state.get("state_dependencies", []) if isinstance(state.get("state_dependencies", []), list) else []:
            add_state_dependency(dependency)
        has_preview = bool(suggestions or related_items)
        transitions.append({
            "event": "edit",
            "trigger": "input_change",
            "value": query,
            "from_state": "active_empty",
            "to_state": "preview_visible" if has_preview else "active_with_value",
        })
        submit_targets = state.get("submit_targets", []) if isinstance(state.get("submit_targets", []), list) else []
        for target in submit_targets[:1]:
            if not isinstance(target, dict):
                continue
            target_filename = _filename_without_query(str(target.get("target_filename") or ""))
            if target_filename:
                transitions.append({
                    "event": "commit",
                    "trigger": "click_commit_button",
                    "value": query,
                    "from_state": "preview_visible" if has_preview else "active_with_value",
                    "to_state": "committed_result",
                    "target_filename": target_filename,
                    "query_param": f"q={query}",
                })
        for item in suggestions[:6]:
            add_preview_item("suggestion", item)
        for item in related_items[:6]:
            add_preview_item("related_item", item)

    contract = {
        "format": "input_surface_contract",
        "schema_revision": 1,
        "surface_id": f"{identity.get('page_id', '')}::typed_input",
        "source_page_id": identity.get("page_id", ""),
        "source_filename": identity.get("filename", ""),
        "source_materialized_page_id": identity.get("materialized_page_id", ""),
        "surface_intent": _surface_intent_for_query_page(identity, query_state_contract),
        "surface_role": "primary_typed_input",
        "observed_values": observed_values,
        "states": [
            "inactive",
            "active_empty",
            "active_with_value",
            "preview_visible",
            "committed_result",
        ],
        "transitions": transitions,
        "preview": {
            "observed": bool(preview_items),
            "trigger_event": "input_change",
            "render_scope": "same_page_surface",
            "items": preview_items[:12],
        },
        "provenance": {
            "source": "derived_from_query_state_contract",
        },
    }
    if state_dependencies:
        contract["state_dependencies"] = state_dependencies[:12]
    if preview_items:
        contract["stateful_surface_components"] = [
            {
                "component_id": f"{identity.get('page_id', '')}::typed_input",
                "component_role": "state_input",
                "state_ref": "query_state.current_value",
                "observed_values": observed_values,
            },
            {
                "component_id": f"{identity.get('page_id', '')}::candidate_collection",
                "component_role": "candidate_collection",
                "state_ref": "query_state.preview_candidates",
                "depends_on": [item.get("state_ref") for item in state_dependencies if item.get("state_ref")],
                "observed_item_count": len(preview_items),
            },
        ]
    return contract


def _state_ref_display_role(state_ref: str) -> str:
    text = str(state_ref or "").strip().lower()
    if "city" in text:
        return "city_selector_label"
    if "filter" in text:
        return "filter_state_label"
    if "address" in text:
        return "selected_address_label"
    if "invoice" in text or "title" in text:
        return "selected_record_label"
    if text:
        return "selected_state_label"
    return ""


def _is_command_control_display_value(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or "")).strip().lower()
    if not text:
        return False
    command_values = {
        "确定",
        "确认",
        "完成",
        "保存",
        "提交",
        "选择",
        "取消",
        "返回",
        "关闭",
        "搜索",
        "支付",
        "去支付",
        "立即支付",
        "去结算",
        "提交订单",
        "confirm",
        "done",
        "save",
        "submit",
        "select",
        "cancel",
        "back",
        "close",
        "search",
        "pay",
        "checkout",
    }
    if text in command_values:
        return True
    return bool(re.fullmatch(r"(立即)?(去)?(支付|结算|提交|确认|确定|完成|保存)(订单)?", text))


def _state_value_from_transition_text(trigger_text: str, state_ref: str) -> str:
    quoted = _extract_quoted_values(trigger_text)
    ref = str(state_ref or "").lower()
    if quoted:
        if "city" in ref:
            city_like = [value for value in quoted if "市" in value or "省" in value or "区" in value]
            return city_like[0] if city_like else quoted[0]
        return quoted[0]
    tokens = _interaction_tokens(trigger_text)
    return tokens[0] if tokens else ""


def _should_bind_state_display(contract: Dict[str, Any]) -> bool:
    action_semantic = str(contract.get("action_semantic") or "").strip()
    source_lifecycle = str(contract.get("source_lifecycle") or "").strip()
    state_provenance = str(contract.get("state_provenance") or "").strip()
    return bool(
        action_semantic.startswith("select_")
        or source_lifecycle in {"selected_option", "selection", "candidate_result"}
        or state_provenance in {"selected_option", "selection", "candidate_result"}
    )


def _append_state_display_binding(
    bindings_by_target: Dict[str, List[Dict[str, Any]]],
    seen: set[Tuple[str, str, str, str]],
    *,
    target_filename: str,
    state_ref: str,
    trigger_text: str,
    source_action_id: str,
    source_page_id: str,
) -> None:
    target = _filename_without_query(target_filename)
    state_ref = str(state_ref or "").strip()
    if not target or not state_ref:
        return
    role = _state_ref_display_role(state_ref)
    if not role:
        return
    state_value = _state_value_from_transition_text(trigger_text, state_ref)
    if not state_value:
        return
    if _is_command_control_display_value(state_value):
        return
    key = (target, state_ref, role, state_value)
    if key in seen:
        return
    seen.add(key)
    bindings_by_target.setdefault(target, []).append({
        "state_ref": state_ref,
        "state_value": state_value,
        "source_action_id": source_action_id,
        "source_page_id": str(source_page_id or ""),
        "target_filename": target,
        "display_role": role,
        "required_display_text": state_value,
        "lifecycle": "after_state_commit",
    })


def _build_state_display_bindings_by_target(
    page_behaviors: Dict[str, Any],
    filename_by_page_id: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    bindings_by_target: Dict[str, List[Dict[str, Any]]] = {}
    seen: set[Tuple[str, str, str, str]] = set()
    for source_page_id, behavior in page_behaviors.items():
        behavior_dict = _as_plain_dict(behavior)
        if not behavior_dict:
            continue
        for continuation in behavior_dict.get("task_continuations", []) if isinstance(behavior_dict.get("task_continuations", []), list) else []:
            if not isinstance(continuation, dict):
                continue
            target = continuation.get("target_filename") or filename_by_page_id.get(str(continuation.get("target_page_id") or ""))
            target_filename = _filename_without_query(str(target or ""))
            if not target_filename:
                continue
            contract = continuation.get("interaction_contract")
            if not isinstance(contract, dict):
                continue
            state_ref = str(contract.get("state_ref") or "").strip()
            if not state_ref:
                continue
            if not _should_bind_state_display(contract):
                continue
            trigger_text = str(
                continuation.get("element_name")
                or continuation.get("display_text")
                or continuation.get("action")
                or ""
            ).strip()
            _append_state_display_binding(
                bindings_by_target,
                seen,
                target_filename=target_filename,
                state_ref=state_ref,
                trigger_text=trigger_text,
                source_action_id=_stable_route_action_id(
                    "state_display",
                    str(contract.get("contract_id") or trigger_text or source_page_id),
                ),
                source_page_id=str(source_page_id or ""),
            )
    return bindings_by_target


def _merge_state_display_bindings_from_route_contracts(
    bindings_by_target: Dict[str, List[Dict[str, Any]]],
    route_execution_contracts_by_filename: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    merged = {target: [dict(item) for item in bindings] for target, bindings in bindings_by_target.items()}
    seen: set[Tuple[str, str, str, str]] = set()
    for target, bindings in merged.items():
        for binding in bindings:
            key = (
                _filename_without_query(str(target or "")),
                str(binding.get("state_ref") or ""),
                str(binding.get("display_role") or ""),
                str(binding.get("state_value") or ""),
            )
            seen.add(key)

    for source_filename, contract in route_execution_contracts_by_filename.items():
        if not isinstance(contract, dict):
            continue
        source_page_id = str(contract.get("page_id") or "")
        actions = list(contract.get("navigation_actions", []) or []) + list(contract.get("actions", []) or [])
        for action in actions:
            if not isinstance(action, dict):
                continue
            target_filename = _filename_without_query(str(action.get("target_filename") or ""))
            if not target_filename or target_filename == _filename_without_query(source_filename):
                continue
            state_commit = action.get("state_commit") if isinstance(action.get("state_commit"), dict) else {}
            state_ref = str(action.get("state_ref") or state_commit.get("commit_state_ref") or "").strip()
            if not state_ref:
                continue
            contract_view = {
                "action_semantic": action.get("action_semantic") or state_commit.get("action_semantic"),
                "source_lifecycle": action.get("source_lifecycle") or state_commit.get("source_lifecycle"),
                "state_provenance": action.get("state_provenance") or state_commit.get("state_provenance"),
            }
            if not _should_bind_state_display(contract_view):
                continue
            trigger_text = str(action.get("trigger_text") or "").strip()
            if not trigger_text and isinstance(action.get("binding"), dict):
                labels = action["binding"].get("labels")
                if isinstance(labels, list):
                    trigger_text = " ".join(str(label) for label in labels if str(label or "").strip())
            _append_state_display_binding(
                merged,
                seen,
                target_filename=target_filename,
                state_ref=state_ref,
                trigger_text=trigger_text,
                source_action_id=str(action.get("action_id") or _stable_route_action_id("state_display", trigger_text)),
                source_page_id=source_page_id,
            )
    return merged


def _dynamic_surface_contract_for_page(
    identity: Dict[str, str],
    query_state_contract: Dict[str, Any],
    input_surface_contract: Dict[str, Any],
    behavior: Any = None,
    state_display_bindings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    components = list(
        input_surface_contract.get("stateful_surface_components", [])
        if isinstance(input_surface_contract, dict)
        and isinstance(input_surface_contract.get("stateful_surface_components", []), list)
        else []
    )
    dependencies = list(
        input_surface_contract.get("state_dependencies", [])
        if isinstance(input_surface_contract, dict)
        and isinstance(input_surface_contract.get("state_dependencies", []), list)
        else []
    )
    if dependencies:
        components.insert(0, {
            "component_id": f"{identity.get('page_id', '')}::context_state_display",
            "component_role": "state_display_or_scope_anchor",
            "state_refs": _dedupe(
                [str(item.get("state_ref") or "") for item in dependencies if isinstance(item, dict)],
                limit=8,
            ),
            "observed_values": _dedupe(
                [str(item.get("value") or "") for item in dependencies if isinstance(item, dict)],
                limit=12,
            ),
            "affects": [input_surface_contract.get("surface_id", "")],
        })

    dynamic_transitions: List[Dict[str, Any]] = []
    for state in query_state_contract.get("states", []) if isinstance(query_state_contract, dict) else []:
        if not isinstance(state, dict):
            continue
        query = str(state.get("query") or "").strip()
        suggestions = state.get("suggestions", []) if isinstance(state.get("suggestions", []), list) else []
        related_items = state.get("related_items", []) if isinstance(state.get("related_items", []), list) else []
        if suggestions or related_items:
            dynamic_transitions.append({
                "event": "input_change",
                "source_component": input_surface_contract.get("surface_id", ""),
                "value": query,
                "produces": "preview_candidates",
                "candidate_count": len(suggestions) + len(related_items),
            })
        for target in state.get("submit_targets", []) if isinstance(state.get("submit_targets", []), list) else []:
            if not isinstance(target, dict):
                continue
            dynamic_transitions.append({
                "event": "commit_query",
                "source_component": input_surface_contract.get("surface_id", ""),
                "value": query,
                "target_filename": _filename_without_query(str(target.get("target_filename") or "")),
                "commit_state_ref": "query_state.current_value",
            })

    task_components, task_transitions = _task_runtime_surfaces_for_behavior(identity, behavior)
    components.extend(task_components)
    dynamic_transitions.extend(task_transitions)
    display_bindings = [
        dict(binding)
        for binding in (state_display_bindings or [])
        if isinstance(binding, dict)
    ]
    for binding in display_bindings:
        state_ref = str(binding.get("state_ref") or "").strip()
        role = str(binding.get("display_role") or "").strip()
        if not state_ref or not role:
            continue
        components.append({
            "component_id": f"{identity.get('page_id', '')}::{role}",
            "component_role": "state_display_surface",
            "state_ref": state_ref,
            "display_role": role,
            "observed_values": [str(binding.get("required_display_text") or binding.get("state_value") or "").strip()],
        })

    if not components and not dynamic_transitions and not display_bindings:
        return {}
    provenance_source = (
        "derived_from_query_input_and_task_interaction_contracts"
        if task_components or task_transitions
        else "derived_from_query_input_and_page_content_evidence"
    )
    return {
        "format": "dynamic_surface_contract",
        "schema_revision": 1,
        "page_id": identity.get("page_id", ""),
        "filename": identity.get("filename", ""),
        "general_page_id": identity.get("general_page_id", ""),
        "materialized_page_id": identity.get("materialized_page_id", ""),
        "surface_id": f"{identity.get('page_id', '')}::dynamic_surface",
        "surface_role": "runtime_state_surface",
        "stateful_surface_components": components[:12],
        "state_dependencies": dependencies[:12],
        "dynamic_transitions": dynamic_transitions[:16],
        "state_display_bindings": display_bindings[:12],
        "render_policy": {
            "same_page_surface": True,
            "no_evidence_behavior": "empty_or_unavailable",
            "synthetic_business_content_allowed": False,
        },
        "provenance": {
            "source": provenance_source,
        },
    }


def _surface_role_for_state_scope(state_scope: str, action_semantic: str = "") -> str:
    scope = str(state_scope or "").strip()
    semantic = str(action_semantic or "").strip()
    if scope == "record" or semantic == "edit_existing_record":
        return "record_surface"
    if scope == "draft" or semantic in {"create_record_draft", "update_current_draft"}:
        return "form_draft_surface"
    if scope == "collection" or semantic in {"open_collection", "update_filter"}:
        return "collection_surface"
    if scope == "selection" or semantic.startswith("select_"):
        return "selection_control_surface"
    if scope == "accumulator" or semantic == "append_accumulator":
        return "accumulator_surface"
    if scope == "transaction" or semantic == "commit_transaction":
        return "transaction_surface"
    if scope == "inline_ui" or semantic == "inline_ui_state":
        return "inline_ui_surface"
    if scope == "session":
        return "session_context_surface"
    return "runtime_state_surface"


def _task_runtime_surfaces_for_behavior(
    identity: Dict[str, str],
    behavior: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    behavior_dict = _as_plain_dict(behavior)
    if not behavior_dict:
        return [], []
    continuations = behavior_dict.get("task_continuations", [])
    if not isinstance(continuations, list):
        return [], []
    components: List[Dict[str, Any]] = []
    transitions: List[Dict[str, Any]] = []
    seen_components: set[Tuple[str, str, str, str]] = set()
    seen_transitions: set[Tuple[str, str, str]] = set()
    for continuation in continuations:
        if not isinstance(continuation, dict):
            continue
        contract = continuation.get("interaction_contract")
        if not isinstance(contract, dict):
            continue
        action_semantic = str(contract.get("action_semantic") or "").strip()
        state_scope = str(contract.get("state_scope") or "").strip()
        state_ref = str(contract.get("state_ref") or "").strip()
        entity_ref = str(contract.get("entity_ref") or "").strip()
        contract_id = str(contract.get("contract_id") or contract.get("candidate_id") or "").strip()
        if not (action_semantic or state_scope or state_ref or entity_ref):
            continue
        role = _surface_role_for_state_scope(state_scope, action_semantic)
        component_key = (role, state_ref, entity_ref, contract_id)
        if component_key not in seen_components:
            seen_components.add(component_key)
            component_seed = contract_id or "|".join(component_key)
            component_suffix = re.sub(r"[^A-Za-z0-9_]+", "_", component_seed).strip("_")
            if not component_suffix:
                component_suffix = hashlib.sha1(component_seed.encode("utf-8")).hexdigest()[:12]
            component = {
                "component_id": "{}::{}".format(
                    identity.get("page_id", ""),
                    component_suffix[:80],
                ),
                "component_role": role,
                "state_scope": state_scope,
                "state_ref": state_ref,
                "entity_ref": entity_ref,
                "contract_id": contract_id,
            }
            components.append({
                key: value
                for key, value in component.items()
                if value not in ("", [], {})
            })
        hydration = contract.get("hydration", {}) if isinstance(contract.get("hydration"), dict) else {}
        sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect"), dict) else {}
        trigger_text = str(
            continuation.get("display_text")
            or continuation.get("element_name")
            or continuation.get("action")
            or ""
        ).strip()
        transition_key = (contract_id, action_semantic, trigger_text)
        if transition_key in seen_transitions:
            continue
        seen_transitions.add(transition_key)
        transition = {
            "event": action_semantic or "runtime_state_action",
            "source_component": components[-1].get("component_id", "") if components else "",
            "trigger_text": trigger_text,
            "state_scope": state_scope,
            "state_ref": state_ref,
            "entity_ref": entity_ref,
            "contract_id": contract_id,
            "hydration_required": bool(hydration.get("required", False)),
            "hydration_mode": str(hydration.get("mode") or ""),
            "target_filename": _filename_without_query(str(continuation.get("target_filename") or "")),
            "sdk_effect": str(sdk_effect.get("effect") or ""),
            "sdk_method": str(sdk_effect.get("method") or ""),
        }
        transitions.append({
            key: value
            for key, value in transition.items()
            if value not in ("", [], {})
        })
    return components, transitions


def _reference_trace_id_from_step(step: str) -> str:
    text = str(step or "").strip()
    return text.split(":", 1)[0] if ":" in text else text


_ACTION_NOISE_RE = re.compile(
    r"(我现在要|现在要|点击|进入|对应|页面|商家页面|商品页面|卡片|商品|商家|板块|下方|里的|中的|第一个|第一|个|的|了|按钮|区域|入口|查看|选择|当前|后续|底部导航栏|底部导航|导航栏|最右侧)"
)


def _interaction_tokens(text: str) -> List[str]:
    normalized = re.sub(r"[“”\"'（）()，,。；;：:、/\\|+\-_\s]+", " ", str(text or ""))
    normalized = _ACTION_NOISE_RE.sub(" ", normalized)
    tokens = []
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", normalized):
        token = token.strip()
        if len(token) >= 3:
            tokens.append(token)
    return _dedupe(tokens, limit=16)


def _interaction_surface(text: str) -> str:
    trigger = str(text or "")
    if "滑动" in trigger or "滚动" in trigger:
        return "scroll"
    if "底部导航" in trigger or "导航栏" in trigger:
        return "global_nav"
    if "地址" in trigger:
        return "address"
    quoted = re.findall(r"[“\"]([^”\"]+)[”\"]", trigger)
    if any("+" in item or "＋" in item for item in quoted):
        return "product_promo"
    if any(marker in trigger for marker in ("商品", "爆品", "新人1元", "促销", "优惠活动")):
        return "product_promo"
    if any(marker in trigger for marker in ("店铺", "门店", "商家")):
        return "merchant"
    if "搜索" in trigger:
        return "search"
    return "unknown"


def _quoted_entity_signatures(text: str) -> List[str]:
    signatures: List[str] = []
    for item in re.findall(r"[“\"]([^”\"]+)[”\"]", str(text or "")):
        normalized = re.sub(r"\s+", "", item)
        normalized = re.sub(r"[，,。；;：:、/\\|]+", "", normalized)
        if len(normalized) >= 2:
            signatures.append(normalized)
    return _dedupe(signatures, limit=8)


def _trigger_signature(trigger: str) -> str:
    entities = _quoted_entity_signatures(trigger)
    if entities:
        return "|".join(entities)
    tokens = _interaction_tokens(trigger)
    return "|".join(tokens[:6])


def _page_lookup_by_id(pages: List[Any]) -> Dict[str, Any]:
    return {
        str(getattr(page, "page_id", "") or ""): page
        for page in pages
        if str(getattr(page, "page_id", "") or "")
    }


def _observed_navigation_records(site_model: Any) -> List[Dict[str, Any]]:
    structure = getattr(site_model, "structure", None)
    pages = list(getattr(structure, "page_prototypes", []) or [])
    page_by_id = _page_lookup_by_id(pages)
    filename_by_page_id = {
        page_id: _filename_without_query(str(getattr(page, "filename", "") or ""))
        for page_id, page in page_by_id.items()
    }
    records: List[Dict[str, Any]] = []
    page_behaviors = getattr(structure, "page_behaviors", {}) if structure is not None else {}
    if isinstance(page_behaviors, dict):
        for source_page_id, behavior in page_behaviors.items():
            if not isinstance(behavior, dict):
                continue
            source_proto = page_by_id.get(str(source_page_id))
            source_identity = _page_identity(source_proto)
            for nav in behavior.get("navigation_out", []) if isinstance(behavior.get("navigation_out", []), list) else []:
                if not isinstance(nav, dict):
                    continue
                target = nav.get("to_filename") or filename_by_page_id.get(str(nav.get("to_page_id") or ""))
                target_filename = _filename_without_query(str(target or ""))
                target_proto = page_by_id.get(str(nav.get("to_page_id") or ""))
                target_page_name = str(
                    getattr(target_proto, "page_name", "")
                    or nav.get("to_page")
                    or ""
                ).strip()
                trigger = str(nav.get("trigger") or "").strip()
                if not source_identity["filename"] or not target_filename or not trigger:
                    continue
                evidence_steps = nav.get("evidence_steps", [])
                if not isinstance(evidence_steps, list):
                    evidence_steps = [str(evidence_steps)]
                records.append({
                    "source_page_id": source_identity["page_id"],
                    "source_filename": _filename_without_query(source_identity["filename"]),
                    "source_materialized_page_id": source_identity["materialized_page_id"],
                    "trigger": trigger,
                    "target_filename": target_filename,
                    "target_page_id": str(nav.get("to_page_id") or ""),
                    "target_page_name": target_page_name,
                    "evidence_steps": [str(step) for step in evidence_steps if str(step or "").strip()],
                    "action_semantic": "page_navigation",
                    "target_source": str(nav.get("target_source") or nav.get("source") or "").strip(),
                    "affordance_kind": str(nav.get("affordance_kind") or "").strip(),
                    "label": str(nav.get("label") or "").strip(),
                    "labels": [
                        str(label or "").strip()
                        for label in nav.get("labels", [])
                        if str(label or "").strip()
                    ] if isinstance(nav.get("labels"), list) else [],
                    "component_ref": str(nav.get("component_ref") or "").strip(),
                    "component_role": str(nav.get("component_role") or "").strip(),
                    "entity": str(nav.get("entity") or "").strip(),
                    "binding": nav.get("binding", {}) if isinstance(nav.get("binding"), dict) else {},
                    "route_binding_semantics": nav.get("route_binding_semantics", {}) if isinstance(nav.get("route_binding_semantics"), dict) else {},
                    "provenance": nav.get("provenance", {}) if isinstance(nav.get("provenance"), dict) else {},
                    "admission_reason": str(nav.get("admission_reason") or "").strip(),
                })
    site_graph = getattr(structure, "site_graph", None) if structure is not None else None
    for edge in getattr(site_graph, "edges", []) or []:
        source_page_id = str(getattr(edge, "from_page_id", "") or "")
        target_page_id = str(getattr(edge, "to_page_id", "") or "")
        source_proto = page_by_id.get(source_page_id)
        target_proto = page_by_id.get(target_page_id)
        source_identity = _page_identity(source_proto)
        target_filename = _filename_without_query(filename_by_page_id.get(target_page_id, ""))
        target_page_name = str(getattr(target_proto, "page_name", "") or "").strip()
        trigger = str(getattr(edge, "trigger", "") or "").strip()
        if not source_identity["filename"] or not target_filename or not trigger:
            continue
        evidence_steps = list(getattr(edge, "evidence_steps", []) or [])
        if not evidence_steps and getattr(edge, "observed_in_trace", "") and getattr(edge, "step_index", -1) not in (-1, None):
            evidence_steps = [f"{getattr(edge, 'observed_in_trace')}:{getattr(edge, 'step_index')}"]
        records.append({
            "source_page_id": source_identity["page_id"],
            "source_filename": _filename_without_query(source_identity["filename"]),
            "source_materialized_page_id": source_identity["materialized_page_id"],
            "trigger": trigger,
            "target_filename": target_filename,
            "target_page_id": target_page_id,
            "target_page_name": target_page_name,
            "evidence_steps": [str(step) for step in evidence_steps if str(step or "").strip()],
            "action_semantic": "page_navigation",
        })

    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for record in records:
        key = (
            str(record.get("source_page_id") or ""),
            str(record.get("target_filename") or ""),
            str(record.get("trigger") or ""),
        )
        existing = merged.setdefault(key, dict(record))
        existing["evidence_steps"] = _dedupe(
            list(existing.get("evidence_steps", [])) + list(record.get("evidence_steps", [])),
            limit=32,
        )
    return list(merged.values())


def _navigation_label_candidates(trigger: str) -> List[str]:
    values = _extract_quoted_values(trigger)
    if not values:
        values = _quoted_entity_signatures(trigger)
    if not values:
        values = _interaction_tokens(trigger)
    return _dedupe(values, limit=6)


def _search_control_label_candidates(trigger: str) -> List[str]:
    text = str(trigger or "")
    labels: List[str] = []
    if re.search(r"顶部.{0,4}搜索框", text):
        labels.append("顶部搜索框")
    if "搜索框" in text:
        labels.append("搜索框")
    if not labels and any(marker in text for marker in ("搜索入口", "搜索区域", "搜索页面")):
        labels.append("搜索")
    return _dedupe(labels, limit=3)


def _navigation_binding_kind(trigger: str) -> str:
    surface = _interaction_surface(trigger)
    if surface == "global_nav":
        return "global_nav_item"
    if surface == "search":
        return "search_control"
    if surface == "merchant":
        return "merchant_card"
    if surface == "product_promo":
        return "product_card"
    text = str(trigger or "")
    if any(marker in text for marker in ("分类", "频道", "图标", "入口")):
        return "category_entry"
    return "text_label"


def _navigation_surface(trigger: str) -> str:
    binding_kind = _navigation_binding_kind(trigger)
    if binding_kind == "global_nav_item":
        return "global_navigation"
    if binding_kind == "category_entry":
        return "category_grid"
    if binding_kind == "merchant_card":
        return "merchant_feed"
    if binding_kind == "product_card":
        return "product_feed"
    if binding_kind == "search_control":
        return "search_surface"
    return "current_page"


def _record_shell_affordance_labels(record: Dict[str, Any], trigger: str) -> List[str]:
    binding = record.get("binding", {}) if isinstance(record.get("binding"), dict) else {}
    labels = [
        str(label or "").strip()
        for label in binding.get("labels", [])
        if str(label or "").strip()
    ] if isinstance(binding.get("labels"), list) else []
    if isinstance(record.get("labels"), list):
        labels.extend(
            str(label or "").strip()
            for label in record.get("labels", [])
            if str(label or "").strip()
        )
    label = str(record.get("label") or "").strip()
    if label:
        labels.append(label)
    if not labels and trigger.startswith("点击"):
        stripped = trigger.replace("点击", "", 1).strip()
        if stripped:
            labels.append(stripped)
    return _dedupe([label for label in labels if label], limit=6)


def _build_observed_navigation_affordances(site_model: Any) -> Dict[str, List[Dict[str, Any]]]:
    affordances_by_page_id: Dict[str, List[Dict[str, Any]]] = {}
    seen: set[Tuple[str, str, str]] = set()
    for record in _observed_navigation_records(site_model):
        trigger = str(record.get("trigger") or "").strip()
        source_page_id = str(record.get("source_page_id") or "").strip()
        source_filename = _filename_without_query(str(record.get("source_filename") or ""))
        target_filename = _filename_without_query(str(record.get("target_filename") or ""))
        target_page_id = str(record.get("target_page_id") or "").strip()
        if not source_page_id or not source_filename or not target_filename or source_filename == target_filename:
            continue
        if not trigger or _is_contextual_back_text(trigger) or _interaction_surface(trigger) == "scroll":
            continue
        target_source = str(record.get("target_source") or "").strip()
        is_shell_affordance = (
            target_source == "shell_component_affordance"
            or str(record.get("affordance_kind") or "").strip() == "shell_component_navigation_affordance"
        )
        source_binding = record.get("binding", {}) if isinstance(record.get("binding"), dict) else {}
        binding_kind = (
            str(source_binding.get("binding_kind") or "").strip()
            if is_shell_affordance
            else ""
        ) or _navigation_binding_kind(trigger)
        labels = (
            _record_shell_affordance_labels(record, trigger)
            if is_shell_affordance
            else (
                _search_control_label_candidates(trigger)
                if binding_kind == "search_control"
                else _navigation_label_candidates(trigger)
            )
        )
        label_key = "|".join(labels) if labels else _trigger_signature(trigger)
        key = (source_page_id, target_filename, label_key)
        if key in seen:
            continue
        seen.add(key)
        evidence_steps = record.get("evidence_steps", [])
        if not isinstance(evidence_steps, list):
            evidence_steps = [str(evidence_steps)]
        evidence_steps = _dedupe([str(step) for step in evidence_steps if str(step or "").strip()], limit=32)
        reference_trace_ids = _dedupe(
            [_reference_trace_id_from_step(step) for step in evidence_steps if _reference_trace_id_from_step(step)],
            limit=16,
        )
        action_id = _stable_route_action_id(
            "shell_component_nav" if is_shell_affordance else "observed_nav",
            f"{source_page_id}:{target_filename}:{label_key or trigger}",
        )
        binding_surface = (
            str(source_binding.get("surface") or "").strip()
            if is_shell_affordance
            else ""
        ) or _navigation_surface(trigger)
        source_scope = (
            str(source_binding.get("source_scope") or "").strip()
            if is_shell_affordance
            else ""
        ) or "exact_materialized_page"
        component_ref = str(
            source_binding.get("component_ref")
            or record.get("component_ref")
            or ""
        ).strip()
        entity = str(source_binding.get("entity") or record.get("entity") or "").strip()
        binding = {
            "binding_kind": binding_kind,
            "labels": labels,
            "surface": binding_surface,
            "source_scope": source_scope,
            "match_policy": str(source_binding.get("match_policy") or "exact_label_or_explicit_attribute"),
            "confidence": str(source_binding.get("confidence") or ("inferred" if is_shell_affordance else "observed")),
        }
        if component_ref:
            binding["component_ref"] = component_ref
        if entity:
            binding["entity"] = entity
        affordance = {
            "action_id": action_id,
            "navigation_kind": "navigate_to_target",
            "target_filename": target_filename,
            "target_source": "shell_component_affordance" if is_shell_affordance else "observed_navigation_affordance",
            "target_page_name": str(record.get("target_page_name") or ""),
            "trigger_text": trigger,
            "history_mode": "push",
            "monotonic": True,
            "source_page_id": source_page_id,
            "source_filename": source_filename,
            "target_page_id": target_page_id,
            "evidence_steps": evidence_steps,
            "reference_trace_ids": reference_trace_ids,
            "binding": binding,
        }
        if isinstance(record.get("route_binding_semantics"), dict):
            affordance["route_binding_semantics"] = record.get("route_binding_semantics")
        if is_shell_affordance:
            affordance["affordance_kind"] = "shell_component_navigation_affordance"
            if record.get("provenance"):
                affordance["provenance"] = record.get("provenance")
            if record.get("admission_reason"):
                affordance["admission_reason"] = record.get("admission_reason")
        affordances_by_page_id.setdefault(source_page_id, []).append(affordance)
    return {
        page_id: _resolve_observed_navigation_target_conflicts(actions)
        for page_id, actions in affordances_by_page_id.items()
    }


def _navigation_conflict_label_key(action: Dict[str, Any]) -> str:
    binding = action.get("binding", {}) if isinstance(action.get("binding"), dict) else {}
    labels = binding.get("labels", []) if isinstance(binding.get("labels"), list) else []
    label = next((str(item or "").strip() for item in labels if str(item or "").strip()), "")
    if not label:
        label = str(action.get("trigger_text") or "").strip()
    label = label.lower().replace("...", "").replace("…", "")
    label = re.sub(r"[\(\（\[\【][^\)\）\]\】]*$", "", label)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", label)


def _navigation_conflict_quality(action: Dict[str, Any]) -> Tuple[int, int]:
    binding = action.get("binding", {}) if isinstance(action.get("binding"), dict) else {}
    labels = binding.get("labels", []) if isinstance(binding.get("labels"), list) else []
    label = next((str(item or "").strip() for item in labels if str(item or "").strip()), "")
    if not label:
        label = str(action.get("trigger_text") or "").strip()
    dangling = 1 if re.search(r"[\(\（\[\【][^\)\）\]\】]*$", label) else 0
    truncated = 1 if ("..." in label or "…" in label) else 0
    normalized_label = _navigation_conflict_label_key(action)
    normalized_target = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "",
        str(action.get("target_page_name") or "").lower(),
    )
    target_match = 1 if normalized_label and normalized_target and normalized_label in normalized_target else 0
    target_source = str(action.get("target_source") or "")
    source_scope = str(binding.get("source_scope") or "")
    has_evidence = bool(action.get("evidence_steps"))
    if target_source == "task_interaction_contract":
        source_rank = 4
    elif target_source == "observed_navigation_affordance" and has_evidence:
        source_rank = 3
    elif target_source == "shell_component_affordance" and source_scope in {"alternative_route_access_plan", "shell_family_component"}:
        source_rank = 2
    else:
        source_rank = 1
    return (source_rank * 100 + target_match * 80 + len(normalized_label) - dangling * 30 - truncated * 20, len(label))


def _resolve_observed_navigation_target_conflicts(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    passthrough: List[Dict[str, Any]] = []
    for action in actions:
        binding = action.get("binding", {}) if isinstance(action.get("binding"), dict) else {}
        label_key = _navigation_conflict_label_key(action)
        if not label_key:
            passthrough.append(action)
            continue
        key = (
            str(binding.get("surface") or ""),
            str(binding.get("binding_kind") or ""),
            label_key,
        )
        grouped.setdefault(key, []).append(action)
    result = list(passthrough)
    for group in grouped.values():
        targets = {_filename_without_query(str(item.get("target_filename") or "")) for item in group}
        if len(targets) <= 1:
            result.extend(group)
            continue
        selected = dict(max(group, key=_navigation_conflict_quality))
        selected["conflict_resolution"] = {
            "policy": "prefer_complete_label_for_same_surface_target_conflict",
            "dropped_target_count": len(group) - 1,
        }
        result.append(selected)
    return result


def _stateful_action_match_score(
    stateful_action: Dict[str, Any],
    observed_action: Dict[str, Any],
) -> int:
    trigger_text = str(stateful_action.get("trigger_text") or "")
    observed_text = str(observed_action.get("trigger_text") or "")
    binding = observed_action.get("binding") if isinstance(observed_action.get("binding"), dict) else {}
    raw_labels = binding.get("labels", []) if isinstance(binding.get("labels", []), list) else []
    labels = [
        str(label or "").strip()
        for label in raw_labels
        if str(label or "").strip()
    ]
    score = 0
    quoted_values = set(_extract_quoted_values(trigger_text))
    if any(label and label in quoted_values for label in labels):
        score += 20
    elif any(label and label in trigger_text for label in labels):
        score += 8

    action_semantic = str(stateful_action.get("action_semantic") or "").lower()
    sdk_effect = stateful_action.get("sdk_effect") if isinstance(stateful_action.get("sdk_effect"), dict) else {}
    sdk_effect_text = str(sdk_effect.get("effect") or "").lower()
    if any(marker in f"{action_semantic} {sdk_effect_text}" for marker in ("select", "candidate", "choose")):
        score += 6

    stateful_tokens = set(_interaction_tokens(trigger_text))
    query_values = stateful_action.get("query_values", [])
    if isinstance(query_values, list):
        stateful_tokens.update(str(value or "").strip() for value in query_values if str(value or "").strip())
    if any(label and label in " ".join(stateful_tokens) for label in labels):
        score += 4
    observed_tokens = set(_interaction_tokens(observed_text))
    if stateful_tokens and observed_tokens:
        score += min(len(stateful_tokens.intersection(observed_tokens)), 4)
    return score


def _state_ref_domain_matches_trigger(state_ref: str, trigger_text: str) -> bool:
    ref = str(state_ref or "").strip().lower()
    trigger = str(trigger_text or "")
    if not ref:
        return True


    if not _state_ref_domain_markers_enabled():
        return True
    for ref_marker, trigger_markers in STATE_REF_DOMAIN_MARKERS.items():
        if ref_marker not in ref:
            continue
        return any(marker in trigger for marker in trigger_markers)
    return True


def _stateful_action_can_attach_to_observed_navigation(
    stateful_action: Dict[str, Any],
    observed_action: Dict[str, Any],
) -> bool:
    trigger_text = str(stateful_action.get("trigger_text") or "")
    observed_text = str(observed_action.get("trigger_text") or "")
    action_semantic = str(stateful_action.get("action_semantic") or "").lower()
    source_lifecycle = str(stateful_action.get("source_lifecycle") or "").lower()
    state_provenance = str(stateful_action.get("state_provenance") or "").lower()
    binding = observed_action.get("binding") if isinstance(observed_action.get("binding"), dict) else {}
    labels = [
        str(label or "").strip()
        for label in binding.get("labels", [])
        if str(label or "").strip()
    ] if isinstance(binding.get("labels", []), list) else []
    if action_semantic.startswith("select") or source_lifecycle == "collection" or state_provenance == "candidate_result":
        if labels and any(label and label in trigger_text for label in labels):
            return True
        if labels:
            return False
        stateful_tokens = set(_interaction_tokens(trigger_text))
        query_values = stateful_action.get("query_values", [])
        if isinstance(query_values, list):
            stateful_tokens.update(
                str(value or "").strip()
                for value in query_values
                if str(value or "").strip()
            )
        if stateful_tokens & set(_interaction_tokens(observed_text)):
            return True
        state_ref = str(stateful_action.get("state_ref") or "")
        if not _state_ref_domain_matches_trigger(state_ref, observed_text):
            return False
        return False
    state_ref = str(stateful_action.get("state_ref") or "")
    if not _state_ref_domain_matches_trigger(state_ref, observed_text):
        return False
    return True


def _matching_stateful_action(
    observed_action: Dict[str, Any],
    stateful_actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    scored = [
        (_stateful_action_match_score(action, observed_action), action)
        for action in stateful_actions
        if isinstance(action, dict) and _stateful_action_can_attach_to_observed_navigation(action, observed_action)
    ]
    scored = [(score, action) for score, action in scored if score > 0]
    if not scored:
        return {}
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _attach_stateful_commit_metadata(
    route_action: Dict[str, Any],
    stateful_action: Dict[str, Any],
) -> Dict[str, Any]:
    if not stateful_action:
        return route_action
    action = dict(route_action)
    action["stateful_transition"] = True
    action["stateful_action_id"] = stateful_action.get("action_id", "")
    action["state_commit"] = {
        "commit_state_ref": stateful_action.get("state_ref", ""),
        "entity_ref": stateful_action.get("entity_ref", ""),
        "state_provenance": stateful_action.get("state_provenance", ""),
        "source_lifecycle": stateful_action.get("source_lifecycle", ""),
        "sdk_effect": stateful_action.get("sdk_effect", {}),
        "must_apply_before_navigation": True,
    }
    for key in ("contract_id", "state_ref", "entity_ref", "state_provenance", "source_lifecycle", "sdk_effect"):
        value = stateful_action.get(key)
        if value not in ("", [], {}):
            action[key] = value
    return action


def _merge_observed_navigation_actions(
    route_contract: Dict[str, Any],
    observed_actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not observed_actions:
        return route_contract
    updated = dict(route_contract or {})
    actions = list(updated.get("actions", []) if isinstance(updated.get("actions", []), list) else [])
    navigation_actions = list(
        updated.get("navigation_actions", [])
        if isinstance(updated.get("navigation_actions", []), list)
        else []
    )
    stateful_actions = list(
        updated.get("stateful_actions", [])
        if isinstance(updated.get("stateful_actions", []), list)
        else []
    )
    existing_keys = {
        (
            str(action.get("navigation_kind") or ""),
            _filename_without_query(str(action.get("target_filename") or "")),
            "|".join(action.get("binding", {}).get("labels", []) if isinstance(action.get("binding"), dict) and isinstance(action.get("binding", {}).get("labels"), list) else [])
            or str(action.get("trigger_text") or ""),
        )
        for action in actions
        if isinstance(action, dict)
    }
    for observed in observed_actions:
        if not isinstance(observed, dict):
            continue
        binding = observed.get("binding") if isinstance(observed.get("binding"), dict) else {}
        labels = binding.get("labels", []) if isinstance(binding.get("labels"), list) else []
        key = (
            str(observed.get("navigation_kind") or ""),
            _filename_without_query(str(observed.get("target_filename") or "")),
            "|".join(str(label) for label in labels) or str(observed.get("trigger_text") or ""),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)
        action = _public_route_action(observed)
        action = _attach_stateful_commit_metadata(
            action,
            _matching_stateful_action(observed, stateful_actions),
        )
        actions.append(action)
        navigation_actions.append(action)
    updated["actions"] = actions
    if navigation_actions:
        updated["navigation_actions"] = navigation_actions
    constraints = dict(updated.get("constraints", {}) if isinstance(updated.get("constraints"), dict) else {})
    constraints["raw_task_navigation_allowed"] = False
    updated["constraints"] = constraints
    return updated


def _build_inbound_fallbacks(page_behaviors: Dict[str, Any], filename_by_page_id: Dict[str, str]) -> Dict[str, str]:
    fallbacks: Dict[str, str] = {}

    def remember(source_filename: str, target_filename: str) -> None:
        source = _filename_without_query(source_filename)
        target = _filename_without_query(target_filename)
        if not source or not target or source == target:
            return
        if target not in fallbacks:
            fallbacks[target] = source
            return
        current = fallbacks.get(target, "")
        target_family = _filename_family(target)
        source_same_family = bool(target_family and _filename_family(source) == target_family)
        current_same_family = bool(target_family and _filename_family(current) == target_family)
        if source_same_family and not current_same_family:
            fallbacks[target] = source

    for page_id, behavior in page_behaviors.items():
        behavior_dict = _as_plain_dict(behavior)
        if not behavior_dict:
            continue
        source_filename = _filename_without_query(
            str(behavior_dict.get("filename") or filename_by_page_id.get(str(page_id) or "") or "")
        )
        if not source_filename:
            continue
        commit_triggers = []
        for continuation in behavior_dict.get("task_continuations", []) if isinstance(behavior_dict.get("task_continuations", []), list) else []:
            if not isinstance(continuation, dict):
                continue
            if not _is_commit_interaction_contract(continuation.get("interaction_contract")):
                continue
            trigger_text = " ".join(
                str(continuation.get(key) or "")
                for key in ("element_name", "display_text", "action")
            ).strip()
            if trigger_text:
                commit_triggers.append(re.sub(r"\s+", " ", trigger_text))

        def is_commit_trigger(trigger: str) -> bool:
            text = re.sub(r"\s+", " ", str(trigger or "")).strip()
            if not text:
                return False
            return any(text in commit_trigger or commit_trigger in text for commit_trigger in commit_triggers)

        for nav in behavior_dict.get("navigation_out", []) if isinstance(behavior_dict.get("navigation_out", []), list) else []:
            if not isinstance(nav, dict):
                continue
            trigger = str(nav.get("trigger") or "")
            if _is_contextual_back_text(trigger) or is_commit_trigger(trigger):
                continue
            target = nav.get("to_filename") or filename_by_page_id.get(str(nav.get("to_page_id") or ""))
            remember(source_filename, str(target or ""))
        for continuation in behavior_dict.get("task_continuations", []) if isinstance(behavior_dict.get("task_continuations", []), list) else []:
            if not isinstance(continuation, dict):
                continue
            if _is_contextual_back_text(
                " ".join(
                    str(continuation.get(key) or "")
                    for key in ("element_name", "display_text", "action")
                )
            ):
                continue
            if _is_commit_interaction_contract(continuation.get("interaction_contract")):
                continue
            target = continuation.get("target_filename") or filename_by_page_id.get(str(continuation.get("target_page_id") or ""))
            remember(source_filename, str(target or ""))
    return fallbacks


def _component_policy(component_name: str) -> str:
    text = str(component_name or "").lower()
    if any(marker in text for marker in OPEN_ENDED_COMPONENT_MARKERS):
        return "appendable_feed"
    if any(marker in text for marker in FIXED_CAPACITY_COMPONENT_MARKERS):
        return "fixed_capacity"
    return "representative"


def _page_identity(proto: Any) -> Dict[str, str]:
    sd = getattr(proto, "structure_data", {}) if proto is not None else {}
    if not isinstance(sd, dict):
        sd = {}
    general_page_id = str(
        sd.get("source_cluster_id")
        or sd.get("cluster_id")
        or getattr(proto, "page_id", "")
        or ""
    ).strip()
    instance_key = str(sd.get("instance_key") or "").strip()
    materialized_page_id = (
        f"{general_page_id}::{instance_key}" if general_page_id and instance_key else str(getattr(proto, "page_id", "") or "")
    )
    return {
        "page_id": str(getattr(proto, "page_id", "") or ""),
        "filename": str(getattr(proto, "filename", "") or ""),
        "page_name": str(getattr(proto, "page_name", "") or ""),
        "page_type": str(getattr(proto, "page_type", "") or ""),
        "general_page_id": general_page_id,
        "instance_key": instance_key,
        "materialized_page_id": materialized_page_id,
    }


def _task_related_entities(content_data: Dict[str, Any]) -> set[str]:
    relevance = content_data.get("entity_task_relevance", {}) if isinstance(content_data, dict) else {}
    if not isinstance(relevance, dict):
        return set()
    result = set()
    for entity_name, details in relevance.items():
        if not isinstance(details, dict):
            continue
        if details.get("task_related") is True:
            result.add(str(entity_name))
    return result


def _entity_component_policies(content_data: Dict[str, Any]) -> Dict[str, str]:
    mapping = content_data.get("entity_component_mapping", {}) if isinstance(content_data, dict) else {}
    policies: Dict[str, str] = {}
    if not isinstance(mapping, list):
        return policies
    for item in mapping:
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity") or "").strip()
        component = str(item.get("source_component") or item.get("component") or "").strip()
        if not entity:
            continue
        policy = _component_policy(component)
        previous = policies.get(entity, "")
        if previous != "appendable_feed" or policy == "appendable_feed":
            policies[entity] = policy
    return policies


def _entity_component_mapping_by_entity(content_data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    mapping = content_data.get("entity_component_mapping", {}) if isinstance(content_data, dict) else {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    if not isinstance(mapping, list):
        return result
    for item in mapping:
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity") or "").strip()
        if entity:
            result.setdefault(entity, []).append(item)
    return result


def _page_has_appendable_surface(content_data: Dict[str, Any]) -> bool:
    return any(policy == "appendable_feed" for policy in _entity_component_policies(content_data).values())


def _is_control_entity(entity_name: str, relevance: Any) -> bool:
    entity_text = str(entity_name or "").lower()
    role = ""
    if isinstance(relevance, dict):
        role = str(relevance.get("role") or "").strip()
    if _is_query_state_entity(entity_name):
        return False
    if any(marker in entity_text for marker in CONTROL_ENTITY_MARKERS):
        return True
    return "state" in entity_text and role in CONTROL_ROLES


def _is_query_state_entity(entity_name: str) -> bool:
    entity_text = str(entity_name or "").lower()
    return any(marker in entity_text for marker in QUERY_STATE_MARKERS)


def _sibling_retention_policy(
    entity_name: str,
    relevance: Any,
    *,
    policy: str,
    is_control_entity: bool,
) -> str:

    if is_control_entity:
        return "visible_control"
    if _is_query_state_entity(entity_name):
        return "reachable_state"
    role = _relevance_role(relevance)
    if role in CONTROL_ROLES:
        return "visible_control"
    if policy == "appendable_feed":
        return "appendable_feed"
    return "reachable_materialized_page"


def _relevance_role(relevance: Any) -> str:
    if not isinstance(relevance, dict):
        return ""
    return str(relevance.get("role") or "").strip()


def _is_execution_anchor_entity(entity_name: str, relevance: Any) -> bool:
    role = _relevance_role(relevance)
    if role in EXECUTION_ANCHOR_ROLES:
        return True
    flags = (
        "execution_anchor",
        "trace_anchor",
        "l0_anchor",
        "must_preserve_l0",
        "state_display_required",
    )
    return any(bool(relevance.get(flag)) for flag in flags) if isinstance(relevance, dict) else False


def _allows_l0_required_text(
    entity_name: str,
    relevance: Any,
    *,
    policy: str,
    is_control_entity: bool,
    source_is_current_page: bool,
) -> bool:
    if is_control_entity:
        return True
    if _is_execution_anchor_entity(entity_name, relevance):
        return True
    if _is_query_state_entity(entity_name):
        return False
    if not source_is_current_page:
        return False
    return False


def _split_control_text(value: str) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"\s*(?:/|、|，|,|；|;|\|)\s*", text)]
    parts = [part for part in parts if _is_required_text_candidate(part)]
    return parts or ([text] if _is_required_text_candidate(text) else [])


def _extract_item_texts(item: Any, *, split_composite: bool = False) -> List[str]:
    if not isinstance(item, dict):
        return []
    values: List[str] = []
    for field in TEXT_FIELD_HINTS:
        value = item.get(field)
        if isinstance(value, str) and _is_required_text_candidate(value):
            if split_composite:
                values.extend(_split_control_text(value))
            else:
                values.append(value)
    for field, value in item.items():
        if not isinstance(value, str):
            continue
        field_text = str(field or "").lower()
        if field_text in TEXT_FIELD_HINTS:
            continue
        if any(field_text.startswith(prefix) for prefix in TEXT_FIELD_PREFIX_HINTS):
            if _is_required_text_candidate(value):
                if split_composite:
                    values.extend(_split_control_text(value))
                else:
                    values.append(value)
    return _dedupe(values, limit=6)


def _item_evidence_source(item: Any) -> str:
    if not isinstance(item, dict):
        return "visual_observed"
    for field in ("evidence_source_type", "source_evidence_type", "source_type", "evidence_type"):
        value = str(item.get(field) or "").strip()
        if value:
            return value.lower()
    provenance = item.get("provenance")
    if isinstance(provenance, dict):
        for field in ("evidence_source_type", "source_evidence_type", "source_type", "evidence_type"):
            value = str(provenance.get(field) or "").strip()
            if value:
                return value.lower()
    return "visual_observed"


def _item_allows_visible_required_text(item: Any) -> bool:
    source = _item_evidence_source(item)
    if not source:
        return True
    return not any(marker in source for marker in NON_VISUAL_EVIDENCE_SOURCES)


def _item_source_steps(item: Any) -> List[str]:
    if not isinstance(item, dict):
        return []
    values: List[str] = []
    for field in ("source_steps", "evidence_steps", "obs_keys", "source_obs_keys"):
        raw = item.get(field)
        if isinstance(raw, list):
            values.extend(str(value) for value in raw if str(value or "").strip())
        elif isinstance(raw, str) and raw.strip():
            values.append(raw)
    provenance = item.get("provenance")
    if isinstance(provenance, dict):
        for field in ("source_steps", "evidence_steps", "obs_keys", "source_obs_keys"):
            raw = provenance.get(field)
            if isinstance(raw, list):
                values.extend(str(value) for value in raw if str(value or "").strip())
            elif isinstance(raw, str) and raw.strip():
                values.append(raw)
    return _dedupe(values, limit=32)


def _record_text_values(item: Any) -> List[str]:
    if not isinstance(item, dict):
        return []
    values: List[str] = []
    for value in item.values():
        if isinstance(value, str) and _is_required_text_candidate(value):
            values.append(value)
        elif isinstance(value, (int, float)):
            text = str(value)
            if _is_required_text_candidate(text):
                values.append(text)
    values.extend(_extract_item_texts(item))
    return _dedupe(values, limit=16)


def _initial_texts_by_entity(content_data: Dict[str, Any]) -> Dict[str, set[str]]:
    extracted_items = content_data.get("extracted_items", {}) if isinstance(content_data, dict) else {}
    if not isinstance(extracted_items, dict):
        return {}
    result: Dict[str, set[str]] = {}
    for entity, items in extracted_items.items():
        if not isinstance(items, list):
            continue
        texts: set[str] = set()
        for item in items:
            texts.update(_record_text_values(item))
        result[str(entity)] = texts
    return result


def _runtime_texts_excluded_from_initial(content_data: Dict[str, Any]) -> Dict[str, List[str]]:
    if not isinstance(content_data, dict):
        return {}
    initial_texts = _initial_texts_by_entity(content_data)
    result: Dict[str, List[str]] = {}

    excluded = content_data.get("excluded_from_initial", [])
    for item in excluded if isinstance(excluded, list) else []:
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity") or "").strip()
        if not entity:
            continue
        hints = item.get("matched_hints", [])
        for hint in hints if isinstance(hints, list) else []:
            text = str(hint or "").strip()
            if _is_required_text_candidate(text):
                result.setdefault(entity, []).append(text)

    runtime_items = content_data.get("runtime_items", {})
    for entity, items in (runtime_items.items() if isinstance(runtime_items, dict) else []):
        entity_text = str(entity or "").strip()
        if not entity_text or not isinstance(items, list):
            continue
        initial = initial_texts.get(entity_text, set())
        for item in items:
            for text in _record_text_values(item):
                if text and text not in initial:
                    result.setdefault(entity_text, []).append(text)

    return {
        entity: _dedupe(values, limit=24)
        for entity, values in result.items()
        if _dedupe(values, limit=1)
    }


def _generic_clicked_record_trigger(entity_ref: str, target_page_name: str = "") -> str:
    entity = str(entity_ref or "记录").strip() or "记录"
    target = str(target_page_name or "").strip()
    suffix = f"，进入{target}" if target else ""
    return f"点击当前可见列表中的一条{entity}记录{suffix}"


def _sanitize_route_actions_for_content_lifecycle(
    route_contract: Dict[str, Any],
    content_data: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(route_contract, dict) or not route_contract:
        return route_contract
    forbidden_by_entity = _runtime_texts_excluded_from_initial(content_data)
    if not forbidden_by_entity:
        return route_contract

    def sanitize_action(action: Any) -> Dict[str, Any]:
        if not isinstance(action, dict):
            return action
        updated = dict(action)
        entity_ref = str(updated.get("entity_ref") or "").strip()
        candidate_entities = [entity_ref] if entity_ref else list(forbidden_by_entity.keys())
        trigger_text = str(updated.get("trigger_text") or "")
        matched = _dedupe(
            [
                text
                for entity in candidate_entities
                for text in forbidden_by_entity.get(entity, [])
                if text and text in trigger_text
            ],
            limit=12,
        )
        if not matched:
            return updated

        route_binding = updated.get("route_binding") if isinstance(updated.get("route_binding"), dict) else {}
        if str(route_binding.get("source") or "").strip() != "clicked_record":
            return updated

        updated["trigger_text"] = _generic_clicked_record_trigger(
            entity_ref,
            str(updated.get("_target_page_name") or updated.get("target_page_name") or "").strip(),
        )
        updated["selector_binding_contract"] = {
            "binding_kind": "record_row",
            "source": "clicked_record",
            "entity": entity_ref,
            "display_text_source": "current_visible_sdk_record",
            "identifier_source": "record.id or route_binding.params",
            "forbidden_initial_text": matched,
            "rationale": (
                "Post-commit/runtime record values must not be baked into the initial page selector; "
                "bind the action to the clicked visible row instead."
            ),
        }
        return updated

    updated_contract = dict(route_contract)
    actions = [sanitize_action(action) for action in updated_contract.get("actions", []) if isinstance(action, dict)]
    navigation_actions = [
        sanitize_action(action)
        for action in updated_contract.get("navigation_actions", [])
        if isinstance(action, dict)
    ]
    commit_actions = [
        sanitize_action(action)
        for action in updated_contract.get("commit_actions", [])
        if isinstance(action, dict)
    ]
    if actions:
        updated_contract["actions"] = actions
    if navigation_actions:
        updated_contract["navigation_actions"] = navigation_actions
    if commit_actions:
        updated_contract["commit_actions"] = commit_actions
    return updated_contract


def _entity_obligations_for_page(
    proto: Any,
    source_proto: Any,
    include_only_appendable: bool,
) -> List[Dict[str, Any]]:
    source_identity = _page_identity(source_proto)
    target_identity = _page_identity(proto)
    content_data = getattr(source_proto, "content_data", {}) or {}
    if not isinstance(content_data, dict):
        return []
    extracted_items = content_data.get("extracted_items", {})
    if not isinstance(extracted_items, dict):
        return []

    task_related = _task_related_entities(content_data)
    policies = _entity_component_policies(content_data)
    obligations: List[Dict[str, Any]] = []
    for entity_name, items in extracted_items.items():
        if entity_name == "_metadata" or not isinstance(items, list):
            continue
        if include_only_appendable and str(entity_name) not in task_related:
            continue
        relevance = (content_data.get("entity_task_relevance", {}) or {}).get(str(entity_name), {})
        if include_only_appendable and isinstance(relevance, dict):
            if str(relevance.get("role") or "").strip() == "form_value":
                continue
        is_control_entity = _is_control_entity(str(entity_name), relevance)
        if source_proto is not proto and _is_query_state_entity(str(entity_name)):
            is_control_entity = False
        policy = policies.get(str(entity_name), "representative")
        retention_policy = _sibling_retention_policy(
            str(entity_name),
            relevance,
            policy=policy,
            is_control_entity=is_control_entity,
        )
        if policy == "fixed_capacity" and source_proto is not proto:
            continue
        if (
            task_related
            and str(entity_name) not in task_related
            and policy != "appendable_feed"
            and source_proto is not proto
        ):
            continue
        source_is_current_page = source_proto is proto
        allows_l0_required_text = _allows_l0_required_text(
            str(entity_name),
            relevance,
            policy=policy,
            is_control_entity=is_control_entity,
            source_is_current_page=source_is_current_page,
        )
        if not allows_l0_required_text and source_is_current_page:
            continue

        texts: List[str] = []
        source_evidence_steps: List[str] = []
        for item in items:
            if not _item_allows_visible_required_text(item):
                continue
            texts.extend(_extract_item_texts(item, split_composite=is_control_entity))
            source_evidence_steps.extend(_item_source_steps(item))
        required_text_limit = 8 if policy == "appendable_feed" else 24
        required_text = _dedupe(texts, limit=required_text_limit)
        if not required_text:
            continue

        obligation_id = "l0:{target}:{source}:{entity}".format(
            target=target_identity["page_id"] or target_identity["filename"],
            source=source_identity["page_id"] or source_identity["filename"],
            entity=str(entity_name),
        )
        is_sibling = bool(include_only_appendable and source_proto is not proto)
        is_sibling_appendable = bool(is_sibling and retention_policy == "appendable_feed")
        obligation_type = "reachable_materialized_page" if is_sibling else "visible_item"
        if is_control_entity:
            obligation_type = "visible_control"
        is_reachability = obligation_type == "reachable_materialized_page"
        requires_validation = bool(
            is_sibling or (source_proto is proto and policy == "appendable_feed")
        )
        dedup_source_steps = _dedupe(source_evidence_steps, limit=32)
        component_ref = _component_ref_for_entity(content_data, str(entity_name))


        if is_control_entity or _is_execution_anchor_entity(str(entity_name), relevance):
            obligation_tier = "l0_execution"
        elif is_sibling_appendable or policy == "appendable_feed":
            obligation_tier = "section_render"
        else:
            obligation_tier = "visual_sample"
        obligation = {
            "obligation_id": obligation_id,
            "obligation_type": obligation_type,
            "required_text": [] if is_reachability else required_text,
            "evidence_text": required_text if is_reachability else [],
            "visible_text_source": "visual_observed",
            "source_evidence_steps": dedup_source_steps,
            "component_ref": component_ref,
            "layout_policy": policy,
            "expected_surface": (
                "reachable_materialized_page_link"
                if is_reachability
                else "same_page_control_region"
                if obligation_type == "visible_control"
                else "same_page_scrollable_region"
                if policy == "appendable_feed"
                else "current_materialized_page"
            ),
            "entity": str(entity_name),
            "source_page_id": source_identity["page_id"],
            "source_filename": source_identity["filename"],
            "source_general_page_id": source_identity["general_page_id"],
            "source_materialized_page_id": source_identity["materialized_page_id"],
            "access_target_filename": source_identity["filename"] if is_reachability else "",
            "access_target_materialized_page_id": source_identity["materialized_page_id"] if is_reachability else "",
            "access_label": source_identity["page_name"] if is_reachability else "",
            "validation_required": requires_validation,
            "tier": obligation_tier,
            "retention_policy": retention_policy if is_sibling else "source_page",
            "rationale": (
                "同 General Page 下的 sibling Materialized Page 内容只作为 L0 可达性义务；"
                "不把 sibling runtime state 的内容并入当前 Materialized Page 的 initial visible state。"
                if is_reachability
                else "模型抽取的 sibling control/state 属于当前 feed surface 的可执行控制义务；"
                "保留为当前 Materialized Page 的 visible_control，而不是降级为 sibling 页面链接。"
                if obligation_type == "visible_control"
                else "由模型抽取的任务相关内容和组件布局元数据派生；当前 Materialized Page 自有内容作为可见页面义务保留。"
            ),
        }


        mid_value = str(source_identity["materialized_page_id"] or "").strip()
        first_source_step = next(
            (step for step in dedup_source_steps if str(step or "").strip()),
            "",
        )
        if mid_value and first_source_step:
            attach_provenance(
                obligation,
                Provenance(
                    mid=mid_value,
                    source_step=first_source_step,
                    slot=component_ref if policy == "fixed_capacity" and component_ref else None,
                ),
            )
        obligations.append(obligation)
    return obligations


def _component_ref_for_entity(content_data: Dict[str, Any], entity_name: str) -> str:
    mapping = content_data.get("entity_component_mapping", {}) if isinstance(content_data, dict) else {}
    if not isinstance(mapping, list):
        return ""
    for item in mapping:
        if isinstance(item, dict) and str(item.get("entity") or "") == entity_name:
            return str(item.get("source_component") or item.get("component") or "")
    return ""


def _first_mapping_value(item: Any, mapping: Any, keys: Iterable[str]) -> str:
    for source in (item, mapping):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return str(value).strip()
    return ""


def _slot_conflict_key(component_ref: str, item: Any, mapping: Any) -> str:
    explicit = _first_mapping_value(item, mapping, ("slot_conflict_key", "conflict_key"))
    if explicit:
        return explicit
    slot = _first_mapping_value(
        item,
        mapping,
        (
            "slot_id",
            "slot_key",
            "component_slot",
            "layout_slot",
            "slot_index",
            "slot_position",
        ),
    )
    if not slot:
        return ""
    component = str(component_ref or _first_mapping_value(item, mapping, ("source_component", "component"))).strip()
    return f"{component}::{slot}" if component else slot


def _candidate_state_role(item: Any) -> str:
    role = _first_mapping_value(
        item,
        {},
        ("state_role", "render_role", "visibility_role", "l0_role"),
    )
    if role:
        return role
    if isinstance(item, dict):
        if any(bool(item.get(key)) for key in ("state_variant", "is_state_variant", "variant_of")):
            return "state_variant"
        if any(bool(item.get(key)) for key in ("default_candidate", "is_default")):
            return "default"
    return "default"


def _candidate_initial_dom_eligible(item: Any, state_role: str) -> bool:
    if isinstance(item, dict) and "initial_dom_eligible" in item:
        return bool(item.get("initial_dom_eligible"))
    role = str(state_role or "").lower()
    return "variant" not in role


def _activation_hint_for_candidate(
    item: Dict[str, Any],
    mapping: Dict[str, Any],
    state_role: str,
) -> str:

    state_ref = str(
        _first_mapping_value(item, mapping, ("state_ref", "state_id", "variant_ref")) or ""
    ).strip()
    if state_ref:
        return f"state_ref={state_ref}"
    route_param = item.get("route_param") if isinstance(item, dict) else None
    if isinstance(route_param, dict) and route_param:
        name = next(iter(route_param.keys()), "")
        if name:
            return f"route_param.{name}={route_param.get(name)}"
    query_param = item.get("query_param") if isinstance(item, dict) else None
    if isinstance(query_param, dict) and query_param:
        name = next(iter(query_param.keys()), "")
        if name:
            return f"query_param.{name}={query_param.get(name)}"
    role = str(state_role or "").strip().lower() or "variant"
    return f"state.{role}=true"


def _fixed_slot_conflicts_for_page(
    content_data: Dict[str, Any],
    source_identity: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:

    extracted_items = content_data.get("extracted_items", {}) if isinstance(content_data, dict) else {}
    if not isinstance(extracted_items, dict):
        return []
    policies = _entity_component_policies(content_data)
    mappings_by_entity = _entity_component_mapping_by_entity(content_data)
    task_relevance = content_data.get("entity_task_relevance", {}) if isinstance(content_data, dict) else {}
    task_related_set = _task_related_entities(content_data)
    grouped: Dict[str, Dict[str, Any]] = {}
    for entity_name, items in extracted_items.items():
        entity = str(entity_name or "").strip()
        if not entity or entity == "_metadata" or not isinstance(items, list):
            continue
        policy = policies.get(entity, "representative")
        implicit_selected_state = any(
            isinstance(item, dict)
            and str(item.get("is_active") or item.get("selected") or item.get("current") or "").strip().lower()
            in {"true", "1", "yes", "selected", "active", "current"}
            for item in items
        )
        if policy != "fixed_capacity" and not implicit_selected_state:
            continue
        relevance = task_relevance.get(entity, {}) if isinstance(task_relevance, dict) else {}
        is_l0_anchor = _is_execution_anchor_entity(entity, relevance) or _is_control_entity(entity, relevance)
        mappings = mappings_by_entity.get(entity) or [{}]
        for item in items:
            if not isinstance(item, dict):
                continue
            mapping = mappings[0]
            explicit_slot_key = _slot_conflict_key(component_ref := (
                _first_mapping_value(item, mapping, ("source_component", "component"))
                or _component_ref_for_entity(content_data, entity)
            ), item, mapping)
            if implicit_selected_state and not explicit_slot_key:
                selected_marker = str(
                    item.get("is_active") or item.get("selected") or item.get("current") or ""
                ).strip().lower()
                if selected_marker not in {"true", "1", "yes", "selected", "active", "current"}:
                    continue
            slot_key = explicit_slot_key
            if not slot_key and implicit_selected_state and component_ref:
                slot_key = f"{component_ref}::selected_state"
            if not slot_key and str((item.get("state_role") or "") if isinstance(item, dict) else "").strip() not in ("", "default") and component_ref:
                slot_key = f"{component_ref}::selected_state"
            if not slot_key:
                continue
            candidate_text = _extract_item_texts(item)
            if not candidate_text:
                continue
            state_role = _candidate_state_role(item)


            item_source_steps = _item_source_steps(item)
            first_source_step = next(
                (step for step in item_source_steps if str(step or "").strip()),
                "",
            )
            candidate = {
                "entity": entity,
                "candidate_text": candidate_text,
                "state_role": state_role,
                "state_ref": _first_mapping_value(item, mapping, ("state_ref", "state_id", "variant_ref")),
                "initial_dom_eligible": _candidate_initial_dom_eligible(item, state_role),
                "is_l0_anchor": bool(is_l0_anchor),


                "_source_step": first_source_step,
                "_item": item,
                "_mapping": mapping,
            }
            bucket = grouped.setdefault(
                slot_key,
                {
                    "slot_conflict_key": slot_key,
                    "component_ref": component_ref,
                    "layout_policy": "fixed_capacity",
                    "candidates": [],
                },
            )
            bucket["candidates"].append(candidate)

    mid_value = ""
    if isinstance(source_identity, dict):
        mid_value = str(source_identity.get("materialized_page_id") or "").strip()

    def _strip_internal(candidate: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in candidate.items()
            if not str(key).startswith("_")
        }

    def _attach_candidate_provenance(
        candidate: Dict[str, Any],
        *,
        slot_key: str,
    ) -> None:
        if not mid_value:
            return
        source_step = str(candidate.get("_source_step") or "").strip()
        if not source_step:
            return
        attach_provenance(
            candidate,
            Provenance(
                mid=mid_value,
                source_step=source_step,
                slot=slot_key or None,
            ),
        )

    conflicts: List[Dict[str, Any]] = []
    for slot_key in sorted(grouped):
        bucket = grouped[slot_key]
        candidates = bucket.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) < 2:
            continue
        default_index = 0


        for idx, candidate in enumerate(candidates):
            if candidate.get("is_l0_anchor") and candidate.get("initial_dom_eligible"):
                default_index = idx
                break
        else:
            for idx, candidate in enumerate(candidates):
                role = str(candidate.get("state_role") or "").lower()
                if candidate.get("initial_dom_eligible") and role in {"default", "current", "initial", "selected"}:
                    default_index = idx
                    break
        default_candidate = dict(candidates[default_index])
        _attach_candidate_provenance(default_candidate, slot_key=slot_key)


        if default_candidate.get("is_l0_anchor") and default_candidate.get("initial_dom_eligible"):
            default_candidate["selection_reason"] = "first_stable_l0_anchor"
        elif default_candidate.get("initial_dom_eligible"):
            default_candidate["selection_reason"] = "current_screenshot_default"
        else:
            default_candidate["selection_reason"] = "first_stable"
        default_candidate = _strip_internal(default_candidate)
        variants: List[Dict[str, Any]] = []
        l0_dropped_count = 0
        for idx, candidate in enumerate(candidates):
            if idx == default_index:
                continue
            entity = str(candidate.get("entity") or "")
            if entity not in task_related_set or not candidate.get("is_l0_anchor"):
                l0_dropped_count += 1
            variant = dict(candidate)
            if "variant" not in str(variant.get("state_role") or "").lower():
                variant["state_role"] = "state_variant"
            variant["initial_dom_eligible"] = False
            variant_item = variant.get("_item") or {}
            variant_mapping = variant.get("_mapping") or {}
            variant["activation_hint"] = _activation_hint_for_candidate(
                variant_item, variant_mapping, variant.get("state_role") or "",
            )
            _attach_candidate_provenance(variant, slot_key=slot_key)
            variants.append(_strip_internal(variant))
        if not variants and not l0_dropped_count:
            continue
        l0_anchor_count = sum(
            1 for candidate in candidates if candidate.get("is_l0_anchor")
        )
        resolution = "render_default_candidate_only"
        conflicts.append({
            "slot_conflict_key": bucket["slot_conflict_key"],
            "component_ref": bucket["component_ref"],
            "layout_policy": bucket["layout_policy"],
            "resolution": resolution,
            "default_candidate": default_candidate,
            "state_variant_candidates": variants,
            "l0_conflict": bool(l0_anchor_count >= 2),
            "l0_dropped_candidate_count": l0_dropped_count,
        })
    return conflicts


def l0_content_contributing_steps(contract: Dict[str, Any]) -> set[str]:
    steps: set[str] = set()
    policy = contract.get("content_selection_policy", {}) if isinstance(contract, dict) else {}
    for conflict in policy.get("fixed_slot_conflicts", []) if isinstance(policy, dict) else []:
        if not isinstance(conflict, dict):
            continue
        default = conflict.get("default_candidate", {})
        if isinstance(default, dict):
            prov = default.get("provenance", {})
            if isinstance(prov, dict) and prov.get("source_step"):
                steps.add(str(prov["source_step"]))
        for variant in conflict.get("state_variant_candidates", []) if isinstance(conflict.get("state_variant_candidates"), list) else []:
            if not isinstance(variant, dict):
                continue
            prov = variant.get("provenance", {})
            if isinstance(prov, dict) and prov.get("source_step"):
                steps.add(str(prov["source_step"]))
    for obligation in contract.get("l0_obligations", []) if isinstance(contract, dict) else []:
        if not isinstance(obligation, dict):
            continue
        for step in obligation.get("source_evidence_steps", []) if isinstance(obligation.get("source_evidence_steps"), list) else []:
            text = str(step or "").strip()
            if text:
                steps.add(text)
    return steps


def _merge_obligations(obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for obligation in obligations:
        key = "|".join([
            str(obligation.get("entity", "")),
            str(obligation.get("source_materialized_page_id", "")),
            str(obligation.get("layout_policy", "")),
        ])
        existing = merged.setdefault(key, dict(obligation))
        if existing is not obligation:
            existing["required_text"] = _dedupe(
                list(existing.get("required_text", [])) + list(obligation.get("required_text", [])),
                limit=32,
            )
            existing["evidence_text"] = _dedupe(
                list(existing.get("evidence_text", [])) + list(obligation.get("evidence_text", [])),
                limit=32,
            )
    return list(merged.values())


def build_page_execution_contracts(site_model: Any) -> Dict[str, Any]:
    structure = getattr(site_model, "structure", None)
    pages = list(getattr(structure, "page_prototypes", []) or [])
    page_behaviors = getattr(structure, "page_behaviors", {}) or {}
    if not isinstance(page_behaviors, dict):
        page_behaviors = {}
    filename_by_page_id = {
        str(getattr(proto, "page_id", "") or ""): str(getattr(proto, "filename", "") or "")
        for proto in pages
        if str(getattr(proto, "page_id", "") or "") and str(getattr(proto, "filename", "") or "")
    }
    page_name_by_page_id = {
        str(getattr(proto, "page_id", "") or ""): str(getattr(proto, "page_name", "") or "")
        for proto in pages
        if str(getattr(proto, "page_id", "") or "") and str(getattr(proto, "page_name", "") or "")
    }
    content = getattr(site_model, "content", None)
    content_samples = getattr(content, "content_samples", {}) if content is not None else {}
    if not isinstance(content_samples, dict):
        content_samples = {}
    submit_targets_by_query = _build_submit_targets_by_query(page_behaviors, filename_by_page_id, page_name_by_page_id)
    all_filenames = [filename for filename in filename_by_page_id.values() if filename]
    inbound_fallback_by_filename = _build_inbound_fallbacks(page_behaviors, filename_by_page_id)
    graph_route_meta_by_page_id = _route_metadata_from_site_graph(structure)
    observed_navigation_affordances_by_page_id = _build_observed_navigation_affordances(site_model)
    route_meta_by_filename: Dict[str, Dict[str, Any]] = {}
    for proto in pages:
        identity = _page_identity(proto)
        filename = _filename_without_query(identity["filename"])
        if not filename:
            continue
        proto_meta = _route_metadata_for_proto(proto)
        graph_meta = graph_route_meta_by_page_id.get(identity["page_id"], {})
        merged_meta = _merge_route_metadata(proto_meta, graph_meta)
        merged_meta["filename"] = filename
        merged_meta["page_id"] = identity["page_id"]
        merged_meta["general_page_id"] = identity["general_page_id"]
        merged_meta["materialized_page_id"] = identity["materialized_page_id"]
        route_meta_by_filename[filename] = merged_meta
    fallback_by_filename: Dict[str, str] = {}
    by_general: Dict[str, List[Any]] = {}
    for proto in pages:
        identity = _page_identity(proto)
        if identity["general_page_id"]:
            by_general.setdefault(identity["general_page_id"], []).append(proto)

    navigation_policy_by_filename: Dict[str, Dict[str, Any]] = {}
    route_execution_contracts_by_filename: Dict[str, Dict[str, Any]] = {}
    for proto in pages:
        identity = _page_identity(proto)
        if not identity["filename"]:
            continue
        content_data = getattr(proto, "content_data", {}) or {}
        filename_key = _filename_without_query(identity["filename"])
        navigation_policy = _navigation_policy_for_page(
            identity,
            page_behaviors.get(identity["page_id"], {}),
            filename_by_page_id,
            all_filenames,
            route_meta_by_filename,
            fallback_by_filename,
        )
        inbound_fallback = inbound_fallback_by_filename.get(filename_key, "")
        if inbound_fallback:
            inbound_target_meta = route_meta_by_filename.get(inbound_fallback, {})
            inbound_back_proof = _back_proof_for_route_fallback(
                route_meta_by_filename.get(filename_key, {}),
                inbound_target_meta,
            )
            inbound_hint = {
                "target_filename": inbound_fallback,
                "source": "inbound_navigation",
            }
            if not navigation_policy:
                navigation_policy = {"prefer_history_for_generic_back": False, "generic_back_targets": []}
                if inbound_back_proof:
                    navigation_policy.update({
                        "safe_same_family_fallback": inbound_fallback,
                        "stack_navigation_policy": {
                            "strategy": "deterministic_inbound_fallback",
                            "fallback_target_filename": inbound_fallback,
                            "fallback_source": "inbound_navigation",
                            "monotonic": True,
                            "back_proof": inbound_back_proof,
                        },
                    })
                else:
                    navigation_policy.update({
                        "safe_same_family_fallback": "",
                        "backtrack_mode": "history_only_hint",
                        "history_only_hint": inbound_hint,
                    })
            else:
                navigation_policy["prefer_history_for_generic_back"] = False
                stack_policy = navigation_policy.get("stack_navigation_policy")
                if not isinstance(stack_policy, dict) or not stack_policy:
                    if inbound_back_proof:
                        navigation_policy["safe_same_family_fallback"] = (
                            str(navigation_policy.get("safe_same_family_fallback") or "").strip()
                            or inbound_fallback
                        )
                        navigation_policy["stack_navigation_policy"] = {
                            "strategy": "deterministic_inbound_fallback",
                            "fallback_target_filename": inbound_fallback,
                            "fallback_source": "inbound_navigation",
                            "monotonic": True,
                            "back_proof": inbound_back_proof,
                        }
                    else:
                        navigation_policy["safe_same_family_fallback"] = (
                            str(navigation_policy.get("safe_same_family_fallback") or "").strip()
                        )
                        if not navigation_policy["safe_same_family_fallback"]:
                            navigation_policy["backtrack_mode"] = "history_only_hint"
                            navigation_policy["history_only_hint"] = inbound_hint
        route_meta = route_meta_by_filename.get(filename_key, {})
        route_execution_contract = _route_execution_contract_for_page(
            identity,
            route_meta,
            navigation_policy,
            page_behaviors.get(identity["page_id"], {}),
            filename_by_page_id,
            page_name_by_page_id,
            route_meta_by_filename,
        )
        route_execution_contract = _merge_observed_navigation_actions(
            route_execution_contract,
            observed_navigation_affordances_by_page_id.get(identity["page_id"], []),
        )
        route_execution_contract = _sanitize_route_actions_for_content_lifecycle(
            route_execution_contract,
            content_data,
        )
        navigation_policy_by_filename[filename_key] = navigation_policy
        if route_execution_contract:
            existing_route_contract = route_execution_contracts_by_filename.get(filename_key, {})
            route_execution_contracts_by_filename[filename_key] = _merge_observed_navigation_actions(
                existing_route_contract or route_execution_contract,
                (
                    route_execution_contract.get("actions", [])
                    if existing_route_contract and isinstance(route_execution_contract.get("actions"), list)
                    else []
                ),
            )

    state_display_bindings_by_target = _merge_state_display_bindings_from_route_contracts(
        _build_state_display_bindings_by_target(
            page_behaviors,
            filename_by_page_id,
        ),
        route_execution_contracts_by_filename,
    )

    page_contracts: List[Dict[str, Any]] = []
    pages_by_filename: Dict[str, Dict[str, Any]] = {}
    for proto in pages:
        identity = _page_identity(proto)
        if not identity["filename"]:
            continue
        content_data = getattr(proto, "content_data", {}) or {}
        own_obligations = _entity_obligations_for_page(proto, proto, include_only_appendable=False)
        obligations = list(own_obligations)
        if _page_has_appendable_surface(content_data):
            for sibling in by_general.get(identity["general_page_id"], []):
                if sibling is proto:
                    continue
                obligations.extend(_entity_obligations_for_page(proto, sibling, include_only_appendable=True))
        filename_key = _filename_without_query(identity["filename"])
        navigation_policy = navigation_policy_by_filename.get(filename_key, {})

        route_meta = route_meta_by_filename.get(_filename_without_query(identity["filename"]), {})
        fixed_slot_conflicts = _fixed_slot_conflicts_for_page(content_data, identity)
        content_selection_policy = {
            "must_preserve_l0_items": True,
            "appendable_surface_detected": _page_has_appendable_surface(content_data),
        }
        if fixed_slot_conflicts:
            content_selection_policy["fixed_slot_conflicts"] = fixed_slot_conflicts
        contract = {
            "filename": identity["filename"],
            "page_id": identity["page_id"],
            "page_name": identity["page_name"],
            "page_type": identity["page_type"],
            "general_page_id": identity["general_page_id"],
            "materialized_page_id": identity["materialized_page_id"],
            "instance_key": identity["instance_key"],
            "route_context": {
                "route_path": route_meta.get("route_path", ""),
                "parent_route_path": route_meta.get("parent_route_path", ""),
                "route_level": route_meta.get("route_level", 0),
            },
            "content_selection_policy": content_selection_policy,
            "l0_obligations": _merge_obligations(obligations),
        }
        route_execution_contract = route_execution_contracts_by_filename.get(filename_key, {})
        if route_execution_contract:
            contract["route_execution_contract"] = route_execution_contract
        page_content_samples = _merge_content_samples(
            content_samples,
            _content_samples_from_page_content(content_data),
        )
        query_state_contract = _query_state_contract_for_page(
            identity,
            page_behaviors.get(identity["page_id"], {}),
            page_content_samples,
            submit_targets_by_query,
        )
        input_surface_contract: Dict[str, Any] = {}
        if query_state_contract:
            contract["query_state_contract"] = query_state_contract
            input_surface_contract = _input_surface_contract_for_page(identity, query_state_contract)
            if input_surface_contract:
                contract["input_surface_contract"] = input_surface_contract
        dynamic_surface_contract = _dynamic_surface_contract_for_page(
            identity,
            query_state_contract,
            input_surface_contract,
            page_behaviors.get(identity["page_id"], {}),
            state_display_bindings_by_target.get(_filename_without_query(identity["filename"]), []),
        )
        if dynamic_surface_contract:
            contract["dynamic_surface_contract"] = dynamic_surface_contract
        if navigation_policy:
            contract["navigation_policy"] = navigation_policy
        page_contracts.append(contract)
        pages_by_filename[identity["filename"]] = contract

    return {
        "format": "page_execution_contracts",
        "contract_version": 1,
        "pages": page_contracts,
        "pages_by_filename": pages_by_filename,
    }


def build_route_execution_contracts(page_execution_contracts: Dict[str, Any]) -> Dict[str, Any]:
    pages = []
    pages_by_filename = {}
    for page in page_execution_contracts.get("pages", []) if isinstance(page_execution_contracts, dict) else []:
        if not isinstance(page, dict):
            continue
        contract = page.get("route_execution_contract")
        if not isinstance(contract, dict) or not contract:
            continue
        item = dict(contract)
        filename = _filename_without_query(str(item.get("filename") or page.get("filename") or ""))
        if not filename:
            continue
        item["filename"] = filename
        pages.append(item)
        pages_by_filename[filename] = item
    return {
        "format": "route_execution_contracts",
        "schema_revision": 1,
        "pages": pages,
        "pages_by_filename": pages_by_filename,
    }


def build_route_execution_contracts_from_page_environment_specs(
    page_environment_specs: Dict[str, Any],
) -> Dict[str, Any]:
    pages: List[Dict[str, Any]] = []
    pages_by_filename: Dict[str, Dict[str, Any]] = {}
    specs_by_filename = (
        page_environment_specs.get("page_environment_specs_by_filename", {})
        if isinstance(page_environment_specs, dict)
        else {}
    )
    if not isinstance(specs_by_filename, dict):
        specs_by_filename = {}
    for raw_filename, page_env in specs_by_filename.items():
        if not isinstance(page_env, dict):
            continue
        filename = _filename_without_query(str(page_env.get("filename") or raw_filename or ""))
        if not filename:
            continue
        actions: List[Dict[str, Any]] = []
        seen_action_keys: set[tuple[str, str]] = set()
        for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            state_effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            route_contract = transition.get("route_contract") if isinstance(transition.get("route_contract"), dict) else {}
            target = _filename_without_query(
                str(
                    state_effect.get("target_filename")
                    or transition.get("target_filename")
                    or transition.get("to_filename")
                    or route_contract.get("target_filename")
                    or ""
                )
            )
            if not target:
                continue
            action_id = str(
                transition.get("action_id")
                or transition.get("transition_id")
                or transition.get("step_key")
                or f"{filename}::{target}"
            )
            action_key = (action_id, target)
            if action_key in seen_action_keys:
                continue
            seen_action_keys.add(action_key)
            trigger_text = str(
                transition.get("trigger_text")
                or transition.get("action_label")
                or transition.get("intent")
                or transition.get("element_text")
                or ""
            ).strip()
            actions.append({
                "action_id": action_id,
                "target_filename": target,
                "target_source": "page_environment_spec.transition",
                "trigger_text": trigger_text,
                "surface": str(transition.get("component_id") or transition.get("surface") or ""),
                "binding": {
                    "binding_kind": str(transition.get("operation") or transition.get("reducer") or ""),
                    "labels": [trigger_text] if trigger_text else [],
                    "surface": str(transition.get("component_id") or transition.get("surface") or ""),
                    "component_ref": str(transition.get("component_id") or ""),
                },
                "state_effect": state_effect,
                "route_contract": route_contract,
                "evidence_steps": [
                    str(step)
                    for step in transition.get("evidence_steps", [])
                    if str(step or "").strip()
                ] if isinstance(transition.get("evidence_steps"), list) else [],
                "source": "page_environment_spec.transitions",
            })
        for affordance in page_env.get("route_affordances", []) if isinstance(page_env.get("route_affordances"), list) else []:
            if not isinstance(affordance, dict):
                continue
            source = str(affordance.get("source") or "").strip()
            if source != "shell_interaction_broadcast":
                continue
            target = _filename_without_query(str(affordance.get("target_filename") or ""))
            if not target or target == filename:
                continue
            action_id = str(
                affordance.get("action_id")
                or f"shell_propagated::{filename}::{target}"
            ).strip()
            action_key = (action_id, target)
            if action_key in seen_action_keys:
                continue
            seen_action_keys.add(action_key)
            trigger_text = str(
                affordance.get("control_label")
                or affordance.get("label")
                or affordance.get("trigger_text")
                or ""
            ).strip()
            surface = str(
                affordance.get("shell_family_id")
                or affordance.get("surface")
                or ""
            ).strip()
            actions.append({
                "action_id": action_id,
                "target_filename": target,
                "target_source": "shell_interaction_broadcast.route_affordance",
                "trigger_text": trigger_text,
                "surface": surface,
                "binding": {
                    "binding_kind": "click",
                    "labels": [trigger_text] if trigger_text else [],
                    "surface": surface,
                    "component_ref": str(affordance.get("component_id") or ""),
                },
                "state_effect": {
                    "effect": "navigate_route",
                    "target_filename": target,
                    "mutation_policy": "route_transition_only",
                    "source": "shell_interaction_broadcast",
                },
                "route_contract": {
                    "target_filename": target,
                    "source": "shell_interaction_broadcast",
                    "shell_family_id": str(affordance.get("shell_family_id") or ""),
                },
                "evidence_steps": [
                    str(step)
                    for step in affordance.get("evidence_steps", [])
                    if str(step or "").strip()
                ] if isinstance(affordance.get("evidence_steps"), list) else [],
                "source": "shell_interaction_broadcast.route_affordances",
            })
        item = {
            "filename": filename,
            "page_id": str(page_env.get("page_id") or ""),
            "page_name": str(page_env.get("page_name") or ""),
            "general_page_id": str(page_env.get("general_page_id") or ""),
            "materialized_page_id": str(page_env.get("materialized_page_id") or ""),
            "navigation_actions": actions,
            "actions": actions,
            "source": "page_environment_spec",
        }
        pages.append(item)
        pages_by_filename[filename] = item
    return {
        "format": "route_execution_contracts",
        "schema_revision": 2,
        "source": "page_environment_specs",
        "pages": pages,
        "pages_by_filename": pages_by_filename,
    }


def build_input_surface_contracts(page_execution_contracts: Dict[str, Any]) -> Dict[str, Any]:
    surfaces: List[Dict[str, Any]] = []
    surfaces_by_id: Dict[str, Dict[str, Any]] = {}
    for page in page_execution_contracts.get("pages", []) if isinstance(page_execution_contracts, dict) else []:
        if not isinstance(page, dict):
            continue
        surface = page.get("input_surface_contract")
        if not isinstance(surface, dict) or not surface:
            continue
        surface_id = str(surface.get("surface_id") or "").strip()
        if not surface_id:
            continue
        surfaces.append(surface)
        surfaces_by_id[surface_id] = surface
    return {
        "format": "input_surface_contracts",
        "schema_revision": 1,
        "surfaces": surfaces,
        "surfaces_by_id": surfaces_by_id,
    }


def build_dynamic_surface_contracts(page_execution_contracts: Dict[str, Any]) -> Dict[str, Any]:
    surfaces: List[Dict[str, Any]] = []
    surfaces_by_page_id: Dict[str, Dict[str, Any]] = {}
    surfaces_by_materialized_page_id: Dict[str, Dict[str, Any]] = {}
    for page in page_execution_contracts.get("pages", []) if isinstance(page_execution_contracts, dict) else []:
        if not isinstance(page, dict):
            continue
        surface = page.get("dynamic_surface_contract")
        if not isinstance(surface, dict) or not surface:
            continue
        item = dict(surface)
        surfaces.append(item)
        page_id = str(item.get("page_id") or page.get("page_id") or "").strip()
        materialized_page_id = str(
            item.get("materialized_page_id") or page.get("materialized_page_id") or ""
        ).strip()
        if page_id:
            surfaces_by_page_id[page_id] = item
        if materialized_page_id:
            surfaces_by_materialized_page_id[materialized_page_id] = item
    return {
        "format": "dynamic_surface_contracts",
        "schema_revision": 1,
        "surfaces": surfaces,
        "surfaces_by_page_id": surfaces_by_page_id,
        "surfaces_by_materialized_page_id": surfaces_by_materialized_page_id,
    }


def page_execution_contract_for_filename(contracts: Dict[str, Any], filename: str) -> Dict[str, Any]:
    if not isinstance(contracts, dict):
        return {}
    pages_by_filename = contracts.get("pages_by_filename", {})
    if isinstance(pages_by_filename, dict):
        contract = pages_by_filename.get(filename, {})
        return contract if isinstance(contract, dict) else {}
    for page in contracts.get("pages", []) if isinstance(contracts.get("pages", []), list) else []:
        if isinstance(page, dict) and page.get("filename") == filename:
            return page
    return {}


def _visible_html_for_text_matching(html_text: str) -> str:
    text = str(html_text or "")
    text = re.sub(r"(?is)<head\b.*?</head>", "", text)
    text = re.sub(r"(?is)<script\b.*?</script>", "", text)
    text = re.sub(r"(?is)<style\b.*?</style>", "", text)
    text = re.sub(r"(?is)<!--.*?-->", "", text)
    return text


def _normalize_validation_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("…", "...")
    return re.sub(r"\s+", " ", text).strip()


def _visible_text_for_text_matching(html_text: str) -> str:
    visible_html = _visible_html_for_text_matching(html_text)
    text = re.sub(r"(?is)<[^>]+>", " ", visible_html)
    return _normalize_validation_text(text)


def _script_html_for_text_matching(html_text: str) -> str:
    text = str(html_text or "")
    return "\n".join(re.findall(r"(?is)<script\b[^>]*>(.*?)</script>", text))


def _has_dynamic_appendable_feed_text(html_text: str, required_text: str) -> bool:
    text = str(required_text or "").strip()
    if not text:
        return False
    script_text = _script_html_for_text_matching(html_text)
    if text not in script_text:
        return False
    if not re.search(r"\b(?:innerHTML|insertAdjacentHTML)\b", script_text):
        return False
    if not re.search(r"\b(?:render|load)[A-Za-z_$][\w$]*\s*\(", script_text):
        return False
    if not re.search(r"\b(?:map|forEach)\s*\(", script_text):
        return False
    return True


def _compact_runtime_binding(action: Dict[str, Any], *, allow_trigger_fallback: bool = True) -> Dict[str, Any]:
    binding = action.get("binding", {}) if isinstance(action, dict) else {}
    result: Dict[str, Any] = {}
    if isinstance(binding, dict):
        for key in (
            "binding_kind",
            "match_policy",
            "surface",
            "source_scope",
            "identifier_source",
            "component_ref",
            "component_role",
            "entity",
            "confidence",
        ):
            value = binding.get(key)
            if value not in (None, "", [], {}):
                result[key] = value
        labels = [
            re.sub(r"\s+", " ", str(label or "").strip())
            for label in binding.get("labels", [])
            if str(label or "").strip()
        ] if isinstance(binding.get("labels"), list) else []
    else:
        labels = []
    if not labels and allow_trigger_fallback:
        labels = _navigation_label_candidates(str(action.get("trigger_text") or ""))
    labels = [label for label in labels if label and len(label) <= 40]
    if labels:
        result["labels"] = sorted(dict.fromkeys(labels))
    return result


def _compact_selector_binding_contract(action: Dict[str, Any]) -> Dict[str, Any]:
    contract = action.get("selector_binding_contract", {}) if isinstance(action, dict) else {}
    if not isinstance(contract, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in (
        "binding_kind",
        "source",
        "entity",
        "display_text_source",
        "identifier_source",
    ):
        value = contract.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    forbidden = contract.get("forbidden_initial_text")
    if isinstance(forbidden, list):
        count = len([item for item in forbidden if str(item or "").strip()])
        if count:
            result["forbidden_initial_text_count"] = count
    count_value = contract.get("forbidden_initial_text_count")
    if count_value not in (None, "", [], {}):
        try:
            count_int = int(count_value)
        except Exception:
            count_int = 0
        if count_int > 0:
            result["forbidden_initial_text_count"] = count_int
    return result


def _compact_prompt_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in (
        "action_id",
        "navigation_kind",
        "target_filename",
        "target_source",
        "history_mode",
        "monotonic",
        "contract_id",
        "state_ref",
        "entity_ref",
        "action_semantic",
        "interaction_kind",
        "source_lifecycle",
        "state_provenance",
    ):
        value = action.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    for key in ("route_binding", "state_commit", "sdk_effect"):
        value = action.get(key)
        if isinstance(value, dict) and value:
            result[key] = value
    binding = _compact_runtime_binding(action, allow_trigger_fallback=False)
    if binding:
        result["binding"] = binding
    selector_binding = _compact_selector_binding_contract(action)
    if selector_binding:
        result["selector_binding_contract"] = selector_binding
    query_values = action.get("query_values")
    if isinstance(query_values, list):
        clean_values = [str(item).strip() for item in query_values if str(item or "").strip()]
        if clean_values:
            result["query_values"] = clean_values[:8]
    return result


def _compact_route_contract_for_prompt(route_contract: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(route_contract, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in ("format", "schema_revision", "page_id", "filename", "route_path", "route_level", "constraints"):
        value = route_contract.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    for key in ("actions", "navigation_actions", "commit_actions", "stateful_actions"):
        actions = route_contract.get(key)
        if not isinstance(actions, list):
            continue
        compact_actions = [_compact_prompt_action(item) for item in actions if isinstance(item, dict)]
        compact_actions = [item for item in compact_actions if item]
        if compact_actions:
            result[key] = compact_actions
    default_back = _compact_runtime_back_action(route_contract.get("default_back_action", {}))
    if default_back:
        result["default_back_action"] = default_back
    fallback_back = _compact_runtime_back_action(route_contract.get("fallback_back_action", {}))
    if fallback_back:
        result["fallback_back_action"] = fallback_back
    return result


def _compact_runtime_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in (
        "action_id",
        "navigation_kind",
        "target_filename",
        "target_source",
        "history_mode",
        "monotonic",
        "contract_id",
        "state_ref",
        "entity_ref",
        "action_semantic",
        "interaction_kind",
        "source_lifecycle",
        "state_provenance",
    ):
        value = action.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    for key in ("route_binding", "state_commit", "sdk_effect"):
        value = action.get(key)
        if isinstance(value, dict) and value:
            result[key] = value
    binding = _compact_runtime_binding(action)
    if binding:
        result["binding"] = binding
    query_values = action.get("query_values")
    if isinstance(query_values, list):
        clean_values = [str(item).strip() for item in query_values if str(item or "").strip()]
        if clean_values:
            result["query_values"] = clean_values[:8]
    return result


def _compact_runtime_back_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in ("action_id", "navigation_kind", "target_filename", "target_source", "monotonic"):
        value = action.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    back_proof = action.get("back_proof")
    if isinstance(back_proof, dict) and back_proof:
        result["back_proof"] = back_proof
    return result


def _compact_route_contract_for_runtime(route_contract: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(route_contract, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in ("format", "schema_revision", "page_id", "filename", "route_path", "route_level", "constraints"):
        value = route_contract.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    actions = route_contract.get("actions", [])
    if isinstance(actions, list):
        compact_actions = [_compact_runtime_action(item) for item in actions if isinstance(item, dict)]
        compact_actions = [item for item in compact_actions if item]
        if compact_actions:
            result["actions"] = compact_actions
    stateful_actions = route_contract.get("stateful_actions", [])
    if isinstance(stateful_actions, list):
        compact_stateful = [_compact_runtime_action(item) for item in stateful_actions if isinstance(item, dict)]
        compact_stateful = [item for item in compact_stateful if item]
        if compact_stateful:
            result["stateful_actions"] = compact_stateful
    default_back = _compact_runtime_back_action(route_contract.get("default_back_action", {}))
    if default_back:
        result["default_back_action"] = default_back
    fallback_back = _compact_runtime_back_action(route_contract.get("fallback_back_action", {}))
    if fallback_back:
        result["fallback_back_action"] = fallback_back
    return result


def _route_execution_runtime_script(route_contract: Dict[str, Any]) -> str:
    runtime_contract = _compact_route_contract_for_runtime(route_contract or {})
    payload = json.dumps(runtime_contract, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return f"""
<script data-ghostweb-runtime="route-execution">
(function () {{
  const CONTRACT = {payload};
  const STACK_KEY = 'ghostweb_route_stack';
  const CURRENT_KEY = 'ghostweb_current_page';
  const LAST_BACK_FROM_KEY = 'ghostweb_last_back_from';
  const PENDING_COMMIT_KEY = 'ghostweb_pending_commit_navigation';
  const STATE_COMMITS_KEY = 'ghostweb_state_commits';
  const RUNTIME_STATE_KEY = 'ghostweb_runtime_state_v1';
  const RUNTIME_EVENTS_KEY = 'ghostweb_runtime_events_v1';
  const MAX_STACK = 64;
  const actions = Array.isArray(CONTRACT && CONTRACT.actions) ? CONTRACT.actions : [];
  const statefulActions = Array.isArray(CONTRACT && CONTRACT.stateful_actions) ? CONTRACT.stateful_actions : [];
  const commitTargets = new Set(actions
    .filter(function (action) {{ return action && action.navigation_kind === 'commit_to_target' && action.target_filename; }})
    .map(function (action) {{ return String(action.target_filename || '').split('?')[0].split('#')[0]; }}));
  const fallbackBack = CONTRACT && (CONTRACT.fallback_back_action || CONTRACT.default_back_action)
    ? (CONTRACT.fallback_back_action || CONTRACT.default_back_action)
    : {{}};
  const defaultBackTarget = String(fallbackBack.target_filename || '').trim();
  function readRuntimeStateStore() {{
    try {{
      const parsed = JSON.parse(window.sessionStorage.getItem(RUNTIME_STATE_KEY) || '{{}}');
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {{}};
    }} catch (e) {{
      return {{}};
    }}
  }}
  function writeRuntimeStateStore(store) {{
    try {{
      window.sessionStorage.setItem(RUNTIME_STATE_KEY, JSON.stringify(store || {{}}));
    }} catch (e) {{}}
  }}
  function appendRuntimeEvent(event) {{
    try {{
      let existing = JSON.parse(window.sessionStorage.getItem(RUNTIME_EVENTS_KEY) || '[]');
      if (!Array.isArray(existing)) existing = [];
      existing.push(event);
      window.sessionStorage.setItem(RUNTIME_EVENTS_KEY, JSON.stringify(existing.slice(-256)));
    }} catch (e) {{}}
  }}
  function installRuntimeStateAuthority() {{
    const existing = window.GhostwebRuntimeState || {{}};
    let existingSnapshot = null;
    try {{
      existingSnapshot = typeof existing.snapshot === 'function' ? existing.snapshot() : null;
    }} catch (e) {{}}
    if (
      existingSnapshot &&
      typeof existingSnapshot === 'object' &&
      !Array.isArray(existingSnapshot) &&
      existingSnapshot.current_state &&
      existingSnapshot.committed_state &&
      typeof existing.setTraceId === 'function' &&
      typeof existing.observeDomState === 'function'
    ) {{
      return existing;
    }}
    window.GhostwebRuntimeState = Object.assign(existing, {{
      write: function (ref, value, meta) {{
        const key = String(ref || '').trim();
        if (!key) return false;
        const store = readRuntimeStateStore();
        store[key] = value;
        writeRuntimeStateStore(store);
        const event = Object.assign({{
          event: 'runtime_state_written',
          event_type: 'runtime_state_written',
          ref: key,
          value: value,
          timestamp: Date.now()
        }}, meta || {{}});
        appendRuntimeEvent(event);
        window.__ghostwebLastRuntimeStateEvent = event;
        return true;
      }},
      read: function (ref) {{
        return readRuntimeStateStore()[String(ref || '').trim()];
      }},
      snapshot: function () {{
        return readRuntimeStateStore();
      }},
      emitTransaction: function (eventType, payload) {{
        const event = Object.assign({{
          event: String(eventType || 'runtime_transaction'),
          event_type: String(eventType || 'runtime_transaction'),
          timestamp: Date.now()
        }}, payload || {{}});
        appendRuntimeEvent(event);
        window.__ghostwebLastRuntimeTransaction = event;
        return event;
      }}
    }});
    return window.GhostwebRuntimeState;
  }}
  installRuntimeStateAuthority();
  function baseFilename(target) {{
    return String(target || '').split('/').pop().split('?')[0].split('#')[0];
  }}
  function hostActionIds(host) {{
    const ids = [];
    function append(value) {{
      String(value || '').split(/\\s+/).filter(Boolean).forEach(function (id) {{
        if (!ids.includes(id)) ids.push(id);
      }});
    }}
    if (!host || !host.getAttribute) return ids;
    append(host.getAttribute('data-action-id'));
    append(host.getAttribute('data-action-aliases'));
    try {{
      const targetMap = JSON.parse(host.getAttribute('data-ghostweb-target-map') || '{{}}');
      Object.keys(targetMap || {{}}).forEach(append);
    }} catch (e) {{}}
    return ids;
  }}
  function contextActionIdForHost(host, context) {{
    const ids = hostActionIds(host);
    const explicitActionId = String((context && context.action_id) || window.__ghostwebActiveActionId || '');
    if (explicitActionId && ids.includes(explicitActionId)) return explicitActionId;
    const traceId = String((context && (context.reference_trace_id || context.trace_id || context.task_id)) || '');
    if (!traceId) return '';
    const matches = ids.filter(function (id) {{ return id.indexOf(traceId + ':step') === 0; }});
    return matches.length === 1 ? matches[0] : '';
  }}
  function resolveContextTarget(target, sourceHost) {{
    const explicitTarget = String(target || '').trim();
    let context = {{}};
    try {{
      context = window.__ghostwebExecutionContext || JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{{}}');
    }} catch (e) {{}}
    // A concrete route chosen by a page-owned interaction (for example a
    // query-specific submit) must win over the context from the preceding
    // action.  Otherwise a stale input action can redirect a valid submit
    // back to the current page through its same-page target map.
    if (/^https?:\\/\\//i.test(explicitTarget) || explicitTarget.charAt(0) === '/' ||
        /(?:^|\\/)[^/?#]+\\.html(?:[?#].*)?$/i.test(explicitTarget)) {{
      return explicitTarget;
    }}
    if (sourceHost) {{
      const sourceActionId = contextActionIdForHost(sourceHost, context);
      if (!sourceActionId) return explicitTarget || target;
      try {{
        const sourceTargetMap = JSON.parse(sourceHost.getAttribute('data-ghostweb-target-map') || '{{}}');
        if (sourceTargetMap && sourceTargetMap[sourceActionId]) return String(sourceTargetMap[sourceActionId]);
      }} catch (e) {{}}
      return explicitTarget || target;
    }}
    const actionId = String((context && context.action_id) || window.__ghostwebActiveActionId || '');
    if (!actionId) return explicitTarget || target;
    const hosts = document.querySelectorAll('[data-action-id], [data-action-aliases]');
    for (let index = 0; index < hosts.length; index += 1) {{
      const host = hosts[index];
      const ids = hostActionIds(host);
      if (!ids.includes(actionId)) continue;
      try {{
        const targetMap = JSON.parse(host.getAttribute('data-ghostweb-target-map') || '{{}}');
        if (targetMap && targetMap[actionId]) return String(targetMap[actionId]);
      }} catch (e) {{}}
    }}
    return explicitTarget || target;
  }}
  function emitContextCheckpointBeforeNavigation(host) {{
    if (!host) return false;
    let context = {{}};
    try {{
      context = window.__ghostwebExecutionContext || JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{{}}');
    }} catch (e) {{}}
    const actionId = contextActionIdForHost(host, context);
    const checkpointMap = window.GhostwebCheckpointActionMap;
    if (!actionId || !checkpointMap || typeof checkpointMap.emitForAction !== 'function') return false;
    checkpointMap.emitForAction(actionId, 'click', host);
    return true;
  }}
  function selectedStateSearch() {{
    let params;
    try {{
      params = new URLSearchParams(window.location.search || '');
    }} catch (e) {{
      return '';
    }}
    const carried = new URLSearchParams();
    params.forEach(function (value, key) {{
      const name = String(key || '');
      if (!value) return;
      if (name === 'id' || /_id$/.test(name) || /Id$/.test(name)) {{
        carried.set(name, value);
      }}
    }});
    const text = carried.toString();
    return text ? '?' + text : '';
  }}
  function targetWithSelectedState(action, target) {{
    const text = String(target || '').trim();
    if (!text || /[?#]/.test(text)) return text;
    const binding = action && action.route_binding && typeof action.route_binding === 'object'
      ? action.route_binding
      : {{}};
    if (binding.preserve_existing === false) return text;
    const search = selectedStateSearch();
    if (!search) return text;
    return text + search;
  }}
  // Session identity is a runtime carrier, not a page-specific route hint.
  // Preserve it on every generated navigation when the target does not already
  // carry an explicit query/hash. This keeps the host's fresh session bound
  // across natural route transitions without creating a second competing URL.
  function targetWithSessionCarrier(target) {{
    const text = String(target || '').trim();
    if (!text || /[?#]/.test(text)) return text;
    let sid = '';
    try {{ sid = String(new URLSearchParams(window.location.search || '').get('sid') || '').trim(); }} catch (e) {{ sid = ''; }}
    if (!sid) return text;
    return text + '?sid=' + encodeURIComponent(sid);
  }}
  function explicitSurfaceTarget(el, target) {{
    const text = String(target || '').trim();
    if (!el || !el.closest || !text) return '';
    const host = el.closest('a[href], [data-ghostweb-target], [data-href], [data-url], [data-target], [data-id], [data-record-id], [data-item-id], [data-entity-id]');
    if (!host) return '';
    const raw = host.getAttribute('data-ghostweb-target') ||
      host.getAttribute('href') ||
      host.getAttribute('data-href') ||
      host.getAttribute('data-url') ||
      host.getAttribute('data-target') ||
      '';
    const recordId = host.getAttribute('data-id') ||
      host.getAttribute('data-record-id') ||
      host.getAttribute('data-item-id') ||
      host.getAttribute('data-entity-id') ||
      '';
    if (raw && raw !== '#' && !/^javascript:/i.test(raw)) {{
      const rawBase = baseFilename(raw);
      if (rawBase && rawBase === baseFilename(text)) return raw;
    }}
    if (recordId) {{
      const separator = text.indexOf('?') >= 0 ? '&' : '?';
      return text + separator + 'id=' + encodeURIComponent(recordId);
    }}
    return '';
  }}
  function currentPageRef() {{
    const configured = baseFilename(CONTRACT && CONTRACT.filename ? CONTRACT.filename : '');
    const actual = baseFilename(window.location.pathname || '');
    // The browser location is authoritative; compiled contract metadata must
    // not report the source page after a real route transition.
    const base = actual || configured;
    return base ? base + (window.location.search || '') + (window.location.hash || '') : '';
  }}
  function syncCurrentPageRuntimeState() {{
    const current = baseFilename(window.location.pathname || '') ||
      baseFilename(CONTRACT && CONTRACT.filename ? CONTRACT.filename : '');
    if (!current || !window.GhostwebRuntimeState || typeof window.GhostwebRuntimeState.write !== 'function') return false;
    window.GhostwebRuntimeState.write('state::route.current_page', current, {{
      source: 'route_execution_runtime',
      page: current,
      runtime_contract_filename: baseFilename(CONTRACT && CONTRACT.filename ? CONTRACT.filename : '')
    }});
    return true;
  }}
  function readStack() {{
    try {{
      const parsed = JSON.parse(window.sessionStorage.getItem(STACK_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.filter(Boolean).map(String) : [];
    }} catch (e) {{
      return [];
    }}
  }}
  function writeStack(stack) {{
    const cleaned = [];
    (Array.isArray(stack) ? stack : []).forEach(function (item) {{
      const text = String(item || '').trim();
      if (!text) return;
      if (cleaned.length && cleaned[cleaned.length - 1] === text) return;
      cleaned.push(text);
    }});
    const bounded = cleaned.slice(-MAX_STACK);
    try {{
      window.sessionStorage.setItem(STACK_KEY, JSON.stringify(bounded));
      window.sessionStorage.setItem(CURRENT_KEY, currentPageRef());
    }} catch (e) {{}}
    return bounded;
  }}
  function reconcileStackForCurrentPage(stack, current) {{
    const currentBase = baseFilename(current);
    if (!currentBase || !Array.isArray(stack) || stack.length < 2) return false;
    let existingIndex = -1;
    for (let i = stack.length - 2; i >= 0; i -= 1) {{
      if (baseFilename(stack[i]) === currentBase) {{
        existingIndex = i;
        break;
      }}
    }}
    if (existingIndex >= 0 && existingIndex < stack.length - 1) {{
      stack.length = existingIndex + 1;
      stack[existingIndex] = current;
      writeStack(stack);
      return true;
    }}
    return false;
  }}
  function rememberCurrentPage() {{
    if (applyPendingCommitIfCurrent()) return;
    const current = currentPageRef();
    if (!current) return;
    const stack = readStack();
    const reconciled = reconcileStackForCurrentPage(stack, current);
    if (reconciled) return;
    if (!stack.length || stack[stack.length - 1] !== current) {{
      stack.push(current);
    }}
    writeStack(stack);
  }}
  function previousFromStack() {{
    const current = currentPageRef();
    const currentBase = baseFilename(current);
    const stack = readStack();
    while (stack.length && baseFilename(stack[stack.length - 1]) === currentBase) {{
      stack.pop();
    }}
    while (stack.length) {{
      const previous = stack[stack.length - 1];
      if (baseFilename(previous) && baseFilename(previous) !== currentBase) {{
        writeStack(stack);
        return previous;
      }}
      stack.pop();
    }}
    writeStack(stack);
    return '';
  }}
  function markBackFrom() {{
    try {{
      window.sessionStorage.setItem(LAST_BACK_FROM_KEY, baseFilename(currentPageRef()));
    }} catch (e) {{}}
  }}
  function fallbackAllowed(target) {{
    const targetBase = baseFilename(target);
    const currentBase = baseFilename(currentPageRef());
    if (!targetBase || targetBase === currentBase) return false;
    try {{
      if (window.sessionStorage.getItem(LAST_BACK_FROM_KEY) === targetBase) return false;
    }} catch (e) {{}}
    return true;
  }}
  function rawHistoryBackAllowed() {{
    const constraints = CONTRACT && CONTRACT.constraints && typeof CONTRACT.constraints === 'object'
      ? CONTRACT.constraints
      : {{}};
    return constraints.raw_history_back_allowed !== false;
  }}
  function replaceStackWithTarget(target, fromBase) {{
    const text = String(target || '').trim();
    const targetBase = baseFilename(text);
    const sourceBase = fromBase || baseFilename(currentPageRef());
    const stack = readStack();
    while (stack.length && sourceBase && baseFilename(stack[stack.length - 1]) === sourceBase) {{
      stack.pop();
    }}
    let existingIndex = -1;
    for (let i = stack.length - 1; i >= 0; i -= 1) {{
      if (baseFilename(stack[i]) === targetBase) {{
        existingIndex = i;
        break;
      }}
    }}
    if (existingIndex >= 0) {{
      stack.length = existingIndex + 1;
      stack[existingIndex] = text;
    }} else if (text) {{
      stack.push(text);
    }}
    writeStack(stack);
    return stack;
  }}
  function applyPendingCommitIfCurrent() {{
    let pending = null;
    try {{
      pending = JSON.parse(window.sessionStorage.getItem(PENDING_COMMIT_KEY) || 'null');
    }} catch (e) {{
      pending = null;
    }}
    if (!pending || !pending.target) return false;
    const currentBase = baseFilename(currentPageRef());
    if (baseFilename(pending.target) !== currentBase) return false;
    replaceStackWithTarget(pending.target, pending.from || '');
    try {{
      window.sessionStorage.removeItem(PENDING_COMMIT_KEY);
      if (pending.from) window.sessionStorage.setItem(LAST_BACK_FROM_KEY, pending.from);
    }} catch (e) {{}}
    return true;
  }}
  function navigate(target, options) {{
    const opts = options || {{}};
    const resolvedTarget = opts.sourceHost && !opts.contextResolved
      ? resolveContextTarget(target, opts.sourceHost)
      : target;
    const text = targetWithSessionCarrier(String(resolvedTarget || '').trim());
    if (!text) return false;
    window.__ghostwebRouteNavigationEpoch = Number(window.__ghostwebRouteNavigationEpoch || 0) + 1;
    const stack = readStack();
    const current = currentPageRef();
    const currentBase = baseFilename(current);
    const targetBase = baseFilename(text);
    if (opts.mode === 'replace') {{
      markBackFrom();
      replaceStackWithTarget(text, currentBase);
      window.location.replace(text);
      return true;
    }}
    if (!stack.length || stack[stack.length - 1] !== current) {{
      stack.push(current);
    }}
    if (targetBase && baseFilename(stack[stack.length - 1]) !== targetBase) {{
      stack.push(text);
    }}
    writeStack(stack);
    window.location.href = text;
    return true;
  }}
  function commitTo(target) {{
    const text = String(target || '').trim();
    if (!text) return false;
    if (commitTargets.size && !commitTargets.has(baseFilename(text))) return false;
    return navigate(text, {{ mode: 'replace' }});
  }}
  function emitHistoryBackAttempt(payload) {{
    const event = Object.assign({{
      event: 'history_back_attempted',
      event_type: 'history_back_attempted',
      timestamp: Date.now()
    }}, payload || {{}});
    try {{
      const key = '__ghostweb_history_back_events_v1__';
      let existing = JSON.parse(window.sessionStorage.getItem(key) || '[]');
      if (!Array.isArray(existing)) existing = [];
      existing.push(event);
      window.sessionStorage.setItem(key, JSON.stringify(existing.slice(-128)));
    }} catch (e) {{}}
    if (window.GhostwebRuntimeState && window.GhostwebRuntimeState.emitTransaction) {{
      window.GhostwebRuntimeState.emitTransaction('history_back_attempted', Object.assign({{}}, event, {{
        source: 'route_execution_runtime'
      }}));
    }}
    window.__ghostwebLastHistoryBackEvent = event;
    return event;
  }}
  function warnLegacyBackAction(action, host) {{
    const warning = {{
      event: 'legacy_history_back_action_normalized',
      deprecated_action: String(action || ''),
      action_id: host && host.getAttribute ? String(host.getAttribute('data-ghostweb-action-id') || '') : '',
      source_page: baseFilename(currentPageRef()),
      timestamp: Date.now()
    }};
    try {{
      const key = '__ghostweb_history_back_warnings_v1__';
      let existing = JSON.parse(window.sessionStorage.getItem(key) || '[]');
      if (!Array.isArray(existing)) existing = [];
      existing.push(warning);
      window.sessionStorage.setItem(key, JSON.stringify(existing.slice(-128)));
    }} catch (e) {{}}
    if (window.console && window.console.warn) {{
      window.console.warn('[T2WRouteExecution] deprecated back action normalized to history_back', warning);
    }}
    return warning;
  }}
  window.GhostwebRouteExecution = Object.assign(window.GhostwebRouteExecution || {{}}, {{
    contract: CONTRACT,
    readStack: readStack,
    writeStack: writeStack,
    previousFromStack: previousFromStack,
    navigate: navigate,
    commitTo: commitTo,
    commit: function (actionId) {{
      const action = actions.find(function (item) {{ return item && item.action_id === actionId; }});
      if (!action || action.navigation_kind !== 'commit_to_target') return false;
      return commitTo(action.target_filename || '');
    }},
    returnBack: function (actionId) {{
      const effectiveActionId = String(actionId || 'fallback_back');
      const stackBefore = readStack().slice();
      const sourcePage = baseFilename(currentPageRef());
      const previous = previousFromStack();
      if (previous) {{
        const stackAfter = readStack().slice();
        emitHistoryBackAttempt({{
          action_id: effectiveActionId,
          source_page: sourcePage,
          resolved_target: previous,
          resolution: 'stack',
          stack_before: stackBefore,
          stack_after: stackAfter
        }});
        markBackFrom();
        window.location.replace(previous);
        return true;
      }}
      if (defaultBackTarget && fallbackAllowed(defaultBackTarget)) {{
        const stackAfter = replaceStackWithTarget(defaultBackTarget, sourcePage).slice();
        emitHistoryBackAttempt({{
          action_id: effectiveActionId,
          source_page: sourcePage,
          resolved_target: defaultBackTarget,
          resolution: 'fallback',
          stack_before: stackBefore,
          stack_after: stackAfter
        }});
        markBackFrom();
        window.location.replace(defaultBackTarget);
        return true;
      }}
      if (rawHistoryBackAllowed()) {{
        if (window.history.length > 1) {{
          emitHistoryBackAttempt({{
            action_id: effectiveActionId,
            source_page: sourcePage,
            resolved_target: '__browser_history__',
            resolution: 'stack',
            stack_before: stackBefore,
            stack_after: readStack().slice()
          }});
          window.history.back();
          return true;
        }}
      }}
      emitHistoryBackAttempt({{
        action_id: effectiveActionId,
        source_page: sourcePage,
        resolved_target: '',
        resolution: 'failed',
        stack_before: stackBefore,
        stack_after: readStack().slice()
      }});
      return false;
    }},
    goBack: function () {{
      return window.GhostwebRouteExecution.returnBack('fallback_back');
    }},
    initSession: function (traceId, initialStack, initialState) {{
      const stack = Array.isArray(initialStack) ? initialStack : [];
      writeStack(stack);
    }},
    step: function (actionId) {{
      return window.GhostwebRouteExecution.navigate(resolveContextTarget(actionId), {{ contextResolved: true }});
    }},
    inspectNavigationStack: function () {{
      return {{
        route_stack: readStack(),
        active_page_id: baseFilename(currentPageRef()),
      }};
    }},
    inspectState: function () {{
      return {{ stack: readStack(), current: currentPageRef() }};
    }},
    stateDiff: function (before, after) {{
      return {{ before: before, after: after }};
    }},
    markPendingCommit: function (target) {{
      const text = String(target || '').trim();
      if (!text) return false;
      try {{
        window.sessionStorage.setItem(PENDING_COMMIT_KEY, JSON.stringify({{
          from: baseFilename(currentPageRef()),
          target: text,
          at: Date.now()
        }}));
      }} catch (e) {{}}
      return true;
    }},
    applyPendingCommitIfCurrent: applyPendingCommitIfCurrent
  }});
  function patchWebsiteSdkRouteFallback() {{
    const sdk = window.WebsiteSDK;
    if (!sdk || sdk.__ghostwebRouteFallbackPatched) return false;
    const methodNames = ['routeTo', 'navigateTo', 'openPage'];
    methodNames.forEach(function (name) {{
      const original = sdk[name];
      if (typeof original !== 'function') return;
      sdk[name] = function () {{
        const args = Array.prototype.slice.call(arguments);
        const target = args.find(function (value) {{
          return typeof value === 'string' && /\\.html(?:[?#].*)?$/.test(value);
        }}) || '';
        const before = String(window.location.href || '');
        let result;
        let threw = false;
        try {{
          result = original.apply(this, args);
        }} catch (e) {{
          threw = true;
        }}
        if (target) {{
          window.setTimeout(function () {{
            const after = String(window.location.href || '');
            if (after === before) {{
              window.GhostwebRouteExecution.navigate(target, {{ mode: 'push' }});
            }}
          }}, 80);
        }}
        if (threw && target) return window.GhostwebRouteExecution.navigate(target, {{ mode: 'push' }});
        if (threw) return false;
        return result;
      }};
    }});
    sdk.__ghostwebRouteFallbackPatched = true;
    return true;
  }}
  function installWebsiteSdkRouteFallback() {{
    if (patchWebsiteSdkRouteFallback()) return;
    const retry = function () {{ patchWebsiteSdkRouteFallback(); }};
    window.setTimeout(retry, 0);
    window.setTimeout(retry, 50);
    window.setTimeout(retry, 200);
    if (document && document.addEventListener) {{
      document.addEventListener('DOMContentLoaded', retry, {{ once: true }});
    }}
    if (window && window.addEventListener) {{
      window.addEventListener('load', retry, {{ once: true }});
    }}
  }}
  installWebsiteSdkRouteFallback();
  rememberCurrentPage();
  syncCurrentPageRuntimeState();

  function textOf(el) {{
    return [
      el.id || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || '',
      el.getAttribute('data-element-id') || '',
      el.getAttribute('data-category-name') || '',
      el.textContent || ''
    ].join(' ');
  }}

  function isBackControl(el) {{
    if (!el) return false;
    if (isDisabledOrNoninteractiveControl(el)) return false;
    if (el.getAttribute('data-ghostweb-action') === 'history_back') return true;
    if (el.getAttribute('data-ghostweb-action') === 'returnBack') return true;
    if (el.getAttribute('data-ghostweb-route-action') === 'default_back') return true;
    // A valid anchor owns its target route.  Do not infer browser-history
    // semantics from visible copy such as "Shop Back to School".
    if (el.matches && el.matches('a[href]')) return false;
    const visible = String(
      el.getAttribute('aria-label') ||
      el.getAttribute('title') ||
      el.textContent ||
      ''
    ).replace(/\\s+/g, ' ').trim();
    return /^(?:go\\s+)?back(?:\\s+button)?$/i.test(visible) || /^(?:返回|上一页|回到上一级)$/.test(visible);
  }}
  function isDisabledOrNoninteractiveControl(el) {{
    if (!el) return false;
    if (el.disabled === true) return true;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return true;
    if (el.getAttribute && el.getAttribute('data-ghostweb-click-contract-kind') === 'noninteractive') return true;
    if (el.closest && el.closest('[aria-disabled="true"], [data-ghostweb-click-contract-kind="noninteractive"]')) return true;
    return false;
  }}
  function commitActionForControl(el) {{
    if (isDisabledOrNoninteractiveControl(el)) return null;
    const label = textOf(el).replace(/\\s+/g, ' ').trim();
    if (!label) return null;
    const commitActions = actions.filter(function (action) {{
      return action && action.navigation_kind === 'commit_to_target' && action.target_filename;
    }});
    for (const action of commitActions) {{
      const actionLabels = labelsForAction(action);
      for (const actionLabel of actionLabels) {{
        if (actionLabel && (label.indexOf(actionLabel) >= 0 || actionLabel.indexOf(label) >= 0)) return action;
      }}
    }}
    if (commitActions.length === 1 && /保存|提交|确认|完成|save|submit|confirm|done/i.test(label)) {{
      return commitActions[0];
    }}
    return null;
  }}

  function normalizedLabel(value) {{
    return String(value || '').replace(/\\s+/g, ' ').trim();
  }}
  function labelsForControl(el) {{
    if (!el) return [];
    const values = [
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || '',
      el.getAttribute('data-element-id') || '',
      el.getAttribute('data-category-name') || '',
      el.textContent || ''
    ];
    const labels = [];
    values.forEach(function (value) {{
      const label = normalizedLabel(value);
      if (label && labels.indexOf(label) < 0) labels.push(label);
    }});
    return labels;
  }}
  function labelsForAction(action) {{
    const binding = action && action.binding && typeof action.binding === 'object' ? action.binding : {{}};
    const labels = Array.isArray(binding.labels) ? binding.labels : [];
    return labels.map(normalizedLabel).filter(Boolean);
  }}
  function stateRefForAction(action) {{
    if (!action || typeof action !== 'object') return '';
    const commit = action.state_commit && typeof action.state_commit === 'object' ? action.state_commit : {{}};
    return String(action.state_ref || commit.commit_state_ref || '').trim();
  }}
  function stateValueForAction(action, el) {{
    const labels = labelsForAction(action);
    if (labels.length) return labels[0];
    const controlLabels = labelsForControl(el);
    if (controlLabels.length) return controlLabels[0];
    return '';
  }}
  function readStateCommits() {{
    try {{
      const parsed = JSON.parse(window.sessionStorage.getItem(STATE_COMMITS_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
    }} catch (e) {{
      return [];
    }}
  }}
  function writeStateCommits(commits) {{
    const bounded = (Array.isArray(commits) ? commits : []).slice(-64);
    try {{
      window.sessionStorage.setItem(STATE_COMMITS_KEY, JSON.stringify(bounded));
    }} catch (e) {{}}
    return bounded;
  }}
  function recordStateCommit(action, el) {{
    const stateRef = stateRefForAction(action);
    const value = stateValueForAction(action, el);
    if (!stateRef || !value) return false;
    const commits = readStateCommits();
    commits.push({{
      state_ref: stateRef,
      value: value,
      action_id: String(action && action.action_id || ''),
      target_filename: String(action && action.target_filename || ''),
      source_filename: String(CONTRACT && CONTRACT.filename || ''),
      at: Date.now()
    }});
    writeStateCommits(commits);
    return true;
  }}
  function isRouteNavigationAction(action) {{
    const kind = String(action && action.navigation_kind || '');
    return Boolean(action && action.target_filename && (kind === 'navigate_to_target' || kind === 'route_to_page'));
  }}
  function navigationActionForControl(el) {{
    if (!el) return null;
    if (isDisabledOrNoninteractiveControl(el)) return null;
    if (commitActionForControl(el)) return null;
    if (isStatefulSubmitControl(el)) return null;
    const explicitEffectHost = el.closest && el.closest('[data-ghostweb-effect-kind]');
    const explicitEffect = explicitEffectHost && explicitEffectHost.getAttribute
      ? String(explicitEffectHost.getAttribute('data-ghostweb-effect-kind') || '')
      : '';
    if (explicitEffect && explicitEffect !== 'route' && explicitEffect !== 'gesture_route') return null;
    if (el.closest && el.closest('a[href], [data-ghostweb-action="navigate"][data-ghostweb-target]')) return null;
    if (el.matches && el.matches('a[href], [data-ghostweb-action="navigate"][data-ghostweb-target]')) return null;
    const controlLabels = labelsForControl(el);
    if (!controlLabels.length) return null;
    const navigationActions = actions.filter(isRouteNavigationAction);
    for (const action of navigationActions) {{
      const actionLabels = labelsForAction(action);
      if (!actionLabels.length) continue;
      for (const actionLabel of actionLabels) {{
        if (controlLabels.indexOf(actionLabel) >= 0) return action;
      }}
    }}
    return null;
  }}
  function isStatefulSubmitControl(el) {{
    if (hasStatefulQueryActionSlot(el)) return true;
    if (!statefulActions.length) return false;
    const label = textOf(el).replace(/\\s+/g, ' ').trim();
    return Boolean(label && /保存|提交|确认|完成|save|submit|confirm|done/i.test(label));
  }}
  function actionSlotRefsForControl(el) {{
    if (!el || !el.getAttribute) return '';
    return [
      el.getAttribute('data-action-slot-id') || '',
      el.getAttribute('data-action-slot-aliases') || ''
    ].join(' ');
  }}
  function hasStatefulQueryActionSlot(el) {{
    return /(?:^|\\s|::)query_text=/.test(actionSlotRefsForControl(el));
  }}

  document.addEventListener('click', function (event) {{
    if (event.defaultPrevented) return;
    const host = event.target && event.target.closest
      ? event.target.closest('button, a, [role="button"], [tabindex], [data-ghostweb-action], [data-ghostweb-route-action]')
      : null;
    if (!host || !isBackControl(host)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    event.stopPropagation();
    const actionName = host.getAttribute('data-ghostweb-action') || host.getAttribute('data-ghostweb-route-action') || '';
    if (actionName === 'returnBack' || actionName === 'default_back') warnLegacyBackAction(actionName, host);
    window.GhostwebRouteExecution.returnBack(host.getAttribute('data-ghostweb-action-id') || 'fallback_back');
  }}, true);

  document.addEventListener('click', function (event) {{
    if (event.defaultPrevented) return;
    const host = event.target && event.target.closest
      ? event.target.closest('button, a, [role="button"], [tabindex], [data-element-id]')
      : null;
    if (!host || isBackControl(host)) return;
    const action = commitActionForControl(host);
    if (!action) return;
    recordStateCommit(action, host);
    window.GhostwebRouteExecution.markPendingCommit(action.target_filename || '');
  }}, true);

  document.addEventListener('click', function (event) {{
    if (event.defaultPrevented) return;
    const host = event.target && event.target.closest
      ? event.target.closest('button, a, [role="button"], [tabindex], [data-element-id], [data-category-name]')
      : null;
    if (!host || isBackControl(host)) return;
    if (host.getAttribute('data-ghostweb-route-owner')) return;
    // A DOM-bound host is deferred only when its page-local action-effect
    // table contains one of its own action ids.  A shared visible host may
    // retain a stale/no-row binding marker; it has no concrete page reducer
    // in that case, so allowing generic route fallback avoids a dead control.
    if (host.getAttribute('onclick')) return;
    if (domBindingOwnsHost(host)) return;
    const contextTarget = resolveContextTarget('', host);
    if (contextTarget && baseFilename(contextTarget) !== baseFilename(window.location.pathname)) {{
      // This capture handler is registered before the page-level checkpoint
      // listener. Persist the context-selected route checkpoint synchronously
      // before stopImmediatePropagation and navigation unload the source page.
      emitContextCheckpointBeforeNavigation(host);
      event.preventDefault();
      event.stopImmediatePropagation();
      event.stopPropagation();
      window.GhostwebRouteExecution.navigate(contextTarget, {{ mode: 'push', contextResolved: true }});
      return;
    }}
    const action = navigationActionForControl(host);
    if (!action) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    event.stopPropagation();
    recordStateCommit(action, host);
    const target = explicitSurfaceTarget(host, action.target_filename || '') ||
      targetWithSelectedState(action, action.target_filename || '');
    window.GhostwebRouteExecution.navigate(target, {{
      mode: action.history_mode === 'replace' ? 'replace' : 'push',
      sourceHost: host
    }});
  }}, true);

  document.addEventListener('click', function (event) {{
    if (event.defaultPrevented) return;
    const componentOwner = event.target && event.target.closest
      ? event.target.closest('[data-ghostweb-route-owner]')
      : null;
    if (componentOwner) return;
    const host = event.target && event.target.closest
      ? event.target.closest('a[href], [data-ghostweb-action="navigate"][data-ghostweb-target]')
      : null;
    if (!host || isBackControl(host)) return;
    // Component-owned route controls may need to close or unwind their own
    // surface before navigating.  Preserve that explicit owner instead of
    // racing it from this generic page-level capture handler.
    if (host.getAttribute('data-ghostweb-route-owner')) return;
    // Inline page handlers are app-state authorities. They may need to write
    // a state carrier before routing, so the generic route runtime must not
    // intercept them in capture phase.
    if (host.getAttribute('onclick')) return;
    if (domBindingOwnsHost(host)) return;
    const explicit = host.getAttribute('data-ghostweb-target') || '';
    const href = host.getAttribute('href') || '';
    const target = explicit || href;
    if (!target || !/\\.html(?:[?#].*)?$/.test(target)) return;
    if (host.getAttribute('target') || host.hasAttribute('download')) return;
    event.preventDefault();
    window.GhostwebRouteExecution.navigate(resolveContextTarget(target, host));
  }}, true);
  function domBindingOwnsHost(host) {{
    if (!host || !host.getAttribute || !host.getAttribute('data-ghostweb-dom-binding-id')) return false;
    const rows = window.__GHOSTWEB_DOM_BINDING_ACTIONS || {{}};
    if (!rows || typeof rows !== 'object') return false;
    const ids = [];
    ['data-action-id', 'data-action-aliases'].forEach(function(name) {{
      String(host.getAttribute(name) || '').split(/\\s+/).forEach(function(id) {{
        id = String(id || '').trim();
        if (id && ids.indexOf(id) === -1) ids.push(id);
      }});
    }});
    return ids.some(function(id) {{
      return Object.prototype.hasOwnProperty.call(rows, id);
    }});
  }}
}})();
</script>
""".strip()


def _inject_route_execution_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    route_contract = page.get("route_execution_contract", {}) if isinstance(page, dict) else {}
    if not isinstance(route_contract, dict):
        return text, False
    has_route_actions = bool(
        route_contract.get("fallback_back_action")
        or route_contract.get("default_back_action")
        or route_contract.get("commit_actions")
        or route_contract.get("actions")
        or route_contract.get("stateful_actions")
    )
    if not has_route_actions:
        return text, False
    script = _route_execution_runtime_script(route_contract)
    inline_runtime_re = re.compile(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']route-execution["\'])'
        r'(?![^>]*\bsrc\s*=)[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    inline_matches = list(inline_runtime_re.finditer(text))
    if inline_matches:
        parts: List[str] = []
        cursor = 0
        for index, match in enumerate(inline_matches):
            parts.append(text[cursor:match.start()])
            if index == 0:
                parts.append(script)
            cursor = match.end()
        parts.append(text[cursor:])
        updated = "".join(parts)
        return updated, updated != text
    external_runtime_re = re.compile(
        r'<script\b[^>]*\bsrc=["\'][^"\']*route_runtime\.js(?:\?[^"\']*)?'
        r'["\'][^>]*>\s*</script>',
        re.IGNORECASE,
    )
    external_runtime = external_runtime_re.search(text)
    if external_runtime:
        insert_at = external_runtime.end()
        return text[:insert_at] + "\n" + script + text[insert_at:], True
    head_match = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    body_match = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    if body_match:
        insert_at = body_match.start()
        return text[:insert_at] + script + "\n" + text[insert_at:], True
    return script + "\n" + text, True


def _compact_dynamic_surface_contract_for_runtime(dynamic_surface_contract: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(dynamic_surface_contract, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in ("format", "schema_revision", "page_id", "filename", "surface_id", "surface_role"):
        value = dynamic_surface_contract.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    bindings = dynamic_surface_contract.get("state_display_bindings", [])
    if isinstance(bindings, list):
        compact_bindings = []
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            item = {
                key: binding.get(key)
                for key in (
                    "state_ref",
                    "target_filename",
                    "display_role",
                    "required_display_text",
                    "lifecycle",
                )
                if binding.get(key) not in (None, "", [], {})
            }
            if item:
                compact_bindings.append(item)
        if compact_bindings:
            result["state_display_bindings"] = compact_bindings
    return result


def _state_display_runtime_script(dynamic_surface_contract: Dict[str, Any]) -> str:
    runtime_contract = _compact_dynamic_surface_contract_for_runtime(dynamic_surface_contract or {})
    payload = json.dumps(runtime_contract, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return f"""
<script data-ghostweb-runtime="state-display">
(function () {{
  const CONTRACT = {payload};
  const STATE_COMMITS_KEY = 'ghostweb_state_commits';
  const bindings = Array.isArray(CONTRACT && CONTRACT.state_display_bindings)
    ? CONTRACT.state_display_bindings
    : [];
  function baseFilename(target) {{
    return String(target || '').split('/').pop().split('?')[0].split('#')[0];
  }}
  function currentBase() {{
    return baseFilename((CONTRACT && CONTRACT.filename) || window.location.pathname || '');
  }}
  function readStateCommits() {{
    try {{
      const parsed = JSON.parse(window.sessionStorage.getItem(STATE_COMMITS_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
    }} catch (e) {{
      return [];
    }}
  }}
  function commitForBinding(binding) {{
    const stateRef = String(binding && binding.state_ref || '').trim();
    const target = baseFilename(binding && binding.target_filename || currentBase());
    const current = currentBase();
    const commits = readStateCommits();
    for (let i = commits.length - 1; i >= 0; i -= 1) {{
      const item = commits[i] || {{}};
      if (String(item.state_ref || '').trim() !== stateRef) continue;
      const itemTarget = baseFilename(item.target_filename || target);
      if (itemTarget && itemTarget !== current && itemTarget !== target) continue;
      return item;
    }}
    return null;
  }}
  function selectorsForBinding(binding) {{
    const stateRef = String(binding && binding.state_ref || '').trim();
    const role = String(binding && binding.display_role || '').trim();
    const selectors = [
      '[data-ghostweb-state-ref="' + stateRef + '"]',
      '[data-state-ref="' + stateRef + '"]',
      '[data-ghostweb-display-role="' + role + '"]'
    ];
    if (role === 'city_selector_label') {{
      selectors.push('#cityLabel');
      selectors.push('[data-city-label]');
      selectors.push('[aria-label*="城市"] span');
      selectors.push('[aria-label*="选择城市"]');
    }} else if (role === 'filter_state_label') {{
      selectors.push('[data-filter-label]');
      selectors.push('[aria-label*="筛选"]');
    }} else if (role === 'selected_address_label') {{
      selectors.push('#addressText');
      selectors.push('[data-address-label]');
      selectors.push('[aria-label*="地址"]');
    }} else if (role === 'selected_record_label') {{
      selectors.push('[data-record-label]');
      selectors.push('[aria-label*="已选"]');
    }}
    return selectors;
  }}
  function applyValue(el, value) {{
    if (!el || !value) return;
    const tag = String(el.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {{
      el.value = value;
      el.dispatchEvent(new Event('input', {{ bubbles: true }}));
      el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      return;
    }}
    el.textContent = value;
  }}
  function applyStateDisplayBindings() {{
    bindings.forEach(function (binding) {{
      const commit = commitForBinding(binding);
      if (!commit || !commit.value) return;
      const value = String(commit.value || '').trim();
      if (!value) return;
      const selectors = selectorsForBinding(binding);
      for (let i = 0; i < selectors.length; i += 1) {{
        const el = document.querySelector(selectors[i]);
        if (!el) continue;
        applyValue(el, value);
        el.setAttribute('data-ghostweb-state-ref', String(binding.state_ref || ''));
        el.setAttribute('data-ghostweb-display-role', String(binding.display_role || ''));
        return;
      }}
    }});
  }}
  window.GhostwebStateDisplay = Object.assign(window.GhostwebStateDisplay || {{}}, {{
    contract: CONTRACT,
    apply: applyStateDisplayBindings,
    readStateCommits: readStateCommits
  }});
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', applyStateDisplayBindings);
  }} else {{
    applyStateDisplayBindings();
  }}
}})();
</script>
""".strip()


def _inject_state_display_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    dynamic_contract = page.get("dynamic_surface_contract", {}) if isinstance(page, dict) else {}
    if not isinstance(dynamic_contract, dict) or not dynamic_contract.get("state_display_bindings"):
        return text, False
    script = _state_display_runtime_script(dynamic_contract)
    runtime_re = re.compile(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']state-display["\'])[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    head_match = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    body_match = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    if body_match:
        insert_at = body_match.start()
        return text[:insert_at] + script + "\n" + text[insert_at:], True
    return script + "\n" + text, True


def _runtime_page_contract(page: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(page, dict):
        return {}
    nested = page.get("page_execution_contract")
    if isinstance(nested, dict):
        runtime_page = dict(nested)
        for key in (
            "query_state_contract",
            "page_execution_obligations",
            "page_execution_obligation_action_surface_runtime_enabled",
            "page_environment_projection",
            "page_environment_spec",
        ):
            if key in page and key not in runtime_page:
                runtime_page[key] = page.get(key)
    else:
        runtime_page = dict(page)
    page_environment = (
        page.get("page_environment_spec")
        or page.get("page_environment_projection")
        or runtime_page.get("page_environment_spec")
        or runtime_page.get("page_environment_projection")
    )
    _rt_filename = str(runtime_page.get("filename") or page.get("filename") or "")
    _rt_page_id = str(runtime_page.get("page_id") or page.get("page_id") or "")


    compiled_query_contract = runtime_page.get("query_state_contract")
    query_contract = _page_environment_query_state_contract(
        page_environment,
        filename=_rt_filename,
        page_id=_rt_page_id,
    )
    if query_contract:
        if isinstance(compiled_query_contract, dict):
            compiled_states = {
                str(state.get("query") or "").strip(): state
                for state in compiled_query_contract.get("states", [])
                if isinstance(state, dict) and str(state.get("query") or "").strip()
            }
            for state in query_contract.get("states", []):
                if not isinstance(state, dict) or state.get("submit_targets"):
                    continue
                compiled_state = compiled_states.get(str(state.get("query") or "").strip())
                if not isinstance(compiled_state, dict):
                    continue
                targets = compiled_state.get("submit_targets")
                if isinstance(targets, list) and targets:
                    state["submit_targets"] = [
                        dict(target) for target in targets if isinstance(target, dict)
                    ]
        runtime_page["query_state_contract"] = query_contract
    if not query_contract:
        draft_contract = _page_environment_draft_state_contract(
            page_environment,
            filename=_rt_filename,
            page_id=_rt_page_id,
        )
        if draft_contract:
            runtime_page["draft_state_contract"] = draft_contract
    return runtime_page


def _step_key_trace_and_index(step_key: Any) -> Tuple[str, int]:
    text = str(step_key or "").strip()
    match = re.match(r"^(?P<trace>.+):step(?P<step>\d+)$", text)
    if match:
        return match.group("trace"), int(match.group("step"))
    return text, 0


def _gesture_from_page_environment_command(command: Any) -> Dict[str, Any]:
    parts = str(command or "").strip().split()
    if len(parts) >= 3 and parts[0] in {"tap", "click"}:
        try:
            x = int(float(parts[1]))
            y = int(float(parts[2]))
        except (TypeError, ValueError):
            return {}
        return {"type": "tap", "x": x, "y": y, "hit_point": {"x": x, "y": y}}
    return {}


def _page_environment_action_surfaces(page_environment: Any) -> List[Dict[str, Any]]:
    if not isinstance(page_environment, dict):
        return []
    surfaces: List[Dict[str, Any]] = []
    transitions = page_environment.get("transitions", [])
    for transition in transitions if isinstance(transitions, list) else []:
        if not isinstance(transition, dict):
            continue
        if str(transition.get("direction") or "source") != "source":
            continue
        operation = str(transition.get("operation") or "").strip().lower()
        if operation not in {"click", "tap", "fill"}:
            continue
        target = _filename_without_query(str(transition.get("target_filename") or ""))
        if not target:
            continue
        trace_id, step_index = _step_key_trace_and_index(transition.get("step_key") or transition.get("transition_id"))
        source = _filename_without_query(str(transition.get("source_filename") or ""))
        if operation == "fill":
            input_text = str(transition.get("input_text") or "").strip()
            if not input_text:
                effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
                input_text = str(effect.get("value") or "").strip()
            surfaces.append({
                "trace_id": trace_id,
                "step_index": step_index,
                "source_filename": source,
                "operation": "fill",
                "operation_intent": str(transition.get("operation_intent") or "fill")[:80],
                "intent_provenance": "page_environment_spec",
                "intent_visible_text_allowed": False,
                "expected_filename": target,
                "input_text": input_text,
                "gesture": {},
            })
            continue
        gesture = _gesture_from_page_environment_command(transition.get("command"))
        if not gesture:
            continue
        surfaces.append({
            "trace_id": trace_id,
            "step_index": step_index,
            "source_filename": source,
            "operation": "click",
            "operation_intent": str(transition.get("operation_intent") or "click")[:80],
            "intent_provenance": "page_environment_spec",
            "intent_visible_text_allowed": False,
            "expected_filename": target,
            "gesture": gesture,
        })
    return surfaces


def _actionability_inspector_runtime_script() -> str:
    return """
<script data-ghostweb-runtime="actionability-inspector">
(function () {
  if (window.GhostwebActionability && window.GhostwebActionability.installed) return;
  const SCHEMA = 'ghostweb.actionability.v1';
  const MODES = { visual_notice: true, env_text: true, silent: true };
  const EXECUTION_AUTHORITY_SELECTOR = [
    '[data-ghostweb-action]',
    '[data-ghostweb-route-action]',
    '[data-ghostweb-target]',
    '[data-ghostweb-effect-kind]',
    '[data-ghostweb-query-result]',
    '[data-ghostweb-query-suggestion]',
    '[data-ghostweb-opens-template]',
    '[data-ghostweb-wheel-trigger]',
    '[data-ghostweb-actionability-authority="semantic_transaction"]',
    '[data-ghostweb-action-id]',
    '[data-action-id]'
  ].join(',');
  const TASK_RELATED_CONTROL_SELECTOR = [
    '[data-action-aliases]',
    '[data-element-aliases]'
  ].join(',');
  const NATIVE_CONTROL_SELECTOR = [
    'button',
    'input',
    'select',
    'textarea',
    'summary'
  ].join(',');
  const CONTROL_SELECTOR = [
    'button',
    'a[href]',
    'input',
    'textarea',
    'select',
    'summary',
    '[role="button"]',
    '[role="checkbox"]',
    '[role="combobox"]',
    '[role="link"]',
    '[role="menuitem"]',
    '[role="menuitemcheckbox"]',
    '[role="menuitemradio"]',
    '[role="option"]',
    '[role="radio"]',
    '[role="slider"]',
    '[role="spinbutton"]',
    '[role="switch"]',
    '[role="tab"]',
    '[role="textbox"]',
    '[tabindex]',
    '[contenteditable="true"]'
  ].join(',');
  const BACK_CONTROL_SELECTOR = [
    '[data-ghostweb-action="history_back"]',
    '[data-ghostweb-action="returnBack"]',
    '[data-ghostweb-route-action="default_back"]',
    '[data-history-back]',
    'button',
    'a[href]',
    '[role="button"]',
    '[tabindex]'
  ].join(',');
  let lastResult = null;
  function storageGet(name) {
    try {
      return window.sessionStorage.getItem(name) || window.localStorage.getItem(name) || '';
    } catch (error) {
      return '';
    }
  }
  function normalizeMode(value) {
    const mode = String(value || '').trim();
    return MODES[mode] ? mode : '';
  }
  function getResponseMode() {
    return normalizeMode(window.__GhostwebResponseMode) ||
      normalizeMode(storageGet('ghostweb.response_mode')) ||
      'visual_notice';
  }
  function setResponseMode(mode) {
    const normalized = normalizeMode(mode) || 'visual_notice';
    window.__GhostwebResponseMode = normalized;
    try {
      window.localStorage.setItem('ghostweb.response_mode', normalized);
      window.sessionStorage.setItem('ghostweb.response_mode', normalized);
    } catch (error) {}
    return normalized;
  }
  function compact(value, limit) {
    return String(value || '').replace(/\\s+/g, ' ').trim().slice(0, limit || 96);
  }
  function attr(el, name) {
    return el && el.getAttribute ? String(el.getAttribute(name) || '') : '';
  }
  function nearest(el, selector) {
    return el && el.closest ? el.closest(selector) : null;
  }
  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') < 0.05) return false;
    return true;
  }
  function isDisabledNative(el) {
    if (!el) return false;
    if (el.disabled === true ||
        (el.matches && el.matches(':disabled')) ||
        attr(el, 'aria-disabled') === 'true' ||
        nearest(el, '[inert]')) return true;
    if (el.readOnly !== true) return false;
    const routeAuthority = attr(el, 'data-ghostweb-target') ||
      attr(el, 'data-ghostweb-route-action') ||
      attr(el, 'data-ghostweb-action');
    return !routeAuthority;
  }
  function isValidHref(el) {
    if (!el || !el.matches || !el.matches('a[href]')) return false;
    const href = String(attr(el, 'href') || '').trim();
    return !!href && href !== '#' && !/^javascript:/i.test(href);
  }
  function isTransitionHiddenLayer(el) {
    if (attr(el, 'data-ghostweb-initial-visibility') !== 'hidden_until_transition') return false;
    if (attr(el, 'aria-hidden') === 'false') return false;
    return !/(^|\\s)(open|visible|show|active|is-open)(\\s|$)/i.test(attr(el, 'class'));
  }
  const ACTIVE_LAYER_SELECTOR = [
    '[aria-modal="true"]',
    '[role="dialog"]',
    '[data-overlay]',
    '[data-ghostweb-overlay-layer-role]',
    '.modal',
    '.drawer',
    '.sheet',
    '[id*="modal" i]',
    '[id*="sheet" i]',
    '[id*="drawer" i]',
    '[id*="popup" i]',
    '[class*="modal" i]',
    '[class*="sheet" i]',
    '[class*="drawer" i]'
  ].join(',');
  function isLayerCandidate(node) {
    if (!node || node.nodeType !== 1) return false;
    // Active layers are structural containers. Native controls and elements
    // with interactive ARIA roles remain controls even when generated ids,
    // classes, or incidental overlay attributes contain modal/sheet tokens.
    // Apply this exclusion before all explicit/token layer inference so a
    // dialog action can never become the active layer that masks its sibling.
    if (node.matches && node.matches(CONTROL_SELECTOR)) return false;
    if (attr(node, 'aria-modal') === 'true' ||
        attr(node, 'role') === 'dialog' ||
        node.hasAttribute('data-overlay') ||
        node.hasAttribute('data-ghostweb-overlay-layer-role')) {
      return true;
    }
    // Token inference is only for structural layer containers.  Controls such
    // as `btn-close-sheet` are descendants of the active layer; treating the
    // last such control as the layer itself masks every sibling action in the
    // same dialog.
    const layerToken = /(?:^|[-_])(?:modal|drawer|sheet|popup)(?:$|[-_](?:overlay|panel|container|root|backdrop|dialog|mask|layer|content)$)/i;
    if (layerToken.test(attr(node, 'id'))) return true;
    return Array.from(node.classList || []).some(function (token) {
      return layerToken.test(String(token || ''));
    });
  }
  function activeLayer() {
    const layers = Array.from(document.querySelectorAll(ACTIVE_LAYER_SELECTOR))
      .filter(function (node) {
        return isLayerCandidate(node) &&
          isVisible(node) &&
          !isTransitionHiddenLayer(node) &&
          attr(node, 'data-ghostweb-overlay-layer-role') !== 'backdrop';
      });
    return layers.length ? layers[layers.length - 1] : null;
  }
  function inActiveLayer(el) {
    const layer = activeLayer();
    const role = attr(el, 'data-ghostweb-overlay-layer-role');
    const action = attr(el, 'data-ghostweb-action').toLowerCase();
    const explicitCloseBackdrop = role === 'backdrop' &&
      (action === 'close_overlay' || action === 'dismiss_overlay') && hasOwnAuthority(el);
    return !layer || layer === el || layer.contains(el) || explicitCloseBackdrop;
  }
  function pointStack(viewport) {
    if (!viewport || typeof document.elementsFromPoint !== 'function') return [];
    const x = Number(viewport.x);
    const y = Number(viewport.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return [];
    const seen = [];
    return Array.from(document.elementsFromPoint(x, y) || []).filter(function (node) {
      if (!node || node.nodeType !== 1 || seen.includes(node)) return false;
      seen.push(node);
      return true;
    });
  }
  function activationLabels(el) {
    const labels = [];
    function append(label) {
      if (label && label.nodeType === 1 && !labels.includes(label)) labels.push(label);
    }
    if (!el || el.nodeType !== 1) return labels;
    if (el.matches && el.matches('label')) append(el);
    append(nearest(el, 'label'));
    try {
      Array.from(el.labels || []).forEach(append);
    } catch (error) {}
    return labels;
  }
  function sharesActivationLabel(node, host) {
    const nodeLabels = activationLabels(node);
    const hostLabels = activationLabels(host);
    const nodeControl = node && node.matches && node.matches(CONTROL_SELECTOR)
      ? node
      : nearest(node, CONTROL_SELECTOR);
    if (nodeControl && nodeControl !== host && !host.contains(nodeControl)) return false;
    return nodeLabels.some(function (label) {
      return hostLabels.includes(label) || label.control === host;
    });
  }
  function isNonInterceptingOverlay(node) {
    if (!node || node.nodeType !== 1) return false;
    const style = window.getComputedStyle(node);
    return style.pointerEvents === 'none' && Number(style.opacity || '1') < 0.05;
  }
  function isSameInteractionSurface(node, host) {
    return !!(node && host && (
      node === host ||
      host.contains(node) ||
      sharesActivationLabel(node, host)
    ));
  }
  function topmostContains(host, viewport) {
    if (!host || !isVisible(host)) return false;
    if (!viewport) return true;
    const stack = pointStack(viewport);
    for (const node of stack) {
      if (isSameInteractionSurface(node, host)) return true;
      // Some engines retain pointer-events:none descendants in the point
      // stack. They are observational layers only and cannot consume the
      // click, so continue to the first real hit-test owner below them.
      if (isNonInterceptingOverlay(node)) continue;
      return false;
    }
    return false;
  }
  function handlerText(host) {
    let value = attr(host, 'onclick');
    try {
      if (host && typeof host.onclick === 'function') value += ' ' + String(host.onclick);
    } catch (error) {}
    return compact(value, 500).toLowerCase();
  }
  function lucideIconTokens(host) {
    if (!host || !host.querySelectorAll) return '';
    return Array.from(host.querySelectorAll('[data-lucide]'))
      .map(function (node) { return attr(node, 'data-lucide').toLowerCase(); })
      .filter(Boolean)
      .join(' ');
  }
  function isTopChromeControl(host) {
    if (!host || !host.getBoundingClientRect) return false;
    const rect = host.getBoundingClientRect();
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 390;
    if (rect.top > 140) return false;
    return rect.left <= 96 || rect.right >= viewportWidth - 96;
  }
  function hasOwnAuthority(el) {
    return !!(
      attr(el, 'data-ghostweb-action') ||
      attr(el, 'data-ghostweb-route-action') ||
      attr(el, 'data-ghostweb-target') ||
      attr(el, 'data-ghostweb-effect-kind') ||
      attr(el, 'data-ghostweb-query-result') ||
      attr(el, 'data-ghostweb-query-suggestion') ||
      attr(el, 'data-ghostweb-opens-template') ||
      attr(el, 'data-ghostweb-wheel-trigger') ||
      hasSemanticTransactionAuthority(el) ||
      attr(el, 'data-ghostweb-action-id') ||
      attr(el, 'data-action-id')
    );
  }
  function hasSemanticTransactionAuthority(el) {
    if (attr(el, 'data-ghostweb-actionability-authority') !== 'semantic_transaction') return false;
    const sourceIds = attr(el, 'data-ghostweb-semantic-source-ids');
    const transactionIds = attr(el, 'data-ghostweb-semantic-transaction-ids');
    const targetKind = attr(el, 'data-ghostweb-semantic-target-kind');
    return !!sourceIds && !!transactionIds &&
      (targetKind === 'page_instance' || targetKind === 'interaction_state' || targetKind === 'explicit_boundary');
  }
  function hasTaskRelatedControlSignal(el) {
    return !!(
      attr(el, 'data-action-aliases') ||
      attr(el, 'data-element-aliases')
    );
  }
  function intrinsicNavigationHandlerHost(element, viewport) {
    // A real visible route control may be a semantic container whose child
    // carries the formal task metadata.  When a point lands on the container,
    // preserve the page-owned navigation handler as the click authority rather
    // than inheriting an unrelated gesture-only ancestor (for example a
    // carousel shell).  This is intentionally contract-based: only handlers
    // that call the generic Ghostweb route executor qualify.
    let candidate = element;
    while (candidate) {
      const handlers = handlerText(candidate);
      const hasRouteHandler = /ghostwebrouteexecution\\.(?:navigate|step)\\s*\\(/.test(handlers);
      if (hasRouteHandler &&
          // A route handler is only a click authority when this element owns
          // the formal action identity.  Generated route shells may carry a
          // GhostwebRouteExecution.navigate() onclick around a child
          // data-action-id host; selecting that shell would navigate without
          // preserving action_observation for /step.
          hasOwnAuthority(candidate) &&
          !candidate.hasAttribute('data-gesture-host') &&
          !candidate.hasAttribute('data-ghostweb-gesture-action') &&
          isVisible(candidate) &&
          !isDisabledNative(candidate) &&
          inActiveLayer(candidate) &&
          topmostContains(candidate, viewport)) {
        return candidate;
      }
      candidate = candidate.parentElement;
    }
    return null;
  }
  function authorityHost(element, viewport) {
    if (!element || !element.closest) return null;
    // Gesture hosts are authority for wheel/pointerup only.  They may wrap a
    // whole page (for example a horizontal carousel shell), so inheriting
    // their data-action-id for a click makes unrelated controls appear
    // non-interactive.  Resolve the nearest click-owned host first and skip
    // gesture-only ancestors for click-like events.
    const eventType = String((viewport && viewport.event_type) || '').toLowerCase();
    const clickLike = eventType === 'click' || eventType === 'double_tap' || eventType === 'long_press';
    if (clickLike) {
      const handlerHost = intrinsicNavigationHandlerHost(element, viewport);
      if (handlerHost) return handlerHost;
    }
    let host = element.matches && element.matches(EXECUTION_AUTHORITY_SELECTOR)
      ? element
      : nearest(element, EXECUTION_AUTHORITY_SELECTOR);
    if (clickLike) {
      let candidate = element;
      while (candidate) {
        if (candidate.matches && candidate.matches(EXECUTION_AUTHORITY_SELECTOR)) {
          const gestureOnly = candidate.hasAttribute('data-gesture-host') ||
            candidate.hasAttribute('data-ghostweb-gesture-action');
          if (!gestureOnly) {
            host = candidate;
            break;
          }
        }
        candidate = candidate.parentElement;
      }
      if (host && (host.hasAttribute('data-gesture-host') || host.hasAttribute('data-ghostweb-gesture-action'))) {
        host = null;
      }
    }
    if (!host || isDisabledNative(host) || !isVisible(host) || !inActiveLayer(host) || !topmostContains(host, viewport)) return null;
    if (hasOwnAuthority(host)) return host;
    return null;
  }
  function taskRelatedControlHost(element, viewport) {
    if (!element || !element.closest) return null;
    const control = element.matches && element.matches(CONTROL_SELECTOR)
      ? element
      : nearest(element, CONTROL_SELECTOR);
    if (!control || isDisabledNative(control) || !isVisible(control) || !inActiveLayer(control) || !topmostContains(control, viewport)) return null;
    return hasTaskRelatedControlSignal(control) ? control : null;
  }
  function isLegalNativeControl(control) {
    if (!control || !control.matches || !control.matches(NATIVE_CONTROL_SELECTOR)) return false;
    if (control.matches('input')) {
      return attr(control, 'type').toLowerCase() !== 'hidden';
    }
    if (!control.matches('summary')) return true;
    const details = control.parentElement;
    if (!details || !details.matches || !details.matches('details')) return false;
    const firstSummary = Array.from(details.children || []).find(function (child) {
      return child.matches && child.matches('summary');
    });
    return firstSummary === control;
  }
  function nativeControlHost(element, viewport) {
    if (!element || !element.closest) return null;
    const control = element.matches && element.matches(NATIVE_CONTROL_SELECTOR)
      ? element
      : nearest(element, NATIVE_CONTROL_SELECTOR);
    if (!isLegalNativeControl(control) ||
        isDisabledNative(control) ||
        !isVisible(control) ||
        !inActiveLayer(control) ||
        !topmostContains(control, viewport)) return null;
    return control;
  }
  function backControlHost(element, viewport) {
    if (!element || !element.closest) return null;
    const host = element.matches && element.matches(BACK_CONTROL_SELECTOR)
      ? element
      : nearest(element, BACK_CONTROL_SELECTOR);
    if (!host || isDisabledNative(host) || !isVisible(host) || !inActiveLayer(host) || !topmostContains(host, viewport)) return null;
    const ghostAction = attr(host, 'data-ghostweb-action');
    const routeAction = attr(host, 'data-ghostweb-route-action');
    if (ghostAction === 'history_back' ||
        ghostAction === 'returnBack' ||
        routeAction === 'default_back') {
      return host;
    }
    const identity = compact([
      host.id || '',
      attr(host, 'class'),
      attr(host, 'aria-label'),
      attr(host, 'title'),
      attr(host, 'data-element-id'),
      host.textContent || ''
    ].join(' '), 300);
    const handlers = handlerText(host);
    if (/history\\.back\\s*\\(|ghostwebrouteexecution\\.(?:goback|returnback)\\s*\\(/.test(handlers)) return host;
    const icons = lucideIconTokens(host);
    if (isTopChromeControl(host) && /(^|\\s)(chevron-left|arrow-left|circle-arrow-left|corner-up-left)(\\s|$)/.test(icons)) return host;
    if (isTopChromeControl(host) && /(^|\\s)(x|x-circle|circle-x)(\\s|$)/.test(icons)) return host;
    if (/返回|上一页|回到上一级|关闭/.test(identity)) return host;
    return /(^|[\\s_-])(btn)?back(btn|button)?([\\s_-]|$)/i.test(identity) ? host : null;
  }
  function hrefHost(element, viewport) {
    if (!element || !element.closest) return null;
    const host = element.matches && element.matches('a[href]')
      ? element
      : nearest(element, 'a[href]');
    if (!host || !isValidHref(host) || isDisabledNative(host) || !isVisible(host) || !inActiveLayer(host) || !topmostContains(host, viewport)) return null;
    return host;
  }
  function labelFor(el) {
    if (!el) return '';
    const values = [
      attr(el, 'aria-label'),
      attr(el, 'title'),
      attr(el, 'data-element-id'),
      attr(el, 'data-ghostweb-action-id'),
      el.textContent || ''
    ];
    for (const value of values) {
      const label = compact(value, 96);
      if (label) return label;
    }
    return '';
  }
  function messageFor(status, label) {
    if (status === 'interactive') return '';
    if (status === 'background') return 'No actionable component was found at the selected point.';
    if (status === 'unknown') return 'The selected component does not expose a Ghostweb action contract.';
    return label
      ? 'The selected component is not interactive: ' + label
      : 'The selected component is not interactive.';
  }
  function actionIdsFor(el) {
    const ids = [];
    function append(value) {
      String(value || '').split(/\\s+/).filter(Boolean).forEach(function (id) {
        if (!ids.includes(id)) ids.push(id);
      });
    }
    if (!el) return ids;
    append(attr(el, 'data-ghostweb-action-id'));
    append(attr(el, 'data-action-id'));
    append(attr(el, 'data-action-aliases'));
    return ids;
  }
  function executionContextForActionability() {
    let context = null;
    try { context = window.__ghostwebExecutionContext || null; } catch (error) {}
    if (context && typeof context === 'object') return context;
    for (const storageName of ['sessionStorage', 'localStorage']) {
      try {
        const storage = window[storageName];
        context = JSON.parse(storage.getItem('ghostweb.execution_context') || 'null');
        if (context && typeof context === 'object') return context;
      } catch (error) {}
    }
    return null;
  }
  function contextualActionIdFor(el) {
    const ids = actionIdsFor(el);
    if (!ids.length) return '';
    const declared = attr(el, 'data-ghostweb-context-action-id');
    if (declared && ids.includes(declared)) return declared;
    const context = executionContextForActionability();
    const active = String((context && context.action_id) || window.__ghostwebActiveActionId || '').trim();
    if (active && ids.includes(active)) return active;
    const traceId = String(
      (context && (context.reference_trace_id || context.trace_id || context.task_id)) ||
      window.__ghostwebActiveTraceId || ''
    ).trim();
    if (!traceId) return '';
    const matches = ids.filter(function (id) { return id.indexOf(traceId + ':step') === 0; });
    return matches.length === 1 ? matches[0] : '';
  }
  function baseResult(status, el, reason, viewport) {
    const label = labelFor(el);
    // Shared DOM hosts retain their page-owned primary id, while the WebEnv
    // task context selects a trace-specific alias for the observation.  Do
    // this in the inspector as well as the optional action-effect bridge: a
    // sealed package can legitimately omit that bridge yet still carries the
    // context injected by the host before every visible action.
    const contextualActionId = contextualActionIdFor(el);
    const result = {
      schema: SCHEMA,
      status: status,
      response_mode: getResponseMode(),
      message: messageFor(status, label),
      label: label,
      element_id: attr(el, 'data-element-id') || attr(el, 'id'),
      action_id: contextualActionId || attr(el, 'data-ghostweb-action-id') || attr(el, 'data-action-id'),
      action_kind: attr(el, 'data-ghostweb-action') || attr(el, 'data-ghostweb-route-action') || attr(el, 'data-ghostweb-effect-kind'),
      target: attr(el, 'data-ghostweb-target') || attr(el, 'href'),
      reason: reason,
      source: 'ghostweb_dom_contract',
      viewport: viewport || null,
      timestamp: Date.now()
    };
    lastResult = result;
    window.__ghostwebLastActionability = result;
    return result;
  }
  function inspectElement(element, options) {
    const viewport = options && options.viewport ? options.viewport : null;
    if (!element || element.nodeType !== 1) {
      return baseResult('background', null, 'no_element_at_point', viewport);
    }
    const disabled = nearest(element, '[disabled], [aria-disabled="true"]');
    if (disabled) return baseResult('disabled', disabled, 'aria_or_dom_disabled', viewport);
    const backHost = backControlHost(element, viewport);
    if (backHost) return baseResult('interactive', backHost, 'intrinsic_back_control', viewport);
    const host = authorityHost(element, viewport);
    if (host) return baseResult('interactive', host, 'action_contract_present', viewport);
    const taskControl = taskRelatedControlHost(element, viewport);
    if (taskControl) return baseResult('interactive', taskControl, 'task_related_control', viewport);
    const nativeControl = nativeControlHost(element, viewport);
    if (nativeControl) return baseResult('interactive', nativeControl, 'native_control', viewport);
    const link = hrefHost(element, viewport);
    if (link) return baseResult('interactive', link, 'href_route_authority', viewport);
    if (!inActiveLayer(element)) return baseResult('noninteractive', element, 'masked_by_active_layer', viewport);
    return baseResult('noninteractive', element, 'masked_region', viewport);
  }
  function labelControlCandidates(element) {
    const candidates = [];
    function append(candidate) {
      if (candidate && candidate.nodeType === 1 && !candidates.includes(candidate)) {
        candidates.push(candidate);
      }
    }
    activationLabels(element).forEach(function (label) {
      append(label.control);
    });
    return candidates;
  }
  function inspectPointStack(stack, options) {
    const viewport = options && options.viewport ? options.viewport : null;
    const candidates = [];
    function append(candidate) {
      if (candidate && candidate.nodeType === 1 && !candidates.includes(candidate)) {
        candidates.push(candidate);
      }
    }
    stack.forEach(function (node) {
      append(node);
      labelControlCandidates(node).forEach(append);
    });
    for (const candidate of candidates) {
      // This precondition is the occlusion boundary. A candidate that merely
      // appears deeper in elementsFromPoint is never enough: the first real
      // hit owner must be the host, its descendant, activating label, or a
      // non-intercepting layer before the candidate can contribute authority.
      if (!isVisible(candidate) || !inActiveLayer(candidate) || !topmostContains(candidate, viewport)) {
        continue;
      }
      const result = inspectElement(candidate, options);
      if (result.status === 'interactive' || result.status === 'disabled') return result;
    }
    return null;
  }
  function inspectPoint(x, y, options) {
    const vx = Number(x);
    const vy = Number(y);
    if (!Number.isFinite(vx) || !Number.isFinite(vy)) {
      return baseResult('bridge_error', null, 'invalid_viewport_point', null);
    }
    const eventType = options && options.event_type ? String(options.event_type) : '';
    const inspectedOptions = Object.assign({}, options || {}, {
      viewport: { x: vx, y: vy, event_type: eventType }
    });
    const stack = pointStack(inspectedOptions.viewport);
    if (!stack.length) return baseResult('background', null, 'no_element_at_point', inspectedOptions.viewport);
    const resolved = inspectPointStack(stack, inspectedOptions);
    return resolved || inspectElement(stack[0], inspectedOptions);
  }
  window.GhostwebActionability = {
    installed: true,
    schema: SCHEMA,
    getResponseMode: getResponseMode,
    setResponseMode: setResponseMode,
    inspectElement: inspectElement,
    inspectPoint: inspectPoint,
    lastResult: function () { return lastResult; }
  };
})();
</script>
""".strip()


def _inject_actionability_inspector_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    script = _actionability_inspector_runtime_script()
    runtime_re = re.compile(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']actionability-inspector["\'])[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    body_end = re.search(r"</body\s*>", text, re.IGNORECASE)
    if body_end:
        insert_at = body_end.start()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    head_match = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    return text + "\n" + script, True


def _input_focus_state_runtime_script() -> str:
    return """
<script data-ghostweb-runtime="input-focus-state">
(function () {
  if (window.GhostwebInputFocusState && window.GhostwebInputFocusState.installed) return;
  const EDITABLE_SELECTOR = [
    'textarea',
    '[contenteditable="true"]',
    '[contenteditable=true]',
    'input:not([type])',
    'input[type="text"]',
    'input[type="search"]',
    'input[type="email"]',
    'input[type="tel"]',
    'input[type="url"]',
    'input[type="number"]',
    'input[type="password"]'
  ].join(',');
  const SKIP_SURFACE_SELECTOR = [
    'button',
    'a[href]',
    'select',
    '[role="button"]',
    '[data-ghostweb-click-contract-kind="noninteractive"]'
  ].join(',');
  const PLACEHOLDER_ATTR = 'data-ghostweb-placeholder-original';
  function ensureStyle() {
    if (document.querySelector('style[data-ghostweb-runtime="input-focus-state-style"]')) return;
    const style = document.createElement('style');
    style.setAttribute('data-ghostweb-runtime', 'input-focus-state-style');
    style.textContent = [
      '[data-ghostweb-input-state="focused"],[data-ghostweb-input-state="focused-filled"]{caret-color:#2563eb!important;}',
      '[contenteditable][data-ghostweb-input-state="focused"],[contenteditable][data-ghostweb-input-state="focused-filled"]{min-height:1.2em;}',
      '[data-ghostweb-mobile-input-surface="true"]{background:#f3f4f6!important;border-radius:9999px!important;padding:4px!important;box-sizing:border-box!important;}',
      '[data-ghostweb-mobile-input-surface="true"] input:not([type]),[data-ghostweb-mobile-input-surface="true"] input[type="text"],[data-ghostweb-mobile-input-surface="true"] input[type="search"],[data-ghostweb-mobile-input-surface="true"] input[type="email"],[data-ghostweb-mobile-input-surface="true"] input[type="tel"],[data-ghostweb-mobile-input-surface="true"] input[type="url"],[data-ghostweb-mobile-input-surface="true"] input[type="number"],[data-ghostweb-mobile-input-surface="true"] input[type="password"]{background:#fff!important;border-radius:9999px!important;caret-color:#2563eb!important;}'
    ].join('\\n');
    (document.head || document.documentElement).appendChild(style);
  }
  function isEditable(el) {
    return !!(el && el.nodeType === 1 && el.matches && el.matches(EDITABLE_SELECTOR));
  }
  function valueOf(el) {
    if (!el) return '';
    if ('value' in el) return String(el.value || '');
    return String(el.textContent || '');
  }
  function stateFor(el, focused) {
    if (focused) return valueOf(el) ? 'focused-filled' : 'focused';
    return valueOf(el) ? 'filled' : '';
  }
  function surfaceFor(el) {
    if (!el || !el.parentElement) return null;
    return el.closest('[data-ghostweb-input-surface]') ||
      el.closest('label') ||
      el.parentElement;
  }
  function textProbe(el) {
    if (!el || !el.getAttribute) return '';
    return [
      el.id || '',
      el.name || '',
      el.getAttribute('type') || '',
      el.getAttribute('role') || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || '',
      el.getAttribute('placeholder') || '',
      el.getAttribute('data-element-id') || '',
      el.getAttribute('data-role') || ''
    ].join(' ').toLowerCase();
  }
  function isSearchLikeEditable(el) {
    if (!isEditable(el)) return false;
    const type = String(el.getAttribute('type') || '').toLowerCase();
    const role = String(el.getAttribute('role') || '').toLowerCase();
    if (type === 'search' || role === 'searchbox') return true;
    return /\\b(search|query|keyword|kw)\\b|搜索/.test(textProbe(el));
  }
  function mobileSurfaceRequested(el) {
    const surface = surfaceFor(el);
    const requestedByControl = !!(el && el.getAttribute &&
      el.getAttribute('data-ghostweb-mobile-input-surface') === 'true');
    const requestedBySurface = !!(surface && surface.getAttribute &&
      surface.getAttribute('data-ghostweb-mobile-input-surface') === 'true');
    return requestedByControl || requestedBySurface;
  }
  function shouldPrepareMobileSurface(el) {
    if (!isEditable(el) || !el.matches || !el.matches('input,textarea')) return false;
    if (el.disabled || el.readOnly) return false;
    if (el.getAttribute && el.getAttribute('data-ghostweb-mobile-input-surface') === 'skip') return false;
    if (el.closest && el.closest('[data-ghostweb-mobile-input-surface="skip"],[data-ghostweb-input-surface-style="preserve"]')) return false;
    return mobileSurfaceRequested(el);
  }
  function prepareMobileInputSurface(el) {
    if (!shouldPrepareMobileSurface(el)) return;
    const surface = surfaceFor(el);
    if (!surface || surface === document.body || surface === document.documentElement) return;
    if (surface.getAttribute && surface.getAttribute('data-ghostweb-mobile-input-surface') === 'skip') return;
    surface.setAttribute('data-ghostweb-mobile-input-surface', 'true');
    if (!surface.hasAttribute('data-ghostweb-input-surface')) {
      surface.setAttribute('data-ghostweb-input-surface', 'mobile');
    }
    el.setAttribute('data-ghostweb-mobile-input-control', 'true');
    el.style.caretColor = '#2563eb';
    el.style.backgroundColor = '#fff';
  }
  function hidePlaceholderForFocus(el) {
    if (!el || !el.getAttribute || !el.setAttribute || valueOf(el)) return;
    const current = el.getAttribute('placeholder');
    if (!current) return;
    if (!el.hasAttribute(PLACEHOLDER_ATTR)) {
      el.setAttribute(PLACEHOLDER_ATTR, current);
    }
    el.setAttribute('placeholder', '');
    el.setAttribute('data-ghostweb-placeholder-hidden', 'true');
  }
  function restorePlaceholderAfterBlur(el) {
    if (!el || !el.getAttribute || !el.setAttribute || valueOf(el)) return;
    const original = el.getAttribute(PLACEHOLDER_ATTR);
    if (!original) return;
    el.setAttribute('placeholder', original);
    el.removeAttribute('data-ghostweb-placeholder-hidden');
  }
  function setState(el, focused) {
    if (!isEditable(el)) return;
    if (focused) {
      hidePlaceholderForFocus(el);
    } else {
      restorePlaceholderAfterBlur(el);
    }
    const state = stateFor(el, focused);
    const surface = surfaceFor(el);
    if (state) {
      el.setAttribute('data-ghostweb-input-state', state);
      el.setAttribute('data-ghostweb-awaiting-input', focused ? 'true' : 'false');
    } else {
      el.removeAttribute('data-ghostweb-input-state');
      el.removeAttribute('data-ghostweb-awaiting-input');
    }
    if (surface) {
      if (state) {
        surface.setAttribute('data-ghostweb-input-surface-state', state);
        surface.setAttribute('data-ghostweb-focused-input', focused ? 'true' : 'false');
      } else {
        surface.removeAttribute('data-ghostweb-input-surface-state');
        surface.removeAttribute('data-ghostweb-focused-input');
      }
    }
  }
  function bind(el) {
    if (!isEditable(el) || el.__ghostwebInputFocusBound) return;
    el.__ghostwebInputFocusBound = true;
    prepareMobileInputSurface(el);
    el.addEventListener('focus', function () { setState(el, true); }, true);
    el.addEventListener('blur', function () { setState(el, false); }, true);
    el.addEventListener('input', function () { setState(el, document.activeElement === el); }, true);
    setState(el, document.activeElement === el);
  }
  function bindAll(root) {
    const scope = root && root.querySelectorAll ? root : document;
    if (isEditable(scope)) bind(scope);
    scope.querySelectorAll(EDITABLE_SELECTOR).forEach(bind);
  }
  function focusCandidateFromSurface(target) {
    if (!target || !target.closest || isEditable(target)) return null;
    if (target.closest(SKIP_SURFACE_SELECTOR)) return null;
    let node = target;
    let depth = 0;
    while (node && node !== document.body && depth < 5) {
      if (node.querySelectorAll) {
        const inputs = Array.from(node.querySelectorAll(EDITABLE_SELECTOR)).filter(function (item) {
          return !item.disabled && !item.readOnly;
        });
        if (inputs.length === 1) return inputs[0];
      }
      node = node.parentElement;
      depth += 1;
    }
    return null;
  }
  function focusEditable(el) {
    if (!isEditable(el)) return;
    prepareMobileInputSurface(el);
    try {
      el.focus({ preventScroll: true });
    } catch (error) {
      try { el.focus(); } catch (_ignored) {}
    }
    setState(el, true);
  }
  function visibleEditable(el) {
    if (!isEditable(el) || el.disabled || el.readOnly) return false;
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    return !style || (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0');
  }
  function focusInitialEditable() {
    if (document.activeElement && isEditable(document.activeElement)) {
      focusEditable(document.activeElement);
      return;
    }
    const candidates = Array.from(document.querySelectorAll(EDITABLE_SELECTOR)).filter(visibleEditable);
    const explicit = candidates.find(function (el) {
      return el.hasAttribute('autofocus') ||
        el.getAttribute('data-ghostweb-autofocus') === 'true' ||
        el.getAttribute('data-ghostweb-input-autofocus') === 'true';
    });
    const searchLike = candidates.find(isSearchLikeEditable);
    const target = explicit;
    if (!target) return;
    focusEditable(target);
    try {
      if ('selectionStart' in target && 'selectionEnd' in target) {
        const end = valueOf(target).length;
        target.setSelectionRange(end, end);
      }
    } catch (e) {}
  }
  ensureStyle();
  bindAll(document);
  document.addEventListener('focusin', function (event) {
    bind(event.target);
    if (isEditable(event.target)) setState(event.target, true);
  }, true);
  document.addEventListener('focusout', function (event) {
    if (isEditable(event.target)) setState(event.target, false);
  }, true);
  document.addEventListener('pointerdown', function (event) {
    const input = focusCandidateFromSurface(event.target);
    if (input) focusEditable(input);
  }, true);
  document.addEventListener('click', function (event) {
    const input = focusCandidateFromSurface(event.target);
    if (input) focusEditable(input);
  }, true);
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        record.addedNodes && record.addedNodes.forEach(function (node) {
          if (node && node.nodeType === 1) bindAll(node);
        });
      });
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      window.setTimeout(focusInitialEditable, 0);
    }, { once: true });
  } else {
    window.setTimeout(focusInitialEditable, 0);
  }
  window.GhostwebInputFocusState = {
    installed: true,
    bindAll: bindAll,
    focusEditable: focusEditable,
    focusInitialEditable: focusInitialEditable
  };
})();
</script>
""".strip()


def _inject_input_focus_state_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    if not re.search(r"<(?:input|textarea)\b|\bcontenteditable\s*=", text, re.IGNORECASE):
        return text, False
    script = _input_focus_state_runtime_script()
    runtime_re = re.compile(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']input-focus-state["\'])[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    head_match = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    body_match = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    if body_match:
        insert_at = body_match.start()
        return text[:insert_at] + script + "\n" + text[insert_at:], True
    return script + "\n" + text, True


def _query_state_runtime_script(query_contract: Dict[str, Any]) -> str:
    payload = json.dumps(query_contract or {}, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    preview_helpers = """
  function renderSuggestions(_input, _state) {
    return false;
  }
"""
    return f"""
<script data-ghostweb-runtime="query-state">
(function () {{
  const CONTRACT = {payload};
  const states = Array.isArray(CONTRACT && CONTRACT.states) ? CONTRACT.states : [];
  const aliases = Array.isArray(CONTRACT && CONTRACT.url_query_aliases) && CONTRACT.url_query_aliases.length
    ? CONTRACT.url_query_aliases
    : ['q', 'query', 'keyword', 'search', 'kw'];
  const hydration = CONTRACT && CONTRACT.hydration && typeof CONTRACT.hydration === 'object'
    ? CONTRACT.hydration
    : {{}};
  const renderPreviewPanel = hydration.render_preview_panel === true;
  let queryResultPointerActive = false;
  let queryResultPointerResetTimer = null;
  function holdQueryResultPointerLifecycle() {{
    queryResultPointerActive = true;
    if (queryResultPointerResetTimer) window.clearTimeout(queryResultPointerResetTimer);
    queryResultPointerResetTimer = window.setTimeout(function () {{
      queryResultPointerActive = false;
      queryResultPointerResetTimer = null;
    }}, 1500);
  }}
  function releaseQueryResultPointerLifecycle() {{
    queryResultPointerActive = false;
    if (queryResultPointerResetTimer) window.clearTimeout(queryResultPointerResetTimer);
    queryResultPointerResetTimer = null;
  }}
  function routedSuggestionHost(target) {{
    return target && target.closest
      ? target.closest(
        '[data-ghostweb-query-suggestion][data-ghostweb-query-target], ' +
        '[data-ghostweb-query-suggestion][data-ghostweb-target]'
      )
      : null;
  }}
  function norm(value) {{
    return String(value || '').trim().toLowerCase();
  }}
  function queryValue() {{
    try {{
      const params = new URLSearchParams(window.location.search || '');
      for (const alias of aliases) {{
        const value = params.get(alias);
        if (value && String(value).trim()) return String(value).trim();
      }}
    }} catch (e) {{}}
    return '';
  }}
  function selectedCityValue() {{
    try {{
      const params = new URLSearchParams(window.location.search || '');
      return params.get('cityNameOrId') || params.get('city_name_or_id') || params.get('city') || '';
    }} catch (e) {{}}
    return '';
  }}
  function primaryInputs() {{
    // Use only standard semantic markers for "search-like input" — type=search
    // and ARIA role=searchbox. Hardcoded Chinese placeholder fallbacks
    // ('搜索','商家','商品') removed 2026-05-29 (core pipeline 不耦合业务).
    // Pages whose search inputs lack the standard markers should be fixed at
    // the pagegen layer; this runtime no longer compensates with placeholder
    // text matching.
    return Array.from(document.querySelectorAll(
      'input[type="search"], input[role="searchbox"], [role="searchbox"] input, textarea[role="searchbox"]'
    ));
  }}
  function activeInput() {{
    const active = document.activeElement;
    if (active && /^(INPUT|TEXTAREA)$/.test(active.tagName || '')) return active;
    return primaryInputs()[0] || null;
  }}
  function matchingState(value) {{
    const text = norm(value);
    if (!text) return null;
    let fallback = null;
    for (const state of states) {{
      const query = norm(state && state.query);
      if (!query) continue;
      if (query === text) return state;
      if (!fallback && (text.indexOf(query) >= 0 || query.indexOf(text) >= 0)) fallback = state;
    }}
    return fallback;
  }}
{preview_helpers}
  function updateFromInput(input) {{
    if (!input) return;
    const value = String(input.value || '').trim();
    const state = matchingState(value);
    window.GhostwebQueryState = Object.assign(window.GhostwebQueryState || {{}}, {{
      contract: CONTRACT,
      current_query: value,
      current_state: state
    }});
    renderSuggestions(input, state);
  }}
  function submitTarget(state) {{
    const targets = Array.isArray(state && state.submit_targets) ? state.submit_targets : [];
    return targets.length ? String(targets[0].target_filename || '').trim() : '';
  }}
  function targetWithQuery(target, query) {{
    const text = String(target || '').trim();
    if (!text) return '';
    const separator = text.indexOf('?') >= 0 ? '&' : '?';
    return text + separator + 'q=' + encodeURIComponent(query || '');
  }}
  function routeTo(target, query) {{
    const url = targetWithQuery(target, query);
    if (!url) return false;
    const runtime = window.GhostwebRouteExecution;
    if (runtime && typeof runtime.navigate === 'function' && runtime.navigate(url, {{ mode: 'push' }})) return true;
    window.location.href = url;
    return true;
  }}
  function isSearchSubmit(host) {{
    if (!host) return false;
    // Element identity describes the owning page/component, not the control's
    // operation.  In particular, route hosts on a search-results page often
    // inherit ids containing "search-results"; treating that id as submit
    // authority intercepts the real route in capture phase.
    const text = [
      host.getAttribute('aria-label') || '',
      host.getAttribute('title') || '',
      host.textContent || ''
    ].join(' ');
    if (/清除|clear|reset/i.test(text)) return false;
    return /搜索|search|submit/i.test(text);
  }}
  function handleSubmit(event, input) {{
    const value = String((input && input.value) || '').trim();
    // An empty query is an invalid submit, not a route request.  Cancel it
    // before the generic action-host bridge can fall back to the button's
    // static trace target; otherwise a repeated search-icon click can expose
    // a result surface without the user entering the query.
    if (!value) {{
      if (event) {{
        event.preventDefault();
        event.stopImmediatePropagation();
        event.stopPropagation();
      }}
      return false;
    }}
    const state = matchingState(value);
    const target = submitTarget(state);
    if (!state || !target) return false;
    if (event) {{
      event.preventDefault();
      event.stopImmediatePropagation();
      event.stopPropagation();
    }}
    return routeTo(target, value || state.query || '');
  }}
  function hydrate() {{
    const value = queryValue();
    const inputs = primaryInputs();
    inputs.forEach(function (input) {{
      if (value && String(input.value || '').trim() !== value) {{
        input.value = value;
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
      }}
      updateFromInput(input);
      input.addEventListener('input', function () {{ updateFromInput(input); }});
      input.addEventListener('change', function () {{
        if (queryResultPointerActive) return; // skip the blur/change rerender that would swallow the first click
        updateFromInput(input);
      }});
      input.addEventListener('keydown', function (event) {{
        if (event.key === 'Enter') handleSubmit(event, input);
      }});
    }});
  }}
  document.addEventListener('pointerdown', function (event) {{
    if (routedSuggestionHost(event.target)) holdQueryResultPointerLifecycle();
  }}, true);
  document.addEventListener('click', function (event) {{
    const suggestion = routedSuggestionHost(event.target);
    if (!suggestion) return;
    const target = String(
      suggestion.getAttribute('data-ghostweb-query-target') ||
      suggestion.getAttribute('data-ghostweb-target') ||
      ''
    ).trim();
    if (!target) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    event.stopPropagation();
    const input = activeInput() || primaryInputs()[0];
    const query = String((input && input.value) || queryValue() || '').trim();
    const routed = routeTo(target, query);
    if (!routed) releaseQueryResultPointerLifecycle();
  }}, true);
  document.addEventListener('click', function (event) {{
    if (event.defaultPrevented) return;
    const host = event.target && event.target.closest
      ? event.target.closest('button, [role="button"], input[type="submit"], a')
      : null;
    if (!host || !isSearchSubmit(host)) return;
    const input = activeInput() || primaryInputs()[0];
    handleSubmit(event, input);
  }}, true);
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', hydrate);
  }} else {{
    hydrate();
  }}
}})();
</script>
""".strip()


def _inject_query_state_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    query_contract = page.get("query_state_contract", {}) if isinstance(page, dict) else {}
    if not isinstance(query_contract, dict) or not query_contract.get("states"):
        return text, False
    script = _query_state_runtime_script(query_contract)
    runtime_re = re.compile(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']query-state["\'])[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    head_match = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    body_match = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    if body_match:
        insert_at = body_match.start()
        return text[:insert_at] + script + "\n" + text[insert_at:], True
    return script + "\n" + text, True


def _record_form_hydration_contract_for_page(page: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(page, dict):
        return {}
    dynamic_contract = page.get("dynamic_surface_contract", {})
    route_contract = page.get("route_execution_contract", {})
    entities: List[str] = []
    if isinstance(dynamic_contract, dict):
        for component in dynamic_contract.get("stateful_surface_components", []):
            if not isinstance(component, dict):
                continue
            role = str(component.get("component_role") or "")
            entity_ref = str(component.get("entity_ref") or "").strip()
            if entity_ref and ("form" in role or "transaction" in role):
                entities.append(entity_ref)
        transitions = dynamic_contract.get("dynamic_transitions", [])
        for transition in (transitions if isinstance(transitions, list) else []):
            if not isinstance(transition, dict):
                continue
            entity_ref = str(transition.get("entity_ref") or "").strip()
            if entity_ref and str(transition.get("state_scope") or "") in {"draft", "transaction", "record"}:
                entities.append(entity_ref)
    if isinstance(route_contract, dict):
        stateful_actions = route_contract.get("stateful_actions", [])
        for action in (stateful_actions if isinstance(stateful_actions, list) else []):
            if not isinstance(action, dict):
                continue
            entity_ref = str(action.get("entity_ref") or "").strip()
            if entity_ref:
                entities.append(entity_ref)
    entities = sorted(dict.fromkeys(entities))
    if not entities:
        return {}
    return {
        "format": "record_form_hydration_contract",
        "schema_revision": 1,
        "filename": page.get("filename") or route_contract.get("filename") if isinstance(route_contract, dict) else "",
        "entities": [{"entity_ref": entity_ref} for entity_ref in entities],
    }


def _record_form_hydration_runtime_script(contract: Dict[str, Any]) -> str:
    payload = json.dumps(contract or {}, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return f"""
<script data-ghostweb-runtime="record-form-hydration">
(function () {{
  const CONTRACT = {payload};
  const entities = Array.isArray(CONTRACT && CONTRACT.entities) ? CONTRACT.entities : [];
  function cleanStem(value) {{
    return String(value || '').replace(/[^A-Za-z0-9_$]/g, '') || 'Entity';
  }}
  function snakeCase(value) {{
    return String(value || '')
      .replace(/(.)([A-Z][a-z]+)/g, '$1_$2')
      .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
      .replace(/[^A-Za-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .toLowerCase();
  }}
  function lowerCamel(value) {{
    const stem = cleanStem(value);
    return stem.slice(0, 1).toLowerCase() + stem.slice(1);
  }}
  function normalize(value) {{
    return String(value || '').replace(/[^A-Za-z0-9\\u4e00-\\u9fff]+/g, '').toLowerCase();
  }}
  function readIdentifier(entityRef) {{
    let params;
    try {{
      params = new URLSearchParams(window.location.search || '');
    }} catch (e) {{
      return '';
    }}
    const snake = snakeCase(entityRef);
    const camel = lowerCamel(entityRef);
    const aliases = ['id', 'identifier', snake + '_id', camel + 'Id'];
    for (const alias of aliases) {{
      const value = params.get(alias);
      if (value) return value;
    }}
    let fallback = '';
    params.forEach(function (value, key) {{
      if (!fallback && value && (/(_id|Id)$/.test(String(key || '')))) fallback = value;
    }});
    return fallback;
  }}
  function sdk() {{
    return window.WebsiteSDK || null;
  }}
  function callFormModel(entityRef, identifier) {{
    const api = sdk();
    if (!api || !identifier) return null;
    const stem = cleanStem(entityRef);
    const internal = '_gwGet' + stem + 'FormPageModel';
    if (typeof api[internal] === 'function') {{
      try {{ return api[internal]('edit', identifier); }} catch (e) {{}}
    }}
    const publicName = 'get' + stem + 'FormPageModel';
    if (typeof api[publicName] === 'function') {{
      try {{ return api[publicName]('edit', identifier); }} catch (e) {{}}
    }}
    const detail = 'get' + stem + 'Detail';
    if (typeof api[detail] === 'function') {{
      try {{ return api[detail](identifier); }} catch (e) {{}}
    }}
    const internalDetail = '_gwGet' + stem + 'Detail';
    if (typeof api[internalDetail] === 'function') {{
      try {{ return api[internalDetail](identifier); }} catch (e) {{}}
    }}
    return null;
  }}
  function firstObject(values) {{
    for (const value of values) {{
      if (value && typeof value === 'object' && !Array.isArray(value)) return value;
    }}
    return {{}};
  }}
  function isScalarValue(value) {{
    return value !== undefined && value !== null && typeof value !== 'object' && typeof value !== 'function';
  }}
  function collectValues(model, entityRef) {{
    const values = {{}};
    if (!model || typeof model !== 'object') return values;
    const lower = lowerCamel(entityRef);
    const record = firstObject([model.record, model.item, model.data, model[lower], model.title]);
    Object.keys(record || {{}}).forEach(function (key) {{
      const value = record[key];
      if (isScalarValue(value) && String(value).trim() !== '') values[key] = value;
    }});
    const fields = model.form && Array.isArray(model.form.fields) ? model.form.fields : [];
    fields.forEach(function (field) {{
      if (!field || typeof field !== 'object') return;
      const key = String(field.key || field.name || '').trim();
      if (!key) return;
      const value = isScalarValue(field.value) && String(field.value).trim() !== ''
        ? field.value
        : values[key];
      if (isScalarValue(value) && String(value).trim() !== '') values[key] = value;
    }});
    return values;
  }}
  function controlDescriptor(control) {{
    const pieces = [
      control.id || '',
      control.name || '',
      control.getAttribute('aria-label') || '',
      control.getAttribute('placeholder') || '',
      control.getAttribute('data-field') || '',
      control.getAttribute('data-field-key') || ''
    ];
    const label = control.id ? document.querySelector('label[for="' + CSS.escape(control.id) + '"]') : null;
    if (label) pieces.push(label.textContent || '');
    return normalize(pieces.join(' '));
  }}
  function setControlValue(control, value) {{
    if (!control || !isScalarValue(value)) return false;
    const text = String(value);
    const tag = String(control.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {{
      if (String(control.value || '') === text) return false;
      control.value = text;
      control.dispatchEvent(new Event('input', {{ bubbles: true }}));
      control.dispatchEvent(new Event('change', {{ bubbles: true }}));
      return true;
    }}
    return false;
  }}
  function isLowInformationKey(keyNorm) {{
    return !keyNorm ||
      keyNorm.length < 4 ||
      ['id', 'name', 'title', 'record', 'item', 'data', 'displayname'].indexOf(keyNorm) >= 0;
  }}
  // [G14 cleanup 2026-05-29]: removed business-keyword fuzzy aliasing helpers
  // that previously mapped record field keys to localized synonyms. Form input
  // descriptors and record field keys must align via entity_schema field names
  // (input.name == entity_schema.fields[].name == record key); mismatches must
  // surface as a contract bug, not be patched up by a fuzzy alias dictionary.
  function fieldMatchScore(descriptor, keyNorm) {{
    if (!descriptor || isLowInformationKey(keyNorm)) return 0;
    if (descriptor === keyNorm) return 1000 + keyNorm.length;
    if (descriptor.endsWith(keyNorm)) return 800 + keyNorm.length;
    if (descriptor.indexOf(keyNorm) >= 0) return 500 + keyNorm.length;
    if (keyNorm.indexOf(descriptor) >= 0 && descriptor.length >= 4) return 100 + descriptor.length;
    return 0;
  }}
  function applyValues(values) {{
    const controls = Array.from(document.querySelectorAll('input, textarea, select'));
    const entries = Object.keys(values || {{}})
      .map(function (key) {{ return [key, values[key], normalize(key)]; }})
      .filter(function (entry) {{ return isScalarValue(entry[1]) && !isLowInformationKey(entry[2]) && String(entry[1]).trim() !== ''; }});
    controls.forEach(function (control) {{
      const descriptor = controlDescriptor(control);
      if (!descriptor) return;
      let best = null;
      let bestScore = 0;
      for (const entry of entries) {{
        const keyNorm = entry[2];
        const score = fieldMatchScore(descriptor, keyNorm);
        if (score > bestScore) {{
          best = entry;
          bestScore = score;
        }}
      }}
      if (best && bestScore > 0) setControlValue(control, best[1]);
    }});
  }}
  function hydrate() {{
    entities.forEach(function (entity) {{
      const entityRef = String(entity && entity.entity_ref || '').trim();
      if (!entityRef) return;
      const identifier = readIdentifier(entityRef);
      if (!identifier) return;
      const model = callFormModel(entityRef, identifier);
      const values = collectValues(model, entityRef);
      applyValues(values);
    }});
  }}
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', function () {{ setTimeout(hydrate, 0); }});
  }} else {{
    setTimeout(hydrate, 0);
  }}
}})();
</script>
""".strip()


def _inject_record_form_hydration_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    contract = _record_form_hydration_contract_for_page(page)
    if not contract:
        return text, False
    script = _record_form_hydration_runtime_script(contract)
    runtime_re = re.compile(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']record-form-hydration["\'])[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    body_match = re.search(r"</body\s*>", text, re.IGNORECASE)
    if body_match:
        insert_at = body_match.start()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    return text + "\n" + script, True


def _overlay_hit_test_runtime_script() -> str:
    return """
<script data-ghostweb-runtime="overlay-hit-test">
(function () {
  if (window.__ghostwebStableOverlayHitTestInstalled) return;
  window.__ghostwebStableOverlayHitTestInstalled = true;
  const OVERLAY_SELECTOR = '[role="dialog"], [aria-modal="true"], [id*="Overlay"], [id*="overlay"], [data-overlay], .modal, .drawer, .sheet';
  let scheduled = false;
  let scanning = false;
  function setStyle(node, prop, value) {
    if (node && node.style && node.style[prop] !== value) node.style[prop] = value;
  }
  function setAttr(node, name, value) {
    value = String(value || '');
    if (node && node.getAttribute(name) !== value) node.setAttribute(name, value);
  }
  function isClosedOverlay(node) {
    if (!node || node.nodeType !== 1) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    const ariaHidden = node.getAttribute('aria-hidden');
    return !style || style.display === 'none' || style.visibility === 'hidden' ||
      (ariaHidden !== 'false' && Number(style.opacity || '1') < 0.05) ||
      rect.width <= 0 || rect.height <= 0 || node.classList.contains('hidden') ||
      ariaHidden === 'true';
  }
  function textTokens(node) {
    return [
      node.id || '',
      node.className || '',
      node.getAttribute('data-overlay') || '',
      node.getAttribute('data-ghostweb-overlay-layer-role') || ''
    ].join(' ').toLowerCase();
  }
  function hasForegroundOverlaySignal(node) {
    return !!node.closest('[role="dialog"], [aria-modal="true"], .modal, .drawer, .sheet') ||
      node.matches('[role="dialog"], [aria-modal="true"], .modal, .drawer, .sheet');
  }
  function isDimBackground(style, tokens) {
    const bg = String(style.backgroundColor || '').replace(/\\s+/g, '').toLowerCase();
    return tokens.includes('backdrop') || tokens.includes('dim') ||
      tokens.includes('bg-black/') || /^rgba\\(0,0,0,0\\.[1-9]/.test(bg);
  }
  function isBackdropLayer(node) {
    if (!node || node.nodeType !== 1 || hasForegroundOverlaySignal(node)) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    const tokens = textTokens(node);
    const viewportWidth = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
    const viewportHeight = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
    const parentRect = node.parentElement ? node.parentElement.getBoundingClientRect() : null;
    const fullScreenLike = tokens.includes('inset-0') ||
      (rect.width >= viewportWidth * 0.8 && rect.height >= viewportHeight * 0.6) ||
      (parentRect && rect.width >= parentRect.width * 0.8 && rect.height >= parentRect.height * 0.6);
    const explicitBackdrop = node.getAttribute('data-ghostweb-overlay-layer-role') === 'backdrop' ||
      tokens.includes('backdrop') || tokens.includes('dim') ||
      (tokens.includes('overlay') && isDimBackground(style, tokens));
    return fullScreenLike && explicitBackdrop;
  }
  function ensureHitTestable(overlay) {
    if (!overlay) return;
    if (isClosedOverlay(overlay)) {
      setStyle(overlay, 'pointerEvents', 'none');
      return;
    }
    setStyle(overlay, 'pointerEvents', 'auto');
    if (!isBackdropLayer(overlay) && !overlay.style.zIndex) setStyle(overlay, 'zIndex', '2147483000');
    if (isBackdropLayer(overlay)) setAttr(overlay, 'data-ghostweb-overlay-layer-role', 'backdrop');
    let node = overlay.parentElement;
    while (node && node !== document.body && node !== document.documentElement) {
      if (isClosedOverlay(node)) break;
      const style = window.getComputedStyle(node);
      if (style && style.pointerEvents === 'none') {
        node.classList.remove('pointer-events-none');
        setStyle(node, 'pointerEvents', 'auto');
        setAttr(node, 'data-ghostweb-overlay-hit-test-restored', 'true');
      }
      node = node.parentElement;
    }
  }
  function scan() {
    if (scanning) return;
    scanning = true;
    try {
      Array.from(document.querySelectorAll(OVERLAY_SELECTOR)).forEach(ensureHitTestable);
    } finally {
      scanning = false;
    }
  }
  function scheduleScan() {
    if (scheduled) return;
    scheduled = true;
    const run = function () {
      scheduled = false;
      scan();
    };
    if (window.requestAnimationFrame) window.requestAnimationFrame(run);
    else window.setTimeout(run, 0);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scheduleScan, { once: true });
  } else {
    scheduleScan();
  }
  document.addEventListener('click', scheduleScan, true);
  const observer = new MutationObserver(scheduleScan);
  if (document.documentElement) {
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class', 'style', 'aria-hidden'],
      subtree: true,
      childList: true
    });
  }
})();
</script>
""".strip()


def _inject_overlay_hit_test_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    if not re.search(
        r'(id=["\'][^"\']*[Oo]verlay|role=["\']dialog|aria-modal=["\']true|data-overlay|class=["\'][^"\']*(?:modal|drawer|sheet))',
        text,
        re.IGNORECASE,
    ):
        return text, False
    script = _overlay_hit_test_runtime_script()
    runtime_re = re.compile(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']overlay-hit-test["\'])[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    head_match = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    body_match = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    if body_match:
        insert_at = body_match.start()
        return text[:insert_at] + script + "\n" + text[insert_at:], True
    return script + "\n" + text, True


def _execution_obligation_action_surface_runtime_script(obligations: List[Dict[str, Any]]) -> str:
    payload = json.dumps(obligations or [], ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return (
        "<script data-ghostweb-metadata=\"execution-obligations\">\n"
        f"  const OBLIGATIONS = {payload};\n"
        "  window.__ghostwebExecutionObligations = OBLIGATIONS;\n"
        "</script>"
    )


def _obligation_action_surfaces_from_page(page: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_obligations: List[Any] = []
    if isinstance(page, dict):
        payload = page.get("page_execution_obligations", {})
        if isinstance(payload, dict):
            obligations = payload.get("obligations", [])
        else:
            obligations = payload
        if isinstance(obligations, list):
            raw_obligations.extend(obligations)
    page_environment = (
        page.get("page_environment_spec")
        or page.get("page_environment_projection")
    ) if isinstance(page, dict) else {}
    surfaces: List[Dict[str, Any]] = _page_environment_action_surfaces(page_environment)
    seen = {
        (
            str(surface.get("trace_id") or ""),
            int(surface.get("step_index") or 0),
            str(surface.get("operation") or "").strip().lower(),
            str(surface.get("expected_filename") or "").strip(),
            str(surface.get("input_text") or ""),
        )
        for surface in surfaces
    }
    for obligation in raw_obligations:
        if not isinstance(obligation, dict):
            continue
        expected = str(obligation.get("expected_filename") or "").strip()
        operation = str(obligation.get("operation") or "").strip().lower()
        dedupe_key = (
            str(obligation.get("trace_id") or ""),
            int(obligation.get("step_index") or 0),
            operation,
            expected,
            str(obligation.get("input_text") or ""),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if operation == "fill":
            input_text = str(obligation.get("input_text") or "").strip()
            command = str(obligation.get("command") or "").strip()
            if not expected or (not input_text and not command.startswith("type-text")):
                continue
            surfaces.append({
                "trace_id": str(obligation.get("trace_id") or ""),
                "step_index": int(obligation.get("step_index") or 0),
                "source_filename": _filename_without_query(str(obligation.get("source_filename") or "")),
                "operation": "fill",
                "operation_intent": str(obligation.get("operation_intent") or obligation.get("operation") or "")[:80],
                "intent_provenance": str(obligation.get("intent_provenance") or "trace_action"),
                "intent_visible_text_allowed": False,
                "expected_filename": expected,
                "input_text": input_text,
                "gesture": {},
            })
            continue
        gesture = obligation.get("gesture", {})
        if not isinstance(gesture, dict) or gesture.get("type") != "tap":
            continue
        point = gesture.get("hit_point") if isinstance(gesture.get("hit_point"), dict) else gesture
        try:
            x = int(float(point.get("x")))
            y = int(float(point.get("y")))
        except (AttributeError, TypeError, ValueError):
            continue
        if not expected:
            continue
        if operation and operation not in {"click", "tap"}:
            continue
        surfaces.append({
            "trace_id": str(obligation.get("trace_id") or ""),
            "step_index": int(obligation.get("step_index") or 0),
            "source_filename": _filename_without_query(str(obligation.get("source_filename") or "")),
            "operation": operation or "click",
            "operation_intent": str(obligation.get("operation_intent") or operation or "click")[:80],
            "intent_provenance": str(obligation.get("intent_provenance") or "trace_action"),
            "intent_visible_text_allowed": False,
            "expected_filename": expected,
            "gesture": {"type": "tap", "x": x, "y": y, "hit_point": {"x": x, "y": y}},
        })
    return surfaces


def _inject_execution_obligation_action_surface_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    enabled = bool(
        isinstance(page, dict)
        and page.get("page_execution_obligation_action_surface_runtime_enabled", True)
    )
    if not enabled:
        return text, False
    obligations = _obligation_action_surfaces_from_page(page)
    if not obligations:
        return text, False
    script = _execution_obligation_action_surface_runtime_script(obligations)
    runtime_re = re.compile(
        r'<script\b(?=[^>]*(?:\bdata-ghostweb-runtime=["\']execution-obligation-action-surface["\']|\bdata-ghostweb-metadata=["\']execution-obligations["\']))[^>]*>.*?</script>',
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    head_match = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    body_match = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    if body_match:
        insert_at = body_match.start()
        return text[:insert_at] + script + "\n" + text[insert_at:], True
    return script + "\n" + text, True


def _execution_obligation_geometry_runtime_script() -> str:
    return """
<script data-ghostweb-runtime="execution-obligation-geometry">
(function () {
  'use strict';
  if (window.GhostwebExecutionGeometry) return;
  function context() {
    try {
      const value = window.__ghostwebExecutionContext || JSON.parse(
        window.sessionStorage.getItem('ghostweb.execution_context') ||
        window.localStorage.getItem('ghostweb.execution_context') || '{}'
      );
      return value && typeof value === 'object' ? value : {};
    } catch (error) { return {}; }
  }
  function obligations() {
    const rows = window.__ghostwebExecutionObligations;
    if (Array.isArray(rows)) return rows;
    const metadata = document.querySelector(
      'script[data-ghostweb-metadata="execution-obligations"]'
    );
    const match = metadata && String(metadata.textContent || '').match(
      /const\\s+OBLIGATIONS\\s*=\\s*(\\[[\\s\\S]*?\\])\\s*;/
    );
    if (!match) return [];
    try {
      const parsed = JSON.parse(match[1]);
      if (Array.isArray(parsed)) {
        window.__ghostwebExecutionObligations = parsed;
        return parsed;
      }
    } catch (error) {}
    return [];
  }
  function actionId(value) {
    return String(value || '').trim();
  }
  function idsFor(host) {
    const ids = [];
    function append(value) {
      String(value || '').split(/\\s+/).filter(Boolean).forEach(function (id) {
        if (!ids.includes(id)) ids.push(id);
      });
    }
    if (!host || !host.getAttribute) return ids;
    append(host.getAttribute('data-ghostweb-action-id'));
    append(host.getAttribute('data-action-id'));
    append(host.getAttribute('data-action-aliases'));
    return ids;
  }
  function currentObligation() {
    const value = context();
    const traceId = String(value.reference_trace_id || value.trace_id || value.task_id || '').trim();
    const stepIndex = Number(value.step_index);
    if (!traceId || !Number.isInteger(stepIndex) || stepIndex < 1) return null;
    const tapRows = obligations().filter(function (candidate) {
      return candidate && candidate.trace_id === traceId &&
        candidate.gesture && candidate.gesture.type === 'tap';
    }).sort(function (left, right) {
      return Number(left.step_index) - Number(right.step_index);
    });
    const exactRows = tapRows.filter(function (candidate) {
      return Number(candidate.step_index) === stepIndex;
    });
    if (exactRows.length > 1) return {
      blocked: true,
      reason: 'ambiguous_exact_tap_obligation',
      trace_id: traceId,
      context_step_index: stepIndex,
      candidate_count: exactRows.length
    };
    const nextRows = tapRows.filter(function (candidate) {
      return Number(candidate.step_index) > stepIndex;
    });
    const nextStepIndex = nextRows.length ? Number(nextRows[0].step_index) : null;
    const nearestNextRows = nextStepIndex === null ? [] : nextRows.filter(function (candidate) {
      return Number(candidate.step_index) === nextStepIndex;
    });
    if (!exactRows.length && nearestNextRows.length > 1) return {
      blocked: true,
      reason: 'ambiguous_next_tap_obligation',
      trace_id: traceId,
      context_step_index: stepIndex,
      next_step_index: nextStepIndex,
      candidate_count: nearestNextRows.length
    };
    const exact = exactRows[0];
    const row = exact || nearestNextRows[0];
    if (!row || !row.gesture || !row.gesture.hit_point) return null;
    const obligationStepIndex = Number(row.step_index);
    if (!Number.isInteger(obligationStepIndex) || obligationStepIndex < 1) return null;
    const expectedActionId = traceId + ':step' + obligationStepIndex;
    const declaredX = Number(row.gesture.hit_point.x);
    const declaredY = Number(row.gesture.hit_point.y);
    if (!Number.isFinite(declaredX) || !Number.isFinite(declaredY)) return null;
    const sourcePoint = row.gesture.source_screen_point;
    let point = { x: declaredX, y: declaredY };
    let pointSource = 'declared_hit_point';
    if (sourcePoint && typeof sourcePoint === 'object') {
      const sourceX = Number(sourcePoint.x);
      const sourceY = Number(sourcePoint.y);
      const sourceWidth = Number(sourcePoint.width);
      const sourceHeight = Number(sourcePoint.height);
      if (Number.isFinite(sourceX) && Number.isFinite(sourceY) &&
          Number.isFinite(sourceWidth) && sourceWidth > 0 &&
          Number.isFinite(sourceHeight) && sourceHeight > 0) {
        point = {
          x: sourceX * window.innerWidth / sourceWidth,
          y: sourceY * window.innerHeight / sourceHeight
        };
        pointSource = 'source_screen_point_projected_to_viewport';
      }
    }
    return {
      row: row,
      action_id: expectedActionId,
      point: point,
      declared_point: { x: declaredX, y: declaredY },
      point_source: pointSource,
      obligation_selection: exact ? 'exact_context_step' : 'next_tap_after_sparse_context'
    };
  }
  function contextActionId() {
    const value = context();
    const traceId = String(value.reference_trace_id || value.trace_id || value.task_id || '').trim();
    const stepIndex = Number(value.step_index);
    if (!traceId || !Number.isInteger(stepIndex) || stepIndex < 1) return '';
    return traceId + ':step' + stepIndex;
  }
  function isSourceReferenceTask() {
    const value = context();
    const taskId = String(value.task_id || value.taskId || '').trim();
    const sourceReferenceTaskId = String(value.source_reference_task_id || '').trim();
    // A reference-derived enhanced task may reuse the source trace's action
    // namespace, but its reviewed visible GUI program owns its coordinates.
    // Only an exact reference task may be pre-aligned to source-trace points.
    return !sourceReferenceTaskId || sourceReferenceTaskId === taskId;
  }
  function hostFor(id) {
    const matches = Array.from(document.querySelectorAll(
      '[data-ghostweb-action-id], [data-action-id], [data-action-aliases]'
    )).filter(function (candidate) { return idsFor(candidate).includes(id); });
    return matches.length === 1 ? matches[0] : null;
  }
  function scrollContainersFor(host) {
    const containers = [];
    let node = host && host.parentElement;
    while (node && node !== document.body && node !== document.documentElement) {
      const style = window.getComputedStyle(node);
      const vertical = /(auto|scroll|overlay)/.test(String(style.overflowY || '')) &&
        node.scrollHeight > node.clientHeight + 1;
      const horizontal = /(auto|scroll|overlay)/.test(String(style.overflowX || '')) &&
        node.scrollWidth > node.clientWidth + 1;
      if (vertical || horizontal) {
        containers.push({ node: node, vertical: vertical, horizontal: horizontal });
      }
      node = node.parentElement;
    }
    return containers;
  }
  function rectFor(host) {
    if (!host || !host.getBoundingClientRect) return null;
    const rect = host.getBoundingClientRect();
    return {
      top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
      width: rect.width, height: rect.height,
      center_x: rect.left + rect.width / 2, center_y: rect.top + rect.height / 2
    };
  }
  function contains(rect, point) {
    return !!rect && point.x >= rect.left && point.x <= rect.right &&
      point.y >= rect.top && point.y <= rect.bottom;
  }
  function topmostAtPoint(host, point) {
    if (!host || !point || typeof document.elementsFromPoint !== 'function') return false;
    const stack = Array.from(document.elementsFromPoint(Number(point.x), Number(point.y)) || []);
    const top = stack.find(function (node) { return node && node.nodeType === 1; });
    return !!(top && (top === host || host.contains(top)));
  }
  function setScrollInstantly(container, left, top) {
    const prior = container.style.scrollBehavior;
    container.style.scrollBehavior = 'auto';
    if (typeof container.scrollTo === 'function') {
      container.scrollTo({ left: left, top: top, behavior: 'instant' });
    } else {
      container.scrollLeft = left;
      container.scrollTop = top;
    }
    container.style.scrollBehavior = prior;
  }
  function alignHostToPoint(host, point, details) {
    if (!host || !point || !Number.isFinite(Number(point.x)) || !Number.isFinite(Number(point.y))) {
      return { status: 'not_applicable', reason: 'invalid_host_or_viewport_point' };
    }
    const expectedPoint = { x: Number(point.x), y: Number(point.y) };
    const beforeRect = rectFor(host);
    if (!beforeRect || beforeRect.width <= 0 || beforeRect.height <= 0) {
      return { status: 'unsatisfied', reason: 'visible_host_geometry_missing', ...details };
    }
    const initiallySatisfied = contains(beforeRect, expectedPoint) && topmostAtPoint(host, expectedPoint);
    if (initiallySatisfied) {
      return { status: 'satisfied', reason: 'viewport_point_inside_visible_host', host_rect: beforeRect,
        point_topmost: true, ...details, alignment: { adjustments: [], scroll_adjustments_rolled_back: false } };
    }
    const containers = scrollContainersFor(host);
    const adjustments = [];
    let matched = false;
    for (const candidate of containers) {
      const rect = rectFor(host);
      if (!rect) break;
      const container = candidate.node;
      const adjustment = {
        vertical: candidate.vertical,
        horizontal: candidate.horizontal,
        before_scroll_top: container.scrollTop,
        before_scroll_left: container.scrollLeft
      };
      if (candidate.vertical && (expectedPoint.y < rect.top || expectedPoint.y > rect.bottom)) {
        const requestedTop = container.scrollTop + rect.center_y - expectedPoint.y;
        const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
        setScrollInstantly(container, container.scrollLeft, Math.max(0, Math.min(maxScrollTop, requestedTop)));
        adjustment.requested_scroll_top = requestedTop;
        adjustment.max_scroll_top = maxScrollTop;
      }
      if (candidate.horizontal && (expectedPoint.x < rect.left || expectedPoint.x > rect.right)) {
        const requestedLeft = container.scrollLeft + rect.center_x - expectedPoint.x;
        const maxScrollLeft = Math.max(0, container.scrollWidth - container.clientWidth);
        setScrollInstantly(container, Math.max(0, Math.min(maxScrollLeft, requestedLeft)), container.scrollTop);
        adjustment.requested_scroll_left = requestedLeft;
        adjustment.max_scroll_left = maxScrollLeft;
      }
      adjustment.after_scroll_top = container.scrollTop;
      adjustment.after_scroll_left = container.scrollLeft;
      adjustments.push(adjustment);
      const afterRect = rectFor(host);
      matched = !!afterRect && contains(afterRect, expectedPoint) && topmostAtPoint(host, expectedPoint);
      if (matched) break;
    }
    const rect = rectFor(host);
    return {
      status: matched ? 'aligned' : 'unsatisfied',
      reason: matched ? 'viewport_point_projected_to_visible_host' : 'viewport_point_outside_visible_host',
      host_rect: rect,
      point_topmost: !!rect && contains(rect, expectedPoint) && topmostAtPoint(host, expectedPoint),
      ...details,
      alignment: { adjustments: adjustments, scroll_adjustments_rolled_back: false }
    };
  }
  function alignToViewportPoint(point) {
    const obligation = currentObligation();
    if (!obligation) return { status: 'not_applicable', reason: 'current_formal_obligation_missing' };
    if (obligation.blocked) return {
      status: 'unsatisfied', reason: obligation.reason,
      trace_id: obligation.trace_id,
      context_step_index: obligation.context_step_index,
      next_step_index: obligation.next_step_index,
      candidate_count: obligation.candidate_count
    };
    const host = hostFor(obligation.action_id);
    if (!host) return {
      status: 'not_applicable', reason: 'current_formal_obligation_host_missing',
      expected_action_id: obligation.action_id,
      expected_point: obligation.point,
      declared_point: obligation.declared_point,
      point_source: obligation.point_source,
      obligation_selection: obligation.obligation_selection
    };
    return alignHostToPoint(host, point, {
      expected_action_id: obligation.action_id,
      expected_point: obligation.point,
      declared_point: obligation.declared_point,
      point_source: 'webenv_actionability_preflight',
      obligation_selection: obligation.obligation_selection
    });
  }
  function inspect() {
    const obligation = currentObligation();
    if (!obligation) return { status: 'not_applicable' };
    if (obligation.blocked) return {
      status: 'unsatisfied',
      reason: obligation.reason,
      trace_id: obligation.trace_id,
      context_step_index: obligation.context_step_index,
      next_step_index: obligation.next_step_index,
      candidate_count: obligation.candidate_count
    };
    const host = hostFor(obligation.action_id);
    if (!host) return {
      status: 'unsatisfied', reason: 'unique_visible_host_missing',
      expected_action_id: obligation.action_id, expected_point: obligation.point,
      declared_point: obligation.declared_point, point_source: obligation.point_source,
      obligation_selection: obligation.obligation_selection
    };
    const rect = rectFor(host);
    if (!rect || rect.width <= 0 || rect.height <= 0) return {
      status: 'unsatisfied', reason: 'visible_host_geometry_missing',
      expected_action_id: obligation.action_id, expected_point: obligation.point,
      declared_point: obligation.declared_point, point_source: obligation.point_source,
      obligation_selection: obligation.obligation_selection
    };
    const pointInside = contains(rect, obligation.point);
    const pointTopmost = pointInside && topmostAtPoint(host, obligation.point);
    return {
      status: pointInside && pointTopmost ? 'satisfied' : 'unsatisfied',
      reason: !pointInside
        ? 'reference_point_outside_visible_host'
        : (pointTopmost ? 'reference_point_inside_visible_host' : 'reference_point_occluded'),
      expected_action_id: obligation.action_id, expected_point: obligation.point,
      declared_point: obligation.declared_point, point_source: obligation.point_source,
      obligation_selection: obligation.obligation_selection,
      host_rect: rect,
      point_topmost: pointTopmost
    };
  }
  function align() {
    const before = inspect();
    if (before.status !== 'unsatisfied' || before.reason !== 'reference_point_outside_visible_host') {
      window.__ghostwebExecutionGeometry = before;
      return before;
    }
    const host = hostFor(before.expected_action_id);
    const containers = scrollContainersFor(host);
    if (!containers.length) {
      const result = {
        ...before,
        status: 'unsatisfied',
        alignment: {
          adjustments: [],
          scroll_adjustments_rolled_back: false
        }
      };
      window.__ghostwebExecutionGeometry = result;
      return result;
    }
    const adjustments = [];
    let after = before;
    containers.some(function (candidate) {
      const container = candidate.node;
      const rect = rectFor(host);
      const adjustment = {
        vertical: candidate.vertical,
        horizontal: candidate.horizontal,
        before_scroll_top: container.scrollTop,
        before_scroll_left: container.scrollLeft
      };
      if (candidate.vertical &&
          (before.expected_point.y < rect.top || before.expected_point.y > rect.bottom)) {
        const requestedTop = container.scrollTop + rect.center_y - Number(before.expected_point.y);
        const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
        const targetTop = Math.max(0, Math.min(maxScrollTop, requestedTop));
        setScrollInstantly(container, container.scrollLeft, targetTop);
        adjustment.requested_scroll_top = requestedTop;
        adjustment.max_scroll_top = maxScrollTop;
      }
      if (candidate.horizontal &&
          (before.expected_point.x < rect.left || before.expected_point.x > rect.right)) {
        const requestedLeft = container.scrollLeft + rect.center_x - Number(before.expected_point.x);
        const maxScrollLeft = Math.max(0, container.scrollWidth - container.clientWidth);
        const targetLeft = Math.max(0, Math.min(maxScrollLeft, requestedLeft));
        setScrollInstantly(container, targetLeft, container.scrollTop);
        adjustment.requested_scroll_left = requestedLeft;
        adjustment.max_scroll_left = maxScrollLeft;
      }
      adjustment.after_scroll_top = container.scrollTop;
      adjustment.after_scroll_left = container.scrollLeft;
      adjustments.push(adjustment);
      after = inspect();
      return after.status === 'satisfied';
    });
    let alignmentFailure = null;
    if (after.status !== 'satisfied' && adjustments.length) {
      alignmentFailure = {
        status: after.status,
        reason: after.reason,
        host_rect: after.host_rect,
        point_topmost: after.point_topmost
      };
      adjustments.slice().reverse().forEach(function (adjustment, index) {
        const candidate = containers[adjustments.length - index - 1];
        setScrollInstantly(
          candidate.node,
          Number(adjustment.before_scroll_left || 0),
          Number(adjustment.before_scroll_top || 0)
        );
        adjustment.rolled_back = true;
        adjustment.rollback_scroll_top = candidate.node.scrollTop;
        adjustment.rollback_scroll_left = candidate.node.scrollLeft;
      });
      after = inspect();
    }
    const result = {
      ...after,
      status: after.status === 'satisfied' ? 'aligned' : 'unsatisfied',
      alignment: {
        adjustments: adjustments,
        scroll_adjustments_rolled_back: !!alignmentFailure,
        failure_before_rollback: alignmentFailure
      }
    };
    window.__ghostwebExecutionGeometry = result;
    return result;
  }
  function scheduleAlign() {
    // A screenshot-grounded caller may resolve a currently visible host before
    // /step projects the provisional source-trace context. Do not move that
    // already visible, topmost host inside the synchronous context-update
    // boundary: doing so invalidates the fresh screenshot between grounding
    // and actionability inspection. Reference-point alignment remains available
    // when the selected host is outside the current viewport.
    if (!isSourceReferenceTask()) {
      return { status: 'not_applicable', reason: 'derivative_task_waits_for_actionability_preflight' };
    }
    const obligation = currentObligation();
    const host = obligation && !obligation.blocked ? hostFor(obligation.action_id) : null;
    const rect = rectFor(host);
    const viewportHeight = window.innerHeight ||
      (document.documentElement && document.documentElement.clientHeight) || 0;
    const visibleCenter = rect && rect.width > 0 && rect.height > 0 &&
      rect.bottom > 0 && rect.top < viewportHeight
      ? { x: rect.center_x, y: rect.center_y }
      : null;
    if (visibleCenter && topmostAtPoint(host, visibleCenter)) {
      const result = {
        status: 'satisfied',
        reason: 'visible_host_preserved_for_screenshot_grounding',
        expected_action_id: obligation.action_id,
        expected_point: obligation.point,
        declared_point: obligation.declared_point,
        point_source: obligation.point_source,
        obligation_selection: obligation.obligation_selection,
        host_rect: rect,
        point_topmost: true,
        alignment: { adjustments: [], scroll_adjustments_rolled_back: false }
      };
      window.__ghostwebExecutionGeometry = result;
      return result;
    }
    const result = align();
    if (window.requestAnimationFrame) window.requestAnimationFrame(align);
    return result;
  }
  window.GhostwebExecutionGeometry = {
    inspect: inspect,
    align: align,
    alignToViewportPoint: alignToViewportPoint
  };
  window.addEventListener('ghostweb:task-context-update', scheduleAlign);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scheduleAlign, { once: true });
  } else {
    scheduleAlign();
  }
})();
</script>
""".strip()


def _inject_execution_obligation_geometry_runtime(html_text: str, page: Dict[str, Any]) -> Tuple[str, bool]:
    text = str(html_text or "")
    declared_tap_obligation = any(
        isinstance(surface.get("gesture"), dict)
        and surface["gesture"].get("type") == "tap"
        for surface in _obligation_action_surfaces_from_page(page)
    )
    existing_obligation_metadata = bool(re.search(
        r"<script\b(?=[^>]*\bdata-ghostweb-metadata=[\"']execution-obligations[\"'])[^>]*>",
        text,
        re.IGNORECASE,
    ))
    if not declared_tap_obligation and not existing_obligation_metadata:
        return text, False
    script = _execution_obligation_geometry_runtime_script()
    runtime_re = re.compile(
        r"<script\b(?=[^>]*\bdata-ghostweb-runtime=[\"']execution-obligation-geometry[\"'])[^>]*>.*?</script>",
        re.IGNORECASE | re.DOTALL,
    )
    if runtime_re.search(text):
        updated = runtime_re.sub(lambda _match: script, text, count=1)
        return updated, updated != text
    body_end = re.search(r"</body\s*>", text, re.IGNORECASE)
    if body_end:
        insert_at = body_end.start()
        return text[:insert_at] + "\n" + script + "\n" + text[insert_at:], True
    return text + "\n" + script, True


def inject_page_execution_runtimes(html_text: str, page: Dict[str, Any] | None = None) -> Tuple[str, bool]:
    runtime_page = _runtime_page_contract(page or {})
    text = str(html_text or "")
    changed = False
    for injector in (
        _inject_actionability_inspector_runtime,
        _inject_input_focus_state_runtime,
        _inject_route_execution_runtime,
        _inject_state_display_runtime,
        _inject_query_state_runtime,
        _inject_record_form_hydration_runtime,
        _inject_overlay_hit_test_runtime,
        _inject_execution_obligation_action_surface_runtime,
        _inject_execution_obligation_geometry_runtime,
    ):
        text, did_change = injector(text, runtime_page)
        changed = changed or did_change
    return text, changed


def _html_excerpt(value: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _validation_control_label_key(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\s:：,，/|]+", "", text)
    text = re.sub(r"\d+(?:\.\d+)?", "", text)
    return text.strip().lower()


def _html_attrs_has_class(attrs: str, class_name: str) -> bool:
    match = re.search(r'\bclass\s*=\s*(["\'])(?P<value>.*?)\1', str(attrs or ""), re.IGNORECASE | re.DOTALL)
    if not match:
        return False
    tokens = re.split(r"\s+", html.unescape(match.group("value")).strip())
    return str(class_name or "") in tokens


def _html_attr_value(attrs: str, name: str) -> str:
    match = re.search(
        rf'\b{re.escape(name)}\s*=\s*(["\'])(?P<value>.*?)\1',
        str(attrs or ""),
        re.IGNORECASE | re.DOTALL,
    )
    return str(match.group("value") or "") if match else ""


def _hidden_container_has_runtime_toggle(html_text: str, attrs: str) -> bool:
    element_id = _html_attr_value(attrs, "id")
    if not element_id:
        return False
    script_text = _script_html_for_text_matching(html_text)
    if not re.search(rf"getElementById\(\s*['\"]{re.escape(element_id)}['\"]\s*\)", script_text):
        return False
    return bool(
        re.search(r"\.classList\.(?:toggle|remove|add)\(\s*['\"]hidden['\"]", script_text)
        or re.search(r"\.style\.display\s*=", script_text)
    )


def _visible_control_label_keys_from_page(page: Optional[Dict[str, Any]]) -> set[str]:
    labels: set[str] = set()
    if not isinstance(page, dict):
        return labels
    obligations = page.get("l0_obligations", [])
    for obligation in obligations if isinstance(obligations, list) else []:
        if not isinstance(obligation, dict):
            continue
        if obligation.get("obligation_type") != "visible_control":
            continue
        values = obligation.get("required_text", [])
        for value in values if isinstance(values, list) else []:
            key = _validation_control_label_key(str(value or ""))
            if key:
                labels.add(key)
    return labels


def detect_page_execution_html_artifacts(
    html_text: str,
    page: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    text = str(html_text or "")
    artifacts: List[Dict[str, str]] = []

    def add(code: str, evidence: str, severity: str = "stale") -> None:
        artifacts.append(
            {
                "code": code,
                "severity": severity,
                "evidence": _html_excerpt(evidence),
            }
        )

    for match in re.finditer(
        r'<section\b[^>]*data-page-execution-contract=["\']appendable-feed["\'][^>]*>',
        text,
        re.IGNORECASE,
    ):
        add("legacy_appendable_feed_section", match.group(0))
    for match in re.finditer(
        r'<(?:div|a)\b[^>]*data-page-execution-repair-item=["\']appendable-feed["\'][^>]*>',
        text,
        re.IGNORECASE,
    ):
        add("legacy_appendable_feed_item", match.group(0))

    hidden_container_re = re.compile(
        r'<(?P<tag>div|section|span)\b(?P<attrs>[^>]*(?:\bsr-only\b|aria-hidden=["\']true["\']|display\s*:\s*none)[^>]*)>'
        r'(?P<body>.*?)</(?P=tag)>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in hidden_container_re.finditer(text):
        body = match.group("body") or ""
        if "data-element-id" not in body or "data-ghostweb-click-contract-kind" not in body:
            continue
        if re.search(r'data-ghostweb-click-contract-kind=["\']task["\']', body, re.IGNORECASE):
            add("hidden_task_surface", match.group(0), severity="contract_violation")

    observed_control_keys = _visible_control_label_keys_from_page(page)
    if observed_control_keys:
        hidden_option_group_re = re.compile(
            r'<div\b(?P<attrs>[^>]*)>'
            r'(?P<body>.*?)</div>',
            re.IGNORECASE | re.DOTALL,
        )
        for match in hidden_option_group_re.finditer(text):
            attrs = match.group("attrs") or ""
            body = match.group("body") or ""
            if not _html_attrs_has_class(attrs, "hidden"):
                continue
            if "<button" not in body.lower():
                continue
            labels = [
                _validation_control_label_key(re.sub(r"<[^>]+>", " ", button_body))
                for button_body in re.findall(r"<button\b[^>]*>(.*?)</button>", body, flags=re.IGNORECASE | re.DOTALL)
            ]
            labels = [label for label in labels if label]
            if not labels:
                continue
            option_group_hint = bool(
                re.search(r"\bdata-(?!ghostweb)[\w-]+\s*=", body, re.IGNORECASE)
                or re.search(r"\b(?:dropdown|popover|option|select|panel)\b", attrs, re.IGNORECASE)
            )
            if option_group_hint and not any(label in observed_control_keys for label in labels):
                if _hidden_container_has_runtime_toggle(text, attrs):
                    continue
                add("unobserved_hidden_option_group", match.group(0), severity="contract_violation")

    for label, target_filename in _shell_component_route_action_labels(page):
        if not label:
            continue
        if _html_has_clickable_label_target(text, label, target_filename):
            continue
        alternate_targets = _route_action_targets_for_label(page, label)
        if any(_html_has_clickable_label_target(text, label, target) for target in alternate_targets):
            continue
        noninteractive = _html_noninteractive_label_host(text, label)
        if noninteractive:
            add("shell_affordance_noninteractive", noninteractive, severity="contract_violation")
        elif _normalize_validation_text(label) in _visible_text_for_text_matching(text):
            add("shell_affordance_missing_clickable_host", label, severity="contract_violation")

    return artifacts


def _shell_component_route_action_labels(page: Optional[Dict[str, Any]]) -> List[Tuple[str, str]]:
    if not isinstance(page, dict):
        return []
    route_contract = page.get("route_execution_contract", {})
    if not isinstance(route_contract, dict):
        return []
    actions: List[Dict[str, Any]] = []
    for key in ("actions", "navigation_actions"):
        values = route_contract.get(key, [])
        if isinstance(values, list):
            actions.extend(item for item in values if isinstance(item, dict))
    result: List[Tuple[str, str]] = []
    seen = set()
    for action in actions:
        target = _filename_without_query(str(action.get("target_filename") or ""))
        if not target:
            continue
        binding = action.get("binding", {}) if isinstance(action.get("binding"), dict) else {}
        is_shell = (
            str(action.get("target_source") or "").strip() == "shell_component_affordance"
            or str(action.get("affordance_kind") or "").strip() == "shell_component_navigation_affordance"
            or str(binding.get("source_scope") or "").strip() == "shell_family_component"
        )
        if not is_shell:
            continue
        labels = binding.get("labels", []) if isinstance(binding.get("labels"), list) else []
        for label in labels:
            clean = str(label or "").strip()
            key = (clean, target)
            if clean and key not in seen:
                result.append(key)
                seen.add(key)
    return result


def _route_action_targets_for_label(page: Optional[Dict[str, Any]], label: str) -> List[str]:
    if not isinstance(page, dict):
        return []
    label_key = _normalize_validation_text(label)
    if not label_key:
        return []
    route_contract = page.get("route_execution_contract", {})
    if not isinstance(route_contract, dict):
        return []
    actions: List[Dict[str, Any]] = []
    for key in ("actions", "navigation_actions"):
        values = route_contract.get(key, [])
        if isinstance(values, list):
            actions.extend(item for item in values if isinstance(item, dict))
    targets: List[str] = []
    seen = set()
    for action in actions:
        target = _filename_without_query(str(action.get("target_filename") or ""))
        if not target or target in seen:
            continue
        binding = action.get("binding", {}) if isinstance(action.get("binding"), dict) else {}
        labels = binding.get("labels", []) if isinstance(binding.get("labels"), list) else []
        if any(_normalize_validation_text(item) == label_key for item in labels):
            targets.append(target)
            seen.add(target)
    return targets


def _html_plain_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))


def _html_has_clickable_label_target(html_text: str, label: str, target_filename: str) -> bool:
    label_key = _normalize_validation_text(label)
    target_key = _filename_without_query(target_filename)
    if not label_key or not target_key:
        return False
    clickable_re = re.compile(
        r'<(?P<tag>a|button)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in clickable_re.finditer(html_text):
        attrs = match.group("attrs") or ""
        body = match.group("body") or ""
        haystack = _normalize_validation_text(_html_plain_text(attrs + " " + body))
        if label_key not in haystack:
            continue
        if re.search(r'data-ghostweb-click-contract-kind=["\']noninteractive["\']', attrs, re.IGNORECASE):
            continue
        href_match = re.search(r'href=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        target_match = re.search(r'data-ghostweb-target=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        route_action = re.search(r'data-ghostweb-route-action=["\'][^"\']+["\']', attrs, re.IGNORECASE)
        declared_target = _filename_without_query(
            (href_match.group(1) if href_match else "")
            or (target_match.group(1) if target_match else "")
        )
        if declared_target == target_key or (route_action and not declared_target):
            return True
    return False


def _html_noninteractive_label_host(html_text: str, label: str) -> str:
    label_key = _normalize_validation_text(label)
    if not label_key:
        return ""
    noninteractive_re = re.compile(
        r'<(?P<tag>[a-zA-Z][\w:-]*)\b'
        r'(?P<attrs>[^>]*)data-ghostweb-click-contract-kind=["\']noninteractive["\'][^>]*>'
        r'(?P<body>.*?)</(?P=tag)>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in noninteractive_re.finditer(html_text):
        haystack = _normalize_validation_text(
            _html_plain_text((match.group("attrs") or "") + " " + (match.group("body") or ""))
        )
        if label_key in haystack:
            return match.group(0)
    return ""


def _page_environment_specs_by_filename(payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        return {}
    nested = payload.get("page_environment_specs_by_filename")
    if isinstance(nested, dict):
        return {str(key): value for key, value in nested.items() if isinstance(value, dict)}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _normalized_step_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    trace_id, step_index = _step_key_trace_and_index(text)
    if trace_id != text or re.match(r"^.+:step\d+$", text):
        return f"{trace_id}:step{step_index}"
    match = re.match(r"^(?P<trace>.+):(?P<step>\d+)$", text)
    if match:
        return f"{match.group('trace')}:step{int(match.group('step'))}"
    return text


def _page_environment_authoritative_step_keys(page_environment: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"step_key", "transition_id"}:
                    normalized = _normalized_step_key(value)
                    if normalized:
                        keys.add(normalized)
                    continue
                if key in {"source_step_keys", "task_evidence_steps"} and isinstance(value, list):
                    for item in value:
                        normalized = _normalized_step_key(item)
                        if normalized:
                            keys.add(normalized)
                    continue
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(page_environment)
    return keys


def _defer_legacy_visual_text_to_page_environment(
    obligation: Dict[str, Any],
    page_environment: Dict[str, Any],
) -> bool:
    if not isinstance(page_environment, dict) or not page_environment:
        return False
    if str(obligation.get("visible_text_source") or "") != "visual_observed":
        return False
    if str(obligation.get("obligation_type") or "") not in {"visible_control", "visible_item"}:
        return False
    source_steps = {
        _normalized_step_key(step)
        for key in ("source_evidence_steps", "source_steps", "evidence_steps")
        for step in (obligation.get(key) or [])
    }
    source_steps.discard("")
    if source_steps:
        authoritative_steps = _page_environment_authoritative_step_keys(page_environment)
        return bool(authoritative_steps) and source_steps.isdisjoint(authoritative_steps)
    return True


def validate_page_execution_contracts(
    contracts: Dict[str, Any],
    html_by_filename: Dict[str, str],
    page_environment_specs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    page_environment_by_filename = _page_environment_specs_by_filename(page_environment_specs)
    pages = [
        page for page in (contracts.get("pages", []) if isinstance(contracts, dict) else [])
        if isinstance(page, dict)
    ]
    materialized_filenames = {
        _filename_without_query(str(page.get("filename") or ""))
        for page in pages
        if _filename_without_query(str(page.get("filename") or ""))
    }
    route_level_by_filename: Dict[str, int] = {}
    route_meta_by_filename: Dict[str, Dict[str, Any]] = {}
    fallback_target_by_filename: Dict[str, str] = {}
    for page in pages:
        filename = _filename_without_query(str(page.get("filename") or ""))
        if not filename:
            continue
        route_contract = page.get("route_execution_contract", {}) if isinstance(page.get("route_execution_contract", {}), dict) else {}
        route_context = page.get("route_context", {}) if isinstance(page.get("route_context", {}), dict) else {}
        route_path = str(route_contract.get("route_path") or route_context.get("route_path") or "")
        route_level_by_filename[filename] = _route_level(
            route_path,
            route_contract.get("route_level", route_context.get("route_level", 0)),
        )
        route_meta_by_filename[filename] = {
            "filename": filename,
            "page_name": str(page.get("page_name") or ""),
            "page_type": str(page.get("page_type") or ""),
            "general_page_id": str(page.get("general_page_id") or ""),
            "materialized_page_id": str(page.get("materialized_page_id") or ""),
            "route_path": route_path,
            "route_level": route_level_by_filename[filename],
        }
        fallback_back = route_contract.get("fallback_back_action") or route_contract.get("default_back_action") or {}
        if isinstance(fallback_back, dict):
            target = _filename_without_query(str(fallback_back.get("target_filename") or ""))
            if target:
                fallback_target_by_filename[filename] = target

    cycle_nodes: set[str] = set()
    for start in fallback_target_by_filename:
        seen: Dict[str, int] = {}
        path: List[str] = []
        current = start
        while current in fallback_target_by_filename:
            if current in seen:
                cycle_nodes.update(path[seen[current]:])
                break
            seen[current] = len(path)
            path.append(current)
            current = fallback_target_by_filename[current]

    pages_report: Dict[str, Dict[str, Any]] = {}
    ok = True
    for page in pages:
        if not isinstance(page, dict):
            continue
        filename = str(page.get("filename") or "")
        filename_key = _filename_without_query(filename)
        html = html_by_filename.get(filename, "")
        visible_html = _visible_html_for_text_matching(html)
        visible_text = _visible_text_for_text_matching(html)
        page_missing: List[str] = []
        missing_access_targets: List[str] = []
        navigation_policy_errors: List[str] = []
        html_artifacts = detect_page_execution_html_artifacts(html, page)
        page_environment = page_environment_by_filename.get(filename_key) or page_environment_by_filename.get(filename)
        deferred_required_text: List[Dict[str, Any]] = []
        navigation_policy = page.get("navigation_policy", {})
        if isinstance(navigation_policy, dict):
            stack_policy = navigation_policy.get("stack_navigation_policy", {})
            if isinstance(stack_policy, dict) and stack_policy:
                if stack_policy.get("monotonic") is not True:
                    navigation_policy_errors.append("stack_navigation_policy fallback is not monotonic")
                fallback_target = str(stack_policy.get("fallback_target_filename") or "").strip()
                if not fallback_target:
                    navigation_policy_errors.append("stack_navigation_policy missing fallback target")
                elif stack_policy.get("monotonic") is True:
                    source_meta = route_meta_by_filename.get(filename_key, {})
                    target_meta = route_meta_by_filename.get(_filename_without_query(fallback_target), {})
                    computed_back_proof = _back_proof_for_route_fallback(source_meta, target_meta)
                    declared_back_proof = stack_policy.get("back_proof")
                    if not computed_back_proof:
                        navigation_policy_errors.append("stack_navigation_policy lacks monotonic proof")
                    elif not isinstance(declared_back_proof, dict) or not declared_back_proof:
                        navigation_policy_errors.append("stack_navigation_policy lacks monotonic proof")
        route_contract = page.get("route_execution_contract", {}) if isinstance(page.get("route_execution_contract", {}), dict) else {}
        fallback_back = route_contract.get("fallback_back_action") or route_contract.get("default_back_action") or {}
        if isinstance(fallback_back, dict):
            fallback_target = _filename_without_query(str(fallback_back.get("target_filename") or ""))
            if fallback_target:
                if fallback_target == filename_key:
                    navigation_policy_errors.append("fallback_back_action targets current page")
                if fallback_target not in materialized_filenames:
                    navigation_policy_errors.append(f"fallback_back_action target {fallback_target} is not materialized")
                if filename_key in cycle_nodes:
                    navigation_policy_errors.append("fallback_back_action participates in fallback cycle")
                target_source = str(fallback_back.get("target_source") or "")
                if target_source in {"route_hierarchy", "inbound_navigation", "navigation_policy"}:
                    source_meta = route_meta_by_filename.get(filename_key, {})
                    target_meta = route_meta_by_filename.get(fallback_target, {})
                    computed_back_proof = _back_proof_for_route_fallback(source_meta, target_meta)
                    declared_back_proof = fallback_back.get("back_proof")
                    if not computed_back_proof:
                        if target_source == "route_hierarchy":
                            navigation_policy_errors.append("route_hierarchy fallback is not route-monotonic")
                        else:
                            navigation_policy_errors.append("fallback_back_action lacks monotonic proof")
                    elif not isinstance(declared_back_proof, dict) or not declared_back_proof:
                        navigation_policy_errors.append("fallback_back_action lacks monotonic proof")
                    elif (
                        str(declared_back_proof.get("source_route_path") or "") != str(computed_back_proof.get("source_route_path") or "")
                        or str(declared_back_proof.get("target_route_path") or "") != str(computed_back_proof.get("target_route_path") or "")
                    ):
                        navigation_policy_errors.append("fallback_back_action monotonic proof mismatch")
        for obligation in page.get("l0_obligations", []) if isinstance(page.get("l0_obligations", []), list) else []:
            if not isinstance(obligation, dict):
                continue
            if obligation.get("validation_required") is False:
                continue
            if obligation.get("obligation_type") == "reachable_materialized_page":
                continue
            for required in obligation.get("required_text", []) if isinstance(obligation.get("required_text", []), list) else []:
                text = str(required or "").strip()
                normalized_text = _normalize_validation_text(text)
                if (
                    text
                    and text not in visible_html
                    and (not normalized_text or normalized_text not in visible_text)
                    and not (
                        obligation.get("layout_policy") == "appendable_feed"
                        and obligation.get("obligation_type") == "visible_item"
                        and _has_dynamic_appendable_feed_text(html, text)
                    )
                ):
                    if _defer_legacy_visual_text_to_page_environment(obligation, page_environment):
                        deferred_required_text.append({
                            "text": text,
                            "obligation_id": str(obligation.get("obligation_id") or ""),
                            "reason": "page_environment_spec_authoritative_legacy_visual_text",
                        })
                        continue
                    page_missing.append(text)
        page_missing = _dedupe(page_missing, limit=128)
        missing_access_targets = _dedupe(missing_access_targets, limit=128)
        navigation_policy_errors = _dedupe(navigation_policy_errors, limit=128)
        if page_missing or missing_access_targets or navigation_policy_errors or html_artifacts:
            ok = False
        pages_report[filename] = {
            "ok": not page_missing and not missing_access_targets and not navigation_policy_errors and not html_artifacts,
            "missing_required_text": page_missing,
            "missing_access_target": missing_access_targets,
            "navigation_policy_errors": navigation_policy_errors,
            "html_artifacts": html_artifacts,
            "deferred_required_text": deferred_required_text,
        }
    return {
        "format": "page_execution_contract_validation",
        "ok": ok,
        "pages": pages_report,
    }


def compact_page_execution_contract(contract: Dict[str, Any], text_limit: int = 48) -> Dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    prompt_provenance_fields = {
        "source_evidence_steps",
        "source_steps",
        "evidence_steps",
        "obs_keys",
        "source_obs_keys",
        "reference_trace_ids",
    }
    compact = {
        "filename": contract.get("filename", ""),
        "page_id": contract.get("page_id", ""),
        "page_name": contract.get("page_name", ""),
        "general_page_id": contract.get("general_page_id", ""),
        "materialized_page_id": contract.get("materialized_page_id", ""),
        "content_selection_policy": contract.get("content_selection_policy", {}),
        "navigation_policy": contract.get("navigation_policy", {}),
        "route_execution_contract": _compact_route_contract_for_prompt(contract.get("route_execution_contract", {})),
        "query_state_contract": contract.get("query_state_contract", {}),
        "input_surface_contract": contract.get("input_surface_contract", {}),
        "dynamic_surface_contract": _compact_dynamic_surface_contract_for_runtime(contract.get("dynamic_surface_contract", {})),
        "l0_obligations": [],
    }
    for obligation in contract.get("l0_obligations", []) if isinstance(contract.get("l0_obligations", []), list) else []:
        if not isinstance(obligation, dict):
            continue
        if obligation.get("obligation_type") == "reachable_materialized_page":
            continue
        item = {
            key: value
            for key, value in obligation.items()
            if key not in prompt_provenance_fields
        }
        item["required_text"] = _dedupe(item.get("required_text", []), limit=text_limit)
        compact["l0_obligations"].append(item)
    return compact
