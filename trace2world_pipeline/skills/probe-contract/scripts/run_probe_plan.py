#!/usr/bin/env python3
"""Run a probe plan against a running Trace2World environment host and judge business state.

A probe plan lists the rows of the mandatory probe matrix for one package revision. Each probe
opens fresh episodes through the public host API, performs its visible actions, snapshots the
business state before and after, evaluates, and closes the episode. Verdicts are computed from the
snapshot and the evaluator output only; screenshots are never consulted.

    python run_probe_plan.py --plan probe_plan.json --host http://127.0.0.1:6800 \
        --output-dir "$RESULT/run_management/probes/<round>"

Plan schema (``trace2world.probe_plan.v1``)::

    {
      "schema": "trace2world.probe_plan.v1",
      "package_revision": "<digest of the package under validation>",
      "state_source": "state" | "runtime",
      "business_paths": ["tables.orders", "state.cart"],
      "probes": [
        {
          "probe_id": "zero_action_t1",
          "kind": "zero_action",
          "task_id": "deal_01",
          "obligation_ids": ["o3"],
          "actions": [],
          "expect": {"success": false, "reward_max": 0.0}
        }
      ]
    }

``kind`` is one of the probe-matrix rows (``zero_action``, ``partial_stage``, ``wrong_entity``,
``premature_terminal``, ``repeated_action``, ``correct_terminal``, ``fresh_reset``,
``alternative_path``) or ``illegal_action`` for the guard-false / wrong-type / unbound attempts of
the verification contract. ``fresh_reset`` runs ``actions`` in a first episode and nothing in a
second, back-to-back one.

``expect`` keys, all optional: ``success`` (bool), ``reward_min``, ``reward_max``,
``business_state_unchanged`` (bool), ``same_final_state_as`` / ``same_reward_as`` (another probe
id), ``reached_checkpoints`` / ``unreached_checkpoints`` (checkpoint ids read from
``details.checkpoint_reward_vector``). ``fresh_reset`` additionally checks that the second episode
starts from the first episode's initial business state.

``business_paths`` are dotted paths into the state snapshot that make up the business projection.
Without them the whole snapshot is compared and the report says so, because route, overlay, focus,
or scroll changes may then count as business effects.

The runner writes ``probe_report.json`` (every episode, snapshot digest, and evaluator output) and
``raw_assertions.json`` (one case per probe with its verdict), the raw assertion report that the
repair report validator reads. Neither file is edited by hand.

Exit status: 0 when every probe passes, 1 when any probe fails, 2 when a probe could not run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "trace2world.probe_plan.v1"
REPORT_SCHEMA = "trace2world.probe_report.v1"
ASSERTIONS_SCHEMA = "trace2world.probe_assertions.v1"
KINDS = {
    "zero_action",
    "partial_stage",
    "wrong_entity",
    "premature_terminal",
    "repeated_action",
    "correct_terminal",
    "fresh_reset",
    "alternative_path",
    "illegal_action",
}
MISSING = object()


class HostError(RuntimeError):
    pass


class Host:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise HostError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HostError(f"{method} {path} -> {exc}") from exc
        try:
            value = json.loads(payload or b"{}")
        except json.JSONDecodeError as exc:
            raise HostError(f"{method} {path} -> non-JSON response") from exc
        if not isinstance(value, dict):
            raise HostError(f"{method} {path} -> response is not an object")
        return value


def lookup(value: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def business_projection(snapshot: dict[str, Any], paths: list[str]) -> Any:
    body = {k: v for k, v in snapshot.items() if k not in {"episode_id", "task_id"}}
    if not paths:
        return body
    projected = {}
    for path in paths:
        found = lookup(body, path)
        projected[path] = None if found is MISSING else found
    return projected


def digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(host: Host, episode_id: str, source: str, paths: list[str]) -> dict[str, Any]:
    path = f"/episodes/{episode_id}/state?kind=audit" if source == "state" else f"/episodes/{episode_id}/runtime"
    projected = business_projection(host.call("GET", path), paths)
    return {"projection": projected, "sha256": digest(projected)}


def run_episode(host: Host, task_id: str, actions: list[Any], source: str, paths: list[str]) -> dict[str, Any]:
    opened = host.call("POST", "/episodes", {"task_id": task_id})
    episode_id = str(opened.get("episode_id") or "")
    if not episode_id:
        raise HostError("POST /episodes returned no episode_id")
    record: dict[str, Any] = {"episode_id": episode_id, "steps": []}
    try:
        record["initial"] = snapshot(host, episode_id, source, paths)
        for index, step in enumerate(actions):
            action = step.get("action", step) if isinstance(step, dict) else step
            result = host.call("POST", f"/episodes/{episode_id}/actions", {"action": action})
            record["steps"].append(
                {
                    "index": index,
                    "action": action,
                    "accepted": result.get("accepted"),
                    "feedback": result.get("feedback"),
                    "page": result.get("page"),
                    "done": result.get("done"),
                }
            )
        record["final"] = snapshot(host, episode_id, source, paths)
        evaluation = host.call("POST", f"/episodes/{episode_id}/evaluate", {"final": True})
        record["evaluation"] = {
            "reward": evaluation.get("reward"),
            "success": evaluation.get("success"),
            "reason": evaluation.get("reason"),
            "details": evaluation.get("details"),
        }
    finally:
        try:
            host.call("DELETE", f"/episodes/{episode_id}")
        except HostError as exc:
            record["close_error"] = str(exc)
    return record


def reached_map(details: Any) -> dict[str, bool]:
    vector = details.get("checkpoint_reward_vector") if isinstance(details, dict) else None
    reached: dict[str, bool] = {}
    if isinstance(vector, dict):
        for key, entry in vector.items():
            reached[str(key)] = bool(entry.get("reached")) if isinstance(entry, dict) else bool(entry)
    elif isinstance(vector, list):
        for entry in vector:
            if isinstance(entry, dict):
                key = entry.get("checkpoint_id") or entry.get("id")
                if key is not None:
                    reached[str(key)] = bool(entry.get("reached"))
    return reached


def judge(probe: dict[str, Any], episodes: list[dict[str, Any]], done: dict[str, dict[str, Any]]) -> list[str]:
    expect = probe.get("expect") or {}
    judged = episodes[-1]
    evaluation = judged["evaluation"]
    reward = evaluation.get("reward")
    failures: list[str] = []

    if "success" in expect and bool(evaluation.get("success")) != bool(expect["success"]):
        failures.append(f"success={evaluation.get('success')!r}, expected {expect['success']!r}")
    for bound, ok in (("reward_min", lambda r, b: r >= b), ("reward_max", lambda r, b: r <= b)):
        if bound in expect:
            if not isinstance(reward, (int, float)):
                failures.append(f"reward={reward!r} is not numeric; cannot check {bound}")
            elif not ok(float(reward), float(expect[bound])):
                failures.append(f"reward={reward} violates {bound}={expect[bound]}")
    if expect.get("business_state_unchanged") and judged["initial"]["sha256"] != judged["final"]["sha256"]:
        failures.append("business state changed although the attempt must preserve it")

    reached = reached_map(evaluation.get("details"))
    for key in expect.get("reached_checkpoints") or []:
        if not reached.get(str(key)):
            failures.append(f"checkpoint {key} not reached")
    for key in expect.get("unreached_checkpoints") or []:
        if reached.get(str(key)):
            failures.append(f"checkpoint {key} reached although it must not be")

    for field, label in (("same_final_state_as", "final business state"), ("same_reward_as", "reward")):
        other_id = expect.get(field)
        if not other_id:
            continue
        other = done.get(str(other_id))
        if other is None or other.get("verdict") == "error":
            failures.append(f"{field}={other_id} has no completed run to compare with")
            continue
        other_last = other["episodes"][-1]
        if field == "same_final_state_as":
            mine, theirs = judged["final"]["sha256"], other_last["final"]["sha256"]
        else:
            mine, theirs = reward, other_last["evaluation"].get("reward")
        if mine != theirs:
            failures.append(f"{label} differs from probe {other_id}")

    if probe["kind"] == "fresh_reset":
        first, second = episodes[0], episodes[1]
        if first["initial"]["sha256"] != second["initial"]["sha256"]:
            failures.append("second episode does not start from the first episode's initial business state")
        if bool(second["evaluation"].get("success")):
            failures.append("second episode reports success without any action")
        if "reward_max" not in expect and isinstance(reward, (int, float)) and float(reward) > 0:
            failures.append(f"second episode reward={reward} without any action")
    return failures


def validate_plan(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    errors = []
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append(f"schema must be {PLAN_SCHEMA}")
    if not plan.get("package_revision"):
        errors.append("package_revision is required: probes must run on one named revision")
    if plan.get("state_source", "state") not in {"state", "runtime"}:
        errors.append("state_source must be 'state' or 'runtime'")
    probes = plan.get("probes")
    if not isinstance(probes, list) or not probes:
        return errors + ["probes must be a non-empty list"]
    seen: set[str] = set()
    for index, probe in enumerate(probes):
        where = f"probes[{index}]"
        if not isinstance(probe, dict):
            errors.append(f"{where} must be an object")
            continue
        probe_id = str(probe.get("probe_id") or "")
        if not probe_id or probe_id in seen:
            errors.append(f"{where}.probe_id must be present and unique")
        seen.add(probe_id)
        if probe.get("kind") not in KINDS:
            errors.append(f"{where}.kind must be one of {sorted(KINDS)}")
        if not probe.get("task_id"):
            errors.append(f"{where}.task_id is required")
        if not isinstance(probe.get("actions", []), list):
            errors.append(f"{where}.actions must be a list")
        if probe.get("kind") == "zero_action" and probe.get("actions"):
            errors.append(f"{where}: a zero_action probe performs no action")
    for index, probe in enumerate(probes):
        expect = probe.get("expect") if isinstance(probe, dict) else None
        for field in ("same_final_state_as", "same_reward_as"):
            ref = (expect or {}).get(field)
            if ref and ref not in seen:
                errors.append(f"probes[{index}].expect.{field} names unknown probe {ref}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--host", required=True, help="environment host base URL")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout in seconds")
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    errors = validate_plan(plan)
    if errors:
        print(json.dumps({"status": "invalid_plan", "errors": errors}, indent=2))
        return 2

    host = Host(args.host, args.timeout)
    source = plan.get("state_source", "state")
    paths = [str(p) for p in plan.get("business_paths") or []]
    ordered = sorted(
        plan["probes"],
        key=lambda p: bool((p.get("expect") or {}).get("same_final_state_as") or (p.get("expect") or {}).get("same_reward_as")),
    )
    done: dict[str, dict[str, Any]] = {}
    for probe in ordered:
        record: dict[str, Any] = {
            "probe_id": probe["probe_id"],
            "kind": probe["kind"],
            "task_id": probe["task_id"],
            "obligation_ids": probe.get("obligation_ids") or [],
            "expect": probe.get("expect") or {},
            "episodes": [],
        }
        try:
            record["episodes"].append(run_episode(host, probe["task_id"], probe.get("actions") or [], source, paths))
            if probe["kind"] == "fresh_reset":
                record["episodes"].append(run_episode(host, probe["task_id"], [], source, paths))
            failures = judge(probe, record["episodes"], done)
            record["verdict"] = "fail" if failures else "pass"
            record["failed_checks"] = failures
        except HostError as exc:
            record["verdict"] = "error"
            record["error"] = str(exc)
        done[probe["probe_id"]] = record

    records = [done[p["probe_id"]] for p in plan["probes"]]
    counts = {v: sum(r["verdict"] == v for r in records) for v in ("pass", "fail", "error")}
    report = {
        "schema": REPORT_SCHEMA,
        "package_revision": plan["package_revision"],
        "host": args.host,
        "state_source": source,
        "business_projection": paths or "full_snapshot",
        "counts": counts,
        "status": "error" if counts["error"] else ("fail" if counts["fail"] else "pass"),
        "probes": records,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "probe_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assertions = {
        "schema": ASSERTIONS_SCHEMA,
        "package_revision": plan["package_revision"],
        "trace_id": plan.get("trace_id"),
        "probe_report": out.name,
        "all_cases_pass": report["status"] == "pass",
        "cases": {
            r["probe_id"]: {"status": r["verdict"], "kind": r["kind"],
                            "failed_checks": r.get("failed_checks") or ([r["error"]] if r.get("error") else [])}
            for r in records
        },
    }
    (args.output_dir / "raw_assertions.json").write_text(
        json.dumps(assertions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(out), "status": report["status"], **counts}))
    return 2 if counts["error"] else (1 if counts["fail"] else 0)


if __name__ == "__main__":
    sys.exit(main())
