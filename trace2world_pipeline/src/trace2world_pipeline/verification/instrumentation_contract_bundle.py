
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Mapping


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dedupe(values: list[Any]) -> list[str]:
    return sorted(dict.fromkeys(_text(value) for value in values if _text(value)))


def _dedupe_probe_specs(probes: list[Any]) -> list[Any]:
    deduped: list[Any] = []
    seen: set[str] = set()
    for probe in probes:
        key = json.dumps(probe, ensure_ascii=False, sort_keys=True) if isinstance(probe, dict) else _text(probe)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(probe)
    return deduped


def _step_number(action_id: str) -> int:
    if ":step" not in action_id:
        return -1
    try:
        return int(action_id.rsplit(":step", 1)[1])
    except (TypeError, ValueError):
        return -1


def _action_steps_by_trace_from_replay_plan(contract_replay_plan: Mapping[str, Any]) -> dict[str, set[int]]:
    steps_by_trace: dict[str, set[int]] = defaultdict(set)
    for trace in _as_list(contract_replay_plan.get("traces")):
        trace = _as_dict(trace)
        trace_id = _text(trace.get("trace_id") or trace.get("id"))
        if not trace_id:
            continue
        for step in _as_list(trace.get("steps")):
            step = _as_dict(step)
            operation = _text(step.get("operation")).lower()
            if operation in {"agent_execute", "open_app", "terminate", "init"}:
                continue
            action_id = _text(step.get("action_id"))
            step_num = _step_number(action_id)
            if step_num < 0:
                try:
                    step_num = int(step.get("step_index"))
                except (TypeError, ValueError):
                    step_num = -1
            if step_num >= 0:
                steps_by_trace[trace_id].add(step_num)
    return steps_by_trace


def _checkpoint_kind(effect_kind: str) -> str:
    mapping = {
        "select_result_return": "selection",
        "same_page_mutation": "mutation",
        "collection_mutation": "collection",
        "query_projection": "projection",
        "stateful_submit": "projection",
        "save_return": "mutation",
        "gesture_route": "route",
        "query_commit": "projection",
    }
    return mapping.get(effect_kind, effect_kind or "process")


def _is_stateful_same_page_route(action: Mapping[str, Any]) -> bool:
    if _text(action.get("effect_kind")) != "route":
        return False
    source = _text(action.get("source_filename"))
    target = _text(action.get("expected_target_filename"))
    if not source or source != target:
        return False
    if not _as_dict(action.get("expected_state_diff")):
        return False
    return bool(_as_list(action.get("state_refs")))


def _checkpoint_kind_for_action(action: Mapping[str, Any], effect_kind: str) -> str:
    if _is_stateful_same_page_route(action):
        return "selection"
    return _checkpoint_kind(effect_kind)


def _runtime_payload_assertions(action: Mapping[str, Any], transaction: Mapping[str, Any]) -> list[dict[str, Any]]:
    event = (_as_list(action.get("required_runtime_events")) or [""])[0]
    payload: dict[str, Any] = {
        "action_id": _text(action.get("action_id")),
        "effect_kind": _text(action.get("effect_kind")),
    }
    txn_id = _text(action.get("transaction_id"))
    if txn_id:
        payload["transaction_id"] = txn_id
    runtime_tx = _as_dict(action.get("runtime_transaction"))
    for key in ("selected_value", "field_role"):
        value = _text(runtime_tx.get(key) or action.get(key))
        if value:
            payload[key] = value
    state_reads = _as_list(runtime_tx.get("state_reads") or transaction.get("state_reads"))
    if state_reads:
        payload["state_reads"] = _dedupe(state_reads)
    state_refs = _as_list(action.get("state_refs"))
    if state_refs:
        payload["state_refs"] = _dedupe(state_refs)
    target = _text(action.get("expected_target_filename") or transaction.get("target_page") or transaction.get("return_target"))
    if target:
        payload["target_filename"] = target
    return [{"event": event, "payload_required": payload}] if event else []


def _projection_assertions(action: Mapping[str, Any], transaction: Mapping[str, Any], binding: Mapping[str, Any]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    projection = _as_dict(transaction.get("projection"))
    if projection:
        row = {
            "page": _text(projection.get("page") or action.get("expected_target_filename")),
            "state_ref": _text(projection.get("state_ref")),
            "expected_text": _text(projection.get("expected_text")),
            "selector_or_binding": _text(projection.get("selector_or_binding") or binding.get("dom_binding_id")),
        }
        assertions.append({k: v for k, v in row.items() if v})
    target = _text(action.get("expected_target_filename"))
    if target and not assertions:
        assertions.append({
            "page": target,
            "selector_or_binding": _text(binding.get("dom_binding_id") or action.get("dom_binding_ref")),
            "expected_state": "postcondition_visible",
        })
    return assertions


def _expected_state_diff(action: Mapping[str, Any], transaction: Mapping[str, Any]) -> dict[str, Any]:
    diff = dict(_as_dict(action.get("expected_state_diff")))
    if diff:
        return diff
    runtime_tx = _as_dict(action.get("runtime_transaction"))
    reads = _as_list(runtime_tx.get("state_reads") or transaction.get("state_reads"))
    target = _text(action.get("expected_target_filename") or transaction.get("target_page"))
    if reads or target:
        synthetic: dict[str, Any] = {}
        if reads:
            synthetic["state_reads"] = _dedupe(reads)
        if target:
            synthetic["target_filename"] = target
        return synthetic
    return {}


def _negative_probes(effect_kind: str, transaction: Mapping[str, Any]) -> list[str]:
    probes = _as_list(transaction.get("negative_probes"))
    if probes:
        return _dedupe(probes)
    mapping = {
        "select_result_return": ["wrong_entity", "missing_caller_context", "missing_projection"],
        "query_projection": ["missing_state_write", "wrong_query_value"],
        "stateful_submit": ["missing_state_read", "wrong_state_tuple", "wrong_route_target"],
        "same_page_mutation": ["missing_state_write", "wrong_same_page_state"],
        "save_return": ["missing_saved_state", "terminal_summary_only"],
        "gesture_route": ["missing_gesture_state", "wrong_route_target"],
    }
    return list(mapping.get(effect_kind, ["event_only_success"]))


def _negative_probes_for_action(action: Mapping[str, Any], transaction: Mapping[str, Any]) -> list[str]:
    if _is_stateful_same_page_route(action):
        return ["missing_state_write", "wrong_same_page_state"]
    return _negative_probes(_text(action.get("effect_kind")), transaction)


def _is_inline_activation_only(action: Mapping[str, Any]) -> bool:
    if _text(action.get("effect_kind")) != "same_page_mutation":
        return False
    refs = _as_list(action.get("state_refs"))
    diff = _as_dict(action.get("expected_state_diff"))
    semantic_diff_fields = {
        "user_selection",
        "selected_value",
        "state_driven_label_change",
        "cta_label",
        "committed_label",
    }
    if any(str(field or "").rsplit(".", 1)[-1] in semantic_diff_fields for field in diff):
        return False
    fields = [*_dedupe(refs), *_dedupe(list(diff.keys()))]
    if not fields:
        return False
    return all(
        "inline_activation" in field.lower()
        or field.lower().endswith(".active")
        or field.lower().endswith("_active")
        for field in fields
    )


def _is_terminal_boundary_action(
    action: Mapping[str, Any],
    terminal_by_trace: Mapping[str, Mapping[str, Any]],
) -> bool:
    trace_id = _text(action.get("trace_id"))
    action_id = _text(action.get("action_id"))
    if not trace_id or not action_id:
        return False
    terminal = _as_dict(terminal_by_trace.get(trace_id))
    boundary = _as_dict(terminal.get("terminal_boundary"))
    boundary_action_id = _text(
        boundary.get("source_action_id")
        or terminal.get("terminal_action_id")
    )
    if not boundary_action_id:
        evidence_steps = _as_list(terminal.get("evidence_steps"))
        boundary_action_id = _text(evidence_steps[-1]) if evidence_steps else ""
    return action_id == boundary_action_id


def _has_process_checkpoint_obligation(action: Mapping[str, Any], transaction: Mapping[str, Any]) -> bool:
    if _as_list(action.get("required_runtime_events")):
        return True
    if _text(action.get("transaction_id")):
        return True
    if _as_list(action.get("state_refs")):
        return True
    if _as_dict(action.get("expected_state_diff")):
        return True
    runtime_tx = _as_dict(action.get("runtime_transaction"))
    if _as_list(runtime_tx.get("state_reads")) or _as_list(runtime_tx.get("state_writes")):
        return True
    if _as_list(transaction.get("state_reads")):
        return True
    if _as_dict(transaction.get("projection")):
        return True
    return False


def _process_checkpoint(
    action: Mapping[str, Any],
    transaction: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    action_id = _text(action.get("action_id"))
    trace_id = _text(action.get("trace_id"))
    effect_kind = _text(action.get("effect_kind"))
    state_refs = _dedupe(_as_list(action.get("state_refs")) or _as_list(_as_dict(action.get("runtime_transaction")).get("state_reads")))
    expected_diff = _expected_state_diff(action, transaction)
    checkpoint_kind = _checkpoint_kind_for_action(action, effect_kind)
    route_only_justification = ""
    if (
        effect_kind == "save_return"
        and not state_refs
        and set(expected_diff.keys()) <= {"target_filename"}
    ):
        route_only_justification = "save_return_route_boundary_without_state_commit"
    checkpoint_id = f"cp_{checkpoint_kind}_{max(_step_number(action_id), 0):02d}"
    return {
        "checkpoint_id": checkpoint_id,
        "supervision_type": "process",
        "checkpoint_kind": checkpoint_kind,
        "trace_id": trace_id,
        "source_steps": [step for step in [_step_number(action_id)] if step >= 0],
        "action_ids": [action_id],
        "effect_kind": effect_kind,
        "transaction_id": _text(action.get("transaction_id")),
        "dom_binding_id": _text(action.get("dom_binding_ref") or binding.get("dom_binding_id")),
        "state_refs": state_refs,
        "expected_state_diff": expected_diff,
        "runtime_event_assertions": _runtime_payload_assertions(action, transaction),
        "visible_projection_assertions": _projection_assertions(action, transaction, binding),
        "negative_probe_specs": _negative_probes_for_action(action, transaction),
        "route_only_justification": route_only_justification,
        "blocking": True,
        "authority": "instrumentation_contract_bundle",
    }


def _route_checkpoint(action: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    action_id = _text(action.get("action_id"))
    target = _text(action.get("expected_target_filename"))
    checkpoint_id = f"cp_route_{max(_step_number(action_id), 0):02d}"
    return {
        "checkpoint_id": checkpoint_id,
        "supervision_type": "process",
        "checkpoint_kind": "route",
        "trace_id": _text(action.get("trace_id")),
        "source_steps": [step for step in [_step_number(action_id)] if step >= 0],
        "action_ids": [action_id],
        "effect_kind": "route",
        "transaction_id": "",
        "dom_binding_id": _text(action.get("dom_binding_ref") or binding.get("dom_binding_id")),
        "state_refs": ["state::page_runtime"],
        "expected_state_diff": {"target_filename": target},
        "runtime_event_assertions": _runtime_payload_assertions(action, {},),
        "visible_projection_assertions": [
            {
                "page": target,
                "selector_or_binding": _text(action.get("dom_binding_ref") or binding.get("dom_binding_id")),
                "expected_state": "postcondition_visible",
            }
        ],
        "negative_probe_specs": ["wrong_route_target", "missing_route_projection"],
        "route_only_justification": "typed_non_transaction_route_boundary",
        "blocking": True,
        "authority": "instrumentation_contract_bundle",
    }


def _normalize_outcome_field(field: Any) -> dict[str, Any]:
    if isinstance(field, dict):
        key = _text(
            field.get("field")
            or field.get("field_name")
            or field.get("name")
            or field.get("key")
        )
        expected = field.get(
            "expected",
            field.get("expected_value", field.get("value", field.get("text", ""))),
        )
        normalized = {"field": key or "outcome", "expected": expected}
        for extra_key in ("role", "entity", "source_steps", "state_ref", "assertion_channel"):
            if field.get(extra_key) not in (None, ""):
                normalized[extra_key] = field.get(extra_key)
        return normalized
    text = _text(field)
    return {"field": text or "outcome", "expected": text}


def _safe_state_token(value: Any) -> str:
    token = _text(value)
    token = token.replace(" ", "_").replace("->", "_to_")
    token = token.replace("/", "_").replace("\\", "_").replace(":", "_")
    return token or "unknown"


def _outcome_field_map(outcome_fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        _text(item.get("field")): item.get("expected")
        for item in outcome_fields
        if _text(item.get("field"))
    }


def _outcome_field_state_ref(field: dict[str, Any]) -> str:
    explicit = _text(field.get("state_ref"))
    if explicit:
        return explicit
    role = _text(field.get("role"))
    if role:
        return f"taskOutcome.{_safe_state_token(role)}.{_safe_state_token(field.get('field'))}"
    entity = _text(field.get("entity"))
    if entity:
        return f"taskOutcome.{_safe_state_token(entity)}.{_safe_state_token(field.get('field'))}"
    return f"taskOutcome.{_safe_state_token(field.get('field'))}"


def _structured_entity_outcome(
    outcome_fields: list[dict[str, Any]],
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    field_map = _outcome_field_map(outcome_fields)
    entity_type = field_map.get("target_entity_type")
    entity_name = field_map.get("target_entity_name")
    edited_field = field_map.get("edited_field")
    new_value = field_map.get("new_value")
    if not (entity_type and entity_name and edited_field and new_value not in (None, "")):
        return [], {}, {}
    field_name = _safe_state_token(edited_field)
    ref = f"state::collection.{_safe_state_token(entity_type)}[{_safe_state_token(entity_name)}].{field_name}"
    expected = {
        field_name: {
            "field_scope": "terminal_observable",
            "value": new_value,
        },
        "target_entity_name": {
            "field_scope": "terminal_observable",
            "value": entity_name,
        },
    }
    diff = {
        field_name: new_value,
        "target_entity_name": entity_name,
    }
    return [ref], expected, diff


def _outcome_runtime_contract(
    outcome_fields: list[dict[str, Any]],
) -> tuple[list[str], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    state_refs: list[str] = []
    expected_state: dict[str, Any] = {}
    expected_diff: dict[str, Any] = {}
    projection_assertions: list[dict[str, Any]] = []

    structured_refs, structured_state, structured_diff = _structured_entity_outcome(outcome_fields)
    state_refs.extend(structured_refs)
    expected_state.update(structured_state)
    expected_diff.update(structured_diff)

    for field in outcome_fields:
        name = _text(field.get("field"))
        if not name:
            continue
        expected = field.get("expected")
        if expected in (None, ""):
            continue
        normalized_name = _safe_state_token(name)
        state_refs.append(_outcome_field_state_ref(field))
        expected_state.setdefault(normalized_name, {
            "field_scope": "terminal_observable",
            "value": expected,
        })
        expected_diff.setdefault(normalized_name, expected)
        if isinstance(expected, (str, int, float)) and _text(expected):
            projection_assertions.append({
                "field": normalized_name,
                "op": "contains_visible",
                "value": str(expected),
            })

    return _dedupe(state_refs), expected_state, expected_diff, projection_assertions


def _outcome_field_negative_probes(
    checkpoint_id: str,
    outcome_fields: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = [
        {
            "probe_id": "terminal_summary_only_success",
            "mutate": {"__remove_event__": checkpoint_id},
            "must_fail": [checkpoint_id],
        }
    ]
    for field in outcome_fields:
        name = _safe_state_token(field.get("field"))
        expected = field.get("expected")
        if not name or expected in (None, ""):
            continue
        probes.append({
            "probe_id": f"wrong_outcome_{name}",
            "mutate": {name: f"__wrong__{expected}"},
            "must_fail": [checkpoint_id],
        })
        probes.append({
            "probe_id": f"missing_outcome_{name}",
            "mutate": {name: ""},
            "must_fail": [checkpoint_id],
        })
    return probes


def _outcome_checkpoint(
    trace_id: str,
    terminal_verified: Mapping[str, Any],
    terminal_candidate: Mapping[str, Any],
    available_action_steps: set[int] | None = None,
) -> dict[str, Any]:
    policy = _text(
        terminal_verified.get("verified_policy")
        or terminal_verified.get("candidate_policy")
        or terminal_candidate.get("terminal_policy_candidate")
        or "observed_terminal_state"
    )
    boundary = _as_dict(terminal_verified.get("terminal_boundary"))
    evidence_steps = _as_list(terminal_verified.get("evidence_steps") or terminal_candidate.get("evidence_steps"))
    source_steps: list[int] = []
    for item in evidence_steps:
        step = _step_number(_text(item))
        if step >= 0:
            source_steps.append(step)
    if not source_steps and terminal_candidate.get("last_task_relevant_step") is not None:
        try:
            source_steps.append(int(terminal_candidate.get("last_task_relevant_step")))
        except (TypeError, ValueError):
            pass
    if available_action_steps:
        source_steps = [step for step in source_steps if step in available_action_steps]
        if not source_steps:
            source_steps.append(max(available_action_steps))
    boundary_source_action_id = _text(boundary.get("source_action_id"))
    boundary_source_step = _step_number(boundary_source_action_id)
    if available_action_steps and source_steps and boundary_source_step not in available_action_steps:
        normalized_action_id = f"{trace_id}:step{source_steps[-1]}"
        boundary = dict(boundary)
        if boundary_source_action_id:
            boundary["original_source_action_id"] = boundary_source_action_id
        boundary["source_action_id"] = normalized_action_id
        boundary_source_action_id = normalized_action_id
    outcome_fields = [_normalize_outcome_field(item) for item in _as_list(terminal_candidate.get("outcome_fields"))]
    state_refs, runtime_expected_state, expected_diff, projection_assertions = _outcome_runtime_contract(outcome_fields)
    expected_state = {
        "terminal_policy": policy,
        "allowed_final_state": terminal_verified.get("allowed_final_state")
        or terminal_candidate.get("allowed_final_state")
        or policy,
        "outcome_fields": outcome_fields,
        **runtime_expected_state,
    }
    forbidden = _as_list(terminal_verified.get("forbidden_terminal_events") or terminal_candidate.get("forbidden_terminal_events"))
    required_mock = _as_list(terminal_verified.get("required_mock_state") or terminal_candidate.get("required_mock_state"))
    checkpoint_id = f"outcome_{policy}"
    probes: list[Any] = _outcome_field_negative_probes(checkpoint_id, outcome_fields)
    if policy == "simulated_commit":
        probes.extend(["missing_mock_side_effect", "real_side_effect_success"])
    if forbidden:
        probes.append("forbidden_real_side_effect")
    runtime_assertions: list[dict[str, Any]] = []
    payload_required = {"trace_id": trace_id, "checkpoint_id": checkpoint_id}
    payload_required.update(expected_diff)
    if boundary_source_action_id:
        payload_required["action_id"] = boundary_source_action_id
    runtime_assertions.append({
        "event": f"ghostweb:checkpoint:{checkpoint_id}",
        "payload_required": payload_required,
    })
    return {
        "checkpoint_id": checkpoint_id,
        "supervision_type": "outcome",
        "checkpoint_kind": "outcome",
        "trace_id": trace_id,
        "source_steps": sorted(dict.fromkeys(source_steps)),
        "action_ids": [boundary_source_action_id] if boundary_source_action_id else [],
        "terminal_policy": policy,
        "terminal_boundary": boundary,
        "expected_state": expected_state,
        "state_refs": state_refs,
        "expected_state_diff": expected_diff,
        "runtime_event_assertions": runtime_assertions,
        "visible_projection_assertions": projection_assertions,
        "forbidden_terminal_events": forbidden,
        "required_mock_side_effects": required_mock,
        "negative_probe_specs": _dedupe_probe_specs(probes),
        "blocking": True,
        "authority": "instrumentation_contract_bundle",
    }


def _load_trace_policies(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _text(trace_id): _as_dict(row)
        for trace_id, row in _as_dict(payload.get("trace_policies")).items()
        if _text(trace_id)
    }


def _reward_outcome_checkpoint(
    trace_id: str,
    reward: Mapping[str, Any],
    terminal_verified: Mapping[str, Any],
    terminal_candidate: Mapping[str, Any],
) -> dict[str, Any]:
    if not reward.get("canonical_outcome_ready"):
        raise ValueError(
            f"Canonical reward authority is blocked for {trace_id}: "
            f"{reward.get('problems') or []}"
        )
    assertions = _as_dict(reward.get("state_assertions"))
    expected_diff = {
        path: _as_dict(assertion).get("value")
        for path, assertion in assertions.items()
    }
    authorities = [_as_dict(row) for row in _as_list(reward.get("required_authorities"))]
    action_ids = [_text(row.get("action_id")) for row in authorities if _text(row.get("action_id"))]
    policy = _text(
        terminal_verified.get("verified_policy")
        or terminal_candidate.get("terminal_policy_candidate")
        or reward.get("outcome_kind")
    )
    outcome_fields = [
        {"field": path, "expected": value, "state_ref": path}
        for path, value in expected_diff.items()
    ]
    if not outcome_fields:
        outcome_fields = [
            {
                "field": "required_authority_receipts",
                "expected": [
                    {"action_id": row.get("action_id"), "receipt_event": row.get("receipt_event")}
                    for row in authorities
                ],
            }
        ]
    return {
        "checkpoint_id": _text(reward.get("outcome_id")) or f"outcome::{trace_id}",
        "supervision_type": "outcome",
        "checkpoint_kind": "outcome",
        "trace_id": trace_id,
        "source_steps": sorted({_step_number(action_id) for action_id in action_ids if _step_number(action_id) >= 0}),
        "action_ids": action_ids,
        "terminal_policy": policy,
        "terminal_boundary": _as_dict(terminal_verified.get("terminal_boundary")),
        "expected_state": {
            "terminal_policy": policy,
            "outcome_fields": outcome_fields,
            **{path: {"field_scope": "terminal_observable", "value": value} for path, value in expected_diff.items()},
        },
        "state_refs": sorted(expected_diff),
        "expected_state_diff": expected_diff,
        "canonical_state_assertions": assertions,
        "required_authorities": authorities,
        "runtime_event_assertions": [
            {
                "event": _text(row.get("receipt_event")),
                "payload_required": {
                    "task_id": trace_id,
                    "action_id": row.get("action_id"),
                    **({"transaction_id": row.get("transaction_id")} if row.get("transaction_id") else {}),
                },
            }
            for row in authorities
        ],
        "visible_projection_assertions": [],
        "forbidden_terminal_events": _dedupe([
            *_as_list(terminal_verified.get("forbidden_terminal_events")),
            *_as_list(terminal_candidate.get("forbidden_terminal_events")),
            *_as_list(reward.get("forbidden_effects")),
        ]),
        "required_mock_side_effects": _as_list(terminal_verified.get("required_mock_state")),
        "negative_probe_specs": _dedupe_probe_specs(_as_list(reward.get("negative_probes"))),
        "blocking": True,
        "authority": "reward_authority_bundle",
    }


def build_instrumentation_contract_bundle(
    *,
    action_effect_contracts: Mapping[str, Any],
    runtime_transaction_contracts: Mapping[str, Any],
    dom_binding_contracts: Mapping[str, Any],
    contract_replay_plan: Mapping[str, Any],
    runtime_binding_manifest: Mapping[str, Any],
    domain_contracts: Mapping[str, Any] | None = None,
    runtime_state_obligations: Mapping[str, Any] | None = None,
    page_execution_obligations: Mapping[str, Any] | None = None,
    terminal_verified: Mapping[str, Any],
    terminal_candidates: Mapping[str, Any] | None = None,
    reward_authority_bundle: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    actions = [_as_dict(action) for action in _as_list(action_effect_contracts.get("actions"))]
    tx_by_id = _as_dict(runtime_transaction_contracts.get("transactions_by_id"))
    bindings_by_id = _as_dict(dom_binding_contracts.get("bindings_by_id"))
    terminal_by_trace = _load_trace_policies(terminal_verified)
    candidate_by_trace = _load_trace_policies(terminal_candidates or {})
    reward_by_trace = {
        _text(row.get("task_id")): _as_dict(row)
        for row in _as_list((reward_authority_bundle or {}).get("contracts"))
        if _text(_as_dict(row).get("task_id"))
    }
    replay_trace_ids = {
        _text(trace.get("trace_id") or trace.get("id"))
        for trace in _as_list(contract_replay_plan.get("traces"))
        if _text(_as_dict(trace).get("trace_id") or _as_dict(trace).get("id"))
    }
    replay_action_steps_by_trace = _action_steps_by_trace_from_replay_plan(contract_replay_plan)
    action_rows_by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    route_candidates_by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    excluded_non_replay_action_ids: list[str] = []
    for action in actions:
        trace_id = _text(action.get("trace_id"))
        effect_kind = _text(action.get("effect_kind"))
        action_id = _text(action.get("action_id"))
        action_step = _step_number(action_id)
        replay_steps = replay_action_steps_by_trace.get(trace_id, set())
        if trace_id in replay_trace_ids and action_id and action_step not in replay_steps:
            excluded_non_replay_action_ids.append(action_id)
            continue
        if not trace_id or not action.get("reward_relevant"):
            continue
        if (
            effect_kind == "route"
            and _text(action.get("source_filename")) != _text(action.get("expected_target_filename"))
            and _text(action.get("operation")).lower() not in {"open_app", "agent_execute", "terminate"}
            and (action_step in replay_steps if replay_steps else True)
        ):
            route_candidates_by_trace[trace_id].append(action)
        if effect_kind not in {
            "select_result_return",
            "stateful_submit",
            "same_page_mutation",
            "collection_mutation",
            "query_projection",
            "save_return",
            "gesture_route",
            "query_commit",
        } and not _is_stateful_same_page_route(action):
            continue


        if _is_inline_activation_only(action) and _is_terminal_boundary_action(
            action,
            terminal_by_trace,
        ):
            continue
        tx = _as_dict(tx_by_id.get(_text(action.get("transaction_id"))))
        if not _has_process_checkpoint_obligation(action, tx):
            continue
        binding = _as_dict(bindings_by_id.get(_text(action.get("dom_binding_ref"))))
        action_rows_by_trace[trace_id].append(_process_checkpoint(action, tx, binding))

    trace_contracts: list[dict[str, Any]] = []
    for trace_id in sorted(replay_trace_ids | set(terminal_by_trace) | set(action_rows_by_trace)):
        terminal_row = terminal_by_trace.get(trace_id, {})
        candidate_row = candidate_by_trace.get(trace_id, {})
        reward_row = reward_by_trace.get(trace_id, {})
        forbidden_real_side_effects = _dedupe([
            *_as_list(terminal_row.get("forbidden_terminal_events")),
            *_as_list(candidate_row.get("forbidden_terminal_events")),
            *_as_list(reward_row.get("forbidden_effects")),
        ])
        checkpoints = list(action_rows_by_trace.get(trace_id, []))
        covered_action_ids = {
            action_id
            for checkpoint in checkpoints
            for action_id in _as_list(checkpoint.get("action_ids"))
            if _text(action_id)
        }
        for route_action in sorted(route_candidates_by_trace.get(trace_id, []), key=lambda item: _step_number(_text(item.get("action_id")))):
            route_action_id = _text(route_action.get("action_id"))
            if route_action_id in covered_action_ids:
                continue
            binding = _as_dict(bindings_by_id.get(_text(route_action.get("dom_binding_ref"))))
            checkpoints.append(_route_checkpoint(route_action, binding))
            covered_action_ids.add(route_action_id)
        if reward_authority_bundle is not None:
            if trace_id not in reward_by_trace:
                raise ValueError(f"Missing canonical reward authority for {trace_id}")
            checkpoints.append(_reward_outcome_checkpoint(
                trace_id,
                reward_by_trace[trace_id],
                terminal_row,
                candidate_row,
            ))
        else:
            checkpoints.append(_outcome_checkpoint(
                trace_id,
                terminal_row,
                candidate_row,
                replay_action_steps_by_trace.get(trace_id),
            ))
        trace_contracts.append({
            "trace_id": trace_id,
            "terminal_policy": _text(
                terminal_row.get("verified_policy")
                or terminal_row.get("candidate_policy")
                or candidate_row.get("terminal_policy_candidate")
                or "observed_terminal_state"
            ),
            "outcome_contract": {
                "allowed_terminal_state": terminal_row.get("allowed_final_state")
                or candidate_row.get("allowed_final_state"),
                "forbidden_real_side_effects": forbidden_real_side_effects,
                "required_mock_side_effects": terminal_row.get("required_mock_state")
                or candidate_row.get("required_mock_state")
                or [],
                "required_outcome_fields": candidate_row.get("outcome_fields") or [],
            },
            "checkpoints": checkpoints,
        })

    bundle = {
        "format": "instrumentation_contract_bundle",
        "schema_revision": 1,
        "source": "instrumentation_contract_bundle.compiler",
        "compiler_inputs": {
            "action_effect_contracts": action_effect_contracts.get("format", ""),
            "runtime_transaction_contracts": runtime_transaction_contracts.get("format", ""),
            "dom_binding_contracts": dom_binding_contracts.get("format", ""),
            "contract_replay_plan": contract_replay_plan.get("format", ""),
            "runtime_binding_manifest": runtime_binding_manifest.get("format", ""),
            "domain_contracts": (domain_contracts or {}).get("format", ""),
            "runtime_state_obligations": (runtime_state_obligations or {}).get("format", ""),
            "page_execution_obligations": (page_execution_obligations or {}).get("format", ""),
            "terminal_verified": terminal_verified.get("format", ""),
            "terminal_candidates": (terminal_candidates or {}).get("format", ""),
            "reward_authority_bundle": (reward_authority_bundle or {}).get("format", ""),
        },
        "trace_contracts": trace_contracts,
        "summary": _bundle_summary(trace_contracts),
        "excluded_non_replay_action_ids": sorted(excluded_non_replay_action_ids),
    }
    bundle["summary"]["excluded_non_replay_action_count"] = len(excluded_non_replay_action_ids)
    matrix_source = build_checkpoint_contract_matrix_source(bundle)
    validation = validate_instrumentation_contract_bundle(bundle)
    return bundle, matrix_source, validation


def _bundle_summary(trace_contracts: list[dict[str, Any]]) -> dict[str, Any]:
    all_rows = [cp for trace in trace_contracts for cp in _as_list(trace.get("checkpoints"))]
    kinds = Counter(_text(cp.get("checkpoint_kind")) for cp in all_rows)
    return {
        "trace_count": len(trace_contracts),
        "checkpoint_count": len(all_rows),
        "process_checkpoint_count": sum(1 for cp in all_rows if cp.get("supervision_type") == "process"),
        "outcome_checkpoint_count": sum(1 for cp in all_rows if cp.get("supervision_type") == "outcome"),
        "blocking_checkpoint_count": sum(1 for cp in all_rows if cp.get("blocking")),
        "checkpoint_kind_counts": dict(sorted(kinds.items())),
    }


def build_checkpoint_contract_matrix_source(bundle: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for trace in _as_list(bundle.get("trace_contracts")):
        trace = _as_dict(trace)
        for cp in _as_list(trace.get("checkpoints")):
            cp = _as_dict(cp)
            rows.append({
                "trace_id": trace.get("trace_id"),
                "checkpoint_id": cp.get("checkpoint_id"),
                "supervision_type": cp.get("supervision_type"),
                "checkpoint_kind": cp.get("checkpoint_kind"),
                "source_steps": cp.get("source_steps", []),
                "action_ids": cp.get("action_ids", []),
                "transaction_id": cp.get("transaction_id", ""),
                "dom_binding_id": cp.get("dom_binding_id", ""),
                "state_refs": cp.get("state_refs", []),
                "expected_state_diff": cp.get("expected_state_diff", {}),
                "expected_state": cp.get("expected_state", {}),
                "runtime_event_assertions": cp.get("runtime_event_assertions", []),
                "visible_projection_assertions": cp.get("visible_projection_assertions", []),
                "terminal_policy": cp.get("terminal_policy", trace.get("terminal_policy", "")),
                "negative_probe_specs": cp.get("negative_probe_specs", []),
                "blocking": bool(cp.get("blocking")),
                "authority": cp.get("authority", ""),
            })
    return {
        "format": "checkpoint_contract_matrix_source",
        "schema_revision": 1,
        "source": "instrumentation_contract_bundle.compiler",
        "bundle_ref": "phase_page_environment/semantic_api/09_instrumentation_contract_bundle/instrumentation_contract_bundle.json",
        "rows": rows,
        "summary": {
            "row_count": len(rows),
            "process_row_count": sum(1 for row in rows if row.get("supervision_type") == "process"),
            "outcome_row_count": sum(1 for row in rows if row.get("supervision_type") == "outcome"),
        },
    }


def validate_instrumentation_contract_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for trace in _as_list(bundle.get("trace_contracts")):
        trace = _as_dict(trace)
        trace_id = _text(trace.get("trace_id"))
        process_count = 0
        outcome_count = 0
        for cp in _as_list(trace.get("checkpoints")):
            cp = _as_dict(cp)
            cp_id = _text(cp.get("checkpoint_id"))
            supervision = _text(cp.get("supervision_type"))
            if supervision == "process":
                process_count += 1
                if cp.get("blocking") and not _text(cp.get("transaction_id")) and not _text(cp.get("route_only_justification")):
                    issues.append(_issue(trace_id, cp_id, "blocking_process_missing_transaction", "blocking process checkpoint has no transaction_id"))
                if cp.get("state_refs") and not _as_dict(cp.get("expected_state_diff")):
                    issues.append(_issue(trace_id, cp_id, "state_refs_without_expected_diff", "checkpoint has state_refs but no expected_state_diff"))
                if cp.get("state_refs") and not (cp.get("runtime_event_assertions") or cp.get("visible_projection_assertions")):
                    issues.append(_issue(trace_id, cp_id, "state_refs_without_runtime_or_projection_assertion", "checkpoint has state_refs but no runtime/projection assertion"))
                if cp.get("runtime_event_assertions") and not any(
                    _as_dict(assertion).get("payload_required") for assertion in _as_list(cp.get("runtime_event_assertions"))
                ):
                    issues.append(_issue(trace_id, cp_id, "event_name_only_assertion", "runtime event assertion has no required payload"))
            elif supervision == "outcome":
                outcome_count += 1
                expected_state = _as_dict(cp.get("expected_state"))
                outcome_fields = _as_list(expected_state.get("outcome_fields"))
                if not expected_state:
                    issues.append(_issue(trace_id, cp_id, "empty_outcome_assertions", "outcome checkpoint has no expected_state"))
                if not outcome_fields:
                    issues.append(_issue(trace_id, cp_id, "missing_terminal_outcome_fields", "outcome checkpoint has no terminal outcome fields"))
                if expected_state and set(expected_state) <= {"terminal_policy", "allowed_final_state"}:
                    issues.append(_issue(trace_id, cp_id, "page_only_or_policy_only_outcome", "outcome only checks terminal policy/final state"))
            if not cp.get("source_steps") and supervision != "outcome":
                issues.append(_issue(trace_id, cp_id, "missing_source_steps", "process checkpoint has no source_steps"))
            if not cp.get("negative_probe_specs"):
                issues.append(_issue(trace_id, cp_id, "missing_negative_probes", "checkpoint has no negative probes"))
        if outcome_count != 1:
            issues.append(_issue(trace_id, "", "missing_single_outcome_checkpoint", f"trace has {outcome_count} outcome checkpoints"))
        if process_count == 0:
            issues.append(_issue(trace_id, "", "missing_process_checkpoints", "trace has no process checkpoints"))

    blocking = [issue for issue in issues if issue.get("severity") == "blocking"]
    return {
        "format": "instrumentation_contract_validation_report",
        "schema_revision": 1,
        "source": "instrumentation_contract_bundle.validator",
        "summary": {
            "status": "pass" if not blocking else "fail",
            "issue_count": len(issues),
            "blocking_issue_count": len(blocking),
            "issue_code_counts": dict(sorted(Counter(_text(issue.get("code")) for issue in issues).items())),
        },
        "issues": issues,
    }


def _issue(trace_id: str, checkpoint_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "severity": "blocking",
        "trace_id": trace_id,
        "checkpoint_id": checkpoint_id,
        "code": code,
        "message": message,
    }
