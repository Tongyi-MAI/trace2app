
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping

from .global_execution_schema import SchemaObligation


SEARCH_FAMILY_ID = "search_interaction"

ROOT_SCHEMA_FIELD_PREFIXES = (
    "searchForm.",
    "stationPicker.",
    "queryState.",
    "selection.",
    "identityForm.",
    "identityProfile.",
    "personalIdentity.",
)

DOWNSTREAM_FIELD_PREFIXES = (
    "taskOutcome.",
    "checkpointState.",
    "runtimeState.",
    "runtime.",
    "ui.",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _append_unique(values: List[str], value: Any) -> None:
    text = _text(value)
    if text and text not in values:
        values.append(text)


def infer_search_field_path(payload: Mapping[str, Any]) -> str:

    joined = " ".join(
        _text(payload.get(key))
        for key in (
            "field_role",
            "parameter_id",
            "label",
            "component_label",
            "operation_intent",
            "result_projection",
            "query_value",
            "target_identity_hints",
            "caller_context",
        )
    ).lower()
    if (
        any(token in joined for token in ("travel_date", "日期", "出发日"))
        or re.search(r"\b(date|calendar)\b", joined)
    ):
        return "searchForm.date"
    if any(token in joined for token in ("origin", "from_station", "departure", "depart", "出发", "始发")):
        return "searchForm.origin"
    if any(token in joined for token in ("destination", "to_station", "arrival", "arrive", "到达", "目的")):
        return "searchForm.destination"
    if any(
        token in joined
        for token in (
            "station search",
            "station_search",
            "stationpicker",
            "station_selection",
            "station selection",
            "location_picker",
            "location picker",
            "c_station_selection",
            "车站",
            "站名",
        )
    ):
        return "stationPicker.query"
    return ""


def collect_obligations_from_page_environment_specs(page_environment_specs: Mapping[str, Any]) -> List[Dict[str, Any]]:
    specs = _as_dict(page_environment_specs.get("page_environment_specs_by_filename"))
    obligations: List[Dict[str, Any]] = []
    for filename, spec in specs.items():
        if not isinstance(spec, dict):
            continue
        _collect_parameterized_inputs(obligations, str(filename), spec)
        _collect_query_projections(obligations, str(filename), spec)
        _collect_transitions(obligations, str(filename), spec)
        _collect_runtime_refs(obligations, str(filename), spec)
    return obligations


def collect_obligations_from_runtime_transactions(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for trace in _as_list(payload.get("traces")):
        if not isinstance(trace, dict):
            continue
        trace_id = _text(trace.get("trace_id"))
        search_form = _as_dict(trace.get("search_form"))
        for field_name, field_path in {
            "origin_station": "searchForm.origin",
            "destination_station": "searchForm.destination",
            "travel_date": "searchForm.date",
        }.items():
            field = _as_dict(search_form.get(field_name))
            if not field:
                continue
            obligations.append(SchemaObligation(
                obligation_id=f"runtime_transaction::{trace_id}::{field_name}",
                obligation_kind="task_transaction_field",
                family_id=SEARCH_FAMILY_ID,
                field_path=field_path,
                field_type="StationRef" if field_name != "travel_date" else "DateValue",
                lifecycle="initial_and_after_commit",
                producers=[_text(field.get("caller_context") or field_name)],
                consumers=["search_submit"],
                evidence_steps=[f"{trace_id}:step{step}" for step in _as_list(field.get("source_steps"))],
                projection_pages=[str(page) for page in _as_list(field.get("projection_pages"))],
                aliases=[_text(field.get("state_ref"))],
                metadata={"expected_value": field.get("expected_value")},
            ).to_dict())
        submit = _as_dict(trace.get("submit"))
        for state_ref in _as_list(submit.get("state_reads")):
            obligations.append({
                "obligation_id": f"runtime_transaction::{trace_id}::submit::{_slug(state_ref)}",
                "obligation_kind": "submit_state_read",
                "family_id": SEARCH_FAMILY_ID,
                "field_path": _runtime_transaction_ref_to_field_path(state_ref),
                "field_type": "StateRef",
                "lifecycle": "read_on_submit",
                "producers": ["search_form_commit"],
                "consumers": ["search_submit"],
                "evidence_steps": [f"{trace_id}:step{submit.get('source_step')}"] if submit.get("source_step") is not None else [],
                "aliases": [_text(state_ref)],
            })
    return obligations


def collect_schema_obligations(
    *,
    page_environment_specs: Mapping[str, Any] | None = None,
    page_execution_obligations: Mapping[str, Any] | None = None,
    runtime_transactions: Mapping[str, Any] | None = None,
    trace_execution_obligations: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    runtime_state_obligations: Mapping[str, Any] | None = None,
    checkpoint_matrix: Mapping[str, Any] | None = None,
    runtime_state_schema: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    obligations: List[Dict[str, Any]] = []
    if trace_execution_obligations:
        obligations.extend(collect_obligations_from_trace_execution_obligations(trace_execution_obligations))
    if page_execution_obligations:
        obligations.extend(collect_obligations_from_page_execution_obligations(page_execution_obligations))
    if runtime_state_obligations:
        obligations.extend(collect_obligations_from_runtime_state_obligations(runtime_state_obligations))
    if page_environment_specs:
        obligations.extend(collect_obligations_from_page_environment_specs(page_environment_specs))
    if runtime_transactions:
        obligations.extend(collect_obligations_from_runtime_transactions(runtime_transactions))
    if checkpoint_matrix:
        obligations.extend(collect_obligations_from_checkpoint_matrix(checkpoint_matrix))
    if runtime_state_schema:
        obligations.extend(collect_obligations_from_runtime_state_schema(runtime_state_schema))
    return {
        "format": "schema_obligations_v1",
        "obligation_count": len(obligations),
        "obligations": obligations,
    }


def canonicalize_runtime_state_obligations_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:

    result = dict(payload or {})
    raw_obligations = payload.get("obligations", []) if isinstance(payload, Mapping) else []
    canonicalized: List[Dict[str, Any]] = []
    unresolved_required: List[Dict[str, Any]] = []
    resolved_required = 0
    not_schema_relevant = 0
    for idx, item in enumerate(raw_obligations if isinstance(raw_obligations, list) else []):
        if not isinstance(item, Mapping):
            continue
        cloned = dict(item)
        field_paths = _runtime_state_obligation_field_paths(item)
        required = _runtime_state_obligation_requires_canonical_owner(item)
        if field_paths:
            resolution = _runtime_state_canonical_resolution(item, field_paths[0], required=required)
            resolution["owner_field_paths"] = field_paths
            if required:
                resolved_required += 1
        elif required:
            resolution = {
                "required": True,
                "status": "unresolved",
                "owner_kind": "",
                "owner_field_path": "",
                "owner_field_paths": [],
                "canonical_state_ref": "",
                "canonical_entity": "",
                "expected_state_delta": _runtime_state_expected_delta(item),
                "expected_value_hints": _as_list(item.get("value_hints")),
                "resolution_source": "runtime_state_obligation_field_paths",
                "reason": "runtime_state_obligation_has_no_canonical_field_path",
            }
            unresolved_required.append({
                "obligation_id": _text(item.get("obligation_id")) or f"runtime_state_obligation::{idx}",
                "state_scope": item.get("state_scope"),
                "reason": resolution["reason"],
            })
        else:
            resolution = {
                "required": False,
                "status": "not_schema_relevant",
                "owner_kind": "not_schema_relevant",
                "owner_field_path": "",
                "owner_field_paths": [],
                "canonical_state_ref": "",
                "canonical_entity": "",
                "expected_state_delta": {},
                "expected_value_hints": _as_list(item.get("value_hints")),
                "resolution_source": "runtime_state_obligation_field_paths",
                "reason": "runtime_state_obligation_is_ui_layout_or_checkpoint_owned_state",
            }
            not_schema_relevant += 1
        cloned["canonical_resolution"] = resolution
        canonicalized.append(cloned)

    result["format"] = "runtime_state_obligations_canonicalized"
    result["schema_revision"] = max(int(result.get("schema_revision", 1) or 1), 2)
    result["obligations"] = canonicalized
    result["canonical_resolution_summary"] = {
        "obligation_count": len(canonicalized),
        "resolved_required": resolved_required,
        "unresolved_required": len(unresolved_required),
        "not_schema_relevant": not_schema_relevant,
    }
    result["unresolved_required"] = unresolved_required
    return result


def collect_obligations_from_trace_execution_obligations(
    payload: Mapping[str, Any] | Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    raw_obligations = payload.get("obligations", []) if isinstance(payload, Mapping) else payload
    obligations: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_obligations if isinstance(raw_obligations, list) else []):
        if not isinstance(item, Mapping):
            continue
        field_path = infer_search_field_path({
            "operation_intent": item.get("operation_intent"),
            "input_text": item.get("input_text"),
            "source_filename": item.get("source_filename"),
            "target_filename": item.get("expected_filename"),
            "caller_context": item.get("command"),
        })
        if not field_path and _looks_like_station_query({
            "source_filename": item.get("source_filename"),
            "target_filename": item.get("expected_filename"),
            "query_value": item.get("input_text"),
            "result_projection": item.get("operation_intent"),
        }):
            field_path = "stationPicker.query"
        if not field_path:
            continue
        trace_id = _text(item.get("trace_id"))
        step_index = item.get("step_index")
        obligations.append({
            "obligation_id": f"trace_execution::{trace_id or 'trace'}::{idx}",
            "obligation_kind": "pre_pageenv_trace_action",
            "family_id": _family_for_field(field_path, item),
            "field_path": field_path,
            "field_type": _field_type_for_path(field_path),
            "lifecycle": "pre_pageenv_action_semantics",
            "producers": ["trace_execution_obligations"],
            "consumers": [_text(item.get("source_filename")), _text(item.get("expected_filename"))],
            "evidence_steps": [f"{trace_id}:step{step_index}"] if trace_id and step_index not in (None, "") else [],
            "projection_pages": [
                value for value in [_text(item.get("source_filename")), _text(item.get("expected_filename"))] if value
            ],
            "metadata": {
                "operation": item.get("operation"),
                "operation_intent": item.get("operation_intent"),
                "input_text": item.get("input_text"),
            },
        })
    return obligations


def collect_obligations_from_page_execution_obligations(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:

    obligations: List[Dict[str, Any]] = []
    raw_packs = payload.get("action_packs", []) if isinstance(payload, Mapping) else []
    for pack in raw_packs if isinstance(raw_packs, list) else []:
        if not isinstance(pack, Mapping):
            continue
        trace_id = _text(pack.get("trace_id"))
        active_station_target = ""
        for idx, step in enumerate(_as_list(pack.get("steps"))):
            if not isinstance(step, Mapping):
                continue
            action = _as_dict(step.get("action"))
            pre_state = _as_dict(step.get("pre_state"))
            post_state = _as_dict(step.get("expected_post_state"))
            source_filename = _text(pre_state.get("filename"))
            target_filename = _text(post_state.get("filename"))
            step_index = step.get("step_index", idx)
            text = " ".join(
                _text(action.get(key))
                for key in ("operation", "intent", "raw_action", "action_text")
            )
            text_l = text.lower()
            evidence_step = f"{trace_id}:step{step_index}" if trace_id and step_index not in (None, "") else ""

            station_target = _station_target_from_text(text)
            if source_filename == "home.html" and target_filename == "station-select.html" and station_target:
                active_station_target = station_target
                obligations.append(_page_execution_obligation(
                    trace_id,
                    step_index,
                    field_path="stationPicker.targetField",
                    field_type="StationPickerTarget",
                    lifecycle="station_picker_open",
                    source_filename=source_filename,
                    target_filename=target_filename,
                    evidence_step=evidence_step,
                    metadata={"target_field": station_target, "action_text": text[:240]},
                ))
                obligations.append(_page_execution_obligation(
                    trace_id,
                    step_index,
                    field_path=f"searchForm.{station_target}",
                    field_type="StationRef",
                    lifecycle="pending_station_commit",
                    source_filename=source_filename,
                    target_filename=target_filename,
                    evidence_step=evidence_step,
                    metadata={"action_text": text[:240]},
                ))

            if source_filename == "station-select.html":
                if "fill" in text_l or "type" in text_l or "输入" in text:
                    obligations.append(_page_execution_obligation(
                        trace_id,
                        step_index,
                        field_path="stationPicker.query",
                        field_type="string",
                        lifecycle="autocomplete_query",
                        source_filename=source_filename,
                        target_filename=target_filename,
                        evidence_step=evidence_step,
                        metadata={"query_value": _extract_quoted_value(text), "action_text": text[:240]},
                    ))
                    obligations.append(_page_execution_obligation(
                        trace_id,
                        step_index,
                        field_path="stationPicker.suggestions",
                        field_type="StationRef[]",
                        lifecycle="autocomplete_results",
                        source_filename=source_filename,
                        target_filename=target_filename,
                        evidence_step=evidence_step,
                        metadata={"action_text": text[:240]},
                    ))
                elif "搜索输入框" in text or "搜索框" in text:
                    obligations.append(_page_execution_obligation(
                        trace_id,
                        step_index,
                        field_path="stationPicker.query",
                        field_type="string",
                        lifecycle="autocomplete_query_focus",
                        source_filename=source_filename,
                        target_filename=target_filename,
                        evidence_step=evidence_step,
                        metadata={"action_text": text[:240]},
                    ))
                elif target_filename == "home.html" and (
                    "搜索结果" in text or "常用车站" in text or "城市" in text or "车站" in text
                ):
                    obligations.append(_page_execution_obligation(
                        trace_id,
                        step_index,
                        field_path="stationPicker.suggestions",
                        field_type="StationRef[]",
                        lifecycle="autocomplete_commit_source",
                        source_filename=source_filename,
                        target_filename=target_filename,
                        evidence_step=evidence_step,
                        metadata={"action_text": text[:240]},
                    ))
                    if active_station_target in {"origin", "destination"}:
                        obligations.append(_page_execution_obligation(
                            trace_id,
                            step_index,
                            field_path=f"searchForm.{active_station_target}",
                            field_type="StationRef",
                            lifecycle="station_picker_commit",
                            source_filename=source_filename,
                            target_filename=target_filename,
                            evidence_step=evidence_step,
                            metadata={"target_field": active_station_target, "action_text": text[:240]},
                        ))

            if "日期" in text or "日历" in text or target_filename == "date-select.html":
                obligations.append(_page_execution_obligation(
                    trace_id,
                    step_index,
                    field_path="searchForm.date",
                    field_type="DateValue",
                    lifecycle="date_picker_commit",
                    source_filename=source_filename,
                    target_filename=target_filename,
                    evidence_step=evidence_step,
                    metadata={"action_text": text[:240]},
                ))
            if "查询车票" in text or (source_filename == "home.html" and target_filename == "search.html"):
                for field_path, field_type in (
                    ("queryState.request", "QueryRequest"),
                    ("queryState.results", "QueryResult[]"),
                ):
                    obligations.append(_page_execution_obligation(
                        trace_id,
                        step_index,
                        field_path=field_path,
                        field_type=field_type,
                        lifecycle="search_submit",
                        source_filename=source_filename,
                        target_filename=target_filename,
                        evidence_step=evidence_step,
                        metadata={"action_text": text[:240]},
                    ))
            if "筛选" in text or "普通车" in text or "高铁" in text or "动车" in text or "出发时间" in text:
                obligations.append(_page_execution_obligation(
                    trace_id,
                    step_index,
                    field_path="queryState.filters",
                    field_type="QueryFilters",
                    lifecycle="filter_mutation",
                    source_filename=source_filename,
                    target_filename=target_filename,
                    evidence_step=evidence_step,
                    metadata={"action_text": text[:240]},
                ))
            if "车次" in text or "预订" in text or "提交订单" in text or "提交" in text:
                obligations.append(_page_execution_obligation(
                    trace_id,
                    step_index,
                    field_path="selection.current",
                    field_type="RecordRef",
                    lifecycle="record_selection",
                    source_filename=source_filename,
                    target_filename=target_filename,
                    evidence_step=evidence_step,
                    metadata={"action_text": text[:240]},
                ))
            if "座" in text or "席别" in text or "硬卧" in text or "软卧" in text:
                obligations.append(_page_execution_obligation(
                    trace_id,
                    step_index,
                    field_path="selection.options",
                    field_type="SelectionOption[]",
                    lifecycle="seat_class_selection",
                    source_filename=source_filename,
                    target_filename=target_filename,
                    evidence_step=evidence_step,
                    metadata={"action_text": text[:240]},
                ))
            if (
                "选座" in text
                or "座位选项" in text
                or "座位偏好" in text
                or "偏好" in text
                or re.search(r"[“\"]?[A-F][”\"]?座", text)
            ):
                obligations.append(_page_execution_obligation(
                    trace_id,
                    step_index,
                    field_path="selection.preference",
                    field_type="SelectionPreference",
                    lifecycle="seat_preference_selection",
                    source_filename=source_filename,
                    target_filename=target_filename,
                    evidence_step=evidence_step,
                    metadata={"action_text": text[:240], "value_hint": _extract_quoted_value(text)},
                ))
            _collect_passenger_edit_obligations(
                obligations,
                trace_id,
                step_index,
                source_filename=source_filename,
                target_filename=target_filename,
                evidence_step=evidence_step,
                action_text=text,
                task_instruction=_text(pack.get("task") or pack.get("task_instruction") or pack.get("instruction")),
            )
    return obligations


def collect_obligations_from_runtime_state_obligations(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw_obligations = payload.get("obligations", []) if isinstance(payload, Mapping) else []
    obligations: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_obligations if isinstance(raw_obligations, list) else []):
        if not isinstance(item, Mapping):
            continue
        field_paths = _runtime_state_obligation_field_paths(item)
        if not field_paths:
            continue
        trace_id = _text(item.get("trace_id"))
        step_index = item.get("step_index")
        page_cluster_id = _text(item.get("page_cluster_id") or item.get("materialized_page_id"))
        for field_path in field_paths:
            obligations.append({
                "obligation_id": f"runtime_state_obligation::{_text(item.get('obligation_id')) or idx}::{_slug(field_path)}",
                "obligation_kind": "pre_pageenv_runtime_state_obligation",
                "family_id": _family_for_field(field_path, {"state_ref": item.get("state_scope"), "source_filename": page_cluster_id}),
                "field_path": field_path,
                "field_type": _field_type_for_path(field_path),
                "lifecycle": _text(item.get("lifecycle") or "runtime_state_obligation"),
                "producers": ["runtime_state_obligations"],
                "consumers": ["page_environment_spec", "pagegen", "runtime_backend"],
                "evidence_steps": [f"{trace_id}:step{step_index}"] if trace_id and step_index not in (None, "") else _as_list(item.get("evidence_steps")),
                "projection_pages": [page_cluster_id] if page_cluster_id else [],
                "aliases": [f"state::{_text(item.get('state_scope'))}"] if _text(item.get("state_scope")) else [],
                "metadata": {
                    "state_scope": item.get("state_scope"),
                    "mutation": item.get("mutation"),
                    "value_hints": item.get("value_hints"),
                    "raw_evidence": item.get("raw_evidence"),
                    "canonical_resolution": _runtime_state_canonical_resolution(
                        item,
                        field_path,
                        required=_runtime_state_obligation_requires_canonical_owner(item),
                    ),
                },
            })
    return obligations


def collect_obligations_from_checkpoint_matrix(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for cp_idx, checkpoint in enumerate(_as_list(payload.get("checkpoints"))):
        if not isinstance(checkpoint, dict):
            continue
        trace_id = _text(checkpoint.get("trace_id"))
        checkpoint_id = _text(checkpoint.get("checkpoint_id")) or f"checkpoint_{cp_idx}"
        supervision = _text(checkpoint.get("supervision"))
        expected_state = _as_dict(checkpoint.get("expected_state"))
        expected_diff = _as_dict(_as_dict(checkpoint.get("global_runtime_state")).get("expected_state_diff"))
        state_refs = _as_list(_as_dict(checkpoint.get("global_runtime_state")).get("state_refs"))
        for ref in state_refs:
            ref_text = _text(ref)
            if not ref_text:
                continue
            field_path = _runtime_transaction_ref_to_field_path(ref_text)
            obligations.append({
                "obligation_id": f"checkpoint_state_ref::{trace_id}::{checkpoint_id}::{_slug(ref_text)}",
                "obligation_kind": "checkpoint_state_ref",
                "family_id": _checkpoint_family(checkpoint),
                "field_path": field_path,
                "field_type": _field_type_for_path(field_path),
                "lifecycle": "task_supervision",
                "semantic_class": _semantic_class_for_field_path(
                    field_path,
                    obligation_kind="checkpoint_state_ref",
                    supervision=supervision,
                ),
                "producers": ["checkpoint_matrix"],
                "consumers": ["runtime_checkpoint_collector", "evaluator"],
                "evidence_steps": [f"{trace_id}:step{step}" for step in _as_list(checkpoint.get("source_steps"))],
                "aliases": [ref_text],
                "metadata": {
                    "checkpoint_id": checkpoint_id,
                    "supervision": supervision,
                    "not_root_reason": _not_root_reason_for_field_path(field_path),
                },
            })
        for field_name in sorted({*expected_state.keys(), *expected_diff.keys()}):
            field_text = _text(field_name)
            if not field_text or field_text in {"page", "current_page", "terminal_page"}:
                continue
            field_path = f"taskOutcome.{_slug(field_text)}" if supervision == "outcome" else f"checkpointState.{_slug(field_text)}"
            obligations.append({
                "obligation_id": f"checkpoint_expected_field::{trace_id}::{checkpoint_id}::{_slug(field_text)}",
                "obligation_kind": "checkpoint_expected_field",
                "family_id": _checkpoint_family(checkpoint),
                "field_path": field_path,
                "field_type": _checkpoint_field_type(expected_state.get(field_name, expected_diff.get(field_name))),
                "lifecycle": "task_supervision",
                "semantic_class": _semantic_class_for_field_path(
                    field_path,
                    obligation_kind="checkpoint_expected_field",
                    supervision=supervision,
                ),
                "producers": ["checkpoint_matrix"],
                "consumers": ["runtime_checkpoint_collector", "evaluator"],
                "evidence_steps": [f"{trace_id}:step{step}" for step in _as_list(checkpoint.get("source_steps"))],
                "aliases": [f"state::outcome.{field_text}"] if supervision == "outcome" else [],
                "metadata": {
                    "checkpoint_id": checkpoint_id,
                    "supervision": supervision,
                    "expected_value": expected_state.get(field_name, expected_diff.get(field_name)),
                    "display_field": field_text,
                    "not_root_reason": _not_root_reason_for_field_path(field_path),
                },
            })
    return obligations


def collect_obligations_from_runtime_state_schema(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    for idx, checkpoint in enumerate(_as_list(payload.get("checkpoints"))):
        if not isinstance(checkpoint, dict):
            continue
        trace_id = _text(checkpoint.get("trace_id"))
        checkpoint_id = _text(checkpoint.get("checkpoint_id")) or f"checkpoint_{idx}"
        for ref in _as_list(checkpoint.get("state_refs")):
            ref_text = _text(ref)
            if not ref_text:
                continue
            field_path = _runtime_transaction_ref_to_field_path(ref_text)
            obligations.append({
                "obligation_id": f"runtime_schema_state_ref::{trace_id}::{checkpoint_id}::{_slug(ref_text)}",
                "obligation_kind": "runtime_schema_state_ref",
                "family_id": _checkpoint_family(checkpoint),
                "field_path": field_path,
                "field_type": _field_type_for_path(field_path),
                "lifecycle": "task_supervision",
                "semantic_class": _semantic_class_for_field_path(
                    field_path,
                    obligation_kind="runtime_schema_state_ref",
                    supervision=_text(checkpoint.get("supervision")),
                ),
                "producers": ["runtime_state_schema"],
                "consumers": ["runtime_checkpoint_collector", "evaluator"],
                "evidence_steps": [f"{trace_id}:step{step}" for step in _as_list(checkpoint.get("source_steps"))],
                "aliases": [ref_text],
                "metadata": {
                    "checkpoint_id": checkpoint_id,
                    "not_root_reason": _not_root_reason_for_field_path(field_path),
                },
            })
        for field in _as_list(checkpoint.get("fields")):
            if not isinstance(field, dict):
                continue
            name = _text(field.get("field"))
            if not name or name in {"page", "current_page", "terminal_page"}:
                continue
            supervision = _text(checkpoint.get("supervision"))
            field_path = f"taskOutcome.{_slug(name)}" if supervision == "outcome" else f"checkpointState.{_slug(name)}"
            obligations.append({
                "obligation_id": f"runtime_schema_field::{trace_id}::{checkpoint_id}::{_slug(name)}",
                "obligation_kind": "runtime_schema_field",
                "family_id": _checkpoint_family(checkpoint),
                "field_path": field_path,
                "field_type": _text(field.get("type") or "StateField"),
                "lifecycle": "task_supervision",
                "semantic_class": _semantic_class_for_field_path(
                    field_path,
                    obligation_kind="runtime_schema_field",
                    supervision=supervision,
                ),
                "producers": ["runtime_state_schema"],
                "consumers": ["runtime_checkpoint_collector", "evaluator"],
                "evidence_steps": [f"{trace_id}:step{step}" for step in _as_list(checkpoint.get("source_steps"))],
                "aliases": [f"state::outcome.{name}"] if supervision == "outcome" else [],
                "metadata": {
                    "checkpoint_id": checkpoint_id,
                    "display_field": name,
                    "not_root_reason": _not_root_reason_for_field_path(field_path),
                },
            })
    return obligations


def _collect_parameterized_inputs(obligations: List[Dict[str, Any]], filename: str, spec: Mapping[str, Any]) -> None:
    for idx, item in enumerate(_as_list(spec.get("parameterized_inputs"))):
        if not isinstance(item, dict):
            continue
        field_path = infer_search_field_path(item)
        obligations.append({
            "obligation_id": f"parameterized_input::{filename}::{idx}",
            "obligation_kind": "parameterized_input",
            "family_id": _family_for_field(field_path, item),
            "field_path": field_path,
            "field_type": _field_type_for_path(field_path),
            "lifecycle": "initial_or_runtime_variant",
            "producers": ["parameterized_input"],
            "consumers": [filename],
            "evidence_steps": _step_keys(item),
            "projection_pages": [filename],
            "aliases": [_text(item.get("state_ref") or item.get("query_state_owner"))],
            "metadata": {
                "field_role": item.get("field_role") or item.get("parameter_id"),
                "query_values": _query_values(item),
            },
        })


def _collect_query_projections(obligations: List[Dict[str, Any]], filename: str, spec: Mapping[str, Any]) -> None:
    for idx, item in enumerate(_as_list(spec.get("query_projections"))):
        if not isinstance(item, dict):
            continue
        field_path = infer_search_field_path(item)
        kind = _text(item.get("query_transition_kind"))
        if kind in {"query_entry", "suggestion_projection"} and not field_path:
            field_path = "stationPicker.query" if _looks_like_station_query(item) else ""
        obligations.append({
            "obligation_id": f"query_projection::{filename}::{idx}",
            "obligation_kind": "autocomplete_control" if kind == "suggestion_projection" else "query_projection",
            "family_id": _family_for_field(field_path, item),
            "field_path": field_path,
            "field_type": _field_type_for_path(field_path),
            "lifecycle": "after_input" if kind in {"query_entry", "suggestion_projection"} else "after_commit",
            "source_collection": "Station" if field_path.startswith("stationPicker") else "",
            "candidate_projection": ["name", "fullName", "pinyin", "code"] if field_path.startswith("stationPicker") else [],
            "commit_target": "searchForm[stationPicker.targetField]" if field_path.startswith("stationPicker") else "",
            "producers": ["query_projection"],
            "consumers": [filename, _text(item.get("submit_target_filename") or item.get("target_filename"))],
            "evidence_steps": _step_keys(item),
            "projection_pages": [filename],
            "aliases": [_text(item.get("query_state_owner"))],
            "metadata": {
                "field_role": item.get("field_role"),
                "query_value": item.get("query_value"),
                "query_transition_kind": kind,
            },
        })


def _collect_transitions(obligations: List[Dict[str, Any]], filename: str, spec: Mapping[str, Any]) -> None:
    for idx, transition in enumerate(_as_list(spec.get("transitions"))):
        if not isinstance(transition, dict):
            continue
        effect = _as_dict(transition.get("state_effect"))
        payload = dict(transition)
        payload.update(effect)
        field_path = infer_search_field_path(payload)
        state_ref = _text(transition.get("query_state_owner") or effect.get("query_state_owner") or effect.get("state_ref") or effect.get("draft_ref"))
        if state_ref or field_path:
            obligations.append({
                "obligation_id": f"transition_state::{filename}::{idx}",
                "obligation_kind": "transition_state_effect",
                "family_id": _family_for_field(field_path, payload),
                "field_path": field_path,
                "field_type": _field_type_for_path(field_path),
                "lifecycle": _text(effect.get("query_transition_kind") or transition.get("query_transition_kind") or "runtime"),
                "producers": [_text(transition.get("reducer") or effect.get("effect") or "transition")],
                "consumers": [_text(transition.get("target_filename") or filename)],
                "evidence_steps": _step_keys(transition),
                "projection_pages": [filename],
                "aliases": [state_ref],
                "metadata": {
                    "state_ref": state_ref,
                    "field_role": payload.get("field_role"),
                },
            })


def _collect_runtime_refs(obligations: List[Dict[str, Any]], filename: str, spec: Mapping[str, Any]) -> None:
    state_schema = _as_dict(spec.get("state_schema"))
    for idx, item in enumerate(_as_list(state_schema.get("runtime_state_refs"))):
        state_ref = _text(item.get("state_ref") if isinstance(item, dict) else item)
        if not state_ref:
            continue
        field_path = _runtime_transaction_ref_to_field_path(state_ref)
        obligations.append({
            "obligation_id": f"runtime_state_ref::{filename}::{idx}",
            "obligation_kind": "runtime_state_ref",
            "family_id": _family_for_field(field_path, {"state_ref": state_ref}),
            "field_path": field_path,
            "field_type": _field_type_for_path(field_path),
            "lifecycle": "runtime_ref",
            "producers": ["page_environment_state_schema"],
            "consumers": [filename],
            "projection_pages": [filename],
            "aliases": [state_ref],
            "metadata": {"state_ref": state_ref},
        })


def _runtime_transaction_ref_to_field_path(state_ref: Any) -> str:
    text = _text(state_ref)
    lower = text.lower()
    if text.endswith(".origin_station") or text.endswith(".origin"):
        return "searchForm.origin"
    if text.endswith(".destination_station") or text.endswith(".destination"):
        return "searchForm.destination"
    if text.endswith(".travel_date") or text.endswith(".date"):
        return "searchForm.date"
    if "selection.origin" in text:
        return "searchForm.origin"
    if "selection.destination" in text:
        return "searchForm.destination"
    if (
        "station_search_results" in lower
        or ("station_select_html" in lower and "search_results" in lower)
        or ("station_select_html" in lower and "station_list" in lower)
        or ("station_select_html" in lower and "frequent_stations" in lower)
    ):
        return "stationPicker.suggestions"
    if "station_select_html_business_query" in lower or (
        text.startswith("draft::") and "station_select_html" in lower
    ):
        return "stationPicker.query"
    if "search_results_query_context" in lower or "search_results_context" in lower:
        return "queryState.request"
    if lower in {"state::c_station_selection", "c_station_selection", "state::station_selection", "station_selection"}:
        return "stationPicker.query"
    if (
        "search_results_filtered_list" in lower
        or "search_results_list" in lower
        or "search_results_empty_state" in lower
        or "train_schedule" in lower
    ):
        return "queryState.results"
    if (
        "search_results_sorting" in lower
        or "results_filter_and_sort" in lower
        or "departure_time_picker" in lower
        or "selected_departure_time" in lower
    ):
        return "queryState.filters"
    if "seat_class_options" in lower:
        return "selection.options"
    if "selected_record" in lower or "selected_runtime_overlay" in lower:
        return "selection.current"
    if "runtime_overlay_stack" in lower:
        return "ui.overlayStack"
    if "inline_activation" in lower:
        return "ui.inlineActivation"
    if "action_sheet" in lower:
        return "ui.actionSheet"
    if "page_runtime" in lower:
        return "runtime.page"
    if text.startswith("state::"):
        return f"runtimeState.{_slug(text[len('state::'):])}"
    return ""


def _semantic_class_for_field_path(
    field_path: str,
    *,
    obligation_kind: str = "",
    supervision: str = "",
) -> str:
    if field_path.startswith("taskOutcome."):
        return "terminal_outcome"
    if field_path.startswith("checkpointState."):
        return "checkpoint_local"
    if field_path.startswith("searchForm.") or field_path.startswith("queryState."):
        return "query_state"
    if field_path.startswith("stationPicker."):
        return "autocomplete_state"
    if field_path.startswith("selection."):
        return "entity_selection"
    if field_path.startswith(("identityForm.", "identityProfile.", "personalIdentity.")):
        return "form_draft"
    if field_path.startswith("ui."):
        return "ui_runtime"
    if field_path.startswith("runtime."):
        return "route_runtime"
    if field_path.startswith("runtimeState."):
        return "checkpoint_local"
    if supervision == "outcome":
        return "terminal_outcome"
    if "runtime_state" in obligation_kind:
        return "checkpoint_local"
    return "business_state"


def _not_root_reason_for_field_path(field_path: str) -> str:
    if not field_path:
        return "missing_field_path"
    if field_path.startswith(ROOT_SCHEMA_FIELD_PREFIXES):
        return ""
    if field_path.startswith("taskOutcome."):
        return "terminal_outcome_downstream_projection"
    if field_path.startswith("checkpointState."):
        return "checkpoint_local_state"
    if field_path.startswith("ui."):
        return "ui_runtime_state_not_business_state"
    if field_path.startswith("runtime."):
        return "route_runtime_state_not_business_state"
    if field_path.startswith("runtimeState."):
        return "runtime_checkpoint_local_state"
    if field_path.startswith(DOWNSTREAM_FIELD_PREFIXES):
        return "downstream_extension_state"
    return "unresolved_global_schema_owner"


def _runtime_state_obligation_field_paths(item: Mapping[str, Any]) -> List[str]:
    scope = _text(item.get("state_scope")).lower()
    raw = " ".join([
        scope,
        _text(item.get("raw_evidence")).lower(),
        " ".join(_text(value).lower() for value in _as_list(item.get("value_hints"))),
    ])
    fields: List[str] = []

    if "station_search_results" in scope or "station_selection_list" in scope:
        _append_unique(fields, "stationPicker.suggestions")
        return fields
    if "station_selection" in scope or "station_search_result_selection" in scope:
        _append_unique(fields, "stationPicker.suggestions")
        if "departure" in raw or "出发" in raw or "始发" in raw:
            _append_unique(fields, "searchForm.origin")
        elif "arrival" in raw or "destination" in raw or "到达" in raw or "目的" in raw:
            _append_unique(fields, "searchForm.destination")
        return fields

    if "filter" in scope or "sort" in scope:
        _append_unique(fields, "queryState.filters")
        return fields
    if any(token in scope for token in ("results_list", "filtered_list", "train_inventory", "empty_results", "search_results_empty")):
        _append_unique(fields, "queryState.results")
        return fields
    if "search_results" in scope or "results_context" in scope:
        _append_unique(fields, "queryState.request")
        if any(token in scope for token in ("list", "inventory", "filtered")):
            _append_unique(fields, "queryState.results")
        return fields

    search_form_scope = any(
        token in scope
        for token in (
            "search_form",
            "homepage_search_criteria",
            "search_criteria",
            "search_card",
            "query_form",
        )
    )
    if search_form_scope:
        if any(token in scope for token in ("departure", "origin")) or "departure:" in raw or "出发地" in raw:
            _append_unique(fields, "searchForm.origin")
        if any(token in scope for token in ("arrival", "destination")) or "arrival:" in raw or "destination:" in raw or "到达" in raw:
            _append_unique(fields, "searchForm.destination")
        if "date" in scope or "日期" in raw or "date:" in raw or "jan " in raw or "feb " in raw or "月" in raw:
            _append_unique(fields, "searchForm.date")
        if not fields:
            _append_unique(fields, "queryState.request")
        return fields

    if any(token in scope for token in ("departure_station", "origin_station")):
        return ["searchForm.origin"]
    if any(token in scope for token in ("arrival_station", "destination_station")):
        return ["searchForm.destination"]
    if "date" in scope or "travel_date" in scope:
        return ["searchForm.date"]
    if "seat" in scope:
        return ["selection.options"]
    if "ticket_booking_selection" in scope or "selected_record" in scope:
        return ["selection.current"]
    if "selected_train" in scope or "train_selection" in scope or "order_form_train_context" in scope:
        return ["selection.current"]
    if "results_query_context" in scope:
        return ["queryState.request"]
    if "results_controls" in scope:
        return ["queryState.filters"]
    if "home_booking_widget_state" in scope or "schedule_form_data" in scope:
        return ["queryState.request"]
    if "personal_info_form" in scope or "personal_identity" in scope or "profile_identity_display" in scope:
        _append_unique(fields, "personalIdentity.name")
        _append_unique(fields, "personalIdentity.certificateType")
        _append_unique(fields, "personalIdentity.certificateNumber")
        if "phone" in raw or "手机号" in raw or "mobile" in raw:
            _append_unique(fields, "personalIdentity.mobile")
        return fields
    if "add_passenger_form" in scope or "passenger_form" in scope or "passenger_edit_form" in scope:
        _append_unique(fields, "identityForm.recordDraft")
        if "phone" in raw or "手机号" in raw or "mobile" in raw:
            _append_unique(fields, "identityForm.mobile")
        if "name" in raw or "姓名" in raw:
            _append_unique(fields, "identityForm.name")
        if "certificate" in raw or "证件号码" in raw or "身份证" in raw:
            _append_unique(fields, "identityForm.certificateNumber")
        if "证件类型" in raw or "certificate type" in raw:
            _append_unique(fields, "identityForm.certificateType")
        if "成人" in raw or "学生" in raw or "passenger type" in raw or "优惠" in raw:
            _append_unique(fields, "identityForm.personType")
        if "核验" in raw or "核对" in raw or "验证" in raw or "未通过" in raw:
            _append_unique(fields, "identityForm.verificationStatus")
        return fields
    if "passenger_phone" in scope or "contact_phone" in scope:
        return ["identityForm.mobile"]
    if "id_number_field" in scope:
        return ["identityForm.certificateNumber"]
    if "discount_type_selection" in scope:
        return ["identityForm.personType"]
    if (
        "student_discount_verification" in scope
        or "form_submission_validation_error" in scope
        or "form_validation" in scope
        or "validation_dialog" in scope
        or "validation_error_modal" in scope
        or "profile_verification" in scope
        or "enable_prerequisite_constraint" in scope
    ):
        return ["identityForm.verificationStatus"]
    if (
        "passenger_list" in scope
        or "passenger_collection" in scope
        or "passenger_selection" in scope
        or "selected_passengers" in scope
        or "waitlist_order_passenger" in scope
    ):
        return ["identityProfile.selectedRecord"]
    return []


def _runtime_state_obligation_requires_canonical_owner(item: Mapping[str, Any]) -> bool:
    if _runtime_state_obligation_is_not_schema_relevant(item):
        return False
    return any(
        _text(item.get(key))
        for key in ("state_scope", "lifecycle", "mutation", "raw_evidence")
    ) or bool(_as_list(item.get("value_hints")))


def _runtime_state_obligation_is_not_schema_relevant(item: Mapping[str, Any]) -> bool:
    scope = _text(item.get("state_scope")).lower()
    raw = " ".join([
        scope,
        _text(item.get("raw_evidence")).lower(),
        " ".join(_text(value).lower() for value in _as_list(item.get("value_hints"))),
    ])
    if _runtime_state_obligation_field_paths(item):
        return False
    non_schema_tokens = (
        "navigation",
        "nav_bar",
        "bottom_tab",
        "tab_state",
        "header",
        "modal",
        "overlay",
        "button",
        "menu",
        "sheet",
        "panel",
        "directory",
        "browse",
        "map",
        "share",
        "checkbox",
        "countdown",
        "history",
        "reminder",
        "utility",
        "categories",
        "common_functions",
        "preview",
        "template",
        "top_level",
        "service_mode",
        "transport_options",
        "stopover_stations",
        "ticket_detail_display",
        "ticket_itinerary_detail",
        "order_lifecycle_status",
        "order_action_menu",
        "order_category_tabs",
        "payment_tracking",
        "payment_countdown",
        "waitlist_order",
        "waitlist_list",
        "waitlist_cart",
        "waitlist_fulfillment",
        "active_arrival_reminders",
        "user_profile_header",
        "primary_action_button",
        "recent_search_history",
        "success_modal",
        "loading_overlay",
        "confirmation_modal",
    )
    return any(token in raw for token in non_schema_tokens)


def _runtime_state_canonical_resolution(
    item: Mapping[str, Any],
    field_path: str,
    *,
    required: bool,
) -> Dict[str, Any]:
    return {
        "required": required,
        "status": "resolved",
        "owner_kind": "root_schema",
        "owner_field_path": field_path,
        "canonical_state_ref": f"global_execution_schema:{field_path}",
        "canonical_entity": _canonical_entity_for_field(field_path),
        "expected_state_delta": _runtime_state_expected_delta(item),
        "expected_value_hints": _as_list(item.get("value_hints")),
        "resolution_source": "runtime_state_obligation_field_paths",
        "reason": "mapped_from_runtime_state_obligation_scope",
    }


def _runtime_state_expected_delta(item: Mapping[str, Any]) -> Dict[str, Any]:
    delta: Dict[str, Any] = {}
    for key in ("state_scope", "lifecycle", "mutation"):
        value = _text(item.get(key))
        if value:
            delta[key] = value
    value_hints = _as_list(item.get("value_hints"))
    if value_hints:
        delta["value_hints"] = value_hints
    raw_evidence = _text(item.get("raw_evidence"))
    if raw_evidence:
        delta["raw_evidence"] = raw_evidence[:500]
    return delta


def _canonical_entity_for_field(field_path: str) -> str:
    if field_path.startswith("searchForm."):
        return "SearchForm"
    if field_path.startswith("stationPicker."):
        return "StationPicker"
    if field_path.startswith("queryState."):
        return "QueryRequestState"
    if field_path.startswith("selection."):
        return "SelectionState"
    if field_path.startswith("identityForm."):
        return "IdentityForm"
    if field_path.startswith("identityProfile."):
        return "IdentityProfile"
    if field_path.startswith("personalIdentity."):
        return "PersonalIdentity"
    if field_path.startswith("taskOutcome."):
        return "TaskOutcome"
    if field_path.startswith("checkpointState."):
        return "CheckpointState"
    if field_path.startswith("ui."):
        return "UIRuntimeState"
    if field_path.startswith("runtimeState.") or field_path.startswith("runtime."):
        return "RuntimeState"
    return "GlobalExecutionState"


def _page_execution_obligation(
    trace_id: str,
    step_index: Any,
    *,
    field_path: str,
    field_type: str,
    lifecycle: str,
    source_filename: str,
    target_filename: str,
    evidence_step: str,
    metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    field_slug = _slug(field_path)
    return {
        "obligation_id": f"page_execution::{trace_id or 'trace'}::{step_index}::{field_slug}",
        "obligation_kind": "pre_pageenv_page_execution_obligation",
        "family_id": _family_for_field(field_path, {
            "source_filename": source_filename,
            "target_filename": target_filename,
        }),
        "field_path": field_path,
        "field_type": field_type,
        "lifecycle": lifecycle,
        "producers": ["page_execution_obligations"],
        "consumers": [value for value in [source_filename, target_filename] if value],
        "evidence_steps": [evidence_step] if evidence_step else [],
        "projection_pages": [value for value in [source_filename, target_filename] if value],
        "metadata": dict(metadata or {}),
    }


def _collect_passenger_edit_obligations(
    obligations: List[Dict[str, Any]],
    trace_id: str,
    step_index: Any,
    *,
    source_filename: str,
    target_filename: str,
    evidence_step: str,
    action_text: str,
    task_instruction: str = "",
) -> None:
    context_raw = " ".join([source_filename, target_filename, action_text])
    context_joined = context_raw.lower()
    page_context_raw = " ".join([source_filename, target_filename])
    page_context_joined = page_context_raw.lower()
    raw = " ".join([context_raw, task_instruction])
    joined = raw.lower()
    passenger_related = any(
        token in joined
        for token in (
            "passenger",
            "personal",
            "profile",
            "identity",
            "c_modify_personal_info",
            "modify-info",
            "me-modify-info",
            "passenger-add",
            "add-passenger",
            "passenger-edit",
            "passengers-list",
        )
    ) or any(token in context_raw for token in ("乘车人", "旅客", "联系人", "个人信息", "姓名", "手机号", "手机号码", "身份证", "证件"))
    context_passenger_related = any(
        token in context_joined
        for token in (
            "passenger",
            "personal",
            "profile",
            "identity",
            "c_modify_personal_info",
            "modify-info",
            "me-modify-info",
            "passenger-add",
            "add-passenger",
            "passenger-edit",
            "add_passenger",
            "passengers-list",
            "select_passenger",
            "edit_passenger",
        )
    ) or any(token in context_raw for token in ("乘车人", "旅客", "联系人", "个人信息", "姓名", "手机号", "手机号码", "身份证", "证件"))
    page_passenger_surface = any(
        token in page_context_joined
        for token in (
            "passenger",
            "personal",
            "profile",
            "identity",
            "c_modify_personal_info",
            "modify-info",
            "me-modify-info",
            "passenger-add",
            "add-passenger",
            "passenger-edit",
            "add_passenger",
            "passengers-list",
            "select_passenger",
            "edit_passenger",
        )
    ) or any(token in page_context_raw for token in ("乘车人", "旅客", "联系人", "个人信息"))
    direct_field_action = (
        any(token in context_joined for token in ("fill", "type", "press", "delete", "backspace"))
        or any(token in context_raw for token in ("输入", "填写", "双击", "删除", "清空", "选中", "提交"))
    ) and any(token in context_raw for token in ("姓名", "手机号", "手机号码", "联系电话", "电话", "身份证", "证件", "证件号码"))
    direct_selection_action = (
        ("乘车人" in context_raw or "passenger" in context_joined)
        and any(token in context_raw for token in ("选择", "勾选", "点击"))
    )
    if not (page_passenger_surface or direct_field_action or direct_selection_action):
        return
    if not context_passenger_related:
        return
    if not passenger_related:
        return

    personal_scope = (
        any(token in context_joined for token in ("personal", "identity", "c_modify_personal_info", "modify-info", "me-modify-info"))
        or "个人信息" in context_raw
    )
    add_or_form_scope = (
        any(token in context_joined for token in ("passenger-add", "add-passenger", "add_passenger"))
        or any(token in context_raw for token in ("新增乘车人", "添加新乘车人", "填写乘车人"))
    )
    edit_scope = (
        any(token in context_joined for token in ("edit_passenger", "passenger-edit", "c_edit_passenger"))
        or (
            any(token in context_raw for token in ("编辑", "修改"))
            and (page_passenger_surface or direct_field_action)
        )
    )
    selection_scope = (
        any(token in context_joined for token in ("select_passenger", "selected_passenger"))
        or ("乘车人" in context_raw and any(token in context_raw for token in ("选择", "勾选")))
    )

    metadata = {
        "action_text": action_text[:240],
        "task_instruction": task_instruction[:300],
        "source_filename": source_filename,
        "target_filename": target_filename,
        "value_hint": _extract_quoted_value(action_text),
    }
    field_specs: List[tuple[str, str, str]] = []

    if edit_scope or ("passengers-list" in context_joined and any(token in context_raw for token in ("编辑", "修改", "详情", "打开"))):
        field_specs.append(("identityProfile.editTarget", "IdentityRecordRef", "identity_record_edit_target"))
    if selection_scope:
        field_specs.append(("identityProfile.selectedRecord", "IdentityRecordRef", "identity_record_selection"))

    name_tokens = ("姓名", "名字", "name")
    mobile_tokens = ("手机号", "手机号码", "联系电话", "电话", "mobile", "phone")
    cert_type_tokens = ("证件类型", "certificate type", "id type")
    cert_number_tokens = ("证件号码", "身份证", "身份证号", "id number", "certificate number")
    passenger_type_tokens = ("乘客类型", "旅客类型", "成人", "儿童", "学生", "passenger type")
    verification_tokens = ("核验", "核对", "认证", "验证", "verification", "verified")

    form_prefix = "personalIdentity" if personal_scope else "identityForm"
    field_source_raw = raw if (personal_scope or add_or_form_scope or edit_scope or direct_field_action) else context_raw
    field_source_joined = field_source_raw.lower()

    if any(token in field_source_raw or token in field_source_joined for token in name_tokens):
        field_specs.append((f"{form_prefix}.name", "string", "form_field_mutation"))
    if any(token in field_source_raw or token in field_source_joined for token in mobile_tokens):
        field_specs.append((f"{form_prefix}.mobile", "PhoneNumber", "form_field_mutation"))
    if any(token in field_source_raw or token in field_source_joined for token in cert_type_tokens):
        field_specs.append((f"{form_prefix}.certificateType", "CertificateType", "form_field_mutation"))
    if any(token in field_source_raw or token in field_source_joined for token in cert_number_tokens):
        field_specs.append((f"{form_prefix}.certificateNumber", "CertificateNumber", "form_field_mutation"))
    if any(token in field_source_raw or token in field_source_joined for token in passenger_type_tokens):
        field_specs.append((f"{form_prefix}.personType", "PersonType", "form_field_mutation"))
    if any(token in field_source_raw or token in field_source_joined for token in verification_tokens):
        field_specs.append((f"{form_prefix}.verificationStatus", "VerificationStatus", "profile_status_read"))
    if add_or_form_scope and not any(path.startswith("identityForm.") for path, _, _ in field_specs):
        field_specs.append(("identityForm.recordDraft", "IdentityDraft", "identity_form_draft"))

    seen: set[str] = set()
    for field_path, field_type, lifecycle in field_specs:
        if field_path in seen:
            continue
        seen.add(field_path)
        obligations.append(_page_execution_obligation(
            trace_id,
            step_index,
            field_path=field_path,
            field_type=field_type,
            lifecycle=lifecycle,
            source_filename=source_filename,
            target_filename=target_filename,
            evidence_step=evidence_step,
            metadata={
                **metadata,
                "semantic_family": "personal_identity" if field_path.startswith("personalIdentity.") else "identity_profile" if field_path.startswith("identityProfile.") else "identity_form",
            },
        ))


def _station_target_from_text(text: Any) -> str:
    raw = _text(text)
    if any(token in raw for token in ("出发", "出发地", "出发站", "左侧")):
        return "origin"
    if any(token in raw for token in ("到达", "目的", "到达站", "右侧")):
        return "destination"
    lower = raw.lower()
    if any(token in lower for token in ("origin", "departure", "from_station")):
        return "origin"
    if any(token in lower for token in ("destination", "arrival", "to_station")):
        return "destination"
    return ""


def _extract_quoted_value(text: Any) -> str:
    raw = _text(text)
    match = re.search(r"[“\"]([^”\"]+)[”\"]", raw)
    if match:
        return match.group(1).strip()
    return ""


def _step_keys(item: Mapping[str, Any]) -> List[str]:
    keys: List[str] = []
    for key in ("step_key", "transition_id", "committed_by_step", "action_id"):
        _append_unique(keys, item.get(key))
    for key in ("step_keys", "source_step_keys", "action_ids", "l0_transition_ids"):
        for value in _as_list(item.get(key)):
            _append_unique(keys, value)
    return keys


def _query_values(item: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("query_value", "input_text", "value"):
        _append_unique(values, item.get(key))
    for field in _as_list(item.get("committed_fields")):
        if isinstance(field, Mapping):
            _append_unique(values, field.get("value"))
    return values


def _looks_like_station_query(item: Mapping[str, Any]) -> bool:
    text = " ".join(_text(item.get(key)) for key in ("source_filename", "target_filename", "result_projection", "query_value")).lower()
    return any(token in text for token in ("station", "车站", "杭州东", "北京", "上海", "天津", "石家庄"))


def _family_for_field(field_path: str, item: Mapping[str, Any]) -> str:
    text = " ".join([field_path, _text(item.get("source_filename")), _text(item.get("target_filename")), _text(item.get("state_ref"))]).lower()
    if any(token in text for token in ("searchform", "stationpicker", "station-select", "home.html", "train-list", "railway")):
        return SEARCH_FAMILY_ID
    if any(token in text for token in ("identity", "personalidentity", "passenger", "passengers-list", "passenger-add", "personal-info", "c_modify_personal_info")):
        return "identity_profile"
    return "global_execution"


def _field_type_for_path(field_path: str) -> str:
    if field_path in {"searchForm.origin", "searchForm.destination"}:
        return "StationRef"
    if field_path == "searchForm.date":
        return "DateValue"
    if field_path == "stationPicker.targetField":
        return "StationPickerTarget"
    if field_path == "stationPicker.query":
        return "string"
    if field_path == "stationPicker.suggestions":
        return "StationRef[]"
    if field_path == "queryState.request":
        return "QueryRequest"
    if field_path == "queryState.results":
        return "QueryResult[]"
    if field_path == "queryState.filters":
        return "QueryFilters"
    if field_path == "selection.current":
        return "RecordRef"
    if field_path == "selection.options":
        return "SelectionOption[]"
    if field_path == "selection.preference":
        return "SelectionPreference"
    if field_path in {"identityProfile.editTarget", "identityProfile.selectedRecord"}:
        return "IdentityRecordRef"
    if field_path == "identityForm.recordDraft":
        return "IdentityDraft"
    if field_path.endswith(".mobile"):
        return "PhoneNumber"
    if field_path.endswith(".certificateType"):
        return "CertificateType"
    if field_path.endswith(".certificateNumber"):
        return "CertificateNumber"
    if field_path.endswith(".personType"):
        return "PersonType"
    if field_path.endswith(".verificationStatus"):
        return "VerificationStatus"
    if field_path.startswith("ui.") or field_path.startswith("runtime."):
        return "object"
    if field_path.startswith("runtimeState."):
        return "object"
    return "string"


def _checkpoint_family(checkpoint: Mapping[str, Any]) -> str:
    text = " ".join(
        _text(checkpoint.get(key))
        for key in ("trace_id", "checkpoint_id", "checkpoint_kind", "supervision")
    ).lower()
    if any(token in text for token in ("railway", "train", "station", "search", "ticket")):
        return SEARCH_FAMILY_ID
    return "task_supervision"


def _checkpoint_field_type(value: Any) -> str:
    raw = value.get("value") if isinstance(value, Mapping) and "value" in value else value
    if isinstance(raw, bool):
        return "boolean"
    if isinstance(raw, int) and not isinstance(raw, bool):
        return "integer"
    if isinstance(raw, float):
        return "number"
    if isinstance(raw, list):
        return "array"
    if isinstance(raw, Mapping):
        return "object"
    return "string"


def _slug(value: Any) -> str:
    text = re.sub(r"[^\w]+", "_", _text(value), flags=re.UNICODE)
    return text.strip("_") or "ref"
