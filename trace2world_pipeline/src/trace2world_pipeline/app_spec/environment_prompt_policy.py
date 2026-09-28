
from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Dict, Mapping

from .environment_profile import normalize_environment_profile


POLICY_FORMAT = "ghostweb_environment_prompt_policy"
POLICY_REVISION = "environment_prompt_policy_v1"
PROMPT_FAMILIES = (
    "pagegen_generation",
    "pagegen_repair",
    "trace_rewrite",
    "resource_analysis",
    "visual_calibration",
    "visual_evaluation",
)


_MOBILE_SECTIONS = {
    "pagegen_generation": "",


    "pagegen_repair": "",
    "trace_rewrite": (
        "Treat the source as a touch-operated mobile GUI. Preserve app-owned "
        "tap/swipe semantics and do not invent desktop-only mouse or keyboard actions."
    ),
    "resource_analysis": (
        "Interpret resources in the canonical 390x844 mobile app viewport; phone shell "
        "chrome is layout context, not reusable business media."
    ),
    "visual_calibration": (
        "Calibrate to a 390x844 mobile app surface with compact touch UI and a page-owned phone shell."
    ),
    "visual_evaluation": (
        "Evaluate as a 390x844 touch mobile app, including compact density, phone-shell containment, "
        "and mobile scroll behavior; do not apply desktop multi-column expectations."
    ),
}

_DESKTOP_SECTIONS = {
    "pagegen_generation": (
        "Target a 1920x1080 desktop web/application canvas with mouse and keyboard interaction, "
        "desktop information density, screenshot-evidenced multi-column layout, and contract-evidenced "
        "fixed/sticky chrome. Never add a phone frame, notch, home indicator, handset-width body cap, "
        "or touch-only minimum sizing."
    ),
    "pagegen_repair": (
        "Preserve the 1920x1080 desktop canvas and mouse/keyboard behavior while repairing the named "
        "contract issues. Do not introduce a phone frame, notch, home indicator, handset-width body cap, "
        "touch-only sizing, or mobile fixed-to-absolute containment."
    ),
    "trace_rewrite": (
        "Treat the source as a mouse-and-keyboard desktop GUI. Preserve click, keyboard, wheel-scroll, "
        "window-local dialog, and app-owned desktop navigation semantics; do not rewrite them as touch gestures."
    ),
    "resource_analysis": (
        "Interpret resources in the authoritative 1920x1080 desktop viewport. Full-canvas backgrounds and "
        "app chrome are layout evidence, not automatically reusable business media."
    ),
    "visual_calibration": (
        "Calibrate to an edge-to-edge 1920x1080 desktop application canvas with desktop density and no phone shell."
    ),
    "visual_evaluation": (
        "Evaluate as a 1920x1080 mouse-and-keyboard desktop web/application UI, including desktop density, "
        "wide layout, focus behavior, and wheel scrolling; do not apply mobile phone-shell criteria."
    ),
}


def build_environment_prompt_policy(
    profile: Mapping[str, Any],
    prompt_family: str,
) -> Dict[str, Any]:
    if prompt_family not in PROMPT_FAMILIES:
        raise ValueError(
            f"Unknown environment prompt family {prompt_family!r}; expected one of {PROMPT_FAMILIES}"
        )
    normalized = normalize_environment_profile(profile)
    sections = _MOBILE_SECTIONS if normalized["platform"] == "mobile" else _DESKTOP_SECTIONS
    section = sections[prompt_family]
    return {
        "format": POLICY_FORMAT,
        "schema_revision": 1,
        "policy_revision": POLICY_REVISION,
        "prompt_family": prompt_family,
        "platform": normalized["platform"],
        "environment_profile_hash": normalized["profile_hash"],
        "viewport": deepcopy(normalized["viewport"]),
        "rendered_section": section,
        "rendered_section_hash": hashlib.sha256(section.encode("utf-8")).hexdigest(),
        "injection_mode": (
            "embedded_legacy_literal"
            if normalized["platform"] == "mobile" and prompt_family in {"pagegen_generation", "pagegen_repair"}
            else "explicit_policy_section"
        ),
    }


def prompt_policy_manifest(profile: Mapping[str, Any], prompt_family: str) -> Dict[str, Any]:
    policy = build_environment_prompt_policy(profile, prompt_family)
    return {key: value for key, value in policy.items() if key != "rendered_section"}
