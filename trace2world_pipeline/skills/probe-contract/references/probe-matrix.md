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
