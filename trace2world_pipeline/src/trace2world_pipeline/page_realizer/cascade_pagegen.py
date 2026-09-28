import asyncio
import base64
import json
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image

from trace2world_pipeline.core.fixloop.text_patch import apply_text_patch_edits
from trace2world_pipeline.core.llm_caller import _call_generation_api_async, _call_generation_api_json_async, encode_image
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.page_realizer.pagegen_input_budget import (
    DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
    DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET,
    DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
    enforce_pagegen_preflight_budget,
    enforce_pagegen_provider_budget,
    normalize_pagegen_input_token_budget,
)
from trace2world_pipeline.utils.page_gen_utils import build_sprite_sheet, dedup_resources, get_resource_dedup_metadata
from trace2world_pipeline.page_realizer.resource_semantics import format_pagegen_resource_list, label_resources_for_pagegen


_MAX_IMAGES_PER_ROUND = 16
_FRAME_OBSERVATION_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}


@dataclass
class CascadeRuntimeConfig:
    round1_image_limit: int = 8
    max_images_per_round: int = 8
    patching_enabled: bool = True
    use_unique_steps_enabled: bool = True
    patch_max_tokens: int = 8000
    prompt_mode: str = "current_pipeline"
    adaptive_cascade_enabled: bool = True
    adaptive_cascade_min_prompt_screenshots: int = 12
    adaptive_cascade_min_resources: int = 48
    resource_semantic_labeling_enabled: bool = True
    scoped_content_enabled: bool = True
    pagegen_max_estimated_input_tokens: int = DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET
    pagegen_input_image_token_proxy: int = DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY
    pagegen_input_overhead_token_proxy: int = DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY

    def __post_init__(self) -> None:
        self.pagegen_max_estimated_input_tokens = normalize_pagegen_input_token_budget(
            self.pagegen_max_estimated_input_tokens
        )


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _step_sort_key(value: str) -> Tuple[Any, ...]:
    text = str(value)
    match = re.search(r"(.+):step(\d+)$", text)
    if match:
        return (0, match.group(1), int(match.group(2)))
    return (1, text)


def _step_key(trace_id: str, step_index: int) -> str:
    return f"{trace_id}:step{int(step_index)}"


def _parse_screenshot_ref(ref: str) -> Optional[Dict[str, Any]]:
    normalized = str(ref or "").replace("\\", "/")
    match = re.search(
        r"/([^/]+)/screenshots/(?:action|step)_0*(\d+)\.(?:jpg|jpeg|png|webp)$",
        normalized,
        re.IGNORECASE,
    )
    if match:
        trace_id = match.group(1)
        step_index = int(match.group(2))
        return {
            "step": _step_key(trace_id, step_index),
            "trace_id": trace_id,
            "step_index": step_index,
            "screenshot_path": str(ref),
        }
    match = re.search(
        r"/([^/]+)/screenshots/[^/]+-\d+-(\d+)\.(?:jpg|jpeg|png|webp)$",
        normalized,
        re.IGNORECASE,
    )
    if match:
        trace_id = match.group(1)
        step_index = max(int(match.group(2)) - 1, 0)
        return {
            "step": _step_key(trace_id, step_index),
            "trace_id": trace_id,
            "step_index": step_index,
            "screenshot_path": str(ref),
        }
    match = re.search(r"/([^/]+)/step[_-]?(\d+)[^/]*\.(?:jpg|jpeg|png|webp)$", normalized, re.IGNORECASE)
    if not match:
        return None
    trace_id = match.group(1)
    step_index = int(match.group(2))
    return {
        "step": _step_key(trace_id, step_index),
        "trace_id": trace_id,
        "step_index": step_index,
        "screenshot_path": str(ref),
    }


def _load_frame_observations(output_dir: str) -> Dict[str, Dict[str, Any]]:
    cached = _FRAME_OBSERVATION_CACHE.get(output_dir)
    if cached is not None:
        return cached
    frame_dir = PhaseOutputManager(output_dir).formal("frame_analysis", "frame_observations")
    result: Dict[str, Dict[str, Any]] = {}
    if frame_dir.exists():
        for path in sorted(frame_dir.glob("*.json")):
            try:
                obs = _read_json(path)
            except Exception:
                continue
            trace_id = obs.get("trace_id")
            step_index = obs.get("step_index")
            screenshot_path = obs.get("screenshot_path")
            if not trace_id or step_index is None or not screenshot_path:
                continue
            key = _step_key(str(trace_id), int(step_index))
            result[key] = {
                "step": key,
                "trace_id": trace_id,
                "step_index": int(step_index),
                "screenshot_path": str(screenshot_path),
                "action": obs.get("action", ""),
                "thought": obs.get("thought", ""),
                "observed_url": obs.get("observed_url", ""),
                "page_state": obs.get("page_state", {}),
            }
    _FRAME_OBSERVATION_CACHE[output_dir] = result
    return result


def _collect_visual_steps(cascade_page_input: Dict[str, Any]) -> Tuple[List[str], Dict[str, Path], Dict[str, Dict[str, Any]]]:
    refs: List[str] = []
    page_refs = (
        cascade_page_input.get("page_screenshot_refs")
        or cascade_page_input.get("resource_screenshot_refs")
        or cascade_page_input.get("prompt_screenshot_refs")
        or cascade_page_input.get("screenshot_refs")
        or []
    )
    for ref in list(page_refs):
        if ref not in refs:
            refs.append(ref)
    ordered_steps: List[str] = []
    step_to_path: Dict[str, Path] = {}
    parsed_map: Dict[str, Dict[str, Any]] = {}
    for ref in refs:
        parsed = _parse_screenshot_ref(ref)
        if not parsed:
            continue
        step = parsed["step"]
        if step in step_to_path:
            continue
        shot_path = Path(parsed["screenshot_path"])
        if not shot_path.exists():
            continue
        ordered_steps.append(step)
        step_to_path[step] = shot_path
        parsed_map[step] = parsed
    return ordered_steps, step_to_path, parsed_map


def _collect_unique_steps(output_dir: str, cascade_page_input: Dict[str, Any], fallback_steps: List[str]) -> List[str]:
    if not fallback_steps:
        return []
    fallback = set(fallback_steps)

    unique = []
    for ref in cascade_page_input.get("unique_prompt_screenshot_refs") or []:
        parsed = _parse_screenshot_ref(ref)
        if not parsed:
            continue
        key = parsed["step"]
        if key in fallback and key not in unique:
            unique.append(key)
    if unique:
        return sorted(unique, key=_step_sort_key)
    return fallback_steps[:1]


def _resource_step_keys(step: str) -> List[str]:
    match = re.match(r"(.+):step(\d+)$", str(step))
    if not match:
        return []
    trace_id, raw_step = match.group(1), int(match.group(2))
    return [
        f"{trace_id}/resources/step_{raw_step:04d}/",
        f"{trace_id}/resources/step{raw_step:02d}/",
        f"{trace_id}/resources/step{raw_step}/",
    ]


def _resource_matches_batch(resource: Dict[str, Any], batch_steps: List[str], path_keys: List[str]) -> bool:
    source_step_key = str(resource.get("source_step_key", "") or "")
    if source_step_key:
        return source_step_key in set(batch_steps)
    resource_path = str(resource.get("path", "")).replace("\\", "/")
    return any(key in resource_path for key in path_keys)


def _build_batch_resource_data(batch_steps: List[str], page_resources: List[Dict[str, Any]], round_dir: Optional[Path]) -> Dict[str, Any]:
    keys = [key for step in batch_steps for key in _resource_step_keys(step)]
    raw_selected_resources = [
        resource for resource in page_resources
        if _resource_matches_batch(resource, batch_steps, keys)
    ]
    selected_resources = dedup_resources(raw_selected_resources) if raw_selected_resources else []
    dedup_metadata = get_resource_dedup_metadata(len(raw_selected_resources), len(selected_resources))
    dedup_metadata["dedup_scope"] = "cascade_round"
    if not selected_resources:
        return {
            "resource_list_text": "",
            "page_resources": [],
            "raw_resource_count": 0,
            "round_resource_dedup": dedup_metadata,
            "sprite_paths": [],
            "icon_sprite_path": None,
            "photo_sprite_path": None,
            "asset_sprite_path": None,
        }

    sprite_paths: List[str] = []
    icon_sprite_path = None
    photo_sprite_path = None
    asset_sprite_path = None
    if round_dir is not None:
        sprite_dir = round_dir / "sprites"
        sprite_result = build_sprite_sheet(selected_resources, str(sprite_dir))
        icon_sprite_path = sprite_result.get("icon_sprite_path")
        photo_sprite_path = sprite_result.get("photo_sprite_path")
        asset_sprite_path = sprite_result.get("asset_sprite_path")
        for path in (icon_sprite_path, photo_sprite_path, asset_sprite_path):
            if path and Path(path).exists():
                sprite_paths.append(path)

    return {
        "resource_list_text": format_pagegen_resource_list(
            selected_resources,
            scope="cascade_round",
        ),
        "page_resources": selected_resources,
        "raw_resource_count": len(raw_selected_resources),
        "round_resource_dedup": dedup_metadata,
        "sprite_paths": sprite_paths,
        "icon_sprite_path": icon_sprite_path,
        "photo_sprite_path": photo_sprite_path,
        "asset_sprite_path": asset_sprite_path,
    }


async def _enrich_round_resource_semantics_async(
    resource_data: Dict[str, Any],
    *,
    runtime_config: CascadeRuntimeConfig,
    filename: str,
    page_id: str,
    round_name: str,
    round_dir: Path | str | None = None,
) -> Dict[str, Any]:
    resources = resource_data.get("page_resources") or []
    if not resources:
        return resource_data
    labeled, report = await label_resources_for_pagegen(
        resources,
        sprite_paths=resource_data.get("sprite_paths") or [],
        page_context={
            "filename": filename,
            "page_id": page_id,
            "round": round_name,
            "logic_flow": "cascade",
            "resource_scope": "cascade_round_dedup",
        },
        enabled=runtime_config.resource_semantic_labeling_enabled,
    )
    updated = dict(resource_data)
    updated["page_resources"] = labeled
    updated["resource_semantic_labeling"] = report
    sprite_paths = list(resource_data.get("sprite_paths") or [])
    if sprite_paths:
        sprite_dir = Path(sprite_paths[0]).parent
        sprite_result = build_sprite_sheet(labeled, str(sprite_dir))
        updated["icon_sprite_path"] = sprite_result.get("icon_sprite_path")
        updated["photo_sprite_path"] = sprite_result.get("photo_sprite_path")
        updated["asset_sprite_path"] = sprite_result.get("asset_sprite_path")
        updated["sprite_paths"] = [
            path for path in (
                sprite_result.get("icon_sprite_path"),
                sprite_result.get("photo_sprite_path"),
                sprite_result.get("asset_sprite_path"),
            )
            if path and Path(path).exists()
        ]
    updated["resource_list_text"] = format_pagegen_resource_list(
        labeled,
        scope="cascade_round",
        semantic_enabled=runtime_config.resource_semantic_labeling_enabled,
    )
    return updated


def _encode_resized_image(path: Path, max_width: int = 800, quality: int = 75) -> str:
    image = Image.open(path).convert("RGB")
    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, int(image.height * ratio)), Image.LANCZOS)
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip()
    return bool(re.match(r"(?:<!doctype\s+html|<html\b)", stripped, re.IGNORECASE))


def _decode_possible_json_string(text: str) -> str:
    stripped = text.strip()
    for candidate in (stripped, f'"{stripped}"'):
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, str):
            return decoded
    if "\\n" in stripped or '\\"' in stripped or "\\u" in stripped:
        try:
            return bytes(stripped, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            return stripped.replace("\\n", "\n").replace('\\"', '"')
    return stripped


def _repair_mojibake(text: str) -> str:
    if not any(marker in text for marker in ("å", "æ", "ç", "è", "é", "Â", "ï¼")):
        return text
    cjk_count = sum(1 for ch in text if "一" <= ch <= "鿿")
    if cjk_count > 20:
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text
    repaired_cjk_count = sum(1 for ch in repaired if "一" <= ch <= "鿿")
    return repaired if repaired_cjk_count > cjk_count else text


def _unescape_html_text(text: str) -> str:
    return _repair_mojibake(_decode_possible_json_string(text).strip())


def _make_debug_html(html: str, depth_to_output_root: int = 5) -> str:
    prefix = "../" * max(0, int(depth_to_output_root))

    def _replace_src(match: re.Match) -> str:
        quote = match.group(1)
        path = match.group(2)
        return f"src={quote}{prefix}{path}{quote}"

    debug_html = re.sub(r"src=(['\"])(images/[^'\"]+)\1", _replace_src, html)
    debug_html = re.sub(
        r"url\((['\"]?)(images/[^)'\"]+)(['\"]?)\)",
        lambda match: f"url({match.group(1)}{prefix}{match.group(2)}{match.group(3)})",
        debug_html,
    )
    return debug_html


def normalize_html_output(html: str) -> str:
    html = _unescape_html_text(html)
    if not _looks_like_html(html):
        match = re.search(r"<!doctype\s+html.*|<html\b.*", html, re.DOTALL | re.IGNORECASE)
        if match:
            html = _unescape_html_text(match.group(0))
    if '\\"' in html or "\\n" in html or "\\u" in html:
        html = _decode_possible_json_string(html)
    closing_tags = list(re.finditer(r"</html\s*>", html, re.IGNORECASE))
    if closing_tags:
        html = html[:closing_tags[-1].end()]
    return html.strip() + "\n"


def extract_html(reply: str) -> str:
    text = reply.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        for key in ("html", "content", "code", "page_html", "final_html", "html_content"):
            value = parsed.get(key)
            if isinstance(value, str) and (_looks_like_html(value) or "<html" in value.lower()):
                return _unescape_html_text(value)
    elif isinstance(parsed, str):
        text = parsed.strip()

    match = re.search(r"```(?:html)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return _unescape_html_text(match.group(1))
    for pattern in (r"<!doctype\s+html.*", r"<html\b.*"):
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return _unescape_html_text(match.group(0))
    return _unescape_html_text(text)


def apply_patch_edits(html: str, patch: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    return apply_text_patch_edits(html, patch, target_id="cascade_pagegen")


def _build_reference_action_section(step_actions: List[Dict[str, Any]]) -> str:
    if not step_actions:
        return ""
    records: List[Dict[str, Any]] = []
    for item in step_actions:
        if not isinstance(item, dict):
            continue
        record: Dict[str, Any] = {}
        for key in (
            "step",
            "trace_id",
            "step_index",
            "operation",
            "command",
            "input_text",
            "gesture",
            "validation_viewport",
            "expected_filename",
            "hard",
            "action",
            "thought",
            "observed_url",
            "page_state",
        ):
            value = item.get(key)
            if value not in (None, "", [], {}):
                record[key] = value
        if not any(key in record for key in ("operation", "command", "gesture", "input_text", "expected_filename")):
            continue
        records.append({k: v for k, v in record.items() if v not in (None, "", [], {})})
    if not records:
        return ""
    lines = [
        "## Reference Trace Action Operations",
        "Implement the operation semantics needed to replay these actions on this single HTML page.",
        "Priority: visual_observed > action > thought. Action text is not visible UI evidence.",
        "All available operation, action, thought, URL, and page-state evidence is included without clipping.",
        "Reference evidence:",
        json.dumps(records, ensure_ascii=False),
    ]
    return "\n".join(lines)


def _enrich_round1_prompt(prompt: str, step_actions: List[Dict[str, Any]]) -> str:
    json_contract = "## Output Format\nReturn a JSON object only. The response must be valid JSON. Use this schema: {\"html\": \"full HTML string\", \"notes\": [\"short debug note\"]}. Do not return markdown fences or prose outside JSON."
    if step_actions and not _has_page_environment_spec_prompt(prompt):
        section = _build_reference_action_section(step_actions)
        return f"{prompt.rstrip()}\n\n{json_contract}\n\n{section}\n"
    return f"{prompt.rstrip()}\n\n{json_contract}\n"


def _has_page_environment_spec_prompt(prompt: str) -> bool:
    text = str(prompt or "")
    return "## Page Environment Spec (PAGEGEN INPUT)" in text


def _build_obligations_delta_section(items: List[Dict[str, Any]] = None) -> str:
    if not items:
        return ""
    return f"\n## Missing Obligations Delta\n```json\n{json.dumps(items, ensure_ascii=False, indent=2)}\n```"


def _build_validator_findings_delta_section(items: List[Dict[str, Any]] = None) -> str:
    if not items:
        return ""
    return f"\n## Validator Findings Delta\n```json\n{json.dumps(items, ensure_ascii=False, indent=2)}\n```"


def build_cascade_delta_prompt_manifest(
    *,
    round_name: str,
    previous_html: str,
    new_prompt_screenshot_refs: List[str],
    new_pagegen_resource_projection_delta: Dict[str, Any],
    missing_obligations_delta: List[Dict[str, Any]] | None = None,
    validator_findings_delta: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:

    return {
        "format": "cascade_lossless_prompt_manifest",
        "schema_revision": 2,
        "round": round_name,
        "previous_html_chars": len(str(previous_html or "")),
        "new_prompt_screenshot_refs": list(new_prompt_screenshot_refs or []),
        "new_pagegen_resource_projection_delta": new_pagegen_resource_projection_delta or {},
        "missing_obligations_delta": list(missing_obligations_delta or []),
        "validator_findings_delta": list(validator_findings_delta or []),
        "full_stable_authority_included": True,
    }


def _build_patch_prompt(page_id: str, previous_html: str, image_manifest: List[Dict[str, Any]], resource_data: Dict[str, Any], round_name: str, *, base_prompt: str = "", missing_obligations_delta: List[Dict[str, Any]] = None, validator_findings_delta: List[Dict[str, Any]] = None, prompt_policy: Optional[Dict[str, Any]] = None) -> str:
    resource_list_text = str(resource_data.get("resource_list_text", "") or "")
    delta_manifest = build_cascade_delta_prompt_manifest(
        round_name=round_name,
        previous_html=previous_html,
        new_prompt_screenshot_refs=[
            str(item.get("path") or item.get("screenshot_path") or item.get("step_key") or "")
            for item in image_manifest
            if isinstance(item, dict)
        ],
        new_pagegen_resource_projection_delta={
            "raw_resource_count": resource_data.get("raw_resource_count", 0),
            "resource_count": len(resource_data.get("page_resources", [])),
            "round_resource_dedup": resource_data.get("round_resource_dedup", {}),
            "resource_semantic_labeling": resource_data.get("resource_semantic_labeling", {}),
        },
        missing_obligations_delta=missing_obligations_delta or [],
        validator_findings_delta=validator_findings_delta or [],
    )
    policy = dict(prompt_policy or {})
    policy_section = str(policy.pop("rendered_section", "") or "")
    policy_infix = f"\n\n{policy_section}" if policy_section else ""
    if policy:
        delta_manifest["environment_prompt_policy"] = policy
    screenshot_delta = {
        "round": round_name,
        "screenshots": image_manifest,
        "resource_delta": delta_manifest["new_pagegen_resource_projection_delta"],
    }
    return f"""You are refining an existing generated HTML page for `{page_id}`.{policy_infix}

This is {round_name}. The original PageGen authority and full previous HTML are included unchanged. The attached screenshots are this producer round's visual shard.

## Lossless Patch Contract
Use the complete original authority, full previous HTML, and this round's screenshot shard. Do NOT regenerate the page from scratch. Return JSON patch edits only.

## Patch Scope Delta
- Patch only screenshot-evidenced deltas from this round: missing visual states, resource bindings, or already-authorized components visible in this round.
- Preserve previous correct implementation outside the delta.
- Never remove, replace, clear, or overwrite a required interaction host or an ancestor subtree that contains one. Do not use `innerHTML`, `outerHTML`, `replaceChildren()`, `remove()`, or broad `footer`/`header`/`main` rewrites to implement visual variants; preserve the same visible actionable host and only update local text/class/icon when the delta is explicitly authorized.
- Do not add initial-DOM alternatives for fixed slots, query states, or runtime-only states unless the current screenshot delta shows that state.
- Resource-only patches must update image src/alt for the same entity/component, not swap entity identity.
- Output JSON only, with this schema:
  {{"edits": [{{"oldText": "unique exact old substring from Previous HTML", "newText": "replacement substring", "reason": "screenshot evidence addressed"}}], "notes": ["short debug note"]}}
- Use only exact replacement edits. Do not output append operations, markdown, or full HTML.
- Every oldText must be copied exactly from the full Previous HTML, including whitespace and newlines.
- Every oldText must be unique in the full Previous HTML. Keep oldText as small as possible.
- All edits are matched against the original Previous HTML, not after earlier edits. Do not emit overlapping or nested edits.

## Complete Original PageGen Prompt
```text
{base_prompt}
```

## Full Previous HTML
```html
{previous_html}
```

Screenshot / Resource Delta:
```json
{json.dumps(screenshot_delta, ensure_ascii=False, indent=2)}
```
Cascade Delta Prompt Manifest:
```json
{json.dumps(delta_manifest, ensure_ascii=False, indent=2)}
```
{_build_reference_action_section([item for item in image_manifest if item.get('action')])}

## Resource Delta List
{resource_list_text}
{_build_obligations_delta_section(missing_obligations_delta)}
{_build_validator_findings_delta_section(validator_findings_delta)}
"""


def _make_messages(prompt: str, screenshots: List[Path], sprite_paths: List[str]) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = []
    for path in screenshots:
        content.append({"type": "input_image", "image_url": _encode_resized_image(path)})
    for path in sprite_paths:
        content.append({"type": "input_image", "image_url": encode_image(path)})
    if sprite_paths:
        if "semantic_label=" in prompt:
            prompt += "\n\n## Visual Resource Reference\nThe final attached images after the screenshots are sprite sheets for the Resources list. Use the semantic labels in the Resources section together with the numeric labels in the sprites and screenshots."
        else:
            prompt += "\n\n## Visual Resource Reference\nThe final attached images after the screenshots are sprite sheets for the Resources list. Use the numeric labels in the sprites together with the screenshots to identify resources."
    content.append({"type": "input_text", "text": prompt})
    return [{"role": "user", "content": content}]


def _build_batches(all_steps: List[str], unique_steps: List[str], step_to_path: Dict[str, Path], round1_image_limit: int, max_images_per_round: int) -> List[List[str]]:
    if not all_steps:
        return []
    first_limit = max(1, min(round1_image_limit, max_images_per_round, _MAX_IMAGES_PER_ROUND))
    round_limit = max(1, min(max_images_per_round, _MAX_IMAGES_PER_ROUND))
    valid_all = [step for step in all_steps if step in step_to_path]
    valid_all_set = set(valid_all)
    seen: set[str] = set()
    ordered_steps: List[str] = []
    for step in unique_steps:
        if step not in valid_all_set or step in seen:
            continue
        seen.add(step)
        ordered_steps.append(step)
    for step in valid_all:
        if step in seen:
            continue
        seen.add(step)
        ordered_steps.append(step)
    first = ordered_steps[:first_limit]
    rest = ordered_steps[first_limit:]
    batches = [first] if first else []
    while rest:
        batches.append(rest[:round_limit])
        rest = rest[round_limit:]
    return batches


async def generate_page_cascade_async(
    page_design: Any,
    page_evidence: Dict[str, Any],
    cascade_page_input: Dict[str, Any],
    page_resources: List[Dict[str, Any]],
    output_dir: Optional[str],
    model: Optional[str],
    reasoning_effort: str,
    round1_prompt_builder: Callable[[Dict[str, Any]], str],
    logger: Optional[Trace2WorldLogger] = None,
    runtime_config: Optional[CascadeRuntimeConfig] = None,
    prompt_policy: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    runtime = runtime_config or CascadeRuntimeConfig()
    filename = str(cascade_page_input.get("filename") or page_evidence.get("filename") or getattr(page_design, "filename", "page.html"))
    page_id = str(cascade_page_input.get("page_id") or page_evidence.get("page_id") or filename)

    all_steps, step_to_path, parsed_map = _collect_visual_steps(cascade_page_input)
    if not all_steps:
        raise ValueError(f"Cascade pagegen requires at least one screenshot ref for {filename}")

    if runtime.use_unique_steps_enabled:
        unique_steps = _collect_unique_steps(output_dir or "", cascade_page_input, all_steps)
    else:
        unique_steps = all_steps[:1]

    frame_observations = _load_frame_observations(output_dir or "") if output_dir else {}
    step_actions = []
    for step in all_steps:
        item = dict(parsed_map.get(step, {}))
        item.update(frame_observations.get(step, {}))
        item.setdefault("step", step)
        item.setdefault("trace_id", item.get("trace_id", step.split(":step", 1)[0]))
        item.setdefault("step_index", item.get("step_index", int(step.rsplit("step", 1)[1])))
        item.setdefault("screenshot_path", str(step_to_path.get(step, item.get("screenshot_path", ""))))
        item.setdefault("action", "")
        step_actions.append(item)

    batches = _build_batches(
        all_steps,
        unique_steps,
        step_to_path,
        runtime.round1_image_limit,
        runtime.max_images_per_round,
    )
    if not batches:
        raise ValueError(f"Cascade pagegen produced no screenshot batches for {filename}")

    debug_dir: Optional[Path] = None
    previous_html = ""
    base_prompt = ""
    round_reports: List[Dict[str, Any]] = []
    round_labeled_resources: List[Dict[str, Any]] = []

    for idx, batch in enumerate(batches, start=1):
        round_name = f"round_{idx}"
        round_dir = debug_dir / round_name if debug_dir else None
        if round_dir:
            round_dir.mkdir(parents=True, exist_ok=True)
        screenshots = [step_to_path[step] for step in batch if step in step_to_path]


        resource_data = _build_batch_resource_data(all_steps, page_resources, round_dir)
        resource_data = await _enrich_round_resource_semantics_async(
            resource_data,
            runtime_config=runtime,
            filename=filename,
            page_id=page_id,
            round_name=round_name,
            round_dir=round_dir,
        )
        for resource in resource_data.get("page_resources", []) or []:
            if not isinstance(resource, dict) or not resource.get("pagegen_semantic_annotation"):
                continue
            round_labeled_resources.append({
                key: resource.get(key)
                for key in (
                    "asset_name",
                    "path",
                    "source_step_key",
                    "source_screenshot_ref",
                    "pagegen_semantic_annotation",
                )
                if resource.get(key) not in (None, "")
            })
        image_manifest = []
        for image_index, step in enumerate(batch, start=1):
            obs = next((item for item in step_actions if item.get("step") == step), {})
            image_manifest.append({
                "image_index": image_index,
                "step": step,
                "trace_id": obs.get("trace_id", ""),
                "step_index": obs.get("step_index"),
                "path": str(step_to_path.get(step, obs.get("screenshot_path", ""))),
                "unique": step in set(unique_steps),
                "operation": obs.get("operation", ""),
                "command": obs.get("command", ""),
                "gesture": obs.get("gesture", {}),
                "expected_filename": obs.get("expected_filename", ""),
            })
        if round_dir:
            _write_json(round_dir / "input_manifest.json", {
                "round": round_name,
                "screenshots": image_manifest,
                "raw_resource_count": resource_data.get("raw_resource_count", 0),
                "resource_count": len(resource_data.get("page_resources", [])),
                "round_resource_dedup": resource_data.get("round_resource_dedup", {}),
                "resource_source_steps": sorted({
                    str(resource.get("source_step_key"))
                    for resource in resource_data.get("page_resources", [])
                    if resource.get("source_step_key")
                }, key=_step_sort_key),
                "sprite_paths": resource_data.get("sprite_paths", []),
                "resource_semantic_labeling": resource_data.get("resource_semantic_labeling", {}),
            })
            _write_text(round_dir / "resource_list.txt", resource_data.get("resource_list_text", ""))

        if idx == 1:
            round_page_evidence = dict(page_evidence)
            round_page_evidence.update({
                "resource_list_text": resource_data.get("resource_list_text", ""),
                "page_resources": resource_data.get("page_resources", []),
                "icon_sprite_path": resource_data.get("icon_sprite_path"),
                "photo_sprite_path": resource_data.get("photo_sprite_path"),
                "asset_sprite_path": resource_data.get("asset_sprite_path"),
            })
            prompt = _enrich_round1_prompt(round1_prompt_builder(round_page_evidence), step_actions)
            base_prompt = prompt
            if round_dir:
                if isinstance(round_page_evidence.get("scoped_content"), dict):
                    _write_json(round_dir / "scoped_content.json", round_page_evidence["scoped_content"])
                _write_text(round_dir / "prompt.txt", prompt)
            messages = _make_messages(
                prompt,
                screenshots,
                resource_data.get("sprite_paths", []),
            )
            preflight = enforce_pagegen_preflight_budget(
                messages,
                stage=f"cascade:{filename}:{round_name}",
                input_token_budget=runtime.pagegen_max_estimated_input_tokens,
                image_token_proxy=runtime.pagegen_input_image_token_proxy,
                overhead_token_proxy=runtime.pagegen_input_overhead_token_proxy,
            )
            reply, usage = await _call_generation_api_async(
                messages,
                response_format={"type": "json_object"},
                max_tokens=24000,
                stage=f"cascade_{Path(filename).stem}_{round_name}",
                model=model,
                reasoning_effort=reasoning_effort,
            )
            provider_input_tokens = enforce_pagegen_provider_budget(
                usage,
                stage=f"cascade:{filename}:{round_name}",
                input_token_budget=runtime.pagegen_max_estimated_input_tokens,
            )
            if reply is None:
                raise RuntimeError(f"Cascade round1 API returned empty reply for {filename}")
            if round_dir:
                _write_text(round_dir / "raw_reply.txt", str(reply))
            previous_html = normalize_html_output(extract_html(reply))
            report = {
                "round": round_name,
                "images": len(screenshots),
                "resources": len(resource_data.get("page_resources", [])),
                "sprites": len(resource_data.get("sprite_paths", [])),
                "input_budget_preflight": preflight.to_dict(),
                "provider_prompt_tokens": provider_input_tokens,
                "usage": usage,
                "output_html_chars": len(previous_html),
            }
            if round_dir:
                _write_text(round_dir / "output.html", _make_debug_html(previous_html))
        else:
            if not runtime.patching_enabled:
                break
            prompt = _build_patch_prompt(
                page_id,
                previous_html,
                image_manifest,
                resource_data,
                round_name,
                base_prompt=base_prompt,
                prompt_policy=prompt_policy,
            )
            if round_dir:
                delta_manifest = build_cascade_delta_prompt_manifest(
                    round_name=round_name,
                    previous_html=previous_html,
                    new_prompt_screenshot_refs=[
                        str(item.get("path") or item.get("screenshot_path") or item.get("step_key") or "")
                        for item in image_manifest
                        if isinstance(item, dict)
                    ],
                    new_pagegen_resource_projection_delta={
                        "raw_resource_count": resource_data.get("raw_resource_count", 0),
                        "resource_count": len(resource_data.get("page_resources", [])),
                        "round_resource_dedup": resource_data.get("round_resource_dedup", {}),
                        "resource_semantic_labeling": resource_data.get("resource_semantic_labeling", {}),
                    },
                )
                if prompt_policy:
                    delta_manifest["environment_prompt_policy"] = {
                        key: value
                        for key, value in prompt_policy.items()
                        if key != "rendered_section"
                    }
                _write_json(round_dir / f"{round_name}_prompt_manifest.json", delta_manifest)
                _write_text(round_dir / "prompt.txt", prompt)
                _write_text(round_dir / "previous_input.html", _make_debug_html(previous_html))
            messages = _make_messages(
                prompt,
                screenshots,
                resource_data.get("sprite_paths", []),
            )
            preflight = enforce_pagegen_preflight_budget(
                messages,
                stage=f"cascade:{filename}:{round_name}",
                input_token_budget=runtime.pagegen_max_estimated_input_tokens,
                image_token_proxy=runtime.pagegen_input_image_token_proxy,
                overhead_token_proxy=runtime.pagegen_input_overhead_token_proxy,
            )
            patch, usage = await _call_generation_api_json_async(
                messages,
                max_tokens=runtime.patch_max_tokens,
                stage=f"cascade_{Path(filename).stem}_{round_name}",
                model=model,
                reasoning_effort=reasoning_effort,
            )
            provider_input_tokens = enforce_pagegen_provider_budget(
                usage,
                stage=f"cascade:{filename}:{round_name}",
                input_token_budget=runtime.pagegen_max_estimated_input_tokens,
            )
            if isinstance(patch, str):
                patch = json.loads(patch)
            updated_html, apply_report = apply_patch_edits(previous_html, patch)
            updated_html = normalize_html_output(updated_html)
            if round_dir:
                _write_json(round_dir / "patch.json", patch)
                _write_json(round_dir / "patch_apply_report.json", apply_report)
                _write_text(round_dir / "output.html", _make_debug_html(updated_html))
            if not apply_report.get("success"):
                if logger:
                    failed_preview = json.dumps(
                        apply_report.get("failed", []),
                        ensure_ascii=False,
                    )[:500]
                    logger.log_warning(
                        f"Cascade patch applied no edits for {filename} {round_name}; "
                        f"preserving previous HTML. failed={failed_preview}"
                    )
            input_html_chars = len(previous_html)
            previous_html = updated_html
            report = {
                "round": round_name,
                "images": len(screenshots),
                "resources": len(resource_data.get("page_resources", [])),
                "sprites": len(resource_data.get("sprite_paths", [])),
                "input_budget_preflight": preflight.to_dict(),
                "provider_prompt_tokens": provider_input_tokens,
                "usage": usage,
                "edits": len(patch.get("edits", [])) if isinstance(patch, dict) else 0,
                "applied_edits": len(apply_report.get("applied", [])),
                "failed_edits": len(apply_report.get("failed", [])),
                "patch_success": apply_report.get("success", False),
                "input_html_chars": input_html_chars,
                "output_html_chars": len(previous_html),
            }
        round_reports.append(report)
        if round_dir:
            _write_json(round_dir / "round_report.json", report)

    final_report = {
        "page_id": page_id,
        "filename": filename,
        "rounds": round_reports,
        "final_html_chars": len(previous_html),
        "batch_plan": batches,
        "step_actions": step_actions,
        "logic_flow": "cascade",
        "round1_prompt": base_prompt,
        "round_labeled_resources": round_labeled_resources,
    }
    if debug_dir:
        _write_text(debug_dir / "final.html", _make_debug_html(previous_html, depth_to_output_root=3))
        _write_json(debug_dir / "final_report.json", final_report)
    if logger:
        logger.log_info(f"Cascade pagegen complete for {filename}: {len(round_reports)} rounds")
    return previous_html, final_report
