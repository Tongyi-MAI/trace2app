
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from trace2world_pipeline.validation.trace_replay import (
    TraceData,
    _deterministic_step_command,
    build_trace_action_execution_pack,
    build_trace_action_execution_pack_from_site_model,
)
from trace2world_pipeline.app_spec.viewport_defaults import MOBILE_VIEWPORT
from trace2world_pipeline.app_spec.environment_profile import (
    load_environment_profile_from_result_dir,
    resolve_environment_profile,
)


def _generated_page_exists(website_dir: str, filename: str, materialized_filenames: set | None = None) -> bool:
    filename = str(filename or "").strip()
    if not filename:
        return False
    if materialized_filenames is not None:
        return filename in materialized_filenames
    root = Path(website_dir)
    phase_dir = root / "phase_pagegen" / "pages"
    if phase_dir.is_dir():
        return (phase_dir / filename).is_file()
    return any(
        (base / filename).is_file()
        for base in (
            root,
            root / "frontend" / "pages",
        )
    )


@dataclass
class PageExecutionObligation:
    trace_id: str
    step_index: int
    source_filename: str
    operation: str
    command: str
    expected_filename: str
    input_text: str = ""
    operation_intent: str = ""
    intent_provenance: str = "trace_action"
    intent_visible_text_allowed: bool = False
    hard: bool = True
    gesture: Dict[str, Any] = field(default_factory=dict)
    validation_viewport: Dict[str, int] = field(default_factory=lambda: dict(MOBILE_VIEWPORT))


def _gesture_from_command(command: str) -> Dict[str, Any]:
    parts = str(command or "").strip().split()
    if len(parts) >= 3 and parts[0] == "tap":
        try:
            x = int(float(parts[1]))
            y = int(float(parts[2]))
        except (TypeError, ValueError):
            return {}
        return {"type": "tap", "x": x, "y": y, "hit_point": {"x": x, "y": y}}
    if len(parts) >= 5 and parts[0] == "swipe":
        try:
            sx, sy, ex, ey = [int(float(item)) for item in parts[1:5]]
        except (TypeError, ValueError):
            return {}
        return {
            "type": "swipe",
            "start": {"x": sx, "y": sy},
            "end": {"x": ex, "y": ey},
            "delta": {"x": ex - sx, "y": ey - sy},
        }
    match = re.match(r"^(swipe|scroll)\s+([a-zA-Z_-]+)(?:\s+(\d+))?$", str(command or "").strip())
    if match:
        gesture: Dict[str, Any] = {"type": match.group(1), "direction": match.group(2)}
        if match.group(3):
            gesture["amount"] = int(match.group(3))
        return gesture
    return {}


def _obligations_from_packs(
    packs: List[Dict[str, Any]],
    materialized_filenames: set | None = None,
    website_dir: str = "",
    validation_viewport: Dict[str, int] | None = None,
) -> List[PageExecutionObligation]:
    obligations: List[PageExecutionObligation] = []
    for pack in packs:
        current_filename = str(pack.get("start_state", {}).get("filename") or "")
        if current_filename and not _generated_page_exists(website_dir, current_filename, materialized_filenames):
            current_filename = ""
        steps = pack.get("steps", [])
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            pre_state = step.get("pre_state", {}) if isinstance(step.get("pre_state", {}), dict) else {}
            post_state = step.get("expected_post_state", {}) if isinstance(step.get("expected_post_state", {}), dict) else {}
            source_filename = str(current_filename or pre_state.get("filename") or "").strip()
            expected_filename = str(post_state.get("filename") or "").strip()
            if expected_filename:
                current_filename = expected_filename
            command = _deterministic_step_command(step)
            if (
                not command
                or not expected_filename
                or step.get("execution_policy") in {
                    "normalize_initial_app_open",
                    "external_system_observation_skip",
                }
            ):
                continue
            gesture = _gesture_from_command(command)
            action = step.get("action", {}) if isinstance(step.get("action", {}), dict) else {}
            intent = str(action.get("intent") or action.get("action_text") or "")
            obligations.append(PageExecutionObligation(
                trace_id=str(pack.get("trace_id") or ""),
                step_index=int(step.get("step_index") or 0),
                source_filename=source_filename,
                operation=str(action.get("operation") or ""),
                command=command,
                expected_filename=expected_filename,
                input_text=str(action.get("input_text") or ""),
                operation_intent=intent,
                intent_provenance="trace_action" if intent else "",
                intent_visible_text_allowed=False,
                gesture=gesture,
                validation_viewport=dict(validation_viewport or MOBILE_VIEWPORT),
            ))
    return obligations


def build_page_execution_obligations_from_site_model(
    site_model: "Any",
    traces: List[TraceData],
    *,
    output_dir: str = "",
) -> Tuple[List[PageExecutionObligation], List[Dict[str, Any]]]:
    packs: List[Dict[str, Any]] = []
    materialized_filenames = {p.filename for p in site_model.structure.page_prototypes if p.filename}
    for trace in traces:
        pack = build_trace_action_execution_pack_from_site_model(
            site_model,
            trace,
            output_dir=output_dir,
        )
        packs.append(pack)
    environment_profile = (
        site_model.environment_profile
        if isinstance(getattr(site_model, "environment_profile", None), dict)
        else {}
    )
    validation_viewport = (
        environment_profile.get("viewport")
        if isinstance(environment_profile.get("viewport"), dict)
        else MOBILE_VIEWPORT
    )
    obligations = _obligations_from_packs(
        packs,
        materialized_filenames=materialized_filenames,
        validation_viewport=validation_viewport,
    )
    return obligations, packs


def build_page_execution_obligations(
    website_dir: str,
    traces: List[TraceData],
    *,
    materialized_filenames: set | None = None,
) -> Tuple[List[PageExecutionObligation], List[Dict[str, Any]]]:
    packs: List[Dict[str, Any]] = []
    for trace in traces:
        pack = build_trace_action_execution_pack(
            website_dir,
            trace,
        )
        packs.append(pack)
    environment_profile = load_environment_profile_from_result_dir(website_dir)
    obligations = _obligations_from_packs(
        packs,
        materialized_filenames=materialized_filenames,
        website_dir=website_dir,
        validation_viewport=environment_profile["viewport"],
    )
    return obligations, packs


def build_page_execution_obligations_by_page_from_site_model(
    site_model: "Any",
    traces: List[TraceData],
    *,
    output_dir: str = "",
) -> Dict[str, Any]:
    obligations, packs = build_page_execution_obligations_from_site_model(
        site_model,
        traces,
        output_dir=output_dir,
    )
    environment_profile = (
        site_model.environment_profile
        if isinstance(getattr(site_model, "environment_profile", None), dict)
        and site_model.environment_profile
        else resolve_environment_profile()
    )
    validation_viewport = environment_profile["viewport"]
    packs = _enrich_action_pack_semantics(packs)
    pages: Dict[str, List[Dict[str, Any]]] = {}
    for obligation in obligations:
        if not obligation.source_filename:
            continue
        pages.setdefault(obligation.source_filename, []).append(asdict(obligation))
    return {
        "format": "page_execution_obligations_by_page",
        "schema_revision": 1,
        "environment_profile_hash": environment_profile.get("profile_hash"),
        "platform": environment_profile.get("platform"),
        "coordinate_space": {
            "kind": "environment_viewport_css_pixels",
            "viewport": dict(validation_viewport),
            "origin": "top_left",
        },
        "total_obligations": sum(len(items) for items in pages.values()),
        "page_count": len(pages),
        "action_packs": packs,
        "obligations": [asdict(obligation) for obligation in obligations],
        "pages": pages,
    }


def build_page_execution_obligations_by_page(
    website_dir: str,
    traces: List[TraceData],
    *,
    materialized_filenames: set | None = None,
) -> Dict[str, Any]:
    obligations, packs = build_page_execution_obligations(
        website_dir,
        traces,
        materialized_filenames=materialized_filenames,
    )
    packs = _enrich_action_pack_semantics(packs)
    pages: Dict[str, List[Dict[str, Any]]] = {}
    for obligation in obligations:
        if not obligation.source_filename:
            continue
        pages.setdefault(obligation.source_filename, []).append(asdict(obligation))
    environment_profile = load_environment_profile_from_result_dir(website_dir)
    return {
        "format": "page_execution_obligations_by_page",
        "schema_revision": 1,
        "environment_profile_hash": environment_profile.get("profile_hash"),
        "platform": environment_profile.get("platform"),
        "coordinate_space": {
            "kind": "environment_viewport_css_pixels",
            "viewport": dict(environment_profile["viewport"]),
            "origin": "top_left",
        },
        "total_obligations": sum(len(items) for items in pages.values()),
        "page_count": len(pages),
        "action_packs": packs,
        "obligations": [asdict(obligation) for obligation in obligations],
        "pages": pages,
    }


def _enrich_action_pack_semantics(packs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for pack in packs or []:
        if not isinstance(pack, dict):
            continue
        cloned_pack = dict(pack)
        steps: List[Dict[str, Any]] = []
        for step in cloned_pack.get("steps", []) if isinstance(cloned_pack.get("steps"), list) else []:
            if not isinstance(step, dict):
                continue
            cloned_step = dict(step)
            semantic_class = cloned_step.get("semantic_class") or _infer_action_step_semantic_class(cloned_step)
            cloned_step["semantic_class"] = semantic_class
            if semantic_class in {"execution_only", "route_boundary"}:
                cloned_step.setdefault(
                    "semantic_non_state_reason",
                    "route reachability" if semantic_class == "route_boundary" else "dom actionability without business state mutation",
                )
            else:
                owner_ref = _schema_owner_ref_from_action_step(cloned_step)
                if owner_ref:
                    if str(owner_ref).startswith("global_execution_schema:"):
                        cloned_step.setdefault("state_ref", owner_ref)
                        cloned_step.setdefault("schema_owner_ref", str(owner_ref).split(":", 1)[1])
                    else:
                        cloned_step.setdefault("downstream_projection_ref", owner_ref)
            steps.append(cloned_step)
        cloned_pack["steps"] = steps
        enriched.append(cloned_pack)
    return enriched


def _infer_action_step_semantic_class(step: Dict[str, Any]) -> str:
    if step.get("terminal_policy") or step.get("checkpoint_kind") == "outcome" or step.get("reward_relevant") is True:
        return "outcome"
    if step.get("expected_state") or step.get("expected_state_diff") or step.get("state_ref") or step.get("state_refs"):
        operation = _step_operation_text(step)
        if any(token in operation for token in ("select", "choose", "点击", "选择", "勾选")):
            return "selection"
        if any(token in operation for token in ("fill", "type", "press", "delete", "input", "输入", "填写", "删除")):
            return "stateful_mutation"
        return "stateful_mutation"
    pre = step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
    post = step.get("expected_post_state") if isinstance(step.get("expected_post_state"), dict) else {}
    if pre.get("filename") and post.get("filename") and pre.get("filename") != post.get("filename"):
        return "route_boundary"
    return "execution_only"


def _schema_owner_ref_from_action_step(step: Dict[str, Any]) -> str:
    for ref in [step.get("state_ref"), *(_as_list(step.get("state_refs")))]:
        text = str(ref or "").strip()
        if text.startswith("global_execution_schema:"):
            return text
        if text:
            return text
    for container_key in ("expected_state_delta", "expected_state"):
        container = step.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in container:
            text = str(key or "").strip()
            if not text or text in {"page", "current_page", "terminal_page"}:
                continue
            if text.startswith("global_execution_schema:"):
                return text
            if "." in text and not text.startswith("state::"):
                return f"global_execution_schema:{text}"
            if text.startswith("state::"):
                return text
    return ""


def _step_operation_text(step: Dict[str, Any]) -> str:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    return " ".join(
        str(value or "")
        for value in (
            step.get("operation"),
            step.get("intent"),
            step.get("raw_action"),
            action.get("operation"),
            action.get("intent"),
            action.get("raw_action"),
        )
    ).lower()


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def build_l0_execution_pack_from_obligations(artifact: Dict[str, Any]) -> Dict[str, Any]:
    action_packs = [
        pack for pack in artifact.get("action_packs", [])
        if isinstance(pack, dict)
    ] if isinstance(artifact, dict) else []
    obligations = [
        item for item in artifact.get("obligations", [])
        if isinstance(item, dict)
    ] if isinstance(artifact, dict) else []
    pages = artifact.get("pages", {}) if isinstance(artifact, dict) else {}
    obligations_by_page = {
        str(filename): [item for item in items if isinstance(item, dict)]
        for filename, items in pages.items()
        if isinstance(items, list)
    } if isinstance(pages, dict) else {}

    action_packs_by_trace_id: Dict[str, Dict[str, Any]] = {}
    trace_index: Dict[str, Dict[str, Any]] = {}
    for pack in action_packs:
        trace_id = str(pack.get("trace_id") or "")
        if not trace_id:
            continue
        action_packs_by_trace_id[trace_id] = pack
        steps = pack.get("steps", []) if isinstance(pack.get("steps"), list) else []
        trace_index[trace_id] = {
            "step_count": len(steps),
            "task": str(pack.get("task") or ""),
            "start_filename": str((pack.get("start_state") or {}).get("filename") or "")
            if isinstance(pack.get("start_state"), dict)
            else "",
        }

    obligations_by_trace_id: Dict[str, List[Dict[str, Any]]] = {}
    for obligation in obligations:
        trace_id = str(obligation.get("trace_id") or "")
        if trace_id:
            obligations_by_trace_id.setdefault(trace_id, []).append(obligation)

    page_index = {
        str(filename): {
            "obligation_count": len(items),
            "trace_ids": sorted({
                str(item.get("trace_id") or "")
                for item in items
                if str(item.get("trace_id") or "")
            }),
        }
        for filename, items in obligations_by_page.items()
    }

    return {
        "format": "l0_execution_pack",
        "schema_revision": 1,
        "environment_profile_hash": artifact.get("environment_profile_hash"),
        "platform": artifact.get("platform"),
        "coordinate_space": artifact.get("coordinate_space", {}),
        "artifact_metadata": {
            "artifact_id": "projections.l0_execution.pack",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "logical_owner": "l0_execution",
            "semantic_role": "l0_execution_consumption_projection",
            "producer": "site_model.page_execution_obligations.build_l0_execution_pack",
            "allowed_consumers": [
                "page_environment_compiler",
                "page_execution_validator",
                "repair",
            ],
            "debug_only": False,
        },
        "source_selection": {
            "derived_from": [
                "reference_traces",
                "capture_actions",
                "page_execution_obligations_by_page",
            ],
        },
        "action_packs": action_packs,
        "action_packs_by_trace_id": action_packs_by_trace_id,
        "obligations_by_page": obligations_by_page,
        "obligations_by_trace_id": obligations_by_trace_id,
        "page_index": page_index,
        "trace_index": trace_index,
        "summary": {
            "trace_count": len(action_packs_by_trace_id),
            "page_count": len(obligations_by_page),
            "action_pack_count": len(action_packs),
            "obligation_count": len(obligations),
        },
    }


def _compact_text_key(value: str) -> str:
    return re.sub(r"[\s\-_:/：，,。.!！?？（）()【】\[\]\"'“”‘’]+", "", str(value or "")).lower()


def _quoted_label_from_action_text(value: str) -> str:
    text = str(value or "")
    labels: List[str] = []
    for pattern in (
        r"“([^”]{1,32})”",
        r"\"([^\"]{1,32})\"",
        r"'([^']{1,32})'",
        r"‘([^’]{1,32})’",
    ):
        labels.extend(match.group(1).strip() for match in re.finditer(pattern, text) if match.group(1).strip())
    return labels[-1] if labels else ""


def _coord_bucket(action: Dict[str, Any], *, bucket_size: int = 16) -> str:
    coords = action.get("viewport_coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        coords = action.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return ""
    try:
        x = int(round(float(coords[0]) / bucket_size) * bucket_size)
        y = int(round(float(coords[1]) / bucket_size) * bucket_size)
    except (TypeError, ValueError):
        return ""
    return f"x{x}_y{y}"


def _first_identity_value(*containers: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}):
                return str(value)
    return ""


def _l0_surface_key(step: Dict[str, Any]) -> Dict[str, str]:
    action = step.get("action", {}) if isinstance(step.get("action"), dict) else {}
    pre_state = step.get("pre_state", {}) if isinstance(step.get("pre_state"), dict) else {}
    post_state = (
        step.get("expected_post_state", {})
        if isinstance(step.get("expected_post_state"), dict)
        else {}
    )
    source_filename = str(pre_state.get("filename") or "").strip()
    operation = str(action.get("operation") or "").strip().lower()
    action_text = str(action.get("intent") or action.get("action_text") or "")
    quoted_label = _quoted_label_from_action_text(action_text)
    label_key = _compact_text_key(quoted_label)
    coordinate_bucket = _coord_bucket(action)
    component_ref = _first_identity_value(
        action,
        step,
        pre_state,
        keys=("component_ref", "target_element", "target_element_id", "data_element_id", "element_id"),
    )
    record_identity = _first_identity_value(
        action,
        step,
        pre_state,
        post_state,
        keys=("record_identity", "record_ref", "item_ref", "entity_ref", "selected_record_ref"),
    )
    query_identity = _first_identity_value(
        action,
        step,
        pre_state,
        post_state,
        keys=("query_identity", "query_ref", "query_value"),
    )
    state_identity = _first_identity_value(
        action,
        step,
        pre_state,
        keys=(
            "state_identity",
            "state_ref",
            "source_lifecycle",
            "runtime_signature",
            "finite_runtime_signature",
            "source_runtime_signature",
        ),
    )
    return {
        "source_filename": source_filename,
        "operation": operation,
        "quoted_label": quoted_label,
        "label_key": label_key,
        "coordinate_bucket": coordinate_bucket,
        "component_ref": component_ref,
        "record_identity": record_identity,
        "query_identity": query_identity,
        "state_identity": state_identity,
    }


def _l0_surface_group_key(surface: Dict[str, str]) -> Tuple[str, ...]:
    return (
        surface.get("source_filename", ""),
        surface.get("operation", ""),
        surface.get("label_key", ""),
        surface.get("coordinate_bucket", ""),
        surface.get("component_ref", ""),
        surface.get("record_identity", ""),
        surface.get("query_identity", ""),
        surface.get("state_identity", ""),
    )


def build_l0_transition_conflicts(l0_execution_pack: Dict[str, Any]) -> Dict[str, Any]:
    groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    action_packs = (
        l0_execution_pack.get("action_packs", [])
        if isinstance(l0_execution_pack, dict)
        else []
    )
    for pack in action_packs if isinstance(action_packs, list) else []:
        if not isinstance(pack, dict):
            continue
        trace_id = str(pack.get("trace_id") or "")
        steps = pack.get("steps", []) if isinstance(pack.get("steps"), list) else []
        active_query_by_source: Dict[str, str] = {}
        for step in steps:
            if not isinstance(step, dict):
                continue
            action = step.get("action", {}) if isinstance(step.get("action"), dict) else {}
            pre_state = step.get("pre_state", {}) if isinstance(step.get("pre_state"), dict) else {}
            post_state = (
                step.get("expected_post_state", {})
                if isinstance(step.get("expected_post_state"), dict)
                else {}
            )
            source_filename = str(pre_state.get("filename") or "").strip()
            target_filename = str(post_state.get("filename") or "").strip()
            operation = str(action.get("operation") or "").strip().lower()
            input_text = str(action.get("input_text") or "").strip()
            if input_text and operation in {"fill", "input", "type"}:
                if source_filename:
                    active_query_by_source[source_filename] = input_text
                if target_filename:
                    active_query_by_source[target_filename] = input_text
            if not target_filename or step.get("execution_policy") == "normalize_initial_app_open":
                continue
            if operation not in {"click", "tap"}:
                continue
            surface = _l0_surface_key(step)
            if not surface.get("query_identity") and source_filename in active_query_by_source:
                surface["query_identity"] = f"query::{active_query_by_source[source_filename]}"
            if not surface.get("source_filename"):
                continue
            if not surface.get("label_key") and not surface.get("component_ref"):
                continue
            candidate = {
                "trace_id": trace_id,
                "step_index": int(step.get("step_index") or 0),
                "source_observation_key": str(step.get("source_observation_key") or ""),
                "source_filename": surface.get("source_filename", ""),
                "expected_target": target_filename,
                "surface": surface,
                "action_text": str(action.get("intent") or action.get("action_text") or ""),
                "viewport_coordinates": action.get("viewport_coordinates", []),
                "pre_state": {
                    "observation_key": str((step.get("pre_state") or {}).get("observation_key") or "")
                    if isinstance(step.get("pre_state"), dict)
                    else "",
                    "screenshot_path": str((step.get("pre_state") or {}).get("screenshot_path") or "")
                    if isinstance(step.get("pre_state"), dict)
                    else "",
                },
                "expected_post_state": {
                    "observation_key": str(post_state.get("observation_key") or ""),
                    "screenshot_path": str(post_state.get("screenshot_path") or ""),
                },
            }
            groups.setdefault(_l0_surface_group_key(surface), []).append(candidate)

    conflicts: List[Dict[str, Any]] = []
    for group_key, candidates in groups.items():
        targets = sorted({str(item.get("expected_target") or "") for item in candidates if item.get("expected_target")})
        traces = sorted({str(item.get("trace_id") or "") for item in candidates if item.get("trace_id")})
        if len(targets) <= 1 or len(traces) <= 1:
            continue
        first_surface = candidates[0].get("surface", {}) if isinstance(candidates[0].get("surface"), dict) else {}
        conflicts.append({
            "conflict_id": "l0_transition::" + _compact_text_key("::".join(group_key))[:96],
            "conflict_type": "incompatible_l0_transition_conflict",
            "severity": "blocking",
            "surface_key": first_surface,
            "target_filenames": targets,
            "trace_ids": traces,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "human_action_required": "drop_one_trace_or_split_dataset",
            "reason": (
                "Same source interaction has incompatible L0 targets and no "
                "distinguishing runtime, record, query, or lifecycle identity in the L0 pack."
            ),
        })

    return {
        "format": "l0_transition_conflicts",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.l0_execution.transition_conflicts",
            "artifact_kind": "validation_report",
            "api_or_det": "det",
            "artifact_role": "validation_gate",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "logical_owner": "l0_execution",
            "semantic_role": "l0_transition_conflict_gate",
            "producer": "site_model.page_execution_obligations.build_l0_transition_conflicts",
            "allowed_consumers": ["trace_generator", "developer"],
            "debug_only": False,
        },
        "source_selection": {
            "derived_from": ["projections/l0_execution/l0_execution_pack.json"],
        },
        "blocking_conflict_count": len(conflicts),
        "warning_count": len(conflicts),
        "conflicts": conflicts,
        "summary": {
            "blocking_conflict_count": len(conflicts),
            "conflict_count": len(conflicts),
            "group_count": len(groups),
        },
    }


def write_l0_transition_conflicts(path: str, artifact: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)


def build_l0_execution_pack_from_site_model(
    site_model: "Any",
    traces: List[TraceData],
    *,
    output_dir: str = "",
) -> Dict[str, Any]:
    return build_l0_execution_pack_from_obligations(
        build_page_execution_obligations_by_page_from_site_model(
            site_model,
            traces,
            output_dir=output_dir,
        )
    )


def build_l0_execution_pack(
    website_dir: str,
    traces: List[TraceData],
) -> Dict[str, Any]:
    return build_l0_execution_pack_from_obligations(
        build_page_execution_obligations_by_page(
            website_dir,
            traces,
        )
    )


def write_page_execution_obligations_by_page(path: str, artifact: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)


def write_l0_execution_pack(path: str, artifact: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)


def _step_action_operation(step: Dict[str, Any]) -> str:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    return str(action.get("operation") or "").strip().lower()


def _step_target(step: Dict[str, Any]) -> Dict[str, Any]:
    target = step.get("target")
    return target if isinstance(target, dict) else {}


def _manual_allowed_reason_for_step(step: Dict[str, Any]) -> str:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    raw_action = str(action.get("raw_action") or "").strip().lower()
    intent = str(action.get("intent") or action.get("action_text") or "").lower()
    target = _step_target(step)
    if raw_action.startswith("long_press") or "长按" in intent:
        return "text_selection_or_long_press_requires_manual_replay_policy"
    if target.get("status") == "not_applicable":
        return "target_marked_not_applicable"
    return ""


def _playwright_action_for_step(step: Dict[str, Any]) -> Tuple[str, str]:
    operation = _step_action_operation(step)
    execution_policy = str(step.get("execution_policy") or "")
    target = _step_target(step)
    selector = str(target.get("selector") or "")
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    viewport_coordinates = action.get("viewport_coordinates")

    if execution_policy in {"normalize_initial_app_open", "external_system_observation_skip"} or operation == "open_app":
        return "skip_initial_app_open", "not_applicable"
    if operation in {"finished", "finish"} or str(action.get("raw_action") or "").startswith("finished"):
        return "skip_terminal_state", "not_applicable"
    if operation == "agent_execute":
        raw_action = str(action.get("raw_action") or "")
        if raw_action.startswith("finished"):
            return "skip_terminal_state", "not_applicable"
        if raw_action == "takeover":
            return "skip_takeover_marker", "not_applicable"
        if _manual_allowed_reason_for_step(step):
            return "manual_declared", "manual"
        return "unresolved", "unresolved"
    if operation in {"swipe", "scroll"}:
        if isinstance(viewport_coordinates, list) and len(viewport_coordinates) >= 4:
            return "swipe_coordinates", "coordinate"
        if target.get("status") == "not_applicable":
            return "manual_declared", "manual"
        return "unresolved", "unresolved"
    if operation == "click" and selector:
        return "click_selector", "selector"
    if operation == "fill" and selector:
        return "fill_selector", "selector"
    if operation == "long_press" and selector:
        return "long_press_selector", "selector"
    if target.get("status") == "not_applicable":
        return "skip_not_applicable", "not_applicable"
    return "unresolved", "unresolved"


def _project_playwright_step(
    *,
    trace_id: str,
    step: Dict[str, Any],
) -> Dict[str, Any]:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    target = _step_target(step)
    pre_state = step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
    expected_post_state = (
        step.get("expected_post_state")
        if isinstance(step.get("expected_post_state"), dict)
        else {}
    )
    playwright_action, replay_status = _playwright_action_for_step(step)
    item: Dict[str, Any] = {
        "trace_id": trace_id,
        "step_index": step.get("step_index"),
        "playwright_action": playwright_action,
        "replay_status": replay_status,
        "operation": _step_action_operation(step),
        "source_filename": str(pre_state.get("filename") or ""),
        "expected_filename": str(expected_post_state.get("filename") or ""),
        "execution_policy": str(step.get("execution_policy") or ""),
        "action_text": str(action.get("action_text") or action.get("intent") or ""),
        "reference": {
            "pre_observation_key": str(pre_state.get("observation_key") or ""),
            "post_observation_key": str(expected_post_state.get("observation_key") or ""),
            "pre_screenshot_path": str(pre_state.get("screenshot_path") or ""),
            "post_screenshot_path": str(expected_post_state.get("screenshot_path") or ""),
        },
    }
    selector = str(target.get("selector") or "")
    if selector:
        item["selector"] = selector
    component_id = str(target.get("component_id") or "")
    if component_id:
        item["component_id"] = component_id
    if action.get("input_text") is not None:
        item["input_text"] = action.get("input_text")
    elif step.get("input_text") is not None:
        item["input_text"] = step.get("input_text")
    viewport_coordinates = action.get("viewport_coordinates")
    if isinstance(viewport_coordinates, list):
        item["viewport_coordinates"] = viewport_coordinates
    coordinates = action.get("coordinates")
    if isinstance(coordinates, list):
        item["raw_coordinates"] = coordinates
    coordinate_space = action.get("coordinate_space")
    if isinstance(coordinate_space, dict):
        item["raw_coordinate_space"] = coordinate_space
    if replay_status == "unresolved":
        item["unresolved_reason"] = str(
            target.get("blocking_reason") or target.get("status") or "unresolved"
        )
        if target.get("candidates"):
            item["candidate_component_ids"] = target.get("candidates")
    elif replay_status == "manual":
        item["manual_allowed_reason"] = _manual_allowed_reason_for_step(step) or "manual_replay_required"
    return item


def build_playwright_replay_plan(l0_execution_pack: Dict[str, Any]) -> Dict[str, Any]:
    action_packs = (
        l0_execution_pack.get("action_packs")
        if isinstance(l0_execution_pack, dict)
        else None
    )
    if not isinstance(action_packs, list):
        action_packs = []

    traces: List[Dict[str, Any]] = []
    flat_steps: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {
        "trace_count": 0,
        "step_count": 0,
        "selector_step_count": 0,
        "coordinate_step_count": 0,
        "skip_step_count": 0,
        "manual_declared_count": 0,
        "unresolved_step_count": 0,
        "by_playwright_action": {},
        "by_unresolved_reason": {},
    }

    for pack in action_packs:
        if not isinstance(pack, dict):
            continue
        trace_id = str(pack.get("trace_id") or "")
        steps = pack.get("steps") if isinstance(pack.get("steps"), list) else []
        projected_steps = [
            _project_playwright_step(trace_id=trace_id, step=step)
            for step in steps
            if isinstance(step, dict)
        ]
        traces.append(
            {
                "trace_id": trace_id,
                "task": str(pack.get("task") or ""),
                "start_state": pack.get("start_state") if isinstance(pack.get("start_state"), dict) else {},
                "steps": projected_steps,
            }
        )
        flat_steps.extend(projected_steps)

    summary["trace_count"] = len(traces)
    summary["step_count"] = len(flat_steps)
    for step in flat_steps:
        action_name = str(step.get("playwright_action") or "")
        summary["by_playwright_action"][action_name] = (
            summary["by_playwright_action"].get(action_name, 0) + 1
        )
        status = str(step.get("replay_status") or "")
        if status == "selector":
            summary["selector_step_count"] += 1
        elif status == "coordinate":
            summary["coordinate_step_count"] += 1
        elif status == "not_applicable":
            summary["skip_step_count"] += 1
        elif status == "manual":
            summary["manual_declared_count"] += 1
        elif status == "unresolved":
            summary["unresolved_step_count"] += 1
            reason = str(step.get("unresolved_reason") or "unresolved")
            summary["by_unresolved_reason"][reason] = (
                summary["by_unresolved_reason"].get(reason, 0) + 1
            )

    return {
        "format": "playwright_replay_plan",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.replay.playwright_replay_plan",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "logical_owner": "l0_execution",
            "semantic_role": "playwright_replay_plan",
            "producer": "site_model.page_execution_obligations.build_playwright_replay_plan",
            "allowed_consumers": ["playwright_replay_executor", "developer"],
            "debug_only": False,
        },
        "source_selection": {
            "derived_from": ["projections/l0_execution/l0_execution_pack.json"],
        },
        "summary": summary,
        "traces": traces,
        "steps": flat_steps,
        "unresolved_steps": [
            step for step in flat_steps if step.get("replay_status") == "unresolved"
        ],
        "manual_declared_steps": [
            step for step in flat_steps if step.get("replay_status") == "manual"
        ],
    }


def write_playwright_replay_plan(path: str, artifact: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)


def build_replay_plan_readiness(replay_plan: Dict[str, Any]) -> Dict[str, Any]:
    steps = replay_plan.get("steps") if isinstance(replay_plan, dict) else []
    if not isinstance(steps, list):
        steps = []
    blocking: List[Dict[str, Any]] = []
    manual_declared: List[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        status = str(step.get("replay_status") or "")
        action = str(step.get("playwright_action") or "")
        if status == "unresolved":
            blocking.append(
                {
                    "trace_id": step.get("trace_id"),
                    "step_index": step.get("step_index"),
                    "reason": step.get("unresolved_reason") or "unresolved",
                    "source_filename": step.get("source_filename"),
                    "expected_filename": step.get("expected_filename"),
                    "action_text": step.get("action_text"),
                }
            )
            continue
        if status == "manual":
            manual_declared.append(
                {
                    "trace_id": step.get("trace_id"),
                    "step_index": step.get("step_index"),
                    "reason": step.get("manual_allowed_reason") or "manual_replay_required",
                    "source_filename": step.get("source_filename"),
                    "expected_filename": step.get("expected_filename"),
                    "action_text": step.get("action_text"),
                }
            )
            continue
        if action in {"click_selector", "fill_selector", "long_press_selector"} and not step.get("selector"):
            blocking.append(
                {
                    "trace_id": step.get("trace_id"),
                    "step_index": step.get("step_index"),
                    "reason": "selector_action_missing_selector",
                    "source_filename": step.get("source_filename"),
                    "expected_filename": step.get("expected_filename"),
                    "action_text": step.get("action_text"),
                }
            )
        if action == "fill_selector" and step.get("input_text") is None:
            blocking.append(
                {
                    "trace_id": step.get("trace_id"),
                    "step_index": step.get("step_index"),
                    "reason": "fill_selector_missing_input_text",
                    "source_filename": step.get("source_filename"),
                    "expected_filename": step.get("expected_filename"),
                    "action_text": step.get("action_text"),
                }
            )
    by_reason: Dict[str, int] = {}
    for item in blocking:
        reason = str(item.get("reason") or "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    report = {
        "format": "playwright_replay_plan_readiness",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.replay.playwright_replay_plan_readiness",
            "artifact_kind": "validation_report",
            "api_or_det": "det",
            "artifact_role": "validation_gate",
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
            "logical_owner": "l0_execution",
            "semantic_role": "playwright_replay_plan_readiness_gate",
            "producer": "site_model.page_execution_obligations.build_replay_plan_readiness",
            "allowed_consumers": ["pipeline_gate", "playwright_replay_executor", "developer"],
        },
        "source_selection": {
            "derived_from": ["projections/replay/playwright_replay_plan.json"],
        },
        "summary": {
            "ready": not blocking,
            "step_count": len(steps),
            "blocking_count": len(blocking),
            "manual_declared_count": len(manual_declared),
            "by_blocking_reason": by_reason,
        },
        "blocking_steps": blocking,
        "manual_declared_steps": manual_declared,
    }
    return report


def write_replay_plan_readiness(path: str, artifact: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)


def validate_replay_visible_topmost_probe_results(
    probe_results: List[Dict[str, Any]],
) -> Dict[str, Any]:

    errors: List[Dict[str, Any]] = []
    probes = [probe for probe in (probe_results or []) if isinstance(probe, dict)]
    for probe in probes:
        bbox = probe.get("bounding_box") if isinstance(probe.get("bounding_box"), dict) else {}
        try:
            area = float(bbox.get("width") or 0) * float(bbox.get("height") or 0)
        except Exception:
            area = 0
        try:
            opacity = float(probe.get("opacity") if probe.get("opacity") is not None else 1)
        except Exception:
            opacity = 1
        pointer_events = str(probe.get("pointer_events") or "auto").strip().lower()
        blocking_reason = ""
        if not probe.get("exists"):
            blocking_reason = "selector_missing"
        elif not probe.get("visible"):
            blocking_reason = "selector_not_visible_topmost"
        elif not probe.get("enabled", True):
            blocking_reason = "selector_not_visible_topmost"
        elif area <= 0:
            blocking_reason = "selector_not_visible_topmost"
        elif not probe.get("hit_target_is_self_or_descendant", False):
            blocking_reason = "selector_not_visible_topmost"
        elif opacity <= 0.05:
            blocking_reason = "selector_not_visible_topmost"
        elif pointer_events == "none":
            blocking_reason = "selector_not_visible_topmost"
        if blocking_reason:
            errors.append(
                {
                    "code": blocking_reason,
                    "severity": "blocking",
                    "trace_id": probe.get("trace_id"),
                    "step_index": probe.get("step_index"),
                    "selector": probe.get("selector", ""),
                    "suggested_fix": "bind data-element-id to visible interaction surface",
                    "probe": probe,
                }
            )
    return {
        "format": "replay_visible_topmost_validation",
        "schema_revision": 1,
        "probe_count": len(probes),
        "blocking_error_count": len(errors),
        "errors": errors,
    }
