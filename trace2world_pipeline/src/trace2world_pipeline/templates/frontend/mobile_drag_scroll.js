(function () {
  if (window.GhostwebMobileDragScroll && window.GhostwebMobileDragScroll.installed) return;

  const STYLE_ID = 'ghostweb-mobile-drag-scroll-style';
  const SCROLLABLE_OVERFLOW = /^(auto|scroll|overlay)$/;
  const DRAG_THRESHOLD_PX = 6;
  const CLICK_SUPPRESS_MS = 350;
  const SNAP_DISABLED_CLASS = 'ghostweb-drag-scroll-snap-disabled';

  function installStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
html, body, .app-shell, .app-shell main {
  touch-action: pan-y;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}
[data-ghostweb-horizontal-scroll], .overflow-x-auto {
  touch-action: pan-x pan-y;
  overscroll-behavior-inline: contain;
}
body, body * {
  -webkit-user-select: none;
  user-select: none;
  -webkit-touch-callout: none;
  -webkit-user-drag: none;
  user-drag: none;
}
input, textarea, select, option, [contenteditable="true"], [contenteditable=true] {
  -webkit-user-select: text;
  user-select: text;
  -webkit-touch-callout: default;
}
body.ghostweb-drag-scrolling, body.ghostweb-drag-scrolling * {
  cursor: grabbing !important;
}
.${SNAP_DISABLED_CLASS} {
  scroll-snap-type: none !important;
  scroll-behavior: auto !important;
}
.${SNAP_DISABLED_CLASS} > * {
  scroll-snap-align: none !important;
}
`;
    (document.head || document.documentElement).appendChild(style);
  }

  function isEditable(el) {
    return !!(el && el.closest && el.closest('input, textarea, select, option, [contenteditable="true"], [contenteditable=true]'));
  }

  function canScroll(el, axis) {
    if (!el || el.nodeType !== 1) return false;
    const style = window.getComputedStyle(el);
    if (axis === 'x') {
      return SCROLLABLE_OVERFLOW.test(style.overflowX) && el.scrollWidth > el.clientWidth + 1;
    }
    return SCROLLABLE_OVERFLOW.test(style.overflowY) && el.scrollHeight > el.clientHeight + 1;
  }

  function nearestScroller(start, axis) {
    let node = start && start.nodeType === 1 ? start : start && start.parentElement;
    while (node && node !== document.documentElement) {
      if (canScroll(node, axis)) return node;
      node = node.parentElement;
    }
    const main = document.querySelector('.app-shell main');
    if (canScroll(main, axis)) return main;
    const scrollingElement = document.scrollingElement || document.documentElement;
    return canScroll(scrollingElement, axis) ? scrollingElement : null;
  }

  function actionIdsFromHost(host) {
    if (!host || !host.getAttribute) return [];
    const values = [
      host.getAttribute('data-action-id') || '',
      host.getAttribute('data-action-aliases') || '',
      host.getAttribute('data-element-id') || '',
      host.getAttribute('data-element-aliases') || ''
    ].join(' ');
    const ids = values.match(/task_[A-Za-z0-9_]+:step\d+/g) || [];
    return Array.from(new Set(ids));
  }

  function executionContext() {
    try {
      return window.__ghostwebExecutionContext ||
        JSON.parse(window.localStorage.getItem('ghostweb.execution_context') || '{}') || {};
    } catch (error) {
      return {};
    }
  }

  function isVisibleGestureHost(host) {
    if (!host || !host.getBoundingClientRect) return false;
    const rect = host.getBoundingClientRect();
    const style = window.getComputedStyle(host);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' && style.visibility !== 'hidden';
  }

  function contextualGestureHost(target, axis) {
    const axisToken = axis === 'x' ? 'horizontal' : axis === 'y' ? 'vertical' : axis;
    const selector = '[data-gesture-host][data-gesture-axis="' + axisToken + '"]';
    const direct = target && target.closest ? target.closest(selector) : null;
    if (direct && isVisibleGestureHost(direct)) return direct;

    const context = executionContext();
    const actionId = context && context.action_id ? String(context.action_id) : '';
    const traceId = context && (context.reference_trace_id || context.task_id)
      ? String(context.reference_trace_id || context.task_id)
      : '';
    const hosts = Array.from(document.querySelectorAll(selector)).filter(isVisibleGestureHost);
    if (actionId) {
      const byAction = hosts.find(function (host) {
        return actionIdsFromHost(host).indexOf(actionId) !== -1;
      });
      if (byAction) return byAction;
    }
    if (traceId) {
      const byTrace = hosts.find(function (host) {
        return actionIdsFromHost(host).some(function (id) {
          return id.indexOf(traceId + ':step') === 0;
        });
      });
      if (byTrace) return byTrace;
    }
    return null;
  }

  function declaredGestureScroller(event, axis) {
    const host = contextualGestureHost(event && event.target, axis);
    if (!host) return null;
    const selectors = axis === 'x'
      ? ['[data-ghostweb-horizontal-scroll]', '.overflow-x-auto']
      : ['[data-ghostweb-vertical-scroll]', '.overflow-y-auto'];
    for (const selector of selectors) {
      if (host.matches && host.matches(selector) && canScroll(host, axis)) return host;
      const descendant = Array.from(host.querySelectorAll(selector)).find(function (node) {
        return canScroll(node, axis);
      });
      if (descendant) return descendant;
    }
    return canScroll(host, axis) ? host : null;
  }

  function declaredWindowStep(scroller, rawDelta) {
    const magnitude = Math.abs(Number(rawDelta) || 0);
    const configured = Number(scroller && scroller.getAttribute
      ? scroller.getAttribute('data-ghostweb-wheel-step')
      : 0);
    if (Number.isFinite(configured) && configured > 0) return Math.max(magnitude, configured);
    const viewportStep = scroller ? Math.min(96, Math.max(32, scroller.clientWidth * 0.25)) : 32;
    return Math.max(magnitude, viewportStep);
  }

  function maxScroll(el, axis) {
    return axis === 'x'
      ? Math.max(0, el.scrollWidth - el.clientWidth)
      : Math.max(0, el.scrollHeight - el.clientHeight);
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function capDragStep(scroller, delta) {
    if (!scroller || !scroller.getAttribute) return delta;
    const configured = Number(scroller.getAttribute('data-ghostweb-max-drag-step'));
    if (!Number.isFinite(configured) || configured <= 0) return delta;
    return clamp(delta, -configured, configured);
  }

  function canMove(el, axis, scrollDelta) {
    if (!el) return false;
    const current = axis === 'x' ? el.scrollLeft : el.scrollTop;
    if (scrollDelta > 0) return current < maxScroll(el, axis);
    if (scrollDelta < 0) return current > 0;
    return false;
  }

  let state = null;
  let suppressClickUntil = 0;

  function disableNativeDrag(root) {
    const scope = root && root.querySelectorAll ? root : document;
    if (scope.matches && scope.matches('img, a, button')) {
      scope.setAttribute('draggable', 'false');
    }
    scope.querySelectorAll('img, a, button').forEach(function (node) {
      node.setAttribute('draggable', 'false');
    });
  }

  function beginState(target, clientX, clientY, pointerId, captureTarget, inputType) {
    state = {
      inputType,
      pointerId,
      captureTarget,
      target,
      lastX: clientX,
      lastY: clientY,
      startX: clientX,
      startY: clientY,
      active: false,
      scrollerX: null,
      scrollerY: null,
      axisX: null,
      axisY: null,
      snapDisabledScrollers: null
    };
  }

  function resetState() {
    if (state && state.captureTarget && state.captureTarget.releasePointerCapture && state.pointerId != null) {
      try { state.captureTarget.releasePointerCapture(state.pointerId); } catch (e) {}
    }
    if (state && state.snapDisabledScrollers) {
      state.snapDisabledScrollers.forEach(function (scroller) {
        scroller.classList && scroller.classList.remove(SNAP_DISABLED_CLASS);
      });
    }
    state = null;
    document.body && document.body.classList.remove('ghostweb-drag-scrolling');
  }

  function getAxisState(axis, scroller) {
    const key = axis === 'x' ? 'axisX' : 'axisY';
    if (!state[key] || state[key].scroller !== scroller) {
      const style = window.getComputedStyle(scroller);
      state[key] = {
        scroller,
        startScrollLeft: scroller.scrollLeft,
        startScrollTop: scroller.scrollTop,
        snapType: style && style.scrollSnapType || 'none',
        lastScrollDelta: 0
      };
    }
    return state[key];
  }

  function disableSnapForDrag(scroller) {
    if (!scroller || !scroller.classList) return;
    scroller.classList.add(SNAP_DISABLED_CLASS);
    state.snapDisabledScrollers = state.snapDisabledScrollers || new Set();
    state.snapDisabledScrollers.add(scroller);
  }

  function settleSnap(axisState, axis) {
    if (!axisState || !axisState.scroller || !axisState.snapType || axisState.snapType === 'none') return;
    const scroller = axisState.scroller;
    const size = axis === 'x' ? scroller.clientWidth : scroller.clientHeight;
    if (!size) return;
    const start = axis === 'x' ? axisState.startScrollLeft : axisState.startScrollTop;
    const max = maxScroll(scroller, axis);
    const delta = axisState.lastScrollDelta || 0;
    const startIndex = Math.round(start / size);
    const current = axis === 'x' ? scroller.scrollLeft : scroller.scrollTop;
    let targetIndex = Math.round(current / size);
    if (Math.abs(delta) >= Math.min(32, size * 0.18)) {
      targetIndex = startIndex + (delta > 0 ? 1 : -1);
    }
    const target = clamp(targetIndex * size, 0, max);
    if (axis === 'x') {
      scroller.scrollLeft = target;
    } else {
      scroller.scrollTop = target;
    }
  }

  function onPointerDown(event) {
    if (event.pointerType === 'mouse') return;
    if (!event.isPrimary || event.button !== 0 || isEditable(event.target)) return;
    installStyle();
    const captureTarget = event.target && event.target.setPointerCapture ? event.target : null;
    if (captureTarget) {
      try { captureTarget.setPointerCapture(event.pointerId); } catch (e) {}
    }
    beginState(event.target, event.clientX, event.clientY, event.pointerId, captureTarget, 'pointer');
  }

  function moveState(event, clientX, clientY) {
    const dx = clientX - state.lastX;
    const dy = clientY - state.lastY;
    const totalX = clientX - state.startX;
    const totalY = clientY - state.startY;
    const axis = Math.abs(totalX) > Math.abs(totalY) ? 'x' : 'y';

    if (!state.active && Math.hypot(totalX, totalY) < DRAG_THRESHOLD_PX) return;
    const scrollerKey = axis === 'x' ? 'scrollerX' : 'scrollerY';
    state[scrollerKey] = state[scrollerKey] || nearestScroller(state.target, axis);
    const scroller = state[scrollerKey];
    const rawScrollDelta = axis === 'x' ? -totalX : -totalY;
    const totalScrollDelta = capDragStep(scroller, rawScrollDelta);
    if (!scroller || !canMove(scroller, axis, totalScrollDelta)) return;

    state.active = true;
    event.preventDefault();
    document.body && document.body.classList.add('ghostweb-drag-scrolling');
    const axisState = getAxisState(axis, scroller);
    axisState.lastScrollDelta = totalScrollDelta;
    disableSnapForDrag(scroller);
    if (axis === 'x') {
      scroller.scrollLeft = clamp(axisState.startScrollLeft + totalScrollDelta, 0, maxScroll(scroller, axis));
    } else {
      scroller.scrollTop = clamp(axisState.startScrollTop + totalScrollDelta, 0, maxScroll(scroller, axis));
    }
    state.lastX = clientX;
    state.lastY = clientY;
  }

  function onPointerMove(event) {
    if (!state || state.inputType !== 'pointer' || event.pointerId !== state.pointerId) return;
    moveState(event, event.clientX, event.clientY);
  }

  function endActiveDrag() {
    if (state.active) {
      settleSnap(state.axisX, 'x');
      settleSnap(state.axisY, 'y');
      suppressClickUntil = Date.now() + CLICK_SUPPRESS_MS;
    }
  }

  function onPointerUp(event) {
    if (!state || state.inputType !== 'pointer' || event.pointerId !== state.pointerId) return;
    endActiveDrag();
    resetState();
  }

  function onPointerCancel(event) {
    if (!state) return;
    if (state.inputType === 'pointer' && event.pointerId === state.pointerId) {
      resetState();
      return;
    }
    if (state.inputType === 'mouse' && event.pointerType === 'mouse') {
      endActiveDrag();
      resetState();
    }
  }

  function onMouseDown(event) {
    if (event.button !== 0 || isEditable(event.target)) return;
    installStyle();
    beginState(event.target, event.clientX, event.clientY, null, null, 'mouse');
  }

  function onMouseMove(event) {
    if (!state || state.inputType !== 'mouse' || !(event.buttons & 1)) return;
    moveState(event, event.clientX, event.clientY);
  }

  function onMouseUp() {
    if (!state || state.inputType !== 'mouse') return;
    endActiveDrag();
    resetState();
  }

  function onClick(event) {
    if (Date.now() > suppressClickUntil) return;
    event.preventDefault();
    event.stopPropagation();
    suppressClickUntil = 0;
  }

  function onWheel(event) {
    if (isEditable(event.target)) return;
    let axis = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? 'x' : 'y';
    let scroller = nearestScroller(event.target, axis);
    let declaredGesture = false;
    const declaredHorizontalScroller = declaredGestureScroller(event, 'x');
    const wheelMagnitude = Math.max(Math.abs(Number(event.deltaX) || 0), Math.abs(Number(event.deltaY) || 0));
    const contractWindowAdvance = declaredHorizontalScroller && wheelMagnitude <= 4;
    if (axis === 'y' && declaredHorizontalScroller && (contractWindowAdvance || !scroller || !canMove(scroller, 'y', event.deltaY))) {
      axis = 'x';
      scroller = declaredHorizontalScroller;
      declaredGesture = true;
    }
    if (!scroller) return;
    const rawDelta = axis === 'x' ? event.deltaX : event.deltaY;
    const sourceDelta = declaredGesture ? event.deltaY : rawDelta;
    let normalizedDelta = sourceDelta * Math.max(1, Number(window.devicePixelRatio) || 1);
    if (declaredGesture) {
      const direction = String(scroller.closest('[data-gesture-direction]')?.getAttribute('data-gesture-direction') || 'forward');
      const sign = direction === 'backward' || direction === 'reverse' ? -1 : 1;
      normalizedDelta = declaredWindowStep(scroller, normalizedDelta) * sign;
    }
    const cappedDelta = capDragStep(scroller, normalizedDelta);
    if (!declaredGesture && cappedDelta === normalizedDelta) return;
    event.preventDefault();
    if (axis === 'x') {
      scroller.scrollLeft = clamp(scroller.scrollLeft + cappedDelta, 0, maxScroll(scroller, axis));
    } else {
      scroller.scrollTop = clamp(scroller.scrollTop + cappedDelta, 0, maxScroll(scroller, axis));
    }
  }

  installStyle();
  disableNativeDrag(document);
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        record.addedNodes && record.addedNodes.forEach(function (node) {
          if (node && node.nodeType === 1) disableNativeDrag(node);
        });
      });
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  window.addEventListener('pointerdown', onPointerDown, { capture: true, passive: true });
  window.addEventListener('pointermove', onPointerMove, { capture: true, passive: false });
  window.addEventListener('pointerup', onPointerUp, { capture: true, passive: true });
  window.addEventListener('pointercancel', onPointerCancel, { capture: true, passive: true });
  window.addEventListener('mousedown', onMouseDown, { capture: true, passive: true });
  window.addEventListener('mousemove', onMouseMove, { capture: true, passive: false });
  window.addEventListener('mouseup', onMouseUp, { capture: true, passive: true });
  window.addEventListener('wheel', onWheel, { capture: true, passive: false });
  window.addEventListener('dragstart', function (event) {
    if (isEditable(event.target)) return;
    event.preventDefault();
    event.stopPropagation();
  }, { capture: true, passive: false });
  window.addEventListener('click', onClick, true);

  window.GhostwebMobileDragScroll = {
    installed: true,
    nearestScroller,
    canScroll
  };
})();
