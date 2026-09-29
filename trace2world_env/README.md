# `trace2world_env` — environment host, evaluators, and bundled agent

The standalone runtime for the Trace2World environments: a Playwright-backed HTTP host that serves
the released environment portfolio (one resettable browser environment per app), the frozen
evaluators and SQLite state kernel that produce rewards, an async Python client, and the
text-feedback agent used in the paper.

The environments themselves are **not** in this package. Download them with
[`scripts/download_environments.py`](../scripts/download_environments.py) (Hugging Face dataset
[`LEONW24/Trace2World`](https://huggingface.co/datasets/LEONW24/Trace2World), `environments/`); the
resulting directory contains `environments_manifest.json` and `environments/<id>/` and is what
`--environments-dir` points at. Below, `$RELEASE` is that directory.

## Contents

```text
trace2world_env/
├── README.md
├── pyproject.toml            distribution trace2world-env; the t2w-serve / t2w-rollout / t2w-smoke CLIs
├── src/trace2world_env/
│   ├── catalog.py            load a release (or one environment): manifests, tasks, browser profile
│   ├── browser.py            Chromium lifecycle; one isolated browser context per episode
│   ├── page_runtime.py       scripts injected into and read from the environment pages
│   ├── actions/              action vocabulary, execution, actionability probes, feedback text
│   ├── scoring/              checkpoint evaluation, state-expression evidence, plan validation,
│   │                         SQLite state-kernel client
│   ├── episodes.py           episode lifecycle: start, act, observe, evaluate, close
│   ├── server.py             FastAPI routes and error mapping
│   ├── client.py             EnvironmentClient (async, httpx)
│   ├── agent/                bundled feedback agent: prompt, tool-call parser, images, chat client
│   └── cli/                  serve.py, rollout.py, smoke.py
└── tests/                    pytest suite; runs without a browser
```

Requirements: Linux x86-64, Python >= 3.12, Playwright Chromium (~0.7 GB), no GPU. Dependencies:
`fastapi`, `httpx`, `pillow`, `playwright`, `pydantic`, `uvicorn`. If a Chromium binary is already
present, skip `playwright install` and pass `t2w-serve --browser-executable <path>`.

```bash
pip install ./trace2world_env            # add [test] for pytest
python -m pytest trace2world_env/tests   # unit tests, no browser needed
```

## Command lines

| Command | Entry point | Purpose |
|---|---|---|
| `t2w-serve` | `trace2world_env.cli.serve:main` | Host all environments; the environment is selected by `task_id` |
| `t2w-rollout` | `trace2world_env.cli.rollout:main` | Run one task with the bundled agent and read back the reward |
| `t2w-smoke` | `trace2world_env.cli.smoke:main` | One short browser episode per environment as a self-check |

`t2w-serve` takes exactly one of `--environments-dir $RELEASE` (the whole portfolio) or
`--environment-dir $RELEASE/environments/<id>` (one environment, for debugging), plus
`--state-root` (writable directory for per-episode SQLite state; the payload itself is read-only),
`--host` / `--port` (default `0.0.0.0:6800`) and `--browser-executable`. Only environments whose
package validation status is `pass` are served.

> **The bind address is constrained.** Environment pages are always loaded from
> `http://localhost:<port>/app/...`, so `--host` must keep `localhost` reachable: use `127.0.0.1`
> for local use, or `0.0.0.0` when the API must be reachable remotely. Binding only a
> private-network IP makes `POST /episodes` fail in `Page.goto` with `ERR_CONNECTION_REFUSED`.

## Tasks

The tasks of the release are available in three places with identical content:

| Location | Use |
|---|---|
| `$RELEASE/environments_manifest.json` -> `environments[].task_ids` | Offline: which `task_id`s belong to which environment |
| `$RELEASE/environments/<id>/tasks.json` -> `tasks[]` | Offline: a task's `instruction`, `start_page`, `initial_state`, `runtime_profile` |
| `GET /tasks` | Online: every task loaded by the host, with its `environment` |

```bash
jq -r '.environments[] | "\(.id)\t\(.task_count)"' "$RELEASE/environments_manifest.json"
jq -r '.environments[] | select(.id=="kfc") | .task_ids[]' "$RELEASE/environments_manifest.json"
```

There is no manual environment selection: the host routes each `task_id` to the pages, evaluator
and state kernel of its environment.

**Initial state.** A task may declare a non-empty `initial_state`. `POST /episodes` injects it
verbatim into the page context of that episode (`sim.task_initial_state` and
`window.SimTaskInitialState`), where the environment's own runtime projects it into page content;
`state_kernel_sqlite` environments additionally seal it into the episode's initial SQLite snapshot.
Every episode gets its own browser context and its own state-kernel session (`sid`), so episodes
never share state.

## HTTP API

Errors are returned as HTTP 4xx/5xx with `{"error": {"code": ..., "message": ...}}`
(`unknown_task`, `unknown_episode`, `invalid_request`, ...).

| Method + path | Body | Response |
|---|---|---|
| `GET /health` | | `{ok, instance_id, environments, tasks, active_episodes}` |
| `GET /tasks` | | `{tasks: [{task_id, environment, instruction, start_page, initial_state, runtime_profile}]}` |
| `POST /episodes` | `{task_id}` | `{episode_id, task_id, environment, instruction}` |
| `GET /episodes/{id}/observation` | | `{screenshot (base64 PNG), url, page, viewport: {width, height}, device_scale_factor}` |
| `POST /episodes/{id}/actions` | `{action}` | `{accepted, feedback, done, result, url, page, viewport, device_scale_factor}` |
| `POST /episodes/{id}/evaluate` | `{final: true}` | `{reward, success, reason, details}` |
| `DELETE /episodes/{id}` | | `{closed: true}` |
| `GET /episodes/{id}/page`, `/runtime`, `/state?kind=audit` | | read-only inspection for auditing |

A rollout is: `POST /episodes`, then repeat `GET .../observation` + `POST .../actions` until `done`
or the step budget is exhausted, then `POST .../evaluate` and `DELETE`.

When the environment accepts an action, `accepted` is `true` and `feedback` is usually `null`; when
it rejects one, `accepted` is `false` and `feedback` says why: an `ERROR[...]` line for actionability
and format rejections (`NOT_INTERACTIVE`, `NO_EDITABLE_FOCUS`, `INVALID_ACTION`, `FORMAT_INVALID`),
or a plain sentence for `open_app`, a system back that would leave the app, and an undeclared
`ask_user`. Out-of-bounds, non-interactive or malformed actions consume one step, leave the business
state unchanged, and never reveal the correct target. `result` carries the full execution record
(actionability verdict, observed action, terminal submission) for analysis.

**Concurrency.** Episodes are independent (own browser context, own SQLite session); several can run
in parallel against one host.

From Python, `trace2world_env.EnvironmentClient` wraps these calls:

```python
from trace2world_env import EnvironmentClient

async with EnvironmentClient("http://127.0.0.1:6800") as env:
    episode = await env.start_episode("<task_id>")
    observation = await env.observe(episode.episode_id)          # .screenshot, .url, .viewport
    outcome = await env.act(episode.episode_id, {"action_type": "click", "x": 430, "y": 900})
    result = await env.evaluate(episode.episode_id)              # {"reward", "success", ...}
    await env.close_episode(episode.episode_id)
```

## Action space

`action` is a dictionary in **screenshot pixel coordinates** (`viewport x device_scale_factor`). All
environments render with a 430x884 CSS viewport at `device_scale_factor=2`, so screenshots are
860x1768; this is declared per environment in `resource_manifest.browser_viewport` and the host
default matches it.

| `action_type` | Parameters | Meaning |
|---|---|---|
| `click` / `double_tap` / `long_press` | `x`, `y` | tap / double tap / long press (500 ms) |
| `type` (alias `input_text`) | `text` | type text into the focused editable control |
| `swipe` / `scroll` | `start_x`,`start_y`,`end_x`,`end_y` or `direction` (optional `x`,`y`) | finger movement / content movement |
| `drag` | `start_x`,`start_y`,`end_x`,`end_y` | drag from start to end |
| `open_app` (alias `awake`) | `app_name` | open an application available to the episode |
| `navigate_home` / `navigate_back` / `keyboard_enter` | - | system buttons (`system_button` + `button` also accepted) |
| `wait` | `waiting_time` (<= 30 s) | wait |
| `answer` | `text` | submit a text answer and end the episode |
| `terminate` | `status=success/fail` | end the episode with the declared status |
| `ask_user` | `text` | only available when the page declares an external action host |

The bundled agent's model-facing `mobile_use` tool call uses a different notation (`click /
double_click / long_press / type / drag / swipe / scroll / wait / terminate` with coordinates normalised to
`[0, 999]`); `trace2world_env.agent` translates it into the pixel dictionary above. A policy that
maps normalised coordinates back to pixels should use the size of **each** observation as the
denominator rather than caching the first frame; the bundled agent does this.

## Reward semantics

`POST /episodes/{id}/evaluate` returns the final scalar `reward`, decided by the **frozen**
checkpoints and evidence contract inside the environment package, together with `success` (terminal
success), `reason`, and `details` (the environment evaluator's full verdict).

- Checkpoint-based environments (`browser_checkpoint_events`) report in `details` the
  `checkpoint_reward_vector` (per-checkpoint `reached` / `weight` / `reward_contribution`),
  `required_checkpoints`, `missing_required_checkpoints` and `env_valid`.
- State-kernel environments (`kfc`, `meituan`, `youdao_dictionary`; `state_kernel_sqlite`) evaluate
  server-side SQLite snapshots; `meituan`'s transaction-based evaluator reports
  `required_transaction_sequence`, `missing_transaction_ids`, `missing_action_receipts` and
  `transaction_sequence_satisfied`.

A trajectory that produces no business-state change receiving `0.0` is the expected outcome. A small
number of tasks in a near-zero-progress state are judged `env_valid=false` by strict evaluators
(`producer_schema_incompatible` / `invalid_state_kernel_expression_evidence`); this is the strictness
of the frozen evaluator, not a host failure.

## Bundled agent

`t2w-rollout` runs the feedback agent used in the paper: the input is a screenshot, or the
environment's text feedback after an action that produced some, and the output is one `mobile_use`
tool call. It only needs an OpenAI-compatible `/chat/completions` endpoint (vLLM / SGLang / any
gateway). The API key is read from the variable named by `--api-key-env` (default `MODEL_API_KEY`;
`OPENAI_API_KEY` is the fallback) and never written to disk.

```bash
t2w-rollout --server http://127.0.0.1:6800 --task-id <task_id> \
            --llm-base-url http://<model-host>:<port>/v1 --model <served-model-name> \
            --max-steps 50 --output trajectory.json
```

Options: `--max-steps` (default 50), `--temperature`, `--top-p`, `--max-new-tokens`,
`--messages-dir` (dump each model step's prompt), `--api-key-env`, `--timeout`. Screenshots are sent
at full resolution with the release pixel budget (`min_pixels=4096`, `max_pixels=2073600`) in every
request; the model server resizes them.

The output follows `trace2world.trajectory.v1`:

```jsonc
{
  "schema": "trace2world.trajectory.v1",
  "task_id": "...", "model": "...", "instruction": "...",
  "steps": [{ "step": 1, "action": {...}, "response": "...", "feedback": "...", "url": "..." }],
  "reward": 0.0,                 // scalar reward, uniform across all environments
  "evaluation": { ... }          // the evaluate response: reward, success, reason, details
}
```

## Environment manifest

`$RELEASE/environments_manifest.json` (`format = trace2world.environment_manifest.v1`), per
`environments[]` entry:

| Field | Meaning |
|---|---|
| `id` | Environment identifier (directory name and `environment` in the API) |
| `display_name` / `domain` | Display name and domain from the paper's application roster |
| `paper_split` | The application's split in the paper (all are policy-training applications) |
| `path` | `environments/<id>` |
| `task_count` / `task_ids` | Number of tasks and list of task IDs |
| `runtime_profile` | `browser_checkpoint_events` or `state_kernel_sqlite` |
| `validation_status` | Package-level validation status (the host serves only `pass`) |
| `source_package` | Build-time package name (provenance) |
| `environment_manifest_sha256` / `tasks_sha256` | Hashes of the key files |

The top level carries `payload_files` / `payload_sha256` / `environment_payload_sha256`, covering the
SHA-256 of every file in the release; [`scripts/verify_environments.py`](../scripts/verify_environments.py)
checks a downloaded copy against them.

## Frozen page contract

The environment pages talk to the host through names that are part of the frozen payload: the
`sim.*` storage keys, `window.Sim*` globals and `data-sim-*` DOM attributes of the page runtime, the
`trace2world.*` runtime-event and evidence contracts, the `trace2world_environment_resource.v2`
package format, the static routes `/app/<environment>/...`, and the state-kernel proxy `/api/v1/...`
addressed by the episode `sid`. The agent-visible feedback text (`ERROR[...]` lines) and the agent's
system prompt are likewise fixed; changing any of them changes environment or policy behavior.
