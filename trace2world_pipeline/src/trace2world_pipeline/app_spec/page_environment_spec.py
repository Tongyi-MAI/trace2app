
from __future__ import annotations

import ast
import json
import re
import asyncio
import hashlib
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from trace2world_pipeline.core.step_keys import canonical_step_key, normalize_step_keyed_mapping
from trace2world_pipeline.app_spec.action_effect_admission import ActionEvidence, admit_same_page_click_effect
from trace2world_pipeline.app_spec.artifact_index import rebuild_artifact_index, register_artifact


SEARCH_MARKERS = ("search", "query", "搜索", "检索")
COLLECTION_MARKERS = ("list", "listing", "collection", "cart", "address", "invoice_title", "results")
SEARCHLIKE_FIELD_ROLES = {"business_query", "location_lookup", "address_lookup"}
TIME_PICKER_MARKERS = (
    "出发时间",
    "预约时间",
    "时间选择",
    "时间设置",
    "现在出发",
    "08:00",
    "08点",
    "8点",
    "time picker",
    "departure time",
    "schedule",
)
FUTURE_QUERY_STATE_HINT_MARKERS = (
    "filter_applied:",
    "sort_applied:",
    "selected_filter:",
    "applied_filter:",
    "applied_sort:",
    "result_controls:filter_applied:",
    "result_controls:sort_applied:",
)
QUERY_TRANSITION_KINDS = {
    "query_entry",
    "suggestion_projection",
    "selected_suggestion_commit",
    "committed_result",
    "search_within_collection",
    "unrelated_navigation",
    "unknown",
}
PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY = "closure_default_review_only"
PHASE_3_LOGICFLOW_LEGACY_UNIFIED_PLANNER = "legacy_unified_planner"
L0_COMPONENT_RENDER_BINDING_PHASE = "l0_component_render_binding"
L0_COMPONENT_RENDER_BINDING_SIGNATURE_PHASE = f"page_environment.{L0_COMPONENT_RENDER_BINDING_PHASE}"
LEGACY_COMPONENT_PROJECTION_SIGNATURE_PHASE = "page_environment.component_projection"
L0_COMPONENT_RENDER_BINDING_STAGE = "page_environment_l0_component_render_binding_planner"
LEGACY_COMPONENT_PROJECTION_STAGE = "page_environment_component_projection_enricher"
L0_COMPONENT_RENDER_BINDING_CACHE_FILE = "l0_component_render_binding_outputs.json"
LEGACY_COMPONENT_PROJECTION_CACHE_FILE = "component_projection_outputs.json"
L0_COMPONENT_RENDER_BINDING_CACHE_FORMAT = "page_environment_unified_planner_l0_component_render_binding_cache"
LEGACY_COMPONENT_PROJECTION_CACHE_FORMAT = "page_environment_unified_planner_component_projection_cache"
L0_COMPONENT_RENDER_BINDING_COMPLETION_FORMAT = "page_environment_l0_component_render_binding_completion_report"
L0_COMPONENT_RENDER_BINDING_COMPLETION_FILE = "l0_component_render_binding_completion_report.json"
L0_COMPONENT_RENDER_BINDING_REPAIR_FORMAT = "page_environment_l0_component_render_binding_repair_report"
L0_COMPONENT_RENDER_BINDING_REPAIR_FILE = "l0_component_render_binding_repair_report.json"
L0_COMPONENT_RENDER_BINDING_BUDGET_FORMAT = "page_environment_l0_component_render_binding_chunk_budget_report"
LEGACY_COMPONENT_PROJECTION_BUDGET_FORMAT = "page_environment_component_projection_chunk_budget_report"
L0_COMPONENT_RENDER_BINDING_BUDGET_FILE = "l0_component_render_binding_chunk_budget_report.json"
L0_COMPONENT_RENDER_BINDING_ENRICHMENT_FORMAT = "page_environment_l0_component_render_binding_enrichment"


def _normalize_phase_3_logicflow(value: Any) -> str:
    logicflow = str(value or "").strip()
    if logicflow in {
        "",
        "default",
        "closure",
        "closure_only",
        "closure_default",
        "review_only_closure_default",
        PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY,
    }:
        return PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY
    if logicflow in {
        "legacy",
        "legacy_unified",
        "legacy_unified_planner",
        "environment_action_route_planner",
        "full_llm_reject",
    }:
        return PHASE_3_LOGICFLOW_LEGACY_UNIFIED_PLANNER
    return PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY


def _planner_log_info(logger: Any | None, message: str) -> None:
    if logger is None:
        return
    log_info = getattr(logger, "log_info", None) or getattr(logger, "info", None)
    if callable(log_info):
        log_info(message)


def _planner_log_warning(logger: Any | None, message: str) -> None:
    if logger is None:
        return
    log_warning = getattr(logger, "log_warning", None) or getattr(logger, "warning", None)
    if callable(log_warning):
        log_warning(message)


def _usage_total_tokens(usage: Any) -> Any:
    if isinstance(usage, dict):
        for key in ("total_tokens", "total_token_count", "total"):
            if usage.get(key) is not None:
                return usage.get(key)
    for key in ("total_tokens", "total_token_count", "total"):
        if hasattr(usage, key):
            value = getattr(usage, key)
            if value is not None:
                return value
    return "unknown"


def _usage_call_timing(usage: Any) -> Dict[str, Any]:
    if isinstance(usage, dict):
        timing = usage.get("_llm_call_timing") or usage.get("llm_call_timing")
        return timing if isinstance(timing, dict) else {}
    timing = getattr(usage, "_llm_call_timing", None) or getattr(usage, "llm_call_timing", None)
    return timing if isinstance(timing, dict) else {}


def _format_seconds_for_log(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "unknown"


def _usage_timing_log_fragment(usage: Any, *, chunk_wall_s: float | None = None) -> str:
    timing = _usage_call_timing(usage)
    llm_total_s = timing.get("total_duration_s")
    chunk_minus_final_llm_s: Any = None
    if chunk_wall_s is not None and llm_total_s is not None:
        try:
            chunk_minus_final_llm_s = max(0.0, float(chunk_wall_s) - float(llm_total_s))
        except (TypeError, ValueError):
            chunk_minus_final_llm_s = None
    return (
        f"final_llm_total_s={_format_seconds_for_log(llm_total_s)} "
        f"final_api_s={_format_seconds_for_log(timing.get('api_duration_s'))} "
        f"final_queue_s={_format_seconds_for_log(timing.get('queue_wait_s'))} "
        f"final_parse_s={_format_seconds_for_log(timing.get('parse_duration_s'))} "
        f"final_retry_s={_format_seconds_for_log(timing.get('retry_wait_s'))} "
        f"chunk_minus_final_llm_s={_format_seconds_for_log(chunk_minus_final_llm_s)}"
    )


def _stable_json_signature(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unified_planner_cache_path(result_dir: Path, filename: str) -> Path:
    return Path(result_dir) / "phase_page_environment" / "debug" / "unified_planner" / filename


def _read_unified_planner_phase_cache(
    result_dir: Path,
    filename: str,
    *,
    expected_format: str,
    input_signature: str,
) -> Dict[str, Any] | None:
    path = _unified_planner_cache_path(result_dir, filename)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("format") != expected_format:
        return None
    if payload.get("input_signature") != input_signature:
        return None
    return payload


def _read_unified_planner_phase_cache_candidates(
    result_dir: Path,
    candidates: Sequence[Tuple[str, str, str]],
) -> Dict[str, Any] | None:
    for filename, expected_format, input_signature in candidates:
        payload = _read_unified_planner_phase_cache(
            result_dir,
            filename,
            expected_format=expected_format,
            input_signature=input_signature,
        )
        if payload is not None:
            payload = dict(payload)
            payload["_cache_filename"] = filename
            payload["_cache_format"] = expected_format
            return payload
    return None


def _write_unified_planner_phase_cache(result_dir: Path, filename: str, payload: Dict[str, Any]) -> Path:
    path = _unified_planner_cache_path(result_dir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)
    return path


def _component_projection_completion_report(
    component_tasks: Sequence[Dict[str, Any]],
    component_outputs: Sequence[Dict[str, Any]],
    *,
    source: str,
) -> Dict[str, Any]:
    expected_filenames = sorted({
        str(task.get("filename") or "").strip()
        for task in component_tasks
        if isinstance(task, dict) and str(task.get("filename") or "").strip()
    })
    output_filenames = sorted({
        str(item.get("filename") or "").strip()
        for item in component_outputs
        if isinstance(item, dict) and str(item.get("filename") or "").strip()
    })
    expected_set = set(expected_filenames)
    output_set = set(output_filenames)
    missing_filenames = sorted(expected_set - output_set)
    extra_filenames = sorted(output_set - expected_set)
    return {
        "format": L0_COMPONENT_RENDER_BINDING_COMPLETION_FORMAT,
        "schema_revision": 1,
        "source": source,
        "status": "complete" if not missing_filenames else "incomplete",
        "expected_page_count": len(expected_filenames),
        "output_page_count": len(output_filenames),
        "expected_filenames": expected_filenames,
        "output_filenames": output_filenames,
        "missing_filenames": missing_filenames,
        "extra_filenames": extra_filenames,
    }


def _write_component_projection_completion_report(
    result_dir: Path,
    report: Dict[str, Any],
) -> Path:
    return _write_unified_planner_phase_cache(
        result_dir,
        L0_COMPONENT_RENDER_BINDING_COMPLETION_FILE,
        report,
    )


def _write_component_projection_repair_report(
    result_dir: Path,
    report: Dict[str, Any],
) -> Path:
    return _write_unified_planner_phase_cache(
        result_dir,
        L0_COMPONENT_RENDER_BINDING_REPAIR_FILE,
        report,
    )


def _query_identity_completion_report(
    llm_query_tasks: Sequence[Dict[str, Any]],
    raw_by_id: Dict[str, Dict[str, Any]],
    *,
    source: str,
    errors: Sequence[str] | None = None,
) -> Dict[str, Any]:
    expected_query_ids = sorted({
        str(task.get("query_id") or "").strip()
        for task in llm_query_tasks
        if isinstance(task, dict) and str(task.get("query_id") or "").strip()
    })
    output_query_ids = sorted({
        str(query_id or "").strip()
        for query_id, value in raw_by_id.items()
        if str(query_id or "").strip() and isinstance(value, dict)
    })
    expected_set = set(expected_query_ids)
    output_set = set(output_query_ids)
    missing_query_ids = sorted(expected_set - output_set)
    extra_query_ids = sorted(output_set - expected_set)
    error_list = [str(item) for item in (errors or []) if str(item)]
    return {
        "format": "page_environment_query_identity_completion_report",
        "schema_revision": 1,
        "source": source,
        "status": "complete" if not missing_query_ids and not error_list else "incomplete",
        "expected_query_count": len(expected_query_ids),
        "output_query_count": len(output_query_ids),
        "missing_query_count": len(missing_query_ids),
        "expected_query_ids": expected_query_ids,
        "output_query_ids": output_query_ids,
        "missing_query_ids": missing_query_ids,
        "extra_query_ids": extra_query_ids,
        "errors": error_list[:16],
    }


def _write_query_identity_completion_report(
    result_dir: Path,
    report: Dict[str, Any],
) -> Path:
    return _write_unified_planner_phase_cache(
        result_dir,
        "query_identity_completion_report.json",
        report,
    )


QUERY_IDENTITY_STATUSES = {"match", "mismatch", "not_applicable", "unknown"}


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


def _contains_any(text: Any, markers: Sequence[str]) -> bool:
    normalized = str(text or "").strip().lower()
    return bool(normalized) and any(marker.lower() in normalized for marker in markers)


def _quoted_or_clicked_label(text: Any, *, limit: int = 24) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    quoted = [
        value.strip()
        for value in re.findall(r"[“\"']([^”\"']{1,32})[”\"']", raw)
        if value.strip()
    ]
    if len(quoted) >= 2 and re.search(
        r"(?:区域|板块|栏目|分区|模块|列表|面板|浮层|弹窗|sheet|overlay|modal)[^，。,.]{0,24}(?:中|中的)",
        raw,
        re.IGNORECASE,
    ):
        return quoted[-1][:limit]
    for pattern in (r"[“\"']([^”\"']{1,32})[”\"']", r"点击\s*([^\s，。,.、]{1,16})"):
        match = re.search(pattern, raw)
        if match:
            return match.group(1).strip()[:limit]
    cleaned = re.sub(r"^(点击|选择|打开|切换到|输入|tap|click|select)\s*", "", raw, flags=re.I)
    return cleaned.strip()[:limit]


def _action_label_contract_from_intent(text: Any, *, limit: int = 24) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    contract: Dict[str, Any] = {}
    quoted = [
        value.strip()
        for value in re.findall(r"[“\"']([^”\"']{1,32})[”\"']", raw)
        if value.strip()
    ]
    context_labels: List[str] = []
    accessible_labels: List[str] = []
    host_label = ""

    if quoted:
        first = quoted[0]
        contextual_container = len(quoted) >= 2 and re.search(
            r"(?:区域|板块|栏目|分区|模块|列表|面板|浮层|弹窗|sheet|overlay|modal)[^，。,.]{0,24}(?:中|中的)",
            raw,
            re.IGNORECASE,
        )
        if contextual_container:
            context_labels.extend(quoted[:-1])
            host_label = quoted[-1]
        after = raw.split(first, 1)[1] if not host_label and first in raw else ""
        if host_label:
            pass
        elif re.search(r"(?:区域|板块|栏目|分区|模块|列表|频道)的(?:第[一二三四五六七八九十\d]+|首个|第一)", after):
            context_labels.append(first)
        elif re.search(r"(?:右侧|左侧|旁边|后面|前面|同一行|所在行|该行|行内)的?", after):
            context_labels.append(first)
            control_match = re.search(
                r"(删除|移除|编辑|修改|更多|查看|选择|勾选|取消|关闭|展开|收起|trash|delete|edit|more|remove)",
                after,
                re.IGNORECASE,
            )
            if control_match:
                host_label = control_match.group(1)
        else:
            host_label = first
    else:
        clicked_match = re.search(r"点击\s*([^\s，。,.、]{1,16})", raw)
        if clicked_match:
            host_label = clicked_match.group(1)

    intent_lower = raw.lower()
    if any(token in intent_lower for token in ("删除", "移除", "退格", "backspace", "delete", "remove", "trash")):
        accessible_labels.extend(["删除", "backspace"])
        if not host_label:
            host_label = "删除"
    elif any(token in intent_lower for token in ("编辑", "修改", "edit")):
        accessible_labels.append("编辑")
        if not host_label:
            host_label = "编辑"
    elif any(token in intent_lower for token in ("更多", "more")):
        accessible_labels.extend(["更多", "more"])
        if not host_label:
            host_label = "更多"

    semantic_visual_label = ""
    if host_label and not quoted:
        label_text = host_label.lower()


        if (
            "座位图" in host_label
            or "图中" in host_label
            or ("区域" in host_label and any(token in host_label for token in ("座位", "可选", "选座")))
            or ("可选座" in host_label)
            or ("seat" in label_text and any(token in label_text for token in ("map", "available", "area")))
        ):
            semantic_visual_label = re.sub(
                r"^(点击|选择|打开|切换到|输入|tap|click|select)\s*",
                "",
                raw,
                flags=re.I,
            ).strip(" ：:，,。")

    if host_label:
        contract["host_display_label"] = host_label[:limit]
    context_labels = _dedupe(context_labels, limit=8)
    accessible_labels = _dedupe(accessible_labels, limit=8)
    if context_labels:
        contract["context_text_must_include"] = context_labels
        contract["context_match_scope"] = "ancestor_or_nearby_region"
    if accessible_labels:
        contract["accessible_label_must_include"] = accessible_labels
    semantic_role_label = ""
    role_source = " ".join(item for item in (raw, host_label) if item)
    if any(token in role_source for token in ("顶部搜索框", "顶部搜索栏", "搜索框", "搜索栏", "搜索入口")):
        semantic_role_label = "顶部搜索框" if "顶部" in role_source else "搜索框"
    if semantic_role_label:
        existing_accessible = list(contract.get("accessible_label_must_include") or [])
        if semantic_role_label not in existing_accessible:
            existing_accessible.append(semantic_role_label)
        contract["accessible_label_must_include"] = _dedupe(existing_accessible, limit=8)
        contract["visible_text_contract"] = {
            "authority": "semantic_role_only",
            "exact_text": "",
        }
        contract["label_authority"] = "semantic_role_only"
        contract.pop("host_display_label", None)
        return contract
    if semantic_visual_label:
        existing_accessible = list(contract.get("accessible_label_must_include") or [])
        if semantic_visual_label not in existing_accessible:
            existing_accessible.append(semantic_visual_label)
        contract["accessible_label_must_include"] = _dedupe(existing_accessible, limit=8)
        contract["visible_text_contract"] = {
            "authority": "semantic_role_only",
            "exact_text": "",
        }
        contract["label_authority"] = "semantic_role_only"
        contract.pop("host_display_label", None)
        return contract
    if host_label:
        contract["visible_text_contract"] = {
            "authority": "observed_host_text",
            "exact_text": host_label[:limit],
        }
        contract["label_authority"] = "observed_host_text"
    return contract


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_l0_execution_pack(site_model) -> Dict[str, Any]:
    payload = site_model.l0_execution_pack
    if not isinstance(payload, dict) or payload.get("format") != "l0_execution_pack":
        raise ValueError("invalid or missing L0 execution pack on AppSpec")
    return payload


def _load_action_packs_for_page_environment(site_model) -> List[Dict[str, Any]]:
    pack = _load_l0_execution_pack(site_model)
    packs = pack.get("action_packs", [])
    if not isinstance(packs, list):
        return []
    return [item for item in packs if isinstance(item, dict)]


def _load_runtime_obligations(site_model) -> Dict[str, List[Dict[str, Any]]]:
    rso = site_model.runtime_state_observations
    payload = rso.get("derived", {}).get("runtime_state_obligations", {}) if isinstance(rso, dict) else {}
    by_trace = payload.get("obligations_by_trace_id", {}) if isinstance(payload, dict) else {}
    if isinstance(by_trace, dict):
        return {
            str(trace_id): [item for item in items if isinstance(item, dict)]
            for trace_id, items in by_trace.items()
            if isinstance(items, list)
        }
    obligations = payload.get("obligations", []) if isinstance(payload, dict) else []
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in obligations if isinstance(obligations, list) else []:
        if isinstance(item, dict) and item.get("trace_id"):
            grouped[str(item["trace_id"])].append(item)
    return dict(grouped)


def _load_pagegen_obligations(site_model) -> Dict[str, List[Dict[str, Any]]]:
    payload = _load_l0_execution_pack(site_model)
    pages = payload.get("obligations_by_page", {}) if isinstance(payload, dict) else {}
    if not isinstance(pages, dict):
        return {}
    return {
        str(filename): [item for item in items if isinstance(item, dict)]
        for filename, items in pages.items()
        if isinstance(items, list)
    }


def _load_page_execution_contracts_by_filename(site_model) -> Dict[str, Dict[str, Any]]:
    payload = site_model.page_execution_contracts
    if not isinstance(payload, dict):
        return {}
    by_filename = payload.get("pages_by_filename")
    if isinstance(by_filename, dict):
        return {
            _base_filename(filename): page
            for filename, page in by_filename.items()
            if _base_filename(filename) and isinstance(page, dict)
        }
    pages = payload.get("pages", [])
    if not isinstance(pages, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        filename = _base_filename(page.get("filename"))
        if filename:
            result[filename] = page
    return result


def _load_route_affordance_candidates_by_filename(site_model) -> Dict[str, List[Dict[str, Any]]]:
    payload = site_model.route_affordance_candidates
    if not isinstance(payload, dict):
        return {}
    pages = payload.get("pages_by_filename")
    if not isinstance(pages, dict):
        return {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    for filename, affordances in pages.items():
        base = _base_filename(filename)
        if not base or not isinstance(affordances, list):
            continue
        result[base] = [item for item in affordances if isinstance(item, dict)]
    return result


def _load_canonical_route_materialization(site_model) -> Dict[str, Any]:
    payload = site_model.canonical_route_materialization
    return payload if isinstance(payload, dict) else {}


def _step_key(trace_id: str, step_index: Any) -> str:
    try:
        return f"{trace_id}:step{int(step_index)}"
    except Exception:
        return f"{trace_id}:step{step_index}"


def _base_filename(filename: Any) -> str:
    return str(filename or "").split("?", 1)[0].strip()


def _materialized_id_to_filename(site_model) -> Dict[str, str]:
    pages_by_id, _pages_by_filename, pages_by_general = _page_signature_maps(site_model)
    mapping: Dict[str, str] = {}
    for page in pages_by_id.values():
        filename = _base_filename(page.get("filename"))
        general_page_id = str(page.get("general_page_id") or "")
        materialized_key = str(page.get("materialized_page_key") or "")
        if filename and general_page_id and materialized_key:
            mapping[f"{general_page_id}::{materialized_key}"] = filename
    for general_page_id, pages in pages_by_general.items():
        if len(pages) == 1:
            filename = _base_filename(pages[0].get("filename"))
            if filename:
                mapping[str(general_page_id)] = filename
    return mapping


def _observation_filename_overrides(
    site_model,
) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    materialized_id_map = _materialized_id_to_filename(site_model)
    try:
        from trace2world_pipeline.app_spec.materialized_observation_bindings import (
            load_obs_to_filename_from_materialized_bindings,
        )

        overrides.update(load_obs_to_filename_from_materialized_bindings(site_model))
    except Exception:
        pass
    cluster_assignments = site_model.cluster_assignments
    step_labels = cluster_assignments.get("step_labels", []) if isinstance(cluster_assignments, dict) else []
    for item in step_labels if isinstance(step_labels, list) else []:
        if not isinstance(item, dict):
            continue
        trace_id = str(item.get("trace_id") or "").strip()
        step_index = item.get("step_index")
        materialized_id = str(item.get("materialized_page_id") or "").strip()
        filename = materialized_id_map.get(materialized_id)
        if not trace_id or step_index is None or not filename:
            continue
        try:
            index = int(step_index)
        except Exception:
            index = step_index
        overrides.setdefault(f"{trace_id}:{index}", filename)
        overrides.setdefault(f"{trace_id}:step{index}", filename)
    return overrides


def _canonical_state_filename(state: Dict[str, Any], overrides: Dict[str, str] | None = None) -> str:
    overrides = overrides or {}
    observation_key = str(state.get("observation_key") or "")
    if observation_key and overrides.get(observation_key):
        return overrides[observation_key]
    return _base_filename(state.get("filename"))


def _command_for_step(step: Dict[str, Any]) -> str:
    action = step.get("action", {}) if isinstance(step.get("action"), dict) else {}
    raw = str(action.get("raw_action") or "").strip()
    if raw:
        return raw
    operation = str(action.get("operation") or "").strip()
    if operation == "fill":
        return "type " + str(action.get("input_text") or "")
    coords = action.get("viewport_coordinates")
    if isinstance(coords, list) and operation == "click" and len(coords) >= 2:
        return f"tap {coords[0]} {coords[1]}"
    return operation


def _is_search_surface(filename: str, text: str = "") -> bool:
    lowered = f"{filename} {text}".lower()
    return any(marker in lowered for marker in SEARCH_MARKERS)


def _is_search_entry_filename(filename: str) -> bool:
    base = _base_filename(filename).lower()
    if not base:
        return False
    if "search" not in base and "搜索" not in base:
        return False
    return "result" not in base and "results" not in base


def _is_search_results_filename(filename: str) -> bool:
    base = _base_filename(filename).lower()
    if not base:
        return False
    if "search" not in base and "搜索" not in base:
        return False
    return "result" in base or "results" in base


def _is_query_entry_click(source: str, target: str, intent: str) -> bool:
    if not source or not target or _base_filename(source) == _base_filename(target):
        return False
    return _is_search_entry_filename(target) and _contains_any(intent, SEARCH_MARKERS + ("搜索框", "输入框", "search box"))


def _is_search_activation_intent(text: str) -> bool:
    return _contains_any(
        text,
        SEARCH_MARKERS
        + (
            "搜索框",
            "输入框",
            "查找框",
            "search box",
            "search field",
            "query field",
        ),
    )


def _selection_state_ref(filename: str, role: str) -> str:
    return _state_ref(f"{_base_filename(filename)}::{role or 'selection'}")


def _is_time_picker_intent(text: str) -> bool:
    return _contains_any(str(text or ""), TIME_PICKER_MARKERS) and _contains_any(
        str(text or ""),
        ("选择", "设置", "打开", "点击", "预约", "出发", "确认", "picker", "schedule"),
    )


def _is_year_month_picker_intent(text: str) -> bool:
    text = str(text or "")
    return _contains_any(
        text,
        (
            "选择年月",
            "年月选择",
            "年份滚轮",
            "月份滚轮",
            "年月滚轮",
            "year month",
            "year/month",
        ),
    ) and _contains_any(text, ("选择", "点击", "打开", "滚轮", "picker", "wheel"))


def _is_direct_time_shortcut_commit_intent(text: str, label: str) -> bool:
    text = str(text or "")
    label = str(label or "").strip()
    lowered = text.lower()
    shortcut_labels = {"今天", "明天", "后天", "现在"}
    if label not in shortcut_labels and not _contains_any(text, ("快捷选项", "快捷日期", "快速选择")):
        return False
    if _contains_any(
        text,
        (
            "打开",
            "选择器",
            "时间选择",
            "日期选择",
            "其他日期",
            "自定义",
            "日历",
            "面板",
            "弹窗",
            "picker",
            "calendar",
            "wheel",
            "modal",
            "sheet",
        ),
    ):
        return False
    return (
        _contains_any(text, ("日期", "时间", "截止", "due"))
        and _contains_any(text, ("点击", "选择", "设为", "设置", "select"))
    ) or ("quick" in lowered and _contains_any(text, ("date", "time")))


def _is_visible_notice_dismiss_intent(
    text: str,
    label: str,
    *,
    surface_contract: Dict[str, Any] | None = None,
) -> bool:
    text = str(text or "")
    label = str(label or "").strip()
    surface_contract = surface_contract if isinstance(surface_contract, dict) else {}
    surface_kind = str(surface_contract.get("opened_surface_kind") or surface_contract.get("surface_kind") or "")
    notice_surface = surface_kind in {"notice_banner", "banner", "toast", "prompt", "notice"}
    if not notice_surface and not _contains_any(
        text,
        (
            "提示",
            "横幅",
            "通知",
            "公告",
            "弹窗",
            "浮层",
            "遮挡",
            "notice",
            "banner",
            "toast",
            "prompt",
        ),
    ):
        return False
    if _contains_any(
        text,
        (
            "打开",
            "展开",
            "进入",
            "选择",
            "更多",
            "列表",
            "菜单",
            "open",
            "expand",
            "select",
            "menu",
        ),
    ):
        return False
    return _contains_any(
        text,
        (
            "右侧",
            "知道了",
            "我知道了",
            "不再提示",
            "关闭",
            "取消",
            "dismiss",
            "close",
        ),
    ) or label in {"知道了", "我知道了", "不再提示", "关闭", "取消"}


def _surface_contract_has_explicit_close_lifecycle(
    surface_contract: Dict[str, Any] | None,
) -> bool:
    if not isinstance(surface_contract, dict):
        return False
    lifecycle_evidence = " ".join(
        [
            str(surface_contract.get("lifecycle") or ""),
            str(surface_contract.get("mutation") or ""),
        ]
        + [
            str(item or "")
            for item in surface_contract.get("terminal_constraints", [])
        ]
        if isinstance(surface_contract.get("terminal_constraints"), list)
        else [
            str(surface_contract.get("lifecycle") or ""),
            str(surface_contract.get("mutation") or ""),
        ]
    ).lower()
    return _contains_any(
        lifecycle_evidence,
        (
            "close",
            "dismiss",
            "hide",
            "hidden",
            "consume",
            "remove",
            "collapse",
            "no longer present",
            "关闭",
            "消失",
            "隐藏",
            "收起",
            "移除",
            "已消费",
        ),
    )


def _surface_contract_has_explicit_open_lifecycle(
    surface_contract: Dict[str, Any] | None,
) -> bool:
    if not isinstance(surface_contract, dict):
        return False
    evidence = " ".join(
        str(surface_contract.get(key) or "")
        for key in ("lifecycle", "mutation")
    ).lower()
    return _contains_any(
        evidence,
        (
            "open",
            "opened",
            "presented",
            "activated",
            "replace_modal",
            "open_panel",
            "open_modal",
            "展开",
            "打开",
            "弹出",
        ),
    )


def _surface_contract_has_progressive_selection_lifecycle(
    surface_contract: Dict[str, Any] | None,
) -> bool:
    if not isinstance(surface_contract, dict):
        return False
    lifecycle = str(surface_contract.get("lifecycle") or "").strip().lower()
    mutation = str(surface_contract.get("mutation") or "").strip().lower()
    return (
        any(token in mutation for token in ("select", "selected", "selection", "choose", "chosen"))
        or any(token in lifecycle for token in ("select", "selected", "selection"))
        or lifecycle in {
            "selected",
            "selection_committed",
            "ready_to_confirm",
            "ready_to_send",
            "ready_to_submit",
            "awaiting_confirmation",
        }
    )


def _runtime_effect_for_same_page_click(
    *,
    source: str,
    target: str,
    intent: str,
    source_step_key: str = "",
    next_step_key: str = "",
    render_role: str = "",
    next_render_role: str = "",
    runtime_observations: Sequence[Dict[str, Any]] | None = None,
    schema_entities: Sequence[Dict[str, Any]] | None = None,
    route_contract: Dict[str, Any] | None = None,
    surface_contract: Dict[str, Any] | None = None,
    next_surface_contract: Dict[str, Any] | None = None,
) -> Tuple[str, Dict[str, Any]]:
    source = _base_filename(source)
    target = _base_filename(target)
    text = str(intent or "")
    label = _quoted_or_clicked_label(text)
    if not source or source != target:
        return "", {}
    surface_contract = surface_contract if isinstance(surface_contract, dict) else {}
    next_surface_contract = next_surface_contract if isinstance(next_surface_contract, dict) else {}
    current_surface_scope = str(surface_contract.get("surface_scope") or "")
    next_surface_scope = str(next_surface_contract.get("surface_scope") or "")
    if surface_contract and _surface_contract_has_explicit_close_lifecycle(next_surface_contract):
        state_ref = str(
            surface_contract.get("state_ref")
            or _selection_state_ref(source, current_surface_scope or "runtime_overlay_stack")
        )
        return "close_overlay", {
            "effect": "close_runtime_overlay",
            "overlay_action": "dismiss",
            "overlay_kind": str(surface_contract.get("surface_kind") or "runtime_overlay"),
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "consume_current_surface_from_reference_snapshot_lifecycle",
            "effect_admission_status": "accepted",
            "effect_family": "overlay_lifecycle",
            "expected_state_diff": {f"{state_ref}.visible": False},
            "effect_evidence": [
                "same_page_click",
                "current_reference_state_snapshot_surface",
                "next_reference_state_snapshot_explicit_close_lifecycle",
            ],
        }
    same_surface_scope = bool(
        surface_contract
        and next_surface_contract
        and current_surface_scope
        and current_surface_scope == next_surface_scope
    )
    if (
        same_surface_scope
        and _surface_contract_has_progressive_selection_lifecycle(next_surface_contract)
    ):
        state_ref = str(
            surface_contract.get("state_ref")
            or next_surface_contract.get("state_ref")
            or _selection_state_ref(source, current_surface_scope)
        )
        return "select_overlay_option", {
            "effect": "select_overlay_option",
            "overlay_action": "select_and_preserve",
            "overlay_kind": str(
                next_surface_contract.get("surface_kind")
                or surface_contract.get("surface_kind")
                or "runtime_overlay"
            ),
            "state_ref": state_ref,
            "selected_label": label,
            "overlay_persists_after_action": True,
            "next_surface_lifecycle": str(next_surface_contract.get("lifecycle") or ""),
            "mutation_policy": "commit_observed_overlay_option_and_preserve_progressive_surface",
            "effect_admission_status": "accepted",
            "effect_family": "overlay_lifecycle",
            "expected_state_diff": {
                f"{state_ref}.selected_label": label,
                f"{state_ref}.visible": True,
            },
            "effect_evidence": [
                "same_page_click",
                "current_reference_state_snapshot_surface",
                "next_reference_state_snapshot_progressive_selection",
            ],
        }
    next_surface_is_new = bool(
        next_surface_contract
        and (
            not surface_contract
            or not current_surface_scope
            or not next_surface_scope
            or current_surface_scope != next_surface_scope
        )
    )
    if next_surface_is_new:
        state_ref = str(
            next_surface_contract.get("state_ref")
            or _selection_state_ref(source, next_surface_scope or "runtime_overlay_stack")
        )
        opened_options = [
            str(item)
            for item in next_surface_contract.get("options", [])
            if str(item or "").strip()
        ] if isinstance(next_surface_contract.get("options"), list) else []
        return "open_overlay", {
            "effect": "open_runtime_overlay",
            "overlay_kind": str(next_surface_contract.get("surface_kind") or "runtime_overlay"),
            "state_ref": state_ref,
            "selected_label": label,
            "opened_surface_options": opened_options,
            "mutation_policy": "open_observed_reference_state_surface_without_committing_selection",
            "effect_admission_status": "accepted",
            "effect_family": "open_runtime_overlay",
            "effect_evidence": [
                "same_page_click",
                "next_reference_state_snapshot_surface",
            ],
        }
    current_options = [
        str(item).strip()
        for item in surface_contract.get("options", [])
        if str(item or "").strip()
    ] if isinstance(surface_contract.get("options"), list) else []
    normalized_label = re.sub(r"\s+", "", label).lower()
    option_match = bool(
        normalized_label
        and any(
            normalized_label == re.sub(r"\s+", "", option).lower()
            for option in current_options
        )
    )
    if surface_contract and option_match:
        state_ref = str(
            surface_contract.get("state_ref")
            or _selection_state_ref(source, current_surface_scope or "runtime_overlay_stack")
        )
        return "select_overlay_option", {
            "effect": "select_overlay_option",
            "overlay_action": "select_and_consume",
            "overlay_kind": str(surface_contract.get("surface_kind") or "runtime_overlay"),
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "commit_observed_overlay_option_and_consume_surface",
            "effect_admission_status": "accepted",
            "effect_family": "overlay_lifecycle",
            "expected_state_diff": {
                f"{state_ref}.selected_label": label,
                f"{state_ref}.visible": False,
            },
            "effect_evidence": [
                "same_page_click",
                "current_reference_state_snapshot_surface",
                "selected_label_matches_observed_surface_option",
            ],
        }
    if _is_direct_time_shortcut_commit_intent(text, label):
        state_ref = _selection_state_ref(source, "visible_time_shortcut_selection")
        selected_label = label or "time_shortcut"
        return "activate_inline_state", {
            "effect": "commit_visible_state",
            "state_ref": state_ref,
            "selected_label": selected_label,
            "mutation_policy": "commit_direct_time_shortcut_without_overlay",
            "effect_admission_status": "accepted",
            "effect_family": "visible_field_commit",
            "domain_intent": "time_shortcut_commit",
            "expected_state_diff": {
                f"{state_ref}.selected_label": selected_label,
            },
            "effect_evidence": ["same_page_click", "direct_time_shortcut_commit"],
        }
    if _is_year_month_picker_intent(text):
        state_ref = _selection_state_ref(source, "selected_year_month_picker_state")
        return "open_overlay", {
            "effect": "open_runtime_overlay",
            "overlay_kind": "year_month_picker",
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "open_wheel_picker_without_committing_selection",
            "template_id": "wheel_picker",
            "trigger_element_id": "btn-select-time",
            "effect_admission_status": "accepted",
            "effect_family": "open_runtime_overlay",
            "effect_evidence": ["same_page_click", "year_month_picker_intent"],
        }
    if _is_time_picker_intent(text):
        state_ref = _selection_state_ref(source, "selected_departure_time_picker_state")
        return "open_overlay", {
            "effect": "open_runtime_overlay",
            "overlay_kind": "departure_time_picker",
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "open_wheel_time_picker_without_committing_selection",
            "template_id": "wheel_time_picker",
            "effect_admission_status": "accepted",
            "effect_family": "open_runtime_overlay",
            "effect_evidence": ["same_page_click", "time_picker_intent"],
        }
    if _contains_any(text, ("加入购物车", "加购", "add to cart")):
        state_ref = _selection_state_ref(source, "cart_accumulator")
        return "update_cart_quantity", {
            "effect": "update_cart_quantity",
            "state_ref": state_ref,
            "selected_label": label,
            "delta": 1,
            "mutation_policy": "commit_overlay_selection_to_cart_state",
            "visible_initial_prefill_allowed": False,
            "effect_admission_status": "accepted",
            "effect_family": "cart_mutation",
            "domain_intent": "cart_quantity",
            "expected_state_diff": {
                f"{state_ref}.delta": 1,
                f"{state_ref}.last_selected_label": label or "加入购物车",
            },
            "effect_evidence": ["same_page_click", "overlay_cart_commit_intent"],
        }
    if _is_visible_notice_dismiss_intent(text, label, surface_contract=surface_contract):
        state_ref = _selection_state_ref(source, "runtime_overlay_stack")
        return "close_overlay", {
            "effect": "close_runtime_overlay",
            "overlay_action": "dismiss",
            "overlay_kind": "notice_banner",
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "dismiss_visible_notice_or_prompt",
            "effect_admission_status": "accepted",
            "effect_family": "overlay_lifecycle",
            "expected_state_diff": {f"{state_ref}.visible": False},
            "effect_evidence": ["same_page_click", "visible_notice_dismiss_intent"],
        }
    if _contains_any(text, ("关闭", "取消", "dismiss", "close")) or label.lower() in {"x", "×", "关闭"}:
        state_ref = _selection_state_ref(source, "runtime_overlay_stack")
        return "close_overlay", {
            "effect": "close_runtime_overlay",
            "overlay_action": "dismiss",
            "overlay_kind": "action_sheet",
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "dismiss_active_runtime_overlay",
            "effect_admission_status": "accepted",
            "effect_family": "overlay_lifecycle",
            "expected_state_diff": {f"{state_ref}.visible": False},
            "effect_evidence": ["same_page_click", "overlay_close_intent"],
        }
    overlay_cta_button_intent = (
        label
        and _contains_any(text, ("弹窗", "面板", "浮层", "sheet", "modal", "overlay"))
        and _contains_any(text, ("按钮", "button", "cta"))
    )
    if (
        _contains_any(text, ("弹窗", "面板", "浮层", "sheet", "modal", "overlay"))
        and _contains_any(text, ("确定", "允许", "确认", "完成", "confirm", "allow", "ok"))
    ) or label in {"确定", "允许", "确认", "完成", "OK", "ok"} or overlay_cta_button_intent:
        state_ref = _selection_state_ref(source, "runtime_overlay_stack")
        return "confirm_overlay", {
            "effect": "confirm_runtime_overlay",
            "overlay_action": "confirm",
            "overlay_kind": "action_sheet",
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "confirm_active_runtime_overlay",
            "effect_admission_status": "accepted",
            "effect_family": "overlay_lifecycle",
            "expected_state_diff": {
                f"{state_ref}.confirmed": True,
                f"{state_ref}.confirmed_label": label or "confirm",
            },
            "effect_evidence": [
                "same_page_click",
                "overlay_cta_button_intent" if overlay_cta_button_intent else "overlay_confirm_intent",
            ],
        }
    admission = admit_same_page_click_effect(
        ActionEvidence(
            source_filename=source,
            target_filename=target,
            operation="click",
            intent_text=text,
            source_step_key=source_step_key,
            next_step_key=next_step_key,
            render_role=render_role,
            next_render_role=next_render_role,
            runtime_observations=list(runtime_observations or []),
            schema_entities=list(schema_entities or []),
            route_contract=dict(route_contract or {}),
            surface_contract=dict(surface_contract or {}),
        )
    )
    if admission.admission_status == "accepted":
        return admission.reducer, admission.state_effect
    if admission.weak_candidates or admission.unresolved_reason:
        return "", {
            "effect_admission_status": admission.admission_status,
            "weak_effect_candidates": admission.weak_candidates,
            "rejected_effect_candidates": admission.rejected_candidates,
            "unresolved_reason": admission.unresolved_reason,
        }
    return "", {}


def _next_canonical_step_key(step_key: str) -> str:
    step_key = canonical_step_key(step_key)
    match = re.match(r"^(.*:step)(\d+)$", step_key)
    if not match:
        return ""
    return f"{match.group(1)}{int(match.group(2)) + 1}"


def _trace_step_order_from_step_key(step_key: str) -> Tuple[str, int | None]:
    text = canonical_step_key(step_key)
    match = re.match(r"(?P<trace>.+):step(?P<idx>\d+)$", text)
    if match:
        return match.group("trace"), int(match.group("idx"))
    match = re.match(r"(?P<trace>.+):(?P<idx>\d+)$", text)
    if match:
        return match.group("trace"), int(match.group("idx"))
    return text.split(":", 1)[0], None


def _input_field_role(filename: str, intent: str) -> str:
    filename_lowered = _base_filename(filename).lower()
    intent_lowered = str(intent or "").lower()
    if any(marker in intent_lowered for marker in (
        "联系人",
        "收货人",
        "手机号",
        "手机",
        "电话",
        "姓名",
        "门牌",
        "房间",
        "楼层",
        "contact",
        "phone",
        "name",
        "room",
        "floor",
    )):
        return "form_value"
    if any(marker in intent_lowered for marker in (
        "taxi-location",
        "pickup",
        "dropoff",
        "浦东机场",
        "上海中心",
        "用车",
        "上车",
        "下车",
    )):
        return "location_lookup"
    if any(marker in intent_lowered for marker in ("address", "地址", "城市", "city", "poi")):
        return "address_lookup"
    explicit_lookup_surface = (
        any(marker in filename_lowered for marker in ("search", "lookup", "搜索", "检索"))
        and any(
            marker in filename_lowered
            for marker in ("address", "location", "poi", "地址", "位置", "城市")
        )
    )
    if explicit_lookup_surface:
        return "address_lookup"
    if _is_search_surface(filename, intent):
        return "business_query"
    return "form_value"


def _should_commit_active_draft_from_trace(source: str, target: str, draft: Dict[str, Any]) -> bool:
    fields = draft.get("fields", []) if isinstance(draft, dict) else []
    source = _base_filename(source)
    target = _base_filename(target)
    return bool(fields and source and target and source != target)


def _is_collection_scope(scope: str, filename: str = "") -> bool:
    lowered = f"{scope} {filename}".lower()
    return any(marker in lowered for marker in COLLECTION_MARKERS)


def _is_record_collection_scope(scope: str) -> bool:
    lowered = str(scope or "").lower()
    positive = (
        "address_list",
        "address.records",
        "invoice_title.list",
        "records",
        "entries",
        "list.contents",
        "saved",
    )
    negative = (
        "form",
        "field",
        "input",
        "keyboard",
        "header",
        "navigation",
        "bottom",
        "feature",
        "favorites",
        "modal",
        "validation",
        "layout",
        "screen",
        "selection",
        "active_tab",
        "primary_action",
        "save_state",
    )
    return any(marker in lowered for marker in positive) and not any(marker in lowered for marker in negative)


def _min_count_from_hints(hints: Sequence[Any], raw_evidence: str = "") -> int:
    text = " ".join(str(item or "") for item in hints) + " " + str(raw_evidence or "")
    values: List[int] = []
    for match in re.findall(r"(?:at least|至少|不少于)\s*(\d+)", text, flags=re.IGNORECASE):
        values.append(int(match))
    for match in re.findall(r"(\d+)\s*(?:items?|cards?|entries?|条|个)", text, flags=re.IGNORECASE):
        values.append(int(match))
    return max(values) if values else 0


def _is_baseline_collection_observation(lifecycle: str, mutation: str) -> bool:
    lifecycle_text = str(lifecycle or "").strip().lower()
    mutation_text = str(mutation or "").strip().lower()
    if lifecycle_text.startswith(("post_", "after_", "during_")):
        return False
    if lifecycle_text in {"initial_state", "screen_loaded", "rendered", "visible"}:
        return True
    if mutation_text in {"state_present", "render"} and lifecycle_text not in {"runtime_only", "post_commit"}:
        return True
    return False


def _value_hints(obligation: Dict[str, Any]) -> List[str]:
    return [str(item).strip() for item in obligation.get("value_hints", []) if str(item or "").strip()] if isinstance(obligation.get("value_hints"), list) else []


def _state_obligations_by_step(items: Sequence[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        try:
            grouped[int(item.get("step_index"))].append(item)
        except Exception:
            continue
    return dict(grouped)


def _extract_step_states(
    trace_id: str,
    steps: Sequence[Dict[str, Any]],
    runtime_obligations: Sequence[Dict[str, Any]],
    filename_overrides: Dict[str, str] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    by_step = _state_obligations_by_step(runtime_obligations)
    snapshots: List[Dict[str, Any]] = []
    collection_facts: Dict[str, Dict[str, Any]] = {}

    def build_state_items(idx: int, filename: str) -> List[Dict[str, Any]]:
        state_items: List[Dict[str, Any]] = []
        for obligation in by_step.get(idx, []):
            scope = str(obligation.get("state_scope") or "").strip()
            hints = _value_hints(obligation)
            min_count = _min_count_from_hints(hints, str(obligation.get("raw_evidence") or ""))
            state_item = {
                "state_scope": scope,
                "lifecycle": str(obligation.get("lifecycle") or ""),
                "mutation": str(obligation.get("mutation") or ""),
                "materialized_page_id": str(obligation.get("materialized_page_id") or ""),
                "value_hints": hints,
                "terminal_constraints": obligation.get("terminal_constraints", []) if isinstance(obligation.get("terminal_constraints"), list) else [],
                "evidence_steps": obligation.get("evidence_steps", []) if isinstance(obligation.get("evidence_steps"), list) else [],
            }
            if min_count:
                state_item["min_observed_count"] = min_count
            state_items.append({key: value for key, value in state_item.items() if value not in ("", [], {})})
            if _is_collection_scope(scope, filename):
                key = f"{filename}::{scope}"
                fact = collection_facts.setdefault(key, {
                    "filename": filename,
                    "state_scope": scope,
                    "min_initial_count": 0,
                    "evidence_steps": [],
                })
                is_baseline = _is_baseline_collection_observation(
                    str(obligation.get("lifecycle") or ""),
                    str(obligation.get("mutation") or ""),
                )
                if is_baseline:
                    fact["baseline_evidence_present"] = True
                    fact["min_initial_count"] = max(int(fact.get("min_initial_count") or 0), min_count)
                    if not min_count:
                        fact["preserve_unknown_initial_records"] = True
                fact["evidence_steps"] = _dedupe(list(fact.get("evidence_steps", [])) + list(state_item.get("evidence_steps", [])), limit=24)
        return state_items

    last_step: Dict[str, Any] | None = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("execution_policy") or "") in {
            "normalize_initial_app_open",
            "external_system_observation_skip",
        }:
            continue
        idx = int(step.get("step_index") or 0)
        last_step = step
        pre = step.get("pre_state", {}) if isinstance(step.get("pre_state"), dict) else {}
        post = step.get("expected_post_state", {}) if isinstance(step.get("expected_post_state"), dict) else {}
        filename = _canonical_state_filename(post, filename_overrides) or _canonical_state_filename(pre, filename_overrides)
        state_items = build_state_items(idx, filename)
        snapshots.append({
            "step_key": _step_key(trace_id, idx),
            "step_index": idx,
            "filename": filename,
            "observation_key": str(post.get("observation_key") or pre.get("observation_key") or ""),
            "state_items": state_items,
        })
    if last_step is not None:
        last_idx = int(last_step.get("step_index") or 0)
        terminal_post = (
            last_step.get("expected_post_state", {})
            if isinstance(last_step.get("expected_post_state"), dict)
            else {}
        )
        terminal_observation_key = str(terminal_post.get("observation_key") or "")
        terminal_step_key = canonical_step_key(terminal_observation_key)
        terminal_match = re.match(r"^.+:step(\d+)$", terminal_step_key)
        terminal_idx = int(terminal_match.group(1)) if terminal_match else 0
        if terminal_idx > last_idx and by_step.get(terminal_idx):
            terminal_filename = _canonical_state_filename(terminal_post, filename_overrides)
            snapshots.append({
                "step_key": _step_key(trace_id, terminal_idx),
                "step_index": terminal_idx,
                "filename": terminal_filename,
                "observation_key": terminal_observation_key,
                "observation_role": "terminal_observation",
                "state_items": build_state_items(terminal_idx, terminal_filename),
            })
    return snapshots, collection_facts


def _draft_ref_for(filename: str, trace_id: str) -> str:
    seed = re.sub(r"[^A-Za-z0-9_]+", "_", f"{filename}:{trace_id}").strip("_")
    return f"draft::{seed[:96]}"


def _build_transitions(
    trace_id: str,
    steps: Sequence[Dict[str, Any]],
    filename_overrides: Dict[str, str] | None = None,
    step_boundary_contracts: Dict[str, Any] | None = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    transitions: List[Dict[str, Any]] = []
    input_sessions: List[Dict[str, Any]] = []
    active_draft_by_filename: Dict[str, Dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        idx = int(step.get("step_index") or 0)
        pre = step.get("pre_state", {}) if isinstance(step.get("pre_state"), dict) else {}
        post = step.get("expected_post_state", {}) if isinstance(step.get("expected_post_state"), dict) else {}
        action = step.get("action", {}) if isinstance(step.get("action"), dict) else {}
        operation = str(action.get("operation") or "").strip()
        source = _canonical_state_filename(pre, filename_overrides)
        target = _canonical_state_filename(post, filename_overrides) or source
        if operation == "open_app":
            source = target
        input_text = str(action.get("input_text") or "").strip()
        intent = str(action.get("intent") or action.get("action_text") or "").strip()
        transition: Dict[str, Any] = {
            "step_key": _step_key(trace_id, idx),
            "step_index": idx,
            "source_filename": source,
            "target_filename": target,
            "operation": operation,
            "command": _command_for_step(step),
            "operation_intent": intent,
            "l0_role": "execution_anchor" if operation in {"click", "fill", "select"} else "viewport_state" if operation in {"swipe", "scroll"} else "normalization" if operation == "open_app" else "terminal",
            "pre_observation_key": str(pre.get("observation_key") or ""),
            "post_observation_key": str(post.get("observation_key") or ""),
        }
        boundary_contract = {}
        if isinstance(step_boundary_contracts, dict):
            boundary_contract = (
                step_boundary_contracts.get("actions_by_id", {}).get(
                    _step_key(trace_id, idx), {}
                )
                if isinstance(step_boundary_contracts.get("actions_by_id"), dict)
                else {}
            )
        if isinstance(boundary_contract, dict) and boundary_contract:
            transition["boundary_contract"] = dict(boundary_contract)
            transition["page_boundary_authority"] = dict(boundary_contract)
            if boundary_contract.get("boundary_decision") == "runtime_projection":
                canonical_filename = _base_filename(
                    boundary_contract.get("canonical_runtime_filename")
                )
                if canonical_filename:
                    transition["source_filename"] = canonical_filename
                    transition["target_filename"] = canonical_filename
                    source = canonical_filename
                    target = canonical_filename
                if boundary_contract.get("reducer"):
                    transition["reducer"] = boundary_contract["reducer"]
                transition["state_refs"] = list(
                    boundary_contract.get("state_refs", [])
                )
                transition["expected_state_diff"] = dict(
                    boundary_contract.get("expected_state_diff", {})
                )
        if input_text:
            transition["input_text"] = input_text
        if operation == "fill":
            draft = active_draft_by_filename.setdefault(source, {
                "draft_ref": _draft_ref_for(source, trace_id),
                "source_filename": source,
                "parameterized": True,
                "fields": [],
                "started_at_step": _step_key(trace_id, idx),
            })
            field_role = _input_field_role(source, intent)
            field = {
                "step_key": _step_key(trace_id, idx),
                "field_role": field_role,
                "value": input_text,
                "source": "trace_action_input_text",
            }
            draft["fields"].append(field)
            transition["state_effect"] = {
                "effect": "update_draft",
                "draft_ref": draft["draft_ref"],
                "field_role": field_role,
                "value": input_text,
                "visible_initial_prefill_allowed": False,
            }
            if source != target:
                transition["state_effect"]["commit_kind"] = "typed_input_route_preview"
                transition["state_effect"]["preview_target_filename"] = target
                active_draft_by_filename.pop(source, None)
                if not (
                    field_role in SEARCHLIKE_FIELD_ROLES
                    and _is_search_results_filename(target)
                ):
                    draft["source_filename"] = target
                    active_draft_by_filename[target] = draft
        elif operation == "click":
            draft = active_draft_by_filename.get(source)
            if draft and _should_commit_active_draft_from_trace(source, target, draft):
                transition["state_effect"] = {
                    "effect": "commit_draft",
                    "draft_ref": draft["draft_ref"],
                    "target_filename": target,
                    "collection_merge_policy": "preserve_existing_then_append_or_upsert",
                    "committed_fields": list(draft.get("fields", [])),
                }
                input_sessions.append(dict(draft, committed_by_step=_step_key(trace_id, idx), target_filename=target))
                active_draft_by_filename.pop(source, None)
            elif draft and source != target:
                active_draft_by_filename.pop(source, None)
        transitions.append({key: value for key, value in transition.items() if value not in ("", [], {})})
    _promote_search_activation_transitions(transitions)
    input_sessions.extend(dict(value, committed=False) for value in active_draft_by_filename.values())
    return transitions, input_sessions


def _promote_search_activation_transitions(transitions: List[Dict[str, Any]]) -> None:
    for index, transition in enumerate(transitions[:-1]):
        if not isinstance(transition, dict):
            continue
        operation = str(transition.get("operation") or "").strip().lower()
        if operation not in {"click", "tap"}:
            continue
        source = _base_filename(transition.get("source_filename"))
        target = _base_filename(transition.get("target_filename"))
        if not source or source != target:
            continue
        intent = " ".join(
            str(transition.get(key) or "")
            for key in ("operation_intent", "command")
        )
        if not _is_search_activation_intent(intent):
            continue
        next_transition = transitions[index + 1]
        if not isinstance(next_transition, dict):
            continue
        next_source = _base_filename(next_transition.get("source_filename"))
        next_target = _base_filename(next_transition.get("target_filename"))
        if (
            str(next_transition.get("operation") or "").strip().lower() != "fill"
            or next_source != source
            or not _is_search_entry_filename(next_target)
            or next_target == source
        ):
            continue
        state_ref = _state_ref(f"{next_target}::query_entry")
        transition["target_filename"] = next_target
        transition["reducer"] = "open_search_entry"
        transition["query_transition_kind"] = "query_entry"
        transition["state_effect"] = {
            "effect": "open_search_entry",
            "state_ref": state_ref,
            "target_filename": next_target,
            "query_transition_kind": "query_entry",
            "visible_initial_prefill_allowed": False,
            "mutation_policy": "route_to_search_entry_without_committing_query",
            "source": "page_environment_search_activation_promotion",
            "promoted_from_next_fill_step": str(next_transition.get("step_key") or ""),
        }


def _build_pages(
    steps: Sequence[Dict[str, Any]],
    pagegen_by_page: Dict[str, List[Dict[str, Any]]],
    filename_overrides: Dict[str, str] | None = None,
) -> List[Dict[str, Any]]:
    by_filename: Dict[str, Dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("execution_policy") or "") in {
            "normalize_initial_app_open",
            "external_system_observation_skip",
        }:
            continue
        action = step.get("action", {}) if isinstance(step.get("action"), dict) else {}
        operation = str(action.get("operation") or "").strip()
        for state_key in ("pre_state", "expected_post_state"):
            if operation == "open_app" and state_key == "pre_state":
                continue
            state = step.get(state_key, {}) if isinstance(step.get(state_key), dict) else {}
            filename = _canonical_state_filename(state, filename_overrides)
            if not filename:
                continue
            item = by_filename.setdefault(filename, {
                "filename": filename,
                "evidence_steps": [],
                "render_obligations": [],
            })
            item["evidence_steps"].append(_step_key(str(step.get("trace_id") or ""), step.get("step_index")))
    for filename, page in by_filename.items():
        obligations = pagegen_by_page.get(filename, [])
        if obligations:
            page["render_obligations"] = [
                {
                    "operation": item.get("operation", ""),
                    "expected_filename": item.get("expected_filename", ""),
                    "input_text": item.get("input_text", ""),
                    "step_key": _step_key(str(item.get("trace_id") or ""), item.get("step_index")),
                }
                for item in obligations[:24]
            ]
    for page in by_filename.values():
        page["evidence_steps"] = _dedupe(page.get("evidence_steps", []), limit=64)
    return list(by_filename.values())


PAGE_ENVIRONMENT_SPEC_KEYS = (
    "page_id",
    "initial_state",
    "state_schema",
    "content_seed",
    "components",
    "render_projections",
    "transitions",
    "parameterized_inputs",
    "query_projections",
    "oracles",
    "conflict_resolutions",
)


CONFLICT_DECISION_BUCKETS = (
    "state_variants",
    "runtime_only",
    "section_render",
    "visual_samples",
    "dropped",
)


def _state_ref(scope: str) -> str:
    seed = re.sub(r"[^A-Za-z0-9_]+", "_", str(scope or "state").strip()).strip("_").lower()
    return f"state::{seed or 'scope'}"


def _transition_reducer(operation: str, effect: Dict[str, Any]) -> str:
    effect_name = str(effect.get("effect") or "")
    effect_reducer = str(effect.get("reducer") or "")
    if effect_reducer:
        return effect_reducer
    if effect_name in {
        "navigate_route",
        "open_search_entry",
        "project_suggestions",
        "open_runtime_overlay",
        "update_selection",
        "commit_selection",
        "hydrate_selected_record",
        "update_cart_quantity",
    }:
        if effect_name == "open_runtime_overlay":
            overlay_kind = str(effect.get("overlay_kind") or "")
            if overlay_kind == "product_option_modal":
                return "open_option_modal"
            return "open_overlay"
        if effect_name == "update_selection":
            selection_kind = str(effect.get("selection_kind") or "")
            if selection_kind == "tab":
                return "switch_tab"
            return "select_filter_option"
        if effect_name == "commit_selection":
            return "apply_filter"
        if effect_name == "navigate_route":
            return "navigate_route"
        return effect_name
    if operation == "fill":
        role = str(effect.get("field_role") or "")
        qkind = str(effect.get("query_transition_kind") or "")
        if qkind == "suggestion_projection":
            return "project_suggestions"
        if role in SEARCHLIKE_FIELD_ROLES:
            return "type_search"
        return "fill_form"
    if effect_name == "commit_draft":
        fields = effect.get("committed_fields", []) if isinstance(effect.get("committed_fields"), list) else []
        if any(isinstance(field, dict) and field.get("field_role") == "business_query" for field in fields):
            return "commit_query"
        return "submit_form"
    if operation in {"scroll", "swipe"}:
        return "scroll_viewport"
    if operation == "click":
        return "activate_control"
    if operation == "select":
        return "select_option"
    return operation or "observe"


def _new_page_environment(filename: str) -> Dict[str, Any]:
    return {
        "format": "page_environment_spec",
        "schema_revision": 2,
        "page_id": filename,
        "filename": filename,
        "initial_state": {
            "collections": {},
            "drafts": {},
            "query": {"default": ""},
            "overlays": {},
            "selections": {},
        },
        "state_schema": {
            "collections": [],
            "drafts": [],
            "parameters": [],
        },
        "content_seed": {
            "seed_policy": "observed_initial_state_only",
            "collections": [],
            "runtime_items_excluded_from_initial": True,
        },
        "components": [],
        "render_projections": [],
        "routes": [],
        "ui_states": [],
        "queryParams": [],
        "scrollContainers": [],
        "transitions": [],
        "parameterized_inputs": [],
        "query_projections": [],
        "oracles": [],
        "layout_content_conflict_policy": {
            "fixed_slot": "one_default_candidate_per_slot",
            "appendable_list": "append_only_with_same_card_template",
            "runtime_only_item": "render_only_after_matching_transition",
            "visual_sample": "style_reference_not_required_render",
            "non_l0_conflict": "stable_drop_before_pagegen",
            "orphan_text_block": "forbidden",
        },
        "provenance": {
            "source_trace_ids": [],
            "evidence_steps": [],
        },
    }


def _ensure_environment_primitives(page_env: Dict[str, Any]) -> None:
    page_env.setdefault("routes", [])
    page_env.setdefault("ui_states", [])
    page_env.setdefault("queryParams", [])
    scroll_containers = page_env.setdefault("scrollContainers", [])
    if not isinstance(scroll_containers, list):
        scroll_containers = []
        page_env["scrollContainers"] = scroll_containers
    transitions = page_env.get("transitions", [])
    has_scroll_transition = any(
        isinstance(transition, dict)
        and str(transition.get("operation") or transition.get("reducer") or "").lower() in {"scroll", "swipe"}
        for transition in transitions if isinstance(transitions, list)
    )
    if has_scroll_transition and not scroll_containers:
        scroll_containers.append(
            {
                "container_id": f"{_safe_id(page_env.get('page_id') or page_env.get('filename') or 'page')}::main_scroll",
                "source_page_id": str(page_env.get("page_id") or page_env.get("filename") or ""),
                "axis": "y",
                "identity_effect": "same_route_offset_only",
            }
        )
    if scroll_containers and isinstance(transitions, list):
        default_container = str(scroll_containers[0].get("container_id") or "")
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            if str(transition.get("operation") or transition.get("reducer") or "").lower() not in {"scroll", "swipe"}:
                continue
            transition.setdefault("scrollContainer", default_container)
            transition.setdefault("identity_effect", "same_route_offset_only")


def _ensure_collection_schema(page_env: Dict[str, Any], scope: str) -> None:
    state_ref = _state_ref(scope)
    schemas = page_env.setdefault("state_schema", {}).setdefault("collections", [])
    if not any(isinstance(item, dict) and item.get("state_ref") == state_ref for item in schemas):
        schemas.append({
            "state_ref": state_ref,
            "state_scope": scope,
            "record_identity": "canonical_record_identity_required",
            "merge_policy": "preserve_existing_then_append_or_upsert",
        })


def _add_unique_dict(items: List[Dict[str, Any]], item: Dict[str, Any], key_fields: Sequence[str]) -> None:
    key = tuple(str(item.get(field, "")) for field in key_fields)
    for existing in items:
        if not isinstance(existing, dict):
            continue
        if tuple(str(existing.get(field, "")) for field in key_fields) == key:
            for merge_key in ("evidence_steps", "observed_values", "binds_to"):
                if merge_key in item:
                    existing[merge_key] = _dedupe(
                        list(existing.get(merge_key, []) or []) + list(item.get(merge_key, []) or []),
                        limit=32,
                    )
            for merge_key in ("interaction_contract", "expected_interaction", "dom_contract"):
                if merge_key in item and item.get(merge_key):
                    if isinstance(existing.get(merge_key), dict) and isinstance(item.get(merge_key), dict):
                        existing[merge_key].update(item[merge_key])
                    else:
                        existing[merge_key] = item[merge_key]
            for merge_key in (
                "render_phase",
                "host_availability_phase",
                "initial_dom_eligible",
                "initial_visibility_policy",
                "runtime_visibility_policy",
                "state_value_phase",
                "content_projection_phase",
                "initial_value_source",
                "post_action_value",
                "forbid_initial_value_from_input_text",
            ):
                if item.get(merge_key) not in (None, "", [], {}):
                    existing[merge_key] = item[merge_key]
            if isinstance(item.get("interaction_contract"), dict) and isinstance(existing.get("interaction_contract"), dict):
                binding = item["interaction_contract"].get("initial_value_binding")
                if binding:
                    existing["interaction_contract"]["initial_value_binding"] = binding
            if isinstance(item.get("expected_interaction"), dict) and isinstance(existing.get("expected_interaction"), dict):
                binding = item["expected_interaction"].get("initial_value_binding")
                if binding:
                    existing["expected_interaction"]["initial_value_binding"] = binding
            if item.get("interaction_required") is True:
                existing["interaction_required"] = True
            return
    items.append(item)


def _optional_json(path: Path) -> Any:
    if not path.is_file():
        return {}
    try:
        return _read_json(path)
    except Exception:
        return {}


def _clip_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _safe_id(value: Any, limit: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9_\-:\u4e00-\u9fff]+", "_", str(value or "")).strip("_")
    return (text or "unknown")[:limit]


def _first_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _page_signature_maps(site_model) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    payload = site_model.page_signatures
    by_id: Dict[str, Dict[str, Any]] = {}
    by_filename: Dict[str, Dict[str, Any]] = {}
    by_general: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if not isinstance(payload, dict):
        return by_id, by_filename, {}

    pages_by_id = payload.get("pages_by_id", {})
    if isinstance(pages_by_id, dict):
        pages = [page for page in pages_by_id.values() if isinstance(page, dict)]
    else:
        pages = [page for page in payload.get("pages", []) if isinstance(page, dict)] if isinstance(payload.get("pages"), list) else []
    for page in pages:
        page_id = str(page.get("page_id") or "")
        filename = _base_filename(page.get("filename"))
        if page_id:
            by_id[page_id] = page
        if filename:
            by_filename[filename] = page
        general_page_id = str(page.get("general_page_id") or "")
        if general_page_id:
            by_general[general_page_id].append(page)
    return by_id, by_filename, dict(by_general)


def _instance_groups_by_general(site_model) -> Dict[str, List[Dict[str, Any]]]:
    payload = site_model.instance_plan
    if not isinstance(payload, dict):
        return {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    for general_page_id, data in payload.items():
        if not isinstance(data, dict):
            continue
        groups = data.get("instance_groups", [])
        if isinstance(groups, list):
            result[str(general_page_id)] = [group for group in groups if isinstance(group, dict)]
    return result


def _instance_group_for_page(
    page: Dict[str, Any],
    groups_by_general: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    general_page_id = str(page.get("general_page_id") or "")
    materialized_key = str(page.get("materialized_page_key") or "")
    for group in groups_by_general.get(general_page_id, []):
        if str(group.get("instance_key") or "") == materialized_key:
            return group
    return {}


def _surface_from_candidate(candidate: Dict[str, Any], contract: Dict[str, Any] | None = None) -> str:
    contract = contract or {}
    text = " ".join(
        str(value or "")
        for value in (
            candidate.get("element_name"),
            candidate.get("display_text"),
            candidate.get("reason"),
            contract.get("action_semantic"),
            _first_dict(contract.get("route_binding")).get("source"),
        )
    ).lower()
    if any(marker in text for marker in ("bottom", "tab", "底部", "导航栏", "我的", "订单", "神券")):
        return "bottom_navigation"
    if any(marker in text for marker in ("header", "地址", "定位", "顶部", "location")):
        return "header"
    if any(marker in text for marker in ("search", "搜索", "query", "猜你想搜", "历史搜索")):
        return "search_surface"
    if any(marker in text for marker in ("cart", "购物车", "结算", "支付", "submit")):
        return "checkout_or_submit"
    if any(marker in text for marker in ("merchant", "store", "店铺", "商家", "商品", "activity", "活动")):
        return "collection_item"
    return "unknown_surface"


def _candidate_l0_role(contract: Dict[str, Any]) -> str:
    action = str(contract.get("action_semantic") or "")
    sdk_effect = _first_dict(contract.get("sdk_effect"))
    if action or str(sdk_effect.get("effect") or "") in {"navigate", "state_mutation"}:
        return "execution_anchor"
    return "section_render"


def _group_conflict_records(conflicts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in conflicts:
        if not isinstance(item, dict):
            continue
        candidate = _first_dict(item.get("candidate"))
        source_page_id = str(candidate.get("source_page_id") or "")
        element_name = str(candidate.get("element_name") or "")
        quarantine_reason = str(item.get("quarantine_reason") or "task_interaction_conflict")
        if not source_page_id:
            continue
        grouped[(source_page_id, element_name, quarantine_reason)].append(item)
    return [
        {
            "source_page_id": source_page_id,
            "element_name": element_name,
            "quarantine_reason": quarantine_reason,
            "items": items,
        }
        for (source_page_id, element_name, quarantine_reason), items in grouped.items()
    ]


def _lifecycle_steps_by_cluster(site_model) -> Dict[str, List[Dict[str, Any]]]:
    payload = site_model.content_lifecycle_plan
    clusters = payload.get("clusters", {}) if isinstance(payload, dict) else {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    for cluster_id, cluster in clusters.items() if isinstance(clusters, dict) else []:
        if not isinstance(cluster, dict):
            continue
        steps = cluster.get("steps", [])
        if isinstance(steps, list):
            result[str(cluster_id)] = [step for step in steps if isinstance(step, dict)]
    return result


def _build_materialized_sibling_group(
    page: Dict[str, Any],
    siblings: Sequence[Dict[str, Any]],
    instance_groups: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any] | None:
    filename = _base_filename(page.get("filename"))
    general_page_id = str(page.get("general_page_id") or "")
    if not filename or not general_page_id or len(siblings) <= 1:
        return None
    current_key = str(page.get("materialized_page_key") or "")
    candidates: List[Dict[str, Any]] = []
    for sibling in siblings:
        sibling_filename = _base_filename(sibling.get("filename"))
        materialized_key = str(sibling.get("materialized_page_key") or "")
        group = _instance_group_for_page(sibling, instance_groups)
        candidates.append({
            "candidate_id": f"instance::{materialized_key or sibling_filename}",
            "role": "current_page" if sibling_filename == filename else "sibling_instance",
            "page_id": str(sibling.get("page_id") or ""),
            "filename": sibling_filename,
            "materialized_page_key": materialized_key,
            "page_name": _clip_text(sibling.get("page_name"), 120),
            "split_dimension": str(group.get("split_dimension") or ""),
            "split_reason": _clip_text(group.get("split_reason"), 240),
            "evidence_obs_keys": list(group.get("obs_keys", []) or [])[:8],
        })
    current_candidate = next(
        (item for item in candidates if item.get("role") == "current_page"),
        None,
    )
    if not current_candidate:
        return None
    return {
        "conflict_id": f"{filename}::materialized_scope::{_safe_id(general_page_id)}",
        "conflict_type": "materialized_page_sibling_scope",
        "filename": filename,
        "page_id": str(page.get("page_id") or ""),
        "general_page_id": general_page_id,
        "materialized_page_key": current_key,
        "surface": "page_scope",
        "policy_intent": "Select the current materialized page as default; sibling materialized pages are state/context variants and must not be merged into the current page initial render.",
        "hard_default_candidate_id": str(current_candidate.get("candidate_id") or ""),
        "candidates": candidates,
        "hard_constraints": [
            "selected_default must be the candidate with role=current_page.",
            "sibling_instance candidates may be state variants or dropped; they must not be rendered side by side in the current default page.",
        ],
    }


def _build_interaction_conflict_group(
    group: Dict[str, Any],
    pages_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any] | None:
    source_page_id = str(group.get("source_page_id") or "")
    source_page = pages_by_id.get(source_page_id, {})
    filename = _base_filename(source_page.get("filename"))
    if not filename:
        first_candidate = _first_dict((_first_dict(group.get("items", [{}])[0])).get("candidate")) if group.get("items") else {}
        filename = _base_filename(first_candidate.get("source_filename"))
    if not filename:
        return None
    element_name = str(group.get("element_name") or "page_navigation")
    candidates: List[Dict[str, Any]] = []
    for item in group.get("items", []) if isinstance(group.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        candidate = _first_dict(item.get("candidate"))
        route_binding = _first_dict(item.get("route_binding"))
        candidate_id = str(item.get("candidate_id") or candidate.get("candidate_id") or item.get("representative_candidate_id") or "")
        if not candidate_id:
            candidate_id = f"target::{candidate.get('target_page_id') or candidate.get('target_filename') or len(candidates)}"
        candidates.append({
            "candidate_id": candidate_id,
            "l0_role": _candidate_l0_role(item),
            "surface": _surface_from_candidate(candidate, item),
            "source_page_id": source_page_id,
            "source_filename": filename,
            "element_name": _clip_text(element_name, 120),
            "display_text": _clip_text(candidate.get("display_text") or item.get("interaction_outcome"), 140),
            "action_semantic": str(item.get("action_semantic") or ""),
            "interaction_outcome": _clip_text(item.get("interaction_outcome"), 180),
            "target_page_id": str(candidate.get("target_page_id") or ""),
            "target_filename": _base_filename(candidate.get("target_filename")),
            "target_page_name": _clip_text(candidate.get("target_page_name"), 120),
            "route_source": str(route_binding.get("source") or ""),
            "route_fallback": str(route_binding.get("fallback") or ""),
            "source_lifecycle": str(item.get("source_lifecycle") or ""),
            "state_ref": str(item.get("state_ref") or ""),
            "entity_ref": str(item.get("entity_ref") or ""),
            "confidence": item.get("confidence", ""),
        })
    if len(candidates) <= 1:
        return None
    surface = "mixed_surface" if len({str(item.get("surface") or "") for item in candidates}) > 1 else str(candidates[0].get("surface") or "unknown_surface")
    return {
        "conflict_id": f"{filename}::interaction::{_safe_id(source_page_id)}::{_safe_id(element_name)}",
        "conflict_type": str(group.get("quarantine_reason") or "task_interaction_conflict"),
        "filename": filename,
        "page_id": source_page_id,
        "general_page_id": str(source_page.get("general_page_id") or ""),
        "materialized_page_key": str(source_page.get("materialized_page_key") or ""),
        "surface": surface,
        "policy_intent": "Resolve same-trigger task interaction collisions into component roles before PageGen; do not merge multiple route targets as adjacent visual controls for one slot.",
        "candidates": candidates,
        "hard_constraints": [
            "execution_anchor candidates must remain executable; do not classify them as dropped.",
            "If candidates correspond to different component roles under one generic trigger, require component_splits instead of rendering all as one fixed slot.",
        ],
    }


def _build_lifecycle_group_for_page(
    page: Dict[str, Any],
    instance_groups: Dict[str, List[Dict[str, Any]]],
    lifecycle_steps: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any] | None:
    filename = _base_filename(page.get("filename"))
    general_page_id = str(page.get("general_page_id") or "")
    if not filename or not general_page_id:
        return None
    instance_group = _instance_group_for_page(page, instance_groups)
    obs_keys = set(str(item) for item in instance_group.get("obs_keys", []) if str(item))
    steps = [
        step for step in lifecycle_steps.get(general_page_id, [])
        if not obs_keys or str(step.get("obs_key") or "") in obs_keys
    ]
    phases = {str(step.get("lifecycle_phase") or "") for step in steps}
    has_runtime_exclusions = any(step.get("exclude_from_initial_value_hints") for step in steps)
    if len(steps) <= 1 or (len(phases) <= 1 and not has_runtime_exclusions):
        return None
    candidates: List[Dict[str, Any]] = []
    for step in steps[:16]:
        obs_key = str(step.get("obs_key") or "")
        phase = str(step.get("lifecycle_phase") or "")
        role = "runtime_only" if phase.startswith("post_") or step.get("exclude_from_initial_value_hints") else "section_render"
        candidates.append({
            "candidate_id": f"obs::{obs_key}",
            "l0_role": role,
            "obs_key": obs_key,
            "trace_id": str(step.get("trace_id") or ""),
            "step_index": step.get("step_index", ""),
            "lifecycle_phase": phase,
            "exclude_from_initial_value_hints": list(step.get("exclude_from_initial_value_hints", []) or [])[:8],
            "reason": _clip_text(step.get("reason"), 240),
        })
    return {
        "conflict_id": f"{filename}::lifecycle::{_safe_id(general_page_id)}",
        "conflict_type": "lifecycle_phase_scope",
        "filename": filename,
        "page_id": str(page.get("page_id") or ""),
        "general_page_id": general_page_id,
        "materialized_page_key": str(page.get("materialized_page_key") or ""),
        "surface": "content_lifecycle",
        "policy_intent": "Classify observed screenshots into initial render versus runtime-only projections before PageGen.",
        "candidates": candidates,
        "hard_constraints": [
            "post_commit or post_refresh observations must not be baked into initial_state.",
            "Initial screenshots may seed default render only for the same materialized page instance.",
        ],
    }


def build_page_environment_conflict_groups(
    site_model,
    page_environment_specs_by_filename: Dict[str, Dict[str, Any]] | None = None,
    *,
    result_dir: Path | None = None,
) -> Dict[str, Any]:
    pages_by_id, pages_by_filename, pages_by_general = _page_signature_maps(site_model)
    instance_groups = _instance_groups_by_general(site_model)
    lifecycle_steps = _lifecycle_steps_by_cluster(site_model)
    groups_by_filename: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    filenames = set(page_environment_specs_by_filename or {})
    if not filenames:
        filenames = set(pages_by_filename)
    for filename in sorted(filenames):
        page = pages_by_filename.get(_base_filename(filename), {})
        if not page:
            continue
        general_page_id = str(page.get("general_page_id") or "")
        sibling_group = _build_materialized_sibling_group(page, pages_by_general.get(general_page_id, []), instance_groups)
        if sibling_group:
            groups_by_filename[_base_filename(filename)].append(sibling_group)
        lifecycle_group = _build_lifecycle_group_for_page(page, instance_groups, lifecycle_steps)
        if lifecycle_group:
            groups_by_filename[_base_filename(filename)].append(lifecycle_group)


    conflict_payload: Dict[str, Any] = {}
    if result_dir is not None:
        from trace2world_pipeline.core.phase_output import PhaseOutputManager
        pom = PhaseOutputManager(result_dir)
        conflict_payload = _optional_json(pom.debug("consolidation", "task_interaction_contract_conflicts.json"))
    conflicts = conflict_payload.get("conflicts", []) if isinstance(conflict_payload, dict) else []
    for grouped in _group_conflict_records(conflicts if isinstance(conflicts, list) else []):
        conflict_group = _build_interaction_conflict_group(grouped, pages_by_id)
        if conflict_group:
            groups_by_filename[str(conflict_group["filename"])].append(conflict_group)

    flat_groups = [
        group
        for groups in groups_by_filename.values()
        for group in groups
    ]
    return {
        "format": "page_environment_conflict_groups",
        "schema_revision": 1,
        "group_count": len(flat_groups),
        "groups_by_filename": dict(groups_by_filename),
        "sources": {
            "page_signatures": "phase_consolidation/site_model/page_signatures.json",
            "instance_plan": "phase_instance_splitting/instance_plan.json",
            "content_lifecycle_plan": "phase_trace_semantic_extraction/content_lifecycle_plan.json",
            "task_interaction_contract_conflicts": "phase_instance_splitting/debug/task_interaction_contract_conflicts.json",
        },
    }


def _candidate_ids(group: Dict[str, Any]) -> List[str]:
    return [
        str(item.get("candidate_id") or "")
        for item in group.get("candidates", [])
        if isinstance(item, dict) and str(item.get("candidate_id") or "")
    ]


def _fallback_resolution(group: Dict[str, Any], reason_code: str = "deterministic_fallback") -> Dict[str, Any]:
    candidates = [item for item in group.get("candidates", []) if isinstance(item, dict)]
    candidate_ids = _candidate_ids(group)
    hard_default = str(group.get("hard_default_candidate_id") or "")
    if hard_default not in candidate_ids:
        current = next((item for item in candidates if item.get("role") == "current_page"), None)
        hard_default = str((current or candidates[0]).get("candidate_id") or "") if candidates else ""
    state_variants: List[str] = []
    runtime_only: List[str] = []
    section_render: List[str] = []
    dropped: List[str] = []
    visual_samples: List[str] = []
    selected = hard_default or (candidate_ids[0] if candidate_ids else "")
    for candidate in candidates:
        cid = str(candidate.get("candidate_id") or "")
        if not cid or cid == selected:
            continue
        if candidate.get("role") == "sibling_instance":
            state_variants.append(cid)
        elif str(candidate.get("l0_role") or "") == "runtime_only" or str(candidate.get("lifecycle_phase") or "").startswith("post_"):
            runtime_only.append(cid)
        elif str(candidate.get("l0_role") or "") == "execution_anchor":
            runtime_only.append(cid)
        elif group.get("conflict_type") == "lifecycle_phase_scope":
            section_render.append(cid)
        else:
            visual_samples.append(cid)
    return {
        "conflict_id": str(group.get("conflict_id") or ""),
        "conflict_type": str(group.get("conflict_type") or ""),
        "surface": str(group.get("surface") or ""),
        "selected_default": selected,
        "state_variants": _dedupe(state_variants, limit=32),
        "runtime_only": _dedupe(runtime_only, limit=32),
        "section_render": _dedupe(section_render, limit=32),
        "visual_samples": _dedupe(visual_samples, limit=32),
        "dropped": _dedupe(dropped, limit=32),
        "component_splits": [],
        "reason_code": reason_code,
        "confidence": 0.0,
        "resolver": "deterministic_fallback",
        "notes": "Fallback selected contract-safe default without LLM output.",
    }


def _validate_resolution(group: Dict[str, Any], raw: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _fallback_resolution(group, "missing_llm_resolution")
    candidate_ids = set(_candidate_ids(group))
    if not candidate_ids:
        return _fallback_resolution(group, "empty_candidate_group")

    selected = str(raw.get("selected_default") or "")
    hard_default = str(group.get("hard_default_candidate_id") or "")
    if group.get("conflict_type") == "materialized_page_sibling_scope":
        if hard_default:
            selected = hard_default
    elif selected not in candidate_ids:
        selected = next(iter(candidate_ids))

    normalized: Dict[str, Any] = {
        "conflict_id": str(group.get("conflict_id") or raw.get("conflict_id") or ""),
        "conflict_type": str(group.get("conflict_type") or raw.get("conflict_type") or ""),
        "surface": str(group.get("surface") or raw.get("surface") or ""),
        "selected_default": selected,
        "state_variants": [],
        "runtime_only": [],
        "section_render": [],
        "visual_samples": [],
        "dropped": [],
        "component_splits": raw.get("component_splits", []) if isinstance(raw.get("component_splits"), list) else [],
        "reason_code": str(raw.get("reason_code") or "llm_resolved"),
        "confidence": raw.get("confidence", 0),
        "resolver": "llm_semantic_conflict_resolver",
        "notes": _clip_text(raw.get("notes"), 240),
    }
    seen = {selected}
    for bucket in CONFLICT_DECISION_BUCKETS:
        values = raw.get(bucket, []) if isinstance(raw.get(bucket), list) else []
        for value in values:
            cid = str(value or "")
            if not cid or cid not in candidate_ids or cid in seen:
                continue
            normalized[bucket].append(cid)
            seen.add(cid)

    candidate_by_id = {
        str(item.get("candidate_id") or ""): item
        for item in group.get("candidates", [])
        if isinstance(item, dict)
    }
    for cid in list(normalized["dropped"]):
        if str(candidate_by_id.get(cid, {}).get("l0_role") or "") == "execution_anchor":
            normalized["dropped"].remove(cid)
            normalized["runtime_only"].append(cid)
            normalized["reason_code"] = "llm_adjusted_preserve_execution_anchor"

    for cid in sorted(candidate_ids - seen):
        candidate = candidate_by_id.get(cid, {})
        if group.get("conflict_type") == "materialized_page_sibling_scope" and candidate.get("role") == "sibling_instance":
            normalized["state_variants"].append(cid)
        elif str(candidate.get("l0_role") or "") == "execution_anchor":
            normalized["runtime_only"].append(cid)
        elif str(candidate.get("lifecycle_phase") or "").startswith("post_"):
            normalized["runtime_only"].append(cid)
        else:
            normalized["visual_samples"].append(cid)

    for bucket in CONFLICT_DECISION_BUCKETS:
        normalized[bucket] = _dedupe(normalized[bucket], limit=32)
    return normalized


def _resolution_candidate_summary(group: Dict[str, Any]) -> Dict[str, str]:
    summary: Dict[str, str] = {}
    for candidate in group.get("candidates", []) if isinstance(group.get("candidates"), list) else []:
        if not isinstance(candidate, dict):
            continue
        cid = str(candidate.get("candidate_id") or "")
        if not cid:
            continue
        label = (
            candidate.get("materialized_page_key")
            or candidate.get("target_page_name")
            or candidate.get("display_text")
            or candidate.get("obs_key")
            or candidate.get("filename")
            or cid
        )
        summary[cid] = _clip_text(label, 96)
    return summary


def _chunked(items: Sequence[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    size = max(1, int(size or 1))
    return [list(items[index:index + size]) for index in range(0, len(items), size)]


_COMPONENT_PROJECTION_INPUT_BUDGET_TOKENS = 16000
_COMPONENT_PROJECTION_REQUEST_SPLIT_STRATEGY = "page_group_transition_budget_v1"
_COMPONENT_PROJECTION_COMPLETION_PROXY_INTERCEPT = 349.235
_COMPONENT_PROJECTION_FEATURE_CAPS = {
    "transitions": 48,
    "oracles": 32,
    "existing_render_projections": 12,
    "conflict_resolutions": 12,
    "parameterized_inputs": 12,
}
_COMPONENT_PROJECTION_NORMALIZED_TOKEN_WEIGHTS = {
    "transitions": 21953.328,
    "oracles": 362.624,
    "existing_render_projections": 561.828,
    "conflict_resolutions": 7736.796,
    "parameterized_inputs": 0.0,
}


def _json_token_estimate(payload: Any) -> int:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _component_projection_feature_counts(task: Dict[str, Any]) -> Dict[str, int]:
    return {
        "transitions": len(task.get("transitions", []) if isinstance(task.get("transitions"), list) else []),
        "oracles": len(task.get("oracles", []) if isinstance(task.get("oracles"), list) else []),
        "existing_render_projections": len(task.get("existing_render_projections", []) if isinstance(task.get("existing_render_projections"), list) else []),
        "conflict_resolutions": len(task.get("conflict_resolutions", []) if isinstance(task.get("conflict_resolutions"), list) else []),
        "parameterized_inputs": len(task.get("parameterized_inputs", []) if isinstance(task.get("parameterized_inputs"), list) else []),
    }


def _component_projection_completion_proxy_tokens(features: Dict[str, int]) -> float:
    score = _COMPONENT_PROJECTION_COMPLETION_PROXY_INTERCEPT
    for name, cap in _COMPONENT_PROJECTION_FEATURE_CAPS.items():
        if cap <= 0:
            continue
        score += (
            _COMPONENT_PROJECTION_NORMALIZED_TOKEN_WEIGHTS.get(name, 0.0)
            * min(max(0, int(features.get(name, 0))), cap)
            / cap
        )
    return max(0.0, score)


def _component_projection_field_input_tokens(task: Dict[str, Any]) -> Dict[str, int]:
    identity = {
        key: task.get(key)
        for key in (
            "filename",
            "page_name",
            "general_page_id",
            "materialized_page_key",
            "request_shard_id",
            "source_page_task_filename",
        )
        if task.get(key) not in ("", [], {}, None)
    }
    return {
        "identity": _json_token_estimate(identity),
        "content_context": _json_token_estimate(task.get("content_context", {})),
        "runtime_observations": _json_token_estimate(task.get("runtime_observations", [])),
        "interaction_contracts": _json_token_estimate(task.get("interaction_contracts", [])),
        "parameterized_inputs": _json_token_estimate(task.get("parameterized_inputs", [])),
        "transitions": _json_token_estimate(task.get("transitions", [])),
        "oracles": _json_token_estimate(task.get("oracles", [])),
        "conflict_resolutions": _json_token_estimate(task.get("conflict_resolutions", [])),
        "existing_render_projections": _json_token_estimate(task.get("existing_render_projections", [])),
    }


def _component_projection_metric(
    task: Dict[str, Any],
    *,
    token_estimator: Any | None = None,
) -> Dict[str, Any]:
    if callable(token_estimator):
        input_tokens = int(token_estimator(task))
    elif isinstance(task.get("input_tokens"), (int, float)):
        input_tokens = int(task.get("input_tokens") or 0)
    else:
        input_tokens = _json_token_estimate(task)
    features = _component_projection_feature_counts(task)
    completion_proxy = _component_projection_completion_proxy_tokens(features)
    field_input_tokens = _component_projection_field_input_tokens(task)
    return {
        "task": task,
        "filename": str(task.get("filename") or ""),
        "input_tokens": max(0, input_tokens),
        "field_input_tokens": field_input_tokens,
        "features": features,
        "completion_proxy_tokens": completion_proxy,
        "balance_load": max(0, input_tokens) + completion_proxy,
    }


def _component_projection_stdev(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5


def _component_projection_pack_objective(bins: Sequence[Dict[str, Any]]) -> Tuple[float, float, int, float]:
    loads = [float(item.get("balance_load", 0.0)) for item in bins]
    inputs = [int(item.get("input_tokens", 0)) for item in bins]
    return (
        max(loads) if loads else 0.0,
        _component_projection_stdev(loads),
        max(inputs) if inputs else 0,
        _component_projection_stdev(inputs),
    )


def _empty_component_projection_bin() -> Dict[str, Any]:
    return {
        "items": [],
        "input_tokens": 0,
        "field_input_tokens": {
            "identity": 0,
            "content_context": 0,
            "runtime_observations": 0,
            "interaction_contracts": 0,
            "parameterized_inputs": 0,
            "transitions": 0,
            "oracles": 0,
            "conflict_resolutions": 0,
            "existing_render_projections": 0,
        },
        "completion_proxy_tokens": 0.0,
        "balance_load": 0.0,
        "features": {name: 0 for name in _COMPONENT_PROJECTION_FEATURE_CAPS},
    }


def _add_metric_to_component_projection_bin(bin_payload: Dict[str, Any], metric: Dict[str, Any]) -> None:
    bin_payload["items"].append(metric)
    bin_payload["input_tokens"] += int(metric.get("input_tokens", 0))
    for name, value in metric.get("field_input_tokens", {}).items():
        bin_payload["field_input_tokens"][name] = int(
            bin_payload["field_input_tokens"].get(name, 0)
        ) + int(value or 0)
    bin_payload["completion_proxy_tokens"] += float(metric.get("completion_proxy_tokens", 0.0))
    bin_payload["balance_load"] += float(metric.get("balance_load", 0.0))
    for name in _COMPONENT_PROJECTION_FEATURE_CAPS:
        bin_payload["features"][name] += int(metric.get("features", {}).get(name, 0))


def _remove_metric_from_component_projection_bin(bin_payload: Dict[str, Any], metric: Dict[str, Any]) -> None:
    bin_payload["items"].pop()
    bin_payload["input_tokens"] -= int(metric.get("input_tokens", 0))
    for name, value in metric.get("field_input_tokens", {}).items():
        bin_payload["field_input_tokens"][name] = int(
            bin_payload["field_input_tokens"].get(name, 0)
        ) - int(value or 0)
    bin_payload["completion_proxy_tokens"] -= float(metric.get("completion_proxy_tokens", 0.0))
    bin_payload["balance_load"] -= float(metric.get("balance_load", 0.0))
    for name in _COMPONENT_PROJECTION_FEATURE_CAPS:
        bin_payload["features"][name] -= int(metric.get("features", {}).get(name, 0))


def _component_projection_metric_fits_bin(
    bin_payload: Dict[str, Any],
    metric: Dict[str, Any],
    input_budget_tokens: int,
) -> bool:
    metric_input = int(metric.get("input_tokens", 0))
    if metric_input > input_budget_tokens:
        return not bin_payload["items"]
    return int(bin_payload.get("input_tokens", 0)) + metric_input <= input_budget_tokens


def _greedy_component_projection_pack(
    metrics: Sequence[Dict[str, Any]],
    chunk_count: int,
    input_budget_tokens: int,
) -> List[Dict[str, Any]] | None:
    bins = [_empty_component_projection_bin() for _ in range(max(1, chunk_count))]
    for metric in sorted(metrics, key=lambda item: float(item.get("balance_load", 0.0)), reverse=True):
        candidates = [
            (float(bin_payload.get("balance_load", 0.0)), int(bin_payload.get("input_tokens", 0)), index)
            for index, bin_payload in enumerate(bins)
            if _component_projection_metric_fits_bin(bin_payload, metric, input_budget_tokens)
        ]
        if not candidates:
            return None
        index = min(candidates)[2]
        _add_metric_to_component_projection_bin(bins[index], metric)
    return bins


def _optimize_component_projection_pack(
    metrics: Sequence[Dict[str, Any]],
    chunk_count: int,
    input_budget_tokens: int,
) -> List[Dict[str, Any]]:
    chunk_count = max(1, int(chunk_count or 1))
    greedy_bins = _greedy_component_projection_pack(metrics, chunk_count, input_budget_tokens)
    while greedy_bins is None:
        chunk_count += 1
        greedy_bins = _greedy_component_projection_pack(metrics, chunk_count, input_budget_tokens)
    best_bins = [
        {
            "items": list(bin_payload["items"]),
            "input_tokens": bin_payload["input_tokens"],
            "field_input_tokens": dict(bin_payload.get("field_input_tokens", {})),
            "completion_proxy_tokens": bin_payload["completion_proxy_tokens"],
            "balance_load": bin_payload["balance_load"],
            "features": dict(bin_payload["features"]),
        }
        for bin_payload in greedy_bins
    ]
    best_objective = _component_projection_pack_objective(best_bins)
    if len(metrics) > 24 or chunk_count > 8:
        return best_bins

    ordered = sorted(metrics, key=lambda item: float(item.get("balance_load", 0.0)), reverse=True)
    bins = [_empty_component_projection_bin() for _ in range(chunk_count)]
    started_at = time.monotonic()

    def snapshot_bins() -> List[Dict[str, Any]]:
        return [
            {
                "items": list(bin_payload["items"]),
                "input_tokens": bin_payload["input_tokens"],
                "field_input_tokens": dict(bin_payload.get("field_input_tokens", {})),
                "completion_proxy_tokens": bin_payload["completion_proxy_tokens"],
                "balance_load": bin_payload["balance_load"],
                "features": dict(bin_payload["features"]),
            }
            for bin_payload in bins
        ]

    def dfs(index: int) -> None:
        nonlocal best_bins, best_objective
        if time.monotonic() - started_at > 3.0:
            return
        if index >= len(ordered):
            objective = _component_projection_pack_objective(bins)
            if objective < best_objective:
                best_objective = objective
                best_bins = snapshot_bins()
            return
        metric = ordered[index]
        seen: set[Tuple[int, int, int]] = set()
        for bin_index in sorted(range(len(bins)), key=lambda idx: (bins[idx]["balance_load"], bins[idx]["input_tokens"])):
            bin_payload = bins[bin_index]
            signature = (
                int(bin_payload["input_tokens"]),
                int(round(float(bin_payload["balance_load"]))),
                len(bin_payload["items"]),
            )
            if signature in seen:
                continue
            seen.add(signature)
            if not _component_projection_metric_fits_bin(bin_payload, metric, input_budget_tokens):
                continue
            new_balance = float(bin_payload["balance_load"]) + float(metric.get("balance_load", 0.0))
            if new_balance > best_objective[0]:
                continue
            _add_metric_to_component_projection_bin(bin_payload, metric)
            dfs(index + 1)
            _remove_metric_from_component_projection_bin(bin_payload, metric)
            if not bin_payload["items"]:
                break

    dfs(0)
    return best_bins


def _component_projection_task_estimate(
    task: Dict[str, Any],
    *,
    token_estimator: Any | None = None,
) -> int:
    if callable(token_estimator):
        try:
            return int(token_estimator(task))
        except Exception:
            pass
    return _json_token_estimate(task)


def _component_projection_task_slice(
    task: Dict[str, Any],
    transitions: Sequence[Dict[str, Any]],
    *,
    request_index: int,
) -> Dict[str, Any]:
    step_keys = {
        str(item.get("step_key") or "")
        for item in transitions
        if isinstance(item, dict) and str(item.get("step_key") or "")
    }
    sliced = dict(task)
    sliced["request_shard_id"] = (
        f"{_safe_id(task.get('filename') or 'page')}::component_projection_request::{request_index:04d}"
    )
    sliced["source_page_task_filename"] = str(task.get("filename") or "")
    sliced["transitions"] = [dict(item) for item in transitions if isinstance(item, dict)]
    oracles = task.get("oracles", []) if isinstance(task.get("oracles"), list) else []
    sliced["oracles"] = [
        dict(item)
        for item in oracles
        if isinstance(item, dict)
        and (
            not str(item.get("step_key") or "")
            or str(item.get("step_key") or "") in step_keys
        )
    ]
    return sliced


def _expand_component_projection_request_tasks(
    component_tasks: Sequence[Dict[str, Any]],
    *,
    input_budget_tokens: int,
    token_estimator: Any | None = None,
) -> List[Dict[str, Any]]:
    budget = max(1, int(input_budget_tokens or _COMPONENT_PROJECTION_INPUT_BUDGET_TOKENS))
    expanded: List[Dict[str, Any]] = []
    for task in component_tasks:
        if not isinstance(task, dict):
            continue
        transitions = [
            item
            for item in task.get("transitions", [])
            if isinstance(item, dict)
        ]
        if len(transitions) <= 1 or _component_projection_task_estimate(task, token_estimator=token_estimator) <= budget:
            expanded.append(task)
            continue
        request_index = 1
        current: List[Dict[str, Any]] = []
        for transition in transitions:
            preview = _component_projection_task_slice(
                task,
                current + [transition],
                request_index=request_index,
            )
            if current and _component_projection_task_estimate(preview, token_estimator=token_estimator) > budget:
                expanded.append(
                    _component_projection_task_slice(
                        task,
                        current,
                        request_index=request_index,
                    )
                )
                request_index += 1
                current = [transition]
            else:
                current.append(transition)
        if current:
            expanded.append(
                _component_projection_task_slice(
                    task,
                    current,
                    request_index=request_index,
                )
            )
    return expanded


def _build_component_projection_chunks(
    component_tasks: Sequence[Dict[str, Any]],
    *,
    input_budget_tokens: int = _COMPONENT_PROJECTION_INPUT_BUDGET_TOKENS,
    token_estimator: Any | None = None,
) -> Tuple[List[List[Dict[str, Any]]], Dict[str, Any]]:
    source_page_task_count = len([task for task in component_tasks if isinstance(task, dict)])
    component_tasks = _expand_component_projection_request_tasks(
        component_tasks,
        input_budget_tokens=input_budget_tokens,
        token_estimator=token_estimator,
    )
    metrics = [
        _component_projection_metric(task, token_estimator=token_estimator)
        for task in component_tasks
        if isinstance(task, dict)
    ]
    input_budget_tokens = max(1, int(input_budget_tokens or _COMPONENT_PROJECTION_INPUT_BUDGET_TOKENS))
    total_input = sum(int(metric["input_tokens"]) for metric in metrics)
    total_field_input_tokens: Dict[str, int] = {}
    for metric in metrics:
        for name, value in metric.get("field_input_tokens", {}).items():
            total_field_input_tokens[name] = total_field_input_tokens.get(name, 0) + int(value or 0)
    chunk_count = max(1, (total_input + input_budget_tokens - 1) // input_budget_tokens) if metrics else 0
    bins = _optimize_component_projection_pack(metrics, chunk_count, input_budget_tokens) if metrics else []
    chunks = [[metric["task"] for metric in bin_payload["items"]] for bin_payload in bins]
    report_chunks = []
    for index, bin_payload in enumerate(bins, start=1):
        report_chunks.append({
            "chunk": index,
            "page_count": len(bin_payload["items"]),
            "filenames": [str(metric.get("filename") or "") for metric in bin_payload["items"]],
            "input_tokens": int(bin_payload["input_tokens"]),
            "field_input_tokens": dict(bin_payload.get("field_input_tokens", {})),
            "completion_proxy_tokens": round(float(bin_payload["completion_proxy_tokens"])),
            "balance_load": round(float(bin_payload["balance_load"])),
            "features": dict(bin_payload["features"]),
            "oversized_input": int(bin_payload["input_tokens"]) > input_budget_tokens,
        })
    total_weight = sum(_COMPONENT_PROJECTION_NORMALIZED_TOKEN_WEIGHTS.values())
    report = {
        "format": L0_COMPONENT_RENDER_BINDING_BUDGET_FORMAT,
        "schema_revision": 2,
        "strategy": "input_budgeted_balance_proxy",
        "request_split_strategy": _COMPONENT_PROJECTION_REQUEST_SPLIT_STRATEGY,
        "hard_constraint": "input_tokens",
        "input_budget_tokens": input_budget_tokens,
        "total_input_tokens": total_input,
        "total_field_input_tokens": total_field_input_tokens,
        "source_page_task_count": source_page_task_count,
        "request_task_count": len(component_tasks),
        "chunk_count": len(chunks),
        "feature_caps": dict(_COMPONENT_PROJECTION_FEATURE_CAPS),
        "completion_proxy_intercept": _COMPONENT_PROJECTION_COMPLETION_PROXY_INTERCEPT,
        "normalized_token_weights": dict(_COMPONENT_PROJECTION_NORMALIZED_TOKEN_WEIGHTS),
        "relative_structural_weights": {
            name: (weight / total_weight if total_weight else 0.0)
            for name, weight in _COMPONENT_PROJECTION_NORMALIZED_TOKEN_WEIGHTS.items()
        },
        "chunks": report_chunks,
    }
    return chunks, report


def _write_component_projection_chunk_budget_report(result_dir: Path, report: Dict[str, Any]) -> Path:
    report_path = Path(result_dir) / "phase_page_environment" / "debug" / L0_COMPONENT_RENDER_BINDING_BUDGET_FILE
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def _page_metadata_by_filename(site_model) -> Dict[str, Dict[str, Any]]:
    _pages_by_id, by_filename, _pages_by_general = _page_signature_maps(site_model)
    return by_filename


def _compact_runtime_observation(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in {
            "obs_key": str(item.get("obs_key") or ""),
            "page_cluster_id": str(item.get("page_cluster_id") or ""),
            "scope": str(item.get("scope") or item.get("state_scope") or ""),
            "lifecycle": str(item.get("lifecycle") or ""),
            "mutation": str(item.get("mutation") or ""),
            "raw_evidence": _clip_text(item.get("raw_evidence"), 360),
            "value_hints": [
                _clip_text(value, 180)
                for value in item.get("value_hints", [])[:8]
            ] if isinstance(item.get("value_hints"), list) else [],
        }.items()
        if value not in ("", [], {})
    }


def _runtime_observation_indexes(site_model) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[Tuple[str, str], List[Dict[str, Any]]]]:
    payload = site_model.runtime_state_observations
    observations = payload.get("observations", []) if isinstance(payload, dict) else []
    by_step: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_trace_cluster: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in observations if isinstance(observations, list) else []:
        if not isinstance(item, dict):
            continue
        compact = _compact_runtime_observation(item)
        obs_key = str(item.get("obs_key") or "")
        trace_id = str(item.get("trace_id") or "")
        cluster_id = str(item.get("page_cluster_id") or "")
        if obs_key and compact:
            by_step[obs_key].append(compact)
        if trace_id and cluster_id and compact:
            by_trace_cluster[(trace_id, cluster_id)].append(compact)
    return dict(by_step), dict(by_trace_cluster)


def _page_content_context(site_model, page: Dict[str, Any]) -> Dict[str, Any]:
    page_id = str(page.get("page_id") or "")
    if not page_id:
        return {}
    payload: Dict[str, Any] = {}
    for proto in site_model.structure.page_prototypes:
        if proto.page_id == page_id:
            payload = proto.content_data if isinstance(proto.content_data, dict) else {}
            break
    if not isinstance(payload, dict):
        return {}
    extracted = payload.get("extracted_items", {}) if isinstance(payload.get("extracted_items"), dict) else {}
    runtime_items = payload.get("runtime_items", {}) if isinstance(payload.get("runtime_items"), dict) else {}
    runtime_summary = {}
    for entity, items in runtime_items.items():
        if not isinstance(items, list):
            continue
        values: List[str] = []
        for item in items[:8]:
            if isinstance(item, dict):
                label = item.get("name") or item.get("title") or item.get("mode") or item.get("range") or item.get("distance_range")
                if label:
                    values.append(_clip_text(label, 80))
            elif item:
                values.append(_clip_text(item, 80))
        if values:
            runtime_summary[str(entity)] = values[:8]
    extracted_summary = {}
    for entity, items in extracted.items():
        if entity == "_metadata" or not isinstance(items, list):
            continue
        values: List[str] = []
        for item in items[:8]:
            if isinstance(item, dict):
                label = item.get("name") or item.get("title") or item.get("mode") or item.get("range")
                if label:
                    values.append(_clip_text(label, 80))
            elif item:
                values.append(_clip_text(item, 80))
        extracted_summary[str(entity)] = values[:8]
    return {
        "entity_component_mapping": payload.get("entity_component_mapping", [])[:16] if isinstance(payload.get("entity_component_mapping"), list) else [],
        "entity_task_relevance_keys": sorted((payload.get("entity_task_relevance", {}) or {}).keys())[:16] if isinstance(payload.get("entity_task_relevance"), dict) else [],
        "extracted_item_summary": extracted_summary,
        "excluded_from_initial": payload.get("excluded_from_initial", [])[:12] if isinstance(payload.get("excluded_from_initial"), list) else [],
        "item_lifecycle": payload.get("item_lifecycle", [])[:20] if isinstance(payload.get("item_lifecycle"), list) else [],
        "runtime_item_summary": runtime_summary,
    }


def _interaction_contracts_by_filename(site_model) -> Dict[str, List[Dict[str, Any]]]:
    payload = site_model.task_interaction_contracts
    contracts = payload.get("contracts", []) if isinstance(payload, dict) else []
    by_filename: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for contract in contracts if isinstance(contracts, list) else []:
        if not isinstance(contract, dict):
            continue
        candidate = contract.get("candidate", {}) if isinstance(contract.get("candidate"), dict) else {}
        filename = _base_filename(candidate.get("source_filename"))
        if not filename:
            continue
        by_filename[filename].append({
            "contract_id": str(contract.get("contract_id") or ""),
            "action_semantic": str(contract.get("action_semantic") or ""),
            "interaction_outcome": _clip_text(contract.get("interaction_outcome"), 180),
            "state_ref": str(contract.get("state_ref") or ""),
            "entity_ref": str(contract.get("entity_ref") or ""),
            "identity_evidence": contract.get("identity_evidence", [])[:6] if isinstance(contract.get("identity_evidence"), list) else [],
            "route_binding": {
                key: value
                for key, value in (contract.get("route_binding", {}) if isinstance(contract.get("route_binding"), dict) else {}).items()
                if key in {
                    "source",
                    "fallback",
                    "preserve_existing",
                    "requires_params",
                    "target_filename",
                    "target_page_id",
                    "rationale",
                    "admission_rationale",
                    "binding_kind",
                    "confidence",
                } and value not in ("", [], {})
            },
            "sdk_effect": {
                key: value
                for key, value in (contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect"), dict) else {}).items()
                if key in {"effect", "method", "trigger"} and value not in ("", [], {})
            },
            "candidate": {
                key: _clip_text(value, 160)
                for key, value in {
                    "element_name": candidate.get("element_name"),
                    "display_text": candidate.get("display_text"),
                    "target_filename": _base_filename(candidate.get("target_filename")),
                    "reason": candidate.get("reason"),
                }.items()
                if value not in ("", [], {})
            },
        })
    return dict(by_filename)


def _query_task_id(
    *,
    kind: str,
    trace_id: str,
    step_key: str,
    draft_ref: str,
    source: str,
    target: str,
    query: str,
) -> str:
    seed = "::".join([
        kind,
        trace_id,
        step_key,
        draft_ref,
        _base_filename(source),
        _base_filename(target),
        str(query or ""),
    ])
    return "qident_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _query_event_id(
    *,
    trace_id: str,
    step_key: str,
    query_role: str,
    source: str,
    target: str,
    query: str,
) -> str:
    seed = "::".join([
        "canonical_query_event",
        trace_id,
        step_key,
        str(query_role or ""),
        _base_filename(source),
        _base_filename(target),
        str(query or "").strip(),
    ])
    return "qevent_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _canonical_query_identity_task_id(query_event_id: str) -> str:
    seed = f"canonical_query_identity::{query_event_id}"
    return "qident_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _query_field_entries_from_session(session: Dict[str, Any]) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for field in session.get("fields", []) if isinstance(session.get("fields"), list) else []:
        if not isinstance(field, dict):
            continue
        role = str(field.get("field_role") or "")
        value = str(field.get("value") or "").strip()
        key = (role, value)
        if role not in SEARCHLIKE_FIELD_ROLES or not value or key in seen:
            continue
        seen.add(key)
        entries.append({"field_role": role, "value": value})
        if len(entries) >= 8:
            break
    return entries


def _query_field_entries_from_effect(effect: Dict[str, Any]) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()

    def add(role: str, value: str) -> None:
        text = str(value or "").strip()
        key = (role, text)
        if role not in SEARCHLIKE_FIELD_ROLES or not text or key in seen:
            return
        seen.add(key)
        entries.append({"field_role": role, "value": text})

    fields = effect.get("committed_fields", []) if isinstance(effect.get("committed_fields"), list) else []
    for field in fields:
        if isinstance(field, dict):
            add(str(field.get("field_role") or ""), str(field.get("value") or ""))
            if len(entries) >= 8:
                break
    if len(entries) < 8:
        add(str(effect.get("field_role") or ""), str(effect.get("value") or ""))
    return entries[:8]


def _query_fields_from_session(session: Dict[str, Any]) -> List[str]:
    return _dedupe((entry["value"] for entry in _query_field_entries_from_session(session)), limit=8)


def _query_fields_from_effect(effect: Dict[str, Any]) -> List[str]:
    return _dedupe((entry["value"] for entry in _query_field_entries_from_effect(effect)), limit=8)


def _merge_query_identity_task(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    provenance = _dedupe(
        list(existing.get("provenance", []) if isinstance(existing.get("provenance"), list) else [])
        + list(incoming.get("provenance", []) if isinstance(incoming.get("provenance"), list) else []),
        limit=8,
    )
    existing["provenance"] = provenance
    existing["source_kind"] = "canonical_query_event"
    existing["session_refs"] = _dedupe(
        list(existing.get("session_refs", []) if isinstance(existing.get("session_refs"), list) else [])
        + list(incoming.get("session_refs", []) if isinstance(incoming.get("session_refs"), list) else []),
        limit=16,
    )
    existing["transition_refs"] = _dedupe(
        list(existing.get("transition_refs", []) if isinstance(existing.get("transition_refs"), list) else [])
        + list(incoming.get("transition_refs", []) if isinstance(incoming.get("transition_refs"), list) else []),
        limit=16,
    )
    existing["draft_refs"] = _dedupe(
        list(existing.get("draft_refs", []) if isinstance(existing.get("draft_refs"), list) else [])
        + list(incoming.get("draft_refs", []) if isinstance(incoming.get("draft_refs"), list) else []),
        limit=16,
    )
    existing["current_hints"] = _dedupe(
        list(existing.get("current_hints", []) if isinstance(existing.get("current_hints"), list) else [])
        + list(incoming.get("current_hints", []) if isinstance(incoming.get("current_hints"), list) else []),
        limit=32,
    )
    for key in ("draft_ref", "operation", "reducer_hint", "target_page_name", "target_materialized_page_key"):
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]
    return existing


def _collect_query_identity_tasks(
    payload: Dict[str, Any],
    site_model,
) -> List[Dict[str, Any]]:
    pages = _page_metadata_by_filename(site_model)
    observations_by_step, observations_by_trace_cluster = _runtime_observation_indexes(site_model)
    contracts_by_filename = _interaction_contracts_by_filename(site_model)
    page_content_by_filename = {
        filename: _page_content_context(site_model, page)
        for filename, page in pages.items()
    }

    def semantic_context(trace_id: str, step_key: str, source: str, target: str) -> Dict[str, Any]:
        source_page = pages.get(source, {})
        target_page = pages.get(target, {})
        contexts = {
            "step_observations": observations_by_step.get(step_key, [])[:8],
            "source_page": {
                "page_name": _clip_text(source_page.get("page_name"), 120),
                "general_page_id": str(source_page.get("general_page_id") or ""),
                "materialized_page_key": str(source_page.get("materialized_page_key") or ""),
            },
            "target_page": {
                "page_name": _clip_text(target_page.get("page_name"), 120),
                "general_page_id": str(target_page.get("general_page_id") or ""),
                "materialized_page_key": str(target_page.get("materialized_page_key") or ""),
            },
            "target_trace_observations": observations_by_trace_cluster.get(
                (trace_id, str(target_page.get("general_page_id") or "")),
                [],
            )[:10],
            "target_content_context": page_content_by_filename.get(target, {}),
            "source_interaction_contracts": contracts_by_filename.get(source, [])[:8],
            "target_interaction_contracts": contracts_by_filename.get(target, [])[:8],
        }
        return {key: value for key, value in contexts.items() if value not in ("", [], {})}

    tasks: Dict[str, Dict[str, Any]] = {}

    def add_task(incoming: Dict[str, Any]) -> None:
        query_event_id = str(incoming.get("query_event_id") or "")
        if not query_event_id:
            return
        existing = tasks.get(query_event_id)
        if existing is None:
            tasks[query_event_id] = incoming
            return
        _merge_query_identity_task(existing, incoming)

    for spec in payload.get("specs", []) if isinstance(payload, dict) else []:
        if not isinstance(spec, dict):
            continue
        trace_id = str(spec.get("trace_id") or "")
        task_text = _clip_text(spec.get("task"), 240)
        for session in spec.get("input_sessions", []) if isinstance(spec.get("input_sessions"), list) else []:
            if not isinstance(session, dict) or not session.get("committed_by_step"):
                continue
            source = _base_filename(session.get("source_filename"))
            target = _base_filename(session.get("target_filename"))
            step_key = str(session.get("committed_by_step") or "")
            for entry in _query_field_entries_from_session(session):
                query = entry["value"]
                query_role = entry["field_role"]
                query_event_id = _query_event_id(
                    trace_id=trace_id,
                    step_key=step_key,
                    query_role=query_role,
                    source=source,
                    target=target,
                    query=query,
                )
                draft_ref = str(session.get("draft_ref") or "")
                add_task({
                    "query_id": _canonical_query_identity_task_id(query_event_id),
                    "query_event_id": query_event_id,
                    "source_kind": "canonical_query_event",
                    "provenance": ["input_session"],
                    "query_role": query_role,
                    "trace_id": trace_id,
                    "task": task_text,
                    "step_key": step_key,
                    "draft_ref": draft_ref,
                    "draft_refs": [draft_ref] if draft_ref else [],
                    "query": query,
                    "source_filename": source,
                    "target_filename": target,
                    "target_page_name": _clip_text(pages.get(target, {}).get("page_name"), 120),
                    "target_materialized_page_key": str(pages.get(target, {}).get("materialized_page_key") or ""),
                    "current_hints": _target_identity_hints(session),
                    "session_refs": [f"{trace_id}:{step_key}:{draft_ref}:{query_role}:{query}"],
                    "transition_refs": [],
                    "admission_status": "ready",
                    "semantic_context": semantic_context(
                        trace_id,
                        step_key,
                        source,
                        target,
                    ),
                })
        for transition in spec.get("transitions", []) if isinstance(spec.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            source = _base_filename(transition.get("source_filename"))
            target = _base_filename(transition.get("target_filename"))
            step_key = str(transition.get("step_key") or "")
            for entry in _query_field_entries_from_effect(effect):
                query = entry["value"]
                query_role = entry["field_role"]
                query_event_id = _query_event_id(
                    trace_id=trace_id,
                    step_key=step_key,
                    query_role=query_role,
                    source=source,
                    target=target,
                    query=query,
                )
                draft_ref = str(effect.get("draft_ref") or "")
                add_task({
                    "query_id": _canonical_query_identity_task_id(query_event_id),
                    "query_event_id": query_event_id,
                    "source_kind": "canonical_query_event",
                    "provenance": ["transition"],
                    "query_role": query_role,
                    "trace_id": trace_id,
                    "task": task_text,
                    "step_key": step_key,
                    "draft_ref": draft_ref,
                    "draft_refs": [draft_ref] if draft_ref else [],
                    "query": query,
                    "source_filename": source,
                    "target_filename": target,
                    "target_page_name": _clip_text(pages.get(target, {}).get("page_name"), 120),
                    "target_materialized_page_key": str(pages.get(target, {}).get("materialized_page_key") or ""),
                    "operation": str(transition.get("operation") or ""),
                    "reducer_hint": _transition_reducer(str(transition.get("operation") or ""), effect),
                    "current_hints": _target_identity_hints(transition),
                    "session_refs": [],
                    "transition_refs": [f"{trace_id}:{step_key}:{query_role}:{query}"],
                    "admission_status": "ready",
                    "semantic_context": semantic_context(
                        trace_id,
                        step_key,
                        source,
                        target,
                    ),
                })
    return list(tasks.values())


def _compact_query_task_for_llm(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in task.items()
        if key in {
            "query_id",
            "query_event_id",
            "source_kind",
            "provenance",
            "query_role",
            "trace_id",
            "task",
            "step_key",
            "query",
            "source_filename",
            "target_filename",
            "target_page_name",
            "target_materialized_page_key",
            "operation",
            "reducer_hint",
            "current_hints",
            "session_refs",
            "transition_refs",
            "admission_status",
            "semantic_context",
        }
        and value not in ("", [], {})
    }


def _query_task_policy_text(task: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "query": task.get("query"),
            "query_role": task.get("query_role"),
            "source_filename": _base_filename(task.get("source_filename")),
            "target_filename": _base_filename(task.get("target_filename")),
            "target_page_name": task.get("target_page_name"),
            "target_materialized_page_key": task.get("target_materialized_page_key"),
            "current_hints": task.get("current_hints", []),
            "semantic_context": task.get("semantic_context", {}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).lower()


def _query_text_present_in_task(task: Dict[str, Any]) -> bool:
    query = str(task.get("query") or "").strip().lower()
    if not query:
        return False
    return query in _query_task_policy_text(task)


def _query_identity_owner(task: Dict[str, Any], fallback: str = "") -> str:
    target_page = (
        task.get("semantic_context", {}).get("target_page", {})
        if isinstance(task.get("semantic_context"), dict)
        and isinstance(task.get("semantic_context", {}).get("target_page"), dict)
        else {}
    )
    owner = (
        target_page.get("general_page_id")
        or task.get("target_materialized_page_key")
        or fallback
        or _base_filename(task.get("target_filename")).rsplit(".", 1)[0]
    )
    return str(owner or "")


def _query_identity_hints_for_task(task: Dict[str, Any], *, limit: int = 12) -> List[str]:
    hints = list(task.get("current_hints", []) if isinstance(task.get("current_hints"), list) else [])
    query = str(task.get("query") or "").strip()
    if query:
        hints.insert(0, f"query:{query}")
    return _dedupe(hints, limit=limit)


def _deterministic_query_identity_resolution(task: Dict[str, Any]) -> Dict[str, Any] | None:
    source = _base_filename(task.get("source_filename")).lower()
    target = _base_filename(task.get("target_filename")).lower()
    query_role = str(task.get("query_role") or "")
    text = _query_task_policy_text(task)
    same_page = bool(source and source == target)

    if same_page and query_role == "address_lookup" and (
        "form_field" in text
        or "address_form_input" in text
        or "phone_number" in text
        or "contact_name" in text
        or "doorplate" in text
        or "门牌" in text
        or "手机号" in text
    ):
        return _validate_query_identity_resolution(task, {
            "query_transition_kind": "unrelated_navigation",
            "query_state_owner": _query_identity_owner(task, "address_form"),
            "target_identity_hints": _query_identity_hints_for_task(task),
            "target_identity_status": "not_applicable",
            "result_projection": "Same-page address form field edit is not a query identity projection.",
            "reason_code": "deterministic_same_page_form_field",
            "confidence": 0.98,
        })

    if same_page and str(task.get("reducer_hint") or "") == "project_suggestions" and "search" in target:
        return _validate_query_identity_resolution(task, {
            "query_transition_kind": "suggestion_projection",
            "query_state_owner": _query_identity_owner(task, "search_entry"),
            "target_identity_hints": _query_identity_hints_for_task(task),
            "target_identity_status": "not_applicable",
            "result_projection": "Same-page search input projects suggestions or inline candidates before query commit.",
            "reason_code": "deterministic_same_page_search_suggestion_projection",
            "confidence": 0.94,
        })

    target_campaign_surface = (
        "flash-deals-listing" in target
        or "campaign-listing" in target
        or "campaign_listing" in target
    )
    if source != target and target_campaign_surface:
        if "search-results" not in target and "search_results" not in target:
            return _validate_query_identity_resolution(task, {
                "query_transition_kind": "committed_result",
                "query_state_owner": _query_identity_owner(task, "mixed_campaign_listing"),
                "target_identity_hints": _query_identity_hints_for_task(task),
                "target_identity_status": "unknown",
                "result_projection": "Query entity appears in a mixed campaign/listing surface, so page identity is not a clean query match.",
                "reason_code": "deterministic_mixed_campaign_listing_unknown",
                "confidence": 0.84,
            })

    if source == "map-search.html" and target == "map-search-results.html" and _query_text_present_in_task(task):
        return _validate_query_identity_resolution(task, {
            "query_transition_kind": "committed_result",
            "query_state_owner": _query_identity_owner(task, "map_search_results"),
            "target_identity_hints": _query_identity_hints_for_task(task),
            "target_identity_status": "match",
            "result_projection": "Map search results are scoped to the committed query.",
            "reason_code": "deterministic_map_search_results_match",
            "confidence": 0.93,
        })

    if source == "map-search-results.html" and "map-poi-detail" in target and (
        "query_applied:" in text or "poi:" in text or "set_active_poi" in text
    ):
        return _validate_query_identity_resolution(task, {
            "query_transition_kind": "selected_suggestion_commit",
            "query_state_owner": _query_identity_owner(task, "map_poi_detail"),
            "target_identity_hints": _query_identity_hints_for_task(task),
            "target_identity_status": "match",
            "result_projection": "Selected map result opens a POI detail bound to the applied suggestion.",
            "reason_code": "deterministic_map_selected_poi_detail_match",
            "confidence": 0.91,
        })

    if "search-results" in source and "detail" in target and (
        "approximate_match:" in text
        or "target_performance_title:" in text
        or "target_venue:" in text
        or "venue:" in text
    ):
        return _validate_query_identity_resolution(task, {
            "query_transition_kind": "committed_result",
            "query_state_owner": _query_identity_owner(task, "search_result_detail"),
            "target_identity_hints": _query_identity_hints_for_task(task),
            "target_identity_status": "match",
            "result_projection": "Search result detail is bound to the selected query-compatible result identity.",
            "reason_code": "deterministic_search_result_detail_match",
            "confidence": 0.88,
        })

    if "groupbuy-store-deals" in target and "target_is_merchant_deals_collection" in text and "venue:" in text:
        return _validate_query_identity_resolution(task, {
            "query_transition_kind": "selected_suggestion_commit",
            "query_state_owner": _query_identity_owner(task, "groupbuy_store_deals"),
            "target_identity_hints": _query_identity_hints_for_task(task),
            "target_identity_status": "match",
            "result_projection": "Selected venue opens the dedicated merchant groupbuy deals collection.",
            "reason_code": "deterministic_selected_groupbuy_venue_deals_match",
            "confidence": 0.9,
        })

    if source != target and ("search-results" in target or "search_results" in target) and _query_text_present_in_task(task):
        if "target_page_mixed_entities_observed" in text:
            return _validate_query_identity_resolution(task, {
                "query_transition_kind": "committed_result",
                "query_state_owner": _query_identity_owner(task, "search_results"),
                "target_identity_hints": _query_identity_hints_for_task(task),
                "target_identity_status": "unknown",
                "result_projection": "Search results contain explicit mixed target identity evidence, so query binding is ambiguous.",
                "reason_code": "deterministic_search_results_explicit_mixed_identity_unknown",
                "confidence": 0.82,
            })
        if (
            "query_applied:" in text
            or "store_mentions:" in text
            or "store_example:" in text
            or "result_list_mentions:" in text
            or "results_list:" in text
            or "results_include:" in text
            or "brand/merchant_chain:" in text
        ):
            return _validate_query_identity_resolution(task, {
                "query_transition_kind": "committed_result",
                "query_state_owner": _query_identity_owner(task, "search_results"),
                "target_identity_hints": _query_identity_hints_for_task(task),
                "target_identity_status": "match",
                "result_projection": "Search results page is query-scoped even when the list contains other normal result items.",
                "reason_code": "deterministic_search_results_query_scoped_match",
                "confidence": 0.9,
            })

    return None


def _partition_query_identity_tasks(tasks: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    deterministic_resolutions: List[Dict[str, Any]] = []
    llm_tasks: List[Dict[str, Any]] = []
    reason_counts: Dict[str, int] = defaultdict(int)
    for task in tasks:
        resolution = _deterministic_query_identity_resolution(task)
        if resolution is None:
            llm_tasks.append(task)
            reason_counts["llm_required"] += 1
            continue
        deterministic_resolutions.append(resolution)
        reason_counts[str(resolution.get("reason_code") or "deterministic")] += 1
    return deterministic_resolutions, llm_tasks, {
        "deterministic_resolved_event_count": len(deterministic_resolutions),
        "llm_required_event_count": len(llm_tasks),
        "deterministic_reason_counts": dict(sorted(reason_counts.items())),
    }


def _query_identity_input_signature(
    query_tasks: Sequence[Dict[str, Any]],
    llm_query_tasks: Sequence[Dict[str, Any]],
    deterministic_resolutions: Sequence[Dict[str, Any]],
    *,
    model: str | None,
    reasoning_effort: str,
    max_tokens: int,
    max_query_groups_per_call: int,
) -> str:
    return _stable_json_signature(
        {
            "phase": "page_environment.query_identity",
            "schema_revision": 2,
            "tasks": list(query_tasks),
            "llm_tasks": list(llm_query_tasks),
            "deterministic_resolutions": list(deterministic_resolutions),
            "model": model or "",
            "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens,
            "max_query_groups_per_call": max_query_groups_per_call,
        }
    )


async def _resolve_query_identity_chunk(
    tasks: Sequence[Dict[str, Any]],
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_tokens: int = 12000,
) -> Dict[str, Any]:
    from trace2world_pipeline.core.llm_caller import call_openai_api_json_async

    messages = [
        {
            "role": "system",
            "content": (
                "You enrich page_environment_spec query semantics. Return JSON only. "
                "Classify each search-like input transition semantically, including business search, address lookup, and location lookup. "
                "Use runtime observations, content lifecycle, "
                "interaction contracts, and page identity together. Do not rely only on filename tokens. "
                "If an input opens a search entry/suggestion/location-pick page, mark it query_entry or suggestion_projection and not_applicable. "
                "If it commits to results/details, provide target_identity_hints and say match or mismatch. "
                "If typed draft text differs from an explicitly selected/applied suggestion, mark selected_suggestion_commit and include target_identity_hints such as query_applied:<applied query>; compare the target with the applied query, not the draft example. "
                "If the target page mixes incompatible query identities, mark mismatch or unknown; do not hide it as a clean match."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "Resolve query identity for these page environment transitions.",
                    "output_schema": {
                        "query_identity_resolutions": [
                            {
                                "query_id": "string",
                                "query_transition_kind": "query_entry|suggestion_projection|selected_suggestion_commit|committed_result|search_within_collection|unrelated_navigation|unknown",
                                "query_state_owner": "stable state id or short owner",
                                "target_identity_hints": ["semantic identity terms for result target"],
                                "target_identity_status": "match|mismatch|not_applicable|unknown",
                                "result_projection": "short description of what component/results should read from query",
                                "reason_code": "short_snake_case",
                                "confidence": 0.0,
                            }
                        ]
                    },
                    "principles": [
                        "Do not mark search entry pages as mismatches just because they are not final result pages.",
                        "For committed result/detail targets, compare the typed query with the target identity.",
                        "For selected suggestion commits, compare the selected/applied query with the target identity and preserve the draft value only as parameter evidence.",
                        "If the query and target clearly disagree, output mismatch with the target identity hints.",
                        "Observed query values are examples of parameterized input, not initial DOM values.",
                    ],
                    "query_tasks": [_compact_query_task_for_llm(task) for task in tasks],
                },
                ensure_ascii=False,
            ),
        },
    ]
    reply, usage = await call_openai_api_json_async(
        messages,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        model=model,
        stage="page_environment_query_identity_enricher",
    )
    try:
        payload = json.loads(reply) if isinstance(reply, str) else reply
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["_usage"] = usage
    return payload


def _validate_query_identity_resolution(task: Dict[str, Any], raw: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "query_id": str(task.get("query_id") or ""),
            "query_event_id": str(task.get("query_event_id") or ""),
            "query_role": str(task.get("query_role") or ""),
            "provenance": task.get("provenance", []) if isinstance(task.get("provenance"), list) else [],
            "query_transition_kind": "unknown",
            "query_state_owner": "",
            "target_identity_hints": [],
            "target_identity_status": "unknown",
            "result_projection": "",
            "reason_code": "missing_llm_resolution",
            "confidence": 0.0,
        }
    kind = str(raw.get("query_transition_kind") or "unknown")
    if kind not in QUERY_TRANSITION_KINDS:
        kind = "unknown"
    status = str(raw.get("target_identity_status") or "unknown")
    if status not in QUERY_IDENTITY_STATUSES:
        status = "unknown"
    hints = _dedupe(raw.get("target_identity_hints", []) if isinstance(raw.get("target_identity_hints"), list) else [], limit=12)
    if kind in {"query_entry", "suggestion_projection", "unrelated_navigation"}:
        status = "not_applicable"
    return {
        "query_id": str(task.get("query_id") or raw.get("query_id") or ""),
        "query_event_id": str(task.get("query_event_id") or raw.get("query_event_id") or ""),
        "query_role": str(task.get("query_role") or raw.get("query_role") or ""),
        "provenance": task.get("provenance", []) if isinstance(task.get("provenance"), list) else [],
        "query": str(task.get("query") or ""),
        "source_filename": _base_filename(task.get("source_filename")),
        "target_filename": _base_filename(task.get("target_filename")),
        "step_key": str(task.get("step_key") or ""),
        "draft_ref": str(task.get("draft_ref") or ""),
        "query_transition_kind": kind,
        "query_state_owner": str(raw.get("query_state_owner") or ""),
        "target_identity_hints": hints,
        "target_identity_status": status,
        "result_projection": _clip_text(raw.get("result_projection"), 180),
        "reason_code": str(raw.get("reason_code") or "llm_query_identity"),
        "confidence": raw.get("confidence", 0),
    }


def _apply_query_identity_resolutions(
    payload: Dict[str, Any],
    resolutions: Sequence[Dict[str, Any]],
) -> None:
    by_id = {str(item.get("query_id") or ""): item for item in resolutions if isinstance(item, dict)}
    payload["query_identity_resolution"] = {
        "format": "page_environment_query_identity_resolution",
        "schema_revision": 1,
        "resolution_count": len(by_id),
        "resolutions": list(by_id.values()),
    }
    by_step_query: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_step_query_route: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    by_event_id: Dict[str, Dict[str, Any]] = {}
    by_session_key: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for item in by_id.values():
        event_id = str(item.get("query_event_id") or "")
        if event_id:
            by_event_id[event_id] = item
        key = (str(item.get("step_key") or ""), str(item.get("query") or ""))
        if key[0] and key[1]:
            by_step_query[key] = item
        route_key = (
            str(item.get("step_key") or ""),
            str(item.get("query") or ""),
            _base_filename(item.get("source_filename")),
            _base_filename(item.get("target_filename")),
        )
        if all(route_key):
            by_step_query_route[route_key] = item
        session_key = (
            str(item.get("draft_ref") or ""),
            str(item.get("step_key") or ""),
            str(item.get("target_filename") or ""),
            str(item.get("query") or ""),
        )
        if all(session_key):
            by_session_key[session_key] = item

    for spec in payload.get("specs", []) if isinstance(payload, dict) else []:
        if not isinstance(spec, dict):
            continue
        trace_id = str(spec.get("trace_id") or "")
        for session in spec.get("input_sessions", []) if isinstance(spec.get("input_sessions"), list) else []:
            if not isinstance(session, dict):
                continue
            for entry in _query_field_entries_from_session(session):
                query = entry["value"]
                query_role = entry["field_role"]
                event_id = _query_event_id(
                    trace_id=trace_id,
                    step_key=str(session.get("committed_by_step") or ""),
                    query_role=query_role,
                    source=_base_filename(session.get("source_filename")),
                    target=_base_filename(session.get("target_filename")),
                    query=query,
                )
                query_id = _query_task_id(
                    kind="session",
                    trace_id=trace_id,
                    step_key=str(session.get("committed_by_step") or ""),
                    draft_ref=str(session.get("draft_ref") or ""),
                    source=_base_filename(session.get("source_filename")),
                    target=_base_filename(session.get("target_filename")),
                    query=query,
                )
                resolution = by_event_id.get(event_id) or by_id.get(query_id) or by_session_key.get((
                    str(session.get("draft_ref") or ""),
                    str(session.get("committed_by_step") or ""),
                    _base_filename(session.get("target_filename")),
                    query,
                ))
                if resolution:
                    session["query_event_id"] = str(resolution.get("query_event_id") or event_id)
                    session["query_identity_resolution_id"] = str(resolution.get("query_id") or "")
                    session["query_transition_kind"] = resolution.get("query_transition_kind", "")
                    session["query_state_owner"] = resolution.get("query_state_owner", "")
                    session["target_identity_hints"] = resolution.get("target_identity_hints", [])
                    session["target_identity_status"] = resolution.get("target_identity_status", "")
                    session["result_projection"] = resolution.get("result_projection", "")
                    if str(resolution.get("query_transition_kind") or "") == "selected_suggestion_commit":
                        session["draft_query_values"] = _query_fields_from_session(session)
                        applied_values = _applied_query_values(resolution)
                        if applied_values:
                            session["committed_query_values"] = applied_values
        for transition in spec.get("transitions", []) if isinstance(spec.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            for entry in _query_field_entries_from_effect(effect):
                query = entry["value"]
                query_role = entry["field_role"]
                event_id = _query_event_id(
                    trace_id=trace_id,
                    step_key=str(transition.get("step_key") or ""),
                    query_role=query_role,
                    source=_base_filename(transition.get("source_filename")),
                    target=_base_filename(transition.get("target_filename")),
                    query=query,
                )
                query_id = _query_task_id(
                    kind="transition",
                    trace_id=trace_id,
                    step_key=str(transition.get("step_key") or ""),
                    draft_ref=str(effect.get("draft_ref") or ""),
                    source=_base_filename(transition.get("source_filename")),
                    target=_base_filename(transition.get("target_filename")),
                    query=query,
                )
                route_key = (
                    str(transition.get("step_key") or ""),
                    query,
                    _base_filename(transition.get("source_filename")),
                    _base_filename(transition.get("target_filename")),
                )
                resolution = (
                    by_event_id.get(event_id)
                    or by_id.get(query_id)
                    or by_step_query_route.get(route_key)
                    or by_step_query.get((str(transition.get("step_key") or ""), query))
                )
                if resolution:
                    transition["query_event_id"] = str(resolution.get("query_event_id") or event_id)
                    transition["query_identity_resolution_id"] = str(resolution.get("query_id") or "")
                    transition["query_transition_kind"] = resolution.get("query_transition_kind", "")
                    transition["query_state_owner"] = resolution.get("query_state_owner", "")
                    transition["target_identity_hints"] = resolution.get("target_identity_hints", [])
                    transition["target_identity_status"] = resolution.get("target_identity_status", "")
                    transition["result_projection"] = resolution.get("result_projection", "")
                    effect["query_transition_kind"] = resolution.get("query_transition_kind", "")
                    effect["query_state_owner"] = resolution.get("query_state_owner", "")
                    effect["target_identity_hints"] = resolution.get("target_identity_hints", [])
                    effect["target_identity_status"] = resolution.get("target_identity_status", "")
                    effect["result_projection"] = resolution.get("result_projection", "")
                    if str(resolution.get("query_transition_kind") or "") == "selected_suggestion_commit":
                        applied_values = _applied_query_values(resolution)
                        if applied_values:
                            transition["committed_query_values"] = applied_values
                            effect["committed_query_values"] = applied_values

    page_envs = payload.get("page_environment_specs_by_filename", {}) if isinstance(payload, dict) else {}
    for page_env in page_envs.values() if isinstance(page_envs, dict) else []:
        if not isinstance(page_env, dict):
            continue
        for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            query_values = _query_fields_from_effect(effect)
            if transition.get("input_text"):
                query_values.append(str(transition.get("input_text") or ""))
            for query in _dedupe(query_values, limit=4):
                route_key = (
                    str(transition.get("step_key") or ""),
                    query,
                    _base_filename(transition.get("source_filename")),
                    _base_filename(transition.get("target_filename")),
                )
                resolution = by_step_query_route.get(route_key) or by_step_query.get((str(transition.get("step_key") or ""), query))
                if not resolution:
                    continue
                if resolution.get("query_event_id"):
                    transition["query_event_id"] = str(resolution.get("query_event_id") or "")
                transition["query_identity_resolution_id"] = str(resolution.get("query_id") or "")
                transition["query_transition_kind"] = resolution.get("query_transition_kind", "")
                transition["query_state_owner"] = resolution.get("query_state_owner", "")
                transition["target_identity_hints"] = resolution.get("target_identity_hints", [])
                transition["target_identity_status"] = resolution.get("target_identity_status", "")
                transition["result_projection"] = resolution.get("result_projection", "")
        for parameter in page_env.get("parameterized_inputs", []) if isinstance(page_env.get("parameterized_inputs"), list) else []:
            if not isinstance(parameter, dict) or str(parameter.get("field_role") or "") not in SEARCHLIKE_FIELD_ROLES:
                continue
            observed = parameter.get("observed_values", []) if isinstance(parameter.get("observed_values"), list) else []
            for query in observed:
                resolution = next(
                    (
                        item for item in by_id.values()
                        if item.get("query") == query
                        and item.get("source_filename") == parameter.get("source_filename")
                        and item.get("target_filename") == parameter.get("target_filename")
                    ),
                    None,
                )
                if resolution:
                    parameter["query_state_owner"] = resolution.get("query_state_owner", "")
                    parameter["query_transition_kind"] = resolution.get("query_transition_kind", "")
                    parameter["target_identity_hints"] = resolution.get("target_identity_hints", [])
                    parameter["target_identity_status"] = resolution.get("target_identity_status", "")
                    if str(resolution.get("query_transition_kind") or "") == "selected_suggestion_commit":
                        parameter["draft_observed_values"] = _dedupe(
                            [str(item or "") for item in observed if str(item or "").strip()],
                            limit=8,
                        )
                        applied_values = _applied_query_values(resolution)
                        if applied_values:
                            parameter["committed_query_values"] = applied_values


def _query_event_admission_summary(query_tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    provenance_counts = defaultdict(int)
    merged_count = 0
    for task in query_tasks:
        provenance = task.get("provenance", []) if isinstance(task.get("provenance"), list) else []
        if len(provenance) > 1:
            merged_count += 1
        key = "+".join(provenance) if provenance else "unknown"
        provenance_counts[key] += 1
    return {
        "format": "page_environment_query_event_admission",
        "schema_revision": 1,
        "canonical_event_count": len(query_tasks),
        "merged_provenance_event_count": merged_count,
        "provenance_counts": dict(sorted(provenance_counts.items())),
        "status": "ready",
    }


def _component_task_for_page(
    filename: str,
    page_env: Dict[str, Any],
    *,
    page_metadata: Dict[str, Dict[str, Any]],
    page_content_context: Dict[str, Dict[str, Any]],
    runtime_observations_by_trace_cluster: Dict[Tuple[str, str], List[Dict[str, Any]]],
    interaction_contracts_by_filename: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any] | None:
    transitions = [
        {
            "transition_id": item.get("transition_id", ""),
            "step_key": item.get("step_key", ""),
            "operation": item.get("operation", ""),
            "reducer": item.get("reducer", ""),
            "source_filename": item.get("source_filename", ""),
            "target_filename": item.get("target_filename", ""),
            "input_text": item.get("input_text", ""),
            "state_effect": item.get("state_effect", {}),
            "query_transition_kind": item.get("query_transition_kind", ""),
            "target_identity_hints": item.get("target_identity_hints", []),
            "l0_role": item.get("l0_role", ""),
        }
        for item in page_env.get("transitions", [])
        if isinstance(item, dict) and str(item.get("l0_role") or "") == "execution_anchor"
    ]
    if not transitions and not page_env.get("conflict_resolutions"):
        return None
    page_meta = page_metadata.get(filename, {})
    general_page_id = str(page_env.get("general_page_id") or page_meta.get("general_page_id") or "")
    trace_ids = (
        page_env.get("provenance", {}).get("source_trace_ids", [])
        if isinstance(page_env.get("provenance"), dict)
        else []
    )
    page_observations: List[Dict[str, Any]] = []
    for trace_id in trace_ids[:8]:
        page_observations.extend(
            runtime_observations_by_trace_cluster.get((str(trace_id), general_page_id), [])[:8]
        )
    return {
        "filename": filename,
        "page_name": page_env.get("page_name", ""),
        "general_page_id": page_env.get("general_page_id", ""),
        "materialized_page_key": page_env.get("materialized_page_key", ""),
        "content_context": page_content_context.get(filename, {}),
        "runtime_observations": page_observations[:24],
        "interaction_contracts": interaction_contracts_by_filename.get(filename, [])[:16],
        "parameterized_inputs": page_env.get("parameterized_inputs", [])[:12],
        "transitions": transitions[:48],
        "oracles": page_env.get("oracles", [])[:32],
        "conflict_resolutions": [
            {
                "conflict_type": item.get("conflict_type", ""),
                "surface": item.get("surface", ""),
                "selected_default": item.get("selected_default", ""),
                "state_variants": item.get("state_variants", []),
                "runtime_only": item.get("runtime_only", []),
                "section_render": item.get("section_render", []),
                "component_splits": item.get("component_splits", []),
                "candidate_summary": item.get("candidate_summary", {}),
            }
            for item in page_env.get("conflict_resolutions", [])[:12]
            if isinstance(item, dict)
        ],
        "existing_render_projections": page_env.get("render_projections", [])[:12],
    }


async def _enrich_component_projection_chunk(
    page_tasks: Sequence[Dict[str, Any]],
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_tokens: int = 16000,
) -> Dict[str, Any]:
    from trace2world_pipeline.core.llm_caller import call_openai_api_json_async

    messages = [
        {
            "role": "system",
            "content": (
                "You compile page_environment_spec component and render projection records. "
                "Return JSON only. Use the provided transitions, state effects, and conflict resolutions. "
                "Also use runtime observations, page content lifecycle, entity-component mappings, and interaction contracts. "
                "Do not discuss resources or images. Do not invent app-specific content beyond the evidence. "
                "Every L0 transition should be covered by a component and a render projection where visible."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "Produce deterministic component/render projection records for these pages.",
                    "output_schema": {
                        "pages": [
                            {
                                "filename": "string",
                                "components": [
                                    {
                                        "component_id": "stable id",
                                        "component_kind": "input|button|fixed_slot_nav|feed|list|form|overlay|section|card|unknown",
                                        "component_role": "short semantic role",
                                        "reads_state": "state ref or query/draft owner",
                                        "render_phase": "initial|runtime|post_transition",
                                        "layout_policy": "fixed_capacity|appendable_list|form_field|query_projection|navigation_control|state_projection",
                                        "source_step_keys": ["step ids"],
                                        "l0_transition_ids": ["transition ids"],
                                        "confidence": 0.0
                                    }
                                ],
                                "render_projections": [
                                    {
                                        "projection_id": "stable id",
                                        "component_ref": "component_id",
                                        "reads_state": "state ref or query/draft owner",
                                        "render_phase": "initial|runtime|post_transition",
                                        "layout_policy": "short policy",
                                        "source_step_keys": ["step ids"],
                                        "l0_transition_ids": ["transition ids"]
                                    }
                                ],
                                "oracle_requirements": [
                                    {
                                        "oracle_type": "component_interactable|state_mutation|route_target|collection_preservation",
                                        "step_key": "step id",
                                        "component_ref": "component id",
                                        "expected": "short expected condition"
                                    }
                                ]
                            }
                        ]
                    },
                    "principles": [
                        "Resources/media are out of scope.",
                        "Do not leave L0 transitions as unbound action logs.",
                        "Fixed slots should be slot-based and not duplicate mutually exclusive labels.",
                        "Query inputs read query/draft state; result sections read committed query state.",
                        "For each L0 transition, emit semantic oracle_requirements rather than text-only checks: component_interactable, route_target, state_mutation, and collection_preservation as applicable.",
                        "Runtime-only content is rendered only after matching transition.",
                        "Do not create new cmp::*::l0::* component ids. Reuse existing L0 component refs when they exist; otherwise describe non-L0 semantic components only.",
                    ],
                    "pages": list(page_tasks),
                },
                ensure_ascii=False,
            ),
        },
    ]
    reply, usage = await call_openai_api_json_async(
        messages,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        model=model,
        stage=L0_COMPONENT_RENDER_BINDING_STAGE,
    )
    try:
        payload = json.loads(reply) if isinstance(reply, str) else reply
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["_usage"] = usage
    return payload


def _normalize_component_record(filename: str, raw: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    component_id = _safe_id(raw.get("component_id") or raw.get("component_role") or raw.get("component_kind") or "component")
    if not component_id.startswith("cmp::"):
        component_id = f"cmp::{_safe_id(filename)}::{component_id}"
    return {
        "component_id": component_id,
        "component_kind": str(raw.get("component_kind") or "unknown"),
        "component_role": _clip_text(raw.get("component_role"), 120),
        "reads_state": str(raw.get("reads_state") or ""),
        "render_phase": str(raw.get("render_phase") or "initial"),
        "layout_policy": str(raw.get("layout_policy") or "state_projection"),
        "source_step_keys": _dedupe(raw.get("source_step_keys", []) if isinstance(raw.get("source_step_keys"), list) else [], limit=24),
        "l0_transition_ids": _dedupe(raw.get("l0_transition_ids", []) if isinstance(raw.get("l0_transition_ids"), list) else [], limit=24),
        "confidence": raw.get("confidence", 0),
        "source": "llm_page_environment_spec_enrichment",
    }


def _normalize_render_projection(filename: str, raw: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    component_ref = str(raw.get("component_ref") or "")
    if not component_ref:
        return None
    projection_id = _safe_id(raw.get("projection_id") or f"projection::{component_ref}")
    if not projection_id.startswith("projection::"):
        projection_id = f"projection::{_safe_id(filename)}::{projection_id}"
    return {
        "projection_id": projection_id,
        "component_ref": component_ref,
        "reads_state": str(raw.get("reads_state") or ""),
        "render_phase": str(raw.get("render_phase") or "initial"),
        "layout_policy": str(raw.get("layout_policy") or "state_projection"),
        "source_step_keys": _dedupe(raw.get("source_step_keys", []) if isinstance(raw.get("source_step_keys"), list) else [], limit=24),
        "l0_transition_ids": _dedupe(raw.get("l0_transition_ids", []) if isinstance(raw.get("l0_transition_ids"), list) else [], limit=24),
        "source": "llm_page_environment_spec_enrichment",
    }


def _l0_component_steps(component: Dict[str, Any]) -> set[str]:
    steps: set[str] = set()
    for field in ("source_step_keys", "l0_transition_ids"):
        values = component.get(field)
        if isinstance(values, list):
            steps.update(str(value) for value in values if str(value or "").strip())
    return steps


def _l0_component_direction(component_id: str) -> str:
    suffix = str(component_id or "").rsplit("::", 1)[-1]
    return suffix if suffix in {"source", "target"} else ""


def _coalesce_llm_l0_component_id(
    page_env: Dict[str, Any],
    component: Dict[str, Any],
) -> tuple[str, bool]:
    component_id = str(component.get("component_id") or "")
    if "::l0::" not in component_id:
        return component_id, False
    if str(component.get("source") or "") != "llm_page_environment_spec_enrichment":
        return component_id, False
    steps = _l0_component_steps(component)
    direction = _l0_component_direction(component_id)
    components = page_env.get("components") if isinstance(page_env, dict) else []
    if isinstance(components, list):
        for existing in components:
            if not isinstance(existing, dict):
                continue
            existing_id = str(existing.get("component_id") or "")
            if "::l0::" not in existing_id:
                continue
            if str(existing.get("source") or "") == "llm_page_environment_spec_enrichment":
                continue
            if direction and _l0_component_direction(existing_id) not in {"", direction}:
                continue
            existing_steps = _l0_component_steps(existing)
            if steps and existing_steps and not steps.intersection(existing_steps):
                continue
            if steps or existing_steps:
                return existing_id, True
    return component_id, True


def _state_ref_tokens(value: Any) -> List[str]:
    if not isinstance(value, str):
        return []
    refs: List[str] = []
    for token in re.split(r"\s*\+\s*|\s*,\s*", value):
        token = token.strip()
        if not token:
            continue
        if token.startswith(("state::", "query::", "draft::")):
            refs.append(token)
        else:
            refs.append(_state_ref(token))
    return _dedupe(refs, limit=16)


def _ensure_runtime_state_refs(page_env: Dict[str, Any], reads_state: Any, *, source: str) -> None:
    refs = _state_ref_tokens(reads_state)
    if not refs:
        return
    state_schema = page_env.setdefault("state_schema", {})
    if not isinstance(state_schema, dict):
        page_env["state_schema"] = {}
        state_schema = page_env["state_schema"]
    bucket = state_schema.setdefault("runtime_state_refs", [])
    if not isinstance(bucket, list):
        state_schema["runtime_state_refs"] = []
        bucket = state_schema["runtime_state_refs"]
    normalized_bucket: List[Dict[str, Any]] = []
    for existing in bucket:
        if isinstance(existing, dict):
            existing_ref = str(existing.get("state_ref") or "").strip()
            if not existing_ref:
                existing_scope = str(existing.get("state_scope") or "").strip()
                if existing_scope:
                    existing_ref = _state_ref(existing_scope)
                    existing["state_ref"] = existing_ref
            if existing_ref and not existing.get("state_scope"):
                existing["state_scope"] = existing_ref.replace("state::", "")
            if existing_ref:
                _add_unique_dict(normalized_bucket, existing, ("state_ref",))
            continue
        for existing_ref in _state_ref_tokens(existing):
            _add_unique_dict(
                normalized_bucket,
                {
                    "state_ref": existing_ref,
                    "state_scope": existing_ref.replace("state::", ""),
                    "source": "legacy_runtime_state_ref_string",
                },
                ("state_ref",),
            )
    state_schema["runtime_state_refs"] = normalized_bucket
    bucket = normalized_bucket
    for ref in refs:
        _add_unique_dict(
            bucket,
            {
                "state_ref": ref,
                "state_scope": ref.replace("state::", ""),
                "source": source,
            },
            ("state_ref",),
        )


def _apply_component_projection_enrichment(
    payload: Dict[str, Any],
    page_outputs: Sequence[Dict[str, Any]],
) -> None:
    page_envs = payload.get("page_environment_specs_by_filename", {}) if isinstance(payload, dict) else {}
    if not isinstance(page_envs, dict):
        return
    enriched_filenames: set[str] = set()
    discarded_oracle_count = 0
    for page_output in page_outputs:
        if not isinstance(page_output, dict):
            continue
        filename = _base_filename(page_output.get("filename"))
        page_env = page_envs.get(filename)
        if not isinstance(page_env, dict):
            continue
        component_id_map: Dict[str, str] = {}
        dropped_llm_l0_refs: set[str] = set()
        for raw_component in page_output.get("components", []) if isinstance(page_output.get("components"), list) else []:
            component = _normalize_component_record(filename, raw_component)
            if not component:
                continue
            old_id = str(raw_component.get("component_id") or "")
            component_id, skip_llm_l0 = _coalesce_llm_l0_component_id(page_env, component)
            if old_id:
                component_id_map[old_id] = component_id
            if skip_llm_l0:
                current_component_ids = _component_ids(page_env)
                if component_id not in current_component_ids and old_id:
                    dropped_llm_l0_refs.add(old_id)
                continue
            component["component_id"] = component_id
            _add_unique_dict(page_env.setdefault("components", []), component, ("component_id",))
            _ensure_runtime_state_refs(
                page_env,
                component.get("reads_state"),
                source="llm_page_environment_spec_enrichment",
            )
        for raw_projection in page_output.get("render_projections", []) if isinstance(page_output.get("render_projections"), list) else []:
            if isinstance(raw_projection, dict):
                ref = str(raw_projection.get("component_ref") or "")
                if ref in dropped_llm_l0_refs:
                    continue
                if ref in component_id_map:
                    raw_projection = dict(raw_projection, component_ref=component_id_map[ref])
            projection = _normalize_render_projection(filename, raw_projection)
            if projection:
                _add_unique_dict(page_env.setdefault("render_projections", []), projection, ("projection_id",))
                _ensure_runtime_state_refs(
                    page_env,
                    projection.get("reads_state"),
                    source="llm_page_environment_spec_enrichment",
                )
        for oracle in page_output.get("oracle_requirements", []) if isinstance(page_output.get("oracle_requirements"), list) else []:
            if not isinstance(oracle, dict):
                continue
            component_ref = str(oracle.get("component_ref") or "")
            if component_ref in component_id_map:
                component_ref = component_id_map[component_ref]
            current_component_ids = _component_ids(page_env)
            if component_ref and component_ref not in current_component_ids:
                discarded_oracle_count += 1
                continue
            _add_unique_dict(
                page_env.setdefault("oracles", []),
                {
                    "oracle_type": str(oracle.get("oracle_type") or "component_interactive"),
                    "step_key": str(oracle.get("step_key") or ""),
                    "component_ref": component_ref,
                    "expected": _clip_text(oracle.get("expected"), 160),
                    "source": "llm_page_environment_spec_enrichment",
                },
                ("oracle_type", "step_key", "component_ref"),
            )
        page_enrichment = {
            "source": "llm_page_environment_spec_enrichment",
            "component_count": len(page_env.get("components", []) if isinstance(page_env.get("components"), list) else []),
            "render_projection_count": len(page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []),
        }
        page_env["l0_component_render_binding_enrichment"] = page_enrichment
        page_env["component_projection_enrichment"] = page_enrichment
        enriched_filenames.add(filename)
    payload_enrichment = {
        "format": L0_COMPONENT_RENDER_BINDING_ENRICHMENT_FORMAT,
        "schema_revision": 1,
        "page_count": len(enriched_filenames),
        "discarded_oracle_count": discarded_oracle_count,
    }
    payload["l0_component_render_binding_enrichment"] = payload_enrichment
    payload["component_projection_enrichment"] = payload_enrichment
    close_page_environment_contracts(payload)


def _transition_component_kind(transition: Dict[str, Any]) -> str:
    reducer = str(transition.get("reducer") or "")
    operation = str(transition.get("operation") or "")
    if reducer in {"fill_form", "type_search", "project_suggestions"} or operation == "fill":
        return "input"
    if reducer in {"select_option", "select_chip", "select_overlay_option"} or operation == "select":
        return "selection_control"
    if reducer in {"select_filter_option", "apply_filter", "switch_tab"}:
        return "selection_control"
    if reducer in {"open_overlay", "open_option_modal"}:
        return "overlay_trigger"
    if reducer in {"update_cart_quantity", "add_to_cart", "remove_from_cart"}:
        return "cart_quantity_control"
    if reducer in {"navigate_route", "open_search_entry"}:
        return "navigation_control"
    if reducer == "scroll_viewport" or operation in {"scroll", "swipe"}:
        return "scroll_surface"
    if reducer in {"submit_form", "commit_query"}:
        return "submit_control"
    if operation == "click":
        return "button_or_card"
    return "interactive_component"


def _transition_reads_state(transition: Dict[str, Any]) -> str:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    return (
        str(transition.get("query_state_owner") or "")
        or str(effect.get("query_state_owner") or "")
        or str(effect.get("draft_ref") or "")
        or str(effect.get("state_ref") or "")
        or "state::page_runtime"
    )


def _effect_requires_record_identity(effect: Dict[str, Any]) -> bool:
    if not isinstance(effect, dict):
        return False
    effect_kind = str(effect.get("effect") or "")
    if effect_kind == "state_delta_signature":


        if not str(effect.get("collection_merge_policy") or ""):
            return False
    elif effect_kind != "commit_draft":
        return False
    if str(effect.get("query_transition_kind") or "") in {"query_entry", "suggestion_projection"}:
        return False
    fields = effect.get("committed_fields", []) if isinstance(effect.get("committed_fields"), list) else []
    field_roles = [
        str(field.get("field_role") or "")
        for field in fields
        if isinstance(field, dict)
    ]
    if field_roles and all(role in SEARCHLIKE_FIELD_ROLES for role in field_roles):
        return False
    if any(role == "business_query" for role in field_roles):
        target_status = str(effect.get("target_identity_status") or "")
        target_kind = str(effect.get("query_transition_kind") or "")
        if target_status == "not_applicable" or target_kind == "query_entry":
            return False
    return any(isinstance(field, dict) for field in fields)


def _record_collection_ref(effect: Dict[str, Any]) -> str:
    explicit = str(effect.get("collection_state_ref") or effect.get("collection_ref") or "")
    if explicit:
        return explicit if explicit.startswith("state::") else _state_ref(explicit)
    draft_ref = str(effect.get("draft_ref") or "committed_record")
    return _state_ref(f"committed_records::{draft_ref}")


def _ensure_record_identity_contract(page_env: Dict[str, Any], transition: Dict[str, Any]) -> None:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    if not _effect_requires_record_identity(effect):
        return
    state_ref = _record_collection_ref(effect)
    fields = effect.get("committed_fields", []) if isinstance(effect.get("committed_fields"), list) else []
    identity_fields = _dedupe(
        [
            str(field.get("field_role") or field.get("name") or "")
            for field in fields
            if isinstance(field, dict)
        ],
        limit=12,
    )
    schema = {
        "state_ref": state_ref,
        "state_scope": state_ref.replace("state::", ""),
        "record_identity": "canonical_record_identity_from_committed_fields",
        "identity_fields": identity_fields,
        "merge_policy": str(effect.get("collection_merge_policy") or "preserve_existing_then_append_or_upsert"),
        "source": "page_environment_record_identity_compiler",
    }
    _add_unique_dict(
        page_env.setdefault("state_schema", {}).setdefault("collections", []),
        schema,
        ("state_ref",),
    )
    page_env.setdefault("initial_state", {}).setdefault("collections", {}).setdefault(
        state_ref,
        {
            "state_scope": schema["state_scope"],
            "min_initial_count": 0,
            "seed_policy": "observed_initial_state_or_empty_collection",
        },
    )
    _add_unique_dict(
        page_env.setdefault("content_seed", {}).setdefault("collections", []),
        {
            "state_ref": state_ref,
            "state_scope": schema["state_scope"],
            "min_initial_count": 0,
            "seed_policy": "observed_initial_state_or_empty_collection",
            "source": "page_environment_record_identity_compiler",
        },
        ("state_ref",),
    )


def _wheel_time_picker_overlay_contract(transition: Dict[str, Any], effect: Dict[str, Any]) -> Dict[str, Any]:
    intent = str(transition.get("operation_intent") or transition.get("command") or "")
    target_date = "明天" if "明天" in intent else "今天"
    hour_match = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：点时]", intent)
    minute_match = re.search(r"[:：点时]([0-5]?\d)", intent)
    target_hour = (hour_match.group(1) if hour_match else "08").zfill(2)
    target_minute = (minute_match.group(1) if minute_match else "00").zfill(2)
    return {
        "template_id": "wheel_time_picker",
        "overlay_kind": "departure_time_picker",
        "initial_visible": False,
        "must_fit_within_app_shell": True,
        "bottom_actions_fully_visible": True,
        "trigger_element_id": str(effect.get("trigger_element_id") or "btn_depart_now"),
        "columns": [
            {"id": "date", "label": "日期", "options": ["今天", "明天", "后天"], "target": target_date},
            {"id": "hour", "label": "小时", "options": [f"{hour:02d}" for hour in range(0, 24)], "target": target_hour},
            {"id": "minute", "label": "分钟", "options": ["00", "05", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55"], "target": target_minute},
        ],
        "confirm_target_filename": str(effect.get("confirm_target_filename") or transition.get("target_filename") or ""),
        "sdk_commit": {
            "method": "setTaxiDepartureTime",
            "args": ["bookingContextId", "departureTimestamp"],
        },
    }


def _year_month_wheel_picker_overlay_contract(transition: Dict[str, Any], effect: Dict[str, Any]) -> Dict[str, Any]:
    intent = str(transition.get("operation_intent") or transition.get("command") or "")
    year_match = re.search(r"(?<!\d)(20\d{2})(?:\s*年)?", intent)
    month_match = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*月", intent)
    target_year = year_match.group(1) if year_match else "2026"
    target_month = str(int(month_match.group(1))) if month_match else "9"
    year_value = int(target_year) if target_year.isdigit() else 2026
    years = [str(year) for year in range(max(2000, year_value - 3), year_value + 4)]
    return {
        "template_id": "wheel_picker",
        "overlay_kind": "year_month_picker",
        "initial_visible": False,
        "must_fit_within_app_shell": True,
        "bottom_actions_fully_visible": True,
        "trigger_element_id": str(effect.get("trigger_element_id") or "btn-select-time"),
        "columns": [
            {"id": "year", "label": "年份", "options": years, "target": target_year},
            {"id": "month", "label": "月份", "options": [str(month) for month in range(1, 13)], "target": target_month},
        ],
        "confirm_target_filename": str(effect.get("confirm_target_filename") or transition.get("target_filename") or ""),
        "sdk_commit": {
            "method": "setWheelPickerSelection",
            "args": ["selectedValues", "stateRef"],
        },
    }


def _ensure_projection_component_records(page_env: Dict[str, Any]) -> None:
    filename = _base_filename(page_env.get("filename") or page_env.get("page_id") or "page")
    for component in page_env.get("components", []) if isinstance(page_env.get("components"), list) else []:
        if isinstance(component, dict):
            _ensure_runtime_state_refs(
                page_env,
                component.get("reads_state"),
                source=str(component.get("source") or "page_environment_component_state_normalization"),
            )
    component_ids = _component_ids(page_env)
    for projection in page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []:
        if not isinstance(projection, dict):
            continue
        _ensure_runtime_state_refs(
            page_env,
            projection.get("reads_state"),
            source=str(projection.get("source") or "page_environment_projection_state_normalization"),
        )
        component_ref = str(projection.get("component_ref") or "")
        if not component_ref or component_ref in component_ids:
            continue
        component_id = component_ref if component_ref.startswith("cmp::") else f"cmp::{_safe_id(filename)}::{_safe_id(component_ref)}"
        projection["component_ref"] = component_id
        _add_unique_dict(
            page_env.setdefault("components", []),
            {
                "component_id": component_id,
                "component_kind": "state_projection",
                "component_role": _clip_text(component_ref, 120),
                "reads_state": str(projection.get("reads_state") or ""),
                "render_phase": str(projection.get("render_phase") or "initial"),
                "layout_policy": str(projection.get("layout_policy") or "state_projection"),
                "source_step_keys": projection.get("source_step_keys", projection.get("evidence_steps", [])) if isinstance(projection.get("source_step_keys", projection.get("evidence_steps", [])), list) else [],
                "l0_transition_ids": projection.get("l0_transition_ids", []) if isinstance(projection.get("l0_transition_ids", []), list) else [],
                "source": "page_environment_projection_component_normalization",
            },
            ("component_id",),
        )
        _ensure_runtime_state_refs(
            page_env,
            projection.get("reads_state"),
            source="page_environment_projection_component_normalization",
        )
        component_ids.add(component_id)


def _sanitize_future_state_hints(item: Dict[str, Any]) -> None:
    if not isinstance(item, dict):
        return
    hints = item.get("target_identity_hints")
    if not isinstance(hints, list):
        return
    reducer = str(item.get("reducer") or "")
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    effect_name = str(effect.get("effect") or "")
    may_keep = reducer in {"select_filter_option", "apply_filter"} or effect_name in {"update_selection", "commit_selection"}
    kept: List[str] = []
    deferred: List[str] = []
    for hint in hints:
        text = str(hint or "").strip()
        if not text:
            continue
        if (not may_keep) and _contains_any(text, FUTURE_QUERY_STATE_HINT_MARKERS):
            deferred.append(text)
            continue
        kept.append(text)
    if deferred:
        item["target_identity_hints"] = _dedupe(kept, limit=32)
        item["deferred_runtime_state_hints"] = _dedupe(
            list(item.get("deferred_runtime_state_hints", []) if isinstance(item.get("deferred_runtime_state_hints"), list) else [])
            + deferred,
            limit=16,
        )
    if isinstance(effect, dict) and effect is not item:
        _sanitize_future_state_hints(effect)


def _generic_commit_has_state_binding(transition: Dict[str, Any]) -> bool:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    effect_name = str(effect.get("effect") or "")
    reducer = str(transition.get("reducer") or "")
    if _is_closed_state_transaction(transition):
        return True
    if effect_name == "commit_draft":
        return bool(effect.get("draft_ref") or effect.get("committed_fields"))
    if effect_name == "commit_selection" or reducer == "apply_filter":
        return bool(effect.get("state_ref") or effect.get("selection_kind"))
    if reducer in {"update_cart_quantity", "add_to_cart", "remove_from_cart"}:
        return bool(effect.get("state_ref") or effect.get("selected_record_ref") or effect.get("selected_label"))
    if reducer in {"open_option_modal", "open_overlay"}:
        return True
    if isinstance(effect.get("selected_record_hydration"), dict):
        return True
    if str(effect.get("query_transition_kind") or transition.get("query_transition_kind") or "") in {
        "committed_result",
        "selected_suggestion_commit",
    }:
        return bool(effect.get("query_state_owner") or transition.get("query_state_owner"))
    return False


def _structured_action_intent_for_transition(transition: Dict[str, Any]) -> str:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    reducer = str(transition.get("reducer") or "")
    effect_name = str(effect.get("effect") or "")
    operation = str(transition.get("operation") or "").lower()
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    field_role = str(effect.get("field_role") or "")
    query_kind = str(effect.get("query_transition_kind") or transition.get("query_transition_kind") or "")

    if operation in {"fill", "type", "input"}:
        if field_role in SEARCHLIKE_FIELD_ROLES:
            if query_kind == "suggestion_projection" or effect_name == "project_suggestions":
                return "project_suggestions"
            return "type_query"
        return "fill_form"
    if effect_name == "commit_draft":
        fields = effect.get("committed_fields", []) if isinstance(effect.get("committed_fields"), list) else []
        roles = {
            str(field.get("field_role") or "")
            for field in fields
            if isinstance(field, dict)
        }
        if roles & SEARCHLIKE_FIELD_ROLES:
            return "commit_query"
        return "save_form"
    if reducer == "open_search_entry" or effect_name == "open_search_entry":
        return "open_query_entry"
    if reducer == "project_suggestions" or effect_name == "project_suggestions":
        return "project_suggestions"
    if reducer in {"open_overlay", "open_option_modal"} or effect_name == "open_runtime_overlay":
        return "open_runtime_overlay"
    if reducer == "select_overlay_option" or effect_name == "select_overlay_option":
        return "select_overlay_option"
    if reducer in {"select_filter_option", "switch_tab"} or effect_name == "update_selection":
        return "select_state"
    if reducer == "apply_filter" or effect_name == "commit_selection":
        return "apply_filter"
    if reducer in {"update_cart_quantity", "add_to_cart"} or effect_name == "update_cart_quantity":
        return "add_to_cart"
    if reducer == "remove_from_cart":
        return "remove_from_cart"
    if isinstance(effect.get("selected_record_hydration"), dict):
        return "open_record_detail"
    if reducer == "navigate_route" or (target and target != source):
        return "open_page"
    if reducer == "activate_inline_state":
        return "activate_inline_state"
    if operation in {"scroll", "swipe"}:
        return "scroll_viewport"
    if operation == "open_app":
        return "open_app"
    return operation or "observe"


def _record_identity_for_transition(transition: Dict[str, Any]) -> str:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    for key in ("record_identity", "selected_record_ref", "product_id", "merchant_id", "item_ref"):
        value = str(effect.get(key) or transition.get(key) or "")
        if value:
            return value
    hydration = effect.get("selected_record_hydration") if isinstance(effect.get("selected_record_hydration"), dict) else {}
    if hydration:
        return str(hydration.get("record_identity") or hydration.get("source_record_ref") or "")
    return ""


def _query_identity_for_transition(transition: Dict[str, Any]) -> str:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    query_values = _query_values_for_projection_item(transition)
    if query_values:
        return f"query::{_safe_id(query_values[0])}"
    query_owner = str(effect.get("query_state_owner") or transition.get("query_state_owner") or "")
    return query_owner


def _state_identity_for_transition(page_env: Dict[str, Any], transition: Dict[str, Any]) -> Dict[str, Any]:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    identity = {
        "source_page_id": str(page_env.get("site_model_page_id") or page_env.get("page_id") or ""),
        "general_page_id": str(page_env.get("general_page_id") or ""),
        "materialized_page_key": str(page_env.get("materialized_page_key") or ""),
        "state_ref": str(
            effect.get("state_ref")
            or effect.get("draft_ref")
            or effect.get("query_state_owner")
            or transition.get("query_state_owner")
            or ""
        ),
    }
    return {key: value for key, value in identity.items() if value not in ("", [], {})}


def _attach_structured_transition_identity(
    page_env: Dict[str, Any],
    transition: Dict[str, Any],
    page_ids_by_filename: Dict[str, str],
) -> None:
    action_intent = _structured_action_intent_for_transition(transition)
    transition["action_intent"] = action_intent
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    current_materialized_id = str(
        page_env.get("materialized_page_id")
        or page_env.get("site_model_page_id")
        or page_env.get("page_id")
        or ""
    )
    identity_key = {
        "source_materialized_page_id": page_ids_by_filename.get(source, "") or current_materialized_id,
        "evidence_step": str(transition.get("step_key") or transition.get("transition_id") or ""),
        "action_intent": action_intent,
        "record_identity": _record_identity_for_transition(transition),
        "query_identity": _query_identity_for_transition(transition),
        "state_identity": _state_identity_for_transition(page_env, transition),
        "target_materialized_page_id": page_ids_by_filename.get(target, ""),
    }
    transition["transition_identity_key"] = {
        key: value
        for key, value in identity_key.items()
        if value not in ("", [], {})
    }


def _is_structured_commit_like_transition(transition: Dict[str, Any]) -> bool:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    reducer = str(transition.get("reducer") or "")
    effect_name = str(effect.get("effect") or "")
    action_intent = str(transition.get("action_intent") or "")
    return (
        action_intent in {
            "submit_specs",
            "checkout",
            "save_form",
            "commit_query",
            "apply_filter",
            "add_to_cart",
            "remove_from_cart",
        }
        or
        reducer in {
            "submit_form",
            "commit_query",
            "apply_filter",
            "submit_specs",
            "checkout",
            "save_form",
            "commit_visible_state",
            "blocked_generic_commit_without_state",
        }
        or effect_name in {
            "commit_draft",
            "commit_selection",
            "commit_visible_state",
            "submit_specs",
            "checkout",
            "save_form",
            "invalid_generic_commit_without_state",
        }
    )


def _has_runtime_overlay_or_option_state(page_env: Dict[str, Any]) -> bool:
    for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
        if not isinstance(transition, dict):
            continue
        reducer = str(transition.get("reducer") or "")
        effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
        if reducer in {"open_overlay", "open_option_modal"}:
            return True
        if str(effect.get("effect") or "") == "open_runtime_overlay":
            return True
        if effect.get("overlay_kind") or effect.get("selected_record_ref"):
            return True
    initial = page_env.get("initial_state", {}) if isinstance(page_env.get("initial_state"), dict) else {}
    overlays = initial.get("overlays", {}) if isinstance(initial.get("overlays"), dict) else {}
    return bool(overlays)


def _is_pure_route_navigation_transition(transition: Dict[str, Any]) -> bool:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    reducer = str(transition.get("reducer") or "")
    effect_name = str(effect.get("effect") or "")
    mutation_policy = str(effect.get("mutation_policy") or "")
    if reducer != "navigate_route" or effect_name != "navigate_route":
        return False
    if mutation_policy and mutation_policy != "route_transition_only":
        return False
    state_binding_keys = {
        "state_ref",
        "draft_ref",
        "selected_record_ref",
        "query_state_owner",
        "collection_merge_policy",
    }
    if any(effect.get(key) for key in state_binding_keys):
        return False
    if isinstance(effect.get("selected_record_hydration"), dict):
        return False
    return True


def _transition_requires_state_binding(page_env: Dict[str, Any], transition: Dict[str, Any]) -> bool:
    if _is_query_activation_transition(transition):
        return False
    if _is_pure_route_navigation_transition(transition):
        return False
    if _is_structured_commit_like_transition(transition):
        return True
    operation = str(transition.get("operation") or "").lower()
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    if operation not in {"click", "tap"} or not source or not target or source == target:
        return False
    if _has_runtime_overlay_or_option_state(page_env):
        return True
    return False


def _is_query_activation_transition(transition: Dict[str, Any]) -> bool:
    if not isinstance(transition, dict):
        return False
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    reducer = str(transition.get("reducer") or "")
    effect_name = str(effect.get("effect") or "")
    qkind = str(transition.get("query_transition_kind") or effect.get("query_transition_kind") or "")
    if reducer == "open_search_entry" or effect_name == "open_search_entry" or qkind == "query_entry":
        return True
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    intent = str(transition.get("operation_intent") or "")
    operation = str(transition.get("operation") or "").lower()
    return operation in {"click", "tap"} and _is_query_entry_click(source, target, intent)


def _block_generic_commit_without_state(transition: Dict[str, Any]) -> None:
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    transition["reducer"] = "blocked_generic_commit_without_state"
    transition["state_effect"] = {
        "effect": "invalid_generic_commit_without_state",
        "requires_state_binding": True,
        "source_filename": source,
        "target_filename": target,
        "mutation_policy": "block_naked_confirm_save_route",
        "source": "page_environment_generic_commit_gate",
    }
    transition["invalid_reason"] = "generic_action_without_selected_record"


def _ensure_structured_transition_identity(
    page_env: Dict[str, Any],
    transition: Dict[str, Any],
    page_ids_by_filename: Dict[str, str],
) -> None:
    if not transition.get("action_intent") or not isinstance(transition.get("transition_identity_key"), dict):
        _attach_structured_transition_identity(page_env, transition, page_ids_by_filename)


def _is_closed_state_transaction(transition: Dict[str, Any]) -> bool:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    status = str(
        transition.get("effect_admission_status")
        or effect.get("effect_admission_status")
        or ""
    )
    if status not in {"accepted", "resolved"}:
        return False
    if not str(transition.get("reducer") or ""):
        return False
    if not (
        effect.get("expected_state_diff")
        or effect.get("state_diff")
        or transition.get("expected_state_diff")
        or transition.get("state_diff")
    ):
        return False
    return bool(effect.get("state_ref") or effect.get("draft_ref") or effect.get("query_state_owner"))


def _closed_transaction_state_ref(transition: Dict[str, Any]) -> str:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    return str(effect.get("state_ref") or effect.get("draft_ref") or effect.get("query_state_owner") or "")


def _closed_transaction_expected_state_diff(transition: Dict[str, Any]) -> Dict[str, Any]:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    for value in (
        effect.get("expected_state_diff"),
        effect.get("state_diff"),
        transition.get("expected_state_diff"),
        transition.get("state_diff"),
    ):
        if isinstance(value, dict) and value:
            return dict(value)
    return {}


def _closed_transaction_oracle_types(transition: Dict[str, Any]) -> List[str]:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    reducer = str(transition.get("reducer") or "")
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    types = ["state_mutation"]
    if target and target != source:
        types.append("route_target")
    if reducer in {"filter_selection", "select_filter_option", "apply_filter", "switch_tab"}:
        types.append("stateful_component_projection")
    if reducer in {"update_cart_quantity", "add_to_cart", "remove_from_cart"}:
        types.extend(["stateful_component_projection", "cart_projection_consistency"])
    if reducer == "project_suggestions" or str(effect.get("query_transition_kind") or "") == "suggestion_projection":
        types.append("query_suggestion_projection")
    if isinstance(effect.get("selected_record_hydration"), dict):
        types.append("selected_record_hydration")
    return _dedupe(types, limit=8)


def _ensure_closed_transaction_semantic_artifacts(page_env: Dict[str, Any], transition: Dict[str, Any]) -> None:
    step_key = str(transition.get("step_key") or transition.get("transition_id") or "")
    state_ref = _closed_transaction_state_ref(transition)
    if not step_key or not state_ref:
        return
    _ensure_runtime_state_refs(
        page_env,
        state_ref,
        source="page_environment_closed_transaction_semantic_closure",
    )
    render_projections = page_env.setdefault("render_projections", [])
    if not isinstance(render_projections, list):
        render_projections = []
        page_env["render_projections"] = render_projections
    projection_id = f"{step_key}::closed_transaction_render_projection"
    if not any(isinstance(item, dict) and item.get("projection_id") == projection_id for item in render_projections):
        render_projections.append({
            "projection_id": projection_id,
            "source": "page_environment_closed_transaction_semantic_closure",
            "source_step_keys": [step_key],
            "l0_transition_ids": [step_key],
            "reads_state": state_ref,
            "reads_state_refs": [state_ref],
            "interaction_required": True,
            "dom_contract": {"required_behavior": "visible_actionable_control"},
            "render_contract": "Closed typed transaction must project its committed runtime state.",
        })
    expected_state_diff = _closed_transaction_expected_state_diff(transition)
    oracles = page_env.setdefault("oracles", [])
    if not isinstance(oracles, list):
        oracles = []
        page_env["oracles"] = oracles
    existing_ids = {
        str(item.get("oracle_id") or "")
        for item in oracles
        if isinstance(item, dict)
    }
    for oracle_type in _closed_transaction_oracle_types(transition):
        oracle_id = f"{step_key}::closed_transaction_{oracle_type}_oracle"
        if oracle_id in existing_ids:
            continue
        oracles.append({
            "oracle_id": oracle_id,
            "source": "page_environment_closed_transaction_semantic_closure",
            "step_key": step_key,
            "oracle_type": oracle_type,
            "state_refs": [state_ref],
            "expected_state_diff": expected_state_diff,
        })


def _normalize_transition_semantics(
    page_env: Dict[str, Any],
    transition: Dict[str, Any],
    render_role_by_step: Dict[str, str] | None = None,
    effective_next_step_key: str = "",
) -> None:
    if not isinstance(transition, dict) or not _is_l0_transition(transition):
        return
    operation = str(transition.get("operation") or "").lower()
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    intent = str(transition.get("operation_intent") or "")
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    _sanitize_future_state_hints(transition)
    step_key = canonical_step_key(transition.get("step_key") or transition.get("transition_id") or "")
    next_step_key = canonical_step_key(effective_next_step_key) or _next_canonical_step_key(step_key)
    render_roles = render_role_by_step or {}
    next_render_role = str(render_roles.get(next_step_key) or transition.get("next_render_role") or "")
    current_surface = transition.get("surface_contract") if isinstance(transition.get("surface_contract"), dict) else {}
    next_surface = transition.get("next_surface_contract") if isinstance(transition.get("next_surface_contract"), dict) else {}
    reference_surface_can_be_upgraded = bool(
        next_surface
        and (
            not current_surface
            or str(next_surface.get("surface_scope") or "")
            != str(current_surface.get("surface_scope") or "")
        )
    )
    generic_inline_can_be_upgraded = (
        str(transition.get("reducer") or "") == "activate_inline_state"
        and str(effect.get("effect") or "") in {"", "activate_inline_state"}
        and (
            next_render_role == "post_interaction_overlay"
            or reference_surface_can_be_upgraded
        )
    )
    if _is_closed_state_transaction(transition) and not generic_inline_can_be_upgraded:
        state_ref = _closed_transaction_state_ref(transition)
        _ensure_runtime_state_refs(
            page_env,
            state_ref,
            source="page_environment_closed_transaction_preservation",
        )
        _ensure_closed_transaction_semantic_artifacts(page_env, transition)
        return

    if operation in {"click", "tap"} and _is_query_entry_click(source, target, intent):
        state_ref = _state_ref(f"{target}::query_entry")
        transition["reducer"] = "open_search_entry"
        transition["state_effect"] = {
            "effect": "open_search_entry",
            "state_ref": state_ref,
            "target_filename": target,
            "query_transition_kind": "query_entry",
            "visible_initial_prefill_allowed": False,
            "mutation_policy": "route_to_search_entry_without_committing_query",
            "source": "page_environment_typed_transition_normalization",
        }
        transition["query_transition_kind"] = "query_entry"
        _ensure_runtime_state_refs(page_env, state_ref, source="page_environment_typed_transition_normalization")
        return

    if operation in {"click", "tap"} and source and target and source != target:
        if _has_runtime_overlay_or_option_state(page_env):
            selected_ref = _state_ref(f"{source}::selected_runtime_overlay")
            effect = dict(effect) if isinstance(effect, dict) else {}
            effect.setdefault("effect", "navigate_route")
            effect.setdefault("target_filename", target)
            effect.setdefault("state_ref", selected_ref)
            effect.setdefault("selected_record_hydration", {
                "required": True,
                "source_filename": source,
                "target_filename": target,
                "policy": "hydrate_target_from_committed_overlay_selection",
            })
            effect.setdefault("mutation_policy", "commit_overlay_selection_then_route")
            effect.setdefault("source", "page_environment_typed_transition_normalization")
            transition["state_effect"] = effect
            transition["reducer"] = "navigate_route"
            _ensure_runtime_state_refs(page_env, selected_ref, source="page_environment_typed_transition_normalization")
            return
        if "detail" in target or "edit" in target or _contains_any(intent, ("详情", "编辑", "查看", "进入", "选择")):
            selected_ref = _state_ref(f"{source}::selected_record")
            effect = dict(effect) if isinstance(effect, dict) else {}
            effect.setdefault("effect", "navigate_route")
            effect.setdefault("target_filename", target)
            effect.setdefault("state_ref", selected_ref)
            effect.setdefault("selected_record_hydration", {
                "required": True,
                "source_filename": source,
                "target_filename": target,
                "policy": "hydrate_target_from_clicked_record",
            })
            effect.setdefault("source", "page_environment_typed_transition_normalization")
            transition["state_effect"] = effect
            transition["reducer"] = "navigate_route"
            _ensure_runtime_state_refs(page_env, selected_ref, source="page_environment_typed_transition_normalization")
            return
        if not effect.get("effect"):
            transition["reducer"] = "navigate_route"
            transition["state_effect"] = {
                "effect": "navigate_route",
                "target_filename": target,
                "mutation_policy": "route_transition_only",
                "source": "page_environment_typed_transition_normalization",
            }
            return

    if operation in {"click", "tap"}:
        reducer, runtime_effect = _runtime_effect_for_same_page_click(
            source=source,
            target=target,
            intent=intent,
            source_step_key=step_key,
            next_step_key=next_step_key,
            render_role=str(render_roles.get(step_key) or transition.get("render_role") or ""),
            next_render_role=next_render_role,
            runtime_observations=transition.get("runtime_observations") if isinstance(transition.get("runtime_observations"), list) else [],
            schema_entities=transition.get("schema_entities") if isinstance(transition.get("schema_entities"), list) else [],
            route_contract=transition.get("route_contract") if isinstance(transition.get("route_contract"), dict) else {},
            surface_contract=transition.get("surface_contract") if isinstance(transition.get("surface_contract"), dict) else {},
            next_surface_contract=transition.get("next_surface_contract") if isinstance(transition.get("next_surface_contract"), dict) else {},
        )
        if reducer and runtime_effect:
            transition["reducer"] = reducer
            runtime_effect.setdefault("source", "page_environment_typed_transition_normalization")
            transition["state_effect"] = runtime_effect
            for key in (
                "effect_admission_status",
                "effect_family",
                "domain_intent",
                "effect_evidence",
            ):
                if runtime_effect.get(key) not in (None, "", [], {}):
                    transition[key] = runtime_effect.get(key)
            _ensure_runtime_state_refs(
                page_env,
                runtime_effect.get("state_ref"),
                source="page_environment_typed_transition_normalization",
            )
            return
        if runtime_effect:
            for source_key, target_key in (
                ("effect_admission_status", "effect_admission_status"),
                ("weak_effect_candidates", "weak_effect_candidates"),
                ("rejected_effect_candidates", "rejected_effect_candidates"),
                ("unresolved_reason", "unresolved_reason"),
            ):
                if runtime_effect.get(source_key) not in (None, "", [], {}):
                    transition[target_key] = runtime_effect.get(source_key)

    if operation == "fill" and isinstance(effect, dict):
        qkind = str(transition.get("query_transition_kind") or effect.get("query_transition_kind") or "")
        field_role = str(effect.get("field_role") or "")
        if qkind == "suggestion_projection" or field_role in {"address_lookup", "location_lookup"}:
            if field_role in {"address_lookup", "location_lookup"}:
                qkind = "suggestion_projection"
            state_ref = str(effect.get("query_state_owner") or _state_ref(f"{source}::{field_role or 'query'}"))
            effect["effect"] = "project_suggestions"
            effect["state_ref"] = state_ref
            effect["query_transition_kind"] = qkind or "suggestion_projection"
            effect["mutation_policy"] = "update_draft_and_project_suggestions"
            effect.setdefault("source", "page_environment_typed_transition_normalization")
            transition["state_effect"] = effect
            transition["reducer"] = "project_suggestions"
            transition["query_transition_kind"] = effect["query_transition_kind"]
            _ensure_runtime_state_refs(page_env, state_ref, source="page_environment_typed_transition_normalization")


def _normalize_inline_activation_transition(page_env: Dict[str, Any], transition: Dict[str, Any]) -> None:
    if not isinstance(transition, dict) or not _is_l0_transition(transition):
        return
    operation = str(transition.get("operation") or "")
    if operation not in {"click", "tap"}:
        return
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    if not source or source != target:
        return
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    if effect.get("effect"):
        return
    state_ref = _state_ref(f"{source}::inline_activation")
    transition["reducer"] = "activate_inline_state"
    transition["state_effect"] = {
        "effect": "activate_inline_state",
        "state_ref": state_ref,
        "mutation_policy": "select_active_or_open_inline_state",
        "source": "page_environment_transition_effect_normalization",
    }
    _ensure_runtime_state_refs(
        page_env,
        state_ref,
        source="page_environment_transition_effect_normalization",
    )


def _inline_activation_source_sequence_item(
    transition: Dict[str, Any],
    *,
    position: int,
) -> Tuple[Tuple[str, str, str], str, str, int | None, int] | None:
    if not isinstance(transition, dict) or not _is_l0_transition(transition):
        return None
    if str(transition.get("direction") or "source") != "source":
        return None
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    if not source or source != target:
        return None
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    if str(transition.get("reducer") or "") != "activate_inline_state":
        return None
    if str(effect.get("effect") or "") != "activate_inline_state":
        return None
    state_ref = str(effect.get("state_ref") or "")
    if not state_ref:
        return None
    step_key = canonical_step_key(
        transition.get("step_key") or transition.get("transition_id") or ""
    )
    if not step_key:
        return None
    trace_id, step_index = _trace_step_order_from_step_key(step_key)
    return (trace_id, source, target), state_ref, step_key, step_index, position


def _inline_activation_followup_source_steps(page_env: Dict[str, Any]) -> set[str]:
    transitions = page_env.get("transitions") if isinstance(page_env, dict) else []
    if not isinstance(transitions, list):
        return set()
    items: List[Tuple[Tuple[str, str, str], str, str, int | None, int]] = []
    for position, transition in enumerate(transitions):
        item = _inline_activation_source_sequence_item(transition, position=position)
        if item:
            items.append(item)
    items.sort(
        key=lambda item: (
            item[0],
            0 if item[3] is not None else 1,
            item[3] if item[3] is not None else item[4],
            item[4],
        )
    )
    followup_steps: set[str] = set()
    previous_by_scope: Dict[Tuple[str, str, str], Tuple[str, int | None, int]] = {}
    for scope, state_ref, step_key, step_index, position in items:
        previous = previous_by_scope.get(scope)
        if previous:
            previous_state_ref, _previous_step_index, _previous_position = previous


            if previous_state_ref == state_ref:
                followup_steps.add(step_key)
        previous_by_scope[scope] = (state_ref, step_index, position)
    gesture_items: List[Tuple[Tuple[str, str], str, int | None, int, bool]] = []
    for position, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            continue
        if str(transition.get("direction") or "source") != "source":
            continue
        step_key = canonical_step_key(transition.get("step_key") or transition.get("transition_id") or "")
        if not step_key:
            continue
        trace_id, step_index = _trace_step_order_from_step_key(step_key)
        source = _base_filename(transition.get("source_filename"))
        target = _base_filename(transition.get("target_filename"))
        if not source:
            continue
        operation = str(transition.get("operation") or "").lower()
        reducer = str(transition.get("reducer") or "").lower()
        intent = str(transition.get("operation_intent") or transition.get("command") or "")
        intent_l = intent.lower()
        is_stateful_gesture = (
            operation in {"swipe", "drag", "scroll", "pan"}
            and (not target or target == source)
            and any(token in intent for token in ("滑", "拖", "横", "切换"))
            and any(token in intent for token in ("分类", "快捷", "轮播", "区域", "列表", "卡片"))
        ) or (
            reducer in {"component_visible_window", "stateful_reveal"}
        ) or (
            any(token in intent_l for token in ("carousel", "shortcut", "category"))
            and operation in {"swipe", "drag", "scroll", "pan"}
            and (not target or target == source)
        )
        gesture_items.append(((trace_id, source), step_key, step_index, position, is_stateful_gesture))
    gesture_items.sort(
        key=lambda item: (
            item[0],
            0 if item[2] is not None else 1,
            item[2] if item[2] is not None else item[3],
            item[3],
        )
    )
    for current, nxt in zip(gesture_items, gesture_items[1:]):
        scope, _step_key, step_index, position, is_stateful_gesture = current
        next_scope, next_step_key, next_step_index, next_position, _next_is_gesture = nxt
        if not is_stateful_gesture or scope != next_scope:
            continue


        followup_steps.add(next_step_key)
    return followup_steps


def _scroll_revealed_followup_source_steps(page_env: Dict[str, Any]) -> set[str]:
    transitions = page_env.get("transitions") if isinstance(page_env, dict) else []
    if not isinstance(transitions, list):
        return set()
    grouped: Dict[Tuple[str, str], List[Tuple[int | None, int, Dict[str, Any]]]] = defaultdict(list)
    for position, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            continue
        if str(transition.get("direction") or "source") != "source":
            continue
        step_key = canonical_step_key(
            transition.get("step_key") or transition.get("transition_id") or ""
        )
        source = _base_filename(transition.get("source_filename"))
        if not step_key or not source:
            continue
        trace_id, step_index = _trace_step_order_from_step_key(step_key)
        grouped[(trace_id, source)].append((step_index, position, transition))

    revealed: set[str] = set()
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                0 if item[0] is not None else 1,
                item[0] if item[0] is not None else item[1],
                item[1],
            )
        )
        for current, nxt in zip(items, items[1:]):
            transition = current[2]
            source = _base_filename(transition.get("source_filename"))
            target = _base_filename(transition.get("target_filename"))
            operation = str(transition.get("operation") or "").lower()
            reducer = str(transition.get("reducer") or "").lower()
            intent = str(transition.get("operation_intent") or transition.get("command") or "")
            intent_l = intent.lower()
            is_scroll_reveal = (
                operation in {"swipe", "drag", "scroll", "pan"}
                and (not target or target == source)
                and (
                    reducer in {"scroll_viewport", "component_visible_window", "stateful_reveal"}
                    or (
                        any(token in intent for token in ("滑", "拖", "横"))
                        and any(token in intent for token in ("分类", "快捷", "轮播", "区域", "列表", "卡片"))
                    )
                    or any(token in intent_l for token in ("carousel", "shortcut", "category"))
                )
            )
            if not is_scroll_reveal:
                continue
            next_key = canonical_step_key(
                nxt[2].get("step_key") or nxt[2].get("transition_id") or ""
            )
            if next_key:
                revealed.add(next_key)
    return revealed


def _effective_next_source_step_by_step(page_env: Dict[str, Any]) -> Dict[str, str]:

    transitions = page_env.get("transitions") if isinstance(page_env, dict) else []
    if not isinstance(transitions, list):
        return {}
    grouped: Dict[Tuple[str, str], List[Tuple[int | None, int, str]]] = defaultdict(list)
    for position, transition in enumerate(transitions):
        if not isinstance(transition, dict) or not _is_l0_transition(transition):
            continue
        if str(transition.get("direction") or "source") != "source":
            continue
        step_key = canonical_step_key(
            transition.get("step_key") or transition.get("transition_id") or ""
        )
        source = _base_filename(transition.get("source_filename"))
        if not step_key or not source:
            continue
        trace_id, step_index = _trace_step_order_from_step_key(step_key)
        grouped[(trace_id, source)].append((step_index, position, step_key))
    effective_next: Dict[str, str] = {}
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                0 if item[0] is not None else 1,
                item[0] if item[0] is not None else item[1],
                item[1],
            )
        )
        for current, nxt in zip(items, items[1:]):
            effective_next[current[2]] = nxt[2]
    return effective_next


def _attach_typed_transition_projection(page_env: Dict[str, Any], transition: Dict[str, Any], component_id: str) -> None:
    reducer = str(transition.get("reducer") or "")
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    if not reducer:
        return
    step_key = str(transition.get("step_key") or transition.get("transition_id") or "")
    state_ref = str(effect.get("state_ref") or effect.get("query_state_owner") or effect.get("draft_ref") or _transition_reads_state(transition))
    source = "page_environment_typed_projection_compiler"

    if reducer in {"project_suggestions", "type_search"} and str(effect.get("query_transition_kind") or transition.get("query_transition_kind") or "") == "suggestion_projection":
        hints = _target_identity_hints(transition)
        suggested_titles = [
            hint.split("suggestions_projection:first_result_title:", 1)[1]
            for hint in hints
            if "suggestions_projection:first_result_title:" in hint
        ]
        suggested_subtitles = [
            hint.split("suggestions_projection:first_result_subtitle:", 1)[1]
            for hint in hints
            if "suggestions_projection:first_result_subtitle:" in hint
        ]
        projection_id = f"projection::{_safe_id(page_env.get('filename'))}::{_safe_id(step_key)}::suggestions"
        _add_unique_dict(
            page_env.setdefault("render_projections", []),
            {
                "projection_id": projection_id,
                "component_ref": component_id,
                "reads_state": state_ref,
                "render_phase": "runtime",
                "layout_policy": "query_suggestion_projection",
                "lifecycle": "runtime_only",
                "interaction_required": True,
                "dom_contract": {
                    "required_behavior": "visible_actionable_control",
                    "forbid_noninteractive_host": True,
                    "click_surface": "real_visible_component",
                },
                "suggestion_contract": {
                    "query_value": str(effect.get("value") or transition.get("input_text") or ""),
                    "expected_titles": _dedupe(suggested_titles, limit=4),
                    "expected_subtitles": _dedupe(suggested_subtitles, limit=4),
                    "projection_policy": "render_matching_suggestions_after_input_event",
                },
                "source_step_keys": [step_key],
                "l0_transition_ids": [str(transition.get("transition_id") or step_key)],
                "source": source,
            },
            ("projection_id",),
        )
        _add_unique_dict(
            page_env.setdefault("oracles", []),
            {
                "oracle_type": "query_suggestion_projection",
                "step_key": step_key,
                "component_ref": component_id,
                "state_ref": state_ref,
                "expected": "Typing into the query/address field projects matching suggestion rows without baking the value into initial DOM.",
                "source": source,
            },
            ("oracle_type", "step_key", "component_ref"),
        )
        return

    if reducer in {"open_overlay", "open_option_modal"}:
        overlay_kind = str(effect.get("overlay_kind") or ("product_option_modal" if reducer == "open_option_modal" else "overlay"))
        overlay_contract = {
            "overlay_kind": overlay_kind,
            "initial_visible": False,
            "must_fit_within_app_shell": True,
            "bottom_actions_fully_visible": True,
        }
        if str(effect.get("template_id") or "") == "wheel_picker" or overlay_kind == "year_month_picker":
            overlay_contract.update(_year_month_wheel_picker_overlay_contract(transition, effect))
        elif str(effect.get("template_id") or "") == "wheel_time_picker" or overlay_kind == "departure_time_picker":
            overlay_contract.update(_wheel_time_picker_overlay_contract(transition, effect))
        projection_id = f"projection::{_safe_id(page_env.get('filename'))}::{_safe_id(step_key)}::{_safe_id(overlay_kind)}"
        _add_unique_dict(
            page_env.setdefault("render_projections", []),
            {
                "projection_id": projection_id,
                "component_ref": component_id,
                "reads_state": state_ref,
                "render_phase": "runtime",
                "layout_policy": "bounded_overlay_in_app_shell",
                "lifecycle": "runtime_only",
                "overlay_contract": overlay_contract,
                "source_step_keys": [step_key],
                "l0_transition_ids": [str(transition.get("transition_id") or step_key)],
                "source": source,
            },
            ("projection_id",),
        )
        _add_unique_dict(
            page_env.setdefault("oracles", []),
            {
                "oracle_type": "modal_layout_within_app_shell",
                "step_key": step_key,
                "component_ref": component_id,
                "expected": "Runtime overlay opens inside the phone/app shell, is initially hidden, and keeps bottom actions visible.",
                "source": source,
            },
            ("oracle_type", "step_key", "component_ref"),
        )
        return

    if reducer in {"select_filter_option", "apply_filter", "switch_tab"}:
        projection_id = f"projection::{_safe_id(page_env.get('filename'))}::{_safe_id(step_key)}::selection_projection"
        if state_ref:
            page_env.setdefault("initial_state", {}).setdefault("selections", {}).setdefault(
                state_ref,
                {
                    "selected_label": "",
                    "source": "default_empty_selection_before_runtime_transition",
                    "forbid_initial_selection_from_transition": True,
                },
            )
        _add_unique_dict(
            page_env.setdefault("render_projections", []),
            {
                "projection_id": projection_id,
                "component_ref": component_id,
                "reads_state": state_ref,
                "render_phase": "runtime" if reducer != "apply_filter" else "post_transition",
                "layout_policy": "stateful_selection_projection",
                "selection_contract": {
                    "selection_kind": str(effect.get("selection_kind") or "selection"),
                    "selected_label": str(effect.get("selected_label") or ""),
                    "projection_policy": str(effect.get("mutation_policy") or "project_selected_state"),
                    "initial_selected": False if reducer != "apply_filter" else None,
                },
                "source_step_keys": [step_key],
                "l0_transition_ids": [str(transition.get("transition_id") or step_key)],
                "source": source,
            },
            ("projection_id",),
        )
        _add_unique_dict(
            page_env.setdefault("oracles", []),
            {
                "oracle_type": "stateful_component_projection",
                "step_key": step_key,
                "component_ref": component_id,
                "state_ref": state_ref,
                "expected": "Selection/tab/filter click mutates component state and reprojects the dependent content, not just button styling.",
                "source": source,
            },
            ("oracle_type", "step_key", "component_ref"),
        )
        return

    if reducer in {"update_cart_quantity", "add_to_cart", "remove_from_cart"}:
        projection_id = f"projection::{_safe_id(page_env.get('filename'))}::{_safe_id(step_key)}::cart_projection"
        _add_unique_dict(
            page_env.setdefault("render_projections", []),
            {
                "projection_id": projection_id,
                "component_ref": component_id,
                "reads_state": state_ref,
                "render_phase": "post_transition",
                "layout_policy": "cart_accumulator_projection",
                "cart_contract": {
                    "selected_label": str(effect.get("selected_label") or ""),
                    "delta": effect.get("delta"),
                    "initial_quantity_source": "page_environment_spec.initial_state_only",
                    "post_transition_policy": str(effect.get("mutation_policy") or "derive_visible_quantity_and_badge_from_cart_state"),
                    "forbid_initial_quantity_from_transition": True,
                },
                "source_step_keys": [step_key],
                "l0_transition_ids": [str(transition.get("transition_id") or step_key)],
                "source": source,
            },
            ("projection_id",),
        )
        _add_unique_dict(
            page_env.setdefault("oracles", []),
            {
                "oracle_type": "cart_projection_consistency",
                "step_key": step_key,
                "component_ref": component_id,
                "state_ref": state_ref,
                "expected": "Cart quantity and badge are derived from cart state after the click; transition quantities must not be rendered as initial DOM.",
                "source": source,
            },
            ("oracle_type", "step_key", "component_ref"),
        )
        _add_unique_dict(
            page_env.setdefault("oracles", []),
            {
                "oracle_type": "stateful_component_projection",
                "step_key": step_key,
                "component_ref": component_id,
                "state_ref": state_ref,
                "expected": "Cart click mutates the accumulator and reprojects dependent quantity/badge/cart summary components.",
                "source": source,
            },
            ("oracle_type", "step_key", "component_ref"),
        )
        return

    hydration = effect.get("selected_record_hydration") if isinstance(effect.get("selected_record_hydration"), dict) else {}
    if hydration:
        _add_unique_dict(
            page_env.setdefault("oracles", []),
            {
                "oracle_type": "selected_record_hydration",
                "step_key": step_key,
                "component_ref": component_id,
                "state_ref": state_ref,
                "expected": "Target detail/edit page hydrates from the clicked selected record instead of rendering a blank or unrelated record.",
                "source": source,
            },
            ("oracle_type", "step_key", "component_ref"),
        )


def _normalize_query_initial_state(page_env: Dict[str, Any]) -> None:
    committed_queries = _page_environment_expected_committed_queries(
        str(page_env.get("filename") or page_env.get("page_id") or ""),
        page_env,
    )
    if not committed_queries:
        return
    query_state = page_env.setdefault("initial_state", {}).setdefault("query", {})
    if not isinstance(query_state, dict):
        page_env.setdefault("initial_state", {})["query"] = {}
        query_state = page_env["initial_state"]["query"]
    query_state["committed_values"] = committed_queries
    query_state["default"] = committed_queries[0]
    _add_unique_dict(
        page_env.setdefault("state_schema", {}).setdefault("parameters", []),
        {
            "parameter_id": "business_query",
            "schema": {"type": "string"},
            "render_role": "committed_query_state",
            "source": "page_environment_query_state_normalization",
        },
        ("parameter_id",),
    )


def _promote_matching_source(items: Any, predicate: Any, source: str) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict) and predicate(item):
            if _is_contract_closure_item(item):
                item["source"] = source


def _reference_state_surface_contracts_by_step(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    surface_markers = (
        "action_sheet",
        "bottom_sheet",
        "overlay",
        "drawer",
        "modal",
        "panel",
        "sheet",
        "浮层",
        "弹窗",
        "面板",
        "选择框",
    )
    result: Dict[str, Dict[str, Any]] = {}
    for spec in payload.get("specs", []) if isinstance(payload.get("specs"), list) else []:
        if not isinstance(spec, dict):
            continue
        active_surface_by_filename: Dict[str, Dict[str, Any]] = {}
        for snapshot in spec.get("state_snapshots", []) if isinstance(spec.get("state_snapshots"), list) else []:
            if not isinstance(snapshot, dict):
                continue
            step_key = canonical_step_key(snapshot.get("step_key") or "")
            filename = _base_filename(snapshot.get("filename"))
            if not step_key or not filename:
                continue
            state_items = snapshot.get("state_items", []) if isinstance(snapshot.get("state_items"), list) else []
            surface_items: List[Dict[str, Any]] = []
            selection_items: List[Dict[str, Any]] = []
            for state_item in state_items:
                if not isinstance(state_item, dict):
                    continue
                surface_scope = str(state_item.get("state_scope") or "").strip()
                structural_text = " ".join(
                    str(state_item.get(key) or "")
                    for key in ("state_scope", "lifecycle", "mutation")
                ).lower()
                if surface_scope and _contains_any(structural_text, surface_markers):
                    surface_items.append(state_item)
                if _surface_contract_has_progressive_selection_lifecycle(state_item):
                    selection_items.append(state_item)

            contract: Dict[str, Any] = {}
            for state_item in surface_items:
                surface_scope = str(state_item.get("state_scope") or "").strip()
                structural_text = " ".join(
                    str(state_item.get(key) or "")
                    for key in ("state_scope", "lifecycle", "mutation")
                ).lower()
                if "action_sheet" in structural_text or "bottom_sheet" in structural_text or "sheet" in structural_text:
                    surface_kind = "action_sheet"
                elif "drawer" in structural_text:
                    surface_kind = "drawer"
                elif "modal" in structural_text:
                    surface_kind = "modal"
                else:
                    surface_kind = "runtime_overlay"
                options = [
                    str(item).strip()
                    for item in state_item.get("value_hints", [])
                    if str(item or "").strip()
                ] if isinstance(state_item.get("value_hints"), list) else []
                existing = contract
                merged_options = _dedupe(
                    list(existing.get("options", []) if isinstance(existing.get("options"), list) else [])
                    + options,
                    limit=64,
                )
                contract = {
                    "surface_kind": str(existing.get("surface_kind") or surface_kind),
                    "surface_scope": str(existing.get("surface_scope") or surface_scope),
                    "lifecycle": str(existing.get("lifecycle") or state_item.get("lifecycle") or ""),
                    "mutation": str(existing.get("mutation") or state_item.get("mutation") or ""),
                    "state_ref": str(
                        existing.get("state_ref")
                        or _selection_state_ref(filename, surface_scope)
                    ),
                    "options": merged_options,
                    "terminal_constraints": _dedupe(
                        list(
                            existing.get("terminal_constraints", [])
                            if isinstance(existing.get("terminal_constraints"), list)
                            else []
                        )
                        + list(
                            state_item.get("terminal_constraints", [])
                            if isinstance(state_item.get("terminal_constraints"), list)
                            else []
                        ),
                        limit=64,
                    ),
                    "evidence_step": step_key,
                    "evidence_source": "reference_trace_state_snapshot",
                }
            inherited_surface = active_surface_by_filename.get(filename, {})
            if selection_items and (contract or inherited_surface):
                base = dict(
                    contract
                    if contract and (
                        not inherited_surface
                        or _surface_contract_has_explicit_open_lifecycle(contract)
                    )
                    else inherited_surface
                )
                selection_options = _dedupe(
                    [
                        str(value).strip()
                        for item in selection_items
                        for value in (
                            item.get("value_hints", [])
                            if isinstance(item.get("value_hints"), list)
                            else []
                        )
                        if str(value or "").strip()
                    ],
                    limit=64,
                )
                selection_item = next(
                    (
                        item
                        for item in reversed(selection_items)
                        if any(
                            token in str(item.get("mutation") or "").lower()
                            for token in ("select", "selected", "selection", "choose", "chosen")
                        )
                    ),
                    selection_items[-1],
                )
                base["lifecycle"] = str(selection_item.get("lifecycle") or base.get("lifecycle") or "")
                base["mutation"] = str(selection_item.get("mutation") or base.get("mutation") or "")
                base["options"] = _dedupe(
                    list(base.get("options", []) if isinstance(base.get("options"), list) else [])
                    + selection_options,
                    limit=64,
                )
                base["selection_state_scopes"] = _dedupe(
                    [str(item.get("state_scope") or "") for item in selection_items],
                    limit=16,
                )
                base["evidence_step"] = step_key
                base["evidence_source"] = "reference_trace_state_snapshot_with_inherited_surface"
                contract = base
            if contract:
                result[step_key] = contract
                if _surface_contract_has_explicit_close_lifecycle(contract):
                    active_surface_by_filename.pop(filename, None)
                else:
                    active_surface_by_filename[filename] = contract
    return result


def close_page_environment_contracts(
    payload: Dict[str, Any],
    render_role_by_step: Dict[str, str] | None = None,
) -> None:
    if render_role_by_step is None and isinstance(payload, dict):
        render_role_by_step = payload.get("screenshot_render_roles", {})
    render_role_by_step = normalize_step_keyed_mapping(render_role_by_step)
    """Close structural L0 contract gaps after semantic resolution/enrichment.

    The LLM resolves semantic ambiguity. This pass enforces the invariant that
    every L0 transition has an executable component/render/oracle spine and
    every draft commit has a record identity contract.
    """
    for spec in payload.get("specs", []) if isinstance(payload.get("specs"), list) else []:
        if not isinstance(spec, dict):
            continue
        for transition in spec.get("transitions", []) if isinstance(spec.get("transitions"), list) else []:
            if isinstance(transition, dict):
                _normalize_draft_only_query_transition(transition)
    page_envs = payload.get("page_environment_specs_by_filename", {}) if isinstance(payload, dict) else {}
    if not isinstance(page_envs, dict):
        return
    page_ids_by_filename = {
        _base_filename(filename): str(page_env.get("site_model_page_id") or page_env.get("page_id") or "")
        for filename, page_env in page_envs.items()
        if isinstance(page_env, dict)
    }
    reference_surfaces_by_step = _reference_state_surface_contracts_by_step(payload)
    closed_steps: set[str] = set()
    for filename, page_env in page_envs.items():
        if not isinstance(page_env, dict):
            continue
        _ensure_environment_primitives(page_env)
        _ensure_projection_component_records(page_env)
        _normalize_query_initial_state(page_env)
        inline_activation_followup_steps = _inline_activation_followup_source_steps(page_env)
        scroll_revealed_followup_steps = _scroll_revealed_followup_source_steps(page_env)
        effective_next_source_steps = _effective_next_source_step_by_step(page_env)
        for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
            if not _is_l0_transition(transition):
                continue
            raw_step_key = canonical_step_key(
                transition.get("step_key") or transition.get("transition_id") or ""
            )
            effective_next_step_key = (
                effective_next_source_steps.get(raw_step_key, "")
                or canonical_step_key(transition.get("post_observation_key") or "")
                or _next_canonical_step_key(raw_step_key)
            )
            current_surface_contract = reference_surfaces_by_step.get(raw_step_key, {})
            next_surface_contract = reference_surfaces_by_step.get(effective_next_step_key, {})
            if current_surface_contract:
                transition["surface_contract"] = dict(current_surface_contract)
            if next_surface_contract:
                transition["next_surface_contract"] = dict(next_surface_contract)
            else:
                transition.pop("next_surface_contract", None)
            _normalize_transition_semantics(
                page_env,
                transition,
                render_role_by_step=render_role_by_step,
                effective_next_step_key=effective_next_step_key,
            )
            _normalize_inline_activation_transition(page_env, transition)
            _normalize_suggestion_projection_preview_transition(transition)
            step_key = canonical_step_key(
                transition.get("step_key") or transition.get("transition_id") or ""
            )
            if not step_key:
                continue
            transition["step_key"] = step_key
            _normalize_draft_only_query_transition(transition)
            _normalize_suggestion_projection_preview_transition(transition)
            _attach_structured_transition_identity(page_env, transition, page_ids_by_filename)
            direction = str(transition.get("direction") or "source")
            component_id = f"cmp::{_safe_id(filename)}::l0::{_safe_id(step_key)}::{direction}"
            reads_state = _transition_reads_state(transition)
            step_render_role = (render_role_by_step or {}).get(step_key, "")
            scroll_reachable_initial = (
                direction == "source" and step_key in scroll_revealed_followup_steps
            )
            source_host_requires_runtime_visibility = (
                not scroll_reachable_initial
                and (
                    _source_host_requires_runtime_visibility(
                        transition,
                        step_render_role=step_render_role,
                    )
                    or step_key in inline_activation_followup_steps
                )
            )
            if scroll_reachable_initial:
                render_phase = "initial"
                host_availability_phase = "initial_dom_scroll_reachable"
                initial_dom_eligible = True
                initial_visibility_policy = "scroll_reachable_initial"
                runtime_visibility_policy = "visible_after_scroll"
            elif direction == "source" and source_host_requires_runtime_visibility:
                render_phase = "runtime"
                host_availability_phase = "runtime"
                initial_dom_eligible = False
                initial_visibility_policy = "hidden_until_transition"
                runtime_visibility_policy = "visible_when_state_active"
            elif direction == "source":
                render_phase = "initial"
                host_availability_phase = "initial"
                initial_dom_eligible = True
                initial_visibility_policy = "visible_initial"
                runtime_visibility_policy = "visible_initial"
            else:
                render_phase = "post_transition"
                host_availability_phase = "runtime"
                initial_dom_eligible = False
                initial_visibility_policy = "hidden_until_transition"
                runtime_visibility_policy = "visible_when_state_active"
            initial_value_binding = _initial_value_binding_for_transition(transition)
            value_lifecycle = _transition_value_lifecycle(transition)
            spine_source = (
                "page_environment_transition_contract_compiler"
                if _semantic_l0_reducer_valid(transition)
                else "page_environment_contract_closure"
            )
            source = _base_filename(transition.get("source_filename"))
            target = _base_filename(transition.get("target_filename"))
            is_source_interaction = direction == "source"
            dom_contract: Dict[str, Any] = {
                "required_behavior": (
                    "initially_hidden_actionable_control"
                    if render_phase == "runtime"
                    else "visible_actionable_control"
                ),
                "forbid_noninteractive_host": True,
                "click_surface": "real_visible_component",
            }
            if scroll_reachable_initial:
                dom_contract["visibility_precondition"] = "scroll_reachable"
            interaction_contract = {
                "operation": str(transition.get("operation") or ""),
                "command": str(transition.get("command") or ""),
                "operation_intent": str(transition.get("operation_intent") or ""),
                "action_intent": str(transition.get("action_intent") or ""),
                "transition_identity_key": transition.get("transition_identity_key", {}) if isinstance(transition.get("transition_identity_key"), dict) else {},
                "expected_filename": target,
                "input_text": str(transition.get("input_text") or ""),
                "dom_contract": dom_contract,
            }
            if initial_value_binding:
                interaction_contract["initial_value_binding"] = initial_value_binding

            intent_text = str(transition.get("operation_intent") or "")
            label_contract = _action_label_contract_from_intent(intent_text)
            display_label = str(label_contract.get("host_display_label") or "")
            context_labels = label_contract.get("context_text_must_include")
            accessible_labels = label_contract.get("accessible_label_must_include")
            label_authority = str(label_contract.get("label_authority") or "").strip()
            visible_text_contract = (
                label_contract.get("visible_text_contract")
                if isinstance(label_contract.get("visible_text_contract"), dict)
                else {}
            )
            if display_label:
                interaction_contract["required_display_label"] = display_label
            if label_authority:
                interaction_contract["label_authority"] = label_authority
            if visible_text_contract:
                interaction_contract["visible_text_contract"] = dict(visible_text_contract)
            if isinstance(context_labels, list) and context_labels:
                interaction_contract["context_text_must_include"] = list(context_labels)
                interaction_contract["context_match_scope"] = str(
                    label_contract.get("context_match_scope") or "ancestor_or_nearby_region"
                )
            if isinstance(accessible_labels, list) and accessible_labels:
                interaction_contract["accessible_label_must_include"] = list(accessible_labels)
            interaction_contract = {
                key: value
                for key, value in interaction_contract.items()
                if value not in ("", [], {})
            }
            component_dict: Dict[str, Any] = {
                    "component_id": component_id,
                    "component_kind": _transition_component_kind(transition),
                    "component_role": "l0_execution_anchor" if direction == "source" else "l0_result_projection_anchor",
                    "reads_state": reads_state,
                    "render_phase": render_phase,
                    "host_availability_phase": value_lifecycle.get("host_availability_phase") or host_availability_phase,
                    "initial_dom_eligible": initial_dom_eligible,
                    "initial_visibility_policy": initial_visibility_policy,
                    "runtime_visibility_policy": runtime_visibility_policy,
                    "state_value_phase": value_lifecycle.get("state_value_phase"),
                    "content_projection_phase": value_lifecycle.get("content_projection_phase"),
                    "initial_value_source": value_lifecycle.get("initial_value_source"),
                    "post_action_value": value_lifecycle.get("post_action_value"),
                    "forbid_initial_value_from_input_text": value_lifecycle.get("forbid_initial_value_from_input_text"),
                    "layout_policy": "transition_bound_component",
                    "interaction_required": is_source_interaction,
                    "interaction_contract": interaction_contract if is_source_interaction else {},
                    "dom_contract": interaction_contract.get("dom_contract", {}) if is_source_interaction else {},
                    "source_step_keys": [step_key],
                    "l0_transition_ids": [str(transition.get("transition_id") or step_key)],
                    "source": spine_source,
            }
            if display_label:
                component_dict["required_display_label"] = display_label
            if label_authority:
                component_dict["label_authority"] = label_authority
            if visible_text_contract:
                component_dict["visible_text_contract"] = dict(visible_text_contract)
            if isinstance(context_labels, list) and context_labels:
                component_dict["context_text_must_include"] = list(context_labels)
                component_dict["context_match_scope"] = str(
                    label_contract.get("context_match_scope") or "ancestor_or_nearby_region"
                )
            if isinstance(accessible_labels, list) and accessible_labels:
                component_dict["accessible_label_must_include"] = list(accessible_labels)
            if intent_text:
                component_dict["operation_intent"] = intent_text
            _add_unique_dict(
                page_env.setdefault("components", []),
                component_dict,
                ("component_id",),
            )
            _promote_matching_source(
                page_env.get("components", []),
                lambda item, cid=component_id: item.get("component_id") == cid,
                spine_source,
            )
            _add_unique_dict(
                page_env.setdefault("render_projections", []),
                {
                    "projection_id": f"projection::{_safe_id(filename)}::l0::{_safe_id(step_key)}::{direction}",
                    "component_ref": component_id,
                    "reads_state": reads_state,
                    "render_phase": render_phase,
                    "host_availability_phase": value_lifecycle.get("host_availability_phase"),
                    "state_value_phase": value_lifecycle.get("state_value_phase"),
                    "content_projection_phase": value_lifecycle.get("content_projection_phase"),
                    "initial_value_source": value_lifecycle.get("initial_value_source"),
                    "post_action_value": value_lifecycle.get("post_action_value"),
                    "forbid_initial_value_from_input_text": value_lifecycle.get("forbid_initial_value_from_input_text"),
                    "layout_policy": "transition_bound_projection",
                    "interaction_required": is_source_interaction,
                    "expected_interaction": interaction_contract if is_source_interaction else {},
                    "dom_contract": interaction_contract.get("dom_contract", {}) if is_source_interaction else {},
                    "source_step_keys": [step_key],
                    "l0_transition_ids": [str(transition.get("transition_id") or step_key)],
                    "source": spine_source,
                },
                ("projection_id",),
            )
            _promote_matching_source(
                page_env.get("render_projections", []),
                lambda item, pid=f"projection::{_safe_id(filename)}::l0::{_safe_id(step_key)}::{direction}": item.get("projection_id") == pid,
                spine_source,
            )
            _attach_typed_transition_projection(page_env, transition, component_id)
            if direction == "source":
                _add_unique_dict(
                    page_env.setdefault("oracles", []),
                    {
                        "oracle_type": "component_interactable",
                        "step_key": step_key,
                        "component_ref": component_id,
                        "expected": "L0 source component is visible and actionable.",
                        "dom_contract": interaction_contract.get("dom_contract", {}),
                        "source": spine_source,
                    },
                    ("oracle_type", "step_key", "component_ref"),
                )
                _promote_matching_source(
                    page_env.get("oracles", []),
                    lambda item, sk=step_key, cid=component_id: item.get("oracle_type") == "component_interactable" and item.get("step_key") == sk and item.get("component_ref") == cid,
                    spine_source,
                )
                if target and target != source:
                    _add_unique_dict(
                        page_env.setdefault("oracles", []),
                        {
                            "oracle_type": "route_target",
                            "step_key": step_key,
                            "component_ref": component_id,
                            "expected_filename": target,
                            "source": spine_source,
                        },
                        ("oracle_type", "step_key", "expected_filename"),
                    )
                    _promote_matching_source(
                        page_env.get("oracles", []),
                        lambda item, sk=step_key, target=target: item.get("oracle_type") == "route_target" and item.get("step_key") == sk and item.get("expected_filename") == target,
                        spine_source,
                    )
            effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            if str(effect.get("effect") or ""):
                _add_unique_dict(
                    page_env.setdefault("oracles", []),
                    {
                        "oracle_type": "state_mutation",
                        "step_key": step_key,
                        "component_ref": component_id,
                        "state_ref": str(effect.get("state_ref") or effect.get("draft_ref") or effect.get("query_state_owner") or ""),
                        "expected": str(effect.get("effect") or ""),
                        "source": spine_source,
                    },
                    ("oracle_type", "step_key", "component_ref"),
                )
                _promote_matching_source(
                    page_env.get("oracles", []),
                    lambda item, sk=step_key, cid=component_id: item.get("oracle_type") == "state_mutation" and item.get("step_key") == sk and item.get("component_ref") == cid,
                    spine_source,
                )
            if _effect_requires_record_identity(effect):
                _ensure_record_identity_contract(page_env, transition)
                _add_unique_dict(
                    page_env.setdefault("oracles", []),
                    {
                        "oracle_type": "collection_preservation",
                        "step_key": step_key,
                        "component_ref": component_id,
                        "state_ref": _record_collection_ref(effect),
                        "mutation_policy": str(effect.get("collection_merge_policy") or "preserve_existing_then_append_or_upsert"),
                        "source": spine_source,
                    },
                    ("oracle_type", "step_key", "state_ref"),
                )
                _promote_matching_source(
                    page_env.get("oracles", []),
                    lambda item, sk=step_key, state_ref=_record_collection_ref(effect): item.get("oracle_type") == "collection_preservation" and item.get("step_key") == sk and item.get("state_ref") == state_ref,
                    spine_source,
                )
            closed_steps.add(step_key)
        _rebuild_page_query_projections(page_env)
    payload["contract_closure"] = {
        "format": "page_environment_contract_closure",
        "schema_revision": 1,
        "closed_l0_step_count": len(closed_steps),
        "source": "deterministic_closure_after_llm_semantic_resolution",
    }


async def enrich_page_environment_specs_async(
    site_model,
    result_dir: Path,
    page_environment_specs: Dict[str, Any],
    *,
    page_behaviors: Dict[str, Any] | None = None,
    content_data_by_page: Dict[str, Dict[str, Any]] | None = None,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_query_groups_per_call: int = 12,
    max_component_pages_per_call: int = 6,
    component_projection_input_budget_tokens: int = _COMPONENT_PROJECTION_INPUT_BUDGET_TOKENS,
    component_projection_repair_max_rounds: int = 3,
    max_concurrent: int = 8,
    max_tokens: int = 16000,
    run_phase_3_cross_page_decisions: bool = True,
    phase_3_logicflow: str = PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY,
    phase_3_max_tokens: int = 12000,
    logger: Any | None = None,
) -> Dict[str, Any]:
    usage_records: List[Any] = []
    errors: List[str] = []

    query_tasks = _collect_query_identity_tasks(page_environment_specs, site_model)
    deterministic_query_resolutions, llm_query_tasks, query_policy_report = _partition_query_identity_tasks(query_tasks)
    query_event_admission = _query_event_admission_summary(query_tasks)
    query_event_admission.update(query_policy_report)
    page_environment_specs["query_event_admission"] = query_event_admission
    query_resolutions: List[Dict[str, Any]] = []
    query_input_signature = _query_identity_input_signature(
        query_tasks,
        llm_query_tasks,
        deterministic_query_resolutions,
        model=model,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        max_query_groups_per_call=max_query_groups_per_call,
    )
    page_envs = page_environment_specs.get("page_environment_specs_by_filename", {}) if isinstance(page_environment_specs, dict) else {}
    _planner_log_info(
        logger,
        "[PageEnv UnifiedPlanner] enrichment start "
        f"pages={len(page_envs) if isinstance(page_envs, dict) else 0} "
        f"query_tasks={len(query_tasks)} query_deterministic={len(deterministic_query_resolutions)} "
        f"query_llm_tasks={len(llm_query_tasks)} max_concurrent={max(1, int(max_concurrent or 1))} "
        f"model={model or ''} reasoning_effort={reasoning_effort} "
        f"run_phase_3={bool(run_phase_3_cross_page_decisions)} "
        f"phase_3_logicflow={_normalize_phase_3_logicflow(phase_3_logicflow)}",
    )

    async def run_query_chunk(
        chunk: Sequence[Dict[str, Any]],
        chunk_index: int,
        total_chunks: int,
    ) -> Dict[str, Any]:
        started_at = time.monotonic()
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=1 query_identity chunk_start "
            f"chunk={chunk_index}/{total_chunks} tasks={len(chunk)}",
        )
        try:
            reply = await _resolve_query_identity_chunk(
                chunk,
                model=model,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
            )
            resolutions = reply.get("query_identity_resolutions", []) if isinstance(reply, dict) else []
            chunk_wall_s = time.monotonic() - started_at
            usage = reply.get("_usage") if isinstance(reply, dict) else None
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=1 query_identity chunk_done "
                f"chunk={chunk_index}/{total_chunks} tasks={len(chunk)} "
                f"resolutions={len(resolutions) if isinstance(resolutions, list) else 0} "
                f"tokens={_usage_total_tokens(usage)} "
                f"duration_s={chunk_wall_s:.1f} duration_kind=chunk_wall "
                f"{_usage_timing_log_fragment(usage, chunk_wall_s=chunk_wall_s)}",
            )
            return reply
        except Exception as exc:
            _planner_log_warning(
                logger,
                "[PageEnv UnifiedPlanner] phase=1 query_identity chunk_failed "
                f"chunk={chunk_index}/{total_chunks} tasks={len(chunk)} "
                f"duration_s={time.monotonic() - started_at:.1f} error={exc}",
            )
            return {"query_identity_resolutions": [], "error": str(exc)}

    if query_tasks:
        query_resolutions = list(deterministic_query_resolutions)
        query_cache = _read_unified_planner_phase_cache(
            result_dir,
            "query_identity_resolutions.json",
            expected_format="page_environment_unified_planner_query_identity_cache",
            input_signature=query_input_signature,
        )
        cached_query_resolutions = (
            query_cache.get("resolutions", [])
            if isinstance(query_cache, dict) and isinstance(query_cache.get("resolutions"), list)
            else []
        )
        cached_query_by_id = {
            str(item.get("query_id") or ""): item
            for item in cached_query_resolutions
            if isinstance(item, dict) and item.get("query_id")
        }
        cached_query_completion_report = _query_identity_completion_report(
            llm_query_tasks,
            cached_query_by_id,
            source="cache",
        )
        if len(cached_query_resolutions) >= len(llm_query_tasks) and cached_query_completion_report.get("status") == "complete":
            page_environment_specs["query_identity_completion"] = cached_query_completion_report
            try:
                completion_report_path = _write_query_identity_completion_report(
                    result_dir,
                    cached_query_completion_report,
                )
                cached_query_completion_report["path"] = str(completion_report_path)
            except Exception as exc:
                _planner_log_warning(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=1 query_identity completion_report_failed "
                    f"error={exc}",
                )
            query_resolutions.extend([
                _validate_query_identity_resolution(task, cached_query_by_id.get(str(task.get("query_id") or "")))
                for task in llm_query_tasks
            ])
            _apply_query_identity_resolutions(page_environment_specs, query_resolutions)
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=1 query_identity cache_hit "
                f"tasks={len(query_tasks)} deterministic={len(deterministic_query_resolutions)} "
                f"llm_tasks={len(llm_query_tasks)} resolutions={len(query_resolutions)}",
            )
        else:
            if query_cache is not None:
                    _planner_log_warning(
                        logger,
                        "[PageEnv UnifiedPlanner] phase=1 query_identity cache_miss "
                        f"reason=incomplete_cached_resolutions tasks={len(query_tasks)} "
                        f"llm_tasks={len(llm_query_tasks)} "
                        f"cached={len(cached_query_resolutions)} "
                        f"missing={cached_query_completion_report.get('missing_query_count', 0)}",
                    )
            else:
                _planner_log_info(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=1 query_identity cache_miss "
                    f"reason=missing_or_signature_mismatch tasks={len(query_tasks)} "
                    f"llm_tasks={len(llm_query_tasks)}",
                )
            query_chunks = list(_chunked(llm_query_tasks, max_query_groups_per_call))
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=1 query_identity start "
                f"tasks={len(query_tasks)} deterministic={len(deterministic_query_resolutions)} "
                f"llm_tasks={len(llm_query_tasks)} chunks={len(query_chunks)}",
            )
            phase_error_count_before = len(errors)
            query_replies = await asyncio.gather(
                *(
                    run_query_chunk(chunk, index, len(query_chunks))
                    for index, chunk in enumerate(query_chunks, start=1)
                )
            )
            raw_by_id: Dict[str, Dict[str, Any]] = {}
            phase_errors: List[str] = []
            for reply in query_replies:
                if not isinstance(reply, dict):
                    continue
                if reply.get("error"):
                    phase_errors.append(str(reply.get("error")))
                if reply.get("_usage"):
                    usage_records.append(reply.get("_usage"))
                for item in reply.get("query_identity_resolutions", []) if isinstance(reply.get("query_identity_resolutions"), list) else []:
                    if isinstance(item, dict) and item.get("query_id"):
                        raw_by_id[str(item["query_id"])] = item
            query_completion_report = _query_identity_completion_report(
                llm_query_tasks,
                raw_by_id,
                source="fresh_llm",
                errors=phase_errors,
            )
            page_environment_specs["query_identity_completion"] = query_completion_report
            try:
                completion_report_path = _write_query_identity_completion_report(
                    result_dir,
                    query_completion_report,
                )
                query_completion_report["path"] = str(completion_report_path)
            except Exception as exc:
                _planner_log_warning(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=1 query_identity completion_report_failed "
                    f"error={exc}",
                )
            if query_completion_report.get("status") != "complete":
                missing_query_ids = query_completion_report.get("missing_query_ids", [])
                message = (
                    "[PageEnv UnifiedPlanner] phase=1 query identity incomplete: "
                    f"llm_tasks={len(llm_query_tasks)} raw_resolutions={len(raw_by_id)} "
                    f"errors={len(phase_errors)} "
                    f"missing={len(missing_query_ids) if isinstance(missing_query_ids, list) else 0} "
                    f"sample_missing={','.join(missing_query_ids[:8]) if isinstance(missing_query_ids, list) else ''}"
                )
                errors.extend(phase_errors)
                errors.append(message)
                _planner_log_warning(logger, message)
                raise RuntimeError(message)
            query_resolutions.extend([
                _validate_query_identity_resolution(task, raw_by_id.get(str(task.get("query_id") or "")))
                for task in llm_query_tasks
            ])
            _apply_query_identity_resolutions(page_environment_specs, query_resolutions)
            if len(errors) == phase_error_count_before:
                cache_path = _write_unified_planner_phase_cache(
                    result_dir,
                    "query_identity_resolutions.json",
                    {
                        "format": "page_environment_unified_planner_query_identity_cache",
                        "schema_revision": 1,
                        "input_signature": query_input_signature,
                        "task_count": len(llm_query_tasks),
                        "all_task_count": len(query_tasks),
                        "deterministic_resolution_count": len(deterministic_query_resolutions),
                        "resolution_count": len(query_resolutions) - len(deterministic_query_resolutions),
                        "resolutions": query_resolutions[len(deterministic_query_resolutions):],
                    },
                )
                _planner_log_info(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=1 query_identity cache_write "
                    f"path={cache_path.relative_to(Path(result_dir))}",
                )
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=1 query_identity complete "
                f"tasks={len(query_tasks)} deterministic={len(deterministic_query_resolutions)} "
                f"llm_tasks={len(llm_query_tasks)} resolutions={len(query_resolutions)} errors={len(errors)}",
            )

    page_metadata = _page_metadata_by_filename(site_model)
    _observations_by_step, observations_by_trace_cluster = _runtime_observation_indexes(site_model)
    interaction_contracts = _interaction_contracts_by_filename(site_model)
    content_context = {
        filename: _page_content_context(site_model, page)
        for filename, page in page_metadata.items()
    }
    component_tasks = [
        task for filename, page_env in page_envs.items() if isinstance(page_envs, dict)
        for task in [_component_task_for_page(
            str(filename),
            page_env,
            page_metadata=page_metadata,
            page_content_context=content_context,
            runtime_observations_by_trace_cluster=observations_by_trace_cluster,
            interaction_contracts_by_filename=interaction_contracts,
        )]
        if task
    ]
    component_signature_payload = {
        "phase": L0_COMPONENT_RENDER_BINDING_SIGNATURE_PHASE,
        "schema_revision": 2,
        "request_split_strategy": _COMPONENT_PROJECTION_REQUEST_SPLIT_STRATEGY,
        "tasks": component_tasks,
        "model": model or "",
        "reasoning_effort": reasoning_effort,
        "max_tokens": max_tokens,
        "max_component_pages_per_call": max_component_pages_per_call,
        "component_projection_input_budget_tokens": component_projection_input_budget_tokens,
        "component_projection_repair_max_rounds": component_projection_repair_max_rounds,
    }
    component_input_signature = _stable_json_signature(component_signature_payload)
    legacy_component_input_signature = _stable_json_signature(
        dict(component_signature_payload, phase=LEGACY_COMPONENT_PROJECTION_SIGNATURE_PHASE)
    )
    component_outputs: List[Dict[str, Any]] = []
    component_chunk_budget_report: Dict[str, Any] = {
        "format": L0_COMPONENT_RENDER_BINDING_BUDGET_FORMAT,
        "schema_revision": 2,
        "strategy": "not_run",
        "request_split_strategy": _COMPONENT_PROJECTION_REQUEST_SPLIT_STRATEGY,
        "hard_constraint": "input_tokens",
        "input_budget_tokens": int(component_projection_input_budget_tokens or 0),
        "total_input_tokens": 0,
        "chunk_count": 0,
        "chunks": [],
    }

    async def run_component_chunk(
        chunk: Sequence[Dict[str, Any]],
        chunk_index: int,
        total_chunks: int,
    ) -> Dict[str, Any]:
        started_at = time.monotonic()
        filenames = [
            str(task.get("filename") or "")
            for task in chunk
            if isinstance(task, dict) and task.get("filename")
        ]
        chunk_budget = (
            component_chunk_budget_report.get("chunks", [])[chunk_index - 1]
            if isinstance(component_chunk_budget_report.get("chunks"), list)
            and chunk_index - 1 < len(component_chunk_budget_report.get("chunks", []))
            else {}
        )
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding chunk_start "
            f"chunk={chunk_index}/{total_chunks} pages={len(chunk)} "
            f"input_tokens={chunk_budget.get('input_tokens', 'unknown')} "
            f"completion_proxy={chunk_budget.get('completion_proxy_tokens', 'unknown')} "
            f"balance_load={chunk_budget.get('balance_load', 'unknown')} "
            f"filenames={','.join(filenames[:8])}",
        )
        try:
            reply = await _enrich_component_projection_chunk(
                chunk,
                model=model,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
            )
            pages = reply.get("pages", []) if isinstance(reply, dict) else []
            chunk_wall_s = time.monotonic() - started_at
            usage = reply.get("_usage") if isinstance(reply, dict) else None
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding chunk_done "
                f"chunk={chunk_index}/{total_chunks} pages={len(chunk)} "
                f"outputs={len(pages) if isinstance(pages, list) else 0} "
                f"input_tokens={chunk_budget.get('input_tokens', 'unknown')} "
                f"completion_proxy={chunk_budget.get('completion_proxy_tokens', 'unknown')} "
                f"tokens={_usage_total_tokens(usage)} "
                f"duration_s={chunk_wall_s:.1f} duration_kind=chunk_wall "
                f"{_usage_timing_log_fragment(usage, chunk_wall_s=chunk_wall_s)}",
            )
            return reply
        except Exception as exc:
            _planner_log_warning(
                logger,
                "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding chunk_failed "
                f"chunk={chunk_index}/{total_chunks} pages={len(chunk)} "
                f"duration_s={time.monotonic() - started_at:.1f} error={exc}",
            )
            return {"pages": [], "error": str(exc)}

    async def run_component_repair_chunk(
        chunk: Sequence[Dict[str, Any]],
        repair_round: int,
        chunk_index: int,
        total_chunks: int,
    ) -> Dict[str, Any]:
        started_at = time.monotonic()
        filenames = [
            str(task.get("filename") or "")
            for task in chunk
            if isinstance(task, dict) and task.get("filename")
        ]
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding repair_chunk_start "
            f"round={repair_round} chunk={chunk_index}/{total_chunks} pages={len(chunk)} "
            f"filenames={','.join(filenames[:8])}",
        )
        try:
            reply = await _enrich_component_projection_chunk(
                chunk,
                model=model,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
            )
            pages = reply.get("pages", []) if isinstance(reply, dict) else []
            chunk_wall_s = time.monotonic() - started_at
            usage = reply.get("_usage") if isinstance(reply, dict) else None
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding repair_chunk_done "
                f"round={repair_round} chunk={chunk_index}/{total_chunks} pages={len(chunk)} "
                f"outputs={len(pages) if isinstance(pages, list) else 0} "
                f"tokens={_usage_total_tokens(usage)} "
                f"duration_s={chunk_wall_s:.1f} duration_kind=chunk_wall "
                f"{_usage_timing_log_fragment(usage, chunk_wall_s=chunk_wall_s)}",
            )
            return reply
        except Exception as exc:
            _planner_log_warning(
                logger,
                "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding repair_chunk_failed "
                f"round={repair_round} chunk={chunk_index}/{total_chunks} pages={len(chunk)} "
                f"duration_s={time.monotonic() - started_at:.1f} error={exc}",
            )
            return {"pages": [], "error": str(exc)}

    async def run_component_chunks_bounded(
        chunks: Sequence[Sequence[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        limit = max(1, int(max_concurrent or 1))
        semaphore = asyncio.Semaphore(limit)
        results: List[Dict[str, Any] | None] = [None] * len(chunks)

        async def run_one(index: int, chunk: Sequence[Dict[str, Any]]) -> None:
            async with semaphore:
                results[index - 1] = await run_component_chunk(chunk, index, len(chunks))

        await asyncio.gather(
            *(run_one(index, chunk) for index, chunk in enumerate(chunks, start=1))
        )
        return [item if isinstance(item, dict) else {"pages": [], "error": "missing chunk result"} for item in results]

    async def run_component_repair_chunks_bounded(
        chunks: Sequence[Sequence[Dict[str, Any]]],
        *,
        repair_round: int,
    ) -> List[Dict[str, Any]]:
        limit = max(1, int(max_concurrent or 1))
        semaphore = asyncio.Semaphore(limit)
        results: List[Dict[str, Any] | None] = [None] * len(chunks)

        async def run_one(index: int, chunk: Sequence[Dict[str, Any]]) -> None:
            async with semaphore:
                results[index - 1] = await run_component_repair_chunk(
                    chunk,
                    repair_round,
                    index,
                    len(chunks),
                )

        await asyncio.gather(
            *(run_one(index, chunk) for index, chunk in enumerate(chunks, start=1))
        )
        return [item if isinstance(item, dict) else {"pages": [], "error": "missing repair chunk result"} for item in results]

    if component_tasks:
        component_cache = _read_unified_planner_phase_cache_candidates(
            result_dir,
            (
                (
                    L0_COMPONENT_RENDER_BINDING_CACHE_FILE,
                    L0_COMPONENT_RENDER_BINDING_CACHE_FORMAT,
                    component_input_signature,
                ),
                (
                    LEGACY_COMPONENT_PROJECTION_CACHE_FILE,
                    LEGACY_COMPONENT_PROJECTION_CACHE_FORMAT,
                    legacy_component_input_signature,
                ),
            ),
        )
        cached_component_outputs = (
            component_cache.get("outputs", [])
            if isinstance(component_cache, dict) and isinstance(component_cache.get("outputs"), list)
            else []
        )
        cached_component_dicts = [dict(item) for item in cached_component_outputs if isinstance(item, dict)]
        cached_completion_report = _component_projection_completion_report(
            component_tasks,
            cached_component_dicts,
            source="cache",
        )
        if len(cached_component_outputs) >= len(component_tasks) and cached_completion_report.get("status") == "complete":
            component_outputs = cached_component_dicts
            page_environment_specs["l0_component_render_binding_completion"] = cached_completion_report
            page_environment_specs["component_projection_completion"] = cached_completion_report
            try:
                completion_report_path = _write_component_projection_completion_report(
                    result_dir,
                    cached_completion_report,
                )
                cached_completion_report["path"] = str(completion_report_path)
            except Exception as exc:
                _planner_log_warning(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding completion_report_failed "
                    f"error={exc}",
                )
            cached_budget_report = (
                component_cache.get("chunk_budget_report", {})
                if isinstance(component_cache, dict) and isinstance(component_cache.get("chunk_budget_report"), dict)
                else {}
            )
            if cached_budget_report:
                component_chunk_budget_report = dict(cached_budget_report)
                if component_chunk_budget_report.get("format") == LEGACY_COMPONENT_PROJECTION_BUDGET_FORMAT:
                    component_chunk_budget_report["legacy_format"] = LEGACY_COMPONENT_PROJECTION_BUDGET_FORMAT
                    component_chunk_budget_report["format"] = L0_COMPONENT_RENDER_BINDING_BUDGET_FORMAT
            _apply_component_projection_enrichment(page_environment_specs, component_outputs)
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding cache_hit "
                f"source={component_cache.get('_cache_filename', '') if isinstance(component_cache, dict) else ''} "
                f"tasks={len(component_tasks)} outputs={len(component_outputs)}",
            )
        else:
            if component_cache is not None:
                _planner_log_warning(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding cache_miss "
                    f"reason=incomplete_cached_outputs tasks={len(component_tasks)} "
                    f"cached={len(cached_component_outputs)} "
                    f"missing={len(cached_completion_report.get('missing_filenames', []))}",
                )
            else:
                _planner_log_info(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding cache_miss "
                    f"reason=missing_or_signature_mismatch tasks={len(component_tasks)}",
                )
            if int(component_projection_input_budget_tokens or 0) > 0:
                component_chunks, component_chunk_budget_report = _build_component_projection_chunks(
                    component_tasks,
                    input_budget_tokens=component_projection_input_budget_tokens,
                )
            else:
                component_chunks = list(_chunked(component_tasks, max_component_pages_per_call))
                component_chunk_budget_report = {
                    "format": L0_COMPONENT_RENDER_BINDING_BUDGET_FORMAT,
                    "schema_revision": 2,
                    "strategy": "fixed_page_count",
                    "request_split_strategy": "disabled_fixed_page_count",
                    "hard_constraint": "page_count",
                    "max_component_pages_per_call": max_component_pages_per_call,
                    "input_budget_tokens": 0,
                    "total_input_tokens": 0,
                    "chunk_count": len(component_chunks),
                    "chunks": [
                        {
                            "chunk": index,
                            "page_count": len(chunk),
                            "filenames": [
                                str(task.get("filename") or "")
                                for task in chunk
                                if isinstance(task, dict)
                            ],
                        }
                        for index, chunk in enumerate(component_chunks, start=1)
                    ],
                }
            try:
                report_path = _write_component_projection_chunk_budget_report(result_dir, component_chunk_budget_report)
                component_chunk_budget_report["path"] = str(report_path)
            except Exception as exc:
                _planner_log_warning(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding budget_report_failed "
                    f"error={exc}",
                )
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding start "
                f"tasks={len(component_tasks)} chunks={len(component_chunks)} "
                f"strategy={component_chunk_budget_report.get('strategy', '')} "
                f"input_budget={component_chunk_budget_report.get('input_budget_tokens', '')} "
                f"scheduler_max_inflight={max(1, int(max_concurrent or 1))}",
            )
            phase_error_count_before = len(errors)
            component_replies = await run_component_chunks_bounded(component_chunks)
            for reply in component_replies:
                if not isinstance(reply, dict):
                    continue
                if reply.get("error"):
                    errors.append(str(reply.get("error")))
                if reply.get("_usage"):
                    usage_records.append(reply.get("_usage"))
                for item in reply.get("pages", []) if isinstance(reply.get("pages"), list) else []:
                    if isinstance(item, dict):
                        component_outputs.append(item)
            completion_report = _component_projection_completion_report(
                component_tasks,
                component_outputs,
                source="fresh_llm",
            )
            component_task_by_filename = {
                str(task.get("filename") or "").strip(): task
                for task in component_tasks
                if isinstance(task, dict) and str(task.get("filename") or "").strip()
            }
            repair_report: Dict[str, Any] = {
                "format": L0_COMPONENT_RENDER_BINDING_REPAIR_FORMAT,
                "schema_revision": 1,
                "status": "not_needed" if completion_report.get("status") == "complete" else "incomplete",
                "enabled": int(component_projection_repair_max_rounds or 0) > 0,
                "max_rounds": max(0, int(component_projection_repair_max_rounds or 0)),
                "round_count": 0,
                "initial_missing_filenames": list(completion_report.get("missing_filenames", []) or []),
                "final_missing_filenames": list(completion_report.get("missing_filenames", []) or []),
                "recovered_filenames": [],
                "rounds": [],
            }
            if completion_report.get("status") != "complete" and repair_report["enabled"]:
                for repair_round in range(1, int(repair_report["max_rounds"]) + 1):
                    missing_filenames = [
                        str(item)
                        for item in (completion_report.get("missing_filenames", []) or [])
                        if str(item) in component_task_by_filename
                    ]
                    if not missing_filenames:
                        break
                    repair_tasks = [component_task_by_filename[filename] for filename in missing_filenames]
                    repair_chunks = [[task] for task in repair_tasks]
                    _planner_log_info(
                        logger,
                        "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding repair_round_start "
                        f"round={repair_round} missing={len(missing_filenames)} chunks={len(repair_chunks)} "
                        f"scheduler_max_inflight={max(1, int(max_concurrent or 1))}",
                    )
                    repair_replies = await run_component_repair_chunks_bounded(
                        repair_chunks,
                        repair_round=repair_round,
                    )
                    recovered_filenames: List[str] = []
                    round_errors: List[str] = []
                    existing_filenames = {
                        str(item.get("filename") or "").strip()
                        for item in component_outputs
                        if isinstance(item, dict) and str(item.get("filename") or "").strip()
                    }
                    for reply in repair_replies:
                        if not isinstance(reply, dict):
                            continue
                        if reply.get("error"):
                            round_errors.append(str(reply.get("error")))
                        if reply.get("_usage"):
                            usage_records.append(reply.get("_usage"))
                        for item in reply.get("pages", []) if isinstance(reply.get("pages"), list) else []:
                            if not isinstance(item, dict):
                                continue
                            filename = str(item.get("filename") or "").strip()
                            if not filename or filename in existing_filenames:
                                continue
                            component_outputs.append(item)
                            existing_filenames.add(filename)
                            if filename in missing_filenames:
                                recovered_filenames.append(filename)
                    completion_report = _component_projection_completion_report(
                        component_tasks,
                        component_outputs,
                        source="fresh_llm_with_repair",
                    )
                    repair_report["round_count"] = repair_round
                    repair_report["rounds"].append({
                        "round": repair_round,
                        "missing_before": missing_filenames,
                        "repair_chunk_count": len(repair_chunks),
                        "recovered_filenames": sorted(set(recovered_filenames)),
                        "error_count": len(round_errors),
                        "errors": round_errors[:8],
                        "missing_after": list(completion_report.get("missing_filenames", []) or []),
                    })
                    repair_report["recovered_filenames"] = sorted(set(
                        list(repair_report.get("recovered_filenames", []) or []) + recovered_filenames
                    ))
                    repair_report["final_missing_filenames"] = list(completion_report.get("missing_filenames", []) or [])
                    _planner_log_info(
                        logger,
                        "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding repair_round_done "
                        f"round={repair_round} recovered={len(set(recovered_filenames))} "
                        f"remaining={len(completion_report.get('missing_filenames', []) or [])}",
                    )
                    if completion_report.get("status") == "complete":
                        break
                repair_report["status"] = "complete" if completion_report.get("status") == "complete" else "incomplete"
            page_environment_specs["l0_component_render_binding_repair"] = repair_report
            page_environment_specs["component_projection_repair"] = repair_report
            try:
                repair_report_path = _write_component_projection_repair_report(result_dir, repair_report)
                repair_report["path"] = str(repair_report_path)
            except Exception as exc:
                _planner_log_warning(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding repair_report_failed "
                    f"error={exc}",
                )
            page_environment_specs["l0_component_render_binding_completion"] = completion_report
            page_environment_specs["component_projection_completion"] = completion_report
            try:
                completion_report_path = _write_component_projection_completion_report(
                    result_dir,
                    completion_report,
                )
                completion_report["path"] = str(completion_report_path)
            except Exception as exc:
                _planner_log_warning(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding completion_report_failed "
                    f"error={exc}",
                )
            if completion_report.get("status") != "complete":
                missing_filenames = completion_report.get("missing_filenames", [])
                message = (
                    "[PageEnv UnifiedPlanner] phase=2 l0 component render binding incomplete: "
                    f"tasks={len(component_tasks)} outputs={len(component_outputs)} "
                    f"missing={len(missing_filenames) if isinstance(missing_filenames, list) else 0} "
                    f"sample_missing={','.join(missing_filenames[:8]) if isinstance(missing_filenames, list) else ''}"
                )
                errors.append(message)
                _planner_log_warning(logger, message)
                raise RuntimeError(message)
            _apply_component_projection_enrichment(page_environment_specs, component_outputs)
            if len(errors) == phase_error_count_before:
                cache_path = _write_unified_planner_phase_cache(
                    result_dir,
                    L0_COMPONENT_RENDER_BINDING_CACHE_FILE,
                    {
                        "format": L0_COMPONENT_RENDER_BINDING_CACHE_FORMAT,
                        "schema_revision": 2,
                        "request_split_strategy": _COMPONENT_PROJECTION_REQUEST_SPLIT_STRATEGY,
                        "input_signature": component_input_signature,
                        "task_count": len(component_tasks),
                        "output_count": len(component_outputs),
                        "outputs": component_outputs,
                        "chunk_budget_report": component_chunk_budget_report,
                    },
                )
                _planner_log_info(
                    logger,
                    "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding cache_write "
                    f"path={cache_path.relative_to(Path(result_dir))}",
                )
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=2 l0_component_render_binding complete "
                f"tasks={len(component_tasks)} outputs={len(component_outputs)} errors={len(errors)}",
            )

    phase_3_meta: Dict[str, Any] = {"enabled": False}
    if run_phase_3_cross_page_decisions:
        normalized_phase_3_logicflow = _normalize_phase_3_logicflow(phase_3_logicflow)
        if normalized_phase_3_logicflow == PHASE_3_LOGICFLOW_LEGACY_UNIFIED_PLANNER:
            phase_3_meta = await _run_phase_3_cross_page_decisions_async(
                site_model,
                result_dir,
                page_environment_specs,
                page_behaviors=page_behaviors,
                content_data_by_page=content_data_by_page,
                model=model,
                reasoning_effort=reasoning_effort,
                max_concurrent=max_concurrent,
                max_tokens=phase_3_max_tokens,
                logger=logger,
            )
            phase_3_meta["logicflow"] = PHASE_3_LOGICFLOW_LEGACY_UNIFIED_PLANNER
            phase_3_meta["source_logicflow"] = PHASE_3_LOGICFLOW_LEGACY_UNIFIED_PLANNER
        else:
            phase_3_meta = _run_phase_3_closure_default_review_only(
                page_environment_specs,
                logger=logger,
            )
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=3 summary "
            f"pages={phase_3_meta.get('page_count', 0)} "
            f"resolved={phase_3_meta.get('resolved_count', 0)} "
            f"errors={len(phase_3_meta.get('errors', []) if isinstance(phase_3_meta.get('errors'), list) else [])}",
        )

    close_page_environment_contracts(page_environment_specs)
    page_environment_specs["semantic_enrichment"] = {
        "format": "page_environment_semantic_enrichment",
        "schema_revision": 2,
        "resolver": "llm_three_phase_enrich_query_l0_component_render_binding_crosspage",
        "model": model or "",
        "reasoning_effort": reasoning_effort,
        "query_task_count": len(query_tasks),
        "query_event_admission": query_event_admission,
        "query_resolution_count": len(query_resolutions),
        "l0_component_render_binding_task_count": len(component_tasks),
        "l0_component_render_binding_resolution_count": len(component_outputs),
        "l0_component_render_binding_chunk_budget_report": component_chunk_budget_report,
        "component_page_task_count": len(component_tasks),
        "component_page_resolution_count": len(component_outputs),
        "component_projection_chunk_budget_report": component_chunk_budget_report,
        "phase_3_cross_page_decisions": phase_3_meta,
        "errors": errors[:16],
        "usage_records": usage_records[:16],
    }
    return page_environment_specs


def enrich_page_environment_specs(
    site_model,
    result_dir: Path,
    page_environment_specs: Dict[str, Any],
    *,
    page_behaviors: Dict[str, Any] | None = None,
    content_data_by_page: Dict[str, Dict[str, Any]] | None = None,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_query_groups_per_call: int = 12,
    max_component_pages_per_call: int = 6,
    component_projection_input_budget_tokens: int = _COMPONENT_PROJECTION_INPUT_BUDGET_TOKENS,
    component_projection_repair_max_rounds: int = 3,
    max_concurrent: int = 8,
    max_tokens: int = 16000,
    run_phase_3_cross_page_decisions: bool = True,
    phase_3_logicflow: str = PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY,
    phase_3_max_tokens: int = 12000,
    logger: Any | None = None,
) -> Dict[str, Any]:
    return asyncio.run(
        enrich_page_environment_specs_async(
            site_model,
            result_dir,
            page_environment_specs,
            page_behaviors=page_behaviors,
            content_data_by_page=content_data_by_page,
            model=model,
            reasoning_effort=reasoning_effort,
            max_query_groups_per_call=max_query_groups_per_call,
            max_component_pages_per_call=max_component_pages_per_call,
            component_projection_input_budget_tokens=component_projection_input_budget_tokens,
            component_projection_repair_max_rounds=component_projection_repair_max_rounds,
            max_concurrent=max_concurrent,
            max_tokens=max_tokens,
            run_phase_3_cross_page_decisions=run_phase_3_cross_page_decisions,
            phase_3_logicflow=phase_3_logicflow,
            phase_3_max_tokens=phase_3_max_tokens,
            logger=logger,
        )
    )


def _compact_group_for_llm(group: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "conflict_id": group.get("conflict_id", ""),
        "conflict_type": group.get("conflict_type", ""),
        "filename": group.get("filename", ""),
        "page_id": group.get("page_id", ""),
        "general_page_id": group.get("general_page_id", ""),
        "materialized_page_key": group.get("materialized_page_key", ""),
        "surface": group.get("surface", ""),
        "policy_intent": group.get("policy_intent", ""),
        "hard_default_candidate_id": group.get("hard_default_candidate_id", ""),
        "hard_constraints": group.get("hard_constraints", []),
        "candidates": [
            {
                key: candidate.get(key)
                for key in (
                    "candidate_id",
                    "role",
                    "l0_role",
                    "surface",
                    "filename",
                    "materialized_page_key",
                    "split_dimension",
                    "lifecycle_phase",
                    "exclude_from_initial_value_hints",
                    "display_text",
                    "interaction_outcome",
                    "target_filename",
                    "target_page_name",
                    "route_source",
                    "route_fallback",
                    "source_lifecycle",
                    "state_ref",
                    "reason",
                )
                if key in candidate and candidate.get(key) not in ("", [], {})
            }
            for candidate in group.get("candidates", [])[:20]
            if isinstance(candidate, dict)
        ],
    }


async def _resolve_conflict_group_chunk(
    groups: Sequence[Dict[str, Any]],
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_tokens: int = 12000,
) -> Dict[str, Any]:
    from trace2world_pipeline.core.llm_caller import call_openai_api_json_async

    messages = [
        {
            "role": "system",
            "content": (
                "You resolve pre-PageGen page-environment conflicts. "
                "Return JSON only. Your job is to turn fragmented contract evidence into "
                "deterministic render/state decisions before HTML generation. "
                "Do not invent app-specific facts. Preserve L0 execution anchors."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "Resolve these conflict groups into deterministic PageGen decisions.",
                    "principles": [
                        "For materialized_page_sibling_scope, selected_default must be the current page candidate.",
                        "Do not merge sibling materialized pages, query states, or fixed-slot alternatives into one initial render.",
                        "Execution anchors must remain executable and must not be dropped.",
                        "Runtime-only observations appear only after the matching transition.",
                        "Visual samples may guide style but are not required render obligations.",
                        "When one generic trigger contains multiple semantic component roles, use component_splits instead of adjacent duplicate controls.",
                    ],
                    "output_schema": {
                        "resolutions": [
                            {
                                "conflict_id": "string",
                                "selected_default": "candidate_id",
                                "state_variants": ["candidate_id"],
                                "runtime_only": ["candidate_id"],
                                "section_render": ["candidate_id"],
                                "visual_samples": ["candidate_id"],
                                "dropped": ["candidate_id"],
                                "component_splits": [
                                    {
                                        "component_role": "header|bottom_navigation|collection_item|form_submit|other",
                                        "candidate_ids": ["candidate_id"],
                                    }
                                ],
                                "reason_code": "short_snake_case",
                                "confidence": 0.0,
                                "notes": "short explanation",
                            }
                        ]
                    },
                    "groups": [_compact_group_for_llm(group) for group in groups],
                },
                ensure_ascii=False,
            ),
        },
    ]
    reply, usage = await call_openai_api_json_async(
        messages,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        model=model,
        stage="page_environment_conflict_resolver",
    )
    try:
        payload = json.loads(reply) if isinstance(reply, str) else reply
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["_usage"] = usage
    return payload


async def resolve_page_environment_conflicts_async(
    site_model,
    result_dir: Path,
    page_environment_specs: Dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_groups_per_call: int = 8,
    max_concurrent: int = 8,
    max_tokens: int = 12000,
) -> Dict[str, Any]:
    by_filename = page_environment_specs.get("page_environment_specs_by_filename", {}) if isinstance(page_environment_specs, dict) else {}
    if not isinstance(by_filename, dict):
        return page_environment_specs
    groups_payload = build_page_environment_conflict_groups(site_model, by_filename, result_dir=result_dir)
    flat_groups = [
        group
        for groups in groups_payload.get("groups_by_filename", {}).values()
        for group in (groups if isinstance(groups, list) else [])
        if isinstance(group, dict)
    ]
    if not flat_groups:
        page_environment_specs["conflict_resolution"] = {
            "format": "page_environment_conflict_resolution",
            "schema_revision": 1,
            "resolver": "not_needed",
            "group_count": 0,
            "resolution_count": 0,
            "groups": groups_payload,
            "resolutions": [],
        }
        return page_environment_specs

    async def run_chunk(chunk: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            return await _resolve_conflict_group_chunk(
                chunk,
                model=model,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            return {
                "resolutions": [],
                "error": str(exc),
            }

    chunks = _chunked(flat_groups, max_groups_per_call)
    replies = await asyncio.gather(*(run_chunk(chunk) for chunk in chunks))
    raw_by_id: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    usage_records: List[Any] = []
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        if reply.get("error"):
            errors.append(str(reply.get("error")))
        if reply.get("_usage"):
            usage_records.append(reply.get("_usage"))
        for item in reply.get("resolutions", []) if isinstance(reply.get("resolutions"), list) else []:
            if isinstance(item, dict) and item.get("conflict_id"):
                raw_by_id[str(item["conflict_id"])] = item

    validated_by_id = {
        str(group.get("conflict_id") or ""): _validate_resolution(group, raw_by_id.get(str(group.get("conflict_id") or "")))
        for group in flat_groups
        if str(group.get("conflict_id") or "")
    }
    apply_page_environment_conflict_resolutions(page_environment_specs, groups_payload, validated_by_id)
    page_environment_specs["conflict_resolution"] = {
        "format": "page_environment_conflict_resolution",
        "schema_revision": 1,
        "resolver": "llm_semantic_conflict_resolver_with_deterministic_validation",
        "model": model or "",
        "reasoning_effort": reasoning_effort,
        "group_count": len(flat_groups),
        "resolution_count": len(validated_by_id),
        "fallback_count": sum(1 for item in validated_by_id.values() if item.get("resolver") == "deterministic_fallback"),
        "errors": errors[:16],
        "usage_records": usage_records[:16],
        "groups": groups_payload,
        "resolutions": list(validated_by_id.values()),
    }
    return page_environment_specs


def resolve_page_environment_conflicts(
    site_model,
    result_dir: Path,
    page_environment_specs: Dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_groups_per_call: int = 8,
    max_concurrent: int = 8,
    max_tokens: int = 12000,
) -> Dict[str, Any]:
    return asyncio.run(
        resolve_page_environment_conflicts_async(
            site_model,
            result_dir,
            page_environment_specs,
            model=model,
            reasoning_effort=reasoning_effort,
            max_groups_per_call=max_groups_per_call,
            max_concurrent=max_concurrent,
            max_tokens=max_tokens,
        )
    )


def _build_environment_action_route_page_payload(
    filename: str,
    page_env: Dict[str, Any],
    page_behaviors: Dict[str, Any],
    content_data: Dict[str, Any],
    interaction_contracts: List[Dict[str, Any]],
    conflict_groups: List[Dict[str, Any]],
    query_tasks: List[Dict[str, Any]],
    route_controls: List[Dict[str, Any]],
    candidate_targets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    page_name = str(page_env.get("page_name") or page_env.get("materialized_page_id") or filename)
    general_page_id = str(page_env.get("general_page_id") or "")
    materialized_page_key = str(page_env.get("materialized_page_key") or "")
    transitions = page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []
    compact_transitions = [
        {
            key: value
            for key, value in {
                "transition_id": str(t.get("transition_id") or ""),
                "operation": str(t.get("operation") or ""),
                "command": _clip_text(t.get("command"), 120),
                "source_filename": _base_filename(t.get("source_filename")),
                "target_filename": _base_filename(t.get("target_filename")),
                "direction": str(t.get("direction") or ""),
                "reducer": str(t.get("reducer") or ""),
                "l0_role": str(t.get("l0_role") or ""),
                "input_text": _clip_text(t.get("input_text"), 120),
            }.items()
            if value not in ("", None)
        }
        for t in transitions[:24]
        if isinstance(t, dict)
    ]

    page_id = str(page_env.get("site_model_page_id") or page_env.get("page_id") or "")
    behavior = page_behaviors.get(page_id, {}) if isinstance(page_behaviors, dict) else {}
    navigation_out = behavior.get("navigation_out", []) if isinstance(behavior, dict) else []
    compact_nav_out = [
        {
            key: value
            for key, value in {
                "target_filename": _base_filename(item.get("target_filename")),
                "trigger_text": _clip_text(item.get("trigger_text") or item.get("label"), 80),
                "surface": str(item.get("surface") or ""),
                "binding_kind": str(item.get("binding_kind") or ""),
            }.items()
            if value not in ("", None)
        }
        for item in (navigation_out if isinstance(navigation_out, list) else [])[:16]
        if isinstance(item, dict)
    ]

    entity_summary = content_data.get("extracted_item_summary", {}) if isinstance(content_data, dict) else {}
    component_mapping = content_data.get("entity_component_mapping", [])[:12] if isinstance(content_data, dict) else []

    payload: Dict[str, Any] = {
        "filename": filename,
        "page_name": page_name,
        "general_page_id": general_page_id,
        "materialized_page_key": materialized_page_key,
    }
    if compact_transitions:
        payload["transitions"] = compact_transitions
    if compact_nav_out:
        payload["navigation_out"] = compact_nav_out
    if interaction_contracts:
        payload["interaction_contracts"] = interaction_contracts[:12]
    if conflict_groups:
        payload["conflict_groups"] = [_compact_group_for_llm(g) for g in conflict_groups[:8]]
    if query_tasks:
        payload["query_tasks"] = [_compact_query_task_for_llm(t) for t in query_tasks[:8]]
    if route_controls:
        payload["route_controls"] = route_controls[:16]
    if candidate_targets:
        payload["candidate_targets"] = candidate_targets[:12]
    if entity_summary:
        payload["content_entities"] = {k: v[:4] for k, v in entity_summary.items()} if isinstance(entity_summary, dict) else {}
    if component_mapping:
        payload["component_mapping"] = component_mapping
    return payload


_ENVIRONMENT_ACTION_ROUTE_PLANNER_SYSTEM = (
    "You are the unified PageGen environment planner. For each page, produce a "
    "single JSON object that resolves all four decision domains in one pass:\n"
    "1) route_affordances — which visible controls navigate to other pages;\n"
    "2) interaction_contracts — what state/action each interaction triggers;\n"
    "3) conflict_resolutions — which competing content is default vs state_variant;\n"
    "4) query_bindings — classify search-like transitions semantically.\n\n"
    "Return JSON only. Preserve L0 execution anchors. Do not invent data or "
    "hallucinate page names not present in the input."
)

_ENVIRONMENT_ACTION_ROUTE_PLANNER_PRINCIPLES = [
    "Route affordances: only declare routes where evidence (route_controls, navigation_out, candidate_targets) supports the link.",
    "Interaction contracts: emit rich interaction_contracts first; typed_actions are a compatibility projection. Keep contract_ids stable from input interaction_contracts.",
    "Conflict resolutions: for materialized_page_sibling_scope, selected_default must be the current page. Runtime-only observations do not appear at initial render.",
    "Query bindings: classify each query_task as query_entry, committed_result, suggestion_projection, or unrelated_navigation. Mark mismatch if query and target disagree.",
    "If a domain has no input candidates for a page, return an empty array for that field.",
    "state_signature_hints: short semantic tags summarizing what makes this page's initial state unique (e.g. 'home_grid', 'search_results_for_query').",
]

async def _resolve_environment_action_route_page(
    page_payload: Dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_tokens: int = 12000,
) -> Dict[str, Any]:
    from trace2world_pipeline.core.llm_caller import call_openai_api_json_async

    messages = [
        {"role": "system", "content": _ENVIRONMENT_ACTION_ROUTE_PLANNER_SYSTEM},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "Produce the environment_action_route_plan for this page.",
                    "principles": _ENVIRONMENT_ACTION_ROUTE_PLANNER_PRINCIPLES,
                    "output_schema": {
                        "filename": "string",
                        "route": {
                            "path": "string",
                            "params": {},
                            "queryParams": {},
                            "entryPoint": "home|none",
                            "scroll_containers": [],
                        },
                        "ui_states": [
                            {
                                "id": "string",
                                "search": {},
                                "description": "string",
                                "actions": [
                                    {
                                        "id": "string",
                                        "label": "string",
                                        "behavior": "navigate|interact|mutate|overlay",
                                        "target_route": "string_or_empty",
                                    }
                                ],
                            }
                        ],
                        "typed_actions": [
                            {
                                "action_id": "string",
                                "label": "string",
                                "behavior": "navigate|interact|mutate|overlay",
                                "target_filename": "string_or_empty",
                                "state_effect": "string_or_empty",
                                "surface": "string",
                            }
                        ],
                        "interaction_contracts": [
                            {
                                "contract_id": "string",
                                "candidate_id": "string",
                                "action_semantic": "navigate|mutate|overlay|interact",
                                "state_ref": "string",
                                "entity_ref": "string",
                                "identity_evidence": [],
                                "route_binding": {},
                                "sdk_effect": {},
                                "source": "unified_environment_action_route_planner",
                            }
                        ],
                        "navigation_edges": [
                            {
                                "control_label": "string",
                                "target_filename": "string",
                                "surface": "string",
                                "confidence": 0.0,
                            }
                        ],
                        "query_bindings": [
                            {
                                "query_id": "string",
                                "query_transition_kind": "query_entry|committed_result|suggestion_projection|selected_suggestion_commit|search_within_collection|unrelated_navigation|unknown",
                                "target_identity_status": "match|mismatch|not_applicable|unknown",
                                "target_identity_hints": [],
                                "reason_code": "string",
                            }
                        ],
                        "conflict_resolutions": [
                            {
                                "conflict_id": "string",
                                "selected_default": "candidate_id",
                                "state_variants": ["candidate_id"],
                                "runtime_only": ["candidate_id"],
                                "dropped": ["candidate_id"],
                                "reason_code": "string",
                            }
                        ],
                        "state_signature_hints": ["string"],
                    },
                    "page": page_payload,
                },
                ensure_ascii=False,
            ),
        },
    ]
    reply, usage = await call_openai_api_json_async(
        messages,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        model=model,
        stage="environment_action_route_planner",
    )
    try:
        payload = json.loads(reply) if isinstance(reply, str) else reply
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["_usage"] = usage
    return payload


def _normalize_unified_interaction_contract(raw: Dict[str, Any]) -> Dict[str, Any]:
    contract_id = str(raw.get("contract_id") or raw.get("action_id") or raw.get("candidate_id") or "").strip()
    candidate_id = str(raw.get("candidate_id") or contract_id).strip()
    route_binding = raw.get("route_binding", {}) if isinstance(raw.get("route_binding"), dict) else {}
    sdk_effect = raw.get("sdk_effect", {}) if isinstance(raw.get("sdk_effect"), dict) else {}
    contract = {
        "contract_id": contract_id,
        "candidate_id": candidate_id,
        "action_semantic": str(raw.get("action_semantic") or raw.get("behavior") or "interact"),
        "state_ref": str(raw.get("state_ref") or ""),
        "entity_ref": str(raw.get("entity_ref") or ""),
        "identity_evidence": raw.get("identity_evidence", [])[:12] if isinstance(raw.get("identity_evidence"), list) else [],
        "route_binding": {
            key: (_base_filename(value) if key in {"target_filename", "source_filename"} else value)
            for key, value in route_binding.items()
            if value not in ("", [], {}, None)
        },
        "sdk_effect": {
            key: value
            for key, value in sdk_effect.items()
            if value not in ("", [], {}, None)
        },
        "source": str(raw.get("source") or "unified_environment_action_route_planner"),
    }
    if raw.get("label"):
        contract["label"] = str(raw.get("label") or "")
    if raw.get("candidate") and isinstance(raw.get("candidate"), dict):
        contract["candidate"] = raw.get("candidate")
    if isinstance(raw.get("state_surface_contract"), dict):
        contract["state_surface_contract"] = raw.get("state_surface_contract")
    if raw.get("contract_status"):
        contract["contract_status"] = str(raw.get("contract_status") or "")
    if raw.get("broadcast_generation_authority") is not None:
        contract["broadcast_generation_authority"] = bool(raw.get("broadcast_generation_authority"))
    return {
        key: value
        for key, value in contract.items()
        if value not in ("", [], {}, None)
    }


def _typed_action_from_interaction_contract(contract: Dict[str, Any]) -> Dict[str, Any]:
    route_binding = contract.get("route_binding", {}) if isinstance(contract.get("route_binding"), dict) else {}
    sdk_effect = contract.get("sdk_effect", {}) if isinstance(contract.get("sdk_effect"), dict) else {}
    candidate = contract.get("candidate", {}) if isinstance(contract.get("candidate"), dict) else {}
    action_id = str(contract.get("contract_id") or contract.get("candidate_id") or "").strip()
    label = str(contract.get("label") or candidate.get("display_text") or candidate.get("element_name") or sdk_effect.get("trigger") or action_id)
    behavior = str(contract.get("action_semantic") or sdk_effect.get("effect") or "interact")
    target_filename = _base_filename(route_binding.get("target_filename") or candidate.get("target_filename"))
    state_effect = str(sdk_effect.get("effect") or sdk_effect.get("method") or behavior)
    return {
        key: value
        for key, value in {
            "action_id": action_id,
            "label": label,
            "behavior": behavior,
            "target_filename": target_filename,
            "state_effect": state_effect,
            "surface": str(contract.get("surface") or candidate.get("surface") or ""),
            "source": "environment_action_route_planner_interaction_contract",
        }.items()
        if value not in ("", [], {}, None)
    }


def _interaction_contract_from_typed_action(action: Dict[str, Any]) -> Dict[str, Any]:
    action_id = str(action.get("action_id") or "").strip()
    target_filename = _base_filename(action.get("target_filename"))
    behavior = str(action.get("behavior") or "interact")
    route_binding: Dict[str, Any] = {}
    if target_filename:
        route_binding["target_filename"] = target_filename
    state_effect = str(action.get("state_effect") or "")
    if state_effect:
        route_binding["rationale"] = state_effect
    sdk_effect = {
        "effect": behavior,
        "trigger": str(action.get("label") or ""),
    }
    return _normalize_unified_interaction_contract({
        "contract_id": action_id,
        "candidate_id": action_id,
        "label": str(action.get("label") or ""),
        "action_semantic": behavior,
        "route_binding": route_binding,
        "sdk_effect": sdk_effect,
        "source": "unified_environment_action_route_planner_typed_action_fallback",
    })


def _validated_environment_action_route_conflict_resolutions(
    conflict_groups: List[Dict[str, Any]],
    raw_resolutions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    raw_by_id = {
        str(item.get("conflict_id") or ""): item
        for item in raw_resolutions
        if isinstance(item, dict) and str(item.get("conflict_id") or "")
    }
    resolutions: List[Dict[str, Any]] = []
    for group in conflict_groups if isinstance(conflict_groups, list) else []:
        if not isinstance(group, dict):
            continue
        conflict_id = str(group.get("conflict_id") or "")
        if not conflict_id:
            continue
        resolution = _validate_resolution(group, raw_by_id.get(conflict_id))
        resolution["candidate_summary"] = _resolution_candidate_summary(group)
        resolutions.append(resolution)
    return resolutions


def _apply_environment_action_route_plan_to_page(
    page_env: Dict[str, Any],
    plan: Dict[str, Any],
    *,
    conflict_groups: List[Dict[str, Any]] | None = None,
    fallback_interaction_contracts: List[Dict[str, Any]] | None = None,
) -> None:
    if not isinstance(plan, dict) or not isinstance(page_env, dict):
        return


    navigation_edges = plan.get("navigation_edges", [])
    if isinstance(navigation_edges, list) and navigation_edges:
        existing = page_env.setdefault("route_affordances", [])
        for edge in navigation_edges:
            if not isinstance(edge, dict):
                continue
            target = _base_filename(edge.get("target_filename"))
            label = str(edge.get("control_label") or "").strip()
            if not target or not label:
                continue
            _add_unique_dict(
                existing,
                {
                    "target_filename": target,
                    "control_label": label,
                    "surface": str(edge.get("surface") or "shell_navigation"),
                    "confidence": float(edge.get("confidence") or 0.8),
                    "source": "environment_action_route_planner",
                },
                ("target_filename", "control_label"),
            )


    interaction_contracts = plan.get("interaction_contracts", [])
    if (not isinstance(interaction_contracts, list) or not interaction_contracts) and fallback_interaction_contracts:
        interaction_contracts = fallback_interaction_contracts
    if isinstance(interaction_contracts, list) and interaction_contracts:
        existing_contracts = page_env.setdefault("interaction_contracts", [])
        existing_actions = page_env.setdefault("typed_actions", [])
        for raw_contract in interaction_contracts:
            if not isinstance(raw_contract, dict):
                continue
            contract = _normalize_unified_interaction_contract(raw_contract)
            contract_id = str(contract.get("contract_id") or "").strip()
            if not contract_id:
                continue
            _add_unique_dict(existing_contracts, contract, ("contract_id",))
            typed_projection = _typed_action_from_interaction_contract(contract)
            if typed_projection.get("action_id"):
                _add_unique_dict(existing_actions, typed_projection, ("action_id",))


    typed_actions = plan.get("typed_actions", [])
    if isinstance(typed_actions, list) and typed_actions:
        existing = page_env.setdefault("typed_actions", [])
        existing_contracts = page_env.setdefault("interaction_contracts", [])
        for action in typed_actions:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("action_id") or "").strip()
            if not action_id:
                continue
            _add_unique_dict(
                existing,
                {
                    "action_id": action_id,
                    "label": str(action.get("label") or ""),
                    "behavior": str(action.get("behavior") or "interact"),
                    "target_filename": _base_filename(action.get("target_filename")),
                    "state_effect": str(action.get("state_effect") or ""),
                    "surface": str(action.get("surface") or ""),
                    "source": "environment_action_route_planner",
                },
                ("action_id",),
            )
            fallback_contract = _interaction_contract_from_typed_action(action)
            if fallback_contract.get("contract_id"):
                _add_unique_dict(existing_contracts, fallback_contract, ("contract_id",))


    conflict_resolutions = plan.get("conflict_resolutions", [])
    groups = conflict_groups or []
    if groups:
        page_env["conflict_resolutions"] = _validated_environment_action_route_conflict_resolutions(
            groups,
            conflict_resolutions if isinstance(conflict_resolutions, list) else [],
        )
        page_env.setdefault("conflict_resolution_summary", {})["resolved_by"] = (
            "unified_environment_action_route_planner_with_deterministic_validation"
        )
    elif isinstance(conflict_resolutions, list) and conflict_resolutions:
        page_env["conflict_resolutions"] = conflict_resolutions
        page_env.setdefault("conflict_resolution_summary", {})["resolved_by"] = (
            "unified_environment_action_route_planner_with_deterministic_validation"
        )


    query_bindings = plan.get("query_bindings", [])
    if isinstance(query_bindings, list) and query_bindings:
        page_env["query_identity_resolutions"] = query_bindings


    state_hints = plan.get("state_signature_hints", [])
    if isinstance(state_hints, list) and state_hints:
        page_env["state_signature_hints"] = [str(h) for h in state_hints[:8] if h]


    route = plan.get("route")
    if isinstance(route, dict):
        page_env.setdefault("route_metadata", {}).update({
            key: value
            for key, value in {
                "path": str(route.get("path") or ""),
                "entryPoint": str(route.get("entryPoint") or "none"),
                "scroll_containers": route.get("scroll_containers", []),
            }.items()
            if value not in ("", [], None)
        })


    ui_states = plan.get("ui_states", [])
    if isinstance(ui_states, list) and ui_states:
        page_env["ui_states"] = [
            {
                key: value
                for key, value in {
                    "id": str(us.get("id") or ""),
                    "search": us.get("search", {}),
                    "description": str(us.get("description") or ""),
                    "actions": us.get("actions", []),
                }.items()
                if value not in ("", [], {}, None)
            }
            for us in ui_states
            if isinstance(us, dict) and str(us.get("id") or "")
        ]


def _normalize_shell_control_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"+", "＋", "add", "plus", "发布", "发表", "create_entry", "open_create_sheet", "act_open_create_sheet"}:
        return "+"
    if any(token in text for token in ("+", "＋")) and any(
        token in text
        for token in ("create", "entry", "publish", "compose", "sheet", "发布", "发表")
    ):
        return "+"
    if text in {"首页", "home", "home_tab", "discover", "首页tab"}:
        return "home"
    if text in {"消息", "messages", "message", "inbox", "notification", "notifications"}:
        return "messages"
    if text in {"我", "我的", "me", "profile", "mine", "account", "me_profile"}:
        return "profile"
    if text in {"市集", "market", "shop", "mall", "store"}:
        return "market"
    return re.sub(r"\s+", "", text)


def _is_create_entry_signal(*values: Any) -> bool:
    text = " ".join(str(value or "").lower() for value in values if str(value or "").strip())
    return any(
        token in text
        for token in (
            "create_entry",
            "create",
            "open_create_sheet",
            "open_create_action_sheet",
            "publish",
            "compose",
            "发布",
            "发表",
        )
    ) and any(
        token in text
        for token in ("sheet", "overlay", "modal", "drawer", "publish", "create", "发布", "发表")
    )


def _display_shell_control_label(control: Dict[str, Any]) -> str:
    if str(control.get("normalized_label") or "") == "+":
        return "+"
    if str(control.get("normalized_label") or "") == "home":
        return "首页"
    if str(control.get("normalized_label") or "") == "messages":
        return "消息"
    if str(control.get("normalized_label") or "") == "profile":
        return "我"
    return str(control.get("control_label") or "")


def _shell_surface_from_interaction_text(*values: Any) -> str:
    text = " ".join(str(value or "").lower() for value in values if str(value or "").strip())
    if not text:
        return ""
    if any(
        token in text
        for token in (
            "底部导航",
            "底部导航栏",
            "bottom nav",
            "bottom navigation",
            "bottom tab",
            "tab bar",
        )
    ):
        return "bottom_tab_bar"
    return ""


def _active_shell_owner_label(page_env: Dict[str, Any]) -> str:
    candidates = " ".join(
        str(value or "")
        for value in (
            page_env.get("page_name"),
            page_env.get("general_page_id"),
            page_env.get("materialized_page_key"),
            page_env.get("route_path"),
            page_env.get("filename"),
        )
        if str(value or "").strip()
    ).lower()
    if not candidates:
        return ""
    if "首页" in candidates or re.search(r"(^|[_/.-])home($|[_/.-])", candidates):
        return "首页"
    if "我的" in candidates or any(token in candidates for token in ("profile", "account", "mine", "user_profile")):
        return "我的"
    if "消息" in candidates or any(token in candidates for token in ("message", "inbox", "notification")):
        return "消息"
    return ""


def _page_env_identity_record(filename: Any, page_env: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(page_env, dict):
        return {}
    record = {
        "filename": _base_filename(filename),
        "page_id": str(page_env.get("site_model_page_id") or page_env.get("page_id") or "").strip(),
        "general_page_id": str(page_env.get("general_page_id") or "").strip(),
        "materialized_page_key": str(page_env.get("materialized_page_key") or page_env.get("instance_key") or "").strip(),
        "materialized_page_id": str(page_env.get("materialized_page_id") or "").strip(),
        "page_name": str(page_env.get("page_name") or "").strip(),
    }
    return {key: value for key, value in record.items() if value}


def _page_env_evidence_steps(page_env: Dict[str, Any]) -> List[str]:
    if not isinstance(page_env, dict):
        return []
    steps: List[str] = []
    provenance = page_env.get("provenance") if isinstance(page_env.get("provenance"), dict) else {}
    for source in (
        page_env.get("evidence_steps"),
        provenance.get("evidence_steps") if isinstance(provenance, dict) else None,
    ):
        if isinstance(source, list):
            steps.extend(str(step) for step in source if str(step or "").strip())
        elif source:
            steps.append(str(source))
    return _dedupe(steps, limit=16)


def _shell_evidence_records(evidence_steps: Any) -> List[Dict[str, str]]:
    steps = evidence_steps if isinstance(evidence_steps, list) else ([evidence_steps] if evidence_steps else [])
    return [
        {"kind": "reference_step", "step_key": str(step)}
        for step in _dedupe([str(step) for step in steps if str(step or "").strip()], limit=16)
    ]


def _shell_surface_family(value: Any) -> str:
    if isinstance(value, dict):
        candidates = [
            value.get("surface"),
            value.get("surface_id"),
            value.get("surface_role"),
            value.get("position"),
            value.get("component_role"),
            value.get("component_kind"),
        ]
    else:
        candidates = [value]
    text = " ".join(str(item or "").lower() for item in candidates if str(item or "").strip())
    if any(
        token in text
        for token in (
            "c_bottom_nav",
            "bottom_nav",
            "bottom navigation",
            "bottom_navigation",
            "bottom tab",
            "bottom_tab",
            "bottom tab bar",
            "tab bar (首页",
        )
    ):
        return "bottom_tab_bar"
    if any(token in text for token in ("header", "top_navigation", "top_nav", "c_top_nav", "toolbar")):
        return "top_navigation_bar"
    if any(token in text for token in ("shell", "global_navigation", "navigation", "nav")):
        return "shell_navigation"
    return ""


def _surface_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("surface", "surface_id", "surface_role", "position", "component_role", "component_kind"):
            if value.get(key):
                return str(value.get(key) or "")
        return ""
    return str(value or "")


def _shell_control_record(
    *,
    filename: str,
    label: Any,
    surface: Any,
    target_filename: Any = "",
    source: str = "",
    component_id: Any = "",
    evidence_steps: Any = None,
    confidence: Any = None,
    action_id: Any = "",
    behavior: Any = "",
    state_effect: Any = "",
    sdk_effect: Any = None,
    interaction_contract: Any = None,
    state_surface_contract: Any = None,
    contract_status: str = "",
) -> Dict[str, Any]:
    normalized_label = _normalize_shell_control_label(label)
    family = _shell_surface_family(surface)
    if not normalized_label or not family:
        return {}
    steps = evidence_steps if isinstance(evidence_steps, list) else ([evidence_steps] if evidence_steps else [])
    try:
        confidence_value = float(confidence) if confidence not in (None, "", [], {}) else 0.9
    except Exception:
        confidence_value = 0.9
    effect_payload = sdk_effect if isinstance(sdk_effect, dict) else {}
    effect_state_delta = effect_payload.get("state_delta") if isinstance(effect_payload.get("state_delta"), dict) else {}
    behavior_text = " ".join(
        str(item or "").lower()
        for item in (
            behavior,
            state_effect,
            effect_payload.get("effect"),
            effect_payload.get("operation"),
            effect_payload.get("reducer"),
            effect_payload.get("state_transition"),
            effect_payload.get("effect_type"),
            effect_payload.get("reducer_hint"),
            " ".join(str(key) for key in effect_state_delta.keys()),
        )
        if str(item or "").strip()
    )
    create_signal_text = " ".join(
        str(item or "").lower()
        for item in (label, action_id, behavior_text)
        if str(item or "").strip()
    )
    target = _base_filename(target_filename)
    source_filename = _base_filename(filename)
    overlay_like = any(token in behavior_text for token in ("overlay", "sheet", "popup", "dialog", "modal", "drawer"))
    create_entry_like = normalized_label == "+" and _is_create_entry_signal(create_signal_text)
    if (overlay_like or create_entry_like) and target == source_filename:
        target = ""
    if normalized_label == "+" and (overlay_like or create_entry_like):
        target = ""
    if target:
        interaction_kind = "navigate"
    elif overlay_like or create_entry_like:
        interaction_kind = "overlay"
    elif behavior_text:
        interaction_kind = "in_page"
    else:
        interaction_kind = ""
    record = {
        "source_filename": source_filename,
        "control_label": str(label or ""),
        "normalized_label": normalized_label,
        "surface": _surface_text(surface) or family,
        "shell_family_id": family,
        "component_id": str(component_id or ""),
        "target_filename": target,
        "source": str(source or ""),
        "action_id": str(action_id or ""),
        "evidence_steps": _dedupe([str(step) for step in steps if str(step or "").strip()], limit=16),
        "confidence": confidence_value,
        "interaction_kind": interaction_kind,
        "state_effect": str(state_effect or ""),
        "sdk_effect": dict(effect_payload),
    }
    if isinstance(interaction_contract, dict):
        record["interaction_contract"] = dict(interaction_contract)
    if isinstance(state_surface_contract, dict):
        record["state_surface_contract"] = dict(state_surface_contract)
    if contract_status:
        record["contract_status"] = str(contract_status)
    return record


def _quoted_label(value: Any) -> str:
    text = str(value or "")
    for left, right in (("“", "”"), ('"', '"'), ("'", "'")):
        if left in text and right in text:
            start = text.find(left)
            end = text.find(right, start + 1)
            if end > start:
                label = text[start + 1:end].strip()
                if label:
                    return label
    return ""


def _overlay_component_hint_from_contract(contract: Dict[str, Any]) -> str:
    candidates: List[str] = []
    sdk_effect = contract.get("sdk_effect") if isinstance(contract.get("sdk_effect"), dict) else {}
    for value in (
        contract.get("overlay_component"),
        contract.get("state_effect"),
        contract.get("contract_id"),
        contract.get("action_id"),
        contract.get("candidate_id"),
        contract.get("label"),
        contract.get("display_label"),
        sdk_effect.get("trigger"),
        sdk_effect.get("state_transition"),
        sdk_effect.get("effect"),
        sdk_effect.get("reducer"),
    ):
        text = str(value or "").strip().lower()
        if text:
            candidates.append(text)
    for text in candidates:
        match = re.search(r"(?:open|show|display)_([a-z0-9_]*(?:sheet|panel|drawer|menu|dialog|modal|popup)[a-z0-9_]*)", text)
        if match:
            return match.group(1)
    for text in candidates:
        match = re.search(r"([a-z0-9_]*(?:sheet|panel|drawer|menu|dialog|modal|popup)[a-z0-9_]*)", text)
        if match:
            return match.group(1)
    return ""


def _surface_matches_hint(surface: Any, hint: str) -> bool:
    if not hint:
        return False
    text = _surface_text(surface).lower()
    if not text:
        return False
    normalized = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    hint_norm = re.sub(r"[^a-z0-9]+", "_", hint.lower()).strip("_")
    if not hint_norm:
        return False
    if hint_norm in normalized or normalized.endswith(hint_norm):
        return True
    hint_tokens = [token for token in hint_norm.split("_") if token]
    return bool(hint_tokens and all(token in normalized for token in hint_tokens))


def _state_surface_contract_for_shell_interaction(
    page_env: Dict[str, Any],
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    sdk_effect = contract.get("sdk_effect") if isinstance(contract.get("sdk_effect"), dict) else {}
    overlay_component = str(
        sdk_effect.get("overlay_component")
        or contract.get("overlay_component")
        or ""
    ).strip()
    if not overlay_component:
        overlay_component = _overlay_component_hint_from_contract(contract)
    contract_id = str(contract.get("contract_id") or contract.get("action_id") or "").strip()
    if not overlay_component and "overlay" not in str(contract.get("action_semantic") or "").lower():
        return {}

    component_ref = ""
    transition_ids: set[str] = set()
    components = page_env.get("components") if isinstance(page_env.get("components"), list) else []
    for component in components:
        if not isinstance(component, dict):
            continue
        cid = str(component.get("component_id") or "")
        role = str(component.get("component_role") or "")
        if overlay_component and overlay_component not in cid and overlay_component not in role:
            continue
        component_ref = cid
        for key in ("l0_transition_ids", "source_step_keys"):
            values = component.get(key)
            if isinstance(values, list):
                transition_ids.update(str(item) for item in values if str(item or "").strip())
        break
    projections = page_env.get("render_projections") if isinstance(page_env.get("render_projections"), list) else []
    for projection in projections:
        if not isinstance(projection, dict):
            continue
        cref = str(projection.get("component_ref") or "")
        if component_ref and cref != component_ref:
            continue
        if overlay_component and overlay_component not in cref and overlay_component not in str(projection.get("projection_id") or ""):
            continue
        component_ref = component_ref or cref
        for key in ("l0_transition_ids", "source_step_keys"):
            values = projection.get(key)
            if isinstance(values, list):
                transition_ids.update(str(item) for item in values if str(item or "").strip())

    child_route_targets: set[str] = set()
    typed_actions = page_env.get("typed_actions") if isinstance(page_env.get("typed_actions"), list) else []
    for action in typed_actions:
        if not isinstance(action, dict):
            continue
        if not overlay_component or not _surface_matches_hint(action.get("surface"), overlay_component):
            continue
        if str(action.get("behavior") or "").lower() not in {"navigate", "route", "open_page"}:
            continue
        target = _base_filename(action.get("target_filename"))
        if target:
            child_route_targets.add(target)

    if not transition_ids and not child_route_targets:
        return {}

    options: List[Dict[str, Any]] = []
    option_targets_seen: set[str] = set()
    transitions = page_env.get("transitions") if isinstance(page_env.get("transitions"), list) else []
    for transition in transitions:
        if not isinstance(transition, dict):
            continue
        step_key = str(transition.get("step_key") or transition.get("transition_id") or "")
        target_filename = _base_filename(transition.get("target_filename"))
        if step_key not in transition_ids and target_filename not in child_route_targets:
            continue
        if str(transition.get("operation") or "").lower() not in {"click", "tap"}:
            continue
        label = _quoted_label(transition.get("operation_intent"))
        if not label or label == "+":
            continue
        if not target_filename or target_filename == _base_filename(transition.get("source_filename")):
            continue
        if child_route_targets and target_filename not in child_route_targets:
            continue
        if target_filename in option_targets_seen:
            continue
        option = {
            "label": label,
            "action_kind": "click",
            "expected_effect": {"type": "route", "target_filename": target_filename},
            "target_filename": target_filename,
            "source_step_key": step_key,
            "interactive_required": True,
        }
        if option not in options:
            options.append(option)
            option_targets_seen.add(target_filename)

    if child_route_targets:
        for action in typed_actions:
            if not isinstance(action, dict):
                continue
            target_filename = _base_filename(action.get("target_filename"))
            if not target_filename or target_filename in option_targets_seen:
                continue
            if target_filename not in child_route_targets:
                continue
            label = str(action.get("display_label") or action.get("label") or action.get("action_id") or "").strip()
            if not label:
                continue
            options.append(
                {
                    "label": label,
                    "action_kind": "click",
                    "expected_effect": {"type": "route", "target_filename": target_filename},
                    "target_filename": target_filename,
                    "source_step_key": str(action.get("step_key") or action.get("action_id") or ""),
                    "interactive_required": True,
                }
            )
            option_targets_seen.add(target_filename)

    if not component_ref and not options:
        return {}
    return {
        "surface_kind": "action_sheet" if overlay_component or options else "state_surface",
        "open_state_id": contract_id or "state_surface",
        "component_ref": component_ref,
        "overlay_component": overlay_component,
        "presentation": {"placement": "bottom", "layout": "source_contract"},
        "initial_visibility_policy": "hidden_until_transition",
        "runtime_visibility_policy": "visible_after_trigger",
        "overlay_layers": [
            {"role": "backdrop", "z_order": "below_foreground"},
            {"role": "foreground_panel", "z_order": "above_backdrop"},
        ],
        "options": options,
    }


def _normalize_in_page_interaction_contract(source: Dict[str, Any]) -> Dict[str, Any]:
    raw_contract = source.get("interaction_contract") if isinstance(source.get("interaction_contract"), dict) else {}
    state_surface = source.get("state_surface_contract") if isinstance(source.get("state_surface_contract"), dict) else {}
    contract_id = str(raw_contract.get("contract_id") or source.get("action_id") or source.get("evidence_action_id") or "").strip()
    interaction_kind = str(source.get("interaction_kind") or raw_contract.get("action_semantic") or "").strip()
    missing: List[str] = []
    if not state_surface:
        missing.append("state_surface_contract")
    elif not state_surface.get("options"):
        missing.append("state_surface_contract.options")
    status = "complete" if state_surface and not missing else "incomplete"
    contract = {
        "contract_id": contract_id,
        "trigger": str(source.get("control_label") or raw_contract.get("label") or ""),
        "interaction_kind": interaction_kind,
        "expected_effect": "open_state_surface" if interaction_kind else "",
        "source_filename": str(source.get("source_filename") or ""),
        "evidence_steps": source.get("evidence_steps", []),
        "sdk_effect": source.get("sdk_effect", {}),
        "state_surface_contract": state_surface,
    }
    return {
        "contract_status": status,
        "missing_contract_fields": missing,
        "interaction_contract": {k: v for k, v in contract.items() if v not in (None, "", [], {})},
    }


def _shell_families_for_page(page_env: Dict[str, Any]) -> set[str]:
    families: set[str] = set()
    if not isinstance(page_env, dict):
        return families
    for field in ("route_affordances", "typed_actions", "interaction_contracts", "components"):
        items = page_env.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            route_binding = item.get("route_binding") if isinstance(item.get("route_binding"), dict) else {}
            candidates = [
                item,
                item.get("surface"),
                item.get("component_role"),
                item.get("component_kind"),
                route_binding.get("surface"),
            ]
            interaction = item.get("interaction_contract") if isinstance(item.get("interaction_contract"), dict) else {}
            text_surface = _shell_surface_from_interaction_text(
                item.get("operation_intent"),
                item.get("required_display_label"),
                interaction.get("operation_intent"),
                interaction.get("command"),
                interaction.get("display_label"),
            )
            if text_surface:
                candidates.append(text_surface)
            for candidate in candidates:
                family = _shell_surface_family(candidate)
                if family:
                    families.add(family)
    return families


def _slot_contract_id(slot: Tuple[str, str]) -> str:
    return f"{slot[0]}::{slot[1]}"


def _shell_route_component_id(source_filename: Any, shell_family_id: Any, shell_slot_key: Any) -> str:
    source_slug = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "_",
        _base_filename(source_filename).replace(".html", ""),
        flags=re.UNICODE,
    ).strip("_")
    family_slug = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "_",
        str(shell_family_id or "").strip() or "shell",
        flags=re.UNICODE,
    ).strip("_")
    slot_slug = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "_",
        str(shell_slot_key or "").strip() or "slot",
        flags=re.UNICODE,
    ).strip("_")
    return f"cmp::{source_slug}_html::shell::{family_slug}::{slot_slug}"


def _compile_shell_navigation_contract(
    validated_by_slot: Dict[Tuple[str, str], List[Dict[str, Any]]],
    families_by_filename: Dict[str, set[str]],
    page_identity_by_filename: Dict[str, Dict[str, str]] | None = None,
) -> Tuple[Dict[str, Any], Dict[Tuple[str, str], Dict[str, Any]]]:
    identity_by_filename = page_identity_by_filename or {}
    contracts_by_slot: Dict[Tuple[str, str], Dict[str, Any]] = {}
    contract_rows: List[Dict[str, Any]] = []
    for slot, sources in sorted(validated_by_slot.items(), key=lambda item: _slot_contract_id(item[0])):
        family, label = slot
        selected_sources = [
            source for source in sources
            if str(source.get("source") or "") == "active_slot_owner"
        ]
        target_sources = [
            source for source in sources
            if str(source.get("target_filename") or "").strip()
            and str(source.get("source") or "") != "active_slot_owner"
        ]
        target_values = sorted({
            str(source.get("target_filename") or "").strip()
            for source in target_sources
            if str(source.get("target_filename") or "").strip()
        })
        in_page_sources = [
            source for source in sources
            if str(source.get("interaction_kind") or "").strip()
            and not str(source.get("target_filename") or "").strip()
        ]
        receiver_pages = sorted(
            filename
            for filename, families in families_by_filename.items()
            if family in families
        )
        status = "incomplete"
        target = ""
        source_control: Dict[str, Any] = {}
        interaction_kind = ""
        reason = ""
        if len(target_values) == 1:
            status = "complete"
            target = target_values[0]
            source_control = next(
                (source for source in target_sources if str(source.get("target_filename") or "") == target),
                target_sources[0] if target_sources else {},
            )
            interaction_kind = "navigate"
            reason = "unique_observed_shell_slot_target"
        elif len(target_values) > 1:
            status = "conflict"
            reason = "multiple_observed_targets_for_same_shell_slot"
        elif in_page_sources:
            status = "in_page_complete"
            source_control = in_page_sources[0]
            interaction_kind = str(source_control.get("interaction_kind") or "in_page")
            reason = "unique_observed_shell_slot_in_page_interaction"
        else:
            reason = "no_observed_target_or_interaction"
        source_filename = str(source_control.get("source_filename") or "")
        evidence_steps = _dedupe(
            [
                str(step)
                for step in (source_control.get("evidence_steps", []) if isinstance(source_control.get("evidence_steps", []), list) else [])
                if str(step or "").strip()
            ],
            limit=16,
        )
        selected_records: List[Dict[str, Any]] = []
        active_owner_filenames: List[str] = []
        for selected in selected_sources:
            selected_filename = str(selected.get("source_filename") or "").strip()
            if selected_filename and selected_filename not in active_owner_filenames:
                active_owner_filenames.append(selected_filename)
            selected_records.append(
                {
                    "source": "active_slot_owner",
                    "source_filename": selected_filename,
                    "active_owner_filename": selected_filename,
                    "evidence_steps": selected.get("evidence_steps", []),
                }
            )
        selected_slot_state = {
            "state_kind": "selected_slot_state",
            "active_owner_filenames": sorted(active_owner_filenames),
            "evidence": selected_records,
        }
        row = {
            "slot_id": _slot_contract_id(slot),
            "shell_family_id": family,
            "normalized_label": label,
            "status": status,
            "target_filename": target,
            "target_page_identity": dict(identity_by_filename.get(target, {})) if target else {},
            "interaction_kind": interaction_kind,
            "scope": "shared_shell" if family == "bottom_tab_bar" else "page_local",
            "receiver_pages": receiver_pages,
            "observed_source_count": len(sources),
            "observed_targets": target_values,
            "reason": reason,
            "authority": "observed_shell_controls",
            "source_filename": source_filename,
            "source_page_identity": dict(identity_by_filename.get(source_filename, {})) if source_filename else {},
            "source": str(source_control.get("source") or ""),
            "evidence_steps": evidence_steps,
            "evidence": _shell_evidence_records(evidence_steps),
            "selected_slot_state": selected_slot_state,
        }
        contract = dict(row)
        contract["source_control"] = source_control
        contracts_by_slot[slot] = contract
        contract_rows.append(row)
    audit_contract = {
        "format": "shell_navigation_contract",
        "schema_revision": 1,
        "authority": "observed_shell_controls",
        "slots": contract_rows,
        "families": [
            {
                "family_id": family,
                "surface_family": family,
                "scope": "app_global" if family == "bottom_tab_bar" else "page_local",
                "receiver_pages": sorted(
                    filename
                    for filename, families in families_by_filename.items()
                    if family in families
                ),
                "slots": [
                    row for row in contract_rows if row.get("shell_family_id") == family
                ],
                "conflicts": [
                    row for row in contract_rows
                    if row.get("shell_family_id") == family and row.get("status") == "conflict"
                ],
            }
            for family in sorted({row.get("shell_family_id") for row in contract_rows if row.get("shell_family_id")})
        ],
        "summary": {
            "slot_count": len(contract_rows),
            "complete_count": sum(1 for row in contract_rows if row.get("status") == "complete"),
            "in_page_complete_count": sum(1 for row in contract_rows if row.get("status") == "in_page_complete"),
            "conflict_count": sum(1 for row in contract_rows if row.get("status") == "conflict"),
            "incomplete_count": sum(1 for row in contract_rows if row.get("status") == "incomplete"),
        },
    }
    return audit_contract, contracts_by_slot


def _allow_shell_family_receiver(
    slot: Tuple[str, str],
    source: Dict[str, Any],
    slot_contract: Dict[str, Any] | None = None,
) -> bool:
    family, _label = slot
    if family == "bottom_tab_bar":
        contract = slot_contract or {}
        return str(contract.get("scope") or "") == "shared_shell" and str(contract.get("status") or "") in {
            "complete",
            "in_page_complete",
        }
    return False


def _iter_shell_controls_for_page(filename: str, page_env: Dict[str, Any]) -> List[Dict[str, Any]]:
    controls: List[Dict[str, Any]] = []
    if not isinstance(page_env, dict):
        return controls
    for affordance in page_env.get("route_affordances", []) if isinstance(page_env.get("route_affordances"), list) else []:
        if not isinstance(affordance, dict):
            continue
        record = _shell_control_record(
            filename=filename,
            label=affordance.get("control_label") or affordance.get("label") or affordance.get("display_label"),
            surface=affordance.get("surface"),
            target_filename=affordance.get("target_filename"),
            source=affordance.get("source") or "route_affordances",
            component_id=affordance.get("component_id") or affordance.get("component_ref"),
            evidence_steps=affordance.get("evidence_steps") or affordance.get("step_key"),
            confidence=affordance.get("confidence"),
            action_id=affordance.get("action_id"),
        )
        if record:
            controls.append(record)
    for action in page_env.get("typed_actions", []) if isinstance(page_env.get("typed_actions"), list) else []:
        if not isinstance(action, dict):
            continue
        action_contract = {
            "contract_id": action.get("action_id"),
            "action_id": action.get("action_id"),
            "label": action.get("label") or action.get("control_label"),
            "action_semantic": action.get("behavior"),
            "state_effect": action.get("state_effect"),
            "overlay_component": action.get("overlay_component"),
            "sdk_effect": {
                "effect": action.get("behavior"),
                "trigger": action.get("label") or action.get("control_label"),
                "state_transition": action.get("state_effect"),
            },
        }
        state_surface_contract = _state_surface_contract_for_shell_interaction(page_env, action_contract)
        record = _shell_control_record(
            filename=filename,
            label=action.get("label") or action.get("control_label"),
            surface=action.get("surface"),
            target_filename=action.get("target_filename"),
            source=action.get("source") or "typed_actions",
            evidence_steps=action.get("evidence_steps") or action.get("step_key"),
            action_id=action.get("action_id"),
            behavior=action.get("behavior"),
            state_effect=action.get("state_effect"),
            sdk_effect=action_contract["sdk_effect"],
            interaction_contract=action_contract,
            state_surface_contract=state_surface_contract,
            contract_status="complete" if state_surface_contract.get("options") else "incomplete",
        )
        if record:
            controls.append(record)
    for contract in page_env.get("interaction_contracts", []) if isinstance(page_env.get("interaction_contracts"), list) else []:
        if not isinstance(contract, dict):
            continue
        route_binding = contract.get("route_binding") if isinstance(contract.get("route_binding"), dict) else {}
        sdk_effect = contract.get("sdk_effect") if isinstance(contract.get("sdk_effect"), dict) else {}
        label = contract.get("label") or contract.get("display_label") or sdk_effect.get("trigger")
        surface = contract.get("surface") or route_binding.get("surface")
        if not label and _is_create_entry_signal(
            contract.get("contract_id"),
            contract.get("action_id"),
            contract.get("action_semantic"),
            route_binding.get("rationale"),
            sdk_effect.get("effect"),
            sdk_effect.get("effect_type"),
            sdk_effect.get("reducer"),
            sdk_effect.get("reducer_hint"),
            sdk_effect.get("trigger"),
        ):
            label = "create_entry"
        if not surface and _normalize_shell_control_label(label) == "+":
            surface = "bottom_tab_bar"
        state_surface_contract = _state_surface_contract_for_shell_interaction(page_env, contract)
        record = _shell_control_record(
            filename=filename,
            label=label,
            surface=surface,
            target_filename=route_binding.get("target_filename"),
            source=contract.get("source") or "interaction_contracts",
            evidence_steps=contract.get("identity_evidence") or contract.get("evidence_steps"),
            action_id=contract.get("contract_id"),
            behavior=contract.get("action_semantic"),
            state_effect=sdk_effect.get("state_transition") or sdk_effect.get("reducer"),
            sdk_effect=sdk_effect,
            interaction_contract=contract,
            state_surface_contract=state_surface_contract,
            contract_status="complete" if state_surface_contract.get("options") else "incomplete",
        )
        if record:
            controls.append(record)
    for component in page_env.get("components", []) if isinstance(page_env.get("components"), list) else []:
        if not isinstance(component, dict):
            continue
        interaction = component.get("interaction_contract") if isinstance(component.get("interaction_contract"), dict) else {}
        intent_surface = _shell_surface_from_interaction_text(
            component.get("operation_intent"),
            component.get("required_display_label"),
            interaction.get("operation_intent"),
            interaction.get("command"),
            interaction.get("display_label"),
        )
        expected_filename = interaction.get("expected_filename") or component.get("target_filename")
        intent_label = (
            component.get("control_label")
            or component.get("required_display_label")
            or interaction.get("display_label")
            or _quoted_or_clicked_label(interaction.get("operation_intent") or component.get("operation_intent"))
        )
        record = _shell_control_record(
            filename=filename,
            label=(
                intent_label
                or component.get("display_label")
                or component.get("label")
                or component.get("text")
                or component.get("component_label")
            ),
            surface=component.get("surface") or intent_surface or component,
            target_filename=expected_filename,
            source=component.get("source") or "component_shell_control",
            component_id=component.get("component_id") or component.get("component_ref"),
            evidence_steps=component.get("source_step_keys"),
            action_id=(component.get("source_step_keys") or [""])[0] if isinstance(component.get("source_step_keys"), list) else "",
            interaction_contract=interaction,
        )
        if record:
            controls.append(record)
    deduped: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for item in controls:
        key = (
            item.get("source_filename", ""),
            item.get("shell_family_id", ""),
            item.get("normalized_label", ""),
            item.get("target_filename", ""),
            item.get("interaction_kind", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def apply_shell_interaction_broadcast(page_environment_specs: Dict[str, Any]) -> Dict[str, Any]:
    by_filename = page_environment_specs.get("page_environment_specs_by_filename", {}) if isinstance(page_environment_specs, dict) else {}
    audit: Dict[str, Any] = {
        "format": "shell_interaction_broadcast",
        "schema_revision": 1,
        "source": "shell_navigation_contract",
        "propagation_count": 0,
        "propagated_routes": [],
        "propagated_interactions": [],
        "rejected_controls": [],
        "shell_navigation_contract": {
            "format": "shell_navigation_contract",
            "schema_revision": 1,
            "authority": "observed_shell_controls",
            "slots": [],
            "summary": {},
        },
    }
    if not isinstance(by_filename, dict) or not by_filename:
        if isinstance(page_environment_specs, dict):
            page_environment_specs["shell_interaction_broadcast"] = audit
        return audit

    controls_by_filename = {
        _base_filename(filename): _iter_shell_controls_for_page(_base_filename(filename), page_env)
        for filename, page_env in by_filename.items()
        if _base_filename(filename) and isinstance(page_env, dict)
    }
    families_by_filename = {
        _base_filename(filename): _shell_families_for_page(page_env)
        for filename, page_env in by_filename.items()
        if _base_filename(filename) and isinstance(page_env, dict)
    }
    page_identity_by_filename = {
        _base_filename(filename): _page_env_identity_record(_base_filename(filename), page_env)
        for filename, page_env in by_filename.items()
        if _base_filename(filename) and isinstance(page_env, dict)
    }
    observed_bottom_shell_pages = {
        filename
        for filename, families in families_by_filename.items()
        if "bottom_tab_bar" in families
    }
    bottom_route_observed_pages: set[str] = set()
    for filename, controls in controls_by_filename.items():
        for control in controls:
            if str(control.get("shell_family_id") or "") != "bottom_tab_bar":
                continue
            target = _base_filename(control.get("target_filename"))
            if target and target in families_by_filename:
                families_by_filename.setdefault(target, set()).add("bottom_tab_bar")
                observed_bottom_shell_pages.add(target)
                bottom_route_observed_pages.add(filename)
                bottom_route_observed_pages.add(target)
    for filename in sorted(bottom_route_observed_pages):
        page_env = by_filename.get(filename)
        if not isinstance(page_env, dict):
            continue
        label = _active_shell_owner_label(page_env)
        if not label:
            continue
        record = _shell_control_record(
            filename=filename,
            label=label,
            surface="bottom_tab_bar",
            target_filename=filename,
            source="active_slot_owner",
            evidence_steps=_page_env_evidence_steps(page_env),
            confidence=0.9,
            action_id=f"active_slot_owner::{_base_filename(filename)}",
        )
        if record:
            controls_by_filename.setdefault(filename, []).append(record)
    bottom_shell_pages = set(observed_bottom_shell_pages)
    validated_by_slot: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for controls in controls_by_filename.values():
        for control in controls:
            if control.get("source") == "shell_interaction_broadcast":
                continue
            if control.get("source") == "component_shell_family_receiver":
                continue
            if control.get("source") == "component_shell_control" and not (
                control.get("target_filename") or control.get("interaction_kind")
            ):
                continue
            if not control.get("target_filename") and not control.get("interaction_kind"):
                continue
            slot = (str(control.get("shell_family_id") or ""), str(control.get("normalized_label") or ""))
            if slot[0] and slot[1]:
                validated_by_slot[slot].append(control)
    shell_navigation_contract, contract_by_slot = _compile_shell_navigation_contract(
        validated_by_slot,
        families_by_filename,
        page_identity_by_filename,
    )
    audit["shell_navigation_contract"] = shell_navigation_contract

    retained_broadcast_route_keys: set[Tuple[str, str, str, str]] = set()
    for filename, page_env in by_filename.items():
        fn = _base_filename(filename)
        if not fn or not isinstance(page_env, dict):
            continue
        page_controls = list(controls_by_filename.get(fn, []))
        explicit_slots = {
            (
                str(control.get("shell_family_id") or ""),
                str(control.get("normalized_label") or ""),
            )
            for control in page_controls
        }
        for slot, sources in validated_by_slot.items():
            if not slot[0] or slot in explicit_slots:
                continue
            slot_contract = contract_by_slot.get(slot, {})
            has_family = slot[0] in families_by_filename.get(fn, set())
            if (
                not has_family
                and not (
                    slot[0] == "bottom_tab_bar"
                    and fn in bottom_shell_pages
                    and _allow_shell_family_receiver(slot, sources[0] if sources else {}, slot_contract)
                )
            ):
                continue
            source = slot_contract.get("source_control") if isinstance(slot_contract.get("source_control"), dict) else (sources[0] if sources else {})
            if not _allow_shell_family_receiver(slot, source, slot_contract):
                continue
            page_controls.append({
                "source_filename": fn,
                "control_label": _display_shell_control_label(source),
                "normalized_label": slot[1],
                "surface": slot[0],
                "shell_family_id": slot[0],
                "component_id": "",
                "target_filename": "",
                "source": "component_shell_family_receiver",
                "action_id": "",
                "evidence_steps": [],
                "confidence": min(float(source.get("confidence") or 0.9), 0.95),
                "interaction_kind": "",
                "state_effect": "",
                "sdk_effect": {},
            })
        existing_targets = {
            (
                str(control.get("shell_family_id") or ""),
                str(control.get("normalized_label") or ""),
            ): str(control.get("target_filename") or "")
            for control in page_controls
            if control.get("target_filename")
        }
        existing_in_page = {
            (
                str(control.get("shell_family_id") or ""),
                str(control.get("normalized_label") or ""),
            )
            for control in page_controls
            if control.get("interaction_kind") and not control.get("target_filename")
        }
        for control in page_controls:
            slot = (str(control.get("shell_family_id") or ""), str(control.get("normalized_label") or ""))
            control_target = str(control.get("target_filename") or "")
            if not slot[0] or not slot[1]:
                continue
            if control_target:
                if str(control.get("source") or "") == "shell_interaction_broadcast":
                    slot_contract = contract_by_slot.get(slot, {})
                    source = (
                        slot_contract.get("source_control")
                        if isinstance(slot_contract.get("source_control"), dict)
                        else {}
                    )
                    target = str(slot_contract.get("target_filename") or source.get("target_filename") or "")
                    if (
                        target
                        and target == control_target
                        and str(slot_contract.get("status") or "") == "complete"
                        and source.get("source_filename") != fn
                    ):
                        key = (fn, slot[0], slot[1], target)
                        if key not in retained_broadcast_route_keys:
                            retained_broadcast_route_keys.add(key)
                            evidence_steps = (
                                source.get("evidence_steps", [])
                                if isinstance(source.get("evidence_steps", []), list)
                                else []
                            )
                            display_label = _display_shell_control_label(control) or _display_shell_control_label(source)
                            shell_slot_key = slot[1]
                            component_id = _shell_route_component_id(fn, slot[0], shell_slot_key)
                            audit["propagated_routes"].append({
                                "source": "shell_interaction_broadcast",
                                "source_filename": fn,
                                "target_filename": target,
                                "control_label": display_label,
                                "normalized_label": shell_slot_key,
                                "surface": control.get("surface", ""),
                                "shell_family_id": control.get("shell_family_id", ""),
                                "shell_slot_key": shell_slot_key,
                                "component_id": component_id,
                                "required_data_element_id": component_id,
                                "slot_id": _slot_contract_id(slot),
                                "slot_contract_status": slot_contract.get("status", ""),
                                "source_authority": slot_contract.get("authority", ""),
                                "interaction_kind": source.get("interaction_kind") or "navigate",
                                "evidence_action_id": source.get("action_id") or ((evidence_steps or [""])[0]),
                                "evidence_source_filename": source.get("source_filename", ""),
                                "evidence_steps": evidence_steps,
                                "evidence": _shell_evidence_records(evidence_steps),
                                "confidence": min(float(source.get("confidence") or 0.9), 0.95),
                                "retained_existing_binding": True,
                            })
                            for affordance in page_env.get("route_affordances", []) if isinstance(page_env.get("route_affordances"), list) else []:
                                if not isinstance(affordance, dict):
                                    continue
                                if (
                                    str(affordance.get("source") or "") == "shell_interaction_broadcast"
                                    and _base_filename(affordance.get("target_filename")) == target
                                    and _normalize_shell_control_label(
                                        affordance.get("control_label") or affordance.get("label") or ""
                                    ) == slot[1]
                                ):
                                    affordance["evidence_steps"] = list(evidence_steps)
                                    affordance["shell_family_id"] = control.get("shell_family_id", "")
                                    break
                continue
            sources = validated_by_slot.get(slot, [])
            slot_contract = contract_by_slot.get(slot, {})
            contract_status = str(slot_contract.get("status") or "")
            if not sources or contract_status not in {"complete", "in_page_complete"}:
                audit["rejected_controls"].append({
                    "source_filename": fn,
                    "control_label": control.get("control_label", ""),
                    "surface": control.get("surface", ""),
                    "shell_family_id": control.get("shell_family_id", ""),
                    "slot_id": _slot_contract_id(slot),
                    "contract_status": contract_status or "missing",
                    "observed_targets": slot_contract.get("observed_targets", []),
                    "reason": "shell_slot_contract_not_complete",
                })
                continue
            source = slot_contract.get("source_control") if isinstance(slot_contract.get("source_control"), dict) else sources[0]
            target = str(slot_contract.get("target_filename") or source.get("target_filename") or "")
            if target and fn == target:
                continue
            if target and target != str(source.get("target_filename") or ""):
                audit["rejected_controls"].append({
                    "source_filename": fn,
                    "control_label": control.get("control_label", ""),
                    "surface": control.get("surface", ""),
                    "shell_family_id": control.get("shell_family_id", ""),
                    "target_filename": target,
                    "source_target_filename": source.get("target_filename", ""),
                    "slot_id": _slot_contract_id(slot),
                    "reason": "shell_slot_target_authority_mismatch",
                })
                continue
            if source.get("source_filename") == fn:
                continue
            conflict_target = existing_targets.get(slot)
            if target and conflict_target == target:
                continue
            if target and conflict_target and conflict_target != target:
                audit["rejected_controls"].append({
                    "source_filename": fn,
                    "control_label": control.get("control_label", ""),
                    "surface": control.get("surface", ""),
                    "shell_family_id": control.get("shell_family_id", ""),
                    "target_filename": target,
                    "existing_target_filename": conflict_target,
                    "reason": "conflicting_local_binding",
                })
                continue
            if not target and slot in existing_in_page and control.get("source") != "component_shell_family_receiver":
                continue
            interaction_kind = source.get("interaction_kind") or ("navigate" if target else "in_page")
            propagation_id = f"shell_propagated::{fn}::{slot[0]}::{slot[1]}::{target or interaction_kind}"
            confidence = min(float(source.get("confidence") or 0.9), 0.95)
            display_label = _display_shell_control_label(control) or _display_shell_control_label(source)
            shell_slot_key = slot[1]
            component_id = _shell_route_component_id(fn, slot[0], shell_slot_key)
            normalized_interaction_contract = (
                _normalize_in_page_interaction_contract(source)
                if not target and interaction_kind
                else {}
            )
            propagated = {
                "source": "shell_interaction_broadcast",
                "source_filename": fn,
                "target_filename": target,
                "control_label": display_label,
                "normalized_label": shell_slot_key,
                "surface": control.get("surface", ""),
                "shell_family_id": control.get("shell_family_id", ""),
                "shell_slot_key": shell_slot_key,
                "component_id": component_id,
                "required_data_element_id": component_id,
                "slot_id": _slot_contract_id(slot),
                "slot_contract_status": contract_status,
                "source_authority": slot_contract.get("authority", ""),
                "interaction_kind": interaction_kind,
                "evidence_action_id": source.get("action_id") or ((source.get("evidence_steps") or [""])[0]),
                "evidence_source_filename": source.get("source_filename", ""),
                "evidence_steps": source.get("evidence_steps", []),
                "evidence": _shell_evidence_records(source.get("evidence_steps", [])),
                "confidence": confidence,
            }
            if normalized_interaction_contract:
                propagated.update({
                    "contract_status": normalized_interaction_contract.get("contract_status", "incomplete"),
                    "missing_contract_fields": normalized_interaction_contract.get("missing_contract_fields", []),
                    "broadcast_generation_authority": normalized_interaction_contract.get("contract_status") == "complete",
                    "interaction_contract": normalized_interaction_contract.get("interaction_contract", {}),
                })
            if target:
                _add_unique_dict(
                    page_env.setdefault("route_affordances", []),
                    {
                        "target_filename": target,
                        "control_label": display_label,
                        "surface": control.get("surface", ""),
                        "confidence": confidence,
                        "source": "shell_interaction_broadcast",
                        "action_id": propagation_id,
                        "evidence_steps": source.get("evidence_steps", []),
                        "shell_family_id": control.get("shell_family_id", ""),
                    },
                    ("target_filename", "control_label"),
                )
            _add_unique_dict(
                page_env.setdefault("typed_actions", []),
                {
                    "action_id": propagation_id,
                    "label": display_label,
                    "behavior": interaction_kind,
                    "target_filename": target,
                    "surface": control.get("surface", ""),
                    "state_effect": (
                        "propagated shared shell navigation binding"
                        if target else source.get("state_effect") or "propagated shared shell in-page interaction"
                    ),
                    "source": "shell_interaction_broadcast",
                    **({
                        "state_surface_contract": normalized_interaction_contract.get("interaction_contract", {}).get("state_surface_contract", {}),
                        "contract_status": normalized_interaction_contract.get("contract_status", "incomplete"),
                    } if normalized_interaction_contract else {}),
                },
                ("action_id",),
            )
            sdk_effect = source.get("sdk_effect") if isinstance(source.get("sdk_effect"), dict) else {}
            _add_unique_dict(
                page_env.setdefault("interaction_contracts", []),
                _normalize_unified_interaction_contract({
                    "contract_id": propagation_id,
                    "label": display_label,
                    "action_semantic": interaction_kind,
                    "route_binding": {
                        **({"target_filename": target} if target else {}),
                        "source": "shell_interaction_broadcast",
                        "rationale": "same shell family and slot as validated source interaction",
                    },
                    "sdk_effect": {
                        **sdk_effect,
                        "effect": sdk_effect.get("effect") or interaction_kind,
                        "trigger": display_label,
                    },
                    "surface": control.get("surface", ""),
                    "source": "shell_interaction_broadcast",
                    **({
                        "state_surface_contract": normalized_interaction_contract.get("interaction_contract", {}).get("state_surface_contract", {}),
                        "contract_status": normalized_interaction_contract.get("contract_status", "incomplete"),
                        "broadcast_generation_authority": normalized_interaction_contract.get("contract_status") == "complete",
                    } if normalized_interaction_contract else {}),
                }),
                ("contract_id",),
            )
            if target:
                audit["propagated_routes"].append(propagated)
            else:
                audit["propagated_interactions"].append(propagated)

    audit["propagation_count"] = len(audit["propagated_routes"]) + len(audit["propagated_interactions"])
    if audit["propagation_count"] == 0 and not audit["rejected_controls"]:
        summary = audit.get("shell_navigation_contract", {}).get("summary", {})
        if isinstance(summary, dict) and not summary.get("slot_count"):
            audit["no_op_reason"] = "no_contract_complete_or_rejected_shell_slots"
        else:
            audit["no_op_reason"] = "complete_shell_slots_already_bound_or_self_target_only"
    page_environment_specs["shell_interaction_broadcast"] = audit
    return audit


def _run_phase_3_closure_default_review_only(
    page_environment_specs: Dict[str, Any],
    *,
    logger: Any | None = None,
) -> Dict[str, Any]:
    by_filename = (
        page_environment_specs.get("page_environment_specs_by_filename", {})
        if isinstance(page_environment_specs, dict)
        else {}
    )
    if not isinstance(by_filename, dict):
        by_filename = {}
    known_filenames = {_base_filename(filename) for filename in by_filename if _base_filename(filename)}
    domain_counts: Dict[str, int] = {}
    findings: List[Dict[str, Any]] = []

    def add_domain(domain: str) -> None:
        domain_counts[domain] = domain_counts.get(domain, 0) + 1

    def add_review(filename: str, domain: str, code: str, message: str) -> None:
        findings.append(
            {
                "phase": "phase3",
                "severity": "review",
                "logicflow": PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY,
                "filename": filename,
                "shard_id": f"phase3::{filename}::{domain}",
                "decision_domain": domain,
                "code": code,
                "message": message,
            }
        )

    for raw_filename, page_env in by_filename.items():
        if not isinstance(page_env, dict):
            continue
        filename = _base_filename(raw_filename) or str(raw_filename)

        route_affordances = page_env.get("route_affordances", [])
        if isinstance(route_affordances, list) and route_affordances:
            add_domain("route")
            for item in route_affordances:
                if not isinstance(item, dict):
                    continue
                target = _base_filename(item.get("target_filename"))
                if target and known_filenames and target not in known_filenames:
                    add_review(
                        filename,
                        "route",
                        "route_affordance_unknown_target",
                        f"route_affordance target_filename is not present in page_environment_specs: {target}",
                    )

        typed_actions = page_env.get("typed_actions", [])
        interaction_contracts = page_env.get("interaction_contracts", [])
        if (
            isinstance(typed_actions, list)
            and typed_actions
        ) or (
            isinstance(interaction_contracts, list)
            and interaction_contracts
        ):
            add_domain("interaction")
            for item in typed_actions if isinstance(typed_actions, list) else []:
                if isinstance(item, dict) and not item.get("action_id"):
                    add_review(filename, "interaction", "typed_action_missing_id", "typed_action is missing action_id")
            for item in interaction_contracts if isinstance(interaction_contracts, list) else []:
                if isinstance(item, dict) and not item.get("contract_id"):
                    add_review(filename, "interaction", "interaction_contract_missing_id", "interaction_contract is missing contract_id")

        conflict_resolutions = page_env.get("conflict_resolutions", [])
        if isinstance(conflict_resolutions, list) and conflict_resolutions:
            add_domain("conflict")
            for item in conflict_resolutions:
                if isinstance(item, dict) and not item.get("conflict_id"):
                    add_review(filename, "conflict", "conflict_resolution_missing_id", "conflict_resolution is missing conflict_id")

        query_bindings = page_env.get("query_identity_resolutions", [])
        if isinstance(query_bindings, list) and query_bindings:
            add_domain("query_binding")
            for item in query_bindings:
                if not isinstance(item, dict):
                    continue
                if not item.get("query_id"):
                    add_review(filename, "query_binding", "query_binding_missing_id", "query_identity_resolution is missing query_id")
                transition_kind = str(item.get("query_transition_kind") or "")
                target_status = str(item.get("target_identity_status") or "")
                if transition_kind in {"query_entry", "suggestion_projection"} and target_status == "mismatch":
                    add_review(
                        filename,
                        "query_binding",
                        "query_entry_or_projection_marked_mismatch",
                        "query entry/projection binding is marked as target mismatch",
                    )

    shell_broadcast = apply_shell_interaction_broadcast(page_environment_specs)
    shard_count = sum(domain_counts.values())
    _planner_log_info(
        logger,
        "[PageEnv UnifiedPlanner] phase=3 closure_default_review_only "
        f"pages={len(by_filename)} shards={shard_count} review_shards={len({item.get('shard_id') for item in findings})}",
    )
    return {
        "enabled": True,
        "logicflow": PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY,
        "api_or_det": "det",
        "page_count": len(by_filename),
        "resolved_count": len(by_filename),
        "shard_count": shard_count,
        "domain_counts": dict(sorted(domain_counts.items())),
        "review_shard_count": len({str(item.get("shard_id") or "") for item in findings if item.get("shard_id")}),
        "blocking_shard_count": 0,
        "llm_review_shard_count": 0,
        "findings": findings[:64],
        "errors": [],
        "usage_records": [],
        "shell_interaction_broadcast": {
            "eligible_component_count": shell_broadcast.get("eligible_component_count", 0) if isinstance(shell_broadcast, dict) else 0,
            "binding_count": shell_broadcast.get("binding_count", 0) if isinstance(shell_broadcast, dict) else 0,
        },
        "source_stages_replaced": [],
    }


async def _run_phase_3_cross_page_decisions_async(
    site_model,
    result_dir: Path,
    page_environment_specs: Dict[str, Any],
    *,
    page_behaviors: Dict[str, Any] | None = None,
    content_data_by_page: Dict[str, Dict[str, Any]] | None = None,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_concurrent: int = 8,
    max_tokens: int = 12000,
    logger: Any | None = None,
) -> Dict[str, Any]:
    by_filename = (
        page_environment_specs.get("page_environment_specs_by_filename", {})
        if isinstance(page_environment_specs, dict)
        else {}
    )
    if not isinstance(by_filename, dict) or not by_filename:
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=3 start pages=0 max_concurrent="
            f"{max(1, int(max_concurrent or 1))} model={model or ''} "
            f"reasoning_effort={reasoning_effort} max_tokens={max_tokens}",
        )
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=3 complete pages=0 resolved=0 errors=0 duration=0.00s usage_records=0",
        )
        return {
            "enabled": True,
            "page_count": 0,
            "resolved_count": 0,
            "errors": [],
            "usage_records": [],
            "source_stages_replaced": [
                "task_interaction_contract_planner",
                "page_environment_conflict_resolver",
                "page_environment_query_identity_enricher",
                "alternative_route_access_planner",
            ],
        }

    page_behaviors = page_behaviors or {}
    content_data_by_page = content_data_by_page or {}

    interaction_contracts_by_filename = _interaction_contracts_by_filename(site_model)
    groups_payload = build_page_environment_conflict_groups(site_model, by_filename, result_dir=result_dir)
    groups_by_filename = groups_payload.get("groups_by_filename", {}) if isinstance(groups_payload, dict) else {}
    query_tasks_all = _collect_query_identity_tasks(page_environment_specs, site_model)
    query_tasks_by_filename: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for task in query_tasks_all:
        if not isinstance(task, dict):
            continue
        source = _base_filename(task.get("source_filename"))
        target = _base_filename(task.get("target_filename"))
        if source:
            query_tasks_by_filename[source].append(task)
        if target and target != source:
            query_tasks_by_filename[target].append(task)

    pages_by_id, pages_by_filename_meta, _pages_by_general = _page_signature_maps(site_model)
    from trace2world_pipeline.app_spec.alternative_route_access import (
        collect_candidate_targets,
        collect_visible_route_controls,
    )

    page_payloads: List[Dict[str, Any]] = []
    conflict_groups_by_payload_filename: Dict[str, List[Dict[str, Any]]] = {}
    for filename, page_env in by_filename.items():
        if not isinstance(page_env, dict):
            continue
        fn = _base_filename(filename)
        if not fn:
            continue
        content_data = content_data_by_page.get(fn, {})
        if not content_data:
            content_data = _page_content_context(site_model, pages_by_filename_meta.get(fn, {}))
        interaction_contracts = interaction_contracts_by_filename.get(fn, [])
        conflict_groups = groups_by_filename.get(fn, []) if isinstance(groups_by_filename, dict) else []
        query_tasks = query_tasks_by_filename.get(fn, [])

        page_meta = pages_by_filename_meta.get(fn, {})
        page_id = str(page_meta.get("page_id") or "")
        route_controls: List[Dict[str, Any]] = []
        candidate_targets: List[Dict[str, Any]] = []
        if page_id:
            structure_data = page_meta.get("structure_data", {}) if isinstance(page_meta, dict) else {}

            class _PageProxy:
                pass

            proxy = _PageProxy()
            proxy.page_id = page_id
            proxy.page_type = str(page_meta.get("page_type") or "")
            proxy.page_name = str(page_meta.get("page_name") or "")
            proxy.page_name_full = str(page_meta.get("page_name_full") or "")
            proxy.filename = fn
            proxy.description = str(page_meta.get("description") or "")
            proxy.structure_data = structure_data if isinstance(structure_data, dict) else {}
            proxy.content_data = {}
            for proto in site_model.structure.page_prototypes:
                if proto.page_id == page_id:
                    proxy.content_data = proto.content_data if isinstance(proto.content_data, dict) else {}
                    break
            route_controls = collect_visible_route_controls(proxy, limit=16)
            if route_controls:
                candidate_targets = collect_candidate_targets(
                    proxy,
                    route_controls,
                    list(getattr(getattr(site_model, "structure", None), "page_prototypes", []) or []),
                    max_candidates=64,
                )

        payload = _build_environment_action_route_page_payload(
            fn,
            page_env,
            page_behaviors,
            content_data,
            interaction_contracts,
            conflict_groups if isinstance(conflict_groups, list) else [],
            query_tasks,
            route_controls,
            candidate_targets,
        )
        conflict_groups_by_payload_filename[fn] = conflict_groups if isinstance(conflict_groups, list) else []
        page_payloads.append(payload)

    metadata: Dict[str, Any] = {
        "enabled": True,
        "page_count": len(page_payloads),
        "resolved_count": 0,
        "errors": [],
        "usage_records": [],
        "source_stages_replaced": [
            "task_interaction_contract_planner",
            "page_environment_conflict_resolver",
            "page_environment_query_identity_enricher",
            "alternative_route_access_planner",
        ],
    }

    phase_started_at = time.perf_counter()
    effective_max_concurrent = max(1, int(max_concurrent or 1))
    _planner_log_info(
        logger,
        "[PageEnv UnifiedPlanner] phase=3 start "
        f"pages={len(page_payloads)} max_concurrent={effective_max_concurrent} "
        f"model={model or ''} reasoning_effort={reasoning_effort} max_tokens={max_tokens}",
    )

    if not page_payloads:
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=3 complete pages=0 resolved=0 errors=0 duration=0.00s usage_records=0",
        )
        return metadata

    phase_3_input_signature = _stable_json_signature(
        {
            "phase": "page_environment.environment_action_route_plan",
            "schema_revision": 1,
            "page_payloads": page_payloads,
            "model": model or "",
            "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens,
        }
    )
    usage_records: List[Any] = []
    errors: List[str] = []
    results_by_filename: Dict[str, Dict[str, Any]] = {}
    phase_3_cache = _read_unified_planner_phase_cache(
        result_dir,
        "environment_action_route_page_plans.json",
        expected_format="page_environment_unified_planner_phase3_page_plan_cache",
        input_signature=phase_3_input_signature,
    )
    cached_page_plans = (
        phase_3_cache.get("page_plans_by_filename", {})
        if isinstance(phase_3_cache, dict) and isinstance(phase_3_cache.get("page_plans_by_filename"), dict)
        else {}
    )

    async def run_page(index: int, payload: Dict[str, Any]) -> None:
        fn_for_log = _base_filename(payload.get("filename")) or "?"
        try:
            payload_chars = len(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            payload_chars = -1
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=3 page_start "
            f"index={index}/{len(page_payloads)} filename={fn_for_log} payload_chars={payload_chars}",
        )
        page_started_at = time.perf_counter()
        try:
            result = await _resolve_environment_action_route_page(
                payload,
                model=model,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            errors.append(f"{payload.get('filename', '?')}: {exc}")
            _planner_log_warning(
                logger,
                "[PageEnv UnifiedPlanner] phase=3 page_failed "
                f"index={index}/{len(page_payloads)} filename={fn_for_log} "
                f"duration={time.perf_counter() - page_started_at:.2f}s "
                f"error={exc}",
            )
            return
        fn = _base_filename(payload.get("filename"))
        usage = result.get("_usage")
        total_tokens = _usage_total_tokens(usage) if usage else "unknown"
        if usage:
            usage_records.append(result.pop("_usage", None))
        if fn:
            results_by_filename[fn] = result
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=3 page_done "
            f"index={index}/{len(page_payloads)} filename={fn_for_log} "
            f"duration={time.perf_counter() - page_started_at:.2f}s "
            f"total_tokens={total_tokens} "
            f"typed_actions={len(result.get('typed_actions', []) if isinstance(result.get('typed_actions'), list) else [])} "
            f"interaction_contracts={len(result.get('interaction_contracts', []) if isinstance(result.get('interaction_contracts'), list) else [])} "
            f"routes={len(result.get('route_affordances', []) if isinstance(result.get('route_affordances'), list) else [])} "
            f"conflicts={len(result.get('conflict_resolutions', []) if isinstance(result.get('conflict_resolutions'), list) else [])} "
            f"query_bindings={len(result.get('query_identity_resolutions', []) if isinstance(result.get('query_identity_resolutions'), list) else [])}",
        )

    if len(cached_page_plans) >= len(page_payloads):
        results_by_filename = {
            _base_filename(filename): dict(plan)
            for filename, plan in cached_page_plans.items()
            if _base_filename(filename) and isinstance(plan, dict)
        }
        _planner_log_info(
            logger,
            "[PageEnv UnifiedPlanner] phase=3 cache_hit "
            f"pages={len(page_payloads)} resolved={len(results_by_filename)}",
        )
    else:
        if phase_3_cache is not None:
            _planner_log_warning(
                logger,
                "[PageEnv UnifiedPlanner] phase=3 cache_miss "
                f"reason=incomplete_cached_page_plans pages={len(page_payloads)} cached={len(cached_page_plans)}",
            )
        else:
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=3 cache_miss "
                f"reason=missing_or_signature_mismatch pages={len(page_payloads)}",
            )
        await asyncio.gather(*(run_page(index, payload) for index, payload in enumerate(page_payloads, start=1)))
        if not errors:
            cache_path = _write_unified_planner_phase_cache(
                result_dir,
                "environment_action_route_page_plans.json",
                {
                    "format": "page_environment_unified_planner_phase3_page_plan_cache",
                    "schema_revision": 1,
                    "input_signature": phase_3_input_signature,
                    "page_count": len(page_payloads),
                    "resolved_count": len(results_by_filename),
                    "page_plans_by_filename": results_by_filename,
                },
            )
            _planner_log_info(
                logger,
                "[PageEnv UnifiedPlanner] phase=3 cache_write "
                f"path={cache_path.relative_to(Path(result_dir))}",
            )

    applied_conflict_resolutions: List[Dict[str, Any]] = []
    applied_interaction_contracts: List[Dict[str, Any]] = []
    for filename, page_env in by_filename.items():
        if not isinstance(page_env, dict):
            continue
        fn = _base_filename(filename)
        plan = results_by_filename.get(fn)
        if plan:
            _apply_environment_action_route_plan_to_page(
                page_env,
                plan,
                conflict_groups=conflict_groups_by_payload_filename.get(fn, []),
                fallback_interaction_contracts=interaction_contracts_by_filename.get(fn, []),
            )
            for resolution in page_env.get("conflict_resolutions", []) if isinstance(page_env.get("conflict_resolutions"), list) else []:
                if isinstance(resolution, dict):
                    applied_conflict_resolutions.append(resolution)
            for contract in page_env.get("interaction_contracts", []) if isinstance(page_env.get("interaction_contracts"), list) else []:
                if isinstance(contract, dict):
                    applied_interaction_contracts.append(dict(contract, source_filename=fn))

    flat_groups = [
        group
        for groups in groups_by_filename.values()
        for group in (groups if isinstance(groups, list) else [])
        if isinstance(group, dict)
    ] if isinstance(groups_by_filename, dict) else []
    if flat_groups:
        page_environment_specs["conflict_resolution"] = {
            "format": "page_environment_conflict_resolution",
            "schema_revision": 1,
            "resolver": "unified_environment_action_route_planner_with_deterministic_validation",
            "group_count": len(flat_groups),
            "resolution_count": len(applied_conflict_resolutions),
            "fallback_count": sum(1 for item in applied_conflict_resolutions if item.get("resolver") == "deterministic_fallback"),
            "groups": groups_payload,
            "resolutions": applied_conflict_resolutions,
        }
    if applied_interaction_contracts:
        page_environment_specs["task_interaction_contracts"] = {
            "format": "task_interaction_contracts",
            "schema_revision": 1,
            "producer": "unified_environment_action_route_planner",
            "contracts": applied_interaction_contracts,
        }
    shell_broadcast = apply_shell_interaction_broadcast(page_environment_specs)

    metadata["resolved_count"] = len(results_by_filename)
    metadata["errors"] = errors[:16]
    metadata["usage_records"] = usage_records[:16]
    metadata["model"] = model or ""
    metadata["reasoning_effort"] = reasoning_effort
    metadata["shell_interaction_broadcast"] = {
        "propagation_count": shell_broadcast.get("propagation_count", 0),
        "rejected_count": len(shell_broadcast.get("rejected_controls", [])),
    }
    _planner_log_info(
        logger,
        "[PageEnv UnifiedPlanner] phase=3 complete "
        f"pages={metadata['page_count']} resolved={metadata['resolved_count']} "
        f"errors={len(errors)} duration={time.perf_counter() - phase_started_at:.2f}s "
        f"usage_records={len(usage_records)}",
    )
    return metadata


def apply_page_environment_conflict_resolutions(
    page_environment_specs: Dict[str, Any],
    groups_payload: Dict[str, Any],
    resolutions_by_id: Dict[str, Dict[str, Any]],
) -> None:
    by_filename = page_environment_specs.get("page_environment_specs_by_filename", {}) if isinstance(page_environment_specs, dict) else {}
    if not isinstance(by_filename, dict):
        return
    groups_by_filename = groups_payload.get("groups_by_filename", {}) if isinstance(groups_payload, dict) else {}
    for filename, page_env in by_filename.items():
        if not isinstance(page_env, dict):
            continue
        groups = groups_by_filename.get(filename, []) if isinstance(groups_by_filename, dict) else []
        resolutions: List[Dict[str, Any]] = []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            conflict_id = str(group.get("conflict_id") or "")
            resolution = dict(resolutions_by_id.get(conflict_id) or _fallback_resolution(group))
            resolution["candidate_summary"] = _resolution_candidate_summary(group)
            resolutions.append(resolution)
        page_env["conflict_resolutions"] = resolutions
        page_env["conflict_resolution_summary"] = {
            "group_count": len(groups) if isinstance(groups, list) else 0,
            "resolution_count": len(resolutions),
            "fallback_count": sum(1 for item in resolutions if item.get("resolver") == "deterministic_fallback"),
            "materialized_scope_count": sum(1 for item in resolutions if item.get("conflict_type") == "materialized_page_sibling_scope"),
        }
        policy = page_env.setdefault("layout_content_conflict_policy", {})
        policy["resolved_by"] = "llm_semantic_conflict_resolver_with_deterministic_validation"
        policy["pagegen_input"] = "resolved_page_environment_spec_only"
        policy["unresolved_conflicts"] = "fallback_to_contract_safe_deterministic_decision"


def build_materialized_page_environment_specs(trace_specs: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    pages_by_filename: Dict[str, Dict[str, Any]] = {}

    def env_for(filename: str) -> Dict[str, Any]:
        filename = _base_filename(filename)
        if not filename:
            filename = "unknown.html"
        return pages_by_filename.setdefault(filename, _new_page_environment(filename))

    for spec in trace_specs:
        if not isinstance(spec, dict):
            continue
        trace_id = str(spec.get("trace_id") or "")
        for page in spec.get("pages", []) if isinstance(spec.get("pages"), list) else []:
            if not isinstance(page, dict):
                continue
            filename = _base_filename(page.get("filename"))
            if not filename:
                continue
            page_env = env_for(filename)
            page_env["provenance"]["source_trace_ids"] = _dedupe(
                list(page_env["provenance"].get("source_trace_ids", [])) + ([trace_id] if trace_id else []),
                limit=64,
            )
            page_env["provenance"]["evidence_steps"] = _dedupe(
                list(page_env["provenance"].get("evidence_steps", [])) + list(page.get("evidence_steps", []) or []),
                limit=128,
            )
            for obligation in page.get("render_obligations", []) if isinstance(page.get("render_obligations"), list) else []:
                if not isinstance(obligation, dict):
                    continue
                step_key = str(obligation.get("step_key") or "")
                _add_unique_dict(
                    page_env["oracles"],
                    {
                        "oracle_type": "navigation_or_state",
                        "step_key": step_key,
                        "operation": str(obligation.get("operation") or ""),
                        "expected_filename": _base_filename(obligation.get("expected_filename")),
                        "input_text": str(obligation.get("input_text") or ""),
                    },
                    ("oracle_type", "step_key", "expected_filename", "operation"),
                )

        for fact in spec.get("collection_facts", []) if isinstance(spec.get("collection_facts"), list) else []:
            if not isinstance(fact, dict):
                continue
            filename = _base_filename(fact.get("filename"))
            scope = str(fact.get("state_scope") or "").strip()
            if not filename or not scope:
                continue
            page_env = env_for(filename)
            state_ref = _state_ref(scope)
            initial_collection = {
                "state_scope": scope,
                "min_initial_count": int(fact.get("min_initial_count") or 0),
                "evidence_steps": _dedupe(fact.get("evidence_steps", []) or [], limit=24),
                "seed_policy": "observed_initial_state_only",
            }
            if fact.get("preserve_unknown_initial_records"):
                initial_collection["preserve_unknown_initial_records"] = True
                initial_collection["seed_policy"] = "observed_initial_state_preserve_unknown"
            page_env["initial_state"]["collections"][state_ref] = initial_collection
            _ensure_collection_schema(page_env, scope)
            content_collection = {
                "state_ref": state_ref,
                "state_scope": scope,
                "min_initial_count": int(fact.get("min_initial_count") or 0),
                "evidence_steps": _dedupe(fact.get("evidence_steps", []) or [], limit=24),
            }
            if fact.get("preserve_unknown_initial_records"):
                content_collection["preserve_unknown_initial_records"] = True
            _add_unique_dict(
                page_env["content_seed"]["collections"],
                content_collection,
                ("state_ref",),
            )
            _add_unique_dict(
                page_env["render_projections"],
                {
                    "projection_id": f"initial::{state_ref}",
                    "render_phase": "initial",
                    "component_ref": scope,
                    "reads_state": state_ref,
                    "layout_policy": "collection_template_from_observed_component",
                    "mutation_policy": "preserve_existing_then_append_or_upsert",
                    "evidence_steps": _dedupe(fact.get("evidence_steps", []) or [], limit=24),
                },
                ("projection_id",),
            )

        for snapshot in spec.get("state_snapshots", []) if isinstance(spec.get("state_snapshots"), list) else []:
            if not isinstance(snapshot, dict):
                continue
            filename = _base_filename(snapshot.get("filename"))
            if not filename:
                continue
            page_env = env_for(filename)
            for state_item in snapshot.get("state_items", []) if isinstance(snapshot.get("state_items"), list) else []:
                if not isinstance(state_item, dict):
                    continue
                scope = str(state_item.get("state_scope") or "").strip()
                lifecycle = str(state_item.get("lifecycle") or "")
                if not scope:
                    continue
                if _is_collection_scope(scope, filename):
                    _ensure_collection_schema(page_env, scope)
                if lifecycle in {"initial_state", "screen_loaded"}:
                    state_ref = _state_ref(scope)
                    page_env["initial_state"]["collections"].setdefault(state_ref, {
                        "state_scope": scope,
                        "min_initial_count": int(state_item.get("min_observed_count") or 0),
                        "evidence_steps": _dedupe(state_item.get("evidence_steps", []) or [], limit=24),
                        "seed_policy": "observed_initial_state_only",
                    })

        for transition in spec.get("transitions", []) if isinstance(spec.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            operation = str(transition.get("operation") or "")
            effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            source = _base_filename(transition.get("source_filename"))
            target = _base_filename(transition.get("target_filename"))
            reducer = _transition_reducer(operation, effect)
            transition_projection = {
                "transition_id": str(transition.get("step_key") or ""),
                "step_key": str(transition.get("step_key") or ""),
                "source_filename": source,
                "target_filename": target,
                "operation": operation,
                "command": str(transition.get("command") or ""),
                "operation_intent": str(transition.get("operation_intent") or ""),
                "reducer": reducer,
                "state_effect": effect,
                "l0_role": str(transition.get("l0_role") or ""),
                "input_text": str(transition.get("input_text") or ""),
            }
            transition_bindings = [(source, "source")]
            if target and target != source:
                transition_bindings.append((target, "target"))
            for filename, direction in transition_bindings:
                if not filename:
                    continue
                page_env = env_for(filename)
                item = dict(transition_projection, direction=direction)
                _add_unique_dict(page_env["transitions"], item, ("transition_id", "direction"))
                if direction == "source":
                    if reducer in {"fill_form", "type_search"}:
                        _add_unique_dict(
                            page_env["oracles"],
                            {
                                "oracle_type": "typed_input_parameterized",
                                "step_key": item["step_key"],
                                "field_role": str(effect.get("field_role") or ""),
                                "visible_initial_prefill_allowed": False,
                            },
                            ("oracle_type", "step_key", "field_role"),
                        )
                    elif target and target != source:
                        _add_unique_dict(
                            page_env["oracles"],
                            {
                                "oracle_type": "navigation",
                                "step_key": item["step_key"],
                                "expected_filename": target,
                                "operation": operation,
                            },
                            ("oracle_type", "step_key", "expected_filename"),
                        )

        for session in spec.get("input_sessions", []) if isinstance(spec.get("input_sessions"), list) else []:
            if not isinstance(session, dict):
                continue
            source = _base_filename(session.get("source_filename"))
            target = _base_filename(session.get("target_filename"))
            fields = session.get("fields", []) if isinstance(session.get("fields"), list) else []
            for filename in _dedupe([source, target], limit=2):
                if not filename:
                    continue
                page_env = env_for(filename)
                draft_ref = str(session.get("draft_ref") or "")
                if draft_ref:
                    page_env["initial_state"]["drafts"].setdefault(draft_ref, {"fields": {}, "status": "empty"})
                    _add_unique_dict(
                        page_env["state_schema"]["drafts"],
                        {
                            "draft_ref": draft_ref,
                            "source_filename": source,
                            "target_filename": target,
                            "fields": [
                                {
                                    "field_role": str(field.get("field_role") or ""),
                                    "schema": {"type": "string"},
                                }
                                for field in fields
                                if isinstance(field, dict)
                            ],
                        },
                        ("draft_ref",),
                    )
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    role = str(field.get("field_role") or "form_value")
                    value = str(field.get("value") or "")
                    parameter = {
                        "parameter_id": role,
                        "field_role": role,
                        "schema": {"type": "string"},
                        "observed_values": [value] if value else [],
                        "binds_to": [draft_ref] if draft_ref else [],
                        "source_filename": source,
                        "target_filename": target,
                        "visible_initial_prefill_allowed": False,
                    }
                    _add_unique_dict(page_env["parameterized_inputs"], parameter, ("parameter_id", "source_filename", "target_filename"))
                    _add_unique_dict(page_env["state_schema"]["parameters"], {
                        "parameter_id": role,
                        "schema": {"type": "string"},
                    }, ("parameter_id",))
                if session.get("committed_by_step"):
                    _add_unique_dict(
                        page_env["oracles"],
                        {
                            "oracle_type": "state_mutation",
                            "step_key": str(session.get("committed_by_step") or ""),
                            "draft_ref": draft_ref,
                            "target_filename": target,
                            "mutation_policy": "preserve_existing_then_append_or_upsert",
                        },
                        ("oracle_type", "step_key", "draft_ref"),
                    )

    for page_env in pages_by_filename.values():
        page_env["provenance"]["source_trace_ids"] = _dedupe(page_env["provenance"].get("source_trace_ids", []), limit=64)
        page_env["provenance"]["evidence_steps"] = _dedupe(page_env["provenance"].get("evidence_steps", []), limit=128)
    return pages_by_filename


def _route_navigation_affordances(
    page_contract: Dict[str, Any],
    target_contracts_by_filename: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    target_contracts_by_filename = target_contracts_by_filename or {}
    route_contract = page_contract.get("route_execution_contract", {}) if isinstance(page_contract, dict) else {}
    actions: List[Dict[str, Any]] = []
    if isinstance(route_contract, dict):
        for key in ("navigation_actions", "actions"):
            values = route_contract.get(key, [])
            if isinstance(values, list):
                actions.extend(item for item in values if isinstance(item, dict))
    dynamic_contract = page_contract.get("dynamic_surface_contract", {}) if isinstance(page_contract, dict) else {}
    if isinstance(dynamic_contract, dict):
        for item in dynamic_contract.get("dynamic_transitions", []) if isinstance(dynamic_contract.get("dynamic_transitions"), list) else []:
            if not isinstance(item, dict):
                continue
            target = _base_filename(item.get("target_filename"))
            trigger = str(item.get("trigger_text") or "").strip()
            if target and trigger:
                actions.append({
                    "action_id": str(item.get("contract_id") or item.get("source_component") or ""),
                    "target_filename": target,
                    "target_source": "dynamic_surface_contract",
                    "trigger_text": trigger,
                    "surface": str(item.get("source_component") or "dynamic_surface"),
                    "binding": {
                        "binding_kind": str(item.get("event") or ""),
                        "labels": [trigger],
                        "surface": "dynamic_surface",
                        "component_ref": str(item.get("source_component") or ""),
                    },
                })
    result: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    for action in actions:
        target = _base_filename(action.get("target_filename"))
        if not target:
            continue
        target_contract = target_contracts_by_filename.get(target, {})
        action_id = str(action.get("action_id") or "")
        trigger_text = str(action.get("trigger_text") or "")
        if action_id == "default_back" or _contains_any(trigger_text, ("page_navigation", "current_page", "返回上一页")):
            continue
        binding = action.get("binding", {}) if isinstance(action.get("binding"), dict) else {}
        is_shell_affordance = (
            str(action.get("target_source") or "").strip() == "shell_component_affordance"
            or str(action.get("target_source") or "").strip() == "observed_navigation_affordance"
            or str(action.get("affordance_kind") or "").strip() == "shell_component_navigation_affordance"
            or str(binding.get("source_scope") or "").strip()
            in {"alternative_route_access_plan", "shell_family_component"}
        )
        if not is_shell_affordance:
            continue
        labels = binding.get("labels", []) if isinstance(binding.get("labels"), list) else []
        clean_labels = _dedupe(
            (
                _quoted_or_clicked_label(label) or str(label or "").strip()
                for label in list(labels) + [trigger_text]
                if str(label or "").strip()
            ),
            limit=8,
        )
        binding_kind = str(binding.get("binding_kind") or "").strip()
        if not clean_labels:
            continue
        surface = str(binding.get("surface") or action.get("surface") or "shell_navigation")
        route_binding_semantics = action.get("route_binding_semantics", {}) if isinstance(action.get("route_binding_semantics"), dict) else {}
        search_control_label = any(
            _contains_any(label, ("搜索", "search", "输入框", "查找框", "放大镜"))
            for label in clean_labels
        )
        target_looks_like_search_surface = (
            "search" in target.lower()
            or "搜索" in str(target_contract.get("page_name") or "")
            or "search" in str(target_contract.get("general_page_id") or "").lower()
            or _contract_is_search_entry(target_contract)
        )
        if (
            not route_binding_semantics
            and binding_kind == "search_control"
            and search_control_label
            and target_looks_like_search_surface
        ):
            route_binding_semantics = {
                "binding_kind": "activation_control",
                "parameter_binding": {},
                "source": "page_execution_contract.route_execution_contract",
            }
        route_binding_kind = str(route_binding_semantics.get("binding_kind") or "").strip()
        if binding_kind == "search_control" and route_binding_kind == "activation_control":
            resolved_target, resolved_contract, resolution = _resolve_search_activation_target_contract(
                target,
                target_contract,
                target_contracts_by_filename,
            )
            route_binding_semantics = dict(route_binding_semantics)
            route_binding_semantics.update(resolution)
            target = resolved_target
            target_contract = resolved_contract
        if (
            (binding_kind == "search_control" or surface == "search_surface")
            and route_binding_kind not in {"route_parameter", "activation_control"}
        ):
            control_labels = [
                label
                for label in clean_labels
                if _contains_any(label, ("搜索", "search", "输入框", "查找框"))
            ]
            clean_labels = control_labels or ["搜索框"]
        for label in clean_labels:
            key = (label, target, surface)
            if key in seen:
                continue
            seen.add(key)
            evidence_steps = action.get("evidence_steps", [])
            if not isinstance(evidence_steps, list):
                evidence_steps = [str(evidence_steps)]
            result.append({
                "label": label,
                "display_labels": clean_labels,
                "target_filename": target,
                "route_binding_semantics": route_binding_semantics,
                "source_page_id": str(page_contract.get("page_id") or action.get("source_page_id") or ""),
                "source_general_page_id": str(page_contract.get("general_page_id") or ""),
                "source_materialized_page_id": str(page_contract.get("materialized_page_id") or ""),
                "target_page_id": str(action.get("target_page_id") or target_contract.get("page_id") or ""),
                "target_page_name": str(action.get("target_page_name") or target_contract.get("page_name") or ""),
                "target_general_page_id": str(target_contract.get("general_page_id") or ""),
                "target_materialized_page_id": str(target_contract.get("materialized_page_id") or ""),
                "surface": surface,
                "binding_kind": binding_kind,
                "component_ref": str(binding.get("component_ref") or ""),
                "action_id": str(action.get("action_id") or ""),
                "evidence_steps": _dedupe([str(step) for step in evidence_steps if str(step or "").strip()], limit=32),
                "source": "page_execution_contract.route_execution_contract",
            })
    return result


def _component_route_trigger_join_key(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = re.sub(
        r"^\s*(点击|点按|轻触|单击|选择|打开|进入|切换到|tap|click|select|open|press)\s*",
        "",
        raw,
        flags=re.I,
    )
    return "".join(ch for ch in raw.lower() if ch.isalnum())


def _component_route_action_join_keys(action: Dict[str, Any]) -> List[str]:
    values: List[Any] = [action.get("trigger_text")]
    binding = action.get("binding") if isinstance(action.get("binding"), dict) else {}
    labels = binding.get("labels") if isinstance(binding.get("labels"), list) else []
    values.extend(labels)
    return _dedupe(
        key
        for value in values
        for key in (_component_route_trigger_join_key(value),)
        if key
    )


def _l0_route_actions_by_source_filename(
    l0_execution_pack: Dict[str, Any] | None,
    page_contracts_by_filename: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    if not isinstance(l0_execution_pack, dict):
        return {}
    target_page_ids_by_filename = {
        _base_filename(filename): str(contract.get("page_id") or "")
        for filename, contract in page_contracts_by_filename.items()
        if _base_filename(filename) and isinstance(contract, dict)
    }
    result: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    packs = l0_execution_pack.get("action_packs", [])
    if not isinstance(packs, list):
        return {}
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        trace_id = str(pack.get("trace_id") or "")
        steps = pack.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            source = _base_filename((step.get("pre_state") or {}).get("filename"))
            target = _base_filename((step.get("expected_post_state") or {}).get("filename"))
            if not source or not target or source == target:
                continue
            action = step.get("action") if isinstance(step.get("action"), dict) else {}
            trigger_text = str(action.get("intent") or action.get("action_text") or "").strip()
            if not trigger_text:
                continue
            step_index = step.get("step_index")
            evidence_step = f"{trace_id}:{step_index}" if trace_id and step_index is not None else ""
            result[source].append({
                "action_id": f"l0_route::{evidence_step}" if evidence_step else "l0_route",
                "target_filename": target,
                "target_page_id": target_page_ids_by_filename.get(target, ""),
                "trigger_text": trigger_text,
                "evidence_steps": [evidence_step] if evidence_step else [],
                "target_source": "l0_execution_pack",
            })
    return result


def _component_route_source_component_key(transition: Dict[str, Any], page_contract: Dict[str, Any]) -> str:
    source_component = str(transition.get("source_component") or "").strip()
    contract_id = str(transition.get("contract_id") or "").strip()
    if not contract_id:
        return source_component
    if "::" in contract_id:
        return contract_id
    source_page_id = str(page_contract.get("page_id") or "").strip()
    if not source_page_id and "::" in source_component:
        source_page_id = source_component.split("::", 1)[0]
    if source_page_id:
        return f"{source_page_id}::{contract_id}"
    return source_component


def build_component_route_outedge_conflicts(
    page_execution_contracts: Dict[str, Any],
    l0_execution_pack: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    by_filename = _page_execution_contracts_payload_by_filename(page_execution_contracts)
    l0_actions_by_filename = _l0_route_actions_by_source_filename(l0_execution_pack, by_filename)
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    unresolved: List[Dict[str, Any]] = []
    navigation_transition_count = 0
    matched_route_count = 0
    l0_route_action_count = sum(len(actions) for actions in l0_actions_by_filename.values())

    for filename, page_contract in by_filename.items():
        route_contract = (
            page_contract.get("route_execution_contract")
            if isinstance(page_contract.get("route_execution_contract"), dict)
            else {}
        )
        actions: List[Dict[str, Any]] = []
        for key in ("navigation_actions", "actions"):
            values = route_contract.get(key, []) if isinstance(route_contract, dict) else []
            if isinstance(values, list):
                actions.extend(item for item in values if isinstance(item, dict))
        actions.extend(l0_actions_by_filename.get(filename, []))
        actions_by_join_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        seen_actions: set[Tuple[str, str, str, str]] = set()
        for action in actions:
            target_filename = _base_filename(action.get("target_filename"))
            target_page_id = str(action.get("target_page_id") or "").strip()
            if not target_filename and not target_page_id:
                continue
            trigger_text = str(action.get("trigger_text") or "").strip()
            action_key = (target_filename, target_page_id, trigger_text, str(action.get("target_source") or ""))
            if action_key in seen_actions:
                continue
            seen_actions.add(action_key)
            for join_key in _component_route_action_join_keys(action):
                actions_by_join_key[join_key].append(action)

        dynamic_contract = (
            page_contract.get("dynamic_surface_contract")
            if isinstance(page_contract.get("dynamic_surface_contract"), dict)
            else {}
        )
        transitions = dynamic_contract.get("dynamic_transitions", []) if isinstance(dynamic_contract, dict) else []
        if not isinstance(transitions, list):
            continue
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            event = str(transition.get("event") or "").strip()
            if event != "navigate_shell":
                continue
            source_component = _component_route_source_component_key(transition, page_contract)
            trigger_text = str(transition.get("trigger_text") or "").strip()
            join_key = _component_route_trigger_join_key(trigger_text)
            if not source_component or not join_key:
                continue
            navigation_transition_count += 1
            matching_actions = actions_by_join_key.get(join_key, [])
            if not matching_actions:
                unresolved.append({
                    "source_filename": filename,
                    "source_page_id": str(page_contract.get("page_id") or source_component.split("::", 1)[0] or ""),
                    "source_component": source_component,
                    "trigger_text": trigger_text,
                    "event": event,
                    "sdk_method": str(transition.get("sdk_method") or ""),
                    "reason": "no_route_execution_action_for_dynamic_transition",
                })
                continue
            source_page_id = str(page_contract.get("page_id") or source_component.split("::", 1)[0] or "")
            group_key = (filename, source_page_id, source_component)
            seen_group_routes = {
                (
                    item.get("target_filename", ""),
                    item.get("target_page_id", ""),
                    item.get("route_trigger_text", ""),
                )
                for item in groups[group_key]
            }
            for action in matching_actions:
                target_filename = _base_filename(action.get("target_filename"))
                target_page_id = str(action.get("target_page_id") or "").strip()
                route_key = (
                    target_filename,
                    target_page_id,
                    str(action.get("trigger_text") or "").strip(),
                )
                if route_key in seen_group_routes:
                    continue
                seen_group_routes.add(route_key)
                matched_route_count += 1
                evidence_steps = action.get("evidence_steps", [])
                if not isinstance(evidence_steps, list):
                    evidence_steps = [str(evidence_steps)]
                groups[group_key].append({
                    "action_id": str(action.get("action_id") or ""),
                    "route_trigger_text": str(action.get("trigger_text") or ""),
                    "dynamic_trigger_text": trigger_text,
                    "target_filename": target_filename,
                    "target_page_id": target_page_id,
                    "evidence_steps": _dedupe((str(step) for step in evidence_steps), limit=32),
                    "event": event,
                    "sdk_method": str(transition.get("sdk_method") or ""),
                    "target_source": str(action.get("target_source") or "route_execution_contract"),
                })

    conflicts: List[Dict[str, Any]] = []
    for (filename, source_page_id, source_component), routes in sorted(groups.items()):
        targets: Dict[str, Dict[str, str]] = {}
        for route in routes:
            target_key = str(route.get("target_page_id") or route.get("target_filename") or "").strip()
            if not target_key:
                continue
            targets.setdefault(target_key, {
                "target_filename": str(route.get("target_filename") or ""),
                "target_page_id": str(route.get("target_page_id") or ""),
            })
        if len(targets) <= 1:
            continue
        sorted_routes = sorted(
            routes,
            key=lambda item: (
                str(item.get("target_filename") or ""),
                str(item.get("target_page_id") or ""),
                str(item.get("action_id") or ""),
            ),
        )
        conflicts.append({
            "conflict_type": "component_instance_multi_target_route",
            "source_filename": filename,
            "source_page_id": source_page_id,
            "source_component": source_component,
            "target_count": len(targets),
            "target_filenames": sorted(
                item["target_filename"] for item in targets.values() if item.get("target_filename")
            ),
            "target_page_ids": sorted(
                item["target_page_id"] for item in targets.values() if item.get("target_page_id")
            ),
            "routes": sorted_routes,
        })

    return {
        "format": "component_route_outedge_conflicts",
        "schema_revision": 1,
        "summary": {
            "page_count": len(by_filename),
            "navigation_transition_count": navigation_transition_count,
            "matched_route_count": matched_route_count,
            "l0_route_action_count": l0_route_action_count,
            "component_instance_count": len(groups),
            "conflict_count": len(conflicts),
            "unresolved_navigation_transition_count": len(unresolved),
        },
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "unresolved_navigation_transitions": unresolved,
        "blocking_policy": {
            "gate": "component_instance_multi_target_route",
            "config_flag": "component_route_outedge_hard_gate_enabled",
            "hard_gate_when_conflict_count_gt_zero": True,
        },
    }


def build_route_affordance_candidates(page_execution_contracts: Dict[str, Any]) -> Dict[str, Any]:
    by_filename = _page_execution_contracts_payload_by_filename(page_execution_contracts)
    pages_by_filename: Dict[str, List[Dict[str, Any]]] = {}
    for filename, page_contract in by_filename.items():
        affordances = _route_navigation_affordances(page_contract, by_filename)
        if affordances:
            pages_by_filename[_base_filename(filename)] = affordances
    return {
        "format": "route_affordance_candidates",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.page_environment.route_affordance_candidates",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "artifact_role": "candidate_projection",
            "logical_owner": "page_environment_spec",
            "semantic_role": "route_affordance_candidates",
            "producer": "site_model.page_environment_spec.build_route_affordance_candidates",
            "allowed_consumers": ["page_environment_compiler"],
            "debug_only": False,
            "source_selection": {
                "derived_from": "phase_consolidation/site_model/page_execution_contracts.json",
                "selected_fields": [
                    "label",
                    "display_labels",
                    "target_filename",
                    "source_page_id",
                    "source_general_page_id",
                    "source_materialized_page_id",
                    "target_page_id",
                    "target_page_name",
                    "target_general_page_id",
                    "target_materialized_page_id",
                    "evidence_steps",
                    "surface",
                    "binding_kind",
                    "component_ref",
                    "action_id",
                    "source",
                ],
            },
        },
        "pages_by_filename": pages_by_filename,
        "page_count": len(pages_by_filename),
        "affordance_count": sum(len(items) for items in pages_by_filename.values()),
    }


def build_route_affordances_final(page_environment_specs: Dict[str, Any]) -> Dict[str, Any]:

    routes: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    page_envs = page_environment_specs.get("page_environment_specs_by_filename", {}) if isinstance(page_environment_specs, dict) else {}
    materialized_id_by_filename: Dict[str, str] = {}
    page_id_by_filename: Dict[str, str] = {}
    if isinstance(page_envs, dict):
        for filename, page_env in page_envs.items():
            if not isinstance(page_env, dict):
                continue
            base_filename = _base_filename(filename)
            if not base_filename:
                continue
            materialized_id = str(
                page_env.get("materialized_page_id")
                or page_env.get("site_model_page_id")
                or page_env.get("page_id")
                or ""
            )
            page_id = str(page_env.get("page_id") or page_env.get("site_model_page_id") or base_filename)
            if materialized_id:
                materialized_id_by_filename[base_filename] = materialized_id
            if page_id:
                page_id_by_filename[base_filename] = page_id

    def _surface_label(surface: Any, fallback: Any = "") -> str:
        if isinstance(surface, dict):
            return str(surface.get("label") or surface.get("display_label") or fallback or "")
        return str(fallback or surface or "")

    def _surface_component(surface: Any, fallback: Any = "") -> str:
        if isinstance(surface, dict):
            return str(surface.get("component_id") or surface.get("component_ref") or fallback or "")
        return str(fallback or "")

    def _append(
        raw: Dict[str, Any],
        *,
        source_filename: str = "",
        default_source_page_id: str = "",
        route_origin: str = "",
    ) -> None:
        if not isinstance(raw, dict):
            return
        surface = raw.get("surface") if isinstance(raw.get("surface"), dict) else {}
        interaction = raw.get("interaction_contract") if isinstance(raw.get("interaction_contract"), dict) else {}
        effect = interaction.get("state_effect") if isinstance(interaction.get("state_effect"), dict) else {}
        source_filename = _base_filename(raw.get("source_filename") or source_filename)
        source_page_id = str(
            raw.get("source_page_id")
            or interaction.get("source_page_id")
            or default_source_page_id
            or page_id_by_filename.get(source_filename, "")
            or source_filename
            or ""
        )
        target_page_id = str(
            raw.get("target_page_id")
            or interaction.get("target_page_id")
            or effect.get("target_page_id")
            or ""
        )
        target_filename = _base_filename(
            raw.get("target_filename")
            or interaction.get("expected_filename")
            or effect.get("target_filename")
            or ""
        )
        label = _surface_label(surface, raw.get("control_label") or raw.get("display_label") or raw.get("label"))
        component_id = _surface_component(surface, raw.get("component_id") or raw.get("component_ref"))
        source_mid = str(
            raw.get("source_materialized_page_id")
            or interaction.get("source_materialized_page_id")
            or materialized_id_by_filename.get(source_filename, "")
            or source_page_id
            or ""
        )
        target_mid = str(
            raw.get("target_materialized_page_id")
            or interaction.get("target_materialized_page_id")
            or effect.get("target_materialized_page_id")
            or materialized_id_by_filename.get(target_filename, "")
            or target_page_id
            or target_filename
            or ""
        )
        if not source_page_id or not (target_page_id or target_filename):
            return
        route_authority = str(
            raw.get("route_authority")
            or raw.get("authority")
            or ("first_level_alternative" if raw.get("source") == "page_environment_route_affordance_compiler" else "")
            or "reference_observed"
        )
        key = (source_page_id, component_id or label, target_page_id or target_filename, route_authority)
        if key in seen:
            return
        seen.add(key)
        item = {
            "route_id": str(raw.get("route_id") or raw.get("action_id") or raw.get("component_id") or "::".join(key)),
            "source_filename": source_filename,
            "source_page_id": source_page_id,
            "target_page_id": target_page_id or target_filename,
            "source_materialized_page_id": source_mid,
            "source_state_signature": str(raw.get("source_state_signature") or interaction.get("source_state_signature") or ".base"),
            "visible_surface_identity": {
                "component_id": component_id,
                "label": label,
                "surface": raw.get("surface") if raw.get("surface") not in (None, "") else "",
            },
            "action_family": str(raw.get("action_family") or interaction.get("operation") or "click"),
            "target_materialized_page_id": target_mid,
            "target_state_signature": str(raw.get("target_state_signature") or interaction.get("target_state_signature") or ".base"),
            "route_authority": route_authority,
            "authority": route_authority,
            "route_origin": route_origin or str(raw.get("source") or "page_environment_spec"),
            "identity_complete": bool(source_mid and target_mid and (target_page_id or target_filename)),
        }
        if target_filename:
            item["target_filename"] = target_filename
        if raw.get("step_key"):
            item["step_key"] = str(raw.get("step_key") or "")
        if isinstance(raw.get("transition_identity_key"), dict):
            item["transition_identity_key"] = raw.get("transition_identity_key")
        routes.append(item)

    if isinstance(page_envs, dict):
        for filename, page_env in page_envs.items():
            if not isinstance(page_env, dict):
                continue
            source_page_id = str(page_env.get("page_id") or page_env.get("site_model_page_id") or filename)
            for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
                if not isinstance(transition, dict):
                    continue
                source = _base_filename(transition.get("source_filename") or filename)
                target = _base_filename(transition.get("target_filename"))
                operation = str(transition.get("operation") or transition.get("operation_type") or "")
                if not source or not target or source == target:
                    continue
                if operation not in {"click", "tap", "fill", "select"}:
                    continue
                raw = dict(transition)
                raw.setdefault("source_page_id", page_id_by_filename.get(source, source_page_id))
                raw.setdefault("target_page_id", page_id_by_filename.get(target, target))
                raw.setdefault("source_materialized_page_id", materialized_id_by_filename.get(source, ""))
                raw.setdefault("target_materialized_page_id", materialized_id_by_filename.get(target, ""))
                raw.setdefault("label", transition.get("operation_intent") or transition.get("command") or operation)
                raw.setdefault("route_authority", "reference_observed")
                raw.setdefault("action_family", operation)
                _append(raw, source_filename=source, default_source_page_id=source_page_id, route_origin="transition")
            for route in page_env.get("routes", []) if isinstance(page_env.get("routes"), list) else []:
                _append(route, source_filename=str(filename), default_source_page_id=source_page_id, route_origin="routes")
            for affordance in page_env.get("route_affordances", []) if isinstance(page_env.get("route_affordances"), list) else []:
                _append(affordance, source_filename=str(filename), default_source_page_id=source_page_id, route_origin="route_affordances")
            for component in page_env.get("components", []) if isinstance(page_env.get("components"), list) else []:
                if isinstance(component, dict) and str(component.get("component_role") or "") == "route_navigation_affordance":
                    _append(component, source_filename=str(filename), default_source_page_id=source_page_id, route_origin="component")

    pages = page_environment_specs.get("pages", []) if isinstance(page_environment_specs, dict) else []
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            source_page_id = str(page.get("page_id") or page.get("filename") or "")
            for route in page.get("routes", []) if isinstance(page.get("routes"), list) else []:
                _append(route, source_filename=str(page.get("filename") or ""), default_source_page_id=source_page_id, route_origin="pages")

    routes_by_source_filename: Dict[str, List[Dict[str, Any]]] = {}
    for route in routes:
        source_filename = _base_filename(route.get("source_filename"))
        if source_filename:
            routes_by_source_filename.setdefault(source_filename, []).append(route)

    return {
        "format": "route_affordances_final",
        "schema_revision": 1,
        "artifact_role": "projection",
        "canonical_prompt_boundary": False,
        "canonical_semantic_source": False,
        "artifact_metadata": {
            "artifact_id": "projections.page_environment.route_affordances_final",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "logical_owner": "page_environment_spec",
            "producer": "site_model.page_environment_spec.build_route_affordances_final",
            "allowed_consumers": ["runtime_backend", "replay", "validator"],
            "source_selection": {
                "derived_from": "phase_page_environment/semantic_api/06_page_environment_specs/page_environment_specs.json"
            },
        },
        "route_count": len(routes),
        "routes": routes,
        "routes_by_source_filename": routes_by_source_filename,
    }


def _page_execution_contracts_payload_by_filename(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    by_filename = payload.get("pages_by_filename")
    if isinstance(by_filename, dict):
        return {
            _base_filename(filename): page
            for filename, page in by_filename.items()
            if _base_filename(filename) and isinstance(page, dict)
        }
    pages = payload.get("pages", [])
    if not isinstance(pages, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        filename = _base_filename(page.get("filename"))
        if filename:
            result[filename] = page
    return result


def _route_affordance_label_key(label: str) -> str:
    text = str(label or "").strip().lower()
    text = text.replace("...", "").replace("…", "")
    text = re.sub(r"[\(\（\[\【][^\)\）\]\】]*$", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text


def _route_l0_binding_targets(page_env: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    bindings: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
        if not isinstance(transition, dict):
            continue
        if str(transition.get("direction") or "") != "source":
            continue
        if str(transition.get("l0_role") or "") != "execution_anchor":
            continue
        if str(transition.get("reducer") or "") in {"fill_form", "type_search", "scroll_viewport"}:
            continue
        source = _base_filename(transition.get("source_filename"))
        target = _base_filename(transition.get("target_filename"))
        if not source or not target or source == target:
            continue
        intent_key = _route_affordance_label_key(str(transition.get("operation_intent") or ""))
        if not intent_key:
            continue
        bindings[intent_key].append({
            "target_filename": target,
            "transition_id": str(transition.get("transition_id") or transition.get("step_key") or ""),
            "operation_intent": str(transition.get("operation_intent") or ""),
        })
    return bindings


def _route_affordance_conflicts_with_l0(
    affordance: Dict[str, Any],
    l0_targets_by_intent: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any] | None:
    label_key = _route_affordance_label_key(str(affordance.get("label") or ""))
    target = _base_filename(affordance.get("target_filename"))
    if not label_key or not target:
        return None
    for intent_key, l0_targets in l0_targets_by_intent.items():
        if label_key not in intent_key and intent_key not in label_key:
            continue
        expected_targets = {
            _base_filename(item.get("target_filename"))
            for item in l0_targets
            if _base_filename(item.get("target_filename"))
        }
        if expected_targets and target not in expected_targets:
            return {
                "label": str(affordance.get("label") or ""),
                "target_filename": target,
                "canonical_l0_target_filenames": sorted(expected_targets),
                "reason": "alternative_route_rejected_by_l0_binding",
                "source": "page_environment_route_affordance_l0_gate",
                "l0_transition_ids": _dedupe(
                    [str(item.get("transition_id") or "") for item in l0_targets],
                    limit=16,
                ),
            }
    return None


def _route_affordance_has_materialized_identity(action: Dict[str, Any]) -> bool:
    return all(
        str(action.get(key) or "").strip()
        for key in (
            "source_page_id",
            "source_general_page_id",
            "source_materialized_page_id",
            "target_page_id",
            "target_general_page_id",
            "target_materialized_page_id",
        )
    )


def _contract_route_path(contract: Dict[str, Any]) -> str:
    route_context = contract.get("route_context") if isinstance(contract.get("route_context"), dict) else {}
    route_execution = contract.get("route_execution_contract") if isinstance(contract.get("route_execution_contract"), dict) else {}
    return str(route_context.get("route_path") or route_execution.get("route_path") or "").strip()


def _contract_materialized_key(contract: Dict[str, Any]) -> str:
    key = str(contract.get("materialized_page_key") or contract.get("instance_key") or "").strip()
    if key:
        return key
    materialized_id = str(contract.get("materialized_page_id") or "").strip()
    if "::" in materialized_id:
        return materialized_id.rsplit("::", 1)[-1]
    return ""


def _contract_is_search_entry(contract: Dict[str, Any]) -> bool:
    if not isinstance(contract, dict):
        return False
    general_id = str(contract.get("general_page_id") or "").strip()
    route_path = _contract_route_path(contract).rstrip("/")
    key = _contract_materialized_key(contract)
    page_name = str(contract.get("page_name") or "").lower()
    page_type = str(contract.get("page_type") or "").strip()
    filename = _base_filename(contract.get("filename"))
    if page_type == "search_entry":
        return True
    if not general_id or "search" not in general_id:
        return False
    if route_path.endswith("/search") or route_path in {"/search", "/search/entry"}:
        return True
    if key in {"entry", "search_entry", "blank", "empty_query"}:
        return True
    if _is_search_entry_filename(filename) and not _contains_any(page_name, ("result", "results", "结果")):
        return True
    return "无关键词" in page_name or "blank" in filename


def _search_namespace_key(contract: Dict[str, Any]) -> str:
    general_id = str(contract.get("general_page_id") or "").strip()
    if general_id.endswith("_search_entry"):
        return general_id[: -len("_entry")]
    if general_id.endswith("_search_results"):
        return general_id[: -len("_results")]
    if general_id:
        return general_id
    route_path = _contract_route_path(contract).strip("/")
    parts = route_path.split("/")
    if "search" in parts:
        index = parts.index("search")
        return "/".join(parts[: index + 1])
    return route_path


def _resolve_search_activation_target_contract(
    target: str,
    target_contract: Dict[str, Any],
    target_contracts_by_filename: Dict[str, Dict[str, Any]],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    if _contract_is_search_entry(target_contract):
        return target, target_contract, {"activation_target_kind": "search_entry"}

    namespace = _search_namespace_key(target_contract)
    candidates: List[Tuple[int, str, Dict[str, Any]]] = []
    for filename, contract in target_contracts_by_filename.items():
        if not isinstance(contract, dict) or not _contract_is_search_entry(contract):
            continue
        if namespace and _search_namespace_key(contract) != namespace:
            continue
        route_path = _contract_route_path(contract).rstrip("/")
        key = _contract_materialized_key(contract)
        rank = 0
        if str(contract.get("page_type") or "").strip() == "search_entry":
            rank += 120
        if key in {"entry", "search_entry", "blank", "empty_query"}:
            rank += 100
        if route_path.endswith("/search") or route_path == "/search":
            rank += 80
        candidates.append((rank, _base_filename(filename), contract))
    if not candidates:
        return target, target_contract, {
            "activation_target_kind": "missing_search_entry",
            "original_target_filename": target,
            "search_namespace": namespace,
        }
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, resolved_filename, resolved_contract = candidates[0]
    return resolved_filename, resolved_contract, {
        "activation_target_kind": "search_entry",
        "original_target_filename": target,
        "search_namespace": namespace,
        "resolution_source": "search_activation_entry_resolver",
    }


def _attach_route_navigation_affordance_specs(
    site_model,
    page_envs: Dict[str, Dict[str, Any]],
) -> None:
    affordances_by_filename = _load_route_affordance_candidates_by_filename(site_model)
    for filename, page_env in page_envs.items():
        if not isinstance(page_env, dict):
            continue
        base_filename = _base_filename(filename)
        affordances = affordances_by_filename.get(base_filename, [])
        if affordances and not all(_route_affordance_has_materialized_identity(item) for item in affordances):
            affordances = []
        if not affordances:
            continue
        l0_targets_by_intent = _route_l0_binding_targets(page_env)
        for affordance in affordances:
            if not _route_affordance_has_materialized_identity(affordance):
                page_env.setdefault("dropped_route_affordances", []).append({
                    "label": str(affordance.get("label") or ""),
                    "target_filename": _base_filename(affordance.get("target_filename")),
                    "reason": "missing_materialized_route_identity",
                    "source": "page_environment_route_affordance_identity_gate",
                })
                continue
            conflict = _route_affordance_conflicts_with_l0(affordance, l0_targets_by_intent)
            if conflict:
                page_env.setdefault("dropped_route_affordances", []).append(conflict)
                continue
            label = str(affordance.get("label") or "")
            target = _base_filename(affordance.get("target_filename"))
            if not label or not target:
                continue
            seed = _safe_id(f"{filename}:{label}:{target}")
            component_id = f"cmp::{seed}::route_affordance"
            projection_id = f"projection::{seed}::route_affordance"
            surface = str(affordance.get("surface") or "shell_navigation")
            dom_contract = {
                "required_behavior": "visible_actionable_control",
                "forbid_noninteractive_host": True,
                "click_surface": "real_visible_component",
            }
            evidence_steps = [
                str(step)
                for step in affordance.get("evidence_steps", [])
                if str(step or "").strip()
            ] if isinstance(affordance.get("evidence_steps", []), list) else []
            expected_interaction = {
                "operation": "click",
                "display_label": label,
                "display_labels": affordance.get("display_labels", [label]),
                "expected_filename": target,
                "source_page_id": str(affordance.get("source_page_id") or page_env.get("site_model_page_id") or ""),
                "source_general_page_id": str(affordance.get("source_general_page_id") or page_env.get("general_page_id") or ""),
                "source_materialized_page_id": str(affordance.get("source_materialized_page_id") or ""),
                "target_page_id": str(affordance.get("target_page_id") or ""),
                "target_page_name": str(affordance.get("target_page_name") or ""),
                "target_general_page_id": str(affordance.get("target_general_page_id") or ""),
                "target_materialized_page_id": str(affordance.get("target_materialized_page_id") or ""),
                "route_binding_semantics": affordance.get("route_binding_semantics", {}) if isinstance(affordance.get("route_binding_semantics"), dict) else {},
                "reducer": "navigate_route",
                "state_effect": {
                    "effect": "navigate_route",
                    "target_filename": target,
                    "target_page_id": str(affordance.get("target_page_id") or ""),
                    "target_page_name": str(affordance.get("target_page_name") or ""),
                    "target_general_page_id": str(affordance.get("target_general_page_id") or ""),
                    "target_materialized_page_id": str(affordance.get("target_materialized_page_id") or ""),
                    "route_binding_semantics": affordance.get("route_binding_semantics", {}) if isinstance(affordance.get("route_binding_semantics"), dict) else {},
                    "mutation_policy": "route_transition_only",
                },
                "surface": surface,
                "dom_contract": dom_contract,
                "evidence_steps": evidence_steps,
            }
            source = "page_environment_route_affordance_compiler"
            _add_unique_dict(
                page_env.setdefault("components", []),
                {
                    "component_id": component_id,
                    "component_kind": "shell_navigation_affordance",
                    "component_role": "route_navigation_affordance",
                    "reads_state": "state::page_runtime",
                    "render_phase": "initial",
                    "layout_policy": "shell_slot_navigation",
                    "surface": surface,
                    "display_label": label,
                    "display_labels": affordance.get("display_labels", [label]),
                    "target_filename": target,
                    "source_page_id": str(affordance.get("source_page_id") or page_env.get("site_model_page_id") or ""),
                    "source_general_page_id": str(affordance.get("source_general_page_id") or page_env.get("general_page_id") or ""),
                    "source_materialized_page_id": str(affordance.get("source_materialized_page_id") or ""),
                    "target_page_id": str(affordance.get("target_page_id") or ""),
                    "target_page_name": str(affordance.get("target_page_name") or ""),
                    "target_general_page_id": str(affordance.get("target_general_page_id") or ""),
                    "target_materialized_page_id": str(affordance.get("target_materialized_page_id") or ""),
                    "route_binding_semantics": affordance.get("route_binding_semantics", {}) if isinstance(affordance.get("route_binding_semantics"), dict) else {},
                    "interaction_required": True,
                    "interaction_contract": expected_interaction,
                    "dom_contract": dom_contract,
                    "source_route_action_id": affordance.get("action_id", ""),
                    "evidence_steps": evidence_steps,
                    "source": source,
                },
                ("component_id",),
            )
            _add_unique_dict(
                page_env.setdefault("render_projections", []),
                {
                    "projection_id": projection_id,
                    "component_ref": component_id,
                    "reads_state": "state::page_runtime",
                    "render_phase": "initial",
                    "layout_policy": "shell_slot_navigation",
                    "surface": surface,
                    "display_label": label,
                    "display_labels": affordance.get("display_labels", [label]),
                    "target_filename": target,
                    "target_page_id": str(affordance.get("target_page_id") or ""),
                    "target_page_name": str(affordance.get("target_page_name") or ""),
                    "target_general_page_id": str(affordance.get("target_general_page_id") or ""),
                    "target_materialized_page_id": str(affordance.get("target_materialized_page_id") or ""),
                    "route_binding_semantics": affordance.get("route_binding_semantics", {}) if isinstance(affordance.get("route_binding_semantics"), dict) else {},
                    "interaction_required": True,
                    "expected_interaction": expected_interaction,
                    "dom_contract": dom_contract,
                    "evidence_steps": evidence_steps,
                    "source": source,
                },
                ("projection_id",),
            )
            _add_unique_dict(
                page_env.setdefault("oracles", []),
                {
                    "oracle_type": "component_interactable",
                    "component_ref": component_id,
                    "expected": "Route/navigation affordance is visible and actionable.",
                    "display_label": label,
                    "dom_contract": dom_contract,
                    "source": source,
                },
                ("oracle_type", "component_ref"),
            )
            _add_unique_dict(
                page_env.setdefault("oracles", []),
                {
                    "oracle_type": "route_target",
                    "component_ref": component_id,
                    "expected_filename": target,
                    "target_page_id": str(affordance.get("target_page_id") or ""),
                    "target_general_page_id": str(affordance.get("target_general_page_id") or ""),
                    "target_materialized_page_id": str(affordance.get("target_materialized_page_id") or ""),
                    "display_label": label,
                    "evidence_steps": evidence_steps,
                    "source": source,
                },
                ("oracle_type", "component_ref", "expected_filename"),
            )


def _annotate_materialized_page_specs(site_model, page_envs: Dict[str, Dict[str, Any]]) -> None:
    _pages_by_id, pages_by_filename, _pages_by_general = _page_signature_maps(site_model)
    for filename, page in pages_by_filename.items():
        page_env = page_envs.setdefault(filename, _new_page_environment(filename))
        page_env["filename"] = filename
        page_env["site_model_page_id"] = str(page.get("page_id") or "")
        page_env["page_name"] = str(page.get("page_name") or "")
        page_env["general_page_id"] = str(page.get("general_page_id") or "")
        page_env["materialized_page_key"] = str(page.get("materialized_page_key") or "")
        materialized_page_id = str(page.get("materialized_page_id") or "").strip()
        if not materialized_page_id:
            general_page_id = str(page.get("general_page_id") or "").strip()
            materialized_page_key = str(page.get("materialized_page_key") or "").strip()
            materialized_page_id = (
                f"{general_page_id}::{materialized_page_key}"
                if general_page_id and materialized_page_key
                else str(page.get("page_id") or "")
            )
        page_env["materialized_page_id"] = materialized_page_id
        page_env["finite_state_dimensions"] = page.get("finite_state_dimensions", []) if isinstance(page.get("finite_state_dimensions"), list) else []
        page_env["interaction_contract_ids"] = page.get("interaction_contract_ids", []) if isinstance(page.get("interaction_contract_ids"), list) else []
        if page.get("excluded_from_pagegen") is True:
            page_env["excluded_from_pagegen"] = True
            page_env["pagegen_exclusion_reason"] = str(page.get("pagegen_exclusion_reason") or "")
        provenance = page_env.setdefault("provenance", {})
        if not provenance.get("evidence_steps"):
            provenance["source"] = "site_model_page_signature_without_trace_step"


def _compact_spec_item_rank(item: Dict[str, Any]) -> Tuple[int, str]:
    if not isinstance(item, dict):
        return (99, "")
    role = str(item.get("component_role") or "")
    source = str(item.get("source") or "")
    oracle_type = str(item.get("oracle_type") or "")
    if role == "route_navigation_affordance" or source == "page_environment_route_affordance_compiler":
        return (0, str(item.get("component_id") or item.get("projection_id") or item.get("component_ref") or ""))
    if item.get("interaction_required") is True or oracle_type == "component_interactable":
        return (1, str(item.get("component_id") or item.get("projection_id") or item.get("step_key") or ""))
    if str(item.get("l0_transition_ids") or "") or str(item.get("step_key") or ""):
        return (2, str(item.get("component_id") or item.get("projection_id") or item.get("step_key") or ""))
    return (3, str(item.get("component_id") or item.get("projection_id") or item.get("oracle_type") or ""))


def compact_page_environment_spec_for_prompt(page_env: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(page_env, dict):
        return {}
    compact = {
        key: page_env.get(key)
        for key in PAGE_ENVIRONMENT_SPEC_KEYS
        if key in page_env
    }
    compact["filename"] = page_env.get("filename", "")
    compact["layout_content_conflict_policy"] = page_env.get("layout_content_conflict_policy", {})
    compact["provenance"] = {
        "source_trace_ids": (page_env.get("provenance", {}) or {}).get("source_trace_ids", [])[:12],
        "evidence_steps": (page_env.get("provenance", {}) or {}).get("evidence_steps", [])[:24],
    }
    for list_key, limit in (
            ("components", 64),
            ("render_projections", 64),
            ("transitions", 48),
            ("parameterized_inputs", 16),
            ("query_projections", 24),
            ("oracles", 96),
            ("conflict_resolutions", 24),
        ):
        if isinstance(compact.get(list_key), list):
            if list_key in {"components", "render_projections", "oracles"}:
                compact[list_key] = sorted(
                    [item for item in compact[list_key] if isinstance(item, dict)],
                    key=_compact_spec_item_rank,
                )[:limit]
            else:
                compact[list_key] = compact[list_key][:limit]
    if isinstance(compact.get("conflict_resolutions"), list):
        compact["conflict_resolutions"] = [
            {
                key: value
                for key, value in item.items()
                if key != "candidate_summary" or isinstance(value, dict)
            }
            for item in compact["conflict_resolutions"]
            if isinstance(item, dict)
        ]
    if page_env.get("conflict_resolution_summary"):
        compact["conflict_resolution_summary"] = page_env.get("conflict_resolution_summary")
    return compact


def build_page_environment_specs(
    site_model,
    result_dir: Path,
    *,
    trace_ids: Sequence[str] | None = None,
    include_route_affordance_candidates: bool = False,
) -> Dict[str, Any]:
    packs = _load_action_packs_for_page_environment(site_model)
    selected_trace_ids = {str(item) for item in trace_ids or [] if str(item)}
    if selected_trace_ids:
        packs = [pack for pack in packs if str(pack.get("trace_id") or "") in selected_trace_ids]
    runtime_by_trace = _load_runtime_obligations(site_model)
    pagegen_by_page = _load_pagegen_obligations(site_model)
    filename_overrides = _observation_filename_overrides(site_model)
    materialized_bindings_source = filename_overrides.pop("__source_materialized_observation_bindings_path__", "")

    specs: List[Dict[str, Any]] = []
    for pack in packs:
        trace_id = str(pack.get("trace_id") or "")
        terminal_contract = (
            dict(pack.get("terminal_contract"))
            if isinstance(pack.get("terminal_contract"), dict)
            else {}
        )
        steps = [dict(step, trace_id=trace_id) for step in pack.get("steps", []) if isinstance(step, dict)]
        snapshots, collection_facts = _extract_step_states(trace_id, steps, runtime_by_trace.get(trace_id, []), filename_overrides)
        transitions, input_sessions = _build_transitions(
            trace_id,
            steps,
            filename_overrides,
            getattr(site_model, "step_boundary_contracts", {}) or {},
        )
        spec = {
            "trace_id": trace_id,
            "task": str(pack.get("task") or ""),
            "terminal_contract": terminal_contract,
            "source": {
                "l0_execution_pack": "phase_consolidation/projections/l0_execution/l0_execution_pack.json",
                "runtime_state_obligations": "phase_instance_splitting/runtime_state_obligations.json",
                "route_affordance_candidates": {
                    "path": "phase_page_environment_compiler/route_affordance_candidates.json",
                    "included": bool(include_route_affordance_candidates),
                },
                "materialized_observation_bindings": materialized_bindings_source,
                "step_boundary_contracts": (
                    "phase_consolidation/materialized_bindings/step_boundary_contracts.json"
                ),
            },
            "principles": {
                "observation_priority": "screenshot_observation_over_action_thought",
                "pagegen_input": "deterministic_unified_spec",
                "typed_inputs": "parameterized_runtime_state_not_initial_prefill",
                "collection_updates": "preserve_existing_then_append_or_upsert",
            },
            "pages": _build_pages(steps, pagegen_by_page, filename_overrides),
            "state_snapshots": snapshots,
            "input_sessions": input_sessions,
            "collection_facts": list(collection_facts.values()),
            "transitions": transitions,
        }
        specs.append(spec)
    page_environment_specs_by_filename = build_materialized_page_environment_specs(specs)
    _annotate_materialized_page_specs(site_model, page_environment_specs_by_filename)
    page_environment_specs_by_filename = {
        filename: page_env
        for filename, page_env in page_environment_specs_by_filename.items()
        if not (isinstance(page_env, dict) and page_env.get("excluded_from_pagegen") is True)
    }
    if include_route_affordance_candidates:
        _attach_route_navigation_affordance_specs(site_model, page_environment_specs_by_filename)
    route_materialization = _load_canonical_route_materialization(site_model)
    payload = {
        "format": "page_environment_specs",
        "schema_revision": 2,
        "artifact_metadata": {
            "artifact_id": "semantic_api.page_environment_specs",
            "artifact_kind": "api_semantic_source",
            "api_or_det": "mixed",
            "canonical_source": True,
            "canonical_semantic_source": True,
            "canonical_prompt_boundary": False,
            "artifact_role": "authority",
            "logical_owner": "page_environment_spec",
            "semantic_role": "authoritative_environment_spec",
            "producer": "site_model.page_environment_spec.build_page_environment_specs",
            "allowed_consumers": ["pagegen", "runtime_backend", "validator"],
            "debug_only": False,
        },
        "result_dir": str(result_dir),
        "trace_count": len(specs),
        "page_count": len(page_environment_specs_by_filename),
        "page_environment_specs_by_filename": page_environment_specs_by_filename,
        "specs": specs,
        "terminal_contracts_by_trace_id": {
            str(spec.get("trace_id") or ""): dict(spec["terminal_contract"])
            for spec in specs
            if str(spec.get("trace_id") or "")
            and isinstance(spec.get("terminal_contract"), dict)
            and spec.get("terminal_contract")
        },
    }
    if route_materialization:
        payload["canonical_route_materialization"] = route_materialization
    close_page_environment_contracts(payload)
    return payload


def _slug_tokens(filename: str) -> set[str]:
    base = Path(_base_filename(filename)).stem.lower()
    return {token for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", base) if len(token) >= 2}


def _filename_identity_tokens(filename: str) -> set[str]:
    base_tokens = _slug_tokens(filename)
    structural_route_tokens = {
        "category",
        "detail",
        "item",
        "page",
        "search",
        "results",
        "result",
        "list",
        "listing",
        "store",
        "stores",
        "merchant",
        "merchants",
        "home",
    }
    return {
        token for token in base_tokens
        if token not in structural_route_tokens and not token.endswith("results")
    }


def _is_generic_search_target(filename: str) -> bool:
    return _is_search_surface(filename) and not _filename_identity_tokens(filename)


def _identity_terms(value: Any) -> List[str]:
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, list):
        raw_values = [str(item or "") for item in value]
    else:
        raw_values = []
    terms: List[str] = []
    for raw in raw_values:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        terms.append(text)
        terms.extend(token for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", text) if len(token) >= 2)
    return _dedupe(terms, limit=32)


def _target_identity_hints(item: Dict[str, Any]) -> List[str]:
    hints: List[str] = []
    for key in (
        "target_identity_hints",
        "target_identity_terms",
        "target_query_hints",
        "identity_hints",
        "query_identity_hints",
    ):
        hints.extend(_identity_terms(item.get(key)))
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    if effect:
        hints.extend(_target_identity_hints(effect))
    return _dedupe(hints, limit=32)


_AUTOCOMPLETE_STRUCTURAL_HINT_MARKERS = (
    "icon",
    "row",
    "rows",
    "section",
    "panel",
    "list",
    "visible",
    "highlight",
    "highlighted",
    "matched",
    "suggestion",
    "suggestions",
    "autocomplete",
    "搜索",
    "列表",
    "图标",
    "高亮",
    "结果",
    "可见",
    "历史",
    "section",
)


def _parse_hint_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    if text.startswith("{") and text.endswith("}"):
        try:
            return ast.literal_eval(text)
        except Exception:
            return text
    return text


def _looks_like_autocomplete_candidate(value: str, query_value: str = "") -> bool:
    text = str(value or "").strip().strip("“”\"'")
    if not text:
        return False
    if len(text) > 36:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _AUTOCOMPLETE_STRUCTURAL_HINT_MARKERS):
        return False
    if re.search(r"\b(?:query|field|value|true|false|active|visible)\s*=", lowered):
        return False
    if "：" in text or ":" in text:
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def _candidate_rows_from_suggestion_hint(value: Any, *, query_value: str = "") -> List[str]:
    parsed = _parse_hint_payload(value)
    rows: List[str] = []
    if isinstance(parsed, dict):
        key = str(parsed.get("key") or parsed.get("field") or "").lower()
        raw = parsed.get("value")
        if key in {"query", "query_text", "keyword_highlight", "active", "city", "destination_input_placeholder"}:
            return []
        if key in {"suggestion_examples", "suggestions", "top_result", "refinement_option", "target_store_entry"}:
            values = raw if isinstance(raw, list) else [raw]
            for item in values:
                text = str(item or "").strip()
                if not text:
                    continue
                if key == "suggestion_examples" and query_value and query_value not in text:
                    text = f"{query_value}{text}"
                if _looks_like_autocomplete_candidate(text, query_value=query_value):
                    rows.append(text)
            return _dedupe(rows, limit=16)
        return []
    if isinstance(parsed, list):
        for item in parsed:
            rows.extend(_candidate_rows_from_suggestion_hint(item, query_value=query_value))
        return _dedupe(rows, limit=16)

    text = str(parsed or "").strip()
    if not text:
        return []
    lowered = text.lower()
    if lowered.startswith("suggestions:") or lowered.startswith("suggestion_examples:"):
        body = text.split(":", 1)[1]
        for part in re.split(r"[|,，、/]+", body):
            candidate = part.strip()
            if query_value and query_value not in candidate and lowered.startswith("suggestion_examples:"):
                candidate = f"{query_value}{candidate}"
            if _looks_like_autocomplete_candidate(candidate, query_value=query_value):
                rows.append(candidate)
        return _dedupe(rows, limit=16)
    if _looks_like_autocomplete_candidate(text, query_value=query_value):
        rows.append(text)
    return _dedupe(rows, limit=16)


def _trace_step_from_action_id(action_id: str) -> Tuple[str, int | None]:
    text = str(action_id or "")
    match = re.match(r"(?P<trace>.+):step(?P<idx>\d+)$", text)
    if match:
        return match.group("trace"), int(match.group("idx"))
    match = re.match(r"(?P<trace>.+):(?P<idx>\d+)$", text)
    if match:
        return match.group("trace"), int(match.group("idx"))
    return text.split(":", 1)[0], None


def _runtime_autocomplete_observation_rows(
    runtime_observations: Dict[str, Any],
    *,
    page_cluster_id: str,
    action_id: str,
    query_value: str,
) -> List[Dict[str, str]]:
    observations = runtime_observations.get("observations", []) if isinstance(runtime_observations, dict) else []
    if not isinstance(observations, list) or not page_cluster_id:
        return []
    trace_id, step_index = _trace_step_from_action_id(action_id)
    rows: List[Dict[str, str]] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        if str(item.get("page_cluster_id") or "") != page_cluster_id:
            continue
        obs_trace = str(item.get("trace_id") or "")
        if trace_id and obs_trace and obs_trace != trace_id:
            continue
        if step_index is not None:
            try:
                obs_step = int(item.get("step_index"))
            except Exception:
                obs_step = None
            if obs_step is not None and obs_step not in {step_index, step_index + 1}:
                continue
        scope_text = " ".join(
            str(item.get(key) or "")
            for key in ("scope", "state_scope", "raw_evidence", "lifecycle", "mutation")
        ).lower()
        if not any(marker in scope_text for marker in ("suggest", "autocomplete", "候选", "联想", "推荐", "result")):
            continue
        for hint in item.get("value_hints", []) if isinstance(item.get("value_hints"), list) else []:
            for title in _candidate_rows_from_suggestion_hint(hint, query_value=query_value):
                if not any(row["title"] == title for row in rows):
                    rows.append(
                        {
                            "title": title,
                            "category": "",
                            "source": "runtime_state_observations.value_hints",
                        }
                    )
        if len(rows) >= 16:
            break
    return rows[:16]


def _hint_rows_for_projection(
    projection: Dict[str, Any],
    transition: Dict[str, Any],
    *,
    query_value: str,
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for source_item in (transition, transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}):
        if not isinstance(source_item, dict):
            continue
        for hint in source_item.get("value_hints", []) if isinstance(source_item.get("value_hints"), list) else []:
            for title in _candidate_rows_from_suggestion_hint(hint, query_value=query_value):
                if not any(row["title"] == title for row in rows):
                    rows.append({"title": title, "category": "", "source": "transition.value_hints"})
    return rows[:16]


def _existing_suggestions_are_identity_hints(rows: Any) -> bool:
    if not isinstance(rows, list) or not rows:
        return False
    sources = set()
    for row in rows:
        if isinstance(row, dict):
            source = str(row.get("source") or "").strip()
            if source:
                sources.add(source)
        elif isinstance(row, str):
            sources.add("string")
    return bool(sources) and sources <= {"target_identity_hints", "suggestion_contract.expected_titles"}


def materialize_autocomplete_suggestion_contracts(
    payload: Dict[str, Any],
    runtime_state_observations: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    runtime_state_observations = runtime_state_observations or {}
    page_envs = payload.get("page_environment_specs_by_filename", {}) if isinstance(payload, dict) else {}
    if not isinstance(page_envs, dict):
        return {"format": "autocomplete_suggestion_materialization", "materialized_count": 0, "pages": []}
    pages: List[Dict[str, Any]] = []
    materialized_count = 0
    for filename, page_env in page_envs.items():
        if not isinstance(page_env, dict):
            continue
        general_page_id = str(page_env.get("general_page_id") or page_env.get("page_cluster_id") or "")
        transitions_by_step = {
            canonical_step_key(t.get("step_key") or t.get("transition_id") or ""): t
            for t in page_env.get("transitions", []) if isinstance(t, dict)
        }
        page_rows = 0
        for projection in page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []:
            if not isinstance(projection, dict):
                continue
            suggestion_contract = projection.get("suggestion_contract")
            if not isinstance(suggestion_contract, dict):
                continue
            existing_rows = suggestion_contract.get("suggestions")
            if _existing_suggestions_are_identity_hints(existing_rows):
                suggestion_contract.pop("suggestions", None)
                suggestion_contract.pop("expected_candidate_count", None)
                suggestion_contract["suppressed_identity_hint_autocomplete_projection"] = True
                suggestion_contract["suppression_reason"] = (
                    "existing_target_identity_hint_rows_are_not_visible_autocomplete_rows"
                )
                existing_rows = None
            if isinstance(existing_rows, list) and existing_rows:
                continue
            query_value = str(suggestion_contract.get("query_value") or projection.get("query_value") or "").strip()
            action_id = canonical_step_key(
                str(
                    projection.get("action_id")
                    or (projection.get("source_step_keys") or [""])[0]
                    or (projection.get("l0_transition_ids") or [""])[0]
                    or ""
                )
            )
            transition = transitions_by_step.get(action_id, {})
            transition_effect = (
                transition.get("state_effect")
                if isinstance(transition, dict) and isinstance(transition.get("state_effect"), dict)
                else {}
            )
            cluster_id = (
                str(suggestion_contract.get("query_state_owner") or "")
                or str(transition_effect.get("query_state_owner") or "").split(":", 1)[0]
                or general_page_id
            )
            cluster_id = cluster_id.split(":", 1)[0]
            rows = _hint_rows_for_projection(projection, transition, query_value=query_value)
            for row in _runtime_autocomplete_observation_rows(
                runtime_state_observations,
                page_cluster_id=cluster_id,
                action_id=action_id,
                query_value=query_value,
            ):
                if not any(existing["title"] == row["title"] for existing in rows):
                    rows.append(row)
            if not rows:
                if (
                    _target_identity_hints(transition)
                    or (
                        isinstance(transition_effect, dict)
                        and _target_identity_hints(transition_effect)
                    )
                    or suggestion_contract.get("expected_titles")
                ):
                    suggestion_contract["suppressed_identity_hint_autocomplete_projection"] = True
                    suggestion_contract["suppression_reason"] = (
                        "target_identity_hints_and_expected_titles_are_not_visible_autocomplete_rows"
                    )
                continue
            titles = _dedupe([row["title"] for row in rows], limit=16)
            suggestion_contract["suggestions"] = rows[:16]
            suggestion_contract["expected_titles"] = titles
            suggestion_contract["expected_candidate_count"] = len(rows[:16])
            suggestion_contract.setdefault("min_visible_count", min(len(rows[:16]), 8))
            suggestion_contract["candidate_row_source"] = "trace_evidence_materialized"
            page_rows += len(rows[:16])
            materialized_count += len(rows[:16])
        if page_rows:
            pages.append({"filename": str(filename), "candidate_row_count": page_rows})
    return {
        "format": "autocomplete_suggestion_materialization",
        "schema_revision": 1,
        "materialized_count": materialized_count,
        "page_count": len(pages),
        "pages": pages,
    }


def _query_target_identity_status(target: str, query: str, identity_hints: Sequence[str]) -> str:
    query_terms = _identity_terms(query)
    if not query_terms:
        return "unknown"
    hint_terms: List[str] = []
    for hint in identity_hints:
        hint_terms.extend(_identity_terms(hint))
    if hint_terms:
        for query_term in query_terms:
            for hint_term in hint_terms:
                if query_term == hint_term or query_term in hint_term or hint_term in query_term:
                    return "match"
        return "mismatch"


    filename_terms = _slug_tokens(target)
    for query_term in query_terms:
        if query_term in filename_terms:
            return "match"
        if any(query_term in token or token in query_term for token in filename_terms):
            return "match"
    if _filename_identity_tokens(target):
        return "unknown"
    return "match"


def _coverage(covered: int, total: int) -> float:
    if total <= 0:
        return 1.0
    return round(float(covered) / float(total), 4)


def _is_l0_transition(item: Dict[str, Any]) -> bool:
    if not isinstance(item, dict) or str(item.get("l0_role") or "") != "execution_anchor":
        return False
    return bool(
        _base_filename(item.get("source_filename"))
        or _base_filename(item.get("target_filename"))
        or _base_filename(item.get("filename"))
        or _base_filename(item.get("page_filename"))
    )


def _query_transition_kind(item: Dict[str, Any]) -> str:
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    effect_name = str(effect.get("effect") or "")
    effect_kind = str(effect.get("query_transition_kind") or "")
    field_role = str(effect.get("field_role") or "")
    if (
        effect_name == "project_suggestions"
        or field_role in {"address_lookup", "location_lookup"}
        or effect_kind == "suggestion_projection"
    ):
        return "suggestion_projection"
    kind = str(item.get("query_transition_kind") or effect_kind or "")
    return kind if kind in QUERY_TRANSITION_KINDS else "unknown"


def _query_identity_status(item: Dict[str, Any]) -> str:
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    status = str(item.get("target_identity_status") or effect.get("target_identity_status") or "")
    return status if status in QUERY_IDENTITY_STATUSES else "unknown"


def _has_applied_query_override(identity_hints: Sequence[str]) -> bool:
    for hint in identity_hints:
        text = str(hint or "").strip().lower()
        if text.startswith(("query_applied:", "applied_query:", "selected_suggestion:", "selected_query:")):
            return True
    return False


def _has_selected_result_binding(identity_hints: Sequence[str]) -> bool:
    for hint in identity_hints:
        text = str(hint or "").strip().lower()
        if text.startswith(("selected_result:", "selected_record:", "clicked_result:", "record_identity:")):
            return True
    return False


def _has_fuzzy_query_binding(identity_hints: Sequence[str]) -> bool:
    for hint in identity_hints:
        text = str(hint or "").strip().lower()
        if text.startswith(("possible_fuzzy_match:", "fuzzy_match:", "normalized_query_match:")):
            return True
    return False


def _is_mixed_identity_promo_query(item: Dict[str, Any], identity_hints: Sequence[str]) -> bool:
    reason_code = str(item.get("reason_code") or "").strip().lower()
    if reason_code in {
        "query_committed_to_mixed_identity_promo_page",
        "query_committed_to_promotional_mixed_identity_page",
        "mixed_identity_promo_page",
        "query_scoped_to_single_brand_but_target_is_multi_brand_promo_page",
    }:
        return True
    if "mixed" in reason_code and ("promo" in reason_code or "flash" in reason_code):
        return True
    if "multi_brand" in reason_code and ("promo" in reason_code or "flash" in reason_code):
        return True
    hint_text = " ".join(str(hint or "").strip().lower() for hint in identity_hints)
    result_text = str(item.get("result_projection") or "").strip().lower()
    combined_text = f"{hint_text} {result_text}"
    brand_hint_count = sum(
        1 for hint in identity_hints
        if str(hint or "").strip().lower().startswith(("brand:", "featured_brand:", "featured_merchant:"))
    )
    if (
        brand_hint_count > 1
        and ("promotion_group:" in combined_text or "promo" in combined_text or "flash_deals" in combined_text or "flash deals" in combined_text)
        and ("multi-brand" in combined_text or "multi brand" in combined_text or "multi_brand" in combined_text)
    ):
        return True
    return bool(
        ("mixed_brands:" in combined_text or "other_brands:" in combined_text or "unrelated brands" in combined_text or "multi-brand" in combined_text or "multi brand" in combined_text)
        and ("promotion_group:" in combined_text or "promo" in combined_text or "flash_deals" in combined_text or "flash deals" in combined_text)
        and ("featured_merchant:" in combined_text or "featured_brand:" in combined_text or "products prominent" in combined_text or brand_hint_count > 1)
    )


def _normalize_query_identity_assessment(
    item: Dict[str, Any],
    kind: str,
    status: str,
    identity_hints: Sequence[str],
) -> Tuple[str, str]:
    if status == "mismatch" and _has_applied_query_override(identity_hints):
        return "selected_suggestion_commit", "match"
    if status == "mismatch" and _has_selected_result_binding(identity_hints):
        return kind, "match"
    if status == "mismatch" and _has_fuzzy_query_binding(identity_hints):
        return kind, "match"
    if status == "mismatch" and _is_mixed_identity_promo_query(item, identity_hints):
        return "unrelated_navigation", "not_applicable"
    return kind, status


def _query_identity_required(target: str, kind: str) -> bool:
    if kind in {"query_entry", "suggestion_projection", "unrelated_navigation"}:
        return False
    if not target:
        return False
    if kind == "unknown" and _is_generic_search_target(target):
        return False
    return True


def _is_draft_only_query_transition(transition: Dict[str, Any]) -> bool:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    return (
        str(effect.get("effect") or "") == "update_draft"
        and str(effect.get("field_role") or "") in SEARCHLIKE_FIELD_ROLES
        and _base_filename(transition.get("source_filename")) == _base_filename(transition.get("target_filename"))
    )


def _normalize_draft_only_query_transition(transition: Dict[str, Any]) -> None:
    if not _is_draft_only_query_transition(transition):
        return
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    owner = str(
        effect.get("query_state_owner")
        or effect.get("draft_ref")
        or transition.get("source_filename")
        or transition.get("target_filename")
        or "query"
    )
    transition["query_transition_kind"] = "query_entry"
    transition["query_state_owner"] = owner
    transition["target_identity_status"] = "not_applicable"
    transition["result_projection"] = (
        str(transition.get("result_projection") or "")
        or f"Draft {effect.get('field_role') or 'query'} state updates the same search surface; no committed result identity is required yet."
    )
    effect["query_transition_kind"] = "query_entry"
    effect["query_state_owner"] = owner
    effect["target_identity_status"] = "not_applicable"
    effect["result_projection"] = (
        str(effect.get("result_projection") or "")
        or f"Draft {effect.get('field_role') or 'query'} state updates the same search surface; no committed result identity is required yet."
    )


def _normalize_suggestion_projection_preview_transition(transition: Dict[str, Any]) -> None:
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    reducer = str(transition.get("reducer") or "")
    query_kind = str(transition.get("query_transition_kind") or effect.get("query_transition_kind") or "")
    if reducer != "project_suggestions" and query_kind != "suggestion_projection":
        return
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    if not source or not target or source == target:
        return
    effect["query_transition_kind"] = "suggestion_projection"
    effect.setdefault("preview_target_filename", target)
    if _base_filename(effect.get("target_filename")) == target:
        effect.pop("target_filename", None)
    transition["query_transition_kind"] = "suggestion_projection"
    transition["target_filename"] = source
    transition.setdefault("preview_target_filename", target)
    route_oracle = transition.get("route_oracle") if isinstance(transition.get("route_oracle"), dict) else {}
    if route_oracle:
        expected = _base_filename(
            route_oracle.get("expected_filename")
            or route_oracle.get("target_filename")
            or route_oracle.get("filename")
        )
        if expected == target:
            route_oracle.setdefault("preview_expected_filename", target)
            if "expected_filename" in route_oracle:
                route_oracle["expected_filename"] = source
            if "target_filename" in route_oracle:
                route_oracle["target_filename"] = source
    transition["state_effect"] = effect


def _transition_equivalence_key(item: Dict[str, Any]) -> Tuple[str, ...]:
    return (
        str(item.get("step_key") or item.get("transition_id") or ""),
        str(item.get("source_filename") or ""),
        str(item.get("target_filename") or ""),
        str(item.get("operation") or ""),
        str(item.get("reducer") or ""),
        str(item.get("input_text") or ""),
    )


def _projection_step_keys(page_env: Dict[str, Any]) -> set[str]:
    components = page_env.get("components", []) if isinstance(page_env.get("components"), list) else []
    projections = page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []
    component_steps: Dict[str, set[str]] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        cid = str(component.get("component_id") or "")
        steps = set(str(item) for item in component.get("source_step_keys", []) if str(item or ""))
        steps.update(str(item) for item in component.get("l0_transition_ids", []) if str(item or ""))
        if cid and steps:
            component_steps[cid] = steps
    projection_steps: set[str] = set()
    for projection in projections:
        if not isinstance(projection, dict):
            continue
        projection_steps.update(str(item) for item in projection.get("source_step_keys", []) if str(item or ""))
        projection_steps.update(str(item) for item in projection.get("l0_transition_ids", []) if str(item or ""))
        projection_steps.update(component_steps.get(str(projection.get("component_ref") or ""), set()))
    return projection_steps


def _interactive_projection_step_keys(page_env: Dict[str, Any], findings: List[Dict[str, Any]], filename: str) -> set[str]:
    components = page_env.get("components", []) if isinstance(page_env.get("components"), list) else []
    projections = page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []
    component_by_id = {
        str(component.get("component_id") or ""): component
        for component in components
        if isinstance(component, dict) and str(component.get("component_id") or "")
    }
    covered: set[str] = set()
    for projection in projections:
        if not isinstance(projection, dict) or _is_contract_closure_item(projection):
            continue
        component_ref = str(projection.get("component_ref") or "")
        component = component_by_id.get(component_ref, {})
        if projection.get("interaction_required") is not True and (
            not isinstance(component, dict) or component.get("interaction_required") is not True
        ):
            continue
        dom_contract = projection.get("dom_contract") if isinstance(projection.get("dom_contract"), dict) else {}
        if not dom_contract and isinstance(component, dict):
            dom_contract = component.get("dom_contract") if isinstance(component.get("dom_contract"), dict) else {}
        required_behavior = str(dom_contract.get("required_behavior") or "")
        projection_runtime_visible = (
            str(projection.get("render_phase") or "") == "runtime"
            and str(projection.get("runtime_visibility_policy") or "") == "visible_when_state_active"
        )
        component_runtime_visible = (
            isinstance(component, dict)
            and str(component.get("render_phase") or "") == "runtime"
            and str(component.get("runtime_visibility_policy") or "") == "visible_when_state_active"
        )
        projection_scroll_reachable = (
            required_behavior == "scroll_reachable_actionable_control"
            and projection.get("initial_dom_eligible") is True
            and str(projection.get("initial_visibility_policy") or "") == "scroll_reachable_initial"
            and str(projection.get("runtime_visibility_policy") or "") == "visible_after_scroll"
        )
        component_scroll_reachable = (
            required_behavior == "scroll_reachable_actionable_control"
            and isinstance(component, dict)
            and component.get("initial_dom_eligible") is True
            and str(component.get("initial_visibility_policy") or "") == "scroll_reachable_initial"
            and str(component.get("runtime_visibility_policy") or "") == "visible_after_scroll"
        )
        actionable_when_visible = required_behavior == "visible_actionable_control" or (
            required_behavior == "initially_hidden_actionable_control"
            and (projection_runtime_visible or component_runtime_visible)
        ) or projection_scroll_reachable or component_scroll_reachable
        if not actionable_when_visible:
            findings.append({
                "severity": "error",
                "code": "l0_interactive_projection_dom_contract_missing",
                "filename": filename,
                "projection_id": projection.get("projection_id", ""),
                "component_ref": component_ref,
                "message": "L0 interactive projection must declare a lifecycle-consistent actionable DOM contract.",
            })
            continue
        covered.update(str(item) for item in projection.get("source_step_keys", []) if str(item or ""))
        covered.update(str(item) for item in projection.get("l0_transition_ids", []) if str(item or ""))
        if isinstance(component, dict):
            covered.update(str(item) for item in component.get("source_step_keys", []) if str(item or ""))
            covered.update(str(item) for item in component.get("l0_transition_ids", []) if str(item or ""))
    return covered


def _route_affordance_coverage(page_env: Dict[str, Any], findings: List[Dict[str, Any]], filename: str) -> Tuple[int, int]:
    components = page_env.get("components", []) if isinstance(page_env.get("components"), list) else []
    projections = page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []
    oracles = page_env.get("oracles", []) if isinstance(page_env.get("oracles"), list) else []
    route_components = [
        component for component in components
        if isinstance(component, dict)
        and str(component.get("component_role") or "") == "route_navigation_affordance"
    ]
    projections_by_component: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for projection in projections:
        if isinstance(projection, dict):
            component_ref = str(projection.get("component_ref") or "")
            if component_ref:
                projections_by_component[component_ref].append(projection)
    oracle_types_by_component: Dict[str, set[str]] = defaultdict(set)
    for oracle in oracles:
        if isinstance(oracle, dict):
            component_ref = str(oracle.get("component_ref") or "")
            oracle_type = str(oracle.get("oracle_type") or "")
            if component_ref and oracle_type:
                oracle_types_by_component[component_ref].add(oracle_type)
    covered = 0
    for component in route_components:
        component_id = str(component.get("component_id") or "")
        missing_identity_fields = [
            key for key in (
                "source_page_id",
                "source_general_page_id",
                "source_materialized_page_id",
                "target_page_id",
                "target_general_page_id",
                "target_materialized_page_id",
            )
            if not str(component.get(key) or "").strip()
        ]
        if missing_identity_fields:
            findings.append({
                "severity": "error",
                "code": "route_navigation_affordance_identity_incomplete",
                "filename": filename,
                "component_id": component_id,
                "display_label": component.get("display_label", ""),
                "missing_identity_fields": missing_identity_fields,
                "message": "Route/navigation affordance must carry source and target materialized identity, not only a target filename.",
            })
        oracle_types = oracle_types_by_component.get(component_id, set())
        has_actionable_projection = any(
            isinstance(projection, dict)
            and projection.get("interaction_required") is True
            and str(
                (
                    projection.get("dom_contract")
                    if isinstance(projection.get("dom_contract"), dict)
                    else component.get("dom_contract")
                    if isinstance(component.get("dom_contract"), dict)
                    else {}
                ).get("required_behavior")
                or ""
            ) == "visible_actionable_control"
            for projection in projections_by_component.get(component_id, [])
        )
        ok = (
            component.get("interaction_required") is True
            and not missing_identity_fields
            and has_actionable_projection
            and {"component_interactable", "route_target"}.issubset(oracle_types)
        )
        if ok:
            covered += 1
        else:
            findings.append({
                "severity": "error",
                "code": "route_navigation_affordance_projection_incomplete",
                "filename": filename,
                "component_id": component_id,
                "display_label": component.get("display_label", ""),
                "message": "Route/navigation affordance must compile to interactive component, render projection, and executable route oracle.",
            })
    return len(route_components), covered


def _is_contract_closure_item(item: Dict[str, Any]) -> bool:
    return isinstance(item, dict) and str(item.get("source") or "") == "page_environment_contract_closure"


def _component_ids(page_env: Dict[str, Any]) -> set[str]:
    components = page_env.get("components", []) if isinstance(page_env.get("components"), list) else []
    return {
        str(component.get("component_id") or "")
        for component in components
        if isinstance(component, dict) and str(component.get("component_id") or "")
    }


def _declared_state_refs(page_env: Dict[str, Any]) -> set[str]:
    refs: set[str] = {"state::page_runtime"}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"state_ref", "reads_state", "draft_ref", "query_state_owner", "collection_state_ref", "collection_ref"}:
                    refs.update(_state_ref_tokens(child))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for section_key in ("initial_state", "state_schema", "content_seed"):
        section = page_env.get(section_key)
        if isinstance(section, (dict, list)):
            visit(section)
    return refs


def _semantic_projection_step_keys(page_env: Dict[str, Any], findings: List[Dict[str, Any]], filename: str) -> set[str]:
    components = page_env.get("components", []) if isinstance(page_env.get("components"), list) else []
    projections = page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []
    component_steps: Dict[str, set[str]] = {}
    component_ids = _component_ids(page_env)
    declared_state_refs = _declared_state_refs(page_env)
    for component in components:
        if not isinstance(component, dict):
            continue
        if _is_contract_closure_item(component):
            continue
        cid = str(component.get("component_id") or "")
        if not cid:
            continue
        steps = set(str(item) for item in component.get("source_step_keys", []) if str(item or ""))
        steps.update(str(item) for item in component.get("l0_transition_ids", []) if str(item or ""))
        if steps:
            component_steps[cid] = steps
    projection_steps: set[str] = set()
    for projection in projections:
        if not isinstance(projection, dict):
            continue
        component_ref = str(projection.get("component_ref") or "")
        reads_state = str(projection.get("reads_state") or "")
        if component_ref and component_ref not in component_ids:
            findings.append({
                "severity": "error",
                "code": "render_projection_component_missing",
                "filename": filename,
                "projection_id": projection.get("projection_id", ""),
                "component_ref": component_ref,
                "message": "Render projection references a component that is not declared in the spec.",
            })
            continue
        missing_state_refs = [
            ref for ref in _state_ref_tokens(reads_state)
            if ref not in declared_state_refs
        ]
        if missing_state_refs:
            findings.append({
                "severity": "error",
                "code": "render_projection_state_ref_missing",
                "filename": filename,
                "projection_id": projection.get("projection_id", ""),
                "reads_state": reads_state,
                "missing_state_refs": missing_state_refs,
                "message": "Render projection reads state that is not declared in initial_state/state_schema/content_seed.",
            })
            continue
        if _is_contract_closure_item(projection):
            continue
        projection_steps.update(str(item) for item in projection.get("source_step_keys", []) if str(item or ""))
        projection_steps.update(str(item) for item in projection.get("l0_transition_ids", []) if str(item or ""))
        projection_steps.update(component_steps.get(component_ref, set()))
    return projection_steps


def _semantic_oracle_step_types(page_env: Dict[str, Any], findings: List[Dict[str, Any]], filename: str) -> Dict[str, set[str]]:
    components = page_env.get("components", []) if isinstance(page_env.get("components"), list) else []
    component_ids = _component_ids(page_env)
    component_l0_steps: Dict[str, set[str]] = {}
    for component in components:
        if not isinstance(component, dict) or _is_contract_closure_item(component):
            continue
        cid = str(component.get("component_id") or "")
        if not cid:
            continue
        steps = set(str(item) for item in component.get("source_step_keys", []) if str(item or ""))
        steps.update(str(item) for item in component.get("l0_transition_ids", []) if str(item or ""))
        if steps:
            component_l0_steps[cid] = steps
    result: Dict[str, set[str]] = {}
    text_only_types = {"text_exists", "required_text_present", "visible_text", "text_presence"}
    oracle_type_aliases = {
        "component_interactive": "component_interactable",
        "state_projection": "state_mutation",
        "navigation": "route_target",
    }
    for oracle in page_env.get("oracles", []) if isinstance(page_env.get("oracles"), list) else []:
        if not isinstance(oracle, dict):
            continue
        step_key = str(oracle.get("step_key") or "")
        oracle_type = str(oracle.get("oracle_type") or "")
        oracle_type = oracle_type_aliases.get(oracle_type, oracle_type)
        component_ref = str(oracle.get("component_ref") or "")
        if component_ref and component_ref not in component_ids:
            findings.append({
                "severity": "error",
                "code": "oracle_component_missing",
                "filename": filename,
                "step_key": step_key,
                "component_ref": component_ref,
                "message": "Oracle references a component that is not declared in the spec.",
            })
            continue
        if _is_contract_closure_item(oracle) or oracle_type in text_only_types:
            continue
        if oracle_type:
            target_steps = set()
            if step_key:
                target_steps.add(step_key)
            target_steps.update(component_l0_steps.get(component_ref, set()))
            for target_step in target_steps:
                if target_step:
                    result.setdefault(target_step, set()).add(oracle_type)
    return result


def _semantic_l0_reducer_valid(transition: Dict[str, Any]) -> bool:
    reducer = str(transition.get("reducer") or "")
    operation = str(transition.get("operation") or "")
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    if not reducer or not operation:
        return False
    if str(effect.get("effect") or "") == "invalid_generic_commit_without_state":
        return False
    if _is_closed_state_transaction(transition):
        return True
    if operation in {"fill", "type", "input"}:
        return bool(transition.get("input_text")) and bool(effect) and effect.get("visible_initial_prefill_allowed") is False
    if reducer in {"activate_inline_state", "activate_control"}:
        effect_name = str(effect.get("effect") or "")
        if effect_name in {"activate_inline_state", "commit_visible_state"}:
            return bool(
                effect.get("state_ref")
                or effect.get("target_filename")
                or effect.get("mutation_policy")
                or effect.get("state_commit_policy")
            )
    if reducer in {
        "navigate_route",
        "open_search_entry",
        "project_suggestions",
        "open_overlay",
        "open_option_modal",
        "select_overlay_option",
        "close_overlay",
        "confirm_overlay",
        "select_filter_option",
        "apply_filter",
        "switch_tab",
        "update_cart_quantity",
        "add_to_cart",
        "remove_from_cart",
    } and str(effect.get("effect") or ""):
        return True
    if target and target != source:
        return True
    if operation in {"scroll", "open_app"}:
        return True
    return False


def _initial_value_binding_for_transition(transition: Dict[str, Any]) -> Dict[str, Any]:
    operation = str(transition.get("operation") or "").lower()
    reducer = str(transition.get("reducer") or "").lower()
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    query_kind = str(effect.get("query_transition_kind") or transition.get("query_transition_kind") or "")
    field_role = str(effect.get("field_role") or transition.get("field_role") or "")
    input_text = str(transition.get("input_text") or "")
    if (
        input_text
        and operation in {"fill", "type", "input"}
        and (
            reducer in {"type_search", "project_suggestions"}
            or field_role == "business_query"
            or query_kind in {"query_entry", "suggestion_projection"}
        )
    ):
        return {
            "host_availability_phase": "initial",
            "initial_value_source": "initial_state.query.default",
            "post_action_value": input_text,
            "post_action_state_effect": "update_query_draft",
            "forbid_initial_value_from_input_text": True,
        }
    if input_text and effect.get("visible_initial_prefill_allowed") is False:
        return {
            "host_availability_phase": "initial",
            "initial_value_source": "empty_or_spec_initial_state",
            "post_action_value": input_text,
            "post_action_state_effect": "update_draft",
            "forbid_initial_value_from_input_text": True,
        }
    return {}


def _transition_value_lifecycle(transition: Dict[str, Any]) -> Dict[str, Any]:
    binding = _initial_value_binding_for_transition(transition)
    if not binding:
        return {}
    return {
        "host_availability_phase": binding["host_availability_phase"],
        "state_value_phase": "post_action",
        "content_projection_phase": "post_transition",
        "initial_value_source": binding["initial_value_source"],
        "post_action_value": binding["post_action_value"],
        "forbid_initial_value_from_input_text": True,
    }


def _source_host_requires_runtime_visibility(
    transition: Dict[str, Any],
    *,
    step_render_role: str = "",
) -> bool:
    if step_render_role in {
        "runtime_pre_action_state",
        "interaction_state_change",
        "post_interaction_overlay",
    }:
        return True
    reducer = str(transition.get("reducer") or "")
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    effect_name = str(effect.get("effect") or "")
    if reducer in {"select_overlay_option", "confirm_overlay", "close_overlay"}:
        return True
    if effect_name in {"confirm_runtime_overlay", "close_runtime_overlay"}:
        return True
    intent = str(transition.get("operation_intent") or transition.get("command") or "")
    if reducer == "navigate_route" and any(token in intent for token in ("弹窗", "提示", "授权")):
        return any(token in intent for token in ("确认", "确定", "允许", "同意"))
    return False


def _transition_requires_component_interactable_oracle(transition: Dict[str, Any]) -> bool:
    operation = str(transition.get("operation") or "").lower()
    reducer = str(transition.get("reducer") or "").lower()
    command = str(transition.get("command") or "").lower()
    l0_role = str(transition.get("l0_role") or "").lower()
    component_ref = str(
        transition.get("component_ref")
        or transition.get("action_component_ref")
        or transition.get("source_component_ref")
        or ""
    )
    if component_ref:
        return True
    if transition.get("dom_binding_required") is True:
        return True
    if operation in {"click", "tap", "press", "fill", "type", "input", "select", "check", "uncheck"}:
        return True
    if reducer in {"open_overlay", "open_option_modal", "select_overlay_option", "close_overlay", "confirm_overlay"}:
        return True
    if reducer in {"project_suggestions", "filter_selection", "select_filter_option", "apply_filter", "switch_tab"}:
        return True
    if reducer in {"update_cart_quantity", "add_to_cart", "remove_from_cart"}:
        return True
    if operation in {"swipe", "scroll", "drag"} or command in {"swipe", "scroll", "drag"} or l0_role == "viewport_state":
        return False
    return True


def _required_oracle_types(transition: Dict[str, Any]) -> set[str]:
    if str(transition.get("direction") or "source") == "target":
        return set()
    source = _base_filename(transition.get("source_filename"))
    target = _base_filename(transition.get("target_filename"))
    effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
    required = set()
    if _transition_requires_component_interactable_oracle(transition):
        required.add("component_interactable")
    if target and target != source:
        required.add("route_target")
    if str(effect.get("effect") or ""):
        required.add("state_mutation")
    reducer = str(transition.get("reducer") or "")
    if reducer == "project_suggestions":
        required.add("query_suggestion_projection")
    if reducer in {"open_overlay", "open_option_modal"}:
        required.add("modal_layout_within_app_shell")
    if reducer in {"filter_selection", "select_filter_option", "apply_filter", "switch_tab"}:
        required.add("stateful_component_projection")
    if reducer in {"update_cart_quantity", "add_to_cart", "remove_from_cart"}:
        required.add("stateful_component_projection")
        required.add("cart_projection_consistency")
    if isinstance(effect.get("selected_record_hydration"), dict):
        required.add("selected_record_hydration")
    if _effect_requires_record_identity(effect):
        required.add("collection_preservation")
    return required


def _runtime_initial_leak_findings(page_env: Dict[str, Any], filename: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for projection in page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []:
        if not isinstance(projection, dict):
            continue
        lifecycle = str(projection.get("lifecycle") or projection.get("state_lifecycle") or "")
        phase = str(projection.get("render_phase") or projection.get("phase") or "")
        if lifecycle == "runtime_only" and phase in {"", "initial", "default"}:
            findings.append({
                "severity": "error",
                "code": "runtime_only_projection_in_initial_state",
                "filename": filename,
                "projection_id": projection.get("projection_id", ""),
                "message": "Runtime-only content is projected into the initial/default render phase.",
            })
    return findings


def _projection_query_norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _query_projection_scope_findings(page_env: Dict[str, Any], filename: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    findings: List[Dict[str, Any]] = []
    projections = [
        item for item in page_env.get("query_projections", [])
        if isinstance(item, dict)
    ] if isinstance(page_env.get("query_projections"), list) else []
    collapse_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    missing_scoped_suggestions = 0
    for projection in projections:
        query = str(projection.get("query_value") or "").strip()
        kind = str(projection.get("query_transition_kind") or "")
        status = str(projection.get("target_identity_status") or "")
        target = _base_filename(projection.get("submit_target_filename") or projection.get("target_filename"))
        if not query:
            findings.append({
                "severity": "error",
                "code": "query_projection_missing_query_value",
                "filename": filename,
                "message": "Query projection is not keyed by a concrete query value.",
            })
            continue
        if kind in {"query_entry", "suggestion_projection"}:
            if not projection.get("query_state_owner"):
                missing_scoped_suggestions += 1
                findings.append({
                    "severity": "error",
                    "code": "query_projection_missing_scoped_suggestions",
                    "filename": filename,
                    "query_value": query,
                    "message": "Runtime suggestion projection must be scoped by query state owner.",
                })
            continue
        if kind == "unrelated_navigation" or status == "not_applicable":
            continue
        if str(projection.get("field_role") or "business_query") != "business_query":
            continue
        if target and not _is_generic_search_target(target):
            collapse_groups[target].append(projection)
    for target, group in collapse_groups.items():
        query_norms = {_projection_query_norm(item.get("query_value")) for item in group}
        query_norms.discard("")
        if len(query_norms) <= 1:
            continue
        matched_count = sum(1 for item in group if str(item.get("target_identity_status") or "") == "match")
        if matched_count < len(query_norms):
            findings.append({
                "severity": "error",
                "code": "query_values_collapsed_to_single_target",
                "filename": filename,
                "target_filename": target,
                "query_values": _dedupe([str(item.get("query_value") or "") for item in group], limit=12),
                "message": "Distinct query values collapse to one non-generic target without per-query identity justification.",
            })
    return findings, {
        "query_projection_count": len(projections),
        "query_projection_missing_scoped_suggestions": missing_scoped_suggestions,
    }


def _semantic_terms_overlap(left: Any, right: Any) -> bool:
    left_terms = set(_identity_terms(left))
    right_terms = set(_identity_terms(right))
    left_terms.discard("")
    right_terms.discard("")
    if not left_terms or not right_terms:
        return True
    for left_term in left_terms:
        for right_term in right_terms:
            if left_term == right_term or left_term in right_term or right_term in left_term:
                return True
    if _shared_non_generic_cjk_identity(left_terms, right_terms):
        return True
    return False


def _shared_non_generic_cjk_identity(left_terms: set[str], right_terms: set[str]) -> bool:
    generic_terms = {
        "首页",
        "页面",
        "图标",
        "入口",
        "频道",
        "按钮",
        "左上",
        "右上",
        "左下",
        "右下",
        "上角",
        "下角",
        "列表",
        "搜索",
        "结果",
        "详情",
        "选择",
        "分类",
    }

    def fragments(terms: set[str]) -> set[str]:
        out: set[str] = set()
        for term in terms:
            for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", term):
                max_len = min(len(chunk), 8)
                for size in range(2, max_len + 1):
                    for idx in range(0, len(chunk) - size + 1):
                        piece = chunk[idx:idx + size]
                        if piece in generic_terms:
                            continue
                        out.add(piece)
        return out

    return bool(fragments(left_terms) & fragments(right_terms))


def _has_explicit_non_identity_route_binding(item: Dict[str, Any], label: str) -> bool:
    semantics = item.get("route_binding_semantics")
    if not isinstance(semantics, dict):
        return False
    binding_kind = str(semantics.get("binding_kind") or "")
    if binding_kind == "activation_control":
        return True
    if binding_kind != "route_parameter":
        return False
    binding = semantics.get("parameter_binding")
    if not isinstance(binding, dict):
        return False
    value = str(binding.get("parameter_value") or "").strip()
    return bool(value and _semantic_terms_overlap(value, label))


def _page_scope_coherence_findings(page_env: Dict[str, Any], filename: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    current = _base_filename(filename or page_env.get("filename") or page_env.get("page_id"))
    current_materialized_id = str(
        page_env.get("materialized_page_id")
        or page_env.get("site_model_page_id")
        or page_env.get("page_id")
        or ""
    )

    def _has_materialized_target_change(transition: Dict[str, Any]) -> bool:
        candidates: List[Any] = []
        target_hydration = transition.get("target_hydration")
        if isinstance(target_hydration, dict):
            candidates.append(target_hydration.get("target_filename"))
        post_action_oracle = transition.get("post_action_oracle")
        if isinstance(post_action_oracle, dict):
            candidates.append(post_action_oracle.get("target_filename"))
        interaction_contract = transition.get("interaction_contract")
        if isinstance(interaction_contract, dict):
            candidates.append(interaction_contract.get("expected_filename"))
        expected_interaction = transition.get("expected_interaction")
        if isinstance(expected_interaction, dict):
            candidates.append(expected_interaction.get("expected_filename"))
        state_effect = transition.get("state_effect")
        if isinstance(state_effect, dict):
            candidates.append(state_effect.get("target_filename"))
            selected_record_hydration = state_effect.get("selected_record_hydration")
            if isinstance(selected_record_hydration, dict):
                candidates.append(selected_record_hydration.get("target_filename"))
        return any(
            _base_filename(candidate) and _base_filename(candidate) != current
            for candidate in candidates
        )

    for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
        if not isinstance(transition, dict):
            continue
        direction = str(transition.get("direction") or "source")
        source = _base_filename(transition.get("source_filename"))
        target = _base_filename(transition.get("target_filename"))
        step_key = str(transition.get("step_key") or transition.get("transition_id") or "")
        if direction == "source" and source and source != current:
            findings.append({
                "severity": "error",
                "code": "source_materialized_mismatch",
                "filename": current,
                "step_key": step_key,
                "source_filename": source,
                "message": "Source-bound transition is attached to a different materialized page.",
            })
        if (
            direction == "target"
            and target
            and target != current
            and _query_transition_kind(transition) != "suggestion_projection"
        ):
            findings.append({
                "severity": "error",
                "code": "target_mismatch",
                "filename": current,
                "step_key": step_key,
                "target_filename": target,
                "message": "Target-bound transition is attached to a different materialized page.",
            })
        if (
            source
            and source == target
            and str(transition.get("reducer") or "") == "navigate_route"
            and direction == "source"
            and not _has_materialized_target_change(transition)
        ):
            findings.append({
                "severity": "error",
                "code": "same_page_runtime_step_has_unjustified_navigation",
                "filename": current,
                "step_key": step_key,
                "message": "Same-page runtime step is modeled as route navigation without a materialized target change.",
            })
    for item in list(page_env.get("components", []) if isinstance(page_env.get("components"), list) else []) + list(page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []):
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "") != "page_environment_route_affordance_compiler" and str(item.get("component_role") or "") != "route_navigation_affordance":
            continue
        source_materialized_id = str(item.get("source_materialized_page_id") or "")
        if source_materialized_id and current_materialized_id and source_materialized_id != current_materialized_id:
            findings.append({
                "severity": "error",
                "code": "source_materialized_mismatch",
                "filename": current,
                "source_materialized_page_id": source_materialized_id,
                "current_materialized_page_id": current_materialized_id,
                "display_label": item.get("display_label", ""),
                "message": "Route affordance source materialized id does not match the page spec that owns it.",
            })
    return findings


def _build_page_query_resolution_index(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    by_filename = payload.get("page_environment_specs_by_filename", {})
    if not isinstance(by_filename, dict):
        return index
    for filename, page_env in by_filename.items():
        if not isinstance(page_env, dict):
            continue
        fn = _base_filename(filename)
        resolutions = page_env.get("query_identity_resolutions", [])
        if not isinstance(resolutions, list):
            continue
        for res in resolutions:
            if not isinstance(res, dict):
                continue
            qid = str(res.get("query_id") or "").strip()
            if qid:
                index[qid] = res

            if fn:
                index[f"fn:{fn}"] = res
    return index


def evaluate_page_environment_specs(payload: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    l0_steps: set[str] = set()
    l0_query_checks: List[Dict[str, Any]] = []
    l0_record_mutation_steps: set[str] = set()
    page_query_resolution_index = _build_page_query_resolution_index(payload)
    for spec in payload.get("specs", []) if isinstance(payload, dict) else []:
        if not isinstance(spec, dict):
            continue
        trace_id = str(spec.get("trace_id") or "")
        transitions = spec.get("transitions", []) if isinstance(spec.get("transitions"), list) else []
        input_sessions = spec.get("input_sessions", []) if isinstance(spec.get("input_sessions"), list) else []
        collection_facts = spec.get("collection_facts", []) if isinstance(spec.get("collection_facts"), list) else []
        trace_findings_before = len(findings)

        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
            if _is_l0_transition(transition):
                l0_steps.add(str(transition.get("step_key") or ""))
            input_text = str(transition.get("input_text") or "").strip()
            source = str(transition.get("source_filename") or "")
            target = str(transition.get("target_filename") or "")
            effect_field_role = str(effect.get("field_role") or "")
            if input_text and (effect_field_role == "business_query" or _is_search_surface(source, input_text)):
                kind = _query_transition_kind(transition)
                status = _query_identity_status(transition)
                hints = _target_identity_hints(transition)
                if _is_draft_only_query_transition(transition):
                    kind = "query_entry"
                    status = "not_applicable"
                if status == "unknown":
                    status = _query_target_identity_status(target, input_text, hints)
                if (status == "unknown" or not hints) and page_query_resolution_index:
                    t_qid = str(transition.get("query_id") or "")
                    t_res = page_query_resolution_index.get(t_qid) if t_qid else None
                    if isinstance(t_res, dict):
                        res_kind = str(t_res.get("query_transition_kind") or "").strip()
                        res_status = str(t_res.get("target_identity_status") or "").strip()
                        res_hints = t_res.get("target_identity_hints", [])
                        if res_kind and res_kind != "unknown":
                            kind = res_kind
                        if res_status and res_status != "unknown":
                            status = res_status
                        if isinstance(res_hints, list) and res_hints and not hints:
                            hints = [str(h) for h in res_hints[:16] if h]
                kind, status = _normalize_query_identity_assessment(transition, kind, status, hints)
                check = {
                    "trace_id": trace_id,
                    "step_key": str(transition.get("step_key") or ""),
                    "input_text": input_text,
                    "source_filename": source,
                    "target_filename": target,
                    "query_transition_kind": kind,
                    "target_identity_status": status,
                    "target_identity_hints": hints,
                    "required": status != "not_applicable" and _query_identity_required(target, kind),
                }
                if _is_l0_transition(transition):
                    l0_query_checks.append(check)
                if check["required"] and status == "mismatch":
                    findings.append({
                        "severity": "error",
                        "code": "query_target_mismatch",
                        "trace_id": trace_id,
                        "step_key": transition.get("step_key", ""),
                        "input_text": input_text,
                        "source_filename": source,
                        "target_filename": target,
                        "message": "Search input commits to a target whose identity does not match the query.",
                    })
                elif check["required"] and (status == "unknown" or not hints):
                    findings.append({
                        "severity": "error",
                        "code": "query_target_identity_not_modeled",
                        "trace_id": trace_id,
                        "step_key": transition.get("step_key", ""),
                        "input_text": input_text,
                        "target_filename": target,
                        "message": "L0 search target requires structured query transition kind and target identity hints.",
                    })
            if input_text and effect.get("visible_initial_prefill_allowed") is not False:
                findings.append({
                    "severity": "error",
                    "code": "typed_input_not_parameterized",
                    "trace_id": trace_id,
                    "step_key": transition.get("step_key", ""),
                    "message": "Typed input must be modeled as runtime parameter, not baked into initial DOM.",
                })
            if _is_l0_transition(transition) and _effect_requires_record_identity(effect):
                l0_record_mutation_steps.add(str(transition.get("step_key") or ""))

        committed_sessions = [
            session for session in input_sessions
            if isinstance(session, dict) and session.get("committed_by_step")
        ]
        for session in committed_sessions:
            target = str(session.get("target_filename") or "")
            form_values = [
                str(field.get("value") or "")
                for field in session.get("fields", [])
                if isinstance(field, dict) and str(field.get("field_role") or "") == "form_value"
            ]
            if form_values and not target:
                findings.append({
                    "severity": "error",
                    "code": "form_draft_missing_commit_target",
                    "trace_id": trace_id,
                    "draft_ref": session.get("draft_ref", ""),
                    "message": "Form draft was filled but has no deterministic commit target.",
                })
            query_values = [
                str(field.get("value") or "")
                for field in session.get("fields", [])
                if isinstance(field, dict) and str(field.get("field_role") or "") == "business_query"
            ]
            query_values = _dedupe(query_values, limit=16)
            if query_values and target and not _is_generic_search_target(target):
                for query in query_values:
                    kind = _query_transition_kind(session)
                    status = _query_identity_status(session)
                    hints = _target_identity_hints(session)
                    if status == "unknown":
                        status = _query_target_identity_status(target, query, hints)


                        if status == "match" and not hints:
                            filename_tokens = _filename_identity_tokens(target)
                            query_terms = _identity_terms(query)
                            derived_hints: List[str] = []
                            for token in filename_tokens:
                                for query_term in query_terms:
                                    if (
                                        token == query_term
                                        or token in query_term
                                        or query_term in token
                                    ):
                                        derived_hints.append(f"derived_from_filename:{token}")
                                        break
                            if derived_hints:
                                hints = _dedupe(derived_hints, limit=16)
                        if status == "unknown" or not hints:
                            page_res = page_query_resolution_index.get(
                                str(session.get("query_id") or "")
                            )
                            if not page_res:
                                page_res = page_query_resolution_index.get(f"fn:{_base_filename(target)}")
                            if isinstance(page_res, dict):
                                res_kind = str(page_res.get("query_transition_kind") or "").strip()
                                res_status = str(page_res.get("target_identity_status") or "").strip()
                                res_hints = page_res.get("target_identity_hints", [])
                                if res_kind and res_kind != "unknown":
                                    kind = res_kind
                                if res_status and res_status != "unknown":
                                    status = res_status
                                if isinstance(res_hints, list) and res_hints and not hints:
                                    hints = [str(h) for h in res_hints[:16] if h]
                    kind, status = _normalize_query_identity_assessment(session, kind, status, hints)
                    check = {
                        "trace_id": trace_id,
                        "step_key": str(session.get("committed_by_step") or ""),
                        "input_text": query,
                        "source_filename": str(session.get("source_filename") or ""),
                        "target_filename": target,
                        "query_transition_kind": kind,
                        "target_identity_status": status,
                        "target_identity_hints": hints,
                        "required": status != "not_applicable" and _query_identity_required(target, kind),
                    }
                    l0_query_checks.append(check)
                    if query and check["required"] and status == "mismatch":
                        findings.append({
                            "severity": "error",
                            "code": "query_commit_target_mismatch",
                            "trace_id": trace_id,
                            "draft_ref": session.get("draft_ref", ""),
                            "committed_by_step": session.get("committed_by_step", ""),
                            "input_text": query,
                            "target_filename": target,
                            "message": "Committed query target identity does not match the typed query.",
                        })
                    elif query and check["required"] and (status == "unknown" or not hints):
                        findings.append({
                            "severity": "error",
                            "code": "query_commit_target_identity_not_modeled",
                            "trace_id": trace_id,
                            "draft_ref": session.get("draft_ref", ""),
                            "committed_by_step": session.get("committed_by_step", ""),
                            "input_text": query,
                            "target_filename": target,
                            "message": "L0 committed query target requires structured query transition kind and target identity hints.",
                        })

        commit_surfaces = {
            _base_filename(session.get("target_filename"))
            for session in input_sessions
            if isinstance(session, dict) and session.get("committed_by_step")
        }
        commit_surfaces.update(
            _base_filename(session.get("source_filename"))
            for session in input_sessions
            if isinstance(session, dict) and session.get("committed_by_step")
        )
        commit_surfaces.discard("")
        baseline_record_facts = [
            fact for fact in collection_facts
            if isinstance(fact, dict)
            and _is_record_collection_scope(str(fact.get("state_scope") or ""))
            and bool(fact.get("baseline_evidence_present"))
            and (
                not commit_surfaces
                or _base_filename(fact.get("filename")) in commit_surfaces
            )
        ]
        has_collection_preserve = any(
            int(fact.get("min_initial_count") or 0) > 0
            or bool(fact.get("preserve_unknown_initial_records"))
            for fact in baseline_record_facts
        )
        has_any_collection_fact = bool(baseline_record_facts)
        has_form_commit = any(
            isinstance(session, dict)
            and any(isinstance(field, dict) and field.get("field_role") == "form_value" for field in session.get("fields", []))
            and session.get("committed_by_step")
            for session in input_sessions
        )
        if has_form_commit and has_any_collection_fact and not has_collection_preserve:
            findings.append({
                "severity": "warning",
                "code": "collection_baseline_count_not_modeled",
                "trace_id": trace_id,
                "message": "A form commit writes into a collection, but the preserved initial collection count is not explicit.",
            })

        summaries.append({
            "trace_id": trace_id,
            "page_count": len(spec.get("pages", []) if isinstance(spec.get("pages"), list) else []),
            "transition_count": len(transitions),
            "input_session_count": len(input_sessions),
            "collection_fact_count": len(collection_facts),
            "finding_count": len(findings) - trace_findings_before,
        })

    page_envs = payload.get("page_environment_specs_by_filename", {}) if isinstance(payload, dict) else {}
    page_summaries: List[Dict[str, Any]] = []
    l0_reducer_steps: set[str] = set()
    l0_oracle_steps: set[str] = set()
    l0_render_projection_steps: set[str] = set()
    l0_interactive_required_steps: set[str] = set()
    l0_interactive_projection_steps: set[str] = set()
    route_affordance_count = 0
    route_affordance_covered_count = 0
    record_identity_steps: set[str] = set()
    structural_closure_l0_steps: set[str] = set()
    duplicate_equivalent_transition_count = 0
    unresolved_fixed_slot_conflicts = 0
    runtime_initial_leak_count = 0
    pagegen_legacy_context_dependency = 0
    query_projection_count = 0
    query_projection_missing_scoped_suggestions = 0
    generic_commit_without_state_count = 0
    transition_identity_missing_count = 0
    conflict_resolution = payload.get("conflict_resolution", {}) if isinstance(payload, dict) else {}
    route_materialization = payload.get("canonical_route_materialization", {}) if isinstance(payload, dict) else {}
    route_materialization_summary = {}
    if isinstance(route_materialization, dict) and route_materialization:
        route_materialization_summary = {
            "policy": str(route_materialization.get("blocking_policy") or ""),
            "canonical_route_count": int(route_materialization.get("canonical_route_count") or 0),
            "cluster_admitted_count": int(route_materialization.get("cluster_admitted_count") or 0),
            "materialized_admitted_count": int(route_materialization.get("materialized_admitted_count") or 0),
            "conflict_count": int(route_materialization.get("conflict_count") or 0),
            "split_general_page_count": len(route_materialization.get("split_general_page_ids", []))
            if isinstance(route_materialization.get("split_general_page_ids"), list)
            else 0,
        }
        if (
            route_materialization_summary["split_general_page_count"] > 0
            and route_materialization_summary["materialized_admitted_count"] == 0
        ):
            findings.append({
                "severity": "warning",
                "code": "route_materialization_no_materialized_routes",
                "message": "Materialized split pages exist, but no canonical routes were materialized from evidence steps.",
            })
    groups_by_filename = (
        conflict_resolution.get("groups", {}).get("groups_by_filename", {})
        if isinstance(conflict_resolution, dict) and isinstance(conflict_resolution.get("groups"), dict)
        else {}
    )


    environment_action_route_plan = (
        payload.get("environment_action_route_plan", {})
        if isinstance(payload, dict)
        else {}
    )
    has_phase5_route_plan = (
        isinstance(environment_action_route_plan, dict)
        and bool(environment_action_route_plan)
    )
    semantic_enrichment_meta = (
        payload.get("semantic_enrichment", {})
        if isinstance(payload, dict)
        else {}
    )
    has_enrich_phase_3 = (
        isinstance(semantic_enrichment_meta, dict)
        and isinstance(semantic_enrichment_meta.get("phase_3_cross_page_decisions"), dict)
        and bool(semantic_enrichment_meta["phase_3_cross_page_decisions"].get("enabled"))
    )
    has_per_page_conflict_resolutions = isinstance(page_envs, dict) and any(
        isinstance(page_env, dict)
        and isinstance(page_env.get("conflict_resolutions"), list)
        and page_env.get("conflict_resolutions")
        for page_env in page_envs.values()
    )
    conflict_groups_generated = (
        (isinstance(conflict_resolution, dict) and bool(conflict_resolution))
        or has_phase5_route_plan
        or has_enrich_phase_3
        or has_per_page_conflict_resolutions
    )
    if isinstance(page_envs, dict) and page_envs and not conflict_groups_generated:
        findings.append({
            "severity": "error",
            "code": "page_conflict_groups_not_generated",
            "message": "Spec evaluation cannot prove layout/content conflicts were resolved because conflict groups were not generated.",
        })
    page_ids_by_filename = {
        _base_filename(filename): str(page_env.get("site_model_page_id") or page_env.get("page_id") or "")
        for filename, page_env in page_envs.items()
        if isinstance(page_env, dict)
    } if isinstance(page_envs, dict) else {}
    for filename, page_env in page_envs.items() if isinstance(page_envs, dict) else []:
        if not isinstance(page_env, dict):
            continue
        resolutions = page_env.get("conflict_resolutions", []) if isinstance(page_env.get("conflict_resolutions"), list) else []
        expected_groups = groups_by_filename.get(filename, []) if isinstance(groups_by_filename, dict) else []
        fallback_count = sum(1 for item in resolutions if isinstance(item, dict) and item.get("resolver") == "deterministic_fallback")
        if expected_groups and not resolutions:
            findings.append({
                "severity": "error",
                "code": "page_conflict_groups_not_resolved",
                "filename": filename,
                "message": "Page has conflict groups, but no validated conflict resolutions were attached.",
            })
        if fallback_count:
            findings.append({
                "severity": "warning",
                "code": "page_conflict_resolution_fallback",
                "filename": filename,
                "message": f"{fallback_count} conflict group(s) used deterministic fallback instead of LLM output.",
                })
        seen_transition_keys: set[Tuple[str, ...]] = set()
        duplicates_for_page = 0
        page_l0_transitions: Dict[str, Dict[str, Any]] = {}
        for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
            if not isinstance(transition, dict):
                continue
            key = _transition_equivalence_key(transition)
            if key in seen_transition_keys:
                duplicate_equivalent_transition_count += 1
                duplicates_for_page += 1
            seen_transition_keys.add(key)
            step_key = str(transition.get("step_key") or transition.get("transition_id") or "")
            _ensure_structured_transition_identity(page_env, transition, page_ids_by_filename)
            if _is_l0_transition(transition):
                identity_key = transition.get("transition_identity_key") if isinstance(transition.get("transition_identity_key"), dict) else {}
                required_identity_fields = {"source_materialized_page_id", "evidence_step", "action_intent"}
                source = _base_filename(transition.get("source_filename"))
                target = _base_filename(transition.get("target_filename"))
                if target and target != source:
                    required_identity_fields.add("target_materialized_page_id")
                missing_identity_fields = sorted(
                    field for field in required_identity_fields if not identity_key.get(field)
                )
                if missing_identity_fields:
                    transition_identity_missing_count += 1
                    findings.append({
                        "severity": "error",
                        "code": "transition_identity_key_incomplete",
                        "filename": filename,
                        "step_key": step_key,
                        "missing_fields": missing_identity_fields,
                        "message": "L0 transition identity must be scoped by materialized page, evidence step, and structured action intent.",
                    })
                page_l0_transitions[step_key] = transition
                if str(transition.get("direction") or "source") != "target":
                    l0_interactive_required_steps.add(step_key)
                if _transition_requires_state_binding(page_env, transition) and not _generic_commit_has_state_binding(transition):
                    generic_commit_without_state_count += 1
                    findings.append({
                        "severity": "error",
                        "code": "generic_action_without_selected_record",
                        "filename": filename,
                        "step_key": step_key,
                        "operation_intent": transition.get("operation_intent", ""),
                        "message": "Generic confirm/save/submit action is not bound to selected record, draft, query, filter, cart, or overlay state.",
                    })
                if _semantic_l0_reducer_valid(transition):
                    l0_reducer_steps.add(step_key)
                else:
                    findings.append({
                        "severity": "error",
                        "code": "l0_transition_reducer_not_semantic",
                        "filename": filename,
                        "step_key": step_key,
                        "operation": transition.get("operation", ""),
                        "reducer": transition.get("reducer", ""),
                        "message": "L0 transition has no reducer-level state/route effect that can be verified.",
                    })
                effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
                if _effect_requires_record_identity(effect):
                    schemas = page_env.get("state_schema", {}).get("collections", []) if isinstance(page_env.get("state_schema"), dict) else []
                    has_record_identity = any(
                        isinstance(schema, dict)
                        and str(schema.get("state_ref") or "") == _record_collection_ref(effect)
                        and str(schema.get("record_identity") or "")
                        and str(schema.get("merge_policy") or "")
                        and (
                            not _is_contract_closure_item(schema)
                            or bool(schema.get("identity_fields"))
                        )
                        for schema in schemas
                    )
                    if has_record_identity and str(effect.get("collection_merge_policy") or ""):
                        record_identity_steps.add(step_key)
        semantic_oracle_types = _semantic_oracle_step_types(page_env, findings, filename)
        for step_key, transition in page_l0_transitions.items():
            required_types = _required_oracle_types(transition)
            covered_types = semantic_oracle_types.get(step_key, set())
            missing_types = sorted(required_types - covered_types)
            if not missing_types:
                l0_oracle_steps.add(step_key)
            else:
                findings.append({
                    "severity": "error",
                    "code": "l0_oracle_semantic_coverage_missing",
                    "filename": filename,
                    "step_key": step_key,
                    "missing_oracle_types": missing_types,
                    "message": "L0 step lacks semantic executable oracle coverage; closure or text-only oracles do not satisfy acceptance.",
                })
        semantic_projection_steps = _semantic_projection_step_keys(page_env, findings, filename)
        l0_render_projection_steps.update(semantic_projection_steps)
        l0_interactive_projection_steps.update(_interactive_projection_step_keys(page_env, findings, filename))
        route_required, route_covered = _route_affordance_coverage(page_env, findings, filename)
        route_affordance_count += route_required
        route_affordance_covered_count += route_covered
        structural_closure_l0_steps.update(_projection_step_keys(page_env) - semantic_projection_steps)
        leak_findings = _runtime_initial_leak_findings(page_env, filename)
        if leak_findings:
            runtime_initial_leak_count += len(leak_findings)
            findings.extend(leak_findings)
        query_findings, query_counts = _query_projection_scope_findings(page_env, filename)
        if query_findings:
            findings.extend(query_findings)
        query_projection_count += int(query_counts.get("query_projection_count") or 0)
        query_projection_missing_scoped_suggestions += int(query_counts.get("query_projection_missing_scoped_suggestions") or 0)
        scope_findings = _page_scope_coherence_findings(page_env, filename)
        if scope_findings:
            findings.extend(scope_findings)
        for group in expected_groups if isinstance(expected_groups, list) else []:
            if not isinstance(group, dict):
                continue
            surface = str(group.get("surface") or "")
            conflict_type = str(group.get("conflict_type") or "")
            if surface in {"bottom_navigation", "header", "search_surface"} or "slot" in conflict_type:
                gid = str(group.get("conflict_id") or "")
                resolved = any(isinstance(item, dict) and item.get("conflict_id") == gid for item in resolutions)
                if not resolved:
                    unresolved_fixed_slot_conflicts += 1
        if (
            page_env.get("task_continuations")
            or page_env.get("page_execution_contract")
            or page_env.get("pagegen_scoped_content")
            or page_env.get("required_text")
            or page_env.get("extracted_items")
            or page_env.get("dynamic_surface_contract")
            or page_env.get("input_surface_contract")
        ):
            pagegen_legacy_context_dependency += 1
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                continue
            if resolution.get("conflict_type") != "materialized_page_sibling_scope":
                continue
            selected = str(resolution.get("selected_default") or "")
            candidate_summary = resolution.get("candidate_summary", {}) if isinstance(resolution.get("candidate_summary"), dict) else {}
            selected_label = str(candidate_summary.get(selected) or "")
            if selected and selected_label and selected_label not in {str(page_env.get("filename") or ""), str(page_env.get("page_id") or "")}:


                pass
        page_summaries.append({
            "filename": filename,
            "transition_count": len(page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []),
            "component_count": len(page_env.get("components", []) if isinstance(page_env.get("components"), list) else []),
            "render_projection_count": len(page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []),
            "oracle_count": len(page_env.get("oracles", []) if isinstance(page_env.get("oracles"), list) else []),
            "conflict_group_count": len(expected_groups) if isinstance(expected_groups, list) else 0,
            "conflict_resolution_count": len(resolutions),
            "fallback_count": fallback_count,
            "duplicate_equivalent_transition_count": duplicates_for_page,
        })

    l0_steps.discard("")
    l0_query_required = [item for item in l0_query_checks if item.get("required")]
    l0_query_covered = [
        item for item in l0_query_required
        if item.get("target_identity_status") == "match" and item.get("target_identity_hints")
    ]
    if duplicate_equivalent_transition_count:
        findings.append({
            "severity": "error",
            "code": "duplicate_equivalent_transition",
            "message": f"{duplicate_equivalent_transition_count} equivalent transition(s) were duplicated in page environment specs.",
        })
    missing_reducer_steps = sorted(l0_steps - l0_reducer_steps)
    if missing_reducer_steps:
        findings.append({
            "severity": "error",
            "code": "l0_transition_reducer_missing",
            "step_keys": missing_reducer_steps[:32],
            "message": "Some L0 steps are not modeled as reducer-level transitions.",
        })
    missing_oracle_steps = sorted(l0_steps - l0_oracle_steps)
    if missing_oracle_steps:
        findings.append({
            "severity": "error",
            "code": "l0_oracle_missing",
            "step_keys": missing_oracle_steps[:32],
            "message": "Some L0 steps do not have executable oracle requirements.",
        })
    missing_render_steps = sorted(l0_steps - l0_render_projection_steps)
    if missing_render_steps:
        findings.append({
            "severity": "error",
            "code": "l0_render_projection_missing",
            "step_keys": missing_render_steps[:32],
            "message": "Some L0 steps are not covered by component/render projections.",
        })
    missing_interactive_projection_steps = sorted(l0_interactive_required_steps - l0_interactive_projection_steps)
    if missing_interactive_projection_steps:
        findings.append({
            "severity": "error",
            "code": "l0_interactive_projection_missing",
            "step_keys": missing_interactive_projection_steps[:32],
            "message": "Some L0 source steps are not covered by an actionable DOM render projection.",
        })
    missing_record_identity_steps = sorted(l0_record_mutation_steps - record_identity_steps)
    if missing_record_identity_steps:
        findings.append({
            "severity": "error",
            "code": "l0_record_identity_missing",
            "step_keys": missing_record_identity_steps[:32],
            "message": "Some L0 collection mutations lack canonical record identity or merge policy coverage.",
        })
    if unresolved_fixed_slot_conflicts:
        findings.append({
            "severity": "error",
            "code": "unresolved_fixed_slot_conflict",
            "message": f"{unresolved_fixed_slot_conflicts} fixed-slot conflict(s) were not resolved before PageGen.",
        })
    if pagegen_legacy_context_dependency:
        findings.append({
            "severity": "error",
            "code": "pagegen_legacy_context_dependency",
            "message": "Page environment spec still carries legacy/raw pagegen context fields.",
        })

    error_count = sum(1 for item in findings if item.get("severity") == "error")
    warning_count = sum(1 for item in findings if item.get("severity") == "warning")
    acceptance = {
        "blocking_error_count": error_count,
        "l0_step_count": len(l0_steps),
        "l0_step_coverage": _coverage(len(l0_steps), len(l0_steps)),
        "l0_transition_reducer_coverage": _coverage(len(l0_reducer_steps & l0_steps), len(l0_steps)),
        "l0_oracle_coverage": _coverage(len(l0_oracle_steps & l0_steps), len(l0_steps)),
        "l0_query_identity_coverage": _coverage(len(l0_query_covered), len(l0_query_required)),
        "l0_query_identity_required_count": len(l0_query_required),
        "l0_record_identity_coverage": _coverage(len(record_identity_steps & l0_record_mutation_steps), len(l0_record_mutation_steps)),
        "l0_record_mutation_count": len(l0_record_mutation_steps),
        "l0_render_projection_coverage": _coverage(len(l0_render_projection_steps & l0_steps), len(l0_steps)),
        "l0_interactive_projection_coverage": _coverage(
            len(l0_interactive_projection_steps & l0_interactive_required_steps),
            len(l0_interactive_required_steps),
        ),
        "route_navigation_affordance_count": route_affordance_count,
        "route_navigation_affordance_coverage": _coverage(
            route_affordance_covered_count,
            route_affordance_count,
        ),
        "structural_closure_l0_step_count": len(structural_closure_l0_steps & l0_steps),
        "unresolved_fixed_slot_conflicts": unresolved_fixed_slot_conflicts,
            "runtime_initial_leak_count": runtime_initial_leak_count,
            "pagegen_legacy_context_dependency": pagegen_legacy_context_dependency,
            "duplicate_equivalent_transition_count": duplicate_equivalent_transition_count,
            "query_projection_count": query_projection_count,
            "query_projection_missing_scoped_suggestions": query_projection_missing_scoped_suggestions,
            "generic_commit_without_state_count": generic_commit_without_state_count,
            "transition_identity_missing_count": transition_identity_missing_count,
        }
    return {
        "format": "page_environment_spec_evaluation",
        "schema_revision": 2,
        "ok": error_count == 0,
        "summary": {
            "trace_count": len(summaries),
            "page_count": len(page_summaries),
            "error_count": error_count,
            "warning_count": warning_count,
            "finding_count": len(findings),
            "conflict_group_count": (
                conflict_resolution.get("group_count", 0)
                if isinstance(conflict_resolution, dict)
                else 0
            ),
            "conflict_resolution_count": (
                conflict_resolution.get("resolution_count", 0)
                if isinstance(conflict_resolution, dict)
                else 0
            ),
            "conflict_fallback_count": (
                conflict_resolution.get("fallback_count", 0)
                if isinstance(conflict_resolution, dict)
                else 0
            ),
            "canonical_route_materialization": route_materialization_summary,
            "acceptance": acceptance,
        },
        "traces": summaries,
        "pages": page_summaries,
        "findings": findings,
    }


def _norm_artifact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


_QUERY_STATE_RUNTIME_SCRIPT_RE = re.compile(
    r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\']query-state["\'])[^>]*>(?P<body>.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_QUERY_STATE_CONTRACT_RE = re.compile(r"\bconst\s+CONTRACT\s*=\s*(?P<payload>\{.*?\});\s*const\s+states\b", re.DOTALL)


def _extract_query_state_contracts_from_html(html_text: str) -> List[Dict[str, Any]]:
    contracts: List[Dict[str, Any]] = []
    text = str(html_text or "")
    for script in _QUERY_STATE_RUNTIME_SCRIPT_RE.finditer(text):
        match = _QUERY_STATE_CONTRACT_RE.search(script.group("body") or "")
        if not match:
            continue
        try:
            payload = json.loads(match.group("payload"))
        except Exception:
            continue
        if isinstance(payload, dict):
            contracts.append(payload)
    return contracts


def _html_attr_value(attrs: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*([\"'])(?P<value>.*?)\1",
        str(attrs or ""),
        re.IGNORECASE | re.DOTALL,
    )
    return match.group("value") if match else ""


def _visible_search_input_values(html_text: str) -> List[str]:
    values: List[str] = []
    for match in re.finditer(r"<input\b(?P<attrs>[^>]*)>", str(html_text or ""), re.IGNORECASE | re.DOTALL):
        attrs = match.group("attrs") or ""
        haystack = attrs.lower()
        if not (
            'type="search"' in haystack
            or "type='search'" in haystack
            or "搜索" in attrs
            or re.search(r"\b(?:id|class)\s*=\s*([\"'])[^\"']*search", attrs, re.IGNORECASE)
        ):
            continue
        value = _html_attr_value(attrs, "value").strip()
        if value:
            values.append(value)
    return _dedupe(values, limit=16)


def _html_without_ghostweb_runtime_scripts(html_text: str) -> str:
    return re.sub(
        r'<script\b(?=[^>]*\bdata-ghostweb-runtime=["\'][^"\']+["\'])[^>]*>.*?</script>',
        "",
        str(html_text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )


_APPLIED_QUERY_HINT_PREFIXES = (
    "query_applied:",
    "applied_query:",
    "selected_suggestion:",
    "selected_query:",
)


def _raw_identity_hint_strings(item: Dict[str, Any]) -> List[str]:
    hints: List[str] = []
    if not isinstance(item, dict):
        return hints
    for key in (
        "target_identity_hints",
        "target_identity_terms",
        "target_query_hints",
        "identity_hints",
        "query_identity_hints",
    ):
        value = item.get(key)
        if isinstance(value, list):
            hints.extend(str(part or "") for part in value if str(part or "").strip())
        elif str(value or "").strip():
            hints.append(str(value or "").strip())
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    if effect:
        hints.extend(_raw_identity_hint_strings(effect))
    return _dedupe(hints, limit=32)


def _applied_query_values(item: Dict[str, Any]) -> List[str]:
    explicit_values: List[str] = []
    for key in ("committed_query_values", "applied_query_values", "target_query_values"):
        value = item.get(key) if isinstance(item, dict) else None
        if isinstance(value, list):
            explicit_values.extend(str(part or "").strip() for part in value if str(part or "").strip())
        elif str(value or "").strip():
            explicit_values.append(str(value or "").strip())
    if explicit_values:
        return _dedupe(explicit_values, limit=8)
    applied: List[str] = []
    for hint in _raw_identity_hint_strings(item):
        text = str(hint or "").strip()
        lowered = text.lower()
        for prefix in _APPLIED_QUERY_HINT_PREFIXES:
            if lowered.startswith(prefix):
                value = text.split(":", 1)[1].strip() if ":" in text else ""
                if value:
                    applied.append(value)
                break
    return _dedupe(applied, limit=8)


def _observed_query_values(item: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    observed = item.get("observed_values") if isinstance(item, dict) else None
    if isinstance(observed, list):
        values.extend(str(part or "").strip() for part in observed if str(part or "").strip())
    elif str(observed or "").strip():
        values.append(str(observed or "").strip())
    for key in ("input_text", "value"):
        value = item.get(key) if isinstance(item, dict) else None
        if str(value or "").strip():
            values.append(str(value or "").strip())
    return _dedupe(values, limit=8)


def _committed_query_values_for_item(item: Dict[str, Any]) -> List[str]:
    kind = str(item.get("query_transition_kind") or "") if isinstance(item, dict) else ""
    effect = item.get("state_effect") if isinstance(item, dict) and isinstance(item.get("state_effect"), dict) else {}
    if not kind and effect:
        kind = str(effect.get("query_transition_kind") or "")
    if kind == "selected_suggestion_commit":
        applied = _applied_query_values(item)
        if applied:
            return applied
        if effect:
            applied = _applied_query_values(effect)
            if applied:
                return applied
    committed = _applied_query_values(item)
    if committed:
        return committed
    if effect:
        committed = _applied_query_values(effect)
        if committed:
            return committed
    return _observed_query_values(item)


def _page_environment_expected_committed_queries(filename: str, page_env: Dict[str, Any]) -> List[str]:
    expected: List[str] = []
    current = _base_filename(filename or page_env.get("filename") or page_env.get("page_id"))
    for item in page_env.get("parameterized_inputs", []) if isinstance(page_env.get("parameterized_inputs"), list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("field_role") or item.get("parameter_id") or "") != "business_query":
            continue
        target = _base_filename(item.get("target_filename") or current)
        kind = str(item.get("query_transition_kind") or "")
        status = str(item.get("target_identity_status") or "")
        if target != current:
            continue
        if kind not in {"committed_result", "selected_suggestion_commit"}:
            continue
        if status and status != "match":
            continue
        expected.extend(_committed_query_values_for_item(item))
    for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
        if not isinstance(transition, dict):
            continue
        effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
        if str(effect.get("field_role") or "") != "business_query":
            continue
        kind = str(transition.get("query_transition_kind") or effect.get("query_transition_kind") or "")
        if kind not in {"committed_result", "selected_suggestion_commit"}:
            continue
        target = _base_filename(transition.get("target_filename") or "")
        if target and target != current:
            continue
        expected.extend(_committed_query_values_for_item(transition))
    return _dedupe(expected, limit=16)


def _query_values_for_projection_item(item: Dict[str, Any]) -> List[str]:
    if _query_projection_kind(item) == "selected_suggestion_commit":
        return _committed_query_values_for_item(item)
    values: List[str] = []
    values.extend(_committed_query_values_for_item(item))
    values.extend(_observed_query_values(item))
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    if effect:
        values.extend(_query_fields_from_effect(effect))
        values.extend(_committed_query_values_for_item(effect))
        values.extend(_observed_query_values(effect))
    if item.get("input_text"):
        values.append(str(item.get("input_text") or ""))
    return _dedupe(values, limit=8)


def _effect_has_business_query_committed_field(effect: Dict[str, Any]) -> bool:
    fields = effect.get("committed_fields", []) if isinstance(effect.get("committed_fields"), list) else []
    return any(
        isinstance(field, dict) and str(field.get("field_role") or "") == "business_query"
        for field in fields
    )


def _is_non_search_form_commit(item: Dict[str, Any]) -> bool:
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    reducer = str(item.get("reducer") or effect.get("effect") or "")
    if reducer != "submit_form":
        return False
    field_role = str(item.get("field_role") or item.get("parameter_id") or effect.get("field_role") or "")
    if field_role == "business_query":
        return False
    return not _effect_has_business_query_committed_field(effect)


def _query_projection_kind(item: Dict[str, Any]) -> str:
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    if _is_non_search_form_commit(item):
        return "unrelated_navigation"
    kind = str(item.get("query_transition_kind") or effect.get("query_transition_kind") or "")
    if kind:
        return kind
    field_role = str(item.get("field_role") or item.get("parameter_id") or effect.get("field_role") or "")
    if field_role in {"address_lookup", "location_lookup"}:
        return "suggestion_projection"
    if field_role == "business_query":
        source = _base_filename(item.get("source_filename"))
        target = _base_filename(item.get("target_filename"))
        if target and target != source:
            return "committed_result"
        return "query_entry"
    return ""


def _query_projection_state_ref(page_env: Dict[str, Any], item: Dict[str, Any]) -> str:
    effect = item.get("state_effect") if isinstance(item.get("state_effect"), dict) else {}
    explicit = str(
        item.get("query_state_owner")
        or effect.get("query_state_owner")
        or effect.get("state_ref")
        or effect.get("draft_ref")
        or ""
    )
    if explicit:
        return explicit
    source = _base_filename(item.get("source_filename") or page_env.get("filename") or page_env.get("page_id"))
    field_role = str(item.get("field_role") or item.get("parameter_id") or effect.get("field_role") or "query")
    return _state_ref(f"{source}::{field_role}")


def _projection_submit_target(kind: str, target: str, current: str) -> str:
    if kind in {"committed_result", "selected_suggestion_commit", "search_within_collection"} and target and target != current:
        return target
    return ""


def _build_query_projection_entry(
    page_env: Dict[str, Any],
    item: Dict[str, Any],
    query: str,
    *,
    source_kind: str,
) -> Dict[str, Any]:
    current = _base_filename(page_env.get("filename") or page_env.get("page_id"))
    source = _base_filename(item.get("source_filename") or current)
    target = _base_filename(item.get("target_filename") or current)
    kind = _query_projection_kind(item)
    status = str(item.get("target_identity_status") or "")
    hints = _target_identity_hints(item)
    if not status:
        status = _query_target_identity_status(target, query, hints)
    elif status == "unknown" and _query_target_identity_status(target, query, hints) == "match":
        status = "match"
    if kind in {"query_entry", "suggestion_projection", "unrelated_navigation"}:
        status = "not_applicable"
    entry = {
        "query_identity": f"query::{_safe_id(query)}",
        "query_value": query,
        "field_role": str(item.get("field_role") or item.get("parameter_id") or "business_query"),
        "source_filename": source,
        "target_filename": target,
        "submit_target_filename": _projection_submit_target(kind, target, current),
        "query_transition_kind": kind or "unknown",
        "target_identity_hints": hints,
        "target_identity_status": status or "unknown",
        "query_state_owner": _query_projection_state_ref(page_env, item),
        "projection_policy": (
            "project_runtime_suggestions_by_query"
            if kind in {"query_entry", "suggestion_projection"}
            else "commit_query_to_scoped_target"
        ),
        "result_projection": _clip_text(item.get("result_projection"), 180),
        "source_kind": source_kind,
        "step_keys": _dedupe(
            [
                str(item.get("step_key") or ""),
                str(item.get("transition_id") or ""),
                str(item.get("committed_by_step") or ""),
            ],
            limit=4,
        ),
        "visible_initial_prefill_allowed": False,
    }
    return {key: value for key, value in entry.items() if value not in ("", [], {})}


def _rebuild_page_query_projections(page_env: Dict[str, Any]) -> None:
    projections: List[Dict[str, Any]] = []
    for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
        if not isinstance(transition, dict):
            continue
        effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
        field_role = str(effect.get("field_role") or transition.get("field_role") or "")
        kind = _query_projection_kind(transition)
        if _is_non_search_form_commit(transition):
            continue
        if field_role not in SEARCHLIKE_FIELD_ROLES and kind not in QUERY_TRANSITION_KINDS:
            continue
        for query in _query_values_for_projection_item(transition):
            _add_unique_dict(
                projections,
                _build_query_projection_entry(page_env, transition, query, source_kind="transition"),
                ("query_identity", "source_filename", "target_filename", "query_transition_kind"),
            )
    for parameter in page_env.get("parameterized_inputs", []) if isinstance(page_env.get("parameterized_inputs"), list) else []:
        if not isinstance(parameter, dict):
            continue
        if str(parameter.get("field_role") or parameter.get("parameter_id") or "") not in SEARCHLIKE_FIELD_ROLES:
            continue
        for query in _query_values_for_projection_item(parameter):
            _add_unique_dict(
                projections,
                _build_query_projection_entry(page_env, parameter, query, source_kind="parameterized_input"),
                ("query_identity", "source_filename", "target_filename", "query_transition_kind"),
            )
    page_env["query_projections"] = projections
    for projection in projections:
        state_ref = str(projection.get("query_state_owner") or "")
        if state_ref:
            _ensure_runtime_state_refs(page_env, state_ref, source="page_environment_query_projection_compiler")


def _page_environment_authorized_query_states(filename: str, page_env: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    current = _base_filename(filename or page_env.get("filename") or page_env.get("page_id"))
    for item in page_env.get("parameterized_inputs", []) if isinstance(page_env.get("parameterized_inputs"), list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("field_role") or item.get("parameter_id") or "") != "business_query":
            continue
        source = _base_filename(item.get("source_filename") or "")
        target = _base_filename(item.get("target_filename") or current)
        if target == current:
            values.extend(_committed_query_values_for_item(item))
        if not target or source == current or (not source and target == current):
            values.extend(_observed_query_values(item))
    for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
        if not isinstance(transition, dict):
            continue
        effect = transition.get("state_effect") if isinstance(transition.get("state_effect"), dict) else {}
        if str(effect.get("field_role") or "") != "business_query":
            continue
        source = _base_filename(transition.get("source_filename") or "")
        target = _base_filename(transition.get("target_filename") or current)
        if target == current:
            values.extend(_committed_query_values_for_item(transition))
        if source == current:
            values.extend(_observed_query_values(transition))
    return _dedupe(values, limit=32)


def _page_environment_has_preserve_collection(page_env: Dict[str, Any]) -> bool:
    state_schema = page_env.get("state_schema", {}) if isinstance(page_env.get("state_schema"), dict) else {}
    for item in state_schema.get("collections", []) if isinstance(state_schema.get("collections"), list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("merge_policy") or "") == "preserve_existing_then_append_or_upsert":
            return True
    for projection in page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []:
        if not isinstance(projection, dict):
            continue
        if str(projection.get("mutation_policy") or "") == "preserve_existing_then_append_or_upsert":
            return True
    return False


_ARTIFACT_COLLECTION_GENERIC_TOKENS = {
    "action",
    "actions",
    "button",
    "cmp",
    "collection",
    "collections",
    "component",
    "default",
    "draft",
    "edit",
    "form",
    "header",
    "html",
    "initial",
    "item",
    "items",
    "list",
    "lists",
    "navigation",
    "page",
    "profile",
    "projection",
    "records",
    "record",
    "result",
    "results",
    "runtime",
    "scope",
    "screen",
    "search",
    "selection",
    "state",
    "user",
    "view",
}


def _artifact_semantic_tokens(value: Any) -> set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        lowered = token.lower()
        if lowered.endswith("s") and len(lowered) > 3:
            lowered = lowered[:-1]
        if len(lowered) < 3 or lowered in _ARTIFACT_COLLECTION_GENERIC_TOKENS:
            continue
        tokens.add(lowered)
    return tokens


def _page_environment_preserve_collection_tokens(page_env: Dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    preserve_refs: set[str] = set()
    preserve_component_refs: set[str] = set()
    state_schema = page_env.get("state_schema", {}) if isinstance(page_env.get("state_schema"), dict) else {}
    for item in state_schema.get("collections", []) if isinstance(state_schema.get("collections"), list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("merge_policy") or "") != "preserve_existing_then_append_or_upsert":
            continue
        ref = str(item.get("state_ref") or item.get("state_scope") or "")
        if "committed_records_draft" in ref:
            continue
        preserve_refs.add(ref)
        tokens.update(_artifact_semantic_tokens(ref))
    for projection in page_env.get("render_projections", []) if isinstance(page_env.get("render_projections"), list) else []:
        if not isinstance(projection, dict):
            continue
        if str(projection.get("mutation_policy") or "") != "preserve_existing_then_append_or_upsert":
            continue
        reads_state = str(projection.get("reads_state") or "")
        if "committed_records_draft" in reads_state:
            continue
        preserve_refs.add(reads_state)
        component_ref = str(projection.get("component_ref") or "")
        if component_ref:
            preserve_component_refs.add(component_ref)
        tokens.update(_artifact_semantic_tokens(reads_state))
    for component in page_env.get("components", []) if isinstance(page_env.get("components"), list) else []:
        if not isinstance(component, dict):
            continue
        component_id = str(component.get("component_id") or "")
        reads_state = str(component.get("reads_state") or "")
        if component_id not in preserve_component_refs and reads_state not in preserve_refs:
            continue
        haystack = " ".join(
            str(component.get(key) or "")
            for key in ("component_kind", "component_role", "reads_state", "layout_policy")
        )
        if "committed_records_draft" in haystack:
            continue
        if not re.search(r"\b(?:appendable|collection|list|records?|addresses?)\b", haystack, re.IGNORECASE):
            continue
        tokens.update(_artifact_semantic_tokens(haystack))
    return tokens


def _replacement_target_semantic_text(text: str, match: re.Match[str]) -> str:
    assignment_start = max(0, match.start() - 160)
    assignment_end = min(len(text), match.end() + 160)
    assignment_context = text[assignment_start:assignment_end]
    target_name = ""
    target_match = re.search(
        r"(?P<target>[A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*(?:innerHTML|textContent|replaceChildren)\b",
        assignment_context,
    )
    if target_match:
        target_name = target_match.group("target")
    parts = [assignment_context]
    if target_name:
        declaration_re = re.compile(
            rf"\b(?:const|let|var)\s+{re.escape(target_name)}\s*=\s*[^;]+;",
            re.IGNORECASE | re.DOTALL,
        )
        search_start = max(0, match.start() - 1400)
        prefix = text[search_start:match.start()]
        declarations = list(declaration_re.finditer(prefix))
        if declarations:
            parts.append(declarations[-1].group(0))
    return " ".join(parts)


def _is_derived_search_projection_replacement(
    semantic_context: str,
    collection_tokens: set[str],
) -> bool:
    search_tokens = {
        "autocomplete",
        "query",
        "result",
        "results",
        "search",
        "suggest",
        "suggestion",
        "suggestions",
    }
    lowered = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(semantic_context or "")).lower()
    if not any(re.search(rf"\b{re.escape(token)}\b", lowered) for token in search_tokens):
        return False
    if re.search(r"\b(?:autocomplete|suggest|suggestion|suggestions)\b", lowered):
        return True


    return not bool(collection_tokens & search_tokens)


def _stateful_collection_replacement_contexts(html_text: str, page_env: Dict[str, Any]) -> List[str]:
    contexts: List[str] = []
    collection_tokens = _page_environment_preserve_collection_tokens(page_env)
    if not collection_tokens:
        return contexts
    text = _html_without_ghostweb_runtime_scripts(html_text)
    pattern = re.compile(
        r"\.(?:innerHTML|textContent)\s*=\s*['\"]\s*['\"]|\.replaceChildren\s*\(",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        start = max(0, match.start() - 900)
        end = min(len(text), match.end() + 500)
        context = text[start:end]
        if not re.search(r"\bWebsiteSDK\s*\.\s*(?:get|list|query|search|load|fetch)", context):
            continue
        semantic_context = _replacement_target_semantic_text(text, match)
        if _is_derived_search_projection_replacement(semantic_context, collection_tokens):
            continue
        if not (_artifact_semantic_tokens(semantic_context) & collection_tokens):
            continue
        contexts.append(re.sub(r"\s+", " ", context).strip()[:400])
        if len(contexts) >= 8:
            break
    return contexts


def validate_page_environment_artifacts(
    payload: Dict[str, Any],
    html_by_filename: Dict[str, str],
    sdk_javascript: str = "",
) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    page_reports: List[Dict[str, Any]] = []
    page_envs = payload.get("page_environment_specs_by_filename", {}) if isinstance(payload, dict) else {}
    for filename, page_env in page_envs.items() if isinstance(page_envs, dict) else []:
        if not isinstance(page_env, dict):
            continue
        html_text = str(html_by_filename.get(filename) or "")
        if not html_text:
            continue
        before = len(findings)
        expected_committed_queries = _page_environment_expected_committed_queries(filename, page_env)
        expected_committed_norm = {_norm_artifact_text(item) for item in expected_committed_queries}
        authorized_queries = _page_environment_authorized_query_states(filename, page_env)
        authorized_norm = {_norm_artifact_text(item) for item in authorized_queries}
        if authorized_norm:
            contract_queries: List[str] = []
            for contract in _extract_query_state_contracts_from_html(html_text):
                for state in contract.get("states", []) if isinstance(contract.get("states"), list) else []:
                    if isinstance(state, dict) and str(state.get("query") or "").strip():
                        contract_queries.append(str(state.get("query") or "").strip())
            mismatched_contract_queries = [
                query for query in _dedupe(contract_queries, limit=16)
                if _norm_artifact_text(query) not in authorized_norm
            ]
            if mismatched_contract_queries:
                findings.append({
                    "severity": "error",
                    "code": "artifact_query_state_contract_mismatch",
                    "filename": filename,
                    "authorized_queries": authorized_queries,
                    "actual_queries": mismatched_contract_queries,
                    "message": "Generated query_state_contract contains query values that are not authorized by page_environment_spec business_query state transitions.",
                })
        if expected_committed_norm:
            visible_values = _visible_search_input_values(html_text)
            mismatched_visible_values = [
                value for value in visible_values
                if _norm_artifact_text(value) not in expected_committed_norm
            ]
            if mismatched_visible_values:
                findings.append({
                    "severity": "error",
                    "code": "artifact_visible_search_input_mismatch",
                    "filename": filename,
                    "expected_queries": expected_committed_queries,
                    "actual_values": mismatched_visible_values,
                    "message": "Generated visible search input value does not match page_environment_spec committed query state.",
                })
        replacement_contexts = (
            _stateful_collection_replacement_contexts(html_text, page_env)
            if _page_environment_has_preserve_collection(page_env)
            else []
        )
        if replacement_contexts:
            findings.append({
                "severity": "error",
                "code": "artifact_collection_replacement_outside_spec",
                "filename": filename,
                "evidence": replacement_contexts,
                "message": "Generated HTML clears/replaces a stateful collection even though page_environment_spec requires preserve-existing merge semantics.",
            })
        page_reports.append({
            "filename": filename,
            "finding_count": len(findings) - before,
            "expected_committed_queries": expected_committed_queries,
            "authorized_query_states": authorized_queries,
            "has_preserve_collection": _page_environment_has_preserve_collection(page_env),
        })
    error_count = sum(1 for item in findings if item.get("severity") == "error")
    warning_count = sum(1 for item in findings if item.get("severity") == "warning")
    return {
        "format": "page_environment_artifact_validation",
        "schema_revision": 1,
        "ok": error_count == 0,
        "summary": {
            "page_count": len(page_reports),
            "error_count": error_count,
            "warning_count": warning_count,
            "finding_count": len(findings),
        },
        "pages": page_reports,
        "findings": findings,
    }


SPEC_ROUTE_AUDIT_BLOCKING_CODES = {
    "source_materialized_mismatch",
    "target_mismatch",
    "missing_query_entry_materialized_page",
    "query_values_collapsed_to_single_target",
    "record_identity_target_mismatch",
    "generic_action_without_selected_record",
    "route_target_kind_mismatch",
    "same_page_runtime_step_has_unjustified_navigation",
    "query_target_mismatch",
    "query_commit_target_mismatch",
    "route_navigation_affordance_identity_incomplete",
    "transition_identity_key_incomplete",
}


def write_full_spec_route_audit(
    result_dir: Path,
    specs: Dict[str, Any],
    evaluation: Dict[str, Any],
    *,
    audit_dir: Path | None = None,
) -> Tuple[Path, Path]:
    if audit_dir is None:
        audit_dir = result_dir / "phase_page_environment" / "debug" / "spec_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    findings = [
        item for item in evaluation.get("findings", [])
        if isinstance(item, dict)
    ] if isinstance(evaluation, dict) else []
    blocking = [
        item for item in findings
        if str(item.get("severity") or "") == "error"
        and str(item.get("code") or "") in SPEC_ROUTE_AUDIT_BLOCKING_CODES
    ]
    route_rows: List[Dict[str, Any]] = []
    page_envs = specs.get("page_environment_specs_by_filename", {}) if isinstance(specs, dict) else {}
    for filename, page_env in page_envs.items() if isinstance(page_envs, dict) else []:
        if not isinstance(page_env, dict):
            continue
        for transition in page_env.get("transitions", []) if isinstance(page_env.get("transitions"), list) else []:
            if not isinstance(transition, dict) or not _is_l0_transition(transition):
                continue
            route_rows.append({
                "filename": filename,
                "step_key": transition.get("step_key", ""),
                "direction": transition.get("direction", "source"),
                "operation": transition.get("operation", ""),
                "reducer": transition.get("reducer", ""),
                "source_filename": transition.get("source_filename", ""),
                "target_filename": transition.get("target_filename", ""),
                "operation_intent": transition.get("operation_intent", ""),
                "invalid_reason": transition.get("invalid_reason", ""),
            })
    issue_counts: Dict[str, int] = defaultdict(int)
    for item in blocking:
        issue_counts[str(item.get("code") or "unknown")] += 1
    audit = {
        "format": "full_spec_route_audit",
        "schema_revision": 1,
        "ok": not blocking,
        "summary": {
            "trace_count": evaluation.get("summary", {}).get("trace_count", 0) if isinstance(evaluation.get("summary"), dict) else 0,
            "page_count": evaluation.get("summary", {}).get("page_count", 0) if isinstance(evaluation.get("summary"), dict) else 0,
            "route_or_transition_rows": len(route_rows),
            "blocking_error_count": len(blocking),
            "issue_counts": dict(sorted(issue_counts.items())),
        },
        "blocking_categories": sorted(SPEC_ROUTE_AUDIT_BLOCKING_CODES),
        "issues": blocking,
        "route_or_transition_rows": route_rows,
    }
    json_path = audit_dir / "full_spec_route_audit_16traces.json"
    md_path = audit_dir / "full_spec_route_audit_16traces.md"
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# Full Spec Route Audit",
        "",
        f"- ok: {audit['ok']}",
        f"- traces: {audit['summary']['trace_count']}",
        f"- pages: {audit['summary']['page_count']}",
        f"- route/transition rows: {audit['summary']['route_or_transition_rows']}",
        f"- blocking errors: {audit['summary']['blocking_error_count']}",
        "",
        "## Blocking Issues",
    ]
    if blocking:
        for item in blocking:
            md_lines.append(
                f"- `{item.get('code')}` {item.get('filename', '')} {item.get('trace_id', '')} "
                f"{item.get('step_key', '')}: {item.get('message', '')}"
            )
    else:
        md_lines.append("- none")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    register_artifact(
        result_dir,
        json_path,
        artifact_id="reports.spec_audit.full_spec_route_audit",
        phase="page_environment_compiler",
        artifact_kind="validation_report",
        api_or_det="det",
        canonical_source=False,
        logical_owner="page_environment_spec",
        producer="site_model.page_environment_spec.write_full_spec_route_audit",
        allowed_consumers=("developer", "artifact_inventory"),
        semantic_role="spec_route_coherence_audit",
        refresh_filesystem_summary=False,
    )
    return json_path, md_path


def write_page_environment_artifacts(
    site_model,
    result_dir: Path,
    output_dir: Path,
    *,
    trace_ids: Sequence[str] | None = None,
    resolve_conflicts: bool = False,
    enrich_spec: bool = False,
    conflict_model: str | None = None,
    conflict_reasoning_effort: str = "medium",
    conflict_max_groups_per_call: int = 8,
    conflict_max_concurrent: int = 8,
    conflict_max_tokens: int = 12000,
    enrichment_model: str | None = None,
    enrichment_reasoning_effort: str = "medium",
    enrichment_max_query_groups_per_call: int = 12,
    enrichment_max_component_pages_per_call: int = 6,
    enrichment_component_projection_input_budget_tokens: int = _COMPONENT_PROJECTION_INPUT_BUDGET_TOKENS,
    enrichment_max_concurrent: int = 8,
    enrichment_max_tokens: int = 16000,
    enrichment_phase_3_logicflow: str = PHASE_3_LOGICFLOW_CLOSURE_DEFAULT_REVIEW_ONLY,
    include_route_affordance_candidates: bool = False,
) -> Tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    page_execution_contracts = site_model.page_execution_contracts
    if isinstance(page_execution_contracts, dict) and page_execution_contracts:
        route_affordance_candidates = build_route_affordance_candidates(page_execution_contracts)
        projection_dir = result_dir / "projections" / "page_environment"
        projection_dir.mkdir(parents=True, exist_ok=True)
        route_affordance_candidates_path = projection_dir / "route_affordance_candidates.json"
        route_affordance_candidates_path.write_text(
            json.dumps(route_affordance_candidates, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        register_artifact(
            result_dir,
            route_affordance_candidates_path,
            artifact_id="projections.page_environment.route_affordance_candidates",
            phase="page_environment_compiler",
            artifact_kind="det_projection",
            api_or_det="det",
            canonical_source=False,
            logical_owner="page_environment_spec",
            producer="site_model.page_environment_spec.build_route_affordance_candidates",
            allowed_consumers=("page_environment_compiler", "validator"),
            semantic_role="route_affordance_candidates",
            canonical_semantic_source=False,
            canonical_prompt_boundary=False,
            artifact_role="candidate_projection",
            source_selection={
                "derived_from": "phase_consolidation/site_model/page_execution_contracts.json",
            },
        )
    specs = build_page_environment_specs(
        site_model,
        result_dir,
        trace_ids=trace_ids,
        include_route_affordance_candidates=include_route_affordance_candidates,
    )
    if resolve_conflicts:
        specs = resolve_page_environment_conflicts(
            site_model,
            result_dir,
            specs,
            model=conflict_model,
            reasoning_effort=conflict_reasoning_effort,
            max_groups_per_call=conflict_max_groups_per_call,
            max_concurrent=conflict_max_concurrent,
            max_tokens=conflict_max_tokens,
        )
    if enrich_spec:
        specs = enrich_page_environment_specs(
            site_model,
            result_dir,
            specs,
            model=enrichment_model or conflict_model,
            reasoning_effort=enrichment_reasoning_effort,
            max_query_groups_per_call=enrichment_max_query_groups_per_call,
            max_component_pages_per_call=enrichment_max_component_pages_per_call,
            component_projection_input_budget_tokens=enrichment_component_projection_input_budget_tokens,
            max_concurrent=enrichment_max_concurrent,
            max_tokens=enrichment_max_tokens,
            phase_3_logicflow=enrichment_phase_3_logicflow,
        )
    evaluation = evaluate_page_environment_specs(specs)
    spec_path = output_dir / "page_environment_specs.json"
    validation_dir = result_dir / "validation" / "spec"
    validation_dir.mkdir(parents=True, exist_ok=True)
    eval_path = validation_dir / "evaluation.json"
    md_path = validation_dir / "evaluation.md"
    spec_path.write_text(json.dumps(specs, ensure_ascii=False, indent=2), encoding="utf-8")
    eval_path.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_json_path, audit_md_path = write_full_spec_route_audit(result_dir, specs, evaluation)
    register_artifact(
        result_dir,
        spec_path,
        artifact_id="semantic_api.page_environment_specs",
        phase="page_environment_compiler",
        artifact_kind="api_semantic_source",
        api_or_det="mixed",
        canonical_source=True,
        logical_owner="page_environment_spec",
        producer="site_model.page_environment_spec.write_page_environment_artifacts",
        allowed_consumers=("pagegen", "runtime_backend", "validator"),
        semantic_role="authoritative_environment_spec",
        refresh_filesystem_summary=False,
    )
    register_artifact(
        result_dir,
        eval_path,
        artifact_id="validation.spec.evaluation",
        phase="page_environment_compiler",
        artifact_kind="validation_report",
        api_or_det="det",
        canonical_source=False,
        logical_owner="page_environment_spec",
        producer="site_model.page_environment_spec.evaluate_page_environment_specs",
        allowed_consumers=("developer", "artifact_inventory"),
        semantic_role="spec_acceptance_evaluation",
        refresh_filesystem_summary=False,
    )
    register_artifact(
        result_dir,
        audit_md_path,
        artifact_id="reports.spec_audit.full_spec_route_audit_markdown",
        phase="page_environment_compiler",
        artifact_kind="validation_report",
        api_or_det="det",
        canonical_source=False,
        logical_owner="page_environment_spec",
        producer="site_model.page_environment_spec.write_full_spec_route_audit",
        allowed_consumers=("developer", "artifact_inventory"),
        semantic_role="spec_route_coherence_audit",
        refresh_filesystem_summary=False,
    )
    lines = [
        "# Page Environment Spec Evaluation",
        "",
        f"- ok: {evaluation['ok']}",
        f"- traces: {evaluation['summary']['trace_count']}",
        f"- pages: {evaluation['summary'].get('page_count', 0)}",
        f"- conflict groups: {evaluation['summary'].get('conflict_group_count', 0)}",
        f"- conflict fallbacks: {evaluation['summary'].get('conflict_fallback_count', 0)}",
        f"- errors: {evaluation['summary']['error_count']}",
        f"- warnings: {evaluation['summary']['warning_count']}",
        f"- l0 reducer coverage: {evaluation['summary'].get('acceptance', {}).get('l0_transition_reducer_coverage', 1.0)}",
        f"- l0 oracle coverage: {evaluation['summary'].get('acceptance', {}).get('l0_oracle_coverage', 1.0)}",
        f"- l0 query identity coverage: {evaluation['summary'].get('acceptance', {}).get('l0_query_identity_coverage', 1.0)}",
        f"- l0 render projection coverage: {evaluation['summary'].get('acceptance', {}).get('l0_render_projection_coverage', 1.0)}",
        f"- l0 interactive projection coverage: {evaluation['summary'].get('acceptance', {}).get('l0_interactive_projection_coverage', 1.0)}",
        "",
        "## Findings",
    ]
    if evaluation["findings"]:
        for item in evaluation["findings"]:
            lines.append(
                f"- {item.get('severity')} `{item.get('code')}` "
                f"{item.get('trace_id')} {item.get('step_key', '')}: {item.get('message')}"
            )
    else:
        lines.append("- No findings.")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    register_artifact(
        result_dir,
        md_path,
        artifact_id="validation.spec.evaluation_markdown",
        phase="page_environment_compiler",
        artifact_kind="validation_report",
        api_or_det="det",
        canonical_source=False,
        logical_owner="page_environment_spec",
        producer="site_model.page_environment_spec.write_page_environment_artifacts",
        allowed_consumers=("developer",),
        semantic_role="spec_acceptance_evaluation_summary",
        refresh_filesystem_summary=False,
    )
    rebuild_artifact_index(result_dir)
    return spec_path, eval_path, md_path
