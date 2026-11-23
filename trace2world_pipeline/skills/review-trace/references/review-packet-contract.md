# Review Packet Contract

This file belongs to `review-trace`. It defines the fine-grained gates of the source-trace review in Algorithm 4: what a faithful environment must show at each retained step of a source trace.

Review is read-only. It produces evidence and, for a non-pass, one defect packet naming the step, the expected and observed state, and the affected obligations. Every non-pass verdict carries an `owner` from the released `closure.Owner` values and `repairable` (`true` only for `implementation`: the packet can be closed by editing the package under the frozen page specifications). A mismatch whose closure would need a page identity, state or data relation, guard, or effect that no source evidence supports is `semantics` and not repairable. Never infer the owner from a missing recipe, a failed earlier repair, or elapsed budget; the budget belongs to the repair ledger of `t2w-close`, not to review.

## Sequence Identity And Visible Locator Provenance

Use these join keys before judging a row:

| Artifact | Key | Indexing |
| --- | --- | --- |
| source screenshot | `action_{step_index:04d}` or equivalent retained source row | 0-indexed retained trajectory step |
| materialized bindings | `{trace_id}:{step_index}` | 0-indexed canonical obs key |
| generated DOM | visible role/name/text and owning-surface identity | selected during execution by `execute-trace`; action ids are diagnostic metadata only |
| execution actions | execution `action_attempt_index` and observed-state ordinal | chronological execution identity; do not coerce it to the source index |
| review matrix | `trace_id`, `step_index` | normalized canonical step index |

Locator priority is verified from the execution packet and raw action log:

1. visible role/name inside the expected source-owning surface;
2. exact visible text/label inside the expected source-owning surface;
3. visible placeholder/title/associated label inside that surface;
4. visually verified natural coordinate on the same owning surface for an icon-only or gesture control.

`data-element-id`, `data-element-aliases`, `data-action-id`, generated action contracts, replay step/action context, and hidden materialized bindings may be inspected after the action as diagnostics. They must not have selected or dispatched the screenshot-producing action. The hard boundary is `execute-trace/references/pure-gui-execution-contract.md`.

If step 0 / `action_0000` is retained by the source trace, the review packet must include it. A runner or matrix that silently drops step 0 is `evidence_blocked`.

## Obligation Baseline

Before judging source step integrity or the execution, consume the matching per-trace checklist produced by `build-obligations`.

Required packet field:

```json
{
  "source_checklist_baseline": {
    "status": "pass | blocked",
    "checklist_path": ".../traces/<trace_id>/stepwise_checklist.json",
    "checklist_sha256": "...",
    "source_trajectory_sha256": "...",
    "checked_step_count": 0,
    "kept_step_count": 0,
    "step_zero_retained": true,
    "step_zero_checked": true,
    "stale": false,
    "blockers": []
  }
}
```

The baseline must be one trace's checklist, not a package-wide step table. Verify its source digest and exact retained-step coverage. For each reviewed row, consume the matching baseline obligations before writing `source_step_integrity` or environment lifecycle verdicts.

If the baseline is missing or stale, return `evidence_blocked` and rebuild it with `build-obligations` from the current trace. Rebuilding an oracle is not a pass.

## Execution And Sequence-Sprite Preflight

Before review, consume a current `trace2world.gui_execution.packet.v1`. Review must not execute actions or extend an unexecuted suffix. Before `review_pass_candidate`, execution must be `execution_complete`.

Required summary:

```json
{
  "schema": "trace2world.gui_execution.packet.v1",
  "status": "execution_complete",
  "driver": "host_api | playwright",
  "pure_gui_execution": true,
  "intermediate_direct_navigation_used": false,
  "action_id_locator_used": false,
  "forced_or_dom_click_used": false,
  "state_or_query_injection_used": false,
  "inspection_endpoint_used_for_action_choice": false,
  "review_performed": false,
  "sprites_generated": false,
  "pairwise_alignment_claimed": false,
  "observed_states": [],
  "action_attempts": []
}
```

Each interactive/stateful row must record:

- `source_step_index`, `capture_step_index`, and visible row label;
- expected visible value, observed host text/value, visible locator basis, owning surface, and source page;
- `semantic_value_ok`, `visible_enabled_clickable`, `human_clickable`;
- host and shell bounding boxes, center hit target, and `elementFromPoint` nearest action host;
- `pre_action_projection=false`;
- natural action method used (host API action or Playwright locator); hidden DOM APIs, forced fill/click, DOM/evaluate dispatch, action-id locator authority, intermediate navigation, and injected query/storage/business state invalidate the capture round.

Then follow `sequence-sprite-review.md` and require the single physical composite sprite and its manifest. The review packet must set `display_order=[source_sequence, execution_sequence]`, `single_physical_sprite=true`, `composite_sprite_path`, and `pairwise_alignment_claimed=false`. Two separate sprites or paginated execution pages are `evidence_blocked`.

If the execution packet is missing, stale, `execution_invalid`, bound to another package revision, contradictory, or not reflected in the observed sequence, return `evidence_blocked`. If it says `stopped_at_first_blocker`, review the observed prefix and blocker but never return a full-trace pass.

## Review Matrix Schema

Write review artifacts under the result directory:

```text
run_management/review/
  trace_step_review_index.json
  traces/<trace_id>/<round>/review_packet.json
  traces/<trace_id>/<round>/step_matrix.json
  traces/<trace_id>/<round>/sprites/...
```

Records in `step_matrix.json` are sequence anchors, not screenshot pairs.

Review does not schedule. Its verdict and owner feed `closure-loop`, which applies Algorithm 4 through `t2w-close`; a non-pass is either repaired (owner `implementation` or `supervision`), sent upstream (`semantics`), or dropped (`model`, `provider`, or an exhausted budget).

Anchor statuses: `unchecked`, `pass`, `fail_first`, `fixed_pending_rerun`, `rerun_pass`, `blocked`, `skipped_after_first_fail`.

Minimum anchor shape:

```json
{
  "trace_id": "...",
  "sequence_anchor_id": "anchor_0000",
  "status": "fail_first",
  "execution_packet_path": "...",
  "source_anchor": {"start_step": 0, "end_step": 0},
  "execution_anchor": {"start_ordinal": 0, "end_ordinal": 2},
  "anchor_kind": "visible_state | action_semantics | route | overlay_lifecycle | terminal_outcome",
  "one_to_one": false,
  "pairwise_alignment_claimed": false,
  "source_screenshot_paths": [],
  "execution_screenshot_paths": [],
  "source_oracle_check": {},
  "source_checklist_baseline": {},
  "source_step_integrity": {},
  "start_viewport_check": {},
  "whole_viewport_scan": {},
  "whole_viewport_scale_density_scan": {},
  "action_surface_parity_check": {},
  "overlay_structural_parity_check": {},
  "progressive_surface_lifecycle_check": {},
  "parent_projection_contamination_check": {},
  "strict_human_equivalence": {},
  "source_host_semantic_check": {},
  "selected_state_lifecycle": {},
  "source_surface_scope": {},
  "autocomplete_surface_check": {},
  "action_host_containment_check": {},
  "page_action_overflow_scan": {},
  "page_structural_audit": {},
  "off_path_surface_audit": {},
  "stateful_component_check": {},
  "transition_projection": {},
  "active_resource_binding_check": {},
  "contamination_check": {},
  "terminal_contract_check": {},
  "root_cause_classification": {},
  "expected": "...",
  "observed": "...",
  "first_blocker": {}
}
```

Generic notes such as `replay passed`, `route reached`, `selected visible`, `option visible`, `sprite ok`, or `terminal reached` are not evidence. Quote the actual generated visible text/value/state or attach DOM/runtime assertions.

`review_pass_candidate` is invalid when `source_checklist_baseline.status!=pass`, the baseline is stale or trace-incomplete, `source_step_integrity.status!=pass`, its checked count does not cover every kept step, a retained step 0 was not reviewed, marked-source-only provenance is false, or any judged row contains a non-empty `whole_viewport_scan.mismatches`, non-empty `whole_viewport_scale_density_scan.mismatches`, `whole_viewport_scale_density_scan.scale_density_ok=false`, non-empty `action_surface_parity_check.mismatches`, `action_surface_parity_check.surface_layout_ok=false`, `action_surface_parity_check.surface_visual_ok=false`, `action_surface_parity_check.surface_content_ok=false`, non-empty `overlay_structural_parity_check.mismatches`, `overlay_structural_parity_check.same_overlay_semantics=false`, non-empty `progressive_surface_lifecycle_check.mismatches`, `progressive_surface_lifecycle_check.lifecycle_ok=false`, non-empty `parent_projection_contamination_check.mismatches`, `parent_projection_contamination_check.projection_ok=false`, `page_structural_audit.mismatches`, non-empty `off_path_surface_audit.mismatches`, `active_resource_binding_check.binding_ok=false`, a failing `terminal_contract_check`, `source_surface_scope.surface_scope_ok=false`, or `root_cause_classification.class` outside a documented non-blocking same-page variant. Do not write row `status=pass` while preserving mismatch arrays for later interpretation. Either classify the mismatch as non-blocking with concrete evidence that it does not affect page identity, source hosts, action set, resources, layout, or task orientation, or return a blocker.

## False-Pass Self-Audit Before Pass

Before returning `review_pass_candidate`, answer these questions from current live artifacts, not old narrative:

- Would a human fail the row because the visible first viewport is too large, too sparse, missing required app chrome/viewport containment, or reaches a much shallower section than SOURCE? If yes, block as `layout_content_variant`.
- Is the clicked action host inside the same owning surface as SOURCE, with matching layout, content, sibling controls, selected/default state, density, and resource binding? If no, block as `action_surface_mismatch`, even when the node is clickable.
- Does the current row already show an effect that belongs to a later action, or does the parent page show a sibling/future chip, price, filter, ticket tier, selected state, result count, or CTA mode too early? If yes, block as lifecycle/projection contamination.
- For pages with visible media, backgrounds, posters, cards, maps, avatars, or picker thumbnails, do active DOM resources come from the page's formal page-realization projection/input manifest plus a valid `resource_resolution` ledger, load successfully, and match the source-visible surface count/order? If no, block as `active_resource_binding_mismatch` or `pipeline_resource_boundary_gap` after ladder exhaustion.
- Do static/formal DOM audit and runtime/browser evidence agree? A host that exists only in JS string templates, stale debug sidecars, replay-only context, hidden duplicates, or action-id maps is not enough.
- Did the issue get reported after an earlier pass packet? Treat it as a false-pass correction: record the stale packet path, reframe acceptance as positive visible conditions, and require a fresh execution round from `execute-trace`.

If any answer is unknown, return `evidence_blocked`; do not pass from route reachability, terminal success, or actionability alone.

## Mandatory Review Dimensions

### Source Step Integrity

Source correctness is reviewed inside the per-trace review packet, not in a separate stage.

Before comparing generated execution, read the obligation checklist row and the complete marked source sequence. The checklist supplies independent expected/forbidden state obligations; the current source sequence verifies that the admitted step descriptions and screenshots still satisfy them. Then fill:

A `ready` checklist may record a `step_description_disagreement`: the step description disagrees with the marked evidence, but the obligation was written from the visual evidence. Review judges against the obligation, not the step description, and copies the disagreement into the packet as information. Return `reference_oracle_blocker` only when the source evidence itself cannot serve as the oracle for a row.

```json
{
  "source_step_integrity": {
    "status": "pass | fail | blocked",
    "kept_step_count": 0,
    "checked_step_count": 0,
    "step_zero_retained": true,
    "step_zero_checked": true,
    "marked_reference_only": true,
    "failed_step_indices": [],
    "step_description_disagreements": [],
    "rows": [
      {
        "step_index": 0,
        "pre_action_source_host": "pass | fail | blocked",
        "marked_target_alignment": "pass | fail | blocked",
        "source_host_not_destination": "pass | fail | blocked",
        "next_state_leak_absent": "pass | fail | blocked",
        "post_action_effect": "pass | committed_effect_unobserved | fail | blocked",
        "instruction_trace_alignment": "pass | fail | blocked",
        "terminal_dependency_explicit": "pass | fail | blocked",
        "baseline_step_index": 0,
        "baseline_obligations_checked": [
          "pre_action_expected_visible_state",
          "pre_action_forbidden_future_state",
          "progressive_lifecycle",
          "input_or_autocomplete",
          "overlay_or_parent_projection",
          "viewport_containment",
          "terminal_contract"
        ],
        "evidence": {
          "marked_reference_screenshot": "...",
          "semantic_action": "...",
          "current_visible_source_host": "...",
          "next_reference_or_terminal_effect": "...",
          "instruction_value": "..."
        }
      }
    ]
  }
}
```

All row checks must pass for every kept step, except that the terminal row's `post_action_effect` may be `committed_effect_unobserved` when its `terminal_contract` records that commit status. Do not start at step 1; retained step 0 is mandatory. Use the marked screenshots rendered by `build-obligations` from the recorded actions; a raw screenshot without the mark is not the oracle. If the current step description names a destination control, borrows next-row text/state, omits an Agent-visible terminal value, or points away from the marked current source host, return `reference_oracle_blocker` before environment parity can pass.

### Terminal Contract

The terminal row of the checklist carries the trace's `terminal_contract`: `commit_status`, `observed_boundary`, `outcome_fields`, `required_state`, `forbidden_state`, `success_relation`, and `remaining_uncertainty`. Review judges the terminal row against it and never widens it.

- `effect_observed`: the source shows the settled effect. After the final action the execution must show the same entity and values named in `outcome_fields`, satisfy `required_state`, and show none of `forbidden_state`.
- `committed_effect_unobserved`: the marked visible source host, the instruction-named target and value, and the issued click, commit, or gesture are unambiguous, but the source does not settle the later effect (a missing, stale, non-settled, or visibly unchanged final observation). This covers navigation/open, tab and scroll gestures, and instruction-defined submit/publish actions. Source and environment review prove only natural reachability and actual action issuance; do not require the unrecorded destination, UI transition, or business result. The expected effect stays in `remaining_uncertainty` and the row's `post_action_expected_effect` is empty. A captured final frame that merely remains in the unambiguous pre-action state after the marked terminal tap is this class, not a contradictory oracle.
- `precommit`: the trace stops before the commit (for example before a payment completes). The execution must reach `observed_boundary` and must not show any state in `forbidden_state`.

Rules for every class:

- Never copy an unchanged or failed source terminal state into the package merely because it is captured, and never require or invent an effect the source does not show. An unobserved effect is never source or environment closure evidence.
- The terminal contract never authorizes real external side effects: payment or order placement, destructive or irreversible behavior, or real-account privacy and security changes.
- The current pure-GUI execution must actually perform the final action on the visible host; a hidden evaluator-only write satisfies no terminal class.
- A changed wrong entity or value, a conflicting instruction, or a different committed outcome in the source is a contradiction, not `committed_effect_unobserved`.
- If the generated visible host or action is missing, return `generated_env_mismatch`; if host, target, value, or issued action is ambiguous, return `reference_oracle_blocker` (source) or `evidence_blocked` (execution).
- The evaluator-visible consequence is checked by `probe-contract` on the same environment digest, not by review.

Required packet object:

```json
{
  "terminal_contract_check": {
    "step_index": 8,
    "commit_status": "effect_observed | committed_effect_unobserved | precommit",
    "observed_boundary": "...",
    "source_host_and_target_unambiguous": true,
    "final_action_issued_on_visible_host": true,
    "observed_outcome_fields": ["entity and values the execution shows"],
    "required_state_observed": true,
    "forbidden_state_absent": true,
    "effect_claimed_beyond_source": false,
    "real_external_side_effect": false,
    "execution_evidence": "..."
  }
}
```

After any repair that intersects this trace, the prior execution and review are stale even if the visible UI did not change: restart execution at step 0 in a new round and review the complete trace again. A suffix-only review cannot become `review_pass_candidate`.

### Pure-GUI Execution Evidence

Consume the chronological evidence produced by `execute-trace` from the reset entry of the same package revision, task, viewport, and shell. Do not drive the browser from review, load a later state, or extend execution after a blocker.

`execution_complete` only proves a human-aligned browser lifecycle reached the end. It does not prove visual fidelity, source-host identity, selected-state lifecycle, overlay parity, or instrumentation correctness. `stopped_at_first_blocker` preserves a reviewable prefix but cannot support full closure.

### Page Structural Audit

For every page identity touched by the active trace, record from the execution packet and its raw log (review never reloads, clicks, or measures the live page; an item the evidence does not cover is `evidence_blocked`):

- back/close/up-level controls are clickable or explicitly noninteractive with a reason;
- no visible action host overflows the generated shell;
- the reset entry of the execution (fresh state) renders the expected chrome;
- page errors are captured and either absent or listed as blockers;
- visible fixed/FAB/CTA/overlay controls are within the shell.

### Off-Path Surface Audit

Enumerate visible interactive-looking controls, including `a[href]`, `button`, `[role=button]`, `[tabindex]`, `[onclick]`, `[data-element-id]`, `[data-action-id]`, list/card rows, suggestion rows, and natural card hit areas.

Classify each as:

- `source_path_action`;
- `semantic_navigation`;
- `semantic_inline_state`;
- `semantic_state_mutation`;
- `non_task_static`.

If a visible control is interactive, its behavior must match visible semantics and page contracts. If correct behavior cannot be proven, report it; the repair makes it explicitly noninteractive instead of leaving an arbitrary fallback route, silent no-op, or sibling trace target.

### Stateful Component Check

For stateful controls, record the component class and expected invariant:

- `single_select`: switching clears the previous selection;
- `multi_select`: toggles are independent unless source/contract shows select-all;
- `collection`: CRUD preserves unrelated records and re-open reflects saved state;
- `query_projection`: typing changes the visible suggestions/results without a wrong route split;
- `draft_commit`: draft input does not mutate collection before save, save commits intended record, cancel/back preserves previous state;
- `mutation_projection`: add/remove/favorite/submit/save projects into visible DOM, not only storage or SDK return.

Stateful controls cover the initial, selected, alternative/deselected, repeated, and reset states across every visible mirror of the same state (for example a header chip, the list row, a card badge, and a footer summary). One selected mirror updating while a sibling mirror stays stale fails lifecycle review.

### Visual Fidelity And Whole Viewport

Read the complete source sprite sequence first, then the complete observed execution sprite sequence. Use explicit semantic/range anchors when relating them. Do not alternate cards or treat numeric indices as a one-to-one mapping. If an effect appears too early in the execution lifecycle relative to the source sequence, mark `fail_first` at the earliest defensible execution anchor.

Scan the full viewport before narrowing to the clicked host: header icons, search bars, tabs, bottom nav, page chrome, open sheets/panels, visible list/feed/card content, fixed CTAs, and stale overlays.

### Phone Frame Non-Mismatch Rule

Do not classify the outer decorative phone frame, bezel, notch shape, border thickness, or background outside the app viewport as an environment mismatch by itself. These are review context, not the task surface. Keep reviewing the app inside the retained shell: source hosts, overlays, bottom bars, CTAs, scroll containers, and visible content must remain contained, unclipped, and semantically equivalent. If a button, footer, sheet, modal, or task content overflows or is clipped by the app shell, that remains `page_action_component_overflow`, `layout_content_variant`, or the relevant surface mismatch. Do not remove the phone shell as a fix for visual differences.

In the released packages the browser viewport is 430x884 (`browser_viewport` in the package `resource_manifest.json`). Most pages render a fixed 390x844 app shell inside it, but some use a fluid container (for example an `.app-shell` with `max-width: 430px` and `min-height: 100vh`), so read the shell rectangle from the rendered page (the bounding box of the outermost app container) rather than assuming one size. Judge containment against that shell: footers, fixed CTAs, modals, and sheets must lie inside it, and a task surface clipped by it is a mismatch. Record `footer_or_cta_inside_shell` and `modal_or_sheet_inside_shell` in `action_host_containment_check`.

Record first-viewport scale/density before narrowing to the clicked host. For scrollable/listing/channel/feed pages, fill `whole_viewport_scale_density_scan`:

```json
{
  "required": true,
  "source_deepest_visible_content": "SOURCE first viewport reaches 热映电影 first card / filter bar / list row / equivalent later section",
  "generated_deepest_visible_content": "Generated first viewport reaches the same semantic depth",
  "source_key_rects": {"action_card": {"top": 0, "bottom": 0}, "next_section": {"top": 0, "bottom": 0}},
  "generated_key_rects": {"action_card": {"top": 0, "bottom": 0}, "next_section": {"top": 0, "bottom": 0}},
  "source_anchor_surfaces": [{"name": "carbon banner / hero / map / card", "top": 0, "bottom": 0, "crop_origin": "top|center|bottom|unknown"}],
  "generated_anchor_surfaces": [{"name": "carbon banner / hero / map / card", "top": 0, "bottom": 0, "crop_origin": "top|center|bottom|unknown"}],
  "anchor_crop_ok": true,
  "scale_density_ok": true,
  "mismatches": [],
  "rationale": "..."
}
```

If the source first viewport exposes a later task-orientation section, first row, filter bar, hot movie card, or comparable downstream content while the generated viewport only reaches an earlier banner/category/module because header, hero, cards, media, or spacing are oversized, classify it as `layout_content_variant`. A correct route, correct action host, and natural clickability do not override this failure. Do not call it a harmless visual difference unless the packet proves the hidden/truncated content is not part of page identity, task orientation, available actions, resource surfaces, or source-visible layout.

For image-backed maps, route previews, hero backgrounds, banners, and other large resource surfaces, compare vertical anchor/crop parity explicitly. A generated viewport fails when the same banner/card/text is shifted into the wrong region, duplicated by a full-screen resource crop plus a generated component, or anchored from the wrong part of a tall resource. Example: a SOURCE row shows the carbon banner centered between the map and order card, while generated crops that banner into the top map area and renders a second banner below. This is `layout_content_variant` or `active_resource_binding_mismatch`, not a pass. Fixers should repair resource crop/position or component duplication, but reviewers must first record the mismatch in `whole_viewport_scale_density_scan.mismatches`.

For the first kept row, the active trace's own `action_0000` source screenshot is the start-viewport oracle. A canonical page layout realized from a sibling trace is not sufficient when the active trace's start viewport differs. Extra or missing top banners, shifted/cropped category grids, biased carousel positions, missing required app chrome/viewport containment, wrong bottom/header state, or visible feed/card differences are blocking `layout_content_variant` issues unless a packet proves they are background-only and not consumed by any action, resource surface, or task orientation. Outer phone-frame/bezel cosmetic variance alone is not a blocker.

For route rows whose target filename is shared by multiple source actions, compare the source action label and the post-action source viewport (`obs_{i+1}`) against all same-target siblings used by the generated page. If a category-grid action such as `演唱会` and a content-module action such as `必看演出 / 全部 >` both target the same generated filename but their source post-route pages differ in header, hero/banner, selected tab, filter/action set, visible cards, or resources, return `page_boundary_blocker` or `generated_env_mismatch`; do not pass from route reachability or a correct clicked host.

If `whole_viewport_scan` or `whole_viewport_scale_density_scan` records mismatches in visible banners, channel tabs, list/card content, card order, resource thumbnails, section reach, spacing, card/media scale, resource crop/anchor, duplicated visual bands, or off-path controls, those mismatches must become the row's `first_blocker` unless the row also records why each mismatch is a same-page variant off the action surface. A blanket statement such as `final sprites show action hosts inside the generated phone shell` or `route reached and target clickable` is not a whole-viewport review.

### Action Surface Parity

For every current-step action host or component related to it, review the owning surface before accepting the child host. Fill `action_surface_parity_check`:

```json
{
  "required": true,
  "source_surface_kind": "card_row | list_section | carousel | filter_panel | calendar_picker | modal | sheet | dropdown | header_search | footer_cta_group | other",
  "generated_surface_kind": "card_row | list_section | carousel | filter_panel | calendar_picker | modal | sheet | dropdown | header_search | footer_cta_group | other",
  "source_surface_role": "surface that contains and contextualizes the clicked component",
  "generated_surface_role": "surface that contains and contextualizes the clicked component",
  "source_surface_rect": {"top": 0, "bottom": 0, "height": 0},
  "generated_surface_rect": {"top": 0, "bottom": 0, "height": 0},
  "source_visible_content": ["section title", "sibling controls", "option group", "card/list neighbors", "media/resource labels"],
  "generated_visible_content": ["..."],
  "surface_layout_ok": true,
  "surface_visual_ok": true,
  "surface_content_ok": true,
  "surface_resource_binding_ok": true,
  "clicked_host_inside_matching_surface": true,
  "mismatches": [],
  "rationale": "..."
}
```

The clicked node is not the surface. A visible, enabled, naturally hittable child button/option/card is insufficient when its owning surface differs from SOURCE in layout, visual form, content, sibling options, media/resources, selected/default state, or density. Examples: a `24` date button in a compact bottom date strip does not pass a SOURCE full calendar panel; a card CTA in the wrong list section does not pass a SOURCE card row; a carousel item in a shifted/cropped category surface does not pass the SOURCE category grid. Return `generated_env_mismatch` with `action_surface_mismatch` before source-host/actionability acceptance.

### Overlay, Sheet, Modal, Calendar, Filter, And Sort Parity

When the source current row shows a visible popup/sheet/modal/calendar/picker/filter panel/dropdown/sort menu, fill `overlay_structural_parity_check` before source-host acceptance:

```json
{
  "required": true,
  "source_overlay_type": "full_calendar_panel | bottom_sheet | modal | dropdown | filter_panel | sort_menu | picker",
  "generated_overlay_type": "full_calendar_panel | bottom_sheet | modal | dropdown | filter_panel | sort_menu | picker",
  "source_coverage": {"top": 0, "bottom": 0, "height": 0},
  "generated_coverage": {"top": 0, "bottom": 0, "height": 0},
  "source_visible_controls": ["全部时间", "一周内", "一月内", "本周末", "2026/01", "重置", "确定"],
  "generated_visible_controls": ["..."],
  "source_default_or_selected_state": "...",
  "generated_default_or_selected_state": "...",
  "same_overlay_semantics": true,
  "mismatches": [],
  "rationale": "..."
}
```

A matching clicked option is not enough. If SOURCE shows a full calendar panel with month grid, range chips, reset/confirm controls, and page backdrop, but the generated page shows only a compact bottom date strip or otherwise lacks the same panel structure, return `generated_env_mismatch` with `overlay_structural_mismatch` or `calendar_surface_mismatch`. Do not accept because the `24` date button is visible, enabled, and routes/updates correctly.

### Parent Projection Contamination

For any stateful confirm/apply action that closes a sheet, modal, calendar, picker, filter panel, dropdown, or sort menu and projects values back to the parent page, fill `parent_projection_contamination_check`:

```json
{
  "required": true,
  "trigger_step": 4,
  "parent_surface": "category-channel-concerts.html filter bar / applied tag row",
  "expected_parent_projection": ["1月24日 date tag after date confirm"],
  "forbidden_parent_projection": ["300-500 price tag before price selection/confirm"],
  "observed_parent_projection": ["1月24日"],
  "projection_ok": true,
  "mismatches": [],
  "rationale": "..."
}
```

Positive projection is not enough. The review must also assert the absence of sibling or future-step state that has not been produced yet. Examples that block pass: after confirming a date picker, the parent page already shows a price tag; after choosing one filter group, another unselected group appears selected; after submitting a draft, unrelated saved items or result counts change. Return `generated_env_mismatch` with `premature_parent_projection` or `sibling_state_contamination` even when the current clicked host, selected option, and route/replay are otherwise correct.

### Progressive Surface Lifecycle

For any same-page multi-stage selection or progressive reveal flow, fill `progressive_surface_lifecycle_check`:

```json
{
  "required": true,
  "trigger_step": 4,
  "source_surface": "ticket-selection page / showtime section",
  "expected_current_surfaces": ["showtime section only before selecting a session"],
  "forbidden_future_surfaces": ["ticket tier section", "100.0元 selected state", "full-width 去选座 footer before the reveal action"],
  "observed_current_surfaces": ["showtime section only"],
  "lifecycle_ok": true,
  "mismatches": [],
  "rationale": "..."
}
```

Actionability and route success are insufficient here. If a later-step section, option group, selected card/tier, result count, footer CTA mode, or sibling filter surface appears in the current row before the action that should reveal/select it, return `generated_env_mismatch` with `premature_progressive_surface` or `future_state_contamination`.

## Active Resource Binding

For any visible image/media grid, picker, carousel, album, product/media card, thumbnail strip, avatar/feed image, or other resource-backed surface, fill:

```json
{
  "required": true,
  "surface": "publish-media-picker.html",
  "resource_projection_path": ".../phase_pagegen/projections/pagegen_resources/<page>.json",
  "actual_pagegen_input_manifest_path": ".../phase_pagegen/page_inputs/<page>/actual_pagegen_input_manifest.json",
  "resource_visualization_path": null,
  "active_dom_resource_count": 0,
  "expected_visible_media_count": 0,
  "dom_resource_refs": [],
  "allowed_active_resource_refs": [],
  "forbidden_resource_refs": [],
  "oracle_only_resource_refs": [],
  "auxiliary_sprite_refs": [],
  "css_or_placeholder_tile_count": 0,
  "broken_image_count": 0,
  "legitimate_trace_extracted_resource_present": true,
  "pipeline_resource_gap": false,
  "resource_resolution": {
    "resolution_class": "matching_extracted_resource | source_derived_fallback | external_semantic_resource_fallback | semantic_placeholder_fallback",
    "semantic_identity": "...",
    "visual_role": "...",
    "identity_bearing": false,
    "extraction_attempts": [],
    "rejected_candidates": [],
    "selected_source": {},
    "source_crop_provenance": {},
    "identity_claimed": false,
    "task_identity_dependency": false,
    "unauthorized_reference_asset_reuse_absent": true,
    "active_resource_binding_satisfied": true,
    "degraded_nonblocking": false,
    "strict_visual_fidelity_satisfied": true
  },
  "binding_ok": true,
  "rationale": "..."
}
```

This check is separate from contamination. A fully audited minimal clean source crop is allowed only after matching extraction fails; full pages, sprites, marked/unclean/context-leaking/unprovenanced crops remain contamination. Active resource binding also requires a legitimate semantic resource for the visible surface.

Acceptance rules:

- Apply `matching_extracted_resource -> source_derived_fallback -> external_semantic_resource_fallback -> semantic_placeholder_fallback`; require extraction/candidate rejection evidence before accepting a fallback.
- An audited minimal clean source crop may pass with source trace/step/path/hash, output hash, bbox, semantic identity, and all crop-integrity assertions. Full pages, sprites, marked/unclean crops, unrelated UI/chrome, and future-state leakage remain forbidden.
- If no faithful crop exists, use a provenance-backed official/open-license/provider-generated external semantic resource, then a role-matching Lucide/Iconify/CSS/neutral placeholder. The package materializes it and records origin/usage/path/hash/semantic role.
- An identity-bearing same-category substitute may be `degraded_nonblocking=true` only when exact pixels/identity are not a task value, source-host discriminator, checkpoint/evaluator input, or page/route identity dependency. It must set `identity_claimed=false`; otherwise it remains `active_resource_binding_mismatch`.
- A known wrong sibling/entity extraction always fails binding. Prefer a neutral semantic placeholder to a false product/person/video/poster/map identity.
- Return `pipeline_resource_boundary_gap` only after all four classes are exhausted or when exact identity is task-critical and no exact legitimate resource exists. Do not return it solely because a non-task-critical surface uses a declared degraded semantic substitute.
- The review packet should include or request a resource visualization for any disputed resource issue: active DOM resources, oracle-only extracted resources, sprite sheets, and omitted resources must be visually separated.
- A negative contamination check is not a positive resource binding check. The packet must compare active DOM resource refs, visible card/media count, rough order, alt/text association, resolution class/provenance, and the allowed active resource set for the current page/trace. If generated cards use legitimate-looking resources from a sibling page or sibling trace but not the active source surface, return `active_resource_binding_mismatch` or `page_boundary_gap`.
- When a generated page is shared by multiple route sources, resource binding must be evaluated per source context. The fact that one trace's resources fit the shared page does not pass a different trace whose source viewport shows a distinct banner, card set, artist strip, theater/play poster set, or event list.

## Step-State Oracle Consistency

Before accepting a row, compare the canonical `action_id`/`operation_intent` with the row's derived `expected_state_diff`, state-delta signatures, runtime binding payload, and DOM action host. If the visible action says one thing but the derived state/effect belongs to another step or later surface, return `generated_env_mismatch` with `stale_derived_oracle_sync_required` or `reference_oracle_blocker` when the source evidence itself is wrong.

Examples that must block pass:

- `step0` visible source host is `必看演出 / 全部 >`, but a formal consumer attaches `listing_header_city_selector/ui_expansion/重庆` to the same `action_id`;
- a category entry click is reviewed as if it were a module `全部 >` click because both currently target the same filename;
- an opened modal/sheet option is accepted from a same-text header or parent-page label.

## Root Cause Classification

Before sending a blocker to a fixer, classify the first mismatch:

```json
{
  "root_cause_classification": {
    "required": true,
    "class": "layout_content_variant | component_conflict | page_boundary_gap | source_host_mismatch | lifecycle_mismatch | actionability_mismatch | active_resource_binding_mismatch | pipeline_resource_boundary_gap | autocomplete_projection_gap | panel_data_source_gap | instrumentation_backedge | reference_oracle_blocker",
    "owner": "implementation | semantics",
    "repairable": true,
    "rationale": "..."
  }
}
```

Map classes to owners: `page_boundary_gap`, `reference_oracle_blocker`, and `pipeline_resource_boundary_gap` are `semantics` (not repairable inside the loop); every other class is `implementation`, except `instrumentation_backedge`: review records it, and `probe-contract` attributes it to `supervision` on the same digest (review never assigns `supervision`). Do not turn ordinary background content variance into a component conflict. Do not patch visible text to hide an action-host identity conflict. A mismatch is `semantics` only when closing it needs a page identity, state or data relation, guard, or effect that no source evidence supports; a supported page, lifecycle, action set, or runtime projection that the package realizes wrongly is `implementation`.

## Selected-State Lifecycle

For every select/switch/check/uncheck/filter/tab/channel/picker/sheet/date/passenger/station action, fill:

```json
{
  "required": true,
  "before_step_selected": false,
  "action_target_value": "...",
  "selected_state_source": "aria-selected | aria-checked | checked | selected class | computed style | check mark | underline",
  "after_step_selected": true,
  "parent_projection": "...",
  "premature_effect_absent": true,
  "rationale": "..."
}
```

The row fails if the generated current row already shows the target as selected while the displayed action is to select it, unless the source current row proves it was already selected and the action is a confirm/no-op/back/close.

## Source Surface Scope

For every interactive/stateful row, fill:

```json
{
  "required": true,
  "source_surface_expected": "header_or_tab_bar | opened_overlay_option | sheet_option | modal_control | picker_row | list_row | card | input | footer_cta",
  "source_surface_observed": "...",
  "target_value_elsewhere_current_row": false,
  "surface_scope_ok": true,
  "rationale": "..."
}
```

The same text/value on the wrong surface is not evidence. A target value rendered as a header chip or parent-page label before the action is a lifecycle/surface failure when the source expects it inside an opened panel, sheet, modal, picker, list row, or card.

## Autocomplete And Same-Page Query Projection

When the source shows suggestions after focus/input, fill:

```json
{
  "required": true,
  "query_value": "...",
  "same_page_projection": true,
  "state_store": "url_query | runtime_store | input_value | session_state | other",
  "suggestion_surface_visible": true,
  "candidate_count_expected": 0,
  "candidate_count_observed": 0,
  "candidate_text_order_match": true,
  "candidate_shape_match": true,
  "source_surface_for_click": "search_entry_suggestion_surface",
  "mis_split_static_results_page": false,
  "default_or_background_content_visibility_ok": true,
  "direct_render_checked": true,
  "natural_click_or_submit_target_verified": true,
  "rationale": "..."
}
```

Do not pass from `fill`, submitted-route success, or terminal reachability when the source-visible suggestion surface is missing, empty, stale, visibly sparse, or materialized as a wrong static results/suggestions page.

If the same generated page or runtime contract serves more than one query/focus value, the review packet must include `cross_trace_runtime_projection_audit` before returning `review_pass_candidate` for a fix touching that shared surface:

```json
{
  "required": true,
  "surface_page": "search-landing.html",
  "enumeration_sources": ["query_state_contract", "action_effect_contracts", "route_execution_contracts", "runtime_binding_manifest", "materialized_observation_bindings", "run_management/review"],
  "checked_traces": [
    {
      "trace_id": "...",
      "step_index": 0,
      "query_or_value": "...",
      "expected_candidates": ["..."],
      "target_action": "...",
      "default_content_hidden_or_visible_as_ref": true,
      "direct_screenshot": "...",
      "panel_screenshot": "...",
      "natural_click_or_submit_verified": true,
      "verdict": "pass | fail"
    }
  ],
  "rationale": "..."
}
```

The packet is `evidence_blocked` if it only checks the originally reported trace while other traces share the same page/contract surface.

## Action Host Containment

For each current-step action host, from the measurements `execute-trace` recorded:

- canonical host and shell/body/app wrapper bounding boxes;
- assert `center_inside_shell=true`;
- assert `elementFromPoint(center)` resolves to the same nearest action host;
- for buttons, FABs, fixed/sticky CTAs, overlay primary actions, tabs, and icon-only controls, assert `full_bbox_inside_shell=true`;
- record `overflow_edges=[]` and masking/cover checks.

Visible fixed/FAB/CTA/overlay actions that protrude past the generated phone shell fail the page even if the current trace does not click them.

## Overlay And Backdrop Actionability

Classify layers before accepting sheet/modal/filter/picker interactions:

- `backdrop`: blocks background or closes surface; it is not the active source surface.
- `active_surface`: visible sheet/modal/dialog/popover containing task controls.
- `background`: page content behind backdrop.

A repair fixes layer classification, action-host authority, and event timing; it never whitelists task ids through `masked_by_active_layer`. A task control inside the active surface must be naturally clickable and must not leave a visible noninteractive notice after click.

## Contamination Check

Generated UI must not paste a full source page, mount a sprite, render the action marks, include unrelated page/system chrome or future-state pixels, or use an unprovenanced source-derived asset. A source-root path/hash match is not automatic failure for a compliant `source_derived_fallback`; validate its hashes, bbox, semantic identity, extraction evidence, and clean-crop assertions.

A new label that displays an evaluator or constraint verdict is answer leakage: fail the review even when the reward is correct.

## False-Pass Pressure Cases

The review packet must block these cases:

- missing app viewport containment or clipped task affordance -> `page_action_component_overflow`; outer phone bezel/notch/border cosmetic variance alone is not a blocker;
- autocomplete surface mismatch after typing query -> `autocomplete_surface_mismatch`;
- visible Sanya search results mismatch -> `content_entity_mismatch` or `page_boundary_blocker`;
- click host is left-top `全部` while source expects top-right `问一问` -> `source_host_semantic_mismatch`;
- filter panel starts with the target selected before the selecting action -> `selected_state_lifecycle_failure`;
- route reaches search results but source query/suggestion state is wrong -> `autocomplete_surface_mismatch` or `page_boundary_blocker`;
- required Xiaohongshu note `这哪是魔王啊，这是天使！` missing -> `content_entity_mismatch`.
- Xiaohongshu `publish-media-picker.html` shows a media grid but active DOM uses known wrong sibling/entity assets, lacks a valid resolution ledger, pastes a full/marked/unclean source asset, or makes a false exact-identity claim for a generic placeholder -> `active_resource_binding_mismatch`, `resource_contamination_or_wrong_boundary`, or `pipeline_resource_boundary_gap` after ladder exhaustion.
- Damai category/home/channel pages whose source first viewport is compact enough to reveal later sections/cards, while generated only reaches a banner/category/module because of oversized layout -> `layout_content_variant`.
- Damai date/filter/ticket/calendar rows where the clicked button exists but the surrounding calendar, filter sheet, ticket section, or card/list surface differs materially from SOURCE -> `action_surface_mismatch` / `overlay_structural_mismatch`.
- Damai parent pages where selecting one filter/date/session leaks a later price tier, ticket tier, result count, or selected state before that later action -> `premature_parent_projection` / `future_state_contamination`.
- Damai generated pages that mount omitted page-realization resources, wrong sibling/entity resources, full/marked/unclean or unprovenanced source assets, or unprovenanced/role-mismatched online assets as active DOM media -> `active_resource_binding_mismatch` / `resource_contamination_or_wrong_boundary`.
- Damai action hosts that are browser-visible only after JS template insertion but absent from static/formal DOM contract, or whose stale debug sidecars still report missing required ids -> `evidence_blocked` until formal audit and live DOM agree.

## Packet Status

Return one status:

- `reference_oracle_blocker`;
- `page_boundary_blocker`;
- `generated_env_mismatch`;
- `evidence_blocked`;
- `review_pass_candidate`.

`review_pass_candidate` is allowed only when `execution_status=execution_complete`, both ordered sequence manifests are present, `pairwise_alignment_claimed=false`, and all required evidence fields are concrete, current, and non-contradictory. It is not validation: `probe-contract` runs next on the same environment digest.
It additionally requires `source_step_integrity.status=pass`, complete kept-step coverage, retained step-0 coverage, and marked screenshots rendered from the recorded actions.
