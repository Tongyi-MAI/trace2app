
import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from trace2world.env.evaluator_contract import (
    validate_browser_evaluator_source,
)
from trace2world.schema.evaluation import EnvironmentEvaluation

logger = logging.getLogger(__name__)


def _configured_evaluator_path(
    *,
    task_id: str,
    evaluators_dir: str,
) -> Path:
    root = Path(evaluators_dir).resolve()
    configured = f"{task_id}.js"
    plan_path = root / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        plan = {}
    entry = plan.get("evaluators", {}).get(task_id, {})
    if isinstance(entry, dict):
        candidate = entry.get("evaluator_file")
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


def _configured_required_checkpoints(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> list[str]:
    if not task_id or not evaluators_dir:
        return []
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    evaluator = plan.get("evaluators", {}).get(task_id, {})
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


def _configured_diagnostic_checkpoints(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> list[str]:
    if not task_id or not evaluators_dir:
        return []
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    evaluator = plan.get("evaluators", {}).get(task_id, {})
    if not isinstance(evaluator, dict):
        return []
    raw = evaluator.get("diagnostic_checkpoints", [])
    return list(
        dict.fromkeys(str(value) for value in raw if value)
    ) if isinstance(raw, list) else []


def _configured_terminal_solution_policy(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> dict[str, Any]:
    if not task_id or not evaluators_dir:
        return {}
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    evaluator = plan.get("evaluators", {}).get(task_id, {})
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


def _configured_terminal_checkpoints(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> list[str]:
    if not task_id or not evaluators_dir:
        return []
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    evaluator = plan.get("evaluators", {}).get(task_id, {})
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


def _configured_checkpoint_dependencies(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> dict[str, list[str]]:
    if not task_id or not evaluators_dir:
        return {}
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    evaluator = plan.get("evaluators", {}).get(task_id, {})
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


def _configured_checkpoint_weights(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> dict[str, float]:
    if not task_id or not evaluators_dir:
        return {}
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    evaluator = plan.get("evaluators", {}).get(task_id, {})
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


def _configured_zero_weight_assertion_ids(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> list[str]:
    if not task_id or not evaluators_dir:
        return []
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    evaluator = plan.get("evaluators", {}).get(task_id, {})
    if not isinstance(evaluator, dict):
        return []
    raw = evaluator.get("zero_weight_assertion_ids", [])
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        return []
    return list(dict.fromkeys(value for value in raw if value))


def _normalize_common_storage_json_source(raw: Any) -> dict[str, str] | None:
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


def _configured_runtime_event_contract(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> dict[str, Any]:
    if not task_id or not evaluators_dir:
        return {}
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    evaluator = plan.get("evaluators", {}).get(task_id, {})
    if not isinstance(evaluator, dict):
        return {}
    raw = evaluator.get("runtime_event_contract", {})
    if not isinstance(raw, dict):
        return {}
    if raw.get("contract_schema") != "agentforge.webenv.runtime-event-contract.v2":
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
        "contract_schema": "agentforge.webenv.runtime-event-contract.v2",
        "runtime_event_schema_version": 2,
        "state_hash_algorithm": "appstate-fnv1a-v1",
        "checkpoint_observation_bindings": bindings,
        "runtime_event_adapters": adapters,
    }


def _configured_checkpoint_evidence_contract(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> dict[str, Any]:
    if not task_id or not evaluators_dir:
        return {}
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    evaluator = plan.get("evaluators", {}).get(task_id, {})
    if not isinstance(evaluator, dict):
        return {}
    raw = evaluator.get("checkpoint_evidence_contract", {})
    if not isinstance(raw, dict):
        return {}
    contract_schema = raw.get("contract_schema")
    evidence_schema_versions = {
        "agentforge.webenv.checkpoint-evidence-contract.v1": 1,
        "agentforge.webenv.checkpoint-evidence-contract.v2": 2,
        "agentforge.webenv.checkpoint-evidence-contract.v3": 3,
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
        "agentforge.webenv.bounded-state-expression.v1"
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


def _configured_source_reference_task_id(
    *,
    task_id: str | None,
    evaluators_dir: str | None,
) -> str | None:
    if not task_id or not evaluators_dir:
        return None
    plan_path = Path(evaluators_dir) / "browser_evaluator_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    evaluator = plan.get("evaluators", {}).get(task_id, {})
    if not isinstance(evaluator, dict):
        return None
    source_reference_task_id = evaluator.get("source_reference_task_id")
    if (
        not isinstance(source_reference_task_id, str)
        or not source_reference_task_id
    ):
        return None
    return source_reference_task_id


async def run_evaluator(
    page: Page,
    *,
    task_id: str | None = None,
    evaluator_js: str | None = None,
    evaluators_dir: str | None = None,
) -> float:
    result = await run_evaluator_result(
        page,
        task_id=task_id,
        evaluator_js=evaluator_js,
        evaluators_dir=evaluators_dir,
    )
    return result.raw_reward or 0.0


async def run_evaluator_result(
    page: Page,
    *,
    task_id: str | None = None,
    evaluator_js: str | None = None,
    evaluators_dir: str | None = None,
) -> EnvironmentEvaluation:
    js_code = evaluator_js

    eval_path: Path | None = None
    if js_code is None and task_id and evaluators_dir:
        try:
            eval_path = _configured_evaluator_path(
                task_id=task_id, evaluators_dir=evaluators_dir
            )
        except ValueError as exc:
            logger.warning("[Evaluator] Invalid evaluator mapping for task %s: %s", task_id, exc)
            return EnvironmentEvaluation(
                raw_reward=0.0,
                reason="invalid_evaluator_mapping",
                terminal_success=False,
                env_valid=False,
                format_valid=False,
            )
        if eval_path.exists():
            js_code = eval_path.read_text(encoding="utf-8")
        else:
            logger.warning("[Evaluator] No evaluator file for task %s at %s", task_id, eval_path)
            return EnvironmentEvaluation(
                raw_reward=0.0,
                reason="missing_evaluator_file",
                terminal_success=False,
                env_valid=False,
                format_valid=False,
            )

    if js_code is None:
        logger.warning("[Evaluator] No evaluator JS provided")
        return EnvironmentEvaluation(
            raw_reward=0.0,
            reason="missing_evaluator_source",
            terminal_success=False,
            env_valid=False,
            format_valid=False,
        )

    try:
        validate_browser_evaluator_source(
            js_code,
            evaluator_path=eval_path or "<inline>",
        )
        configured_required = _configured_required_checkpoints(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        configured_diagnostic = _configured_diagnostic_checkpoints(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        configured_terminal = _configured_terminal_checkpoints(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        configured_dependencies = _configured_checkpoint_dependencies(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        configured_weights = _configured_checkpoint_weights(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        configured_zero_weight_assertion_ids = (
            _configured_zero_weight_assertion_ids(
                task_id=task_id,
                evaluators_dir=evaluators_dir,
            )
        )
        configured_solution_policy = _configured_terminal_solution_policy(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        configured_runtime_event_contract = _configured_runtime_event_contract(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        configured_checkpoint_evidence_contract = (
            _configured_checkpoint_evidence_contract(
                task_id=task_id,
                evaluators_dir=evaluators_dir,
            )
        )
        configured_source_reference_task_id = _configured_source_reference_task_id(
            task_id=task_id,
            evaluators_dir=evaluators_dir,
        )
        required_json = json.dumps(configured_required, ensure_ascii=False)
        diagnostic_json = json.dumps(configured_diagnostic, ensure_ascii=False)
        terminal_json = json.dumps(configured_terminal, ensure_ascii=False)
        dependencies_json = json.dumps(configured_dependencies, ensure_ascii=False)
        weights_json = json.dumps(configured_weights, ensure_ascii=False)
        zero_weight_assertion_ids_json = json.dumps(
            configured_zero_weight_assertion_ids,
            ensure_ascii=False,
        )
        solution_policy_json = json.dumps(
            configured_solution_policy, ensure_ascii=False
        )
        runtime_event_contract_json = json.dumps(
            configured_runtime_event_contract, ensure_ascii=False
        )
        checkpoint_evidence_contract_json = json.dumps(
            configured_checkpoint_evidence_contract, ensure_ascii=False
        )
        task_id_json = json.dumps(task_id or "", ensure_ascii=False)
        source_reference_task_id_json = json.dumps(
            configured_source_reference_task_id or "", ensure_ascii=False
        )
        runtime_event_validation_js = """
    const isPlainRecord = (value) => Boolean(value)
      && typeof value === 'object' && !Array.isArray(value);
    const canonicalClone = (value) => JSON.parse(JSON.stringify(value));
    const canonicalStable = (value) => {
      if (value === null || value === undefined) return JSON.stringify(value);
      if (Array.isArray(value)) return '[' + value.map(canonicalStable).join(',') + ']';
      if (typeof value === 'object') return '{' + Object.keys(value).sort()
        .map((key) => JSON.stringify(key) + ':' + canonicalStable(value[key])).join(',') + '}';
      return JSON.stringify(value);
    };
    const canonicalStateHash = (value) => {
      const text = canonicalStable(value);
      let first = 2166136261 >>> 0;
      let second = 2246822519 >>> 0;
      for (let index = 0; index < text.length; index += 1) {
        const code = text.charCodeAt(index);
        first = Math.imul(first ^ code, 16777619) >>> 0;
        second = Math.imul(
          second ^ (code + ((index & 255) << 8)), 3266489917
        ) >>> 0;
      }
      const hex = (number) => ('00000000' + (number >>> 0).toString(16)).slice(-8);
      return 'appstate-fnv1a-v1:' + hex(first) + hex(second);
    };
    const canonicalEqual = (left, right) =>
      canonicalStable(left) === canonicalStable(right);
    const pointerTokens = (pointer) => {
      if (typeof pointer !== 'string' || !pointer.startsWith('/')) return null;
      return pointer.slice(1).split('/').map((token) =>
        token.replace(/~1/g, '/').replace(/~0/g, '~'));
    };
    const resolvePointer = (root, pointer) => {
      const tokens = pointerTokens(pointer);
      if (!tokens || !tokens.length) return {found: false, value: null, tokens: []};
      let current = root;
      for (const token of tokens) {
        if ((isPlainRecord(current) || Array.isArray(current))
            && Object.prototype.hasOwnProperty.call(current, token)) {
          current = current[token];
        } else {
          return {found: false, value: null, tokens};
        }
      }
      return {found: true, value: current, tokens};
    };
    const canonicalSet = (value) => Array.isArray(value)
      ? Array.from(new Set(value.map(canonicalStable))).sort() : null;
    const typedContains = (actual, expected) => {
      if (typeof actual === 'string' && typeof expected === 'string') {
        return {typed: true, contains: actual.includes(expected)};
      }
      if (Array.isArray(actual)) {
        return {
          typed: true,
          contains: actual.some((value) => canonicalEqual(value, expected)),
        };
      }
      return {typed: false, contains: false};
    };
    const compilePlanRegex = (expected) => {
      if (!isPlainRecord(expected)
          || typeof expected.pattern !== 'string'
          || typeof expected.flags !== 'string') return null;
      try {
        return new RegExp(expected.pattern, expected.flags);
      } catch (_) {
        return null;
      }
    };
    const validateEvidencePredicate = (resolved, predicate, expected) => {
      if (predicate === 'exists') {
        return resolved.found === expected;
      }
      if (!resolved.found) return false;
      if (predicate === 'eq') return canonicalEqual(resolved.value, expected);
      if (predicate === 'contains') {
        const result = typedContains(resolved.value, expected);
        return result.typed && result.contains;
      }
      if (predicate === 'not_contains') {
        const result = typedContains(resolved.value, expected);
        return result.typed && !result.contains;
      }
      if (['numeric_eq', 'numeric_lte', 'numeric_gte'].includes(predicate)) {
        if (typeof resolved.value !== 'number' || !Number.isFinite(resolved.value)
            || typeof expected !== 'number' || !Number.isFinite(expected)) {
          return false;
        }
        if (predicate === 'numeric_eq') return resolved.value === expected;
        if (predicate === 'numeric_lte') return resolved.value <= expected;
        return resolved.value >= expected;
      }
      if (predicate === 'matches_regex') {
        if (typeof resolved.value !== 'string' || resolved.value.length > 4096) {
          return false;
        }
        const compiled = compilePlanRegex(expected);
        return compiled !== null && compiled.test(resolved.value);
      }
      if (predicate === 'set_equals') {
        const actualSet = canonicalSet(resolved.value);
        const expectedSet = canonicalSet(expected);
        return actualSet !== null && expectedSet !== null
          && canonicalEqual(actualSet, expectedSet);
      }
      return false;
    };
    const finiteNumber = (value) =>
      typeof value === 'number' && Number.isFinite(value);
    const evaluateStateExpression = (expression, root, budget) => {
      const activeBudget = budget || {remaining: 64};
      activeBudget.remaining -= 1;
      const invalid = (reason) => ({ok: false, value: null, reason});
      const valid = (value) => ({ok: true, value, reason: null});
      if (activeBudget.remaining < 0 || !isPlainRecord(expression)) {
        return invalid('expression_budget_or_shape_invalid');
      }
      const op = String(expression.op || '');
      if (op === 'pointer') {
        const resolved = resolvePointer(root, expression.path);
        return resolved.found
          ? valid(resolved.value) : invalid('expression_pointer_missing');
      }
      if (op === 'literal') return valid(expression.value);
      const args = Array.isArray(expression.args) ? expression.args : [];
      const evaluatedArgs = args.map((arg) =>
        evaluateStateExpression(arg, root, activeBudget));
      if (evaluatedArgs.some((row) => !row.ok)) {
        return invalid(evaluatedArgs.find((row) => !row.ok).reason);
      }
      const values = evaluatedArgs.map((row) => row.value);
      const allNumbers = values.every(finiteNumber);
      if (op === 'not') {
        return typeof values[0] === 'boolean'
          ? valid(!values[0]) : invalid('expression_type_mismatch');
      }
      if (op === 'object_entries') {
        if (!isPlainRecord(values[0]) || Object.keys(values[0]).length > 1024) {
          return invalid('expression_type_or_bound_mismatch');
        }
        return valid(Object.keys(values[0]).sort().map((key) => ({
          key, value: values[0][key],
        })));
      }
      if (op === 'at') {
        const collection = typeof values[0] === 'string'
          ? Array.from(values[0]) : values[0];
        const index = values[1];
        if (!Array.isArray(collection) || collection.length > 4096
            || !Number.isSafeInteger(index) || index < 0
            || index >= collection.length) {
          return invalid('expression_index_or_bound_invalid');
        }
        return valid(collection[index]);
      }
      if (op === 'and' || op === 'or') {
        if (!values.length || !values.every((value) => typeof value === 'boolean')) {
          return invalid('expression_type_mismatch');
        }
        return valid(op === 'and'
          ? values.every(Boolean) : values.some(Boolean));
      }
      if (op === 'eq' || op === 'neq') {
        const equal = canonicalEqual(values[0], values[1]);
        return valid(op === 'eq' ? equal : !equal);
      }
      if (['lt', 'lte', 'gt', 'gte'].includes(op)) {
        if (!allNumbers) return invalid('expression_type_mismatch');
        if (op === 'lt') return valid(values[0] < values[1]);
        if (op === 'lte') return valid(values[0] <= values[1]);
        if (op === 'gt') return valid(values[0] > values[1]);
        return valid(values[0] >= values[1]);
      }
      if (op === 'approx_eq') {
        if (!allNumbers || values[2] < 0) {
          return invalid('expression_type_mismatch');
        }
        return valid(Math.abs(values[0] - values[1]) <= values[2]);
      }
      if (op === 'contains' || op === 'not_contains') {
        const result = typedContains(values[0], values[1]);
        if (!result.typed) return invalid('expression_type_mismatch');
        return valid(op === 'contains' ? result.contains : !result.contains);
      }
      if (op === 'one_of') {
        if (!Array.isArray(values[1]) || values[1].length > 2048) {
          return invalid('expression_type_mismatch');
        }
        return valid(values[1].some((row) => canonicalEqual(values[0], row)));
      }
      if (op === 'set_equals' || op === 'set_subset') {
        const left = canonicalSet(values[0]);
        const right = canonicalSet(values[1]);
        if (left === null || right === null
            || values[0].length > 2048 || values[1].length > 2048) {
          return invalid('expression_type_mismatch');
        }
        if (op === 'set_equals') return valid(canonicalEqual(left, right));
        const rightSet = new Set(right);
        return valid(left.every((value) => rightSet.has(value)));
      }
      if (op === 'prefix' || op === 'suffix') {
        if (typeof values[0] === 'string' && typeof values[1] === 'string') {
          return valid(op === 'prefix'
            ? values[0].startsWith(values[1]) : values[0].endsWith(values[1]));
        }
        if (Array.isArray(values[0]) && Array.isArray(values[1])
            && values[0].length <= 2048 && values[1].length <= 2048) {
          if (values[1].length > values[0].length) return valid(false);
          const offset = op === 'prefix' ? 0 : values[0].length - values[1].length;
          return valid(values[1].every((value, index) =>
            canonicalEqual(values[0][offset + index], value)));
        }
        return invalid('expression_type_mismatch');
      }
      if (op === 'ordered_subsequence') {
        let sequence = values[0];
        let subsequence = values[1];
        if (typeof sequence === 'string' && typeof subsequence === 'string') {
          sequence = Array.from(sequence); subsequence = Array.from(subsequence);
        }
        if (!Array.isArray(sequence) || !Array.isArray(subsequence)
            || sequence.length > 2048 || subsequence.length > 2048) {
          return invalid('expression_type_mismatch');
        }
        let cursor = 0;
        for (const value of sequence) {
          if (cursor < subsequence.length
              && canonicalEqual(value, subsequence[cursor])) cursor += 1;
        }
        return valid(cursor === subsequence.length);
      }
      if (op === 'length') {
        const value = values[0];
        if (typeof value === 'string') return valid(Array.from(value).length);
        if (Array.isArray(value)) return valid(value.length);
        if (isPlainRecord(value)) return valid(Object.keys(value).length);
        return invalid('expression_type_mismatch');
      }
      if (op === 'type_is') {
        const value = values[0];
        const actualType = value === null ? 'null'
          : (Array.isArray(value) ? 'array' : typeof value);
        const typeValid = expression.expected_type !== 'number'
          || finiteNumber(value);
        return valid(typeValid && actualType === expression.expected_type);
      }
      if (op === 'normalize_string') {
        if (typeof values[0] !== 'string' || values[0].length > 4096) {
          return invalid('expression_type_mismatch');
        }
        return valid(values[0].normalize('NFKC').trim().toLowerCase()
          .replace(/\\s+/gu, ' '));
      }
      if (op === 'matches_regex') {
        if (typeof values[0] !== 'string' || values[0].length > 4096) {
          return invalid('expression_type_mismatch');
        }
        const regex = compilePlanRegex(expression);
        return regex ? valid(regex.test(values[0]))
          : invalid('expression_regex_compile_failed');
      }
      if (['add', 'sub', 'mul', 'div', 'mod', 'abs', 'min', 'max', 'round']
          .includes(op)) {
        let numericValues = values;
        if ((op === 'min' || op === 'max') && values.length === 1
            && Array.isArray(values[0])) numericValues = values[0];
        if (!numericValues.length || !numericValues.every(finiteNumber)) {
          return invalid('expression_type_mismatch');
        }
        if (op === 'add') return valid(numericValues.reduce((a, b) => a + b, 0));
        if (op === 'mul') return valid(numericValues.reduce((a, b) => a * b, 1));
        if (op === 'sub') return valid(numericValues[0] - numericValues[1]);
        if (op === 'div') return numericValues[1] !== 0
          ? valid(numericValues[0] / numericValues[1])
          : invalid('expression_division_by_zero');
        if (op === 'mod') return numericValues[1] !== 0
          ? valid(numericValues[0] % numericValues[1])
          : invalid('expression_division_by_zero');
        if (op === 'abs') return valid(Math.abs(numericValues[0]));
        if (op === 'min') return valid(Math.min(...numericValues));
        if (op === 'max') return valid(Math.max(...numericValues));
        const digits = numericValues[1];
        if (!Number.isInteger(digits) || digits < -12 || digits > 12) {
          return invalid('expression_round_digits_invalid');
        }
        const factor = 10 ** digits;
        const rounded = digits >= 0
          ? Math.round((numericValues[0] + Number.EPSILON) * factor) / factor
          : Math.round(numericValues[0] / (10 ** -digits)) * (10 ** -digits);
        return finiteNumber(rounded) ? valid(rounded)
          : invalid('expression_numeric_overflow');
      }
      if (op === 'is_prime') {
        const value = values[0];
        if (!Number.isSafeInteger(value) || value < 2 || value > 1000000000) {
          return invalid('expression_type_or_bound_mismatch');
        }
        if (value % 2 === 0) return valid(value === 2);
        for (let divisor = 3; divisor * divisor <= value; divisor += 2) {
          if (value % divisor === 0) return valid(false);
        }
        return valid(true);
      }
      if (op === 'digit_sum') {
        let digits = null;
        if (Number.isSafeInteger(values[0])) digits = String(Math.abs(values[0]));
        if (typeof values[0] === 'string' && /^[0-9]+$/.test(values[0])
            && values[0].length <= 256) digits = values[0];
        return digits === null ? invalid('expression_type_or_bound_mismatch')
          : valid(Array.from(digits).reduce((sum, digit) => sum + Number(digit), 0));
      }
      if (['argmin', 'argmax', 'argclosest'].includes(op)) {
        const collection = values[0];
        const target = op === 'argclosest' ? values[1] : null;
        if (!Array.isArray(collection) || !collection.length
            || collection.length > 1024
            || (op === 'argclosest' && !finiteNumber(target))) {
          return invalid('expression_collection_invalid');
        }
        const rows = [];
        for (const item of collection) {
          if (!isPlainRecord(item)) return invalid('expression_collection_invalid');
          const scored = resolvePointer(item, expression.value_pointer);
          const selected = resolvePointer(item, expression.select_pointer);
          if (!scored.found || !selected.found || !finiteNumber(scored.value)) {
            return invalid('expression_collection_pointer_invalid');
          }
          const score = op === 'argclosest'
            ? Math.abs(scored.value - target) : scored.value;
          rows.push({score, selected: selected.value});
        }
        const best = op === 'argmax'
          ? Math.max(...rows.map((row) => row.score))
          : Math.min(...rows.map((row) => row.score));
        const selectedValues = [];
        const seenValues = new Set();
        rows.filter((row) => row.score === best).forEach((row) => {
          const key = canonicalStable(row.selected);
          if (!seenValues.has(key)) {
            seenValues.add(key); selectedValues.push(row.selected);
          }
        });
        return valid(selectedValues);
      }
      return invalid('expression_operator_unsupported');
    };
    const deriveTopLevelStateDiff = (before, after) => {
      if (!isPlainRecord(before) || !isPlainRecord(after)) return null;
      const diff = {};
      const keys = Array.from(new Set([
        ...Object.keys(before), ...Object.keys(after)
      ])).sort();
      for (const key of keys) {
        if (!Object.prototype.hasOwnProperty.call(before, key)
            || !Object.prototype.hasOwnProperty.call(after, key)) return null;
        if (!canonicalEqual(before[key], after[key])) {
          diff[key] = {
            before: canonicalClone(before[key]),
            after: canonicalClone(after[key]),
          };
        }
      }
      return diff;
    };
    const validateAndApplyStateDiff = (before, after, stateDiff) => {
      const reasons = [];
      if (!isPlainRecord(stateDiff)) {
        return {valid: false, reasons: ['invalid_state_diff'], applied: null};
      }
      const keys = Object.keys(stateDiff);
      if (!keys.length) {
        return {valid: false, reasons: ['empty_state_diff'], applied: null};
      }
      const applied = canonicalClone(before);
      for (const key of keys) {
        const row = stateDiff[key];
        if (!isPlainRecord(row)
            || !Object.prototype.hasOwnProperty.call(row, 'before')
            || !Object.prototype.hasOwnProperty.call(row, 'after')
            || !Object.prototype.hasOwnProperty.call(before, key)
            || !Object.prototype.hasOwnProperty.call(after, key)) {
          reasons.push('state_diff_not_applicable');
          continue;
        }
        if (!canonicalEqual(before[key], row.before)) {
          reasons.push('state_diff_before_mismatch');
        }
        if (canonicalEqual(row.before, row.after)) {
          reasons.push('state_diff_noop');
        }
        applied[key] = canonicalClone(row.after);
      }
      if (!canonicalEqual(applied, after)) reasons.push('state_diff_after_mismatch');
      return {
        valid: reasons.length === 0,
        reasons: Array.from(new Set(reasons)),
        applied,
      };
    };
    const validateCheckpointObservation = (event, checkpointId, after, stateDiff) => {
      const configuredBindings = configuredRuntimeEventContract
        && configuredRuntimeEventContract.checkpoint_observation_bindings;
      const bindings = configuredBindings
        && Array.isArray(configuredBindings[checkpointId])
        ? configuredBindings[checkpointId] : [];
      if (!bindings.length) {
        return {valid: false, reasons: ['missing_checkpoint_observation_binding']};
      }
      const payload = isPlainRecord(event && event.payload) ? event.payload : {};
      const valid = bindings.some((binding) => {
        const evidenceRoot = binding.evidence_source === 'event' ? event : payload;
        const evidence = resolvePointer(evidenceRoot, binding.evidence_pointer);
        const state = resolvePointer(after, binding.state_pointer);
        const topLevelStateKey = state.tokens.length ? state.tokens[0] : '';
        return evidence.found && state.found
          && Object.prototype.hasOwnProperty.call(stateDiff, topLevelStateKey)
          && canonicalEqual(evidence.value, state.value);
      });
      return valid
        ? {valid: true, reasons: []}
        : {valid: false, reasons: ['checkpoint_observation_binding_mismatch']};
    };
    const runtimeEventSchemaVersion = (event) => {
      const payload = isPlainRecord(event && event.payload) ? event.payload : {};
      const envelopeDeclared = Object.prototype.hasOwnProperty.call(
        event || {}, 'runtime_event_schema_version');
      const payloadDeclared = Object.prototype.hasOwnProperty.call(
        payload, 'runtime_event_schema_version');
      const envelope = envelopeDeclared
        ? Number(event.runtime_event_schema_version) : null;
      const payloadVersion = payloadDeclared
        ? Number(payload.runtime_event_schema_version) : null;
      if (envelopeDeclared && payloadDeclared && envelope !== payloadVersion) {
        return {version: null, declared: true, conflict: true};
      }
      const selected = envelopeDeclared ? envelope
        : (payloadDeclared ? payloadVersion : 1);
      return {
        version: Number.isInteger(selected) && selected > 0 ? selected : null,
        declared: envelopeDeclared || payloadDeclared,
        conflict: false,
      };
    };
    const validateRuntimeEvent = (event, eventIndex) => {
      const checkpointId = eventCheckpointId(event);
      const version = runtimeEventSchemaVersion(event);
      const base = {
        event_index: eventIndex,
        checkpoint_id: checkpointId,
        input_runtime_event_schema_version: version.version,
        normalized_runtime_event_schema_version: null,
        version_declared: version.declared,
        adapter_id: null,
        evidence_source: 'runtime_event_log',
        status: 'invalid_event',
        reasons: [],
      };
      if (version.conflict) {
        base.reasons = ['runtime_event_schema_version_conflict'];
        return base;
      }
      if (version.version === null) {
        base.reasons = ['invalid_runtime_event_schema_version'];
        return base;
      }
      if (version.version !== 1 && version.version !== 2) {
        base.status = 'producer_schema_incompatible';
        base.reasons = ['unsupported_runtime_event_schema_version'];
        return base;
      }
      const legacyAdapter = version.version === 1
        ? (configuredRuntimeEventContract
          && configuredRuntimeEventContract.runtime_event_adapters
          && configuredRuntimeEventContract.runtime_event_adapters['1'])
        : null;
      if (version.version === 1 && !legacyAdapter) {
        base.status = 'producer_schema_incompatible';
        base.reasons = ['missing_v1_adapter'];
        return base;
      }
      let formalEvent = event;
      if (version.version === 1) {
        if (!v1AdapterEnvelopeStable || !isPlainRecord(commonV1AdapterEnvelope)) {
          base.status = 'producer_schema_incompatible';
          const sourceStatus = v1AdapterEnvelopeBefore.status !== 'valid'
            ? v1AdapterEnvelopeBefore.status : v1AdapterEnvelopeAfter.status;
          base.reasons = [
            v1AdapterEnvelopeBefore.status === 'valid'
              && v1AdapterEnvelopeAfter.status === 'valid'
              ? 'evaluator_side_effect'
              : 'v1_adapter_snapshot_source_' + sourceStatus,
          ];
          return base;
        }
        const adapterEvents = Array.isArray(commonV1AdapterEnvelope.events)
          ? commonV1AdapterEnvelope.events : [];
        const candidates = adapterEvents.filter((candidate) =>
          eventCheckpointId(candidate) === checkpointId
          && validateEventIdentity(candidate).valid
          && runtimeEventSchemaVersion(candidate).version === 1);
        if (candidates.length !== 1) {
          base.status = 'producer_schema_incompatible';
          base.reasons = [candidates.length
            ? 'ambiguous_v1_adapter_event' : 'missing_v1_adapter_event'];
          return base;
        }
        formalEvent = candidates[0];
        base.evidence_source = 'common_owned_runtime_event_envelope';
      }
      const payload = isPlainRecord(formalEvent && formalEvent.payload)
        ? formalEvent.payload : null;
      if (!payload || !checkpointId
          || String(payload.checkpoint_id || '') !== checkpointId) {
        base.reasons = ['invalid_checkpoint_payload_identity'];
        return base;
      }
      const before = formalEvent && formalEvent.state_snapshot_before;
      const after = formalEvent && formalEvent.state_snapshot_after;
      if (!isPlainRecord(before) || !isPlainRecord(after)) {
        if (version.version === 1) base.status = 'producer_schema_incompatible';
        base.reasons = [version.version === 1
          ? 'v1_adapter_missing_app_owned_state_snapshot'
          : 'missing_state_snapshot'];
        return base;
      }
      if (version.version === 1) {
        const normalizedDiff = deriveTopLevelStateDiff(before, after);
        if (!normalizedDiff || !Object.keys(normalizedDiff).length) {
          base.reasons = ['empty_normalized_state_diff'];
          return base;
        }
        const observation = validateCheckpointObservation(
          formalEvent, checkpointId, after, normalizedDiff
        );
        if (!observation.valid) {
          base.reasons = observation.reasons;
          return base;
        }
        base.normalized_runtime_event_schema_version = 2;
        base.adapter_id = legacyAdapter.adapter_id;
        base.state_before_hash_recomputed = canonicalStateHash(before);
        base.state_after_hash_recomputed = canonicalStateHash(after);
        base.status = 'valid';
        return base;
      }
      base.normalized_runtime_event_schema_version = 2;
      const reasons = [];
      if (String(event.state_hash_algorithm || '') !== 'appstate-fnv1a-v1') {
        reasons.push('unsupported_state_hash_algorithm');
      }
      const beforeHash = canonicalStateHash(before);
      const afterHash = canonicalStateHash(after);
      base.state_before_hash_recomputed = beforeHash;
      base.state_after_hash_recomputed = afterHash;
      if (String(event.state_before_hash || '') !== beforeHash) {
        reasons.push('state_before_hash_mismatch');
      }
      if (String(event.state_after_hash || '') !== afterHash) {
        reasons.push('state_after_hash_mismatch');
      }
      const diffValidation = validateAndApplyStateDiff(
        before, after, event && event.state_diff
      );
      reasons.push(...diffValidation.reasons);
      if (diffValidation.valid) {
        const observation = validateCheckpointObservation(
          event, checkpointId, after, event.state_diff
        );
        reasons.push(...observation.reasons);
      }
      base.reasons = Array.from(new Set(reasons));
      if (!base.reasons.length) base.status = 'valid';
      return base;
    };
"""


        wrapped = (
            "(() => {\n"
            f"  const configuredRuntimeEventContract = {runtime_event_contract_json};\n"
            "  const configuredCheckpointEvidenceContract = "
            f"{checkpoint_evidence_contract_json};\n"
            "  const readCommonStorageJsonSource = (source) => {\n"
            "    if (!source || typeof source !== 'object') {\n"
            "      return {status: 'not_declared', raw: null, snapshot: null};\n"
            "    }\n"
            "    const storage = source.kind === 'session_storage_json'\n"
            "      ? window.sessionStorage\n"
            "      : (source.kind === 'local_storage_json'\n"
            "        ? window.localStorage : null);\n"
            "    if (!storage || typeof storage.getItem !== 'function') {\n"
            "      return {status: 'source_unavailable', raw: null, snapshot: null};\n"
            "    }\n"
            "    let raw = null;\n"
            "    try { raw = storage.getItem(source.key); } catch (_) {\n"
            "      return {status: 'read_error', raw: null, snapshot: null};\n"
            "    }\n"
            "    if (typeof raw !== 'string') {\n"
            "      return {status: 'missing', raw: null, snapshot: null};\n"
            "    }\n"
            "    let parsed = null;\n"
            "    try { parsed = JSON.parse(raw); } catch (_) {\n"
            "      return {status: 'parse_error', raw, snapshot: null};\n"
            "    }\n"
            "    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {\n"
            "      return {status: 'invalid_root', raw, snapshot: null};\n"
            "    }\n"
            "    try {\n"
            "      return {\n"
            "        status: 'valid', raw,\n"
            "        snapshot: JSON.parse(JSON.stringify(parsed)),\n"
            "      };\n"
            "    } catch (_) {\n"
            "      return {status: 'clone_error', raw, snapshot: null};\n"
            "    }\n"
            "  };\n"
            "  const readCommonRuntimeEventLog = () => {\n"
            "    const key = '__ghostweb_checkpoint_events_v1__';\n"
            "    const candidates = [\n"
            "      ['session_storage_json', window.sessionStorage],\n"
            "      ['local_storage_json', window.localStorage],\n"
            "    ];\n"
            "    let persisted = null;\n"
            "    for (const [source, storage] of candidates) {\n"
            "      if (!storage || typeof storage.getItem !== 'function') continue;\n"
            "      let raw = null;\n"
            "      try { raw = storage.getItem(key); } catch (_) {\n"
            "        return {status: 'read_error', source, raw: null, events: []};\n"
            "      }\n"
            "      if (typeof raw !== 'string') continue;\n"
            "      let parsed = null;\n"
            "      try { parsed = JSON.parse(raw); } catch (_) {\n"
            "        return {status: 'parse_error', source, raw, events: []};\n"
            "      }\n"
            "      if (!Array.isArray(parsed)) {\n"
            "        return {status: 'invalid_root', source, raw, events: []};\n"
            "      }\n"
            "      try {\n"
            "        persisted = {source, events: JSON.parse(JSON.stringify(parsed))};\n"
            "      } catch (_) {\n"
            "        return {status: 'clone_error', source, raw, events: []};\n"
            "      }\n"
            "      break;\n"
            "    }\n"
            "    let volatile = null;\n"
            "    if (window.GhostwebCheckpointRuntime\n"
            "        && typeof window.GhostwebCheckpointRuntime.eventLog === 'function') {\n"
            "      try {\n"
            "        const events = window.GhostwebCheckpointRuntime.eventLog();\n"
            "        if (!Array.isArray(events)) return {status: 'invalid_root',\n"
            "          source: 'runtime_event_log_api', raw: null, events: []};\n"
            "        volatile = JSON.parse(JSON.stringify(events));\n"
            "      } catch (_) {\n"
            "        return {status: 'read_error', source: 'runtime_event_log_api',\n"
            "          raw: null, events: []};\n"
            "      }\n"
            "    }\n"
            "    if (!persisted && !volatile) {\n"
            "      return {status: 'missing', source: null, raw: null, events: []};\n"
            "    }\n"
            "    const merged = [];\n"
            "    const seen = new Set();\n"
            "    // Both persisted and in-memory event logs may carry their identity\n"
            "    // only in payload.  Deduplicate a provable copy, never an anonymous\n"
            "    // row: otherwise an unrelated event with five empty envelope fields\n"
            "    // can erase a later payload-only checkpoint observation.\n"
            "    const stableEventJson = (value) => {\n"
            "      if (value === null) return 'null';\n"
            "      if (Array.isArray(value)) return '['\n"
            "        + value.map(stableEventJson).join(',') + ']';\n"
            "      if (typeof value === 'object') return '{'\n"
            "        + Object.keys(value).sort().map((key) =>\n"
            "          JSON.stringify(key) + ':' + stableEventJson(value[key]))\n"
            "          .join(',') + '}';\n"
            "      return JSON.stringify(value);\n"
            "    };\n"
            "    const eventCopyIdentity = (event) => {\n"
            "      if (!event || typeof event !== 'object' || Array.isArray(event))\n"
            "        return null;\n"
            "      const payload = event.payload && typeof event.payload === 'object'\n"
            "        && !Array.isArray(event.payload) ? event.payload : {};\n"
            "      const eventId = event.event_id == null\n"
            "        ? payload.event_id : event.event_id;\n"
            "      if (eventId != null && String(eventId))\n"
            "        return 'event_id:' + JSON.stringify(String(eventId));\n"
            "      const fields = ['trace_id', 'checkpoint_id', 'action_id',\n"
            "        'sequence', 'timestamp'];\n"
            "      const hasIdentitySignal = fields.some((field) => {\n"
            "        const value = event[field] == null ? payload[field] : event[field];\n"
            "        return value != null && String(value) !== '';\n"
            "      });\n"
            "      if (!hasIdentitySignal) return null;\n"
            "      try { return 'canonical:' + stableEventJson(event); } catch (_) {\n"
            "        return null;\n"
            "      }\n"
            "    };\n"
            "    [persisted && persisted.events || [], volatile || []].forEach((rows) => {\n"
            "      rows.forEach((event) => {\n"
            "        const identity = eventCopyIdentity(event);\n"
            "        if (identity === null) { merged.push(event); return; }\n"
            "        if (seen.has(identity)) return;\n"
            "        seen.add(identity); merged.push(event);\n"
            "      });\n"
            "    });\n"
            "    const raw = JSON.stringify(merged);\n"
            "    return {status: 'valid',\n"
            "      source: persisted && volatile ? persisted.source + '+runtime_event_log_api'\n"
            "        : (persisted ? persisted.source : 'runtime_event_log_api'),\n"
            "      raw, events: merged};\n"
            "  };\n"
            "  const checkpointSnapshotSource = configuredCheckpointEvidenceContract\n"
            "    && configuredCheckpointEvidenceContract.snapshot_source;\n"
            "  const configuredV1Adapter = configuredRuntimeEventContract\n"
            "    && configuredRuntimeEventContract.runtime_event_adapters\n"
            "    && configuredRuntimeEventContract.runtime_event_adapters['1'];\n"
            "  const v1AdapterSnapshotSource = configuredV1Adapter\n"
            "    && configuredV1Adapter.snapshot_source;\n"
            "  const checkpointSnapshotBefore = readCommonStorageJsonSource(\n"
            "    checkpointSnapshotSource);\n"
            "  const v1AdapterEnvelopeBefore = readCommonStorageJsonSource(\n"
            "    v1AdapterSnapshotSource);\n"
            "  const runtimeEventLogBefore = readCommonRuntimeEventLog();\n"
            "  window.__ghostweb_last_eval_result__ = null;\n"
            "  let raw_result = (() => {\n"
            f"{js_code}\n"
            "  })();\n"
            "  const checkpointSnapshotAfter = readCommonStorageJsonSource(\n"
            "    checkpointSnapshotSource);\n"
            "  const v1AdapterEnvelopeAfter = readCommonStorageJsonSource(\n"
            "    v1AdapterSnapshotSource);\n"
            "  const runtimeEventLogAfter = readCommonRuntimeEventLog();\n"
            "  const checkpointSnapshotStable =\n"
            "    checkpointSnapshotBefore.status === 'valid'\n"
            "    && checkpointSnapshotAfter.status === 'valid'\n"
            "    && checkpointSnapshotBefore.raw === checkpointSnapshotAfter.raw;\n"
            "  const v1AdapterEnvelopeStable =\n"
            "    v1AdapterEnvelopeBefore.status === 'valid'\n"
            "    && v1AdapterEnvelopeAfter.status === 'valid'\n"
            "    && v1AdapterEnvelopeBefore.raw === v1AdapterEnvelopeAfter.raw;\n"
            "  const runtimeEventLogStable = (\n"
            "    runtimeEventLogBefore.status === 'missing'\n"
            "      && runtimeEventLogAfter.status === 'missing')\n"
            "    || (runtimeEventLogBefore.status === 'valid'\n"
            "      && runtimeEventLogAfter.status === 'valid'\n"
            "      && runtimeEventLogBefore.source === runtimeEventLogAfter.source\n"
            "      && runtimeEventLogBefore.raw === runtimeEventLogAfter.raw);\n"
            "  const commonCheckpointSnapshot = checkpointSnapshotStable\n"
            "    ? checkpointSnapshotAfter.snapshot : null;\n"
            "  const commonV1AdapterEnvelope = v1AdapterEnvelopeStable\n"
            "    ? v1AdapterEnvelopeAfter.snapshot : null;\n"
            "  const commonRuntimeEvents = runtimeEventLogStable\n"
            "    && runtimeEventLogAfter.status === 'valid'\n"
            "    ? runtimeEventLogAfter.events : [];\n"
            "  let details = (raw_result && typeof raw_result === 'object')\n"
            "    ? raw_result\n"
            "    : (window.__ghostweb_last_eval_result__ || null);\n"
            f"  const configuredRequired = {required_json};\n"
            f"  const configuredDiagnostic = {diagnostic_json};\n"
            f"  const configuredTerminalRequired = {terminal_json};\n"
            f"  const configuredCheckpointDependencies = {dependencies_json};\n"
            f"  const configuredCheckpointWeights = {weights_json};\n"
            f"  const configuredZeroWeightAssertionIds = {zero_weight_assertion_ids_json};\n"
            f"  const configuredTerminalSolutionPolicy = {solution_policy_json};\n"
            f"  const configuredTaskId = {task_id_json};\n"
            "  const configuredSourceReferenceTaskId = "
            f"{source_reference_task_id_json};\n"
            "  const requiredFromDetails = details && Array.isArray(details.required)\n"
            "    ? details.required.map(String) : [];\n"
            "  const reportedVector = details && Array.isArray(details.checkpoint_reward_vector)\n"
            "    ? details.checkpoint_reward_vector : [];\n"
            "  const vectorCheckpointProjection = reportedVector.length\n"
            "    ? Object.fromEntries(reportedVector.map((row) => [\n"
            "      String(row && row.checkpoint_id || ''), Boolean(row && row.reached)\n"
            "    ]).filter(([checkpointId]) => checkpointId)) : null;\n"
            "  const producerCheckpointVectorProjectionLedger = reportedVector\n"
            "    .map((row) => ({\n"
            "      checkpoint_id: String(row && row.checkpoint_id || ''),\n"
            "      reached: row && Object.prototype.hasOwnProperty.call(row, 'reached')\n"
            "        ? row.reached : null,\n"
            "      reward_weight: row\n"
            "        && Object.prototype.hasOwnProperty.call(row, 'reward_weight')\n"
            "        ? row.reward_weight : null,\n"
            "      reward_contribution: row\n"
            "        && Object.prototype.hasOwnProperty.call(row, 'reward_contribution')\n"
            "        ? row.reward_contribution : null,\n"
            "      earned: row && Object.prototype.hasOwnProperty.call(row, 'earned')\n"
            "        ? row.earned : null,\n"
            "    })).filter((row) => row.checkpoint_id);\n"
            "  // Producer maps and reward vectors are output projections only.\n"
            "  // Their `reached` or self-reported validity flags never establish\n"
            "  // checkpoint credit in the common reducer.\n"
            "  const explicitStateCandidates = [\n"
            "    details && details.checkpoints,\n"
            "    details && details.checkpoint_state,\n"
            "    details && details.dependency_gated_checkpoint_state,\n"
            "    details && details.checkpoint_outcomes,\n"
            "  ];\n"
            "  const producerCheckpointState = explicitStateCandidates.find((candidate) =>\n"
            "    candidate && typeof candidate === 'object' && !Array.isArray(candidate)\n"
            "  ) || null;\n"
            "  const reportedSeen = new Set(details && Array.isArray(details.seen)\n"
            "    ? details.seen.map(String) : []);\n"
            "  const reportedMissing = new Set(details && Array.isArray(details.missing)\n"
            "    ? details.missing.map(String) : []);\n"
            "  const reportedInvalid = new Set(details && details.invalid\n"
            "    && typeof details.invalid === 'object'\n"
            "    ? Object.entries(details.invalid).filter(([, reasons]) =>\n"
            "      Array.isArray(reasons) ? reasons.length > 0 : Boolean(reasons))\n"
            "      .map(([checkpointId]) => String(checkpointId)) : []);\n"
            "  const required = configuredRequired.length ? configuredRequired\n"
            "    : (requiredFromDetails.length ? requiredFromDetails\n"
            "      : (producerCheckpointState ? Object.keys(producerCheckpointState)\n"
            "        : (vectorCheckpointProjection\n"
            "          ? Object.keys(vectorCheckpointProjection) : [])));\n"
            "  if (details && required.length) {\n"
            "    const runtimeEventFormalAuthorityDeclared = Boolean(\n"
            "      configuredRuntimeEventContract\n"
            "      && configuredRuntimeEventContract.contract_schema\n"
            "        === 'agentforge.webenv.runtime-event-contract.v2');\n"
            "    const events = runtimeEventFormalAuthorityDeclared\n"
            "      ? commonRuntimeEvents\n"
            "      : (runtimeEventLogAfter.status === 'valid'\n"
            "        ? runtimeEventLogAfter.events : []);\n"
            "    const taskId = String(configuredTaskId || '');\n"
            "    const sourceReferenceTaskId = String(\n"
            "      configuredSourceReferenceTaskId || '');\n"
            "    const eventPayload = (event) => event && event.payload\n"
            "      && typeof event.payload === 'object' ? event.payload : {};\n"
            "    const firstDefined = (...values) => {\n"
            "      for (const value of values) {\n"
            "        if (value !== null && value !== undefined) return value;\n"
            "      }\n"
            "      return null;\n"
            "    };\n"
            "    const eventTaskId = (event) => String(\n"
            "      event && (event.task_id || eventPayload(event).task_id) || '');\n"
            "    const eventTraceId = (event) => String(\n"
            "      event && (event.trace_id || eventPayload(event).trace_id) || '');\n"
            "    const eventCheckpointId = (event) => String(\n"
            "      event && (event.checkpoint_id\n"
            "        || eventPayload(event).checkpoint_id) || '');\n"
            "    const eventContractType = (event) => String(\n"
            "      event && (event.contract_type\n"
            "        || eventPayload(event).contract_type) || '');\n"
            "    const allowedEventIdentities = new Set([\n"
            "      taskId, sourceReferenceTaskId\n"
            "    ].filter(Boolean));\n"
            "    const validateEventIdentity = (event) => {\n"
            "      const payload = event && typeof event.payload === 'object'\n"
            "        ? event.payload : {};\n"
            "      const identities = [\n"
            "        event && event.task_id, payload.task_id,\n"
            "        event && event.trace_id, payload.trace_id,\n"
            "      ].map((value) => String(value || '')).filter(Boolean);\n"
            "      if (!identities.length) {\n"
            "        return {valid: false, reason: 'unbound_event_identity'};\n"
            "      }\n"
            "      if (!allowedEventIdentities.size\n"
            "          || identities.some((identity) =>\n"
            "            !allowedEventIdentities.has(identity))) {\n"
            "        return {valid: false, reason: 'event_identity_mismatch'};\n"
            "      }\n"
            "      return {valid: true, reason: null};\n"
            "    };\n"
            "    const eventIdentityRows = (Array.isArray(events) ? events : [])\n"
            "      .map((event, rawEventIndex) => ({\n"
            "        event, raw_event_index: rawEventIndex,\n"
            "        identity: validateEventIdentity(event),\n"
            "      }));\n"
            "    const matchingRows = eventIdentityRows.filter((row) =>\n"
            "      row.identity.valid);\n"
            "    const matching = matchingRows.map((row) => row.event);\n"
            f"{runtime_event_validation_js}\n"
            "    const producerDiagnostic = details && details.supervision_contract\n"
            "      && Array.isArray(details.supervision_contract.diagnostic_domain)\n"
            "      ? details.supervision_contract.diagnostic_domain.map(String) : [];\n"
            "    const diagnosticDomain = new Set([\n"
            "      ...configuredDiagnostic, ...producerDiagnostic\n"
            "    ]);\n"
            "    const taskCheckpointDomain = new Set([\n"
            "      ...required, ...Array.from(diagnosticDomain)\n"
            "    ]);\n"
            "    const legacyValidEventByCheckpoint = {};\n"
            "    const legacyDuplicateCounts = {};\n"
            "    const legacyOutOfDomain = [];\n"
            "    const runtimeEventValidationLedger = [];\n"
            "    const producerSchemaIncompatibleCheckpointIds = new Set();\n"
            "    const runtimeEventInvalidCheckpointIds = new Set();\n"
            "    eventIdentityRows.forEach((identityRow) => {\n"
            "      const event = identityRow.event;\n"
            "      const checkpointId = eventCheckpointId(event);\n"
            "      if (!taskCheckpointDomain.has(checkpointId)) {\n"
            "        if (checkpointId) legacyOutOfDomain.push(checkpointId);\n"
            "        return;\n"
            "      }\n"
            "      if (!runtimeEventFormalAuthorityDeclared) {\n"
            "        runtimeEventValidationLedger.push({\n"
            "          event_index: identityRow.raw_event_index,\n"
            "          checkpoint_id: checkpointId,\n"
            "          input_runtime_event_schema_version:\n"
            "            runtimeEventSchemaVersion(event).version,\n"
            "          normalized_runtime_event_schema_version: null,\n"
            "          version_declared: runtimeEventSchemaVersion(event).declared,\n"
            "          adapter_id: null,\n"
            "          evidence_source: 'runtime_event_log',\n"
            "          status: 'diagnostic_only',\n"
            "          reasons: ['undeclared_runtime_event_contract'],\n"
            "        });\n"
            "        return;\n"
            "      }\n"
            "      const eventIndex = matchingRows.findIndex((row) =>\n"
            "        row.raw_event_index === identityRow.raw_event_index);\n"
            "      if (!identityRow.identity.valid) {\n"
            "        runtimeEventValidationLedger.push({\n"
            "          event_index: identityRow.raw_event_index,\n"
            "          checkpoint_id: checkpointId,\n"
            "          input_runtime_event_schema_version:\n"
            "            runtimeEventSchemaVersion(event).version,\n"
            "          normalized_runtime_event_schema_version: null,\n"
            "          version_declared: runtimeEventSchemaVersion(event).declared,\n"
            "          adapter_id: null,\n"
            "          status: 'producer_schema_incompatible',\n"
            "          reasons: [identityRow.identity.reason],\n"
            "        });\n"
            "        producerSchemaIncompatibleCheckpointIds.add(checkpointId);\n"
            "        return;\n"
            "      }\n"
            "      const validation = validateRuntimeEvent(event, eventIndex);\n"
            "      runtimeEventValidationLedger.push(validation);\n"
            "      if (validation.status === 'producer_schema_incompatible') {\n"
            "        producerSchemaIncompatibleCheckpointIds.add(checkpointId);\n"
            "        return;\n"
            "      }\n"
            "      if (validation.status !== 'valid') {\n"
            "        runtimeEventInvalidCheckpointIds.add(checkpointId);\n"
            "        return;\n"
            "      }\n"
            "      if (!required.includes(checkpointId)) return;\n"
            "      if (legacyValidEventByCheckpoint[checkpointId]) {\n"
            "        legacyDuplicateCounts[checkpointId] =\n"
            "          (legacyDuplicateCounts[checkpointId] || 0) + 1;\n"
            "        return;\n"
            "      }\n"
            "      legacyValidEventByCheckpoint[checkpointId] = {event, eventIndex};\n"
            "    });\n"
            "    const runtimeEventSchemaVersionsSeen = Array.from(new Set(\n"
            "      runtimeEventValidationLedger.map((row) =>\n"
            "        row.input_runtime_event_schema_version)\n"
            "        .filter((value) => Number.isInteger(value))\n"
            "    )).sort((left, right) => left - right);\n"
            "    const runtimeEventAuthorityDeclared =\n"
            "      runtimeEventFormalAuthorityDeclared;\n"
            "    const checkpointEvidenceAuthorityDeclared = Boolean(\n"
            "      configuredCheckpointEvidenceContract\n"
            "      && [\n"
            "        'agentforge.webenv.checkpoint-evidence-contract.v1',\n"
            "        'agentforge.webenv.checkpoint-evidence-contract.v2',\n"
            "        'agentforge.webenv.checkpoint-evidence-contract.v3',\n"
            "      ].includes(configuredCheckpointEvidenceContract.contract_schema));\n"
            "    const missingCheckpointEvidenceAuthority = required.length > 0\n"
            "      && !runtimeEventAuthorityDeclared\n"
            "      && !checkpointEvidenceAuthorityDeclared;\n"
            "    const checkpointSnapshotSourceReasons = [];\n"
            "    const runtimeEventLogSourceReasons = [];\n"
            "    if (runtimeEventFormalAuthorityDeclared) {\n"
            "      if (!['valid', 'missing'].includes(runtimeEventLogBefore.status)) {\n"
            "        runtimeEventLogSourceReasons.push(\n"
            "          'runtime_event_log_source_' + runtimeEventLogBefore.status);\n"
            "      } else if (!['valid', 'missing'].includes(runtimeEventLogAfter.status)) {\n"
            "        runtimeEventLogSourceReasons.push(\n"
            "          'runtime_event_log_source_' + runtimeEventLogAfter.status);\n"
            "      } else if (!runtimeEventLogStable) {\n"
            "        runtimeEventLogSourceReasons.push('evaluator_side_effect');\n"
            "      }\n"
            "    }\n"
            "    if (checkpointEvidenceAuthorityDeclared) {\n"
            "      if (checkpointSnapshotBefore.status !== 'valid') {\n"
            "        checkpointSnapshotSourceReasons.push(\n"
            "          'checkpoint_snapshot_source_' + checkpointSnapshotBefore.status);\n"
            "      } else if (checkpointSnapshotAfter.status !== 'valid') {\n"
            "        checkpointSnapshotSourceReasons.push(\n"
            "          'checkpoint_snapshot_source_' + checkpointSnapshotAfter.status);\n"
            "      } else if (!checkpointSnapshotStable) {\n"
            "        checkpointSnapshotSourceReasons.push('evaluator_side_effect');\n"
            "      }\n"
            "    }\n"
            "    if (missingCheckpointEvidenceAuthority) {\n"
            "      required.forEach((checkpointId) =>\n"
            "        producerSchemaIncompatibleCheckpointIds.add(checkpointId));\n"
            "    }\n"
            "    if (checkpointSnapshotSourceReasons.length) {\n"
            "      required.forEach((checkpointId) =>\n"
            "        producerSchemaIncompatibleCheckpointIds.add(checkpointId));\n"
            "    }\n"
            "    if (runtimeEventLogSourceReasons.length) {\n"
            "      required.forEach((checkpointId) =>\n"
            "        producerSchemaIncompatibleCheckpointIds.add(checkpointId));\n"
            "    }\n"
            "    const producerSchemaIncompatibleReasons = [\n"
            "      ...(missingCheckpointEvidenceAuthority\n"
            "        ? ['missing_checkpoint_evidence_authority'] : []),\n"
            "      ...checkpointSnapshotSourceReasons,\n"
            "      ...runtimeEventLogSourceReasons,\n"
            "    ];\n"
            "    const producerSchemaIncompatible =\n"
            "      producerSchemaIncompatibleCheckpointIds.size > 0;\n"
            "    const runtimeEventSchemaInvalid =\n"
            "      runtimeEventInvalidCheckpointIds.size > 0;\n"
            "    const runtimeEventCarrierValid =\n"
            "      !producerSchemaIncompatible && !runtimeEventSchemaInvalid;\n"
            "    const producerAppOwnedStateSnapshotProjection = details\n"
            "      && isPlainRecord(details.app_owned_state_snapshot)\n"
            "      ? details.app_owned_state_snapshot : null;\n"
            "    const appOwnedStateSnapshot = commonCheckpointSnapshot;\n"
            "    const producerSnapshotMatchesCommon = Boolean(\n"
            "      producerAppOwnedStateSnapshotProjection\n"
            "      && appOwnedStateSnapshot\n"
            "      && canonicalEqual(\n"
            "        producerAppOwnedStateSnapshotProjection, appOwnedStateSnapshot));\n"
            "    const checkpointEvidenceSpecs = configuredCheckpointEvidenceContract\n"
            "      && isPlainRecord(configuredCheckpointEvidenceContract.checkpoints)\n"
            "      ? configuredCheckpointEvidenceContract.checkpoints : {};\n"
            "    const validateAppOwnedAssertions = (assertions) => {\n"
            "      const assertionRows = assertions.map((assertion) => {\n"
            "        if (assertion && isPlainRecord(assertion.expression)) {\n"
            "          const evaluated = appOwnedStateSnapshot\n"
            "            ? evaluateStateExpression(\n"
            "              assertion.expression, appOwnedStateSnapshot)\n"
            "            : {ok: false, value: null, reason: 'snapshot_missing'};\n"
            "          const valid = evaluated.ok && evaluated.value === true;\n"
            "          return {\n"
            "            expression: assertion.expression,\n"
            "            expression_result: evaluated.ok ? evaluated.value : null,\n"
            "            valid,\n"
            "            reason: valid ? null : (evaluated.ok\n"
            "              ? 'expression_false_or_non_boolean' : evaluated.reason),\n"
            "          };\n"
            "        }\n"
            "        const resolved = appOwnedStateSnapshot\n"
            "          ? resolvePointer(appOwnedStateSnapshot, assertion.actual_pointer)\n"
            "          : {found: false, value: null};\n"
            "        const valid = validateEvidencePredicate(\n"
            "          resolved, assertion.predicate, assertion.expected\n"
            "        );\n"
            "        return {\n"
            "          actual_pointer: assertion.actual_pointer,\n"
            "          predicate: assertion.predicate,\n"
            "          expected: assertion.expected,\n"
            "          actual_found: resolved.found,\n"
            "          actual: resolved.found ? resolved.value : null,\n"
            "          valid,\n"
            "          reason: valid ? null\n"
            "            : (resolved.found || assertion.predicate === 'exists'\n"
            "              ? 'predicate_mismatch' : 'actual_pointer_missing'),\n"
            "        };\n"
            "      });\n"
            "      return {\n"
            "        valid: Boolean(appOwnedStateSnapshot && assertionRows.length)\n"
            "          && assertionRows.every((row) => row.valid),\n"
            "        assertions: assertionRows,\n"
            "      };\n"
            "    };\n"
            "    const checkpointEvidenceState = {};\n"
            "    const checkpointEvidenceValidationLedger = Object.entries(\n"
            "      checkpointEvidenceSpecs\n"
            "    ).map(([checkpointId, checkpointSpec]) => {\n"
            "      const assertions = checkpointSpec\n"
            "        && Array.isArray(checkpointSpec.assertions)\n"
            "        ? checkpointSpec.assertions : [];\n"
            "      const validation = validateAppOwnedAssertions(assertions);\n"
            "      checkpointEvidenceState[checkpointId] = validation.valid;\n"
            "      return {checkpoint_id: checkpointId, ...validation};\n"
            "    });\n"
            "    const forbiddenConditionSpecs = configuredCheckpointEvidenceContract\n"
            "      && Array.isArray(\n"
            "        configuredCheckpointEvidenceContract.forbidden_conditions)\n"
            "      ? configuredCheckpointEvidenceContract.forbidden_conditions : [];\n"
            "    const forbiddenConditionState = {};\n"
            "    const forbiddenConditionValidationLedger = forbiddenConditionSpecs\n"
            "      .map((conditionSpec) => {\n"
            "        const conditionId = String(conditionSpec && conditionSpec.id || '');\n"
            "        const assertions = conditionSpec\n"
            "          && Array.isArray(conditionSpec.assertions)\n"
            "          ? conditionSpec.assertions : [];\n"
            "        const validation = validateAppOwnedAssertions(assertions);\n"
            "        forbiddenConditionState[conditionId] = validation.valid;\n"
            "        return {condition_id: conditionId, ...validation};\n"
            "      });\n"
            "    const independentlyValidatedEvidenceCoversRequired = required.length > 0\n"
            "      && required.every((checkpointId) =>\n"
            "        checkpointEvidenceState[checkpointId] === true);\n"
            "    const checkpointEventTimeline = matching\n"
            "      .map((event, eventIndex) => ({\n"
            "        event_index: eventIndex,\n"
            "        checkpoint_id: eventCheckpointId(event),\n"
            "        task_id: eventTaskId(event),\n"
            "        trace_id: eventTraceId(event),\n"
            "        contract_type: eventContractType(event),\n"
            "        event_type: String(event && (event.event_type || event.event\n"
            "          || event.type || eventPayload(event).event_type) || ''),\n"
            "        timestamp: event ? firstDefined(\n"
            "          event.timestamp, event.ts, eventPayload(event).timestamp)\n"
            "          : null,\n"
            "      }))\n"
            "      .filter((row) => row.checkpoint_id);\n"
            "    const firstEventIndex = required.map((checkpointId) => {\n"
            "      const producerIndexes = details && details.checkpoint_first_event_index;\n"
            "      const selected = producerIndexes && producerIndexes[checkpointId];\n"
            "      if (Number.isInteger(selected) && selected >= 0) return selected;\n"
            "      const row = reportedVector.find((candidate) =>\n"
            "        String(candidate && candidate.checkpoint_id || '') === checkpointId);\n"
            "      const value = row && row.first_event_index;\n"
            "      return Number.isInteger(value) && value >= 0 ? value : null;\n"
            "    });\n"
            "    for (let checkpointIndex = 0; checkpointIndex < required.length;\n"
            "      checkpointIndex += 1) {\n"
            "      if (firstEventIndex[checkpointIndex] !== null) continue;\n"
            "      const checkpointId = required[checkpointIndex];\n"
            "      const eventRow = checkpointEventTimeline.find((row) =>\n"
            "        row.checkpoint_id === checkpointId);\n"
            "      if (eventRow) firstEventIndex[checkpointIndex] = eventRow.event_index;\n"
            "    }\n"
            "    const fallbackContribution = required.length ? 1 / required.length : 0;\n"
            "    const producerZeroWeightAssertionIds = details\n"
            "      && Array.isArray(details.zero_weight_assertion_ids)\n"
            "      ? details.zero_weight_assertion_ids.map(String) : [];\n"
            "    const zeroWeightAssertionIds = new Set(\n"
            "      configuredZeroWeightAssertionIds.filter((checkpointId) =>\n"
            "        required.includes(checkpointId)));\n"
            "    const configuredContributionAt = (index) => {\n"
            "      const checkpointId = required[index];\n"
            "      if (Object.prototype.hasOwnProperty.call(\n"
            "          configuredCheckpointWeights, checkpointId)) {\n"
            "        const configured = Number(configuredCheckpointWeights[checkpointId]);\n"
            "        return Number.isFinite(configured) && configured >= 0\n"
            "          ? configured : null;\n"
            "      }\n"
            "      return null;\n"
            "    };\n"
            "    const contributionAt = (index) => {\n"
            "      if (zeroWeightAssertionIds.has(required[index])) return 0;\n"
            "      const configured = configuredContributionAt(index);\n"
            "      if (configured !== null) return configured;\n"
            "      return fallbackContribution;\n"
            "    };\n"
            "    const explicitTerminal = typeof details.terminal_success === 'boolean'\n"
            "      ? details.terminal_success : null;\n"
            "    // Browser assertion rows are retained only as producer diagnostics.\n"
            "    // Common terminal authority comes from the same package-declared\n"
            "    // checkpoint evidence predicates used for ordinary checkpoint credit.\n"
            "    const terminalActualCheckpointReached = (checkpointId) =>\n"
            "      configuredTerminalRequired.includes(checkpointId)\n"
            "      && checkpointEvidenceState[checkpointId] === true;\n"
            "    const terminalAssertionsSatisfied =\n"
            "      configuredTerminalRequired.length > 0\n"
            "      && configuredTerminalRequired.every(terminalActualCheckpointReached);\n"
            "    const baseCheckpointReached = (checkpointId) => {\n"
            "      if (checkpointEvidenceState[checkpointId] === true) return true;\n"
            "      if (legacyValidEventByCheckpoint[checkpointId]) return true;\n"
            "      // Producer map/vector/seen/missing/invalid fields are projections\n"
            "      // only and cannot establish or veto independently validated truth.\n"
            "      return false;\n"
            "    };\n"
            "    const producerCheckpointDependenciesProjection = details\n"
            "      && isPlainRecord(details.checkpoint_dependencies)\n"
            "      ? details.checkpoint_dependencies\n"
            "      : (details && isPlainRecord(details.checkpoint_dependency_graph)\n"
            "        ? details.checkpoint_dependency_graph : null);\n"
            "    const checkpointDependencies = configuredCheckpointDependencies;\n"
            "    const hasExplicitCheckpointDependencies =\n"
            "      Object.keys(checkpointDependencies).length > 0;\n"
            "    const dependencyMemo = new Map();\n"
            "    const dependencyGatedReached = (checkpointId, visiting) => {\n"
            "      if (dependencyMemo.has(checkpointId)) return dependencyMemo.get(checkpointId);\n"
            "      if (!baseCheckpointReached(checkpointId)) return false;\n"
            "      const active = visiting || new Set();\n"
            "      if (active.has(checkpointId)) return false;\n"
            "      const next = new Set(active); next.add(checkpointId);\n"
            "      const dependencies = Array.isArray(checkpointDependencies[checkpointId])\n"
            "        ? checkpointDependencies[checkpointId].map(String) : [];\n"
            "      const reached = dependencies.every((dependency) =>\n"
            "        dependencyGatedReached(dependency, next));\n"
            "      dependencyMemo.set(checkpointId, reached);\n"
            "      return reached;\n"
            "    };\n"
            "    const checkpointReached = (checkpointId) => hasExplicitCheckpointDependencies\n"
            "      ? dependencyGatedReached(checkpointId) : baseCheckpointReached(checkpointId);\n"
            "    const reachedCount = required.filter(checkpointReached).length;\n"
            "    let groundedCoverageReward = 0;\n"
            "    required.forEach((checkpointId, index) => {\n"
            "      if (checkpointReached(checkpointId))\n"
            "        groundedCoverageReward += contributionAt(index);\n"
            "    });\n"
            "    const evaluatorScalar = (typeof raw_result === 'number'\n"
            "        && Number.isFinite(raw_result)) ? raw_result\n"
            "      : (details && Number.isFinite(Number(details.raw_reward))\n"
            "        ? Number(details.raw_reward)\n"
            "        : (details && Number.isFinite(Number(details.score))\n"
            "          ? Number(details.score) : null));\n"
            "    const producerSafetyViolationProjection = {\n"
            "      forbiddenHit: typeof details.forbiddenHit === 'boolean'\n"
            "        ? details.forbiddenHit : null,\n"
            "      safety_violation: typeof details.safety_violation === 'boolean'\n"
            "        ? details.safety_violation : null,\n"
            "    };\n"
            "    const safetyViolation = Object.values(forbiddenConditionState)\n"
            "      .some((value) => value === true);\n"
            "    const producerEnvironmentValidProjection =\n"
            "      typeof details.env_valid === 'boolean' ? details.env_valid : null;\n"
            "    // Any declared-carrier schema defect or evaluator-side mutation is\n"
            "    // fail-closed even when another carrier independently proves state.\n"
            "    const evaluationEnvironmentValid = runtimeEventCarrierValid;\n"
            "    const coverageScore = safetyViolation || !evaluationEnvironmentValid\n"
            "      ? 0 : Math.min(1, groundedCoverageReward);\n"
            "    const terminalSolutionMode = String(\n"
            "      configuredTerminalSolutionPolicy.mode || 'required_checkpoint_gate');\n"
            "    const terminalRequiresAllRequired =\n"
            "      configuredTerminalSolutionPolicy.require_all_required_checkpoints !== false;\n"
            "    const terminalRequired = configuredTerminalRequired.length\n"
            "      ? configuredTerminalRequired : required;\n"
            "    const terminalCheckpointStateSatisfied = terminalRequired.length > 0\n"
            "      && terminalRequired.every(checkpointReached);\n"
            "    const declaredTerminalCheckpointStateSatisfied =\n"
            "      configuredTerminalRequired.length === 0\n"
            "      || configuredTerminalRequired.every(checkpointReached);\n"
            "    const allRequiredCheckpointStateSatisfied = required.length > 0\n"
            "      && required.every(checkpointReached);\n"
            "    const terminalStateSatisfied =\n"
            "      terminalSolutionMode === 'terminal_business_state'\n"
            "      ? terminalAssertionsSatisfied\n"
            "        && declaredTerminalCheckpointStateSatisfied\n"
            "        && (!terminalRequiresAllRequired || allRequiredCheckpointStateSatisfied)\n"
            "      : terminalCheckpointStateSatisfied;\n"
            "    const evaluatorTerminalProjection = evaluatorScalar === null\n"
            "      ? null : evaluatorScalar >= 1;\n"
            "    const terminalVerdict = terminalStateSatisfied;\n"
            "    const terminalSuccess = !safetyViolation && evaluationEnvironmentValid\n"
            "      && terminalVerdict;\n"
            "    const terminalContributions = terminalRequired\n"
            "      .map((checkpointId) => required.indexOf(checkpointId))\n"
            "      .filter((index) => index >= 0)\n"
            "      .map((index) => contributionAt(index));\n"
            "    const terminalPenalty = terminalContributions.length\n"
            "      ? Math.max(...terminalContributions) : fallbackContribution;\n"
            "    const maxNonTerminalScore = Math.max(0, 1 - terminalPenalty);\n"
            "    const finalScore = terminalSuccess\n"
            "      ? 1 : Math.min(coverageScore, maxNonTerminalScore);\n"
            "    // A v3 checkpoint-evidence contract is the common-owned authority\n"
            "    // for the scalar.  The app evaluator's historical event assertions\n"
            "    // remain useful diagnostics, but cannot lower a score independently\n"
            "    // established from the immutable snapshot and its declared predicates.\n"
            "    const checkpointEvidenceV3AggregationAuthoritative =\n"
            "      configuredCheckpointEvidenceContract\n"
            "      && configuredCheckpointEvidenceContract.contract_schema\n"
            "        === 'agentforge.webenv.checkpoint-evidence-contract.v3';\n"
            "    const appEvaluatorScalar = checkpointEvidenceV3AggregationAuthoritative\n"
            "      ? finalScore : evaluatorScalar;\n"
            "    const canonicalSeen = required.filter(checkpointReached).sort((a, b) => {\n"
            "      const ai = firstEventIndex[required.indexOf(a)];\n"
            "      const bi = firstEventIndex[required.indexOf(b)];\n"
            "      if (ai === null && bi === null)\n"
            "        return required.indexOf(a) - required.indexOf(b);\n"
            "      if (ai === null) return 1;\n"
            "      if (bi === null) return -1;\n"
            "      return ai - bi;\n"
            "    });\n"
            "    const canonicalCredited = evaluationEnvironmentValid ? canonicalSeen : [];\n"
            "    const canonicalDiagnosticSeen = Array.from(diagnosticDomain).filter((id) =>\n"
            "      reportedSeen.has(id) || matching.some((event) =>\n"
            "        eventCheckpointId(event) === id)).sort((a, b) => {\n"
            "        const ai = matching.findIndex((event) => eventCheckpointId(event) === a);\n"
            "        const bi = matching.findIndex((event) => eventCheckpointId(event) === b);\n"
            "        return (ai < 0 ? Number.MAX_SAFE_INTEGER : ai)\n"
            "          - (bi < 0 ? Number.MAX_SAFE_INTEGER : bi);\n"
            "      });\n"
            "    const priorInvalid = details && details.invalid\n"
            "      && typeof details.invalid === 'object' ? details.invalid : {};\n"
            "    const canonicalPriorInvalid =\n"
            "      checkpointEvidenceV3AggregationAuthoritative ? {} : priorInvalid;\n"
            "    const duplicateCheckpointIds = Array.from(new Set([\n"
            "      ...Object.keys(legacyDuplicateCounts),\n"
            "      ...(Array.isArray(priorInvalid.duplicate_checkpoint_ids)\n"
            "        ? priorInvalid.duplicate_checkpoint_ids.map(String) : []),\n"
            "    ]));\n"
            "    const outOfDomainIds = Array.from(new Set([\n"
            "      ...legacyOutOfDomain,\n"
            "      ...(Array.isArray(priorInvalid.out_of_domain)\n"
            "        ? priorInvalid.out_of_domain.map(String) : []),\n"
            "    ])).filter((checkpointId) => !taskCheckpointDomain.has(checkpointId));\n"
            "    details = {\n"
            "      ...details,\n"
            "      app_evaluator_scalar: appEvaluatorScalar,\n"
            "      app_evaluator_scalar_authority:\n"
            "        checkpointEvidenceV3AggregationAuthoritative\n"
            "        ? 'checkpoint_evidence_contract_v3'\n"
            "        : 'producer_evaluator_scalar_projection',\n"
            "      producer_evaluator_scalar_projection: evaluatorScalar,\n"
            "      grounded_reducer_reward: finalScore,\n"
            "      grounded_reducer_contract: 'browser_checkpoint_grounded_v3e',\n"
            "      raw_reward: finalScore,\n"
            "      env_valid: evaluationEnvironmentValid,\n"
            "      forbiddenHit: safetyViolation,\n"
            "      safety_violation: safetyViolation,\n"
            "      runtime_event_schema_version:\n"
            "        configuredRuntimeEventContract.runtime_event_schema_version\n"
            "        || (runtimeEventSchemaVersionsSeen.length === 1\n"
            "          ? runtimeEventSchemaVersionsSeen[0] : null),\n"
            "      runtime_event_schema_versions_seen: runtimeEventSchemaVersionsSeen,\n"
            "      runtime_event_contract: configuredRuntimeEventContract,\n"
            "      runtime_event_formal_authority_declared:\n"
            "        runtimeEventFormalAuthorityDeclared,\n"
            "      runtime_event_identity_contract: {\n"
            "        task_id: taskId,\n"
            "        source_reference_task_id: sourceReferenceTaskId || null,\n"
            "      },\n"
            "      runtime_event_log_acquisition: {\n"
            "        source: runtimeEventLogAfter.source || runtimeEventLogBefore.source,\n"
            "        before_status: runtimeEventLogBefore.status,\n"
            "        after_status: runtimeEventLogAfter.status,\n"
            "        status: runtimeEventLogStable\n"
            "          ? runtimeEventLogAfter.status\n"
            "          : (runtimeEventLogSourceReasons.includes('evaluator_side_effect')\n"
            "            ? 'evaluator_side_effect'\n"
            "            : (runtimeEventLogBefore.status !== 'valid'\n"
            "              ? runtimeEventLogBefore.status : runtimeEventLogAfter.status)),\n"
            "        stable: runtimeEventLogStable,\n"
            "        event_count: commonRuntimeEvents.length,\n"
            "        event_log_hash: runtimeEventLogStable\n"
            "          && runtimeEventLogAfter.status === 'valid'\n"
            "          ? canonicalStateHash(commonRuntimeEvents) : null,\n"
            "      },\n"
            "      v1_adapter_snapshot_acquisition: {\n"
            "        source: v1AdapterSnapshotSource || null,\n"
            "        before_status: v1AdapterEnvelopeBefore.status,\n"
            "        after_status: v1AdapterEnvelopeAfter.status,\n"
            "        status: v1AdapterEnvelopeStable ? 'valid'\n"
            "          : (v1AdapterEnvelopeBefore.status === 'valid'\n"
            "            && v1AdapterEnvelopeAfter.status === 'valid'\n"
            "            ? 'evaluator_side_effect'\n"
            "            : (v1AdapterEnvelopeBefore.status !== 'valid'\n"
            "              ? v1AdapterEnvelopeBefore.status\n"
            "              : v1AdapterEnvelopeAfter.status)),\n"
            "        stable: v1AdapterEnvelopeStable,\n"
            "        envelope_hash: commonV1AdapterEnvelope\n"
            "          ? canonicalStateHash(commonV1AdapterEnvelope) : null,\n"
            "      },\n"
            "      runtime_event_validation_ledger: runtimeEventValidationLedger,\n"
            "      checkpoint_evidence_contract: configuredCheckpointEvidenceContract,\n"
            "      checkpoint_snapshot_acquisition: {\n"
            "        source: checkpointSnapshotSource || null,\n"
            "        before_status: checkpointSnapshotBefore.status,\n"
            "        after_status: checkpointSnapshotAfter.status,\n"
            "        status: checkpointSnapshotStable ? 'valid'\n"
            "          : (checkpointSnapshotSourceReasons.includes('evaluator_side_effect')\n"
            "            ? 'evaluator_side_effect'\n"
            "            : (checkpointSnapshotBefore.status !== 'valid'\n"
            "              ? checkpointSnapshotBefore.status\n"
            "              : checkpointSnapshotAfter.status)),\n"
            "        stable: checkpointSnapshotStable,\n"
            "        state_hash: appOwnedStateSnapshot\n"
            "          ? canonicalStateHash(appOwnedStateSnapshot) : null,\n"
            "      },\n"
            "      checkpoint_evidence_state: checkpointEvidenceState,\n"
            "      checkpoint_evidence_validation_ledger:\n"
            "        checkpointEvidenceValidationLedger,\n"
            "      forbidden_condition_state: forbiddenConditionState,\n"
            "      forbidden_condition_validation_ledger:\n"
            "        forbiddenConditionValidationLedger,\n"
            "      producer_checkpoint_state_projection: producerCheckpointState,\n"
            "      producer_app_owned_state_snapshot_projection:\n"
            "        producerAppOwnedStateSnapshotProjection,\n"
            "      producer_snapshot_matches_common: producerSnapshotMatchesCommon,\n"
            "      producer_checkpoint_vector_projection: vectorCheckpointProjection,\n"
            "      producer_checkpoint_vector_projection_ledger:\n"
            "        producerCheckpointVectorProjectionLedger,\n"
            "      producer_safety_violation_projection:\n"
            "        producerSafetyViolationProjection,\n"
            "      producer_checkpoint_dependencies_projection:\n"
            "        producerCheckpointDependenciesProjection,\n"
            "      producer_zero_weight_assertion_ids_projection:\n"
            "        producerZeroWeightAssertionIds,\n"
            "      producer_terminal_success_projection: explicitTerminal,\n"
            "      producer_evaluator_terminal_projection:\n"
            "        evaluatorTerminalProjection,\n"
            "      producer_environment_valid_projection:\n"
            "        producerEnvironmentValidProjection,\n"
            "      producer_schema_incompatible: producerSchemaIncompatible,\n"
            "      producer_schema_incompatible_reasons:\n"
            "        producerSchemaIncompatibleReasons,\n"
            "      producer_schema_incompatible_checkpoint_ids:\n"
            "        Array.from(producerSchemaIncompatibleCheckpointIds),\n"
            "      runtime_event_invalid_checkpoint_ids:\n"
            "        Array.from(runtimeEventInvalidCheckpointIds),\n"
            "      evaluation_invalid_reason: producerSchemaIncompatible\n"
            "        ? 'producer_schema_incompatible'\n"
            "        : (runtimeEventSchemaInvalid\n"
            "          ? 'runtime_event_invalid' : null),\n"
            "      ordered_checkpoint_prefix_length: hasExplicitCheckpointDependencies\n"
            "        ? reachedCount : null,\n"
            "      ordered_checkpoint_total: required.length,\n"
            "      grounded_checkpoint_coverage_count: canonicalCredited.length,\n"
            "      checkpoint_order_mode: hasExplicitCheckpointDependencies\n"
            "        ? 'explicit_dependency_gated_coverage'\n"
            "        : 'grounded_event_state_coverage',\n"
            "      checkpoint_weights: configuredCheckpointWeights,\n"
            "      zero_weight_assertion_ids: Array.from(zeroWeightAssertionIds),\n"
            "      checkpoint_dependencies: checkpointDependencies,\n"
            "      checkpoint_state_source: Object.values(checkpointEvidenceState)\n"
            "        .some((value) => value === true)\n"
            "        ? 'common_validated_app_owned_state_snapshot'\n"
            "        : 'runtime_event_log',\n"
            "      checkpoint_event_timeline: checkpointEventTimeline,\n"
            "      terminal_solution_policy: configuredTerminalSolutionPolicy,\n"
            "      terminal_assertions_satisfied: terminalAssertionsSatisfied,\n"
            "      terminal_success: terminalSuccess,\n"
            "      required,\n"
            "      seen: canonicalCredited,\n"
            "      credited: canonicalCredited,\n"
            "      missing: required.filter((checkpointId) =>\n"
            "        !canonicalCredited.includes(checkpointId)),\n"
            "      diagnostic_seen: canonicalDiagnosticSeen,\n"
            "      seen_domain:\n"
            "        'deduplicated_valid_required_checkpoint_ids_in_first_valid_event_order',\n"
            "      invalid: {\n"
            "        ...canonicalPriorInvalid,\n"
            "        duplicate_checkpoint_ids: [],\n"
            "        out_of_domain: outOfDomainIds,\n"
            "        legacy_event_observation_invalid: required.filter((checkpointId) =>\n"
            "          runtimeEventInvalidCheckpointIds.has(checkpointId)),\n"
            "        runtime_event_invalid:\n"
            "          Array.from(runtimeEventInvalidCheckpointIds),\n"
            "      },\n"
            "      diagnostics: {\n"
            "        ...(details && details.diagnostics\n"
            "          && typeof details.diagnostics === 'object'\n"
            "          ? details.diagnostics : {}),\n"
            "        producer_evaluator_invalid_projection: priorInvalid,\n"
            "        producer_evaluator_invalid_projection_authority:\n"
            "          checkpointEvidenceV3AggregationAuthoritative\n"
            "          ? 'diagnostic_only_checkpoint_evidence_contract_v3'\n"
            "          : 'legacy_compatibility',\n"
            "        duplicate_event_checkpoint_ids: duplicateCheckpointIds,\n"
            "        diagnostic_checkpoint_ids_seen: canonicalDiagnosticSeen,\n"
            "      },\n"
            "      checkpoint_reward_vector: required.map((checkpointId, index) => {\n"
            "        const reported = reportedVector.find((candidate) =>\n"
            "          String(candidate && candidate.checkpoint_id || '') ===\n"
            "            checkpointId) || {};\n"
            "        const anchorIndex = firstEventIndex[index];\n"
            "        return {\n"
            "          ...reported,\n"
            "          checkpoint_id: checkpointId,\n"
            "          reached: evaluationEnvironmentValid\n"
            "            && checkpointReached(checkpointId),\n"
            "          checkpoint_dependencies: Array.isArray(\n"
            "            checkpointDependencies[checkpointId])\n"
            "            ? checkpointDependencies[checkpointId].map(String) : [],\n"
            "          credit_assignment_mode: hasExplicitCheckpointDependencies\n"
            "            ? 'explicit_dependency_gated_coverage'\n"
            "            : 'grounded_event_state_coverage',\n"
            "          first_event_index: anchorIndex,\n"
            "          first_event_index_source: anchorIndex !== null ? 'runtime_event_log'\n"
            "            : (terminalActualCheckpointReached(checkpointId)\n"
            "              ? 'checkpoint_evidence_contract'\n"
            "              : (checkpointEvidenceState[checkpointId] === true\n"
            "              ? 'checkpoint_evidence_contract'\n"
            "              : 'unobserved_event_anchor')),\n"
            "          reward_weight: contributionAt(index),\n"
            "          reward_contribution: evaluationEnvironmentValid\n"
            "            && checkpointReached(checkpointId)\n"
            "            ? contributionAt(index) : 0,\n"
            "          earned: evaluationEnvironmentValid\n"
            "            && checkpointReached(checkpointId)\n"
            "            ? contributionAt(index) : 0,\n"
            "        };\n"
            "      }),\n"
            "    };\n"
            "    raw_result = finalScore;\n"
            "  }\n"
            "  return {raw_result, details};\n"
            "})()"
        )
        result = await page.evaluate(wrapped)
        normalized = _normalize_browser_result(result)
        return _attach_evaluation_metadata(
            normalized,
            task_id=task_id,
            evaluator_path=eval_path,
            evaluator_source=js_code,
            configured_required=configured_required,
            configured_diagnostic=configured_diagnostic,
            configured_terminal=configured_terminal,
            configured_dependencies=configured_dependencies,
            configured_solution_policy=configured_solution_policy,
            configured_runtime_event_contract=configured_runtime_event_contract,
            configured_checkpoint_evidence_contract=(
                configured_checkpoint_evidence_contract
            ),
        )
    except Exception as exc:
        logger.warning("[Evaluator] JS evaluation failed: %s", exc)
        return EnvironmentEvaluation(
            raw_reward=0.0,
            reason="evaluator_execution_error",
            terminal_success=False,
            env_valid=False,
            format_valid=False,
            details={"error": str(exc)},
        )


def _normalize_browser_result(result: Any) -> EnvironmentEvaluation:
    raw_result = result
    details: dict[str, Any] = {}
    if isinstance(result, dict) and "raw_result" in result:
        raw_result = result.get("raw_result")
        if isinstance(result.get("details"), dict):
            details = dict(result["details"])
    elif isinstance(result, dict):
        details = dict(result)

    if isinstance(raw_result, dict):
        details = {**raw_result, **details}
        raw_result = details.get("raw_reward", details.get("score"))

    try:
        score = float(raw_result) if isinstance(raw_result, (int, float)) else 0.0
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    details["raw_reward"] = score
    details.setdefault("terminal_success", score >= 1.0)
    details.setdefault("nonzero_checkpoint_reached", score > 0.0)
    details.setdefault("checkpoint_reward_vector", [])
    if (
        "ability_reward_vector" not in details
        and isinstance(details.get("reward_vector"), dict)
    ):
        details["ability_reward_vector"] = {
            str(key): float(value)
            for key, value in details["reward_vector"].items()
            if key not in {"raw_reward", "terminal_success", "safety_violation"}
            and isinstance(value, (int, float))
        }
    details.setdefault("ability_reward_vector", {})
    details.setdefault("env_valid", True)


    details.setdefault("format_valid", None)
    reason = details.get("evaluation_invalid_reason")
    if not isinstance(reason, str):
        reason = ""
    return EnvironmentEvaluation.from_payload(
        {"raw_reward": score, "reason": reason, "evaluation": details}
    )


def _attach_evaluation_metadata(
    result: EnvironmentEvaluation,
    *,
    task_id: str | None,
    evaluator_path: Path | None,
    evaluator_source: str,
    configured_required: list[str],
    configured_diagnostic: list[str],
    configured_terminal: list[str],
    configured_dependencies: dict[str, list[str]],
    configured_solution_policy: dict[str, Any],
    configured_runtime_event_contract: dict[str, Any],
    configured_checkpoint_evidence_contract: dict[str, Any],
) -> EnvironmentEvaluation:
    if not task_id:
        return result
    vector = (
        result.checkpoint_reward_vector
        if isinstance(result.checkpoint_reward_vector, list)
        else []
    )
    vector_ids = [
        str(row.get("checkpoint_id") or "")
        for row in vector
        if isinstance(row, dict) and row.get("checkpoint_id")
    ]
    required = list(configured_required or dict.fromkeys(vector_ids))
    seen = [
        checkpoint_id
        for checkpoint_id in required
        if any(
            isinstance(row, dict)
            and str(row.get("checkpoint_id") or "") == checkpoint_id
            and row.get("reached") is True
            for row in vector
        )
    ]
    missing = [checkpoint_id for checkpoint_id in required if checkpoint_id not in seen]
    attribution = []
    for checkpoint_id in required:
        row = next(
            (
                candidate
                for candidate in vector
                if isinstance(candidate, dict)
                and str(candidate.get("checkpoint_id") or "") == checkpoint_id
            ),
            {},
        )
        attribution.append(
            {
                "checkpoint_id": checkpoint_id,
                "reached": checkpoint_id in seen,
                "first_event_index": row.get("first_event_index"),
                "first_event_index_source": row.get("first_event_index_source"),
                "reward_contribution": row.get("reward_contribution", 0),
                "checkpoint_dependencies": row.get("checkpoint_dependencies", []),
                "credit_assignment_mode": row.get("credit_assignment_mode"),
            }
        )
    metadata = {
        "schema": "agentforge.webenv.evaluation_metadata.v1",
        "status": "complete",
        "task_id": task_id,
        "evaluator_path": str(evaluator_path) if evaluator_path is not None else "<inline>",
        "evaluator_sha256": "sha256:"
        + hashlib.sha256(evaluator_source.encode("utf-8")).hexdigest(),
        "required_checkpoints": required,
        "required_checkpoints_source": (
            "evaluator_plan"
            if configured_required
            else "evaluator_reward_vector"
            if vector_ids
            else "none_configured"
        ),
        "terminal_checkpoints": list(configured_terminal),
        "diagnostic_checkpoints": list(configured_diagnostic),
        "terminal_solution_policy": dict(configured_solution_policy),
        "runtime_event_contract": dict(configured_runtime_event_contract),
        "checkpoint_evidence_contract": dict(
            configured_checkpoint_evidence_contract
        ),
        "checkpoint_dependencies": configured_dependencies,
        "seen_checkpoints": seen,
        "missing_checkpoints": missing,
        "checkpoint_attribution": attribution,
        "checkpoint_contract": (
            "configured_checkpoint_denominator"
            if required
            else "no_configured_checkpoints"
        ),
        "grounded_reducer_contract": "browser_checkpoint_grounded_v3e",
    }
    details = dict(result.details)
    details.setdefault("task_id", task_id)
    details["evaluation_metadata"] = metadata
    return EnvironmentEvaluation(
        raw_reward=result.raw_reward,
        reason=result.reason,
        terminal_success=result.terminal_success,
        checkpoint_reward_vector=result.checkpoint_reward_vector,
        ability_reward_vector=result.ability_reward_vector,
        nonzero_checkpoint_reached=result.nonzero_checkpoint_reached,
        env_valid=result.env_valid,
        format_valid=result.format_valid,
        details=details,
    )
