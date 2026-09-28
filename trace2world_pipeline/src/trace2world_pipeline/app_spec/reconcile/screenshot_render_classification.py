import asyncio
import copy
import os
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from trace2world_pipeline.core.step_keys import canonical_step_key, normalize_step_keyed_mapping
from trace2world_pipeline.app_spec.model import FrameObservation


ClusterJudge = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _cluster_scheduler_max_attempts() -> int:
    raw = os.getenv("SCREENSHOT_RENDER_CLASSIFICATION_CLUSTER_MAX_ATTEMPTS", "3")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 3


def _cluster_scheduler_max_steps() -> int:
    raw = os.getenv("SCREENSHOT_RENDER_CLASSIFICATION_CLUSTER_MAX_STEPS", "12")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 12

_RENDER_ROLES = {
    "canonical_layout",
    "post_interaction_overlay",
    "interaction_state_change",
    "runtime_status",
}

_STRUCTURAL_ROLES = {
    "base_shell",
    "overlay_shell",
    "state_variant",
    "async_status",
}

_LIFECYCLE_ROLES = {
    "page_initial",
    "trace_entry_initial",
    "runtime_pre_action_state",
    "post_interaction_overlay",
    "interaction_state_change",
    "runtime_status",
}

_STATE_DEPENDENCY_TOKENS = (
    "筛选",
    "过滤",
    "价格",
    "日期",
    "时间",
    "排序",
    "综合排序",
    "热度排序",
    "选择",
    "选中",
    "确认",
    "确定",
    "提交",
    "搜索",
    "输入",
    "tab",
    "filter",
    "sort",
    "price",
    "date",
    "select",
    "selected",
    "confirm",
    "submit",
    "query",
    "search",
)


class ScreenshotRenderClassificationError(ValueError):
    ...


def _obs_key(obs: FrameObservation) -> str:
    return canonical_step_key(f"{obs.trace_id}:{obs.step_index}")


def _step_key(label: Dict[str, Any]) -> str:
    return canonical_step_key(
        f"{str(label.get('trace_id', '')).strip()}:{int(label.get('step_index', -1))}"
    )


def _label_sort_key(label: Dict[str, Any]) -> Tuple[str, int]:
    trace_id = str(label.get("trace_id", "")).strip()
    try:
        step_index = int(label.get("step_index", -1))
    except Exception:
        step_index = -1
    return trace_id, step_index


def _compact_step(
    label: Dict[str, Any],
    obs: FrameObservation,
    *,
    previous_same_trace_steps: List[Dict[str, Any]] | None = None,
    outgoing_l0_obligation: bool = False,
) -> Dict[str, Any]:
    description = str(getattr(obs, "description", "") or "").strip()
    previous_steps = previous_same_trace_steps or []
    prior_signatures: List[str] = []
    for prior in previous_steps:
        prior_text = " ".join(
            str(prior.get(key) or "")
            for key in ("action", "thought", "description")
            if str(prior.get(key) or "").strip()
        )
        if any(token in prior_text.lower() for token in _STATE_DEPENDENCY_TOKENS):
            prior_signatures.append(
                f"{prior.get('step_key', '')}:{str(prior.get('action') or prior.get('description') or '')[:80]}"
            )
    has_prior_runtime_dependency = bool(prior_signatures)
    result = {
        "step_key": _step_key(label),
        "incoming_unique_label": bool(label.get("has_unique_page_structure", True)),
        "is_page_transition": bool(label.get("is_page_transition", True)),
        "structure_signature": str(getattr(obs, "structure_signature", "") or "").strip(),
        "content_signature": str(getattr(obs, "content_signature", "") or "").strip(),
        "header_pattern": str(getattr(obs, "header_pattern", "") or "").strip(),
        "body_pattern": str(getattr(obs, "body_pattern", "") or "").strip(),
        "sticky_action_pattern": str(getattr(obs, "sticky_action_pattern", "") or "").strip(),
        "description": description,
        "action": str(getattr(obs, "action", "") or "").strip(),
        "thought": str(getattr(obs, "thought", "") or "").strip(),
        "previous_same_trace_steps": [
            str(item.get("step_key") or "")
            for item in previous_steps
            if str(item.get("step_key") or "").strip()
        ],
        "prior_state_delta_signatures": prior_signatures,
        "state_preconditions_before_step": prior_signatures,
        "has_prior_runtime_dependency": has_prior_runtime_dependency,
        "is_trace_entry_on_page": not bool(previous_steps),
        "outgoing_l0_obligation": bool(outgoing_l0_obligation),
    }
    return result


def _cluster_payload(
    cluster_id: str,
    labels: List[Dict[str, Any]],
    obs_map: Dict[str, FrameObservation],
    page_by_cluster: Dict[str, Dict[str, Any]],
    task_actions_on_page: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    task_action_step_keys = {
        canonical_step_key(step_key)
        for item in (task_actions_on_page or [])
        if isinstance(item, dict)
        for step_key in _task_action_item_step_keys(item)
        if canonical_step_key(step_key)
    }
    steps: List[Dict[str, Any]] = []
    previous_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for label in sorted(labels, key=_label_sort_key):
        key = _step_key(label)
        obs = obs_map.get(key)
        if obs:
            trace_id = str(label.get("trace_id") or "").strip()
            compact = _compact_step(
                label,
                obs,
                previous_same_trace_steps=previous_by_trace.get(trace_id, []),
                outgoing_l0_obligation=key in task_action_step_keys,
            )
            steps.append(compact)
            previous_by_trace.setdefault(trace_id, []).append(compact)
    canonical_page = page_by_cluster.get(cluster_id, {})
    general_page_id = str(
        canonical_page.get("general_page_id")
        or canonical_page.get("source_page_cluster_id")
        or cluster_id
    ).strip()
    payload: Dict[str, Any] = {
        "format": "screenshot_render_classification_v2",
        "scope_type": "materialized_page",
        "general_page_id": general_page_id,
        "materialized_page_id": cluster_id,
        "page_cluster_id": cluster_id,
        "canonical_page": canonical_page,
        "ordered_steps": steps,
        "classification_step_keys": [step["step_key"] for step in steps],
        "classification_skeleton": _classification_skeleton(steps),
        "instructions": {
            "task": (
                "Mark which steps have a unique page structure within this Materialized Page, "
                "and classify each unique step's render role."
            ),
            "scope": "Only compare steps inside this materialized_page_id. page_cluster_id is a legacy alias for the same Materialized Page.",
            "language": "Write cluster_rationale and every reason field in Chinese.",
            "unique_principle": (
                "A step is unique when it introduces a new component, a new component layout, "
                "or a new relationship/composition among components within this Materialized Page."
            ),
            "decision_rubric": {
                "mark_unique_when": [
                    "A visible component appears or disappears and changes the page structure, not just item content.",
                    "The layout of existing components changes materially, such as a new sticky banner, modal, drawer, tab mode, filter bar, CTA region, or above-the-fold module arrangement.",
                    "The composition or parent-child relationship among components changes, such as category grid plus promo modules versus a different home-feed module stack.",
                    "For home/feed pages, a different named above-the-fold campaign/module family or module stack is a component variant and should be unique, even if the broad header/grid/feed shell is unchanged.",
                    "A task-relevant transient component changes available actions or blocks/redirects the workflow, such as an unpaid-order sticky prompt.",
                ],
                "mark_duplicate_when": [
                    "Only merchant names, product names, prices, thumbnails, query text, or ordinary list/card data changes.",
                    "Only scroll position changes while the same components and component relationships remain visible.",
                    "Only copywriting changes without adding/removing/rearranging components or named module families.",
                    "Different feed item content, card ordering, or recommendation results between traces when the component structure (grid, list, tabs, header, footer) is unchanged.",
                ],
            },
            "render_role_rubric": {
                "canonical_layout": (
                    "The page in its initial entry state with no overlays, modals, or post-interaction panels visible. "
                    "An outgoing L0 obligation only proves action_host_authority for that source step; "
                    "it does not make the screenshot initial-DOM eligible by itself. "
                    "If has_prior_runtime_dependency=true, the screenshot is a runtime_pre_action_state, not canonical initial DOM, "
                    "unless an explicit trace entry initial-state contract says otherwise. "
                    "When no L0 obligation differentiates candidates, pick the cleanest entry."
                ),
                "post_interaction_overlay": (
                    "A modal, sheet, drawer, panel, or overlay appeared on top of the base layout "
                    "as a result of a user action. Initially hidden in DOM."
                ),
                "interaction_state_change": (
                    "A visible component changed internal state (selection highlighted, input filled, "
                    "rows hidden/shown, tab switched, header collapsed) due to a user action."
                ),
                "runtime_status": (
                    "An asynchronous status indicator (upload progress, toast, banner) "
                    "not triggered by a direct UI click."
                ),
            },
            "schema_constraints": [
                "Return classifications as an object keyed by the exact ordered_steps[].step_key values.",
                "Do not create, delete, rename, or reorder classification keys.",
                "Every ordered_steps[].step_key must appear exactly once in classifications.",
                "classification must be either unique or duplicate.",
                "Every duplicate classification must include duplicate_of referencing a unique step_key.",
                "Every unique classification render_role must be one of: canonical_layout, post_interaction_overlay, interaction_state_change, runtime_status.",
                "Every unique classification must include structural_role, lifecycle_role, initial_dom_eligible, and action_host_authority.",
                "If ordered_steps[].has_prior_runtime_dependency is true, initial_dom_eligible must be false and lifecycle_role must not be page_initial or trace_entry_initial.",
            ],
            "do_not": [
                "Do not create, rename, merge, or split General Pages or Materialized Pages.",
                "Do not mark ordinary feed content, scroll position, merchant/card data, or query text changes as unique structure by themselves unless they introduce a new component, component layout, or component composition.",
                "Do not use incoming_unique_label as ground truth; it is only provenance.",
                "Do not classify a step-1 screenshot as interaction_state_change solely because another trace has a 'cleaner' page-default baseline with different feed items or card content. Different feed/card content at entry is a content variant of canonical_layout, not a structural state change.",
                "Do not classify a step screenshot with an outgoing L0 obligation (listed in task_actions_on_page) as initial-DOM authority only because it is an action source. The outgoing action is what the user WILL do, not proof of global initial state.",
                "Do not mark prior-runtime-dependent source screenshots as initial_dom_eligible.",
            ],
            "output_schema": {
                "classifications": {
                    "trace_id:step_index": {
                        "classification": "unique | duplicate",
                        "reason": "Chinese reason for this exact step_key",
                        "render_role": "canonical_layout | post_interaction_overlay | interaction_state_change | runtime_status; required for unique",
                        "structural_role": "base_shell | overlay_shell | state_variant | async_status; required for unique",
                        "lifecycle_role": "page_initial | trace_entry_initial | runtime_pre_action_state | post_interaction_overlay | interaction_state_change | runtime_status; required for unique",
                        "initial_dom_eligible": "boolean; false when has_prior_runtime_dependency=true",
                        "action_host_authority": "boolean; true when this screenshot provides the source host for an outgoing L0 obligation",
                        "state_preconditions": "array of strings copied or summarized from ordered_steps[].state_preconditions_before_step",
                        "duplicate_of": "trace_id:step_index; required for duplicate and must reference a unique step",
                    }
                },
                "cluster_rationale": "short cluster-level summary",
            },
        },
    }
    if task_actions_on_page:
        payload["task_actions_on_page"] = task_actions_on_page
    return payload


def _classification_skeleton(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "classifications": {
            str(step.get("step_key", "")).strip(): {
                "classification": "unique | duplicate",
                "reason": "用中文解释该 step_key 为什么是唯一结构或重复结构。",
                "render_role": "canonical_layout | post_interaction_overlay | interaction_state_change | runtime_status; unique 时必填。",
                "structural_role": "base_shell | overlay_shell | state_variant | async_status; unique 时必填。",
                "lifecycle_role": "page_initial | trace_entry_initial | runtime_pre_action_state | post_interaction_overlay | interaction_state_change | runtime_status; unique 时必填。",
                "initial_dom_eligible": "boolean；当该 step 有 prior runtime dependency 时必须为 false。",
                "action_host_authority": "boolean；当该 screenshot 只提供 L0 source host authority 时为 true。",
                "state_preconditions": "array of strings；复制或概括 ordered_steps[].state_preconditions_before_step。",
                "duplicate_of": "duplicate 时必填，且必须引用 classification=unique 的 step_key。",
            }
            for step in steps
            if str(step.get("step_key", "")).strip()
        },
        "cluster_rationale": "用中文概述这个 Materialized Page 内结构去重的理由。",
    }


def classification_json_schema_for_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    step_keys = [
        canonical_step_key(key)
        for key in payload.get("classification_step_keys", []) or []
        if canonical_step_key(key)
    ]
    if not step_keys:
        step_keys = _ordered_step_keys(payload)
    classification_item_schema = {
        "type": "object",
        "properties": {
            "classification": {"type": "string", "enum": ["unique", "duplicate"]},
            "reason": {"type": "string"},
            "render_role": {
                "type": "string",
                "enum": [
                    "",
                    "canonical_layout",
                    "post_interaction_overlay",
                    "interaction_state_change",
                    "runtime_status",
                ],
            },
            "structural_role": {
                "type": "string",
                "enum": [
                    "",
                    "base_shell",
                    "overlay_shell",
                    "state_variant",
                    "async_status",
                ],
            },
            "lifecycle_role": {
                "type": "string",
                "enum": [
                    "",
                    "page_initial",
                    "trace_entry_initial",
                    "runtime_pre_action_state",
                    "post_interaction_overlay",
                    "interaction_state_change",
                    "runtime_status",
                ],
            },
            "initial_dom_eligible": {"type": "boolean"},
            "action_host_authority": {"type": "boolean"},
            "state_preconditions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "duplicate_of": {"type": "string"},
        },
        "required": [
            "classification",
            "reason",
            "render_role",
            "structural_role",
            "lifecycle_role",
            "initial_dom_eligible",
            "action_host_authority",
            "state_preconditions",
            "duplicate_of",
        ],
        "additionalProperties": False,
    }
    classification_properties = {
        key: copy.deepcopy(classification_item_schema)
        for key in step_keys
    }
    return {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "object",
                "properties": classification_properties,
                "required": step_keys,
                "additionalProperties": False,
            },
            "cluster_rationale": {"type": "string"},
        },
        "required": ["classifications", "cluster_rationale"],
        "additionalProperties": False,
    }


def _ordered_step_keys(payload: Dict[str, Any]) -> List[str]:
    result: List[str] = []
    for step in payload.get("ordered_steps", []) or []:
        if not isinstance(step, dict):
            continue
        key = canonical_step_key(step.get("step_key", ""))
        if key:
            result.append(key)
    return result


def _task_action_item_step_keys(item: Dict[str, Any]) -> set[str]:
    trace_id = str(item.get("trace_id") or "").strip()
    if not trace_id:
        return set()
    raw_steps: List[Any] = []
    if item.get("step_key"):
        raw_steps.append(item.get("step_key"))
    for field in ("entry_step", "exit_step"):
        if item.get(field):
            raw_steps.append(item.get(field))
    steps_on_page = item.get("steps_on_page")
    if isinstance(steps_on_page, list):
        raw_steps.extend(steps_on_page)

    result: set[str] = set()
    for raw_step in raw_steps:
        step_text = str(raw_step or "").strip()
        if not step_text:
            continue
        if ":" in step_text:
            key = canonical_step_key(step_text)
        else:
            key = canonical_step_key(f"{trace_id}:{step_text}")
        if key:
            result.add(key)
    return result


def _split_cluster_payload(payload: Dict[str, Any], max_steps: int) -> List[Dict[str, Any]]:
    ordered_steps = [
        copy.deepcopy(step)
        for step in payload.get("ordered_steps", []) or []
        if isinstance(step, dict)
    ]
    if len(ordered_steps) <= max_steps:
        return [payload]

    batches: List[Dict[str, Any]] = []
    batch_count = (len(ordered_steps) + max_steps - 1) // max_steps
    for batch_index, start in enumerate(range(0, len(ordered_steps), max_steps), start=1):
        batch_steps = ordered_steps[start:start + max_steps]
        batch_step_keys = [
            canonical_step_key(step.get("step_key", ""))
            for step in batch_steps
            if canonical_step_key(step.get("step_key", ""))
        ]
        batch = copy.deepcopy(payload)
        batch["ordered_steps"] = batch_steps
        batch["classification_step_keys"] = batch_step_keys
        batch["classification_skeleton"] = _classification_skeleton(batch_steps)
        batch["batch_context"] = {
            "batch_index": batch_index,
            "batch_count": batch_count,
            "batch_step_count": len(batch_steps),
            "batch_step_keys": batch_step_keys,
            "full_ordered_step_count": len(ordered_steps),
            "instruction": (
                "This is a bounded sub-batch of one Materialized Page. Classify every "
                "batch step key exactly once. Do not infer or emit keys outside this batch."
            ),
        }
        task_actions = payload.get("task_actions_on_page")
        if isinstance(task_actions, list):
            batch_key_set = set(batch_step_keys)
            batch["task_actions_on_page"] = [
                copy.deepcopy(item)
                for item in task_actions
                if isinstance(item, dict) and (_task_action_item_step_keys(item) & batch_key_set)
            ]
        batches.append(batch)
    return batches


def _step_reason_entries(
    result: Dict[str, Any],
    field: str,
    *,
    require_duplicate_of: bool = False,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    entries = result.get(field, [])
    if not isinstance(entries, list):
        raise ScreenshotRenderClassificationError(f"Screenshot render classification result field {field} must be a list")

    by_key: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            errors.append(f"{field}[{index}] must be an object")
            continue
        step_key = canonical_step_key(item.get("step_key", ""))
        reason = str(item.get("reason", "") or "").strip()
        duplicate_of = canonical_step_key(item.get("duplicate_of", ""))
        if not step_key:
            errors.append(f"{field}[{index}] missing step_key")
            continue
        if not reason:
            errors.append(f"{field}[{index}] missing reason")
        if require_duplicate_of and not duplicate_of:
            errors.append(f"{field}[{index}] missing duplicate_of")
        if step_key in by_key:
            existing = by_key[step_key]
            if reason and not existing.get("reason"):
                existing["reason"] = reason
            if duplicate_of and not existing.get("duplicate_of"):
                existing["duplicate_of"] = duplicate_of
            continue
        entry: Dict[str, str] = {
            "step_key": step_key,
            "reason": reason,
        }
        render_role = str(item.get("render_role", "") or "").strip()
        if render_role:
            entry["render_role"] = render_role
        if duplicate_of:
            entry["duplicate_of"] = duplicate_of
        by_key[step_key] = entry
    return by_key, errors


def _keyed_classification_entries(
    result: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], List[str]]:
    raw = result.get("classifications")
    if raw is None:
        return {}, {}, []
    if not isinstance(raw, dict):
        return {}, {}, ["classifications must be an object keyed by step_key"]

    unique_steps: Dict[str, Dict[str, Any]] = {}
    duplicate_steps: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    for raw_key, item in raw.items():
        step_key = canonical_step_key(raw_key)
        if not step_key:
            errors.append("classifications contains an empty step_key")
            continue
        if not isinstance(item, dict):
            errors.append(f"classifications[{step_key}] must be an object")
            continue
        classification = str(item.get("classification", "") or "").strip().lower()
        reason = str(item.get("reason", "") or "").strip()
        render_role = str(item.get("render_role", "") or "").strip()
        structural_role = str(item.get("structural_role", "") or "").strip()
        lifecycle_role = str(item.get("lifecycle_role", "") or "").strip()
        initial_dom_eligible = item.get("initial_dom_eligible")
        action_host_authority = item.get("action_host_authority")
        state_preconditions = item.get("state_preconditions", [])
        duplicate_of = canonical_step_key(item.get("duplicate_of", ""))
        if classification not in {"unique", "duplicate"}:
            errors.append(f"classifications[{step_key}] classification must be unique or duplicate")
            continue
        if not reason:
            errors.append(f"classifications[{step_key}] missing reason")
        if classification == "unique":
            if render_role and render_role not in _RENDER_ROLES:
                errors.append(f"classifications[{step_key}] invalid render_role {render_role}")
            if structural_role and structural_role not in _STRUCTURAL_ROLES:
                errors.append(f"classifications[{step_key}] invalid structural_role {structural_role}")
            if lifecycle_role and lifecycle_role not in _LIFECYCLE_ROLES:
                errors.append(f"classifications[{step_key}] invalid lifecycle_role {lifecycle_role}")
            if initial_dom_eligible is not None and not isinstance(initial_dom_eligible, bool):
                errors.append(f"classifications[{step_key}] initial_dom_eligible must be boolean")
            if action_host_authority is not None and not isinstance(action_host_authority, bool):
                errors.append(f"classifications[{step_key}] action_host_authority must be boolean")
            if state_preconditions is not None and not isinstance(state_preconditions, list):
                errors.append(f"classifications[{step_key}] state_preconditions must be an array")
            unique_steps[step_key] = {
                "step_key": step_key,
                "reason": reason,
                "render_role": render_role or "canonical_layout",
                "structural_role": structural_role,
                "lifecycle_role": lifecycle_role,
                "initial_dom_eligible": initial_dom_eligible,
                "action_host_authority": action_host_authority,
                "state_preconditions": state_preconditions if isinstance(state_preconditions, list) else [],
                "classification": "unique",
            }
        else:
            if not duplicate_of:
                errors.append(f"classifications[{step_key}] missing duplicate_of")
            if render_role and render_role not in _RENDER_ROLES:
                errors.append(f"classifications[{step_key}] invalid render_role {render_role}")
            if structural_role and structural_role not in _STRUCTURAL_ROLES:
                errors.append(f"classifications[{step_key}] invalid structural_role {structural_role}")
            if lifecycle_role and lifecycle_role not in _LIFECYCLE_ROLES:
                errors.append(f"classifications[{step_key}] invalid lifecycle_role {lifecycle_role}")
            if initial_dom_eligible is not None and not isinstance(initial_dom_eligible, bool):
                errors.append(f"classifications[{step_key}] initial_dom_eligible must be boolean")
            if action_host_authority is not None and not isinstance(action_host_authority, bool):
                errors.append(f"classifications[{step_key}] action_host_authority must be boolean")
            if state_preconditions is not None and not isinstance(state_preconditions, list):
                errors.append(f"classifications[{step_key}] state_preconditions must be an array")
            duplicate_steps[step_key] = {
                "step_key": step_key,
                "reason": reason,
                "duplicate_of": duplicate_of,
                "render_role": render_role,
                "structural_role": structural_role,
                "lifecycle_role": lifecycle_role,
                "initial_dom_eligible": initial_dom_eligible,
                "action_host_authority": action_host_authority,
                "state_preconditions": state_preconditions if isinstance(state_preconditions, list) else [],
                "classification": "duplicate",
            }
    return unique_steps, duplicate_steps, errors


def _classification_entries(
    result: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], List[str], str]:
    if "classifications" in result:
        unique_steps, duplicate_steps, errors = _keyed_classification_entries(result)
        return unique_steps, duplicate_steps, errors, "keyed_classifications"
    unique_steps, unique_errors = _step_reason_entries(result, "unique_steps")
    duplicate_steps, duplicate_errors = _step_reason_entries(
        result,
        "duplicate_steps",
        require_duplicate_of=True,
    )
    return unique_steps, duplicate_steps, unique_errors + duplicate_errors, "legacy_lists"


def _classification_validation_details(
    payload: Dict[str, Any],
    result: Any,
) -> Dict[str, Any]:
    expected_step_keys = _ordered_step_keys(payload)
    expected = set(expected_step_keys)
    provided: set[str] = set()
    if isinstance(result, dict):
        raw_classifications = result.get("classifications")
        if isinstance(raw_classifications, dict):
            provided.update(
                canonical_step_key(key)
                for key in raw_classifications
                if canonical_step_key(key)
            )
        for field in ("unique_steps", "duplicate_steps"):
            raw_entries = result.get(field)
            if isinstance(raw_entries, list):
                for item in raw_entries:
                    if isinstance(item, dict):
                        key = canonical_step_key(item.get("step_key", ""))
                        if key:
                            provided.add(key)
    return {
        "expected_step_keys": expected_step_keys,
        "provided_step_keys": sorted(provided),
        "missing_step_keys": sorted(expected - provided),
        "invalid_step_keys": sorted(provided - expected),
        "classification_skeleton": payload.get("classification_skeleton") or _classification_skeleton(
            payload.get("ordered_steps", []) or []
        ),
    }


def _task_action_source_step_keys(payload: Dict[str, Any]) -> set:
    result = set()
    for item in payload.get("task_actions_on_page", []) or []:
        if not isinstance(item, dict):
            continue
        result.update(_task_action_item_step_keys(item))
    return result


def _step_context_by_key(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    contexts: Dict[str, Dict[str, Any]] = {}
    for step in payload.get("ordered_steps", []) or []:
        if not isinstance(step, dict):
            continue
        key = canonical_step_key(step.get("step_key", ""))
        if key:
            contexts[key] = step
    return contexts


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _infer_structural_role(render_role: str) -> str:
    if render_role == "post_interaction_overlay":
        return "overlay_shell"
    if render_role == "interaction_state_change":
        return "state_variant"
    if render_role == "runtime_status":
        return "async_status"
    return "base_shell"


def _infer_lifecycle_role(render_role: str, context: Dict[str, Any], supplied: str = "") -> str:
    if supplied in _LIFECYCLE_ROLES:
        return supplied
    if bool(context.get("has_prior_runtime_dependency")):
        return "runtime_pre_action_state"
    if render_role in {"post_interaction_overlay", "interaction_state_change", "runtime_status"}:
        return render_role
    if bool(context.get("is_trace_entry_on_page")):
        return "trace_entry_initial"
    return "page_initial"


def _normalize_state_preconditions(value: Any, context: Dict[str, Any]) -> List[str]:
    raw_values: List[Any] = []
    if isinstance(value, list):
        raw_values = value
    elif value not in (None, ""):
        raw_values = [value]
    result = [
        str(item).strip()
        for item in raw_values
        if str(item).strip()
    ]
    if result:
        return result
    return [
        str(item).strip()
        for item in context.get("state_preconditions_before_step", []) or []
        if str(item).strip()
    ]


def _normalized_lifecycle_for_step(
    *,
    cluster_id: str,
    step_key: str,
    item: Dict[str, Any],
    context: Dict[str, Any],
    render_role: str,
) -> Dict[str, Any]:
    structural_role = str(item.get("structural_role") or "").strip()
    if structural_role and structural_role not in _STRUCTURAL_ROLES:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} returned invalid structural_role for {step_key}: {structural_role}"
        )
    if not structural_role:
        structural_role = _infer_structural_role(render_role)

    lifecycle_role = _infer_lifecycle_role(
        render_role,
        context,
        str(item.get("lifecycle_role") or "").strip(),
    )
    supplied_initial = _bool_or_none(item.get("initial_dom_eligible"))
    if supplied_initial is None:
        initial_dom_eligible = (
            render_role == "canonical_layout"
            and lifecycle_role in {"page_initial", "trace_entry_initial"}
            and not bool(context.get("has_prior_runtime_dependency"))
        )
    else:
        initial_dom_eligible = supplied_initial

    if bool(context.get("has_prior_runtime_dependency")):
        if initial_dom_eligible or lifecycle_role in {"page_initial", "trace_entry_initial"}:
            raise ScreenshotRenderClassificationError(
                f"Screenshot render classification judge for {cluster_id} marked prior-runtime-dependent {step_key} as initial DOM"
            )

    supplied_action_host = _bool_or_none(item.get("action_host_authority"))
    action_host_authority = bool(supplied_action_host) if supplied_action_host is not None else bool(
        context.get("outgoing_l0_obligation")
    )
    state_preconditions = _normalize_state_preconditions(
        item.get("state_preconditions", []),
        context,
    )
    return {
        "structural_role": structural_role,
        "lifecycle_role": lifecycle_role,
        "initial_dom_eligible": bool(initial_dom_eligible),
        "action_host_authority": bool(action_host_authority),
        "state_preconditions": state_preconditions,
    }


def _normalize_judge_result(
    cluster_id: str,
    payload: Dict[str, Any],
    result: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, bool]]:
    if not isinstance(result, dict):
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} returned non-object result"
        )

    valid_step_keys = _ordered_step_keys(payload)
    valid_keys = set(valid_step_keys)
    unique_steps, duplicate_steps, parse_errors, result_format = _classification_entries(result)
    if parse_errors:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} returned invalid structured reasons: {parse_errors}"
        )

    unique_keys = set(unique_steps)
    duplicate_keys = set(duplicate_steps)

    invalid_keys = sorted((unique_keys | duplicate_keys) - valid_keys)
    if invalid_keys:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} returned invalid step keys: {invalid_keys}"
        )
    overlap = sorted(unique_keys & duplicate_keys)
    if overlap:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} returned overlapping step keys: {overlap}"
        )
    missing_keys = sorted(valid_keys - unique_keys - duplicate_keys)
    if missing_keys:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} omitted step keys: {missing_keys}"
        )
    if valid_keys and not unique_keys:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} returned no unique step"
        )

    invalid_duplicate_refs = sorted({
        item.get("duplicate_of", "")
        for item in duplicate_steps.values()
        if item.get("duplicate_of", "") not in unique_keys
    })
    if invalid_duplicate_refs:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification judge for {cluster_id} returned duplicate_of references outside unique_steps: {invalid_duplicate_refs}"
        )

    updates = {key: (key in unique_keys) for key in valid_keys}
    contexts = _step_context_by_key(payload)
    render_role_by_step: Dict[str, str] = {}
    for key in unique_keys:
        render_role = unique_steps[key].get("render_role", "canonical_layout")
        if render_role not in _RENDER_ROLES:
            raise ScreenshotRenderClassificationError(
                f"Screenshot render classification judge for {cluster_id} returned invalid render_role for {key}: {render_role}"
            )
        render_role_by_step[key] = render_role
    for key in duplicate_keys:
        dup_of = duplicate_steps[key].get("duplicate_of", "")
        render_role_by_step[key] = render_role_by_step.get(dup_of, "canonical_layout")

    structural_role_by_step: Dict[str, str] = {}
    lifecycle_role_by_step: Dict[str, str] = {}
    initial_dom_eligible_by_step: Dict[str, bool] = {}
    action_host_authority_by_step: Dict[str, bool] = {}
    state_preconditions_by_step: Dict[str, List[str]] = {}
    for key in valid_step_keys:
        source_item = unique_steps.get(key) or duplicate_steps.get(key) or {}
        context = contexts.get(key, {})
        lifecycle = _normalized_lifecycle_for_step(
            cluster_id=cluster_id,
            step_key=key,
            item=source_item,
            context=context,
            render_role=render_role_by_step.get(key, "canonical_layout"),
        )
        structural_role_by_step[key] = lifecycle["structural_role"]
        lifecycle_role_by_step[key] = lifecycle["lifecycle_role"]
        initial_dom_eligible_by_step[key] = bool(lifecycle["initial_dom_eligible"])
        action_host_authority_by_step[key] = bool(lifecycle["action_host_authority"])
        state_preconditions_by_step[key] = list(lifecycle.get("state_preconditions", []) or [])

    task_action_step_keys = _task_action_source_step_keys(payload)
    initial_authority_kind_by_step: Dict[str, str] = {}
    for key in sorted(task_action_step_keys & valid_keys):
        initial_authority_kind_by_step[key] = "l0_source"
    render_role_by_step = normalize_step_keyed_mapping(render_role_by_step)
    initial_authority_kind_by_step = normalize_step_keyed_mapping(initial_authority_kind_by_step)
    classification_matrix: Dict[str, Dict[str, Any]] = {}
    for key in valid_step_keys:
        if key in unique_steps:
            classification_matrix[key] = {
                "classification": "unique",
                "reason": unique_steps[key].get("reason", ""),
                "render_role": render_role_by_step.get(key, "canonical_layout"),
                "structural_role": structural_role_by_step.get(key, ""),
                "lifecycle_role": lifecycle_role_by_step.get(key, ""),
                "initial_dom_eligible": initial_dom_eligible_by_step.get(key, False),
                "action_host_authority": action_host_authority_by_step.get(key, False),
                "state_preconditions": state_preconditions_by_step.get(key, []),
            }
        elif key in duplicate_steps:
            classification_matrix[key] = {
                "classification": "duplicate",
                "duplicate_of": duplicate_steps[key].get("duplicate_of", ""),
                "reason": duplicate_steps[key].get("reason", ""),
                "render_role": render_role_by_step.get(key, "canonical_layout"),
                "structural_role": structural_role_by_step.get(key, ""),
                "lifecycle_role": lifecycle_role_by_step.get(key, ""),
                "initial_dom_eligible": initial_dom_eligible_by_step.get(key, False),
                "action_host_authority": action_host_authority_by_step.get(key, False),
                "state_preconditions": state_preconditions_by_step.get(key, []),
            }

    debug = {
        "ordered_step_count": len(valid_step_keys),
        "classified_step_count": len(unique_keys | duplicate_keys),
        "missing_step_keys": [],
        "invalid_step_keys": [],
        "result_format": result_format,
        "classification_matrix": classification_matrix,
        "step_count": len(valid_keys),
        "unique_count": len(unique_keys),
        "duplicate_count": len(duplicate_keys),
        "unique_step_keys": sorted(unique_keys),
        "duplicate_step_keys": sorted(duplicate_keys),
        "unique_steps": [unique_steps[key] for key in sorted(unique_keys)],
        "duplicate_steps": [duplicate_steps[key] for key in sorted(duplicate_keys)],
        "unique_step_reasons": {
            key: unique_steps[key]["reason"]
            for key in sorted(unique_keys)
        },
        "duplicate_step_reasons": {
            key: {
                "duplicate_of": duplicate_steps[key].get("duplicate_of", ""),
                "reason": duplicate_steps[key]["reason"],
            }
            for key in sorted(duplicate_keys)
        },
        "render_role_by_step": render_role_by_step,
        "structural_role_by_step": normalize_step_keyed_mapping(structural_role_by_step),
        "lifecycle_role_by_step": normalize_step_keyed_mapping(lifecycle_role_by_step),
        "initial_dom_eligible_by_step": normalize_step_keyed_mapping(initial_dom_eligible_by_step),
        "action_host_authority_by_step": normalize_step_keyed_mapping(action_host_authority_by_step),
        "state_preconditions_by_step": normalize_step_keyed_mapping(state_preconditions_by_step),
        "screenshot_lifecycle_by_step": normalize_step_keyed_mapping({
            key: {
                "structural_role": structural_role_by_step.get(key, ""),
                "lifecycle_role": lifecycle_role_by_step.get(key, ""),
                "initial_dom_eligible": initial_dom_eligible_by_step.get(key, False),
                "action_host_authority": action_host_authority_by_step.get(key, False),
                "state_preconditions": state_preconditions_by_step.get(key, []),
            }
            for key in valid_step_keys
        }),
        "initial_authority_kind_by_step": initial_authority_kind_by_step,
        "cluster_rationale": str(result.get("cluster_rationale", "") or "").strip(),
        "raw_result": result,
    }
    return debug, updates


def _merge_batched_cluster_results(
    cluster_id: str,
    original_payload: Dict[str, Any],
    batch_results: List[Tuple[int, Dict[str, Any], Dict[str, Any], Dict[str, bool]]],
    *,
    max_steps: int,
) -> Tuple[Dict[str, Any], Dict[str, bool]]:
    expected_step_keys = _ordered_step_keys(original_payload)
    expected = set(expected_step_keys)
    updates: Dict[str, bool] = {}
    classification_matrix: Dict[str, Dict[str, str]] = {}
    render_role_by_step: Dict[str, str] = {}
    structural_role_by_step: Dict[str, str] = {}
    lifecycle_role_by_step: Dict[str, str] = {}
    initial_dom_eligible_by_step: Dict[str, bool] = {}
    action_host_authority_by_step: Dict[str, bool] = {}
    state_preconditions_by_step: Dict[str, List[str]] = {}
    screenshot_lifecycle_by_step: Dict[str, Dict[str, Any]] = {}
    initial_authority_kind_by_step: Dict[str, str] = {}
    unique_step_reasons: Dict[str, str] = {}
    duplicate_step_reasons: Dict[str, Dict[str, str]] = {}
    unique_steps: List[Dict[str, str]] = []
    duplicate_steps: List[Dict[str, str]] = []
    unique_step_keys: List[str] = []
    duplicate_step_keys: List[str] = []
    cluster_rationales: List[str] = []
    batch_debug_entries: List[Dict[str, Any]] = []

    for batch_index, batch_payload, batch_debug, batch_updates in batch_results:
        batch_step_keys = _ordered_step_keys(batch_payload)
        updates.update(batch_updates)
        classification_matrix.update(batch_debug.get("classification_matrix", {}) or {})
        render_role_by_step.update(batch_debug.get("render_role_by_step", {}) or {})
        structural_role_by_step.update(batch_debug.get("structural_role_by_step", {}) or {})
        lifecycle_role_by_step.update(batch_debug.get("lifecycle_role_by_step", {}) or {})
        initial_dom_eligible_by_step.update(batch_debug.get("initial_dom_eligible_by_step", {}) or {})
        action_host_authority_by_step.update(batch_debug.get("action_host_authority_by_step", {}) or {})
        state_preconditions_by_step.update(batch_debug.get("state_preconditions_by_step", {}) or {})
        screenshot_lifecycle_by_step.update(batch_debug.get("screenshot_lifecycle_by_step", {}) or {})
        initial_authority_kind_by_step.update(batch_debug.get("initial_authority_kind_by_step", {}) or {})
        unique_step_reasons.update(batch_debug.get("unique_step_reasons", {}) or {})
        duplicate_step_reasons.update(batch_debug.get("duplicate_step_reasons", {}) or {})
        unique_steps.extend(batch_debug.get("unique_steps", []) or [])
        duplicate_steps.extend(batch_debug.get("duplicate_steps", []) or [])
        unique_step_keys.extend(batch_debug.get("unique_step_keys", []) or [])
        duplicate_step_keys.extend(batch_debug.get("duplicate_step_keys", []) or [])
        rationale = str(batch_debug.get("cluster_rationale", "") or "").strip()
        if rationale:
            cluster_rationales.append(rationale)
        batch_debug_entries.append({
            "batch_index": batch_index,
            "batch_step_keys": batch_step_keys,
            "ordered_step_count": len(batch_step_keys),
            "classified_step_count": int(batch_debug.get("classified_step_count", 0) or 0),
            "unique_count": int(batch_debug.get("unique_count", 0) or 0),
            "duplicate_count": int(batch_debug.get("duplicate_count", 0) or 0),
            "cluster_attempt": int(batch_debug.get("cluster_attempt", 1) or 1),
            "retry_context": batch_debug.get("retry_context"),
        })

    provided = set(updates)
    missing = sorted(expected - provided)
    invalid = sorted(provided - expected)
    if missing or invalid:
        raise ScreenshotRenderClassificationError(
            f"Screenshot render classification batched merge for {cluster_id} has "
            f"missing step keys {missing} and invalid step keys {invalid}"
        )

    unique_step_keys = sorted(set(unique_step_keys), key=expected_step_keys.index)
    duplicate_step_keys = sorted(set(duplicate_step_keys), key=expected_step_keys.index)
    unique_steps = sorted(
        unique_steps,
        key=lambda item: expected_step_keys.index(str(item.get("step_key", ""))),
    )
    duplicate_steps = sorted(
        duplicate_steps,
        key=lambda item: expected_step_keys.index(str(item.get("step_key", ""))),
    )
    debug = {
        "ordered_step_count": len(expected_step_keys),
        "classified_step_count": len(provided),
        "missing_step_keys": [],
        "invalid_step_keys": [],
        "result_format": "batched_keyed_classifications",
        "classification_matrix": {
            key: classification_matrix[key]
            for key in expected_step_keys
            if key in classification_matrix
        },
        "step_count": len(expected_step_keys),
        "unique_count": len(unique_step_keys),
        "duplicate_count": len(duplicate_step_keys),
        "unique_step_keys": unique_step_keys,
        "duplicate_step_keys": duplicate_step_keys,
        "unique_steps": unique_steps,
        "duplicate_steps": duplicate_steps,
        "unique_step_reasons": {
            key: unique_step_reasons[key]
            for key in expected_step_keys
            if key in unique_step_reasons
        },
        "duplicate_step_reasons": {
            key: duplicate_step_reasons[key]
            for key in expected_step_keys
            if key in duplicate_step_reasons
        },
        "render_role_by_step": normalize_step_keyed_mapping(render_role_by_step),
        "structural_role_by_step": normalize_step_keyed_mapping(structural_role_by_step),
        "lifecycle_role_by_step": normalize_step_keyed_mapping(lifecycle_role_by_step),
        "initial_dom_eligible_by_step": normalize_step_keyed_mapping(initial_dom_eligible_by_step),
        "action_host_authority_by_step": normalize_step_keyed_mapping(action_host_authority_by_step),
        "state_preconditions_by_step": normalize_step_keyed_mapping(state_preconditions_by_step),
        "screenshot_lifecycle_by_step": normalize_step_keyed_mapping(screenshot_lifecycle_by_step),
        "initial_authority_kind_by_step": normalize_step_keyed_mapping(initial_authority_kind_by_step),
        "cluster_rationale": "；".join(cluster_rationales),
        "raw_result": {
            "batch_count": len(batch_results),
            "batch_results": [
                {
                    "batch_index": batch_index,
                    "raw_result": batch_debug.get("raw_result"),
                }
                for batch_index, _batch_payload, batch_debug, _batch_updates in batch_results
            ],
        },
        "payload": original_payload,
        "cluster_batch_count": len(batch_results),
        "cluster_batch_size_limit": max_steps,
        "batches": batch_debug_entries,
    }
    return debug, updates


async def apply_screenshot_render_classification(
    materialized_reasoning: Dict[str, Any],
    observations: List[FrameObservation],
    judge_cluster: ClusterJudge,
    task_actions_by_cluster: Dict[str, List[Dict[str, Any]]] | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    result = copy.deepcopy(materialized_reasoning or {})
    labels = [item for item in result.get("step_labels", []) if isinstance(item, dict)]
    obs_map = {_obs_key(obs): obs for obs in observations or []}

    labels_by_cluster: Dict[str, List[Dict[str, Any]]] = {}
    for label in labels:
        cluster_id = str(label.get("page_cluster_id", "")).strip()
        if not cluster_id:
            continue
        labels_by_cluster.setdefault(cluster_id, []).append(label)

    page_by_cluster = {
        str(page.get("page_cluster_id", "")).strip(): page
        for page in result.get("canonical_pages", [])
        if isinstance(page, dict) and str(page.get("page_cluster_id", "")).strip()
    }
    _task_actions = task_actions_by_cluster or {}
    payloads = {
        cluster_id: _cluster_payload(
            cluster_id, cluster_labels, obs_map, page_by_cluster,
            task_actions_on_page=_task_actions.get(cluster_id),
        )
        for cluster_id, cluster_labels in sorted(labels_by_cluster.items())
    }

    async def classify_payload(cluster_id: str, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, bool]]:
        raw_result = None
        retry_error = ""
        try:
            raw_result = await judge_cluster(payload)
            cluster_debug, cluster_updates = _normalize_judge_result(cluster_id, payload, raw_result)
        except ScreenshotRenderClassificationError as exc:
            retry_error = str(exc)
            validation_details = _classification_validation_details(payload, raw_result)
            retry_payload = copy.deepcopy(payload)
            retry_payload["retry_context"] = {
                "previous_error": retry_error,
                "previous_result": raw_result,
                **validation_details,
                "instruction": (
                    "Return the keyed classifications schema again. Fix the validation error exactly. "
                    "Use classification_skeleton.classifications keys as the complete and only allowed "
                    "set of output keys. Do not create, delete, rename, or reorder keys. Every expected "
                    "step_key must appear exactly once under classifications. duplicate_of must reference "
                    "a classification=unique key, and all reason fields must be written in Chinese."
                ),
            }
            raw_result = await judge_cluster(retry_payload)
            cluster_debug, cluster_updates = _normalize_judge_result(cluster_id, payload, raw_result)
            cluster_debug["retry_context"] = {
                "previous_error": retry_error,
                "previous_result": retry_payload["retry_context"]["previous_result"],
            }
        cluster_debug["payload"] = payload
        return cluster_debug, cluster_updates

    async def run_one(cluster_id: str, payload: Dict[str, Any], attempt: int = 1) -> Tuple[str, Dict[str, Any], Dict[str, bool]]:
        max_steps = _cluster_scheduler_max_steps()
        batch_payloads = _split_cluster_payload(payload, max_steps)
        if len(batch_payloads) == 1:
            cluster_debug, cluster_updates = await classify_payload(cluster_id, payload)
            cluster_debug["cluster_attempt"] = attempt
            cluster_debug["cluster_batch_count"] = 1
            cluster_debug["cluster_batch_size_limit"] = max_steps
            return cluster_id, cluster_debug, cluster_updates

        batch_results: List[Tuple[int, Dict[str, Any], Dict[str, Any], Dict[str, bool]]] = []
        for batch_index, batch_payload in enumerate(batch_payloads, start=1):
            batch_debug, batch_updates = await classify_payload(cluster_id, batch_payload)
            batch_debug["cluster_attempt"] = attempt
            batch_results.append((batch_index, batch_payload, batch_debug, batch_updates))
        cluster_debug, cluster_updates = _merge_batched_cluster_results(
            cluster_id,
            payload,
            batch_results,
            max_steps=max_steps,
        )
        cluster_debug["cluster_attempt"] = attempt
        return cluster_id, cluster_debug, cluster_updates

    updates: Dict[str, bool] = {}
    clusters_debug: Dict[str, Dict[str, Any]] = {}
    pending_payloads = dict(payloads)
    cluster_failures: Dict[str, List[str]] = {}
    exhausted_failures: Dict[str, List[str]] = {}
    max_attempts = _cluster_scheduler_max_attempts()
    for attempt in range(1, max_attempts + 1):
        if not pending_payloads:
            break
        attempt_items = list(pending_payloads.items())
        raw_results = await asyncio.gather(
            *(run_one(cluster_id, payload, attempt=attempt) for cluster_id, payload in attempt_items),
            return_exceptions=True,
        )
        next_pending: Dict[str, Dict[str, Any]] = {}
        for (cluster_id, payload), raw_result in zip(attempt_items, raw_results):
            if isinstance(raw_result, Exception):
                cluster_failures.setdefault(cluster_id, []).append(str(raw_result))
                if attempt < max_attempts:
                    next_pending[cluster_id] = payload
                else:
                    exhausted_failures[cluster_id] = cluster_failures.get(cluster_id, [str(raw_result)])
                continue
            result_cluster_id, cluster_debug, cluster_updates = raw_result
            clusters_debug[result_cluster_id] = cluster_debug
            updates.update(cluster_updates)
        pending_payloads = next_pending
    if exhausted_failures:
        details = "; ".join(
            f"{cluster_id}: {cluster_failures.get(cluster_id, ['unknown error'])[-1]}"
            for cluster_id in sorted(exhausted_failures)
        )
        raise ScreenshotRenderClassificationError(
            "screenshot render classification failed for clusters after "
            f"{max_attempts} attempts: {details}"
        )

    incoming_unique_count = sum(
        1 for label in labels
        if bool(label.get("has_unique_page_structure", True))
    )
    for label in labels:
        key = _step_key(label)
        if key in updates:
            label["has_unique_page_structure"] = bool(updates[key])

    gr_p4_unique_count = sum(
        1 for label in labels
        if bool(label.get("has_unique_page_structure", True))
    )
    gr_p4_duplicate_count = len(labels) - gr_p4_unique_count
    result["step_labels"] = labels
    all_render_roles: Dict[str, str] = {}
    all_structural_roles: Dict[str, str] = {}
    all_lifecycle_roles: Dict[str, str] = {}
    all_initial_dom_eligible: Dict[str, bool] = {}
    all_action_host_authority: Dict[str, bool] = {}
    all_state_preconditions: Dict[str, List[str]] = {}
    all_screenshot_lifecycle: Dict[str, Dict[str, Any]] = {}
    all_initial_authority_kinds: Dict[str, str] = {}
    for cluster_debug in clusters_debug.values():
        all_render_roles.update(cluster_debug.get("render_role_by_step", {}))
        all_structural_roles.update(cluster_debug.get("structural_role_by_step", {}))
        all_lifecycle_roles.update(cluster_debug.get("lifecycle_role_by_step", {}))
        all_initial_dom_eligible.update(cluster_debug.get("initial_dom_eligible_by_step", {}))
        all_action_host_authority.update(cluster_debug.get("action_host_authority_by_step", {}))
        all_state_preconditions.update(cluster_debug.get("state_preconditions_by_step", {}))
        all_screenshot_lifecycle.update(cluster_debug.get("screenshot_lifecycle_by_step", {}))
        all_initial_authority_kinds.update(cluster_debug.get("initial_authority_kind_by_step", {}))
    debug = {
        "schema_version": "screenshot_render_classification.gpt_v1",
        "labeling_scope": "materialized_pages",
        "scope_term": "Materialized Page",
        "parent_term": "General Page",
        "naming": {
            "scope_term": "Materialized Page",
            "scope_id_field": "materialized_page_id",
            "parent_term": "General Page",
            "parent_id_field": "general_page_id",
            "legacy_scope_id_field": "page_cluster_id",
        },
        "summary": {
            "cluster_count": len(labels_by_cluster),
            "materialized_page_count": len(labels_by_cluster),
            "step_label_count": len(labels),
            "incoming_unique_count": incoming_unique_count,
            "gr_p4_unique_count": gr_p4_unique_count,
            "gr_p4_duplicate_count": gr_p4_duplicate_count,
            "phase4_unique_count": gr_p4_unique_count,
            "phase4_duplicate_count": gr_p4_duplicate_count,
            "materialized_unique_count": gr_p4_unique_count,
            "materialized_duplicate_count": gr_p4_duplicate_count,
            "unique_delta_from_incoming": gr_p4_unique_count - incoming_unique_count,
            "materialized_page_group_count": len(labels_by_cluster),
        },
        "clusters": clusters_debug,
        "render_role_by_step": all_render_roles,
        "structural_role_by_step": all_structural_roles,
        "lifecycle_role_by_step": all_lifecycle_roles,
        "initial_dom_eligible_by_step": all_initial_dom_eligible,
        "action_host_authority_by_step": all_action_host_authority,
        "state_preconditions_by_step": all_state_preconditions,
        "screenshot_lifecycle_by_step": all_screenshot_lifecycle,
        "initial_authority_kind_by_step": all_initial_authority_kinds,
    }
    return result, debug
