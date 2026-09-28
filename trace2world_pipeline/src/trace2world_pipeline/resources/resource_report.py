

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from html import escape
from typing import Any, Dict, List, Tuple


def load_trace_index(trace_dir: str, trace_id: str) -> List[Dict[str, Any]]:
    index_path = os.path.join(trace_dir, trace_id, "index.json")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("steps", [])
    trajectory_path = os.path.join(trace_dir, trace_id, "trajectory.json")
    if not os.path.exists(trajectory_path):
        return []
    with open(trajectory_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    steps = []
    for index, item in enumerate(data.get("trajectory", []) or []):
        if not isinstance(item, dict):
            continue
        step_index = item.get("step_index")
        if not isinstance(step_index, int):
            step_index = index
        screenshot = str(item.get("screenshot") or item.get("image_file") or f"screenshots/action_{step_index:04d}.jpg")
        steps.append({
            "step": step_index + 1,
            "action": item.get("prediction") or item.get("action") or "",
            "source_image": screenshot,
        })
    return steps


def load_filtered_resources(path: str) -> Tuple[List[Dict], Dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, {}
    resources = data.get("resources", [])
    meta = {k: v for k, v in data.items() if k != "resources"}
    return resources, meta


def _resource_rel_path(trace_id: str, path: str) -> str:
    suffix_match = re.search(r"(step_?\d+/images/[^/]+)$", path)
    if suffix_match:
        return f"{trace_id}/resources/{suffix_match.group(1)}"
    return path


def _resource_step_index(path: str) -> int | None:
    m = re.search(r"step(_?)(\d+)/images/", path)
    if not m:
        return None
    step_index = int(m.group(2))


    return step_index + 1 if m.group(1) else step_index


def load_all_resources(trace_dir: str, trace_ids: List[str]) -> Dict[str, Dict[int, List[Dict]]]:
    grouped: Dict[str, Dict[int, List[Dict]]] = defaultdict(lambda: defaultdict(list))

    for trace_id in trace_ids:
        e2e_path = os.path.join(trace_dir, trace_id, "e2e_results.json")
        if not os.path.isfile(e2e_path):
            continue
        with open(e2e_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        judgment_by_path = {j.get("path", ""): j for j in data.get("judgments", [])}
        for resource in data.get("resources", []):
            path = resource.get("path", "")
            step_index = _resource_step_index(path)
            if step_index is None:
                continue
            rel_path = _resource_rel_path(trace_id, path)
            judgment = judgment_by_path.get(path, {})
            metadata = resource.get("metadata", {})
            grouped[trace_id][step_index].append({
                "path": path,
                "trace": trace_id,
                "source": resource.get("source", ""),
                "original_category": judgment.get("category", resource.get("type", "")),
                "semantic_category": "",
                "width": metadata.get("width", 0),
                "height": metadata.get("height", 0),
                "confidence": judgment.get("confidence", 0),
                "reason": judgment.get("reason", ""),
                "suggested_usage": judgment.get("suggested_usage", ""),
                "has_state_indicator": judgment.get("has_state_indicator", False),
                "overlapping_component_count": judgment.get("overlapping_component_count", 0),
                "usable": judgment.get("usable"),
                "_rel_path": rel_path,
                "_exists": os.path.exists(os.path.join(trace_dir, rel_path)),
                "_kept": False,
            })
    return {trace_id: dict(by_step) for trace_id, by_step in grouped.items()}


def merge_filtered_status(
    all_resources: Dict[str, Dict[int, List[Dict]]], filtered_resources: List[Dict]
) -> Dict[str, Dict[int, List[Dict]]]:
    filtered_by_path = {r.get("path", ""): r for r in filtered_resources}
    for by_step in all_resources.values():
        for resources in by_step.values():
            for resource in resources:
                filtered = filtered_by_path.get(resource["path"])
                if not filtered:
                    continue
                resource["_kept"] = True
                resource["semantic_category"] = filtered.get("semantic_category", "")
                resource["confidence"] = filtered.get("confidence", resource.get("confidence", 0))
                resource["reason"] = filtered.get("reason", resource.get("reason", ""))
                resource["suggested_usage"] = filtered.get("suggested_usage", resource.get("suggested_usage", ""))
                resource["width"] = filtered.get("width", resource.get("width", 0))
                resource["height"] = filtered.get("height", resource.get("height", 0))
    return all_resources


def count_total_extracted(trace_dir: str, trace_id: str, step_index: int) -> int:
    candidates = [
        os.path.join(trace_dir, trace_id, "resources", f"step{step_index:02d}", "images"),
        os.path.join(trace_dir, trace_id, "resources", f"step_{step_index - 1:04d}", "images"),
    ]
    for img_dir in candidates:
        if os.path.isdir(img_dir):
            return len([f for f in os.listdir(img_dir) if not f.startswith(".")])
    return 0


def _html_rel_path(path: str, html_dir: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.relpath(path, html_dir)
    return path


def build_html(
    trace_dir: str,
    trace_ids: List[str],
    trace_steps: Dict[str, List[Dict]],
    grouped_resources: Dict[str, Dict[int, List[Dict]]],
    meta: Dict,
    output_path: str,
) -> str:
    total_resources = sum(
        len(rs) for by_step in grouped_resources.values() for rs in by_step.values()
    )
    total_kept = sum(
        sum(1 for r in rs if r.get("_kept"))
        for by_step in grouped_resources.values()
        for rs in by_step.values()
    )
    total_steps = sum(len(steps) for steps in trace_steps.values())
    html_dir = os.path.dirname(os.path.abspath(output_path))

    parts = [f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Resource Preview — {os.path.basename(os.path.abspath(trace_dir))}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f5f5f5; color: #333; padding: 20px; }}
h1 {{ font-size: 24px; margin-bottom: 8px; }}
.stats {{ color: #666; margin-bottom: 24px; font-size: 14px; }}
.stats span {{ background: #e8e8e8; padding: 2px 8px; border-radius: 4px; margin-right: 8px; }}
h2 {{ font-size: 20px; margin: 24px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #ddd; }}
.step-row {{ display: flex; gap: 16px; margin-bottom: 16px; background: #fff;
             border-radius: 8px; padding: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.screenshot {{ flex: 0 0 320px; text-align: center; }}
.screenshot img {{ max-width: 300px; max-height: 400px; border-radius: 4px;
                   border: 1px solid #ddd; object-fit: contain; background: #fafafa; }}
.screenshot .caption {{ font-size: 12px; color: #666; margin-top: 6px; }}
.screenshot .caption strong {{ color: #333; }}
.resources {{ flex: 1; min-width: 0; }}
.resources .header {{ font-size: 13px; color: #888; margin-bottom: 8px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
.resources .header .count {{ font-weight: 600; color: #333; }}
.legend span {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; margin-right:8px; }}
.legend .kept {{ background:#e8f7ee; color:#116b3a; border:1px solid #9dd7b5; }}
.legend .filtered {{ background:#fdecec; color:#9a1f1f; border:1px solid #efb1b1; }}
.grid {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.grid .thumb {{ position: relative; width: 96px; height: 116px; border-radius: 6px;
               border: 1px solid #ddd; overflow: hidden; background: #fafafa; cursor: pointer; }}
.grid .thumb img {{ width: 100%; height: 80px; object-fit: contain; background:#fff; }}
.grid .thumb:hover {{ border-color: #4a90d9; box-shadow: 0 0 0 2px rgba(74,144,217,.3); }}
.grid .thumb .status {{ position:absolute; top:4px; left:4px; padding:1px 6px; border-radius:999px; font-size:10px; font-weight:600; }}
.grid .thumb.kept {{ border-color:#9dd7b5; background:#f6fffa; }}
.grid .thumb.kept .status {{ background:#1f8f54; color:#fff; }}
.grid .thumb.filtered {{ border-color:#efb1b1; background:#fff8f8; opacity:.82; }}
.grid .thumb.filtered .status {{ background:#c53b3b; color:#fff; }}
.grid .thumb .badge {{ position: absolute; bottom: 24px; left: 0; right: 0; font-size: 9px;
                       background: rgba(0,0,0,.68); color: #fff; padding: 2px 4px;
                       white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.grid .thumb .meta {{ position:absolute; bottom:0; left:0; right:0; font-size:10px; color:#555; background:#fff; padding:3px 4px; border-top:1px solid #eee; }}
.no-resources {{ color: #bbb; font-style: italic; padding: 20px; }}
.missing {{ opacity: .4; border-color: #f88 !important; }}
.filter-meta {{ background: #fff; padding: 12px; border-radius: 8px; margin-bottom: 20px;
                box-shadow: 0 1px 3px rgba(0,0,0,.08); font-size: 13px; }}
.filter-meta dt {{ font-weight: 600; display: inline; }}
.filter-meta dd {{ display: inline; margin-right: 16px; }}
</style>
</head>
<body>
<h1>Resource Preview</h1>
<div class="stats">
  <span>{len(trace_ids)} traces</span>
  <span>{total_steps} steps</span>
  <span>{total_resources} extracted resources</span>
  <span>{total_kept} kept</span>
  <span>{total_resources - total_kept} filtered out</span>
</div>
<div class="legend">
  <span class="kept">保留</span>
  <span class="filtered">已过滤</span>
</div>
"""]

    if meta:
        parts.append('<div class="filter-meta"><dl>')
        for k, v in meta.items():
            if isinstance(v, dict):
                parts.append(f"<dt>{escape(str(k))}:</dt> <dd>{escape(json.dumps(v, ensure_ascii=False))}</dd><br>")
            else:
                parts.append(f"<dt>{escape(str(k))}:</dt> <dd>{escape(str(v))}</dd> ")
        parts.append("</dl></div>")

    for trace_id in trace_ids:
        steps = trace_steps.get(trace_id, [])
        trace_resources = grouped_resources.get(trace_id, {})
        trace_total = sum(len(rs) for rs in trace_resources.values())
        trace_kept = sum(sum(1 for r in rs if r.get("_kept")) for rs in trace_resources.values())
        parts.append(
            f'<h2>{escape(trace_id)} ({len(steps)} steps, {trace_total} extracted, {trace_kept} kept, {trace_total - trace_kept} filtered)</h2>'
        )

        for step_info in steps:
            step_num = step_info.get("step", 0)
            action = step_info.get("action", "")
            page = step_info.get("page", "")
            desc = step_info.get("description", "")
            source_img = step_info.get("source_image", "")

            screenshot_abs = os.path.join(trace_dir, trace_id, source_img) if source_img else ""
            screenshot_rel = _html_rel_path(screenshot_abs, html_dir)
            screenshot_exists = os.path.exists(screenshot_abs) if screenshot_abs else False

            step_resources = trace_resources.get(step_num, [])
            kept_count = sum(1 for r in step_resources if r.get("_kept"))
            filtered_count = len(step_resources) - kept_count

            parts.append('<div class="step-row">')

            parts.append('<div class="screenshot">')
            if screenshot_exists:
                parts.append(f'<img src="{escape(screenshot_rel)}" alt="Step {step_num}" loading="lazy">')
            else:
                parts.append(
                    f'<div style="width:300px;height:200px;background:#eee;'
                    f'display:flex;align-items:center;justify-content:center;'
                    f'border-radius:4px;color:#aaa;">No screenshot</div>'
                )
            caption = f"<strong>Step {step_num}</strong>: {escape(str(action))}"
            if page:
                caption += f" <br><em>{escape(str(page))}</em>"
            if desc and desc != page:
                caption += f" — {escape(str(desc))}"
            parts.append(f'<div class="caption">{caption}</div>')
            parts.append("</div>")

            parts.append('<div class="resources">')
            parts.append(
                f'<div class="header">'
                f'<span><span class="count">{len(step_resources)}</span> extracted</span>'
                f'<span><span class="count">{kept_count}</span> kept</span>'
                f'<span><span class="count">{filtered_count}</span> filtered</span>'
                f"</div>"
            )
            if step_resources:
                parts.append('<div class="grid">')
                for r in sorted(
                    step_resources,
                    key=lambda item: (0 if item.get("_kept") else 1, -float(item.get("confidence") or 0)),
                ):
                    rel = _html_rel_path(os.path.join(trace_dir, r.get("_rel_path", "")), html_dir)
                    exists = r.get("_exists", False)
                    kept = r.get("_kept", False)
                    status_label = "保留" if kept else "已过滤"
                    status_class = "kept" if kept else "filtered"
                    cat = r.get("semantic_category") or r.get("original_category", "") or "unknown"
                    w = r.get("width", "?")
                    h = r.get("height", "?")
                    conf = float(r.get("confidence") or 0)
                    reason = r.get("reason", "")
                    suggested = r.get("suggested_usage", "")
                    title_parts = [status_label, cat, f"{w}x{h}", f"conf={conf:.2f}"]
                    if suggested:
                        title_parts.append(f"usage={suggested}")
                    if reason:
                        title_parts.append(f"reason={reason}")
                    title = escape(" | ".join(title_parts))
                    meta_text = escape(f"{w}×{h} · {conf:.2f}")
                    badge_text = escape(str(cat))
                    cls = f"thumb {status_class}" + ("" if exists else " missing")
                    if exists:
                        parts.append(
                            f'<div class="{cls}" title="{title}">'
                            f'<span class="status">{status_label}</span>'
                            f'<img src="{escape(rel)}" loading="lazy">'
                            f'<div class="badge">{badge_text}</div>'
                            f'<div class="meta">{meta_text}</div>'
                            f"</div>"
                        )
                    else:
                        parts.append(
                            f'<div class="{cls}" title="{title} (missing)">'
                            f'<span class="status">{status_label}</span>'
                            f'<div style="height:80px;padding:8px;font-size:10px;color:#c00;display:flex;align-items:center;justify-content:center;">Missing</div>'
                            f'<div class="badge">{badge_text}</div>'
                            f'<div class="meta">{meta_text}</div>'
                            f"</div>"
                        )
                parts.append("</div>")
            else:
                parts.append('<div class="no-resources">No extracted resources for this step</div>')
            parts.append("</div>")

            parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Visualize trace resources as standalone HTML")
    parser.add_argument("--trace-dir", required=True, help="Path to mt_traces directory")
    parser.add_argument("--filtered-resources", help="Path to filtered_resources.json (default: trace-dir/filtered_resources.json)")
    parser.add_argument("--output", help="Output HTML path (default: trace-dir/resource_preview.html)")
    args = parser.parse_args()

    trace_dir = args.trace_dir
    if not os.path.isdir(trace_dir):
        print(f"Error: trace-dir does not exist: {trace_dir}", file=sys.stderr)
        sys.exit(1)

    fr_path = args.filtered_resources or os.path.join(trace_dir, "filtered_resources.json")
    if not os.path.exists(fr_path):
        print(f"Error: filtered_resources.json not found: {fr_path}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or os.path.join(trace_dir, "resource_preview.html")


    trace_ids = sorted([
        d for d in os.listdir(trace_dir)
        if os.path.isdir(os.path.join(trace_dir, d))
        and os.path.exists(os.path.join(trace_dir, d, "index.json"))
    ])
    if not trace_ids:
        print(f"Error: no traces found in {trace_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(trace_ids)} traces: {', '.join(trace_ids)}")


    trace_steps = {}
    for tid in trace_ids:
        trace_steps[tid] = load_trace_index(trace_dir, tid)
        print(f"  {tid}: {len(trace_steps[tid])} steps")

    filtered_resources, meta = load_filtered_resources(fr_path)
    print(f"Loaded {len(filtered_resources)} kept resources from filtered_resources.json")

    all_resources = load_all_resources(trace_dir, trace_ids)
    grouped = merge_filtered_status(all_resources, filtered_resources)


    html = build_html(trace_dir, trace_ids, trace_steps, grouped, meta, output_path)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Generated: {output_path} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
