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

## Evidence order

1. Source screenshots and the obligation checklist rows (visible state, committed actions).
2. The frozen page specifications `C`: states, data relations, guards, effects, projections.
3. The package's instrumentation and evaluators.
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
