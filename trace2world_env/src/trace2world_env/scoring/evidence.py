"""Strict state-kernel evidence contracts.

A contract (``trace2world.state-kernel-evidence-contract.v1``) declares, per checkpoint, bounded
expressions over the canonical snapshot state and optionally a causal event sequence whose
single transaction anchor supplies the post-state the expressions are checked against.
:func:`reduce_state_kernel_evidence` validates the snapshot/event carrier, evaluates every
checkpoint and forbidden condition, gates checkpoints by their dependencies, and returns the
reward payload.
"""

from __future__ import annotations

import json
import math
from typing import Any

from trace2world_env.scoring.errors import EvidenceContractError
from trace2world_env.scoring.expressions import (
    EXPRESSION_SCHEMA,
    MISSING,
    canonical_equal,
    canonical_hash,
    canonical_json,
    evaluate_expression,
    normalize_expression,
    pointer_tokens,
    resolve_pointer,
)
from trace2world_env.scoring.state_replay import is_valid_diff_row, validate_carrier


CONTRACT_SCHEMA = "trace2world.state-kernel-evidence-contract.v1"
_STRICT_MODE = "strict_state_kernel_v1"


def _normalize_causal_event_sequence(raw: Any) -> list[dict[str, Any]]:
    """Validate a causal event sequence: ordered transaction/receipt bindings with exactly
    one transaction ``state_anchor`` and at least one state-affecting transaction.
    """
    if not isinstance(raw, list) or not raw:
        raise EvidenceContractError("causal_event_sequence must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    binding_ids: set[str] = set()
    has_state_causal_transaction = False
    anchor_count = 0
    for row in raw:
        if not isinstance(row, dict):
            raise EvidenceContractError("causal event binding must be an object")
        event_kind = row.get("event_kind")
        expected_fields = {
            "binding_id",
            "event_kind",
            "event_type",
            "affected_state",
            "state_anchor",
            "transaction_id" if event_kind == "transaction" else "action_id",
        }
        if event_kind not in {"transaction", "receipt"} or set(row) != expected_fields:
            raise EvidenceContractError("causal event binding fields are invalid")
        binding_id = row.get("binding_id")
        event_type = row.get("event_type")
        identity_field = "transaction_id" if event_kind == "transaction" else "action_id"
        identity_value = row.get(identity_field)
        state_anchor = row.get("state_anchor")
        if (
            not isinstance(binding_id, str)
            or not binding_id
            or len(binding_id) > 256
            or binding_id in binding_ids
            or not isinstance(event_type, str)
            or not event_type
            or len(event_type) > 256
            or not isinstance(identity_value, str)
            or not identity_value
            or len(identity_value) > 512
            or not isinstance(state_anchor, bool)
        ):
            raise EvidenceContractError("causal event identity is invalid")
        if state_anchor and event_kind != "transaction":
            raise EvidenceContractError("only a transaction event may anchor state")
        anchor_count += int(state_anchor)
        affected_state = row.get("affected_state")
        if not isinstance(affected_state, list):
            raise EvidenceContractError("affected_state must be a list")
        if event_kind == "transaction" and not affected_state:
            raise EvidenceContractError("transaction causal binding requires affected raw state")
        if event_kind == "receipt" and affected_state:
            raise EvidenceContractError("receipt identity cannot masquerade as state-diff evidence")
        normalized_affected = []
        for affected in affected_state:
            if not isinstance(affected, dict) or set(affected) != {
                "diff_path",
                "state_pointer",
            }:
                raise EvidenceContractError("affected state binding is invalid")
            diff_path = affected.get("diff_path")
            state_pointer = affected.get("state_pointer")
            if (
                not isinstance(diff_path, str)
                or not diff_path.startswith("$.")
                or len(diff_path) > 1024
            ):
                raise EvidenceContractError("affected diff_path is invalid")
            pointer_tokens(state_pointer)
            normalized_affected.append(
                {
                    "diff_path": diff_path,
                    "state_pointer": state_pointer,
                }
            )
        has_state_causal_transaction = (
            has_state_causal_transaction
            or event_kind == "transaction"
            and bool(normalized_affected)
        )
        binding_ids.add(binding_id)
        normalized.append(
            {
                "binding_id": binding_id,
                "event_kind": event_kind,
                "event_type": event_type,
                identity_field: identity_value,
                "state_anchor": state_anchor,
                "affected_state": normalized_affected,
            }
        )
    if not has_state_causal_transaction:
        raise EvidenceContractError(
            "identity/receipt-only causal sequence cannot grant checkpoint credit"
        )
    if anchor_count != 1:
        raise EvidenceContractError(
            "causal_event_sequence requires exactly one transaction state_anchor"
        )
    return normalized


def normalize_state_kernel_evaluator(evaluator: Any) -> dict[str, Any] | None:
    """Normalise the strict evidence contract of one evaluator row.

    Returns ``None`` when the row declares no ``state_kernel_evidence_contract``; raises
    :class:`EvidenceContractError` when it declares an invalid one. Dependencies, weights,
    zero-weight ids, and terminal checkpoints are validated against the checkpoint domain.
    """
    if not isinstance(evaluator, dict):
        raise EvidenceContractError("state-kernel evaluator row must be an object")
    raw_contract = evaluator.get("state_kernel_evidence_contract")
    if raw_contract is None:
        return None
    if not isinstance(raw_contract, dict):
        raise EvidenceContractError("state_kernel_evidence_contract must be an object")
    if raw_contract.get("contract_schema") != CONTRACT_SCHEMA:
        raise EvidenceContractError("unsupported state-kernel evidence contract schema")
    if raw_contract.get("evidence_schema_version") != 1:
        raise EvidenceContractError("unsupported state-kernel evidence schema version")
    if raw_contract.get("expression_schema") != EXPRESSION_SCHEMA:
        raise EvidenceContractError("unsupported checkpoint expression schema")
    if raw_contract.get("evidence_root") != "canonical_state_snapshot":
        raise EvidenceContractError("state-kernel evidence root must be canonical snapshot state")
    if set(raw_contract) != {
        "contract_schema",
        "evidence_schema_version",
        "expression_schema",
        "evidence_root",
        "checkpoints",
        "forbidden_conditions",
    }:
        raise EvidenceContractError("state-kernel evidence contract has unknown fields")

    raw_checkpoints = raw_contract.get("checkpoints")
    if not isinstance(raw_checkpoints, dict) or not raw_checkpoints:
        raise EvidenceContractError("strict contract requires checkpoint expressions")
    checkpoints: dict[str, dict[str, Any]] = {}
    for checkpoint_id, raw_spec in raw_checkpoints.items():
        if (
            not isinstance(checkpoint_id, str)
            or not checkpoint_id
            or not isinstance(raw_spec, dict)
            or not set(raw_spec).issubset({"assertions", "causal_event_sequence"})
            or "assertions" not in raw_spec
            or not isinstance(raw_spec.get("assertions"), list)
            or not raw_spec["assertions"]
        ):
            raise EvidenceContractError("checkpoint expression row is invalid")
        assertions = []
        for raw_assertion in raw_spec["assertions"]:
            if not isinstance(raw_assertion, dict) or set(raw_assertion) != {"expression"}:
                raise EvidenceContractError("checkpoint assertion row is invalid")
            expression, pointer_count = normalize_expression(raw_assertion["expression"])
            if pointer_count < 1:
                raise EvidenceContractError("checkpoint expression requires raw pointer evidence")
            assertions.append({"expression": expression})
        normalized_spec: dict[str, Any] = {"assertions": assertions}
        if "causal_event_sequence" in raw_spec:
            normalized_spec["causal_event_sequence"] = _normalize_causal_event_sequence(
                raw_spec["causal_event_sequence"]
            )
        checkpoints[checkpoint_id] = normalized_spec

    raw_forbidden = raw_contract.get("forbidden_conditions")
    if not isinstance(raw_forbidden, list):
        raise EvidenceContractError("forbidden_conditions must be a list")
    forbidden_conditions = []
    forbidden_ids: set[str] = set()
    for raw_condition in raw_forbidden:
        if (
            not isinstance(raw_condition, dict)
            or not set(raw_condition).issubset(
                {
                    "id",
                    "assertions",
                    "causal_event_sequence",
                }
            )
            or not {"id", "assertions"}.issubset(raw_condition)
            or not isinstance(raw_condition.get("assertions"), list)
            or not raw_condition["assertions"]
        ):
            raise EvidenceContractError("forbidden condition row is invalid")
        condition_id = raw_condition.get("id")
        if not isinstance(condition_id, str) or not condition_id or condition_id in forbidden_ids:
            raise EvidenceContractError("forbidden condition id is missing or duplicate")
        assertions = []
        for raw_assertion in raw_condition["assertions"]:
            if not isinstance(raw_assertion, dict) or set(raw_assertion) != {"expression"}:
                raise EvidenceContractError("forbidden assertion row is invalid")
            expression, pointer_count = normalize_expression(raw_assertion["expression"])
            if pointer_count < 1:
                raise EvidenceContractError("forbidden expression requires raw pointer evidence")
            assertions.append({"expression": expression})
        forbidden_ids.add(condition_id)
        normalized_condition: dict[str, Any] = {
            "id": condition_id,
            "assertions": assertions,
        }
        if "causal_event_sequence" in raw_condition:
            normalized_condition["causal_event_sequence"] = _normalize_causal_event_sequence(
                raw_condition["causal_event_sequence"]
            )
        forbidden_conditions.append(normalized_condition)

    domain = set(checkpoints)
    required = evaluator.get("required_checkpoints")
    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(value, str) or not value for value in required)
        or len(set(required)) != len(required)
        or not set(required).issubset(domain)
    ):
        raise EvidenceContractError("required_checkpoints is not covered by strict expressions")
    dependencies = evaluator.get("checkpoint_dependencies", {})
    if not isinstance(dependencies, dict) or any(
        checkpoint_id not in domain
        or not isinstance(values, list)
        or any(not isinstance(value, str) or value not in domain for value in values)
        or len(set(values)) != len(values)
        for checkpoint_id, values in dependencies.items()
    ):
        raise EvidenceContractError("checkpoint_dependencies is invalid")
    normalized_dependencies = {
        checkpoint_id: list(dependencies.get(checkpoint_id, [])) for checkpoint_id in checkpoints
    }
    remaining = set(domain)
    resolved: set[str] = set()
    while remaining:
        available = {
            checkpoint_id
            for checkpoint_id in remaining
            if set(normalized_dependencies[checkpoint_id]).issubset(resolved)
        }
        if not available:
            raise EvidenceContractError("checkpoint dependency graph contains a cycle")
        resolved.update(available)
        remaining.difference_update(available)

    weights = evaluator.get("checkpoint_weights", {})
    if not isinstance(weights, dict) or any(
        checkpoint_id not in domain
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
        for checkpoint_id, value in weights.items()
    ):
        raise EvidenceContractError("checkpoint_weights is invalid")
    fallback_weight = 1.0 / len(required)
    normalized_weights = {
        checkpoint_id: float(weights.get(checkpoint_id, fallback_weight))
        for checkpoint_id in checkpoints
    }
    zero_weight = evaluator.get("zero_weight_assertion_ids", [])
    if (
        not isinstance(zero_weight, list)
        or any(not isinstance(value, str) or value not in domain for value in zero_weight)
        or len(set(zero_weight)) != len(zero_weight)
    ):
        raise EvidenceContractError("zero_weight_assertion_ids is invalid")
    for checkpoint_id in zero_weight:
        normalized_weights[checkpoint_id] = 0.0
    terminal = evaluator.get("terminal_checkpoints")
    if (
        not isinstance(terminal, list)
        or not terminal
        or any(not isinstance(value, str) or value not in domain for value in terminal)
        or len(set(terminal)) != len(terminal)
    ):
        raise EvidenceContractError("terminal_checkpoints is invalid")
    return {
        "contract_schema": CONTRACT_SCHEMA,
        "evidence_schema_version": 1,
        "expression_schema": EXPRESSION_SCHEMA,
        "evidence_root": "canonical_state_snapshot",
        "checkpoints": checkpoints,
        "forbidden_conditions": forbidden_conditions,
        "required_checkpoints": list(required),
        "checkpoint_dependencies": normalized_dependencies,
        "checkpoint_weights": normalized_weights,
        "zero_weight_assertion_ids": list(zero_weight),
        "terminal_checkpoints": list(terminal),
        "validate_runtime_events": any(
            "causal_event_sequence" in spec for spec in checkpoints.values()
        )
        or any("causal_event_sequence" in condition for condition in forbidden_conditions),
    }


def _event_matches_binding(
    event: dict[str, Any],
    binding: dict[str, Any],
    state: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Check one state-service event against one causal binding; returns ``(matched, reasons)``."""
    reasons: list[str] = []
    if binding["event_kind"] == "transaction":
        event_type = str(event.get("event_type") or "transaction_committed")
        if event_type != binding["event_type"]:
            reasons.append("event_type_mismatch")
        if event.get("transaction_id") != binding["transaction_id"]:
            reasons.append("transaction_id_mismatch")
        diffs = event.get("state_diff")
        if not isinstance(diffs, list) or not diffs:
            reasons.append("empty_transaction_state_diff")
        else:
            for affected in binding["affected_state"]:
                candidates = [
                    row
                    for row in diffs
                    if isinstance(row, dict)
                    and row.get("path") == affected["diff_path"]
                    and is_valid_diff_row(row)
                ]
                if not candidates:
                    reasons.append("affected_state_diff_missing")
                    continue
                actual = resolve_pointer(state, affected["state_pointer"])
                if actual is MISSING:
                    reasons.append("affected_state_pointer_missing")
                elif not any(canonical_equal(row.get("after"), actual) for row in candidates):
                    reasons.append("affected_state_after_mismatch")
    else:
        if event.get("event_type") != binding["event_type"]:
            reasons.append("event_type_mismatch")
        if event.get("action_id") != binding["action_id"]:
            reasons.append("action_id_mismatch")
    return not reasons, list(dict.fromkeys(reasons))


def _match_causal_event_sequence(
    sequence: list[dict[str, Any]],
    events: list[dict[str, Any]],
    post_state_by_event_index: dict[int, dict[str, Any]],
) -> tuple[bool, dict[str, Any], dict[str, Any] | None]:
    """Match bindings as an ordered subsequence of ``events``.

    Returns ``(matched, ledger, anchor_post_state)``; the anchor binding must match exactly one
    event so its replayed post-state is unambiguous.
    """
    cursor = 0
    rows = []
    anchor_state: dict[str, Any] | None = None
    anchor_ledger: dict[str, Any] = {}
    for binding in sequence:
        matched_indices: list[int] = []
        near_miss_reasons: list[str] = []
        for index in range(cursor, len(events)):
            candidate_state = post_state_by_event_index.get(index)
            if binding["event_kind"] == "transaction" and candidate_state is None:
                near_miss_reasons.append("transaction_post_state_missing")
                continue
            matched, reasons = _event_matches_binding(
                events[index], binding, candidate_state or {}
            )
            if matched:
                matched_indices.append(index)
                if not binding["state_anchor"]:
                    break
            near_miss_reasons.extend(reasons)
        matched_index = matched_indices[0] if len(matched_indices) == 1 else None
        if binding["state_anchor"] and len(matched_indices) > 1:
            near_miss_reasons.append("ambiguous_state_anchor_event")
        if matched_index is not None:
            cursor = matched_index + 1
            if binding["state_anchor"]:
                anchor_state = json.loads(
                    canonical_json(post_state_by_event_index[matched_index])
                )
                anchor_ledger = {
                    "anchor_binding_id": binding["binding_id"],
                    "anchor_event_index": matched_index,
                    "anchor_revision": events[matched_index].get("revision"),
                    "anchor_state_hash": canonical_hash(anchor_state),
                }
        row = {
            "binding_id": binding["binding_id"],
            "event_kind": binding["event_kind"],
            "state_anchor": binding["state_anchor"],
            "matched": matched_index is not None,
            "matched_event_index": matched_index,
            "matched_revision": (
                events[matched_index].get("revision")
                if matched_index is not None
                else None
            ),
            "reasons": (
                []
                if matched_index is not None
                else list(dict.fromkeys(near_miss_reasons)) or ["causal_event_missing"]
            ),
        }
        rows.append(row)
        if matched_index is None:
            return False, {
                "matched": False,
                "ordered": True,
                "bindings": rows,
                **anchor_ledger,
            }, None
    if anchor_state is None:
        return False, {
            "matched": False,
            "ordered": True,
            "bindings": rows,
            "reasons": ["state_anchor_post_state_missing"],
        }, None
    return True, {
        "matched": True,
        "ordered": True,
        "bindings": rows,
        **anchor_ledger,
    }, anchor_state


def _evaluate_assertions(
    assertions: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate every assertion expression; all must be exactly ``True``."""
    matched = True
    rows: list[dict[str, Any]] = []
    for assertion in assertions:
        pointers: list[dict[str, Any]] = []
        value, error = evaluate_expression(assertion["expression"], state, pointers)
        assertion_matched = error is None and value is True
        matched = matched and assertion_matched
        rows.append(
            {
                "valid": error is None,
                "matched": assertion_matched,
                "reason": error,
                "expression": assertion["expression"],
                **({"value": value} if value is not MISSING else {}),
                "pointer_resolutions": pointers,
            }
        )
    return matched, rows


def _rejected_evidence(
    reason: str,
    ledger: dict[str, Any],
    evaluation_handle: Any,
) -> dict[str, Any]:
    """Zero-reward payload for a contract or carrier that cannot be trusted."""
    return {
        "score": 0.0,
        "raw_reward": 0.0,
        "progress_reward": 0.0,
        "terminal_success": False,
        "reason": reason,
        "env_valid": False,
        "format_valid": True,
        "checkpoint_outcomes": {},
        "checkpoint_reward_vector": [],
        "forbidden_condition_state": {},
        "producer_projection_credit_used": False,
        "portfolio_release_ready": False,
        "evidence_contract_mode": _STRICT_MODE,
        "state_kernel_evidence_ledger": ledger,
        "evaluation_handle": evaluation_handle,
    }


def _evaluate_spec(
    spec: dict[str, Any],
    state: dict[str, Any],
    events: list[dict[str, Any]],
    post_state_by_event_index: dict[int, dict[str, Any]],
) -> tuple[bool, bool, list[dict[str, Any]], str, dict[str, Any], dict[str, Any] | None]:
    """Evaluate one checkpoint or forbidden-condition spec.

    Returns ``(state_matched, causal_matched, assertion_rows, state_source, causal_ledger,
    anchor_state)``. Without a causal sequence the assertions run on the final snapshot; with
    one they run on the anchor transaction's replayed post-state.
    """
    causal_matched = True
    anchor_state: dict[str, Any] | None = None
    causal_ledger: dict[str, Any] = {
        "required": False,
        "matched": True,
        "ordered": True,
        "bindings": [],
    }
    if "causal_event_sequence" in spec:
        causal_matched, causal_ledger, anchor_state = _match_causal_event_sequence(
            spec["causal_event_sequence"], events, post_state_by_event_index
        )
        causal_ledger["required"] = True
    if "causal_event_sequence" not in spec:
        matched, assertion_rows = _evaluate_assertions(spec["assertions"], state)
        source = "final_snapshot"
    elif anchor_state is not None:
        matched, assertion_rows = _evaluate_assertions(spec["assertions"], anchor_state)
        source = "transaction_anchor_post_state"
    else:
        matched = False
        assertion_rows = []
        source = "unavailable_causal_anchor_post_state"
    return matched, causal_matched, assertion_rows, source, causal_ledger, anchor_state


def _anchor_fields(
    matched: bool,
    anchor_state: dict[str, Any] | None,
    source: str,
    causal_ledger: dict[str, Any],
) -> dict[str, Any]:
    """Ledger fields shared by checkpoint and forbidden-condition rows."""
    return {
        "state_matched": matched,
        "anchor_state_matched": matched if anchor_state is not None else None,
        "state_evaluation_source": source,
        "anchor_binding_id": causal_ledger.get("anchor_binding_id"),
        "anchor_event_index": causal_ledger.get("anchor_event_index"),
        "anchor_revision": causal_ledger.get("anchor_revision"),
        "anchor_state_hash": causal_ledger.get("anchor_state_hash"),
    }


def reduce_state_kernel_evidence(
    evaluator: dict[str, Any],
    *,
    task_id: str,
    sid: str,
    binding: Any,
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    expected_snapshot_kind: str,
) -> dict[str, Any]:
    """Score one episode snapshot under the evaluator's strict evidence contract.

    ``binding`` is the host's record of the episode (task, sid, epoch, sealed initial state);
    ``snapshot`` and ``events`` come from the state service. Reward is binary: 1.0 when every
    required and terminal checkpoint holds and no forbidden condition matched.
    """
    try:
        contract = normalize_state_kernel_evaluator(evaluator)
    except EvidenceContractError as error:
        return _rejected_evidence(
            "invalid_state_kernel_evidence_contract",
            {
                "contract_valid": False,
                "identity_valid": False,
                "snapshot_valid": False,
                "events_valid": False,
                "reasons": [str(error)],
            },
            snapshot.get("evaluation_handle"),
        )
    if contract is None:
        raise EvidenceContractError("strict reducer called without strict contract")
    carrier_ledger, reasons, post_state_by_event_index = validate_carrier(
        task_id=task_id,
        sid=sid,
        binding=binding,
        snapshot=snapshot,
        events=events,
        expected_snapshot_kind=expected_snapshot_kind,
        validate_runtime_events=contract["validate_runtime_events"],
    )
    if reasons:
        return _rejected_evidence(
            "invalid_state_kernel_evidence",
            carrier_ledger,
            carrier_ledger.get("effective_evaluation_handle"),
        )

    state = snapshot["state"]
    expression_ledger: dict[str, Any] = {}
    raw_outcomes: dict[str, bool] = {}
    terminal_checkpoint_ids = set(contract["terminal_checkpoints"])
    for checkpoint_id, spec in contract["checkpoints"].items():
        matched, causal_matched, assertion_rows, source, causal_ledger, anchor_state = (
            _evaluate_spec(spec, state, events, post_state_by_event_index)
        )
        # A causally anchored terminal checkpoint must also still hold in the final state.
        final_state_gate_required = bool(
            "causal_event_sequence" in spec and checkpoint_id in terminal_checkpoint_ids
        )
        if final_state_gate_required:
            final_state_matched, final_state_assertion_rows = _evaluate_assertions(
                spec["assertions"], state
            )
        else:
            final_state_matched = True
            final_state_assertion_rows = []
        raw_outcomes[checkpoint_id] = bool(matched and causal_matched and final_state_matched)
        expression_ledger[checkpoint_id] = {
            "valid": (
                all(row["valid"] for row in assertion_rows)
                and all(row["valid"] for row in final_state_assertion_rows)
            ),
            **_anchor_fields(matched, anchor_state, source, causal_ledger),
            "final_state_gate_required": final_state_gate_required,
            "final_state_matched": final_state_matched,
            "final_state_assertions": final_state_assertion_rows,
            "causal_event_matched": causal_matched,
            "matched": raw_outcomes[checkpoint_id],
            "assertions": assertion_rows,
            "causal_event_sequence": causal_ledger,
        }
    outcomes: dict[str, bool] = {}
    remaining = set(contract["checkpoints"])
    while remaining:
        for checkpoint_id in contract["checkpoints"]:
            if checkpoint_id not in remaining:
                continue
            dependencies = contract["checkpoint_dependencies"][checkpoint_id]
            if any(dependency not in outcomes for dependency in dependencies):
                continue
            outcomes[checkpoint_id] = bool(
                raw_outcomes[checkpoint_id]
                and all(outcomes[dependency] for dependency in dependencies)
            )
            remaining.remove(checkpoint_id)

    forbidden_state: dict[str, bool] = {}
    forbidden_ledger: dict[str, Any] = {}
    for condition in contract["forbidden_conditions"]:
        matched, causal_matched, assertion_rows, source, causal_ledger, anchor_state = (
            _evaluate_spec(condition, state, events, post_state_by_event_index)
        )
        forbidden_state[condition["id"]] = matched and causal_matched
        forbidden_ledger[condition["id"]] = {
            "valid": all(row["valid"] for row in assertion_rows),
            **_anchor_fields(matched, anchor_state, source, causal_ledger),
            "causal_event_matched": causal_matched,
            "matched": forbidden_state[condition["id"]],
            "assertions": assertion_rows,
            "causal_event_sequence": causal_ledger,
        }
    invalid_expression_evidence = [
        f"checkpoint:{checkpoint_id}"
        for checkpoint_id, row in expression_ledger.items()
        if not row["valid"]
    ] + [
        f"forbidden:{condition_id}"
        for condition_id, row in forbidden_ledger.items()
        if not row["valid"]
    ]
    if invalid_expression_evidence:
        carrier_ledger.update(
            {
                "checkpoint_expressions": expression_ledger,
                "forbidden_expressions": forbidden_ledger,
                "contract_schema": contract["contract_schema"],
                "expression_schema": contract["expression_schema"],
                "invalid_expression_evidence": invalid_expression_evidence,
            }
        )
        carrier_ledger["reasons"].append("invalid_expression_evidence")
        return _rejected_evidence(
            "invalid_state_kernel_expression_evidence",
            carrier_ledger,
            carrier_ledger.get("effective_evaluation_handle"),
        )
    vector = []
    progress_reward = 0.0
    zero_weight = set(contract["zero_weight_assertion_ids"])
    for checkpoint_id in contract["checkpoints"]:
        weight = contract["checkpoint_weights"][checkpoint_id]
        contribution = weight if outcomes[checkpoint_id] else 0.0
        progress_reward += contribution
        vector.append(
            {
                "checkpoint_id": checkpoint_id,
                "reached": outcomes[checkpoint_id],
                "depends_on": contract["checkpoint_dependencies"][checkpoint_id],
                "dependencies_satisfied": all(
                    outcomes[dependency]
                    for dependency in contract["checkpoint_dependencies"][checkpoint_id]
                ),
                "weight": weight,
                "zero_weight": checkpoint_id in zero_weight,
                "reward_contribution": contribution,
            }
        )
    missing_required = [
        checkpoint_id
        for checkpoint_id in contract["required_checkpoints"]
        if not outcomes[checkpoint_id]
    ]
    missing_terminal = [
        checkpoint_id
        for checkpoint_id in contract["terminal_checkpoints"]
        if not outcomes[checkpoint_id]
    ]
    forbidden_hit = any(forbidden_state.values())
    terminal_success = not missing_terminal and not missing_required and not forbidden_hit
    carrier_ledger.update(
        {
            "checkpoint_expressions": expression_ledger,
            "forbidden_expressions": forbidden_ledger,
            "contract_schema": contract["contract_schema"],
            "expression_schema": contract["expression_schema"],
        }
    )
    return {
        "score": 1.0 if terminal_success else 0.0,
        "raw_reward": 1.0 if terminal_success else 0.0,
        "progress_reward": round(progress_reward, 12),
        "terminal_success": terminal_success,
        "reason": (
            "strict_state_kernel_terminal_satisfied"
            if terminal_success
            else "forbidden_condition_observed"
            if forbidden_hit
            else "strict_state_kernel_checkpoint_mismatch"
        ),
        "env_valid": True,
        "format_valid": True,
        "checkpoint_outcomes": outcomes,
        "checkpoint_reward_vector": vector,
        "required_checkpoints": contract["required_checkpoints"],
        "missing_required_checkpoints": missing_required,
        "terminal_checkpoints": contract["terminal_checkpoints"],
        "missing_terminal_checkpoints": missing_terminal,
        "forbidden_condition_state": forbidden_state,
        "producer_projection_credit_used": False,
        "portfolio_release_ready": True,
        "evidence_contract_mode": _STRICT_MODE,
        "state_kernel_evidence_ledger": carrier_ledger,
        "evaluation_handle": carrier_ledger.get("effective_evaluation_handle"),
    }
