from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from trace2world_pipeline.compiler.delivery_contract import (
    build_formal_manifest,
    copy_formal_app,
    leaked_scaffold,
    read_json,
    sha256_file,
    transient_files,
    write_json,
)
from trace2world_pipeline.compiler.export.browser_checkpoint_evaluator import (
    compile_browser_evaluator_specs,
)
from trace2world_pipeline.compiler.export.inventory_export import export_visible_structure_inventory
from trace2world_pipeline.verification.runtime_bridge_injector import run as inject_runtime_bridge
from trace2world_pipeline.app_spec.page_execution_contracts import inject_page_execution_runtimes
from trace2world_pipeline.app_spec.environment_profile import load_environment_profile_from_result_dir


MOBILE_DRAG_SCROLL_SCRIPT = "../../phase_sdk_generation/runtime/mobile_drag_scroll.js?v=20260708"
EVIDENCE_REVISIT_SCRIPT = "../../phase_sdk_generation/runtime/evidence_revisit_runtime.js?v=20260813"
STATE_KERNEL_PROFILE = "state_kernel_sqlite"
BROWSER_CHECKPOINT_PROFILE = "browser_checkpoint_events"
# Canonical wire format for precompiled state evaluator plans, plus the legacy
# name still declared by result directories compiled before the rename.
_STATE_EVALUATOR_PLAN_FORMATS = {
    "ghostweb_trace2world_state_evaluator_plan",
    "ghostweb_anchorgym_state_evaluator_plan",
}
EVALUATOR_EXPORT_REQUIRED = "required"
EVALUATOR_EXPORT_DISABLED_ENV_ONLY = "disabled_env_only"
EVALUATOR_EXPORT_DISABLED_ARTIFACT_ONLY = "disabled_artifact_only"
EVALUATOR_EXPORT_MODES = {
    EVALUATOR_EXPORT_REQUIRED,
    EVALUATOR_EXPORT_DISABLED_ENV_ONLY,
    EVALUATOR_EXPORT_DISABLED_ARTIFACT_ONLY,
}


_EDITABLE_TAG_RE = re.compile(r"<(?:input|textarea)\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ROUTE_TARGET_INPUT_OPERATIONS = {"fill", "input", "type", "type_text"}


def _html_attribute(tag: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*([\"'])(?P<value>.*?)\1",
        tag,
        re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(match.group("value")) if match else ""


def _tag_owns_action(tag: str, action_id: str) -> bool:
    primary = _html_attribute(tag, "data-action-id")
    aliases = _html_attribute(tag, "data-action-aliases").split()
    return action_id == primary or action_id in aliases


def _with_initial_input_focus(tag: str) -> str:
    if _html_attribute(tag, "data-ghostweb-input-autofocus").lower() == "true":
        return tag
    insertion = ' data-ghostweb-input-autofocus="true"'
    if tag.rstrip().endswith("/>"):
        offset = tag.rfind("/>")
    else:
        offset = tag.rfind(">")
    if offset < 0:
        raise ValueError("editable host tag is not closed")
    return tag[:offset] + insertion + tag[offset:]


def _with_action_alias(tag: str, action_id: str) -> str:
    primary = _html_attribute(tag, "data-action-id")
    aliases = _html_attribute(tag, "data-action-aliases").split()
    if action_id == primary or action_id in aliases:
        return tag
    value = " ".join([*aliases, action_id])
    existing = re.search(
        r'(\bdata-action-aliases\s*=\s*)(["\'])(?P<value>.*?)(\2)',
        tag,
        re.IGNORECASE | re.DOTALL,
    )
    if existing:
        return (
            tag[: existing.start("value")]
            + html.escape(value, quote=True)
            + tag[existing.end("value") :]
        )
    insertion = f' data-action-aliases="{html.escape(value, quote=True)}"'
    offset = tag.rfind("/>") if tag.rstrip().endswith("/>") else tag.rfind(">")
    if offset < 0:
        raise ValueError("editable host tag is not closed")
    return tag[:offset] + insertion + tag[offset:]


def _route_target_query_input_actions(app_dir: Path) -> dict[str, set[str]]:

    authority_path = (
        app_dir
        / "phase_post_processing/action_authority/action_execution_contracts.json"
    )
    if not authority_path.is_file():
        return {}
    payload = read_json(authority_path, {}) or {}
    rows = _action_rows(payload)
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        trace_id = str(row.get("trace_id") or "").strip()
        if trace_id:
            by_trace.setdefault(trace_id, []).append(row)

    actions_by_target: dict[str, set[str]] = {}
    for trace_id, trace_rows in sorted(by_trace.items()):
        ordered = sorted(trace_rows, key=lambda row: int(row.get("step_index") or 0))
        for route_action, input_action in zip(ordered, ordered[1:]):
            route_step = int(route_action.get("step_index") or 0)
            input_step = int(input_action.get("step_index") or 0)
            source_page = str(route_action.get("source_page") or "").strip()
            target_page = str(route_action.get("target_page") or "").strip()
            input_source_page = str(input_action.get("source_page") or "").strip()
            operation = str(input_action.get("operation") or "").strip().lower()
            effect_kind = str(input_action.get("effect_kind") or "").strip().lower()
            action_id = str(input_action.get("action_id") or "").strip()
            if not (
                route_step > 0
                and input_step == route_step + 1
                and source_page
                and target_page
                and source_page != target_page
                and input_source_page == target_page
                and operation in _ROUTE_TARGET_INPUT_OPERATIONS
                and effect_kind == "query_projection"
                and action_id
            ):
                continue
            actions_by_target.setdefault(target_page, set()).add(action_id)
    return actions_by_target


def _bind_route_target_query_input_focus(app_dir: Path) -> int:

    bindings = _route_target_query_input_actions(app_dir)
    pages_dir = app_dir / "phase_pagegen/pages"
    bound_count = 0
    for target_page, action_ids in sorted(bindings.items()):
        page_path = pages_dir / target_page
        if not page_path.is_file():
            raise RuntimeError(
                f"Route-target query input page is missing: {target_page}"
            )
        content = page_path.read_text(encoding="utf-8")
        tags = [match.group(0) for match in _EDITABLE_TAG_RE.finditer(content)]
        matched_indexes: set[int] = set()
        for action_id in sorted(action_ids):
            matches = [
                index for index, tag in enumerate(tags) if _tag_owns_action(tag, action_id)
            ]
            if not matches:
                query_hosts = [
                    index
                    for index, tag in enumerate(tags)
                    if _html_attribute(tag, "data-ghostweb-query-input").lower() == "true"
                ]
                if len(query_hosts) == 1:
                    host_index = query_hosts[0]
                    original_tag = tags[host_index]
                    bound_tag = _with_action_alias(original_tag, action_id)
                    content = content.replace(original_tag, bound_tag, 1)
                    tags[host_index] = bound_tag
                    matches = [host_index]
            if len(matches) != 1:
                raise RuntimeError(
                    "Route-target query input authority must resolve to exactly one "
                    f"editable DOM host: page={target_page!r} action_id={action_id!r} "
                    f"matches={len(matches)}"
                )
            matched_indexes.update(matches)
        if len(matched_indexes) != 1:
            raise RuntimeError(
                "One route-target page resolves to multiple initial query input hosts: "
                f"page={target_page!r} host_count={len(matched_indexes)}"
            )
        host_index = next(iter(matched_indexes))
        host_tag = tags[host_index]
        if _html_attribute(host_tag, "data-ghostweb-query-input").lower() != "true":
            raise RuntimeError(
                "Route-target query projection is not bound to a formal query input: "
                f"page={target_page!r} actions={sorted(action_ids)!r}"
            )
        updated_tag = _with_initial_input_focus(host_tag)
        if updated_tag != host_tag:
            content = content.replace(host_tag, updated_tag, 1)
            page_path.write_text(content, encoding="utf-8")
        bound_count += 1
    return bound_count


# Frontend runtime scripts copied verbatim into every exported mobile
# environment; shipped as package data so the export stage is self-contained.
_FRONTEND_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "frontend"


def _postprocess_pages(app_dir: Path, *, mobile_runtime_enabled: bool) -> int:
    if mobile_runtime_enabled:
        template = _FRONTEND_TEMPLATES_DIR / "mobile_drag_scroll.js"
        if not template.is_file():
            raise FileNotFoundError(f"Missing mobile runtime template: {template}")
        runtime = app_dir / "phase_sdk_generation/runtime/mobile_drag_scroll.js"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        if not runtime.is_file() or runtime.read_bytes() != template.read_bytes():
            shutil.copyfile(template, runtime)
        evidence_template = _FRONTEND_TEMPLATES_DIR / "evidence_revisit_runtime.js"
        if not evidence_template.is_file():
            raise FileNotFoundError(f"Missing evidence revisit runtime template: {evidence_template}")
        evidence_runtime = app_dir / "phase_sdk_generation/runtime/evidence_revisit_runtime.js"
        evidence_runtime.parent.mkdir(parents=True, exist_ok=True)
        if not evidence_runtime.is_file() or evidence_runtime.read_bytes() != evidence_template.read_bytes():
            shutil.copyfile(evidence_template, evidence_runtime)
    _bind_route_target_query_input_focus(app_dir)
    count = 0
    for page in sorted((app_dir / "phase_pagegen/pages").glob("*.html")):
        content = page.read_text(encoding="utf-8")
        content, _changed = inject_page_execution_runtimes(content, {"filename": page.name})
        if mobile_runtime_enabled and "mobile_drag_scroll.js" not in content:
            tag = f'<script src="{MOBILE_DRAG_SCROLL_SCRIPT}"></script>'
            if re.search(r"</body\s*>", content, flags=re.IGNORECASE):
                content = re.sub(
                    r"</body\s*>",
                    tag + "\n</body>",
                    content,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                content = content.rstrip() + "\n" + tag + "\n"
        if mobile_runtime_enabled and "evidence_revisit_runtime.js" not in content:
            tag = f'<script src="{EVIDENCE_REVISIT_SCRIPT}"></script>'
            if re.search(r"</body\s*>", content, flags=re.IGNORECASE):
                content = re.sub(r"</body\s*>", tag + "\n</body>", content, count=1, flags=re.IGNORECASE)
            else:
                content = content.rstrip() + "\n" + tag + "\n"
        page.write_text(content, encoding="utf-8")
        count += 1
    return count


def _materialize_checkpoint_runtime_bridge(result_dir: Path) -> dict[str, Any] | None:
    bindings_path = (
        result_dir
        / "phase_post_processing/instrumentation/runtime_event_bindings.json"
    )
    if not bindings_path.is_file():
        return None
    return inject_runtime_bridge(result_dir, contract_source_dir=result_dir)


def _task_records(payload: Any) -> list[dict[str, Any]]:
    records = payload.get("tasks", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("phase_post_processing/tasks/tasks.json must contain a tasks list")
    normalized: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        task_id = str(record.get("id") or record.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("Task record is missing id/task_id")
        normalized.append({**record, "task_id": task_id})
    return normalized


def _action_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("actions") or payload.get("action_execution_contracts") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [row for row in rows if isinstance(row, dict)]


def _build_state_evaluator_plan(result_dir: Path) -> dict[str, Any]:
    reward_bundle = read_json(
        result_dir
        / "phase_page_environment/semantic_api/08_spec_compiled_execution_views/reward_authority_bundle.json",
        {},
    ) or {}
    if reward_bundle.get("format") != "ghostweb_reward_authority_bundle":
        raise ValueError("Missing formal ghostweb_reward_authority_bundle")
    reward_by_task = {
        str(row.get("task_id")): row
        for row in reward_bundle.get("contracts", [])
        if isinstance(row, dict) and row.get("task_id")
    }
    executable = read_json(
        result_dir / "phase_post_processing/evaluators/executable_evaluator_manifest.json",
        {},
    ) or {}
    evaluators = executable.get("evaluators", {})
    if not isinstance(evaluators, dict):
        raise ValueError("executable_evaluator_manifest.evaluators must be an object")
    binding = read_json(
        result_dir / "phase_post_processing/evaluators/evaluator_state_bindings.json",
        {},
    ) or {}
    action_authority = read_json(
        result_dir / "phase_post_processing/action_authority/action_execution_contracts.json",
        {},
    ) or {}
    transaction_manifest = read_json(
        result_dir / "phase_sdk_generation/runtime/backend/contracts/transaction_manifest.json",
        {},
    ) or {}
    transaction_rows = [
        row for row in transaction_manifest.get("transactions", []) if isinstance(row, dict)
    ]
    tx_by_action = {
        str(row.get("source_action_id")): str(row.get("transaction_id"))
        for row in transaction_rows
        if row.get("source_action_id") and row.get("transaction_id")
    }
    tx_by_id = {
        str(row.get("transaction_id")): row
        for row in transaction_rows
        if row.get("transaction_id")
    }
    actions_by_trace: dict[str, list[dict[str, Any]]] = {}
    for action in _action_rows(action_authority):
        actions_by_trace.setdefault(str(action.get("trace_id") or ""), []).append(action)

    plans: dict[str, Any] = {}
    missing_canonical_outcomes: list[dict[str, Any]] = []
    canonical_checkpoint_count = 0
    projection_checkpoint_count = 0
    for task_id, evaluator in sorted(evaluators.items()):
        if not isinstance(evaluator, dict):
            raise ValueError(f"Invalid executable evaluator for {task_id}")
        checkpoint_authority: dict[str, list[dict[str, Any]]] = {}
        for action in actions_by_trace.get(str(task_id), []):
            transaction_id = str(action.get("transaction_id") or tx_by_action.get(str(action.get("action_id") or "")) or "")
            evidence_mode = "canonical_transaction" if transaction_id else "visible_projection"
            for checkpoint_id in action.get("checkpoint_ids") or []:
                checkpoint_authority.setdefault(str(checkpoint_id), []).append(
                    {
                        "action_id": action.get("action_id"),
                        "transaction_id": transaction_id or None,
                        "evidence_mode": evidence_mode,
                        "source_page": action.get("source_page"),
                        "target_page": action.get("target_page"),
                    }
                )
                if transaction_id:
                    canonical_checkpoint_count += 1
                else:
                    projection_checkpoint_count += 1
        outcome = evaluator.get("outcome") if isinstance(evaluator.get("outcome"), dict) else {}
        outcome_id = str(outcome.get("checkpoint_id") or "")
        reward = reward_by_task.get(str(task_id))
        if not isinstance(reward, dict):
            raise ValueError(f"Missing formal reward authority for {task_id}")
        outcome_authority = [
            row for row in reward.get("required_authorities", []) if isinstance(row, dict)
        ]
        outcome_transaction_ids = sorted(
            {str(item["transaction_id"]) for item in outcome_authority if item.get("transaction_id")}
        )
        canonical_assertions = reward.get("state_assertions")
        if not isinstance(canonical_assertions, dict):
            canonical_assertions = {}
        asserted_paths = set(canonical_assertions)
        declared_writes = {
            str(path)
            for transaction_id in outcome_transaction_ids
            for path in tx_by_id.get(transaction_id, {}).get("state_writes", [])
        }
        uncovered_assertions = sorted(asserted_paths - declared_writes)
        reasons = [
            str(problem.get("code") or "reward_authority_blocked")
            for problem in reward.get("problems", [])
            if isinstance(problem, dict)
        ]
        if not outcome_authority:
            reasons.append("outcome_has_no_causal_authority")
        mutation_authority = reward.get("outcome_kind") in {
            "business_mutation", "runtime_selection", "query_projection"
        }
        if mutation_authority and not outcome_transaction_ids:
            reasons.append("mutation_outcome_has_no_canonical_transaction_authority")
        if mutation_authority and not canonical_assertions:
            reasons.append("mutation_outcome_has_no_canonical_state_assertions")
        if uncovered_assertions:
            reasons.append("outcome_assertions_not_written_by_outcome_transaction")
        canonical_outcome = bool(outcome_id and reward.get("canonical_outcome_ready") and not reasons)
        if outcome_id and not canonical_outcome:
            missing_canonical_outcomes.append(
                {
                    "task_id": task_id,
                    "checkpoint_id": outcome_id,
                    "reasons": reasons,
                    "uncovered_canonical_paths": uncovered_assertions,
                }
            )
        plans[str(task_id)] = {
            "task_id": task_id,
            "state_source": "finalized_environment_state_snapshot",
            "requires_evaluation_handle": True,
            "executable_evaluator": evaluator,
            "checkpoint_authority": checkpoint_authority,
            "canonical_assertions": canonical_assertions,
            "outcome_transaction_ids": outcome_transaction_ids,
            "required_outcome_authorities": outcome_authority,
            "reward_authority": reward,
            "canonical_outcome_ready": canonical_outcome,
        }
    return {
        "format": "ghostweb_slime_state_evaluator_plan",
        "schema_revision": 1,
        "state_source": "finalized_environment_state_snapshot",
        "evaluation_handle_fields": binding.get("evaluation_handle_fields") or [
            "sid",
            "episode_epoch",
            "revision",
            "snapshot_id",
            "state_hash",
        ],
        "binding": binding,
        "action_authority_ref": "app/phase_post_processing/action_authority/action_execution_contracts.json",
        "transaction_manifest_ref": "app/phase_sdk_generation/runtime/backend/contracts/transaction_manifest.json",
        "reward_authority_bundle_ref": "app/phase_page_environment/semantic_api/08_spec_compiled_execution_views/reward_authority_bundle.json",
        "reward_authority_bundle_hash": reward_bundle.get("bundle_hash"),
        "task_count": len(plans),
        "canonical_checkpoint_count": canonical_checkpoint_count,
        "projection_checkpoint_count": projection_checkpoint_count,
        "canonical_outcome_ready_count": len(plans) - len(missing_canonical_outcomes),
        "missing_canonical_outcomes": missing_canonical_outcomes,
        "canonical_runtime_status": "ready" if not missing_canonical_outcomes else "partial_authority",
        "evaluators": plans,
    }


def _browser_evaluator_javascript(
    task_id: str,
    required: list[str],
    forbidden: list[str],
    source_result_dir: Path,
    evaluator_contract: dict[str, Any],
) -> str:
    task_json = json.dumps(task_id, ensure_ascii=False)
    required_json = json.dumps(required, ensure_ascii=False)
    forbidden_json = json.dumps(forbidden, ensure_ascii=False)
    source_json = json.dumps(str(source_result_dir), ensure_ascii=False)
    contract_json = json.dumps(evaluator_contract, ensure_ascii=False)
    return rf"""const TASK_ID = {task_json};
const REQUIRED = {required_json};
const FORBIDDEN = {forbidden_json};
const CONTRACT = {contract_json};
function safeParse(raw, fallback) {{
  if (!raw) return fallback;
  try {{ return JSON.parse(raw); }} catch (error) {{ return fallback; }}
}}
function safeStorageGet(key) {{
  try {{
    const value = window.sessionStorage?.getItem(key);
    if (value !== null && value !== undefined) return value;
  }} catch (error) {{}}
  try {{ return window.localStorage?.getItem(key) ?? null; }}
  catch (error) {{ return null; }}
}}
function safeLocalStorageGet(key) {{
  try {{ return window.localStorage?.getItem(key) ?? null; }}
  catch (error) {{ return null; }}
}}
function readEvents() {{
  let events = safeParse(safeStorageGet('__ghostweb_checkpoint_events_v1__'), null);
  if (!Array.isArray(events)) events = safeParse(safeLocalStorageGet('__ghostweb_checkpoint_events_v1__'), []);
  if ((!events || !events.length) && window.GhostwebCheckpointRuntime && window.GhostwebCheckpointRuntime.eventLog) {{
    try {{ events = window.GhostwebCheckpointRuntime.eventLog(); }} catch (error) {{ events = []; }}
  }}
  return Array.isArray(events) ? events : [];
}}
const events = readEvents();
const matching = events.filter((event) => event && (!event.trace_id || event.trace_id === TASK_ID));
function valueAt(event, field) {{
  if (!event) return undefined;
  if (event.payload && Object.prototype.hasOwnProperty.call(event.payload, field)) return event.payload[field];
  if (Object.prototype.hasOwnProperty.call(event, field)) return event[field];
  return undefined;
}}
function normalizedText(value) {{
  return String(value == null ? '' : value)
    .normalize('NFKC')
    .replace(/\s+/g, ' ')
    .trim()
    .toLocaleLowerCase();
}}
function matchesAssertion(actual, assertion) {{
  if (!assertion || typeof assertion !== 'object') return true;
  if (assertion.op === 'present') return actual !== undefined && actual !== null && actual !== '';
  if (assertion.op === 'eq') {{
    const matcher = assertion.typed_matcher || {{}};
    if (matcher.match_mode === 'normalized_text') {{
      return normalizedText(actual) === normalizedText(assertion.value);
    }}
    return JSON.stringify(actual) === JSON.stringify(assertion.value);
  }}
  if (assertion.op === 'numeric_eq') return Number(actual) === Number(assertion.value);
  if (assertion.op === 'contains' || assertion.op === 'contains_visible') {{
    return String(actual || '').includes(String(assertion.value || ''));
  }}
  if (assertion.op === 'selected_contains') {{
    if (Array.isArray(actual)) return actual.some((item) => String(item) === String(assertion.value));
    return String(actual || '').includes(String(assertion.value || ''));
  }}
  if (assertion.op === 'set_contains_all') {{
    const actualValues = Array.isArray(actual) ? actual : [actual];
    const expectedValues = Array.isArray(assertion.value) ? assertion.value : [assertion.value];
    return expectedValues.every((expected) =>
      actualValues.some((item) => JSON.stringify(item) === JSON.stringify(expected))
    );
  }}
  return false;
}}
function checkpointContract(checkpoint) {{
  if (CONTRACT.checkpoints && CONTRACT.checkpoints[checkpoint]) return CONTRACT.checkpoints[checkpoint];
  if (CONTRACT.outcome && CONTRACT.outcome.checkpoint_id === checkpoint) return CONTRACT.outcome;
  return null;
}}
function validateEvent(checkpoint, event) {{
  const rule = checkpointContract(checkpoint);
  if (!rule) return {{ valid: false, reasons: ['missing_evaluator_contract'] }};
  const reasons = [];
  if (rule.required_event) {{
    const actualEvent = valueAt(event, 'event') || event.name || event.type || '';
    if (String(actualEvent) !== String(rule.required_event)) reasons.push('required_event_mismatch');
  }}
  const assertions = Object.assign({{}}, rule.payload_assertions || {{}}, rule.state_assertions || {{}});
  Object.keys(assertions).forEach((field) => {{
    if (!matchesAssertion(valueAt(event, field), assertions[field])) reasons.push('assertion_failed:' + field);
  }});
  return {{ valid: reasons.length === 0, reasons }};
}}
const valid = new Set();
const invalid = {{}};
REQUIRED.forEach((checkpoint) => {{
  const candidates = matching.filter((event) => String(event.checkpoint_id || '') === checkpoint);
  for (const event of candidates) {{
    const result = validateEvent(checkpoint, event);
    if (result.valid) {{ valid.add(checkpoint); break; }}
    invalid[checkpoint] = result.reasons;
  }}
  if (!candidates.length) invalid[checkpoint] = ['event_missing'];
}});
const missing = REQUIRED.filter((checkpoint) => !valid.has(checkpoint));
const forbiddenHit = matching.some((event) => {{
  const haystack = [event.checkpoint_id, event.action_id, event.event, event.name, event.type,
    event.payload && event.payload.event_name].filter(Boolean).join(' ').toLowerCase();
  return FORBIDDEN.some((name) => haystack.includes(String(name).toLowerCase()));
}});
const score = REQUIRED.length ? (REQUIRED.length - missing.length) / REQUIRED.length : 0;
window.__ghostweb_last_eval_result__ = {{
  task_id: TASK_ID,
  required: REQUIRED,
  missing,
  seen: Array.from(valid),
  invalid,
  event_count: matching.length,
  forbiddenHit,
  score,
  source_result_dir: {source_json}
}};
if (forbiddenHit) return 0.0;
return score;
"""


def _build_browser_evaluators(
    result_dir: Path,
    evaluators_dir: Path,
) -> dict[str, Any]:
    evaluator_specs = compile_browser_evaluator_specs(result_dir)
    plans: dict[str, Any] = {}
    for task_id, spec in sorted(evaluator_specs.items()):
        evaluators_dir.mkdir(parents=True, exist_ok=True)
        evaluator_file = str(spec.get("evaluator_file") or f"{task_id}.js")
        if Path(evaluator_file).name != evaluator_file:
            raise ValueError(
                f"Invalid packaged browser evaluator_file for {task_id}: {evaluator_file!r}"
            )
        evaluator_path = evaluators_dir / evaluator_file
        evaluator_path.write_text(
            str(spec["evaluation_logic"]),
            encoding="utf-8",
        )
        actual_digest = sha256_file(evaluator_path)
        declared_digest = spec.get("evaluator_sha256")
        if declared_digest and actual_digest != declared_digest:
            raise ValueError(
                f"Packaged browser evaluator digest mismatch for {task_id}: "
                f"declared={declared_digest}, actual={actual_digest}"
            )
        source_plan_entry = spec.get("source_plan_entry")
        if not isinstance(source_plan_entry, dict):
            source_plan_entry = {}
        plans[str(task_id)] = {
            **source_plan_entry,
            "task_id": str(task_id),
            "evaluator_file": evaluator_file,


            "evaluator_sha256": actual_digest,
            "required_checkpoints": spec["required_checkpoints"],
            "forbidden_events": spec["forbidden_events"],
            "terminal_policy": spec.get("terminal_policy"),
        }
    return {
        "format": "ghostweb_slime_browser_evaluator_plan",
        "schema_revision": 1,
        "mode": BROWSER_CHECKPOINT_PROFILE,
        "task_count": len(plans),
        "evaluators": plans,
    }


def _create_archive(package_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(package_dir, arcname=package_dir.name)


def build_package(
    *,
    result_dir: Path,
    package_dir: Path,
    env_name: str,
    response_mode: str = "env_text",
    actionability_preflight: bool = True,
    finalize_archive: bool = False,
    evaluator_export_mode: str = EVALUATOR_EXPORT_REQUIRED,
    postprocess_pages: bool = True,
) -> tuple[Path, Path | None]:
    result_dir = result_dir.resolve()
    environment_profile = load_environment_profile_from_result_dir(result_dir)
    mobile_runtime_enabled = environment_profile["platform"] == "mobile"
    package_dir = package_dir.resolve()
    if package_dir.exists():
        raise FileExistsError(f"Package directory already exists: {package_dir}")
    if not env_name.strip():
        raise ValueError("env_name must not be empty")
    if evaluator_export_mode not in EVALUATOR_EXPORT_MODES:
        raise ValueError(
            "evaluator_export_mode must be one of "
            f"{sorted(EVALUATOR_EXPORT_MODES)}; got {evaluator_export_mode!r}"
        )
    evaluation_enabled = evaluator_export_mode == EVALUATOR_EXPORT_REQUIRED
    package_dir.mkdir(parents=True)
    app_dir = package_dir / "app"
    state_kernel_enabled = (
        result_dir / "phase_sdk_generation/runtime/backend/service_manifest.json"
    ).is_file()
    runtime_profile = STATE_KERNEL_PROFILE if state_kernel_enabled else BROWSER_CHECKPOINT_PROFILE

    source_tasks_payload = read_json(
        result_dir / "phase_post_processing/tasks/tasks.json", {}
    )
    source_tasks = _task_records(source_tasks_payload)
    l0_pack = read_json(
        result_dir / "phase_consolidation/l0_execution/l0_execution_pack.json",
        {},
    ) or {}
    trace_index = l0_pack.get("trace_index", {})
    if not isinstance(trace_index, dict):
        raise ValueError("l0_execution_pack.trace_index must be an object")

    tasks: list[dict[str, Any]] = []
    for source_task in source_tasks:
        task_id = source_task["task_id"]
        trace_entry = trace_index.get(task_id, {})
        start_filename = str(trace_entry.get("start_filename") or "").strip()
        declared_start_page = str(source_task.get("start_page") or "").strip()
        if start_filename:
            start_page = f"phase_pagegen/pages/{start_filename}"
        elif declared_start_page:


            start_page = declared_start_page
        else:
            raise ValueError(f"Missing authoritative start page for {task_id}")
        if not start_page.startswith("phase_pagegen/pages/"):
            raise ValueError(
                f"Task start_page must target phase_pagegen/pages/: {task_id} -> {start_page}"
            )
        if not (result_dir / start_page).is_file():
            raise FileNotFoundError(f"Authoritative start page does not exist for {task_id}: {start_page}")
        instruction = str(
            source_task.get("instruction")
            or source_task.get("description")
            or source_task.get("name")
            or task_id
        )
        initial_state = source_task.get("initial_state")
        tasks.append(
            {
                "task_id": task_id,
                "instruction": instruction,
                "description": str(source_task.get("description") or instruction),
                "start_page": start_page,
                "initial_state": initial_state if isinstance(initial_state, dict) else {},
                "runtime_profile": runtime_profile,
                "response_mode": response_mode,
                "actionability_preflight": actionability_preflight,


                "evaluation_eligible": source_task.get("evaluation_eligible", True),
                "evaluation_portfolio_included": source_task.get(
                    "evaluation_portfolio_included", True
                ),
            }
        )


    checkpoint_bridge_summary = _materialize_checkpoint_runtime_bridge(result_dir)

    copy_formal_app(
        result_dir,
        app_dir,
        require_evaluators=evaluation_enabled,
    )
    if postprocess_pages:
        page_count = _postprocess_pages(
            app_dir,
            mobile_runtime_enabled=mobile_runtime_enabled,
        )
    else:


        page_count = len(list((app_dir / "phase_pagegen/pages").glob("*.html")))
    formal_manifest = build_formal_manifest(
        source_root=result_dir,
        app_root=app_dir,
        package_name=package_dir.name,
        target="slime_webenv",
        require_action_authority=state_kernel_enabled and evaluation_enabled,
        require_evaluators=evaluation_enabled,
    )
    write_json(app_dir / "formal_artifact_manifest.json", formal_manifest)
    write_json(package_dir / "data/formal_artifact_manifest.json", formal_manifest)
    write_json(
        package_dir / "data/source_hashes.json",
        {
            "format": "ghostweb_slime_source_hashes.v1",
            "source_result_dir": str(result_dir),
            "files": {
                row["path"]: {
                    "source_sha256": row["source_sha256"],
                    "package_sha256": row["package_sha256"],
                    "hash_match": row["hash_match"],
                }
                for row in formal_manifest["required_artifacts"]
            },
        },
    )

    evaluators_dir = package_dir / "evaluators"
    evaluator_plan: dict[str, Any] | None = None
    evaluator_plan_name: str | None = None
    replay_authority: dict[str, Any] | None = None
    if not evaluation_enabled:
        replay_source = (
            result_dir
            / "phase_page_environment"
            / "semantic_api"
            / "08_spec_compiled_execution_views"
            / "contract_replay_plan.json"
        )
        replay_plan = read_json(replay_source, {}) or {}
        if replay_plan.get("format") != "spec_contract_replay_plan":
            raise ValueError(f"Missing formal spec_contract_replay_plan: {replay_source}")
        readiness = replay_plan.get("readiness") if isinstance(replay_plan.get("readiness"), dict) else {}
        if (
            evaluator_export_mode == EVALUATOR_EXPORT_DISABLED_ENV_ONLY
            and readiness.get("ready") is not True
        ):
            raise ValueError(f"Formal contract replay plan is not ready: {replay_source}")
        replay_target = package_dir / "data/replay_authority/contract_replay_plan.json"
        replay_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(replay_source, replay_target)
        replay_authority = {
            "enabled": True,
            "mode": "page_environment_contract_replay_plan",
            "plan": "data/replay_authority/contract_replay_plan.json",
            "source_sha256": sha256_file(replay_source),
            "package_sha256": sha256_file(replay_target),
            "hash_match": sha256_file(replay_source) == sha256_file(replay_target),
            "readiness": readiness,
        }
        evaluator_mode = evaluator_export_mode
        artifact_only = evaluator_export_mode == EVALUATOR_EXPORT_DISABLED_ARTIFACT_ONLY
        evaluator_runtime_manifest = {
            "format": "ghostweb_slime_evaluator_runtime_manifest",
            "schema_revision": 1,
            "enabled": False,
            "mode": evaluator_mode,
            "reason": (
                "source_result_declares_instrumentation_artifacts_only_without_formal_evaluators"
                if artifact_only
                else "instrumentation_skipped_by_user"
            ),
            "canonical_runtime_status": "not_applicable_env_only",
        }
    elif state_kernel_enabled:
        precompiled_plan_path = (
            result_dir
            / "phase_post_processing/evaluators/state_evaluator_plan.json"
        )
        precompiled_runtime_path = (
            result_dir
            / "phase_post_processing/evaluators/evaluator_runtime_manifest.json"
        )
        if precompiled_plan_path.is_file():
            evaluator_plan = read_json(precompiled_plan_path, {}) or {}
            # Precompiled plans carry the canonical Trace2World format; result
            # directories produced by the upstream evaluator compiler
            # before the rename still declare the legacy name and stay importable.
            if evaluator_plan.get("format") not in _STATE_EVALUATOR_PLAN_FORMATS:
                raise ValueError(
                    f"Invalid precompiled state evaluator plan: {precompiled_plan_path}"
                )
            precompiled_ids = set((evaluator_plan.get("evaluators") or {}).keys())
            source_task_ids = {task["task_id"] for task in tasks}
            if precompiled_ids != source_task_ids:
                raise ValueError(
                    "Precompiled evaluator plan task ids do not match the formal task product"
                )
            evaluator_runtime_manifest = read_json(precompiled_runtime_path, {}) or {}
            if evaluator_runtime_manifest.get("canonical_runtime_status") != "ready":
                raise ValueError(
                    f"Precompiled evaluator runtime is not ready: {precompiled_runtime_path}"
                )
            evaluator_runtime_manifest = {
                **evaluator_runtime_manifest,
                "plan": "state_evaluator_plan.json",
            }
        else:
            evaluator_plan = _build_state_evaluator_plan(result_dir)
            evaluator_runtime_manifest = {
                "format": "ghostweb_slime_evaluator_runtime_manifest",
                "schema_revision": 1,
                "mode": "finalized_environment_state_snapshot",
                "plan": "state_evaluator_plan.json",
                "browser_evaluator_allowed": False,
                "canonical_runtime_status": evaluator_plan["canonical_runtime_status"],
            }
        evaluator_plan_name = "state_evaluator_plan.json"
        evaluator_mode = str(
            evaluator_runtime_manifest.get("mode")
            or "finalized_environment_state_snapshot"
        )
    else:
        evaluator_plan = _build_browser_evaluators(result_dir, evaluators_dir)
        evaluator_plan_name = "browser_evaluator_plan.json"
        evaluator_mode = BROWSER_CHECKPOINT_PROFILE


        precompiled_plan_path = result_dir.parent / "evaluators/browser_evaluator_plan.json"
        if precompiled_plan_path.is_file():
            precompiled = read_json(precompiled_plan_path, {}) or {}
            precompiled_entries = precompiled.get("evaluators", {})
            if isinstance(precompiled_entries, dict):
                formal_ids = set(evaluator_plan["evaluators"])
                holdout_entries = {
                    str(task_id): entry
                    for task_id, entry in precompiled_entries.items()
                    if str(task_id) not in formal_ids
                    and isinstance(entry, dict)
                    and entry.get("evaluation_portfolio_included") is False
                    and entry.get("authoring_checkpoint_credit") is False
                }
                for task_id, entry in sorted(holdout_entries.items()):
                    evaluator_file = str(entry.get("evaluator_file") or "")
                    if not evaluator_file or Path(evaluator_file).name != evaluator_file:
                        raise ValueError(f"Invalid non-formal evaluator file for {task_id}")
                    source_eval = result_dir.parent / "evaluators" / evaluator_file
                    if not source_eval.is_file():
                        raise ValueError(f"Missing non-formal evaluator for {task_id}: {source_eval}")
                    target_eval = evaluators_dir / evaluator_file
                    shutil.copyfile(source_eval, target_eval)
                    evaluator_plan["evaluators"][task_id] = {
                        **entry,
                        "task_id": task_id,
                        "evaluator_sha256": sha256_file(target_eval),
                        "evaluation_eligible": False,
                        "evaluation_portfolio_included": False,
                        "authoring_checkpoint_credit": False,
                    }
        precompiled_runtime_path = (
            result_dir.parent / "evaluators/evaluator_runtime_manifest.json"
        )
        precompiled_plan_path = result_dir.parent / "evaluators/browser_evaluator_plan.json"
        precompiled_runtime = (
            read_json(precompiled_runtime_path, {}) or {}
            if precompiled_plan_path.is_file()
            else {}
        )
        if precompiled_plan_path.is_file() and not isinstance(precompiled_runtime, dict):
            raise ValueError(
                f"Invalid precompiled browser evaluator runtime manifest: {precompiled_runtime_path}"
            )
        if precompiled_plan_path.is_file() and not precompiled_runtime_path.is_file():
            raise ValueError(
                f"Missing precompiled browser evaluator runtime manifest: {precompiled_runtime_path}"
            )
        if precompiled_runtime and precompiled_runtime.get("mode") != evaluator_mode:
            raise ValueError(
                "Precompiled browser evaluator runtime mode mismatch: "
                f"expected={evaluator_mode}, actual={precompiled_runtime.get('mode')}"
            )
        evaluator_runtime_manifest = {
            **precompiled_runtime,
            "format": "ghostweb_slime_evaluator_runtime_manifest",
            "schema_revision": 1,
            "mode": evaluator_mode,
            "plan": evaluator_plan_name,
            "browser_evaluator_allowed": True,
            "canonical_runtime_status": "not_applicable_browser_checkpoint_profile",
            "evaluator_count": len(evaluator_plan["evaluators"]),
        }
    if evaluation_enabled:
        assert evaluator_plan is not None and evaluator_plan_name is not None
        write_json(evaluators_dir / evaluator_plan_name, evaluator_plan)
        write_json(evaluators_dir / "evaluator_runtime_manifest.json", evaluator_runtime_manifest)


    write_json(
        package_dir / "tasks.json",
        source_tasks_payload if isinstance(source_tasks_payload, dict) else {"tasks": tasks},
    )

    service_manifest = read_json(
        app_dir / "phase_sdk_generation/runtime/backend/service_manifest.json",
        {},
    ) or {}
    delivery_scope = (
        "full_evaluation"
        if evaluation_enabled
        else (
            "env_only_unscored_artifact"
            if evaluator_export_mode == EVALUATOR_EXPORT_DISABLED_ARTIFACT_ONLY
            else "env_only_replay_diagnostic"
        )
    )
    training_ready = evaluation_enabled
    inventory_export = export_visible_structure_inventory(result_dir, package_dir)
    resource_manifest = {


            "format": "ghostweb_agentforge_webenv_resource.v2",
        "schema_revision": 2,
        "package_name": package_dir.name,
        "env_name": env_name,
        "runtime_profile": runtime_profile,
        "app_root": "app",
        "tasks_file": "tasks.json",
        "entry_page": tasks[0]["start_page"] if tasks else "",
        "response_mode": response_mode,
        "actionability_preflight": actionability_preflight,
        "platform": environment_profile["platform"],
        "environment_profile_hash": environment_profile["profile_hash"],
        "browser_viewport": environment_profile["browser_viewport"],
        "delivery_scope": delivery_scope,
        "training_ready": training_ready,
        "evaluator": (
            {
                "enabled": True,
                "mode": evaluator_mode,
                "plan": f"evaluators/{evaluator_plan_name}",
                **({"directory": "evaluators"} if not state_kernel_enabled else {}),
                "runtime_manifest": "evaluators/evaluator_runtime_manifest.json",
                "canonical_runtime_status": evaluator_runtime_manifest["canonical_runtime_status"],
            }
            if evaluation_enabled
            else {
                "enabled": False,
                "mode": evaluator_mode,
                "reason": evaluator_runtime_manifest["reason"],
                "canonical_runtime_status": evaluator_runtime_manifest["canonical_runtime_status"],
            }
        ),
        "formal_artifacts": {
            "manifest": "data/formal_artifact_manifest.json",
            "policy": formal_manifest["export_policy"],
            "status": formal_manifest["status"],
        },
    }
    if replay_authority is not None:
        resource_manifest["replay_authority"] = replay_authority
    if inventory_export is not None:
        resource_manifest["visible_structure_inventory"] = inventory_export
    if state_kernel_enabled:
        resource_manifest["state_service"] = {
            "mode": "packaged_sidecar",
            "service_manifest": "app/phase_sdk_generation/runtime/backend/service_manifest.json",
            "entrypoint": "app/phase_sdk_generation/runtime/backend/run_state_service.py",
            "proxy_prefix": "/api/v1",
            "schema_hash": service_manifest.get("schema_hash"),
            "world_hash": service_manifest.get("world_hash"),
            "bundle_hash": service_manifest.get("bundle_hash"),
        }
    write_json(package_dir / "resource_manifest.json", resource_manifest)

    task_ids = {
        task["task_id"] for task in tasks if task.get("evaluation_eligible", True)
    }
    all_task_ids = {task["task_id"] for task in tasks}
    evaluator_ids = (
        {
            task_id
            for task_id, evaluator in evaluator_plan["evaluators"].items()
            if not (
                isinstance(evaluator, dict)
                and evaluator.get("evaluation_portfolio_included") is False
                and evaluator.get("authoring_checkpoint_credit") is False
            )
        }
        if evaluator_plan is not None
        else set()
    )
    scaffold = leaked_scaffold(app_dir)
    transients = transient_files(app_dir)
    issues: list[dict[str, Any]] = []
    if formal_manifest["status"] != "pass":
        issues.append({"code": "formal_artifact_manifest_failed", "details": formal_manifest["issues"]})
    if evaluation_enabled and task_ids != evaluator_ids:
        issues.append({"code": "task_evaluator_id_mismatch"})
    if scaffold:
        issues.append({"code": "excluded_scaffold_packaged", "paths": scaffold})
    if transients:
        issues.append({"code": "transient_files_packaged", "paths": transients})
    missing_start_pages = [task["task_id"] for task in tasks if not (app_dir / task["start_page"]).is_file()]
    if missing_start_pages:
        issues.append({"code": "missing_start_pages", "task_ids": missing_start_pages})
    if replay_authority is not None and not replay_authority["hash_match"]:
        issues.append({"code": "replay_authority_hash_mismatch"})
    if (
        evaluation_enabled
        and state_kernel_enabled
        and evaluator_plan is not None
        and evaluator_plan["canonical_runtime_status"] != "ready"
    ):
        issues.append(
            {
                "code": "canonical_evaluator_authority_incomplete",
                "missing_outcomes": evaluator_plan["missing_canonical_outcomes"],
            }
        )

    authority = formal_manifest["instrumentation"]["action_authority"]
    validation = {
        "format": "ghostweb_slime_webenv_package_validation.v2",
        "status": "pass" if not issues else "fail",
        "package_dir": str(package_dir),
        "source_result_dir": str(result_dir),
        "task_count": len(tasks),
        "evaluator_count": len(evaluator_ids),
        "evaluation_enabled": evaluation_enabled,
        "delivery_scope": delivery_scope,
        "training_ready": training_ready,
        "page_count": page_count,
        "platform": environment_profile["platform"],
        "environment_profile_hash": environment_profile["profile_hash"],
        "mobile_runtime_enabled": mobile_runtime_enabled,
        "all_start_pages_exist": not missing_start_pages,
        "task_ids_match_evaluators": task_ids == evaluator_ids if evaluation_enabled else None,
        "formal_task_count": len(task_ids),
        "metadata_only_task_count": len(all_task_ids - task_ids),
        "formal_artifact_status": formal_manifest["status"],
        "required_artifact_count": len(formal_manifest["required_artifacts"]),
        "source_package_hash_mismatch_count": len(
            [row for row in formal_manifest["required_artifacts"] if not row["hash_match"]]
        ),
        "action_authority": authority,
        "runtime_profile": runtime_profile,
        "evaluator_mode": evaluator_mode,
        "replay_authority": replay_authority,
        "canonical_evaluator_runtime_status": evaluator_runtime_manifest["canonical_runtime_status"],
        "canonical_outcome_ready_count": (
            evaluator_plan.get("canonical_outcome_ready_count", 0)
            if evaluator_plan is not None
            else 0
        ),
        "missing_canonical_outcome_count": (
            len(evaluator_plan.get("missing_canonical_outcomes", []))
            if evaluator_plan is not None
            else 0
        ),
        "excluded_scaffold_count": len(scaffold),
        "transient_file_count": len(transients),
        "checkpoint_runtime_bridge": checkpoint_bridge_summary,
        "issues": issues,
    }
    write_json(package_dir / "package_validation_report.json", validation)

    provenance = {
        "format": "ghostweb_slime_webenv_package_provenance.v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_result_dir": str(result_dir),
        "package_dir": str(package_dir),
        "delivery_surface": "slime_webenv_direct_package",
        "delivery_scope": delivery_scope,
        "training_ready": training_ready,
        "evaluation_enabled": evaluation_enabled,
        "replay_authority": replay_authority,
        "resource_manifest": "resource_manifest.json",
        "formal_artifact_manifest": "data/formal_artifact_manifest.json",
        "package_validation_report": "package_validation_report.json",
        "final_archive_status": "created" if finalize_archive else "deferred_until_finalize",
        "platform": environment_profile["platform"],
        "environment_profile_hash": environment_profile["profile_hash"],
        "mobile_runtime_enabled": mobile_runtime_enabled,
    }
    write_json(package_dir / "package_provenance.json", provenance)
    host_graph = {
        "format": "ghostweb_slime_package_host_graph.v1",
        "host_command": "t2w-serve --environment-dir <package_dir>",
        "resource_manifest": "resource_manifest.json",
        "app_root": "app",
        "tasks": "tasks.json",
        "runtime_profile": runtime_profile,
        "evaluation_enabled": evaluation_enabled,
    }
    if evaluation_enabled:
        host_graph["evaluator_plan"] = f"evaluators/{evaluator_plan_name}"
    elif replay_authority is not None:
        host_graph["replay_authority"] = replay_authority["plan"]
    if state_kernel_enabled:
        host_graph["state_service"] = resource_manifest["state_service"]
    write_json(package_dir / "data/package_host_graph.json", host_graph)
    if issues:
        raise RuntimeError(f"Package validation failed: {package_dir / 'package_validation_report.json'}")

    archive_path: Path | None = None
    if finalize_archive:
        archive_path = package_dir.parent / f"{package_dir.name}.tar.gz"
        _create_archive(package_dir, archive_path)
    return package_dir, archive_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a Trace2World result directory as a standalone environment package."
    )
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--env-name", required=True)
    parser.add_argument("--response-mode", default="env_text")
    parser.add_argument("--finalize-archive", action="store_true")
    parser.add_argument("--no-actionability-preflight", action="store_true")
    parser.add_argument(
        "--evaluator-export-mode",
        choices=sorted(EVALUATOR_EXPORT_MODES),
        default=EVALUATOR_EXPORT_REQUIRED,
    )
    parser.add_argument(
        "--preserve-source-bytes",
        action="store_true",
        help="Do not rewrite already materialized formal pages during export.",
    )
    args = parser.parse_args()
    package_dir, archive_path = build_package(
        result_dir=args.result_dir,
        package_dir=args.package_dir,
        env_name=args.env_name,
        response_mode=args.response_mode,
        actionability_preflight=not args.no_actionability_preflight,
        finalize_archive=args.finalize_archive,
        evaluator_export_mode=args.evaluator_export_mode,
        postprocess_pages=not args.preserve_source_bytes,
    )
    print(json.dumps({"package_dir": str(package_dir), "archive": str(archive_path) if archive_path else None}))


if __name__ == "__main__":
    main()
