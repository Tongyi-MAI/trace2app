
from __future__ import annotations

from copy import deepcopy
import difflib
import json
from typing import Any, Dict, List, Tuple


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _unified_diff(old: Any, new: Any, target_id: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            _json_text(old).splitlines(),
            _json_text(new).splitlines(),
            fromfile=f"{target_id}:before",
            tofile=f"{target_id}:after",
            lineterm="",
            n=4,
        )
    )


def _first_changed_line(old: Any, new: Any) -> int | None:
    old_lines = _json_text(old).splitlines()
    new_lines = _json_text(new).splitlines()
    for index, (old_line, new_line) in enumerate(zip(old_lines, new_lines), start=1):
        if old_line != new_line:
            return index
    if len(old_lines) != len(new_lines):
        return min(len(old_lines), len(new_lines)) + 1
    return None


def _decode_pointer(path: str) -> List[str]:
    if path == "":
        return []
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must be a JSON pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]


def _path_matches(path: str, prefixes: List[str]) -> bool:
    for prefix in prefixes:
        if not prefix:
            continue
        normalized = prefix.rstrip("/") or "/"
        if path == normalized or path.startswith(normalized + "/"):
            return True
    return False


def _resolve_parent(document: Any, path: str) -> Tuple[Any, str]:
    parts = _decode_pointer(path)
    if not parts:
        raise ValueError("path must not target the document root")
    current = document
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"missing object key: {part}")
            current = current[part]
            continue
        if isinstance(current, list):
            if part == "-":
                raise KeyError("cannot traverse list append marker")
            index = int(part)
            current = current[index]
            continue
        raise TypeError("path traverses a non-container value")
    return current, parts[-1]


def _set_value(document: Any, path: str, value: Any) -> None:
    parent, key = _resolve_parent(document, path)
    if isinstance(parent, dict):
        if key not in parent:
            raise KeyError(f"missing object key: {key}")
        parent[key] = value
        return
    if isinstance(parent, list):
        index = int(key)
        parent[index] = value
        return
    raise TypeError("set target parent is not a container")


def _add_value(document: Any, path: str, value: Any) -> None:
    parent, key = _resolve_parent(document, path)
    if isinstance(parent, dict):
        if key in parent:
            raise KeyError(f"object key already exists: {key}")
        parent[key] = value
        return
    if isinstance(parent, list):
        if key == "-":
            parent.append(value)
            return
        parent.insert(int(key), value)
        return
    raise TypeError("add target parent is not a container")


def _merge_value(document: Any, path: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("merge value must be an object")
    parent, key = _resolve_parent(document, path)
    if isinstance(parent, dict):
        target = parent.get(key)
    elif isinstance(parent, list):
        target = parent[int(key)]
    else:
        raise TypeError("merge target parent is not a container")
    if not isinstance(target, dict):
        raise TypeError("merge target must be an object")
    target.update(value)


def apply_json_patch_edits(
    document: Any,
    patch: Dict[str, Any],
    *,
    allow_paths: List[str] | None = None,
    deny_paths: List[str] | None = None,
    target_id: str = "json_artifact",
) -> Tuple[Any, Dict[str, Any]]:

    original = deepcopy(document)
    updated = deepcopy(document)
    edits = patch.get("edits", []) if isinstance(patch, dict) else []
    if not isinstance(edits, list):
        edits = []
    allow = allow_paths or []
    deny = deny_paths or []
    report: Dict[str, Any] = {
        "target_id": target_id,
        "applied": [],
        "failed": [],
        "success": False,
        "partial_success": False,
        "input_chars": len(_json_text(document)),
        "output_chars": len(_json_text(document)),
    }
    if not edits:
        report["failed"].append({
            "edit_index": None,
            "reason": "patch.edits must be a non-empty list",
            "fatal": True,
        })
        return document, report

    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            report["failed"].append({"edit_index": index, "reason": "edit must be an object"})
            continue
        op = str(edit.get("op") or "set").strip()
        path = str(edit.get("path") or "").strip()
        if not path:
            report["failed"].append({"edit_index": index, "reason": "path is empty"})
            continue
        if _path_matches(path, deny):
            report["failed"].append({"edit_index": index, "path": path, "reason": "path is denied"})
            continue
        if allow and not _path_matches(path, allow):
            report["failed"].append({"edit_index": index, "path": path, "reason": "path is not allowed"})
            continue
        if op == "remove":
            report["failed"].append({
                "edit_index": index,
                "path": path,
                "reason": "remove edits are not allowed for spec repair",
            })
            continue
        try:
            if op == "set":
                _set_value(updated, path, edit.get("value"))
            elif op == "add":
                _add_value(updated, path, edit.get("value"))
            elif op == "merge":
                _merge_value(updated, path, edit.get("value"))
            else:
                raise ValueError(f"unsupported op: {op}")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            report["failed"].append({
                "edit_index": index,
                "path": path,
                "op": op,
                "reason": str(exc),
            })
            continue
        report["applied"].append({
            "edit_index": index,
            "op": op,
            "path": path,
            "reason": edit.get("reason", ""),
        })

    report["success"] = bool(report["applied"])
    report["partial_success"] = bool(report["applied"] and report["failed"])
    report["output_chars"] = len(_json_text(updated))
    if report["success"]:
        report["diff"] = _unified_diff(original, updated, target_id)
        report["first_changed_line"] = _first_changed_line(original, updated)
        return updated, report
    if report["failed"]:
        report["fatal"] = True
    return document, report
