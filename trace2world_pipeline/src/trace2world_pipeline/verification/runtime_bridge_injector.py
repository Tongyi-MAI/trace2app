
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


BRIDGE_JS = r"""
window.GhostwebRuntimeState = (function(existingRuntimeState) {
  const STORE_KEY = '__ghostweb_runtime_state_v1__';
  // Chromium grants roughly 5 MiB per storage area.  This store shares
  // sessionStorage with app-owned business state and formal checkpoint
  // events, so audit history may not consume the whole origin quota.  The
  // exact current/committed state remains authoritative; only redundant,
  // replayable audit rows are compacted when their serialized union exceeds
  // this producer-owned reservation.
  // Storage quota is accounted in UTF-16 code units by browser engines, not
  // UTF-8 payload bytes.  Keep formal events and the structured runtime
  // state below one common conservative reservation so either producer can
  // persist across a page transition without evicting the other.
  const SESSION_STORAGE_BUDGET_BYTES = 4 * 1024 * 1024;
  const MAX_PERSISTED_STORE_BYTES = 1024 * 1024;
  const EVENT_STORE_KEY = '__ghostweb_checkpoint_events_v1__';
  const COMPACTABLE_AUDIT_LOG_FIELDS = [
    'state_reads',
    'state_writes',
    'before_after_diff',
    'projection_log',
    'event_log'
  ];
  const EMPTY_STORE = {
    trace_id: '',
    current_state: {},
    committed_state: {},
    event_log: [],
    state_reads: [],
    state_writes: [],
    before_after_diff: [],
    projection_log: [],
    source_index: {},
    audit_compaction: {
      max_persisted_bytes: MAX_PERSISTED_STORE_BYTES,
      last_persisted_bytes: 0,
      dropped_by_log: {
        state_reads: 0,
        state_writes: 0,
        before_after_diff: 0,
        projection_log: 0,
        event_log: 0
      }
    }
  };
  let _store = loadStore();

  function clone(value) {
    try { return JSON.parse(JSON.stringify(value === undefined ? null : value)); }
    catch (e) { return value; }
  }

  function fresh(traceId) {
    var next = clone(EMPTY_STORE);
    next.trace_id = traceId || '';
    return next;
  }

  function loadStore() {
    try {
      var raw = window.sessionStorage ? window.sessionStorage.getItem(STORE_KEY) : '';
      if (!raw) return fresh('');
      var parsed = JSON.parse(raw);
      return Object.assign(fresh(parsed.trace_id || ''), parsed || {});
    } catch (e) {
      return fresh('');
    }
  }

  function isLocationOwnedStateRef(ref) {
    // A new page realm may load an episode snapshot whose route still names
    // the page that initiated navigation.  The route runtime installed in the
    // current realm observes the browser's actual location before this bridge
    // migrates it, so that one ref is fresher than the session-backed copy.
    return ref === 'state::route.current_page';
  }

  function migrateExistingRuntimeState(existing) {
    if (!existing || typeof existing.snapshot !== 'function') return;
    var snapshotValue;
    try { snapshotValue = existing.snapshot(); }
    catch (e) { return; }
    if (!snapshotValue || typeof snapshotValue !== 'object' || Array.isArray(snapshotValue)) return;

    // A prior full checkpoint bridge may already expose the structured store.
    // Merge its state without discarding a fresher session-backed snapshot.
    var structured = snapshotValue.current_state || snapshotValue.committed_state;
    if (structured) {
      Object.keys(snapshotValue.current_state || {}).forEach(function(ref) {
        if (
          isLocationOwnedStateRef(ref)
          || !Object.prototype.hasOwnProperty.call(_store.current_state, ref)
        ) {
          _store.current_state[ref] = clone(snapshotValue.current_state[ref]);
        }
      });
      Object.keys(snapshotValue.committed_state || {}).forEach(function(ref) {
        if (!Object.prototype.hasOwnProperty.call(_store.committed_state, ref)) {
          _store.committed_state[ref] = clone(snapshotValue.committed_state[ref]);
        }
      });
      Object.keys(snapshotValue.source_index || {}).forEach(function(ref) {
        if (
          isLocationOwnedStateRef(ref)
          || !Object.prototype.hasOwnProperty.call(_store.source_index, ref)
        ) {
          _store.source_index[ref] = clone(snapshotValue.source_index[ref]);
        }
      });
      return;
    }

    // Route execution runtime historically exposed a flat ref -> value map.
    // Upgrade that protocol into the authoritative structured checkpoint store.
    Object.keys(snapshotValue).forEach(function(ref) {
      if (!ref || ref === 'trace_id') return;
      if (
        isLocationOwnedStateRef(ref)
        || !Object.prototype.hasOwnProperty.call(_store.current_state, ref)
      ) {
        _store.current_state[ref] = clone(snapshotValue[ref]);
        _store.source_index[ref] = {
          source: 'runtime_write',
          meta: { migrated_from: 'legacy_flat_runtime_state' },
          timestamp: Date.now()
        };
      }
    });
  }

  migrateExistingRuntimeState(existingRuntimeState);
  persist();

  function storageByteLength(value) {
    // sessionStorage quota is conventionally measured in UTF-16 code units.
    // This is deliberately conservative for non-BMP characters as well.
    return String(value === undefined || value === null ? '' : value).length * 2;
  }

  function storedValueByteLength(key) {
    try {
      return storageByteLength(window.sessionStorage && window.sessionStorage.getItem(key));
    } catch (_) {
      return 0;
    }
  }

  function compactionState() {
    var value = _store.audit_compaction;
    if (!value || typeof value !== 'object' || Array.isArray(value)) value = {};
    var dropped = value.dropped_by_log;
    if (!dropped || typeof dropped !== 'object' || Array.isArray(dropped)) dropped = {};
    COMPACTABLE_AUDIT_LOG_FIELDS.forEach(function(field) {
      dropped[field] = Math.max(0, Number(dropped[field] || 0));
      if (!Array.isArray(_store[field])) _store[field] = [];
    });
    value.max_persisted_bytes = MAX_PERSISTED_STORE_BYTES;
    value.last_persisted_bytes = Math.max(0, Number(value.last_persisted_bytes || 0));
    value.dropped_by_log = dropped;
    _store.audit_compaction = value;
    return value;
  }

  function largestCompactableLog() {
    var candidates = COMPACTABLE_AUDIT_LOG_FIELDS.filter(function(field) {
      return Array.isArray(_store[field]) && _store[field].length > 0;
    }).map(function(field) {
      var serialized = '';
      try { serialized = JSON.stringify(_store[field]); } catch (_) {}
      return { field: field, bytes: storageByteLength(serialized) };
    });
    candidates.sort(function(left, right) {
      if (left.bytes !== right.bytes) return right.bytes - left.bytes;
      return COMPACTABLE_AUDIT_LOG_FIELDS.indexOf(left.field) -
        COMPACTABLE_AUDIT_LOG_FIELDS.indexOf(right.field);
    });
    return candidates.length ? candidates[0].field : '';
  }

  function compactForStorage() {
    var audit = compactionState();
    var serialized = JSON.stringify(_store);
    var bytes = storageByteLength(serialized);
    var checkpointBytes = storedValueByteLength(EVENT_STORE_KEY);
    while (bytes > MAX_PERSISTED_STORE_BYTES || bytes + checkpointBytes > SESSION_STORAGE_BUDGET_BYTES) {
      var field = largestCompactableLog();
      if (!field) break;
      _store[field].shift();
      audit.dropped_by_log[field] += 1;
      serialized = JSON.stringify(_store);
      bytes = storageByteLength(serialized);
    }
    audit.last_persisted_bytes = bytes;
    serialized = JSON.stringify(_store);
    bytes = storageByteLength(serialized);
    // Updating the byte receipt can cross the boundary by a few digits. Keep
    // applying the same deterministic compaction rule until the receipt and
    // payload fit together.
    while (bytes > MAX_PERSISTED_STORE_BYTES || bytes + checkpointBytes > SESSION_STORAGE_BUDGET_BYTES) {
      var receiptField = largestCompactableLog();
      if (!receiptField) break;
      _store[receiptField].shift();
      audit.dropped_by_log[receiptField] += 1;
      audit.last_persisted_bytes = 0;
      serialized = JSON.stringify(_store);
      bytes = storageByteLength(serialized);
      audit.last_persisted_bytes = bytes;
      serialized = JSON.stringify(_store);
      bytes = storageByteLength(serialized);
    }
    audit.last_persisted_bytes = bytes;
    serialized = JSON.stringify(_store);
    bytes = storageByteLength(serialized);
    return {
      serialized: serialized,
      bytes: bytes,
      fits: bytes <= MAX_PERSISTED_STORE_BYTES && bytes + checkpointBytes <= SESSION_STORAGE_BUDGET_BYTES
    };
  }

  function persist() {
    try {
      var compacted = compactForStorage();
      if (!compacted.fits) {
        window.__ghostwebRuntimeStatePersistError = {
          reason: 'authoritative_runtime_state_exceeds_storage_budget',
          bytes: compacted.bytes,
          max_persisted_bytes: MAX_PERSISTED_STORE_BYTES,
          session_storage_budget_bytes: SESSION_STORAGE_BUDGET_BYTES
        };
        return false;
      }
      if (window.sessionStorage) window.sessionStorage.setItem(STORE_KEY, compacted.serialized);
      window.__ghostwebRuntimeStatePersistError = null;
      return true;
    } catch (e) {
      // Runtime state remains available in memory if storage is blocked.
      window.__ghostwebRuntimeStatePersistError = {
        reason: 'runtime_state_storage_write_failed',
        message: String(e && e.message || e).substring(0, 200),
        max_persisted_bytes: MAX_PERSISTED_STORE_BYTES,
        session_storage_budget_bytes: SESSION_STORAGE_BUDGET_BYTES
      };
      return false;
    }
  }

  function episodeStateOwnerTraceId(fallbackTraceId) {
    // Business state belongs to the logical task episode, not to whichever
    // Reference action namespace happens to own the current visible control.
    // Enhanced tasks can compose actions from several Reference traces in one
    // browser episode.  Treating each action/source trace as the state-store
    // owner clears previously observed scope and selection on every route.
    var context = null;
    try { context = window.__ghostwebExecutionContext || null; } catch (error) {}
    if (!context || typeof context !== 'object') {
      try {
        context = JSON.parse(
          window.sessionStorage.getItem('ghostweb.execution_context')
          || window.localStorage.getItem('ghostweb.execution_context')
          || '{}'
        );
      } catch (error) { context = {}; }
    }
    var taskId = String(context && (context.task_id || context.taskId) || '').trim();
    if (!taskId) {
      try {
        taskId = String(
          window.sessionStorage.getItem('ghostweb.current_task_id')
          || window.localStorage.getItem('ghostweb.current_task_id')
          || ''
        ).trim();
      } catch (error) { taskId = ''; }
    }
    return taskId || String(fallbackTraceId || '').trim();
  }

  function setTraceId(traceId) {
    var stateOwnerTraceId = episodeStateOwnerTraceId(traceId);
    if (!stateOwnerTraceId) return;
    if (_store.trace_id && _store.trace_id !== stateOwnerTraceId) {
      _store = fresh(stateOwnerTraceId);
    } else {
      _store.trace_id = stateOwnerTraceId;
    }
    persist();
  }

  function ensureTrace(traceId) {
    if (traceId) setTraceId(traceId);
    if (!_store.trace_id) _store.trace_id = episodeStateOwnerTraceId(traceId);
  }

  function isStateRef(ref) {
    return typeof ref === 'string' && ref.indexOf('state::') === 0;
  }

  function markSource(ref, source, meta) {
    if (!ref) return;
    _store.source_index[ref] = {
      source: source || 'runtime_store',
      meta: clone(meta || {}),
      timestamp: Date.now()
    };
  }

  function write(ref, value, meta) {
    if (!ref) return null;
    ensureTrace(meta && meta.trace_id);
    var before = clone(_store.current_state[ref]);
    _store.current_state[ref] = clone(value);
    _store.state_writes.push({
      ref: ref,
      value: clone(value),
      before: before,
      meta: clone(meta || {}),
      timestamp: Date.now()
    });
    _store.before_after_diff.push({
      ref: ref,
      before: before,
      after: clone(value),
      meta: clone(meta || {}),
      timestamp: Date.now()
    });
    markSource(ref, (meta && meta.source) || 'runtime_write', meta);
    persist();
    return clone(value);
  }

  function read(ref, meta) {
    ensureTrace(meta && meta.trace_id);
    var value = Object.prototype.hasOwnProperty.call(_store.current_state, ref)
      ? _store.current_state[ref]
      : _store.committed_state[ref];
    _store.state_reads.push({
      ref: ref,
      value: clone(value),
      meta: clone(meta || {}),
      timestamp: Date.now()
    });
    persist();
    return clone(value);
  }

  function commit(commitId, meta) {
    ensureTrace(meta && meta.trace_id);
    var refs = (meta && Array.isArray(meta.refs)) ? meta.refs : Object.keys(_store.current_state || {});
    refs.forEach(function(ref) {
      if (Object.prototype.hasOwnProperty.call(_store.current_state, ref)) {
        _store.committed_state[ref] = clone(_store.current_state[ref]);
        markSource(ref, 'runtime_commit', Object.assign({}, meta || {}, { commit_id: commitId || '' }));
      }
    });
    _store.event_log.push({
      kind: 'commit',
      commit_id: commitId || '',
      refs: refs,
      meta: clone(meta || {}),
      timestamp: Date.now()
    });
    persist();
    return snapshot();
  }

  function project(page, refs, meta) {
    ensureTrace(meta && meta.trace_id);
    var values = {};
    (refs || []).forEach(function(ref) {
      values[ref] = read(ref, Object.assign({}, meta || {}, { projection_page: page || '' }));
    });
    _store.projection_log.push({
      page: page || '',
      refs: clone(refs || []),
      values: clone(values),
      meta: clone(meta || {}),
      timestamp: Date.now()
    });
    persist();
    return values;
  }

  function emitTransaction(transactionId, meta) {
    ensureTrace(meta && meta.trace_id);
    _store.event_log.push({
      kind: 'transaction',
      transaction_id: transactionId || '',
      meta: clone(meta || {}),
      timestamp: Date.now()
    });
    persist();
    return snapshot();
  }

  function observeDomState(domState, meta) {
    ensureTrace(meta && meta.trace_id);
    var page = (meta && meta.page) || '';
    var observedRefs = [];
    Object.keys(domState || {}).forEach(function(key) {
      if (!isStateRef(key)) return;
      _store.current_state[key] = clone(domState[key]);
      markSource(key, 'dom_projection', meta);
      observedRefs.push(key);
    });
    var currentPage = page || (domState && domState.__page__) || '';
    if (currentPage) {
      _store.current_state['state::route.current_page'] = currentPage;
      markSource('state::route.current_page', 'location', meta);
      observedRefs.push('state::route.current_page');
    }
    if (observedRefs.length > 0) {
      _store.projection_log.push({
        page: currentPage,
        refs: observedRefs,
        meta: clone(Object.assign({}, meta || {}, { source: 'dom_projection' })),
        timestamp: Date.now()
      });
      persist();
    }
    return snapshot();
  }

  function reset(traceId) {
    _store = fresh(traceId || '');
    persist();
  }

  function snapshot() {
    return clone(_store);
  }

  return {
    setTraceId: setTraceId,
    reset: reset,
    write: write,
    read: read,
    commit: commit,
    project: project,
    emitTransaction: emitTransaction,
    observeDomState: observeDomState,
    snapshot: snapshot
  };
})(window.GhostwebRuntimeState || null);

window.GhostwebCheckpointRuntime = (function() {
  const EVENT_STORE_KEY = '__ghostweb_checkpoint_events_v1__';
  const SESSION_STORAGE_BUDGET_BYTES = 4 * 1024 * 1024;
  const EVENT_STORE_MAX_BYTES = 3 * 1024 * 1024;
  const RUNTIME_STATE_STORE_KEY = '__ghostweb_runtime_state_v1__';
  let _traceId = '';
  let _sequence = 0;
  let _eventLog = [];
  let _currentState = {};
  let _initialState = {};

  function init(traceId) {
    _traceId = traceId;
    // A navigation creates a new page realm, but sessionStorage is the
    // episode-owned checkpoint carrier.  Resume the highest sequence already
    // persisted for this exact trace so ordered evaluator assertions observe
    // a monotonic cross-page event stream.  Do not use an action count,
    // expected checkpoint order, or evaluator fixture as the sequence source.
    _sequence = readStoredEvents().reduce(function(maximum, event) {
      if (!event || String(event.trace_id || '') !== String(traceId || '')) return maximum;
      var value = Number(event.sequence || 0);
      return Number.isFinite(value) && value > maximum ? value : maximum;
    }, 0);
    _eventLog = [];
    if (window.GhostwebRuntimeState && window.GhostwebRuntimeState.setTraceId) {
      window.GhostwebRuntimeState.setTraceId(traceId || '');
    }
    _currentState = captureState();
    _initialState = JSON.parse(JSON.stringify(_currentState));
  }

  function readStoredEvents() {
    try {
      var raw = window.sessionStorage ? window.sessionStorage.getItem(EVENT_STORE_KEY) : '';
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function storageByteLength(value) {
    return String(value === undefined || value === null ? '' : value).length * 2;
  }

  function storedValueByteLength(key) {
    try {
      return storageByteLength(window.sessionStorage && window.sessionStorage.getItem(key));
    } catch (_) {
      return 0;
    }
  }

  function compactEventForStorage(event) {
    var stored = cloneJson(event) || {};
    var payload = plainRecord(stored.payload) ? stored.payload : {};
    // A terminal evaluator can run in a different page realm after a route
    // transition, so its required action observations must survive in the
    // session-backed event carrier.  Compact only recursively growing runtime
    // audit arrays; do not remove the observed DOM or snapshot fields.
    if (typeof payload.observed_dom_excerpt === 'string') {
      payload.observed_dom_excerpt = payload.observed_dom_excerpt.slice(0, 2400);
    }
    if (plainRecord(payload.observed_runtime_snapshot)) {
      var snapshot = cloneJson(payload.observed_runtime_snapshot) || {};
      delete snapshot.event_log;
      delete snapshot.state_reads;
      delete snapshot.state_writes;
      delete snapshot.before_after_diff;
      delete snapshot.projection_log;
      payload.observed_runtime_snapshot = snapshot;
    }
    if (plainRecord(payload.observed_dom_action)) {
      delete payload.observed_dom_action.runtime_snapshot;
    }
    stored.payload = payload;
    return stored;
  }

  function writeStoredEvents(events) {
    if (!window.sessionStorage) throw new Error('checkpoint_event_store_unavailable');
    var compacted = (events || []).map(compactEventForStorage);
    var serialized = JSON.stringify(compacted);
    var bytes = storageByteLength(serialized);
    if (bytes > EVENT_STORE_MAX_BYTES) {
      throw new Error('checkpoint_event_store_limit_exceeded');
    }
    if (bytes + storedValueByteLength(RUNTIME_STATE_STORE_KEY) > SESSION_STORAGE_BUDGET_BYTES) {
      throw new Error('checkpoint_event_store_shared_budget_exceeded');
    }
    window.sessionStorage.setItem(EVENT_STORE_KEY, serialized);
  }

  function persistEvent(event) {
    try {
      var events = readStoredEvents();
      events.push(event);
      writeStoredEvents(events);
      window.__ghostwebCheckpointEventPersistError = null;
      return true;
    } catch (error) {
      window.__ghostwebCheckpointEventPersistError = {
        reason: String(error && error.message || error).substring(0, 200),
        checkpoint_id: String(event && event.checkpoint_id || ''),
        max_event_store_bytes: EVENT_STORE_MAX_BYTES,
        session_storage_budget_bytes: SESSION_STORAGE_BUDGET_BYTES
      };
      return false;
    }
  }

  function captureState() {
    var state = {};
    var draftValues = {};
    var selectedValues = [];
    document.querySelectorAll('input, textarea, [contenteditable]').forEach(function(el) {
      var key = el.id || el.name || el.getAttribute('aria-label') || '';
      if (key) {
        var val = el.value || el.textContent || '';
        state[key] = val;
        if (val) draftValues[key] = val;
      }
    });
    document.querySelectorAll('[data-checked], [data-selected], .selected, .checked, .active').forEach(function(el) {
      var key = el.id || el.className;
      if (key) {
        state['selected:' + key] = true;
      }
      var text = (el.textContent || '').trim().substring(0, 200);
      var val = el.getAttribute('data-value') || el.getAttribute('value') || text;
      if (val && selectedValues.indexOf(val) === -1) selectedValues.push(val);
    });
    document.querySelectorAll('[role="tab"][aria-selected="true"], [role="radio"][aria-checked="true"], [role="checkbox"][aria-checked="true"]').forEach(function(el) {
      var text = (el.textContent || '').trim().substring(0, 200);
      if (text && selectedValues.indexOf(text) === -1) selectedValues.push(text);
    });
    // Generated apps often expose a same-page selection through ARIA while
    // keeping the text content unchanged. Capture those producer-owned
    // interactive attributes under a stable DOM identity so a real visible
    // tab/radio/expander transition is not collapsed into an empty event.
    // This is an observation of current DOM state, not evaluator authority.
    var interactiveStateIndex = 0;
    document.querySelectorAll('[aria-selected], [aria-checked], [aria-expanded], [aria-pressed]').forEach(function(el) {
      var key = el.id || el.getAttribute('data-state-ref') || el.getAttribute('data-element-id') || el.getAttribute('name') || '';
      if (!key) key = 'interactive:' + interactiveStateIndex;
      interactiveStateIndex += 1;
      var observed = {};
      ['aria-selected', 'aria-checked', 'aria-expanded', 'aria-pressed'].forEach(function(attr) {
        var value = el.getAttribute(attr);
        if (value !== null) observed[attr] = value;
      });
      state['interactive:' + key] = observed;
      if (observed['aria-selected'] === 'true' || observed['aria-checked'] === 'true') {
        var interactiveText = (el.textContent || '').trim().substring(0, 200);
        if (interactiveText && selectedValues.indexOf(interactiveText) === -1) selectedValues.push(interactiveText);
      }
    });
    document.querySelectorAll('input[type="radio"]:checked, input[type="checkbox"]:checked').forEach(function(el) {
      var label = el.getAttribute('aria-label') || el.getAttribute('data-value') || el.value || '';
      var parent = el.closest('label');
      if (!label && parent) label = (parent.textContent || '').trim().substring(0, 200);
      if (label && selectedValues.indexOf(label) === -1) selectedValues.push(label);
    });
    document.querySelectorAll('select').forEach(function(el) {
      var opt = el.options[el.selectedIndex];
      if (opt) {
        var text = (opt.textContent || '').trim().substring(0, 200);
        if (text && selectedValues.indexOf(text) === -1) selectedValues.push(text);
      }
    });
    document.querySelectorAll('.tab-active, .tab-selected, .nav-active, .nav-selected, .current, [data-tab-active="true"]').forEach(function(el) {
      var text = (el.textContent || '').trim().substring(0, 200);
      if (text && selectedValues.indexOf(text) === -1) selectedValues.push(text);
    });
    document.querySelectorAll('[data-state-ref]').forEach(function(el) {
      var ref = el.getAttribute('data-state-ref');
      if (ref) {
        var keys = ['data-selected-count', 'data-selected-values', 'data-value',
                     'data-fare-min', 'data-fare-max', 'data-query', 'data-draft'];
        keys.forEach(function(attr) {
          var v = el.getAttribute(attr);
          if (v !== null && v !== '') {
            var shortAttr = attr.replace('data-', '');
            state[ref + '.' + shortAttr] = v;
          }
        });
      }
    });
    if (Object.keys(draftValues).length > 0) {
      state['draft_values'] = draftValues;
    } else {
      state['draft_values'] = draftValues;
    }
    state['selected_values'] = selectedValues;
    state['selection_values'] = selectedValues;
    if (_initialState && Object.keys(_initialState).length > 0) {
      var mutated = {};
      Object.keys(state).forEach(function(k) {
        if (k === '__page__' || k === '__visible_text__') return;
        if (JSON.stringify(state[k]) !== JSON.stringify(_initialState[k])) {
          mutated[k] = state[k];
        }
      });
      state['mutated_values'] = mutated;
      state['mutation_values'] = mutated;
    } else {
      state['mutated_values'] = {};
      state['mutation_values'] = {};
    }
    state['__page__'] = location.pathname.split('/').pop();
    state['__visible_text__'] = document.body ? (document.body.innerText || '').substring(0, 2000) : '';
    if (window.GhostwebRuntimeState) {
      try {
        if (window.GhostwebRuntimeState.setTraceId) {
          window.GhostwebRuntimeState.setTraceId(_traceId || '');
        }
        var runtimeStore = window.GhostwebRuntimeState.observeDomState
          ? window.GhostwebRuntimeState.observeDomState(state, {
              trace_id: _traceId || '',
              page: state['__page__'],
              source: 'checkpoint_capture'
            })
          : (window.GhostwebRuntimeState.snapshot ? window.GhostwebRuntimeState.snapshot() : null);
        if (runtimeStore) {
          Object.keys(runtimeStore.committed_state || {}).forEach(function(ref) {
            if (state[ref] === undefined) state[ref] = runtimeStore.committed_state[ref];
          });
          Object.keys(runtimeStore.current_state || {}).forEach(function(ref) {
            state[ref] = runtimeStore.current_state[ref];
          });
          state['__runtime_store_present__'] = true;
        }
      } catch (e) {
        state['__runtime_store_present__'] = false;
        state['__runtime_store_error__'] = String(e && e.message || e).substring(0, 200);
      }
    } else {
      state['__runtime_store_present__'] = false;
    }
    return state;
  }

  function cloneJson(value) {
    try { return JSON.parse(JSON.stringify(value)); }
    catch (_) { return null; }
  }

  function plainRecord(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  // This canonicalizer and hash are the producer half of
  // agentforge.webenv.runtime-event-contract.v2.  Keep it byte-for-byte
  // semantically aligned with the host's evaluator-side implementation:
  // JSON.stringify alone preserves insertion order and previously generated
  // a value that could not be verified after transport/replay.
  function canonicalStable(value) {
    if (value === null || value === undefined) return JSON.stringify(value);
    if (Array.isArray(value)) return '[' + value.map(canonicalStable).join(',') + ']';
    if (typeof value === 'object') {
      return '{' + Object.keys(value).sort().map(function(key) {
        return JSON.stringify(key) + ':' + canonicalStable(value[key]);
      }).join(',') + '}';
    }
    return JSON.stringify(value);
  }

  function canonicalEqual(left, right) {
    return canonicalStable(left) === canonicalStable(right);
  }

  function hashState(state) {
    var text = canonicalStable(state);
    var first = 2166136261 >>> 0;
    var second = 2246822519 >>> 0;
    for (var index = 0; index < text.length; index++) {
      var code = text.charCodeAt(index);
      first = Math.imul(first ^ code, 16777619) >>> 0;
      second = Math.imul(second ^ (code + ((index & 255) << 8)), 3266489917) >>> 0;
    }
    function hex(value) { return ('00000000' + (value >>> 0).toString(16)).slice(-8); }
    return 'appstate-fnv1a-v1:' + hex(first) + hex(second);
  }

  function diffState(before, after) {
    // A v2 delta is replayable only when it can be applied to the entire
    // top-level app-state record.  Add/remove-key snapshots have no safe
    // compact patch representation in the contract, so retain them as an
    // invalid/empty delta for the evaluator to reject rather than silently
    // publishing a partial transition.
    if (!plainRecord(before) || !plainRecord(after)) return {};
    var beforeKeys = Object.keys(before).sort();
    var afterKeys = Object.keys(after).sort();
    if (beforeKeys.join('\u0000') !== afterKeys.join('\u0000')) return {};
    var diff = {};
    beforeKeys.forEach(function(key) {
      if (!canonicalEqual(before[key], after[key])) {
        diff[key] = { before: cloneJson(before[key]), after: cloneJson(after[key]) };
      }
    });
    return diff;
  }

  function activeTaskId() {
    var context = null;
    try { context = window.__ghostwebExecutionContext || null; } catch (_) {}
    if (!context || typeof context !== 'object') {
      try {
        context = JSON.parse(window.sessionStorage.getItem('ghostweb.execution_context') || '{}');
      } catch (_) { context = {}; }
    }
    return String(context && (context.task_id || context.taskId || context.trace_id) || _traceId || '');
  }

  function contextSnapshot(context, key) {
    var candidate = context && context[key];
    return plainRecord(candidate) ? cloneJson(candidate) : null;
  }

  function emitCheckpoint(checkpointId, payload, context) {
    context = context || {};
    var suppliedBefore = contextSnapshot(context, 'stateSnapshotBefore');
    var suppliedAfter = contextSnapshot(context, 'stateSnapshotAfter');
    var stateBefore = suppliedBefore || cloneJson(_currentState) || {};
    var stateAfter = suppliedAfter || captureState();
    _currentState = cloneJson(stateAfter) || {};
    var stateDiff = diffState(stateBefore, stateAfter);
    var actionId = String(context.actionId || '');
    var taskId = activeTaskId();
    var normalizedCheckpointId = String(checkpointId || '');
    var normalizedPayload = (
      payload && typeof payload === 'object' && !Array.isArray(payload)
    ) ? Object.assign({}, payload) : {};
    // Formal runtime-event evidence may only mirror the app-owned values that
    // this exact capture observed to change.  It neither writes state nor
    // consumes evaluator expectations: the receiving reducer recomputes the
    // before/after hash and requires each declared field to equal the actual
    // state snapshot after applying this event's diff.
    if (normalizedPayload.runtime_observation === undefined) {
      var changedState = {};
      Object.keys(stateDiff).sort().forEach(function(key) {
        changedState[key] = cloneJson(stateAfter[key]);
      });
      normalizedPayload.runtime_observation = {
        schema: 'ghostweb.changed-state-observation.v1',
        changed_state: changedState
      };
    }
    // Event identity is required by the common-owned envelope.  Observation
    // bindings are intentionally carried separately in runtime_observation;
    // evaluators must never mistake these identity fields for state evidence.
    normalizedPayload.trace_id = taskId;
    normalizedPayload.task_id = taskId;
    normalizedPayload.checkpoint_id = normalizedCheckpointId;
    normalizedPayload.runtime_event_schema_version = 2;
    if (
      normalizedPayload.runtime_observation === undefined
      && context.runtime_observation !== undefined
    ) {
      normalizedPayload.runtime_observation = cloneJson(context.runtime_observation);
    }
    if (actionId) normalizedPayload.action_id = actionId;

    var event = {
      runtime_event_schema_version: 2,
      state_hash_algorithm: 'appstate-fnv1a-v1',
      trace_id: taskId,
      task_id: taskId,
      checkpoint_id: normalizedCheckpointId,
      action_id: actionId,
      page: location.pathname.split('/').pop(),
      payload: normalizedPayload,
      state_before_hash: hashState(stateBefore),
      state_after_hash: hashState(stateAfter),
      state_snapshot_before: cloneJson(stateBefore),
      state_snapshot_after: cloneJson(stateAfter),
      state_diff: stateDiff,
      sequence: _sequence + 1,
      timestamp: Date.now()
    };
    if (!persistEvent(event)) return null;
    _sequence = event.sequence;
    _eventLog.push(event);
    if (window.GhostwebRuntimeState && window.GhostwebRuntimeState.emitTransaction) {
      window.GhostwebRuntimeState.emitTransaction('checkpoint:' + normalizedCheckpointId, {
        trace_id: taskId,
        task_id: taskId,
        checkpoint_id: normalizedCheckpointId,
        action_id: actionId,
        page: event.page,
        payload: normalizedPayload,
        state_diff: stateDiff,
        source: 'checkpoint_event'
      });
    }
    return event;
  }

  function step(actionId) {
    _currentState = captureState();
  }

  function eventKey(event) {
    return [
      event && event.trace_id || '',
      event && event.action_id || '',
      event && event.checkpoint_id || '',
      event && event.sequence || '',
      event && event.timestamp || ''
    ].join('::');
  }

  function eventLog() {
    var merged = [];
    var seen = {};
    // Keep the one canonical event identity, but prefer the live event when
    // this document emitted it. sessionStorage deliberately omits redundant
    // audit projections, whereas the in-memory event is the complete capture
    // boundary. On a later document only the stored canonical event exists.
    _eventLog.concat(readStoredEvents()).forEach(function(event) {
      var key = eventKey(event);
      if (!seen[key]) {
        seen[key] = true;
        merged.push(event);
      }
    });
    return JSON.parse(JSON.stringify(merged));
  }
  function currentState() { return JSON.parse(JSON.stringify(_currentState)); }
  function initialState() { return JSON.parse(JSON.stringify(_initialState)); }
  function stateDiffFromInit() { return diffState(_initialState, _currentState); }
  function resetState(traceId) {
    init(traceId || _traceId);
    // A reset starts a new interaction episode. Pending cross-document
    // observations belong to the discarded episode and cannot be redeemed by
    // a later arrival.
    try {
      window.sessionStorage.removeItem('__ghostweb_pending_cross_page_checkpoints_v1__');
      window.sessionStorage.removeItem('__ghostweb_checkpoint_episode_id_v1__');
    } catch (_) {}
  }
  function setTraceId(traceId) {
    if (traceId) {
      _traceId = traceId;
      if (window.GhostwebRuntimeState && window.GhostwebRuntimeState.setTraceId) {
        window.GhostwebRuntimeState.setTraceId(traceId);
      }
    }
  }

  return { init: init, step: step, captureState: captureState, diffState: diffState,
           emitCheckpoint: emitCheckpoint, eventLog: eventLog, currentState: currentState,
           initialState: initialState, stateDiffFromInit: stateDiffFromInit,
           resetState: resetState, setTraceId: setTraceId };
})();
"""


def _build_emission_harness_js(
    checkpoint_map: dict[str, list[dict[str, Any]]],
    trace_id: str,
    obligations: list[dict[str, Any]] | None = None,
    terminal_outcomes_on_load: list[dict[str, Any]] | None = None,
    cross_page_checkpoints_on_load: list[dict[str, Any]] | None = None,
    action_host_observation_bindings: list[dict[str, str]] | None = None,
    preserve_runtime_entry_fields: bool = False,
) -> str:


    runtime_fields = (
        "trace_id",
        "checkpoint_id",
        "checkpoint_kind",
        "depends_on",
        "binding_trace_id",
        "source_action_ids",
        "action_aliases",
        "source_steps",
        "event",
        "effect_kind",
        "navigation_target_filename",
        "cross_page_state_capture",
        "app_owned_capture_envelope",
        "has_terminal_observable_dom_bindings",
        "terminal_observable_page_filename",
        "operation",
        "operation_intent",
        "action_id",


        "external_observation_producer",
    )

    def runtime_entry(entry: dict[str, Any]) -> dict[str, Any]:


        if preserve_runtime_entry_fields:
            return dict(entry)
        return {key: entry[key] for key in runtime_fields if key in entry}

    runtime_checkpoint_map = {
        action_id: [runtime_entry(entry) for entry in entries]
        for action_id, entries in checkpoint_map.items()
    }
    map_json = json.dumps(runtime_checkpoint_map, ensure_ascii=False)
    action_host_bindings_json = json.dumps(
        action_host_observation_bindings or [], ensure_ascii=False
    )
    terminal_outcomes = [
        runtime_entry(entry) for entry in (terminal_outcomes_on_load or [])
    ]
    cross_page_checkpoints = [
        runtime_entry(entry) for entry in (cross_page_checkpoints_on_load or [])
    ]


    cross_page_checkpoint_prelude = ""
    cross_page_checkpoint_emit_guard = ""
    cross_page_checkpoint_init = ""
    cross_page_checkpoint_enabled = bool(cross_page_checkpoints) or any(
        entry.get("cross_page_state_capture")
        for entries in runtime_checkpoint_map.values()
        for entry in entries
    )
    if cross_page_checkpoint_enabled:
        checkpoint_json = json.dumps(cross_page_checkpoints, ensure_ascii=False)
        cross_page_checkpoint_prelude = f"""
  var CROSS_PAGE_CHECKPOINTS_ON_LOAD = {checkpoint_json};
  var PENDING_CROSS_PAGE_CHECKPOINT_KEY = '__ghostweb_pending_cross_page_checkpoints_v1__';
  var CROSS_PAGE_EPISODE_KEY = '__ghostweb_checkpoint_episode_id_v1__';
  var CROSS_PAGE_CHECKPOINT_MAX_AGE_MS = 15000;

  function crossPageFilename() {{
    try {{
      return decodeURIComponent(String(window.location.pathname || '').split('/').pop() || '');
    }} catch (_) {{
      return String(window.location.pathname || '').split('/').pop() || '';
    }}
  }}

  function crossPageCheckpointKey(entry) {{
    return [
      entry && entry.trace_id || '',
      entry && entry.action_id || '',
      entry && entry.checkpoint_id || '',
      entry && entry.navigation_target_filename || ''
    ].join('::');
  }}

  function currentCrossPageEpisode(taskId) {{
    var context = activeExecutionContext();
    var externalId = String(context && (
      context.episode_id || context.episodeId || context.session_id
      || context.sessionId || context.sid
    ) || '').trim();
    if (externalId) return 'context:' + externalId + '::task:' + String(taskId || '');
    // Some native direct-replay contexts do not expose an external sid. Give
    // that browser session an opaque, resettable carrier id instead of using
    // a checkpoint name, expected state, or DOM content as an episode token.
    var generated = '';
    try {{ generated = String(window.sessionStorage.getItem(CROSS_PAGE_EPISODE_KEY) || ''); }} catch (_) {{}}
    if (!generated) {{
      generated = 'bridge:' + String(Date.now()) + ':' + Math.random().toString(36).slice(2);
      try {{ window.sessionStorage.setItem(CROSS_PAGE_EPISODE_KEY, generated); }} catch (_) {{}}
    }}
    return generated + '::task:' + String(taskId || '');
  }}

  function readPendingCrossPageCheckpoints() {{
    try {{
      var parsed = JSON.parse(window.sessionStorage.getItem(PENDING_CROSS_PAGE_CHECKPOINT_KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    }} catch (_) {{
      return [];
    }}
  }}

  function writePendingCrossPageCheckpoints(rows) {{
    try {{
      window.sessionStorage.setItem(PENDING_CROSS_PAGE_CHECKPOINT_KEY, JSON.stringify(rows || []));
    }} catch (_) {{}}
  }}

  function rememberPendingCrossPageCheckpoint(entry, eventType, stateBefore, sourceObservation, taskId) {{
    var key = crossPageCheckpointKey(entry);
    if (!key || !stateBefore || typeof stateBefore !== 'object' || Array.isArray(stateBefore)) return;
    if (!sourceObservation || typeof sourceObservation !== 'object' || Array.isArray(sourceObservation)) return;
    var rows = readPendingCrossPageCheckpoints().filter(function(row) {{
      return row && row.key !== key && Date.now() - Number(row.created_at || 0) <= CROSS_PAGE_CHECKPOINT_MAX_AGE_MS;
    }});
    rows.push({{
      key: key,
      trace_id: entry.trace_id || '',
      task_id: taskId || '',
      episode_id: currentCrossPageEpisode(taskId),
      action_id: entry.action_id || '',
      checkpoint_id: entry.checkpoint_id || '',
      navigation_target_filename: entry.navigation_target_filename || '',
      event_type: eventType || '',
      // Preserve the source-page action observation exactly as it was read
      // before navigation. It proves the clicked host and its actual target;
      // the independent state payload below is still synthesized solely from
      // the target page's captureState() result.
      source_observation: sourceObservation,
      // This is the actual source-document read-only capture, not expected
      // state or a task fixture.  The target document supplies the matching
      // after capture immediately before formal event emission.
      state_snapshot_before: stateBefore,
      created_at: Date.now()
    }});
    writePendingCrossPageCheckpoints(rows.slice(-64));
  }}

  function consumePendingCrossPageCheckpoints() {{
    if (!CROSS_PAGE_CHECKPOINTS_ON_LOAD.length) return;
    var runtime = window.GhostwebCheckpointRuntime;
    if (!runtime) return;
    var pageName = crossPageFilename();
    var contracts = {{}};
    CROSS_PAGE_CHECKPOINTS_ON_LOAD.forEach(function(entry) {{
      contracts[crossPageCheckpointKey(entry)] = entry;
    }});
    var remaining = [];
    readPendingCrossPageCheckpoints().forEach(function(pending) {{
      if (!pending || Date.now() - Number(pending.created_at || 0) > CROSS_PAGE_CHECKPOINT_MAX_AGE_MS) return;
      var entry = contracts[pending.key || ''];
      var activeTaskId = String(activeExecutionContext().task_id || activeExecutionContext().taskId || entry && entry.trace_id || '');
      if (
        !entry
        || pending.navigation_target_filename !== pageName
        || pending.trace_id !== entry.trace_id
        || pending.action_id !== entry.action_id
        || pending.task_id !== activeTaskId
        || pending.episode_id !== currentCrossPageEpisode(activeTaskId)
      ) {{
        remaining.push(pending);
        return;
      }}
      var alreadyEmitted = runtime.eventLog().some(function(event) {{
        return event
          && event.trace_id === entry.trace_id
          && event.action_id === entry.action_id
          && event.checkpoint_id === entry.checkpoint_id;
      }});
      if (alreadyEmitted) return;
      var stateAfter = runtime.captureState();
      if (!stateAfter || typeof stateAfter !== 'object' || Array.isArray(stateAfter)) return;
      if (entry.trace_id && runtime.setTraceId) runtime.setTraceId(entry.trace_id);
      var payload = Object.assign(
        observedPayload(entry, null, pending.event_type || 'click'),
        pending.source_observation || {{}}
      );
      writeRuntimeStateDiffs(entry, pending.event_type || 'click');
      runtime.emitCheckpoint(entry.checkpoint_id, Object.assign(payload, {{
        trace_id: entry.trace_id || TRACE_ID || '',
        checkpoint_id: entry.checkpoint_id || '',
        source_steps: entry.source_steps || [],
        action_id: entry.action_id || '',
        checkpoint_kind: entry.checkpoint_kind || '',
        operation: entry.operation || '',
        operation_intent: entry.operation_intent || '',
        event_type: pending.event_type || 'click',
        event: entry.event || ''
      }}), {{
        actionId: entry.action_id || '',
        traceId: entry.trace_id || TRACE_ID || '',
        // The envelope makes two actual page captures structurally stable for
        // the common v2 state-diff reducer; it does not alter either capture.
        stateSnapshotBefore: {{ app_owned_capture: pending.state_snapshot_before }},
        stateSnapshotAfter: {{ app_owned_capture: stateAfter }}
      }});
      // Restore the native page snapshot as the baseline for later same-page
      // actions. The formal cross-page event itself retains the two wrapped
      // captures above, while subsequent app interactions remain unchanged.
      runtime.step();
    }});
    writePendingCrossPageCheckpoints(remaining);
  }}"""
        cross_page_checkpoint_emit_guard = """
      var crossPageTarget = String(entry.navigation_target_filename || '').trim();
      if (
        entry.cross_page_state_capture
        && crossPageTarget
        && crossPageTarget !== crossPageFilename()
      ) {
        var observedNavigationPayload = observedPayload(entry, target, eventType);
        if (observedNavigationPayload.observed_target_filename !== crossPageTarget) return;
        var activeTaskId = String(activeExecutionContext().task_id || activeExecutionContext().taskId || entry.trace_id || '');
        rememberPendingCrossPageCheckpoint(
          entry,
          eventType,
          runtime.captureState(),
          observedNavigationPayload,
          activeTaskId
        );
        return;
      }"""
        cross_page_checkpoint_init = """
      if (CROSS_PAGE_CHECKPOINTS_ON_LOAD.length) {
        window.setTimeout(consumePendingCrossPageCheckpoints, 0);
        window.setTimeout(consumePendingCrossPageCheckpoints, 100);
        window.setTimeout(consumePendingCrossPageCheckpoints, 500);
      }"""
    cross_page_outcome_enabled = bool(terminal_outcomes) or any(
        entry.get("terminal_observable_page_filename")
        for entries in runtime_checkpoint_map.values()
        for entry in entries
        if entry.get("checkpoint_kind") == "outcome"
    )
    cross_page_prelude = ""
    cross_page_emit_guard = ""
    cross_page_init = ""
    if cross_page_outcome_enabled:
        terminal_json = json.dumps(terminal_outcomes, ensure_ascii=False)
        cross_page_prelude = f"""
  var TERMINAL_OUTCOMES_ON_LOAD = {terminal_json};
  var PENDING_TERMINAL_OUTCOME_KEY = '__ghostweb_pending_terminal_outcomes_v1__';

  function currentFilename() {{
    try {{
      return decodeURIComponent(String(window.location.pathname || '').split('/').pop() || '');
    }} catch (_) {{
      return String(window.location.pathname || '').split('/').pop() || '';
    }}
  }}

  function terminalOutcomeKey(entry) {{
    return [
      entry && entry.trace_id || '',
      entry && entry.action_id || '',
      entry && entry.checkpoint_id || '',
      entry && entry.terminal_observable_page_filename || ''
    ].join('::');
  }}

  function readPendingTerminalOutcomes() {{
    try {{
      var parsed = JSON.parse(window.sessionStorage.getItem(PENDING_TERMINAL_OUTCOME_KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    }} catch (_) {{
      return [];
    }}
  }}

  function writePendingTerminalOutcomes(rows) {{
    try {{
      window.sessionStorage.setItem(PENDING_TERMINAL_OUTCOME_KEY, JSON.stringify(rows || []));
    }} catch (_) {{}}
  }}

  function rememberPendingTerminalOutcome(entry, eventType) {{
    var key = terminalOutcomeKey(entry);
    if (!key) return;
    var rows = readPendingTerminalOutcomes().filter(function(row) {{
      return row && row.key !== key;
    }});
    rows.push({{
      key: key,
      trace_id: entry.trace_id || '',
      action_id: entry.action_id || '',
      checkpoint_id: entry.checkpoint_id || '',
      terminal_observable_page_filename: entry.terminal_observable_page_filename || '',
      event_type: eventType || '',
      created_at: Date.now()
    }});
    writePendingTerminalOutcomes(rows.slice(-64));
  }}

  function consumePendingTerminalOutcomes() {{
    if (!TERMINAL_OUTCOMES_ON_LOAD.length) return;
    var runtime = window.GhostwebCheckpointRuntime;
    if (!runtime) return;
    var pageName = currentFilename();
    var contracts = {{}};
    TERMINAL_OUTCOMES_ON_LOAD.forEach(function(entry) {{
      contracts[terminalOutcomeKey(entry)] = entry;
    }});
    var remaining = [];
    readPendingTerminalOutcomes().forEach(function(pending) {{
      var entry = contracts[pending && pending.key || ''];
      if (!entry || pending.terminal_observable_page_filename !== pageName) {{
        remaining.push(pending);
        return;
      }}
      var alreadyEmitted = runtime.eventLog().some(function(event) {{
        return event
          && event.trace_id === entry.trace_id
          && event.action_id === entry.action_id
          && event.checkpoint_id === entry.checkpoint_id;
      }});
      if (alreadyEmitted) return;
      if (!observedTerminalStateSatisfied(entry)) {{
        remaining.push(pending);
        return;
      }}
      if (entry.trace_id && runtime.setTraceId) runtime.setTraceId(entry.trace_id);
      var expectedPayload = observedPayload(entry, null, pending.event_type || 'click');
      writeRuntimeStateDiffs(entry, pending.event_type || 'click');
      var terminalContext = {{
        actionId: entry.action_id || '',
        traceId: entry.trace_id || TRACE_ID || ''
      }};
      if (entry.app_owned_capture_envelope) {{
        var terminalBefore = runtime.currentState ? runtime.currentState() : runtime.captureState();
        var terminalAfter = runtime.captureState();
        terminalContext.stateSnapshotBefore = {{app_owned_capture: terminalBefore}};
        terminalContext.stateSnapshotAfter = {{app_owned_capture: terminalAfter}};
      }}
      runtime.emitCheckpoint(entry.checkpoint_id, Object.assign(expectedPayload, {{
        trace_id: entry.trace_id || TRACE_ID || '',
        checkpoint_id: entry.checkpoint_id || '',
        source_steps: entry.source_steps || [],
        action_id: entry.action_id || '',
        checkpoint_kind: entry.checkpoint_kind || '',
        operation: entry.operation || '',
        operation_intent: entry.operation_intent || '',
        event_type: pending.event_type || 'click',
        event: entry.event || '',
      }}), terminalContext);
    }});
    writePendingTerminalOutcomes(remaining);
  }}"""
        cross_page_emit_guard = """
      var terminalPage = String(entry.terminal_observable_page_filename || '').trim();
      if (
        entry.checkpoint_kind === 'outcome'
        && terminalPage
        && terminalPage !== currentFilename()
      ) {
        rememberPendingTerminalOutcome(entry, eventType);
        return;
      }"""
        cross_page_init = """
      if (TERMINAL_OUTCOMES_ON_LOAD.length) {
        window.setTimeout(consumePendingTerminalOutcomes, 0);
        window.setTimeout(consumePendingTerminalOutcomes, 100);
        window.setTimeout(consumePendingTerminalOutcomes, 500);
      }"""
    return f"""
(function() {{
  var CP_MAP_BY_ACTION_ID = {map_json};
  var ACTION_HOST_OBSERVATION_BINDINGS = {action_host_bindings_json};
  var TRACE_ID = {json.dumps(trace_id)};
  var fired = {{}};
  // Pointer/touch handlers can project app state before the browser later
  // dispatches ``click``.  Keep the actual pre-gesture capture narrowly by
  // action id so a delayed same-page outcome can still form a real envelope.
  // This is transient event-boundary data, never expected-state data.
  var pendingPreActionCaptures = {{}};{cross_page_checkpoint_prelude}{cross_page_prelude}

  function textAttr(el, name) {{
    return el && el.getAttribute ? (el.getAttribute(name) || '') : '';
  }}

  function actionIdsFromElement(el) {{
    var ids = [];
    var seen = {{}};
    function collect(value) {{
      // Action identity is producer-owned and may use a canonical namespace
      // other than the legacy ``trace:stepN`` form (for example
      // ``trace:gui:15``). Only scan explicit action-id attributes, never
      // labels or arbitrary page text, so this cannot turn business content
      // into action authority.
      String(value || '').replace(/[A-Za-z0-9][A-Za-z0-9_-]*(?::[A-Za-z0-9][A-Za-z0-9_-]*)+/g, function(id) {{
        if (!seen[id]) {{
          seen[id] = true;
          ids.push(id);
        }}
        return id;
      }});
    }}
    var nearestActionHost = null;
    for (var node = el, depth = 0; node && depth < 8; node = node.parentElement, depth++) {{
      var before = ids.length;
      collect(textAttr(node, 'data-action-id'));
      collect(textAttr(node, 'data-ghostweb-action-id'));
      collect(textAttr(node, 'data-action-aliases'));
      // PageGen's DOM contract may retain a canonical component id and map
      // source actions through explicit element aliases.  These attributes
      // are producer-owned action identity, just like the action-* form;
      // accepting them here restores the same visible host without deriving
      // identity from labels or arbitrary page text.
      collect(textAttr(node, 'data-element-id'));
      collect(textAttr(node, 'data-element-aliases'));
      if (ids.length > before) {{
        nearestActionHost = node;
        break;
      }}
    }}
    // Action attributes on an ancestor layout/gesture rail describe that
    // ancestor, not a nested button.  Once the nearest host has supplied an
    // id (and any aliases owned by that same host), do not merge farther
    // ancestors into the current click.  The execution context is updated
    // after DOM dispatch and can still name such an ancestor action.
    // PageGen can merge several task actions onto one visible route control
    // while leaving one active-trace action as a hidden duplicate. Recover
    // that alias only from the current trace's compiled route checkpoint and
    // the clicked control's materialized target. This keeps the visible host
    // authoritative without relying on labels, filenames, or task literals.
    var activeTrace = explicitActiveTraceId();
    var activeContext = activeExecutionContext();
    var sourceReferenceTrace = String(
      activeContext && (
        activeContext.source_reference_task_id
        || activeContext.sourceReferenceTaskId
      ) || ''
    ).trim();
    if (activeTrace && !nearestActionHost) {{
      var targets = [];
      var backLike = false;
      var explicitNonRoute = false;
      function addTarget(value) {{
        value = String(value || '').trim();
        if (value && targets.indexOf(value) === -1) targets.push(value);
      }}
      for (var routeNode = el, routeDepth = 0; routeNode && routeDepth < 8; routeNode = routeNode.parentElement, routeDepth++) {{
        var declaredEffectKind = textAttr(routeNode, 'data-ghostweb-effect-kind').toLowerCase();
        if (
          declaredEffectKind
          && ['route', 'navigate', 'save_return'].indexOf(declaredEffectKind) === -1
        ) explicitNonRoute = true;
        addTarget(textAttr(routeNode, 'data-ghostweb-target'));
        try {{
          var targetMap = JSON.parse(textAttr(routeNode, 'data-ghostweb-target-map') || '{{}}');
          Object.keys(targetMap || {{}}).forEach(function(key) {{ addTarget(targetMap[key]); }});
        }} catch (_) {{}}
        var handler = textAttr(routeNode, 'onclick');
        var match = handler.match(/(?:navigate|returnBack)\\s*\\(\\s*['\"]([^'\"]+)['\"]/);
        if (match) addTarget(match[1]);
        var identity = [
          textAttr(routeNode, 'id'),
          textAttr(routeNode, 'aria-label'),
          textAttr(routeNode, 'data-ghostweb-action'),
          textAttr(routeNode, 'data-ghostweb-route-action')
        ].join(' ');
        if (/back|返回|上一页|history_back|returnBack/i.test(identity)) backLike = true;
        var routeHost = routeNode.matches && routeNode.matches(
          'button, a[href], [role="button"], [data-ghostweb-action], '
          + '[data-ghostweb-route-action], [data-ghostweb-target], '
          + '[data-ghostweb-target-map], [onclick]'
        );
        // Search for a back icon only inside the clicked interactive host.
        // Looking through an arbitrary ancestor can see an unrelated shell or
        // footer icon and misclassify every click on the page as the trace's
        // sole back/route checkpoint.
        if (
          routeHost
          && routeNode.querySelector
          && routeNode.querySelector('[data-lucide="chevron-left"], [data-lucide="arrow-left"]')
        ) backLike = true;
      }}
      var candidates = [];
      Object.keys(CP_MAP_BY_ACTION_ID).forEach(function(actionId) {{
        var entries = CP_MAP_BY_ACTION_ID[actionId] || [];
        var routeEntry = entries.find(function(entry) {{
          return entry
            && (entry.trace_id === activeTrace || entry.trace_id === sourceReferenceTrace)
            && entry.checkpoint_kind === 'route'
            && entry.navigation_target_filename;
        }});
        // Enhanced task episodes may intentionally omit a separate
        // source_reference_task_id while their semantic spec still consumes
        // a Reference-owned route action.  The visible target-map is the
        // producer binding in that case; recover a unique route entry by
        // target, while the strict trace filter below still prevents emitting
        // a foreign checkpoint stream.
        if (!routeEntry) routeEntry = entries.find(function(entry) {{
          return entry
            && entry.checkpoint_kind === 'route'
            && entry.navigation_target_filename;
        }});
        if (routeEntry) candidates.push({{ actionId: actionId, target: String(routeEntry.navigation_target_filename) }});
      }});
      var matched = candidates.filter(function(candidate) {{ return targets.indexOf(candidate.target) >= 0; }});
      if (!explicitNonRoute) {{
        matched.forEach(function(candidate) {{ collect(candidate.actionId); }});
        if (!matched.length && backLike && candidates.length === 1) collect(candidates[0].actionId);
      }}
    }}
    // A shared visible host can carry aliases for many native tasks. Once the
    // loader has bound an exact current task/trace, retain only aliases owned
    // by that live context. Falling back to all ids remains necessary for an
    // enhanced episode that intentionally consumes a Reference-owned action
    // namespace without publishing a separate source trace id.
    var context = activeExecutionContext();
    var contextTraceIds = [
      context && (context.task_id || context.taskId),
      context && context.trace_id,
      context && context.reference_trace_id,
      context && (context.source_reference_task_id || context.sourceReferenceTaskId),
      explicitActiveTraceId()
    ].map(function(value) {{ return String(value || '').trim(); }}).filter(Boolean);
    var contextualIds = ids.filter(function(id) {{
      return contextTraceIds.indexOf(traceIdFromActionId(id)) >= 0;
    }});
    return contextualIds.length ? contextualIds : ids;
  }}

  function activeExecutionContext() {{
    var ctx = null;
    try {{ ctx = window.__ghostwebExecutionContext || null; }} catch (e) {{}}
    if (!ctx || (!ctx.reference_trace_id && !ctx.task_id && !ctx.action_id)) {{
      try {{ ctx = JSON.parse(localStorage.getItem('ghostweb.execution_context') || '{{}}'); }} catch (e) {{ ctx = {{}}; }}
    }}
    return ctx || {{}};
  }}

  function explicitActiveTraceId() {{
    var ctx = activeExecutionContext();
    // Runtime persistence follows the executable action/trace namespace, not
    // the logical evaluator task id.  Enhanced tasks commonly retain a
    // Reference-owned action namespace; initializing the store with task_id
    // and then emitting a Reference trace event would make setTraceId() clear
    // all state on every page transition.
    return String(
      window.GhostwebActiveTraceId
      || window.__ghostwebActiveTraceId
      || (ctx && (ctx.trace_id || ctx.reference_trace_id))
      || (ctx && (ctx.source_reference_task_id || ctx.sourceReferenceTaskId))
      || (ctx && (ctx.task_id || ctx.taskId))
      || ''
    );
  }}

  function activeTraceId() {{
    return explicitActiveTraceId() || TRACE_ID || '';
  }}

  function activeActionId() {{
    var ctx = activeExecutionContext();
    return String(window.GhostwebActiveActionId || window.__ghostwebActiveActionId || (ctx && ctx.action_id) || '');
  }}

  function traceIdFromActionId(actionId) {{
    var text = String(actionId || '');
    var idx = text.indexOf(':');
    return idx > 0 ? text.substring(0, idx) : '';
  }}

  function recordActionHostBindingFailure(binding, count) {{
    var rows = window.__ghostwebActionHostBindingFailures || [];
    rows.push({{
      action_id: binding && binding.action_id || '',
      selector: binding && binding.selector || '',
      source_filename: binding && binding.source_filename || '',
      observed_match_count: count,
      reason: 'selector_not_unique',
      observed_at_ms: Date.now()
    }});
    window.__ghostwebActionHostBindingFailures = rows.slice(-32);
  }}

  function boundActionHost(binding) {{
    if (!binding || currentFilenameForObservation() !== binding.source_filename) return null;
    var matches;
    try {{ matches = document.querySelectorAll(binding.selector); }} catch (_) {{
      recordActionHostBindingFailure(binding, -1);
      return null;
    }}
    if (matches.length !== 1) {{
      recordActionHostBindingFailure(binding, matches.length);
      return null;
    }}
    return matches[0];
  }}

  // A dynamic host is eligible only after the exact, uniquely resolved DOM
  // node receives a user click.  The page-owned custom event then proves that
  // its reducer committed the matching state transition.  No execution
  // context, step order, text, task literal, expected state, or evaluator
  // assertion participates in this join.
  var pendingActionHostObservations = {{}};
  var latestActionHostStateByEventName = {{}};
  function rememberActionHostObservation(target) {{
    ACTION_HOST_OBSERVATION_BINDINGS.forEach(function(binding) {{
      var host = boundActionHost(binding);
      if (!host || !(host === target || (host.contains && host.contains(target)))) return;
      var observation = {{
        host: host,
        target: target,
        app_state_before: latestActionHostStateByEventName[binding.event_name]
          ? JSON.parse(JSON.stringify(latestActionHostStateByEventName[binding.event_name])) : null,
        observed_at_ms: Date.now()
      }};
      pendingActionHostObservations[binding.action_id] = observation;
      // The page-owned reducer dispatches its observation synchronously from
      // the same GUI event.  Expire the click token at the next task so an
      // unrelated later event with the same semantic reason cannot redeem a
      // stale click and award a checkpoint early.
      window.setTimeout(function() {{
        if (pendingActionHostObservations[binding.action_id] === observation) {{
          delete pendingActionHostObservations[binding.action_id];
        }}
      }}, 0);
    }});
  }}

  function emitActionHostObservableEvent(event) {{
    var detail = event && event.detail || {{}};
    var reason = String(detail && detail.reason || '');
    var appStateAfter = detail && detail.state && typeof detail.state === 'object'
      && !Array.isArray(detail.state) ? JSON.parse(JSON.stringify(detail.state)) : null;
    if (appStateAfter) latestActionHostStateByEventName[event.type] = appStateAfter;
    ACTION_HOST_OBSERVATION_BINDINGS.forEach(function(binding) {{
      if (!binding || binding.event_name !== event.type || binding.event_reason !== reason) return;
      var pending = pendingActionHostObservations[binding.action_id];
      if (!pending) return;
      delete pendingActionHostObservations[binding.action_id];
      var host = boundActionHost(binding);
      if (!host || host !== pending.host) return;
      var appStateBefore = pending.app_state_before;
      if (!appStateBefore || !appStateAfter) return;
      var beforeChanged = {{}}, afterChanged = {{}};
      var keys = Array.from(new Set(Object.keys(appStateBefore).concat(Object.keys(appStateAfter)))).sort();
      keys.forEach(function(key) {{
        var beforeValue = appStateBefore[key];
        var afterValue = appStateAfter[key];
        if (JSON.stringify(beforeValue) === JSON.stringify(afterValue)) return;
        beforeChanged[key] = beforeValue === undefined ? null : beforeValue;
        afterChanged[key] = afterValue === undefined ? null : afterValue;
      }});
      if (!Object.keys(afterChanged).length) return;
      var runtime = window.GhostwebCheckpointRuntime;
      if (!runtime) return;
      runtime.emitCheckpoint(binding.observation_checkpoint_id, {{
        observed_page: currentFilenameForObservation(),
        observed_event_type: binding.event_name,
        observed_event_reason: binding.event_reason,
        observed_host_text: String(host.innerText || host.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 512)
      }}, {{
        actionId: binding.action_id,
        traceId: traceIdFromActionId(binding.action_id),
        stateSnapshotBefore: {{app_owned_state: beforeChanged}},
        stateSnapshotAfter: {{app_owned_state: afterChanged}}
      }});
    }});
  }}

  function traceMatchesActiveContext(entry, actionId, activeTrace) {{
    if (!activeTrace || !entry) return true;
    var ctx = activeExecutionContext();
    var taskId = String(ctx && (ctx.task_id || ctx.taskId) || '').trim();
    var sourceReferenceTrace = String(
      ctx && (ctx.source_reference_task_id || ctx.sourceReferenceTaskId) || ''
    ).trim();
    // The task context is the checkpoint-stream owner.  Enhanced episodes
    // intentionally reuse a Reference-owned visible action host, and the
    // context bridge consequently exposes that Reference trace through
    // GhostwebActiveTraceId.  A task-owned binding is always valid. A
    // source-reference binding is valid only when the task explicitly names
    // that source trace and this exact action is declared by the binding;
    // active trace alone must not select a foreign checkpoint stream.
    if (taskId) {{
      if (String(entry.trace_id || '') === taskId) return true;
      if (sourceReferenceTrace && String(entry.trace_id || '') === sourceReferenceTrace) {{
        var sourceActionIds = Array.isArray(entry.source_action_ids)
          ? entry.source_action_ids.map(function(value) {{ return String(value || ''); }})
          : [];
        return sourceActionIds.indexOf(actionId) >= 0 || String(entry.action_id || '') === actionId;
      }}
      return false;
    }}
    if (activeTrace === String(entry.trace_id || '')) return true;
    return !!sourceReferenceTrace && sourceReferenceTrace === String(entry.trace_id || '');
  }}

  function actionMatchesActiveContext(entry, actionId, activeAction, hostActionIds) {{
    if (!activeAction || activeAction === actionId) return true;
    var equivalentIds = [];
    if (entry && Array.isArray(entry.source_action_ids)) {{
      equivalentIds = equivalentIds.concat(entry.source_action_ids);
    }}
    if (entry && Array.isArray(entry.action_aliases)) {{
      equivalentIds = equivalentIds.concat(entry.action_aliases);
    }}
    // Context may disambiguate aliases owned by this nearest host.  If it
    // names no id on the host, it is stale context from the preceding action
    // (or from a broader ancestor) and cannot veto the directly clicked host.
    var hostIds = Array.isArray(hostActionIds) ? hostActionIds : [actionId];
    if (hostIds.indexOf(activeAction) === -1) return true;
    if (equivalentIds.indexOf(actionId) !== -1) return true;
    return equivalentIds.indexOf(activeAction) !== -1;
  }}

  function writeRuntimeStateDiffs(entry, eventType) {{
    // Evaluator assertions are never runtime producers. Business/runtime
    // state is written only by the page
    // reducer and the action host.  Checkpoint emission may read the current
    // DOM/runtime snapshot but must not manufacture state to satisfy a rule.
    return null;
  }}

  function currentFilenameForObservation() {{
    try {{ return decodeURIComponent(String(window.location.pathname || '').split('/').pop() || ''); }}
    catch (_) {{ return String(window.location.pathname || '').split('/').pop() || ''; }}
  }}

  function observedActionRuntimeRow(actionId) {{
    try {{
      var rows = window.__GHOSTWEB_DOM_BINDING_ACTIONS || {{}};
      var row = rows && rows[actionId];
      return row && typeof row === 'object' ? row : {{}};
    }} catch (_) {{
      return {{}};
    }}
  }}

  function observedTargetFromCurrentHost(target, observedHost, actionId, runtimeSnapshot) {{
    var nodes = [];
    function addNodeAndAncestors(node) {{
      for (var current = node, depth = 0; current && depth < 8; current = current.parentElement, depth += 1) {{
        if (current.getAttribute && nodes.indexOf(current) === -1) nodes.push(current);
      }}
    }}
    // The visible control can be an input nested in a producer-owned route
    // host. Read that host's materialized target map; do not fall back to a
    // checkpoint fixture when the nested control itself has no target.
    addNodeAndAncestors(target);
    addNodeAndAncestors(observedHost);
    for (var index = 0; index < nodes.length; index += 1) {{
      var node = nodes[index];
      try {{
        var targetMap = JSON.parse(node.getAttribute('data-ghostweb-target-map') || '{{}}');
        var mapped = String(targetMap && targetMap[actionId] || '').trim();
        if (mapped) return mapped.replace(/^.*\\//, '');
      }} catch (_) {{}}
      var direct = String(
        node.getAttribute('data-ghostweb-target')
        || node.getAttribute('href')
        || ''
      ).trim();
      if (direct) return direct.replace(/^.*\\//, '');
    }}
    // Gesture-route producers commit the real destination into runtime state
    // before checkpoint capture. Consume that app-owned write only when its
    // source metadata belongs to this exact action; never substitute the
    // checkpoint contract's expected target.
    var currentState = runtimeSnapshot && runtimeSnapshot.current_state || {{}};
    var sourceIndex = runtimeSnapshot && runtimeSnapshot.source_index || {{}};
    var targetSource = sourceIndex.target_filename || {{}};
    var sourceActionId = String(targetSource.meta && targetSource.meta.action_id || '');
    var runtimeTarget = String(currentState.target_filename || '').trim();
    if (runtimeTarget && sourceActionId && sourceActionId === actionId) {{
      return runtimeTarget.replace(/^.*\\//, '');
    }}
    return '';
  }}

  function currentDomActionObservation(entry, target, eventType, observedHost, payload) {{
    var actionId = String(entry && entry.action_id || '');
    var runtimeRow = observedActionRuntimeRow(actionId);
    var hostPrimaryActionId = observedHost && observedHost.getAttribute
      ? String(observedHost.getAttribute('data-action-id') || '') : '';
    var hostOwnsExactAction = actionIdsFromElement(observedHost || target).indexOf(actionId) >= 0;
    return {{
      action_id: actionId,
      trace_id: String(entry && entry.trace_id || traceIdFromActionId(actionId) || ''),
      dom_binding_id: String(
        runtimeRow.dom_binding_id
        || (hostOwnsExactAction && hostPrimaryActionId === actionId && observedHost && observedHost.getAttribute
          ? observedHost.getAttribute('data-ghostweb-dom-binding-id') || '' : '')
      ),
      effect_kind: String(
        runtimeRow.effect_kind
        || (hostOwnsExactAction && hostPrimaryActionId === actionId && observedHost && observedHost.getAttribute
          ? observedHost.getAttribute('data-ghostweb-effect-kind') || '' : '')
      ),
      trigger: eventType || '',
      observed_page: payload.observed_page || '',
      observed_target_filename: payload.observed_target_filename || '',
      observed_host_text: payload.observed_host_text || '',
      observed_control_value: payload.observed_control_value || '',
      observed_selected: !!payload.observed_selected,
      observed_aria_expanded: payload.observed_aria_expanded || '',
      runtime_snapshot: payload.observed_runtime_snapshot || null,
      producer: 'current_dom_event_host',
      observed_at_ms: Date.now()
    }};
  }}

  function observedPayload(entry, target, eventType) {{
    var body = document && document.body ? String(document.body.innerText || '').replace(/\\s+/g, ' ').trim() : '';
    var observedHost = target && target.closest
      ? target.closest('[data-action-id], [data-action-aliases], [data-ghostweb-dom-binding-id], a[href], button, [role="button"], input, textarea, select')
      : target;
    var payload = {{}};
    payload.observed_page = currentFilenameForObservation();
    payload.observed_dom_excerpt = body.slice(0, 2400);
    payload.observed_dom_nonempty = body.length > 0;
    payload.observed_event_type = eventType || '';
    payload.observed_host_text = observedHost
      ? String(observedHost.innerText || observedHost.textContent || '').replace(/\\s+/g, ' ').trim()
      : '';
    payload.observed_control_value = targetTextValue(observedHost || target);
    var selectedValue = selectionValueFromTarget(observedHost || target);
    if (selectedValue) payload.selected_value = selectedValue;
    payload.observed_selected = !!(observedHost && observedHost.getAttribute && (
      observedHost.getAttribute('data-selected') === 'true'
      || observedHost.getAttribute('aria-selected') === 'true'
      || observedHost.getAttribute('aria-checked') === 'true'
    ));
    payload.observed_aria_expanded = observedHost && observedHost.getAttribute
      ? String(observedHost.getAttribute('aria-expanded') || '') : '';
    payload.observed_effect_kind = observedHost && observedHost.getAttribute
      ? String(observedHost.getAttribute('data-ghostweb-effect-kind') || '') : '';
    if (payload.observed_effect_kind) payload.effect_kind = payload.observed_effect_kind;
    try {{
      payload.observed_runtime_snapshot = window.GhostwebRuntimeState && window.GhostwebRuntimeState.snapshot
        ? window.GhostwebRuntimeState.snapshot() : null;
    }} catch (_) {{
      payload.observed_runtime_snapshot = null;
    }}
    var actionId = String(entry && entry.action_id || '');
    payload.observed_target_filename = observedTargetFromCurrentHost(
      target,
      observedHost,
      actionId,
      payload.observed_runtime_snapshot
    );
    if (entry && entry.checkpoint_kind === 'route' && payload.observed_target_filename) {{
      payload.target_filename = payload.observed_target_filename;
    }}
    // The checkpoint payload must be complete at the capture boundary. A
    // later bubbling observer may persist the same action for audit, but it is
    // not the producer and cannot repair an already-emitted checkpoint.
    payload.observed_dom_action = currentDomActionObservation(
      entry,
      target,
      eventType,
      observedHost,
      payload
    );
    return payload;
  }}

  function targetTextValue(target) {{
    if (!target) return '';
    var ownedSelection = selectionValueFromTarget(target);
    if (ownedSelection) return ownedSelection;
    var value = '';
    if ('value' in target) {{
      value = target.value;
    }} else {{
      value = target.textContent || '';
    }}
    return String(value == null ? '' : value).trim();
  }}

  function selectionValueFromTarget(target) {{
    if (!target) return '';
    var selectionHost = target.closest ? target.closest('[data-selection-value]') : null;
    if (!selectionHost || !selectionHost.getAttribute) return '';
    var value = selectionHost.getAttribute('data-selection-value');
    return value === null ? '' : String(value).trim();
  }}

  function shouldEmitForTarget(entry, target, eventType) {{
    // Emission records what happened. Expected payload/state fixtures must not
    // decide whether the observed GUI action counts as correct.
    return !!(entry && target);
  }}

  function usesDeclaredExternalObservationProducer(entry) {{
    var producer = String(entry && entry.external_observation_producer || '');
    return producer === 'shared_route_runtime_checkpoint_bridge_v2'
      || /^evaluators\\//.test(producer);
  }}

  function observedTerminalStateSatisfied(entry) {{
    // Emission records a real action/arrival observation, not a success
    // verdict.  The browser evaluator independently re-reads live DOM/app
    // state against its terminal assertion contract.
    return !!(document && document.body);
  }}

  function eventMatchesOperation(entry, eventType, target) {{
    var operation = String(entry && entry.operation || '').toLowerCase();
    if (operation === 'click' || operation === 'tap') return eventType === 'click';
    // A text-entry checkpoint is causally owned by the input value changing.
    // Pointer events can precede or follow a Playwright fill/type sequence,
    // but they must never redeem the checkpoint before the app-owned input
    // reducer has written the query state.
    if (operation === 'fill' || operation === 'type' || operation === 'input_text') {{
      return eventType === 'input' || eventType === 'change';
    }}
    if (operation === 'scroll' || operation === 'swipe') {{
      // A bare scroll event is also emitted by programmatic scrollTo/scrollBy
      // calls that materialize a tab or overlay.  It is therefore not proof of
      // the user-authored scroll checkpoint.  Accept only input-originating
      // gesture events here; the terminal DOM predicate still verifies the
      // resulting visible state.
      if (['wheel', 'touchend'].indexOf(eventType) >= 0) return true;
      if (eventType !== 'pointerup') return false;
      // Pointer-driven horizontal carousels complete with pointerup rather
      // than wheel/touchend. Accept that signal only from a producer-owned
      // gesture host; ordinary/programmatic page scrolling has no such host.
      for (var node = target, depth = 0; node && depth < 8; node = node.parentElement, depth += 1) {{
        if (
          textAttr(node, 'data-ghostweb-gesture-action')
          || textAttr(node, 'data-gesture-host')
        ) return true;
      }}
      return false;
    }}
    if (entry && entry.checkpoint_kind === 'draft') {{
      return eventType === 'input' || eventType === 'change';
    }}
    return true;
  }}

  function isTextEntryOperation(entry) {{
    var operation = String(entry && entry.operation || '').toLowerCase();
    return operation === 'fill' || operation === 'type' || operation === 'input_text';
  }}

  function checkpointMayNavigate(entry) {{
    var effectKind = String(entry && entry.effect_kind || '').toLowerCase();
    if (entry && entry.checkpoint_kind === 'route' || effectKind === 'route') return true;
    var targetFilename = String(entry && entry.navigation_target_filename || '').trim();
    if (!targetFilename) return false;
    var currentFilename = '';
    try {{
      currentFilename = decodeURIComponent(String(window.location.pathname || '').split('/').pop() || '');
    }} catch (_) {{
      currentFilename = String(window.location.pathname || '').split('/').pop() || '';
    }}
    return !!currentFilename && targetFilename !== currentFilename;
  }}

  function rememberPreActionCaptures(target) {{
    var runtime = window.GhostwebCheckpointRuntime;
    if (!runtime || !target) return;
    var snapshot = runtime.captureState();
    if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return;
    actionIdsFromElement(target).forEach(function(actionId) {{
      var entries = CP_MAP_BY_ACTION_ID[actionId] || [];
      if (!entries.some(function(entry) {{ return entry && entry.app_owned_capture_envelope; }})) return;
      pendingPreActionCaptures[actionId] = {{ snapshot: snapshot, captured_at: Date.now() }};
    }});
  }}

  function consumePreActionCapture(actionId) {{
    var pending = pendingPreActionCaptures[actionId];
    delete pendingPreActionCaptures[actionId];
    if (!pending || Date.now() - Number(pending.captured_at || 0) > 5000) return null;
    var snapshot = pending.snapshot;
    return snapshot && typeof snapshot === 'object' && !Array.isArray(snapshot) ? snapshot : null;
  }}

  function emitForAction(actionId, eventType, target, synchronousBeforeNavigation, hostActionIds) {{
    // A single visible commit host can own both its process mutation and the
    // terminal outcome. Preserve the checkpoint dependency order by emitting
    // the outcome only after non-outcome entries on that host.
    var entries = (CP_MAP_BY_ACTION_ID[actionId] || []).slice().sort(function(left, right) {{
      var leftOutcome = left && left.checkpoint_kind === 'outcome' ? 1 : 0;
      var rightOutcome = right && right.checkpoint_kind === 'outcome' ? 1 : 0;
      return leftOutcome - rightOutcome;
    }});
    var runtime = window.GhostwebCheckpointRuntime;
    if (!runtime) return;
    var activeAction = activeActionId();
    var activeTrace = explicitActiveTraceId() || traceIdFromActionId(actionId);
    entries.forEach(function(entry) {{
      if (!entry || !entry.checkpoint_id) return;
      // Producer ownership is exclusive.  When the compiled binding declares
      // the shared route bridge, this page-local harness must not emit an
      // expected-literal candidate for the same checkpoint first.  The shared
      // bridge observes the accepted DOM event, validates any input-dependent
      // expected value and persists the only authoritative event.  Falling
      // back here would let an empty/partial input consume the deduplication
      // key and block the later valid query event.
      if (usesDeclaredExternalObservationProducer(entry)) return;
      if (!actionMatchesActiveContext(entry, actionId, activeAction, hostActionIds)) return;
      if (!traceMatchesActiveContext(entry, actionId, activeTrace)) return;
      if (!eventMatchesOperation(entry, eventType, target)) return;
      if (!shouldEmitForTarget(entry, target, eventType)) return;
      if (entry.trace_id && runtime.setTraceId) runtime.setTraceId(entry.trace_id);
      var fireKey = (entry.trace_id || TRACE_ID || '') + ':' + (entry.action_id || actionId || '') + ':' + entry.checkpoint_id;
      if (fired[fireKey]) return;{cross_page_checkpoint_emit_guard}{cross_page_emit_guard}
      // A click might arrive after a pointerup/touchend app handler has
      // changed the document. Consume the capture taken at the earlier window
      // capture boundary; keyboard/programmatic clicks have no such gesture
      // and fall back to the still-real click-boundary capture.
      var appOwnedCaptureBefore = null;
      if (entry.app_owned_capture_envelope) {{
        appOwnedCaptureBefore = eventType === 'click' ? consumePreActionCapture(actionId) : null;
        if (!appOwnedCaptureBefore) appOwnedCaptureBefore = runtime.captureState();
      }}
      function emitMaterializedCheckpoint() {{
        if (entry.trace_id && runtime.setTraceId) runtime.setTraceId(entry.trace_id);
        var requiresObservedState = (
          entry.checkpoint_kind === 'outcome'
          || ['pointerup', 'touchend', 'wheel'].indexOf(eventType) >= 0
        );
        if (requiresObservedState && !observedTerminalStateSatisfied(entry)) return;
        if (fired[fireKey]) return;
        var expectedPayload = observedPayload(entry, target, eventType);
        writeRuntimeStateDiffs(entry, eventType);
        var emissionContext = {{
          actionId: entry.action_id || actionId || '',
          traceId: entry.trace_id || TRACE_ID || ''
        }};
        if (entry.app_owned_capture_envelope) {{
          var emissionAfter = runtime.captureState();
          emissionContext.stateSnapshotBefore = {{app_owned_capture: appOwnedCaptureBefore}};
          emissionContext.stateSnapshotAfter = {{app_owned_capture: emissionAfter}};
        }}
        var persistedEvent = runtime.emitCheckpoint(entry.checkpoint_id, Object.assign(expectedPayload, {{
          trace_id: entry.trace_id || TRACE_ID || '',
          checkpoint_id: entry.checkpoint_id || '',
          source_steps: entry.source_steps || [],
          action_id: Object.prototype.hasOwnProperty.call(expectedPayload, 'action_id')
            ? expectedPayload.action_id
            : (entry.action_id || actionId || ''),
          checkpoint_kind: entry.checkpoint_kind || '',
          operation: entry.operation || '',
          operation_intent: entry.operation_intent || '',
          event_type: eventType,
          event: entry.event || '',
        }}), emissionContext);
        // Exactly-once means successfully persisted exactly once.  A failed
        // session-backed write must not consume this page-realm dedupe key:
        // deferred retries and a later navigation still need a chance to
        // carry the formal event to the evaluator.
        if (!persistedEvent) return;
        fired[fireKey] = true;
      }}
      var requiresPostActionState = (
        (
          eventType === 'click'
          && entry.checkpoint_kind !== 'route'
          && !checkpointMayNavigate(entry)
          && !synchronousBeforeNavigation
        )
        || ['pointerup', 'touchend', 'wheel'].indexOf(eventType) >= 0
        // The document-level bridge is installed before page-local input
        // reducers. Defer text checkpoint observation by one task so the
        // reducer handling this very event can persist app-owned query state
        // before payload/runtime snapshots are captured.
        || (isTextEntryOperation(entry) && ['input', 'change'].indexOf(eventType) >= 0)
      );
      if (requiresPostActionState) {{
        // Give process checkpoints queued by the same visible gesture one
        // event-loop turn to persist before the dependent terminal outcome.
        // This preserves dependency order without changing either predicate.
        window.setTimeout(emitMaterializedCheckpoint, entry.checkpoint_kind === 'outcome' ? 25 : 0);
      }} else {{
        emitMaterializedCheckpoint();
      }}
    }});
  }}

  function emitFromTarget(target, eventType) {{
    var commitHost = target && target.closest ? target.closest('[data-ghostweb-commit-precondition]') : null;
    if (commitHost && commitHost.getAttribute('data-ghostweb-commit-ready') !== 'true') return;
    var orderedActionIds = actionIdsFromElement(target).sort(function(leftId, rightId) {{
      function checkpointIds(actionId) {{
        return (CP_MAP_BY_ACTION_ID[actionId] || []).map(function(entry) {{
          return entry && entry.checkpoint_id;
        }}).filter(Boolean);
      }}
      function dependsOnAny(actionId, checkpointIdsToMatch) {{
        return (CP_MAP_BY_ACTION_ID[actionId] || []).some(function(entry) {{
          var dependencies = entry && entry.depends_on;
          return Array.isArray(dependencies) && dependencies.some(function(checkpointId) {{
            return checkpointIdsToMatch.indexOf(checkpointId) >= 0;
          }});
        }});
      }}
      var leftCheckpointIds = checkpointIds(leftId);
      var rightCheckpointIds = checkpointIds(rightId);
      if (dependsOnAny(leftId, rightCheckpointIds)) return 1;
      if (dependsOnAny(rightId, leftCheckpointIds)) return -1;
      function outcomeOnly(actionId) {{
        var entries = CP_MAP_BY_ACTION_ID[actionId] || [];
        return entries.length && entries.every(function(entry) {{ return entry && entry.checkpoint_kind === 'outcome'; }});
      }}
      return Number(outcomeOnly(leftId)) - Number(outcomeOnly(rightId));
    }});
    // A route link may be the canonical visible host while its generated
    // markup carries only a target-map (no data-action-id).  Materialize the
    // inferred source action on that host before the app semantic runtime's
    // bubbling listener observes the same click.  This keeps route authority
    // producer-owned and lets enhanced tasks consume source action aliases
    // without selector or label heuristics.
    if (orderedActionIds.length && target && target.closest) {{
      var routeHost = target.closest('a[href], button, [role="button"], [data-ghostweb-target-map], [onclick]');
      if (routeHost && !textAttr(routeHost, 'data-action-id') && !textAttr(routeHost, 'data-action-aliases')) {{
        routeHost.setAttribute('data-action-id', orderedActionIds[0]);
      }}
    }}
    var synchronousBeforeNavigation = eventType === 'click' && orderedActionIds.some(function(actionId) {{
      return (CP_MAP_BY_ACTION_ID[actionId] || []).some(checkpointMayNavigate);
    }});
    orderedActionIds.forEach(function(actionId) {{
      if (CP_MAP_BY_ACTION_ID[actionId]) {{
        emitForAction(actionId, eventType, target, synchronousBeforeNavigation, orderedActionIds);
        if (eventType === 'input' || eventType === 'change') {{
          window.setTimeout(function() {{ emitForAction(actionId, eventType, target, false, orderedActionIds); }}, 50);
          window.setTimeout(function() {{ emitForAction(actionId, eventType, target, false, orderedActionIds); }}, 250);
        }}
        if (
          eventType === 'click'
          && (CP_MAP_BY_ACTION_ID[actionId] || []).some(function(entry) {{
            return entry
              && entry.checkpoint_kind === 'outcome'
              && !entry.terminal_observable_page_filename
              && entry.has_terminal_observable_dom_bindings;
          }})
        ) {{
          // Same-page business commits may settle after an animation, mock
          // request, or bounded async projection.  Re-observe the declared
          // terminal DOM instead of emitting on click or requiring every
          // page to manufacture an extra task-specific event.  ``fired``
          // preserves exactly-once reward semantics across these retries.
          window.setTimeout(function() {{ emitForAction(actionId, eventType, target, false, orderedActionIds); }}, 500);
          window.setTimeout(function() {{ emitForAction(actionId, eventType, target, false, orderedActionIds); }}, 1200);
          window.setTimeout(function() {{ emitForAction(actionId, eventType, target, false, orderedActionIds); }}, 1900);
        }}
      }}
    }});
  }}

  function hasDraftCheckpoint(actionId) {{
    return (CP_MAP_BY_ACTION_ID[actionId] || []).some(function(entry) {{
      return (
        entry && entry.checkpoint_kind === 'draft'
        || entry && entry.event === 'query_projection_updated'
        || entry && entry.effect_kind === 'query_projection'
      );
    }});
  }}

  function installDraftMaterializationWatchers() {{
    var selector = 'input[data-action-id], input[data-action-aliases], textarea[data-action-id], textarea[data-action-aliases], [contenteditable][data-action-id], [contenteditable][data-action-aliases]';
    Array.prototype.slice.call(document.querySelectorAll(selector)).forEach(function(target) {{
      var ids = actionIdsFromElement(target).filter(hasDraftCheckpoint);
      if (!ids.length) return;
      var initialValue = targetTextValue(target);
      var attempts = 0;
      var timer = window.setInterval(function() {{
        attempts += 1;
        var currentValue = targetTextValue(target);
        if (currentValue !== initialValue) {{
          ids.forEach(function(actionId) {{ emitForAction(actionId, 'input', target, false, ids); }});
        }}
        var allFired = ids.every(function(actionId) {{
          return (CP_MAP_BY_ACTION_ID[actionId] || []).every(function(entry) {{
            var fireKey = (entry.trace_id || TRACE_ID || '') + ':' + (entry.action_id || actionId || '') + ':' + entry.checkpoint_id;
            return !!fired[fireKey];
          }});
        }});
        if (allFired || attempts >= 240) window.clearInterval(timer);
      }}, 50);
    }});
  }}

  // Route/action materializers may navigate synchronously from a document
  // capture listener. Observe clicks at the window capture boundary so the
  // checkpoint is persisted before the old document unloads.
  // Some WebEnv drivers synthesize the MouseEvent sequence rather than a
  // PointerEvent sequence. Both down boundaries precede a target click
  // handler, so they are equivalent real observations for this purpose.
  ['pointerdown', 'mousedown', 'touchstart'].forEach(function(eventType) {{
    window.addEventListener(eventType, function(e) {{
      rememberPreActionCaptures(e.target);
    }}, true);
  }});

  window.addEventListener('click', function(e) {{
    // Keep a capture at the actual click dispatch boundary too. This covers
    // automation drivers that expose only a click event, while still running
    // before target/bubbling app handlers mutate same-page DOM state.
    rememberPreActionCaptures(e.target);
    rememberActionHostObservation(e.target);
    emitFromTarget(e.target, 'click');
  }}, true);

  Array.from(new Set(ACTION_HOST_OBSERVATION_BINDINGS.map(function(binding) {{
    return binding && binding.event_name;
  }}).filter(Boolean))).forEach(function(eventName) {{
    window.addEventListener(eventName, emitActionHostObservableEvent, true);
  }});

  document.addEventListener('change', function(e) {{
    emitFromTarget(e.target, 'change');
  }}, true);

  document.addEventListener('input', function(e) {{
    emitFromTarget(e.target, 'input');
  }}, true);

  ['pointerup', 'touchend', 'wheel', 'scroll'].forEach(function(eventType) {{
    document.addEventListener(eventType, function(e) {{
      emitFromTarget(e.target, eventType);
    }}, true);
  }});

  window.GhostwebCheckpointActionMap = {{
    byActionId: CP_MAP_BY_ACTION_ID,
    emitForAction: emitForAction,
    actionIdsFromElement: actionIdsFromElement
  }};

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', function() {{
      var rt = window.GhostwebCheckpointRuntime;
      if (rt) rt.init(activeTraceId());
      installDraftMaterializationWatchers();{cross_page_checkpoint_init}{cross_page_init}
    }});
  }} else {{
    var rt = window.GhostwebCheckpointRuntime;
    if (rt) rt.init(activeTraceId());
    installDraftMaterializationWatchers();{cross_page_checkpoint_init}{cross_page_init}
  }}
}})();
"""


_OBLIGATIONS_RE = re.compile(
    r'const\\s+OBLIGATIONS\\s*=\\s*(\\[.*?\\]);',
    re.DOTALL,
)


def _extract_obligations(html: str) -> list[dict[str, Any]]:
    m = _OBLIGATIONS_RE.search(html)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return []


def _strip_existing_bridge(html: str) -> str:

    html = re.sub(
        r'\n?<script\s+data-ghostweb-runtime="checkpoint-bridge">.*?</script>',
        '',
        html,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'\n?<script\s+data-ghostweb-runtime="checkpoint-emission">.*?</script>',
        '',
        html,
        flags=re.DOTALL,
    )
    return html


_CHARSET_META_RE = re.compile(
    r"\s*<meta\b(?=[^>]*\bcharset\s*=)[^>]*>\s*",
    flags=re.IGNORECASE,
)


def _ensure_utf8_charset_meta(html: str) -> str:
    html = _CHARSET_META_RE.sub("\n", html)
    meta = '<meta charset="utf-8">'
    head_open = re.search(r"<head\b[^>]*>", html, flags=re.IGNORECASE)
    if head_open:
        return html[: head_open.end()] + f"\n{meta}" + html[head_open.end() :]
    html_open = re.search(r"<html\b[^>]*>", html, flags=re.IGNORECASE)
    if html_open:
        return html[: html_open.end()] + f"<head>\n{meta}\n</head>" + html[html_open.end() :]
    return f"<head>\n{meta}\n</head>" + html


def _inject_bridge_scripts(
    html: str,
    bridge_js: str,
    harness_js: str,
) -> str:
    html = _strip_existing_bridge(html)
    html = _ensure_utf8_charset_meta(html)
    bridge_block = f'<script data-ghostweb-runtime="checkpoint-bridge">\n{bridge_js}\n</script>'
    harness_block = f'<script data-ghostweb-runtime="checkpoint-emission">\n{harness_js}\n</script>'
    injection = f"\n{bridge_block}\n{harness_block}\n"

    head_close = re.search(r"</head\s*>", html, flags=re.IGNORECASE)
    if not head_close:
        html_open = re.search(r"<html\b[^>]*>", html, flags=re.IGNORECASE)
        if html_open:
            return html[: html_open.end()] + f"<head>{injection}</head>" + html[html_open.end() :]
        return f"<head>{injection}</head>" + html


    head_prefix = html[: head_close.start()].rstrip()
    return head_prefix + injection + html[head_close.start() :]


_CHECKPOINT_BRIDGE_BLOCK_RE = re.compile(
    r'<script\s+data-ghostweb-runtime="checkpoint-bridge">.*?</script>',
    flags=re.DOTALL,
)
_CHECKPOINT_EMISSION_BLOCK_RE = re.compile(
    r'<script\s+data-ghostweb-runtime="checkpoint-emission">(?P<body>.*?)</script>',
    flags=re.DOTALL,
)


def _read_embedded_harness_json(body: str, variable: str, *, required: bool) -> Any:
    match = re.search(
        rf"^\s*var\s+{re.escape(variable)}\s*=\s*(?P<value>.+?);\s*$",
        body,
        flags=re.MULTILINE,
    )
    if not match:
        if required:
            raise RuntimeError(f"checkpoint emission harness has no {variable} declaration")
        return []
    try:
        return json.loads(match.group("value"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid {variable} JSON in checkpoint emission harness") from exc


def _embedded_emission_contract(body: str) -> tuple[dict, str, list, list, list]:
    checkpoint_map = _read_embedded_harness_json(body, "CP_MAP_BY_ACTION_ID", required=True)


    action_hosts = _read_embedded_harness_json(body, "ACTION_HOST_OBSERVATION_BINDINGS", required=False)
    trace_id = _read_embedded_harness_json(body, "TRACE_ID", required=True)
    terminal_outcomes = _read_embedded_harness_json(body, "TERMINAL_OUTCOMES_ON_LOAD", required=False)
    cross_page_checkpoints = _read_embedded_harness_json(body, "CROSS_PAGE_CHECKPOINTS_ON_LOAD", required=False)
    if not isinstance(checkpoint_map, dict) or not isinstance(trace_id, str):
        raise RuntimeError("checkpoint emission map/trace contract is invalid")
    if not all(isinstance(rows, list) for rows in (action_hosts, terminal_outcomes, cross_page_checkpoints)):
        raise RuntimeError("checkpoint emission observation contract is invalid")
    return checkpoint_map, trace_id, terminal_outcomes, cross_page_checkpoints, action_hosts


def refresh_emission_runtime_from_embedded_contract(result_dir: Path) -> dict[str, Any]:
    pages_dir = _find_pages_dir(result_dir)
    if not pages_dir:
        raise RuntimeError(f"no pages directory found under {result_dir}")


    capture_keys = _cross_page_state_capture_keys(result_dir)
    envelope_keys = _app_owned_capture_envelope_keys(result_dir)
    page_contracts: list[tuple[Path, str, tuple[dict, str, list, list, list]]] = []
    for path in sorted(pages_dir.glob("*.html")):
        content = path.read_text(encoding="utf-8")
        matches = list(_CHECKPOINT_EMISSION_BLOCK_RE.finditer(content))
        if not matches:
            continue
        if len(matches) != 1:
            raise RuntimeError(f"expected one checkpoint emission harness in {path}")
        before = _embedded_emission_contract(matches[0].group("body"))
        projected_map: dict[str, list[dict[str, Any]]] = {}
        for action_id, entries in before[0].items():
            if not isinstance(entries, list):
                raise RuntimeError(f"invalid checkpoint entries in {path}: {action_id}")
            projected_entries: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise RuntimeError(f"invalid checkpoint entry in {path}: {action_id}")
                projected = dict(entry)
                key = (str(projected.get("trace_id") or ""), str(projected.get("checkpoint_id") or ""))
                if key in envelope_keys:
                    projected["app_owned_capture_envelope"] = True
                else:
                    projected.pop("app_owned_capture_envelope", None)
                target = str(projected.get("navigation_target_filename") or "").strip()
                projected["cross_page_state_capture"] = bool(
                    key in capture_keys and target and target != path.name
                )
                projected_entries.append(projected)
            projected_map[action_id] = projected_entries
        page_contracts.append((path, content, (projected_map, before[1], before[2], before[3], before[4])))

    pending: list[tuple[Path, str]] = []
    for path, content, before in page_contracts:
        cross_page_on_load: list[dict[str, Any]] = []
        seen_cross_page: set[tuple[str, str, str]] = set()
        for _source_path, _source_content, source in page_contracts:
            for action_id, entries in source[0].items():
                for entry in entries:
                    if not entry.get("cross_page_state_capture"):
                        continue
                    if str(entry.get("navigation_target_filename") or "") != path.name:
                        continue
                    key = (str(entry.get("trace_id") or ""), action_id, str(entry.get("checkpoint_id") or ""))
                    if key in seen_cross_page:
                        continue
                    on_load_entry = dict(entry)
                    on_load_entry["action_id"] = action_id
                    cross_page_on_load.append(on_load_entry)
                    seen_cross_page.add(key)
        replacement = _build_emission_harness_js(
            before[0], before[1], terminal_outcomes_on_load=before[2],
            cross_page_checkpoints_on_load=cross_page_on_load,
            action_host_observation_bindings=before[4],
            preserve_runtime_entry_fields=True,
        )
        after = _embedded_emission_contract(replacement)
        if after[0] != before[0] or after[1] != before[1] or after[2] != before[2] or after[4] != before[4]:
            raise RuntimeError(f"emission projection drift during runtime-only refresh: {path}")
        if after[3] != cross_page_on_load:
            raise RuntimeError(f"cross-page checkpoint projection drift during runtime-only refresh: {path}")
        updated = _CHECKPOINT_EMISSION_BLOCK_RE.sub(
            lambda _match: '<script data-ghostweb-runtime="checkpoint-emission">' + replacement + "</script>",
            content,
            count=1,
        )
        pending.append((path, updated))
    if not pending:
        raise RuntimeError("no checkpoint emission harnesses found")
    for path, updated in pending:
        path.write_text(updated, encoding="utf-8")
    return {
        "status": "pass",
        "mode": "emission_runtime_from_embedded_contract",
        "pages_dir": str(pages_dir),
        "instrumented_page_count": len(pending),
        "cross_page_state_capture_contract_keys": len(capture_keys),
        "app_owned_capture_envelope_contract_keys": len(envelope_keys),
        "bindings_recompiled": False,
        "checkpoint_emission_harness_changed": True,
    }


def _app_owned_capture_envelope_keys(contract_root: Path) -> set[tuple[str, str]]:
    plan_path = next(
        (
            candidate
            for candidate in (
                contract_root / "evaluators" / "browser_evaluator_plan.json",
                contract_root.parent / "evaluators" / "browser_evaluator_plan.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    if plan_path is None:
        return set()
    try:
        evaluators = json.loads(plan_path.read_text(encoding="utf-8")).get("evaluators", {})
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(evaluators, dict):
        return set()
    keys: set[tuple[str, str]] = set()
    for trace_id, evaluator in evaluators.items():
        if not isinstance(trace_id, str) or not isinstance(evaluator, dict):
            continue
        bindings = (evaluator.get("runtime_event_contract") or {}).get(
            "checkpoint_observation_bindings", {}
        )
        if not isinstance(bindings, dict):
            continue
        for checkpoint_id, rows in bindings.items():
            if isinstance(checkpoint_id, str) and isinstance(rows, list) and any(
                isinstance(row, dict) and row.get("state_pointer") == "/app_owned_capture"
                for row in rows
            ):
                keys.add((trace_id, checkpoint_id))
    return keys


def _has_page_authored_formal_checkpoint_event(html: str) -> bool:
    return all(
        anchor in html
        for anchor in (
            "__ghostweb_checkpoint_events_v1__",
            "ghostweb:checkpoint:",
            "data-reference-terminal-observation",
        )
    )


_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", flags=re.DOTALL | re.IGNORECASE)


def _page_authored_formal_event_blocks(html: str) -> list[str]:
    return [
        block
        for block in _SCRIPT_BLOCK_RE.findall(html)
        if 'data-ghostweb-runtime="checkpoint-bridge"' not in block
        and "__ghostweb_checkpoint_events_v1__" in block
        and "ghostweb:checkpoint:" in block
    ]


def _inject_bridge_only(html: str, replacement: str) -> str:
    if _CHECKPOINT_BRIDGE_BLOCK_RE.search(html):
        raise RuntimeError("bridge-only admission requires a page with no existing bridge")
    html = _ensure_utf8_charset_meta(html)
    injection = "\n" + replacement + "\n"
    head_close = re.search(r"</head\s*>", html, flags=re.IGNORECASE)
    if not head_close:
        html_open = re.search(r"<html\b[^>]*>", html, flags=re.IGNORECASE)
        if html_open:
            return html[: html_open.end()] + f"<head>{injection}</head>" + html[html_open.end() :]
        return f"<head>{injection}</head>" + html
    head_prefix = html[: head_close.start()].rstrip()
    return head_prefix + injection + html[head_close.start() :]


def refresh_bridge_runtime_only(
    result_dir: Path,
    *,
    admit_page_authored_formal_events: bool = False,
) -> dict[str, Any]:
    pages_dir = _find_pages_dir(result_dir)
    if not pages_dir:
        raise RuntimeError(f"no pages directory found under {result_dir}")
    html_files = sorted(pages_dir.glob("*.html"))
    if not html_files:
        raise RuntimeError(f"no generated HTML pages found under {pages_dir}")
    replacement = (
        '<script data-ghostweb-runtime="checkpoint-bridge">\n'
        + BRIDGE_JS
        + "\n</script>"
    )
    pending: list[tuple[Path, str]] = []
    skipped_uninstrumented: list[str] = []
    admitted_page_authored_formal_event_pages: list[str] = []
    for path in html_files:
        content = path.read_text(encoding="utf-8")
        matches = list(_CHECKPOINT_BRIDGE_BLOCK_RE.finditer(content))
        emission_count = content.count('data-ghostweb-runtime="checkpoint-emission"')
        if not matches and emission_count == 0:
            if (
                admit_page_authored_formal_events
                and _has_page_authored_formal_checkpoint_event(content)
            ):
                authored_event_blocks = _page_authored_formal_event_blocks(content)
                if not authored_event_blocks:
                    raise RuntimeError(
                        f"page-authored formal-event anchors do not share one script block: {path}"
                    )
                updated = _inject_bridge_only(content, replacement)
                if updated.count('data-ghostweb-runtime="checkpoint-bridge"') != 1:
                    raise RuntimeError(f"page-authored formal-event bridge admission failed: {path}")
                if updated.count('data-ghostweb-runtime="checkpoint-emission"') != 0:
                    raise RuntimeError(f"bridge-only admission synthesized an emission harness: {path}")
                if _page_authored_formal_event_blocks(updated) != authored_event_blocks:
                    raise RuntimeError(
                        f"page-authored formal-event script changed during bridge admission: {path}"
                    )
                if updated.count("data-reference-terminal-observation") != content.count(
                    "data-reference-terminal-observation"
                ):
                    raise RuntimeError(
                        f"reference terminal-observation host changed during bridge admission: {path}"
                    )
                pending.append((path, updated))
                admitted_page_authored_formal_event_pages.append(path.name)
                continue
            skipped_uninstrumented.append(path.name)
            continue
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one checkpoint bridge in {path}, found {len(matches)}"
            )


        updated = _CHECKPOINT_BRIDGE_BLOCK_RE.sub(lambda _match: replacement, content, count=1)
        if updated.count('data-ghostweb-runtime="checkpoint-emission"') != emission_count:
            raise RuntimeError(f"checkpoint emission harness changed while refreshing {path}")
        pending.append((path, updated))
    for path, updated in pending:
        path.write_text(updated, encoding="utf-8")
    return {
        "status": "pass",
        "mode": "bridge_runtime_only",
        "pages_dir": str(pages_dir),
        "page_count": len(html_files),
        "instrumented_page_count": len(pending),
        "skipped_uninstrumented_pages": skipped_uninstrumented,
        "admitted_page_authored_formal_event_pages": (
            admitted_page_authored_formal_event_pages
        ),
        "bindings_recompiled": False,
        "checkpoint_emission_harness_changed": False,
    }


def admit_page_authored_formal_event_pages(result_dir: Path) -> dict[str, Any]:
    pages_dir = _find_pages_dir(result_dir)
    if not pages_dir:
        raise RuntimeError(f"no pages directory found under {result_dir}")
    html_files = sorted(pages_dir.glob("*.html"))
    if not html_files:
        raise RuntimeError(f"no generated HTML pages found under {pages_dir}")

    canonical_blocks: set[str] = set()
    source_page_count = 0
    contents: dict[Path, str] = {}
    for path in html_files:
        content = path.read_text(encoding="utf-8")
        contents[path] = content
        matches = [match.group(0) for match in _CHECKPOINT_BRIDGE_BLOCK_RE.finditer(content)]
        emission_count = content.count('data-ghostweb-runtime="checkpoint-emission"')
        if len(matches) > 1:
            raise RuntimeError(f"expected at most one checkpoint bridge in {path}, found {len(matches)}")
        if emission_count and not matches:
            raise RuntimeError(f"compiled emission harness has no checkpoint bridge: {path}")
        if matches:
            canonical_blocks.add(matches[0])
            source_page_count += 1
    if len(canonical_blocks) != 1:
        raise RuntimeError(
            "page-authored formal-event admission requires one unique compiled bridge; "
            f"found {len(canonical_blocks)} across {source_page_count} pages"
        )
    canonical_bridge = next(iter(canonical_blocks))

    pending: list[tuple[Path, str]] = []
    admitted: list[str] = []
    skipped_uninstrumented: list[str] = []
    for path in html_files:
        content = contents[path]
        if _CHECKPOINT_BRIDGE_BLOCK_RE.search(content):
            continue
        emission_count = content.count('data-ghostweb-runtime="checkpoint-emission"')
        if emission_count:
            raise RuntimeError(f"compiled emission harness has no checkpoint bridge: {path}")
        if not _has_page_authored_formal_checkpoint_event(content):
            skipped_uninstrumented.append(path.name)
            continue
        authored_event_blocks = _page_authored_formal_event_blocks(content)
        if not authored_event_blocks:
            raise RuntimeError(
                f"page-authored formal-event anchors do not share one script block: {path}"
            )
        updated = _inject_bridge_only(content, canonical_bridge)
        if _page_authored_formal_event_blocks(updated) != authored_event_blocks:
            raise RuntimeError(
                f"page-authored formal-event script changed during bridge admission: {path}"
            )
        if updated.count("data-reference-terminal-observation") != content.count(
            "data-reference-terminal-observation"
        ):
            raise RuntimeError(
                f"reference terminal-observation host changed during bridge admission: {path}"
            )
        if updated.count('data-ghostweb-runtime="checkpoint-bridge"') != 1:
            raise RuntimeError(f"compiled bridge admission failed: {path}")
        if updated.count('data-ghostweb-runtime="checkpoint-emission"') != 0:
            raise RuntimeError(f"compiled bridge admission synthesized an emission harness: {path}")
        pending.append((path, updated))
        admitted.append(path.name)

    if not admitted:
        raise RuntimeError("no page-authored formal-event page requires bridge admission")
    for path, updated in pending:
        path.write_text(updated, encoding="utf-8")
    return {
        "status": "pass",
        "mode": "admit_page_authored_formal_events_only",
        "pages_dir": str(pages_dir),
        "page_count": len(html_files),
        "compiled_bridge_source_page_count": source_page_count,
        "compiled_bridge_sha256": "sha256:"
        + hashlib.sha256(canonical_bridge.encode("utf-8")).hexdigest(),
        "admitted_page_authored_formal_event_pages": admitted,
        "skipped_uninstrumented_pages": skipped_uninstrumented,
        "existing_instrumented_pages_changed": False,
        "bindings_recompiled": False,
        "checkpoint_emission_harness_changed": False,
    }


def _build_action_to_checkpoint_map(
    bindings: list[dict[str, Any]],
    cross_page_state_capture_keys: set[tuple[str, str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for b in bindings:
        cp_id = b.get("checkpoint_id", "")
        trace_id_b = b.get("trace_id", "")
        required_payload = b.get("payload_required", {})
        if not isinstance(required_payload, dict):
            required_payload = {}
        action_ids = list(b.get("source_action_ids", []) or [])
        action_ids.extend(
            alias
            for alias in (b.get("action_aliases", []) or [])
            if alias not in action_ids
        )
        for action_id in action_ids:
            action_metadata = b.get("source_action_metadata") or {}
            if not isinstance(action_metadata, dict):
                action_metadata = {}
            source_action_metadata = action_metadata.get(action_id) or {}
            if not isinstance(source_action_metadata, dict):
                source_action_metadata = {}
            entry: dict[str, Any] = {
                "trace_id": trace_id_b,
                "checkpoint_id": cp_id,
                "checkpoint_kind": b.get("checkpoint_kind", ""),
                "depends_on": b.get("depends_on", []),
                "binding_trace_id": trace_id_b,
                "source_action_ids": list(b.get("source_action_ids", []) or []),
                "action_aliases": list(b.get("action_aliases", []) or []),
                "source_steps": b.get("source_steps", []),
                "event": b.get("event", ""),
                "payload_fields": b.get("payload_fields", []),


                "effect_kind": str(
                    required_payload.get("effect_kind")
                    or source_action_metadata.get("effect_kind")
                    or ""
                ),
                "navigation_target_filename": str(
                    required_payload.get("target_filename")
                    or source_action_metadata.get("target_filename")
                    or ""
                ),
                "has_terminal_observable_dom_bindings": bool(
                    b.get("terminal_observable_dom_bindings")
                ),
                "external_observation_producer": str(
                    (b.get("runtime_interface") or {}).get(
                        "observation_producer", ""
                    )
                ),
                "cross_page_state_capture": (
                    str(trace_id_b or ""), str(cp_id or "")
                ) in (cross_page_state_capture_keys or set()),
            }
            terminal_page = str(b.get("terminal_observable_page_filename") or "").strip()
            if terminal_page:
                entry["terminal_observable_page_filename"] = terminal_page
            mapping.setdefault(action_id, []).append(entry)
    return mapping


def _page_checkpoint_map(
    obligations: list[dict[str, Any]],
    action_map: dict[str, list[dict[str, Any]]],
    page_name: str = "",
    action_filename_map: dict[str, str] | None = None,
    action_metadata_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}

    def add_entries(
        action_id: str,
        trace_id: str,
        infos: list[dict[str, Any]],
        obl: dict[str, Any] | None = None,
    ) -> None:
        if not infos:
            return
        metadata = (action_metadata_map or {}).get(action_id) or {}
        entries = result.setdefault(action_id, [])
        seen = {entry["checkpoint_id"] for entry in entries}
        for info in infos:
            if info["checkpoint_id"] in seen:
                continue
            entry: dict[str, Any] = {
                "trace_id": trace_id,
                "checkpoint_id": info["checkpoint_id"],
                "operation": (obl or {}).get("operation") or metadata.get("operation") or "",
                "operation_intent": (
                    (obl or {}).get("operation_intent")
                    or metadata.get("operation_intent")
                    or ""
                ),
                "action_id": action_id,
                "checkpoint_kind": info.get("checkpoint_kind", ""),
                "depends_on": info.get("depends_on", []),
                "source_action_ids": list(info.get("source_action_ids", []) or []),
                "action_aliases": list(info.get("action_aliases", []) or []),
                "source_steps": info.get("source_steps", []),
                "event": info.get("event", ""),
                "payload_fields": info.get("payload_fields", []),
                "effect_kind": info.get("effect_kind", ""),
                "navigation_target_filename": info.get(
                    "navigation_target_filename", ""
                ),
                "cross_page_state_capture": bool(
                    info.get("cross_page_state_capture")
                ),
                "has_terminal_observable_dom_bindings": bool(
                    info.get("has_terminal_observable_dom_bindings")
                ),
                "external_observation_producer": str(
                    info.get("external_observation_producer") or ""
                ),
            }
            terminal_page = str(info.get("terminal_observable_page_filename") or "").strip()
            if terminal_page:
                entry["terminal_observable_page_filename"] = terminal_page
            entries.append(entry)
            seen.add(info["checkpoint_id"])

    for obl in obligations:
        trace_id = obl.get("trace_id", "")
        step_idx = obl.get("step_index")
        if step_idx is None:
            continue
        action_id = f"{trace_id}:step{step_idx}"


        if (
            page_name
            and action_filename_map
            and action_id in action_filename_map
            and action_filename_map[action_id] != page_name
        ):
            continue
        infos = action_map.get(action_id) or []
        add_entries(action_id, trace_id, infos, obl)
        for info in infos:
            for alias in info.get("action_aliases", []) or []:
                add_entries(str(alias), trace_id, [info], obl)

    if page_name and action_filename_map:
        for action_id, infos in action_map.items():
            if action_filename_map.get(action_id) != page_name:
                continue
            trace_id = infos[0].get("trace_id", "") if infos else ""
            add_entries(action_id, trace_id, infos)
            for info in infos:
                for alias in info.get("action_aliases", []) or []:
                    add_entries(str(alias), trace_id, [info])
    return result


def _terminal_outcomes_for_page(
    action_map: dict[str, list[dict[str, Any]]],
    page_name: str,
    action_metadata_map: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for action_id, infos in action_map.items():
        metadata = (action_metadata_map or {}).get(action_id) or {}
        for info in infos:
            if info.get("checkpoint_kind") != "outcome":
                continue
            target_page = str(info.get("terminal_observable_page_filename") or "").strip()
            if target_page != page_name:
                continue
            key = (
                str(info.get("trace_id") or ""),
                action_id,
                str(info.get("checkpoint_id") or ""),
            )
            if key in seen:
                continue
            entry = dict(info)
            entry["action_id"] = action_id
            entry["operation"] = metadata.get("operation") or ""
            entry["operation_intent"] = metadata.get("operation_intent") or ""
            outcomes.append(entry)
            seen.add(key)
    return outcomes


def _cross_page_checkpoints_for_page(
    action_map: dict[str, list[dict[str, Any]]],
    page_name: str,
    action_metadata_map: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for action_id, infos in action_map.items():
        metadata = (action_metadata_map or {}).get(action_id) or {}
        for info in infos:
            if not info.get("cross_page_state_capture"):
                continue
            target_page = str(info.get("navigation_target_filename") or "").strip()
            if target_page != page_name:
                continue
            key = (
                str(info.get("trace_id") or ""),
                action_id,
                str(info.get("checkpoint_id") or ""),
            )
            if key in seen:
                continue
            entry = dict(info)
            entry["action_id"] = action_id
            entry["operation"] = metadata.get("operation") or ""
            entry["operation_intent"] = metadata.get("operation_intent") or ""
            checkpoints.append(entry)
            seen.add(key)
    return checkpoints


def _cross_page_state_capture_keys(contract_root: Path) -> set[tuple[str, str]]:
    plan_path = next(
        (
            candidate
            for candidate in (
                contract_root / "evaluators" / "browser_evaluator_plan.json",
                contract_root.parent / "evaluators" / "browser_evaluator_plan.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    if plan_path is None:
        return set()
    try:
        evaluators = json.loads(plan_path.read_text(encoding="utf-8")).get("evaluators", {})
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(evaluators, dict):
        return set()
    keys: set[tuple[str, str]] = set()
    for trace_id, evaluator in evaluators.items():
        if not isinstance(trace_id, str) or not isinstance(evaluator, dict):
            continue
        contract = evaluator.get("runtime_event_contract")
        if not isinstance(contract, dict):
            continue
        bindings = contract.get("checkpoint_observation_bindings")
        if not isinstance(bindings, dict):
            continue
        for checkpoint_id, rows in bindings.items():
            if not isinstance(checkpoint_id, str) or not isinstance(rows, list):
                continue
            if any(
                isinstance(row, dict)
                and (
                    str(row.get("state_pointer") or "") == "/app_owned_capture"
                    or str(row.get("state_pointer") or "").startswith(
                        "/app_owned_capture/"
                    )
                )
                for row in rows
            ):
                keys.add((trace_id, checkpoint_id))
    return keys


def _find_bindings_path(result_dir: Path) -> Path | None:
    candidates = [
        result_dir / "phase_post_processing" / "instrumentation" / "runtime_event_bindings.json",
        result_dir / "instrumentation" / "runtime_event_bindings.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _find_action_host_observation_bindings_path(result_dir: Path) -> Path | None:
    candidates = [
        result_dir
        / "phase_post_processing"
        / "instrumentation"
        / "action_host_observation_bindings.json",
        result_dir / "instrumentation" / "action_host_observation_bindings.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


_ACTION_HOST_OBSERVATION_BINDING_FIELDS = {
    "action_id",
    "observation_checkpoint_id",
    "source_filename",
    "selector",
    "event_name",
    "event_reason",
    "source_action_authority",
}


def _load_action_host_observation_bindings(
    result_dir: Path,
    *,
    authority_root: Path,
    runtime_bindings: list[dict[str, Any]],
    trace_filter: str | None = None,
) -> list[dict[str, str]]:
    path = _find_action_host_observation_bindings_path(result_dir)
    if path is None:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid action-host observation bindings: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("bindings"), list):
        raise RuntimeError(f"action-host observation bindings has no bindings list: {path}")

    authority_path = next(
        (
            candidate
            for candidate in (
                authority_root
                / "phase_post_processing/action_authority/action_execution_contracts.json",
                authority_root / "action_authority/action_execution_contracts.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    if authority_path is None:
        raise RuntimeError(
            "action-host observation bindings require current action execution authority under "
            f"{authority_root}"
        )
    try:
        authority_payload = json.loads(authority_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid action execution authority: {authority_path}: {exc}") from exc
    actions = {
        str(row.get("action_id") or "").strip(): row
        for row in _action_contract_rows(authority_payload)
        if str(row.get("action_id") or "").strip()
    }
    runtime_events_by_action: dict[str, set[str]] = {}
    runtime_checkpoints_by_action: dict[str, set[str]] = {}
    for runtime_binding in runtime_bindings:
        if not isinstance(runtime_binding, dict):
            continue
        event_name = str(runtime_binding.get("event") or "").strip()
        if not event_name:
            continue
        for action_id in runtime_binding.get("source_action_ids") or []:
            runtime_events_by_action.setdefault(str(action_id), set()).add(event_name)
            checkpoint_id = str(runtime_binding.get("checkpoint_id") or "").strip()
            if checkpoint_id:
                runtime_checkpoints_by_action.setdefault(str(action_id), set()).add(checkpoint_id)

    normalized: list[dict[str, str]] = []
    seen_action_ids: set[str] = set()
    for index, row in enumerate(payload["bindings"]):
        if not isinstance(row, dict):
            raise RuntimeError(f"action-host observation binding {index} is not an object: {path}")
        unexpected = set(row) - _ACTION_HOST_OBSERVATION_BINDING_FIELDS
        if unexpected:
            raise RuntimeError(
                f"action-host observation binding {index} contains non-observation fields "
                f"{sorted(unexpected)!r}: {path}"
            )
        missing = _ACTION_HOST_OBSERVATION_BINDING_FIELDS - set(row)
        if missing:
            raise RuntimeError(
                f"action-host observation binding {index} is missing {sorted(missing)!r}: {path}"
            )
        binding = {key: str(row[key] or "").strip() for key in _ACTION_HOST_OBSERVATION_BINDING_FIELDS}
        if not all(binding.values()):
            raise RuntimeError(f"action-host observation binding {index} contains an empty field: {path}")
        action_id = binding["action_id"]
        action = actions.get(action_id)
        if action is None:
            raise RuntimeError(
                f"action-host observation binding references unknown formal action {action_id!r}: {path}"
            )
        if trace_filter and str(action.get("trace_id") or "") != trace_filter:
            continue
        source_filename = _canonical_page_filename(action.get("source_page"))
        if binding["source_filename"] != source_filename:
            raise RuntimeError(
                f"action-host source page mismatch for {action_id!r}: "
                f"{binding['source_filename']!r} != {source_filename!r}"
            )
        if binding["event_name"] not in runtime_events_by_action.get(action_id, set()):
            raise RuntimeError(
                f"action-host event {binding['event_name']!r} is not consumed by a current "
                f"runtime binding for {action_id!r}"
            )
        if binding["observation_checkpoint_id"] not in runtime_checkpoints_by_action.get(action_id, set()):
            raise RuntimeError(
                f"action-host observation checkpoint {binding['observation_checkpoint_id']!r} "
                f"is not consumed by a current runtime binding for {action_id!r}"
            )
        expected_authority = str(authority_path.relative_to(authority_root))
        if binding["source_action_authority"] != expected_authority:
            raise RuntimeError(
                f"action-host authority mismatch for {action_id!r}: "
                f"{binding['source_action_authority']!r} != {expected_authority!r}"
            )
        if action_id in seen_action_ids:
            raise RuntimeError(f"duplicate action-host observation binding for {action_id!r}: {path}")
        seen_action_ids.add(action_id)
        normalized.append(binding)
    return normalized


def _find_observation_bindings_path(result_dir: Path) -> Path | None:
    candidates = [
        result_dir / "phase_consolidation" / "site_model" / "materialized_observation_bindings.json",
        result_dir / "site_model" / "materialized_observation_bindings.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _find_capture_actions_path(result_dir: Path) -> Path | None:
    candidates = [
        result_dir / "phase_global_reasoning" / "debug" / "capture_actions.json",
        result_dir / "debug" / "capture_actions.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_action_metadata_map(
    result_dir: Path,
    trace_filter: str | None = None,
) -> dict[str, dict[str, Any]]:
    path = _find_capture_actions_path(result_dir)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    mapping: dict[str, dict[str, Any]] = {}
    for trace_id, rows in data.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            step_index = row.get("step_index")
            if step_index is None:
                continue
            action_id = f"{trace_id}:step{step_index}"
            if trace_filter and not action_id.startswith(f"{trace_filter}:"):
                continue
            operation = str(row.get("action") or row.get("operation") or "").strip()
            operation_intent = str(
                row.get("description") or row.get("operation_intent") or ""
            ).strip()
            mapping[action_id] = {
                "operation": operation,
                "operation_intent": operation_intent,
            }
    return mapping


def _load_page_boundary_aliases(result_dir: Path) -> dict[str, set[str]]:
    candidates = [
        result_dir / "phase_post_processing/action_authority/page_boundary_authority.json",
        result_dir / "environment_closure/page_boundary_authority.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid page-boundary authority packet: {path}: {exc}") from exc
        aliases: dict[str, set[str]] = {}
        for row in payload.get("rows", []) if isinstance(payload, dict) else []:
            if not isinstance(row, dict):
                continue
            action_id = str(row.get("action_id") or "").strip()
            pages = row.get("non_authoritative_alias_pages") or []
            if action_id and isinstance(pages, list):
                aliases[action_id] = {str(page).strip() for page in pages if str(page).strip()}
        return aliases
    return {}


def _load_action_filename_map(
    result_dir: Path,
    trace_filter: str | None = None,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    approved_aliases = _load_page_boundary_aliases(result_dir)

    def record(action_id: str, filename: str, authority: str) -> None:
        action_id = str(action_id or "").strip()
        filename = _canonical_page_filename(filename)
        if not action_id or not filename:
            return
        if trace_filter and not action_id.startswith(f"{trace_filter}:"):
            return
        existing = mapping.get(action_id)
        if existing and existing != filename:
            if filename in approved_aliases.get(action_id, set()):


                return
            raise RuntimeError(
                f"conflicting source-page authority for {action_id}: "
                f"{existing!r} vs {filename!r} from {authority}"
            )
        mapping[action_id] = filename


    action_contract_candidates = [
        result_dir / "phase_post_processing/action_authority/action_execution_contracts.json",
        result_dir / "action_authority/action_execution_contracts.json",
    ]
    for action_contract_path in action_contract_candidates:
        if not action_contract_path.is_file():
            continue
        try:
            action_contract_data = json.loads(action_contract_path.read_text(encoding="utf-8"))
        except Exception:
            action_contract_data = {}
        rows = (
            action_contract_data.get("action_execution_contracts")
            or action_contract_data.get("actions")
            or []
        ) if isinstance(action_contract_data, dict) else []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                record(
                    row.get("action_id"),
                    row.get("source_page") or row.get("source_filename"),
                    str(action_contract_path),
                )
        break

    path = _find_observation_bindings_path(result_dir)
    if path:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        bindings = data.get("bindings_by_obs_key", data) if isinstance(data, dict) else {}
        if isinstance(bindings, dict):
            for obs_key, row in bindings.items():
                if not isinstance(row, dict):
                    continue
                filename = str(row.get("filename") or "").strip()
                if filename:
                    key = str(obs_key)
                    record(key, filename, str(path))
                    if ":step" not in key:
                        prefix, sep, step = key.rpartition(":")
                        if sep and step.isdigit():
                            record(f"{prefix}:step{step}", filename, str(path))
    host_manifest = result_dir / "phase_pagegen" / "action_host_manifest.json"
    if host_manifest.is_file():
        try:
            host_data = json.loads(host_manifest.read_text(encoding="utf-8"))
        except Exception:
            host_data = {}
        hosts = host_data.get("hosts") if isinstance(host_data, dict) else []
        if isinstance(hosts, list):
            for host in hosts:
                if not isinstance(host, dict):
                    continue
                action_id = str(host.get("action_id") or "").strip()
                filename = str(host.get("filename") or host.get("page") or "").strip()
                if action_id and filename:
                    record(action_id, filename, str(host_manifest))
    return mapping


def _canonical_page_filename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    return text.rsplit("/", 1)[-1].strip()


def _action_contract_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = (
        payload.get("action_execution_contracts")
        or payload.get("actions")
        or list((payload.get("actions_by_id") or {}).values())
    )
    if not isinstance(candidates, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        if not isinstance(row, dict):
            continue
        action_id = str(row.get("action_id") or "").strip()
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        rows.append(row)
    return rows


def _materialize_missing_binding_source_actions(
    bindings: list[dict[str, Any]],
    result_dir: Path,
) -> int:
    contract_path = next(
        (
            candidate
            for candidate in (
                result_dir / "phase_post_processing/action_authority/action_execution_contracts.json",
                result_dir / "action_authority/action_execution_contracts.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    if contract_path is None:
        return 0
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid action execution authority: {contract_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid action execution authority payload: {contract_path}")

    actions_by_checkpoint: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for action in _action_contract_rows(payload):
        trace_id = str(action.get("trace_id") or "").strip()
        action_id = str(action.get("action_id") or "").strip()
        checkpoint_ids = action.get("checkpoint_ids") or []
        if not trace_id or not action_id or not isinstance(checkpoint_ids, list):
            continue
        for checkpoint_id in checkpoint_ids:
            checkpoint_id = str(checkpoint_id or "").strip()
            if checkpoint_id:
                actions_by_checkpoint.setdefault((trace_id, checkpoint_id), []).append(action)

    materialized = 0
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        trace_id = str(binding.get("trace_id") or "").strip()
        checkpoint_ids = [
            str(binding.get("checkpoint_id") or "").strip(),
            str(binding.get("parent_checkpoint_id") or "").strip(),
        ]
        candidates: list[dict[str, Any]] = []
        seen_actions: set[str] = set()
        for checkpoint_id in checkpoint_ids:
            for action in actions_by_checkpoint.get((trace_id, checkpoint_id), []):
                action_id = str(action.get("action_id") or "").strip()
                if action_id and action_id not in seen_actions:
                    seen_actions.add(action_id)
                    candidates.append(action)
        if not candidates:
            continue
        candidate_action_ids = [str(action["action_id"]) for action in candidates]
        changed = False
        if not binding.get("source_action_ids"):
            binding["source_action_ids"] = candidate_action_ids
            changed = True
        if not binding.get("source_steps"):
            binding["source_steps"] = [
                action["step_index"]
                for action in candidates
                if isinstance(action.get("step_index"), int)
            ]
            changed = True
        source_action_metadata = {
            str(action["action_id"]): {
                "source_filename": _canonical_page_filename(action.get("source_page")),
                "target_filename": _canonical_page_filename(action.get("target_page")),
                "effect_kind": str(action.get("effect_kind") or ""),
            }
            for action in candidates
        }
        if binding.get("source_action_metadata") != source_action_metadata:
            binding["source_action_metadata"] = source_action_metadata
            changed = True
        if binding.get("source_action_authority") != str(contract_path.relative_to(result_dir)):
            binding["source_action_authority"] = str(contract_path.relative_to(result_dir))
            changed = True
        if changed:
            materialized += 1
    return materialized


_GESTURE_MANIFEST_EVENT_TYPES = {
    "drag",
    "gesture",
    "scroll",
    "swipe",
    "touch",
    "wheel",
}
_HTML_OPEN_TAG_RE = re.compile(
    r"<(?!/|!|\?)(?P<tag>[A-Za-z][\w:-]*)(?P<attrs>[^<>]*)>",
    flags=re.DOTALL,
)


def _load_manifest_gesture_hosts(
    result_dir: Path,
    *,
    action_ids: set[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    manifest_path = result_dir / "phase_pagegen" / "action_host_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid action-host manifest: {manifest_path}: {exc}") from exc
    rows = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"action-host manifest has no hosts list: {manifest_path}")

    by_page: dict[str, list[dict[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        event_type = str(row.get("event_type") or "").strip().lower()
        event_types = {
            str(value).strip().lower()
            for value in (row.get("event_types") or [])
            if str(value).strip()
        }
        explicitly_gesture_owned = (
            event_type in _GESTURE_MANIFEST_EVENT_TYPES
            or bool(event_types & {"pointermove", "pointerup", "touchend", "wheel"})
        )
        if not explicitly_gesture_owned:
            continue
        action_id = str(row.get("action_id") or "").strip()
        page = str(row.get("filename") or row.get("page") or "").strip()
        if not action_id or not page:
            raise RuntimeError(
                "gesture action-host manifest row requires action_id and filename/page: "
                f"{row!r}"
            )


        if action_ids is not None and action_id not in action_ids:
            continue
        key = (page, action_id)
        if key in seen:
            continue
        seen.add(key)
        by_page.setdefault(page, []).append(
            {
                "action_id": action_id,
                "gesture_action": event_type if event_type in _GESTURE_MANIFEST_EVENT_TYPES else "gesture",
                "selector": str(row.get("selector") or row.get("host_selector") or "").strip(),
            }
        )
    return by_page


def _project_manifest_gesture_authority(
    html: str,
    page_name: str,
    gesture_hosts: list[dict[str, str]],
) -> tuple[str, int]:
    projected = 0
    for host in gesture_hosts:
        action_id = host["action_id"]
        candidates: list[re.Match[str]] = []
        selector = str(host.get("selector") or "").strip()
        for match in _HTML_OPEN_TAG_RE.finditer(html):
            attrs = match.group("attrs")
            if selector.startswith("#"):
                selector_id = re.escape(selector[1:])
                if not re.search(rf'\bid\s*=\s*(["\']){selector_id}\1', attrs):
                    continue
            elif selector.startswith("[data-action-id="):
                selector_action = re.search(r'\[data-action-id=["\']([^"\']+)["\']\]', selector)
                if selector_action and not re.search(
                    rf'\bdata-action-id\s*=\s*(["\']){re.escape(selector_action.group(1))}\1', attrs
                ):
                    continue
            action_match = re.search(
                r'\bdata-action-id\s*=\s*(["\'])(.*?)\1',
                attrs,
                flags=re.DOTALL,
            )
            alias_match = re.search(
                r'\bdata-action-aliases\s*=\s*(["\'])(.*?)\1',
                attrs,
                flags=re.DOTALL,
            )
            exact_action = bool(action_match and action_match.group(2).strip() == action_id)
            alias_action = bool(alias_match and action_id in alias_match.group(2).split())
            if exact_action or alias_action:
                candidates.append(match)
        if len(candidates) != 1:
            raise RuntimeError(
                f"manifest gesture host {action_id!r} on {page_name!r} resolved to "
                f"{len(candidates)} DOM hosts; expected exactly one"
            )
        match = candidates[0]
        attrs = match.group("attrs")
        if re.search(r'\bdata-(?:ghostweb-gesture-action|gesture-host)\s*=', attrs):
            continue
        tag_text = match.group(0)
        insertion_at = tag_text.rfind("/>")
        if insertion_at < 0:
            insertion_at = tag_text.rfind(">")
        gesture_action = host["gesture_action"]
        rewritten = (
            tag_text[:insertion_at]
            + f' data-gesture-host="action_host_manifest" data-ghostweb-gesture-action="{gesture_action}"'
            + tag_text[insertion_at:]
        )
        html = html[: match.start()] + rewritten + html[match.end() :]
        projected += 1
    return html, projected


def _find_pages_dir(result_dir: Path) -> Path | None:
    candidates = [
        result_dir / "phase_pagegen" / "pages",
        result_dir / "frontend" / "pages",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


_RUNTIME_BRIDGE_MARKER_RE = re.compile(
    r"\n?/\* Ghostweb runtime bridge injection summary: .*? \*/\n?",
    flags=re.DOTALL,
)


def _refresh_instrumented_runtime_marker(
    result_dir: Path,
    *,
    bindings_path: Path,
    summary: dict[str, Any],
) -> bool:
    runtime_path = result_dir / "phase_sdk_generation" / "runtime" / "instrumented_business_logic.js"
    if not runtime_path.is_file():
        return False
    marker = {
        "runtime_bridge_status": "runtime_bridge_injected",
        "runtime_event_bindings": str(bindings_path.relative_to(result_dir)),
        "pages_injected": summary.get("pages_injected", 0),
        "pages_wired": summary.get("pages_wired", 0),
        "checkpoints_wired": summary.get("checkpoints_wired", 0),
    }
    existing = runtime_path.read_text(encoding="utf-8")
    stripped = _RUNTIME_BRIDGE_MARKER_RE.sub("\n", existing).rstrip()
    runtime_path.write_text(
        stripped
        + "\n\n"
        + "/* Ghostweb runtime bridge injection summary: "
        + json.dumps(marker, ensure_ascii=False, sort_keys=True)
        + " */\n",
        encoding="utf-8",
    )
    return True


def run(
    result_dir: Path,
    trace_id: str | None = None,
    *,
    contract_source_dir: Path | None = None,
    only_trace_id: str | None = None,
    only_checkpoint_id: str | None = None,
) -> dict[str, Any]:
    pages_dir = _find_pages_dir(result_dir)
    if not pages_dir:
        print(f"[ERROR] No pages/ directory found under {result_dir}", file=sys.stderr)
        sys.exit(1)

    bindings_path = _find_bindings_path(result_dir)
    if not bindings_path:
        print(f"[ERROR] runtime_event_bindings.json not found under {result_dir}", file=sys.stderr)
        sys.exit(1)

    bindings_data = json.loads(bindings_path.read_text(encoding="utf-8"))
    bindings_list = bindings_data.get("bindings", [])
    if not isinstance(bindings_list, list):
        raise RuntimeError(f"runtime event bindings has no bindings list: {bindings_path}")
    materialized_binding_count = _materialize_missing_binding_source_actions(
        bindings_list,
        contract_source_dir or result_dir,
    )
    if only_checkpoint_id and not only_trace_id:
        raise ValueError("only_checkpoint_id requires only_trace_id")
    selected_bindings = [
        binding
        for binding in bindings_list
        if (
            not only_trace_id
            or (
                str(binding.get("trace_id") or "") == only_trace_id
                and (
                    not only_checkpoint_id
                    or str(binding.get("checkpoint_id") or "") == only_checkpoint_id
                )
            )
        )
    ]
    if only_trace_id and not selected_bindings:
        raise RuntimeError(
            f"no runtime bindings selected for trace={only_trace_id!r} "
            f"checkpoint={only_checkpoint_id!r}"
        )


    contract_root = contract_source_dir or result_dir
    bridge_bindings = bindings_list if only_trace_id else selected_bindings
    cross_page_state_capture_keys = _cross_page_state_capture_keys(contract_root)
    action_map = _build_action_to_checkpoint_map(
        bridge_bindings,
        cross_page_state_capture_keys,
    )
    action_host_observation_bindings = _load_action_host_observation_bindings(
        result_dir,
        authority_root=contract_root,
        runtime_bindings=bindings_list,
        trace_filter=only_trace_id,
    )


    action_filename_map = _load_action_filename_map(contract_root, only_trace_id)
    action_metadata_map = _load_action_metadata_map(contract_root)
    manifest_gesture_hosts = _load_manifest_gesture_hosts(
        result_dir,
        action_ids=set(action_map),
    )

    html_files = sorted(pages_dir.glob("*.html"))
    print(f"[INFO] Found {len(html_files)} HTML pages in {pages_dir}")
    print(
        f"[INFO] Found {len(bindings_list)} bindings in {bindings_path.name}; "
        f"selected {len(selected_bindings)}"
    )
    print(f"[INFO] Action→checkpoint map has {len(action_map)} entries")

    injected_pages: list[str] = []
    injected_bindings: set[tuple[str, str]] = set()
    summary: dict[str, Any] = {
        "pages_scanned": 0,
        "pages_injected": 0,
        "pages_wired": 0,
        "bridge_only_pages": 0,
        "checkpoints_wired": 0,
        "details": [],
        "only_trace_id": only_trace_id or "",
        "only_checkpoint_id": only_checkpoint_id or "",
        "bindings_materialized_from_action_authority": materialized_binding_count,
        "action_host_observation_bindings": len(action_host_observation_bindings),
        "cross_page_state_capture_contract_keys": len(cross_page_state_capture_keys),
    }
    pending_page_writes: list[tuple[Path, str]] = []

    for html_path in html_files:
        html = html_path.read_text(encoding="utf-8")
        obligations = _extract_obligations(html)
        cp_map = _page_checkpoint_map(
            obligations,
            action_map,
            page_name=html_path.name,
            action_filename_map=action_filename_map,
            action_metadata_map=action_metadata_map,
        )
        terminal_outcomes_on_load = _terminal_outcomes_for_page(
            action_map,
            html_path.name,
            action_metadata_map,
        )
        cross_page_checkpoints_on_load = _cross_page_checkpoints_for_page(
            action_map,
            html_path.name,
            action_metadata_map,
        )
        if (
            only_trace_id
            and not cp_map
            and not terminal_outcomes_on_load
            and not cross_page_checkpoints_on_load
        ):


            continue
        summary["pages_scanned"] += 1
        html, gesture_hosts_projected = _project_manifest_gesture_authority(
            html,
            html_path.name,
            manifest_gesture_hosts.get(html_path.name, []),
        )


        page_trace_id = (
            trace_id
            or only_trace_id
            or (obligations[0].get("trace_id", "") if obligations else "")
        )

        harness_js = _build_emission_harness_js(
            cp_map,
            page_trace_id,
            obligations,
            terminal_outcomes_on_load,
            cross_page_checkpoints_on_load,
            action_host_observation_bindings,
        )
        new_html = _inject_bridge_scripts(html, BRIDGE_JS, harness_js)
        pending_page_writes.append((html_path, new_html))

        page_name = html_path.name
        injected_pages.append(page_name)
        for step_infos in cp_map.values():
            for step_info in step_infos:
                injected_bindings.add((step_info.get("trace_id", ""), step_info["checkpoint_id"]))
        for outcome in terminal_outcomes_on_load:
            injected_bindings.add((outcome.get("trace_id", ""), outcome.get("checkpoint_id", "")))
        for checkpoint in cross_page_checkpoints_on_load:
            injected_bindings.add(
                (checkpoint.get("trace_id", ""), checkpoint.get("checkpoint_id", ""))
            )

        checkpoint_ids = [
            step_info["checkpoint_id"]
            for step_infos in cp_map.values()
            for step_info in step_infos
        ]
        checkpoint_keys = [
            [step_info.get("trace_id", ""), step_info["checkpoint_id"]]
            for step_infos in cp_map.values()
            for step_info in step_infos
        ]
        for outcome in terminal_outcomes_on_load:
            checkpoint_id = str(outcome.get("checkpoint_id") or "")
            checkpoint_key = [str(outcome.get("trace_id") or ""), checkpoint_id]
            if checkpoint_id and checkpoint_id not in checkpoint_ids:
                checkpoint_ids.append(checkpoint_id)
            if checkpoint_id and checkpoint_key not in checkpoint_keys:
                checkpoint_keys.append(checkpoint_key)
        for checkpoint in cross_page_checkpoints_on_load:
            checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
            checkpoint_key = [str(checkpoint.get("trace_id") or ""), checkpoint_id]
            if checkpoint_id and checkpoint_id not in checkpoint_ids:
                checkpoint_ids.append(checkpoint_id)
            if checkpoint_id and checkpoint_key not in checkpoint_keys:
                checkpoint_keys.append(checkpoint_key)

        summary["details"].append({
            "page": page_name,
            "obligations": len(obligations),
            "bridge_only": not bool(cp_map),
            "checkpoints_wired": len(checkpoint_ids),
            "checkpoint_ids": checkpoint_ids,
            "checkpoint_keys": checkpoint_keys,
            "terminal_outcomes_on_load": len(terminal_outcomes_on_load),
            "cross_page_checkpoints_on_load": len(cross_page_checkpoints_on_load),
            "gesture_hosts_projected": gesture_hosts_projected,
        })

    summary["pages_injected"] = len(injected_pages)
    summary["pages_wired"] = sum(1 for d in summary["details"] if d["checkpoints_wired"] > 0)
    summary["bridge_only_pages"] = summary["pages_injected"] - summary["pages_wired"]
    summary["checkpoints_wired"] = len(injected_bindings)
    selected_binding_keys = {
        (str(binding.get("trace_id") or ""), str(binding.get("checkpoint_id") or ""))
        for binding in selected_bindings
    }
    if only_trace_id and not selected_binding_keys <= injected_bindings:
        raise RuntimeError(
            "trace-scoped runtime bridge did not materialize every selected binding: "
            f"missing={sorted(selected_binding_keys - injected_bindings)!r}"
        )
    for html_path, new_html in pending_page_writes:
        html_path.write_text(new_html, encoding="utf-8")


    bindings_data["runtime_bridge_status"] = "runtime_bridge_injected"
    for b in bindings_list:
        binding_key = (b.get("trace_id", ""), b.get("checkpoint_id", ""))
        if only_trace_id and binding_key not in selected_binding_keys:
            continue
        if binding_key in injected_bindings:
            b["binding_status"] = "runtime_bridge_injected"

            bound_pages = []
            for detail in summary["details"]:
                if [b.get("trace_id", ""), b.get("checkpoint_id", "")] in detail.get("checkpoint_keys", []):
                    bound_pages.append(detail["page"])
            if bound_pages:
                b["injected_page"] = bound_pages[0] if len(bound_pages) == 1 else bound_pages
        else:


            b.pop("injected_page", None)
            _mark_state_projection_oracle_binding(b)

    bindings_path.write_text(
        json.dumps(bindings_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary["instrumented_runtime_refreshed"] = False
    if not only_trace_id:
        summary["instrumented_runtime_refreshed"] = _refresh_instrumented_runtime_marker(
            result_dir,
            bindings_path=bindings_path,
            summary=summary,
        )

    print(f"\n[OK] Injected bridge into {summary['pages_injected']} pages")
    print(f"[OK] Wired {summary['checkpoints_wired']} unique checkpoints")
    print(f"[OK] Updated {bindings_path.name}")
    return summary


def _mark_state_projection_oracle_binding(binding: dict[str, Any]) -> None:
    runtime_interface = binding.get("runtime_interface") or {}
    canonical_state_refs = runtime_interface.get("canonical_state_refs") or []
    if binding.get("supervision") != "outcome" or not canonical_state_refs:
        return
    if binding.get("binding_status") == "runtime_bridge_injected":
        return
    binding["binding_status"] = "state_projection_oracle"
    binding["oracle_binding_status"] = "state_projection_oracle"
    binding["oracle_evidence"] = {
        "kind": "canonical_state_projection",
        "canonical_state_refs": canonical_state_refs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inject GhostwebCheckpointRuntime bridge into generated HTML pages.",
    )
    parser.add_argument(
        "--result-dir",
        required=True,
        type=Path,
        help="Path to the result directory containing phase_pagegen/pages/ and runtime_event_bindings.json",
    )
    parser.add_argument(
        "--trace-id",
        default=None,
        help="Override trace ID for init(); defaults to per-page obligation trace_id",
    )
    parser.add_argument(
        "--contract-source-dir",
        type=Path,
        default=None,
        help=(
            "Read materialized observation bindings from this authoritative result dir "
            "when the target package omits phase_consolidation artifacts"
        ),
    )
    parser.add_argument(
        "--only-trace-id",
        default=None,
        help=(
            "Precisely recompile only bindings and pages owned by one trace; "
            "unrelated bindings, pages and the global runtime marker remain unchanged"
        ),
    )
    parser.add_argument(
        "--only-checkpoint-id",
        default=None,
        help="Optional checkpoint filter; requires --only-trace-id",
    )
    parser.add_argument(
        "--bridge-runtime-only",
        action="store_true",
        help=(
            "Refresh the shared checkpoint-bridge block on every page while "
            "preserving compiled checkpoint-emission harnesses and bindings"
        ),
    )
    parser.add_argument(
        "--emission-runtime-from-embedded-contract",
        action="store_true",
        help="Refresh generic emission code from each page's existing compiled contract.",
    )
    parser.add_argument(
        "--admit-page-authored-formal-events",
        action="store_true",
        help=(
            "With --bridge-runtime-only, also add the shared bridge to pages "
            "that already own a formal checkpoint event and a reference "
            "terminal-observation host; never synthesizes an emission harness"
        ),
    )
    parser.add_argument(
        "--admit-page-authored-formal-events-only",
        action="store_true",
        help=(
            "Add the unique bridge already compiled in the package only to "
            "page-authored formal-event surfaces that lack it; leave every "
            "existing instrumented page and binding byte-identical"
        ),
    )
    args = parser.parse_args()

    if not args.result_dir.is_dir():
        print(f"[ERROR] {args.result_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.contract_source_dir is not None and not args.contract_source_dir.is_dir():
        print(f"[ERROR] {args.contract_source_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.bridge_runtime_only:
        if args.emission_runtime_from_embedded_contract:
            parser.error("--bridge-runtime-only cannot be combined with emission refresh")
        if args.admit_page_authored_formal_events_only:
            parser.error(
                "--bridge-runtime-only cannot be combined with "
                "--admit-page-authored-formal-events-only"
            )
        if args.trace_id or args.only_trace_id or args.only_checkpoint_id or args.contract_source_dir:
            parser.error("--bridge-runtime-only cannot be combined with binding filters")
        summary = refresh_bridge_runtime_only(
            args.result_dir,
            admit_page_authored_formal_events=args.admit_page_authored_formal_events,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return

    if args.emission_runtime_from_embedded_contract:
        if (
            args.trace_id or args.only_trace_id or args.only_checkpoint_id
            or args.contract_source_dir or args.admit_page_authored_formal_events
            or args.admit_page_authored_formal_events_only
        ):
            parser.error("emission runtime refresh cannot be combined with binding filters")
        summary = refresh_emission_runtime_from_embedded_contract(args.result_dir)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return

    if args.admit_page_authored_formal_events_only:
        if args.trace_id or args.only_trace_id or args.only_checkpoint_id or args.contract_source_dir:
            parser.error(
                "--admit-page-authored-formal-events-only cannot be combined with binding filters"
            )
        if args.admit_page_authored_formal_events:
            parser.error(
                "--admit-page-authored-formal-events-only cannot be combined with "
                "--admit-page-authored-formal-events"
            )
        summary = admit_page_authored_formal_event_pages(args.result_dir)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return

    if args.admit_page_authored_formal_events:
        parser.error("--admit-page-authored-formal-events requires --bridge-runtime-only")

    summary = run(
        args.result_dir,
        args.trace_id,
        contract_source_dir=args.contract_source_dir,
        only_trace_id=args.only_trace_id,
        only_checkpoint_id=args.only_checkpoint_id,
    )
    for d in summary["details"]:
        print(f"  {d['page']}: {d['checkpoints_wired']} checkpoints → {d['checkpoint_ids']}")


if __name__ == "__main__":
    main()
