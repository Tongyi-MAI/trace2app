#!/usr/bin/env python3
"""Diagnose one screening packet of Trace2World Algorithm 5, and guard refinements.

    python diagnose.py verdict --packet "$SCREEN/screen_packet.json"
    python diagnose.py check-refinement --parent parent.json --child child.json --mode harden

``verdict`` reads a ``trace2world.screen_packet.v1`` (the attempts of ``PassAtFour`` from the
learner and the solver, their review, and the findings), re-reads every trajectory by path and
SHA-256, and returns Diagnose's status:

    Incomplete   an attempt is unreviewed or unresolved, a finding lacks evidence, or a policy has
                 fewer than four valid attempts: review or PassAtFour continues
    Failed       the task needs behavior the frozen model does not support (owner semantics), or
                 PassAtFour ended early on a provider fault: Record
    EnvDefect    an implementation or supervision fault: requeue as EnvDefect
    TaskIssue    an actionable task issue (owner task): RefineOrHold
    Held         complete and fault-free, and the solver never succeeds: RefineOrHold holds it
    Refinable    complete and fault-free, solver succeeds, learner mean reward > 0.8: RefineOrHold
    Selectable   otherwise

Rewards and terminal success come from the trajectory files (``trace2world.trajectory.v1``:
``reward`` and ``evaluation.success``), never from the packet. Invalid attempts stay in the packet
and count for nothing. Exit 0 when a status is decided, 2 for ``Incomplete``, 1 for a malformed
packet.

``check-refinement`` checks that a refined candidate keeps every obligation of its parent (every
bound index item, every required phenomenon, the long-horizon class) and, in ``harden`` mode, adds
cross-state dependencies or evidence revisits. Exit 0 when it holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any

SCHEMA = "trace2world.screen_packet.v1"
TRAJECTORY_SCHEMA = "trace2world.trajectory.v1"
POLICIES = ("learner", "solver")
VALIDITY = {"valid", "invalid", "unresolved"}
INVALID_REASONS = {"timeout", "provider", "harness", "parser", "reset", "package", "evaluator", "environment"}
OWNERS = {"semantics", "implementation", "supervision", "task", "model", "provider"}
FAULT_OWNERS = {"implementation", "supervision"}
EVIDENCE_STATES = {
    "trace_confirmed",
    "static_contract_confirmed",
    "high_confidence_inference",
    "requires_live_reproduction",
    "insufficient_evidence",
}
CATEGORY_OWNERS = {
    "environment_start_reset_defect": {"implementation"},
    "environment_action_host_defect": {"implementation"},
    "environment_gesture_scroll_picker_defect": {"implementation"},
    "environment_input_ime_defect": {"implementation"},
    "environment_route_transition_defect": {"implementation"},
    "environment_state_handoff_projection_defect": {"implementation"},
    "environment_dynamic_query_binding_defect": {"implementation"},
    "environment_terminal_surface_defect": {"implementation"},
    "package_reset_or_host_defect": {"implementation"},
    "alternative_path_gap": {"implementation", "semantics"},
    "unsupported_task_requirement": {"semantics"},
    "checkpoint_missing_producer": {"supervision"},
    "checkpoint_false_negative": {"supervision"},
    "checkpoint_false_positive_risk": {"supervision"},
    "checkpoint_dependency_weighting_defect": {"supervision"},
    "checkpoint_sparse_or_weak_progress": {"supervision"},
    "checkpoint_open_outcome_undercoverage": {"supervision"},
    "task_query_ambiguity": {"task"},
    "task_terminal_mismatch": {"task"},
    "task_answer_leakage": {"task"},
    "model_failure": {"model"},
    "runner_or_parser_defect": {"provider"},
    "external_provider_failure": {"provider"},
}
INVALIDATING_OWNERS = {"implementation", "supervision", "provider"}
SATURATION = 0.8
BINDINGS = (
    "goal", "terminal_value", "launch_state", "subtasks", "decisions", "cross_state_dependencies",
    "evidence_revisits", "acceptable_terminal_set", "reset_boundary", "evaluator_outcome",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def read_attempts(packet: dict[str, Any], base: Path, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, attempt in enumerate(packet.get("attempts") or []):
        aid = str(attempt.get("attempt_id") or "")
        where = f"attempts[{position}]"
        if not aid or aid in seen:
            errors.append(f"{where}: attempt_id must be unique and non-empty")
            continue
        seen.add(aid)
        policy = attempt.get("policy")
        if policy not in POLICIES:
            errors.append(f"{aid}: policy must be learner or solver")
        if not isinstance(attempt.get("launch_index"), int):
            errors.append(f"{aid}: launch_index (launch order within its policy) is required")
        if attempt.get("package_revision") != packet.get("package_revision"):
            errors.append(f"{aid}: ran on another package revision; screen again on the current one")
        validity = attempt.get("validity")
        if validity not in VALIDITY:
            errors.append(f"{aid}: validity must be one of {sorted(VALIDITY)}")
        if validity == "invalid" and attempt.get("invalid_reason") not in INVALID_REASONS:
            errors.append(f"{aid}: an invalid attempt names invalid_reason from {sorted(INVALID_REASONS)}")
        row = {"attempt_id": aid, "policy": policy, "launch_index": attempt.get("launch_index"),
               "validity": validity, "reviewed": attempt.get("reviewed") is True,
               "finding_ids": [str(x) for x in attempt.get("finding_ids") or []], "reward": None, "success": None}
        path_value = attempt.get("trajectory_path")
        if path_value:
            path = Path(path_value)
            path = path if path.is_absolute() else base / path
            if not path.is_file():
                errors.append(f"{aid}: trajectory {path} does not exist")
            elif sha256(path) != str(attempt.get("trajectory_sha256") or "").removeprefix("sha256:"):
                errors.append(f"{aid}: trajectory_sha256 does not match the file; raw attempts are immutable")
            else:
                trajectory = load(path)
                if trajectory.get("schema") != TRAJECTORY_SCHEMA:
                    errors.append(f"{aid}: trajectory schema must be {TRAJECTORY_SCHEMA}")
                if trajectory.get("task_id") != packet.get("task_id"):
                    errors.append(f"{aid}: trajectory task_id differs from the packet")
                expected_model = ((packet.get("policies") or {}).get(policy) or {}).get("model")
                if expected_model and trajectory.get("model") != expected_model:
                    errors.append(f"{aid}: trajectory model differs from policies.{policy}.model")
                evaluation = trajectory.get("evaluation") or {}
                row["reward"] = float(trajectory.get("reward") or 0.0)
                row["success"] = evaluation.get("success") is True
        elif validity == "valid":
            errors.append(f"{aid}: a valid attempt binds its trajectory")
        elif not attempt.get("error_log_path"):
            errors.append(f"{aid}: an attempt without a trajectory binds its error_log_path")
        rows.append(row)
    return rows


def read_findings(packet: dict[str, Any], attempts: dict[str, dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
    findings = []
    seen: set[str] = set()
    for position, finding in enumerate(packet.get("findings") or []):
        fid = str(finding.get("finding_id") or "")
        if not fid or fid in seen:
            errors.append(f"findings[{position}]: finding_id must be unique and non-empty")
            continue
        seen.add(fid)
        owner, category, state = finding.get("owner"), finding.get("category"), finding.get("evidence_state")
        if category not in CATEGORY_OWNERS:
            errors.append(f"{fid}: unknown category {category!r}")
        elif owner not in CATEGORY_OWNERS[category]:
            errors.append(f"{fid}: category {category} belongs to {sorted(CATEGORY_OWNERS[category])}, not {owner!r}")
        if owner not in OWNERS:
            errors.append(f"{fid}: owner must be one of {sorted(OWNERS)}")
        if state not in EVIDENCE_STATES:
            errors.append(f"{fid}: evidence_state must be one of {sorted(EVIDENCE_STATES)}")
        if not finding.get("symptom") or not finding.get("evidence"):
            errors.append(f"{fid}: a finding states its symptom and evidence")
        members = [str(x) for x in finding.get("attempt_ids") or []]
        if not members and owner != "semantics":
            errors.append(f"{fid}: a finding names the attempts it covers")
        for aid in members:
            if aid not in attempts:
                errors.append(f"{fid}: names unknown attempt {aid}")
            elif fid not in attempts[aid]["finding_ids"]:
                errors.append(f"{fid}: attempt {aid} does not list this finding")
        if category == "checkpoint_false_negative" and state == "trace_confirmed" \
                and not (packet.get("checkpoint_audit") or {}).get("reward_vector_available"):
            errors.append(f"{fid}: a confirmed false negative needs a reward vector or checkpoint events")
        findings.append({"finding_id": fid, "owner": owner, "category": category, "evidence_state": state,
                         "attempt_ids": members})
    known = {f["finding_id"] for f in findings}
    for aid, row in attempts.items():
        for fid in row["finding_ids"]:
            if fid not in known:
                errors.append(f"{aid}: lists unknown finding {fid}")
    return findings


def verdict(packet_path: Path) -> tuple[dict[str, Any], int]:
    packet = load(packet_path)
    errors: list[str] = []
    if packet.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    for field in ("candidate_id", "task_id", "package_revision"):
        if not packet.get(field):
            errors.append(f"{field} is required")
    for policy in POLICIES:
        if not ((packet.get("policies") or {}).get(policy) or {}).get("model"):
            errors.append(f"policies.{policy}.model is required")
    rows = read_attempts(packet, packet_path.parent, errors)
    attempts = {row["attempt_id"]: row for row in rows}
    findings = read_findings(packet, attempts, errors)
    by_owner: dict[str, list[str]] = {}
    for finding in findings:
        by_owner.setdefault(str(finding["owner"]), []).append(finding["finding_id"])
    for row in rows:
        owners = {f["owner"] for f in findings if row["attempt_id"] in f["attempt_ids"]}
        if row["validity"] == "valid" and owners & INVALIDATING_OWNERS:
            errors.append(f"{row['attempt_id']}: valid, but carries a {sorted(owners & INVALIDATING_OWNERS)} finding")
    if errors:
        return {"status": "invalid_packet", "errors": errors}, 1

    result: dict[str, Any] = {"candidate_id": packet["candidate_id"], "task_id": packet["task_id"],
                              "package_revision": packet["package_revision"], "findings_by_owner": by_owner}
    pending = [r["attempt_id"] for r in rows if not r["reviewed"] or r["validity"] == "unresolved"]
    unproven = [f["finding_id"] for f in findings if f["evidence_state"] == "insufficient_evidence"]
    if pending or unproven:
        result.update(status="Incomplete", unreviewed_or_unresolved=pending, findings_without_evidence=unproven)
        return result, 2

    early = packet.get("early_failure")
    if by_owner.get("semantics"):
        result.update(status="Failed", owner="semantics", findings=by_owner["semantics"])
        return result, 0
    if by_owner.get("implementation") or by_owner.get("supervision"):
        result.update(status="EnvDefect",
                      findings=by_owner.get("implementation", []) + by_owner.get("supervision", []))
        return result, 0
    if early:
        owner = early.get("owner")
        if owner in FAULT_OWNERS:
            result.update(status="EnvDefect", early_failure=early)
        else:
            result.update(status="Failed", owner=owner or "provider", early_failure=early)
        return result, 0

    valid: dict[str, list[dict[str, Any]]] = {}
    for policy in POLICIES:
        ordered = sorted((r for r in rows if r["policy"] == policy and r["validity"] == "valid"),
                         key=lambda r: r["launch_index"])
        valid[policy] = ordered[:4]
        if len(ordered) > 4:
            result.setdefault("excess_valid_attempts", []).extend(r["attempt_id"] for r in ordered[4:])
    short = {p: 4 - len(v) for p, v in valid.items() if len(v) < 4}
    if short:
        result.update(status="Incomplete", valid_attempts_missing=short)
        return result, 2

    learner_rewards = [r["reward"] for r in valid["learner"]]
    solver_successes = sum(1 for r in valid["solver"] if r["success"])
    result.update(
        learner_attempts=[r["attempt_id"] for r in valid["learner"]],
        solver_attempts=[r["attempt_id"] for r in valid["solver"]],
        learner_rewards=learner_rewards,
        learner_mean_reward=round(statistics.fmean(learner_rewards), 4),
        learner_reward_sd=round(statistics.pstdev(learner_rewards), 4),
        learner_successes=sum(1 for r in valid["learner"] if r["success"]),
        solver_successes=solver_successes,
        invalid_attempts=[r["attempt_id"] for r in rows if r["validity"] == "invalid"],
    )
    if by_owner.get("task"):
        result.update(status="TaskIssue", findings=by_owner["task"])
    elif solver_successes == 0:
        result.update(status="Held")
    elif result["learner_mean_reward"] > SATURATION:
        result.update(status="Refinable")
    else:
        result.update(status="Selectable")
    return result, 0


def items(entry: Any) -> set[str]:
    values = entry if isinstance(entry, list) else [entry]
    out: set[str] = set()
    for value in values:
        if isinstance(value, str):
            out.add(value)
        elif isinstance(value, dict):
            out.update(str(x) for x in value.get("index_items") or [])
    return out


def required_phenomena(candidate: dict[str, Any]) -> set[str]:
    phenomena = (candidate.get("difficulty") or {}).get("phenomena") or {}
    return {k for k, v in phenomena.items() if isinstance(v, dict) and v.get("status") == "required"}


def check_refinement(parent_path: Path, child_path: Path, mode: str) -> tuple[dict[str, Any], int]:
    parent, child = load(parent_path), load(child_path)
    errors: list[str] = []
    if parent.get("candidate_id") == child.get("candidate_id"):
        errors.append("a refinement is a new candidate id")
    pb, cb = parent.get("bindings") or {}, child.get("bindings") or {}
    child_all = set().union(*(items(cb.get(n)) for n in BINDINGS)) if cb else set()
    for name in BINDINGS:
        lost = items(pb.get(name)) - (items(cb.get(name)) if isinstance(pb.get(name), list) else child_all)
        if lost:
            errors.append(f"{name}: drops prior obligations {sorted(lost)}")
    lost_phenomena = required_phenomena(parent) - required_phenomena(child)
    if lost_phenomena:
        errors.append(f"drops required phenomena {sorted(lost_phenomena)}")
    if (parent.get("difficulty") or {}).get("long_horizon") and not (child.get("difficulty") or {}).get("long_horizon"):
        errors.append("drops the long-horizon class")
    if mode == "harden":
        before = len(items(pb.get("cross_state_dependencies"))) + len(items(pb.get("evidence_revisits")))
        after = len(items(cb.get("cross_state_dependencies"))) + len(items(cb.get("evidence_revisits")))
        if after <= before:
            errors.append("hardening adds supported cross-state dependencies or evidence revisits")
    elif child.get("instruction") == parent.get("instruction") and cb == pb:
        errors.append("a task-issue refinement changes the instruction or a binding")
    return {"status": "fail" if errors else "pass", "mode": mode, "errors": errors}, 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verdict")
    v.add_argument("--packet", required=True, type=Path)
    v.add_argument("--output", type=Path, default=None)
    r = sub.add_parser("check-refinement")
    r.add_argument("--parent", required=True, type=Path)
    r.add_argument("--child", required=True, type=Path)
    r.add_argument("--mode", required=True, choices=["harden", "task-issue"])
    args = parser.parse_args()
    if args.command == "verdict":
        result, code = verdict(args.packet)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        result, code = check_refinement(args.parent, args.child, args.mode)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
