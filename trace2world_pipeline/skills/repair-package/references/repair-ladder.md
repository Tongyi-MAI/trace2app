# Repair Ladder

Rules for `repair-package`. The defect packet names the first known failure, not the scope of the
fix: a report on one row invalidates every earlier pass judgment for that trace, and the repair
must hold for the whole surface class it touches.

The ordered repair chain of each issue class is in `references/repair-details.md`.

## 0. Bind the work item and audit the whole surface

### Bind the work item

Before diagnosis, bind the exact object under repair:

- the result directory and the slice (`tasks.json` `task_id`);
- `trace_id` and the step or step range;
- the defect id, or the review or probe packet that reported it;
- the source trace root and the marked source screenshot paths;
- the active scope: one package, a shared surface, or a referral upstream;
- the report path and the statuses this run may return.

Do not inspect a sibling rerun, a stale review directory, or a same-named page in another result.
Do not start from the generated page filename alone; bind it to the trace step and the source
observation first.

### First evidence ledger

Write this ledger before editing:

| Evidence | What to record |
|---|---|
| source current row | visible source page, source surface, target host, selected and default state, scroll anchor |
| source next row | action effect, parent projection, route, overlay or sheet state, business value |
| generated current row | the same fields, plus missing and extra surfaces |
| generated next row | the same fields, plus premature and future state leaks |
| marked screenshot | whether the mark is on the same semantic host and the same owning surface |
| natural browser action | page errors, clickability, route and state effect without diagnostic-only shortcuts |

If the mismatch cannot be explained after this pass, stop and gather more evidence. Do not edit
from memory or from the action text alone.

### Whole-surface audit before the first edit

Compare the source against fresh screenshots of the served package for every affected step:

1. **Every visible region**: header, sidebar or nav, content area, bottom bar, badges, counters.
2. **Dynamic elements**: which elements change between steps? Badges, counts, prices, checkmarks,
   scroll position.
3. **Layout and scroll state**: what is visible in the viewport? Are nav or sidebar tabs fully
   visible? Is the content scrolled?
4. **State transitions**: trace each step's before and after change for every dynamic element, not
   only the reported one.
5. **Page identity and entry-source fit**: when the target page is shared by several source actions,
   compare the active trace's post-action source row against the sibling source rows that reuse the
   same generated filename. Two entries whose hero or banner, selected tab, action set, list
   content, or resources differ are compared before any patch, never assumed equal. When the
   frozen specification models them as states of one shared page, repair the state projection
   under that identity; when it models distinct pages, repair each; when it does not decide, the
   boundary is a `semantics` referral. The repair never merges or splits pages itself.
6. **Initial viewport fit**: for a fix that starts from a home page or a shared shell, compare the
   active trace's first source row against the generated first row. A correct action host does not
   pass when the generated page has an extra or missing banner, a shifted or cropped grid, or the
   wrong visible feed or cards.
7. **Positive resource binding**: for every visible media or card surface, list the active DOM image
   refs, the expected source-visible count and order, and the allowed active resources. A check
   that only says "no source screenshot or crop is rendered" is insufficient.
8. **Adjacent action coverage**: for overlays, panels, menus, pickers, tabs, carousels, and
   selection surfaces, list the reverse, close, deselect, reopen, and next actions that can be
   performed naturally from the same surface. When the fix changes one of those surfaces, verify
   the adjacent actions in the same pass.

Write the audit as a table: rows are steps, columns are sidebar state, checkbox state, badge text,
bottom bar text, and scroll position, comparing the source against the naturally reached generated
state row by row.

One comprehensive audit, then one comprehensive fix. Fixing only the reported symptom while other
mismatches stay (sidebar visibility, badge updates, scroll position) costs one more round per
missed dimension.

### One-pass full-chain fix

Before writing code, trace the complete dependency chain of the fix:

0. The package's own authority chain, back to front: the runtime contract, route manifest, and
   route runtime under `app/phase_sdk_generation/runtime/`; the action-host manifest validation
   under `app/phase_pagegen/`; the generated page under `app/phase_pagegen/pages/`; and the
   packaged resources it loads.
1. The host binding: is the action host bound to the correct element?
2. The click handler: does it produce the correct behavior (navigation, toggle, overlay open)?
3. Cross-page state: does the action write a real handoff before the route or navigation, and does
   the target or parent page read it and project it visibly?
4. Next-row and parent projection: does the state after the action show the exact source entity,
   value, message, or file, not only the expected route?
5. Adjacent surface actions: do close, cancel, reopen, deselect, retry, and next still work after
   the fix?
6. Resource handoff: do the source surface, the preview or footer, and the consuming page read the
   same legitimate resource or value?

Fix every missing link in one pass. Do not fix one link, run the execution again, discover the next
missing link, and fix that one too.

When the fix only changes generated HTML, the report must name the package consumers that were
inspected for the same behavior and which of them were edited.

### Several defects at once

1. Cluster every issue by root cause; do not fix them step by step in isolation.
2. Rank the root causes by slices affected multiplied by severity.
3. Fix the root causes in order, one at a time, and verify after each.
4. **No cost-based dismissal**: if the mismatch exists and is fixable, fix it.

## 1. Classify before editing

Answer in the report's `diagnosis.rationale`:

- Which source evidence fixes the expected state: the marked pre-action screenshot of the step,
  the next screenshot (the effect), the obligation row, and the instruction?
- Does `C` support the behavior? If closure needs a new page identity, state or data relation,
  guard, or effect, stop: `not_repairable`, referral `semantics`.
- Does the defect expose several filenames for one conceptual surface, a mode-suffixed page, or a
  task-specific value promoted into page identity? Inventory every sibling filename, route, page,
  and source observation **before any patch**, and record the inventory in `diagnosis.rationale`.
  A language, filter, query, or delivery mode is not a page split by itself. The repair must not
  choose a canonical page from the failing trace, from resource richness, or from current HTML
  quality: an undecided page identity is a `semantics` referral, not a package edit.
- Is the package the owner? If the page already realizes the state and only a checkpoint,
  binding, evaluator, or terminal policy reads it wrongly, stop: the owner is `supervision`.
- Has the source a coherent action here? A wrong recorded action or an impossible transition
  between two source screenshots is not a package defect: referral `semantics`.

Only fix from evidence:

- visible in the source but not actionable: patch the HTML or JS;
- wrong route: patch the route runtime or the data attributes;
- content not in the source screenshot: do **not** add it;
- a step description asking for UI that is not in the screenshot: mark it noisy, do not add it;
- **source evidence is the only authority**: the bound source sequence (screenshots and recorded
  actions), the obligation rows, the instruction, and the frozen specification; do not use domain
  knowledge to infer what "should" exist;
- **a source-derived fallback is controlled, not blanket-forbidden**: an audited minimal clean crop
  may enter a packaged resource tree after matching extraction fails, while full pages, sprites,
  and marked, unclean, or unprovenanced crops stay forbidden;
- **a negative resource guard is not a positive binding**: the absence of unauthorized assets does
  not prove correct binding. Verify the active DOM refs, their count and order, and the
  `resource_resolution` ledger whose fields are in
  `review-trace/references/review-packet-contract.md` ("Active Resource Binding"). Refer
  `semantics` only after `matching_extracted_resource`, `source_derived_fallback`,
  `external_semantic_resource_fallback`, and `semantic_placeholder_fallback` are exhausted.

Record every value the defect involves in exactly one authority layer, then repair at the first
stale one:

| Layer | Meaning |
|---|---|
| canonical initial state | the app's default state from the specification |
| task launch state | the task's declared `initial_state`, injected by the host at reset |
| page entry initialization | what a page derives on entry from launch state and committed state |
| runtime page initial state | the page-entry result the runtime derives; a derived result, never a second source of truth |
| action-surface source-state precondition | the state the action surface requires before the action is legal; it constrains action legality |
| action transition | the state change a visible action commits; only this layer may normally advance state |
| trace observation | what a screenshot showed; evidence only, never an authority |

The runtime page initial state is a derived page-entry result, not a second source of truth, and
the trace-entry observation is evidence only. The action-surface source-state precondition
constrains action legality, and only the action transition may normally advance state. Prove that
the distinction survives reset, natural entry, and the first responsible action, with before and
after evidence for each.

A trace id, step index, action id, or file-name suffix never authors business state.

## 2. State model first

Restate the defect at the environment-model level before touching HTML or JavaScript:

- surface kind: route page, same-page section, overlay or sheet, sticky shell, carousel,
  virtualized list, or runtime projection;
- lifecycle: default state, current-step state, post-action state, forbidden future state, parent
  projection, selected and default values;
- continuity: continuous scroll or input, modal open and close, route navigation, or panel
  replacement;
- modalities that must agree: tap, small cumulative swipe or wheel, typed input, back;
- owning surface: the card, list, sheet, section, header, or footer that gives the host its
  meaning, not only the host.

Change the earliest stale consumer of that model and verify its lifecycle, continuity, and
modalities. Making the next row look right is not a repair.

## 3. Issue map

`diagnosis.issue_class` is one of these classes.

| Class | Repair and verify |
|---|---|
| `initialization` | defaults come from the canonical state or the task's launch state, or stay neutral; static DOM never preloads a future selection. Verify the first DOM after reset for two tasks and no cross-task leakage. |
| `state_handoff` | the visible source action commits state before navigation or same-page projection; the target reads that state. Four variants recur: the route bridge navigates before the click handler writes state; the state is written only under diagnostic context; the target page has hidden markup but no load or projection logic; the send control clears the input without appending the visible item. Add the handoff at the earliest reliable user action point (selection, input, or submit) and prove the projection also works in a plain browser session with no diagnostic context. Verify the natural source flow without diagnostic context. |
| `parent_projection` | the parent shows only what earlier actions committed; no sibling or future value. |
| `shared_page` | a page shared by several tasks is parameterized by launch and committed state; changing a shared default must not preload one task's later state into another. Scan every task that uses the page. |
| `lifecycle` | future-step surfaces and sibling selections are absent before their revealing action and present after it. For a same-page section or tab, first decide whether the source shows true panel replacement or one continuous scroll surface; when it is one surface, keep the regions in the same DOM flow, update the active tab from scroll or anchor state, suppress a programmatic anchor scroll being read as a user transition, keep no duplicate or retired panel visible, and verify at least one intermediate screenshot proving it is not a hard panel swap. |
| `actionability` | the visible host on the owning surface carries the action, label, hit area, and effect; a hidden duplicate or a static element does not count. |
| `route` | the natural tap on the visible host routes to the next source page identity and keeps the required handoff; shared submit hosts resolve the current task's action, not a sibling default. |
| `overlay` | default closed, open, close or backdrop, reopen, and the target action after reopen; the panel stays inside the app shell in every state. Replace an ambiguous `toggle()` with explicit open and close functions whenever `class`, `opacity`, `pointer-events`, and `aria-hidden` can desynchronize, and verify all four stay synchronized. If a global capture handler blocks the close host, repair the event ordering on that host instead of disabling the runtime. |
| `visual_layout` | the same viewport shows comparable content, chrome, density, and fold position; measure the element chain before any CSS change. Do not repair decorative device-frame differences by removing the app shell: outer bezel, notch, or border variance is not a mismatch unless task content, action hosts, CTAs, sheets, or overlays overflow or are clipped inside the shell. |
| `resource` | active media refs match the source-visible count, order, and identity before and after the action and on the consuming page. Resource order: matching extracted resource, audited clean source crop, provenance-backed semantic media, role-matching placeholder; never a full screenshot, a marked crop, or a known-wrong sibling entity. The repairer produces the `resource_resolution` ledger `review-trace` blocks on: resolution class, semantic identity, visual role, `identity_bearing`, extraction attempts, rejected candidates, selected source, crop provenance (source trace, step, path, hash, output hash, bbox, semantic identity, extraction-rejection evidence, crop-integrity assertions), `identity_claimed`, `task_identity_dependency`, and `active_resource_binding_satisfied`. A negative contamination check is not a positive binding. Never clone a sibling page and string-replace: compare both source screenshots and use the instance's own resources. |
| `content` | every visible row, card, option, badge, and count matches the source-visible surface; add every missing item in one pass and check the same pattern on every sibling page. A page can render correct static markup and still show an empty list because runtime JS clears the container on load: guard the clear (`if (container.children.length > 0) return;`) instead of re-adding the content. |
| `structural` | an unclickable back or close control, a dead `href`, a dead surface, or horizontal overflow. Fix the `href`, add a handler, or make the control explicitly non-interactive; for overflow, find the exact element and the exact pixel offset before changing CSS. `review-trace` blocks on `page_structural_audit.mismatches` and on its off-path surface audit. |
| `runtime_projection` | a same-page query, suggestion, or typeahead projection on the source shell. Render it from the real state fields (URL or query state, input value, runtime store, session state); do not create or rely on a separate static suggestions or results page for a same-shell projection; verify the empty or default state and the typed or focused state separately; dump and assert the live top-N rows after natural typing **before** clicking any target; and remove a demoted static page from every active package consumer (route tables, action-host maps, runtime binding manifests). `review-trace` emits `autocomplete_projection_gap` as `implementation`. |

## 4. State-ownership gate

Required for `initialization`, `state_handoff`, `parent_projection`, and `shared_page`. Record the
ownership chain:

```text
visible action -> writer -> state handle -> payload -> commit before navigation
-> consumer -> visible projection -> reset and dirty-sibling behavior
```

Classify every input the chain reads as `app_owned`, `task_setup`, `diagnostic_context`, or
`legacy_fallback`. Diagnostic context may locate a host but never choose a business value; remove
legacy fallbacks from the business path.

### Backend-neutral state ownership

Task-specific initialization is a semantic contract, not a storage-backend requirement. Before
changing where state lives, define the backend-neutral contract:

```text
state handle + scope + reset policy + task-launch writes
-> page-entry hydration and projection
-> action precondition and transition writes
-> visible consumers
```

Then bind that contract to whatever the package already uses: the task setup payload
(`tasks.json` `initial_state`), URL or query state, browser storage, an in-memory runtime store, a
structured store, or a legacy adapter. The adapter preserves the same handles, scopes, lifecycle,
and negative and positive assertions. **Do not migrate the package's storage backend.** A patch
that only fits one storage choice is not a design fix, and a backend migration is out of scope
unless the frozen specification requires it.

For every task-specific initialization repair, record the authoritative producer, the selected
adapter and its host-consumed entry point, the page-entry hydration path, the action-transition
write path, and proof that no page-local file is the state authority.

### Role and state-mirror agreement

Compare the visible action's semantics with every active role and state mirror. If the action
selects one role while the state target names another, repair or refer the first stale layer; a
page-local store must not conceal the contradiction.

### Query authority and multiplexed hosts

Static `aria-label` text, aggregate action aliases, step-description text, and trace-wide label
inventories must never be treated as query state. Record whether the source action's state is
committed **before** a shared or global navigation handler can strip URL state or pick a sibling
default.

When one visible confirm, submit, or input host multiplexes sibling actions, diagnostic context may
identify the trace but must not own the business phase: one role versus another, draft versus
committed, first versus subsequent confirm. Derive that phase from an app-owned state handle and
test its natural transition sequence. **If removing the step or action context changes the business
result, the repair is diagnostic-conditioned and invalid.** Repair the app-owned state machine
before touching the target widget.

### The six browser cases

Run `scripts/audit_state_ownership.py` and these six browser cases, all in the raw assertion report:

1. `empty_state_with_full_diagnostic_context`: diagnostic context alone leaves the neutral state;
2. `legacy_trace_key_only`: a trace-keyed storage entry is ignored;
3. `dirty_sibling_state`: stale state of another task, role, or query is ignored;
4. `valid_app_state_without_step_context`: real committed state projects without step or action
   context;
5. `natural_action_then_context_ablation`: the visible source action, then removal of step and
   action context, gives the same business result;
6. `parent_reload_or_reset`: reload or reset follows the product lifecycle and resurrects nothing.

## 5. Acceptance expansion

The reported symptom is the entry point. List every adjacent check in `acceptance_expansion`,
each `pass` or with a reason:

| Reported surface | Adjacent checks |
|---|---|
| overlay, panel, sheet, menu | default closed, open, close or backdrop, reopen, target action after reopen, containment open and closed, and `class`, `opacity`, `pointer-events`, and `aria-hidden` synchronized |
| tab, section, carousel, scroll region | default position, forward and reverse movement, target reachable, real scroll container offsets, no sticky drift; and when the source shows one continuous scroll surface: one DOM flow, active tab following scroll or anchor state, no visible duplicate or retired panel, and at least one intermediate screenshot |
| picker, chip, checkbox, filter | default unselected, selection on the owning cell only (measure the owning cell rect and the surrounding surface rect), deselect or reset when visible, count or badge update, the next action uses the selection; for a wheel or drag picker also small cumulative wheel, a segmented fast swipe that must not skip multiple rows, keyboard arrow, close and reopen, a finite selected index, and the active option centered in the highlight band |
| search, typeahead, suggestions | default surface, natural typing, top-N rows in order with headers, target row position and reachability, committed projection after the tap; plus the query authority the runtime actually reads (a static `aria-label`, aggregate aliases, step-description text, and a trace-wide label inventory are never query state) and whether the state is committed before a shared or global navigation handler can strip URL state or pick a sibling default |
| media | refs before and after the action, in preview or footer, on the consuming page; load count; no wrong-entity refs; and the `resource_resolution` class and provenance of every visible surface |
| submit, send, confirm | pre-submit validation, successful submit state, retry when visible, parent or terminal projection, the state write |

## 6. Required assertions

Run against the served package in a fresh browser context, with a `pageerror` listener, through
visible actions only. The raw report (`schema`, `trace_id`, `all_cases_pass`, `cases` with a
`status` per case, evidence paths) is written by the probe program and never edited by hand; its
`trace_id` must equal `defect.trace_id`, and its SHA-256 goes into the repair report. Include, as
the class requires:

- a negative assertion for the forbidden premature or future state;
- a positive assertion for the current and post-action state;
- action-effect alignment: for an action at step *i*, inspect the post-action DOM against source row
  *i+1*. A pass on the pre-action row is not action-effect evidence;
- the round trip for overlays;
- host and owning-surface actionability;
- a whole-viewport comparison against the source screenshot;
- real scroll-container offsets and natural cumulative gesture deltas for scroll fixes;
- for a same-page tab or section treated as one scroll surface: one DOM flow, no visible duplicate
  or retired panel, the active tab following scroll state, and at least one intermediate screenshot
  proving the transition is not a hard panel swap;
- a live top-N dump of the typed or focused surface before any target click, for a query projection;
- input-dependent controls checked after natural typing;
- the same assertion on every other page or task that shares the changed surface.

## 7. Practical pitfalls

- A DOM `el.click()` or forced dispatch is not an action; if a natural tap fails, repair the host.
- An inline `onclick` that resets `className` overrides listener-managed classes.
- `history.back()` needs a history stack created by visible navigation.
- A duplicate element id elsewhere on the page can capture the selector or the tap.
- A static `div` has no handler; replace it with an interactive control.
- `scrollIntoView` scrolls every scrollable ancestor; set the dedicated container's offset.
- Mutate the DOM first, scroll second.
- Measure `clientWidth`, `scrollWidth`, and `getBoundingClientRect()` along the element chain
  before changing CSS.
- Counters, badges, and price ranges need real update handlers.
- A "select all" control must start in the state of its children.
- Map the complete show/hide flow of a panel across all steps, then write the state machine once.
- Read every source screenshot of a carousel before implementing direction; carousels bounce.
- When an earlier fix was reported incomplete, first state why its assertions missed the defect,
  and add the assertion that would have caught it.
- A query-suggestion runtime can clear an already-populated result container on load; guard the
  clear (`if (container.children.length > 0) return;`) instead of re-adding the content.
- Start a fresh browser context after the last fix and record the generated artifact timestamps or
  digests; a cached page proves nothing about the edited one.
- Never copy an existing page and string-replace. Different instances of the same conceptual page
  often have completely different layouts. Compare the source screenshots of both instances before
  choosing an approach, and use the target instance's own resources, not the template's assets.
- When the user says "X is wrong", verify against the source screenshots before disagreeing.
- After the mechanism works, **count** the items. If the source shows 15 suggestions and the package
  shows 3, the fix is incomplete. Match the source item count and content, including badges, result
  counts, embedded cards, and rows.
- "Content is in the HTML, just below the fold" is **not** a valid excuse. If the source screenshot
  shows it, the package screenshot must show it too; scroll before capturing.
- When "X is missing" is reported, read the **full** source screenshot and add **all** missing
  elements in one pass. Do not add 2 of 5 items and wait for the next report.
- Do not blindly `scrollTo(0,0)`. Identify the **viewport anchor element** in the source screenshot
  (what is at the top of the visible area) and scroll to *that* element; the source often shows a
  mid-page anchor.
- For a terminal row with no interaction, scroll the package's real scroll container to the
  source's position before capturing.
- Never guess-and-iterate CSS. Find the **exact** element causing overflow and the **exact** pixel
  offset, then write **one** targeted fix. Known pattern: an `overflow:hidden` container
  auto-scrolled by browser focus leaves `scrollLeft` non-zero and shifts absolutely-positioned
  children; fix it with `overflow-x: hidden` on the container or reset `scrollLeft` after the
  interaction.
- Measuring width after open is not enough for a panel: verify close, the hidden state, reopen, and
  a target action after reopen. If the close control needs `pointerdown` or capture ordering to beat
  a global handler, verify it directly instead of assuming the click handler fires.
- `position:sticky; top:0` sticks to the nearest scrolling ancestor. If the scroll container is a
  parent of the sidebar's flex container rather than a sibling, scrolling pushes the sidebar out of
  view; make the content area a scrollable **sibling** of the sidebar.
- For a wheel or drag picker, verify the natural modality set: open, small cumulative wheel, a
  segmented fast swipe that must not skip multiple rows, keyboard arrow, close or cancel, reopen,
  confirm, a finite selected index and value, and the active option centered in the highlight band.
- After fixing one query or suggestion set on a page, check every other query in the same data array
  and every sibling page with the same pattern. Do not fix one entry and leave a sibling sparse.
- Before fixing a host binding, grep the whole page for every occurrence of that id. A stale binding
  on the wrong element intercepts `querySelector` and the tap.
- Validate selector identity and uniqueness directly. A manifest that writes `services_more` while
  the DOM writes `services-more` can still leave a validator reporting zero missing hosts; check the
  live selector match count and uniqueness, not the manifest count.
- A reached route is not a full task path. `home -> channel -> list` cannot close a task that also
  requires filtering, selecting, checkout, and a pre-submit stop; a focused probe proves only its
  own segment. Closure needs the complete instruction path or an explicit referral.
- A model rerun that randomly avoids the blocker is not repair evidence: if the originally failing
  path is still blocked, the repair is not done.
- Never convert a raw `pass=false`, an exception, a missing assertion, or an unknown state into a
  hand-written `status=pass`. Preserve the raw report, hash it, and let the validator read it.
- A one-direction fix is a false fix. If a previous repair only solved opening, visibility, or
  highlight position, write the false-pass postmortem first, then add the round-trip or
  resource-handoff assertions that would have caught the miss.
- Never use a random or known-wrong extraction. Search the source trace resources first; then an
  audited minimal clean crop; then a provenance-backed external semantic resource; finally a
  role-matching placeholder with origin and usage provenance. For an identity-bearing substitute,
  set `identity_claimed=false` unless exact identity is proven.
- Do not validate only the selected ring on a media picker. Record the DOM refs of the source grid,
  the selected or preview footer, and the next-page consumer; the selected state attaches to the
  owning cell, not to the whole surface.
- After adding an asset, hash and path scan it against the source trace root. Full pages, sprites,
  marked or unclean crops, copied screenshot filenames used as page substitutes, and unprovenanced
  derivatives are invalid.

### Never equivalent

- a source edit is not repair closure;
- a reached route is not the full task path;
- a clickable component is not a correct business state;
- a static evaluator, checkpoint, or probe pass is not GUI lifecycle closure;
- `file://` or an ad-hoc static server is not the lifecycle host: serve the package with `t2w-serve`
  and verify the served root is the requested package;
- a ready manager is not the correct resource loaded;
- a target-model score is not environment path correctness;
- a post-closure benchmark success is not the producer of the earlier `repaired` report.
