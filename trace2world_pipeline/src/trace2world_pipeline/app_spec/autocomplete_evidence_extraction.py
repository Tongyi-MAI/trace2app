
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_step_key(key: str) -> str:
    text = str(key or "").strip()
    match = re.match(r"^(.+?):(?:step)?(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{match.group(2)}"
    return text


def _step_index_from_key(step_key: str) -> int:
    match = re.search(r":step(\d+)$", step_key)
    return int(match.group(1)) if match else -1


def _trace_id_from_step_key(step_key: str) -> str:
    parts = step_key.rsplit(":step", 1)
    return parts[0] if len(parts) == 2 else step_key.rsplit(":", 1)[0]


def _quoted_query_from_transition(transition: Mapping[str, Any]) -> str:
    intent = _text(transition.get("operation_intent"))
    match = re.search(r'["“「](.+?)["”」]', intent)
    return match.group(1).strip() if match else ""


def _is_query_input_transition(transition: Dict[str, Any]) -> bool:
    reducer = _text(transition.get("reducer")).lower()
    return reducer in (
        "type_search", "update_query_draft", "project_suggestions",
    )


def _is_autocomplete_lifecycle_entry(entry: Mapping[str, Any]) -> bool:
    lifecycle = _text(entry.get("lifecycle")).lower()
    if lifecycle:
        return lifecycle == "query_autocomplete_project"
    reducer = _text(entry.get("reducer")).lower()
    if reducer:
        return reducer in ("type_search", "update_query_draft", "project_suggestions")
    evidence_kind = _text(entry.get("evidence_kind")).lower()
    if evidence_kind:
        return evidence_kind == "post_input_suggestion_visible"
    return True


def _source_steps_for_query_state(qs: Mapping[str, Any]) -> set[str]:
    return {
        _normalize_step_key(_text(step))
        for step in _as_list(qs.get("source_step_keys"))
        if _text(step)
    }


def _rows_from_frame_observation(frame: Mapping[str, Any], query_value: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            title = _text(value.get("title") or value.get("name") or value.get("text") or value.get("label"))
            if title and title != query_value and title not in seen:
                rows.append({
                    "title": title,
                    "subtitle": _text(value.get("subtitle") or value.get("address") or value.get("description")),
                    "row_order": len(rows) + 1,
                    "layout_role": "suggestion_row",
                    "extraction_source": "frame_observation_extracted_items",
                })
                seen.add(title)
            for child in value.values():
                if isinstance(child, (list, dict)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(frame.get("extracted_items"))
    visit(frame.get("components"))
    return rows


def _clear_prior_evidence_hydration(spec: Dict[str, Any]) -> None:
    query_states = spec.get("query_states")
    if not isinstance(query_states, list):
        return
    for qs in query_states:
        if not isinstance(qs, dict):
            continue
        sc = qs.get("suggestion_contract")
        if not isinstance(sc, dict):
            continue
        suggestions = _as_list(sc.get("suggestions"))
        if not suggestions:
            continue
        kept = [
            row for row in suggestions
            if not (
                isinstance(row, dict)
                and _text(row.get("source")) == "autocomplete_evidence_extraction"
            )
        ]
        if len(kept) == len(suggestions):
            continue
        if kept:
            sc["suggestions"] = kept
            sc["expected_candidate_count"] = len(kept)
        else:
            sc.pop("suggestions", None)
            sc.pop("expected_candidate_count", None)
            sc.pop("_evidence_hydrated", None)


_EXTRACTION_PROMPT = """\
You are extracting visible query-candidate rows from a mobile app screenshot taken after the user entered a search query.

Use a semantic definition, not a widget-name definition:
A candidate row is any visible option or result produced by the query that the user could select, inspect, or expand next. It may appear as an autocomplete dropdown, typeahead overlay, grouped search result list, filtered result list, recommendation list, or expandable more-results row. Do not require the UI to use the word autocomplete.

Query typed by user: "{query_value}"

Extraction rules:
- Extract visible row-level options/results related to the query.
- In grouped layouts, standalone group labels or category badges are metadata, not rows. Attach the visible group/category text to the adjacent or following row when visually associated.
- If a visible row is an expandable control that reveals more query results, include it with row_kind="expand_more".
- Exclude app chrome, the search field text itself, icons, clear/cancel buttons, keyboard controls, status bar text, and standalone labels that are not row options.
- Do not invent or complete hidden rows.

Return strict JSON only:
{{"visible_rows": [{{"title": "main visible row text", "subtitle": "secondary visible text if any, else empty string", "category": "group/category badge text if visually associated, else empty string", "row_order": 1, "row_kind": "selectable_candidate|expand_more|other_candidate", "evidence_reason": "short visual reason"}}], "total_visible_count": 0, "empty_reason": "only set when no candidate rows are visible"}}"""


def _collect_extraction_tasks(
    page_environment_specs: Mapping[str, Any],
    frame_observations_dir: Path,
    *,
    focus_trace_ids: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = _as_dict(payload.get("page_environment_specs_by_filename"))
    tasks: List[Dict[str, Any]] = []

    for page_filename, spec in specs_by_filename.items():
        if not isinstance(spec, dict):
            continue
        filename = _text(spec.get("filename") or page_filename)
        transitions = _as_list(spec.get("transitions"))
        query_states = _as_list(spec.get("query_states"))

        query_values_by_step: Dict[str, str] = {}
        for qs in query_states:
            if isinstance(qs, dict):
                qv = _text(qs.get("query_value") or qs.get("value"))
                if qv:
                    for sk in _as_list(qs.get("source_step_keys")):
                        query_values_by_step[_normalize_step_key(_text(sk))] = qv
        for qp in _as_list(spec.get("query_projections")):
            if isinstance(qp, dict):
                qv = _text(qp.get("query_value"))
                if qv:
                    for sk in _as_list(qp.get("source_step_keys")):
                        query_values_by_step[_normalize_step_key(_text(sk))] = qv
        global_query_value = ""
        for qs in query_states:
            if isinstance(qs, dict):
                qv = _text(qs.get("query_value") or qs.get("value"))
                if qv:
                    global_query_value = qv
                    break
        if not global_query_value:
            for qp in _as_list(spec.get("query_projections")):
                if isinstance(qp, dict):
                    qv = _text(qp.get("query_value"))
                    if qv:
                        global_query_value = qv
                        break

        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            if not _is_query_input_transition(transition):
                continue
            step_key = _normalize_step_key(_text(transition.get("step_key")))
            if not step_key:
                continue
            trace_id = _trace_id_from_step_key(step_key)
            if focus_trace_ids and trace_id not in focus_trace_ids:
                continue
            step_index = _step_index_from_key(step_key)

            post_step_index = step_index + 1
            post_frame_path = frame_observations_dir / f"{trace_id}_step{post_step_index}.json"
            screenshot_path = ""
            if post_frame_path.is_file():
                try:
                    frame = json.load(open(post_frame_path, encoding="utf-8"))
                    screenshot_path = _text(frame.get("screenshot_path"))
                except Exception:
                    pass

            if not screenshot_path:
                current_frame_path = frame_observations_dir / f"{trace_id}_step{step_index}.json"
                if current_frame_path.is_file():
                    try:
                        frame = json.load(open(current_frame_path, encoding="utf-8"))
                        screenshot_path = _text(frame.get("screenshot_path"))
                    except Exception:
                        pass

            is_interacted = any(
                isinstance(t, dict) and _text(t.get("reducer")).lower() in ("select_suggestion", "commit_query")
                and _trace_id_from_step_key(_normalize_step_key(_text(t.get("step_key")))) == trace_id
                for t in transitions
            )

            step_query = query_values_by_step.get(step_key, "")
            if not step_query:
                step_query = _quoted_query_from_transition(transition)
            if not step_query:
                step_query = global_query_value

            tasks.append({
                "page_id": filename,
                "query_value": step_query,
                "evidence_step": step_key,
                "post_input_step": f"{trace_id}:step{post_step_index}",
                "screenshot_path": screenshot_path,
                "interaction_required": is_interacted,
                "reducer": _text(transition.get("reducer")),
                "lifecycle": "query_autocomplete_project",
                "trace_id": trace_id,
            })

    return tasks


async def _extract_rows_from_screenshot(
    screenshot_path: str,
    query_value: str,
    *,
    model: Optional[str] = None,
    max_tokens: int = 2000,
    reasoning_effort: str = "low",
) -> Dict[str, Any]:
    empty_result = {
        "rows": [],
        "status": "api_failed",
        "empty_reason": "screenshot_path_missing_or_not_found",
        "raw_visible_count": 0,
        "raw_reply_preview": "",
    }
    if not screenshot_path or not os.path.exists(screenshot_path):
        return empty_result

    from trace2world_pipeline.core.llm_caller import encode_image, call_openai_api_async

    image_data = encode_image(screenshot_path)
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": _EXTRACTION_PROMPT.format(query_value=query_value)},
            {"type": "image_url", "image_url": {"url": image_data}},
        ]},
    ]
    try:
        reply, _ = await call_openai_api_async(
            messages,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            reasoning_effort=reasoning_effort,
            model=model,
            stage="autocomplete_evidence_extraction",
        )
    except Exception:
        return {**empty_result, "status": "api_failed", "empty_reason": "api_exception"}

    if not reply:
        return {**empty_result, "status": "empty_reply", "empty_reason": "llm_returned_empty_reply"}

    raw_preview = reply[:500]
    try:
        data = json.loads(reply)
    except json.JSONDecodeError:
        return {**empty_result, "status": "json_parse_failed", "empty_reason": "llm_reply_not_valid_json", "raw_reply_preview": raw_preview}

    rows = _as_list(data.get("visible_rows"))
    if not rows:
        model_reason = _text(data.get("empty_reason"))
        return {
            "rows": [],
            "status": "model_returned_empty",
            "empty_reason": model_reason or "model_returned_no_visible_rows",
            "raw_visible_count": 0,
            "raw_reply_preview": raw_preview,
        }

    result = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        if not title or title == query_value:
            continue
        result.append({
            "title": title,
            "subtitle": _text(item.get("subtitle")),
            "category": _text(item.get("category")),
            "row_order": int(item.get("row_order", len(result) + 1)),
            "row_kind": _text(item.get("row_kind")) or "selectable_candidate",
            "evidence_reason": _text(item.get("evidence_reason")),
            "layout_role": "suggestion_row",
            "extraction_source": "llm_screenshot_extraction",
        })

    return {
        "rows": result,
        "status": "success" if result else "model_returned_empty",
        "empty_reason": "" if result else "all_rows_filtered_after_extraction",
        "raw_visible_count": len(result),
        "raw_reply_preview": raw_preview,
    }


async def extract_autocomplete_evidence_async(
    page_environment_specs: Mapping[str, Any],
    frame_observations_dir: Path,
    *,
    model: Optional[str] = None,
    max_concurrent: int = 8,
    focus_trace_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    tasks = _collect_extraction_tasks(
        page_environment_specs, frame_observations_dir,
        focus_trace_ids=focus_trace_ids,
    )

    seen: set[Tuple[str, str]] = set()
    unique_tasks: List[Dict[str, Any]] = []
    for task in tasks:
        key = (task["page_id"], task["screenshot_path"])
        if key in seen or not task["screenshot_path"]:
            continue
        seen.add(key)
        unique_tasks.append(task)

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _process(task: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            extraction = await _extract_rows_from_screenshot(
                task["screenshot_path"],
                task["query_value"],
                model=model,
            )
            rows = extraction.get("rows", [])
            status = extraction.get("status", "api_failed")
            empty_reason = extraction.get("empty_reason", "")

            blocked_reason = None
            if not rows:
                if status == "success":
                    blocked_reason = "no_candidate_rows_visible_in_screenshot"
                elif status in ("api_failed", "json_parse_failed", "empty_reply"):
                    blocked_reason = f"extraction_producer_failed:{status}"
                elif status == "model_returned_empty":
                    if task.get("interaction_required"):
                        blocked_reason = f"autocomplete_candidate_extraction_failed:{empty_reason}"
                    else:
                        blocked_reason = f"model_returned_empty:{empty_reason}"
                else:
                    blocked_reason = f"no_suggestion_rows_found:{status}"

            return {
                **task,
                "visible_rows": rows,
                "raw_visible_count": extraction.get("raw_visible_count", len(rows)),
                "extraction_confidence": "observed" if rows else "no_evidence",
                "blocked_reason": blocked_reason,
                "extraction_audit": {
                    "status": status,
                    "empty_reason": empty_reason,
                    "raw_reply_preview": extraction.get("raw_reply_preview", ""),
                },
                "evidence_kind": "post_input_suggestion_visible",
            }

    results = await asyncio.gather(*[_process(t) for t in unique_tasks])

    evidence_by_page: Dict[str, List[Dict[str, Any]]] = {}
    total_rows = 0
    for entry in results:
        page_id = entry.pop("screenshot_path", "")
        entry.pop("trace_id", "")
        entry.pop("reducer", "")
        evidence_by_page.setdefault(entry["page_id"], []).append(entry)
        total_rows += entry["raw_visible_count"]

    pages_with_evidence = sum(
        1 for entries in evidence_by_page.values()
        if any(e["raw_visible_count"] > 0 for e in entries)
    )

    return {
        "format": "autocomplete_evidence_by_page",
        "schema_revision": 1,
        "evidence_by_page": evidence_by_page,
        "summary": {
            "total_pages_with_query_input": len(evidence_by_page),
            "pages_with_evidence": pages_with_evidence,
            "total_visible_rows": total_rows,
            "total_extraction_tasks": len(unique_tasks),
        },
    }


def extract_autocomplete_evidence(
    page_environment_specs: Mapping[str, Any],
    frame_observations_dir: Path,
    *,
    focus_trace_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    tasks = _collect_extraction_tasks(
        page_environment_specs,
        frame_observations_dir,
        focus_trace_ids=focus_trace_ids,
    )
    evidence_by_page: Dict[str, List[Dict[str, Any]]] = {}
    total_rows = 0
    for task in tasks:
        post_step = _text(task.get("post_input_step"))
        trace_id = _trace_id_from_step_key(post_step)
        step_index = _step_index_from_key(post_step)
        frame_path = frame_observations_dir / f"{trace_id}_step{step_index}.json"
        frame: Dict[str, Any] = {}
        if frame_path.is_file():
            try:
                frame = json.load(open(frame_path, encoding="utf-8"))
            except Exception:
                frame = {}
        rows = _rows_from_frame_observation(frame, _text(task.get("query_value")))
        entry = {
            **task,
            "visible_rows": rows,
            "raw_visible_count": len(rows),
            "extraction_confidence": "observed" if rows else "no_evidence",
            "blocked_reason": None if rows else "no_suggestion_rows_found_in_frame_observation",
            "evidence_kind": "post_input_suggestion_visible",
        }
        entry.pop("screenshot_path", None)
        entry.pop("trace_id", None)
        evidence_by_page.setdefault(entry["page_id"], []).append(entry)
        total_rows += len(rows)

    pages_with_evidence = sum(
        1 for entries in evidence_by_page.values()
        if any(e["raw_visible_count"] > 0 for e in entries)
    )
    return {
        "format": "autocomplete_evidence_by_page",
        "schema_revision": 1,
        "evidence_by_page": evidence_by_page,
        "summary": {
            "total_pages_with_query_input": len(evidence_by_page),
            "pages_with_evidence": pages_with_evidence,
            "total_visible_rows": total_rows,
            "total_extraction_tasks": len(tasks),
        },
    }


def hydrate_pes_from_autocomplete_evidence(
    page_environment_specs: Dict[str, Any],
    autocomplete_evidence: Mapping[str, Any],
) -> Dict[str, Any]:
    specs_by_filename = _as_dict(page_environment_specs.get("page_environment_specs_by_filename"))
    evidence_by_page = _as_dict(autocomplete_evidence.get("evidence_by_page"))
    successful_entries_by_step_query: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entries in evidence_by_page.values():
        for entry in _as_list(entries):
            if not isinstance(entry, dict):
                continue
            if not _is_autocomplete_lifecycle_entry(entry):
                continue
            rows = _as_list(entry.get("visible_rows"))
            if not rows:
                continue
            evidence_step = _normalize_step_key(_text(entry.get("evidence_step")))
            query_value = _text(entry.get("query_value"))
            if evidence_step and query_value:
                successful_entries_by_step_query.setdefault((evidence_step, query_value), entry)
    hydrated_count = 0

    for page_filename, entries in evidence_by_page.items():
        spec = specs_by_filename.get(page_filename)
        if not isinstance(spec, dict):
            continue
        _clear_prior_evidence_hydration(spec)
        transitions_by_step = {
            _normalize_step_key(_text(t.get("step_key") or t.get("transition_id"))): t
            for t in _as_list(spec.get("transitions"))
            if isinstance(t, dict)
        }

        for entry in _as_list(entries):
            if not isinstance(entry, dict):
                continue
            rows = _as_list(entry.get("visible_rows"))
            extraction_audit = entry.get("extraction_audit")
            evidence_step = _normalize_step_key(_text(entry.get("evidence_step")))
            transition = _as_dict(transitions_by_step.get(evidence_step))
            enriched_entry = dict(entry)
            reducer = _text(enriched_entry.get("reducer")) or _text(transition.get("reducer"))
            if reducer:
                enriched_entry["reducer"] = reducer
            if not enriched_entry.get("lifecycle") and reducer in ("type_search", "update_query_draft", "project_suggestions"):
                enriched_entry["lifecycle"] = "query_autocomplete_project"
            if not _is_autocomplete_lifecycle_entry(enriched_entry):
                continue
            query_value = _quoted_query_from_transition(transition) or _text(entry.get("query_value"))
            successful_entry = successful_entries_by_step_query.get((evidence_step, query_value))
            if not rows and isinstance(successful_entry, dict):
                rows = _as_list(successful_entry.get("visible_rows"))
                enriched_entry["visible_rows"] = rows


            if not rows and isinstance(extraction_audit, dict):
                query_states = spec.get("query_states")
                if isinstance(query_states, list):
                    for qs in query_states:
                        if not isinstance(qs, dict):
                            continue
                        qs_value = _text(qs.get("query_value") or qs.get("value"))
                        if qs_value and qs_value != query_value:
                            continue
                        source_steps = _source_steps_for_query_state(qs)
                        if source_steps and evidence_step and evidence_step not in source_steps:
                            continue
                        sc = qs.setdefault("suggestion_contract", {})
                        if not isinstance(sc, dict):
                            sc = {}
                            qs["suggestion_contract"] = sc
                        sc["extraction_audit"] = extraction_audit
                        sc.setdefault("query_value", query_value)
                        sc.setdefault("source_step_keys", [evidence_step] if evidence_step else [])
                        hydrated_count += 1
                        break
                continue
            if not rows:
                continue
            hydrated_this_entry = False
            query_states = spec.get("query_states")
            if isinstance(query_states, list):
                for qs in query_states:
                    if not isinstance(qs, dict):
                        continue
                    qs_value = _text(qs.get("query_value") or qs.get("value"))
                    if qs_value and qs_value != query_value:
                        continue
                    source_steps = _source_steps_for_query_state(qs)
                    if source_steps and evidence_step and evidence_step not in source_steps:
                        continue
                    sc = qs.setdefault("suggestion_contract", {})
                    if not isinstance(sc, dict):
                        sc = {}
                        qs["suggestion_contract"] = sc
                    existing = _as_list(sc.get("suggestions"))
                    existing_titles = {_text(s.get("title")) for s in existing if isinstance(s, dict)}
                    added = 0
                    for row in rows:
                        title = _text(row.get("title"))
                        if title and title not in existing_titles:
                            existing.append({"title": title, "subtitle": row.get("subtitle", ""), "source": "autocomplete_evidence_extraction"})
                            existing_titles.add(title)
                            added += 1
                    if added > 0:
                        sc["suggestions"] = existing
                        sc["expected_candidate_count"] = len(existing)
                        sc.setdefault("query_value", query_value)
                        sc.setdefault("source_step_keys", [evidence_step] if evidence_step else [])
                        sc.setdefault("lifecycle", "query_autocomplete_project")
                        sc["_evidence_hydrated"] = True
                        hydrated_count += 1
                        hydrated_this_entry = True
                    break

            if not hydrated_this_entry:
                query_states_list = spec.setdefault("query_states", [])
                if not isinstance(query_states_list, list):
                    query_states_list = []
                    spec["query_states"] = query_states_list
                suggestions = [{"title": _text(r.get("title")), "subtitle": _text(r.get("subtitle", "")), "source": "autocomplete_evidence_extraction"} for r in rows if _text(r.get("title"))]
                if suggestions:
                    query_states_list.append({
                        "state_ref": f"query::{page_filename.replace('.html', '')}::{evidence_step or query_value}",
                        "query_value": query_value,
                        "source_step_keys": [evidence_step] if evidence_step else [],
                        "suggestion_contract": {
                            "suggestions": suggestions,
                            "expected_candidate_count": len(suggestions),
                            "query_value": query_value,
                            "source_step_keys": [evidence_step] if evidence_step else [],
                            "lifecycle": "query_autocomplete_project",
                            "_evidence_hydrated": True,
                        },
                    })
                    hydrated_count += 1

    return {
        "hydrated_count": hydrated_count,
        "page_count": len(evidence_by_page),
    }
