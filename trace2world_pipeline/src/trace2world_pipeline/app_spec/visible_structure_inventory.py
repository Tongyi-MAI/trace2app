"""Source-linked visible structure extracted after L0 screenshot selection.

The API describes one selected Reference observation at a time.  The reducer
only joins rows for which the API supplied the same component key under an
already reconciled general-page identity; it never invents page semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from trace2world_pipeline.app_spec.artifact_index import register_artifact
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async, encode_image
from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.page_realizer.pagegen_input_scope import step_key_from_screenshot_ref


FORMAT = "trace2world.visible_structure_inventory.v1"
PROMPT_REVISION = 1
_COMPONENT_KEY = re.compile(r"^[a-z][a-z0-9_.-]*$")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _normalize_response(response: Any, *, filename: str, step_key: str) -> dict[str, Any]:
    if isinstance(response, str):
        response = json.loads(response)
    if not isinstance(response, dict) or not isinstance(response.get("regions"), list):
        raise ValueError(f"{filename}:{step_key}: API response requires a regions list")
    regions = []
    for index, region in enumerate(response["regions"]):
        if not isinstance(region, dict):
            raise ValueError(f"{filename}:{step_key}: region {index} must be an object")
        key = str(region.get("component_key") or "").strip().lower()
        if not _COMPONENT_KEY.fullmatch(key):
            raise ValueError(f"{filename}:{step_key}: invalid component_key {key!r}")
        kind = str(region.get("component_type") or "").strip()
        if not kind:
            raise ValueError(f"{filename}:{step_key}: component {key} has no type")
        parent_key = str(region.get("parent_component_key") or "").strip().lower()
        if parent_key and not _COMPONENT_KEY.fullmatch(parent_key):
            raise ValueError(f"{filename}:{step_key}: invalid parent_component_key {parent_key!r}")
        visibility = str(region.get("action_host_visibility") or "unclear").strip()
        if visibility not in {"visible", "not_visible", "unclear"}:
            raise ValueError(f"{filename}:{step_key}: invalid action host visibility")
        regions.append({
            "component_key": key,
            "component_type": kind,
            "parent_component_key": parent_key,
            "visual_region": str(region.get("visual_region") or "").strip(),
            "visible_labels": _strings(region.get("visible_labels", []), "visible_labels"),
            "visible_entity_fields": _strings(region.get("visible_entity_fields", []), "visible_entity_fields"),
            "visible_option_values": _strings(region.get("visible_option_values", []), "visible_option_values"),
            "visible_state_values": _strings(region.get("visible_state_values", []), "visible_state_values"),
            "state_fields": _strings(region.get("state_fields", []), "state_fields"),
            "parameterizable_fields": _strings(region.get("parameterizable_fields", []), "parameterizable_fields"),
            "repeatable_item_fields": _strings(region.get("repeatable_item_fields", []), "repeatable_item_fields"),
            "action_host_visibility": visibility,
        })
    exclusion = str(response.get("exclusion_reason") or "").strip()
    if not regions and not exclusion:
        raise ValueError(f"{filename}:{step_key}: empty extraction needs an exclusion_reason")
    if regions and exclusion:
        raise ValueError(f"{filename}:{step_key}: regions and exclusion_reason conflict")
    return {"regions": regions, "exclusion_reason": exclusion}


def _prompt(*, filename: str, page_name: str, general_page_id: str, step_key: str, render_role: str) -> str:
    return f"""Extract visible structure from ONE selected real-app screenshot.
Page instance: {filename}; page name: {page_name}; reconciled general page: {general_page_id}.
Source step: {step_key}; render role: {render_role or 'unknown'}.
Return one JSON object with regions and exclusion_reason. For every visible region,
return component_key (stable lower-case semantic role within this general page),
component_type, parent_component_key, visual_region, visible_labels,
visible_entity_fields, visible_option_values, visible_state_values,
state_fields, parameterizable_fields, repeatable_item_fields (arrays of strings),
and action_host_visibility (visible|not_visible|unclear). Keep distinct controls
and overlays distinct; keep the same role across entity values or scroll states.
Record only what this image visibly supports. Do not infer a route, effect,
guard, hidden field, independence of information sources, or task solution.
If no usable app structure is visible, return regions=[] and a concrete
exclusion_reason. Preserve the original language of labels.
Prompt revision: {PROMPT_REVISION}."""


def _general_page_id(proto: Any) -> str:
    structure = getattr(proto, "structure_data", {}) or {}
    return str(
        structure.get("general_page_id")
        or structure.get("source_cluster_id")
        or structure.get("cluster_id")
        or getattr(proto, "page_id", "")
        or ""
    ).strip()


def _write_formal_inventory(
    phase_output: PhaseOutputManager,
    rel_path: str,
    payload: dict[str, Any],
    *,
    artifact_id: str,
    selection_path: Path,
) -> Path:
    path = phase_output.write_json("l0_fidelity", "formal", rel_path, payload, register=False)
    register_artifact(
        phase_output.result_dir, path,
        artifact_id=artifact_id,
        phase="l0_fidelity",
        artifact_kind="api_semantic_source",
        api_or_det="mixed",
        canonical_source=True,
        logical_owner="app_spec.visible_structure_inventory",
        producer="build_visible_structure_inventory",
        allowed_consumers=("task_generation", "environment_export", "source_review"),
        semantic_role="source_linked_visible_structure",
        source_selection={"l0_selection_path": str(selection_path)},
        canonical_semantic_source=True,
        refresh_filesystem_summary=False,
    )
    return path


def _merge_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    list_fields = (
        "page_ids", "filenames", "evidence_refs", "screenshot_refs", "visible_labels",
        "visible_entity_fields", "visible_option_values", "visible_state_values",
        "state_fields", "parameterizable_fields", "repeatable_item_fields",
        "render_roles", "parent_component_keys", "visual_regions",
    )
    for observation in observations:
        for region in observation["regions"]:
            key = (observation["general_page_id"], region["component_key"])
            row = grouped.get(key)
            if row is None:
                row = {
                    "structure_id": f"structure::{_digest(json.dumps(key).encode())[:20]}",
                    "surface_id": observation["general_page_id"],
                    "general_page_id": observation["general_page_id"],
                    "component_key": region["component_key"],
                    "component_type": region["component_type"],
                    "action_host_visibility": region["action_host_visibility"],
                    "authorization": "selected_reference_visual_observation",
                    "support_status": "visible_structure_only",
                    "prohibited_inferences": ["unobserved action effects", "unobserved routes", "hidden business fields"],
                    **{field: [] for field in list_fields},
                }
                grouped[key] = row
            elif row["component_type"] != region["component_type"]:
                raise ValueError(f"conflicting component types for general-page key {key}")
            if row["action_host_visibility"] != region["action_host_visibility"]:
                row["action_host_visibility"] = "unclear"
            additions = {
                "page_ids": [observation["page_id"]],
                "filenames": [observation["filename"]],
                "evidence_refs": [observation["step_key"]],
                "screenshot_refs": [observation["screenshot_ref"]],
                "render_roles": [observation["render_role"]] if observation["render_role"] else [],
                "parent_component_keys": [region["parent_component_key"]] if region["parent_component_key"] else [],
                "visual_regions": [region["visual_region"]] if region["visual_region"] else [],
                **{field: region[field] for field in list_fields if field in region},
            }
            for field, values in additions.items():
                row[field] = list(dict.fromkeys([*row[field], *values]))
    return [grouped[key] for key in sorted(grouped)]


async def build_visible_structure_inventory(
    *,
    selection_path: Path,
    site_model: Any,
    output_dir: Path,
    trace_root: Path,
    model: str,
    reasoning_effort: str = "medium",
    max_concurrent: int = 8,
    api_caller: Callable[..., Any] = call_openai_api_json_async,
) -> Path:
    """Call the multimodal API for every selected image and publish one app inventory."""
    selection_path = Path(selection_path)
    output_dir = Path(output_dir)
    trace_root = Path(trace_root)
    selection_sha = _file_digest(selection_path)
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection.get("format") != "l0_visual_evidence_selection":
        raise ValueError("visible structure inventory requires a formal L0 selection manifest")
    selected_pages = selection.get("pages")
    if not isinstance(selected_pages, dict) or not selected_pages:
        raise ValueError("L0 selection has no page instances")
    prototypes = {proto.filename: proto for proto in site_model.structure.page_prototypes}
    debug_dir = output_dir / "phase_l0_fidelity/debug/structure_extraction"
    debug_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
    jobs: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    excluded_pages: list[dict[str, str]] = []
    for filename, page in sorted(selected_pages.items()):
        if filename not in prototypes:
            raise ValueError(f"selected page has no AppSpec prototype: {filename}")
        if not isinstance(page, dict):
            raise ValueError(f"invalid L0 page selection: {filename}")
        proto = prototypes[filename]
        general_id = _general_page_id(proto)
        if not general_id:
            raise ValueError(f"selected page lacks reconciled general identity: {filename}")
        refs = page.get("selected_screenshot_refs") or []
        if not isinstance(refs, list) or len(refs) != len(set(refs)):
            raise ValueError(f"invalid or duplicate selected screenshots: {filename}")
        page_entry = {
            "page_id": proto.page_id,
            "filename": filename,
            "general_page_id": general_id,
            "selected_screenshot_refs": refs,
            "selected_step_keys": [],
            "structure_row_ids": [],
        }
        pages.append(page_entry)
        if not refs:
            excluded_pages.append({"filename": filename, "reason": "no_selected_visual_evidence"})
            continue
        roles = page.get("screenshot_render_roles") or {}
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError(f"invalid screenshot ref: {filename}")
            image_path = Path(ref)
            if not image_path.is_absolute():
                image_path = trace_root / image_path
            if not image_path.is_file():
                raise FileNotFoundError(f"selected source screenshot missing: {image_path}")
            step_key = str(step_key_from_screenshot_ref(ref) or "").strip()
            if not step_key:
                raise ValueError(f"selected screenshot lacks source step key: {filename}: {ref}")
            page_entry["selected_step_keys"].append(step_key)
            jobs.append({
                "filename": filename, "page_id": proto.page_id,
                "page_name": proto.page_name, "general_page_id": general_id,
                "screenshot_ref": ref, "image_path": image_path, "step_key": step_key,
                "render_role": str(roles.get(step_key) or ""),
            })
    if not jobs:
        raise ValueError("L0 selection contains no screenshots for structure extraction")

    async def extract(job: dict[str, Any]) -> dict[str, Any]:
        prompt = _prompt(**{key: job[key] for key in ("filename", "page_name", "general_page_id", "step_key", "render_role")})
        image_sha = _file_digest(job["image_path"])
        input_hash = _digest(json.dumps({
            "prompt": prompt, "image_sha256": image_sha, "model": model,
            "reasoning_effort": reasoning_effort,
        }, ensure_ascii=False, sort_keys=True).encode())
        packet_path = debug_dir / f"{input_hash}.json"
        if packet_path.is_file():
            try:
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                if isinstance(packet, dict) and packet.get("input_sha256") == input_hash:
                    normalized = _normalize_response(packet.get("response"), filename=job["filename"], step_key=job["step_key"])
                    return {**job, **normalized}
            except (OSError, ValueError, json.JSONDecodeError):
                pass  # A partial debug packet has no semantic authority.
        async with semaphore:
            messages = [{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": encode_image(str(job["image_path"]))},
            ]}]
            response, usage = await api_caller(
                messages, max_tokens=4096, model=model, reasoning_effort=reasoning_effort,
                stage="l0_visible_structure_extraction",
            )
        normalized = _normalize_response(response, filename=job["filename"], step_key=job["step_key"])
        packet_bytes = json.dumps({
            "format": "trace2world.instance_visible_structure.v1",
            "input_sha256": input_hash,
            "image_sha256": image_sha,
            "model": model, "reasoning_effort": reasoning_effort,
            "prompt": prompt, "screenshot_ref": job["screenshot_ref"],
            "filename": job["filename"], "step_key": job["step_key"],
            "render_role": job["render_role"],
            "response": normalized, "usage": usage if isinstance(usage, dict) else {},
        }, ensure_ascii=False, indent=2).encode("utf-8")
        packet_temp = packet_path.with_suffix(".json.tmp")
        packet_temp.write_bytes(packet_bytes)
        packet_temp.replace(packet_path)
        return {**job, **normalized}

    observations = await asyncio.gather(*(extract(job) for job in jobs))
    observations_by_page: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        observations_by_page.setdefault(observation["filename"], []).append(observation)
    phase_output = PhaseOutputManager(output_dir)
    for filename, page_observations in sorted(observations_by_page.items()):
        page_rows = _merge_observations(page_observations)
        _write_formal_inventory(
            phase_output,
            f"instance_structure/{_digest(filename.encode())[:20]}.json",
            {
                "format": "trace2world.instance_page_structure.v1",
                "filename": filename,
                "source_l0_selection_sha256": selection_sha,
                "general_page_id": page_observations[0]["general_page_id"],
                "selected_step_keys": [item["step_key"] for item in page_observations],
                "selected_screenshot_refs": [item["screenshot_ref"] for item in page_observations],
                "rows": page_rows,
                "explicit_exclusions": [
                    {"step_key": item["step_key"], "reason": item["exclusion_reason"]}
                    for item in page_observations if item["exclusion_reason"]
                ],
            },
            artifact_id=f"l0_fidelity.instance_structure.{_digest(filename.encode())[:20]}",
            selection_path=selection_path,
        )
    rows = _merge_observations(observations)
    if not rows:
        raise ValueError("selected L0 screenshots yielded no usable visible structure")
    rows_by_page: dict[str, list[str]] = {}
    for row in rows:
        for filename in row["filenames"]:
            rows_by_page.setdefault(filename, []).append(row["structure_id"])
    for page in pages:
        page["structure_row_ids"] = sorted(rows_by_page.get(page["filename"], []))
    exclusions = [
        {"filename": item["filename"], "step_key": item["step_key"], "reason": item["exclusion_reason"]}
        for item in observations if item["exclusion_reason"]
    ]
    if sum(len(item["selected_screenshot_refs"]) for item in pages) != len(observations):
        raise ValueError("selected screenshot coverage changed during extraction")
    payload = {
        "format": FORMAT, "schema_revision": 1,
        "inventory_scope": "selected_l0_instance_pages",
        "source_l0_selection_path": str(selection_path),
        "source_l0_selection_sha256": selection_sha,
        "trace_ids": list(site_model.trace_ids),
        "canonical_page_count": len(pages),
        "selected_screenshot_count": len(observations),
        "structure_row_count": len(rows),
        "coverage_status": "complete_for_selected_screenshots",
        "extraction_method": "multimodal_api_per_selected_screenshot",
        "pages": pages, "rows": rows,
        "explicit_exclusions": [*excluded_pages, *exclusions],
    }
    return _write_formal_inventory(
        phase_output, "visible_structure_inventory.json", payload,
        artifact_id="l0_fidelity.visible_structure_inventory",
        selection_path=selection_path,
    )
