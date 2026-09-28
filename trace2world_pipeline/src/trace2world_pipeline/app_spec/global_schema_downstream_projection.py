
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from .global_execution_schema import (
    build_field_owner_index,
    normalize_field_path,
)
from .schema_obligations import collect_schema_obligations


DOWNSTREAM_FIELD_PREFIXES = (
    "taskOutcome.",
    "checkpointState.",
    "draft.",
    "runtimeState.",
    "ui.",
    "runtime.",
)

STATE_REF_KEYS = {
    "state_ref",
    "state_refs",
    "canonical_state_ref",
    "query_state_owner",
    "draft_ref",
    "binds_to",
}


def build_downstream_required_ref_payload(
    *,
    page_environment_specs: Mapping[str, Any] | None = None,
    runtime_transactions: Mapping[str, Any] | None = None,
    checkpoint_matrix: Mapping[str, Any] | None = None,
    runtime_state_schema: Mapping[str, Any] | None = None,
    schema_obligations_payload: Mapping[str, Any] | None = None,
    include_obligation_aliases: bool = True,
) -> Dict[str, Any]:

    obligations_payload = (
        dict(schema_obligations_payload)
        if isinstance(schema_obligations_payload, Mapping)
        else collect_schema_obligations(
            page_environment_specs=page_environment_specs,
            runtime_transactions=runtime_transactions,
            checkpoint_matrix=checkpoint_matrix,
            runtime_state_schema=runtime_state_schema,
        )
    )
    obligation_refs = required_refs_from_schema_obligations(
        obligations_payload.get("obligations", []),
        include_aliases=include_obligation_aliases,
    )
    runtime_refs = collect_runtime_required_refs(runtime_state_schema, "runtime_state_schema")
    checkpoint_refs = collect_runtime_required_refs(checkpoint_matrix, "checkpoint_matrix")
    required_refs = dedupe_required_refs([*obligation_refs, *runtime_refs, *checkpoint_refs])
    return {
        "format": "global_execution_schema_downstream_required_refs_v1",
        "schema_obligations": obligations_payload,
        "obligation_refs": obligation_refs,
        "runtime_refs": runtime_refs,
        "checkpoint_refs": checkpoint_refs,
        "required_refs": required_refs,
        "summary": {
            "schema_obligation_count": obligations_payload.get("obligation_count", 0),
            "required_ref_count": len(required_refs),
            "from_schema_obligations": len(obligation_refs),
            "from_runtime_state_schema": len(runtime_refs),
            "from_checkpoint_matrix": len(checkpoint_refs),
        },
    }


def required_refs_from_schema_obligations(
    obligations: Iterable[Any],
    *,
    include_aliases: bool = True,
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for item in obligations:
        if not isinstance(item, Mapping):
            continue
        field_path = _text(item.get("field_path"))
        if field_path:
            refs.append({
                "ref": field_path,
                "source": "schema_obligation",
                "obligation_id": item.get("obligation_id", ""),
                "obligation_kind": item.get("obligation_kind", ""),
                "family_id": item.get("family_id", ""),
            })
        if include_aliases:
            for alias in item.get("aliases", []) if isinstance(item.get("aliases"), list) else []:
                alias_text = _text(alias)
                if alias_text and (field_path or _looks_like_runtime_state_alias(alias_text)):
                    refs.append({
                        "ref": alias_text,
                        "source": "schema_obligation_alias",
                        "obligation_id": item.get("obligation_id", ""),
                        "field_path": field_path,
                    })
    return refs


def collect_runtime_required_refs(payload: Any, source: str) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key in STATE_REF_KEYS:
                    for ref in _iter_ref_values(child):
                        refs.append({"ref": ref, "source": source, "json_path": child_path})
                elif key in {"expected_state", "expected_state_diff", "state_diff", "state_reads"}:
                    for ref in _extract_state_keys(child):
                        refs.append({"ref": ref, "source": source, "json_path": child_path})
                visit(child, child_path)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                visit(item, f"{path}[{idx}]")

    visit(payload, "")
    return refs


def dedupe_required_refs(refs: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: List[Dict[str, Any]] = []
    for ref in refs:
        text = _text(ref.get("ref") or ref.get("field_path") or ref.get("state_ref"))
        if not text:
            continue
        key = (text, _text(ref.get("source")), _text(ref.get("obligation_id") or ref.get("json_path")))
        if key in seen:
            continue
        seen.add(key)
        row = dict(ref)
        row["ref"] = text
        result.append(row)
    return result


def build_downstream_schema_projection(
    global_schema: Mapping[str, Any],
    required_refs: Iterable[Mapping[str, Any]],
    *,
    source: str = "downstream_runtime_checkpoint_projection",
) -> Dict[str, Any]:

    root_index = build_field_owner_index(global_schema)
    projection_fields: Dict[str, Dict[str, Any]] = {}
    ref_aliases: Dict[str, Dict[str, Any]] = {}
    unresolved: List[Dict[str, Any]] = []
    extension_reason_counts: Dict[str, int] = {}

    for ref in required_refs:
        if not isinstance(ref, Mapping):
            continue
        raw_ref = _text(ref.get("ref") or ref.get("field_path") or ref.get("state_ref"))
        if not raw_ref:
            continue


        if raw_ref in ref_aliases:
            continue
        decision = _infer_projection_owner(ref, root_index)
        row = dict(ref)
        row["ref"] = raw_ref
        row["normalized_ref"] = normalize_field_path(raw_ref)
        if not decision:
            row["reason"] = "unresolved_downstream_projection_owner"
            unresolved.append(row)
            continue
        owner_kind = decision["owner_kind"]
        owner_field_path = decision["owner_field_path"]
        if owner_kind == "downstream_extension":
            not_root_reason = _not_root_reason_for_projection(owner_field_path, decision.get("reason", ""))
            field = projection_fields.setdefault(
                owner_field_path,
                {
                    "field_path": owner_field_path,
                    "field_type": _field_type_for_projection(owner_field_path),
                    "owner_kind": owner_kind,
                    "not_root_reason": not_root_reason,
                    "source_refs": [],
                    "sources": [],
                    "aliases": [],
                },
            )
            extension_reason_counts[not_root_reason] = extension_reason_counts.get(not_root_reason, 0) + 1
            _append_unique(field["source_refs"], raw_ref)
            _append_unique(field["sources"], _text(ref.get("source")) or source)
            if raw_ref != owner_field_path:
                _append_unique(field["aliases"], raw_ref)
        alias = {
            "ref": raw_ref,
            "owner_field_path": owner_field_path,
            "owner_kind": owner_kind,
            "source": _text(ref.get("source")) or source,
            "reason": decision.get("reason", ""),
            **({"root_field_path": owner_field_path} if owner_kind == "root_schema" else {}),
        }
        if owner_kind == "downstream_extension":
            alias["not_root_reason"] = _not_root_reason_for_projection(owner_field_path, decision.get("reason", ""))
        ref_aliases[raw_ref] = alias

    return {
        "format": "global_execution_schema_downstream_projection_v1",
        "source": source,
        "root_schema_id": _text(global_schema.get("schema_id")) if isinstance(global_schema, Mapping) else "",
        "projection_fields": sorted(projection_fields.values(), key=lambda item: item["field_path"]),
        "ref_aliases": sorted(ref_aliases.values(), key=lambda item: normalize_field_path(item["ref"])),
        "unresolved_refs": unresolved,
        "summary": {
            "projection_field_count": len(projection_fields),
            "ref_alias_count": len(ref_aliases),
            "unresolved_count": len(unresolved),
            "extension_reason_counts": dict(sorted(extension_reason_counts.items())),
            "status": "pass" if not unresolved else "fail",
        },
    }


def validate_required_refs_with_downstream_projection(
    global_schema: Mapping[str, Any],
    downstream_projection: Mapping[str, Any],
    required_refs: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    root_index = build_field_owner_index(global_schema)
    projection_index: Dict[str, Dict[str, Any]] = {}
    for field in downstream_projection.get("projection_fields", []) if isinstance(downstream_projection, Mapping) else []:
        if not isinstance(field, Mapping):
            continue
        field_path = normalize_field_path(field.get("field_path"))
        if field_path:
            projection_index[field_path] = dict(field)
        for alias in field.get("aliases", []) if isinstance(field.get("aliases"), list) else []:
            alias_norm = normalize_field_path(alias)
            if alias_norm:
                projection_index[alias_norm] = dict(field)
    alias_exact_index = {
        _text(item.get("ref")): dict(item)
        for item in downstream_projection.get("ref_aliases", []) if isinstance(item, Mapping)
        if _text(item.get("ref"))
    }
    alias_norm_index: Dict[str, Dict[str, Any]] = {}
    alias_norm_conflicts: set[str] = set()
    for raw_ref, item in alias_exact_index.items():
        norm_ref = normalize_field_path(raw_ref)
        if not norm_ref:
            continue
        existing = alias_norm_index.get(norm_ref)
        if existing and existing.get("ref") != raw_ref:
            alias_norm_conflicts.add(norm_ref)
            continue
        alias_norm_index[norm_ref] = item
    for norm_ref in alias_norm_conflicts:
        alias_norm_index.pop(norm_ref, None)

    resolved: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for ref in required_refs:
        raw_ref = _text(ref.get("ref") or ref.get("field_path") or ref.get("state_ref"))
        norm = normalize_field_path(raw_ref)
        row = dict(ref)
        row["ref"] = raw_ref
        row["normalized_ref"] = norm
        if norm in root_index:
            row["owner_field_path"] = root_index[norm].get("field_path")
            row["owner_kind"] = "root_schema"
            resolved.append(row)
            continue
        alias = alias_exact_index.get(raw_ref) or alias_norm_index.get(norm)
        if alias:
            row["owner_field_path"] = alias.get("owner_field_path")
            row["owner_kind"] = alias.get("owner_kind")
            row["projection_reason"] = alias.get("reason", "")
            resolved.append(row)
            continue
        projection = projection_index.get(norm)
        if projection:
            row["owner_field_path"] = projection.get("field_path")
            row["owner_kind"] = projection.get("owner_kind", "downstream_extension")
            resolved.append(row)
            continue
        row["reason"] = "unresolved_global_or_downstream_schema_owner"
        unresolved.append(row)

    return {
        "format": "global_execution_schema_downstream_ref_validation_v1",
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "resolved": resolved,
        "unresolved": unresolved,
        "status": "pass" if not unresolved else "fail",
    }


def _infer_projection_owner(
    ref: Mapping[str, Any],
    root_index: Mapping[str, Any],
) -> Dict[str, str] | None:
    raw_ref = _text(ref.get("ref") or ref.get("field_path") or ref.get("state_ref"))
    norm = normalize_field_path(raw_ref)
    declared_field_path = normalize_field_path(ref.get("field_path"))
    if norm in root_index:
        return {
            "owner_kind": "root_schema",
            "owner_field_path": _text(root_index[norm].get("field_path")) or norm,
            "reason": "direct_root_owner",
        }
    if declared_field_path in root_index:
        return {
            "owner_kind": "root_schema",
            "owner_field_path": _text(root_index[declared_field_path].get("field_path")) or declared_field_path,
            "reason": "declared_obligation_field_path",
        }
    mapped = _map_known_runtime_alias(norm, root_index)
    if mapped:
        owner_kind = "root_schema" if mapped in root_index else "downstream_extension"
        return {
            "owner_kind": owner_kind,
            "owner_field_path": _text(root_index.get(mapped, {}).get("field_path")) if owner_kind == "root_schema" else mapped,
            "reason": "known_runtime_alias",
        }
    if raw_ref.startswith("state::") and norm:
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": f"runtimeState.{norm}",
            "reason": "explicit_state_namespace",
        }
    if _is_downstream_field_path(declared_field_path):
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": declared_field_path,
            "reason": "declared_downstream_field_path",
        }
    if _is_downstream_field_path(norm):
        return {
            "owner_kind": "downstream_extension",
            "owner_field_path": norm,
            "reason": "downstream_namespace",
        }
    return None


def _map_known_runtime_alias(norm: str, root_index: Mapping[str, Any]) -> str:
    if norm.startswith("outcome."):
        return f"taskOutcome.{norm.split('.', 1)[1]}"
    if norm == "page_runtime":
        return "runtime.page"
    if norm == "query_context":
        return "queryState.request" if "queryState.request" in root_index else ""
    if norm in {"c_station_selection", "station_selection"}:
        return "stationPicker.query" if "stationPicker.query" in root_index else ""
    if "departure_time_picker" in norm or "selected_departure_time" in norm:
        return "queryState.filters" if "queryState.filters" in root_index else ""
    if "inline_activation" in norm:
        return _runtime_alias_with_leaf_suffix(norm, "inline_activation", "ui.inlineActivation")
    if "action_sheet" in norm:
        return _runtime_alias_with_leaf_suffix(norm, "action_sheet", "ui.actionSheet")
    for marker in ("runtime_overlay_stack", "overlay_stack", "runtime_overlay"):
        if marker in norm:
            return _runtime_alias_with_leaf_suffix(norm, marker, "ui.overlayStack")
    if norm.endswith("_overlay"):
        return "ui.overlayStack"
    if "selected_record" in norm:
        return "selection.current" if "selection.current" in root_index else "runtimeState.selectedRecord"
    if norm.startswith("draft.station_select") or ("station_select" in norm and "task_" in norm):
        return "stationPicker.query" if "stationPicker.query" in root_index else ""
    if norm.startswith("draft.") and "passenger" in norm:
        return "identityForm.recordDraft" if "identityForm.recordDraft" in root_index else ""
    if norm.startswith("selection.origin"):
        return "searchForm.origin" if "searchForm.origin" in root_index else ""
    if norm.startswith("selection.destination"):
        return "searchForm.destination" if "searchForm.destination" in root_index else ""
    if norm.startswith("selection.travel") or norm.startswith("selection.date"):
        return "searchForm.date" if "searchForm.date" in root_index else ""
    return ""


def _looks_like_runtime_state_alias(value: str) -> bool:

    text = _text(value)
    normalized = normalize_field_path(text)
    if text.startswith(("state::", "draft::", "global_execution_schema:")):
        return True
    if normalized.startswith((*DOWNSTREAM_FIELD_PREFIXES, "outcome.")):
        return True
    return normalized in {
        "page_runtime",
        "query_context",
        "c_station_selection",
        "station_selection",
    }


def _runtime_alias_with_leaf_suffix(norm: str, marker: str, owner_field_path: str) -> str:

    marker_index = norm.find(marker)
    if marker_index < 0:
        return owner_field_path
    suffix = norm[marker_index + len(marker):]
    if suffix.startswith(".") and len(suffix) > 1:
        return f"{owner_field_path}{suffix}"
    return owner_field_path


def _is_downstream_field_path(value: str) -> bool:
    return bool(value) and value.startswith(DOWNSTREAM_FIELD_PREFIXES)


def _field_type_for_projection(field_path: str) -> str:
    if field_path.startswith("taskOutcome.") or field_path.startswith("checkpointState."):
        return "CheckpointValue"
    if field_path.startswith("runtimeState.") or field_path.startswith("runtime."):
        return "RuntimeValue"
    if field_path.startswith("draft."):
        return "DraftRuntimeState"
    if field_path.startswith("ui."):
        return "UiRuntimeState"
    return "RuntimeValue"


def _not_root_reason_for_projection(field_path: str, reason: str = "") -> str:
    if field_path.startswith("taskOutcome."):
        return "terminal_outcome_downstream_projection"
    if field_path.startswith("checkpointState."):
        return "checkpoint_local_state"
    if field_path.startswith("draft."):
        return "task_scoped_draft_runtime_state"
    if field_path.startswith("ui."):
        return "ui_runtime_state_not_business_state"
    if field_path.startswith("runtime."):
        return "route_runtime_state_not_business_state"
    if field_path.startswith("runtimeState."):
        return "runtime_checkpoint_local_state"
    return reason or "downstream_extension_state"


def _append_unique(target: List[str], value: Any) -> None:
    text = _text(value)
    if text and text not in target:
        target.append(text)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iter_ref_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = value.strip()
        if _looks_like_ref(text):
            yield text
    elif isinstance(value, list):
        for item in value:
            yield from _iter_ref_values(item)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_ref_values(item)


def _extract_state_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip()
            if _looks_like_ref(key_text):
                yield key_text
            yield from _extract_state_keys(child)
    elif isinstance(value, list):
        for item in value:
            yield from _extract_state_keys(item)
    elif isinstance(value, str) and _looks_like_ref(value):
        yield value.strip()


def _looks_like_ref(value: str) -> bool:
    return value.startswith((
        "state::",
        "draft::",
        "searchForm.",
        "stationPicker.",
        "queryState.",
        "identityForm.",
        "identityProfile.",
        "taskOutcome.",
        "checkpointState.",
        "runtimeState.",
        "ui.",
        "runtime.",
    ))
