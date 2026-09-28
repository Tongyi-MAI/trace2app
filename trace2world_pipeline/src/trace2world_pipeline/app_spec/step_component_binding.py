
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


OPERATIONS_NEEDING_BINDING = {"click", "fill", "long_press"}

_INPUT_KINDS = {"input", "form_field", "input_field"}
_NON_FIELD_INPUT_KINDS = {"form", "state_projection", "section", "container"}
_ROUTE_AFFORDANCE_KINDS = {"shell_navigation_affordance", "fixed_slot_nav"}

_QUOTED_RE = re.compile(r"[“”‘’\"\'`]([^“”‘’\"\'`]{1,80})[“”‘’\"\'`]")
_PUNCT_RE = re.compile(r"[\s　·\-_.,，。、:：!！?？()\[\]{}]+")


@dataclass
class StepComponentBinding:
    trace_id: str
    step_index: int
    source_filename: str
    operation: str
    status: str
    component_id: str = ""
    binding_source: str = ""
    candidates: List[str] = field(default_factory=list)
    blocking_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if not self.candidates:
            d.pop("candidates", None)
        if self.status == "resolved":
            d.pop("blocking_reason", None)
        elif self.status == "not_applicable":
            d.pop("blocking_reason", None)
            d.pop("binding_source", None)
        if not d.get("component_id"):
            d.pop("component_id", None)
        if not d.get("binding_source"):
            d.pop("binding_source", None)
        return d


def _normalize_label(value: Any) -> str:
    return _PUNCT_RE.sub("", str(value or "")).lower()


def _quoted_phrases(intent: str) -> List[str]:
    return [m for m in _QUOTED_RE.findall(intent or "") if m]


def _is_input_like(component: Dict[str, Any]) -> bool:
    kind = str(component.get("component_kind") or "").lower()
    role = str(component.get("component_role") or "").lower()
    if kind in {"button", "button_or_card", "navigation_control", "submit_control", "selection_control"}:
        return kind in _INPUT_KINDS
    if kind in _NON_FIELD_INPUT_KINDS:
        return False
    return kind in _INPUT_KINDS or "input" in role or "form_field" in role


def _is_l0_execution_anchor(component: Dict[str, Any]) -> bool:
    return (
        str(component.get("component_role") or "").lower() == "l0_execution_anchor"
        or "::l0::" in _component_id(component)
    )


def _is_route_affordance(component: Dict[str, Any]) -> bool:
    kind = str(component.get("component_kind") or "").lower()
    return kind in _ROUTE_AFFORDANCE_KINDS


def _canonical_input_candidates(components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    canonical = [
        c
        for c in components
        if _is_input_like(c) and not _is_l0_execution_anchor(c) and not _is_route_affordance(c)
    ]
    return canonical or [
        c
        for c in components
        if _is_input_like(c) and not _is_route_affordance(c)
    ]


def _component_id(component: Dict[str, Any]) -> str:
    return str(component.get("component_id") or "")


def _select_best_by_label(
    candidates: List[Dict[str, Any]], intent: str
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    intent_norm = _normalize_label(intent)
    quoted = [_normalize_label(q) for q in _quoted_phrases(intent) if q]
    scored: List[tuple] = []
    for c in candidates:
        label = _normalize_label(c.get("display_label"))
        if not label:
            continue
        score = 0
        if quoted:
            if any(label == q for q in quoted):
                score += 1000
            elif any(label and (q in label or label in q) for q in quoted):
                score += 500
        if intent_norm:
            if label in intent_norm:
                score += 200 + min(len(label), 50)
            elif intent_norm in label:
                score += 100 + min(len(label), 50)
        if score > 0:
            scored.append((score, c))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) >= 2 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _next_same_page_fill_intent(
    steps: List[Dict[str, Any]], index: int, source_filename: str
) -> str:
    for s in steps[index + 1 :]:
        if not isinstance(s, dict):
            continue
        pre = s.get("pre_state") if isinstance(s.get("pre_state"), dict) else {}
        pre_file = str(pre.get("filename") or "")
        if pre_file and source_filename and pre_file != source_filename:
            return ""
        action = s.get("action") if isinstance(s.get("action"), dict) else {}
        op = str(action.get("operation") or "").lower()
        if op == "fill":
            return str(action.get("intent") or action.get("action_text") or "")
        return ""
    return ""


def _next_same_page_fill_step_key(
    steps: List[Dict[str, Any]],
    index: int,
    source_filename: str,
    trace_id: str,
) -> str:
    for s in steps[index + 1 :]:
        if not isinstance(s, dict):
            continue
        pre = s.get("pre_state") if isinstance(s.get("pre_state"), dict) else {}
        pre_file = str(pre.get("filename") or "")
        if pre_file and source_filename and pre_file != source_filename:
            return ""
        action = s.get("action") if isinstance(s.get("action"), dict) else {}
        op = str(action.get("operation") or "").lower()
        if op == "fill":
            try:
                next_step_index = int(s.get("step_index") or 0)
            except (TypeError, ValueError):
                return ""
            return f"{trace_id}:step{next_step_index}"
        return ""
    return ""


def _find_l0_execution_anchor(
    components: List[Dict[str, Any]], step_key: str
) -> Optional[Dict[str, Any]]:
    if not step_key:
        return None
    matches: List[Dict[str, Any]] = []
    for c in components:
        if not _is_l0_execution_anchor(c):
            continue
        source_keys = c.get("source_step_keys") or []
        if isinstance(source_keys, list) and step_key in [str(k) for k in source_keys]:
            matches.append(c)
            continue
        transition_ids = c.get("l0_transition_ids") or []
        if isinstance(transition_ids, list) and step_key in [str(k) for k in transition_ids]:
            matches.append(c)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        def score(component: Dict[str, Any]) -> tuple[int, int, int, int]:
            cid = _component_id(component)
            role = str(component.get("component_role") or "").lower()
            kind = str(component.get("component_kind") or "").lower()
            source = str(component.get("source") or "").lower()
            is_llm_l0_duplicate = source == "llm_page_environment_spec_enrichment"
            return (
                0 if is_llm_l0_duplicate else 1,
                1 if role != "l0_execution_anchor" and not is_llm_l0_duplicate else 0,
                1 if cid.endswith("::source") else 0,
                1 if kind in {"button", "input", "navigation_control", "submit_control", "button_or_card"} else 0,
            )

        ranked = sorted(matches, key=score, reverse=True)
        if score(ranked[0]) != score(ranked[1]):
            return ranked[0]
    return None


def _components_matching_step(
    components: List[Dict[str, Any]], step_key: str
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for c in components:
        source_keys = [str(k) for k in c.get("source_step_keys") or []]
        transition_ids = [str(k) for k in c.get("l0_transition_ids") or []]
        if step_key in source_keys or step_key in transition_ids:
            matches.append(c)
    return matches


def _is_input_anchor(component: Optional[Dict[str, Any]]) -> bool:
    return bool(component) and _is_l0_execution_anchor(component or {}) and _is_input_like(component or {})


def _is_actionable_anchor(component: Optional[Dict[str, Any]]) -> bool:
    if not component or not _is_l0_execution_anchor(component):
        return False
    kind = str(component.get("component_kind") or "").lower()
    return kind not in {"section", "state_projection"}


def _prefer_stable_input_identity(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    non_l0 = [
        c for c in candidates
        if not _is_l0_execution_anchor(c) and "::l0::" not in _component_id(c)
    ]
    if len(non_l0) == 1:
        return non_l0[0]
    return None


def _bind_step(
    step: Dict[str, Any],
    steps: List[Dict[str, Any]],
    step_index_in_list: int,
    components: List[Dict[str, Any]],
    trace_id: str,
) -> StepComponentBinding:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    operation = str(action.get("operation") or "").lower()
    intent = str(action.get("intent") or action.get("action_text") or "")
    pre_state = step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
    post_state = (
        step.get("expected_post_state")
        if isinstance(step.get("expected_post_state"), dict)
        else {}
    )
    source_filename = str(pre_state.get("filename") or "")
    target_filename = str(post_state.get("filename") or "")
    step_index = int(step.get("step_index") or 0)

    binding = StepComponentBinding(
        trace_id=trace_id,
        step_index=step_index,
        source_filename=source_filename,
        operation=operation,
        status="unresolved",
    )

    if str(step.get("execution_policy") or "") in {
        "normalize_initial_app_open",
        "external_system_observation_skip",
    }:
        binding.status = "not_applicable"
        return binding

    if operation not in OPERATIONS_NEEDING_BINDING:
        binding.status = "not_applicable"
        return binding

    if not source_filename:
        if target_filename:
            binding.status = "not_applicable"
            return binding
        binding.blocking_reason = "missing_source_filename"
        return binding

    if not components:
        binding.blocking_reason = "page_environment_spec_missing_components"
        return binding

    step_key = f"{trace_id}:step{step_index}"
    anchor = _find_l0_execution_anchor(components, step_key)

    if operation == "click":
        is_route = bool(target_filename) and target_filename != source_filename
        if is_route:
            all_route_candidates = [
                c
                for c in components
                if str(c.get("target_filename") or "") == target_filename
            ]
            route_candidates = [
                c
                for c in all_route_candidates
                if not _is_l0_execution_anchor(c)
            ] or all_route_candidates
            if len(route_candidates) == 1:
                binding.status = "resolved"
                binding.component_id = _component_id(route_candidates[0])
                binding.binding_source = "route_target_unique"
                return binding
            if len(route_candidates) > 1:
                pick = _select_best_by_label(route_candidates, intent)
                if pick:
                    binding.status = "resolved"
                    binding.component_id = _component_id(pick)
                    binding.binding_source = "route_target_label_match"
                    return binding
                binding.candidates = [_component_id(c) for c in route_candidates]
                binding.blocking_reason = "route_candidates_ambiguous"
                return binding
            if _is_actionable_anchor(anchor):
                binding.status = "resolved"
                binding.component_id = _component_id(anchor or {})
                binding.binding_source = "l0_execution_anchor_route_fallback"
                return binding
            binding.blocking_reason = "no_component_with_target_filename"
            return binding

        next_fill_intent = _next_same_page_fill_intent(
            steps, step_index_in_list, source_filename
        )
        if next_fill_intent:
            if _is_actionable_anchor(anchor) and not _is_input_anchor(anchor):
                binding.status = "resolved"
                binding.component_id = _component_id(anchor or {})
                binding.binding_source = "l0_execution_anchor_inline_activation"
                return binding
            input_candidates = _canonical_input_candidates(components)
            step_matched_inputs = [
                c for c in _components_matching_step(input_candidates, step_key)
                if _is_input_like(c)
            ]
            pick = _prefer_stable_input_identity(step_matched_inputs)
            if pick:
                binding.status = "resolved"
                binding.component_id = _component_id(pick)
                binding.binding_source = "same_page_input_step_identity"
                return binding
            if _is_input_anchor(anchor):
                binding.status = "resolved"
                binding.component_id = _component_id(anchor or {})
                binding.binding_source = "l0_execution_anchor_input_activation"
                return binding
            next_fill_step_key = _next_same_page_fill_step_key(
                steps, step_index_in_list, source_filename, trace_id
            )
            if next_fill_step_key:
                next_step_matched_inputs = [
                    c for c in _components_matching_step(input_candidates, next_fill_step_key)
                    if _is_input_like(c)
                ]
                pick = _prefer_stable_input_identity(next_step_matched_inputs)
                if pick:
                    binding.status = "resolved"
                    binding.component_id = _component_id(pick)
                    binding.binding_source = "same_page_input_next_fill_identity"
                    return binding
            if len(input_candidates) == 1:
                binding.status = "resolved"
                binding.component_id = _component_id(input_candidates[0])
                binding.binding_source = "same_page_input_unique"
                return binding
            if input_candidates:
                merged = (intent + " " + next_fill_intent).strip()
                pick = _select_best_by_label(input_candidates, merged)
                if pick:
                    binding.status = "resolved"
                    binding.component_id = _component_id(pick)
                    binding.binding_source = "same_page_input_label_match"
                    return binding
                binding.candidates = [_component_id(c) for c in input_candidates]
                binding.blocking_reason = "input_candidates_ambiguous"
                return binding
            binding.blocking_reason = "no_input_component_for_fill_pair"
            return binding

        same_page_candidates = [
            c
            for c in components
            if (
                not _is_l0_execution_anchor(c)
                and not _is_route_affordance(c)
                and not _is_input_like(c)
            )
        ]
        pick = _select_best_by_label(same_page_candidates, intent)
        if pick:
            binding.status = "resolved"
            binding.component_id = _component_id(pick)
            binding.binding_source = "same_page_label_match"
            return binding
        if anchor:
            binding.status = "resolved"
            binding.component_id = _component_id(anchor)
            binding.binding_source = "l0_execution_anchor_fallback"
            return binding
        binding.blocking_reason = "no_same_page_component_matched"
        return binding

    if operation == "fill":
        if _is_input_anchor(anchor):
            binding.status = "resolved"
            binding.component_id = _component_id(anchor or {})
            binding.binding_source = "l0_execution_anchor_fill"
            return binding
        input_candidates = _canonical_input_candidates(components)
        step_matched_inputs = [
            c for c in _components_matching_step(input_candidates, step_key)
            if _is_input_like(c)
        ]
        if len(step_matched_inputs) == 1:
            binding.status = "resolved"
            binding.component_id = _component_id(step_matched_inputs[0])
            binding.binding_source = "fill_input_step_identity"
            return binding
        if len(input_candidates) == 1:
            binding.status = "resolved"
            binding.component_id = _component_id(input_candidates[0])
            binding.binding_source = "fill_input_unique"
            return binding
        if input_candidates:
            pick = _select_best_by_label(input_candidates, intent)
            if pick:
                binding.status = "resolved"
                binding.component_id = _component_id(pick)
                binding.binding_source = "fill_input_label_match"
                return binding
            binding.candidates = [_component_id(c) for c in input_candidates]
            binding.blocking_reason = "fill_input_candidates_ambiguous"
            return binding
        binding.blocking_reason = "no_input_component_for_fill"
        return binding

    binding.blocking_reason = "operation_not_handled"
    return binding


def _binding_to_target(binding_dict: Dict[str, Any]) -> Dict[str, Any]:
    status = str(binding_dict.get("status") or "")
    target: Dict[str, Any] = {"status": status}
    component_id = binding_dict.get("component_id")
    if component_id:
        target["component_id"] = component_id
    if binding_dict.get("binding_source"):
        target["binding_source"] = binding_dict["binding_source"]
    if binding_dict.get("blocking_reason"):
        target["blocking_reason"] = binding_dict["blocking_reason"]
    if binding_dict.get("candidates"):
        target["candidates"] = binding_dict["candidates"]
    return target


def apply_step_component_bindings_to_pack(
    l0_execution_pack: Dict[str, Any],
    bindings_artifact: Dict[str, Any],
) -> Dict[str, Any]:
    by_step = (bindings_artifact or {}).get("bindings_by_step") or {}
    action_packs = l0_execution_pack.get("action_packs") if isinstance(l0_execution_pack, dict) else None
    if not isinstance(action_packs, list):
        return l0_execution_pack
    for pack in action_packs:
        if not isinstance(pack, dict):
            continue
        trace_id = str(pack.get("trace_id") or "")
        trace_bindings = by_step.get(trace_id) or {}
        steps = pack.get("steps") if isinstance(pack.get("steps"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_idx = int(step.get("step_index") or 0)
            binding = trace_bindings.get(step_idx)
            if binding is None and isinstance(trace_bindings, dict):

                binding = trace_bindings.get(str(step_idx))
            if binding is None:
                step["target"] = {
                    "status": "unresolved",
                    "blocking_reason": "no_binding_record",
                }
            else:
                step["target"] = _binding_to_target(binding)


    if isinstance(l0_execution_pack.get("action_packs_by_trace_id"), dict):
        l0_execution_pack["action_packs_by_trace_id"] = {
            str(p.get("trace_id") or ""): p
            for p in action_packs
            if isinstance(p, dict) and p.get("trace_id")
        }
    return l0_execution_pack


def selector_for_component_id(component_id: str) -> str:
    cid = str(component_id or "")
    if not cid:
        return ""
    return f'[data-element-id="{cid}"], [data-element-aliases~="{cid}"]'


def finalize_selectors_in_pack(l0_execution_pack: Dict[str, Any]) -> Dict[str, Any]:
    action_packs = l0_execution_pack.get("action_packs") if isinstance(l0_execution_pack, dict) else None
    if not isinstance(action_packs, list):
        return l0_execution_pack
    for pack in action_packs:
        if not isinstance(pack, dict):
            continue
        steps = pack.get("steps") if isinstance(pack.get("steps"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                continue
            target = step.get("target")
            if not isinstance(target, dict):
                continue
            if target.get("status") != "resolved":
                continue
            component_id = str(target.get("component_id") or "")
            if not component_id:
                continue
            target["selector"] = selector_for_component_id(component_id)
    if isinstance(l0_execution_pack.get("action_packs_by_trace_id"), dict):
        l0_execution_pack["action_packs_by_trace_id"] = {
            str(p.get("trace_id") or ""): p
            for p in action_packs
            if isinstance(p, dict) and p.get("trace_id")
        }
    return l0_execution_pack


def build_step_component_bindings(
    l0_execution_pack: Dict[str, Any],
    page_environment_specs_by_filename: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    action_packs = l0_execution_pack.get("action_packs") if isinstance(l0_execution_pack, dict) else None
    if not isinstance(action_packs, list):
        action_packs = []

    bindings: List[StepComponentBinding] = []
    bindings_by_step: Dict[str, Dict[int, Dict[str, Any]]] = {}

    for pack in action_packs:
        if not isinstance(pack, dict):
            continue
        trace_id = str(pack.get("trace_id") or "")
        steps = pack.get("steps") if isinstance(pack.get("steps"), list) else []
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            pre = step.get("pre_state") if isinstance(step.get("pre_state"), dict) else {}
            source_filename = str(pre.get("filename") or "")
            page_spec = page_environment_specs_by_filename.get(source_filename) or {}
            components = page_spec.get("components") if isinstance(page_spec.get("components"), list) else []
            binding = _bind_step(step, steps, i, components, trace_id)
            bindings.append(binding)
            if trace_id:
                bindings_by_step.setdefault(trace_id, {})[binding.step_index] = binding.to_dict()

    summary = {
        "total": len(bindings),
        "resolved": sum(1 for b in bindings if b.status == "resolved"),
        "unresolved": sum(1 for b in bindings if b.status == "unresolved"),
        "not_applicable": sum(1 for b in bindings if b.status == "not_applicable"),
        "by_blocking_reason": {},
        "by_binding_source": {},
    }
    for b in bindings:
        if b.status == "unresolved" and b.blocking_reason:
            summary["by_blocking_reason"][b.blocking_reason] = (
                summary["by_blocking_reason"].get(b.blocking_reason, 0) + 1
            )
        if b.status == "resolved" and b.binding_source:
            summary["by_binding_source"][b.binding_source] = (
                summary["by_binding_source"].get(b.binding_source, 0) + 1
            )

    return {
        "format": "step_component_bindings",
        "schema_revision": 1,
        "artifact_metadata": {
            "artifact_id": "projections.step_component_bindings",
            "artifact_kind": "det_projection",
            "api_or_det": "det",
            "artifact_role": "projection",
            "logical_owner": "step_component_binding",
            "producer": "site_model.step_component_binding.build_step_component_bindings",
            "allowed_consumers": [
                "l0_execution_pack",
                "pagegen_dom_contract_validator",
                "replay_executor",
            ],
            "canonical_source": False,
            "canonical_semantic_source": False,
            "canonical_prompt_boundary": False,
        },
        "source": {
            "l0_execution_pack": "projections/l0_execution/l0_execution_pack.json",
            "page_environment_specs": "phase_page_environment/semantic_api/06_page_environment_specs/page_environment_specs.json",
        },
        "bindings": [b.to_dict() for b in bindings],
        "bindings_by_step": bindings_by_step,
        "summary": summary,
    }
