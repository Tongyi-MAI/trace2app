
import json
import os
from typing import Any, Dict, List, Optional

from trace2world_pipeline.core.phase_output import PhaseOutputManager


def _infer_back_target(
    filename: str,
    route_path: str,
    all_pages: Dict[str, Dict[str, Any]],
    inbound_edges: Dict[str, List[tuple]],
    home_filename: str = "",
) -> Optional[str]:
    if filename == home_filename:
        return None
    sources = inbound_edges.get(filename, [])
    if sources:
        best_source, _ = max(sources, key=lambda s: s[1])
        if best_source != filename:
            return best_source
    if not route_path or route_path == "/":
        return home_filename or None
    parts = route_path.strip("/").split("/")
    if len(parts) > 1:
        parent_prefix = "/".join(parts[:-1])
        for fn, page in all_pages.items():
            if fn == filename:
                continue
            pp = str(page.get("route_path", "")).strip("/")
            if pp == parent_prefix:
                return fn
    return home_filename or None


def _classify_action(action: Dict[str, Any]) -> str:
    nav_kind = str(action.get("navigation_kind", "")).lower()
    if nav_kind in ("navigate_to_target", "push", "replace"):
        return "cross_page"
    reducer = str(action.get("reducer", "")).lower()
    if reducer in ("activate_inline_state", "open_overlay", "open_option_modal"):
        return "overlay"
    return "cross_page"


def build_route_manifest_from_data(
    route_contracts: Dict[str, Any],
    site_graph: Dict[str, Any],
    global_nav: Dict[str, Any],
) -> Dict[str, Any]:
    pages_list = route_contracts.get("pages", []) if isinstance(route_contracts, dict) else []
    pages_by_fn: Dict[str, Dict[str, Any]] = route_contracts.get("pages_by_filename", {}) if isinstance(route_contracts, dict) else {}
    if not pages_by_fn and isinstance(pages_list, list):
        pages_by_fn = {
            str(p.get("filename", "")): p
            for p in pages_list
            if isinstance(p, dict) and p.get("filename")
        }

    inbound_edges: Dict[str, List[tuple]] = {}
    for fn, page in pages_by_fn.items():
        for action in page.get("actions", []):
            target = str(action.get("target_filename", "")).strip()
            if target and target != fn:
                evidence_count = len(action.get("evidence_steps", []) or action.get("reference_trace_ids", []) or [1])
                inbound_edges.setdefault(target, []).append((fn, evidence_count))

    home_filename = ""
    for fn, page in pages_by_fn.items():
        rp = str(page.get("route_path", "")).strip().lower()
        if "home" in rp or "discover" in rp:
            home_filename = fn
            break
    if not home_filename:
        min_level = min((int(p.get("route_level", 99)) for p in pages_by_fn.values()), default=99)
        for fn, page in pages_by_fn.items():
            if int(page.get("route_level", 99)) == min_level:
                home_filename = fn
                break

    manifest_pages: Dict[str, Any] = {}
    runtime_projections: Dict[str, List[Dict[str, Any]]] = {}
    runtime_projection_views: Dict[str, Dict[str, Any]] = {}
    for fn, page in sorted(pages_by_fn.items()):
        route_path = str(page.get("route_path", "")).strip()
        back_target = _infer_back_target(fn, route_path, pages_by_fn, inbound_edges, home_filename)

        actions: List[Dict[str, str]] = []
        overlays: List[Dict[str, Any]] = []
        seen_targets: set = set()

        for action in page.get("actions", []):
            if str(action.get("boundary_decision", "")).strip() == "runtime_projection":
                canonical = str(action.get("canonical_runtime_filename", "")).strip()
                state_refs = action.get("state_refs", [])
                expected_state_diff = action.get("expected_state_diff", {})
                if not canonical or canonical != fn or not state_refs or not expected_state_diff:
                    raise ValueError(
                        f"{action.get('action_id', '')}: runtime projection lacks canonical page/state authority"
                    )
                runtime_projections.setdefault(fn, []).append({
                    "action_id": str(action.get("action_id", "")).strip(),
                    "canonical_runtime_filename": canonical,
                    "runtime_state_ref": str(action.get("runtime_state_ref", "")).strip(),
                    "reset_state": action.get("reset_state", {}),
                    "expected_state_diff": expected_state_diff,
                    "state_refs": state_refs,
                    "runtime_events": action.get("runtime_events", []),
                    "host_locator": action.get("host_locator", {}),
                    "authority_ref": str(action.get("authority_ref", "")).strip(),
                })
                view = action.get("runtime_projection_view")
                if isinstance(view, dict) and view:
                    prior = runtime_projection_views.get(fn)
                    if prior is not None and prior != view:
                        raise ValueError(
                            f"{action.get('action_id', '')}: conflicting runtime projection view for {fn}"
                        )
                    runtime_projection_views[fn] = view
                continue
            target = str(action.get("target_filename", "")).strip()
            if not target or target == fn:
                continue
            kind = _classify_action(action)
            if kind == "cross_page":
                key = target
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                actions.append({
                    "action_id": str(action.get("action_id", "")).strip(),
                    "target": target,
                    "trigger_text": str(action.get("trigger_text", "")).strip()[:60],
                })

        manifest_pages[fn] = {
            "route_path": route_path,
            "back_target": back_target,
            "actions": actions,
            "overlays": overlays,
        }

    global_nav_tabs: List[Dict[str, str]] = []
    if isinstance(global_nav, dict):
        for tab in global_nav.get("tabs", global_nav.get("items", [])):
            if isinstance(tab, dict):
                global_nav_tabs.append({
                    "label": str(tab.get("label", tab.get("name", ""))).strip(),
                    "target": str(tab.get("target_filename", tab.get("target", ""))).strip(),
                })

    return {
        "format": "ghostweb_route_manifest",
        "version": 1,
        "pages": manifest_pages,
        "runtime_projections": runtime_projections,
        "runtime_projection_views": runtime_projection_views,
        "global_navigation": {"tabs": global_nav_tabs} if global_nav_tabs else {},
    }


def build_route_manifest(
    route_execution_contracts_path: str,
    site_graph_path: str = "",
    global_navigation_path: str = "",
) -> Dict[str, Any]:
    with open(route_execution_contracts_path, "r", encoding="utf-8") as f:
        rec = json.load(f)
    global_nav: Dict[str, Any] = {}
    if global_navigation_path and os.path.exists(global_navigation_path):
        with open(global_navigation_path, "r", encoding="utf-8") as f:
            global_nav = json.load(f)
    return build_route_manifest_from_data(rec, {}, global_nav)


def write_route_manifest_js(manifest: Dict[str, Any], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    js_content = "window.GhostwebRouteManifest = " + json.dumps(manifest, indent=2, ensure_ascii=False) + ";\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(js_content)


def generate_route_manifest_from_site_model(site_model: "Any", output_dir: str) -> str:
    route_contracts = site_model.route_execution_contracts
    if not route_contracts:
        return ""
    site_graph = site_model.structure.site_graph.to_dict() if site_model.structure.site_graph else {"edges": []}
    global_nav = site_model.structure.global_navigation or {}
    manifest = build_route_manifest_from_data(route_contracts, site_graph, global_nav)
    runtime_dir = str(PhaseOutputManager(output_dir).formal("sdk_generation", "runtime"))
    manifest_path = os.path.join(runtime_dir, "route_manifest.js")
    write_route_manifest_js(manifest, manifest_path)
    return manifest_path


def generate_route_manifest(output_dir: str) -> str:
    site_model_dir = str(PhaseOutputManager(output_dir).site_model_dir())
    rec_path = os.path.join(site_model_dir, "route_execution_contracts.json")
    sg_path = os.path.join(site_model_dir, "site_graph.json")
    gnav_path = os.path.join(site_model_dir, "global_navigation.json")

    if not os.path.exists(rec_path):
        return ""

    manifest = build_route_manifest(
        route_execution_contracts_path=rec_path,
        site_graph_path=sg_path if os.path.exists(sg_path) else "",
        global_navigation_path=gnav_path if os.path.exists(gnav_path) else "",
    )

    runtime_dir = str(PhaseOutputManager(output_dir).formal("sdk_generation", "runtime"))
    manifest_path = os.path.join(runtime_dir, "route_manifest.js")
    write_route_manifest_js(manifest, manifest_path)
    return manifest_path
