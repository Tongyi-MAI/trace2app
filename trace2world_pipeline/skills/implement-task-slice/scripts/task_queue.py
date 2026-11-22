#!/usr/bin/env python3
"""Queue, selection, and freeze state of Trace2World Algorithm 5 (learner-guided task selection).

One JSON file holds the work queue Q, the selected batch A, the recorded failures, and the holds:

    init              Q <- {(c, New)} for the admissible candidates, in stable order
    pop               take the next queued entry and mark it active
    push              requeue an entry: EnvDefect after Diagnose, or New for a refinement
                      (--refine-of; a lineage is refined at most twice)
    select            add a Selectable candidate to A with the package files its evidence consumed
    record            log a failed candidate and its owner (Record) or a hold (RefineOrHold)
    requeue-affected  RequeueAffected(A, Q, Delta): selections whose evidence intersects Delta go
                      back to Q as New, queued evidence intersecting Delta is dropped
    freeze            Freeze(E, A): bind the selected batch and its evidence to one package
                      revision; refused while Q holds work
    show              print the state

Repair counts are not kept here: they live in the closure ledger (`repair_ledger.json`), keyed by
the candidate's slice id, and survive requeues and refinements.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "trace2world.task_supply_queue.v1"
BATCH_SCHEMA = "trace2world.task_batch.v1"
MAX_ROUND = 3  # the initial candidate and at most two refinements
QUEUE_STATES = {"New", "EnvDefect"}
RECORD_KINDS = {"failed", "held", "rejected"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return code


def load(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != SCHEMA:
        raise SystemExit(f"{path} is not a {SCHEMA} file")
    return state


def save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def known(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["candidate_id"]: c for c in state["candidates"]}


def location(state: dict[str, Any], cid: str) -> str:
    if any(e["candidate_id"] == cid for e in state["queue"]):
        return "queued"
    if state.get("active") and state["active"]["candidate_id"] == cid:
        return "active"
    if any(s["candidate_id"] == cid for s in state["selected"]):
        return "selected"
    if any(r["candidate_id"] == cid for r in state["records"]):
        return "recorded"
    return "none"


def cmd_init(args: argparse.Namespace) -> int:
    if args.queue.exists():
        return emit({"status": "refused", "reason": f"{args.queue} exists; a queue is initialized once"}, 1)
    rows = json.loads(args.candidates.read_text(encoding="utf-8"))
    rows = rows.get("candidates", rows) if isinstance(rows, dict) else rows
    candidates, seen = [], set()
    for row in rows:
        cid = str(row["candidate_id"] if isinstance(row, dict) else row)
        if cid in seen:
            return emit({"status": "refused", "reason": f"duplicate candidate {cid}"}, 1)
        seen.add(cid)
        packet = row.get("packet") if isinstance(row, dict) else None
        candidates.append({"candidate_id": cid, "lineage_id": cid, "round": 1, "parent": None, "packet": packet})
    state = {
        "schema": SCHEMA,
        "created_at": now(),
        "candidates": candidates,
        "queue": [{"candidate_id": c["candidate_id"], "state": "New", "evidence": []} for c in candidates],
        "active": None,
        "selected": [],
        "records": [],
        "frozen": None,
    }
    save(args.queue, state)
    return emit({"status": "initialized", "queued": len(candidates)})


def cmd_pop(args: argparse.Namespace) -> int:
    state = load(args.queue)
    if state["frozen"]:
        return emit({"status": "refused", "reason": "the batch is frozen"}, 1)
    if state["active"]:
        return emit({"status": "refused", "reason": f"{state['active']['candidate_id']} is still active; "
                     "select, push, or record it first"}, 1)
    if not state["queue"]:
        return emit({"status": "empty"})
    entry = state["queue"].pop(0)
    state["active"] = entry
    save(args.queue, state)
    return emit({"status": "active", **entry, **{k: known(state)[entry["candidate_id"]][k]
                                               for k in ("lineage_id", "round", "packet")}})


def close_active(state: dict[str, Any], cid: str) -> str | None:
    active = state.get("active")
    if not active or active["candidate_id"] != cid:
        return f"{cid} is not the active candidate"
    state["active"] = None
    return None


def cmd_push(args: argparse.Namespace) -> int:
    state = load(args.queue)
    cands = known(state)
    if args.refine_of:
        parent = cands.get(args.refine_of)
        if parent is None:
            return emit({"status": "refused", "reason": f"unknown candidate {args.refine_of}"}, 1)
        if args.state != "New":
            return emit({"status": "refused", "reason": "a refinement enters the queue as New"}, 1)
        if parent["round"] >= MAX_ROUND:
            return emit({"status": "refused", "reason": "lineage already refined twice; record a hold instead"}, 1)
        if args.candidate in cands:
            return emit({"status": "refused", "reason": f"{args.candidate} already exists; a refinement is a new candidate"}, 1)
        error = close_active(state, args.refine_of)
        if error:
            return emit({"status": "refused", "reason": error}, 1)
        state["records"].append({"candidate_id": args.refine_of, "kind": "refined", "into": args.candidate,
                                 "reason": args.reason, "at": now()})
        state["candidates"].append({"candidate_id": args.candidate, "lineage_id": parent["lineage_id"],
                                    "round": parent["round"] + 1, "parent": args.refine_of, "packet": args.packet})
    else:
        if args.candidate not in cands:
            return emit({"status": "refused", "reason": f"unknown candidate {args.candidate}"}, 1)
        error = close_active(state, args.candidate)
        if error:
            return emit({"status": "refused", "reason": error}, 1)
    state["queue"].append({"candidate_id": args.candidate, "state": args.state, "evidence": args.evidence or []})
    save(args.queue, state)
    return emit({"status": "queued", "candidate_id": args.candidate, "state": args.state,
                 "round": known(state)[args.candidate]["round"]})


def consumed_from_index(index_path: Path, key: str) -> list[str]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    files: set[str] = set()
    for record in index.get("records", []):
        if record.get("trace_id") == key:
            files.update(record.get("consumed") or [])
    return sorted(files)


def cmd_select(args: argparse.Namespace) -> int:
    state = load(args.queue)
    error = close_active(state, args.candidate)
    if error:
        return emit({"status": "refused", "reason": error}, 1)
    consumed = consumed_from_index(args.evidence_index, args.evidence_key or args.candidate)
    if not consumed:
        return emit({"status": "refused", "reason": "no indexed evidence for this candidate; index its execution first"}, 1)
    state["selected"].append({"candidate_id": args.candidate, "screen_packet": str(args.screen_packet.resolve()),
                              "package_revision": args.package_revision, "consumed": consumed, "at": now()})
    save(args.queue, state)
    return emit({"status": "selected", "candidate_id": args.candidate, "consumed_files": len(consumed)})


def cmd_record(args: argparse.Namespace) -> int:
    state = load(args.queue)
    if location(state, args.candidate) == "active":
        close_active(state, args.candidate)
    elif location(state, args.candidate) == "queued":
        state["queue"] = [e for e in state["queue"] if e["candidate_id"] != args.candidate]
    else:
        return emit({"status": "refused", "reason": f"{args.candidate} is not active or queued"}, 1)
    if args.kind == "failed" and not args.owner:
        return emit({"status": "refused", "reason": "a failed candidate records its owner"}, 1)
    state["records"].append({"candidate_id": args.candidate, "kind": args.kind, "status": args.status,
                             "owner": args.owner, "reason": args.reason, "packet": args.packet, "at": now()})
    save(args.queue, state)
    return emit({"status": "recorded", "candidate_id": args.candidate, "kind": args.kind})


def cmd_requeue_affected(args: argparse.Namespace) -> int:
    state = load(args.queue)
    delta = {d for d in json.loads(args.delta.read_text(encoding="utf-8")).get("delta") or [] if ":" not in d}
    moved, kept = [], []
    for item in state["selected"]:
        if delta & set(item["consumed"]):
            moved.append(item["candidate_id"])
            state["queue"].append({"candidate_id": item["candidate_id"], "state": "New", "evidence": []})
        else:
            kept.append(item)
    state["selected"] = kept
    dropped = 0
    for entry in state["queue"]:
        before = len(entry.get("evidence") or [])
        entry["evidence"] = [e for e in entry.get("evidence") or [] if not delta & set(e.get("consumed") or [])]
        dropped += before - len(entry["evidence"])
    save(args.queue, state)
    return emit({"status": "requeued", "moved_back_to_queue": moved, "queued_evidence_dropped": dropped})


def cmd_freeze(args: argparse.Namespace) -> int:
    state = load(args.queue)
    if state["queue"] or state["active"]:
        return emit({"status": "refused", "reason": "Q still holds work; freeze runs after the loop ends"}, 1)
    stale = [s["candidate_id"] for s in state["selected"] if s["package_revision"] != args.package_revision
             and not args.allow_unchanged_evidence]
    if stale:
        return emit({"status": "refused", "reason": "selected evidence was produced on another revision; requeue "
                     "it, or pass --allow-unchanged-evidence after requeue-affected kept it", "candidates": stale}, 1)
    cands = known(state)
    batch = {
        "schema": BATCH_SCHEMA,
        "frozen_at": now(),
        "package_revision": args.package_revision,
        "tasks": [{**s, "lineage_id": cands[s["candidate_id"]]["lineage_id"], "round": cands[s["candidate_id"]]["round"]}
                  for s in state["selected"]],
        "records": state["records"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(batch, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    state["frozen"] = {"package_revision": args.package_revision, "batch": str(args.output.resolve()), "at": now()}
    save(args.queue, state)
    return emit({"status": "frozen", "selected": len(batch["tasks"]), "records": len(batch["records"])})


def cmd_show(args: argparse.Namespace) -> int:
    state = load(args.queue)
    kinds: dict[str, int] = {}
    for r in state["records"]:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return emit({"queued": [(e["candidate_id"], e["state"]) for e in state["queue"]],
                 "active": state["active"]["candidate_id"] if state["active"] else None,
                 "selected": [s["candidate_id"] for s in state["selected"]],
                 "records": kinds, "frozen": state["frozen"]})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def q(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--queue", required=True, type=Path)
        return p

    q(sub.add_parser("init")).add_argument("--candidates", required=True, type=Path)
    q(sub.add_parser("pop"))
    q(sub.add_parser("show"))
    p = q(sub.add_parser("push"))
    p.add_argument("--candidate", required=True)
    p.add_argument("--state", required=True, choices=sorted(QUEUE_STATES))
    p.add_argument("--refine-of", default=None)
    p.add_argument("--packet", default=None)
    p.add_argument("--reason", default="")
    p.add_argument("--evidence", type=json.loads, default=None, help="JSON list of {path, consumed}")
    s = q(sub.add_parser("select"))
    s.add_argument("--candidate", required=True)
    s.add_argument("--screen-packet", required=True, type=Path)
    s.add_argument("--package-revision", required=True)
    s.add_argument("--evidence-index", required=True, type=Path)
    s.add_argument("--evidence-key", default=None, help="trace or task id in the evidence index")
    r = q(sub.add_parser("record"))
    r.add_argument("--candidate", required=True)
    r.add_argument("--kind", required=True, choices=sorted(RECORD_KINDS))
    r.add_argument("--status", default=None)
    r.add_argument("--owner", default=None, choices=["semantics", "implementation", "supervision", "model", "provider", "task"])
    r.add_argument("--reason", required=True)
    r.add_argument("--packet", default=None)
    ra = q(sub.add_parser("requeue-affected"))
    ra.add_argument("--delta", required=True, type=Path)
    f = q(sub.add_parser("freeze"))
    f.add_argument("--package-revision", required=True)
    f.add_argument("--output", required=True, type=Path)
    f.add_argument("--allow-unchanged-evidence", action="store_true",
                   help="accept selections made on an earlier revision that requeue-affected retained")
    args = parser.parse_args()
    return {"init": cmd_init, "pop": cmd_pop, "show": cmd_show, "push": cmd_push, "select": cmd_select,
            "record": cmd_record, "requeue-affected": cmd_requeue_affected, "freeze": cmd_freeze}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
