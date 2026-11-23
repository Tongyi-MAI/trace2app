# Probe Matrix

Every probe runs on the same package revision as the execution and review it follows, and is
judged on canonical business state (paper Appendix B.3, Table "Mandatory probe matrix").

## Business projection

`Proj_biz` keeps the State fields and Data records that source evidence supports and drops the
route, overlay, focus, scroll, and selection variables of the view. Take it from the page
specifications, not from intuition, and write it into the plan as `business_paths`: dotted paths
into the host snapshot (`GET /episodes/{id}/state?kind=audit` for state-kernel packages,
`GET /episodes/{id}/runtime` otherwise). A plan without `business_paths` compares whole
snapshots; the report then says `full_snapshot`, and a difference may be a view change rather
than a business effect, so read it before attributing anything.

## Action contract

For a page `P` in state `x`:

- a legal action `a` in `A+_P(x)` must realize the prescribed transition:
  `Proj_biz(Exec_P(x, a)) = Proj_biz(C_P(x, a))`;
- a guard-false, wrong-type, or unbound attempt `a` in `A-_P(x)` must preserve business state and
  dispatch no route or transaction: `Proj_biz(Exec_P(x, a)) = Proj_biz(x)`.

Probe at least one illegal attempt per guarded effect of the slice (submit with a missing
required value, confirm before selection, a tap on a non-interactive region near the host). The
host's `NOT_INTERACTIVE` feedback alone is not the verdict; the unchanged business state is.

Two more attempts belong to the same contract and reuse the existing `illegal_action` and
`wrong_entity` kinds rather than adding rows:

- click an unrelated **legal** visible control and assert no task success fires. An off-path control
  that is itself a legal action of the page must leave the contracted checkpoints untouched; a
  guard-false attempt only proves the guard, not the off-path path.
- click the natural hit areas of a search, autocomplete, or list row. If the event only fires from
  an invisible handler, the component is effectively noninteractive: record it with the
  `wrong_entity` or `illegal_action` kind and name the unreachable host in the failure.

Count the negative-probe specifications alongside these attempts. The compiled
`negative_probe_specs` of the executable manifest are what `negative_probe_executor` runs, per
guarded effect and per checkpoint; zero specifications for a guarded effect is `weak_evaluator`
(`supervision`), not a fresh pass.

When the host answers a legal visible action with an error instead of a transition, read who
produced it. An unreachable or timed-out host, a browser that would not start, and a plan the
runner rejected are `probe_blocked`. An error the package itself returned while handling an action
`A+_P(x)` admits means the package's action handling prevents a valid page transition from being
observed: that is `implementation_gap` (`page_runtime_gap`), never a blocked probe. The runner
records `host_error_kind` on the probe that could not run, with `host_error_path` naming the request
and `host_error_code` the host's structured error code:

- `transport`: unreachable host, timeout, or non-JSON answer; `probe_blocked`.
- `request_rejected`: HTTP 4xx (`unknown_task`, `unknown_episode`, `invalid_request`,
  `state_kernel_not_configured`); the plan, catalog, or host configuration is wrong; `probe_blocked`.
- `server_error`: HTTP 5xx. The host reports every exception as `internal_error`, so read the
  message: an error raised by the package's own page or state handling of a legal visible action is
  `implementation_gap`; a host-side failure is `probe_blocked`.

## The matrix

| Probe (`kind`) | Required verdict | False pass it exposes | Plan expectation |
|---|---|---|---|
| Zero-action initial state (`zero_action`) | no progress, no reward | reward at launch; leaked prior state | `success: false`, `reward_max: 0` |
| Each legal partial stage (`partial_stage`) | progress only | terminal credit for route-only arrival | `success: false`, the stage's checkpoints in `reached_checkpoints`, later ones in `unreached_checkpoints` |
| Wrong entity or value (`wrong_entity`) | fail | a terminal check that ignores the bound object | `success: false` |
| Premature terminal (`premature_terminal`) | fail | success before the required mutation | `success: false`; `business_state_unchanged: true` when the attempt is guard-false |
| Repeated action (`repeated_action`) | judged on final state | double credit from a historical maximum | `same_final_state_as` and `same_reward_as` the correct-terminal probe when the repetition leaves the same state |
| Correct terminal (`correct_terminal`) | pass | a missing verifier-visible consequence | `success: true`, `reward_min` at the terminal value |
| Fresh reset, back-to-back (`fresh_reset`) | state, events, transactions, reward cleared | cross-episode leakage | the runner checks that the second episode starts from the first episode's initial state and earns nothing |
| Legitimate alternative path (`alternative_path`) | pass iff same contracted state | trace-path imitation as the reward | `same_final_state_as` the correct-terminal probe and `success: true` |

A row applies whenever the slice has the structure it tests: every task has the zero-action,
correct-terminal, and fresh-reset rows; every partial stage, bound entity, guarded terminal, and
repeatable mutation adds its row; every alternative path admitted for the task adds one. An
inapplicable row is recorded with its reason, never silently omitted.

The `route stack` commitment of `oracle-audit.md` has no row of its own in the table; build it from
the rows that already exist. Navigate through the page pair the trace or an off-path exploration
creates, then send back or close twice while it is visible, and judge it as an `illegal_action`
attempt: assert the destination each time with `expect.step_pages` (the page filename the host
reports after each action), and assert that business state does not advance
(`business_state_unchanged`). A cycle
between the same two pages must not read as progress. When the slice has no back or close control,
record the commitment in `inapplicable_rows` with that reason.

Actions in a plan are the same visible actions an agent can send (`POST /episodes/{id}/actions`):
screenshot-pixel coordinates from the current observation, typed text, swipes, back. Take them
from the execution packet's observed states; never inject state or navigate directly.

## Probe packet

`$RESULT/run_management/probes/<slice_id>/wave_<k>/probe_packet.json`:

```json
{
  "schema": "trace2world.probe_packet.v1",
  "package_revision": "<digest>",
  "slice_id": "<slice>",
  "trace_id": "<trace the probes ran on>",
  "consumed": {
    "execution_packets": ["..."],
    "review_packets": ["..."],
    "checkpoint_runtime_validation": "phase_post_processing/evaluators/checkpoint_runtime_validation.json",
    "negative_probe_report": "phase_post_processing/evaluators/evaluator_negative_probe_report.json",
    "probe_report": "probe_report.json"
  },
  "oracle_audit": {"rows": 0, "gaps": []},
  "inapplicable_rows": [{"kind": "repeated_action", "reason": "no repeatable mutation"}],
  "verdict": "pass | supervision_gap | implementation_gap | unsupported_obligation | probe_blocked",
  "failure": {
    "probe_id": "...",
    "trace_id": "...",
    "step_index": 0,
    "expected_business_state": "...",
    "observed_business_state": "...",
    "obligation_ids": ["..."],
    "owner": "supervision | implementation | semantics | provider",
    "gap": "<gap class from oracle-audit.md>",
    "dependencies": ["files or package regions the repair is expected to touch"]
  }
}
```

`failure` is absent on `pass`. `dependencies` feeds `Invalidate`: it names the package regions
whose execution, review, probe, and checkpoint evidence a repair will discard.

The top-level `trace_id` is required on every verdict, `pass` included, and `failure.trace_id`
repeats it for the reader. `closure_ledger.py index --kind probe` reads the top-level field,
binds one record to that trace, and refuses a `--trace-id` that disagrees with it. A slice whose
probes cover several traces therefore writes one probe packet per trace, each naming its own trace
and citing the shared `probe_report.json`, and indexes each. A plan that runs on one trace carries
the same `trace_id`, and `run_probe_plan.py` copies it into `probe_report.json` and
`raw_assertions.json`; a plan covering several traces omits it.

`oracle_audit.rows` counts the audit rows of `oracle-audit.md` (one per supported obligation, keyed
by `trace_id|checkpoint_id|checkpoint_kind|expected_state_id`) and `gaps` lists the non-empty `gap`
column of those rows.
