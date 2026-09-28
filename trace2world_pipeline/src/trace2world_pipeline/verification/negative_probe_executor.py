
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _collect_probes(manifest_entry: dict) -> list[dict]:
    probes: list[dict] = []
    seen: set[str] = set()
    for cp in (manifest_entry.get("checkpoints") or {}).values():
        for p in cp.get("negative_probe_specs") or []:
            pid = p.get("probe_id", "")
            if pid and pid not in seen:
                seen.add(pid)
                probes.append(p)
    outcome = manifest_entry.get("outcome") or {}
    for p in outcome.get("negative_probe_specs") or []:
        pid = p.get("probe_id", "")
        if pid and pid not in seen:
            seen.add(pid)
            probes.append(p)
    return probes


def _event_name_for_checkpoint(cp_id: str, cp: dict) -> str:
    return cp.get("required_event") or f"ghostweb:checkpoint:{cp_id}"


def _apply_assertions_to_snapshot(snapshot: dict[str, Any], assertions: dict[str, dict]) -> None:
    for field, assertion in (assertions or {}).items():
        if assertion.get("op") == "eq":
            snapshot[field] = assertion["value"]


def _build_runtime_checkpoint_snapshots(manifest_entry: dict) -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    for cp_id, cp in (manifest_entry.get("checkpoints") or {}).items():
        visible = [a.get("value") for a in cp.get("projection_assertions", []) if a.get("value") is not None]
        if visible:
            snapshots[cp_id] = {"visible_text": visible}
    outcome = manifest_entry.get("outcome") or {}
    outcome_id = outcome.get("checkpoint_id")
    if outcome_id:
        visible = [a.get("value") for a in outcome.get("projection_assertions", []) if a.get("value") is not None]
        if visible:
            snapshots[outcome_id] = {"visible_text": visible}
    return snapshots


def _build_baseline(manifest_entry: dict) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    events: list[dict] = []
    snapshots: dict[str, dict] = {}

    for cp_id, cp in (manifest_entry.get("checkpoints") or {}).items():
        event_name = _event_name_for_checkpoint(cp_id, cp)
        event: dict[str, Any] = {
            "event": event_name,
            "type": event_name,
            "checkpoint_id": cp_id,
            "trace_id": manifest_entry.get("trace_id", ""),
            "page": "",
            "payload": {},
            "sequence": len(events) + 1,
        }
        snapshot: dict[str, Any] = {}
        for field, assertion in (cp.get("payload_assertions") or {}).items():
            if assertion.get("op") == "eq":
                event["payload"][field] = assertion["value"]
            elif assertion.get("op") == "present":
                event["payload"][field] = f"{field}::present"
        _apply_assertions_to_snapshot(snapshot, cp.get("state_assertions") or {})
        _apply_assertions_to_snapshot(snapshot, cp.get("diff_assertions") or {})
        snapshots[cp_id] = snapshot
        events.append(event)

    outcome = manifest_entry.get("outcome") or {}
    if outcome.get("checkpoint_id"):
        event_name = _event_name_for_checkpoint(outcome["checkpoint_id"], outcome)
        event = {
            "event": event_name,
            "type": event_name,
            "checkpoint_id": outcome["checkpoint_id"],
            "trace_id": manifest_entry.get("trace_id", ""),
            "page": "",
            "payload": {},
            "sequence": len(events) + 1,
        }
        snapshot: dict[str, Any] = {}
        _apply_assertions_to_snapshot(snapshot, outcome.get("state_assertions") or {})
        _apply_assertions_to_snapshot(snapshot, outcome.get("diff_assertions") or {})
        for field, value in snapshot.items():
            event["payload"][field] = value
        snapshots[outcome["checkpoint_id"]] = snapshot
        events.append(event)

    return events, snapshots, _build_runtime_checkpoint_snapshots(manifest_entry)


def _create_mutated_data(
    probe: dict,
    baseline_events: list[dict],
    baseline_snapshots: dict[str, dict],
    manifest_entry: dict,
) -> tuple[list[dict], dict[str, dict], dict[str, Any]]:
    events = copy.deepcopy(baseline_events)
    snapshots = copy.deepcopy(baseline_snapshots)
    mutate = probe.get("mutate") or {}
    mutated_fields: list[str] = []

    for field, value in mutate.items():
        if field == "__remove_event__":
            remove_checkpoint = str(value)
            before_count = len(events)
            events = [
                ev for ev in events
                if str(ev.get("checkpoint_id") or "") != remove_checkpoint
                and str(ev.get("event") or "") != f"ghostweb:checkpoint:{remove_checkpoint}"
            ]
            if len(events) != before_count:
                mutated_fields.append(f"__event__:{remove_checkpoint}")
            continue
        if field in {"__probe__", "__remove__"}:
            applied = _apply_generic_probe_mutation(
                snapshots,
                events,
                probe.get("must_fail") or [],
                remove=field == "__remove__",
            )
            if applied:
                mutated_fields.extend(applied)
            continue
        applied = False
        for snapshot in snapshots.values():
            if field in snapshot:
                snapshot[field] = value
                applied = True
        for ev in events:
            if field in ev.get("payload", {}):
                ev["payload"][field] = value
                applied = True
        if not applied:
            for cp_id in probe.get("must_fail") or []:
                snapshots.setdefault(cp_id, {})[field] = value
                applied = True
        if applied:
            mutated_fields.append(str(field))

    probe_id = probe.get("probe_id", "")

    if probe_id == "premature_submit":
        events.append({
            "event": "submit_order",
            "type": "submit_order",
            "checkpoint_id": "submit_order",
            "trace_id": manifest_entry.get("trace_id", ""),
            "page": "",
            "payload": {"type": "premature"},
            "sequence": 0,
        })
        mutated_fields.append("__event__:submit_order")

    if "forbidden_events" in probe:
        for evt_name in probe["forbidden_events"]:
            events.append({
                "event": evt_name,
                "type": evt_name,
                "checkpoint_id": evt_name,
                "trace_id": manifest_entry.get("trace_id", ""),
                "page": "",
                "payload": {"type": "forbidden"},
                "sequence": len(events) + 1,
            })

    if probe_id == "missing_address":
        for snapshot in snapshots.values():
            if "delivery_address" in snapshot:
                snapshot.pop("delivery_address", None)
                mutated_fields.append("delivery_address")
            if "address_name" in snapshot:
                snapshot.pop("address_name", None)
                mutated_fields.append("address_name")
        for ev in events:
            payload = ev.get("payload", {})
            if "delivery_address" in payload:
                payload.pop("delivery_address", None)
                mutated_fields.append("delivery_address")
            if "address_name" in payload:
                payload.pop("address_name", None)
                mutated_fields.append("address_name")

    missing_selected_filter = probe.get("missing_selected_filter")
    if missing_selected_filter:
        def remove_filter(value):
            if isinstance(value, list):
                return [item for item in value if str(item) != str(missing_selected_filter)]
            if str(value) == str(missing_selected_filter):
                return ""
            return value

        for snapshot in snapshots.values():
            if "selected_filters" in snapshot:
                snapshot["selected_filters"] = remove_filter(snapshot["selected_filters"])
            for field in list(snapshot.keys()):
                if str(field).endswith(".selected") and str(missing_selected_filter) in str(field):
                    snapshot[field] = False
                    mutated_fields.append(str(field))
        for ev in events:
            payload = ev.get("payload", {})
            if "selected_filters" in payload:
                payload["selected_filters"] = remove_filter(payload["selected_filters"])
                mutated_fields.append("selected_filters")

    metadata = {
        "executed": True,
        "mutation_applied": bool(mutated_fields) or bool(probe.get("forbidden_events")),
        "mutated_fields": sorted(set(mutated_fields)),
        "probe_unbound_to_assertion": bool(probe.get("probe_unbound_to_assertion")),
    }
    return events, snapshots, metadata


def _apply_generic_probe_mutation(
    snapshots: dict[str, dict],
    events: list[dict],
    must_fail: list[str],
    *,
    remove: bool,
) -> list[str]:
    mutated: list[str] = []
    for cp_id in must_fail:
        snapshot = snapshots.get(cp_id)
        if isinstance(snapshot, dict):
            for field in list(snapshot.keys()):
                if field.startswith("__"):
                    continue
                if remove:
                    snapshot.pop(field, None)
                    mutated.append(field)
                else:
                    snapshot[field] = f"__wrong__{snapshot[field]}"
                    mutated.append(field)
        for ev in events:
            if str(ev.get("checkpoint_id") or "") != str(cp_id):
                continue
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                continue
            for field in list(payload.keys()):
                if field.startswith("__"):
                    continue
                if remove:
                    payload.pop(field, None)
                    mutated.append(field)
                else:
                    payload[field] = f"__wrong__{payload[field]}"
                    mutated.append(field)
    return sorted(set(mutated))


def _check_probe_result(
    probe: dict,
    eval_result: dict,
    metadata: dict[str, Any] | None = None,
) -> dict:
    must_fail = probe.get("must_fail") or []
    checkpoint_status = eval_result.get("progress", {}).get("checkpoint_status", {})
    issues = eval_result.get("issues") or []

    actual_fail: list[str] = []
    for cp_id in must_fail:
        status = checkpoint_status.get(cp_id, {})
        if not status.get("passed", False):
            actual_fail.append(cp_id)
        elif status.get("passed") is None:
            issue_cps = [i.get("checkpoint_id", "") for i in issues if i.get("checkpoint_id") == cp_id]
            if issue_cps:
                actual_fail.append(cp_id)

    if not eval_result.get("success") and "outcome_form_ready_to_submit" in must_fail:
        if "outcome_form_ready_to_submit" not in actual_fail:
            actual_fail.append("outcome_form_ready_to_submit")

    failure_codes = list({
        i.get("code", "")
        for i in issues
        if i.get("checkpoint_id") in must_fail
    })
    for cp_id in must_fail:
        status = checkpoint_status.get(cp_id, {})
        for issue_code in status.get("issues") or []:
            if issue_code:
                failure_codes.append(str(issue_code))

    metadata = metadata or {}
    return {
        "probe_id": probe.get("probe_id", ""),
        "mutate": probe.get("mutate"),
        "forbidden_events": probe.get("forbidden_events", []),
        "executed": bool(metadata.get("executed", True)),
        "mutation_applied": bool(metadata.get("mutation_applied")),
        "mutated_fields": metadata.get("mutated_fields", []),
        "probe_unbound_to_assertion": bool(metadata.get("probe_unbound_to_assertion")),
        "expected_fail": must_fail,
        "actual_fail": sorted(set(actual_fail)),
        "passed": (
            set(must_fail).issubset(set(actual_fail))
            and not metadata.get("probe_unbound_to_assertion")
            and (bool(metadata.get("mutation_applied")) or bool(probe.get("forbidden_events")))
        ),
        "failure_codes": sorted(set(failure_codes)),
    }


def execute_negative_probes(
    result_dir: Path | str,
    trace_id: str | None = None,
) -> dict:
    from trace2world_pipeline.verification.evaluator_runner import run_evaluator

    root = Path(result_dir)
    manifest_path = root / "phase_post_processing" / "evaluators" / "executable_evaluator_manifest.json"
    manifest = _load_json(manifest_path)
    if not manifest:
        return {"error": "executable_evaluator_manifest.json not found"}

    evaluators = manifest.get("evaluators") or {}
    trace_ids = [trace_id] if trace_id else sorted(evaluators.keys())

    traces: dict[str, Any] = {}
    total_probes = 0
    passed_probes = 0

    for tid in trace_ids:
        entry = evaluators.get(tid)
        if not entry:
            continue

        probes = _collect_probes(entry)
        if not probes:
            continue

        baseline_events, baseline_snapshots, runtime_checkpoint_snapshots = _build_baseline(entry)
        probe_results: list[dict] = []

        for probe in probes:
            mutated_events, mutated_snapshots, mutation_metadata = _create_mutated_data(
                probe, baseline_events, baseline_snapshots, entry
            )
            eval_result = run_evaluator(
                entry,
                mutated_events,
                mutated_snapshots,
                runtime_checkpoint_snapshots,
            )
            pr = _check_probe_result(probe, eval_result, mutation_metadata)
            probe_results.append(pr)
            total_probes += 1
            if pr["passed"]:
                passed_probes += 1

        traces[tid] = {
            "probes": probe_results,
            "all_probes_passed": all(p["passed"] for p in probe_results),
            "total_probes": len(probe_results),
            "passed_probes": sum(1 for p in probe_results if p["passed"]),
        }

    report = {
        "format": "ghostweb_negative_probe_report",
        "schema_revision": 1,
        "traces": traces,
        "summary": {
            "total_traces_with_probes": len(traces),
            "total_probes": total_probes,
            "passed_probes": passed_probes,
            "failed_probes": total_probes - passed_probes,
        },
    }

    output_path = root / "phase_post_processing" / "evaluators" / "evaluator_negative_probe_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Execute negative probes")
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--trace-id", default=None)
    args = parser.parse_args()

    report = execute_negative_probes(args.result_dir, args.trace_id)
    s = report.get("summary", {})
    print(f"Traces with probes: {s.get('total_traces_with_probes', 0)}")
    print(f"Total probes: {s.get('total_probes', 0)}")
    print(f"Passed: {s.get('passed_probes', 0)}")
    print(f"Failed: {s.get('failed_probes', 0)}")
