#!/usr/bin/env python3
"""Compose the single review sprite: the complete source sequence first, then the complete execution.

Inputs are the per-trace sprite manifest written by build-obligations
(`<checklists>/traces/<trace_id>/reference_sprites/manifest.json`, which lists the marked source
screenshots) and one execution packet written by execute-trace. Output:

    <output-dir>/sprites/<trace_id>__<round>__source_then_execution.jpg
    <output-dir>/manifest.json   schema trace2world.sequence_sprite_review.v1

Rows are never paired: the two sections keep their own chronological order and lengths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

SCHEMA = "trace2world.sequence_sprite_review.v1"
SOURCE_SCHEMA = "trace2world.reference_trace_sprites.v1"
EXECUTION_SCHEMA = "trace2world.gui_execution.packet.v1"
BACKGROUND = (12, 12, 16)
INK = (255, 255, 255)
SOURCE_BAND = (70, 40, 120)
EXECUTION_BAND = (20, 90, 110)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_font(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if shutil.which("fc-list"):
        try:
            out = subprocess.run(["fc-list", ":lang=zh", "file"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        files = sorted(line.split(":", 1)[0].strip() for line in out.stdout.splitlines() if line.strip())
        return files[0] if files else None
    return None


def load_font(path: str | None, size: int) -> ImageFont.ImageFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        trial = current + char
        if draw.textlength(trial, font=font) > width and current:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def cell(path: Path, label: str, height: int, font: ImageFont.ImageFont) -> Image.Image:
    with Image.open(path) as image:
        image = image.convert("RGB")
        scale = height / image.height
        image = image.resize((max(1, int(image.width * scale)), height), Image.LANCZOS)
    band = 34
    framed = Image.new("RGB", (image.width, height + band), BACKGROUND)
    framed.paste(image, (0, band))
    ImageDraw.Draw(framed).text((6, 6), label, fill=INK, font=font)
    return framed


def section(title: str, color: tuple[int, int, int], cells: list[Image.Image], columns: int, cell_width: int,
            instruction: str, font_path: str | None) -> Image.Image:
    width = columns * cell_width
    title_font = load_font(font_path, 30)
    text_font = load_font(font_path, 24)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = wrap(probe, instruction, text_font, width - 24)
    header = 56 + 32 * len(lines)
    rows = (len(cells) + columns - 1) // columns
    cell_height = max(c.height for c in cells) if cells else 0
    canvas = Image.new("RGB", (width, header + rows * cell_height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width, header), fill=color)
    draw.text((12, 10), title, fill=INK, font=title_font)
    for index, line in enumerate(lines):
        draw.text((12, 50 + 32 * index), line, fill=INK, font=text_font)
    for position, item in enumerate(cells):
        col, row = position % columns, position // columns
        canvas.paste(item, (col * cell_width + (cell_width - item.width) // 2, header + row * cell_height))
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--execution-packet", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--columns", type=int, default=5)
    parser.add_argument("--cell-width", type=int, default=360)
    parser.add_argument("--image-height", type=int, default=720)
    parser.add_argument("--font", default=None, help="TrueType/OpenType font able to render the instruction")
    args = parser.parse_args()

    source = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    execution = json.loads(args.execution_packet.read_text(encoding="utf-8"))
    if source.get("schema") != SOURCE_SCHEMA or len(source.get("traces") or []) != 1:
        parser.error(f"--source-manifest must be one-trace {SOURCE_SCHEMA}")
    if execution.get("schema") != EXECUTION_SCHEMA:
        parser.error(f"--execution-packet must be {EXECUTION_SCHEMA}")
    if execution.get("status") == "execution_invalid":
        parser.error("execution_invalid rounds cannot be reviewed; record evidence_blocked instead")
    trace = source["traces"][0]
    trace_id = trace["trace_id"]
    if execution.get("trace_id") != trace_id:
        parser.error(f"trace mismatch: source {trace_id} vs execution {execution.get('trace_id')}")

    font_path = find_font(args.font)
    label_font = load_font(font_path, 22)
    source_cells = [
        cell(Path(step["marked_screenshot"]), f"step {step['step_index']} | {step['action']}", args.image_height, label_font)
        for step in trace["steps"]
    ]
    attempts = {i: a for i, a in enumerate(execution.get("action_attempts") or []) if isinstance(a, dict)}
    execution_cells = []
    for state in execution.get("observed_states") or []:
        attempt = attempts.get(state.get("action_attempt_index"), {})
        intent = str(attempt.get("semantic_action") or attempt.get("action") or "")
        label = f"#{state.get('ordinal')} {state.get('phase', '')} {intent}".strip()
        execution_cells.append(cell(Path(state["screenshot_path"]), label[:60], args.image_height, label_font))
    if not source_cells or not execution_cells:
        parser.error("both sequences must contain at least one screenshot")

    instruction = trace.get("instruction") or ""
    round_name = Path(str(execution.get("execution_attempt_root") or "round")).name
    top = section(f"SOURCE  {trace_id}  ({len(source_cells)} retained steps)", SOURCE_BAND, source_cells,
                  args.columns, args.cell_width, instruction, font_path)
    bottom = section(f"EXECUTION  {round_name}  status={execution.get('status')}  ({len(execution_cells)} observed states)",
                     EXECUTION_BAND, execution_cells, args.columns, args.cell_width, instruction, font_path)
    composite = Image.new("RGB", (top.width, top.height + bottom.height), BACKGROUND)
    composite.paste(top, (0, 0))
    composite.paste(bottom, (0, top.height))
    sprite_dir = args.output_dir / "sprites"
    sprite_dir.mkdir(parents=True, exist_ok=True)
    sprite_path = sprite_dir / f"{trace_id}__{round_name}__source_then_execution.jpg"
    composite.save(sprite_path, quality=90)

    manifest = {
        "schema": SCHEMA,
        "trace_id": trace_id,
        "source_manifest_path": str(args.source_manifest),
        "source_manifest_sha256": sha256(args.source_manifest),
        "execution_packet_path": str(args.execution_packet),
        "execution_packet_sha256": sha256(args.execution_packet),
        "execution_status": execution.get("status"),
        "composite_sprite_path": str(sprite_path),
        "single_physical_sprite": True,
        "layout": "source_sequence_then_execution_sequence",
        "display_order": ["source_sequence", "execution_sequence"],
        "pairwise_alignment_claimed": False,
        "source_step_count": len(source_cells),
        "execution_state_count": len(execution_cells),
        "source_sequence_complete": True,
        "execution_sequence_complete": execution.get("status") == "execution_complete",
        "instruction_font": font_path,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"composite_sprite_path": str(sprite_path), "source_steps": len(source_cells),
                      "execution_states": len(execution_cells)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
