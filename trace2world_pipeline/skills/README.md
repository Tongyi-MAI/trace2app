# Trace2World agent skills

The automatic construction path (`t2w-construct`, `t2w-verify`, `t2w-export-environment`, ...)
runs bounded model calls with fixed inputs. Two stages of the paper are agentic instead: an agent
drives the environment, reads evidence, and edits the package under fixed contracts. These skills
are those contracts, one per operator or group of operators:

- **Algorithm 4, environment validation and repair** (paper Appendix B.2): validate an exported
  package against its source traces, slice by slice, and repair it within a bounded budget.
- **Algorithm 5, learner-guided task selection** (paper Appendix C): implement supported task
  candidates in a validated package, screen them with the learner and a fixed solver, and freeze
  the next training batch.

Each skill is a directory with a `SKILL.md` (front matter `name` and `description`, then the
procedure), `references/` read on demand, and `scripts/` that check or record what the procedure
produces. Load the directory into any coding agent that reads `SKILL.md` skills, or give the agent
the relevant `SKILL.md` as its instructions.

## Operators

| Algorithm | Operator | Skill | Scripts |
|---|---|---|---|
| 4 | `BuildObligations` | [`build-obligations`](build-obligations/SKILL.md) | `scaffold_tracewise_checklists.py`, `render_marked_trace.py`, `validate_tracewise_checklists.py`, `reduce_tracewise_checklist_set.py` |
| 4 | `Execute` | [`execute-trace`](execute-trace/SKILL.md) | `validate_gui_execution_packet.py` |
| 4 | `Review` | [`review-trace`](review-trace/SKILL.md) | `build_review_sprite.py` |
| 4 | `Probe` | [`probe-contract`](probe-contract/SKILL.md) | `run_probe_plan.py` |
| 4 | `Repair` (owner `implementation`) | [`repair-package`](repair-package/SKILL.md) | `package_delta.py`, `audit_state_carriers.py`, `validate_repair_report.py` |
| 4 | `Repair` (owner `supervision`) | [`repair-supervision`](repair-supervision/SKILL.md) | uses `repair-package` and `probe-contract` scripts |
| 4 | main loop, `Invalidate`, ledger, outcomes | [`closure-loop`](closure-loop/SKILL.md) | `closure_ledger.py`, `t2w-close` |
| 5 | `Implement`, `RequeueAffected`, queue, task index, candidate binding, alternative paths | [`implement-task-slice`](implement-task-slice/SKILL.md) | `task_queue.py`, `validate_task_candidate.py`, `validate_alternative_path_packet.py` |
| 5 | `PassAtFour`, `Diagnose`, `RefineOrHold`, `Record`, `Freeze`, learner gaps | [`diagnose-attempts`](diagnose-attempts/SKILL.md) | `diagnose.py` |

## Flow

Algorithm 4, per selected slice (one task and its source traces), with a repair budget of three
waves:

```text
build-obligations (once per source trace)
for each wave on one package revision:
    execute-trace -> review-trace -> probe-contract          -> Verified
    failure owned by implementation  -> repair-package       -> invalidate, next wave
    failure owned by supervision     -> repair-supervision   -> invalidate, next wave
    failure owned by semantics       -> UpstreamReferral
budget spent or not repairable       -> DroppedSlice (exit cap | not_repairable)
```

Algorithm 5, over the candidates authored from the validated package's task-facing index:

```text
queue <- candidates
while queue not empty:
    implement-task-slice: Implement, RequeueAffected      (EnvDefect -> Algorithm 4 on the slice)
    diagnose-attempts:    PassAtFour, Diagnose
        Selectable -> batch;  EnvDefect -> queue;  Refinable / TaskIssue / Held -> RefineOrHold
diagnose-attempts: Freeze -> batch.json
```

Neither algorithm changes the frozen app specification. A failure that would need a new page
identity, relation, state, guard, or effect is referred upstream, not repaired.

## Shared contracts

- Owners (`trace2world_pipeline.closure.owners`): `semantics`, `implementation`, `supervision`,
  `model`, `provider`. Only `implementation` and `supervision` are repairable.
- Outcomes (`trace2world_pipeline.closure.outcomes`): `Verified`, `UpstreamReferral`,
  `DroppedSlice` with exit `cap` or `not_repairable`.
- Repair budget: three waves per slice, counted in one ledger
  (`trace2world.repair_ledger.v1`) that survives requeues and refinements.
- Package revision: `repair-package/scripts/package_delta.py revision`. Every execution, review,
  probe, and screening packet names the revision it ran on and goes stale when an edit touches the
  files it consumed.
- Screening attempts never enter training, reward, or formal evaluation.

## Setup

```bash
pip install -e ./trace2world_pipeline        # t2w-close, the closure package, Pillow
pip install -e ./trace2world_env             # t2w-serve, t2w-rollout
export T2W_SKILLS="$PWD/trace2world_pipeline/skills"
export RESULT=<result directory of one app>  # written by t2w-construct
export TRACES=<source traces of that app>    # <trace_id>/trajectory.json + screenshots/
```

Scripts run on Python 3.10 or newer with the standard library only, except Pillow
(`render_marked_trace.py`, `build_review_sprite.py`) and the installed pipeline
(`closure_ledger.py`, which therefore needs the pipeline's Python 3.12). Each script documents its
arguments with `--help`.

## Result layout

```text
$RESULT/
  package/                                  exported environment package under validation
  phase_consolidation/site_model/           frozen app specification (identifiers and support anchors)
  run_management/
    reference_stepwise_checklists/          build-obligations
    execution/<trace_id>/round_<NNNN>/       execute-trace
    review/traces/<trace_id>/<round>/        review-trace
    probes/<slice_id>/wave_<k>/              probe-contract
    repairs/<slice_id>/wave_<k>/             repair-package, repair-supervision
    closure/<task_id>/                       closure-loop waves and outcome
    repair_ledger.json                       shared repair counts and outcomes
    evidence_index.json                      which package files each packet consumed
    closure_report.json                      t2w-close report
  task_supply/
    index.json                               task-facing index
    candidates/, candidates.json             authored candidates
    queue.json                               Algorithm 5 queue, selections, records
    implement/<candidate_id>/<attempt>/      implement-task-slice
    alternative_paths/<task_id>.json         alternative-path packets
    screen/<candidate_id>/<round>/           PassAtFour attempts, screen packet, verdict
    batch.json                               frozen batch
```
