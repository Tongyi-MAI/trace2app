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
   (never replay its coordinates). Not reproduced: record why and continue at step 3 with
   `z = New`. Reproduced: run `closure-loop` on the candidate's slice with its remaining budget
   from the shared ledger, and continue with the repaired package or stop with its outcome.
3. Snapshot the package (`repair-package/scripts/package_delta.py snapshot`, frozen roots: the
   specification and the source checklists).
4. Instantiate the task slice in the package, and nothing beyond it:
   - the task row (`tasks.json`: instruction, start page, launch state, runtime profile);
   - schema-backed entity, value, and state instances the candidate requests, with reset;
   - hosts and state-dependent transitions of indexed, already-supported operations;
   - visible projections, reset and session isolation, and the evaluator-visible terminal and
     checkpoints (following `repair-supervision`'s contract).
5. Check the slice:
   - one fresh GUI execution of the task from reset by visible actions, as `execute-trace`
     prescribes, reaching the terminal; its packet goes into the evidence index under the
     candidate id;
   - the probe matrix of `probe-contract` for the new task (at least zero action, each partial
     stage, wrong entity, premature terminal, correct terminal, fresh reset);
   - affected seed-task regression: `closure_ledger.py invalidate` with the slice's `delta.json`
     lists the seed traces whose evidence the edit touched; each runs a fresh `closure-loop` wave
     on the new revision. A seed failure is an implementation defect of this slice: repair it
     within the budget or restore the snapshot and record the candidate.
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
| `failed` (`implementation`, `supervision`) | the slice could not be validated within the budget | `record --kind failed` |
| `failed` (`semantics`) | the slice would need a transition, guard, effect, page, or relation the model does not support | `record --kind failed`; nothing is reconciled here |

## Never

- add a page, route, field, relation, state, transition, guard, effect, or lifecycle that the
  index does not reference as supported;
- extract structure from screenshots, existing tasks, or the package to bind a candidate; bind
  only index items;
- weaken or simplify a candidate to make it implementable or passable;
- skip seed-task regression after editing a shared page, runtime, or reset;
- count an implementation attempt as a repair, or reset a slice's repair count on requeue.
