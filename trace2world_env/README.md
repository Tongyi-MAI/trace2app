# `trace2world_env` — environment host, evaluators, and bundled agent

The standalone runtime for the Trace2World environments: a Playwright-backed
HTTP host that serves the released environment portfolio (one resettable browser
environment per app, 50 apps across 10 domains), the frozen evaluators and
SQLite state kernel that produce rewards, and the `trace2world_env_text`
text-feedback agent used in the paper.

The environments themselves are **not** in this package. Download them with
[`scripts/download_environments.py`](../scripts/download_environments.py)
(Hugging Face dataset
[`LEONW24/Trace2World`](https://huggingface.co/datasets/LEONW24/Trace2World), `environments/`);
the resulting directory contains `environments_manifest.json` and
`environments/<id>/` and is what `--environments-dir` points at. Below,
`$RELEASE` is that directory.

## Contents

```text
trace2world_env/
├── README.md
├── pyproject.toml                 # package trace2world; declares the 3 CLIs
└── src/trace2world/
    ├── env/                       # Playwright hosting, action execution, evaluator,
    │                              #   evidence contract, SQLite state kernel, CLI
    ├── agent/                     # text-feedback policy, tool parser, prompt, HTTP backend
    ├── schema/                    # configuration and evaluation data models
    ├── utils/                     # image and message helpers
    └── cli/                       # rollout CLI and 20-environment smoke CLI
```

35 modules plus one prompt template: the minimal slice along the single path
"hosting + evaluation + text-feedback agent". Training, rollout scheduling,
data pipelines and other environment families are not included.

Requirements: Linux x86-64, Python >= 3.12, Playwright Chromium (~0.7 GB), no
GPU. Dependencies: `click`, `fastapi`, `httpx`, `jinja2`, `pillow`,
`playwright`, `pydantic`, `uvicorn`. If a Chromium binary is already present,
skip `playwright install` and pass `t2w-serve --browser-executable <path>`.

## Command lines

| Command | Entry point | Purpose |
|---|---|---|
| `t2w-serve` | `trace2world.env.__main__:main` | Host all environments; select the environment by `task_id` |
| `t2w-rollout` | `trace2world.cli.run_task:main` | Run one task with the bundled agent and read back the reward |
| `t2w-smoke` | `trace2world.cli.smoke:main` | One browser round-trip self-check per environment |

`t2w-serve` accepts exactly one layout option: `--environments-dir $RELEASE`
(hosts the whole portfolio) or `--environment-dir $RELEASE/environments/<id>`
(one environment, for debugging). Other options: `--state-root` (writable
directory for per-episode SQLite state; the payload itself is read-only),
`--host` / `--port`, `--browser-executable`, `--response-mode`,
`--actionability-preflight/--no-actionability-preflight`,
`--allow-package-validation-status`.

> **The bind address is constrained.** Environment pages are always published
> at `http://localhost:<port>/app`, and Playwright only accepts
> `127.0.0.1 / localhost / ::1` as the page origin. `--host` must therefore keep
> `localhost` reachable: use `127.0.0.1` for local use, or `0.0.0.0` when the
> API must be reachable remotely. Binding only a private-network IP makes
> `POST /task/init` fail in `Page.goto` with `ERR_CONNECTION_REFUSED`.

After start, `GET /health` reports `ok=true` and
`portfolio={"environment_count":…,"task_count":…}`, and `GET /tasks`
returns every loaded task with `task_id`, `instruction`, `start_page`,
`initial_state`.

## Tasks

The tasks of the release are available in three places with identical content:

| Location | Use |
|---|---|
| `$RELEASE/environments_manifest.json` -> `environments[].task_ids` | Offline: which `task_id`s belong to which environment |
| `$RELEASE/environments/<id>/tasks.json` -> `tasks[]` | Offline: a task's `instruction`, `start_page`, `initial_state`, `runtime_profile` |
| `GET /tasks` | Online: every task actually loaded by the host |

```bash
jq -r '.environments[] | "\(.id)\t\(.task_count)"' "$RELEASE/environments_manifest.json"
jq -r '.environments[] | select(.id=="kfc") | .task_ids[]' "$RELEASE/environments_manifest.json"
jq -r --arg t task_20260114_154634_341917 \
   '.environments[] | select(.task_ids | index($t)) | .id' "$RELEASE/environments_manifest.json"
```

There is no manual environment selection: the host routes each `task_id` to
the page, tasks, evaluator and state kernel of its environment.

**Initial state.** Every task is task-conditioned: a task may declare a non-empty `initial_state`.
`/task/init` injects it verbatim into the page context of that episode
(`ghostweb.task_initial_state` and `window.GhostwebTaskInitialState`), where
the environment's own runtime projects it into concrete page content;
`state_kernel_sqlite` environments additionally seal it into the episode's
initial SQLite snapshot. Every `/task/init` allocates an independent episode
`sid`, so episodes never share state.

## HTTP API

One rollout is the following sequence of calls:

| Step | Endpoint | Description |
|---|---|---|
| 1 | `POST /display/acquire` | Acquire an isolated browser context; returns `logical_id` |
| 2 | `POST /task/init` `{task_name, display_id}` | Initialise page and session state for the task |
| 3 | `GET /task/goal?task_name=&display_id=` | Read the task instruction |
| 4 | `GET /screenshot?display_id=&return_b64=true` | Observation: `b64_png`, `current_url`, `page_filename`, `viewport`, `device_scale_factor` |
| 5 | `POST /step` `{display_id, action}` | Execute one action; returns the new observation fields; on rejection also `env_text` and `actionability_feedback` |
| 6 | repeat 4-5 | until `answer` / `terminate` is emitted or the step budget is exhausted |
| 7 | `GET /task/eval?task_name=&display_id=&preview=false` | Final reward and verdict details |
| 8 | `POST /task/tear_down` + `POST /display/release` | Release session and context |

Auxiliary endpoints: `GET /health`, `GET /tasks`, `GET /display/status`,
`GET /display/page_state`, `GET /display/runtime_state`,
`GET /task/state_snapshot` (read-only inspection, useful for auditing).

When the environment accepts an action, `/step` returns `ok=true` with
`env_text` = `null`; when it rejects it, `env_text` is one `ERROR[...]`
feedback line (e.g. `NOT_INTERACTIVE`, `NO_EDITABLE_FOCUS`, `INVALID_ACTION`).
Out-of-bounds, non-interactive or malformed actions consume one step, leave the
business state unchanged, and never reveal the correct target.

**Concurrency.** Every `display_id` is an independent browser context plus an
independent SQLite session; several can run in parallel against one host. The
release acceptance run used 5 concurrent displays.

## Action space

The `action` of `POST /step` is a dictionary in **screenshot pixel
coordinates** (`viewport x device_scale_factor`). All environments render
with a 430x884 CSS viewport at `device_scale_factor=2`, so screenshots are
860x1768; this is declared per environment in
`resource_manifest.browser_viewport` and the host default matches it.

| action_type | Parameters | Meaning |
|---|---|---|
| `click` / `double_tap` / `long_press` | `x`, `y` | tap / double tap / long press (500 ms) |
| `input_text` (alias `type`) | `text` | type text into the currently editable control |
| `swipe` / `scroll` | `start_x`,`start_y`,`end_x`,`end_y` or `direction` (optional `x`,`y`) | finger movement direction / content movement direction |
| `drag` | `start_x`,`start_y`,`end_x`,`end_y` | drag from start to end |
| `open_app` (alias `awake`) | `app_name` | open an application listed in the episode manifest |
| `navigate_home` / `navigate_back` / `keyboard_enter` | - | system buttons (`system_button` + `button` also accepted) |
| `wait` | `waiting_time` (<= 30 s) | wait |
| `answer` | `text` | submit a text answer and end the trajectory |
| `terminate` | `status=success/fail` | end the trajectory with the declared status |
| `ask_user` | `text` | only available when the page declares an external action host |

The bundled agent's model-facing `mobile_use` tool call uses a different
notation (`click / double_click / long_press / type / open / drag / swipe /
system_button / wait / terminate / answer / ask_user` with coordinates
normalised to `[0, 999]`); `trace2world.agent` translates it into the pixel
dictionary above. A policy that maps normalised coordinates back to pixels
should use the actual size of **each** observation as the denominator rather
than caching the first frame; the bundled agent does this.

## Reward semantics

`GET /task/eval` returns the final scalar reward, decided by the **frozen**
checkpoints and evidence contract inside the environment package. Read it
uniformly as `ev.get("raw_reward", ev.get("score"))`.

Shared fields: `score`, `terminal_success`, `reason`.

- Checkpoint-based environments (17, `browser_checkpoint_events`) add
  `raw_reward`, `checkpoint_reward_vector` (per-checkpoint `reached` /
  `weight` / `reward_contribution`), `required_checkpoints`,
  `missing_required_checkpoints`, `env_valid`.
- State-kernel environments (`kfc`, `meituan`, `youdao_dictionary`,
  `state_kernel_sqlite`) evaluate server-side SQLite snapshots; `meituan`'s
  transaction-based evaluator reports `score` plus
  `required_transaction_sequence`, `missing_transaction_ids`,
  `missing_action_receipts`, `transaction_sequence_satisfied`.

A trajectory that produces no actual business-state change receiving `0.0` is
the expected outcome. A small number of tasks in a near-zero-progress state are
judged `env_valid=false` by strict evaluators
(`producer_schema_incompatible` / `invalid_state_kernel_expression_evidence`);
this is the strictness of the frozen evaluator, not a host failure.

## Bundled agent

`t2w-rollout` runs the `trace2world_env_text` policy: the observation is a
screenshot plus the environment's text feedback, and the output is one
`mobile_use` tool call. It only needs an OpenAI-compatible `/chat/completions`
endpoint (vLLM / SGLang / any gateway). The API key is read from the variable
named by `--api-key-env` (default `MODEL_API_KEY`; `OPENAI_API_KEY` is the
fallback when that variable is unset) and never written to disk.

```bash
t2w-rollout --server http://127.0.0.1:6800 --task-id <task_id> \
            --llm-base-url http://<model-host>:<port>/v1 --model <served-model-name> \
            --max-steps 50 --output trajectory.json
```

Options: `--max-steps` (default 50), `--temperature`, `--top-p`,
`--max-new-tokens`, `--messages-dir` (dump the per-turn conversation),
`--api-key-env`, `--timeout`.

The output follows `trace2world.direct_text_feedback_trajectory.v1`:

```jsonc
{
  "task_id": "...", "agent": "trace2world_env_text", "goal": "...",
  "steps": [{ "step": 1, "action": {...}, "model_response": "...",
              "environment_feedback": "...", "actionability_feedback": {...},
              "current_url": "..." }],
  "reward": 0.0,                 // scalar reward, uniform across all environments
  "evaluation": { ... }          // raw verdict payload of that environment's evaluator
}
```

## Environment manifest

`$RELEASE/environments_manifest.json` (`format = trace2world.environment_manifest.v1`),
per `environments[]` entry:

| Field | Meaning |
|---|---|
| `id` | Environment identifier (directory name and environment name in the API) |
| `display_name` / `domain` | Display name and domain from the paper's application roster |
| `paper_split` | The application's split in the paper (all are policy-training applications) |
| `path` | `environments/<id>` |
| `task_count` / `task_ids` | Number of tasks and list of task IDs |
| `runtime_profile` | `browser_checkpoint_events` or `state_kernel_sqlite` |
| `validation_status` | Package-level validation status (hosting accepts only `pass` by default) |
| `source_package` | Build-time package name (provenance) |
| `environment_manifest_sha256` / `tasks_sha256` | Hashes of the key files |

The top level carries `payload_files` / `payload_sha256` /
`environment_payload_sha256`, covering the SHA-256 of every file in the
release; [`scripts/verify_environments.py`](../scripts/verify_environments.py)
checks a downloaded copy against them.

## Preserved frozen identifiers

The following names are part of the frozen environment payload and of the
reward verdict; renaming them would change environment semantics, so they are
kept as-is in both the payload and this package:

- page-runtime contract keys `ghostweb.*` and DOM attributes `data-ghostweb-*`
- environment package format `ghostweb_agentforge_webenv_resource.v2`
- runtime event / evidence contracts `agentforge.webenv.*`
- the agent-visible environment feedback text (`ERROR[...]` etc.) and the
  system prompt template

Optional environment variables share the `T2W_` prefix: `T2W_APP_ALIASES`
(alias table for `open_app`), `T2W_TASK_INIT_SNAPSHOT_TIMEOUT_S` (timeout of
the state-kernel snapshot taken by `/task/init`).
