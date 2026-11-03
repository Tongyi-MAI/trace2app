# Trace2World constructor (`trace2world_pipeline`)

The environment constructor described in *Trace2World: Reconstructing App Worlds from Real Interaction Traces for Scalable GUI Agent Training*. It takes a reference trace
set for one real app and produces a verified, executable environment together
with its tasks, evaluators and evidence sidecars.

The environment portfolio this constructor produced (50 apps, 10 domains, from
1,570 source traces) is distributed as the
Hugging Face dataset
[`LEONW24/Trace2World`](https://huggingface.co/datasets/LEONW24/Trace2World) (`environments/`)
and are hosted and evaluated by the sibling package
`trace2world_env`. The reference traces they were
built from are not distributed.
This package is the construction path only.

## Install

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

The only optional extra is `.[semantic]` (sentence-transformers/scikit-learn
clustering fallback); the published run never enters that branch.

## Model configuration

Every stage calls one OpenAI-compatible endpoint through
`trace2world_pipeline.core.llm_caller`. Credentials are read from the
environment only:

```bash
export OPENAI_API_KEY='...'
export OPENAI_BASE_URL='https://api.openai.com/v1'   # optional; any OpenAI-compatible gateway
```

The config file selects models: `deployment` is the default model,
`stage_model_routing` maps a stage name or prefix to another model, and
`model_concurrency` / `max_concurrent` bound in-flight requests. Models whose
name starts with `gpt-5`, `o1`, `o3`, or `o4` receive `reasoning_effort`;
other models are called without it. The `Generate` image mode uses the same
key with the Images API (`OPENAI_IMAGE_MODEL`, default `gpt-image-1`).

Two further external services are used by `t2w-construct`:

- **MinerU** (`https://mineru.net`) performs the L0 prompt-resource extraction
  from reference screenshots. Set `MINERU_API_KEY`; the URL and rate limits are
  configurable through `mineru_*` config keys. The released reference traces
  are raw recordings (screenshots, actions, instructions, terminal contracts)
  and carry no extraction products, so this stage runs on a fresh build; a
  result directory that already contains `filtered_resources.json` skips it.
- **Chromium** for DOM-contract probing comes from the Playwright-managed
  browser installed above; `T2W_CHROMIUM_EXECUTABLE` overrides it.

Image placeholders are resolved from the trace screenshot library; with
`image_mode: "Generate"` unresolved placeholders are generated through the
Images API. Audio, video, and document placeholders are left untouched.

## Run

The automatic construction path:

```bash
.venv/bin/t2w-construct \
  --config <config.json> \
  --trace-dirs <reference_trace_dir> [...] \
  --output-dir <result_dir>
```

`--stop-after {global_reasoning,instance_splitting,tasks,site_model}` halts after
a named stage for inspection. The constructor exposes exactly one construction
flow; there is no human-feedback gate and no alternative route-prior branch.

With `pagegen_logic_flow: "l0_fidelity"`, construction keeps the existing
page-level L0 visual selector in its PageGen preparation position. Immediately
after selection, a multimodal API call extracts visible components from each
selected Reference screenshot. The producer writes per-instance-page records
under `phase_l0_fidelity/instance_structure/` and one source-linked
`phase_l0_fidelity/visible_structure_inventory.json`, joining instances only
under their reconciled general-page identity and component key. Missing source
images, malformed responses, or conflicting component types stop inventory
publication. Each successful image call has a reusable input-hashed packet in
`phase_l0_fidelity/debug/structure_extraction/`. Configure the stage with
`stage_configs.visible_structure_inventory` (`model`, `reasoning_effort`,
`max_concurrent`); otherwise it inherits the construction model and global
concurrency. This profile uses source reference tasks and skips task-driven
AppSpec enrichment; a configuration that enables synthetic task generation or
task-inferred enrichment is rejected. The inventory describes visible structure, not unobserved action
effects or completed source replay.

The later paper stages are separate console scripts:

| Command | Paper stage |
|---|---|
| `t2w-construct` | Alg. 1 `Restore`/`Reconcile`/`Factorize`/`Supported` + page realization + task composition |
| `t2w-compile-action-hosts` | `Project_p` state-dependent action effects |
| `t2w-state-kernel`, `t2w-state-runtime` | runtime state ownership |
| `t2w-export-environment` | compiled page specifications and environment export — the stage that produced the released environments |
| `t2w-verify` | Alg. 2 `Probe`: verification contract and probe matrix |
| `t2w-close` | Alg. 2: bounded `Execute`/`Review`/`Probe` with `Repair`/`Invalidate` |

[`WORKFLOW.md`](WORKFLOW.md) gives the end-to-end order with real flags.

## Stage layout

| Package | Paper stage |
|---|---|
| `app_spec/` | Reconciled app specification `S`: Structure/State/Data relations and the evidence map `Sigma` |
| `app_spec/reconcile/` | `Restore` and `Reconcile`: trace-local evidence to canonical page/host/entity identity |
| `page_realizer/` | `Project_p`: compiled page specifications and their DOM/route/effect materialization |
| `compiler/export/` | Environment export: package, formal artifact manifest, checkpoint wiring |
| `state_kernel/` | Runtime state ownership, separate from the compiled page |
| `verification/` | `Probe`: instrumentation, action/reward authority, negative probes |
| `validation/` | `Execute` and `Review`: replay against obligations, independent review |
| `closure/` | Alg. 2: the bounded `Repair`/`Invalidate` loop, the repair ledger, and the three terminal outcomes |
| `task_generation/` | `Compose`: tasks derived from the app specification |
| `runtime_backend/` | Generated app SDK and runtime contract |

## Algorithm 2

`closure/` implements the environment validation and repair loop. The control
structure is owned here: the repair budget, the per-slice ledger, the evidence
invalidation, and the terminal outcomes. The labour of `Execute`, `Review`,
`Probe` and `Repair` is supplied through `closure.ClosureOperators` by the
implementation agents; `WORKFLOW.md` documents that contract.

```bash
.venv/bin/t2w-close --result-dir "$RESULT" --ledger "$LEDGER" --operators <module>:<factory>
```

The budget defaults to 3, giving an initial check, three repairs, and a final
check. The ledger is persistent and per slice, so a requeued or refined slice
resumes with the budget it had left rather than a fresh allowance.

A run ends in exactly one of `Verified`, `UpstreamReferral`, or `DroppedSlice`.
A dropped slice records its owner and its exit: `cap` when the budget ran out,
`not_repairable` when the failure was never repairable.

## Frozen runtime contract

The constructor emits a runtime contract that the published environments and
their evaluators read back. These identifiers keep their original namespace and
are deliberately **not** renamed to Trace2World vocabulary — renaming them would
invalidate every released environment:

- `data-ghostweb-*` DOM attribute namespace (>100k occurrences in the exports)
- `window.GhostwebRouteExecution`, `window.GhostwebRuntimeState`,
  `window.GhostwebCheckpointRuntime`, `window.__ghostwebExecutionContext`
- `ghostweb.*` runtime schema identifiers
- `phase_consolidation/site_model/` artifact paths and `site_model_*` config keys,
  which appear in exported resource manifests

Python-level identities do use the paper's vocabulary: the app-specification
builder is `AppSpecBuilder`, its product is `AppSpec`, and internal classes are
`Trace2World*`.

## Scope

This package contains the construction path only. It does not contain the
downstream packaging adapters for other environment families, training code, or the ad-hoc
per-app build scripts from the development tree; the published environments are
shipped pre-exported on the Hugging Face Hub.
