
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .viewport_defaults import MOBILE_VIEWPORT_HEIGHT, MOBILE_VIEWPORT_WIDTH


PROFILE_FORMAT = "environment_profile"
PROFILE_SCHEMA_REVISION = 1
SUPPORTED_PLATFORMS = ("mobile", "desktop")


def _mobile_behavior() -> Dict[str, Any]:
    viewport = {
        "width": MOBILE_VIEWPORT_WIDTH,
        "height": MOBILE_VIEWPORT_HEIGHT,
        "aspect_ratio": "195:422",
    }
    app_surface = {
        "x": 20,
        "y": 20,
        "width": MOBILE_VIEWPORT_WIDTH,
        "height": MOBILE_VIEWPORT_HEIGHT,
    }
    return {
        "format": PROFILE_FORMAT,
        "schema_revision": PROFILE_SCHEMA_REVISION,
        "platform": "mobile",
        "viewport": viewport,
        "interaction_viewport": deepcopy(viewport),
        "browser_viewport": {"width": 430, "height": 884},
        "app_surface": app_surface,
        "app_content_rect": deepcopy(app_surface),
        "device_scale_factor": 1,
        "input_mode": "touch",
        "shell_mode": "phone_frame",
        "shell": {"owner": "page", "kind": "phone_frame"},
        "pointer": "coarse",
        "fixed_position_scope": "app_surface",
        "layout_policy": {
            "density": "compact",
            "multi_column_allowed": False,
            "viewport_fixed_positioning_allowed": False,
            "minimum_target_px": 44,
        },
    }


def _desktop_behavior(width: int = 1920, height: int = 1080) -> Dict[str, Any]:
    viewport = {
        "width": int(width),
        "height": int(height),
        "aspect_ratio": f"{int(width)}:{int(height)}",
    }
    app_surface = {"x": 0, "y": 0, "width": int(width), "height": int(height)}
    return {
        "format": PROFILE_FORMAT,
        "schema_revision": PROFILE_SCHEMA_REVISION,
        "platform": "desktop",
        "viewport": viewport,
        "interaction_viewport": deepcopy(viewport),
        "browser_viewport": {"width": int(width), "height": int(height)},
        "app_surface": app_surface,
        "app_content_rect": deepcopy(app_surface),
        "device_scale_factor": 1,
        "input_mode": "mouse_keyboard",
        "shell_mode": "browser_canvas",
        "shell": {"owner": "host", "kind": "none"},
        "pointer": "fine",
        "fixed_position_scope": "browser_viewport",
        "layout_policy": {
            "density": "desktop",
            "multi_column_allowed": True,
            "viewport_fixed_positioning_allowed": True,
            "minimum_target_px": None,
        },
    }


def _behavior_payload(profile: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in profile.items()
        if key not in {"profile_hash", "resolution_source", "resolution_inputs"}
    }


def compute_profile_hash(profile: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _behavior_payload(profile), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _with_provenance(
    behavior: Mapping[str, Any],
    *,
    resolution_source: str,
    resolution_inputs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    profile = deepcopy(dict(behavior))
    profile["resolution_source"] = str(resolution_source)
    profile["resolution_inputs"] = deepcopy(dict(resolution_inputs or {}))
    profile["profile_hash"] = compute_profile_hash(profile)
    validate_environment_profile(profile)
    return profile


def parse_viewport(value: Any) -> Optional[Tuple[int, int]]:
    if value in (None, "", {}):
        return None
    if isinstance(value, Mapping):
        width, height = value.get("width"), value.get("height")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        width, height = value
    else:
        match = re.fullmatch(r"\s*(\d+)\s*[xX,]\s*(\d+)\s*", str(value))
        if not match:
            raise ValueError(f"Invalid viewport {value!r}; expected WIDTHxHEIGHT")
        width, height = match.groups()
    try:
        width_i, height_i = int(width), int(height)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid viewport {value!r}; width/height must be integers") from exc
    if width_i <= 0 or height_i <= 0:
        raise ValueError(f"Invalid viewport {value!r}; width/height must be positive")
    return width_i, height_i


def resolve_environment_profile(
    config_profile: Any = None,
    *,
    cli_platform: Optional[str] = None,
    cli_viewport: Any = None,
) -> Dict[str, Any]:
    config_mapping = dict(config_profile) if isinstance(config_profile, Mapping) else {}
    config_platform = (
        str(config_mapping.get("platform") or "").strip().lower()
        if config_mapping
        else str(config_profile or "").strip().lower()
    )
    platform = str(cli_platform or "").strip().lower() or config_platform or "mobile"
    if platform == "web":
        platform = "desktop"
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"Unsupported environment platform {platform!r}; expected one of {SUPPORTED_PLATFORMS}"
        )

    if cli_platform or cli_viewport:
        source = "cli_override"
    elif config_profile not in (None, "", {}):
        source = "config_explicit"
    else:
        source = "legacy_default_mobile"

    config_viewport = config_mapping.get("viewport") if config_mapping else None
    viewport = parse_viewport(cli_viewport) or parse_viewport(config_viewport)
    if platform == "mobile":
        canonical = (MOBILE_VIEWPORT_WIDTH, MOBILE_VIEWPORT_HEIGHT)
        if viewport and viewport != canonical:
            raise ValueError(
                "Mobile compatibility profile has a fixed canonical viewport "
                f"{canonical[0]}x{canonical[1]}; received {viewport[0]}x{viewport[1]}"
            )
        behavior = _mobile_behavior()
    else:
        width, height = viewport or (1920, 1080)
        behavior = _desktop_behavior(width, height)

    return _with_provenance(
        behavior,
        resolution_source=source,
        resolution_inputs={
            "cli_platform": cli_platform,
            "cli_viewport": cli_viewport,
            "config_profile_present": config_profile not in (None, "", {}),
        },
    )


def validate_environment_profile(profile: Mapping[str, Any]) -> None:
    if not isinstance(profile, Mapping):
        raise ValueError("environment_profile must be an object")
    platform = profile.get("platform")
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"environment_profile.platform must be one of {SUPPORTED_PLATFORMS}")
    for name in ("viewport", "browser_viewport", "app_surface"):
        value = profile.get(name)
        if not isinstance(value, Mapping):
            raise ValueError(f"environment_profile.{name} must be an object")
        for dimension in ("width", "height"):
            if int(value.get(dimension) or 0) <= 0:
                raise ValueError(f"environment_profile.{name}.{dimension} must be positive")
    if profile.get("interaction_viewport") != profile.get("viewport"):
        raise ValueError("environment_profile.interaction_viewport must match viewport")
    if profile.get("app_content_rect") != profile.get("app_surface"):
        raise ValueError("environment_profile.app_content_rect must match app_surface")
    browser = profile["browser_viewport"]
    rect = profile["app_content_rect"]
    if (
        int(rect.get("x", -1)) < 0
        or int(rect.get("y", -1)) < 0
        or int(rect["x"]) + int(rect["width"]) > int(browser["width"])
        or int(rect["y"]) + int(rect["height"]) > int(browser["height"])
    ):
        raise ValueError("environment_profile.app_content_rect must fit within browser_viewport")
    if int(profile.get("device_scale_factor") or 0) <= 0:
        raise ValueError("environment_profile.device_scale_factor must be positive")
    expected_input = "touch" if platform == "mobile" else "mouse_keyboard"
    if profile.get("input_mode") != expected_input:
        raise ValueError(
            f"environment_profile.input_mode must be {expected_input!r} for {platform}"
        )
    expected_shell = (
        {"owner": "page", "kind": "phone_frame"}
        if platform == "mobile"
        else {"owner": "host", "kind": "none"}
    )
    if profile.get("shell") != expected_shell:
        raise ValueError(f"environment_profile.shell must be {expected_shell!r} for {platform}")
    expected = compute_profile_hash(profile)
    actual = str(profile.get("profile_hash") or "")
    if actual and actual != expected:
        raise ValueError(
            "environment_profile.profile_hash does not match its behavioral payload: "
            f"expected {expected}, got {actual}"
        )


def normalize_environment_profile(profile: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = deepcopy(dict(profile))
    normalized["profile_hash"] = compute_profile_hash(normalized)
    validate_environment_profile(normalized)
    return normalized


def build_render_context(profile: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = normalize_environment_profile(profile)
    platform = normalized["platform"]
    return {
        "format": "environment_render_context",
        "schema_revision": 1,
        "authority_ref": "phase_consolidation/site_model/environment_profile.json",
        "profile_hash": normalized["profile_hash"],
        "platform": platform,
        "viewport": deepcopy(normalized["viewport"]),
        "interaction_viewport": deepcopy(normalized["interaction_viewport"]),
        "browser_viewport": deepcopy(normalized["browser_viewport"]),
        "app_surface": deepcopy(normalized["app_surface"]),
        "app_content_rect": deepcopy(normalized["app_content_rect"]),
        "device_scale_factor": normalized["device_scale_factor"],
        "input_mode": normalized["input_mode"],
        "shell_mode": normalized["shell_mode"],
        "shell": deepcopy(normalized["shell"]),
        "layout_policy": deepcopy(normalized["layout_policy"]),
        "fixed_position_scope": normalized["fixed_position_scope"],
        "prompt_policy": {
            "family": "legacy_mobile_v1" if platform == "mobile" else "desktop_web_v1",
            "phone_frame_required": platform == "mobile",
            "desktop_responsive_layout_required": platform == "desktop",
            "touch_language_required": platform == "mobile",
            "mouse_keyboard_language_required": platform == "desktop",
        },
    }


def device_context_mirror(profile: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = normalize_environment_profile(profile)
    viewport = normalized["viewport"]
    return {
        "device_class": normalized["platform"],
        "viewport_width": viewport["width"],
        "viewport_height": viewport["height"],
        "input_mode": normalized["input_mode"],
        "shell_mode": normalized["shell_mode"],
        "authority_ref": "environment_profile.json",
        "profile_hash": normalized["profile_hash"],
    }


def load_environment_profile_from_result_dir(result_dir: Any) -> Dict[str, Any]:
    root = Path(result_dir)
    candidates = (
        root / "phase_consolidation" / "site_model" / "environment_profile.json",
        root / "run_management" / "environment_profile_resolution.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid environment profile JSON: {path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"Invalid environment profile object: {path}")
        return normalize_environment_profile(payload)
    return resolve_environment_profile()
