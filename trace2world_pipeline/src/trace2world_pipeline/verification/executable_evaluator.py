
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


NON_CANONICAL_ASSERTION_KEYS = {
    "operation_intent",
    "terminal_policy",
    "allowed_final_state",
    "outcome_fields",


    "terminal_observable_dom_bindings",
}


def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_optional_json(path: Path, default: dict | None = None) -> dict:
    if not path.is_file():
        return {} if default is None else default
    return _load_json(path)


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  wrote: {path}")


def _extract_field_scope(value) -> tuple[str, Any]:
    if isinstance(value, dict) and "field_scope" in value:
        return value["field_scope"], value.get("value", value)
    return "unscoped", value


def _compile_typed_matcher(key: str, raw_value) -> dict:
    if isinstance(raw_value, list):
        items = [str(v) for v in raw_value]
        return {
            "match_mode": "alias_any",
            "canonical_value": items[0] if items else "",
            "aliases": items,
            "disambiguation_set": items,
        }
    str_val = str(raw_value)
    if any(c in str_val for c in ("¥", "$", "￥")):
        return {
            "match_mode": "numeric_tolerance",
            "canonical_value": str_val,
            "tolerance": 0.01,
        }
    if any(c in str_val for c in ("区", "路", "街", "号", "小区", "楼")):
        return {
            "match_mode": "address_normalized",
            "canonical_value": str_val,
        }
    if len(str_val) >= 7 and str_val.isdigit():
        return {
            "match_mode": "phone_masked",
            "canonical_value": str_val,
        }
    return {
        "match_mode": "normalized_text",
        "canonical_value": str_val,
    }


def _assertion_op_for_field(key: str, raw_value) -> str:
    field_l = str(key).lower()
    if isinstance(raw_value, dict) and {"min", "max"} <= set(raw_value):
        return "numeric_range_eq"
    if isinstance(raw_value, list):
        if "count" in field_l:
            return "count_eq"
        if any(token in field_l for token in ("selected", "checked", "values", "vehicles", "options")):
            return "set_contains_all"
        return "set_contains_all"
    if isinstance(raw_value, (int, float)) and "count" in field_l:
        return "numeric_eq"
    if any(token in field_l for token in ("selected", "checked")) and not isinstance(raw_value, bool):
        return "selected_contains"
    return "eq"


def _compile_state_assertions(expected_state: dict) -> dict:
    if not expected_state:
        return {}
    result = {}
    for k, v in expected_state.items():
        if k in NON_CANONICAL_ASSERTION_KEYS:
            continue
        field_scope, raw_value = _extract_field_scope(v)
        assertion: dict = {
            "op": _assertion_op_for_field(k, raw_value),
            "value": raw_value,
            "assertion_channel": field_scope,
        }
        if isinstance(raw_value, (str, list, dict)) and raw_value:
            assertion["typed_matcher"] = _compile_typed_matcher(k, raw_value)
        result[k] = assertion
    return result


def _compile_diff_assertions(expected_state_diff: dict) -> dict:
    if not expected_state_diff:
        return {}
    assertions: dict[str, dict[str, Any]] = {}
    for key, value in expected_state_diff.items():
        if key in NON_CANONICAL_ASSERTION_KEYS:
            continue
        assertion_value = value
        assertion: dict[str, Any] = {"op": "eq"}
        if isinstance(value, dict) and "value" in value:
            assertion_value = value.get("value")
            if value.get("field"):
                assertion["source_field"] = value.get("field")
        assertion["value"] = assertion_value
        assertions[key] = assertion
    return assertions


def _compile_payload_assertions(payload_fields: list[str]) -> dict:
    if not payload_fields:
        return {}
    return {field: {"op": "present"} for field in payload_fields}


def _declared_canonical_assertions(cp: dict) -> dict[str, dict[str, Any]]:
    declared = cp.get("canonical_assertions")
    if declared is None:
        declared = cp.get("canonical_state_assertions")
    if not isinstance(declared, dict):
        return {}

    compiled: dict[str, dict[str, Any]] = {}
    for field, assertion in declared.items():
        if not isinstance(field, str) or not field or not isinstance(assertion, dict):
            continue
        if "op" not in assertion or "value" not in assertion:
            continue
        normalized = dict(assertion)
        normalized.setdefault("assertion_channel", "terminal_observable")
        compiled[field] = normalized
    return compiled


def _checkpoint_assertion_contract(cp: dict) -> dict[str, Any]:
    global_rt = cp.get("global_runtime_state") or {}
    expected_state = dict(cp.get("expected_state") or {})
    declared = _declared_canonical_assertions(cp)
    if not expected_state and declared:
        expected_state = {
            field: {
                "field_scope": assertion.get("assertion_channel", "terminal_observable"),
                "value": assertion["value"],
            }
            for field, assertion in declared.items()
        }

    canonical_diff = dict(global_rt.get("canonical_expected_state_diff") or {})
    if not canonical_diff and declared:
        canonical_diff = {
            field: {"field": field, "value": assertion["value"]}
            for field, assertion in declared.items()
        }
    downstream_diff = dict(global_rt.get("downstream_expected_state_diff") or {})
    legacy_diff = dict(global_rt.get("expected_state_diff") or {})
    canonical_refs = list(global_rt.get("canonical_state_refs") or declared.keys())
    downstream_refs = list(global_rt.get("downstream_extension_refs") or [])
    legacy_refs = list(global_rt.get("legacy_state_refs") or global_rt.get("state_refs") or [])
    fields = sorted({
        *(
            field for field in expected_state
            if field not in NON_CANONICAL_ASSERTION_KEYS
        ),
        *canonical_diff,
        *downstream_diff,
    })
    return {
        "checkpoint_id": cp.get("checkpoint_id"),
        "state_refs": canonical_refs or downstream_refs or legacy_refs,
        "legacy_state_refs": legacy_refs,
        "canonical_state_refs": canonical_refs,
        "downstream_extension_refs": downstream_refs,
        "expected_state": expected_state,
        "expected_state_diff": canonical_diff or downstream_diff or legacy_diff,
        "legacy_expected_state_diff": legacy_diff,
        "canonical_expected_state_diff": canonical_diff,
        "downstream_expected_state_diff": downstream_diff,
        "fields": fields,
    }


def _compile_projection_assertions(projection_checks: list[dict] | None) -> list[dict]:
    if not projection_checks:
        return []
    assertions = []
    for check in projection_checks:
        page = check.get("page", "")
        if check.get("value") is not None:
            assertions.append({
                "page": page,
                "op": check.get("op", "contains_visible"),
                "field": check.get("field", ""),
                "value": check.get("value"),
            })
            continue
        for text in check.get("must_display", []):
            assertions.append({
                "page": page,
                "op": "contains_visible",
                "value": text,
            })
    return assertions


def _compile_negative_probe_specs(negative_probes: list[dict] | None) -> list[dict]:
    if not negative_probes:
        return []
    specs: list[dict] = []
    for probe in negative_probes:
        if isinstance(probe, str):
            specs.append({"probe_id": probe, "must_fail": [], "probe_unbound_to_assertion": True})
            continue
        if not isinstance(probe, dict) or not probe.get("probe_id"):
            continue
        spec = {
            "probe_id": probe["probe_id"],
            "must_fail": probe.get("must_fail", []),
        }
        if "mutate" in probe:
            spec["mutate"] = probe["mutate"]
        if "forbidden_events" in probe:
            spec["forbidden_events"] = probe["forbidden_events"]
        if probe.get("probe_unbound_to_assertion"):
            spec["probe_unbound_to_assertion"] = True
        specs.append(spec)
    return specs


def _dedupe_probe_specs(probes: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for probe in probes:
        if not isinstance(probe, dict) or not probe.get("probe_id"):
            continue
        normalized = {
            "probe_id": probe.get("probe_id"),
            "must_fail": probe.get("must_fail", []),
            "mutate": probe.get("mutate"),
            "forbidden_events": probe.get("forbidden_events") or probe.get("events"),
        }
        key = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out = {
            "probe_id": probe["probe_id"],
            "must_fail": probe.get("must_fail", []),
        }
        if "mutate" in probe:
            out["mutate"] = probe["mutate"]
        forbidden_events = probe.get("forbidden_events") or probe.get("events")
        if forbidden_events:
            out["forbidden_events"] = forbidden_events
        deduped.append(out)
    return deduped


def _index_trace_flows(test_flows: dict) -> dict[str, dict]:
    flows = test_flows.get("trace_flows") or {}
    if isinstance(flows, dict):
        return {str(k): v for k, v in flows.items() if isinstance(v, dict)}
    indexed: dict[str, dict] = {}
    for flow in flows or []:
        if not isinstance(flow, dict):
            continue
        trace_id = flow.get("trace_id") or flow.get("task_id") or flow.get("id")
        if trace_id:
            indexed[str(trace_id)] = flow
    return indexed


def _compile_process_checkpoint(cp: dict, binding: dict | None) -> dict:
    expected_state = cp.get("expected_state", {})
    global_rt = cp.get("global_runtime_state", {})
    expected_state_diff = global_rt.get("expected_state_diff", {})
    canonical_expected_state_diff = global_rt.get("canonical_expected_state_diff", {})
    downstream_expected_state_diff = global_rt.get("downstream_expected_state_diff", {})

    state_assertions = _compile_state_assertions(expected_state)
    for assertion in state_assertions.values():
        if assertion.get("assertion_channel") == "unscoped":
            assertion["assertion_channel"] = "process_prerequisite"

    result = {
        "supervision": cp.get("supervision", "process"),
        "blocking": cp.get("blocking", True),
        "required_event": cp.get("event", ""),
        "assertion_channels": {
            "process_prerequisite": state_assertions,
        },
        "payload_assertions": _compile_payload_assertions(cp.get("payload_fields", [])),
        "state_assertions": state_assertions,
        "diff_assertions": _compile_diff_assertions(expected_state_diff),
        "canonical_diff_assertions": _compile_diff_assertions(canonical_expected_state_diff),
        "downstream_diff_assertions": _compile_diff_assertions(downstream_expected_state_diff),
        "legacy_state_refs": global_rt.get("legacy_state_refs") or global_rt.get("state_refs", []),
        "canonical_state_refs": global_rt.get("canonical_state_refs", []),
        "downstream_extension_refs": global_rt.get("downstream_extension_refs", []),
        "projection_assertions": _compile_projection_assertions(cp.get("projection_assertions") or cp.get("projection_checks")),
        "negative_probe_specs": _compile_negative_probe_specs(cp.get("negative_probes")),
        "depends_on": cp.get("depends_on", []),
        "source_steps": cp.get("source_steps", []),
        "reward_weight": cp.get("reward_weight", 0.0),
        "failure_codes": _process_failure_codes(cp),
    }
    return result


def _process_failure_codes(cp: dict) -> list[str]:
    codes = ["event_missing"]
    if cp.get("expected_state"):
        codes.append("state_mismatch")
    if cp.get("global_runtime_state", {}).get("expected_state_diff"):
        codes.append("diff_mismatch")
    if cp.get("projection_checks"):
        codes.append("projection_mismatch")
    if cp.get("payload_fields"):
        codes.append("payload_mismatch")
    return codes


def _compile_outcome_checkpoint(cp: dict, process_cps: list[dict]) -> dict:
    expected_state = cp.get("expected_state", {})
    global_rt = cp.get("global_runtime_state", {})
    expected_state_diff = global_rt.get("expected_state_diff", {})
    canonical_expected_state_diff = global_rt.get("canonical_expected_state_diff", {})
    downstream_expected_state_diff = global_rt.get("downstream_expected_state_diff", {})

    forbidden_events = list(cp.get("forbidden_events", []))
    if "real_payment_success" not in forbidden_events:
        forbidden_events.append("real_payment_success")

    blocking_process_ids = [
        p["checkpoint_id"] for p in process_cps if p.get("blocking", True)
    ]

    all_state_assertions = _compile_state_assertions(expected_state)
    declared_canonical_assertions = _declared_canonical_assertions(cp)
    for field, assertion in declared_canonical_assertions.items():
        all_state_assertions.setdefault(field, assertion)

    for assertion in all_state_assertions.values():
        if assertion.get("assertion_channel") == "unscoped":
            assertion["assertion_channel"] = "terminal_observable"
    terminal_observable_assertions = {
        k: v for k, v in all_state_assertions.items()
        if v.get("assertion_channel") == "terminal_observable"
    }
    process_prerequisite_assertions = {
        k: v for k, v in all_state_assertions.items()
        if v.get("assertion_channel") == "process_prerequisite"
    }

    result = {
        "checkpoint_id": cp["checkpoint_id"],
        "requires_all_process": True,
        "blocking_process_checkpoints": blocking_process_ids,
        "forbidden_events": forbidden_events,
        "required_mock_state": cp.get("required_mock_state", []),
        "terminal_policy": cp.get("terminal_policy", ""),
        "terminal_boundary_target": cp.get("terminal_boundary_target", ""),
        "terminal_boundary_override_source": cp.get("terminal_boundary_override_source", ""),
        "assertion_channels": {
            "terminal_observable": terminal_observable_assertions,
            "process_prerequisite": process_prerequisite_assertions,
            "commit_payload": _compile_payload_assertions(cp.get("payload_fields", [])),
            "mock_side_effect": cp.get("required_mock_state", []),
            "forbidden_side_effect": forbidden_events,
        },
        "state_assertions": terminal_observable_assertions,
        "diff_assertions": _compile_diff_assertions(expected_state_diff),
        "canonical_diff_assertions": (
            _compile_diff_assertions(canonical_expected_state_diff)
            or declared_canonical_assertions
        ),
        "downstream_diff_assertions": _compile_diff_assertions(downstream_expected_state_diff),
        "legacy_state_refs": global_rt.get("legacy_state_refs") or global_rt.get("state_refs", []),
        "canonical_state_refs": (
            global_rt.get("canonical_state_refs", [])
            or list(declared_canonical_assertions)
        ),
        "downstream_extension_refs": global_rt.get("downstream_extension_refs", []),
        "projection_assertions": _compile_projection_assertions(cp.get("projection_assertions") or cp.get("projection_checks")),
        "negative_probe_specs": _compile_negative_probe_specs(cp.get("negative_probes")),
        "depends_on": cp.get("depends_on", []),
        "source_steps": cp.get("source_steps", []),
        "reward_weight": cp.get("reward_weight", 0.0),
        "failure_codes": [
            "event_missing",
            "forbidden_event_fired",
            "state_mismatch",
            "mock_state_missing",
            "process_checkpoint_failed",
        ],
    }
    return result


def _build_clean_contract() -> dict:
    return {
        "unexpected_state_diff_codes": [
            "unexpected_state_diff",
            "stale_entity",
            "extra_mutation",
        ],
        "ignored_namespaces": [
            "transient_ui",
            "scroll_position",
        ],
    }


def compile_executable_evaluator(result_dir: Path) -> dict:
    ppd = result_dir / "phase_post_processing"


    checkpoint_matrix = _load_json(ppd / "instrumentation" / "checkpoint_matrix.json")
    terminal_policy = _load_json(ppd / "terminal_policy" / "terminal_policy_verified.json")
    runtime_bindings = _load_json(ppd / "instrumentation" / "runtime_event_bindings.json")
    evaluators = _load_json(ppd / "evaluators" / "evaluators.json")
    test_flows = _load_optional_json(ppd / "test_logic" / "test_flows.json", {"trace_flows": {}})
    environment_state_binding = _load_optional_json(
        ppd / "evaluators" / "evaluator_state_bindings.json",
        {},
    )


    binding_lookup: dict[tuple[str, str], dict] = {}
    for b in runtime_bindings.get("bindings", []):
        key = (b["trace_id"], b["checkpoint_id"])
        binding_lookup[key] = b


    policy_lookup: dict[str, dict] = {}
    trace_policies = terminal_policy.get("trace_policies", {})
    if isinstance(trace_policies, dict):
        for tid, pol in trace_policies.items():
            if isinstance(pol, dict):
                policy_lookup[str(tid)] = pol
    else:
        for pol in trace_policies or []:
            if isinstance(pol, dict):
                tid = pol.get("trace_id") or pol.get("task_id") or pol.get("id")
                if tid:
                    policy_lookup[str(tid)] = pol

    flow_lookup = _index_trace_flows(test_flows or {})


    trace_checkpoints: dict[str, list[dict]] = defaultdict(list)
    for cp in checkpoint_matrix.get("checkpoints", []):
        trace_checkpoints[cp["trace_id"]].append(cp)


    compiled_evaluators: dict[str, dict] = {}

    for trace_id, cps in sorted(trace_checkpoints.items()):
        process_cps = [cp for cp in cps if cp.get("supervision") != "outcome"]
        outcome_cps = [cp for cp in cps if cp.get("supervision") == "outcome"]


        tp = policy_lookup.get(trace_id, {})
        trace_terminal_policy = tp.get("verified_policy", "")


        compiled_checkpoints: dict[str, dict] = {}
        for cp in process_cps:
            binding = binding_lookup.get((trace_id, cp["checkpoint_id"]))
            compiled_checkpoints[cp["checkpoint_id"]] = _compile_process_checkpoint(cp, binding)


        outcome = None
        if outcome_cps:
            outcome_cp = outcome_cps[0]
            outcome = _compile_outcome_checkpoint(outcome_cp, process_cps)
            flow_probes = (flow_lookup.get(trace_id) or {}).get("negative_probes") or []
            if flow_probes:
                outcome["negative_probe_specs"] = _dedupe_probe_specs([
                    *outcome.get("negative_probe_specs", []),
                    *flow_probes,
                ])


        if outcome and tp.get("forbidden_terminal_events"):
            merged = set(outcome["forbidden_events"])
            merged.update(tp["forbidden_terminal_events"])
            outcome["forbidden_events"] = sorted(merged)

        entry = {
            "trace_id": trace_id,
            "terminal_policy": trace_terminal_policy,
            "checkpoints": compiled_checkpoints,
        }
        if environment_state_binding:
            entry["state_source"] = "finalized_environment_state_snapshot"
            entry["requires_evaluation_handle"] = True
        if outcome:
            entry["outcome"] = outcome
        entry["clean_contract"] = _build_clean_contract()
        entry["pass_rule"] = "passed = success && clean"
        entry["result_fields"] = [
            "success", "clean", "progress", "passed",
            "issues", "warnings", "evidence",
        ]

        compiled_evaluators[trace_id] = entry


    manifest = {
        "format": "ghostweb_executable_evaluator_manifest",
        "schema_revision": 1,
        "reward_oracle_mode": "runtime_state_payload_projection",
        "source": {
            "checkpoint_matrix": "phase_post_processing/instrumentation/checkpoint_matrix.json",
            "terminal_policy": "phase_post_processing/terminal_policy/terminal_policy_verified.json",
            "runtime_event_bindings": "phase_post_processing/instrumentation/runtime_event_bindings.json",
            "evaluators": "phase_post_processing/evaluators/evaluators.json",
        },
        "trace_count": len(compiled_evaluators),
        "total_checkpoints": sum(
            len(e["checkpoints"]) for e in compiled_evaluators.values()
        ),
        "evaluators": compiled_evaluators,
    }
    if environment_state_binding:
        manifest["reward_oracle_mode"] = "finalized_environment_state_snapshot"
        manifest["environment_state_binding"] = environment_state_binding
        manifest["source"]["evaluator_state_binding"] = (
            "phase_post_processing/evaluators/evaluator_state_bindings.json"
        )


    _update_evalators_json(
        evaluators,
        manifest,
        checkpoint_matrix,
        ppd / "evaluators" / "evaluators.json",
    )

    manifest_path = ppd / "evaluators" / "executable_evaluator_manifest.json"
    _save_json(manifest_path, manifest)

    return manifest


def _update_evalators_json(
    evaluators: dict,
    manifest: dict,
    checkpoint_matrix: dict,
    path: Path,
) -> None:
    clean_contract = _build_clean_contract()


    evaluators["executable_manifest_ref"] = (
        "phase_post_processing/evaluators/executable_evaluator_manifest.json"
    )
    evaluators["reward_oracle_mode"] = "runtime_state_payload_projection"
    evaluators["evaluation_contract"] = {
        "pass_rule": "passed = success && clean",
        "result_fields": [
            "success", "clean", "progress", "passed",
            "issues", "warnings", "evidence",
        ],
    }
    evaluators["clean_contract"] = clean_contract

    rows_by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in checkpoint_matrix.get("checkpoints", []):
        if isinstance(row, dict) and row.get("trace_id"):
            rows_by_trace[str(row["trace_id"])].append(row)

    compiled_by_trace = manifest.get("evaluators") or {}
    for evaluator in evaluators.get("evaluators", []):
        if not isinstance(evaluator, dict):
            continue
        trace_id = str(evaluator.get("task_id") or "")
        compiled = compiled_by_trace.get(trace_id)
        if not isinstance(compiled, dict):
            continue
        rows = rows_by_trace.get(trace_id, [])
        process_ids = [
            str(row.get("checkpoint_id"))
            for row in rows
            if row.get("supervision") != "outcome" and row.get("checkpoint_id")
        ]
        outcome_row = next(
            (row for row in rows if row.get("supervision") == "outcome"),
            None,
        )
        outcome_id = str(outcome_row.get("checkpoint_id")) if outcome_row else None
        required_ids = [
            str(row.get("checkpoint_id"))
            for row in rows
            if row.get("checkpoint_id") and row.get("blocking", True)
        ]
        outcome = compiled.get("outcome") or {}

        evaluation_contract = evaluator.setdefault("evaluation_contract", {})
        evaluation_contract.setdefault("contract_style", "appwise_semantic_state")
        evaluation_contract["result_fields"] = [
            "success", "clean", "progress", "passed", "issues", "warnings",
        ]
        evaluation_contract["pass_rule"] = "passed = success && clean"
        evaluation_contract["success_contract"] = {
            "required_checkpoints": required_ids,
            "outcome_checkpoint": outcome_id,
            "forbidden_terminal_events": list(outcome.get("forbidden_events") or []),
            "required_mock_state": list(outcome.get("required_mock_state") or []),
        }
        evaluation_contract["progress_contract"] = {
            "process_checkpoints": process_ids,
            "denominator": len(process_ids),
            "metric": "passed_process_checkpoints / process_checkpoint_count",
        }
        evaluation_contract.setdefault("clean_contract", clean_contract)

        reward_contract = evaluator.setdefault("reward_contract", {})
        reward_contract["pass_rule"] = "success && clean"
        reward_contract["requires"] = required_ids
        reward_contract["forbids"] = list(outcome.get("forbidden_events") or [])
        reward_contract["required_mock_state"] = list(outcome.get("required_mock_state") or [])
        reward_contract["canonical_state_assertions"] = [
            _checkpoint_assertion_contract(row)
            for row in rows
            if (
                row.get("expected_state")
                or row.get("global_runtime_state")
                or _declared_canonical_assertions(row)
            )
        ]

    _save_json(path, evaluators)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile executable evaluator manifest from declarative artifacts"
    )
    parser.add_argument(
        "--result-dir",
        type=Path,
        required=True,
        help="Path to the result directory",
    )
    args = parser.parse_args()

    result_dir = args.result_dir
    if not result_dir.exists():
        print(f"ERROR: result directory does not exist: {result_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Compiling executable evaluator manifest for: {result_dir.name}")
    manifest = compile_executable_evaluator(result_dir)

    print(f"\nSummary:")
    print(f"  traces: {manifest['trace_count']}")
    print(f"  total checkpoints: {manifest['total_checkpoints']}")
    print(f"  reward_oracle_mode: {manifest['reward_oracle_mode']}")


if __name__ == "__main__":
    main()
