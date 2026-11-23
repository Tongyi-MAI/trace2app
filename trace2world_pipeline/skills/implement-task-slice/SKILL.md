---
name: implement-task-slice
description: "Implement of Trace2World Algorithm 5 (learner-guided task selection), plus the queue the algorithm runs on. Use to turn a supported task candidate into a validated task slice of a source-validated package: check its bindings against the task-facing index, instantiate schema-backed instances and already-supported hosts, transitions, projections, reset, and evaluator-visible outcomes, verify fresh GUI execution and affected seed-task regression, refresh the index, and requeue selections that the edit invalidated. Reproduces an EnvDefect from screening and hands it to the Algorithm 4 closure loop with the slice's remaining repair budget. Also admits alternative paths whose edges are supported. Adds no transition, guard, or effect beyond the frozen app model."
---

# Implement task slice

`Implement`, `RequeueAffected`, and the queue of Algorithm 5 (paper Appendix C.2), and the task
index and candidate binding of Appendix C.1. `diagnose-attempts` owns `PassAtFour`, `Diagnose`,
`RefineOrHold`, `Record`, and `Freeze` on the same queue:

```text
A <- {}; Q <- {(c, New) : c in C}
while Q not empty:
    (c, z) <- Pop(Q)
    (E, Delta, z) <- Implement(E, c, z)          implement-task-slice
    (A, Q) <- RequeueAffected(A, Q, Delta)        implement-task-slice
    if z != Ready: Record(c, z); continue
    (A_L, A_S) <- PassAtFour(E, c, pi_L, pi_S)    diagnose-attempts
    z <- Diagnose(c, A_L, A_S)                    diagnose-attempts
    Selectable -> A; EnvDefect -> Q; else Q <- Q + RefineOrHold(c, z)
return Freeze(E, A)                               diagnose-attempts
```

Read before the first candidate:

- `references/task-index.md`: the read-only task-facing index and its refresh;
- `references/candidate-binding.md`: what a candidate binds, the difficulty floor, diversity, and
  rejection rules;
- `references/alternative-paths.md` when a candidate or a screening attempt exposes a non-source
  route.

## Inputs

- `$RESULT/package`: a package whose seed slices `closure-loop` verified, and its revision;
- the frozen app specification `C` (`$RESULT/phase_consolidation/site_model/`), for identifiers
  and support anchors only;
- `$RESULT/task_supply/index.json`: the task-facing index (`trace2world.task_index.v1`);
- candidates (`trace2world.task_candidate.v1`) under `$RESULT/task_supply/candidates/`;
- the queue `$RESULT/task_supply/queue.json`, the closure ledger, and the evidence index of
  `closure-loop`.

## Queue

```bash
QUEUE="python $T2W_SKILLS/implement-task-slice/scripts/task_queue.py"
Q="$RESULT/task_supply/queue.json"
$QUEUE init --queue "$Q" --candidates "$RESULT/task_supply/candidates.json"
$QUEUE pop  --queue "$Q"
```

`init` admits only candidates whose binding check did not reject them. One candidate is active at a
time; it leaves the active slot by `select`, `push`, or `record`.

## Implement

For the popped candidate `c` with state `z`:

1. Rebind. Set the candidate's `index_sha256` to the current index digest and check it:

   ```bash
   python "$T2W_SKILLS/implement-task-slice/scripts/validate_task_candidate.py" \
     --candidate "$RESULT/task_supply/candidates/<id>.json" --index "$RESULT/task_supply/index.json"
   ```

   `rejected` ends here with `$QUEUE record --queue "$Q" --kind rejected --reason <first error>` (a missing
   support binding rejects the candidate; it never goes to reconciliation). `needs_implementation` lists the supported,
   not-yet-implemented items the slice must instantiate; `admissible` lists none.
2. If `z = EnvDefect`, reproduce the attributed failure on the current revision first, through
   visible actions from reset, using the screening attempt only as a description of the path
   (never replay its coordinates). Preserve the attempt as immutable evidence, attribute the
   failure to the exact owner, and repair that owner: never repair the attempt, and never blame
   the model before the attribution is made.

   Not reproduced: triage first. The finding is never silently discarded and never read as proof
   that the environment was correct.

   | Triage class | When | Route |
   |---|---|---|
   | `package_host_drift` | the current host is not the host that produced the attempt | rebuild the earlier authority or declare the attempts incomparable; the current behavior proves nothing about the old one |
   | `stale_eval_attempt` | artifacts, hashes, or attempt provenance are incomplete | bind the missing provenance or re-screen before any verdict |
   | `model_action_parse_mismatch` | the raw output and the parsed action disagree | a `model` diagnostic, not an environment defect |
   | `nondeterministic_external_dependency` | a business-critical dependency does not reproduce stably | name the dependency and its consumer; not a pass |
   | `not_environment_reproducible` | the current environment behaves correctly on the same path | re-attribute to `model` or `provider`; never a pass, and never a blind repair |

   Write the class and its evidence to `$RESULT/task_supply/implement/<candidate_id>/triage.json`
   (class, the attempts it covers, evidence paths); the candidate stays active and the queue is not
   touched. Continue at step 3 with `z = New` only after that file exists. When the finding ends the
   candidate instead, record it terminally:
   `record --kind failed --owner model|provider --status <triage class> --reason ...`.

   Reproduced: run `closure-loop` on the candidate's slice with its remaining budget from the
   shared ledger, and continue with the repaired package or stop with its outcome. The repair
   closes only when these paths hold on the new revision, each from a fresh reset:

   | Path | Required when | Acceptance |
   |---|---|---|
   | `failed_eval_path` | always | re-walk the original failing semantic path and continue from the former blocker to the terminal or the safe stop |
   | `eval_discovered_alternative` | the path's verdict is admitted | the same acceptable terminal predicate, equivalence class, and evaluator terminal; different clicks and legal variant state are allowed |
   | `canonical_or_source_path` | the task contract declares one | not regressed by the alternative repair |
   | `negative_wrong_or_premature` | the evaluator or a checkpoint is in scope | reaches neither the terminal nor full reward |
   | `repeated_and_reset` | always | repeated semantics stay correct and a new episode is uncontaminated |
   | `affected_sibling_task` | a shared surface or runtime changed | natural entry, action host, layout, and state are not regressed |
3. Snapshot the package (`repair-package/scripts/package_delta.py snapshot`, frozen roots: the
   specification and the source checklists), and bind what the edit may break before touching a
   shared surface: its current layout, the revision digest the host consumes
   (`package_delta.py revision`), its action hosts, and the affected seed-task set. Enhanced
   correctness cannot hide a seed regression, so the regression set is fixed here and not chosen
   after the edit.
4. Instantiate the task slice in the package, and nothing beyond it:
   - the task row (`tasks.json`: instruction, start page, launch state, runtime profile). The
     canonical task row and its host mirror are compared by stable task id and exact canonical JSON
     payload: a count or an id match cannot establish equality of instruction, initial state,
     reset, legal solution, or terminal contract. On a mismatch, pick the real source from current
     evidence, update the stale side (the canonical row or its host mirror) from that row, and
     rerun the exact-payload comparison; never declare the pair equal on a count, and never edit the
     authoritative side to match the stale one;
   - schema-backed entity, value, and state instances the candidate requests, with reset;
   - hosts and state-dependent transitions of indexed, already-supported operations;
   - visible projections, reset and session isolation, and the evaluator-visible terminal and
     checkpoints (following `repair-supervision`'s contract).
5. Check the slice:
   - one fresh GUI execution of the task from reset by visible actions, as `execute-trace`
     prescribes, reaching the terminal; its packet goes into the evidence index under the
     candidate id. A composed or schema-backed task has no single source execution: review it
     against the candidate's bindings and its marked authority or template sequences, never
     against a fabricated source trace, and never pair frames by index;
   - the probe matrix of `probe-contract` for the new task (at least zero action, each partial
     stage, wrong entity, premature terminal, correct terminal, fresh reset);
   - stateful controls across their initial, selected, alternative-deselected, repeated, and reset
     states on every visible projection of the state they write: one projection updating while a
     sibling stays stale fails the slice;
   - no new label displays an evaluator or constraint verdict; that is answer leakage even when
     the reward is correct;
   - affected seed-task regression. Build the slice from the real consumer graph — what the loader,
     reset, runtime, and evaluator actually consume — before calling `closure_ledger.py invalidate`
     with the slice's `delta.json`: it lists the seed traces whose evidence the edit touched, and
     each runs a fresh `closure-loop` wave on the new revision. A multi-task `tasks.json`
     contributes one canonical task row only when stable-id lookup independence is physically
     established; bind file-wide defaults, ordering, count, and index dependencies, and parser or
     loader code, whenever they can affect selection or reset. Never let a hand-picked path list
     or a hand-written "unaffected" assertion establish closure. A seed failure is an
     implementation defect of this slice: repair it within the budget or restore the snapshot and
     record the candidate.

   A masked or unreachable control in natural GUI is behavior and action-host evidence, not a
   reason to route around it: diagnose page ownership, overlay, route, state initialization, and
   actionability, repair the owning product, and rerun from a fresh reset. Never bypass it with
   hidden selectors or direct navigation.

   From the fresh-reset execution record effective GUI actions, observed source reads, state
   updates, evidence revisits, correction loops, completion time when measured, and the first
   exposed difficulty phenomenon for a failed attempt. These are descriptive calibration evidence:
   they never replace the structural floor, the authority bindings, the difficulty phenomena, the
   terminal semantics, or the diversity proof, and they are never authoring targets
   (`diagnose-attempts` owns the reward side).
6. Compute `Delta` (`package_delta.py diff`), refresh the index for the edited regions
   (`references/task-index.md`), and requeue:

   ```bash
   $QUEUE requeue-affected --queue "$Q" --delta "$IMPL/delta.json"
   ```

7. Return `Ready` (hand the candidate to `diagnose-attempts`), or record the failure with its
   owner: `$QUEUE record --queue "$Q" --kind failed --owner <owner> --reason ...`.

`$IMPL` is `$RESULT/task_supply/implement/<candidate_id>/<attempt>/`. Implementation edits count
against no budget; repairs of defects found while checking the slice go through `closure-loop`
and its ledger, keyed by the candidate's task id, so counts survive requeues and refinements.

## Statuses

| Status | Meaning | Next |
|---|---|---|
| `Ready` | slice instantiated, executed, probed, seed regression clean | `diagnose-attempts` |
| `rejected` | a binding lacks support | `record --kind rejected` |
| `failed` (`task`) | the instruction, terminal, solution policy, natural entry, or a hidden constraint of the task is wrong | `record --kind failed --owner task` |
| `failed` (`implementation`, `supervision`) | the slice could not be validated within the budget | `record --kind failed` |
| `failed` (`semantics`) | the slice would need a transition, guard, effect, page, or relation the model does not support | `record --kind failed`; nothing is reconciled here |

`record` carries `--owner` and a free-text `--status` next to `--kind` and `--reason`; use
`--status` for the exact verdict or triage class that produced the record (for example
`--status not_environment_reproducible`).

Classify the symptom before repairing it:

| Symptom | Owner |
|---|---|
| wrong instruction, terminal, solution policy, natural entry, or hidden constraint | `task` |
| the source supports the task but the page, runtime, or action host is missing, for an operation the index already lists as supported | `implementation` |
| the checkpoint or evaluator disagrees with correct behavior | `supervision` |
| the operation itself is unsupported by the frozen model | `semantics`; nothing is repaired here |

Prefer execution-preserving refinement that keeps the user goal, the constraints, the business
subtasks, the terminal value, and the difficulty. Replace the task only when those invariants
cannot be preserved, and record the removed and added goals together with the difficulty
conservation. After a task-semantics change, record the exact changed members, recompute that
task's `consumed` set in the evidence index, and mark its dependent behavior, supervision, package,
and review conclusions stale. Preserve another task only when every changed member is outside its
`consumed` set and its task, evaluator, init, reset, and shared runtime bindings remain identical.
A changed package digest is provenance evidence, not by itself a reason to restart every task.

## Never

- add a page, route, field, relation, state, transition, guard, effect, or lifecycle that the
  index does not list as supported;
- extract structure from screenshots, existing tasks, or the package to bind a candidate; bind
  only index items;
- weaken or simplify a candidate to make it implementable or passable;
- skip seed-task regression after editing a shared page, runtime, or reset;
- count an implementation attempt as a repair, or reset a slice's repair count on requeue;
- let a validator write status or emit a next action: the scripts here are read-only proof gates;
- hand-edit a stale generated projection to satisfy its own validator; retire it, or regenerate it
  from source;
- close the batch on a single task pass: a per-task pass continues the denominator;
- repeat the same diagnostic on a repeated local issue; change the diagnostic strategy, inspect the
  next upstream authority, or use a different real probe.
