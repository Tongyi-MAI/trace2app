
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Dict, Mapping

from .global_execution_schema import normalize_field_path, resolve_field_owner
from .schema_obligations import infer_search_field_path


DOWNSTREAM_SCHEMA_PREFIXES = ("runtime.", "runtimeState.", "ui.", "taskOutcome.", "checkpointState.")
PAGEENV_RUNTIME_IDENTITY_PREFIXES = ("state::", "draft::", "runtime::", "ui::")
PAGEENV_RUNTIME_IDENTITY_EXACT = {"state::page_runtime", "page_runtime"}


def project_global_schema_into_page_environment_specs(
    page_environment_specs: Mapping[str, Any],
    global_execution_schema: Mapping[str, Any],
    *,
    source: str = "phase_consolidation/schema_registry/global_execution_schema.json",
) -> Dict[str, Any]:

    result = deepcopy(dict(page_environment_specs or {}))
    previous_projection = result.get("global_schema_projection")
    projection_history = list(result.get("global_schema_projection_history") or [])
    if isinstance(previous_projection, dict):
        previous_source = previous_projection.get("source")
        if previous_source and not any(
            isinstance(item, dict) and item.get("source") == previous_source
            for item in projection_history
        ):
            projection_history.append(previous_projection)
    specs = result.get("page_environment_specs_by_filename")
    if not isinstance(specs, dict):
        return result
    projection_count = 0
    cleared_stale_ref_count = 0
    downstream_ref_count = 0
    unresolved_stateful_projection_count = 0
    for filename, spec in specs.items():
        if not isinstance(spec, dict):
            continue
        for collection_name in ("parameterized_inputs", "query_projections", "render_projections"):
            rows = spec.get(collection_name)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if _annotate_row(row, global_execution_schema):
                    projection_count += 1
                else:
                    cleanup = clear_stale_global_schema_refs(row, global_execution_schema)
                    cleared_stale_ref_count += cleanup["cleared_stale_ref_count"]
                    downstream_ref_count += cleanup["downstream_ref_count"]
                    if cleanup["state_bearing_unresolved"]:
                        unresolved_stateful_projection_count += 1
        for transition in spec.get("transitions", []) if isinstance(spec.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            state_effect = transition.get("state_effect")
            if isinstance(state_effect, dict) and _annotate_row(state_effect, global_execution_schema):
                transition["schema_ref"] = state_effect.get("schema_ref")
                transition["field_path"] = state_effect.get("field_path")
                projection_count += 1
            elif isinstance(state_effect, dict):
                cleanup = clear_stale_global_schema_refs(state_effect, global_execution_schema)
                cleared_stale_ref_count += cleanup["cleared_stale_ref_count"]
                downstream_ref_count += cleanup["downstream_ref_count"]
                if cleanup["state_bearing_unresolved"]:
                    unresolved_stateful_projection_count += 1
                transition.pop("schema_ref", None)
                transition.pop("field_path", None)
    result["global_schema_projection"] = {
        "format": "page_environment_global_schema_projection_v1",
        "schema_id": global_execution_schema.get("schema_id", "global_execution_schema"),
        "source": source,
        "canonical_schema_hash": _stable_schema_hash(global_execution_schema),
        "annotated_projection_count": projection_count,
        "cleared_stale_ref_count": cleared_stale_ref_count,
        "downstream_ref_count": downstream_ref_count,
        "unresolved_stateful_projection_count": unresolved_stateful_projection_count,
        "status": "pass" if unresolved_stateful_projection_count == 0 else "fail",
    }
    if projection_history:
        result["global_schema_projection_history"] = projection_history
    return result


def clear_stale_global_schema_refs(
    row: Dict[str, Any],
    global_schema: Mapping[str, Any],
) -> Dict[str, Any]:

    cleared = 0
    downstream = 0
    state_bearing_unresolved = False
    candidate_refs = []
    schema_ref = row.get("schema_ref")
    if isinstance(schema_ref, str) and schema_ref.startswith("global_execution_schema:"):
        candidate_refs.append(schema_ref.rsplit(":", 1)[-1])
    if row.get("field_path"):
        candidate_refs.append(row.get("field_path"))

    seen_refs: set[str] = set()
    for ref in candidate_refs:
        norm = normalize_field_path(ref)
        if not norm or norm in seen_refs:
            continue
        seen_refs.add(norm)
        if resolve_field_owner(global_schema, norm):
            continue
        if norm.startswith(DOWNSTREAM_SCHEMA_PREFIXES):
            row["downstream_schema_ref"] = f"downstream_schema:{norm}"
            downstream += 1
        if row.pop("schema_ref", None) is not None:
            cleared += 1
        if row.pop("field_path", None) is not None:
            cleared += 1
        row.pop("field_alias_source", None)

    if downstream == 0 and row_requires_global_schema_projection(row, global_schema):
        state_bearing_unresolved = True
    return {
        "cleared_stale_ref_count": cleared,
        "downstream_ref_count": downstream,
        "state_bearing_unresolved": state_bearing_unresolved,
    }


def _annotate_row(row: Dict[str, Any], global_schema: Mapping[str, Any]) -> bool:
    field_path = _infer_row_field_path(row, global_schema)
    if not field_path:
        return False
    owner = resolve_field_owner(global_schema, field_path)
    if not owner:
        return False
    row["field_path"] = owner.get("field_path", field_path)
    row["schema_ref"] = f"global_execution_schema:{owner.get('field_path', field_path)}"
    row["field_alias_source"] = _row_alias_source(row)
    return True


def _infer_row_field_path(row: Mapping[str, Any], global_schema: Mapping[str, Any]) -> str:
    for key in ("field_path", "schema_ref", "state_ref", "query_state_owner", "reads_state", "writes_state", "draft_ref"):
        value = row.get(key)
        if key == "schema_ref" and isinstance(value, str) and ":" in value:
            value = value.rsplit(":", 1)[-1]
        owner = resolve_field_owner(global_schema, value)
        if owner:
            return str(owner.get("field_path") or "")
    inferred = infer_search_field_path(row)
    if inferred and resolve_field_owner(global_schema, inferred):
        return inferred
    return ""


def _row_alias_source(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key in ("state_ref", "query_state_owner", "reads_state", "writes_state", "draft_ref", "field_role", "parameter_id")
        if (value := row.get(key)) not in (None, "", [], {})
    }


def row_requires_global_schema_projection(row: Mapping[str, Any], global_schema: Mapping[str, Any]) -> bool:

    inferred = infer_search_field_path(row)
    if inferred:
        return not bool(resolve_field_owner(global_schema, inferred))
    for key in ("state_ref", "query_state_owner", "reads_state", "writes_state", "draft_ref"):
        value = row.get(key)
        if not value or resolve_field_owner(global_schema, value):
            continue
        text = str(value or "").strip()
        if _is_pageenv_runtime_identity(text):
            continue
        norm = normalize_field_path(text)
        if _looks_like_global_field_path(norm):
            return True
    return False


def _is_pageenv_runtime_identity(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text in PAGEENV_RUNTIME_IDENTITY_EXACT:
        return True
    return text.startswith(PAGEENV_RUNTIME_IDENTITY_PREFIXES)


def _looks_like_global_field_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text or "::" in text:
        return False
    if text.startswith(DOWNSTREAM_SCHEMA_PREFIXES):
        return False
    return "." in text


def _stable_schema_hash(global_schema: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(global_schema, ensure_ascii=False, sort_keys=True)
    except TypeError:
        payload = str(global_schema)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
