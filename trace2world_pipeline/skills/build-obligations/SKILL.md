---
name: build-obligations
description: "BuildObligations of Trace2World Algorithm 4 (environment validation and repair). Use before a compiled environment is validated against its source traces: derive one independent obligation checklist per source trace from the instruction and the chronologically marked screenshots, covering current visible state, forbidden future state, lifecycle, input/typeahead, overlay and parent projection, viewport containment, and terminal obligations. Builds checklists only; it never executes, reviews, repairs, or passes an environment."
---

# Build obligations

`O <- BuildObligations(R)` is the first line of Algorithm 4 (paper Appendix B.2). Every later
check of the loop compares against these obligations: `review-trace` reads them as the per-step
checklist, `probe-contract` uses their terminal rows as the obligation inventory, and
`repair-supervision` maps them onto business-state checkpoints. Obligations come from source-trace
evidence alone. Nothing in this skill looks at the compiled environment.

## Inputs

- `$TRACES`: a directory of source traces for one app, each `<trace_id>/trajectory.json` plus
  `screenshots/`. Each recorded step carries the action and its screen-pixel coordinates.
- `$RESULT`: the result directory being validated. Checklists go to
  `$RESULT/run_management/reference_stepwise_checklists/`, next to the repair ledger of `t2w-close`.
- `$T2W_SKILLS`: `trace2world_pipeline/skills` in this repository.

Read `references/checklist-contract.md` before filling or validating a checklist.

## Procedure

Build one trace at a time, and finish it before starting the next.

1. Scaffold the set index and one pending checklist per selected trace:

   ```bash
   python "$T2W_SKILLS/build-obligations/scripts/scaffold_tracewise_checklists.py" \
     --trace-root "$TRACES" --checklist-dir "$CHECKLISTS" --traces <id1,id2,...>
   ```

2. Render the marked screenshots and the chronological sprite of that trace. The script draws
   each recorded action (tap ring, drag arrow, typed-text label) on the screen the user saw
   before acting:

   ```bash
   python "$T2W_SKILLS/build-obligations/scripts/render_marked_trace.py" \
     --trace-root "$TRACES" --traces <trace_id> \
     --output-dir "$CHECKLISTS/traces/<trace_id>/reference_sprites"
   ```

3. Open the sprite and read the whole sequence at native scale before writing any row. Do not
   derive obligations from the JSON action text alone.
4. Fill every retained row of `traces/<trace_id>/stepwise_checklist.json`, including step 0.
5. Write `traces/<trace_id>/evidence.json` with the render command, the trajectory digest, the
   sprite paths, and `sprite_opened_and_read=true`.
6. Reduce per-trace status into the set index, then validate:

   ```bash
   python "$T2W_SKILLS/build-obligations/scripts/reduce_tracewise_checklist_set.py" --checklist-dir "$CHECKLISTS"
   python "$T2W_SKILLS/build-obligations/scripts/validate_tracewise_checklists.py" \
     --trace-root "$TRACES" --checklist-dir "$CHECKLISTS" --traces <id1,id2,...> --require-ready
   ```

   `--allow-indexed-superset` validates one newly appended trace against a larger existing set; it
   relaxes nothing for that trace.

## Writing a row

- The marked screenshot is the pre-action state; the next retained screenshot, the parent
  projection, or terminal evidence is the action's effect.
- Describe the whole task-relevant viewport, not only the tapped target.
- Name the visible source host. A control that appears only after navigation is a destination,
  not the host.
- Record what must not be visible yet. This is mandatory for progressive selection, reveals,
  result counts, autocomplete, overlays, sheets, filters, pickers, and parent projections.
- Record selected/default state before and after a selection, input value and suggestion order
  for typeahead, overlay structure with its parent projection, and controls that must stay inside
  the phone content shell.
- Record terminal values as relations (for example "the first value below the threshold"), not
  only the final label.

Example: before tapping `happiness` in a dictionary result, the row states that no meaning entry
is selected and that `happiness` and `93.4%` must not be shown yet; the tap row states that both
appear only after the action.

## Terminal steps without a settled observation

A recording often ends before the effect of the last action is visible. That alone does not block
the trace; record one of two handoffs:

- `terminal_synthetic_projection_handoff` for a small, local, deterministic delta whose target and
  before-state are unambiguous (favorite, like, follow, toggle, a local count). Never for
  payments, orders, privacy or destructive actions, ambiguous entities, or hidden data.
- `terminal_action_commit_handoff` for any other final action whose host, target or value, and
  issued tap/commit/gesture are unambiguous. Success is the commit on the correct visible host;
  the unrecorded destination or business result is out of scope and is never synthesized,
  required, checkpointed, or claimed.

## Annotation text is a candidate

The action text in a trace is a candidate description. Obligations come from the instruction and
the marked screenshots. When the text disagrees with the visual evidence (wrong host, stale
wording, a redundant step) but the correct obligation is still knowable, write the obligation
from the visual evidence and record the disagreement in `annotation_text_disagreement`. Mark a
trace `blocked` only when the correct host, target, value, order, or disposition stays unknowable
after reading the marked sequence. A blocked trace contributes no obligations and cannot enter
validation; report it to the construction owner as missing source evidence (owner `semantics`).

## Staleness

A checklist is bound to the SHA-256 of the trajectory it was built from. If a source trace
changes, the checklist is stale: rebuild it from the current trace instead of editing rows in
place.

## Block readiness when

- a selected trace lacks `trajectory.json` or a screenshot of a retained step;
- the sprite was not opened and read;
- a retained step, including step 0, has no row;
- an expected state or effect is vague, except for an eligible terminal handoff with every field
  recorded;
- a progressive selection or reveal omits its forbidden future state;
- an applicable input, projection, containment, or terminal obligation is left implicit;
- the trajectory digest no longer matches;
- a checklist file at the set root mixes rows from several traces;
- any trace remains `pending` or `blocked`.

## Output

Return the checklist directory, trace count, per-trace checklist paths and digests, sprite
paths, the validation command and result, and blocked traces. Never return a pass verdict for an
environment, a probe, or a trace: this skill only states what a faithful environment must show.
