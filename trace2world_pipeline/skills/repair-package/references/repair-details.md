# Repair Details

Ordered repair chains for the issue classes of `references/repair-ladder.md` §3. Run the whole
chain of the class in one pass. A chain that stops after its first visible improvement is the
one-direction false fix of §7, and the next round pays for it.

Only the package is edited. A chain step that would need a page identity, a state or data relation,
a guard, or an effect that no source evidence supports stops the repair and becomes a `semantics`
referral.

## `structural`

Symptom: a back or close control is not clickable, a link is dead, a surface does nothing, or the
page scrolls horizontally.

1. Enumerate the visible interactive-looking controls on the page: `a[href]`, `button`,
   `[role=button]`, `[tabindex]`, `[onclick]`, `[data-element-id]`, `[data-action-id]`, list and
   card rows, suggestion rows, and natural card hit areas.
2. Classify each one as a source-path action, semantic navigation, semantic inline state, semantic
   state mutation, or non-task static content.
3. A visible control that looks interactive must behave the way its visible semantics and the page
   contracts say. When correct behavior cannot be proven, make the control explicitly
   non-interactive instead of leaving an arbitrary fallback route, a silent no-op, or a sibling
   task's target.
4. Dead link: fix the `href`. Unclickable back or close: add the handler. A control that must not
   act: remove the interactive affordance and record that in the report.
5. Horizontal overflow: find the exact element causing it and the exact pixel offset, then write
   one targeted fix.

`review-trace` blocks a row on `page_structural_audit.mismatches` and on its off-path surface audit,
so every finding of those two dimensions has to be closed or referred.

## `state_handoff`

Symptom: a confirm, send, share, or submit action reaches the expected target page, and the
execution may report the step as done, but the next generated state lacks the business object the
source shows on the parent or consumer page: a sent file card, a selected recipient, a submitted
draft, a chosen picker value, or a sent text bubble.

Root cause: the action surface has route behavior but no real app-owned state handoff, or the
target page never reads or projects it. Four variants recur: the route bridge navigates before the
click handler writes state; the state is written only under diagnostic context; the target page
has hidden markup but no load or projection logic; the send control clears the input without
appending the visible item.

1. Run the state-ownership gate of `references/repair-ladder.md` §4 first. Enumerate the writer, the
   state handle, the payload, the commit ordering, the consumer, the projection, and the reset and
   dirty-sibling behavior before patching anything.
2. Read the source row that performs the action and the next or terminal row where the effect
   should be visible. Name the exact entity, value, message, or file and the expected consumer
   surface.
3. Compare the visible action's semantics with every active role and state mirror. If the action
   selects one role while the state target names another, repair or refer the first stale layer.
4. Inspect the source action host, the route bridge, the click handler, and any projection hook.
   Identify whether navigation happens before the normal event handlers can write state.
5. Add or repair a real app-owned handoff at the earliest reliable user action point: selection,
   input, or submit. Use explicit state with the entity and value metadata in session storage, local
   storage, URL state, the runtime store, or a window-message style channel. Do not rely on
   diagnostic context.
6. Add target-page consumer logic that reads the handoff on load and projects it into visible DOM
   before the screenshot is captured: the parent chip or value, the message bubble, the file card,
   the draft row, the selected option, the result row, or the terminal outcome text.
7. Make the projection work in a plain browser session with no diagnostic context. A diagnostic
   hook may assist diagnosis but can never be the only source of the visible state.
8. Verify by executing the full segment naturally: source action, then target or parent page, then
   the visible projection assertion, with a `pageerror` listener and the exact text, value, or file
   metadata asserted in the DOM.
9. Run `scripts/audit_state_ownership.py`, the six browser cases of `repair-ladder.md` §4, and
   `scripts/validate_repair_report.py`. A parent value that appears only under step or action
   context is a failed repair even when the screenshots match.

Changing the target page is valid when it repairs that page as a runtime and state consumer. It is
a shortcut when the target page fabricates the handoff from the trace id, the step index, or
diagnostic context.

## `runtime_projection`

Symptom: the source shows typed or focused suggestions, an inline preview, picker suggestions, or
query draft state on the same source shell, but the package misses the surface, leaves default or
background content wrongly visible, or keeps a stale static suggestions page as the authority.

1. Repair the source page and its runtime so the projection renders from the real state fields: URL
   or query state, the input value, the runtime store, or session state. Do not create or rely on a
   separate static suggestions or results page for a same-shell projection.
2. Verify the empty or default state and the typed or focused state separately. Default content must
   be hidden or visible according to the source lifecycle, and the rows must match the expected
   candidate text, count, order, grouping headers, and shape.
3. For a typeahead or query-result list, dump and assert the live top-N visible rows after natural
   typing, **before** clicking any target row. Record the row text and order, the group headers, the
   action-host ownership, the bounding boxes and top positions, the scroll-container offset, and
   whether the target host is initially visible or has to be reached by scrolling.
4. Repair the visible action hosts on the projection surface so the natural click or submit points
   at the correct target with no diagnostic context.
5. Verify the post-click or post-submit projection only after the pre-selection list is correct.
   Route success, committed state, or a parent projection cannot compensate for a visibly wrong
   typed or focused list order or grouping.
6. Clean the stale authority of a demoted static page out of every active package consumer: the
   generated pages, the route manifest and route runtime, the action-host map, and the runtime
   binding manifests. A debug log may keep the historical string only when the report records why
   it is not an active consumer.
7. When the page or runtime is shared by several traces, run the cross-trace runtime projection
   audit of `review-trace/references/review-packet-contract.md` and cite its artifact path.
8. A report that proves only "clicked the target and the next page is correct" is incomplete. It
   must also prove the source current-row typed or focused surface: the top-N visible candidates,
   the ordering, the grouping, the action-host row position, and the scroll reachability.
9. Reject any implementation that derives the query from a static `aria-label`, aggregate action
   aliases, step-description text, a trace-wide label inventory, or the step index alone. The
   visible default suggestion surface stays correct until a real type or fill action commits query
   state.
10. When the boundary between a same-page projection and a distinct page is undecided, stop and
    refer `semantics`.

`review-trace` emits `autocomplete_projection_gap` as an `implementation` mismatch.

## `overlay`

Symptom: an overlay, panel, menu, drawer, filter sheet, or action sheet was repaired in one
direction and then cannot close, cannot reopen, loses its hit targets, traps pointer events, or
leaks outside the app shell in another state.

Root cause: the fix treated the visible layout defect as the whole acceptance target.

1. Measure the app shell and the panel with `getBoundingClientRect()` before editing. Record the
   exact `left`, `right`, and `width`, not only screenshots.
2. Replace ambiguous `toggle` logic with explicit open and close functions whenever `class`,
   `opacity`, `pointer-events`, and `aria-hidden` can desynchronize.
3. Keep the panel positioned inside the app shell in every visible state. Do not rely on
   viewport-wide `left:0; right:0` unless the app shell itself owns the viewport.
4. Make the visible close, cancel, backdrop, or arrow host truly actionable. If a global capture
   handler blocks the host, repair the event ordering on that host instead of disabling the
   runtime.
5. Verify directly: default closed, open, the shell-contained open rect, close, the hidden state
   (`class`, `opacity`, `pointer-events`, `aria-hidden`), reopen, and a real target action after
   reopen.
6. When a route or action host inside the panel was task-relevant before the fix, click it after
   the reopen verification. A panel that opens and closes but breaks its target action still fails.
7. Read the source screenshot first and understand the complete visible layout. Reproduce every
   visible option, button, and text, not only the task-critical ones. Task-related elements are
   visible by default rather than hidden behind extra JS, and the non-task-critical content of the
   surface stays visually consistent.

## Picker, selection, and media highlight

Symptom: a selected cell, checkbox, media thumbnail, picker option, or chip shows the wrong
highlight scope, the wrong selected resource, or a stale preview thumbnail, or it looks correct on
the current row and breaks the next page or resource consumer.

Root cause: the fix validated a visual selected state without proving the owning surface and the
resource handoff.

1. Measure the selected control's bounding box and the surrounding surface's bounding box. When
   the selected visual state covers the surface instead of the owning cell or control, repair the
   class or overlay target before touching resources.
2. List the DOM image refs before the selection, immediately after it, in any preview or footer,
   and on the next or parent page that consumes the selection.
3. Compare the refs to the page's resource projection and input manifest and to the source resource
   labels. Prove the refs are legitimate active resources for this trace and page; the absence of
   source screenshots is not enough.
4. Preserve or repair the real state handoff (session storage, the runtime store, URL or query
   state, or an explicit route payload) so the next page reads the same selected business object
   the picker displayed.
5. Verify both directions: the default unselected surface has no selected ring, count, or footer;
   selecting adds the ring only to the owning cell or control, updates the count or footer, and
   leaves sibling cells and resources unselected; deselect or reset works when it is visible.
6. Click the next or confirm action after the selection and assert that the consuming page shows
   the same resource or value. A current picker screenshot alone is not closure.

## App-shell containment

Do not repair decorative device-frame differences by removing the app shell. Outer bezel, notch, or
border variance is not a mismatch unless task content, action hosts, CTAs, sheets, or overlays
overflow or are clipped inside the shell.

For an overlay, panel, or menu, measure the shell and the panel `getBoundingClientRect()` before and
after the change, then prove close and reopen: width containment alone is not enough. For a picker,
selection, or media surface, measure the owning cell or control rect and the surrounding surface
rect, and prove the selected visual state attaches to the owning cell or control rather than to the
whole surface.

## Runtime clears already-populated static content

A page can render correct static markup and still show an empty list because runtime JS clears the
container on load. Guard the clear instead of re-adding the content:

```js
if (container.children.length > 0) return;
```

## Same-page section or tab as one scroll surface

First decide whether the source shows true panel replacement or one continuous scroll surface. When
it is one surface:

- keep the regions in the same DOM flow;
- update the active tab from scroll or anchor state;
- suppress a programmatic anchor scroll being read as a user transition;
- keep no duplicate or retired panel visible;
- verify at least one intermediate screenshot proving it is not a hard panel swap.

Modeling the regions as two hidden panels that swap is the recurring false fix here.

## Terminal boundary

When the checklist's terminal row sets `terminal_contract.commit_status` to
`committed_effect_unobserved` or `precommit`, repair only what natural execution needs to reach and
actually issue the marked visible action. Do not fabricate destination content the source never
recorded, and do not fail merely because the source has no settled destination. Preserve any real
observed route, loading state, or overlay as evidence with the destination content not claimed.
