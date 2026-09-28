
from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Sequence, Tuple

from trace2world_pipeline.core.llm_caller import call_openai_api_json_async
from trace2world_pipeline.app_spec.evidence_resolver import step_key_from_screenshot_ref
from trace2world_pipeline.app_spec.provenance import Provenance


LLMJsonFunc = Callable[..., Awaitable[Tuple[Any, Any]]]

BUTTON_LABEL_FIELDS = (
    "name",
    "title",
    "label",
    "text",
    "display_name",
    "displayText",
    "display_text",
    "channelName",
    "channel_name",
    "serviceName",
    "service_name",
    "categoryName",
    "category_name",
    "tabName",
    "tab_name",
)

BUTTON_ENTITY_MARKERS = (
    "shortcut",
    "channel",
    "category",
    "navigation",
    "nav",
    "tab",
    "menu",
    "entry",
    "service",
    "suggestion",
    "search",
)

FIRST_LEVEL_ROUTE_ENTITY_MARKERS = (
    "servicechannel",
    "channelservice",
    "channelentry",
    "categorychannel",
    "categoryservice",
)

SURFACE_HINTS = (
    ("search", "search_surface"),
    ("suggestion", "search_suggestion"),
    ("bottom", "bottom_navigation"),
    ("footer", "bottom_navigation"),
    ("nav", "navigation"),
    ("tab", "tab_bar"),
    ("category", "category_grid"),
    ("channel", "category_grid"),
    ("shortcut", "category_grid"),
)


def _dedupe(values: Iterable[str], limit: int = 64) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _base_filename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    return text.rsplit("/", 1)[-1]


def _normalize_label(text: Any) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"[\s“”\"'（）()\[\]【】,，。:：;；、/\\|_\-·]+", "", value)
    for suffix in ("按钮", "入口", "链接", "页签", "标签", "图标", "tab"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value


def _score_label_match(query: str, candidate: str) -> float:
    q = _normalize_label(query)
    c = _normalize_label(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.92
    q_tokens = {tok for tok in re.split(r"[\s:：,，!！·\-_./()\[\]\"'…]+", str(query or "").lower()) if tok}
    c_tokens = {tok for tok in re.split(r"[\s:：,，!！·\-_./()\[\]\"'…]+", str(candidate or "").lower()) if tok}
    if not q_tokens or not c_tokens:
        return 0.0
    overlap = len(q_tokens & c_tokens)
    return overlap / max(len(c_tokens), 1)


def _page_aliases(page: Any) -> List[str]:
    aliases: List[str] = []
    for field in ("page_name", "page_name_full", "filename"):
        value = str(getattr(page, field, "") or "").strip()
        if value:
            aliases.append(value)
    structure_data = getattr(page, "structure_data", {}) if isinstance(getattr(page, "structure_data", {}), dict) else {}
    for field in ("route_path", "general_page_id", "materialized_page_key"):
        value = str(structure_data.get(field, "") or "").strip()
        if value:
            aliases.append(value)
    return _dedupe(aliases, limit=12)


def _entity_looks_button_like(entity_name: str) -> bool:
    lowered = str(entity_name or "").lower()
    return any(marker in lowered for marker in BUTTON_ENTITY_MARKERS)


def _entity_looks_first_level_route_entry(entity_name: str) -> bool:
    lowered = str(entity_name or "").lower()
    return any(marker in lowered for marker in FIRST_LEVEL_ROUTE_ENTITY_MARKERS)


def _route_path_depth(route_path: str) -> int:
    return len([part for part in str(route_path or "").split("/") if part])


def _page_route_path(page: Any) -> str:
    structure_data = getattr(page, "structure_data", {}) if isinstance(getattr(page, "structure_data", {}), dict) else {}
    return str(structure_data.get("route_path", "") or "").strip()


def _is_first_level_route_source_page(page: Any) -> bool:
    page_type = str(getattr(page, "page_type", "") or "").strip().lower()
    route_path = _page_route_path(page)
    if page_type != "homepage":
        return False
    return not route_path or route_path in {"/", "/home"} or _route_path_depth(route_path) <= 1


def _is_first_level_route_target_page(page: Any) -> bool:
    page_type = str(getattr(page, "page_type", "") or "").strip().lower()
    if page_type not in {"homepage", "listing", "category", "category_listing"}:
        return False
    route_path = _page_route_path(page)
    return not route_path or _route_path_depth(route_path) <= 3


def _surface_for_item(entity_name: str, item: Dict[str, Any]) -> str:
    text = " ".join(
        str(value or "")
        for value in (
            entity_name,
            item.get("section_id"),
            item.get("section"),
            item.get("section_kind"),
            item.get("component"),
            item.get("component_kind"),
            item.get("component_role"),
            item.get("surface"),
        )
    ).lower()
    for marker, surface in SURFACE_HINTS:
        if marker in text:
            return surface
    return "visible_button"


def _component_ref_for_entity(content_data: Dict[str, Any], entity_name: str) -> str:
    mapping = content_data.get("entity_component_mapping", []) if isinstance(content_data, dict) else []
    for item in mapping if isinstance(mapping, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("entity") or "").strip() == entity_name:
            return str(item.get("source_component") or item.get("component_ref") or "").strip()
    return ""


def _page_mid(page: Any) -> str:
    structure_data = getattr(page, "structure_data", {}) if isinstance(getattr(page, "structure_data", {}), dict) else {}
    for value in (
        structure_data.get("materialized_page_id"),
        structure_data.get("materialized_page_key"),
        structure_data.get("general_page_id"),
        getattr(page, "page_id", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _canonical_provenance(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        return Provenance.from_mapping(value).to_dict()
    except ValueError:
        return {}


def _fallback_provenance_from_screenshot_refs(page: Any) -> Dict[str, Any]:
    mid = _page_mid(page)
    if not mid:
        return {}
    for ref in list(getattr(page, "screenshot_refs", []) or []):
        step_key = step_key_from_screenshot_ref(str(ref or ""))
        if step_key:
            return Provenance(mid=mid, source_step=step_key).to_dict()
    return {}


def collect_visible_route_controls(page: Any, *, limit: int = 48) -> List[Dict[str, Any]]:
    if not _is_first_level_route_source_page(page):
        return []
    content_data = getattr(page, "content_data", {}) if isinstance(getattr(page, "content_data", {}), dict) else {}
    extracted_items = content_data.get("extracted_items", {}) if isinstance(content_data, dict) else {}
    if not isinstance(extracted_items, dict):
        return []
    controls: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    fallback_provenance = _fallback_provenance_from_screenshot_refs(page)
    for entity_name, items in extracted_items.items():
        if not isinstance(items, list) or not _entity_looks_first_level_route_entry(str(entity_name)):
            continue
        component_ref = _component_ref_for_entity(content_data, str(entity_name))
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            label = ""
            for field in BUTTON_LABEL_FIELDS:
                value = str(item.get(field) or "").strip()
                if value:
                    label = value
                    break
            if not label:
                continue
            surface = str(item.get("surface") or "").strip() or _surface_for_item(str(entity_name), item)
            key = (_normalize_label(label), surface)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            control = {
                "control_id": f"{getattr(page, 'page_id', '')}::{entity_name}::{index}",
                "label": label,
                "surface": surface,
                "component_kind": str(entity_name),
                "component_ref": component_ref,
                "source_entity": str(entity_name),
            }
            provenance = _canonical_provenance(item.get("provenance")) or fallback_provenance
            if provenance:
                control["provenance"] = provenance
            controls.append(control)
            if len(controls) >= limit:
                return controls
    return controls


def collect_candidate_targets(
    source_page: Any,
    controls: Sequence[Dict[str, Any]],
    page_prototypes: Sequence[Any],
    *,
    max_candidates: int = 64,
) -> List[Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    source_page_id = str(getattr(source_page, "page_id", "") or "")
    labels = [str(control.get("label") or "") for control in controls if isinstance(control, dict)]
    for page in page_prototypes:
        page_id = str(getattr(page, "page_id", "") or "")
        filename = _base_filename(getattr(page, "filename", ""))
        if not page_id or page_id == source_page_id or not filename:
            continue
        if not _is_first_level_route_target_page(page):
            continue
        aliases = _page_aliases(page)
        best = 0.0
        matched_label = ""
        matched_alias = ""
        matched_controls: List[Dict[str, Any]] = []
        for label in labels:
            for alias in aliases:
                score = _score_label_match(label, alias)
                if score >= 0.45:
                    matched_controls.append({
                        "label": label,
                        "alias": alias,
                        "score": round(score, 3),
                    })
                if score > best:
                    best = score
                    matched_label = label
                    matched_alias = alias
        route_path = ""
        if isinstance(getattr(page, "structure_data", None), dict):
            route_path = str(page.structure_data.get("route_path", "") or "")
        page_type = str(getattr(page, "page_type", "") or "")
        if best < 0.45:
            continue
        candidates[filename] = {
            "page_id": page_id,
            "filename": filename,
            "page_name": str(getattr(page, "page_name_full", "") or getattr(page, "page_name", "") or ""),
            "page_type": page_type,
            "route_path": route_path,
            "aliases": aliases[:8],
            "matched_source_label": matched_label,
            "matched_alias": matched_alias,
            "matched_controls": sorted(
                matched_controls,
                key=lambda item: (-float(item.get("score") or 0), str(item.get("label") or "")),
            )[:8],
            "retrieval_score": round(best, 3),
        }
    ordered = sorted(
        candidates.values(),
        key=lambda item: (-float(item.get("retrieval_score") or 0), item.get("filename") or ""),
    )
    return ordered[:max_candidates]


def _task_for_page(page: Any, page_prototypes: Sequence[Any], *, max_controls: int, max_candidates: int) -> Dict[str, Any] | None:
    controls = collect_visible_route_controls(page, limit=max_controls)
    if not controls:
        return None
    targets = collect_candidate_targets(page, controls, page_prototypes, max_candidates=max_candidates)
    if not targets:
        return None
    matched_control_labels = {
        _normalize_label(match.get("label"))
        for target in targets
        for match in (target.get("matched_controls", []) if isinstance(target.get("matched_controls"), list) else [])
        if isinstance(match, dict)
    }
    matched_control_labels.discard("")
    if matched_control_labels:
        controls = [
            control
            for control in controls
            if _normalize_label(control.get("label")) in matched_control_labels
        ]
    return {
        "source_page_id": str(getattr(page, "page_id", "") or ""),
        "source_filename": _base_filename(getattr(page, "filename", "")),
        "source_page_name": str(getattr(page, "page_name_full", "") or getattr(page, "page_name", "") or ""),
        "source_page_type": str(getattr(page, "page_type", "") or ""),
        "screenshot_refs": list(getattr(page, "screenshot_refs", []) or [])[:8],
        "visible_controls": controls,
        "candidate_targets": targets,
    }


def collect_alternative_route_access_tasks(
    site_model: Any,
    *,
    max_controls_per_page: int = 48,
    max_candidates_per_page: int = 64,
) -> List[Dict[str, Any]]:

    structure = getattr(site_model, "structure", None)
    page_prototypes = list(getattr(structure, "page_prototypes", []) or [])
    return [
        task
        for page in page_prototypes
        for task in [
            _task_for_page(
                page,
                page_prototypes,
                max_controls=max_controls_per_page,
                max_candidates=max_candidates_per_page,
            )
        ]
        if task
    ]


def _compact_task_for_llm(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source_page": {
            "page_id": task.get("source_page_id"),
            "filename": task.get("source_filename"),
            "page_name": task.get("source_page_name"),
            "page_type": task.get("source_page_type"),
            "screenshot_refs": task.get("screenshot_refs", []),
        },
        "visible_controls": [
            {
                "control_id": control.get("control_id"),
                "label": control.get("label"),
                "surface": control.get("surface"),
                "component_kind": control.get("component_kind"),
                "component_ref": control.get("component_ref"),
            }
            for control in task.get("visible_controls", [])
            if isinstance(control, dict)
        ],
        "candidate_targets": [
            {
                "page_id": target.get("page_id"),
                "filename": target.get("filename"),
                "page_name": target.get("page_name"),
                "page_type": target.get("page_type"),
                "route_path": target.get("route_path"),
                "aliases": target.get("aliases", []),
                "matched_source_label": target.get("matched_source_label"),
                "matched_alias": target.get("matched_alias"),
                "matched_controls": target.get("matched_controls", []),
            }
            for target in task.get("candidate_targets", [])
            if isinstance(target, dict)
        ],
    }


def compact_route_access_task_for_llm(task: Dict[str, Any]) -> Dict[str, Any]:
    return _compact_task_for_llm(task)


async def _plan_route_access_chunk(
    tasks: Sequence[Dict[str, Any]],
    *,
    model: str | None,
    reasoning_effort: str,
    max_tokens: int,
    llm_func: LLMJsonFunc = call_openai_api_json_async,
) -> Dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "You are compiling alternative route access for a generated GUI environment. "
                "Return JSON only. Decide whether visible controls on each source materialized page "
                "are first-level functional/channel entries that can directly navigate to existing "
                "channel or category landing pages. This is top-level business route reachability, "
                "not task-action broadcasting and not deep workflow inference."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "Map first-level source channel controls to existing channel/category landing pages when the route is semantically supported.",
                    "output_schema": {
                        "pages": [
                            {
                                "source_filename": "string",
                                "routes": [
                                    {
                                        "control_id": "string",
                                        "component_label": "visible label string",
                                        "surface": "category_grid|bottom_navigation|tab_bar|visible_button",
                                        "component_kind": "short kind",
                                        "target_filename": "must be one candidate filename",
                                        "target_node": "stable business target identity",
                                        "route_access_kind": "direct_shell_entry|top_level_channel_entry|tab_or_nav_entry",
                                        "route_binding_kind": "target_identity|route_parameter",
                                        "route_parameter_binding": {
                                            "parameter_name": "string, e.g. category/query/filter/selection",
                                            "parameter_value": "visible label value if the control parameterizes a generic target shell",
                                            "target_shell_identity": "stable identity of the target shell page",
                                        },
                                        "confidence": "high|medium|low",
                                        "evidence": "short reason grounded in the source controls and target identity",
                                    }
                                ],
                                "rejected": [
                                    {
                                        "control_id": "string",
                                        "component_label": "string",
                                        "reason": "short reason",
                                    }
                                ],
                            }
                        ]
                    },
                    "principles": [
                        "Only choose among candidate_targets; never invent a page.",
                        "Only accept top-level functional/channel entries. Reject product cards, merchant cards, form buttons, payment controls, filter chips, search results, suggestions, and runtime-only modal controls.",
                        "A target channel/category landing page may have multiple alternative access paths.",
                        "Do not use trace thought/action text as a visible component label.",
                        "Search query values and search suggestions are runtime parameters, not alternative route affordances.",
                        "Prefer the most specific visible control for a target. If a target page name contains both a parent vertical/channel label and a child category label visible on the same source page, choose the child/specific category control and reject the parent control for that child target.",
                        "A broad app/channel entry should navigate to the channel landing page, not to a narrower category/list/detail page when a narrower visible control exists.",
                        "If the visible control label is not the target page identity but a parameter for a generic target shell, set route_binding_kind=route_parameter and fill route_parameter_binding. Do not rely on filename words.",
                        "Use route_binding_kind=target_identity only when the visible control label denotes the destination identity itself.",
                        "If a visible control and target identity are ambiguous, reject it instead of guessing.",
                    ],
                    "pages": [_compact_task_for_llm(task) for task in tasks],
                },
                ensure_ascii=False,
            ),
        },
    ]
    reply, usage = await llm_func(
        messages,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        model=model,
        stage="alternative_route_access_planner",
    )
    try:
        payload = json.loads(reply) if isinstance(reply, str) else reply
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["_usage"] = usage
    return payload


def _validate_route(raw: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    controls = {
        str(control.get("control_id") or ""): control
        for control in task.get("visible_controls", [])
        if isinstance(control, dict) and control.get("control_id")
    }
    targets = {
        _base_filename(target.get("filename")): target
        for target in task.get("candidate_targets", [])
        if isinstance(target, dict) and _base_filename(target.get("filename"))
    }
    control_id = str(raw.get("control_id") or "").strip()
    target_filename = _base_filename(raw.get("target_filename"))
    if control_id not in controls or target_filename not in targets:
        return None
    confidence = str(raw.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    if confidence == "low":
        return None
    route_binding_kind = str(raw.get("route_binding_kind") or "target_identity").strip()
    if route_binding_kind not in {"target_identity", "route_parameter", "activation_control"}:
        route_binding_kind = "target_identity"
    route_parameter_binding = raw.get("route_parameter_binding")
    if not isinstance(route_parameter_binding, dict):
        route_parameter_binding = {}
    target = targets[target_filename]
    control = _prefer_specific_control_for_target(controls[control_id], controls, target)
    control_id = str(control.get("control_id") or control_id)
    control_label = str(control.get("label") or raw.get("component_label") or "").strip()
    if route_binding_kind == "target_identity":
        label_key = _normalize_label(control_label)
        target_key = _normalize_label(
            " ".join(
                str(value or "")
                for value in (
                    raw.get("target_node"),
                    target.get("page_name"),
                    target.get("filename"),
                    target.get("page_id"),
                )
            )
        )
        if label_key and target_key and label_key not in target_key and target_key not in label_key:
            return None
    route = {
        "source_page_id": task.get("source_page_id"),
        "source_filename": task.get("source_filename"),
        "control_id": control_id,
        "component_label": control_label,
        "surface": str(control.get("surface") or raw.get("surface") or "visible_button").strip(),
        "component_kind": str(control.get("component_kind") or raw.get("component_kind") or "").strip(),
        "component_ref": str(control.get("component_ref") or "").strip(),
        "source_entity": str(control.get("source_entity") or "").strip(),
        "target_page_id": str(target.get("page_id") or "").strip(),
        "target_filename": target_filename,
        "target_node": str(raw.get("target_node") or target.get("page_name") or target_filename).strip(),
        "route_access_kind": str(raw.get("route_access_kind") or "other_visible_entry").strip(),
        "route_binding_kind": route_binding_kind,
        "route_parameter_binding": {
            "parameter_name": str(route_parameter_binding.get("parameter_name") or "").strip(),
            "parameter_value": str(route_parameter_binding.get("parameter_value") or control.get("label") or "").strip(),
            "target_shell_identity": str(route_parameter_binding.get("target_shell_identity") or target.get("page_name") or target_filename).strip(),
        } if route_binding_kind == "route_parameter" else {},
        "confidence": confidence,
        "evidence": str(raw.get("evidence") or "").strip()[:300],
    }
    provenance = _canonical_provenance(control.get("provenance"))
    if provenance:
        route["provenance"] = provenance
        source_step = str(provenance.get("source_step") or "").strip()
        if source_step:
            route["evidence_steps"] = [source_step]
    return route


def validate_route_access_resolution(raw: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any] | None:
    return _validate_route(raw, task)


def _alias_specificity_for_label(label: str, alias: str) -> int:
    label_key = _normalize_label(label)
    if not label_key:
        return 0
    parts = [
        _normalize_label(part)
        for part in re.split(r"[-—–·:：/|>＞]+", str(alias or ""))
        if _normalize_label(part)
    ]
    if len(parts) <= 1:
        return 1 if label_key in _normalize_label(alias) else 0
    for index, part in enumerate(parts):
        if label_key and label_key in part:
            return 4 if index > 0 else 1
    return 0


def _prefer_specific_control_for_target(
    selected_control: Dict[str, Any],
    controls_by_id: Dict[str, Dict[str, Any]],
    target: Dict[str, Any],
) -> Dict[str, Any]:
    selected_label = str(selected_control.get("label") or "")
    selected_score = 0.0
    best_control = selected_control
    best_score = -1.0
    matched_controls = target.get("matched_controls", [])
    if not isinstance(matched_controls, list):
        return selected_control
    controls_by_label: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for control in controls_by_id.values():
        controls_by_label[_normalize_label(control.get("label"))].append(control)
    for match in matched_controls:
        if not isinstance(match, dict):
            continue
        label = str(match.get("label") or "")
        label_key = _normalize_label(label)
        if not label_key:
            continue
        alias = str(match.get("alias") or "")
        try:
            match_score = float(match.get("score") or 0)
        except Exception:
            match_score = 0.0
        specificity = _alias_specificity_for_label(label, alias)
        score = specificity * 10 + match_score + min(len(label_key), 16) / 100.0
        if _normalize_label(selected_label) == label_key:
            selected_score = max(selected_score, score)
        for control in controls_by_label.get(label_key, []):
            if score > best_score:
                best_score = score
                best_control = control
    if best_control is not selected_control and best_score >= selected_score + 5:
        return best_control
    return selected_control


async def build_alternative_route_access_plan_async(
    site_model: Any,
    result_dir: Path | str,
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_pages_per_call: int = 4,
    max_concurrent: int = 8,
    max_controls_per_page: int = 48,
    max_candidates_per_page: int = 64,
    max_tokens: int = 12000,
    llm_func: LLMJsonFunc = call_openai_api_json_async,
) -> Dict[str, Any]:
    structure = getattr(site_model, "structure", None)
    page_prototypes = list(getattr(structure, "page_prototypes", []) or [])
    tasks = [
        task
        for page in page_prototypes
        for task in [_task_for_page(
            page,
            page_prototypes,
            max_controls=max_controls_per_page,
            max_candidates=max_candidates_per_page,
        )]
        if task
    ]
    usage_records: List[Any] = []
    errors: List[str] = []
    raw_pages: List[Dict[str, Any]] = []

    async def run_chunk(chunk: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            return await _plan_route_access_chunk(
                chunk,
                model=model,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                llm_func=llm_func,
            )
        except Exception as exc:
            return {"pages": [], "error": str(exc)}

    chunks = [tasks[i : i + max(1, int(max_pages_per_call or 1))] for i in range(0, len(tasks), max(1, int(max_pages_per_call or 1)))]
    replies = await asyncio.gather(*(run_chunk(chunk) for chunk in chunks)) if chunks else []
    task_by_filename = {str(task.get("source_filename") or ""): task for task in tasks}
    routes_by_filename: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        if reply.get("error"):
            errors.append(str(reply.get("error")))
        if reply.get("_usage"):
            usage_records.append(reply.get("_usage"))
        for page_payload in reply.get("pages", []) if isinstance(reply.get("pages"), list) else []:
            if not isinstance(page_payload, dict):
                continue
            source_filename = _base_filename(page_payload.get("source_filename"))
            task = task_by_filename.get(source_filename)
            if not task:
                continue
            raw_pages.append(page_payload)
            seen: set[Tuple[str, str]] = set()
            for raw_route in page_payload.get("routes", []) if isinstance(page_payload.get("routes"), list) else []:
                route = _validate_route(raw_route, task)
                if not route:
                    continue
                key = (str(route.get("component_label") or ""), str(route.get("target_filename") or ""))
                if key in seen:
                    continue
                seen.add(key)
                routes_by_filename[source_filename].append(route)

    routes = [route for values in routes_by_filename.values() for route in values]
    return {
        "format": "alternative_route_access_plan",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "semantic_api.alternative_route_access_plan",
            "artifact_kind": "api_semantic_source",
            "api_or_det": "api",
            "canonical_source": True,
            "canonical_semantic_source": True,
            "canonical_prompt_boundary": False,
            "logical_owner": "route_access_planner",
            "semantic_role": "alternative_route_access",
            "producer": "site_model.alternative_route_access.build_alternative_route_access_plan_async",
            "allowed_consumers": ["page_execution_contracts", "page_environment_compiler", "validator"],
        },
        "result_dir": str(result_dir),
        "model": model or "",
        "reasoning_effort": reasoning_effort,
        "task_count": len(tasks),
        "route_count": len(routes),
        "pages_by_filename": dict(routes_by_filename),
        "routes": routes,
        "raw_pages": raw_pages,
        "errors": errors[:16],
        "usage_records": usage_records[:16],
    }


def _nav_entry_from_route(route: Dict[str, Any]) -> Dict[str, Any]:
    label = str(route.get("component_label") or "").strip()
    target_filename = _base_filename(route.get("target_filename"))
    target_page_id = str(route.get("target_page_id") or "").strip()
    component_ref = str(route.get("component_ref") or "").strip()
    entity = str(route.get("source_entity") or "").strip()
    surface = str(route.get("surface") or "visible_button").strip()
    binding_kind = "category_entry"
    if surface in {"search_surface", "search_suggestion"}:
        binding_kind = "search_control" if surface == "search_surface" else "search_suggestion"
    elif surface in {"bottom_navigation", "navigation"}:
        binding_kind = "global_nav_item"
    elif surface == "tab_bar":
        binding_kind = "tab_entry"
    binding = {
        "binding_kind": binding_kind,
        "labels": [label] if label else [],
        "surface": surface,
        "source_scope": "alternative_route_access_plan",
        "match_policy": "exact_visible_component_label",
        "confidence": str(route.get("confidence") or "medium"),
    }
    if component_ref:
        binding["component_ref"] = component_ref
    if entity:
        binding["entity"] = entity
    return {
        "to_page_id": target_page_id,
        "to_page": str(route.get("target_node") or target_page_id or target_filename),
        "to_filename": target_filename,
        "trigger": f"点击{label}" if label else "点击可见入口",
        "kind": "navigation",
        "to_route": "",
        "to_route_pattern": "",
        "params_needed": [],
        "source": "alternative_route_access_plan",
        "target_source": "shell_component_affordance",
        "affordance_kind": "shell_component_navigation_affordance",
        "route_access_kind": str(route.get("route_access_kind") or ""),
        "route_binding_semantics": {
            "binding_kind": str(route.get("route_binding_kind") or "target_identity"),
            "parameter_binding": route.get("route_parameter_binding", {}) if isinstance(route.get("route_parameter_binding"), dict) else {},
            "source": "alternative_route_access_plan",
        },
        "target_node": str(route.get("target_node") or ""),
        "label": label,
        "labels": [label] if label else [],
        "component_ref": component_ref,
        "component_role": surface,
        "entity": entity,
        "binding": binding,
        "provenance": {
            "source": "alternative_route_access_plan",
            "source_page_id": str(route.get("source_page_id") or ""),
            "source_filename": str(route.get("source_filename") or ""),
            "target_filename": target_filename,
            "evidence": str(route.get("evidence") or ""),
        },
        "admission_reason": "llm_alternative_route_access",
        "trigger_visible_text_allowed": True,
    }


def apply_alternative_route_access_plan(site_model: Any, plan: Dict[str, Any]) -> int:
    structure = getattr(site_model, "structure", None)
    if structure is None or not isinstance(plan, dict):
        return 0
    page_behaviors = getattr(structure, "page_behaviors", None)
    if not isinstance(page_behaviors, dict):
        page_behaviors = {}
        structure.page_behaviors = page_behaviors
    applied = 0
    for route in plan.get("routes", []) if isinstance(plan.get("routes"), list) else []:
        if not isinstance(route, dict):
            continue
        source_page_id = str(route.get("source_page_id") or "").strip()
        if not source_page_id:
            continue
        behavior = page_behaviors.setdefault(source_page_id, {})
        if not isinstance(behavior, dict):
            behavior = {}
            page_behaviors[source_page_id] = behavior
        nav_out = behavior.get("navigation_out", [])
        if not isinstance(nav_out, list):
            nav_out = []
        entry = _nav_entry_from_route(route)
        key = (
            str(entry.get("label") or ""),
            str(entry.get("to_filename") or ""),
            str(entry.get("component_role") or ""),
        )
        existing = {
            (
                str(item.get("label") or ""),
                str(item.get("to_filename") or ""),
                str(item.get("component_role") or ""),
            )
            for item in nav_out
            if isinstance(item, dict)
        }
        if key in existing:
            continue
        nav_out.append(entry)
        behavior["navigation_out"] = nav_out
        applied += 1
    return applied
