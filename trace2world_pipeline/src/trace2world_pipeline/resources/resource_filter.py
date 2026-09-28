

import argparse
import json
import os
import re
import sys
from typing import Any

try:
    from PIL import Image
except ImportError:
    Image = None


MIN_CONTENT_SIZE = 70


CSS_EXPRESSIBLE_PATTERNS = [

    r"\barrow\s+(?:icon|indicator|button)\b", r"\bchevron\b", r"\bcaret\b",
    r"\bback button\b", r"\bforward button\b",
    r"\bclose\b(?:\s+(?:button|icon))?\b", r"\bdismiss\b",
    r"\b[xX×]\s*(?:button|icon)\b",
    r"\bscroll\s*(?:bar|indicator|arrow|button|icon)\b",
    r"\bbullet\b", r"\bseparator\b", r"\bdivider\b",
    r"\bspinner\b", r"\bloading\s*(?:spinner|indicator|icon|animation)\b",

    r"\blocation\b.*\b(?:pin|marker)\b",
    r"\bmap\s*(?:pin|marker)\b",

    r"\bdropdown\s*(?:arrow|chevron|icon|indicator)\b",
    r"\btoggle\s*(?:switch|button|control)\b",
    r"\b(?:collapse|expand)\s*(?:arrow|button|icon|control)\b",

    r"\bstar\s+(?:icon|rating|indicator|symbol)\b",
    r"\brating\s+(?:star|icon|indicator)\b",
    r"\bcheckmark\s+(?:icon|indicator|mark|symbol)\b",
    r"\btick\s+(?:icon|mark|indicator)\b",
]
_CSS_EXPRESSIBLE_RE = re.compile("|".join(CSS_EXPRESSIBLE_PATTERNS), re.IGNORECASE)


LOW_CONFIDENCE_THRESHOLD = 0.80
LOW_CONFIDENCE_MAX_SIZE = 100

OVERLAY_KEYWORDS = [
    "overlay", "icon on top", "badge", "sticker", "watermark",
]


_CLOSE_BUTTON_RE = re.compile(
    r"\bclose\s*\(?x\)?|\bclose\s+button\b|\bdismiss\s+button\b",
    re.IGNORECASE,
)


def _count_overlay_mentions(text: str) -> int:
    lower = text.lower()
    return sum(1 for kw in OVERLAY_KEYWORDS if kw in lower)


def _minimal_overlay(text: str) -> bool:
    lower = text.lower()
    return bool(re.search(r"minimal\s+\w*\s*overlay", lower) or
                re.search(r"small\s+corner", lower))


def _get_dimensions(path: str, fallback_meta: dict | None = None) -> tuple[int, int]:
    if Image is not None and os.path.isfile(path):
        try:
            with Image.open(path) as img:
                return img.size
        except Exception:
            pass

    if fallback_meta and "width" in fallback_meta and "height" in fallback_meta:
        return int(fallback_meta["width"]), int(fallback_meta["height"])
    return 0, 0


def _size_class(w: int, h: int) -> str:
    return "icon_size" if max(w, h) < 200 else "photo_size"


_SKIP_WORDS = frozenset({
    "small", "generic", "single", "can", "a", "the", "an", "large", "main",
    "or", "for", "in", "as", "be", "used", "it", "its", "this", "that",
})


def _derive_semantic_label(suggested_usage: str, original_cat: str) -> str:
    if original_cat in ("logo", "promotional", "decoration", "ui_element", "unusable"):
        return original_cat
    for word in suggested_usage.lower().split():
        word = word.rstrip(",.:;!?")
        if word not in _SKIP_WORDS and len(word) > 2 and word != original_cat:
            return f"{word}_{original_cat}"
    return original_cat


def load_judgments(input_dir: str) -> tuple[list[dict], int, int]:
    all_judgments: list[dict] = []
    total = 0
    trace_count = 0
    if not os.path.isdir(input_dir):
        return all_judgments, total, trace_count
    for entry in sorted(os.listdir(input_dir)):
        path = os.path.join(input_dir, entry, "e2e_results.json")
        if not os.path.isfile(path):
            continue
        trace_count += 1
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        judgments = data.get("judgments", [])

        resource_meta: dict[str, dict] = {}
        for r in data.get("resources", []):
            resource_meta[r["path"]] = {
                "metadata": r.get("metadata", {}),
                "source": r.get("source", ""),
            }
        for j in judgments:
            j["_trace"] = entry
            j["_resource_meta"] = resource_meta.get(j.get("path", ""), {})
        total += len(judgments)
        all_judgments.extend(judgments)
    return all_judgments, total, trace_count


def filter_resources(judgments: list[dict]) -> dict[str, Any]:


    usable: list[dict] = []
    recovered_count = 0
    recovery_rejected = 0
    for j in judgments:
        resource_meta = j.get("_resource_meta") if isinstance(j.get("_resource_meta"), dict) else {}
        metadata = resource_meta.get("metadata", {}) if isinstance(resource_meta.get("metadata"), dict) else resource_meta
        w, h = _get_dimensions(j["path"], metadata)
        j["_width"], j["_height"] = w, h
        if j.get("usable"):
            usable.append(j)
        elif max(w, h) >= 180:
            min_dim = min(w, h) if min(w, h) > 0 else 1
            aspect = max(w, h) / min_dim


            is_large_moderate = min_dim >= 500 and aspect < 2.0
            if aspect > 1.7 and not is_large_moderate:
                recovery_rejected += 1
                continue

            if h > w and h / max(w, 1) > 1.3:
                recovery_rejected += 1
                continue


            cat = j.get("category", "")
            if cat == "unusable" and max(w, h) < 300:
                recovery_rejected += 1
                continue
            recovered_count += 1
            j["_recovered"] = True
            usable.append(j)
    usable_count = len(usable)


    size_filtered = 0
    sized: list[dict] = []
    for j in usable:
        w, h = j.get("_width", 0), j.get("_height", 0)
        if w > 0 and h > 0 and max(w, h) < MIN_CONTENT_SIZE:
            size_filtered += 1
            continue
        if w > 0 and h > 0 and min(w, h) < MIN_CONTENT_SIZE:
            size_filtered += 1
            continue


        if max(w, h) < 200 and min(w, h) > 0:
            icon_aspect = max(w, h) / min(w, h)
            if icon_aspect > 1.7:
                size_filtered += 1
                continue
        sized.append(j)


    css_filtered = 0
    icon_cat_filtered = 0
    non_css: list[dict] = []

    _ICON_CATEGORIES = frozenset({
        "brand_logo", "general_icon", "service_icon",
    })
    for j in sized:
        w, h = j.get("_width", 0), j.get("_height", 0)
        cat = j.get('category', '')
        if max(w, h) < 200:
            reason_text = j.get('reason', '')
            if _CSS_EXPRESSIBLE_RE.search(reason_text):
                css_filtered += 1
                continue

            if cat in _ICON_CATEGORIES:
                icon_cat_filtered += 1
                continue
        elif j.get("_recovered") and cat in _ICON_CATEGORIES:


            icon_cat_filtered += 1
            continue
        non_css.append(j)


    conf_filtered = 0
    quality_pass: list[dict] = []
    for j in non_css:
        conf = j.get("confidence", 1.0)
        max_dim = max(j.get("_width", 0), j.get("_height", 0))
        if conf <= LOW_CONFIDENCE_THRESHOLD and 0 < max_dim < LOW_CONFIDENCE_MAX_SIZE:
            conf_filtered += 1
            continue
        quality_pass.append(j)


    overlay_filtered = 0
    passing: list[dict] = []

    for j in quality_pass:


        if j.get("has_state_indicator"):
            overlay_filtered += 1
            continue


        reason_text = j.get("reason", "")
        if _CLOSE_BUTTON_RE.search(reason_text):
            overlay_filtered += 1
            continue

        cat = j.get("category", "")
        w, h = j.get("_width", 0), j.get("_height", 0)
        overlap_count = j.get("overlapping_component_count", -1)


        conf = j.get("confidence", 1.0)
        if conf < LOW_CONFIDENCE_THRESHOLD and overlap_count >= 1 and max(w, h) < 200:
            overlay_filtered += 1
            continue


        if j.get("_recovered") and cat in ("promotional", "unusable") and max(w, h) < 200:
            passing.append(j)
            continue


        if overlap_count >= 5 and max(w, h) >= 200:
            overlay_filtered += 1
            continue


        if overlap_count >= 0 and max(w, h) < 200:
            if overlap_count > 1:
                overlay_filtered += 1
                continue
        elif overlap_count < 0:

            reason = j.get("reason", "")
            suggested = j.get("suggested_usage", "")
            combined = f"{reason} {suggested}"
            if cat == "photo":
                overlay_count = _count_overlay_mentions(combined)
                if overlay_count > 1 and not _minimal_overlay(combined):
                    overlay_filtered += 1
                    continue

        passing.append(j)


    resources: list[dict] = []
    for j in passing:
        w, h = j.get("_width", 0), j.get("_height", 0)
        resource_meta = j.get("_resource_meta") if isinstance(j.get("_resource_meta"), dict) else {}
        metadata = resource_meta.get("metadata", {}) if isinstance(resource_meta.get("metadata"), dict) else resource_meta
        if w == 0 and h == 0:
            w, h = _get_dimensions(j["path"], metadata)
        item = {
            "path": j["path"],
            "original_category": j.get("category", ""),
            "semantic_category": _derive_semantic_label(
                j.get("suggested_usage", ""), j.get("category", "")
            ),
            "width": w,
            "height": h,
            "size_class": _size_class(w, h),
            "reason": j.get("reason", ""),
            "suggested_usage": j.get("suggested_usage", ""),
            "trace": j["_trace"],
            "confidence": j.get("confidence", 0),
        }
        if resource_meta.get("source"):
            item["source"] = resource_meta.get("source")
        bbox = metadata.get("bbox") if isinstance(metadata, dict) else None
        if isinstance(bbox, dict):
            item["source_pixel_bbox"] = bbox
        nearby_text = metadata.get("nearby_text") if isinstance(metadata, dict) else None
        if nearby_text:
            item["source_nearby_text"] = str(nearby_text)[:240]
        resources.append(item)


    resources.sort(key=lambda r: r["confidence"], reverse=True)

    return {
        "total_input": len(judgments),
        "usable_input": usable_count,
        "photo_size_recovered": recovered_count,
        "constraints_applied": {
            "recovery_rejected": recovery_rejected,
            "size_filtered": size_filtered,
            "css_expressible_filtered": css_filtered,
            "icon_category_filtered": icon_cat_filtered,
            "confidence_filtered": conf_filtered,
            "overlay_filtered": overlay_filtered,
        },
        "final_count": len(resources),
        "resources": resources,
    }


def print_summary(result: dict[str, Any]) -> None:
    print("=" * 60)
    print("  Resource Filter Summary")
    print("=" * 60)
    print(f"  Total input judgments : {result['total_input']}")
    print(f"  Usable (all categories): {result['usable_input']}")
    print(f"  Photo-size recovered   : {result['photo_size_recovered']}")
    ca = result["constraints_applied"]
    print(f"  Recovery rejected      : {ca['recovery_rejected']}")
    print(f"  Size-filtered (<{MIN_CONTENT_SIZE}px): {ca['size_filtered']}")
    print(f"  CSS-expressible-filtered: {ca['css_expressible_filtered']}")
    print(f"  Icon-category-filtered : {ca['icon_category_filtered']}")
    print(f"  Confidence-filtered   : {ca['confidence_filtered']}")
    print(f"  Overlay-filtered      : {ca['overlay_filtered']}")
    print(f"  Final passing resources: {result['final_count']}")

    icon_count = sum(1 for r in result["resources"] if r["size_class"] == "icon_size")
    photo_count = sum(1 for r in result["resources"] if r["size_class"] == "photo_size")
    print(f"    icon_size: {icon_count}  |  photo_size: {photo_count}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter extracted screenshot resources with quality gates."
    )
    parser.add_argument(
        "--input-dir", required=True,
        help="Directory containing trace subdirectories with e2e_results.json",
    )
    parser.add_argument(
        "--output", default="filtered_resources.json",
        help="Path for the output JSON file",
    )
    args = parser.parse_args()

    print(f"Loading judgments from: {args.input_dir}")
    judgments, total, trace_count = load_judgments(args.input_dir)
    if not judgments:
        print("No judgments found. Exiting.")
        sys.exit(1)

    print(f"Loaded {total} judgments across {trace_count} traces. Filtering...")
    result = filter_resources(judgments)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.output}")
    print()
    print_summary(result)


if __name__ == "__main__":
    main()
