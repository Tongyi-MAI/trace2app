#!/usr/bin/env python3
"""Ledger, evidence index, and invalidation for the Algorithm 4 closure loop.

Wraps the released ``trace2world_pipeline.closure`` objects (``RepairLedger`` and
``invalidate``) for an operator-driven run, where the agent performs Execute, Review, Probe, and
Repair with the sibling skills and this script keeps the bookkeeping that must not drift:

    status          budget consumed / remaining and the recorded outcome of one slice
    record-repair   count one repair, only after its report validates and budget remains
    record-outcome  verified | upstream | dropped (with --exit cap | not_repairable); --write saves
                    the outcome in the shape of the released ClosureOutcome.as_dict()
    index           add an execution, review, or probe packet to the evidence index, with the
                    package files it consumed
    invalidate      drop indexed evidence that intersects a repair's Delta; list restarted traces

The ledger is the same file ``t2w-close`` reads (``trace2world.repair_ledger.v1``), so a slice keeps
its spent budget across runs and tools. Point every run of one result directory at one ledger.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve()
try:
    from trace2world_pipeline.closure import Evidence, Failure, RepairLedger, invalidate
except ImportError:  # running from a source checkout without installing the package
    sys.path.insert(0, str(HERE.parents[3] / "src"))
    from trace2world_pipeline.closure import Evidence, Failure, RepairLedger, invalidate

INDEX_SCHEMA = "trace2world.evidence_index.v1"
OUTCOMES = {"verified", "upstream", "dropped"}
REPAIRABLE = {"implementation", "supervision"}
# Package regions shared by every task: an edit here reaches every dependent trace.
DEFAULT_SHARED = (
    "tasks.json",
    "resource_manifest.json",
    "app/assets/",
    "app/page_index.json",
    "app/phase_sdk_generation/",
    "app/phase_pagegen/vendor/",
    "state_service/",
    "data/",
)
# What a probe consumes in addition to the pages its execution visited.
PROBE_EXTRA = ("evaluators/", "state_evaluator_plan.json", "tasks.json")


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return code


def load_validator():
    path = HERE.parents[2] / "repair-package" / "scripts" / "validate_repair_report.py"
    spec = importlib.util.spec_from_file_location("t2w_validate_repair_report", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_files(package: Path) -> list[str]:
    return sorted(p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file())


def expand(prefixes: tuple[str, ...] | list[str], files: list[str]) -> set[str]:
    out: set[str] = set()
    for prefix in prefixes:
        if prefix.endswith("/"):
            out.update(f for f in files if f.startswith(prefix))
        elif prefix in files:
            out.add(prefix)
    return out


def cmd_status(args: argparse.Namespace) -> int:
    ledger = RepairLedger(args.ledger)
    return emit({
        "slice_id": args.slice,
        "budget": args.budget,
        "repairs_consumed": ledger.consumed(args.slice),
        "repairs_remaining": ledger.remaining(args.slice, args.budget),
        "outcome": ledger.outcome(args.slice),
    })


def cmd_record_repair(args: argparse.Namespace) -> int:
    ledger = RepairLedger(args.ledger)
    if ledger.outcome(args.slice) is not None:
        return emit({"status": "refused", "reason": f"slice already closed as {ledger.outcome(args.slice)}"}, 1)
    if ledger.remaining(args.slice, args.budget) == 0:
        return emit({"status": "refused", "reason": "repair budget exhausted; the slice must drop with exit=cap"}, 1)
    validator = load_validator()
    report = json.loads(args.repair_report.read_text(encoding="utf-8"))
    errors = validator.validate(report, args.repair_report.resolve())
    if errors:
        return emit({"status": "refused", "reason": "repair report is invalid", "errors": errors}, 1)
    if report.get("status") != "repaired" or report.get("owner") not in REPAIRABLE:
        return emit({"status": "refused", "reason": "only a repaired implementation or supervision report consumes budget"}, 1)
    if report.get("slice_id") != args.slice:
        return emit({"status": "refused", "reason": "report slice_id differs from --slice"}, 1)
    count = ledger.record_repair(args.slice)
    return emit({"status": "recorded", "slice_id": args.slice, "repairs_consumed": count,
                 "repairs_remaining": ledger.remaining(args.slice, args.budget),
                 "delta": report.get("delta", [])})


def cmd_record_outcome(args: argparse.Namespace) -> int:
    ledger = RepairLedger(args.ledger)
    if args.outcome == "dropped" and args.exit is None:
        return emit({"status": "refused", "reason": "a dropped slice records its exit: cap or not_repairable"}, 1)
    if args.outcome != "dropped" and args.exit is not None:
        return emit({"status": "refused", "reason": "--exit applies only to dropped slices"}, 1)
    if args.exit == "cap" and ledger.remaining(args.slice, args.budget) != 0:
        return emit({"status": "refused", "reason": "exit=cap requires an exhausted budget"}, 1)
    failure = None
    if args.outcome != "verified":
        if not (args.failure_code and args.owner and args.stage and args.failure_packet):
            return emit({"status": "refused", "reason": "upstream and dropped outcomes name the last failure: "
                         "--failure-code, --owner, --stage, --failure-packet"}, 1)
        if args.outcome == "upstream" and args.owner != "semantics":
            return emit({"status": "refused", "reason": "only a semantics failure leaves upstream"}, 1)
        if args.exit == "not_repairable" and args.owner in REPAIRABLE and ledger.remaining(args.slice, args.budget):
            return emit({"status": "refused", "reason": "an implementation or supervision failure with budget left "
                         "is repairable; repair it or record why closure needs unsupported behavior as semantics"}, 1)
        failure = Failure(code=args.failure_code, owner=args.owner, stage=args.stage, slice_id=args.slice,
                          detail=str(args.failure_packet.resolve()))
    ledger.record_outcome(args.slice, args.outcome)
    payload: dict[str, Any] = {"status": args.outcome, "slice_id": args.slice, "waves": [],
                               "repairs_consumed": ledger.consumed(args.slice)}
    if args.waves:
        payload["waves"] = json.loads(args.waves.read_text(encoding="utf-8"))
    if args.outcome == "verified":
        payload["closed_at_wave"] = payload["waves"][-1].get("wave") if payload["waves"] else None
        if args.package_revision:
            payload["package_revision"] = args.package_revision
    else:
        payload["failure"] = failure.as_dict()
    if args.outcome == "dropped":
        payload["owner"] = failure.owner.value
        payload["exit"] = args.exit
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return emit({"status": "recorded", **payload})


def load_index(path: Path) -> dict[str, Any]:
    if path.is_file():
        index = json.loads(path.read_text(encoding="utf-8"))
        if index.get("schema") != INDEX_SCHEMA:
            raise SystemExit(f"{path} is not a {INDEX_SCHEMA} index")
        return index
    return {"schema": INDEX_SCHEMA, "records": []}


def visited_files(packet: dict[str, Any], files: list[str]) -> set[str]:
    hits: set[str] = set()
    for state in packet.get("observed_states") or []:
        url = state.get("url") if isinstance(state, dict) else None
        if not url:
            continue
        path = unquote(urlparse(str(url)).path)
        hits.update(f for f in files if path.endswith("/" + f) or path == f)
    return hits


def cmd_index(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    files = package_files(package)
    index = load_index(args.evidence_index)
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    packet_trace = str(packet.get("trace_id") or "")
    if packet_trace and args.trace_id and args.trace_id != packet_trace:
        return emit({"status": "refused", "reason": f"the packet names trace {packet_trace}; "
                     f"--trace-id {args.trace_id} disagrees (write one packet per trace)"}, 1)
    trace_id = packet_trace or str(args.trace_id or "")
    if not trace_id:
        return emit({"status": "refused", "reason": "the packet names no trace_id; pass --trace-id"}, 1)
    records = index["records"]
    if args.kind == "execution":
        pages = visited_files(packet, files)
        if not pages:
            return emit({"status": "refused", "reason": "no observed state URL maps to a package file"}, 1)
        consumed = pages | expand(DEFAULT_SHARED + tuple(args.shared_prefix or ()), files)
    else:
        executions = [r for r in records if r["kind"] == "execution" and r["trace_id"] == trace_id]
        if not executions:
            return emit({"status": "refused", "reason": "index the trace's execution packet first"}, 1)
        consumed = set(executions[-1]["consumed"])
        if args.kind == "probe":
            consumed |= expand(PROBE_EXTRA, files)
    evidence_id = args.evidence_id or f"{args.kind}:{trace_id}:{args.packet.resolve()}"
    records[:] = [r for r in records if r["evidence_id"] != evidence_id]
    records.append({"evidence_id": evidence_id, "kind": args.kind, "trace_id": trace_id,
                    "path": str(args.packet.resolve()), "consumed": sorted(consumed), "step": 0})
    index["package_root"] = str(package)
    args.evidence_index.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_index.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return emit({"status": "indexed", "evidence_id": evidence_id, "consumed_files": len(consumed)})


def cmd_invalidate(args: argparse.Namespace) -> int:
    index = load_index(args.evidence_index)
    delta_record = json.loads(args.delta.read_text(encoding="utf-8"))
    delta = [d for d in delta_record.get("delta") or [] if ":" not in d]  # package files only
    evidence = [Evidence(evidence_id=r["evidence_id"], kind=r["kind"], trace_id=r["trace_id"],
                         consumed=frozenset(r["consumed"]), step=int(r.get("step", 0)))
                for r in index["records"]]
    result = invalidate(evidence, delta)
    removed_ids = {e.evidence_id for e in result.removed}
    restarted = set(result.restarted_traces)
    kept = []
    for record in index["records"]:
        if record["evidence_id"] in removed_ids:
            continue
        if record["trace_id"] in restarted:
            record["step"] = 0
        kept.append(record)
    index["records"] = kept
    index["last_invalidation"] = {"delta": str(args.delta.resolve()), **result.as_dict()}
    args.evidence_index.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return emit({"status": "invalidated", "removed": sorted(removed_ids),
                 "restarted_traces": sorted(restarted), "retained": len(kept),
                 "formal_source_edits": [d for d in delta_record.get("delta") or [] if ":" in d]})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def ledger_args(p: argparse.ArgumentParser, budget: bool = True) -> None:
        p.add_argument("--ledger", required=True, type=Path)
        p.add_argument("--slice", required=True)
        if budget:
            p.add_argument("--budget", type=int, default=3)

    ledger_args(sub.add_parser("status"))
    rr = sub.add_parser("record-repair")
    ledger_args(rr)
    rr.add_argument("--repair-report", required=True, type=Path)
    ro = sub.add_parser("record-outcome")
    ledger_args(ro)
    ro.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    ro.add_argument("--exit", choices=["cap", "not_repairable"], default=None)
    ro.add_argument("--failure-code", default=None)
    ro.add_argument("--owner", choices=["semantics", "implementation", "supervision", "model", "provider"], default=None)
    ro.add_argument("--stage", choices=["review", "probe"], default=None)
    ro.add_argument("--failure-packet", type=Path, default=None)
    ro.add_argument("--waves", type=Path, default=None, help="JSON list of this slice's wave records")
    ro.add_argument("--package-revision", default=None, help="revision a verified slice was validated on")
    ro.add_argument("--write", type=Path, default=None, help="write the outcome record here")
    ix = sub.add_parser("index")
    ix.add_argument("--evidence-index", required=True, type=Path)
    ix.add_argument("--kind", required=True, choices=["execution", "review", "probe"])
    ix.add_argument("--packet", required=True, type=Path)
    ix.add_argument("--package", required=True, type=Path)
    ix.add_argument("--trace-id", default=None)
    ix.add_argument("--evidence-id", default=None)
    ix.add_argument("--shared-prefix", action="append", help="extra shared package region; repeatable")
    iv = sub.add_parser("invalidate")
    iv.add_argument("--evidence-index", required=True, type=Path)
    iv.add_argument("--delta", required=True, type=Path)

    args = parser.parse_args()
    handler = {"status": cmd_status, "record-repair": cmd_record_repair, "record-outcome": cmd_record_outcome,
               "index": cmd_index, "invalidate": cmd_invalidate}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
