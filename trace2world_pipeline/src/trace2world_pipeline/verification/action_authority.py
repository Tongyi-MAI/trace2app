from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ACTION_ID_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*:step\d+)")
ACTION_ID_EXACT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:step\d+$")
SHELL_ACTION_ID_RE = re.compile(r"(shell_propagated::[^\s\"'<>]+)")
SHELL_ACTION_ID_EXACT_RE = re.compile(r"^shell_propagated::[^\s\"'<>]+$")
ROUTE_FALLBACK_ACTION_ID = "fallback_back"
SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script>", re.IGNORECASE)
OPEN_TAG_RE = re.compile(r"<(?P<tag>[A-Za-z][\w:-]*)\b(?P<attrs>[^>]*)>", re.DOTALL)
ATTR_RE = re.compile(r"""(?P<name>[\w:-]+)\s*=\s*("(?P<dq>[^"]*)"|'(?P<sq>[^']*)')""", re.DOTALL)
ACTION_HOST_ATTRS = (
    "data-action-id",
    "data-ghostweb-action-id",
    "data-action-aliases",
    "data-element-id",
    "data-element-aliases",
)


def _is_exact_action_id(value: str) -> bool:
    return bool(
        ACTION_ID_EXACT_RE.fullmatch(value)
        or SHELL_ACTION_ID_EXACT_RE.fullmatch(value)
        or value == ROUTE_FALLBACK_ACTION_ID
    )
NON_DOM_ACTION_EVENTS = {
    "agent_execute",
    "drag",
    "init",
    "open_app",
    "scroll",
    "scroll_viewport",
    "swipe",
    "terminate",
    "wheel_drag",
}

NON_INTERACTIVE_ACTION_EVENTS = {
    "agent_execute",
    "init",
    "open_app",
    "terminate",
}


def _field_role_from_intent(text: str) -> str:
    lowered = str(text or "").lower()
    if any(token in str(text or "") for token in ("出发", "左侧", "起点", "始发")) or "origin" in lowered or "from" in lowered:
        return "origin"
    if any(token in str(text or "") for token in ("目的", "到达", "右侧", "终点")) or "destination" in lowered or "to" in lowered:
        return "destination"
    if any(token in str(text or "") for token in ("日期", "日历", "时间")) or "date" in lowered or "time" in lowered:
        return "date_or_time"
    return "selection"


def _looks_like_picker_activation(step: dict[str, Any]) -> bool:
    source = str(step.get("source_page") or "")
    target = str(step.get("target_page") or "")
    if not source or not target or source == target:
        return False
    target_lower = target.lower()
    if not any(token in target_lower for token in ("select", "selector", "station", "picker", "date", "calendar")):
        return False
    intent = str(step.get("intent") or "")
    return any(token in intent for token in ("出发", "到达", "目的", "左侧", "右侧", "日期", "日历", "选择"))


def _looks_like_picker_commit(step: dict[str, Any]) -> bool:
    if not step.get("previous_activation_intent"):
        return False
    operation = str(step.get("operation") or "").strip().lower()
    if operation not in {"click", "tap", "press", "double_click", "dblclick"}:
        return False
    source = str(step.get("source_page") or "")
    target = str(step.get("target_page") or "")
    next_source = str(step.get("next_source_page") or "")
    next_target = str(step.get("next_target_page") or "")
    closed_target = target
    if source and target and source == target and next_source and next_source != source:
        closed_target = next_source
    elif source and target and source == target and next_target and next_target != source:
        closed_target = next_target
    if not source or not closed_target or source == closed_target:
        return False
    intent = str(step.get("intent") or "")
    if any(token in intent for token in ("查看更多", "更多")) and any(
        token in intent for token in ("火车站", "车站", "候选", "列表", "结果")
    ):
        return False
    if any(token in intent for token in ("选择框", "勾选", "复选框", "checkbox")):
        return False
    if any(token in intent for token in ("输入框", "搜索框", "搜索输入框")) and any(
        token in intent for token in ("顶部", "车站", "搜索")
    ):
        return False
    return any(token in intent for token in ("搜索结果", "候选", "结果中的", "城市", "车站", "火车站", "选择"))


def _looks_like_edit_surface_route(step: dict[str, Any]) -> bool:
    operation = str(step.get("operation") or "").strip().lower()
    if operation not in {"click", "tap", "press", "double_click", "dblclick"}:
        return False
    source = str(step.get("source_page") or "")
    target = str(step.get("target_page") or "")
    next_source = str(step.get("next_source_page") or "")
    next_target = str(step.get("next_target_page") or "")
    if not source or not target or source != target:
        return False
    route_target = next_source if next_source and next_source != source else next_target
    if not route_target or route_target == source:
        return False
    next_lower = route_target.lower()
    if not any(token in next_lower for token in ("utility", "edit", "form", "input")):
        return False
    intent = str(step.get("intent") or "")
    return any(
        token in intent
        for token in (
            "输入栏",
            "输入框",
            "字段",
            "手机号",
            "手机号码",
            "证件号码",
            "编辑",
            "修改",
            "填写",
            "表单",
        )
    )


def _closed_target_from_next_source(
    effect_kind: str,
    source: str,
    target: str,
    next_source: str,
    next_target: str = "",
) -> str:
    if effect_kind not in {"select_result_return", "gesture_route", "route"}:
        return target
    if source and target and target == source and next_source and next_source != source:
        return next_source
    if source and target and target == source and next_target and next_target != source:
        return next_target
    return target


def make_action_id(trace_id: str, step_index: int | str) -> str:
    return f"{trace_id}:step{int(step_index)}"


def parse_action_id(action_id: str) -> tuple[str, int] | None:
    if ":step" not in action_id:
        return None
    trace_id, step = action_id.rsplit(":step", 1)
    if not trace_id or not step.isdigit():
        return None
    return trace_id, int(step)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def action_host_exclusion_reason(binding: dict[str, Any]) -> str:
    interaction = binding.get("interaction") if isinstance(binding.get("interaction"), dict) else {}
    payload = interaction.get("payload") if isinstance(interaction.get("payload"), dict) else {}
    if binding.get("dom_binding_required") is True:
        return ""
    if payload.get("dom_binding_required") is True:
        return ""
    gesture_scope = binding.get("gesture_scope") or payload.get("gesture_scope")
    state_refs = binding.get("state_refs") or payload.get("state_refs")
    if gesture_scope and gesture_scope != "viewport_scroll" and state_refs:
        return ""
    candidates = (
        interaction.get("event"),
        binding.get("event"),
        binding.get("operation"),
        binding.get("reducer"),
        binding.get("action_type"),
    )
    for candidate in candidates:
        event = str(candidate or "").strip().lower()
        if event in NON_DOM_ACTION_EVENTS:
            return f"{event}_action_no_dom_host_required"
    return ""


def action_requires_dom_host(binding: dict[str, Any]) -> bool:
    return not action_host_exclusion_reason(binding)


def checkpoint_source_actions(checkpoint: dict[str, Any]) -> list[str]:
    explicit = checkpoint.get("source_actions")
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit if x]

    runtime_state = checkpoint.get("global_runtime_state")
    if isinstance(runtime_state, dict):
        source_action_ids = runtime_state.get("source_action_ids")
        if isinstance(source_action_ids, list) and source_action_ids:
            return [str(x) for x in source_action_ids if x]

    trace_id = checkpoint.get("trace_id")
    actions: list[str] = []
    if trace_id:
        for step in checkpoint.get("source_steps") or []:
            try:
                actions.append(make_action_id(str(trace_id), int(step)))
            except (TypeError, ValueError):
                continue
    return actions


def normalize_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(checkpoint)
    normalized["source_actions"] = checkpoint_source_actions(checkpoint)
    return normalized


def infer_effect_kind(step: dict[str, Any], checkpoints: list[dict[str, Any]]) -> str:
    if _looks_like_picker_commit(step):
        return "select_result_return"
    if _looks_like_edit_surface_route(step):
        return "route"
    operation = str(step.get("operation") or step.get("action") or "").strip().lower()
    reducer = str(step.get("reducer") or "").strip().lower()
    if operation in NON_INTERACTIVE_ACTION_EVENTS or reducer in NON_INTERACTIVE_ACTION_EVENTS:
        return operation or reducer
    expected_target = step.get("expected_target_page") or step.get("target_page") or ""
    source_page = step.get("source_page") or step.get("filename") or ""
    if (
        operation in {"scroll", "drag", "swipe", "wheel_drag", "scroll_viewport"}
        and expected_target
        and expected_target != source_page
    ):
        return "gesture_route"
    for checkpoint in checkpoints:
        expected = checkpoint.get("expected_state") or {}
        if isinstance(expected, dict) and expected:
            if "page" in expected or "current_page" in expected:
                return "route"
            return "state"
    kinds = [cp.get("checkpoint_kind") for cp in checkpoints if cp.get("checkpoint_kind")]
    if kinds:
        return str(kinds[0])
    if expected_target and expected_target != source_page:
        return "route"
    if operation == "fill":
        return "draft"
    if operation in {"click", "tap"}:
        return "selection"
    return str(operation or "unknown")


def _steps_from_contract_replay_plan(contract_replay_plan: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for trace in contract_replay_plan.get("traces") or []:
        if not isinstance(trace, dict):
            continue
        trace_id = str(trace.get("trace_id") or "")
        trace_steps: list[dict[str, Any]] = []
        for step in trace.get("steps") or []:
            if not isinstance(step, dict):
                continue
            action_id = str(step.get("action_id") or "")
            parsed = parse_action_id(action_id) if action_id else None
            step_index = parsed[1] if parsed else step.get("step_index")
            try:
                step_index_int = int(step_index)
            except (TypeError, ValueError):
                continue
            transaction = step.get("transaction") if isinstance(step.get("transaction"), dict) else {}
            trace_steps.append(
                {
                    "trace_id": trace_id or (parsed[0] if parsed else ""),
                    "step_index": step_index_int,
                    "reference_step_index": step_index_int,
                    "action_id": action_id or make_action_id(trace_id, step_index_int),
                    "source_page": step.get("source_filename") or transaction.get("source_filename") or "",
                    "target_page": step.get("target_filename") or transaction.get("target_filename") or "",
                    "operation": step.get("operation") or transaction.get("operation") or "",
                    "intent": transaction.get("operation_intent") or step.get("operation_intent") or "",
                    "reducer": step.get("reducer") or transaction.get("reducer") or "",
                    "expected_state_diff": (
                        (step.get("assertions") or {}).get("expected_state_diff")
                        if isinstance(step.get("assertions"), dict)
                        else {}
                    )
                    or ((transaction.get("state_commit") or {}).get("expected_state_diff") if isinstance(transaction.get("state_commit"), dict) else {})
                    or {},
                    "state_refs": (
                        (transaction.get("state_commit") or {}).get("state_refs")
                        if isinstance(transaction.get("state_commit"), dict)
                        else []
                    )
                    or [],
                }
            )
        trace_steps.sort(key=lambda row: (int(row.get("step_index") or 0), str(row.get("action_id") or "")))
        previous_activation_intent = ""
        previous_activation_source = ""
        previous_activation_target = ""
        for index, row in enumerate(trace_steps):
            next_source = ""
            next_target = ""
            if index + 1 < len(trace_steps):
                next_source = str(trace_steps[index + 1].get("source_page") or "")
                next_target = str(trace_steps[index + 1].get("target_page") or "")
            row["next_source_page"] = next_source
            row["next_target_page"] = next_target
            row["previous_activation_intent"] = previous_activation_intent
            row["previous_activation_source"] = previous_activation_source
            row["previous_activation_target"] = previous_activation_target
            if _looks_like_picker_commit(row):
                previous_activation_intent = ""
                previous_activation_source = ""
                previous_activation_target = ""
            elif _looks_like_picker_activation(row):
                previous_activation_intent = str(row.get("intent") or "")
                previous_activation_source = str(row.get("source_page") or "")
                previous_activation_target = str(row.get("target_page") or "")
            steps.append(row)
    return steps


def build_action_execution_contracts_from_replay_plan(
    contract_replay_plan: dict[str, Any],
    checkpoint_matrix: dict[str, Any],
    action_effect_contracts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checkpoints = [normalize_checkpoint(cp) for cp in checkpoint_matrix.get("checkpoints", [])]
    checkpoints_by_action: dict[str, list[dict[str, Any]]] = {}
    for checkpoint in checkpoints:
        for action_id in checkpoint.get("source_actions") or []:
            checkpoints_by_action.setdefault(action_id, []).append(checkpoint)
    action_effect_by_id = {
        str(action.get("action_id") or ""): action
        for action in (action_effect_contracts or {}).get("actions", [])
        if isinstance(action, dict) and str(action.get("action_id") or "")
    }

    actions: list[dict[str, Any]] = []
    duplicate_action_ids: list[str] = []
    seen: set[str] = set()
    for step in _steps_from_contract_replay_plan(contract_replay_plan):
        action_id = str(step.get("action_id") or "")
        if not action_id:
            continue
        if action_id in seen:
            duplicate_action_ids.append(action_id)
            continue
        seen.add(action_id)
        bound_checkpoints = checkpoints_by_action.get(action_id, [])
        effect_kind = infer_effect_kind(
            {
                "operation": step.get("operation") or "",
                "expected_target_page": step.get("target_page") or "",
                "target_page": step.get("target_page") or "",
                "source_page": step.get("source_page") or "",
                "intent": step.get("intent") or "",
                "next_source_page": step.get("next_source_page") or "",
                "next_target_page": step.get("next_target_page") or "",
                "previous_activation_intent": step.get("previous_activation_intent") or "",
            },
            bound_checkpoints,
        )
        source_page = str(step.get("source_page") or "")
        target_page = _closed_target_from_next_source(
            effect_kind,
            source_page,
            str(step.get("target_page") or ""),
            str(step.get("next_source_page") or ""),
            str(step.get("next_target_page") or ""),
        )
        if effect_kind in NON_INTERACTIVE_ACTION_EVENTS:
            target_page = ""
        previous_activation_source = str(step.get("previous_activation_source") or "")
        previous_activation_target = str(step.get("previous_activation_target") or "")
        raw_target_page = str(step.get("target_page") or "")
        if (
            effect_kind == "select_result_return"
            and previous_activation_source
            and previous_activation_target
            and source_page == previous_activation_source
            and raw_target_page == previous_activation_source
        ):
            source_page = previous_activation_target
            target_page = previous_activation_source
        row = {
            "action_id": action_id,
            "trace_id": step.get("trace_id") or "",
            "step_index": step.get("step_index"),
            "reference_step_index": step.get("reference_step_index", step.get("step_index")),
            "source_page": source_page,
            "target_page": target_page,
            "operation": step.get("operation") or "",
            "intent": step.get("intent") or "",
            "reducer": step.get("reducer") or "",
            "effect_kind": effect_kind,
            "state_refs": step.get("state_refs") or [],
            "expected_state_diff": step.get("expected_state_diff") or {},
            "checkpoint_ids": [cp.get("checkpoint_id") for cp in bound_checkpoints if cp.get("checkpoint_id")],
            "binding_role": "contract_replay_action",
        }
        action_effect = action_effect_by_id.get(action_id)
        if action_effect:
            row.update(
                {
                    "source_page": action_effect.get("source_filename") or row["source_page"],
                    "target_page": action_effect.get("target_filename") or row["target_page"],
                    "operation": action_effect.get("operation") or row["operation"],
                    "reducer": action_effect.get("reducer") or row["reducer"],
                    "effect_kind": action_effect.get("effect_kind") or row["effect_kind"],
                    "state_refs": action_effect.get("state_refs") or row["state_refs"],
                    "expected_state_diff": action_effect.get("expected_state_diff") or row["expected_state_diff"],
                    "dom_binding_required": bool(action_effect.get("dom_binding_required")),
                    "dom_binding_ref": action_effect.get("dom_binding_ref") or "",
                    "transaction_id": action_effect.get("transaction_id") or "",
                    "required_runtime_events": action_effect.get("required_runtime_events") or [],
                    "gesture_scope": action_effect.get("gesture_scope") or "",
                    "component_ref": action_effect.get("component_ref") or "",
                    "downstream_expected_state_diff": action_effect.get("downstream_expected_state_diff") or {},
                    "legacy_state_refs": action_effect.get("legacy_state_refs") or action_effect.get("state_refs") or [],
                    "canonical_state_refs": action_effect.get("canonical_state_refs") or [],
                    "downstream_extension_refs": action_effect.get("downstream_extension_refs") or [],
                    "binding_role": "contract_replay_action+action_effect_authority",
                }
            )
            row["authority_sources"] = ["contract_replay_plan", "action_effect_contracts"]
        actions.append(row)

    missing_source_actions: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        source_actions = checkpoint.get("source_actions") or []
        missing = [action_id for action_id in source_actions if action_id not in seen]
        if missing or not source_actions:
            missing_source_actions.append(
                {
                    "trace_id": checkpoint.get("trace_id"),
                    "checkpoint_id": checkpoint.get("checkpoint_id"),
                    "source_steps": checkpoint.get("source_steps") or [],
                    "missing_source_action_ids": missing,
                }
            )

    actions_by_id = {row["action_id"]: row for row in actions}
    checkpoint_count = len(checkpoints)
    checkpoint_source_action_count = sum(1 for cp in checkpoints if cp.get("source_actions"))
    return {
        "format": "ghostweb_action_execution_contracts",
        "schema_revision": 2,
        "source": (
            "contract_replay_plan + checkpoint_matrix + action_effect_contracts"
            if action_effect_by_id
            else "contract_replay_plan + checkpoint_matrix"
        ),
        "action_count": len(actions),
        "checkpoint_count": checkpoint_count,
        "checkpoint_source_action_count": checkpoint_source_action_count,
        "missing_source_actions": missing_source_actions,
        "duplicate_action_ids": sorted(set(duplicate_action_ids)),
        "summary": {
            "checkpoint_rows_without_source_actions": sum(1 for cp in checkpoints if not cp.get("source_actions")),
            "missing_source_action_count": len(missing_source_actions),
            "duplicate_action_id_count": len(set(duplicate_action_ids)),
        },
        "action_execution_contracts": actions,
        "actions": actions,
        "actions_by_id": actions_by_id,
        "checkpoint_matrix": {"checkpoints": checkpoints},
        "checkpoints": checkpoints,
    }


def build_action_execution_contracts_from_formal_package_inputs(
    checkpoint_matrix: dict[str, Any],
    action_host_manifest: dict[str, Any],
    runtime_event_bindings: dict[str, Any],
) -> dict[str, Any]:
    checkpoints = [
        normalize_checkpoint(checkpoint)
        for checkpoint in checkpoint_matrix.get("checkpoints", [])
        if isinstance(checkpoint, dict)
    ]
    hosts_by_action: dict[str, list[dict[str, Any]]] = {}
    for host in action_host_manifest.get("hosts", []):
        if not isinstance(host, dict):
            continue
        action_id = str(host.get("action_id") or "").strip()
        if action_id:
            hosts_by_action.setdefault(action_id, []).append(host)

    bindings_by_action: dict[str, list[dict[str, Any]]] = {}
    for binding in runtime_event_bindings.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        for action_id in binding.get("source_action_ids") or []:
            action_id = str(action_id or "").strip()
            if action_id:
                bindings_by_action.setdefault(action_id, []).append(binding)

    checkpoints_by_action: dict[str, list[dict[str, Any]]] = {}
    for checkpoint in checkpoints:
        for action_id in checkpoint.get("source_actions") or []:
            checkpoints_by_action.setdefault(action_id, []).append(checkpoint)


    action_ids = {
        action_id
        for action_id in set(hosts_by_action) | set(checkpoints_by_action)
        if parse_action_id(action_id) is not None
    }
    actions: list[dict[str, Any]] = []
    for action_id in sorted(
        action_ids,
        key=lambda value: (
            (parse_action_id(value) or ("", -1))[0],
            (parse_action_id(value) or ("", -1))[1],
            value,
        ),
    ):
        trace_id, step_index = parse_action_id(action_id) or ("", 0)
        action_hosts = hosts_by_action.get(action_id, [])
        action_bindings = bindings_by_action.get(action_id, [])
        action_checkpoints = checkpoints_by_action.get(action_id, [])
        primary_host = action_hosts[0] if action_hosts else {}
        primary_binding = action_bindings[0] if action_bindings else {}
        payload_required = (
            primary_binding.get("payload_required")
            if isinstance(primary_binding.get("payload_required"), dict)
            else {}
        )
        expected_state_diff = (
            primary_binding.get("expected_state_diff")
            if isinstance(primary_binding.get("expected_state_diff"), dict)
            else {}
        )
        if not expected_state_diff and action_checkpoints:
            runtime_state = action_checkpoints[0].get("global_runtime_state")
            expected_state_diff = (
                runtime_state.get("expected_state_diff")
                if isinstance(runtime_state, dict)
                and isinstance(runtime_state.get("expected_state_diff"), dict)
                else action_checkpoints[0].get("expected_state") or {}
            )
        checkpoint_ids = [
            str(checkpoint.get("checkpoint_id"))
            for checkpoint in action_checkpoints
            if checkpoint.get("checkpoint_id")
        ]


        onload_outcome_observer = bool(
            not action_hosts
            and primary_binding.get("checkpoint_kind") == "outcome"
            and str(primary_binding.get("event") or "").startswith("ghostweb:checkpoint:")
            and primary_binding.get("terminal_observable_page_filename")
        )
        source_page = str(
            primary_binding.get("injected_page")
            or primary_binding.get("terminal_observable_page_filename")
            or primary_host.get("source_page")
            or primary_host.get("page")
            or primary_host.get("filename")
            or ""
        )
        target_page = str(
            payload_required.get("target_filename")
            or (primary_binding.get("expected_state") or {}).get("target_filename")
            or primary_host.get("target_filename")
            or source_page
        )
        state_refs = list(primary_binding.get("state_refs") or [])
        if not state_refs and action_checkpoints:
            runtime_state = action_checkpoints[0].get("global_runtime_state")
            if isinstance(runtime_state, dict):
                state_refs = list(runtime_state.get("state_refs") or [])
        effect_kind = str(payload_required.get("effect_kind") or "").strip()
        if not effect_kind:
            effect_kind = infer_effect_kind(
                {
                    "operation": primary_binding.get("operation") or primary_host.get("event_type") or "",
                    "source_page": source_page,
                    "target_page": target_page,
                },
                action_checkpoints,
            )
        actions.append(
            {
                "action_id": action_id,
                "trace_id": trace_id,
                "step_index": step_index,
                "reference_step_index": step_index,
                "source_page": source_page,
                "target_page": target_page,
                "operation": str(primary_binding.get("operation") or primary_host.get("event_type") or ""),
                "intent": str(primary_binding.get("operation_intent") or ""),
                "reducer": "",
                "effect_kind": effect_kind,
                "state_refs": state_refs,
                "expected_state_diff": expected_state_diff,
                "checkpoint_ids": checkpoint_ids,
                "dom_binding_required": bool(action_hosts),
                "host_binding_status": (
                    "bound" if len(action_hosts) == 1 else "ambiguous" if action_hosts else "not_required_onload_observer" if onload_outcome_observer else "unbound"
                ),
                "host_candidate_count": len(action_hosts),
                "host_exclusion_reason": (
                    "outcome_observer_emitted_on_page_load" if onload_outcome_observer else ""
                ),
                "binding_role": "sealed_package_formal_inputs",
                "authority_sources": [
                    source
                    for source, present in (
                        ("action_host_manifest", bool(action_hosts)),
                        ("runtime_event_bindings", bool(action_bindings)),
                        ("checkpoint_matrix", bool(action_checkpoints)),
                    )
                    if present
                ],
            }
        )

    missing_source_actions = [
        {
            "trace_id": checkpoint.get("trace_id"),
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "source_steps": checkpoint.get("source_steps") or [],
            "missing_source_action_ids": [
                action_id
                for action_id in checkpoint.get("source_actions") or []
                if parse_action_id(action_id) is not None and action_id not in action_ids
            ],
        }
        for checkpoint in checkpoints
        if any(
            parse_action_id(action_id) is not None and action_id not in action_ids
            for action_id in checkpoint.get("source_actions") or []
        )
    ]
    actions_by_id = {row["action_id"]: row for row in actions}
    return {
        "format": "ghostweb_action_execution_contracts",
        "schema_revision": 3,
        "source": "sealed_package_action_host_manifest + runtime_event_bindings + checkpoint_matrix",
        "action_count": len(actions),
        "checkpoint_count": len(checkpoints),
        "checkpoint_source_action_count": sum(
            1 for checkpoint in checkpoints if checkpoint.get("source_actions")
        ),
        "missing_source_actions": missing_source_actions,
        "duplicate_action_ids": [],
        "summary": {
            "checkpoint_rows_without_source_actions": sum(
                1 for checkpoint in checkpoints if not checkpoint.get("source_actions")
            ),
            "missing_source_action_count": len(missing_source_actions),
            "duplicate_action_id_count": 0,
            "visible_host_bound_action_count": sum(1 for row in actions if row["host_binding_status"] == "bound"),
            "ambiguous_visible_host_action_count": sum(1 for row in actions if row["host_binding_status"] == "ambiguous"),
            "onload_outcome_observer_action_count": sum(1 for row in actions if row["host_binding_status"] == "not_required_onload_observer"),
            "unbound_action_count": sum(1 for row in actions if row["host_binding_status"] == "unbound"),
        },
        "action_execution_contracts": actions,
        "actions": actions,
        "actions_by_id": actions_by_id,
        "checkpoint_matrix": {"checkpoints": checkpoints},
        "checkpoints": checkpoints,
    }


def build_action_execution_contracts(
    replay_results: dict[str, Any],
    checkpoint_matrix: dict[str, Any],
) -> dict[str, Any]:
    checkpoints = [normalize_checkpoint(cp) for cp in checkpoint_matrix.get("checkpoints", [])]
    checkpoints_by_action: dict[str, list[dict[str, Any]]] = {}
    for checkpoint in checkpoints:
        for action_id in checkpoint.get("source_actions") or []:
            checkpoints_by_action.setdefault(action_id, []).append(checkpoint)

    actions: list[dict[str, Any]] = []
    missing_source_actions: list[dict[str, Any]] = []

    for trace_id, steps in sorted(replay_results.items()):
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_index = step.get("step_index")
            if step_index is None:
                continue
            try:
                step_index_int = int(step_index)
            except (TypeError, ValueError):
                continue
            action_id = make_action_id(trace_id, step_index_int)
            bound_checkpoints = checkpoints_by_action.get(action_id, [])
            actions.append(
                {
                    "action_id": action_id,
                    "trace_id": trace_id,
                    "step_index": step_index_int,
                    "reference_step_index": step.get("reference_step_index", step_index_int),
                    "source_page": step.get("source_page") or step.get("filename") or "",
                    "target_page": step.get("expected_target_page") or step.get("actual_page_after") or step.get("target") or "",
                    "operation": step.get("operation") or step.get("action") or "",
                    "intent": step.get("action") or step.get("operation_intent") or step.get("target") or "",
                    "effect_kind": infer_effect_kind(step, bound_checkpoints),
                    "checkpoint_ids": [cp.get("checkpoint_id") for cp in bound_checkpoints if cp.get("checkpoint_id")],
                    "binding_role": "source_action",
                }
            )

    actions_by_id = {row["action_id"]: row for row in actions}
    for checkpoint in checkpoints:
        if checkpoint.get("source_actions"):
            continue
        missing_source_actions.append(
            {
                "trace_id": checkpoint.get("trace_id"),
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "source_steps": checkpoint.get("source_steps") or [],
            }
        )

    checkpoint_count = len(checkpoints)
    checkpoint_source_action_count = sum(1 for cp in checkpoints if cp.get("source_actions"))
    return {
        "format": "ghostweb_action_execution_contracts",
        "schema_revision": 1,
        "action_count": len(actions),
        "checkpoint_count": checkpoint_count,
        "checkpoint_source_action_count": checkpoint_source_action_count,
        "missing_source_actions": missing_source_actions,
        "summary": {
            "checkpoint_rows_without_source_actions": len(missing_source_actions),
        },
        "action_execution_contracts": actions,
        "actions": actions,
        "actions_by_id": actions_by_id,
        "checkpoint_matrix": {"checkpoints": checkpoints},
        "checkpoints": checkpoints,
    }


def contracts_by_source_page(contracts: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_page: dict[str, list[dict[str, Any]]] = {}
    for action in contracts.get("actions") or []:
        page = action.get("source_page") or ""
        if page:
            by_page.setdefault(page, []).append(action)
    return by_page


def checkpoints_by_action(contracts: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_action: dict[str, list[dict[str, Any]]] = {}
    for checkpoint in contracts.get("checkpoints") or []:
        for action_id in checkpoint.get("source_actions") or []:
            by_action.setdefault(action_id, []).append(checkpoint)
    return by_action


def action_ids_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for match in ACTION_ID_RE.finditer(text or ""):
        action_id = match.group(1)
        if action_id not in seen:
            seen.add(action_id)
            ids.append(action_id)
    for match in SHELL_ACTION_ID_RE.finditer(text or ""):
        action_id = match.group(1)
        if action_id not in seen:
            seen.add(action_id)
            ids.append(action_id)
    return ids


def strip_script_blocks(html: str) -> str:
    return SCRIPT_BLOCK_RE.sub("", html or "")


def _attrs_from_text(attrs: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for match in ATTR_RE.finditer(attrs or ""):
        parsed[match.group("name").lower()] = match.group("dq") if match.group("dq") is not None else match.group("sq") or ""
    return parsed


def action_ids_from_attrs(attrs: dict[str, str]) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for attr_name in ACTION_HOST_ATTRS:
        value = str(attrs.get(attr_name, "") or "").strip()
        if not value:
            continue
        if attr_name in {"data-action-id", "data-ghostweb-action-id"}:
            candidates = [value] if _is_exact_action_id(value) else []
        elif attr_name == "data-action-aliases":
            candidates = [token for token in value.split() if _is_exact_action_id(token)]
        else:
            candidates = action_ids_from_text(value)
        for action_id in candidates:
            if action_id not in seen:
                seen.add(action_id)
                ids.append(action_id)
    return ids


def action_ids_from_dom_hosts(html: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for match in OPEN_TAG_RE.finditer(strip_script_blocks(html)):
        attrs = _attrs_from_text(match.group("attrs") or "")
        for action_id in action_ids_from_attrs(attrs):
            if action_id not in seen:
                seen.add(action_id)
                ids.append(action_id)
    return ids


def _script_blocks(html: str) -> list[str]:
    return [match.group(0) for match in SCRIPT_BLOCK_RE.finditer(html or "")]


def _selector_for_attr(attr_name: str, value: str, action_id: str) -> str:
    if value == action_id:
        return f'[{attr_name}="{action_id}"]'
    if attr_name == "data-action-aliases" and action_id in value.split():
        return f'[{attr_name}~="{action_id}"]'
    if attr_name in {"data-element-id", "data-element-aliases"} and action_id in value:
        return f'[{attr_name}*="{action_id}"]'
    return ""


def _selector_for_action(attrs: dict[str, str], action_id: str) -> str:
    for attr_name in ACTION_HOST_ATTRS:
        value = attrs.get(attr_name, "")
        if attr_name in {"data-action-id", "data-ghostweb-action-id"} and value != action_id:
            continue
        if attr_name == "data-action-aliases" and action_id not in value.split():
            continue
        if attr_name in {"data-element-id", "data-element-aliases"} and action_id not in value:
            continue
        return _selector_for_attr(attr_name, value, action_id)
    return ""


def _event_types_for_host(tag: str, attrs: dict[str, str]) -> list[str]:
    if attrs.get("data-gesture-host") or attrs.get("data-ghostweb-gesture-action"):
        return ["wheel", "scroll", "pointerup", "touchend"]
    tag_lower = tag.lower()
    if tag_lower in {"input", "textarea", "select"}:
        return ["input", "change"]
    return ["click"]


def _state_fields_for_host(attrs: dict[str, str]) -> list[str]:
    raw_values = [
        attrs.get("data-state-ref", ""),
        attrs.get("data-state-refs", ""),
        attrs.get("data-ghostweb-state-ref", ""),
        attrs.get("data-ghostweb-state-refs", ""),
    ]
    fields: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        text = html_lib.unescape(str(raw)).strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            candidates = [str(item).strip() for item in parsed]
        else:
            candidates = [part.strip() for part in re.split(r"[\s,]+", text) if part.strip()]
        for candidate in candidates:
            if candidate and candidate not in fields:
                fields.append(candidate)
    return fields


def _runtime_event_for_host(attrs: dict[str, str]) -> str:
    if attrs.get("data-ghostweb-effect-kind") == "same_page_mutation":
        return "same_page_state_mutated"
    if attrs.get("data-gesture-host") or attrs.get("data-ghostweb-gesture-action"):
        return str(attrs.get("data-ghostweb-runtime-event") or "gesture")
    return str(attrs.get("data-ghostweb-runtime-event") or "")


def _script_has_dynamic_action_host(script_source: str, action_id: str) -> bool:
    for match in re.finditer(re.escape(action_id), script_source or ""):
        start = max(0, match.start() - 240)
        end = min(len(script_source), match.end() + 240)
        window = script_source[start:end]
        if (
            "data-action-id" in window
            or "data-action-aliases" in window
            or "actionId" in window
            or "setAttribute" in window
        ):
            return True
    return False


def _target_filename_for_action(attrs: dict[str, str], action_id: str) -> str:
    raw_target_map = str(attrs.get("data-ghostweb-target-map") or "").strip()
    if raw_target_map:
        try:
            target_map = json.loads(html_lib.unescape(raw_target_map))
        except json.JSONDecodeError:
            target_map = {}
        if isinstance(target_map, dict):
            target = str(target_map.get(action_id) or "").strip()
            if target:
                return target
    return attrs.get("data-ghostweb-target", "")


def _dynamic_host_contract_for_action(
    *,
    script_source: str,
    action_id: str,
    expected_action_semantics: dict[str, dict[str, Any]],
) -> dict[str, str] | None:
    expectation = expected_action_semantics.get(action_id)
    if not isinstance(expectation, dict):
        return None
    marker = str(expectation.get("dynamic_host_marker") or "").strip()
    selector = str(expectation.get("dynamic_host_selector") or "").strip()
    target = str(expectation.get("target_filename") or "").strip()
    source_contract = str(expectation.get("source_contract") or "").strip()
    if not marker or marker not in script_source or not selector or not target:
        return None
    return {
        "host_selector": selector,
        "target_filename": target,
        "source_contract": source_contract or f"binding::{action_id}",
    }


def _is_retired_action_host(attrs: dict[str, str]) -> bool:
    for name, value in attrs.items():
        marker = f"{name} {value}".lower()
        if "retired" in marker:
            return True
    return False


def component_identity_from_attrs(attrs: dict[str, str]) -> dict[str, Any]:

    def usable(value: Any) -> str:
        candidate = str(value or "").strip()
        return "" if not candidate or _is_exact_action_id(candidate) else candidate

    legacy_component_id = usable(attrs.get("data-component-id"))
    if legacy_component_id:
        return {
            "component_id": legacy_component_id,
            "component_identity_status": "present",
            "component_identity_source": "data-component-id",
        }

    primary_component_id = usable(attrs.get("data-element-id"))
    if primary_component_id:
        return {
            "component_id": primary_component_id,
            "component_identity_status": "present",
            "component_identity_source": "data-element-id",
        }

    alias_candidates: list[str] = []
    for token in str(attrs.get("data-element-aliases") or "").split():
        candidate = usable(token)
        if candidate and candidate not in alias_candidates:
            alias_candidates.append(candidate)
    if len(alias_candidates) == 1:
        return {
            "component_id": alias_candidates[0],
            "component_identity_status": "present",
            "component_identity_source": "data-element-aliases",
        }
    if len(alias_candidates) > 1:
        return {
            "component_id": "",
            "component_identity_status": "ambiguous",
            "component_identity_source": "data-element-aliases",
            "component_identity_candidates": alias_candidates,
        }
    return {
        "component_id": "",
        "component_identity_status": "missing",
        "component_identity_source": "",
    }


def _normalize_host_semantic_text(value: Any) -> str:
    return re.sub(r"\s+", "", html_lib.unescape(str(value or ""))).casefold()


def _element_text_after_open_tag(source: str, match: re.Match[str], tag: str) -> str:
    closing = re.search(rf"</{re.escape(tag)}\s*>", source[match.end() :], re.IGNORECASE)
    if not closing:
        return ""
    fragment = source[match.end() : match.end() + closing.start()]
    fragment = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>", " ", fragment)
    fragment = re.sub(r"(?is)<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(fragment)).strip()


def build_action_host_manifest_for_page(
    page_name: str,
    html: str,
    expected_action_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    expected_action_semantics: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    expected = set(expected_action_ids or [])
    semantics = (
        expected_action_semantics
        if isinstance(expected_action_semantics, dict)
        else {}
    )
    expected.update(str(action_id) for action_id in semantics if str(action_id))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    dom_action_ids: set[str] = set()

    def explicit_action_ids(source: str) -> set[str]:
        ids: set[str] = set()
        for match in OPEN_TAG_RE.finditer(source):
            attrs = _attrs_from_text(match.group("attrs") or "")
            if _is_retired_action_host(attrs):
                continue
            for name in (
                "data-action-id",
                "data-ghostweb-action-id",
                "data-action-aliases",
            ):
                value = str(attrs.get(name, "") or "").strip()
                if not value:
                    continue
                if name in {"data-action-id", "data-ghostweb-action-id"}:
                    candidates = [value] if _is_exact_action_id(value) else []
                else:
                    candidates = [token for token in value.split() if _is_exact_action_id(token)]
                for candidate in candidates:
                    if not expected or candidate in expected:
                        ids.add(candidate)
        return ids

    def add_rows(source: str, authority: str) -> None:
        explicit_ids = explicit_action_ids(source)
        for match in OPEN_TAG_RE.finditer(source):
            tag = match.group("tag")
            attrs = _attrs_from_text(match.group("attrs") or "")
            if _is_retired_action_host(attrs):
                continue
            for action_id in action_ids_from_attrs(attrs):
                if expected and action_id not in expected:
                    continue
                selector = _selector_for_action(attrs, action_id)
                if not selector:
                    continue
                if selector.startswith("[data-element-") and action_id in explicit_ids:
                    continue
                if authority == "dynamic_action_host_template" and action_id in dom_action_ids:
                    continue
                key = (action_id, selector)
                if key in seen:
                    continue
                seen.add(key)
                if authority == "dom_action_host":
                    dom_action_ids.add(action_id)
                event_types = _event_types_for_host(tag, attrs)
                element_text = _element_text_after_open_tag(source, match, tag)
                accessible_name = str(attrs.get("aria-label") or element_text).strip()
                component_identity = component_identity_from_attrs(attrs)
                rows.append(
                    {
                        "action_id": action_id,
                        "page": page_name,
                        "filename": page_name,
                        "host_selector": selector,
                        "selector": selector,
                        "event_types": event_types,
                        "event_type": event_types[0],
                        **component_identity,
                        "host_tag": tag.lower(),
                        "element_role": attrs.get("role", tag.lower()),
                        "element_text": element_text,
                        "accessible_name": accessible_name,
                        "target_filename": _target_filename_for_action(attrs, action_id),
                        "runtime_event": _runtime_event_for_host(attrs),
                        "state_fields": _state_fields_for_host(attrs),
                        "source_contract": attrs.get("data-ghostweb-dom-binding-id", ""),
                        "authority": authority,
                    }
                )

    static_source = strip_script_blocks(html)
    script_source = "\n".join(_script_blocks(html))
    add_rows(static_source, "dom_action_host")
    add_rows(script_source, "dynamic_action_host_template")
    present_action_ids = {str(row.get("action_id") or "") for row in rows if row.get("action_id")}
    for action_id in sorted(expected - present_action_ids):
        contract = _dynamic_host_contract_for_action(
            script_source=script_source,
            action_id=action_id,
            expected_action_semantics=semantics,
        )
        if contract is None and not _script_has_dynamic_action_host(script_source, action_id):
            continue
        selector = contract["host_selector"] if contract else f'[data-action-id="{action_id}"]'
        key = (action_id, selector)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "action_id": action_id,
                "page": page_name,
                "filename": page_name,
                "host_selector": selector,
                "selector": selector,
                "event_types": ["click"],
                "event_type": "click",
                "component_id": "",
                "host_tag": "",
                "element_role": "",
                "element_text": "",
                "target_filename": contract["target_filename"] if contract else "",
                "runtime_event": "",
                "state_fields": [],
                "source_contract": contract["source_contract"] if contract else f"binding::{action_id}",
                "authority": "dynamic_action_contract" if contract else "dynamic_action_binding",
            }
        )
    return rows


def validate_action_host_semantics(
    expected_action_semantics: dict[str, dict[str, Any]],
    manifest_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    rows_by_action: dict[str, list[dict[str, Any]]] = {}
    for row in manifest_rows:
        action_id = str(row.get("action_id") or "")
        if action_id:
            rows_by_action.setdefault(action_id, []).append(row)

    mismatches: list[dict[str, Any]] = []
    checked_action_ids: list[str] = []
    for action_id, expectation in sorted(expected_action_semantics.items()):
        rows = rows_by_action.get(str(action_id), [])
        if len(rows) != 1:
            continue
        checked_action_ids.append(str(action_id))
        row = rows[0]
        confidence = str(expectation.get("confidence") or "").strip().lower()
        require_label = bool(expectation.get("require_visible_label")) or confidence == "observed"
        labels = [
            str(label).strip()
            for label in expectation.get("labels", [])
            if str(label).strip()
        ]
        host_name = str(row.get("accessible_name") or row.get("element_text") or "").strip()
        if require_label and labels:
            normalized_host = _normalize_host_semantic_text(host_name)
            label_match = any(
                _normalize_host_semantic_text(label) in normalized_host
                for label in labels
                if _normalize_host_semantic_text(label)
            )
            if not label_match:
                mismatches.append(
                    {
                        "action_id": action_id,
                        "reason": "visible_or_accessible_label_mismatch",
                        "expected_labels": labels,
                        "observed_accessible_name": host_name,
                        "host_selector": row.get("host_selector", ""),
                    }
                )
        expected_target = str(expectation.get("target_filename") or "").strip()
        observed_target = str(row.get("target_filename") or "").strip()
        if expected_target and observed_target != expected_target:
            mismatches.append(
                {
                    "action_id": action_id,
                    "reason": "target_filename_mismatch",
                    "expected_target_filename": expected_target,
                    "observed_target_filename": observed_target,
                    "host_selector": row.get("host_selector", ""),
                }
            )
    return {
        "checked_action_ids": checked_action_ids,
        "semantic_mismatches": mismatches,
        "semantic_mismatch_count": len(mismatches),
    }


def validate_action_host_coverage(
    expected_action_ids: set[str] | list[str] | tuple[str, ...],
    manifest_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = sorted(set(expected_action_ids))
    present = sorted({str(row.get("action_id")) for row in manifest_rows if row.get("action_id")})
    counts: dict[str, int] = {}
    for row in manifest_rows:
        action_id = str(row.get("action_id") or "")
        if action_id:
            counts[action_id] = counts.get(action_id, 0) + 1
    missing_component_id_hosts: list[dict[str, str]] = []
    ambiguous_component_id_hosts: list[dict[str, Any]] = []
    for row in manifest_rows:
        action_id = str(row.get("action_id") or "")
        event_types = {str(item) for item in row.get("event_types", [])}
        if (
            action_id not in expected
            or row.get("authority") != "dom_action_host"
            or "click" not in event_types
        ):
            continue
        host = {
            "action_id": action_id,
            "host_selector": str(row.get("host_selector") or ""),
            "page": str(row.get("page") or ""),
        }
        if str(row.get("component_identity_status") or "missing") == "ambiguous":
            ambiguous_component_id_hosts.append(
                {
                    **host,
                    "component_identity_candidates": list(
                        row.get("component_identity_candidates") or []
                    ),
                }
            )
        elif not str(row.get("component_id") or "").strip():
            missing_component_id_hosts.append(host)
    return {
        "expected_action_ids": expected,
        "present_action_ids": present,
        "missing_hosts": sorted(set(expected) - set(present)),
        "duplicate_hosts": sorted(action_id for action_id, count in counts.items() if count > 1),
        "missing_component_id_hosts": missing_component_id_hosts,
        "ambiguous_component_id_hosts": ambiguous_component_id_hosts,
        "missing_component_id_host_count": len(missing_component_id_hosts),
        "ambiguous_component_id_host_count": len(ambiguous_component_id_hosts),
    }


@dataclass(frozen=True)
class StepAccounting:
    successful_action_execution: int
    target_miss: int
    skipped_or_unknown: int


SUCCESSFUL_STEP_STATUSES = {
    "executed",
    "skipped_swipe",
    "skipped_agent",
}


def summarize_step_accounting(step_results: list[dict[str, Any]]) -> StepAccounting:
    successful = 0
    target_miss = 0
    skipped_or_unknown = 0
    for step in step_results:
        status = str(step.get("status") or "")
        if status == "executed":
            successful += 1
        elif status in {"no_target", "no_input", "no_input_text"} or status.startswith("fill_error"):
            target_miss += 1
        else:
            skipped_or_unknown += 1
    return StepAccounting(successful, target_miss, skipped_or_unknown)


def validate_html_action_authority(html: str) -> dict[str, Any]:
    has_action_map = "CP_MAP_BY_ACTION_ID" in html
    naked_step_map = bool(re.search(r"\bCP_MAP\s*=\s*\{", html))
    emit_has_context = "traceId" in html and "actionId" in html and "emitCheckpoint" in html
    host_action_ids = action_ids_from_dom_hosts(html)
    return {
        "has_action_id_map": has_action_map,
        "naked_step_index_authority": naked_step_map and not has_action_map,
        "emit_has_trace_action_context": emit_has_context,
        "action_ids": action_ids_from_text(html),
        "host_action_ids": host_action_ids,
        "script_only_action_ids": sorted(set(action_ids_from_text(html)) - set(host_action_ids)),
    }
