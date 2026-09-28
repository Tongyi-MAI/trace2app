

from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any


from trace2world_pipeline.core.llm_caller import call_openai_api_json_async, encode_image
from trace2world_pipeline.app_spec.reconcile.page_identity_reconciliation import (
    build_page_identity_index,
    build_page_identity_reconciliation_report,
)
from trace2world_pipeline.app_spec.reconcile.route_graph_audit import build_audit

try:
    from PIL import Image
except ImportError:
    Image = None


def derive_page_assignment_audit_fanout(
    task_count: int,
    requested_max_concurrent: int = 0,
) -> int:
    if task_count <= 0:
        return 1
    if requested_max_concurrent > 0:
        return max(1, min(task_count, int(requested_max_concurrent)))
    return max(1, min(task_count, int(math.ceil(math.sqrt(task_count)))))


def derive_page_assignment_audit_batch_size(
    requested_batch_size: int,
    candidate_page_count: int,
    *,
    pair_budget: int = 256,
) -> int:
    requested = max(1, int(requested_batch_size or 1))
    budget = int(pair_budget or 0)
    if budget <= 0:
        return requested
    candidates = max(1, int(candidate_page_count or 1))
    pair_limited = max(1, budget // candidates)
    return max(1, min(requested, pair_limited))


def build_audit_tasks(
    *,
    data: dict[str, Any],
    target_ids: list[str],
    all_pages: bool,
    limit: int,
    requested_batch_size: int,
    candidate_page_count: int,
    pair_budget: int,
    batch_id: str,
) -> tuple[list[tuple[str, str, int, int]], int]:
    effective_batch_size = derive_page_assignment_audit_batch_size(
        requested_batch_size,
        candidate_page_count,
        pair_budget=pair_budget,
    )
    tasks: list[tuple[str, str, int, int]] = []
    for page_id in target_ids:
        step_count = sum(1 for step in audit_steps(data) if str(step.get("pageId", "") or "") == page_id)
        for batch_index, (offset, count) in enumerate(chunks(step_count, effective_batch_size)):
            if limit > 0 and not all_pages and offset >= limit:
                continue
            effective_count = count
            if limit > 0 and not all_pages:
                effective_count = min(count, limit - offset)
            if effective_count <= 0:
                continue
            effective_batch_id = batch_id if len(target_ids) == 1 and batch_index == 0 else f"batch_{batch_index:03d}"
            tasks.append((page_id, effective_batch_id, offset, effective_count))
    return tasks, effective_batch_size


async def run_tasks_bounded(
    tasks: list[tuple[str, str, int, int]],
    *,
    max_concurrent: int,
    runner,
) -> list[dict[str, Any]]:
    if not tasks:
        return []
    fanout = derive_page_assignment_audit_fanout(len(tasks), max_concurrent)
    semaphore = asyncio.Semaphore(fanout)
    results: list[dict[str, Any] | None] = [None] * len(tasks)

    async def _run(index: int, task: tuple[str, str, int, int]) -> None:
        async with semaphore:
            results[index] = await runner(*task)

    await asyncio.gather(*[_run(index, task) for index, task in enumerate(tasks)])
    return [result for result in results if result is not None]


def load_audit_data(audit_html: Path) -> dict[str, Any]:
    text = audit_html.read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="audit-data">(.*?)</script>', text, re.S)
    if not match:
        raise ValueError(f"audit-data script not found in {audit_html}")
    return json.loads(match.group(1))


def default_reasoning_json(result_root: Path) -> Path:
    return result_root / "data" / "debug" / "global_reasoning_final.json"


def build_audit_data_from_reasoning(
    result_root: Path,
    trace_root: Path,
    reasoning_json: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    if not reasoning_json.exists():
        raise FileNotFoundError(f"global reasoning JSON not found: {reasoning_json}")
    reports_dir.mkdir(parents=True, exist_ok=True)
    data = build_audit(
        result_root.expanduser().resolve(),
        trace_root.expanduser().resolve(),
        reports_dir.expanduser().resolve(),
        reasoning_json.expanduser().resolve(),
    )
    if not isinstance(data, dict):
        raise ValueError(f"Invalid route graph audit data built from {reasoning_json}")
    return data


def build_audit_data_from_reasoning_payload(
    result_root: Path,
    trace_root: Path,
    reasoning: dict[str, Any],
    reports_dir: Path,
    reasoning_label: str = "<in_memory>",
) -> dict[str, Any]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    data = build_audit(
        result_root.expanduser().resolve(),
        trace_root.expanduser().resolve(),
        reports_dir.expanduser().resolve(),
        None,
        global_reasoning_payload=reasoning,
        global_reasoning_label=reasoning_label,
    )
    if not isinstance(data, dict):
        raise ValueError("Invalid route graph audit data built from in-memory reasoning")
    return data


def compact_page(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_cluster_id": page.get("id") or page.get("page_cluster_id"),
        "page_name": page.get("name") or page.get("page_name"),
        "description": page.get("description", ""),
        "route_path": page.get("routePath") or page.get("route_path", ""),
        "parent_route_path": page.get("parentRoutePath") or page.get("parent_route_path", ""),
        "route_level": page.get("routeLevel") or page.get("route_level", ""),
        "step_count": page.get("stepCount") or page.get("step_count", 0),
        "trace_count": page.get("traceCount") or page.get("trace_count", 0),
        "signatures": page.get("signatures", [])[:5],
    }


def audit_steps(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("steps"), list):
        return [step for step in data.get("steps", []) if isinstance(step, dict)]
    steps_by_key = data.get("stepsByKey", {})
    if isinstance(steps_by_key, dict):
        return [step for step in steps_by_key.values() if isinstance(step, dict)]
    return []


def trace_step_maps(trace_root: Path) -> dict[str, dict[int, dict[str, Any]]]:
    maps: dict[str, dict[int, dict[str, Any]]] = {}
    for trace_dir in sorted(p for p in trace_root.iterdir() if p.is_dir()):
        step_map: dict[int, dict[str, Any]] = {}
        for step_path in sorted(trace_dir.glob("step_*.json")):
            try:
                data = json.loads(step_path.read_text(encoding="utf-8"))
                idx = int(data.get("step_index", step_path.stem.split("_")[-1]))
            except Exception:
                continue
            step_map[idx] = data
        if step_map:
            maps[trace_dir.name] = step_map
    return maps


def parse_step_key(key: str) -> tuple[str, int]:
    trace_id, _, index = key.rpartition(":")
    return trace_id, int(index)


def action_to_text(action: Any) -> str:
    if isinstance(action, dict):
        kind = action.get("action_type") or action.get("type") or ""
        parts = [str(kind)]
        if action.get("text"):
            parts.append(f"text={action.get('text')}")
        if action.get("app_name"):
            parts.append(f"app={action.get('app_name')}")
        if action.get("x") is not None and action.get("y") is not None:
            parts.append(f"xy=({action.get('x')},{action.get('y')})")
        return ", ".join(p for p in parts if p)
    return str(action or "")


def action_looks_like_text_entry(text: Any) -> bool:
    value = str(text or "").strip().lower()
    if not value:
        return False
    markers = (
        "type",
        "fill",
        "input",
        "text=",
        "输入",
        "键入",
        "填写",
    )
    return any(marker in value for marker in markers)


def search_result_like_text(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values).lower()
    markers = (
        "search_results",
        "search result",
        "search-results",
        "results",
        "result list",
        "query",
        "suggestion",
        "autocomplete",
        "检索结果",
        "搜索结果",
        "查询结果",
        "联想",
        "自动推荐",
        "候选",
        "结果列表",
    )
    return any(marker in text for marker in markers)


def explicit_transition_override_evidence(decision: dict[str, Any]) -> list[str]:
    if decision.get("override_transition_hint") is not True:
        return []
    evidence: list[str] = []
    for key in (
        "route_or_shell_evidence",
        "transition_override_evidence",
        "override_evidence",
    ):
        raw = decision.get(key)
        if isinstance(raw, list):
            evidence.extend(str(item).strip() for item in raw if str(item).strip())
        elif str(raw or "").strip():
            evidence.append(str(raw).strip())
    return evidence


def should_block_search_typeahead_move(decision: dict[str, Any], label: dict[str, Any]) -> tuple[bool, str]:
    transition_hint = decision.get("transition_hint") or {}
    if not isinstance(transition_hint, dict):
        transition_hint = {}
    transition_from_previous = transition_hint.get("transition_from_previous")
    transition_kind = str(transition_hint.get("transition_kind", "") or "").strip()
    if transition_from_previous is not False and not transition_kind.startswith("same_page"):
        return False, ""
    previous_action = decision.get("previous_action") or transition_hint.get("previous_action", "")
    if not action_looks_like_text_entry(previous_action):
        return False, ""
    corrected_page = str(decision.get("corrected_page_id") or decision.get("proposed_new_page_id") or "")
    old_page = str(label.get("page_cluster_id", "") or "")
    if not search_result_like_text(
        corrected_page,
        old_page,
        decision.get("durable_page_identity", ""),
        decision.get("reason", ""),
        transition_hint.get("transition_evidence", []),
    ):
        return False, ""
    if explicit_transition_override_evidence(decision):
        return False, ""
    return True, "search_typeahead_move_requires_override_evidence"


def parse_prediction(prediction: str) -> tuple[str, str]:
    thought = ""
    action = ""
    thought_match = re.search(r"<thinking>\s*(.*?)\s*</thinking>", prediction or "", re.S)
    action_match = re.search(r"<action>\s*(.*?)\s*</action>", prediction or "", re.S)
    if thought_match:
        thought = " ".join(thought_match.group(1).split())
    if action_match:
        action = " ".join(action_match.group(1).split())
    return thought, action


def resolve_audit_screenshot_path(result_root: Path, reports_dir: Path, screenshot_ref: str) -> Path:
    ref = Path(str(screenshot_ref or ""))
    if ref.is_absolute():
        return ref.resolve()
    candidates = [
        reports_dir / ref,
        result_root / ref,
        Path.cwd() / ref,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def prepare_image(src: Path, dst: Path, size: tuple[int, int] = (1080, 1920)) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if Image is None:
        if not dst.exists():
            dst.symlink_to(src)
        return
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, "white")
        x = (size[0] - im.width) // 2
        y = (size[1] - im.height) // 2
        canvas.paste(im, (x, y))
        canvas.save(dst, format="JPEG", quality=88, optimize=True)


def _opened_overlay_hint_fields(
    result_root: Path, trace_id: str, step_index: int,
) -> dict[str, Any]:
    frame_obs_name = f"{trace_id}_step{step_index}.json"
    frame_obs_path = result_root / "phase_frame_analysis" / "frame_observations" / frame_obs_name
    if not frame_obs_path.is_file():
        return {}
    try:
        obs = json.loads(frame_obs_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    page_state = obs.get("page_state", {}) if isinstance(obs.get("page_state"), dict) else {}
    role = str(page_state.get("interaction_surface_role", "") or "none").strip()
    if role == "none":
        return {}
    return {
        "interaction_surface_role": role,
        "host_page_still_visible": bool(page_state.get("host_page_still_visible", False)),
        "host_page_identity": str(page_state.get("host_page_identity", "") or "").strip(),
        "opened_surface_identity": str(page_state.get("opened_surface_identity", "") or "").strip(),
    }


def phase1a_observation_path(result_root: Path, trace_id: str, step_index: int) -> Path:
    return result_root / "phase_frame_analysis" / "frame_observations" / f"{trace_id}_step{step_index}.json"


def compact_phase1a_observation(obs: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(obs, dict):
        raise ValueError("phase1a observation must be a JSON object")
    page_state = obs.get("page_state", {}) if isinstance(obs.get("page_state"), dict) else {}
    return {
        "description": str(obs.get("description", "") or "").strip(),
        "header_pattern": str(obs.get("header_pattern", "") or "").strip(),
        "shell_section": str(obs.get("shell_section", "") or "").strip(),
        "body_pattern": str(obs.get("body_pattern", "") or "").strip(),
        "sticky_action_pattern": str(obs.get("sticky_action_pattern", "") or "").strip(),
        "structure_signature": str(obs.get("structure_signature", "") or "").strip(),
        "content_signature": str(obs.get("content_signature", "") or "").strip(),
        "transition_from_previous": obs.get("transition_from_previous", page_state.get("transition_from_previous")),
        "transition_kind": str(obs.get("transition_kind", page_state.get("transition_kind", "")) or "").strip(),
        "transition_evidence": obs.get("transition_evidence") or page_state.get("transition_evidence", []),
        "interaction_surface_role": str(page_state.get("interaction_surface_role", "none") or "none").strip(),
        "host_page_still_visible": bool(page_state.get("host_page_still_visible", False)),
        "host_page_identity": str(page_state.get("host_page_identity", "") or "").strip(),
        "opened_surface_identity": str(page_state.get("opened_surface_identity", "") or "").strip(),
        "standalone_page_evidence": page_state.get("standalone_page_evidence", []),
        "host_page_evidence": page_state.get("host_page_evidence", []),
        "route_hints": obs.get("route_hints", {}) if isinstance(obs.get("route_hints"), dict) else {},
        "confidence": obs.get("confidence"),
        "identity_confidence": obs.get("identity_confidence"),
        "phase1a_action": str(obs.get("action", "") or "").strip(),
        "phase1a_thought": str(obs.get("thought", "") or "").strip(),
    }


def attach_phase1a_observations(items: list[dict[str, Any]], result_root: Path) -> list[dict[str, Any]]:
    for item in items:
        trace_id = str(item.get("trace_id", "") or "").strip()
        step_index = int(item.get("step_index"))
        path = phase1a_observation_path(result_root, trace_id, step_index)
        if not path.is_file():
            raise FileNotFoundError(f"phase1a observation missing: {path}")
        try:
            obs = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"phase1a observation is not valid JSON: {path}") from exc
        item["phase1a_observation_path"] = str(path)
        item["phase1a_observation"] = compact_phase1a_observation(obs)
    return items


def build_items(
    data: dict[str, Any],
    target_page_id: str,
    result_root: Path,
    reports_dir: Path,
    trace_steps: dict[str, dict[int, dict[str, Any]]],
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    node = next((n for n in data.get("nodes", []) if n.get("id") == target_page_id), None)
    if not node:
        raise ValueError(f"target page not found: {target_page_id}")
    assets_dir = reports_dir / f"page_assignment_audit_{target_page_id}_assets"
    items = []
    page_steps = [
        step for step in audit_steps(data)
        if isinstance(step, dict) and str(step.get("pageId", "") or "") == target_page_id
    ]
    page_steps.sort(key=lambda step: (
        str(step.get("traceId", "") or ""),
        int(step.get("stepIndex", 0) or 0),
    ))
    selected = page_steps[offset: offset + limit if limit > 0 else None]
    for idx, example in enumerate(selected, start=offset + 1):
        key = str(example.get("key") or "")
        trace_id, step_index = parse_step_key(key)
        step_json = trace_steps.get(trace_id, {}).get(step_index, {})
        prev_json = trace_steps.get(trace_id, {}).get(step_index - 1, {})
        prev_thought, prev_action = parse_prediction(str(prev_json.get("prediction", "")))
        cur_thought, cur_action = parse_prediction(str(step_json.get("prediction", "")))
        screenshot_rel = str(example.get("screenshot") or "")
        screenshot_src = resolve_audit_screenshot_path(result_root, reports_dir, screenshot_rel)
        tile_id = f"H{idx:02d}"
        image_name = f"{tile_id}_{trace_id}_step{step_index}_clean.jpg"
        image_dst = assets_dir / image_name
        prepare_image(screenshot_src, image_dst)
        item = {
            "tile_id": tile_id,
            "trace_id": trace_id,
            "step_index": step_index,
            "key": key,
            "image_path": image_dst,
            "image_rel": str(image_dst.relative_to(reports_dir)),
            "task_instruction": step_json.get("instruction") or prev_json.get("instruction") or "",
            "previous_thought": prev_thought,
            "previous_action": action_to_text(prev_json.get("action")) or str((example.get("prev") or {}).get("action", "") or ""),
            "current_thought": cur_thought or str(example.get("action", "")),
            "current_action": action_to_text(step_json.get("action")) or str(example.get("rawAction", "")),
            "transition_hint": {
                "transition_from_previous": (example.get("observation") or {}).get("transitionFromPrevious"),
                "transition_kind": (example.get("observation") or {}).get("transitionKind", ""),
                "transition_evidence": (example.get("observation") or {}).get("transitionEvidence", []),
                "source": "phase_frame_analysis.frame_observations",
                "is_ground_truth": False,
                **_opened_overlay_hint_fields(result_root, trace_id, step_index),
            },
            "current_assignment": {
                "page_cluster_id": example.get("pageId") or target_page_id,
                "step_index": step_index,
                "trace_id": trace_id,
            },
        }
        items.append(item)
    return items


def chunks(total: int, size: int) -> list[tuple[int, int]]:
    return [(start, min(size, total - start)) for start in range(0, total, size)]


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    return value


def build_prompt(
    target_page: dict[str, Any],
    candidates: list[dict[str, Any]],
    items: list[dict[str, Any]],
    batch_id: str,
) -> list[dict[str, Any]]:
    instructions = """You are auditing page assignment for a AppSpec reasoning artifact.

The screenshot visual state is authoritative. Task/thought/action text is context only and may be noisy.

For each IMAGE Hxx, decide one of:
- keep: the screenshot belongs to the target page.
- move_existing: the screenshot is assigned to the wrong page, and one existing candidate page in CANDIDATE PAGE CATALOG is the correct page.
- create_new_page: the screenshot is assigned to the wrong page, and no existing candidate page fits. Create a new page proposal and assign the screenshot there.
- skip: the screenshot is not an app/page state that should enter AppSpec, e.g. phone launcher, transient loading, OS chrome, or explicit no-op retry. A launch-time splash or interstitial ad whose only task action is close/skip/dismiss is a transient launch gate, not a durable page identity.
- uncertain: not enough visual evidence.

Important:
- A retained observation-only frame is page evidence, not an executable action. If metadata or Phase1a page_state says observation_role=terminal_observation and observation_disposition=retain, skip is forbidden. Use keep, move_existing, or create_new_page according to the visible durable page identity; use uncertain only when the evidence cannot distinguish those choices.
- Do NOT force a wrong screenshot into the closest existing candidate. If the visible layout/intent is coherent but absent from the candidate catalog, use create_new_page.
- Separate page identity from runtime state. A screen that only opens, configures, confirms, dismisses, or applies state for an underlying page is not a standalone page, even if it visually looks full-screen. Use the screenshot together with previous/current thought/action to identify the underlying owner page. If the owner is the target page, keep; if the owner is another existing candidate, move_existing; if the owner is missing from the catalog, create_new_page for that owner page, not for the transient surface itself.
- Frame transition hints are structured evidence, not ground truth. If you override a transition hint, cite concrete route, shell, or app-stack evidence in route_or_shell_evidence and set override_transition_hint=true.
- For search/query flows: clicking or focusing a search input may navigate to a dedicated search-entry page. Typing into a search input usually updates the same page's query projection, suggestions, autocomplete list, or inline results. Do not move/create a search_results page solely because typed-query results are visible. Treat a submitted search as a separate result page only when there is explicit submit evidence: search key/button/Enter, route/url change, app stack change, or a distinct route-level shell.
- Keep independent business surfaces separate. If screenshots share a similar shell but represent different stable business verticals, submitted queries with explicit submit evidence, selected categories, record/detail identities, or route-level namespaces, do not merge them into one generic page. Prefer an existing specific candidate; otherwise create a new page proposal for the missing business surface.
- For every decision, first state the durable_page_identity: the stable owner page or business surface implied by the screenshot and interaction provenance. Then state whether the target page represents that identity.
- Scroll-only changes should usually stay in the same page unless the fixed shell and core component family change.

Return strict JSON only."""
    schema = {
        "target_page_id": target_page["page_cluster_id"],
        "batch_id": batch_id,
        "summary": {
            "total": len(items),
            "keep": 0,
            "move_existing": 0,
            "create_new_page": 0,
            "skip": 0,
            "uncertain": 0,
            "target_page_purity": "pure | mixed",
        },
        "new_pages": [
            {
                "proposed_page_id": "c_domain_specific_name",
                "page_name": "short human readable name",
                "route_path": "/optional/new/path",
                "page_type_hint": "homepage | search_entry | search_results | form | settings | list | detail | utility",
                "description": "what visual state and interaction purpose this new page represents",
                "distinguishing_visual_signature": [
                    "fixed shell/header/body/sticky controls that distinguish it from target and existing candidates"
                ],
                "seed_tile_ids": ["Hxx"],
                "split_from_page_id": target_page["page_cluster_id"],
                "why_existing_candidates_do_not_fit": "short reason",
            }
        ],
        "decisions": [
            {
                "tile_id": "Hxx",
                "trace_id": "...",
                "step_index": 0,
                "current_assigned_page_id": target_page["page_cluster_id"],
                "verdict": "keep | move_existing | create_new_page | skip | uncertain",
                "corrected_page_id": "existing page id or proposed_new_page_id or null",
                "proposed_new_page_id": "only for create_new_page",
                "confidence": 0.0,
                "durable_page_identity": "visible stable page identity, or empty if none",
                "target_encodes_durable_identity": True,
                "override_transition_hint": False,
                "route_or_shell_evidence": ["required only when overriding transition_hint"],
                "reason": "concise rationale",
                "visual_evidence": ["..."],
                "visual_mismatch": ["..."],
            }
        ],
    }
    content: list[dict[str, Any]] = [
        {"type": "text", "text": instructions},
        {"type": "text", "text": "TARGET PAGE:\n" + json.dumps(target_page, ensure_ascii=False, indent=2)},
        {"type": "text", "text": "CANDIDATE PAGE CATALOG:\n" + json.dumps(candidates, ensure_ascii=False, indent=2)},
    ]
    for item in items:
        metadata = {
            k: v for k, v in item.items()
            if k not in {"image_path", "image_rel"}
        }
        content.append({
            "type": "text",
            "text": f"IMAGE {item['tile_id']} METADATA (the next input_image is {item['tile_id']}):\n"
            + json.dumps(metadata, ensure_ascii=False, indent=2),
        })
        content.append({"type": "image_url", "image_url": {"url": encode_image(str(item["image_path"])), "detail": "high"}})
    content.append({"type": "text", "text": "OUTPUT JSON SCHEMA:\n" + json.dumps(schema, ensure_ascii=False, indent=2)})
    return [{"role": "user", "content": content}]


def build_phase1a_obs_prompt(
    *,
    target_page: dict[str, Any],
    candidates: list[dict[str, Any]],
    items: list[dict[str, Any]],
    batch_id: str,
) -> list[dict[str, Any]]:
    instructions = """You are auditing page assignment for a AppSpec reasoning artifact.

No screenshots are provided in this evidence mode. Each item contains a Phase1a frame observation extracted from the screenshot earlier in the pipeline.
Treat Phase1a observations as structured evidence, not ground truth. If the observation is too weak or internally inconsistent, use uncertain rather than guessing.

For each PHASE1A OBSERVATION Hxx, decide one of:
- keep: the observation belongs to the target page.
- move_existing: the observation is assigned to the wrong page, and one existing candidate page in CANDIDATE PAGE CATALOG is the correct page.
- create_new_page: the observation is assigned to the wrong page, and no existing candidate page fits.
- skip: the observation is not an app/page state that should enter AppSpec, e.g. phone launcher, transient loading, OS chrome, or explicit no-op retry.
- uncertain: not enough Phase1a evidence.

Important:
- A retained observation-only frame is page evidence, not an executable action. If Phase1a page_state says observation_role=terminal_observation and observation_disposition=retain, skip is forbidden. Use keep, move_existing, or create_new_page according to the durable page identity; use uncertain only when the evidence cannot distinguish those choices.
- Page identity must be based on durable shell, route/business surface, stable owner page, and Phase1a transition/page-state evidence.
- Do not create or move pages solely from action text. Use action/thought only as provenance.
- Overlay, bottom sheet, dialog, or configuration surfaces belong to their host page when host_page_still_visible or host_page_identity indicates an underlying owner page.
- Search/query typing usually updates the same search-entry page unless Phase1a evidence shows explicit submit, route/app-stack change, or a distinct result-page shell.
- If you override transition_hint, cite concrete Phase1a route/shell/page-state evidence in route_or_shell_evidence and set override_transition_hint=true.
- For every decision, state durable_page_identity and whether the target page encodes that identity.

Return strict JSON only."""
    schema = {
        "target_page_id": target_page["page_cluster_id"],
        "batch_id": batch_id,
        "summary": {
            "total": len(items),
            "keep": 0,
            "move_existing": 0,
            "create_new_page": 0,
            "skip": 0,
            "uncertain": 0,
            "target_page_purity": "pure | mixed",
        },
        "new_pages": [
            {
                "proposed_page_id": "c_domain_specific_name",
                "page_name": "short human readable name",
                "route_path": "/optional/new/path",
                "page_type_hint": "homepage | search_entry | search_results | form | settings | list | detail | utility",
                "description": "what Phase1a evidence identifies this new page",
                "distinguishing_visual_signature": ["Phase1a shell/header/body/sticky patterns or page-state evidence"],
                "seed_tile_ids": ["Hxx"],
                "split_from_page_id": target_page["page_cluster_id"],
                "why_existing_candidates_do_not_fit": "short reason",
            }
        ],
        "decisions": [
            {
                "tile_id": "Hxx",
                "trace_id": "...",
                "step_index": 0,
                "current_assigned_page_id": target_page["page_cluster_id"],
                "verdict": "keep | move_existing | create_new_page | skip | uncertain",
                "corrected_page_id": "existing page id or proposed_new_page_id or null",
                "proposed_new_page_id": "only for create_new_page",
                "confidence": 0.0,
                "durable_page_identity": "stable owner page identity, or empty if none",
                "target_encodes_durable_identity": True,
                "override_transition_hint": False,
                "route_or_shell_evidence": ["required only when overriding transition_hint"],
                "reason": "concise rationale",
                "visual_evidence": ["Phase1a-derived evidence"],
                "visual_mismatch": ["Phase1a-derived mismatch evidence"],
            }
        ],
    }
    content: list[dict[str, Any]] = [
        {"type": "text", "text": instructions},
        {"type": "text", "text": "TARGET PAGE:\n" + json.dumps(target_page, ensure_ascii=False, indent=2)},
        {"type": "text", "text": "CANDIDATE PAGE CATALOG:\n" + json.dumps(candidates, ensure_ascii=False, indent=2)},
    ]
    for item in items:
        metadata = {
            k: v for k, v in item.items()
            if k not in {"image_path", "image_rel", "phase1a_observation"}
        }
        obs = item.get("phase1a_observation")
        if not isinstance(obs, dict):
            raise ValueError(f"item missing phase1a_observation: {item.get('key')}")
        content.append({
            "type": "text",
            "text": (
                f"PHASE1A OBSERVATION {item['tile_id']} METADATA:\n"
                + json.dumps(metadata, ensure_ascii=False, indent=2)
                + f"\nPHASE1A OBSERVATION {item['tile_id']}:\n"
                + json.dumps(obs, ensure_ascii=False, indent=2)
            ),
        })
    content.append({"type": "text", "text": "OUTPUT JSON SCHEMA:\n" + json.dumps(schema, ensure_ascii=False, indent=2)})
    return [{"role": "user", "content": content}]


def render_html(
    output_html: Path,
    target_page_id: str,
    target_page: dict[str, Any],
    items: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    parsed = result.get("parsed") or {}
    decisions = {d.get("tile_id"): d for d in parsed.get("decisions", []) if isinstance(d, dict)}
    cards = []
    for item in items:
        decision = decisions.get(item["tile_id"], {})
        verdict = decision.get("verdict", "missing")
        cards.append(f"""
        <section class="card {html.escape(str(verdict))}">
          <img src="{html.escape(item['image_rel'])}" alt="{html.escape(item['tile_id'])}">
          <div class="body">
            <h3>{html.escape(item['tile_id'])} · step {html.escape(str(item['step_index']))}</h3>
            <p><code>{html.escape(item['key'])}</code></p>
            <p><b>verdict</b>: {html.escape(str(verdict))}</p>
            <p><b>corrected</b>: {html.escape(str(decision.get('corrected_page_id') or ''))}</p>
            <p><b>reason</b>: {html.escape(str(decision.get('reason') or ''))}</p>
            <p><b>action</b>: {html.escape(str(item.get('current_action') or item.get('current_thought') or '')[:240])}</p>
          </div>
        </section>
        """)
    html_text = f"""<!doctype html>
<meta charset="utf-8">
<title>Page Assignment Audit · {html.escape(target_page_id)}</title>
<style>
body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:#f8fafc; color:#0f172a; }}
header {{ position:sticky; top:0; z-index:5; background:#fff; border-bottom:1px solid #e2e8f0; padding:14px 18px; }}
h1 {{ margin:0 0 6px; font-size:20px; }}
.summary {{ display:flex; gap:8px; flex-wrap:wrap; }}
.pill {{ border:1px solid #cbd5e1; border-radius:999px; padding:3px 9px; background:#fff; font-size:12px; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap:14px; padding:16px; }}
.card {{ background:#fff; border:2px solid #e2e8f0; border-radius:10px; overflow:hidden; }}
.card.keep {{ border-color:#22c55e; }}
.card.move_existing {{ border-color:#f59e0b; }}
.card.create_new_page {{ border-color:#7c3aed; }}
.card.skip {{ border-color:#64748b; }}
.card.uncertain,.card.missing {{ border-color:#ef4444; }}
.card img {{ width:100%; display:block; background:#fff; }}
.body {{ padding:10px; font-size:13px; line-height:1.4; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; }}
pre {{ white-space:pre-wrap; overflow:auto; background:#0f172a; color:#e2e8f0; padding:12px; margin:16px; border-radius:10px; }}
</style>
<header>
  <h1>Page Assignment Audit · {html.escape(target_page_id)}</h1>
  <div>{html.escape(target_page.get('page_name') or '')} · {html.escape(target_page.get('route_path') or '')}</div>
  <div class="summary">
    {''.join(f'<span class="pill">{html.escape(str(k))}: {html.escape(str(v))}</span>' for k, v in (parsed.get('summary') or {}).items())}
  </div>
</header>
<main>
  <div class="grid">{''.join(cards)}</div>
  <h2 style="padding:0 16px">New Pages</h2>
  <pre>{html.escape(json.dumps(parsed.get('new_pages', []), ensure_ascii=False, indent=2))}</pre>
</main>
"""
    output_html.write_text(html_text, encoding="utf-8")


def page_ids_to_audit(data: dict[str, Any], target_page_id: str, all_pages: bool) -> list[str]:
    if not all_pages:
        return [target_page_id]
    result = []
    for page in data.get("nodes", []):
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id", "") or "")
        if not page_id:
            continue
        if int(page.get("stepCount", 0) or 0) <= 1:
            continue
        result.append(page_id)
    return result


def candidate_pages_for_target(
    *,
    target_page_id: str,
    full_pages: list[dict[str, Any]],
    candidate_pages_by_target: dict[str, list[dict[str, Any]]] | None,
    require_candidate_map: bool,
) -> list[dict[str, Any]]:
    if not candidate_pages_by_target:
        if require_candidate_map:
            raise RuntimeError(
                f"candidate_pages_by_target is required for target={target_page_id}"
            )
        return full_pages
    if target_page_id not in candidate_pages_by_target:
        raise RuntimeError(
            f"candidate_pages_by_target missing target={target_page_id}"
        )
    candidates = candidate_pages_by_target[target_page_id]
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError(
            f"candidate_pages_by_target target={target_page_id} has no candidates"
        )
    candidate_ids = {
        str(page.get("page_cluster_id") or page.get("id") or "").strip()
        for page in candidates
        if isinstance(page, dict)
    }
    if target_page_id not in candidate_ids:
        raise RuntimeError(
            f"candidate_pages_by_target target={target_page_id} missing target page candidate"
        )
    return candidates


def build_page_identity_reconciliation_inputs(
    *,
    data: dict[str, Any],
    target_ids: list[str],
    result_root: Path,
    reports_dir: Path,
    trace_steps: dict[str, dict[int, dict[str, Any]]],
    evidence_mode: str,
    limit: int,
    retrieval_mode: str,
    max_candidates: int,
    top_k: int,
    context_budget: int,
    image_token_cost: int,
    output_tokens_per_item: int,
    merge_similarity_threshold: float,
    merge_composite_threshold: float,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    index = build_page_identity_index(data, retrieval_mode=retrieval_mode)
    target_item_map: dict[str, list[dict[str, Any]]] = {}
    for target_id in target_ids:
        items = build_items(
            data,
            target_id,
            result_root,
            reports_dir,
            trace_steps,
            limit=limit,
            offset=0,
        )
        if evidence_mode == "phase1a_obs":
            attach_phase1a_observations(items, result_root)
        if items:
            target_item_map[target_id] = items
    report = build_page_identity_reconciliation_report(
        index=index,
        target_item_map=target_item_map,
        evidence_mode=evidence_mode,
        max_candidates=max_candidates,
        top_k=top_k,
        context_budget=context_budget,
        image_token_cost=image_token_cost,
        output_tokens_per_item=output_tokens_per_item,
        merge_similarity_threshold=merge_similarity_threshold,
        merge_composite_threshold=merge_composite_threshold,
    )
    assignment = report["consumers"]["StepPageAssignmentReview"]
    candidate_pages_by_target = {
        target["target_page_id"]: target["candidates"]
        for target in assignment.get("targets", [])
        if isinstance(target, dict) and target.get("target_page_id")
    }
    missing_targets = [target_id for target_id in target_item_map if target_id not in candidate_pages_by_target]
    if missing_targets:
        raise RuntimeError(
            "Page Identity Reconciliation failed to produce candidates for targets: "
            + ", ".join(missing_targets)
        )
    return candidate_pages_by_target, report


async def run_batch(
    *,
    data: dict[str, Any],
    pages: list[dict[str, Any]],
    full_candidate_page_count: int | None = None,
    candidate_selection_policy: str = "full_catalog",
    trace_steps: dict[str, dict[int, dict[str, Any]]],
    result_root: Path,
    reports_dir: Path,
    target_page_id: str,
    batch_id: str,
    offset: int,
    batch_size: int,
    model: str,
    reasoning_effort: str,
    max_tokens: int,
    viz_only: bool,
    evidence_mode: str,
) -> dict[str, Any]:
    if evidence_mode not in {"phase1a_obs", "image"}:
        raise ValueError(f"unsupported evidence_mode: {evidence_mode}")
    target_page = next((p for p in pages if p.get("page_cluster_id") == target_page_id), None)
    if not target_page:
        raise ValueError(f"target page not found: {target_page_id}")
    items = build_items(
        data,
        target_page_id,
        result_root,
        reports_dir,
        trace_steps,
        limit=batch_size,
        offset=offset,
    )
    if evidence_mode == "phase1a_obs":
        attach_phase1a_observations(items, result_root)
    prefix = reports_dir / f"page_assignment_audit_{target_page_id}_{batch_id}"
    manifest = {
        "target_page_id": target_page_id,
        "batch_id": batch_id,
        "evidence_mode": evidence_mode,
        "offset": offset,
        "batch_size": batch_size,
        "prompt_policy": "keep | move_existing | create_new_page | skip | uncertain",
        "item_count": len(items),
        "items": [json_safe(item) for item in items],
        "target_page": target_page,
        "candidate_page_count": len(pages),
        "full_candidate_page_count": full_candidate_page_count or len(pages),
        "candidate_selection_policy": candidate_selection_policy,
    }
    (prefix.parent / f"page_assignment_audit_{target_page_id}_assets").mkdir(parents=True, exist_ok=True)
    (prefix.with_suffix(".manifest.json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if viz_only:
        result = {"parsed": {"summary": {"total": len(items), "viz_only": True}, "decisions": [], "new_pages": []}}
    else:
        if evidence_mode == "phase1a_obs":
            messages = build_phase1a_obs_prompt(
                target_page=target_page,
                candidates=pages,
                items=items,
                batch_id=batch_id,
            )
        else:
            messages = build_prompt(target_page, pages, items, batch_id)
        started = time.time()
        reply, usage = await call_openai_api_json_async(
            messages,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            model=model,
            stage="page_assignment_audit",
        )
        parsed = json.loads(reply or "{}")
        result = {
            "reply": reply,
            "usage": usage,
            "elapsed_sec": round(time.time() - started, 2),
            "parsed": parsed,
        }
    result["target_page_id"] = target_page_id
    result["batch_id"] = batch_id
    result["offset"] = offset
    result["item_count"] = len(items)
    result["item_metadata"] = [
        {
            key: json_safe(value)
            for key, value in item.items()
            if key not in {"image_path", "image_rel"}
        }
        for item in items
    ]
    result_path = prefix.with_suffix(".api_result.json")
    html_path = prefix.with_suffix(".html")
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html(html_path, target_page_id, target_page, items, result)
    result["result_path"] = str(result_path)
    result["html_path"] = str(html_path)
    return result


def merge_results(results: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    new_pages_by_id: dict[str, dict[str, Any]] = {}
    totals = {
        "batches": len(results),
        "pages_audited": len({r.get("target_page_id") for r in results}),
        "screenshots": 0,
        "keep": 0,
        "move_existing": 0,
        "create_new_page": 0,
        "skip": 0,
        "uncertain": 0,
    }
    for result in results:
        parsed = result.get("parsed") or {}
        item_metadata_by_tile = {
            str(item.get("tile_id", "") or ""): item
            for item in result.get("item_metadata", []) or []
            if isinstance(item, dict)
        }
        item_metadata_by_key = {
            str(item.get("key", "") or ""): item
            for item in result.get("item_metadata", []) or []
            if isinstance(item, dict)
        }
        for key in ("keep", "move_existing", "create_new_page", "skip", "uncertain"):
            totals[key] += int((parsed.get("summary") or {}).get(key, 0) or 0)
        totals["screenshots"] += int((parsed.get("summary") or {}).get("total", result.get("item_count", 0)) or 0)
        for page in parsed.get("new_pages", []) or []:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("proposed_page_id", "") or "").strip()
            if page_id and page_id not in new_pages_by_id:
                new_pages_by_id[page_id] = page
        for decision in parsed.get("decisions", []) or []:
            if not isinstance(decision, dict):
                continue
            decision = dict(decision)
            decision.setdefault("target_page_id", result.get("target_page_id"))
            decision.setdefault("batch_id", result.get("batch_id"))
            metadata = (
                item_metadata_by_key.get(f"{decision.get('trace_id')}:{decision.get('step_index')}")
                or item_metadata_by_tile.get(str(decision.get("tile_id", "") or ""))
                or {}
            )
            for metadata_key in (
                "previous_action",
                "current_action",
                "previous_thought",
                "current_thought",
                "transition_hint",
                "current_assignment",
            ):
                if metadata_key not in decision and metadata_key in metadata:
                    decision[metadata_key] = metadata[metadata_key]
            decisions.append(decision)
    single_screenshot_pages = [
        str(page.get("id", "") or "")
        for page in data.get("nodes", [])
        if isinstance(page, dict) and int(page.get("stepCount", 0) or 0) <= 1
    ]
    return {
        "schema_version": "page_assignment_audit_merged.v1",
        "summary": totals,
        "prefilter": {
            "policy": "skip_only_pages_with_step_count_lte_1",
            "single_screenshot_pages_not_audited": single_screenshot_pages,
            "single_screenshot_page_count": len(single_screenshot_pages),
        },
        "new_pages": list(new_pages_by_id.values()),
        "decisions": decisions,
        "batch_results": [
            {
                "target_page_id": r.get("target_page_id"),
                "batch_id": r.get("batch_id"),
                "item_count": r.get("item_count"),
                "html_path": r.get("html_path"),
                "result_path": r.get("result_path"),
                "usage": r.get("usage"),
                "elapsed_sec": r.get("elapsed_sec"),
            }
            for r in results
        ],
    }


def canonical_page_from_new_page(page: dict[str, Any]) -> dict[str, Any]:
    route_path = str(page.get("route_path", "") or "")
    parent = route_path.rsplit("/", 1)[0] if "/" in route_path.strip("/") else "/"
    if not parent:
        parent = "/"
    return {
        "page_cluster_id": page.get("proposed_page_id"),
        "page_type_hint": page.get("page_type_hint", "utility"),
        "page_name": page.get("page_name") or page.get("proposed_page_id"),
        "description": page.get("description", ""),
        "primary_functions": [],
        "route_path": route_path or f"/{str(page.get('proposed_page_id', '')).replace('c_', '').replace('_', '/')}",
        "parent_route_path": parent,
        "route_level": max(1, len([p for p in (route_path or "").split("/") if p])),
        "created_by": "page_assignment_audit",
        "split_from_page_id": page.get("split_from_page_id", ""),
    }


def apply_to_reasoning_payload(
    reasoning_payload: dict[str, Any],
    merged: dict[str, Any],
    output_path: Path | None = None,
    source_reasoning: str = "<in_memory>",
) -> tuple[dict[str, Any], dict[str, Any]]:
    reasoning = json.loads(json.dumps(reasoning_payload, ensure_ascii=False))
    page_ids = {
        str(page.get("page_cluster_id", "") or "")
        for page in reasoning.get("canonical_pages", [])
        if isinstance(page, dict)
    }
    for new_page in merged.get("new_pages", []):
        page_id = str(new_page.get("proposed_page_id", "") or "").strip()
        if not page_id or page_id in page_ids:
            continue
        reasoning.setdefault("canonical_pages", []).append(canonical_page_from_new_page(new_page))
        page_ids.add(page_id)

    labels_by_key = {
        f"{label.get('trace_id')}:{label.get('step_index')}": label
        for label in reasoning.get("step_labels", [])
        if isinstance(label, dict)
    }
    skipped = reasoning.setdefault("skipped_steps", [])
    skipped_by_key = {
        f"{item.get('trace_id')}:{item.get('step_index')}": item
        for item in skipped
        if isinstance(item, dict)
    }
    applied = []
    blocked = []
    for decision in merged.get("decisions", []):
        verdict = str(decision.get("verdict", "") or "").strip()
        trace_id = str(decision.get("trace_id", "") or "").strip()
        step_index = decision.get("step_index")
        key = f"{trace_id}:{step_index}"
        if verdict in {"move_existing", "create_new_page"}:
            corrected = str(decision.get("corrected_page_id") or decision.get("proposed_new_page_id") or "").strip()
            if not corrected:
                continue
            label = labels_by_key.get(key)
            if not label:
                continue
            old_page = label.get("page_cluster_id", "")
            should_block, block_reason = should_block_search_typeahead_move(decision, label)
            if should_block:
                blocked.append({
                    "key": key,
                    "from": old_page,
                    "to": corrected,
                    "verdict": verdict,
                    "reason": block_reason,
                    "transition_hint": decision.get("transition_hint", {}),
                    "audit_reason": decision.get("reason", ""),
                })
                continue
            label["page_cluster_id"] = corrected
            label["materialized_page_id"] = corrected
            label["general_page_id"] = corrected
            label["source_page_cluster_id"] = corrected
            label["page_assignment_audit"] = {
                "old_page_cluster_id": old_page,
                "verdict": verdict,
                "confidence": decision.get("confidence"),
                "reason": decision.get("reason", ""),
            }
            applied.append({"key": key, "from": old_page, "to": corrected, "verdict": verdict})
        elif verdict == "skip":
            label = labels_by_key.pop(key, None)
            if (
                isinstance(label, dict)
                and str(label.get("observation_role", "")).strip()
                == "terminal_observation"
            ):
                labels_by_key[key] = label
                blocked.append({
                    "key": key,
                    "from": label.get("page_cluster_id", ""),
                    "to": "SKIP",
                    "verdict": verdict,
                    "reason": "retained_terminal_observation_skip_forbidden",
                    "audit_reason": decision.get("reason", ""),
                })
                continue
            if label is not None:
                reasoning["step_labels"] = [
                    item for item in reasoning.get("step_labels", [])
                    if f"{item.get('trace_id')}:{item.get('step_index')}" != key
                ]
            if key not in skipped_by_key:
                skipped.append({
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "skip_reason": "page_assignment_audit_skip",
                    "authority": "page_assignment_audit",
                    "reason": decision.get("reason", ""),
                })
            applied.append({"key": key, "from": label.get("page_cluster_id", "") if label else "", "to": "SKIP", "verdict": verdict})
    reasoning.setdefault("debug", {})["page_assignment_audit"] = {
        "source_reasoning": source_reasoning,
        "applied_count": len(applied),
        "blocked_count": len(blocked),
        "applied": applied,
        "blocked": blocked,
        "summary": merged.get("summary", {}),
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(reasoning, ensure_ascii=False, indent=2), encoding="utf-8")
    return reasoning, {
        "applied_count": len(applied),
        "blocked_count": len(blocked),
        "output_path": str(output_path) if output_path else "",
        "applied": applied,
        "blocked": blocked,
    }


def apply_to_reasoning(reasoning_path: Path, merged: dict[str, Any], output_path: Path) -> dict[str, Any]:
    reasoning = json.loads(reasoning_path.read_text(encoding="utf-8"))
    _patched, report = apply_to_reasoning_payload(
        reasoning,
        merged,
        output_path=output_path,
        source_reasoning=str(reasoning_path),
    )
    return report


def render_index(output_html: Path, merged: dict[str, Any]) -> None:
    rows = []
    for item in merged.get("batch_results", []):
        html_path = str(item.get("html_path", "") or "")
        rel = Path(html_path).name if html_path else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('target_page_id', '')))}</td>"
            f"<td>{html.escape(str(item.get('batch_id', '')))}</td>"
            f"<td>{html.escape(str(item.get('item_count', '')))}</td>"
            f"<td><a href='{html.escape(rel)}'>{html.escape(rel)}</a></td>"
            "</tr>"
        )
    text = f"""<!doctype html>
<meta charset="utf-8">
<title>Page Assignment Audit Index</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; background:#f8fafc; color:#0f172a; }}
table {{ border-collapse: collapse; width: 100%; background:#fff; }}
td,th {{ border:1px solid #e2e8f0; padding:7px 9px; text-align:left; }}
pre {{ background:#0f172a; color:#e2e8f0; padding:12px; border-radius:8px; overflow:auto; }}
</style>
<h1>Page Assignment Audit Index</h1>
<pre>{html.escape(json.dumps(merged.get('summary', {}), ensure_ascii=False, indent=2))}</pre>
<h2>New Pages</h2>
<pre>{html.escape(json.dumps(merged.get('new_pages', []), ensure_ascii=False, indent=2))}</pre>
<h2>Batches</h2>
<table><thead><tr><th>page</th><th>batch</th><th>items</th><th>html</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
"""
    output_html.write_text(text, encoding="utf-8")


async def run_page_assignment_audit(
    *,
    result_root: Path,
    trace_root: Path,
    reports_dir: Path,
    reasoning: dict[str, Any],
    target_page_id: str = "",
    all_pages: bool = True,
    limit: int = 32,
    batch_size: int = 32,
    pair_budget: int = 256,
    max_concurrent: int = 0,
    batch_id: str = "batch_000",
    model: str = "gpt-5.2-2025-12-11",
    reasoning_effort: str = "medium",
    max_tokens: int = 32768,
    viz_only: bool = False,
    evidence_mode: str = "phase1a_obs",
    candidate_pages_by_target: dict[str, list[dict[str, Any]]] | None = None,
    require_candidate_pages_by_target: bool = False,
    candidate_selection_policy: str = "full_catalog",
    enable_page_identity_reconciliation: bool = False,
    page_identity_retrieval_mode: str = "embedding",
    page_identity_max_candidates: int = 8,
    page_identity_top_k: int = 6,
    page_identity_context_budget: int = 32000,
    page_identity_image_token_cost: int = 1800,
    page_identity_output_tokens_per_item: int = 220,
    page_identity_merge_similarity_threshold: float = 0.83,
    page_identity_merge_composite_threshold: float = 0.68,
    patched_reasoning_json: Path | None = None,
    reasoning_label: str = "<in_memory>",
) -> dict[str, Any]:
    if evidence_mode not in {"phase1a_obs", "image"}:
        raise ValueError(f"unsupported evidence_mode: {evidence_mode}")
    reports_dir.mkdir(parents=True, exist_ok=True)
    data = build_audit_data_from_reasoning_payload(
        result_root=result_root,
        trace_root=trace_root,
        reasoning=reasoning,
        reports_dir=reports_dir,
        reasoning_label=reasoning_label,
    )
    trace_steps = trace_step_maps(trace_root)
    pages = [compact_page(page) for page in data.get("nodes", [])]
    target_ids = page_ids_to_audit(data, target_page_id, all_pages)
    if not target_ids or (not all_pages and not target_page_id):
        raise ValueError("target_page_id is required unless all_pages is set")
    page_identity_report: dict[str, Any] | None = None
    if enable_page_identity_reconciliation:
        if candidate_pages_by_target:
            raise RuntimeError(
                "candidate_pages_by_target must not be provided when enable_page_identity_reconciliation is true"
            )
        candidate_pages_by_target, page_identity_report = build_page_identity_reconciliation_inputs(
            data=data,
            target_ids=target_ids,
            result_root=result_root,
            reports_dir=reports_dir,
            trace_steps=trace_steps,
            evidence_mode=evidence_mode,
            limit=limit,
            retrieval_mode=page_identity_retrieval_mode,
            max_candidates=page_identity_max_candidates,
            top_k=page_identity_top_k,
            context_budget=page_identity_context_budget,
            image_token_cost=page_identity_image_token_cost,
            output_tokens_per_item=page_identity_output_tokens_per_item,
            merge_similarity_threshold=page_identity_merge_similarity_threshold,
            merge_composite_threshold=page_identity_merge_composite_threshold,
        )
        require_candidate_pages_by_target = True
        candidate_selection_policy = "PageIdentityIndex:mandatory_route_current_plus_embedding_or_lexical_topk"
    page_count_for_batching = len(pages)
    if candidate_pages_by_target:
        page_count_for_batching = max(
            len(candidate_pages_for_target(
                target_page_id=page_id,
                full_pages=pages,
                candidate_pages_by_target=candidate_pages_by_target,
                require_candidate_map=True,
            ))
            for page_id in target_ids
        )

    tasks, effective_batch_size = build_audit_tasks(
        data=data,
        target_ids=target_ids,
        all_pages=all_pages,
        limit=limit,
        requested_batch_size=batch_size,
        candidate_page_count=page_count_for_batching,
        pair_budget=pair_budget,
        batch_id=batch_id,
    )

    async def _run_one(page_id: str, effective_batch_id: str, offset: int, count: int) -> dict[str, Any]:
        print(f"[audit] {page_id} {effective_batch_id} offset={offset} count={count}")
        target_candidates = candidate_pages_for_target(
            target_page_id=page_id,
            full_pages=pages,
            candidate_pages_by_target=candidate_pages_by_target,
            require_candidate_map=require_candidate_pages_by_target,
        )
        return await run_batch(
            data=data,
            pages=target_candidates,
            full_candidate_page_count=len(pages),
            candidate_selection_policy=candidate_selection_policy,
            trace_steps=trace_steps,
            result_root=result_root,
            reports_dir=reports_dir,
            target_page_id=page_id,
            batch_id=effective_batch_id,
            offset=offset,
            batch_size=count,
            model=model,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            viz_only=viz_only,
            evidence_mode=evidence_mode,
        )

    effective_fanout = derive_page_assignment_audit_fanout(len(tasks), max_concurrent)
    print(
        f"[audit] bounded_fanout={effective_fanout} "
        f"task_count={len(tasks)} requested_max_concurrent={max_concurrent} "
        f"requested_batch_size={batch_size} effective_batch_size={effective_batch_size} "
        f"candidate_page_count={page_count_for_batching} full_candidate_page_count={len(pages)} "
        f"pair_budget={pair_budget} evidence_mode={evidence_mode}"
    )
    results = await run_tasks_bounded(
        tasks,
        max_concurrent=max_concurrent,
        runner=_run_one,
    )
    merged = merge_results(results, data)
    merged_path = reports_dir / "page_assignment_audit_merged.json"
    index_path = reports_dir / "page_assignment_audit_index.html"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    render_index(index_path, merged)
    print(index_path)
    print(merged_path)

    patched_reasoning, patch_report = apply_to_reasoning_payload(
        reasoning,
        merged,
        output_path=patched_reasoning_json,
        source_reasoning=reasoning_label,
    )
    if patched_reasoning_json:
        patch_report_path = patched_reasoning_json.with_suffix(".page_assignment_patch_report.json")
        patch_report_path.write_text(json.dumps(patch_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(patched_reasoning_json)
        print(patch_report_path)
    page_identity_artifact_dir = result_root / "phase_global_reasoning" / "page_identity_reconciliation"
    if page_identity_report is not None:
        page_identity_artifact_dir.mkdir(parents=True, exist_ok=True)
        report_path = page_identity_artifact_dir / "page_identity_reconciliation_report.json"
        report_path.write_text(json.dumps(page_identity_report, ensure_ascii=False, indent=2), encoding="utf-8")
        (page_identity_artifact_dir / "page_identity_index.json").write_text(
            json.dumps(page_identity_report.get("shared_index", {}), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        assignment = page_identity_report["consumers"]["StepPageAssignmentReview"]
        (page_identity_artifact_dir / "candidate_retrieval.json").write_text(
            json.dumps(assignment.get("targets", []), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        patched_reasoning.setdefault("debug", {})["page_identity_reconciliation"] = {
            "shared_index_id": page_identity_report["shared_index"]["index_id"],
            "artifact_dir": str(page_identity_artifact_dir),
            "report_path": str(report_path),
            "merge_candidate_clusters": page_identity_report["consumers"]["OverSplitPageMergeReview"]["merge_candidate_clusters"],
        }
        if patched_reasoning_json:
            patched_reasoning_json.write_text(
                json.dumps(patched_reasoning, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return {
        "audit_data": data,
        "merged": merged,
        "patched_reasoning": patched_reasoning,
        "patch_report": patch_report,
        "index_path": str(index_path),
        "merged_path": str(merged_path),
        "page_identity_reconciliation": page_identity_report,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audit-html",
        type=Path,
        default=None,
        help="Legacy/debug input. Pipeline mode builds audit data directly from --reasoning-json.",
    )
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--trace-root", type=Path, required=True)
    parser.add_argument("--target-page-id", default="")
    parser.add_argument("--all-pages", action="store_true")
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--pair-budget", type=int, default=256)
    parser.add_argument("--max-concurrent", type=int, default=6)
    parser.add_argument("--batch-id", default="batch_000")
    parser.add_argument("--model", default="gpt-5.2-2025-12-11")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--viz-only", action="store_true")
    parser.add_argument("--evidence-mode", choices=["phase1a_obs", "image"], default="phase1a_obs")
    parser.add_argument("--reasoning-json", type=Path, default=None)
    parser.add_argument("--patched-reasoning-json", type=Path, default=None)
    args = parser.parse_args()

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    reasoning_json = args.reasoning_json or default_reasoning_json(args.result_root)
    if args.audit_html:
        data = load_audit_data(args.audit_html)
    else:
        data = build_audit_data_from_reasoning(
            result_root=args.result_root,
            trace_root=args.trace_root,
            reasoning_json=reasoning_json,
            reports_dir=args.reports_dir,
        )
    trace_steps = trace_step_maps(args.trace_root)
    pages = [compact_page(page) for page in data.get("nodes", [])]
    target_ids = page_ids_to_audit(data, args.target_page_id, args.all_pages)
    if not target_ids or (not args.all_pages and not args.target_page_id):
        raise SystemExit("--target-page-id is required unless --all-pages is set")

    tasks, effective_batch_size = build_audit_tasks(
        data=data,
        target_ids=target_ids,
        all_pages=args.all_pages,
        limit=args.limit,
        requested_batch_size=args.batch_size,
        candidate_page_count=len(pages),
        pair_budget=args.pair_budget,
        batch_id=args.batch_id,
    )

    async def _run_one(target_page_id: str, batch_id: str, offset: int, count: int) -> dict[str, Any]:
        print(f"[audit] {target_page_id} {batch_id} offset={offset} count={count}")
        return await run_batch(
            data=data,
            pages=pages,
            trace_steps=trace_steps,
            result_root=args.result_root,
            reports_dir=args.reports_dir,
            target_page_id=target_page_id,
            batch_id=batch_id,
            offset=offset,
            batch_size=count,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            max_tokens=args.max_tokens,
            viz_only=args.viz_only,
            evidence_mode=args.evidence_mode,
        )

    effective_fanout = derive_page_assignment_audit_fanout(len(tasks), args.max_concurrent)
    print(
        f"[audit] bounded_fanout={effective_fanout} "
        f"task_count={len(tasks)} requested_max_concurrent={args.max_concurrent} "
        f"requested_batch_size={args.batch_size} effective_batch_size={effective_batch_size} "
        f"candidate_page_count={len(pages)} pair_budget={args.pair_budget} evidence_mode={args.evidence_mode}"
    )
    results = await run_tasks_bounded(
        tasks,
        max_concurrent=args.max_concurrent,
        runner=_run_one,
    )
    merged = merge_results(results, data)
    merged_path = args.reports_dir / "page_assignment_audit_merged.json"
    index_path = args.reports_dir / "page_assignment_audit_index.html"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    render_index(index_path, merged)
    print(index_path)
    print(merged_path)

    if args.patched_reasoning_json:
        patch_report = apply_to_reasoning(reasoning_json, merged, args.patched_reasoning_json)
        patch_report_path = args.patched_reasoning_json.with_suffix(".page_assignment_patch_report.json")
        patch_report_path.write_text(json.dumps(patch_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(args.patched_reasoning_json)
        print(patch_report_path)


if __name__ == "__main__":
    asyncio.run(main())
