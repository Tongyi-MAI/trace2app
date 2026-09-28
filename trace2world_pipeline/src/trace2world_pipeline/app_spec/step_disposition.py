
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class StepVerdict(str, Enum):
    KEEP = "keep"
    SKIP = "skip"
    NEEDS_REASONING = "needs_reasoning"


class BridgePolicy(str, Enum):
    NONE = "none"
    ALLOWS_ROUTE_BRIDGE = "allows_route_bridge"
    BLOCKS_ROUTE = "blocks_route"


PREPROCESSING_SKIP_REASONS = frozenset({
    "phone_home",
    "system_launcher",
    "app_switcher",
    "os_permission_or_settings",
    "non_target_app",
    "loading_wait",
    "transient_launch_gate",
    "manual_takeover_noop",
    "failed_noop",
    "dropped_by_preprocess",
    "phone_keyboard_state",
    "manual_intervention_state_bridge",
})

REASONING_AUDIT_SKIP_REASONS = frozenset({
    "runtime_state_bridge",
    "unassigned_step",
    "page_assignment_audit_skip",
})

ALL_SKIP_REASONS = PREPROCESSING_SKIP_REASONS | REASONING_AUDIT_SKIP_REASONS

BRIDGE_ALLOWED_REASONS = frozenset({
    "loading_wait",
    "transient_wait",
    "noop_retry",
    "transient_launch_gate",
    "manual_intervention_state_bridge",
})

HARD_KEEP_REASONS = frozenset({
    "target_app_business_action",
    "first_task_owned_action_after_bootstrap",
    "task_critical_picker_selection",
    "task_critical_form_input",
    "task_critical_submit",
    "manual_reference_trace_fix",
})

HARD_KEEP_AUTHORITIES = frozenset({
    "preprocessing",
    "manual_reference_trace_fix",
    "reference_trace_agentic_rewrite",
})

ROUTE_BLOCKING_REASONS = frozenset({
    "phone_home",
    "system_launcher",
    "app_switcher",
    "os_permission_or_settings",
    "non_target_app",
    "external_system",
    "system_chrome",
    "unassigned_step",
})

LEGACY_SKIP_REASON_MAP: Dict[str, str] = {
    "phone_system_state": "phone_home",
    "phone_keyboard_state": "phone_keyboard_state",
    "transient_wait": "loading_wait",
    "duplicate_retry": "failed_noop",
    "grounding_retry": "failed_noop",
    "accidental_detour": "dropped_by_preprocess",
    "unsupported_old_text": "dropped_by_preprocess",
}

APP_OWNED_SURFACE_MARKERS = (
    "form", "input", "search", "filter", "picker", "modal",
    "login", "sign_in", "checkout", "order", "cart", "detail",
    "list", "feed", "tab", "home", "profile", "settings",
    "表单", "输入", "搜索", "筛选", "选择", "登录",
    "结算", "订单", "购物车", "详情", "列表", "首页",
)


@dataclass
class StepDisposition:
    trace_id: str
    step_index: int
    verdict: str = StepVerdict.KEEP.value
    skip_reason: str = ""
    keep_reason: str = ""
    bridge_policy: str = BridgePolicy.NONE.value
    authority: str = "preprocessing"
    confidence: float = 1.0
    evidence: str = ""
    screenshot_path: str = ""
    raw_action_type: str = ""
    raw_action_payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StepDisposition:
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


def normalize_skip_reason(reason: str) -> str:
    text = str(reason or "").strip().lower()
    if text in ALL_SKIP_REASONS:
        return text
    mapped = LEGACY_SKIP_REASON_MAP.get(text, "")
    return mapped if mapped else text


def resolve_bridge_policy(skip_reason: str) -> str:
    reason = normalize_skip_reason(skip_reason)
    if reason in ROUTE_BLOCKING_REASONS:
        return BridgePolicy.BLOCKS_ROUTE.value
    if reason in BRIDGE_ALLOWED_REASONS:
        return BridgePolicy.ALLOWS_ROUTE_BRIDGE.value
    return BridgePolicy.NONE.value


def is_app_owned_surface(text: str) -> bool:
    lower = str(text or "").lower()
    return any(marker in lower for marker in APP_OWNED_SURFACE_MARKERS)


def app_owned_negative_guard(
    verdict: str,
    evidence_text: str = "",
) -> str:
    if verdict == StepVerdict.SKIP.value and is_app_owned_surface(evidence_text):
        return StepVerdict.NEEDS_REASONING.value
    return verdict


def build_preprocessing_disposition(
    *,
    trace_id: str,
    step_index: int,
    skip_reason: str,
    evidence: str = "",
    screenshot_path: str = "",
    raw_action_type: str = "",
    raw_action_payload: Optional[Dict[str, Any]] = None,
    confidence: float = 1.0,
) -> StepDisposition:
    normalized = normalize_skip_reason(skip_reason)
    bridge = resolve_bridge_policy(normalized)
    return StepDisposition(
        trace_id=trace_id,
        step_index=step_index,
        verdict=StepVerdict.SKIP.value,
        skip_reason=normalized,
        bridge_policy=bridge,
        authority="preprocessing",
        confidence=confidence,
        evidence=evidence,
        screenshot_path=screenshot_path,
        raw_action_type=raw_action_type,
        raw_action_payload=raw_action_payload or {},
    )


def build_keep_disposition(
    *,
    trace_id: str,
    step_index: int,
    keep_reason: str,
    evidence: str = "",
    screenshot_path: str = "",
    raw_action_type: str = "",
    raw_action_payload: Optional[Dict[str, Any]] = None,
    authority: str = "preprocessing",
    confidence: float = 1.0,
) -> StepDisposition:
    return StepDisposition(
        trace_id=trace_id,
        step_index=step_index,
        verdict=StepVerdict.KEEP.value,
        keep_reason=str(keep_reason or "target_app_business_action"),
        bridge_policy=BridgePolicy.NONE.value,
        authority=authority,
        confidence=confidence,
        evidence=evidence,
        screenshot_path=screenshot_path,
        raw_action_type=raw_action_type,
        raw_action_payload=raw_action_payload or {},
    )


def is_hard_keep_disposition(data: Dict[str, Any] | StepDisposition) -> bool:
    item = data.to_dict() if isinstance(data, StepDisposition) else data
    if not isinstance(item, dict):
        return False
    verdict = str(item.get("verdict", "")).strip().lower()
    if verdict != StepVerdict.KEEP.value:
        return False
    authority = str(item.get("authority", "")).strip()
    keep_reason = str(item.get("keep_reason", "")).strip()
    return authority in HARD_KEEP_AUTHORITIES and keep_reason in HARD_KEEP_REASONS


def validate_step_authority_consistency(
    step: Dict[str, Any],
    *,
    trace_id: str = "",
    step_index: int = -1,
) -> List[str]:
    if not isinstance(step, dict):
        return []
    effective = step.get("effective_step")
    disposition = step.get("step_disposition")
    if not isinstance(effective, dict) or not isinstance(disposition, dict):
        return []
    effective_verdict = str(effective.get("verdict", "")).strip().lower()
    disposition_verdict = str(disposition.get("verdict", "")).strip().lower()
    if not effective_verdict or not disposition_verdict:
        return []
    effective_norm = "skip" if effective_verdict in {"skip", "drop", "manual", "manual_required", "not_applicable"} else effective_verdict
    disposition_norm = "skip" if disposition_verdict in {"skip", "drop", "manual", "manual_required", "not_applicable"} else disposition_verdict
    if effective_norm == disposition_norm:
        return []
    label = f"{trace_id}:{step_index}" if trace_id or step_index >= 0 else "<unknown>"
    return [
        "step authority conflict: "
        f"{label} effective_step.verdict={effective_verdict} "
        f"step_disposition.verdict={disposition_verdict} "
        f"skip_reason={disposition.get('skip_reason', '')}"
    ]


def disposition_to_skipped_step(d: StepDisposition) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "trace_id": d.trace_id,
        "step_index": d.step_index,
        "verdict": d.verdict,
        "skip_reason": d.skip_reason,
        "authority": d.authority,
        "bridge_policy": d.bridge_policy,
    }
    if d.evidence:
        result["reason"] = d.evidence
    if d.screenshot_path:
        result["screenshot_path"] = d.screenshot_path
    if d.raw_action_type:
        result["raw_action"] = d.raw_action_type
    if d.raw_action_payload:
        result["raw_action_payload"] = d.raw_action_payload
    return result


def merge_dispositions(
    preprocessing: List[StepDisposition],
    reasoning: List[StepDisposition],
) -> List[StepDisposition]:
    by_key: Dict[str, StepDisposition] = {}
    for d in preprocessing:
        key = f"{d.trace_id}:{d.step_index}"
        by_key[key] = d
    for d in reasoning:
        key = f"{d.trace_id}:{d.step_index}"
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = d
        elif existing.verdict == StepVerdict.SKIP.value and existing.authority == "preprocessing":
            pass
        else:
            by_key[key] = d
    return sorted(by_key.values(), key=lambda d: (d.trace_id, d.step_index))
