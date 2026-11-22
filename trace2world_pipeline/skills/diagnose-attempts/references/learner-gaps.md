# Learner Gaps

After a batch is frozen, the learner attempts of its screening are summarized into conditioning
notes for the next authoring pass (paper Appendix C.3; condensed prompt in Appendix G, "Rollout-to-gap
summary for the next batch"). The notes steer which supported opportunities the next candidates
use; they never change app semantics or the index.

## Input

- the frozen batch manifest of the preceding batch (`batch.json`) and the candidates it selected;
- the screen packets and verdicts of every candidate screened in that batch, including held,
  refined, and failed ones;
- each candidate's capability and dependency labels from its bindings and difficulty profile;
- solver outcomes where present, and the attributed findings.

Never read held-out benchmark outcomes.

## Procedure

1. Separate non-learner faults first. Invalid attempts and attempts carrying an `implementation`,
   `supervision`, `semantics`, `task`, or `provider` finding are not learner evidence. An
   environment or solver failure is never a learner gap.
2. Group the remaining valid learner attempts by supported capability and dependency motif
   (aggregation, comparison, cross-surface state, lifecycle revisit, recovery, multi-entity
   tracking, and the candidate's required phenomena). For each group record: attempts, terminal and
   partial progress, repeated or no-effect actions, lost state or entity bindings, recovery
   behavior, weak combinations of motifs, and saturation (learner mean reward > 0.8).
3. Write evidence-linked notes:

```json
{
  "schema": "trace2world.learner_gaps.v1",
  "batch": "task_supply/batch.json",
  "excluded_attempts": {"invalid": 0, "non_learner_findings": 0},
  "motifs": [
    {
      "motif": "multi_item_state_tracking",
      "candidates": ["..."], "attempts": 0,
      "mean_reward": 0.0, "terminal_successes": 0,
      "patterns": [{"pattern": "drops the second entity after returning to the list", "attempts": ["..."]}],
      "verdict": "weak | unsaturated | saturated",
      "uncertainty": "attribution incomplete for 2 attempts"
    }
  ]
}
```

Weak or unsaturated motifs are targets for the next batch; saturated motifs are deprioritized.
Carry uncertainty explicitly when attribution is incomplete.

## Never

- count invalid or fault-attributed attempts as learner competence evidence;
- request interactions the index does not support, or alter app semantics;
- inspect held-out benchmark outcomes.
