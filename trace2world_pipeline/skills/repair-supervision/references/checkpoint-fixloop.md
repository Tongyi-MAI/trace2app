# Checkpoint Fixloop

Use this file only after the refined checkpoint contract has been materialized and one or more
required execution probes do not fully pass. It carries the field-level runtime, payload,
evaluator, probe, artifact-sync, and false-positive rules of the repair.

The common false positive is task-level success coverage without step-level semantic coverage. A
trace can emit a final `success_*` event while missing critical progress events, using stale
payload variables, firing success too early, or failing to prove that a visible UI state changed
as the source step requires.

Refinement semantics are owned by `supervision-contract.md`. Do not infer or generate the refined
checkpoint set from this fixloop.

## Contents

1. Artifact discovery
2. Static audit before rerunning probes
3. Per-checkpoint event replay and the focused probe
4. Negative and off-path probes for a supervision edit
5. Fixing rules
6. Common pitfalls

## Artifact discovery

If the operator names a result dir, that dir is the only source of truth. Do not inspect or patch
a sibling rerun unless explicitly asked.

Locate checkpoint, evaluator, runtime, and instrumentation files dynamically because names vary:

```bash
rg --files "$RESULT" | rg 'instrument|evaluator|checkpoint|test_flows|business_logic|terminal_policy|action_execution'
```

Typical released files:

- `phase_post_processing/instrumentation/checkpoint_matrix.json`
- `phase_post_processing/instrumentation/semantic_checkpoint_plan.json`
- `phase_post_processing/instrumentation/effect_checkpoint_matrix.json`
- `phase_post_processing/instrumentation/checkpoint_matrix_validation_report.json`
- `phase_post_processing/instrumentation/task_semantic_obligation_matrix.json`
- `phase_post_processing/instrumentation/runtime_event_bindings.json`
- `phase_post_processing/instrumentation/runtime_state_schema.json`
- `phase_post_processing/instrumentation/instrumentation_state_bindings.json`
- `phase_post_processing/evaluators/evaluators.json`
- `phase_post_processing/evaluators/executable_evaluator_manifest.json`
- `phase_post_processing/evaluators/evaluator_state_bindings.json`
- `phase_post_processing/evaluators/checkpoint_runtime_validation.json`
- `phase_post_processing/evaluators/evaluator_negative_probe_report.json`
- `phase_post_processing/terminal_policy/terminal_policy_verified.json`
- `phase_post_processing/action_authority/action_execution_contracts.json`
- `phase_post_processing/tasks/tasks.json`
- `phase_post_processing/test_logic/test_flows.json`
- `phase_post_processing/test_logic/playwright_flows/<trace_id>.json`
- `phase_sdk_generation/runtime/instrumented_business_logic.js`
- `phase_sdk_generation/validation/test_flows/test_flows.js`
- the package's task runner and its `state_evaluator_plan.json` reward binding

If `phase_sdk_generation/validation/test_flows/test_flows.js` is missing and the postprocessor
skipped JS injection, that is not a harmless skip. Report which layer is missing: no test flow, no
emitted event, no evaluator, or no runner reward binding.

## Static audit before rerunning probes

Audit all five instrumentation layers:

1. Plan layer:
   - Does the plan include each checkpoint?
   - Are event names task-scoped and specific enough?
   - Are event preconditions tied to the right task/page/action, not global loose text?
   - Are grouped checkpoints split so each source-trace commitment can fail independently?

2. Emit layer:
   - Does `instrumented_business_logic.js` wrap the actual function or UI action that runs during
     the probe?
   - Are function selectors normalized to real function names?
   - Are placeholders such as `"<id>"`, `"<checkoutItem.id>"`, or `"${query}"` resolved to runtime
     values or rejected?
   - Are events emitted after the state mutation/commit, not before?

3. Payload layer:
   - Does payload contain canonical ids and user-visible values needed to disambiguate entities?
   - Does it include before/after state where preservation matters?
   - For collection mutations, does it expose enough state to prove unrelated records remain?
   - For single-select, does it expose selected id and enough state to prove previous selection
     cleared?

4. Evaluator/reward layer:
   - Does `evaluators.json` consume the emitted event?
   - Does it assert meaningful fields rather than only event existence?
   - Does it reject premature success, wrong entity, stale route, unresolved placeholders, or
     empty payloads?
   - Is the package's task runner wired to the task id and outcome reward?

5. Page/runtime artifact layer:
   - Does the generated HTML expose the source state through visible DOM and stable
     machine-readable state?
   - Does the target action actually mutate state, route, selection, modal, record collection, or
     terminal outcome?
   - Does the page distinguish the source entity from similar visible entities?
   - If the task is a simulated app, does submit/confirm leave an explicit terminal state instead
     of silently staying on the same page?

## Per-checkpoint event replay and the focused probe

Serve the package with `t2w-serve`:

```bash
t2w-serve --environment-dir "$RESULT/package" --state-root "$RESULT/run_management/host_state" \
  --host 127.0.0.1 --port <port>
```

If reusing a server, verify its served root is the requested package.

Replay with the visible-action driver (`POST /episodes/{id}/actions`) or a direct Playwright
script, not an opaque trace runner. For each checkpoint:

- execute the visible action;
- capture a screenshot when the checkpoint has visible UI state;
- read emitted events from the page/runtime storage or the task runner's event surface;
- assert the expected event appears only after the triggering action;
- assert payload values match the visible source entity/value/state;
- assert evaluator state changes from not-yet-satisfied to satisfied at the correct point.

When patching instrumentation, re-run the same checkpoint segment. Do not claim success from code
inspection, event-name grep, or final success alone.

Prefer focused probes for semantic false positives. A good probe captures all three states: before
the required action, after a wrong/off-path action, and after the correct action. Read both
visible DOM and runtime/event/evaluator surfaces. For terminal actions, assert a durable page
state such as a `data-*` attribute, a runtime store field, or a visible confirmation or pending
status.

## Negative and off-path probes for a supervision edit

Refined checkpoints used for training must not reward accidental or stale paths. These recipes
extend the rows of `probe-contract`'s `references/probe-matrix.md`; add them when relevant:

- Before the required action, assert final success is not already satisfied.
- Click an unrelated visible control and assert no task success event fires.
- Select the wrong entity/value and assert the evaluator rejects it.
- For named list/card/detail navigation, click the wrong sibling row/card and assert the
  route/event/evaluator does not satisfy the checkpoint; then click the correct visible row/card
  and assert the host identity, route, and payload all match.
- For search/autocomplete/list rows, click natural hit areas and assert events bind to the visible
  entity/query or the component is non-interactive.
- For cart and collection flows, change quantity or add another item and assert payload reflects
  the resulting collection, not a constant fixture.
- For route variants, assert the event payload or evaluator distinguishes the visible destination
  entity/resource, not just the filename.
- For generic back/close/up-level controls, execute a negative loop probe: navigate through the
  page pair that the trace or off-path exploration creates, click back/close twice when visible,
  and assert the evaluator or probe oracle does not accept a route cycle as progress.
- For repeated actions, start from an already-correct intermediate state, repeat the visible
  action, and assert reward follows the resulting business state rather than a historical maximum.
- For reset, complete or partially complete the refined task, reset naturally, and assert
  task-scoped state, events, transactions, checkpoint progress, and reward return to the initial
  contract before the next attempt.

## Fixing rules

- Do not add business-specific hardcoded reward branches unless the task-specific evaluator
  genuinely requires that exact entity/value.
- If a checkpoint cannot execute because the page lacks the underlying business transition, the
  owner is `implementation` (`repair-package`). If the business state exists but lacks a
  machine-readable projection, add that projection directly and re-run the affected probes.
- If the checkpoint matrix is too coarse, split or strengthen the checkpoint contract before
  adding page-side success events.
- If runtime supervision passes but a refined checkpoint probe fails, treat the aggregate as weak
  evidence and continue the fixloop.
- If the only available evidence is noisy thought/action text, do not create a reward checkpoint;
  record it as unsupported.
- If an event can fire from multiple tasks, include task id and canonical entity/state identifiers
  in the payload or precondition.
- If a generated route/back fix changes the observable filename sequence, update or explicitly
  revalidate the corresponding test flow and evaluator. Do not leave a stale flow that expects
  browser-history behavior, and do not add a reward for a generic back action unless it is
  source-supported task progress.

For a reward false positive, create or strengthen intermediate checkpoints instead of adding
another final success shortcut.

## Common pitfalls

- Do not equate final success with full instrumentation coverage.
- Do not count an event as useful if the evaluator never reads it.
- Do not count an evaluator as useful if it only checks event existence for a complex state
  change.
- Do not trust placeholder payloads, constant fixtures, or stale filenames.
- Do not create a checkpoint for a state transition that the generated UI cannot actually execute.
- Do not skip intermediate modal, picker, selection, cart footer, or submit-enabled states just
  because the trace eventually reaches checkout or payment.
- Do not claim a mutation is covered unless visible projection and payload both reflect the
  mutation.
- Do not let the task runner's context hide no-context page bugs or premature reward.
- Do not rely on raw coordinates when a semantic selector or visible control can be used, but do
  test natural hit areas for agent-visible controls.
- Do not ignore route/back changes as "not instrumentation" by default. They can invalidate
  step-level route checkpoints, replay plans, or off-path safety oracles even when no final
  success reward changes.
