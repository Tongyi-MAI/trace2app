
from __future__ import annotations

import json
import html
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


_OPEN_TAG_RE = re.compile(r"<(?P<tag>[A-Za-z][\w:-]*)\b(?P<attrs>[^>]*)>", re.DOTALL)
_INPUT_TAG_RE = re.compile(r"<(?P<tag>input|textarea)\b(?P<attrs>[^>]*)/?>", re.IGNORECASE | re.DOTALL)
_ACTIONABLE_PAIR_RE = re.compile(
    r"<(?P<tag>button|a|div|span)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_PRIMARY_ACTIONABLE_PAIR_RE = re.compile(
    r"<(?P<tag>button|a)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_SECONDARY_ACTIONABLE_PAIR_RE = re.compile(
    r"<(?P<tag>div|span)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_CARD_CONTAINER_PAIR_RE = re.compile(
    r"<(?P<tag>article|li)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_GESTURE_CONTAINER_PAIR_RE = re.compile(
    r"<(?P<tag>section|nav|aside|div|ul|ol)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"</?(?P<tag>[A-Za-z][\w:-]*)\b(?P<attrs>[^>]*)>", re.DOTALL)
_ATTR_RE_TEMPLATE = r'(?<![\w:-]){name}(?![\w:-])\s*=\s*("([^"]*)"|\'([^\']*)\')'
_ACTION_ID_RE = re.compile(r"task_[A-Za-z0-9_]+:step\d+")


_OPAQUE_ACTION_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*")
_NONINTERACTIVE_RE = re.compile(
    r"""\s+data-ghostweb-click-contract-kind\s*=\s*(['"])noninteractive\1""",
    re.IGNORECASE,
)
_COMPLETION_LABELS = ("确认", "确定", "完成", "保存", "提交", "confirm", "done", "save", "submit")
_COMMAND_LABELS = _COMPLETION_LABELS + ("预订", "下一步", "下单", "订购", "book", "reserve", "next")
_GESTURE_EVENTS = {"swipe", "drag", "scroll", "pan", "wheel"}
_GENERIC_BINDING_LABELS = {
    "category",
    "分类",
    "分类入口",
    "入口",
    "按钮",
    "图标",
    "shortcut",
    "shortcuts",
    "control",
    "navigation",
    "nav",
}
_DASH_TRANSLATION = str.maketrans({
    "－": "-",
    "—": "-",
    "–": "-",
    "‑": "-",
    "−": "-",
    "～": "-",
    "~": "-",
})
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(value or "").translate(_DASH_TRANSLATION))


def _strip_self_closing_attr_tail(attrs: str) -> str:
    return re.sub(r"\s*/\s*$", "", attrs or "")


def _attr_value(attrs: str, name: str) -> str:
    pattern = re.compile(_ATTR_RE_TEMPLATE.format(name=re.escape(name)), re.IGNORECASE | re.DOTALL)
    match = pattern.search(attrs or "")
    if not match:
        return ""
    return html.unescape((match.group(2) if match.group(2) is not None else match.group(3) or "").strip())


def _set_attr(attrs: str, name: str, value: str) -> str:
    attrs = _strip_self_closing_attr_tail(attrs)
    pattern = re.compile(_ATTR_RE_TEMPLATE.format(name=re.escape(name)), re.IGNORECASE | re.DOTALL)
    escaped = html.escape(value, quote=True)
    if pattern.search(attrs or ""):
        return pattern.sub(f'{name}="{escaped}"', attrs, count=1)
    return (attrs or "").rstrip() + f' {name}="{escaped}"'


def _remove_attr(attrs: str, name: str) -> str:
    pattern = re.compile(
        rf"""\s+(?<![\w:-]){re.escape(name)}(?![\w:-])(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>]+))?""",
        re.IGNORECASE,
    )
    return pattern.sub("", attrs or "")


def _remove_noninteractive(attrs: str) -> str:
    attrs = _NONINTERACTIVE_RE.sub("", attrs or "")
    attrs = re.sub(r"""\s+aria-disabled\s*=\s*(['"])true\1""", "", attrs, flags=re.IGNORECASE)
    return attrs


def _make_action_host_visible(attrs: str) -> str:
    attrs = _remove_attr(attrs, "hidden")
    attrs = re.sub(r"""\s+aria-hidden\s*=\s*(['"])true\1""", "", attrs, flags=re.IGNORECASE)
    class_value = _attr_value(attrs, "class")
    if class_value:
        classes = [
            cls
            for cls in class_value.split()
            if cls not in {"hidden", "invisible", "pointer-events-none"}
        ]
        attrs = _set_attr(attrs, "class", " ".join(classes))
    style_value = _attr_value(attrs, "style")
    if style_value:
        declarations = []
        for declaration in style_value.split(";"):
            name, sep, value = declaration.partition(":")
            if not sep:
                if declaration.strip():
                    declarations.append(declaration.strip())
                continue
            prop = name.strip().lower()
            normalized_value = re.sub(r"\s+", "", value).lower()
            if prop == "display" and normalized_value == "none":
                continue
            if prop == "visibility" and normalized_value == "hidden":
                continue
            if prop == "pointer-events" and normalized_value == "none":
                continue
            declarations.append(f"{name.strip()}: {value.strip()}")
        if declarations:
            attrs = _set_attr(attrs, "style", "; ".join(declarations))
        else:
            attrs = _remove_attr(attrs, "style")
    return attrs


def _make_fill_input_visible(attrs: str) -> str:
    return _make_action_host_visible(attrs)


def _tokens(value: str) -> List[str]:
    return [token for token in re.split(r"\s+", value or "") if token]


def _extract_action_ids(value: str) -> List[str]:
    seen: set[str] = set()
    ids: List[str] = []
    for token in _tokens(value):
        for match in _ACTION_ID_RE.finditer(token):
            action_id = match.group(0)
            if action_id not in seen:
                seen.add(action_id)
                ids.append(action_id)
    return ids


def _remove_action_id_from_attr(
    attrs: str,
    attr_name: str,
    action_id: str,
    *,
    remove_embedded: bool = True,
) -> str:
    current = _attr_value(attrs, attr_name)
    if not current:
        return attrs
    remaining = [
        token
        for token in _tokens(current)
        if token != action_id
        and (
            not remove_embedded
            or action_id not in _extract_action_ids(token)
        )
    ]
    if len(remaining) == len(_tokens(current)):
        return attrs
    if not remaining:
        return _remove_attr(attrs, attr_name)
    return _set_attr(attrs, attr_name, " ".join(remaining))


def _binding_component_ref(binding: Mapping[str, Any]) -> str:
    identity = _as_dict(binding.get("element_identity"))
    payload = _binding_payload(binding)
    for value in (
        identity.get("component_ref"),
        binding.get("component_ref"),
        payload.get("component_ref"),
    ):
        text = _text(value)
        if text:
            return text
    return ""


def _sync_component_ref_attrs(attrs: str, binding: Mapping[str, Any]) -> str:
    component_ref = _binding_component_ref(binding)
    if not component_ref:
        return attrs
    primary = _attr_value(attrs, "data-element-id")
    aliases = _tokens(_attr_value(attrs, "data-element-aliases"))
    if primary == component_ref or component_ref in aliases:
        return attrs
    if not primary:
        return _set_attr(attrs, "data-element-id", component_ref)
    aliases.append(component_ref)
    return _set_attr(attrs, "data-element-aliases", " ".join(aliases))


def _read_target_map(attrs: str) -> Dict[str, str]:
    raw = _attr_value(attrs, "data-ghostweb-target-map")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in parsed.items()
        if str(key or "").strip() and str(value or "").strip()
    }


def _write_target_map(attrs: str, target_map: Mapping[str, str]) -> str:
    clean = {
        str(key): str(value)
        for key, value in sorted(target_map.items())
        if str(key or "").strip() and str(value or "").strip()
    }
    if not clean:
        return _remove_attr(attrs, "data-ghostweb-target-map")
    return _set_attr(
        attrs,
        "data-ghostweb-target-map",
        json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _read_visibility_precondition_map(attrs: str) -> Dict[str, Dict[str, str]]:
    raw = _attr_value(attrs, "data-ghostweb-visibility-precondition-map")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    clean: Dict[str, Dict[str, str]] = {}
    for action_id, value in parsed.items():
        if not str(action_id or "").strip() or not isinstance(value, dict):
            continue
        row = {
            key: str(value.get(key) or "").strip()
            for key in ("producer_action_id", "state_ref", "required_visible_value")
        }
        if row["producer_action_id"]:
            clean[str(action_id)] = row
    return clean


def _write_visibility_precondition_map(
    attrs: str,
    precondition_map: Mapping[str, Mapping[str, str]],
) -> str:
    clean = {
        str(action_id): {
            key: str(row.get(key) or "").strip()
            for key in ("producer_action_id", "state_ref", "required_visible_value")
        }
        for action_id, row in sorted(precondition_map.items())
        if str(action_id or "").strip() and str(row.get("producer_action_id") or "").strip()
    }
    if not clean:
        return _remove_attr(attrs, "data-ghostweb-visibility-precondition-map")
    return _set_attr(
        attrs,
        "data-ghostweb-visibility-precondition-map",
        json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _action_tokens_from_attrs(attrs: str) -> List[str]:
    action_ids: List[str] = []
    for attr_name in ("data-action-id", "data-action-aliases"):
        for token in _tokens(_attr_value(attrs, attr_name)):
            if not _OPAQUE_ACTION_TOKEN_RE.fullmatch(token):
                continue
            action_id = token
            if action_id not in action_ids:
                action_ids.append(action_id)
    for attr_name in ("data-element-id", "data-element-aliases"):
        for action_id in _extract_action_ids(_attr_value(attrs, attr_name)):
            if action_id not in action_ids:
                action_ids.append(action_id)
    for action_id in _read_target_map(attrs).keys():
        if _OPAQUE_ACTION_TOKEN_RE.fullmatch(action_id) and action_id not in action_ids:
            action_ids.append(action_id)
    return action_ids


def _remove_action_id_from_target_map(attrs: str, action_id: str) -> str:
    target_map = _read_target_map(attrs)
    if not target_map:
        return attrs
    target_map = {
        key: value
        for key, value in target_map.items()
        if action_id not in key
    }
    return _write_target_map(attrs, target_map)


def _protect_script_blocks(html_text: str) -> Tuple[str, List[str]]:
    blocks: List[str] = []

    def repl(match: re.Match[str]) -> str:
        blocks.append(match.group(0))
        return f"__GHOSTWEB_SCRIPT_BLOCK_{len(blocks) - 1}__"

    return _SCRIPT_BLOCK_RE.sub(repl, html_text), blocks


def _restore_script_blocks(html_text: str, blocks: List[str]) -> str:
    for index, block in enumerate(blocks):
        html_text = html_text.replace(f"__GHOSTWEB_SCRIPT_BLOCK_{index}__", block)
    return html_text


def _normalize_malformed_input_open_tags(html_text: str) -> str:
    html_text = re.sub(
        r"<(input|textarea)(?=(?:type|id|class|role|name|placeholder|autocomplete)=)",
        r"<\1 ",
        html_text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(<(?:input|textarea)\b[^>]*?)\s*/\s+(?=(?:role|data-[\w:-]+|aria-[\w:-]+|class|id|name|type|placeholder|autocomplete|inputmode)\s*=)",
        r"\1 ",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _remove_orphan_target_map_suffixes(attrs: str) -> str:
    attrs = re.sub(r"""(?<=")-map\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", attrs or "", flags=re.IGNORECASE)
    attrs = re.sub(r"""(?<=')-map\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", attrs, flags=re.IGNORECASE)
    attrs = re.sub(r"""\s+-map\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", attrs, flags=re.IGNORECASE)
    return attrs


def _remove_malformed_target_map_fragments(html_text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        tag = match.group("tag")
        attrs = _remove_orphan_target_map_suffixes(match.group("attrs"))
        return f"<{tag}{attrs}>"

    return _OPEN_TAG_RE.sub(repl, html_text)


def _strip_action_id_from_other_hosts(html_text: str, action_id: str, keep_start: Optional[int] = None) -> str:
    def repl(match: re.Match[str]) -> str:
        if keep_start is not None and match.start() == keep_start:
            return match.group(0)
        tag = match.group("tag")
        attrs = match.group("attrs")
        if action_id not in attrs:
            return match.group(0)
        if _attr_value(attrs, "data-action-id") == action_id:
            attrs = _remove_attr(attrs, "data-ghostweb-action")
            attrs = _remove_attr(attrs, "data-ghostweb-target")
            attrs = _remove_attr(attrs, "data-action-id")
            attrs = _remove_attr(attrs, "data-ghostweb-effect-kind")
            attrs = _remove_attr(attrs, "data-ghostweb-transaction-id")
            attrs = _remove_attr(attrs, "data-ghostweb-dom-binding-id")
        attrs = _remove_action_id_from_attr(attrs, "data-action-aliases", action_id)
        attrs = _remove_action_id_from_attr(attrs, "data-element-id", action_id, remove_embedded=False)
        attrs = _remove_action_id_from_attr(attrs, "data-element-aliases", action_id, remove_embedded=False)
        attrs = _remove_action_id_from_target_map(attrs, action_id)
        return f"<{tag}{attrs}>"

    return _OPEN_TAG_RE.sub(repl, html_text)


def _component_identity_host_matches(attrs: str, action_id: str) -> bool:
    if not action_id:
        return False
    for attr_name in ("data-element-id", "data-element-aliases"):
        if action_id in _extract_action_ids(_attr_value(attrs, attr_name)):
            return True
    return False


def _element_body_for_open_tag(html_text: str, opening: re.Match[str]) -> str:
    tag_name = str(opening.group("tag") or "").lower()
    if not tag_name or tag_name in _VOID_TAGS:
        return ""
    depth = 1
    for token in _HTML_TAG_RE.finditer(html_text, opening.end()):
        token_tag = str(token.group("tag") or "").lower()
        if token_tag != tag_name:
            continue
        raw = token.group(0)
        if raw.startswith("</"):
            depth -= 1
            if depth == 0:
                return html_text[opening.end() : token.start()]
        elif not raw.rstrip().endswith("/>"):
            depth += 1
    return ""


def _component_host_matches_visible_label(
    html_text: str,
    opening: re.Match[str],
    labels: Iterable[str],
) -> bool:
    labels = list(labels)
    if not labels:
        return True
    body = _element_body_for_open_tag(html_text, opening)
    return any(
        _label_matches_host(label, _visible_text(body), opening.group("attrs"))
        for label in labels
    )


def _component_ref_host_matches(attrs: str, binding: Mapping[str, Any]) -> bool:
    component_ref = _binding_component_ref(binding)
    if not component_ref:
        return False
    if _attr_value(attrs, "data-element-id") == component_ref:
        return True
    return component_ref in _tokens(_attr_value(attrs, "data-element-aliases"))


def _component_ref_match_is_eligible(
    html_text: str,
    match: re.Match[str],
    *,
    click_host: bool,
) -> bool:
    tag = match.group("tag")
    attrs = match.group("attrs")
    attrs_lower = html.unescape(attrs or "").lower()
    if _attrs_have_hidden_state(attrs):
        return False
    if (
        _hidden_ancestor_penalty(html_text, match.start()) < 0
        and not _hidden_ancestors_are_page_owned_transition_layers(
            html_text,
            match.start(),
        )
    ):
        return False
    if _attr_value(attrs, "data-ghostweb-initial-visibility") == "hidden_until_transition":
        return False
    if "retired" in _attr_value(attrs, "data-ghostweb-action-host").lower():
        return False
    if "noninteractive" in attrs_lower:
        return False
    if _attr_value(attrs, "disabled") or _attr_value(attrs, "aria-disabled").lower() == "true":
        return False
    if not click_host:
        existing_gesture_host = bool(_attr_value(attrs, "data-gesture-host"))
        existing_route_host = bool(
            _attr_value(attrs, "href")
            or _attr_value(attrs, "data-ghostweb-target")
            or _attr_value(attrs, "data-ghostweb-action") == "navigate"
            or re.search(
                r"(?:navigate|location\s*=|location\.href)",
                _attr_value(attrs, "onclick"),
                re.IGNORECASE,
            )
        )
        if existing_route_host and not existing_gesture_host:
            return False
    if not click_host:
        return True
    return (
        _is_click_host_candidate(tag, attrs)
        or tag.lower() in {"input", "textarea", "select"}
        or _attr_value(attrs, "contenteditable").lower() == "true"
    )


def _exact_component_ref_matches(
    html_text: str,
    binding: Mapping[str, Any],
    *,
    click_host: bool,
) -> List[re.Match[str]]:
    return [
        match
        for match in _OPEN_TAG_RE.finditer(html_text)
        if _component_ref_host_matches(match.group("attrs"), binding)
        and _component_ref_match_is_eligible(
            html_text,
            match,
            click_host=click_host,
        )
    ]


def _set_canonical_action_id(attrs: str, action_id: str) -> str:
    action_ids = _action_tokens_from_attrs(attrs)
    if action_id and action_id not in action_ids:
        action_ids.insert(0, action_id)
    primary = _attr_value(attrs, "data-action-id")
    if primary not in action_ids:
        primary = action_id or (action_ids[0] if action_ids else "")
    aliases = [candidate for candidate in action_ids if candidate and candidate != primary]
    attrs = _remove_attr(attrs, "data-ghostweb-action-id")
    attrs = _set_attr(attrs, "data-action-id", primary)
    if aliases:
        attrs = _set_attr(attrs, "data-action-aliases", " ".join(aliases))
    else:
        attrs = _remove_attr(attrs, "data-action-aliases")
    return attrs


def _sync_action_aliases(attrs: str, action_ids: Iterable[str], preferred_primary: str = "") -> str:
    ids: List[str] = []
    for action_id in list(_action_tokens_from_attrs(attrs)) + [str(x) for x in action_ids if str(x or "").strip()]:
        if action_id and action_id not in ids:
            ids.append(action_id)
    primary = _attr_value(attrs, "data-action-id")
    if preferred_primary and preferred_primary in ids:
        primary = preferred_primary
    if not primary or primary not in ids:
        primary = ids[0] if ids else preferred_primary
    if primary:
        attrs = _set_attr(attrs, "data-action-id", primary)
    aliases = [action_id for action_id in ids if action_id and action_id != primary]
    if aliases:
        attrs = _set_attr(attrs, "data-action-aliases", " ".join(aliases))
    else:
        attrs = _remove_attr(attrs, "data-action-aliases")
    return attrs


def _visible_text(fragment: str) -> str:
    text = re.sub(r"<script\b[\s\S]*?</script>", " ", fragment or "", flags=re.IGNORECASE)
    text = re.sub(r"<style\b[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return _normalize_match_text(text)


def _compact_text(value: str) -> str:
    return _normalize_match_text(value)


def _label_matches_haystack(label: str, haystack: str) -> bool:
    if not label:
        return False
    if label in haystack:
        return True
    compact_label = _compact_text(label)
    compact_haystack = _compact_text(haystack)
    if compact_label and compact_label in compact_haystack:
        return True
    if compact_label and compact_label.lower() in compact_haystack.lower():
        return True
    parts = [part for part in re.split(r"[\s,/，、:：|]+", html.unescape(label or "")) if part]
    return bool(
        len(parts) >= 2
        and all(_compact_text(part).lower() in compact_haystack.lower() for part in parts)
    )


def _label_matches_host(label: str, text: str, attrs: str) -> bool:
    compact_label = _compact_text(label)
    if not compact_label:
        return False
    compact_label_lower = compact_label.lower()
    if compact_label_lower in {_compact_text(value).lower() for value in _COMPLETION_LABELS}:
        explicit_values = [
            text,
            _attr_value(attrs, "aria-label"),
            _attr_value(attrs, "title"),
        ]
        return any(compact_label_lower in _compact_text(value).lower() for value in explicit_values)
    if len(compact_label) <= 2:
        explicit_values = [
            text,
            _attr_value(attrs, "aria-label"),
            _attr_value(attrs, "title"),
            _attr_value(attrs, "data-label"),
            _attr_value(attrs, "data-value"),
            _attr_value(attrs, "data-seat-pref"),
        ]
        return any(compact_label == _compact_text(value) for value in explicit_values)
    return _label_matches_haystack(label, text + " " + attrs)


def _explicit_host_label_text(attrs: str, body: str) -> str:
    values = [
        _visible_text(body),
        _attr_value(attrs, "aria-label"),
        _attr_value(attrs, "title"),
        _attr_value(attrs, "data-label"),
        _attr_value(attrs, "data-value"),
        _attr_value(attrs, "value"),
    ]
    for _quote, value in re.findall(
        r"\b(?:alt|aria-label|title|data-label|data-value|value)\s*=\s*(['\"])(.*?)\1",
        body or "",
        flags=re.IGNORECASE | re.DOTALL,
    ):
        values.append(value)
    return " ".join(value for value in values if value)


def _host_satisfies_required_visible_label(binding: Mapping[str, Any], attrs: str, body: str) -> bool:
    required = _binding_required_visible_labels(binding)
    if not required:
        return True
    explicit = _explicit_host_label_text(attrs, body)
    return any(_label_matches_haystack(label, explicit) for label in required)


def _attrs_have_hidden_state(attrs: str) -> bool:
    attrs_lower = html.unescape(attrs or "").lower()
    if re.search(r"(?<![\w:-])hidden(?![\w:-])(?:\s|=|$)", attrs_lower):
        return True
    if re.search(r"(^|\s)hidden(\s|$)", _attr_value(attrs, "class")):
        return True
    if re.search(r"(^|\s)(invisible|pointer-events-none)(\s|$)", _attr_value(attrs, "class")):
        return True
    if re.search(r"""aria-hidden\s*=\s*(['"])true\1""", attrs_lower):
        return True
    style = _attr_value(attrs, "style").lower()
    if re.search(r"(^|;)\s*display\s*:\s*none\s*(;|$)", style):
        return True
    if re.search(r"(^|;)\s*visibility\s*:\s*hidden\s*(;|$)", style):
        return True
    return False


def _open_ancestor_stack(html_text: str, position: int) -> List[Tuple[str, str]]:
    stack: List[Tuple[str, str]] = []
    for match in _HTML_TAG_RE.finditer(html_text[: max(position, 0)]):
        raw = match.group(0)
        tag = match.group("tag").lower()
        if raw.startswith("</"):
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == tag:
                    del stack[idx:]
                    break
            continue
        if raw.rstrip().endswith("/>") or tag in _VOID_TAGS:
            continue
        stack.append((tag, match.group("attrs") or ""))
    return stack


def _hidden_ancestor_penalty(html_text: str, position: int) -> int:
    stack = _open_ancestor_stack(html_text, position)
    return -360 if any(_attrs_have_hidden_state(attrs) for _tag, attrs in stack) else 0


def _hidden_ancestors_are_page_owned_transition_layers(
    html_text: str,
    position: int,
) -> bool:

    hidden_ancestors = [
        attrs
        for _tag, attrs in _open_ancestor_stack(html_text, position)
        if _attrs_have_hidden_state(attrs)
    ]
    if not hidden_ancestors:
        return False
    return all(
        _attr_value(attrs, "data-ghostweb-initial-visibility")
        == "hidden_until_transition"
        and (
            _attr_value(attrs, "role").lower() == "dialog"
            or _attr_value(attrs, "aria-modal").lower() == "true"
            or bool(_attr_value(attrs, "data-ghostweb-overlay-layer-role"))
        )
        for attrs in hidden_ancestors
    )


def _is_command_label(label: str) -> bool:
    compact = _compact_text(label).lower()
    if not compact:
        return False
    commands = [_compact_text(command).lower() for command in _COMMAND_LABELS]
    if compact in commands:
        return True


    return len(compact) <= 16 and any(command and command in compact for command in commands)


def _command_host_score(
    *,
    effect_kind: str,
    labels: Iterable[str],
    matching_labels: Iterable[str],
    context: str,
) -> int:
    if effect_kind not in {"route", "stateful_submit", "save_return"}:
        return 0
    match_set = {_compact_text(label).lower() for label in matching_labels if _compact_text(label)}
    labels_list = [label for label in labels if _compact_text(label)]
    command_matches = [label for label in labels_list if _is_command_label(label) and _compact_text(label).lower() in match_set]
    if not command_matches:
        if any(_is_command_label(label) for label in labels_list):
            return -120
        return 0
    score = 220
    non_command_labels = [label for label in labels_list if not _is_command_label(label)]
    context_hits = [label for label in non_command_labels if _label_matches_haystack(label, context)]
    if context_hits:
        score += 160 * len(context_hits)
    elif non_command_labels:
        score -= 180
    return score


def _local_action_context(html_text: str, start: int, end: int) -> str:
    lower = html_text.lower()
    block_open = max(
        lower.rfind("<li", 0, start),
        lower.rfind("<tr", 0, start),
        lower.rfind("<div", 0, start),
        lower.rfind("<article", 0, start),
        lower.rfind("<section", 0, start),
    )
    if block_open < 0:
        block_open = max(0, start - 240)
    block_close_candidates = [
        pos
        for pos in (
            lower.find("</li>", end),
            lower.find("</tr>", end),
            lower.find("</div>", end),
            lower.find("</article>", end),
            lower.find("</section>", end),
        )
        if pos >= 0
    ]
    block_close = min(block_close_candidates) + 10 if block_close_candidates else min(len(html_text), end + 240)
    return html_text[block_open:block_close]


def _enclosing_action_contexts(html_text: str, position: int) -> List[str]:

    stack: List[Tuple[str, int, int]] = []
    for match in _HTML_TAG_RE.finditer(html_text, 0, max(position, 0)):
        raw = match.group(0)
        tag = match.group("tag").lower()
        if raw.startswith("</"):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == tag:
                    del stack[index:]
                    break
            continue
        if raw.rstrip().endswith("/>") or tag in _VOID_TAGS:
            continue
        stack.append((tag, match.start(), match.end()))

    contexts: List[str] = []
    for tag, open_start, open_end in reversed(stack):
        if tag not in {"div", "li", "article", "section", "form", "main"}:
            continue
        depth = 1
        close_end = -1
        for match in _HTML_TAG_RE.finditer(html_text, open_end):
            if match.group("tag").lower() != tag:
                continue
            raw = match.group(0)
            if raw.startswith("</"):
                depth -= 1
                if depth == 0:
                    close_end = match.end()
                    break
            elif not raw.rstrip().endswith("/>"):
                depth += 1
        if close_end > position:
            contexts.append(html_text[open_start:close_end])
    return contexts


def _quoted_intent_labels(binding: Mapping[str, Any]) -> List[str]:
    intent = _text(_binding_payload(binding).get("operation_intent"))
    labels: List[str] = []
    for left, value, right in re.findall(r'([“"「『])([^”"」』]+)([”"」』])', intent):
        del left, right
        text = _text(value)
        if text and text not in labels:
            labels.append(text)
    return labels


def _contextual_command_host_score(
    html_text: str,
    start: int,
    attrs: str,
    body: str,
    binding: Mapping[str, Any],
) -> int:

    host_text = _explicit_host_label_text(attrs, body)
    command_labels = [label for label in _quoted_intent_labels(binding) if _label_matches_haystack(label, host_text)]
    if not command_labels:
        return 0
    entity_labels = [
        label
        for label in _binding_labels(binding)
        if not _is_command_label(label)
        and not any(_compact_text(label) == _compact_text(command) for command in command_labels)
    ]
    if not entity_labels:
        return 0
    for depth, context in enumerate(_enclosing_action_contexts(html_text, start)):
        context_text = _visible_text(context)
        if any(_label_matches_haystack(label, context_text) for label in entity_labels):
            return max(240, 760 - depth * 40)
    return 0


def _binding_effect(binding: Mapping[str, Any]) -> str:
    return _text(_as_dict(_as_dict(binding.get("interaction")).get("payload")).get("effect_kind"))


def _binding_event(binding: Mapping[str, Any]) -> str:
    return _text(_as_dict(binding.get("interaction")).get("event")).lower()


def _binding_action_id(binding: Mapping[str, Any]) -> str:
    return _text(binding.get("action_id") or _as_dict(_as_dict(binding.get("interaction")).get("payload")).get("action_id"))


def _binding_target(binding: Mapping[str, Any]) -> str:
    return _text(_as_dict(binding.get("postcondition")).get("target_filename"))


def _binding_labels(binding: Mapping[str, Any]) -> List[str]:
    labels = [_text(v) for v in _as_list(_as_dict(binding.get("element_identity")).get("text_must_include"))]
    for quoted in _quoted_intent_labels(binding):
        candidates = [quoted]
        parenthetical = re.fullmatch(r"(.+?)[（(]([^）)]+)[）)]", quoted)
        if parenthetical:
            candidates.extend([parenthetical.group(1), parenthetical.group(2)])
        for candidate in candidates:
            candidate = _text(candidate)
            if candidate and candidate not in labels:
                labels.append(candidate)
    return [label for label in labels if label]


def _binding_intent_text(binding: Mapping[str, Any]) -> str:
    parts: List[str] = []
    payload = _binding_payload(binding)
    for key in ("operation_intent", "intent", "label"):
        text = _text(payload.get(key))
        if text:
            parts.append(text)
    for value in _as_dict(_as_dict(binding.get("postcondition")).get("state_diff")).values():
        text = _text(value)
        if text:
            parts.append(text)
    for label in _binding_labels(binding):
        parts.append(label)
    for label in _binding_context_labels(binding):
        parts.append(label)
    return " ".join(parts)


def _binding_input_dependency_text(binding: Mapping[str, Any]) -> str:

    if _binding_effect(binding) != "query_projection":
        return ""
    parts: List[str] = []
    payload = _binding_payload(binding)
    for key in ("operation_intent", "intent", "label"):
        text = _text(payload.get(key))
        if text and text not in parts:
            parts.append(text)
    for state_diff in (
        _binding_state_diff(binding),
        _binding_canonical_expected_state_diff(binding),
        _binding_downstream_expected_state_diff(binding),
    ):
        for value in state_diff.values():
            text = _text(value)
            if text and text not in parts:
                parts.append(text)
    for label in [*_binding_labels(binding), *_binding_context_labels(binding)]:
        if label and label not in parts:
            parts.append(label)
    return " ".join(parts)


def _matching_labels(binding: Mapping[str, Any], labels: Iterable[str]) -> List[str]:
    expanded: List[str] = []
    for label in labels:
        if label and label not in expanded:
            expanded.append(label)
    if _binding_effect(binding) in {"save_return", "stateful_submit"}:
        compact_labels = {_compact_text(label).lower() for label in expanded}
        if any(_compact_text(label).lower() in compact_labels for label in _COMPLETION_LABELS):
            for label in _COMPLETION_LABELS:
                if label not in expanded:
                    expanded.append(label)
    return expanded


def _binding_payload(binding: Mapping[str, Any]) -> Dict[str, Any]:
    return _as_dict(_as_dict(binding.get("interaction")).get("payload"))


def _binding_visibility_precondition(binding: Mapping[str, Any]) -> Dict[str, Any]:
    return _as_dict(_binding_payload(binding).get("visibility_precondition"))


def _binding_required_visible_labels(binding: Mapping[str, Any]) -> List[str]:
    payload = _binding_payload(binding)
    labels: List[str] = []
    for value in (
        payload.get("required_visible_value"),
        _binding_visibility_precondition(binding).get("required_visible_value"),
    ):
        text = _text(value)
        if text and text not in labels:
            labels.append(text)
    return labels


def _binding_context_labels(binding: Mapping[str, Any]) -> List[str]:
    identity = _as_dict(binding.get("element_identity"))
    labels: List[str] = []
    for value in _as_list(identity.get("context_text_must_include")):
        text = _text(value)
        if text and text not in labels:
            labels.append(text)
    return labels


def _binding_accessible_labels(binding: Mapping[str, Any]) -> List[str]:
    identity = _as_dict(binding.get("element_identity"))
    labels: List[str] = []
    for value in [*_as_list(identity.get("accessible_label_must_include")), *_binding_labels(binding)]:
        text = _text(value)
        if text and text not in labels:
            labels.append(text)
    return labels


def _binding_primary_materialized_label(binding: Mapping[str, Any]) -> str:
    for label in _binding_required_visible_labels(binding):
        if label:
            return label
    for label in _binding_labels(binding):
        if label and _compact_text(label).lower() not in _GENERIC_BINDING_LABELS:
            return label
    labels = _binding_labels(binding)
    return labels[0] if labels else _binding_action_id(binding)


def _binding_state_refs(binding: Mapping[str, Any]) -> List[str]:
    payload = _binding_payload(binding)
    refs: List[str] = []
    for key in ("state_refs", "state_reads"):
        for ref in _as_list(payload.get(key)):
            text = _text(ref)
            if text and text not in refs:
                refs.append(text)
    return refs


def _binding_state_diff(binding: Mapping[str, Any]) -> Dict[str, Any]:
    return _as_dict(_as_dict(binding.get("postcondition")).get("state_diff"))


def _binding_canonical_expected_state_diff(binding: Mapping[str, Any]) -> Dict[str, Any]:
    return _as_dict(_as_dict(binding.get("postcondition")).get("canonical_expected_state_diff"))


def _binding_downstream_expected_state_diff(binding: Mapping[str, Any]) -> Dict[str, Any]:
    return _as_dict(_as_dict(binding.get("postcondition")).get("downstream_expected_state_diff"))


def _binding_closes_owning_overlay(binding: Mapping[str, Any]) -> bool:

    if _binding_effect(binding) != "same_page_mutation":
        return False
    diffs = (
        _binding_state_diff(binding),
        _binding_canonical_expected_state_diff(binding),
        _binding_downstream_expected_state_diff(binding),
    )

    def value_of(value: Any) -> Any:
        value = _as_dict(value)
        return value.get("value") if "value" in value else value

    for state_diff in diffs:
        for ref, raw_value in state_diff.items():
            key = _text(ref).lower()
            value = value_of(raw_value)
            if re.search(r"(?:^|[._])(dismissed|closed)$", key) and (
                value is True or _text(value).lower() == "true"
            ):
                return True

    progressive = re.compile(
        r"(?:overlay_show|open_overlay|opened|select_radio_option|selection|selected)(?:$|[._])"
    )
    if any(progressive.search(_text(ref).lower()) for state_diff in diffs for ref in state_diff):
        return False

    return any(
        re.search(r"(?:^|[._])visible$", _text(ref).lower())
        and (value_of(raw_value) is False or _text(value_of(raw_value)).lower() == "false")
        for state_diff in diffs
        for ref, raw_value in state_diff.items()
    )


def _binding_same_page_activation_ref(binding: Mapping[str, Any]) -> str:

    if _binding_effect(binding) != "same_page_mutation":
        return ""
    refs = list(_binding_state_refs(binding))
    for state_diff in (
        _binding_state_diff(binding),
        _binding_canonical_expected_state_diff(binding),
        _binding_downstream_expected_state_diff(binding),
    ):
        refs.extend(_text(ref) for ref in state_diff)
    for ref in refs:
        normalized = re.sub(r"[^a-z0-9]+", "_", _text(ref).lower()).strip("_")
        if "inline_activation" in normalized or "inlineactivation" in normalized:
            return _text(ref)
    return ""


def _binding_requires_route_navigation(binding: Mapping[str, Any]) -> bool:
    effect = _binding_effect(binding)
    target = _binding_target(binding)
    source = _text(binding.get("source_filename"))
    if effect in {"same_page_mutation", "query_projection"}:
        return False
    if not target:
        return False
    if source and source == target:
        return False
    return True


def _binding_runtime_state_diff(binding: Mapping[str, Any]) -> Dict[str, Any]:
    state_diff = dict(_binding_state_diff(binding))
    effect = _binding_effect(binding)
    target = _binding_target(binding)
    if target and _binding_requires_route_navigation(binding):
        state_diff.setdefault("target_filename", target)

    payload = _binding_payload(binding)
    state_reads = _as_list(payload.get("state_reads"))
    if not state_reads and effect == "stateful_submit":
        state_reads = _as_list(payload.get("state_refs"))
    state_reads = [_text(ref) for ref in state_reads if _text(ref)]
    if state_reads:
        state_diff.setdefault("state_reads", state_reads)
    return state_diff


def _binding_runtime_source(binding: Mapping[str, Any]) -> str:
    event = _binding_event(binding)
    effect = _binding_effect(binding)
    if event in {"fill", "type", "input"} or effect == "query_projection":
        return "dom_form"
    return "page_handler"


def _binding_visible_window_transition(binding: Mapping[str, Any]) -> Dict[str, Any]:
    if not _is_stateful_gesture_binding(binding):
        return {}
    state_refs = set(_binding_state_refs(binding))
    for ref, raw_value in _binding_state_diff(binding).items():
        value = _as_dict(raw_value)
        if ref not in state_refs or value.get("effect") != "advance_visible_window":
            continue
        axis = _text(value.get("axis")) or _text(_binding_payload(binding).get("gesture_axis"))
        direction = _text(value.get("direction")) or _text(
            _binding_payload(binding).get("gesture_direction")
        )
        reveals = [_text(item) for item in _as_list(value.get("reveals")) if _text(item)]
        transition: Dict[str, Any] = {
            "state_ref": _text(ref),
            "effect": "advance_visible_window",
        }
        if axis:
            transition["axis"] = axis
        if direction:
            transition["direction"] = direction
        if reveals:
            transition["reveals"] = reveals
        return transition
    return {}


def _is_stateful_gesture_binding(binding: Mapping[str, Any]) -> bool:
    event = _binding_event(binding)
    payload = _binding_payload(binding)
    gesture_scope = _text(payload.get("gesture_scope"))
    return event in _GESTURE_EVENTS and bool(
        gesture_scope and gesture_scope != "viewport_scroll" and _binding_state_refs(binding)
    )


def _binding_time_range_label(binding: Mapping[str, Any]) -> str:
    for label in _binding_labels(binding):
        normalized = _compact_text(label)
        if re.search(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", normalized):
            return normalized
    return ""


def _semantic_option_attrs(attrs: str, binding: Mapping[str, Any]) -> str:
    time_label = _binding_time_range_label(binding)
    if not time_label:
        return attrs
    semantic_context = " ".join(_binding_state_refs(binding) + [_binding_field_role(binding), _binding_intent_text(binding)])
    semantic_context = semantic_context.lower()
    if not any(token in semantic_context for token in ("time", "departure_time", "arrival_time", "picker", "时间")):
        return attrs
    attrs = _set_attr(attrs, "data-filter-option", "time")
    attrs = _set_attr(attrs, "data-filter-value", time_label)
    return attrs


def _binding_field_role(binding: Mapping[str, Any]) -> str:
    return _text(_binding_payload(binding).get("field_role"))


def _is_click_host_candidate(tag: str, attrs: str) -> bool:
    tag_lower = (tag or "").lower()
    if tag_lower in {"button", "a"}:
        return True
    attrs_lower = (attrs or "").lower()
    return any(
        token in attrs_lower
        for token in (
            "onclick",
            'role="button"',
            "role='button'",
            'role="tab"',
            "role='tab'",
            "data-ghostweb-action",
            "data-ghostweb-target",
            "data-ghostweb-route",
            "cursor-pointer",
        )
    )


def _allows_static_label_click_host(
    tag: str,
    attrs: str,
    body: str,
    labels: Iterable[str],
    binding: Mapping[str, Any],
) -> bool:
    tag_lower = (tag or "").lower()
    if tag_lower not in {"span", "div"}:
        return False
    if not _binding_target(binding):
        return False
    effect = _binding_effect(binding)
    if effect not in {"route", "activation", "select_result_return", "save_return", "same_page_mutation"}:
        return False
    text = _visible_text(body)
    if not text or len(text) > 80:
        return False
    if re.search(r"<\s*(?:button|a)\b", body or "", flags=re.IGNORECASE):
        return False
    if not _host_satisfies_required_visible_label(binding, attrs, body):
        return False
    label_matches = [label for label in labels if _label_matches_haystack(label, text)]
    if not label_matches:
        return False
    if tag_lower == "span" and any(text == label or label == text for label in label_matches):
        return True
    attrs_lower = (attrs or "").lower()
    if any(token in attrs_lower for token in ("card", "item", "row", "cell", "date", "day", "order", "entry")):
        return True
    return len(text) <= 24


def _field_role_host_score(field_role: str, target: str, attrs: str, body: str) -> int:
    field_role = (field_role or "").lower()
    target = (target or "").lower()
    attrs_lower = (attrs or "").lower()
    text = _visible_text(body)
    score = 0
    if _is_origin_role(field_role):
        if any(token in attrs_lower for token in ("departure", "origin", "from", "start")):
            score += 120
        if any(token in text for token in ("出发", "始发", "起点")):
            score += 60
    elif _is_destination_role(field_role):
        if any(token in attrs_lower for token in ("arrival", "destination", "dest", "to", "end")):
            score += 120
        if any(token in text for token in ("到达", "目的", "终点")):
            score += 60
    elif field_role == "date_or_time":
        if any(token in attrs_lower for token in ("date", "calendar", "time")):
            score += 120
        if any(token in text for token in ("日期", "时间", "周", "月", "日")):
            score += 60
        if any(token in target for token in ("calendar", "date", "picker")):
            score += 30
    return score


def _is_origin_role(field_role: str) -> bool:
    return field_role in {"origin", "from", "source", "origin_station"} or field_role.endswith("_origin")


def _is_destination_role(field_role: str) -> bool:
    return field_role in {"destination", "to", "target", "destination_station"} or field_role.endswith("_destination")


def _card_primary_host_score(tag: str, attrs: str, body: str, target: str) -> int:
    score = 0
    tag_lower = (tag or "").lower()
    attrs_lower = (attrs or "").lower()
    text = _visible_text(body)
    if tag_lower in {"button", "a"}:
        score += 80
    if any(token in attrs_lower for token in ("detail", "open", "record", "order", "item")):
        score += 60
    if any(token in text for token in ("详情", "打开", "查看")):
        score += 30
    if target and target in attrs:
        score += 100
    if "noninteractive" in attrs_lower:
        score -= 120
    return score


def _command_attr_host_score(binding: Mapping[str, Any], tag: str, attrs: str) -> int:
    tag_lower = (tag or "").lower()
    if tag_lower not in {"button", "a", "input"}:
        return 0
    labels = _matching_labels(binding, _binding_labels(binding))
    command_labels = [_compact_text(label).lower() for label in labels if _is_command_label(label)]
    if not command_labels:
        return 0


    semantic_attrs = " ".join(
        _attr_value(attrs, name)
        for name in (
            "id",
            "name",
            "class",
            "type",
            "role",
            "aria-label",
            "title",
            "value",
            "data-label",
        )
    )
    attrs_lower = html.unescape(semantic_attrs).lower()
    intent = html.unescape(_binding_intent_text(binding)).lower()
    surface_tokens = ("弹窗", "对话框", "浮层", "底部面板", "modal", "dialog", "sheet", "drawer", "overlay")
    if any(token in intent for token in surface_tokens) and not any(token in attrs_lower for token in surface_tokens):
        return 0
    score = 0
    submit_tokens = ("submit", "commit", "save", "confirm", "done", "ok")
    if any(token in attrs_lower for token in submit_tokens):
        score += 520
    for attr_name in ("aria-label", "title", "value", "data-label"):
        value = _compact_text(_attr_value(attrs, attr_name)).lower()
        if value and value in command_labels:
            score += 620
    if _attr_value(attrs, "disabled") or "noninteractive" in attrs_lower:
        score -= 300
    score += _surface_context_host_score(binding, attrs, "")
    return score


def _surface_context_host_score(binding: Mapping[str, Any], attrs: str, body: str) -> int:
    intent = _binding_intent_text(binding).lower()
    if not intent:
        return 0
    haystack = (html.unescape(attrs or "") + " " + _visible_text(body)).lower()
    score = 0

    def has_any(text: str, tokens: Iterable[str]) -> bool:
        return any(token and token.lower() in text for token in tokens)

    surface_rules = [
        (
            ("筛选", "过滤", "价格", "filter"),
            ("filter", "screen", "panel", "price", "筛选", "过滤", "价格"),
            ("date", "calendar", "time", "sort", "排序"),
        ),
        (
            ("日期", "日历", "时间", "date", "calendar"),
            ("date", "calendar", "time", "day", "日期", "日历", "时间"),
            ("filter", "screen", "price", "sort", "排序"),
        ),
        (
            ("排序", "sort"),
            ("sort", "order", "排序"),
            ("filter", "screen", "date", "calendar", "price"),
        ),
        (
            ("删除", "移除", "delete", "remove", "trash"),
            ("delete", "remove", "trash", "删除", "移除", "confirm", "ok"),
            ("date", "calendar", "filter", "sort"),
        ),
    ]
    for intent_tokens, positive_tokens, negative_tokens in surface_rules:
        if not has_any(intent, intent_tokens):
            continue
        if has_any(haystack, positive_tokens):
            score += 560
        if has_any(haystack, negative_tokens):
            score -= 420
    return score


def _intent_keyword_host_score(binding: Mapping[str, Any], attrs: str, body: str) -> int:
    intent = _binding_intent_text(binding)
    if not intent:
        return 0
    haystack = _visible_text(body) + " " + attrs
    score = 0
    keyword_groups = [
        ("分享", ("分享", "share")),
        ("保存", ("保存", "保存图片", "save")),
        ("选择", ("选择", "模板", "select", "choose")),
        ("完成", ("完成", "确定", "确认", "done", "confirm")),
        ("展开", ("展开", "更多", "expand", "more")),
        ("勾选", ("勾选", "同意", "协议", "checkbox", "consent")),
        ("同意", ("同意", "协议", "checkbox", "consent")),
        ("协议", ("同意", "协议", "checkbox", "consent")),
    ]
    for intent_token, host_tokens in keyword_groups:
        if intent_token not in intent:
            continue
        if any(_label_matches_haystack(token, haystack) for token in host_tokens):
            score += 180
    return score


def _quantity_direction_host_score(binding: Mapping[str, Any], attrs: str, body: str) -> int:
    intent = html.unescape(_binding_intent_text(binding)).lower()
    haystack = html.unescape(
        " ".join(
            (
                attrs or "",
                _visible_text(body),
                _attr_value(attrs, "aria-label"),
                _attr_value(attrs, "title"),
            )
        )
    ).lower()

    increment_intent = any(
        token in intent
        for token in ("加号", "增加数量", "数量增加", "increment", "plus button")
    )
    decrement_intent = any(
        token in intent
        for token in ("减号", "减少数量", "数量减少", "decrement", "minus button")
    )
    if increment_intent == decrement_intent:
        return 0

    increment_host = any(
        token in haystack
        for token in (
            "增加",
            "加号",
            "increment",
            "data-lucide=\"plus\"",
            "data-lucide='plus'",
            "btn-add",
        )
    )
    decrement_host = any(
        token in haystack
        for token in (
            "减少",
            "减号",
            "decrement",
            "data-lucide=\"minus\"",
            "data-lucide='minus'",
            "btn-minus",
        )
    )
    if increment_intent:
        if decrement_host and not increment_host:
            return -2000
        if increment_host and not decrement_host:
            return 900
    if decrement_intent:
        if increment_host and not decrement_host:
            return -2000
        if decrement_host and not increment_host:
            return 900
    return 0


def _contract_label_score(binding: Mapping[str, Any], attrs: str, body: str) -> int:
    labels = _matching_labels(binding, _binding_labels(binding))
    if not labels:
        return 0
    if not _host_satisfies_required_visible_label(binding, attrs, body):
        return 0
    text = _visible_text(body)
    haystack = text + " " + attrs
    compact_haystack = _compact_text(haystack)
    score = 0
    generic_parts = {"火车站", "车站", "站点", "城市", "市", "站", "入口", "按钮"}
    for label in labels:
        if _label_matches_host(label, text, attrs):
            score += 420
            continue
        parts = [part for part in re.split(r"[\s,/，、:：|]+", html.unescape(label or "")) if part]
        meaningful_parts = [
            _compact_text(part)
            for part in parts
            if len(_compact_text(part)) >= 2 and _compact_text(part) not in generic_parts
        ]
        if meaningful_parts and any(part in compact_haystack for part in meaningful_parts):
            score += 220
    if not score:
        return 0
    tag_hint = attrs.lower()
    if "noninteractive" in tag_hint:
        score -= 160
    score += _field_role_host_score(_binding_field_role(binding), _binding_target(binding), attrs, body)
    score += _intent_keyword_host_score(binding, attrs, body)
    score += _quantity_direction_host_score(binding, attrs, body)
    score += _surface_context_host_score(binding, attrs, body)
    return score


def _labeled_card_primary_host(
    html_text: str,
    labels: Iterable[str],
    binding: Mapping[str, Any],
) -> Optional[Tuple[int, int, str, str, str, int]]:
    effect = _binding_effect(binding)
    source = _text(binding.get("source_filename"))
    target = _binding_target(binding)
    if effect not in {"route", "activation", "select_result_return", "same_page_mutation"} or not target:
        return None
    if source == target and effect != "same_page_mutation":
        return None
    best: Optional[Tuple[int, int, str, str, str, int]] = None
    match_labels = _matching_labels(binding, labels)
    for container in _CARD_CONTAINER_PAIR_RE.finditer(html_text):
        container_text = _visible_text(container.group("body"))
        if not any(_label_matches_haystack(label, container_text) for label in match_labels):
            continue
        body_start = container.start("body")
        for host in _PRIMARY_ACTIONABLE_PAIR_RE.finditer(container.group("body")):
            tag = host.group("tag")
            attrs = host.group("attrs")
            if not _is_click_host_candidate(tag, attrs):
                continue
            score = (
                150
                + _card_primary_host_score(tag, attrs, host.group("body"), target)
                + _intent_keyword_host_score(binding, attrs, host.group("body"))
            )
            start = body_start + host.start()
            end = body_start + host.end()
            if best is None or score > best[5]:
                best = (start, end, tag, attrs, host.group("body"), score)
    return best


def _open_query_containers(html_text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        tag = match.group("tag")
        attrs = match.group("attrs")
        attrs_lower = attrs.lower()
        if not any(token in attrs_lower for token in ("search", "query", "autocomplete", "suggest")):
            return match.group(0)
        if "hidden" not in attrs_lower:
            return match.group(0)
        attrs = _remove_attr(attrs, "hidden")
        class_value = _attr_value(attrs, "class")
        if class_value:
            classes = [cls for cls in class_value.split() if cls != "hidden"]
            attrs = _set_attr(attrs, "class", " ".join(classes))
        return f"<{tag}{attrs}>"

    return _OPEN_TAG_RE.sub(repl, html_text)


def _materialize_query_binding(html_text: str, binding: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    html_text = _normalize_malformed_input_open_tags(html_text)
    action_id = _binding_action_id(binding)
    report = {
        "action_id": action_id,
        "source_filename": _text(binding.get("source_filename")),
        "effect_kind": _binding_effect(binding),
        "event": _binding_event(binding),
        "status": "host_not_found",
        "reason": "",
    }
    if not action_id:
        report["reason"] = "missing action_id"
        return html_text, report

    html_text = _remove_existing_materialized_query_input(html_text)
    html_text = _strip_action_id_from_other_hosts(html_text, action_id)
    matched = False

    def repl(match: re.Match[str]) -> str:
        nonlocal matched
        tag = match.group("tag")
        attrs = match.group("attrs")
        if matched or action_id not in _action_tokens_from_attrs(attrs):
            return match.group(0)
        attrs = _remove_noninteractive(attrs)
        attrs = _make_fill_input_visible(attrs)
        attrs = _set_canonical_action_id(attrs, action_id)
        attrs = _set_attr(attrs, "role", "searchbox")
        attrs = _set_attr(attrs, "data-ghostweb-query-input", "true")
        attrs = _set_attr(attrs, "data-ghostweb-effect-kind", "query_projection")
        attrs = _set_attr(attrs, "data-ghostweb-dom-binding-id", _text(binding.get("dom_binding_id")))
        if tag.lower() == "input" and not _attr_value(attrs, "type"):
            attrs = _set_attr(attrs, "type", "search")
        matched = True
        report["status"] = "bound"
        return f"<{tag}{attrs}>"

    html_text = _INPUT_TAG_RE.sub(repl, html_text)
    if matched:
        return html_text, report

    input_matches = list(_INPUT_TAG_RE.finditer(html_text))
    best: Optional[Tuple[int, int, str, str, int]] = None
    event = _binding_event(binding)
    labels = _binding_labels(binding)
    numeric_labels = [label for label in labels if re.fullmatch(r"[+\d][\d\s-]{5,}", _compact_text(label))]
    for match in input_matches:
        tag = _text(match.group("tag") or match.group("input_tag") or match.group("ce_tag"))
        attrs = _text(match.group("attrs") or match.group("input_attrs") or match.group("ce_attrs"))
        attrs_l = attrs.lower()
        context = _local_action_context(html_text, match.start(), match.end())
        context_text = _visible_text(context)
        placeholder = _attr_value(attrs, "placeholder")
        score = 0
        if 'role="searchbox"' in attrs_l or "role='searchbox'" in attrs_l:
            score += 180
        if 'type="search"' in attrs_l or "type='search'" in attrs_l:
            score += 160
        if any(token in attrs_l for token in ("search", "query", "keyword", "station-search", "搜索", "输入")):
            score += 120
        if event in {"fill", "type", "input"}:
            if len(input_matches) == 1:
                score += 120
            if any(token in attrs_l for token in ("tel", "phone", "mobile", "numeric", "number")):
                score += 120
            if any(token in context_text for token in ("手机号", "手机号码", "联系电话", "电话")):
                score += 140
            if any(token in placeholder for token in ("手机号", "手机号码", "电话")):
                score += 140
            if numeric_labels and any(token in attrs_l for token in ("tel", "numeric", "number")):
                score += 160
        for label in labels:
            if _label_matches_haystack(label, attrs + " " + placeholder + " " + context_text):
                score += 80
        if _action_tokens_from_attrs(attrs):
            score += 60
        if score <= 0:
            continue
        if best is None or score > best[4]:
            best = (match.start(), match.end(), tag, attrs, score)
    if best is not None:
        start, end, tag, attrs, _score = best
        attrs = _remove_noninteractive(attrs)
        attrs = _make_fill_input_visible(attrs)
        attrs = _set_canonical_action_id(attrs, action_id)
        attrs = _set_attr(attrs, "role", "searchbox")
        attrs = _set_attr(attrs, "data-ghostweb-query-input", "true")
        attrs = _set_attr(attrs, "data-ghostweb-effect-kind", "query_projection")
        attrs = _set_attr(attrs, "data-ghostweb-dom-binding-id", _text(binding.get("dom_binding_id")))
        if tag.lower() == "input" and not _attr_value(attrs, "type"):
            attrs = _set_attr(attrs, "type", "search")
        html_text = html_text[:start] + f"<{tag}{attrs}>" + html_text[end:]
        report["status"] = "bound"
        report["reason"] = "bound_existing_searchbox"
        return html_text, report

    report["reason"] = "missing canonical data-action-id host"
    return html_text, report


def _action_attrs(attrs: str, binding: Mapping[str, Any]) -> str:
    action_id = _binding_action_id(binding)
    target = _binding_target(binding)
    payload = _binding_payload(binding)
    effect_kind = _text(payload.get("effect_kind"))
    source = _text(binding.get("source_filename"))
    same_page_mutation = effect_kind == "same_page_mutation" and bool(source and target and source == target)
    same_page_route = effect_kind in {"route", "navigation"} and bool(source and target and source == target)
    existing_target = _attr_value(attrs, "data-ghostweb-target") or _attr_value(attrs, "href")
    target_for_action = _target_for_binding_host(attrs, binding)
    requires_route_navigation = _binding_requires_route_navigation(binding)
    existing_action_ids = _action_tokens_from_attrs(attrs)
    target_map = _read_target_map(attrs)
    if existing_target:
        for existing_action_id in existing_action_ids:
            target_map.setdefault(existing_action_id, existing_target)
    if action_id and target_for_action:
        target_map[action_id] = target_for_action
    attrs = _remove_noninteractive(attrs)
    attrs = _make_action_host_visible(attrs)
    if not _is_stateful_gesture_binding(binding) and _attr_value(attrs, "data-ghostweb-materialized") == "action-effect-host":
        if _attr_value(attrs, "role") == "group":
            attrs = _remove_attr(attrs, "role")
    attrs = _set_canonical_action_id(attrs, action_id)
    attrs = _sync_component_ref_attrs(attrs, binding)
    attrs = _sync_action_aliases(attrs, list(target_map.keys()) + existing_action_ids + [action_id])
    primary_action_id = _attr_value(attrs, "data-action-id")
    is_primary_action = bool(action_id and primary_action_id == action_id)
    if not is_primary_action:
        if target_map:
            attrs = _write_target_map(attrs, target_map)
        return _apply_visibility_precondition_attrs(attrs, binding)
    if target_for_action and requires_route_navigation:
        attrs = _set_attr(attrs, "data-ghostweb-action", "navigate")
        attrs = _set_attr(attrs, "data-ghostweb-target", target_for_action)
        if _attr_value(attrs, "href"):
            attrs = _set_attr(attrs, "href", target_for_action)
        if _attr_value(attrs, "onclick") and _attr_value(attrs, "onpointerdown"):
            attrs = _remove_attr(attrs, "onclick")
        attrs = _write_target_map(attrs, target_map)
    elif not requires_route_navigation:
        attrs = _remove_attr(attrs, "data-ghostweb-action")
        attrs = _remove_attr(attrs, "data-ghostweb-target")
        if target_map:
            attrs = _write_target_map(attrs, target_map)
    attrs = _semantic_option_attrs(attrs, binding)
    attrs = _merge_context_into_accessible_label(attrs, binding)
    attrs = _set_attr(attrs, "data-ghostweb-effect-kind", effect_kind)
    attrs = _set_attr(attrs, "data-ghostweb-transaction-id", _text(payload.get("transaction_id")))
    attrs = _set_attr(attrs, "data-ghostweb-dom-binding-id", _text(binding.get("dom_binding_id")))
    if _is_stateful_gesture_binding(binding):
        state_refs = _binding_state_refs(binding)
        attrs = _set_attr(attrs, "data-gesture-host", _text(payload.get("gesture_scope")) or "component_gesture")
        attrs = _set_attr(attrs, "data-gesture-axis", _text(payload.get("gesture_axis")) or "horizontal")
        attrs = _set_attr(attrs, "data-gesture-direction", _text(payload.get("gesture_direction")) or "forward")
        attrs = _set_attr(attrs, "data-state-ref", state_refs[0] if state_refs else "")
        attrs = _set_attr(attrs, "data-ghostweb-gesture-action", _binding_event(binding) or "swipe")
        attrs = _set_attr(attrs, "tabindex", _attr_value(attrs, "tabindex") or "0")
        style = _attr_value(attrs, "style")
        if "touch-action" not in style:
            style = (style.rstrip("; ") + "; touch-action: pan-y;").lstrip("; ")
        attrs = _set_attr(attrs, "style", style)
    return _apply_visibility_precondition_attrs(attrs, binding)


def _merge_context_into_accessible_label(attrs: str, binding: Mapping[str, Any]) -> str:
    context_labels = _binding_context_labels(binding)
    if not context_labels:
        return attrs
    accessible_label = next(
        (label for label in _binding_accessible_labels(binding) if label),
        "",
    )
    canonical_context = [
        label
        for label in [accessible_label, *context_labels]
        if label
    ]
    values: List[str] = []
    existing = _text(_attr_value(attrs, "aria-label"))
    canonical_suffix = " ".join(canonical_context)
    duplicate_suffix = f"{canonical_suffix} {canonical_suffix}"
    while canonical_suffix and duplicate_suffix in existing:
        existing = existing.replace(duplicate_suffix, canonical_suffix)
    if existing:
        values.append(existing)

    def already_in_label(label: str) -> bool:
        needle = _compact_text(label).casefold()
        return bool(needle) and any(
            needle in _compact_text(value).casefold()
            for value in values
        )

    if accessible_label and not already_in_label(accessible_label):
        values.append(accessible_label)
    for label in context_labels:
        if label and not already_in_label(label):
            values.append(label)
    if not values:
        return attrs
    return _set_attr(attrs, "aria-label", " ".join(values))


def _neutralize_same_page_action_host(
    html_text: str,
    binding: Mapping[str, Any],
    report: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    action_id = _binding_action_id(binding)
    source = _text(binding.get("source_filename"))
    target = _binding_target(binding)
    if _binding_effect(binding) != "same_page_mutation" or not action_id or not source or source != target:
        return html_text, report

    matched = False

    def repl(match: re.Match[str]) -> str:
        nonlocal matched
        tag = match.group("tag")
        attrs = match.group("attrs")
        if action_id not in attrs:
            return match.group(0)
        attrs = _remove_noninteractive(attrs)
        attrs = _make_action_host_visible(attrs)
        attrs = _remove_attr(attrs, "data-ghostweb-action")
        attrs = _remove_attr(attrs, "data-ghostweb-target")
        attrs = _remove_attr(attrs, "data-ghostweb-target-map")
        attrs = _set_attr(attrs, "data-ghostweb-effect-kind", "same_page_mutation")
        attrs = _set_attr(attrs, "data-ghostweb-transaction-id", _text(_binding_payload(binding).get("transaction_id")))
        attrs = _set_attr(attrs, "data-ghostweb-dom-binding-id", _text(binding.get("dom_binding_id")))
        matched = True
        return f"<{tag}{attrs}>"

    html_text = _OPEN_TAG_RE.sub(repl, html_text)
    if matched:
        report["status"] = "bound"
        report["reason"] = "neutralized same-page action host"
    return html_text, report


def _remove_existing_materialized_action_host(html_text: str, action_id: str = "") -> str:
    def repl(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        if _attr_value(attrs, "data-ghostweb-materialized") != "action-effect-host":
            return match.group(0)
        return ""

    return _ACTIONABLE_PAIR_RE.sub(repl, html_text)


def _remove_existing_materialized_query_input(html_text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        if _attr_value(attrs, "data-ghostweb-materialized") != "query-input-host":
            return match.group(0)
        return ""

    return _INPUT_TAG_RE.sub(repl, html_text)


def _target_for_binding_host(attrs: str, binding: Mapping[str, Any]) -> str:
    target = _binding_target(binding)
    effect_kind = _binding_effect(binding)
    source = _text(binding.get("source_filename"))
    dom_target = _attr_value(attrs, "data-ghostweb-target")
    href_target = _attr_value(attrs, "href")
    if effect_kind in {"route", "navigation"} and source and target and target == source and not (dom_target or href_target):
        return ""
    if (
        effect_kind in {"route", "navigation"}
        and source
        and target
        and target == source
        and (dom_target or href_target)
    ):
        return dom_target or href_target
    return target


def _add_hidden_until_transition_attrs(attrs: str) -> str:
    attrs = _set_attr(attrs, "data-ghostweb-initial-visibility", "hidden_until_transition")
    attrs = _set_attr(attrs, "aria-hidden", "true")
    style = _attr_value(attrs, "style")
    declarations = [part.strip() for part in style.split(";") if part.strip()]
    if not any(part.split(":", 1)[0].strip().lower() == "display" for part in declarations if ":" in part):
        declarations.append("display: none")
    attrs = _set_attr(attrs, "style", "; ".join(declarations))
    return attrs


def _apply_visibility_precondition_attrs(attrs: str, binding: Mapping[str, Any]) -> str:
    payload = _binding_payload(binding)
    precondition = _binding_visibility_precondition(binding)
    producer_action_id = _text(
        payload.get("visibility_precondition_action_id")
        or precondition.get("producer_action_id")
    )
    if not producer_action_id:
        return attrs
    action_id = _binding_action_id(binding)
    state_ref = _text(precondition.get("state_ref"))
    required_value = _text(payload.get("required_visible_value") or precondition.get("required_visible_value"))
    precondition_map = _read_visibility_precondition_map(attrs)
    if action_id:
        precondition_map[action_id] = {
            "producer_action_id": producer_action_id,
            "state_ref": state_ref,
            "required_visible_value": required_value,
        }
        attrs = _write_visibility_precondition_map(attrs, precondition_map)


    primary_action_id = _attr_value(attrs, "data-action-id")
    if not _attr_value(attrs, "data-ghostweb-visibility-producer-action-id") or primary_action_id == action_id:
        attrs = _set_attr(attrs, "data-ghostweb-visibility-producer-action-id", producer_action_id)
        if state_ref:
            attrs = _set_attr(attrs, "data-ghostweb-visibility-state-ref", state_ref)
        if required_value:
            attrs = _set_attr(attrs, "data-ghostweb-required-visible-value", required_value)
    return _add_hidden_until_transition_attrs(attrs)


def _cleanup_orphan_visibility_preconditions(html_text: str) -> str:

    def repl(match: re.Match[str]) -> str:
        tag = match.group("tag")
        attrs = match.group("attrs")
        if _attr_value(attrs, "data-ghostweb-initial-visibility") != "hidden_until_transition":
            return match.group(0)
        if _action_tokens_from_attrs(attrs):
            return match.group(0)


        if (
            _attr_value(attrs, "role").lower() == "dialog"
            or _attr_value(attrs, "aria-modal").lower() == "true"
            or bool(_attr_value(attrs, "data-ghostweb-overlay-layer-role"))
        ):
            return match.group(0)
        if _attr_value(attrs, "data-ghostweb-materialized") == "initial-hidden-state-container":
            return match.group(0)
        for name in (
            "data-ghostweb-initial-visibility",
            "data-ghostweb-visibility-producer-action-id",
            "data-ghostweb-visibility-state-ref",
            "data-ghostweb-required-visible-value",
            "data-ghostweb-visibility-precondition-map",
            "data-ghostweb-precondition-satisfied",
            "aria-hidden",
        ):
            attrs = _remove_attr(attrs, name)
        attrs = _make_action_host_visible(attrs)
        return f"<{tag}{attrs}>"

    return _OPEN_TAG_RE.sub(repl, html_text)


def _reconcile_action_target_maps(
    html_text: str,
    bindings: Iterable[Mapping[str, Any]],
) -> str:
    bindings_by_action = {
        _binding_action_id(binding): binding
        for binding in bindings
        if _binding_action_id(binding)
    }
    if not bindings_by_action:
        return html_text

    def repl(match: re.Match[str]) -> str:
        tag = match.group("tag")
        attrs = match.group("attrs")
        action_ids = _action_tokens_from_attrs(attrs)
        if not action_ids:
            return match.group(0)
        target_map = _read_target_map(attrs)
        changed = False
        for action_id in action_ids:
            binding = bindings_by_action.get(action_id)
            if not binding:
                continue
            if _binding_effect(binding) == "same_page_mutation":
                continue
            target = _target_for_binding_host(attrs, binding)
            if target and target_map.get(action_id) != target:
                target_map[action_id] = target
                changed = True
        if not changed:
            return match.group(0)
        attrs = _write_target_map(attrs, target_map)
        return f"<{tag}{attrs}>"

    return _OPEN_TAG_RE.sub(repl, html_text)


def reconcile_static_route_target_aliases(
    html_text: str,
    bindings: Iterable[Mapping[str, Any]],
) -> str:
    bindings_by_action = {
        _binding_action_id(binding): binding
        for binding in bindings
        if _binding_action_id(binding)
    }
    if not bindings_by_action:
        return html_text

    def target_filename(value: str) -> str:
        return _text(value).split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1]

    def repl(match: re.Match[str]) -> str:
        tag = match.group("tag")
        attrs = match.group("attrs")
        static_target = _attr_value(attrs, "data-ghostweb-target") or _attr_value(attrs, "href")
        static_filename = target_filename(static_target)
        if not static_filename:
            return match.group(0)
        stale_ids = []
        for action_id in _action_tokens_from_attrs(attrs):
            binding = bindings_by_action.get(action_id)
            if not binding or _binding_effect(binding) == "same_page_mutation":
                continue
            binding_filename = target_filename(_binding_target(binding))
            if binding_filename and binding_filename != static_filename:
                stale_ids.append(action_id)
        if not stale_ids:
            return match.group(0)
        for action_id in stale_ids:


            attrs = _remove_action_id_from_attr(attrs, "data-action-aliases", action_id)
            attrs = _remove_action_id_from_attr(attrs, "data-element-aliases", action_id, remove_embedded=False)
            attrs = _remove_action_id_from_target_map(attrs, action_id)
        return f"<{tag}{attrs}>"

    return _OPEN_TAG_RE.sub(repl, html_text)


def _gesture_host_score(binding: Mapping[str, Any], attrs: str, body: str) -> int:
    payload = _binding_payload(binding)
    labels = _matching_labels(binding, _binding_labels(binding))
    text = _visible_text(body)
    haystack = (text + " " + attrs).lower()
    score = 0
    if _attr_value(attrs, "data-ghostweb-materialized") == "action-effect-host":
        return 0
    if _attr_value(attrs, "data-ghostweb-initial-visibility") == "hidden_until_transition":
        return 0
    if _attr_value(attrs, "data-ghostweb-visibility-producer-action-id"):
        return 0
    existing_gesture_host = bool(_attr_value(attrs, "data-gesture-host"))
    existing_route_host = bool(
        _attr_value(attrs, "href")
        or _attr_value(attrs, "data-ghostweb-target")
        or _attr_value(attrs, "data-ghostweb-action") == "navigate"
        or re.search(r"(?:navigate|location\s*=|location\.href)", _attr_value(attrs, "onclick"), re.IGNORECASE)
    )
    if existing_route_host and not existing_gesture_host:
        return 0
    action_id = _binding_action_id(binding)


    if _component_identity_host_matches(attrs, action_id):
        score += 2000
    state_refs = _binding_state_refs(binding)
    if state_refs and _attr_value(attrs, "data-state-ref") in state_refs:
        score += 520
    if any(token in haystack for token in ("category", "shortcut", "carousel", "grid", "tab", "分类", "快捷", "轮播")):
        score += 260
    component_ref = _text(payload.get("component_ref")).lower()
    if component_ref and any(token in component_ref for token in ("category", "shortcut", "carousel")):
        score += 120
    matched_labels = [label for label in labels if _label_matches_haystack(label, text)]
    if matched_labels:
        score += 80 * len(matched_labels)
    if len(text) > 12 and sum(1 for label in labels if label and label in text) >= 1:
        score += 60
    if "noninteractive" in attrs.lower():
        score -= 300
    return score


def _materialize_gesture_binding(html_text: str, binding: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    action_id = _binding_action_id(binding)
    report = {
        "action_id": action_id,
        "source_filename": _text(binding.get("source_filename")),
        "effect_kind": _binding_effect(binding),
        "event": _binding_event(binding),
        "target_filename": _binding_target(binding),
        "labels": _binding_labels(binding),
        "status": "host_not_found",
        "reason": "",
    }
    if not action_id:
        report["reason"] = "missing action_id"
        return html_text, report
    component_matches = [
        match
        for match in _OPEN_TAG_RE.finditer(html_text)
        if _component_identity_host_matches(match.group("attrs"), action_id)
        and _attr_value(match.group("attrs"), "data-ghostweb-initial-visibility") != "hidden_until_transition"
        and not _attr_value(match.group("attrs"), "data-ghostweb-visibility-producer-action-id")
    ]
    if len(component_matches) == 1:
        html_text = _strip_action_id_from_other_hosts(
            html_text,
            action_id,
            keep_start=component_matches[0].start(),
        )
        rebound = next(
            (
                candidate
                for candidate in _OPEN_TAG_RE.finditer(html_text)
                if _component_identity_host_matches(candidate.group("attrs"), action_id)
            ),
            None,
        )
        if rebound is not None:
            tag = rebound.group("tag")
            attrs = _action_attrs(rebound.group("attrs"), binding)
            attrs = _set_attr(attrs, "role", _attr_value(attrs, "role") or "group")
            html_text = html_text[:rebound.start()] + f"<{tag}{attrs}>" + html_text[rebound.end():]
            report["status"] = "bound"
            report["reason"] = "bound_by_unique_component_identity"
            return html_text, report
    component_ref_matches = _exact_component_ref_matches(
        html_text,
        binding,
        click_host=False,
    )
    if len(component_ref_matches) == 1:
        html_text = _strip_action_id_from_other_hosts(
            html_text,
            action_id,
            keep_start=component_ref_matches[0].start(),
        )
        rebound = next(
            (
                candidate
                for candidate in _OPEN_TAG_RE.finditer(html_text)
                if _component_ref_host_matches(candidate.group("attrs"), binding)
                and _component_ref_match_is_eligible(
                    html_text,
                    candidate,
                    click_host=False,
                )
            ),
            None,
        )
        if rebound is not None:
            tag = rebound.group("tag")
            attrs = _action_attrs(rebound.group("attrs"), binding)
            attrs = _set_attr(attrs, "role", _attr_value(attrs, "role") or "group")
            html_text = html_text[:rebound.start()] + f"<{tag}{attrs}>" + html_text[rebound.end():]
            report["status"] = "bound"
            report["reason"] = "bound_by_exact_component_ref"
            return html_text, report
    html_text = _strip_action_id_from_other_hosts(html_text, action_id)
    best: Optional[Tuple[int, int, str, str, str, int]] = None
    for pair_re in (_GESTURE_CONTAINER_PAIR_RE, _SECONDARY_ACTIONABLE_PAIR_RE, _PRIMARY_ACTIONABLE_PAIR_RE):
        for match in pair_re.finditer(html_text):
            score = _gesture_host_score(binding, match.group("attrs"), match.group("body"))
            if score <= 0:
                continue
            if best is None or score > best[5]:
                best = (match.start(), match.end(), match.group("tag"), match.group("attrs"), match.group("body"), score)
    if best is None:
        report["reason"] = "missing visible gesture component host"
        return html_text, report
    start, end, tag, attrs, body, _score = best
    attrs = _action_attrs(attrs, binding)
    attrs = _set_attr(attrs, "role", _attr_value(attrs, "role") or "group")
    html_text = html_text[:start] + f"<{tag}{attrs}>{body}</{tag}>" + html_text[end:]
    report["status"] = "bound"
    report["reason"] = "bound_stateful_gesture_component_host"
    return html_text, report


def _materialize_click_binding(html_text: str, binding: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    action_id = _binding_action_id(binding)
    report = {
        "action_id": action_id,
        "source_filename": _text(binding.get("source_filename")),
        "effect_kind": _binding_effect(binding),
        "event": _binding_event(binding),
        "target_filename": _binding_target(binding),
        "labels": _binding_labels(binding),
        "status": "host_not_found",
        "reason": "",
    }
    if not action_id:
        report["reason"] = "missing action_id"
        return html_text, report

    html_text = _remove_existing_materialized_action_host(html_text, action_id)

    component_ref_matches = _exact_component_ref_matches(
        html_text,
        binding,
        click_host=True,
    )
    if len(component_ref_matches) == 1:
        html_text = _strip_action_id_from_other_hosts(
            html_text,
            action_id,
            keep_start=component_ref_matches[0].start(),
        )
        rebound = next(
            (
                candidate
                for candidate in _OPEN_TAG_RE.finditer(html_text)
                if _component_ref_host_matches(candidate.group("attrs"), binding)
                and _component_ref_match_is_eligible(
                    html_text,
                    candidate,
                    click_host=True,
                )
            ),
            None,
        )
        if rebound is not None:
            tag = rebound.group("tag")
            attrs = _action_attrs(rebound.group("attrs"), binding)
            html_text = html_text[:rebound.start()] + f"<{tag}{attrs}>" + html_text[rebound.end():]
            report["status"] = "bound"
            report["reason"] = "bound_by_exact_component_ref"
            return html_text, report

    component_matches = [
        match
        for match in _OPEN_TAG_RE.finditer(html_text)
        if _component_identity_host_matches(match.group("attrs"), action_id)
    ]
    if len(component_matches) == 1:
        match = component_matches[0]
        labels = _binding_labels(binding)
        label_matches = _component_host_matches_visible_label(html_text, match, labels)
        if label_matches:
            html_text = _strip_action_id_from_other_hosts(html_text, action_id, keep_start=match.start())
            rebound = next(
                (
                    candidate
                    for candidate in _OPEN_TAG_RE.finditer(html_text)
                    if _component_identity_host_matches(candidate.group("attrs"), action_id)
                ),
                None,
            )
            if rebound is not None:
                tag = rebound.group("tag")
                attrs = _action_attrs(rebound.group("attrs"), binding)
                html_text = html_text[:rebound.start()] + f"<{tag}{attrs}>" + html_text[rebound.end():]
                report["status"] = "bound"
                report["reason"] = "bound_by_unique_component_identity"
                return html_text, report

    html_text = _strip_action_id_from_other_hosts(html_text, action_id)
    best_command_open: Optional[Tuple[int, int, str, str, int]] = None
    for match in _OPEN_TAG_RE.finditer(html_text):
        tag = match.group("tag")
        attrs = match.group("attrs")
        if not _host_satisfies_required_visible_label(binding, attrs, ""):
            continue
        score = _command_attr_host_score(binding, tag, attrs)
        score += _hidden_ancestor_penalty(html_text, match.start())
        if score <= 0:
            continue
        if best_command_open is None or score > best_command_open[4]:
            best_command_open = (match.start(), match.end(), tag, attrs, score)
    if best_command_open is not None:
        start, end, tag, attrs, _score = best_command_open
        attrs = _action_attrs(attrs, binding)
        html_text = html_text[:start] + f"<{tag}{attrs}>" + html_text[end:]
        report["status"] = "bound"
        report["reason"] = "bound_by_command_attr"
        return html_text, report

    for pair_re in (_PRIMARY_ACTIONABLE_PAIR_RE, _SECONDARY_ACTIONABLE_PAIR_RE):
        for match in pair_re.finditer(html_text):
            tag = match.group("tag")
            attrs = match.group("attrs")
            if action_id in _action_tokens_from_attrs(attrs):
                if not _host_satisfies_required_visible_label(binding, attrs, match.group("body")):
                    continue
                attrs = _action_attrs(attrs, binding)
                html_text = html_text[:match.start()] + f"<{tag}{attrs}>{match.group('body')}</{tag}>" + html_text[match.end():]
                report["status"] = "bound"
                return html_text, report

    for match in _OPEN_TAG_RE.finditer(html_text):
        tag = match.group("tag")
        attrs = match.group("attrs")
        if action_id not in _action_tokens_from_attrs(attrs):
            continue


        if not _is_click_host_candidate(tag, attrs):
            continue
        if not _host_satisfies_required_visible_label(binding, attrs, ""):
            continue
        attrs = _action_attrs(attrs, binding)
        html_text = html_text[:match.start()] + f"<{tag}{attrs}>" + html_text[match.end():]
        report["status"] = "bound"
        return html_text, report

    best: Optional[Tuple[int, int, str, str, str, int]] = None
    for pair_re in (_PRIMARY_ACTIONABLE_PAIR_RE, _SECONDARY_ACTIONABLE_PAIR_RE):
        for match in pair_re.finditer(html_text):
            tag = match.group("tag")
            attrs = match.group("attrs")
            body = match.group("body")
            if not (_is_click_host_candidate(tag, attrs) or _allows_static_label_click_host(tag, attrs, body, _binding_labels(binding), binding)):
                continue
            score = _contract_label_score(binding, attrs, body)
            score += _contextual_command_host_score(
                html_text,
                match.start(),
                attrs,
                body,
                binding,
            )
            score += _hidden_ancestor_penalty(html_text, match.start())
            if score <= 0:
                continue
            if best is None or score > best[5]:
                best = (match.start(), match.end(), tag, attrs, body, score)
    if best is not None:
        start, end, tag, attrs, body, _score = best
        attrs = _action_attrs(attrs, binding)
        html_text = html_text[:start] + f"<{tag}{attrs}>{body}</{tag}>" + html_text[end:]
        report["status"] = "bound"
        report["reason"] = "bound_by_text_contract"
        return html_text, report

    report["reason"] = "missing canonical data-action-id host"
    return html_text, report


def _unique_authoritative_click_host_start(
    html_text: str,
    binding: Mapping[str, Any],
) -> Optional[int]:

    component_ref_matches = _exact_component_ref_matches(
        html_text,
        binding,
        click_host=True,
    )
    if len(component_ref_matches) == 1:
        return component_ref_matches[0].start()

    action_id = _binding_action_id(binding)
    if not action_id:
        return None
    component_matches = [
        match
        for match in _OPEN_TAG_RE.finditer(html_text)
        if _component_identity_host_matches(match.group("attrs"), action_id)
    ]
    if len(component_matches) != 1:
        return None
    match = component_matches[0]
    labels = _binding_labels(binding)
    if not labels:
        return match.start()
    if not _component_host_matches_visible_label(html_text, match, labels):
        return None
    return match.start()


def reconcile_unique_action_identity_hosts(
    html_text: str,
    bindings: Iterable[Mapping[str, Any]],
) -> str:

    for binding in bindings:
        action_id = _binding_action_id(binding)
        if not action_id:
            continue
        keep_start = _unique_authoritative_click_host_start(html_text, binding)
        if keep_start is None:
            continue
        html_text = _strip_action_id_from_other_hosts(
            html_text,
            action_id,
            keep_start=keep_start,
        )
    return html_text


def _migrate_fill_action_to_input(html_text: str, binding: Mapping[str, Any]) -> Tuple[str, bool]:
    action_id = _binding_action_id(binding)
    event = _binding_event(binding)
    if not action_id or event not in {"fill", "type", "input"}:
        return html_text, False

    escaped_action_id = re.escape(action_id)

    pattern = re.compile(
        r'(?P<open><(?P<tag>[a-z][a-z0-9]*)\s+(?P<attrs>[^>]*?(?:data-action-id=["\']'
        + escaped_action_id
        + r'["\']|data-action-aliases=["\'][^"\']*?\b'
        + escaped_action_id
        + r'\b[^"\']*?["\'])[^>]*?)>)',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html_text)
    if not match:
        return html_text, False

    tag = match.group("tag").lower()

    if tag in {"input", "textarea", "select"}:
        return html_text, False
    attrs = match.group("attrs")
    if "contenteditable" in attrs.lower():
        return html_text, False


    start = match.end()

    if match.group(0).rstrip(">").endswith("/"):
        return html_text, False


    depth = 1
    pos = start
    open_pattern = re.compile(r"<" + re.escape(tag) + r"\b[^>]*>", re.IGNORECASE)
    close_pattern = re.compile(r"</" + re.escape(tag) + r"\s*>", re.IGNORECASE)

    while depth > 0 and pos < len(html_text):
        next_open = open_pattern.search(html_text, pos)
        next_close = close_pattern.search(html_text, pos)

        if not next_close:
            return html_text, False

        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            if depth == 0:
                close_match = next_close
                break
            pos = next_close.end()
    else:
        return html_text, False


    content = html_text[start : close_match.start()]


    input_pattern = re.compile(
        r"<(?P<input_tag>input|textarea|select)\b(?P<input_attrs>[^>]*?)>|<(?P<ce_tag>[a-z][a-z0-9]*)\b(?P<ce_attrs>[^>]*?\bcontenteditable\b[^>]*?)>",
        re.IGNORECASE | re.DOTALL,
    )
    input_match = input_pattern.search(content)
    if not input_match:
        return html_text, False


    if input_match.group("input_tag"):
        input_tag_name = input_match.group("input_tag")
        input_attrs = input_match.group("input_attrs") or ""
        input_open = f"<{input_tag_name}{input_attrs}>"
    else:
        input_tag_name = input_match.group("ce_tag")
        input_attrs = input_match.group("ce_attrs") or ""
        input_open = f"<{input_tag_name}{input_attrs}>"


    if "data-action-id" not in input_open and "data-action-aliases" not in input_open:

        input_open = input_open.rstrip(">") + f' data-action-id="{action_id}">'


    attrs_no_action = _remove_attr(attrs, "data-action-id")

    attrs_no_action = re.sub(
        r'(\bdata-action-aliases=["\'])([^"\']*?)(["\'])',
        lambda m: _remove_alias_from_list(m, action_id),
        attrs_no_action,
        flags=re.IGNORECASE,
    )
    wrapper_open = f"<{tag}{attrs_no_action}>"


    new_content = content[: input_match.start()] + input_open + content[input_match.end() :]
    new_html = (
        html_text[: match.start()]
        + wrapper_open
        + new_content
        + html_text[close_match.start() :]
    )
    return new_html, True


def _remove_alias_from_list(match: re.Match, action_id: str) -> str:
    prefix = match.group(1)
    aliases_str = match.group(2)
    suffix = match.group(3)
    aliases = [a.strip() for a in aliases_str.split() if a.strip() and a.strip() != action_id]
    if not aliases:
        return ""
    return f'{prefix}{" ".join(aliases)}{suffix}'


_MATERIALIZER_RUNTIME = """\
<script data-ghostweb-runtime="action-effect-materializer">
(function() {
  if (window.__ghostwebActionEffectMaterializerReady) return;
  window.__ghostwebActionEffectMaterializerReady = true;
  // The DOM-binding bridge keeps its contract table inside its own closure.
  // Route fallback also needs that table when a task-context-selected action
  // bubbles through a same-page gesture host; use the explicit page-owned
  // bridge export instead of relying on an out-of-scope local ``ACTIONS``.
  var ACTIONS = window.__GHOSTWEB_DOM_BINDING_ACTIONS || {};
  function pushActionIds(ids, value) {
    String(value || '').split(/\\s+/).forEach(function(match) {
      match = String(match || '').trim();
      if (match && ids.indexOf(match) === -1) ids.push(match);
    });
  }
  function hostActionIds(host) {
    var ids = [];
    pushActionIds(ids, host.getAttribute('data-action-id'));
    pushActionIds(ids, host.getAttribute('data-action-aliases'));
    try {
      var map = JSON.parse(host.getAttribute('data-ghostweb-target-map') || '{}') || {};
      Object.keys(map).forEach(function(id) { pushActionIds(ids, id); });
    } catch (error) {}
    return ids;
  }
  function contextTraceIds() {
    var context = null;
    try { context = window.__ghostwebExecutionContext || null; } catch (error) {}
    if (!context) {
      try { context = JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{}'); } catch (error) {}
    }
    var ids = [];
    [context && context.task_owner_id, context && context.augmented_task_id,
      context && context.task_id, context && context.taskId,
      context && context.source_reference_task_id, context && context.sourceReferenceTaskId,
      context && context.reference_trace_id, context && context.trace_id].forEach(function(value) {
      value = String(value || '').trim();
      if (value && ids.indexOf(value) === -1) ids.push(value);
    });
    return ids;
  }
  function actionMatchesStaticHostTarget(host, actionId) {
    var directTarget = host.getAttribute('data-ghostweb-target') || host.getAttribute('href') || '';
    var hostTarget = directTarget;
    // A shared visible route host may carry a primary target for its default
    // action and per-action targets for context-selected aliases.  The
    // action-keyed map is therefore the authoritative host projection for
    // this candidate.  Falling back to the primary target first would reject
    // a valid alias solely because another action owns the default route.
    try {
      var targetMap = JSON.parse(host.getAttribute('data-ghostweb-target-map') || '{}') || {};
      if (Object.prototype.hasOwnProperty.call(targetMap, actionId)) {
        hostTarget = String(targetMap[actionId] || '');
      }
    } catch (error) {
      return false;
    }
    var row = ACTIONS[actionId] || {};
    var actionTarget = String(row.target || '');
    // The selected per-action target identifies the visible entity on this
    // exact host. A target-map alias for another entity remains stale
    // cross-entity context and cannot take ownership merely because its
    // trace id is current. Same-page aliases deliberately remain eligible.
    if (!hostTarget || !actionTarget || row.route_navigation !== true) return true;
    return baseFilename(hostTarget) === baseFilename(actionTarget);
  }
  function actionIdForTrace(host, traceId) {
    if (!traceId) return '';
    var primary = host.getAttribute('data-action-id') || '';
    var matches = hostActionIds(host).filter(function(id) {
      return id.indexOf(traceId + ':step') === 0 && actionMatchesStaticHostTarget(host, id);
    });
    if (matches.length === 1) return matches[0];
    if (primary && primary.indexOf(traceId + ':step') === 0 && matches.indexOf(primary) !== -1) return primary;
    return '';
  }
  function contextActionId(host) {
    var context = null;
    try { context = window.__ghostwebExecutionContext || null; } catch (error) {}
    if (!context) {
      try { context = JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{}'); } catch (error) {}
    }
    var actionId = context && context.action_id ? String(context.action_id) : '';
    if (!actionId) return '';
    var actionTraceId = traceIdFromActionId(actionId);
    if (actionTraceId && contextTraceIds().indexOf(actionTraceId) === -1) return '';
    var primary = host.getAttribute('data-action-id') || '';
    var aliases = String(host.getAttribute('data-action-aliases') || '').split(/\\s+/).filter(Boolean);
    if ((primary === actionId || aliases.indexOf(actionId) !== -1) && actionMatchesStaticHostTarget(host, actionId)) return actionId;
    return '';
  }
  function activeActionId(host) {
    var explicit = contextActionId(host);
    if (explicit) return explicit;
    var ids = contextTraceIds();
    for (var index = 0; index < ids.length; index += 1) {
      var matched = actionIdForTrace(host, ids[index]);
      if (matched) return matched;
    }
    return '';
  }
  function resolveActionTarget(host) {
    var directTarget = host.getAttribute('data-ghostweb-target') || '';
    var rawMap = host.getAttribute('data-ghostweb-target-map') || '';
    var actionId = activeActionId(host) || host.getAttribute('data-action-id') || '';
    if (!actionId) {
      console.error('[T2WActionEffectMaterializer] missing data-action-id on route host');
      return '';
    }
    try {
      if (rawMap) {
        var map = JSON.parse(rawMap);
        if (!Object.prototype.hasOwnProperty.call(map, actionId)) {
          console.error('[T2WActionEffectMaterializer] missing target-map entry for ' + actionId);
          return '';
        }
        return String(map[actionId] || '');
      }
    } catch (error) {
      console.error('[T2WActionEffectMaterializer] invalid data-ghostweb-target-map', error);
      return '';
    }
    if (!directTarget) {
      console.error('[T2WActionEffectMaterializer] missing data-ghostweb-target for ' + actionId);
      return '';
    }
    return directTarget;
  }
  function staticRouteTargetForMissingDynamicRow(host, actionId) {
    // A package-only re-materialization can legitimately retain an exact
    // static route alias after the page-local dynamic bridge has lost that
    // row.  It is safe only when no dynamic row was selected at all: an
    // existing same-page row remains authoritative over any old static map.
    if (!actionId || !host.matches('a[href], [data-ghostweb-action="navigate"][data-ghostweb-target]')) return '';
    try {
      var map = JSON.parse(host.getAttribute('data-ghostweb-target-map') || '{}') || {};
      if (!Object.prototype.hasOwnProperty.call(map, actionId)) return '';
      return String(map[actionId] || '');
    } catch (error) {
      console.error('[T2WActionEffectMaterializer] invalid data-ghostweb-target-map', error);
      return '';
    }
  }
  function emitCheckpointFromHost(host) {
    if (!host || !window.GhostwebCheckpointActionMap || !window.GhostwebCheckpointActionMap.emitForAction) return;
    var ids = [];
    var active = activeActionId(host);
    if (active) ids.push(active);
    if (!ids.length) {
      var primary = host.getAttribute('data-action-id') || '';
      if (primary) ids.push(primary);
    }
    ids.forEach(function(id) {
      try { window.GhostwebCheckpointActionMap.emitForAction(id, 'click'); } catch (error) {}
    });
  }
  function baseFilename(value) {
    return String(value || '').split(/[?#]/, 1)[0].split('/').pop() || '';
  }
  function traceIdFromActionId(actionId) {
    var text = String(actionId || '');
    var marker = text.indexOf(':step');
    return marker > 0 ? text.slice(0, marker) : '';
  }
  function stepIndexFromActionId(actionId) {
    var match = String(actionId || '').match(/:step(\\d+)$/);
    return match ? Number(match[1]) : null;
  }
  function synchronizeExecutionContextAction(actionId) {
    actionId = String(actionId || '').trim();
    if (!actionId) return false;
    var traceId = traceIdFromActionId(actionId);
    var stepIndex = stepIndexFromActionId(actionId);
    if (traceId && contextTraceIds().indexOf(traceId) === -1) return false;
    var context = null;
    try { context = window.__ghostwebExecutionContext || null; } catch (error) {}
    if (!context || typeof context !== 'object') {
      try { context = JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{}'); } catch (error) { context = {}; }
    }
    context = Object.assign({}, context || {}, { action_id: actionId });
    if (stepIndex !== null) {
      context.step_index = stepIndex;
      context.raw_step_index = stepIndex;
    }
    var serialized = JSON.stringify(context);
    window.__ghostwebExecutionContext = context;
    window.GhostwebActiveActionId = actionId;
    window.__ghostwebActiveActionId = actionId;
    if (traceId) {
      window.GhostwebActiveTraceId = traceId;
      window.__ghostwebActiveTraceId = traceId;
    }
    try { window.localStorage.setItem('ghostweb.execution_context', serialized); } catch (error) {}
    try { window.sessionStorage.setItem('ghostweb.execution_context', serialized); } catch (error) {}
    try {
      window.dispatchEvent(new CustomEvent('ghostweb:context-action-resolved', {
        detail: { action_id: actionId, step_index: stepIndex, context: context }
      }));
    } catch (error) {}
    return true;
  }
  function persistTargetEntryProjection(host, target) {
    if (!host || !target) return;
    var surface = String(host.getAttribute('data-ghostweb-target-entry-surface') || '').trim();
    if (!surface) return;
    var actionId = activeActionId(host) || host.getAttribute('data-action-id') || '';
    if (!actionId) return;
    var context = {};
    try {
      context = window.__ghostwebExecutionContext || JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{}') || {};
    } catch (error) {}
    var projection = {
      surface: surface,
      target: baseFilename(target),
      target_filename: baseFilename(target),
      action_id: actionId,
      trace_id: traceIdFromActionId(actionId) || String(context.reference_trace_id || context.trace_id || context.task_id || ''),
      task_id: String(context.task_id || ''),
      augmented_task_id: String(context.augmented_task_id || context.task_owner_id || ''),
      source: 'action_effect_materializer.target_entry_projection',
      written_at: Date.now()
    };
    var serialized = JSON.stringify(projection);
    try { window.sessionStorage.setItem('ghostweb.route.target_entry_projection', serialized); } catch (error) {}
    try { window.localStorage.setItem('ghostweb.route.target_entry_projection', serialized); } catch (error) {}
  }
  function intrinsicBackOwner(target) {
    if (!target || !target.closest) return null;
    return target.closest(
      '[data-ghostweb-action="returnBack"], ' +
      '[data-ghostweb-action="history_back"], ' +
      '[data-ghostweb-route-action="default_back"]'
    );
  }
  document.addEventListener('click', function(event) {
    // Back controls own their route lifecycle intrinsically. They may live
    // inside a context-bound ancestor that also carries an ordinary route
    // action. Never let that ancestor install a deferred navigate fallback
    // after returnBack/history_back has already resolved the route stack.
    if (intrinsicBackOwner(event.target)) return;
    // Select semantics from the context-bound formal ACTIONS row, not from
    // the host's static primary attributes. A shared visible control may own
    // a same-page primary action and a route alias for another current task.
    var host = event.target && event.target.closest && event.target.closest(
      '[data-action-id], [data-action-aliases], [data-ghostweb-action="navigate"], [data-ghostweb-target-map]'
    );
    if (!host) return;
    if (host.closest('[data-ghostweb-noninteractive-notice]')) return;
    var selectedActionId = activeActionId(host) || host.getAttribute('data-action-id') || '';
    synchronizeExecutionContextAction(selectedActionId);
    var selectedRow = selectedActionId ? ACTIONS[selectedActionId] : null;
    var selectedRowIsRoute = !!(
      selectedRow
      && selectedRow.route_navigation === true
      && selectedRow.target
    );
    var hasStaticNavigation = host.matches('a[href], [data-ghostweb-action="navigate"][data-ghostweb-target]');
    var staticRouteTarget = !selectedRow
      ? staticRouteTargetForMissingDynamicRow(host, selectedActionId)
      : '';
    if (!selectedRowIsRoute && !staticRouteTarget) {
      if (selectedRow && hasStaticNavigation) {
        emitCheckpointFromHost(host);
        event.preventDefault();
        // Suppress only stale default navigation. Page-owned target/bubble
        // reducers remain the authority for the selected same-page row.
      }
      return;
    }
    var target = selectedRowIsRoute ? String(selectedRow.target || '') : staticRouteTarget;
    // Persist a declared target-entry surface before any page-owned handler
    // navigates.  This is a generic route/action contract projection: the
    // destination page decides how to materialize the named surface, while
    // the current context-selected action id remains the sole identity.
    persistTargetEntryProjection(host, target);
    emitCheckpointFromHost(host);
    // A context-selected alias can be a same-page mutation even when the
    // host's static primary action is a route. Do not reload the page and
    // preempt its page-owned business handler.
    if (baseFilename(target) === baseFilename(window.location.pathname)) return;
    // Page-owned inline and registered listeners may need the same click to
    // commit an app-state carrier before the route fallback runs. Queueing
    // directly from capture is too early, while a bubble-tail listener is
    // skipped when a page reducer stops propagation. Wrap only this exact
    // Event's native propagation methods with an own property. The wrapper
    // calls the native method first, then schedules one post-dispatch route.
    var sourceAtClick = baseFilename(window.location.pathname);
    var navigationEpochAtClick = Number(window.__ghostwebRouteNavigationEpoch || 0);
    var routeScheduled = false;
    host.setAttribute('data-ghostweb-route-fallback-owner', 'action-effect-materializer');
    var deferRoute = typeof window.queueMicrotask === 'function'
      ? window.queueMicrotask.bind(window)
      : function(callback) { Promise.resolve().then(callback); };
    function routeStillOwned() {
      // A page-owned inline route can win during the same click turn. The
      // epoch/source checks make that owner authoritative and suppress only
      // this fallback, not the page-owned transition.
      if (host.getAttribute('data-ghostweb-route-fallback-cancelled') === 'true') return false;
      if (Number(window.__ghostwebRouteNavigationEpoch || 0) !== navigationEpochAtClick) return false;
      if (baseFilename(window.location.pathname) !== sourceAtClick) return false;
      if (baseFilename(target) === baseFilename(window.location.pathname)) return false;
      return true;
    }
    function scheduleRoute() {
      if (routeScheduled) return;
      routeScheduled = true;
      deferRoute(function() {
        routeScheduled = false;
        if (!routeStillOwned()) return;
        if (window.GhostwebRouteExecution && window.GhostwebRouteExecution.navigate) {
          window.GhostwebRouteExecution.navigate(target, { mode: host.getAttribute('data-ghostweb-history-mode') || 'push' });
          return;
        }
        console.error('[T2WActionEffectMaterializer] GhostwebRouteExecution.navigate is required');
      });
    }
    function wrapPropagationMethod(event, methodName) {
      if (!event || typeof event[methodName] !== 'function') return false;
      var descriptor = null;
      try { descriptor = Object.getOwnPropertyDescriptor(event, methodName); } catch (error) {}
      if (
        descriptor &&
        descriptor.configurable === false &&
        descriptor.writable === false &&
        !descriptor.set
      ) {
        return false;
      }
      var original = event[methodName];
      var wrapper = function() {
        var result;
        try {
          result = original.apply(this, arguments);
        } finally {
          scheduleRoute();
        }
        return result;
      };
      try {
        Object.defineProperty(event, methodName, {
          configurable: descriptor ? descriptor.configurable !== false : true,
          enumerable: descriptor ? descriptor.enumerable : false,
          writable: true,
          value: wrapper
        });
      } catch (error) {
        return false;
      }
      return event[methodName] === wrapper;
    }
    wrapPropagationMethod(event, 'stopImmediatePropagation');
    wrapPropagationMethod(event, 'stopPropagation');
    // If no page-owned listener stops propagation, this tail still runs in
    // the same click turn and gives inline/registered route owners a chance
    // to win before the fallback microtask.
    window.addEventListener('click', function routeAtBubbleTail(routeEvent) {
      if (routeEvent === event) scheduleRoute();
    }, { once: true });
  }, true);
})();
</script>"""


def _dom_binding_runtime(bindings: Iterable[Mapping[str, Any]]) -> str:
    rows: Dict[str, Dict[str, Any]] = {}
    for binding in bindings:
        action_id = _binding_action_id(binding)
        if not action_id:
            continue
        rows[action_id] = {
            "target": _binding_target(binding),
            "effect_kind": _binding_effect(binding),
            "dom_binding_id": _text(binding.get("dom_binding_id")),
            "component_ref": _binding_component_ref(binding),
            "input_dependency_text": _binding_input_dependency_text(binding),
            "closes_owning_overlay": _binding_closes_owning_overlay(binding),
            "same_page_activation_ref": _binding_same_page_activation_ref(binding),
            "visible_window_transition": _binding_visible_window_transition(binding),
            "route_navigation": _binding_requires_route_navigation(binding),
        }
    if not rows:
        return ""
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"""\
<script data-ghostweb-runtime="dom-binding-action-host-bridge">
(function() {{
  window.__GHOSTWEB_DOM_BINDING_ACTIONS = {payload};
  const ACTIONS = window.__GHOSTWEB_DOM_BINDING_ACTIONS || {{}};
  const COMPONENT_ACTIONS = {{}};
  Object.keys(ACTIONS).forEach(function(actionId) {{
    const componentRef = String((ACTIONS[actionId] || {{}}).component_ref || '').trim();
    if (!componentRef) return;
    if (!COMPONENT_ACTIONS[componentRef]) COMPONENT_ACTIONS[componentRef] = [];
    if (COMPONENT_ACTIONS[componentRef].indexOf(actionId) === -1) {{
      COMPONENT_ACTIONS[componentRef].push(actionId);
    }}
  }});
  const ACTION_ID_RE = /task_[A-Za-z0-9_]+:step\\d+/g;
  function idsFrom(value) {{
    const ids = [];
    String(value || '').replace(ACTION_ID_RE, function(match) {{
      if (ids.indexOf(match) === -1) ids.push(match);
      return match;
    }});
    return ids;
  }}
  function directActionIdsFrom(value) {{
    return String(value || '').split(/\\s+/).map(function(id) {{ return id.trim(); }}).filter(Boolean);
  }}
  function actionIdsFromComponentRefs(el) {{
    const ids = [];
    const refs = [];
    const primary = String(el.getAttribute('data-element-id') || '').trim();
    if (primary) refs.push(primary);
    directActionIdsFrom(el.getAttribute('data-element-aliases')).forEach(function(ref) {{
      if (refs.indexOf(ref) === -1) refs.push(ref);
    }});
    refs.forEach(function(ref) {{
      (COMPONENT_ACTIONS[ref] || []).forEach(function(id) {{
        if (ids.indexOf(id) === -1) ids.push(id);
      }});
    }});
    return ids;
  }}
  function readIds(el) {{
    const ids = [];
    // Action attributes are the owning action-id authority.  Preserve opaque
    // schema/runtime namespaces (for example `product::open-panel`) as well
    // as trace ids.  Element/binding ids may embed action ids inside longer
    // component strings, so those still use the trace-id extractor below.
    ['data-action-id', 'data-action-aliases'].forEach(function(name) {{
      directActionIdsFrom(el.getAttribute(name)).forEach(function(id) {{
        if (ids.indexOf(id) === -1) ids.push(id);
      }});
    }});
    ['data-element-id', 'data-element-aliases', 'data-ghostweb-dom-binding-id'].forEach(function(name) {{
      idsFrom(el.getAttribute(name)).forEach(function(id) {{
        if (ids.indexOf(id) === -1) ids.push(id);
      }});
    }});
    /* component_ref is a formal relational key and may be opaque rather than
       embedding an action id. Runtime-created hosts therefore join the
       contract through exact data-element token equality. */
    actionIdsFromComponentRefs(el).forEach(function(id) {{
      if (ids.indexOf(id) === -1) ids.push(id);
    }});
    Object.keys(readMap(el)).forEach(function(id) {{
      if (ids.indexOf(id) === -1) ids.push(id);
    }});
    return ids;
  }}
  function readMap(el) {{
    try {{ return JSON.parse(el.getAttribute('data-ghostweb-target-map') || '{{}}') || {{}}; }}
    catch (error) {{ return {{}}; }}
  }}
  function contextTraceIds() {{
    let context = null;
    try {{ context = window.__ghostwebExecutionContext || null; }} catch (error) {{}}
    if (!context) {{
      try {{ context = JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{{}}'); }} catch (error) {{}}
    }}
    const ids = [];
    [context && context.task_owner_id, context && context.augmented_task_id,
      context && context.task_id, context && context.taskId,
      context && context.source_reference_task_id, context && context.sourceReferenceTaskId,
      context && context.reference_trace_id, context && context.trace_id].forEach(function(value) {{
      value = String(value || '').trim();
      if (value && ids.indexOf(value) === -1) ids.push(value);
    }});
    return ids;
  }}
  function targetFilename(value) {{
    return String(value || '').split(/[?#]/, 1)[0].split('/').pop() || '';
  }}
  function actionMatchesStaticHostTarget(host, actionId) {{
    const directTarget = host.getAttribute('data-ghostweb-target') || host.getAttribute('href') || '';
    let hostTarget = directTarget;
    // A shared visible route host may carry a primary target for its default
    // action and per-action targets for context-selected aliases.  Resolve
    // this candidate through its action-keyed map before comparing it with
    // the formal binding; otherwise the primary route masks a valid alias.
    try {{
      const targetMap = JSON.parse(host.getAttribute('data-ghostweb-target-map') || '{{}}') || {{}};
      if (Object.prototype.hasOwnProperty.call(targetMap, actionId)) {{
        hostTarget = String(targetMap[actionId] || '');
      }}
    }} catch (error) {{
      return false;
    }}
    const row = ACTIONS[actionId] || {{}};
    const actionTarget = String(row.target || '');
    // A target-map alias for another entity remains stale cross-entity
    // context. Same-page aliases remain eligible because they can
    // intentionally suppress a static route.
    if (!hostTarget || !actionTarget || row.route_navigation !== true) return true;
    return targetFilename(hostTarget) === targetFilename(actionTarget);
  }}
  function actionIdForTrace(host, traceId) {{
    if (!traceId) return '';
    const primary = host.getAttribute('data-action-id') || '';
    const matches = readIds(host).filter(function(id) {{
      return id.indexOf(traceId + ':step') === 0 && actionMatchesStaticHostTarget(host, id);
    }});
    if (matches.length === 1) return matches[0];
    if (primary && primary.indexOf(traceId + ':step') === 0 && matches.indexOf(primary) !== -1) return primary;
    return '';
  }}
  function contextActionId(host) {{
    let context = null;
    try {{ context = window.__ghostwebExecutionContext || null; }} catch (error) {{}}
    if (!context) {{
      try {{ context = JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{{}}'); }} catch (error) {{}}
    }}
    const actionId = context && context.action_id ? String(context.action_id) : '';
    if (!actionId) return '';
    const actionTraceId = traceIdFromActionId(actionId);
    if (actionTraceId && contextTraceIds().indexOf(actionTraceId) === -1) return '';
    const ids = readIds(host);
    return ids.indexOf(actionId) !== -1 && actionMatchesStaticHostTarget(host, actionId) ? actionId : '';
  }}
  function activeActionId(host) {{
    const explicit = contextActionId(host);
    if (explicit) return explicit;
    const ids = contextTraceIds();
    for (let index = 0; index < ids.length; index += 1) {{
      const matched = actionIdForTrace(host, ids[index]);
      if (matched) return matched;
    }}
    return '';
  }}
  function runtimeActionIds(host) {{
    const active = activeActionId(host);
    if (active) return [active];
    const ids = readIds(host);
    const primary = host.getAttribute('data-action-id') || '';
    if (primary && ids.indexOf(primary) !== -1) return [primary];
    return ids.length ? [ids[0]] : [];
  }}
  function writeMap(el, map) {{
    const clean = {{}};
    Object.keys(map).sort().forEach(function(key) {{
      if (key && map[key]) clean[key] = String(map[key]);
    }});
    if (Object.keys(clean).length) setObservedAttr(el, 'data-ghostweb-target-map', JSON.stringify(clean));
  }}
  function setObservedAttr(el, name, value) {{
    value = String(value || '');
    if (el.getAttribute(name) !== value) el.setAttribute(name, value);
  }}
  function traceIdFromActionId(actionId) {{
    const marker = String(actionId || '').indexOf(':step');
    return marker > 0 ? String(actionId).slice(0, marker) : '';
  }}
  function stepIndexFromActionId(actionId) {{
    const match = String(actionId || '').match(/:step(\\d+)$/);
    return match ? Number(match[1]) : null;
  }}
  function synchronizeExecutionContextAction(actionId) {{
    actionId = String(actionId || '').trim();
    if (!actionId) return false;
    const traceId = traceIdFromActionId(actionId);
    const stepIndex = stepIndexFromActionId(actionId);
    if (traceId && contextTraceIds().indexOf(traceId) === -1) return false;
    let context = null;
    try {{ context = window.__ghostwebExecutionContext || null; }} catch (error) {{}}
    if (!context || typeof context !== 'object') {{
      try {{ context = JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{{}}'); }} catch (error) {{ context = {{}}; }}
    }}
    context = Object.assign({{}}, context || {{}}, {{ action_id: actionId }});
    if (stepIndex !== null) {{
      context.step_index = stepIndex;
      context.raw_step_index = stepIndex;
    }}
    const serialized = JSON.stringify(context);
    window.__ghostwebExecutionContext = context;
    window.GhostwebActiveActionId = actionId;
    window.__ghostwebActiveActionId = actionId;
    if (traceId) {{
      window.GhostwebActiveTraceId = traceId;
      window.__ghostwebActiveTraceId = traceId;
    }}
    try {{ window.localStorage.setItem('ghostweb.execution_context', serialized); }} catch (error) {{}}
    try {{ window.sessionStorage.setItem('ghostweb.execution_context', serialized); }} catch (error) {{}}
    try {{
      window.dispatchEvent(new CustomEvent('ghostweb:context-action-resolved', {{
        detail: {{ action_id: actionId, step_index: stepIndex, context: context }}
      }}));
    }} catch (error) {{}}
    return true;
  }}
  function targetTextValue(target) {{
    if (!target) return '';
    const value = ('value' in target) ? target.value : (target.textContent || '');
    return String(value == null ? '' : value).trim();
  }}
  function isInitialHiddenStateContainer(el) {{
    return !!el && el.getAttribute('data-ghostweb-materialized') === 'initial-hidden-state-container';
  }}
  function forceHideInitialHiddenStateContainer(el) {{
    if (!isInitialHiddenStateContainer(el)) return false;
    el.setAttribute('aria-hidden', 'true');
    const style = el.getAttribute('style') || '';
    if (!/(^|;)\\s*display\\s*:/i.test(style)) {{
      el.setAttribute('style', (style ? style.replace(/;\\s*$/, '') + '; ' : '') + 'display: none');
    }} else if (el.style) {{
      el.style.display = 'none';
    }}
    return true;
  }}
  function isNaturalScrollViewportHost(el) {{
    if (!el || !el.closest) return false;
    const scroller = el.closest('[data-ghostweb-horizontal-scroll]');
    if (!scroller) return false;
    const scalarRef = String(el.getAttribute('data-ghostweb-visibility-state-ref') || '');
    let mappedRefs = [];
    try {{
      const mapped = JSON.parse(el.getAttribute('data-ghostweb-visibility-precondition-map') || '{{}}');
      mappedRefs = Object.keys(mapped || {{}}).map(function(actionId) {{
        return String((mapped[actionId] || {{}}).state_ref || '');
      }});
    }} catch (error) {{}}
    const refs = [scalarRef].concat(mappedRefs).filter(Boolean);
    if (!refs.some(function(ref) {{ return /(?:^|\\.)visible_window$/.test(ref); }})) return false;
    const gestureHost = scroller.closest('[data-gesture-host="component_visible_window"]');
    return !!gestureHost;
  }}
  function restoreNaturalScrollViewportHost(el) {{
    if (!isNaturalScrollViewportHost(el) || !el.style) return false;
    el.setAttribute('data-ghostweb-precondition-satisfied', 'natural_scroll_viewport');
    el.setAttribute('aria-hidden', 'false');
    el.style.display = '';
    el.style.visibility = '';
    el.style.pointerEvents = '';
    ['hidden', 'invisible', 'pointer-events-none'].forEach(function(cls) {{ el.classList.remove(cls); }});
    return true;
  }}
  function hidePendingPreconditionHost(el) {{
    if (!el || !(el.getAttribute('data-ghostweb-visibility-producer-action-id') || el.getAttribute('data-ghostweb-visibility-precondition-map'))) return;
    if (forceHideInitialHiddenStateContainer(el)) return;
    /* A real horizontal visible-window rail owns visibility through viewport
       clipping and scroll position. Removing a child with display:none makes
       that child naturally unreachable and incorrectly requires replay step
       context to reveal it. Keep such children in layout so pointer/touch/
       wheel scrolling can expose them without injected trace authority. */
    if (restoreNaturalScrollViewportHost(el)) return;
    if (el.getAttribute('data-ghostweb-precondition-satisfied') === 'true') return;
    el.setAttribute('aria-hidden', 'true');
    const style = el.getAttribute('style') || '';
    if (!/(^|;)\\s*display\\s*:/i.test(style)) {{
      el.setAttribute('style', (style ? style.replace(/;\\s*$/, '') + '; ' : '') + 'display: none');
    }}
  }}
  function revealPreconditionHost(el) {{
    if (!el || !el.style) return;
    el.setAttribute('data-ghostweb-precondition-satisfied', 'true');
    if (forceHideInitialHiddenStateContainer(el)) return;
    el.setAttribute('aria-hidden', 'false');
    el.style.display = '';
    el.style.visibility = '';
    el.style.pointerEvents = '';
    ['hidden', 'invisible', 'pointer-events-none'].forEach(function(cls) {{ el.classList.remove(cls); }});
  }}
  function rowClosesOwningOverlay(row) {{
    return !!(row && row.closes_owning_overlay);
  }}
  function clearStaleOwningSurfaceBlockers(scope, overlay) {{
    var traceId = contextTraceId();
    if (!traceId || !scope || !scope.querySelectorAll || !document.elementFromPoint) return;
    scope.querySelectorAll('[data-action-id], [data-action-aliases]').forEach(function(host) {{
      if (!readIds(host).some(function(id) {{ return id.indexOf(traceId + ':step') === 0; }})) return;
      var rect = host.getBoundingClientRect ? host.getBoundingClientRect() : null;
      if (!rect || rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.top >= window.innerHeight) return;
      var blocker = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      if (!blocker || blocker === host || host.contains(blocker) || blocker.contains(host) || overlay.contains(blocker)) return;
      var surface = blocker.closest && blocker.closest('section, article, [data-ghostweb-surface-role]');
      if (!surface || !scope.contains(surface) || surface === overlay) return;
      var surfaceRect = surface.getBoundingClientRect();
      var style = window.getComputedStyle ? window.getComputedStyle(surface) : null;
      var positioned = style && (style.position === 'absolute' || style.position === 'fixed' || Number(style.zIndex || 0) > 0);
      if (!positioned || surfaceRect.width < window.innerWidth * 0.5 || surfaceRect.height < window.innerHeight * 0.5) return;
      surface.setAttribute('aria-hidden', 'true');
      if (surface.style) surface.style.display = 'none';
    }});
  }}
  function clearStaleOwningSurfaceLayers(scope, overlay) {{
    var traceId = contextTraceId();
    if (!traceId || !scope || !scope.querySelectorAll) return;
    scope.querySelectorAll('section, article, [data-ghostweb-surface-role]').forEach(function(surface) {{
      if (!surface || surface === overlay || surface.contains(overlay) || overlay.contains(surface)) return;
      var ownsTraceHost = Array.prototype.some.call(
        surface.querySelectorAll('[data-action-id], [data-action-aliases]'),
        function(candidate) {{
          return readIds(candidate).some(function(id) {{ return id.indexOf(traceId + ':step') === 0; }});
        }}
      );
      if (ownsTraceHost) return;
      var rect = surface.getBoundingClientRect ? surface.getBoundingClientRect() : null;
      var style = window.getComputedStyle ? window.getComputedStyle(surface) : null;
      var positioned = style && (style.position === 'absolute' || style.position === 'fixed');
      var positiveLayer = style && Number(style.zIndex || 0) > 0;
      if (!rect || !positioned || !positiveLayer || rect.width < window.innerWidth * 0.5 || rect.height < window.innerHeight * 0.5) return;
      surface.setAttribute('aria-hidden', 'true');
      if (surface.style) surface.style.display = 'none';
    }});
  }}
  function owningOverlayForHost(host) {{
    if (!host || !host.closest) return null;
    var selector = '[data-ghostweb-overlay-layer-role="backdrop"], [aria-modal="true"], [role="dialog"], dialog, [id*="overlay" i], [id*="modal" i]';
    var closest = host.closest(selector);
    if (!closest) return null;
    if (closest.hasAttribute('aria-hidden') || closest.hasAttribute('open') || closest.hasAttribute('data-ghostweb-overlay-layer-role')) return closest;
    var ancestor = closest.parentElement && closest.parentElement.closest(selector);
    while (ancestor) {{
      if (ancestor.hasAttribute('aria-hidden') || ancestor.hasAttribute('open') || ancestor.hasAttribute('data-ghostweb-overlay-layer-role')) return ancestor;
      ancestor = ancestor.parentElement && ancestor.parentElement.closest(selector);
    }}
    return closest;
  }}
  function overlayAlreadyClosed(overlay) {{
    if (!overlay) return true;
    if (overlay.getAttribute('aria-hidden') === 'true' || overlay.hasAttribute('hidden')) return true;
    if (overlay.tagName && overlay.tagName.toLowerCase() === 'dialog' && !overlay.hasAttribute('open')) return true;
    var style = window.getComputedStyle ? window.getComputedStyle(overlay) : null;
    return !!style && (style.display === 'none' || style.visibility === 'hidden');
  }}
  function hideContractOwnedOverlay(overlay) {{
    if (!overlay || overlayAlreadyClosed(overlay)) return false;
    var previousDisplay = overlay.style ? overlay.style.getPropertyValue('display') : '';
    var previousPriority = overlay.style ? overlay.style.getPropertyPriority('display') : '';
    overlay.setAttribute('data-ghostweb-contract-projected-hidden', 'true');
    overlay.setAttribute('aria-hidden', 'true');
    if (overlay.style) overlay.style.setProperty('display', 'none', 'important');
    if (typeof MutationObserver === 'function') {{
      var observer = new MutationObserver(function() {{
        var className = String(overlay.getAttribute('class') || '').toLowerCase();
        var reopened = overlay.getAttribute('aria-hidden') === 'false' || overlay.hasAttribute('open') || /(?:^|\\s)(?:active|open|opened|show|shown|visible)(?:\\s|$)/.test(className);
        if (!reopened) return;
        if (overlay.style) {{
          if (previousDisplay) overlay.style.setProperty('display', previousDisplay, previousPriority);
          else overlay.style.removeProperty('display');
        }}
        overlay.removeAttribute('data-ghostweb-contract-projected-hidden');
        observer.disconnect();
      }});
      observer.observe(overlay, {{ attributes: true, attributeFilter: ['aria-hidden', 'class', 'open'] }});
    }}
    return true;
  }}
  function applyContractDomProjection(host, row) {{
    if (!rowClosesOwningOverlay(row) || !host || !host.closest) return;
    var overlay = owningOverlayForHost(host);
    /* Page-owned handlers run before this deferred bridge.  If they already
       closed the owning surface, do not permanently hide a reusable nested
       dialog.  Contract DOM projection is only the missing-handler fallback. */
    if (!overlay || !hideContractOwnedOverlay(overlay)) return;
    var scope = overlay.parentElement || document;
    scope.querySelectorAll('.pointer-events-none, [inert]').forEach(function(el) {{
      el.removeAttribute('inert');
      if (el.classList) {{
        el.classList.remove('pointer-events-none');
        el.classList.remove('brightness-50');
        el.classList.remove('filter');
      }}
    }});
    clearStaleOwningSurfaceLayers(scope, overlay);
    window.requestAnimationFrame(function() {{ clearStaleOwningSurfaceBlockers(scope, overlay); }});
  }}
  function ensureSamePageActivationStyle() {{
    if (document.getElementById('ghostweb-same-page-activation-style')) return;
    var style = document.createElement('style');
    style.id = 'ghostweb-same-page-activation-style';
    style.textContent = '[data-ghostweb-inline-active="true"]{{outline:2px solid #1677ff!important;outline-offset:-2px!important;}}';
    (document.head || document.documentElement).appendChild(style);
  }}
  function setSamePageActivationState(host, active) {{
    if (!host || !host.setAttribute) return;
    host.setAttribute('data-ghostweb-inline-active', active ? 'true' : 'false');
    host.setAttribute('data-selected', active ? 'true' : 'false');
    host.setAttribute('aria-pressed', active ? 'true' : 'false');
  }}
  function applySamePageActivationProjection(host, row) {{
    var stateRef = String(row && row.same_page_activation_ref || '').trim();
    if (!stateRef || !host || row.effect_kind !== 'same_page_mutation') return false;
    ensureSamePageActivationStyle();
    document.querySelectorAll('[data-action-id], [data-action-aliases]').forEach(function(candidate) {{
      var sharesActivation = readIds(candidate).some(function(id) {{
        return String((ACTIONS[id] || {{}}).same_page_activation_ref || '') === stateRef;
      }});
      if (sharesActivation) setSamePageActivationState(candidate, candidate === host);
    }});
    setSamePageActivationState(host, true);
    return true;
  }}
  function applyVisibilityPreconditions(producerId, row) {{
    if (!producerId) return;
    document.querySelectorAll('[data-ghostweb-visibility-producer-action-id], [data-ghostweb-visibility-precondition-map]').forEach(function(el) {{
      var matches = [];
      try {{
        var mapped = JSON.parse(el.getAttribute('data-ghostweb-visibility-precondition-map') || '{{}}');
        Object.keys(mapped || {{}}).forEach(function(actionId) {{
          var entry = mapped[actionId] || {{}};
          if (String(entry.producer_action_id || '') === producerId) matches.push(entry);
        }});
      }} catch (error) {{}}
      if (el.getAttribute('data-ghostweb-visibility-producer-action-id') === producerId) {{
        matches.push({{ required_visible_value: el.getAttribute('data-ghostweb-required-visible-value') || '' }});
      }}
      if (!matches.length) return;
      revealPreconditionHost(el);
    }});
  }}
  function runtimeSnapshot() {{
    try {{
      if (window.GhostwebRuntimeState && typeof window.GhostwebRuntimeState.snapshot === 'function') {{
        return window.GhostwebRuntimeState.snapshot();
      }}
    }} catch (error) {{}}
    return null;
  }}
  function instanceActionId(host, sourceActionId) {{
    /* A source trace step describes an operation template, not necessarily a
       unique visible entity.  Preserve that source identity for contracts,
       while giving observations a stable instance suffix when the rendered
       host exposes entity-bearing DOM data. */
    if (!host || !host.attributes) return sourceActionId;
    const entityParts = [];
    Array.from(host.attributes).forEach(function(attribute) {{
      const name = String(attribute.name || '').toLowerCase();
      if (!name.startsWith('data-') ||
          name.startsWith('data-action-') ||
          name.startsWith('data-ghostweb-') ||
          name.startsWith('data-element-')) return;
      const value = String(attribute.value || '').trim();
      if (value) entityParts.push(name + '=' + value);
    }});
    if (!entityParts.length) return sourceActionId;
    entityParts.sort();
    let hash = 2166136261;
    const identity = entityParts.join('&');
    for (let index = 0; index < identity.length; index += 1) {{
      hash ^= identity.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }}
    return sourceActionId + '::instance=' + (hash >>> 0).toString(16);
  }}
  function recordObservedActionForHost(host, trigger, gestureMetadata) {{
    if (!host) return;
    runtimeActionIds(host).forEach(function(id) {{
      const row = ACTIONS[id] || {{}};
      applyVisibilityPreconditions(id, row);
      /* DOM lifecycle projection must run after the page-owned click handler.
         Running it from pointerup or capture-phase click can hide the host's
         overlay before its native listener advances to the next surface. */
      if (trigger === 'click') {{
        applySamePageActivationProjection(host, row);
        window.setTimeout(function() {{ applyContractDomProjection(host, row); }}, 0);
      }}
      const observedActionId = instanceActionId(host, id);
      const instancePrefix = id + '::instance=';
      const observation = {{
        action_id: observedActionId,
        source_action_id: id,
        instance_identity: observedActionId === id ? '' : observedActionId.slice(instancePrefix.length),
        trace_id: traceIdFromActionId(id),
        dom_binding_id: row.dom_binding_id || '',
        effect_kind: row.effect_kind || '',
        trigger: trigger || '',
        observed_page: decodeURIComponent(String(window.location.pathname || '').split('/').pop() || ''),
        observed_target_filename: String(host.getAttribute('data-ghostweb-target') || host.getAttribute('href') || '').replace(/^.*\\//, ''),
        observed_host_text: String(host.innerText || host.textContent || '').replace(/\\s+/g, ' ').trim(),
        observed_control_value: targetTextValue(host),
        observed_selected: host.getAttribute('data-selected') === 'true' || host.getAttribute('aria-selected') === 'true' || host.getAttribute('aria-checked') === 'true',
        observed_aria_expanded: host.getAttribute('aria-expanded') || '',
        runtime_snapshot: runtimeSnapshot(),
        gesture_event_count: gestureMetadata ? gestureMetadata.event_count : 0,
        gesture_events: gestureMetadata ? gestureMetadata.event_names : [],
        gesture_flush_reason: gestureMetadata ? gestureMetadata.flush_reason : '',
        observed_at_ms: Date.now()
      }};
      let observations = [];
      try {{ observations = JSON.parse(sessionStorage.getItem('__ghostweb_dom_action_observations_v1__') || '[]'); }} catch (error) {{}}
      if (!Array.isArray(observations)) observations = [];
      observations.push(observation);
      if (observations.length > 200) observations = observations.slice(-200);
      try {{ sessionStorage.setItem('__ghostweb_dom_action_observations_v1__', JSON.stringify(observations)); }} catch (error) {{}}
      window.__ghostwebLastDomActionObservation = observation;
      try {{ window.dispatchEvent(new CustomEvent('ghostweb:dom-action-observed', {{ detail: observation }})); }} catch (error) {{}}
    }});
  }}
  function isVisibleGestureHost(host) {{
    if (!host) return false;
    const rect = host.getBoundingClientRect ? host.getBoundingClientRect() : null;
    const style = window.getComputedStyle ? window.getComputedStyle(host) : null;
    return !!rect && rect.width > 0 && rect.height > 0 &&
      (!style || (style.display !== 'none' && style.visibility !== 'hidden'));
  }}
  function gestureHostForEvent(event) {{
    const direct = event.target && event.target.closest &&
      event.target.closest('[data-gesture-host][data-action-id], [data-gesture-host][data-action-aliases]');
    const context = (function() {{
      try {{ return window.__ghostwebExecutionContext || JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{{}}'); }}
      catch (error) {{ return {{}}; }}
    }})();
    const actionId = context && context.action_id ? String(context.action_id) : '';
    const traceId = context && (context.reference_trace_id || context.task_id) ? String(context.reference_trace_id || context.task_id) : '';
    const hosts = Array.from(document.querySelectorAll('[data-gesture-host][data-action-id], [data-gesture-host][data-action-aliases]'))
      .filter(isVisibleGestureHost);
    if (actionId) {{
      const byAction = hosts.find(function(host) {{ return readIds(host).indexOf(actionId) !== -1; }});
      /* An active non-gesture action (for example a button inside a page
         scroller) must not fall through to an unrelated gesture host merely
         because its pointerup bubbles through that container. A real gesture
         whose event target is inside a declared host is different: the
         execution context still names the preceding action until the host
         observes this gesture, so retain only that direct, producer-owned
         host as the no-context transition candidate. */
      return byAction || direct || null;
    }}
    if (traceId) {{
      const byTrace = hosts.find(function(host) {{
        return readIds(host).some(function(id) {{ return id.indexOf(traceId + ':step') === 0; }});
      }});
      return byTrace || null;
    }}
    /* Only unscoped native gestures may use DOM ancestry/fallback. With an
       active task context, the host must match that action/trace namespace. */
    return direct || hosts[0] || null;
  }}
  const gestureStarts = new WeakMap();
  function gestureCoordinate(event, axis) {{
    if (!event) return null;
    var point = event.changedTouches && event.changedTouches.length
      ? event.changedTouches[0]
      : event.touches && event.touches.length ? event.touches[0] : event;
    var coordinate = axis === 'vertical' ? point.clientY : point.clientX;
    return Number.isFinite(coordinate) ? coordinate : null;
  }}
  function gestureTransitionFor(host, actionId) {{
    if (!host || !actionId) return null;
    var transition = (ACTIONS[actionId] || {{}}).visible_window_transition;
    if (!transition || transition.effect !== 'advance_visible_window') return null;
    var stateRef = String(transition.state_ref || '').trim();
    if (!stateRef) return null;
    var hostStateRef = String(host.getAttribute('data-state-ref') || '').trim();
    if (hostStateRef && hostStateRef !== stateRef) return null;
    return transition;
  }}
  function gestureDeltaMatchesTransition(transition, delta) {{
    if (!transition || !Number.isFinite(delta) || Math.abs(delta) < 24) return false;
    var direction = String(transition.direction || '').toLowerCase();
    // Forward gesture progression moves a horizontal/vertical viewport toward
    // later content (left/up); backward moves it toward earlier content.
    if (direction === 'forward') return delta < 0;
    if (direction === 'backward') return delta > 0;
    return true;
  }}
  function commitVisibleWindowGesture(host, delta) {{
    if (!host) return false;
    var actionId = runtimeActionIds(host)[0] || '';
    var transition = gestureTransitionFor(host, actionId);
    if (!gestureDeltaMatchesTransition(transition, delta)) return false;
    var runtime = window.GhostwebRuntimeState;
    if (!runtime || typeof runtime.write !== 'function') return false;
    var value = {{ effect: 'advance_visible_window' }};
    ['axis', 'direction', 'reveals'].forEach(function(key) {{
      if (Object.prototype.hasOwnProperty.call(transition, key)) value[key] = transition[key];
    }});
    runtime.write(String(transition.state_ref), value, {{
      action_id: actionId,
      source: 'action_effect_materializer.visible_window_gesture'
    }});
    try {{
      window.dispatchEvent(new CustomEvent('ghostweb:visible-window-advanced', {{
        detail: {{ action_id: actionId, state_ref: transition.state_ref, value: value }}
      }}));
    }} catch (error) {{}}
    return true;
  }}
  function removeObservedAttr(el, name) {{
    if (el.hasAttribute(name)) el.removeAttribute(name);
  }}
  function applyHost(el) {{
    if (!el || el.nodeType !== 1) return;
    const ids = readIds(el);
    if (!ids.length) return;
    const active = activeActionId(el);
    /* Preserve the page-authored primary data-action-id for reducers while
       exposing the exact current task/trace alias to generic observation
       consumers.  This is a presentation of already materialized host
       identity, not a task answer or evaluator-derived state write. */
    if (active && ids.indexOf(active) !== -1) {{
      setObservedAttr(el, 'data-ghostweb-context-action-id', active);
    }} else {{
      removeObservedAttr(el, 'data-ghostweb-context-action-id');
    }}
    /* Keep the page-authored primary identity stable. Page-owned reducers may
       bind their listener during DOMContentLoaded with an exact
       ``[data-action-id=...]`` selector. Promoting a context-selected alias
       to primary before that listener runs disconnects the reducer and loses
       its business-state write. The active alias still selects routing and
       effect semantics below, while actionability continues to read both the
       primary and aliases. */
    let primary = el.getAttribute('data-action-id') || '';
    if (ids.indexOf(primary) === -1) primary = ids[0];
    setObservedAttr(el, 'data-action-id', primary);
    const aliases = ids.filter(function(id) {{ return id !== primary; }});
    if (aliases.length) setObservedAttr(el, 'data-action-aliases', aliases.join(' '));
    else removeObservedAttr(el, 'data-action-aliases');
    removeObservedAttr(el, 'data-ghostweb-click-contract-kind');
    removeObservedAttr(el, 'aria-disabled');
    const map = readMap(el);
    const staticTarget = el.getAttribute('data-ghostweb-target') || el.getAttribute('href') || '';
    const hasStaticRoute = Boolean(staticTarget) && (
      el.getAttribute('data-ghostweb-action') === 'navigate' ||
      el.hasAttribute('href') ||
      el.hasAttribute('onclick')
    );
    const effectiveAction = active && ids.indexOf(active) !== -1 ? active : primary;
    const effectiveRow = ACTIONS[effectiveAction] || ACTIONS[primary] || {{}};
    /* A sealed page can already carry an exact action-to-target mapping even
       when this bridge did not receive that action's compiled binding row.
       The bridge must not turn a current, page-authored route into a dead
       control merely because its dynamic projection is incomplete.  An alias
       cannot borrow the primary host's target: it needs its own exact map
       entry, so stale cross-target aliases continue to fail closed. */
    const staticMappedTarget = String(map[effectiveAction] || '').trim();
    const hasExactStaticRoute = Boolean(staticMappedTarget) && hasStaticRoute;
    let hasRoute = false;
    ids.forEach(function(id) {{
      const row = ACTIONS[id] || {{}};
      if (row.target && row.effect_kind !== 'same_page_mutation' && row.route_navigation !== false) {{
        map[id] = row.target;
        hasRoute = true;
      }}
    }});
    const effectiveHasRoute = Boolean(
      effectiveRow.target &&
      effectiveRow.effect_kind !== 'same_page_mutation' &&
      effectiveRow.route_navigation !== false
    );
    if (effectiveHasRoute) {{
      el.setAttribute('data-ghostweb-action', 'navigate');
      if (effectiveRow.target) el.setAttribute('data-ghostweb-target', effectiveRow.target);
      writeMap(el, map);
    }} else if (hasExactStaticRoute) {{
      el.setAttribute('data-ghostweb-action', 'navigate');
      el.setAttribute('data-ghostweb-target', staticMappedTarget);
    }} else if (!active && hasStaticRoute) {{
      // No context/formal route may erase the page-authored entity target.
      // This is the fallback after rejecting a stale cross-target alias.
      if (hasRoute) writeMap(el, map);
    }} else {{
      removeObservedAttr(el, 'data-ghostweb-action');
      removeObservedAttr(el, 'data-ghostweb-target');
      if (hasRoute) writeMap(el, map);
    }}
    if (effectiveRow.effect_kind) setObservedAttr(el, 'data-ghostweb-effect-kind', effectiveRow.effect_kind);
    if (effectiveRow.dom_binding_id) setObservedAttr(el, 'data-ghostweb-dom-binding-id', effectiveRow.dom_binding_id);
    hidePendingPreconditionHost(el);
  }}
  function scan(root) {{
    const scope = root && root.querySelectorAll ? root : document;
    let scannedHostCount = 0;
    if (scope.nodeType === 1) {{ applyHost(scope); scannedHostCount += 1; }}
    scope.querySelectorAll('[data-element-id], [data-element-aliases], [data-action-id], [data-action-aliases]').forEach(function(host) {{
      applyHost(host);
      scannedHostCount += 1;
    }});
    return scannedHostCount;
  }}
  /* Do not observe the HTML parser. This bridge is injected in <head>, so an
     eager document-wide observer used to materialize every parser-added node
     and then repeat the same work in the DOMContentLoaded full scan. The
     bridge's own action-identity writes also fed that observer. Initialize
     the static document once, then batch only genuine runtime mutations. */
  let hostObserver = null;
  let pendingScanRoots = [];
  let scanFrame = 0;
  function queueScan(root) {{
    if (!root || (root.nodeType !== 1 && root.nodeType !== 9)) return;
    if (pendingScanRoots.some(function(pending) {{
      return pending === root || (pending.contains && pending.contains(root));
    }})) return;
    pendingScanRoots = pendingScanRoots.filter(function(pending) {{
      return !(root.contains && root.contains(pending));
    }});
    pendingScanRoots.push(root);
    if (scanFrame) return;
    const flush = function() {{
      scanFrame = 0;
      const roots = pendingScanRoots;
      pendingScanRoots = [];
      roots.forEach(scan);
    }};
    scanFrame = window.requestAnimationFrame ? window.requestAnimationFrame(flush) : window.setTimeout(flush, 0);
  }}
  function installHostObserver() {{
    if (hostObserver || typeof MutationObserver !== 'function' || !document.documentElement) return;
    hostObserver = new MutationObserver(function(records) {{
      records.forEach(function(record) {{
        record.addedNodes && record.addedNodes.forEach(function(node) {{ queueScan(node); }});
        if (record.type === 'attributes') queueScan(record.target);
      }});
    }});
    hostObserver.observe(document.documentElement, {{ childList: true, subtree: true, attributes: true, attributeFilter: ['data-element-id', 'data-element-aliases', 'data-action-id', 'data-action-aliases'] }});
  }}
  function initializeHostBridge() {{
    scan(document);
    installHostObserver();
  }}
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initializeHostBridge, {{ once: true }});
  else initializeHostBridge();
  function recordContextHostRefresh(status, actionId, candidates, scannedHostCount) {{
    const diagnostic = {{
      status: status,
      action_id: actionId || '',
      candidate_count: candidates.length,
      candidate_ids: candidates.map(function(host) {{ return host.id || ''; }}),
      scanned_host_count: scannedHostCount || 0,
      refreshed_subtree_root_id: candidates.length === 1 ? (candidates[0].id || '') : ''
    }};
    window.__ghostwebLastContextHostRefresh = diagnostic;
    try {{ window.dispatchEvent(new CustomEvent('ghostweb:task-context-host-refresh', {{ detail: diagnostic }})); }} catch (error) {{}}
  }}
  function refreshCurrentContextHost(event) {{
    flushPendingGestureObservations('before_context_action_change');
    const detail = event && event.detail && typeof event.detail === 'object' ? event.detail : {{}};
    const context = detail.context && typeof detail.context === 'object'
      ? detail.context
      : (window.__ghostwebExecutionContext || {{}});
    const actionId = String(detail.action_id || context.action_id || '').trim();
    if (!actionId) {{
      recordContextHostRefresh('fail_closed_missing_action_contract', actionId, [], 0);
      return;
    }}
    const candidates = Array.from(document.querySelectorAll('[data-action-id], [data-action-aliases]')).filter(function(host) {{
      return readIds(host).indexOf(actionId) !== -1;
    }});
    const hasDynamicContract = Object.prototype.hasOwnProperty.call(ACTIONS, actionId);
    const exactStaticRouteCandidates = candidates.filter(function(host) {{
      const target = String(readMap(host)[actionId] || '').trim();
      const declaredRoute = host.getAttribute('data-ghostweb-action') === 'navigate' ||
        host.hasAttribute('href') || host.hasAttribute('onclick') ||
        Boolean(host.getAttribute('data-ghostweb-target'));
      return Boolean(target) && declaredRoute;
    }});
    if (!hasDynamicContract && exactStaticRouteCandidates.length !== 1) {{
      recordContextHostRefresh('fail_closed_missing_action_contract', actionId, candidates, 0);
      return;
    }}
    if (candidates.length !== 1) {{
      recordContextHostRefresh(
        candidates.length === 0 ? 'fail_closed_missing_host' : 'fail_closed_ambiguous_host',
        actionId,
        candidates,
        0
      );
      return;
    }}
    /* Initial load owns the only full-document scan. Context changes refresh
       only the unique subtree that consumes the current action contract;
       dynamic insertions remain owned by the batched MutationObserver. */
    const scannedHostCount = scan(candidates[0]);
    recordContextHostRefresh(
      hasDynamicContract ? 'refreshed_unique_host_subtree' : 'refreshed_unique_static_route_host',
      actionId,
      candidates,
      scannedHostCount
    );
  }}
  window.addEventListener('ghostweb:task-context-update', function(event) {{
    /* The unique current contract host must update synchronously so a click
       issued in this same host turn sees its current route/effect projection.
       Missing or ambiguous hosts fail closed instead of scanning unrelated
       document content. */
    refreshCurrentContextHost(event);
  }});
  document.addEventListener('click', function(event) {{
    /* A click may navigate or terminate the current surface. Preserve any
       final gesture observation before the owning page can disappear. */
    flushPendingGestureObservations('before_click_or_navigation');
    const host = event.target && event.target.closest && event.target.closest('[data-action-id], [data-action-aliases]');
    if (!host || host.closest('[data-ghostweb-noninteractive-notice]')) return;
    const active = activeActionId(host);
    if (active) synchronizeExecutionContextAction(active);
    /* Observe after page-owned reducers have handled the real GUI action. */
    window.setTimeout(function() {{ recordObservedActionForHost(host, 'click'); }}, 0);
  }}, true);
  document.addEventListener('input', function(event) {{
    const host = event.target && event.target.closest && event.target.closest('[data-action-id], [data-action-aliases]');
    if (!host) return;
    recordObservedActionForHost(host, 'input');
  }}, true);
  document.addEventListener('change', function(event) {{
    const host = event.target && event.target.closest && event.target.closest('[data-action-id], [data-action-aliases]');
    if (!host) return;
    recordObservedActionForHost(host, 'change');
  }}, true);
  let pendingGestureFrame = 0;
  let pendingGestureSequence = 0;
  let pendingGestureObservations = new Map();
  const gestureHostIds = new WeakMap();
  function gestureHostId(host) {{
    if (!gestureHostIds.has(host)) gestureHostIds.set(host, ++pendingGestureSequence);
    return gestureHostIds.get(host);
  }}
  function pendingGestureKey(host) {{
    const actionId = runtimeActionIds(host)[0] || '';
    return actionId + '::host-' + gestureHostId(host);
  }}
  function queueGestureObservation(host, eventName) {{
    if (!host) return;
    const key = pendingGestureKey(host);
    const actionId = runtimeActionIds(host)[0] || '';
    const pending = pendingGestureObservations.get(key) || {{
      host: host,
      action_id: actionId,
      event_count: 0,
      event_names: [],
      last_event_name: ''
    }};
    pending.event_count += 1;
    if (pending.event_names.indexOf(eventName) === -1) pending.event_names.push(eventName);
    pending.last_event_name = eventName;
    pendingGestureObservations.set(key, pending);
    if (pendingGestureFrame) return;
    const flush = function() {{
      pendingGestureFrame = 0;
      flushPendingGestureObservations('animation_frame');
    }};
    pendingGestureFrame = window.requestAnimationFrame
      ? window.requestAnimationFrame(flush)
      : window.setTimeout(flush, 0);
  }}
  function flushPendingGestureObservations(reason) {{
    if (pendingGestureFrame) {{
      if (window.cancelAnimationFrame) window.cancelAnimationFrame(pendingGestureFrame);
      else window.clearTimeout(pendingGestureFrame);
      pendingGestureFrame = 0;
    }}
    const pending = Array.from(pendingGestureObservations.values());
    pendingGestureObservations = new Map();
    pending.forEach(function(entry) {{
      recordObservedActionForHost(entry.host, entry.last_event_name, {{
        event_count: entry.event_count,
        event_names: entry.event_names.slice(),
        flush_reason: reason || 'explicit'
      }});
    }});
    window.__ghostwebLastGestureObservationFlush = {{
      reason: reason || 'explicit',
      observation_count: pending.length,
      input_event_count: pending.reduce(function(total, entry) {{ return total + entry.event_count; }}, 0),
      action_ids: pending.map(function(entry) {{ return entry.action_id; }})
    }};
    return window.__ghostwebLastGestureObservationFlush;
  }}
  window.__ghostwebFlushPendingGestureObservations = flushPendingGestureObservations;
  ['pointerdown', 'touchstart'].forEach(function(eventName) {{
    document.addEventListener(eventName, function(event) {{
      var host = gestureHostForEvent(event);
      if (!host) return;
      var actionId = runtimeActionIds(host)[0] || '';
      var transition = gestureTransitionFor(host, actionId);
      if (!transition) return;
      var axis = String(transition.axis || host.getAttribute('data-gesture-axis') || 'horizontal').toLowerCase();
      var coordinate = gestureCoordinate(event, axis);
      if (coordinate !== null) gestureStarts.set(host, coordinate);
    }}, true);
  }});
  ['scroll', 'wheel'].forEach(function(eventName) {{
    document.addEventListener(eventName, function(event) {{
      if (eventName === 'wheel') {{
        var wheelHost = gestureHostForEvent(event);
        if (wheelHost) {{
          var wheelActionId = runtimeActionIds(wheelHost)[0] || '';
          var wheelTransition = gestureTransitionFor(wheelHost, wheelActionId);
          if (wheelTransition) {{
            var wheelAxis = String(wheelTransition.axis || wheelHost.getAttribute('data-gesture-axis') || 'horizontal').toLowerCase();
            var wheelDelta = wheelAxis === 'vertical' ? Number(event.deltaY) : Number(event.deltaX);
            commitVisibleWindowGesture(wheelHost, wheelDelta);
          }}
        }}
      }}
      queueGestureObservation(gestureHostForEvent(event), eventName);
    }}, true);
  }});
  ['pointerup', 'touchend'].forEach(function(eventName) {{
    document.addEventListener(eventName, function(event) {{
      var host = gestureHostForEvent(event);
      if (host) {{
        var actionId = runtimeActionIds(host)[0] || '';
        var transition = gestureTransitionFor(host, actionId);
        if (transition) {{
          var axis = String(transition.axis || host.getAttribute('data-gesture-axis') || 'horizontal').toLowerCase();
          var start = gestureStarts.get(host);
          var end = gestureCoordinate(event, axis);
          if (Number.isFinite(start) && end !== null) commitVisibleWindowGesture(host, end - start);
          gestureStarts.delete(host);
        }}
      }}
      queueGestureObservation(host, eventName);
      /* Bubble phase runs after the page-owned target/ancestor gesture
         reducer, so the flushed snapshot contains the final gesture state. */
      flushPendingGestureObservations('final_gesture_boundary');
    }}, false);
  }});
  ['pagehide', 'beforeunload', 'submit'].forEach(function(eventName) {{
    window.addEventListener(eventName, function() {{
      flushPendingGestureObservations('before_navigation_or_submit');
    }}, true);
  }});
  window.addEventListener('ghostweb:before-terminate', function() {{
    flushPendingGestureObservations('before_terminate');
  }});
  window.addEventListener('ghostweb:before-evaluator-read', function() {{
    flushPendingGestureObservations('before_evaluator_read');
  }});
}})();
</script>"""


_CHARSET_META_RE = re.compile(
    r"\s*<meta\b(?=[^>]*\bcharset\s*=)[^>]*>\s*",
    flags=re.IGNORECASE,
)


def _ensure_utf8_charset_meta(html_text: str) -> str:
    html_text = _CHARSET_META_RE.sub("\n", html_text)
    meta = '<meta charset="utf-8">'
    head_match = re.search(r"<head\b[^>]*>", html_text, flags=re.IGNORECASE)
    if not head_match:
        raise ValueError("runtime injection requires a <head> tag")
    return html_text[:head_match.end()] + "\n" + meta + html_text[head_match.end():]


def _inject_head_runtime_after_charset(html_text: str, runtime: str) -> str:
    html_text = _ensure_utf8_charset_meta(html_text)
    head_match = re.search(r"<head\b[^>]*>", html_text, flags=re.IGNORECASE)
    if not head_match:
        raise ValueError("runtime injection requires a <head> tag")
    charset_match = re.match(
        r"\s*<meta\b(?=[^>]*\bcharset\s*=)[^>]*>\s*",
        html_text[head_match.end():],
        flags=re.IGNORECASE,
    )
    insert_at = head_match.end() + (charset_match.end() if charset_match else 0)
    return html_text[:insert_at] + "\n" + runtime + html_text[insert_at:]


def _ensure_runtime(html_text: str) -> str:
    html_text = re.sub(
        r"<script\b[^>]*data-ghostweb-runtime=[\"']action-effect-materializer[\"'][^>]*>[\s\S]*?</script>\s*",
        "",
        html_text,
        flags=re.IGNORECASE,
    )
    try:
        return _inject_head_runtime_after_charset(html_text, _MATERIALIZER_RUNTIME)
    except ValueError as exc:
        raise ValueError("action-effect materializer runtime injection requires a <head> tag") from exc


_DOM_BINDING_RUNTIME_BLOCK_RE = re.compile(
    r"<script\b[^>]*data-ghostweb-runtime=[\"']dom-binding-action-host-bridge[\"'][^>]*>(?P<body>[\s\S]*?)</script>\s*",
    flags=re.IGNORECASE,
)
_DOM_BINDING_ACTIONS_ASSIGNMENT_RE = re.compile(
    r"window\.__GHOSTWEB_DOM_BINDING_ACTIONS\s*=\s*(?P<payload>\{[\s\S]*?\})\s*;\s*const\s+ACTIONS",
)


def _existing_dom_binding_runtime_bindings(
    html_text: str,
    *,
    source_filename: str,
) -> List[Dict[str, Any]]:
    match = _DOM_BINDING_RUNTIME_BLOCK_RE.search(html_text)
    if not match:
        return []
    assignment = _DOM_BINDING_ACTIONS_ASSIGNMENT_RE.search(match.group("body"))
    if not assignment:
        return []
    try:
        rows = json.loads(assignment.group("payload"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(rows, dict):
        return []
    recovered: List[Dict[str, Any]] = []
    for action_id, row in rows.items():
        if not isinstance(row, dict) or not _text(action_id):
            continue
        runtime_state_diff = _as_dict(row.get("state_diff"))
        visible_window_transition = _as_dict(row.get("visible_window_transition"))
        if not visible_window_transition:
            for ref, raw_value in runtime_state_diff.items():
                value = _as_dict(raw_value)
                if value.get("effect") == "advance_visible_window":
                    visible_window_transition = {"state_ref": _text(ref), **value}
                    break
        interaction_payload: Dict[str, Any] = {"effect_kind": _text(row.get("effect_kind"))}
        postcondition: Dict[str, Any] = {"target_filename": _text(row.get("target"))}
        if visible_window_transition:
            state_ref = _text(visible_window_transition.get("state_ref"))
            transition_value = {
                key: visible_window_transition[key]
                for key in ("effect", "axis", "direction", "reveals")
                if key in visible_window_transition
            }
            if state_ref and transition_value.get("effect") == "advance_visible_window":
                interaction_payload.update({
                    "gesture_scope": "component_visible_window",
                    "gesture_axis": _text(transition_value.get("axis")),
                    "gesture_direction": _text(transition_value.get("direction")),
                    "state_refs": [state_ref],
                })
                postcondition["state_diff"] = {state_ref: transition_value}
                interaction_payload["event"] = "swipe"
        recovered.append({
            "action_id": _text(action_id),
            "dom_binding_id": _text(row.get("dom_binding_id")),
            "source_filename": source_filename,
            "element_identity": {"component_ref": _text(row.get("component_ref"))},
            "interaction": {"event": interaction_payload.pop("event", ""), "payload": interaction_payload},
            "postcondition": postcondition,
        })
    return recovered


def _merge_dom_binding_runtime_bindings(
    existing: Iterable[Mapping[str, Any]],
    current: Iterable[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    by_action: Dict[str, Mapping[str, Any]] = {}
    for binding in [*existing, *current]:
        action_id = _binding_action_id(binding)
        if action_id:
            by_action[action_id] = binding
    return [by_action[action_id] for action_id in sorted(by_action)]


def _ensure_dom_binding_runtime(html_text: str, bindings: Iterable[Mapping[str, Any]]) -> str:
    html_text = _DOM_BINDING_RUNTIME_BLOCK_RE.sub("", html_text, count=1)
    runtime = _dom_binding_runtime(bindings)
    if not runtime:
        return html_text
    try:
        return _inject_head_runtime_after_charset(html_text, runtime)
    except ValueError as exc:
        raise ValueError("dom binding runtime injection requires a <head> tag") from exc


def materialize_action_effect_dom(
    *,
    dom_binding_contracts: Mapping[str, Any],
    generated_pages_dir: Path,
    write_modified: bool = True,
    excluded_action_ids: Optional[Iterable[str]] = None,
    included_page_filenames: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    excluded_actions = sorted({_text(action_id) for action_id in (excluded_action_ids or []) if _text(action_id)})
    included_pages = (
        {_text(filename) for filename in included_page_filenames if _text(filename)}
        if included_page_filenames is not None
        else None
    )
    bindings_by_page: Dict[str, List[Mapping[str, Any]]] = {}
    for binding in _as_list(dom_binding_contracts.get("bindings")):
        binding = _as_dict(binding)
        source = _text(binding.get("source_filename"))
        if not source:
            continue
        effect = _binding_effect(binding)
        event = _binding_event(binding)
        if _is_stateful_gesture_binding(binding):
            bindings_by_page.setdefault(source, []).append(binding)
        elif effect == "query_projection" or event in {"fill", "type", "input"}:
            bindings_by_page.setdefault(source, []).append(binding)
        elif event in {"click", "tap", "double_click", "dblclick", "submit", "press"} and _binding_target(binding):
            bindings_by_page.setdefault(source, []).append(binding)

    reports: List[Dict[str, Any]] = []
    modified_pages: Dict[str, str] = {}
    pages_to_visit = set(bindings_by_page)
    if excluded_actions:
        pages_to_visit.update(path.name for path in generated_pages_dir.glob("*.html"))
    for path in generated_pages_dir.glob("*.html"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if (
            'data-ghostweb-runtime="action-effect-materializer"' in text
            or "data-ghostweb-runtime='action-effect-materializer'" in text
            or 'data-ghostweb-runtime="dom-binding-action-host-bridge"' in text
            or "data-ghostweb-runtime='dom-binding-action-host-bridge'" in text
        ):
            pages_to_visit.add(path.name)
    if included_pages is not None:
        pages_to_visit.intersection_update(included_pages)

    for source in sorted(pages_to_visit):
        path = generated_pages_dir / source
        if not path.exists():
            for binding in bindings_by_page.get(source, []):
                reports.append({
                    "action_id": _binding_action_id(binding),
                    "source_filename": source,
                    "effect_kind": _binding_effect(binding),
                    "status": "page_missing",
                    "reason": f"generated page not found: {source}",
                })
            continue
        html_text = path.read_text(encoding="utf-8")
        original = html_text
        existing_runtime_bindings = _existing_dom_binding_runtime_bindings(
            html_text,
            source_filename=source,
        )


        html_text = _DOM_BINDING_RUNTIME_BLOCK_RE.sub("", html_text, count=1)
        html_text, script_blocks = _protect_script_blocks(html_text)
        html_text = _normalize_malformed_input_open_tags(html_text)
        html_text = _remove_malformed_target_map_fragments(html_text)
        stripped_excluded = 0
        for action_id in excluded_actions:
            if action_id in html_text:
                html_text = _strip_action_id_from_other_hosts(html_text, action_id)
                stripped_excluded += 1
        if stripped_excluded:
            reports.append({
                "action_id": "",
                "source_filename": source,
                "effect_kind": "excluded_action_cleanup",
                "status": "stale_action_removed",
                "reason": f"removed {stripped_excluded} excluded action id(s)",
            })
        for binding in bindings_by_page.get(source, []):
            effect = _binding_effect(binding)
            event = _binding_event(binding)
            if _is_stateful_gesture_binding(binding):
                html_text, report = _materialize_gesture_binding(html_text, binding)
            elif effect == "query_projection" or event in {"fill", "type", "input"}:
                html_text, report = _materialize_query_binding(html_text, binding)

                if event in {"fill", "type", "input"}:
                    html_text, migrated = _migrate_fill_action_to_input(html_text, binding)
                    if migrated and report.get("status") == "bound":
                        report["reason"] = (report.get("reason") or "") + " (migrated to input child)"
            else:
                html_text, report = _materialize_click_binding(html_text, binding)
            reports.append(report)
        html_text = reconcile_unique_action_identity_hosts(
            html_text,
            bindings_by_page.get(source, []),
        )
        html_text = _reconcile_action_target_maps(html_text, bindings_by_page.get(source, []))
        html_text = reconcile_static_route_target_aliases(
            html_text,
            bindings_by_page.get(source, []),
        )
        html_text = _cleanup_orphan_visibility_preconditions(html_text)
        html_text = _restore_script_blocks(html_text, script_blocks)
        html_text = _normalize_malformed_input_open_tags(html_text)
        html_text = _remove_malformed_target_map_fragments(html_text)
        html_text = _ensure_runtime(html_text)
        html_text = _ensure_dom_binding_runtime(
            html_text,
            _merge_dom_binding_runtime_bindings(
                existing_runtime_bindings,
                bindings_by_page.get(source, []),
            ),
        )
        if html_text != original:
            modified_pages[source] = html_text
            if write_modified:
                path.write_text(html_text, encoding="utf-8")

    status_counts: Dict[str, int] = {}
    for report in reports:
        status = _text(report.get("status")) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "format": "action_effect_dom_materialization",
        "schema_revision": 1,
        "summary": {
            "binding_count": len(reports),
            "modified_page_count": len(modified_pages),
            "status_counts": dict(sorted(status_counts.items())),
            "excluded_action_cleanup_count": sum(
                1 for report in reports
                if _text(report.get("status")) == "stale_action_removed"
            ),
            "unbound_count": sum(
                1 for report in reports
                if _text(report.get("status")) in {"host_not_found", "page_missing"}
            ),
        },
        "bindings": reports,
        "modified_pages": sorted(modified_pages),
    }
