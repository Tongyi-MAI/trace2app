
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence


CANONICAL_SCHEMA_FORMAT = "global_execution_schema_v1"
OWNER_INDEX_FORMAT = "global_execution_schema_owner_index_v1"
API_LLM_SOURCE_KIND = "api_llm_schema_inference"
API_LLM_SCHEMA_STAGE = "site_model_schema_inference"
DETERMINISTIC_SOURCE_KIND = "deterministic_obligation_owner_index"


ANONYMOUS_FIELD_MARKERS = (
    "business_query",
    "query_draft",
    "draft::",
)


@dataclass(frozen=True)
class SchemaObligation:

    obligation_id: str
    obligation_kind: str
    family_id: str
    field_path: str = ""
    field_type: str = "string"
    lifecycle: str = "runtime"
    source_collection: str = ""
    candidate_projection: List[str] = field(default_factory=list)
    commit_target: str = ""
    producers: List[str] = field(default_factory=list)
    consumers: List[str] = field(default_factory=list)
    evidence_steps: List[str] = field(default_factory=list)
    projection_pages: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SchemaObligation":
        return cls(
            obligation_id=_text(value.get("obligation_id")),
            obligation_kind=_text(value.get("obligation_kind") or value.get("kind")),
            family_id=_text(value.get("family_id") or "global"),
            field_path=_text(value.get("field_path")),
            field_type=_text(value.get("field_type") or "string"),
            lifecycle=_text(value.get("lifecycle") or "runtime"),
            source_collection=_text(value.get("source_collection")),
            candidate_projection=_string_list(value.get("candidate_projection")),
            commit_target=_text(value.get("commit_target")),
            producers=_string_list(value.get("producers")),
            consumers=_string_list(value.get("consumers")),
            evidence_steps=_string_list(value.get("evidence_steps")),
            projection_pages=_string_list(value.get("projection_pages")),
            aliases=_string_list(value.get("aliases")),
            metadata=dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), Mapping) else {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            key: val
            for key, val in {
                "obligation_id": self.obligation_id,
                "obligation_kind": self.obligation_kind,
                "family_id": self.family_id,
                "field_path": self.field_path,
                "field_type": self.field_type,
                "lifecycle": self.lifecycle,
                "source_collection": self.source_collection,
                "candidate_projection": self.candidate_projection,
                "commit_target": self.commit_target,
                "producers": self.producers,
                "consumers": self.consumers,
                "evidence_steps": self.evidence_steps,
                "projection_pages": self.projection_pages,
                "aliases": self.aliases,
                "metadata": self.metadata,
            }.items()
            if val not in ("", [], {})
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    result: List[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def normalize_field_path(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    for prefix in ("GlobalExecutionSchema.", "RailwayAppState.", "AppState."):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("state::"):
        text = text[len("state::"):]
    if text.startswith("searchState."):
        text = "queryState." + text[len("searchState."):]
    legacy_exact = {
        "searchForm.travelDate": "searchForm.date",
        "selection.seatClassOptions": "selection.options",
        "selection.seatPreference": "selection.preference",
        "passengerForm.passengerType": "identityForm.personType",
    }
    text = legacy_exact.get(text, text)
    for old_prefix, new_prefix in (
        ("passengerForm.", "identityForm."),
        ("passengerProfile.", "identityProfile."),
    ):
        if text.startswith(old_prefix):
            text = new_prefix + text[len(old_prefix):]
            break
    return text.replace("::", ".").strip(".")


def is_anonymous_or_local_ref(value: Any) -> bool:
    text = _text(value)
    lower = text.lower()
    if not text:
        return True
    if any(marker in lower for marker in ANONYMOUS_FIELD_MARKERS):
        return True
    return False


def build_schema_owner_index(
    obligations: Iterable[Mapping[str, Any] | SchemaObligation],
    *,
    schema_id: str = "schema_owner_index",
) -> Dict[str, Any]:

    fields: Dict[str, Dict[str, Any]] = {}
    rejected: List[Dict[str, Any]] = []
    families: Dict[str, Dict[str, Any]] = {}

    for raw in obligations:
        obligation = raw if isinstance(raw, SchemaObligation) else SchemaObligation.from_mapping(raw)
        family_id = obligation.family_id or "global"
        family = families.setdefault(
            family_id,
            {
                "family_id": family_id,
                "obligation_ids": [],
                "field_paths": [],
            },
        )
        if obligation.obligation_id:
            family["obligation_ids"].append(obligation.obligation_id)
        field_path = normalize_field_path(obligation.field_path)
        if not field_path or is_anonymous_or_local_ref(field_path):
            rejected.append({
                "obligation_id": obligation.obligation_id,
                "obligation_kind": obligation.obligation_kind,
                "family_id": family_id,
                "reason": "missing_or_anonymous_global_field_path",
                "raw_field_path": obligation.field_path,
            })
            continue
        entry = fields.setdefault(
            field_path,
            {
                "field_path": field_path,
                "field_type": obligation.field_type,
                "lifecycle": obligation.lifecycle,
                "families": [],
                "producers": [],
                "consumers": [],
                "evidence_steps": [],
                "projection_pages": [],
                "aliases": [],
                "source_obligations": [],
            },
        )
        _append_unique(entry["families"], family_id)
        _append_unique(entry["source_obligations"], obligation.obligation_id)
        _extend_unique(entry["producers"], obligation.producers)
        _extend_unique(entry["consumers"], obligation.consumers)
        _extend_unique(entry["evidence_steps"], obligation.evidence_steps)
        _extend_unique(entry["projection_pages"], obligation.projection_pages)
        _extend_unique(entry["aliases"], obligation.aliases)
        if obligation.source_collection:
            entry["source_collection"] = obligation.source_collection
        if obligation.candidate_projection:
            entry["candidate_projection"] = obligation.candidate_projection
        if obligation.commit_target:
            entry["commit_target"] = obligation.commit_target
        if field_path not in family["field_paths"]:
            family["field_paths"].append(field_path)

    return {
        "format": OWNER_INDEX_FORMAT,
        "schema_id": schema_id,
        "source_provenance": {
            "source_kind": DETERMINISTIC_SOURCE_KIND,
            "canonical": False,
        },
        "global_fields": sorted(fields.values(), key=lambda item: item["field_path"]),
        "families": sorted(families.values(), key=lambda item: item["family_id"]),
        "rejected_obligations": rejected,
    }


def build_global_execution_schema(
    obligations: Iterable[Mapping[str, Any] | SchemaObligation],
    *,
    schema_id: str = "global_execution_schema",
) -> Dict[str, Any]:

    payload = build_schema_owner_index(obligations, schema_id=schema_id)
    payload["format"] = CANONICAL_SCHEMA_FORMAT
    payload["source_provenance"] = {
        "source_kind": DETERMINISTIC_SOURCE_KIND,
        "canonical": False,
    }
    return payload


def normalize_canonical_global_execution_schema(
    raw: Mapping[str, Any],
    *,
    schema_id: str = "global_execution_schema",
    source: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:

    if not isinstance(raw, Mapping):
        raise ValueError("canonical global execution schema output must be a JSON object")
    payload = raw.get("global_execution_schema") if isinstance(raw.get("global_execution_schema"), Mapping) else raw
    if not isinstance(payload, Mapping):
        raise ValueError("missing global_execution_schema object")
    raw_fields = payload.get("global_fields", [])
    if raw_fields is None:
        raw_fields = []
    if not isinstance(raw_fields, list):
        raise ValueError("global_execution_schema.global_fields must be a list")

    fields: List[Dict[str, Any]] = []
    seen_fields: set[str] = set()
    for idx, raw_field in enumerate(raw_fields):
        if not isinstance(raw_field, Mapping):
            raise ValueError(f"global_fields[{idx}] must be an object")
        field_path = normalize_field_path(raw_field.get("field_path"))
        if not field_path:
            raise ValueError(f"global_fields[{idx}] is missing field_path")
        if is_anonymous_or_local_ref(field_path):
            raise ValueError(f"global_fields[{idx}] uses non-canonical owner {field_path!r}")
        if field_path in seen_fields:
            raise ValueError(f"duplicate global field_path {field_path!r}")
        seen_fields.add(field_path)
        fields.append(_normalize_field(raw_field, field_path=field_path))

    autocomplete_contracts = [
        item
        for item in (
            _normalize_autocomplete_contract(raw_contract)
            for raw_contract in payload.get("autocomplete_contracts", []) or []
            if isinstance(raw_contract, Mapping)
        )
        if item
    ]
    _promote_autocomplete_refs_to_global_fields(fields, seen_fields, autocomplete_contracts)
    rejected_obligations = [
        item
        for item in (
            _normalize_rejected_obligation(raw_item)
            for raw_item in payload.get("rejected_obligations", []) or []
            if isinstance(raw_item, Mapping)
        )
        if item
    ]
    provenance = dict(source or {})
    provenance.setdefault("source_kind", API_LLM_SOURCE_KIND)
    provenance.setdefault("stage", API_LLM_SCHEMA_STAGE)
    provenance.setdefault("canonical", True)
    return {
        "format": CANONICAL_SCHEMA_FORMAT,
        "schema_id": _text(payload.get("schema_id") or schema_id),
        "source_provenance": provenance,
        "global_fields": sorted(fields, key=lambda item: item["field_path"]),
        "autocomplete_contracts": autocomplete_contracts,
        "rejected_obligations": rejected_obligations,
    }


def complete_global_schema_from_obligations(
    global_schema: Mapping[str, Any],
    schema_obligations: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
) -> Dict[str, Any]:

    payload = dict(global_schema or {})
    fields = [
        dict(field)
        for field in payload.get("global_fields", []) or []
        if isinstance(field, Mapping)
    ]
    seen_fields: set[str] = set()
    alias_to_field: Dict[str, str] = {}
    for field in fields:
        field_path = normalize_field_path(field.get("field_path"))
        if field_path:
            seen_fields.add(field_path)
        for alias in _string_list(field.get("aliases")):
            alias_norm = normalize_field_path(alias)
            if alias_norm and field_path:
                alias_to_field[alias_norm] = field_path

    completed: List[str] = []
    for raw_obligation in _normalize_obligation_list(schema_obligations):
        obligation = SchemaObligation.from_mapping(raw_obligation)
        field_path = normalize_field_path(obligation.field_path)
        if not field_path or is_anonymous_or_local_ref(field_path):
            continue
        if field_path in seen_fields or field_path in alias_to_field:
            continue
        resolved_by_alias = False
        for alias in _string_list(obligation.aliases):
            alias_norm = normalize_field_path(alias)
            if alias_norm in seen_fields or alias_norm in alias_to_field:
                resolved_by_alias = True
                break
        if resolved_by_alias:
            continue

        raw_field = {
            "field_path": field_path,
            "field_type": obligation.field_type,
            "lifecycle": obligation.lifecycle,
            "initial_value_policy": "from_trace_or_empty",
            "runtime_value_policy": "mutated_by_action_or_derived",
            "producers": obligation.producers or ["schema_obligations"],
            "consumers": obligation.consumers or ["page_environment_spec", "pagegen", "runtime_backend"],
            "evidence_steps": obligation.evidence_steps,
            "projection_pages": obligation.projection_pages,
            "aliases": obligation.aliases,
            "source_collection": obligation.source_collection,
            "candidate_projection": obligation.candidate_projection,
            "commit_target": obligation.commit_target,
            "description": "Owner completed from schema_obligations hard requirement",
        }
        field = _normalize_field(raw_field, field_path=field_path)
        if obligation.obligation_id:
            field["source_obligations"] = [obligation.obligation_id]
        if obligation.family_id:
            field["families"] = [obligation.family_id]
        fields.append(field)
        seen_fields.add(field_path)
        completed.append(field_path)

    payload["global_fields"] = sorted(fields, key=lambda item: item.get("field_path", ""))
    if completed:
        provenance = dict(payload.get("source_provenance") or {})
        provenance["owner_completion_from_schema_obligations"] = True
        provenance["owner_completion_count"] = len(completed)
        provenance["owner_completion_field_paths"] = sorted(completed)
        payload["source_provenance"] = provenance
    return payload


def mark_unreferenced_global_fields_optional(
    global_schema: Mapping[str, Any],
    schema_obligations: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    *,
    reason: str = "LLM-inferred field has no formal schema obligation; retained as optional projection evidence.",
) -> Dict[str, Any]:

    payload = dict(global_schema or {})
    fields = [
        dict(field)
        for field in payload.get("global_fields", []) or []
        if isinstance(field, Mapping)
    ]
    fields_by_path: Dict[str, Mapping[str, Any]] = {}
    alias_to_field: Dict[str, str] = {}
    for field in fields:
        field_path = normalize_field_path(field.get("field_path"))
        if not field_path:
            continue
        fields_by_path[field_path] = field
        for alias in _string_list(field.get("aliases")):
            alias_norm = normalize_field_path(alias)
            if alias_norm:
                alias_to_field[alias_norm] = field_path

    zero_ref = _global_fields_without_formal_obligations(
        fields_by_path,
        _normalize_obligation_list(schema_obligations),
        alias_to_field,
        payload.get("autocomplete_contracts", []) if isinstance(payload, Mapping) else [],
    )
    downgrade_paths = {
        _text(item.get("field_path"))
        for item in zero_ref.get("required_fields", [])
        if _text(item.get("field_path"))
    }
    if not downgrade_paths:
        return payload

    updated_fields: List[Dict[str, Any]] = []
    for field in fields:
        field_path = normalize_field_path(field.get("field_path"))
        if field_path in downgrade_paths:
            field["optional_inferred"] = True
            field["optional_inferred_reason"] = reason
        updated_fields.append(field)
    payload["global_fields"] = sorted(updated_fields, key=lambda item: item.get("field_path", ""))

    provenance = dict(payload.get("source_provenance") or {})
    provenance["optional_inferred_from_unreferenced_llm_fields"] = True
    provenance["optional_inferred_count"] = len(downgrade_paths)
    provenance["optional_inferred_field_paths"] = sorted(downgrade_paths)
    payload["source_provenance"] = provenance
    return payload


def validate_canonical_global_execution_schema(
    global_schema: Mapping[str, Any],
    *,
    schema_obligations: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    require_api_llm_source: bool = True,
) -> Dict[str, Any]:

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(global_schema, Mapping):
        errors.append({"code": "schema_not_object"})
        return _validation_report(errors, warnings, [], [], [])

    if global_schema.get("format") != CANONICAL_SCHEMA_FORMAT:
        errors.append({
            "code": "invalid_format",
            "expected": CANONICAL_SCHEMA_FORMAT,
            "actual": global_schema.get("format"),
        })
    provenance = global_schema.get("source_provenance", {})
    source_kind = provenance.get("source_kind") if isinstance(provenance, Mapping) else ""
    if require_api_llm_source and source_kind != API_LLM_SOURCE_KIND:
        errors.append({
            "code": "non_api_llm_schema_source",
            "source_kind": source_kind or "missing",
        })
    source_stage = provenance.get("stage") if isinstance(provenance, Mapping) else ""
    if require_api_llm_source and source_stage != API_LLM_SCHEMA_STAGE:
        errors.append({
            "code": "non_schema_inference_stage",
            "stage": source_stage or "missing",
            "expected_stage": API_LLM_SCHEMA_STAGE,
        })

    fields_by_path: Dict[str, Mapping[str, Any]] = {}
    field_paths: set[str] = set()
    alias_to_field: Dict[str, str] = {}
    for idx, field in enumerate(global_schema.get("global_fields", []) or []):
        if not isinstance(field, Mapping):
            errors.append({"code": "field_not_object", "index": idx})
            continue
        field_path = normalize_field_path(field.get("field_path"))
        if not field_path:
            errors.append({"code": "field_missing_path", "index": idx})
            continue
        if is_anonymous_or_local_ref(field_path):
            errors.append({"code": "field_uses_local_or_anonymous_owner", "field_path": field_path})
        if field_path in field_paths:
            errors.append({"code": "duplicate_field_path", "field_path": field_path})
        field_paths.add(field_path)
        fields_by_path[field_path] = field
        for alias in _string_list(field.get("aliases")):
            alias_norm = normalize_field_path(alias)
            if alias_norm:
                alias_to_field[alias_norm] = field_path

    for idx, contract in enumerate(global_schema.get("autocomplete_contracts", []) or []):
        if not isinstance(contract, Mapping):
            errors.append({"code": "autocomplete_contract_not_object", "index": idx})
            continue
        for key in ("query_ref", "suggestions_ref"):
            ref = normalize_field_path(contract.get(key))
            if ref and ref not in field_paths and ref not in alias_to_field:
                errors.append({
                    "code": "autocomplete_ref_without_global_owner",
                    "index": idx,
                    "key": key,
                    "ref": ref,
                })
        commit_target = _text(contract.get("commit_target"))
        if commit_target and "[" not in commit_target:
            commit_norm = normalize_field_path(commit_target)
            if commit_norm and commit_norm not in field_paths and commit_norm not in alias_to_field:
                warnings.append({
                    "code": "autocomplete_commit_target_without_owner",
                    "index": idx,
                    "ref": commit_norm,
                })

    obligations = _normalize_obligation_list(schema_obligations)
    rejected_ids: set[str] = set()
    for idx, item in enumerate(global_schema.get("rejected_obligations", []) or []):
        if not isinstance(item, Mapping):
            errors.append({"code": "rejected_obligation_not_object", "index": idx})
            continue
        obligation_id = _text(item.get("obligation_id"))
        reason = _text(item.get("reason"))
        if obligation_id:
            rejected_ids.add(obligation_id)
        if obligation_id and len(reason) < 8:
            errors.append({
                "code": "rejected_obligation_missing_specific_reason",
                "obligation_id": obligation_id,
            })
    resolved: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for obligation in obligations:
        obligation_id = _text(obligation.get("obligation_id"))
        field_path = normalize_field_path(obligation.get("field_path"))
        aliases = [normalize_field_path(alias) for alias in _string_list(obligation.get("aliases"))]
        owner = ""
        if field_path and field_path in field_paths:
            owner = field_path
        elif field_path and field_path in alias_to_field:
            owner = alias_to_field[field_path]
        else:
            for alias in aliases:
                if alias in field_paths:
                    owner = alias
                    break
                if alias in alias_to_field:
                    owner = alias_to_field[alias]
                    break
        if owner:
            resolved.append({
                "obligation_id": obligation_id,
                "resolution": "promoted_or_aliased",
                "owner_field_path": owner,
            })
        elif obligation_id and obligation_id in rejected_ids:
            resolved.append({
                "obligation_id": obligation_id,
                "resolution": "rejected",
            })
        else:
            unresolved.append({
                "obligation_id": obligation_id,
                "field_path": field_path,
                "reason": "obligation_not_promoted_aliased_or_rejected",
            })
    if unresolved:
        errors.append({
            "code": "unresolved_schema_obligations",
            "count": len(unresolved),
        })
    zero_ref_fields = _global_fields_without_formal_obligations(
        fields_by_path,
        obligations,
        alias_to_field,
        global_schema.get("autocomplete_contracts", []) if isinstance(global_schema, Mapping) else [],
    )
    if obligations and zero_ref_fields.get("required_count", 0):
        errors.append({
            "code": "required_global_fields_without_formal_obligations",
            "count": zero_ref_fields.get("required_count", 0),
            "field_paths": [
                item.get("field_path")
                for item in zero_ref_fields.get("required_fields", [])
            ],
        })
    return _validation_report(
        errors,
        warnings,
        resolved,
        unresolved,
        sorted(field_paths),
        global_fields_without_formal_obligations=zero_ref_fields,
    )


def validate_canonical_schema_against_obligations(
    global_schema: Mapping[str, Any],
    schema_obligations: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    *,
    require_api_llm_source: bool = True,
) -> Dict[str, Any]:

    return validate_canonical_global_execution_schema(
        global_schema,
        schema_obligations=schema_obligations,
        require_api_llm_source=require_api_llm_source,
    )


def build_field_owner_index(global_schema: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for field in global_schema.get("global_fields", []) if isinstance(global_schema, Mapping) else []:
        if not isinstance(field, Mapping):
            continue
        field_path = normalize_field_path(field.get("field_path"))
        if not field_path:
            continue
        entry = dict(field)
        for key in [field_path, f"state::{field_path}", *(_string_list(field.get("aliases")))]:
            norm = normalize_field_path(key)
            if norm:
                index[norm] = entry
    return index


def resolve_field_owner(global_schema: Mapping[str, Any], ref: Any) -> Dict[str, Any] | None:
    norm = normalize_field_path(ref)
    if not norm:
        return None
    owner = build_field_owner_index(global_schema).get(norm)
    if owner:
        return owner
    if is_anonymous_or_local_ref(ref):
        return None
    return None


def validate_required_refs(
    global_schema: Mapping[str, Any],
    required_refs: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    unresolved: List[Dict[str, Any]] = []
    resolved: List[Dict[str, Any]] = []
    for ref in required_refs:
        raw_ref = _text(ref.get("ref") or ref.get("field_path") or ref.get("state_ref"))
        owner = resolve_field_owner(global_schema, raw_ref)
        row = dict(ref)
        row["normalized_ref"] = normalize_field_path(raw_ref)
        if owner:
            row["owner_field_path"] = owner.get("field_path")
            resolved.append(row)
        else:
            row["reason"] = "unresolved_global_schema_owner"
            unresolved.append(row)
    return {
        "format": "global_execution_schema_ref_validation_v1",
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "resolved": resolved,
        "unresolved": unresolved,
        "status": "pass" if not unresolved else "fail",
    }


def _append_unique(target: List[str], value: str) -> None:
    if value and value not in target:
        target.append(value)


def _extend_unique(target: List[str], values: Iterable[str]) -> None:
    for value in values:
        _append_unique(target, value)


def _normalize_field(raw_field: Mapping[str, Any], *, field_path: str) -> Dict[str, Any]:
    field = {
        "field_path": field_path,
        "field_type": _text(raw_field.get("field_type") or raw_field.get("type") or "string"),
        "lifecycle": _text(raw_field.get("lifecycle") or "runtime"),
        "initial_value_policy": _text(raw_field.get("initial_value_policy") or "from_trace_or_empty"),
        "runtime_value_policy": _text(raw_field.get("runtime_value_policy") or "mutated_by_action_or_derived"),
        "producers": _string_list(raw_field.get("producers")),
        "consumers": _string_list(raw_field.get("consumers")),
        "evidence_steps": _string_list(raw_field.get("evidence_steps")),
        "projection_pages": _string_list(raw_field.get("projection_pages")),
        "aliases": _string_list(raw_field.get("aliases")),
    }
    for optional_key in (
        "description",
        "source_collection",
        "candidate_projection",
        "commit_target",
        "optional_inferred",
        "optional_inferred_reason",
        "source_obligations",
        "families",
    ):
        value = raw_field.get(optional_key)
        if value not in (None, "", [], {}):
            field[optional_key] = value
    return field


def _global_fields_without_formal_obligations(
    fields_by_path: Mapping[str, Mapping[str, Any]],
    obligations: Iterable[Mapping[str, Any]],
    alias_to_field: Mapping[str, str],
    autocomplete_contracts: Iterable[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    formally_referenced: set[str] = set()
    direct_pre_counts: Dict[str, int] = {}
    direct_post_counts: Dict[str, int] = {}
    runtime_or_checkpoint_counts: Dict[str, int] = {}
    for obligation in obligations:
        if not isinstance(obligation, Mapping):
            continue
        refs = [normalize_field_path(obligation.get("field_path"))]
        refs.extend(normalize_field_path(alias) for alias in _string_list(obligation.get("aliases")))
        obligation_kind = _text(obligation.get("obligation_kind"))
        for ref in refs:
            if not ref:
                continue
            owner = ref if ref in fields_by_path else alias_to_field.get(ref, "")
            if not owner:
                continue
            formally_referenced.add(owner)
            if obligation_kind.startswith("pre_pageenv") or obligation_kind in {
                "parameterized_input",
                "query_projection",
                "autocomplete_control",
                "transition_state_effect",
                "runtime_state_ref",
                "trace_execution",
            }:
                direct_pre_counts[owner] = direct_pre_counts.get(owner, 0) + 1
            elif "checkpoint" in obligation_kind or "runtime_schema" in obligation_kind:
                runtime_or_checkpoint_counts[owner] = runtime_or_checkpoint_counts.get(owner, 0) + 1
            else:
                direct_post_counts[owner] = direct_post_counts.get(owner, 0) + 1
    for contract in autocomplete_contracts or []:
        if not isinstance(contract, Mapping):
            continue
        for key in ("query_ref", "suggestions_ref", "commit_target"):
            ref = normalize_field_path(contract.get(key))
            if not ref:
                continue
            owner = ref if ref in fields_by_path else alias_to_field.get(ref, "")
            if not owner:
                continue
            formally_referenced.add(owner)
            direct_pre_counts[owner] = direct_pre_counts.get(owner, 0) + 1

    required_fields: List[Dict[str, Any]] = []
    optional_inferred_fields: List[Dict[str, Any]] = []
    for field_path in sorted(fields_by_path):
        if field_path in formally_referenced:
            continue
        field = fields_by_path[field_path]
        row = {
            "field_path": field_path,
            "producer_evidence": _string_list(field.get("producers")),
            "consumer_evidence": _string_list(field.get("consumers")),
            "direct_pre_obligations": direct_pre_counts.get(field_path, 0),
            "direct_post_obligations": direct_post_counts.get(field_path, 0),
            "runtime_or_checkpoint_consumers": runtime_or_checkpoint_counts.get(field_path, 0),
        }
        if field.get("optional_inferred") is True:
            row["decision"] = "optional_inferred"
            row["reason"] = _text(field.get("optional_inferred_reason")) or "field_marked_optional_inferred"
            optional_inferred_fields.append(row)
        else:
            row["decision"] = "required_missing_formal_obligation"
            row["reason"] = "required root field has no direct schema obligation, alias obligation, or source obligation"
            required_fields.append(row)
    return {
        "required_count": len(required_fields),
        "optional_inferred_count": len(optional_inferred_fields),
        "required_fields": required_fields,
        "optional_inferred_fields": optional_inferred_fields,
    }


def _promote_autocomplete_refs_to_global_fields(
    fields: List[Dict[str, Any]],
    seen_fields: set[str],
    autocomplete_contracts: List[Dict[str, Any]],
) -> None:
    alias_to_field: Dict[str, str] = {}
    for field in fields:
        field_path = normalize_field_path(field.get("field_path"))
        for alias in _string_list(field.get("aliases")):
            alias_norm = normalize_field_path(alias)
            if alias_norm:
                alias_to_field[alias_norm] = field_path

    for contract in autocomplete_contracts:
        for key in ("query_ref", "suggestions_ref"):
            ref = normalize_field_path(contract.get(key))
            if not ref or is_anonymous_or_local_ref(ref):
                continue
            if ref in alias_to_field:
                contract[key] = alias_to_field[ref]
                continue
            contract[key] = ref
            if ref in seen_fields:
                continue
            field_type = "AutocompleteSuggestion[]" if key == "suggestions_ref" else "string"
            fields.append(_normalize_field(
                {
                    "field_path": ref,
                    "field_type": field_type,
                    "lifecycle": "derived" if key == "suggestions_ref" else "runtime",
                    "initial_value_policy": "from_trace_or_empty",
                    "runtime_value_policy": "mutated_by_action_or_derived",
                    "producers": ["autocomplete_contracts"],
                    "consumers": ["page_environment_spec", "pagegen", "runtime_backend"],
                    "source_collection": contract.get("source_collection"),
                    "candidate_projection": contract.get("candidate_projection"),
                    "description": f"Owner promoted from autocomplete_contracts.{key}",
                },
                field_path=ref,
            ))
            seen_fields.add(ref)


def _normalize_autocomplete_contract(raw_contract: Mapping[str, Any]) -> Dict[str, Any]:
    contract = {
        "query_ref": normalize_field_path(raw_contract.get("query_ref")),
        "source_collection": _text(raw_contract.get("source_collection")),
        "candidate_projection": _string_list(raw_contract.get("candidate_projection")),
        "suggestions_ref": normalize_field_path(raw_contract.get("suggestions_ref")),
        "commit_target": _text(raw_contract.get("commit_target")),
    }
    return {key: value for key, value in contract.items() if value not in ("", [], {})}


def _normalize_rejected_obligation(raw_item: Mapping[str, Any]) -> Dict[str, Any]:
    obligation_id = _text(raw_item.get("obligation_id"))
    reason = _text(raw_item.get("reason"))
    if not obligation_id and not reason:
        return {}
    result = {"obligation_id": obligation_id, "reason": reason}
    for key in ("field_path", "family_id", "obligation_kind"):
        value = _text(raw_item.get(key))
        if value:
            result[key] = value
    return result


def _normalize_obligation_list(
    value: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
) -> List[Dict[str, Any]]:
    if not value:
        return []
    raw = value.get("obligations", []) if isinstance(value, Mapping) else value
    result: List[Dict[str, Any]] = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return result
    for item in raw:
        if isinstance(item, SchemaObligation):
            result.append(item.to_dict())
        elif isinstance(item, Mapping):
            result.append(SchemaObligation.from_mapping(item).to_dict())
    return result


def _validation_report(
    errors: List[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
    resolved: List[Dict[str, Any]],
    unresolved: List[Dict[str, Any]],
    field_paths: List[str],
    *,
    global_fields_without_formal_obligations: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "format": "global_execution_schema_validation_v1",
        "status": "pass" if not errors else "fail",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "global_field_count": len(field_paths),
            "resolved_obligation_count": len(resolved),
            "unresolved_obligation_count": len(unresolved),
        },
        "resolved_obligations": resolved,
        "unresolved_obligations": unresolved,
        "field_paths": field_paths,
        "global_fields_without_formal_obligations": dict(
            global_fields_without_formal_obligations or {
                "required_count": 0,
                "optional_inferred_count": 0,
                "required_fields": [],
                "optional_inferred_fields": [],
            }
        ),
    }
