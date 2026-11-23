# Wave Protocol

## Slices

A slice is one task of the package (`tasks.json` `task_id`), the unit `t2w-close` and the ledger
use. Its traces are the source traces the task was built from; its obligations are their
checklists. Slices that share pages are still closed separately, but a repair to a shared region
invalidates every slice that consumed it.

## Layout

```text
$RESULT/run_management/
  repair_ledger.json                     ledger shared with t2w-close
  source_checklists/                     build-obligations
  host_state/                            t2w-serve state root
  execution/<trace_id>/round_<NNNN>/     execute-trace
  review/traces/<trace_id>/round_<NNNN>/ review-trace (named after the execution round)
  probes/<slice_id>/wave_<k>/            probe-contract
  repairs/<slice_id>/wave_<k>/           repair-package or repair-supervision
  evidence_index.json                    packets and the package files each consumed
  closure/<slice_id>/waves.json          one record per wave
  closure/<slice_id>/outcome.json        verified | upstream | dropped
  closure_report.json                    t2w-close --report
```

Round and wave directories are allocated by exclusive creation and never overwritten.

## Wave record

Append one object per wave to `closure/<slice_id>/waves.json`, in the shape the released loop
writes:

```json
{
  "wave": 0,
  "package_revision": "sha256:...",
  "result": "pass | fail",
  "stage": "review | probe",
  "failure": {"code": "...", "owner": "implementation", "packet": "..."},
  "repaired": ["app/phase_pagegen/pages/...html"]
}
```

`failure` is absent on pass; `repaired` lists the `Delta` of the repair made after this wave.

## Revision binding

Every packet names the package revision it was produced on (`package_delta.py revision`). Within
a wave, execution, review, and probe must share one revision. A repair changes the revision; the
next wave runs on the new one.

## Invalidation

`closure_ledger.py index` records, for each packet, the package files it consumed:

- execution: the pages its observed URLs resolve to, plus the shared regions (`tasks.json`,
  `resource_manifest.json`, `app/assets/`, `app/page_index.json`, `app/phase_sdk_generation/`,
  `app/phase_pagegen/vendor/`, `state_service/`, `data/`, and any `--shared-prefix`);
- review: the files of the execution it judged;
- probe: those files plus `evaluators/`, `state_evaluator_plan.json`, and `tasks.json`.

`closure_ledger.py invalidate` applies the released `invalidate()`: every record whose consumed
files intersect `Delta` is removed, and its trace restarts at step zero. Records that consumed
nothing in `Delta` are retained, so an unchanged slice keeps its evidence. Shared regions are in
every execution's consumed set, so a shared runtime or reset edit reaches every dependent task. A
supervision-only edit (evaluators, task checkpoints) removes probes but keeps behavior evidence;
an edit to a page, runtime, route, action host, initialization, or reset removes both.

Formal-source edits outside the package (`phase_post_processing:...` entries of `Delta`) are not
consumed by the host; their packaged mirrors are, and those carry the invalidation.

## Order inside a wave

1. Execute every trace of the slice that has no retained execution on this revision, each from a
   fresh reset.
2. Review each execution. The first non-pass review ends the wave.
3. Probe once per slice, after all reviews pass.

A trace stopped at its first blocker still gets reviewed: the review attributes the blocker.

## Repair turn

1. Select the owner's skill with the first failure of the wave.
2. The repair returns `repaired` with `Delta`, or `not_repairable` with a referral.
3. `record-repair` counts a valid `repaired` report; it refuses once the budget is spent.
4. `invalidate` with the repair's `delta.json`; then start the next wave.

## Outcomes

| Outcome | When | Record |
|---|---|---|
| `verified` | execution, review, and probes pass on one revision | `--outcome verified --package-revision <rev>` |
| `upstream` | the failure's owner is `semantics` | `--outcome upstream` with the failure |
| `dropped`, `exit=cap` | the budget is spent and the last wave failed | `--outcome dropped --exit cap` |
| `dropped`, `exit=not_repairable` | the owner is `model` or `provider` | `--outcome dropped --exit not_repairable` |

`record-outcome` refuses `exit=cap` with budget left, `upstream` for a non-semantics owner, and
`not_repairable` for an implementation or supervision failure while budget remains.
