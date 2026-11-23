---
name: diagnose-attempts
description: "Screening and diagnosis of Trace2World Algorithm 5 (learner-guided task selection). Use after implement-task-slice returns Ready for a candidate: run PassAtFour (four valid attempts each from the preceding learner and the fixed solver), review every attempt, attribute non-learner faults to one owner, and decide Selectable, Refinable, Held, TaskIssue, EnvDefect, or Failed; then route through RefineOrHold, Record, and Freeze on the shared task queue, and summarize learner gaps for the next authoring pass. Read-only on the package; never repairs, never simplifies a task, and never lets a screening attempt into training, reward, or formal evaluation."
---

# Diagnose attempts

`PassAtFour`, `Diagnose`, `RefineOrHold`, `Record`, and `Freeze` of Algorithm 5 (paper Appendix
C.2), on the queue that `implement-task-slice` owns (`implement-task-slice/scripts/task_queue.py`).
A candidate arrives here only with status `Ready`: its slice is instantiated, executed, probed, and
seed-regression clean on the current package revision.

This skill reads and decides. It does not edit the package, the evaluator, the task, or the
attempts; an environment fault goes back to the queue as `EnvDefect`, and `implement-task-slice`
reproduces it and runs the Algorithm 4 closure loop.

Read `references/attempt-review.md` before the first review, and `references/learner-gaps.md`
before summarizing a batch for the next authoring pass.

## Inputs

- the package `$RESULT/package` at revision `R` (`repair-package/scripts/package_delta.py revision`),
  served by `t2w-serve`;
- the candidate (`trace2world.task_candidate.v1`) and its task id in the package;
- the learner `pi_L`: the preceding learner checkpoint; the solver `pi_S`: a fixed stronger model
  (paper Appendix D.2). Both are frozen for the whole batch, with their sampling settings;
- the queue `$RESULT/task_supply/queue.json` and the closure evidence index.

`$SCREEN` is `$RESULT/task_supply/screen/<candidate_id>/`.

## PassAtFour

Run attempts of the task from reset, per policy, in launch order:

```bash
t2w-rollout --server http://127.0.0.1:<port> --task-id <task_id> \
  --llm-base-url <policy endpoint> --model <policy model> \
  --max-steps <frozen> --temperature <frozen> --top-p <frozen> \
  --messages-dir "$SCREEN/attempts/<policy>/<attempt_id>.messages" \
  --output "$SCREEN/attempts/<policy>/<attempt_id>.json"
```

- Every launched attempt is kept and listed in the screen packet with its `launch_index`, including
  timeouts, crashes (bind the error log), and attempts later found invalid, and counted in the
  packet's `coverage` block against what the launch log says was launched.
- An attempt is invalid when an infrastructure, harness, provider, reset, package, or evaluator
  fault decided its outcome (`references/attempt-review.md`). Invalid attempts are retained and
  count for nothing; launch another attempt of the same policy to fill the cell.
- Stop each policy at four valid attempts. The first four valid attempts by launch order are the
  screen; later valid attempts are listed as excess and never substituted.
- Refills are bounded by the batch's frozen refill limit. When it is exhausted, set
  `early_failure` in the packet with its owner instead of launching more.
- All attempts run on revision `R`. If the package changes during screening, the packet is stale:
  start a new one on the new revision.
- A packet from an earlier round is immutable historical review evidence: never rewrite it, and never
  reopen it to add a later round's attempts. When its attempts are still relevant, write a new packet
  for the new round that references the same immutable trajectory hashes.

Screening attempts never enter training, reward, or formal evaluation credit.

## Review and Diagnose

Review every attempt in full (`references/attempt-review.md`), write the screen packet
`$SCREEN/<round>/screen_packet.json` (`trace2world.screen_packet.v1`), then:

```bash
python "$T2W_SKILLS/diagnose-attempts/scripts/diagnose.py" verdict \
  --packet "$SCREEN/<round>/screen_packet.json" --output "$SCREEN/<round>/verdict.json"
```

The script re-reads every trajectory by hash, checks the packet's consistency, and decides in the
paper's order (non-learner faults first):

| Status | Condition | Route |
|---|---|---|
| `Incomplete` | unreviewed or unresolved attempt, finding without evidence, or fewer than four valid attempts per policy | continue the review or PassAtFour |
| `Failed` (semantics) | the task needs behavior the frozen model does not support | `Record` |
| `EnvDefect` | an implementation or supervision fault | requeue as `EnvDefect` |
| `Failed` (provider) | PassAtFour ended early on a provider or harness fault | `Record` |
| `TaskIssue` | an actionable task issue: ambiguity, terminal mismatch, answer leakage | `RefineOrHold` |
| `Held` | complete and fault-free, the solver never succeeds in four | `RefineOrHold` holds |
| `Refinable` | complete and fault-free, solver succeeds, learner mean reward > 0.8 | `RefineOrHold` hardens |
| `Selectable` | otherwise | select |

The learner mean is over the four valid learner attempts' final rewards (checkpoint credit);
solver success is terminal success. Model failures of either policy are learner or solver
evidence, not faults.

The mean is this candidate's own four valid learner attempts, never a batch or portfolio average and
never a scalar-only partial score: difficulty is not inferred from how other tasks scored. A
four-zero learner vector is not auto-admission either. It additionally needs current local
solvability evidence (the solver's successful attempt, read in full) and evaluator-meaningfulness
evidence (the checkpoint chain audited); without both, the zeros are a limitation on the packet and
the candidate is not selected on them alone. `diagnose.py verdict` enforces the second half: with
four zero learner rewards it returns `Selectable` only when `checkpoint_audit.rows` is non-empty,
every row is `confirmed_ok` or `not_applicable`, and at least one is `confirmed_ok`; otherwise it
returns `Incomplete` with `four_zero_evaluator_unaudited`.

A leaked shortcut is a fault, not a difficulty signal: when a reward arrived because the visible flow
skipped a business constraint, the candidate is `EnvDefect`, never `Selectable` or `Refinable`,
whatever the learner mean says (`references/attempt-review.md`, checkpoint rules).

## Routing

```bash
QUEUE="python $T2W_SKILLS/implement-task-slice/scripts/task_queue.py"
Q="$RESULT/task_supply/queue.json"
```

- `Selectable`: `$QUEUE select --queue "$Q" --candidate <c> --screen-packet <packet> --package-revision <R>
  --evidence-index <closure evidence index>`. Selection binds the files the candidate's execution
  evidence consumed, so a later edit that touches them requeues it.
- `EnvDefect`: `$QUEUE push --queue "$Q" --candidate <c> --state EnvDefect --packet <packet> --reason <finding ids>`.
  The finding rows (attempt, steps, symptom, expected state) are the reproduction description for
  `implement-task-slice`; it never replays the attempt's coordinates.
- `Failed`: `$QUEUE record --queue "$Q" --kind failed --owner semantics|provider --reason ... --packet <packet>`.
- `Held`: `$QUEUE record --queue "$Q" --kind held --reason "solver never succeeds" --packet <packet>`. A hard or
  solver-unsolved lineage is held, never simplified.
- `Refinable` and `TaskIssue`: RefineOrHold below.
- `Incomplete`: no queue change; the candidate stays active.

## RefineOrHold

Refinement is one-directional and bounded: a lineage is refined at most twice (the queue refuses a
third), and a refined candidate is screened again from `Implement`.

1. Decide whether a concrete supported refinement exists:
   - `Refinable` (saturated): add supported cross-state dependencies or evidence revisits from the
     index, so the task becomes more demanding within fixed support;
   - `TaskIssue`: fix the instruction, terminal set, or leak the finding names, without lowering
     any obligation.
   If none exists, or the lineage is at its second refinement, record a hold:
   `$QUEUE record --queue "$Q" --kind held --reason ...`.
2. Author the refined candidate `c'` under `implement-task-slice/references/candidate-binding.md`
   and check it:

   ```bash
   python "$T2W_SKILLS/implement-task-slice/scripts/validate_task_candidate.py" --candidate c2.json --index "$RESULT/task_supply/index.json"
   python "$T2W_SKILLS/diagnose-attempts/scripts/diagnose.py" check-refinement \
     --parent c.json --child c2.json --mode harden    # or task-issue
   ```

   `check-refinement` fails when `c'` drops a bound item, a required phenomenon, or the long-horizon
   class, or (in harden mode) adds no dependency or revisit.
3. `$QUEUE push --queue "$Q" --candidate <c'> --state New --refine-of <c> --reason <status and finding ids>`.

## Record and Freeze

`Record` logs every failed or held candidate with its owner and packet; exhausted slices (repair
budget spent in the closure loop) are dropped through `record --kind failed`. When the queue is
empty:

```bash
$QUEUE freeze --queue "$Q" --package-revision <R_final> --output "$RESULT/task_supply/batch.json"
```

`freeze` refuses while the queue holds work or when a selection's evidence was produced on another
revision and was not kept by `requeue-affected`. The batch binds the selected tasks and their
current evidence to the final package revision; screening attempts and records stay in
`$RESULT/task_supply/` for independent failure reproduction.

After freezing, summarize learner gaps over the batch's learner attempts for the next authoring
pass (`references/learner-gaps.md`).

## Never

- edit the package, evaluator, task, trajectories, or messages; or call a policy outside
  PassAtFour;
- count an invalid attempt, substitute a later valid attempt, or reuse attempts from another
  revision;
- infer a fault from a score, or a learner gap from an environment, provider, or solver failure;
- simplify a held or hard lineage, or refine past the second round;
- read a host that reports ready as the requested package loaded: confirm the root `t2w-serve` serves
  is the package at revision `R` before reading any attempt from it;
- read a target-model score as environment path correctness: a score says what that policy did, not
  that the path the task requires is correct;
- let a screening attempt into training data, reward, or formal evaluation.
