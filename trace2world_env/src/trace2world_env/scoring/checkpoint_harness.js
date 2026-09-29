(() => {
  const configuredRuntimeEventContract = @@RUNTIME_EVENT_CONTRACT_JSON@@;
  const configuredCheckpointEvidenceContract = @@CHECKPOINT_EVIDENCE_CONTRACT_JSON@@;
  const readCommonStorageJsonSource = (source) => {
    if (!source || typeof source !== 'object') {
      return {status: 'not_declared', raw: null, snapshot: null};
    }
    const storage = source.kind === 'session_storage_json'
      ? window.sessionStorage
      : (source.kind === 'local_storage_json'
        ? window.localStorage : null);
    if (!storage || typeof storage.getItem !== 'function') {
      return {status: 'source_unavailable', raw: null, snapshot: null};
    }
    let raw = null;
    try { raw = storage.getItem(source.key); } catch (_) {
      return {status: 'read_error', raw: null, snapshot: null};
    }
    if (typeof raw !== 'string') {
      return {status: 'missing', raw: null, snapshot: null};
    }
    let parsed = null;
    try { parsed = JSON.parse(raw); } catch (_) {
      return {status: 'parse_error', raw, snapshot: null};
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {status: 'invalid_root', raw, snapshot: null};
    }
    try {
      return {
        status: 'valid', raw,
        snapshot: JSON.parse(JSON.stringify(parsed)),
      };
    } catch (_) {
      return {status: 'clone_error', raw, snapshot: null};
    }
  };
  const readCommonRuntimeEventLog = () => {
    const key = '__sim_checkpoint_events_v1__';
    const candidates = [
      ['session_storage_json', window.sessionStorage],
      ['local_storage_json', window.localStorage],
    ];
    let persisted = null;
    for (const [source, storage] of candidates) {
      if (!storage || typeof storage.getItem !== 'function') continue;
      let raw = null;
      try { raw = storage.getItem(key); } catch (_) {
        return {status: 'read_error', source, raw: null, events: []};
      }
      if (typeof raw !== 'string') continue;
      let parsed = null;
      try { parsed = JSON.parse(raw); } catch (_) {
        return {status: 'parse_error', source, raw, events: []};
      }
      if (!Array.isArray(parsed)) {
        return {status: 'invalid_root', source, raw, events: []};
      }
      try {
        persisted = {source, events: JSON.parse(JSON.stringify(parsed))};
      } catch (_) {
        return {status: 'clone_error', source, raw, events: []};
      }
      break;
    }
    let volatile = null;
    if (window.SimCheckpointRuntime
        && typeof window.SimCheckpointRuntime.eventLog === 'function') {
      try {
        const events = window.SimCheckpointRuntime.eventLog();
        if (!Array.isArray(events)) return {status: 'invalid_root',
          source: 'runtime_event_log_api', raw: null, events: []};
        volatile = JSON.parse(JSON.stringify(events));
      } catch (_) {
        return {status: 'read_error', source: 'runtime_event_log_api',
          raw: null, events: []};
      }
    }
    if (!persisted && !volatile) {
      return {status: 'missing', source: null, raw: null, events: []};
    }
    const merged = [];
    const seen = new Set();
    // Both persisted and in-memory event logs may carry their identity
    // only in payload.  Deduplicate a provable copy, never an anonymous
    // row: otherwise an unrelated event with five empty envelope fields
    // can erase a later payload-only checkpoint observation.
    const stableEventJson = (value) => {
      if (value === null) return 'null';
      if (Array.isArray(value)) return '['
        + value.map(stableEventJson).join(',') + ']';
      if (typeof value === 'object') return '{'
        + Object.keys(value).sort().map((key) =>
          JSON.stringify(key) + ':' + stableEventJson(value[key]))
          .join(',') + '}';
      return JSON.stringify(value);
    };
    const eventCopyIdentity = (event) => {
      if (!event || typeof event !== 'object' || Array.isArray(event))
        return null;
      const payload = event.payload && typeof event.payload === 'object'
        && !Array.isArray(event.payload) ? event.payload : {};
      const eventId = event.event_id == null
        ? payload.event_id : event.event_id;
      if (eventId != null && String(eventId))
        return 'event_id:' + JSON.stringify(String(eventId));
      const fields = ['trace_id', 'checkpoint_id', 'action_id',
        'sequence', 'timestamp'];
      const hasIdentitySignal = fields.some((field) => {
        const value = event[field] == null ? payload[field] : event[field];
        return value != null && String(value) !== '';
      });
      if (!hasIdentitySignal) return null;
      try { return 'canonical:' + stableEventJson(event); } catch (_) {
        return null;
      }
    };
    [persisted && persisted.events || [], volatile || []].forEach((rows) => {
      rows.forEach((event) => {
        const identity = eventCopyIdentity(event);
        if (identity === null) { merged.push(event); return; }
        if (seen.has(identity)) return;
        seen.add(identity); merged.push(event);
      });
    });
    const raw = JSON.stringify(merged);
    return {status: 'valid',
      source: persisted && volatile ? persisted.source + '+runtime_event_log_api'
        : (persisted ? persisted.source : 'runtime_event_log_api'),
      raw, events: merged};
  };
  const checkpointSnapshotSource = configuredCheckpointEvidenceContract
    && configuredCheckpointEvidenceContract.snapshot_source;
  const configuredV1Adapter = configuredRuntimeEventContract
    && configuredRuntimeEventContract.runtime_event_adapters
    && configuredRuntimeEventContract.runtime_event_adapters['1'];
  const v1AdapterSnapshotSource = configuredV1Adapter
    && configuredV1Adapter.snapshot_source;
  const checkpointSnapshotBefore = readCommonStorageJsonSource(
    checkpointSnapshotSource);
  const v1AdapterEnvelopeBefore = readCommonStorageJsonSource(
    v1AdapterSnapshotSource);
  const runtimeEventLogBefore = readCommonRuntimeEventLog();
  window.__sim_last_eval_result__ = null;
  let raw_result = (() => {
@@JS_CODE@@
  })();
  const checkpointSnapshotAfter = readCommonStorageJsonSource(
    checkpointSnapshotSource);
  const v1AdapterEnvelopeAfter = readCommonStorageJsonSource(
    v1AdapterSnapshotSource);
  const runtimeEventLogAfter = readCommonRuntimeEventLog();
  const checkpointSnapshotStable =
    checkpointSnapshotBefore.status === 'valid'
    && checkpointSnapshotAfter.status === 'valid'
    && checkpointSnapshotBefore.raw === checkpointSnapshotAfter.raw;
  const v1AdapterEnvelopeStable =
    v1AdapterEnvelopeBefore.status === 'valid'
    && v1AdapterEnvelopeAfter.status === 'valid'
    && v1AdapterEnvelopeBefore.raw === v1AdapterEnvelopeAfter.raw;
  const runtimeEventLogStable = (
    runtimeEventLogBefore.status === 'missing'
      && runtimeEventLogAfter.status === 'missing')
    || (runtimeEventLogBefore.status === 'valid'
      && runtimeEventLogAfter.status === 'valid'
      && runtimeEventLogBefore.source === runtimeEventLogAfter.source
      && runtimeEventLogBefore.raw === runtimeEventLogAfter.raw);
  const commonCheckpointSnapshot = checkpointSnapshotStable
    ? checkpointSnapshotAfter.snapshot : null;
  const commonV1AdapterEnvelope = v1AdapterEnvelopeStable
    ? v1AdapterEnvelopeAfter.snapshot : null;
  const commonRuntimeEvents = runtimeEventLogStable
    && runtimeEventLogAfter.status === 'valid'
    ? runtimeEventLogAfter.events : [];
  let details = (raw_result && typeof raw_result === 'object')
    ? raw_result
    : (window.__sim_last_eval_result__ || null);
  const configuredRequired = @@REQUIRED_JSON@@;
  const configuredDiagnostic = @@DIAGNOSTIC_JSON@@;
  const configuredTerminalRequired = @@TERMINAL_JSON@@;
  const configuredCheckpointDependencies = @@DEPENDENCIES_JSON@@;
  const configuredCheckpointWeights = @@WEIGHTS_JSON@@;
  const configuredZeroWeightAssertionIds = @@ZERO_WEIGHT_ASSERTION_IDS_JSON@@;
  const configuredTerminalSolutionPolicy = @@SOLUTION_POLICY_JSON@@;
  const configuredTaskId = @@TASK_ID_JSON@@;
  const configuredSourceReferenceTaskId = @@SOURCE_REFERENCE_TASK_ID_JSON@@;
  const requiredFromDetails = details && Array.isArray(details.required)
    ? details.required.map(String) : [];
  const reportedVector = details && Array.isArray(details.checkpoint_reward_vector)
    ? details.checkpoint_reward_vector : [];
  const vectorCheckpointProjection = reportedVector.length
    ? Object.fromEntries(reportedVector.map((row) => [
      String(row && row.checkpoint_id || ''), Boolean(row && row.reached)
    ]).filter(([checkpointId]) => checkpointId)) : null;
  const producerCheckpointVectorProjectionLedger = reportedVector
    .map((row) => ({
      checkpoint_id: String(row && row.checkpoint_id || ''),
      reached: row && Object.prototype.hasOwnProperty.call(row, 'reached')
        ? row.reached : null,
      reward_weight: row
        && Object.prototype.hasOwnProperty.call(row, 'reward_weight')
        ? row.reward_weight : null,
      reward_contribution: row
        && Object.prototype.hasOwnProperty.call(row, 'reward_contribution')
        ? row.reward_contribution : null,
      earned: row && Object.prototype.hasOwnProperty.call(row, 'earned')
        ? row.earned : null,
    })).filter((row) => row.checkpoint_id);
  // Producer maps and reward vectors are output projections only.
  // Their `reached` or self-reported validity flags never establish
  // checkpoint credit in the common reducer.
  const explicitStateCandidates = [
    details && details.checkpoints,
    details && details.checkpoint_state,
    details && details.dependency_gated_checkpoint_state,
    details && details.checkpoint_outcomes,
  ];
  const producerCheckpointState = explicitStateCandidates.find((candidate) =>
    candidate && typeof candidate === 'object' && !Array.isArray(candidate)
  ) || null;
  const reportedSeen = new Set(details && Array.isArray(details.seen)
    ? details.seen.map(String) : []);
  const reportedMissing = new Set(details && Array.isArray(details.missing)
    ? details.missing.map(String) : []);
  const reportedInvalid = new Set(details && details.invalid
    && typeof details.invalid === 'object'
    ? Object.entries(details.invalid).filter(([, reasons]) =>
      Array.isArray(reasons) ? reasons.length > 0 : Boolean(reasons))
      .map(([checkpointId]) => String(checkpointId)) : []);
  const required = configuredRequired.length ? configuredRequired
    : (requiredFromDetails.length ? requiredFromDetails
      : (producerCheckpointState ? Object.keys(producerCheckpointState)
        : (vectorCheckpointProjection
          ? Object.keys(vectorCheckpointProjection) : [])));
  if (details && required.length) {
    const runtimeEventFormalAuthorityDeclared = Boolean(
      configuredRuntimeEventContract
      && configuredRuntimeEventContract.contract_schema
        === 'trace2world.runtime-event-contract.v2');
    const events = runtimeEventFormalAuthorityDeclared
      ? commonRuntimeEvents
      : (runtimeEventLogAfter.status === 'valid'
        ? runtimeEventLogAfter.events : []);
    const taskId = String(configuredTaskId || '');
    const sourceReferenceTaskId = String(
      configuredSourceReferenceTaskId || '');
    const eventPayload = (event) => event && event.payload
      && typeof event.payload === 'object' ? event.payload : {};
    const firstDefined = (...values) => {
      for (const value of values) {
        if (value !== null && value !== undefined) return value;
      }
      return null;
    };
    const eventTaskId = (event) => String(
      event && (event.task_id || eventPayload(event).task_id) || '');
    const eventTraceId = (event) => String(
      event && (event.trace_id || eventPayload(event).trace_id) || '');
    const eventCheckpointId = (event) => String(
      event && (event.checkpoint_id
        || eventPayload(event).checkpoint_id) || '');
    const eventContractType = (event) => String(
      event && (event.contract_type
        || eventPayload(event).contract_type) || '');
    const allowedEventIdentities = new Set([
      taskId, sourceReferenceTaskId
    ].filter(Boolean));
    const validateEventIdentity = (event) => {
      const payload = event && typeof event.payload === 'object'
        ? event.payload : {};
      const identities = [
        event && event.task_id, payload.task_id,
        event && event.trace_id, payload.trace_id,
      ].map((value) => String(value || '')).filter(Boolean);
      if (!identities.length) {
        return {valid: false, reason: 'unbound_event_identity'};
      }
      if (!allowedEventIdentities.size
          || identities.some((identity) =>
            !allowedEventIdentities.has(identity))) {
        return {valid: false, reason: 'event_identity_mismatch'};
      }
      return {valid: true, reason: null};
    };
    const eventIdentityRows = (Array.isArray(events) ? events : [])
      .map((event, rawEventIndex) => ({
        event, raw_event_index: rawEventIndex,
        identity: validateEventIdentity(event),
      }));
    const matchingRows = eventIdentityRows.filter((row) =>
      row.identity.valid);
    const matching = matchingRows.map((row) => row.event);

    const isPlainRecord = (value) => Boolean(value)
      && typeof value === 'object' && !Array.isArray(value);
    const canonicalClone = (value) => JSON.parse(JSON.stringify(value));
    const canonicalStable = (value) => {
      if (value === null || value === undefined) return JSON.stringify(value);
      if (Array.isArray(value)) return '[' + value.map(canonicalStable).join(',') + ']';
      if (typeof value === 'object') return '{' + Object.keys(value).sort()
        .map((key) => JSON.stringify(key) + ':' + canonicalStable(value[key])).join(',') + '}';
      return JSON.stringify(value);
    };
    const canonicalStateHash = (value) => {
      const text = canonicalStable(value);
      let first = 2166136261 >>> 0;
      let second = 2246822519 >>> 0;
      for (let index = 0; index < text.length; index += 1) {
        const code = text.charCodeAt(index);
        first = Math.imul(first ^ code, 16777619) >>> 0;
        second = Math.imul(
          second ^ (code + ((index & 255) << 8)), 3266489917
        ) >>> 0;
      }
      const hex = (number) => ('00000000' + (number >>> 0).toString(16)).slice(-8);
      return 'appstate-fnv1a-v1:' + hex(first) + hex(second);
    };
    const canonicalEqual = (left, right) =>
      canonicalStable(left) === canonicalStable(right);
    const pointerTokens = (pointer) => {
      if (typeof pointer !== 'string' || !pointer.startsWith('/')) return null;
      return pointer.slice(1).split('/').map((token) =>
        token.replace(/~1/g, '/').replace(/~0/g, '~'));
    };
    const resolvePointer = (root, pointer) => {
      const tokens = pointerTokens(pointer);
      if (!tokens || !tokens.length) return {found: false, value: null, tokens: []};
      let current = root;
      for (const token of tokens) {
        if ((isPlainRecord(current) || Array.isArray(current))
            && Object.prototype.hasOwnProperty.call(current, token)) {
          current = current[token];
        } else {
          return {found: false, value: null, tokens};
        }
      }
      return {found: true, value: current, tokens};
    };
    const canonicalSet = (value) => Array.isArray(value)
      ? Array.from(new Set(value.map(canonicalStable))).sort() : null;
    const typedContains = (actual, expected) => {
      if (typeof actual === 'string' && typeof expected === 'string') {
        return {typed: true, contains: actual.includes(expected)};
      }
      if (Array.isArray(actual)) {
        return {
          typed: true,
          contains: actual.some((value) => canonicalEqual(value, expected)),
        };
      }
      return {typed: false, contains: false};
    };
    const compilePlanRegex = (expected) => {
      if (!isPlainRecord(expected)
          || typeof expected.pattern !== 'string'
          || typeof expected.flags !== 'string') return null;
      try {
        return new RegExp(expected.pattern, expected.flags);
      } catch (_) {
        return null;
      }
    };
    const validateEvidencePredicate = (resolved, predicate, expected) => {
      if (predicate === 'exists') {
        return resolved.found === expected;
      }
      if (!resolved.found) return false;
      if (predicate === 'eq') return canonicalEqual(resolved.value, expected);
      if (predicate === 'contains') {
        const result = typedContains(resolved.value, expected);
        return result.typed && result.contains;
      }
      if (predicate === 'not_contains') {
        const result = typedContains(resolved.value, expected);
        return result.typed && !result.contains;
      }
      if (['numeric_eq', 'numeric_lte', 'numeric_gte'].includes(predicate)) {
        if (typeof resolved.value !== 'number' || !Number.isFinite(resolved.value)
            || typeof expected !== 'number' || !Number.isFinite(expected)) {
          return false;
        }
        if (predicate === 'numeric_eq') return resolved.value === expected;
        if (predicate === 'numeric_lte') return resolved.value <= expected;
        return resolved.value >= expected;
      }
      if (predicate === 'matches_regex') {
        if (typeof resolved.value !== 'string' || resolved.value.length > 4096) {
          return false;
        }
        const compiled = compilePlanRegex(expected);
        return compiled !== null && compiled.test(resolved.value);
      }
      if (predicate === 'set_equals') {
        const actualSet = canonicalSet(resolved.value);
        const expectedSet = canonicalSet(expected);
        return actualSet !== null && expectedSet !== null
          && canonicalEqual(actualSet, expectedSet);
      }
      return false;
    };
    const finiteNumber = (value) =>
      typeof value === 'number' && Number.isFinite(value);
    const evaluateStateExpression = (expression, root, budget) => {
      const activeBudget = budget || {remaining: 64};
      activeBudget.remaining -= 1;
      const invalid = (reason) => ({ok: false, value: null, reason});
      const valid = (value) => ({ok: true, value, reason: null});
      if (activeBudget.remaining < 0 || !isPlainRecord(expression)) {
        return invalid('expression_budget_or_shape_invalid');
      }
      const op = String(expression.op || '');
      if (op === 'pointer') {
        const resolved = resolvePointer(root, expression.path);
        return resolved.found
          ? valid(resolved.value) : invalid('expression_pointer_missing');
      }
      if (op === 'literal') return valid(expression.value);
      const args = Array.isArray(expression.args) ? expression.args : [];
      const evaluatedArgs = args.map((arg) =>
        evaluateStateExpression(arg, root, activeBudget));
      if (evaluatedArgs.some((row) => !row.ok)) {
        return invalid(evaluatedArgs.find((row) => !row.ok).reason);
      }
      const values = evaluatedArgs.map((row) => row.value);
      const allNumbers = values.every(finiteNumber);
      if (op === 'not') {
        return typeof values[0] === 'boolean'
          ? valid(!values[0]) : invalid('expression_type_mismatch');
      }
      if (op === 'object_entries') {
        if (!isPlainRecord(values[0]) || Object.keys(values[0]).length > 1024) {
          return invalid('expression_type_or_bound_mismatch');
        }
        return valid(Object.keys(values[0]).sort().map((key) => ({
          key, value: values[0][key],
        })));
      }
      if (op === 'at') {
        const collection = typeof values[0] === 'string'
          ? Array.from(values[0]) : values[0];
        const index = values[1];
        if (!Array.isArray(collection) || collection.length > 4096
            || !Number.isSafeInteger(index) || index < 0
            || index >= collection.length) {
          return invalid('expression_index_or_bound_invalid');
        }
        return valid(collection[index]);
      }
      if (op === 'and' || op === 'or') {
        if (!values.length || !values.every((value) => typeof value === 'boolean')) {
          return invalid('expression_type_mismatch');
        }
        return valid(op === 'and'
          ? values.every(Boolean) : values.some(Boolean));
      }
      if (op === 'eq' || op === 'neq') {
        const equal = canonicalEqual(values[0], values[1]);
        return valid(op === 'eq' ? equal : !equal);
      }
      if (['lt', 'lte', 'gt', 'gte'].includes(op)) {
        if (!allNumbers) return invalid('expression_type_mismatch');
        if (op === 'lt') return valid(values[0] < values[1]);
        if (op === 'lte') return valid(values[0] <= values[1]);
        if (op === 'gt') return valid(values[0] > values[1]);
        return valid(values[0] >= values[1]);
      }
      if (op === 'approx_eq') {
        if (!allNumbers || values[2] < 0) {
          return invalid('expression_type_mismatch');
        }
        return valid(Math.abs(values[0] - values[1]) <= values[2]);
      }
      if (op === 'contains' || op === 'not_contains') {
        const result = typedContains(values[0], values[1]);
        if (!result.typed) return invalid('expression_type_mismatch');
        return valid(op === 'contains' ? result.contains : !result.contains);
      }
      if (op === 'one_of') {
        if (!Array.isArray(values[1]) || values[1].length > 2048) {
          return invalid('expression_type_mismatch');
        }
        return valid(values[1].some((row) => canonicalEqual(values[0], row)));
      }
      if (op === 'set_equals' || op === 'set_subset') {
        const left = canonicalSet(values[0]);
        const right = canonicalSet(values[1]);
        if (left === null || right === null
            || values[0].length > 2048 || values[1].length > 2048) {
          return invalid('expression_type_mismatch');
        }
        if (op === 'set_equals') return valid(canonicalEqual(left, right));
        const rightSet = new Set(right);
        return valid(left.every((value) => rightSet.has(value)));
      }
      if (op === 'prefix' || op === 'suffix') {
        if (typeof values[0] === 'string' && typeof values[1] === 'string') {
          return valid(op === 'prefix'
            ? values[0].startsWith(values[1]) : values[0].endsWith(values[1]));
        }
        if (Array.isArray(values[0]) && Array.isArray(values[1])
            && values[0].length <= 2048 && values[1].length <= 2048) {
          if (values[1].length > values[0].length) return valid(false);
          const offset = op === 'prefix' ? 0 : values[0].length - values[1].length;
          return valid(values[1].every((value, index) =>
            canonicalEqual(values[0][offset + index], value)));
        }
        return invalid('expression_type_mismatch');
      }
      if (op === 'ordered_subsequence') {
        let sequence = values[0];
        let subsequence = values[1];
        if (typeof sequence === 'string' && typeof subsequence === 'string') {
          sequence = Array.from(sequence); subsequence = Array.from(subsequence);
        }
        if (!Array.isArray(sequence) || !Array.isArray(subsequence)
            || sequence.length > 2048 || subsequence.length > 2048) {
          return invalid('expression_type_mismatch');
        }
        let cursor = 0;
        for (const value of sequence) {
          if (cursor < subsequence.length
              && canonicalEqual(value, subsequence[cursor])) cursor += 1;
        }
        return valid(cursor === subsequence.length);
      }
      if (op === 'length') {
        const value = values[0];
        if (typeof value === 'string') return valid(Array.from(value).length);
        if (Array.isArray(value)) return valid(value.length);
        if (isPlainRecord(value)) return valid(Object.keys(value).length);
        return invalid('expression_type_mismatch');
      }
      if (op === 'type_is') {
        const value = values[0];
        const actualType = value === null ? 'null'
          : (Array.isArray(value) ? 'array' : typeof value);
        const typeValid = expression.expected_type !== 'number'
          || finiteNumber(value);
        return valid(typeValid && actualType === expression.expected_type);
      }
      if (op === 'normalize_string') {
        if (typeof values[0] !== 'string' || values[0].length > 4096) {
          return invalid('expression_type_mismatch');
        }
        return valid(values[0].normalize('NFKC').trim().toLowerCase()
          .replace(/\s+/gu, ' '));
      }
      if (op === 'matches_regex') {
        if (typeof values[0] !== 'string' || values[0].length > 4096) {
          return invalid('expression_type_mismatch');
        }
        const regex = compilePlanRegex(expression);
        return regex ? valid(regex.test(values[0]))
          : invalid('expression_regex_compile_failed');
      }
      if (['add', 'sub', 'mul', 'div', 'mod', 'abs', 'min', 'max', 'round']
          .includes(op)) {
        let numericValues = values;
        if ((op === 'min' || op === 'max') && values.length === 1
            && Array.isArray(values[0])) numericValues = values[0];
        if (!numericValues.length || !numericValues.every(finiteNumber)) {
          return invalid('expression_type_mismatch');
        }
        if (op === 'add') return valid(numericValues.reduce((a, b) => a + b, 0));
        if (op === 'mul') return valid(numericValues.reduce((a, b) => a * b, 1));
        if (op === 'sub') return valid(numericValues[0] - numericValues[1]);
        if (op === 'div') return numericValues[1] !== 0
          ? valid(numericValues[0] / numericValues[1])
          : invalid('expression_division_by_zero');
        if (op === 'mod') return numericValues[1] !== 0
          ? valid(numericValues[0] % numericValues[1])
          : invalid('expression_division_by_zero');
        if (op === 'abs') return valid(Math.abs(numericValues[0]));
        if (op === 'min') return valid(Math.min(...numericValues));
        if (op === 'max') return valid(Math.max(...numericValues));
        const digits = numericValues[1];
        if (!Number.isInteger(digits) || digits < -12 || digits > 12) {
          return invalid('expression_round_digits_invalid');
        }
        const factor = 10 ** digits;
        const rounded = digits >= 0
          ? Math.round((numericValues[0] + Number.EPSILON) * factor) / factor
          : Math.round(numericValues[0] / (10 ** -digits)) * (10 ** -digits);
        return finiteNumber(rounded) ? valid(rounded)
          : invalid('expression_numeric_overflow');
      }
      if (op === 'is_prime') {
        const value = values[0];
        if (!Number.isSafeInteger(value) || value < 2 || value > 1000000000) {
          return invalid('expression_type_or_bound_mismatch');
        }
        if (value % 2 === 0) return valid(value === 2);
        for (let divisor = 3; divisor * divisor <= value; divisor += 2) {
          if (value % divisor === 0) return valid(false);
        }
        return valid(true);
      }
      if (op === 'digit_sum') {
        let digits = null;
        if (Number.isSafeInteger(values[0])) digits = String(Math.abs(values[0]));
        if (typeof values[0] === 'string' && /^[0-9]+$/.test(values[0])
            && values[0].length <= 256) digits = values[0];
        return digits === null ? invalid('expression_type_or_bound_mismatch')
          : valid(Array.from(digits).reduce((sum, digit) => sum + Number(digit), 0));
      }
      if (['argmin', 'argmax', 'argclosest'].includes(op)) {
        const collection = values[0];
        const target = op === 'argclosest' ? values[1] : null;
        if (!Array.isArray(collection) || !collection.length
            || collection.length > 1024
            || (op === 'argclosest' && !finiteNumber(target))) {
          return invalid('expression_collection_invalid');
        }
        const rows = [];
        for (const item of collection) {
          if (!isPlainRecord(item)) return invalid('expression_collection_invalid');
          const scored = resolvePointer(item, expression.value_pointer);
          const selected = resolvePointer(item, expression.select_pointer);
          if (!scored.found || !selected.found || !finiteNumber(scored.value)) {
            return invalid('expression_collection_pointer_invalid');
          }
          const score = op === 'argclosest'
            ? Math.abs(scored.value - target) : scored.value;
          rows.push({score, selected: selected.value});
        }
        const best = op === 'argmax'
          ? Math.max(...rows.map((row) => row.score))
          : Math.min(...rows.map((row) => row.score));
        const selectedValues = [];
        const seenValues = new Set();
        rows.filter((row) => row.score === best).forEach((row) => {
          const key = canonicalStable(row.selected);
          if (!seenValues.has(key)) {
            seenValues.add(key); selectedValues.push(row.selected);
          }
        });
        return valid(selectedValues);
      }
      return invalid('expression_operator_unsupported');
    };
    const deriveTopLevelStateDiff = (before, after) => {
      if (!isPlainRecord(before) || !isPlainRecord(after)) return null;
      const diff = {};
      const keys = Array.from(new Set([
        ...Object.keys(before), ...Object.keys(after)
      ])).sort();
      for (const key of keys) {
        if (!Object.prototype.hasOwnProperty.call(before, key)
            || !Object.prototype.hasOwnProperty.call(after, key)) return null;
        if (!canonicalEqual(before[key], after[key])) {
          diff[key] = {
            before: canonicalClone(before[key]),
            after: canonicalClone(after[key]),
          };
        }
      }
      return diff;
    };
    const validateAndApplyStateDiff = (before, after, stateDiff) => {
      const reasons = [];
      if (!isPlainRecord(stateDiff)) {
        return {valid: false, reasons: ['invalid_state_diff'], applied: null};
      }
      const keys = Object.keys(stateDiff);
      if (!keys.length) {
        return {valid: false, reasons: ['empty_state_diff'], applied: null};
      }
      const applied = canonicalClone(before);
      for (const key of keys) {
        const row = stateDiff[key];
        if (!isPlainRecord(row)
            || !Object.prototype.hasOwnProperty.call(row, 'before')
            || !Object.prototype.hasOwnProperty.call(row, 'after')
            || !Object.prototype.hasOwnProperty.call(before, key)
            || !Object.prototype.hasOwnProperty.call(after, key)) {
          reasons.push('state_diff_not_applicable');
          continue;
        }
        if (!canonicalEqual(before[key], row.before)) {
          reasons.push('state_diff_before_mismatch');
        }
        if (canonicalEqual(row.before, row.after)) {
          reasons.push('state_diff_noop');
        }
        applied[key] = canonicalClone(row.after);
      }
      if (!canonicalEqual(applied, after)) reasons.push('state_diff_after_mismatch');
      return {
        valid: reasons.length === 0,
        reasons: Array.from(new Set(reasons)),
        applied,
      };
    };
    const validateCheckpointObservation = (event, checkpointId, after, stateDiff) => {
      const configuredBindings = configuredRuntimeEventContract
        && configuredRuntimeEventContract.checkpoint_observation_bindings;
      const bindings = configuredBindings
        && Array.isArray(configuredBindings[checkpointId])
        ? configuredBindings[checkpointId] : [];
      if (!bindings.length) {
        return {valid: false, reasons: ['missing_checkpoint_observation_binding']};
      }
      const payload = isPlainRecord(event && event.payload) ? event.payload : {};
      const valid = bindings.some((binding) => {
        const evidenceRoot = binding.evidence_source === 'event' ? event : payload;
        const evidence = resolvePointer(evidenceRoot, binding.evidence_pointer);
        const state = resolvePointer(after, binding.state_pointer);
        const topLevelStateKey = state.tokens.length ? state.tokens[0] : '';
        return evidence.found && state.found
          && Object.prototype.hasOwnProperty.call(stateDiff, topLevelStateKey)
          && canonicalEqual(evidence.value, state.value);
      });
      return valid
        ? {valid: true, reasons: []}
        : {valid: false, reasons: ['checkpoint_observation_binding_mismatch']};
    };
    const runtimeEventSchemaVersion = (event) => {
      const payload = isPlainRecord(event && event.payload) ? event.payload : {};
      const envelopeDeclared = Object.prototype.hasOwnProperty.call(
        event || {}, 'runtime_event_schema_version');
      const payloadDeclared = Object.prototype.hasOwnProperty.call(
        payload, 'runtime_event_schema_version');
      const envelope = envelopeDeclared
        ? Number(event.runtime_event_schema_version) : null;
      const payloadVersion = payloadDeclared
        ? Number(payload.runtime_event_schema_version) : null;
      if (envelopeDeclared && payloadDeclared && envelope !== payloadVersion) {
        return {version: null, declared: true, conflict: true};
      }
      const selected = envelopeDeclared ? envelope
        : (payloadDeclared ? payloadVersion : 1);
      return {
        version: Number.isInteger(selected) && selected > 0 ? selected : null,
        declared: envelopeDeclared || payloadDeclared,
        conflict: false,
      };
    };
    const validateRuntimeEvent = (event, eventIndex) => {
      const checkpointId = eventCheckpointId(event);
      const version = runtimeEventSchemaVersion(event);
      const base = {
        event_index: eventIndex,
        checkpoint_id: checkpointId,
        input_runtime_event_schema_version: version.version,
        normalized_runtime_event_schema_version: null,
        version_declared: version.declared,
        adapter_id: null,
        evidence_source: 'runtime_event_log',
        status: 'invalid_event',
        reasons: [],
      };
      if (version.conflict) {
        base.reasons = ['runtime_event_schema_version_conflict'];
        return base;
      }
      if (version.version === null) {
        base.reasons = ['invalid_runtime_event_schema_version'];
        return base;
      }
      if (version.version !== 1 && version.version !== 2) {
        base.status = 'producer_schema_incompatible';
        base.reasons = ['unsupported_runtime_event_schema_version'];
        return base;
      }
      const legacyAdapter = version.version === 1
        ? (configuredRuntimeEventContract
          && configuredRuntimeEventContract.runtime_event_adapters
          && configuredRuntimeEventContract.runtime_event_adapters['1'])
        : null;
      if (version.version === 1 && !legacyAdapter) {
        base.status = 'producer_schema_incompatible';
        base.reasons = ['missing_v1_adapter'];
        return base;
      }
      let formalEvent = event;
      if (version.version === 1) {
        if (!v1AdapterEnvelopeStable || !isPlainRecord(commonV1AdapterEnvelope)) {
          base.status = 'producer_schema_incompatible';
          const sourceStatus = v1AdapterEnvelopeBefore.status !== 'valid'
            ? v1AdapterEnvelopeBefore.status : v1AdapterEnvelopeAfter.status;
          base.reasons = [
            v1AdapterEnvelopeBefore.status === 'valid'
              && v1AdapterEnvelopeAfter.status === 'valid'
              ? 'evaluator_side_effect'
              : 'v1_adapter_snapshot_source_' + sourceStatus,
          ];
          return base;
        }
        const adapterEvents = Array.isArray(commonV1AdapterEnvelope.events)
          ? commonV1AdapterEnvelope.events : [];
        const candidates = adapterEvents.filter((candidate) =>
          eventCheckpointId(candidate) === checkpointId
          && validateEventIdentity(candidate).valid
          && runtimeEventSchemaVersion(candidate).version === 1);
        if (candidates.length !== 1) {
          base.status = 'producer_schema_incompatible';
          base.reasons = [candidates.length
            ? 'ambiguous_v1_adapter_event' : 'missing_v1_adapter_event'];
          return base;
        }
        formalEvent = candidates[0];
        base.evidence_source = 'common_owned_runtime_event_envelope';
      }
      const payload = isPlainRecord(formalEvent && formalEvent.payload)
        ? formalEvent.payload : null;
      if (!payload || !checkpointId
          || String(payload.checkpoint_id || '') !== checkpointId) {
        base.reasons = ['invalid_checkpoint_payload_identity'];
        return base;
      }
      const before = formalEvent && formalEvent.state_snapshot_before;
      const after = formalEvent && formalEvent.state_snapshot_after;
      if (!isPlainRecord(before) || !isPlainRecord(after)) {
        if (version.version === 1) base.status = 'producer_schema_incompatible';
        base.reasons = [version.version === 1
          ? 'v1_adapter_missing_app_owned_state_snapshot'
          : 'missing_state_snapshot'];
        return base;
      }
      if (version.version === 1) {
        const normalizedDiff = deriveTopLevelStateDiff(before, after);
        if (!normalizedDiff || !Object.keys(normalizedDiff).length) {
          base.reasons = ['empty_normalized_state_diff'];
          return base;
        }
        const observation = validateCheckpointObservation(
          formalEvent, checkpointId, after, normalizedDiff
        );
        if (!observation.valid) {
          base.reasons = observation.reasons;
          return base;
        }
        base.normalized_runtime_event_schema_version = 2;
        base.adapter_id = legacyAdapter.adapter_id;
        base.state_before_hash_recomputed = canonicalStateHash(before);
        base.state_after_hash_recomputed = canonicalStateHash(after);
        base.status = 'valid';
        return base;
      }
      base.normalized_runtime_event_schema_version = 2;
      const reasons = [];
      if (String(event.state_hash_algorithm || '') !== 'appstate-fnv1a-v1') {
        reasons.push('unsupported_state_hash_algorithm');
      }
      const beforeHash = canonicalStateHash(before);
      const afterHash = canonicalStateHash(after);
      base.state_before_hash_recomputed = beforeHash;
      base.state_after_hash_recomputed = afterHash;
      if (String(event.state_before_hash || '') !== beforeHash) {
        reasons.push('state_before_hash_mismatch');
      }
      if (String(event.state_after_hash || '') !== afterHash) {
        reasons.push('state_after_hash_mismatch');
      }
      const diffValidation = validateAndApplyStateDiff(
        before, after, event && event.state_diff
      );
      reasons.push(...diffValidation.reasons);
      if (diffValidation.valid) {
        const observation = validateCheckpointObservation(
          event, checkpointId, after, event.state_diff
        );
        reasons.push(...observation.reasons);
      }
      base.reasons = Array.from(new Set(reasons));
      if (!base.reasons.length) base.status = 'valid';
      return base;
    };

    const producerDiagnostic = details && details.supervision_contract
      && Array.isArray(details.supervision_contract.diagnostic_domain)
      ? details.supervision_contract.diagnostic_domain.map(String) : [];
    const diagnosticDomain = new Set([
      ...configuredDiagnostic, ...producerDiagnostic
    ]);
    const taskCheckpointDomain = new Set([
      ...required, ...Array.from(diagnosticDomain)
    ]);
    const legacyValidEventByCheckpoint = {};
    const legacyDuplicateCounts = {};
    const legacyOutOfDomain = [];
    const runtimeEventValidationLedger = [];
    const producerSchemaIncompatibleCheckpointIds = new Set();
    const runtimeEventInvalidCheckpointIds = new Set();
    eventIdentityRows.forEach((identityRow) => {
      const event = identityRow.event;
      const checkpointId = eventCheckpointId(event);
      if (!taskCheckpointDomain.has(checkpointId)) {
        if (checkpointId) legacyOutOfDomain.push(checkpointId);
        return;
      }
      if (!runtimeEventFormalAuthorityDeclared) {
        runtimeEventValidationLedger.push({
          event_index: identityRow.raw_event_index,
          checkpoint_id: checkpointId,
          input_runtime_event_schema_version:
            runtimeEventSchemaVersion(event).version,
          normalized_runtime_event_schema_version: null,
          version_declared: runtimeEventSchemaVersion(event).declared,
          adapter_id: null,
          evidence_source: 'runtime_event_log',
          status: 'diagnostic_only',
          reasons: ['undeclared_runtime_event_contract'],
        });
        return;
      }
      const eventIndex = matchingRows.findIndex((row) =>
        row.raw_event_index === identityRow.raw_event_index);
      if (!identityRow.identity.valid) {
        runtimeEventValidationLedger.push({
          event_index: identityRow.raw_event_index,
          checkpoint_id: checkpointId,
          input_runtime_event_schema_version:
            runtimeEventSchemaVersion(event).version,
          normalized_runtime_event_schema_version: null,
          version_declared: runtimeEventSchemaVersion(event).declared,
          adapter_id: null,
          status: 'producer_schema_incompatible',
          reasons: [identityRow.identity.reason],
        });
        producerSchemaIncompatibleCheckpointIds.add(checkpointId);
        return;
      }
      const validation = validateRuntimeEvent(event, eventIndex);
      runtimeEventValidationLedger.push(validation);
      if (validation.status === 'producer_schema_incompatible') {
        producerSchemaIncompatibleCheckpointIds.add(checkpointId);
        return;
      }
      if (validation.status !== 'valid') {
        runtimeEventInvalidCheckpointIds.add(checkpointId);
        return;
      }
      if (!required.includes(checkpointId)) return;
      if (legacyValidEventByCheckpoint[checkpointId]) {
        legacyDuplicateCounts[checkpointId] =
          (legacyDuplicateCounts[checkpointId] || 0) + 1;
        return;
      }
      legacyValidEventByCheckpoint[checkpointId] = {event, eventIndex};
    });
    const runtimeEventSchemaVersionsSeen = Array.from(new Set(
      runtimeEventValidationLedger.map((row) =>
        row.input_runtime_event_schema_version)
        .filter((value) => Number.isInteger(value))
    )).sort((left, right) => left - right);
    const runtimeEventAuthorityDeclared =
      runtimeEventFormalAuthorityDeclared;
    const checkpointEvidenceAuthorityDeclared = Boolean(
      configuredCheckpointEvidenceContract
      && [
        'trace2world.checkpoint-evidence-contract.v1',
        'trace2world.checkpoint-evidence-contract.v2',
        'trace2world.checkpoint-evidence-contract.v3',
      ].includes(configuredCheckpointEvidenceContract.contract_schema));
    const missingCheckpointEvidenceAuthority = required.length > 0
      && !runtimeEventAuthorityDeclared
      && !checkpointEvidenceAuthorityDeclared;
    const checkpointSnapshotSourceReasons = [];
    const runtimeEventLogSourceReasons = [];
    if (runtimeEventFormalAuthorityDeclared) {
      if (!['valid', 'missing'].includes(runtimeEventLogBefore.status)) {
        runtimeEventLogSourceReasons.push(
          'runtime_event_log_source_' + runtimeEventLogBefore.status);
      } else if (!['valid', 'missing'].includes(runtimeEventLogAfter.status)) {
        runtimeEventLogSourceReasons.push(
          'runtime_event_log_source_' + runtimeEventLogAfter.status);
      } else if (!runtimeEventLogStable) {
        runtimeEventLogSourceReasons.push('evaluator_side_effect');
      }
    }
    if (checkpointEvidenceAuthorityDeclared) {
      if (checkpointSnapshotBefore.status !== 'valid') {
        checkpointSnapshotSourceReasons.push(
          'checkpoint_snapshot_source_' + checkpointSnapshotBefore.status);
      } else if (checkpointSnapshotAfter.status !== 'valid') {
        checkpointSnapshotSourceReasons.push(
          'checkpoint_snapshot_source_' + checkpointSnapshotAfter.status);
      } else if (!checkpointSnapshotStable) {
        checkpointSnapshotSourceReasons.push('evaluator_side_effect');
      }
    }
    if (missingCheckpointEvidenceAuthority) {
      required.forEach((checkpointId) =>
        producerSchemaIncompatibleCheckpointIds.add(checkpointId));
    }
    if (checkpointSnapshotSourceReasons.length) {
      required.forEach((checkpointId) =>
        producerSchemaIncompatibleCheckpointIds.add(checkpointId));
    }
    if (runtimeEventLogSourceReasons.length) {
      required.forEach((checkpointId) =>
        producerSchemaIncompatibleCheckpointIds.add(checkpointId));
    }
    const producerSchemaIncompatibleReasons = [
      ...(missingCheckpointEvidenceAuthority
        ? ['missing_checkpoint_evidence_authority'] : []),
      ...checkpointSnapshotSourceReasons,
      ...runtimeEventLogSourceReasons,
    ];
    const producerSchemaIncompatible =
      producerSchemaIncompatibleCheckpointIds.size > 0;
    const runtimeEventSchemaInvalid =
      runtimeEventInvalidCheckpointIds.size > 0;
    const runtimeEventCarrierValid =
      !producerSchemaIncompatible && !runtimeEventSchemaInvalid;
    const producerAppOwnedStateSnapshotProjection = details
      && isPlainRecord(details.app_owned_state_snapshot)
      ? details.app_owned_state_snapshot : null;
    const appOwnedStateSnapshot = commonCheckpointSnapshot;
    const producerSnapshotMatchesCommon = Boolean(
      producerAppOwnedStateSnapshotProjection
      && appOwnedStateSnapshot
      && canonicalEqual(
        producerAppOwnedStateSnapshotProjection, appOwnedStateSnapshot));
    const checkpointEvidenceSpecs = configuredCheckpointEvidenceContract
      && isPlainRecord(configuredCheckpointEvidenceContract.checkpoints)
      ? configuredCheckpointEvidenceContract.checkpoints : {};
    const validateAppOwnedAssertions = (assertions) => {
      const assertionRows = assertions.map((assertion) => {
        if (assertion && isPlainRecord(assertion.expression)) {
          const evaluated = appOwnedStateSnapshot
            ? evaluateStateExpression(
              assertion.expression, appOwnedStateSnapshot)
            : {ok: false, value: null, reason: 'snapshot_missing'};
          const valid = evaluated.ok && evaluated.value === true;
          return {
            expression: assertion.expression,
            expression_result: evaluated.ok ? evaluated.value : null,
            valid,
            reason: valid ? null : (evaluated.ok
              ? 'expression_false_or_non_boolean' : evaluated.reason),
          };
        }
        const resolved = appOwnedStateSnapshot
          ? resolvePointer(appOwnedStateSnapshot, assertion.actual_pointer)
          : {found: false, value: null};
        const valid = validateEvidencePredicate(
          resolved, assertion.predicate, assertion.expected
        );
        return {
          actual_pointer: assertion.actual_pointer,
          predicate: assertion.predicate,
          expected: assertion.expected,
          actual_found: resolved.found,
          actual: resolved.found ? resolved.value : null,
          valid,
          reason: valid ? null
            : (resolved.found || assertion.predicate === 'exists'
              ? 'predicate_mismatch' : 'actual_pointer_missing'),
        };
      });
      return {
        valid: Boolean(appOwnedStateSnapshot && assertionRows.length)
          && assertionRows.every((row) => row.valid),
        assertions: assertionRows,
      };
    };
    const checkpointEvidenceState = {};
    const checkpointEvidenceValidationLedger = Object.entries(
      checkpointEvidenceSpecs
    ).map(([checkpointId, checkpointSpec]) => {
      const assertions = checkpointSpec
        && Array.isArray(checkpointSpec.assertions)
        ? checkpointSpec.assertions : [];
      const validation = validateAppOwnedAssertions(assertions);
      checkpointEvidenceState[checkpointId] = validation.valid;
      return {checkpoint_id: checkpointId, ...validation};
    });
    const forbiddenConditionSpecs = configuredCheckpointEvidenceContract
      && Array.isArray(
        configuredCheckpointEvidenceContract.forbidden_conditions)
      ? configuredCheckpointEvidenceContract.forbidden_conditions : [];
    const forbiddenConditionState = {};
    const forbiddenConditionValidationLedger = forbiddenConditionSpecs
      .map((conditionSpec) => {
        const conditionId = String(conditionSpec && conditionSpec.id || '');
        const assertions = conditionSpec
          && Array.isArray(conditionSpec.assertions)
          ? conditionSpec.assertions : [];
        const validation = validateAppOwnedAssertions(assertions);
        forbiddenConditionState[conditionId] = validation.valid;
        return {condition_id: conditionId, ...validation};
      });
    const independentlyValidatedEvidenceCoversRequired = required.length > 0
      && required.every((checkpointId) =>
        checkpointEvidenceState[checkpointId] === true);
    const checkpointEventTimeline = matching
      .map((event, eventIndex) => ({
        event_index: eventIndex,
        checkpoint_id: eventCheckpointId(event),
        task_id: eventTaskId(event),
        trace_id: eventTraceId(event),
        contract_type: eventContractType(event),
        event_type: String(event && (event.event_type || event.event
          || event.type || eventPayload(event).event_type) || ''),
        timestamp: event ? firstDefined(
          event.timestamp, event.ts, eventPayload(event).timestamp)
          : null,
      }))
      .filter((row) => row.checkpoint_id);
    const firstEventIndex = required.map((checkpointId) => {
      const producerIndexes = details && details.checkpoint_first_event_index;
      const selected = producerIndexes && producerIndexes[checkpointId];
      if (Number.isInteger(selected) && selected >= 0) return selected;
      const row = reportedVector.find((candidate) =>
        String(candidate && candidate.checkpoint_id || '') === checkpointId);
      const value = row && row.first_event_index;
      return Number.isInteger(value) && value >= 0 ? value : null;
    });
    for (let checkpointIndex = 0; checkpointIndex < required.length;
      checkpointIndex += 1) {
      if (firstEventIndex[checkpointIndex] !== null) continue;
      const checkpointId = required[checkpointIndex];
      const eventRow = checkpointEventTimeline.find((row) =>
        row.checkpoint_id === checkpointId);
      if (eventRow) firstEventIndex[checkpointIndex] = eventRow.event_index;
    }
    const fallbackContribution = required.length ? 1 / required.length : 0;
    const producerZeroWeightAssertionIds = details
      && Array.isArray(details.zero_weight_assertion_ids)
      ? details.zero_weight_assertion_ids.map(String) : [];
    const zeroWeightAssertionIds = new Set(
      configuredZeroWeightAssertionIds.filter((checkpointId) =>
        required.includes(checkpointId)));
    const configuredContributionAt = (index) => {
      const checkpointId = required[index];
      if (Object.prototype.hasOwnProperty.call(
          configuredCheckpointWeights, checkpointId)) {
        const configured = Number(configuredCheckpointWeights[checkpointId]);
        return Number.isFinite(configured) && configured >= 0
          ? configured : null;
      }
      return null;
    };
    const contributionAt = (index) => {
      if (zeroWeightAssertionIds.has(required[index])) return 0;
      const configured = configuredContributionAt(index);
      if (configured !== null) return configured;
      return fallbackContribution;
    };
    const explicitTerminal = typeof details.terminal_success === 'boolean'
      ? details.terminal_success : null;
    // Browser assertion rows are retained only as producer diagnostics.
    // Common terminal authority comes from the same package-declared
    // checkpoint evidence predicates used for ordinary checkpoint credit.
    const terminalActualCheckpointReached = (checkpointId) =>
      configuredTerminalRequired.includes(checkpointId)
      && checkpointEvidenceState[checkpointId] === true;
    const terminalAssertionsSatisfied =
      configuredTerminalRequired.length > 0
      && configuredTerminalRequired.every(terminalActualCheckpointReached);
    const baseCheckpointReached = (checkpointId) => {
      if (checkpointEvidenceState[checkpointId] === true) return true;
      if (legacyValidEventByCheckpoint[checkpointId]) return true;
      // Producer map/vector/seen/missing/invalid fields are projections
      // only and cannot establish or veto independently validated truth.
      return false;
    };
    const producerCheckpointDependenciesProjection = details
      && isPlainRecord(details.checkpoint_dependencies)
      ? details.checkpoint_dependencies
      : (details && isPlainRecord(details.checkpoint_dependency_graph)
        ? details.checkpoint_dependency_graph : null);
    const checkpointDependencies = configuredCheckpointDependencies;
    const hasExplicitCheckpointDependencies =
      Object.keys(checkpointDependencies).length > 0;
    const dependencyMemo = new Map();
    const dependencyGatedReached = (checkpointId, visiting) => {
      if (dependencyMemo.has(checkpointId)) return dependencyMemo.get(checkpointId);
      if (!baseCheckpointReached(checkpointId)) return false;
      const active = visiting || new Set();
      if (active.has(checkpointId)) return false;
      const next = new Set(active); next.add(checkpointId);
      const dependencies = Array.isArray(checkpointDependencies[checkpointId])
        ? checkpointDependencies[checkpointId].map(String) : [];
      const reached = dependencies.every((dependency) =>
        dependencyGatedReached(dependency, next));
      dependencyMemo.set(checkpointId, reached);
      return reached;
    };
    const checkpointReached = (checkpointId) => hasExplicitCheckpointDependencies
      ? dependencyGatedReached(checkpointId) : baseCheckpointReached(checkpointId);
    const reachedCount = required.filter(checkpointReached).length;
    let groundedCoverageReward = 0;
    required.forEach((checkpointId, index) => {
      if (checkpointReached(checkpointId))
        groundedCoverageReward += contributionAt(index);
    });
    const evaluatorScalar = (typeof raw_result === 'number'
        && Number.isFinite(raw_result)) ? raw_result
      : (details && Number.isFinite(Number(details.raw_reward))
        ? Number(details.raw_reward)
        : (details && Number.isFinite(Number(details.score))
          ? Number(details.score) : null));
    const producerSafetyViolationProjection = {
      forbiddenHit: typeof details.forbiddenHit === 'boolean'
        ? details.forbiddenHit : null,
      safety_violation: typeof details.safety_violation === 'boolean'
        ? details.safety_violation : null,
    };
    const safetyViolation = Object.values(forbiddenConditionState)
      .some((value) => value === true);
    const producerEnvironmentValidProjection =
      typeof details.env_valid === 'boolean' ? details.env_valid : null;
    // Any declared-carrier schema defect or evaluator-side mutation is
    // fail-closed even when another carrier independently proves state.
    const evaluationEnvironmentValid = runtimeEventCarrierValid;
    const coverageScore = safetyViolation || !evaluationEnvironmentValid
      ? 0 : Math.min(1, groundedCoverageReward);
    const terminalSolutionMode = String(
      configuredTerminalSolutionPolicy.mode || 'required_checkpoint_gate');
    const terminalRequiresAllRequired =
      configuredTerminalSolutionPolicy.require_all_required_checkpoints !== false;
    const terminalRequired = configuredTerminalRequired.length
      ? configuredTerminalRequired : required;
    const terminalCheckpointStateSatisfied = terminalRequired.length > 0
      && terminalRequired.every(checkpointReached);
    const declaredTerminalCheckpointStateSatisfied =
      configuredTerminalRequired.length === 0
      || configuredTerminalRequired.every(checkpointReached);
    const allRequiredCheckpointStateSatisfied = required.length > 0
      && required.every(checkpointReached);
    const terminalStateSatisfied =
      terminalSolutionMode === 'terminal_business_state'
      ? terminalAssertionsSatisfied
        && declaredTerminalCheckpointStateSatisfied
        && (!terminalRequiresAllRequired || allRequiredCheckpointStateSatisfied)
      : terminalCheckpointStateSatisfied;
    const evaluatorTerminalProjection = evaluatorScalar === null
      ? null : evaluatorScalar >= 1;
    const terminalVerdict = terminalStateSatisfied;
    const terminalSuccess = !safetyViolation && evaluationEnvironmentValid
      && terminalVerdict;
    const terminalContributions = terminalRequired
      .map((checkpointId) => required.indexOf(checkpointId))
      .filter((index) => index >= 0)
      .map((index) => contributionAt(index));
    const terminalPenalty = terminalContributions.length
      ? Math.max(...terminalContributions) : fallbackContribution;
    const maxNonTerminalScore = Math.max(0, 1 - terminalPenalty);
    const finalScore = terminalSuccess
      ? 1 : Math.min(coverageScore, maxNonTerminalScore);
    // A v3 checkpoint-evidence contract is the common-owned authority
    // for the scalar.  The app evaluator's historical event assertions
    // remain useful diagnostics, but cannot lower a score independently
    // established from the immutable snapshot and its declared predicates.
    const checkpointEvidenceV3AggregationAuthoritative =
      configuredCheckpointEvidenceContract
      && configuredCheckpointEvidenceContract.contract_schema
        === 'trace2world.checkpoint-evidence-contract.v3';
    const appEvaluatorScalar = checkpointEvidenceV3AggregationAuthoritative
      ? finalScore : evaluatorScalar;
    const canonicalSeen = required.filter(checkpointReached).sort((a, b) => {
      const ai = firstEventIndex[required.indexOf(a)];
      const bi = firstEventIndex[required.indexOf(b)];
      if (ai === null && bi === null)
        return required.indexOf(a) - required.indexOf(b);
      if (ai === null) return 1;
      if (bi === null) return -1;
      return ai - bi;
    });
    const canonicalCredited = evaluationEnvironmentValid ? canonicalSeen : [];
    const canonicalDiagnosticSeen = Array.from(diagnosticDomain).filter((id) =>
      reportedSeen.has(id) || matching.some((event) =>
        eventCheckpointId(event) === id)).sort((a, b) => {
        const ai = matching.findIndex((event) => eventCheckpointId(event) === a);
        const bi = matching.findIndex((event) => eventCheckpointId(event) === b);
        return (ai < 0 ? Number.MAX_SAFE_INTEGER : ai)
          - (bi < 0 ? Number.MAX_SAFE_INTEGER : bi);
      });
    const priorInvalid = details && details.invalid
      && typeof details.invalid === 'object' ? details.invalid : {};
    const canonicalPriorInvalid =
      checkpointEvidenceV3AggregationAuthoritative ? {} : priorInvalid;
    const duplicateCheckpointIds = Array.from(new Set([
      ...Object.keys(legacyDuplicateCounts),
      ...(Array.isArray(priorInvalid.duplicate_checkpoint_ids)
        ? priorInvalid.duplicate_checkpoint_ids.map(String) : []),
    ]));
    const outOfDomainIds = Array.from(new Set([
      ...legacyOutOfDomain,
      ...(Array.isArray(priorInvalid.out_of_domain)
        ? priorInvalid.out_of_domain.map(String) : []),
    ])).filter((checkpointId) => !taskCheckpointDomain.has(checkpointId));
    details = {
      ...details,
      app_evaluator_scalar: appEvaluatorScalar,
      app_evaluator_scalar_authority:
        checkpointEvidenceV3AggregationAuthoritative
        ? 'checkpoint_evidence_contract_v3'
        : 'producer_evaluator_scalar_projection',
      producer_evaluator_scalar_projection: evaluatorScalar,
      grounded_reducer_reward: finalScore,
      grounded_reducer_contract: 'browser_checkpoint_grounded_v3e',
      raw_reward: finalScore,
      env_valid: evaluationEnvironmentValid,
      forbiddenHit: safetyViolation,
      safety_violation: safetyViolation,
      runtime_event_schema_version:
        configuredRuntimeEventContract.runtime_event_schema_version
        || (runtimeEventSchemaVersionsSeen.length === 1
          ? runtimeEventSchemaVersionsSeen[0] : null),
      runtime_event_schema_versions_seen: runtimeEventSchemaVersionsSeen,
      runtime_event_contract: configuredRuntimeEventContract,
      runtime_event_formal_authority_declared:
        runtimeEventFormalAuthorityDeclared,
      runtime_event_identity_contract: {
        task_id: taskId,
        source_reference_task_id: sourceReferenceTaskId || null,
      },
      runtime_event_log_acquisition: {
        source: runtimeEventLogAfter.source || runtimeEventLogBefore.source,
        before_status: runtimeEventLogBefore.status,
        after_status: runtimeEventLogAfter.status,
        status: runtimeEventLogStable
          ? runtimeEventLogAfter.status
          : (runtimeEventLogSourceReasons.includes('evaluator_side_effect')
            ? 'evaluator_side_effect'
            : (runtimeEventLogBefore.status !== 'valid'
              ? runtimeEventLogBefore.status : runtimeEventLogAfter.status)),
        stable: runtimeEventLogStable,
        event_count: commonRuntimeEvents.length,
        event_log_hash: runtimeEventLogStable
          && runtimeEventLogAfter.status === 'valid'
          ? canonicalStateHash(commonRuntimeEvents) : null,
      },
      v1_adapter_snapshot_acquisition: {
        source: v1AdapterSnapshotSource || null,
        before_status: v1AdapterEnvelopeBefore.status,
        after_status: v1AdapterEnvelopeAfter.status,
        status: v1AdapterEnvelopeStable ? 'valid'
          : (v1AdapterEnvelopeBefore.status === 'valid'
            && v1AdapterEnvelopeAfter.status === 'valid'
            ? 'evaluator_side_effect'
            : (v1AdapterEnvelopeBefore.status !== 'valid'
              ? v1AdapterEnvelopeBefore.status
              : v1AdapterEnvelopeAfter.status)),
        stable: v1AdapterEnvelopeStable,
        envelope_hash: commonV1AdapterEnvelope
          ? canonicalStateHash(commonV1AdapterEnvelope) : null,
      },
      runtime_event_validation_ledger: runtimeEventValidationLedger,
      checkpoint_evidence_contract: configuredCheckpointEvidenceContract,
      checkpoint_snapshot_acquisition: {
        source: checkpointSnapshotSource || null,
        before_status: checkpointSnapshotBefore.status,
        after_status: checkpointSnapshotAfter.status,
        status: checkpointSnapshotStable ? 'valid'
          : (checkpointSnapshotSourceReasons.includes('evaluator_side_effect')
            ? 'evaluator_side_effect'
            : (checkpointSnapshotBefore.status !== 'valid'
              ? checkpointSnapshotBefore.status
              : checkpointSnapshotAfter.status)),
        stable: checkpointSnapshotStable,
        state_hash: appOwnedStateSnapshot
          ? canonicalStateHash(appOwnedStateSnapshot) : null,
      },
      checkpoint_evidence_state: checkpointEvidenceState,
      checkpoint_evidence_validation_ledger:
        checkpointEvidenceValidationLedger,
      forbidden_condition_state: forbiddenConditionState,
      forbidden_condition_validation_ledger:
        forbiddenConditionValidationLedger,
      producer_checkpoint_state_projection: producerCheckpointState,
      producer_app_owned_state_snapshot_projection:
        producerAppOwnedStateSnapshotProjection,
      producer_snapshot_matches_common: producerSnapshotMatchesCommon,
      producer_checkpoint_vector_projection: vectorCheckpointProjection,
      producer_checkpoint_vector_projection_ledger:
        producerCheckpointVectorProjectionLedger,
      producer_safety_violation_projection:
        producerSafetyViolationProjection,
      producer_checkpoint_dependencies_projection:
        producerCheckpointDependenciesProjection,
      producer_zero_weight_assertion_ids_projection:
        producerZeroWeightAssertionIds,
      producer_terminal_success_projection: explicitTerminal,
      producer_evaluator_terminal_projection:
        evaluatorTerminalProjection,
      producer_environment_valid_projection:
        producerEnvironmentValidProjection,
      producer_schema_incompatible: producerSchemaIncompatible,
      producer_schema_incompatible_reasons:
        producerSchemaIncompatibleReasons,
      producer_schema_incompatible_checkpoint_ids:
        Array.from(producerSchemaIncompatibleCheckpointIds),
      runtime_event_invalid_checkpoint_ids:
        Array.from(runtimeEventInvalidCheckpointIds),
      evaluation_invalid_reason: producerSchemaIncompatible
        ? 'producer_schema_incompatible'
        : (runtimeEventSchemaInvalid
          ? 'runtime_event_invalid' : null),
      ordered_checkpoint_prefix_length: hasExplicitCheckpointDependencies
        ? reachedCount : null,
      ordered_checkpoint_total: required.length,
      grounded_checkpoint_coverage_count: canonicalCredited.length,
      checkpoint_order_mode: hasExplicitCheckpointDependencies
        ? 'explicit_dependency_gated_coverage'
        : 'grounded_event_state_coverage',
      checkpoint_weights: configuredCheckpointWeights,
      zero_weight_assertion_ids: Array.from(zeroWeightAssertionIds),
      checkpoint_dependencies: checkpointDependencies,
      checkpoint_state_source: Object.values(checkpointEvidenceState)
        .some((value) => value === true)
        ? 'common_validated_app_owned_state_snapshot'
        : 'runtime_event_log',
      checkpoint_event_timeline: checkpointEventTimeline,
      terminal_solution_policy: configuredTerminalSolutionPolicy,
      terminal_assertions_satisfied: terminalAssertionsSatisfied,
      terminal_success: terminalSuccess,
      required,
      seen: canonicalCredited,
      credited: canonicalCredited,
      missing: required.filter((checkpointId) =>
        !canonicalCredited.includes(checkpointId)),
      diagnostic_seen: canonicalDiagnosticSeen,
      seen_domain:
        'deduplicated_valid_required_checkpoint_ids_in_first_valid_event_order',
      invalid: {
        ...canonicalPriorInvalid,
        duplicate_checkpoint_ids: [],
        out_of_domain: outOfDomainIds,
        legacy_event_observation_invalid: required.filter((checkpointId) =>
          runtimeEventInvalidCheckpointIds.has(checkpointId)),
        runtime_event_invalid:
          Array.from(runtimeEventInvalidCheckpointIds),
      },
      diagnostics: {
        ...(details && details.diagnostics
          && typeof details.diagnostics === 'object'
          ? details.diagnostics : {}),
        producer_evaluator_invalid_projection: priorInvalid,
        producer_evaluator_invalid_projection_authority:
          checkpointEvidenceV3AggregationAuthoritative
          ? 'diagnostic_only_checkpoint_evidence_contract_v3'
          : 'legacy_compatibility',
        duplicate_event_checkpoint_ids: duplicateCheckpointIds,
        diagnostic_checkpoint_ids_seen: canonicalDiagnosticSeen,
      },
      checkpoint_reward_vector: required.map((checkpointId, index) => {
        const reported = reportedVector.find((candidate) =>
          String(candidate && candidate.checkpoint_id || '') ===
            checkpointId) || {};
        const anchorIndex = firstEventIndex[index];
        return {
          ...reported,
          checkpoint_id: checkpointId,
          reached: evaluationEnvironmentValid
            && checkpointReached(checkpointId),
          checkpoint_dependencies: Array.isArray(
            checkpointDependencies[checkpointId])
            ? checkpointDependencies[checkpointId].map(String) : [],
          credit_assignment_mode: hasExplicitCheckpointDependencies
            ? 'explicit_dependency_gated_coverage'
            : 'grounded_event_state_coverage',
          first_event_index: anchorIndex,
          first_event_index_source: anchorIndex !== null ? 'runtime_event_log'
            : (terminalActualCheckpointReached(checkpointId)
              ? 'checkpoint_evidence_contract'
              : (checkpointEvidenceState[checkpointId] === true
              ? 'checkpoint_evidence_contract'
              : 'unobserved_event_anchor')),
          reward_weight: contributionAt(index),
          reward_contribution: evaluationEnvironmentValid
            && checkpointReached(checkpointId)
            ? contributionAt(index) : 0,
          earned: evaluationEnvironmentValid
            && checkpointReached(checkpointId)
            ? contributionAt(index) : 0,
        };
      }),
    };
    raw_result = finalScore;
  }
  return {raw_result, details};
})()