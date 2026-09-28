
from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..algorithm import DEFAULT_REPAIR_BUDGET, run_closure
from ..ledger import RepairLedger


def _load_operators(spec: str) -> object:
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise SystemExit("--operators must be given as 'module:factory'")
    factory = getattr(importlib.import_module(module_name), attr)
    return factory()


def _slice_ids(result_dir: Path, explicit: list[str]) -> list[str]:
    if explicit:
        return list(explicit)
    tasks = result_dir / "tasks.json"
    if not tasks.is_file():
        raise SystemExit(f"no --slice given and {tasks} is absent")
    payload = json.loads(tasks.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("tasks", [])
    return [str(row["task_id"]) for row in rows if isinstance(row, dict) and row.get("task_id")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Algorithm 2 closure over a result directory")
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True, help="persistent per-slice repair ledger")
    parser.add_argument("--repair-budget", type=int, default=DEFAULT_REPAIR_BUDGET)
    parser.add_argument("--slice", action="append", default=[], dest="slices")
    parser.add_argument("--operators", default="", help="'module:factory' supplying the closure operators")
    parser.add_argument("--report", action="store_true", help="summarise the ledger without executing")
    parser.add_argument("--write-report", type=Path, default=None)
    args = parser.parse_args()

    ledger = RepairLedger(args.ledger)
    slices = _slice_ids(args.result_dir, args.slices)

    if args.report:
        outcomes = [
            {"slice_id": s, "repairs_consumed": ledger.consumed(s), "outcome": ledger.outcome(s)}
            for s in slices
        ]
    else:
        if not args.operators:
            raise SystemExit("--operators is required unless --report is given")
        operators = _load_operators(args.operators)
        outcomes = []
        for slice_id in slices:
            result = run_closure(
                environment=args.result_dir,
                page_specifications=args.result_dir / "phase_pagegen",
                source_traces=args.result_dir / "admitted_reference_traces",
                slice_id=slice_id,
                operators=operators,
                ledger=ledger,
                repair_budget=args.repair_budget,
            )
            outcomes.append(result.as_dict())

    counts: dict[str, int] = {}
    for row in outcomes:
        key = str(row.get("status") or row.get("outcome") or "unknown")
        counts[key] = counts.get(key, 0) + 1

    report = {
        "schema": "trace2world.closure_report.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result_dir": str(args.result_dir),
        "repair_budget": args.repair_budget,
        "slice_count": len(slices),
        "outcome_counts": counts,
        "outcomes": outcomes,
    }
    if args.write_report:
        args.write_report.parent.mkdir(parents=True, exist_ok=True)
        args.write_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
