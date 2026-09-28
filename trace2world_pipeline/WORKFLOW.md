# Construction workflow

End-to-end order for building one Trace2World environment from a reference trace
set, with the paper stage each command implements. Every command below is a
console script published by `pyproject.toml`; `--help` output is authoritative.

Install first (see `README.md`):

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

`$RESULT` below is the result directory the run writes into; `$TRACES` is the
reference trace directory for one app.

---

## 1. Construct the app specification and realize pages

**Paper stages:** Alg. 1 `Restore` → `Reconcile` → `Factorize` → `Supported`,
then `Project_p` page realization and task composition.

```bash
.venv/bin/t2w-construct \
  --config <config.json> \
  --trace-dirs "$TRACES" \
  --output-dir "$RESULT"
```

This is the single automatic flow. There is no human-feedback gate, no APK
route-prior branch, and no legacy schema-planning branch.

In the L0 fidelity profile, construction writes
`phase_l0_fidelity/l0_visual_evidence_selection.json`, then calls the visual
structure API on its selected screenshots and writes one
`phase_l0_fidelity/instance_structure/*.json` record per instance page plus
`phase_l0_fidelity/visible_structure_inventory.json` for the app. The latter
is a source-visual inventory for later enhancement; task admission still waits
for natural GUI behavior and source-replay acceptance. Export copies the
inventory to `data/visible_structure_inventory.json` and binds its digest in
`resource_manifest.json`. A result with an L0 selection but no current
inventory cannot be exported under the new contract.

Inspect an intermediate stage instead of running to completion:

```bash
.venv/bin/t2w-construct ... --stop-after global_reasoning
.venv/bin/t2w-construct ... --stop-after instance_splitting
.venv/bin/t2w-construct ... --stop-after site_model   # reconciled app specification
.venv/bin/t2w-construct ... --stop-after tasks
```

`site_model` is retained as the stage keyword because the reconciled app
specification is written to `phase_consolidation/site_model/`, a path recorded
in the published environments' resource manifests.

Two stages inside this command are themselves separate modules:

- `trace2world_pipeline.app_spec.reconcile.page_assignment_audit` — the
  page-level reasoning fix, awaited unconditionally during global reasoning.
- `trace2world_pipeline.resources.static_resource_pipeline` — prompt resource
  extraction, spawned as `python -m …static_resource_pipeline`.

Neither needs a manual invocation; both are listed because they are the two
places the flow leaves the parent process.

---

## 2. Materialize action hosts and action effects

**Paper stage:** `Project_p`, state-dependent action effects.

```bash
.venv/bin/t2w-compile-action-hosts \
  --result-dir "$RESULT" \
  --write-report "$RESULT/action_host_report.json"
```

Produces `action_host_manifest.json` and the action-effect DOM. The published
environments contain this stage's output (`action_effect` in 435 files,
`data-ghostweb-overlay-layer-role` in 1167).

---

## 3. Materialize runtime state

**Paper stage:** runtime state ownership — state is owned by the environment
kernel, not the compiled page.

```bash
.venv/bin/t2w-state-kernel   --result-dir "$RESULT"
.venv/bin/t2w-state-runtime  --result-dir "$RESULT"
```

`t2w-state-kernel --replace-existing` rebuilds an existing kernel.

---

## 4. Export the environment

**Paper stage:** Compiled Page Specifications and Environment Export.

```bash
.venv/bin/t2w-export-environment \
  --result-dir  "$RESULT" \
  --package-dir "$RESULT/package" \
  --env-name    <environment_id> \
  --response-mode env_text
```

This is the stage that produced the released environments. It
injects the runtime checkpoint bridge into every generated page, wires
checkpoints, builds the formal artifact manifest, and writes the package:

```text
<package>/app/                            compiled pages, runtime, SDK
<package>/data/formal_artifact_manifest.json
<package>/data/package_host_graph.json
<package>/resource_manifest.json
<package>/package_validation_report.json
<package>/package_provenance.json
<package>/tasks.json
```

The exporter validates its own output and exits non-zero when
`package_validation_report.json` reports issues; the report names each one. Add
`--finalize-archive` to also emit a tarball, and
`--evaluator-export-mode {required,disabled_artifact_only,disabled_env_only}` to
control evaluator export.

A differential test of this command against the development-tree original on one
real result directory produces the same 155 files, 149 of them byte-identical,
the rest differing only in the run's own paths and timestamps.

---

## 5. Verify

**Paper stage:** Alg. 2 `Probe` — the verification contract and probe matrix.

```bash
.venv/bin/t2w-verify --config <instrumentation-config.json>
```

Generates contract-first instrumentation, compiles executable evaluators, and
executes negative probes against the result directory named in the config.

---

## 6. Close the environment

**Paper stage:** Alg. 2 — `BuildObligations` → `Execute` → `Review` → `Probe`,
with `Repair`/`Invalidate` bounded by `K`.

```bash
.venv/bin/t2w-close \
  --result-dir "$RESULT" \
  --ledger     "$RESULT/run_management/repair_ledger.json" \
  --operators  <module>:<factory> \
  --write-report "$RESULT/run_management/closure_report.json"
```

`--repair-budget` defaults to 3: an initial check, three repairs, and a final
check. `--report` summarises an existing ledger without executing anything.

The ledger is persistent and per slice. It is the only authority on spent
budget, so a slice that is requeued or refined resumes with what it had left
instead of a fresh allowance. Point every run of the same result directory at
the same ledger file.

`--operators` names `module:factory`; the factory takes no argument and returns
an object satisfying `trace2world_pipeline.closure.ClosureOperators`:

| Operator | Algorithm | Constraints |
|---|---|---|
| `build_obligations(R)` | `BuildObligations` | — |
| `execute(E, O)` | `Execute` | replays affected traces at native reset in a fresh browser context, stops at the first blocker; no review oracle, no state injection, no repair |
| `review(R, X, O)` | `Review` | independent, frozen evidence packet, reads the full source trace first, allows valid alternative paths and unequal lengths; cannot modify or drive anything |
| `probe(E, C, O)` | `Probe` | runs only after `review` passes |
| `repair(E, z, C)` | `Repair` | changes the owned layer under fixed `C`, returns the edited `Delta` |
| `invalidate(Delta)` | `Invalidate` | drops execution, review, probe and checkpoint evidence intersecting `Delta`; affected traces restart at step zero |
| `revision_evidence(E)` | — | the revision-matched evidence `Verified` returns |

`Owner(z)` returns one of `semantics`, `implementation`, `supervision`, `model`,
`provider`. Only `implementation` and `supervision` are repairable; `semantics`
leaves through `Upstream` because only source-trace evidence may revise the app
specification; `model` and `provider` are not environment defects and drop.

A dropped slice records its owner and which exit it took — `cap` when the budget
ran out, `not_repairable` when the failure was never repairable. The two are
different exits and must not be merged when reporting.

---

## Not in this package

- Training code, RL rollout, and model checkpoints.
- Downstream packaging adapters for other environment families.
- The development tree's per-app one-off build and repair scripts.
- Reference traces. The constructor consumes them; the release does not ship them.

The environments this workflow produced are published pre-exported as the
Hugging Face project dataset `LEONW24/Trace2World` (`environments/`).
