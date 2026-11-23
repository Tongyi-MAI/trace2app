# Attempt Review

How a screening attempt is read and recorded before `diagnose.py verdict` decides. Review is
read-only: trajectories, messages, screenshots, evaluator output, and the package are immutable
evidence, referenced by path and SHA-256.

## Boundaries

1. A reward or score distribution is a triage index. It never names a missing checkpoint or an
   owner by itself.
2. A visible terminal page is not business success, and a checkpoint event is not visible success.
3. Static analysis of the package or evaluator establishes a risk or a contract defect, not a
   runtime false positive or negative.
4. One attempt may carry several findings; each finding has exactly one owner.
5. Review never calls a policy, edits anything, or reruns the attempt; live reproduction belongs to
   `implement-task-slice` after an `EnvDefect`.

## Screen packet

```json
{
  "schema": "trace2world.screen_packet.v1",
  "candidate_id": "meituan_c017", "task_id": "meituan_aug_017", "lineage_id": "meituan_c017", "round": 1,
  "package_revision": "sha256:...",
  "policies": {
    "learner": {"model": "<served name>", "checkpoint": "<iteration>", "temperature": 0.0, "max_steps": 45},
    "solver":  {"model": "<served name>", "temperature": 0.0, "max_steps": 45}
  },
  "early_failure": null,
  "attempts": [
    {
      "attempt_id": "l1", "policy": "learner", "launch_index": 0, "package_revision": "sha256:...",
      "trajectory_path": "attempts/learner/l1.json", "trajectory_sha256": "...",
      "messages_path": "attempts/learner/l1.messages",
      "validity": "valid", "invalid_reason": null, "reviewed": true,
      "last_verified_state": "...", "first_known_failure_step": 9, "path_verdict": "reasonable",
      "finding_ids": ["f2"]
    }
  ],
  "coverage": {
    "attempts_expected": 8, "attempts_indexed": 8, "attempts_reviewed": 8,
    "all_attempts_indexed": true, "all_attempts_reviewed": true, "all_steps_read": true,
    "score_only_claims": false,
    "cluster_method": "same host, route, state field, or checkpoint producer"
  },
  "findings": [
    {
      "finding_id": "f2", "attempt_ids": ["l1"], "step_range": [9, 12],
      "category": "model_failure", "owner": "model", "evidence_state": "trace_confirmed",
      "symptom": "...", "expected": "...", "evidence": ["attempts/learner/l1.json#steps[8]"],
      "environment_checkpoint_boundary": "environment: the action host answered and the state advanced; checkpoint: not read",
      "exact_missing_checkpoint_ids_claimed": false,
      "missing_proof": null
    }
  ],
  "checkpoint_audit": {
    "reward_vector_available": true, "checkpoint_event_timeline_available": false,
    "scalar_only_inference_labeled": true, "rows": []
  },
  "clusters": [],
  "review_ownership": {
    "environment_mutated": false, "checkpoint_or_evaluator_mutated": false,
    "raw_evidence_mutated": false, "external_policy_or_model_called": false,
    "attempt_actions_replayed_as_script": false
  },
  "closure": {
    "raw_evidence_immutable": true, "all_attempts_indexed": true,
    "all_attempt_reviews_complete": true, "all_findings_evidence_backed": true,
    "scalar_inference_labeled": true, "no_fix_claimed": true
  }
}
```

Relative paths resolve against the packet's directory. An attempt with no trajectory (crash before
the first step) binds `error_log_path` and is `invalid`.

`coverage` is the review's own arithmetic against the launch log, and `diagnose.py` checks it:

- `attempts_expected` is how many attempts the launch log says were launched for this round, both
  policies together, including timeouts, crashes, and attempts later found invalid.
- `attempts_indexed` is how many attempt rows the packet lists, and `attempts_reviewed` is how many
  of them were read in full. `attempts_indexed` must equal the number of rows, and
  `attempts_reviewed` must equal `attempts_expected`: a packet that silently drops a crashed launch
  is invalid, not complete. Sampling is permitted only after every attempt is indexed and
  mechanically classified.
- `all_attempts_indexed`, `all_attempts_reviewed`, and `all_steps_read` are the three claims that
  make the counts meaningful; a false one leaves the review incomplete and blocks the verdict.
- `score_only_claims` is always `false`: no finding in the packet rests on a score alone.
- `cluster_method` names how `clusters` were grouped. It is required whenever `clusters` is
  non-empty.

`review_ownership` and `closure` are the reviewer's own statements, read by whichever skill consumes
the packet later. `review_ownership` is five negative self-attestations, all `false`: the package,
the evaluator or checkpoint instrumentation, and the raw evidence were not mutated, no policy or
model was called outside PassAtFour, and the reviewed attempts' actions were never replayed as a
script. `closure` states that the raw evidence stayed immutable, every attempt was indexed and
reviewed, every finding is evidence-backed, scalar-only inference is labeled as such, and
`no_fix_claimed`: this review decided a status and never repaired anything.

## Validity

| `validity` | When |
|---|---|
| `valid` | the attempt ran to a terminal action, the step limit, or the policy's own stop, and no infrastructure or environment fault decided its outcome |
| `invalid` | `invalid_reason`: `timeout` (wall clock, not step limit), `provider` (endpoint errors, empty or truncated responses), `harness` (runner defect), `parser` (a well-formed response misparsed), `reset`, `package`, `evaluator`, or `environment` (an implementation or supervision fault decided the outcome) |
| `unresolved` | review cannot yet tell; blocks the verdict |

A malformed response from the policy itself is a model failure and the attempt stays valid. A
valid attempt carries no `implementation`, `supervision`, or `provider` finding; an attempt whose
outcome such a fault decided is invalid with that reason.

Separate raw-response intent from parsed action. `steps[i].response` is what the policy said and
`steps[i].action` is what the runner parsed and executed; read them apart. A malformed action or a
parser mismatch is a `runner_or_parser_defect` or a model format failure, never an environment
defect. A format-repair retry (`format_repair` in the messages log) belongs to the policy's output,
not to the host.

## Reading an attempt

Read every attempt from reset to its end, aligning one row per step:

```text
observation before the action -> raw response -> parsed action -> feedback -> next observation
-> evaluator state when recorded
```

Keep the raw step indices; do not shift screenshots by one step. Only what the trajectory records
is evidence. The released rollout hands the policy the environment's text feedback when there is
any and a fresh screenshot otherwise, and persists no image: `steps[i]` carries `step`, `action`,
`response`, `feedback`, and `url` (`trace2world_env/src/trace2world_env/cli/rollout.py`), and the
messages log elides image data to a length marker
(`trace2world_env/src/trace2world_env/agent/messages.py`). So an empty `steps[i].feedback` means the
next observation was a screenshot that was never persisted: the step after it starts from an unseen
observation, and the previous step's text is never that observation. A viewer image displayed beside
text feedback is context only unless the raw trajectory records it as the next observation; bind no
finding to it, and never shift it by one step to fill a gap.

The first failure is where analysis starts, not where it ends. Record the last verified state, the
first contradicted expectation, later consequences and independent defects, and whether the path is
reasonable, ambiguous, invalid, or unknown. When the attempt took a route the source never took,
judge it with `implement-task-slice/references/alternative-paths.md`.

Score triage (zero, partial, full, error, timeout, step-limit counts; repeated actions; unchanged
screens) only orders the reading. Repetition alone is not a model failure, and a zero score when the
policy never reached a reasonable path is not an environment failure.

## Categories and owners

`diagnose.py` enforces this table.

| Category | Owner |
|---|---|
| `environment_start_reset_defect`, `environment_action_host_defect`, `environment_gesture_scroll_picker_defect`, `environment_input_ime_defect`, `environment_route_transition_defect`, `environment_state_handoff_projection_defect`, `environment_dynamic_query_binding_defect`, `environment_terminal_surface_defect`, `package_reset_or_host_defect` | `implementation` |
| `alternative_path_gap` | `implementation` when every edge is a supported operation, else `semantics` |
| `unsupported_task_requirement` | `semantics` |
| `checkpoint_missing_producer`, `checkpoint_false_negative`, `checkpoint_false_positive_risk`, `checkpoint_dependency_weighting_defect`, `checkpoint_sparse_or_weak_progress`, `checkpoint_open_outcome_undercoverage` | `supervision` |
| `task_query_ambiguity`, `task_terminal_mismatch`, `task_answer_leakage` | `task` |
| `model_failure` | `model` |
| `runner_or_parser_defect`, `external_provider_failure` | `provider` |

Environment checks: visible host and hit region, truthful feedback, scroll and gesture owner, picker
ownership, input focus, route transition, canonical state write, cross-surface state projection, result
projection, dynamic row binding, terminal action, reset of a new episode. Repeated actions on an
unchanged screen are environment evidence only when the action targets a reasonable visible host
and the feedback or state shows the host failed; when the host works and the policy keeps choosing
the wrong place, that is a model finding.

Checkpoint rules:

- `checkpoint_false_negative` is `trace_confirmed` only when business success is verified
  independently and the reward vector or checkpoint events show the miss; otherwise it is
  `requires_live_reproduction`.
- A prefilled expected payload or a selector detached from app-owned state is
  `checkpoint_false_positive_risk`; it is an observed false positive only when an invalid execution
  actually received the reward.
- An accepted value narrower than a visibly valid instruction is open-outcome undercoverage only
  after the task semantics establish the equivalence.
- A static validation pass under a checkpoint profile the environment does not run is not runtime
  closure. The environment manifest declares `runtime_profile` as `browser_checkpoint_events` or
  `state_kernel_sqlite`, so a package validator can pass, and a `checkpoint_audit.rows[].status` can
  read `not_applicable`, while the profile that actually produced this attempt's reward was never
  exercised. Such a pass is `static_contract_confirmed` at best and never closes a runtime finding.
- A full reward whose visible flow skipped a business constraint is reward hacking: a shortcut that
  reaches the score without the work. Record it as `checkpoint_false_positive_risk` (owner
  `supervision`) when the leak is in the producer, the evaluator read, or the aggregation, and as
  the matching `environment_*` category (owner `implementation`) when the app itself accepts the
  shortcut, such as a terminal surface that commits without the required state. It is never a model
  finding, and a candidate carrying it is never `Selectable` or `Refinable`, even when the leaked
  reward lifts the learner mean above 0.8.

An early environment blocker and a later model mistake in the same attempt are two findings.

## Evidence states

| State | Meaning |
|---|---|
| `trace_confirmed` | the attempt's own steps, feedback, and evaluator output show it |
| `static_contract_confirmed` | the package or evaluator contract shows it, independent of this run |
| `high_confidence_inference` | consistent with all evidence, not directly observed |
| `requires_live_reproduction` | only a fresh run can decide; `EnvDefect` sends it to reproduction |
| `insufficient_evidence` | blocks the verdict (`Incomplete`) |

Each finding states the symptom, the attempts and steps, the expected state, the evidence paths,
the environment-versus-checkpoint boundary (`environment_checkpoint_boundary`, non-empty on every
finding: which side of the chain the evidence puts the defect on), and what proof is missing.

## Checkpoint chain

For every partial or full-looking attempt, audit the supervision chain:

```text
visible operation -> app-owned state -> checkpoint producer -> runtime state field or event
-> evaluator read and aggregation -> reported reward
```

Each `checkpoint_audit.rows` entry names the predicate (progress or terminal), producer, state field,
evaluator read, dependencies, weight, acceptable outcomes, positive, negative, repeated, and reset
evidence, and a status (`confirmed_ok`, `confirmed_defect`, `risk_only`,
`requires_live_reproduction`, `not_applicable`). With only a scalar reward, set
`reward_vector_available=false`; naming an exact missing checkpoint is then forbidden and
`findings[].exact_missing_checkpoint_ids_claimed` stays `false`. `scalar_only_inference_labeled` is
`true` on every packet, scalar reward or not: it asserts that wherever the evidence was only a
scalar, the finding says so.

## Clusters

Group findings by observable mechanism, not score: the same host, route, state field, or producer; the
same unchanged-screen and feedback signature; the same malformed runner action; the same reset; the
same acceptable-outcome mismatch. Each cluster lists every member, whether members are exact or
semantic equivalents, its representative and why, and whether every member was read in full. Every
attempt stays individually reviewed; a representative's finding is not copied to members without
member-level evidence.
