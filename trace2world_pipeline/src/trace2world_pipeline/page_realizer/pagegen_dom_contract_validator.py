
from __future__ import annotations

import os

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


_SCRIPT_OR_STYLE_BLOCK_RE = re.compile(r"<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)>", re.IGNORECASE)
_DATA_ELEMENT_ID_RE = re.compile(r'data-element-id\s*=\s*"([^"]+)"')
_TAG_WITH_DATA_ELEMENT_RE = re.compile(
    r"<(?P<tag>[A-Za-z][\w:-]*)\b(?P<attrs>[^<>]*?\b(?:data-element-id|data-element-aliases)\s*=[^<>]*)>",
    re.IGNORECASE,
)
_SEAT_MAP_ZONE_PAIR_RE = re.compile(
    r"<(?P<tag>div|section|article|button)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_NONINTERACTIVE_ATTR_RE = re.compile(
    r'data-ghostweb-click-contract-kind\s*=\s*["\']noninteractive["\']',
    re.IGNORECASE,
)
_REMOTE_SCRIPT_OR_LINK_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*['\"]https?://[^>]*>\s*</script>|"
    r"<link\b[^>]*\bhref\s*=\s*['\"]https?://[^>]*>",
    re.IGNORECASE,
)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_CLASS_ATTR_RE = re.compile(r"\bclass\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)

_TAILWIND_SPACING_PX: Dict[str, float] = {
    "0": 0,
    "0.5": 2,
    "1": 4,
    "1.5": 6,
    "2": 8,
    "2.5": 10,
    "3": 12,
    "3.5": 14,
    "4": 16,
    "5": 20,
    "6": 24,
    "7": 28,
    "8": 32,
    "9": 36,
    "10": 40,
    "11": 44,
    "12": 48,
    "14": 56,
    "16": 64,
    "20": 80,
    "24": 96,
    "28": 112,
    "32": 128,
    "36": 144,
    "40": 160,
    "44": 176,
    "48": 192,
    "52": 208,
    "56": 224,
    "60": 240,
    "64": 256,
    "72": 288,
    "80": 320,
    "96": 384,
}

_SEAT_MAP_ZONE_CLASS_TOKENS = {
    "seat-block",
    "seat-zone",
    "seat-area",
    "seat-section",
    "seat-cluster",
    "seat-price-zone",
}

_SEAT_COLOR_PATTERNS = (
    ("pink", re.compile(r"\b(?:bg-|text-|border-)?pink(?:-\d+)?\b|#(?:e050c4|ec4899|db2777|f472b6|c026a8)", re.IGNORECASE)),
    ("blue", re.compile(r"\b(?:bg-|text-|border-)?blue(?:-\d+)?\b|#(?:9aa7ff|3b82f6|2563eb|60a5fa)", re.IGNORECASE)),
    ("green", re.compile(r"\b(?:bg-|text-|border-)?green(?:-\d+)?\b|#(?:b8e0cd|22c55e|16a34a|4ade80)", re.IGNORECASE)),
    ("yellow", re.compile(r"\b(?:bg-|text-|border-)?yellow(?:-\d+)?\b|#(?:f8d77b|eab308|facc15|fde047)", re.IGNORECASE)),
    ("orange", re.compile(r"\b(?:bg-|text-|border-)?orange(?:-\d+)?\b|#(?:fb923c|f97316|ff8f3d)", re.IGNORECASE)),
    ("red", re.compile(r"\b(?:bg-|text-|border-)?red(?:-\d+)?\b|#(?:ef4444|dc2626|f87171)", re.IGNORECASE)),
    ("purple", re.compile(r"\b(?:bg-|text-|border-)?purple(?:-\d+)?\b|#(?:a855f7|9333ea|c084fc)", re.IGNORECASE)),
)



def _chromium_executable() -> Optional[str]:
    """Optional Chromium override; unset means the Playwright-managed browser."""
    override = os.environ.get("T2W_CHROMIUM_EXECUTABLE", "").strip()
    return override if override and Path(override).is_file() else None

def _base_filename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    return Path(text).name


class DomContractViolation(RuntimeError):

    def __init__(self, audit: Dict[str, Any]):
        self.audit = audit
        summary = audit.get("summary", {})
        super().__init__(
            "pagegen DOM contract violation: "
            f"missing={summary.get('missing_total', 0)}, "
            f"ambiguous={summary.get('ambiguous_total', 0)}, "
            f"noninteractive={summary.get('noninteractive_total', 0)}, "
            f"action_binding={summary.get('action_binding_mismatch_total', 0)}, "
            f"hidden_or_tiny={summary.get('hidden_or_tiny_total', 0)}, "
            f"seat_map_visual={summary.get('seat_map_visual_issue_total', 0)}, "
            f"affected_pages={summary.get('failing_pages', 0)}"
        )


def _iter_data_element_ids(html: str) -> List[str]:
    return _DATA_ELEMENT_ID_RE.findall(_static_dom_scan_html(html))


def _static_dom_scan_html(html: str) -> str:
    return _SCRIPT_OR_STYLE_BLOCK_RE.sub("", html or "")


def _attr_value(attrs: str, name: str) -> str:
    pattern = rf"\b{name}\s*=\s*(['\"])(.*?)\1"
    match = re.search(pattern, attrs or "", re.IGNORECASE | re.DOTALL)
    return match.group(2) if match else ""


def _tag_records_by_data_element_id(html: str) -> Dict[str, List[Dict[str, Any]]]:
    records: Dict[str, List[Dict[str, Any]]] = {}
    scan_html = _static_dom_scan_html(html)
    for match in _TAG_WITH_DATA_ELEMENT_RE.finditer(scan_html):
        attrs = match.group("attrs") or ""
        data_id = _attr_value(attrs, "data-element-id")
        start = match.start()
        prefix = scan_html[:start]
        last_noninteractive_open = -1
        for ancestor_match in re.finditer(
            r"<[A-Za-z][\w:-]*\b[^<>]*data-ghostweb-click-contract-kind\s*=\s*['\"]noninteractive['\"][^<>]*>",
            prefix,
            re.IGNORECASE,
        ):
            last_noninteractive_open = ancestor_match.start()
        last_close = prefix.rfind("</")
        has_noninteractive_ancestor = last_noninteractive_open > last_close
        record = {
            "tag": (match.group("tag") or "").lower(),
            "attrs": attrs[:1000],
            "class": _attr_value(attrs, "class"),
            "style": _attr_value(attrs, "style"),
            "role": _attr_value(attrs, "role"),
            "href": _attr_value(attrs, "href"),
            "tabindex": _attr_value(attrs, "tabindex"),
            "data_action_id": _attr_value(attrs, "data-action-id"),
            "data_action_aliases": _attr_value(attrs, "data-action-aliases"),
            "noninteractive_self": bool(_NONINTERACTIVE_ATTR_RE.search(attrs)),
            "noninteractive_ancestor": has_noninteractive_ancestor,
            "start": match.start(),
        }
        if data_id:
            records.setdefault(data_id, []).append(dict(record, match_kind="primary"))
        aliases = _attr_value(attrs, "data-element-aliases")
        for alias in str(aliases or "").split():
            alias = alias.strip()
            if alias and alias != data_id:
                records.setdefault(alias, []).append(dict(record, match_kind="alias"))
    return records


def _looks_hidden_or_tiny(record: Dict[str, Any]) -> str:
    class_tokens = set(str(record.get("class") or "").split())
    classes = f" {' '.join(class_tokens)} "
    style = str(record.get("style") or "").lower().replace(" ", "")
    attrs = str(record.get("attrs") or "").lower()
    if {"hidden", "sr-only", "invisible"} & class_tokens:
        return "hidden_class"
    if "opacity-0" in class_tokens or "opacity:0" in style or "opacity:0.0" in style:
        return "transparent"
    if "pointer-events-none" in class_tokens or "pointer-events:none" in style:
        return "pointer_events_none"
    if "display:none" in style or "visibility:hidden" in style:
        return "hidden_style"
    if {"w-0", "h-0"} & class_tokens or re.search(r"(?:width|height):(?:0|1|2|3)px", style):
        return "tiny_surface"
    if re.search(r"\b(?:opacity|aria-hidden)\s*=\s*['\"](?:0|true)['\"]", attrs):
        return "hidden_attr"
    return ""


def _looks_interactive(record: Dict[str, Any]) -> bool:
    if record.get("noninteractive_self") or record.get("noninteractive_ancestor"):
        return False
    tag = str(record.get("tag") or "").lower()
    attrs = str(record.get("attrs") or "").lower()
    role = str(record.get("role") or "").lower()
    if tag in {"a", "button", "input", "select", "textarea", "summary"}:
        if tag == "a" and not record.get("href") and "onclick" not in attrs and "data-ghostweb" not in attrs:
            return False
        return True
    if role in {"button", "link", "textbox", "checkbox", "radio", "switch", "menuitem", "tab", "combobox"}:
        return True
    if record.get("tabindex") not in ("", None):
        return True
    return any(
        token in attrs
        for token in (
            "onclick",
            "data-ghostweb-action",
            "data-ghostweb-target",
            "data-route",
            "data-target",
            "href=",
        )
    )


def _is_seat_map_page(filename: str, html: str) -> bool:
    haystack = f"{filename}\n{_static_dom_scan_html(html)[:6000]}".lower()
    return any(token in haystack for token in ("seat-map", "seat map", "seatmap", "座位图", "可选座", "选座"))


def _seat_map_color_buckets(fragment: str) -> Set[str]:
    buckets: Set[str] = set()
    text = str(fragment or "")
    for bucket, pattern in _SEAT_COLOR_PATTERNS:
        if pattern.search(text):
            buckets.add(bucket)
    return buckets


def _seat_map_zone_visual_issues(filename: str, html: str) -> List[Dict[str, Any]]:
    if not _is_seat_map_page(filename, html):
        return []
    scan_html = _static_dom_scan_html(html)
    issues: List[Dict[str, Any]] = []
    for match in _SEAT_MAP_ZONE_PAIR_RE.finditer(scan_html):
        attrs = match.group("attrs") or ""
        class_tokens = set(str(_attr_value(attrs, "class") or "").split())
        if not class_tokens.intersection(_SEAT_MAP_ZONE_CLASS_TOKENS):
            continue
        body = match.group("body") or ""
        color_buckets = _seat_map_color_buckets(body)
        if "mixed" in class_tokens:
            color_buckets.add("mixed")
        explicit_color_tokens = {
            token
            for token in class_tokens
            if token in {bucket for bucket, _pattern in _SEAT_COLOR_PATTERNS}
        }
        color_buckets.update(explicit_color_tokens)
        if len(color_buckets) <= 1:
            continue
        issues.append(
            {
                "reason": "mixed_zone_colors",
                "tag": (match.group("tag") or "").lower(),
                "class": " ".join(sorted(class_tokens)),
                "colors": sorted(color_buckets),
                "expected": (
                    "Each seat-map section must use one non-neutral price/status color; "
                    "split differently colored seats into separate visible sub-zones."
                ),
            }
        )
    return issues


def _px_values_for_props(style_text: str, props: Set[str]) -> List[float]:
    values: List[float] = []
    for prop in props:
        for match in re.finditer(
            rf"(?:^|[;{{])\s*{re.escape(prop)}\s*:\s*([0-9]+(?:\.[0-9]+)?)px\b",
            style_text or "",
            re.IGNORECASE,
        ):
            try:
                values.append(float(match.group(1)))
            except ValueError:
                continue
    return values


def _tailwind_dimension_values(class_tokens: Set[str], axis: str) -> List[float]:
    values: List[float] = []
    prefixes = ("w-", "min-w-") if axis == "width" else ("h-", "min-h-")
    for token in class_tokens:
        for prefix in prefixes:
            if not token.startswith(prefix):
                continue
            suffix = token[len(prefix):]
            if suffix in _TAILWIND_SPACING_PX:
                values.append(float(_TAILWIND_SPACING_PX[suffix]))
                continue
            arbitrary = re.fullmatch(r"\[([0-9]+(?:\.[0-9]+)?)px\]", suffix)
            if arbitrary:
                values.append(float(arbitrary.group(1)))
    return values


def _css_rule_text_for_classes(html: str, class_tokens: Set[str]) -> str:
    chunks: List[str] = []
    for token in class_tokens:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token or ""):
            continue
        pattern = re.compile(rf"\.{re.escape(token)}(?:\s|[:.#,{{])[^{{]*{{(?P<body>[^}}]*)}}", re.IGNORECASE)
        for match in pattern.finditer(html or ""):
            chunks.append(match.group("body") or "")
    return ";".join(chunks)


def _seat_map_host_size_px(record: Dict[str, Any], html: str) -> Dict[str, Optional[float]]:
    class_tokens = set(str(record.get("class") or "").split())
    style_text = str(record.get("style") or "")
    css_text = _css_rule_text_for_classes(html, class_tokens)
    width_values = []
    height_values = []
    width_values.extend(_px_values_for_props(style_text, {"width", "min-width"}))
    height_values.extend(_px_values_for_props(style_text, {"height", "min-height"}))
    width_values.extend(_px_values_for_props(css_text, {"width", "min-width"}))
    height_values.extend(_px_values_for_props(css_text, {"height", "min-height"}))
    width_values.extend(_tailwind_dimension_values(class_tokens, "width"))
    height_values.extend(_tailwind_dimension_values(class_tokens, "height"))
    return {
        "width": max(width_values) if width_values else None,
        "height": max(height_values) if height_values else None,
    }


def _seat_map_host_too_small(
    filename: str,
    component_id: str,
    record: Dict[str, Any],
    html: str,
) -> Optional[Dict[str, Any]]:
    if not _is_seat_map_page(filename, html) and "seat-map" not in str(component_id).lower():
        return None
    size = _seat_map_host_size_px(record, html)
    width = size.get("width")
    height = size.get("height")
    if width is None or height is None:
        return None
    if width >= 32 and height >= 32:
        return None
    return {
        "reason": "seat_map_hit_target_too_small",
        "measured_width_px": width,
        "measured_height_px": height,
        "minimum_width_px": 32,
        "minimum_height_px": 32,
    }


def _route_target_matches(value: str, target_filename: str) -> bool:
    return bool(value) and _base_filename(value) == _base_filename(target_filename)


def _record_has_ghostweb_route_executor(
    record: Dict[str, Any],
    target_filename: str,
) -> bool:
    attrs = str(record.get("attrs") or "")
    target = re.escape(str(target_filename or "").strip())
    if not target:
        return False
    return bool(
        re.search(
            rf"GhostwebRouteExecution\.navigate\s*\(\s*['\"]{target}(?:[?#][^'\"]*)?['\"]",
            attrs,
            re.IGNORECASE | re.DOTALL,
        )
    )


def _record_has_data_route_executor(
    record: Dict[str, Any],
    target_filename: str,
) -> bool:
    attrs = str(record.get("attrs") or "")
    data_target = _attr_value(attrs, "data-ghostweb-target")
    href = _attr_value(attrs, "href")
    data_action = _attr_value(attrs, "data-ghostweb-action")
    return (
        data_action == "navigate"
        and _route_target_matches(data_target, target_filename)
    ) or _route_target_matches(href, target_filename)


def _record_has_sdk_route_side_effect(
    record: Dict[str, Any],
    target_filename: str,
) -> bool:
    attrs = str(record.get("attrs") or "")
    target = re.escape(str(target_filename or "").strip())
    if not target:
        return False
    return bool(
        re.search(
            rf"WebsiteSDK\.navigateRoute\s*\(\s*['\"]{target}(?:[?#][^'\"]*)?['\"]",
            attrs,
            re.IGNORECASE | re.DOTALL,
        )
        or "WebsiteSDK.navigateRoute" in attrs
    )


def _html_has_route_runtime_target(html: str, target_filename: str) -> bool:
    target = str(target_filename or "").strip()
    if not target or 'data-ghostweb-runtime="route-execution"' not in html:
        return False
    pattern = re.escape(target)
    return bool(
        re.search(
            rf'"target_filename"\s*:\s*"{pattern}"',
            html,
            re.IGNORECASE,
        )
        and "GhostwebRouteExecution.navigate" in html
    )


def _slot_query_text(slot: Dict[str, Any]) -> str:
    query_precondition = slot.get("query_precondition")
    if isinstance(query_precondition, dict):
        query = str(query_precondition.get("input_text") or "").strip()
        if query:
            return query
    state_binding = slot.get("state_binding")
    if isinstance(state_binding, dict) and state_binding.get("kind") == "search_draft":
        query = str(state_binding.get("value") or "").strip()
        if query:
            return query
    return str(slot.get("input_text") or "").strip()


def _html_has_query_route_guard(html: str, query_text: str, target_filename: str) -> bool:
    query = str(query_text or "").strip()
    target = str(target_filename or "").strip()
    if not query or not target:
        return False
    if query not in html or target not in html:
        return False
    target_pattern = re.escape(target)
    return bool(
        re.search(
            rf"GhostwebRouteExecution\.navigate\s*\(\s*['\"]{target_pattern}(?:[?#][^'\"]*)?['\"]",
            html,
            re.IGNORECASE | re.DOTALL,
        )
        and (
            re.search(r"\bq\b|\bquery\b|searchInput|input", html, re.IGNORECASE)
            or "search_draft" in html
        )
    )


def _required_component_ids_for_page(
    page_spec: Dict[str, Any],
) -> List[str]:
    components = page_spec.get("components")
    if not isinstance(components, list):
        return []
    ids: List[str] = []
    for c in components:
        if not isinstance(c, dict):
            continue
        if str(c.get("component_role") or "").lower() == "l0_execution_anchor":
            continue
        if not c.get("interaction_required"):
            continue
        cid = str(c.get("component_id") or "")
        if cid:
            ids.append(cid)
    return ids


def _canonical_action_id(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(.+):(\d+)", text)
    return f"{match.group(1)}:step{match.group(2)}" if match else text


def _expected_action_ids_by_component(page_spec: Dict[str, Any]) -> Dict[str, Set[str]]:
    result: Dict[str, Set[str]] = {}
    components = page_spec.get("components") if isinstance(page_spec, dict) else []
    if not isinstance(components, list):
        return result
    for component in components:
        if not isinstance(component, dict) or not component.get("interaction_required"):
            continue
        component_id = str(component.get("component_id") or "").strip()
        if not component_id:
            continue
        for source_step in component.get("source_step_keys") or []:
            action_id = _canonical_action_id(source_step)
            if action_id:
                result.setdefault(component_id, set()).add(action_id)
    return result


def _validate_one_page(
    filename: str,
    page_spec: Dict[str, Any],
    html_path: Optional[Path],
    required_ids_override: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "filename": filename,
        "html_path": str(html_path) if html_path else "",
        "status": "ok",
        "required_count": 0,
        "present_count": 0,
        "missing": [],
        "ambiguous": [],
        "noninteractive": [],
        "action_binding_mismatches": [],
        "hidden_or_tiny": [],
        "seat_map_visual": [],
        "unknown_in_html": [],
        "html_loaded": False,
    }

    if required_ids_override is not None:
        required_ids = list(required_ids_override)
    else:
        required_ids = _required_component_ids_for_page(page_spec)
    record["required_count"] = len(required_ids)

    if html_path is None or not html_path.is_file():
        record["status"] = "missing_html"
        record["missing"] = list(required_ids)
        return record

    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        record["status"] = "html_read_error"
        record["error"] = repr(exc)
        record["missing"] = list(required_ids)
        return record

    record["html_loaded"] = True
    record["seat_map_visual"] = _seat_map_zone_visual_issues(filename, html)
    tag_records = _tag_records_by_data_element_id(html)
    expected_actions = _expected_action_ids_by_component(page_spec)
    counts: Dict[str, int] = {}
    for fid, records in tag_records.items():
        counts[fid] = len(records)

    required_set: Set[str] = set(required_ids)
    for cid in required_ids:
        records = tag_records.get(cid, [])
        primary_records = [row for row in records if row.get("match_kind") == "primary"]
        selected_records = primary_records or records
        count = len(selected_records)
        if count == 0:
            record["missing"].append(cid)
        elif count > 1:
            record["ambiguous"].append({"component_id": cid, "occurrences": count})
        else:
            tag_record = selected_records[0] if selected_records else {}
            expected_action_ids = expected_actions.get(cid, set())
            actual_action_ids = {
                str(tag_record.get("data_action_id") or "").strip(),
                *str(tag_record.get("data_action_aliases") or "").split(),
            }
            actual_action_ids.discard("")
            if expected_action_ids and not expected_action_ids.intersection(actual_action_ids):
                record["action_binding_mismatches"].append({
                    "component_id": cid,
                    "expected_action_ids": sorted(expected_action_ids),
                    "actual_action_ids": sorted(actual_action_ids),
                    "reason": "data_element_and_action_id_not_colocated",
                    "expected": "The exact data-element-id and action id must be on the same real host.",
                })
            hidden_reason = _looks_hidden_or_tiny(tag_record)
            seat_map_size_issue = _seat_map_host_too_small(filename, cid, tag_record, html)
            if hidden_reason:
                record["hidden_or_tiny"].append({
                    "component_id": cid,
                    "reason": hidden_reason,
                    "tag": tag_record.get("tag", ""),
                    "class": tag_record.get("class", ""),
                    "style": tag_record.get("style", ""),
                })
            elif seat_map_size_issue:
                record["hidden_or_tiny"].append({
                    "component_id": cid,
                    "reason": seat_map_size_issue["reason"],
                    "tag": tag_record.get("tag", ""),
                    "class": tag_record.get("class", ""),
                    "style": tag_record.get("style", ""),
                    "measured_width_px": seat_map_size_issue["measured_width_px"],
                    "measured_height_px": seat_map_size_issue["measured_height_px"],
                    "minimum_width_px": seat_map_size_issue["minimum_width_px"],
                    "minimum_height_px": seat_map_size_issue["minimum_height_px"],
                })
            elif not _looks_interactive(tag_record):
                reason = (
                    "noninteractive_self"
                    if tag_record.get("noninteractive_self")
                    else "noninteractive_ancestor"
                    if tag_record.get("noninteractive_ancestor")
                    else "not_actionable_host"
                )
                record["noninteractive"].append({
                    "component_id": cid,
                    "reason": reason,
                    "tag": tag_record.get("tag", ""),
                    "class": tag_record.get("class", ""),
                    "role": tag_record.get("role", ""),
                    "expected": "data-element-id must be attached to the real visible interactive control, not a static visual wrapper",
                })
            else:
                record["present_count"] += 1

    for fid in counts:
        if fid not in required_set:
            record["unknown_in_html"].append({"data_element_id": fid, "occurrences": counts[fid]})

    if (
        record["missing"]
        or record["ambiguous"]
        or record["noninteractive"]
        or record["action_binding_mismatches"]
        or record["hidden_or_tiny"]
        or record["seat_map_visual"]
    ):
        record["status"] = "violation"
    return record


def validate_pagegen_dom_contracts(
    *,
    page_environment_specs_by_filename: Dict[str, Dict[str, Any]],
    generated_pages_dir: Path,
    required_component_ids_by_filename: Optional[Dict[str, Iterable[str]]] = None,
) -> Dict[str, Any]:
    pages: List[Dict[str, Any]] = []
    pages_dir = Path(generated_pages_dir)
    required_by_filename = required_component_ids_by_filename or {}

    for filename, page_spec in sorted(page_environment_specs_by_filename.items()):
        if not isinstance(page_spec, dict):
            continue
        html_path = pages_dir / filename
        override = required_by_filename.get(filename)
        record = _validate_one_page(filename, page_spec, html_path, override)
        pages.append(record)

    missing_total = sum(len(p["missing"]) for p in pages)
    ambiguous_total = sum(len(p["ambiguous"]) for p in pages)
    noninteractive_total = sum(len(p.get("noninteractive", [])) for p in pages)
    action_binding_mismatch_total = sum(len(p.get("action_binding_mismatches", [])) for p in pages)
    hidden_or_tiny_total = sum(len(p.get("hidden_or_tiny", [])) for p in pages)
    seat_map_visual_issue_total = sum(len(p.get("seat_map_visual", [])) for p in pages)
    failing_pages = sum(1 for p in pages if p["status"] == "violation")
    missing_html_pages = sum(1 for p in pages if p["status"] == "missing_html")
    return {
        "format": "pagegen_dom_contract_audit",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "audits.pagegen_dom_contract",
            "artifact_kind": "validation_report",
            "api_or_det": "det",
            "logical_owner": "pagegen_dom_contract_validator",
            "producer": "frontend.pagegen_dom_contract_validator.validate_pagegen_dom_contracts",
            "canonical_source": False,
            "canonical_prompt_boundary": False,
        },
        "summary": {
            "page_count": len(pages),
            "passing_pages": sum(1 for p in pages if p["status"] == "ok"),
            "failing_pages": failing_pages,
            "missing_html_pages": missing_html_pages,
            "missing_total": missing_total,
            "ambiguous_total": ambiguous_total,
            "noninteractive_total": noninteractive_total,
            "action_binding_mismatch_total": action_binding_mismatch_total,
            "hidden_or_tiny_total": hidden_or_tiny_total,
            "seat_map_visual_issue_total": seat_map_visual_issue_total,
            "required_total": sum(p["required_count"] for p in pages),
            "present_total": sum(p["present_count"] for p in pages),
        },
        "pages": pages,
    }


def _runtime_required_ids_for_page(
    page_spec: Dict[str, Any],
    override: Optional[Iterable[str]],
) -> List[str]:
    if override is not None:
        return list(override)
    return _required_component_ids_for_page(page_spec)


def _runtime_dom_probe(html_path: Path, required_ids: List[str]) -> Dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_runtime_dom_probe_async(html_path, required_ids))
    raise RuntimeError(
        "_runtime_dom_probe() cannot run inside an asyncio loop; "
        "use _runtime_dom_probe_async() instead"
    )


def _is_allowed_probe_remote_url(url: str) -> bool:
    return False


def _css_attr_value(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _css_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value).rstrip("0").rstrip(".")


def _css_px(value: float) -> str:
    return f"{_css_number(value)}px"


def _tailwind_spacing_value(token_value: str) -> str:
    if token_value in _TAILWIND_SPACING_PX:
        return _css_px(_TAILWIND_SPACING_PX[token_value])
    return ""


def _tailwind_arbitrary_value(token_value: str) -> str:
    match = re.fullmatch(r"\[(.+)\]", token_value or "")
    if not match:
        return ""
    value = match.group(1).strip()
    if not value:
        return ""
    return value.replace("_", " ")


def _tailwind_probe_class_tokens(html: str) -> Set[str]:
    tokens: Set[str] = set()
    for match in _CLASS_ATTR_RE.finditer(html or ""):
        for token in re.split(r"\s+", match.group(2) or ""):
            token = token.strip()
            if token:
                tokens.add(token)
    return tokens


def _tailwind_probe_selector(token: str) -> str:
    return f'[class~="{_css_attr_value(token)}"]'


def _tailwind_probe_rule(token: str, declarations: str) -> str:
    return f"{_tailwind_probe_selector(token)}{{{declarations}}}"


def _tailwind_probe_dynamic_rule(token: str) -> str:
    negative = token.startswith("-")
    raw = token[1:] if negative else token
    if raw in {"translate-x-1/2", "translate-y-1/2"}:
        axis = "x" if "translate-x" in raw else "y"
        value = "-50%" if negative else "50%"
        return _tailwind_probe_rule(
            token,
            f"--tw-translate-{axis}:{value};transform:translate(var(--tw-translate-x,0),var(--tw-translate-y,0));",
        )
    if raw.startswith("aspect-") and not negative:
        suffix = raw[len("aspect-"):]
        if suffix == "square":
            value = "1 / 1"
        elif suffix == "video":
            value = "16 / 9"
        else:
            value = _tailwind_arbitrary_value(suffix)
        if value:
            return _tailwind_probe_rule(token, f"aspect-ratio:{value};")
    for prefix, prop in (
        ("top-", "top"),
        ("right-", "right"),
        ("bottom-", "bottom"),
        ("left-", "left"),
        ("z-", "z-index"),
        ("w-", "width"),
        ("h-", "height"),
        ("min-w-", "min-width"),
        ("min-h-", "min-height"),
        ("max-w-", "max-width"),
        ("max-h-", "max-height"),
        ("p-", "padding"),
        ("px-", "padding-left;padding-right"),
        ("py-", "padding-top;padding-bottom"),
        ("pt-", "padding-top"),
        ("pr-", "padding-right"),
        ("pb-", "padding-bottom"),
        ("pl-", "padding-left"),
        ("m-", "margin"),
        ("mx-", "margin-left;margin-right"),
        ("my-", "margin-top;margin-bottom"),
        ("mt-", "margin-top"),
        ("mr-", "margin-right"),
        ("mb-", "margin-bottom"),
        ("ml-", "margin-left"),
        ("gap-", "gap"),
    ):
        if not raw.startswith(prefix):
            continue
        suffix = raw[len(prefix):]
        if suffix == "full":
            value = "100%"
        elif suffix == "screen" and prop in {"width", "height", "min-width", "min-height", "max-width", "max-height"}:
            value = "100vw" if "width" in prop else "100vh"
        elif suffix == "auto" and prop.startswith("margin"):
            value = "auto"
        else:
            value = _tailwind_arbitrary_value(suffix) or _tailwind_spacing_value(suffix)
        if not value:
            continue
        if negative and value not in {"auto"}:
            value = f"-{value}"
        declarations = "".join(f"{name}:{value};" for name in prop.split(";"))
        return _tailwind_probe_rule(token, declarations)
    return ""


def _tailwind_probe_fallback_css(html: str) -> str:
    static_rules = {
        "absolute": "position:absolute;",
        "fixed": "position:fixed;",
        "relative": "position:relative;",
        "sticky": "position:sticky;",
        "block": "display:block;",
        "inline-block": "display:inline-block;",
        "inline": "display:inline;",
        "flex": "display:flex;",
        "inline-flex": "display:inline-flex;",
        "grid": "display:grid;",
        "hidden": "display:none;",
        "flex-col": "flex-direction:column;",
        "flex-row": "flex-direction:row;",
        "items-start": "align-items:flex-start;",
        "items-center": "align-items:center;",
        "items-end": "align-items:flex-end;",
        "items-stretch": "align-items:stretch;",
        "justify-start": "justify-content:flex-start;",
        "justify-center": "justify-content:center;",
        "justify-end": "justify-content:flex-end;",
        "justify-between": "justify-content:space-between;",
        "justify-around": "justify-content:space-around;",
        "justify-evenly": "justify-content:space-evenly;",
        "content-center": "align-content:center;",
        "self-center": "align-self:center;",
        "overflow-hidden": "overflow:hidden;",
        "overflow-auto": "overflow:auto;",
        "overflow-y-auto": "overflow-y:auto;",
        "overflow-x-auto": "overflow-x:auto;",
        "w-full": "width:100%;",
        "h-full": "height:100%;",
        "min-h-screen": "min-height:100vh;",
        "h-screen": "height:100vh;",
        "inset-0": "inset:0;",
        "left-0": "left:0;",
        "right-0": "right:0;",
        "top-0": "top:0;",
        "bottom-0": "bottom:0;",
        "mx-auto": "margin-left:auto;margin-right:auto;",
        "pointer-events-none": "pointer-events:none;",
        "pointer-events-auto": "pointer-events:auto;",
        "box-border": "box-sizing:border-box;",
        "box-content": "box-sizing:content-box;",
    }
    rules = [
        "*,::before,::after{box-sizing:border-box;}",
        "html,body{margin:0;min-height:844px;width:390px;}",
        "button,input,select,textarea{font:inherit;}",
    ]
    tokens = _tailwind_probe_class_tokens(html)
    for token in sorted(tokens):
        declarations = static_rules.get(token)
        if declarations:
            rules.append(_tailwind_probe_rule(token, declarations))
            continue
        dynamic = _tailwind_probe_dynamic_rule(token)
        if dynamic:
            rules.append(dynamic)
    return "<style data-ghostweb-probe-tailwind-fallback>" + "\n".join(rules) + "</style>"


def _prepare_probe_html(html: str, html_path: Path) -> tuple[str, bool]:
    prepared = html or ""
    prepared = _REMOTE_SCRIPT_OR_LINK_RE.sub("", prepared)
    prepared = _SCRIPT_BLOCK_RE.sub("", prepared)
    fallback_css = _tailwind_probe_fallback_css(prepared)

    base_href = html_path.parent.resolve().as_uri() + "/"
    base_tag = f'<base href="{base_href}">' + fallback_css
    if re.search(r"<head\b[^>]*>", prepared, re.IGNORECASE):
        prepared = re.sub(r"(<head\b[^>]*>)", r"\1" + base_tag, prepared, count=1, flags=re.IGNORECASE)
    else:
        prepared = base_tag + prepared
    return prepared, False


async def _route_probe_resource(route: Any) -> None:
    url = str(getattr(getattr(route, "request", None), "url", "") or "")
    if url.startswith(("http://", "https://")) and not _is_allowed_probe_remote_url(url):
        await route.abort()
        return
    await route.continue_()


async def _goto_probe_html_ready(page: Any, html_path: Path) -> None:


    await page.route("**/*", _route_probe_resource)
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    html, _uses_remote_styles = _prepare_probe_html(html, html_path)
    await page.set_content(
        html,
        wait_until="domcontentloaded",
        timeout=15000,
    )


async def _runtime_dom_probe_async(
    html_path: Path,
    required_ids: List[str],
    viewport: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=_chromium_executable())
        page = await browser.new_page(viewport=dict(viewport or {"width": 390, "height": 844}))
        await _goto_probe_html_ready(page, html_path)
        await page.wait_for_timeout(50)
        result = await page.evaluate(
            """
            (requiredIds) => {
              function escapedIdentity(id) {
                return String(id).replace(/["\\\\]/g, '\\\\$&');
              }
              function nodesFor(id) {
                const escaped = escapedIdentity(id);
                const primaryNodes = Array.from(document.querySelectorAll('[data-element-id="' + escaped + '"]'));
                if (primaryNodes.length) return primaryNodes;
                return Array.from(document.querySelectorAll('[data-element-aliases~="' + escaped + '"]'));
              }
              function styleHidden(el) {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (style.display === 'none' || style.visibility === 'hidden') return 'hidden_style';
                if (Number(style.opacity || '1') === 0) return 'transparent';
                if (style.pointerEvents === 'none') return 'pointer_events_none';
                if (rect.width <= 3 || rect.height <= 3) return 'tiny_surface';
                if (el.classList && (el.classList.contains('hidden') || el.classList.contains('invisible') || el.classList.contains('sr-only'))) return 'hidden_class';
                if (el.getAttribute('aria-hidden') === 'true' && (rect.width <= 3 || rect.height <= 3)) return 'hidden_attr';
                return '';
              }
              function isInteractive(el) {
                if (!el) return false;
                if (el.closest('[data-ghostweb-click-contract-kind="noninteractive"], [aria-disabled="true"]')) return false;
                const tag = String(el.tagName || '').toLowerCase();
                const role = String(el.getAttribute('role') || '').toLowerCase();
                if (['button', 'input', 'select', 'textarea', 'summary'].includes(tag)) return true;
                if (tag === 'a') return !!el.getAttribute('href') || !!el.onclick || el.hasAttribute('data-ghostweb-action');
                if (['button', 'link', 'textbox', 'checkbox', 'radio', 'switch', 'menuitem', 'tab', 'combobox'].includes(role)) return true;
                if (el.hasAttribute('tabindex')) return true;
                return !!el.onclick || el.hasAttribute('data-ghostweb-action') || el.hasAttribute('data-route') || el.hasAttribute('data-target');
              }
              function topmostState(el) {
                function probe() {
                  const rect = el.getBoundingClientRect();
                  const xs = [0.5, 0.25, 0.75].map((ratio) => rect.left + rect.width * ratio);
                  const ys = [0.5, 0.25, 0.75].map((ratio) => rect.top + rect.height * ratio);
                  let sawViewportPoint = false;
                  for (const x of xs) {
                    for (const y of ys) {
                      if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) continue;
                      sawViewportPoint = true;
                      const top = document.elementFromPoint(x, y);
                      if (top && (top === el || el.contains(top))) return '';
                    }
                  }
                  return sawViewportPoint ? 'not_topmost' : 'outside_viewport';
                }
                let state = probe();
                if (state && el.scrollIntoView) {
                  el.scrollIntoView({ block: 'center', inline: 'nearest' });
                  state = probe();
                }
                return state;
              }
              function normalizedText(node) {
                if (!node) return '';
                const text = String(node.textContent || '').replace(/\\s+/g, ' ').trim();
                const aria = String((node.getAttribute && node.getAttribute('aria-label')) || '').replace(/\\s+/g, ' ').trim();
                const title = String((node.getAttribute && node.getAttribute('title')) || '').replace(/\\s+/g, ' ').trim();
                const value = String(node.value || (node.getAttribute && node.getAttribute('value')) || '').replace(/\\s+/g, ' ').trim();
                return [text, aria, title, value].filter(Boolean).join(' ');
              }
              function hostText(node) {
                if (!node) return '';
                const text = String(node.textContent || '').replace(/\\s+/g, ' ').trim();
                if (text) return text;
                const aria = String((node.getAttribute && node.getAttribute('aria-label')) || '').replace(/\\s+/g, ' ').trim();
                if (aria) return aria;
                const title = String((node.getAttribute && node.getAttribute('title')) || '').replace(/\\s+/g, ' ').trim();
                if (title) return title;
                return String(node.value || (node.getAttribute && node.getAttribute('value')) || '').replace(/\\s+/g, ' ').trim();
              }
              function nearbyContextText(el) {
                const parts = [];
                let cur = el;
                for (let depth = 0; depth < 7 && cur; depth += 1) {
                  const text = normalizedText(cur);
                  if (text) parts.push(text);
                  const prev = cur.previousElementSibling;
                  if (prev) {
                    const prevText = normalizedText(prev);
                    if (prevText) parts.push(prevText);
                  }
                  cur = cur.parentElement;
                }
                return parts.join(' ');
              }
              return requiredIds.map((id) => {
                const nodes = nodesFor(id);
                if (!nodes.length) return { component_id: id, status: 'missing' };
                if (nodes.length > 1) return { component_id: id, status: 'ambiguous', occurrences: nodes.length };
                const el = nodes[0];
                const hidden = styleHidden(el);
                const topmost = hidden ? '' : topmostState(el);
                const rect = el.getBoundingClientRect();
                return {
                  component_id: id,
                  status: 'present',
                  tag: String(el.tagName || '').toLowerCase(),
                  class: el.getAttribute('class') || '',
                  role: el.getAttribute('role') || '',
                  text: hostText(el),
                  accessible_text: normalizedText(el),
                  context_text: nearbyContextText(el),
                  hidden_reason: hidden,
                  interactive: isInteractive(el),
                  topmost_reason: topmost,
                  rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                };
              });
            }
            """,
            required_ids,
        )
        await browser.close()
        return {"items": result}


async def _domain_materialization_probe_async(
    html_path: Path,
    *,
    autocomplete_contracts: List[Dict[str, Any]],
    initial_control_contracts: List[Dict[str, Any]],
    viewport: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=_chromium_executable())
        page = await browser.new_page(viewport=dict(viewport or {"width": 390, "height": 844}))
        await _goto_probe_html_ready(page, html_path)
        await page.wait_for_timeout(50)
        result = await page.evaluate(
            """
            async ({ autocompleteContracts, initialControlContracts }) => {
              function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  Number(style.opacity || '1') > 0 && rect.width > 2 && rect.height > 2;
              }
              function esc(value) {
                return String(value || '').replace(/["\\\\]/g, '\\\\$&');
              }
              function findControl(contract) {
                const id = String(contract.control_id || '');
                const label = String(contract.label || '');
                let el = id ? document.querySelector('[data-element-id="' + esc(id) + '"], #' + CSS.escape(id) + ', [name="' + esc(id) + '"]') : null;
                if (!el && label) {
                  const candidates = Array.from(document.querySelectorAll('label,button,[role="checkbox"],[role="radio"],[role="switch"],input'));
                  const host = candidates.find((node) => String(node.textContent || node.value || '').includes(label));
                  if (host) el = host.matches('input') ? host : host.querySelector('input') || host;
                }
                return el;
              }
              const controls = {};
              for (const contract of initialControlContracts || []) {
                const key = String(contract.control_id || contract.label || '');
                const el = findControl(contract);
                if (!key || !el) continue;
                controls[key] = {
                  visible: visible(el),
                  checked: typeof el.checked === 'boolean'
                    ? Boolean(el.checked)
                    : String(el.getAttribute('aria-checked') || '').toLowerCase() === 'true',
                  value: el.value || el.getAttribute('aria-valuenow') || el.textContent || ''
                };
              }

              const autocomplete = {};
              const input = Array.from(document.querySelectorAll('input[type="search"], input[type="text"], input:not([type]), textarea, [role="searchbox"], [contenteditable="true"]')).find(visible);
              for (const contract of autocompleteContracts || []) {
                const query = String(contract.query_value || '');
                if (input && query) {
                  if (input.isContentEditable) input.textContent = query;
                  else input.value = query;
                  input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: query }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  await new Promise((resolve) => setTimeout(resolve, 80));
                }
                const nodes = Array.from(document.querySelectorAll('[data-ghostweb-query-suggestions="true"] *, [role="option"], li, button, a, .suggestion, .autocomplete, .typeahead'));
                const rendered = [];
                for (const node of nodes) {
                  if (!visible(node)) continue;
                  const text = String(node.textContent || '').trim();
                  if (text && !rendered.includes(text)) rendered.push(text);
                }
                autocomplete[String(contract.query_value || contract.input_action_id || 'default')] = {
                  rendered_candidates: rendered,
                  rendered_candidate_count: rendered.length,
                  rendered_categories: []
                };
              }
              return { autocomplete, controls };
            }
            """,
            {
                "autocompleteContracts": autocomplete_contracts,
                "initialControlContracts": initial_control_contracts,
            },
        )
        await browser.close()
        return result if isinstance(result, dict) else {}


def _surfaces_for_filename(
    interaction_surface_contract: Optional[Dict[str, Any]],
    filename: str,
) -> List[Dict[str, Any]]:
    if not isinstance(interaction_surface_contract, dict):
        return []
    surfaces_by_filename = interaction_surface_contract.get("surfaces_by_filename")
    if not isinstance(surfaces_by_filename, dict):
        return []
    surfaces = surfaces_by_filename.get(filename) or []
    return [surface for surface in surfaces if isinstance(surface, dict)]


def _initial_hidden_surface_ids(surfaces: List[Dict[str, Any]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for surface in surfaces:
        cid = str(surface.get("required_data_element_id") or surface.get("surface_id") or "")
        if not cid:
            continue
        policy = str(surface.get("initial_visibility_policy") or "")
        if surface.get("initial_dom_eligible") is False or policy in {
            "hidden_until_transition",
            "runtime_only",
        }:
            result[cid] = str(surface.get("surface_role") or policy or "runtime_surface")
    return result


def _state_surface_probe_contracts(surfaces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    triggers: Dict[str, str] = {}
    roots: Dict[str, Dict[str, Any]] = {}
    options_by_state: Dict[str, List[Dict[str, Any]]] = {}
    for surface in surfaces:
        role = str(surface.get("surface_role") or "")
        cid = str(surface.get("required_data_element_id") or surface.get("surface_id") or "")
        if not cid:
            continue
        expected_effects = surface.get("expected_effects")
        if role in {"shell_broadcast_interaction", "shell_broadcast_interaction_trigger"} and isinstance(expected_effects, list):
            for effect in expected_effects:
                if not isinstance(effect, dict):
                    continue
                overlay_id = str(effect.get("overlay_id") or "").strip()
                if overlay_id:
                    triggers.setdefault(overlay_id, cid)
        if role == "shell_broadcast_state_surface":
            state_id = str(surface.get("required_in_state") or "").removeprefix("state::")
            if state_id:
                roots[state_id] = {"id": cid, "surface_role": role}
        elif role == "shell_broadcast_state_surface_option":
            state_id = str(surface.get("required_in_state") or "").removeprefix("state::")
            label = str(surface.get("required_display_label") or surface.get("control_label") or "")
            if state_id and label:
                options_by_state.setdefault(state_id, []).append(
                    {"id": cid, "label": label, "surface_role": role}
                )
    contracts: List[Dict[str, Any]] = []
    for state_id, root in roots.items():
        options = options_by_state.get(state_id, [])
        if not options:
            continue
        contracts.append(
            {
                "open_state_id": state_id,
                "trigger_id": triggers.get(state_id, ""),
                "root_id": root.get("id", ""),
                "options": options,
            }
        )
    return contracts


async def _runtime_state_surface_probe_async(
    html_path: Path,
    contracts: List[Dict[str, Any]],
    viewport: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    if not contracts:
        return {"items": []}
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=_chromium_executable())
        page = await browser.new_page(viewport=dict(viewport or {"width": 390, "height": 844}))
        await _goto_probe_html_ready(page, html_path)
        await page.wait_for_timeout(50)
        result = await page.evaluate(
            """
            async (contracts) => {
              function escapedIdentity(id) {
                return String(id).replace(/["\\\\]/g, '\\\\$&');
              }
              function findIdentityNode(id) {
                const escaped = escapedIdentity(id);
                const primary = document.querySelector('[data-element-id="' + escaped + '"]');
                if (primary) return primary;
                return document.querySelector('[data-element-aliases~="' + escaped + '"]');
              }
              function styleHidden(el) {
                if (!el) return 'missing';
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (style.display === 'none' || style.visibility === 'hidden') return 'hidden_style';
                if (Number(style.opacity || '1') === 0) return 'transparent';
                if (rect.width <= 3 || rect.height <= 3) return 'tiny_surface';
                if (el.classList && (el.classList.contains('hidden') || el.classList.contains('invisible') || el.classList.contains('sr-only'))) return 'hidden_class';
                return '';
              }
              function isInteractive(el) {
                if (!el) return false;
                if (el.closest('[data-ghostweb-click-contract-kind="noninteractive"], [aria-disabled="true"]')) return false;
                const tag = String(el.tagName || '').toLowerCase();
                const role = String(el.getAttribute('role') || '').toLowerCase();
                if (['button', 'input', 'select', 'textarea', 'summary'].includes(tag)) return true;
                if (tag === 'a') return !!el.getAttribute('href') || !!el.onclick || el.hasAttribute('data-ghostweb-action');
                if (['button', 'link', 'menuitem', 'tab'].includes(role)) return true;
                if (el.hasAttribute('tabindex')) return true;
                return !!el.onclick || el.hasAttribute('data-ghostweb-action') || el.hasAttribute('data-route') || el.hasAttribute('data-target');
              }
              function topmostState(el) {
                if (!el) return 'missing';
                function probe() {
                  const rect = el.getBoundingClientRect();
                  const xs = [0.5, 0.25, 0.75].map((ratio) => rect.left + rect.width * ratio);
                  const ys = [0.5, 0.25, 0.75].map((ratio) => rect.top + rect.height * ratio);
                  let sawViewportPoint = false;
                  for (const x of xs) {
                    for (const y of ys) {
                      if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) continue;
                      sawViewportPoint = true;
                      const top = document.elementFromPoint(x, y);
                      if (top && (top === el || el.contains(top))) return '';
                    }
                  }
                  return sawViewportPoint ? 'not_topmost' : 'outside_viewport';
                }
                let state = probe();
                if (state && el.scrollIntoView) {
                  el.scrollIntoView({ block: 'center', inline: 'nearest' });
                  state = probe();
                }
                return state;
              }
              const findings = [];
              for (const contract of contracts) {
                const trigger = contract.trigger_id ? findIdentityNode(contract.trigger_id) : null;
                if (!trigger) {
                  findings.push({ reason: 'missing_state_surface_trigger', open_state_id: contract.open_state_id, component_id: contract.trigger_id || '' });
                  continue;
                }
                trigger.click();
                await new Promise((resolve) => setTimeout(resolve, 80));
                const root = contract.root_id ? findIdentityNode(contract.root_id) : null;
                const rootHidden = styleHidden(root);
                if (!root || rootHidden) {
                  findings.push({ reason: 'missing_state_surface', open_state_id: contract.open_state_id, component_id: contract.root_id || '', hidden_reason: rootHidden });
                } else {
                  const rootTopmost = topmostState(root);
                  if (rootTopmost) {
                    findings.push({ reason: 'state_surface_not_topmost', open_state_id: contract.open_state_id, component_id: contract.root_id || '', topmost_reason: rootTopmost });
                  }
                }
                for (const option of (contract.options || [])) {
                  const optionEl = option.id ? findIdentityNode(option.id) : null;
                  const hidden = styleHidden(optionEl);
                  if (!optionEl || hidden) {
                    findings.push({ reason: 'missing_state_surface_option', open_state_id: contract.open_state_id, component_id: option.id || '', expected_label: option.label || '', hidden_reason: hidden });
                    continue;
                  }
                  const text = String(optionEl.textContent || '').trim();
                  if (option.label && !text.includes(option.label)) {
                    findings.push({ reason: 'state_surface_option_label_mismatch', open_state_id: contract.open_state_id, component_id: option.id || '', expected_label: option.label || '', actual_label: text });
                  }
                  if (!isInteractive(optionEl)) {
                    findings.push({ reason: 'noninteractive_state_surface_option', open_state_id: contract.open_state_id, component_id: option.id || '', expected_label: option.label || '' });
                  }
                  const topmost = topmostState(optionEl);
                  if (topmost) {
                    findings.push({ reason: 'state_surface_option_not_topmost', open_state_id: contract.open_state_id, component_id: option.id || '', expected_label: option.label || '', topmost_reason: topmost });
                  }
                }
              }
              return findings;
            }
            """,
            contracts,
        )
        await browser.close()
        return {"items": result}


def _domain_materialization_report(
    domain_contracts: Dict[str, Any],
    *,
    rendered_autocomplete_by_page: Dict[str, Dict[str, Any]],
    rendered_initial_controls_by_page: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    from trace2world_pipeline.app_spec.typed_action_contracts import build_domain_contract_materialization_report

    return build_domain_contract_materialization_report(
        domain_contracts,
        rendered_autocomplete_by_page=rendered_autocomplete_by_page,
        rendered_initial_controls_by_page=rendered_initial_controls_by_page,
    )


def _prompt_manifest_path(result_dir: Path | None, filename: str) -> Path | None:
    if result_dir is None:
        return None
    root = Path(result_dir)
    slug = Path(str(filename)).stem
    candidates = [
        root / "phase_pagegen" / "sprites" / slug / "pagegen_prompt_manifest.json",
        root / "sprites" / slug / "pagegen_prompt_manifest.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _render_roles_by_step_key(result_dir: Path | None, filename: str) -> Dict[str, str]:
    manifest_path = _prompt_manifest_path(result_dir, filename)
    if manifest_path is None:
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    annotations = payload.get("prompt_screenshot_annotations")
    if not isinstance(annotations, list):
        return {}
    roles: Dict[str, str] = {}
    for item in annotations:
        if not isinstance(item, dict):
            continue
        step_key = str(item.get("step_key") or "").strip()
        role = str(item.get("render_role") or "").strip()
        if step_key and role:
            roles[step_key] = role
    return roles


def _initial_hidden_required_ids(
    required_ids: Iterable[str],
    render_roles_by_step_key: Dict[str, str],
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    hidden_roles = {
        "interaction_state_change",
        "post_interaction_overlay",
    }
    for cid in required_ids:
        text = str(cid or "")
        for step_key, role in render_roles_by_step_key.items():
            if role not in hidden_roles:
                continue
            if step_key and step_key in text:
                result[text] = role
                break
    return result


def _build_step_replay_context(
    surfaces: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    context: Dict[str, Dict[str, Any]] = {}
    for surface in surfaces:
        if not isinstance(surface, dict):
            continue
        cid = str(surface.get("required_data_element_id") or surface.get("surface_id") or "")
        if not cid:
            continue
        evidence_steps = surface.get("evidence_steps") if isinstance(surface.get("evidence_steps"), list) else []
        render_phase = str(surface.get("render_phase") or "")
        availability = str(surface.get("host_availability_phase") or "")
        requires_scroll = any(
            isinstance(step, dict) and "scroll" in str(step.get("action_kind") or "").lower()
            for step in evidence_steps
        )
        requires_precondition = render_phase in ("runtime", "post_interaction") or availability not in ("", "initial")
        initial_dom = surface.get("initial_dom_eligible", True)
        context[cid] = {
            "requires_scroll": requires_scroll,
            "requires_precondition": requires_precondition or not initial_dom,
            "render_phase": render_phase,
            "host_availability_phase": availability,
            "initial_dom_eligible": initial_dom,
            "evidence_step_count": len(evidence_steps),
        }
    return context


def _surface_label_expectations(surfaces: List[Dict[str, Any]]) -> Dict[str, str]:
    expectations: Dict[str, str] = {}
    for surface in surfaces:
        if not isinstance(surface, dict):
            continue
        if not _surface_requires_exact_visible_label(surface):
            continue
        cid = str(surface.get("required_data_element_id") or surface.get("surface_id") or "")
        visible_contract = surface.get("visible_text_contract") if isinstance(surface.get("visible_text_contract"), dict) else {}
        label = str(
            visible_contract.get("exact_text")
            or surface.get("required_display_label")
            or surface.get("control_label")
            or ""
        ).strip()
        if cid and label:
            expectations[cid] = label
    return expectations


def _surface_context_label_expectations(surfaces: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    expectations: Dict[str, List[str]] = {}
    for surface in surfaces:
        if not isinstance(surface, dict):
            continue
        cid = str(surface.get("required_data_element_id") or surface.get("surface_id") or "")
        if not cid:
            continue
        labels = surface.get("context_text_must_include")
        if not isinstance(labels, list):
            continue
        clean = [
            str(label or "").strip()
            for label in labels
            if str(label or "").strip()
        ]
        if clean:
            expectations[cid] = clean
    return expectations


def _surface_requires_exact_visible_label(surface: Dict[str, Any]) -> bool:
    visible_contract = surface.get("visible_text_contract") if isinstance(surface.get("visible_text_contract"), dict) else {}
    authority = str(surface.get("label_authority") or visible_contract.get("authority") or "").strip()
    if authority in {"semantic_role_only", "context_only", "unresolved"}:
        return False
    if authority in {"observed_host_text", "visual_slot_observed", "exact_visible_label_required"}:
        exact_text = str(visible_contract.get("exact_text") or "").strip()
        if exact_text:
            return True
    label = str(surface.get("required_display_label") or surface.get("control_label") or "").strip()
    if not label:
        return False
    action_kinds = {
        str(kind or "").strip().lower()
        for kind in (surface.get("action_kinds") if isinstance(surface.get("action_kinds"), list) else [])
        if str(kind or "").strip()
    }
    intent = str(surface.get("operation_intent") or "")
    intent_l = intent.lower()
    if action_kinds & {"fill", "type", "input", "fill_input", "text_input"}:
        return False
    if any(token in intent_l for token in ("input", "fill", "type")) or "输入" in intent:
        return False
    allowed_action = bool(action_kinds & {"click", "tap", "select", "open", "toggle"})
    if not allowed_action:
        return False
    expected_effects = surface.get("expected_effects") if isinstance(surface.get("expected_effects"), list) else []
    effect_types = {
        str(effect.get("type") or "").strip()
        for effect in expected_effects
        if isinstance(effect, dict) and str(effect.get("type") or "").strip()
    }
    if effect_types and effect_types <= {"route", "navigate", "navigation"}:
        return False
    label_norm = re.sub(r"\s+", "", label)
    if not label_norm or len(label_norm) > 16:
        return False
    descriptor_tokens = (
        "第一个",
        "第一项",
        "列表",
        "板块",
        "座位图",
        "可选座",
        "搜索框",
        "搜索栏",
        "页面顶部",
        "顶部搜索",
    )
    if any(token in label for token in descriptor_tokens):
        return False
    quoted_values = [
        value.strip()
        for value in re.findall(r"[“\"'《](.+?)[”\"'》]", intent)
        if value.strip()
    ]
    if quoted_values:
        return any(label_norm == re.sub(r"\s+", "", value) for value in quoted_values)
    return len(label_norm) <= 6 and label_norm in re.sub(r"\s+", "", intent)


def _label_matches_required(actual: str, expected: str) -> bool:
    expected_norm = re.sub(r"\s+", "", expected or "")
    if not expected_norm:
        return True
    actual_norm = re.sub(r"\s+", "", actual or "")
    if expected_norm in actual_norm:
        return True
    if re.search(r"(?:\.{2,}|…|⋯)", expected_norm):
        parts = [
            part
            for part in re.split(r"(?:\.{2,}|…|⋯)+", expected_norm)
            if part
        ]
        if not parts:
            return False
        cursor = 0
        for part in parts:
            index = actual_norm.find(part, cursor)
            if index < 0:
                return False
            cursor = index + len(part)
        return True
    return False


async def validate_pagegen_runtime_dom_contracts_async(
    *,
    page_environment_specs_by_filename: Dict[str, Dict[str, Any]],
    generated_pages_dir: Path,
    result_dir: Path | None = None,
    required_component_ids_by_filename: Optional[Dict[str, Iterable[str]]] = None,
    interaction_surface_contract: Optional[Dict[str, Any]] = None,
    domain_contracts: Optional[Dict[str, Any]] = None,
    viewport: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    pages: List[Dict[str, Any]] = []
    pages_dir = Path(generated_pages_dir)
    required_by_filename = required_component_ids_by_filename or {}
    domain = domain_contracts if isinstance(domain_contracts, dict) else {}
    autocomplete_by_page = domain.get("autocomplete_contracts_by_page_id") if isinstance(domain.get("autocomplete_contracts_by_page_id"), dict) else {}
    initial_controls_by_page = domain.get("initial_control_state_contracts_by_page_id") if isinstance(domain.get("initial_control_state_contracts_by_page_id"), dict) else {}
    rendered_autocomplete_by_page: Dict[str, Dict[str, Any]] = {}
    rendered_initial_controls_by_page: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for filename, page_spec in sorted(page_environment_specs_by_filename.items()):
        if not isinstance(page_spec, dict):
            continue
        required_ids = _runtime_required_ids_for_page(
            page_spec,
            required_by_filename.get(filename),
        )
        initial_hidden_ids = _initial_hidden_required_ids(
            required_ids,
            _render_roles_by_step_key(Path(result_dir) if result_dir is not None else None, filename),
        )
        surfaces = _surfaces_for_filename(interaction_surface_contract, filename)
        initial_hidden_ids.update(_initial_hidden_surface_ids(surfaces))
        state_surface_contracts = _state_surface_probe_contracts(surfaces)
        step_replay_context = _build_step_replay_context(surfaces)
        label_expectations = _surface_label_expectations(surfaces)
        context_label_expectations = _surface_context_label_expectations(surfaces)
        html_path = pages_dir / filename
        record: Dict[str, Any] = {
            "filename": filename,
            "html_path": str(html_path),
            "status": "ok",
            "required_count": len(required_ids),
            "present_count": 0,
            "missing": [],
            "ambiguous": [],
            "noninteractive": [],
            "hidden_or_tiny": [],
            "not_topmost": [],
            "label_mismatches": [],
            "context_mismatches": [],
            "initial_visibility_violations": [],
            "state_surface_violations": [],
            "runtime_visibility_warnings": [],
            "runtime_probe_errors": [],
            "html_loaded": False,
        }
        if not html_path.is_file():
            record["status"] = "missing_html"
            record["missing"] = list(required_ids)
            pages.append(record)
            continue
        probe = (
            await _runtime_dom_probe_async(html_path, required_ids, viewport=viewport)
            if viewport is not None
            else await _runtime_dom_probe_async(html_path, required_ids)
        )
        record["html_loaded"] = True
        for item in probe.get("items", []):
            cid = item.get("component_id")
            status = item.get("status")
            if status == "missing":
                record["missing"].append(cid)
                continue
            if status == "ambiguous":
                record["ambiguous"].append({"component_id": cid, "occurrences": item.get("occurrences", 0)})
                continue
            hidden = item.get("hidden_reason")
            if cid in initial_hidden_ids:
                if status == "present" and not hidden:
                    record["initial_visibility_violations"].append({
                        "component_id": cid,
                        "render_role": initial_hidden_ids[cid],
                        "reason": "post_interaction_surface_visible_in_initial_state",
                        "tag": item.get("tag", ""),
                        "class": item.get("class", ""),
                        "rect": item.get("rect", {}),
                    })
                continue
            if hidden:
                record["hidden_or_tiny"].append({
                    "component_id": cid,
                    "reason": hidden,
                    "tag": item.get("tag", ""),
                    "class": item.get("class", ""),
                })
                continue
            expected_context_labels = context_label_expectations.get(str(cid or ""), [])
            missing_context_labels = [
                label
                for label in expected_context_labels
                if not _label_matches_required(str(item.get("context_text") or ""), label)
            ]
            if missing_context_labels:
                record["context_mismatches"].append({
                    "component_id": cid,
                    "expected_context_labels": expected_context_labels,
                    "missing_context_labels": missing_context_labels,
                    "actual_context_text": str(item.get("context_text") or "")[:500],
                    "tag": item.get("tag", ""),
                    "class": item.get("class", ""),
                    "reason": "required_context_label_not_present_near_bound_host",
                })
                continue
            expected_label = label_expectations.get(str(cid or ""))
            actual_label_text = " ".join(
                text
                for text in (
                    str(item.get("text") or ""),
                    str(item.get("accessible_text") or ""),
                )
                if text
            )
            if expected_label and not _label_matches_required(actual_label_text, expected_label):
                record["label_mismatches"].append({
                    "component_id": cid,
                    "expected_label": expected_label,
                    "actual_text": str(item.get("text") or ""),
                    "actual_accessible_text": str(item.get("accessible_text") or ""),
                    "tag": item.get("tag", ""),
                    "class": item.get("class", ""),
                    "reason": "required_display_label_not_present_on_bound_host",
                })
                continue
            if not item.get("interactive"):
                record["noninteractive"].append({
                    "component_id": cid,
                    "reason": "not_actionable_or_noninteractive_runtime_host",
                    "tag": item.get("tag", ""),
                    "class": item.get("class", ""),
                    "role": item.get("role", ""),
                })
                continue
            topmost_reason = str(item.get("topmost_reason") or "")
            if topmost_reason:
                step_ctx = step_replay_context.get(cid, {})
                needs_scroll = step_ctx.get("requires_scroll", False)
                needs_precondition = step_ctx.get("requires_precondition", False)
                entry: Dict[str, Any] = {
                    "component_id": cid,
                    "reason": topmost_reason,
                    "rect": item.get("rect", {}),
                }
                if needs_scroll or needs_precondition:
                    entry["failure_class"] = "validator_semantics_gap"
                    entry["step_replay_oracle"] = {
                        "requires_scroll": needs_scroll,
                        "requires_precondition": needs_precondition,
                        "render_phase": step_ctx.get("render_phase", ""),
                        "host_availability_phase": step_ctx.get("host_availability_phase", ""),
                    }
                    record["not_topmost"].append(entry)
                    continue
                if "checkout" in str(cid) or "l0::" in str(cid) or ":step" in str(cid):
                    record["not_topmost"].append(entry)
                    continue
                record["runtime_visibility_warnings"].append(entry)
            record["present_count"] += 1
        if state_surface_contracts:
            state_probe = (
                await _runtime_state_surface_probe_async(
                    html_path,
                    state_surface_contracts,
                    viewport=viewport,
                )
                if viewport is not None
                else await _runtime_state_surface_probe_async(
                    html_path,
                    state_surface_contracts,
                )
            )
            for item in state_probe.get("items", []):
                if isinstance(item, dict):
                    record["state_surface_violations"].append(item)
        page_autocomplete_contracts = [
            item for item in (autocomplete_by_page.get(filename) or [])
            if isinstance(item, dict)
        ]
        page_initial_control_contracts = [
            item for item in (initial_controls_by_page.get(filename) or [])
            if isinstance(item, dict)
        ]
        if page_autocomplete_contracts or page_initial_control_contracts:
            try:
                domain_probe = (
                    await _domain_materialization_probe_async(
                        html_path,
                        autocomplete_contracts=page_autocomplete_contracts,
                        initial_control_contracts=page_initial_control_contracts,
                        viewport=viewport,
                    )
                    if viewport is not None
                    else await _domain_materialization_probe_async(
                        html_path,
                        autocomplete_contracts=page_autocomplete_contracts,
                        initial_control_contracts=page_initial_control_contracts,
                    )
                )
            except Exception as exc:
                record["runtime_probe_errors"].append({
                    "probe": "domain_materialization",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                })
                domain_probe = {}
            auto_samples = domain_probe.get("autocomplete") if isinstance(domain_probe.get("autocomplete"), dict) else {}
            if auto_samples:
                rendered_candidates: List[str] = []
                rendered_categories: List[str] = []
                rendered_candidate_count = 0
                for sample in auto_samples.values():
                    if not isinstance(sample, dict):
                        continue
                    rendered_candidates.extend(
                        str(item) for item in sample.get("rendered_candidates", [])
                        if str(item or "").strip()
                    )
                    rendered_categories.extend(
                        str(item) for item in sample.get("rendered_categories", [])
                        if str(item or "").strip()
                    )
                    rendered_candidate_count = max(
                        rendered_candidate_count,
                        int(sample.get("rendered_candidate_count") or 0),
                    )
                rendered_autocomplete_by_page[filename] = {
                    "rendered_candidates": list(dict.fromkeys(rendered_candidates)),
                    "rendered_candidate_count": rendered_candidate_count,
                    "rendered_categories": list(dict.fromkeys(rendered_categories)),
                }
            controls_sample = domain_probe.get("controls") if isinstance(domain_probe.get("controls"), dict) else {}
            if controls_sample:
                rendered_initial_controls_by_page[filename] = controls_sample
        if (
            record["missing"]
            or record["ambiguous"]
            or record["noninteractive"]
            or record["hidden_or_tiny"]
            or record["not_topmost"]
            or record["label_mismatches"]
            or record["context_mismatches"]
            or record["initial_visibility_violations"]
            or record["state_surface_violations"]
            or record["runtime_probe_errors"]
        ):
            record["status"] = "violation"
        pages.append(record)

    missing_total = sum(len(p.get("missing", [])) for p in pages)
    ambiguous_total = sum(len(p.get("ambiguous", [])) for p in pages)
    noninteractive_total = sum(len(p.get("noninteractive", [])) for p in pages)
    hidden_or_tiny_total = sum(len(p.get("hidden_or_tiny", [])) for p in pages)
    not_topmost_total = sum(len(p.get("not_topmost", [])) for p in pages)
    label_mismatch_total = sum(len(p.get("label_mismatches", [])) for p in pages)
    context_mismatch_total = sum(len(p.get("context_mismatches", [])) for p in pages)
    initial_visibility_violation_total = sum(len(p.get("initial_visibility_violations", [])) for p in pages)
    state_surface_violation_total = sum(len(p.get("state_surface_violations", [])) for p in pages)
    runtime_visibility_warning_total = sum(len(p.get("runtime_visibility_warnings", [])) for p in pages)
    runtime_probe_error_total = sum(len(p.get("runtime_probe_errors", [])) for p in pages)
    return {
        "format": "pagegen_runtime_dom_contract_audit",
        "schema_revision": 1,
        "viewport": dict(viewport or {"width": 390, "height": 844}),
        "artifact_metadata": {
            "artifact_id": "audits.pagegen_runtime_dom_contract",
            "artifact_kind": "validation_report",
            "api_or_det": "det",
            "logical_owner": "pagegen_dom_contract_validator",
            "producer": "frontend.pagegen_dom_contract_validator.validate_pagegen_runtime_dom_contracts",
            "canonical_source": False,
            "canonical_prompt_boundary": False,
        },
        "summary": {
            "page_count": len(pages),
            "passing_pages": sum(1 for p in pages if p.get("status") == "ok"),
            "failing_pages": sum(1 for p in pages if p.get("status") == "violation"),
            "missing_html_pages": sum(1 for p in pages if p.get("status") == "missing_html"),
            "missing_total": missing_total,
            "ambiguous_total": ambiguous_total,
            "noninteractive_total": noninteractive_total,
            "hidden_or_tiny_total": hidden_or_tiny_total,
            "not_topmost_total": not_topmost_total,
            "label_mismatch_total": label_mismatch_total,
            "context_mismatch_total": context_mismatch_total,
            "initial_visibility_violation_total": initial_visibility_violation_total,
            "state_surface_violation_total": state_surface_violation_total,
            "runtime_visibility_warning_total": runtime_visibility_warning_total,
            "runtime_probe_error_total": runtime_probe_error_total,
            "required_total": sum(p.get("required_count", 0) for p in pages),
            "present_total": sum(p.get("present_count", 0) for p in pages),
        },
        "pages": pages,
        "domain_materialization_report": _domain_materialization_report(
            domain,
            rendered_autocomplete_by_page=rendered_autocomplete_by_page,
            rendered_initial_controls_by_page=rendered_initial_controls_by_page,
        ) if domain else {},
    }


def validate_pagegen_runtime_dom_contracts(
    *,
    page_environment_specs_by_filename: Dict[str, Dict[str, Any]],
    generated_pages_dir: Path,
    result_dir: Path | None = None,
    required_component_ids_by_filename: Optional[Dict[str, Iterable[str]]] = None,
    interaction_surface_contract: Optional[Dict[str, Any]] = None,
    domain_contracts: Optional[Dict[str, Any]] = None,
    viewport: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            validate_pagegen_runtime_dom_contracts_async(
                page_environment_specs_by_filename=page_environment_specs_by_filename,
                generated_pages_dir=generated_pages_dir,
                result_dir=result_dir,
                required_component_ids_by_filename=required_component_ids_by_filename,
                interaction_surface_contract=interaction_surface_contract,
                domain_contracts=domain_contracts,
                viewport=viewport,
            )
        )
    raise RuntimeError(
        "validate_pagegen_runtime_dom_contracts() cannot run inside an asyncio loop; "
        "await validate_pagegen_runtime_dom_contracts_async() instead"
    )


def validate_action_slot_route_contracts(
    *,
    action_slot_registry: Dict[str, Any],
    generated_pages_dir: Path,
) -> Dict[str, Any]:
    slots_by_source = (
        action_slot_registry.get("slots_by_source_filename")
        if isinstance(action_slot_registry, dict)
        else None
    )
    if not isinstance(slots_by_source, dict):
        slots_by_source = {}

    pages: List[Dict[str, Any]] = []
    pages_dir = Path(generated_pages_dir)
    checked_slot_count = 0
    missing_executor_total = 0
    sdk_only_route_total = 0
    missing_component_total = 0
    stateful_route_guard_total = 0
    bound_host_total = 0

    for filename, slots in sorted(slots_by_source.items()):
        if not isinstance(slots, list):
            continue
        html_path = pages_dir / str(filename)
        page_record: Dict[str, Any] = {
            "filename": str(filename),
            "html_path": str(html_path),
            "status": "ok",
            "slot_count": 0,
            "bound_host_count": 0,
            "missing_component": [],
            "missing_executor": [],
            "sdk_only_route": [],
            "stateful_route_guard": [],
            "html_loaded": False,
        }
        if not html_path.is_file():
            page_record["status"] = "missing_html"
            for slot in slots:
                if isinstance(slot, dict):
                    page_record["missing_component"].append(
                        {
                            "action_slot_id": slot.get("action_slot_id"),
                            "component_id": slot.get("component_id"),
                            "target_filename": slot.get("target_filename"),
                            "reason": "missing_html",
                        }
                    )
            pages.append(page_record)
            continue
        try:
            html = html_path.read_text(encoding="utf-8")
        except OSError as exc:
            page_record["status"] = "html_read_error"
            page_record["error"] = repr(exc)
            pages.append(page_record)
            continue

        page_record["html_loaded"] = True
        records_by_id = _tag_records_by_data_element_id(html)
        query_groups: Dict[str, List[Dict[str, Any]]] = {}
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            component_id = str(slot.get("component_id") or "").strip()
            if not component_id or not _slot_query_text(slot):
                continue
            query_groups.setdefault(component_id, []).append(slot)
        for component_id, group in sorted(query_groups.items()):
            queries = {_slot_query_text(slot) for slot in group if _slot_query_text(slot)}
            targets = {
                str(slot.get("target_filename") or "").strip()
                for slot in group
                if str(slot.get("target_filename") or "").strip()
            }
            if len(queries) < 2 or len(targets) < 2:
                continue
            records = records_by_id.get(component_id, [])
            record = records[0] if records else {}
            attrs = str(record.get("attrs") or "")
            data_target = _attr_value(attrs, "data-ghostweb-target")
            data_action = _attr_value(attrs, "data-ghostweb-action")
            if data_action == "navigate" and _base_filename(data_target) in {
                _base_filename(target) for target in targets
            }:
                page_record["stateful_route_guard"].append(
                    {
                        "component_id": component_id,
                        "reason": "fixed_data_target_on_stateful_query_host",
                        "data_ghostweb_target": data_target,
                        "expected_queries": sorted(queries),
                        "expected_targets": sorted(targets),
                    }
                )
                continue
            for slot in group:
                query = _slot_query_text(slot)
                target_filename = str(slot.get("target_filename") or "").strip()
                if not query or not target_filename:
                    continue
                if not _html_has_query_route_guard(html, query, target_filename):
                    page_record["stateful_route_guard"].append(
                        {
                            "action_slot_id": slot.get("action_slot_id"),
                            "component_id": component_id,
                            "target_filename": target_filename,
                            "query_text": query,
                            "reason": "missing_query_route_guard",
                        }
                    )
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            component_id = str(slot.get("component_id") or "").strip()
            target_filename = str(slot.get("target_filename") or "").strip()
            if not component_id or not target_filename:
                continue
            checked_slot_count += 1
            page_record["slot_count"] += 1
            records = records_by_id.get(component_id, [])
            if not records:
                page_record["missing_component"].append(
                    {
                        "action_slot_id": slot.get("action_slot_id"),
                        "component_id": component_id,
                        "target_filename": target_filename,
                        "reason": "missing_data_element_id_or_alias",
                    }
                )
                continue
            record = records[0]
            host_attrs = str(record.get("attrs") or "")
            if (
                'data-ghostweb-action=' in host_attrs.lower()
                and 'data-ghostweb-target=' in host_attrs.lower()
            ):
                page_record["bound_host_count"] += 1
                bound_host_total += 1
            has_executor = _record_has_data_route_executor(
                record, target_filename
            ) or _html_has_route_runtime_target(
                html, target_filename
            )
            if has_executor:
                continue
            item = {
                "action_slot_id": slot.get("action_slot_id"),
                "component_id": component_id,
                "target_filename": target_filename,
                "expected_executor": "GhostwebRouteExecution.navigate",
                "attrs": str(record.get("attrs") or "")[:600],
            }
            if _record_has_sdk_route_side_effect(record, target_filename):
                page_record["sdk_only_route"].append(item)
            else:
                page_record["missing_executor"].append(item)
        if (
            page_record["missing_component"]
            or page_record["missing_executor"]
            or page_record["sdk_only_route"]
            or page_record["stateful_route_guard"]
        ):
            page_record["status"] = "violation"
        pages.append(page_record)

    for page in pages:
        missing_component_total += len(page.get("missing_component", []))
        missing_executor_total += len(page.get("missing_executor", []))
        sdk_only_route_total += len(page.get("sdk_only_route", []))
        stateful_route_guard_total += len(page.get("stateful_route_guard", []))
    blocking_reasons: List[str] = []
    if missing_component_total:
        blocking_reasons.append("missing_data_element_id_or_alias")
    if missing_executor_total:
        blocking_reasons.append("missing_executor")
    if sdk_only_route_total:
        blocking_reasons.append("sdk_only_route")
    if stateful_route_guard_total:
        blocking_reasons.append("stateful_route_guard")

    return {
        "format": "action_slot_route_contract_audit",
        "schema_revision": 2,
        "artifact_metadata": {
            "artifact_id": "audits.action_slot_route_contract",
            "artifact_kind": "validation_report",
            "api_or_det": "det",
            "logical_owner": "pagegen_dom_contract_validator",
            "producer": "frontend.pagegen_dom_contract_validator.validate_action_slot_route_contracts",
            "canonical_source": False,
            "canonical_prompt_boundary": False,
        },
        "summary": {
            "page_count": len(pages),
            "slot_count": checked_slot_count,
            "bound_host_total": bound_host_total,
            "materialized_host_total": bound_host_total,
            "passing_pages": sum(1 for page in pages if page.get("status") == "ok"),
            "failing_pages": sum(1 for page in pages if page.get("status") == "violation"),
            "missing_component_total": missing_component_total,
            "missing_executor_total": missing_executor_total,
            "sdk_only_route_total": sdk_only_route_total,
            "stateful_route_guard_total": stateful_route_guard_total,
        },
        "generated_env_route_closure": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "pages": pages,
    }


_RUNTIME_REFERENCE_PATTERN = re.compile(
    r"GhostwebRouteExecution\.",
    re.IGNORECASE,
)
_RUNTIME_SCRIPT_PATTERN = re.compile(
    r'data-ghostweb-runtime=["\']route-execution["\']',
    re.IGNORECASE,
)
_RETURN_BACK_REFERENCE_PATTERN = re.compile(
    r"GhostwebRouteExecution\.returnBack",
    re.IGNORECASE,
)
_ROUTE_OBLIGATION_PATTERN = re.compile(
    r'data-ghostweb-(?:action|target)\s*=',
    re.IGNORECASE,
)


def validate_runtime_injection_closure(
    *,
    generated_pages_dir: str,
) -> Dict[str, Any]:
    pages_dir = Path(generated_pages_dir)
    if not pages_dir.is_dir():
        return {
            "format": "runtime_injection_closure_audit",
            "schema_revision": 2,
            "summary": {
                "runtime_missing_total": 0,
                "return_back_missing_total": 0,
                "route_obligation_without_runtime_total": 0,
            },
            "pages": [],
        }
    pages: List[Dict[str, Any]] = []
    runtime_missing_total = 0
    return_back_missing_total = 0
    route_obligation_without_runtime_total = 0
    for html_path in sorted(pages_dir.glob("*.html")):
        try:
            html = html_path.read_text(encoding="utf-8")
        except Exception:
            continue
        has_runtime_ref = bool(_RUNTIME_REFERENCE_PATTERN.search(html))
        has_runtime_script = bool(_RUNTIME_SCRIPT_PATTERN.search(html))
        has_return_back_ref = bool(_RETURN_BACK_REFERENCE_PATTERN.search(html))
        has_route_obligation = bool(_ROUTE_OBLIGATION_PATTERN.search(html))
        violations: List[str] = []
        if has_runtime_ref and not has_runtime_script:
            violations.append("runtime_missing:GhostwebRouteExecution referenced but no route-execution runtime script")
            runtime_missing_total += 1
        if has_return_back_ref and not has_runtime_script:
            violations.append("return_back_missing:GhostwebRouteExecution.returnBack referenced but no runtime")
            return_back_missing_total += 1
        if has_route_obligation and not has_runtime_script:
            violations.append("route_obligation_without_runtime:data-ghostweb-action/target present but no runtime script")
            route_obligation_without_runtime_total += 1
        pages.append({
            "filename": html_path.name,
            "has_runtime_reference": has_runtime_ref,
            "has_runtime_script": has_runtime_script,
            "has_return_back_reference": has_return_back_ref,
            "has_route_obligation": has_route_obligation,
            "violations": violations,
            "status": "violation" if violations else "ok",
        })
    return {
        "format": "runtime_injection_closure_audit",
        "schema_revision": 2,
        "summary": {
            "page_count": len(pages),
            "runtime_missing_total": runtime_missing_total,
            "return_back_missing_total": return_back_missing_total,
            "route_obligation_without_runtime_total": route_obligation_without_runtime_total,
            "passing_pages": sum(1 for p in pages if p["status"] == "ok"),
            "failing_pages": sum(1 for p in pages if p["status"] == "violation"),
        },
        "pages": [p for p in pages if p["violations"]],
    }


def write_audit(path: Path, audit: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=2)


def assert_audit_passes(audit: Dict[str, Any]) -> None:
    summary = audit.get("summary", {})
    if (
        summary.get("missing_total")
        or summary.get("ambiguous_total")
        or summary.get("noninteractive_total")
        or summary.get("action_binding_mismatch_total")
        or summary.get("hidden_or_tiny_total")
        or summary.get("not_topmost_total")
        or summary.get("initial_visibility_violation_total")
        or summary.get("state_surface_violation_total")
        or summary.get("missing_html_pages")
        or summary.get("runtime_missing_total")
        or summary.get("return_back_missing_total")
    ):
        raise DomContractViolation(audit)
