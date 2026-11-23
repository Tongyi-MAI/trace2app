# Candidate Binding

A task candidate is authored from the task-facing index (`task-index.md`) and nothing else: task
authoring performs no structure extraction of its own (paper Appendix C.1, "Candidate binding";
condensed prompt in Appendix G, "Task authoring"). `scripts/validate_task_candidate.py` checks the
mechanical part of this contract; the semantic part below is the author's and the reviewer's.
`enhancement_blueprint`, `information_topology`, `difficulty_delta`, and
`template_equivalence_group` are candidate fields the validator does not require: they carry the
authoring and admission evidence a reviewer reads.

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
  "information_topology": [
    {"source_id": "store_detail.delivery_fee", "owner": "store_detail", "fields": ["delivery_fee"],
     "entity_scope": "store", "freshness": "per visit", "authority_rank": 1,
     "evidence": ["<item_id>"], "conflicts": ["store_list.delivery_fee"],
     "discovery_path": ["store_list", "store_detail"]}
  ],
  "enhancement_blueprint": {
    "index_revision": "", "baseline": "", "source_ids": [], "freshness_authority_rules": [],
    "conflicts": [], "implicit_state_discovery_paths": [], "terminal_equivalence_class": "",
    "strategy_families": [], "why_each_family_adds_user_visible_work": "",
    "unsupported_assumptions": [], "prohibited_shortcuts": [], "required_materialization_gaps": []
  },
  "difficulty": {
    "long_horizon": true,
    "phenomena": {"multi_item_state_tracking": {"status": "required", "evidence": ["<item_id>"]}},
    "delta": {
      "baseline": "", "shared_items": [], "added_subtasks": [], "added_subtask_consumers": [],
      "added_decisions": [], "added_cross_state_dependencies": [], "added_evidence_revisits": [],
      "added_reasoning_conditional_batch_or_safe_terminal_depth": [], "not_counted": []
    }
  },
  "verification_and_repair": {"terminal_assertions": [], "recheck_evidence": [], "correction_path": []},
  "diversity_signature": {
    "archetype_id": "", "goal_family": "", "decision_graph": "", "dependency_signature": "",
    "terminal_value_type": "", "reasoning_family": "", "authority_class": "source_exact",
    "structure_surface_families": "", "information_topology_shape": "", "phenomenon_and_repair_shape": ""
  },
  "template_equivalence_group": {"group_id": "", "member_ids": [], "representative_id": "",
                                  "equivalence": ""},
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

## Authoring order and blueprint

Author every representative candidate in this order:

```text
current task-facing index
-> app-local information/state topology
-> item opportunity and limitation matrix
-> user goal and terminal business value
-> enhancement strategy family
-> business subtask/decision/state-dependency graph
-> visible evidence, verification/repair path, acceptable terminal set and safe boundary
-> structural floor, difficulty phenomena, difficulty delta and diversity signature
-> user-facing instruction
-> natural-entry, generated-instance and evaluator preflight
```

The opportunity matrix identifies which current items can support composition, comparison, state
continuation, conditional behavior, batch work, recovery, or safe stopping. It also records why a
tempting idea is unsupported. Never begin with a generic "make it longer/harder" prompt.

Record an `enhancement_blueprint` per candidate with:

- index revision and the exact item, surface, and evidence refs;
- information-topology revision, the exact source ids, freshness and authority rules, conflicts, and
  implicit-state discovery paths the task uses;
- the baseline task or template, or `new_from_visible_opportunity`;
- user goal, terminal value, and the acceptable terminal equivalence class;
- the strategy family or families, and why each adds user-visible work;
- business subtasks, non-trivial decisions, cross-state dependencies, and evidence revisits;
- reasoning, aggregation, conditional, and batch contracts when applicable;
- natural entry, solution policy, reset boundary, verification and repair obligations, and the
  evaluator-observable terminal;
- structural-floor applicability, the difficulty-phenomenon profile, the difficulty delta, the
  diversity signature, and the instance/template classification;
- unsupported assumptions, prohibited shortcuts, and required materialization gaps.

Every non-terminal business subtask needs a later consumer. A task with no exact index binding is
an idea, not an admitted task.

## Information topology

Visible structure is necessary but not sufficient for information-bearing difficulty. Build an
app-local `information_topology` for every source the candidate consumes. Each source records:

- a stable `source_id`;
- its surface or state owner;
- its visible or schema-backed fields, and its entity scope;
- its freshness or currentness rule;
- its authority rank, with evidence refs;
- its producer-to-consumer dependencies;
- its competing-source conflicts;
- the legal discovery path for any instruction-implicit state.

Treat two panels or pages backed by one unchanged state as one information source unless they
contribute independent facts. The index carries the same per-source rows (`task-index.md`,
`dependencies`).

The topology stays inside the bound app and package: do not add external tutorials,
multimodal-production requirements, asynchronous updates, streaming targets, or simulated-user
questions to manufacture difficulty. If a required fact is neither stated nor legally discoverable
from current app-local evidence or state, reject or refine the task instead of calling it
implicit-state reasoning.

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

Two further families bind the same way. Schema-backed instance generalization varies a reviewed
parameter domain, world seed, candidate order, language, entity, value combination, or initial
state, with producer, visible projection, reset preflight, and holdout; it tests generalization and
does not by itself create representative diversity. Alternative natural-path robustness admits
semantically equivalent visible routes, or diagnoses non-equivalent ones, without leaking a
canonical click script; route variation alone is coverage, not a new representative task, unless it
changes user value or decision and state semantics.

Minimum bindings per family:

| Family | Binds |
|---|---|
| aggregation or decision | operation, minimum visible items, visible input fields and refs, user-visible rule, tie or equivalence policy, derived decision, terminal encoding |
| cross-surface or lifecycle | source and consumer surfaces, exact source ids and fields, canonical state written, read, and preserved, freshness rule, dependency direction, revisit observation, reset behavior |
| conditional, boundary, or recovery | operands, operator, threshold, source and unit when applicable, equality boundary, conflicting-source precedence, true and false branches, invalid or recovery state, observable terminal for each branch |
| multi-entity synthesis | stable entity denominator, order and count, per-entity result schema, entity-to-evidence mapping, missing, duplicate and exception policy, exact conservation, output channel, evaluator projection |
| schema-backed instance | template refs, parameter domains, world or state producer, parameter bindings, visible projection, reset digest and preflight, novelty axes, at least one authoring-held-out combination |
| alternative paths | natural entry, visible route cues, semantic equivalence rule, non-equivalent route exclusions, shared terminal class; never a canonical route hint in the task |
| safe transaction | reversible or irreversible boundary, state before the boundary, user value already obtained, forbidden action, evaluator-observable stop condition |
| evidence authority or implicit state | exact source ids, the omitted instruction field, legal discovery path, freshness and authority rule, conflict set, downstream consumer |
| precision-sensitive operation | precision-bearing business property, visible target or relationship, tolerance or equivalence rule, before and after evidence, terminal consumer |

A family label without these bindings earns neither difficulty nor coverage. Do not force every
family on every app: mark one `not_applicable` only with evidence that the current index cannot
support it without inventing semantics. Unsupported families stay explicit gaps or exclusions; they
are never silently replaced by lexical variants.

Reaching a list, opening a page, or selecting a row is normally an intermediate state. Reject a
candidate that stops at an intermediate list, page, or selection with no user-understandable
terminal value: rebuild it from a user-valued terminal, add evidence-backed decisions, state
dependencies, and revisits, or classify it as a shorter auxiliary task.

## Difficulty

Difficulty follows four capabilities: long horizon, reasoning, memory and state, and recovery or
backtracking. It is a semantic delta from the simplest supported task for the same goal, not a
count of actions.

Record that delta as `difficulty_delta` for every representative task:

- the baseline task or template, and the shared index items;
- added business subtasks and their later consumers;
- added non-trivial decisions;
- added cross-state dependencies and evidence revisits;
- added visible-data reasoning, conditional branches, batch obligations, or safe-terminal depth;
- what was not counted: navigation, scrolling, opening overlays, repeated clicks, prose length, and
  evaluator-only work.

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

A prior admission receipt is not evidence. Reopen the floor and prove it again, and prove each
phenomenon separately: the structural counts and the difficulty phenomena cannot substitute for
each other. `not_applicable` names the task semantic or the unsupported app capability and still
proves a non-trivial user terminal. Never infer applicability from task length, the task id, a prior
queue or ledger record, or a package-wide default. Navigation, repeated reading, disconnected
calculations, and multiple projections of one unchanged state may satisfy a loose count, but they do
not prove information or state difficulty.

An implicit fact the executing agent cannot discover makes the candidate invalid, not difficult.
Three remedies, in this order: bind a legal visible or schema-backed discovery path; move the fact
into the instruction when that preserves the user goal; or remove the obligation. Evaluator-only
knowledge and hidden answers never earn implicit-state credit.

When records conflict, bind each competing source, its freshness or currentness evidence, and one
user-understandable precedence rule before the instruction and the checkpoints are admitted;
without an authority rule the candidate stays unresolved. Do not let a `repair-supervision`
checkpoint privately select the winning record.

The phenomenon contract is closed: `multimodal_editing`, `tutorial_following`,
`dynamic_environment`, `streaming_interaction`, and `proactive_interaction` are outside it. They are
neither requirements nor coverage gaps, and they are no reason to add external tutorials, media
production, asynchronous events, moving targets, or a simulated-user protocol.

When the current schema provides usable numeric or ranked fields, prefer reasoning-bearing tasks
and state the applicable denominator. When it does not, use evidence-backed semantic, state,
lifecycle, conflict, multi-item, or precision difficulty instead of inventing numbers: never invent
numeric fields, and never duplicate arithmetic templates to hit a percentage. Derive any
aggregation, comparison, rank, or threshold target from the current opportunity matrix and its
exact supported denominator; reject a package-wide convenience quota.

Effective action count, completion time, and checkpoint count are post-admission calibration
evidence, never authoring targets and never admission thresholds. A benchmark-scale task length,
checkpoint count, or model score is not an admission threshold.

Before admission, when an obligation was illegitimately claimed, repair the difficulty design
instead of the task's value: merge surfaces that are not independent sources, define source
precedence for real conflicts, repair the tracked-item denominator, add terminal verification or
correction. This repair is a pre-admission authoring step only. It never runs after a learner or
solver failure: refinement after a failure belongs to `diagnose-attempts` and only adds supported
obligations, because Algorithm 5 refinement is one-directional.

Every candidate also binds verification and repair: the terminal assertions, the evidence that
rechecks them, the condition that reopens an earlier decision, and the correction path. Ending at a
submit without checking the resulting business state earns no verification credit.

## Instances and diversity

`authority_class` is `source_exact` when every value comes from source rows, and
`schema_backed_generated` when the task combines schema-backed values no single trace shows
together. The latter requires `generated_instance` with `parameter_domain`, `producer`,
`reset_preflight`, and `holdout`; such values are never presented as observed facts. Instance
variation changes entities and values without claiming a new workflow.

The diversity signature counts templates, not task ids. A candidate is a new representative only
when a primary axis changes (goal family, decision graph, dependency signature, terminal value
type, reasoning family) and it is not equivalent under the others. Entity names, paraphrases,
values, candidate order, route labels, and seeds are instance axes. Near duplicates whose wording
differs but whose goal, decisions, dependencies, terminal, and limitations match are rejected.

The five axes from `goal_family` through `reasoning_family` are primary semantics; `archetype_id`
is identity; `structure_surface_families`, `information_topology_shape`,
`phenomenon_and_repair_shape`, and `authority_class` are coverage and provenance. A task is a new
representative only when at least one primary axis changes materially and the candidate is not
equivalent under the remaining graph and terminal semantics.

Group every representative and its equivalent instances into one explicit
`template_equivalence_group`: a stable group or archetype id, the member ids, one reasoned
representative, and the equivalence explanation.

Structure and surface coverage is tracked separately: a new surface can close coverage without
creating a new task archetype. A new page or surface becomes representative task diversity only
when the user goal, decision graph, cross-state dependency, terminal value, or reasoning and
limitation semantics also change; a route or label change alone is not a new archetype.

Do not create Cartesian products merely to hit a requested task count. If an exact count is
requested, preserve it as a delivery denominator while separately reporting the
representative-template count, the instance-only count, and any uncovered semantic opportunities.
Preserve the seed and enhanced task denominators separately.

## Admission

A catalog row is not an admitted task. Process candidates in a stable order and complete, for each:

1. the generated-instance runtime preflight, when applicable;
2. the query, limitation, natural-entry, and terminal verdict;
3. the information-source independence, implicit-state discoverability, conflict authority,
   tracked-item conservation, and precision applicability verdict;
4. the terminal verification and repair verdict, and the evaluator's alternative-terminal verdict;
5. the required refinement or replacement;
6. a fresh binding check and a fresh review after any semantic change.

Only current-pass candidates enter. Rejected or stale attempts remain immutable evidence and earn
no denominator credit. A wrong instruction, terminal, solution policy, natural entry, or hidden
constraint is a `task`-owner defect and is recorded as such on the queue (`SKILL.md`, Statuses); a
missing host for an operation the index already supports is `implementation`, and a checkpoint that
disagrees with correct behavior is `supervision`.

Portfolio admission additionally requires an exact representative or instance-only classification,
one template-equivalence group per task, explicit strategy, structure, goal, terminal,
source-topology, difficulty-phenomenon, and verification coverage, and named missing or
not-applicable opportunities. Task ids, counts, and lexical variation do not establish diversity.

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
- stop at an intermediate list, page, or selection with no user-understandable terminal value;
- let a checkpoint privately select the winning record among conflicting sources;
- create Cartesian products to hit a requested count, or sell instance variants as diversity;
- invent numeric fields, or duplicate arithmetic templates to hit a percentage;
- treat action count, completion time, or checkpoint count as an authoring target or an admission
  threshold;
- begin from a generic "make it longer/harder" prompt, or claim a new archetype from a route or
  label change;
- weaken or simplify a candidate after a learner or solver fails it (refinement belongs to
  `diagnose-attempts` and only adds supported obligations), or repair a difficulty-design defect
  after such a failure.
