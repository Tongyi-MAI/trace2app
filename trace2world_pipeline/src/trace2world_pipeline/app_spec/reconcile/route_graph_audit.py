

import argparse
import html
import json
import os
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def site_model_dir_candidates(output_dir: Path) -> List[Path]:
    return [
        output_dir / "phase_consolidation" / "site_model",
    ]


def load_first_site_model_json(output_dir: Path, name: str, default: Any = None) -> Any:
    for site_model_dir in site_model_dir_candidates(output_dir):
        data = load_json(site_model_dir / name, None)
        if data is not None:
            return data
    return default


def resolve_global_reasoning_path(
    output_dir: Path,
    stage: str = "default",
    explicit_path: str = "",
) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()

    debug_dir = output_dir / "data" / "debug"
    stage = str(stage or "default").strip().lower()
    candidates_by_stage = {
        "default": [
            debug_dir / "global_reasoning_final.json",
            debug_dir / "global_reasoning_page_assignment_audited.json",
            debug_dir / "global_reasoning.json",
        ],
        "audited": [
            debug_dir / "global_reasoning_page_assignment_audited.json",
            debug_dir / "global_reasoning_feedback_fixed.json",
            debug_dir / "global_reasoning_final.json",
        ],
        "final": [
            debug_dir / "global_reasoning_final.json",
            debug_dir / "global_reasoning.json",
        ],
        "resolved": [
            debug_dir / "global_reasoning_resolved.json",
        ],
        "proposal": [
            debug_dir / "global_reasoning_proposal.json",
        ],
    }
    if stage not in candidates_by_stage:
        raise ValueError(
            "--global-reasoning-stage must be one of: {}".format(
                ", ".join(sorted(candidates_by_stage))
            )
        )

    for candidate in candidates_by_stage[stage]:
        if candidate.exists():
            return candidate.resolve()
    return candidates_by_stage[stage][0].resolve()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def clip(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _short_path(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


def rel_path(path: str, from_dir: Path) -> str:
    if not path:
        return ""
    try:
        return os.path.relpath(path, from_dir)
    except Exception:
        return path


def resolve_trace_screenshot(trace_dir: Path, step_index: int, image_file: str = "") -> Path:
    screenshots_dir = trace_dir / "screenshots"
    if screenshots_dir.exists():
        stems = [
            f"{trace_dir.name}-0-{step_index + 1}",
            f"step_{step_index:04d}",
            f"action_{step_index:04d}",
        ]
        for stem in stems:
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                candidate = screenshots_dir / f"{stem}{ext}"
                if candidate.exists():
                    return candidate

    if image_file:
        candidate = trace_dir / image_file
        if candidate.exists():
            return candidate
    return trace_dir / f"step_{step_index:04d}.png"


def audit_asset_ref(source: str, html_dir: Path) -> str:
    if not source:
        return ""
    source_path = Path(source)
    if not source_path.exists() or not source_path.is_file():
        return rel_path(str(source_path), html_dir)

    trace_id = source_path.parent.name
    if source_path.parent.name == "screenshots" and source_path.parent.parent.name:
        trace_id = source_path.parent.parent.name
    asset_path = html_dir / "route_graph_audit_assets" / trace_id / source_path.name
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    if not asset_path.exists():
        try:
            asset_path.symlink_to(source_path)
        except OSError:

            return rel_path(str(source_path), html_dir)
    return rel_path(str(asset_path), html_dir)


def action_to_text(action: Any) -> str:
    if isinstance(action, dict):
        action_type = str(action.get("action_type", "") or "")
        if action_type == "click":
            return "click ({}, {})".format(action.get("x"), action.get("y"))
        if action_type == "swipe":
            return "swipe ({}, {}) -> ({}, {})".format(
                action.get("start_x"),
                action.get("start_y"),
                action.get("end_x"),
                action.get("end_y"),
            )
        if action_type == "open_app":
            return "open_app {}".format(action.get("app_name") or "").strip()
        if action_type == "type":
            return "type {}".format(action.get("text") or "").strip()
        return action_type or json.dumps(action, ensure_ascii=False)
    return str(action or "")


def parse_prediction_tags(prediction: Any) -> Tuple[str, str]:
    if not isinstance(prediction, str) or not prediction.strip():
        return "", ""

    def extract(tag: str) -> str:
        match = re.search(
            r"<{tag}>\s*(.*?)\s*</{tag}>".format(tag=re.escape(tag)),
            prediction,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        return " ".join(match.group(1).strip().split())

    return extract("thinking"), extract("action")


def action_class(action: str) -> str:
    text = str(action or "").lower()
    if "swipe" in text or "scroll" in text or "滑动" in text or "滚动" in text:
        return "swipe"
    if "type" in text or "input" in text or "输入" in text:
        return "type"
    if "click" in text or "tap" in text or "点击" in text or "选择" in text:
        return "click"
    if "open_app" in text or "open app" in text or "打开" in text or "启动" in text:
        return "open_app"
    if "wait" in text or "sleep" in text or "等待" in text or "稍等" in text:
        return "wait"
    if "finished" in text or "done" in text or "完成" in text or "结束" in text:
        return "finished"
    if "takeover" in text:
        return "takeover"
    return "other"


def route_domain(page: Dict[str, Any]) -> str:
    route = str(page.get("route_path", "") or "")
    parts = [p for p in route.split("/") if p]
    if parts:
        return re.sub(r"[^a-z0-9_:-]+", "_", parts[0].strip().lower()).strip("_") or "unscoped"
    cid = str(page.get("page_cluster_id", "") or "")
    cid_tokens = [token for token in re.split(r"[^A-Za-z0-9]+", cid.lower()) if token and token != "c"]
    return cid_tokens[0] if cid_tokens else "unscoped"


def color_for(key: str) -> str:
    palette = [
        "#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed", "#0891b2",
        "#be123c", "#4d7c0f", "#9333ea", "#0f766e", "#ea580c", "#475569",
        "#c026d3", "#1d4ed8", "#15803d", "#b45309", "#be185d", "#0369a1",
    ]
    total = sum((idx + 1) * ord(ch) for idx, ch in enumerate(str(key)))
    return palette[total % len(palette)]


def load_traces(trace_root: Path) -> Dict[str, Dict[str, Any]]:
    traces: Dict[str, Dict[str, Any]] = {}
    for trace_dir in sorted(trace_root.iterdir() if trace_root.exists() else []):
        if not trace_dir.is_dir():
            continue
        steps = []
        task = ""
        trajectory_path = trace_dir / "trajectory.json"
        if trajectory_path.exists():
            payload = load_json(trajectory_path, {}) or {}
            task = str(payload.get("instruction", "") or "")
            step_entries = payload.get("trajectory", []) if isinstance(payload.get("trajectory", []), list) else []
            for idx, data in enumerate(step_entries):
                if not isinstance(data, dict):
                    continue
                step_index = data.get("step_index")
                if not isinstance(step_index, int):
                    step_index = idx
                image_file = str(data.get("screenshot", "") or data.get("image_file", "") or "")
                screenshot = resolve_trace_screenshot(trace_dir, step_index, image_file=image_file)
                raw_action = action_to_text(data.get("action", ""))
                if data.get("text") is not None:
                    raw_action = (raw_action + " " + str(data.get("text"))).strip()
                coordinate = data.get("coordinate")
                if isinstance(coordinate, list) and len(coordinate) >= 2:
                    raw_action = (raw_action + f" ({coordinate[0]}, {coordinate[1]})").strip()
                prediction_thought, prediction_action = parse_prediction_tags(data.get("prediction", ""))
                steps.append({
                    "trace_id": trace_dir.name,
                    "step_index": step_index,
                    "key": "{}:{}".format(trace_dir.name, step_index),
                    "action": prediction_action or raw_action,
                    "raw_action": raw_action,
                    "thought": prediction_thought or data.get("thought", "") or data.get("reasoning_content", ""),
                    "screenshot_path": str(screenshot),
                    "raw": data,
                })
        elif (trace_dir / "traj.json").exists():
            for step_path in sorted(trace_dir.glob("step_*.json")):
                data = load_json(step_path, {}) or {}
                if not task:
                    task = str(data.get("instruction", "") or "")
                try:
                    idx = int(data.get("step_index", step_path.stem.split("_")[-1]))
                except Exception:
                    continue
                image_file = str(data.get("image_file", "") or "")
                screenshot = resolve_trace_screenshot(trace_dir, idx, image_file=image_file)
                raw_action = action_to_text(data.get("action", ""))
                prediction_thought, prediction_action = parse_prediction_tags(data.get("prediction", ""))
                steps.append({
                    "trace_id": trace_dir.name,
                    "step_index": idx,
                    "key": "{}:{}".format(trace_dir.name, idx),
                    "action": prediction_action or raw_action,
                    "raw_action": raw_action,
                    "thought": prediction_thought or data.get("thought", "") or data.get("reasoning_content", ""),
                    "screenshot_path": str(screenshot),
                    "raw": data,
                })
        else:
            continue
        traces[trace_dir.name] = {"trace_id": trace_dir.name, "task": task, "steps": steps}
    return traces


def load_observations(output_dir: Path) -> Dict[str, Dict[str, Any]]:
    observations: Dict[str, Dict[str, Any]] = {}
    obs_dirs = [
        output_dir / "data" / "debug" / "frame_observations",
        output_dir / "phase_frame_analysis" / "frame_observations",
    ]
    for obs_dir in obs_dirs:
        for path in sorted(obs_dir.glob("*.json") if obs_dir.exists() else []):
            data = load_json(path, {}) or {}
            key = "{}:{}".format(data.get("trace_id"), data.get("step_index"))
            observations[key] = data
    return observations


def _step_key_from_screenshot_ref(path_text: str) -> str:
    path = Path(str(path_text or ""))
    trace_id = path.parent.name
    match = re.search(r"step_(\d+)\.(?:png|jpg|jpeg)$", path.name, flags=re.IGNORECASE)
    if not trace_id or not match:
        return ""
    try:
        return "{}:{}".format(trace_id, int(match.group(1)))
    except Exception:
        return ""


def _entity_counts_and_samples(content_data: Dict[str, Any]) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
    extracted = content_data.get("extracted_items", {}) if isinstance(content_data, dict) else {}
    counts: Dict[str, int] = {}
    samples: Dict[str, List[str]] = {}
    if not isinstance(extracted, dict):
        return counts, samples
    for entity_name, items in extracted.items():
        if entity_name == "_metadata" or not isinstance(items, list):
            continue
        counts[str(entity_name)] = len(items)
        labels: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = ""
            for field in ("title", "name", "display_name", "label"):
                value = str(item.get(field, "") or "").strip()
                if value:
                    label = value
                    break
            if label and label not in labels:
                labels.append(label)
            if len(labels) >= 5:
                break
        if labels:
            samples[str(entity_name)] = labels
    return counts, samples


def _load_page_prototype_records(output_dir: Path) -> List[Dict[str, Any]]:
    proto_dir = None
    index: Dict[str, Any] = {}
    for site_model_dir in site_model_dir_candidates(output_dir):
        candidate = site_model_dir / "page_prototypes"
        candidate_index = load_json(candidate / "index.json", {}) or {}
        if isinstance(candidate_index, dict) and candidate_index.get("pages"):
            proto_dir = candidate
            index = candidate_index
            break
    if proto_dir is None:
        return []
    pages = index.get("pages", []) if isinstance(index, dict) else []
    records: List[Dict[str, Any]] = []
    if not isinstance(pages, list):
        return records
    for item in pages:
        if not isinstance(item, dict):
            continue
        structure_file = str(item.get("structure_file", "") or "")
        content_file = str(item.get("content_file", "") or "")
        if not structure_file:
            continue
        structure = load_json(proto_dir / structure_file, {}) or {}
        content = load_json(proto_dir / content_file, {}) if content_file else {}
        if not isinstance(structure, dict):
            continue
        if not isinstance(content, dict):
            content = {}
        records.append({
            "index": item,
            "structure": structure,
            "content": content,
        })
    return records


def _load_instance_planning(output_dir: Path) -> Dict[str, Any]:
    for path in (
        output_dir / "phase_instance_splitting" / "instance_plan.json",
        output_dir / "phase_instance_splitting" / "instance_planning.json",
        output_dir / "phase_consolidation" / "site_model" / "instance_plan.json",
    ):
        data = load_json(path, {}) or {}
        if isinstance(data, dict) and data:
            return data
    return {}


def _planning_by_instance_key(planning: Dict[str, Any], cluster_id: str) -> Dict[str, Dict[str, Any]]:
    cluster_plan = planning.get(cluster_id, {}) if isinstance(planning, dict) else {}
    groups = cluster_plan.get("instance_groups", []) if isinstance(cluster_plan, dict) else []
    result: Dict[str, Dict[str, Any]] = {}
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        instance_key = str(group.get("instance_key", "") or "").strip()
        if instance_key:
            result[instance_key] = group
    return result


def build_instance_audit(
    output_dir: Path,
    html_dir: Path,
    gr: Dict[str, Any],
    step_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    records = _load_page_prototype_records(output_dir)
    planning = _load_instance_planning(output_dir)
    validation = {}
    for path in (
        output_dir / "phase_instance_splitting" / "page_instance_validation.json",
        output_dir / "phase_consolidation" / "site_model" / "page_instance_validation.json",
    ):
        validation = load_json(path, {}) or {}
        if isinstance(validation, dict) and validation:
            break
    canonical_pages = {
        str(page.get("page_cluster_id", "") or "").strip(): page
        for page in gr.get("canonical_pages", [])
        if isinstance(page, dict) and str(page.get("page_cluster_id", "") or "").strip()
    }
    suspicious_by_cluster = {
        str(item.get("cluster_id", "") or "").strip(): item
        for item in validation.get("suspicious_splits", [])
        if isinstance(item, dict) and str(item.get("cluster_id", "") or "").strip()
    } if isinstance(validation, dict) else {}
    rows = step_rows or []
    row_page_by_key = {
        str(row.get("key", "") or "").strip(): str(row.get("pageId", "") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("key", "") or "").strip()
    }

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        structure = record["structure"]
        content = record["content"]
        structure_data = structure.get("structure_data", {}) if isinstance(structure.get("structure_data", {}), dict) else {}
        cluster_id = str(
            structure_data.get("source_cluster_id", "")
            or structure_data.get("cluster_id", "")
            or ""
        ).strip()
        if not cluster_id:
            cluster_id = str(structure.get("page_id", "") or "").strip()
        instance_key = str(structure_data.get("instance_key", "") or "").strip()
        screenshot_refs = []
        evidence_steps = []
        for ref in [str(ref) for ref in structure.get("screenshot_refs", []) if str(ref).strip()]:
            step_key = _step_key_from_screenshot_ref(ref)
            row_page_id = row_page_by_key.get(step_key, "") if step_key else ""
            if row_page_id and cluster_id and row_page_id != cluster_id:
                continue
            screenshot_refs.append(ref)
            evidence_steps.append({
                "screenshot": audit_asset_ref(ref, html_dir),
                "screenshotPath": ref,
                "stepKey": step_key,
            })
        entity_counts, entity_samples = _entity_counts_and_samples(content)
        grouped[cluster_id].append({
            "instanceId": str(structure.get("page_id", "") or "").strip(),
            "pageId": str(structure.get("page_id", "") or "").strip(),
            "pageName": str(structure.get("page_name_full", "") or structure.get("page_name", "") or "").strip(),
            "pageType": str(structure.get("page_type", "") or "").strip(),
            "filename": str(structure.get("filename", "") or "").strip(),
            "sourceClusterId": cluster_id,
            "instanceKey": instance_key,
            "routePath": str(structure_data.get("route_path", "") or "").strip(),
            "parentRoutePath": str(structure_data.get("parent_route_path", "") or "").strip(),
            "screenshotRefs": screenshot_refs,
            "evidenceSteps": evidence_steps,
            "entityCounts": entity_counts,
            "entitySamples": entity_samples,
            "primaryFunctions": structure.get("primary_functions", []) if isinstance(structure.get("primary_functions", []), list) else [],
        })

    planning_grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for cluster_id, plan in planning.items():
        if not isinstance(plan, dict):
            continue
        groups = plan.get("instance_groups", [])
        existing_by_key = {inst["instanceKey"]: inst for inst in grouped.get(cluster_id, [])}
        if groups:
            planned_keys = set()
            planned_obs_keys = set()
            for matched in groups:
                key = str(matched.get("instance_key", "") or "").strip()
                if key:
                    planned_keys.add(key)
                obs_keys = matched.get("obs_keys", [])
                if isinstance(obs_keys, list):
                    planned_obs_keys.update(
                        str(obs_key or "").strip()
                        for obs_key in obs_keys
                        if str(obs_key or "").strip()
                    )
                base = existing_by_key.get(key, {})
                planning_grouped[cluster_id].append({
                    "instanceId": base.get("instanceId", key or cluster_id),
                    "pageId": base.get("pageId", key or cluster_id),
                    "pageName": base.get("pageName", str(matched.get("instance_label", "") or key or cluster_id)),
                    "pageType": base.get("pageType", str(plan.get("page_type_hint", "") or "")),
                    "filename": base.get("filename", ""),
                    "sourceClusterId": cluster_id,
                    "instanceKey": key,
                    "routePath": base.get("routePath", str(plan.get("route_path", "") or "").strip()),
                    "parentRoutePath": base.get("parentRoutePath", ""),
                    "screenshotRefs": base.get("screenshotRefs", []),
                    "evidenceSteps": base.get("evidenceSteps", []),
                    "entityCounts": base.get("entityCounts", {}),
                    "entitySamples": base.get("entitySamples", {}),
                    "primaryFunctions": base.get("primaryFunctions", []),
                })
            for existing in grouped.get(cluster_id, []):
                key = str(existing.get("instanceKey", "") or "").strip()
                evidence_keys = {
                    str(evidence.get("stepKey", "") or "").strip()
                    for evidence in existing.get("evidenceSteps", [])
                    if isinstance(evidence, dict) and str(evidence.get("stepKey", "") or "").strip()
                }
                if key and key not in planned_keys and not (evidence_keys & planned_obs_keys):
                    planning_grouped[cluster_id].append(existing)
        else:
            existing = grouped.get(cluster_id, [])
            if existing:
                rep = dict(existing[0])
                rep["instanceKey"] = rep.get("instanceKey") or cluster_id
                rep["pageName"] = rep.get("pageName") or str(plan.get("page_name", "") or cluster_id)
                planning_grouped[cluster_id] = [rep]
            else:
                planning_grouped[cluster_id] = [{
                    "instanceId": cluster_id,
                    "pageId": cluster_id,
                    "pageName": str(plan.get("page_name", "") or cluster_id),
                    "pageType": str(plan.get("page_type_hint", "") or ""),
                    "filename": "",
                    "sourceClusterId": cluster_id,
                    "instanceKey": cluster_id,
                    "routePath": str(plan.get("route_path", "") or "").strip(),
                    "parentRoutePath": "",
                    "screenshotRefs": [],
                    "evidenceSteps": [],
                    "entityCounts": {},
                    "entitySamples": {},
                    "primaryFunctions": [],
                }]

    for cluster_id, instances in grouped.items():
        if cluster_id not in planning_grouped:
            planning_grouped[cluster_id] = instances

    source_clusters: List[Dict[str, Any]] = []
    for cluster_id, instances in planning_grouped.items():
        canonical = canonical_pages.get(cluster_id, {})
        plan = planning.get(cluster_id, {}) if isinstance(planning, dict) else {}
        plan_by_key = _planning_by_instance_key(planning, cluster_id)
        suspicious = suspicious_by_cluster.get(cluster_id, {})
        for instance in instances:
            matched = plan_by_key.get(instance.get("instanceKey", ""), {})
            instance["matchedPlanning"] = {
                "instance_key": matched.get("instance_key", ""),
                "instance_label": matched.get("instance_label", ""),
                "trace_ids": matched.get("trace_ids", [matched.get("trace_id", "")]),
                "obs_keys": matched.get("obs_keys", []),
            } if matched else {}
        source_clusters.append({
            "clusterId": cluster_id,
            "canonicalName": str(canonical.get("page_name", "") or plan.get("page_name", "") or cluster_id),
            "canonicalType": str(canonical.get("page_type_hint", "") or plan.get("page_type_hint", "") or ""),
            "routePath": str(canonical.get("route_path", "") or plan.get("route_path", "") or ""),
            "materializedCount": len(instances),
            "splitMode": "split" if len(instances) > 1 else "single",
            "isSuspicious": bool(suspicious),
            "suspiciousReason": str(suspicious.get("reason", "") or ""),
            "planning": plan if isinstance(plan, dict) else {},
            "instances": sorted(instances, key=lambda item: (item.get("routePath", ""), item.get("pageId", ""))),
        })

    source_clusters.sort(key=lambda item: (-item["materializedCount"], item["clusterId"]))
    instance_by_id: Dict[str, Dict[str, Any]] = {}
    instances_by_cluster: Dict[str, List[Dict[str, Any]]] = {}
    for group in source_clusters:
        instances = group.get("instances", [])
        if not isinstance(instances, list):
            continue
        instances_by_cluster[str(group.get("clusterId", "") or "")] = instances
        for instance in instances:
            instance_id = str(instance.get("instanceId", "") or "").strip()
            if instance_id:
                instance_by_id[instance_id] = instance

    step_to_instance: Dict[str, str] = {}
    assignment_sources: Dict[str, str] = {}
    for instance_id, instance in instance_by_id.items():
        for evidence in instance.get("evidenceSteps", []) if isinstance(instance.get("evidenceSteps", []), list) else []:
            key = str(evidence.get("stepKey", "") or "").strip() if isinstance(evidence, dict) else ""
            if key:
                row_page_id = row_page_by_key.get(key, "")
                instance_cluster_id = str(instance.get("sourceClusterId", "") or "").strip()
                if row_page_id and instance_cluster_id and row_page_id != instance_cluster_id:
                    continue
                step_to_instance[key] = instance_id
                assignment_sources[key] = "screenshot_evidence"
        matched = instance.get("matchedPlanning", {})
        obs_keys = matched.get("obs_keys", []) if isinstance(matched, dict) else []
        for key in obs_keys if isinstance(obs_keys, list) else []:
            key = str(key or "").strip()
            if key:
                step_to_instance.setdefault(key, instance_id)
                assignment_sources.setdefault(key, "matched_planning")

    for row in rows:
        key = str(row.get("key", "") or "").strip()
        cluster_id = str(row.get("pageId", "") or "").strip()
        instances = instances_by_cluster.get(cluster_id, [])
        if key and key not in step_to_instance and len(instances) == 1:
            instance_id = str(instances[0].get("instanceId", "") or "").strip()
            if instance_id:
                step_to_instance[key] = instance_id
                assignment_sources[key] = "single_instance_cluster"

    rows_by_trace: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        trace_id = str(row.get("traceId", "") or "").strip()
        if trace_id:
            rows_by_trace[trace_id].append(row)

    trace_instance_flows: List[Dict[str, Any]] = []
    edge_acc: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for trace_id, trace_rows in sorted(rows_by_trace.items()):
        ordered = sorted(trace_rows, key=lambda item: item.get("stepIndex", 0))
        instance_steps: List[Dict[str, Any]] = []
        unassigned_steps: List[Dict[str, Any]] = []
        previous_instance_id = ""
        for row in ordered:
            key = str(row.get("key", "") or "").strip()
            cluster_id = str(row.get("pageId", "") or "").strip()
            instance_id = step_to_instance.get(key, "")
            if instance_id:
                instance = instance_by_id.get(instance_id, {})
                item = {
                    "key": key,
                    "stepIndex": row.get("stepIndex"),
                    "traceId": trace_id,
                    "clusterId": cluster_id,
                    "instanceId": instance_id,
                    "instanceName": instance.get("pageName", instance_id),
                    "action": row.get("action", ""),
                    "assignmentSource": assignment_sources.get(key, ""),
                }
                instance_steps.append(item)
                if previous_instance_id and previous_instance_id != instance_id:
                    edge_key = (previous_instance_id, instance_id)
                    acc = edge_acc.setdefault(edge_key, {
                        "id": f"instance:{previous_instance_id}->{instance_id}",
                        "from": previous_instance_id,
                        "to": instance_id,
                        "count": 0,
                        "traces": [],
                        "examples": [],
                    })
                    acc["count"] += 1
                    if trace_id not in acc["traces"]:
                        acc["traces"].append(trace_id)
                    if len(acc["examples"]) < 8:
                        acc["examples"].append({
                            "traceId": trace_id,
                            "fromStep": instance_steps[-2]["key"] if len(instance_steps) >= 2 else "",
                            "toStep": key,
                            "action": row.get("action", ""),
                        })
                previous_instance_id = instance_id
            elif len(instances_by_cluster.get(cluster_id, [])) > 1:
                unassigned_steps.append({
                    "key": key,
                    "traceId": trace_id,
                    "stepIndex": row.get("stepIndex"),
                    "clusterId": cluster_id,
                    "action": row.get("action", ""),
                })
        if instance_steps or unassigned_steps:
            trace_instance_flows.append({
                "id": trace_id,
                "instanceSteps": instance_steps,
                "instanceIds": list(dict.fromkeys(item["instanceId"] for item in instance_steps)),
                "edgeIds": [],
                "unassignedSteps": unassigned_steps,
            })

    instance_edges = sorted(edge_acc.values(), key=lambda item: (-item["count"], item["from"], item["to"]))
    edge_ids_by_trace: Dict[str, List[str]] = defaultdict(list)
    for edge in instance_edges:
        for trace_id in edge.get("traces", []):
            edge_ids_by_trace[trace_id].append(edge["id"])
    for flow in trace_instance_flows:
        flow["edgeIds"] = edge_ids_by_trace.get(flow["id"], [])

    split_count = sum(1 for item in source_clusters if item["materializedCount"] > 1)
    suspicious_count = sum(1 for item in source_clusters if item.get("isSuspicious"))
    return {
        "summary": {
            "materializedPages": sum(len(instances) for instances in planning_grouped.values()),
            "sourceClusters": len(source_clusters),
            "splitClusters": split_count,
            "suspiciousSplits": suspicious_count,
            "traceInstanceFlows": len(trace_instance_flows),
            "instanceEdges": len(instance_edges),
        },
        "sourceClusters": source_clusters,
        "stepToInstance": step_to_instance,
        "traceInstanceFlows": trace_instance_flows,
        "instanceEdges": instance_edges,
    }


def load_materialized_observation_bindings(output_dir: Path) -> Dict[str, Dict[str, Any]]:
    payload = load_first_site_model_json(output_dir, "materialized_observation_bindings.json", {}) or {}
    bindings = payload.get("bindings_by_obs_key", {}) if isinstance(payload, dict) else {}
    if not isinstance(bindings, dict):
        return {}
    return {
        str(key or "").strip(): value
        for key, value in bindings.items()
        if str(key or "").strip() and isinstance(value, dict)
    }


def load_step_component_bindings(output_dir: Path) -> Dict[str, Dict[str, Any]]:
    for path in (
        output_dir / "data" / "site_model" / "step_component_bindings.json",
        output_dir / "projections" / "interaction_surfaces" / "step_component_bindings.json",
    ):
        payload = load_json(path, {}) or {}
        if not isinstance(payload, dict):
            continue
        result: Dict[str, Dict[str, Any]] = {}
        by_step = payload.get("bindings_by_step", {})
        if isinstance(by_step, dict):
            for trace_id, steps in by_step.items():
                if not isinstance(steps, dict):
                    continue
                for step_index, binding in steps.items():
                    if isinstance(binding, dict):
                        result[f"{trace_id}:{step_index}"] = binding
        bindings = payload.get("bindings", [])
        if isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                trace_id = str(binding.get("trace_id", "") or "").strip()
                step_index = binding.get("step_index")
                if trace_id and isinstance(step_index, int):
                    result.setdefault(f"{trace_id}:{step_index}", binding)
        if result:
            return result
    return {}


def materialized_page_for_step(
    key: str,
    label: Any,
    bindings_by_step: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    binding = bindings_by_step.get(key, {})
    label = label if isinstance(label, dict) else {}
    general_page_id = str(
        binding.get("general_page_id", "")
        or label.get("general_page_id", "")
        or label.get("source_page_cluster_id", "")
        or label.get("page_cluster_id", "")
        or ""
    ).strip()
    materialized_page_id = str(
        binding.get("materialized_page_id", "")
        or binding.get("page_id", "")
        or label.get("materialized_page_id", "")
        or ""
    ).strip()
    return {
        "generalPageId": general_page_id,
        "pageId": materialized_page_id,
        "pageName": str(binding.get("page_name", "") or "").strip(),
        "filename": str(binding.get("filename", "") or "").strip(),
        "instanceKey": str(binding.get("instance_key", "") or "").strip(),
        "source": str(binding.get("source", "") or "").strip(),
        "confidence": str(binding.get("confidence", "") or "").strip(),
    }


def build_step_rows(
    traces: Dict[str, Dict[str, Any]],
    gr: Dict[str, Any],
    observations: Dict[str, Dict[str, Any]],
    html_dir: Path,
    materialized_bindings: Optional[Dict[str, Dict[str, Any]]] = None,
    step_component_bindings: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    labels = {
        "{}:{}".format(item.get("trace_id"), item.get("step_index")): item
        for item in gr.get("step_labels", [])
        if isinstance(item, dict)
    }
    skips = {
        "{}:{}".format(item.get("trace_id"), item.get("step_index")): item
        for item in gr.get("skipped_steps", [])
        if isinstance(item, dict)
    }
    rows: List[Dict[str, Any]] = []
    rows_by_key: Dict[str, Dict[str, Any]] = {}
    materialized_bindings = materialized_bindings or {}
    step_component_bindings = step_component_bindings or {}
    for trace in traces.values():
        trace_steps = sorted(trace["steps"], key=lambda s: s["step_index"])
        for pos, step in enumerate(trace_steps):
            key = step["key"]
            obs = observations.get(key, {}) or {}
            label = labels.get(key)
            skip = skips.get(key)
            materialized_page = materialized_page_for_step(key, label, materialized_bindings)
            prev_step = trace_steps[pos - 1] if pos > 0 else None
            next_step = trace_steps[pos + 1] if pos + 1 < len(trace_steps) else None
            row = {
                "key": key,
                "traceId": trace["trace_id"],
                "task": trace.get("task", ""),
                "stepIndex": step["step_index"],
                "action": step.get("action", ""),
                "rawAction": step.get("raw_action", ""),
                "actionClass": action_class(step.get("action", "")),
                "thought": step.get("thought", ""),
                "screenshot": audit_asset_ref(step.get("screenshot_path", ""), html_dir),
                "label": label,
                "skip": skip,
                "pageId": label.get("page_cluster_id", "") if isinstance(label, dict) else "",
                "materializedPage": materialized_page,
                "componentBinding": step_component_bindings.get(key, {}),
                "feedbackFixed": bool(label.get("feedback_fixed")) if isinstance(label, dict) else False,
                "skipReason": skip.get("skip_reason", "") if isinstance(skip, dict) else "",
                "observation": {
                    "description": obs.get("description", ""),
                    "headerPattern": obs.get("header_pattern", ""),
                    "shellSection": obs.get("shell_section", ""),
                    "bodyPattern": obs.get("body_pattern", ""),
                    "stickyActionPattern": obs.get("sticky_action_pattern", ""),
                    "identityConfidence": obs.get("identity_confidence", 0),
                    "transitionFromPrevious": (
                        obs.get("transition_from_previous")
                        if obs.get("transition_from_previous") is not None
                        else (obs.get("page_state", {}) or {}).get("transition_from_previous")
                    ),
                    "transitionKind": (
                        obs.get("transition_kind")
                        or (obs.get("page_state", {}) or {}).get("transition_kind", "")
                    ),
                    "transitionEvidence": (
                        obs.get("transition_evidence")
                        or (obs.get("page_state", {}) or {}).get("transition_evidence", [])
                    ),
                },
                "prev": {
                    "key": prev_step.get("key", "") if prev_step else "",
                    "action": prev_step.get("action", "") if prev_step else "",
                },
                "next": {
                    "key": next_step.get("key", "") if next_step else "",
                    "action": next_step.get("action", "") if next_step else "",
                },
            }
            rows.append(row)
            rows_by_key[key] = row
    return rows, rows_by_key


def signature(row: Dict[str, Any]) -> str:
    obs = row.get("observation") or {}
    return "|".join([
        str(obs.get("headerPattern", "")),
        str(obs.get("shellSection", "")),
        str(obs.get("bodyPattern", "")),
        str(obs.get("stickyActionPattern", "")),
    ])


def looks_like_phone_home(row: Dict[str, Any]) -> bool:
    obs = row.get("observation") or {}
    text = " ".join([
        str(obs.get("description", "")),
        str(obs.get("shellSection", "")),
        str(obs.get("headerPattern", "")),
        str(obs.get("bodyPattern", "")),
    ]).lower()
    terms = (
        "phone launcher", "system launcher", "android launcher",
        "phone home", "device home", "手机桌面", "系统桌面",
        "启动器", "桌面",
    )
    return any(term in text for term in terms)


def is_app_like_skipped(row: Dict[str, Any]) -> bool:
    if row.get("skipReason") != "phone_home":
        return False
    if looks_like_phone_home(row):
        return False
    obs = row.get("observation") or {}
    try:
        confidence = float(obs.get("identityConfidence", 0) or 0)
    except Exception:
        confidence = 0.0
    patterns = signature(row).lower()
    return confidence >= 0.70 or any(
        token in patterns
        for token in ("search", "feed", "bottom_tab_bar", "home", "list", "tabs")
    )


SYSTEM_BRIDGE_REASONS = {
    "phone_home",
    "system_launcher",
    "non_target_app",
    "non_target_app_state",
    "external_app",
    "external_system",
    "system_chrome",
}

LOADING_BRIDGE_REASONS = {"loading_wait", "transient_wait", "noop_retry"}


def row_is_unassigned(row: Dict[str, Any]) -> bool:
    return not row.get("pageId") and not row.get("skipReason")


def classify_bridge_steps(bridge: List[Dict[str, Any]], previous: Optional[Dict[str, Any]] = None) -> str:
    if previous and previous.get("actionClass") == "open_app":
        return "system_bridge"
    if not bridge:
        if previous and previous.get("actionClass") in {"swipe", "type", "wait", "open_app", "finished", "takeover"}:
            return "same_page_state_misclassified"
        return "direct"
    reasons = {
        str(row.get("skipReason", "") or "").strip()
        for row in bridge
        if str(row.get("skipReason", "") or "").strip()
    }
    action_classes = {
        str(row.get("actionClass", "") or action_class(row.get("action", ""))).strip()
        for row in bridge
    }
    if reasons & SYSTEM_BRIDGE_REASONS or "open_app" in action_classes or any(looks_like_phone_home(row) for row in bridge):
        return "system_bridge_not_skipped" if any(row_is_unassigned(row) for row in bridge) else "system_bridge"
    if reasons and reasons <= LOADING_BRIDGE_REASONS:
        return "loading_bridge"
    if not reasons and action_classes <= {"wait", "other"}:
        return "loading_bridge_unclosed"
    if any(row_is_unassigned(row) for row in bridge):
        return "unassigned_bridge_step"
    if previous and previous.get("actionClass") in {"swipe", "type"}:
        return "same_page_state_misclassified"
    return "mixed_bridge"


def next_labeled_row(
    trace_rows: List[Dict[str, Any]],
    start_index: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    bridge = []
    for row in trace_rows:
        if row["stepIndex"] <= start_index:
            continue
        if row.get("pageId"):
            return row, bridge
        bridge.append(row)
    return {}, bridge


def observed_transitions(
    traces: Dict[str, Dict[str, Any]],
    rows_by_key: Dict[str, Dict[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    observed: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for trace in traces.values():
        previous = None
        bridge: List[Dict[str, Any]] = []
        for step in sorted(trace["steps"], key=lambda s: s["step_index"]):
            row = rows_by_key.get(step["key"], {})
            if not row.get("pageId"):
                if previous:
                    bridge.append(row)
                continue
            if previous and previous.get("pageId") != row.get("pageId"):
                pair = (previous["pageId"], row["pageId"])
                bridge_class = classify_bridge_steps(bridge, previous)
                item = observed.setdefault(pair, {
                    "from": pair[0],
                    "to": pair[1],
                    "count": 0,
                    "examples": [],
                    "bridgeExamples": [],
                    "bridgeClasses": {},
                })
                item["count"] += 1
                item["bridgeClasses"][bridge_class] = item["bridgeClasses"].get(bridge_class, 0) + 1
                if len(item["examples"]) < 8:
                    item["examples"].append({
                        "fromStep": previous["key"],
                        "toStep": row["key"],
                        "action": previous.get("action", ""),
                        "bridgeClass": bridge_class,
                    })
                if bridge and len(item["bridgeExamples"]) < 8:
                    item["bridgeExamples"].append({
                        "fromStep": previous["key"],
                        "toStep": row["key"],
                        "bridgeClass": bridge_class,
                        "bridgeSteps": [compact_step(b) for b in bridge],
                    })
            previous = row
            bridge = []
    return observed


def compact_step(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": row.get("key", ""),
        "stepIndex": row.get("stepIndex", -1),
        "pageId": row.get("pageId", ""),
        "materializedPage": row.get("materializedPage", {}),
        "componentBinding": row.get("componentBinding", {}),
        "skipReason": row.get("skipReason", ""),
        "action": row.get("action", ""),
        "rawAction": row.get("rawAction", ""),
        "screenshot": row.get("screenshot", ""),
        "feedbackFixed": bool(row.get("feedbackFixed")),
        "observation": row.get("observation", {}),
    }


def operation_action_class(operation: Any) -> str:
    op = str(operation or "").strip().lower()
    if op in {"click", "tap", "long_press", "press"}:
        return "click"
    if op in {"fill", "type", "input", "enter_text"}:
        return "type"
    if op in {"swipe", "scroll"}:
        return "swipe"
    if op in {"wait", "sleep"}:
        return "wait"
    return action_class(op)


def route_trigger_matches_evidence_action(route_trigger: str, row: Dict[str, Any]) -> Tuple[bool, str]:
    trigger = str(route_trigger or "").strip()
    if not trigger:
        return True, "missing_trigger"
    row_actions = {
        str(row.get("action", "")).strip(),
        str(row.get("rawAction", "")).strip(),
    }
    if trigger in row_actions:
        return True, "exact_text"

    trigger_class = action_class(trigger)
    if trigger_class == "other":
        return True, "unclassified_trigger"

    binding = row.get("componentBinding", {})
    if isinstance(binding, dict) and str(binding.get("status", "") or "") == "resolved":
        binding_class = operation_action_class(binding.get("operation"))
        if binding_class != "other":
            return (trigger_class == binding_class, "component_binding_action_class")

    row_classes = {
        str(row.get("actionClass", "") or "").strip(),
        action_class(row.get("action", "")),
        action_class(row.get("rawAction", "")),
    }
    if trigger_class in row_classes:
        return True, "action_class"
    return False, "trigger_semantic_mismatch"


def parse_step_key(key: str) -> Tuple[str, int]:
    if ":" not in key:
        return "", -1
    trace_id, step = key.rsplit(":", 1)
    try:
        return trace_id, int(step)
    except Exception:
        return trace_id, -1


def validate_routes(
    gr: Dict[str, Any],
    traces: Dict[str, Dict[str, Any]],
    rows_by_key: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], List[Dict[str, Any]]]]:
    trace_rows = {
        trace_id: [rows_by_key[s["key"]] for s in sorted(trace["steps"], key=lambda x: x["step_index"]) if s["key"] in rows_by_key]
        for trace_id, trace in traces.items()
    }
    invalid = []
    routes_by_pair: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for idx, route in enumerate(gr.get("canonical_routes", [])):
        if not isinstance(route, dict):
            continue
        route_id = "route_{}".format(idx)
        from_id = str(route.get("from_page_cluster_id", "") or "")
        to_id = str(route.get("to_page_cluster_id", "") or "")
        evidence_steps = route.get("evidence_steps", [])
        if not isinstance(evidence_steps, list):
            evidence_steps = []
        route_item = dict(route)
        route_item["id"] = route_id
        route_item["rawTrigger"] = str(route.get("trigger", "") or "")
        route_item["displayTrigger"] = route_item["rawTrigger"]
        route_item["actionClass"] = action_class(route_item["displayTrigger"])
        route_item["evidenceTriplets"] = []
        routes_by_pair[(from_id, to_id)].append(route_item)
        if not evidence_steps:
            invalid.append({
                "routeId": route_id,
                "from": from_id,
                "to": to_id,
                "reason": "missing_evidence_steps",
                "severity": "warn",
            })
            continue
        for step_key in evidence_steps:
            row = rows_by_key.get(str(step_key), {})
            if not row:
                invalid.append({
                    "routeId": route_id,
                    "from": from_id,
                    "to": to_id,
                    "evidenceStep": str(step_key),
                    "reason": "evidence_step_not_found",
                    "severity": "fail",
                })
                continue
            trace_id, step_index = parse_step_key(str(step_key))
            next_row, bridge = next_labeled_row(trace_rows.get(trace_id, []), step_index)
            checks = []
            if row.get("pageId") != from_id:
                checks.append("from_label_mismatch")
            if next_row and next_row.get("pageId") != to_id:
                checks.append("to_label_mismatch")
            if not next_row:
                checks.append("missing_next_labeled_step")
            route_trigger = str(route.get("trigger", "")).strip()
            trigger_matches, trigger_check = route_trigger_matches_evidence_action(route_trigger, row)
            route_item["triggerCheck"] = trigger_check
            if not trigger_matches:
                checks.append("trigger_semantic_mismatch")
            if row.get("action") and route_item["displayTrigger"] == route_item["rawTrigger"]:
                route_item["displayTrigger"] = str(row.get("action", "")).strip()
                route_item["actionClass"] = action_class(route_item["displayTrigger"])
            if checks:
                invalid.append({
                    "routeId": route_id,
                    "from": from_id,
                    "to": to_id,
                    "evidenceStep": str(step_key),
                    "reason": ",".join(checks),
                    "severity": "fail",
                })
            route_item["evidenceTriplets"].append({
                "fromStep": compact_step(row),
                "bridgeSteps": [compact_step(b) for b in bridge],
                "toStep": compact_step(next_row) if next_row else {},
                "checks": checks,
            })
    return invalid, routes_by_pair


def build_nodes(
    gr: Dict[str, Any],
    step_rows: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    labels_by_page: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in step_rows:
        if row.get("pageId"):
            labels_by_page[row["pageId"]].append(row)
    nodes: Dict[str, Dict[str, Any]] = {}
    for page in gr.get("canonical_pages", []):
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("page_cluster_id", "") or "")
        if not page_id:
            continue
        rows = labels_by_page.get(page_id, [])
        sig_counts = Counter(signature(row) for row in rows)
        traces = sorted(set(row["traceId"] for row in rows))
        examples = [compact_step(row) for row in rows]
        nodes[page_id] = {
            "id": page_id,
            "name": str(page.get("page_name", "") or page_id),
            "description": str(page.get("description", "") or ""),
            "routePath": str(page.get("route_path", "") or ""),
            "parentRoutePath": str(page.get("parent_route_path", "") or ""),
            "routeLevel": page.get("route_level", 0),
            "domain": route_domain(page),
            "color": color_for(page_id),
            "stepCount": len(rows),
            "traceCount": len(traces),
            "signatures": [{"signature": k, "count": v} for k, v in sig_counts.most_common()],
            "examples": examples,
        }
    return nodes


def layout_nodes(nodes: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]]) -> None:
    outgoing: Dict[str, List[str]] = defaultdict(list)
    for edge in edges:
        if edge["from"] and edge["to"]:
            outgoing[edge["from"]].append(edge["to"])
    root = "c_meituan_home" if "c_meituan_home" in nodes else ""
    if not root:
        root = "c_waimai_home" if "c_waimai_home" in nodes else ""
    if not root and nodes:
        root = sorted(nodes)[0]

    levels: Dict[str, int] = {}
    if root:
        levels[root] = 0
        queue = deque([root])
        while queue:
            current = queue.popleft()
            for target in outgoing.get(current, []):
                if target not in nodes:
                    continue
                next_level = min(6, levels[current] + 1)
                if target not in levels or next_level < levels[target]:
                    levels[target] = next_level
                    queue.append(target)
    for node_id in nodes:
        levels.setdefault(node_id, min(6, int(nodes[node_id].get("routeLevel", 1) or 1)))

    by_level: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for node in nodes.values():
        by_level[levels.get(node["id"], 1)].append(node)
    for level, items in by_level.items():
        items.sort(key=lambda n: (n.get("domain", ""), n.get("routePath", ""), n.get("name", "")))
        for idx, node in enumerate(items):
            node["x"] = 80 + level * 310
            node["y"] = 80 + idx * 86
            node["level"] = level


def edge_id(from_id: str, to_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", "{}__{}".format(from_id, to_id))
    return safe.strip("_")


def build_edges(
    nodes: Dict[str, Dict[str, Any]],
    observed: Dict[Tuple[str, str], Dict[str, Any]],
    routes_by_pair: Dict[Tuple[str, str], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    pairs = set(observed.keys()) | set(routes_by_pair.keys())
    edges = []
    for from_id, to_id in sorted(pairs):
        routes = routes_by_pair.get((from_id, to_id), [])
        observed_item = observed.get((from_id, to_id), {})
        if from_id not in nodes or to_id not in nodes:
            continue
        classes = Counter(route.get("actionClass", "other") for route in routes)
        kind = "canonical"
        if not routes and observed_item:
            bridge_classes = Counter(observed_item.get("bridgeClasses", {}) or {})
            dominant_bridge_class = bridge_classes.most_common(1)[0][0] if bridge_classes else "direct"
            if dominant_bridge_class == "loading_bridge":
                kind = "missing_observed"
            elif dominant_bridge_class in {
                "loading_bridge_unclosed",
                "unassigned_bridge_step",
                "system_bridge",
                "system_bridge_not_skipped",
                "same_page_state_misclassified",
                "mixed_bridge",
            }:
                kind = dominant_bridge_class
            else:
                kind = "missing_observed"
        elif routes and not observed_item:
            kind = "extra_unobserved"
        label = " / ".join("{} x{}".format(k, v) for k, v in classes.most_common()) if classes else "缺失"
        edges.append({
            "id": edge_id(from_id, to_id),
            "from": from_id,
            "to": to_id,
            "kind": kind,
            "label": label,
            "routeCount": len(routes),
            "observedCount": observed_item.get("count", 0),
            "routes": routes,
            "observed": observed_item,
        })
    return edges


def build_trace_flows(
    traces: Dict[str, Dict[str, Any]],
    rows_by_key: Dict[str, Dict[str, Any]],
    edge_ids: set,
) -> List[Dict[str, Any]]:
    trace_flows: List[Dict[str, Any]] = []
    for trace_id, trace in sorted(traces.items()):
        rows = [
            rows_by_key[step["key"]]
            for step in sorted(trace["steps"], key=lambda s: s["step_index"])
            if step["key"] in rows_by_key
        ]
        nodes = []
        edges = []
        transitions = []
        previous = None
        bridge: List[Dict[str, Any]] = []
        labeled_count = 0
        skipped_count = 0
        for row in rows:
            if row.get("pageId"):
                labeled_count += 1
                if row["pageId"] not in nodes:
                    nodes.append(row["pageId"])
                if previous and previous.get("pageId") != row.get("pageId"):
                    eid = edge_id(previous["pageId"], row["pageId"])
                    bridge_class = classify_bridge_steps(bridge, previous)
                    if eid in edge_ids and eid not in edges:
                        edges.append(eid)
                    transition_index = len(transitions)
                    transitions.append({
                        "traceId": trace_id,
                        "index": transition_index,
                        "from": previous["pageId"],
                        "to": row["pageId"],
                        "fromMaterializedPage": previous.get("materializedPage", {}),
                        "toMaterializedPage": row.get("materializedPage", {}),
                        "edgeId": eid,
                        "fromStep": previous["key"],
                        "fromStepIndex": previous.get("stepIndex", -1),
                        "toStep": row["key"],
                        "toStepIndex": row.get("stepIndex", -1),
                        "action": previous.get("action", ""),
                        "bridgeClass": bridge_class,
                        "bridgeSteps": [compact_step(item) for item in bridge],
                    })
                previous = row
                bridge = []
                continue
            if row.get("skipReason"):
                skipped_count += 1
            if previous:
                bridge.append(row)
        trace_flows.append({
            "id": trace_id,
            "task": trace.get("task", ""),
            "stepCount": len(rows),
            "labeledCount": labeled_count,
            "skippedCount": skipped_count,
            "nodeIds": nodes,
            "edgeIds": edges,
            "transitions": transitions,
            "steps": [compact_step(row) for row in rows],
            "labeledSteps": [compact_step(row) for row in rows if row.get("pageId")],
        })
    return trace_flows


def build_trace_transition_targets(trace_flows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    edge_targets: Dict[str, Dict[str, Any]] = {}
    step_targets: Dict[str, Dict[str, Any]] = {}
    for trace in trace_flows:
        trace_id = trace.get("id", "")
        for transition_index, transition in enumerate(trace.get("transitions", [])):
            target = {
                "kind": "traceTransition",
                "traceId": trace_id,
                "transitionIndex": transition.get("index", transition_index),
                "edgeId": transition.get("edgeId", ""),
            }
            if target["edgeId"]:
                edge_targets.setdefault(target["edgeId"], target)
            for bridge_step in transition.get("bridgeSteps", []):
                if bridge_step.get("key"):
                    step_targets.setdefault(bridge_step["key"], target)
    return {"edges": edge_targets, "steps": step_targets}


def build_issues(
    nodes: Dict[str, Dict[str, Any]],
    edges: List[Dict[str, Any]],
    invalid_routes: List[Dict[str, Any]],
    step_rows: List[Dict[str, Any]],
    trace_transition_targets: Dict[str, Dict[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    issues = []

    def add(issue_type: str, severity: str, title: str, target: Dict[str, Any], detail: str = "") -> None:
        issues.append({
            "id": "issue_{}".format(len(issues)),
            "type": issue_type,
            "severity": severity,
            "title": title,
            "target": target,
            "detail": detail,
        })

    for edge in edges:
        if edge["kind"] == "missing_observed":
            target = trace_transition_targets["edges"].get(edge["id"], {"kind": "edge", "id": edge["id"]})
            add(
                "missing_observed_edge",
                "fail",
                "{} -> {} 在 trace 中出现，但 canonical_routes 缺少这条边".format(edge["from"], edge["to"]),
                target,
                "先打开边证据，检查中间是否有误跳过的桥接 step，再接受这张图。",
            )
        elif edge["kind"] == "loading_bridge_unclosed":
            target = trace_transition_targets["edges"].get(edge["id"], {"kind": "edge", "id": edge["id"]})
            add(
                "loading_bridge_unclosed",
                "fail",
                "{} -> {} 经过未显式标注的 loading/wait 桥接帧，canonical_routes 未闭合".format(edge["from"], edge["to"]),
                target,
                "这通常说明 global reasoning 没有把 wait/loading step 标成 loading_wait，或 trace transition closure 没有补 route。",
            )
        elif edge["kind"] == "unassigned_bridge_step":
            target = trace_transition_targets["edges"].get(edge["id"], {"kind": "edge", "id": edge["id"]})
            add(
                "unassigned_bridge_step",
                "fail",
                "{} -> {} 中间存在既未 assign 也未 skip 的 step".format(edge["from"], edge["to"]),
                target,
                "先修 global reasoning step coverage；不要把未分类 bridge 直接当 canonical route。",
            )
        elif edge["kind"] == "system_bridge_not_skipped":
            target = trace_transition_targets["edges"].get(edge["id"], {"kind": "edge", "id": edge["id"]})
            add(
                "system_bridge_not_skipped",
                "fail",
                "{} -> {} 经过系统/launcher 桥接帧，不能作为页面内 route".format(edge["from"], edge["to"]),
                target,
                "该 bridge 应该标成 phone_home/system_launcher 并切断 observed route。",
            )
        elif edge["kind"] == "same_page_state_misclassified":
            target = trace_transition_targets["edges"].get(edge["id"], {"kind": "edge", "id": edge["id"]})
            add(
                "same_page_state_misclassified",
                "warn",
                "{} -> {} 更像 runtime state 变化，不应直接当 route".format(edge["from"], edge["to"]),
                target,
                "检查 scroll/type/wheel/overlay 是否被拆成了过细的页面身份。",
            )
        elif edge["kind"] == "system_bridge":
            target = trace_transition_targets["edges"].get(edge["id"], {"kind": "edge", "id": edge["id"]})
            add(
                "system_bridge",
                "warn",
                "{} -> {} 跨过系统 skip，不计为页面内缺失 route".format(edge["from"], edge["to"]),
                target,
                "这条边应在 route closure 中被切断，除非它代表真实 app 内导航。",
            )
        elif edge["kind"] == "extra_unobserved":
            add(
                "extra_unobserved_edge",
                "warn",
                "{} -> {} 没有对应的 trace 观测跳转".format(edge["from"], edge["to"]),
                {"kind": "edge", "id": edge["id"]},
                "只有在明确接受推断边时才可以保留。",
            )

    for row in step_rows:
        if is_app_like_skipped(row):
            target = trace_transition_targets["steps"].get(row["key"], {"kind": "step", "key": row["key"]})
            add(
                "suspicious_skip",
                "fail",
                "{} 被跳过为 phone_home，但截图/结构看起来像 app 内页面".format(row["key"]),
                target,
                clip((row.get("observation") or {}).get("description", ""), 260),
            )

    for item in invalid_routes:
        add(
            "invalid_route_evidence",
            item.get("severity", "fail"),
            "{} 的 evidence {} 校验失败：{}".format(item.get("routeId", ""), item.get("evidenceStep", ""), item.get("reason", "")),
            {"kind": "route", "id": item.get("routeId", ""), "edge": edge_id(item.get("from", ""), item.get("to", ""))},
        )

    outgoing = Counter(edge["from"] for edge in edges if edge.get("kind") == "canonical")
    for edge in edges:
        if outgoing.get(edge["from"], 0) >= 8 and edge.get("routeCount", 0) <= 1:
            add(
                "high_fanout_low_evidence",
                "review",
                "{} 是高 fan-out 节点，但该出边只有 1 条 route evidence".format(edge["from"]),
                {"kind": "edge", "id": edge["id"]},
            )

    for node in nodes.values():
        sigs = node.get("signatures", [])
        if node.get("stepCount", 0) >= 4 and len(sigs) >= 4:
            add(
                "high_entropy_cluster",
                "review",
                "{} 包含 {} 种 visual signature".format(node["id"], len(sigs)),
                {"kind": "node", "id": node["id"]},
                "检查这个 page cluster 是否应该拆分。",
            )

    order = {"fail": 0, "warn": 1, "review": 2, "info": 3}
    return sorted(issues, key=lambda x: (order.get(x["severity"], 9), x["type"], x["title"]))


def build_audit(
    output_dir: Path,
    trace_root: Path,
    html_dir: Path,
    global_reasoning_path: Optional[Path] = None,
    *,
    global_reasoning_payload: Optional[Dict[str, Any]] = None,
    global_reasoning_label: str = "",
) -> Dict[str, Any]:
    if global_reasoning_payload is not None:
        gr = global_reasoning_payload
    else:
        if global_reasoning_path is None:
            raise ValueError("global_reasoning_path is required when global_reasoning_payload is not provided")
        gr = load_json(global_reasoning_path, {}) or {}
    reasoning_ref = global_reasoning_label or (str(global_reasoning_path) if global_reasoning_path else "<in_memory>")
    traces = load_traces(trace_root)
    observations = load_observations(output_dir)
    materialized_bindings = load_materialized_observation_bindings(output_dir)
    step_component_bindings = load_step_component_bindings(output_dir)
    step_rows, rows_by_key = build_step_rows(
        traces,
        gr,
        observations,
        html_dir,
        materialized_bindings=materialized_bindings,
        step_component_bindings=step_component_bindings,
    )
    nodes = build_nodes(gr, step_rows)
    observed = observed_transitions(traces, rows_by_key)
    invalid_routes, routes_by_pair = validate_routes(gr, traces, rows_by_key)
    edges = build_edges(nodes, observed, routes_by_pair)
    layout_nodes(nodes, edges)
    trace_flows = build_trace_flows(traces, rows_by_key, {edge["id"] for edge in edges})
    trace_transition_targets = build_trace_transition_targets(trace_flows)
    issues = build_issues(nodes, edges, invalid_routes, step_rows, trace_transition_targets)
    instance_audit = build_instance_audit(output_dir, html_dir, gr, step_rows=step_rows)

    stats = {
        "traces": len(traces),
        "steps": len(step_rows),
        "pages": len(nodes),
        "canonicalRoutes": len(gr.get("canonical_routes", [])),
        "graphEdges": len(edges),
        "issues": len(issues),
        "failIssues": sum(1 for x in issues if x["severity"] == "fail"),
        "missingObservedEdges": sum(1 for x in edges if x["kind"] == "missing_observed"),
        "loadingBridgeUnclosed": sum(1 for x in edges if x["kind"] == "loading_bridge_unclosed"),
        "unassignedBridgeSteps": sum(1 for x in edges if x["kind"] == "unassigned_bridge_step"),
        "systemBridgeNotSkipped": sum(1 for x in edges if x["kind"] == "system_bridge_not_skipped"),
        "samePageStateMisclassified": sum(1 for x in edges if x["kind"] == "same_page_state_misclassified"),
        "extraUnobservedEdges": sum(1 for x in edges if x["kind"] == "extra_unobserved"),
        "suspiciousSkips": sum(1 for x in issues if x["type"] == "suspicious_skip"),
        "invalidEvidence": len(invalid_routes),
        "materializedInstances": instance_audit.get("summary", {}).get("materializedPages", 0),
        "splitClusters": instance_audit.get("summary", {}).get("splitClusters", 0),
        "suspiciousInstanceSplits": instance_audit.get("summary", {}).get("suspiciousSplits", 0),
    }
    return {
        "meta": {
            "outputDir": _short_path(output_dir),
            "traceRoot": _short_path(trace_root),
            "globalReasoning": rel_path(reasoning_ref, html_dir) if reasoning_ref != "<in_memory>" else reasoning_ref,
        },
        "stats": stats,
        "nodes": list(nodes.values()),
        "edges": edges,
        "traceFlows": trace_flows,
        "issues": issues,
        "stepsByKey": {row["key"]: row for row in step_rows},
        "instanceAudit": instance_audit,
    }


def json_for_html(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>路由图人工核验</title>
<style>
* { box-sizing: border-box; }
:root {
  --left-width: 340px;
  --right-width: 430px;
  --film-height: 180px;
  --resizer-size: 7px;
}
body { margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f4f6f8; color: #111827; }
body.dragging-layout, body.panning-graph { user-select: none; -webkit-user-select: none; }
body.dragging-layout * , body.panning-graph * { user-select: none !important; -webkit-user-select: none !important; }
body.dragging-layout { cursor: col-resize; }
body.dragging-layout.resizing-row { cursor: row-resize; }
body.panning-graph { cursor: grabbing; }
button, input, select { font: inherit; }
button { border: 1px solid #cbd5e1; background: #fff; border-radius: 7px; padding: 7px 10px; cursor: pointer; }
button:hover { background: #f8fafc; }
body.queue-collapsed { --left-width: 44px; }
body.queue-collapsed .queue-head > :not(.queue-title-row),
body.queue-collapsed .issue-list,
body.queue-collapsed #leftResizer { display: none; }
body.queue-collapsed .queue { overflow: hidden; }
body.queue-collapsed .queue-title-text { writing-mode: vertical-rl; text-orientation: mixed; font-size: 12px; }
body.queue-collapsed .queue-title-row { flex-direction: column; align-items: center; gap: 8px; }
.app { height: 100vh; display: grid; grid-template-columns: var(--left-width) var(--resizer-size) minmax(520px, 1fr) var(--resizer-size) var(--right-width); grid-template-rows: minmax(58px, auto) minmax(260px, 1fr) var(--resizer-size) var(--film-height); gap: 0; min-width: 0; }
.topbar { grid-column: 1 / 6; display: flex; align-items: center; gap: 12px; padding: 10px 14px; border-bottom: 1px solid #dbe3ef; background: #fff; flex-wrap: wrap; min-width: 0; }
.topbar h1 { margin: 0; font-size: 18px; flex: 0 0 auto; }
.topbar .meta { flex: 1 1 280px; min-width: 180px; color: #64748b; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.top-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex: 1 1 auto; min-width: 260px; flex-wrap: wrap; }
.save-status { color: #64748b; font-size: 12px; white-space: nowrap; max-width: min(520px, 100%); overflow: hidden; text-overflow: ellipsis; }
.save-status.dirty { color: #b45309; font-weight: 700; }
.save-status.saved { color: #047857; font-weight: 700; }
.queue { grid-column: 1; grid-row: 2 / 5; border-right: 1px solid #dbe3ef; background: #fff; min-width: 0; overflow: hidden; display: flex; flex-direction: column; }
.queue-head { padding: 12px; border-bottom: 1px solid #e5e7eb; }
.queue-title-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.queue-toggle { width: 30px; height: 30px; padding: 0; display: inline-flex; align-items: center; justify-content: center; font-weight: 800; }
.stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 10px; }
.stat { border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px; background: #fbfdff; }
.stat strong { display: block; font-size: 18px; }
.stat span { color: #64748b; font-size: 11px; }
.filters { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }
.issue-list { overflow: auto; padding: 8px; }
.issue { width: 100%; text-align: left; display: block; padding: 9px; margin: 0 0 8px; border-radius: 8px; border: 1px solid #e5e7eb; background: #fff; }
.issue.fail { border-color: #fecaca; background: #fff7f7; }
.issue.warn { border-color: #fed7aa; background: #fff9f2; }
.issue.review { border-color: #bfdbfe; background: #f8fbff; }
.issue.active { outline: 2px solid #2563eb; }
.issue-title { font-size: 12px; line-height: 1.35; font-weight: 700; }
.issue-type { margin-top: 6px; color: #64748b; font-size: 11px; }
.graph-wrap { grid-column: 3; grid-row: 2; position: relative; min-width: 0; min-height: 0; overflow: hidden; background: #eef2f7; user-select: none; -webkit-user-select: none; display: grid; grid-template-rows: auto minmax(0, 1fr); }
.graph-tools { grid-row: 1; z-index: 5; min-width: 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; background: #fff; border-bottom: 1px solid #dbe3ef; padding: 8px 10px; box-shadow: 0 3px 10px rgba(15,23,42,.06); }
.graph-tools input { flex: 1 1 180px; width: 100%; min-width: 0; border: 1px solid #cbd5e1; border-radius: 7px; padding: 7px 9px; }
.graph-tools select { flex: 1 1 220px; width: 100%; min-width: 0; max-width: 360px; border: 1px solid #cbd5e1; border-radius: 7px; padding: 7px 9px; background: #fff; }
.graph-tools button { flex: 0 0 auto; }
#graph { grid-row: 2; width: 100%; height: 100%; min-height: 0; display: block; user-select: none; -webkit-user-select: none; touch-action: none; cursor: grab; }
#graph:active { cursor: grabbing; }
.edge { fill: none; stroke: #64748b; stroke-width: 2.1; opacity: .58; cursor: pointer; }
.edge.missing_observed,
.edge.loading_bridge_unclosed,
.edge.unassigned_bridge_step,
.edge.system_bridge_not_skipped { stroke: #dc2626; stroke-dasharray: 8 5; opacity: .9; }
.edge.system_bridge,
.edge.same_page_state_misclassified,
.edge.mixed_bridge { stroke: #d97706; stroke-dasharray: 5 5; }
.edge.extra_unobserved { stroke: #d97706; stroke-dasharray: 5 5; }
.edge.selected, .edge.hovered { stroke-width: 4; opacity: 1; }
.edge-label { pointer-events: none; font-size: 11px; fill: #334155; opacity: 0; paint-order: stroke; stroke: #fff; stroke-width: 4px; }
.edge-label.visible { opacity: 1; }
.trace-step-label { pointer-events: none; font-size: 12px; font-weight: 900; fill: #b91c1c; paint-order: stroke; stroke: #fff7ed; stroke-width: 4px; }
.node { cursor: pointer; }
.node rect { fill: #fff; stroke-width: 2; rx: 8; filter: drop-shadow(0 2px 5px rgba(15,23,42,.12)); }
.node text { font-size: 11px; fill: #111827; pointer-events: none; user-select: none; -webkit-user-select: none; }
.node .route { fill: #64748b; font-size: 9px; }
.node .trace-action.inter-page { fill: #7c2d12; font-size: 9px; font-weight: 800; }
.node .trace-action.intra-page { fill: #1d4ed8; font-size: 9px; font-weight: 700; }
.node.selected rect, .node.hovered rect { stroke-width: 4; }
.node.search-dim, .edge.search-dim, .node.trace-dim, .edge.trace-dim,
.instance-graph-node.search-dim, .instance-graph-edge.search-dim { opacity: .18; }
body.trace-layout-mode .node.trace-dim,
body.trace-layout-mode .edge.trace-dim,
body.trace-layout-mode .edge-label:not(.visible) { display: none; }
.node.trace-active rect { stroke-width: 4; }
.edge.trace-active { stroke-width: 4; opacity: .96; }
.hierarchy-band { stroke-width: 1.5; rx: 10; }
.hierarchy-band.lane-waimai { fill: #eef6ff; stroke: #93c5fd; }
.hierarchy-band.lane-medical { fill: #f0fdf4; stroke: #86efac; }
.hierarchy-band.lane-grocery { fill: #fff7ed; stroke: #fdba74; }
.hierarchy-band.lane-ridehail { fill: #f5f3ff; stroke: #c4b5fd; }
.hierarchy-band.lane-utility { fill: #f1f5f9; stroke: #94a3b8; }
.hierarchy-band.lane-other { fill: #f8fafc; stroke: #cbd5e1; }
.hierarchy-label { font-size: 13px; font-weight: 850; fill: #334155; pointer-events: none; paint-order: stroke; stroke: #f8fafc; stroke-width: 4px; }
.depth-guide { stroke: #cbd5e1; stroke-width: 1; stroke-dasharray: 3 7; opacity: .6; }
.depth-label { font-size: 12px; font-weight: 850; fill: #475569; pointer-events: none; paint-order: stroke; stroke: #eef2f7; stroke-width: 4px; }
.instance-source-band { stroke-width: 1.8; stroke-dasharray: 8 5; rx: 12; }
.instance-source-label { font-size: 12px; font-weight: 850; fill: #334155; pointer-events: none; paint-order: stroke; stroke: #fff; stroke-width: 4px; }
.instance-level-guide { stroke: #cbd5e1; stroke-width: 1; stroke-dasharray: 3 7; opacity: .72; }
.instance-level-label { font-size: 12px; font-weight: 850; fill: #475569; pointer-events: none; paint-order: stroke; stroke: #eef2f7; stroke-width: 4px; }
.instance-graph-edge { fill: none; stroke: #64748b; stroke-width: 2.2; opacity: .68; cursor: pointer; }
.instance-graph-edge.low-count { stroke-width: 1.4; opacity: .18; }
.instance-graph-edge.low-count.trace-active,
.instance-graph-edge.low-count.selected,
.instance-graph-edge.low-count.hovered { stroke-width: 3.4; opacity: .95; }
.instance-graph-edge.trace-active, .instance-graph-edge.selected { opacity: .98; stroke-width: 4; stroke-dasharray: none; }
.instance-graph-edge.trace-dim { opacity: .08; }
.instance-graph-label { font-size: 10px; fill: #475569; opacity: 0; paint-order: stroke; stroke: #fff; stroke-width: 4px; pointer-events: none; }
.instance-graph-label.visible { opacity: .84; }
.instance-graph-node { cursor: pointer; }
.instance-graph-node rect { fill: #ecfeff; stroke: #0891b2; stroke-width: 1.8; rx: 9; filter: drop-shadow(0 2px 6px rgba(15,23,42,.12)); }
.instance-graph-node text { font-size: 10px; fill: #0f172a; pointer-events: none; user-select: none; -webkit-user-select: none; }
.instance-graph-node .route { fill: #64748b; font-size: 9px; }
.instance-graph-node.suspicious rect { fill: #fff7ed; stroke: #f97316; }
.instance-graph-node.selected rect, .instance-graph-node.hovered rect { stroke-width: 4; }
.instance-graph-node.trace-dim { opacity: .18; }
.instance-graph-node.trace-active rect { stroke-width: 4; }
.instance-controls { display: none; flex: 1 0 100%; align-items: center; gap: 8px; flex-wrap: wrap; width: 100%; min-width: 0; box-sizing: border-box; }
body.instance-graph-mode .instance-controls { display: flex; }
.instance-controls label { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; white-space: nowrap; color: #334155; }
.instance-controls select { flex: 0 0 132px; width: 132px; max-width: 132px; }
.instance-controls input[type="checkbox"] { margin: 0; }
.graph-tools button.mode-active { background: #dbeafe; border-color: #2563eb; color: #1d4ed8; font-weight: 800; }
.inspector { grid-column: 5; grid-row: 2 / 5; border-left: 1px solid #dbe3ef; background: #fff; min-width: 0; overflow: auto; }
.panel { padding: 14px; border-bottom: 1px solid #e5e7eb; }
.panel h2 { margin: 0 0 8px; font-size: 17px; }
.panel h3 { margin: 16px 0 8px; font-size: 13px; }
.muted { color: #64748b; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .92em; }
.pill { display: inline-block; margin: 3px 5px 3px 0; border-radius: 999px; padding: 3px 7px; font-size: 11px; background: #e0f2fe; color: #075985; }
.pill.fail { background: #fee2e2; color: #991b1b; }
.pill.warn { background: #fef3c7; color: #92400e; }
.kv { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 6px 8px; font-size: 12px; }
.kv > div:nth-child(odd) { color: #64748b; }
.entity-counts { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 5px; }
.entity-chip { display: inline-block; border-radius: 999px; padding: 2px 6px; font-size: 10px; background: #ecfeff; color: #155e75; }
.instance-group { border: 1px solid #e5e7eb; border-radius: 10px; margin: 10px 0; background: #fbfdff; overflow: hidden; }
.instance-group.warn { border-color: #fed7aa; background: #fff7ed; }
.instance-group-head { padding: 10px; border-bottom: 1px solid #e5e7eb; }
.instance-list { padding: 8px; display: grid; gap: 8px; }
.instance-card { text-align: left; width: 100%; border: 1px solid #dbe3ef; background: #fff; border-radius: 8px; padding: 9px; }
.instance-card:hover { border-color: #2563eb; }
.instance-card.active { outline: 2px solid #2563eb; }
.evidence-list { display: grid; gap: 8px; }
.evidence-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.shot { border: 1px solid #dbe3ef; border-radius: 8px; padding: 6px; background: #fbfdff; min-width: 0; }
.shot img { width: 100%; aspect-ratio: 9 / 16; object-fit: cover; object-position: top; border-radius: 6px; border: 1px solid #e5e7eb; background: #fff; }
.shot strong { display: block; margin-top: 5px; font-size: 11px; word-break: break-all; }
.shot div { font-size: 11px; color: #475569; line-height: 1.35; overflow-wrap: anywhere; }
.examples { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.step-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 7px; min-width: 0; }
.step-card.selected { border-color: #2563eb; box-shadow: 0 0 0 2px rgba(37,99,235,.18); }
.step-card img { width: 100%; aspect-ratio: 9 / 16; object-fit: cover; object-position: top; border-radius: 6px; border: 1px solid #e5e7eb; }
.page-binding { margin: 5px 0; padding: 6px 7px; border-radius: 7px; background: #f8fafc; border: 1px solid #e2e8f0; }
.page-binding .small { overflow-wrap: anywhere; }
.screenshot-frame { position: relative; width: 100%; border-radius: 7px; overflow: hidden; background: #fff; }
.screenshot-frame img { display: block; width: 100%; }
.screenshot-frame.is-skip { outline: 3px solid #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,.18); }
.skip-ribbon {
  position: absolute;
  top: 6px;
  left: 6px;
  z-index: 2;
  border-radius: 999px;
  padding: 3px 7px;
  background: rgba(146,64,14,.94);
  color: #fff;
  font-size: 10px;
  font-weight: 900;
  letter-spacing: .04em;
}
.skip-reason {
  position: absolute;
  left: 6px;
  right: 6px;
  bottom: 6px;
  z-index: 2;
  border-radius: 7px;
  padding: 4px 6px;
  background: rgba(255,251,235,.95);
  color: #92400e;
  font-size: 10px;
  font-weight: 800;
  line-height: 1.25;
  overflow-wrap: anywhere;
  box-shadow: 0 1px 4px rgba(15,23,42,.16);
}
.small { font-size: 11px; line-height: 1.35; overflow-wrap: anywhere; }
.filmstrip { grid-column: 3; grid-row: 4; border-top: 1px solid #dbe3ef; background: #fff; overflow-x: auto; overflow-y: hidden; padding: 8px 10px; }
.filmstrip h2 { margin: 0 0 6px; font-size: 13px; }
.film-items { display: flex; gap: 10px; align-items: flex-start; }
.film-item { height: calc(var(--film-height) - 38px); width: calc((var(--film-height) - 88px) * 0.5625 + 18px); min-width: 92px; flex: 0 0 auto; border: 1px solid #e5e7eb; border-radius: 8px; padding: 6px; background: #fbfdff; overflow: hidden; display: flex; flex-direction: column; }
.film-item.is-skip { border-color: #f59e0b; background: #fffbeb; box-shadow: inset 0 0 0 1px rgba(245,158,11,.22); }
.film-item img { width: 100%; height: calc(var(--film-height) - 88px); min-height: 72px; object-fit: contain; object-position: center; border-radius: 6px; background: #fff; }
.film-item .screenshot-frame { height: calc(var(--film-height) - 88px); min-height: 72px; }
.film-item .screenshot-frame img { height: 100%; }
.film-meta { display: flex; gap: 4px; flex-wrap: wrap; margin: 4px 0; align-items: center; }
.film-item .small { flex: 0 0 auto; max-height: 28px; overflow: hidden; }
.step-index-pill { display: inline-block; border-radius: 999px; padding: 2px 7px; font-size: 11px; font-weight: 800; background: #fee2e2; color: #991b1b; }
.resizer { background: #e5e7eb; z-index: 10; user-select: none; -webkit-user-select: none; touch-action: none; }
.resizer:hover, .resizer.active { background: #94a3b8; }
.resizer.vertical { cursor: col-resize; }
.resizer.horizontal { cursor: row-resize; }
#leftResizer { grid-column: 2; grid-row: 2 / 5; }
#rightResizer { grid-column: 4; grid-row: 2 / 5; }
#bottomResizer { grid-column: 3; grid-row: 3; }
.resize-hint { margin-left: auto; color: #64748b; font-size: 12px; }
.annotation-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.trace-note { margin-top: 12px; }
.trace-note label { display: block; margin-bottom: 6px; font-size: 12px; font-weight: 700; color: #334155; }
.trace-note textarea {
  width: 100%;
  min-height: 84px;
  resize: vertical;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 8px 9px;
  font: inherit;
  line-height: 1.4;
}
.trace-note textarea:focus { outline: 2px solid rgba(37,99,235,.25); border-color: #2563eb; }
.route-feedback-form { display: grid; gap: 8px; grid-template-columns: 1fr; margin: 12px 0; }
.route-feedback-form select,
.route-feedback-form textarea {
  width: 100%;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 8px 9px;
  font: inherit;
}
.route-feedback-form textarea { min-height: 72px; resize: vertical; }
.route-feedback-item { border: 1px solid #dbe3ef; border-radius: 8px; padding: 10px; margin-top: 8px; background: #f8fafc; }
.empty { color: #64748b; padding: 12px; }
@media (max-width: 1400px) {
  :root { --left-width: 280px; --right-width: 380px; --film-height: 160px; }
  .app { grid-template-columns: var(--left-width) var(--resizer-size) minmax(0, 1fr) var(--resizer-size) var(--right-width); grid-template-rows: minmax(58px, auto) minmax(420px, 1fr) var(--resizer-size) var(--film-height); }
  .top-actions button { padding: 6px 8px; }
  .evidence-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 1320px) {
  .app { grid-template-rows: minmax(88px, auto) minmax(420px, 1fr) var(--resizer-size) var(--film-height); }
  .topbar { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; row-gap: 8px; }
  .topbar .meta { min-width: 0; width: 100%; }
  .top-actions { grid-column: 1 / -1; width: 100%; min-width: 0; justify-content: flex-end; }
}
@media (max-width: 900px) {
  :root { --film-height: 220px; }
  .app { height: auto; min-height: 100vh; display: flex; flex-direction: column; overflow-x: hidden; }
  .topbar { display: grid; grid-template-columns: 1fr; align-items: start; }
  .topbar .meta { width: 100%; min-width: 0; }
  .top-actions { width: 100%; min-width: 0; justify-content: flex-start; }
  .save-status { width: 100%; max-width: 100%; }
  body.queue-collapsed .queue-title-text { writing-mode: initial; }
  .queue { min-height: 260px; max-height: 46vh; border-right: 0; border-bottom: 1px solid #dbe3ef; }
  .graph-wrap { height: min(58vh, 620px); min-height: 420px; width: 100%; }
  .inspector { border-left: 0; border-top: 1px solid #dbe3ef; max-height: none; }
  .filmstrip { min-height: var(--film-height); }
  .resizer { display: none; }
  .graph-tools { box-shadow: none; }
  .graph-tools input { flex: 1 1 180px; }
  .graph-tools select { flex: 1 1 180px; max-width: none; }
  .evidence-grid, .examples { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <h1>路由图人工核验</h1>
    <div class="meta">输出目录: <code>__OUTPUT_DIR__</code> · Trace 根目录: <code>__TRACE_ROOT__</code></div>
    <div class="top-actions">
      <span id="annotationSaveStatus" class="save-status">Feedback JSON 已同步</span>
      <button id="reloadServerAnnotationsBtn" type="button" style="display:none">从服务器读取 feedback</button>
      <button id="importAnnotationsBtn" type="button">读取本地 feedback JSON</button>
      <button id="saveAnnotationsBtn" type="button">保存 feedback JSON</button>
      <button id="acceptAuditedReasoningBtn" type="button" style="display:none">确认 audit 无问题</button>
      <button id="globalRouteFeedbackBtn" type="button">Global route feedback</button>
      <input id="annotationsFileInput" type="file" accept="application/json,.json" style="display:none">
    </div>
  </header>
  <aside class="queue">
    <div class="queue-head">
      <div class="queue-title-row">
        <strong class="queue-title-text">核验队列</strong>
        <button id="queueToggleBtn" class="queue-toggle" type="button" title="折叠/展开核验队列">‹</button>
      </div>
      <div class="stats" id="stats"></div>
      <div class="filters">
        <button data-filter="all">全部</button>
        <button data-filter="fail">失败</button>
        <button data-filter="warn">警告</button>
        <button data-filter="review">复核</button>
      </div>
    </div>
    <div class="issue-list" id="issueList"></div>
  </aside>
  <div class="resizer vertical" id="leftResizer" title="拖拽调整左侧栏宽度"></div>
  <main class="graph-wrap">
    <div class="graph-tools">
      <input id="search" placeholder="搜索页面、路由、边、动作">
      <select id="traceSelect"></select>
      <button id="clearTraceBtn">清除 Trace</button>
      <button id="routeGraphBtn">Global Route</button>
      <button id="instanceAuditBtn">Page→Instance</button>
      <button id="fitBtn">适配视图</button>
      <div id="instanceControls" class="instance-controls">
        <select id="instanceViewMode" title="Instance view mode">
          <option value="split">Split only</option>
          <option value="focus">Focus</option>
          <option value="all">All</option>
        </select>
        <label><input id="instanceDimLowEdges" type="checkbox" checked>弱化低频边</label>
        <label><input id="instanceShowLabels" type="checkbox">显示边标签</label>
      </div>
    </div>
    <svg id="graph" aria-label="路由图"></svg>
  </main>
  <div class="resizer vertical" id="rightResizer" title="拖拽调整右侧栏宽度"></div>
  <aside class="inspector" id="inspector"></aside>
  <div class="resizer horizontal" id="bottomResizer" title="拖拽调整 Trace 片段高度"></div>
  <section class="filmstrip">
    <h2>Trace 片段</h2>
    <div class="film-items" id="filmstrip"></div>
  </section>
</div>
<script type="application/json" id="audit-data">__DATA_JSON__</script>
<script type="application/json" id="annotation-data">__ANNOTATION_JSON__</script>
<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/split-grid@1.0.11/dist/split-grid.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/elkjs/lib/elk.bundled.js"></script>
<script>
const DATA = JSON.parse(document.getElementById('audit-data').textContent);
const nodeById = new Map(DATA.nodes.map(n => [n.id, n]));
const edgeById = new Map(DATA.edges.map(e => [e.id, e]));
const traceById = new Map((DATA.traceFlows || []).map(t => [t.id, t]));
const stepByKey = DATA.stepsByKey || {};
const instanceAudit = DATA.instanceAudit || { summary: {}, sourceClusters: [], instanceEdges: [] };
const instanceGroupByCluster = new Map((instanceAudit.sourceClusters || []).map(group => [group.clusterId, group]));
const instanceFlowByTrace = new Map((instanceAudit.traceInstanceFlows || []).map(flow => [flow.id, flow]));
const instanceEdgeById = new Map((instanceAudit.instanceEdges || []).map(edge => [edge.id, edge]));
const instanceById = new Map();
for (const group of instanceAudit.sourceClusters || []) {
  for (const instance of group.instances || []) {
    instanceById.set(instance.instanceId, { ...instance, group });
  }
}
const SEVERITY_LABELS = { fail: '失败', warn: '警告', review: '复核', info: '信息' };
const ISSUE_TYPE_LABELS = {
  missing_observed_edge: '缺失观测边',
  loading_bridge_unclosed: 'Loading 桥未闭合',
  unassigned_bridge_step: '未分类桥接 step',
  system_bridge_not_skipped: '系统桥未跳过',
  same_page_state_misclassified: '状态变化误作路由',
  system_bridge: '系统桥接边',
  extra_unobserved_edge: '未观测边',
  suspicious_skip: '可疑跳过',
  invalid_route_evidence: '无效 evidence',
  high_fanout_low_evidence: '高 fan-out 低证据',
  high_entropy_cluster: '高熵页面簇',
};
const EDGE_KIND_LABELS = {
  canonical: 'canonical route',
  missing_observed: '缺失观测边',
  loading_bridge_unclosed: 'Loading 桥未闭合',
  unassigned_bridge_step: '未分类桥接',
  system_bridge_not_skipped: '系统桥未跳过',
  system_bridge: '系统桥接',
  same_page_state_misclassified: '状态误作路由',
  mixed_bridge: '混合桥接',
  extra_unobserved: '未观测边',
};
const DEFAULT_ANNOTATION_LABELS = ['通过','误跳过','页面标注错','缺少路由','页面内状态','拆分页面簇','合并页面簇'];
const MISSING_OBSERVED_EDGE_LABELS = [
  '确认缺失：应补 canonical route',
  '非缺失：页面内状态/无需路由',
  '非缺失：页面聚类或标注错',
  '误跳过导致假边',
  '接受现状：不用补路由',
  '暂不确定',
];
const TRACE_TRANSITION_LABELS = [
  '该跳转正确',
  '确认缺路由：应补 canonical route',
  '非缺失：页面内状态/无需路由',
  '误跳过：skip 规则错',
  '页面标注错',
  '需拆分页面簇',
  '需合并页面簇',
  '暂不确定',
];
const LAYOUT_KEY = 'routeGraphAuditLayout';
const LAYOUT_VERSION = 2;
const MIN_ZOOM = 0.12;
const MAX_ZOOM = 32;
const FIT_TARGET_TEXT_PX = 14;
const WHEEL_ZOOM_SPEED = 0.005;
const WHEEL_LINE_HEIGHT_PX = 16;
const TRACE_ACTION_WRAP_WIDTH = 180;
const ANNOTATION_FILE_NAME = 'route_review_annotations.json';
const GLOBAL_ROUTE_FEEDBACK_KEY = 'global_route_feedback';
const SERVER_ANNOTATION_API = '__ANNOTATION_API__';
const SERVER_ACCEPT_AUDIT_API = '__ACCEPT_AUDIT_API__';
let annotations = JSON.parse(document.getElementById('annotation-data')?.textContent || '{}');
let annotationsDirty = false;
let serverSaveEnabled = SERVER_ANNOTATION_API !== '';
let serverAcceptAuditEnabled = SERVER_ACCEPT_AUDIT_API !== '';
let filter = 'all';
let selected = { kind: 'none', id: '' };
let activeTraceId = '';
let activeTransitionIndex = null;
let graphMode = 'route';
let instanceGraphOptions = {
  viewMode: 'split',
  dimLowEdges: true,
  showLabels: false,
};
let reviewContext = { mode: 'none' };
let contextStack = [];
let transform = { x: 40, y: 30, k: 0.75 };
let zoomBehavior = null;
let traceLayoutToken = 0;
let traceLayout = emptyTraceLayout();
let globalLayoutToken = 0;
let globalLayout = emptyGraphLayout();
let elk = null;

const svg = document.getElementById('graph');
const ns = 'http://www.w3.org/2000/svg';
const viewport = document.createElementNS(ns, 'g');
svg.appendChild(viewport);
const hierarchyLayer = document.createElementNS(ns, 'g');
hierarchyLayer.setAttribute('id', 'hierarchyLayer');
const traceStepLayer = document.createElementNS(ns, 'g');
traceStepLayer.setAttribute('id', 'traceStepLayer');
const instanceGraphLayer = document.createElementNS(ns, 'g');
instanceGraphLayer.setAttribute('id', 'instanceGraphLayer');
const instanceNodePositions = new Map();

function emptyTraceLayout() {
  return {
    traceId: '',
    transitionIndex: null,
    nodePositions: new Map(),
    edgeSections: new Map(),
    width: 0,
    height: 0,
  };
}

function emptyGraphLayout() {
  return {
    nodePositions: new Map(),
    edgeSections: new Map(),
    bands: [],
    depthGuides: [],
    width: 0,
    height: 0,
    ready: false,
  };
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function applyLayout(layout) {
  if (!layout) return;
  const root = document.documentElement;
  if (layout.leftWidth) root.style.setProperty('--left-width', `${layout.leftWidth}px`);
  if (layout.rightWidth) root.style.setProperty('--right-width', `${layout.rightWidth}px`);
  if (layout.filmHeight) root.style.setProperty('--film-height', `${layout.filmHeight}px`);
}

function readLayout() {
  try {
    const layout = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}');
    return layout?._version === LAYOUT_VERSION ? layout : {};
  } catch (err) {
    return {};
  }
}

function saveLayout(next) {
  const layout = { ...readLayout(), ...next, _version: LAYOUT_VERSION };
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  applyLayout(layout);
}

function gridPixels(prop) {
  const app = document.querySelector('.app');
  return getComputedStyle(app)[prop].split(/\\s+/).map(part => Number(part.replace('px', '')) || 0);
}

function syncLayoutVarsFromGrid() {
  const columns = gridPixels('gridTemplateColumns');
  const rows = gridPixels('gridTemplateRows');
  const mobile = window.matchMedia('(max-width: 900px)').matches;
  const next = {};
  if (columns[0]) next.leftWidth = Math.round(columns[0]);
  if (!mobile && columns[4]) next.rightWidth = Math.round(columns[4]);
  const filmIndex = mobile ? 5 : 3;
  if (rows[filmIndex]) next.filmHeight = Math.round(rows[filmIndex]);
  saveLayout(next);
}

function setupResizableLayout() {
  applyLayout(readLayout());
  if (!window.Split) {
    throw new Error('split-grid 1.0.11 is required for route graph audit layout resizing');
  }
  const app = document.querySelector('.app');
  const computed = getComputedStyle(app);
  app.style.gridTemplateColumns = computed.gridTemplateColumns;
  app.style.gridTemplateRows = computed.gridTemplateRows;
  const rowTrack = window.matchMedia('(max-width: 900px)').matches ? 4 : 2;
  window.Split({
    gridTemplateColumns: computed.gridTemplateColumns,
    gridTemplateRows: computed.gridTemplateRows,
    columnGutters: [
      { track: 1, element: document.getElementById('leftResizer') },
      { track: 3, element: document.getElementById('rightResizer') },
    ].filter(item => item.element),
    rowGutters: [
      { track: rowTrack, element: document.getElementById('bottomResizer') },
    ].filter(item => item.element),
    columnMinSizes: { 0: 240, 2: 420, 4: 320 },
    rowMinSizes: { 1: 320, 3: 130, 5: 130 },
    snapOffset: 0,
    onDragStart(direction) {
      window.getSelection()?.removeAllRanges();
      document.body.classList.add('dragging-layout');
      document.body.classList.toggle('resizing-row', direction === 'row');
    },
    onDrag() {
      syncLayoutVarsFromGrid();
    },
    onDragEnd() {
      syncLayoutVarsFromGrid();
      clearResizeState();
    },
  });
}

function clearResizeState() {
  document.querySelectorAll('.resizer.active').forEach(elem => elem.classList.remove('active'));
  document.body.classList.remove('dragging-layout');
  document.body.classList.remove('resizing-row');
}

function el(tag, attrs = {}, text = '') {
  const node = document.createElementNS(ns, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text) node.textContent = text;
  return node;
}

function html(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function short(s, n = 90) {
  s = String(s || '').replace(/\\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n - 1) + '...' : s;
}

function materializedPageLine(stepOrPage, generalPageId = '') {
  const page = stepOrPage?.materializedPage || stepOrPage || {};
  const general = generalPageId || page.generalPageId || '';
  const materialized = page.pageId || page.materializedPageId || '';
  const name = page.pageName || '';
  const filename = page.filename || '';
  const instanceKey = page.instanceKey || '';
  if (!general && !materialized && !filename && !name) return '';
  return `<div class="page-binding">
    <div class="small">General page: <code>${html(general || '(unknown)')}</code></div>
    <div class="small">Materialized page: <code>${html(materialized || '(unresolved)')}</code>${name ? ` · ${html(name)}` : ''}${filename ? ` · ${html(filename)}` : ''}</div>
    ${instanceKey ? `<div class="small">Instance key: <code>${html(instanceKey)}</code></div>` : ''}
  </div>`;
}

function currentScrollState() {
  return {
    inspectorTop: document.getElementById('inspector')?.scrollTop || 0,
    filmLeft: document.querySelector('.filmstrip')?.scrollLeft || 0,
  };
}

function restoreScrollState(scroll) {
  if (!scroll) return;
  requestAnimationFrame(() => {
    const inspector = document.getElementById('inspector');
    const filmstrip = document.querySelector('.filmstrip');
    if (inspector) inspector.scrollTop = scroll.inspectorTop || 0;
    if (filmstrip) filmstrip.scrollLeft = scroll.filmLeft || 0;
  });
}

function returnToTracePath() {
  const context = contextStack.pop();
  if (!context) return;
  if (context.mode === 'traceTransition') {
    selectTraceTransition(context.traceId, context.transitionIndex, { restoreScroll: context.scroll, keepStack: true });
  } else if (context.mode === 'trace') {
    selectTrace(context.traceId, { restoreScroll: context.scroll, keepStack: true });
  }
}

function returnButtonHtml() {
  const top = contextStack[contextStack.length - 1];
  if (!top || (top.mode !== 'trace' && top.mode !== 'traceTransition')) return '';
  return `<button onclick="returnToTracePath()">返回 Trace 路径</button>`;
}

function transitionSteps(transition) {
  if (!transition) return [];
  const steps = [];
  if (stepByKey[transition.fromStep]) steps.push(stepByKey[transition.fromStep]);
  for (const bridgeStep of transition.bridgeSteps || []) steps.push(bridgeStep);
  if (stepByKey[transition.toStep]) steps.push(stepByKey[transition.toStep]);
  return steps;
}

function traceTransitionKey(traceId, transitionIndex) {
  return `traceTransition:${traceId}:${transitionIndex}`;
}

function traceNoteKey(traceId) {
  return `traceNote:${traceId}`;
}

function stats() {
  const pairs = [
    ['问题数', DATA.stats.issues],
    ['失败项', DATA.stats.failIssues],
    ['缺失边', DATA.stats.missingObservedEdges],
    ['Loading 桥', DATA.stats.loadingBridgeUnclosed || 0],
    ['系统桥', DATA.stats.systemBridgeNotSkipped || 0],
    ['状态误作路由', DATA.stats.samePageStateMisclassified || 0],
    ['可疑跳过', DATA.stats.suspiciousSkips],
    ['页面数', DATA.stats.pages],
    ['路由数', DATA.stats.canonicalRoutes],
  ];
  document.getElementById('stats').innerHTML = pairs.map(([k, v]) =>
    `<div class="stat"><strong>${html(v)}</strong><span>${html(k)}</span></div>`
  ).join('');
}

function renderTraceSelector() {
  const select = document.getElementById('traceSelect');
  const options = ['<option value="">全部 Trace</option>'];
  for (const trace of DATA.traceFlows || []) {
    const label = `${trace.id} · ${trace.nodeIds.length} 页 · ${trace.transitions.length} 跳转`;
    options.push(`<option value="${html(trace.id)}">${html(short(label, 110))}</option>`);
  }
  select.innerHTML = options.join('');
  select.addEventListener('change', () => selectTrace(select.value));
  document.getElementById('clearTraceBtn').addEventListener('click', () => {
    select.value = '';
    selectTrace('');
  });
}

function colorWithAlpha(hex, alpha) {
  const clean = String(hex || '').replace('#', '');
  if (!/^[0-9a-f]{6}$/i.test(clean)) return `rgba(8,145,178,${alpha})`;
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function colorForKey(key) {
  const palette = [
    '#2563eb', '#059669', '#d97706', '#dc2626', '#7c3aed', '#0891b2',
    '#be123c', '#4d7c0f', '#9333ea', '#0f766e', '#ea580c', '#475569',
    '#c026d3', '#1d4ed8', '#15803d', '#b45309', '#be185d', '#0369a1',
  ];
  const text = String(key || 'unscoped');
  let total = 0;
  for (let i = 0; i < text.length; i += 1) total += (i + 1) * text.charCodeAt(i);
  return palette[Math.abs(total) % palette.length];
}

function safeLaneClass(lane) {
  return `lane-${String(lane || 'unscoped').replace(/[^A-Za-z0-9_-]+/g, '-')}`;
}

function instanceNodeWidth() {
  return 230;
}

function instanceNodeHeight() {
  return 58;
}

function instanceFocusIds() {
  const focus = new Set();
  const activeFlow = activeTraceId ? instanceFlowByTrace.get(activeTraceId) : null;
  for (const id of activeFlow?.instanceIds || []) {
    if (id) focus.add(id);
  }
  if (selected.kind === 'instance' && selected.id) {
    focus.add(selected.id);
  }
  if (selected.kind === 'instanceEdge') {
    const edge = instanceEdgeById.get(selected.id);
    if (edge?.from) focus.add(edge.from);
    if (edge?.to) focus.add(edge.to);
  }
  return focus;
}

function instanceGroupHasFocus(group, focusIds) {
  if (!focusIds.size) return false;
  return (group.instances || []).some(instance => focusIds.has(instance.instanceId));
}

function visibleInstanceGroups(focusIds) {
  const groups = (instanceAudit.sourceClusters || [])
    .filter(group => (group.instances || []).length)
    .sort((a, b) => String(a.clusterId).localeCompare(String(b.clusterId)));
  const viewMode = instanceGraphOptions.viewMode || 'split';
  return groups.filter(group => {
    if (viewMode === 'all') return true;
    if (instanceGroupHasFocus(group, focusIds)) return true;
    if (viewMode === 'focus') return false;
    return (group.instances || []).length > 1 || group.isSuspicious;
  });
}

function instanceEdgeIsFocused(edge, activeEdges, focusIds) {
  if (selected.kind === 'instanceEdge' && selected.id === edge.id) return true;
  if (activeEdges.has(edge.id)) return true;
  if (!focusIds.size) return false;
  return focusIds.has(edge.from) || focusIds.has(edge.to);
}

function routeGraphRootId(nodes = DATA.nodes) {
  if (nodeById.has('c_meituan_home')) return 'c_meituan_home';
  if (nodeById.has('c_home')) return 'c_home';
  return nodes[0]?.id || '';
}

function computeInstanceSourceRanks() {
  const nodes = DATA.nodes.filter(node => nodeById.has(node.id));
  const edges = DATA.edges.filter(edge => nodeById.has(edge.from) && nodeById.has(edge.to));
  const rootId = routeGraphRootId(nodes);
  const outgoing = new Map();
  for (const edge of edges) {
    if (!outgoing.has(edge.from)) outgoing.set(edge.from, []);
    outgoing.get(edge.from).push(edge.to);
  }
  const bfsRankById = computeBfsRanks(nodes, outgoing, rootId);
  return computeInformationArchitectureRanks(nodes, edges, bfsRankById, rootId);
}

function compareInstanceGroups(a, b, rankByCluster, rootId) {
  const rankCompare = (rankByCluster.get(a.clusterId) ?? 999) - (rankByCluster.get(b.clusterId) ?? 999);
  if (rankCompare !== 0) return rankCompare;
  if (a.clusterId === rootId) return -1;
  if (b.clusterId === rootId) return 1;
  const laneA = semanticLane(nodeById.get(a.clusterId) || {}, rootId);
  const laneB = semanticLane(nodeById.get(b.clusterId) || {}, rootId);
  const laneCompare = laneLabel(laneA).localeCompare(laneLabel(laneB), 'zh-Hans-CN');
  if (laneCompare !== 0) return laneCompare;
  const countCompare = (b.instances || []).length - (a.instances || []).length;
  if (countCompare !== 0) return countCompare;
  return String(a.canonicalName || a.clusterId).localeCompare(String(b.canonicalName || b.clusterId), 'zh-Hans-CN');
}

function layoutInstanceGraph() {
  instanceNodePositions.clear();
  const bands = [];
  const width = instanceNodeWidth();
  const height = instanceNodeHeight();
  const focusIds = instanceFocusIds();
  const sourceRankById = computeInstanceSourceRanks();
  const rootId = routeGraphRootId();
  const groups = visibleInstanceGroups(focusIds)
    .sort((a, b) => compareInstanceGroups(a, b, sourceRankById, rootId));
  const columnByRank = new Map();
  const levelGuides = [];
  const left = 120;
  const top = 78;
  const columnGap = 330;
  const groupGap = 54;
  for (const group of groups) {
    const rank = Math.max(0, sourceRankById.get(group.clusterId) ?? routeDepth(nodeById.get(group.clusterId) || {}));
    const column = columnByRank.get(rank) || { rank, x: left + rank * columnGap, y: top };
    columnByRank.set(rank, column);
    const stackHeight = (group.instances || []).length * height + Math.max(0, (group.instances || []).length - 1) * 12;
    const y = column.y;
    for (const [index, instance] of (group.instances || []).entries()) {
      const instanceId = instance.instanceId;
      if (!instanceId) continue;
      instanceNodePositions.set(instanceId, {
        x: column.x,
        y: y + index * (height + 12),
        width,
        height,
        clusterId: group.clusterId,
        rank,
      });
    }
    bands.push({
      clusterId: group.clusterId,
      label: group.canonicalName || group.clusterId,
      materializedCount: (group.instances || []).length,
      rank,
      x: column.x - 16,
      y: y - 24,
      width: width + 32,
      height: stackHeight + 44,
      isSuspicious: Boolean(group.isSuspicious),
      color: nodeById.get(group.clusterId)?.color || '#0891b2',
    });
    column.y = y + stackHeight + groupGap;
  }
  const columns = Array.from(columnByRank.values()).sort((a, b) => a.rank - b.rank);
  const minY = Math.min(top, ...bands.map(band => band.y));
  const maxY = Math.max(top + 600, ...bands.map(band => band.y + band.height));
  for (const column of columns) {
    levelGuides.push({
      rank: column.rank,
      x: column.x - 34,
      y1: minY,
      y2: maxY,
      label: `level ${column.rank}`,
    });
  }
  return { bands, levelGuides };
}

function instanceEdgePath(edge) {
  const from = instanceNodePositions.get(edge.from);
  const to = instanceNodePositions.get(edge.to);
  if (!from || !to) return '';
  const x1 = from.x + from.width;
  const y1 = from.y + from.height / 2;
  const x2 = to.x;
  const y2 = to.y + to.height / 2;
  if (x2 < x1 + 30) {
    const lift = 78 + Math.abs(y2 - y1) * 0.18;
    return `M${x1 - 16},${y1} C${x1 + lift},${y1 - lift} ${x2 - lift},${y2 - lift} ${x2 + 16},${y2}`;
  }
  const dx = Math.max(80, Math.abs(x2 - x1) * 0.42);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

function instanceEdgeMid(edge) {
  const from = instanceNodePositions.get(edge.from);
  const to = instanceNodePositions.get(edge.to);
  if (!from || !to) return { x: 0, y: 0 };
  return {
    x: (from.x + from.width + to.x) / 2,
    y: (from.y + from.height / 2 + to.y + to.height / 2) / 2 - 8,
  };
}

function renderInstanceGraph() {
  instanceGraphLayer.innerHTML = '';
  const instanceLayout = layoutInstanceGraph();
  const bands = instanceLayout.bands || [];
  const activeFlow = activeTraceId ? instanceFlowByTrace.get(activeTraceId) : null;
  const activeInstances = new Set((activeFlow?.instanceIds || []).filter(Boolean));
  const activeEdges = new Set((activeFlow?.edgeIds || []).filter(Boolean));
  const focusIds = instanceFocusIds();
  for (const guide of instanceLayout.levelGuides || []) {
    instanceGraphLayer.appendChild(el('line', {
      class: 'instance-level-guide',
      x1: guide.x,
      y1: guide.y1,
      x2: guide.x,
      y2: guide.y2,
    }));
    instanceGraphLayer.appendChild(el('text', {
      class: 'instance-level-label',
      x: guide.x + 8,
      y: guide.y1 + 18,
    }, guide.label));
  }
  for (const band of bands) {
    instanceGraphLayer.appendChild(el('rect', {
      class: `instance-source-band${band.isSuspicious ? ' suspicious' : ''}`,
      x: band.x,
      y: band.y,
      width: band.width,
      height: band.height,
      style: `stroke:${band.color}; fill:${colorWithAlpha(band.color, 0.035)}`,
    }));
    instanceGraphLayer.appendChild(el('text', {
      class: 'instance-source-label',
      x: band.x + 12,
      y: band.y + 18,
    }, short(`${band.label} · ${band.clusterId} · level ${band.rank} · ${band.materializedCount} instance`, 64)));
  }
  for (const edge of instanceAudit.instanceEdges || []) {
    const pathText = instanceEdgePath(edge);
    if (!pathText) continue;
    const edgeFocused = instanceEdgeIsFocused(edge, activeEdges, focusIds);
    if (activeTraceId && !edgeFocused) continue;
    if (instanceGraphOptions.viewMode === 'focus' && focusIds.size && !edgeFocused) continue;
    const classes = ['instance-graph-edge'];
    if (instanceGraphOptions.dimLowEdges && Number(edge.count || 0) <= 1 && !edgeFocused) classes.push('low-count');
    if (selected.kind === 'instanceEdge' && selected.id === edge.id) classes.push('selected');
    if (activeTraceId) classes.push(activeEdges.has(edge.id) ? 'trace-active' : 'trace-dim');
    const path = el('path', {
      id: `instance-edge-${edge.id}`,
      class: classes.join(' '),
      d: pathText,
      'marker-end': 'url(#arrow)',
    });
    path.addEventListener('click', evt => {
      evt.stopPropagation();
      selectInstanceEdge(edge.id);
    });
    path.addEventListener('mouseenter', () => hoverInstanceEdge(edge.id, true));
    path.addEventListener('mouseleave', () => hoverInstanceEdge(edge.id, false));
    instanceGraphLayer.appendChild(path);
    const mid = instanceEdgeMid(edge);
    const labelClasses = ['instance-graph-label'];
    if (instanceGraphOptions.showLabels || edgeFocused || (selected.kind === 'instanceEdge' && selected.id === edge.id)) {
      labelClasses.push('visible');
    }
    instanceGraphLayer.appendChild(el('text', {
      id: `instance-edge-label-${edge.id}`,
      class: labelClasses.join(' '),
      x: mid.x,
      y: mid.y,
      'text-anchor': 'middle',
    }, `${edge.count || 0}`));
  }
  for (const [instanceId, pos] of instanceNodePositions.entries()) {
    const entry = instanceById.get(instanceId) || {};
    const color = nodeById.get(entry.sourceClusterId)?.color || '#0891b2';
    const classes = ['instance-graph-node'];
    if (entry.group?.isSuspicious) classes.push('suspicious');
    if (selected.kind === 'instance' && selected.id === instanceId) classes.push('selected');
    if (activeTraceId) classes.push(activeInstances.has(instanceId) ? 'trace-active' : 'trace-dim');
    else if (focusIds.size && !focusIds.has(instanceId)) classes.push('trace-dim');
    const g = el('g', {
      id: `instance-node-${instanceId}`,
      class: classes.join(' '),
      transform: `translate(${pos.x},${pos.y})`,
    });
    g.addEventListener('click', evt => {
      evt.stopPropagation();
      selectInstance(instanceId);
    });
    g.appendChild(el('rect', { width: pos.width, height: pos.height, stroke: color }));
    g.appendChild(el('text', { x: 12, y: 19 }, short(entry.pageName || instanceId, 30)));
    g.appendChild(el('text', { class: 'route', x: 12, y: 36 }, short(`${entry.instanceKey || '(no key)'} · ${entry.sourceClusterId || ''}`, 34)));
    instanceGraphLayer.appendChild(g);
  }
}

function instanceGraphBounds() {
  if (graphMode !== 'instance' || !instanceNodePositions.size) return null;
  const boxes = [];
  for (const pos of instanceNodePositions.values()) {
    boxes.push({
      left: pos.x,
      top: pos.y,
      right: pos.x + pos.width,
      bottom: pos.y + pos.height,
    });
  }
  const left = Math.min(...boxes.map(box => box.left)) - 96;
  const top = Math.min(...boxes.map(box => box.top)) - 48;
  const right = Math.max(...boxes.map(box => box.right)) + 96;
  const bottom = Math.max(...boxes.map(box => box.bottom)) + 48;
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

function currentNodeX(nodeId) {
  return traceLayout.nodePositions.get(nodeId)?.x ?? globalLayout.nodePositions.get(nodeId)?.x ?? nodeById.get(nodeId)?.x ?? 0;
}

function currentNodeY(nodeId) {
  return traceLayout.nodePositions.get(nodeId)?.y ?? globalLayout.nodePositions.get(nodeId)?.y ?? nodeById.get(nodeId)?.y ?? 0;
}

function visibleEdgeIdsForTrace(trace) {
  const ids = new Set();
  if (!trace) return ids;
  if (activeTransitionIndex !== null) {
    const transition = (trace.transitions || [])[activeTransitionIndex];
    if (transition?.edgeId) ids.add(transition.edgeId);
    return ids;
  }
  for (const transition of trace.transitions || []) {
    if (transition.edgeId) ids.add(transition.edgeId);
  }
  return ids;
}

function traceLayoutEdgePath(edge) {
  const section = activeTraceId ? traceLayout.edgeSections.get(edge.id) : globalLayout.edgeSections.get(edge.id);
  if (!section) return '';
  const points = compactPoints([section.startPoint, ...(section.bendPoints || []), section.endPoint].filter(Boolean));
  if (!points.length) return '';
  return points.map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x},${point.y}`).join(' ');
}

function compactPoints(points) {
  const compacted = [];
  for (const point of points) {
    const prev = compacted[compacted.length - 1];
    if (prev && prev.x === point.x && prev.y === point.y) continue;
    compacted.push(point);
  }
  return compacted;
}

function edgePath(edge) {
  const routed = traceLayoutEdgePath(edge);
  if (routed) return routed;
  const fromHeight = nodeHeight(edge.from);
  const toHeight = nodeHeight(edge.to);
  const x1 = currentNodeX(edge.from) + nodeWidth(edge.from);
  const y1 = currentNodeY(edge.from) + fromHeight / 2;
  const x2 = currentNodeX(edge.to);
  const y2 = currentNodeY(edge.to) + toHeight / 2;
  const sameLevel = Math.abs(x2 - x1) < 80;
  if (sameLevel) {
    const lift = 70 + Math.abs(y2 - y1) * 0.24;
    return `M${x1 - 90},${y1 - 26} C${x1 - 90},${y1 - lift} ${x2 + 90},${y2 - lift} ${x2 + 90},${y2 - 26}`;
  }
  if (x2 < x1) {
    const lift = 70 + Math.abs(y2 - y1) * 0.18;
    return `M${x1 - 90},${y1 - 26} C${x1 - 90},${y1 - lift} ${x2 + 90},${y2 - lift} ${x2 + 90},${y2 - 26}`;
  }
  const dx = Math.max(80, (x2 - x1) * 0.48);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

function edgeMid(edge) {
  const section = activeTraceId ? traceLayout.edgeSections.get(edge.id) : globalLayout.edgeSections.get(edge.id);
  if (section) {
    const points = [section.startPoint, ...(section.bendPoints || []), section.endPoint].filter(Boolean);
    if (points.length >= 2) {
      const idx = Math.floor((points.length - 1) / 2);
      const a = points[idx];
      const b = points[idx + 1] || a;
      return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 9 };
    }
  }
  return {
    x: (currentNodeX(edge.from) + nodeWidth(edge.from) + currentNodeX(edge.to)) / 2,
    y: (currentNodeY(edge.from) + nodeHeight(edge.from) / 2 + currentNodeY(edge.to) + nodeHeight(edge.to) / 2) / 2 - 9,
  };
}

function nodeWidth(nodeId) {
  const actionWidth = traceActionLines(nodeId)
    .reduce((maxWidth, action) => Math.max(maxWidth, 24 + visualTextWidth(action.text)), 0);
  return activeTraceId ? Math.max(160, actionWidth) : Math.max(190, actionWidth);
}

function nodeHeight(nodeId) {
  const actionCount = traceActionLines(nodeId).length;
  return actionCount > 0 ? 64 + actionCount * 12 : 46;
}

function visualTextWidth(text) {
  return [...String(text || '')].reduce((total, ch) => total + (ch.charCodeAt(0) > 127 ? 9 : 5.4), 0);
}

function traceActionText(transition) {
  const stepIndex = transition.fromStepIndex ?? parseStepIndex(transition.fromStep);
  const prefix = stepIndex >= 0 ? String(stepIndex) : '?';
  return `${prefix}: ${transition.action || '(无动作)'}`;
}

function parseStepIndex(stepKey) {
  const match = String(stepKey || '').match(/:(\\d+)$/);
  return match ? Number(match[1]) : -1;
}

function traceTransitionsForDisplay() {
  if (!activeTraceId) return;
  const trace = traceById.get(activeTraceId);
  if (!trace) return [];
  return activeTransitionIndex === null
    ? (trace.transitions || [])
    : [(trace.transitions || [])[activeTransitionIndex]].filter(Boolean);
}

function renderTraceStepLabels() {
  traceStepLayer.innerHTML = '';
  for (const transition of traceTransitionsForDisplay() || []) {
    const edge = edgeById.get(transition.edgeId) || { from: transition.from, to: transition.to };
    const mid = edgeMid(edge);
    const stepIndex = transition.fromStepIndex ?? parseStepIndex(transition.fromStep);
    traceStepLayer.appendChild(el('text', {
      class: 'trace-step-label',
      x: mid.x,
      y: mid.y - 8,
      'text-anchor': 'middle',
    }, stepIndex >= 0 ? String(stepIndex) : '?'));
  }
}

function updateTraceGeometry() {
  renderHierarchyBands();
  if (graphMode === 'instance') {
    renderInstanceGraph();
    const bounds = instanceGraphBounds();
    svg.setAttribute('viewBox', `0 0 ${Math.max(1200, (bounds?.right || 0) + 140)} ${Math.max(760, (bounds?.bottom || 0) + 140)}`);
    return;
  }
  for (const node of DATA.nodes) {
    const group = document.getElementById(`node-${CSS.escape(node.id)}`);
    if (group) {
      group.setAttribute('transform', `translate(${currentNodeX(node.id)},${currentNodeY(node.id)})`);
    }
    const rect = document.querySelector(`#node-${CSS.escape(node.id)} rect`);
    if (!rect) continue;
    rect.setAttribute('width', nodeWidth(node.id));
    rect.setAttribute('height', nodeHeight(node.id));
  }
  for (const edge of DATA.edges) {
    const path = document.getElementById(`edge-${CSS.escape(edge.id)}`);
    if (path && nodeById.has(edge.from) && nodeById.has(edge.to)) {
      path.setAttribute('d', edgePath(edge));
    }
    const label = document.getElementById(`edge-label-${CSS.escape(edge.id)}`);
    if (label && nodeById.has(edge.from) && nodeById.has(edge.to)) {
      const mid = edgeMid(edge);
      label.setAttribute('x', mid.x);
      label.setAttribute('y', mid.y);
    }
  }
  const activeLayout = activeTraceId ? traceLayout : globalLayout;
  const width = activeLayout.width
    ? Math.max(activeTraceId ? 900 : 1600, activeLayout.width + 140)
    : Math.max(1600, Math.max(...DATA.nodes.map(n => currentNodeX(n.id) + nodeWidth(n.id))) + 170);
  const height = activeLayout.height
    ? Math.max(activeTraceId ? 650 : 900, activeLayout.height + 140)
    : Math.max(900, Math.max(...DATA.nodes.map(n => currentNodeY(n.id) + nodeHeight(n.id))) + 180);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
}

function renderHierarchyBands() {
  hierarchyLayer.innerHTML = '';
  if (activeTraceId || graphMode === 'instance' || !globalLayout.ready) return;
  for (const guide of globalLayout.depthGuides || []) {
    hierarchyLayer.appendChild(el('line', {
      class: 'depth-guide',
      x1: guide.x,
      y1: guide.y1,
      x2: guide.x,
      y2: guide.y2,
    }));
    if (guide.label) {
      hierarchyLayer.appendChild(el('text', {
        class: 'depth-label',
        x: guide.x + 8,
        y: guide.y1 + 18,
      }, guide.label));
    }
  }
  for (const band of globalLayout.bands || []) {
    const bandColor = band.color || colorForKey(band.lane || band.label || 'lane');
    hierarchyLayer.appendChild(el('rect', {
      class: `hierarchy-band ${safeLaneClass(band.lane || 'unscoped')}`,
      x: band.x,
      y: band.y,
      width: band.width,
      height: band.height,
      style: `fill:${colorWithAlpha(bandColor, 0.12)};stroke:${colorWithAlpha(bandColor, 0.62)};`,
    }));
    hierarchyLayer.appendChild(el('text', {
      class: 'hierarchy-label',
      x: band.x + 14,
      y: band.y + 23,
    }, band.label));
  }
}

function renderTraceNodeActions() {
  document.querySelectorAll('.node-actions').forEach(group => {
    group.innerHTML = '';
  });
  for (const node of DATA.nodes) {
    const group = document.getElementById(`node-actions-${CSS.escape(node.id)}`);
    if (!group) continue;
    const actions = traceActionLines(node.id);
    let y = 68;
    actions.forEach(action => {
      group.appendChild(el('text', {
        class: `trace-action ${action.kind}`,
        x: 12,
        y,
      }, action.text));
      y += 12;
    });
  }
}

function traceNodeActions(nodeId) {
  const trace = activeTraceId ? traceById.get(activeTraceId) : null;
  if (!trace) return [];
  const transitionFromSteps = new Set((trace.transitions || []).map(transition => transition.fromStep));
  const actions = [];
  for (const step of trace.labeledSteps || []) {
    if (step.pageId !== nodeId) continue;
    const key = step.key || `${trace.id}:${step.stepIndex}`;
    const kind = transitionFromSteps.has(key) ? 'inter-page' : 'intra-page';
    actions.push({
      kind,
      stepIndex: step.stepIndex,
      text: `${step.stepIndex}: ${step.action || '(无动作)'}`,
    });
  }
  if (activeTransitionIndex !== null) {
    const transition = (trace.transitions || [])[activeTransitionIndex];
    const focused = transition ? transition.fromStep : '';
    return actions.filter(action => `${trace.id}:${action.stepIndex}` === focused);
  }
  return actions;
}

function traceActionLines(nodeId) {
  const lines = [];
  for (const action of traceNodeActions(nodeId)) {
    for (const line of wrapTraceActionText(action.text)) {
      lines.push({ kind: action.kind, text: line });
    }
  }
  return lines;
}

function wrapTraceActionText(text, maxWidth = TRACE_ACTION_WRAP_WIDTH) {
  const source = String(text || '').trim();
  if (!source) return [''];
  const lines = [];
  let current = '';
  for (const ch of source) {
    const candidate = current + ch;
    if (current && visualTextWidth(candidate) > maxWidth) {
      lines.push(current);
      current = ch;
    } else {
      current = candidate;
    }
  }
  if (current) lines.push(current);
  return lines;
}

function ensureElk() {
  if (!window.ELK) {
    throw new Error('elkjs is required for trace-wise route graph layout');
  }
  if (!elk) elk = new ELK();
  return elk;
}

function applyElkResult(layout, result, xOffset = 60, yOffset = 50) {
  for (const child of result.children || []) {
    layout.nodePositions.set(child.id, {
      x: Math.max(40, Math.round((child.x || 0) + xOffset)),
      y: Math.max(40, Math.round((child.y || 0) + yOffset)),
    });
  }
  for (const edge of result.edges || []) {
    const section = (edge.sections || [])[0];
    if (section) {
      const shift = point => point ? { x: Math.round(point.x + xOffset), y: Math.round(point.y + yOffset) } : point;
      layout.edgeSections.set(edge.id, {
        startPoint: shift(section.startPoint),
        bendPoints: (section.bendPoints || []).map(shift),
        endPoint: shift(section.endPoint),
      });
    }
  }
  const positioned = Array.from(layout.nodePositions, ([id, pos]) => ({
    id,
    right: pos.x + nodeWidth(id),
    bottom: pos.y + nodeHeight(id),
  }));
  layout.width = positioned.length ? Math.max(...positioned.map(item => item.right), (result.width || 0) + xOffset) : 0;
  layout.height = positioned.length ? Math.max(...positioned.map(item => item.bottom), (result.height || 0) + yOffset) : 0;
  layout.ready = true;
  return layout;
}

function queueGlobalLayout() {
  const token = ++globalLayoutToken;
  computeGlobalLayout()
    .then(nextLayout => {
      if (token !== globalLayoutToken) return;
      globalLayout = nextLayout;
      if (!activeTraceId) {
        updateTraceGeometry();
        scheduleFitGraph();
        applySelection();
      }
    })
    .catch(err => {
      console.error(err);
      throw err;
    });
}

async function computeGlobalLayout() {
  const layout = emptyGraphLayout();
  const nodes = DATA.nodes.filter(node => nodeById.has(node.id));
  const edges = DATA.edges.filter(edge => nodeById.has(edge.from) && nodeById.has(edge.to));
  const rootId = routeGraphRootId(nodes);
  const outgoing = new Map();
  for (const edge of edges) {
    if (!outgoing.has(edge.from)) outgoing.set(edge.from, []);
    outgoing.get(edge.from).push(edge.to);
  }
  const directFromRoot = new Set(outgoing.get(rootId) || []);
  const externalNodes = nodes.filter(isExternalSystemNode);
  const externalNodeIds = new Set(externalNodes.map(node => node.id));
  const bfsRankById = computeBfsRanks(nodes, outgoing, rootId);
  const rankById = computeInformationArchitectureRanks(nodes, edges, bfsRankById, rootId);
  const laneEdgesByTarget = new Map();
  const laneById = new Map();
  for (const node of nodes) {
    laneById.set(node.id, semanticLane(node, rootId));
  }
  for (const edge of edges) {
    const fromLane = laneById.get(edge.from);
    const toLane = laneById.get(edge.to);
    if (!fromLane || fromLane !== toLane) continue;
    if (!laneEdgesByTarget.has(edge.to)) laneEdgesByTarget.set(edge.to, []);
    laneEdgesByTarget.get(edge.to).push(edge.from);
  }
  const byLane = new Map();
  for (const node of nodes) {
    if (node.id === rootId) continue;
    if (externalNodeIds.has(node.id)) continue;
    const lane = laneById.get(node.id) || 'other';
    if (!byLane.has(lane)) byLane.set(lane, []);
    byLane.get(lane).push(node);
  }
  const sortedLanes = Array.from(byLane.keys()).sort((a, b) =>
    compareLanes(a, b, byLane, rankById, directFromRoot)
  );
  const maxRank = Math.max(1, ...Array.from(rankById.values()));
  const rootX = 120;
  const left = 430;
  const top = 78;
  const columnGap = 300;
  const rowGap = 54;
  const laneGap = 48;
  const bandPaddingTop = 42;
  const bandPaddingBottom = 32;
  const graphRight = left + Math.max(0, maxRank - 1) * columnGap + 260;
  const bandX = left - 42;
  const bandWidth = graphRight - bandX;
  let y = top;

  for (const lane of sortedLanes) {
    const items = (byLane.get(lane) || []).slice().sort((a, b) => compareSemanticNodes(a, b, rankById));
    if (!items.length) continue;
    const bandY = y;
    const bandRectY = bandY - 26;
    y += bandPaddingTop;
    const byRank = new Map();
    for (const node of items) {
      const rank = rankById.get(node.id) ?? 0;
      if (!byRank.has(rank)) byRank.set(rank, []);
      byRank.get(rank).push(node);
    }
    const rowById = new Map();
    const usedRowsByRank = new Map();
    for (const [rank, group] of Array.from(byRank.entries()).sort((a, b) => a[0] - b[0])) {
      group.sort(compareRouteNodes);
      const usedRows = usedRowsByRank.get(rank) || new Set();
      usedRowsByRank.set(rank, usedRows);
      group.forEach(node => {
        const preferredRows = (laneEdgesByTarget.get(node.id) || [])
          .map(sourceId => rowById.get(sourceId))
          .filter(row => row !== undefined);
        const preferred = preferredRows.length ? Math.min(...preferredRows) : 0;
        const rowIndex = claimNearestFreeRow(usedRows, preferred);
        rowById.set(node.id, rowIndex);
        layout.nodePositions.set(node.id, {
          x: left + Math.max(0, rank - 1) * columnGap,
          y: y + rowIndex * rowGap,
        });
      });
    }
    const laneRows = Math.max(1, ...Array.from(rowById.values(), row => row + 1));
    y += laneRows * rowGap;
    const nodeBottom = y - rowGap + nodeHeight(items[0].id);
    const contentBottom = nodeBottom + bandPaddingBottom;
    layout.bands.push({
      kind: 'lane',
      lane,
      label: `${laneLabel(lane)} (${items.length})`,
      color: colorForKey(lane),
      x: bandX,
      y: bandRectY,
      width: bandWidth,
      height: contentBottom - bandRectY,
    });
    y = contentBottom + laneGap;
  }

  const minBandY = Math.min(...layout.bands.map(band => band.y), top);
  const maxBandY = Math.max(...layout.bands.map(band => band.y + band.height), y);
  if (rootId) {
    layout.nodePositions.set(rootId, {
      x: rootX,
      y: Math.round((minBandY + maxBandY) / 2 - nodeHeight(rootId) / 2),
    });
  }
  externalNodes.forEach((node, index) => {
    layout.nodePositions.set(node.id, {
      x: rootX,
      y: Math.max(10, minBandY - 42 - index * (nodeHeight(node.id) + 16)),
    });
  });
  for (let depth = 1; depth <= maxRank; depth++) {
    layout.depthGuides.push({
      depth,
      x: left + (depth - 1) * columnGap - 24,
      y1: minBandY,
      y2: maxBandY,
      label: `${depth}-hop`,
    });
  }

  for (const edge of edges) {
    const from = layout.nodePositions.get(edge.from);
    const to = layout.nodePositions.get(edge.to);
    if (!from || !to) continue;
    layout.edgeSections.set(edge.id, orthogonalEdgeSection(edge, from, to));
  }
  layout.width = graphRight + 80;
  layout.height = Math.max(900, y + 40);
  layout.ready = true;
  return layout;
}

function routeDomainFromPath(path) {
  return normalizeRouteDomain(routeSegments(path)[0] || '');
}

function normalizeRouteDomain(raw) {
  return cleanNamespaceSegment(raw) || 'unscoped';
}

function cleanNamespaceSegment(raw) {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_:-]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

function routeSegments(path) {
  return String(path || '').split('/').map(cleanNamespaceSegment).filter(Boolean);
}

function routeDepth(node) {
  const segments = routeSegments(node.routePath);
  return Math.max(0, segments.length - 1);
}

function claimNearestFreeRow(usedRows, preferred) {
  const start = Math.max(0, Math.round(preferred || 0));
  for (let offset = 0; offset <= usedRows.size + 1; offset++) {
    const candidates = offset === 0 ? [start] : [start + offset, start - offset].filter(row => row >= 0);
    for (const row of candidates) {
      if (!usedRows.has(row)) {
        usedRows.add(row);
        return row;
      }
    }
  }
  const fallback = usedRows.size;
  usedRows.add(fallback);
  return fallback;
}

function isExternalSystemNode(node) {
  const domain = node.domain || routeDomainFromPath(node.routePath) || '';
  const blob = `${node.id || ''} ${node.name || ''} ${node.routePath || ''}`.toLowerCase();
  return domain === 'system' || blob.includes('launcher') || blob.includes('phone_home');
}

function computeBfsRanks(nodes, outgoing, rootId) {
  const ids = new Set(nodes.map(node => node.id));
  const ranks = new Map();
  if (rootId && ids.has(rootId)) {
    ranks.set(rootId, 0);
    const queue = [rootId];
    while (queue.length) {
      const current = queue.shift();
      const nextRank = (ranks.get(current) || 0) + 1;
      for (const target of outgoing.get(current) || []) {
        if (!ids.has(target)) continue;
        if (!ranks.has(target) || nextRank < ranks.get(target)) {
          ranks.set(target, nextRank);
          queue.push(target);
        }
      }
    }
  }
  for (const node of nodes) {
    if (!ranks.has(node.id)) {
      ranks.set(node.id, Math.max(1, routeDepth(node)));
    }
  }
  return ranks;
}

function computeInformationArchitectureRanks(nodes, edges, bfsRankById, rootId) {
  const ranks = new Map();
  const nodesById = new Map(nodes.map(node => [node.id, node]));
  const edgePairs = new Set(edges.map(edge => `${edge.from}->${edge.to}`));
  for (const node of nodes) {
    if (node.id === rootId) {
      ranks.set(node.id, 0);
      continue;
    }
    const domain = node.domain || routeDomainFromPath(node.routePath) || 'other';
    const segments = routeSegments(node.routePath);
    const bfsRank = bfsRankById.get(node.id) ?? Math.max(1, routeDepth(node));
    if (isUtilityDomain(domain)) {
      ranks.set(node.id, Math.max(1, bfsRank));
      continue;
    }
    if (domain === 'meituan') {
      ranks.set(node.id, Math.max(1, bfsRank));
      continue;
    }
    const leaf = segments[1] || '';
    const isDomainLanding = ['home', 'landing', 'booking'].includes(leaf);
    if (isDomainLanding) {
      ranks.set(node.id, 1);
      continue;
    }
    ranks.set(node.id, Math.max(2, routeDepth(node)));
  }
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false;
    for (const edge of edges) {
      const source = nodesById.get(edge.from);
      const target = nodesById.get(edge.to);
      if (!source || !target || !shouldPromoteIaEdge(source, target, rootId, edgePairs, bfsRankById)) continue;
      const nextRank = (ranks.get(source.id) || 0) + 1;
      if (nextRank > (ranks.get(target.id) || 0)) {
        ranks.set(target.id, nextRank);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return ranks;
}

function shouldPromoteIaEdge(source, target, rootId, edgePairs, bfsRankById) {
  if (source.id === rootId || target.id === rootId) return false;
  const sourceDomain = source.domain || routeDomainFromPath(source.routePath) || 'other';
  const targetDomain = target.domain || routeDomainFromPath(target.routePath) || 'other';
  if (sourceDomain !== targetDomain) return false;
  if (isUtilityDomain(sourceDomain) || isUtilityDomain(targetDomain)) return false;
  const targetSegments = routeSegments(target.routePath);
  if (['home', 'landing', 'booking'].includes(targetSegments[1] || '')) return false;
  const sourceDepth = routeDepth(source);
  const targetDepth = routeDepth(target);
  const sourceBfs = bfsRankById.get(source.id) ?? sourceDepth;
  const targetBfs = bfsRankById.get(target.id) ?? targetDepth;
  if (targetDepth < sourceDepth) return false;
  if (targetDepth === sourceDepth && edgePairs.has(`${target.id}->${source.id}`)) return false;
  if (!(targetDepth > sourceDepth || targetBfs > sourceBfs)) return false;
  return true;
}

function isUtilityDomain(domain) {
  return ['common', 'webview', 'system'].includes(domain || '');
}

function nodeNamespaceSegments(node) {
  const pathSegments = routeSegments(node.routePath);
  if (pathSegments.length) return pathSegments;
  const idSegments = String(node.id || '')
    .replace(/^c_/, '')
    .split('_')
    .map(cleanNamespaceSegment)
    .filter(Boolean);
  if (idSegments.length) return idSegments;
  const domain = cleanNamespaceSegment(node.domain);
  return domain ? [domain] : ['unscoped'];
}

function semanticLane(node, rootId) {
  if (node.id === rootId) return 'root';
  const segments = nodeNamespaceSegments(node);
  const rootSegments = nodeNamespaceSegments(nodeById.get(rootId) || {});
  const top = segments[0] || 'unscoped';
  const second = segments[1] || '';
  const rootTop = rootSegments[0] || '';
  if (top && top === rootTop && second) return `${top}/${second}`;
  return top;
}

function compareLanes(a, b, byLane, rankById, directFromRoot) {
  const stats = lane => {
    const items = byLane.get(lane) || [];
    const ranks = items.map(node => rankById.get(node.id) ?? 999);
    return {
      direct: items.some(node => directFromRoot.has(node.id)) ? 0 : 1,
      rank: ranks.length ? Math.min(...ranks) : 999,
      count: items.length,
      depth: String(lane).split('/').length,
      label: laneLabel(lane),
    };
  };
  const left = stats(a);
  const right = stats(b);
  if (left.direct !== right.direct) return left.direct - right.direct;
  if (left.rank !== right.rank) return left.rank - right.rank;
  if (left.count !== right.count) return right.count - left.count;
  if (left.depth !== right.depth) return left.depth - right.depth;
  return left.label.localeCompare(right.label, 'zh-Hans-CN');
}

function laneLabel(lane) {
  return String(lane || 'unscoped').split('/').join(' / ');
}

function compareSemanticNodes(a, b, rankById) {
  const rankCompare = (rankById.get(a.id) ?? 0) - (rankById.get(b.id) ?? 0);
  if (rankCompare !== 0) return rankCompare;
  return compareRouteNodes(a, b);
}

function compareRouteNodes(a, b) {
  const pathCompare = String(a.routePath || a.id).localeCompare(String(b.routePath || b.id));
  if (pathCompare !== 0) return pathCompare;
  return String(a.name || a.id).localeCompare(String(b.name || b.id));
}

function queueTraceLayout() {
  document.body.classList.toggle('trace-layout-mode', Boolean(activeTraceId && graphMode !== 'instance'));
  if (!activeTraceId || graphMode === 'instance') {
    if (traceLayout.traceId) {
      traceLayout = emptyTraceLayout();
      updateTraceGeometry();
      scheduleFitGraph();
    }
    return;
  }
  if (traceLayout.traceId === activeTraceId && traceLayout.transitionIndex === activeTransitionIndex) return;
  const token = ++traceLayoutToken;
  computeTraceLayout(activeTraceId, activeTransitionIndex)
    .then(nextLayout => {
      if (token !== traceLayoutToken) return;
      traceLayout = nextLayout;
      updateTraceGeometry();
      renderTraceStepLabels();
      renderTraceNodeActions();
      scheduleFitGraph();
      applySelection();
    })
    .catch(err => {
      console.error(err);
      throw err;
    });
}

async function computeTraceLayout(traceId, transitionIndex) {
  const trace = traceById.get(traceId);
  if (!trace) return emptyTraceLayout();
  const layout = emptyTraceLayout();
  layout.traceId = traceId;
  layout.transitionIndex = transitionIndex;

  const transition = transitionIndex === null ? null : (trace.transitions || [])[transitionIndex];
  const nodeIds = transition
    ? [transition.from, transition.to].filter(Boolean)
    : (trace.nodeIds || []).filter(Boolean);
  const edgeIds = transition?.edgeId
    ? [transition.edgeId]
    : Array.from(visibleEdgeIdsForTrace(trace));
  const uniqueNodeIds = Array.from(new Set(nodeIds)).filter(id => nodeById.has(id));
  const uniqueEdges = Array.from(new Set(edgeIds))
    .map(id => edgeById.get(id))
    .filter(edge => edge && uniqueNodeIds.includes(edge.from) && uniqueNodeIds.includes(edge.to));

  if (!uniqueNodeIds.length) return layout;
  const graph = {
    id: 'trace-root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.spacing.nodeNode': '52',
      'elk.spacing.edgeNode': '30',
      'elk.spacing.edgeEdge': '18',
      'elk.layered.spacing.nodeNodeBetweenLayers': '56',
      'elk.layered.spacing.edgeNodeBetweenLayers': '18',
      'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
      'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
    },
    children: uniqueNodeIds.map(id => ({
      id,
      width: nodeWidth(id),
      height: nodeHeight(id),
    })),
    edges: uniqueEdges.map(edge => ({
      id: edge.id,
      sources: [edge.from],
      targets: [edge.to],
    })),
  };
  const result = await ensureElk().layout(graph);
  return applyElkResult(layout, result, 60, 50);
}

function orthogonalEdgeSection(edge, from, to, offset = 0) {
  const x1 = from.x + nodeWidth(edge.from);
  const y1 = from.y + nodeHeight(edge.from) / 2;
  const x2 = to.x;
  const y2 = to.y + nodeHeight(edge.to) / 2;
  const forward = x2 >= x1;
  const midXBase = forward
    ? Math.round(x1 + Math.max(52, (x2 - x1) / 2))
    : Math.round(Math.min(from.x, to.x) - 58);
  const midX = midXBase + (forward ? offset : -Math.abs(offset));
  const y1Out = Math.round(y1 + offset);
  const y2In = Math.round(y2 + offset);
  return {
    startPoint: { x: Math.round(x1), y: Math.round(y1) },
    bendPoints: [
      { x: Math.round(x1 + Math.max(14, Math.abs(offset))), y: y1Out },
      { x: midX, y: y1Out },
      { x: midX, y: y2In },
      { x: Math.round(x2 - Math.max(14, Math.abs(offset))), y: y2In },
    ],
    endPoint: { x: Math.round(x2), y: Math.round(y2) },
  };
}

function renderGraph() {
  document.body.classList.toggle('instance-graph-mode', graphMode === 'instance');
  viewport.innerHTML = '';
  const width = Math.max(1600, Math.max(...DATA.nodes.map(n => currentNodeX(n.id) + nodeWidth(n.id))) + 170);
  const height = Math.max(900, Math.max(...DATA.nodes.map(n => currentNodeY(n.id) + nodeHeight(n.id))) + 180);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const defs = el('defs');
  defs.innerHTML = '<marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#64748b"/></marker>';
  viewport.appendChild(defs);
  if (graphMode === 'instance') {
    viewport.appendChild(instanceGraphLayer);
    renderInstanceGraph();
    const bounds = instanceGraphBounds();
    if (bounds) {
      const viewWidth = Math.max(1000, bounds.right + 140);
      const viewHeight = Math.max(720, bounds.bottom + 140);
      svg.setAttribute('viewBox', `0 0 ${viewWidth} ${viewHeight}`);
    }
    updateTraceGeometry();
    applyTransform();
    applySelection();
    return;
  }
  viewport.appendChild(hierarchyLayer);

  for (const edge of DATA.edges) {
    if (!nodeById.has(edge.from) || !nodeById.has(edge.to)) continue;
    const path = el('path', {
      id: `edge-${edge.id}`,
      class: `edge ${edge.kind}`,
      d: edgePath(edge),
      'marker-end': 'url(#arrow)',
    });
    path.addEventListener('click', evt => {
      evt.stopPropagation();
      const trace = activeTraceId ? traceById.get(activeTraceId) : null;
      const index = trace ? (trace.transitions || []).findIndex(item => item.edgeId === edge.id) : -1;
      if (trace && index >= 0) openEdgeFromTrace(edge.id, trace.id, index);
      else selectEdge(edge.id);
    });
    path.addEventListener('mouseenter', () => hoverEdge(edge.id, true));
    path.addEventListener('mouseleave', () => hoverEdge(edge.id, false));
    viewport.appendChild(path);
    const mid = edgeMid(edge);
    const label = el('text', { id: `edge-label-${edge.id}`, class: 'edge-label', x: mid.x, y: mid.y }, edge.label || edge.kind);
    viewport.appendChild(label);
  }
  viewport.appendChild(traceStepLayer);
  for (const node of DATA.nodes) {
    const g = el('g', { id: `node-${node.id}`, class: 'node', transform: `translate(${currentNodeX(node.id)},${currentNodeY(node.id)})` });
    g.addEventListener('click', evt => {
      evt.stopPropagation();
      const trace = activeTraceId ? traceById.get(activeTraceId) : null;
      if (trace && (trace.nodeIds || []).includes(node.id)) openNodeFromTrace(node.id, trace.id, activeTransitionIndex);
      else selectNode(node.id);
    });
    g.addEventListener('mouseenter', () => hoverNode(node.id, true));
    g.addEventListener('mouseleave', () => hoverNode(node.id, false));
    g.appendChild(el('rect', { width: nodeWidth(node.id), height: nodeHeight(node.id), stroke: node.color }));
    g.appendChild(el('circle', { cx: 14, cy: 18, r: 5, fill: node.color }));
    g.appendChild(el('text', { x: 26, y: 21 }, short(node.name, 24)));
    g.appendChild(el('text', { class: 'route', x: 12, y: 39 }, short(node.routePath, 34)));
    if (activeTraceId) {
      g.appendChild(el('text', { class: 'route', x: 12, y: 52 }, `${node.stepCount} 步 · ${node.traceCount} 条 trace`));
    }
    g.appendChild(el('g', { id: `node-actions-${node.id}`, class: 'node-actions' }));
    viewport.appendChild(g);
  }
  applyTransform();
  applyTraceHighlight();
  applySelection();
}

function applyTransform() {
  viewport.setAttribute('transform', `translate(${transform.x},${transform.y}) scale(${transform.k})`);
}

function activeGraphBounds() {
  if (graphMode === 'instance') {
    return instanceGraphBounds() || { left: 0, top: 0, right: 1200, bottom: 760, width: 1200, height: 760 };
  }
  const trace = activeTraceId ? traceById.get(activeTraceId) : null;
  let nodeIds = trace
    ? Array.from(new Set(activeTransitionIndex === null
      ? (trace.nodeIds || [])
      : [trace.transitions?.[activeTransitionIndex]?.from, trace.transitions?.[activeTransitionIndex]?.to].filter(Boolean)))
    : DATA.nodes.map(node => node.id);
  if (!activeTraceId && selected.kind === 'node') {
    nodeIds = [selected.id];
  } else if (!activeTraceId && selected.kind === 'edge') {
    const edge = edgeById.get(selected.id);
    if (edge) nodeIds = [edge.from, edge.to];
  }
  const boxes = [];
  for (const nodeId of nodeIds) {
    if (!nodeById.has(nodeId)) continue;
    boxes.push({
      left: currentNodeX(nodeId),
      top: currentNodeY(nodeId),
      right: currentNodeX(nodeId) + nodeWidth(nodeId),
      bottom: currentNodeY(nodeId) + nodeHeight(nodeId),
    });
  }
  if (!activeTraceId && selected.kind !== 'node' && selected.kind !== 'edge') {
    for (const band of globalLayout.bands || []) {
      boxes.push({
        left: band.x,
        top: band.y,
        right: band.x + band.width,
        bottom: band.y + band.height,
      });
    }
  }
  if (!boxes.length) {
    return { left: 0, top: 0, right: 1200, bottom: 900, width: 1200, height: 900 };
  }
  const left = Math.min(...boxes.map(box => box.left));
  const top = Math.min(...boxes.map(box => box.top));
  const right = Math.max(...boxes.map(box => box.right));
  const bottom = Math.max(...boxes.map(box => box.bottom));
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

function graphFitPadding() {
  const shortSide = Math.min(svg?.clientWidth || 0, svg?.clientHeight || 0);
  if (shortSide < 520) return activeTraceId ? 24 : 32;
  if (shortSide < 820) return activeTraceId ? 36 : 48;
  return activeTraceId ? 56 : 74;
}

function graphReadableBaseFontPx() {
  if (graphMode === 'instance') return 10;
  return 11;
}

function graphViewportScale() {
  if (!svg?.clientWidth || !svg?.clientHeight) return 1;
  const viewBox = svg.viewBox?.baseVal;
  const viewBoxWidth = Number(viewBox?.width || 0);
  const viewBoxHeight = Number(viewBox?.height || 0);
  if (!viewBoxWidth || !viewBoxHeight) return 1;
  return Math.max(0.05, Math.min(svg.clientWidth / viewBoxWidth, svg.clientHeight / viewBoxHeight));
}

function isReadableOverviewFit(options = {}) {
  if (options.readableText) return true;
  if (graphMode === 'instance') return !activeTraceId && (selected.kind === 'instanceOverview' || selected.kind === 'none');
  return !activeTraceId && selected.kind === 'none';
}

function readableOverviewAnchorBounds(bounds) {
  if (graphMode !== 'route' || activeTraceId || !(globalLayout.bands || []).length) return bounds;
  const bandLeft = Math.min(...globalLayout.bands.map(band => band.x));
  return { ...bounds, left: Math.min(bounds.right, bandLeft) };
}

function fitGraphToActiveLayout(animate = true, options = {}) {
  if (!zoomBehavior || !svg?.clientWidth || !svg?.clientHeight) return;
  const bounds = activeGraphBounds();
  const padding = graphFitPadding();
  const width = Math.max(320, bounds.width);
  const height = Math.max(220, bounds.height);
  const viewportScale = graphViewportScale();
  const extentScale = clamp(
    Math.min(
      (svg.clientWidth - padding * 2) / (width * viewportScale),
      (svg.clientHeight - padding * 2) / (height * viewportScale)
    ),
    MIN_ZOOM,
    MAX_ZOOM
  );
  const readableScale = FIT_TARGET_TEXT_PX / (graphReadableBaseFontPx() * viewportScale);
  const readableOverviewFit = isReadableOverviewFit(options);
  const scale = clamp(
    readableOverviewFit ? readableScale : Math.max(extentScale, Math.min(readableScale, 1.35)),
    MIN_ZOOM,
    MAX_ZOOM
  );
  const anchorBounds = readableOverviewFit ? readableOverviewAnchorBounds(bounds) : bounds;
  const translateX = readableOverviewFit && scale > extentScale
    ? Math.round((padding / viewportScale) - anchorBounds.left * scale)
    : Math.round(((svg.clientWidth - width * scale * viewportScale) / (2 * viewportScale)) - anchorBounds.left * scale);
  const translateY = readableOverviewFit && scale > extentScale
    ? Math.round((padding / viewportScale) - anchorBounds.top * scale)
    : Math.round(((svg.clientHeight - height * scale * viewportScale) / (2 *viewportScale)) - anchorBounds.top * scale);
  const next = d3.zoomIdentity
    .translate(translateX, translateY)
    .scale(scale);
  const selection = d3.select(svg);
  if (animate) selection.transition().duration(180).call(zoomBehavior.transform, next);
  else selection.call(zoomBehavior.transform, next);
}

function scheduleFitGraph() {
  requestAnimationFrame(() => {
    requestAnimationFrame(() => fitGraphToActiveLayout(false));
  });
}

function syncZoomTransform() {
  if (!zoomBehavior || !window.d3) return;
  d3.select(svg).property('__zoom', d3.zoomIdentity.translate(transform.x, transform.y).scale(transform.k));
}

function setQueueCollapsed(collapsed) {
  document.body.classList.toggle('queue-collapsed', collapsed);
  const app = document.querySelector('.app');
  if (app) {
    const columns = gridPixels('gridTemplateColumns');
    if (columns.length >= 3) {
      const restoredLeft = Number(readLayout().leftWidth || getComputedStyle(document.documentElement).getPropertyValue('--left-width').replace('px', '') || 340);
      const left = collapsed ? 44 : restoredLeft;
      const leftResizer = columns[1] || 0;
      const rightResizer = columns[3] || 0;
      const right = columns[4] || Number(getComputedStyle(document.documentElement).getPropertyValue('--right-width').replace('px', '') || 430);
      const graph = Math.max(320, app.clientWidth - left - leftResizer - rightResizer - right);
      const nextColumns = columns.length >= 5 ? [left, leftResizer, graph, rightResizer, right] : [left, leftResizer, graph];
      app.style.gridTemplateColumns = nextColumns.map(value => `${Math.max(0, Math.round(value))}px`).join(' ');
    }
  }
  const button = document.getElementById('queueToggleBtn');
  if (button) {
    button.textContent = collapsed ? '›' : '‹';
    button.title = collapsed ? '展开核验队列' : '折叠核验队列';
  }
  scheduleFitGraph();
}

function setupQueueToggle() {
  const button = document.getElementById('queueToggleBtn');
  if (!button) return;
  button.addEventListener('click', () => {
    setQueueCollapsed(!document.body.classList.contains('queue-collapsed'));
  });
  setQueueCollapsed(document.body.classList.contains('queue-collapsed'));
}

function setupGraphResizeObserver() {
  const graphWrap = document.querySelector('.graph-wrap');
  if (!graphWrap || typeof ResizeObserver === 'undefined') return;
  const observer = new ResizeObserver(() => scheduleFitGraph());
  observer.observe(graphWrap);
}

function setupGraphZoom() {
  if (!window.d3 || !d3.zoom) {
    throw new Error('d3@7.9.0 is required for route graph audit pan/zoom');
  }
  zoomBehavior = d3.zoom()
    .scaleExtent([MIN_ZOOM, MAX_ZOOM])
    .clickDistance(5)
    .filter(event => {
      if (event.target.closest?.('.graph-tools')) return false;
      if (event.type === 'wheel') return false;
      if (event.type === 'mousedown' && event.button !== 0) return false;
      return true;
    })
    .on('start', () => {
      window.getSelection()?.removeAllRanges();
      document.body.classList.add('panning-graph');
    })
    .on('zoom', event => {
      transform = event.transform;
      applyTransform();
    })
    .on('end', () => {
      document.body.classList.remove('panning-graph');
    });
  d3.select(svg)
    .call(zoomBehavior)
    .call(zoomBehavior.transform, d3.zoomIdentity.translate(transform.x, transform.y).scale(transform.k));
  svg.addEventListener('wheel', handleGraphWheel, { passive: false });
}

function handleGraphWheel(event) {
  if (event.target.closest?.('.graph-tools')) return;
  event.preventDefault();
  if (event.ctrlKey || event.metaKey) {
    zoomAtPoint(event);
    return;
  }
  const delta = wheelDeltaPixels(event);
  const viewportScale = graphViewportScale();
  transform = {
    x: transform.x - delta.x / viewportScale,
    y: transform.y - delta.y / viewportScale,
    k: transform.k,
  };
  applyTransform();
  syncZoomTransform();
}

function wheelDeltaPixels(event) {
  let multiplier = 1;
  if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) multiplier = WHEEL_LINE_HEIGHT_PX;
  else if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) multiplier = Math.max(svg?.clientHeight || 0, 1);
  return {
    x: event.deltaX * multiplier,
    y: event.deltaY * multiplier,
  };
}

function zoomAtPoint(event) {
  const rect = svg.getBoundingClientRect();
  const px = event.clientX - rect.left;
  const py = event.clientY - rect.top;
  const normalizedDelta = clamp(event.deltaY, -180, 180);
  const zoomVelocity = WHEEL_ZOOM_SPEED * Math.max(1, Math.sqrt(transform.k));
  const factor = Math.exp(-normalizedDelta * zoomVelocity);
  const nextK = clamp(transform.k * factor, MIN_ZOOM, MAX_ZOOM);
  if (nextK === transform.k) return;
  const worldX = (px - transform.x) / transform.k;
  const worldY = (py - transform.y) / transform.k;
  transform = {
    x: px - worldX * nextK,
    y: py - worldY * nextK,
    k: nextK,
  };
  applyTransform();
  syncZoomTransform();
}

document.getElementById('fitBtn').addEventListener('click', () => {
  fitGraphToActiveLayout(true, { readableText: true });
});
document.getElementById('routeGraphBtn').addEventListener('click', () => {
  selectRouteOverview();
});
document.getElementById('instanceAuditBtn').addEventListener('click', () => {
  selectInstanceOverview();
});

document.getElementById('instanceViewMode')?.addEventListener('change', event => {
  instanceGraphOptions.viewMode = event.target.value || 'split';
  if (graphMode === 'instance') {
    renderGraph();
    scheduleFitGraph();
    applySearch();
  }
});
document.getElementById('instanceDimLowEdges')?.addEventListener('change', event => {
  instanceGraphOptions.dimLowEdges = Boolean(event.target.checked);
  if (graphMode === 'instance') {
    renderGraph();
    applySearch();
  }
});
document.getElementById('instanceShowLabels')?.addEventListener('change', event => {
  instanceGraphOptions.showLabels = Boolean(event.target.checked);
  if (graphMode === 'instance') {
    renderGraph();
    applySearch();
  }
});

document.getElementById('search').addEventListener('input', () => applySearch());

function applySearch() {
  const q = document.getElementById('search').value.toLowerCase().trim();
  document.querySelectorAll('.node,.edge,.instance-graph-node,.instance-graph-edge').forEach(x => x.classList.remove('search-dim'));
  if (!q) return;
  if (graphMode === 'instance') {
    const matchedInstances = new Set();
    const matchedEdges = new Set();
    for (const [instanceId, instance] of instanceById.entries()) {
      const blob = `${instanceId} ${instance.pageName || ''} ${instance.pageId || ''} ${instance.instanceKey || ''} ${instance.sourceClusterId || ''}`.toLowerCase();
      if (blob.includes(q)) matchedInstances.add(instanceId);
    }
    for (const edge of instanceAudit.instanceEdges || []) {
      const blob = `${edge.id} ${edge.from} ${edge.to} ${(edge.traces || []).join(' ')}`.toLowerCase();
      if (blob.includes(q)) {
        matchedEdges.add(edge.id);
        matchedInstances.add(edge.from);
        matchedInstances.add(edge.to);
      }
    }
    for (const instanceId of instanceById.keys()) {
      if (!matchedInstances.has(instanceId)) document.getElementById(`instance-node-${CSS.escape(instanceId)}`)?.classList.add('search-dim');
    }
    for (const edge of instanceAudit.instanceEdges || []) {
      if (!matchedEdges.has(edge.id) && !matchedInstances.has(edge.from) && !matchedInstances.has(edge.to)) {
        document.getElementById(`instance-edge-${CSS.escape(edge.id)}`)?.classList.add('search-dim');
      }
    }
    return;
  }
  const matchedNodes = new Set();
  const matchedEdges = new Set();
  for (const n of DATA.nodes) {
    const blob = `${n.id} ${n.name} ${n.routePath} ${n.domain}`.toLowerCase();
    if (blob.includes(q)) matchedNodes.add(n.id);
  }
  for (const e of DATA.edges) {
    const blob = `${e.id} ${e.from} ${e.to} ${e.label} ${e.kind}`.toLowerCase();
    if (blob.includes(q)) {
      matchedEdges.add(e.id);
      matchedNodes.add(e.from);
      matchedNodes.add(e.to);
    }
  }
  for (const n of DATA.nodes) {
    if (!matchedNodes.has(n.id)) document.getElementById(`node-${CSS.escape(n.id)}`)?.classList.add('search-dim');
  }
  for (const e of DATA.edges) {
    if (!matchedEdges.has(e.id) && !matchedNodes.has(e.from) && !matchedNodes.has(e.to)) {
      document.getElementById(`edge-${CSS.escape(e.id)}`)?.classList.add('search-dim');
    }
  }
}

function applyTraceHighlight() {
  queueTraceLayout();
  updateTraceGeometry();
  renderTraceStepLabels();
  renderTraceNodeActions();
  document.querySelectorAll('.node,.edge').forEach(x => {
    x.classList.remove('trace-dim');
    x.classList.remove('trace-active');
  });
  document.querySelectorAll('.instance-graph-node,.instance-graph-edge').forEach(x => {
    x.classList.remove('trace-dim');
    x.classList.remove('trace-active');
  });
  if (!activeTraceId) return;
  if (graphMode === 'instance') {
    const flow = instanceFlowByTrace.get(activeTraceId);
    const instanceSet = new Set(flow?.instanceIds || []);
    const edgeSet = new Set(flow?.edgeIds || []);
    for (const instanceId of instanceById.keys()) {
      const elem = document.getElementById(`instance-node-${CSS.escape(instanceId)}`);
      if (!elem) continue;
      if (instanceSet.has(instanceId)) elem.classList.add('trace-active');
      else elem.classList.add('trace-dim');
    }
    for (const edge of instanceAudit.instanceEdges || []) {
      const elem = document.getElementById(`instance-edge-${CSS.escape(edge.id)}`);
      if (!elem) continue;
      if (edgeSet.has(edge.id)) elem.classList.add('trace-active');
      else elem.classList.add('trace-dim');
    }
    return;
  }
  const trace = traceById.get(activeTraceId);
  if (!trace) return;
  let nodeSet = new Set(trace.nodeIds || []);
  let edgeSet = new Set(trace.edgeIds || []);
  const transition = activeTransitionIndex === null ? null : (trace.transitions || [])[activeTransitionIndex];
  if (transition) {
    nodeSet = new Set([transition.from, transition.to]);
    edgeSet = new Set([transition.edgeId]);
  }
  for (const node of DATA.nodes) {
    const elem = document.getElementById(`node-${CSS.escape(node.id)}`);
    if (!elem) continue;
    if (nodeSet.has(node.id)) elem.classList.add('trace-active');
    else elem.classList.add('trace-dim');
  }
  for (const edge of DATA.edges) {
    const elem = document.getElementById(`edge-${CSS.escape(edge.id)}`);
    const label = document.getElementById(`edge-label-${CSS.escape(edge.id)}`);
    if (!elem) continue;
    if (edgeSet.has(edge.id)) {
      elem.classList.add('trace-active');
      label?.classList.add('visible');
    } else {
      elem.classList.add('trace-dim');
      label?.classList.remove('visible');
    }
  }
}

function selectTrace(traceId, options = {}) {
  if (!options.keepStack) contextStack = [];
  activeTraceId = traceId || '';
  activeTransitionIndex = null;
  reviewContext = activeTraceId ? { mode: 'trace', traceId: activeTraceId } : { mode: 'none' };
  selected = activeTraceId ? { kind: 'trace', id: activeTraceId } : { kind: 'none', id: '' };
  if (graphMode === 'instance') {
    renderGraph();
    scheduleFitGraph();
    applySearch();
  } else {
    applyTraceHighlight();
    applySelection();
  }
  if (!activeTraceId) {
    renderTraceStepLabels();
    renderFilmstrip([]);
    document.getElementById('inspector').innerHTML = '<div class="panel"><h2>未选择</h2><div class="muted">请选择一个风险项、页面节点、路由边或 Trace。</div></div>';
    return;
  }
  const trace = traceById.get(activeTraceId);
  if (!trace) return;
  renderTraceInspector(trace);
  renderFilmstrip(trace.steps || []);
  restoreScrollState(options.restoreScroll);
}

function selectTraceTransition(traceId, transitionIndex, options = {}) {
  if (!options.keepStack) contextStack = [];
  const trace = traceById.get(traceId);
  if (!trace) return;
  const transition = (trace.transitions || [])[transitionIndex];
  if (!transition) return;
  activeTraceId = traceId;
  activeTransitionIndex = transitionIndex;
  reviewContext = { mode: 'traceTransition', traceId, transitionIndex };
  selected = { kind: 'traceTransition', traceId, transitionIndex };
  const select = document.getElementById('traceSelect');
  if (select) select.value = traceId;
  if (graphMode === 'instance') {
    renderGraph();
    scheduleFitGraph();
    applySearch();
  } else {
    applyTraceHighlight();
    applySelection();
  }
  renderTraceInspector(trace);
  renderFilmstrip(transitionSteps(transition));
  restoreScrollState(options.restoreScroll);
}

function hoverEdge(id, on) {
  document.getElementById(`edge-${CSS.escape(id)}`)?.classList.toggle('hovered', on);
  document.getElementById(`edge-label-${CSS.escape(id)}`)?.classList.toggle('visible', on);
}

function hoverInstanceEdge(id, on) {
  document.getElementById(`instance-edge-${CSS.escape(id)}`)?.classList.toggle('hovered', on);
  document.getElementById(`instance-edge-label-${CSS.escape(id)}`)?.classList.toggle('visible', on);
}

function hoverNode(id, on) {
  document.getElementById(`node-${CSS.escape(id)}`)?.classList.toggle('hovered', on);
}

function applySelection() {
  document.querySelectorAll('.selected').forEach(x => x.classList.remove('selected'));
  document.querySelectorAll('.edge-label.visible').forEach(x => x.classList.remove('visible'));
  if (selected.kind === 'node') {
    document.getElementById(`node-${CSS.escape(selected.id)}`)?.classList.add('selected');
    for (const e of DATA.edges) {
      if (e.from === selected.id || e.to === selected.id) {
        document.getElementById(`edge-${CSS.escape(e.id)}`)?.classList.add('selected');
      }
    }
  }
  if (selected.kind === 'edge') {
    const edge = edgeById.get(selected.id);
    document.getElementById(`edge-${CSS.escape(selected.id)}`)?.classList.add('selected');
    document.getElementById(`edge-label-${CSS.escape(selected.id)}`)?.classList.add('visible');
    if (edge) {
      document.getElementById(`node-${CSS.escape(edge.from)}`)?.classList.add('selected');
      document.getElementById(`node-${CSS.escape(edge.to)}`)?.classList.add('selected');
    }
  }
  if (selected.kind === 'instance') {
    const entry = instanceById.get(selected.id);
    const clusterId = entry?.sourceClusterId;
    if (clusterId) document.getElementById(`node-${CSS.escape(clusterId)}`)?.classList.add('selected');
    document.getElementById(`instance-node-${CSS.escape(selected.id)}`)?.classList.add('selected');
  }
  if (selected.kind === 'instanceEdge') {
    const edge = instanceEdgeById.get(selected.id);
    document.getElementById(`instance-edge-${CSS.escape(selected.id)}`)?.classList.add('selected');
    document.getElementById(`instance-edge-label-${CSS.escape(selected.id)}`)?.classList.add('visible');
    if (edge) {
      document.getElementById(`instance-node-${CSS.escape(edge.from)}`)?.classList.add('selected');
      document.getElementById(`instance-node-${CSS.escape(edge.to)}`)?.classList.add('selected');
    }
  }
  if (selected.kind === 'traceTransition') {
    const trace = traceById.get(selected.traceId);
    const transition = trace ? (trace.transitions || [])[selected.transitionIndex] : null;
    if (!transition) return;
    document.getElementById(`edge-${CSS.escape(transition.edgeId)}`)?.classList.add('selected');
    document.getElementById(`edge-label-${CSS.escape(transition.edgeId)}`)?.classList.add('visible');
    document.getElementById(`node-${CSS.escape(transition.from)}`)?.classList.add('selected');
    document.getElementById(`node-${CSS.escape(transition.to)}`)?.classList.add('selected');
  }
}

function selectNode(id) {
  const wasInstanceMode = graphMode === 'instance';
  graphMode = 'route';
  document.getElementById('routeGraphBtn')?.classList.add('mode-active');
  document.getElementById('instanceAuditBtn')?.classList.remove('mode-active');
  activeTraceId = '';
  activeTransitionIndex = null;
  reviewContext = { mode: 'node', nodeId: id };
  selected = { kind: 'node', id };
  if (wasInstanceMode) renderGraph();
  applyTraceHighlight();
  scheduleFitGraph();
  applySelection();
  renderNodeInspector(nodeById.get(id));
  renderFilmstrip(nodeById.get(id)?.examples || []);
}

function selectEdge(id) {
  const wasInstanceMode = graphMode === 'instance';
  graphMode = 'route';
  document.getElementById('routeGraphBtn')?.classList.add('mode-active');
  document.getElementById('instanceAuditBtn')?.classList.remove('mode-active');
  activeTraceId = '';
  activeTransitionIndex = null;
  reviewContext = { mode: 'edge', edgeId: id };
  selected = { kind: 'edge', id };
  if (wasInstanceMode) renderGraph();
  applyTraceHighlight();
  scheduleFitGraph();
  applySelection();
  const edge = edgeById.get(id);
  renderEdgeInspector(edge);
  const steps = [];
  for (const route of edge.routes || []) {
    for (const triplet of route.evidenceTriplets || []) {
      if (triplet.fromStep?.key) steps.push(triplet.fromStep);
      for (const b of triplet.bridgeSteps || []) steps.push(b);
      if (triplet.toStep?.key) steps.push(triplet.toStep);
    }
  }
  for (const ex of edge.observed?.bridgeExamples || []) {
    if (stepByKey[ex.fromStep]) steps.push(stepByKey[ex.fromStep]);
    for (const b of ex.bridgeSteps || []) steps.push(b);
    if (stepByKey[ex.toStep]) steps.push(stepByKey[ex.toStep]);
  }
  renderFilmstrip(steps.slice(0, 18));
}

function selectRouteOverview() {
  graphMode = 'route';
  document.getElementById('routeGraphBtn')?.classList.add('mode-active');
  document.getElementById('instanceAuditBtn')?.classList.remove('mode-active');
  activeTraceId = '';
  activeTransitionIndex = null;
  reviewContext = { mode: 'none' };
  selected = { kind: 'none', id: '' };
  contextStack = [];
  const select = document.getElementById('traceSelect');
  if (select) select.value = '';
  renderGraph();
  applyTraceHighlight();
  applySelection();
  scheduleFitGraph();
  renderFilmstrip([]);
  document.getElementById('inspector').innerHTML = '<div class="panel"><h2>未选择</h2><div class="muted">请选择一个风险项、页面节点、路由边或 Trace。</div></div>';
}

function selectInstanceOverview() {
  graphMode = 'instance';
  document.getElementById('routeGraphBtn')?.classList.remove('mode-active');
  document.getElementById('instanceAuditBtn')?.classList.add('mode-active');
  activeTraceId = '';
  activeTransitionIndex = null;
  reviewContext = { mode: 'instanceOverview' };
  selected = { kind: 'instanceOverview', id: '' };
  contextStack = [];
  const select = document.getElementById('traceSelect');
  if (select) select.value = '';
  renderGraph();
  applyTraceHighlight();
  applySelection();
  scheduleFitGraph();
  renderInstanceOverview();
  renderFilmstrip([]);
}

function instanceFilmstripSteps(entry) {
  const planning = entry.matchedPlanning || {};
  const steps = [];
  const seen = new Set();
  const pushStep = (key, fallback = {}) => {
    const stepKey = String(key || '').trim();
    if (!stepKey || seen.has(stepKey)) return;
    seen.add(stepKey);
    const base = stepByKey[stepKey] || {};
    steps.push({
      ...fallback,
      ...base,
      key: base.key || stepKey,
      screenshot: base.screenshot || fallback.screenshot || '',
      action: base.action || fallback.action || 'Instance evidence',
      pageId: base.pageId || entry.sourceClusterId || '',
      instanceId: entry.instanceId,
      instanceKey: entry.instanceKey,
      instanceName: entry.pageName,
    });
  };
  for (const key of planning.obs_keys || []) {
    pushStep(key, { action: 'Instance planning evidence' });
  }
  const assignedKeys = Object.entries(instanceAudit.stepToInstance || {})
    .filter(([, assignedInstanceId]) => assignedInstanceId === entry.instanceId)
    .map(([key]) => key)
    .sort((a, b) => {
      const stepA = stepByKey[a] || {};
      const stepB = stepByKey[b] || {};
      const traceCompare = String(stepA.traceId || a).localeCompare(String(stepB.traceId || b));
      if (traceCompare) return traceCompare;
      return (stepA.stepIndex ?? parseStepIndex(a)) - (stepB.stepIndex ?? parseStepIndex(b));
    });
  for (const key of assignedKeys) {
    pushStep(key, { action: 'Instance assigned step' });
  }
  for (const evidence of entry.evidenceSteps || []) {
    pushStep(evidence.stepKey || evidence.screenshotPath || evidence.screenshot, {
      screenshot: evidence.screenshot || '',
      action: evidence.stepKey ? 'AppSpec screenshot evidence' : '',
    });
  }
  return steps;
}

function selectInstance(instanceId) {
  const entry = instanceById.get(instanceId);
  if (!entry) return;
  graphMode = 'instance';
  document.getElementById('routeGraphBtn')?.classList.remove('mode-active');
  document.getElementById('instanceAuditBtn')?.classList.add('mode-active');
  activeTraceId = '';
  activeTransitionIndex = null;
  reviewContext = { mode: 'instance', instanceId };
  selected = { kind: 'instance', id: instanceId };
  renderGraph();
  applyTraceHighlight();
  applySelection();
  renderInstanceInspector(entry);
  renderFilmstrip(instanceFilmstripSteps(entry));
}

function selectInstanceEdge(edgeId) {
  const edge = instanceEdgeById.get(edgeId);
  if (!edge) return;
  graphMode = 'instance';
  document.getElementById('routeGraphBtn')?.classList.remove('mode-active');
  document.getElementById('instanceAuditBtn')?.classList.add('mode-active');
  activeTraceId = '';
  activeTransitionIndex = null;
  reviewContext = { mode: 'instanceEdge', edgeId };
  selected = { kind: 'instanceEdge', id: edgeId };
  renderGraph();
  applyTraceHighlight();
  applySelection();
  renderInstanceEdgeInspector(edge);
  const steps = [];
  for (const example of edge.examples || []) {
    if (stepByKey[example.fromStep]) steps.push(stepByKey[example.fromStep]);
    if (stepByKey[example.toStep]) steps.push(stepByKey[example.toStep]);
  }
  renderFilmstrip(steps.slice(0, 16));
}

function openNodeFromTrace(nodeId, traceId, transitionIndex = null) {
  contextStack.push({
    mode: transitionIndex === null ? 'trace' : 'traceTransition',
    traceId,
    transitionIndex,
    scroll: currentScrollState(),
  });
  reviewContext = { mode: 'node', nodeId };
  selected = { kind: 'node', id: nodeId };
  activeTraceId = traceId;
  activeTransitionIndex = transitionIndex;
  applyTraceHighlight();
  applySelection();
  renderNodeInspector(nodeById.get(nodeId));
  renderFilmstrip(nodeById.get(nodeId)?.examples || []);
}

function openEdgeFromTrace(edgeId, traceId, transitionIndex) {
  const returnMode = reviewContext.mode === 'traceTransition' && reviewContext.traceId === traceId
    ? 'traceTransition'
    : 'trace';
  contextStack.push(returnMode === 'traceTransition'
    ? {
        mode: 'traceTransition',
        traceId,
        transitionIndex: reviewContext.transitionIndex,
        scroll: currentScrollState(),
      }
    : {
        mode: 'trace',
        traceId,
        scroll: currentScrollState(),
      });
  reviewContext = { mode: 'edge', edgeId };
  selected = { kind: 'edge', id: edgeId };
  activeTraceId = traceId;
  activeTransitionIndex = transitionIndex;
  applyTraceHighlight();
  applySelection();
  const edge = edgeById.get(edgeId);
  renderEdgeInspector(edge);
  const trace = traceById.get(traceId);
  const transition = trace ? (trace.transitions || [])[transitionIndex] : null;
  renderFilmstrip(transitionSteps(transition));
}

function annotationKey() {
  if (reviewContext.mode === 'trace') return `trace:${reviewContext.traceId}`;
  if (reviewContext.mode === 'traceTransition') return traceTransitionKey(reviewContext.traceId, reviewContext.transitionIndex);
  if (reviewContext.mode === 'edge') return `edge:${reviewContext.edgeId}`;
  if (reviewContext.mode === 'node') return `node:${reviewContext.nodeId}`;
  if (reviewContext.mode === 'step') return `step:${reviewContext.stepKey}`;
  if (reviewContext.mode === 'instance') return `instance:${reviewContext.instanceId}`;
  if (reviewContext.mode === 'instanceEdge') return `instanceEdge:${reviewContext.edgeId}`;
  return selected.kind === 'none' ? '' : `${selected.kind}:${selected.id}`;
}

function markAnnotationsDirty() {
  annotationsDirty = true;
  renderAnnotationSaveStatus();
}

function setAnnotationSaveStatus(message, state = '') {
  const status = document.getElementById('annotationSaveStatus');
  if (!status) return;
  status.textContent = message;
  status.classList.toggle('dirty', state === 'dirty' || state === 'error');
  status.classList.toggle('saved', state === 'saved');
}

function renderAnnotationSaveStatus() {
  const target = serverSaveEnabled ? '服务器 feedback JSON' : 'Feedback JSON';
  setAnnotationSaveStatus(
    annotationsDirty ? `${target} 未保存（${Object.keys(annotations).length} 条）` : `${target} 已同步（${Object.keys(annotations).length} 条）`,
    annotationsDirty ? 'dirty' : 'saved',
  );
}

function rerenderReviewContext() {
  if (reviewContext.mode === 'trace' || reviewContext.mode === 'traceTransition') {
    const trace = traceById.get(reviewContext.traceId);
    if (trace) renderTraceInspector(trace);
  } else if (reviewContext.mode === 'edge') {
    renderEdgeInspector(edgeById.get(reviewContext.edgeId));
  } else if (reviewContext.mode === 'node') {
    renderNodeInspector(nodeById.get(reviewContext.nodeId));
  } else if (reviewContext.mode === 'step') {
    renderStepInspector(stepByKey[reviewContext.stepKey]);
  } else if (reviewContext.mode === 'instance') {
    const entry = instanceById.get(reviewContext.instanceId);
    if (entry) renderInstanceInspector(entry);
  } else if (reviewContext.mode === 'instanceEdge') {
    const edge = instanceEdgeById.get(reviewContext.edgeId);
    if (edge) renderInstanceEdgeInspector(edge);
  } else if (reviewContext.mode === 'globalRouteFeedback') {
    renderGlobalRouteFeedbackPanel();
  }
}

function annotate(value, explicitKey = '') {
  const key = explicitKey || annotationKey();
  if (!key) return;
  annotations[key] = { ...(annotations[key] || {}), value, at: new Date().toISOString() };
  markAnnotationsDirty();
  rerenderReviewContext();
}

function saveTraceNote(traceId, note) {
  if (!traceId) return;
  const key = traceNoteKey(traceId);
  const text = String(note || '').trim();
  if (text) annotations[key] = { ...(annotations[key] || {}), note: text, at: new Date().toISOString() };
  else delete annotations[key];
  markAnnotationsDirty();
}

function traceNoteValue(traceId) {
  return annotations[traceNoteKey(traceId)]?.note || '';
}

function globalRouteFeedbackContainer() {
  const current = annotations[GLOBAL_ROUTE_FEEDBACK_KEY];
  if (current && !Array.isArray(current) && typeof current === 'object') {
    if (!Array.isArray(current.items)) current.items = [];
    return current;
  }
  const next = { schema_version: 'route_review_global_feedback.v1', items: [] };
  annotations[GLOBAL_ROUTE_FEEDBACK_KEY] = next;
  return next;
}

function globalRouteFeedbackItems() {
  return globalRouteFeedbackContainer().items;
}

function pageOptionHtml(selectedId = '') {
  return DATA.nodes.map(node => {
    const label = `${node.name || node.id} (${node.id})`;
    const selectedAttr = node.id === selectedId ? ' selected' : '';
    return `<option value="${html(node.id)}"${selectedAttr}>${html(label)}</option>`;
  }).join('');
}

function addGlobalRouteFeedback(sourceId, targetId, note) {
  const source = String(sourceId || '').trim();
  const target = String(targetId || '').trim();
  if (!source || !target) {
    alert('source 和 target 不能为空');
    return;
  }
  if (source === target) {
    alert('source 和 target 不能相同');
    return;
  }
  const container = globalRouteFeedbackContainer();
  container.items.push({
    operation: 'merge_page_clusters',
    source_page_cluster_id: source,
    target_page_cluster_id: target,
    scope: 'all_steps',
    note: String(note || '').trim(),
    at: new Date().toISOString(),
  });
  markAnnotationsDirty();
  renderGlobalRouteFeedbackPanel(source);
}

function addGlobalRouteFeedbackFromForm() {
  const source = document.getElementById('globalRouteFeedbackSource')?.value || '';
  const target = document.getElementById('globalRouteFeedbackTarget')?.value || '';
  const note = document.getElementById('globalRouteFeedbackNote')?.value || '';
  addGlobalRouteFeedback(source, target, note);
}

function addGlobalRouteFeedbackFromNode(sourceId) {
  const target = document.getElementById(`mergeTarget-${CSS.escape(sourceId)}`)?.value || '';
  const note = document.getElementById(`mergeNote-${CSS.escape(sourceId)}`)?.value || '';
  addGlobalRouteFeedback(sourceId, target, note);
}

function deleteGlobalRouteFeedback(index) {
  const items = globalRouteFeedbackItems();
  if (index < 0 || index >= items.length) return;
  items.splice(index, 1);
  markAnnotationsDirty();
  renderGlobalRouteFeedbackPanel();
}

function renderGlobalRouteFeedbackList(filterPageId = '') {
  const items = globalRouteFeedbackItems();
  const visible = filterPageId
    ? items.map((item, index) => ({ item, index })).filter(entry =>
        entry.item.source_page_cluster_id === filterPageId || entry.item.target_page_cluster_id === filterPageId)
    : items.map((item, index) => ({ item, index }));
  if (!visible.length) return '<div class="muted">暂无 global route feedback。</div>';
  return visible.map(({ item, index }) => `
    <div class="route-feedback-item">
      <div><strong>${html(item.operation || 'merge_page_clusters')}</strong></div>
      <div><code>${html(item.source_page_cluster_id || '')}</code> -> <code>${html(item.target_page_cluster_id || '')}</code></div>
      <div class="small">${html(item.note || '')}</div>
      <div class="annotation-row">
        <button onclick="deleteGlobalRouteFeedback(${index})">删除</button>
      </div>
    </div>
  `).join('');
}

function renderGlobalRouteFeedbackPanel(defaultSourceId = '') {
  reviewContext = { mode: 'globalRouteFeedback' };
  selected = { kind: 'none', id: '' };
  activeTraceId = '';
  activeTransitionIndex = null;
  applyTraceHighlight();
  applySelection();
  document.getElementById('inspector').innerHTML = `<div class="panel">
    <h2>Global Route Feedback</h2>
    <p class="small muted">用于表达跨 trace 的 page cluster 级人工裁决，例如“这个页面簇整体应该并入另一个页面簇”。Trace-wise feedback 仍通过具体 trace/edge/step 的按钮与备注保存。</p>
    <div class="route-feedback-form">
      <label class="small">Source page cluster</label>
      <select id="globalRouteFeedbackSource">${pageOptionHtml(defaultSourceId)}</select>
      <label class="small">Target page cluster</label>
      <select id="globalRouteFeedbackTarget">${pageOptionHtml('')}</select>
      <label class="small">Reason</label>
      <textarea id="globalRouteFeedbackNote" placeholder="说明为什么 source 应整体并入 target"></textarea>
      <button onclick="addGlobalRouteFeedbackFromForm()">添加 page merge feedback</button>
    </div>
    <h3>已记录的 global route feedback</h3>
    ${renderGlobalRouteFeedbackList()}
  </div>`;
}

function renderAnnotationControls(explicitKey = '', labels = DEFAULT_ANNOTATION_LABELS) {
  const key = explicitKey || annotationKey();
  const current = key && annotations[key] ? annotations[key].value : '未核验';
  return `<div class="annotation-row">
    <span class="pill">${html(current)}</span>
    ${labels.map(v => `<button class="annotation-btn" data-annotation-value="${html(v)}" data-annotation-key="${html(key)}">${html(v)}</button>`).join('')}
  </div>`;
}

function annotationLabelsForEdge(edge) {
  return edge?.kind === 'missing_observed' ? MISSING_OBSERVED_EDGE_LABELS : DEFAULT_ANNOTATION_LABELS;
}

function annotationHintForEdge(edge) {
  if (edge?.kind !== 'missing_observed') return '';
  return `<div class="small muted">
    这条边在 trace 里真实出现了 ${html(edge.observedCount)} 次，但 canonical_routes 没有对应 route。
    如果它应该由业务动作解释，点“确认缺失：应补 canonical route”；
    如果它其实不是跨页面路由，点“非缺失”或“接受现状”的具体原因。
  </div>`;
}

async function saveAnnotationsToServer() {
  const response = await fetch(SERVER_ANNOTATION_API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(annotations, null, 2),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(text);
  return JSON.parse(text || '{}');
}

async function loadAnnotationsFromServer() {
  const response = await fetch(SERVER_ANNOTATION_API, { cache: 'no-store' });
  if (!response.ok) throw new Error(await response.text());
  const loaded = await response.json();
  if (!loaded || Array.isArray(loaded) || typeof loaded !== 'object') throw new Error('server feedback JSON 必须是对象');
  annotations = loaded;
  annotationsDirty = false;
  renderAnnotationSaveStatus();
  rerenderReviewContext();
}

async function acceptAuditedReasoning() {
  if (!serverAcceptAuditEnabled) throw new Error('当前 HTML 不是由 audit server 提供，不能直接固化 audited reasoning');
  const confirmed = window.confirm(
    '确认当前 page-assignment audit 没有问题？这会把 global_reasoning_page_assignment_audited.json 固化为 global_reasoning_feedback_fixed.json，用于下游 pipeline。'
  );
  if (!confirmed) return;
  setAnnotationSaveStatus('正在固化 audited reasoning...', 'dirty');
  const response = await fetch(SERVER_ACCEPT_AUDIT_API, { method: 'POST' });
  const text = await response.text();
  if (!response.ok) throw new Error(text);
  const result = JSON.parse(text || '{}');
  setAnnotationSaveStatus(`已固化 feedback fixed: ${result.target || ''}`, 'saved');
}

function downloadAnnotationsFile(text) {
  const blob = new Blob([text], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = ANNOTATION_FILE_NAME;
  a.click();
  URL.revokeObjectURL(url);
}

async function saveAnnotationsFile() {
  const text = JSON.stringify(annotations, null, 2);
  setAnnotationSaveStatus(`正在保存 ${Object.keys(annotations).length} 条 feedback...`, 'dirty');
  let serverResult = null;
  if (serverSaveEnabled) {
    serverResult = await saveAnnotationsToServer();
  } else if (window.showSaveFilePicker) {
    const handle = await window.showSaveFilePicker({
      suggestedName: ANNOTATION_FILE_NAME,
      types: [{ description: 'Route review annotations', accept: { 'application/json': ['.json'] } }],
    });
    const writable = await handle.createWritable();
    await writable.write(text);
    await writable.close();
  } else {
    downloadAnnotationsFile(text);
  }
  annotationsDirty = false;
  if (serverResult && serverResult.path) {
    setAnnotationSaveStatus(`已保存 ${serverResult.count ?? Object.keys(annotations).length} 条到 ${serverResult.path}`, 'saved');
  } else {
    renderAnnotationSaveStatus();
  }
}

async function importAnnotationsFile(file) {
  if (!file) return;
  const text = await file.text();
  const loaded = JSON.parse(text || '{}');
  if (!loaded || Array.isArray(loaded) || typeof loaded !== 'object') throw new Error('feedback JSON 必须是对象');
  annotations = loaded;
  annotationsDirty = false;
  renderAnnotationSaveStatus();
  rerenderReviewContext();
}

function screenshotFrame(src, alt, step) {
  if (!src) return '';
  const skipReason = step?.skipReason || '';
  const skipClass = skipReason ? ' is-skip' : '';
  return `<div class="screenshot-frame${skipClass}">
    <img src="${html(src)}" loading="lazy" alt="${html(alt || step?.key || 'screenshot')}">
    ${skipReason ? `<span class="skip-ribbon">SKIP</span><span class="skip-reason">${html(skipReason)}</span>` : ''}
  </div>`;
}

function shot(step, title) {
  if (!step || !step.key) return `<div class="shot"><strong>${html(title)}</strong><div class="muted">无 step</div></div>`;
  return `<div class="shot">
    ${screenshotFrame(step.screenshot, step.key, step)}
    <strong>${html(title)} · ${html(step.key)}</strong>
    <div>${html(short(step.action, 120))}</div>
    ${step.skipReason ? `<span class="pill warn">skip: ${html(step.skipReason)}</span>` : ''}
    ${step.pageId ? `<span class="pill">${html(step.pageId)}</span>` : ''}
  </div>`;
}

function entityCountsHtml(entityCounts) {
  const entries = Object.entries(entityCounts || {}).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (!entries.length) return '<span class="entity-chip">no entities</span>';
  return entries.map(([name, count]) => `<span class="entity-chip">${html(name)} x${html(count)}</span>`).join('');
}

function renderInstanceOverview() {
  const summary = instanceAudit.summary || {};
  const groups = instanceAudit.sourceClusters || [];
  const splitGroups = groups.filter(group => (group.instances || []).length > 1);
  const body = (splitGroups.length ? splitGroups : groups).map(group => {
    const warn = group.isSuspicious ? ' warn' : '';
    const instances = (group.instances || []).map(instance => `
      <button class="instance-card" onclick="selectInstance('${html(instance.instanceId)}')">
        <div><strong>${html(instance.pageName || instance.pageId)}</strong></div>
        <div class="small muted"><code>${html(instance.pageId)}</code> · key=${html(instance.instanceKey || '(none)')}</div>
        <div class="small">${html(instance.routePath || '')}</div>
        <div class="entity-counts">${entityCountsHtml(instance.entityCounts)}</div>
      </button>
    `).join('');
    return `<div class="instance-group${warn}">
      <div class="instance-group-head">
        <div><strong>${html(group.canonicalName)}</strong> <code>${html(group.clusterId)}</code></div>
        <div class="small muted">${html(group.canonicalType || '')} · ${html(group.routePath || '')}</div>
        <span class="pill">${html(group.materializedCount)} instance</span>
        ${group.isSuspicious ? `<span class="pill warn">可疑拆分: ${html(group.suspiciousReason)}</span>` : ''}
        ${group.planning?.extraction_mode ? `<span class="pill">planning: ${html(group.planning.extraction_mode)}</span>` : ''}
      </div>
      <div class="instance-list">${instances}</div>
    </div>`;
  }).join('');
  document.getElementById('inspector').innerHTML = `<div class="panel">
    <h2>Page→Instance 划分</h2>
    <div class="kv">
      <div>materialized pages</div><div>${html(summary.materializedPages || 0)}</div>
      <div>source clusters</div><div>${html(summary.sourceClusters || 0)}</div>
      <div>split clusters</div><div>${html(summary.splitClusters || 0)}</div>
      <div>instance edges</div><div>${html(summary.instanceEdges || 0)}</div>
      <div>suspicious splits</div><div>${html(summary.suspiciousSplits || 0)}</div>
    </div>
    <p class="small muted">这里展示 canonical page cluster 如何被 materialized 成一个或多个 page instance。点击 instance 可以查看 split planning、entity evidence 和截图证据。</p>
    ${body || '<div class="empty">没有 AppSpec page instance 数据。请先执行 page→instance 划分并生成 phase_consolidation/site_model 与 phase_instance_splitting。</div>'}
  </div>`;
}

function renderInstanceInspector(entry) {
  const group = entry.group || {};
  const planning = entry.matchedPlanning || {};
  const evidence = (entry.evidenceSteps || []).slice(0, 10).map((item, index) => {
    const step = stepByKey[item.stepKey] || {};
    const viewStep = { ...step, key: item.stepKey || step.key || '', screenshot: item.screenshot || step.screenshot || '' };
    return `<div class="shot">
      ${screenshotFrame(viewStep.screenshot, item.stepKey || entry.pageId, viewStep)}
      <strong>${html(item.stepKey || `evidence ${index + 1}`)}</strong>
      <div>${html(short(step.action || 'AppSpec screenshot evidence', 110))}</div>
      ${viewStep.skipReason ? `<span class="pill warn">skip: ${html(viewStep.skipReason)}</span>` : ''}
    </div>`;
  }).join('');
  const samples = Object.entries(entry.entitySamples || {}).slice(0, 8).map(([entity, labels]) => `
    <div class="small"><strong>${html(entity)}</strong>: ${html((labels || []).join(' / '))}</div>
  `).join('');
  document.getElementById('inspector').innerHTML = `<div class="panel">
    <button onclick="selectInstanceOverview()">返回 Page→Instance</button>
    ${nodeById.has(entry.sourceClusterId) ? `<button onclick="selectNode('${html(entry.sourceClusterId)}')">打开 source page cluster</button>` : ''}
    <h2>${html(entry.pageName || entry.pageId)}</h2>
    <div class="kv">
      <div>page_id</div><div><code>${html(entry.pageId)}</code></div>
      <div>source_cluster</div><div><code>${html(entry.sourceClusterId)}</code></div>
      <div>instance_key</div><div><code>${html(entry.instanceKey || '(none)')}</code></div>
      <div>route_path</div><div><code>${html(entry.routePath || '')}</code></div>
      <div>filename</div><div><code>${html(entry.filename || '')}</code></div>
      <div>split_mode</div><div>${html(group.splitMode || '')}</div>
      <div>planning_mode</div><div>${html(group.planning?.extraction_mode || '')}</div>
      <div>matched_label</div><div>${html(planning.instance_label || '')}</div>
      <div>obs_keys</div><div>${html((planning.obs_keys || []).join(', '))}</div>
    </div>
    ${group.isSuspicious ? `<p><span class="pill warn">可疑拆分</span> ${html(group.suspiciousReason)}</p>` : ''}
    ${renderAnnotationControls()}
    <h3>Entity Evidence</h3>
    <div class="entity-counts">${entityCountsHtml(entry.entityCounts)}</div>
    ${samples || '<div class="muted">没有 entity sample labels</div>'}
    <h3>Screenshot Evidence</h3>
    <div class="evidence-list">${evidence || '<div class="muted">没有截图证据</div>'}</div>
  </div>`;
}

function renderInstanceEdgeInspector(edge) {
  const from = instanceById.get(edge.from) || {};
  const to = instanceById.get(edge.to) || {};
  const examples = (edge.examples || []).slice(0, 8).map(example => {
    const fromStep = stepByKey[example.fromStep] || {};
    const toStep = stepByKey[example.toStep] || {};
    return `<div class="step-card">
      <div class="small"><strong>${html(example.traceId || '')}</strong></div>
      <div class="small">from: <code>${html(example.fromStep || '')}</code> ${html(short(fromStep.action || '', 80))}</div>
      <div class="small">to: <code>${html(example.toStep || '')}</code> ${html(short(toStep.action || example.action || '', 80))}</div>
    </div>`;
  }).join('');
  document.getElementById('inspector').innerHTML = `<div class="panel">
    <button onclick="selectInstanceOverview()">返回 Page→Instance</button>
    <h2>Instance Transition</h2>
    <div class="kv">
      <div>edge</div><div><code>${html(edge.id)}</code></div>
      <div>from</div><div><button onclick="selectInstance('${html(edge.from)}')">${html(from.pageName || edge.from)}</button></div>
      <div>to</div><div><button onclick="selectInstance('${html(edge.to)}')">${html(to.pageName || edge.to)}</button></div>
      <div>observed_count</div><div>${html(edge.count || 0)}</div>
      <div>traces</div><div>${html((edge.traces || []).join(', '))}</div>
    </div>
    ${renderAnnotationControls()}
    <h3>Trace Evidence</h3>
    <div class="evidence-list">${examples || '<div class="muted">没有 transition evidence</div>'}</div>
  </div>`;
}

function renderNodeInspector(node) {
  if (!node) return;
  const instanceGroup = instanceGroupByCluster.get(node.id);
  const instanceSummary = instanceGroup ? `
    <h3>Page→Instance</h3>
    <span class="pill">${html(instanceGroup.materializedCount)} instance</span>
    ${instanceGroup.isSuspicious ? `<span class="pill warn">可疑拆分: ${html(instanceGroup.suspiciousReason)}</span>` : ''}
    <div class="instance-list">
      ${(instanceGroup.instances || []).map(instance => `
        <button class="instance-card" onclick="selectInstance('${html(instance.instanceId)}')">
          <div><strong>${html(instance.pageName || instance.pageId)}</strong></div>
          <div class="small muted"><code>${html(instance.pageId)}</code> · ${html(instance.instanceKey || '')}</div>
          <div class="entity-counts">${entityCountsHtml(instance.entityCounts)}</div>
        </button>
      `).join('')}
    </div>
  ` : '';
  const signatures = (node.signatures || []).slice(0, 8).map(s =>
    `<div class="small"><code>${html(s.signature)}</code> x${html(s.count)}</div>`
  ).join('');
  const examples = (node.examples || []).map(step => `
    <div class="step-card">
      ${screenshotFrame(step.screenshot, step.key, step)}
      <div class="small"><strong>${html(step.key)}</strong></div>
      ${step.feedbackFixed ? `<span class="pill">feedback fixed</span>` : ''}
      ${step.skipReason ? `<span class="pill warn">skip: ${html(step.skipReason)}</span>` : ''}
      <div class="small">上一步: ${html(short(step.prev?.action, 80))}</div>
      <div class="small">当前: ${html(short(step.action, 90))}</div>
      <div class="small">下一步: ${html(short(step.next?.action, 80))}</div>
    </div>
  `).join('');
  document.getElementById('inspector').innerHTML = `<div class="panel">
    ${returnButtonHtml()}
    <h2>${html(node.name)}</h2>
    <div><code>${html(node.id)}</code></div>
    <div class="muted">${html(node.routePath)}</div>
    <p>${html(node.description)}</p>
    <span class="pill">${html(node.domain)}</span>
    <span class="pill">${html(node.stepCount)} 步</span>
    <span class="pill">${html(node.traceCount)} 条 trace</span>
    ${renderAnnotationControls()}
    <h3>Global Route Feedback</h3>
    <div class="route-feedback-form">
      <label class="small">将当前 page cluster 整体并入</label>
      <select id="mergeTarget-${html(node.id)}">${pageOptionHtml('')}</select>
      <textarea id="mergeNote-${html(node.id)}" placeholder="说明为什么 ${html(node.id)} 应整体并入目标页面簇"></textarea>
      <button onclick='addGlobalRouteFeedbackFromNode(${JSON.stringify(node.id)})'>添加 merge feedback</button>
    </div>
    ${renderGlobalRouteFeedbackList(node.id)}
    ${instanceSummary}
    <h3>视觉签名分布</h3>
    ${signatures || '<div class="muted">没有签名信息</div>'}
    <h3>Step 证据</h3>
    <div class="examples">${examples || '<div class="muted">没有示例</div>'}</div>
  </div>`;
}

function renderEdgeInspector(edge) {
  if (!edge) return;
  const from = nodeById.get(edge.from);
  const to = nodeById.get(edge.to);
    const routes = (edge.routes || []).map(route => {
    const triplets = (route.evidenceTriplets || []).slice(0, 4).map(t => `
      <div class="evidence-grid">
        ${shot(t.fromStep, '起点/当前 step')}
        ${shot((t.bridgeSteps || [])[0], '中间 skipped step')}
        ${shot(t.toStep, '下一个页面')}
      </div>
      ${t.checks && t.checks.length ? `<div>${t.checks.map(c => `<span class="pill fail">${html(c)}</span>`).join('')}</div>` : ''}
    `).join('');
    return `<details open>
      <summary><code>${html(route.id)}</code> · ${html(short(route.displayTrigger || route.trigger, 120))}</summary>
      ${triplets || '<div class="muted">没有 evidence triplet</div>'}
    </details>`;
  }).join('');
  const observed = edge.observed?.bridgeExamples?.map(ex => `
    <details open>
      <summary>观测到的跳过桥接 <code>${html(ex.fromStep)}</code> -> <code>${html(ex.toStep)}</code></summary>
      <div class="evidence-grid">
        ${shot(stepByKey[ex.fromStep], '起点')}
        ${shot((ex.bridgeSteps || [])[0], '中间 skipped step')}
        ${shot(stepByKey[ex.toStep], '终点')}
      </div>
    </details>
  `).join('') || '';
  document.getElementById('inspector').innerHTML = `<div class="panel">
    ${returnButtonHtml()}
    <h2>${html(from?.name || edge.from)} -> ${html(to?.name || edge.to)}</h2>
    <div><code>${html(edge.from)}</code> -> <code>${html(edge.to)}</code></div>
    <span class="pill ${['missing_observed','loading_bridge_unclosed','unassigned_bridge_step','system_bridge_not_skipped'].includes(edge.kind) ? 'fail' : edge.kind === 'extra_unobserved' || edge.kind === 'system_bridge' || edge.kind === 'same_page_state_misclassified' || edge.kind === 'mixed_bridge' ? 'warn' : ''}">${html(EDGE_KIND_LABELS[edge.kind] || edge.kind)}</span>
    <span class="pill">${html(edge.routeCount)} 条 route</span>
    <span class="pill">${html(edge.observedCount)} 次观测</span>
    ${annotationHintForEdge(edge)}
    ${renderAnnotationControls('', annotationLabelsForEdge(edge))}
    <h3>Canonical Route 证据</h3>
    ${routes || '<div class="muted">这条观测边没有对应的 canonical route。</div>'}
    <h3>观测到的 Skipped Bridge</h3>
    ${observed || '<div class="muted">没有跳过桥接示例。</div>'}
  </div>`;
}

function renderTraceInspector(trace) {
  const traceNote = traceNoteValue(trace.id);
  const transitionTotal = (trace.transitions || []).length;
  const reviewedTransitions = (trace.transitions || []).filter((_, index) => annotations[traceTransitionKey(trace.id, index)]).length;
  const flow = (trace.transitions || []).map((item, index) => {
    const from = nodeById.get(item.from);
    const to = nodeById.get(item.to);
    const bridge = item.bridgeSteps && item.bridgeSteps.length
      ? `<span class="pill warn">跳过 ${item.bridgeSteps.length} 步</span>`
      : '';
    const key = traceTransitionKey(trace.id, index);
    const edge = edgeById.get(item.edgeId);
    const status = annotations[key]?.value || '未核验';
    const active = activeTransitionIndex === index ? ' selected' : '';
    const edgeButton = edgeById.has(item.edgeId)
      ? `<button onclick='openEdgeFromTrace(${JSON.stringify(item.edgeId)}, ${JSON.stringify(trace.id)}, ${index})'>查看边证据</button>`
      : `<span class="pill fail">图中无边</span>`;
    return `<div class="step-card${active}">
      <div class="small"><strong>#${index + 1}</strong> <code>${html(item.fromStep)}</code> -> <code>${html(item.toStep)}</code></div>
      <div class="small">${html(from?.name || item.from)} -> ${html(to?.name || item.to)}</div>
      ${materializedPageLine(item.fromMaterializedPage, item.from)}
      ${materializedPageLine(item.toMaterializedPage, item.to)}
      <div class="small">动作: ${html(short(item.action, 120))}</div>
      <div>${bridge}<span class="pill">${html(status)}</span></div>
      <div class="annotation-row">
        <button onclick='selectTraceTransition(${JSON.stringify(trace.id)}, ${index})'>选中此跳转</button>
        ${edgeButton}
        <button onclick='openNodeFromTrace(${JSON.stringify(item.from)}, ${JSON.stringify(trace.id)}, ${index})'>查看起点页面</button>
        <button onclick='openNodeFromTrace(${JSON.stringify(item.to)}, ${JSON.stringify(trace.id)}, ${index})'>查看终点页面</button>
      </div>
      ${edge?.kind === 'missing_observed' ? annotationHintForEdge(edge) : ''}
      ${renderAnnotationControls(key, edge?.kind === 'missing_observed' ? MISSING_OBSERVED_EDGE_LABELS : TRACE_TRANSITION_LABELS)}
    </div>`;
  }).join('');
  const visited = (trace.nodeIds || []).map(id => {
    const node = nodeById.get(id);
    return `<button onclick='openNodeFromTrace(${JSON.stringify(id)}, ${JSON.stringify(trace.id)}, null)'>${html(node?.name || id)}</button>`;
  }).join('');
  document.getElementById('inspector').innerHTML = `<div class="panel">
    <h2>Trace 路径</h2>
    <div><code>${html(trace.id)}</code></div>
    <p>${html(trace.task || '')}</p>
    <span class="pill">${html(trace.stepCount)} 步</span>
    <span class="pill">${html(trace.labeledCount)} 个已标注 step</span>
    <span class="pill">${html(trace.skippedCount)} 个 skipped step</span>
    <span class="pill">${html((trace.nodeIds || []).length)} 个页面节点</span>
    <span class="pill">${html((trace.transitions || []).length)} 次页面跳转</span>
    <span class="pill">${html(reviewedTransitions)} / ${html(transitionTotal)} 跳转已核验</span>
    <div class="trace-note">
      <label for="traceNote">Trace 备注</label>
      <textarea id="traceNote" data-trace-note-id="${html(trace.id)}" placeholder="记录整条 trace 的问题或结论">${html(traceNote)}</textarea>
    </div>
    ${renderAnnotationControls(`trace:${trace.id}`)}
    <h3>经过的页面</h3>
    <div class="annotation-row">${visited || '<span class="muted">没有已标注页面</span>'}</div>
    <h3>页面流</h3>
    <div class="examples">${flow || '<div class="muted">没有页面跳转。</div>'}</div>
  </div>`;
}

function renderStepInspector(step) {
  document.getElementById('inspector').innerHTML = `<div class="panel">
    ${returnButtonHtml()}
    <h2>${html(step.key)}</h2>
    <span class="pill warn">${html(step.skipReason || 'step')}</span>
    <div class="evidence-grid">${shot(step, '当前选中 step')}</div>
    ${materializedPageLine(step, step.pageId)}
    <h3>Description</h3>
    <div class="small">${html(step.observation?.description || '')}</div>
    ${renderAnnotationControls()}
  </div>`;
  renderFilmstrip([step]);
}

function renderFilmstrip(steps) {
  const seen = new Set();
  const unique = [];
  for (const step of steps || []) {
    const normalized = normalizeFilmstripStep(step);
    if (!normalized || !normalized.key || seen.has(normalized.key)) continue;
    seen.add(normalized.key);
    unique.push(normalized);
  }
  document.getElementById('filmstrip').innerHTML = unique.length ? unique.map(step => `
    <div class="film-item${step.skipReason ? ' is-skip' : ''}">
      ${screenshotFrame(step.screenshot, step.key, step)}
      <div class="film-meta">
        <span class="step-index-pill">step ${html(step.stepIndex ?? parseStepIndex(step.key))}</span>
        ${filmstripStepStatus(step)}
      </div>
      <div class="small"><strong>${html(step.key)}</strong></div>
      ${materializedPageLine(step, step.pageId)}
      <div class="small">${html(short(step.action, 70))}</div>
    </div>
  `).join('') : '<div class="empty">请选择一个风险项、页面节点或路由边来查看 trace 证据。</div>';
}

function normalizeFilmstripStep(step) {
  if (!step) return null;
  const key = String(step.key || '').trim();
  const base = key ? (stepByKey[key] || {}) : {};
  return {
    ...base,
    ...step,
    key: step.key || base.key || key,
    screenshot: step.screenshot || base.screenshot || '',
    action: step.action || base.action || '',
    pageId: step.pageId || base.pageId || '',
    skipReason: step.skipReason || base.skipReason || '',
    stepIndex: step.stepIndex ?? base.stepIndex ?? parseStepIndex(key || base.key || ''),
  };
}

function filmstripStepStatus(step) {
  if (step.skipReason) return `<span class="pill warn">skip: ${html(step.skipReason)}</span>`;
  if (step.feedbackFixed) return '<span class="pill">feedback fixed</span>';
  if (step.instanceId) return `<span class="pill">instance: ${html(step.instanceId)}</span>`;
  if (!step.pageId) return '<span class="pill warn">未入路由图</span>';
  return `<span class="pill">${html(step.pageId)}</span>`;
}

function renderIssues() {
  const items = DATA.issues.filter(item => filter === 'all' || item.severity === filter);
  document.getElementById('issueList').innerHTML = items.length ? items.map(item => `
    <button class="issue ${html(item.severity)}" id="issue-${html(item.id)}" onclick="selectIssue('${html(item.id)}')">
      <div class="issue-title">${html(item.title)}</div>
      <div class="issue-type">${html(SEVERITY_LABELS[item.severity] || item.severity)} · ${html(ISSUE_TYPE_LABELS[item.type] || item.type)}</div>
    </button>
  `).join('') : '<div class="empty">当前筛选条件下没有问题。</div>';
}

function selectIssue(issueId) {
  document.querySelectorAll('.issue.active').forEach(x => x.classList.remove('active'));
  document.getElementById(`issue-${CSS.escape(issueId)}`)?.classList.add('active');
  const issue = DATA.issues.find(x => x.id === issueId);
  if (!issue) return;
  const t = issue.target || {};
  if (t.kind === 'traceTransition') selectTraceTransition(t.traceId, t.transitionIndex);
  else if (t.kind === 'edge') selectEdge(t.id);
  else if (t.kind === 'node') selectNode(t.id);
  else if (t.kind === 'step') {
    activeTraceId = '';
    activeTransitionIndex = null;
    reviewContext = { mode: 'step', stepKey: t.key };
    selected = { kind: 'step', id: t.key };
    applyTraceHighlight();
    applySelection();
    renderStepInspector(stepByKey[t.key]);
  } else if (t.edge) selectEdge(t.edge);
}

document.querySelectorAll('[data-filter]').forEach(btn => {
  btn.addEventListener('click', () => {
    filter = btn.dataset.filter;
    renderIssues();
  });
});

document.addEventListener('click', evt => {
  const button = evt.target.closest('.annotation-btn');
  if (!button) return;
  annotate(button.dataset.annotationValue || '', button.dataset.annotationKey || '');
});

document.addEventListener('input', evt => {
  const textarea = evt.target.closest('textarea[data-trace-note-id]');
  if (!textarea) return;
  saveTraceNote(textarea.dataset.traceNoteId || '', textarea.value || '');
});

document.getElementById('saveAnnotationsBtn')?.addEventListener('click', () => {
  saveAnnotationsFile().catch(error => {
    setAnnotationSaveStatus(`保存失败：${error.message || String(error)}`, 'error');
    alert(error.message || String(error));
  });
});
document.getElementById('reloadServerAnnotationsBtn')?.addEventListener('click', () => {
  loadAnnotationsFromServer().catch(error => alert(error.message || String(error)));
});
document.getElementById('importAnnotationsBtn')?.addEventListener('click', () => {
  document.getElementById('annotationsFileInput')?.click();
});
document.getElementById('globalRouteFeedbackBtn')?.addEventListener('click', () => {
  renderGlobalRouteFeedbackPanel();
});
document.getElementById('acceptAuditedReasoningBtn')?.addEventListener('click', () => {
  acceptAuditedReasoning().catch(error => {
    setAnnotationSaveStatus(`固化失败：${error.message || String(error)}`, 'error');
    alert(error.message || String(error));
  });
});
document.getElementById('annotationsFileInput')?.addEventListener('change', evt => {
  importAnnotationsFile(evt.target.files?.[0]).catch(error => alert(error.message || String(error)));
  evt.target.value = '';
});

  setupResizableLayout();
  setupQueueToggle();
  setupGraphZoom();
  setupGraphResizeObserver();
  document.getElementById('reloadServerAnnotationsBtn')?.style.setProperty('display', serverSaveEnabled ? '' : 'none');
  document.getElementById('acceptAuditedReasoningBtn')?.style.setProperty('display', serverAcceptAuditEnabled ? '' : 'none');
  renderAnnotationSaveStatus();
  stats();
  renderTraceSelector();
  document.getElementById('routeGraphBtn')?.classList.add('mode-active');
  renderGraph();
  queueGlobalLayout();
  renderIssues();
renderFilmstrip([]);
document.getElementById('inspector').innerHTML = '<div class="panel"><h2>未选择</h2><div class="muted">请选择一个风险项、页面节点、路由边或 Trace。</div></div>';
if (DATA.issues.length) selectIssue(DATA.issues[0].id);
</script>
</body>
</html>
"""


def build_html(args: argparse.Namespace) -> str:
    output_dir = Path(args.output_dir).resolve()
    trace_root = Path(args.trace_root).resolve()
    html_path = Path(args.html).resolve() if args.html else output_dir / "reports" / "route_graph_audit.html"
    html_dir = html_path.parent
    global_reasoning_path = resolve_global_reasoning_path(
        output_dir,
        stage=args.global_reasoning_stage,
        explicit_path=args.global_reasoning_json,
    )
    audit = build_audit(output_dir, trace_root, html_dir, global_reasoning_path)
    annotation_path = html_dir / "route_review_annotations.json"
    annotations = load_json(annotation_path, {}) or {}
    if not isinstance(annotations, dict):
        annotations = {}
    html_text = (
        HTML_TEMPLATE
        .replace("__DATA_JSON__", json_for_html(audit))
        .replace("__ANNOTATION_JSON__", json_for_html(annotations))
        .replace("__ANNOTATION_API__", "")
        .replace("__ACCEPT_AUDIT_API__", "")
        .replace("__OUTPUT_DIR__", esc(_short_path(output_dir)))
        .replace("__TRACE_ROOT__", esc(_short_path(trace_root)))
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_text, encoding="utf-8")
    return str(html_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--html", default="")
    parser.add_argument(
        "--global-reasoning-stage",
        choices=["default", "proposal", "resolved", "final", "audited"],
        default="default",
        help="Global reasoning artifact to visualize from data/debug.",
    )
    parser.add_argument(
        "--global-reasoning-json",
        default="",
        help="Explicit global reasoning JSON path. Overrides --global-reasoning-stage.",
    )
    args = parser.parse_args()
    print(build_html(args))


if __name__ == "__main__":
    main()
