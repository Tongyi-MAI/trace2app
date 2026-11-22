#!/usr/bin/env python3
"""Check one task candidate's bindings against the task-facing index before it is implemented.

A candidate (``trace2world.task_candidate.v1``) binds every part of the task to items of the
current task-facing index. This script decides only what the bindings allow:

- ``rejected``: a binding is missing, names an item the index does not list, or names an item
  without support; or the candidate is malformed. A missing support binding rejects the candidate.
- ``needs_implementation``: every binding is supported, but some bound items are supported and not
  yet implemented; Implement instantiates exactly those (it never reconciles new semantics).
- ``admissible``: every bound item is supported and implemented.

    python validate_task_candidate.py --candidate cand.json --index "$RESULT/task_supply/index.json"

Index items are ``{"item_id", "kind", "supported_operation_ids", "evidence", "implementation_status"}``
with ``implementation_status`` ``implemented`` or ``supported_not_implemented``. Exit 0 for
``admissible`` or ``needs_implementation``, 1 for ``rejected``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "trace2world.task_candidate.v1"
INDEX_SCHEMA = "trace2world.task_index.v1"
BINDINGS = (
    "goal",
    "terminal_value",
    "launch_state",
    "subtasks",
    "decisions",
    "cross_state_dependencies",
    "evidence_revisits",
    "acceptable_terminal_set",
    "reset_boundary",
    "evaluator_outcome",
)
FLOOR = {"subtasks": 7, "decisions": 4, "cross_state_dependencies": 3, "evidence_revisits": 2}
PHENOMENA = {
    "cross_source_reasoning",
    "visual_spatial_precision",
    "implicit_state_inference",
    "multi_item_state_tracking",
    "conflict_disambiguation",
}
INFO_STATE_PHENOMENA = PHENOMENA - {"visual_spatial_precision"}
SIGNATURE = (
    "archetype_id",
    "goal_family",
    "decision_graph",
    "dependency_signature",
    "terminal_value_type",
    "reasoning_family",
    "authority_class",
)
NON_WORK_KINDS = {"navigation", "scroll", "overlay_open", "repeat_click"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refs(entry: Any) -> list[str]:
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        return [str(x) for x in entry.get("index_items") or []]
    return []


def check(candidate: dict[str, Any], index: dict[str, Any], index_digest: str) -> dict[str, Any]:
    errors: list[str] = []
    to_implement: set[str] = set()
    if candidate.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if index.get("schema") != INDEX_SCHEMA:
        errors.append(f"index schema must be {INDEX_SCHEMA}")
    if candidate.get("index_sha256") != index_digest:
        errors.append("index_sha256 does not match the current index; rebind the candidate")
    items = {str(i.get("item_id")): i for i in index.get("items") or [] if isinstance(i, dict)}

    instruction = str(candidate.get("instruction") or "")
    if not instruction.strip():
        errors.append("instruction is required")
    if candidate.get("candidate_id") and str(candidate["candidate_id"]) in instruction:
        errors.append("the instruction leaks the candidate id")
    if re.search(r"\b(action_id|data-action-id|step_index|\.html)\b", instruction):
        errors.append("the instruction leaks implementation identifiers")

    bindings = candidate.get("bindings")
    if not isinstance(bindings, dict):
        errors.append("bindings must be an object")
        bindings = {}
    for name in BINDINGS:
        value = bindings.get(name)
        entries = value if isinstance(value, list) else [value]
        if value in (None, [], {}, ""):
            errors.append(f"binding {name} is missing")
            continue
        for position, entry in enumerate(entries):
            ids = refs(entry)
            if not ids:
                errors.append(f"binding {name}[{position}] names no index item")
            if isinstance(entry, dict) and entry.get("kind") in NON_WORK_KINDS and name in FLOOR:
                errors.append(f"binding {name}[{position}] is {entry['kind']}; it does not count as work")
            for item_id in ids:
                item = items.get(item_id)
                if item is None:
                    errors.append(f"binding {name} names {item_id}, which the index does not list")
                elif not item.get("supported_operation_ids") or not item.get("evidence"):
                    errors.append(f"binding {name} names {item_id}, which has no supported operation and evidence")
                elif item.get("implementation_status") == "supported_not_implemented":
                    to_implement.add(item_id)
                elif item.get("implementation_status") != "implemented":
                    errors.append(f"index item {item_id} has unknown implementation_status")

    profile = candidate.get("difficulty") or {}
    if profile.get("long_horizon"):
        for name, minimum in FLOOR.items():
            count = sum(1 for e in bindings.get(name) or []
                        if not (isinstance(e, dict) and e.get("kind") in NON_WORK_KINDS))
            if count < minimum:
                errors.append(f"long-horizon floor: {name} has {count}, needs {minimum}")
        phenomena = profile.get("phenomena") or {}
        unknown = set(phenomena) - PHENOMENA
        if unknown:
            errors.append(f"unknown difficulty phenomena: {sorted(unknown)}")
        required = {k for k, v in phenomena.items() if isinstance(v, dict) and v.get("status") == "required"}
        for name in required:
            if not phenomena[name].get("evidence"):
                errors.append(f"phenomenon {name} is required but cites no evidence")
        if len(required) < 2 or not required & INFO_STATE_PHENOMENA:
            errors.append("long-horizon tasks need two required phenomena, one information- or state-bearing")
        if any(isinstance(v, dict) and v.get("status") == "unresolved" for v in phenomena.values()):
            errors.append("a phenomenon is unresolved; admission stays open")

    signature = candidate.get("diversity_signature") or {}
    for field in SIGNATURE:
        if not signature.get(field):
            errors.append(f"diversity_signature.{field} is required")
    if signature.get("authority_class") not in {None, "reference_exact", "schema_backed_generated"}:
        errors.append("authority_class must be reference_exact or schema_backed_generated")
    if signature.get("authority_class") == "schema_backed_generated":
        generated = candidate.get("generated_instance") or {}
        for field in ("parameter_domain", "producer", "reset_preflight", "holdout"):
            if not generated.get(field):
                errors.append(f"schema-backed instances need generated_instance.{field}")

    if errors:
        verdict = "rejected"
    elif to_implement:
        verdict = "needs_implementation"
    else:
        verdict = "admissible"
    return {"candidate_id": candidate.get("candidate_id"), "verdict": verdict, "errors": errors,
            "to_implement": sorted(to_implement)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    args = parser.parse_args()
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    index = json.loads(args.index.read_text(encoding="utf-8"))
    result = check(candidate, index, sha256(args.index))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if result["verdict"] == "rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
