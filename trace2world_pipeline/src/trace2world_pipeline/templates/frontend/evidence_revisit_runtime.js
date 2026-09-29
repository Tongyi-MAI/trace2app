(function () {
  'use strict';

  // Generic app-owned evidence revisit carrier.  It deliberately observes
  // only visible route transitions and state already written by the page's
  // own reducers/handlers; it never reads evaluator/checkpoint verdicts and
  // never writes business state on their behalf.
  var KEY = 'sim:evidence_revisit:v1';
  var INFRA_KEYS = {
    'sim:evidence_revisit:v1': true,
    'sim.execution_context': true,
    'sim.task_initial_state': true,
    'sim.current_task_id': true,
    'sim.trainable_trace_id': true,
    'sim.response_mode': true,
    'sim.actionability_preflight': true,
    'sim_runtime_state_v1': true,
    'sim_runtime_events_v1': true,
    '__sim_checkpoint_events_v1__': true,
    '__sim_history_back_events_v1__': true,
    '__sim_history_back_warnings_v1__': true,
    'sim.route.pending_commit': true,
    'sim_route_stack': true,
    'sim_current_page': true,
    'sim_last_back_from': true,
    'sim_pending_commit_navigation': true,
    'sim_state_commits': true
  };

  function parse(raw, fallback) {
    try {
      var value = JSON.parse(raw || '');
      return value && typeof value === 'object' ? value : fallback;
    } catch (_) { return fallback; }
  }

  function context() {
    try {
      return window.__simExecutionContext ||
        parse(window.sessionStorage.getItem('sim.execution_context') ||
          window.localStorage.getItem('sim.execution_context'), {});
    } catch (_) { return {}; }
  }

  function taskId() {
    var ctx = context();
    return String(ctx.task_id || ctx.taskId ||
      window.sessionStorage.getItem('sim.current_task_id') ||
      window.localStorage.getItem('sim.current_task_id') || '').trim();
  }

  function surface() {
    return String(location.pathname || '').split('/').pop() || 'index.html';
  }

  function businessSnapshot() {
    var result = { session: {}, local: {} };
    function collect(store, target) {
      for (var i = 0; i < store.length; i += 1) {
        var key = store.key(i);
        if (!key || INFRA_KEYS[key] || key.indexOf('__') === 0 ||
            key.indexOf('sim.execution') === 0) continue;
        var raw = store.getItem(key);
        if (raw == null || raw === '') continue;
        // Keep the app-owned value itself: this is evidence, not a verdict.
        target[key] = String(raw).slice(0, 4096);
      }
    }
    try { collect(window.sessionStorage, result.session); } catch (_) {}
    try { collect(window.localStorage, result.local); } catch (_) {}
    return result;
  }

  function hasBusinessState(snapshot) {
    return !!snapshot &&
      (Object.keys(snapshot.session || {}).length > 0 ||
       Object.keys(snapshot.local || {}).length > 0);
  }

  function read() {
    try {
      var value = parse(window.sessionStorage.getItem(KEY), null);
      return value && typeof value === 'object' ? value : null;
    } catch (_) { return null; }
  }

  function write(value) {
    try { window.sessionStorage.setItem(KEY, JSON.stringify(value)); } catch (_) {}
    window.SimEvidenceRevisit = value;
    return value;
  }

  function freshState(id, page, snapshot) {
    return {
      format: 'sim.evidence_revisit_state.v1',
      task_id: id,
      episode_id: String(Date.now()) + '-' + Math.random().toString(16).slice(2),
      current_surface: page,
      visited_surfaces: [page],
      transitions: [],
      revisits: [],
      progressive_traversals: [],
      current_snapshot: snapshot,
      evidence_revisit_count: 0,
      progressive_traversal_count: 0,
      evidence_revisit_consistency: false,
      reset_observed: true,
      producer: 'app_owned_visible_route_and_state_observer'
    };
  }

  function update() {
    var id = taskId();
    if (!id) return;
    var page = surface();
    var snapshot = businessSnapshot();
    var state = read();
    if (!state || state.task_id !== id) state = freshState(id, page, snapshot);
    var previous = state.current_surface;
    var previousSnapshot = state.current_snapshot || {};
    if (previous && previous !== page) {
      var seen = Array.isArray(state.visited_surfaces) && state.visited_surfaces.indexOf(page) !== -1;
      var transition = {
        from_surface: previous,
        to_surface: page,
        route_transition: true,
        prior_business_state_observed: hasBusinessState(previousSnapshot),
        current_business_state_observed: hasBusinessState(snapshot),
        state_snapshot_before: previousSnapshot,
        state_snapshot_after: snapshot,
        visible_route: true,
        timestamp: Date.now()
      };
      state.transitions = Array.isArray(state.transitions) ? state.transitions : [];
      state.transitions.push(transition);
      if (seen && transition.prior_business_state_observed && transition.current_business_state_observed) {
        state.revisits = Array.isArray(state.revisits) ? state.revisits : [];
        state.revisits.push({
          from_surface: previous,
          to_surface: page,
          kind: 'evidence_revisit',
          business_state_reads: Object.keys(previousSnapshot.session || {}).concat(Object.keys(previousSnapshot.local || {})),
          visible_route: true,
          state_snapshot_before: previousSnapshot,
          state_snapshot_after: snapshot,
          timestamp: transition.timestamp
        });
      } else if (!seen && transition.prior_business_state_observed) {
        state.progressive_traversals = Array.isArray(state.progressive_traversals) ? state.progressive_traversals : [];
        state.progressive_traversals.push({
          from_surface: previous,
          to_surface: page,
          kind: 'progressive_traversal',
          business_state_reads: Object.keys(previousSnapshot.session || {}).concat(Object.keys(previousSnapshot.local || {})),
          visible_route: true,
          timestamp: transition.timestamp
        });
      }
    }
    state.current_surface = page;
    state.visited_surfaces = Array.isArray(state.visited_surfaces) ? state.visited_surfaces : [];
    if (state.visited_surfaces.indexOf(page) === -1) state.visited_surfaces.push(page);
    state.current_snapshot = snapshot;
    state.evidence_revisit_count = Array.isArray(state.revisits) ? state.revisits.length : 0;
    state.progressive_traversal_count = Array.isArray(state.progressive_traversals) ? state.progressive_traversals.length : 0;
    state.evidence_revisit_consistency = state.evidence_revisit_count >= 2 &&
      state.revisits.every(function (row) {
        return row && row.visible_route && row.state_snapshot_before && row.state_snapshot_after &&
          Object.keys(row.state_snapshot_before.session || {}).length + Object.keys(row.state_snapshot_before.local || {}).length > 0;
      });
    write(state);
  }

  update();
  window.addEventListener('pageshow', update, { once: true });
  window.addEventListener('sim:task-context-update', update);
})();
