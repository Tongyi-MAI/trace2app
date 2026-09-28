
from __future__ import annotations

from typing import Any, Dict, List


def _field_entries_to_item(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("fields"), list):
        item: Dict[str, Any] = {}
        for field in raw.get("fields", []):
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "").strip()
            if not name:
                continue
            item[name] = field.get("value", "")
        for key in ("item_ref", "source_component", "reason"):
            value = raw.get(key)
            if value not in (None, "", [], {}):
                item[key] = value
        evidence = raw.get("evidence_steps")
        if isinstance(evidence, list) and evidence:
            item["evidence_steps"] = evidence
        return item
    if isinstance(raw, dict):
        return dict(raw)
    return {"value": str(raw)}


def _entity_groups_to_map(groups: Any) -> Dict[str, List[Dict[str, Any]]]:
    mapped: Dict[str, List[Dict[str, Any]]] = {}
    if isinstance(groups, dict):
        for entity, items in groups.items():
            if entity == "_metadata":
                continue
            if isinstance(items, list):
                mapped[str(entity)] = [
                    _field_entries_to_item(item)
                    for item in items
                ]
        return mapped
    if not isinstance(groups, list):
        return mapped
    for group in groups:
        if not isinstance(group, dict):
            continue
        entity = str(group.get("entity") or "").strip()
        if not entity:
            continue
        items = group.get("items", [])
        if not isinstance(items, list):
            items = []
        mapped.setdefault(entity, []).extend(_field_entries_to_item(item) for item in items)
    return mapped


def normalize_content_extraction_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    normalized = dict(payload)

    metadata = payload.get("metadata", {})
    extracted_items: Dict[str, Any]
    if isinstance(payload.get("extracted_items"), dict):
        extracted_items = dict(payload.get("extracted_items") or {})
        if isinstance(metadata, dict) and metadata:
            extracted_items.setdefault("_metadata", dict(metadata))
    else:
        extracted_items = _entity_groups_to_map(payload.get("extracted_item_groups"))
        extracted_items["_metadata"] = dict(metadata) if isinstance(metadata, dict) else {}
    normalized["extracted_items"] = extracted_items

    if isinstance(payload.get("runtime_items"), dict):
        normalized["runtime_items"] = dict(payload.get("runtime_items") or {})
    else:
        normalized["runtime_items"] = _entity_groups_to_map(payload.get("runtime_item_groups"))

    if isinstance(payload.get("entity_task_relevance"), dict):
        normalized["entity_task_relevance"] = dict(payload.get("entity_task_relevance") or {})
    else:
        relevance: Dict[str, Dict[str, Any]] = {}
        entries = payload.get("entity_task_relevance_entries", [])
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            entity = str(entry.get("entity") or "").strip()
            if not entity:
                continue
            relevance[entity] = {
                "task_related": bool(entry.get("task_related")),
                "role": str(entry.get("role") or ""),
                "evidence_steps": entry.get("evidence_steps", [])
                if isinstance(entry.get("evidence_steps"), list)
                else [],
                "task_instruction_refs": entry.get("task_instruction_refs", [])
                if isinstance(entry.get("task_instruction_refs"), list)
                else [],
                "reason": str(entry.get("reason") or ""),
            }
        normalized["entity_task_relevance"] = relevance

    for key in ("excluded_from_initial", "item_lifecycle", "entity_component_mapping"):
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    return normalized
