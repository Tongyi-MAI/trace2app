
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping

from trace2world_pipeline.app_spec.environment_profile import normalize_environment_profile


def build_environment_render_policy(profile: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = normalize_environment_profile(profile)
    mobile = normalized["platform"] == "mobile"
    return {
        "format": "environment_render_policy",
        "schema_revision": 1,
        "policy_revision": "environment_render_policy_v1",
        "environment_profile_hash": normalized["profile_hash"],
        "platform": normalized["platform"],
        "viewport": deepcopy(normalized["viewport"]),
        "browser_viewport": deepcopy(normalized["browser_viewport"]),
        "shell_mode": normalized["shell_mode"],
        "inject_phone_frame": mobile,
        "scope_viewport_fixed_to_app_surface": mobile,
        "inject_mobile_drag_runtime": mobile,
        "allow_viewport_fixed_or_sticky": not mobile,
        "interaction_mode": normalized["input_mode"],
    }
