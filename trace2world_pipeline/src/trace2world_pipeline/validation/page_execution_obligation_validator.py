
from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

from trace2world_pipeline.validation.trace_replay import (
    _drop_replay_execution_context,
    _browser_command_succeeded,
    _deterministic_step_command,
    _expected_post_state_error,
    _filename_from_page_url,
    _register_replay_execution_context,
    exec_playwright,
)
from trace2world_pipeline.app_spec.environment_profile import load_environment_profile_from_result_dir


def _resolve_generated_page_path(root: Path, filename: str) -> Path:
    filename = str(filename or "").strip()
    phase_dir = root / "phase_pagegen" / "pages"
    phase_pagegen = phase_dir / filename
    if phase_dir.is_dir():
        return phase_pagegen
    if phase_pagegen.is_file():
        return phase_pagegen
    direct = root / filename
    if direct.is_file():
        return direct
    legacy_frontend = root / "frontend" / "pages" / filename
    if legacy_frontend.is_file():
        return legacy_frontend
    return phase_pagegen


def _browser_coordinates_for_action(
    action: Dict[str, Any],
    environment_profile: Dict[str, Any],
) -> List[int]:
    browser_coordinates = action.get("browser_coordinates")
    if isinstance(browser_coordinates, list) and len(browser_coordinates) >= 2:
        return [int(value) for value in browser_coordinates]
    viewport_coordinates = action.get("viewport_coordinates")
    if not isinstance(viewport_coordinates, list) or len(viewport_coordinates) < 2:
        return []
    app_surface = environment_profile.get("app_surface", {})
    offset_x = int(app_surface.get("x", 0) or 0)
    offset_y = int(app_surface.get("y", 0) or 0)
    return [
        int(value) + (offset_x if index % 2 == 0 else offset_y)
        for index, value in enumerate(viewport_coordinates)
    ]


def _deterministic_obligation_command(
    step: Dict[str, Any],
    environment_profile: Dict[str, Any],
) -> str:
    execution_step = deepcopy(step)
    action = execution_step.get("action", {})
    if isinstance(action, dict):
        browser_coordinates = _browser_coordinates_for_action(
            action,
            environment_profile,
        )
        if browser_coordinates:
            action["viewport_coordinates"] = browser_coordinates
    return _deterministic_step_command(execution_step)


async def _page_uses_materialized_app_surface(
    session_id: str,
    website_dir: str,
    env: Dict[str, str],
    environment_profile: Dict[str, Any],
) -> bool:
    app_surface = environment_profile.get("app_surface", {})
    expected_x = int(app_surface.get("x", 0) or 0)
    expected_y = int(app_surface.get("y", 0) or 0)
    expected_width = int(app_surface.get("width", 0) or 0)
    expected_height = int(app_surface.get("height", 0) or 0)
    measurement = await exec_playwright(
        session_id,
        "run-code "
        + json.dumps(
            "() => {"
            " const r = document.body.getBoundingClientRect();"
            " const shell = document.querySelector('.app-shell');"
            " const sr = shell ? shell.getBoundingClientRect() : null;"
            " return {"
            "   body: {x: Math.round(r.x), y: Math.round(r.y),"
            "     width: Math.round(r.width), height: Math.round(r.height)},"
            "   shell: sr ? {x: Math.round(sr.x), y: Math.round(sr.y),"
            "     width: Math.round(sr.width), height: Math.round(sr.height)} : null"
            " };"
            "}"
        ),
        website_dir,
        env,
        timeout=10,
    )
    try:
        measured = json.loads(measurement)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    rect = measured.get("body", {}) if isinstance(measured, dict) else {}
    shell_rect = measured.get("shell") if isinstance(measured, dict) else None
    return (
        isinstance(shell_rect, dict)
        and int(shell_rect.get("width", 0) or 0) > 0
        and int(shell_rect.get("height", 0) or 0) > 0
        and abs(int(rect.get("x", 0)) - expected_x) <= 1
        and abs(int(rect.get("y", 0)) - expected_y) <= 1
        and abs(int(rect.get("width", 0)) - expected_width) <= 1
        and abs(int(rect.get("height", 0)) - expected_height) <= 1
    )


async def validate_page_execution_obligations(
    website_dir: str,
    l0_execution_pack: Dict[str, Any],
    output_path: str | None = None,
    action_pack_path: str | None = None,
    max_failures: int = 32,
) -> Dict[str, Any]:
    if not isinstance(l0_execution_pack, dict) or l0_execution_pack.get("format") != "l0_execution_pack":
        raise ValueError("validate_page_execution_obligations requires canonical l0_execution_pack")
    pack_payload = l0_execution_pack
    packs = [
        pack for pack in pack_payload.get("action_packs", [])
        if isinstance(pack, dict)
    ]
    obligations_by_page = pack_payload.get("obligations_by_page", {})
    obligations = [
        item
        for items in obligations_by_page.values()
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict)
    ] if isinstance(obligations_by_page, dict) else []
    if action_pack_path:
        Path(action_pack_path).parent.mkdir(parents=True, exist_ok=True)
        with open(action_pack_path, "w", encoding="utf-8") as f:
            for pack in packs:
                f.write(json.dumps(pack, ensure_ascii=False) + "\n")

    root = Path(website_dir)
    environment_profile = load_environment_profile_from_result_dir(root)
    materialized_profile_path = (
        root / "phase_consolidation" / "site_model" / "environment_profile.json"
    )
    use_browser_coordinates = materialized_profile_path.is_file()
    validation_viewport = dict(environment_profile["viewport"])
    browser_viewport = dict(environment_profile["browser_viewport"])
    execution_viewport = browser_viewport if use_browser_coordinates else validation_viewport
    viewport_width = int(execution_viewport["width"])
    viewport_height = int(execution_viewport["height"])
    active_viewport = dict(execution_viewport)
    observed_coordinate_modes: set[str] = set()
    results: List[Dict[str, Any]] = []
    failures = 0
    env = os.environ.copy()
    env["PLAYWRIGHT_MCP_BROWSER"] = "chrome"
    env["PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS"] = "true"

    obligations_by_step = {
        (str(obligation.get("trace_id") or ""), int(obligation.get("step_index") or 0)): obligation
        for obligation in obligations
    }

    for pack_index, pack in enumerate(packs):
        if max_failures > 0 and failures >= max_failures:
            break
        trace_id = str(pack.get("trace_id") or "")
        start_filename = str(pack.get("start_state", {}).get("filename") or "").strip()
        source_path = _resolve_generated_page_path(root, start_filename)
        start_reason = str(pack.get("start_state", {}).get("reason") or "")
        session_id = f"obligation_trace_{pack_index}_{int(time.time() * 1000) % 100000}"
        _register_replay_execution_context(
            session_id,
            {
                "mode": "reference_trace_replay",
                "task_id": trace_id,
                "reference_trace_id": trace_id,
                "start_filename": start_filename,
                "start_reason": start_reason,
            },
        )
        try:
            if not source_path.is_file():
                failures += 1
                results.append({
                    "trace_id": trace_id,
                    "step_index": 0,
                    "source_filename": start_filename,
                    "operation": "open",
                    "command": "",
                    "intent": "open trace start page",
                    "expected_filename": start_filename,
                    "input_text": "",
                    "hard": True,
                    "gesture": {},
                    "validation_viewport": validation_viewport,
                    "status": "failed",
                    "failure_category": "missing_source_filename",
                    "actual_filename": "",
                    "actual_url": "",
                    "command_output": f"Missing source filename {start_filename}",
                    "pre_hit_target": "",
                })
                continue

            await exec_playwright(session_id, f'open "{source_path.resolve().as_uri()}"', website_dir, env, timeout=30)
            await exec_playwright(
                session_id,
                f'run-code "async page => {{ await page.setViewportSize({{width: {viewport_width}, height: {viewport_height}}}); }}"',
                website_dir,
                env,
                timeout=10,
            )
            for step in pack.get("steps", []) if isinstance(pack.get("steps", []), list) else []:
                if max_failures > 0 and failures >= max_failures:
                    break
                if not isinstance(step, dict):
                    continue
                use_browser_coordinates_for_step = (
                    use_browser_coordinates
                    and await _page_uses_materialized_app_surface(
                        session_id,
                        website_dir,
                        env,
                        environment_profile,
                    )
                )
                desired_viewport = (
                    browser_viewport
                    if use_browser_coordinates_for_step
                    else validation_viewport
                )
                if (
                    int(active_viewport["width"]) != int(desired_viewport["width"])
                    or int(active_viewport["height"]) != int(desired_viewport["height"])
                ):
                    await exec_playwright(
                        session_id,
                        'run-code "async page => { await page.setViewportSize('
                        f'{{width: {int(desired_viewport["width"])}, '
                        f'height: {int(desired_viewport["height"])}}}'
                        '); }"',
                        website_dir,
                        env,
                        timeout=10,
                    )
                    active_viewport = dict(desired_viewport)
                observed_coordinate_modes.add(
                    "browser_app_surface"
                    if use_browser_coordinates_for_step
                    else "interaction_viewport"
                )
                command = (
                    _deterministic_obligation_command(step, environment_profile)
                    if use_browser_coordinates_for_step
                    else _deterministic_step_command(step)
                )
                if not command or step.get("execution_policy") == "normalize_initial_app_open":
                    if step.get("execution_policy") != "normalize_initial_app_open":
                        post_state = step.get("expected_post_state", {}) if isinstance(step.get("expected_post_state", {}), dict) else {}
                        expected_filename = str(post_state.get("filename") or "").strip()
                        expected_path = _resolve_generated_page_path(root, expected_filename)
                        if expected_filename and expected_path.is_file():
                            await exec_playwright(
                                session_id,
                                f'open "{expected_path.resolve().as_uri()}"',
                                website_dir,
                                env,
                                timeout=30,
                            )
                    continue
                obligation = obligations_by_step.get((trace_id, int(step.get("step_index") or 0)))
                if not obligation:
                    continue
                result = {
                    **dict(obligation),
                    "status": "pending",
                    "failure_category": "",
                    "actual_filename": "",
                    "actual_url": "",
                    "command_output": "",
                    "pre_hit_target": "",
                }
                action = step.get("action", {}) if isinstance(step.get("action", {}), dict) else {}
                browser_coordinates = (
                    _browser_coordinates_for_action(action, environment_profile)
                    if use_browser_coordinates_for_step
                    else list(action.get("viewport_coordinates") or [])
                )
                if (
                    str(action.get("operation") or "") == "click"
                    and len(browser_coordinates) >= 2
                ):
                    x = browser_coordinates[0]
                    y = browser_coordinates[1]
                    hit_script = (
                        "() => {"
                        f" const e = document.elementFromPoint({x}, {y});"
                        " if (!e) return null;"
                        " const r = e.getBoundingClientRect();"
                        " const host = e.closest && e.closest('button,a,input,textarea,select,[role=\"button\"],[onclick],[data-route],[data-target]');"
                        " const hr = host ? host.getBoundingClientRect() : null;"
                        " return {"
                        "   element_tag: e.tagName, element_id: e.id || '', element_text: (e.innerText || e.alt || e.getAttribute('aria-label') || '').slice(0, 120),"
                        "   element_rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},"
                        "   actionable_tag: host ? host.tagName : '', actionable_id: host ? (host.id || '') : '',"
                        "   actionable_text: host ? (host.innerText || host.getAttribute('aria-label') || '').slice(0, 120) : '',"
                        "   actionable_rect: hr ? {x: Math.round(hr.x), y: Math.round(hr.y), w: Math.round(hr.width), h: Math.round(hr.height)} : null"
                        " };"
                        "}"
                    )
                    result["pre_hit_target"] = await exec_playwright(
                        session_id,
                        "run-code " + json.dumps(hit_script),
                        website_dir,
                        env,
                        timeout=10,
                    )
                command_output = await exec_playwright(session_id, command, website_dir, env, timeout=20)
                page_url = await exec_playwright(session_id, 'run-code "async page => page.url()"', website_dir, env, timeout=10)
                result["command_output"] = command_output
                result["actual_url"] = page_url
                result["actual_filename"] = _filename_from_page_url(page_url)

                step_contract = {"expected_post_state": {"filename": obligation.get("expected_filename", "")}}
                expected_error = _expected_post_state_error(step_contract, page_url)
                if not _browser_command_succeeded(command_output):
                    result.update({
                        "status": "failed",
                        "failure_category": "browser_command_failed",
                    })
                    failures += 1
                elif expected_error:
                    result.update({
                        "status": "failed",
                        "failure_category": "wrong_route_or_nonactionable_target",
                        "command_output": f"{command_output}; {expected_error}",
                    })
                    failures += 1
                else:
                    result["status"] = "passed"
                results.append(result)
                if result["status"] == "failed":
                    break
        finally:
            await exec_playwright(session_id, "close", website_dir, env, timeout=10)
            _drop_replay_execution_context(session_id)

    report = {
        "format": "page_execution_obligation_validation",
        "schema_revision": 1,
        "environment_profile_hash": environment_profile["profile_hash"],
        "platform": environment_profile["platform"],
        "validation_viewport": validation_viewport,
        "browser_viewport": browser_viewport,
        "execution_viewport": execution_viewport,
        "coordinate_mode": (
            "adaptive_materialized_app_surface_coordinates"
            if use_browser_coordinates
            else "legacy_viewport_coordinates"
        ),
        "observed_coordinate_modes": sorted(observed_coordinate_modes),
        "ok": failures == 0,
        "total_obligations": len(obligations),
        "validated_obligations": len(results),
        "failed_obligations": failures,
        "max_failures": max_failures,
        "obligations": results,
    }
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return report
