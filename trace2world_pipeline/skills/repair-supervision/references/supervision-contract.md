# Supervision Contract

How checkpoints and evaluators are refined for `repair-supervision`. Existing checkpoint rows are
implementation evidence, not the authority: the authority is the obligation checklist (built from
the source trace) and the canonical business state the package produces.

The checklist guides what must be considered, but it cannot independently decide reward
granularity. OCR, generated labels, action text, and existing checkpoint names cannot
independently define semantics. The repairer must open the actual checklist-cited source images
and the current page before authoring.

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

`$REPAIR/checklist_mapping.json` carries the mapping. Cite the checklist path and its SHA-256 at
the top level, next to the top-level `status` and `blockers`, so a consumer can prove which
checklist version the mapping belongs to. It has exactly one entry per retained checklist row, and
each entry records:

```text
trace_id
step_index
checklist_row_evidence
disposition
obligation_ids
checkpoint_ids
negative_discriminator_ids
terminal_assertion_ids
probe_classes
ability_dimensions
reward_relevant
reason
status
```

`disposition` is exactly one of the seven values in the table above. Row `status` is `mapped` or
`blocked`. The mapping passes only when every retained row is `mapped`, `step_index` coverage is
unique, and the top level records `status` and `blockers`.

Checklist coverage is mandatory; reward coverage is not. Do not convert each step into a reward
checkpoint. Geometry-only, shell, or source-host rows may remain probe obligations;
forbidden-future-state rows may become negative discriminators; terminal rows may supply evaluator
assertions; multiple rows may jointly support one canonical-state checkpoint.

Never edit, ignore, or reinterpret a ready checklist to make a probe pass. If live evidence proves
the checklist is stale or the source evidence changed, preserve the failed attempt, return to
`build-obligations`, regenerate or project the checklist, invalidate the affected mapping and
packets with `closure-loop`'s `closure_ledger.py invalidate`, and restart refinement.

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

### Obligation rows

Author one obligation row per applicable commitment kind (`route`, `route_stack`, `activation`,
`draft`, `selection`, `mutation`, `collection`, `projection`, `outcome`; the kinds are defined in
`probe-contract`'s `references/oracle-audit.md`). Each row includes:

```text
trace_id
source_step
checklist_row_id
checklist_sha256
checklist_row_evidence
source_filename
visible_target_identity
checkpoint_kind
business_commitment
canonical_state_fields
dependency
later_consumer
terminal_relevance
evidence_refs
```

The row is what binds a checkpoint to its evidence: without `source_step`, `checklist_row_id`, and
`evidence_refs`, a checkpoint states a wish rather than a source-supported commitment. Do not
automatically connect adjacent source steps; create an edge only when the downstream business
state truly depends on the upstream state.

### Per-checkpoint record and coverage matrix

For every checkpoint record:

- trace id and step index;
- source filename and expected filename/state;
- visible evidence source;
- expected event name pattern;
- payload fields needed by the evaluator;
- whether it is blocking reward, progress-only, or diagnostic.

The expected event-name pattern and the payload fields the evaluator needs are what link a
checkpoint row to the emit and evaluator layers; a row without them cannot be audited.

Create a matrix before patching:

```text
step | checkpoint_kind | expected_state | UI selector/action | event | payload | evaluator
| replay_status | runtime_status | gaps
```

Required coverage rules:

- Every `outcome` checkpoint needs a success evaluator.
- Outcome checkpoints must verify the terminal state, not only that the trace reached a payment,
  checkout, submit, detail, or confirmation page.
- Every task-relevant `mutation` checkpoint needs a canonical committed state or event and an
  evaluator assertion on meaningful fields.
- Every `selection` checkpoint that later affects submit or reward needs a progress event or
  evaluator-visible state.
- Selection checkpoints that identify entities must assert the chosen entity id/name and
  distinguishing badge/text when the source depends on it, such as nearest store, specific
  address, selected payment method, or selected coupon.
- Entity-selection checkpoints that navigate from a list/card/detail source must also assert the
  source action-host identity: clicked host/container visible text and stable identity must match
  the source entity, and sibling entities must not be accepted merely because they share the
  target route.
- Every `draft` checkpoint that is later committed needs either a draft event or a commit event
  whose payload proves the draft value was used.
- Every `projection` checkpoint that a user or agent relies on needs DOM or runtime-state
  verification; browser storage or a runtime SDK return value alone is insufficient.
- Route-only checkpoints may be covered by the execution packet if route correctness is already
  evaluated, but route events are still useful when the reward depends on reaching a page before
  final submit.
- Back/close route-stack checkpoints do not always need reward events, but they must be covered by
  a replayable oracle when they affect task continuation or generic agent exploration. The oracle
  should assert the destination filename and assert that repeated back/close does not bounce
  between the same two pages.
- Diagnostic events may be non-reward, but they must not be confused with final success.

Split a grouped checkpoint when one row hides multiple source-trace commitments. A single
checkpoint should not simultaneously cover search submission, modal opening, entity selection,
item purchase, payment method selection, and final submit unless the event payload and evaluator
prove every one of those facts. If a grouped checkpoint can be satisfied by an earlier step alone,
its `diagnosis.issue_class` is `over_grouped_checkpoint`.

### Checkpoint rules

- The correct operation on the wrong entity earns nothing for the entity checkpoint.
- A checkpoint whose dependency is false earns no full credit.
- Selection and mutation checkpoints assert canonical ids and values, not event existence.
- Collection mutations also assert that unrelated records are preserved.
- Repeated actions are judged by the final business state, never by a historical maximum or an
  action-count penalty.
- Partial reward is never terminal success; the full terminal contract is.
- Route checkpoints are low-weight or diagnostic unless the route is a real business precondition.
- Never raise a reward level without a distinct observable business state.
- Normalize scalar weights without flattening the vector dimensions.
- Any legitimate path that reaches the same canonical state passes.

### Continuous and recovery states

Create continuous-control checkpoints only when the current environment has real, resettable
intermediate state. Worked progressions:

- carousel: gesture committed, target index/range reached, target visible, target aligned;
- map: viewport changed, target region reached, target POI visible, target POI selected,
  confirmation committed;
- date/time picker: picker active, value changed, target value selected, range legal, value
  committed.

Do not reward swipe count or invent an intermediate state absent from the page/runtime.

For repeated-action and correction behavior, check:

- whether the target was already satisfied;
- whether repetition toggled or damaged it;
- duplicate collection entries;
- target quantity and selected value;
- preservation of already-correct fields;
- whether recovery changes only the wrong field.

## 4. Evaluator

Treat these as independent verdict layers, each proving only its own claim:

- `execute-trace`'s execution packet: proves the admitted action sequence was executed naturally
  or identifies the first operational blocker.
- `review-trace`: reads the source sequence first and the execution sequence second, uses
  non-pairwise anchors, and owns visual, action, lifecycle, and resource verdicts.
- a reachability replay of the execution packet: can prove that a trace reaches its pages, but not
  reward correctness by itself.
- the released `runtime_checkpoint_collector`: can prove that known checkpoint contracts close,
  but not that the checkpoint set is complete or semantically strong.
- `checkpoint_matrix`: can define intended milestones, but may be under-specified, grouped too
  broadly, or missing per-step state.
- `runtime_event_bindings` and page handlers: can emit events, but may use weak payloads or fire
  from the wrong action.
- `evaluators` and the reward oracle: can pass while only checking event existence, route
  presence, or a stale terminal page.
- the generated page/runtime artifact: may lack the state transition or visible terminal state
  that the refined checkpoint claims to verify.

When these layers disagree, diagnose the first weak layer instead of patching the most visible
symptom.

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

Formal sources under `$RESULT/phase_post_processing/` (the environment's supervision layer, not
the frozen app specification under `phase_consolidation/site_model/`, which `package_delta.py`
snapshots as `--frozen`; edits here are declared `--editable`, and their packaged mirrors carry the
invalidation):

- `instrumentation/checkpoint_matrix.json`, `runtime_state_schema.json`,
  `runtime_event_bindings.json`, `instrumentation_state_bindings.json`;
- `instrumentation/semantic_checkpoint_plan.json`;
- `instrumentation/effect_checkpoint_matrix.json`, written as a copy of `checkpoint_matrix.json`,
  so it must change whenever `checkpoint_matrix.json` changes;
- `instrumentation/checkpoint_matrix_validation_report.json`;
- `instrumentation/task_semantic_obligation_matrix.json`, the plan's input: cite it, never
  contradict it;
- `evaluators/evaluators.json`, `executable_evaluator_manifest.json`,
  `evaluator_state_bindings.json`;
- `terminal_policy/terminal_policy_verified.json`;
- `action_authority/action_execution_contracts.json`;
- `tasks/tasks.json`;
- `test_logic/test_flows.json` and `test_logic/playwright_flows/<trace_id>.json`.

Compiled runtime artifacts under `$RESULT/phase_sdk_generation/`:

- `runtime/instrumented_business_logic.js`, the emit layer;
- `validation/test_flows/test_flows.js`, the compiled test flow.

Packaged mirrors under `$RESULT/package/`: `evaluators/`, `tasks.json`, the runtime
instrumentation scripts under the app's runtime directory, and `state_evaluator_plan.json` for
state-kernel packages. When `evaluators.json` changes, keep its canonical state assertions equal
to the executable manifest; when the manifest's outcome changes, change every copy of it. Skipping
a mirror leaves packaged consumers disagreeing with the formal source.

## 6. Acceptance of the repair's own checks

The repair's probe report passes only when: wrong and premature probes fail; every partial stage
earns only its progress; the correct terminal reaches terminal success; vector and scalar match
the final state; a repeated action keeps no stale maximum; reset clears all task-scoped state,
events, transactions, and reward; the evaluator and the DOM read the same canonical data; and
every retained checklist row has a disposition. `closure-loop` still re-executes, re-reviews, and
re-probes the new revision before anything is verified.

The off-path recipes that extend the probe matrix for a supervision edit, the fixing rules, and
the pitfalls are in `checkpoint-fixloop.md`.
