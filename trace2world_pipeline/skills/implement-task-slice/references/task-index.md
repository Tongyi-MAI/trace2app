# Task-Facing Index

The index is extracted from the source-validated package, its replay records, and the frozen
specification's identifiers and support anchors (paper Appendix C.1; condensed prompt in
Appendix G, "Task-facing structure extraction"). It is read-only and has no semantic authority:
it lists what the validated package already does, with references to established operations,
and introduces no behavior.

## Extraction rules

- Extract; do not model, generate tasks, or introduce app behavior.
- Every emitted item references an existing supported relation and its source evidence. An item
  that cannot be resolved is dropped, never inferred.
- Guards, effects, and destinations appear as references to established operations, not as a
  second copy of transition semantics.
- Exclude unsupported or unresolved behavior, inaccessible producers, incompatible types or
  scope, and non-observable outcomes. Keep supported items that the package does not implement
  yet, tagged `supported_not_implemented`.
- Never infer a transition, guard, or effect; never invent a page, route, field, relation, state,
  action, lifecycle, or dependency; never infer diversity from wording or names.

## Format

`$RESULT/task_supply/index.json`:

```json
{
  "schema": "trace2world.task_index.v1",
  "package_revision": "sha256:...",
  "specification": "phase_consolidation/site_model/",
  "items": [
    {
      "item_id": "store_list.sort_by_distance",
      "kind": "control | surface | host | entity_schema | field | operation | outcome",
      "surface_id": "store_list",
      "supported_operation_ids": ["op.store_list.sort"],
      "observable_fields": ["store.distance", "store.name"],
      "evidence": ["trace:<trace_id>:<step>", "replay:<execution packet>"],
      "implementation_status": "implemented | supported_not_implemented"
    }
  ],
  "dependencies": [
    {
      "producer": "store_detail.delivery_fee",
      "consumer": "checkout.choose_store",
      "value_type": "currency",
      "entity_scope": "store",
      "freshness": "per visit",
      "discovery_path": ["store_list", "store_detail"],
      "evidence": ["trace:<trace_id>:<step>"]
    }
  ],
  "opportunities": ["aggregation", "comparison", "cross_surface_state", "lifecycle_revisit",
                    "recovery", "multi_entity_tracking"],
  "exclusions": [{"what": "...", "reason": "unsupported | unresolved | not observable"}]
}
```

`item_id` values are stable across refreshes: an unchanged item keeps its id.

## Refresh

The index is refreshed after each instantiated task slice, not re-derived by modeling. After
`Implement`, re-extract only the items whose surfaces, hosts, operations, or outcomes lie in the
slice's `Delta`: mark instantiated items `implemented`, add the new task's observable outcome, and
keep every other item unchanged. Record the new `package_revision`. A queued candidate is rebound
by setting its `index_sha256` to the new digest and re-running `validate_task_candidate.py`; its
item-level checks decide whether it is still admissible.
