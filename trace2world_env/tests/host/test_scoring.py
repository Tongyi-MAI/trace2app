"""Bounded expressions, strict state-kernel evidence, and canonical-outcome evaluation."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from trace2world_env.scoring.errors import EvidenceContractError
from trace2world_env.scoring.evidence import (
    normalize_state_kernel_evaluator,
    reduce_state_kernel_evidence,
)
from trace2world_env.scoring.expressions import (
    MISSING,
    canonical_hash,
    evaluate_expression,
    normalize_expression,
)
from trace2world_env.scoring.outcomes import evaluate_canonical_outcome, reduce_declared_checkpoints
from trace2world_env.scoring.plans import validate_state_kernel_evaluator_plan


def ptr(path: str) -> dict[str, Any]:
    return {"op": "pointer", "path": path}


def lit(value: Any) -> dict[str, Any]:
    return {"op": "literal", "value": value}


def evaluate(expression: dict[str, Any], state: dict[str, Any]) -> tuple[Any, str | None]:
    normalized, _ = normalize_expression(expression)
    return evaluate_expression(normalized, state, [])


def test_expression_normalisation_limits() -> None:
    normalized, pointers = normalize_expression({"op": "eq", "args": [ptr("/a/b"), lit(1)]})
    assert pointers == 1 and normalized["args"][0] == {"op": "pointer", "path": "/a/b"}
    with pytest.raises(EvidenceContractError, match="identity or self-certifying"):
        normalize_expression(ptr("/checkpoints/x"))
    with pytest.raises(EvidenceContractError, match="unsafe"):
        normalize_expression(
            {"op": "matches_regex", "args": [ptr("/a")], "pattern": "(a+)+", "flags": ""}
        )
    with pytest.raises(EvidenceContractError, match="arity"):
        normalize_expression({"op": "not", "args": [lit(True), lit(False)]})
    deep: dict[str, Any] = ptr("/a")
    for _ in range(8):
        deep = {"op": "not", "args": [deep]}
    with pytest.raises(EvidenceContractError, match="depth/node"):
        normalize_expression(deep)


def test_expression_evaluation() -> None:
    state = {
        "items": [{"id": "a", "price": 3}, {"id": "b", "price": 1}],
        "name": " Ｃafé  Latte ",
        "n": 2.345,
    }
    assert evaluate({"op": "eq", "args": [ptr("/items/1/id"), lit("b")]}, state) == (True, None)
    assert evaluate({"op": "eq", "args": [lit(True), lit(1)]}, state) == (False, None)
    assert evaluate({"op": "length", "args": [ptr("/items")]}, state) == (2, None)
    assert evaluate({"op": "normalize_string", "args": [ptr("/name")]}, state) == (
        "café latte",
        None,
    )
    assert evaluate(
        {
            "op": "argmin",
            "args": [ptr("/items")],
            "value_pointer": "/price",
            "select_pointer": "/id",
        },
        state,
    ) == (["b"], None)
    assert evaluate({"op": "round", "args": [ptr("/n"), lit(2)]}, state) == (2.35, None)
    mismatch = evaluate({"op": "gt", "args": [ptr("/name"), lit(1)]}, state)
    assert mismatch == (MISSING, "type_mismatch")
    ledger: list[dict[str, Any]] = []
    normalized, _ = normalize_expression(ptr("/absent"))
    assert evaluate_expression(normalized, state, ledger) == (MISSING, "pointer_missing")
    assert ledger == [{"path": "/absent", "resolved": False}]


def _contract_evaluator(**overrides: Any) -> dict[str, Any]:
    evaluator = {
        "state_kernel_evidence_contract": {
            "contract_schema": "trace2world.state-kernel-evidence-contract.v1",
            "evidence_schema_version": 1,
            "expression_schema": "trace2world.bounded-state-expression.v1",
            "evidence_root": "canonical_state_snapshot",
            "checkpoints": {
                "cart_has_item": {
                    "assertions": [
                        {"expression": {"op": "eq", "args": [ptr("/cart/0/sku"), lit("A1")]}}
                    ]
                },
                "ordered": {
                    "assertions": [
                        {
                            "expression": {
                                "op": "gte",
                                "args": [ptr("/runtime_fields/orders"), lit(1)],
                            }
                        }
                    ]
                },
            },
            "forbidden_conditions": [
                {
                    "id": "negative_total",
                    "assertions": [
                        {"expression": {"op": "lt", "args": [ptr("/runtime_fields/total"), lit(0)]}}
                    ],
                }
            ],
        },
        "required_checkpoints": ["cart_has_item", "ordered"],
        "checkpoint_dependencies": {"ordered": ["cart_has_item"]},
        "terminal_checkpoints": ["ordered"],
    }
    evaluator.update(overrides)
    return evaluator


def _carrier(state: dict[str, Any], *, revision: int = 0, initial: dict[str, Any] | None = None):
    sid = "sid_1_x"
    initial = initial if initial is not None else state
    binding = {
        "task_id": "task",
        "sid": sid,
        "episode_epoch": 1,
        "initial_snapshot_id": "initial",
        "initial_revision": 0,
        "initial_state_hash": canonical_hash(initial),
        "initial_state": initial,
    }
    state_hash = canonical_hash(state)
    snapshot = {
        "state": state,
        "state_version": {"sid": sid, "episode_epoch": 1, "revision": revision},
        "state_hash": state_hash,
        "snapshot_id": "final-1",
        "snapshot_kind": "final",
        "evaluation_handle": {
            "sid": sid,
            "episode_epoch": 1,
            "revision": revision,
            "snapshot_id": "final-1",
            "state_hash": state_hash,
        },
    }
    return sid, binding, snapshot


def _reduce(evaluator: dict[str, Any], state: dict[str, Any], events=None, **carrier: Any):
    sid, binding, snapshot = _carrier(state, **carrier)
    return reduce_state_kernel_evidence(
        evaluator,
        task_id="task",
        sid=sid,
        binding=binding,
        snapshot=snapshot,
        events=events or [],
        expected_snapshot_kind="final",
    )


def test_strict_evidence_success_and_dependency_gating() -> None:
    state = {"cart": [{"sku": "A1"}], "runtime_fields": {"orders": 1, "total": 5}}
    result = _reduce(_contract_evaluator(), state)
    assert result["raw_reward"] == 1.0 and result["terminal_success"] is True
    assert result["reason"] == "strict_state_kernel_terminal_satisfied"
    assert result["progress_reward"] == 1.0
    assert [row["reward_contribution"] for row in result["checkpoint_reward_vector"]] == [0.5, 0.5]

    gated = _reduce(_contract_evaluator(), {**state, "cart": [{"sku": "B2"}]})
    assert gated["checkpoint_outcomes"] == {"cart_has_item": False, "ordered": False}
    assert gated["reason"] == "strict_state_kernel_checkpoint_mismatch"

    negative = {**state, "runtime_fields": {"orders": 1, "total": -1}}
    forbidden = _reduce(_contract_evaluator(), negative)
    assert forbidden["reason"] == "forbidden_condition_observed"
    assert forbidden["raw_reward"] == 0.0


def test_strict_evidence_rejects_bad_carrier_and_contract() -> None:
    state = {"cart": [{"sku": "A1"}], "runtime_fields": {"orders": 1, "total": 5}}
    sid, binding, snapshot = _carrier(state)
    snapshot["state_hash"] = "0" * 64
    rejected = reduce_state_kernel_evidence(
        _contract_evaluator(),
        task_id="task",
        sid=sid,
        binding=binding,
        snapshot=snapshot,
        events=[],
        expected_snapshot_kind="final",
    )
    assert rejected["reason"] == "invalid_state_kernel_evidence"
    assert rejected["state_kernel_evidence_ledger"]["reasons"] == ["snapshot_state_hash_mismatch"]

    broken = _contract_evaluator(terminal_checkpoints=["unknown"])
    assert _reduce(broken, state)["reason"] == "invalid_state_kernel_evidence_contract"
    with pytest.raises(EvidenceContractError):
        normalize_state_kernel_evaluator(broken)
    assert normalize_state_kernel_evaluator({"canonical_assertions": {}}) is None


def test_causal_sequence_replays_typed_diffs() -> None:
    evaluator = _contract_evaluator()
    contract = evaluator["state_kernel_evidence_contract"]
    contract["checkpoints"]["ordered"]["causal_event_sequence"] = [
        {
            "binding_id": "commit",
            "event_kind": "transaction",
            "event_type": "transaction_committed",
            "transaction_id": "tx.order",
            "affected_state": [
                {"diff_path": "$.runtime_fields.orders", "state_pointer": "/runtime_fields/orders"}
            ],
            "state_anchor": True,
        }
    ]
    initial = {"cart": [{"sku": "A1"}], "runtime_fields": {"orders": 0, "total": 5}}
    final = copy.deepcopy(initial)
    final["runtime_fields"]["orders"] = 1
    events = [
        {
            "revision": 1,
            "transaction_id": "tx.order",
            "state_diff": [
                {
                    "operation": "field_replaced",
                    "path": "$.runtime_fields.orders",
                    "before": 0,
                    "after": 1,
                }
            ],
        }
    ]
    result = _reduce(evaluator, final, events, revision=1, initial=initial)
    assert result["terminal_success"] is True
    ledger = result["state_kernel_evidence_ledger"]
    assert ledger["events_valid"] is True
    assert ledger["checkpoint_expressions"]["ordered"]["state_evaluation_source"] == (
        "transaction_anchor_post_state"
    )

    tampered = copy.deepcopy(events)
    tampered[0]["state_diff"][0]["after"] = 2
    bad = _reduce(evaluator, final, tampered, revision=1, initial=initial)
    assert bad["reason"] == "invalid_state_kernel_evidence"
    assert "event_replay_final_state_mismatch" in bad["state_kernel_evidence_ledger"]["reasons"]


def _outcome(evaluator: dict[str, Any], state: dict[str, Any], events: list[Any]) -> dict[str, Any]:
    calls: list[int] = []

    async def read_events() -> list[Any]:
        calls.append(1)
        return events

    result = asyncio.run(
        evaluate_canonical_outcome(
            evaluator,
            task_id="task",
            snapshot={"state": state, "evaluation_handle": {"sid": "s"}},
            read_events=read_events,
        )
    )
    result["_event_reads"] = len(calls)
    return result


def test_canonical_outcome_state_and_transactions() -> None:
    evaluator = {
        "canonical_outcome_ready": True,
        "reward_authority": {"outcome_kind": "canonical_state"},
        "canonical_assertions": {"runtime_fields.orders": {"op": "eq", "value": 1}},
        "outcome_transaction_ids": ["tx.order"],
    }
    state = {"runtime_fields": {"orders": 1}}
    passed = _outcome(evaluator, state, [{"transaction_id": "tx.order"}, "noise"])
    assert passed["score"] == 1.0
    assert passed["reason"] == "canonical_outcome_state_and_transaction_observed"
    failed = _outcome(evaluator, state, [])
    assert failed["missing_transaction_ids"] == ["tx.order"]
    assert failed["reason"] == "canonical_outcome_state_or_transaction_mismatch"
    not_ready = _outcome({**evaluator, "canonical_outcome_ready": False}, state, [])
    assert not_ready == {
        "score": 0.0,
        "reason": "missing_canonical_outcome_authority",
        "evaluation_handle": {"sid": "s"},
        "_event_reads": 0,
    }


def test_declared_checkpoint_dag() -> None:
    evaluator = {
        "checkpoint_assertions": [
            {
                "checkpoint_id": "viewed",
                "weight": 0.25,
                "progress_only": True,
                "state_assertions": {"runtime_fields.viewed": {"op": "truthy"}},
            },
            {
                "checkpoint_id": "bought",
                "weight": 0.75,
                "depends_on": ["viewed"],
                "event_assertions": [{"match": {"transaction_id": {"op": "eq", "value": "tx"}}}],
            },
        ],
        "terminal_required_checkpoint_ids": ["bought"],
    }
    reduction = reduce_declared_checkpoints(
        evaluator, {"runtime_fields": {"viewed": True}}, [{"transaction_id": "tx"}]
    )
    assert reduction is not None
    assert reduction["checkpoint_outcomes"] == {"viewed": True, "bought": True}
    assert reduction["progress_reward"] == 0.25
    assert reduction["terminal_checkpoints_satisfied"] is True
    assert reduce_declared_checkpoints({}, {}, []) is None


def test_state_kernel_plan_coverage(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"evaluators": {"task": _contract_evaluator(), "other": {}}}))
    coverage = validate_state_kernel_evaluator_plan(
        plan, [{"task_id": "task"}, {"task_id": "other"}]
    )
    assert coverage["strict_task_ids"] == ["task"]
    assert coverage["strict_evaluation_gaps"] == ["other"]
    assert coverage["portfolio_release_ready"] is False
    with pytest.raises(ValueError, match="missing plan entries=absent"):
        validate_state_kernel_evaluator_plan(plan, [{"task_id": "absent"}])
