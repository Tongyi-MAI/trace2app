---
name: review-trace
description: "Review of Trace2World Algorithm 4 (environment validation and repair). Use after execute-trace has produced a fresh execution packet: read the complete marked source trace first, then the frozen execution, compare causal states against the per-trace obligations, and emit one verdict with the first material mismatch, its owner, and whether it is repairable. Read-only: it never drives actions, extends a stopped execution, edits the trace, checklist, or environment, or repairs."
---

# Review trace

`z <- Review(R, X, O)` in Algorithm 4 (paper Appendix B.2; condensed prompt in Appendix G,
"Construction-time source-trace validation"). Review is independent and works on a frozen evidence
packet. It reads the complete source trace before the execution and compares required
information, decisions, effects, lifecycle, and terminal state. Valid alternative paths and
unequal sequence lengths are allowed. It emits a pass candidate or the first material mismatch
with its owner. A pass candidate still has to pass `probe-contract` on the same environment
digest.

Read both references before writing a packet:

- `references/sequence-sprite-review.md`: the single source-then-execution sprite and the
  reading order;
- `references/review-packet-contract.md`: every review dimension, the false-pass gates, the
  terminal handoffs, and the packet fields.

## Inputs

- the per-trace obligation checklist from `build-obligations` and its SHA-256;
- the marked source screenshots and sprite manifest written by `build-obligations`;
- one current execution packet from `execute-trace` (`trace2world.gui_execution.packet.v1`) and
  the package revision it executed;
- the review directory `$RESULT/run_management/review/traces/<trace_id>/`.

## Procedure

1. Verify the checklist digest against the current trajectory, retained-step coverage, and
   step 0. A missing or stale checklist goes back to `build-obligations`; nothing is reviewed.
2. Reject an `execution_invalid` packet, a packet bound to another package revision, or missing
   screenshots as `evidence_blocked`.
3. Build the review sprite and read it at native scale, source section first:

   ```bash
   python "$T2W_SKILLS/review-trace/scripts/build_review_sprite.py" \
     --source-manifest "$CHECKLISTS/traces/<trace_id>/reference_sprites/manifest.json" \
     --execution-packet "$ROUND/gui_execution_packet.json" \
     --output-dir "$RESULT/run_management/review/traces/<trace_id>/<round>"
   ```

4. Compare causal states, not equal row indices. Each source row is pre-action; its next row or
   terminal evidence establishes the intended effect. Relate the sequences only through explicit
   range anchors.
5. For each judged row, check the whole viewport before the tapped host: route and start data,
   chrome, selected tabs, overlays, list content, density, scale, anchor, and crop; then action
   value, exact host scope, reachability, enabled state, lifecycle, parent projection, scroll
   region, typeahead order, forbidden future state, and terminal obligations. Use visible text
   and read-only DOM/runtime measurements, never route reachability alone.
6. Stop at the first material mismatch unless full enumeration was requested, and write the
   packet.

## Verdicts and owners

| Verdict | Meaning | Owner | Repairable |
|---|---|---|---|
| `review_pass_candidate` | every dimension passes for the complete trace | none; `probe-contract` runs next | n/a |
| `generated_env_mismatch` (with subtype) | the package renders, routes, stores, binds, or projects a supported behavior differently from the source, including an execution blocker | `implementation` | yes |
| `page_boundary_blocker` | closing the mismatch would need a different page identity, materialized state, or runtime projection than the frozen specification has | `semantics` | no, leaves upstream |
| `reference_oracle_blocker` | the source evidence cannot serve as the oracle for this row (ambiguous host, target, value, or order) | `semantics` | no, leaves upstream |
| `evidence_blocked` | the execution or checklist evidence is missing, stale, or contradictory | none: re-execute or rebuild; a persistent infrastructure cause is `provider` | n/a |

`semantics`, `implementation`, and `provider` are the released `closure.Owner` values. Review
never assigns `supervision`: checkpoint and evaluator defects are found by `probe-contract`.
Every non-pass verdict names the first mismatch with its step, the expected and the observed
state, source and execution evidence, and the affected obligations, so `repair-package` can act
on it without re-reviewing.

## Never

- pass a right tap in a wrong viewport, or dismiss scale, anchor, or crop differences without
  evidence;
- infer success from missing evidence, elapsed time, a reached route, or a terminal screen alone;
- drive the browser, extend a stopped execution, or edit the trace, checklist, execution, or
  environment;
- treat `review_pass_candidate` as validation: the environment is validated only after the probes
  pass on the same digest.
