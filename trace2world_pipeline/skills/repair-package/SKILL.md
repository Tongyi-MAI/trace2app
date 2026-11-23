---
name: repair-package
description: "Repair of Trace2World Algorithm 4 (environment validation and repair) for defects owned by `implementation`. Use when review-trace or probe-contract returned a repairable failure in the package itself: rendering, route wiring, storage, bindings, projection, a missing implementation of a supported relation, or a supported state update or control behavior realized differently from the source. Edits only the package under the frozen app specification, returns the edited dependency set Delta and raw assertions, and never certifies its own fix."
---

# Repair package

`(E, Delta) <- Repair(E, z | C)` in Algorithm 4 (paper Appendix B.2; condensed rule in Appendix G,
"Workflow rule: repair under a frozen app model"), for failures whose owner is `implementation`.
The input is the existing package, one reproduced defect packet, and the source evidence. The
output is an edited package, the set of edited files `Delta`, and a repair report. Execution,
review, and probes then run again from reset on the new revision; this skill does not decide
whether the fix worked.

Read `references/repair-ladder.md` before the first edit: the state-model questions, the issue
map, the state-ownership gate, the acceptance expansion, and the required assertions.

## Scope

Repairable means closable by editing the package under the fixed specification `C`. In scope:

- rendering, layout, density, resources, and overlays of a supported page;
- route wiring and action hosts of a supported transition;
- storage, state handoff, initialization, and reset of supported state;
- bindings and visible projections of supported state;
- a missing implementation of a supported relation;
- a supported state update or control behavior realized differently from the source.

Not in scope, and never done here:

- editing the app specification (`$RESULT/phase_consolidation/site_model/`), the source traces,
  the obligation checklists, or execution, review, and probe packets;
- choosing an interpretation in the UI, or adding a page identity, state or data relation, guard,
  or effect that no source evidence supports;
- checkpoint, evaluator, binding, or terminal-policy edits when the package already realizes the
  state: those belong to `repair-supervision`;
- regenerating the package from the specification: a fresh compilation is a construction
  revision, not a repair.

If closure would need anything out of scope, stop, restore the package, and return
`not_repairable` with a referral (`semantics` for unsupported behavior, `provider` for an
infrastructure cause). `closure-loop` then drops the slice or refers it upstream.

## Procedure

1. Read the defect packet, the cited obligation rows, the marked source screenshots around the
   step, and the execution screenshots. Reproduce the defect on the current revision with a
   natural browser action before editing; an unreproduced packet goes back to `closure-loop`.
2. Restate the defect at the model level (surface kind, lifecycle, perceived continuity,
   modalities, owning surface) and find the first stale consumer with the issue map. "Make this
   button clickable" is not a diagnosis.
3. Snapshot the package and the frozen inputs:

   ```bash
   python "$T2W_SKILLS/repair-package/scripts/package_delta.py" snapshot \
     --package "$RESULT/package" \
     --frozen "$RESULT/phase_consolidation/site_model" --frozen "$CHECKLISTS" \
     --output "$REPAIR/before.json"
   ```

   `$REPAIR` is `$RESULT/run_management/repairs/<slice_id>/wave_<k>/`.
4. Edit the earliest stale consumer, and every active copy of the same behavior in the package
   (shared pages, action-host maps, route tables, runtime helpers). Fix the whole class of the
   defect on that surface, not the one reported pixel.
5. Run the state-ownership audit over the package and resolve every finding:

   ```bash
   python "$T2W_SKILLS/repair-package/scripts/audit_state_ownership.py" \
     --root "$RESULT/package" --output "$REPAIR/state_ownership_audit.json"
   ```

6. Run the focused browser assertions of `references/repair-ladder.md` against the served package
   and persist their raw JSON report unedited (`$REPAIR/raw_assertions.json`). These are the
   repairer's own checks; they do not replace execution, review, or probes.
7. Compute Delta and write the report:

   ```bash
   python "$T2W_SKILLS/repair-package/scripts/package_delta.py" diff \
     --before "$REPAIR/before.json" --output "$REPAIR/delta.json"
   python "$T2W_SKILLS/repair-package/scripts/validate_repair_report.py" \
     --report "$REPAIR/repair_report.json"
   ```

   `diff` fails when a frozen input changed or the package did not change. The validator fails on
   any closure claim, on a delta that differs from the measured one, or on a raw report whose
   SHA-256 or cases do not hold.

## Repair report

`$REPAIR/repair_report.json`, schema `trace2world.repair_report.v1`:

| Field | Content |
|---|---|
| `slice_id`, `owner` | the slice, and `implementation` |
| `status` | `repaired` or `not_repairable` |
| `defect` | `stage` (`review` or `probe`), `code`, `trace_id`, `step_index`, `expected_state`, `observed_state`, `obligation_ids`, `packet_path` |
| `diagnosis` | `issue_class` (one of the issue map's classes), `first_stale_consumer`, `rationale` |
| `delta_path`, `delta` | `delta.json` and its sorted file list: the `Delta` that `Invalidate` consumes |
| `package_revision_before`, `package_revision_after` | copied from `delta.json` |
| `assertions` | `raw_report_path` and `raw_report_sha256` of the unedited browser report |
| `acceptance_expansion` | the reported surface and every adjacent check with `pass` or a reason |
| `state_ownership_audit_path` | the audit output |
| `referral` | only for `not_repairable`: `owner` and `unsupported_behavior` |

## Never

- use DOM `el.click()`, forced dispatch, state or query injection, or direct navigation to make
  an assertion pass;
- let a trace id, step index, or action id choose a business value;
- weaken a check, add a success shortcut, or edit replay tooling to hide a defect;
- report a fix from a reached route, a visible option, or a green replay;
- mark the slice verified: only `closure-loop` returns `Verified`, after fresh execution, review,
  and probes on the new revision.
