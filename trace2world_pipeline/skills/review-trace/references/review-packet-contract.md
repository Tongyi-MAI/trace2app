# Review Packet Contract

The fine-grained gates of `Review(R, X, O)` in Algorithm 4 (paper Appendix B.2; condensed prompt in
Appendix G, "Construction-time source-trace validation"). Review compares one frozen execution
against its source trace and the trace's obligation checklist, and writes one packet.

## Boundary

Review is independent and read-only. It reads the frozen evidence packet (the execution packet, its
screenshots and recorded measurements, the marked source screenshots, and the checklist) and may
read package files without running them. It never drives the browser, reloads a page, extends a
stopped execution, or edits the trace, checklist, execution, or environment, and it never repairs.
When a judgment needs evidence the packet does not hold, the verdict is `evidence_blocked` with the
missing evidence named; `execute-trace` captures it in a new round.

## Inputs and preflight

1. **Obligation baseline.** The per-trace checklist from `build-obligations`, status `ready`, with a
   `trajectory_sha256` equal to the current trajectory. A missing or stale checklist is
   `evidence_blocked`; rebuild it from the current trace. Rebuilding an oracle is not a pass.
2. **Execution packet.** A current `trace2world.gui_execution.packet.v1` on the package revision
   under validation, `pure_gui_execution=true`, and no shortcut flag. `execution_invalid`, missing
   screenshots, or another revision is `evidence_blocked`. `stopped_at_first_blocker` is reviewed as
   a prefix plus the blocker and cannot yield a full-trace pass.
3. **Sprite.** One source-then-execution sprite built by `scripts/build_review_sprite.py`
   (`sequence-sprite-review.md`). Two separate sprites or paginated pages are `evidence_blocked`.
4. **Step 0.** If the trace retains step 0, the packet reviews it. A packet that skips it is
   `evidence_blocked`.

Element ids, action ids, and other generated attributes may be read as diagnostics after the
fact. They must not have chosen or dispatched any action in the execution
(`execute-trace/references/pure-gui-execution-contract.md`).

## Reading order

Read the complete source sequence before the execution. Compare causal states, not equal row
indices: each source row is the state before its action, and the next row or the terminal evidence
establishes the intended effect. Relate the sequences only through explicit range anchors
(`sequence-sprite-review.md`); valid alternative paths and unequal lengths are allowed.

## Source step integrity

Before judging the execution, confirm that each kept source row can serve as the oracle. For every
kept row, including step 0:

| Check | Passes when |
|---|---|
| `pre_action_source_host` | the checklist's source host is visible in the marked screenshot before the action |
| `marked_target_alignment` | the mark sits on that host |
| `source_host_not_destination` | the host is not a control that appears only after the action |
| `next_state_leak_absent` | the row's state shows nothing that belongs to a later action |
| `post_action_effect` | the next row or terminal evidence shows the recorded effect; on the terminal row, `commit_only` when the terminal contract says `committed_effect_unobserved` |
| `instruction_trace_alignment` | the row serves the instruction's entity and values |
| `terminal_dependency_explicit` | the terminal row names its outcome fields and success relation |

A recorded `step_description_disagreement` is copied into the packet as information; the row is
judged against the obligation written from the marked evidence. When the source evidence itself
cannot serve as the oracle for a row (ambiguous host, target, value, or order), the verdict is
`reference_oracle_blocker`.

## Execution checks

Check every judged anchor along these dimensions, scanning the whole viewport before narrowing to
the tapped host. Quote the observed visible text, value, or state, or cite a recorded measurement.
Notes such as "route reached", "option visible", or "terminal reached" are not evidence.

### Whole viewport (`whole_viewport_scan`)

- **Route and start data.** For the first kept row, the trace's own step-0 screenshot is the start
  oracle; a page layout realized from a sibling trace is not enough when this trace's start
  viewport differs.
- **Chrome and tabs.** Header icons, search bars, selected tabs, bottom navigation.
- **Overlays.** Open sheets, panels, dialogs, and stale overlays.
- **List content.** Visible cards, rows, and their order and entities.
- **Density, scale, anchor, and crop origin.** The first viewport reaches the same semantic depth as
  the source (the same first card, filter bar, or section). Oversized headers, media, or spacing
  that hide content the source shows are `layout_content_variant`. Large resource surfaces (maps,
  banners, hero images) keep the source's vertical anchor and crop; a shifted, duplicated, or
  wrongly anchored surface is a mismatch.

Rendering a source screenshot, a mark, or a sprite as page content is a mismatch. The decorative
device frame outside the app viewport is not part of the task surface and is never a mismatch by
itself; app content clipped or overflowing the viewport still is.

### Action host (`action_surface_parity_check`)

- **Value and host scope.** The executed action targets the source host with the source value,
  inside the same owning surface. The tapped control is not the surface: a matching button inside a
  surface whose layout, sibling options, content, selected state, or density differs from the
  source fails (for example a date button in a compact strip where the source shows a full
  calendar panel). The same text elsewhere on the screen is not evidence.
- **Reachability and enabled state.** The host was reached by visible operations and was enabled
  when used.
- **Containment.** From the execution packet's measurements: the host's center lies inside the app
  viewport, and the element at that point is the host itself. Buttons, fixed or sticky CTAs,
  overlay primary actions, tabs, and icon-only controls lie fully inside the viewport.

### Lifecycle and projection

- **Selected state.** For select, switch, check, filter, tab, picker, and date actions: the target
  is not already selected before the action (unless the source shows it selected and the action
  confirms), and is selected after it.
- **Progressive surfaces** (`progressive_surface_lifecycle_check`). A later section, option group,
  selected tier, result count, or footer mode does not appear before the action that reveals it.
- **Overlay structure.** When the source shows a sheet, modal, calendar, picker, filter panel,
  dropdown, or sort menu, the execution shows the same structure and controls, not just the tapped
  option. Backdrop, active surface, and background are distinguished; a control inside the active
  surface is used there.
- **Parent projection** (`parent_projection_contamination_check`). After a confirm or apply, the
  parent shows the chosen values and nothing from sibling or future steps (for example no price
  tag before the price is chosen).
- **Forbidden future state.** Every `pre_action_forbidden_future_state` of the checklist row is
  absent.

### Scroll region and autocomplete

- The scroll or drag happens in the source's scroll region and reaches the same content.
- After typing, the suggestion surface is present with the source's candidates in the source's
  order and shape, on the same page. A static results page standing in for live suggestions, or a
  missing or sparse suggestion list, is a mismatch even if the submitted route works.

### Terminal obligations

Judge the terminal row by its terminal contract:

- `effect_observed`: the execution shows the same terminal effect with the outcome fields and
  success relation of the contract, and none of its forbidden state.
- `committed_effect_unobserved`: the execution reaches the same visible host and issues the same
  action with the same target and value. The unrecorded effect is neither required nor credited.
- `precommit`: the execution reaches the same pre-commit state and does not commit.

## Mismatch subtypes

A `generated_env_mismatch` names one subtype:

| Subtype | Dimension |
|---|---|
| `layout_content_variant` | whole viewport: depth, density, scale, anchor, crop, chrome |
| `content_entity_mismatch` | wrong or missing entity, card, text, or image |
| `action_surface_mismatch` | the host's owning surface differs from the source |
| `overlay_structural_mismatch` | overlay type, structure, or controls differ |
| `source_host_mismatch` | wrong host, value, or surface scope |
| `actionability_mismatch` | the host is unreachable, disabled, covered, or outside the viewport |
| `selected_state_lifecycle_failure` | selection already present before its action, or absent after |
| `future_state_contamination` | a later surface or value appears too early |
| `premature_parent_projection` | the parent shows sibling or future values |
| `autocomplete_surface_mismatch` | suggestions missing, reordered, or on the wrong page |
| `scroll_region_mismatch` | the gesture acts on another region or reaches other content |
| `terminal_mismatch` | the terminal contract is not met |
| `execution_blocker` | the execution stopped at a blocker the source does not have |

## Verdicts and owners

| Verdict | Owner | Repairable |
|---|---|---|
| `review_pass_candidate` | none; `probe-contract` runs next on the same revision | n/a |
| `generated_env_mismatch` | `implementation` when closing it edits the package under the frozen specification: rendering, route wiring, storage, bindings, projection, a missing implementation of a supported relation, or a supported state update or control behavior realized differently from the source | yes |
| `generated_env_mismatch` | `semantics` when closing it would need a state or data relation, guard, or effect that no source evidence supports | no |
| `page_boundary_blocker` | `semantics`: the source shows distinct pages where the specification has one, or the reverse; closing it needs a different page identity | no |
| `reference_oracle_blocker` | `semantics`: the source trace cannot serve as the oracle for a row | no |
| `evidence_blocked` | none; produce the missing evidence (a persistent infrastructure cause is `provider`) | n/a |

Review never assigns `supervision`: checkpoint and evaluator defects are found by `probe-contract`.
Do not infer the owner from a failed earlier repair or from remaining budget; the budget belongs to
the repair ledger.

## False passes

Never:

- pass a correct tap in a wrong viewport, or dismiss scale, anchor, or crop differences without
  evidence;
- pass from route reachability, a correct tapped host, actionability, or a terminal screen alone;
- infer success from missing evidence or elapsed time;
- accept a host that exists only in script templates, hidden duplicates, or id maps, but not in the
  captured viewport;
- reuse a pass from an earlier revision, or a suffix-only review after a repair touched the trace.

Before returning `review_pass_candidate`, answer from the packet's evidence: does the first viewport
reach the source's depth; is every tapped host inside the same owning surface as in the source;
does any row show a later action's effect or a sibling's value; does the terminal meet its contract?
Any answer that the packet cannot establish makes the verdict `evidence_blocked`.

## Packet

`$RESULT/run_management/review/traces/<trace_id>/<round>/review_packet.json`:

```json
{
  "schema": "trace2world.review_packet.v1",
  "trace_id": "...",
  "round": "round_0001",
  "package_revision": "sha256:...",
  "checklist": {"path": "...", "sha256": "...", "trajectory_sha256": "...", "kept_step_count": 0},
  "execution_packet": {"path": "...", "sha256": "...", "status": "execution_complete"},
  "sprite_manifest": "...",
  "display_order": ["source_sequence", "execution_sequence"],
  "pairwise_alignment_claimed": false,
  "complete_trace": true,
  "source_step_integrity": {
    "status": "pass | fail | blocked",
    "checked_step_count": 0,
    "step_zero_checked": true,
    "description_disagreements": [],
    "rows": [{"step_index": 0, "pre_action_source_host": "pass", "marked_target_alignment": "pass",
              "source_host_not_destination": "pass", "next_state_leak_absent": "pass",
              "post_action_effect": "pass | commit_only | fail | blocked",
              "instruction_trace_alignment": "pass", "terminal_dependency_explicit": "pass"}]
  },
  "anchors": [
    {
      "anchor_id": "anchor_0000",
      "status": "unchecked | pass | fail_first | blocked",
      "source_range": {"start_step": 0, "end_step": 0},
      "execution_range": {"start_ordinal": 0, "end_ordinal": 1},
      "anchor_kind": "visible_state | action_semantics | route | overlay_lifecycle | terminal_outcome",
      "obligation_ids": ["<trace_id>:0"],
      "checks": {"whole_viewport_scan": {}, "action_surface_parity_check": {}, "selected_state": {},
                 "progressive_surface_lifecycle_check": {}, "overlay_structure": {},
                 "parent_projection_contamination_check": {}, "scroll_or_autocomplete": {},
                 "terminal_contract": {}},
      "expected": "...",
      "observed": "..."
    }
  ],
  "status": "review_pass_candidate | generated_env_mismatch | page_boundary_blocker | reference_oracle_blocker | evidence_blocked",
  "first_mismatch": {
    "stage": "review",
    "code": "<subtype or verdict>",
    "trace_id": "...",
    "step_index": 0,
    "anchor_id": "anchor_0000",
    "expected_state": "...",
    "observed_state": "...",
    "obligation_ids": ["<trace_id>:0"],
    "source_evidence": ["..."],
    "execution_evidence": ["..."],
    "owner": "implementation | semantics",
    "repairable": true
  },
  "supersedes": []
}
```

Each `checks` entry records what was compared (`required`, the source and execution observations,
`mismatches`, `rationale`); a check that does not apply to the anchor is `{"required": false}`.
`first_mismatch` is `null` for a pass candidate; for a non-pass it carries the fields
`repair-package` reads as its defect.

## Pass conditions

`review_pass_candidate` requires `execution_complete`, `complete_trace=true`, a current checklist,
`source_step_integrity.status=pass` with every kept step checked (step 0 included), every anchor
`pass`, and the terminal contract met. It is not validation: the slice is validated only after
`probe-contract` passes on the same package revision. After any repair that touches this trace, the
earlier execution and review are stale; the trace runs again from step 0 and is reviewed whole.
