---
name: probe-contract
description: "Probe of Trace2World Algorithm 4 (environment validation and repair). Use after review-trace returns a pass candidate for the same environment digest: audit that every supported obligation is carried by a business-state checkpoint, producer, visible projection, and evaluator consumer, then run the mandatory probe matrix (zero action, partial stages, wrong entity, premature terminal, repeated action, correct terminal, fresh reset, legitimate alternative path) and the legal/illegal action contract. Judges canonical business state, never screenshots. Reports Pass or one attributed failure; it never edits the environment, checkpoints, or evaluators."
---

# Probe contract

`z <- Probe(E, C, O)` in Algorithm 4 (paper Appendix B.2), applying the state-based verification
contract and the mandatory probe matrix of Appendix B.3. Probe runs only after `review-trace`
returned `review_pass_candidate` on the same package revision. It reports; it does not repair. A
failure is a defect packet that names the step or probe, the expected and the observed business
state, and the affected obligations, with an owner that tells `closure-loop` which repair skill
receives it.

Read both references before writing a verdict:

- `references/oracle-audit.md`: the audit row and its stable key, row roles, obligation-to-checkpoint
  coverage, the gap classes, the drift checks, and the false-pass patterns;
- `references/probe-matrix.md`: how to build each probe, the action contract, and the packet.

## Inputs

- the package under validation (`$RESULT/package`) and its revision digest;
- the frozen page specifications `C` and the per-trace obligation checklists `O` from
  `build-obligations`;
- the current `execute-trace` packet and the `review-trace` pass candidate for every trace in the
  slice, both bound to that digest;
- the instrumentation the package carries: `phase_post_processing/instrumentation/`
  (`checkpoint_matrix.json`, `runtime_state_schema.json`, `runtime_event_bindings.json`,
  `instrumentation_state_bindings.json`), `phase_post_processing/evaluators/` (`evaluators.json`,
  `executable_evaluator_manifest.json`, `evaluator_state_bindings.json`),
  `phase_post_processing/action_authority/action_execution_contracts.json`, and
  `phase_post_processing/terminal_policy/terminal_policy_verified.json`;
- the packaged mirrors of that contract, which are what the host actually runs and what every probe
  consumes: `$RESULT/package/evaluators/`, `$RESULT/package/state_evaluator_plan.json`, and
  `$RESULT/package/tasks.json` (the `PROBE_EXTRA` set of `closure-loop/scripts/closure_ledger.py`; a
  state-kernel package ships the plan as `evaluators/state_evaluator_plan.json`);
- a probe directory `$RESULT/run_management/probes/<slice_id>/wave_<k>/`.

Audit the formal source and the packaged mirror together and keep them aligned: a probe that reads
only the formal source can pass while the evaluator the host runs is stale.

## Procedure

1. Bind the digest and lock the scope. Every execution, review, and probe consumed below must name
   the same package revision; anything older is stale and goes back to `execute-trace`. The named
   result directory is the only source of truth: do not inspect a sibling rerun, do not mix result
   directories, and treat evidence produced against another result directory as invalid. The host
   must serve that exact package, and a reused server must be confirmed to serve the requested
   package before any probe runs.
2. Audit the oracle (`references/oracle-audit.md`). Write one audit row per supported obligation in
   `O`, keyed by the released stable key, and read its role before demanding an evaluator consumer:
   a blocking reward row needs the checkpoint that represents it, the canonical producer or reducer
   that writes it after the triggering action, the visible projection, and the evaluator field that
   consumes it; a progress-only or diagnostic row needs the producer and the visible projection and
   is proven by the matrix, not by an evaluator field of its own. Partial progress and terminal
   success stay separate. A missing, over-grouped, weakly asserted, or stale link is a failure even
   if every aggregate is green. A missing instrumentation file is never a harmless skip: name the
   layer that is missing.
3. Refresh the released checks for the slice on this digest:

   ```bash
   python -m trace2world_pipeline.verification.runtime_checkpoint_collector \
     --result-dir "$RESULT" --trace-id <trace_id> --base-url <served-pages-url>
   python -m trace2world_pipeline.verification.negative_probe_executor \
     --result-dir "$RESULT" --trace-id <trace_id>
   ```

   They write `phase_post_processing/evaluators/checkpoint_runtime_validation.json` and
   `evaluator_negative_probe_report.json`. These cover the compiled negative-probe specifications;
   they do not replace the matrix below.

   Count those specifications before trusting either report. The executor runs only the
   `negative_probe_specs` compiled into the executable manifest, so a manifest with zero or thin
   specifications yields a green, freshly written report that probed nothing; zero specifications
   for a guarded effect is `weak_evaluator` (`supervision`), not a fresh pass.

   Freshness is per slice. The collector merges a `--trace-id` run into
   `checkpoint_runtime_validation.json` (`collection_mode.mode = trace_scoped_merge`) unless
   `--replace-output` is given, so the canonical file can still carry other traces' rows from an
   older revision. A focused trace-local log is not closure when the packet cites the canonical
   output: rerun the producer instead, and report unrelated residual pending rows separately, never
   merged into this slice's verdict.
4. Write a probe plan covering every applicable row of the probe matrix and the illegal-action
   attempts (`references/probe-matrix.md`), serve the package, and run it:

   ```bash
   t2w-serve --environment-dir "$RESULT/package" --state-root "$RESULT/run_management/host_state" \
     --host 127.0.0.1 --port <port> &
   python "$T2W_SKILLS/probe-contract/scripts/run_probe_plan.py" \
     --plan "$RESULT/run_management/probes/<slice_id>/wave_<k>/probe_plan.json" \
     --host http://127.0.0.1:<port> \
     --output-dir "$RESULT/run_management/probes/<slice_id>/wave_<k>"
   ```

   The runner opens fresh episodes, performs only the plan's visible actions, snapshots business
   state before and after, evaluates, and writes `probe_report.json`. Exit 0 means every probe
   passed, 1 that a probe failed, 2 that a probe could not run. Confirm the served revision before
   reading the report.
5. Write the probe packet: `pass` only when the oracle audit is clean, the released checks are
   fresh and pass, and every applicable probe passed on the digest; otherwise the first failure
   with its owner. The packet names its trace: `trace_id` is a top-level field, present on `pass`
   as well as on a failure. `closure_ledger.py index --kind probe` binds each record to that one
   trace and refuses a `--trace-id` that disagrees with it, so a slice whose probes cover several
   traces writes one packet per trace (each may cite the same `probe_report.json`) and indexes each.

## Verdicts and owners

| Verdict | Meaning | Owner | Repairable |
|---|---|---|---|
| `pass` | oracle audit clean and every applicable probe passes on the digest | none; `closure-loop` returns `Verified` | n/a |
| `supervision_gap` | the package already realizes the state, but a checkpoint, binding, payload, evaluator, terminal policy, or test flow is missing, grouped, weak, or stale | `supervision` | yes, `repair-supervision` |
| `implementation_gap` | the state is supported by `C`, but the package cannot produce, persist, project, or reset it, or an illegal attempt changes business state or dispatches, or the package's own action handling rejects or swallows a legal visible action | `implementation` | yes, `repair-package` |
| `unsupported_obligation` | closing the failure needs a state, relation, guard, or effect that no source evidence supports | `semantics` | no, leaves upstream |
| `probe_blocked` | the host, browser, or plan could not run for a cause outside the package (runner exit 2) | none: rerun; a persistent infrastructure cause is `provider` | n/a |

`supervision`, `implementation`, `semantics`, and `provider` are the released `closure.Owner`
values. When a defect could be closed in either layer, attribute it to the lowest layer that makes
the oracle honest: if the page never writes the state, it is `implementation`, and adding an
evaluator shortcut would hide it.

`probe_blocked` is reserved for a host, browser, or plan failure the package did not cause: an
unreachable or timed-out host, a browser that would not start, a plan the runner rejected. A host
error the package itself produced while handling a legal visible action is not blocked. The package
rejecting, swallowing, or failing on an action that `A+_P(x)` admits violates the action contract
and is `implementation_gap`; do not let it drop the slice as an infrastructure cause. Step or action
context injected by the runner is the same class: the state must hold without it, so probe it
without it (`repair-package`'s `natural_action_then_context_ablation` and
`valid_app_state_without_step_context` cases) rather than letting runner context hide a no-context
page bug or a premature reward.

## Never

- judge a probe from a screenshot, a reached route, an event name, or a green aggregate;
- accept trace-path imitation as the reward: a legitimate alternative path passes iff it realizes
  the same contracted state;
- run probes on a different digest from the execution and review they follow, or against a host
  serving another result directory;
- edit the package, checkpoints, evaluators, or obligations, or add a success shortcut;
- report `pass` while any applicable probe row is missing from the plan;
- report `pass` on a negative-probe report that ran no probe, or fold another trace's residual rows
  into this slice's verdict.
