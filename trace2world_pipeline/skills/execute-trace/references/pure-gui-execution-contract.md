# Pure-GUI Execution Contract

Execution evidence is defined independently of review. A round shows what a user could do in the
compiled environment by following the recorded workflow, nothing more.

## Hard Boundary

The executing agent drives the environment through the same visible controls and state
transitions available to a person. One initial entry at the reset state is allowed: `POST
/episodes` on the environment host, or one navigation to the package's reset entry when driving
pages with Playwright. Every later state must follow from a preceding visible GUI operation or a
browser-native back action that the recorded workflow requires.

The packet must set:

```text
schema=trace2world.gui_execution.packet.v1
driver=host_api|playwright
pure_gui_execution=true
review_performed=false
sprites_generated=false
pairwise_alignment_claimed=false
intermediate_direct_navigation_used=false
action_id_locator_used=false
forced_or_dom_click_used=false
state_or_query_injection_used=false
inspection_endpoint_used_for_action_choice=false
```

Any shortcut flag set to `true` makes the round `execution_invalid`.

## Artifact Isolation

Allocate one new round per execution with an exclusive directory create:

```text
$RESULT/run_management/execution/<trace_id>/round_<NNNN>/
  capture_program.py          the program that drove this round
  raw_browser_log.json        raw action outcomes, page and console errors
  gui_execution_packet.json
  screenshots/
```

A concurrent executor receives a different round. Never reuse or clear an existing round, even
after a stop or failure. Every path in the packet is absolute and resolves inside the round;
`screenshot_output_dir` is the round's `screenshots/`, and every screenshot call receives an
explicit absolute path inside it. An escaped artifact invalidates the round. Relocate only
artifacts proven to belong to this round, update the packet, and validate again; never delete
files with a broad glob, because another executor may own them.

## Allowed Operations

- host API actions in the public action space: tap, double tap, long press, type into the focused
  editable control, swipe or scroll, drag, system back;
- with Playwright: `locator.click()` on an enabled, naturally hittable control; fill, type, and
  key operations on a visible input; natural wheel, touch, pointer, drag, and swipe;
- a visually verified coordinate on the correct owning surface when no semantic locator exists;
- read-only measurements and assertions for logging.

Locator priority with Playwright: visible role and name, exact visible text or label, visible
placeholder, title, or associated label, then a visually verified coordinate. DOM ids and classes
may be logged after the host is chosen visually; they never choose it.

## Forbidden Shortcuts

- direct navigation to an intermediate or target state;
- `data-action-id`, `data-element-id`, hidden bindings, contract-only selectors, or step indices
  choosing the host;
- forced click, DOM `click()`, `dispatchEvent`, runtime helpers, or evaluate-driven actions;
- URL/query, cookie, storage, IndexedDB, runtime, task-state, or business-phase injection;
- reading the host's inspection endpoints to choose an action;
- preloading a target query, selection, or value, or skipping an action to reach a later screen;
- rewriting a failed or unknown outcome as a success.

## First-Blocker Policy

An operational blocker is the earliest point where the next recorded operation cannot be
performed or cannot reach a stable, observable next state: a missing, covered, or disabled host;
an absent close or back control; an unscrollable owning surface; an input that rejects the
required value; a page error; broken navigation; a transition that leaves the next action
unreachable; or a `NOT_INTERACTIVE` response on the host the workflow requires.

A visual difference that does not prevent the next operation is not an execution blocker; it
belongs to review. A review-looking defect that makes the next action unreachable is a blocker.

At a blocker:

1. keep the full executed prefix;
2. capture the blocker state and the raw error or response;
3. set `status=stopped_at_first_blocker` and fill `first_blocker` (step, attempted action, host,
   observed outcome, screenshot);
4. do not load or inject the later state;
5. leave the unexecuted suffix absent instead of marking it passed or failed.

## Screenshot Sequence

Record chronological observed states, not pairs with source steps:

```json
{
  "ordinal": 0,
  "phase": "entry | before_action | after_action | blocker",
  "action_attempt_index": null,
  "screenshot_path": "...",
  "url": "...",
  "visible_state_summary": "...",
  "page_errors": []
}
```

The sequence may hold more or fewer screenshots than the source trace. Do not normalize counts,
duplicate images, or invent indices that imply one-to-one alignment.

## Freshness and Use

- A packet is bound to the package revision it executed. After any repair that intersects the
  trace, the round is invalidated and the trace restarts at step zero in a new round.
- `execution_complete` is required before a full-trace review can pass.
- `stopped_at_first_blocker` supports a review of the observed prefix and the blocker, never a
  full-trace pass.
- Screenshot count, a reward value, or sprite existence never overrides an execution blocker.
