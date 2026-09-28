
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Any

_MISSING = object()
_CONTRACT_SCHEMA = "agentforge.webenv.state-kernel-evidence-contract.v1"
_EXPRESSION_SCHEMA = "agentforge.webenv.bounded-state-expression.v1"
_MAX_EXPRESSION_DEPTH = 8
_MAX_EXPRESSION_NODES = 64
_FORBIDDEN_POINTER_TOKENS = frozenset(
    {
        "action_id",
        "action_history",
        "action_type",
        "browser_terminal_assertions",
        "checkpoint",
        "checkpoint_event",
        "checkpoint_first_event_index",
        "checkpoint_id",
        "checkpoint_outcomes",
        "checkpoint_predicates",
        "checkpoint_reward_vector",
        "checkpoint_state",
        "checkpoints",
        "credit",
        "credited",
        "current_episode",
        "current_terminal_validation",
        "dependency_gated_checkpoint_state",
        "earned",
        "env_valid",
        "episode_id",
        "event_id",
        "event_history",
        "event_type",
        "expected",
        "expected_state",
        "firstEventIndex",
        "first_event_index",
        "forbiddenHit",
        "invalid",
        "missing",
        "nonzero_checkpoint_reached",
        "payload_valid",
        "pass",
        "passed",
        "present",
        "reached",
        "reward",
        "reward_contribution",
        "reward_weight",
        "runtime_history",
        "safety_violation",
        "state_observation_valid",
        "score",
        "seen",
        "structuredVectorTrusted",
        "structured_vector_trusted",
        "success",
        "task",
        "task_definition",
        "task_id",
        "terminal_success",
        "terminal_assertions",
        "terminal_validation",
        "terminal_verdict",
        "trace_id",
        "valid",
        "vector",
        "verdict",
        "verification",
    }
)
_BINARY_OPS = frozenset(
    {
        "at",
        "contains",
        "div",
        "eq",
        "gt",
        "gte",
        "lt",
        "lte",
        "mod",
        "neq",
        "not_contains",
        "one_of",
        "ordered_subsequence",
        "prefix",
        "set_equals",
        "set_subset",
        "sub",
        "suffix",
        "round",
    }
)
_NARY_OPS = frozenset({"add", "and", "max", "min", "mul", "or"})
_UNARY_OPS = frozenset(
    {
        "abs",
        "digit_sum",
        "is_prime",
        "length",
        "normalize_string",
        "not",
        "object_entries",
    }
)
_REGEX_BAD = (
    re.compile(r"\(\?"),
    re.compile(r"\\[1-9]"),
    re.compile(r"\)[*+?{]"),
    re.compile(r"(?:[*+?]|\})(?:[*+?]|\{)"),
)


class EvidenceContractError(ValueError):
    ...


@dataclass
class _ExpressionBudget:
    nodes: int = 0


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_clone(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _pointer_tokens(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise EvidenceContractError("expression pointer must be a non-root JSON pointer")
    tokens = [token.replace("~1", "/").replace("~0", "~") for token in path[1:].split("/")]
    if any(not token for token in tokens):
        raise EvidenceContractError("expression pointer contains an empty token")
    if any(token in _FORBIDDEN_POINTER_TOKENS for token in tokens):
        raise EvidenceContractError("expression pointer uses identity or self-certifying evidence")
    if len(tokens) >= 2 and tokens[:2] == ["verification", "group"]:
        raise EvidenceContractError("expression pointer uses a verification alias")
    return tokens


def _resolve_pointer(root: Any, path: str) -> Any:
    current = root
    for token in _pointer_tokens(path):
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return _MISSING
    return current


def _normalize_regex(pattern: Any, flags: Any) -> tuple[str, str]:
    if (
        not isinstance(pattern, str)
        or not 1 <= len(pattern) <= 256
        or not isinstance(flags, str)
        or len(flags) > 3
        or len(set(flags)) != len(flags)
        or any(flag not in "ims" for flag in flags)
        or any(rule.search(pattern) for rule in _REGEX_BAD)
    ):
        raise EvidenceContractError("matches_regex declaration is unsafe")
    regex_flags = 0
    if "i" in flags:
        regex_flags |= re.IGNORECASE
    if "m" in flags:
        regex_flags |= re.MULTILINE
    if "s" in flags:
        regex_flags |= re.DOTALL
    try:
        compiled = re.compile(pattern, regex_flags)
    except re.error as error:
        raise EvidenceContractError("matches_regex pattern does not compile") from error
    if compiled.search("") is not None:
        raise EvidenceContractError("matches_regex pattern may not match empty text")
    return pattern, flags


def _normalize_expression(
    raw: Any,
    *,
    depth: int = 1,
    budget: _ExpressionBudget | None = None,
) -> tuple[dict[str, Any], int]:
    if budget is None:
        budget = _ExpressionBudget()
    budget.nodes += 1
    if depth > _MAX_EXPRESSION_DEPTH or budget.nodes > _MAX_EXPRESSION_NODES:
        raise EvidenceContractError("expression exceeds depth/node limits")
    if not isinstance(raw, dict):
        raise EvidenceContractError("expression node must be an object")
    op = raw.get("op")
    if not isinstance(op, str):
        raise EvidenceContractError("expression node requires op")
    pointer_count = 0
    if op == "pointer":
        if set(raw) != {"op", "path"}:
            raise EvidenceContractError("pointer expression has unknown fields")
        path = raw.get("path")
        _pointer_tokens(path)
        return {"op": op, "path": path}, 1
    if op == "literal":
        if set(raw) != {"op", "value"}:
            raise EvidenceContractError("literal expression has unknown fields")
        try:
            encoded = _canonical_json(raw.get("value"))
        except (TypeError, ValueError) as error:
            raise EvidenceContractError("literal is not finite JSON") from error
        if len(encoded.encode("utf-8")) > 8192:
            raise EvidenceContractError("literal exceeds the expression byte limit")
        return {"op": op, "value": raw.get("value")}, 0
    if op in _BINARY_OPS | _NARY_OPS | _UNARY_OPS | {"approx_eq"}:
        if set(raw) != {"op", "args"}:
            raise EvidenceContractError(f"{op} expression requires args")
        args = raw.get("args")
        expected_count = (
            1 if op in _UNARY_OPS else 2 if op in _BINARY_OPS else 3 if op == "approx_eq" else None
        )
        if (
            not isinstance(args, list)
            or (expected_count is not None and len(args) != expected_count)
            or (op in _NARY_OPS and not 1 <= len(args) <= 16)
        ):
            raise EvidenceContractError(f"{op} expression has invalid arity")
        normalized_args = []
        for child in args:
            normalized, child_pointers = _normalize_expression(
                child, depth=depth + 1, budget=budget
            )
            normalized_args.append(normalized)
            pointer_count += child_pointers
        return {"op": op, "args": normalized_args}, pointer_count
    if op == "type_is":
        if set(raw) != {"op", "args", "expected_type"}:
            raise EvidenceContractError("type_is expression has invalid fields")
        args = raw.get("args")
        expected_type = raw.get("expected_type")
        if (
            not isinstance(args, list)
            or len(args) != 1
            or expected_type
            not in {
                "array",
                "boolean",
                "null",
                "number",
                "object",
                "string",
            }
        ):
            raise EvidenceContractError("type_is expression is invalid")
        arg, pointer_count = _normalize_expression(args[0], depth=depth + 1, budget=budget)
        return {
            "op": op,
            "args": [arg],
            "expected_type": expected_type,
        }, pointer_count
    if op == "matches_regex":
        if set(raw) != {"op", "args", "pattern", "flags"}:
            raise EvidenceContractError("matches_regex expression has invalid fields")
        args = raw.get("args")
        if not isinstance(args, list) or len(args) != 1:
            raise EvidenceContractError("matches_regex expression has invalid arity")
        pattern, flags = _normalize_regex(raw.get("pattern"), raw.get("flags"))
        arg, pointer_count = _normalize_expression(args[0], depth=depth + 1, budget=budget)
        return {
            "op": op,
            "args": [arg],
            "pattern": pattern,
            "flags": flags,
        }, pointer_count
    if op in {"argmin", "argmax", "argclosest"}:
        if set(raw) != {"op", "args", "value_pointer", "select_pointer"}:
            raise EvidenceContractError(f"{op} expression has invalid fields")
        args = raw.get("args")
        expected_count = 2 if op == "argclosest" else 1
        if not isinstance(args, list) or len(args) != expected_count:
            raise EvidenceContractError(f"{op} expression has invalid arity")
        value_pointer = raw.get("value_pointer")
        select_pointer = raw.get("select_pointer")
        _pointer_tokens(value_pointer)
        _pointer_tokens(select_pointer)
        normalized_args = []
        for child in args:
            normalized, child_pointers = _normalize_expression(
                child, depth=depth + 1, budget=budget
            )
            normalized_args.append(normalized)
            pointer_count += child_pointers
        return {
            "op": op,
            "args": normalized_args,
            "value_pointer": value_pointer,
            "select_pointer": select_pointer,
        }, pointer_count
    raise EvidenceContractError(f"unsupported expression op: {op}")


def _normalize_causal_event_sequence(raw: Any) -> list[dict[str, Any]]:
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
            _pointer_tokens(state_pointer)
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
    if not isinstance(evaluator, dict):
        raise EvidenceContractError("state-kernel evaluator row must be an object")
    raw_contract = evaluator.get("state_kernel_evidence_contract")
    if raw_contract is None:
        return None
    if not isinstance(raw_contract, dict):
        raise EvidenceContractError("state_kernel_evidence_contract must be an object")
    if raw_contract.get("contract_schema") != _CONTRACT_SCHEMA:
        raise EvidenceContractError("unsupported state-kernel evidence contract schema")
    if raw_contract.get("evidence_schema_version") != 1:
        raise EvidenceContractError("unsupported state-kernel evidence schema version")
    if raw_contract.get("expression_schema") != _EXPRESSION_SCHEMA:
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
            expression, pointer_count = _normalize_expression(raw_assertion["expression"])
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
            expression, pointer_count = _normalize_expression(raw_assertion["expression"])
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
        "contract_schema": _CONTRACT_SCHEMA,
        "evidence_schema_version": 1,
        "expression_schema": _EXPRESSION_SCHEMA,
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


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _canonical_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _number(left) is not None or _number(right) is not None:
        return _number(left) is not None and _number(right) is not None and left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_canonical_equal(a, b) for a, b in zip(left, right, strict=True))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(_canonical_equal(left[key], right[key]) for key in left)
        )
    return False


def _canonical_set(value: Any) -> list[Any] | None:
    if not isinstance(value, list) or len(value) > 2048:
        return None
    unique: list[Any] = []
    for item in value:
        if not any(_canonical_equal(item, existing) for existing in unique):
            unique.append(item)
    return unique


def _expression_value(
    expression: dict[str, Any],
    state: dict[str, Any],
    pointer_ledger: list[dict[str, Any]],
) -> tuple[Any, str | None]:
    op = expression["op"]
    if op == "pointer":
        value = _resolve_pointer(state, expression["path"])
        pointer_ledger.append(
            {
                "path": expression["path"],
                "resolved": value is not _MISSING,
                **({"value": value} if value is not _MISSING else {}),
            }
        )
        return value, None if value is not _MISSING else "pointer_missing"
    if op == "literal":
        return expression["value"], None
    values = []
    for child in expression.get("args", []):
        value, error = _expression_value(child, state, pointer_ledger)
        if error:
            return _MISSING, error
        values.append(value)
    if op in _UNARY_OPS:
        value = values[0]
        if op == "not":
            return (not value, None) if isinstance(value, bool) else (_MISSING, "type_mismatch")
        if op == "length":
            return (
                (len(value), None)
                if isinstance(value, (str, list, dict))
                else (_MISSING, "type_mismatch")
            )
        if op == "abs":
            number = _number(value)
            return (abs(number), None) if number is not None else (_MISSING, "type_mismatch")
        if op == "normalize_string":
            if not isinstance(value, str) or len(value) > 4096:
                return _MISSING, "type_mismatch"
            normalized = " ".join(unicodedata.normalize("NFKC", value).strip().lower().split())
            return normalized, None
        if op == "object_entries":
            if not isinstance(value, dict) or len(value) > 1024:
                return _MISSING, "type_or_bound_mismatch"
            return [{"key": key, "value": value[key]} for key in sorted(value)], None
        if op == "is_prime":
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 2 <= value <= 1_000_000_000
            ):
                return _MISSING, "type_or_bound_mismatch"
            if value % 2 == 0:
                return value == 2, None
            divisor = 3
            while divisor * divisor <= value:
                if value % divisor == 0:
                    return False, None
                divisor += 2
            return True, None
        if op == "digit_sum":
            if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 2**53 - 1:
                digits = str(abs(value))
            elif (
                isinstance(value, str) and len(value) <= 256 and value.isascii() and value.isdigit()
            ):
                digits = value
            else:
                return _MISSING, "type_or_bound_mismatch"
            return sum(int(digit) for digit in digits), None
    if op in _NARY_OPS:
        if op in {"and", "or"}:
            if any(not isinstance(value, bool) for value in values):
                return _MISSING, "type_mismatch"
            return (all(values) if op == "and" else any(values)), None
        numeric_values = values
        if op in {"min", "max"} and len(values) == 1 and isinstance(values[0], list):
            numeric_values = values[0]
        numbers = [_number(value) for value in numeric_values]
        if any(value is None for value in numbers):
            return _MISSING, "type_mismatch"
        typed_numbers = [value for value in numbers if value is not None]
        if op == "add":
            result = sum(typed_numbers)
        elif op == "mul":
            result = math.prod(typed_numbers)
        elif op == "min":
            result = min(typed_numbers)
        else:
            result = max(typed_numbers)
        return (result, None) if math.isfinite(result) else (_MISSING, "non_finite_result")

    if op == "type_is":
        value = values[0]
        actual_type = (
            "null"
            if value is None
            else "array"
            if isinstance(value, list)
            else "boolean"
            if isinstance(value, bool)
            else "number"
            if _number(value) is not None
            else "string"
            if isinstance(value, str)
            else "object"
            if isinstance(value, dict)
            else "unsupported"
        )
        return actual_type == expression["expected_type"], None
    if op == "matches_regex":
        value = values[0]
        if not isinstance(value, str) or len(value) > 4096:
            return _MISSING, "type_mismatch"
        regex_flags = sum(
            flag
            for marker, flag in (
                ("i", re.IGNORECASE),
                ("m", re.MULTILINE),
                ("s", re.DOTALL),
            )
            if marker in expression["flags"]
        )
        return bool(re.search(expression["pattern"], value, regex_flags)), None
    if op in {"argmin", "argmax", "argclosest"}:
        collection = values[0]
        target = values[1] if op == "argclosest" else None
        if (
            not isinstance(collection, list)
            or not collection
            or len(collection) > 1024
            or (op == "argclosest" and _number(target) is None)
        ):
            return _MISSING, "collection_invalid"
        rows: list[tuple[float | int, Any]] = []
        for item in collection:
            if not isinstance(item, dict):
                return _MISSING, "collection_invalid"
            scored = _resolve_pointer(item, expression["value_pointer"])
            selected = _resolve_pointer(item, expression["select_pointer"])
            number = _number(scored)
            if scored is _MISSING or selected is _MISSING or number is None:
                return _MISSING, "collection_pointer_invalid"
            score = abs(number - target) if op == "argclosest" else number
            rows.append((score, selected))
        best = (
            max(score for score, _ in rows) if op == "argmax" else min(score for score, _ in rows)
        )
        selected_values = []
        seen: set[str] = set()
        for score, selected in rows:
            key = _canonical_json(selected)
            if score == best and key not in seen:
                seen.add(key)
                selected_values.append(selected)
        return selected_values, None

    left = values[0]
    right = values[1] if len(values) > 1 else None
    if op == "at":
        if isinstance(right, bool) or not isinstance(right, int) or right < 0:
            return _MISSING, "index_invalid"
        if isinstance(left, str):
            sequence: Any = list(left)
            if len(sequence) > 4096:
                return _MISSING, "type_or_bound_mismatch"
        elif isinstance(left, list):
            sequence = left
            if len(sequence) > 4096:
                return _MISSING, "type_or_bound_mismatch"
        else:
            return _MISSING, "type_mismatch"
        return (
            (sequence[right], None) if right < len(sequence) else (_MISSING, "index_out_of_range")
        )
    if op == "eq":
        return _canonical_equal(left, right), None
    if op == "neq":
        return not _canonical_equal(left, right), None
    if op == "approx_eq":
        numbers = [_number(value) for value in values]
        if any(value is None for value in numbers) or numbers[2] < 0:
            return _MISSING, "type_mismatch"
        return abs(numbers[0] - numbers[1]) <= numbers[2], None
    if op in {"lt", "lte", "gt", "gte", "sub", "div", "mod", "round"}:
        left_number = _number(left)
        right_number = _number(right)
        if left_number is None or right_number is None:
            return _MISSING, "type_mismatch"
        if op == "lt":
            return left_number < right_number, None
        if op == "lte":
            return left_number <= right_number, None
        if op == "gt":
            return left_number > right_number, None
        if op == "gte":
            return left_number >= right_number, None
        if op in {"div", "mod"} and right_number == 0:
            return _MISSING, "division_by_zero"
        if op == "round":
            if not isinstance(right_number, int) or not -12 <= right_number <= 12:
                return _MISSING, "round_digits_invalid"
            if right_number >= 0:
                factor = 10**right_number
                result = math.floor((left_number + sys.float_info.epsilon) * factor + 0.5) / factor
            else:
                factor = 10**-right_number
                result = math.floor(left_number / factor + 0.5) * factor
            return (result, None) if math.isfinite(result) else (_MISSING, "numeric_overflow")
        result = (
            left_number - right_number
            if op == "sub"
            else left_number / right_number
            if op == "div"
            else math.fmod(left_number, right_number)
        )
        return (result, None) if math.isfinite(result) else (_MISSING, "non_finite_result")
    if op in {"contains", "not_contains"}:
        if isinstance(left, str) and isinstance(right, str):
            contains = right in left
        elif isinstance(left, list):
            contains = any(_canonical_equal(item, right) for item in left)
        else:
            return _MISSING, "type_mismatch"
        return (not contains if op == "not_contains" else contains), None
    if op == "one_of":
        return (
            (any(_canonical_equal(left, item) for item in right), None)
            if isinstance(right, list) and len(right) <= 2048
            else (_MISSING, "type_mismatch")
        )
    if op in {"set_equals", "set_subset"}:
        left_set = _canonical_set(left)
        right_set = _canonical_set(right)
        if left_set is None or right_set is None:
            return _MISSING, "type_mismatch"
        if op == "set_equals":
            return (
                len(left_set) == len(right_set)
                and all(
                    any(_canonical_equal(item, other) for other in right_set) for item in left_set
                )
            ), None
        return all(
            any(_canonical_equal(item, other) for other in right_set) for item in left_set
        ), None
    if op in {"prefix", "suffix"}:
        if isinstance(left, str) and isinstance(right, str):
            return (left.startswith(right) if op == "prefix" else left.endswith(right)), None
        if isinstance(left, list) and isinstance(right, list):
            if len(left) > 2048 or len(right) > 2048:
                return _MISSING, "type_mismatch"
            if len(right) > len(left):
                return False, None
            return (
                all(_canonical_equal(a, b) for a, b in zip(left[: len(right)], right, strict=True))
                if op == "prefix"
                else all(
                    _canonical_equal(a, b)
                    for a, b in zip(left[len(left) - len(right) :], right, strict=True)
                )
            ), None
        return _MISSING, "type_mismatch"
    if op == "ordered_subsequence":
        if isinstance(left, str) and isinstance(right, str):
            left, right = list(left), list(right)
        if (
            not isinstance(left, list)
            or not isinstance(right, list)
            or len(left) > 2048
            or len(right) > 2048
        ):
            return _MISSING, "type_mismatch"
        cursor = 0
        for item in left:
            if cursor < len(right) and _canonical_equal(item, right[cursor]):
                cursor += 1
        return cursor == len(right), None
    return _MISSING, "unsupported_expression"


def _typed_path_value(state: Any, path: str) -> Any:
    if path == "$":
        return state
    if not isinstance(path, str) or not path.startswith("$."):
        return _MISSING
    remaining = path[2:]
    current = state
    while remaining:
        if isinstance(current, dict):
            key = next(
                (
                    candidate
                    for candidate in sorted(current, key=len, reverse=True)
                    if remaining == candidate
                    or remaining.startswith(f"{candidate}.")
                    or remaining.startswith(f"{candidate}[")
                ),
                None,
            )
            if key is None:
                return _MISSING
            current = current[key]
            remaining = remaining[len(key) :]
            if remaining.startswith("."):
                remaining = remaining[1:]
            continue
        if isinstance(current, list) and remaining.startswith("[id="):
            end = remaining.find("]")
            if end < 0:
                return _MISSING
            entity_id = remaining[4:end]
            current = next(
                (
                    item
                    for item in current
                    if isinstance(item, dict) and str(item.get("id")) == entity_id
                ),
                _MISSING,
            )
            if current is _MISSING:
                return _MISSING
            remaining = remaining[end + 1 :]
            if remaining.startswith("."):
                remaining = remaining[1:]
            continue
        return _MISSING
    return current


def _typed_path_target(state: Any, path: str) -> tuple[Any, Any] | None:
    if not isinstance(path, str) or not path.startswith("$."):
        return None
    remaining = path[2:]
    current = state
    while remaining:
        if isinstance(current, dict):
            key = next(
                (
                    candidate
                    for candidate in sorted(current, key=len, reverse=True)
                    if remaining == candidate
                    or remaining.startswith(f"{candidate}.")
                    or remaining.startswith(f"{candidate}[")
                ),
                None,
            )
            if key is None:


                if "[id=" in remaining:
                    return None
                return current, remaining
            if remaining == key:
                return current, key
            current = current[key]
            remaining = remaining[len(key) :]
            if remaining.startswith("."):
                remaining = remaining[1:]
            continue
        if isinstance(current, list) and remaining.startswith("[id="):
            end = remaining.find("]")
            if end < 0:
                return None
            entity_id = remaining[4:end]
            index = next(
                (
                    index
                    for index, item in enumerate(current)
                    if isinstance(item, dict) and str(item.get("id")) == entity_id
                ),
                None,
            )
            tail = remaining[end + 1 :]
            if index is None:
                return (current, ("entity", entity_id)) if not tail else None
            if not tail:
                return current, index
            current = current[index]
            remaining = tail[1:] if tail.startswith(".") else tail
            continue
        return None
    return None


def _apply_typed_diff(state: dict[str, Any], row: dict[str, Any]) -> str | None:
    operation = row["operation"]
    path = row["path"]
    detached_after = _canonical_clone(row["after"])
    actual = _typed_path_value(state, path)
    expected_missing = operation in {"field_added", "entity_added"}
    if expected_missing:
        if actual is not _MISSING:
            return "diff_before_expected_missing"
    elif actual is _MISSING or not _canonical_equal(actual, row["before"]):
        return "diff_before_mismatch"

    target = _typed_path_target(state, path)
    if target is None:
        return "diff_path_not_applicable"
    parent, key = target
    if operation == "field_added":
        if not isinstance(parent, dict) or not isinstance(key, str):
            return "diff_operation_target_mismatch"
        parent[key] = detached_after
    elif operation == "field_removed":
        if not isinstance(parent, dict) or not isinstance(key, str) or key not in parent:
            return "diff_operation_target_mismatch"
        del parent[key]
    elif operation == "entity_added":
        if (
            not isinstance(parent, list)
            or not isinstance(key, tuple)
            or len(key) != 2
            or key[0] != "entity"
            or not isinstance(row["after"], dict)
            or str(row["after"].get("id")) != key[1]
        ):
            return "diff_operation_target_mismatch"
        parent.append(detached_after)
    elif operation == "entity_removed":
        if not isinstance(parent, list) or not isinstance(key, int):
            return "diff_operation_target_mismatch"
        del parent[key]
    else:
        if (
            isinstance(parent, dict)
            and isinstance(key, str)
            and key in parent
            or isinstance(parent, list)
            and isinstance(key, int)
            and key < len(parent)
        ):
            parent[key] = detached_after
        else:
            return "diff_operation_target_mismatch"
    updated = _typed_path_value(state, path)
    if operation in {"field_removed", "entity_removed"}:
        return None if updated is _MISSING else "diff_after_mismatch"
    return None if _canonical_equal(updated, row["after"]) else "diff_after_mismatch"


def _valid_diff_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        _canonical_json(row)
    except (TypeError, ValueError):
        return False
    if not {"operation", "path", "before", "after"}.issubset(row):
        return False
    operation = row.get("operation")
    path = row.get("path")
    before = row.get("before")
    after = row.get("after")
    if not isinstance(path, str) or not path.startswith("$") or _canonical_equal(before, after):
        return False
    if operation == "field_added":
        return before is None
    if operation == "field_removed":
        return after is None
    if operation == "field_replaced":
        return True
    if operation == "ordered_sequence_changed":
        return isinstance(before, list) and isinstance(after, list)
    if operation == "entity_added":
        return "[id=" in path and before is None and isinstance(after, dict)
    if operation == "entity_removed":
        return "[id=" in path and isinstance(before, dict) and after is None
    if operation == "numeric_delta":
        before_number = _number(before)
        after_number = _number(after)
        delta = _number(row.get("delta"))
        return (
            before_number is not None
            and after_number is not None
            and delta is not None
            and delta == after_number - before_number
        )
    return False


def _validate_carrier(
    *,
    task_id: str,
    sid: str,
    binding: Any,
    snapshot: Any,
    events: Any,
    expected_snapshot_kind: str,
    validate_runtime_events: bool,
) -> tuple[dict[str, Any], list[str], dict[int, dict[str, Any]]]:
    ledger: dict[str, Any] = {
        "contract_valid": True,
        "identity_valid": False,
        "snapshot_valid": False,
        "events_valid": False,
        "reasons": [],
        "event_rows": [],
        "server_episode_binding": None,
    }
    reasons: list[str] = ledger["reasons"]
    post_state_by_event_index: dict[int, dict[str, Any]] = {}
    if (
        not isinstance(binding, dict)
        or binding.get("task_id") != task_id
        or binding.get("sid") != sid
    ):
        reasons.append("server_episode_binding_mismatch")
        return ledger, reasons, post_state_by_event_index
    ledger["server_episode_binding"] = {
        "task_id": binding.get("task_id"),
        "sid": binding.get("sid"),
        "episode_epoch": binding.get("episode_epoch"),
        "initial_snapshot_id": binding.get("initial_snapshot_id"),
        "initial_revision": binding.get("initial_revision"),
        "initial_state_hash": binding.get("initial_state_hash"),
    }
    if not isinstance(snapshot, dict):
        reasons.append("canonical_snapshot_missing")
        return ledger, reasons, post_state_by_event_index
    state = snapshot.get("state")
    version = snapshot.get("state_version")
    handle = snapshot.get("evaluation_handle")
    if (
        not isinstance(handle, dict)
        and expected_snapshot_kind == "preview"
        and isinstance(version, dict)
    ):
        handle = {
            "sid": version.get("sid"),
            "episode_epoch": version.get("episode_epoch"),
            "revision": version.get("revision"),
            "snapshot_id": snapshot.get("snapshot_id"),
            "state_hash": snapshot.get("state_hash"),
        }
        ledger["evaluation_handle_source"] = "server_bound_preview_snapshot"
    else:
        ledger["evaluation_handle_source"] = "sidecar_finalization_handle"
    if not isinstance(state, dict) or not isinstance(version, dict) or not isinstance(handle, dict):
        reasons.append("canonical_snapshot_schema_invalid")
        return ledger, reasons, post_state_by_event_index
    revision = version.get("revision")
    identity_values = {
        "sid": sid,
        "episode_epoch": binding.get("episode_epoch"),
        "revision": revision,
    }
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or any(version.get(key) != value for key, value in identity_values.items())
        or any(handle.get(key) != value for key, value in identity_values.items())
        or not isinstance(snapshot.get("snapshot_id"), str)
        or not snapshot["snapshot_id"]
        or handle.get("snapshot_id") != snapshot.get("snapshot_id")
        or snapshot.get("snapshot_kind") != expected_snapshot_kind
    ):
        reasons.append("snapshot_identity_mismatch")
        return ledger, reasons, post_state_by_event_index
    ledger["identity_valid"] = True
    ledger["effective_evaluation_handle"] = dict(handle)
    try:
        state_hash = _canonical_hash(state)
    except (TypeError, ValueError):
        reasons.append("canonical_snapshot_state_not_finite_json")
        return ledger, reasons, post_state_by_event_index
    ledger["snapshot_identity"] = {
        "sid": version.get("sid"),
        "episode_epoch": version.get("episode_epoch"),
        "revision": revision,
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_kind": snapshot.get("snapshot_kind"),
    }
    ledger["snapshot_hash_validation"] = {
        "declared": snapshot.get("state_hash"),
        "computed": state_hash,
        "handle": handle.get("state_hash"),
    }
    if snapshot.get("state_hash") != state_hash or handle.get("state_hash") != state_hash:
        reasons.append("snapshot_state_hash_mismatch")
        return ledger, reasons, post_state_by_event_index
    ledger["snapshot_valid"] = True
    if not validate_runtime_events:
        ledger["events_valid"] = None
        ledger["event_validation_mode"] = "not_declared"
        return ledger, reasons, post_state_by_event_index
    ledger["event_validation_mode"] = "strict_canonical_sidecar_v1"
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        reasons.append("event_stream_schema_invalid")
        return ledger, reasons, post_state_by_event_index

    initial_state = binding.get("initial_state")
    initial_state_hash = binding.get("initial_state_hash")
    try:
        computed_initial_state_hash = (
            _canonical_hash(initial_state) if isinstance(initial_state, dict) else None
        )
    except (TypeError, ValueError):
        computed_initial_state_hash = None
    if (
        not isinstance(initial_state, dict)
        or not isinstance(binding.get("initial_snapshot_id"), str)
        or not binding["initial_snapshot_id"]
        or binding.get("initial_revision") != 0
        or not isinstance(initial_state_hash, str)
        or computed_initial_state_hash != initial_state_hash
    ):
        reasons.append("initial_snapshot_binding_missing_or_invalid")
        return ledger, reasons, post_state_by_event_index
    replay_state = json.loads(_canonical_json(initial_state))
    ledger["event_replay"] = {
        "initial_snapshot_id": binding.get("initial_snapshot_id"),
        "initial_state_hash": initial_state_hash,
        "diff_rows": [],
        "touched_paths": [],
    }

    previous_revision = 0
    touched_paths: set[str] = set()
    for index, event in enumerate(events):
        event_revision = event.get("revision")
        row_ledger = {"index": index, "revision": event_revision, "valid": False, "reasons": []}
        ledger["event_rows"].append(row_ledger)
        if (
            isinstance(event_revision, bool)
            or not isinstance(event_revision, int)
            or event_revision < 1
        ):
            row_ledger["reasons"].append("invalid_event_revision")
            reasons.append("invalid_event_revision")
            continue
        if event_revision <= previous_revision:
            row_ledger["reasons"].append("non_monotonic_event_revision")
            reasons.append("non_monotonic_event_revision")
        elif event_revision != previous_revision + 1:
            row_ledger["reasons"].append("event_revision_gap")
            reasons.append("event_revision_gap")
        if event_revision > revision:
            row_ledger["reasons"].append("event_revision_after_snapshot")
            reasons.append("event_revision_after_snapshot")
        previous_revision = event_revision
        transaction_id = event.get("transaction_id")
        if isinstance(transaction_id, str) and transaction_id:
            diffs = event.get("state_diff")
            if not isinstance(diffs, list) or not diffs:
                row_ledger["reasons"].append("empty_transaction_state_diff")
                reasons.append("empty_transaction_state_diff")
            else:
                for diff_index, diff in enumerate(diffs):
                    diff_ledger = {
                        "event_index": index,
                        "diff_index": diff_index,
                        "operation": diff.get("operation") if isinstance(diff, dict) else None,
                        "path": diff.get("path") if isinstance(diff, dict) else None,
                        "valid": False,
                        "reason": None,
                    }
                    ledger["event_replay"]["diff_rows"].append(diff_ledger)
                    if not _valid_diff_row(diff):
                        row_ledger["reasons"].append("invalid_typed_state_diff")
                        reasons.append("invalid_typed_state_diff")
                        diff_ledger["reason"] = "invalid_typed_state_diff"
                        continue
                    path = diff["path"]
                    touched_paths.add(path)
                    replay_error = _apply_typed_diff(replay_state, diff)
                    if replay_error is not None:
                        row_ledger["reasons"].append("state_diff_replay_mismatch")
                        reasons.append("state_diff_replay_mismatch")
                        diff_ledger["reason"] = replay_error
                    else:
                        diff_ledger["valid"] = True
        elif (
            not isinstance(event.get("event_type"), str)
            or not event.get("event_type")
            or not isinstance(event.get("action_id"), str)
            or not event.get("action_id")
            or not isinstance(event.get("payload", {}), dict)
        ):
            row_ledger["reasons"].append("unknown_event_shape")
            reasons.append("unknown_event_shape")
        row_ledger["valid"] = not row_ledger["reasons"]
        post_state_by_event_index[index] = json.loads(_canonical_json(replay_state))
        row_ledger["post_state_hash"] = _canonical_hash(replay_state)
    if events and previous_revision != revision:
        reasons.append("event_revision_gap")
    if not events and revision != 0:
        reasons.append("event_revision_gap")
    ledger["event_replay"]["touched_paths"] = sorted(touched_paths)
    ledger["event_replay"]["replayed_touched_state_hash"] = _canonical_hash(
        {
            path: (
                {"missing": True}
                if _typed_path_value(replay_state, path) is _MISSING
                else _typed_path_value(replay_state, path)
            )
            for path in sorted(touched_paths)
        }
    )
    ledger["event_replay"]["replayed_final_state_hash"] = _canonical_hash(replay_state)
    ledger["event_replay"]["final_snapshot_state_hash"] = _canonical_hash(state)
    if not _canonical_equal(replay_state, state):
        reasons.append("event_replay_final_state_mismatch")
    ledger["events_valid"] = not reasons
    return ledger, reasons, post_state_by_event_index


def _event_matches_binding(
    event: dict[str, Any],
    binding: dict[str, Any],
    state: dict[str, Any],
) -> tuple[bool, list[str]]:
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
                    and _valid_diff_row(row)
                ]
                if not candidates:
                    reasons.append("affected_state_diff_missing")
                    continue
                actual = _resolve_pointer(state, affected["state_pointer"])
                if actual is _MISSING:
                    reasons.append("affected_state_pointer_missing")
                elif not any(_canonical_equal(row.get("after"), actual) for row in candidates):
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
                    _canonical_json(post_state_by_event_index[matched_index])
                )
                anchor_ledger = {
                    "anchor_binding_id": binding["binding_id"],
                    "anchor_event_index": matched_index,
                    "anchor_revision": events[matched_index].get("revision"),
                    "anchor_state_hash": _canonical_hash(anchor_state),
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
    matched = True
    rows: list[dict[str, Any]] = []
    for assertion in assertions:
        pointers: list[dict[str, Any]] = []
        value, error = _expression_value(assertion["expression"], state, pointers)
        assertion_matched = error is None and value is True
        matched = matched and assertion_matched
        rows.append(
            {
                "valid": error is None,
                "matched": assertion_matched,
                "reason": error,
                "expression": assertion["expression"],
                **({"value": value} if value is not _MISSING else {}),
                "pointer_resolutions": pointers,
            }
        )
    return matched, rows


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
    try:
        contract = normalize_state_kernel_evaluator(evaluator)
    except EvidenceContractError as error:
        return {
            "score": 0.0,
            "raw_reward": 0.0,
            "progress_reward": 0.0,
            "terminal_success": False,
            "reason": "invalid_state_kernel_evidence_contract",
            "env_valid": False,
            "format_valid": True,
            "checkpoint_outcomes": {},
            "checkpoint_reward_vector": [],
            "forbidden_condition_state": {},
            "producer_projection_credit_used": False,
            "portfolio_release_ready": False,
            "evidence_contract_mode": "strict_state_kernel_v1",
            "state_kernel_evidence_ledger": {
                "contract_valid": False,
                "identity_valid": False,
                "snapshot_valid": False,
                "events_valid": False,
                "reasons": [str(error)],
            },
            "evaluation_handle": snapshot.get("evaluation_handle"),
        }
    if contract is None:
        raise EvidenceContractError("strict reducer called without strict contract")
    carrier_ledger, reasons, post_state_by_event_index = _validate_carrier(
        task_id=task_id,
        sid=sid,
        binding=binding,
        snapshot=snapshot,
        events=events,
        expected_snapshot_kind=expected_snapshot_kind,
        validate_runtime_events=contract["validate_runtime_events"],
    )
    if reasons:
        return {
            "score": 0.0,
            "raw_reward": 0.0,
            "progress_reward": 0.0,
            "terminal_success": False,
            "reason": "invalid_state_kernel_evidence",
            "env_valid": False,
            "format_valid": True,
            "checkpoint_outcomes": {},
            "checkpoint_reward_vector": [],
            "forbidden_condition_state": {},
            "producer_projection_credit_used": False,
            "portfolio_release_ready": False,
            "evidence_contract_mode": "strict_state_kernel_v1",
            "state_kernel_evidence_ledger": carrier_ledger,
            "evaluation_handle": carrier_ledger.get("effective_evaluation_handle"),
        }

    state = snapshot["state"]
    expression_ledger: dict[str, Any] = {}
    raw_outcomes: dict[str, bool] = {}
    terminal_checkpoint_ids = set(contract["terminal_checkpoints"])
    for checkpoint_id, spec in contract["checkpoints"].items():
        causal_matched = True
        anchor_state: dict[str, Any] | None = None
        causal_ledger = {
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
            state_evaluation_source = "final_snapshot"
        elif anchor_state is not None:
            matched, assertion_rows = _evaluate_assertions(
                spec["assertions"], anchor_state
            )
            state_evaluation_source = "transaction_anchor_post_state"
        else:
            matched = False
            assertion_rows = []
            state_evaluation_source = "unavailable_causal_anchor_post_state"

        final_state_gate_required = bool(
            "causal_event_sequence" in spec
            and checkpoint_id in terminal_checkpoint_ids
        )
        if final_state_gate_required:
            final_state_matched, final_state_assertion_rows = _evaluate_assertions(
                spec["assertions"], state
            )
        else:
            final_state_matched = True
            final_state_assertion_rows = []
        raw_outcomes[checkpoint_id] = bool(
            matched and causal_matched and final_state_matched
        )
        expression_ledger[checkpoint_id] = {
            "valid": (
                all(row["valid"] for row in assertion_rows)
                and all(row["valid"] for row in final_state_assertion_rows)
            ),
            "state_matched": matched,
            "anchor_state_matched": matched if anchor_state is not None else None,
            "state_evaluation_source": state_evaluation_source,
            "anchor_binding_id": causal_ledger.get("anchor_binding_id"),
            "anchor_event_index": causal_ledger.get("anchor_event_index"),
            "anchor_revision": causal_ledger.get("anchor_revision"),
            "anchor_state_hash": causal_ledger.get("anchor_state_hash"),
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
        causal_matched = True
        anchor_state = None
        causal_ledger = {
            "required": False,
            "matched": True,
            "ordered": True,
            "bindings": [],
        }
        if "causal_event_sequence" in condition:
            causal_matched, causal_ledger, anchor_state = _match_causal_event_sequence(
                condition["causal_event_sequence"],
                events,
                post_state_by_event_index,
            )
            causal_ledger["required"] = True
        if "causal_event_sequence" not in condition:
            matched, assertion_rows = _evaluate_assertions(
                condition["assertions"], state
            )
            state_evaluation_source = "final_snapshot"
        elif anchor_state is not None:
            matched, assertion_rows = _evaluate_assertions(
                condition["assertions"], anchor_state
            )
            state_evaluation_source = "transaction_anchor_post_state"
        else:
            matched = False
            assertion_rows = []
            state_evaluation_source = "unavailable_causal_anchor_post_state"
        forbidden_state[condition["id"]] = matched and causal_matched
        forbidden_ledger[condition["id"]] = {
            "valid": all(row["valid"] for row in assertion_rows),
            "state_matched": matched,
            "anchor_state_matched": matched if anchor_state is not None else None,
            "state_evaluation_source": state_evaluation_source,
            "anchor_binding_id": causal_ledger.get("anchor_binding_id"),
            "anchor_event_index": causal_ledger.get("anchor_event_index"),
            "anchor_revision": causal_ledger.get("anchor_revision"),
            "anchor_state_hash": causal_ledger.get("anchor_state_hash"),
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
        return {
            "score": 0.0,
            "raw_reward": 0.0,
            "progress_reward": 0.0,
            "terminal_success": False,
            "reason": "invalid_state_kernel_expression_evidence",
            "env_valid": False,
            "format_valid": True,
            "checkpoint_outcomes": {},
            "checkpoint_reward_vector": [],
            "forbidden_condition_state": {},
            "producer_projection_credit_used": False,
            "portfolio_release_ready": False,
            "evidence_contract_mode": "strict_state_kernel_v1",
            "state_kernel_evidence_ledger": carrier_ledger,
            "evaluation_handle": carrier_ledger.get("effective_evaluation_handle"),
        }
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
        "evidence_contract_mode": "strict_state_kernel_v1",
        "state_kernel_evidence_ledger": carrier_ledger,
        "evaluation_handle": carrier_ledger.get("effective_evaluation_handle"),
    }
