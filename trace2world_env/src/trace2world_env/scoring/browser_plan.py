"""Per-task entries of an environment's ``browser_evaluator_plan.json``.

Each entry names the task's evaluator script and declares the checkpoint denominator,
dependencies, weights, terminal solution policy, and the optional runtime-event and
checkpoint-evidence contracts the browser harness enforces. Every field is normalised
defensively: a malformed declaration degrades to "not declared" rather than failing the episode.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLAN_FILE = "browser_evaluator_plan.json"


@dataclass(frozen=True)
class BrowserPlanEntry:
    """Normalised plan declarations for one task."""

    required_checkpoints: list[str]
    diagnostic_checkpoints: list[str]
    terminal_checkpoints: list[str]
    checkpoint_dependencies: dict[str, list[str]]
    checkpoint_weights: dict[str, float]
    zero_weight_assertion_ids: list[str]
    terminal_solution_policy: dict[str, Any]
    runtime_event_contract: dict[str, Any]
    checkpoint_evidence_contract: dict[str, Any]
    source_reference_task_id: str | None

    @classmethod
    def from_entry(cls, evaluator: Any) -> BrowserPlanEntry:
        """Normalise one raw plan entry (any JSON value; non-objects declare nothing)."""
        return cls(
            required_checkpoints=_required_checkpoints(evaluator),
            diagnostic_checkpoints=_diagnostic_checkpoints(evaluator),
            terminal_checkpoints=_terminal_checkpoints(evaluator),
            checkpoint_dependencies=_checkpoint_dependencies(evaluator),
            checkpoint_weights=_checkpoint_weights(evaluator),
            zero_weight_assertion_ids=_zero_weight_assertion_ids(evaluator),
            terminal_solution_policy=_terminal_solution_policy(evaluator),
            runtime_event_contract=_runtime_event_contract(evaluator),
            checkpoint_evidence_contract=_checkpoint_evidence_contract(evaluator),
            source_reference_task_id=_source_reference_task_id(evaluator),
        )


def load_plan_entry(evaluators_dir: Path, task_id: str) -> Any:
    """The raw plan entry of ``task_id``; ``{}`` when the plan file is missing or unreadable."""
    try:
        plan = json.loads((evaluators_dir / PLAN_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        plan = {}
    return plan.get("evaluators", {}).get(task_id, {})


def evaluator_script_path(evaluators_dir: Path, task_id: str, evaluator: Any) -> Path:
    """Resolve the task's evaluator script (``evaluator_file`` or ``<task_id>.js``).

    Raises ``ValueError`` when the configured file escapes ``evaluators_dir``.
    """
    root = evaluators_dir.resolve()
    configured = f"{task_id}.js"
    if isinstance(evaluator, dict):
        candidate = evaluator.get("evaluator_file")
        if isinstance(candidate, str) and candidate.strip():
            configured = candidate.strip()
    resolved = (root / configured).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"configured evaluator escapes evaluator directory: {configured}"
        ) from exc
    return resolved


def _required_checkpoints(evaluator: Any) -> list[str]:
    """Checkpoint denominator: progress-only, blocking-process, then required ids."""
    if not isinstance(evaluator, dict):
        return []
    process = evaluator.get("process_checkpoints_progress_only", [])
    blocking = evaluator.get("blocking_process_checkpoints", [])
    required = evaluator.get("required_checkpoints", [])
    ordered = [
        *(process if isinstance(process, list) else []),
        *(blocking if isinstance(blocking, list) else []),
        *(required if isinstance(required, list) else []),
    ]
    return list(dict.fromkeys(str(value) for value in ordered if value))


def _diagnostic_checkpoints(evaluator: Any) -> list[str]:
    if not isinstance(evaluator, dict):
        return []
    raw = evaluator.get("diagnostic_checkpoints", [])
    return list(
        dict.fromkeys(str(value) for value in raw if value)
    ) if isinstance(raw, list) else []


def _terminal_solution_policy(evaluator: Any) -> dict[str, Any]:
    """The ``terminal_business_state`` policy when it is sourced from the evidence contract."""
    if not isinstance(evaluator, dict):
        return {}
    raw = evaluator.get("terminal_solution_policy", {})
    if not isinstance(raw, dict):
        return {}
    mode = str(raw.get("mode") or "")
    assertion_source = str(raw.get("assertion_source") or "")
    if (
        mode != "terminal_business_state"
        or assertion_source != "checkpoint_evidence_contract"
    ):
        return {}
    return {
        "mode": mode,
        "assertion_source": assertion_source,
        "require_all_required_checkpoints": raw.get(
            "require_all_required_checkpoints"
        ) is not False,
    }


def _terminal_checkpoints(evaluator: Any) -> list[str]:
    """Checkpoints that must hold at the end (explicit list, or blocking + required)."""
    if not isinstance(evaluator, dict):
        return []
    solution_policy = evaluator.get("terminal_solution_policy", {})
    if (
        isinstance(solution_policy, dict)
        and solution_policy.get("mode") == "terminal_business_state"
        and solution_policy.get("assertion_source")
        == "checkpoint_evidence_contract"
        and solution_policy.get("require_all_required_checkpoints") is False
    ):
        explicit = evaluator.get("terminal_checkpoints", [])
        return list(dict.fromkeys(str(value) for value in explicit if value))\
            if isinstance(explicit, list) else []
    required = evaluator.get("required_checkpoints", [])
    blocking = evaluator.get("blocking_process_checkpoints", [])
    ordered = [
        *(blocking if evaluator.get("requires_all_process") is True
          and isinstance(blocking, list) else []),
        *(required if isinstance(required, list) else []),
    ]
    return list(dict.fromkeys(str(value) for value in ordered if value))


def _checkpoint_dependencies(evaluator: Any) -> dict[str, list[str]]:
    if not isinstance(evaluator, dict):
        return {}
    raw = evaluator.get("checkpoint_dependencies", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(checkpoint_id): list(
            dict.fromkeys(str(value) for value in dependencies if value)
        )
        for checkpoint_id, dependencies in raw.items()
        if checkpoint_id and isinstance(dependencies, list)
    }


def _checkpoint_weights(evaluator: Any) -> dict[str, float]:
    """Non-negative finite weights; any invalid weight voids the whole declaration."""
    if not isinstance(evaluator, dict):
        return {}
    raw = evaluator.get("checkpoint_weights", {})
    if not isinstance(raw, dict):
        return {}
    weights: dict[str, float] = {}
    for checkpoint_id, value in raw.items():
        if not checkpoint_id or isinstance(value, bool):
            return {}
        try:
            weight = float(value)
        except (TypeError, ValueError):
            return {}
        if weight < 0 or not (weight < float("inf")):
            return {}
        weights[str(checkpoint_id)] = weight
    return weights


def _zero_weight_assertion_ids(evaluator: Any) -> list[str]:
    if not isinstance(evaluator, dict):
        return []
    raw = evaluator.get("zero_weight_assertion_ids", [])
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        return []
    return list(dict.fromkeys(value for value in raw if value))


def _normalize_common_storage_json_source(raw: Any) -> dict[str, str] | None:
    """Validate a ``{kind, key}`` session/local-storage JSON source declaration."""
    if not isinstance(raw, dict) or set(raw) != {"kind", "key"}:
        return None
    kind = raw.get("kind")
    key = raw.get("key")
    if kind not in {"session_storage_json", "local_storage_json"}:
        return None
    if (
        not isinstance(key, str)
        or not key
        or key != key.strip()
        or len(key) > 256
        or any(ord(character) < 32 for character in key)
    ):
        return None
    return {"kind": kind, "key": key}


def _runtime_event_contract(evaluator: Any) -> dict[str, Any]:
    """Normalise ``runtime_event_contract`` (v2): observation-to-state bindings and the
    optional app-owned snapshot adapter. Returns ``{}`` when anything is malformed.
    """
    if not isinstance(evaluator, dict):
        return {}
    raw = evaluator.get("runtime_event_contract", {})
    if not isinstance(raw, dict):
        return {}
    if raw.get("contract_schema") != "trace2world.runtime-event-contract.v2":
        return {}
    if raw.get("runtime_event_schema_version") != 2:
        return {}
    if raw.get("state_hash_algorithm") != "appstate-fnv1a-v1":
        return {}

    bindings: dict[str, list[dict[str, str]]] = {}
    raw_bindings = raw.get("checkpoint_observation_bindings", {})
    if not isinstance(raw_bindings, dict):
        return {}
    identity_only_payload_roots = {
        "action_id",
        "checkpoint_id",
        "contract_type",
        "event",
        "event_id",
        "event_type",
        "task_id",
        "trace_id",
    }
    for checkpoint_id, rows in raw_bindings.items():
        if not isinstance(checkpoint_id, str) or not checkpoint_id or not isinstance(rows, list):
            return {}
        normalized_rows: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                return {}
            source = row.get("evidence_source")
            evidence_pointer = row.get("evidence_pointer")
            state_pointer = row.get("state_pointer")
            if source not in {"payload", "event"}:
                return {}
            if not isinstance(evidence_pointer, str) or not evidence_pointer.startswith("/"):
                return {}
            if not isinstance(state_pointer, str) or not state_pointer.startswith("/"):
                return {}
            evidence_tokens = [
                token.replace("~1", "/").replace("~0", "~")
                for token in evidence_pointer[1:].split("/")
            ]
            state_tokens = [
                token.replace("~1", "/").replace("~0", "~")
                for token in state_pointer[1:].split("/")
            ]
            evidence_root = evidence_tokens[0]
            if (
                any(token in identity_only_payload_roots for token in state_tokens)
                or any(
                    token in identity_only_payload_roots
                    for token in evidence_tokens
                    if source == "payload" or token != evidence_root
                )
            ):
                return {}
            if source == "event" and evidence_root not in {
                "observation",
                "runtime_observation",
            }:
                return {}
            normalized_rows.append(
                {
                    "evidence_source": source,
                    "evidence_pointer": evidence_pointer,
                    "state_pointer": state_pointer,
                }
            )
        if normalized_rows:
            bindings[checkpoint_id] = normalized_rows

    adapters: dict[str, dict[str, Any]] = {}
    raw_adapters = raw.get("runtime_event_adapters", {})
    if not isinstance(raw_adapters, dict):
        return {}
    adapter = raw_adapters.get("1")
    if adapter is not None:
        if not isinstance(adapter, dict):
            return {}
        snapshot_source = _normalize_common_storage_json_source(
            adapter.get("snapshot_source")
        )
        if (
            set(adapter)
            != {
                "adapter_schema_version",
                "adapter_id",
                "source_kind",
                "snapshot_source",
            }
            or adapter.get("adapter_schema_version") != 1
            or adapter.get("adapter_id") != "app_owned_state_snapshot_v1"
            or adapter.get("source_kind") != "common_owned_runtime_event_envelope"
            or snapshot_source is None
        ):
            return {}
        adapters["1"] = {
            "adapter_schema_version": 1,
            "adapter_id": "app_owned_state_snapshot_v1",
            "source_kind": "common_owned_runtime_event_envelope",
            "snapshot_source": snapshot_source,
        }

    return {
        "contract_schema": "trace2world.runtime-event-contract.v2",
        "runtime_event_schema_version": 2,
        "state_hash_algorithm": "appstate-fnv1a-v1",
        "checkpoint_observation_bindings": bindings,
        "runtime_event_adapters": adapters,
    }


def _checkpoint_evidence_contract(evaluator: Any) -> dict[str, Any]:
    """Normalise ``checkpoint_evidence_contract`` (v1-v3): per-checkpoint assertions over
    the app-owned state snapshot and forbidden conditions. Returns ``{}`` when anything is
    malformed.
    """
    if not isinstance(evaluator, dict):
        return {}
    raw = evaluator.get("checkpoint_evidence_contract", {})
    if not isinstance(raw, dict):
        return {}
    contract_schema = raw.get("contract_schema")
    evidence_schema_versions = {
        "trace2world.checkpoint-evidence-contract.v1": 1,
        "trace2world.checkpoint-evidence-contract.v2": 2,
        "trace2world.checkpoint-evidence-contract.v3": 3,
    }
    evidence_schema_version = evidence_schema_versions.get(contract_schema)
    if evidence_schema_version is None:
        return {}
    if raw.get("evidence_schema_version") != evidence_schema_version:
        return {}
    if raw.get("evidence_root") != "app_owned_state_snapshot":
        return {}
    snapshot_source = _normalize_common_storage_json_source(
        raw.get("snapshot_source")
    )
    if snapshot_source is None:
        return {}
    expression_schema = raw.get("expression_schema")
    if evidence_schema_version >= 3 and expression_schema != (
        "trace2world.bounded-state-expression.v1"
    ):
        return {}

    raw_checkpoints = raw.get("checkpoints")
    if not isinstance(raw_checkpoints, dict) or not raw_checkpoints:
        return {}
    allowed_predicates = {"eq", "exists", "contains", "set_equals"}
    if evidence_schema_version >= 2:
        allowed_predicates.update(
            {
                "numeric_eq",
                "numeric_lte",
                "numeric_gte",
                "not_contains",
                "matches_regex",
            }
        )
    forbidden_actual_roots = {
        "action_id",
        "action_history",
        "action_type",
        "browser_terminal_assertions",
        "checkpoint",
        "checkpoint_first_event_index",
        "checkpoint_event",
        "checkpoint_outcomes",
        "checkpoint_predicates",
        "checkpoint_reward_vector",
        "checkpoint_state",
        "checkpoint_id",
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
        "pass",
        "passed",
        "payload_valid",
        "present",
        "reached",
        "reward",
        "reward_contribution",
        "reward_weight",
        "runtime_history",
        "safety_violation",
        "score",
        "seen",
        "state_observation_valid",
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
        "verdict",
        "vector",
        "verification",
    }

    def normalize_safe_pointer(pointer: Any) -> str | None:
        if (
            not isinstance(pointer, str)
            or not pointer.startswith("/")
            or pointer == "/"
        ):
            return None
        pointer_tokens = [
            token.replace("~1", "/").replace("~0", "~")
            for token in pointer[1:].split("/")
        ]
        if not pointer_tokens or any(
            not token or token in forbidden_actual_roots
            for token in pointer_tokens
        ):
            return None
        return pointer

    def normalize_regex_expected(expected: Any) -> dict[str, str] | None:
        if not isinstance(expected, dict) or set(expected) != {
            "pattern",
            "flags",
        }:
            return None
        pattern = expected.get("pattern")
        flags = expected.get("flags")
        if (
            not isinstance(pattern, str)
            or not 1 <= len(pattern) <= 256
            or not isinstance(flags, str)
            or len(flags) > 3
            or len(set(flags)) != len(flags)
            or any(flag not in "ims" for flag in flags)
            or "(?" in pattern
            or re.search(r"\\[1-9]", pattern)
            or re.search(r"\)[*+?{]", pattern)
            or re.search(r"(?:[*+?]|\})(?:[*+?]|\{)", pattern)
        ):
            return None
        regex_flags = 0
        if "i" in flags:
            regex_flags |= re.IGNORECASE
        if "m" in flags:
            regex_flags |= re.MULTILINE
        if "s" in flags:
            regex_flags |= re.DOTALL
        try:
            compiled_pattern = re.compile(pattern, regex_flags)
        except re.error:
            return None
        if compiled_pattern.search("") is not None:
            return None
        return {"pattern": pattern, "flags": flags}

    def normalize_expression(expression: Any) -> dict[str, Any] | None:
        node_count = 0
        pointer_leaf_count = 0

        def walk(node: Any, depth: int) -> dict[str, Any] | None:
            nonlocal node_count, pointer_leaf_count
            if depth > 8 or not isinstance(node, dict):
                return None
            node_count += 1
            if node_count > 64:
                return None
            op = node.get("op")
            if not isinstance(op, str):
                return None
            if op == "pointer":
                if set(node) != {"op", "path"}:
                    return None
                path = normalize_safe_pointer(node.get("path"))
                if path is None:
                    return None
                pointer_leaf_count += 1
                return {"op": op, "path": path}
            if op == "literal":
                if set(node) != {"op", "value"}:
                    return None
                value = node.get("value")
                try:
                    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
                except (TypeError, ValueError):
                    return None
                if len(encoded.encode("utf-8")) > 8192:
                    return None
                return {"op": op, "value": value}

            unary_ops = {
                "abs",
                "digit_sum",
                "is_prime",
                "length",
                "normalize_string",
                "not",
                "object_entries",
            }
            binary_ops = {
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
                "round",
                "set_equals",
                "set_subset",
                "sub",
                "suffix",
            }
            nary_ops = {"add", "and", "max", "min", "mul", "or"}
            if op in unary_ops | binary_ops | nary_ops | {"approx_eq"}:
                if set(node) != {"op", "args"} or not isinstance(
                    node.get("args"), list
                ):
                    return None
                args = node["args"]
                if op in unary_ops and len(args) != 1:
                    return None
                if op in binary_ops and len(args) != 2:
                    return None
                if op == "approx_eq" and len(args) != 3:
                    return None
                if op in nary_ops and not 1 <= len(args) <= 16:
                    return None
                normalized_args = [walk(arg, depth + 1) for arg in args]
                if any(arg is None for arg in normalized_args):
                    return None
                return {"op": op, "args": normalized_args}

            if op == "type_is":
                if set(node) != {"op", "args", "expected_type"}:
                    return None
                args = node.get("args")
                expected_type = node.get("expected_type")
                if (
                    not isinstance(args, list)
                    or len(args) != 1
                    or expected_type
                    not in {"array", "boolean", "null", "number", "object", "string"}
                ):
                    return None
                normalized_arg = walk(args[0], depth + 1)
                if normalized_arg is None:
                    return None
                return {
                    "op": op,
                    "args": [normalized_arg],
                    "expected_type": expected_type,
                }

            if op == "matches_regex":
                if set(node) != {"op", "args", "pattern", "flags"}:
                    return None
                args = node.get("args")
                if not isinstance(args, list) or len(args) != 1:
                    return None
                normalized_arg = walk(args[0], depth + 1)
                normalized_regex = normalize_regex_expected(
                    {"pattern": node.get("pattern"), "flags": node.get("flags")}
                )
                if normalized_arg is None or normalized_regex is None:
                    return None
                return {"op": op, "args": [normalized_arg], **normalized_regex}

            if op in {"argmin", "argmax", "argclosest"}:
                expected_keys = {
                    "op",
                    "args",
                    "value_pointer",
                    "select_pointer",
                }
                if set(node) != expected_keys:
                    return None
                args = node.get("args")
                required_args = 2 if op == "argclosest" else 1
                if not isinstance(args, list) or len(args) != required_args:
                    return None
                value_pointer = normalize_safe_pointer(node.get("value_pointer"))
                select_pointer = normalize_safe_pointer(node.get("select_pointer"))
                normalized_args = [walk(arg, depth + 1) for arg in args]
                if (
                    value_pointer is None
                    or select_pointer is None
                    or any(arg is None for arg in normalized_args)
                ):
                    return None
                return {
                    "op": op,
                    "args": normalized_args,
                    "value_pointer": value_pointer,
                    "select_pointer": select_pointer,
                }
            return None

        normalized = walk(expression, 1)
        if normalized is None or pointer_leaf_count < 1:
            return None
        return normalized

    def normalize_assertions(
        assertion_owner_id: str,
        assertion_spec: Any,
    ) -> list[dict[str, Any]] | None:
        if (
            not isinstance(assertion_owner_id, str)
            or not assertion_owner_id
            or not isinstance(assertion_spec, dict)
        ):
            return None
        raw_assertions = assertion_spec.get("assertions")
        if not isinstance(raw_assertions, list) or not raw_assertions:
            return None
        assertions: list[dict[str, Any]] = []
        for raw_assertion in raw_assertions:
            if not isinstance(raw_assertion, dict):
                return None
            if evidence_schema_version >= 3:
                if set(raw_assertion) != {"expression"}:
                    return None
                expression = normalize_expression(raw_assertion.get("expression"))
                if expression is None:
                    return None
                assertions.append({"expression": expression})
                continue
            actual_pointer = raw_assertion.get("actual_pointer")
            predicate = raw_assertion.get("predicate")
            if (
                normalize_safe_pointer(actual_pointer) is None
                or predicate not in allowed_predicates
                or "expected" not in raw_assertion
            ):
                return None
            expected = raw_assertion["expected"]
            if predicate == "exists" and not isinstance(expected, bool):
                return None
            if predicate in {"contains", "not_contains"} and (
                expected == "" or expected is None
            ):
                return None
            if predicate in {"numeric_eq", "numeric_lte", "numeric_gte"} and (
                isinstance(expected, bool)
                or not isinstance(expected, (int, float))
                or not math.isfinite(expected)
            ):
                return None
            if predicate == "matches_regex":
                normalized_regex = normalize_regex_expected(expected)
                if normalized_regex is None:
                    return None
                expected = normalized_regex
            try:
                json.dumps(expected, allow_nan=False)
            except (TypeError, ValueError):
                return None
            assertions.append(
                {
                    "actual_pointer": actual_pointer,
                    "predicate": predicate,
                    "expected": expected,
                }
            )
        return assertions

    checkpoints: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for checkpoint_id, checkpoint_spec in raw_checkpoints.items():
        assertions = normalize_assertions(checkpoint_id, checkpoint_spec)
        if assertions is None:
            return {}
        checkpoints[checkpoint_id] = {"assertions": assertions}

    raw_forbidden_conditions = raw.get("forbidden_conditions", [])
    if not isinstance(raw_forbidden_conditions, list):
        return {}
    forbidden_conditions: list[dict[str, Any]] = []
    forbidden_condition_ids: set[str] = set()
    for condition_spec in raw_forbidden_conditions:
        if not isinstance(condition_spec, dict):
            return {}
        condition_id = condition_spec.get("id")
        assertions = normalize_assertions(condition_id, condition_spec)
        if assertions is None or condition_id in forbidden_condition_ids:
            return {}
        forbidden_condition_ids.add(condition_id)
        forbidden_conditions.append(
            {"id": condition_id, "assertions": assertions}
        )

    normalized_contract = {
        "contract_schema": contract_schema,
        "evidence_schema_version": evidence_schema_version,
        "evidence_root": "app_owned_state_snapshot",
        "snapshot_source": snapshot_source,
        "checkpoints": checkpoints,
        "forbidden_conditions": forbidden_conditions,
    }
    if evidence_schema_version >= 3:
        normalized_contract["expression_schema"] = expression_schema
    return normalized_contract


def _source_reference_task_id(evaluator: Any) -> str | None:
    if not isinstance(evaluator, dict):
        return None
    source_reference_task_id = evaluator.get("source_reference_task_id")
    if (
        not isinstance(source_reference_task_id, str)
        or not source_reference_task_id
    ):
        return None
    return source_reference_task_id
