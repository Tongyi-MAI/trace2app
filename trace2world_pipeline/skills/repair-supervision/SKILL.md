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
Read `references/checkpoint-fixloop.md` when a refined checkpoint fails a probe: artifact
discovery, the static audit of the five instrumentation layers, the focused replay and off-path
probe recipes, the fixing rules, and the pitfalls.

Refinement is the primary operation: correct weak existing checkpoint semantics, augment missing
business-state checkpoints, and strengthen evaluator dependencies and negative discriminators. The
fixloop is the bounded execution repair that follows, entered only for failures of the
already-materialized refined contract; it is not a substitute for refinement. Existing checkpoint
rows are implementation evidence, not the authority.

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

Never edit, ignore, or reinterpret a ready checklist to make a probe pass. If live evidence proves
the checklist is stale or the source evidence changed, preserve the failed attempt, return to
`build-obligations`, regenerate or project the checklist, invalidate the affected mapping and
packets with `closure-loop`'s `closure_ledger.py invalidate`, and restart refinement. A runner or
infrastructure gap that no package edit can close is a `not_repairable` referral with owner
`provider`.

If closure would need anything out of scope, restore the edited files and return
`not_repairable` with the referral. The report is checked by
`repair-package/scripts/validate_repair_report.py`, so the referral carries `owner` (`semantics`
or `provider`), `unsupported_behavior`, `why_package_fix_impossible`, `attempted_legal_repairs`,
`missing_or_conflicting_authority`, and `user_or_external_change_required`.

## Procedure

1. Bind the scope before the first edit: the exact result/package directory, the output dir, the
   source traces and the checklist set, the task and trace ids, the served root, the current
   artifact hashes, and the baseline evaluator metrics. The named result/package is the only
   source of truth; do not inspect or patch a sibling rerun. Then read the probe packet and its
   raw `probe_report.json` and reproduce the failed probe on the current revision; an unreproduced
   packet goes back to `closure-loop`.
2. Confirm the state exists: reach it through visible actions and read it from the host
   (`GET /episodes/{id}/state?kind=audit` or `/runtime`). If it does not exist, stop and refer to
   `implementation`.
3. Run the correctness admission before refining anything:

   - the required visible action host exists;
   - the business transition can be executed;
   - the resulting business state exists;
   - the terminal state is deterministic;
   - reset and session isolation work;
   - evaluator state has a real producer;
   - the correct visible-action path can complete.

   If any item fails, the owner is `implementation` (`repair-package`). Correctness repairs are
   prerequisites and are never counted as refinement gains; refinement begins only after this
   admission passes.
4. Snapshot the package, the formal instrumentation sources, and the frozen inputs:

   ```bash
   python "$T2W_SKILLS/repair-package/scripts/package_delta.py" snapshot \
     --package "$RESULT/package" --editable "$RESULT/phase_post_processing" \
     --frozen "$RESULT/phase_consolidation/site_model" --frozen "$CHECKLISTS" \
     --output "$REPAIR/before.json"
   ```

   With the same snapshot, write `$REPAIR/baseline_metrics.json`:

   - checkpoint count and reward levels;
   - terminal success behavior;
   - mean raw reward and raw reward standard deviation when probe data exists;
   - constant-zero, constant-one, and constant-partial behavior;
   - wrong-entity false-positive rate;
   - correct-terminal false-negative rate;
   - reset contamination.

   The baseline is what proves the repair improved discrimination instead of only turning one
   probe green.
5. Map every retained row of the affected checklists to a disposition (rewarded checkpoint,
   dependency-only state, negative discriminator, terminal assertion, projection obligation,
   diagnostic only, or unsupported), then refine the business-state model, the checkpoint DAG, and
   the evaluator for the failed slice.
6. Edit in synchronization order (state binding, schema, payload, checkpoint, evaluator, terminal
   policy, test flow) in both the formal source under `$RESULT/phase_post_processing/` and its
   packaged mirror (`$RESULT/package/evaluators/`, `tasks.json`, the runtime instrumentation
   scripts, `state_evaluator_plan.json`). Locate the artifacts by pattern because names vary;
   `references/checkpoint-fixloop.md` lists the released layout. If the compiled test-flow layer
   is missing because JS injection was skipped, that is not a harmless skip: report which layer is
   missing — no test flow, no emitted event, no evaluator, or no runner reward binding.
7. Verify narrowly first, then broadly. For the failed checkpoint: run a focused trace shard of
   the collector for that trace id; add a DOM/event probe for the exact checkpoint when replay or
   runtime aggregates can hide the defect; and always run at least one negative or
   premature-success probe when fixing a reward false positive. Broaden only after the focused
   check passes: rerun the full modes when the fix changed shared artifacts, and refresh the
   canonical artifacts under the result dir —
   `phase_post_processing/evaluators/checkpoint_runtime_validation.json` and
   `phase_post_processing/evaluators/evaluator_negative_probe_report.json` — after changing
   checkpoint, evaluator, or test-flow artifacts. Per-trace evidence folders are useful but do not
   satisfy freshness by themselves. Report any unrelated residual pending checkpoints separately
   from the trace being fixed. Then rerun every probe whose checkpoints the edit touches, using
   `probe-contract`'s runner; keep its raw report unedited:

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
8. Compute Delta and validate the report:

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

Write `$REPAIR/business_state_model.json` (the contract's §2 fields for every rewarded field) and
`$REPAIR/reward_vector_design.json` (the §4 vector and its weights), plus an artifact index
recording source and output hashes, consumers, traces, probes, and freshness. The canonical
result/package consumers must also be updated: sidecars alone do not complete the refinement.

`validate_repair_report.py` does not require the following, so they are report content rather
than schema. For each repaired trace, report:

- result dir and verified served root;
- trace id and step range;
- the checklist path, its SHA-256, and the mapping path with its SHA-256;
- execution packet path and status, review packet path and status;
- checkpoint matrix summary;
- failed checkpoints by layer: state producer, projection, plan, emit, payload, evaluator, flow,
  runner, reset;
- patches made and files touched;
- replay evidence folder;
- events observed before and after each checkpoint;
- evaluator and reward status before and after the required action;
- negative and off-path probes run;
- remaining failed or unsupported checkpoints and residual risk.

## Never

- reward an event's existence, a route, an action count, or the source path;
- let a partial stage grant terminal success, or a historical maximum survive a repeated action;
- read evaluator state from fixtures, step context, task-id branches, or a previous episode;
- add an evaluator shortcut for state the package does not produce;
- for a reward false positive, add another final success shortcut: create or strengthen
  intermediate checkpoints instead;
- mark the slice verified: only `closure-loop` returns `Verified`.
