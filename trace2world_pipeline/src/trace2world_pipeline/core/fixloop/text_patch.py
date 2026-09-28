
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Tuple


def _line_col(text: str, index: int) -> Dict[str, int]:
    line = text.count("\n", 0, index) + 1
    line_start = text.rfind("\n", 0, index)
    column = index + 1 if line_start < 0 else index - line_start
    return {"line": line, "column": column}


def _first_changed_line(old: str, new: str) -> int | None:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    for idx, (old_line, new_line) in enumerate(zip(old_lines, new_lines), start=1):
        if old_line != new_line:
            return idx
    if len(old_lines) != len(new_lines):
        return min(len(old_lines), len(new_lines)) + 1
    return None


def _unified_diff(old: str, new: str, target_id: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{target_id}:before",
            tofile=f"{target_id}:after",
            lineterm="",
            n=4,
        )
    )


def _patch_edits(patch: Dict[str, Any]) -> List[Dict[str, Any]]:
    edits = patch.get("edits", []) if isinstance(patch, dict) else []
    return edits if isinstance(edits, list) else []


def apply_text_patch_edits(
    content: str,
    patch: Dict[str, Any],
    *,
    target_id: str = "artifact",
) -> Tuple[str, Dict[str, Any]]:

    report: Dict[str, Any] = {
        "target_id": target_id,
        "applied": [],
        "failed": [],
        "success": False,
        "partial_success": False,
        "input_chars": len(content or ""),
        "output_chars": len(content or ""),
    }
    edits = _patch_edits(patch)
    if not edits:
        report["failed"].append({
            "edit_index": None,
            "reason": "patch.edits must be a non-empty list",
            "fatal": True,
        })
        return content, report

    matches: List[Dict[str, Any]] = []
    for idx, edit in enumerate(edits):
        if not isinstance(edit, dict):
            report["failed"].append({"edit_index": idx, "reason": "edit must be an object"})
            continue
        old = edit.get("oldText", "")
        new = edit.get("newText", "")
        if not isinstance(old, str) or not old:
            report["failed"].append({"edit_index": idx, "reason": "oldText is empty"})
            continue
        if not isinstance(new, str):
            report["failed"].append({"edit_index": idx, "reason": "newText must be a string"})
            continue
        if old == new:
            report["failed"].append({
                "edit_index": idx,
                "reason": "replacement is identical",
                "oldText_preview": old[:240],
            })
            continue
        count = content.count(old)
        if count == 0:
            report["failed"].append({
                "edit_index": idx,
                "reason": "oldText not found",
                "oldText_preview": old[:240],
            })
            continue
        if count > 1:
            report["failed"].append({
                "edit_index": idx,
                "reason": "oldText is not unique",
                "occurrences": count,
                "oldText_preview": old[:240],
            })
            continue
        start = content.index(old)
        matches.append({
            "edit_index": idx,
            "start": start,
            "end": start + len(old),
            "oldText": old,
            "newText": new,
            "reason": edit.get("reason", ""),
        })

    matches.sort(key=lambda item: item["start"])
    applicable: List[Dict[str, Any]] = []
    last_end = -1
    last_index = None
    for item in matches:
        if item["start"] < last_end:
            report["failed"].append({
                "edit_index": item["edit_index"],
                "reason": "edit overlaps another edit",
                "overlaps_with": last_index,
            })
            continue
        applicable.append(item)
        last_end = item["end"]
        last_index = item["edit_index"]

    updated = content
    for item in reversed(applicable):
        updated = updated[:item["start"]] + item["newText"] + updated[item["end"]:]
        loc = _line_col(content, item["start"])
        report["applied"].append({
            "edit_index": item["edit_index"],
            "line": loc["line"],
            "column": loc["column"],
            "old_chars": len(item["oldText"]),
            "new_chars": len(item["newText"]),
            "reason": item["reason"],
        })

    report["applied"].sort(key=lambda item: item["edit_index"])
    report["success"] = bool(report["applied"])
    report["partial_success"] = bool(report["applied"] and report["failed"])
    report["output_chars"] = len(updated)
    if report["success"]:
        report["diff"] = _unified_diff(content, updated, target_id)
        report["first_changed_line"] = _first_changed_line(content, updated)
    elif report["failed"]:
        report["fatal"] = True
    return updated, report
