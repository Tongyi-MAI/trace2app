# Oracle Audit

The oracle is the chain that turns a supported obligation into reward:

```text
obligation -> checkpoint -> producer/reducer (after the triggering action) -> visible projection -> evaluator field
```

The audit checks that chain for every supported obligation of the slice before any probe runs.
It answers four questions per trace:

1. Which business-state commitments does the source trace make (the obligation checklist)?
2. Which checkpoint represents each one, and which producer writes it, with which fields?
3. Which evaluator field consumes it, and does it assert the meaningful value?
4. Does the package produce that state at the expected step, and not before?

A green aggregate (all checkpoints closed, all replays reached their route) answers none of these
by itself: it shows that the current oracle closes, not that it is complete or strong.

## Audit rows

Write one audit row per supported obligation of the trace, keyed by the stable key the released
checkpoint matrix declares:

```text
stable_key = trace_id|checkpoint_id|checkpoint_kind|expected_state_id
```

Read the row's fields from the released matrix instead of restating them: `source_steps`,
`supervision` (`process` or `outcome`), `blocking`, `reward_weight`,
`global_runtime_state.state_refs`, `global_runtime_state.source_action_ids`, `event`,
`payload_fields`, `projection_assertions`, `negative_probes`, and `depends_on` on the rows that
declare it (the outcome row lists the blocking process checkpoints it requires). The evaluator side
comes from `evaluators.json`: `reward_contract.canonical_state_assertions`,
`reward_contract.projection_assertions`, the `reward_contract.requires` set, and the
`clean_contract.failure_codes` a failed check can emit.

One row per obligation, in this shape:

```text
trace_id | checkpoint_id | checkpoint_kind | expected_state_id | expected_state | source_steps | role | state_refs | event | payload_fields | evaluator_field | gap
```

The first four columns are the stable key: merge, compare, and report rows by it. `role` is read
from the matrix (next section), `evaluator_field` is empty for a row that needs none, and `gap` is
empty only when the row is covered.

## Evidence order

1. Source screenshots and the obligation checklist rows (visible state, committed actions).
2. The frozen page specifications `C`: states, data relations, guards, effects, projections.
3. The package's instrumentation and evaluators, read in both their formal source under
   `phase_post_processing/` and their packaged mirrors under `$RESULT/package/` (`evaluators/`,
   `state_evaluator_plan.json`, `tasks.json`). The host runs the mirror, so a formal source that
   disagrees with it is already a finding.
4. Trace thoughts and labels. Never create a checkpoint from a thought alone.

## Commitment kinds

| Kind | The checkpoint must prove |
|---|---|
| route | the page or route that task progress needs; never sufficient as an outcome |
| route stack | back/close lands on the contextual parent and does not loop between two pages |
| activation | the control opened the correct search, form, picker, sheet, or modal |
| draft | an app-owned input or picker value before commit |
| selection | which tab, chip, option, address, time, payment method, coupon, or filter is selected, distinguishable from its siblings |
| mutation | cart, quantity, coupon, favorite, save, submit, payment, or record change, written after the action |
| collection | the changed record set, with unrelated records preserved |
| projection | the visible state later steps depend on (selected indicator, cart footer, enabled CTA, list row, pending or confirmed status) |
| outcome | the durable terminal business state, not `current_page`, a generic done flag, or an event's existence |

Loading states, phone system chrome, and the OS keyboard are not checkpoints; the app-owned input
value behind the keyboard is.

## Row roles

Read the role from the released matrix fields `blocking`, `reward_weight`, and `supervision`
(`process` or `outcome`) before demanding an evaluator consumer:

| Role | Read from | What the audit demands |
|---|---|---|
| blocking reward | `blocking: true`, and the rows `reward_contract.requires` names, including the outcome row (`supervision: outcome`) | the checkpoint that represents it, the producer that writes it after the triggering action, the visible projection, and the evaluator field that consumes it |
| progress-only | `blocking: false`, or a process row outside `reward_contract.requires` | checkpoint, producer, and visible projection; it earns progress, not terminal credit, and needs no evaluator field of its own |
| diagnostic | a row whose commitment has no business effect: route, opening, or context | a producer and a replayable oracle only |

Route-only rows may be covered by the executed action sequence when route correctness is already
evaluated, and back/close route-stack rows do not always need a reward event. They must still be
covered by a replayable oracle whenever they affect task continuation or generic exploration, and
that oracle asserts the destination and asserts that repeated back/close does not bounce between
the same two pages. Diagnostic events may be non-reward, but they must not be confused with final
success.

Demanding an evaluator field for every supported obligation fails correct dependency, projection,
and diagnostic rows as `missing_checkpoint`. State that exists only so a later checkpoint can be
read, a visible projection a probe must check, and route or opening context with no business effect
are obligations of the slice, not reward checkpoints: they carry `repair-supervision`'s
`dependency_state`, `projection_obligation`, and `diagnostic_only` dispositions, and the rule there
is not to create one rewarded checkpoint per row.

## Coverage rules

- Every outcome has an evaluator that asserts terminal business state.
- Every task-relevant mutation has a producer and an evaluator assertion on its meaningful fields.
- Every selection that later affects submit or reward is evaluator-visible, and identifies the
  chosen entity by id and by the distinguishing value the source depends on.
- A selection that navigates from a list or card also proves the source host: the activated row's
  visible identity matches the obligation; a sibling that routes to the same target must fail.
- Every committed draft is proven either by a draft checkpoint or by a commit payload that carries
  the draft value.
- Every projection a later step relies on is checked in the DOM or runtime state, not only in
  storage.
- Where preservation matters the payload carries before and after state: for a collection mutation,
  enough state to prove unrelated records remain; for a single-select, the selected id plus enough
  state to prove the previous selection cleared.
- Every row's `source_steps` points at the step that commits the state, not at an earlier
  navigation, input focus, modal open, or stale generated action.
- Every row's `checkpoint_kind` matches the commitment it claims: search text is `draft` or
  `selection` before it becomes a committed route or result; add-cart, delete, save, payment
  confirm, and submit are `mutation` or `outcome`.
- Every row encodes its prerequisite state (`depends_on`, and the order the matrix implies): query
  submitted before filter selection, product selected before cart mutation, modal open before
  confirm, payment selected before the final transaction.
- Every row's state is written after the real user action commits the change, not prefilled during
  page load, route bootstrap, or modal render.
- Every state field is closed in both directions: a field declared in `runtime_state_schema.json`
  that no page writes is an orphan, and a field the page runtime writes that no evaluator consumes
  is a dead emit. Check the same two directions in `instrumentation_state_bindings.json` and
  `evaluator_state_bindings.json`.
- One checkpoint never hides several commitments: if an earlier step alone can satisfy it, split
  it.
- Partial progress and terminal success are separate checkpoints.

## Gap classes

| Gap | Meaning | Owner |
|---|---|---|
| `missing_checkpoint` | a supported commitment has no checkpoint | `supervision` |
| `over_grouped_checkpoint` | one checkpoint passes before all its commitments happen | `supervision` |
| `missing_emit` | the checkpoint exists, but nothing writes it after the real action | `supervision` when the page state exists; `implementation` when it does not |
| `weak_payload` | the producer writes a constant, placeholder, or identity-free value | `supervision` |
| `weak_evaluator` | the evaluator checks a route, an event's existence, or a broad text match | `supervision` |
| `stale_test_flow` | a flow or action-authority row targets an old page, action, or step order | `supervision` |
| `page_runtime_gap` | the package cannot express the state transition, selection, projection, or terminal state | `implementation` |
| `unsupported_reference_evidence` | the commitment has no source support, or closing it needs a new state or effect | `semantics` |

Three static checks settle `missing_emit` without running anything: the binding names a producer the
visible path never invokes; its selector is not normalized to a real function or action name, or
still carries an unresolved placeholder such as `"<id>"` or `"${query}"`; or the event is emitted
before the state mutation commits rather than after it. A declared field that no page writes and a
field the page writes that no evaluator consumes are the same gap seen from its two directions.

`weak_evaluator` also covers an evaluator with nothing to reject. Count the `negative_probe_specs`
compiled into the executable manifest, per guarded effect and per checkpoint: zero specifications
for a guarded effect is `weak_evaluator` (`supervision`), never a fresh pass. The negative-probe
executor runs only what the manifest compiled, so a manifest with zero or thin specifications
produces a green, freshly written report that probed nothing.

`stale_test_flow` also covers a shifted or missing step binding on the checkpoint row itself. The
bindings key on `<trace_id>:step<N>`, so a row whose `source_steps` moved, or whose late-step
binding was never written, makes the execution skip the real draft or outcome steps and leaves
`checkpoint_runtime_validation.json` stale. `action_execution_contracts.json` already reports the
missing side of this as `missing_source_action_ids`.

`page_runtime_gap` also covers the package's own action handling: when the package rejects,
swallows, or fails on a legal visible action, the state transition it admits cannot be observed at
all. That is `implementation`, not a blocked probe, and it is the same class as a state that only
holds while the runner injects step or action context.

## Missing layers

A missing instrumentation file is not a harmless skip. Name the layer that is missing instead of
recording a skip:

| Missing | Gap | Owner |
|---|---|---|
| no emitted event or state binding (`runtime_event_bindings.json`, `instrumentation_state_bindings.json`) | `missing_emit` | `supervision`, or `implementation` when the page state does not exist |
| no test flow for the trace | `stale_test_flow` | `supervision` |
| no evaluator, or no compiled manifest entry | `missing_checkpoint` when the commitment has no row; `weak_evaluator` when a row exists with nothing consuming it | `supervision` |
| no runner reward binding, so the host cannot evaluate the episode at all | `probe_blocked` | rerun; a persistent cause is `provider` |

## Drift checks

Two copies of the same contract ship in the package and no released checker compares them, so the
audit compares them directly:

- `evaluators.json` and `executable_evaluator_manifest.json` must agree on the canonical state
  assertions and on the dependencies between checkpoints. When `evaluators.json` changes, keep
  `reward_contract.canonical_state_assertions` aligned with the executable manifest; when the
  manifest changes, the collection that produced `checkpoint_runtime_validation.json` is stale
  relative to the new contract and must be rerun.
- Both copies of the outcome block must agree: a manifest whose outcome checkpoint and top-level
  `outcome` block say different things is invisible to every released verifier.
- `terminal_policy_verified.json` must not claim the outcome is verified while the terminal boundary
  asserts only route or page reachability.

Verifiers may not catch this drift.

## Freshness

A trace-local focused log is not closure when the packet cites a canonical output that the final
verifier reads; rerun the producer instead of treating focused logs as final. The runtime collector
merges a `--trace-id` run into `checkpoint_runtime_validation.json`
(`collection_mode.mode = trace_scoped_merge`) unless `--replace-output` is given, so read
`collection_mode` before trusting that file, and report unrelated residual pending rows separately
from the trace being probed, never merged into its verdict. Report broader package-level results
separately from slice-level closure: a package can remain partially audited while the probed slice
passes.

A static validation pass with a non-browser or not-applicable checkpoint profile is not runtime
closure either. The environment manifest carries `runtime_profile` (`browser_checkpoint_events` or
`state_kernel_sqlite`), so a package can validate while the checkpoint profile the probes need is
inapplicable.

## False-pass patterns

- A route or page reached satisfies an outcome although no submit or confirm created terminal state.
- A list or card checkpoint passes because the target page matches, while the action host belongs
  to a different visible entity.
- A grouped checkpoint is closed by an earlier search or route while a later selection is wrong.
- A payment, store, address, coupon, tab, or filter checkpoint checks that something was clicked,
  not which option is selected.
- All checkpoint rows close, but the source trace has more commitments than the rows.
- The DOM shows a value while the payload or evaluator uses a fixture, placeholder, or stale route.
- A click visibly changes a control but writes no state the evaluator can read.
- The same event can fire from another task without task or entity identifiers.

Each of these is a failure of the slice even when replay and runtime supervision are green.
