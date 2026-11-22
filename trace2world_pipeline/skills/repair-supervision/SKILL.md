---
name: repair-supervision
description: "Repair of Trace2World Algorithm 4 (environment validation and repair) for defects owned by `supervision`. Use when probe-contract found that the package already realizes the source-supported business state but a checkpoint, state binding, event payload, evaluator, terminal policy, or test flow is missing, over-grouped, weak, stale, or reads another episode. Refines the checkpoints and evaluators against the obligation checklist and canonical business state, keeps formal sources and packaged mirrors in sync, returns Delta, and never certifies its own fix."
---

# Repair supervision

`(E, Delta) <- Repair(E, z | C)` in Algorithm 4 (paper Appendix B.2) for failures whose owner is
`supervision`, under the state-based verification contract of Appendix B.3: checkpoints represent
completed business subgoals, partial progress and terminal success stay separate, and a valid
alternative path passes when it realizes the same contracted state. The repair makes the oracle
honest about state the package already produces. Execution, review, and probes then run again on
the new revision; this skill does not decide whether the fix worked.

Read `references/supervision-contract.md` before the first edit: the checklist mapping, the
business-state model, the checkpoint DAG, the evaluator rules, and the synchronization order.

## Scope

In scope, when the business state and its transition already exist in the package:

- adding, splitting, or strengthening checkpoints bound to canonical business state;
- dependency edges and negative discriminators (wrong entity or value, premature terminal,
  stale episode, unrelated action, broken preserved state);
- event payloads, runtime state schema, and state bindings;
- evaluator assertions, the reward vector, and the terminal policy;
- test flows and action-authority rows that point at stale pages, actions, or step orders;
- a machine-readable attribute or runtime field that exposes an existing, visible state.

Out of scope:

- creating the state or transition itself, a page, an entity, a filter, or an effect: if the
  package cannot produce the state, the owner is `implementation` (`repair-package`); if `C` does
  not support it, the owner is `semantics`;
- editing the app specification, source traces, obligation checklists, or execution, review, and
  probe packets;
- weakening, deleting, or merging a checkpoint to make a probe pass, or rewarding the source click
  sequence instead of the state.

If closure would need anything out of scope, restore the edited files and return
`not_repairable` with the referral.

## Procedure

1. Read the probe packet and its raw `probe_report.json`. Reproduce the failed probe on the
   current revision; an unreproduced packet goes back to `closure-loop`.
2. Confirm the state exists: reach it through visible actions and read it from the host
   (`GET /episodes/{id}/state?kind=audit` or `/runtime`). If it does not exist, stop and refer to
   `implementation`.
3. Snapshot the package, the formal instrumentation sources, and the frozen inputs:

   ```bash
   python "$T2W_SKILLS/repair-package/scripts/package_delta.py" snapshot \
     --package "$RESULT/package" --editable "$RESULT/phase_post_processing" \
     --frozen "$RESULT/phase_consolidation/site_model" --frozen "$CHECKLISTS" \
     --output "$REPAIR/before.json"
   ```

4. Map every retained row of the affected checklists to a disposition (rewarded checkpoint,
   dependency-only state, negative discriminator, terminal assertion, projection obligation,
   diagnostic only, or unsupported), then refine the business-state model, the checkpoint DAG, and
   the evaluator for the failed slice.
5. Edit in synchronization order (state binding, schema, payload, checkpoint, evaluator, terminal
   policy, test flow) in both the formal source under `$RESULT/phase_post_processing/` and its
   packaged mirror (`$RESULT/package/evaluators/`, `tasks.json`, the runtime instrumentation
   scripts, `state_evaluator_plan.json`).
6. Rerun the released checks for the trace and the failed probe plus every probe whose
   checkpoints the edit touches, using `probe-contract`'s runner; keep its raw report unedited:

   ```bash
   python -m trace2world_pipeline.verification.runtime_checkpoint_collector \
     --result-dir "$RESULT" --trace-id <trace_id> --base-url <served-pages-url>
   python -m trace2world_pipeline.verification.negative_probe_executor \
     --result-dir "$RESULT" --trace-id <trace_id>
   python "$T2W_SKILLS/probe-contract/scripts/run_probe_plan.py" \
     --plan "$REPAIR/probe_plan.json" --host http://127.0.0.1:<port> --output-dir "$REPAIR/probes"
   ```

   The runner's `raw_assertions.json` is the raw assertion report the repair report cites, with its
   SHA-256.
7. Compute Delta and validate the report:

   ```bash
   python "$T2W_SKILLS/repair-package/scripts/package_delta.py" diff \
     --before "$REPAIR/before.json" --output "$REPAIR/delta.json"
   python "$T2W_SKILLS/repair-package/scripts/validate_repair_report.py" \
     --report "$REPAIR/repair_report.json"
   ```

## Repair report

Same schema as `repair-package` (`trace2world.repair_report.v1`) with `owner=supervision`.
`diagnosis.issue_class` is one of `missing_checkpoint`, `over_grouped_checkpoint`, `missing_emit`,
`weak_payload`, `weak_evaluator`, `stale_test_flow`, `terminal_policy`, or `stale_episode_read`.
`acceptance_expansion.adjacent_checks` lists the probe-matrix rows the edit affects. Also write
`$REPAIR/checklist_mapping.json` (one disposition per retained checklist row) and
`$REPAIR/checkpoint_dag.json` (the refined checkpoints of the slice), and cite both in
`diagnosis.rationale`.

## Never

- reward an event's existence, a route, an action count, or the source path;
- let a partial stage grant terminal success, or a historical maximum survive a repeated action;
- read evaluator state from fixtures, step context, task-id branches, or a previous episode;
- add an evaluator shortcut for state the package does not produce;
- mark the slice verified: only `closure-loop` returns `Verified`.
