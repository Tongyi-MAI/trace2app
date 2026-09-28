from __future__ import annotations

import re
from typing import Any, Dict


_STEP_KEY_RE = re.compile(r"^(.+?):(?:step)?(\d+)$")


def canonical_step_key(value: Any) -> str:
    text = str(value or "").strip()
    match = _STEP_KEY_RE.match(text)
    if not match:
        return text
    return f"{match.group(1)}:step{int(match.group(2))}"


def legacy_step_key_alias(value: Any) -> str:
    canonical = canonical_step_key(value)
    match = re.match(r"^(.+?):step(\d+)$", canonical)
    if not match:
        return canonical
    return f"{match.group(1)}:{int(match.group(2))}"


def normalize_step_keyed_mapping(
    value: Any,
    *,
    include_legacy_alias: bool = False,
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        canonical = canonical_step_key(raw_key)
        if not canonical:
            continue
        result[canonical] = raw_value
        if include_legacy_alias:
            result[legacy_step_key_alias(canonical)] = raw_value
    return result
