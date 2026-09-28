
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Tuple

from PIL import Image

from trace2world_pipeline.core.llm_caller import call_openai_api_json_async, encode_image
from trace2world_pipeline.utils.page_gen_utils import (
    ASSET_CELL_SIZE,
    ICON_CELL_SIZE,
    PHOTO_CELL_SIZE,
    _resource_sprite_kind,
)


RESOURCE_TEXT_VERSION = "pagegen_resource_text_semantic_v7_asset_kind_groups"
LEGACY_RESOURCE_TEXT_VERSION = "pagegen_resource_text_legacy_v2_size_only"

_SEMANTIC_FIELDS = (
    "semantic_label",
    "asset_kind",
    "sprite_group",
    "visual_role",
    "text_only",
)

_RESOURCE_LIST_SEMANTIC_FIELDS = (
    "semantic_label",
)

_CORE_SEMANTIC_FIELDS = (
    "asset_kind",
    "sprite_group",
    "visual_role",
)

_SPRITE_CELL_SIZE = {
    "icon": ICON_CELL_SIZE,
    "photo": PHOTO_CELL_SIZE,
    "asset": ASSET_CELL_SIZE,
}

_SPRITE_GROUP_ORDER = ("icon", "photo", "asset")

_BASIC_SHELL_ICON_LABEL_MARKERS = (
    "back",
    "return",
    "arrow",
    "chevron",
    "close",
    "clear",
    "search",
    "magnifier",
    "cart",
    "shopping cart",
    "more",
    "overflow",
    "menu/options",
    "favorite",
    "star",
    "refresh",
    "返回",
    "箭头",
    "搜索",
    "放大镜",
    "购物车",
    "清空",
    "关闭",
    "更多",
    "刷新",
)

_FORM_CONTROL_ICON_LABEL_MARKERS = (
    "radio",
    "radio button",
    "checkbox",
    "check box",
    "toggle",
    "switch",
    "add",
    "plus",
    "edit",
    "pencil",
    "checked",
    "unchecked",
    "selected",
    "unselected",
    "add passenger",
    "edit entry",
    "单选",
    "复选",
    "多选",
    "添加",
    "新增",
    "编辑",
    "铅笔",
    "选中",
    "未选中",
    "已选",
    "未选",
    "开关",
)

_BUSINESS_ICON_LABEL_MARKERS = (
    "category",
    "coupon",
    "reward",
    "red envelope",
    "merchant",
    "logo",
    "product",
    "dish",
    "drink",
    "badge",
    "分类",
    "优惠",
    "红包",
    "商家",
    "商品",
    "菜品",
    "饮品",
    "标签",
)


def resource_text_version(semantic_enabled: bool) -> str:
    return RESOURCE_TEXT_VERSION if semantic_enabled else LEGACY_RESOURCE_TEXT_VERSION


def _size_label(resource: Dict[str, Any]) -> str:
    render_width = resource.get("render_width")
    render_height = resource.get("render_height")
    if render_width and render_height:
        return f"{render_width}x{render_height}"
    render_bbox = resource.get("render_bbox")
    if isinstance(render_bbox, dict) and render_bbox.get("width") and render_bbox.get("height"):
        return f"{render_bbox.get('width')}x{render_bbox.get('height')}"
    width = resource.get("width")
    height = resource.get("height")
    if width and height:
        return f"{width}x{height}"
    path = resource.get("path", "")
    try:
        with Image.open(path) as img:
            return f"{img.width}x{img.height}"
    except Exception:
        return "?x?"


def _source_pixel_size_label(resource: Dict[str, Any]) -> str:
    source_size = resource.get("source_pixel_size")
    if isinstance(source_size, dict) and source_size.get("width") and source_size.get("height"):
        return f"{source_size.get('width')}x{source_size.get('height')}"
    width = resource.get("width")
    height = resource.get("height")
    if width and height:
        return f"{width}x{height}"
    return ""


def _source_screenshot_size_label(resource: Dict[str, Any]) -> str:
    source_size = resource.get("source_screenshot_size")
    if isinstance(source_size, dict) and source_size.get("width") and source_size.get("height"):
        return f"{source_size.get('width')}x{source_size.get('height')}"
    return ""


def _resource_size_detail_label(resource: Dict[str, Any]) -> str:
    render_size = _size_label(resource)
    return f"size={render_size}"


def _render_bbox_label(resource: Dict[str, Any]) -> str:
    bbox = resource.get("render_bbox")
    if not isinstance(bbox, dict):
        return ""
    values = [bbox.get(key) for key in ("x", "y", "width", "height")]
    if any(value in (None, "") for value in values):
        return ""
    return "{},{},{},{}".format(*values)


def _raw_image_dimensions(resource: Dict[str, Any]) -> Tuple[int, int]:
    width = resource.get("width")
    height = resource.get("height")
    try:
        if width and height:
            return int(width), int(height)
    except Exception:
        pass
    source_size = resource.get("source_pixel_size")
    if isinstance(source_size, dict):
        try:
            return int(source_size.get("width") or 0), int(source_size.get("height") or 0)
        except Exception:
            pass
    path = resource.get("path", "")
    try:
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return 0, 0


def _sprite_fit_size(resource: Dict[str, Any], cell_size: int) -> str:
    width, height = _raw_image_dimensions(resource)
    if width <= 0 or height <= 0 or cell_size <= 0:
        return "?x?"
    scale = min(cell_size / width, cell_size / height)
    return f"{max(1, round(width * scale))}x{max(1, round(height * scale))}"


def _render_max_dimension(resource: Dict[str, Any]) -> int:
    try:
        width = int(resource.get("render_width") or 0)
        height = int(resource.get("render_height") or 0)
        if width > 0 and height > 0:
            return max(width, height)
    except Exception:
        pass
    size = _size_label(resource)
    try:
        width_text, height_text = size.lower().split("x", 1)
        return max(int(float(width_text)), int(float(height_text)))
    except Exception:
        return 0


def _asset_path(resource: Dict[str, Any], index: int) -> str:
    return f"images/{resource.get('asset_name', f'asset_{index}.jpg')}"


def _semantic_annotation(resource: Dict[str, Any]) -> Dict[str, Any]:
    annotation = resource.get("pagegen_semantic_annotation")
    if isinstance(annotation, dict):
        return annotation
    return {}


def _resource_priority_rank(resource: Dict[str, Any]) -> int:
    annotation = _semantic_annotation(resource)
    kind = str(annotation.get("asset_kind") or "").strip().lower()
    role = str(annotation.get("visual_role") or "").strip().lower()
    if kind in ("photo", "hero", "banner", "product"):
        return 0
    if kind == "icon" and role in ("category", "business", "brand"):
        return 1
    if kind == "icon":
        return 3
    return 2


def is_pagegen_implementation_icon(resource: Dict[str, Any]) -> bool:
    annotation = _semantic_annotation(resource)
    if not annotation:
        return False
    asset_kind = str(annotation.get("asset_kind") or "").strip().lower()
    visual_role = str(annotation.get("visual_role") or "").strip().lower()
    label = str(annotation.get("semantic_label") or "").strip().lower()
    if asset_kind not in {"icon", "marker", "unknown", "symbol_icon"} and visual_role not in {"icon", "decorative_image", "symbol_icon"}:
        return False
    if any(marker in label for marker in _BUSINESS_ICON_LABEL_MARKERS):
        return False
    if not any(
        marker in label
        for marker in (
            *_BASIC_SHELL_ICON_LABEL_MARKERS,
            *_FORM_CONTROL_ICON_LABEL_MARKERS,
        )
    ):
        return False
    return True


_PROMPT_OMISSION_HINTS = {
    "implementation_icon": "Use Lucide/Tailwind/CSS for this reusable shell or form-control icon; do not use the extracted bitmap resource.",
    "symbol_icon_omitted": "Simple generic UI control icon; render with Lucide/Tailwind/CSS instead of the extracted bitmap.",
    "source_icon_symbol_omitted": "Source categorized this as a small icon/symbol; render with HTML/CSS/Lucide instead of treating it as a photo.",
    "css_badge_or_symbol": "Small badge/tag/symbol crop; render with HTML/CSS/text instead of an extracted bitmap.",
    "text_or_label_crop": "This crop is text, a label, a price/status/rating marker, or button copy; render it as HTML text/CSS, not as a bitmap resource.",
    "screenshot_background_viewport_crop": "Large background crops that cover most of the source viewport are screenshot-derived layout evidence, not active reusable media resources.",
}

_TEXT_OR_LABEL_ASSET_KINDS = {
    "text",
    "text_label",
    "ui_text",
    "button",
    "button_label",
    "price",
    "price_tag",
    "rating",
    "rating_label",
    "status",
    "status_label",
    "tag",
    "text_badge",
}

_TEXT_OR_LABEL_ROLE_MARKERS = {
    "text",
    "label",
    "button_text",
    "price",
    "rating",
    "status",
    "caption",
    "tag",
}

_TEXT_OR_LABEL_LABEL_MARKERS = (
    "title text",
    "text crop",
    "text label",
    "button text",
    "button label",
    "price text",
    "price label",
    "rating text",
    "rating label",
    "status text",
    "status label",
    "ui text",
    "plain text",
    "typography",
    "文字",
    "文本",
    "按钮文字",
    "价格",
    "评分",
    "状态",
    "标签文字",
)

_FLAT_TEXT_STRIP_LABEL_MARKERS = (
    *_TEXT_OR_LABEL_LABEL_MARKERS,
    "section title",
    "event title",
    "content title",
    "card title",
    "feed title",
    "list title",
    "item title",
    "module title",
    " title",
    "headline",
    "price",
    "yuan",
    "discount",
    "percent off",
    "rating",
    "score",
    "coupon",
    "tag",
    "stat",
    "count",
    "wants to see",
    "标题",
    "票价",
    "折扣",
    "评分",
    "状态",
)


def _small_rendered_resource(resource: Dict[str, Any], max_side: int = 96) -> bool:
    width = resource.get("render_width", resource.get("display_width", resource.get("width")))
    height = resource.get("render_height", resource.get("display_height", resource.get("height")))
    try:
        width_i = int(float(width))
        height_i = int(float(height))
    except (TypeError, ValueError):
        return False
    return max(width_i, height_i) <= max_side


def _number_from_mapping(value: Any, key: str) -> float:
    if not isinstance(value, dict):
        return 0.0
    try:
        return float(value.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _number_field(resource: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = resource.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def _large_background_viewport_crop(resource: Dict[str, Any]) -> bool:

    if str(resource.get("original_category") or "").strip().lower() != "background":
        return False

    source_size = resource.get("source_pixel_size")
    source_width = _number_from_mapping(source_size, "width") or _number_field(resource, "width")
    source_height = _number_from_mapping(source_size, "height") or _number_field(resource, "height")
    screenshot_size = resource.get("source_screenshot_size")
    screenshot_width = _number_from_mapping(screenshot_size, "width")
    screenshot_height = _number_from_mapping(screenshot_size, "height")
    if screenshot_width > 0 and screenshot_height > 0 and source_width > 0 and source_height > 0:
        width_ratio = source_width / screenshot_width
        height_ratio = source_height / screenshot_height
        if width_ratio >= 0.85 and height_ratio >= 0.60:
            return True

    render_width = _number_field(resource, "render_width", "display_width")
    render_height = _number_field(resource, "render_height", "display_height")
    target_size = resource.get("target_viewport_size")
    target_width = _number_from_mapping(target_size, "width")
    target_height = _number_from_mapping(target_size, "height")
    if target_width > 0 and target_height > 0 and render_width > 0 and render_height > 0:
        width_ratio = render_width / target_width
        height_ratio = render_height / target_height
        if width_ratio >= 0.85 and height_ratio >= 0.60:
            return True
    return False


def _annotation_bool(annotation: Dict[str, Any], key: str) -> bool:
    value = annotation.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _flat_text_strip_candidate(resource: Dict[str, Any], label: str) -> bool:
    render_width = _number_field(resource, "render_width", "display_width", "width")
    render_height = _number_field(resource, "render_height", "display_height", "height")
    if render_width <= 0 or render_height <= 0:
        return False
    aspect_ratio = render_width / max(render_height, 1.0)
    if render_height > 28 or aspect_ratio < 2.2:
        return False
    return any(marker in label for marker in _FLAT_TEXT_STRIP_LABEL_MARKERS)


def pagegen_resource_prompt_omission_reason(resource: Dict[str, Any]) -> str:
    annotation = _semantic_annotation(resource)
    visibility = str(resource.get("prompt_visibility") or annotation.get("prompt_visibility") or "").strip()
    if _large_background_viewport_crop(resource):
        return "screenshot_background_viewport_crop"
    if annotation and _annotation_bool(annotation, "text_only"):
        return "text_or_label_crop"
    if annotation and visibility == "source_icon_symbol_omitted":
        return visibility
    if visibility == "prompt":
        return ""
    if not annotation:
        return visibility if visibility in _PROMPT_OMISSION_HINTS else ""
    if is_pagegen_implementation_icon(resource):
        return "implementation_icon"

    asset_kind = str(annotation.get("asset_kind") or "").strip().lower()
    visual_role = str(annotation.get("visual_role") or "").strip().lower()
    sprite_group = str(annotation.get("sprite_group") or "").strip().lower()
    label = str(annotation.get("semantic_label") or "").strip().lower()

    if (
        str(resource.get("original_category") or "").strip().lower() == "icon"
        and _small_rendered_resource(resource)
        and asset_kind in {"cover_image", "content_visual", "product_photo", "merchant_photo", "banner", "photo", "image"}
        and not any(marker in label for marker in _BUSINESS_ICON_LABEL_MARKERS)
    ):
        return "source_icon_symbol_omitted"
    if (
        asset_kind in _TEXT_OR_LABEL_ASSET_KINDS
        or visual_role in _TEXT_OR_LABEL_ROLE_MARKERS
        or any(marker in label for marker in _TEXT_OR_LABEL_LABEL_MARKERS)
        or _flat_text_strip_candidate(resource, label)
    ):
        return "text_or_label_crop"
    return ""


def is_pagegen_prompt_omitted_resource(resource: Dict[str, Any]) -> bool:
    return bool(pagegen_resource_prompt_omission_reason(resource))


def apply_pagegen_prompt_resource_policy(resource: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(resource or {})
    annotation = dict(_semantic_annotation(item))
    reason = pagegen_resource_prompt_omission_reason(item)
    if annotation and reason:
        annotation["prompt_visibility"] = reason
        annotation["implementation_hint"] = _PROMPT_OMISSION_HINTS.get(reason, "Do not expose this resource as a bitmap asset to PageGen.")
        item["pagegen_semantic_annotation"] = annotation
        item["prompt_visibility"] = reason
    return item


def _apply_prompt_resource_policy(resource: Dict[str, Any]) -> Dict[str, Any]:
    return apply_pagegen_prompt_resource_policy(resource)


def has_semantic_annotations(resources: Iterable[Dict[str, Any]]) -> bool:
    return any(_semantic_annotation(resource) for resource in resources)


def merge_resource_semantic_annotations(
    page_resources: List[Dict[str, Any]],
    labeled_resources: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    by_asset_step: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_asset: Dict[str, Dict[str, Any]] = {}
    for resource in labeled_resources or []:
        if not isinstance(resource, dict):
            continue
        annotation = _semantic_annotation(resource)
        if not annotation:
            continue
        key = (
            str(resource.get("asset_name") or ""),
            str(resource.get("source_step_key") or ""),
            str(resource.get("path") or ""),
        )
        by_key[key] = annotation
        asset_name = key[0]
        source_step_key = key[1]
        if asset_name and source_step_key:
            by_asset_step[(asset_name, source_step_key)] = annotation
        if asset_name and asset_name not in by_asset:
            by_asset[asset_name] = annotation
    merged: List[Dict[str, Any]] = []
    annotated_count = 0
    for resource in page_resources or []:
        item = dict(resource)
        key = (
            str(item.get("asset_name") or ""),
            str(item.get("source_step_key") or ""),
            str(item.get("path") or ""),
        )
        annotation = (
            by_key.get(key)
            or by_asset_step.get((key[0], key[1]))
            or by_asset.get(key[0])
        )
        if annotation:
            item["pagegen_semantic_annotation"] = annotation
            item = _apply_prompt_resource_policy(item)
            annotated_count += 1
        merged.append(item)
    return merged, {
        "enabled": True,
        "source": "cascade_round_aggregate",
        "resource_count": len(page_resources or []),
        "annotated_count": annotated_count,
        "round_labeled_resource_count": len(by_key),
    }


def validate_page_wise_resource_labeling_calls(
    call_records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:

    records = [record for record in (call_records or []) if isinstance(record, dict)]
    errors: List[Dict[str, Any]] = []
    for record in records:
        refs = [
            str(ref)
            for ref in (record.get("source_screenshot_refs") or [])
            if str(ref or "").strip()
        ]
        if refs:
            errors.append(
                {
                    "code": "resource_labeling_used_screenshot_input",
                    "severity": "blocking",
                    "call_id": record.get("call_id", ""),
                    "source_screenshot_refs": refs,
                    "resource_refs": record.get("resource_refs") or [],
                }
            )
    return {
        "format": "resource_semantic_labeling_validation",
        "call_count": len(records),
        "blocking_error_count": len(errors),
        "errors": errors,
    }


def format_pagegen_resource_list(
    resources: List[Dict[str, Any]],
    *,
    scope: str,
    semantic_enabled: bool = False,
) -> str:
    if scope == "cascade_round":
        intro = (
            "Use these round-scoped resource images for this page patch. Reference by exact path. "
            "They were selected from the screenshots in this cascade round and deduplicated only within the round."
        )
    else:
        intro = "Use these resource images for this page. Reference by exact path."

    annotated = has_semantic_annotations(resources)
    if semantic_enabled and annotated:
        intro += (
            " Resources are grouped by semantic sprite sheet. "
            "size is the expected render size in the PageGen viewport. "
            "Draw only generic shell/form controls with Lucide/Tailwind/CSS/text; keep business, navigation, badge, category, and content icons as resource images."
        )
    else:
        intro += " Semantic labels are unavailable; infer usage only from the attached sprite sheets and screenshots."

    grouped: Dict[str, List[Tuple[int, int, str]]] = {key: [] for key in _SPRITE_GROUP_ORDER}
    omitted_counts: Dict[str, int] = {}
    for idx, raw_resource in enumerate(resources):
        resource = _apply_prompt_resource_policy(raw_resource)
        omission_reason = pagegen_resource_prompt_omission_reason(resource)
        if omission_reason:
            omitted_counts[omission_reason] = omitted_counts.get(omission_reason, 0) + 1
            continue
        sprite_kind = _resource_sprite_kind(resource)
        if sprite_kind not in grouped:
            sprite_kind = "asset"
        parts = [
            f"[{idx}] {_asset_path(resource, idx)}",
            _resource_size_detail_label(resource),
        ]
        annotation = _semantic_annotation(resource)
        if annotation:
            for field in _RESOURCE_LIST_SEMANTIC_FIELDS:
                value = annotation.get(field)
                if value:
                    parts.append(f"{field}={value}")
        priority_rank = _resource_priority_rank(resource)
        grouped[sprite_kind].append((priority_rank, idx, " | ".join(parts)))

    section_lines: List[str] = []
    if omitted_counts:
        summary = ", ".join(f"{reason}={count}" for reason, count in sorted(omitted_counts.items()))
        section_lines.append(
            f"Omitted {sum(omitted_counts.values())} prompt-hidden symbolic/chrome resource(s) ({summary}); use Lucide/Tailwind/CSS/text for these controls, badges, and decorative symbols."
        )
    for kind in _SPRITE_GROUP_ORDER:
        entries = grouped.get(kind) or []
        if not entries:
            continue
        entries.sort()
        section_lines.append(f"# {kind} group")
        section_lines.extend(line for _rank, _idx, line in entries)

    return intro + "\n" + "\n".join(section_lines) + "\n\nUse real images, NOT emoji/text placeholders."


def _resource_manifest(resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    manifest: List[Dict[str, Any]] = []
    for idx, resource in enumerate(resources):
        manifest.append(
            {
                "index": idx,
                "path": _asset_path(resource, idx),
                "size": _size_label(resource),
                "source_pixel_size": _source_pixel_size_label(resource),
                "source_screenshot_size": _source_screenshot_size_label(resource),
                "render_bbox": _render_bbox_label(resource),
                "original_category": resource.get("original_category", ""),
                "source_step_key": resource.get("source_step_key", ""),
                "prompt_visibility": resource.get("prompt_visibility", ""),
            }
        )
    return manifest


def _build_labeling_prompt(resources: List[Dict[str, Any]], page_context: Dict[str, Any]) -> str:
    return (
        "You are labeling image resources before HTML page generation.\n"
        "Return concise semantic labels for exactly the listed resources. Do not invent new resources. Use the listed index values.\n\n"
        "Use semantic_label to name the visible business/UI meaning of the resource.\n"
        "Use asset_kind for the visible asset type: cover_image, banner, avatar, logo, icon, symbol_icon, badge, map_marker, decorative_image, or unknown.\n"
        "  - symbol_icon: ONLY for simple monochrome/black-white UI control icons (back arrow, close X, search magnifier, chevron, share icon, hamburger menu). These are replaceable with CSS/SVG.\n"
        "  - icon: colorful or branded icons that carry business meaning (app category entries like food delivery/hotel/movie icons, service type icons, feature icons). These MUST be kept as bitmap assets.\n"
        "  - decorative_image: ONLY for pure decorative backgrounds, dividers, or gradient overlays with no informational content. Do NOT use for product photos, promotional images, content card images, or any image inside a content section — those are cover_image or banner.\n"
        "Use sprite_group for the rendering resource group: icon for avatar/profile images, logos, badges, symbolic icons, chrome controls, and map markers; content_visual for cover images, banners, product/content photos, and large feed visuals; asset for uncertain or mixed resources. Do not classify by size alone.\n"
        "Use visual_role for the UI role, such as cover_image, banner, avatar, merchant_logo, icon, symbol_icon, map_marker, badge, or decorative_image.\n"
        "Set text_only=true when the crop is only rendered text, a section/card title, price, rating, status, coupon/tag text, button label, or statistic. Text-only crops must not be treated as reusable bitmap media even when the extractor calls them banner.\n"
        "Important: original_category is an extractor/source hint, not the final semantic type. Avatars/profile pictures must use asset_kind=avatar and sprite_group=icon. Do not classify avatars as cover_image, banner, product_photo, or content_visual.\n\n"
        "Page context:\n"
        f"{json.dumps(page_context or {}, ensure_ascii=False, indent=2)}\n\n"
        "Resources:\n"
        f"{json.dumps(_resource_manifest(resources), ensure_ascii=False, indent=2)}\n\n"
        "Attached images after this text, when present, are resource sprite sheets only. "
        "Use sprite sheets to inspect each numbered resource. Do not infer page section, slot, nearby text, entity binding, or interaction semantics.\n\n"
        "Output JSON only with this schema:\n"
        '{"resources":[{"index":0,"semantic_label":"short label","asset_kind":"cover_image","sprite_group":"content_visual","visual_role":"cover_image","text_only":false}]}'
    )


def _build_labeling_content(
    resources: List[Dict[str, Any]],
    *,
    sprite_paths: List[str],
    page_context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [
        {"type": "input_text", "text": _build_labeling_prompt(resources, page_context)}
    ]
    for path in sprite_paths:
        if path and os.path.exists(path):
            content.append({"type": "input_image", "image_url": encode_image(path)})
    return content


async def _call_labeling_api(
    resources: List[Dict[str, Any]],
    *,
    sprite_paths: List[str],
    page_context: Dict[str, Any],
    model: str | None,
    reasoning_effort: str,
) -> tuple[Dict[int, Dict[str, str]], Dict[str, Any]]:
    content = _build_labeling_content(
        resources,
        sprite_paths=sprite_paths,
        page_context=page_context,
    )
    payload, usage = await call_openai_api_json_async(
        [{"role": "user", "content": content}],
        max_tokens=4096,
        reasoning_effort=reasoning_effort,
        model=model,
        stage="pagegen_resource_semantic_labeling",
    )
    return _normalize_annotations(payload), usage or {}


def _normalize_annotations(payload: Any) -> Dict[int, Dict[str, Any]]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("resources")
    if not isinstance(rows, list):
        return {}
    normalized: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
        except Exception:
            continue
        annotation: Dict[str, Any] = {}
        for field in _SEMANTIC_FIELDS:
            value = row.get(field)
            if value is not None:
                if field == "text_only":
                    annotation[field] = bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"true", "yes", "1"}
                else:
                    annotation[field] = str(value).strip()
        annotation.setdefault("text_only", False)
        if annotation:
            _normalize_annotation_contract(annotation)
            normalized[index] = annotation
    return normalized


def _normalize_annotation_contract(annotation: Dict[str, str]) -> None:
    asset_kind = str(annotation.get("asset_kind") or "").strip().lower()
    visual_role = str(annotation.get("visual_role") or "").strip().lower()
    if asset_kind == "avatar" or visual_role == "avatar":
        annotation["sprite_group"] = "icon"


def _has_complete_pagegen_annotation(resource: Dict[str, Any]) -> bool:
    annotation = resource.get("pagegen_semantic_annotation")
    if not isinstance(annotation, dict):
        return False
    if not str(annotation.get("semantic_label") or "").strip():
        return False
    return any(str(annotation.get(field) or "").strip() for field in _CORE_SEMANTIC_FIELDS)


async def label_resources_for_pagegen(
    resources: List[Dict[str, Any]],
    *,
    sprite_paths: List[str] | None = None,
    page_context: Dict[str, Any] | None = None,
    enabled: bool = False,
    model: str | None = None,
    reasoning_effort: str = "low",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    resource_count = len(resources or [])
    if not enabled or not resources:
        return list(resources or []), {
            "enabled": bool(enabled),
            "resource_count": resource_count,
            "annotated_count": 0,
            "skipped_reason": "disabled" if not enabled else "empty_resources",
        }


    valid_sprite_paths = [
        path for path in (sprite_paths or [])
        if path and os.path.exists(path)
    ]
    labeled_resources = [dict(item) for item in resources]

    try:
        annotations, usage = await _call_labeling_api(
            labeled_resources,
            sprite_paths=valid_sprite_paths,
            page_context=page_context or {},
            model=model,
            reasoning_effort=reasoning_effort,
        )
        usage = dict(usage or {})
        usage["labeling_granularity"] = "page_wise"
        usage["labeling_call_records"] = [
            {
                "call_id": f"{str((page_context or {}).get('filename') or 'page')}::resource_page::0",
                "page_id": str((page_context or {}).get("filename") or ""),
                "source_screenshot_refs": [],
                "resource_refs": [
                    str(resource.get("resource_ref") or resource.get("asset_name") or resource.get("path") or idx)
                    for idx, resource in enumerate(labeled_resources)
                ],
                "model": model or "",
                "reasoning_effort": reasoning_effort,
                "output_artifact": "",
            }
        ]
    except Exception as exc:
        return list(resources), {
            "enabled": True,
            "resource_count": resource_count,
            "annotated_count": 0,
            "skipped_reason": f"api_error:{type(exc).__name__}",
        }

    for index in range(resource_count):
        annotation = annotations.get(index)
        if annotation:
            labeled_resources[index]["pagegen_semantic_annotation"] = annotation

    final = [_apply_prompt_resource_policy(item) for item in labeled_resources]
    return final, {
        "enabled": True,
        "resource_count": resource_count,
        "annotated_count": sum(
            1 for item in final if item.get("pagegen_semantic_annotation")
        ),
        "screenshot_count": 0,
        "screenshot_group_count": 0,
        "sprite_count": len(valid_sprite_paths),
        "labeling_granularity": "page_wise",
        "usage": usage or {},
        "labeling_call_records": (usage or {}).get("labeling_call_records") or [],
    }
