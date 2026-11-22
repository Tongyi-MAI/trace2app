---
name: closure-loop
description: "Main loop of Trace2World Algorithm 4 (environment validation and repair). Use to validate an exported environment package against its source traces slice by slice: build obligations once, then per wave execute, review, and probe on one package revision, route each attributed failure to its owner (repair-package for implementation, repair-supervision for supervision, upstream for semantics, drop for model or provider), spend at most K repairs per slice from a persistent ledger, invalidate evidence that intersects each repair's Delta, and close every slice as verified, upstream, or dropped with its exit."
---

# Closure loop

The loop of Algorithm 4 (paper Appendix B.2), run by an agent with the sibling skills as operators.
It is the operator-driven counterpart of the released `t2w-close` and keeps the same ledger,
owners, budget, and outcomes (`trace2world_pipeline.closure`):

```text
O <- BuildObligations(R); E <- E0
for k = 0..K:
    X <- Execute(E, O); z <- Review(R, X, O)
    if z = Pass: z <- Probe(E, C, O)
    if z = Pass: return Verified(E)
    if k = K or not Repairable(z | C): break
    (E, Delta) <- Repair(E, z | C); Invalidate(Delta)
return DropSlice(E, z)
```

Read `references/wave-protocol.md` before the first wave: slice binding, the directory layout,
the wave record, and the invalidation rules.

## Operators

| Algorithm 4 | Skill | Returns |
|---|---|---|
| `BuildObligations(R)` | `build-obligations` | one ready checklist per source trace |
| `Execute(E, O)` | `execute-trace` | one execution packet per trace, from reset, first blocker stops it |
| `Review(R, X, O)` | `review-trace` | pass candidate, or the first mismatch with its owner |
| `Probe(E, C, O)` | `probe-contract` | pass, or an attributed failure |
| `Repair(E, z \| C)` | `repair-package` (`implementation`), `repair-supervision` (`supervision`) | the edited package, `Delta`, a repair report |
| `Invalidate(Delta)` | `scripts/closure_ledger.py invalidate` | removed evidence and restarted traces |
| `Verified`, `DropSlice`, upstream | `scripts/closure_ledger.py record-outcome` | the slice's outcome record |

## Inputs

- `$RESULT`: the result directory; `$RESULT/package` is the environment `E0` under validation;
- `$TRACES`: the source traces `R` of the app;
- the frozen app specification `C` (`$RESULT/phase_consolidation/site_model/`);
- the slices to close: task ids from `$RESULT/package/tasks.json`, each with its source traces;
- the repair budget `K` (default 3: an initial check, three repairs, and a final check);
- `$T2W_SKILLS`: `trace2world_pipeline/skills` in this repository.

## Procedure

1. Build obligations once for every source trace of the selected slices (`build-obligations`).
   A trace whose checklist cannot become ready is not validated; its slice goes upstream.
2. For each slice, read the ledger before the first wave. A slice that already has an outcome is
   not reopened; its spent budget carries over to a new run:

   ```bash
   python "$T2W_SKILLS/closure-loop/scripts/closure_ledger.py" status \
     --ledger "$RESULT/run_management/repair_ledger.json" --slice <task_id> --budget 3
   ```

3. Run one wave on the current package revision
   (`python "$T2W_SKILLS/repair-package/scripts/package_delta.py" revision --package "$RESULT/package"`):
   execute every trace of the slice whose evidence is not retained, review each execution, and
   probe only when every review is a pass candidate. Index each packet as it is written
   (`closure_ledger.py index --kind execution|review|probe`).
4. On pass, record `verified` with the revision. Otherwise take the first failure and route it by
   owner:

   | Owner | Next |
   |---|---|
   | `implementation` | `repair-package`, if budget remains |
   | `supervision` | `repair-supervision`, if budget remains |
   | `semantics` | record `upstream`; only new source evidence and a construction revision can change `C` |
   | `model`, `provider` | record `dropped`, `exit=not_repairable` |
   | none (`evidence_blocked`, `probe_blocked`) | rerun the operator on the same revision; a persistent cause is `provider` |

5. After a repair, count it only through the ledger, which refuses an invalid report or an
   exhausted budget, then invalidate:

   ```bash
   python "$T2W_SKILLS/closure-loop/scripts/closure_ledger.py" record-repair \
     --ledger "$RESULT/run_management/repair_ledger.json" --slice <task_id> --budget 3 \
     --repair-report "$REPAIR/repair_report.json"
   python "$T2W_SKILLS/closure-loop/scripts/closure_ledger.py" invalidate \
     --evidence-index "$RESULT/run_management/evidence_index.json" --delta "$REPAIR/delta.json"
   ```

   Restarted traces execute again from step zero in the next wave; retained evidence is reused.
   A repair that returns `not_repairable` with a referral consumes no budget and closes the slice
   by its referral owner.
6. When the budget is spent and the last wave failed, record `dropped` with `exit=cap`.
7. Write each outcome and the run summary:

   ```bash
   python "$T2W_SKILLS/closure-loop/scripts/closure_ledger.py" record-outcome \
     --ledger "$RESULT/run_management/repair_ledger.json" --slice <task_id> --budget 3 \
     --outcome dropped --exit cap --failure-code <code> --owner implementation --stage review \
     --failure-packet <last packet> --waves "$RESULT/run_management/closure/<task_id>/waves.json" \
     --write "$RESULT/run_management/closure/<task_id>/outcome.json"
   t2w-close --result-dir "$RESULT" --ledger "$RESULT/run_management/repair_ledger.json" \
     --report --slice <task_id> [--slice <task_id> ...] \
     --write-report "$RESULT/run_management/closure_report.json"
   ```

   Name every selected slice with `--slice`. Without it `t2w-close` reads `$RESULT/tasks.json`,
   which a constructed result directory does not have (its tasks are in
   `phase_post_processing/tasks/tasks.json` and the exported `package/tasks.json`).

## Reporting

Report verified, upstream, and dropped slices separately over the explicit denominator of
selected slices, and split dropped slices by exit: `cap` (budget spent) and `not_repairable`
(never repairable). Never merge the two exits, and never count a slice without an outcome record.
A dropped slice is frozen out of the environment; it re-enters only through a new reconciliation
of its source evidence and a fresh compilation, which is a construction revision, not a repair.

## Never

- probe before every review of the slice passed, or accept evidence from another revision;
- repair without a reproduced, attributed failure, or route a `semantics` failure to a repair;
- edit `C`, the source traces, or the checklists inside the loop;
- reset a slice's budget, count a repair outside the ledger, or reopen a closed slice;
- keep evidence that intersects a repair's `Delta`, or resume a restarted trace mid-way;
- treat replay, sprites, a reached route, or an aggregate check as `Verified`.
