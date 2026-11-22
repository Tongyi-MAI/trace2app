# Per-Trace Obligation Checklist Contract

## Purpose

This is the obligation set `O` of Algorithm 4. It is built from source-trace evidence before any execution and is read, never edited, by source-trace review, probes, and supervision repair. The unit of ownership is one trace, not one app, package, or trace root.

## Artifact Layout

```text
<checklist_dir>/
├── manifest.json
├── trace_index.json
└── traces/
    └── <trace_id>/
        ├── reference_sprites/
        │   ├── manifest.json
        │   ├── marked/step_XXXX.jpg
        │   └── sprites/<trace_id>_reference_sprite.jpg
        ├── stepwise_checklist.json
        └── evidence.json
```

The root files are indexes only. These root-level files are forbidden because they would collapse trace ownership:

- `package_wide_checklist.json`
- `stepwise_checklist.json`
- `closure_checklist.json`

## Root Schemas

`manifest.json`:

```json
{
  "schema": "trace2world.reference_stepwise_checklist_set.v1",
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
  "schema": "trace2world.reference_stepwise_checklist_index.v1",
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
  "schema": "trace2world.reference_stepwise_checklist.v1",
  "trace_id": "task_...",
  "source_trace_root": "...",
  "trajectory_path": ".../trajectory.json",
  "trajectory_sha256": "...",
  "construction_stage": "source_evidence",
  "annotation_text_is_candidate_only": true,
  "sprite_manifest_path": ".../reference_sprites/manifest.json",
  "sprite_path": ".../reference_sprites/sprites/task_..._reference_sprite.jpg",
  "sprite_opened_and_read": true,
  "rows": [],
  "status": "ready",
  "blockers": []
}
```

Write one row for every retained step, including step 0:

```json
{
  "step_index": 0,
  "disposition": "keep",
  "raw_action_snapshot": "...",
  "semantic_action_snapshot": "...",
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
  "terminal_or_supervision": {
    "applies": false,
    "required_values": [],
    "success_relation": "",
    "must_not_pass_without": []
  },
  "terminal_synthetic_projection_handoff": {
    "applies": false,
    "eligibility": "not_applicable | eligible | ineligible",
    "pre_action_visible_state": "...",
    "expected_terminal_delta": "...",
    "forbidden_terminal_outcomes": [],
    "source_host_and_target_unambiguous": null,
    "local_deterministic_delta": null,
    "no_later_reference_dependency": null,
    "no_reference_contradiction": null,
    "safe_reversible_or_low_risk": null,
    "requires_generated_visible_projection": null,
    "requires_app_owned_state_and_evaluator_probe": null,
    "rationale": "..."
  },
  "terminal_action_commit_handoff": {
    "applies": false,
    "eligibility": "not_applicable | eligible | ineligible",
    "visible_source_host": "...",
    "marked_target": "...",
    "committed_action": "...",
    "success_boundary": "action_commit_only",
    "destination_content_claimed": false,
    "reference_post_action_effect_claimed": false,
    "effect_class": "navigation_or_open | immediate_ui_transition | business_state_mutation | scroll_or_gesture | other",
    "post_action_effect_out_of_scope": null,
    "required_gui_evidence": [],
    "forbidden_success_inference": [],
    "source_host_and_target_unambiguous": null,
    "action_target_and_commit_unambiguous": null,
    "no_later_reference_dependency": null,
    "reference_proves_action_was_issued": null,
    "rationale": "..."
  },
  "annotation_text_disagreement": "",
  "reference_annotation_checks": [
    "pre_action_source_host",
    "marked_target_alignment",
    "source_host_not_destination",
    "next_state_leak_absent",
    "post_action_effect",
    "instruction_trace_alignment",
    "terminal_dependency_explicit"
  ],
  "env_execution_checks": [
    "whole_viewport_chrome_scan",
    "l0_related_surface_parity_check",
    "progressive_surface_lifecycle_check",
    "parent_projection_contamination_check",
    "viewport_containment"
  ],
  "evidence": ["..."],
  "blockers": []
}
```

## Filling Rules

Use the current marked screenshot as the pre-action state. Use the next chronological screenshot, parent-page projection, or terminal evidence as the action effect. When the final kept action has no settled post-action observation, the checklist may still be ready through one of two distinct handoffs:

- Use `terminal_synthetic_projection_handoff.eligibility=eligible` when the expected effect is a small local deterministic state delta.
- Use `terminal_action_commit_handoff.eligibility=eligible` whenever a non-micro final action's visible source host, instruction-named target/value, and issued click/commit/gesture are unambiguous, but source trace does not settle or visibly realize the later effect. This includes navigation/open, tab/scroll gestures, and instruction-defined submit/publish actions. The handoff must set `success_boundary=action_commit_only`, `destination_content_claimed=false`, `reference_post_action_effect_claimed=false`, classify `effect_class`, and set `post_action_effect_out_of_scope=true`.

For an action-commit handoff, later environment GUI review must prove that the same visible host was naturally reached and the action was actually issued without forbidden shortcuts or operational error. The unchanged or failed-looking source trace terminal frame is not a checklist blocker and must not be copied into the package. The unrecorded destination page, immediate UI transition, or business result is outside this terminal verdict and must not be synthesized, required, checkpointed, or claimed. This classification does not authorize real external side effects outside the bound offline generated environment.

Do not claim that source trace visually proved an unobserved projection or destination. Do not treat matching numeric indices between future execution and reference as a pairwise alignment claim.

The action text is candidate-only. If the marked screenshot and the instruction make the correct source host and action chain knowable, write the row from that visual evidence and describe the disagreement in `annotation_text_disagreement` (for example `source_host_mismatch`, `instruction_trace_mismatch`, `redundant_retained_action`, or `stale_action_text`, plus one sentence). Block only when the correct source host, target, value, ordering, or disposition remains unknowable after reading the marked sequence.

For each row:

- Describe the complete task-relevant current viewport, not only the clicked target.
- Name the visible source host. Do not name a destination control that appears only after navigation.
- Record state that must not appear before the action. This is mandatory for multi-stage selection, progressive reveal, result count, percentage/value reveal, autocomplete, overlay, sheet, filter, picker, and parent projection flows.
- Record selected/default state before and after a selection action.
- Record input value, suggestion visibility/order, and forbidden preselection for autocomplete/typeahead.
- Record overlay structure and parent projection, including forbidden sibling/future projection.
- Record controls that must remain inside the phone/content shell. Shell/panel geometry is a review obligation even when it is not a reward checkpoint.
- Record terminal values and relations, such as the first encountered value below a threshold, rather than only the final selected label.
- Use terminal synthetic projection only for small deterministic deltas such as favorite/collect, like, follow, subscribe, switch/check selection, a local count increment, or a visible success toast/label whose target and before-state are unambiguous. Do not use it for payment/order placement, privacy/security changes, destructive/irreversible actions, ambiguous entity/value selection, hidden business data, or any case where source trace evidence indicates the action failed.
- Use terminal action commit for any non-micro final action with an unambiguous marked host, instruction-named target/value, and action-issued source trace record when source trace does not settle the effect. Judge terminal success only at the click/commit/gesture boundary; keep unobserved destination/UI/business effects out of required values, checkpoints, and closure claims.

## Status Reduction

`status=ready` requires:

- exact retained-step coverage;
- step 0 coverage when retained;
- sprite opened/read;
- matching source digest;
- no row blockers;
- all required row fields filled;
- any applicable terminal synthetic projection is explicitly eligible and carries the required generated-visible-state, app-owned-state, evaluator, negative, repeated-action, and reset obligations;
- any applicable terminal action commit is explicitly eligible, sets `destination_content_claimed=false`, `reference_post_action_effect_claimed=false`, and `post_action_effect_out_of_scope=true`, classifies the effect, and carries natural-GUI action evidence;
- every known annotation-text disagreement is described in `annotation_text_disagreement`; a disagreement whose correct obligation is knowable does not block the checklist;
- applicable lifecycle/input/projection/terminal objects filled;
- `blockers=[]`.

`status=blocked` preserves available evidence and names the missing or unknowable oracle. Do not invent values.

## Consumer Rules

- `review-trace` compares each observed current state and action effect against the matching row, while still reading the complete source sequence and the complete execution sequence. A checklist is an oracle baseline, never a substitute for a fresh execution and review.
- `execute-trace` receives the instruction and, per retained row, only the action to perform (`operation_intent`, `source_host`). Expected and forbidden states are the review oracle and are not given to execution.
- `probe-contract` may use `terminal_or_supervision` as the obligation inventory; this checklist produces no checkpoint and no evaluator pass.
- `repair-supervision` consumes the checklist path and SHA-256 cited by the current review packet, verifies `trajectory_sha256`, and writes a row-complete checklist-to-checkpoint mapping. A row may become a checkpoint candidate, a dependency-only state, a negative discriminator, a terminal assertion, a visible projection probe, diagnostic-only evidence, or a blocker. Row coverage is mandatory; one reward checkpoint per row is forbidden.
- Every consumer verifies `trajectory_sha256` against the current trajectory. A digest difference means the checklist is stale and must be rebuilt.
