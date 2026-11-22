# Repair Ladder

Rules for `repair-package`. The defect packet names the first known failure, not the scope of the
fix: a report on one row invalidates every earlier pass judgment for that trace, and the repair
must hold for the whole surface class it touches.

## 1. Classify before editing

Answer in the report's `diagnosis.rationale`:

- Which source evidence fixes the expected state: the marked pre-action screenshot of the step,
  the next screenshot (the effect), the obligation row, and the instruction?
- Does `C` support the behavior? If closure needs a new page identity, state or data relation,
  guard, or effect, stop: `not_repairable`, referral `semantics`.
- Is the package the owner? If the page already realizes the state and only a checkpoint,
  binding, evaluator, or terminal policy reads it wrongly, stop: the owner is `supervision`.
- Has the source a coherent action here? A wrong recorded action or an impossible transition
  between two source screenshots is not a package defect: referral `semantics`.

Record every value the defect involves in exactly one authority layer, then repair at the first
stale one:

| Layer | Meaning |
|---|---|
| canonical initial state | the app's default state from the specification |
| task launch state | the task's declared `initial_state`, injected by the host at reset |
| page entry initialization | what a page derives on entry from launch state and committed state |
| action transition | the state change a visible action commits |
| trace observation | what a screenshot showed; evidence only, never an authority |

A trace id, step index, action id, or file-name suffix never authors business state.

## 2. State model first

Restate the defect at the environment-model level before touching HTML or JavaScript:

- surface kind: route page, same-page section, overlay or sheet, sticky shell, carousel,
  virtualized list, or runtime projection;
- lifecycle: default state, current-step state, post-action state, forbidden future state, parent
  projection, selected and default values;
- continuity: continuous scroll or input, modal open and close, route navigation, or panel
  replacement;
- modalities that must agree: tap, small cumulative swipe or wheel, typed input, back;
- owning surface: the card, list, sheet, section, header, or footer that gives the host its
  meaning, not only the host.

Change the earliest stale consumer of that model and verify its lifecycle, continuity, and
modalities. Making the next row look right is not a repair.

## 3. Issue map

`diagnosis.issue_class` is one of these classes.

| Class | Repair and verify |
|---|---|
| `initialization` | defaults come from the canonical state or the task's launch state, or stay neutral; static DOM never preloads a future selection. Verify the first DOM after reset for two tasks and no cross-task leakage. |
| `state_handoff` | the visible source action commits state before navigation or same-page projection; the target reads that state. Verify the natural source flow without diagnostic context. |
| `parent_projection` | the parent shows only what earlier actions committed; no sibling or future value. |
| `shared_page` | a page shared by several tasks is parameterized by launch and committed state; changing a shared default must not preload one task's later state into another. Scan every task that uses the page. |
| `lifecycle` | future-step surfaces and sibling selections are absent before their revealing action and present after it. |
| `actionability` | the visible host on the owning surface carries the action, label, hit area, and effect; a hidden duplicate or a static element does not count. |
| `route` | the natural tap on the visible host routes to the next source page identity and keeps the required handoff; shared submit hosts resolve the current task's action, not a sibling default. |
| `overlay` | default closed, open, close or backdrop, reopen, and the target action after reopen; the panel stays inside the app shell in every state. |
| `visual_layout` | the same viewport shows comparable content, chrome, density, and fold position; measure the element chain before any CSS change. |
| `resource` | active media refs match the source-visible count, order, and identity before and after the action and on the consuming page. Resource order: matching extracted resource, audited clean source crop, provenance-backed semantic media, role-matching placeholder; never a full screenshot, a marked crop, or a known-wrong sibling entity. |
| `content` | every visible row, card, option, badge, and count matches the source-visible surface; add every missing item in one pass and check the same pattern on every sibling page. |

## 4. Carrier gate

Required for `initialization`, `state_handoff`, `parent_projection`, and `shared_page`. Record the
carrier chain:

```text
visible action -> writer -> state handle -> payload -> commit before navigation
-> consumer -> visible projection -> reset and dirty-sibling behavior
```

Classify every input the chain reads as `app_owned`, `task_setup`, `diagnostic_context`, or
`legacy_fallback`. Diagnostic context may locate a host but never choose a business value; remove
legacy fallbacks from the business path. Then run `scripts/audit_state_carriers.py` and these six
browser cases, all in the raw assertion report:

1. `empty_state_with_full_diagnostic_context`: diagnostic context alone leaves the neutral state;
2. `legacy_trace_key_only`: a trace-keyed storage entry is ignored;
3. `dirty_sibling_state`: stale state of another task, role, or query is ignored;
4. `valid_app_state_without_step_context`: real committed state projects without step or action
   context;
5. `natural_action_then_context_ablation`: the visible source action, then removal of step and
   action context, gives the same business result;
6. `parent_reload_or_reset`: reload or reset follows the product lifecycle and resurrects nothing.

## 5. Acceptance expansion

The reported symptom is the entry point. List every adjacent check in `acceptance_expansion`,
each `pass` or with a reason:

| Reported surface | Adjacent checks |
|---|---|
| overlay, panel, sheet, menu | default closed, open, close or backdrop, reopen, target action after reopen, containment open and closed |
| tab, section, carousel, scroll region | default position, forward and reverse movement, target reachable, real scroll container offsets, no sticky drift |
| picker, chip, checkbox, filter | default unselected, selection on the owning cell only, deselect or reset when visible, count or badge update, the next action uses the selection |
| search, typeahead, suggestions | default surface, natural typing, top-N rows in order with headers, target row position and reachability, committed projection after the tap |
| media | refs before and after the action, in preview or footer, on the consuming page; load count; no wrong-entity refs |
| submit, send, confirm | pre-submit validation, successful submit state, retry when visible, parent or terminal projection, the state write |

## 6. Required assertions

Run against the served package in a fresh browser context, with a `pageerror` listener, through
visible actions only. The raw report (`schema`, `trace_id`, `all_cases_pass`, `cases` with a
`status` per case, evidence paths) is written by the probe program and never edited by hand; its
SHA-256 goes into the repair report. Include, as the class requires:

- a negative assertion for the forbidden premature or future state;
- a positive assertion for the current and post-action state;
- the round trip for overlays;
- host and owning-surface actionability;
- a whole-viewport comparison against the source screenshot;
- real scroll-container offsets and natural cumulative gesture deltas for scroll fixes;
- input-dependent controls checked after natural typing;
- the same assertion on every other page or task that shares the changed surface.

## 7. Practical pitfalls

- A DOM `el.click()` or forced dispatch is not an action; if a natural tap fails, repair the host.
- An inline `onclick` that resets `className` overrides listener-managed classes.
- `history.back()` needs a history stack created by visible navigation.
- A duplicate element id elsewhere on the page can capture the selector or the tap.
- A static `div` has no handler; replace it with an interactive control.
- `scrollIntoView` scrolls every scrollable ancestor; set the dedicated container's offset.
- Mutate the DOM first, scroll second.
- Measure `clientWidth`, `scrollWidth`, and `getBoundingClientRect()` along the element chain
  before changing CSS.
- Counters, badges, and price ranges need real update handlers.
- A "select all" control must start in the state of its children.
- Map the complete show/hide flow of a panel across all steps, then write the state machine once.
- Read every source screenshot of a carousel before implementing direction; carousels bounce.
- When an earlier fix was reported incomplete, first state why its assertions missed the defect,
  and add the assertion that would have caught it.
