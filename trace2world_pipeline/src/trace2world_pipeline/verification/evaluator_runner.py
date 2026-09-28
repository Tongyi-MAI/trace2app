
from __future__ import annotations

import re


def _normalize_value(value):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)

        if stripped.startswith("-") and stripped[1:].isdigit():
            return int(stripped)
    return value


def _check_assertion(op: str, actual, expected) -> bool:
    if op == "present":
        return actual is not None

    if op == "eq":
        return _normalize_value(actual) == _normalize_value(expected)

    if op == "numeric_eq":
        try:
            return float(str(actual).strip()) == float(str(expected).strip())
        except (ValueError, TypeError):
            return False

    if op == "contains":
        if actual is None:
            return False
        return str(expected) in str(actual)

    if op == "contains_visible":
        if actual is None:
            return False
        if isinstance(actual, list):
            return any(str(expected) in str(item) for item in actual)
        return str(expected) in str(actual)

    if op == "selected_contains":
        if actual is None:
            return False
        values = actual if isinstance(actual, list) else [actual]
        return any(str(expected) in str(item) for item in values)

    if op == "set_contains_all":
        values = actual if isinstance(actual, list) else ([] if actual is None else [actual])
        actual_set = {str(item) for item in values}
        expected_values = expected if isinstance(expected, list) else [expected]
        return all(str(item) in actual_set for item in expected_values)

    if op == "set_equals":
        values = actual if isinstance(actual, list) else ([] if actual is None else [actual])
        expected_values = expected if isinstance(expected, list) else [expected]
        return {str(item) for item in values} == {str(item) for item in expected_values}

    if op == "count_eq":
        try:
            count = len(actual or []) if isinstance(actual, (list, tuple, set, dict)) else int(actual)
            return count == int(expected)
        except (ValueError, TypeError):
            return False

    if op == "numeric_range_eq":
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        try:
            return (
                float(actual.get("min")) == float(expected.get("min"))
                and float(actual.get("max")) == float(expected.get("max"))
            )
        except (ValueError, TypeError):
            return False

    if op == "single_select_exactly_one":
        values = actual if isinstance(actual, list) else ([] if actual is None else [actual])
        return len(values) == 1 and str(values[0]) == str(expected)

    if op == "enabled":
        if isinstance(actual, dict):
            label = expected.get("label") if isinstance(expected, dict) else expected
            return bool(actual.get(str(label)))
        return bool(actual) is True


    return False


def _event_names(event: dict) -> list[str]:
    names: list[str] = []
    for key in ("event", "type", "t"):
        value = event.get(key)
        if value:
            names.append(str(value))
    checkpoint_id = event.get("checkpoint_id")
    if checkpoint_id:
        names.append(str(checkpoint_id))
        names.append(f"ghostweb:checkpoint:{checkpoint_id}")
    return list(dict.fromkeys(names))


def _snapshot_for_checkpoint(state_snapshots: dict[str, dict], checkpoint_id: str) -> dict:
    snapshot: dict = {}
    global_state = state_snapshots.get("__global__")
    if isinstance(global_state, dict):
        snapshot.update(global_state)
    checkpoint_state = state_snapshots.get(checkpoint_id)
    if isinstance(checkpoint_state, dict):
        snapshot.update(checkpoint_state)
    return snapshot


def run_evaluator(
    manifest_entry: dict,
    event_log: list[dict],
    state_snapshots: dict[str, dict],
    runtime_checkpoint_snapshots: dict[str, dict],
) -> dict:
    issues: list[dict] = []
    warnings: list[dict] = []
    evidence_events: list[dict] = []
    evidence_state: list[dict] = []
    evidence_diff: list[dict] = []
    evidence_projection: list[dict] = []

    checkpoints = manifest_entry.get("checkpoints", {})
    outcome = manifest_entry.get("outcome")
    clean_contract = manifest_entry.get("clean_contract", {})


    event_index: dict[str, list[dict]] = {}
    for ev in event_log:
        for etype in _event_names(ev):
            event_index.setdefault(etype, []).append(ev)


    checkpoint_status: dict[str, dict] = {}
    passed_process_count = 0
    total_process_count = len(checkpoints)

    for cp_id, cp_spec in checkpoints.items():
        cp_issues: list[str] = []
        event_verified = False


        required_event = cp_spec.get("required_event", "")
        matching_events = event_index.get(required_event, [])
        if matching_events:
            event_verified = True
            evidence_events.append({
                "checkpoint_id": cp_id,
                "event": required_event,
                "verified": True,
                "source": matching_events[0].get("source", "runtime"),
            })
        else:
            cp_issues.append("event_missing")
            evidence_events.append({
                "checkpoint_id": cp_id,
                "event": required_event,
                "verified": False,
            })


        payload_ok = True
        payload_assertions = cp_spec.get("payload_assertions", {})
        if event_verified and payload_assertions:
            event_payload = matching_events[0].get("payload", {})
            for field, assertion in payload_assertions.items():
                op = assertion.get("op", "eq")
                actual = event_payload.get(field)
                if not _check_assertion(op, actual, assertion.get("value")):
                    payload_ok = False
                    cp_issues.append("payload_mismatch")
                    issues.append({
                        "checkpoint_id": cp_id,
                        "code": "payload_mismatch",
                        "field": field,
                        "op": op,
                        "expected": assertion.get("value"),
                        "actual": actual,
                    })


        state_ok = True
        state_assertions = cp_spec.get("state_assertions", {})
        snapshot = _snapshot_for_checkpoint(state_snapshots, cp_id)
        for field, assertion in state_assertions.items():
            op = assertion.get("op", "eq")
            expected = assertion.get("value")
            actual = snapshot.get(field)
            if not _check_assertion(op, actual, expected):
                state_ok = False
                cp_issues.append("state_mismatch")
                issues.append({
                    "checkpoint_id": cp_id,
                    "code": "state_mismatch",
                    "field": field,
                    "op": op,
                    "expected": expected,
                    "actual": actual,
                })
            else:
                evidence_state.append({
                    "checkpoint_id": cp_id,
                    "field": field,
                    "op": op,
                    "matched": True,
                })


        diff_ok = True
        diff_assertions = cp_spec.get("diff_assertions", {})
        for field, assertion in diff_assertions.items():
            op = assertion.get("op", "eq")
            expected = assertion.get("value")
            actual = snapshot.get(field)
            if not _check_assertion(op, actual, expected):
                diff_ok = False
                cp_issues.append("diff_mismatch")
                issues.append({
                    "checkpoint_id": cp_id,
                    "code": "diff_mismatch",
                    "field": field,
                    "op": op,
                    "expected": expected,
                    "actual": actual,
                })
            else:
                evidence_diff.append({
                    "checkpoint_id": cp_id,
                    "field": field,
                    "op": op,
                    "matched": True,
                })


        projection_ok = True
        projection_assertions = cp_spec.get("projection_assertions", [])
        runtime_checkpoint_snapshot = runtime_checkpoint_snapshots.get(cp_id, {})
        for assertion in projection_assertions:
            op = assertion.get("op", "contains_visible")
            value = assertion.get("value", "")
            actual = runtime_checkpoint_snapshot.get("visible_text", [])
            if not _check_assertion(op, actual, value):
                projection_ok = False
                cp_issues.append("projection_mismatch")
                issues.append({
                    "checkpoint_id": cp_id,
                    "code": "projection_mismatch",
                    "op": op,
                    "expected": value,
                    "page": assertion.get("page", ""),
                })
            else:
                evidence_projection.append({
                    "checkpoint_id": cp_id,
                    "op": op,
                    "value": value,
                    "matched": True,
                })


        cp_passed = event_verified and payload_ok and state_ok and diff_ok and projection_ok
        if cp_passed:
            passed_process_count += 1

        checkpoint_status[cp_id] = {
            "passed": cp_passed,
            "event_verified": event_verified,
            "issues": cp_issues,
        }


    outcome_passed = False
    if outcome:
        outcome_issues: list[str] = []
        outcome_id = outcome.get("checkpoint_id", "outcome")


        blocking_ids = outcome.get("blocking_process_checkpoints", [])
        all_blocking_passed = all(
            checkpoint_status.get(bid, {}).get("passed", False)
            for bid in blocking_ids
        )
        if not all_blocking_passed:
            outcome_issues.append("process_checkpoint_failed")


        required_event = outcome.get("required_event", "")

        outcome_event_key = f"ghostweb:checkpoint:{outcome_id}" if not required_event else required_event
        outcome_events = event_index.get(outcome_event_key, [])
        outcome_event_verified = len(outcome_events) > 0
        if not outcome_event_verified:
            outcome_issues.append("event_missing")


        outcome_state_ok = True
        outcome_snapshot = _snapshot_for_checkpoint(state_snapshots, outcome_id)
        for field, assertion in outcome.get("state_assertions", {}).items():
            op = assertion.get("op", "eq")
            expected = assertion.get("value")
            actual = outcome_snapshot.get(field)
            if not _check_assertion(op, actual, expected):
                outcome_state_ok = False
                outcome_issues.append("state_mismatch")
                issues.append({
                    "checkpoint_id": outcome_id,
                    "code": "state_mismatch",
                    "field": field,
                    "op": op,
                    "expected": expected,
                    "actual": actual,
                })
            else:
                evidence_state.append({
                    "checkpoint_id": outcome_id,
                    "field": field,
                    "op": op,
                    "matched": True,
                })


        outcome_diff_ok = True
        for field, assertion in outcome.get("diff_assertions", {}).items():
            op = assertion.get("op", "eq")
            expected = assertion.get("value")
            actual = outcome_snapshot.get(field)
            if not _check_assertion(op, actual, expected):
                outcome_diff_ok = False
                outcome_issues.append("diff_mismatch")
                issues.append({
                    "checkpoint_id": outcome_id,
                    "code": "diff_mismatch",
                    "field": field,
                    "op": op,
                    "expected": expected,
                    "actual": actual,
                })
            else:
                evidence_diff.append({
                    "checkpoint_id": outcome_id,
                    "field": field,
                    "op": op,
                    "matched": True,
                })


        outcome_proj_ok = True
        outcome_runtime_checkpoint_snapshot = runtime_checkpoint_snapshots.get(outcome_id, {})
        for assertion in outcome.get("projection_assertions", []):
            op = assertion.get("op", "contains_visible")
            value = assertion.get("value", "")
            actual = outcome_runtime_checkpoint_snapshot.get("visible_text", [])
            if not _check_assertion(op, actual, value):
                outcome_proj_ok = False
                outcome_issues.append("projection_mismatch")


        forbidden = outcome.get("forbidden_events", [])
        forbidden_fired = False
        for fe in forbidden:
            if fe in event_index:
                forbidden_fired = True
                outcome_issues.append("forbidden_event_fired")
                issues.append({
                    "checkpoint_id": outcome_id,
                    "code": "forbidden_event_fired",
                    "event": fe,
                })

        outcome_passed = (
            all_blocking_passed
            and outcome_event_verified
            and outcome_state_ok
            and outcome_diff_ok
            and outcome_proj_ok
            and not forbidden_fired
        )

        checkpoint_status[outcome_id] = {
            "passed": outcome_passed,
            "event_verified": outcome_event_verified,
            "issues": outcome_issues,
        }


    blocking_process_ids = (
        outcome.get("blocking_process_checkpoints", [])
        if outcome
        else [cp_id for cp_id, cp in checkpoints.items() if cp.get("blocking", True)]
    )
    all_blocking_passed = all(
        checkpoint_status.get(bid, {}).get("passed", False)
        for bid in blocking_process_ids
    )
    success = all_blocking_passed and outcome_passed


    clean = True
    state_diff_evidence: dict = {}
    ignored_ns = set(clean_contract.get("ignored_namespaces", []))
    unexpected_codes = clean_contract.get("unexpected_state_diff_codes", [])


    init_state = state_snapshots.get("__init__", {})
    final_state = state_snapshots.get("__final__", {})
    if init_state and final_state:
        for key in set(list(init_state.keys()) + list(final_state.keys())):
            ns = key.split(".")[0] if "." in key else key
            if ns in ignored_ns:
                continue
            if init_state.get(key) != final_state.get(key):
                state_diff_evidence[key] = {
                    "init": init_state.get(key),
                    "final": final_state.get(key),
                }
                if "unexpected_state_diff" in unexpected_codes:
                    clean = False
                    issues.append({
                        "checkpoint_id": "__clean__",
                        "code": "unexpected_state_diff",
                        "field": key,
                    })


    passed = success and clean

    return {
        "success": success,
        "clean": clean,
        "progress": {
            "passed_process_checkpoints": passed_process_count,
            "process_checkpoint_count": total_process_count,
            "checkpoint_status": checkpoint_status,
        },
        "passed": passed,
        "issues": issues,
        "warnings": warnings,
        "evidence": {
            "events": evidence_events,
            "state_assertions": evidence_state,
            "diff_assertions": evidence_diff,
            "projection_assertions": evidence_projection,
            "state_diff": state_diff_evidence,
        },
    }
