
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


CREATE_HINT_MARKERS = ("发布", "创作", "写文字", "发图文", "发视频", "拍视频", "create", "compose", "publish")
CART_INCREMENT_HINT_MARKERS = ("加号", "黄色“+”", "“+”", "+", "加入购物车", "加购", "增加", "add to cart", "add")
CART_DECREMENT_HINT_MARKERS = ("减号", "“-”", "-", "移出购物车", "减少", "remove from cart", "minus", "decrement")
EXPLICIT_CART_MARKERS = ("加入购物车", "加购", "移出购物车", "remove from cart", "add to cart")
FILTER_HINT_MARKERS = ("筛选", "filter", "过滤", "排序", "sort")
OPTION_HINT_MARKERS = ("规格", "选规格", "option", "sku", "马上抢", "抢购")
REVIEW_HINT_MARKERS = ("评价", "评论", "全部", "最新", "差评", "好评", "review", "comment")
OVERLAY_HINT_MARKERS = ("弹窗", "面板", "浮层", "drawer", "sheet", "modal", "panel", "overlay")


@dataclass
class ActionEvidence:
    source_filename: str
    target_filename: str
    operation: str
    intent_text: str
    source_step_key: str = ""
    next_step_key: str = ""
    render_role: str = ""
    next_render_role: str = ""
    runtime_observations: list[dict[str, Any]] = field(default_factory=list)
    schema_entities: list[dict[str, Any]] = field(default_factory=list)
    route_contract: dict[str, Any] = field(default_factory=dict)
    surface_contract: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionEffectAdmission:
    admission_status: Literal["accepted", "weak_hint_only", "unresolved"]
    effect_family: str = ""
    reducer: str = ""
    state_effect: dict[str, Any] = field(default_factory=dict)
    domain_intent: str = ""
    evidence: list[str] = field(default_factory=list)
    weak_candidates: list[str] = field(default_factory=list)
    rejected_candidates: list[dict[str, Any]] = field(default_factory=list)
    unresolved_reason: str = ""


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lower = str(text or "").lower()
    return any(marker.lower() in lower for marker in markers)


def _base_filename(value: str) -> str:
    return str(value or "").split("?")[0].split("#")[0]


def _state_ref(scope: str) -> str:
    seed = "".join(ch if ch.isalnum() else "_" for ch in str(scope or "state").strip()).strip("_").lower()
    return f"state::{seed or 'scope'}"


def _quoted_or_clicked_label(text: str) -> str:
    text = str(text or "")
    for left, right in (("“", "”"), ("\"", "\""), ("'", "'")):
        if left in text and right in text:
            start = text.find(left)
            end = text.find(right, start + 1)
            if end > start:
                return text[start + 1:end].strip()
    return text.strip()


def _weak_candidates_for_text(text: str) -> list[str]:
    candidates: list[str] = []
    if _contains_any(text, CART_INCREMENT_HINT_MARKERS + CART_DECREMENT_HINT_MARKERS):
        candidates.append("cart_mutation")
    if _contains_any(text, FILTER_HINT_MARKERS):
        candidates.append("filter_selection")
    if _contains_any(text, REVIEW_HINT_MARKERS):
        candidates.append("review_or_tab_selection")
    if _contains_any(text, OPTION_HINT_MARKERS):
        candidates.append("option_overlay")
    if _contains_any(text, OVERLAY_HINT_MARKERS):
        candidates.append("runtime_overlay")
    if _contains_any(text, CREATE_HINT_MARKERS):
        candidates.append("create_entry")
    return list(dict.fromkeys(candidates))


def _evidence_texts(evidence: ActionEvidence) -> list[str]:
    texts = [
        evidence.source_filename,
        evidence.target_filename,
        evidence.intent_text,
        evidence.render_role,
        evidence.next_render_role,
    ]
    for item in evidence.runtime_observations:
        if isinstance(item, dict):
            texts.extend(str(value) for value in item.values() if value not in ("", [], {}))
    for item in evidence.schema_entities:
        if isinstance(item, dict):
            texts.extend(str(value) for value in item.values() if value not in ("", [], {}))
    for container in (evidence.route_contract, evidence.surface_contract):
        for value in container.values():
            if isinstance(value, list):
                texts.extend(str(item) for item in value)
            elif value not in ("", [], {}):
                texts.append(str(value))
    return texts


def _has_cart_evidence(evidence: ActionEvidence) -> bool:
    joined = " ".join(_evidence_texts(evidence)).lower()
    if _contains_any(evidence.intent_text, EXPLICIT_CART_MARKERS):
        return True
    cart_terms = ("cart", "cartitem", "shopping_cart", "购物车", "加购", "加入购物车")
    quantity_terms = ("quantity", "qty", "badge", "count", "delta", "数量", "份", "件")
    product_terms = ("product", "menuitem", "sku", "商品", "菜品")
    return (
        any(term in joined for term in cart_terms)
        and (any(term in joined for term in quantity_terms) or any(term in joined for term in product_terms))
    )


def _cart_delta(text: str) -> int:
    if _contains_any(text, CART_DECREMENT_HINT_MARKERS):
        return -1
    return 1


def _has_action_sheet_evidence(evidence: ActionEvidence) -> bool:
    surface = evidence.surface_contract if isinstance(evidence.surface_contract, dict) else {}
    opened_kind = str(surface.get("opened_surface_kind") or surface.get("surface_kind") or surface.get("overlay_kind") or "")
    if opened_kind == "action_sheet":
        return True
    if str(evidence.next_render_role or "") in {"post_interaction_overlay", "interaction_state_change"}:
        options = surface.get("opened_surface_options")
        if isinstance(options, list) and options:
            return True


        return _contains_any(evidence.intent_text, OVERLAY_HINT_MARKERS)
    return False


def _has_runtime_overlay_observation(evidence: ActionEvidence) -> bool:
    structural_keys = (
        "state_scope",
        "lifecycle",
        "mutation",
        "interaction_surface_role",
        "opened_surface_identity",
        "raw_evidence",
    )
    for item in evidence.runtime_observations:
        if not isinstance(item, dict):
            continue
        structural_text = " ".join(
            str(item.get(key) or "")
            for key in structural_keys
        )
        if _contains_any(structural_text, OVERLAY_HINT_MARKERS):
            return True
    return False


def admit_same_page_click_effect(evidence: ActionEvidence) -> ActionEffectAdmission:
    source = _base_filename(evidence.source_filename)
    target = _base_filename(evidence.target_filename)
    operation = str(evidence.operation or "").lower()
    text = str(evidence.intent_text or "")
    weak_candidates = _weak_candidates_for_text(text)
    if operation not in {"click", "tap"} or not source or source != target:
        return ActionEffectAdmission(
            admission_status="unresolved",
            weak_candidates=weak_candidates,
            unresolved_reason="not_same_page_click",
        )

    label = _quoted_or_clicked_label(text)
    if _has_action_sheet_evidence(evidence):
        state_ref = _state_ref(f"{source}::action_sheet")
        surface = evidence.surface_contract if isinstance(evidence.surface_contract, dict) else {}
        options = surface.get("opened_surface_options") if isinstance(surface.get("opened_surface_options"), list) else []
        effect = {
            "effect": "open_runtime_overlay",
            "overlay_kind": "action_sheet",
            "state_ref": state_ref,
            "selected_label": label,
            "mutation_policy": "open_action_sheet_without_committing_selection",
            "template_id": "action_sheet",
            "effect_admission_status": "accepted",
            "effect_family": "open_runtime_overlay",
            "domain_intent": "create_entry_hint" if _contains_any(text, CREATE_HINT_MARKERS) else "",
            "effect_evidence": ["same_page_click", "opened_action_sheet_surface"],
        }
        if options:
            effect["opened_surface_options"] = [str(item) for item in options]
        return ActionEffectAdmission(
            admission_status="accepted",
            effect_family="open_runtime_overlay",
            reducer="open_overlay",
            state_effect=effect,
            domain_intent=str(effect.get("domain_intent") or ""),
            evidence=list(effect["effect_evidence"]),
            weak_candidates=[item for item in weak_candidates if item != "cart_mutation"],
        )

    if (
        str(evidence.next_render_role or "") == "post_interaction_overlay"
        and _has_runtime_overlay_observation(evidence)
    ):
        state_ref = _state_ref(f"{source}::runtime_overlay_stack")
        return ActionEffectAdmission(
            admission_status="accepted",
            effect_family="open_runtime_overlay",
            reducer="open_overlay",
            state_effect={
                "effect": "open_runtime_overlay",
                "overlay_kind": "runtime_overlay",
                "state_ref": state_ref,
                "selected_label": label,
                "mutation_policy": "open_observed_post_action_overlay",
                "effect_admission_status": "accepted",
                "effect_family": "open_runtime_overlay",
                "effect_evidence": [
                    "same_page_click",
                    "next_observation_post_interaction_overlay",
                ],
            },
            evidence=[
                "same_page_click",
                "next_observation_post_interaction_overlay",
            ],
            weak_candidates=[
                item for item in weak_candidates if item != "cart_mutation"
            ],
        )

    if "cart_mutation" in weak_candidates and _has_cart_evidence(evidence):
        state_ref = _state_ref(f"{source}::cart_accumulator")
        delta = _cart_delta(text)
        effect = {
            "effect": "update_cart_quantity",
            "state_ref": state_ref,
            "selected_label": label,
            "delta": delta,
            "mutation_policy": "derive_visible_quantity_and_badge_from_cart_state",
            "visible_initial_prefill_allowed": False,
            "effect_admission_status": "accepted",
            "effect_family": "cart_mutation",
            "domain_intent": "cart_quantity",
            "effect_evidence": ["same_page_click", "cart_quantity_evidence"],
        }
        return ActionEffectAdmission(
            admission_status="accepted",
            effect_family="cart_mutation",
            reducer="update_cart_quantity",
            state_effect=effect,
            domain_intent="cart_quantity",
            evidence=list(effect["effect_evidence"]),
            weak_candidates=weak_candidates,
        )

    rejected = [
        {"candidate": candidate, "reason": "missing_strong_reference_trace_evidence"}
        for candidate in weak_candidates
    ]
    return ActionEffectAdmission(
        admission_status="unresolved",
        weak_candidates=weak_candidates,
        rejected_candidates=rejected,
        unresolved_reason="same_page_click_has_only_weak_semantic_hints",
    )
