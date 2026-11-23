# Sequence Sprite Review Contract

Review starts after execution has ended or stopped. Execution and review are separate operators.

## Required Inputs

- the current execution packet (`trace2world.gui_execution.packet.v1`) and its screenshots;
- the source sprite manifest of the same trace from `build-obligations`
  (`trace2world.source_trace_sprites.v1`), which lists the marked source screenshots;
- a review output directory under `$RESULT/run_management/review/traces/<trace_id>/<round>/`;
- the package revision the execution ran on.

Reject `execution_invalid`, missing screenshots, or a packet bound to another package revision
as `evidence_blocked`.

## One Physical Sprite

```bash
python "$T2W_SKILLS/review-trace/scripts/build_review_sprite.py" \
  --source-manifest <checklists>/traces/<trace_id>/source_sprites/manifest.json \
  --execution-packet <round>/gui_execution_packet.json \
  --output-dir <review_dir>
```

The script writes exactly one JPEG,
`sprites/<trace_id>__<round>__source_then_execution.jpg`, and a manifest with schema
`trace2world.sequence_sprite_review.v1`, `single_physical_sprite=true`,
`layout=source_sequence_then_execution_sequence`, and `pairwise_alignment_claimed=false`.

- The complete instruction is printed above both sections. If the manifest's `instruction_font`
  cannot render the instruction's script, pass `--font` with one that can.
- Every screenshot is drawn at the same height (720 px by default) without cropping.
- Source cells are the marked screenshots rendered from the recorded actions; execution cells are
  labelled from the execution packet's own action records, never copied from source labels.

## Display And Reading Order

The packet declares:

```json
{
  "display_order": ["source_sequence", "execution_sequence"],
  "pairwise_alignment_claimed": false,
  "source_sequence_complete": true,
  "execution_sequence_complete": false
}
```

Read the source section in chronological order first, then the execution section. Never
alternate cards or build a pairwise grid. The two counts may differ: execution can stop at the
first blocker, add stabilization states, or produce no screenshot for a textual response. Never
duplicate, drop, or relabel images to equalize counts. An execution without an evaluator reward is
`unscored`; a sprite is not a reward producer.

## Permitted Alignment

Review may record explicit, non-bijective anchors:

```json
{
  "source_range": {"start_step": 2, "end_step": 4},
  "execution_range": {"start_ordinal": 3, "end_ordinal": 6},
  "anchor_kind": "visible_state | action_semantics | route | overlay_lifecycle | terminal_outcome",
  "rationale": "...",
  "one_to_one": false
}
```

Anchors explain lifecycle, order, or semantic differences and valid alternative paths. Never
assert row equality from numeric indices, filenames, action counts, or screenshot counts.

## Stop And Pass Rules

- Review may stop at its first visual or semantic mismatch and return it with its owner.
- If execution is `stopped_at_first_blocker`, review the observed prefix plus the blocker state
  and set `complete_trace=false`; it cannot return a full-trace `review_pass_candidate`.
- A full-trace `review_pass_candidate` needs `execution_complete`, complete source and execution
  sections, no shortcut flag, and every review dimension passing.
- Building a sprite is not a pass. An existing sprite may be reused only when its manifest digests
  match the current checklist and execution packet.

## Output Fields

Every review packet includes the execution packet path and status, the sprite manifest path,
`display_order`, `pairwise_alignment_claimed=false`, the reviewed execution prefix and whether the
trace completed, the anchors used, the first mismatch with its owner (or every dimension for a
pass candidate), and the earlier review packets this run supersedes.
