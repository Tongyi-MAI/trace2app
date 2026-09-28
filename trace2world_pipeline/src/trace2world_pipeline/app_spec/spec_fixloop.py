
from __future__ import annotations

import inspect
import json
import re
from typing import Any, Awaitable, Callable, Dict, Mapping, Tuple

from trace2world_pipeline.core.llm_caller import call_openai_api_json_async
from trace2world_pipeline.core.fixloop.json_patch import apply_json_patch_edits

from .spec_deterministic_normalizer import normalize_spec_deterministically
from .spec_executability_eval import evaluate_spec_executability
from .spec_issue_router import route_spec_executability_issues

SpecRepairer = Callable[[Dict[str, Any]], Dict[str, Any] | Awaitable[Dict[str, Any]]]
SpecExecutabilityEvaluator = Callable[[Mapping[str, Any]], Dict[str, Any]]

SPEC_PATCH_ALLOW_PATHS = ["/page_environment_specs_by_filename", "/specs"]
SPEC_PATCH_DENY_PATHS = [
    "/compiled_views",
    "/pagegen_spec_view_by_page",
    "/runtime_binding_manifest",
    "/contract_replay_plan",
    "/semantic_api",
]

SPEC_PATCH_REPAIR_PROMPT = """You repair Trace2World page_environment_specs before PageGen.

Return only JSON in this format:
{"edits":[{"op":"set|add|merge","path":"/json/pointer","value":...,"reason":"short reason"}]}

Rules:
- Patch only page_environment_specs paths listed in allowed_patch_paths.
- Do not edit HTML, PageGen output, runtime compiled views, replay plans, or semantic_api artifacts.
- Do not remove transitions or downgrade l0_role.
- Prefer minimal reducer/state_effect/effect_admission_status fixes grounded in weak_effect_candidates.
- For autocomplete issues: patch candidate_rows and expected_candidate_count in query_states/suggestion_contract. Do not generate fallback JS.
- For initial control state issues: patch initial_state.controls expected_checked/expected_value. Do not infer from selectedCount.
- If source evidence is insufficient to construct candidate rows or control state, return {"edits":[]}.
- If the issue cannot be repaired safely at spec level, return {"edits":[]}.
"""


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return text or "page"


_AUTOCOMPLETE_FIXLOOP_CODES = frozenset({
    "autocomplete_candidate_count_mismatch",
    "autocomplete_category_coverage_missing",
    "autocomplete_initial_visibility_wrong",
})

_INITIAL_CONTROL_FIXLOOP_CODES = frozenset({
    "initial_control_state_wrong",
})

_INITIAL_DOM_FIXLOOP_CODES = frozenset({
    "initial_dom_contract_missing",
    "initial_collection_oracle_missing",
    "initial_control_oracle_missing",
})


def _candidate_context(specs: Mapping[str, Any], router_report: Mapping[str, Any]) -> Dict[str, Any] | None:
    for route in _as_list(_as_dict(router_report).get("issue_routes")):
        if not isinstance(route, dict) or route.get("repair_class") != "spec_fixloop_candidate":
            continue
        page_filename = str(route.get("page_filename") or "")
        page_spec = _as_dict(_as_dict(specs.get("page_environment_specs_by_filename")).get(page_filename))
        code = _text(route.get("code"))
        patch_hint = ""
        if code in _AUTOCOMPLETE_FIXLOOP_CODES:
            patch_hint = (
                "Patch candidate_rows and expected_candidate_count in query_states.suggestion_contract. "
                "Do NOT generate fallback JS or PageGen hints. "
                "If source evidence is insufficient to construct rows, return empty edits."
            )
        elif code in _INITIAL_CONTROL_FIXLOOP_CODES:
            patch_hint = (
                "Patch initial_state.controls expected_checked/expected_value. "
                "Do NOT infer from selectedCount or group count. "
                "If source evidence contradicts, return empty edits."
            )
        elif code in _INITIAL_DOM_FIXLOOP_CODES:
            patch_hint = (
                "Patch initial_state to add missing DOM obligations: visible_sections, "
                "visible_lists (with expected_item_count), tabs (with selected), "
                "counters, absent_overlays. Do NOT patch HTML or compiled views. "
                "If source evidence is screenshot-only without structured data, return empty edits."
            )
        context: Dict[str, Any] = {
            "format": "spec_fixloop_repair_context",
            "schema_revision": 1,
            "repair_class": "spec_fixloop_candidate",
            "issue_route": route,
            "page_filename": page_filename,
            "page_spec": page_spec,
            "allowed_patch_paths": SPEC_PATCH_ALLOW_PATHS,
            "denied_patch_paths": SPEC_PATCH_DENY_PATHS,
            "patch_contract": {
                "format": {"edits": [{"op": "set|add|merge", "path": "/json/pointer", "value": "any"}]},
                "forbidden": ["html edits", "compiled views edits", "remove transition", "downgrade l0_role"],
            },
        }
        if patch_hint:
            context["patch_hint"] = patch_hint
        return context
    return None


def _weak_candidates(transition: Mapping[str, Any]) -> list[str]:
    state_effect = _as_dict(transition.get("state_effect"))
    candidates: list[str] = []
    for value in (transition.get("weak_effect_candidates"), state_effect.get("weak_effect_candidates")):
        if isinstance(value, list):
            candidates.extend(_text(item) for item in value if _text(item))
    return list(dict.fromkeys(candidates))


def _transition_status(transition: Mapping[str, Any]) -> str:
    state_effect = _as_dict(transition.get("state_effect"))
    return _text(
        transition.get("effect_admission_status")
        or state_effect.get("effect_admission_status")
        or transition.get("admission_status")
    )


def _transition_for_action(page_spec: Mapping[str, Any], action_id: str) -> tuple[int, Dict[str, Any]] | None:
    transitions = page_spec.get("transitions")
    if not isinstance(transitions, list):
        return None
    for index, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            continue
        step_key = _text(transition.get("step_key") or transition.get("transition_id"))
        if step_key == action_id:
            return index, transition
    return None


def _contains_ref(items: Any, state_ref: str, action_id: str = "") -> bool:
    text = json.dumps(items, ensure_ascii=False, sort_keys=True) if items is not None else ""
    return bool(state_ref and state_ref in text) or bool(action_id and action_id in text)


def _append_unique_runtime_state_ref(page_spec: Dict[str, Any], state_ref: str) -> None:
    state_schema = page_spec.setdefault("state_schema", {})
    if not isinstance(state_schema, dict):
        state_schema = {}
        page_spec["state_schema"] = state_schema
    refs = state_schema.setdefault("runtime_state_refs", [])
    if not isinstance(refs, list):
        refs = []
        state_schema["runtime_state_refs"] = refs
    if state_ref not in refs:
        refs.append(state_ref)


def _complete_filter_transaction_closure(
    current: Dict[str, Any],
    context: Mapping[str, Any],
) -> Dict[str, Any]:

    route = _as_dict(context.get("issue_route"))
    page_filename = _text(context.get("page_filename") or route.get("page_filename"))
    action_id = _text(route.get("action_id"))
    pages = _as_dict(current.get("page_environment_specs_by_filename"))
    page_spec = _as_dict(pages.get(page_filename))
    found = _transition_for_action(page_spec, action_id)
    if not found:
        return {"applied": False, "reason": "transition_not_found"}
    _index, transition = found
    state_effect = transition.setdefault("state_effect", {})
    if not isinstance(state_effect, dict):
        state_effect = {}
        transition["state_effect"] = state_effect

    candidates = set(_weak_candidates(transition))
    reducer = _text(transition.get("reducer") or transition.get("action_intent"))
    effect = _text(state_effect.get("effect") or state_effect.get("reducer"))
    if "filter_selection" not in candidates and reducer != "filter_selection" and effect != "filter_selection":
        return {"applied": False, "reason": "not_filter_selection"}
    if _transition_status(transition) not in {"accepted", "resolved"} and reducer != "filter_selection":
        return {"applied": False, "reason": "repair_not_accepted"}

    state_ref = _text(state_effect.get("state_ref"))
    if not state_ref.startswith("state::"):
        state_ref = f"state::{_slug(page_filename)}.filter"
    diff_key = f"{state_ref}.active"
    diff_value = _text(transition.get("operation_intent")) or "selected"

    transition["reducer"] = "filter_selection"
    transition["effect_admission_status"] = "accepted"
    state_effect["effect"] = "filter_selection"
    state_effect["effect_admission_status"] = "accepted"
    state_effect["state_ref"] = state_ref
    state_effect.setdefault("expected_state_diff", {diff_key: diff_value})
    state_effect.setdefault("state_diff", dict(state_effect["expected_state_diff"]))
    _append_unique_runtime_state_ref(page_spec, state_ref)

    render_projections = page_spec.setdefault("render_projections", [])
    if not isinstance(render_projections, list):
        render_projections = []
        page_spec["render_projections"] = render_projections
    if not _contains_ref(render_projections, state_ref):
        render_projections.append(
            {
                "projection_id": f"{action_id}::filter_render_projection",
                "source": "spec_fixloop_transaction_closure",
                "action_id": action_id,
                "reads_state_refs": [state_ref],
                "render_contract": "list/filter projection must read active filter state",
            }
        )

    oracles = page_spec.setdefault("oracles", [])
    if not isinstance(oracles, list):
        oracles = []
        page_spec["oracles"] = oracles
    if not _contains_ref(oracles, state_ref, action_id):
        oracles.append(
            {
                "oracle_id": f"{action_id}::filter_state_oracle",
                "source": "spec_fixloop_transaction_closure",
                "action_id": action_id,
                "state_refs": [state_ref],
                "expected_state_diff": dict(state_effect["expected_state_diff"]),
            }
        )

    return {
        "applied": True,
        "action_id": action_id,
        "page_filename": page_filename,
        "state_ref": state_ref,
    }


async def _call_repairer(repairer: SpecRepairer, context: Dict[str, Any]) -> Dict[str, Any]:
    result = repairer(context)
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, dict) else {}


async def call_llm_spec_patch_repairer(
    context: Dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_tokens: int = 4000,
) -> Dict[str, Any]:
    messages = [
        {"role": "system", "content": SPEC_PATCH_REPAIR_PROMPT},
        {
            "role": "user",
            "content": (
                "Repair context:\n"
                "```json\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2)}\n"
                "```"
            ),
        },
    ]
    result, _usage = await call_openai_api_json_async(
        messages,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        model=model,
        stage="spec_self_healing_fixloop",
    )
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return {"edits": []}
    if not isinstance(result, dict):
        return {"edits": []}
    patch = result.get("patch") if isinstance(result.get("patch"), dict) else result
    return patch if isinstance(patch, dict) else {"edits": []}


async def run_spec_self_healing_loop(
    page_environment_specs: Mapping[str, Any],
    *,
    max_rounds: int = 1,
    repairer: SpecRepairer | None = None,
    llm_repair_enabled: bool = False,
    model: str | None = None,
    reasoning_effort: str = "medium",
    max_tokens: int = 4000,
    executability_evaluator: SpecExecutabilityEvaluator | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:

    current: Dict[str, Any] = dict(page_environment_specs) if isinstance(page_environment_specs, dict) else {}
    eval_spec = executability_evaluator or evaluate_spec_executability
    initial_eval = eval_spec(current)
    rounds: list[Dict[str, Any]] = []

    for round_index in range(max(1, int(max_rounds or 1))):
        pre_eval = eval_spec(current)
        current_eval = pre_eval
        normalizer_passes: list[Dict[str, Any]] = []
        normalizer_operations: list[Dict[str, Any]] = []
        normalizer_applied_count = 0
        router_report: Dict[str, Any] = {}
        after_normalize_eval = pre_eval
        for deterministic_pass_index in range(8):
            router_report = route_spec_executability_issues(current_eval)
            before_summary = _as_dict(current_eval.get("summary"))
            current, pass_report = normalize_spec_deterministically(current, router_report)
            after_normalize_eval = eval_spec(current)
            pass_summary = _as_dict(pass_report.get("summary"))
            applied_count = int(pass_summary.get("applied_operation_count", 0) or 0)
            normalizer_applied_count += applied_count
            normalizer_operations.extend(
                item
                for item in pass_report.get("operations", [])
                if isinstance(item, dict)
            )
            normalizer_passes.append(
                {
                    "pass_index": deterministic_pass_index,
                    "pre_eval_summary": before_summary,
                    "router_summary": _as_dict(router_report.get("summary")),
                    "normalizer_summary": pass_summary,
                    "post_eval_summary": _as_dict(after_normalize_eval.get("summary")),
                }
            )
            current_eval = after_normalize_eval
            if applied_count <= 0:
                break
            if int(_as_dict(after_normalize_eval.get("summary")).get("blocking_issue_count", 0) or 0) == 0:
                break
        normalizer_report = {
            "format": "spec_deterministic_normalizer_report",
            "schema_revision": 1,
            "operations": normalizer_operations,
            "passes": normalizer_passes,
            "summary": {
                "applied_operation_count": normalizer_applied_count,
                "pass_count": len(normalizer_passes),
            },
        }
        round_report: Dict[str, Any] = {
            "round_index": round_index,
            "pre_eval_summary": _as_dict(pre_eval.get("summary")),
            "router_summary": _as_dict(router_report.get("summary")),
            "deterministic_normalizer": normalizer_report,
            "after_normalize_eval_summary": _as_dict(after_normalize_eval.get("summary")),
        }

        context = _candidate_context(current, route_spec_executability_issues(after_normalize_eval))
        if not context:
            round_report["stop_reason"] = "no_spec_fixloop_candidate"
            rounds.append(round_report)
            break
        if not llm_repair_enabled:
            round_report["stop_reason"] = "spec_patch_repairer_disabled"
            round_report["repair_context"] = context
            rounds.append(round_report)
            break

        if repairer is not None:
            patch = await _call_repairer(repairer, context)
            round_report["repairer_source"] = "injected_repairer"
        else:
            patch = await call_llm_spec_patch_repairer(
                context,
                model=model,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
            )
            round_report["repairer_source"] = "llm_json_patch"
        current, apply_report = apply_json_patch_edits(
            current,
            patch,
            allow_paths=SPEC_PATCH_ALLOW_PATHS,
            deny_paths=SPEC_PATCH_DENY_PATHS,
            target_id="page_environment_specs",
        )
        transaction_closure_report = (
            _complete_filter_transaction_closure(current, context)
            if apply_report.get("success")
            else {"applied": False, "reason": "patch_not_applied"}
        )
        post_patch_eval = eval_spec(current)
        round_report["repair_context"] = context
        round_report["patch_proposal"] = patch
        round_report["patch_apply_report"] = apply_report
        round_report["deterministic_transaction_closure"] = transaction_closure_report
        round_report["post_patch_eval_summary"] = _as_dict(post_patch_eval.get("summary"))
        rounds.append(round_report)
        if int(_as_dict(post_patch_eval.get("summary")).get("blocking_issue_count", 0) or 0) == 0:
            break

    final_eval = eval_spec(current)
    return current, {
        "format": "spec_self_healing_fixloop",
        "schema_revision": 1,
        "initial_eval": initial_eval,
        "final_eval": final_eval,
        "rounds": rounds,
        "summary": {
            "round_count": len(rounds),
            "initial_status": _as_dict(initial_eval.get("summary")).get("status", "unknown"),
            "final_status": _as_dict(final_eval.get("summary")).get("status", "unknown"),
            "initial_blocking_issue_count": int(_as_dict(initial_eval.get("summary")).get("blocking_issue_count", 0) or 0),
            "final_blocking_issue_count": int(_as_dict(final_eval.get("summary")).get("blocking_issue_count", 0) or 0),
            "initial_warning_issue_count": int(_as_dict(initial_eval.get("summary")).get("warning_issue_count", 0) or 0),
            "final_warning_issue_count": int(_as_dict(final_eval.get("summary")).get("warning_issue_count", 0) or 0),
        },
    }
