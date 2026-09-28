
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set



from trace2world_pipeline.core.config_manager import Trace2WorldConfigManager
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.resources.resource_judge import ResourceJudge
from trace2world_pipeline.core.llm_caller import configure_load_balancing, configure_stage_model_routing
from trace2world_pipeline.resources.screenshot_resource_extractor import ScreenshotResourceExtractor
from trace2world_pipeline.app_spec.environment_profile import resolve_environment_profile
from trace2world_pipeline.resources.resource_filter import filter_resources, load_judgments
from trace2world_pipeline.resources.resource_report import build_html, load_all_resources, load_filtered_resources, load_trace_index, merge_filtered_status


def _normalize_scope_for_payload(screenshot_scope: Optional[Dict[str, Set[str]]]) -> Dict[str, List[str]]:
    return {
        str(trace_id): sorted(str(item) for item in items)
        for trace_id, items in sorted((screenshot_scope or {}).items())
    }


def _build_all_trace_screenshot_scope(mt_resource_root: Path, traces: List[Dict]) -> Dict[str, Any]:
    trace_screenshots: Dict[str, List[str]] = {}
    for trace in traces:
        trace_id = str(trace.get("id") or "").strip()
        trace_dir = mt_resource_root / str(trace.get("path") or trace_id)
        refs: List[str] = []
        trajectory_path = trace_dir / "trajectory.json"
        payload = _load_json_if_exists(trajectory_path)
        steps = payload.get("trajectory") if isinstance(payload, dict) else []
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                ref = str(step.get("screenshot") or "").strip()
                if ref and (trace_dir / ref).exists():
                    refs.append(ref)
        if not refs:
            screenshot_dir = trace_dir / "screenshots"
            refs.extend(
                f"screenshots/{path.name}"
                for path in sorted(screenshot_dir.glob("*"))
                if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            )
        trace_screenshots[trace_id] = sorted(dict.fromkeys(refs))
    return {
        "schema": "ghostweb.resource_extraction_scope.v1",
        "mode": "all_trace_screenshots",
        "trace_root": str(mt_resource_root),
        "trace_count": len(traces),
        "trace_screenshots": trace_screenshots,
        "selected_screenshot_count": sum(len(items) for items in trace_screenshots.values()),
    }


def _build_raw_resources_output(
    mt_resource_root: Path,
    traces: List[Dict],
    screenshot_scope: Optional[Dict[str, Set[str]]] = None,
) -> Dict:
    existing_path = mt_resource_root / "filtered_resources.json"
    existing_resources: List[Dict] = []
    existing_paths: Set[str] = set()


    if screenshot_scope is None and existing_path.is_file():
        try:
            existing_data = json.loads(existing_path.read_text(encoding="utf-8"))
            existing_resources = existing_data.get("resources", [])
            existing_paths = {str(r.get("path", "")) for r in existing_resources if r.get("path")}
        except Exception:
            pass

    new_resources: List[Dict] = []
    for trace in traces:
        trace_id = str(trace.get("id") or "")
        if screenshot_scope is not None and trace_id not in screenshot_scope:
            continue
        e2e_path = mt_resource_root / trace["id"] / "e2e_results.json"
        if not e2e_path.exists():
            continue
        data = json.loads(e2e_path.read_text(encoding="utf-8"))
        if screenshot_scope is not None:
            data = filter_e2e_payload_by_scope(data, screenshot_scope.get(trace_id, set()))
        for r in data.get("resources", []):
            path = r.get("path", "")
            if not path or path in existing_paths:
                continue
            existing_paths.add(path)
            metadata = r.get("metadata", {})
            new_resources.append({
                "path": path,
                "trace": trace["id"],
                "original_category": r.get("type", ""),
                "semantic_category": "",
                "width": metadata.get("width", 0),
                "height": metadata.get("height", 0),
                "confidence": 0,
                "reason": "",
                "suggested_usage": "",
            })

    merged = existing_resources + new_resources
    return {
        "total_input": len(merged),
        "usable_input": len(merged),
        "photo_size_recovered": 0,
        "mode": "raw_resources_incremental" if existing_resources else "raw_resources",
        "resource_scope": {
            "mode": "l0_selected_screenshots" if screenshot_scope else "all_trace_screenshots",
            "trace_screenshots": _normalize_scope_for_payload(screenshot_scope),
            "selected_screenshot_count": sum(len(items) for items in (screenshot_scope or {}).values()),
        },
        "constraints_applied": {},
        "final_count": len(merged),
        "resources": merged,
        "incremental_stats": {
            "previously_existing": len(existing_resources),
            "newly_added": len(new_resources),
        },
    }


def _trace_step_png(step_index: int) -> str:
    return f"step_{int(step_index):04d}.png"


def _trace_screenshot_candidates(trace_dir: Path, step_index: int) -> List[Path]:
    names = [
        f"step_{step_index:04d}.png",
        f"step_{step_index:04d}.jpg",
        f"step_{step_index:04d}.jpeg",
        f"step{step_index:02d}.png",
        f"step{step_index:02d}.jpg",
        f"action_{step_index:04d}.png",
        f"action_{step_index:04d}.jpg",
        f"action_{step_index:04d}.jpeg",
    ]
    candidates = [trace_dir / name for name in names]
    candidates.extend(trace_dir / "screenshots" / name for name in names)
    return candidates


def _screenshot_scope_ref(trace_dir: Path, step_index: int, preferred_ref: str = "") -> Optional[str]:
    preferred_text = str(preferred_ref or "").strip()
    preferred = Path(preferred_text) if preferred_text else None
    if preferred and preferred.exists():
        try:
            return preferred.resolve().relative_to(trace_dir.resolve()).as_posix()
        except ValueError:
            return str(preferred.resolve())
    for candidate in _trace_screenshot_candidates(trace_dir, step_index):
        if candidate.exists():
            try:
                return candidate.resolve().relative_to(trace_dir.resolve()).as_posix()
            except ValueError:
                return str(candidate.resolve())
    return None


def _load_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _global_reasoning_path(result_dir: Path) -> Optional[Path]:
    for path in (
        result_dir / "phase_global_reasoning" / "global_reasoning_final.json",
        result_dir / "phase_global_reasoning" / "debug" / "global_reasoning_feedback_fixed.json",
        result_dir / "phase_global_reasoning" / "debug" / "global_reasoning.json",
    ):
        if path.exists():
            return path

    debug_dir = result_dir / "data" / "debug"
    for name in (
        "global_reasoning_final.json",
        "global_reasoning_feedback_fixed.json",
        "global_reasoning.json",
    ):
        path = debug_dir / name
        if path.exists():
            return path
    return None


def _l0_visual_evidence_selection_path(result_dir: Path) -> Optional[Path]:
    for path in (
        result_dir / "phase_l0_fidelity" / "l0_visual_evidence_selection.json",
        result_dir / "data" / "site_model" / "l0_visual_evidence_selection.json",
    ):
        if path.exists():
            return path
    return None


def _step_from_filename(path: Path) -> Optional[int]:
    match = re.search(r"(?:step|action)[_-]?(\d+)", path.stem, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _trace_step_from_selected_ref(
    ref: str,
    *,
    mt_resource_root: Path,
    trace_dirs: Dict[str, Path],
) -> Optional[tuple[str, int]]:
    from trace2world_pipeline.page_realizer.pagegen_input_scope import step_key_from_screenshot_ref

    step_key = step_key_from_screenshot_ref(str(ref))
    if step_key and ":step" in step_key:
        trace_id, raw_step = step_key.rsplit(":step", 1)
        try:
            return trace_id, int(raw_step)
        except ValueError:
            pass

    ref_text = str(ref or "").strip()
    if not ref_text:
        return None
    ref_path = Path(ref_text)
    if not ref_path.is_absolute():
        ref_path = mt_resource_root / ref_path
    try:
        resolved = ref_path.resolve()
    except Exception:
        resolved = ref_path

    for trace_id, trace_dir in trace_dirs.items():
        try:
            resolved.relative_to(trace_dir.resolve())
        except ValueError:
            continue
        step_index = _step_from_filename(resolved)
        if step_index is None:
            return None
        return trace_id, step_index
    return None


def _build_scope_from_l0_visual_selection(
    mt_resource_root: Path,
    selection_path: Path,
) -> Optional[Dict[str, Any]]:
    payload = json.loads(selection_path.read_text(encoding="utf-8"))
    pages = payload.get("pages") if isinstance(payload, dict) else {}
    if not isinstance(pages, dict):
        return None

    root_index = json.loads((mt_resource_root / "index.json").read_text(encoding="utf-8"))
    trace_dirs = {
        str(item.get("id") or item.get("path") or ""): mt_resource_root / str(item.get("path") or item.get("id") or "")
        for item in root_index.get("traces", [])
        if isinstance(item, dict) and (item.get("id") or item.get("path"))
    }
    scope: Dict[str, Set[str]] = {}
    reasons: Dict[str, Dict[str, List[str]]] = {}
    page_count = 0
    for filename, page in sorted(pages.items()):
        if not isinstance(page, dict):
            continue
        refs = (
            page.get("selected_screenshot_refs")
            or (page.get("render_scope_contract") or {}).get("selected_screenshot_refs")
            or page.get("prompt_screenshot_refs")
            or []
        )
        if not isinstance(refs, list):
            continue
        page_count += 1
        for ref in refs:
            parsed = _trace_step_from_selected_ref(
                str(ref),
                mt_resource_root=mt_resource_root,
                trace_dirs=trace_dirs,
            )
            if not parsed:
                continue
            trace_id, step_index = parsed
            trace_dir = trace_dirs.get(trace_id, mt_resource_root / trace_id)
            screenshot_ref = _screenshot_scope_ref(trace_dir, step_index, preferred_ref=str(ref))
            if not screenshot_ref:
                continue
            scope.setdefault(trace_id, set()).add(screenshot_ref)
            reasons.setdefault(trace_id, {}).setdefault(screenshot_ref, []).append(
                f"l0_visual_evidence_selection:{filename}"
            )

    if not scope:
        return None
    return {
        "mode": "l0_visual_evidence_selection",
        "selection_source": str(selection_path),
        "trace_screenshots": {
            trace_id: sorted(items)
            for trace_id, items in sorted(scope.items())
        },
        "reasons": reasons,
        "summary": {
            "page_count": page_count,
            "trace_count": len(scope),
            "selected_screenshot_count": sum(len(items) for items in scope.values()),
        },
    }


def build_l0_screenshot_scope(mt_resource_root: Path, result_dir: Path) -> Dict[str, Any]:
    selection_path = _l0_visual_evidence_selection_path(result_dir)
    if selection_path:
        selection_scope = _build_scope_from_l0_visual_selection(mt_resource_root, selection_path)
        if selection_scope:
            return selection_scope

    gr_path = _global_reasoning_path(result_dir)
    if not gr_path:
        raise FileNotFoundError(f"No global_reasoning artifact found under {result_dir}")
    global_reasoning = json.loads(gr_path.read_text(encoding="utf-8"))
    skipped = {
        (str(item.get("trace_id")), int(item.get("step_index")))
        for item in global_reasoning.get("skipped_steps", [])
        if isinstance(item, dict) and item.get("trace_id") is not None and item.get("step_index") is not None
    }
    scope: Dict[str, Set[str]] = {}
    reasons: Dict[str, Dict[str, List[str]]] = {}

    def add(trace_id: str, step_index: int, reason: str, preferred_ref: str = "") -> None:
        if (trace_id, step_index) in skipped:
            return
        trace_dir = mt_resource_root / trace_id
        screenshot_ref = _screenshot_scope_ref(trace_dir, step_index, preferred_ref=preferred_ref)
        if not screenshot_ref:
            return
        scope.setdefault(trace_id, set()).add(screenshot_ref)
        reasons.setdefault(trace_id, {}).setdefault(screenshot_ref, []).append(reason)

    site_model_dir = result_dir / "data" / "site_model"
    if site_model_dir.exists():
        from trace2world_pipeline.page_realizer.pagegen_input_scope import build_pagegen_input_scopes, step_key_from_screenshot_ref
        from trace2world_pipeline.app_spec.model import AppSpec

        site_model = AppSpec.load(str(site_model_dir))
        final_page_inputs = site_model.get_final_page_inputs()
        pagegen_scopes = build_pagegen_input_scopes(final_page_inputs, [], fidelity_mode="l0")
        for filename, page_scope in pagegen_scopes.items():
            selected_refs = (
                (page_scope.get("render_scope_contract") or {}).get("selected_screenshot_refs")
                or page_scope.get("prompt_screenshot_refs")
                or []
            )
            for ref in selected_refs:
                step_key = step_key_from_screenshot_ref(str(ref))
                if not step_key or ":step" not in step_key:
                    continue
                trace_id, raw_step = step_key.rsplit(":step", 1)
                try:
                    add(trace_id, int(raw_step), f"pagegen_l0_render_scope:{filename}", preferred_ref=str(ref))
                except Exception:
                    continue
        if scope:
            return {
                "mode": "l0_selected_screenshots",
                "selection_source": "site_model_final_page_inputs",
                "source_global_reasoning": str(gr_path),
                "trace_screenshots": {
                    trace_id: sorted(items)
                    for trace_id, items in sorted(scope.items())
                },
                "reasons": reasons,
                "summary": {
                    "trace_count": len(scope),
                    "selected_screenshot_count": sum(len(items) for items in scope.values()),
                    "skipped_step_count": len(skipped),
                },
            }

    for item in global_reasoning.get("step_labels", []):
        if not isinstance(item, dict):
            continue
        trace_id = str(item.get("trace_id") or "")
        step_index = item.get("step_index")
        if trace_id and step_index is not None:
            add(trace_id, int(step_index), "global_reasoning_step_label")

    root_index = json.loads((mt_resource_root / "index.json").read_text(encoding="utf-8"))
    excluded_action_types = {"scroll", "swipe", "drag", "wait", "finished", "finish", "noop", "no_op"}
    for trace in root_index.get("traces", []):
        trace_id = str(trace.get("id") or trace.get("path") or "")
        if not trace_id:
            continue
        trace_dir = mt_resource_root / trace_id
        step_files = sorted(trace_dir.glob("step_*.json"))
        max_step = len(step_files) - 1
        for step_file in step_files:
            payload = _load_json_if_exists(step_file) or {}
            step_index = int(payload.get("step_index", step_file.stem.split("_")[-1]))
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            action_type = str(action.get("action_type") or "").strip().lower()
            if not action_type or action_type in excluded_action_types:
                continue
            add(trace_id, step_index, f"l0_action_source:{action_type}")
            if step_index < max_step:
                add(trace_id, step_index + 1, f"l0_action_post_state:{action_type}")

    scope_payload = {
        "mode": "l0_selected_screenshots",
        "source_global_reasoning": str(gr_path),
        "trace_screenshots": {
            trace_id: sorted(items)
            for trace_id, items in sorted(scope.items())
        },
        "reasons": reasons,
        "summary": {
            "trace_count": len(scope),
            "selected_screenshot_count": sum(len(items) for items in scope.values()),
            "skipped_step_count": len(skipped),
        },
    }
    return scope_payload


def load_screenshot_scope(path: Optional[str]) -> Dict[str, Set[str]]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = payload.get("trace_screenshots", payload)
    return {
        str(trace_id): {str(item) for item in items}
        for trace_id, items in raw.items()
        if isinstance(items, list)
    }


def build_filtered_resource_scope_contract(
    scope_path: Optional[str],
    screenshot_scope: Dict[str, Set[str]],
) -> Dict[str, Any]:
    source_payload: Dict[str, Any] = {}
    if scope_path:
        payload = json.loads(Path(scope_path).read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            source_payload = payload
    selection_mode = str(source_payload.get("mode") or "provided_screenshot_scope")
    normalized = _normalize_scope_for_payload(screenshot_scope)
    contract = {


        "mode": "l0_selected_screenshots",
        "trace_screenshots": normalized,
        "trace_count": len(normalized),
        "selected_screenshot_count": sum(len(items) for items in normalized.values()),
        "scope_path": str(Path(scope_path).resolve()) if scope_path else "",
    }
    if selection_mode != "l0_selected_screenshots":
        contract["selection_mode"] = selection_mode
    return contract


def filter_e2e_payload_by_scope(payload: Dict, allowed_screenshots: Optional[Set[str]]) -> Dict:
    if allowed_screenshots is None:
        return payload
    allowed = {str(item).replace("\\", "/") for item in allowed_screenshots}
    trace_dir = str(payload.get("trace_dir") or "")
    trace_id = str(payload.get("trace_id") or "")
    if not trace_dir and trace_id:
        trace_dir = trace_id
    allowed_tokens = set(allowed)
    for item in allowed:
        allowed_tokens.update(ScreenshotResourceExtractor._screenshot_match_tokens(item, trace_dir or "."))

    def keep_source(source: str) -> bool:
        return bool(ScreenshotResourceExtractor._screenshot_match_tokens(source, trace_dir or ".") & allowed_tokens)

    payload = dict(payload)
    payload["resources"] = [
        item for item in payload.get("resources", [])
        if isinstance(item, dict) and keep_source(str(item.get("source") or ""))
    ]
    payload["total_resources"] = len(payload["resources"])
    payload["resource_scope"] = {
        "mode": "l0_selected_screenshots",
        "selected_screenshots": sorted(allowed_screenshots),
    }
    return payload


def screenshot_step_dir_name(screenshot_path: str) -> str:
    fname = Path(screenshot_path).name
    stem = Path(screenshot_path).stem
    if stem.startswith("step") and len(stem) >= 6 and stem[4:6].isdigit():
        return stem[:6]
    return stem


def load_mt_resource_traces(mt_resource_root: Path) -> List[Dict]:
    root_index = json.loads((mt_resource_root / "index.json").read_text(encoding="utf-8"))
    traces = []
    for item in root_index.get("traces", []):
        trace_dir = mt_resource_root / item["path"]
        traces.append(
            {
                "id": item["id"],
                "path": item["path"],
                "description_en": item.get("description_en", ""),
                "trace_dir": trace_dir,
            }
        )
    return traces


def select_traces_for_screenshot_scope(
    traces: List[Dict],
    screenshot_scope: Optional[Dict[str, Set[str]]],
) -> List[Dict]:
    if screenshot_scope is None:
        return list(traces)
    return [
        trace
        for trace in traces
        if str(trace.get("id") or "") in screenshot_scope
    ]


def _e2e_result_successful(path: Path, allowed_screenshots: Optional[Set[str]] = None) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    mineru = data.get("mineru", {}) if isinstance(data, dict) else {}
    resources = data.get("resources", []) if isinstance(data, dict) else []
    if mineru.get("error") or not resources:
        return False
    if allowed_screenshots:
        recorded = (
            (data.get("resource_scope") or {}).get("selected_screenshots")
            if isinstance(data, dict)
            else []
        )
        if recorded:
            recorded_set = {str(item) for item in recorded}
            if not {str(item) for item in allowed_screenshots}.issubset(recorded_set):
                return False
    return any(Path(str(item.get("path") or "")).exists() for item in resources if isinstance(item, dict))


def reuse_successful_resource_results(
    mt_resource_root: Path,
    reuse_roots: List[Path],
    traces: List[Dict],
    logger: Trace2WorldLogger,
    screenshot_scope: Optional[Dict[str, Set[str]]] = None,
) -> int:
    reused = 0
    for trace in traces:
        trace_id = trace["id"]
        if screenshot_scope is not None and trace_id not in screenshot_scope:
            continue
        target = mt_resource_root / trace["path"] / "e2e_results.json"
        allowed_screenshots = (screenshot_scope or {}).get(trace_id)
        if _e2e_result_successful(target, allowed_screenshots):
            continue
        for reuse_root in reuse_roots:
            source = reuse_root / trace_id / "e2e_results.json"
            if not _e2e_result_successful(source, allowed_screenshots):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload = filter_e2e_payload_by_scope(payload, (screenshot_scope or {}).get(trace_id))
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            reused += 1
            logger.log_info(f"[StaticResourcePipeline] Reused {source} -> {target}")
            break
    return reused


def build_runtime_config(config_path: str | None, args) -> Trace2WorldConfigManager:
    config = Trace2WorldConfigManager(config_path) if config_path else Trace2WorldConfigManager()
    if not config.get("mineru_api_key"):
        config.set("mineru_api_key", os.environ.get("MINERU_API_KEY", ""))
    config.set("mineru_api_url", args.mineru_api_url or config.get("mineru_api_url", "https://mineru.net"))
    config.set("mineru_timeout", args.mineru_timeout or config.get("mineru_timeout", 300))
    config.set("mineru_max_concurrent", args.mineru_max_concurrent or config.get("mineru_max_concurrent", 2))
    config.set(
        "mineru_file_rate_limit",
        args.mineru_file_rate_limit
        if args.mineru_file_rate_limit is not None
        else config.get("mineru_file_rate_limit", 50),
    )
    config.set(
        "mineru_file_rate_window_seconds",
        args.mineru_file_rate_window_seconds
        if args.mineru_file_rate_window_seconds is not None
        else config.get("mineru_file_rate_window_seconds", 60),
    )
    config.set(
        "mineru_rate_limit_retry_max_attempts",
        args.mineru_rate_limit_retry_max_attempts
        if args.mineru_rate_limit_retry_max_attempts is not None
        else config.get("mineru_rate_limit_retry_max_attempts", 6),
    )
    config.set(
        "mineru_rate_limit_retry_sleep",
        args.mineru_rate_limit_retry_sleep
        if args.mineru_rate_limit_retry_sleep is not None
        else config.get("mineru_rate_limit_retry_sleep", 65),
    )
    config.set(
        "mineru_rate_limit_state_path",
        args.mineru_rate_limit_state_path or config.get("mineru_rate_limit_state_path", ""),
    )
    config.set(
        "mineru_upload_max_concurrent",
        args.mineru_upload_max_concurrent or config.get("mineru_upload_max_concurrent", 5),
    )
    config.set(
        "mineru_download_max_concurrent",
        args.mineru_download_max_concurrent or config.get("mineru_download_max_concurrent", 5),
    )
    config.set("mineru_poll_interval", args.mineru_poll_interval or config.get("mineru_poll_interval", 5))
    configure_load_balancing(endpoints=config.get("endpoints"), deployment=config.get("deployment"))
    configure_stage_model_routing(config.get_stage_model_routing())
    return config


async def extract_trace(
    trace_info: Dict,
    extractor: ScreenshotResourceExtractor,
    judge: Optional[ResourceJudge],
    logger: Trace2WorldLogger,
    screenshot_scope: Optional[Dict[str, Set[str]]] = None,
) -> Dict:
    trace_dir = trace_info["trace_dir"]
    trace_id = trace_info["id"]
    if screenshot_scope is not None and trace_id not in screenshot_scope:
        logger.log_info(
            f"[StaticResourcePipeline] Excluded {trace_id}: absent from explicit screenshot scope"
        )
        return {
            "trace_id": trace_id,
            "resources": 0,
            "judged": 0,
            "error": None,
            "excluded_by_scope": True,
        }
    allowed_screenshots = (screenshot_scope or {}).get(trace_id)
    existing_e2e = trace_dir / "e2e_results.json"
    if _e2e_result_successful(existing_e2e, allowed_screenshots):
        logger.log_info(f"[StaticResourcePipeline] Skip existing successful {existing_e2e}")
        data = json.loads(existing_e2e.read_text(encoding="utf-8"))
        if allowed_screenshots:
            data = filter_e2e_payload_by_scope(data, allowed_screenshots)
            existing_e2e.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "trace_id": trace_id,
            "resources": len(data.get("resources", [])),
            "judged": len(data.get("judgments", [])),
            "error": None,
            "reused_or_existing": True,
        }
    resources_dir = trace_dir / "resources"
    resources_dir.mkdir(parents=True, exist_ok=True)

    extraction = await extractor.extract_from_trace(
        str(trace_dir),
        str(resources_dir),
        allowed_screenshots=allowed_screenshots,
    )
    resources = extraction.resources
    judgments = []
    if judge and resources:
        judgments = await judge.batch_judge(resources, {"website_type": "bilibili"})

    result_payload = {
        "trace": trace_id,
        "trace_description": trace_info.get("description_en", ""),
        "screenshot_count": extraction.screenshot_count,
        "total_resources": len(resources),
        "mineru": {
            "batch_id": extraction.mineru_batch_id,
            "request": extraction.mineru_request,
            "extract_result": extraction.mineru_extract_result,
            "error": extraction.error,
        },
        "resource_scope": {
            "mode": "l0_selected_screenshots" if allowed_screenshots else "all_trace_screenshots",
            "selected_screenshots": sorted(allowed_screenshots) if allowed_screenshots else [],
        },
        "resources": [
            {
                "path": r.resource_path,
                "source": r.source_screenshot,
                "type": r.resource_type,
                "metadata": r.metadata,
            }
            for r in resources
        ],
        "judgments": [
            {
                "path": j.resource_path,
                "usable": j.usable,
                "confidence": j.confidence,
                "category": j.category,
                "reason": j.reason,
                "suggested_usage": j.suggested_usage,
                "has_state_indicator": j.has_state_indicator,
                "overlapping_component_count": j.overlapping_component_count,
            }
            for j in judgments
        ],
    }

    e2e_path = trace_dir / "e2e_results.json"
    e2e_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.log_info(f"[StaticResourcePipeline] Wrote {e2e_path}")

    return {
        "trace_id": trace_id,
        "resources": len(resources),
        "judged": len(judgments),
        "error": extraction.error,
    }


async def run_pipeline(args) -> Dict:
    mt_resource_root = Path(args.mt_resource).resolve()
    traces = load_mt_resource_traces(mt_resource_root)
    config = build_runtime_config(args.config, args)
    environment_profile = resolve_environment_profile(config.get("environment_profile"))
    logger = Trace2WorldLogger()
    scope_path = args.screenshot_scope
    scope_summary: Dict[str, Any] = {}
    if args.l0_scope_from_result:
        scope_payload = build_l0_screenshot_scope(mt_resource_root, Path(args.l0_scope_from_result).resolve())
        scope_summary = scope_payload.get("summary", {}) if isinstance(scope_payload.get("summary"), dict) else {}
        scope_out = mt_resource_root / "resource_extraction_scope.json"
        scope_out.write_text(json.dumps(scope_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        scope_path = str(scope_out)
        logger.log_info(
            "[StaticResourcePipeline] Built L0 screenshot scope: "
            f"{scope_payload.get('summary', {})}"
        )
    elif not scope_path:
        scope_payload = _build_all_trace_screenshot_scope(mt_resource_root, traces)
        scope_summary = {
            "mode": scope_payload["mode"],
            "trace_count": scope_payload["trace_count"],
            "selected_screenshot_count": scope_payload["selected_screenshot_count"],
        }
        scope_out = mt_resource_root / "resource_extraction_scope.json"
        scope_out.write_text(json.dumps(scope_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        scope_path = str(scope_out)
        logger.log_info(
            "[StaticResourcePipeline] Built all-trace screenshot scope: "
            f"{scope_summary}"
        )
    screenshot_scope = load_screenshot_scope(scope_path)
    if scope_path and not screenshot_scope:
        raise ValueError(f"Declared screenshot scope is empty: {scope_path}")
    scoped_traces = select_traces_for_screenshot_scope(traces, screenshot_scope)
    reuse_roots = [Path(path).resolve() for path in args.reuse_resource_root or []]
    reused_count = (
        reuse_successful_resource_results(mt_resource_root, reuse_roots, scoped_traces, logger, screenshot_scope)
        if reuse_roots else 0
    )
    extractor = ScreenshotResourceExtractor(config, logger)


    judge: Optional[ResourceJudge] = None
    if args.use_filter:
        judge = ResourceJudge(
            logger=logger,
            model=args.judge_model or config.get("deployment"),
            reasoning_effort=args.judge_reasoning_effort,
            max_concurrent=args.resource_judge_max_concurrent,
            environment_profile=environment_profile,
        )

    semaphore = asyncio.Semaphore(config.get("mineru_max_concurrent", 2))

    async def _run_one(trace_info: Dict):
        async with semaphore:
            return await extract_trace(trace_info, extractor, judge, logger, screenshot_scope)

    trace_results = await asyncio.gather(*[_run_one(trace) for trace in scoped_traces])
    existing_or_reused_count = sum(
        1
        for item in trace_results
        if isinstance(item, dict) and item.get("reused_or_existing") is True
    )
    newly_extracted_count = sum(
        1
        for item in trace_results
        if isinstance(item, dict)
        and item.get("reused_or_existing") is not True
        and not item.get("error")
    )
    error_count = sum(1 for item in trace_results if isinstance(item, dict) and item.get("error"))
    total_resources = sum(
        int(item.get("resources") or 0)
        for item in trace_results
        if isinstance(item, dict)
    )

    filtered_resources_path = mt_resource_root / "filtered_resources.json"

    if args.use_filter:

        judgments = load_judgments(str(mt_resource_root))[0]
        judgments = [
            item
            for item in judgments
            if str(item.get("_trace") or "") in screenshot_scope
        ]
        filter_result = filter_resources(judgments)
    else:


        filter_result = _build_raw_resources_output(mt_resource_root, scoped_traces, screenshot_scope)

    filter_result["resource_scope"] = build_filtered_resource_scope_contract(
        scope_path,
        screenshot_scope,
    )

    filtered_resources_path.write_text(
        json.dumps(filter_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    filtered_resources, meta = load_filtered_resources(str(filtered_resources_path))
    trace_ids = [trace["id"] for trace in scoped_traces]
    trace_steps = {trace_id: load_trace_index(str(mt_resource_root), trace_id) for trace_id in trace_ids}
    grouped = merge_filtered_status(load_all_resources(str(mt_resource_root), trace_ids), filtered_resources)
    preview_path = mt_resource_root / "resource_preview.html"
    preview_html = build_html(str(mt_resource_root), trace_ids, trace_steps, grouped, meta, str(preview_path))
    preview_path.write_text(preview_html, encoding="utf-8")

    return {
        "trace_results": trace_results,
        "trace_count": len(trace_results),
        "existing_or_reused_count": existing_or_reused_count,
        "newly_extracted_count": newly_extracted_count,
        "error_count": error_count,
        "total_resources": total_resources,
        "reused_count": reused_count,
        "screenshot_scope_path": scope_path,
        "screenshot_scope_summary": scope_summary,
        "filtered_resources_path": str(filtered_resources_path),
        "resource_preview_path": str(preview_path),
        "final_count": filter_result["final_count"],
        "platform": environment_profile["platform"],
        "environment_profile_hash": environment_profile["profile_hash"],
    }


def main():
    parser = argparse.ArgumentParser(description="Run MinerU extraction + optional static resource filtering for a flat mt_resource root")
    parser.add_argument("--mt-resource", required=True, help="Flat mt_resource root, e.g. ./b_trace")
    parser.add_argument("--config", default=None, help="Optional config JSON file")
    parser.add_argument("--filter", dest="use_filter", action="store_true", default=False,
                        help="Enable judgment + full resource_filter.py quality gates. Default: extract only.")
    parser.add_argument("--mineru-api-url", default=None)
    parser.add_argument("--mineru-timeout", type=int, default=None)
    parser.add_argument("--mineru-max-concurrent", type=int, default=None)
    parser.add_argument(
        "--mineru-file-rate-limit",
        type=int,
        default=None,
        help="Cross-process MinerU file-urls limit per window. Use 0 to disable. Default: 50.",
    )
    parser.add_argument(
        "--mineru-file-rate-window-seconds",
        type=float,
        default=None,
        help="MinerU file rate-limit window in seconds. Default: 60.",
    )
    parser.add_argument(
        "--mineru-rate-limit-retry-max-attempts",
        type=int,
        default=None,
        help="Retry attempts for MinerU 429 file-urls submissions. Default: 6.",
    )
    parser.add_argument(
        "--mineru-rate-limit-retry-sleep",
        type=float,
        default=None,
        help="Minimum sleep before retrying a MinerU 429 submission. Default: 65.",
    )
    parser.add_argument(
        "--mineru-rate-limit-state-path",
        default=None,
        help="Shared limiter state path for coordinating multiple extraction processes.",
    )
    parser.add_argument("--mineru-upload-max-concurrent", type=int, default=None)
    parser.add_argument("--mineru-download-max-concurrent", type=int, default=None)
    parser.add_argument("--mineru-poll-interval", type=int, default=5)
    parser.add_argument(
        "--reuse-resource-root",
        action="append",
        default=[],
        help="Reuse successful e2e_results.json from another mt_resource root before submitting MinerU jobs. Can be repeated.",
    )
    parser.add_argument(
        "--screenshot-scope",
        default=None,
        help="JSON mapping trace id to selected screenshots, or an object with trace_screenshots.",
    )
    parser.add_argument(
        "--l0-scope-from-result",
        default=None,
        help="Build selected screenshot scope from a AppSpec result directory before extraction.",
    )
    parser.add_argument("--resource-judge-max-concurrent", type=int, default=6)
    parser.add_argument("--judge-model", default=None, help="Defaults to the config deployment model")
    parser.add_argument("--judge-reasoning-effort", default="low")
    args = parser.parse_args()

    result = asyncio.run(run_pipeline(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
