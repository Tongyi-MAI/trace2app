"""Canonical-outcome evaluation for state-kernel evaluators without a strict evidence contract.

The evaluator row declares canonical assertions over the final snapshot state (dotted paths,
``runtime_fields`` first), the outcome transactions and action receipts that must appear in the
event stream, and optionally a checkpoint DAG (``checkpoint_assertions``) whose weights give
progress credit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trace2world_env.scoring.errors import StateKernelError
from trace2world_env.scoring.expressions import MISSING


def canonical_value(state: dict[str, Any], path: str) -> Any:
    """Read a dotted path from the snapshot state; ``runtime_fields`` keys (longest prefix)
    win over nested state. Returns :data:`MISSING` when absent.
    """
    runtime_fields = state.get("runtime_fields", {})
    if isinstance(runtime_fields, dict) and path in runtime_fields:
        return runtime_fields[path]
    if isinstance(runtime_fields, dict):
        matching_key = next(
            (
                key
                for key in sorted(runtime_fields, key=len, reverse=True)
                if path.startswith(f"{key}.")
            ),
            None,
        )
        if matching_key is not None:
            current: Any = runtime_fields[matching_key]
            suffix = path[len(matching_key) + 1 :]
            for part in suffix.split("."):
                if not isinstance(current, dict) or part not in current:
                    return MISSING
                current = current[part]
            return current
    current: Any = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def assertion_matches(actual: Any, assertion: dict[str, Any]) -> bool:
    """Apply one ``{op, value}`` assertion (eq, present, contains, selected_contains, truthy)."""
    operation = str(assertion.get("op") or "eq")
    expected = assertion.get("value")
    if operation == "present":
        return actual is not MISSING
    if actual is MISSING:
        return False
    if operation == "eq":
        return actual == expected
    if operation in {"contains", "selected_contains"}:
        if isinstance(actual, (list, tuple, set)):
            return expected in actual
        return str(expected) in str(actual)
    if operation == "truthy":
        return bool(actual)
    return False


def is_ordered_subsequence(expected: list[str], observed: list[str]) -> bool:
    """Whether ``expected`` occurs in ``observed`` in order (gaps allowed)."""
    if not expected:
        return True
    cursor = 0
    for transaction_id in observed:
        if transaction_id == expected[cursor]:
            cursor += 1
            if cursor == len(expected):
                return True
    return False


def _nested_value(value: Any, path: str) -> Any:

    """Read a dotted path from a nested dict; :data:`MISSING` when absent."""
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def _state_diff_assertions_match(
    event: dict[str, Any], declarations: Any,
) -> bool:

    """Whether each declared path has a state-diff row satisfying its field assertions."""
    if declarations is None:
        return True
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint state_diff_assertions must be a non-empty list")
    diffs = event.get("state_diff")
    if not isinstance(diffs, list):
        return False
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise StateKernelError("checkpoint state-diff assertion must be an object")
        path = declaration.get("path")
        if not isinstance(path, str) or not path:
            raise StateKernelError("checkpoint state-diff assertion requires path")
        candidates = [
            row for row in diffs
            if isinstance(row, dict) and str(row.get("path") or "") == path
        ]
        matched = False
        for row in candidates:
            if all(
                isinstance(assertion, dict)
                and assertion_matches(row.get(field, MISSING), assertion)
                for field, assertion in declaration.items()
                if field != "path"
            ):
                matched = True
                break
        if not matched:
            return False
    return True


def _checkpoint_event_matches(event: dict[str, Any], declaration: Any) -> bool:

    """Whether one event satisfies a declaration's ``match`` fields and state-diff assertions."""
    if not isinstance(declaration, dict):
        raise StateKernelError("checkpoint event assertion must be an object")
    match = declaration.get("match", {})
    if not isinstance(match, dict):
        raise StateKernelError("checkpoint event assertion match must be an object")
    for path, assertion in match.items():
        if not isinstance(assertion, dict):
            raise StateKernelError("checkpoint event field assertion must be an object")
        if not assertion_matches(_nested_value(event, str(path)), assertion):
            return False
    return _state_diff_assertions_match(
        event, declaration.get("state_diff_assertions")
    )


def _checkpoint_event_assertions_match(
    events: list[dict[str, Any]], declarations: Any,
) -> bool:
    """Whether every event assertion is met by at least ``min_count`` events."""
    if declarations is None:
        return True
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint event_assertions must be a non-empty list")
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise StateKernelError("checkpoint event assertion must be an object")
        minimum = declaration.get("min_count", 1)
        if not isinstance(minimum, int) or minimum < 1:
            raise StateKernelError("checkpoint event assertion min_count must be positive")
        if sum(
            _checkpoint_event_matches(event, declaration) for event in events
        ) < minimum:
            return False
    return True


def _checkpoint_event_sequence_matches(
    events: list[dict[str, Any]], declarations: Any,
) -> bool:
    """Whether the declared events occur in order (gaps allowed)."""
    if declarations is None:
        return True
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint event_sequence must be a non-empty list")
    cursor = 0
    for event in events:
        if _checkpoint_event_matches(event, declarations[cursor]):
            cursor += 1
            if cursor == len(declarations):
                return True
    return False


def reduce_declared_checkpoints(
    evaluator: dict[str, Any],
    state: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:

    """Reduce the evaluator's ``checkpoint_assertions`` DAG against state and events.

    Returns ``None`` when no DAG is declared. A checkpoint is reached when its own predicates
    hold and all dependencies are reached; weights of ``progress_only`` checkpoints sum to
    ``progress_reward``.
    """
    declarations = evaluator.get("checkpoint_assertions")
    if declarations is None:
        return None
    if not isinstance(declarations, list) or not declarations:
        raise StateKernelError("checkpoint_assertions must be a non-empty list")

    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise StateKernelError("checkpoint declaration must be an object")
        checkpoint_id = declaration.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise StateKernelError("checkpoint declaration requires checkpoint_id")
        if checkpoint_id in rows:
            raise StateKernelError(f"duplicate checkpoint declaration: {checkpoint_id}")
        dependencies = declaration.get("depends_on", [])
        weight = declaration.get("weight")
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(value, str) or not value for value in dependencies)
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not 0 <= float(weight) <= 1
        ):
            raise StateKernelError(f"invalid checkpoint dependency/weight: {checkpoint_id}")
        assertions = declaration.get("state_assertions")
        has_state = isinstance(assertions, dict) and bool(assertions)
        has_events = any(
            declaration.get(field) is not None
            for field in ("event_assertions", "event_sequence")
        )
        if not has_state and not has_events:
            raise StateKernelError(f"checkpoint has no executable predicate: {checkpoint_id}")
        rows[checkpoint_id] = declaration
        order.append(checkpoint_id)

    unknown = sorted({
        dependency
        for declaration in rows.values()
        for dependency in declaration.get("depends_on", [])
        if dependency not in rows
    })
    if unknown:
        raise StateKernelError(f"checkpoint dependency is not declared: {unknown}")

    terminal_required = evaluator.get("terminal_required_checkpoint_ids", [])
    if not isinstance(terminal_required, list) or any(
        not isinstance(checkpoint_id, str) or not checkpoint_id
        for checkpoint_id in terminal_required
    ):
        raise StateKernelError(
            "terminal_required_checkpoint_ids must be a list of checkpoint ids"
        )
    if len(set(terminal_required)) != len(terminal_required):
        raise StateKernelError("terminal_required_checkpoint_ids contains duplicates")
    unknown_terminal = sorted(set(terminal_required) - set(rows))
    if unknown_terminal:
        raise StateKernelError(
            f"terminal required checkpoint is not declared: {unknown_terminal}"
        )

    outcomes: dict[str, bool] = {}
    remaining = set(order)
    while remaining:
        progressed = False
        for checkpoint_id in order:
            if checkpoint_id not in remaining:
                continue
            declaration = rows[checkpoint_id]
            dependencies = declaration.get("depends_on", [])
            if any(dependency not in outcomes for dependency in dependencies):
                continue
            assertions = declaration.get("state_assertions") or {}
            state_matches = all(
                isinstance(assertion, dict)
                and assertion_matches(canonical_value(state, str(path)), assertion)
                for path, assertion in assertions.items()
            )
            own_predicate = bool(
                state_matches
                and _checkpoint_event_assertions_match(
                    events, declaration.get("event_assertions")
                )
                and _checkpoint_event_sequence_matches(
                    events, declaration.get("event_sequence")
                )
            )
            outcomes[checkpoint_id] = bool(
                own_predicate and all(outcomes[dependency] for dependency in dependencies)
            )
            remaining.remove(checkpoint_id)
            progressed = True
        if not progressed:
            raise StateKernelError("checkpoint dependency graph contains a cycle")

    vector = []
    progress_reward = 0.0
    for checkpoint_id in order:
        declaration = rows[checkpoint_id]
        reached = outcomes[checkpoint_id]
        weight = float(declaration["weight"])
        contribution = weight if reached else 0.0
        progress_only = declaration.get("progress_only") is True
        if progress_only:
            progress_reward += contribution
        vector.append({
            "checkpoint_id": checkpoint_id,
            "reached": reached,
            "depends_on": list(declaration.get("depends_on", [])),
            "dependencies_satisfied": all(
                outcomes[dependency] for dependency in declaration.get("depends_on", [])
            ),
            "weight": weight,
            "progress_only": progress_only,
            "reward_contribution": contribution,
        })
    return {
        "checkpoint_outcomes": outcomes,
        "checkpoint_reward_vector": vector,
        "progress_reward": round(progress_reward, 12),
        "nonzero_checkpoint_reached": any(outcomes.values()),
        "terminal_required_checkpoint_ids": list(terminal_required),
        "missing_terminal_checkpoint_ids": [
            checkpoint_id
            for checkpoint_id in terminal_required
            if not outcomes[checkpoint_id]
        ],
        "terminal_checkpoints_satisfied": all(
            outcomes[checkpoint_id] for checkpoint_id in terminal_required
        ),
        "env_valid": True,
        "format_valid": True,
    }


async def evaluate_canonical_outcome(
    evaluator: dict[str, Any],
    *,
    task_id: str,
    snapshot: dict[str, Any],
    read_events: Callable[[], Awaitable[list[Any]]],
) -> dict[str, Any]:
    """Score an evaluator row that has no strict evidence contract.

    Success requires the declared canonical assertions on the final snapshot state and, unless
    ``terminal_authority_mode`` makes the assertions alone authoritative, the declared outcome
    transactions, action receipts, transaction order, and terminal-action receipt in the event
    stream. A declared checkpoint DAG adds progress credit and can veto terminal success.
    ``read_events`` is only awaited once the declaration itself is valid.
    """
    if not evaluator.get("canonical_outcome_ready"):
        return {
            "score": 0.0,
            "reason": "missing_canonical_outcome_authority",
            "evaluation_handle": snapshot.get("evaluation_handle"),
        }
    expected = {str(value) for value in evaluator.get("outcome_transaction_ids", [])}
    required_authorities = [
        row for row in evaluator.get("required_outcome_authorities", [])
        if isinstance(row, dict)
    ]
    assertions = evaluator.get("canonical_assertions", {})
    terminal_authority_mode = evaluator.get("terminal_authority_mode")
    explicit_terminal_authority = terminal_authority_mode is not None
    allowed_terminal_authority_modes = {
        "canonical_assertions",
        "canonical_assertions_plus_checkpoint_dag",
    }
    terminal_checkpoint_ids = evaluator.get(
        "terminal_required_checkpoint_ids", []
    )
    checkpoint_declarations = evaluator.get("checkpoint_assertions")
    authority_contract_error = None
    if explicit_terminal_authority:
        if terminal_authority_mode not in allowed_terminal_authority_modes:
            authority_contract_error = "unknown_terminal_authority_mode"
        elif not isinstance(assertions, dict) or not assertions:
            authority_contract_error = "canonical_assertions_missing"
        elif terminal_authority_mode == "canonical_assertions" and (
            terminal_checkpoint_ids or checkpoint_declarations is not None
        ):
            authority_contract_error = "canonical_only_mode_declares_checkpoint_dag"
        elif (
            terminal_authority_mode
            == "canonical_assertions_plus_checkpoint_dag"
            and (
                not isinstance(terminal_checkpoint_ids, list)
                or not terminal_checkpoint_ids
                or not isinstance(checkpoint_declarations, list)
                or not checkpoint_declarations
            )
        ):
            authority_contract_error = "checkpoint_mode_missing_terminal_dag"
    if authority_contract_error is not None:
        return {
            "score": 0.0,
            "raw_reward": 0.0,
            "progress_reward": 0.0,
            "terminal_success": False,
            "reason": "invalid_terminal_authority_contract",
            "terminal_authority_mode": terminal_authority_mode,
            "terminal_authority_contract_error": authority_contract_error,
            "canonical_assertions_satisfied": False,
            "checkpoint_outcomes": {},
            "checkpoint_reward_vector": [],
            "nonzero_checkpoint_reached": False,
            "env_valid": False,
            "format_valid": True,
            "evaluation_handle": snapshot.get("evaluation_handle"),
        }
    outcome_kind = str(evaluator.get("reward_authority", {}).get("outcome_kind") or "")
    canonical_state = outcome_kind == "canonical_state"
    mutation = outcome_kind in {
        "business_mutation", "runtime_selection", "query_projection"
    }
    if (mutation or canonical_state) and (
        not isinstance(assertions, dict) or not assertions
    ):
        return {
            "score": 0.0,
            "reason": "missing_canonical_state_assertions",
            "evaluation_handle": snapshot.get("evaluation_handle"),
        }
    raw_events = await read_events()
    observed = {
        str(event.get("transaction_id"))
        for event in raw_events
        if isinstance(event, dict) and event.get("transaction_id")
    }
    events = [event for event in raw_events if isinstance(event, dict)]
    required_sequence = [
        str(transaction_id)
        for transaction_id in evaluator.get("required_transaction_sequence", [])
        if transaction_id
    ]
    observed_sequence = [
        str(event.get("transaction_id"))
        for event in events
        if event.get("transaction_id")
    ]
    transaction_sequence_satisfied = is_ordered_subsequence(
        required_sequence,
        observed_sequence,
    )
    missing_receipts = []
    for authority in required_authorities:
        if authority.get("transaction_id"):
            continue
        action_id = str(authority.get("action_id") or "")
        receipt_event = str(authority.get("receipt_event") or "action_effect_observed")
        if not any(
            str(event.get("action_id") or "") == action_id
            and str(event.get("event_type") or "") == receipt_event
            for event in events
        ):
            missing_receipts.append({"action_id": action_id, "receipt_event": receipt_event})
    terminal_boundary = evaluator.get("required_terminal_action_step")
    terminal_receipt_plan = evaluator.get("terminal_action_receipt")
    terminal_receipt_plan = (
        terminal_receipt_plan if isinstance(terminal_receipt_plan, dict) else {}
    )
    terminal_match = terminal_receipt_plan.get("observation_match")
    terminal_receipt_required = (
        isinstance(terminal_match, dict)
        or (isinstance(terminal_boundary, int) and terminal_boundary > 0)
    )
    terminal_boundary_receipt = {
        "action_id": str(
            terminal_receipt_plan.get("action_id")
            or f"{task_id}:terminal_action_boundary"
        ),
        "receipt_event": str(
            terminal_receipt_plan.get("receipt_event")
            or "terminal_action_observed"
        ),
    }
    if (
        terminal_receipt_required
        and not any(
            str(event.get("action_id") or "") == terminal_boundary_receipt["action_id"]
            and str(event.get("event_type") or "") == terminal_boundary_receipt["receipt_event"]
            and (
                isinstance(terminal_match, dict)
                or int(
                    (event.get("payload") or {}).get("execution_step_index", -1)
                ) == terminal_boundary
            )
            for event in events
            if isinstance(event, dict)
        )
    ):
        missing_receipts.append(terminal_boundary_receipt)
    missing = sorted(expected - observed)
    state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
    assertion_failures = [
        path
        for path, assertion in assertions.items()
        if not isinstance(assertion, dict)
        or not assertion_matches(canonical_value(state, str(path)), assertion)
    ]
    if explicit_terminal_authority:
        passed = not assertion_failures
        success_reason = "canonical_assertions_satisfied"
    elif canonical_state:
        passed = bool(
            not assertion_failures
            and not missing
            and transaction_sequence_satisfied
        )
        success_reason = (
            "canonical_outcome_state_and_transaction_observed"
            if expected or required_sequence
            else "canonical_outcome_state_observed"
        )
    else:
        passed = bool(
            required_authorities
            and not missing
            and not missing_receipts
            and not assertion_failures
            and transaction_sequence_satisfied
        )
        if expected and any(not row.get("transaction_id") for row in required_authorities):
            success_reason = "canonical_outcome_transaction_and_receipt_observed"
        elif expected:
            success_reason = "canonical_outcome_transaction_observed"
        else:
            success_reason = "canonical_outcome_receipt_observed"
    result = {
        "score": 1.0 if passed else 0.0,
        "terminal_success": passed,
        "reason": (
            success_reason
            if passed
            else "canonical_outcome_state_or_transaction_mismatch"
        ),
        "expected_transaction_ids": sorted(expected),
        "missing_transaction_ids": missing,
        "missing_action_receipts": missing_receipts,
        "failed_canonical_assertions": sorted(assertion_failures),
        "required_transaction_sequence": required_sequence,
        "observed_transaction_sequence": observed_sequence,
        "transaction_sequence_satisfied": transaction_sequence_satisfied,
        "required_terminal_action_step": terminal_boundary,
        "terminal_action_match_mode": (
            "semantic_observation"
            if isinstance(terminal_match, dict)
            else "exact_execution_ordinal"
        ),
        "terminal_action_boundary_observed": not any(
            row.get("action_id") == terminal_boundary_receipt["action_id"]
            and row.get("receipt_event") == terminal_boundary_receipt["receipt_event"]
            for row in missing_receipts
        ),
        "evaluation_handle": snapshot.get("evaluation_handle"),
        "evidence_contract_mode": "legacy_compatibility",
        "portfolio_release_ready": False,
    }
    if explicit_terminal_authority:
        result.update({
            "raw_reward": result["score"],
            "progress_reward": 0.0,
            "terminal_authority_mode": terminal_authority_mode,
            "canonical_assertions_satisfied": not assertion_failures,
            "transaction_attribution_only": True,
            "checkpoint_outcomes": {},
            "checkpoint_reward_vector": [],
            "nonzero_checkpoint_reached": False,
            "env_valid": True,
            "format_valid": True,
        })
    checkpoint_reduction = reduce_declared_checkpoints(evaluator, state, events)
    if checkpoint_reduction is not None:
        result.update(checkpoint_reduction)
        if passed and not checkpoint_reduction["terminal_checkpoints_satisfied"]:
            passed = False
            result["score"] = 0.0
            result["terminal_success"] = False
            result["reason"] = "terminal_required_checkpoints_not_reached"
        result["raw_reward"] = result["score"]
    return result
