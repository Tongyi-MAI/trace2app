# Candidate Binding

A task candidate is authored from the task-facing index (`task-index.md`) and nothing else: task
authoring performs no structure extraction of its own (paper Appendix C.1, "Candidate binding";
condensed prompt in Appendix G, "Task authoring"). `scripts/validate_task_candidate.py` checks the
mechanical part of this contract; the semantic part below is the author's and the reviewer's.

## What a candidate binds

`trace2world.task_candidate.v1`:

```json
{
  "schema": "trace2world.task_candidate.v1",
  "candidate_id": "meituan_c017",
  "index_sha256": "<sha256 of task_supply/index.json>",
  "instruction": "user-facing text",
  "bindings": {
    "goal": "<item_id>",
    "terminal_value": "<item_id>",
    "launch_state": "<item_id>",
    "subtasks": [{"index_items": ["<item_id>", "..."], "kind": "business"}],
    "decisions": ["<item_id>"],
    "cross_state_dependencies": ["<item_id>"],
    "evidence_revisits": ["<item_id>"],
    "acceptable_terminal_set": ["<item_id>"],
    "reset_boundary": "<item_id>",
    "evaluator_outcome": "<item_id>"
  },
  "difficulty": {
    "long_horizon": true,
    "phenomena": {"multi_item_state_tracking": {"status": "required", "evidence": ["<item_id>"]}}
  },
  "verification_and_repair": {"terminal_assertions": [], "recheck_evidence": [], "correction_path": []},
  "diversity_signature": {
    "archetype_id": "", "goal_family": "", "decision_graph": "", "dependency_signature": "",
    "terminal_value_type": "", "reasoning_family": "", "authority_class": "reference_exact"
  },
  "generated_instance": null
}
```

A binding entry is an index `item_id`, or an object with `index_items` and a `kind`. Every named
item must exist in the current index with a supported operation and evidence.

| Validator verdict | Meaning |
|---|---|
| `rejected` | a binding is missing or names an item the index lacks or does not support; the floor or phenomena fail; the instruction leaks ids; the signature is incomplete |
| `needs_implementation` | all bindings supported; some items are `supported_not_implemented`, and `Implement` instantiates exactly those |
| `admissible` | all bound items supported and implemented |

A missing support binding rejects the candidate; it never routes to reconciliation. A supported
but not-yet-implemented binding routes to implementation.

## Composition

Compose only opportunities the index marks: aggregation, comparison, state carried across
surfaces, lifecycle revisits, recovery, and multi-entity tracking. Combine them when every
intermediate state has a later consumer. For each family the candidate names its inputs and
consumer:

- aggregation or comparison: visible input fields, the user-visible rule, tie policy, and where the
  derived decision is encoded;
- cross-surface state: source and consumer surfaces, the exact field preserved, freshness, reset;
  two projections of one unchanged state are not composition;
- lifecycle revisit: the mutation and the changed state the revisit reads;
- recovery or boundary: operands, both branches, the invalid or reversible state, the terminal of
  each branch;
- multi-entity tracking: the entity denominator, per-entity result, missing or duplicate policy,
  exact conservation, and a structured output or committed state;
- safe terminal: the reversible or irreversible boundary and the observable stop condition.

A family label without these bindings earns neither difficulty nor coverage.

## Difficulty

Difficulty follows four capabilities: long horizon, reasoning, memory and state, and recovery or
backtracking. It is a semantic delta from the simplest supported task for the same goal, not a
count of actions.

Deliberately long tasks (`long_horizon: true`) meet the structural floor: at least 7 business
subtasks, 4 non-trivial decisions, 3 cross-state dependencies, and 2 evidence revisits. Entries of
kind `navigation`, `scroll`, `overlay_open`, or `repeat_click` do not count. The floor is necessary,
not sufficient: they also need two `required` phenomena, at least one information- or
state-bearing:

| Phenomenon | Qualifies when | Does not qualify |
|---|---|---|
| `cross_source_reasoning` | independent app-local sources contribute different facts | more surfaces showing the same fact |
| `implicit_state_inference` | a necessary condition the instruction omits is recovered through a legal visible discovery path | evaluator-only or inaccessible state |
| `multi_item_state_tracking` | state and exceptions are preserved over an explicit item denominator | repeating one lookup |
| `conflict_disambiguation` | stale or contradictory app facts are resolved by an evidenced authority rule | a conflict the app does not show |
| `visual_spatial_precision` | a business result depends on precise location, order, range, or selected state | ordinary clicks; fragility of the implementation |

Each phenomenon is `required` (with evidence items), `not_applicable`, or `unresolved`; an
unresolved phenomenon keeps the candidate out. A shorter task may be authored when it has
independent user value; it does not count toward the long-horizon quota.

Every candidate also binds verification and repair: the terminal assertions, the evidence that
rechecks them, the condition that reopens an earlier decision, and the correction path. Ending at a
submit without checking the resulting business state earns no verification credit.

## Instances and diversity

`authority_class` is `reference_exact` when every value comes from source rows, and
`schema_backed_generated` when the task combines schema-backed values no single trace shows
together. The latter requires `generated_instance` with `parameter_domain`, `producer`,
`reset_preflight`, and `holdout`; such values are never presented as observed facts. Instance
variation changes entities and values without claiming a new workflow.

The diversity signature counts templates, not task ids. A candidate is a new representative only
when a primary axis changes (goal family, decision graph, dependency signature, terminal value
type, reasoning family) and it is not equivalent under the others. Entity names, paraphrases,
values, candidate order, route labels, and seeds are instance axes. Near duplicates whose wording
differs but whose goal, decisions, dependencies, terminal, and limitations match are rejected.

## Instruction

The instruction states a user goal and a user-understandable terminal result. It never contains
the candidate id, task ids, action ids, page file names, internal fields, hidden formulas, a fixed
answer order, a direct route, or an answer.

## Never

- bind an item the index does not list, or extract structure from screenshots, existing tasks, or
  the package;
- add hidden metrics, task ids, unsupported app behavior, new transitions, guards, or effects,
  unverifiable outcomes, wording-only diversity, or navigation for length;
- manufacture a source, hidden state, conflict, or precision defect to reach a phenomenon;
- weaken or simplify a candidate after a learner or solver fails it (refinement belongs to
  `diagnose-attempts` and only adds supported obligations).
