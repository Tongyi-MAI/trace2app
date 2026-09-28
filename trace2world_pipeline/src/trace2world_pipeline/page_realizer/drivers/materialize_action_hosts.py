

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


from trace2world_pipeline.page_realizer.action_effect_materializer import materialize_action_effect_dom
from trace2world_pipeline.page_realizer.route_materializer import materialize_visible_return_affordances
from trace2world_pipeline.verification.action_authority import (
    action_host_exclusion_reason,
    build_action_execution_contracts_from_formal_package_inputs,
    build_action_host_manifest_for_page,
    validate_action_host_coverage,
)
from trace2world_pipeline.app_spec.page_execution_contracts import (
    _inject_route_execution_runtime,
    inject_page_execution_runtimes,
    page_execution_contract_for_filename,
)
from trace2world_pipeline.compiler.delivery_contract import (
    build_formal_manifest,
    copy_formal_app,
    write_json as write_formal_json,
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pages_dir(result_dir: Path) -> Path:
    path = result_dir / "phase_pagegen" / "pages"
    if not path.is_dir():
        raise FileNotFoundError(f"generated pages directory not found: {path}")
    return path


def _load_page_execution_contracts(result_dir: Path) -> dict[str, Any]:
    for rel in (
        "phase_consolidation/site_model/page_execution_contracts.json",
        "data/site_model/page_execution_contracts.json",
    ):
        path = result_dir / rel
        if path.is_file():
            return _load_json(path)
    return {}


def _load_route_manifest(result_dir: Path) -> dict[str, Any]:
    path = result_dir / "phase_sdk_generation" / "runtime" / "route_manifest.js"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    marker = "window.GhostwebRouteManifest"
    start = text.rfind(marker)
    if start < 0:
        raise ValueError(f"route manifest is not a JSON assignment: {path}")
    assignment = text[start + len(marker) :].lstrip()
    if not assignment.startswith("="):
        raise ValueError(f"route manifest is not a JSON assignment: {path}")
    payload_text = assignment[1:].strip()
    if not payload_text.endswith(";"):
        raise ValueError(f"route manifest is not a JSON assignment: {path}")
    payload = json.loads(payload_text[:-1].strip())
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), dict):
        raise ValueError(f"route manifest lacks pages object: {path}")
    return payload


def _preserved_action_host_expectations(
    *,
    manifest_path: Path,
    pages_dir: Path,
) -> dict[str, set[str]]:
    manifest = _load_json(manifest_path)
    if manifest.get("format") != "ghostweb_action_host_manifest":
        raise ValueError(f"invalid preserved action-host manifest: {manifest_path}")
    hosts = manifest.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError(f"preserved action-host manifest has no hosts: {manifest_path}")

    expected: dict[str, set[str]] = {}
    malformed: list[dict[str, Any]] = []
    for index, host in enumerate(hosts):
        if not isinstance(host, dict):
            malformed.append({"index": index, "reason": "host_not_object"})
            continue
        page = str(host.get("filename") or host.get("page") or "").strip()
        action_id = str(host.get("action_id") or "").strip()
        if not page or not action_id:
            malformed.append(
                {
                    "index": index,
                    "reason": "host_missing_page_or_action_id",
                    "page": page,
                    "action_id": action_id,
                }
            )
            continue
        if not (pages_dir / page).is_file():
            malformed.append(
                {"index": index, "reason": "host_page_not_materialized", "page": page}
            )
            continue
        expected.setdefault(page, set()).add(action_id)
    if malformed:
        raise ValueError(
            "preserved action-host manifest is not a complete physical contract: "
            + json.dumps(malformed, ensure_ascii=False)
        )
    return expected


def _refresh_page_execution_runtimes(
    *,
    pages_dir: Path,
    page_execution_contracts: dict[str, Any],
) -> dict[str, Any]:
    changed_pages: list[str] = []
    if not isinstance(page_execution_contracts, dict) or not page_execution_contracts:
        return {
            "status": "skipped",
            "reason": "page_execution_contracts_missing",
            "changed_page_count": 0,
            "changed_pages": [],
        }
    for page_path in sorted(pages_dir.glob("*.html")):
        if page_path.name == "ghostweb-task-runner.html":
            continue
        contract = page_execution_contract_for_filename(page_execution_contracts, page_path.name)
        original = page_path.read_text(encoding="utf-8")
        updated, changed = inject_page_execution_runtimes(original, contract or {"filename": page_path.name})
        if changed and updated != original:
            page_path.write_text(updated, encoding="utf-8")
            changed_pages.append(page_path.name)
    return {
        "status": "refreshed",
        "changed_page_count": len(changed_pages),
        "changed_pages": changed_pages,
    }


def _refresh_route_execution_runtime_only(
    *,
    pages_dir: Path,
    page_execution_contracts: dict[str, Any],
    included_page_filenames: list[str] | None = None,
) -> dict[str, Any]:

    changed_pages: list[str] = []
    included_pages = set(included_page_filenames or [])
    for page_path in sorted(pages_dir.glob("*.html")):
        if page_path.name == "ghostweb-task-runner.html":
            continue
        if included_pages and page_path.name not in included_pages:
            continue
        contract = page_execution_contract_for_filename(page_execution_contracts, page_path.name)
        original = page_path.read_text(encoding="utf-8")
        updated, changed = _inject_route_execution_runtime(
            original,
            contract or {"filename": page_path.name},
        )
        if changed and updated != original:
            page_path.write_text(updated, encoding="utf-8")
            changed_pages.append(page_path.name)
    return {
        "status": "route_runtime_only_refreshed",
        "changed_page_count": len(changed_pages),
        "changed_pages": changed_pages,
    }


def _gesture_route_actions_by_page(action_effect_contracts: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    actions_by_page: dict[str, list[dict[str, str]]] = {}
    for action in action_effect_contracts.get("actions", []) if isinstance(action_effect_contracts, dict) else []:
        if not isinstance(action, dict):
            continue
        if str(action.get("effect_kind") or "").strip() != "gesture_route":
            continue
        action_id = str(action.get("action_id") or "").strip()
        source = str(action.get("source_filename") or "").strip()
        target = str(action.get("expected_target_filename") or action.get("target_filename") or "").strip()
        if not action_id or not source or not target:
            continue
        actions_by_page.setdefault(source, []).append({
            "action_id": action_id,
            "target_filename": target,
            "transaction_id": str(action.get("transaction_id") or "").strip(),
        })
    return actions_by_page


_GESTURE_ROUTE_RUNTIME_RE = re.compile(
    r"\s*<script\s+data-ghostweb-runtime=[\"']gesture-route-materializer[\"'][^>]*>.*?</script>",
    re.DOTALL,
)


def _strip_gesture_route_runtime(html: str) -> str:
    return _GESTURE_ROUTE_RUNTIME_RE.sub("", html)


def _gesture_route_runtime_script(actions: list[dict[str, str]]) -> str:
    payload = json.dumps(actions, ensure_ascii=False, sort_keys=True)
    return f"""
<script data-ghostweb-runtime="gesture-route-materializer">
(function() {{
  const ACTIONS = {payload};
  if (!Array.isArray(ACTIONS) || !ACTIONS.length) return;
  const fired = new Set();
  function writeRuntime(action) {{
    const target = action && action.target_filename;
    if (!target || !window.GhostwebRuntimeState || !window.GhostwebRuntimeState.write) return;
    const meta = {{
      action_id: action.action_id,
      transaction_id: action.transaction_id || '',
      effect_kind: 'gesture_route',
      source: 'gesture_route_materializer'
    }};
    try {{ window.GhostwebRuntimeState.write('target_filename', target, meta); }} catch (error) {{}}
    try {{ window.GhostwebRuntimeState.write('runtime.page', target, meta); }} catch (error) {{}}
  }}
  function emit(action, eventType) {{
    if (!action || !action.action_id) return;
    if (window.GhostwebCheckpointActionMap && window.GhostwebCheckpointActionMap.emitForAction) {{
      try {{ window.GhostwebCheckpointActionMap.emitForAction(action.action_id, eventType || 'gesture'); }} catch (error) {{}}
    }}
  }}
  function navigate(action) {{
    const target = action && action.target_filename;
    if (!target) return;
    if (window.GhostwebRouteExecution && window.GhostwebRouteExecution.navigate) {{
      window.GhostwebRouteExecution.navigate(target, {{ mode: 'push' }});
      return;
    }}
    window.location.href = target;
  }}
  function fire(action, eventType) {{
    if (!action || !action.action_id || fired.has(action.action_id)) return;
    fired.add(action.action_id);
    emit(action, eventType);
    writeRuntime(action);
    window.setTimeout(function() {{ navigate(action); }}, 0);
  }}
  window.GhostwebGestureRouteRuntime = Object.assign(window.GhostwebGestureRouteRuntime || {{}}, {{
    actions: ACTIONS.slice(),
    fireAction: function(actionId, eventType) {{
      const action = ACTIONS.find(function(row) {{ return row.action_id === actionId; }});
      fire(action || ACTIONS[0], eventType || 'programmatic');
    }}
  }});
  let wheelScore = 0;
  window.addEventListener('wheel', function(event) {{
    wheelScore += Math.abs(Number(event.deltaX || 0)) + Math.abs(Number(event.deltaY || 0));
    if (wheelScore >= 40) fire(ACTIONS[0], 'wheel');
  }}, {{ passive: true }});
  let touchStart = null;
  window.addEventListener('touchstart', function(event) {{
    const point = event.changedTouches && event.changedTouches[0];
    touchStart = point ? {{ x: point.clientX, y: point.clientY }} : null;
  }}, {{ passive: true }});
  window.addEventListener('touchend', function(event) {{
    const point = event.changedTouches && event.changedTouches[0];
    if (!touchStart || !point) return;
    const dx = Math.abs(point.clientX - touchStart.x);
    const dy = Math.abs(point.clientY - touchStart.y);
    if (dx + dy >= 40) fire(ACTIONS[0], 'touch');
  }}, {{ passive: true }});
}})();
</script>
""".strip()


def _inject_gesture_route_runtimes(
    *,
    pages_dir: Path,
    action_effect_contracts: dict[str, Any],
) -> dict[str, Any]:
    actions_by_page = _gesture_route_actions_by_page(action_effect_contracts)
    changed_pages: list[str] = []
    skipped_missing_pages: list[str] = []
    for page_name, actions in sorted(actions_by_page.items()):
        page_path = pages_dir / page_name
        if not page_path.exists():
            skipped_missing_pages.append(page_name)
            continue
        original = page_path.read_text(encoding="utf-8")
        stripped = _strip_gesture_route_runtime(original)
        script = _gesture_route_runtime_script(actions)
        if "</body>" in stripped:
            updated = stripped.replace("</body>", f"{script}\n</body>", 1)
        else:
            updated = f"{stripped}\n{script}\n"
        if updated != original:
            page_path.write_text(updated, encoding="utf-8")
            changed_pages.append(page_name)
    return {
        "status": "refreshed",
        "route_count": sum(len(actions) for actions in actions_by_page.values()),
        "changed_page_count": len(changed_pages),
        "changed_pages": changed_pages,
        "skipped_missing_pages": skipped_missing_pages,
    }


def _reseal_exported_formal_mirror(*, source_root: Path, export_app_dir: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    export_app_dir = export_app_dir.resolve()
    if export_app_dir == source_root:
        raise ValueError("export app directory must differ from materialized source root")
    if export_app_dir.name != "app" or not export_app_dir.parent.is_dir():
        raise ValueError(f"export app directory must be an existing <package>/app path: {export_app_dir}")
    copy_formal_app(source_root, export_app_dir, require_evaluators=True)
    manifest = build_formal_manifest(
        source_root=source_root,
        app_root=export_app_dir,
        package_name=export_app_dir.parent.name,
        target="slime_webenv_preserved_package_route_materialization",
        require_action_authority=True,
        require_evaluators=True,
    )
    if manifest.get("status") != "pass":
        raise RuntimeError(
            "formal export mirror validation failed: "
            + json.dumps(manifest.get("issues") or [], ensure_ascii=False)
        )
    write_formal_json(export_app_dir / "formal_artifact_manifest.json", manifest)
    write_formal_json(export_app_dir.parent / "data" / "formal_artifact_manifest.json", manifest)
    return {
        "status": "pass",
        "source_root": str(source_root),
        "export_app_dir": str(export_app_dir),
        "formal_manifest_path": str(export_app_dir.parent / "data" / "formal_artifact_manifest.json"),
        "required_artifact_count": len(manifest.get("required_artifacts") or []),
    }


def _rebuild_sealed_action_execution_contracts(result_dir: Path) -> dict[str, Any]:
    matrix_path = result_dir / "phase_post_processing" / "instrumentation" / "checkpoint_matrix.json"
    bindings_path = result_dir / "phase_post_processing" / "instrumentation" / "runtime_event_bindings.json"
    hosts_path = result_dir / "phase_pagegen" / "action_host_manifest.json"
    if not matrix_path.is_file() and not bindings_path.is_file():
        return {"status": "skipped", "reason": "instrumentation_not_present"}
    missing = [str(path) for path in (matrix_path, bindings_path, hosts_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "sealed package action authority requires retained formal inputs: " + ", ".join(missing)
        )
    contracts = build_action_execution_contracts_from_formal_package_inputs(
        _load_json(matrix_path),
        _load_json(hosts_path),
        _load_json(bindings_path),
    )
    if contracts.get("missing_source_actions"):
        raise RuntimeError(
            "sealed package action authority has checkpoint actions without a retained formal identity: "
            + json.dumps(contracts["missing_source_actions"], ensure_ascii=False)
        )
    output_path = result_dir / "phase_post_processing" / "action_authority" / "action_execution_contracts.json"
    _write_json(output_path, contracts)
    return {
        "status": "rebuilt",
        "path": str(output_path),
        "action_count": contracts["action_count"],
        "checkpoint_count": contracts["checkpoint_count"],
        "summary": contracts["summary"],
    }


def run(
    result_dir: Path,
    *,
    contract_source_dir: Path | None = None,
    preserve_generated_env: bool = False,
    preserved_action_host_manifest: Path | None = None,
    export_app_dir: Path | None = None,
    included_page_filenames: list[str] | None = None,
) -> dict[str, Any]:
    contract_root = (contract_source_dir or result_dir).resolve()
    compiled_dir = contract_root / "phase_page_environment" / "semantic_api" / "08_spec_compiled_execution_views"
    dom_binding_path = compiled_dir / "dom_binding_contracts.json"
    action_effect_path = compiled_dir / "action_effect_contracts.json"
    package_only_contract_mode = False
    preserved_manifest_path: Path | None = None
    preserved_host_rows: list[dict[str, Any]] = []
    if dom_binding_path.is_file() and action_effect_path.is_file():
        dom_binding_contracts = _load_json(dom_binding_path)
        action_effect_contracts = _load_json(action_effect_path)
        preserved_expected_actions: dict[str, set[str]] = {}
    elif preserve_generated_env:


        package_only_contract_mode = True
        dom_binding_contracts = {"bindings": []}
        action_effect_contracts = {"actions": [], "actions_by_id": {}}
        preserved_manifest_path = (
            preserved_action_host_manifest.resolve()
            if preserved_action_host_manifest is not None
            else result_dir / "phase_pagegen" / "action_host_manifest.json"
        )
        preserved_expected_actions = _preserved_action_host_expectations(
            manifest_path=preserved_manifest_path,
            pages_dir=_pages_dir(result_dir),
        )
        preserved = _load_json(preserved_manifest_path)
        preserved_host_rows = [dict(row) for row in preserved["hosts"]]
    else:
        missing = [str(path) for path in (dom_binding_path, action_effect_path) if not path.is_file()]
        raise FileNotFoundError(
            "compiled action-host inputs are required unless preserving a sealed package contract: "
            + ", ".join(missing)
        )
    excluded_action_ids = [
        str(item.get("action_id") or "").strip()
        for item in action_effect_contracts.get("excluded_actions", [])
        if isinstance(item, dict) and str(item.get("action_id") or "").strip()
    ]
    pages_dir = _pages_dir(result_dir)
    page_execution_contracts = _load_page_execution_contracts(contract_root)
    route_manifest = _load_route_manifest(contract_root)
    if preserve_generated_env:
        materialization_report = materialize_action_effect_dom(
            dom_binding_contracts=dom_binding_contracts,
            generated_pages_dir=pages_dir,
            write_modified=True,
            excluded_action_ids=excluded_action_ids,
            included_page_filenames=included_page_filenames,
        )
        page_runtime_refresh_report = _refresh_route_execution_runtime_only(
            pages_dir=pages_dir,
            page_execution_contracts=page_execution_contracts,
            included_page_filenames=included_page_filenames,
        )
        gesture_route_runtime_report = {
            "status": "skipped_package_preserve_generated_env",
            "route_count": 0,
            "changed_page_count": 0,
            "changed_pages": [],
            "skipped_missing_pages": [],
        }
    else:
        materialization_report = materialize_action_effect_dom(
            dom_binding_contracts=dom_binding_contracts,
            generated_pages_dir=pages_dir,
            write_modified=True,
            excluded_action_ids=excluded_action_ids,
            included_page_filenames=included_page_filenames,
        )
        page_runtime_refresh_report = _refresh_page_execution_runtimes(
            pages_dir=pages_dir,
            page_execution_contracts=page_execution_contracts,
        )
        gesture_route_runtime_report = _inject_gesture_route_runtimes(
            pages_dir=pages_dir,
            action_effect_contracts=action_effect_contracts,
        )
    visible_return_affordance_report = (
        materialize_visible_return_affordances(
            route_manifest=route_manifest,
            generated_pages_dir=pages_dir,
            write_modified=True,
            included_page_filenames=included_page_filenames,
        )
        if route_manifest
        else {
            "status": "skipped",
            "reason": "route_manifest_missing",
            "summary": {
                "declared_fallback_count": 0,
                "materialized_count": 0,
                "existing_declared_return_host_count": 0,
                "missing_page_count": 0,
            },
            "bindings": [],
        }
    )
    _write_json(
        result_dir / "phase_pagegen" / "validation" / "action_effect_dom_materialization.json",
        materialization_report,
    )
    _write_json(
        result_dir / "phase_pagegen" / "validation" / "page_execution_runtime_refresh.json",
        page_runtime_refresh_report,
    )
    _write_json(
        result_dir / "phase_pagegen" / "validation" / "gesture_route_runtime_refresh.json",
        gesture_route_runtime_report,
    )
    _write_json(
        result_dir / "phase_pagegen" / "validation" / "visible_return_affordance_materialization.json",
        visible_return_affordance_report,
    )

    expected_actions_by_page: dict[str, set[str]] = {
        page: set(action_ids) for page, action_ids in preserved_expected_actions.items()
    }
    excluded_non_dom_actions: list[dict[str, str]] = []
    actions_by_id = action_effect_contracts.get("actions_by_id")
    if not isinstance(actions_by_id, dict):
        actions_by_id = {
            str(action.get("action_id") or ""): action
            for action in action_effect_contracts.get("actions", [])
            if isinstance(action, dict) and action.get("action_id")
        }
    for binding in dom_binding_contracts.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        action_id = str(binding.get("action_id") or "").strip()
        action_contract = actions_by_id.get(action_id) if action_id else None
        page = str(
            (action_contract or {}).get("source_filename")
            or binding.get("source_filename")
            or ""
        ).strip()
        exclusion_reason = action_host_exclusion_reason(binding)
        if exclusion_reason:
            if page and action_id:
                excluded_non_dom_actions.append(
                    {"page": page, "action_id": action_id, "reason": exclusion_reason}
                )
            continue
        if page and action_id:
            expected_actions_by_page.setdefault(page, set()).add(action_id)


    incremental_expected_actions_by_page: dict[str, set[str]] = {}
    rematerialized_fallback_pages: set[str] = set()
    for binding in visible_return_affordance_report.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if binding.get("status") not in {"materialized", "existing_declared_return_host"}:
            continue
        page = str(binding.get("source_filename") or "").strip()
        if page:
            if package_only_contract_mode:
                if binding.get("status") == "materialized":
                    incremental_expected_actions_by_page.setdefault(page, set()).add("fallback_back")
                    rematerialized_fallback_pages.add(page)
            else:
                expected_actions_by_page.setdefault(page, set()).add("fallback_back")

    action_host_manifest: dict[str, Any] = {
        "format": "ghostweb_action_host_manifest",
        "schema_revision": 2,
        "source": "scripts.analysis.materialize_action_hosts",
        "contract_source_mode": (
            "preserved_action_host_manifest_incremental_route_return"
            if package_only_contract_mode
            else "compiled_dom_and_action_effect_contracts_and_route_manifest"
        ),
        "result_dir": str(result_dir),
        "hosts": [
            row
            for row in preserved_host_rows
            if not (
                str(row.get("filename") or row.get("page") or "") in rematerialized_fallback_pages
                and str(row.get("action_id") or "") == "fallback_back"
            )
        ],
    }
    action_host_validation: dict[str, Any] = {
        "format": "ghostweb_action_host_manifest_validation",
        "schema_revision": 2,
        "source": "scripts.analysis.materialize_action_hosts",
        "contract_source_mode": action_host_manifest["contract_source_mode"],
        "missing_hosts": [],
        "duplicate_hosts": [],
        "missing_component_id_hosts": [],
        "ambiguous_component_id_hosts": [],
        "excluded_non_dom_actions": excluded_non_dom_actions,
        "preserved_host_contract": (
            {
                "path": str(preserved_manifest_path),
                "preserved_host_count": len(preserved_host_rows),
                "rematerialized_action_id": "fallback_back",
                "rematerialized_page_count": len(rematerialized_fallback_pages),
            }
            if package_only_contract_mode and preserved_manifest_path is not None
            else None
        ),
        "pages": {},
    }
    pages_to_validate = (
        incremental_expected_actions_by_page
        if package_only_contract_mode
        else expected_actions_by_page
    )
    for page_name, expected_action_ids in sorted(pages_to_validate.items()):
        page_path = pages_dir / page_name
        html_text = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
        host_rows = build_action_host_manifest_for_page(page_name, html_text, expected_action_ids)
        host_coverage = validate_action_host_coverage(expected_action_ids, host_rows)
        action_host_manifest["hosts"].extend(host_rows)
        action_host_validation["pages"][page_name] = host_coverage
        action_host_validation["missing_hosts"].extend(
            {"page": page_name, "action_id": action_id}
            for action_id in host_coverage.get("missing_hosts", [])
        )
        action_host_validation["duplicate_hosts"].extend(
            {"page": page_name, "action_id": action_id}
            for action_id in host_coverage.get("duplicate_hosts", [])
        )
        action_host_validation["missing_component_id_hosts"].extend(
            host_coverage.get("missing_component_id_hosts", [])
        )
        action_host_validation["ambiguous_component_id_hosts"].extend(
            host_coverage.get("ambiguous_component_id_hosts", [])
        )

    action_host_manifest["host_count"] = len(action_host_manifest["hosts"])
    action_host_validation["missing_host_count"] = len(action_host_validation["missing_hosts"])
    action_host_validation["duplicate_host_count"] = len(action_host_validation["duplicate_hosts"])
    action_host_validation["missing_component_id_host_count"] = len(
        action_host_validation["missing_component_id_hosts"]
    )
    action_host_validation["ambiguous_component_id_host_count"] = len(
        action_host_validation["ambiguous_component_id_hosts"]
    )
    _write_json(result_dir / "phase_pagegen" / "action_host_manifest.json", action_host_manifest)
    _write_json(result_dir / "phase_pagegen" / "action_host_manifest_validation.json", action_host_validation)
    action_execution_contracts = (
        _rebuild_sealed_action_execution_contracts(result_dir)
        if package_only_contract_mode
        else {"status": "not_applicable", "reason": "compiled_action_contracts_remain_authoritative"}
    )

    report = {
        "result_dir": str(result_dir),
        "contract_source_dir": str(contract_root),
        "contract_source_mode": action_host_manifest["contract_source_mode"],
        "preserved_host_contract": action_host_validation["preserved_host_contract"],
        "materialization_summary": materialization_report.get("summary", {}),
        "page_runtime_refresh_summary": page_runtime_refresh_report,
        "gesture_route_runtime_summary": gesture_route_runtime_report,
        "visible_return_affordance_summary": visible_return_affordance_report.get("summary", {}),
        "action_execution_contracts": action_execution_contracts,
        "host_validation_summary": {
            "missing_host_count": action_host_validation["missing_host_count"],
            "duplicate_host_count": action_host_validation["duplicate_host_count"],
            "missing_component_id_host_count": action_host_validation[
                "missing_component_id_host_count"
            ],
            "ambiguous_component_id_host_count": action_host_validation[
                "ambiguous_component_id_host_count"
            ],
            "host_count": action_host_manifest["host_count"],
        },
    }
    if (
        action_host_validation["missing_host_count"]
        or action_host_validation["duplicate_host_count"]
        or action_host_validation["missing_component_id_host_count"]
        or action_host_validation["ambiguous_component_id_host_count"]
        or visible_return_affordance_report.get("summary", {}).get("missing_page_count", 0)
    ):
        raise RuntimeError(
            "Action host validation failed: "
            f"missing={action_host_validation['missing_host_count']} "
            f"duplicate={action_host_validation['duplicate_host_count']} "
            f"missing_component_id={action_host_validation['missing_component_id_host_count']} "
            f"ambiguous_component_id={action_host_validation['ambiguous_component_id_host_count']}"
            f" missing_return_route_pages={visible_return_affordance_report.get('summary', {}).get('missing_page_count', 0)}"
        )
    export_mirror = (
        _reseal_exported_formal_mirror(
            source_root=result_dir,
            export_app_dir=export_app_dir,
        )
        if export_app_dir is not None
        else None
    )
    report["export_mirror"] = export_mirror
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize action hosts and action effects")
    parser.add_argument("--result-dir", required=True)
    parser.add_argument(
        "--contract-source-dir",
        default="",
        help="Read compiled/page-execution authority from this result dir while writing target pages in --result-dir.",
    )
    parser.add_argument(
        "--preserved-action-host-manifest",
        default="",
        help=(
            "In --preserve-generated-env mode, use this sealed host manifest as "
            "the pre-existing contract instead of the manifest being replaced."
        ),
    )
    parser.add_argument(
        "--export-app-dir",
        default="",
        help="Optional existing delivery <package>/app directory to reseal through the formal export allowlist.",
    )
    parser.add_argument(
        "--write-report",
        default="",
        help="Optional immutable evidence destination for the complete materialization report.",
    )
    parser.add_argument(
        "--preserve-generated-env",
        action="store_true",
        help="For a polished package, refresh only protected route runtime and manifests; preserve DOM/query/overlay/gesture behavior.",
    )
    parser.add_argument(
        "--included-page",
        action="append",
        default=[],
        help="Optional explicit dependency-sliced page filename; may be repeated.",
    )
    args = parser.parse_args()
    report = run(
        Path(args.result_dir).resolve(),
        contract_source_dir=(
            Path(args.contract_source_dir).resolve()
            if args.contract_source_dir
            else None
        ),
        preserve_generated_env=args.preserve_generated_env,
        preserved_action_host_manifest=(
            Path(args.preserved_action_host_manifest).resolve()
            if args.preserved_action_host_manifest
            else None
        ),
        export_app_dir=(Path(args.export_app_dir).resolve() if args.export_app_dir else None),
        included_page_filenames=args.included_page or None,
    )
    if args.write_report:
        _write_json(Path(args.write_report).resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
