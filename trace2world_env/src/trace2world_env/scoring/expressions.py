"""Bounded state expressions: canonical JSON helpers, JSON-pointer resolution, and the
side-effect-free expression language used by checkpoint evidence contracts.

An expression is a small JSON tree (at most 8 levels deep and 64 nodes) of ``pointer`` and
``literal`` leaves combined by fixed operators. Evaluation never raises on data: it returns
``(value, None)`` or ``(MISSING, reason)``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Any

from trace2world_env.scoring.errors import EvidenceContractError


MISSING = object()
EXPRESSION_SCHEMA = "trace2world.bounded-state-expression.v1"
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


@dataclass
class _ExpressionBudget:
    """Shared node counter for one expression tree during normalisation."""

    nodes: int = 0


def canonical_json(value: Any) -> str:
    """Serialise ``value`` as sorted, compact, finite JSON (the hashing form)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_json`."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_clone(value: Any) -> Any:
    """Deep copy of a JSON value through its canonical serialisation."""
    return json.loads(canonical_json(value))


def pointer_tokens(path: str) -> list[str]:
    """Split a non-root JSON pointer, rejecting identity and self-certifying tokens."""
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


def resolve_pointer(root: Any, path: str) -> Any:
    """Resolve a JSON pointer against ``root``; returns :data:`MISSING` when absent."""
    current = root
    for token in pointer_tokens(path):
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return MISSING
    return current


def _normalize_regex(pattern: Any, flags: Any) -> tuple[str, str]:
    """Validate a ``matches_regex`` pattern/flags pair against the safe subset."""
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


def normalize_expression(
    raw: Any,
    *,
    depth: int = 1,
    budget: _ExpressionBudget | None = None,
) -> tuple[dict[str, Any], int]:
    """Validate an expression tree and return ``(normalised, pointer_leaf_count)``.

    Raises :class:`EvidenceContractError` for unknown operators, bad arity, unsafe regexes,
    forbidden pointers, or trees beyond the depth/node budget.
    """
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
        pointer_tokens(path)
        return {"op": op, "path": path}, 1
    if op == "literal":
        if set(raw) != {"op", "value"}:
            raise EvidenceContractError("literal expression has unknown fields")
        try:
            encoded = canonical_json(raw.get("value"))
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
            normalized, child_pointers = normalize_expression(
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
        arg, pointer_count = normalize_expression(args[0], depth=depth + 1, budget=budget)
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
        arg, pointer_count = normalize_expression(args[0], depth=depth + 1, budget=budget)
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
        pointer_tokens(value_pointer)
        pointer_tokens(select_pointer)
        normalized_args = []
        for child in args:
            normalized, child_pointers = normalize_expression(
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


def finite_number(value: Any) -> float | int | None:
    """Return ``value`` if it is a finite int/float (not a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def canonical_equal(left: Any, right: Any) -> bool:
    """Type-strict JSON equality (bools never equal numbers, dict keys must match)."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if finite_number(left) is not None or finite_number(right) is not None:
        return (
            finite_number(left) is not None
            and finite_number(right) is not None
            and left == right
        )
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(canonical_equal(a, b) for a, b in zip(left, right, strict=True))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(canonical_equal(left[key], right[key]) for key in left)
        )
    return False


def _canonical_set(value: Any) -> list[Any] | None:
    """De-duplicate a bounded list under :func:`canonical_equal`; ``None`` if not a list."""
    if not isinstance(value, list) or len(value) > 2048:
        return None
    unique: list[Any] = []
    for item in value:
        if not any(canonical_equal(item, existing) for existing in unique):
            unique.append(item)
    return unique


def evaluate_expression(
    expression: dict[str, Any],
    state: dict[str, Any],
    pointer_ledger: list[dict[str, Any]],
) -> tuple[Any, str | None]:
    """Evaluate a normalised expression against ``state``.

    Every pointer lookup is appended to ``pointer_ledger``. Returns ``(value, None)`` on success
    or ``(MISSING, reason)`` when an operand is missing or has the wrong type.
    """
    op = expression["op"]
    if op == "pointer":
        value = resolve_pointer(state, expression["path"])
        pointer_ledger.append(
            {
                "path": expression["path"],
                "resolved": value is not MISSING,
                **({"value": value} if value is not MISSING else {}),
            }
        )
        return value, None if value is not MISSING else "pointer_missing"
    if op == "literal":
        return expression["value"], None
    values = []
    for child in expression.get("args", []):
        value, error = evaluate_expression(child, state, pointer_ledger)
        if error:
            return MISSING, error
        values.append(value)
    if op in _UNARY_OPS:
        value = values[0]
        if op == "not":
            return (not value, None) if isinstance(value, bool) else (MISSING, "type_mismatch")
        if op == "length":
            return (
                (len(value), None)
                if isinstance(value, (str, list, dict))
                else (MISSING, "type_mismatch")
            )
        if op == "abs":
            number = finite_number(value)
            return (abs(number), None) if number is not None else (MISSING, "type_mismatch")
        if op == "normalize_string":
            if not isinstance(value, str) or len(value) > 4096:
                return MISSING, "type_mismatch"
            normalized = " ".join(unicodedata.normalize("NFKC", value).strip().lower().split())
            return normalized, None
        if op == "object_entries":
            if not isinstance(value, dict) or len(value) > 1024:
                return MISSING, "type_or_bound_mismatch"
            return [{"key": key, "value": value[key]} for key in sorted(value)], None
        if op == "is_prime":
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 2 <= value <= 1_000_000_000
            ):
                return MISSING, "type_or_bound_mismatch"
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
                return MISSING, "type_or_bound_mismatch"
            return sum(int(digit) for digit in digits), None
    if op in _NARY_OPS:
        if op in {"and", "or"}:
            if any(not isinstance(value, bool) for value in values):
                return MISSING, "type_mismatch"
            return (all(values) if op == "and" else any(values)), None
        numeric_values = values
        if op in {"min", "max"} and len(values) == 1 and isinstance(values[0], list):
            numeric_values = values[0]
        numbers = [finite_number(value) for value in numeric_values]
        if any(value is None for value in numbers):
            return MISSING, "type_mismatch"
        typed_numbers = [value for value in numbers if value is not None]
        if op == "add":
            result = sum(typed_numbers)
        elif op == "mul":
            result = math.prod(typed_numbers)
        elif op == "min":
            result = min(typed_numbers)
        else:
            result = max(typed_numbers)
        return (result, None) if math.isfinite(result) else (MISSING, "non_finite_result")

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
            if finite_number(value) is not None
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
            return MISSING, "type_mismatch"
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
            or (op == "argclosest" and finite_number(target) is None)
        ):
            return MISSING, "collection_invalid"
        rows: list[tuple[float | int, Any]] = []
        for item in collection:
            if not isinstance(item, dict):
                return MISSING, "collection_invalid"
            scored = resolve_pointer(item, expression["value_pointer"])
            selected = resolve_pointer(item, expression["select_pointer"])
            number = finite_number(scored)
            if scored is MISSING or selected is MISSING or number is None:
                return MISSING, "collection_pointer_invalid"
            score = abs(number - target) if op == "argclosest" else number
            rows.append((score, selected))
        best = (
            max(score for score, _ in rows) if op == "argmax" else min(score for score, _ in rows)
        )
        selected_values = []
        seen: set[str] = set()
        for score, selected in rows:
            key = canonical_json(selected)
            if score == best and key not in seen:
                seen.add(key)
                selected_values.append(selected)
        return selected_values, None

    left = values[0]
    right = values[1] if len(values) > 1 else None
    if op == "at":
        if isinstance(right, bool) or not isinstance(right, int) or right < 0:
            return MISSING, "index_invalid"
        if isinstance(left, str):
            sequence: Any = list(left)
            if len(sequence) > 4096:
                return MISSING, "type_or_bound_mismatch"
        elif isinstance(left, list):
            sequence = left
            if len(sequence) > 4096:
                return MISSING, "type_or_bound_mismatch"
        else:
            return MISSING, "type_mismatch"
        return (
            (sequence[right], None) if right < len(sequence) else (MISSING, "index_out_of_range")
        )
    if op == "eq":
        return canonical_equal(left, right), None
    if op == "neq":
        return not canonical_equal(left, right), None
    if op == "approx_eq":
        numbers = [finite_number(value) for value in values]
        if any(value is None for value in numbers) or numbers[2] < 0:
            return MISSING, "type_mismatch"
        return abs(numbers[0] - numbers[1]) <= numbers[2], None
    if op in {"lt", "lte", "gt", "gte", "sub", "div", "mod", "round"}:
        left_number = finite_number(left)
        right_number = finite_number(right)
        if left_number is None or right_number is None:
            return MISSING, "type_mismatch"
        if op == "lt":
            return left_number < right_number, None
        if op == "lte":
            return left_number <= right_number, None
        if op == "gt":
            return left_number > right_number, None
        if op == "gte":
            return left_number >= right_number, None
        if op in {"div", "mod"} and right_number == 0:
            return MISSING, "division_by_zero"
        if op == "round":
            if not isinstance(right_number, int) or not -12 <= right_number <= 12:
                return MISSING, "round_digits_invalid"
            if right_number >= 0:
                factor = 10**right_number
                result = math.floor((left_number + sys.float_info.epsilon) * factor + 0.5) / factor
            else:
                factor = 10**-right_number
                result = math.floor(left_number / factor + 0.5) * factor
            return (result, None) if math.isfinite(result) else (MISSING, "numeric_overflow")
        result = (
            left_number - right_number
            if op == "sub"
            else left_number / right_number
            if op == "div"
            else math.fmod(left_number, right_number)
        )
        return (result, None) if math.isfinite(result) else (MISSING, "non_finite_result")
    if op in {"contains", "not_contains"}:
        if isinstance(left, str) and isinstance(right, str):
            contains = right in left
        elif isinstance(left, list):
            contains = any(canonical_equal(item, right) for item in left)
        else:
            return MISSING, "type_mismatch"
        return (not contains if op == "not_contains" else contains), None
    if op == "one_of":
        return (
            (any(canonical_equal(left, item) for item in right), None)
            if isinstance(right, list) and len(right) <= 2048
            else (MISSING, "type_mismatch")
        )
    if op in {"set_equals", "set_subset"}:
        left_set = _canonical_set(left)
        right_set = _canonical_set(right)
        if left_set is None or right_set is None:
            return MISSING, "type_mismatch"
        if op == "set_equals":
            return (
                len(left_set) == len(right_set)
                and all(
                    any(canonical_equal(item, other) for other in right_set) for item in left_set
                )
            ), None
        return all(
            any(canonical_equal(item, other) for other in right_set) for item in left_set
        ), None
    if op in {"prefix", "suffix"}:
        if isinstance(left, str) and isinstance(right, str):
            return (left.startswith(right) if op == "prefix" else left.endswith(right)), None
        if isinstance(left, list) and isinstance(right, list):
            if len(left) > 2048 or len(right) > 2048:
                return MISSING, "type_mismatch"
            if len(right) > len(left):
                return False, None
            return (
                all(canonical_equal(a, b) for a, b in zip(left[: len(right)], right, strict=True))
                if op == "prefix"
                else all(
                    canonical_equal(a, b)
                    for a, b in zip(left[len(left) - len(right) :], right, strict=True)
                )
            ), None
        return MISSING, "type_mismatch"
    if op == "ordered_subsequence":
        if isinstance(left, str) and isinstance(right, str):
            left, right = list(left), list(right)
        if (
            not isinstance(left, list)
            or not isinstance(right, list)
            or len(left) > 2048
            or len(right) > 2048
        ):
            return MISSING, "type_mismatch"
        cursor = 0
        for item in left:
            if cursor < len(right) and canonical_equal(item, right[cursor]):
                cursor += 1
        return cursor == len(right), None
    return MISSING, "unsupported_expression"
