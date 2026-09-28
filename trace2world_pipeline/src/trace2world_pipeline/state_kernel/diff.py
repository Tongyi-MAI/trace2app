from __future__ import annotations

from typing import Any


def typed_diff(before: Any, after: Any, path: str = "$") -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}"
            if key not in before:
                changes.append({"operation": "field_added", "path": child, "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"operation": "field_removed", "path": child, "before": before[key], "after": None})
            else:
                changes.extend(typed_diff(before[key], after[key], child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        before_by_id = {str(item.get("id")): item for item in before if isinstance(item, dict) and item.get("id") is not None}
        after_by_id = {str(item.get("id")): item for item in after if isinstance(item, dict) and item.get("id") is not None}
        if len(before_by_id) == len(before) and len(after_by_id) == len(after):
            changes = []
            for entity_id in sorted(set(before_by_id) | set(after_by_id)):
                child = f"{path}[id={entity_id}]"
                if entity_id not in before_by_id:
                    changes.append({"operation": "entity_added", "path": child, "before": None, "after": after_by_id[entity_id]})
                elif entity_id not in after_by_id:
                    changes.append({"operation": "entity_removed", "path": child, "before": before_by_id[entity_id], "after": None})
                else:
                    changes.extend(typed_diff(before_by_id[entity_id], after_by_id[entity_id], child))
            return changes
        return [{"operation": "ordered_sequence_changed", "path": path, "before": before, "after": after}]
    operation = "numeric_delta" if isinstance(before, (int, float)) and isinstance(after, (int, float)) else "field_replaced"
    item = {"operation": operation, "path": path, "before": before, "after": after}
    if operation == "numeric_delta":
        item["delta"] = after - before
    return [item]
