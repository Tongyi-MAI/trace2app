# Alternative Paths

An alternative path is a non-source route that a reasonable user could take, from visible state,
to complete the same task with the same business result. The source trace proves one successful
execution, not the only one. Checkpoints are predicates over the final business state, so a valid
alternative path that realizes the same contracted state earns the same credit (paper Appendices
B.3 and C.3), and the probe matrix of `probe-contract` holds a "legitimate alternative path" row.

Alternative paths come up in three places:

- authoring: a candidate's decisions can be reached by more than one supported route;
- screening: a learner or solver attempt reaches the terminal by a route the source never took,
  and the evaluator scores it differently from the source route;
- probing: the alternative-path row of the probe matrix needs a concrete route.

Task augmentation may recombine supported operations beyond any recorded path; it never adds an
edge. A semantically equivalent path is implemented only when every edge is a supported operation
of the frozen model. A path that needs an unsupported edge is recorded as a finding and stays
outside the package (paper Appendix E.3.3 audits this split).

## Three verdicts

Each candidate path gets three independent verdicts; never collapse them into one flag.

| Verdict | Question | Values |
|---|---|---|
| `semantic_verdict` | Does the visible path complete the same task under normal product semantics? | `valid`, `invalid`, `uncertain` |
| `materialization_status` | Does the current package execute it end to end from reset? | `materialized`, `partial`, `missing`, `not_probed` |
| `evaluator_status` | Would the evaluator recognize the equivalent completion? | `covered`, `reference_overfit`, `missing`, `not_inspected` |

A visible "payment method" row may be `valid + missing + not_inspected`. A clickable detour may be
`invalid + materialized + covered`.

## Evidence order

1. the instruction and its terminal business semantics;
2. the visible state at each relevant point (labels, values, selected and disabled state, rows,
   tabs, arrows);
3. the source trace, as one known successful path;
4. DOM and package files, for materialization only;
5. the evaluator and its checkpoints, for coverage only.

The absence of an action host, handler, or checkpoint does not disprove a semantic candidate: the
package was built along the source traces, so other controls may be visible but uncovered. Never
derive a candidate from hidden DOM, file names, comments, or behavior the visible state does not
support.

## Procedure

1. Bind the target: package revision, task id and instruction, source trace, launch state, commit
   boundary, and the expected terminal entity and payload.
2. Normalize the terminal into business invariants (entity, quantity and variants, amounts,
   required selections, whether submit or payment is allowed, final visible state), not into the
   source's clicks.
3. Normalize the source path into business operations and the states where each changes canonical
   state; drop coordinates, waits, and replay mechanics.
4. At the launch state and every consequential state, list the visible controls that could
   advance, reorder, shortcut, or preselect part of the task: other entry points and searches,
   tabs and filters, rows with values and disclosure arrows, pre-submit selectors, shortcuts versus
   drill-downs, state-preserving back paths, sheets exposing an equivalent choice.
5. For each control, write the substitution row: where the path diverges, which source steps and
   business operations it replaces, the shortcut surface, and where it rejoins. Name skipped
   business operations, not the target button.
6. Apply the seven gates below. A failure makes the candidate `invalid`; insufficient visible
   evidence makes it `uncertain`, never `valid`.
7. Only then assess materialization and evaluator coverage, without revisiting the semantic
   verdict.

Candidate families: alternative entry route; reordered commuting operations; preselection carried
downstream; shortcut versus drill-down; equivalent selection surface; state-preserving navigation
that the task actually needs.

## Equivalence gates

| Gate | Passes when |
|---|---|
| `A_visible_support` | every divergence action has a visible affordance or a visible state that conventionally implies it |
| `B_entity_identity` | the task entity (item, merchant, trip, event) stays the same |
| `C_payload_identity` | quantity, variant, address, date, seat, coupon, payment method, and total stay equivalent unless the instruction permits variation |
| `D_terminal_identity` | the final business state and visible completion match the task, not just the final page |
| `E_commit_boundary_safety` | the path does not cross a submit, save, purchase, or payment boundary the instruction forbids |
| `F_task_relevance` | every divergence has a plausible role in this task |
| `G_state_continuity` | required business state is carried across divergence and rejoin by app state, not by step context, injected ids, or trace metadata |

Reject, naming the reason, a candidate that detours through unrelated features; reaches the target
by file navigation, a handcrafted URL, script injection, or an id jump; depends on an invisible
control; changes the entity, payload, or terminal; crosses a forbidden boundary; picks a business
value from replay context; is the same control at another coordinate; or exists only in a route
graph. Do not reject because a host is missing, the source never clicked it, replay cannot run it,
or the evaluator lacks a checkpoint: those are materialization and coverage findings.

## Materialization and coverage

`materialized` requires, from reset: a host on the visible control, an effect on app-owned state,
the selected value projected on return, the downstream commit consuming it, and state surviving
natural navigation. `reference_overfit` means the reward requires source-only intermediates,
ordering, page visits, or process checkpoints although the terminal is equivalent; process
checkpoints stay only when they encode required semantics or a safety boundary.

Routing:

- `valid + materialized + reference_overfit`: a supervision defect; `repair-supervision` fixes the
  evaluator (in screening, `diagnose-attempts` attributes it as `EnvDefect`);
- `valid + partial/missing` with every edge a supported operation: an implementation gap;
  `implement-task-slice` may instantiate it within the candidate's slice (`implement: true`);
- `valid` but some edge unsupported: a finding only; it is not implemented and the task does not
  depend on it;
- `invalid` or `uncertain`: kept in the ledger with its reason so it is not rediscovered.

## Packet

`$RESULT/task_supply/alternative_paths/<task_id>.json`, validated by
`scripts/validate_alternative_path_packet.py`:

```json
{
  "schema": "trace2world.alternative_path_packet.v1",
  "task_id": "...", "source_trace": "...", "package_revision": "sha256:...",
  "terminal_semantics": {"entity": "...", "payload": {}, "commit_boundary": "before submit"},
  "reference_path": ["business state", "..."],
  "candidate_ledger": [
    {
      "candidate_id": "alt_01",
      "path": ["visible label or business operation", "..."],
      "equivalence_gates": {"A_visible_support": "pass", "...": "pass"},
      "semantic_verdict": "valid",
      "materialization_status": "missing",
      "evaluator_status": "covered",
      "reference_replacement": {"replaced_reference_steps": [], "replaced_reference_operations": [],
                                "shortcut_surface": "...", "rejoin_state": "..."},
      "primary_semantic_finding": true,
      "implement": true,
      "edges_supported": true,
      "supported_operation_ids": ["op.checkout.payment_method"],
      "closure": {"execution_packet": "...", "probe_report": "..."},
      "reason": null
    }
  ],
  "primary_semantic_findings": ["alt_01"]
}
```

Rank accepted paths by semantic significance before materialization: a shortcut that removes a
source drill-down is a primary finding even when its host is missing. Run the validator with
`--index` to check that implemented paths use only supported operations, and with
`--require-materialization-closure` after implementation: each implemented path then needs its own
fresh execution packet (`execute-trace`) and a passing alternative-path probe (`probe-contract`).
