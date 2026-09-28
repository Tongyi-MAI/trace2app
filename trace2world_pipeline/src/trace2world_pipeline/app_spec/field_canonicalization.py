
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Tuple


TRUE_TOKENS = {
    "1",
    "true",
    "yes",
    "y",
    "on",
    "enabled",
    "selected",
    "checked",
    "active",
    "是",
    "有",
    "已选",
    "已选中",
    "选中",
    "勾选",
    "已勾选",
    "默认",
    "开启",
    "打开",
    "启用",
}

FALSE_TOKENS = {
    "",
    "0",
    "false",
    "no",
    "n",
    "off",
    "disabled",
    "unselected",
    "unchecked",
    "inactive",
    "否",
    "无",
    "未选",
    "未选中",
    "未勾选",
    "非默认",
    "关闭",
    "禁用",
}


def _snake_case(name: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", str(name or ""))
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return value


def _field_specs(entity_schemas: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    specs: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for entity in entity_schemas or []:
        if not isinstance(entity, dict):
            continue
        entity_name = str(entity.get("name", "") or "").strip()
        if not entity_name:
            continue
        fields: Dict[str, Dict[str, Any]] = {}
        for field in entity.get("fields", []) or []:
            if not isinstance(field, dict):
                continue
            name = _snake_case(field.get("name", ""))
            if not name:
                continue
            fields[name] = {
                "name": name,
                "type": str(field.get("type", "") or "").strip().lower(),
            }
        specs[entity_name] = fields
    return specs


def _value_signature(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value).strip()


def _non_empty_signatures(values: List[Any]) -> set[str]:
    return {item for item in (_value_signature(value) for value in values) if item}


def _field_values(records: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    values: Dict[str, List[Any]] = defaultdict(list)
    for record in records or []:
        if not isinstance(record, dict):
            continue
        for key, value in record.items():
            values[_snake_case(key)].append(value)
    return values


def _is_boolean_field(name: str, spec: Dict[str, Any] | None) -> bool:
    if name.endswith("_badge") or name.endswith("_text"):
        return False
    if spec and str(spec.get("type", "") or "").lower() in {"bool", "boolean"}:
        return True
    return bool(re.match(r"^(is|has|can|should|selected|enabled)_", name))


def _normalize_boolean(value: Any) -> Tuple[bool | Any, bool]:
    if isinstance(value, bool):
        return value, False
    text = str(value if value is not None else "").strip()
    lowered = text.lower()
    if lowered in TRUE_TOKENS or text in TRUE_TOKENS:
        return True, True
    if lowered in FALSE_TOKENS or text in FALSE_TOKENS:
        return False, True
    return value, False


def build_field_canonicalization_contract(
    entity_schemas: List[Dict[str, Any]],
    content_samples: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    specs = _field_specs(entity_schemas)
    entities: Dict[str, Any] = {}
    for entity_name, records in sorted((content_samples or {}).items()):
        if not isinstance(records, list):
            continue
        fields = specs.get(entity_name, {})
        if not fields:
            continue
        values_by_field = _field_values(records)
        aliases: Dict[str, str] = {}
        alias_reasons: Dict[str, str] = {}
        canonical_names = set(fields.keys())
        canonical_value_sets = {
            field: _non_empty_signatures(values)
            for field, values in values_by_field.items()
            if field in canonical_names
        }
        for raw_field, raw_values in sorted(values_by_field.items()):
            if raw_field in canonical_names:
                continue
            raw_values_set = _non_empty_signatures(raw_values)
            if not raw_values_set:
                continue
            best_field = ""
            best_overlap = 0
            for canonical_field, canonical_values in canonical_value_sets.items():
                if raw_field.endswith("_badge") and _is_boolean_field(canonical_field, fields.get(canonical_field)):
                    continue
                if not canonical_values:
                    continue
                overlap = len(raw_values_set & canonical_values)
                if overlap > best_overlap:
                    best_field = canonical_field
                    best_overlap = overlap
            if best_field and best_overlap:
                aliases[raw_field] = best_field
                alias_reasons[raw_field] = "matched observed values with canonical field"
        boolean_fields = sorted(
            field_name for field_name, spec in fields.items() if _is_boolean_field(field_name, spec)
        )
        if aliases or boolean_fields:
            entities[entity_name] = {
                "aliases": aliases,
                "alias_reasons": alias_reasons,
                "boolean_fields": boolean_fields,
            }
    return {
        "format": "field_canonicalization_contract",
        "schema_revision": 1,
        "entities": entities,
    }


def canonicalize_content_samples(
    content_samples: Dict[str, List[Dict[str, Any]]],
    contract: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    entities = contract.get("entities", {}) if isinstance(contract, dict) else {}
    canonical_samples: Dict[str, List[Dict[str, Any]]] = {}
    decisions: List[Dict[str, Any]] = []
    for entity_name, records in sorted((content_samples or {}).items()):
        if not isinstance(records, list):
            continue
        entity_contract = entities.get(entity_name, {}) if isinstance(entities, dict) else {}
        aliases = entity_contract.get("aliases", {}) if isinstance(entity_contract, dict) else {}
        boolean_fields = set(entity_contract.get("boolean_fields", []) if isinstance(entity_contract, dict) else [])
        canonical_records: List[Dict[str, Any]] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            canonical: Dict[str, Any] = {}
            for raw_key, raw_value in record.items():
                normalized_key = _snake_case(raw_key)
                canonical_key = str(aliases.get(normalized_key, normalized_key) or normalized_key)
                value = raw_value
                changed = canonical_key != normalized_key
                if canonical_key in boolean_fields:
                    value, bool_changed = _normalize_boolean(value)
                    changed = changed or bool_changed
                if canonical.get(canonical_key) in (None, "") and value not in (None, ""):
                    canonical[canonical_key] = value
                else:
                    canonical.setdefault(canonical_key, value)
                if changed:
                    decisions.append({
                        "entity_name": entity_name,
                        "record_index": index,
                        "source_field": normalized_key,
                        "canonical_field": canonical_key,
                        "source_value": raw_value,
                        "canonical_value": value,
                    })
            canonical_records.append(canonical)
        canonical_samples[entity_name] = canonical_records
    return canonical_samples, {
        "format": "field_canonicalization_decisions",
        "schema_revision": 1,
        "decision_count": len(decisions),
        "decisions": decisions,
    }
