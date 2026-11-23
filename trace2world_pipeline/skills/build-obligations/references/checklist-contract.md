# Per-Trace Obligation Checklist Contract

## Purpose

This is the obligation set `O` of Algorithm 4 (paper Appendix B.2): "BuildObligations derives a
per-trace checklist from source-trace evidence." It is built before any execution and is read,
never edited, by `review-trace`, `probe-contract`, and `repair-supervision`. The unit of ownership
is one source trace, not one app, package, or trace root.

## Artifact Layout

```text
<checklist_dir>/                      $RESULT/run_management/source_checklists/
├── manifest.json
├── trace_index.json
└── traces/
    └── <trace_id>/
        ├── source_sprites/
        │   ├── manifest.json
        │   ├── marked/step_XXXX.jpg
        │   └── sprites/<trace_id>_source_sprite.jpg
        ├── stepwise_checklist.json
        └── evidence.json
```

The root files are indexes only. These root-level files are forbidden because they would collapse
trace ownership: `package_wide_checklist.json`, `stepwise_checklist.json`, `closure_checklist.json`.

## Root Schemas

`manifest.json`:

```json
{
  "schema": "trace2world.source_checklist_set.v1",
  "source_trace_root": "...",
  "construction_stage": "source_evidence",
  "trace_count": 1,
  "trace_ids": ["task_..."],
  "aggregate_stepwise_checklist_forbidden": true,
  "status": "pending"
}
```

`trace_index.json`:

```json
{
  "schema": "trace2world.source_checklist_index.v1",
  "source_trace_root": "...",
  "traces": [
    {
      "trace_id": "task_...",
      "trajectory_path": ".../trajectory.json",
      "trajectory_sha256": "...",
      "steps_total": 8,
      "kept_steps": 8,
      "checklist_path": ".../stepwise_checklist.json",
      "evidence_path": ".../evidence.json",
      "status": "pending",
      "blockers": []
    }
  ]
}
```

## Per-Trace Checklist Schema

`stepwise_checklist.json`:

```json
{
  "schema": "trace2world.source_checklist.v1",
  "trace_id": "task_...",
  "source_trace_root": "...",
  "trajectory_path": ".../trajectory.json",
  "trajectory_sha256": "...",
  "construction_stage": "source_evidence",
  "step_descriptions_are_candidates": true,
  "instruction": "...",
  "source_terminal_contract": {},
  "sprite_manifest_path": ".../source_sprites/manifest.json",
  "sprite_path": ".../source_sprites/sprites/task_..._source_sprite.jpg",
  "sprite_opened_and_read": true,
  "rows": [],
  "status": "ready",
  "blockers": []
}
```

`source_terminal_contract` is a verbatim copy of the trace's `terminal_contract` (written by
source-trace preprocessing, paper Appendix G), or `{}` when the trace has none.

Write one row for every retained step (disposition `keep`), including step 0:

```json
{
  "step_index": 0,
  "disposition": "keep",
  "raw_action": "...",
  "step_description": "...",
  "current_observation": "...",
  "marked_screenshot": "...",
  "pre_action_expected_visible_state": ["..."],
  "pre_action_forbidden_future_state": ["..."],
  "source_host": "...",
  "operation_intent": "...",
  "post_action_expected_effect": ["..."],
  "next_state_anchor": "...",
  "progressive_lifecycle": {
    "applies": false,
    "expected_before": [],
    "expected_after": [],
    "forbidden_before_action": []
  },
  "input_or_autocomplete": {
    "applies": false,
    "input_value_before": "",
    "input_value_after": "",
    "expected_suggestions": [],
    "forbidden_preselection": []
  },
  "overlay_or_parent_projection": {
    "applies": false,
    "expected_current_surface": [],
    "expected_parent_projection": [],
    "forbidden_sibling_or_future_projection": []
  },
  "viewport_containment": {
    "expected_shell": "mobile_content_viewport",
    "must_be_inside_shell": [],
    "allowed_outside_shell": []
  },
  "terminal_contract": {
    "applies": false,
    "commit_status": "",
    "observed_boundary": "",
    "outcome_fields": [],
    "required_state": [],
    "forbidden_state": [],
    "success_relation": "",
    "remaining_uncertainty": []
  },
  "step_description_disagreement": "",
  "source_step_checks": [
    "pre_action_source_host",
    "marked_target_alignment",
    "source_host_not_destination",
    "next_state_leak_absent",
    "post_action_effect",
    "instruction_trace_alignment",
    "terminal_dependency_explicit"
  ],
  "execution_checks": [
    "whole_viewport_scan",
    "action_surface_parity_check",
    "progressive_surface_lifecycle_check",
    "parent_projection_contamination_check",
    "viewport_containment"
  ],
  "evidence": ["..."],
  "blockers": []
}
```

`raw_action` is the recorded action payload (family, coordinates, typed value); `step_description`
is the preprocessing description of the step. Both are copied from the trace.

## Filling Rules

Use the current marked screenshot as the pre-action state. Use the next chronological screenshot,
the parent-page projection, or terminal evidence as the action effect.

The step description is a candidate. Raw action family and value override prose, and the marked
screenshot overrides both. If the marked screenshot and the instruction make the correct source
host and action knowable, write the row from that visual evidence and record the disagreement in
`step_description_disagreement` (for example `source_host_mismatch`, `instruction_trace_mismatch`,
`redundant_retained_action`, or `stale_description`, plus one sentence). Block only when the
correct source host, target, value, ordering, or disposition remains unknowable after reading the
marked sequence.

For each row:

- Describe the complete task-relevant current viewport, not only the clicked target.
- Name the visible source host. Do not name a destination control that appears only after
  navigation.
- Record state that must not appear before the action. This is mandatory for multi-stage
  selection, progressive reveal, result count, value reveal, autocomplete, overlay, sheet, filter,
  picker, and parent projection flows.
- Record selected/default state before and after a selection action.
- Record input value, suggestion visibility and order, and forbidden preselection for
  autocomplete/typeahead.
- Record overlay structure and parent projection, including forbidden sibling or future projection.
- Record controls that must remain inside the app's content viewport. Viewport containment is a
  review obligation even when it is not a reward checkpoint.

## Terminal Contract

Exactly one row, the last task-relevant step, sets `terminal_contract.applies=true`. Fill it from
`source_terminal_contract` and the marked sequence:

| Field | Content |
|---|---|
| `commit_status` | `effect_observed`: the effect of the terminal action is visible in a recorded screenshot; `committed_effect_unobserved`: the terminal action is issued on an unambiguous host but its effect was not recorded; `precommit`: the task ends at a state before a commit (ready to submit, before payment) |
| `observed_boundary` | the terminal boundary as observed in the trace |
| `outcome_fields` | the instruction-named entity and values the terminal must carry |
| `required_state` / `forbidden_state` | state that must hold, or must not occur, at the terminal (for example no real payment or external side effect) |
| `success_relation` | the terminal relation, such as "the first value below the threshold", not only the final label |
| `remaining_uncertainty` | what the trace does not establish |

The row's `post_action_expected_effect` lists only what the trace shows:

- `effect_observed`: the observed terminal effect (non-empty);
- `committed_effect_unobserved`: empty. The terminal obligation is the commit itself: the same
  visible host, target, and value, reached and issued naturally. The unrecorded destination,
  transition, or business result goes into `remaining_uncertainty`; it is not claimed, required,
  or checkpointed from this trace. If another source trace shows the same effect, that effect is
  part of the app specification through that trace, and `probe-contract` checks it there;
- `precommit`: the pre-commit state the task ends in (non-empty), with the commit itself forbidden.

## Status Reduction

`status=ready` requires:

- exact retained-step coverage, including step 0 when retained;
- the sprite opened and read;
- a matching source digest;
- every required row field filled and every applicable lifecycle, input, projection, and
  containment object filled;
- exactly one terminal row, filled as above;
- every known description disagreement recorded;
- no row or trace blockers.

`status=blocked` preserves available evidence and names the missing or unknowable source evidence.
Do not invent values.

## Consumer Rules

- `review-trace` compares each observed state and action effect against the matching row, while
  still reading the complete source sequence and the complete execution sequence. A checklist is
  the review oracle, never a substitute for a fresh execution and review.
- `execute-trace` receives the instruction and, per retained row, only the action to perform
  (`operation_intent`, `source_host`). Expected and forbidden states are the review oracle and are
  not given to execution.
- `probe-contract` uses the terminal row and the rows' required and forbidden states as the
  obligation inventory; this checklist produces no checkpoint and no evaluator pass.
- `repair-supervision` consumes the checklist path and SHA-256 cited by the current review packet,
  verifies `trajectory_sha256`, and writes a row-complete checklist-to-checkpoint mapping. A row may
  become a checkpoint candidate, a dependency-only state, a negative discriminator, a terminal
  assertion, a visible projection probe, diagnostic-only evidence, or a blocker. Row coverage is
  mandatory; one reward checkpoint per row is forbidden.
- Every consumer verifies `trajectory_sha256` against the current trajectory. A digest difference
  means the checklist is stale and must be rebuilt.
