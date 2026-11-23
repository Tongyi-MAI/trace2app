# Alternative Paths

An alternative path is a non-source route that a reasonable user could take, from visible state,
to complete the same task with the same business result. The source trace proves one successful
execution, not the only one. Checkpoints are predicates over the final business state, so a valid
alternative path that realizes the same contracted state earns the same credit (paper Appendices
B.3 and C.3), and the probe matrix of `probe-contract` holds a "legitimate alternative path" row.

Alternative paths come up in three places:

- authoring: a candidate's decisions can be reached by more than one supported route;
- screening: a learner or solver attempt reaches the terminal by a route the source never took,
  and the evaluator scores it differently from the source route;
- probing: the alternative-path row of the probe matrix needs a concrete route.

Task augmentation may recombine supported operations beyond any recorded path; it never adds an
edge. A semantically equivalent path is implemented only when every edge is a supported operation
of the frozen model. A path that needs an unsupported edge is recorded as a finding and stays
outside the package (paper Appendix E.3.3 audits this split).

## Scope and record boundary

Discovery is read-only with respect to pages, runtime contracts, evaluator logic, and package
payloads: it records what it finds, and the owning skill performs the mutation. Inside this skill
the same boundary holds while a packet is written — classify first, implement afterwards.

- One packet per task, unless the tasks share exactly the same instruction, terminal invariants,
  source path, and visible evidence; record that equivalence explicitly, and never silently merge
  task scopes.
- Do not inspect sibling tasks as a substitute for the named task.
- Write the packet to a temporary file, validate the JSON, then rename it into place; never let a
  reader consume a partially written packet.
- Screenshots, probe logs, actionability output, and a chat summary are not packets, and there are
  no synthetic completion packets.
- Actionability feedback, current click handlers, execution reachability, and evaluator checkpoint
  results are diagnostic-only inputs: they never decide semantic candidacy.

## Three verdicts

Each candidate path gets three independent verdicts; never collapse them into one flag.

| Verdict | Question | Values |
|---|---|---|
| `semantic_verdict` | Does the visible path complete the same task under normal product semantics? | `valid`, `invalid`, `uncertain` |
| `materialization_status` | Does the current package execute it end to end from reset? | `materialized`, `partial`, `missing`, `not_probed` |
| `evaluator_status` | Would the evaluator recognize the equivalent completion? | `covered`, `source_overfit`, `missing`, `not_inspected` |

A visible "payment method" row may be `valid + missing + not_inspected`. A clickable detour may be
`invalid + materialized + covered`.

## Evidence order

1. the instruction and its terminal business semantics;
2. the visible state at each relevant point (labels, values, selected and disabled state, rows,
   tabs, arrows);
3. the source trace, as one known successful path;
4. DOM and package files, for materialization only;
5. the evaluator and its checkpoints, for coverage only.

The absence of an action host, handler, or checkpoint does not disprove a semantic candidate: the
package was built along the source traces, so other controls may be visible but uncovered. Never
derive a candidate from hidden DOM, file names, comments, or behavior the visible state does not
support.

## Procedure

1. Bind the target: package revision, task id and instruction, source trace, launch state, commit
   boundary, and the expected terminal entity and payload.
2. Normalize the terminal into business invariants (entity, quantity and variants, amounts,
   required selections, whether submit or payment is allowed, final visible state), not into the
   source's clicks.
3. Normalize the source path into business operations and the states where each changes canonical
   state; drop coordinates, waits, and replay mechanics.
4. At the launch state and every consequential state, list the visible controls that could
   advance, reorder, shortcut, or preselect part of the task: other entry points and searches,
   tabs and filters, rows with values and disclosure arrows, pre-submit selectors, shortcuts versus
   drill-downs, state-preserving back paths, sheets exposing an equivalent choice.
5. For each control, ask the five generation questions before judging anything:

   1. What business state would this control plausibly change?
   2. Can that state commute with, precede, or replace a source operation?
   3. Where would the path diverge from the source?
   4. Where would it rejoin, if at all?
   5. Does it preserve every terminal invariant?

   Then write the substitution row: where the path diverges, which source steps and business
   operations it replaces, the shortcut surface, and where it rejoins. Name skipped business
   operations, not the target button: a `人气热卖同商品＋` shortcut replaces
   `打开花筒分类 -> 打开该商品规格 -> 在规格弹窗加入购物车`, not "the plus button". Describe the path in
   visible labels and business operations, never in file names or action ids.
6. Apply the seven gates below. A failure makes the candidate `invalid`; insufficient visible
   evidence makes it `uncertain`, never `valid`.
7. Only then assess materialization and evaluator coverage, without revisiting the semantic
   verdict.

Candidate families: alternative entry route; reordered commuting operations; preselection carried
downstream; shortcut versus drill-down; equivalent selection surface; state-preserving navigation
that the task actually needs.

## Reporting order

Never lead with `missing` for an accepted shortcut. Report in this order:

1. the source operations and step range that can be skipped or replaced;
2. the visible alternative arrow path;
3. the semantic verdict and the invariant proof;
4. materialization and evaluator status;
5. the repair handoff, or the verified closure evidence.

`materialization_status: missing` and `evaluator_status: source_overfit` never demote, omit, or bury
a `semantic_verdict: valid` shortcut: lead with the semantic substitution, then report the
implementation and evaluator gaps.

## Equivalence gates

| Gate | Passes when |
|---|---|
| `A_visible_support` | every divergence action has a visible affordance or a visible state that conventionally implies it |
| `B_entity_identity` | the task entity (item, merchant, trip, event) stays the same |
| `C_payload_identity` | quantity, variant, address, date, seat, coupon, payment method, and total stay equivalent unless the instruction permits variation |
| `D_terminal_identity` | the final business state and visible completion match the task, not just the final page |
| `E_commit_boundary_safety` | the path does not cross a submit, save, purchase, or payment boundary the instruction forbids |
| `F_task_relevance` | every divergence has a plausible role in this task |
| `G_state_continuity` | required business state is carried across divergence and rejoin by app state, not by step context, injected ids, or trace metadata |

Reject, naming the reason, a candidate that detours through unrelated features; reaches the target
by file navigation, a handcrafted URL, script injection, or an id jump; depends on an invisible
control; changes the entity, payload, or terminal; crosses a forbidden boundary; picks a business
value from replay context; is the same control at another coordinate; or exists only in a route
graph. Do not reject because a host is missing, the source never clicked it, replay cannot run it,
or the evaluator lacks a checkpoint: those are materialization and coverage findings.

An admitted path — one that may be implemented — satisfies two further conditions the gates above
do not cover:

| Admission condition | Passes when |
|---|---|
| no answer leakage, no task-id dependence | the path reveals no answer and branches on no task id |
| shared-surface identity unchanged | the path does not change the identity of a seed or shared surface |

## Materialization and coverage

`materialized` requires, from reset: a host on the visible control, an effect on app-owned state,
the selected value projected on return, the downstream commit consuming it, and state surviving
natural navigation. `source_overfit` means the reward requires source-only intermediates,
ordering, page visits, or process checkpoints although the terminal is equivalent; process
checkpoints stay only when they encode required semantics or a safety boundary.

Classify the four materialization values:

- `materialized`: every required transition works naturally;
- `partial`: some hosts or transitions exist, but the path breaks;
- `missing`: a visible semantic affordance has no usable implementation;
- `not_probed`: discovery stayed read-only and did not execute the path.

Inspect the evaluator on four points: the selected entity and payload, the final selection or
payment state, the commit boundary, and the absence of a forbidden side effect.

When a query admits several equivalent seats or entities but the evaluator accepts one fixed id
set, triage by the solution policy first: it is either a supervision defect or a task ambiguity. If
the acceptance set has visible grounding, repair the evaluator to accept the whole equivalence
class — and never let an arbitrary result pass.

Routing:

- `valid + materialized + source_overfit`: a supervision defect; `repair-supervision` fixes the
  evaluator (in screening, `diagnose-attempts` attributes it as `EnvDefect`);
- `valid + partial/missing` with every edge a supported operation: an implementation gap;
  `implement-task-slice` may instantiate it within the candidate's slice (`implement: true`);
- `valid` but some edge unsupported: a finding only; it is not implemented and the task does not
  depend on it;
- `invalid` or `uncertain`: kept in the ledger with its reason so it is not rediscovered.

Every accepted path carries a repair handoff, including the paths that already execute:

- the replaced source steps and operations;
- the visible root-entry action sequence;
- the entity, payload, and terminal invariants;
- the first missing host, runtime, or state-ownership layer;
- the evaluator gap and the source-only checkpoints;
- the owner and the exact artifact scope;
- the positive root-entry execution and the negative verifier requirements;
- the closure status: `already_materialized`, `repair_required`, or `verified_after_repair`.

Do not hand off only the currently broken candidates. When all alternatives are being implemented,
an already-materialized accepted path still needs fresh path-specific verification: one executable
shortcut cannot stand in for the rest.

## Packet

`$RESULT/task_supply/alternative_paths/<task_id>.json`, validated by
`scripts/validate_alternative_path_packet.py`:

```json
{
  "schema": "trace2world.alternative_path_packet.v1",
  "task_id": "...", "source_trace": "...", "package_revision": "sha256:...",
  "terminal_semantics": {"entity": "...", "payload": {}, "commit_boundary": "before submit"},
  "source_path": ["business state", "..."],
  "candidate_ledger": [
    {
      "candidate_id": "alt_01",
      "path": ["visible label or business operation", "..."],
      "divergence_point": "business state where the path leaves the source",
      "rejoin_point": "business state where it rejoins, or null",
      "business_state_transitions": ["..."],
      "visible_evidence": ["visible label, value, row, or arrow"],
      "equivalence_reason": "why every terminal invariant still holds",
      "equivalence_gates": {"A_visible_support": "pass", "...": "pass"},
      "semantic_verdict": "valid",
      "materialization_status": "missing",
      "evaluator_status": "covered",
      "coverage_gaps": ["the specific missing host, runtime, or evaluator coverage"],
      "source_replacement": {"replaced_source_steps": [], "replaced_source_operations": [],
                                "shortcut_surface": "...", "rejoin_state": "..."},
      "primary_semantic_finding": true,
      "implement": true,
      "edges_supported": true,
      "supported_operation_ids": ["op.checkout.payment_method"],
      "repair_handoff": {"status": "repair_required", "owners": ["repair-package"],
                          "root_entry_path": ["..."],
                          "required_verification": ["positive root-entry execution",
                                                     "negative verifier"]},
      "closure": {"execution_packet": "...", "probe_report": "..."},
      "reason": null
    }
  ],
  "primary_semantic_findings": ["alt_01"],
  "outcome": "complete_with_alternatives",
  "blocked_reasons": []
}
```

`outcome` is `complete_with_alternatives`, `complete_no_visible_alternative`, or
`blocked_missing_evidence`; `blocked_reasons` names the missing evidence when the outcome is
`blocked_missing_evidence`. These are discovery outcomes, never environment pass/fail verdicts.

Every accepted candidate states its path in one concise arrow sequence before the details, and
carries `divergence_point`, `rejoin_point`, `business_state_transitions`, `visible_evidence`, and
`equivalence_reason`: without the visible evidence and the invariant proof the semantic verdict is
unreviewable. `coverage_gaps` names the specific host, runtime, or evaluator coverage that path is
missing, so a valid-but-unmaterialized path is recorded as a gap rather than discarded. The
validator requires `outcome`, and requires `divergence_point`, `rejoin_point`, `visible_evidence`,
and `equivalence_reason` on every accepted candidate.

Rank accepted paths by semantic significance before materialization: a shortcut that removes a
source drill-down is a primary finding even when its host is missing. Run the validator with
`--index` to check that implemented paths use only supported operations, and with
`--require-materialization-closure` after implementation: each implemented path then needs its own
fresh execution packet (`execute-trace`) and a passing alternative-path probe (`probe-contract`).

## Examples

**Homepage search instead of channel search.** Visible homepage search can locate the same product
directly:

```text
首页全局搜索 -> 搜索目标商品 -> 同一商品详情 -> 同一结算状态
```

When entity, payload, and commit boundary match, this is a valid alternative entry route. Whether
the homepage search is currently wired is a separate materialization verdict.

**Choose payment before submit.** The checkout page visibly shows `支付方式 / 普通支付 >`, while the
source submits first and chooses payment later:

```text
结算页 -> 支付方式 -> 微信支付 -> 返回结算页（显示微信支付） -> 提交订单
```

This is a valid reordered and preselection alternative when the submit action consumes the selected
payment method and preserves the same product, quantity, amount, and terminal boundary. A missing
handler on the visible payment row is `materialization_status: missing`; it does not invalidate the
alternative.

**Unrelated category detour.**

```text
首页 -> 丽人美发 -> 任意返回/搜索 -> 目标商品
```

Reject it: the category has no task role and adds only arbitrary reachability. Eventual arrival at
the target is insufficient.

**Direct page navigation.** Opening the checkout page file directly is not a user path. A file name
can help inspect evidence, but bypassing visible application navigation is not an alternative path.

**Currently clickable but wrong payload.** A visible and executable shortcut reaches the same
merchant but defaults to a different package or quantity. Reject it, unless visible controls restore
the exact required payload before the commit boundary.

**Correct terminal, but the evaluator requires source order.** Accept the semantic alternative when
all task invariants hold. Mark the evaluator `source_overfit` and name the source-specific
checkpoint that causes the false negative.

## Completion

Discovery for one task is complete only when:

- the exact target and terminal semantics are bound;
- the source path is normalized into business states;
- every consequential visible state has an evidence inventory and a candidate ledger: never infer
  `complete_no_visible_alternative` before that inventory exists;
- each candidate has an explicit semantic verdict, and every accepted path passes all gates;
- arbitrary detours and invisible shortcuts are rejected with their reason;
- semantic, materialization, and evaluator verdicts are reported separately;
- valid-but-unmaterialized paths are recorded as coverage gaps rather than discarded;
- every accepted candidate maps to the source step and operation range it replaces;
- `primary_semantic_findings` holds every task-relevant shortcut that removes one or more source
  operations, regardless of current interactivity;
- when implementation was requested, every accepted path has a repair handoff and fresh closure
  evidence, or an explicit owner blocker.

The report either identifies at least one valid alternative or gives an evidence-backed reason why
none is visible.
