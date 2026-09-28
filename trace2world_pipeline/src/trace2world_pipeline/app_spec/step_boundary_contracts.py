
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping


RUNTIME_PROJECTION_DECISIONS = frozenset({
    "runtime_projection",
    "same_general_page_runtime_projection",
    "same_page_projection",
})

ROUTE_BOUNDARY_DECISIONS = frozenset({
    "route_transition",
    "materialized_page_split",
    "same_filename_materialized_page_transition",
})


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _base_filename(value: Any) -> str:
    text = _text(value)
    return text.split("?", 1)[0].split("#", 1)[0].split("/")[-1] if text else ""


def _step_index(step: Mapping[str, Any]) -> int:
    try:
        return int(step.get("step_index", -1))
    except (TypeError, ValueError):
        return -1


def _action_id(trace_id: str, step: Mapping[str, Any]) -> str:
    return _text(step.get("step_key") or step.get("action_id")) or f"{trace_id}:step{_step_index(step)}"


def _observation_key(
    trace_id: str,
    step_index: int,
    state: Mapping[str, Any],
) -> str:
    return _text(state.get("observation_key")) or f"{trace_id}:{step_index}"


def _binding(
    bindings_by_obs_key: Mapping[str, Any],
    obs_key: str,
) -> Dict[str, Any]:
    candidates = [obs_key]
    if ":step" in obs_key:
        trace_id, raw_step = obs_key.rsplit(":step", 1)
        candidates.append(f"{trace_id}:{raw_step}")
    else:
        trace_id, sep, raw_step = obs_key.rpartition(":")
        if sep and raw_step.isdigit():
            candidates.append(f"{trace_id}:step{raw_step}")
    for candidate in candidates:
        row = _as_dict(bindings_by_obs_key.get(candidate))
        if row:
            return dict(row)
    return {}


def _state_delta(source: Mapping[str, Any], target: Mapping[str, Any]) -> Dict[str, Any]:
    before = deepcopy(_as_dict(source.get("state_params")))
    after = deepcopy(_as_dict(target.get("state_params")))
    if before == after:
        return {}
    return {
        "kind": "binding_state_params_delta",
        "before": before,
        "after": after,
    }


def _explicit_decision(
    reviewed_boundary_decisions: Mapping[str, Any],
    action_id: str,
) -> Dict[str, Any]:
    actions_by_id = _as_dict(reviewed_boundary_decisions.get("actions_by_id"))
    return dict(_as_dict(actions_by_id.get(action_id)))


def _derived_boundary_decision(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    observed_source_filename: str,
    observed_target_filename: str,
    state_delta: Mapping[str, Any],
) -> str:
    source_general = _text(source.get("general_page_id"))
    target_general = _text(target.get("general_page_id"))
    source_materialized = _text(source.get("materialized_page_id"))
    target_materialized = _text(target.get("materialized_page_id"))
    if source_general and target_general and source_general != target_general:
        return "route_transition"
    if source_materialized and target_materialized and source_materialized != target_materialized:
        return "materialized_page_split"
    if observed_source_filename and observed_target_filename and observed_source_filename != observed_target_filename:
        return "route_transition"
    if state_delta:
        return "same_page_state_transition"
    return "same_page_no_state_change"


def _issue(code: str, action_id: str, message: str) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": "blocking",
        "action_id": action_id,
        "message": message,
    }


def _runtime_projection_issues(row: Mapping[str, Any]) -> list[Dict[str, Any]]:
    action_id = _text(row.get("action_id"))
    issues: list[Dict[str, Any]] = []
    canonical = _text(row.get("canonical_runtime_filename"))
    source_general = _text(row.get("source_general_page_id"))
    target_general = _text(row.get("target_general_page_id"))
    state_refs = [_text(ref) for ref in _as_list(row.get("state_refs")) if _text(ref)]
    expected_state_diff = _as_dict(row.get("expected_state_diff"))
    if not canonical or not source_general or not target_general:
        issues.append(_issue(
            "runtime_projection_canonical_identity_missing",
            action_id,
            "runtime projection requires canonical runtime filename and source/target General Page identity",
        ))
    elif source_general != target_general:
        issues.append(_issue(
            "runtime_projection_general_page_conflict",
            action_id,
            "runtime projection cannot cross General Page identity",
        ))
    if not state_refs or not expected_state_diff:
        issues.append(_issue(
            "runtime_projection_missing_state_contract",
            action_id,
            "runtime projection requires non-empty state_refs and expected_state_diff",
        ))
    return issues


def build_step_boundary_contracts(
    *,
    l0_execution_pack: Mapping[str, Any],
    materialized_observation_bindings: Mapping[str, Any],
    reviewed_boundary_decisions: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:

    reviewed_boundary_decisions = reviewed_boundary_decisions or {}
    bindings_by_obs_key = _as_dict(materialized_observation_bindings.get("bindings_by_obs_key"))
    rows: list[Dict[str, Any]] = []
    rows_by_action_id: Dict[str, Dict[str, Any]] = {}
    issues: list[Dict[str, Any]] = []

    for pack in _as_list(l0_execution_pack.get("action_packs")):
        pack = _as_dict(pack)
        trace_id = _text(pack.get("trace_id"))
        if not trace_id:
            continue
        for step in _as_list(pack.get("steps")):
            step = _as_dict(step)
            step_index = _step_index(step)
            if step_index < 0:
                continue
            action_id = _action_id(trace_id, step)
            pre_state = _as_dict(step.get("pre_state"))
            post_state = _as_dict(step.get("expected_post_state"))
            source_observation_key = _observation_key(trace_id, step_index, pre_state)
            post_observation_key = _observation_key(trace_id, step_index + 1, post_state)
            source_binding = _binding(bindings_by_obs_key, source_observation_key)
            target_binding = _binding(bindings_by_obs_key, post_observation_key)
            observed_source_filename = _base_filename(
                source_binding.get("filename") or pre_state.get("filename")
            )
            observed_target_filename = _base_filename(
                target_binding.get("filename") or post_state.get("filename")
            ) or observed_source_filename
            binding_state_delta = _state_delta(source_binding, target_binding)
            reviewed = _explicit_decision(reviewed_boundary_decisions, action_id)
            if not reviewed:
                reviewed = dict(
                    _as_dict(target_binding.get("page_boundary_authority"))
                    or _as_dict(source_binding.get("page_boundary_authority"))
                )
            decision = _text(
                reviewed.get("boundary_decision")
                or reviewed.get("transition_kind")
                or reviewed.get("disposition")
            ).lower()
            if not decision:
                decision = _derived_boundary_decision(
                    source_binding,
                    target_binding,
                    observed_source_filename,
                    observed_target_filename,
                    binding_state_delta,
                )

            source_general = _text(
                reviewed.get("source_general_page_id")
                or reviewed.get("general_page_id")
                or source_binding.get("general_page_id")
            )
            target_general = _text(
                reviewed.get("target_general_page_id")
                or reviewed.get("general_page_id")
                or target_binding.get("general_page_id")
            )
            canonical_runtime_filename = _base_filename(
                reviewed.get("canonical_runtime_filename")
                or reviewed.get("canonical_filename")
                or reviewed.get("canonical_materialized_filename")
            )
            if decision in RUNTIME_PROJECTION_DECISIONS:
                decision = "runtime_projection"
            elif not canonical_runtime_filename and decision not in RUNTIME_PROJECTION_DECISIONS:
                canonical_runtime_filename = observed_source_filename

            row: Dict[str, Any] = {
                "action_id": action_id,
                "trace_id": trace_id,
                "step_index": step_index,
                "source_observation_key": source_observation_key,
                "post_observation_key": post_observation_key,
                "boundary_decision": decision,
                "source_general_page_id": source_general,
                "target_general_page_id": target_general,
                "source_materialized_page_id": _text(
                    reviewed.get("source_materialized_page_id")
                    or source_binding.get("materialized_page_id")
                ),
                "target_materialized_page_id": _text(
                    reviewed.get("target_materialized_page_id")
                    or target_binding.get("materialized_page_id")
                ),
                "canonical_runtime_filename": canonical_runtime_filename,
                "observed_source_filename": observed_source_filename,
                "observed_target_filename": observed_target_filename,
                "state_delta": deepcopy(_as_dict(reviewed.get("state_delta")) or binding_state_delta),
                "state_refs": [
                    _text(ref) for ref in _as_list(reviewed.get("state_refs")) if _text(ref)
                ],
                "expected_state_diff": deepcopy(_as_dict(reviewed.get("expected_state_diff"))),
                "reducer": _text(reviewed.get("reducer")),
                "authority_ref": _text(
                    reviewed.get("authority_ref")
                    or reviewed_boundary_decisions.get("authority_ref")
                ),
                "authority_kind": "explicit_reviewed_boundary" if reviewed else "materialized_observation_identity",
            }
            row_issues = (
                _runtime_projection_issues(row)
                if decision == "runtime_projection"
                else []
            )
            row["conflict_status"] = "blocked" if row_issues else "pass"
            rows.append(row)
            rows_by_action_id[action_id] = row
            issues.extend(row_issues)

    return {
        "format": "step_boundary_contracts",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_role": "compiled_boundary_authority",
            "producer": "site_model.step_boundary_contracts.build_step_boundary_contracts",
            "derived_from": [
                "l0_execution_pack",
                "materialized_observation_bindings",
                "reviewed_boundary_decisions",
            ],
        },
        "summary": {
            "contract_count": len(rows),
            "runtime_projection_count": sum(
                1 for row in rows if row.get("boundary_decision") == "runtime_projection"
            ),
            "blocking_issue_count": len(issues),
            "status": "fail" if issues else "pass",
        },
        "contracts": rows,
        "actions_by_id": rows_by_action_id,
        "issues": issues,
    }


def boundary_contract_for_action(
    step_boundary_contracts: Mapping[str, Any] | None,
    action_id: str,
) -> Dict[str, Any]:

    payload = step_boundary_contracts if isinstance(step_boundary_contracts, Mapping) else {}
    return dict(_as_dict(_as_dict(payload.get("actions_by_id")).get(action_id)))
