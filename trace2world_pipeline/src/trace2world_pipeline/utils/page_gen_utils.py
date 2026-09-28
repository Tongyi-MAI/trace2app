
import hashlib
import json as _json
import os
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from trace2world_pipeline.app_spec.viewport_defaults import MOBILE_VIEWPORT_HEIGHT, MOBILE_VIEWPORT_WIDTH

RESOURCE_DEDUP_VERSION = "resource_dedup_v2"
PHASH_THRESHOLD = 10


def get_resource_dedup_metadata(input_resource_count: int, kept_resource_count: int) -> dict:
    try:
        import imagehash
        imagehash_version = getattr(imagehash, "__version__", "unknown")
    except ImportError:
        imagehash_version = None
    return {
        "dedup_version": RESOURCE_DEDUP_VERSION,
        "sha256_enabled": True,
        "phash_enabled": imagehash_version is not None,
        "phash_threshold": PHASH_THRESHOLD,
        "imagehash_version": imagehash_version,
        "input_resource_count": int(input_resource_count),
        "kept_resource_count": int(kept_resource_count),
    }


def dedup_resources(resources: list[dict], require_phash: bool = True) -> list[dict]:

    hash_map: dict[str, dict] = {}

    for r in resources:
        path = r.get("path", "")
        if not os.path.exists(path):
            continue

        with open(path, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()

        if file_hash not in hash_map or r.get("confidence", 0) > hash_map[file_hash].get("confidence", 0):
            hash_map[file_hash] = r


    seen_hashes: set[str] = set()
    result = []
    for r in resources:
        path = r.get("path", "")
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
        if file_hash not in seen_hashes and hash_map.get(file_hash) is r:
            seen_hashes.add(file_hash)
            result.append(r)


    if len(result) < 2:
        return result

    try:
        import imagehash
    except ImportError:
        if require_phash:
            raise RuntimeError(
                "imagehash is required for perceptual resource deduplication; "
                "install ImageHash or call dedup_resources(..., require_phash=False) "
                "for an explicit SHA256-only diagnostic fallback."
            )
        import sys
        print("WARNING: imagehash not available; skipping pHash dedup pass", file=sys.stderr)
        return result


    phashes = []
    for r in result:
        try:
            h = imagehash.phash(Image.open(r["path"]))
            phashes.append(h)
        except Exception:
            phashes.append(None)


    kept_indices: list[int] = []
    for i, r in enumerate(result):
        if phashes[i] is None:

            kept_indices.append(i)
            continue
        dominated = False
        for j in kept_indices:
            if phashes[j] is None:
                continue
            dist = phashes[i] - phashes[j]
            if dist <= PHASH_THRESHOLD:


                if r.get("confidence", 0) > result[j].get("confidence", 0):

                    kept_indices[kept_indices.index(j)] = i
                dominated = True
                break
        if not dominated:
            kept_indices.append(i)


    kept_set = set(kept_indices)
    return [r for idx, r in enumerate(result) if idx in kept_set]


ICON_CELL_SIZE = 160
ICON_COLS = 5
PHOTO_CELL_SIZE = 320
PHOTO_COLS = 3
ASSET_CELL_SIZE = 260
ASSET_COLS = 4
LABEL_HEIGHT = 42
LABEL_INDEX_FONT_SIZE = 30


def _get_font(size: int):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except (OSError, IOError):
        try:
            return ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", size)
        except (OSError, IOError):
            try:
                return ImageFont.load_default(size=size)
            except TypeError:
                return ImageFont.load_default()


def build_sprite_sheet(
    resources: list[dict],
    output_dir: str,
) -> dict[str, Any]:
    os.makedirs(output_dir, exist_ok=True)
    _retire_existing_sprite_outputs(output_dir)

    if not resources:
        return {"icon_sprite_path": None, "photo_sprite_path": None, "asset_sprite_path": None, "index_map": {}}

    icons: list[tuple[int, dict]] = []
    photos: list[tuple[int, dict]] = []
    assets: list[tuple[int, dict]] = []
    index_map: dict[int, dict] = {}

    for i, r in enumerate(resources):
        if _resource_prompt_omission_reason(r):
            continue
        filename = os.path.basename(r["path"])
        original_size = _resource_original_size(r)
        sprite_kind = _resource_sprite_kind(r)
        index_map[i] = {
            "filename": filename,
            "size_class": r.get("size_class", "unknown"),
            "original_size": original_size,
            "render_size": _resource_render_size(r),
            "sprite_kind": sprite_kind,
        }
        if sprite_kind == "icon":
            icons.append((i, r))
        elif sprite_kind == "photo":
            photos.append((i, r))
        else:
            assets.append((i, r))

    icon_sprite_path = None
    photo_sprite_path = None
    asset_sprite_path = None

    if icons:
        icon_sprite_path = _build_single_sprite(
            icons, output_dir, "icon_sprite.jpg",
            cell_size=ICON_CELL_SIZE, cols=ICON_COLS,
        )

    if photos:
        photo_sprite_path = _build_single_sprite(
            photos, output_dir, "photo_sprite.jpg",
            cell_size=PHOTO_CELL_SIZE, cols=PHOTO_COLS,
        )

    if assets:
        asset_sprite_path = _build_single_sprite(
            assets, output_dir, "asset_sprite.jpg",
            cell_size=ASSET_CELL_SIZE, cols=ASSET_COLS,
        )

    return {
        "icon_sprite_path": icon_sprite_path,
        "photo_sprite_path": photo_sprite_path,
        "asset_sprite_path": asset_sprite_path,
        "index_map": index_map,
    }


def _resource_prompt_omission_reason(resource: dict) -> str:
    try:
        from trace2world_pipeline.page_realizer.resource_semantics import pagegen_resource_prompt_omission_reason
    except Exception:
        return ""
    try:
        return pagegen_resource_prompt_omission_reason(resource)
    except Exception:
        return ""


def _retire_existing_sprite_outputs(output_dir: str) -> None:
    stale_dir = os.path.join(output_dir, "deprecated", "stale_sprites")


    for filename in ("icon_sprite.jpg", "photo_sprite.jpg"):
        path = os.path.join(output_dir, filename)
        if not os.path.isfile(path):
            continue
        os.makedirs(stale_dir, exist_ok=True)
        digest = ""
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()[:12]
        except Exception:
            digest = "unknown"
        stem, ext = os.path.splitext(filename)
        candidate = os.path.join(stale_dir, f"{stem}.{digest}{ext}")
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(stale_dir, f"{stem}.{digest}.{counter}{ext}")
            counter += 1
        os.replace(path, candidate)


def _build_single_sprite(
    indexed_resources: list[tuple[int, dict]],
    output_dir: str,
    filename: str,
    cell_size: int,
    cols: int,
) -> str:
    n = len(indexed_resources)
    rows = (n + cols - 1) // cols
    total_cell_h = cell_size + LABEL_HEIGHT

    canvas_w = cols * cell_size
    canvas_h = rows * total_cell_h
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    index_font = _get_font(LABEL_INDEX_FONT_SIZE)

    for pos, (global_idx, r) in enumerate(indexed_resources):
        col = pos % cols
        row = pos // cols
        x = col * cell_size
        y = row * total_cell_h


        try:
            img = Image.open(r["path"]).convert("RGB")
            scale = min(cell_size / img.width, cell_size / img.height)
            new_w = max(1, int(round(img.width * scale)))
            new_h = max(1, int(round(img.height * scale)))
            img = img.resize((new_w, new_h), Image.LANCZOS)
            paste_x = x + (cell_size - new_w) // 2
            paste_y = y + (cell_size - new_h) // 2
            canvas.paste(img, (paste_x, paste_y))
        except Exception:

            draw.rectangle([x, y, x + cell_size, y + cell_size], fill=(200, 200, 200))
            draw.text((x + 4, y + cell_size // 2), "ERR", fill=(255, 0, 0), font=index_font)


        index_label = f"[{global_idx}]"
        label_y = y + cell_size
        draw.rectangle([x, label_y, x + cell_size, label_y + LABEL_HEIGHT], fill=(255, 255, 255))
        draw.text((x + 4, label_y + 5), index_label, fill=(0, 0, 0), font=index_font)

    out_path = os.path.join(output_dir, filename)
    canvas.save(out_path, "JPEG", quality=90)
    return out_path


def _resource_original_size(resource: dict) -> str:
    width, height = _resource_dimensions(resource)
    if width and height:
        return f"{width}x{height}"
    return "?x?"


def _resource_render_size(resource: dict) -> str:
    width = resource.get("render_width")
    height = resource.get("render_height")
    try:
        if width and height:
            return f"{int(width)}x{int(height)}"
    except Exception:
        pass
    bbox = resource.get("render_bbox")
    if isinstance(bbox, dict):
        try:
            width = int(bbox.get("width") or 0)
            height = int(bbox.get("height") or 0)
            if width > 0 and height > 0:
                return f"{width}x{height}"
        except Exception:
            pass
    return ""


def _resource_sprite_size_label(resource: dict) -> str:
    render_size = _resource_render_size(resource)
    original_size = _resource_original_size(resource)
    if render_size and render_size != original_size:
        return f"r:{render_size} src:{original_size}"
    return original_size


def _resource_sprite_kind(resource: dict) -> str:
    annotation = resource.get("pagegen_semantic_annotation")
    if isinstance(annotation, dict):
        sprite_group = str(annotation.get("sprite_group") or "").strip().lower()
        asset_kind = str(annotation.get("asset_kind") or annotation.get("visual_role") or "").strip().lower()
        visual_role = str(annotation.get("visual_role") or "").strip().lower()
        if asset_kind in {"avatar", "logo", "symbol_icon", "badge", "marker", "map_marker", "merchant_logo", "icon"}:
            return "icon"
        if visual_role in {"avatar", "merchant_logo", "symbol_icon", "badge", "map_marker", "icon"}:
            return "icon"
        if sprite_group in {"icon", "identity_icon", "symbol_icon", "small_visual"}:
            return "icon"
        if asset_kind in {"cover_image", "content_image", "content_visual", "product_photo", "merchant_photo", "banner", "map_tile", "photo", "image"}:
            return "photo"
        if visual_role in {"cover_image", "content_image", "content_visual", "product_photo", "merchant_photo", "banner"}:
            return "photo"
        if sprite_group in {"content_visual", "photo"}:
            return "photo"
    return "asset"


def _resource_dimensions(resource: dict) -> tuple[int | None, int | None]:
    width = resource.get("width")
    height = resource.get("height")
    try:
        if width and height:
            return int(width), int(height)
    except Exception:
        pass
    path = resource.get("path", "")
    try:
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None, None


def format_resource_list(resources: list) -> str:
    lines = []
    for i, r in enumerate(resources):
        filename = os.path.basename(r["path"])
        size_class = r.get("size_class", "unknown")
        width = r.get("width", "?")
        height = r.get("height", "?")
        confidence = r.get("confidence", 0)
        lines.append(
            f"  [{i}] images/{filename} | {size_class} {width}x{height} | conf={confidence}"
        )
    return "\\n".join(lines)


def format_components(components: list) -> str:
    lines = []
    for c in components:
        bounds = c.get("bounds_pct", {})
        children = c.get("children", [])
        lines.append(
            f"  {c['component_id']}: type={c.get('type','?')} "
            f"pos={c.get('position','?')}(order={c.get('position_order','?')}) "
            f"bounds=[x:{bounds.get('x_pct',0)}% y:{bounds.get('y_pct',0)}% "
            f"w:{bounds.get('width_pct',0)}% h:{bounds.get('height_pct',0)}%] "
            f"layout={c.get('layout','?')} "
            f"children={children if children else 'none'}"
        )
    return "\n".join(lines)


def format_content(content: dict) -> str:
    lines = []
    for entity_type, items in content.items():
        if isinstance(items, list):
            lines.append(f"\n  {entity_type} ({len(items)} items):")
            for item in items[:10]:
                lines.append(f"    {_json.dumps(item, ensure_ascii=False)}")
        elif isinstance(items, dict):
            lines.append(f"\n  {entity_type}: {_json.dumps(items, ensure_ascii=False)[:300]}")
    return "\n".join(lines)


def fix_image_index_refs(html: str, resources: list) -> str:
    filename_map = {}
    for i, r in enumerate(resources):
        path = r.get("path", "")
        if path:
            filename_map[str(i)] = os.path.basename(path)

    if not filename_map:
        return html

    def _fix_ref(m):
        idx = m.group(1)
        if idx in filename_map:
            return f'src="images/{filename_map[idx]}"'
        return m.group(0)


    html = re.sub(r'src="images/(\d+)\.(?:jpg|png|jpeg|webp)"', _fix_ref, html)

    html = re.sub(r'src="images/\[(\d+)\]\.(?:jpg|png|jpeg|webp)"', _fix_ref, html)
    return html


def build_prompt(
    visual_layout_text: str,
    components: list,
    content: dict,
    structure_data: dict,
    resources: list,
    *,
    page_metadata: dict | None = None,
    sprite_index_map: dict | None = None,
) -> str:

    brand = structure_data.get("brand_identity", {})
    app_name = brand.get("name", "App")

    pm = page_metadata or {}
    page_type = pm.get("page_type", "page")
    page_description = pm.get("description", "")


    comp_text = format_components(components)
    content_text = format_content(content)


    resource_section = format_resource_list(resources)


    sprite_ref = ""
    if sprite_index_map:
        sprite_ref = """
## Visual Resource Reference

The attached images include:
- Image 1: Reference screenshot (the target design to replicate)
- Image 2: Icon sprite sheet -- small icons/UI elements, each cell labeled [index] matching the resource list
- Image 3: Photo sprite sheet -- larger photos/promotional images, each cell labeled [index]

Use these sprite sheets to see what each resource actually looks like. Semantic labels are intentionally unavailable; infer resource usage only from the sprite visuals, screenshot context, and resource indices below."""


    ds_json = _json.dumps(
        structure_data.get("design_system", {}),
        ensure_ascii=False, indent=2,
    )

    return f"""You are a senior frontend developer. Generate a complete, production-quality HTML page that faithfully reproduces the {app_name} {page_type} shown in the reference screenshot.

## Target

{page_description or f'{app_name} {page_type}'}

## Design Reference

The attached screenshot shows the actual {page_type} on a mobile device ({MOBILE_VIEWPORT_WIDTH}x{MOBILE_VIEWPORT_HEIGHT} viewport). Reproduce its visual design as faithfully as possible.
{sprite_ref}

## Visual Layout (ASCII Wireframe)

This wireframe describes the spatial arrangement of all elements:

```
{visual_layout_text}
```

## Component Tree

These are the semantic components identified in the page, with their positions and bounds:

{comp_text}

## Design System

```json
{ds_json}
```

## Content Data

The following entities should be rendered with real data:

{content_text}

## Resources

{resource_section}

## Requirements

1. **Single file**: Output a complete standalone HTML file with embedded `<style>` CSS. No external dependencies.
2. **Phone Frame (iPhone shell)**: Simulate an iPhone shell around the page.
   Set these styles exactly:
   - `html {{ background: #1a1a2e !important; height: 100% !important; overflow: hidden !important; }}`
   - `body {{ max-width: {MOBILE_VIEWPORT_WIDTH}px; height: {MOBILE_VIEWPORT_HEIGHT}px; margin: 20px auto; border-radius: 44px;
             box-shadow: 0 0 0 10px #1a1a2e, 0 0 0 13px #333, 0 20px 60px rgba(0,0,0,0.5);
             overflow: hidden; background: #fff; position: relative; }}`
   Use `<meta name="viewport" content="width=device-width, initial-scale=1.0">`.
3. **Faithful reproduction**: Match the screenshot's layout, colors, spacing, and typography as closely as possible.
4. **Real data**: Render all content entities with the provided data -- do NOT use placeholder text.
5. **Resource usage**: Use ONLY the listed resources above. Semantic labels are intentionally unavailable; infer usage from the screenshot plus sprite sheets, and reference images as `<img src="images/filename.jpg">`.
6. **Chinese text**: All UI text should be in the same language as shown in the wireframe and content data.
7. **Interactive elements**: Search bar, category icons, and tab bar should have appropriate HTML structure (links, buttons) even if non-functional.
8. **No JavaScript required**: Pure HTML+CSS is sufficient. Data should be rendered statically.
9. **Footer navigation layout**: If the component tree includes a navigation bar at
   position=footer, it MUST use `flex-shrink: 0; position: relative;` — do NOT use
   `position: fixed`. The app-shell (Req 11) handles vertical layout; fixed positioning
   breaks inside `overflow: hidden`.
10. **Layout precision**: Each major section must occupy approximately the viewport percentage indicated by its bounds_pct. Use explicit `min-height` or `height` values on section containers. Maintain a clear vertical flow: header area at top -> scrollable main content in the middle -> footer navigation at bottom. Do not let sections collapse or expand beyond their intended proportions. The app-shell (Req 11) handles vertical distribution automatically.
11. **App Shell Wrapper**: Wrap all page content in `<div class="app-shell">` as the
    direct first child of `<body>`. Apply these styles to `.app-shell`:
    `display: flex; flex-direction: column; height: 100%; padding-top: 36px; padding-bottom: 13px;`
    Set `header {{ flex-shrink: 0; }}`, `main {{ flex: 1; overflow-y: auto; }}`,
    `footer {{ flex-shrink: 0; position: relative; }}`.
12. **Notch + Home Indicator**: Add iOS hardware details via CSS pseudo-elements:
    - `body::before` — notch: `content: ''; display: block; position: absolute;
      top: 0; left: 50%; transform: translateX(-50%);
      width: 120px; height: 28px; background: #1a1a2e; border-radius: 0 0 18px 18px; z-index: 100;`
    - `body::after` — home indicator: `content: ''; display: block; position: absolute;
      bottom: 8px; left: 50%; transform: translateX(-50%);
      width: 134px; height: 5px; background: rgba(0,0,0,0.3); border-radius: 3px;`
13. **Text decoration**: Apply `a, a:visited {{ text-decoration: none; color: inherit; }}` globally in the `<style>` block. This prevents browser default underlines on category icon labels, tab bar items, and any other linked UI elements.

Output ONLY the complete HTML code, no explanations."""
