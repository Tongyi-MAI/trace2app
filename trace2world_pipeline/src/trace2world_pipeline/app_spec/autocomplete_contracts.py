
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


_SCHEMA_TOKEN_EXACT: frozenset[str] = frozenset({
    "and", "or", "the", "a", "an", "of", "for", "to", "in", "on", "at", "by",
    "query", "draft", "projection", "results", "result", "poi",
    "entry", "search", "lookup", "options", "option", "entrance",
    "location", "destination", "business", "global", "type", "text",
    "state", "reducer", "lifecycle", "intent", "operation", "transition",
    "navigation", "commit", "submitted", "committed", "select", "selected",
    "filter", "sort", "tab", "overlay", "modal", "sheet", "drawer",
    "form", "input", "field", "value", "key", "ref", "owner",
    "target", "source", "action", "step", "page", "route", "url",
    "render", "project", "trigger", "effect", "mutation", "schema",
    "contract", "spec", "requirement", "candidate", "suggestion",
})

_SCHEMA_COMPOUND_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}$"
)

_SCHEMA_COMPOUND_PARTS: frozenset[str] = frozenset({
    "search", "entry", "lookup", "query", "draft", "projection",
    "results", "destination", "location", "business", "global",
    "state", "contract", "spec", "suggestion", "committed", "selected",
    "shell", "precommit", "postcommit",
})


def _is_schema_token(title: str) -> bool:
    lower = title.lower().strip()
    if not lower:
        return False
    if lower in _SCHEMA_TOKEN_EXACT:
        return True
    if _SCHEMA_COMPOUND_RE.match(lower):
        parts = lower.split("_")
        schema_part_count = sum(1 for p in parts if p in _SCHEMA_COMPOUND_PARTS)
        if schema_part_count >= max(1, len(parts) // 2):
            return True
    if re.match(r"^[a-z_]+::", lower):
        return True
    return False


def _extract_candidate_rows_from_suggestion_contract(
    suggestion_contract: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in _as_list(suggestion_contract.get("suggestions")):
        if isinstance(item, dict):
            title = _text(item.get("title") or item.get("text") or item.get("label"))
            if title:
                row_source = _text(item.get("source") or item.get("source_kind") or "suggestion_contract")
                rows.append({
                    "title": title,
                    "subtitle": _text(item.get("subtitle") or item.get("description")),
                    "category": _text(item.get("category")),
                    "query_value": _text(item.get("query_value")),
                    "select_behavior": _text(item.get("select_behavior")),
                    "submit_behavior": _text(item.get("submit_behavior")),
                    "source_kind": row_source,
                    "source_ref": _text(suggestion_contract.get("query_value") or suggestion_contract.get("query_state_owner")),
                })
        elif isinstance(item, str) and item.strip():
            rows.append({
                "title": item.strip(),
                "subtitle": "",
                "category": "",
                "query_value": "",
                "select_behavior": "",
                "submit_behavior": "",
                "source_kind": "suggestion_contract",
                "source_ref": "",
            })
    return rows


def _source_kinds(rows: List[Dict[str, Any]]) -> set[str]:
    return {_text(row.get("source_kind")) for row in rows if _text(row.get("source_kind"))}


def _is_identity_hint_candidate_set(rows: List[Dict[str, Any]]) -> bool:
    if not rows:
        return False
    kinds = _source_kinds(rows)
    return bool(kinds) and kinds <= {"target_identity_hints", "suggestion_contract.expected_titles"}


def _has_evidence_hydrated_suggestions(suggestion_contract: Mapping[str, Any], rows: List[Dict[str, Any]]) -> bool:
    if bool(suggestion_contract.get("_evidence_hydrated")):
        return True
    return any(_text(row.get("source_kind")) == "autocomplete_evidence_extraction" for row in rows)


def _extract_candidate_rows_from_render_projection(
    render_projection: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    suggestion_contract = _as_dict(render_projection.get("suggestion_contract"))
    rows.extend(_extract_candidate_rows_from_suggestion_contract(suggestion_contract))
    for item in _as_list(render_projection.get("projected_items")):
        if isinstance(item, dict):
            title = _text(item.get("title") or item.get("text") or item.get("name"))
            if title and not any(r["title"] == title for r in rows):
                rows.append({
                    "title": title,
                    "subtitle": _text(item.get("subtitle") or item.get("description")),
                    "category": _text(item.get("category")),
                    "query_value": _text(item.get("query_value")),
                    "select_behavior": _text(item.get("select_behavior")),
                    "submit_behavior": _text(item.get("submit_behavior")),
                    "source_kind": "render_projection",
                    "source_ref": _text(render_projection.get("trigger_state_ref") or render_projection.get("trigger")),
                })
    return rows


def _required_categories(value: Any) -> Dict[str, int]:
    if isinstance(value, dict):
        result: Dict[str, int] = {}
        for key, count in value.items():
            name = _text(key)
            if not name:
                continue
            try:
                result[name] = int(count or 0)
            except (TypeError, ValueError):
                result[name] = 1
        return result
    result: Dict[str, int] = {}
    for item in _as_list(value):
        if isinstance(item, dict):
            name = _text(item.get("category") or item.get("name") or item.get("label"))
            count = item.get("count", 1)
        else:
            name = _text(item)
            count = 1
        if not name:
            continue
        try:
            result[name] = int(count or 0)
        except (TypeError, ValueError):
            result[name] = 1
    return result


def _requirement_for_query(requirements: List[Dict[str, Any]], query_value: str) -> Dict[str, Any]:
    fallback: Dict[str, Any] = {}
    for item in requirements:
        if not isinstance(item, dict):
            continue
        req_query = _text(item.get("query_value") or item.get("query"))
        if req_query and req_query == query_value:
            return item
        if not req_query and not fallback:
            fallback = item
    return fallback


def _infer_select_behavior(
    transition: Mapping[str, Any],
    suggestion_contract: Mapping[str, Any],
) -> str:
    nav_effect = _as_dict(transition.get("navigation_effect"))
    if nav_effect.get("cross_page"):
        return "navigate"
    behavior = _text(suggestion_contract.get("select_behavior"))
    if behavior:
        return behavior
    reducer = _text(transition.get("reducer")).lower()
    if "commit" in reducer or "submit" in reducer:
        return "commit_query"
    return "fill_input"


def _infer_contract_kind(select_behavior: str, projection_lifecycle: str) -> str:
    if select_behavior == "navigate":
        return "submit_target"
    if select_behavior == "commit_query":
        return "submit_target"
    if projection_lifecycle == "after_commit":
        return "search_results"
    return "typeahead_suggestion"


def _transition_step_key(transition: Mapping[str, Any]) -> str:
    return _text(transition.get("step_key") or transition.get("action_id"))


def _step_keys_from_ref(value: Any) -> List[str]:
    text = _text(value)
    if not text:
        return []
    return re.findall(r"[A-Za-z0-9_-]+:step\d+", text)


def _transition_query_value(transition: Mapping[str, Any]) -> str:
    effect = _as_dict(transition.get("state_effect"))
    return _text(
        effect.get("value")
        or transition.get("input_text")
        or transition.get("query_value")
        or transition.get("value")
    )


def _transition_matches_query_entry(
    transition: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> bool:
    step_key = _transition_step_key(transition)
    entry_step_keys = {
        _text(item)
        for item in _as_list(entry.get("source_step_keys"))
        if _text(item)
    }
    entry_step_keys.update(_step_keys_from_ref(entry.get("query_state_owner")))
    if step_key and step_key in entry_step_keys:
        return True

    query_value = _text(entry.get("query_value"))
    if not query_value:
        return False
    transition_value = _transition_query_value(transition)
    if transition_value and transition_value == query_value:
        return True
    return query_value in _text(transition.get("operation_intent") or transition.get("command"))


def _transition_step_matches_query_entry(
    transition: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> bool:
    step_key = _transition_step_key(transition)
    if not step_key:
        return False
    entry_step_keys = {
        _text(item)
        for item in _as_list(entry.get("source_step_keys"))
        if _text(item)
    }
    entry_step_keys.update(_step_keys_from_ref(entry.get("query_state_owner")))
    return step_key in entry_step_keys


def _select_transition_for_query_entry(
    entry: Mapping[str, Any],
    suggestion_transitions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    for transition in suggestion_transitions:
        if _transition_step_matches_query_entry(transition, entry):
            return transition
    for transition in suggestion_transitions:
        if _transition_matches_query_entry(transition, entry):
            return transition
    return suggestion_transitions[0] if len(suggestion_transitions) == 1 else {}


def _render_projection_matches_query_entry(
    render_projection: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> bool:
    source_steps = {
        _text(item)
        for item in _as_list(render_projection.get("source_step_keys"))
        if _text(item)
    }
    source_steps.add(_text(render_projection.get("action_id")))
    source_steps.discard("")
    entry_steps = {
        _text(item)
        for item in _as_list(entry.get("source_step_keys"))
        if _text(item)
    }
    entry_steps.update(_step_keys_from_ref(entry.get("query_state_owner")))
    if source_steps and entry_steps:
        return bool(source_steps & entry_steps)

    suggestion_contract = _as_dict(render_projection.get("suggestion_contract"))
    projection_owner = _text(
        suggestion_contract.get("query_state_owner")
        or render_projection.get("trigger_state_ref")
        or render_projection.get("trigger")
    )
    entry_owner = _text(entry.get("query_state_owner"))
    if projection_owner and entry_owner:
        return projection_owner == entry_owner

    projection_query = _text(
        suggestion_contract.get("query_value")
        or render_projection.get("query_value")
        or render_projection.get("query")
    )
    entry_query = _text(entry.get("query_value"))
    return bool(projection_query and entry_query and projection_query == entry_query)


def compile_autocomplete_contracts(
    page_environment_specs: Mapping[str, Any],
    typed_action_contracts: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:

    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = _as_dict(payload.get("page_environment_specs_by_filename"))
    typed_contracts = _as_dict(
        typed_action_contracts.get("typed_action_contracts_by_action_id")
        if isinstance(typed_action_contracts, dict) else {}
    )

    contracts_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    issues: List[Dict[str, Any]] = []
    total_candidate_count = 0

    for page_filename, spec in specs_by_filename.items():
        if not isinstance(spec, dict):
            continue
        filename = _text(spec.get("filename") or page_filename)

        query_states = _as_list(spec.get("query_states"))
        render_projections = _as_list(spec.get("render_projections"))
        transitions = _as_list(spec.get("transitions"))
        requirements = [
            item for item in (
                _as_list(spec.get("autocomplete_requirements"))
                + _as_list(spec.get("trace_review_autocomplete_requirements"))
            )
            if isinstance(item, dict)
        ]

        suggestion_transitions = [
            t for t in transitions
            if isinstance(t, dict) and _text(t.get("reducer")).lower() in (
                "project_suggestions", "select_suggestion", "type_search",
                "commit_query", "submit_query",
            )
        ]

        query_entries: List[Dict[str, Any]] = []
        seen_query_keys = set()
        evidence_query_keys = set()
        evidence_query_values = set()
        for qs in query_states:
            if not isinstance(qs, dict):
                continue
            query_value = _text(qs.get("query_value") or qs.get("value"))
            state_ref = _text(qs.get("state_ref") or qs.get("owner"))
            suggestion_contract = _as_dict(qs.get("suggestion_contract"))
            candidate_rows = _extract_candidate_rows_from_suggestion_contract(suggestion_contract)
            if not suggestion_contract and not candidate_rows:
                continue
            seen_query_keys.add((query_value, state_ref))
            if _has_evidence_hydrated_suggestions(suggestion_contract, candidate_rows):
                evidence_query_keys.add((query_value, state_ref))
                if query_value:
                    evidence_query_values.add(query_value)
            query_entries.append({
                "query_value": query_value,
                "query_state_owner": state_ref,
                "source_step_keys": _step_keys_from_ref(state_ref),
                "suggestion_contract": suggestion_contract,
                "candidate_rows": candidate_rows,
                "source_entry_kind": "query_state",
            })
        for rp in render_projections:
            if not isinstance(rp, dict):
                continue
            suggestion_contract = _as_dict(rp.get("suggestion_contract"))
            candidate_rows = _extract_candidate_rows_from_render_projection(rp)
            if suggestion_contract.get("suppressed_identity_hint_autocomplete_projection") and not candidate_rows:
                continue
            if not suggestion_contract and not candidate_rows:
                continue
            query_value = _text(
                suggestion_contract.get("query_value")
                or rp.get("query_value")
                or rp.get("query")
            )
            state_ref = _text(
                suggestion_contract.get("query_state_owner")
                or rp.get("trigger_state_ref")
                or rp.get("trigger")
            )
            key = (query_value, state_ref)
            stale_identity_projection = _is_identity_hint_candidate_set(candidate_rows)
            if stale_identity_projection and (key in evidence_query_keys or query_value in evidence_query_values):
                issues.append({
                    "code": "autocomplete_stale_identity_hint_projection",
                    "severity": "blocking",
                    "page_filename": filename,
                    "action_id": _text(rp.get("action_id") or (_as_list(rp.get("source_step_keys")) or [""])[0]),
                    "message": (
                        f"autocomplete render projection for {filename} / {query_value} "
                        "contains target identity hints while evidence-backed suggestions exist"
                    ),
                })
                continue
            if key in evidence_query_keys or query_value in evidence_query_values:
                continue
            if key in seen_query_keys:
                continue
            seen_query_keys.add(key)
            query_entries.append({
                "query_value": query_value,
                "query_state_owner": state_ref,
                "source_step_keys": [
                    _text(item)
                    for item in _as_list(rp.get("source_step_keys"))
                    if _text(item)
                ] + _step_keys_from_ref(state_ref),
                "suggestion_contract": suggestion_contract,
                "candidate_rows": candidate_rows,
                "source_entry_kind": "render_projection",
            })

        for entry in query_entries:
            query_value = _text(entry.get("query_value"))
            suggestion_contract = _as_dict(entry.get("suggestion_contract"))
            requirement = _requirement_for_query(requirements, query_value)
            candidate_rows = list(_as_list(entry.get("candidate_rows")))

            for rp in render_projections:
                if not isinstance(rp, dict):
                    continue
                if not _render_projection_matches_query_entry(rp, entry):
                    continue
                trigger = _text(rp.get("trigger_state_ref") or rp.get("trigger"))
                if trigger and "query" in trigger.lower():
                    suggestion_contract = _as_dict(rp.get("suggestion_contract"))
                    if suggestion_contract.get("suppressed_identity_hint_autocomplete_projection"):
                        continue
                    extra = _extract_candidate_rows_from_render_projection(rp)
                    if _is_identity_hint_candidate_set(extra) and (
                        (query_value, _text(entry.get("query_state_owner"))) in evidence_query_keys
                        or query_value in evidence_query_values
                    ):
                        continue
                    for row in extra:
                        if not any(r["title"] == row["title"] for r in candidate_rows):
                            candidate_rows.append(row)

            pre_filter_count = len(candidate_rows)
            filtered_schema_tokens = [r for r in candidate_rows if _is_schema_token(r.get("title", ""))]
            candidate_rows = [r for r in candidate_rows if not _is_schema_token(r.get("title", ""))]
            filtered_count = len(filtered_schema_tokens)

            categories: Dict[str, int] = defaultdict(int)
            for row in candidate_rows:
                cat = row.get("category", "")
                if cat:
                    categories[cat] += 1

            input_action_id = ""
            select_behavior = "fill_input"
            transition = _select_transition_for_query_entry(entry, suggestion_transitions)
            if transition:
                input_action_id = _transition_step_key(transition)
                select_behavior = _infer_select_behavior(transition, suggestion_contract)

            raw_expected_count = int(
                requirement.get("expected_candidate_count")
                or suggestion_contract.get("expected_candidate_count", 0)
                or 0
            )
            expected_count = raw_expected_count
            expected_adjustment_reason = ""
            if expected_count == 0:
                expected_count = len(candidate_rows)
                expected_adjustment_reason = "default_to_actual_rows"
            if filtered_count > 0 and expected_count > len(candidate_rows):
                expected_count = len(candidate_rows)
                expected_adjustment_reason = f"schema_token_filter_reduced_{filtered_count}_tokens"

            min_visible = int(
                requirement.get("min_visible_count")
                or suggestion_contract.get("min_visible_count", 0)
                or 0
            )
            if min_visible == 0:
                min_visible = min(expected_count, len(candidate_rows)) if candidate_rows else 0
            min_visible_count_satisfied = len(candidate_rows) >= min_visible
            required_categories = _required_categories(
                requirement.get("required_candidate_categories")
                or suggestion_contract.get("required_candidate_categories")
                or suggestion_contract.get("candidate_category_requirements")
                or suggestion_contract.get("expected_candidate_categories")
            )

            projection_lifecycle = "after_input"
            contract_kind = _infer_contract_kind(select_behavior, projection_lifecycle)
            contract: Dict[str, Any] = {
                "page_id": filename,
                "input_action_id": input_action_id,
                "contract_kind": contract_kind,
                "query_value": query_value,
                "query_state_owner": _text(entry.get("query_state_owner")),
                "candidate_rows": candidate_rows,
                "raw_expected_candidate_count": raw_expected_count,
                "expected_candidate_count": expected_count,
                "min_visible_count": min_visible,
                "min_visible_count_satisfied": min_visible_count_satisfied,
                "candidate_categories": dict(categories) if categories else {},
                "required_candidate_categories": required_categories,
                "projection_lifecycle": projection_lifecycle,
                "select_behavior": select_behavior,
                "source_evidence_refs": [input_action_id] if input_action_id else [],
            }
            if filtered_count > 0:
                contract["filtered_schema_token_count"] = filtered_count
            if expected_adjustment_reason:
                contract["expected_adjustment_reason"] = expected_adjustment_reason
            contracts_by_page[filename].append(contract)
            total_candidate_count += len(candidate_rows)

            if filtered_count > 0 and not candidate_rows:
                issues.append({
                    "code": "autocomplete_candidate_evidence_missing",
                    "severity": "blocking",
                    "page_filename": filename,
                    "action_id": input_action_id,
                    "message": (
                        f"autocomplete contract for {filename} had {pre_filter_count} candidates "
                        f"but all {filtered_count} were schema/intent tokens — "
                        f"no real evidence available"
                    ),
                })
            elif not candidate_rows:

                extraction_audit = _as_dict(suggestion_contract.get("extraction_audit"))
                audit_status = _text(extraction_audit.get("status"))
                evidence_hydrated = bool(suggestion_contract.get("_evidence_hydrated"))
                has_select_obligation = _text(select_behavior) in ("select_candidate", "commit_query", "select_suggestion")

                if audit_status and audit_status != "success":
                    issue_code = "autocomplete_candidate_extraction_failed"
                    issue_msg = (
                        f"autocomplete contract for {filename} extraction failed "
                        f"(status={audit_status}, reason={_text(extraction_audit.get('empty_reason'))})"
                    )
                elif evidence_hydrated or has_select_obligation:
                    issue_code = "autocomplete_candidate_rows_missing"
                    issue_msg = f"autocomplete contract for {filename} has no candidate rows"
                else:

                    issue_code = "autocomplete_candidate_rows_missing"
                    issue_msg = f"autocomplete contract for {filename} has no candidate rows (query draft, no select obligation)"

                severity = "blocking" if (evidence_hydrated or has_select_obligation) else "warning"
                issues.append({
                    "code": issue_code,
                    "severity": severity,
                    "page_filename": filename,
                    "action_id": input_action_id,
                    "message": issue_msg,
                })
            elif len(candidate_rows) < expected_count:
                issues.append({
                    "code": "autocomplete_candidate_count_mismatch",
                    "severity": "blocking",
                    "page_filename": filename,
                    "action_id": input_action_id,
                    "message": (
                        f"autocomplete contract for {filename} has {len(candidate_rows)} rows "
                        f"but expected {expected_count}"
                    ),
                })
            if candidate_rows and not min_visible_count_satisfied:
                issues.append({
                    "code": "autocomplete_materialization_incomplete",
                    "severity": "blocking",
                    "page_filename": filename,
                    "action_id": input_action_id,
                    "message": (
                        f"autocomplete contract for {filename} has {len(candidate_rows)} visible rows "
                        f"but min_visible_count is {min_visible}"
                    ),
                })
            for category, required_count in required_categories.items():
                actual_count = int(categories.get(category, 0) or 0)
                if actual_count < required_count:
                    issues.append({
                        "code": "autocomplete_category_coverage_missing",
                        "severity": "blocking",
                        "page_filename": filename,
                        "action_id": input_action_id,
                        "message": (
                            f"autocomplete category '{category}' has {actual_count} rows "
                            f"but expected {required_count}"
                        ),
                    })

        has_query_suggestion_actions = any(
            isinstance(t, dict) and _text(t.get("reducer")).lower() in (
                "project_suggestions", "select_suggestion",
            )
            for t in transitions
        )
        if has_query_suggestion_actions and not contracts_by_page.get(filename):
            issues.append({
                "code": "autocomplete_contract_missing",
                "severity": "blocking",
                "page_filename": filename,
                "action_id": "",
                "message": f"page {filename} has query suggestion actions but no autocomplete contract",
            })

    return {
        "format": "autocomplete_contracts",
        "schema_revision": 2,
        "autocomplete_contracts_by_page_id": dict(contracts_by_page),
        "issues": issues,
        "summary": {
            "autocomplete_page_count": len(contracts_by_page),
            "autocomplete_candidate_count": total_candidate_count,
            "issue_count": len(issues),
        },
    }
