
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from trace2world_pipeline.page_realizer.pagegen_input_scope import step_key_from_screenshot_ref
from trace2world_pipeline.page_realizer.pagegen_input_scope import normalize_step_key
from trace2world_pipeline.page_realizer.resource_semantics import (
    format_pagegen_resource_list,
    pagegen_resource_prompt_omission_reason,
)


PROJECTION_FORMAT = "pagegen_resource_projection"
SCHEMA_REVISION = 2

_PUBLIC_RESOURCE_FIELDS = (
    "resource_ref",
    "source_screenshot_ref",
    "source_step_key",
    "asset_name",
    "asset_path",
    "width",
    "height",
    "scaled_width",
    "scaled_height",
    "semantic_label",
    "asset_kind",
    "sprite_group",
    "visual_role",
    "source_screenshot_role",
    "initial_dom_eligible",
    "allowed_usage",
    "prompt_visibility",
    "resource_origin",
    "provider",
    "source_url",
    "license",
    "license_url",
    "provenance_path",
    "external_query",
)

_SEMANTIC_FIELDS = (
    "semantic_label",
    "asset_kind",
    "sprite_group",
    "visual_role",
    "prompt_visibility",
)


def _slug(value: Any, *, fallback: str = "x") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    text = text or fallback
    return text[:96].strip("._") or fallback


def make_pagegen_asset_name(
    *,
    page_id: str,
    source_screenshot_ref: str,
    resource_index: int,
    execution_flow: str,
    extension: str = ".jpg",
) -> str:

    ext = str(extension or ".jpg")
    if not ext.startswith("."):
        ext = f".{ext}"
    prefix = _slug(page_id, fallback="page")
    source = _slug(source_screenshot_ref, fallback="source")
    return f"{prefix}__{source}__r{int(resource_index)}{ext}"


def _normalize_ref(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(.+):step(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    match = re.match(r"^(.+):(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{int(match.group(2))}"
    return text


def _normalize_render_roles(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    roles: Dict[str, str] = {}
    for key, role in value.items():
        text = str(role or "").strip()
        if text:
            roles[normalize_step_key(str(key))] = text
    return roles


def _resource_lifecycle_for_role(role: str) -> Dict[str, Any]:
    if role == "canonical_layout":
        return {
            "source_screenshot_role": role,
            "initial_dom_eligible": True,
            "allowed_usage": "initial_dom",
        }
    if role in {"interaction_state_change", "post_interaction_overlay"}:
        return {
            "source_screenshot_role": role,
            "initial_dom_eligible": False,
            "allowed_usage": "state_transition_only",
        }
    if role == "runtime_status":
        return {
            "source_screenshot_role": role,
            "initial_dom_eligible": False,
            "allowed_usage": "runtime_only",
        }
    return {
        "source_screenshot_role": "unknown_render_role",
        "initial_dom_eligible": False,
        "allowed_usage": "blocked_unknown",
    }


def _prompt_ref_set(prompt_screenshot_refs: Iterable[Any]) -> set[str]:
    refs: set[str] = set()
    for value in prompt_screenshot_refs or []:
        text = str(value or "").strip()
        if not text:
            continue
        refs.add(_normalize_ref(text))
        step_key = step_key_from_screenshot_ref(text)
        if step_key:
            refs.add(_normalize_ref(step_key))
    return refs


def _resource_ref(resource: Mapping[str, Any], index: int) -> str:
    for key in ("resource_ref", "asset_name", "asset_path", "image_path", "path"):
        value = resource.get(key)
        if value not in (None, ""):
            return str(value)
    return f"resource_{index}"


def _resource_asset_name(resource: Mapping[str, Any]) -> str:
    value = resource.get("asset_name")
    if value not in (None, ""):
        return str(value)
    asset_path = resource.get("asset_path")
    if asset_path not in (None, ""):
        return str(asset_path).replace("\\", "/").rsplit("/", 1)[-1]
    return ""


def _resource_asset_path(resource: Mapping[str, Any]) -> str:
    asset_name = resource.get("asset_name")
    if asset_name:
        return f"images/{asset_name}"
    for key in ("asset_path", "image_path", "path"):
        value = resource.get(key)
        if value not in (None, ""):
            text = str(value).replace("\\", "/")
            if text.startswith("images/"):
                return text
            return f"images/{text.rsplit('/', 1)[-1]}"
    return ""


def _resource_source_refs(resource: Mapping[str, Any]) -> List[str]:
    refs: List[str] = []
    for key in (
        "source_screenshot_ref",
        "source_screenshot_path",
        "screenshot_ref",
        "screenshot_path",
        "source_step_key",
        "step_key",
    ):
        value = resource.get(key)
        if value in (None, ""):
            continue
        refs.append(_normalize_ref(value))
        step_key = step_key_from_screenshot_ref(str(value))
        if step_key:
            refs.append(_normalize_ref(step_key))
    for key in ("source_screenshot_refs", "screenshot_refs", "source_step_keys"):
        value = resource.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            refs.append(_normalize_ref(item))
            step_key = step_key_from_screenshot_ref(str(item))
            if step_key:
                refs.append(_normalize_ref(step_key))
    return [ref for idx, ref in enumerate(refs) if ref and ref not in refs[:idx]]


def _first_source_ref(resource: Mapping[str, Any]) -> str:
    refs = _resource_source_refs(resource)
    return refs[0] if refs else ""


def _source_step_key(resource: Mapping[str, Any]) -> str:
    for value in (
        resource.get("source_step_key"),
        resource.get("step_key"),
        _first_source_ref(resource),
    ):
        if value in (None, ""):
            continue
        step_key = step_key_from_screenshot_ref(str(value))
        return _normalize_ref(step_key or value)
    return ""


def _resource_matches_prompt(resource: Mapping[str, Any], prompt_refs: set[str]) -> bool:
    if not prompt_refs:
        return False
    return bool(set(_resource_source_refs(resource)) & prompt_refs)


def _semantic_from_cache(resource: Mapping[str, Any], semantic_cache: Any) -> Dict[str, Any]:
    candidates: List[Mapping[str, Any]] = []
    annotation = resource.get("pagegen_semantic_annotation")
    if isinstance(annotation, Mapping):
        candidates.append(annotation)
    if isinstance(semantic_cache, Mapping):
        resource_ref = str(resource.get("resource_ref") or resource.get("asset_name") or "")
        direct = semantic_cache.get(resource_ref) if resource_ref else None
        if isinstance(direct, Mapping):
            candidates.append(direct)
        for key in ("resources", "dedup_page_resources"):
            value = semantic_cache.get(key)
            if isinstance(value, Mapping):
                direct = value.get(resource_ref) if resource_ref else None
                if isinstance(direct, Mapping):
                    candidates.append(direct)
            elif isinstance(value, list):
                for item in value:
                    if not isinstance(item, Mapping):
                        continue
                    item_ref = str(item.get("resource_ref") or item.get("asset_name") or "")
                    if resource_ref and item_ref == resource_ref:
                        item_annotation = item.get("pagegen_semantic_annotation")
                        candidates.append(item_annotation if isinstance(item_annotation, Mapping) else item)

    merged: Dict[str, Any] = {}
    for candidate in candidates:
        for field in _SEMANTIC_FIELDS:
            value = candidate.get(field)
            if value not in (None, "", [], {}):
                merged[field] = value
    return merged


def _scaled_dimension(resource: Mapping[str, Any], axis: str) -> Optional[Any]:
    key = f"scaled_{axis}"
    if resource.get(key) not in (None, ""):
        return resource.get(key)
    render_key = f"render_{axis}"
    if resource.get(render_key) not in (None, ""):
        return resource.get(render_key)
    render_bbox = resource.get("render_bbox")
    if isinstance(render_bbox, Mapping) and render_bbox.get(axis) not in (None, ""):
        return render_bbox.get(axis)
    return None


def build_pagegen_resource_projection(
    *,
    page_id: str,
    prompt_screenshot_refs: Iterable[Any],
    resource_screenshot_refs: Optional[Iterable[Any]] = None,
    screenshot_render_roles: Any = None,
    resources: Iterable[Mapping[str, Any]],
    semantic_cache: Any,
) -> Dict[str, Any]:

    prompt_refs = _prompt_ref_set(prompt_screenshot_refs or [])
    resource_scope_values = list(resource_screenshot_refs or prompt_screenshot_refs or [])
    resource_refs = _prompt_ref_set(resource_scope_values)
    render_roles = _normalize_render_roles(screenshot_render_roles)
    projected_resources: List[Dict[str, Any]] = []
    resource_scope_matched_count = 0
    prompt_matched_count = 0
    omitted_counts: Dict[str, int] = {}
    for index, resource in enumerate(resources or []):
        if not isinstance(resource, Mapping):
            continue
        if not _resource_matches_prompt(resource, resource_refs):
            continue
        resource_scope_matched_count += 1
        matches_prompt = _resource_matches_prompt(resource, prompt_refs)
        if matches_prompt:
            prompt_matched_count += 1
        semantic = _semantic_from_cache(resource, semantic_cache)
        resource_for_policy: Dict[str, Any] = dict(resource)


        resource_for_policy["prompt_visibility"] = (
            "prompt" if matches_prompt else "resource_scope"
        )
        if semantic:
            annotation = resource_for_policy.get("pagegen_semantic_annotation")
            annotation = dict(annotation) if isinstance(annotation, Mapping) else {}
            annotation.update({
                key: value
                for key, value in semantic.items()
                if value not in (None, "", [], {})
            })
            resource_for_policy["pagegen_semantic_annotation"] = annotation
            if semantic.get("prompt_visibility") not in (None, "", [], {}):
                resource_for_policy["prompt_visibility"] = semantic.get("prompt_visibility")
        omission_reason = pagegen_resource_prompt_omission_reason(resource_for_policy)
        if omission_reason:
            omitted_counts[omission_reason] = omitted_counts.get(omission_reason, 0) + 1
            continue
        item: Dict[str, Any] = {
            "resource_ref": _resource_ref(resource, index),
            "source_screenshot_ref": _first_source_ref(resource),
            "source_step_key": _source_step_key(resource),
            "asset_name": _resource_asset_name(resource),
            "asset_path": _resource_asset_path(resource),
            "width": resource.get("width"),
            "height": resource.get("height"),
            "scaled_width": _scaled_dimension(resource, "width"),
            "scaled_height": _scaled_dimension(resource, "height"),
            "prompt_visibility": "prompt" if matches_prompt else "resource_scope",
        }
        item.update(
            {
                field: resource.get(field)
                for field in (
                    "resource_origin",
                    "provider",
                    "source_url",
                    "license",
                    "license_url",
                    "provenance_path",
                    "external_query",
                )
                if resource.get(field) not in (None, "", [], {})
            }
        )
        item.update(_resource_lifecycle_for_role(render_roles.get(item["source_step_key"], "")))
        item.update(semantic)
        item["prompt_visibility"] = "prompt" if matches_prompt else "resource_scope"
        projected_resources.append(
            {
                field: item[field]
                for field in _PUBLIC_RESOURCE_FIELDS
                if item.get(field) not in (None, "", [], {})
            }
        )

    return {
        "format": PROJECTION_FORMAT,
        "schema_revision": SCHEMA_REVISION,
        "artifact_role": "projection",
        "canonical_prompt_boundary": True,
        "canonical_semantic_source": False,
        "page_id": str(page_id or ""),
        "prompt_screenshot_refs": list(prompt_screenshot_refs or []),
        "resource_screenshot_refs": resource_scope_values,
        "selection_policy": {
            "source": "pagegen_materialized_page_resource_policy",
            "resource_scope_matched_resource_count": resource_scope_matched_count,
            "prompt_matched_resource_count": prompt_matched_count,
            "authorized_resource_count": len(projected_resources),
            "prompt_visible_resource_count": len(projected_resources),
            "omitted_resource_count": sum(omitted_counts.values()),
            "omitted_counts": omitted_counts,
        },
        "resources": projected_resources,
    }


def _resource_list_item_from_projection(resource: Mapping[str, Any]) -> Dict[str, Any]:
    annotation = {
        field: resource.get(field)
        for field in _SEMANTIC_FIELDS
        if resource.get(field) not in (None, "", [], {})
    }
    annotation.setdefault("prompt_visibility", str(resource.get("prompt_visibility") or "prompt"))
    item: Dict[str, Any] = {
        "resource_ref": resource.get("resource_ref"),
        "asset_name": resource.get("asset_name"),
        "path": resource.get("asset_path") or _resource_asset_path(resource),
        "width": resource.get("width"),
        "height": resource.get("height"),
        "render_width": resource.get("scaled_width"),
        "render_height": resource.get("scaled_height"),
        "source_screenshot_ref": resource.get("source_screenshot_ref"),
        "source_step_key": resource.get("source_step_key"),
        "prompt_visibility": str(resource.get("prompt_visibility") or "prompt"),
        "pagegen_semantic_annotation": annotation,
    }
    return {
        key: value
        for key, value in item.items()
        if value not in (None, "", [], {})
    }


def format_pagegen_resource_list_from_projection(
    projection: Mapping[str, Any],
    *,
    scope: str,
) -> str:

    resources = projection.get("resources") if isinstance(projection, Mapping) else []
    resource_items = [
        _resource_list_item_from_projection(resource)
        for resource in resources or []
        if isinstance(resource, Mapping)
    ]
    text = format_pagegen_resource_list(
        resource_items,
        scope=scope,
        semantic_enabled=True,
    )
    selection_policy = projection.get("selection_policy") if isinstance(projection, Mapping) else {}
    omitted_counts = (
        selection_policy.get("omitted_counts")
        if isinstance(selection_policy, Mapping)
        else {}
    )
    if isinstance(omitted_counts, Mapping) and omitted_counts:
        summary = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(omitted_counts.items())
            if count
        )
        if summary:
            omitted_line = (
                f"Omitted {sum(int(count or 0) for count in omitted_counts.values())} "
                f"prompt-hidden resource(s) from projection ({summary}); render those controls, badges, "
                "and decorative symbols with Lucide/Tailwind/CSS/text."
            )
            if not resource_items:
                return (
                    "No exact local resource image paths are authorized for this page. "
                    "Use the screenshots and Page Environment Spec for layout/content authority; "
                    "draw generic controls, badges, text labels, prices, ratings, and status markers "
                    "with HTML/CSS/Lucide instead of extracted bitmap resources.\n"
                    f"{omitted_line}\n\n"
                    "Do not invent remote image URLs, generated data URI placeholders, emoji, or text placeholders."
                )
            text = text.replace(
                "\n\nUse real images, NOT emoji/text placeholders.",
                f"\n{omitted_line}\n\nUse real images, NOT emoji/text placeholders.",
            )
    return text
