from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized_executable_evaluators(
    executable: Any,
    *,
    manifest_path: Path,
) -> dict[str, dict[str, Any]]:
    raw = executable.get("evaluators") if isinstance(executable, dict) else None
    if isinstance(raw, dict):
        normalized: dict[str, dict[str, Any]] = {}
        for task_id, evaluator in raw.items():
            if not isinstance(evaluator, dict):
                raise ValueError(f"Invalid executable evaluator for {task_id}: {manifest_path}")
            normalized[str(task_id)] = evaluator
        return normalized
    if isinstance(raw, list):
        normalized = {}
        for index, evaluator in enumerate(raw):
            if not isinstance(evaluator, dict):
                raise ValueError(
                    f"Invalid executable evaluator row {index}: {manifest_path}"
                )
            task_id = str(evaluator.get("trace_id") or evaluator.get("task_id") or "").strip()
            if not task_id:
                raise ValueError(
                    f"Executable evaluator row {index} has no trace_id/task_id: {manifest_path}"
                )
            if task_id in normalized:
                raise ValueError(
                    f"Duplicate executable evaluator identity {task_id}: {manifest_path}"
                )
            normalized[task_id] = evaluator
        return normalized
    raise ValueError(
        "Missing executable evaluator authority: "
        f"{manifest_path} must contain a non-empty evaluators object or array"
    )


def _precompiled_browser_evaluator_authority(
    result_dir: Path,
    evaluator_ids: set[str],
) -> tuple[Path, dict[str, dict[str, Any]]] | None:
    evaluator_root = result_dir.parent / "evaluators"
    plan_path = evaluator_root / "browser_evaluator_plan.json"
    if not plan_path.is_file():
        return None
    plan = _read_json(plan_path)
    entries = plan.get("evaluators") if isinstance(plan, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"Invalid precompiled browser evaluator plan: {plan_path}")
    plan_ids = {str(task_id) for task_id in entries}
    missing = sorted(evaluator_ids - plan_ids)
    extra = sorted(plan_ids - evaluator_ids)


    non_formal_extra = [
        task_id
        for task_id in extra
        if isinstance(entries.get(task_id), dict)
        and entries[task_id].get("evaluation_portfolio_included") is False
        and entries[task_id].get("authoring_checkpoint_credit") is False
    ]
    unexpected_extra = sorted(set(extra) - set(non_formal_extra))
    if missing or unexpected_extra:
        raise ValueError(
            "Precompiled browser evaluator plan does not exactly cover the formal "
            f"evaluator population: missing={missing}, extra={unexpected_extra}, "
            f"non_formal_extra={sorted(non_formal_extra)}, plan={plan_path}"
        )
    return evaluator_root, {
        str(task_id): entry for task_id, entry in entries.items() if str(task_id) in evaluator_ids
    }


def _precompiled_evaluator_source(
    *,
    task_id: str,
    formal_evaluator: dict[str, Any],
    evaluator_root: Path,
    plan_entry: dict[str, Any],
) -> tuple[str, str, str]:
    evaluator_file = str(plan_entry.get("evaluator_file") or "").strip()
    if not evaluator_file or Path(evaluator_file).name != evaluator_file:
        raise ValueError(
            f"Invalid precompiled evaluator_file for {task_id}: {evaluator_file!r}"
        )
    evaluator_path = evaluator_root / evaluator_file
    if not evaluator_path.is_file():
        raise ValueError(f"Missing precompiled evaluator for {task_id}: {evaluator_path}")
    actual_digest = hashlib.sha256(evaluator_path.read_bytes()).hexdigest()
    declared_digest = str(plan_entry.get("evaluator_sha256") or "").strip()


    if declared_digest and (
        len(declared_digest) != 64
        or any(character not in "0123456789abcdef" for character in declared_digest.lower())
    ):
        raise ValueError(
            f"Invalid precompiled evaluator_sha256 for {task_id}: {declared_digest!r}"
        )
    if declared_digest and declared_digest != actual_digest:
        raise ValueError(
            f"Precompiled evaluator digest mismatch for {task_id}: "
            f"declared={declared_digest}, actual={actual_digest}, path={evaluator_path}"
        )
    formal_file = str(formal_evaluator.get("evaluator_file") or "").strip()
    formal_digest = str(formal_evaluator.get("evaluator_sha256") or "").strip()
    if bool(formal_file) != bool(formal_digest):
        raise ValueError(
            f"Incomplete formal evaluator byte binding for {task_id}: "
            f"evaluator_file={formal_file!r}, evaluator_sha256={formal_digest!r}"
        )
    formal_path_matches = formal_file in {
        evaluator_file,
        str(Path("evaluators") / evaluator_file),
    }
    if formal_file and (not formal_path_matches or formal_digest != actual_digest):
        raise ValueError(
            f"Formal/precompiled evaluator binding mismatch for {task_id}: "
            f"formal=({formal_file},{formal_digest}), "
            f"precompiled=({evaluator_file},{actual_digest})"
        )
    return evaluator_file, actual_digest, evaluator_path.read_text(encoding="utf-8")


def browser_evaluator_javascript(
    task_id: str,
    required: list[str],
    forbidden: list[str],
    source_result_dir: Path,
    evaluator_contract: dict[str, Any],
    accepted_trace_ids: list[str] | None = None,
) -> str:
    task_json = json.dumps(task_id, ensure_ascii=False)
    required_json = json.dumps(required, ensure_ascii=False)
    forbidden_json = json.dumps(forbidden, ensure_ascii=False)
    source_json = json.dumps(str(source_result_dir), ensure_ascii=False)
    contract_json = json.dumps(evaluator_contract, ensure_ascii=False)
    accepted_trace_json = json.dumps(
        sorted({str(value) for value in (accepted_trace_ids or [task_id]) if str(value)}),
        ensure_ascii=False,
    )
    return rf"""const TASK_ID = {task_json};
const REQUIRED = {required_json};
const FORBIDDEN = {forbidden_json};
const CONTRACT = {contract_json};
const ACCEPTED_TRACE_IDS = {accepted_trace_json};
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
function readStorageJson(key) {{
  const sessionValue = safeParse(safeStorageGet(key), null);
  return sessionValue !== null ? sessionValue : safeParse(safeLocalStorageGet(key), null);
}}
function runtimeStateSnapshot() {{
  try {{
    if (window.GhostwebRuntimeState && typeof window.GhostwebRuntimeState.snapshot === 'function') {{
      return window.GhostwebRuntimeState.snapshot() || null;
    }}
  }} catch (error) {{}}
  return readStorageJson('__ghostweb_runtime_state_v1__');
}}
function valueAtPath(value, path) {{
  const parts = String(path || '').split('.').filter(Boolean);
  let current = value;
  for (let index = 0; index < parts.length; index += 1) {{
    if (current == null || typeof current !== 'object') return undefined;
    const remaining = parts.slice(index).join('.');
    if (Object.prototype.hasOwnProperty.call(current, remaining)) return current[remaining];
    current = current[parts[index]];
  }}
  return current;
}}
function visibleNode(node) {{
  if (!node || !node.getBoundingClientRect) return false;
  const rect = node.getBoundingClientRect();
  const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
  return rect.width > 0 && rect.height > 0 &&
    (!style || (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'));
}}
function eventMeta(event) {{
  const meta = event && event.meta;
  return meta && typeof meta === 'object' ? meta : {{}};
}}
function eventPayload(event) {{
  const direct = event && event.payload;
  if (direct && typeof direct === 'object') return direct;
  const nested = eventMeta(event).payload;
  return nested && typeof nested === 'object' ? nested : {{}};
}}
function diffValue(event, field, channel) {{
  const meta = eventMeta(event);
  const payload = eventPayload(event);
  const names = channel === 'canonical'
    ? ['canonical_state_diff', 'canonical_diff']
    : channel === 'downstream'
      ? ['downstream_state_diff', 'downstream_diff']
      : ['state_diff', 'diff'];
  let diff = null;
  for (const source of [event, meta, payload]) {{
    if (!source || typeof source !== 'object') continue;
    for (const name of names) {{
      if (source[name] !== undefined && source[name] !== null) {{ diff = source[name]; break; }}
    }}
    if (diff !== null) break;
  }}
  if (typeof diff === 'string') diff = safeParse(diff, {{}});
  if (!diff || typeof diff !== 'object') diff = {{}};
  const value = valueAtPath(diff, field);
  if (value && typeof value === 'object' && Object.prototype.hasOwnProperty.call(value, 'after')) {{
    return value.after;
  }}
  return value;
}}
function projectionVisible(assertion) {{
  const kind = String(assertion && (assertion.kind || assertion.source) || '');
  if (kind === 'url_filename') {{
    const actual = decodeURIComponent(String(window.location.pathname || '').split('/').pop() || '');
    return matchesAssertion(actual, assertion);
  }}
  if (kind === 'storage_json') {{
    const state = readStorageJson(String(assertion.key || assertion.storage_key || ''));
    const actual = valueAtPath(state, assertion.path || assertion.field);
    return matchesAssertion(actual, assertion);
  }}
  if (kind === 'storage_string') {{
    return matchesAssertion(safeStorageGet(String(assertion.key || assertion.storage_key || '')), assertion);
  }}
  if (kind === 'runtime_state_field') {{
    const snapshot = runtimeStateSnapshot() || {{}};
    const scope = String(assertion.scope || 'current_state');
    const bucket = snapshot && typeof snapshot === 'object' ? snapshot[scope] : null;
    let actual = bucket && typeof bucket === 'object'
      ? bucket[String(assertion.state_ref || '')] : undefined;
    if (assertion.field && actual && typeof actual === 'object') actual = actual[String(assertion.field)];
    return matchesAssertion(actual, assertion);
  }}
  const expectedPage = String(assertion && assertion.page || '').trim();
  if (expectedPage) {{
    const currentPage = decodeURIComponent(String(window.location.pathname || '').split('/').pop() || '');
    if (currentPage !== expectedPage) return false;
  }}
  const selector = String(assertion && assertion.selector || '').trim();
  let nodes = [];
  try {{ nodes = selector ? Array.from(document.querySelectorAll(selector)) : [document.body]; }}
  catch (error) {{ return false; }}
  const visibleNodes = nodes.filter(visibleNode);
  if (kind === 'selector_visible' || assertion && assertion.op === 'rendered_presence') {{
    return visibleNodes.length > 0;
  }}
  if (kind === 'selector_absent_or_hidden') return visibleNodes.length === 0;
  const expected = assertion && assertion.value;
  if (assertion && assertion.attribute) {{
    return visibleNodes.some((node) => matchesAssertion(node.getAttribute(String(assertion.attribute)), assertion));
  }}
  if (assertion && assertion.field) {{
    const runtime = readStorageJson('__ghostweb_runtime_state_v1__') || {{}};
    const current = valueAtPath(runtime.current_state || runtime.committed_state || runtime, assertion.field);
    if (current !== undefined) return matchesAssertion(current, assertion);
  }}
  if (expected === undefined) return visibleNodes.length > 0;
  const haystack = visibleNodes.map((node) => String(node.innerText || node.textContent || '')).join(' ');
  return matchesAssertion(haystack, assertion);
}}
function validateProjectionAssertions(rule) {{
  const assertions = Array.isArray(rule && rule.projection_assertions)
    ? rule.projection_assertions : [];
  return assertions.map((assertion) => projectionVisible(assertion));
}}
function outcomeTerminalAssertions() {{
  const outcome = CONTRACT.outcome || {{}};
  const direct = outcome.browser_terminal_assertions && outcome.browser_terminal_assertions.assertions;
  if (Array.isArray(direct) && direct.length) return direct;
  const bindings = Array.isArray(outcome.terminal_observable_dom_bindings)
    ? outcome.terminal_observable_dom_bindings : [];
  return bindings.map((binding) => {{
    const mode = String(binding.match_mode || '');
    if (mode === 'rendered_presence') return {{kind: 'rendered_presence', selector: binding.selector}};
    if (mode === 'text_contains' || mode === 'text_equals') return {{
      kind: 'visible_text', selector: binding.selector, op: mode === 'text_equals' ? 'eq' : 'contains', value: binding.value
    }};
    if (mode === 'attribute_equals') return {{
      kind: 'visible_attribute', selector: binding.selector, attribute: binding.attribute, op: 'eq', value: binding.value
    }};
    return {{kind: 'unsupported_binding', match_mode: mode}};
  }});
}}
function validateTerminalAssertion(assertion) {{
  if (!assertion || typeof assertion !== 'object') return {{valid: false, reason: 'invalid_rule'}};
  const kind = String(assertion.kind || '');
  if (kind === 'url_filename') {{
    const actual = decodeURIComponent(String(window.location.pathname || '').split('/').pop() || '');
    return {{valid: matchesAssertion(actual, assertion), reason: 'url_filename_mismatch'}};
  }}
  if (['storage_json', 'storage_string', 'runtime_state_field'].includes(kind)) {{
    return {{valid: projectionVisible(assertion), reason: kind + '_mismatch'}};
  }}
  let nodes = [];
  try {{ nodes = Array.from(document.querySelectorAll(String(assertion.selector || 'body'))); }}
  catch (error) {{ return {{valid: false, reason: 'invalid_selector'}}; }}
  const visibleNodes = nodes.filter(visibleNode);
  if (kind === 'rendered_presence') return {{valid: visibleNodes.length > 0, reason: 'rendered_presence_mismatch'}};
  if (kind === 'visible_attribute') {{
    const actual = visibleNodes.map((node) => node.getAttribute(String(assertion.attribute || '')));
    return {{valid: actual.some((value) => matchesAssertion(value, assertion)), reason: 'visible_attribute_mismatch'}};
  }}
  if (kind === 'visible_text') {{
    const actual = visibleNodes.map((node) => String(node.innerText || node.textContent || '')).join(' ');
    const valid = matchesAssertion(actual, assertion);
    return {{valid, reason: 'visible_text_mismatch'}};
  }}
  return {{valid: false, reason: 'unsupported_kind:' + kind}};
}}
function negativeProbeStatus() {{
  const specs = [];
  Object.entries(CONTRACT.checkpoints || {{}}).forEach(([checkpointId, rule]) =>
    (rule.negative_probe_specs || []).forEach((spec) => specs.push({{checkpoint_id: checkpointId, rule, spec}})));
  const outcome = CONTRACT.outcome || {{}};
  (outcome.negative_probe_specs || []).forEach((spec) =>
    specs.push({{checkpoint_id: String(outcome.checkpoint_id || ''), rule: outcome, spec}}));
  if (!specs.length) return {{ required: 0, verified: 0, failed: 0, unverified: 0, results: [] }};
  const results = specs.map((entry) => executeNegativeProbe(entry));
  return {{ required: results.length, verified: results.filter((row) => row.status === 'passed').length,
    failed: results.filter((row) => row.status === 'failed').length,
    unverified: results.filter((row) => row.status === 'unverified').length, results }};
}}
function readEvents() {{
  const carriers = [];
  const sessionEvents = safeParse(safeStorageGet('__ghostweb_checkpoint_events_v1__'), null);
  const localEvents = safeParse(safeLocalStorageGet('__ghostweb_checkpoint_events_v1__'), null);
  if (Array.isArray(sessionEvents)) carriers.push(sessionEvents);
  if (Array.isArray(localEvents)) carriers.push(localEvents);
  if (window.GhostwebCheckpointRuntime && window.GhostwebCheckpointRuntime.eventLog) {{
    try {{
      const runtimeEvents = window.GhostwebCheckpointRuntime.eventLog();
      if (Array.isArray(runtimeEvents)) carriers.push(runtimeEvents);
    }} catch (error) {{}}
  }}
  try {{
    if (window.GhostwebRuntimeState && typeof window.GhostwebRuntimeState.snapshot === 'function') {{
      const snapshot = window.GhostwebRuntimeState.snapshot() || {{}};
      if (Array.isArray(snapshot.event_log)) carriers.push(snapshot.event_log);
      if (Array.isArray(snapshot.events)) carriers.push(snapshot.events);
    }}
  }} catch (error) {{}}
  const unique = new Map();
  carriers.flat().forEach((event) => {{
    if (!event || typeof event !== 'object') return;
    let key = '';
    try {{ key = JSON.stringify(event); }} catch (error) {{ key = String(unique.size); }}
    if (!unique.has(key)) unique.set(key, event);
  }});
  return Array.from(unique.values()).sort((left, right) => {{
    const leftTimestamp = Number(valueAtPath(left, 'timestamp') || valueAtPath(left, 'meta.timestamp') || 0);
    const rightTimestamp = Number(valueAtPath(right, 'timestamp') || valueAtPath(right, 'meta.timestamp') || 0);
    return leftTimestamp - rightTimestamp;
  }});
}}
const events = readEvents();
const matching = events.filter((event) => {{
  const eventTraceId = valueAt(event, 'trace_id');
  return event && (!eventTraceId || ACCEPTED_TRACE_IDS.includes(String(eventTraceId)));
}});
function valueAt(event, field) {{
  if (!event) return undefined;
  const payload = eventPayload(event);
  if (Object.prototype.hasOwnProperty.call(payload, field)) return payload[field];
  const meta = eventMeta(event);
  if (Object.prototype.hasOwnProperty.call(meta, field)) return meta[field];
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
  if (assertion.op === 'ne') return JSON.stringify(actual) !== JSON.stringify(assertion.value);
  if (assertion.op === 'numeric_eq') return Number(actual) === Number(assertion.value);
  if (assertion.op === 'numeric_lte') return Number(actual) <= Number(assertion.value);
  if (assertion.op === 'numeric_gte') return Number(actual) >= Number(assertion.value);
  if (assertion.op === 'contains' || assertion.op === 'contains_visible') {{
    return String(actual || '').includes(String(assertion.value || ''));
  }}
  if (assertion.op === 'not_contains') {{
    return !String(actual || '').includes(String(assertion.value || ''));
  }}
  if (assertion.op === 'matches_regex') {{
    try {{ return new RegExp(String(assertion.value || '')).test(String(actual || '')); }}
    catch (error) {{ return false; }}
  }}
  if (assertion.op === 'is_true') return actual === true || String(actual) === 'true';
  if (assertion.op === 'is_false') return actual === false || String(actual) === 'false';
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
  const assertionGroups = [
    ['payload', rule.payload_assertions || {{}}, (field) => valueAt(event, field)],
    ['state', rule.state_assertions || {{}}, (field) => valueAt(event, field)],
    ['diff', rule.diff_assertions || {{}}, (field) => diffValue(event, field, 'state')],
    ['canonical_diff', rule.canonical_diff_assertions || {{}}, (field) => diffValue(event, field, 'canonical')],
    ['downstream_diff', rule.downstream_diff_assertions || {{}}, (field) => diffValue(event, field, 'downstream')],
  ];
  assertionGroups.forEach(([channel, assertions, read]) => Object.keys(assertions).forEach((field) => {{
    if (!matchesAssertion(read(field), assertions[field])) reasons.push(channel + '_assertion_failed:' + field);
  }}));
  validateProjectionAssertions(rule).forEach((passed) => {{
    if (!passed) reasons.push('projection_assertion_failed');
  }});
  return {{ valid: reasons.length === 0, reasons }};
}}
function cloned(value) {{
  try {{ return JSON.parse(JSON.stringify(value)); }} catch (error) {{ return null; }}
}}
function diffObjects(event) {{
  const payload = eventPayload(event);
  const meta = eventMeta(event);
  const objects = [];
  for (const source of [event, meta, payload]) {{
    if (!source || typeof source !== 'object') continue;
    for (const name of ['state_diff', 'diff', 'canonical_state_diff', 'canonical_diff', 'downstream_state_diff', 'downstream_diff']) {{
      let value = source[name];
      if (typeof value === 'string') value = safeParse(value, null);
      if (value && typeof value === 'object') objects.push(value);
    }}
  }}
  return objects;
}}
function mutateOwnPath(target, field, value, remove) {{
  if (!target || typeof target !== 'object') return;
  if (Object.prototype.hasOwnProperty.call(target, field)) {{
    if (remove) delete target[field]; else target[field] = value;
    return;
  }}
  const parts = String(field || '').split('.').filter(Boolean);
  let current = target;
  for (let index = 0; index < parts.length - 1; index += 1) {{
    const remaining = parts.slice(index).join('.');
    if (Object.prototype.hasOwnProperty.call(current, remaining)) {{
      if (remove) delete current[remaining]; else current[remaining] = value;
      return;
    }}
    if (!current[parts[index]] || typeof current[parts[index]] !== 'object') return;
    current = current[parts[index]];
  }}
  if (!parts.length) return;
  const leaf = parts[parts.length - 1];
  if (Object.prototype.hasOwnProperty.call(current, leaf)) {{
    if (remove) delete current[leaf]; else current[leaf] = value;
  }}
}}
function mutateEventField(event, field, value, remove) {{
  const payload = eventPayload(event);
  const meta = eventMeta(event);
  for (const source of [event, meta, payload]) mutateOwnPath(source, field, value, remove);
  for (const diff of diffObjects(event)) {{
    const current = valueAtPath(diff, field);
    if (current && typeof current === 'object' && Object.prototype.hasOwnProperty.call(current, 'after')) {{
      if (remove) mutateOwnPath(diff, field, undefined, true);
      else current.after = value;
    }} else {{
      mutateOwnPath(diff, field, remove ? undefined : {{after: value}}, remove);
    }}
  }}
}}
function probeFields(spec, rule) {{
  const mutate = spec && spec.mutate && typeof spec.mutate === 'object' ? spec.mutate : {{}};
  if (Array.isArray(mutate.__remove_fields__)) return mutate.__remove_fields__.map(String);
  if (Array.isArray(mutate.__wrong_fields__)) return mutate.__wrong_fields__.map(String);
  const direct = Object.keys(mutate).filter((field) => !field.startsWith('__'));
  if (direct.length) return direct;
  const remove = String(mutate.__remove__ || '');
  const probe = String(mutate.__probe__ || '');
  const assertionGroups = [
    rule.payload_assertions || {{}},
    rule.state_assertions || {{}},
    rule.diff_assertions || {{}},
    rule.canonical_diff_assertions || {{}},
    rule.downstream_diff_assertions || {{}},
  ];
  const assertedFields = Array.from(new Set(assertionGroups.flatMap((group) => Object.keys(group))));
  if (remove === 'missing_state_read') {{
    // A probe may only mutate an input that validateEvent() consumes.  Older
    // contracts named `missing_state_read` even when `state_reads` existed
    // only in a declarative assertion channel; hardcoding that field made an
    // unbound mutation look executable and vetoed otherwise valid episodes.
    return assertedFields.filter((field) => String(field) === 'state_reads');
  }}
  if (remove === 'missing_route_projection' || probe === 'wrong_route_target') {{
    // Route negative probes must mutate a field that validateEvent() actually
    // consumes.  Contracts commonly bind the host-observed payload field
    // `observed_target_filename`; mutating the similarly named unasserted
    // `target_filename` makes an effective no-op look like an evaluator that
    // accepted an invalid route and incorrectly vetoes an otherwise valid
    // episode.  Resolve the target field from the declared assertion groups.
    const assertedTargets = Array.from(new Set(assertionGroups.flatMap((group) =>
      Object.keys(group).filter((field) => String(field).endsWith('target_filename'))
    )));
    return assertedTargets;
  }}
  const asserted = Object.keys(rule.diff_assertions || {{}});
  if (remove === 'missing_state_write' || ['wrong_state_tuple', 'wrong_query_value', 'wrong_same_page_state'].includes(probe)) {{
    return asserted;
  }}
  return asserted;
}}
function executeNegativeProbe(entry) {{
  const checkpointId = String(entry.checkpoint_id || '');
  const spec = entry.spec || {{}};
  const probeId = String(spec.probe_id || 'unnamed');
  const result = {{checkpoint_id: checkpointId, probe_id: probeId, execution: 'inline_contract_mutation'}};
  if (checkpointId === outcomeId) {{
    if (spec.mutate && spec.mutate.__remove_event__) {{
      return {{...result, status: processContractPassed && terminalContractPassed ? 'passed' : 'unverified'}};
    }}
    if (Array.isArray(spec.forbidden_events) && spec.forbidden_events.length) {{
      const detectable = spec.forbidden_events.some((eventName) =>
        FORBIDDEN.some((forbidden) => String(eventName).toLowerCase().includes(String(forbidden).toLowerCase())));
      return {{...result, status: detectable ? 'passed' : 'failed'}};
    }}
    const assertionIndex = Number(spec.terminal_assertion_index);
    if (Number.isInteger(assertionIndex) && terminalAssertions[assertionIndex]) {{
      const assertion = terminalAssertions[assertionIndex];
      const mutate = spec.mutate || {{}};
      const candidate = mutate.__remove_terminal_assertion__ ? undefined : mutate.__wrong_terminal_assertion__;
      const rejected = !matchesAssertion(candidate, assertion);
      return {{...result, terminal_assertion_index: assertionIndex, status: rejected ? 'passed' : 'failed'}};
    }}
    return {{...result, status: 'unverified', reason: 'outcome_probe_has_no_browser_assertion_binding'}};
  }}
  const projectionAssertionIndex = Number(spec.projection_assertion_index);
  const projectionAssertions = Array.isArray(entry.rule && entry.rule.projection_assertions)
    ? entry.rule.projection_assertions : [];
  if (Number.isInteger(projectionAssertionIndex) && projectionAssertions[projectionAssertionIndex]) {{
    const assertion = projectionAssertions[projectionAssertionIndex];
    const mutate = spec.mutate || {{}};
    const candidate = mutate.__remove_projection_assertion__
      ? undefined : mutate.__wrong_projection_assertion__;
    const rejected = !matchesAssertion(candidate, assertion);
    return {{...result, projection_assertion_index: projectionAssertionIndex,
      status: rejected ? 'passed' : 'failed'}};
  }}
  const candidates = matching.filter((event) => String(valueAt(event, 'checkpoint_id') || '') === checkpointId);
  const source = candidates.find((event) => validateEvent(checkpointId, event).valid);
  if (!source) return {{...result, status: 'unverified', reason: 'positive_event_missing_or_invalid'}};
  const mutated = cloned(source);
  if (!mutated) return {{...result, status: 'unverified', reason: 'event_clone_failed'}};
  const mutate = spec.mutate || {{}};
  const remove = Boolean(mutate.__remove__) || Array.isArray(mutate.__remove_fields__);
  const fields = probeFields(spec, entry.rule || {{}});
  if (!fields.length) return {{...result, status: 'unverified', reason: 'mutation_field_unbound'}};
  fields.forEach((field) => {{
    const direct = Object.prototype.hasOwnProperty.call(mutate, field) ? mutate[field] : '__ghostweb_negative_probe_wrong__';
    mutateEventField(mutated, field, direct, remove);
  }});
  const rejected = !validateEvent(checkpointId, mutated).valid;
  return {{...result, mutated_fields: fields, status: rejected ? 'passed' : 'failed'}};
}}
const assertionSatisfied = new Set();
const firstValidEventIndex = new Map();
const stateBackedCheckpointIds = new Set();
const invalid = {{}};
const outcomeId = String((CONTRACT.outcome || {{}}).checkpoint_id || '');
const checkpointDependencies = Object.fromEntries(REQUIRED.map((checkpoint) => {{
  const rule = checkpointContract(checkpoint) || {{}};
  return [checkpoint, Array.isArray(rule.depends_on) ? rule.depends_on.map(String) : []];
}}));
REQUIRED.forEach((checkpoint) => {{
  if (checkpoint === outcomeId) return;
  const rule = checkpointContract(checkpoint) || {{}};
  const stateProjectionResults = validateProjectionAssertions(rule);
  if (!rule.required_event && stateProjectionResults.length > 0 && stateProjectionResults.every(Boolean)) {{
    assertionSatisfied.add(checkpoint);
    stateBackedCheckpointIds.add(checkpoint);
    return;
  }}
  const candidates = matching.filter((event) => String(valueAt(event, 'checkpoint_id') || '') === checkpoint);
  for (const event of candidates) {{
    const result = validateEvent(checkpoint, event);
    if (result.valid) {{
      assertionSatisfied.add(checkpoint);
      firstValidEventIndex.set(checkpoint, matching.indexOf(event));
      break;
    }}
    invalid[checkpoint] = result.reasons;
  }}
  if (!candidates.length) {{
    invalid[checkpoint] = stateProjectionResults.length
      ? ['app_owned_state_or_dom_assertion_failed'] : ['event_missing'];
  }}
}});
const dependencyMemo = new Map();
function dependencySatisfied(checkpoint, stack) {{
  if (dependencyMemo.has(checkpoint)) return dependencyMemo.get(checkpoint);
  if (stack.includes(checkpoint)) {{
    invalid[checkpoint] = Array.from(new Set((invalid[checkpoint] || []).concat(['dependency_cycle'])));
    dependencyMemo.set(checkpoint, false);
    return false;
  }}
  if (!assertionSatisfied.has(checkpoint)) {{
    dependencyMemo.set(checkpoint, false);
    return false;
  }}
  const dependencies = checkpointDependencies[checkpoint] || [];
  const passed = dependencies.every((dependency) => {{
    if (!Object.prototype.hasOwnProperty.call(checkpointDependencies, dependency)) {{
      invalid[checkpoint] = Array.from(new Set((invalid[checkpoint] || [])
        .concat(['missing_dependency_rule:' + dependency])));
      return false;
    }}
    const dependencyPassed = dependencySatisfied(dependency, stack.concat([checkpoint]));
    if (!dependencyPassed) {{
      invalid[checkpoint] = Array.from(new Set((invalid[checkpoint] || [])
        .concat(['dependency_failed:' + dependency])));
    }}
    return dependencyPassed;
  }});
  dependencyMemo.set(checkpoint, passed);
  return passed;
}}
const valid = new Set();
REQUIRED.forEach((checkpoint) => {{
  if (checkpoint !== outcomeId && dependencySatisfied(checkpoint, [])) valid.add(checkpoint);
}});
const terminalAssertions = outcomeTerminalAssertions();
const terminalResults = terminalAssertions.map(validateTerminalAssertion);
const blockingProcessIds = Array.isArray((CONTRACT.outcome || {{}}).blocking_process_checkpoints)
  ? CONTRACT.outcome.blocking_process_checkpoints.map(String)
  : REQUIRED.filter((checkpoint) => checkpoint !== outcomeId);
const processContractPassed = blockingProcessIds.every((checkpoint) => valid.has(checkpoint));
const terminalContractPassed = terminalAssertions.length > 0 && terminalResults.every((result) => result.valid);
if (outcomeId) {{
  const outcomeCandidates = matching.filter((event) => String(valueAt(event, 'checkpoint_id') || '') === outcomeId);
  let explicitOutcomePassed = false;
  for (const event of outcomeCandidates) {{
    if (validateEvent(outcomeId, event).valid) {{
      explicitOutcomePassed = true;
      firstValidEventIndex.set(outcomeId, matching.indexOf(event));
      break;
    }}
  }}
  const observedTerminalPolicy = String((CONTRACT.outcome || {{}}).terminal_policy || CONTRACT.terminal_policy || '') === 'observed_terminal_state';
  if (!processContractPassed) {{
    invalid[outcomeId] = ['outcome_process_contract_failed'];
  }} else if (!terminalContractPassed) {{
    invalid[outcomeId] = terminalAssertions.length
      ? terminalResults.filter((result) => !result.valid).map((result) => result.reason)
      : ['missing_browser_terminal_assertions'];
  }} else if (explicitOutcomePassed || (observedTerminalPolicy && outcomeCandidates.length === 0)) {{
    assertionSatisfied.add(outcomeId);
    valid.add(outcomeId);
  }} else {{
    invalid[outcomeId] = outcomeCandidates.length ? ['invalid_outcome_event'] : ['event_missing'];
  }}
}}
const missing = REQUIRED.filter((checkpoint) => !valid.has(checkpoint));
const negativeProbe = negativeProbeStatus();
// Inline negative mutations audit the evaluator contract; they are not
// observations of the current business episode.  A probe that was actually
// executed and accepted an invalid mutation is a real evaluator defect and
// may veto reward.  An unverified row only says this positive episode did not
// carry the action-level source event needed to execute that audit mutation;
// its closure belongs to the independent supervision probe denominator.
const negativeProbePassed = negativeProbe.failed === 0;
const negativeProbeExecutionComplete = negativeProbe.unverified === 0;
const forbiddenHit = matching.some((event) => {{
  const haystack = ['checkpoint_id', 'action_id', 'event', 'event_name', 'name', 'type']
    .map((field) => valueAt(event, field)).filter(Boolean).join(' ').toLowerCase();
  return FORBIDDEN.some((name) => haystack.includes(String(name).toLowerCase()));
}});
const groundedScore = REQUIRED.length ? (REQUIRED.length - missing.length) / REQUIRED.length : 0;
const evaluatorInvalid = forbiddenHit || negativeProbe.failed > 0;
const score = evaluatorInvalid ? 0 : groundedScore;
const dependencyEdgeCount = Object.values(checkpointDependencies)
  .reduce((total, dependencies) => total + dependencies.length, 0);
const checkpointRewardVector = REQUIRED.map((checkpoint) => {{
  const hasRuntimeEventAnchor = firstValidEventIndex.has(checkpoint);
  const stateBackedCheckpoint = stateBackedCheckpointIds.has(checkpoint)
    || (checkpoint === outcomeId && valid.has(checkpoint) && !hasRuntimeEventAnchor);
  return {{
    checkpoint_id: checkpoint,
    reached: valid.has(checkpoint),
    payload_valid: hasRuntimeEventAnchor && assertionSatisfied.has(checkpoint),
    state_observation_valid: stateBackedCheckpoint,
    checkpoint_dependencies: checkpointDependencies[checkpoint] || [],
    credit_assignment_mode: dependencyEdgeCount > 0
      ? 'declared_dependency_dag_coverage' : 'grounded_event_state_coverage',
    first_event_index: hasRuntimeEventAnchor ? firstValidEventIndex.get(checkpoint) : null,
    first_event_index_source: hasRuntimeEventAnchor
      ? 'runtime_event_log'
      : stateBackedCheckpoint
        ? 'not_applicable_app_owned_storage_runtime_dom'
        : 'runtime_event_not_observed',
    reward_contribution: valid.has(checkpoint) && REQUIRED.length ? 1 / REQUIRED.length : 0,
  }};
}});
const checkpointEventTimeline = matching.map((event, eventIndex) => ({{
  event_index: eventIndex,
  checkpoint_id: String(valueAt(event, 'checkpoint_id') || ''),
  task_id: String(valueAt(event, 'task_id') || ''),
  trace_id: String(valueAt(event, 'trace_id') || ''),
  event_type: String(valueAt(event, 'event_type') || valueAt(event, 'type') || ''),
}})).filter((row) => REQUIRED.includes(row.checkpoint_id));
const terminalSuccess = missing.length === 0 && !evaluatorInvalid;
window.__ghostweb_last_eval_result__ = {{
  task_id: TASK_ID,
  required: REQUIRED,
  missing,
  seen: Array.from(valid),
  invalid,
  event_count: matching.length,
  forbiddenHit,
  browser_terminal_assertions: terminalResults,
  process_contract_passed: processContractPassed,
  terminal_contract_passed: terminalContractPassed,
  outcome_evidence_source: valid.has(outcomeId)
    ? 'blocking_process_event_or_state_coverage_and_browser_terminal_assertions' : null,
  negative_probe: negativeProbe,
  negative_probe_gate_passed: negativeProbePassed,
  negative_probe_execution_complete: negativeProbeExecutionComplete,
  negative_probe_unverified_is_diagnostic_only: true,
  negative_probe_gate_applied_to_scalar: true,
  checkpoint_dependencies: checkpointDependencies,
  checkpoint_order_mode: dependencyEdgeCount > 0
    ? 'declared_dependency_dag_coverage' : 'grounded_event_state_coverage',
  checkpoint_order_semantics: 'presentation_only_except_explicit_dependency_edges',
  ordered_checkpoint_prefix_length: null,
  ordered_checkpoint_total: REQUIRED.length,
  grounded_checkpoint_coverage_count: valid.size,
  checkpoint_reward_vector: checkpointRewardVector,
  checkpoint_state: Object.fromEntries(
    REQUIRED.map((checkpoint) => [checkpoint, valid.has(checkpoint)])),
  checkpoint_event_timeline: checkpointEventTimeline,
  raw_reward: score,
  terminal_success: terminalSuccess,
  evaluator_scalar_terminal_veto: evaluatorInvalid,
  checkpoint_state_source: 'app_owned_storage_runtime_dom',
  nonzero_checkpoint_reached: valid.size > 0,
  ability_reward_vector: {{
    state_progress_score: groundedScore,
    terminal_success: terminalSuccess,
    safety_violation: forbiddenHit,
  }},
  env_valid: true,
  format_valid: true,
  score,
  source_result_dir: {source_json}
}};
if (evaluatorInvalid) return 0.0;
return score;
"""


def compile_browser_evaluator_specs(result_dir: Path) -> dict[str, dict[str, Any]]:
    result_dir = result_dir.resolve()
    manifest_path = (
        result_dir
        / "phase_post_processing"
        / "evaluators"
        / "executable_evaluator_manifest.json"
    )
    executable = _read_json(manifest_path) or {}
    evaluators = _normalized_executable_evaluators(
        executable,
        manifest_path=manifest_path,
    )
    if not evaluators:
        raise ValueError(f"Executable evaluator population is empty: {manifest_path}")

    evaluator_ids = {str(task_id) for task_id in evaluators}
    precompiled_authority = _precompiled_browser_evaluator_authority(
        result_dir,
        evaluator_ids,
    )

    specs: dict[str, dict[str, Any]] = {}
    for task_id, evaluator in sorted(evaluators.items()):
        if not isinstance(evaluator, dict):
            raise ValueError(f"Invalid executable evaluator for {task_id}")
        checkpoints = evaluator.get("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
        required = [
            str(checkpoint_id)
            for checkpoint_id, checkpoint in checkpoints.items()
            if isinstance(checkpoint, dict) and checkpoint.get("blocking", True)
        ]
        outcome = evaluator.get("outcome") if isinstance(evaluator.get("outcome"), dict) else {}
        outcome_id = str(outcome.get("checkpoint_id") or "").strip()
        if outcome_id and outcome.get("blocking", True) and outcome_id not in required:
            required.append(outcome_id)
        if not required:
            raise ValueError(f"Browser evaluator has no required checkpoints for {task_id}")
        forbidden = sorted(
            {
                str(event)
                for event in outcome.get("forbidden_events", [])
                if str(event).strip()
            }
        )
        source_plan_entry: dict[str, Any] = {}
        evaluator_file = f"{task_id}.js"
        evaluator_sha256: str | None = None
        evaluation_logic: str
        if precompiled_authority is not None:
            evaluator_root, precompiled_entries = precompiled_authority
            entry = precompiled_entries[str(task_id)]
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid precompiled evaluator plan entry for {task_id}")
            source_plan_entry = dict(entry)
            evaluator_file, evaluator_sha256, evaluation_logic = _precompiled_evaluator_source(
                task_id=str(task_id),
                formal_evaluator=evaluator,
                evaluator_root=evaluator_root,
                plan_entry=entry,
            )
            plan_required = entry.get("required_checkpoints")
            if not isinstance(plan_required, list) or not plan_required:
                raise ValueError(
                    f"Precompiled browser evaluator has no required checkpoints for {task_id}"
                )
            required = [str(value) for value in plan_required if str(value)]
            forbidden = sorted(
                {
                    str(event)
                    for event in entry.get("forbidden_events", [])
                    if str(event).strip()
                }
            )
        else:
            evaluation_logic = browser_evaluator_javascript(
                str(task_id),
                required,
                forbidden,
                result_dir,
                evaluator,
                accepted_trace_ids=[str(task_id)],
            )
        specs[str(task_id)] = {
            "task_id": str(task_id),
            "evaluator_file": evaluator_file,
            "evaluator_sha256": evaluator_sha256,
            "required_checkpoints": required,
            "forbidden_events": forbidden,
            "terminal_policy": source_plan_entry.get(
                "terminal_policy", evaluator.get("terminal_policy")
            ),
            "source_plan_entry": source_plan_entry,
            "evaluation_logic": evaluation_logic,
        }
    return specs


def compile_hosted_browser_evaluator_spec(
    result_dir: Path,
    *,
    hosted_task_id: str,
    source_task_id: str,
) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    manifest_path = result_dir / "phase_post_processing/evaluators/executable_evaluator_manifest.json"
    executable = _read_json(manifest_path) or {}
    evaluators = _normalized_executable_evaluators(
        executable,
        manifest_path=manifest_path,
    )
    if source_task_id not in evaluators:
        raise ValueError(f"Missing source executable evaluator for {source_task_id}: {manifest_path}")
    evaluator = evaluators[source_task_id]
    if not isinstance(evaluator, dict):
        raise ValueError(f"Invalid source executable evaluator for {source_task_id}")
    checkpoints = evaluator.get("checkpoints", {})
    if not isinstance(checkpoints, dict):
        checkpoints = {}
    required = [
        str(checkpoint_id)
        for checkpoint_id, checkpoint in checkpoints.items()
        if isinstance(checkpoint, dict) and checkpoint.get("blocking", True)
    ]
    outcome = evaluator.get("outcome") if isinstance(evaluator.get("outcome"), dict) else {}
    outcome_id = str(outcome.get("checkpoint_id") or "").strip()
    if outcome_id and outcome.get("blocking", True) and outcome_id not in required:
        required.append(outcome_id)
    if not required:
        raise ValueError(f"Hosted browser evaluator has no blocking checkpoints for {source_task_id}")
    forbidden = sorted({str(event) for event in outcome.get("forbidden_events", []) if str(event).strip()})
    accepted_trace_ids = sorted({str(hosted_task_id), str(source_task_id)})
    return {
        "task_id": str(hosted_task_id),
        "source_reference_task_id": str(source_task_id),
        "accepted_trace_ids": accepted_trace_ids,
        "required_checkpoints": required,
        "forbidden_events": forbidden,
        "terminal_policy": evaluator.get("terminal_policy"),
        "evaluation_logic": browser_evaluator_javascript(
            str(hosted_task_id),
            required,
            forbidden,
            result_dir,
            evaluator,
            accepted_trace_ids=accepted_trace_ids,
        ),
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def materialize_hosted_browser_evaluator(
    result_dir: Path,
    package_root: Path,
    *,
    hosted_task_id: str,
    source_task_id: str,
) -> dict[str, Any]:
    package_root = package_root.resolve()
    spec = compile_hosted_browser_evaluator_spec(
        result_dir,
        hosted_task_id=hosted_task_id,
        source_task_id=source_task_id,
    )
    evaluator_path = package_root / "evaluators" / f"{hosted_task_id}.js"
    evaluator_path.parent.mkdir(parents=True, exist_ok=True)
    evaluator_path.write_text(str(spec["evaluation_logic"]), encoding="utf-8")
    digest = hashlib.sha256(evaluator_path.read_bytes()).hexdigest()

    if hosted_task_id == source_task_id:
        manifest_path = (
            result_dir
            / "phase_post_processing/evaluators/executable_evaluator_manifest.json"
        )
        executable = _read_json(manifest_path) or {}
        raw_evaluators = executable.get("evaluators") if isinstance(executable, dict) else None
        matched = False
        if isinstance(raw_evaluators, dict):
            row = raw_evaluators.get(source_task_id)
            if isinstance(row, dict):
                row["evaluator_file"] = str(Path("evaluators") / evaluator_path.name)
                row["evaluator_sha256"] = digest
                matched = True
        elif isinstance(raw_evaluators, list):
            for row in raw_evaluators:
                if not isinstance(row, dict):
                    continue
                identity = str(row.get("trace_id") or row.get("task_id") or "")
                if identity != source_task_id:
                    continue
                row["evaluator_file"] = str(Path("evaluators") / evaluator_path.name)
                row["evaluator_sha256"] = digest
                matched = True
                break
        if not matched:
            raise ValueError(
                f"Missing formal evaluator byte-binding row: {manifest_path} -> {source_task_id}"
            )
        _write_json(manifest_path, executable)

    plan_path = package_root / "evaluators/browser_evaluator_plan.json"
    plan = _read_json(plan_path) or {}
    plans = plan.get("evaluators") if isinstance(plan, dict) else None
    if not isinstance(plans, dict) or hosted_task_id not in plans:
        raise ValueError(f"Missing hosted evaluator plan entry: {plan_path} -> {hosted_task_id}")
    previous_plan = dict(plans[hosted_task_id])
    plans[hosted_task_id] = {
        **previous_plan,
        "task_id": hosted_task_id,
        "evaluator_file": evaluator_path.name,
        "evaluator_sha256": digest,
        "required_checkpoints": spec["required_checkpoints"],
        "forbidden_events": spec["forbidden_events"],
        "terminal_policy": spec.get("terminal_policy"),
        "source_reference_task_id": source_task_id,
        "accepted_trace_ids": spec["accepted_trace_ids"],
    }
    _write_json(plan_path, plan)

    runtime_path = package_root / "evaluators/evaluator_runtime_manifest.json"
    runtime = _read_json(runtime_path) or {}
    runtime_evaluators = runtime.get("evaluators") if isinstance(runtime, dict) else None
    if not isinstance(runtime_evaluators, dict) or hosted_task_id not in runtime_evaluators:
        raise ValueError(f"Missing hosted runtime evaluator entry: {runtime_path} -> {hosted_task_id}")
    runtime_evaluators[hosted_task_id] = {
        **dict(runtime_evaluators[hosted_task_id]),
        "evaluator_file": evaluator_path.name,
        "sha256": digest,
        "required_checkpoints": spec["required_checkpoints"],
        "source_reference_task_id": source_task_id,
        "accepted_trace_ids": spec["accepted_trace_ids"],
    }
    _write_json(runtime_path, runtime)
    return {**spec, "evaluator_sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile a current browser checkpoint evaluator")
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--hosted-task-id")
    parser.add_argument("--source-task-id")
    args = parser.parse_args()
    if bool(args.hosted_task_id) != bool(args.source_task_id):
        parser.error("--hosted-task-id and --source-task-id must be provided together")
    if args.package_root and args.hosted_task_id and args.source_task_id:
        result = materialize_hosted_browser_evaluator(
            args.result_dir,
            args.package_root,
            hosted_task_id=args.hosted_task_id,
            source_task_id=args.source_task_id,
        )
    else:
        result = compile_browser_evaluator_specs(args.result_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
