
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _normalize_step_key(key: str) -> str:
    text = str(key or "").strip()
    match = re.match(r"^(.+?):(?:step)?(\d+)$", text)
    if match:
        return f"{match.group(1)}:step{match.group(2)}"
    return text


def _simple_token(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9一-鿿()（）]+", "_", text).strip("_") or fallback


def _state_delta_signature(scope: str, mutation: str, value: str) -> str:
    s = _simple_token(scope, "state")
    m = _simple_token(mutation, "observed")
    v = _simple_token(value, "value")
    return f"state_delta|{s}|{m}|{v}"


def _is_trivial_observation(mutation: str, lifecycle: str) -> bool:
    return (mutation in ("", "none") and lifecycle in ("", "active", "loaded"))


_SCROLL_MUTATIONS = frozenset({
    "scroll_update", "scroll_down", "scroll_up", "scroll_left", "scroll_right",
    "scrolled_to", "scroll_position_changed", "scroll_within_h", "scroll_reveal",
    "horizontal_page_changed_by_swipe_left", "horizontal_page_changed_by_swipe_right",
    "revealed_by_scroll", "revealed_by_horizontal_swipe",
    "swipe_left_to_align_target", "swipe_left_applied", "swipe_right_applied",
    "fine_scroll_adjustment_complete", "wheel_scrolled",
})

_STATIC_MUTATIONS = frozenset({
    "none", "none_observed", "content_present", "load", "loaded",
    "expose", "presented", "expandable_hidden",
})
_INITIAL_READ_MUTATIONS = frozenset({
    "read", "view", "available", "retained", "revealed", "display",
    "entry_point", "page_loaded", "view_home", "view_detail",
    "form_load", "seat_map_load",
})
_INITIAL_LIFECYCLE_TOKENS = (
    "initial", "precondition", "rendering", "rendered",
    "static", "loaded", "visible", "persisted",
)

_SCROLL_SCOPE_PATTERNS = (
    "scroll_position", "viewport", "carousel_scroll",
)

_EPHEMERAL_MUTATIONS = frozenset({
    "range_adjust", "text_selection", "cursor_move",
    "focus", "blur", "hover",
})

_IMPLICIT_ROUTE_SCOPES = (
    "bottom_tabbar", "bottom_tab_bar", "tabbar_active",
    "shell_navigation", "active_tab",
)


def _classify_coverage(
    mutation: str, scope: str, alignment_status: str,
) -> tuple[str, str]:
    mutation_lower = mutation.lower()
    scope_lower = scope.lower()

    if mutation_lower in _SCROLL_MUTATIONS:
        return "excluded_scroll_viewport", f"mutation '{mutation}' is scroll/viewport change"
    if any(p in scope_lower for p in _SCROLL_SCOPE_PATTERNS):
        return "excluded_scroll_viewport", f"scope '{scope}' is scroll/viewport position"

    lifecycle_lower = ""
    if "::" in alignment_status:
        alignment_status, lifecycle_lower = alignment_status.split("::", 1)

    if (
        mutation_lower in _INITIAL_READ_MUTATIONS
        and any(token in lifecycle_lower for token in _INITIAL_LIFECYCLE_TOKENS)
    ):
        return (
            "excluded_initial_state_observation",
            f"mutation '{mutation}' with lifecycle '{lifecycle_lower}' is initial/read-only state",
        )

    if mutation_lower in _STATIC_MUTATIONS:
        return "excluded_static_observation", f"mutation '{mutation}' is static/initial observation"

    if mutation_lower in _EPHEMERAL_MUTATIONS or "text_selection" in scope_lower:
        return "excluded_ephemeral_ui_state", f"mutation '{mutation}' / scope '{scope}' is ephemeral UI state"

    if any(p in scope_lower for p in _IMPLICIT_ROUTE_SCOPES):
        return "excluded_implicit_route_state", f"scope '{scope}' is implicitly covered by route navigation"

    if alignment_status == "scroll_or_navigation":
        return "excluded_scroll_viewport", f"action alignment is scroll_or_navigation"

    if alignment_status in ("initial_frame", ""):
        if mutation_lower in _STATIC_MUTATIONS:
            return "excluded_static_observation", "initial frame with static mutation"

    return "requires_spec_contract", ""


def _next_step_key(step_key: str) -> str:
    match = re.match(r"^(.+?):step(\d+)$", step_key)
    if match:
        return f"{match.group(1)}:step{int(match.group(2)) + 1}"
    return ""


def _build_effect_action_index(
    alignments_by_step: Mapping[str, Any],
) -> Dict[str, Dict[str, str]]:
    index: Dict[str, Dict[str, str]] = {}
    for raw_step_key, raw_alignment in alignments_by_step.items():
        step_key = _normalize_step_key(_text(raw_step_key))
        alignment = raw_alignment if isinstance(raw_alignment, Mapping) else {}
        if not step_key or not alignment:
            continue
        status = _text(alignment.get("alignment_status"))
        effect_obs_key = _normalize_step_key(_text(alignment.get("effect_obs_key")))
        if effect_obs_key:
            index[effect_obs_key] = {
                "action_id": step_key,
                "resolution": "alignment_effect_obs_key",
            }
            continue
        if status == "aligned_state_effect":
            legacy_effect_key = _next_step_key(step_key)
            if legacy_effect_key:
                index.setdefault(legacy_effect_key, {
                    "action_id": step_key,
                    "resolution": "alignment_adjacent_effect_obs_key",
                })
    return index


def compile_state_delta_signatures(
    runtime_state_observations: List[Dict[str, Any]],
    alignment: Optional[Mapping[str, Any]] = None,
    *,
    focus_trace_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:

    alignments_by_step = (
        alignment.get("alignments_by_step", {})
        if isinstance(alignment, dict) else {}
    )
    effect_action_index = _build_effect_action_index(alignments_by_step)

    by_step: Dict[str, List[Dict[str, Any]]] = {}
    total_count = 0

    for obs in runtime_state_observations:
        if not isinstance(obs, dict):
            continue
        trace_id = _text(obs.get("trace_id"))
        if focus_trace_ids and trace_id not in focus_trace_ids:
            continue

        obs_key = _normalize_step_key(_text(obs.get("obs_key")))
        if not obs_key:
            continue

        mutation = _text(obs.get("mutation"))
        lifecycle = _text(obs.get("lifecycle"))
        scope = _text(obs.get("state_scope") or obs.get("scope"))
        if _is_trivial_observation(mutation, lifecycle):
            continue

        values = _as_list(obs.get("value_hints"))
        primary_value = _text(values[0]) if values else ""
        signature = _state_delta_signature(scope, mutation, primary_value)

        resolved_action = effect_action_index.get(obs_key, {})
        action_step = _text(resolved_action.get("action_id")) or obs_key
        action_resolution = _text(resolved_action.get("resolution")) or "obs_key_action_id"
        action_alignment = alignments_by_step.get(action_step, {})

        alignment_status = _text(action_alignment.get("alignment_status"))
        coverage_class, exclusion_reason = _classify_coverage(
            mutation,
            scope,
            f"{alignment_status}::{lifecycle}",
        )

        entry: Dict[str, Any] = {
            "signature": signature,
            "state_scope": scope,
            "mutation": mutation,
            "lifecycle": lifecycle,
            "value_hints": values,
            "observation_id": _text(obs.get("observation_id")),
            "effect_obs_key": obs_key,
            "action_id": action_step,
            "action_id_resolution": action_resolution,
            "action_alignment_status": alignment_status,
            "coverage_class": coverage_class,
        }
        if exclusion_reason:
            entry["exclusion_reason"] = exclusion_reason

        if alignment_status == "aligned_state_effect" and coverage_class == "requires_spec_contract":
            entry["required_reducer"] = f"{scope}_{mutation}"

        by_step.setdefault(obs_key, []).append(entry)
        total_count += 1

    from collections import Counter
    class_counts = Counter(
        s.get("coverage_class", "unknown")
        for sigs in by_step.values() for s in sigs
    )
    requires_spec = class_counts.get("requires_spec_contract", 0)
    excluded_scroll = class_counts.get("excluded_scroll_viewport", 0)
    excluded_static = class_counts.get("excluded_static_observation", 0)
    excluded_initial = class_counts.get("excluded_initial_state_observation", 0)
    excluded_ephemeral = class_counts.get("excluded_ephemeral_ui_state", 0)
    excluded_implicit_route = class_counts.get("excluded_implicit_route_state", 0)

    return {
        "format": "trace_state_delta_signatures",
        "schema_revision": 3,
        "state_delta_signatures_by_step": by_step,
        "summary": {
            "total_signatures": total_count,
            "requires_spec_contract": requires_spec,
            "excluded_scroll_viewport": excluded_scroll,
            "excluded_static_observation": excluded_static,
            "excluded_initial_state_observation": excluded_initial,
            "excluded_ephemeral_ui_state": excluded_ephemeral,
            "excluded_implicit_route_state": excluded_implicit_route,
            "step_count": len(by_step),
        },
    }
