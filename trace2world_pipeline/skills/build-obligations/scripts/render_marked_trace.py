#!/usr/bin/env python3
"""Draw each recorded action onto its pre-action screenshot and tile one source trace into a sprite.

The released source traces store raw screenshots and the recorded action with its screen-pixel
coordinates (`coordinate`, or `start_coordinate`/`end_coordinate` for a drag). This script renders
the marked screenshots that the obligation checklist and the source-trace review read:

    <output-dir>/marked/step_XXXX.jpg                    one per retained step
    <output-dir>/sprites/<trace_id>_reference_sprite.jpg  chronological grid of the retained steps
    <output-dir>/manifest.json                            schema trace2world.reference_trace_sprites.v1

Only retained steps (disposition `keep`) are drawn. The mark is the recorded action on the
screen the user saw before acting; the next retained screenshot shows its effect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

SPRITE_SCHEMA = "trace2world.reference_trace_sprites.v1"
POINT_ACTIONS = {"click", "double_click", "long_press"}
MARK = (255, 64, 160)
INK = (255, 255, 255)
BAND = (24, 24, 32)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def disposition(step: dict[str, Any]) -> str:
    return str(
        (step.get("step_disposition") or {}).get("verdict")
        or (step.get("effective_step") or {}).get("verdict")
        or ("skip" if step.get("drop") else "keep")
    )


def font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def point(value: Any, width: int, height: int, warnings: list[str], label: str) -> tuple[int, int] | None:
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value)):
        return None
    x, y = int(value[0]), int(value[1])
    if not (0 <= x <= width and 0 <= y <= height):
        warnings.append(f"{label}: coordinate {value} lies outside the {width}x{height} screenshot; clamped")
        x, y = min(max(x, 0), width), min(max(y, 0), height)
    return x, y


def action_caption(step: dict[str, Any]) -> str:
    action = str(step.get("action") or "")
    if action == "type":
        return f"type ({len(str(step.get('text') or ''))} chars)"
    if action == "open":
        return "open app"
    if action == "system_button":
        return f"system_button {step.get('button') or step.get('text') or ''}".strip()
    return action or "unknown"


def draw_step(image: Image.Image, step: dict[str, Any], index: int, warnings: list[str]) -> Image.Image:
    marked = image.convert("RGB").copy()
    draw = ImageDraw.Draw(marked)
    width, height = marked.size
    radius = max(18, width // 22)
    stroke = max(4, width // 160)
    action = str(step.get("action") or "")
    label = f"step {index}"
    if action in POINT_ACTIONS:
        target = point(step.get("coordinate"), width, height, warnings, label)
        if target:
            x, y = target
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=MARK, width=stroke)
            draw.ellipse((x - stroke, y - stroke, x + stroke, y + stroke), fill=MARK)
            if action == "double_click":
                outer = radius + 2 * stroke
                draw.ellipse((x - outer, y - outer, x + outer, y + outer), outline=MARK, width=stroke)
        else:
            warnings.append(f"{label}: {action} without a coordinate")
    elif action == "drag":
        start = point(step.get("start_coordinate"), width, height, warnings, label)
        end = point(step.get("end_coordinate"), width, height, warnings, label)
        if start and end:
            draw.line((start, end), fill=MARK, width=stroke)
            draw.ellipse((start[0] - radius // 2, start[1] - radius // 2, start[0] + radius // 2, start[1] + radius // 2), outline=MARK, width=stroke)
            draw.ellipse((end[0] - stroke * 2, end[1] - stroke * 2, end[0] + stroke * 2, end[1] + stroke * 2), fill=MARK)
        else:
            warnings.append(f"{label}: drag without start/end coordinates")
    band = max(40, height // 28)
    draw.rectangle((0, 0, width, band), fill=BAND)
    draw.text((12, band // 6), f"{label} | {action_caption(step)}", fill=INK, font=font(int(band * 0.6)))
    return marked


def fit(image: Image.Image, cell_width: int, cell_height: int) -> Image.Image:
    scale = min(cell_width / image.width, cell_height / image.height)
    return image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)


def render_trace(trace_dir: Path, output_dir: Path, columns: int, cell_width: int, cell_height: int) -> dict[str, Any]:
    trajectory_path = trace_dir / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    steps = trajectory.get("trajectory") or trajectory.get("steps") or []
    warnings: list[str] = []
    marked_dir = output_dir / "marked"
    marked_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    cells: list[Image.Image] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or disposition(step) != "keep":
            continue
        source = trace_dir / str(step.get("screenshot") or "")
        if not source.is_file():
            raise FileNotFoundError(f"{trace_dir.name} step {index}: missing screenshot {source}")
        with Image.open(source) as image:
            marked = draw_step(image, step, index, warnings)
        target = marked_dir / f"step_{index:04d}.jpg"
        marked.save(target, quality=92)
        cells.append(fit(marked, cell_width, cell_height))
        rows.append(
            {
                "step_index": index,
                "action": str(step.get("action") or ""),
                "source_screenshot": str(source),
                "source_screenshot_sha256": sha256(source),
                "marked_screenshot": str(target),
            }
        )
    if not cells:
        raise ValueError(f"{trace_dir.name}: no retained steps to render")
    grid_rows = (len(cells) + columns - 1) // columns
    sprite = Image.new("RGB", (columns * cell_width, grid_rows * cell_height), (12, 12, 16))
    for position, cell in enumerate(cells):
        col, row = position % columns, position // columns
        sprite.paste(cell, (col * cell_width + (cell_width - cell.width) // 2, row * cell_height + (cell_height - cell.height) // 2))
    sprite_dir = output_dir / "sprites"
    sprite_dir.mkdir(parents=True, exist_ok=True)
    sprite_path = sprite_dir / f"{trace_dir.name}_reference_sprite.jpg"
    sprite.save(sprite_path, quality=90)
    return {
        "trace_id": trace_dir.name,
        "trajectory_path": str(trajectory_path),
        "trajectory_sha256": sha256(trajectory_path),
        "instruction": str(trajectory.get("instruction") or ""),
        "sprite_path": str(sprite_path),
        "steps": rows,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trace-root", required=True, type=Path, help="directory holding <trace_id>/trajectory.json")
    parser.add_argument("--traces", required=True, help="exactly one trace id; sprites are built per trace")
    parser.add_argument("--output-dir", required=True, type=Path, help="<checklist_dir>/traces/<trace_id>/reference_sprites")
    parser.add_argument("--columns", type=int, default=5)
    parser.add_argument("--cell-width", type=int, default=360)
    parser.add_argument("--cell-height", type=int, default=720)
    args = parser.parse_args()

    trace_ids = [item.strip() for item in args.traces.split(",") if item.strip()]
    if len(trace_ids) != 1:
        parser.error("--traces must name exactly one trace; build one sprite per trace")
    entry = render_trace(args.trace_root / trace_ids[0], args.output_dir, args.columns, args.cell_width, args.cell_height)
    manifest = {
        "schema": SPRITE_SCHEMA,
        "trace_root": str(args.trace_root),
        "columns": args.columns,
        "cell_width": args.cell_width,
        "cell_height": args.cell_height,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "traces": [entry],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sprite_path": entry["sprite_path"], "steps": len(entry["steps"]), "warnings": entry["warnings"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
