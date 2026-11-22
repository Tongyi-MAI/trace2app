---
name: execute-trace
description: "Execute of Trace2World Algorithm 4 (environment validation and repair). Use when a source workflow must be replayed in a compiled environment through natural GUI operations only: start at native reset in a fresh episode, perform the recorded actions on visible hosts, capture every observed state, and stop at the first operational blocker. Produces an execution packet; it never reviews, compares screenshots, injects state, or repairs."
---

# Execute trace

`X <- Execute(E, O)` in Algorithm 4 (paper Appendix B.2). Execution starts each affected trace
at native reset in a fresh browser context, records visible GUI operations, and stops at the
first blocker. It cannot access the review oracle, inject state, or repair. The packet it writes
is the only execution evidence `review-trace` may read.

Read `references/pure-gui-execution-contract.md` before executing a trace or accepting a packet.

## Inputs

- the exported package under validation (`$RESULT/package`) and its current revision, printed by
  `python "$T2W_SKILLS/repair-package/scripts/package_delta.py" revision --package "$RESULT/package"`;
- the trace id, its instruction, and its replay task: the package task (or launch specification)
  whose start page and initial state reproduce the trace's launch state at reset;
- from the obligation set, per retained row, only the action to perform (`operation_intent`,
  `source_host`). Expected and forbidden states are the review oracle; execution does not read
  them;
- one new round directory `$RESULT/run_management/execution/<trace_id>/round_<NNNN>/`.

## Drivers

Use the environment host by default. It exposes exactly the public interface a learner sees:

```bash
t2w-serve --environment-dir "$RESULT/package" --state-root "$RESULT/run_management/host_state" --host 127.0.0.1
```

`POST /episodes {task_id}` with the replay task opens a fresh browser context at its reset state;
then repeat `GET /episodes/{id}/observation` and `POST /episodes/{id}/actions` in
screenshot-pixel coordinates. A rejected action returns `NOT_INTERACTIVE` (or another
`ERROR[...]` line) and is recorded as an observed outcome. The read-only inspection endpoints
(`/page`, `/runtime`,
`/state`) may be logged for diagnosis but never used to choose an action.

Driving the package pages directly with Playwright is allowed when richer evidence is needed
(page errors, console, locator basis). The same boundary applies: one initial navigation to the
reset entry, then visible operations only.

## Procedure

1. Allocate the round directory with an exclusive create; if it exists, take the next number.
   Never reuse, clear, or overwrite a round, including stopped or failed ones.
2. Start a fresh episode and capture the initial full viewport.
3. Perform the recorded actions in order on visible hosts: visible role/name/text, visible input,
   keyboard, wheel, touch, drag, or a visually verified coordinate on the owning surface.
4. Save one screenshot before each attempted action and one after each action or blocker, with an
   absolute path inside `screenshots/` of the round.
5. Record locator basis, owning surface, URL/page changes, page and console errors, and the raw
   action outcome.
6. Stop at the first operational blocker (see the contract). Continue past a non-blocking anomaly
   only when later actions stay naturally reachable and the extra evidence is useful; record
   `continued_after_anomaly=true` and the rationale.
7. Write `gui_execution_packet.json` in the round directory and validate it:

   ```bash
   python "$T2W_SKILLS/execute-trace/scripts/validate_gui_execution_packet.py" \
     --execution-packet "$ROUND/gui_execution_packet.json"
   ```

## Statuses

- `execution_complete`: the recorded workflow was executed naturally to its end.
- `stopped_at_first_blocker`: execution stopped at the first observed operational blocker; the
  executed prefix stays valid evidence.
- `execution_invalid`: a forbidden shortcut or a provenance violation occurred; nothing in the
  round may support validation.
- `evidence_blocked`: the host entry, browser, trace binding, or output cannot be established.

`execution_complete` is not a pass: only `review-trace` and `probe-contract` decide that.
`stopped_at_first_blocker` is a valid result, not an error to hide or route around.

## Packet

```json
{
  "schema": "trace2world.gui_execution.packet.v1",
  "trace_id": "...",
  "task_id": "...",
  "package_revision": "<package_delta.py revision output>",
  "status": "stopped_at_first_blocker",
  "stop_policy": "first_operational_blocker",
  "driver": "host_api",
  "pure_gui_execution": true,
  "review_performed": false,
  "sprites_generated": false,
  "pairwise_alignment_claimed": false,
  "intermediate_direct_navigation_used": false,
  "action_id_locator_used": false,
  "forced_or_dom_click_used": false,
  "state_or_query_injection_used": false,
  "inspection_endpoint_used_for_action_choice": false,
  "entry": "POST /episodes or the reset URL",
  "execution_attempt_root": "<absolute round directory>",
  "screenshot_output_dir": "<execution_attempt_root>/screenshots",
  "artifact_isolation_verified": true,
  "capture_program_path": "<execution_attempt_root>/capture_program.py",
  "raw_browser_log_path": "<execution_attempt_root>/raw_browser_log.json",
  "observed_states": [],
  "action_attempts": [],
  "executed_action_count": 0,
  "recorded_action_count": 0,
  "first_blocker": {},
  "continued_after_anomaly": false,
  "generated_at": "..."
}
```

## Never

- open source screenshots, sprites, or obligation states, or issue a fidelity verdict;
- jump to a later step after a blocker, or load or inject a later state;
- patch the environment during a round;
- claim a review, probe, or validation pass.
