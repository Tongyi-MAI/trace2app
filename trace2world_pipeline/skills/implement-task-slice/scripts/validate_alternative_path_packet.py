#!/usr/bin/env python3
"""Validate one alternative-path packet for a task (``trace2world.alternative_path_packet.v1``).

An alternative path is a non-source route a reasonable user could take, from visible state, to the
same business result. Each candidate carries three independent verdicts (semantic validity,
materialization, evaluator coverage) and the seven equivalence gates. A discovered path may be
implemented only when its edges are supported operations of the frozen model and its task
semantics are equivalent; a semantically valid path whose edges are unsupported stays a finding.

    python validate_alternative_path_packet.py --packet alt.json [--index "$RESULT/task_supply/index.json"]
    python validate_alternative_path_packet.py --packet alt.json --require-materialization-closure

With ``--index``, every ``supported_operation_ids`` entry of an implementable candidate must be a
supported operation of an index item. Exit 0 when valid, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "trace2world.alternative_path_packet.v1"
GATES = {
    "A_visible_support",
    "B_entity_identity",
    "C_payload_identity",
    "D_terminal_identity",
    "E_commit_boundary_safety",
    "F_task_relevance",
    "G_state_continuity",
}
SEMANTIC = {"valid", "invalid", "uncertain"}
MATERIALIZATION = {"materialized", "partial", "missing", "not_probed"}
EVALUATOR = {"covered", "reference_overfit", "missing", "not_inspected"}
REPLACEMENT_FIELDS = ("replaced_reference_steps", "replaced_reference_operations", "shortcut_surface", "rejoin_state")


def validate(packet: dict[str, Any], supported_ops: set[str] | None, require_closure: bool) -> list[str]:
    errors: list[str] = []
    if packet.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    for field in ("task_id", "source_trace", "terminal_semantics", "reference_path"):
        if not packet.get(field):
            errors.append(f"{field} is required")

    ledger = packet.get("candidate_ledger") or []
    by_id: dict[str, dict[str, Any]] = {}
    for candidate in ledger:
        cid = str(candidate.get("candidate_id") or "")
        if not cid or cid in by_id:
            errors.append("every ledger candidate needs a unique candidate_id")
            continue
        by_id[cid] = candidate
        gates = candidate.get("equivalence_gates") or {}
        if set(gates) != GATES:
            errors.append(f"{cid}: equivalence_gates must list exactly {sorted(GATES)}")
        if candidate.get("semantic_verdict") not in SEMANTIC:
            errors.append(f"{cid}: semantic_verdict must be one of {sorted(SEMANTIC)}")
        if candidate.get("materialization_status") not in MATERIALIZATION:
            errors.append(f"{cid}: materialization_status must be one of {sorted(MATERIALIZATION)}")
        if candidate.get("evaluator_status") not in EVALUATOR:
            errors.append(f"{cid}: evaluator_status must be one of {sorted(EVALUATOR)}")
        if candidate.get("semantic_verdict") == "valid" and not all(v == "pass" for v in gates.values()):
            errors.append(f"{cid}: a valid candidate passes every gate")
        if candidate.get("semantic_verdict") != "valid" and not candidate.get("reason"):
            errors.append(f"{cid}: an invalid or uncertain candidate names the failed gate or missing evidence")

    accepted = [c for c in ledger if c.get("semantic_verdict") == "valid"]
    primary = {str(x if isinstance(x, str) else x.get("candidate_id")) for x in packet.get("primary_semantic_findings") or []}
    for candidate in accepted:
        cid = candidate.get("candidate_id")
        replacement = candidate.get("reference_replacement") or {}
        for field in REPLACEMENT_FIELDS:
            if not replacement.get(field):
                errors.append(f"{cid}: reference_replacement.{field} is required")
        if candidate.get("primary_semantic_finding") and cid not in primary:
            errors.append(f"{cid}: primary candidate missing from primary_semantic_findings")
        if candidate.get("implement"):
            ops = candidate.get("supported_operation_ids") or []
            if candidate.get("edges_supported") is not True or not ops:
                errors.append(f"{cid}: only a path whose edges are supported operations may be implemented")
            elif supported_ops is not None:
                unknown = sorted(set(map(str, ops)) - supported_ops)
                if unknown:
                    errors.append(f"{cid}: operations not supported by the index: {unknown}")
    for candidate in ledger:
        if candidate.get("implement") and candidate.get("semantic_verdict") != "valid":
            errors.append(f"{candidate.get('candidate_id')}: only a semantically valid path may be implemented")

    if require_closure:
        for candidate in accepted:
            if not candidate.get("implement"):
                continue
            cid = candidate.get("candidate_id")
            if candidate.get("materialization_status") != "materialized":
                errors.append(f"{cid}: implemented path is not materialized")
            if candidate.get("evaluator_status") != "covered":
                errors.append(f"{cid}: implemented path is not covered by the evaluator")
            closure = candidate.get("closure") or {}
            if not closure.get("execution_packet") or not closure.get("probe_report"):
                errors.append(f"{cid}: implemented path needs its own execution packet and alternative-path probe")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--require-materialization-closure", action="store_true")
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    supported = None
    if args.index:
        index = json.loads(args.index.read_text(encoding="utf-8"))
        supported = {str(op) for item in index.get("items") or [] for op in item.get("supported_operation_ids") or []}
    errors = validate(packet, supported, args.require_materialization_closure)
    accepted = sum(1 for c in packet.get("candidate_ledger") or [] if c.get("semantic_verdict") == "valid")
    print(json.dumps({"status": "fail" if errors else "pass", "accepted": accepted, "errors": errors},
                     indent=2, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
