
from __future__ import annotations

import json
from collections.abc import Iterable as IterableABC
from typing import Any, Dict, Iterable, List, Mapping

from .global_execution_schema import SchemaObligation


DEFAULT_SCHEMA_CONTEXT_BUDGET = 12_000


def build_schema_generation_context(
    *,
    schema_candidate_ir: Mapping[str, Any] | None = None,
    schema_obligations: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    traces_context: Iterable[Mapping[str, Any]] | None = None,
    page_identity_context: Mapping[str, Any] | None = None,
    global_structure_context: Any = None,
    content_summary: Any = None,
    layout_content_boundary_context: Mapping[str, Any] | None = None,
    budget_tokens: int = DEFAULT_SCHEMA_CONTEXT_BUDGET,
) -> Dict[str, Any]:

    candidate_ir = schema_candidate_ir if isinstance(schema_candidate_ir, Mapping) else {}
    obligations = _normalize_obligations(schema_obligations)
    task_universe = _build_task_universe(traces_context or [])
    textual_trace_context = _build_textual_trace_context(traces_context or [], obligations)
    catalog_context = _build_catalog_candidate_context(candidate_ir)
    execution_context = _build_execution_obligation_context(obligations)
    site_structure_context = _compact_context_value(global_structure_context, max_chars=4000)
    content_distribution_context = _compact_context_value(content_summary, max_chars=4000)
    family_groups = _build_family_groups(obligations, catalog_context.get("entity_candidates", []))
    prompt_view = _build_prompt_view(
        obligations,
        catalog_context,
        site_structure_context=site_structure_context,
        content_distribution_context=content_distribution_context,
        budget_tokens=budget_tokens,
    )

    context = {
        "format": "schema_generation_context_v1",
        "task_universe": task_universe,
        "page_identity_context": _compact_page_identity_context(page_identity_context or {}),
        "site_structure_context": {
            "format": "schema_generation_site_structure_context_v1",
            "source": "site_model.builder.consolidation._build_global_structure_context",
            "summary": site_structure_context,
            "policy": "Use layout/components/relationships as evidence for schema ownership; do not create schema fields from decorative UI structure alone.",
        },
        "content_distribution_context": {
            "format": "schema_generation_content_distribution_context_v1",
            "source": "site_model.builder.consolidation._build_content_summary",
            "summary": content_distribution_context,
            "policy": "Use cross-page extracted content distribution to merge same-concept entities and keep component-boundary conflicts separate.",
        },
        "catalog_candidate_context": catalog_context,
        "execution_obligation_context": execution_context,
        "textual_trace_context": textual_trace_context,
        "prompt_view": prompt_view,
        "conflict_policy_context": {
            "layout_content_slot_policy": _text(
                (layout_content_boundary_context or {}).get("layout_content_slot_policy")
            ) or "Fixed layout slots with variable task values must become global state fields, not page splits.",
            "component_conflict_identity_policy": _text(
                (layout_content_boundary_context or {}).get("component_conflict_identity_policy")
            ) or "Components conflict only when the same stable slot requires incompatible behavior or schema ownership.",
        },
        "packing_metadata": {
            "budget_tokens": int(budget_tokens or DEFAULT_SCHEMA_CONTEXT_BUDGET),
            "family_count": len(family_groups),
            "families": family_groups,
            "chunking_rule": "Keep all state-bearing obligations for one execution family in the same schema chunk when possible.",
            "token_estimate": estimate_context_tokens({
                "catalog_candidate_context": catalog_context,
                "site_structure_context": site_structure_context,
                "content_distribution_context": content_distribution_context,
                "prompt_view": prompt_view,
                "textual_trace_context": textual_trace_context,
            }),
            "prompt_view_token_estimate": estimate_context_tokens(prompt_view),
        },
    }
    context["packing_metadata"]["chunks"] = prompt_view.get("chunks", [])
    return context


def pack_schema_generation_context_by_family(
    context: Mapping[str, Any],
    *,
    budget_tokens: int = DEFAULT_SCHEMA_CONTEXT_BUDGET,
) -> List[Dict[str, Any]]:

    prompt_view = context.get("prompt_view", {}) if isinstance(context, Mapping) else {}
    if isinstance(prompt_view, Mapping) and isinstance(prompt_view.get("field_owner_requirements"), list):
        return _pack_field_requirements_by_family(
            prompt_view.get("field_owner_requirements", []),
            budget_tokens=budget_tokens,
        )

    exec_context = context.get("execution_obligation_context", {}) if isinstance(context, Mapping) else {}
    obligations = exec_context.get("schema_obligations", []) if isinstance(exec_context, Mapping) else []
    requirements = _build_field_owner_requirements([
        dict(item) for item in obligations if isinstance(item, Mapping)
    ])
    return _pack_field_requirements_by_family(requirements, budget_tokens=budget_tokens)


def _build_prompt_view(
    obligations: List[Dict[str, Any]],
    catalog_context: Mapping[str, Any],
    *,
    site_structure_context: str,
    content_distribution_context: str,
    budget_tokens: int,
) -> Dict[str, Any]:
    requirements = _build_field_owner_requirements(obligations)
    chunks = _pack_field_requirements_by_family(requirements, budget_tokens=budget_tokens)
    unresolved_samples = [
        {
            "obligation_id": _text(item.get("obligation_id")),
            "obligation_kind": _text(item.get("obligation_kind")),
            "family_id": _text(item.get("family_id")),
        }
        for item in obligations
        if not _text(item.get("field_path"))
    ][:40]
    view = {
        "format": "schema_generation_prompt_view_v1",
        "purpose": "Budgeted view for schema inference prompts; full obligations remain in schema_obligations.json.",
        "obligation_count": len(obligations),
        "field_owner_requirement_count": len(requirements),
        "catalog_candidate_count": catalog_context.get("candidate_count", 0),
        "site_structure_context": site_structure_context,
        "content_distribution_context": content_distribution_context,
        "requirements_by_family": _requirements_by_family_table(requirements),
        "unresolved_obligation_samples": unresolved_samples,
        "chunks": chunks,
        "budget_tokens": int(budget_tokens or DEFAULT_SCHEMA_CONTEXT_BUDGET),
        "requirement_detail_policy": (
            "Rows are [field_path, field_type, lifecycle, obligation_count, obligation_kinds]. "
            "Use schema_obligations.json for full source-obligation details."
        ),
    }
    view["token_estimate"] = estimate_context_tokens(view)
    return view


def _compact_context_value(value: Any, *, max_chars: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
        text = text.strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + " ..."
    return text


def _build_field_owner_requirements(obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_field: Dict[tuple[str, str], Dict[str, Any]] = {}
    by_family: Dict[str, List[Dict[str, Any]]] = {}
    for obligation in obligations:
        if not isinstance(obligation, Mapping):
            continue
        field_path = _text(obligation.get("field_path"))
        if not field_path:
            continue
        family_id = _text(obligation.get("family_id")) or "global_execution"
        key = (family_id, field_path)
        entry = by_field.setdefault(key, {
            "requirement_id": f"{family_id}::{field_path}",
            "family_id": family_id,
            "field_path": field_path,
            "field_type": _text(obligation.get("field_type") or "string"),
            "lifecycle": _text(obligation.get("lifecycle") or "runtime"),
            "obligation_count": 0,
            "obligation_kinds": [],
            "producers": [],
            "consumers": [],
            "evidence_steps_sample": [],
            "projection_pages": [],
            "aliases_sample": [],
            "source_obligation_ids_sample": [],
        })
        entry["obligation_count"] += 1
        _append_unique(entry["obligation_kinds"], obligation.get("obligation_kind"))
        _extend_unique_limited(entry["producers"], obligation.get("producers", []), 8)
        _extend_unique_limited(entry["consumers"], obligation.get("consumers", []), 8)
        _extend_unique_limited(entry["evidence_steps_sample"], obligation.get("evidence_steps", []), 8)
        _extend_unique_limited(entry["projection_pages"], obligation.get("projection_pages", []), 8)
        _extend_unique_limited(entry["aliases_sample"], obligation.get("aliases", []), 6)
        _append_unique_limited(entry["source_obligation_ids_sample"], obligation.get("obligation_id"), 12)
    return sorted(by_field.values(), key=lambda item: (item["family_id"], item["field_path"]))


def _pack_field_requirements_by_family(
    requirements: List[Dict[str, Any]],
    *,
    budget_tokens: int,
) -> List[Dict[str, Any]]:
    budget = int(budget_tokens or DEFAULT_SCHEMA_CONTEXT_BUDGET)
    by_family: Dict[str, List[Dict[str, Any]]] = {}
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        family_id = _text(requirement.get("family_id")) or "global_execution"
        by_family.setdefault(family_id, []).append(dict(requirement))

    chunks: List[Dict[str, Any]] = []
    for family_id, family_requirements in sorted(by_family.items()):
        current: List[Dict[str, Any]] = []
        current_tokens = 0
        chunk_index = 0
        for requirement in family_requirements:
            item_tokens = estimate_context_tokens(requirement)
            if current and current_tokens + item_tokens > budget:
                chunks.append(_field_requirement_chunk(family_id, chunk_index, current, budget))
                chunk_index += 1
                current = []
                current_tokens = 0
            current.append(requirement)
            current_tokens += item_tokens
        if current:
            chunks.append(_field_requirement_chunk(family_id, chunk_index, current, budget))
    return chunks


def _field_requirement_chunk(
    family_id: str,
    chunk_index: int,
    requirements: List[Dict[str, Any]],
    budget: int,
) -> Dict[str, Any]:
    token_estimate = estimate_context_tokens(requirements)
    return {
        "chunk_id": f"schema_family::{family_id}::{chunk_index}",
        "family_id": family_id,
        "chunk_index": chunk_index,
        "requirement_count": len(requirements),
        "obligation_count": sum(int(item.get("obligation_count") or 0) for item in requirements),
        "field_paths": [item.get("field_path", "") for item in requirements if item.get("field_path")],
        "token_estimate": token_estimate,
        "within_budget": token_estimate <= budget,
        "reason_related_candidates_were_kept_together": (
            "Requirements stay grouped by execution family; oversized families are split at field-owner "
            "boundaries so one schema owner is not divided across prompt chunks."
        ),
    }


def _requirements_by_family_table(requirements: List[Dict[str, Any]]) -> Dict[str, List[List[Any]]]:
    table: Dict[str, List[List[Any]]] = {}
    for requirement in requirements:
        family_id = _text(requirement.get("family_id")) or "global_execution"
        table.setdefault(family_id, []).append([
            requirement.get("field_path", ""),
            requirement.get("field_type", ""),
            requirement.get("lifecycle", ""),
            requirement.get("obligation_count", 0),
            requirement.get("obligation_kinds", [])[:4],
        ])
    return table


def estimate_context_tokens(value: Any) -> int:

    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        raw = str(value)
    return max(1, (len(raw) + 3) // 4)


def _normalize_obligations(
    value: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
) -> List[Dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, Mapping):
        raw_obligations = value.get("obligations", [])
    else:
        raw_obligations = value
    result: List[Dict[str, Any]] = []
    for item in raw_obligations if isinstance(raw_obligations, IterableABC) else []:
        if isinstance(item, SchemaObligation):
            result.append(item.to_dict())
        elif isinstance(item, Mapping):
            result.append(SchemaObligation.from_mapping(item).to_dict())
    return result


def _build_task_universe(traces_context: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    trace_ids: List[str] = []
    instructions: List[Dict[str, str]] = []
    for trace in traces_context:
        if not isinstance(trace, Mapping):
            continue
        trace_id = _text(trace.get("trace_id")) or f"trace_{len(trace_ids)}"
        if trace_id not in trace_ids:
            trace_ids.append(trace_id)
        task = _text(trace.get("task") or trace.get("task_instruction") or trace.get("instruction"))
        if task:
            instructions.append({"trace_id": trace_id, "task_instruction": task[:600]})
    return {
        "trace_ids": trace_ids,
        "task_instructions": instructions,
        "task_families": _infer_task_families(instructions),
    }


def _build_textual_trace_context(
    traces_context: Iterable[Mapping[str, Any]],
    obligations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    evidence_steps = {
        _text(step)
        for obligation in obligations
        for step in obligation.get("evidence_steps", [])
        if _text(step)
    }
    trace_summaries: List[Dict[str, Any]] = []
    for trace in traces_context:
        if not isinstance(trace, Mapping):
            continue
        trace_id = _text(trace.get("trace_id")) or f"trace_{len(trace_summaries)}"
        steps = trace.get("steps", [])
        compact_steps: List[Dict[str, Any]] = []
        if isinstance(steps, list):
            for idx, step in enumerate(steps[:80]):
                if isinstance(step, Mapping):
                    step_idx = step.get("step", step.get("step_index", idx))
                    key = f"{trace_id}:step{step_idx}"
                    compact_steps.append({
                        "step": step_idx,
                        "action": _text(step.get("action") or step.get("action_description"))[:240],
                        "observation": _text(step.get("observation") or step.get("state") or step.get("screen"))[:240],
                        "evidence_hit": key in evidence_steps or _text(step.get("step_key")) in evidence_steps,
                    })
        trace_summaries.append({
            "trace_id": trace_id,
            "step_count": len(steps) if isinstance(steps, list) else 0,
            "compact_steps": compact_steps,
        })
    return {
        "trace_summaries": trace_summaries,
        "local_step_window_rule": "Keep input, selector-open, suggestion-select, submit, and checkpoint-adjacent steps.",
    }


def _build_catalog_candidate_context(schema_candidate_ir: Mapping[str, Any]) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for candidate in schema_candidate_ir.get("entity_candidates", []) if isinstance(schema_candidate_ir, Mapping) else []:
        if not isinstance(candidate, Mapping):
            continue
        candidates.append({
            "entity_name": _text(candidate.get("entity_name") or candidate.get("name")),
            "role": _text(candidate.get("role")),
            "task_related": bool(candidate.get("task_related")),
            "field_candidates": sorted((candidate.get("field_candidates") or {}).keys())[:16]
            if isinstance(candidate.get("field_candidates"), Mapping) else candidate.get("field_names", [])[:16]
            if isinstance(candidate.get("field_names"), list) else [],
            "page_clusters": (
                candidate.get("support", {}).get("page_clusters", [])[:8]
                if isinstance(candidate.get("support"), Mapping) else candidate.get("page_clusters", [])[:8]
                if isinstance(candidate.get("page_clusters"), list) else []
            ),
        })
    return {
        "entity_candidates": candidates,
        "candidate_count": len(candidates),
    }


def _build_execution_obligation_context(obligations: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    unresolved: List[Dict[str, Any]] = []
    for obligation in obligations:
        kind = _text(obligation.get("obligation_kind")) or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if not _text(obligation.get("field_path")):
            unresolved.append({
                "obligation_id": obligation.get("obligation_id", ""),
                "obligation_kind": kind,
                "family_id": obligation.get("family_id", ""),
                "reason": "missing_global_field_path",
            })
    return {
        "schema_obligations": obligations,
        "obligation_count": len(obligations),
        "obligation_kind_counts": by_kind,
        "unresolved_obligations": unresolved,
        "authority_rule": "Every state-bearing obligation must resolve to a stable global field owner or carry an explicit rejection reason.",
    }


def _build_family_groups(
    obligations: List[Dict[str, Any]],
    entity_candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for obligation in obligations:
        family_id = _text(obligation.get("family_id")) or "global_execution"
        group = groups.setdefault(family_id, {
            "family_id": family_id,
            "field_paths": [],
            "candidate_names": [],
            "obligation_ids": [],
        })
        _append_unique(group["field_paths"], obligation.get("field_path"))
        _append_unique(group["obligation_ids"], obligation.get("obligation_id"))
    for candidate in entity_candidates:
        role = _text(candidate.get("role"))
        family_id = "search_interaction" if role in {"catalog_data", "runtime_task_state"} else "global_execution"
        group = groups.setdefault(family_id, {
            "family_id": family_id,
            "field_paths": [],
            "candidate_names": [],
            "obligation_ids": [],
        })
        _append_unique(group["candidate_names"], candidate.get("entity_name"))
    return sorted(groups.values(), key=lambda item: item["family_id"])


def _compact_page_identity_context(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "page_clusters": value.get("page_clusters", [])[:16] if isinstance(value.get("page_clusters"), list) else [],
        "page_names": value.get("page_names", [])[:16] if isinstance(value.get("page_names"), list) else [],
        "page_identity_summary": _text(value.get("page_identity_summary"))[:1200],
        "route_neighbors": value.get("route_neighbors", [])[:16] if isinstance(value.get("route_neighbors"), list) else [],
        "policy": "Page identity locates projections; it is not the global schema boundary.",
    }


def _infer_task_families(instructions: List[Dict[str, str]]) -> List[str]:
    joined = " ".join(item.get("task_instruction", "") for item in instructions).lower()
    families: List[str] = []
    if any(token in joined for token in ("车票", "火车", "railway", "train", "车站", "出发", "到达")):
        families.append("search_interaction")
    if not families and instructions:
        families.append("global_execution")
    return families


def _text(value: Any) -> str:
    return str(value or "").strip()


def _append_unique(values: List[str], value: Any) -> None:
    text = _text(value)
    if text and text not in values:
        values.append(text)


def _append_unique_limited(values: List[str], value: Any, limit: int) -> None:
    if len(values) >= limit:
        return
    _append_unique(values, value)


def _extend_unique_limited(values: List[str], raw_values: Any, limit: int) -> None:
    if isinstance(raw_values, str):
        candidates = [raw_values]
    elif isinstance(raw_values, list):
        candidates = raw_values
    else:
        candidates = []
    for value in candidates:
        if len(values) >= limit:
            break
        _append_unique(values, value)


def _unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        _append_unique(result, value)
    return result
