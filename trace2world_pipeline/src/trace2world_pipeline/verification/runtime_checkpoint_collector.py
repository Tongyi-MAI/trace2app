
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright, Page
from trace2world_pipeline.app_spec.environment_profile import load_environment_profile_from_result_dir


_TERMINAL_STEP_FAILURES = {
    "page_missing",
    "nav_error",
    "source_page_mismatch",
    "not_found",
    "fill_failed",
    "input_not_found",
    "no_text",
}


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  wrote: {path}")


def _load_action_host_filename_map(result_dir: Path) -> dict[str, str]:
    path = result_dir / "phase_pagegen" / "action_host_manifest.json"
    if not path.is_file():
        return {}
    try:
        data = _load_json(path)
    except Exception:
        return {}
    hosts = data.get("hosts") if isinstance(data, dict) else []
    if not isinstance(hosts, list):
        return {}
    mapping: dict[str, str] = {}
    for host in hosts:
        if not isinstance(host, dict):
            continue
        action_id = str(host.get("action_id") or "").strip()
        filename = str(host.get("filename") or host.get("page") or "").strip()
        if action_id and filename:
            mapping.setdefault(action_id, filename)
    return mapping


def _resolve_step_source_filename(
    action_host_filename_map: dict[str, str],
    observation_bindings: dict[str, Any],
    trace_id: str,
    step_index: int,
) -> str:
    action_id = f"{trace_id}:step{step_index}"
    formal_host = str(action_host_filename_map.get(action_id) or "").strip()
    if formal_host:
        return formal_host
    entry = observation_bindings.get(f"{trace_id}:{step_index}", {})
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("filename") or "").strip()


def _merge_evaluator_checkpoints(task_eval: dict[str, Any]) -> dict[str, dict]:
    checkpoints = task_eval.get("checkpoints", {})
    merged: dict[str, dict] = dict(checkpoints) if isinstance(checkpoints, dict) else {}
    outcome = task_eval.get("outcome", {})
    if isinstance(outcome, dict) and outcome.get("checkpoint_id"):
        checkpoint_id = str(outcome["checkpoint_id"])
        existing = merged.get(checkpoint_id, {})
        merged[checkpoint_id] = {
            **(existing if isinstance(existing, dict) else {}),
            **outcome,
        }
    return merged


def _merge_runtime_checkpoint_validation(
    existing: dict[str, Any] | None,
    update: dict[str, Any],
    updated_trace_ids: list[str],
) -> dict[str, Any]:
    if not isinstance(existing, dict) or not isinstance(existing.get("traces"), dict):
        merged: dict[str, Any] = {
            "format": "ghostweb_checkpoint_runtime_validation",
            "schema_revision": 1,
            "traces": {},
        }
    else:
        merged = json.loads(json.dumps(existing, ensure_ascii=False))

    update_traces = update.get("traces") or {}
    for trace_id in updated_trace_ids:
        if trace_id in update_traces:
            merged.setdefault("traces", {})[trace_id] = update_traces[trace_id]

    merged["format"] = update.get("format") or merged.get("format") or "ghostweb_checkpoint_runtime_validation"
    merged["schema_revision"] = update.get("schema_revision") or merged.get("schema_revision") or 1
    merged["collection_mode"] = {
        "mode": "trace_scoped_merge",
        "updated_trace_ids": list(updated_trace_ids),
        "previous_trace_count": len((existing or {}).get("traces", {})) if isinstance(existing, dict) else 0,
        "trace_count": len(merged.get("traces", {})),
        "latest_update": update.get("collection_mode", {}),
    }
    return merged


def _runtime_validation_output_path(result_dir: Path) -> Path:
    return result_dir / "phase_post_processing" / "evaluators" / "checkpoint_runtime_validation.json"


def _load_runtime_state_schema_by_checkpoint(result_dir: Path) -> dict[tuple[str, str], dict]:
    schema_path = result_dir / "phase_post_processing" / "instrumentation" / "runtime_state_schema.json"
    schema = _load_json(schema_path)
    by_key: dict[tuple[str, str], dict] = {}
    for item in schema.get("checkpoints", []) if isinstance(schema, dict) else []:
        if not isinstance(item, dict):
            continue
        trace_id = str(item.get("trace_id") or "")
        checkpoint_id = str(item.get("checkpoint_id") or "")
        if trace_id and checkpoint_id:
            by_key[(trace_id, checkpoint_id)] = item
    return by_key


def _write_runtime_checkpoint_shards(result: dict[str, Any], shard_output_dir: Path) -> None:
    shard_output_dir.mkdir(parents=True, exist_ok=True)
    for trace_id, trace_result in (result.get("traces") or {}).items():
        shard_result = {
            "format": result.get("format", "ghostweb_checkpoint_runtime_validation"),
            "schema_revision": result.get("schema_revision", 1),
            "traces": {trace_id: trace_result},
        }
        _save_json(shard_output_dir / _safe_trace_filename(trace_id), shard_result)


def _parse_obligations(html_content: str) -> list[dict]:
    m = re.search(r"const OBLIGATIONS\s*=\s*(\[.*?\]);\s*\n", html_content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return []


def _find_obligation(obligations: list[dict], trace_id: str, step_index: int) -> dict:
    for ob in obligations:
        if ob.get("trace_id") == trace_id and ob.get("step_index") == step_index:
            return ob
    return {}


_ACTION_MAP_STATUS_JS = """
(actionId) => {
    const map = window.GhostwebCheckpointActionMap;
    const entry = map && map.byActionId ? map.byActionId[actionId] : null;
    const runtimeStore = (window.GhostwebRuntimeState && window.GhostwebRuntimeState.snapshot)
        ? window.GhostwebRuntimeState.snapshot()
        : null;
    return {
        action_id: actionId,
        has_action_map: !!map,
        has_emit_for_action: !!(map && map.emitForAction),
        action_map_has_action_id: !!entry,
        checkpoint_ids: Array.isArray(entry)
            ? entry.map(item => item && item.checkpoint_id).filter(Boolean)
            : [],
        runtime_store: runtimeStore
    };
}
"""


def _runtime_store_delta(before: Any, after: Any) -> dict[str, Any]:
    before_state = before.get("current_state", {}) if isinstance(before, dict) else {}
    after_state = after.get("current_state", {}) if isinstance(after, dict) else {}
    if not isinstance(before_state, dict):
        before_state = {}
    if not isinstance(after_state, dict):
        after_state = {}
    before_keys = set(before_state)
    after_keys = set(after_state)
    changed_keys = sorted(
        key for key in before_keys & after_keys
        if before_state.get(key) != after_state.get(key)
    )
    added_keys = sorted(after_keys - before_keys)
    removed_keys = sorted(before_keys - after_keys)
    changed_values: dict[str, Any] = {}
    for key in [*added_keys, *changed_keys]:
        value = after_state.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            changed_values[key] = value
        else:
            changed_values[key] = str(value)[:200]
    return {
        "added_keys": added_keys,
        "changed_keys": changed_keys,
        "removed_keys": removed_keys,
        "changed_values": changed_values,
    }


def _base_step_audit(
    trace_id: str,
    step_index: int,
    operation: str,
    filename: str,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "step_index": step_index,
        "operation": operation,
        "filename": filename,
        "targeting_strategy": "",
        "selected_action_id": "",
        "action_map_has_action_id": False,
        "checkpoint_ids": [],
        "runtime_store_delta": _runtime_store_delta(None, None),
    }


def _action_host_selector(pattern: str) -> str:
    return (
        f'button[data-action-id="{pattern}"], button[data-action-aliases~="{pattern}"], '
        f'a[data-action-id="{pattern}"], a[data-action-aliases~="{pattern}"], '
        f'input[data-action-id="{pattern}"], input[data-action-aliases~="{pattern}"], '
        f'[role="button"][data-action-id="{pattern}"], [role="button"][data-action-aliases~="{pattern}"], '
        f'[data-action-id="{pattern}"], [data-action-aliases~="{pattern}"], '
        f'[data-element-id*="{pattern}"], [data-element-aliases*="{pattern}"]'
    )


def _primary_action_host_selector(pattern: str) -> str:
    return f'[data-action-id="{pattern}"]'


def _query_action_host(page: Page, pattern: str) -> Any:
    return page.query_selector(_primary_action_host_selector(pattern)) or page.query_selector(
        _action_host_selector(pattern)
    )


async def _query_action_host_async(page: Any, pattern: str) -> Any:
    return await page.query_selector(_primary_action_host_selector(pattern)) or await page.query_selector(
        _action_host_selector(pattern)
    )


def _visible_action_host_candidates(page: Page, pattern: str) -> list[tuple[Any, str]]:
    selector = _action_host_selector(pattern)
    modal_scopes = page.locator('[role="dialog"]:visible, [aria-modal="true"]:visible')
    if modal_scopes.count():
        modal_hosts = modal_scopes.last.locator(selector)
        visible_modal_hosts = [
            modal_hosts.nth(index)
            for index in range(modal_hosts.count())
            if modal_hosts.nth(index).is_visible(timeout=250)
        ]
        if visible_modal_hosts:
            return [(host, "action_id_visible_modal") for host in visible_modal_hosts]

    hosts = page.locator(selector)
    return [
        (hosts.nth(index), "action_id_visible")
        for index in range(hosts.count())
        if hosts.nth(index).is_visible(timeout=250)
    ]


async def _visible_action_host_candidates_async(page: Any, pattern: str) -> list[tuple[Any, str]]:
    selector = _action_host_selector(pattern)
    modal_scopes = page.locator('[role="dialog"]:visible, [aria-modal="true"]:visible')
    if await modal_scopes.count():
        modal_hosts = modal_scopes.last.locator(selector)
        visible_modal_hosts = []
        for index in range(await modal_hosts.count()):
            host = modal_hosts.nth(index)
            if await host.is_visible(timeout=250):
                visible_modal_hosts.append(host)
        if visible_modal_hosts:
            return [(host, "action_id_visible_modal") for host in visible_modal_hosts]

    hosts = page.locator(selector)
    visible_hosts = []
    for index in range(await hosts.count()):
        host = hosts.nth(index)
        if await host.is_visible(timeout=250):
            visible_hosts.append((host, "action_id_visible"))
    return visible_hosts


def _quoted_action_labels(text: str) -> list[str]:
    return [label.strip() for label in re.findall(r'[\u201c"\u300c]([^\u201d"\u300d]+)[\u201d"\u300d]', text) if label.strip()]


def _visible_semantic_control(page: Page, text: str) -> tuple[Any, str] | None:
    modal_scopes = page.locator('[role="dialog"]:visible, [aria-modal="true"]:visible')
    scopes = [modal_scopes.nth(index) for index in reversed(range(modal_scopes.count()))]
    for scope in [*scopes, page]:
        for locator, strategy in (
            (scope.get_by_role("button", name=text, exact=True), "semantic_button"),
            (scope.get_by_text(text, exact=True), "semantic_text"),
        ):
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible(timeout=250):
                    prefix = "modal_" if scope is not page else ""
                    return candidate, f"{prefix}{strategy}"
    return None


async def _visible_semantic_control_async(page: Any, text: str) -> tuple[Any, str] | None:
    modal_scopes = page.locator('[role="dialog"]:visible, [aria-modal="true"]:visible')
    scopes = [modal_scopes.nth(index) for index in reversed(range(await modal_scopes.count()))]
    for scope in [*scopes, page]:
        candidates = (
            (scope.get_by_role("button", name=text, exact=True), "semantic_button"),
            (scope.get_by_text(text, exact=True), "semantic_text"),
        )
        for locator, strategy in candidates:
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=250):
                    prefix = "modal_" if scope is not page else ""
                    return candidate, f"{prefix}{strategy}"
    return None


def _action_host_selectors(patterns: list[str]) -> str:
    return ", ".join(_action_host_selector(pattern) for pattern in patterns)


def _current_page_filename(page: Any, fallback: str = "") -> str:
    try:
        name = Path(unquote(urlparse(str(page.url or "")).path)).name
    except Exception:
        name = ""
    return name or fallback


def _derive_step_operation(
    step: dict,
    obligation: dict,
    input_text: str = "",
    *,
    action_host_present: bool = False,
) -> tuple[str, str, str]:
    operation = str(obligation.get("operation") or "")
    if operation in {"double-click", "dblclick"}:
        operation = "double_click"
    if operation:
        return operation, input_text, "obligation"

    raw_action = str(step.get("action") or "")
    desc = str(step.get("description") or "")
    if raw_action == "init" and action_host_present:
        return "click", input_text, "action_host_for_init"
    if raw_action == "init" and ("输入" in desc or "type" in desc.lower()):
        if not input_text:
            quoted = re.findall(r'["""\u300c]([^"""\u300d]+)["""\u300d]', desc)
            if quoted:
                input_text = quoted[0]
        return "fill", input_text, "init_input_rewrite"
    if raw_action == "click":
        return "click", input_text, "capture_action"
    if raw_action in {"double_click", "double-click", "dblclick"}:
        return "double_click", input_text, "capture_action"
    if raw_action == "scroll":
        return "scroll", input_text, "capture_action"
    if raw_action == "type":
        return "fill", input_text, "capture_action"
    return raw_action, input_text, "capture_action"


def _page_has_action_host(page: Page, patterns: list[str]) -> bool:
    try:
        return bool(page.query_selector(_action_host_selectors(patterns)))
    except Exception:
        return False


async def _page_has_action_host_async(page, patterns: list[str]) -> bool:
    try:
        return bool(await page.query_selector(_action_host_selectors(patterns)))
    except Exception:
        return False


def _stabilize_visual_hit_testing(page: Page) -> bool:
    try:
        page.evaluate(
            "() => new Promise(resolve => "
            "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
    except Exception:
        pass
    try:
        page.screenshot(type="png", timeout=3000)
        return True
    except Exception:
        return False


async def _stabilize_visual_hit_testing_async(page: Any) -> bool:
    try:
        await page.evaluate(
            "() => new Promise(resolve => "
            "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
    except Exception:
        pass
    try:
        await page.screenshot(type="png", timeout=3000)
        return True
    except Exception:
        return False


def _execute_step(
    page: Page,
    step: dict,
    obligation: dict,
    trace_id: str,
    pages_dir: Path,
    base_url: str,
    prev_filename: str | None,
) -> tuple[str, str | None, dict[str, Any]]:

    si = step.get("step_index", step.get("step", 0))
    obs_key = f"{trace_id}:{si}"


    filename = obligation.get("source_filename") or obligation.get("expected_filename") or ""
    if not filename:

        filename = step.get("source_filename") or step.get("expected_filename") or ""


    if prev_filename is not None and filename and filename != prev_filename:
        audit = _base_step_audit(trace_id, si, "", filename)
        audit["expected_source_filename"] = filename
        audit["actual_source_filename"] = prev_filename
        return ("source_page_mismatch", prev_filename, audit)
    if filename and prev_filename is None:
        page_path = pages_dir / filename
        if not page_path.exists():
            return ("page_missing", filename, _base_step_audit(trace_id, si, "", filename))
        try:
            page.goto(f"{base_url}/{filename}", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(1000)
        except Exception as e:
            return ("nav_error", filename, _base_step_audit(trace_id, si, "", filename))

    try:
        page.evaluate(_SET_EXECUTION_CONTEXT_JS, _execution_context(trace_id, si))
    except Exception:
        pass


    operation = obligation.get("operation", "")
    input_text = obligation.get("input_text", "")
    gesture = obligation.get("gesture", {})
    hit_point = gesture.get("hit_point", {})
    hit_x = hit_point.get("x", gesture.get("x"))
    hit_y = hit_point.get("y", gesture.get("y"))

    action_patterns = [f"{trace_id}:step{si}", f"{trace_id}:{si}"]
    operation, input_text, operation_source = _derive_step_operation(
        step,
        obligation,
        input_text,
        action_host_present=_page_has_action_host(page, action_patterns),
    )

    audit = _base_step_audit(trace_id, si, operation, filename)
    audit["operation_source"] = operation_source


    if operation in {"click", "double_click"}:
        audit["visual_frame_stabilized"] = _stabilize_visual_hit_testing(page)
        found = False

        for pattern in action_patterns:
            candidates = _visible_action_host_candidates(page, pattern)
            for el, targeting_strategy in candidates:
                audit["targeting_strategy"] = targeting_strategy
                audit["selected_action_id"] = pattern
                before_store = None
                try:
                    action_map_status = {}
                    try:
                        action_map_status = page.evaluate(_ACTION_MAP_STATUS_JS, pattern) or {}
                        before_store = action_map_status.get("runtime_store")
                        audit["action_map_has_action_id"] = bool(action_map_status.get("action_map_has_action_id"))
                        audit["has_action_map"] = bool(action_map_status.get("has_action_map"))
                        audit["has_emit_for_action"] = bool(action_map_status.get("has_emit_for_action"))
                        audit["checkpoint_ids"] = list(action_map_status.get("checkpoint_ids") or [])
                    except Exception:
                        pass
                    el.scroll_into_view_if_needed(timeout=2000)
                    if operation == "double_click":
                        el.dblclick(timeout=2000)
                    else:
                        el.click(timeout=2000)
                    try:
                        after_click_status = page.evaluate(_ACTION_MAP_STATUS_JS, pattern) or {}
                        audit["runtime_store_delta"] = _runtime_store_delta(
                            before_store,
                            after_click_status.get("runtime_store"),
                        )
                    except Exception:
                        pass
                    found = True
                except Exception as exc:
                    audit["action_id_click_error"] = str(exc)[:500]
                if found:
                    break
            if found:
                break


        if not found:
            target_text = obligation.get("operation_intent", "") or step.get("description", "")
            for text in _quoted_action_labels(target_text)[:3]:
                try:
                    semantic_target = _visible_semantic_control(page, text)
                    if semantic_target:
                        loc, targeting_strategy = semantic_target
                        audit["targeting_strategy"] = targeting_strategy
                        audit["selected_text"] = text
                        if operation == "double_click":
                            loc.dblclick(timeout=2000)
                        else:
                            loc.click(timeout=2000)
                        found = True
                        break
                except Exception as exc:
                    audit["text_click_error"] = str(exc)[:500]


        if not found and hit_x is not None and hit_y is not None:
            try:
                audit["targeting_strategy"] = "hit_point"
                action_id = f"{trace_id}:step{si}"
                before_store = None
                try:
                    action_map_status = page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
                    before_store = action_map_status.get("runtime_store")
                    audit["selected_action_id"] = action_id
                    audit["action_map_has_action_id"] = bool(action_map_status.get("action_map_has_action_id"))
                    audit["has_action_map"] = bool(action_map_status.get("has_action_map"))
                    audit["has_emit_for_action"] = bool(action_map_status.get("has_emit_for_action"))
                    audit["checkpoint_ids"] = list(action_map_status.get("checkpoint_ids") or [])
                except Exception:
                    pass
                if operation == "double_click":
                    page.mouse.dblclick(int(hit_x), int(hit_y))
                else:
                    page.mouse.click(int(hit_x), int(hit_y))
                try:
                    after_click_status = page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
                    audit["runtime_store_delta"] = _runtime_store_delta(
                        before_store,
                        after_click_status.get("runtime_store"),
                    )
                except Exception:
                    pass
                found = True
            except Exception as exc:
                audit["hit_point_click_error"] = str(exc)[:500]

        if found:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception as exc:
                audit["load_state_wait_error"] = str(exc)[:500]
        page.wait_for_timeout(700 if found else 300)
        return (
            "executed" if found else "not_found",
            _current_page_filename(page, filename),
            audit,
        )

    elif operation == "fill":
        text_to_fill = input_text
        if not text_to_fill:
            desc = step.get("description", "")
            desc_quoted = re.findall(r'["""\u300c]([^"""\u300d]+)["""\u300d]', desc)
            if desc_quoted:
                text_to_fill = desc_quoted[0]

        if text_to_fill:
            input_el = None
            for pattern in [f"{trace_id}:step{si}", f"{trace_id}:{si}"]:
                input_el = page.query_selector(
                    f'input[data-action-id="{pattern}"], input[data-action-aliases~="{pattern}"], '
                    f'input[data-element-id*="{pattern}"], input[data-element-aliases*="{pattern}"], '
                    f'textarea[data-action-id="{pattern}"], textarea[data-action-aliases~="{pattern}"], '
                    f'textarea[data-element-id*="{pattern}"], textarea[data-element-aliases*="{pattern}"], '
                    f'[contenteditable][data-action-id="{pattern}"], [contenteditable][data-action-aliases~="{pattern}"], '
                    f'[contenteditable][data-element-id*="{pattern}"], [contenteditable][data-element-aliases*="{pattern}"], '
                    f'[role="searchbox"][data-action-id="{pattern}"], [role="searchbox"][data-action-aliases~="{pattern}"], '
                    f'[role="searchbox"][data-element-id*="{pattern}"], [role="searchbox"][data-element-aliases*="{pattern}"]'
                )
                if input_el:
                    break
            if not input_el:
                for pattern in [f"{trace_id}:step{si}", f"{trace_id}:{si}"]:
                    input_el = page.query_selector(
                        f'[data-action-id="{pattern}"], [data-action-aliases~="{pattern}"], '
                        f'[data-element-id*="{pattern}"], [data-element-aliases*="{pattern}"]'
                    )
                    if input_el:
                        break
            if not input_el:
                input_el = page.query_selector(
                    'input:visible, textarea:visible, [contenteditable]:visible, [role="searchbox"]:visible'
                )
            if input_el:
                try:
                    input_el.fill(text_to_fill, timeout=2000)
                    page.wait_for_timeout(400)
                    return ("executed", _current_page_filename(page, filename), audit)
                except Exception:
                    return ("fill_failed", _current_page_filename(page, filename), audit)
            return ("input_not_found", _current_page_filename(page, filename), audit)
        return ("no_text", _current_page_filename(page, filename), audit)

    elif operation in ("scroll", "wheel_drag"):


        dy = gesture.get("dy", 200) if gesture else 200
        action_id = f"{trace_id}:step{si}"
        host = None
        before_store = None
        gesture_action = ""
        try:
            action_map_status = page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
            before_store = action_map_status.get("runtime_store")
            audit["targeting_strategy"] = "action_id"
            audit["selected_action_id"] = action_id
            audit["action_map_has_action_id"] = bool(action_map_status.get("action_map_has_action_id"))
            audit["has_action_map"] = bool(action_map_status.get("has_action_map"))
            audit["has_emit_for_action"] = bool(action_map_status.get("has_emit_for_action"))
            audit["checkpoint_ids"] = list(action_map_status.get("checkpoint_ids") or [])
        except Exception:
            pass
        try:
            host = page.query_selector(
                f'[data-action-id="{action_id}"], [data-action-aliases~="{action_id}"]'
            )
            if host:
                host.scroll_into_view_if_needed(timeout=2000)
                box = host.bounding_box()
                gesture_action = str(
                    host.get_attribute("data-ghostweb-gesture-action") or ""
                ).strip().lower()
                if box:
                    if gesture_action == "drag":
                        start_x = box["x"] + box["width"] * 0.75
                        end_x = box["x"] + box["width"] * 0.25
                        center_y = box["y"] + box["height"] * 0.5
                        page.mouse.move(start_x, center_y)
                        page.mouse.down()
                        page.mouse.move(end_x, center_y, steps=8)
                        page.mouse.up()
                        audit["targeting_strategy"] = "action_host_drag"
                    else:
                        page.mouse.move(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        audit["targeting_strategy"] = "action_host_wheel"
            if gesture_action != "drag":
                page.mouse.wheel(0, dy)
            page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            after_status = page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
            audit["runtime_store_delta"] = _runtime_store_delta(
                before_store,
                after_status.get("runtime_store"),
            )
        except Exception:
            pass
        return ("executed", _current_page_filename(page, filename), audit)

    else:

        return ("skipped", _current_page_filename(page, filename), audit)


def _text_in_sources(
    needle: str,
    visible_text: str,
    form_values: dict,
    selected_elements: list,
    runtime_state: Any = None,
) -> bool:
    if needle in visible_text:
        return True
    for fv in form_values.values():
        if isinstance(fv, str) and needle in fv:
            return True
    for sel in selected_elements:
        if isinstance(sel, str) and needle in sel:
            return True
    return False


def _strict_runtime_field_source(field: str, runtime_checkpoint_data: dict) -> str:
    source = _runtime_field_source(field, runtime_checkpoint_data)
    if source == "missing":
        return "missing"
    if source.startswith("runtime_store:"):
        if source == "runtime_store:dom_projection":
            return "non_authoritative:runtime_store:dom_projection"
        return source
    if field in {"current_page", "terminal_page"} and source == "location":
        return source
    return f"non_authoritative:{source}"


def _strict_runtime_field_present(field: str, runtime_checkpoint_data: dict) -> bool:
    source = _strict_runtime_field_source(field, runtime_checkpoint_data)
    if source == "missing" or source.startswith("non_authoritative:"):
        return False
    value = _runtime_field_value(field, runtime_checkpoint_data)
    return value is not None and value != "" and value != [] and value != {}


def _strict_runtime_field_value(field: str, runtime_checkpoint_data: dict) -> Any:
    if not _strict_runtime_field_present(field, runtime_checkpoint_data):
        return None
    return _runtime_field_value(field, runtime_checkpoint_data)


def _evaluate_runtime_checkpoint_assertions(
    assertions: list[dict],
    runtime_checkpoint_data: dict,
) -> list[dict]:
    results = []
    visible_text = runtime_checkpoint_data.get("visible_text", "")
    form_values = runtime_checkpoint_data.get("form_values", {})
    selected_elements = runtime_checkpoint_data.get("selected_elements", [])
    runtime_state = runtime_checkpoint_data.get("runtime_state", {})

    for assertion in assertions:
        op = assertion.get("op", "")
        value = assertion.get("value", "")
        field = assertion.get("field", "")
        explicit_source_field = assertion.get("source_state_field") or assertion.get("source_field")
        lookup_field = explicit_source_field or field
        page_filter = assertion.get("page", "")
        assertion_source = "not_evaluated"

        passed = False
        if op in {"contains_visible", "contains"}:
            if explicit_source_field:
                if _strict_runtime_field_present(lookup_field, runtime_checkpoint_data):
                    actual_value = _strict_runtime_field_value(lookup_field, runtime_checkpoint_data)
                    passed = _runtime_values_contain(actual_value, value)
                    assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
                else:
                    assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
            elif isinstance(value, list):
                passed = all(
                    _text_in_sources(str(item), visible_text, form_values, selected_elements, runtime_state)
                    for item in value
                    if item is not None and item != ""
                )
                if passed:
                    assertion_source = _value_assertion_source(value, runtime_checkpoint_data)
            else:

                text_value = str(value)
                if _text_in_sources(text_value, visible_text, form_values, selected_elements, runtime_state):
                    passed = True
                    assertion_source = _value_assertion_source(text_value, runtime_checkpoint_data)
        elif op == "selected_contains":
            needle = str(value)
            if explicit_source_field:
                actual = _strict_runtime_field_value(lookup_field, runtime_checkpoint_data)
                passed = any(needle in str(item) for item in _iter_runtime_checkpoint_values(actual))
                assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
            else:
                selected_values = list(selected_elements or [])
                passed = any(needle in str(item) for item in selected_values)
                if passed:
                    assertion_source = "dom_selection"
            if passed and not explicit_source_field:
                assertion_source = _value_assertion_source(needle, runtime_checkpoint_data)
        elif op in {"set_contains_all", "set_equals"}:
            actual = _strict_runtime_field_value(lookup_field, runtime_checkpoint_data)
            actual_values = [str(item) for item in _iter_runtime_checkpoint_values(actual)]
            expected_values = [str(item) for item in _iter_runtime_checkpoint_values(value)]
            passed = all(any(expected in actual for actual in actual_values) for expected in expected_values)
            if op == "set_equals" and passed:
                passed = len(set(actual_values)) == len(set(expected_values))
            assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data) if lookup_field else "missing"
        elif op in {"count_eq", "numeric_eq"}:
            actual = _strict_runtime_field_value(lookup_field, runtime_checkpoint_data)
            try:
                passed = float(actual) == float(value)
            except (TypeError, ValueError):
                passed = False
            assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
        elif op == "numeric_range_eq":
            actual = _strict_runtime_field_value(lookup_field, runtime_checkpoint_data)
            if isinstance(actual, dict):
                passed = (
                    _runtime_values_equal(actual.get("min"), assertion.get("min"))
                    and _runtime_values_equal(actual.get("max"), assertion.get("max"))
                )
            assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
        elif op == "eq":

            if value == "current_page" and page_filter:
                passed = str(runtime_checkpoint_data.get("current_page", "")) == str(page_filter)
                assertion_source = "location"
            elif lookup_field:
                passed = (
                    _strict_runtime_field_present(lookup_field, runtime_checkpoint_data)
                    and _runtime_values_equal(_strict_runtime_field_value(lookup_field, runtime_checkpoint_data), value)
                )
                assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
            else:
                passed = str(runtime_checkpoint_data.get(lookup_field, "")) == str(value)
                assertion_source = "top_level_capture"
        elif op == "page_loaded":
            passed = str(runtime_checkpoint_data.get("current_page", "")) == str(value)
            assertion_source = "location"
        elif op in {"is_visible", "enabled"}:
            passed = _text_in_sources(str(value), visible_text, form_values, selected_elements, runtime_state)
            if passed:
                assertion_source = _value_assertion_source(value, runtime_checkpoint_data)
        elif op == "not_contains":
            passed = not _text_in_sources(str(value), visible_text, form_values, selected_elements, runtime_state)
            assertion_source = "negative_dom_projection"
        elif op == "has_runtime_field":
            passed = _strict_runtime_field_present(lookup_field, runtime_checkpoint_data)
            assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
        elif op == "present":
            if lookup_field:
                passed = _strict_runtime_field_present(lookup_field, runtime_checkpoint_data)
                assertion_source = _strict_runtime_field_source(lookup_field, runtime_checkpoint_data)
            else:
                passed = bool(value in visible_text)
                assertion_source = "dom_projection" if passed else "not_found"

        results.append({
            "field": field or assertion.get("page", ""),
            "source_state_field": lookup_field if lookup_field != field else "",
            "op": op,
            "value": value,
            "passed": passed,
            "assertion_source": assertion_source if passed else assertion_source or "not_found",
        })

    return results


def _runtime_field_present(field: str, runtime_checkpoint_data: dict) -> bool:
    source = _runtime_field_source(field, runtime_checkpoint_data)
    if source != "missing":
        value = _runtime_field_value(field, runtime_checkpoint_data)
        return value is not None and value != "" and value != [] and value != {}
    runtime_state = runtime_checkpoint_data.get("runtime_state") or {}
    if isinstance(runtime_state, dict):
        field_prefix = field + "."
        return any(k.startswith(field_prefix) and runtime_state.get(k) for k in runtime_state)
    return False


def _runtime_field_source(field: str, runtime_checkpoint_data: dict) -> str:
    if field in {"current_page", "terminal_page"}:
        return "location" if "current_page" in runtime_checkpoint_data else "missing"
    runtime_store = _runtime_store_snapshot(runtime_checkpoint_data)
    for bucket_name in ("committed_state", "current_state"):
        bucket = runtime_store.get(bucket_name) if isinstance(runtime_store, dict) else None
        if isinstance(bucket, dict) and field in bucket:
            source_index = runtime_store.get("source_index") or {}
            source_meta = source_index.get(field) if isinstance(source_index, dict) else None
            if isinstance(source_meta, dict) and source_meta.get("source"):
                return f"runtime_store:{source_meta.get('source')}"
            return f"runtime_store:{bucket_name}"
    if field in runtime_checkpoint_data:
        return "top_level_capture"
    if field in (runtime_checkpoint_data.get("form_values") or {}):
        return "dom_form"
    runtime_state = runtime_checkpoint_data.get("runtime_state") or {}
    if isinstance(runtime_state, dict) and field in runtime_state:
        if field.startswith("state::"):
            return "runtime_state"
        return "dom_projection"
    if isinstance(runtime_state, dict):
        projected_key = _runtime_state_projected_key(field, runtime_state)
        if projected_key:
            return "dom_projection"
    return "missing"


def _runtime_field_value(field: str, runtime_checkpoint_data: dict) -> Any:
    if field in {"current_page", "terminal_page"}:
        return runtime_checkpoint_data.get("current_page")
    runtime_store = _runtime_store_snapshot(runtime_checkpoint_data)
    if isinstance(runtime_store, dict):
        for bucket_name in ("committed_state", "current_state"):
            bucket = runtime_store.get(bucket_name)
            if isinstance(bucket, dict) and field in bucket:
                return bucket.get(field)
    if field in runtime_checkpoint_data:
        return runtime_checkpoint_data.get(field)
    form_values = runtime_checkpoint_data.get("form_values") or {}
    if field in form_values:
        return form_values.get(field)
    runtime_state = runtime_checkpoint_data.get("runtime_state") or {}
    if isinstance(runtime_state, dict):
        if field in runtime_state:
            return runtime_state.get(field)
        projected_key = _runtime_state_projected_key(field, runtime_state)
        if projected_key:
            return runtime_state.get(projected_key)
    return None


def _runtime_state_projected_key(field: str, runtime_state: dict) -> str | None:
    if not field:
        return None
    for suffix in (".value", ".text", ".label"):
        key = field + suffix
        if key in runtime_state and runtime_state.get(key) not in (None, "", [], {}):
            return key
    field_prefix = field + "."
    for key, value in runtime_state.items():
        if key.startswith(field_prefix) and value not in (None, "", [], {}):
            return key
    return None


def _value_assertion_source(value: Any, runtime_checkpoint_data: dict) -> str:
    values = [str(item) for item in _iter_runtime_checkpoint_values(value)]
    runtime_store = _runtime_store_snapshot(runtime_checkpoint_data)
    if isinstance(runtime_store, dict):
        for bucket_name in ("committed_state", "current_state"):
            bucket = runtime_store.get(bucket_name)
            if not isinstance(bucket, dict):
                continue
            haystack = " ".join(str(item) for item in _iter_runtime_checkpoint_values(bucket))
            if all(v in haystack for v in values if v):
                return f"runtime_store:{bucket_name}"
    form_values = runtime_checkpoint_data.get("form_values") or {}
    if any(str(v) in str(item) for v in values for item in form_values.values()):
        return "dom_form"
    selected = runtime_checkpoint_data.get("selected_elements") or []
    if any(str(v) in str(item) for v in values for item in selected):
        return "dom_selection"
    visible_text = runtime_checkpoint_data.get("visible_text", "")
    if any(str(v) and str(v) in visible_text for v in values):
        return "dom_visible_text"
    return "unknown_explicit_dom_source"


def _runtime_store_snapshot(runtime_checkpoint_data: dict) -> dict:
    runtime_store = runtime_checkpoint_data.get("runtime_store")
    if isinstance(runtime_store, dict) and runtime_store:
        return runtime_store
    runtime_state = runtime_checkpoint_data.get("runtime_state") or {}
    if isinstance(runtime_state, dict):
        nested = runtime_state.get("__runtime_store__")
        if isinstance(nested, dict):
            return nested
    return {}


def _runtime_values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return bool(actual) is expected
    if isinstance(actual, (dict, list)) or isinstance(expected, (dict, list)):
        return actual == expected
    return str(actual) == str(expected)


def _runtime_values_contain(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return bool(actual) is expected
    needles = [str(item) for item in _iter_runtime_checkpoint_values(expected) if item not in (None, "")]
    if not needles:
        return True
    haystack = " ".join(str(item) for item in _iter_runtime_checkpoint_values(actual) if item not in (None, ""))
    return all(needle in haystack or haystack in needle for needle in needles)


def _iter_runtime_checkpoint_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        values: list[Any] = []
        for item in value:
            values.extend(_iter_runtime_checkpoint_values(item))
        return values
    if isinstance(value, dict):
        values: list[Any] = []
        for item in value.values():
            values.extend(_iter_runtime_checkpoint_values(item))
        return values
    return [value]


def _checkpoint_runtime_assertions(cp_data: dict, runtime_schema_entry: dict | None = None) -> list[dict]:
    assertions = list(cp_data.get("runtime_checkpoint_assertions") or [])
    assertions.extend(_runtime_schema_presence_assertions(runtime_schema_entry))
    authoritative_source_fields = _authoritative_projection_source_fields(cp_data)
    for item in cp_data.get("projection_assertions") or []:
        normalized = _normalize_projection_assertion(item)
        if str(normalized.get("field") or "") in authoritative_source_fields:
            continue
        assertions.append(normalized)
    state_like_items: list[tuple[str, Any]] = []
    for group_name, assertion_group in (
        ("state_assertions", cp_data.get("state_assertions") or {}),
        ("diff_assertions", cp_data.get("diff_assertions") or {}),
        ("canonical_diff_assertions", cp_data.get("canonical_diff_assertions") or {}),
        ("downstream_diff_assertions", cp_data.get("downstream_diff_assertions") or {}),
    ):
        if isinstance(assertion_group, dict):
            for field, raw_spec in assertion_group.items():
                if group_name in {"state_assertions", "diff_assertions"} and str(field) in authoritative_source_fields:
                    continue
                state_like_items.append((group_name, field, raw_spec))

    visible_state_fields = {
        "draft",
        "draft_values",
        "form_values",
        "input_values",
    }
    selected_state_fields = {
        "selected",
        "selected_value",
        "selected_values",
        "selection",
        "selection_values",
        "checked",
        "checked_values",
    }
    presence_only_fields = {
        "mutated_values",
        "mutation_values",
    }
    page_fields = {"current_page", "page", "snapshot_page"}
    dom_authority_fields: set[str] = set()
    if runtime_schema_entry:
        for schema_field in runtime_schema_entry.get("fields") or []:
            if not isinstance(schema_field, dict):
                continue
            name = str(schema_field.get("field") or "")
            authority = str(schema_field.get("authority") or "").lower()
            field_scope = str(schema_field.get("field_scope") or "").lower()
            sources = {str(src).lower() for src in (schema_field.get("sources") or [])}
            structured_required = bool(schema_field.get("structured_required"))
            if (
                name
                and (
                    authority in {"terminal_dom_state", "dom_projection", "visible_dom_state"}
                    or (
                        field_scope == "terminal_observable"
                        and not sources.intersection({"runtime_state", "runtime_store"})
                        and not structured_required
                    )
                )
            ):
                dom_authority_fields.add(name)

    for group_name, field, raw_spec in state_like_items:
        spec = raw_spec if isinstance(raw_spec, dict) else {"value": raw_spec}
        value = spec.get("value")
        field_l = str(field).split(".")[-1].lower()
        structured_projection_group = group_name in {"canonical_diff_assertions", "downstream_diff_assertions"}
        if field_l in presence_only_fields:
            assertions.append({
                "field": field,
                "op": "has_runtime_field",
                "value": True,
                "source_state_field": field,
                "assertion_channel": "runtime_state_schema",
            })
            continue
        if field_l in page_fields and value:
            item = {
                "field": "current_page",
                "op": "eq",
                "value": value,
            }
            if str(field) not in {"runtime.page", "current_page", "page", "terminal_page"}:
                item["source_state_field"] = field
            assertions.append(item)
            continue
        if structured_projection_group and spec.get("op") in {"eq", "", None} and value is not None and value != "":
            assertions.append({
                "field": field,
                "op": "eq",
                "value": value,
                "source_state_field": field,
                "assertion_channel": "runtime_state_schema",
            })
            continue
        if spec.get("op") == "selected_contains":
            for item in _iter_runtime_checkpoint_values(value):
                if item is None or item == "":
                    continue
                assertions.append({
                    "field": field,
                    "op": "selected_contains",
                    "value": str(item),
                    "source_state_field": field,
                })
            continue
        if field_l in selected_state_fields:
            for item in _iter_runtime_checkpoint_values(value):
                if item is None or item == "":
                    continue
                assertions.append({
                    "field": field,
                    "op": "selected_contains",
                    "value": str(item),
                    "source_state_field": field,
                })
            continue
        if isinstance(value, list):
            assertions.append({
                "field": field,
                "op": "set_contains_all" if spec.get("op") != "set_equals" else "set_equals",
                "value": value,
                "source_state_field": field,
            })
            continue
        if not any(token in field_l for token in visible_state_fields):

            if value is not None and value != "":
                if isinstance(value, bool):
                    continue
                str_value = str(value)


                _action_intent_markers = (
                    "点击", "选择", "搜索", "输入", "滑动", "长按", "拖动",
                    "返回", "关闭", "打开", "确认", "提交", "取消",
                    "click", "click_event", "tap", "swipe", "scroll",
                    "immediate_or_default", "accepted_or_default_true",
                    "submit_available",
                )
                if any(marker in str_value for marker in _action_intent_markers):
                    continue
                if len(str_value) > 1:
                    item = {
                        "field": field,
                        "op": "contains_visible",
                        "value": str_value,
                    }
                    if field not in dom_authority_fields:
                        item["source_state_field"] = field
                    assertions.append(item)
            continue
        for item in _iter_runtime_checkpoint_values(value):
            if item is None or item == "":
                continue
            assertion = {
                "op": "contains_visible",
                "value": str(item),
            }
            if field not in dom_authority_fields:
                assertion["source_state_field"] = field
            assertions.append(assertion)
    return assertions


def _authoritative_projection_source_fields(cp_data: dict) -> set[str]:
    source_fields: set[str] = set()
    for group_name in ("canonical_diff_assertions", "downstream_diff_assertions"):
        group = cp_data.get(group_name) or {}
        if not isinstance(group, dict):
            continue
        for projected_field, raw_spec in group.items():
            if not isinstance(raw_spec, dict):
                continue
            source_field = str(raw_spec.get("source_field") or "").strip()
            if source_field:
                source_fields.add(source_field)
                continue
            projected = str(projected_field or "")
            if "." in projected:
                source_fields.add(projected.rsplit(".", 1)[-1])
    return source_fields


def _runtime_schema_presence_assertions(runtime_schema_entry: dict | None) -> list[dict]:
    if not runtime_schema_entry:
        return []
    assertions: list[dict] = []
    for field in runtime_schema_entry.get("fields") or []:
        if not isinstance(field, dict) or not field.get("structured_required"):
            continue
        authority = str(field.get("authority") or "").lower()
        field_scope = str(field.get("field_scope") or "").lower()
        sources = {str(src).lower() for src in (field.get("sources") or [])}
        structured_required = bool(field.get("structured_required"))
        if (
            authority in {"terminal_dom_state", "dom_projection", "visible_dom_state"}
            or (
                field_scope == "terminal_observable"
                and not sources.intersection({"runtime_state", "runtime_store"})
                and not structured_required
            )
        ):
            continue
        name = str(field.get("field") or "")
        if not name or name in {"page", "current_page", "terminal_page", "route"}:
            continue
        assertions.append({
            "field": name,
            "op": "has_runtime_field",
            "value": True,
            "source_state_field": name,
            "assertion_channel": "runtime_state_schema",
        })
    return assertions


def _normalize_projection_assertion(assertion: dict) -> dict:
    out = dict(assertion)
    if not out.get("op") and out.get("page"):
        out["field"] = "current_page"
        out["op"] = "eq"
        out["value"] = out["page"]
    if out.get("op") == "eq" and out.get("value") == "current_page" and out.get("page"):
        out["field"] = "current_page"
        out["value"] = out["page"]
    return out


def _reconcile_replayed_filename(prev_filename: str | None, snapshot_page: str) -> str | None:
    observed = str(snapshot_page or "").strip()
    return observed or prev_filename


def _last_replayable_source_step(
    source_steps: list[int],
    step_by_index: dict[int, dict],
    resolve_filename,
) -> int | None:
    normalized: list[int] = []
    for step in source_steps:
        try:
            normalized.append(int(step))
        except (TypeError, ValueError):
            continue
    for si in sorted(normalized, reverse=True):
        if si in step_by_index and resolve_filename(si):
            return si
    for si in sorted(normalized, reverse=True):
        if si in step_by_index:
            return si
    return max(normalized) if normalized else None


_SET_EXECUTION_CONTEXT_JS = """
(context) => {
    context = context || {};
    const traceId = context.reference_trace_id || context.task_id || '';
    const actionId = context.action_id || '';
    try { window.__ghostwebExecutionContext = context; } catch (e) {}
    try { window.GhostwebActiveTraceId = traceId; } catch (e) {}
    try { window.__ghostwebActiveTraceId = traceId; } catch (e) {}
    try { window.GhostwebActiveActionId = actionId; } catch (e) {}
    try { window.__ghostwebActiveActionId = actionId; } catch (e) {}
    try { localStorage.setItem('ghostweb.execution_context', JSON.stringify(context)); } catch (e) {}
    try {
        if (window.GhostwebRuntimeState && window.GhostwebRuntimeState.setTraceId && traceId) {
            window.GhostwebRuntimeState.setTraceId(traceId);
        }
    } catch (e) {}
    try {
        if (window.GhostwebCheckpointRuntime && window.GhostwebCheckpointRuntime.setTraceId && traceId) {
            window.GhostwebCheckpointRuntime.setTraceId(traceId);
        }
    } catch (e) {}
    try {
        if (typeof window.GhostwebMaterializeExecutionContext === 'function') {
            window.GhostwebMaterializeExecutionContext(context);
        }
    } catch (e) {}
    try {
        if (typeof window.GhostwebProjectSearchTerminalOutcome === 'function') {
            window.GhostwebProjectSearchTerminalOutcome(null);
        }
    } catch (e) {}
    try { if (typeof window.renderState === 'function') window.renderState(); } catch (e) {}
    try { if (typeof window.GhostwebStationNavSyncNearbyHost === 'function') window.GhostwebStationNavSyncNearbyHost(); } catch (e) {}
}
"""


def _execution_context(trace_id: str, step_index: int | None = None) -> dict[str, Any]:
    context: dict[str, Any] = {
        "reference_trace_id": trace_id,
        "task_id": trace_id,
    }
    if step_index is not None:
        context["step_index"] = step_index
        context["action_id"] = f"{trace_id}:step{step_index}"
    return context


_CAPTURE_PROJECTION_JS = """
(context) => {
    const rt = window.GhostwebCheckpointRuntime;
    if (!rt) return { __bridge_missing: true };

    const state = rt.captureState();
    const formValues = {};
    const selectedElements = [];

    // Extract form values
    document.querySelectorAll('input, textarea, [contenteditable]').forEach(el => {
        const key = el.id || el.name || el.getAttribute('aria-label') || el.getAttribute('data-element-id') || '';
        if (key) {
            const val = el.value !== undefined ? el.value : (el.textContent || '');
            if (val) formValues[key] = val;
        }
    });

    // Extract selected/checked/active elements
    document.querySelectorAll('[aria-checked="true"], [aria-selected="true"], [data-checked="true"], [data-selected="true"], .selected, .checked, .active').forEach(el => {
        const key = el.id || el.getAttribute('data-element-id') || '';
        const text = (el.textContent || '').trim().substring(0, 100);
        const dataVal = el.getAttribute('data-value') || '';
        if (dataVal) selectedElements.push(dataVal);
        selectedElements.push(key || text);
        if (key && text) selectedElements.push(text);
    });

    // CTA state: check if submit/send/confirm buttons are enabled
    const ctaState = {};
    const ctaSelectors = [
        'button[type="submit"]', 'input[type="submit"]',
        '[data-action="submit"]', '.submit-btn', '.btn-submit',
        'button'
    ];
    for (const sel of ctaSelectors) {
        const els = document.querySelectorAll(sel);
        els.forEach(el => {
            const text = (el.textContent || el.value || '').trim();
            if (text && /提交|发送|确认|确定|下单|submit|send|confirm/i.test(text)) {
                ctaState[text] = !el.disabled;
            }
        });
    }

    const page = (location.pathname.split('/').pop() || '');
    const visibleText = (document.body ? document.body.innerText : '').substring(0, 2000);
    const runtimeStore = (window.GhostwebRuntimeState && window.GhostwebRuntimeState.snapshot)
        ? window.GhostwebRuntimeState.snapshot()
        : null;

    return {
        current_page: page,
        visible_text: visibleText,
        form_values: formValues,
        selected_elements: selectedElements,
        cta_state: ctaState,
        runtime_state: state,
        runtime_store: runtimeStore,
        runtime_store_present: !!runtimeStore,
        __bridge_missing: false,
    };
}
"""


def _capture_runtime_checkpoint(page: Page, trace_id: str = "", step_index: int | None = None) -> dict:
    try:
        try:
            page.wait_for_function("() => !!window.GhostwebCheckpointRuntime", timeout=2000)
        except Exception:
            pass
        context = _execution_context(trace_id, step_index)
        try:
            page.evaluate(_SET_EXECUTION_CONTEXT_JS, context)
        except Exception:
            pass
        result = page.evaluate(_CAPTURE_PROJECTION_JS, context)
        return result or {"__bridge_missing": True}
    except Exception as e:
        return {"__bridge_missing": True, "__error": str(e)[:200]}


async def _execute_step_async(
    page,
    step: dict,
    obligation: dict,
    trace_id: str,
    pages_dir: Path,
    base_url: str,
    prev_filename: str | None,
) -> tuple[str, str | None, dict[str, Any]]:
    si = step.get("step_index", step.get("step", 0))
    filename = obligation.get("source_filename") or obligation.get("expected_filename") or ""
    if not filename:
        filename = step.get("source_filename") or step.get("expected_filename") or ""

    if prev_filename is not None and filename and filename != prev_filename:
        audit = _base_step_audit(trace_id, si, "", filename)
        audit["expected_source_filename"] = filename
        audit["actual_source_filename"] = prev_filename
        return ("source_page_mismatch", prev_filename, audit)
    if filename and prev_filename is None:
        page_path = pages_dir / filename
        if not page_path.exists():
            return ("page_missing", filename, _base_step_audit(trace_id, si, "", filename))
        try:
            await page.goto(f"{base_url}/{filename}", wait_until="domcontentloaded", timeout=10000)
            await page.wait_for_timeout(1000)
        except Exception:
            return ("nav_error", filename, _base_step_audit(trace_id, si, "", filename))

    try:
        await page.evaluate(_SET_EXECUTION_CONTEXT_JS, _execution_context(trace_id, si))
    except Exception:
        pass

    operation = obligation.get("operation", "")
    input_text = obligation.get("input_text", "")
    gesture = obligation.get("gesture", {})
    hit_point = gesture.get("hit_point", {})
    hit_x = hit_point.get("x", gesture.get("x"))
    hit_y = hit_point.get("y", gesture.get("y"))

    action_patterns = [f"{trace_id}:step{si}", f"{trace_id}:{si}"]
    operation, input_text, operation_source = _derive_step_operation(
        step,
        obligation,
        input_text,
        action_host_present=await _page_has_action_host_async(page, action_patterns),
    )

    audit = _base_step_audit(trace_id, si, operation, filename)
    audit["operation_source"] = operation_source

    if operation in {"click", "double_click"}:
        audit["visual_frame_stabilized"] = await _stabilize_visual_hit_testing_async(page)
        found = False
        for pattern in action_patterns:
            candidates = await _visible_action_host_candidates_async(page, pattern)
            for el, targeting_strategy in candidates:
                audit["targeting_strategy"] = targeting_strategy
                audit["selected_action_id"] = pattern
                before_store = None
                try:
                    action_map_status = {}
                    try:
                        action_map_status = await page.evaluate(_ACTION_MAP_STATUS_JS, pattern) or {}
                        before_store = action_map_status.get("runtime_store")
                        audit["action_map_has_action_id"] = bool(action_map_status.get("action_map_has_action_id"))
                        audit["has_action_map"] = bool(action_map_status.get("has_action_map"))
                        audit["has_emit_for_action"] = bool(action_map_status.get("has_emit_for_action"))
                        audit["checkpoint_ids"] = list(action_map_status.get("checkpoint_ids") or [])
                    except Exception:
                        pass
                    await el.scroll_into_view_if_needed(timeout=2000)
                    if operation == "double_click":
                        await el.dblclick(timeout=2000)
                    else:
                        await el.click(timeout=2000)
                    try:
                        after_click_status = await page.evaluate(_ACTION_MAP_STATUS_JS, pattern) or {}
                        audit["runtime_store_delta"] = _runtime_store_delta(
                            before_store,
                            after_click_status.get("runtime_store"),
                        )
                    except Exception:
                        pass
                    found = True
                except Exception as exc:
                    audit["action_id_click_error"] = str(exc)[:500]
                if found:
                    break
            if found:
                break

        if not found:
            target_text = obligation.get("operation_intent", "") or step.get("description", "")
            for text in _quoted_action_labels(target_text)[:3]:
                try:
                    semantic_target = await _visible_semantic_control_async(page, text)
                    if semantic_target:
                        loc, targeting_strategy = semantic_target
                        audit["targeting_strategy"] = targeting_strategy
                        audit["selected_text"] = text
                        if operation == "double_click":
                            await loc.dblclick(timeout=2000)
                        else:
                            await loc.click(timeout=2000)
                        found = True
                        break
                except Exception as exc:
                    audit["text_click_error"] = str(exc)[:500]

        if not found and hit_x is not None and hit_y is not None:
            try:
                audit["targeting_strategy"] = "hit_point"
                action_id = f"{trace_id}:step{si}"
                before_store = None
                try:
                    action_map_status = await page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
                    before_store = action_map_status.get("runtime_store")
                    audit["selected_action_id"] = action_id
                    audit["action_map_has_action_id"] = bool(action_map_status.get("action_map_has_action_id"))
                    audit["has_action_map"] = bool(action_map_status.get("has_action_map"))
                    audit["has_emit_for_action"] = bool(action_map_status.get("has_emit_for_action"))
                    audit["checkpoint_ids"] = list(action_map_status.get("checkpoint_ids") or [])
                except Exception:
                    pass
                if operation == "double_click":
                    await page.mouse.dblclick(int(hit_x), int(hit_y))
                else:
                    await page.mouse.click(int(hit_x), int(hit_y))
                try:
                    after_click_status = await page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
                    audit["runtime_store_delta"] = _runtime_store_delta(
                        before_store,
                        after_click_status.get("runtime_store"),
                    )
                except Exception:
                    pass
                found = True
            except Exception as exc:
                audit["hit_point_click_error"] = str(exc)[:500]

        if found:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception as exc:
                audit["load_state_wait_error"] = str(exc)[:500]
        await page.wait_for_timeout(700 if found else 300)
        return (
            "executed" if found else "not_found",
            _current_page_filename(page, filename),
            audit,
        )

    if operation == "fill":
        text_to_fill = input_text
        if not text_to_fill:
            desc_quoted = re.findall(r'["""\u300c]([^"""\u300d]+)["""\u300d]', step.get("description", ""))
            if desc_quoted:
                text_to_fill = desc_quoted[0]
        if text_to_fill:
            input_el = None
            for pattern in [f"{trace_id}:step{si}", f"{trace_id}:{si}"]:
                input_el = await page.query_selector(
                    f'input[data-action-id="{pattern}"], input[data-action-aliases~="{pattern}"], '
                    f'input[data-element-id*="{pattern}"], input[data-element-aliases*="{pattern}"], '
                    f'textarea[data-action-id="{pattern}"], textarea[data-action-aliases~="{pattern}"], '
                    f'textarea[data-element-id*="{pattern}"], textarea[data-element-aliases*="{pattern}"], '
                    f'[contenteditable][data-action-id="{pattern}"], [contenteditable][data-action-aliases~="{pattern}"], '
                    f'[contenteditable][data-element-id*="{pattern}"], [contenteditable][data-element-aliases*="{pattern}"], '
                    f'[role="searchbox"][data-action-id="{pattern}"], [role="searchbox"][data-action-aliases~="{pattern}"], '
                    f'[role="searchbox"][data-element-id*="{pattern}"], [role="searchbox"][data-element-aliases*="{pattern}"]'
                )
                if input_el:
                    break
            if not input_el:
                for pattern in [f"{trace_id}:step{si}", f"{trace_id}:{si}"]:
                    input_el = await page.query_selector(
                        f'[data-action-id="{pattern}"], [data-action-aliases~="{pattern}"], '
                        f'[data-element-id*="{pattern}"], [data-element-aliases*="{pattern}"]'
                    )
                    if input_el:
                        break
            if not input_el:
                input_el = await page.query_selector(
                    'input:visible, textarea:visible, [contenteditable]:visible, [role="searchbox"]:visible'
                )
            if input_el:
                try:
                    await input_el.fill(text_to_fill, timeout=2000)
                    await page.wait_for_timeout(400)
                    return ("executed", _current_page_filename(page, filename), audit)
                except Exception:
                    return ("fill_failed", _current_page_filename(page, filename), audit)
            return ("input_not_found", _current_page_filename(page, filename), audit)
        return ("no_text", _current_page_filename(page, filename), audit)

    if operation in ("scroll", "wheel_drag"):
        dy = gesture.get("dy", 200) if gesture else 200
        action_id = f"{trace_id}:step{si}"
        before_store = None
        gesture_action = ""
        try:
            action_map_status = await page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
            before_store = action_map_status.get("runtime_store")
            audit["targeting_strategy"] = "action_id"
            audit["selected_action_id"] = action_id
            audit["action_map_has_action_id"] = bool(action_map_status.get("action_map_has_action_id"))
            audit["has_action_map"] = bool(action_map_status.get("has_action_map"))
            audit["has_emit_for_action"] = bool(action_map_status.get("has_emit_for_action"))
            audit["checkpoint_ids"] = list(action_map_status.get("checkpoint_ids") or [])
        except Exception:
            pass
        try:
            host = await page.query_selector(
                f'[data-action-id="{action_id}"], [data-action-aliases~="{action_id}"]'
            )
            if host:
                await host.scroll_into_view_if_needed(timeout=2000)
                box = await host.bounding_box()
                gesture_action = str(
                    await host.get_attribute("data-ghostweb-gesture-action") or ""
                ).strip().lower()
                if box:
                    if gesture_action == "drag":
                        start_x = box["x"] + box["width"] * 0.75
                        end_x = box["x"] + box["width"] * 0.25
                        center_y = box["y"] + box["height"] * 0.5
                        await page.mouse.move(start_x, center_y)
                        await page.mouse.down()
                        await page.mouse.move(end_x, center_y, steps=8)
                        await page.mouse.up()
                        audit["targeting_strategy"] = "action_host_drag"
                    else:
                        await page.mouse.move(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        audit["targeting_strategy"] = "action_host_wheel"
        except Exception:
            pass
        try:
            if gesture_action != "drag":
                await page.mouse.wheel(0, dy)
            await page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            after_status = await page.evaluate(_ACTION_MAP_STATUS_JS, action_id) or {}
            audit["runtime_store_delta"] = _runtime_store_delta(
                before_store,
                after_status.get("runtime_store"),
            )
        except Exception:
            pass
        return ("executed", _current_page_filename(page, filename), audit)

    return ("skipped", _current_page_filename(page, filename), audit)


async def _capture_runtime_checkpoint_async(page, trace_id: str = "", step_index: int | None = None) -> dict:
    try:
        try:
            await page.wait_for_function("() => !!window.GhostwebCheckpointRuntime", timeout=2000)
        except Exception:
            pass
        context = _execution_context(trace_id, step_index)
        try:
            await page.evaluate(_SET_EXECUTION_CONTEXT_JS, context)
        except Exception:
            pass
        result = await page.evaluate(_CAPTURE_PROJECTION_JS, context)
        return result or {"__bridge_missing": True}
    except Exception as e:
        return {"__bridge_missing": True, "__error": str(e)[:200]}


def collect_runtime_checkpoint_snapshots(
    result_dir: str,
    trace_id: str | None = None,
    base_url: str = "http://127.0.0.1:8765/pages",
    block_assets: bool = False,
    trace_timeout_ms: int | None = None,
    contract_source_dir: str | None = None,
) -> dict:
    rd = Path(result_dir)
    contract_root = Path(contract_source_dir) if contract_source_dir else rd
    environment_profile = load_environment_profile_from_result_dir(rd)
    viewport = dict(environment_profile["viewport"])
    pages_dir = rd / "phase_pagegen" / "pages"
    manifest_path = rd / "phase_post_processing" / "evaluators" / "executable_evaluator_manifest.json"
    capture_actions_path = contract_root / "phase_global_reasoning" / "debug" / "capture_actions.json"
    bindings_path = contract_root / "phase_consolidation" / "site_model" / "materialized_observation_bindings.json"


    manifest = _load_json(manifest_path)
    capture_actions = _load_json(capture_actions_path)
    bindings_data = _load_json(bindings_path)
    bindings = bindings_data.get("bindings_by_obs_key", bindings_data)
    action_host_filename_map = _load_action_host_filename_map(rd)
    runtime_schema_by_checkpoint = _load_runtime_state_schema_by_checkpoint(rd)


    obligations_cache: dict[str, list[dict]] = {}

    def _get_obligations(filename: str) -> list[dict]:
        if filename not in obligations_cache:
            page_path = pages_dir / filename
            if page_path.exists():
                obligations_cache[filename] = _parse_obligations(page_path.read_text(encoding="utf-8"))
            else:
                obligations_cache[filename] = []
        return obligations_cache[filename]


    evaluators = manifest.get("evaluators", {})
    if trace_id:
        trace_ids = [trace_id]
    else:
        trace_ids = list(evaluators.keys())

    result: dict[str, Any] = {
        "format": "ghostweb_checkpoint_runtime_validation",
        "schema_revision": 1,
        "environment_profile_hash": environment_profile["profile_hash"],
        "platform": environment_profile["platform"],
        "viewport": viewport,
        "traces": {},
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport=viewport)
        if block_assets:
            context.route("**/*", _asset_blocking_route_handler)

        for tid in trace_ids:
            print(f"\n[runtime-checkpoint] Processing {tid}...", flush=True)
            trace_start = time.monotonic()
            task_eval = evaluators.get(tid, {})
            if not task_eval:
                print(f"  skip: no evaluator entry", flush=True)
                continue

            steps = capture_actions.get(tid, [])
            if not steps:
                print(f"  skip: no capture_actions", flush=True)
                continue


            all_checkpoints = _merge_evaluator_checkpoints(task_eval)


            step_by_index: dict[int, dict] = {}
            for s in steps:
                si = s.get("step_index", s.get("step", 0))
                step_by_index[si] = s


            def _resolve_filename(si: int) -> str:
                return _resolve_step_source_filename(
                    action_host_filename_map,
                    bindings,
                    tid,
                    si,
                )

            trace_result: dict[str, Any] = {"checkpoints": {}, "step_execution_audit": {}}

            page = context.new_page()


            capture_points: dict[int, list[tuple[str, list[dict]]]] = {}
            for cp_id, cp_data in all_checkpoints.items():
                source_steps = cp_data.get("source_steps", [])
                if not source_steps:
                    continue
                last_step = _last_replayable_source_step(source_steps, step_by_index, _resolve_filename)
                if last_step is None:
                    continue
                runtime_checkpoint_assertions = _checkpoint_runtime_assertions(
                    cp_data,
                    runtime_schema_by_checkpoint.get((tid, cp_id)),
                )
                capture_points.setdefault(last_step, []).append(
                    (cp_id, runtime_checkpoint_assertions)
                )


            all_step_indices = sorted(si for si in step_by_index if si >= 0)
            if not all_step_indices:
                continue

            max_step = max(
                (max(cp_data.get("source_steps", [0])) for cp_data in all_checkpoints.values() if cp_data.get("source_steps")),
                default=0,
            )
            steps_to_replay = [si for si in all_step_indices if si <= max_step]

            prev_filename: str | None = None
            replayed_up_to = 0

            for si in steps_to_replay:
                if trace_timeout_ms and (time.monotonic() - trace_start) * 1000 > trace_timeout_ms:
                    for cp_step, cp_list in capture_points.items():
                        if cp_step >= si:
                            for cp_id, assertions in cp_list:
                                trace_result["checkpoints"][cp_id] = {
                                    "snapshot_page": prev_filename or "",
                                    "runtime_checkpoint_data": {},
                                    "assertions_checked": [],
                                    "all_passed": False,
                                    "replay_status": "trace_timeout",
                                }
                    trace_result["trace_status"] = "trace_timeout"
                    break

                step = step_by_index.get(si, {})
                fn = _resolve_filename(si)
                if not fn:
                    continue

                obligation = _find_obligation(_get_obligations(fn), tid, si)


                if not obligation.get("source_filename"):
                    obligation["source_filename"] = fn
                if not step.get("source_filename"):
                    step["source_filename"] = fn

                status, prev_filename, step_audit = _execute_step(
                    page, step, obligation, tid, pages_dir, base_url, prev_filename
                )
                step_audit["status"] = status
                trace_result.setdefault("step_execution_audit", {})[str(si)] = step_audit
                replayed_up_to = si

                if status in _TERMINAL_STEP_FAILURES:

                    for cp_step, cp_list in capture_points.items():
                        if cp_step >= si:
                            for cp_id, assertions in cp_list:
                                trace_result["checkpoints"][cp_id] = {
                                    "snapshot_page": fn,
                                    "runtime_checkpoint_data": {},
                                    "assertions_checked": [],
                                    "all_passed": False,
                                    "replay_status": status,
                                }
                    break


                if si in capture_points:
                    for cp_id, runtime_checkpoint_assertions in capture_points[si]:
                        print(f"  checkpoint {cp_id}: captured after step {si}", flush=True)

                        runtime_checkpoint_data = _capture_runtime_checkpoint(page, tid, si)

                        snapshot_page = (
                            runtime_checkpoint_data.get("current_page")
                            or prev_filename
                            or ""
                        )
                        prev_filename = _reconcile_replayed_filename(prev_filename, snapshot_page)
                        bridge_missing = runtime_checkpoint_data.get("__bridge_missing", False)

                        clean_runtime_checkpoint = {
                            "current_page": runtime_checkpoint_data.get("current_page", snapshot_page),
                            "form_values": runtime_checkpoint_data.get("form_values", {}),
                            "selected_elements": runtime_checkpoint_data.get("selected_elements", []),
                            "runtime_state": runtime_checkpoint_data.get("runtime_state", {}),
                            "runtime_store": _runtime_store_snapshot(runtime_checkpoint_data),
                            "runtime_store_present": bool(_runtime_store_snapshot(runtime_checkpoint_data)),
                            "visible_text_contains": _extract_visible_matches(
                                runtime_checkpoint_data.get("visible_text", ""),
                                runtime_checkpoint_assertions,
                            ),
                        }
                        if runtime_checkpoint_data.get("cta_state"):
                            clean_runtime_checkpoint["cta_state"] = runtime_checkpoint_data["cta_state"]

                        if bridge_missing:
                            assertions_checked = []
                            all_passed = False
                        else:
                            assertions_checked = _evaluate_runtime_checkpoint_assertions(
                                runtime_checkpoint_assertions,
                                runtime_checkpoint_data,
                            )
                            all_passed = all(a["passed"] for a in assertions_checked) if assertions_checked else True
                            is_blocking = all_checkpoints.get(cp_id, {}).get("blocking", False)
                            if is_blocking and not assertions_checked:
                                all_passed = False

                        cp_result: dict[str, Any] = {
                            "snapshot_page": snapshot_page,
                            "runtime_checkpoint_data": clean_runtime_checkpoint,
                            "assertions_checked": assertions_checked,
                            "all_passed": all_passed,
                            "source_step_execution_audit": {
                                str(source_step): trace_result.get("step_execution_audit", {}).get(str(source_step), {})
                                for source_step in all_checkpoints.get(cp_id, {}).get("source_steps", [])
                            },
                        }
                        if not bridge_missing and all_checkpoints.get(cp_id, {}).get("blocking", False) and not assertions_checked:
                            cp_result["hard_gate"] = "blocking_checkpoint_has_no_runtime_assertions"
                        if bridge_missing:
                            cp_result["bridge_missing"] = True
                            if "__error" in runtime_checkpoint_data:
                                cp_result["error"] = runtime_checkpoint_data["__error"]

                        trace_result["checkpoints"][cp_id] = cp_result

                        status_icon = "PASS" if all_passed else "FAIL"
                        if bridge_missing:
                            status_icon = "BRIDGE_MISSING"
                        print(f"    {cp_id}: {status_icon} ({len(assertions_checked)} assertions)", flush=True)

            page.close()
            result["traces"][tid] = trace_result

        context.close()
        browser.close()

    return result


def _asset_blocking_route_handler(route) -> None:
    request = route.request
    if request.resource_type in {"image", "font", "media"}:
        route.abort()
    else:
        route.continue_()


def _load_trace_ids(result_dir: Path, trace_id: str | None = None) -> list[str]:
    manifest_path = result_dir / "phase_post_processing" / "evaluators" / "executable_evaluator_manifest.json"
    manifest = _load_json(manifest_path)
    evaluators = manifest.get("evaluators", {})
    if trace_id:
        return [trace_id]
    return list(evaluators.keys())


def _safe_trace_filename(trace_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", trace_id) + ".json"


async def _collect_trace_async(
    browser,
    sem: asyncio.Semaphore,
    tid: str,
    rd: Path,
    pages_dir: Path,
    evaluators: dict,
    capture_actions: dict,
    bindings: dict,
    base_url: str,
    block_assets: bool,
    trace_timeout_ms: int | None,
    shard_output_dir: Path,
    runtime_schema_by_checkpoint: dict[tuple[str, str], dict],
    action_host_filename_map: dict[str, str],
    viewport: dict[str, int],
) -> tuple[str, dict]:
    async with sem:
        print(f"\n[runtime-checkpoint] Processing {tid}...", flush=True)
        trace_start = time.monotonic()
        task_eval = evaluators.get(tid, {})
        trace_result: dict[str, Any] = {"checkpoints": {}, "step_execution_audit": {}}
        if not task_eval:
            trace_result["trace_status"] = "missing_evaluator"
            return tid, trace_result

        steps = capture_actions.get(tid, [])
        if not steps:
            trace_result["trace_status"] = "missing_capture_actions"
            return tid, trace_result

        obligations_cache: dict[str, list[dict]] = {}

        def _get_obligations(filename: str) -> list[dict]:
            if filename not in obligations_cache:
                page_path = pages_dir / filename
                if page_path.exists():
                    obligations_cache[filename] = _parse_obligations(page_path.read_text(encoding="utf-8"))
                else:
                    obligations_cache[filename] = []
            return obligations_cache[filename]

        all_checkpoints = _merge_evaluator_checkpoints(task_eval)

        step_by_index: dict[int, dict] = {}
        for s in steps:
            si = s.get("step_index", s.get("step", 0))
            step_by_index[si] = s

        def _resolve_filename(si: int) -> str:
            return _resolve_step_source_filename(
                action_host_filename_map,
                bindings,
                tid,
                si,
            )

        capture_points: dict[int, list[tuple[str, list[dict]]]] = {}
        for cp_id, cp_data in all_checkpoints.items():
            source_steps = cp_data.get("source_steps", [])
            if not source_steps:
                continue
            last_step = _last_replayable_source_step(source_steps, step_by_index, _resolve_filename)
            if last_step is None:
                continue
            capture_points.setdefault(last_step, []).append(
                (
                    cp_id,
                    _checkpoint_runtime_assertions(
                        cp_data,
                        runtime_schema_by_checkpoint.get((tid, cp_id)),
                    ),
                )
            )

        all_step_indices = sorted(si for si in step_by_index if si >= 0)
        if not all_step_indices:
            trace_result["trace_status"] = "no_steps"
            return tid, trace_result

        max_step = max(
            (max(cp_data.get("source_steps", [0])) for cp_data in all_checkpoints.values() if cp_data.get("source_steps")),
            default=0,
        )
        steps_to_replay = [si for si in all_step_indices if si <= max_step]

        context = await browser.new_context(viewport=viewport)
        if block_assets:
            await context.route("**/*", _asset_blocking_route_handler_async)
        page = await context.new_page()
        prev_filename: str | None = None
        try:
            for si in steps_to_replay:
                if trace_timeout_ms and (time.monotonic() - trace_start) * 1000 > trace_timeout_ms:
                    for cp_step, cp_list in capture_points.items():
                        if cp_step >= si:
                            for cp_id, _assertions in cp_list:
                                trace_result["checkpoints"][cp_id] = {
                                    "snapshot_page": prev_filename or "",
                                    "runtime_checkpoint_data": {},
                                    "assertions_checked": [],
                                    "all_passed": False,
                                    "replay_status": "trace_timeout",
                                }
                    trace_result["trace_status"] = "trace_timeout"
                    break

                step = step_by_index.get(si, {})
                fn = _resolve_filename(si)
                if not fn:
                    continue

                obligation = _find_obligation(_get_obligations(fn), tid, si)
                if not obligation.get("source_filename"):
                    obligation["source_filename"] = fn
                if not step.get("source_filename"):
                    step["source_filename"] = fn

                status, prev_filename, step_audit = await _execute_step_async(
                    page, step, obligation, tid, pages_dir, base_url, prev_filename
                )
                step_audit["status"] = status
                trace_result.setdefault("step_execution_audit", {})[str(si)] = step_audit
                if status in _TERMINAL_STEP_FAILURES:
                    for cp_step, cp_list in capture_points.items():
                        if cp_step >= si:
                            for cp_id, _assertions in cp_list:
                                trace_result["checkpoints"][cp_id] = {
                                    "snapshot_page": fn,
                                    "runtime_checkpoint_data": {},
                                    "assertions_checked": [],
                                    "all_passed": False,
                                    "replay_status": status,
                                }
                    break

                if si in capture_points:
                    for cp_id, runtime_checkpoint_assertions in capture_points[si]:
                        runtime_checkpoint_data = await _capture_runtime_checkpoint_async(page, tid, si)
                        snapshot_page = runtime_checkpoint_data.get("current_page") or prev_filename or ""
                        prev_filename = _reconcile_replayed_filename(prev_filename, snapshot_page)
                        bridge_missing = runtime_checkpoint_data.get("__bridge_missing", False)
                        clean_runtime_checkpoint = {
                            "current_page": runtime_checkpoint_data.get("current_page", snapshot_page),
                            "form_values": runtime_checkpoint_data.get("form_values", {}),
                            "selected_elements": runtime_checkpoint_data.get("selected_elements", []),
                            "runtime_state": runtime_checkpoint_data.get("runtime_state", {}),
                            "runtime_store": _runtime_store_snapshot(runtime_checkpoint_data),
                            "runtime_store_present": bool(_runtime_store_snapshot(runtime_checkpoint_data)),
                            "visible_text_contains": _extract_visible_matches(
                                runtime_checkpoint_data.get("visible_text", ""),
                                runtime_checkpoint_assertions,
                            ),
                        }
                        if runtime_checkpoint_data.get("cta_state"):
                            clean_runtime_checkpoint["cta_state"] = runtime_checkpoint_data["cta_state"]
                        if bridge_missing:
                            assertions_checked = []
                            all_passed = False
                        else:
                            assertions_checked = _evaluate_runtime_checkpoint_assertions(
                                runtime_checkpoint_assertions,
                                runtime_checkpoint_data,
                            )
                            all_passed = all(a["passed"] for a in assertions_checked) if assertions_checked else True
                            is_blocking = all_checkpoints.get(cp_id, {}).get("blocking", False)
                            if is_blocking and not assertions_checked:
                                all_passed = False

                        cp_result: dict[str, Any] = {
                            "snapshot_page": snapshot_page,
                            "runtime_checkpoint_data": clean_runtime_checkpoint,
                            "assertions_checked": assertions_checked,
                            "all_passed": all_passed,
                            "source_step_execution_audit": {
                                str(source_step): trace_result.get("step_execution_audit", {}).get(str(source_step), {})
                                for source_step in all_checkpoints.get(cp_id, {}).get("source_steps", [])
                            },
                        }
                        if not bridge_missing and all_checkpoints.get(cp_id, {}).get("blocking", False) and not assertions_checked:
                            cp_result["hard_gate"] = "blocking_checkpoint_has_no_runtime_assertions"
                        if bridge_missing:
                            cp_result["bridge_missing"] = True
                            if "__error" in runtime_checkpoint_data:
                                cp_result["error"] = runtime_checkpoint_data["__error"]
                        trace_result["checkpoints"][cp_id] = cp_result
        finally:
            await context.close()

        shard_result = {
            "format": "ghostweb_checkpoint_runtime_validation",
            "schema_revision": 1,
            "traces": {tid: trace_result},
        }
        _save_json(shard_output_dir / _safe_trace_filename(tid), shard_result)
        return tid, trace_result


async def _asset_blocking_route_handler_async(route) -> None:
    request = route.request
    if request.resource_type in {"image", "font", "media"}:
        await route.abort()
    else:
        await route.continue_()


async def _collect_runtime_checkpoint_snapshots_parallel_async(
    result_dir: str,
    trace_ids: list[str],
    base_url: str,
    workers: int,
    block_assets: bool,
    trace_timeout_ms: int | None,
    shard_output_dir: Path,
    clear_existing_shards: bool = True,
) -> dict:
    from playwright.async_api import async_playwright

    rd = Path(result_dir)
    environment_profile = load_environment_profile_from_result_dir(rd)
    viewport = dict(environment_profile["viewport"])
    pages_dir = rd / "phase_pagegen" / "pages"
    manifest = _load_json(rd / "phase_post_processing" / "evaluators" / "executable_evaluator_manifest.json")
    capture_actions = _load_json(rd / "phase_global_reasoning" / "debug" / "capture_actions.json")
    bindings_data = _load_json(rd / "phase_consolidation" / "site_model" / "materialized_observation_bindings.json")
    bindings = bindings_data.get("bindings_by_obs_key", bindings_data)
    evaluators = manifest.get("evaluators", {})
    runtime_schema_by_checkpoint = _load_runtime_state_schema_by_checkpoint(rd)
    action_host_filename_map = _load_action_host_filename_map(rd)

    shard_output_dir.mkdir(parents=True, exist_ok=True)
    if clear_existing_shards:
        for old_shard in shard_output_dir.glob("*.json"):
            old_shard.unlink()

    result: dict[str, Any] = {
        "format": "ghostweb_checkpoint_runtime_validation",
        "schema_revision": 1,
        "environment_profile_hash": environment_profile["profile_hash"],
        "platform": environment_profile["platform"],
        "viewport": viewport,
        "traces": {},
        "collection_mode": {
            "workers": workers,
            "block_assets": block_assets,
            "trace_timeout_ms": trace_timeout_ms,
            "shard_output_dir": str(shard_output_dir),
        },
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        sem = asyncio.Semaphore(workers)
        tasks = [
            _collect_trace_async(
                browser,
                sem,
                tid,
                rd,
                pages_dir,
                evaluators,
                capture_actions,
                bindings,
                base_url,
                block_assets,
                trace_timeout_ms,
                shard_output_dir,
                runtime_schema_by_checkpoint,
                action_host_filename_map,
                viewport,
            )
            for tid in trace_ids
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

    for tid, item in zip(trace_ids, results_list):
        if isinstance(item, Exception):
            result["traces"][tid] = {
                "trace_status": "collector_error",
                "error": str(item)[:500],
                "checkpoints": {},
            }
            print(f"[runtime-checkpoint] shard failed: {tid}: {item}", flush=True)
        else:
            completed_tid, trace_result = item
            result["traces"][completed_tid] = trace_result
            print(f"[runtime-checkpoint] shard complete: {completed_tid}", flush=True)

    result["traces"] = {tid: result["traces"][tid] for tid in trace_ids if tid in result["traces"]}
    return result


def _collect_runtime_checkpoint_snapshots_parallel(
    result_dir: str,
    trace_ids: list[str],
    base_url: str,
    workers: int,
    block_assets: bool,
    trace_timeout_ms: int | None,
    shard_output_dir: Path,
    clear_existing_shards: bool = True,
) -> dict:
    return asyncio.run(
        _collect_runtime_checkpoint_snapshots_parallel_async(
            result_dir=result_dir,
            trace_ids=trace_ids,
            base_url=base_url,
            workers=workers,
            block_assets=block_assets,
            trace_timeout_ms=trace_timeout_ms,
            shard_output_dir=shard_output_dir,
            clear_existing_shards=clear_existing_shards,
        )
    )


def _extract_visible_matches(
    visible_text: str,
    assertions: list[dict],
) -> list[str]:
    matches = []
    for assertion in assertions:
        value = assertion.get("value", "")
        if value and str(value) in visible_text:
            matches.append(str(value))
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Runtime Checkpoint Snapshot Oracle (P4)")
    parser.add_argument("--result-dir", required=True, help="Path to result directory")
    parser.add_argument("--trace-id", default=None, help="Specific trace ID to process")
    parser.add_argument(
        "--contract-source-dir",
        default="",
        help="Read capture actions and observation bindings from this result dir while validating --result-dir pages.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8765/pages",
        help="Base URL for page serving (default: http://127.0.0.1:8765/pages)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=32,
        help="Trace-level collector concurrency. Defaults to 32; --trace-id forces single-trace mode.",
    )
    parser.add_argument(
        "--block-assets",
        action="store_true",
        help="Abort image/font/media requests during replay; DOM/script/fetch requests are preserved.",
    )
    parser.add_argument(
        "--trace-timeout-ms",
        type=int,
        default=0,
        help="Soft per-trace replay timeout in milliseconds. 0 disables the timeout.",
    )
    parser.add_argument(
        "--shard-output-dir",
        type=Path,
        default=None,
        help="Directory for per-trace shard JSON files when --workers > 1.",
    )
    parser.add_argument(
        "--replace-output",
        action="store_true",
        help=(
            "When --trace-id is set, replace checkpoint_runtime_validation.json with only "
            "that trace instead of merging into the existing full report."
        ),
    )
    args = parser.parse_args()

    print(f"[runtime-checkpoint] result_dir: {args.result_dir}")
    print(f"[runtime-checkpoint] trace_id: {args.trace_id or 'all'}")
    print(f"[runtime-checkpoint] base_url: {args.base_url}")
    print(f"[runtime-checkpoint] block_assets: {args.block_assets}")
    trace_timeout_ms = args.trace_timeout_ms if args.trace_timeout_ms > 0 else None
    if trace_timeout_ms:
        print(f"[runtime-checkpoint] trace_timeout_ms: {trace_timeout_ms}")

    rd = Path(args.result_dir)
    workers = 1 if args.trace_id else max(1, args.workers)
    print(f"[runtime-checkpoint] workers: {workers}")
    shard_output_dir = (
        args.shard_output_dir
        or rd / "phase_post_processing" / "evaluators" / "checkpoint_runtime_validation.shards"
    )

    if workers > 1:
        trace_ids = _load_trace_ids(rd, args.trace_id)
        result = _collect_runtime_checkpoint_snapshots_parallel(
            result_dir=args.result_dir,
            trace_ids=trace_ids,
            base_url=args.base_url,
            workers=workers,
            block_assets=args.block_assets,
            trace_timeout_ms=trace_timeout_ms,
            shard_output_dir=shard_output_dir,
            clear_existing_shards=not bool(args.trace_id),
        )
    else:
        result = collect_runtime_checkpoint_snapshots(
            result_dir=args.result_dir,
            trace_id=args.trace_id,
            base_url=args.base_url,
            block_assets=args.block_assets,
            trace_timeout_ms=trace_timeout_ms,
            contract_source_dir=args.contract_source_dir or None,
        )
        if args.trace_id:
            _write_runtime_checkpoint_shards(result, shard_output_dir)


    output_path = _runtime_validation_output_path(rd)
    if args.trace_id and not args.replace_output:
        existing = _load_json(output_path) if output_path.exists() else None
        result = _merge_runtime_checkpoint_validation(existing, result, [args.trace_id])
    _save_json(output_path, result)


    total_cps = 0
    total_passed = 0
    total_failed = 0
    total_bridge_missing = 0
    for tid, trace_data in result["traces"].items():
        for cp_id, cp_data in trace_data["checkpoints"].items():
            total_cps += 1
            if cp_data.get("bridge_missing"):
                total_bridge_missing += 1
            elif cp_data.get("all_passed"):
                total_passed += 1
            else:
                total_failed += 1

    print(f"\n[runtime-checkpoint] Summary: {total_cps} checkpoints, "
          f"{total_passed} passed, {total_failed} failed, "
          f"{total_bridge_missing} bridge_missing")


if __name__ == "__main__":
    main()
