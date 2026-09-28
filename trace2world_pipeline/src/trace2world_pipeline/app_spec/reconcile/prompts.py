import json
from typing import Dict, Any, List, Optional

from trace2world_pipeline.app_spec.model import FrameObservation


class PromptsMixin:

    def _build_screenshot_context(self, screenshot_path: str = None) -> str:
        if not screenshot_path:
            return ""
        try:
            from PIL import Image
            with Image.open(screenshot_path) as img:
                w, h = img.size
            ratio = round(h / w, 2) if w > 0 else 1.0
            device = "mobile app" if ratio > 1.5 and w < 900 else "desktop website"
            return f"""## Screenshot Dimensions
This is a {device} screenshot: {w}×{h} pixels (aspect ratio h/w = {ratio}).
All layout dimensions MUST be expressed as PERCENTAGES of viewport dimensions, NOT absolute pixel values.
- Width values → percentage of viewport width (e.g., "100%", "70%", "25%")
- Height values → percentage of viewport height (e.g., "5%", "8%", "60%")
"""
        except Exception:
            return ""

    def _build_global_reasoning_prompt(self, payload: Dict[str, Any]) -> str:
        return f"""You are an expert in web trajectory analysis.

Goal:
1) Infer the app type from the traces (e.g., "food_delivery_app", "social_qa_content_platform_app", "e_commerce_app", "messaging_collaboration_app").
2) Assign app-relevant steps to General Pages: pre-instance-split canonical page clusters. The compatibility field is `page_cluster_id`; conceptually it is `general_page_id`.
3) Merge isomorphic states into the same General Page (do NOT split pages for input/filter/scroll state changes).
4) Build only high-level deterministic General Page-to-General Page routing edges.
5) Distinguish true navigation vs same-page state updates.

Input trajectories:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Output strict JSON:
{{
  "app_type": "a concise, lowercase, underscore-separated app type inferred from the traces. Examples: food_delivery_app, social_qa_content_platform_app, e_commerce_app, messaging_collaboration_app. Infer from page content, navigation structure, and user tasks.",
  "step_labels": [
    {{
      "trace_id": "trace id",
      "step_index": 0,
      "page_cluster_id": "General Page id in format c_<snake_case>, e.g. c_messages_inbox, c_meetings_home. Must match between step_labels and canonical_pages. This is the legacy compatibility key for general_page_id; do NOT use Materialized Page ids here.",
      "is_page_transition": true,
      "confidence": 0.0
    }}
  ],
  "skipped_steps": [
    {{
      "trace_id": "trace id",
      "step_index": 0,
      "skip_reason": "phone_home|system_launcher|non_target_app|loading_wait|transient_launch_gate|other"
    }}
  ],
  "canonical_pages": [
    {{
      "page_cluster_id": "General Page id in format c_<snake_case>, e.g. c_messages_inbox, c_meetings_home. Must match between step_labels and canonical_pages. This is the legacy compatibility key for general_page_id; do NOT use Materialized Page ids here.",
      "page_type_hint": "homepage|listing|detail|form|utility|search_entry|search_results",
      "page_name": "short page title as shown in app UI (max 5 words, like a real tab/header title, NOT a long description)",
      "description": "complete page description",
      "primary_functions": ["concrete user-facing function this page provides, grounded in trace evidence"],
      "route_path": "/hierarchical/route/path",
      "parent_route_path": "/hierarchical/route",
      "route_level": 1
    }}
  ],
  "canonical_routes": [
    {{
      "from_page_cluster_id": "cluster id in c_<snake_case> format, must match a page_cluster_id from canonical_pages",
      "to_page_cluster_id": "cluster id in c_<snake_case> format, must match a page_cluster_id from canonical_pages",
      "trigger": "action summary",
      "evidence_steps": ["trace_id:step_index"],
      "confidence": 0.0
    }}
  ],
  "task_related_elements": {{
    "<page_cluster_id>": [
      {{
        "element_name": "btn_<display_text>",
        "element_type": "icon_button|category_tab|list_item|action_button|nav_link|form_input|scroll_target",
        "display_text": "visible text on the element",
        "task_step_refs": [{{"trace_id": "trace id", "step": 0}}]
      }}
    ]
  }}
}}

Rules:
- Every input step must appear exactly once in either step_labels OR skipped_steps.
- Steps already listed in the input hard_skips are preprocessing-authoritative skips. Preserve them in skipped_steps with their original skip_reason; do not assign them to pages.
- Use skipped_steps for screenshots that should not contribute a generated app page, resources, task elements, or routing edges. Examples: phone launcher/home screen before opening the app, OS permission/settings screens, app switcher, unrelated app pages, pure loading/wait states without stable target-app UI, splash/startup interstitial ads whose only task action is close/skip (transient_launch_gate), or any state outside the target app requested by the trace task.
- Do NOT create canonical_pages entries for skipped_steps. Do NOT reference skipped_steps in canonical_routes or task_related_elements.
- The first trace step is often a phone home/launcher state before the app opens. If the screenshot description shows the device home screen rather than the target app, put it in skipped_steps instead of assigning it to an app homepage.
- External/System UI Exclusion: `external_ui_regions` are OS or non-target-app surfaces over/around the target app, such as verification SMS previews, OS notification banners, incoming call bars, permission sheets, app switcher surfaces, keyboard/IME chrome, or status-bar-only system UI. Ignore them for page identity, route decisions, task_related_elements, primary_functions, resources, and canonical page descriptions. Do not create a page or route for a local external overlay; only full-screen non-target/system states should be skipped.
- Interpret each trace step with the correct time semantics:
  - the screenshot description describes the CURRENT page state at that step;
  - thought/reasoning describes how the agent understands that CURRENT screenshot;
  - action describes the NEXT action the agent plans to take from that CURRENT screenshot, not the page the screenshot already belongs to.
- Evidence priority for clustering: CURRENT screenshot structure > description > thought > action.
- Page clustering should be driven by stable page identity, not by the next action.

Identity-first clustering procedure:
- First decide whether the CURRENT screenshot can still be explained as the previous page after an in-page state change (for example scroll, filter, query/value change, tab highlight change, or other same-shell feed/list state updates).
- Same-page explanation is the DEFAULT decision. Only if the CURRENT screenshot cannot be reasonably explained as an in-page state change should you consider starting a new cluster.
- Stable page identity should mean a clear standalone route-level page frame or top-level app-section surface, not just a denser feed, a different scroll depth, visible filter chips, or a merchant list that still lives inside the same shell/header/tab structure.
- If thought explicitly says the current screenshot is already on a new page (e.g. detail page / message page / inbox page / favorites page / personal center page), treat that as supporting evidence, not an automatic override of same-page state-change constraints.
- If a feed click lands on a screenshot that already shows detail-page identity (such as back button, title, author block, article/post body, or fixed engagement bar), that is strong evidence for a detail cluster, but still verify it is not contradicted by an explicit same-page transition signal.
- If the CURRENT screenshot already shows a different top-level app section (for example a personal center / profile / me page, or another bottom-tab destination), that screenshot MUST start a new cluster immediately.

State-change rules:
- If two steps are the same page with only query/filter/value differences, use same page_cluster_id.
- Do not move a step out of the previous cluster unless the CURRENT screenshot clearly shows a new standalone route-level page frame or a different top-level app section that cannot be explained as an in-page state change.
- A subsequent scroll on that new page remains in the same cluster.
- If an input step includes `transition_from_previous=false`, treat it as same-page evidence unless the CURRENT screenshot clearly shows a different top-level app section or a new standalone route-level page frame that cannot plausibly exist as an in-page state of the previous cluster.
- `transition_kind=same_page_scroll` is the only forced same-page transition constraint. It MUST NOT create a new General Page cluster or canonical page by itself, and it overrides generic listing/search-results heuristics and other "looks like a new page" heuristics by itself.
- `transition_kind=same_page_filter`, `same_page_other`, overlays, and `transition_from_previous=false` are state evidence to evaluate against the visible stable identity, not automatic cluster identity rules.
- For search/input steps, typing into a field can change the content directly below the input into autocomplete/suggestion rows. That input-driven suggestion surface should not create a new cluster by itself when the stable search scope and shell are unchanged.
- Distinguish `search_entry` from `search_results`: a focused search box, keyboard/IME, history chips, recommendation chips, autocomplete rows, or suggestion rows are search-entry runtime states. A search-results page requires explicit query commit evidence, such as tapping the search button/keyboard search, or a stable result-list page after the keyboard is dismissed. Do not label a search-entry/suggestion shell as `search_results` merely because query text is visible.
- Back+search headers, tab rows, filter chips, denser merchant/product feeds, search-like controls, or longer listing content are NOT sufficient override evidence by themselves. These cues can still belong to the same underlying route-level page.

Transient UI rules:
- **Global overlay/modal/drawer rule: If the input description OR thought says the change is an overlay/panel/drawer/modal/popover/bottom-sheet that "partially covers", "opens on top of", "appears on the right side", or otherwise indicates clicking “更多” only opened a same-page menu/overlay, do NOT assign it a standalone General Page cluster or canonical_pages entry. Keep it as the same page_cluster_id as the underlying General Page. The underlying General Page already shows this UI state in its description — the overlay is a transient UI component, not a page. Never give overlays a route_path like /more/overlay or /overlay/xxx.**
- Transient overlays, drawers, popovers, modals, and menus are not new pages when they appear on top of an unchanged underlying page identity.
- Do NOT rely on keyword heuristics such as “clicked a module name means navigation” or “clicked menu means same-page change”. Infer page assignment from the combined screenshot evidence, description, and thought.

Output quality rules:
- canonical_routes are pre-materialization General Page edges. Emit them only when the route is deterministic at the General Page level.
- If a General Page may later split into multiple Materialized Pages because of durable context, top-level mode, city/location, entity identity, selected record, checkout context, or query identity, do NOT emit canonical_routes that enter or leave that ambiguous General Page. The pipeline will rebuild those concrete routes after instance splitting from step-level evidence.
- Keep canonical_routes for stable high-level shell routes only, such as app hub → vertical home or bottom-nav destinations, when the source and target are not context/entity/query-dependent.
- Avoid noisy state-only edges unless necessary.
- Use complete page_name and description; do not truncate text.
- primary_functions: List the concrete, user-facing functions this page provides, grounded in what the traces actually show. Do NOT use generic labels like "Detailed content viewing" or "Content browsing". Instead, describe what the page ACTUALLY lets users do based on evidence in the traces (e.g., "Browse KTV venue packages and pricing", "Select booking time slot and room type", "View and filter restaurant menu items", "Submit order with coupon and payment method"). Each function should be specific enough that a developer can design an SDK interface from it. 2-5 functions per page.
- route_path should be hierarchical and stable for clustering/merging.
- Do NOT decide has_unique_page_structure here. The pipeline performs unique-structure labeling after instance splitting has produced Materialized Pages, using all Phase1a raw observations inside each Materialized Page.

Task element rules:
- task_related_elements: For each canonical page, list ALL interactive elements that any trace step targets (clicks, types into, selects). element_name format: "{{type_prefix}}_{{display_text}}" where type_prefix is btn/tab/item/link/input. element_name must be deterministic — same element across traces must have the same name.

Multi-vertical / super-app clustering:
Some apps bundle multiple business verticals (e.g., food delivery, beauty, entertainment, hotel) under one shell. Each vertical typically owns its own route prefix, visual theme, and component set in the real codebase. Apply these rules:
- Different business verticals MUST use separate clusters for their detail, booking, and sub-home pages. These pages have different tab structures, hero styles, CTAs, and domain-specific components, so they belong to different route prefixes and must generate separate HTML.
- Assign route_path per vertical rather than sharing a generic path across verticals.
- Pages that ARE genuinely shared across verticals should remain merged: the app-level hub/home page, generic utility pages (payment/checkout, cart, search), and listing pages that use the exact same template with only filter/query differences.
- Key test: if two pages require different component trees (different tabs, different content sections, different bottom-bar actions), they are separate clusters regardless of structural similarity.
"""

    def _build_global_reasoning_reduce_prompt(
        self,
        partial_results: List[Dict[str, Any]],
        full_payload: Dict[str, Any]
    ) -> str:
        compact_payload = {
            "trace_ids": [
                tr.get("trace_id", "")
                for tr in full_payload.get("traces", [])
                if isinstance(tr, dict)
            ]
        }
        return f"""You are merging partial page-clustering results into one global result.

Partial results:
{json.dumps(partial_results, ensure_ascii=False, indent=2)}

Full payload summary:
{json.dumps(compact_payload, ensure_ascii=False, indent=2)}

Return strict JSON with exactly this schema:
{{
  "app_type": "inferred app type (use the most common value from partial results, or merge them into one coherent descriptor)",
  "step_labels": [...],
  "skipped_steps": [...],
  "canonical_pages": [...],
  "canonical_routes": [...],
  "task_related_elements": {{...}}
}}

Requirements:
- Resolve cluster-id naming conflicts and produce one unified cluster space.
- Preserve every input step exactly once across step_labels or skipped_steps.
- Preserve skipped_steps for phone launcher/home, OS/system, non-target app, and pure loading/wait states; do not attach them to canonical_pages/routes/task elements.
- Keep routes canonical and remove redundant/noisy duplicates.
- Pages from different business verticals must remain in separate clusters with distinct route prefixes — do NOT merge them even if structurally similar.
- When merging canonical_pages, union primary_functions from all partial results for the same General Page. Keep them concrete and trace-grounded — do not replace with generic labels.
"""

    def _build_compact_global_reasoning_chunk_prompt(self, payload: Dict[str, Any]) -> str:
        return f"""You are analyzing compressed mobile-app trajectories.

The input is a compact Trace IR. Each segment contains one or more consecutive
steps that share a stable visual/action signature. The screenshot/state fields
describe the CURRENT page state before the next action is executed.

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Return strict JSON:
{{
  "app_type": "lowercase_underscore app type",
  "page_candidates": [
    {{
      "local_page_id": "p_<snake_case>",
      "page_kind": "homepage|listing|detail|form|utility|search_entry|search_results",
      "page_name": "short page title",
      "description": "stable page identity and role",
      "primary_functions": ["concrete functions grounded in the segments"],
      "route_path": "/stable/route/path",
      "include_as_page": true,
      "evidence_segments": ["trace_id:s_000"],
      "state_variants": ["optional same-page states or overlays"]
    }}
  ],
  "segment_assignments": [
    {{
      "segment_id": "trace_id:s_000",
      "local_page_id": "p_<snake_case>",
      "is_page_transition": true,
      "confidence": 0.0
    }}
  ],
  "state_overlays": [
    {{
      "segment_id": "trace_id:s_000",
      "underlying_local_page_id": "p_<snake_case>",
      "state_kind": "modal|panel|picker|drawer|popover",
      "surface_id": "s_<snake_case stable interaction-surface identity>",
      "opened_surface_identity": "short visible identity of this exact opened surface"
    }}
  ],
  "skip_segments": [
    {{"segment_id": "trace_id:s_000", "skip_reason": "phone_home|system_launcher|non_target_app|loading_wait|transient_launch_gate|other"}}
  ]
}}

Rules:
- Assign each non-hard-skipped input segment exactly once, either in
  segment_assignments or skip_segments.
- Before returning, count every input segment_id across both lists: it must
  appear once and only once. Never repeat a segment_id, including by putting it
  in both segment_assignments and skip_segments.
- Every segment_id in segment_assignments or skip_segments MUST exactly match one
  of the input segment ids, for example "trace_id:s_000". Never use a bare
  trace_id as a segment_id.
- Do not omit uncertain target-app UI segments. If a segment is inside the
  target app and contains executable or observable UI, assign it to the best page
  candidate with lower confidence rather than skipping it.
- skip_segments are only for hard skip/system/loading/non-target-app segments.
  Do not use skip_reason="other" for target-app executable UI.
- local_page_id values must use a p_ prefix. Do not use global c_ ids here.
- Do not split pages for query text, filters, scroll position, selected tabs,
  transient overlays, modals, panels, bottom sheets, or picker states.
- Search entry, history, autocomplete, and suggestion states are one page shell
  unless there is explicit submitted-query result evidence.
- Ignore `external_ui_regions` such as verification SMS previews, OS notification banners, incoming call bars, system permission sheets, app switcher surfaces, keyboard/IME chrome, and status-bar-only system UI. They must not define page candidates, state overlays, task elements, page functions, or routes.
- If a segment is an overlay/modal/panel on top of an unchanged page, assign it
  to the underlying page and include it in state_overlays. The segment's
  `segment_assignments.local_page_id` MUST equal its
  `state_overlays.underlying_local_page_id`; an overlay cannot simultaneously
  support a standalone page candidate.
- Give every opened interaction surface a stable `surface_id` and
  `opened_surface_identity`. Two surfaces in one workflow remain distinct when
  their visible controls, input model, lifecycle, or downstream action set
  differ. Workflow adjacency or shared intent is not merge evidence. Reuse one
  host-scoped `surface_id` across traces only for the same visible surface.
- `interaction_surface_hint` is non-ground-truth Phase-1 evidence. Reconcile it
  with the CURRENT segment description/visual structure; do not blindly copy a
  hint that contradicts the visible host/surface relationship.
- Page identity is based on the CURRENT segment visual structure and state
  summary, not the destination implied by the action.
- If `page_identity_constraints` contains `canonical_page_identity_group`
  entries, each `canonical_page_key` is a hard page-identity boundary. Segments
  from different canonical_page_key groups MUST NOT share the same local_page_id.
  Use separate page_candidates even when the visual shell or route_path looks
  similar. Query text, field values, selected entities, and same-page overlays
  may be state variants only inside the same canonical_page_key group.
- If a segment is listed under `same_page_surface_constraints`, do not create a
  standalone page for that surface. Attach it as a state_overlay or assignment
  to the page candidate that matches its canonical_page_key.
- `apk_route_priors` provides strong route-family and deeplink priors extracted from the app APK. Use them as stronger evidence than weak public web hints for namespace continuity, vertical boundaries, and page-family existence.
- Treat APK bundle/module names as strong page-family evidence, but do not assume they are exact route templates unless a direct deeplink or path string is present in `apk_route_priors`.
- If `apk_route_priors` shows a dedicated family such as waimai_address, waimai_invoice, waimai_cart, grocery, medical, taxi, or pinhaofan, prefer keeping that family separate from generic root/profile/list/search pages unless the screenshot clearly contradicts it.
- Keep page_candidates compact. They are candidates for later global merge, not
  final full step labels.
"""

    def _build_compact_global_reasoning_merge_prompt(self, payload: Dict[str, Any]) -> str:
        return f"""You are merging compact page candidates from trajectory chunks.

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Return strict JSON:
{{
  "app_type": "lowercase_underscore app type",
  "local_to_global": [
    {{
      "local_page_id": "chunk_00:p_local_page",
      "global_page_id": "c_global_page"
    }}
  ],
  "canonical_pages": [
    {{
      "page_cluster_id": "c_<snake_case>",
      "page_type_hint": "homepage|listing|detail|form|utility|search_results",
      "page_name": "short page title",
      "description": "complete page description",
      "primary_functions": ["concrete page functions"],
      "route_path": "/stable/route/path",
      "parent_route_path": "/stable/route",
      "route_level": 1
    }}
  ],
  "state_policy": [
    {{
      "state_kind": "modal|panel|picker|drawer|popover",
      "materialize_as_page": false,
      "underlying_global_page_id": "c_<snake_case>"
    }}
  ],
  "task_related_elements": {{}}
}}

Rules:
- local_to_global must include one mapping entry for every page candidate from every input chunk.
- Global page ids must use c_ prefix.
- Merge candidates only when they represent the same stable page identity.
- Keep different business verticals separate when they require different
  component trees or route prefixes.
- `apk_route_priors` provides aggregate APK-derived namespace, route-pattern, and page-family priors. Use them to avoid collapsing vertical families like waimai/grocery/medical/taxi/pinhaofan into generic root clusters.
- If APK priors only prove family existence but not an exact route template, preserve the family boundary without inventing a stronger canonical route path.
- Do not output step_labels or canonical_routes. The pipeline derives those
  deterministically from segment assignments and adjacent trace steps.
"""

    def _build_conditional_extraction_rules(self, action: str, thought: str) -> str:
        rules = []

        action_lower = action.lower() if action else ""
        thought_lower = thought.lower() if thought else ""


        if any(keyword in action_lower for keyword in ["first_visit", "goto", "navigate", "open"]):
            rules.append(
                "- **First Visit detected**: Must include COMPLETE `header` and `navigation` extraction. "
                "Capture full brand identity, all nav links, and page structure overview."
            )
        elif any(keyword in action_lower for keyword in ["scroll", "scroll_down", "scroll_up"]):
            rules.append(
                "- **Scroll action detected**: Focus on newly visible content entering the viewport. "
                "Mark previously extracted components as `completeness: \"already_extracted\"` if still visible. "
                "Pay special attention to elements at the top/bottom edge that may be partially visible."
            )
        elif any(keyword in action_lower for keyword in ["click", "select", "press", "tap"]):
            rules.append(
                "- **Click/interaction detected**: Focus on UI state changes — new modals, expanded sections, "
                "updated content areas, active states, and any error/success messages."
            )
        elif any(keyword in action_lower for keyword in ["type", "input", "fill", "enter"]):
            rules.append(
                "- **Input action detected**: Prioritize `forms` extraction. Capture current field values, "
                "validation states, autocomplete suggestions, and any dynamic feedback."
            )
        elif any(keyword in action_lower for keyword in ["search", "query"]):
            rules.append(
                "- **Search action detected**: Prioritize search results extraction. Capture result count, "
                "result items, filters applied, pagination state, and any 'no results' messaging."
            )


        if any(keyword in thought_lower for keyword in ["verify", "check", "validate", "confirm"]):
            rules.append(
                "- **Verification intent**: Add extra `validation_log` entries. "
                "Double-check form completeness, data accuracy, and element states."
            )
        if any(keyword in thought_lower for keyword in ["form", "input", "field", "submit"]):
            rules.append(
                "- **Form-focused thought**: Ensure `forms` section is exhaustive — "
                "capture every field label, type, value, placeholder, required status, and validation state."
            )
        if any(keyword in thought_lower for keyword in ["compare", "difference", "change"]):
            rules.append(
                "- **Comparison intent**: Note any visible differences from typical page state. "
                "Flag dynamic content, loading states, or conditional UI elements."
            )

        if not rules:
            rules.append(
                "- No specific conditional rules triggered. "
                "Perform standard full-page extraction following the scan sequence below."
            )

        return "\n".join(rules)

    def _build_first_pass_prompt(self, action: str, thought: str, page_context: Dict[str, str] = None, screenshot_path: str = None) -> str:

        conditional_rules = self._build_conditional_extraction_rules(action, thought)


        page_context_section = ""
        if page_context and any(page_context.get(k) for k in ("page_name", "page_type_hint", "description")):
            parts = []
            if page_context.get("page_cluster_id"):
                parts.append(f"- **Page ID**: {page_context['page_cluster_id']}")
            if page_context.get("page_name"):
                parts.append(f"- **Page Name**: {page_context['page_name']}")
            if page_context.get("page_type_hint"):
                parts.append(f"- **Page Type**: {page_context['page_type_hint']}")
            if page_context.get("description"):
                parts.append(f"- **Page Description**: {page_context['description']}")
            if page_context.get("route_path"):
                parts.append(f"- **Route Pattern**: {page_context['route_path']}")
            page_context_section = f"""
## Page Identity (from SM-P2 routing analysis)
This screenshot belongs to the following page type — use this context to guide extraction focus and entity schema:
{chr(10).join(parts)}

**Important**: Align your extraction with this page identity. For example:
- If this is a "Detail" page, prioritize extracting the primary entity's full data fields
- If this is a "Search Results" page, focus on result items and pagination
- Use the page type to choose appropriate entity names in `extracted_items`
"""

        return f"""You are a web page analysis expert. Extract structural and content information with precision.

{self._build_screenshot_context(screenshot_path)}## Context
- **Agent Action**: {action}
- **Agent Thought**: {thought}
{page_context_section}
## Context Integration
{conditional_rules}

## EXTRACTION PRINCIPLES
1. **Visible Only**: Extract ONLY what is visually rendered in the screenshot (not HTML source assumptions)
2. **Partial Handling**: Mark partially visible content with `"completeness": "partial"` and `"truncated_at": "top|bottom|left|right"`
3. **Null Over Omission**: Use `null` for missing required fields; omit optional fields only if truly irrelevant
4. **Text Fidelity**: Extract text verbatim; use `"[truncated]"` marker for cut-off paragraphs
5. **Confidence Marking**: For estimated/inferred values, append `" [estimated]"` to the value string
6. **Form Field Evidence**: For forms, extract every visible field label, required/optional marker, current value, and submit trigger. If the task/action mentions a field that is not visible in the screenshot, record explicit negative evidence in `validation_log` instead of inventing the field.

## EXTRACTION WORKFLOW

### PROMPT-S1: Boundary Definition
Scan the screenshot and classify each region:
- **Visible**: Fully rendered and readable
- **Partially Visible**: Content is cut off at viewport edge — mark with `"completeness": "partial"`
- **Confirmed Absent**: Explicitly not present on this page type
- **Unknown**: May exist outside the current viewport — mark with `"completeness": "unknown"`

### PROMPT-S2: Content Extraction (by priority)
Scan in this order: [1] Header/Nav → [2] Main Content (top-to-bottom) → [3] Sidebars → [4] Overlays/Modals → [5] Footer (if visible)

Priority levels:
1. **Critical**: Page core functionality content (e.g., primary entity details, search results, listing items)
2. **Structural**: Navigation, layout structure, forms, tabs
3. **Ancillary**: Advertisements, recommended content, footer links, newsletter forms

### PROMPT-S3: Cross-Validation
Before finalizing output, verify:
- `item_count` matches the actual length of corresponding arrays
- All empty form fields are marked as `""` or `null`, NOT invented values
- Any task-mentioned but currently invisible form field is logged as negative evidence, for example `Task mentions email, but no email field is visible in this screenshot`
- Text extraction: spot-check first and last sentences against screenshot
- All partially visible content is flagged with `completeness` field

## OUTPUT SCHEMA (JSON)
{{
  "confidence": 0.85,
  "page_type_hint": "homepage|listing|detail|form|utility|search_results",
  "description": "1-2 sentence description of page purpose and key features",
  "viewport": {{
    "coverage": "full|partial|scrollable",
    "visible_region": "header + main_content + partial_sidebar",
    "header_status": "visible|partial|not_in_viewport",
    "footer_status": "visible|partial|not_in_viewport",
    "scroll_position": "top|middle|bottom [estimated]"
  }},

  "brand_identity": {{
    "name": "exact visible site name",
    "tagline": null,
    "logo": {{
      "type": "text-based|icon-based|image-based",
      "position": "left|center",
      "visible": true
    }},
    "primary_color": "#hex [estimated]"
  }},

  "layout_structure": {{
    "structure": "single-column|two-column|sidebar-left|sidebar-right|three-column|grid",
    "responsive_breakpoint": "desktop|tablet|mobile [estimated]",
    "viewport_width_pct": "100%",
    "max_content_width_pct": "83% [estimated]",
    "has_sticky_header": "true|false|unknown",
    "header_visible": true,
    "footer_visible": false,
    "header": {{
      "height_pct": "5% [estimated]",
      "rows": 3,
      "sections": ["logo", "search", "nav", "user-menu", "browse-strip"]
    }},
    "main_content": {{
      "width_pct": "100%|70%|75%",
      "padding_pct": "3% [estimated]",
      "columns": 1
    }},
    "sidebar": {{
      "exists": true,
      "position": "left|right|none",
      "width_pct": "25% [estimated]"
    }},
    "section_count": 5
  }},

  "design_system": {{
    "color_scheme": {{
      "primary": "#hex [estimated]",
      "secondary": "#hex [estimated]",
      "background": "#hex",
      "text_primary": "#hex"
    }},
    "typography": {{
      "font_family": "observed or system-default [estimated]",
      "h1_size": "28px [estimated]",
      "body_size": "14px [estimated]"
    }},
    "spacing": {{
      "base_unit": "4px [estimated]",
      "section_gap": "24px [estimated]"
    }},
    "border_radius": "4px [estimated]",
    "icons": {{
      "commonly_used": ["icon-name [estimated]"]
    }}
  }},

  "components": [
    {{
      "component_id": "c_descriptive_name",
      "type": "header|nav|alpha-nav|hero-banner|search-form|feature-grid|card-list|advertisement|newsletter-form|carousel|breadcrumb|filter-bar|tabs|content|footer",
      "position": "header|banner|main|sidebar|footer",
      "position_order": 1,
      "layout": "flex-row|flex-column|grid|block",
      "completeness": "complete|partial|truncated|unknown",
      "truncated_at": null,
      "children": [],
      "items": ["item descriptions or extracted data"],
      "item_count": 3,
      "item_count_verified": true
    }}
  ],

  "navigation_elements": {{
    "main_nav": [
      {{ "text": "exact link text", "href_hint": "page.html" }}
    ],
    "footer_links": [
      {{ "text": "Help & Support|Advanced Search|etc", "href_hint": "..." }}
    ],
    "breadcrumb": ["Home", "Category", "Current"],
    "contextual": ["in-page anchors or tab navigation items"],
    "cta_buttons": [
      {{ "text": "Button Text", "type": "primary|secondary", "state": "enabled|disabled" }}
    ]
  }},

  "forms": {{
    "form_name": {{
      "fields": [
        {{
          "label": "Field Label",
          "type": "text|email|select|checkbox|radio|toggle",
          "value": "",
          "placeholder": "placeholder text if visible",
          "required": true,
          "visibility": "visible|partial",
          "evidence": "exact visible label/value/required marker"
        }}
      ],
      "absent_task_fields": [
        {{"label": "task-mentioned field not visible", "evidence": "why it is absent from the screenshot"}}
      ],
      "submit_button": {{
        "text": "Submit|Subscribe|Search",
        "state": "enabled|disabled|loading"
      }}
    }}
  }},

  "interactive_elements": {{
    "buttons": [
      {{ "text": "Subscribe", "state": "enabled|disabled|loading", "location": "sidebar" }}
    ],
    "links_summary": {{
      "total_visible": 25,
      "by_area": {{ "header": 10, "main": 12, "sidebar": 3 }}
    }},
    "inputs_focused": null
  }},

  "extracted_items": {{
    "_metadata": {{
      "active_tab": "active tab name if any"
    }},
    "EntityName": [
      {{
        "field1": "actual visible value (use empty string for empty fields)",
        "field2": "actual visible value"
      }}
    ]
  }},

  "identified_resources": {{
    "icons_used": [
      {{ "location": "where", "semantic_type": "search|menu|etc", "lucide_icon": "icon-name" }}
    ],
    "advertisement_placements": [
      {{ "location": "top-banner|sidebar|in-content", "size": "leaderboard|rectangle|etc" }}
    ]
  }},

  "validation_log": [
    "Verified: item_count=3 matches extracted array length for c_feature_grid",
    "Warning: Footer not in viewport - marked as unknown",
    "Verified: Newsletter form has 3 checkboxes + email input + submit button",
    "Note: Primary color #2563eb is estimated from button appearance"
  ]
}}

## VALIDATION CHECKPOINTS (verify before output)
- [ ] Item counts match array lengths in all components?
- [ ] All empty form inputs marked as `""` not omitted?
- [ ] Task-mentioned invisible fields recorded in `forms.*.absent_task_fields` / `validation_log`, not added to visible fields?
- [ ] Partial/truncated content flagged with `completeness` and `truncated_at`?
- [ ] Colors and sizes marked `[estimated]` if not from direct measurement?
- [ ] All validation results recorded in `validation_log`?
- [ ] Navigation includes ALL visible links (including utility links like Help & Support)?

Quality, completeness, and accuracy matter — be thorough and precise."""

    def _build_quality_assessment_prompt(self, extraction_result: Dict[str, Any]) -> str:

        extraction_summary = json.dumps(extraction_result, indent=2, ensure_ascii=False)

        return f"""You are a quality assessor for web page extraction. Compare the screenshot with the extraction result below and assess the extraction quality.

## Extraction Result to Assess:
```json
{extraction_summary}
```

## Your Task:
Look at the screenshot carefully and compare it with the extraction result above. Assess whether the extraction is COMPLETE and ACCURATE.

Return JSON:
{{
  "needs_refinement": false,
  "quality_score": 0.85,
  "assessment": {{
    "layout_complete": true,
    "layout_issues": "none or describe what's missing",
    "style_complete": true,
    "style_issues": "none or describe what's missing",
    "content_complete": true,
    "content_issues": "none or describe what's missing",
    "components_complete": true,
    "components_issues": "none or describe what's missing"
  }},
  "missing_elements": [
    "list of specific elements visible in screenshot but not in extraction"
  ],
  "refinement_focus": "layout|style|content|none"
}}

## Quality Assessment Criteria:

### needs_refinement should be TRUE only if:
1. **Major components missing**: Important visible sections (hero, sidebar, major card groups) not extracted at all
2. **Significant data loss**: More than 30% of visible data items not captured
3. **Layout fundamentally wrong**: Overall structure misidentified (e.g., two-column described as single-column)
4. **Critical navigation missing**: Main nav items or important CTAs not captured

### needs_refinement should be FALSE if:
- Minor details missing (exact pixel values, subtle color variations)
- A few navigation links missing but main ones captured
- Some data items missing but representative samples captured
- Minor component details incomplete but structure correct
- Confidence score >= 0.7 and no major structural errors

### Quality Score Guidelines:
- 0.9-1.0: Near perfect extraction, all visible elements captured
- 0.8-0.9: Good extraction, minor details missing
- 0.7-0.8: Acceptable extraction, some elements missing but structure correct
- 0.6-0.7: Partial extraction, needs improvement but usable
- Below 0.6: Poor extraction, needs refinement

**Default to needs_refinement: false** - Only trigger refinement for significant quality issues.
Most extractions (90%+) should NOT need refinement."""


    def _build_layout_extraction_prompt(self, action: str, thought: str, screenshot_path: str = None) -> str:
        return f"""You are a web layout analysis expert. Analyze this screenshot and extract PRECISE layout information.

{self._build_screenshot_context(screenshot_path)}Context:
- Agent action: {action}
- Agent thought: {thought}

Focus ONLY on layout structure, component positions, and spatial relationships.
All dimensions MUST be expressed as PERCENTAGES of viewport width/height, NOT absolute pixel values.

Return JSON:
{{
  "page_type_hint": "homepage|listing|detail|form|utility|search_results",
  "description": "page layout description",

  "layout_structure": {{
    "overall_layout": "single-column|two-column|sidebar-left|sidebar-right|three-column|grid",
    "viewport_width_pct": "100%",
    "max_content_width_pct": "83% [estimated]",
    "header": {{
      "height_pct": "5% [estimated]",
      "position": "fixed|static|sticky",
      "layout": "flex-row"
    }},
    "main_content": {{
      "width_pct": "100%|70%|75%",
      "padding_pct": "3% [estimated]",
      "columns": "1|2|3|4"
    }},
    "sidebar": {{
      "exists": true,
      "position": "left|right",
      "width_pct": "25% [estimated]"
    }},
    "footer": {{
      "height_pct": "5% [estimated]",
      "columns": "3|4|5"
    }},
    "grid_system": {{
      "columns": 12,
      "gutter_pct": "2% [estimated]",
      "margin": "auto"
    }}
  }},

  "components": [
    {{
      "component_id": "c_header",
      "type": "header|nav|hero-banner|search-form|feature-grid|card-list|sidebar|breadcrumb|filter-bar|tabs|footer|...",
      "area": "header|main|sidebar|footer",
      "position_order": 1,
      "bounds_pct": {{
        "x_pct": 0,
        "y_pct": 0,
        "width_pct": 100,
        "height_pct": 5
      }},
      "layout": "flex-row|flex-column|grid|block",
      "children_count": 5,
      "is_sticky": false,
      "z_index": "auto|10|100"
    }}
  ],

		  "section_flow": [
		    "header", "hero", "features", "content", "sidebar", "footer"
		  ],
		  "structure_signature": "stable structure signature string"
		}}"""

    def _build_style_extraction_prompt(self, action: str, thought: str) -> str:
        return f"""You are a CSS design system expert. Analyze this screenshot and extract the complete design system.

Context:
- Agent action: {action}
- Agent thought: {thought}

Focus ONLY on visual styling, colors, typography, spacing, and brand elements.

Return JSON:
{{
  "brand_identity": {{
    "site_name": "exact visible site name",
    "tagline": "visible tagline if any",
    "logo_type": "text-based|icon-based|image-based",
    "logo_position": "left|center",
    "primary_brand_color": "#hex"
  }},

  "design_system": {{
    "color_scheme": {{
      "primary": "#0066cc",
      "primary_dark": "#004d99",
      "primary_light": "#e6f2ff",
      "secondary": "#666666",
      "accent": "#28a745",
      "background": "#ffffff",
      "surface": "#f5f5f5",
      "text_primary": "#333333",
      "text_secondary": "#666666",
      "text_muted": "#999999",
      "border": "#e0e0e0",
      "success": "#28a745",
      "warning": "#ffc107",
      "danger": "#dc3545"
    }},
    "typography": {{
      "font_family": "system-ui, -apple-system, sans-serif",
      "heading_font": "Arial, sans-serif",
      "h1": {{ "size": "32px", "weight": 700, "line_height": 1.25 }},
      "h2": {{ "size": "24px", "weight": 600, "line_height": 1.33 }},
      "h3": {{ "size": "18px", "weight": 600, "line_height": 1.4 }},
      "body": {{ "size": "14px", "weight": 400, "line_height": 1.5 }},
      "caption": {{ "size": "12px", "weight": 400, "line_height": 1.33 }}
    }},
    "spacing": {{
      "unit": 4,
      "xs": "4px",
      "sm": "8px",
      "md": "16px",
      "lg": "24px",
      "xl": "32px",
      "xxl": "48px"
    }},
    "border_radius": {{
      "sm": "4px",
      "md": "8px",
      "lg": "12px",
      "pill": "9999px"
    }},
    "shadows": {{
      "none": "none",
      "card": "0 2px 4px rgba(0,0,0,0.1)",
      "hover": "0 4px 8px rgba(0,0,0,0.15)",
      "dropdown": "0 8px 16px rgba(0,0,0,0.2)",
      "modal": "0 16px 32px rgba(0,0,0,0.25)"
    }}
  }},

	  "identified_resources": {{
    "logo": {{
      "type": "text-based|icon-based|svg",
      "text": "Site Name",
      "svg_shape": "circle with cross|pill shape|...",
      "colors": ["#0066cc", "#ffffff"]
    }},
	    "icons_used": [
	      {{
	        "location": "search button|nav|feature card",
	        "semantic_type": "search|menu|user|cart|pill|heart|warning",
	        "lucide_icon": "search|menu|user|shopping-cart|pill|heart|alert-triangle"
	      }}
	    ]
		  }}
		}}"""

    def _build_content_extraction_prompt(self, action: str, thought: str) -> str:
        return f"""You are a content extraction specialist. Extract business data entities and page state from this screenshot.

Context:
- Agent action: {action}
- Agent thought: {thought}

Return JSON:
{{
  "page_type_hint": "homepage|listing|detail|form|utility|search_results",
  "description": "1-2 sentence description of what is visible on this screenshot",

  "navigation_elements": {{
    "main_nav": [
      {{ "text": "visible link text", "href_hint": "inferred-page.html" }}
    ],
    "breadcrumb": ["Home", "Category", "Item"],
    "footer_links": [
      {{ "section": "section name", "links": ["link text 1", "link text 2"] }}
    ]
  }},

  "extracted_items": {{
    "_metadata": {{
      "active_tab": "active tab name if any",
      "active_filters": ["active filter if any"]
    }},
    "EntityName": [
      {{
        "field1": "actual visible value",
        "field2": "actual visible value"
      }}
    ]
  }},

}}

## Rules for extracted_items
- Extract only business domain entities: real-world objects that would be stored in a database and need generated sample data (e.g., Product, Restaurant, Venue, Deal, Review, Category, Booking, Order).
- Ask yourself: "Would this entity have its own database table with meaningful content rows?" If yes, extract it. If no, skip it.
- Do NOT extract UI elements: search bars, tabs, buttons, headers, footers, navigation bars, badges, banners (as containers), filter chips, loading indicators, modals, or any other interface controls.
- Extract ALL visible attributes for each entity, including:
  - Per-item card fields (name, price, description, etc.)
  - Section/group header metadata that applies to items in that section (e.g., selection limits, quantity constraints, duplication rules)
  - Per-item restrictions and status indicators (e.g., purchase limits, sold-out status)
  If a section header shows constraints for a category (e.g., "可选2份，同一商品不可重复选"), those are attributes of the Category entity, not UI decoration."""

    def _build_state_only_description_prompt(self, action: str, thought: str, previous_action: str = "") -> str:
        return f"""Analyze this screenshot and produce a rich text description for General Page clustering.

Context:
- Previous action that produced this CURRENT screenshot: {previous_action}
- Agent action: {action}
- Agent thought: {thought}

Return strict JSON:
{{
  "description": "A rich, detailed paragraph describing this page: what major sections and components are visible (header, navigation, search bars, content areas, sidebars, footers), what the page's primary purpose is, what data or content is shown, and any notable UI state (active tabs, search queries, filters, scroll position). Be thorough — this description will be used for General Page clustering.",
  "transition_from_previous": true,
  "transition_kind": "navigation|same_page_scroll|same_page_filter|same_page_other|unknown",
  "transition_evidence": ["short evidence strings"],
  "interaction_surface_role": "none|opened_overlay|opened_drawer|opened_modal|opened_action_sheet|external_ui|unknown",
  "host_page_still_visible": true,
  "host_page_identity": "short stable host page identity or empty string",
  "opened_surface_identity": "short stable opened surface identity or empty string",
  "standalone_page_evidence": ["concrete standalone page cues"],
  "host_page_evidence": ["concrete host page cues still visible"],
  "header_pattern": "One short structural label for the current header identity, such as search_tabs_header, back_title_actions_header, avatar_stats_header, title_toolbar_header, or unknown.",
  "shell_section": "Current top-level app section if clearly visible, such as home, messages, me, or unknown.",
  "body_pattern": "One short structural label for the dominant body layout, such as feed_list, article_body, message_list, profile_dashboard, favorites_list, invite_list, or unknown.",
  "sticky_action_pattern": "One short label for the main persistent action area, such as bottom_tab_bar, detail_action_bar, none, or unknown.",
  "external_ui_regions": [
    {{
      "region_type": "os_notification_banner|incoming_call_bar|system_status_bar|keyboard_ime|permission_dialog|app_switcher|other",
      "location": "top|bottom|center|full_screen|unknown",
      "description": "brief description of system or non-target-app UI visible over/around the target app",
      "ignore_for_app_model": true
    }}
  ],
  "identity_confidence": 0.0
}}

Rules:
- Write a comprehensive text description (3-8 sentences). Cover layout, visible sections, content type, and page purpose.
- External/System UI Exclusion: if a verification SMS preview, OS notification banner, incoming call bar, Android/iOS system permission sheet, app switcher, keyboard/IME chrome, status-bar-only system UI, or other non-target-app surface is visible, list it in `external_ui_regions`. These regions must not become app components, page identity cues, task elements, routes, business data, or HTML features.
- Keep external/system UI out of `description`, `header_pattern`, `body_pattern`, `sticky_action_pattern`, and `shell_section` except for a brief note that it was ignored via `external_ui_regions`.
- Use previous_action only as transition context. It explains how the current screenshot was reached; it is not itself the current page identity.
- transition_from_previous means: did the CURRENT screenshot become a new standalone page compared with the previous step, or is it still the same underlying page after a state change?
- If previous_action is scroll/swipe, prefer `same_page_scroll` unless the screenshot clearly shows a new standalone page surface.
- If previous_action is a filter-chip/filter selection action, prefer `same_page_filter` unless the screenshot clearly shows a new standalone page surface.
- **Standalone page vs opened overlay**: `transition_kind=navigation` requires concrete standalone page evidence: the previous page's stable identity is no longer the main visible surface, a new independent header/title/back control replaces it, and the body content is fully replaced. If the previous page's stable identity (message list, feed, tab bar) remains visible as background or主体 and the new UI is a panel, drawer, bottom sheet, modal, action sheet, or shortcut entry surface that overlays part of the host, prefer `same_page_other` and set `interaction_surface_role=opened_overlay` with `host_page_still_visible=true`.
- Bottom tab highlight alone is not sufficient standalone navigation evidence. A highlighted bottom tab with the same underlying page content visible is `same_page_other`, not `navigation`.
- Do not infer navigation from denser feed content, merchant/product cards, filter chips, or back+search headers alone.
- transition_evidence should list short concrete reasons for the transition judgement.
- `interaction_surface_role`: use `opened_overlay` when a panel/drawer/bottom sheet/action sheet overlays the host page. Use `none` when no interaction-opened surface is visible. Use `external_ui` for system/OS surfaces already in `external_ui_regions`.
- `host_page_still_visible`: true when the previous/host page's stable identity (message list, feed, content area) is still visible behind or around the opened surface.
- `host_page_identity`: the stable name/identity of the host page if still visible (e.g., "消息会话列表", "首页feed"). Empty if fully replaced.
- `opened_surface_identity`: the stable identity of the opened interaction surface if any (e.g., "more_shortcut_panel", "action_sheet"). Empty if none.
- `standalone_page_evidence`: list concrete cues that prove this is a new standalone page (independent header, back button, full body replacement, URL/route change). Empty if not standalone.
- `host_page_evidence`: list concrete cues that the host page is still visible (background content, persistent tab bar, underlying message list).
- header_pattern, shell_section, body_pattern, and sticky_action_pattern must describe structural identity cues, not user intent or the next action.
- Use unknown when a cue is not clear from the current screenshot.
- identity_confidence should reflect how strongly these cues establish a stable page identity.
- Do NOT output structured fields like components, layout_structure, extracted_items, etc.
- Do NOT extract individual data entities or design tokens.
- Focus on describing WHAT you see, not measuring HOW it looks.
"""

    def _build_structure_extraction_prompt(self, page_context: Dict[str, str], num_screenshots: int, screenshot_path: str = None) -> str:
        return self._build_visual_layout_prompt(page_context, num_screenshots, screenshot_path)

    def _build_visual_layout_prompt(self, page_context: Dict[str, str], num_screenshots: int, screenshot_path: str = None) -> str:
        page_info_parts = []
        if page_context.get("page_cluster_id"):
            page_info_parts.append(f"- **Page ID**: {page_context['page_cluster_id']}")
        if page_context.get("page_name"):
            page_info_parts.append(f"- **Page Name**: {page_context['page_name']}")
        if page_context.get("page_type_hint"):
            page_info_parts.append(f"- **Page Type**: {page_context['page_type_hint']}")
        if page_context.get("description"):
            page_info_parts.append(f"- **Description**: {page_context['description']}")
        if page_context.get("route_path"):
            page_info_parts.append(f"- **Route**: {page_context['route_path']}")
        page_info_section = "\n".join(page_info_parts) if page_info_parts else "- No page context available"

        return f"""You are a web page structure analysis expert. You are given {num_screenshots} screenshot(s) from the SAME page (possibly at different scroll positions or minor state variations). Analyze them holistically to extract the page's spatial layout and components.

{self._build_screenshot_context(screenshot_path)}## Page Identity
{page_info_section}

## Your Task
Look at ALL provided screenshots together. They show the same page — combine information from all of them to build a complete structural picture of the spatial layout.

## OUTPUT SCHEMA (JSON)
{{
  "visual_layout_text": "A plain-text ASCII wireframe of the FULL page layout (top to bottom), showing how sections and components are spatially arranged. Use box-drawing characters or simple ASCII art. Example:\\n┌─────────────────────────────────┐\\n│ [Logo]  [Search...]  [Nav Links]│  ← header row 1\\n│ [Category A-Z] [ID Lookup] [Verify]  │  ← header row 2\\n├────────────────────┬────────────┤\\n│ Hero Banner        │            │\\n│ [Search Form]      │  Sidebar   │\\n├────────────────────┤  [Ad]      │\\n│ Feature Grid (3col)│  [Links]   │\\n│ [Card][Card][Card] │            │\\n├────────────────────┤            │\\n│ Item List (rows)   │            │\\n│ [Item 1]           │            │\\n│ [Item 2]           │            │\\n├────────────────────┴────────────┤\\n│ Footer [Links] [Links] [Links]  │\\n└─────────────────────────────────┘\\nMake it detailed enough to reconstruct the page layout. Include ALL sections from all screenshots (scrolled or not). Annotate each region with its component type and approximate content. IMPORTANT: preserve the original language of visible text labels in the wireframe — if buttons/tabs show Chinese/Korean/mixed text, write them as-is (e.g., [首页] [搜索] not [Home] [Search]).",

  "layout_structure": {{
    "structure": "single-column|two-column|sidebar-left|sidebar-right|three-column|grid",
    "responsive_breakpoint": "desktop|tablet|mobile [estimated]",
    "viewport_width_pct": "100%",
    "max_content_width_pct": "83% [estimated]",
    "has_sticky_header": true,
    "header_visible": true,
    "footer_visible": false,
    "header": {{
      "height_pct": "5% [estimated]",
      "rows": 2,
      "sections": ["logo", "search", "nav", "user-menu"]
    }},
    "main_content": {{
      "width_pct": "100%|70%|75%",
      "padding_pct": "3% [estimated]",
      "columns": 1
    }},
    "sidebar": {{
      "exists": true,
      "position": "left|right|none",
      "width_pct": "25% [estimated]"
    }},
    "section_count": 5
  }},

  "components": [
    {{
      "component_id": "c_descriptive_name",
      "type": "header|nav|alpha-nav|hero-banner|search-form|feature-grid|card-list|advertisement|newsletter-form|carousel|breadcrumb|filter-bar|tabs|content|footer",
      "position": "header|banner|main|sidebar|footer",
      "position_order": 1,
      "bounds_pct": {{
        "x_pct": 0,
        "y_pct": 0,
        "width_pct": 100,
        "height_pct": 5
      }},
      "layout": "flex-row|flex-column|grid|block",
      "completeness": "complete|partial",
      "is_transient": false,
      "children": [],
      "item_count": 3
    }}
  ],

  "component_relationships": [
    {{
      "source": "c_source_component_id",
      "target": "c_target_component_id",
      "relationship_type": "controls|feeds_into|groups_with|toggles|navigates_to"
    }}
  ]
}}

## Rules
- **visual_layout_text FIRST**: Generate the ASCII wireframe BEFORE filling in the structured fields. This forces you to think spatially about the full page layout. The wireframe should cover the entire page from top to bottom (combining all scroll positions).
- **bounds_pct is REQUIRED** for every component: estimate each component's bounding box as percentages of the VISIBLE VIEWPORT (not the full scrollable page). x_pct/y_pct = top-left corner position, width_pct/height_pct = component dimensions. For components below the fold, y_pct may exceed 100%. Two components side-by-side should have different x_pct values and their width_pct should sum to ~100%.
- Combine evidence from ALL screenshots to build the most complete picture.
- If a component is visible in one screenshot but not another (e.g., footer only visible when scrolled down), still include it.
- components should list ALL distinct UI components visible across all screenshots.
- **Preserve original language**: All text labels, button names, tab titles, and category names visible in screenshots must be written in their ORIGINAL language. Do NOT translate.
- **Mark transient components**: Set is_transient=true for overlays, popups, floating banners, weather alerts, promotional toasts, cookie notices, floating action buttons, bottom sheets, snackbar notifications, sticky action bars, sticky CTA bars, app download prompts, floating chat widgets, and other temporary UI elements that appear conditionally and are not part of the permanent page structure.
- **Component relationships**: After listing all components, identify semantic relationships between them. Only include relationships where one component meaningfully affects or interacts with another. Use the component_id values from the components list. Common patterns: filter/sort controls that affect a list (controls), search inputs that produce results (feeds_into), tab bars that switch content panels (toggles), sidebar navigation that targets content areas (navigates_to). Skip trivial containment relationships already captured by the children field. If no meaningful inter-component relationships exist, return an empty array.
"""

    def _build_visual_layout_text_only_prompt(self, page_context: Dict[str, str], num_screenshots: int, screenshot_path: str = None) -> str:
        page_info_parts = []
        if page_context.get("page_cluster_id"):
            page_info_parts.append(f"- **Page ID**: {page_context['page_cluster_id']}")
        if page_context.get("page_name"):
            page_info_parts.append(f"- **Page Name**: {page_context['page_name']}")
        if page_context.get("page_type_hint"):
            page_info_parts.append(f"- **Page Type**: {page_context['page_type_hint']}")
        if page_context.get("description"):
            page_info_parts.append(f"- **Description**: {page_context['description']}")
        if page_context.get("route_path"):
            page_info_parts.append(f"- **Route**: {page_context['route_path']}")
        page_info_section = "\n".join(page_info_parts) if page_info_parts else "- No page context available"

        return f"""You are a web page layout analyst. You are given {num_screenshots} screenshot(s) from the SAME page (possibly at different scroll positions or minor state variations). Create a detailed ASCII wireframe of the full page layout.

{self._build_screenshot_context(screenshot_path)}## Page Identity
{page_info_section}

## Your Task
Examine ALL provided screenshots together. They show the same page — combine information from all of them (including different scroll positions) to build a complete spatial map of the entire page from top to bottom.

## OUTPUT SCHEMA (JSON)
{{
  "visual_layout_text": "A plain-text ASCII wireframe of the FULL page layout (top to bottom), showing how sections and components are spatially arranged. Use box-drawing characters or simple ASCII art. Example:\\n┌─────────────────────────────────┐\\n│ [Logo]  [Search...]  [Nav Links]│  ← header\\n├────────────────────┬────────────┤\\n│ Hero Banner        │            │\\n│ [Search Form]      │  Sidebar   │\\n├────────────────────┤  [Ad]      │\\n│ Feature Grid (3col)│  [Links]   │\\n│ [Card][Card][Card] │            │\\n├────────────────────┴────────────┤\\n│ Footer [Links] [Links] [Links]  │\\n└─────────────────────────────────┘\\nMake it detailed enough to reconstruct the page layout. Include ALL sections from all screenshots (scrolled or not). Annotate each region with its component type and approximate content. IMPORTANT: preserve the original language of visible text labels — if buttons/tabs show Chinese/Korean/mixed text, write them as-is (e.g., [首页] [搜索] not [Home] [Search])."
}}

## Rules
- Focus EXCLUSIVELY on spatial layout — produce a detailed ASCII wireframe, nothing else.
- Combine evidence from ALL screenshots to build the most complete picture of the full page.
- If a section is visible in one screenshot but not another (e.g., footer only visible when scrolled down), include it at the correct position.
- Annotate each region with its component type (header, nav, hero-banner, card-list, footer, etc.) and approximate content.
- **Preserve original language**: All text labels, button names, tab titles visible in screenshots must be written in their ORIGINAL language. Do NOT translate.
- Mark overlays, popups, and floating elements with [OVERLAY] or [FLOATING] tags to distinguish from permanent structure.
"""

    def _build_components_layout_prompt(self, page_context: Dict[str, str], num_screenshots: int, screenshot_path: str = None) -> str:
        page_info_parts = []
        if page_context.get("page_cluster_id"):
            page_info_parts.append(f"- **Page ID**: {page_context['page_cluster_id']}")
        if page_context.get("page_name"):
            page_info_parts.append(f"- **Page Name**: {page_context['page_name']}")
        if page_context.get("page_type_hint"):
            page_info_parts.append(f"- **Page Type**: {page_context['page_type_hint']}")
        if page_context.get("description"):
            page_info_parts.append(f"- **Description**: {page_context['description']}")
        if page_context.get("route_path"):
            page_info_parts.append(f"- **Route**: {page_context['route_path']}")
        page_info_section = "\n".join(page_info_parts) if page_info_parts else "- No page context available"

        return f"""You are a web page component analysis expert. You are given {num_screenshots} screenshot(s) from the SAME page (possibly at different scroll positions or minor state variations). Analyze them to extract the page's structural components, layout structure, and component relationships.

{self._build_screenshot_context(screenshot_path)}## Page Identity
{page_info_section}

## Your Task
Before listing components, mentally scan ALL screenshots from top to bottom to build a spatial model of the page. Then extract every distinct UI component with its position, layout, and metadata.

## OUTPUT SCHEMA (JSON)
{{
  "layout_structure": {{
    "structure": "single-column|two-column|sidebar-left|sidebar-right|three-column|grid",
    "responsive_breakpoint": "desktop|tablet|mobile [estimated]",
    "viewport_width_pct": "100%",
    "max_content_width_pct": "83% [estimated]",
    "has_sticky_header": true,
    "header_visible": true,
    "footer_visible": false,
    "header": {{
      "height_pct": "5% [estimated]",
      "rows": 2,
      "sections": ["logo", "search", "nav", "user-menu"]
    }},
    "main_content": {{
      "width_pct": "100%|70%|75%",
      "padding_pct": "3% [estimated]",
      "columns": 1
    }},
    "sidebar": {{
      "exists": true,
      "position": "left|right|none",
      "width_pct": "25% [estimated]"
    }},
    "section_count": 5
  }},

  "components": [
    {{
      "component_id": "c_descriptive_name",
      "type": "header|nav|alpha-nav|hero-banner|search-form|feature-grid|card-list|advertisement|newsletter-form|carousel|breadcrumb|filter-bar|tabs|content|footer",
      "position": "header|banner|main|sidebar|footer",
      "position_order": 1,
      "bounds_pct": {{
        "x_pct": 0,
        "y_pct": 0,
        "width_pct": 100,
        "height_pct": 5
      }},
      "layout": "flex-row|flex-column|grid|block",
      "completeness": "complete|partial",
      "is_transient": false,
      "children": [],
      "item_count": 3
    }}
  ],

  "component_relationships": [
    {{
      "source": "c_source_component_id",
      "target": "c_target_component_id",
      "relationship_type": "controls|feeds_into|groups_with|toggles|navigates_to"
    }}
  ]
}}

## Rules
- **Scan before listing**: Mentally map the full page layout before outputting components. This ensures correct position_order and complete coverage.
- **bounds_pct is REQUIRED** for every component: estimate each component's bounding box as percentages of the VISIBLE VIEWPORT. x_pct/y_pct = top-left corner position, width_pct/height_pct = component dimensions. For components below the fold, y_pct may exceed 100%. Two components side-by-side should have different x_pct values and their width_pct should sum to ~100%.
- Combine evidence from ALL screenshots to build the most complete picture.
- If a component is visible in one screenshot but not another, still include it.
- **Preserve original language**: All text labels, button names, tab titles visible in screenshots must be written in their ORIGINAL language. Do NOT translate.
- **Mark transient components**: Set is_transient=true for overlays, popups, floating banners, weather alerts, promotional toasts, cookie notices, floating action buttons, bottom sheets, snackbar notifications, sticky action bars, sticky CTA bars, app download prompts, floating chat widgets, and other temporary UI elements.
- **Component relationships**: After listing all components, identify semantic relationships between them. Only include relationships where one component meaningfully affects or interacts with another. Common patterns: filter/sort controls that affect a list (controls), search inputs that produce results (feeds_into), tab bars that switch content panels (toggles), sidebar navigation that targets content areas (navigates_to). Skip trivial containment already captured by children. If no meaningful relationships exist, return an empty array.
"""

    def _build_layout_metadata_prompt(self, page_context: Dict[str, str], num_screenshots: int, screenshot_path: str = None) -> str:
        page_info_parts = []
        if page_context.get("page_cluster_id"):
            page_info_parts.append(f"- **Page ID**: {page_context['page_cluster_id']}")
        if page_context.get("page_name"):
            page_info_parts.append(f"- **Page Name**: {page_context['page_name']}")
        if page_context.get("page_type_hint"):
            page_info_parts.append(f"- **Page Type**: {page_context['page_type_hint']}")
        if page_context.get("description"):
            page_info_parts.append(f"- **Description**: {page_context['description']}")
        page_info_section = "\n".join(page_info_parts) if page_info_parts else "- No page context available"

        return f"""You are a web design analysis expert. You are given {num_screenshots} screenshot(s) from the SAME page. Extract design tokens, navigation elements, and brand identity.

{self._build_screenshot_context(screenshot_path)}## Page Identity
{page_info_section}

## Your Task
Analyze ALL provided screenshots to extract the page's design system, navigation structure, brand identity, and icon usage.

## OUTPUT SCHEMA (JSON)
{{
  "design_system": {{
    "color_scheme": {{
      "primary": "#hex [estimated]",
      "secondary": "#hex [estimated]",
      "background": "#hex",
      "text_primary": "#hex",
      "text_secondary": "#hex",
      "border": "#hex"
    }},
    "typography": {{
      "font_family": "observed or system-default [estimated]",
      "heading_font": "Arial, sans-serif [estimated]",
      "h1_size": "28px [estimated]",
      "h2_size": "20px [estimated]",
      "body_size": "14px [estimated]",
      "line_height": "1.5 [estimated]"
    }},
    "spacing": {{
      "base_unit": "4px [estimated]",
      "section_gap": "24px [estimated]",
      "card_padding": "16px [estimated]"
    }},
    "border_radius": {{
      "small": "4px [estimated]",
      "medium": "8px [estimated]"
    }},
    "icons": {{
      "library": "lucide [estimated]",
      "commonly_used": ["icon-name"]
    }}
  }},

  "navigation_elements": {{
    "main_nav": [
      {{ "text": "exact link text (preserve original language)", "href_hint": "page.html" }}
    ],
    "footer_links": [
      {{ "text": "link text (preserve original language)", "href_hint": "..." }}
    ],
    "breadcrumb": ["Home", "Category", "Current"],
    "contextual": ["in-page anchors or tab navigation items (preserve original language)"],
    "cta_buttons": [
      {{ "text": "Button Text (preserve original language)", "type": "primary|secondary" }}
    ]
  }},

  "brand_identity": {{
    "name": "exact visible site name",
    "tagline": "visible tagline if any",
    "logo": {{
      "type": "text-based|icon-based|image-based",
      "position": "left|center",
      "visible": true,
      "svg_description": "brief shape description if icon-based",
      "colors": ["#hex"]
    }},
    "primary_color": "#hex [estimated]"
  }},

  "identified_resources": {{
    "icons_used": [
      {{ "location": "where", "semantic_type": "search|menu|etc", "lucide_icon": "icon-name" }}
    ]
  }}
}}

## Rules
- Combine evidence from ALL screenshots to build the most complete picture.
- For design system values, use your best estimate and mark with [estimated].
- navigation_elements should capture ALL navigation links visible across screenshots.
- **Preserve original language**: All navigation text, button labels, and link text must use the ORIGINAL language visible in screenshots. Do NOT translate.
"""

    def _build_cluster_content_extraction_prompt(
        self,
        page_context: Dict[str, str],
        num_screenshots: int,
        structure_data: Dict[str, Any] = None,
        task_trace_context: Dict[str, Any] = None,
        lifecycle_context: Dict[str, Any] = None,
    ) -> str:
        page_info_parts = []
        if page_context.get("page_name"):
            page_info_parts.append(f"- **Page Name**: {page_context['page_name']}")
        if page_context.get("page_type_hint"):
            page_info_parts.append(f"- **Page Type**: {page_context['page_type_hint']}")
        if page_context.get("description"):
            page_info_parts.append(f"- **Description**: {page_context['description']}")
        page_info_section = "\n".join(page_info_parts) if page_info_parts else "- No page context available"


        structure_section = ""
        if structure_data and isinstance(structure_data, dict):
            parts = []


            vlt = str(structure_data.get("visual_layout_text", "")).strip()
            if vlt:
                if len(vlt) > 800:
                    vlt = vlt[:800] + "\n... [truncated]"
                parts.append(f"### Spatial Layout (ASCII wireframe)\n```\n{vlt}\n```")


            components = structure_data.get("components", [])
            if isinstance(components, list) and components:
                comp_lines = []
                for c in components:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("component_id", "")
                    ctype = c.get("type", "")
                    position = c.get("position", "")
                    item_count = c.get("item_count", "")
                    is_transient = c.get("is_transient", False)
                    if is_transient:
                        continue
                    entry = f"- `{cid}` ({ctype}) — position: {position}"
                    if item_count:
                        entry += f", items: {item_count}"
                    comp_lines.append(entry)
                if comp_lines:
                    parts.append("### Components Identified\n" + "\n".join(comp_lines))

            if parts:
                structure_section = "\n## Page Structure (from SM-P3a analysis)\nThe following structure was already extracted from this page's screenshots. Use it to guide your content extraction — focus on extracting business entities that correspond to these components.\n\n" + "\n\n".join(parts) + "\n"

        task_trace_section = ""
        if task_trace_context and isinstance(task_trace_context, dict):
            task_trace_section = (
                "\n## Page-Related Textual Trace Context\n"
                "This context is limited to traces and steps that touch the current Materialized Page. "
                "Use task_instruction and page/neighbor steps to decide which visible data is task-related.\n\n"
                "```json\n"
                + json.dumps(task_trace_context, ensure_ascii=False, indent=2)
                + "\n```\n"
            )

        lifecycle_section = ""
        if lifecycle_context and isinstance(lifecycle_context, dict):
            lifecycle_section = (
                "\n## Content Lifecycle Boundary\n"
                "This deterministic context identifies which observations are initial page state and which are "
                "runtime/post-commit state after the user created or updated data. Use it only to separate "
                "initial content from runtime state; still extract only values visible in the provided screenshots.\n\n"
                "```json\n"
                + json.dumps(lifecycle_context, ensure_ascii=False, indent=2)
                + "\n```\n"
            )

        return f"""You are a content extraction specialist. You are given {num_screenshots} screenshot(s) from the SAME Materialized Page.

## Page Identity
{page_info_section}
{structure_section}
{task_trace_section}
{lifecycle_section}
## Your Task
Extract business entities visible across all screenshots and mark whether each entity is task-related. Combine evidence from ALL screenshots and the page-related textual trace context into a single unified entity set with consistent naming. Do NOT analyze layout, design system, or component structure.

## External/System UI Exclusion
Ignore any OS or non-target-app regions visible in the screenshots, including verification SMS previews, OS notification banners, incoming call bars, system permission sheets, app switcher surfaces, keyboard/IME chrome, and status-bar-only system UI. Do NOT extract system overlay text, phone numbers, verification codes, caller names, notification app names, or OS controls as catalog entities, metadata, page descriptions, source components, or component mappings.

	## OUTPUT SCHEMA (JSON)
	Return the strict-compatible shape below. Do not use dynamic entity names as object keys; put entity names in the `entity` field.
	{{
	  "confidence": 0.0,
	  "page_type_hint": "short page type",
	  "description": "1-2 sentence description of what is visible across these screenshots",
	  "metadata": {{
	    "active_tab": "active tab name if any, else empty string",
	    "active_filters": ["active filter if any"]
	  }},
	  "extracted_item_groups": [
	    {{
	      "entity": "EntityName",
	      "items": [
	        {{
	          "item_ref": "short stable identifier such as a name/title",
	          "fields": [
	            {{"name": "field1", "value": "actual visible value"}},
	            {{"name": "field2", "value": "actual visible value"}}
	          ],
	          "evidence_steps": ["trace_id:step_index"],
	          "source_component": "c_component_id or empty string",
	          "reason": "why this item belongs to initial page content"
	        }}
	      ]
	    }}
	  ],
	  "runtime_item_groups": [
	    {{
	      "entity": "EntityName",
	      "items": [
	        {{
	          "item_ref": "short stable identifier",
	          "fields": [
	            {{"name": "field1", "value": "visible value that only appears after a user commit/update in this trace"}}
	          ],
	          "evidence_steps": ["trace_id:step_index"],
	          "source_component": "c_component_id or empty string",
	          "reason": "why this is runtime/post-commit content"
	        }}
	      ]
	    }}
	  ],
	  "excluded_from_initial": [
	    {{
	      "entity": "EntityName",
	      "item_ref": "short stable identifier",
	      "matched_hints": ["value from exclude_from_initial_value_hints"],
	      "reason": "why this item is runtime/post-commit rather than initial page content"
	    }}
	  ],
	  "item_lifecycle": [
	    {{
	      "entity": "EntityName",
	      "item_ref": "short stable identifier such as a name/title",
	      "lifecycle_phase": "initial|runtime_post_commit",
	      "evidence_steps": ["trace_id:step_index"],
	      "reason": "why this item belongs to the phase"
	    }}
	  ],
	  "entity_component_mapping": [
	    {{
	      "entity": "EntityName",
	      "source_component": "c_component_id",
	      "relationship": "items_in|detail_of|header_of"
	    }}
	  ],
	  "entity_task_relevance_entries": [
	    {{
	      "entity": "EntityName",
	      "task_related": true,
	      "role": "catalog_data|selectable_task_state|runtime_state|form_value|filter_condition|rule_or_policy|decorative",
	      "evidence_steps": ["trace_id:step_index"],
	      "task_instruction_refs": ["trace_id"],
	      "reason": "why this entity matters or does not matter for the task"
	    }}
	  ],
	  "validation_log": ["short notes about omissions or ambiguities"]
	}}

## Rules
- Extract both **catalog entities** and **task-related state entities** when they are visible or directly evidenced by this page's textual trace context.
- Catalog entities are objects the business authors pre-seed for browsing, such as venues, products, services, packages, locations, or articles.
- Task-related state entities are objects the user selects, modifies, validates, submits, or returns from while completing the task, such as payment methods, coupons/discounts, cart/order state, product options, delivery slots, fulfillment modes, review/filter/sort selections, form values, or pickup/destination places.
- Example task-state entity names include `PaymentMethod`, `CouponOrDiscount`, `DeliveryTimeSlot`, and `FilterSortState` when the page-local task context supports them.
- Do NOT skip runtime objects solely because they are created or modified by user actions. If the task directly manipulates them, extract them and mark `task_related=true`.
- Do not place post-commit or runtime-created records in `extracted_item_groups` when they match `exclude_from_initial_value_hints`; place them in `runtime_item_groups` and explain them in `excluded_from_initial` / `item_lifecycle`.
- `extracted_item_groups` is the initial page/content collection state. `runtime_item_groups` is task-created or task-updated state observed after save/submit/confirm actions.
- For visible data that is not task-related but is still business catalog data, extract it and mark `task_related=false`, `role="catalog_data"`.
- For decorative content, navigation chrome, layout-only UI controls, banners with no task evidence, or footer/header links, either omit them or mark `role="decorative"` with `task_related=false`.
- Name entities in **PascalCase** with no underscores (e.g., `BookingPackage`, `KtvVenue`, `RoomType`). Do not append generic UI suffixes like `Option`, `Item`, `Card`, or `Entry` unless they carry essential semantic meaning distinct from the base concept.
- Use the same entity name consistently across all screenshots — if the same concept appears in multiple screenshots, merge into one entity key with all observed instances.
- Use entity names that reflect the business semantics of this page, inferred from page context.
- For empty or not-yet-filled form fields, use empty string "".
- Only extract what is actually visible — do not infer off-screen content.
- Keep description focused on the combined visible content and state across all screenshots.
- **Extract all business-critical visible attributes for each entity** (up to the field count limit below), not just per-item card fields. In particular, look for **constraints and restrictions** at every level:
  - **Group-level constraints**: Section or category headers often display rules governing all items in that group — selection limits, quantity caps, duplication rules, minimum/maximum requirements. These are attributes of the parent/group entity, not UI decoration.
  - **Per-item constraints**: Individual items may show purchase limits, availability restrictions, eligibility conditions, or quantity bounds.
  - **Status indicators**: Availability states shown on items (sold out, limited stock, coming soon, disabled, etc.).
  Constraint values must match the screenshot exactly — copy the precise number or rule text as displayed. Do NOT paraphrase, round, or omit any constraint.
- **Use generic site-level entity names**: Use generic names that strip page-specific or venue-specific prefixes (e.g., `Venue` not `KaraokeVenue`). However, keep entity names semantically distinct when items have fundamentally different field structures — a food menu item, a service package, and a group-buy deal are different entities even if they all appear on merchant pages.
- **Minimum entity complexity**: Do not create entities with only 1 field. Every entity must have at least 2 meaningful fields that together describe a real business object.
- **Cross-page entity consistency**: If the same business concept appears across multiple screenshots (e.g., a venue, a product, a service package), use the exact same entity name for all occurrences. Do not create page-specific variants.
- **Maximum entity fields**: Each entity should have at most 20 fields. Focus on essential business attributes — things that differentiate items (name, price, rating, status, category). Exclude UI presentation details (colors, font sizes, badge positions), layout descriptions, and navigation metadata. If more than 20 candidates, keep the most business-critical ones.
- **Reference parent components**: When structure data is provided above, map each extracted entity to its source component using `entity_component_mapping`. Use the component_id from the structure data. The `relationship` field describes how the entity relates to the component: "items_in" for list/grid items, "detail_of" for detail page content, "header_of" for header/banner content.
- **Component-guided extraction**: Use the component list to ensure completeness — if a non-transient component has item_count > 0 but you haven't extracted any entity from it, reconsider whether it contains business data worth extracting.
- **Task-relevance evidence**: When `task_related=true`, include concrete `evidence_steps` and `task_instruction_refs`. Use page-local steps and neighbor steps from the Page-Related Textual Trace Context. Do not claim task relevance without evidence.
"""

    def _build_instance_split_prompt(
        self,
        page_context: Dict[str, str],
        steps_payload: List[Dict[str, Any]],
        boundary_preservation_constraint: Optional[Dict[str, Any]] = None,
    ) -> str:
        page_info_parts = []
        if page_context.get("page_name"):
            page_info_parts.append(f"- **Page Name**: {page_context['page_name']}")
        if page_context.get("page_type_hint"):
            page_info_parts.append(f"- **Page Type**: {page_context['page_type_hint']}")
        if page_context.get("description"):
            page_info_parts.append(f"- **Description**: {page_context['description']}")
        page_info_section = "\n".join(page_info_parts) if page_info_parts else "- No page context available"

        steps_json = json.dumps(steps_payload, ensure_ascii=False, indent=2)
        eligible_entry_obs_keys = self._instance_split_entry_action_obs_keys(steps_payload)
        eligible_entry_keys_json = json.dumps(
            eligible_entry_obs_keys,
            ensure_ascii=False,
        )
        empty_entry_contract = (
            "The authoritative eligible list is EMPTY for this call. Return "
            "entry_action_audit with all three arrays empty (or omit the entire audit "
            "only when the schema branch permits it). Do not create any "
            "fixed_capacity_task_content instance: continuation actions are lossless "
            "membership evidence, not trace-entry task-content rows."
            if not eligible_entry_obs_keys
            else "The authoritative eligible list is non-empty; copy and partition it exactly."
        )
        boundary_constraint_section = ""
        if isinstance(boundary_preservation_constraint, dict) and boundary_preservation_constraint:
            boundary_constraint_section = f"""

## Bound Positive Human-Reviewed Boundary Constraint
This constraint is digest-backed authority from the after-instance visual audit:
{json.dumps(boundary_preservation_constraint, ensure_ascii=False, indent=2)}

Apply it only to the explicitly listed `must_separate_obs_key_groups`. Those
anchored observations were reviewed against their original screenshots, so the
constraint overrides a conflicting `transition_kind` hint for those anchors only.
It does not weaken the ordinary same-page/scroll rule for any unlisted
observation. Assign every anchor key verbatim, keep each listed group in one
explicit instance, keep different listed groups in distinct instances, and
preserve every `required_split_dimensions` value in the corrected candidate.
"""

        return f"""You are an instance boundary detector for web/app page analysis.

## Page Identity
{page_info_section}

## Steps (all from the SAME General Page)
Each step includes its trace's `task_instruction` to help you understand the user's goal when this step was recorded.
{steps_json}

## Machine-Computed Entry-Action Audit Denominator
The following list is computed from the input rows and is authoritative for this
call. Copy every key exactly once into `entry_action_audit.audited_obs_keys`, then
partition it exactly once between `task_content_obs_keys` and
`stable_shell_control_obs_keys`:
`eligible_entry_obs_keys = {eligible_entry_keys_json}`

{empty_entry_contract}
{boundary_constraint_section}

An observation is eligible only when it satisfies the existing trace-entry rule
(`transition_from_previous=false` or an empty `action_context.previous_action`)
**and** has a non-empty `action_context.current_action`. A loading, waiting, or
otherwise no-current-action frame remains part of lossless instance membership,
but is not an action-audit row and must not be assigned a guessed role.

## Your Task
These steps all belong to the same page type (e.g., "merchant detail page", "chat conversation page").
Determine whether they show the **same content instance** or **different content instances**.

**Same instance**: The steps show the same underlying entity with only state changes
(scroll, filter, overlay, form filling). Examples:
- Same merchant page scrolled to different sections
- Same chat conversation with new messages
- Same note with editing state changes

**Different instance**: The steps show fundamentally different entities of the same page type.
Examples:
- Merchant A detail page vs Merchant B detail page
- Chat with Person A vs Chat with Person B
- Note "Shopping List" vs Note "Meeting Minutes"
- Search surfaces for different stable visible scopes or underlying entities, such as
  a global app search shell vs an in-store product search shell. Query text alone is
  not a stable entity.

## Decision Rules
1. If `transition_kind` is "same_page_scroll" AND
   `transition_from_previous=true`, keep SAME instance. This is the only hard
   SAME-instance rule: an actual scroll, pull refresh, or ordinary swipe continuation can
   only reveal content under the current Materialized Page. Do not apply this hard rule to
   a trace-entry observation with `transition_from_previous=false` (or no real previous
   action): an entry frame can be mislabeled `same_page_scroll` by frame analysis even
   though no in-task scroll produced it. Evaluate that CURRENT visible entry state with the
   position-bound task-consumption rule below.
   `same_page_filter`, `same_page_other`, `overlay`, and `transition_from_previous=false`
   are state evidence to evaluate with the visible stable context, not automatic
   Materialized Page identity rules and not hard blockers to an otherwise valid split.
2. If `transition_kind` is "navigation" AND the description describes a clearly different
   core entity (different merchant name, different chat partner, different note title),
   prefer DIFFERENT instance.
3. Use `task_instruction` only as weak context for interpreting visible evidence.
   It must NOT be used as instance identity by itself. Different future goals,
   downstream action chains, or intended outcomes (for example save draft vs publish,
   add topic vs submit, share later vs edit later) do not define different
   Materialized Page instances unless the CURRENT screenshot already shows a
   different durable entity, committed context, stable mode, or
   lifecycle/state variant.
4. If steps are contiguous or near-contiguous within the same trace and the surrounding
   route, layout, action flow, and stable visual identity indicate the same visible entity,
   treat minor OCR/name spelling variants as SAME instance unless there is clear evidence
   of an entity switch. Do not create a new instance solely from one inconsistent character
   or noisy entity name in otherwise stable context.
5. For search-entry and search-suggestion clusters, split by stable search scope, not by
   transient input state. Within the same stable search scope, different input text,
   autocomplete rows, history chips, recommendation chips, or suggestion rows should be ONE
   instance; generated JavaScript can update suggestions/results-preview based on the search
   input value.
   When typing into a search/input field changes autocomplete/suggestion rows directly below
   that input, treat those rows as input-state suggestions unless the stable visible scope
   or underlying entity changes.
   However, if a visible scope anchor changes how the query is interpreted or where results
   and actions apply, that anchor is not input state and should define a DIFFERENT instance.
   Use this rule generally: do not rely on app-specific names or concrete query examples.
6. For search-results clusters, distinguish query shell state from task-critical
   committed result identity. If the keyboard is still open and the visible rows are
   suggestions, autocomplete, history, or recommendation chips, keep SAME instance and
   model the query as runtime state. If the page shows a committed submitted-query
   result collection and the task will choose, compare, filter, or navigate from a
   selected result/entity in that collection, preserve that task-critical committed result
   set as a DIFFERENT Materialized Page when the visible result records, fixed-capacity
   result slots, selected result, address/context, fulfillment mode, or stable result
   universe differs. Use `split_dimension=submitted_query` and include
   `state_params.submitted_query` evidence so downstream materialized bindings can keep
   the result identity explicit. A query value alone is not enough; the current screenshot
   must expose a committed result collection or selected result whose content is necessary
   for replay.
7. Component-layout capacity rule: if multiple observations show the same fixed-capacity
   component slot group (for example a titled section with exactly four visible product
   cards), do NOT merge all item sets into one enlarged content set just because the page
   type is the same.
8. Appendable-content rule: if content appears in an open-ended feed/list/search-results
   region where appending additional items preserves the observed component layout, keep
   SAME instance.
9. Position-bound task-consumption exception: the appendable-content rule does NOT merge
   observations when the CURRENT action directly consumes a specific visible entity at a
   bounded viewport position or ordinal slot (for example the first visible card, the
   second visible result, or a named item that must already be present at entry) and other
   observations require a different entity in that same position/slot. The action must be
   supported by `action_context.current_action`, the current screenshot description, and
   the task instruction; a future goal alone is insufficient. Split those observations as
   DIFFERENT Materialized Pages with
   `split_dimension=fixed_capacity_task_content`, because merely appending every entity to
   one feed cannot preserve which entity occupies the task-consumed source slot. Keep
   observations whose current action uses only stable shell/navigation/search controls in
   the shared representative instance; do not split them merely because background feed
   cards differ. Before emitting any `fixed_capacity_task_content` group, audit EVERY
   machine-eligible member observation independently. Non-entry continuation or
   no-current-action rows must still be assigned completely to the instance, but
   do not receive a guessed action role:
   - a member whose `action_context.current_action` targets a stable tab, navigation item,
     search control, menu, filter, category tab, action-sheet button, or other ordinary
     non-task control MUST NOT be placed in the task-content
     group, even if its task instruction mentions content that will be consumed later;
   - members may share one task-content instance only when the CURRENT visible target
     entity and its bounded ordinal/viewport slot are compatible;
   - different named entities in the same slot, or different required slots whose card
     ordering cannot coexist in one entry viewport, require separate instances.
   For every `fixed_capacity_task_content` instance, include
   `split_reason.member_action_audit` with one row per machine-eligible member
   `obs_key`, containing the exact
   `current_action_target`, `target_role` (`task_content` or `stable_shell_control`),
   `bounded_slot`, and `visible_entity`. A group is invalid if this audit is missing, if it
   omits an eligible member, contains a non-eligible extra row, or if any eligible member
   has `target_role=stable_shell_control`.
10. Fixed-layout representative rule: if observations differ only by task-unrelated content
   inside the same fixed-capacity component layout, keep SAME instance and let downstream
   rendering choose one coherent representative content set.
11. Fixed-layout task-related split rule: if observations differ by task-related content
   inside the same fixed-capacity component layout, split into DIFFERENT instances so each
   generated page preserves one coherent content set.
12. Do NOT split instances solely because of minor UI state differences (section tabs,
   filter chips, selected-state highlights, loading states).
   When `semantic_identity_evidence.state_authority` identifies the observations as
   values of a structured `state_scope` with initial/read and mutated/update lifecycle
   evidence, keep one Materialized Page and parameterize that component state. The state
   authority is stronger than a filename-like label or a generic semantic-mode name.
   Detail pages are an exception to the "same template" intuition: the primary detail
   entity is durable instance identity. If two observations of the same detail route show
   different primary entity titles, product names, merchant names, medication names, order
   records, invoice records, or address records, split them into DIFFERENT instances unless
   one is clearly the same entity after an edit/quantity/state change. Same layout, same
   CTA bar, same price area, or same cart state is not enough to merge different primary
   detail entities.
13. Instance identity must be based on the ACTIVE/SELECTED state, not on all visible
   text. Tab bars, navigation headers, and category rows often display multiple labels,
   but only the CURRENTLY SELECTED tab defines the instance. Unselected tab labels that
   merely appear in the UI are NOT instance identity. Two observations showing the same
   selected tab but different unselected tab labels are SAME instance.
14. top-level semantic mode tabs are not minor UI state. A visible top-level mode switch
   should define a DIFFERENT Materialized Page when the active selection changes the
   primary content universe, feed/list source, fulfillment mode, serviceability,
   checkout path, or card template while the surrounding route-level shell remains the
   same. Examples include delivery vs self-pickup, home vs self-pickup, dine-in vs
   takeout, or standard vs express fulfillment modes. A root/home shell with one
   observation showing the delivery/home tab active and another showing the self-pickup tab active
   is a fulfillment-mode split, not a section-tab/filter-chip change, when
   the visible merchant/feed/card universe changes with that active mode. Keep the same
   General Page, but split Materialized Pages by stable mode identity even when the
   transition is recorded as `same_page_other` or `transition_from_previous=false`.
   same_page_scroll remains hard SAME within the currently selected mode when
   `transition_from_previous=true` and should be assigned to that mode's instance, except
   for exact obs_key groups in a bound positive human-reviewed boundary constraint whose
   original screenshots explicitly disprove that transition hint. This exception applies
   only to the listed anchors and must not generalize to other scroll rows. Generic
   `semantic_mode`, `state_variant`, or `lifecycle_variant` are not enough to preserve
   a split made only from same-page controls such as filters, search focus, selected-state
   highlights, opened/closed overlays, or scroll-revealed list/feed positions. When
   structured state authority exists, these dimensions are runtime projections of the
   current Materialized Page and will be deterministically demoted. For a durable mode split, use a
   specific durable dimension such as `fulfillment_mode`, `top_level_mode`,
   `primary_mode`, `service_mode`, or `stable_context_mode`, backed by visible evidence
   that the primary content universe, action set, serviceability, checkout path, or card
   template changes. A selector value, translation direction, filter value, selected
   component state, or state-scope mutation is not such evidence; parameterize it inside
   the current Materialized Page. Otherwise keep the state inside the current
   Materialized Page instance.
15. Committed durable context state can define a different Materialized Page when it
   changes the stable context identity, such as selecting a city, address, account, or
   submitted top-level context. Filter chips, tabs, sort controls, query refinements,
   and other same-page controls are weak state signals: prefer SAME when the visible
   shell and stable scope remain unchanged, but do not use them as hard blockers to a
   split when they commit a stable context, scope, or entity change. A same_page_scroll
   transition, pull refresh, or ordinary swipe must not be the split trigger; it can
   only reveal content under the current Materialized Page.
16. Instance identity must be based on CURRENT visible state, not on future
   downstream actions. Two observations of the same page with the same visible
   content, layout, and state must NOT be split into different instances just
   because the traces perform different subsequent actions (e.g., one trace
   browses, another deletes; one saves draft, another publishes). Those future
   actions belong to action slots and route obligations inside the current
   Materialized Page, not to instance identity.
17. The input is expected to contain steps from one General Page, but upstream
   clustering can occasionally include cross-page contamination. If a step clearly
   does NOT belong to the Page Identity General Page, do not force it into an
   instance. Put it in `out_of_page_obs` with the target/general-page evidence.
   All returned `instances` must be in-page candidates.

## Mandatory Top-Level Branch
Choose exactly one branch before writing JSON:
- NO SPLIT: `split_needed=false`, `instances=[]`, and `out_of_page_obs=[]`.
  The observations bind implicitly to `cluster_default`; never emit an explicit
  one-instance description in this branch.
- SPLIT OR REJECTED MEMBERSHIP: `split_needed=true`, with every input observation
  assigned exactly once to `instances[]` or `out_of_page_obs[]`.

The combination `split_needed=false` with any explicit instance or out-of-page
row is structurally invalid. If you need to describe even one explicit instance,
you must choose `split_needed=true`.

## Output Schema (JSON)
{{
  "entry_action_audit": {{
    "audited_obs_keys": ["every eligible entry obs_key exactly once"],
    "task_content_obs_keys": ["eligible entries whose CURRENT action consumes bounded visible task content"],
    "stable_shell_control_obs_keys": ["eligible entries whose CURRENT action targets any non-bounded task control, including shell/navigation/search/filter/tab/action-sheet controls"]
  }},
  "instances": [
    {{
      "instance_id": "i_001",
      "instance_label": "concise label for this instance (e.g., merchant name, chat partner, note title)",
      "instance_key_hint": "short_ascii_machine_key_hint (e.g., alpha_store, shanghai_century_avenue_8)",
      "obs_keys": ["trace_id:step_index"],
      "trace_steps": [{{"trace_id": "trace id", "step_index": 1}}],
      "step_indices": [0, 1, 2],
      "split_dimension": "entity|stable_context|submitted_query|fulfillment_mode|top_level_mode|primary_mode|service_mode|stable_context_mode|semantic_mode|fixed_capacity_task_content|state_variant|lifecycle_variant|none",
      "split_reason": {{
        "dimension": "same value as split_dimension",
        "same_general_page": true,
        "why_not_same_instance": "required when split_dimension is not none",
        "member_action_audit": [
          {{
            "obs_key": "required for fixed_capacity_task_content members",
            "current_action_target": "exact current target",
            "target_role": "task_content|stable_shell_control",
            "bounded_slot": "first|second|named|none",
            "visible_entity": "current visible entity or empty"
          }}
        ],
        "why_not_general_page": "required only when rejecting an out-of-page observation"
      }},
      "reason": "why these steps belong together or are separate"
    }}
  ],
  "out_of_page_obs": [
    {{
      "obs_key": "trace_id:step_index",
      "observed_general_page": "concise page identity visible in the screenshot",
      "why_not_page_identity": "why this observation is not the Page Identity above"
    }}
  ],
  "split_needed": true
}}

Requirements:
- Include `entry_action_audit` whenever the machine-computed eligible list is
  non-empty. Its three lists must form an exact, non-overlapping partition of
  that list; do not defer this audit to a smaller follow-up prompt or omit an
  ordinary filter/tab/action-sheet control. Copy the machine list rather than
  recomputing a smaller denominator from prose. Rows with no current action are
  lossless membership only and are not assigned an action role.
- Every observation listed in `task_content_obs_keys` MUST be assigned exactly
  once to an explicit `fixed_capacity_task_content` instance. Never classify a
  task-content entry in the top-level audit while placing it only in an `entity`,
  `stable_context`, or other non-fixed instance; if the visible target is not
  bounded task content, classify it as `stable_shell_control` instead.
- `stable_shell_control` is the non-task-control role for this contract. It
  includes ordinary visible filter, category-tab, action-sheet, navigation,
  search, and menu controls; do not reserve the role only for the persistent
  app shell.
- `split_needed` is false only if ALL input steps belong to the same in-page
  instance. In that case return `instances=[]` and `out_of_page_obs=[]`; the
  producer binds every input observation implicitly to `cluster_default`.
- Under `split_needed=false`, `entry_action_audit.task_content_obs_keys` MUST be
  empty: a CURRENT action that consumes bounded visible task content requires an
  explicit `fixed_capacity_task_content` instance. Stable shell/control entries
  may remain implicitly bound to `cluster_default` and must not be fabricated as
  a formal instance solely to satisfy the audit.
- You may ONLY reference steps that appear in the input above. Never reference,
  infer, or hallucinate step indices or obs_keys that are not in the input list.
- When `split_needed=true`, every input step must end up in exactly one place:
  either assigned to one `instances[]` entry, or reported in `out_of_page_obs`.
- When `split_needed=false`, do not emit an explicit single instance and do not
  repeat input observations in either list; return both lists empty.
- `out_of_page_obs` is for input steps that are visibly misassigned by upstream
  reasoning — i.e., the screenshot clearly shows a DIFFERENT page than the Page
  Identity above. These are steps that ARE in your input but should NOT have been
  (upstream clustering error). They must not appear in `instances`.
- Never return an instance whose `split_reason.same_general_page` is false. A
  false `same_general_page` means the observation is cross-page contamination and
  must be rejected instead of materialized.
- `instance_label` should be derived from the actual content (merchant name, person name, note title).
- A `fixed_capacity_task_content` instance MUST include a complete
  `split_reason.member_action_audit` for its machine-eligible members; non-entry
  continuation/no-current-action membership rows remain in `obs_keys` without a
  guessed role. It must not contain any eligible member classified as
  `stable_shell_control`, and all `task_content` members must be compatible in both visible
  entity and bounded slot. Re-audit the group instead of inferring compatibility from the
  shared task topic.
- `instance_key_hint` must be an ASCII machine-readable hint using only letters, digits, and underscores.
  It should be short, stable, and semantic, and must NOT be only a number, a pure ordinal,
  or a generic placeholder like `instance_1` unless there is truly no visible stable anchor.
- When a group was kept together despite minor OCR/name spelling variants, choose the best
  supported canonical visible entity name for `instance_label`. Do not include OCR,
  uncertainty markers, slash-separated alternatives, or debug explanations in
  `instance_label`; put uncertainty only in `reason`.
- Use a specific durable dimension such as `fulfillment_mode`, `top_level_mode`,
  `primary_mode`, `service_mode`, or `stable_context_mode` for top-level mode tabs that
  change the primary content universe while keeping the same General Page. Generic
  `semantic_mode`, `state_variant`, and `lifecycle_variant` are diagnostic-only when
  structured runtime state authority exists and will be deterministically demoted. Do not
  use mode dimensions for ordinary section tabs, selector
  values, language direction, filter chips, selected component state, or input
  suggestions.
  For visible delivery/home vs self-pickup modes, prefer stable key hints that include
  the mode token, such as `delivery_home`, `self_pickup_home`, or `pickup_mode_home`.
- Never use a future task goal or downstream action as `split_dimension`. If the only
  distinction is what the user will do next, keep the observations in the SAME instance.
  Use `state_variant` or `lifecycle_variant` only when the variant is already visible
  in the current screenshot.
- Each input observation is the CURRENT source screenshot before
  `action_context.current_action` executes. Attribute visible state changes only to
  `action_context.previous_action`. Never classify the CURRENT observation as an opened
  overlay, selected tab, submitted query, or other post-action state merely because its
  current action will open, select, submit, or mutate that state. When a current action
  changes a durable mode, keep the source observation in its visibly selected source
  mode and place only the later post-action observation in the target mode.
- An observation where the screenshot shows a panel, drawer, bottom sheet, action sheet, or shortcut surface overlaying a host page that remains visible is an opened interaction surface, not a standalone materialized page. Do not create a separate `lifecycle_variant` or `state_variant` instance for it. Keep it in the host page instance or report it in `out_of_page_obs` if it is visibly misassigned.
- Prefer `obs_keys` or `trace_steps` for assignment. `step_indices` alone are ambiguous when
  different traces share the same numeric step index.
- Preserve step order within each instance.
- Do not create instances with 0 steps.
- **HARD CONSTRAINT**: Every `obs_keys` entry and `trace_steps` entry in your output MUST
  reference a step that is actually present in the input steps list above. Do NOT infer,
  hallucinate, or reference steps from the same trace that are not in the input. If you
  believe a step from the same trace belongs to a different page, put the input step that
  evidences this in `out_of_page_obs` — never reference a step_index that was not provided.
"""

    def _build_schema_inference_prompt(
        self,
        traces_context: List[Dict[str, Any]] = None,
        routing_context: str = "",
        global_structure_context: str = "",
        content_summary: str = "",
        schema_candidate_ir: Dict[str, Any] = None,
        **legacy_kwargs,
    ) -> str:
        if traces_context is None:
            traces_context = []
        if legacy_kwargs:


            task = legacy_kwargs.get("task", "")
            steps = legacy_kwargs.get("steps", [])
            if task or steps:
                traces_context = [{
                    "trace_id": "legacy_trace",
                    "task": task,
                    "level": legacy_kwargs.get("level", ""),
                    "steps": steps if isinstance(steps, list) else [],
                }]
            routing_context = routing_context or legacy_kwargs.get("routing_context", "")
            global_structure_context = (
                global_structure_context
                or legacy_kwargs.get("global_structure_context", "")
            )
        traces_json = json.dumps(traces_context, ensure_ascii=False, indent=2)
        candidate_section = ""
        if schema_candidate_ir:
            candidate_section = f"""
## Schema Candidate IR And Execution Context
The following compact intermediate representation aggregates page-level extracted_items, entity_task_relevance, and when present a budgeted `schema_generation_prompt_view_v1`.
Prefer these candidates and execution obligations over raw extracted item volume.
State-bearing obligations are not optional examples: every origin/destination/date/filter/selector/autocomplete obligation must resolve to a stable global field owner or be explicitly rejected with a reason.

{json.dumps(schema_candidate_ir, ensure_ascii=False, indent=2)}
"""

        global_structure_section = ""
        if global_structure_context:
            global_structure_section = f"""
## Site Structure Overview
The following summarizes page-level layout, non-transient components, component relationships, and spatial text. Use it to identify data-bearing controls and component-boundary conflicts. Do not create schema fields from decorative UI structure alone.

{global_structure_context}
"""

        content_summary_section = ""
        if content_summary:
            content_summary_section = f"""
## Content Distribution
{content_summary}
"""

        return f"""You are building global execution schemas and entity data schemas for a website from user interaction traces.

## Site Pages
{routing_context}
{global_structure_section}
{content_summary_section}
{candidate_section}
## All Traces
{traces_json}

## Your Task
Analyze ALL traces above. Output a JSON object with a root `global_execution_schema`, complete `entities`, and complete `relationships` arrays.

**CRITICAL — TASK-RELEVANT ONLY**: Only create schemas for entities DIRECTLY needed to complete the user tasks. Entities are task-relevant if:
1. The user clicks on, selects, or interacts with items of that type during the task
2. The entity is displayed on a page the user navigates through AND its data must be consistent across pages
3. The entity is a parameter or result of an SDK method the task triggers

Do NOT create schemas for decorative content, promotional banners, navigation chrome, footer info, or data the user never interacts with. However, task-interacted payment methods, coupons, policies, filters, options, runtime states, and form values ARE valid schemas when they affect task outcome, SDK state, or validation.

**GLOBAL EXECUTION SCHEMA REQUIREMENT**:
- Page/component schemas are projections only. The root execution schema owns mutable task state, cross-page form state, query state, selected records, checkpoint refs, and autocomplete contracts.
- Final field ownership MUST use stable field paths such as `searchForm.origin`, `searchForm.destination`, `searchForm.date`, `stationPicker.query`, `stationPicker.suggestions`, `queryState.results`, or equivalent domain-specific paths.
- `business_query`, `query_draft`, `draft::...`, and instrumentation-only `state::...` refs are never acceptable final field owners for origin/destination/date/filter/selector/autocomplete controls. They may appear only as aliases or downstream expectations that resolve to a global field.
- Autocomplete/search controls must include query ref, source collection, candidate projection, suggestions ref, and commit target when evidence exists.
- For every row in `schema_generation_prompt_view_v1.requirements_by_family`, either promote it to `global_execution_schema.global_fields[]` or list it in `rejected_obligations[]` with a concrete reason. Rows are `[field_path, field_type, lifecycle, obligation_count, obligation_kinds]`.
- Treat those requirement rows as the only required global field authority. If raw trace/content evidence suggests a helper field that is not one of the requirement field paths, either add it as an alias of the nearest requirement owner or include it with `"optional_inferred": true` and a specific `"optional_inferred_reason"`. Do not create an unreferenced extra field as a required owner.

## Output Format
{{
  "global_execution_schema": {{
    "schema_id": "GlobalExecutionSchema",
    "global_fields": [
      {{
        "field_path": "stable.global.field",
        "field_type": "string|number|boolean|date|LocationRef|RecordRef|array|object",
        "lifecycle": "initial|runtime|initial_and_after_commit|derived|read_on_submit",
        "initial_value_policy": "from_trace|from_catalog_default|empty_until_user_input|task_instruction",
        "runtime_value_policy": "mutated_by_action|derived_from_query|selected_from_autocomplete|read_only",
        "producers": ["action_or_contract_id"],
        "consumers": ["page_or_checkpoint_or_sdk_method"],
        "evidence_steps": ["trace_id:stepN"],
        "projection_pages": ["page.html"],
        "aliases": ["draft::temporary_or_state_ref"]
      }}
    ],
    "autocomplete_contracts": [
      {{
        "query_ref": "stationPicker.query",
        "source_collection": "Station",
        "candidate_projection": ["name", "fullName", "pinyin", "code"],
        "suggestions_ref": "stationPicker.suggestions",
        "commit_target": "searchForm.origin"
      }}
    ],
    "rejected_obligations": [
      {{"obligation_id": "id", "reason": "why it is not promoted"}}
    ]
  }},
  "entities": [
    {{
      "name": "PascalCase entity name",
      "storage_key": "lowercase_plural",
      "fields": [
        {{"name": "snake_case_field", "type": "string|number|boolean|url|date", "observed_examples": ["val1", "val2"]}}
      ],
      "data_pre_generation_num": "many|few|none",
      "schema_role": "catalog|task_state|runtime_generated|selectable_option"
    }}
  ],
  "relationships": [
    {{"from": "EntityA", "to": "EntityB", "type": "belongs_to|has_many|references", "field": "entity_b_id"}}
  ]
}}

## Rules
- **Max 20 entities total**. Merge same-concept entities across traces (e.g., KTV venue + hair salon + restaurant = one Venue entity with superset of fields), but do not prune task-related candidate entities before decorative/catalog-only entities.
- Each entity must have an "id" field (type "string") as primary key.
- Entity names: PascalCase. Field names: snake_case.
- Type rules: "number" for numeric/currency, "boolean" for is_*/has_*, "url" for links, "date" for timestamps, "string" otherwise.
- Parent-child: use "_id" FK field in child.
- data_pre_generation_num: "many" (>=10 items), "few" (2-9), "none" (runtime user-generated like cart/order).
- Include observed_examples from extracted_items (2-3 actual values per field).
- **Max 20 fields per entity**. Focus on essential business attributes.
- Each entity should have at most 20 fields. If more fields are observed, keep the task-critical and business-identifying fields first.
- Use generic names (Venue not KtvVenue). Keep semantically distinct types separate (MenuItem vs BookingPackage vs GroupBuyDeal).
- **Min 2 fields per entity** (excluding id).
- Preserve task-related candidates from Schema Candidate IR unless you provide a rejected reason in `rejected_candidates`.
- If an inferred broad entity combines semantically distinct task states or catalog types, use a `split_entity` operation in downstream schema operations with `entity_name`, `new_entities`, and `field_names` to document the intended split.

Output JSON must retain the same required root keys:
{{"global_execution_schema": {{}}, "entities": [...], "relationships": [...], "local_to_global_entity": {{}}, "rejected_candidates": []}}"""

    def _build_schema_inference_chunk_prompt(
        self,
        *,
        traces_context: List[Dict[str, Any]],
        routing_context: str,
        global_structure_context: str,
        content_summary: str,
        schema_candidate_ir: Dict[str, Any],
        chunk: Dict[str, Any],
        chunk_index: int,
        chunk_count: int,
    ) -> str:

        base = self._build_schema_inference_prompt(
            traces_context=traces_context,
            routing_context=routing_context,
            global_structure_context=global_structure_context,
            content_summary=content_summary,
            schema_candidate_ir=schema_candidate_ir,
        )
        chunk_header = f"""## Schema Chunk Execution Mode
This is chunk {chunk_index + 1} of {chunk_count} for canonical global execution schema generation.
Chunk id: {chunk.get('chunk_id', '')}
Family id: {chunk.get('family_id', '')}
Field paths in scope: {json.dumps(chunk.get('field_paths', []), ensure_ascii=False)}

Rules for this chunk:
- Decide every row in the chunk-local `requirements_by_family` table.
- Keep field owners stable enough to merge with other chunks.
- Do not invent local owners such as `business_query`, `query_draft`, `draft::...`, or instrumentation-only `state::...`.
- Extra fields outside this chunk's requirement field paths must be aliases or `optional_inferred`; they are not required owners.
- Return a complete JSON object with `global_execution_schema`, `entities`, and `relationships`.
"""
        return f"{chunk_header}\n\n{base}"

    def _build_schema_inference_merge_prompt(
        self,
        *,
        traces_context: List[Dict[str, Any]],
        routing_context: str,
        global_structure_context: str,
        content_summary: str,
        schema_candidate_ir: Dict[str, Any],
        chunk_outputs: List[Dict[str, Any]],
    ) -> str:

        traces_json = json.dumps(traces_context or [], ensure_ascii=False, indent=2)
        candidates_json = json.dumps(schema_candidate_ir or {}, ensure_ascii=False, indent=2)
        outputs_json = json.dumps(chunk_outputs or [], ensure_ascii=False, indent=2)
        structure_section = (
            f"\n## Site Structure Overview\n{global_structure_context}\n"
            if global_structure_context else ""
        )
        content_section = f"\n## Content Distribution\n{content_summary}\n" if content_summary else ""
        return f"""You are merging family-level schema inference outputs into ONE canonical root global execution schema.

## Site Pages
{routing_context}
{structure_section}
{content_section}
## Full Schema Candidate Context
{candidates_json}

## All Traces
{traces_json}

## Chunk Outputs To Merge
{outputs_json}

## Merge Requirements
- Output one `global_execution_schema` with globally unique stable `global_fields`.
- Preserve every promoted field from chunk outputs unless two fields are true aliases of the same state owner.
- If two chunks propose equivalent owners, keep the more general stable owner and move the other paths to `aliases`.
- Every row in `schema_generation_prompt_view_v1.requirements_by_family` must be promoted, aliased, or rejected with a concrete reason.
- Requirement rows are the only required owner authority. Extra merged fields without direct requirement/alias support must have `optional_inferred=true` with a concrete reason, or be collapsed into an alias of a required owner.
- `business_query`, `query_draft`, `draft::...`, and instrumentation-only `state::...` are never final field owners.
- Preserve autocomplete contracts and ensure their `query_ref`, `suggestions_ref`, and `commit_target` resolve to canonical global fields or valid field expressions.
- Return complete `entities` and `relationships` arrays, merging duplicates across chunks.

## Output Format
Return JSON only:
{{
  "global_execution_schema": {{
    "schema_id": "GlobalExecutionSchema",
    "global_fields": [],
    "autocomplete_contracts": [],
    "rejected_obligations": []
  }},
  "entities": [],
  "relationships": [],
  "local_to_global_entity": {{}},
  "rejected_candidates": []
}}"""


GLOBAL_REASONING_FIX_PROMPT = """You previously returned a global reasoning result, but it has schema violations.
Fix ONLY the issues listed below. Keep everything else exactly the same — do not change correct data, General Page clustering, or routes.

{issues}

Return the corrected JSON with the EXACT same top-level keys: app_type, step_labels, skipped_steps, canonical_pages, canonical_routes, task_related_elements.
"""
