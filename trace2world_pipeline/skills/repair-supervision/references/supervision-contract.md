# Supervision Contract

How checkpoints and evaluators are refined for `repair-supervision`. Existing checkpoint rows are
implementation evidence, not the authority: the authority is the obligation checklist (built from
the source trace) and the canonical business state the package produces.

## 1. Checklist mapping

Every retained row of each affected checklist gets exactly one disposition:

| Disposition | Use |
|---|---|
| `rewarded_checkpoint` | a task-relevant, observable business-state commitment |
| `dependency_state` | state a later checkpoint requires but that earns nothing itself |
| `negative_discriminator` | a wrong or premature state the evaluator must reject |
| `terminal_assertion` | part of the terminal contract |
| `projection_obligation` | a visible projection that probes must check |
| `diagnostic_only` | route, opening, or context with no business effect |
| `unsupported` | the source cannot define the expected state; cite the row |

Do not create one rewarded checkpoint per row. Do not connect adjacent rows automatically: an
edge exists only when the downstream state depends on the upstream state.

For a final micro-effect the source does not show (favorite, like, follow, toggle, count, success
label), the row maps to a terminal assertion only when `C` supports the relation; the
checkpoint then reads the package's real state for the marked entity, and the report states that
the source did not show the effect. For a terminal commit whose result the source does not show,
map only the host and the issued action; reward no destination, UI, or business effect.

## 2. Business-state model

For every rewarded field record:

```text
state_field | category | producer | reducer or transaction | state_before | state_after
| preserved_fields | changed_fields | forbidden_changes | visible_projection
| evaluator_consumer | episode_scope | reset_rule
```

Categories: route or context, entity identity, draft, selected value, collection, mutation result,
visible projection, terminal phase, forbidden or safety state. Reject a field that exists only in
evaluator fixtures, step context, task-id branches, or stale event history.

For comparison tasks (maximum, minimum, nearest, cheapest, difference), read candidate attributes
from the same canonical data the page projects and judge the final selected entity against the
constraint, not whether every candidate was visited.

## 3. Checkpoint DAG

```json
{
  "checkpoint_id": "cp_select_store",
  "checkpoint_kind": "selection",
  "ability_dimension": "entity_selection",
  "obligation_ids": ["o5"],
  "depends_on": ["cp_open_store_list"],
  "state_refs": ["state.selected_store_id"],
  "expected_state": {"state.selected_store_id": "<id bound by the task>"},
  "negative_discriminators": {"wrong_sibling": "state.selected_store_id != <id>"},
  "blocking": true,
  "progress_only": false,
  "terminal_relevance": "required",
  "reward_weight": 0.2
}
```

Ability dimensions: `route`, `entity_selection`, `constraint_satisfaction`, `state_mutation`,
`state_preservation`, `recovery`, `continuous_control`, `terminal_success`, `safety`.

- The correct operation on the wrong entity earns nothing for the entity checkpoint.
- A checkpoint whose dependency is false earns no full credit.
- Selection and mutation checkpoints assert canonical ids and values, not event existence.
- Collection mutations also assert that unrelated records are preserved.
- Repeated actions are judged by the final business state, never by a historical maximum or an
  action-count penalty.
- Partial reward is never terminal success; the full terminal contract is.
- Route checkpoints are low-weight or diagnostic unless the route is a real business precondition.
- Continuous-control checkpoints (carousel index, map region, picker value) exist only when the
  package has real, resettable intermediate state; never reward swipe counts.
- Any legitimate path that reaches the same canonical state passes.

## 4. Evaluator

Read state in this order: app-owned canonical state, the state kernel or session store, the
runtime store, a machine-readable DOM projection, a task-scoped committed event payload. An event
reports state; it is not the reward authority.

Positive assertions: correct entity and value, satisfied dependencies, commit after the action,
preserved state, current episode, task scope, absence of forbidden state.

Negative discriminators: wrong sibling or same-name wrong id; wrong SKU, quantity, filter,
address, coupon, payment, or option; draft without commit; terminal route without terminal state;
stale state or events from an earlier episode; a repetition that damaged the target; an unrelated
action emitting the same event.

Keep the vector separate from the scalar:

```json
{
  "route_score": 0.0, "entity_selection_score": 0.0, "constraint_satisfaction_score": 0.0,
  "state_mutation_score": 0.0, "state_preservation_score": 0.0, "recovery_score": 0.0,
  "terminal_success": false, "safety_violation": false, "raw_reward": 0.0,
  "nonzero_checkpoint_reached": false, "env_valid": true, "format_valid": true
}
```

The repairer decides fields, entities, dependencies, assertions, the terminal contract, and the
weights. Deterministic code may compile exact mirrors of that contract, execute evaluators, hash,
and probe; it does not choose semantics.

## 5. Synchronization

Edit in this order and keep each formal source equal to its packaged mirror:

```text
existing state producer -> machine-readable projection -> runtime state schema
-> event payload -> checkpoint matrix -> evaluator -> terminal policy -> test flow
```

Formal sources under `$RESULT/phase_post_processing/`:

- `instrumentation/checkpoint_matrix.json`, `runtime_state_schema.json`,
  `runtime_event_bindings.json`, `instrumentation_state_bindings.json`;
- `evaluators/evaluators.json`, `executable_evaluator_manifest.json`,
  `evaluator_state_bindings.json`;
- `terminal_policy/terminal_policy_verified.json`;
- `action_authority/action_execution_contracts.json`;
- `tasks/tasks.json`.

Packaged mirrors under `$RESULT/package/`: `evaluators/`, `tasks.json`, the runtime
instrumentation scripts under the app's runtime directory, and `state_evaluator_plan.json` for
state-kernel packages. When `evaluators.json` changes, keep its canonical state assertions equal
to the executable manifest; when the manifest's outcome changes, change every copy of it.

## 6. Acceptance of the repair's own checks

The repair's probe report passes only when: wrong and premature probes fail; every partial stage
earns only its progress; the correct terminal reaches terminal success; vector and scalar match
the final state; a repeated action keeps no stale maximum; reset clears all task-scoped state,
events, transactions, and reward; the evaluator and the DOM read the same canonical data; and
every retained checklist row has a disposition. `closure-loop` still re-executes, re-reviews, and
re-probes the new revision before anything is verified.
