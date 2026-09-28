
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _normalize_step_key(key: str) -> str:
    text = str(key or "").strip()
    match = re.match(r"^(.+?):(?:step)?(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{match.group(2)}"
    return text


def _step_key(trace_id: str, step_index: int) -> str:
    return f"{trace_id}:step{step_index}"


_SCROLL_NAV_ACTIONS = frozenset({
    "scroll", "swipe", "navigate", "go_back", "return",
})

_SCROLL_PATTERNS = re.compile(
    r"(向[上下左右]滑动|scroll|swipe|翻页|上滑|下滑)", re.IGNORECASE
)
_INITIAL_ACTION_PATTERNS = re.compile(
    r"(打开应用|启动应用|open\s+app|launch\s+app)", re.IGNORECASE
)


def _is_scroll_or_navigation(action: str) -> bool:
    action_lower = action.lower()
    if any(token in action_lower for token in _SCROLL_NAV_ACTIONS):
        return True
    if _SCROLL_PATTERNS.search(action):
        return True
    return False


def _is_initial_frame_action(action: str) -> bool:
    if not action:
        return True
    return bool(_INITIAL_ACTION_PATTERNS.search(action))


def _trace_step_files(obs_dir: Path, trace_id: str) -> List[Tuple[int, Path]]:
    files: List[Tuple[int, Path]] = []
    pattern = re.compile(rf"^{re.escape(trace_id)}_step(\d+)\.json$")
    for path in obs_dir.glob(f"{trace_id}_step*.json"):
        match = pattern.match(path.name)
        if match:
            files.append((int(match.group(1)), path))
    return sorted(files)


def _has_state_evidence(
    frame_obs: Dict[str, Any],
    runtime_observations: List[Dict[str, Any]],
    obs_key: str,
) -> bool:
    page_state = _as_dict(frame_obs.get("page_state"))
    if page_state.get("transition_from_previous") is True:
        tk = _text(page_state.get("transition_kind"))
        if tk not in ("same_page_scroll", "same_page_other"):
            return True

    for obs in runtime_observations:
        if _normalize_step_key(_text(obs.get("obs_key"))) != obs_key:
            continue
        mutation = _text(obs.get("mutation"))
        lifecycle = _text(obs.get("lifecycle"))
        if mutation not in ("", "none") or lifecycle not in ("", "active", "loaded"):
            return True
    return False


def compile_trace_action_state_alignment(
    frame_observations_dir: Path,
    trace_ids: List[str],
    runtime_state_observations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:

    obs_dir = Path(frame_observations_dir)
    runtime_obs = runtime_state_observations or []
    alignments: Dict[str, Dict[str, Any]] = {}
    issues: List[Dict[str, Any]] = []
    summary_counts: Dict[str, int] = {
        "aligned_state_effect": 0,
        "action_only_no_state_evidence": 0,
        "scroll_or_navigation": 0,
        "initial_frame": 0,
        "total_steps": 0,
    }

    for trace_id in trace_ids:
        step_files = _trace_step_files(obs_dir, trace_id)
        for position, (step_index, path_current) in enumerate(step_files):
            step_key = _step_key(trace_id, step_index)
            summary_counts["total_steps"] += 1

            try:
                with open(path_current, encoding="utf-8") as f:
                    current_obs = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            action = _text(current_obs.get("action"))
            page_state = _as_dict(current_obs.get("page_state"))
            transition_kind = _text(page_state.get("transition_kind"))

            if _is_initial_frame_action(action):
                alignments[step_key] = {
                    "step_key": step_key,
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "action": action,
                    "alignment_status": "initial_frame",
                }
                summary_counts["initial_frame"] += 1
                continue

            if _is_scroll_or_navigation(action):
                alignments[step_key] = {
                    "step_key": step_key,
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "action": action,
                    "alignment_status": "scroll_or_navigation",
                    "transition_kind": transition_kind,
                }
                summary_counts["scroll_or_navigation"] += 1
                continue

            next_step_key = ""
            next_obs: Dict[str, Any] = {}
            if position + 1 < len(step_files):
                next_step_index, path_next = step_files[position + 1]
                next_step_key = _step_key(trace_id, next_step_index)
                try:
                    with open(path_next, encoding="utf-8") as f:
                        next_obs = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            has_evidence = _has_state_evidence(
                next_obs if next_obs else current_obs,
                runtime_obs,
                next_step_key if next_obs else step_key,
            )

            if has_evidence:
                alignment_status = "aligned_state_effect"
                summary_counts["aligned_state_effect"] += 1
            else:
                alignment_status = "action_only_no_state_evidence"
                summary_counts["action_only_no_state_evidence"] += 1
                issues.append({
                    "code": "action_only_no_state_evidence",
                    "step_key": step_key,
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "action": action,
                    "message": f"action at {step_key} claims '{action[:80]}' but no observable state change in next frame",
                })

            entry: Dict[str, Any] = {
                "step_key": step_key,
                "trace_id": trace_id,
                "step_index": step_index,
                "action": action,
                "alignment_status": alignment_status,
                "transition_kind": transition_kind,
            }
            if alignment_status == "aligned_state_effect" and next_obs:
                entry["effect_obs_key"] = next_step_key
            alignments[step_key] = entry

    return {
        "format": "trace_action_state_alignment",
        "schema_revision": 1,
        "alignments_by_step": alignments,
        "issues": issues,
        "summary": summary_counts,
    }
