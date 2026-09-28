
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _simple_token(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9一-鿿()（）]+", "_", text).strip("_") or fallback


_GENERIC_STATE_CONTROL_LABELS = frozenset({
    "加入购物车", "加购", "+", "去结算", "立即购买", "提交", "确认",
    "-", "−", "减少", "增加",
})


def _action_family(component: Dict[str, Any], contract: Dict[str, Any]) -> str:
    intent = _text(contract.get("action_intent") or contract.get("operation_intent") or component.get("operation_intent"))
    operation = _text(contract.get("operation") or component.get("operation"))
    label = _text(component.get("required_display_label") or contract.get("display_label"))
    intent_lower = intent.lower()
    label_lower = label.lower()

    if any(k in intent_lower for k in ("输入", "搜索框", "query", "type_search", "fill_form")):
        return "query_input"
    if any(k in intent_lower for k in ("选择候选", "commit_query", "select_suggestion", "选中搜索建议")):
        return "query_candidate_commit"
    if any(k in intent_lower for k in ("加购", "加入购物车", "add_to_cart", "添加商品")):
        return "add_to_cart"
    if any(k in intent_lower for k in ("提交", "确认", "结算", "submit", "checkout", "commit")):
        return "submit"
    if any(k in intent_lower for k in ("选择", "select_option", "option_select", "pick")):
        if "filter" not in intent_lower and "tab" not in intent_lower:
            return "option_select"
    expected = _text(contract.get("expected_filename"))
    if expected and operation in ("click", "tap"):
        return "navigation"
    return "interaction"


def _visual_signature_for_component(
    component: Dict[str, Any],
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    label = _text(
        component.get("required_display_label")
        or component.get("display_label")
        or contract.get("display_label")
    )
    token_label = _simple_token(label, "unlabeled")
    family = _action_family(component, contract)
    kind = _simple_token(component.get("component_kind"), "component")

    if family == "query_input":
        return {
            "signature": "visual_surface|query_input_surface",
            "reason": "value-insensitive query input surface",
            "source": "l0_visual_surface_compiler",
        }
    if family == "query_candidate_commit":
        return {
            "signature": f"visual_surface|query_suggestion_surface|{token_label}",
            "reason": "visible query candidate row/commit surface",
            "source": "l0_visual_surface_compiler",
        }
    if family == "add_to_cart" and label not in _GENERIC_STATE_CONTROL_LABELS:
        return {
            "signature": f"visual_surface|resource_row_identity|{token_label}",
            "reason": "task-required resource row identity",
            "source": "l0_visual_surface_compiler",
        }
    if family == "submit":
        return {
            "signature": f"visual_surface|route_affordance|{token_label}",
            "reason": "task-required route affordance",
            "source": "l0_visual_surface_compiler",
        }
    if family == "option_select":
        return {
            "signature": f"visual_surface|component_surface|{kind}|{family}|{token_label}",
            "reason": "task-required component surface",
            "source": "l0_visual_surface_compiler",
        }
    return {}


def _component_source_steps(component: Dict[str, Any]) -> List[str]:
    steps = component.get("source_step_keys")
    if isinstance(steps, list):
        return [_text(s) for s in steps if _text(s)]
    return []


def _trace_id_from_step_key(step_key: str) -> str:
    parts = step_key.rsplit(":step", 1)
    if len(parts) == 2:
        return parts[0]
    parts = step_key.rsplit(":", 1)
    return parts[0] if len(parts) == 2 else step_key


def compile_visual_surface_signatures(
    page_environment_specs: Mapping[str, Any],
    *,
    focus_trace_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:

    payload = page_environment_specs if isinstance(page_environment_specs, dict) else {}
    specs_by_filename = _as_dict(payload.get("page_environment_specs_by_filename"))

    by_step: Dict[str, List[Dict[str, Any]]] = {}
    by_page: Dict[str, List[Dict[str, Any]]] = {}
    total_count = 0

    for page_filename, spec in specs_by_filename.items():
        if not isinstance(spec, dict):
            continue
        filename = _text(spec.get("filename") or page_filename)
        components = _as_list(spec.get("components"))
        page_signatures: List[Dict[str, Any]] = []

        for component in components:
            if not isinstance(component, dict):
                continue
            if component.get("interaction_required") is not True:
                continue
            role = _text(component.get("component_role"))
            if "l0_execution_anchor" not in role and not component.get("l0_transition_ids"):
                continue

            contract = _as_dict(component.get("interaction_contract"))
            surface = _visual_signature_for_component(component, contract)
            if not surface:
                continue

            page_signatures.append(surface)

            for step_key in _component_source_steps(component):
                trace_id = _trace_id_from_step_key(step_key)
                if focus_trace_ids and trace_id not in focus_trace_ids:
                    continue
                by_step.setdefault(step_key, []).append(surface)
                total_count += 1

        if page_signatures:
            by_page[filename] = page_signatures

    return {
        "format": "l0_visual_surface_signatures",
        "schema_revision": 1,
        "visual_surface_signatures_by_step": by_step,
        "visual_surface_signatures_by_page": by_page,
        "summary": {
            "total_signatures": total_count,
            "step_count": len(by_step),
            "page_count": len(by_page),
        },
    }
