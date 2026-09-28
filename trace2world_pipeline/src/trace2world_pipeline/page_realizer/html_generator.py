
import os
import re
import json
import asyncio
import hashlib
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, Any, Iterable, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async
from trace2world_pipeline.core.fixloop.text_patch import apply_text_patch_edits
from trace2world_pipeline.page_realizer.cascade_pagegen import CascadeRuntimeConfig, extract_html, generate_page_cascade_async, normalize_html_output
from trace2world_pipeline.page_realizer.pagegen_input_budget import (
    DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
    DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET,
    DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
    PAGEGEN_TEXT_TOKENIZER,
    enforce_pagegen_preflight_budget,
    enforce_pagegen_provider_budget,
    estimate_pagegen_message_input_tokens,
    normalize_pagegen_input_token_budget,
)
from trace2world_pipeline.page_realizer.resource_semantics import pagegen_resource_prompt_omission_reason
from trace2world_pipeline.app_spec.viewport_defaults import MOBILE_VIEWPORT_HEIGHT, MOBILE_VIEWPORT_WIDTH
from trace2world_pipeline.app_spec.environment_profile import build_render_context, resolve_environment_profile
from trace2world_pipeline.app_spec.environment_prompt_policy import build_environment_prompt_policy, prompt_policy_manifest
from trace2world_pipeline.page_realizer.environment_render_policy import build_environment_render_policy


PAGE_HTML_FIX_PROMPT = """You previously generated HTML for {filename}, but it violates page contract rules.
Fix ONLY the issues listed below by returning a minimal JSON patch. Keep everything else exactly the same — preserve correct copy, resource references, data-element-id values, existing SDK calls, and correct navigation targets.

Issues:
{issues}

Page contract:
- Valid filenames: {valid_filenames}
- Floating component: {floating_component}
- Overlay policy (legacy alias): {floating_component}
- Interaction policy: {interaction_policy}
- Navigation Out: {navigation_out}
- Task elements that must keep their exact data-element-id values: {task_elements}

Additional rules:
- Return JSON only as {{"edits": [{{"oldText": "...", "newText": "...", "reason": "..."}}]}}.
- oldText MUST be an exact substring copied from the provided Full Previous HTML and MUST occur exactly once.
- newText MUST be only the replacement for oldText, not the full page.
- Do NOT return html_content, full HTML, explanations, markdown fences, or extra keys.
- Make the smallest patch that fixes the listed contract issues.
- Do NOT use scrollIntoView or similar viewport scrolling hacks to simulate overlays.
- Do NOT push main/footer layout to create overlay space; the floating component must float above existing content without reflow.
- Follow the floating component template exactly when it is present: fullscreen host, optional backdrop, floating panel, deterministic toggle/close behavior.
- Do NOT invent new target filenames. Use only the valid filenames listed above or keep the item non-navigable.
- Do NOT nest interactive elements. Elements such as button, a, label, select, input, textarea, and summary must not contain each other; use a non-interactive wrapper plus sibling controls instead.
- Do NOT use raw Tailwind arbitrary-value class selectors in querySelector/querySelectorAll. `.w-[52px]`, `.bg-[#ffd100]`, `.translate-x-[20px]` are invalid CSS selectors unless escaped; prefer explicit data attributes or quoted `[class~="..."]` selectors.
"""

DEFAULT_PAGE_FIX_ROUNDS = 0


@dataclass
class GeneratedPage:
    filename: str
    html_content: str
    css_content: str
    entity_keys: List[str] = field(default_factory=list)
    fix_issues: List[Dict[str, Any]] = field(default_factory=list)
    fix_rounds: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class PageGenContractValidationError(ValueError):

    def __init__(self, filename: str, issues: List[Dict[str, Any]], candidate_page: GeneratedPage):
        self.filename = filename
        self.issues = list(issues or [])
        self.candidate_page = candidate_page
        summary = [
            {
                "code": issue.get("code"),
                "selector_hint": issue.get("selector_hint"),
            }
            for issue in self.issues[:5]
            if isinstance(issue, dict)
        ]
        super().__init__(
            "PageGen contract validation failed for "
            f"{filename}: {len(self.issues)} blocking issue(s); "
            f"issues={json.dumps(summary, ensure_ascii=False)}"
        )


class Trace2WorldHtmlGenerator:

    def __init__(self, logger: Trace2WorldLogger = None, max_concurrent: int = 3,
                 model: str = None, reasoning_effort: str = "medium",
                 max_prompt_screenshots: int = 16,
                 page_fixloop_enabled: bool = False,
                 page_fix_max_rounds: Optional[int] = None,
                 environment_profile: Optional[Dict[str, Any]] = None):
        self.logger = logger or Trace2WorldLogger()
        self.max_concurrent = max_concurrent
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_prompt_screenshots = max(1, int(max_prompt_screenshots or 16))
        self.page_fixloop_enabled = bool(page_fixloop_enabled)
        self.environment_profile = (
            dict(environment_profile)
            if isinstance(environment_profile, dict) and environment_profile
            else resolve_environment_profile()
        )
        self.render_context = build_render_context(self.environment_profile)
        self.render_policy = build_environment_render_policy(self.environment_profile)
        self.generation_prompt_policy = build_environment_prompt_policy(
            self.environment_profile, "pagegen_generation"
        )
        self.repair_prompt_policy = build_environment_prompt_policy(
            self.environment_profile, "pagegen_repair"
        )
        if page_fix_max_rounds is None:
            page_fix_max_rounds = self._env_int("T2W_PAGEGEN_MAX_REPAIR_ROUNDS", DEFAULT_PAGE_FIX_ROUNDS, minimum=0)
        self.page_fix_max_rounds = max(0, int(page_fix_max_rounds or 0)) if self.page_fixloop_enabled else 0

    @staticmethod
    def _normalize_escaped_html_attribute_quotes(html_content: str) -> str:
        if not html_content:
            return html_content or ""

        def normalize_tag(match: re.Match[str]) -> str:
            tag = match.group(0)
            return tag.replace(r"\"", '"').replace(r"\'", "'")

        return re.sub(r"<[^<>]+>", normalize_tag, str(html_content))

    @staticmethod
    def _normalize_page_html_content(html_content: Any) -> str:
        text = str(html_content or "")
        if not text.strip():
            return text
        text = Trace2WorldHtmlGenerator._normalize_escaped_html_attribute_quotes(text)
        normalized = Trace2WorldHtmlGenerator._normalize_escaped_html_attribute_quotes(
            normalize_html_output(extract_html(text))
        )
        lower = normalized.lower()
        if "<html" in lower or "<!doctype html" in lower:
            return normalized
        return text

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return max(minimum, int(default))
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
        return max(minimum, value)

    @staticmethod
    def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return max(minimum, float(default))
        try:
            value = float(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be a number, got {raw!r}") from exc
        return max(minimum, value)

    def _resolve_pagegen_worker_count(self, job_count: int) -> int:
        if job_count <= 0:
            return 0
        stage_cap = self._env_int("T2W_PAGEGEN_STAGE_MAX_INFLIGHT", 16)
        global_cap = max(1, int(self.max_concurrent or 1))
        return max(1, min(job_count, global_cap, stage_cap))

    def _resolve_pagegen_max_attempts(self) -> int:
        raw_attempts = os.getenv("T2W_PAGEGEN_MAX_ATTEMPTS")
        if raw_attempts is not None and str(raw_attempts).strip():
            return self._env_int("T2W_PAGEGEN_MAX_ATTEMPTS", 1, minimum=1)
        raw_rounds = os.getenv("T2W_PAGEGEN_MAX_REPAIR_ROUNDS")
        if raw_rounds is not None and str(raw_rounds).strip():
            return max(1, 1 + self._env_int("T2W_PAGEGEN_MAX_REPAIR_ROUNDS", 0, minimum=0))
        if not self.page_fixloop_enabled:
            return 1
        return max(1, 1 + self.page_fix_max_rounds)

    def _resolve_pagegen_retry_backoff_seconds(self) -> float:
        return self._env_float("T2W_PAGEGEN_RETRY_BACKOFF_SECONDS", 30.0)

    @staticmethod
    def _safe_len(value: Any) -> int:
        if isinstance(value, (list, tuple, set)):
            return len(value)
        if isinstance(value, dict):
            return len(value)
        if value:
            return 1
        return 0

    @classmethod
    def _estimate_json_complexity(cls, value: Any, depth: int = 0) -> int:
        if value is None:
            return 0
        if depth > 5:
            return 1
        if isinstance(value, dict):
            total = len(value)
            for k, v in value.items():
                total += min(len(str(k)), 80) // 16
                total += cls._estimate_json_complexity(v, depth + 1)
            return total
        if isinstance(value, (list, tuple, set)):
            total = len(value)
            for index, item in enumerate(value):
                if index >= 80:
                    break
                total += cls._estimate_json_complexity(item, depth + 1)
            return total
        if isinstance(value, str):
            return max(1, min(len(value), 4000) // 80)
        return 1

    @staticmethod
    def _estimate_json_chars(value: Any) -> int:
        if value in (None, "", [], {}):
            return 0
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            return len(str(value))

    @staticmethod
    def _rough_token_estimate(chars: int) -> int:
        try:
            char_count = max(0, int(chars or 0))
        except (TypeError, ValueError):
            char_count = 0
        if char_count <= 0:
            return 0
        return max(1, (char_count + 3) // 4)

    @staticmethod
    def _estimate_pagegen_input_token_proxy(
        *,
        estimated_text_tokens: int,
        prompt_screenshots: int,
        has_icon_sprite: bool = False,
        has_photo_sprite: bool = False,
        has_asset_sprite: bool = False,
        image_token_proxy: int = DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
    ) -> Dict[str, int]:
        image_proxy = max(0, int(image_token_proxy or DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY))
        screenshot_token_proxy = max(0, int(prompt_screenshots or 0)) * image_proxy


        sprite_count = int(bool(has_icon_sprite)) + int(bool(has_photo_sprite))
        sprite_token_proxy = sprite_count * image_proxy
        prompt_boilerplate_token_proxy = 6000
        total = (
            max(0, int(estimated_text_tokens or 0))
            + screenshot_token_proxy
            + sprite_token_proxy
            + prompt_boilerplate_token_proxy
        )
        return {
            "estimated_input_tokens": total,
            "screenshot_token_proxy": screenshot_token_proxy,
            "sprite_token_proxy": sprite_token_proxy,
            "prompt_boilerplate_token_proxy": prompt_boilerplate_token_proxy,
        }

    @classmethod
    def _compute_pagegen_scheduling_weight(
        cls,
        filename: str,
        page_design: Dict[str, Any],
        page_interfaces: List[Dict[str, Any]],
        page_evidence: Dict[str, Any],
        task_elements_for_page: Any,
        page_content_samples: Dict[str, Any],
        page_layout: Dict[str, Any],
        extra_page: Dict[str, Any],
    ) -> Dict[str, Any]:
        page_evidence = page_evidence or {}
        extra_page = extra_page or {}
        page_design = page_design if isinstance(page_design, dict) else {}

        prompt_screenshots = cls._safe_len(page_evidence.get("prompt_screenshot_refs"))
        page_screenshots = cls._safe_len(page_evidence.get("page_screenshot_refs"))
        unique_screenshots = cls._safe_len(page_evidence.get("unique_prompt_screenshot_refs"))
        raw_resources = cls._safe_len(extra_page.get("page_resources"))
        dedup_resources = cls._as_int(
            extra_page.get("dedup_resource_count"),
            cls._safe_len(extra_page.get("dedup_page_resources")) or raw_resources,
        )
        resources = dedup_resources
        components = cls._safe_len(page_evidence.get("components")) + cls._safe_len(page_design.get("components"))
        interfaces = cls._safe_len(page_interfaces)
        task_elements = cls._estimate_json_complexity(task_elements_for_page)
        extracted_items = cls._estimate_json_complexity(page_evidence.get("extracted_items") or page_content_samples)
        layout = cls._estimate_json_complexity(page_layout)
        actions = (
            cls._safe_len(page_evidence.get("page_actions"))
            + cls._safe_len(page_evidence.get("task_continuations"))
            + cls._safe_len(page_evidence.get("outbound_edges"))
            + cls._safe_len(page_evidence.get("inbound_edges"))
        )
        description_chars = len(str(page_design.get("description") or page_evidence.get("description") or ""))
        resource_list_chars = len(str(extra_page.get("dedup_resource_list_text") or extra_page.get("resource_list_text") or ""))
        has_icon_sprite = bool(extra_page.get("icon_sprite_path"))
        has_photo_sprite = bool(extra_page.get("photo_sprite_path"))
        has_asset_sprite = bool(extra_page.get("asset_sprite_path"))
        page_environment_projection = (
            page_evidence.get("page_environment_projection")
            or page_evidence.get("page_environment_spec")
            or {}
        )
        page_environment_chars = cls._estimate_json_chars(
            cls._compact_page_environment_spec_for_pagegen_prompt(page_environment_projection)
            if isinstance(page_environment_projection, dict)
            else page_environment_projection
        )
        pagegen_input_scope = (
            page_evidence.get("pagegen_input_scope")
            if isinstance(page_evidence.get("pagegen_input_scope"), dict)
            else {}
        )
        pagegen_render_view_chars = cls._estimate_json_chars(
            pagegen_input_scope.get("pagegen_render_view", {})
        )
        raw_render_scope_contract = page_evidence.get("render_scope_contract") or {}
        raw_render_scope_contract_chars = cls._estimate_json_chars(raw_render_scope_contract)
        render_scope_contract_chars = raw_render_scope_contract_chars
        if pagegen_render_view_chars:


            text_context_chars = (
                pagegen_render_view_chars
                + render_scope_contract_chars
                + description_chars
            )
        else:
            text_context_chars = (
                page_environment_chars
                + render_scope_contract_chars
                + resource_list_chars
                + description_chars
            )
        estimated_text_tokens = cls._rough_token_estimate(text_context_chars)
        input_token_proxy = cls._estimate_pagegen_input_token_proxy(
            estimated_text_tokens=estimated_text_tokens,
            prompt_screenshots=prompt_screenshots,
            has_icon_sprite=has_icon_sprite,
            has_photo_sprite=has_photo_sprite,
            has_asset_sprite=has_asset_sprite,
        )

        features = {
            "prompt_screenshots": prompt_screenshots,
            "page_screenshots": page_screenshots,
            "unique_screenshots": unique_screenshots,
            "resources": resources,
            "raw_resources": raw_resources,
            "dedup_resources": dedup_resources,
            "components": components,
            "interfaces": interfaces,
            "task_elements_complexity": task_elements,
            "extracted_items_complexity": extracted_items,
            "layout_complexity": layout,
            "actions": actions,
            "description_chars": description_chars,
            "resource_list_chars": resource_list_chars,
            "has_icon_sprite": has_icon_sprite,
            "has_photo_sprite": has_photo_sprite,
            "has_asset_sprite": has_asset_sprite,
            "page_environment_chars": page_environment_chars,
            "pagegen_render_view_chars": pagegen_render_view_chars,
            "raw_render_scope_contract_chars": raw_render_scope_contract_chars,
            "render_scope_contract_chars": render_scope_contract_chars,
            "text_context_chars": text_context_chars,
            "estimated_text_tokens": estimated_text_tokens,
            **input_token_proxy,
        }


        scheduling_weight = (
            prompt_screenshots * 4
            + page_screenshots
            + unique_screenshots
            + resources
            + components
            + actions
        )
        return {
            "filename": filename,
            "scheduling_weight": int(scheduling_weight),
            "features": features,
            "reason": "larger visual/context/action payloads are scheduled first to reduce pagegen tail latency",
        }

    @classmethod
    def _predict_pagegen_cost(
        cls,
        filename: str,
        page_design: Dict[str, Any],
        page_interfaces: List[Dict[str, Any]],
        page_evidence: Dict[str, Any],
        task_elements_for_page: Any,
        page_content_samples: Dict[str, Any],
        page_layout: Dict[str, Any],
        extra_page: Dict[str, Any],
    ) -> Dict[str, Any]:
        return cls._compute_pagegen_scheduling_weight(
            filename,
            page_design,
            page_interfaces,
            page_evidence,
            task_elements_for_page,
            page_content_samples,
            page_layout,
            extra_page,
        )

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _resolve_pagegen_execution_flow(
        pagegen_decision: Optional[Dict[str, Any]],
        pagegen_logic_flow: str = "standard",
    ) -> str:
        decision = pagegen_decision or {}
        flow = str(decision.get("pagegen_execution_flow") or "").strip()
        if not flow:
            legacy_flow = str(decision.get("effective_logic_flow") or "").strip()
            if legacy_flow == "standard":
                flow = "single_pass"
            elif legacy_flow:
                flow = legacy_flow
        if not flow:
            flow = "single_pass"
        return "cascade" if flow == "cascade" else "single_pass"

    @classmethod
    def _build_pagegen_budget_decision(
        cls,
        requested_logic_flow: str,
        cascade_page_input: Dict[str, Any],
        schedule: Dict[str, Any],
        page_evidence: Dict[str, Any],
        page_resources: List[Dict[str, Any]],
        runtime_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        requested = str(requested_logic_flow or "standard")
        runtime_config = runtime_config or {}
        schedule = schedule or {}
        features = schedule.get("features") if isinstance(schedule.get("features"), dict) else {}
        page_evidence = page_evidence or {}

        prompt_screenshots = cls._as_int(
            features.get("prompt_screenshots"),
            cls._safe_len(page_evidence.get("prompt_screenshot_refs") or page_evidence.get("screenshot_refs")),
        )
        raw_resource_count = max(
            cls._as_int(features.get("raw_resources"), 0),
            cls._safe_len(page_resources),
            cls._safe_len(page_evidence.get("page_resources")),
        )
        dedup_resource_count = max(
            cls._as_int(features.get("dedup_resources"), -1),
            cls._as_int(page_evidence.get("dedup_resource_count"), -1),
            cls._safe_len(page_evidence.get("dedup_page_resources")) if "dedup_page_resources" in page_evidence else -1,
        )
        resource_count = dedup_resource_count if dedup_resource_count >= 0 else raw_resource_count
        scheduling_weight = cls._as_int(schedule.get("scheduling_weight"), 0)
        resource_list_chars = cls._as_int(
            features.get("resource_list_chars"),
            len(str(page_evidence.get("resource_list_text") or "")),
        )
        estimated_text_tokens = cls._as_int(
            features.get("estimated_text_tokens"),
            cls._rough_token_estimate(
                cls._as_int(features.get("text_context_chars"), 0)
                or resource_list_chars
                or len(str(page_evidence.get("resource_list_text") or ""))
            ),
        )
        estimated_input_tokens = cls._as_int(
            features.get("estimated_input_tokens"),
            cls._estimate_pagegen_input_token_proxy(
                estimated_text_tokens=estimated_text_tokens,
                prompt_screenshots=prompt_screenshots,
                has_icon_sprite=bool(features.get("has_icon_sprite")),
                has_photo_sprite=bool(features.get("has_photo_sprite")),
                has_asset_sprite=bool(features.get("has_asset_sprite")),
            )["estimated_input_tokens"],
        )

        round1_limit = max(1, cls._as_int(runtime_config.get("round1_image_limit"), 8))
        prompt_threshold = max(1, cls._as_int(
            runtime_config.get("adaptive_cascade_min_prompt_screenshots"),
            12,
        ))
        resource_threshold = max(1, cls._as_int(
            runtime_config.get("adaptive_cascade_min_resources"),
            48,
        ))
        estimated_input_token_threshold = normalize_pagegen_input_token_budget(cls._as_int(
            runtime_config.get("pagegen_max_estimated_input_tokens"),
            runtime_config.get(
                "max_estimated_input_tokens",
                DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET,
            ),
        ))
        image_token_proxy = max(0, cls._as_int(
            runtime_config.get("pagegen_input_image_token_proxy"),
            DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
        ))
        overhead_token_proxy = max(0, cls._as_int(
            runtime_config.get("pagegen_input_overhead_token_proxy"),
            DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
        ))
        adaptive_enabled = bool(runtime_config.get("adaptive_cascade_enabled", True))

        cascade_triggered_by: List[str] = []
        if prompt_screenshots > prompt_threshold:
            cascade_triggered_by.append("prompt_screenshots")
        raw_prompt_over_budget = estimated_input_tokens > estimated_input_token_threshold
        input_budget_triggered_by: List[str] = []
        if raw_prompt_over_budget:
            input_budget_triggered_by.append("estimated_input_tokens")
        resource_pressure = resource_count > resource_threshold

        over_budget = bool(input_budget_triggered_by)
        cascade_available = cls._has_cascade_visual_refs(cascade_page_input)
        if requested == "cascade":
            requested = "full"
        pagegen_execution_flow = "single_pass"
        reason = f"{requested}_within_budget"
        if adaptive_enabled and cascade_triggered_by and cascade_available:
            pagegen_execution_flow = "cascade"
            reason = f"{requested}_adaptive_cascade"
        elif adaptive_enabled and cascade_triggered_by and not cascade_available:
            reason = f"{requested}_cascade_needed_missing_visual_input"
        elif over_budget:
            reason = f"{requested}_budgeted_prompt"
        elif resource_pressure:
            reason = f"{requested}_resource_pressure_single_pass"

        return {
            "requested_logic_flow": requested,
            "pagegen_execution_flow": pagegen_execution_flow,
            "reason": reason,
            "cascade_available": cascade_available,
            "input_budget_gate": {
                "enabled": adaptive_enabled,
                "over_budget": over_budget,
                "raw_prompt_over_budget": raw_prompt_over_budget,
                "triggered_by": input_budget_triggered_by,
                "cascade_triggered_by": cascade_triggered_by,
                "resource_pressure": resource_pressure,
                "resource_pressure_by": ["resources"] if resource_pressure else [],
                "metrics": {
                    "prompt_screenshots": prompt_screenshots,
                    "resources": resource_count,
                    "raw_resources": raw_resource_count,
                    "dedup_resources": resource_count,
                    "scheduling_weight": scheduling_weight,
                    "resource_list_chars": resource_list_chars,
                    "estimated_text_tokens": estimated_text_tokens,
                    "estimated_input_tokens": estimated_input_tokens,
                    "raw_estimated_input_tokens": estimated_input_tokens,
                    "screenshot_token_proxy": cls._as_int(features.get("screenshot_token_proxy"), 0),
                    "sprite_token_proxy": cls._as_int(features.get("sprite_token_proxy"), 0),
                    "prompt_boilerplate_token_proxy": cls._as_int(features.get("prompt_boilerplate_token_proxy"), 0),
                    "text_context_chars": cls._as_int(features.get("text_context_chars"), 0),
                    "page_environment_chars": cls._as_int(features.get("page_environment_chars"), 0),
                    "pagegen_render_view_chars": cls._as_int(features.get("pagegen_render_view_chars"), 0),
                    "raw_render_scope_contract_chars": cls._as_int(
                        features.get("raw_render_scope_contract_chars"),
                        0,
                    ),
                    "render_scope_contract_chars": cls._as_int(features.get("render_scope_contract_chars"), 0),
                },
                "thresholds": {
                    "prompt_screenshots": prompt_threshold,
                    "resource_pressure_soft_limit": resource_threshold,
                    "round1_image_limit": round1_limit,
                    "estimated_input_tokens": estimated_input_token_threshold,
                    "image_token_proxy": image_token_proxy,
                    "overhead_token_proxy": overhead_token_proxy,
                },
            },
        }

    @staticmethod
    def _has_cascade_visual_refs(cascade_page_input: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(cascade_page_input, dict):
            return False
        for key in (
            "page_screenshot_refs",
            "resource_screenshot_refs",
            "prompt_screenshot_refs",
            "screenshot_refs",
        ):
            value = cascade_page_input.get(key)
            if isinstance(value, list) and value:
                return True
        return False

    @staticmethod
    def _normalize_cascade_page_input(
        filename: str,
        cascade_page_input: Optional[Dict[str, Any]],
        page_evidence: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized: Dict[str, Any] = dict(cascade_page_input or {})
        evidence = page_evidence if isinstance(page_evidence, dict) else {}
        scope = evidence.get("pagegen_input_scope")
        if not isinstance(scope, dict):
            scope = {}
        for key in (
            "page_screenshot_refs",
            "prompt_screenshot_refs",
            "unique_prompt_screenshot_refs",
            "task_extra_screenshot_refs",
            "non_prompt_page_screenshot_refs",
            "full_page_screenshot_refs",
            "excluded_page_screenshot_refs",
            "source_host_visual_authority_screenshots",
            "source_host_visual_authority_issues",
        ):
            if normalized.get(key):
                continue
            value = scope.get(key) or evidence.get(key)
            if value:
                normalized[key] = value
        if not normalized.get("filename") and filename:
            normalized["filename"] = filename
        for key in ("page_id", "page_name", "page_type", "fidelity_mode", "render_scope_contract"):
            if not normalized.get(key):
                value = scope.get(key) or evidence.get(key)
                if value:
                    normalized[key] = value
        return normalized

    @staticmethod
    def _merge_cascade_scope_into_page_evidence(
        page_evidence: Optional[Dict[str, Any]],
        cascade_page_input: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        evidence = dict(page_evidence or {})
        cascade = cascade_page_input if isinstance(cascade_page_input, dict) else {}
        if not cascade:
            return evidence
        scope = dict(evidence.get("pagegen_input_scope") or {})
        changed = False
        screenshot_scope_keys = {
            "page_screenshot_refs",
            "prompt_screenshot_refs",
            "unique_prompt_screenshot_refs",
            "task_extra_screenshot_refs",
            "non_prompt_page_screenshot_refs",
            "full_page_screenshot_refs",
            "excluded_page_screenshot_refs",
            "source_host_visual_authority_screenshots",
        }
        for key in (
            "page_screenshot_refs",
            "prompt_screenshot_refs",
            "unique_prompt_screenshot_refs",
            "task_extra_screenshot_refs",
            "non_prompt_page_screenshot_refs",
            "full_page_screenshot_refs",
            "excluded_page_screenshot_refs",
            "source_host_visual_authority_screenshots",
            "source_host_visual_authority_issues",
            "route_action_slots",
            "action_slot_route_contract",
            "spec_execution_view",
            "required_interaction_surfaces",
            "action_effect_contracts",
            "runtime_transaction_contracts",
            "dom_binding_contracts",
        ):
            value = cascade.get(key)
            if value in (None, "", [], {}):
                continue
            if scope.get(key) != value:
                scope[key] = value
                changed = True
            if key in screenshot_scope_keys and evidence.get(key) != value:
                evidence[key] = value
                changed = True
        if changed:
            evidence["pagegen_input_scope"] = scope
        return evidence

    @classmethod
    def _sort_pagegen_jobs(cls, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            jobs,
            key=lambda job: (-int(job.get("schedule", {}).get("scheduling_weight", 0)), int(job.get("input_index", 0))),
        )

    @staticmethod
    def _write_pagegen_schedule_debug(output_dir: str, schedule: List[Dict[str, Any]]) -> None:
        if not output_dir:
            return
        phase_output = PhaseOutputManager(output_dir)
        phase_output.write_json(
            "pagegen",
            "debug",
            "pagegen_schedule.json",
            {
                "format": "pagegen_schedule",
                "strategy": "scheduling_weight_desc",
                "pages": schedule,
            },
            producer="frontend.html_generator",
        )
        phase_output.write_json(
            "pagegen",
            "debug",
            "pagegen_input_budget_decisions.json",
            {
                "format": "pagegen_input_budget_decisions",
                "pages": [
                    {
                        "filename": item.get("filename", ""),
                        "rank": item.get("rank"),
                        "scheduling_weight": item.get("scheduling_weight", 0),
                        "pagegen_decision": item.get("pagegen_decision", {}),
                    }
                    for item in schedule
                ],
            },
            producer="frontend.html_generator",
        )
        budget_pages = []
        for item in schedule:
            decision = item.get("pagegen_decision", {}) if isinstance(item.get("pagegen_decision"), dict) else {}
            gate = decision.get("input_budget_gate", {}) if isinstance(decision.get("input_budget_gate"), dict) else {}
            metrics = gate.get("metrics", {}) if isinstance(gate.get("metrics"), dict) else {}
            thresholds = gate.get("thresholds", {}) if isinstance(gate.get("thresholds"), dict) else {}
            budget_pages.append(
                {
                    "filename": item.get("filename", ""),
                    "rank": item.get("rank"),
                    "pagegen_execution_flow": decision.get("pagegen_execution_flow", "single_pass"),
                    "reason": decision.get("reason", ""),
                    "over_budget": bool(gate.get("over_budget")),
                    "raw_prompt_over_budget": bool(gate.get("raw_prompt_over_budget")),
                    "triggered_by": list(gate.get("triggered_by") or []),
                    "cascade_triggered_by": list(gate.get("cascade_triggered_by") or []),
                    "resource_pressure": bool(gate.get("resource_pressure")),
                    "resource_pressure_by": list(gate.get("resource_pressure_by") or []),
                    "metrics": metrics,
                    "thresholds": thresholds,
                    "budget_status": "over_budget" if gate.get("over_budget") else "within_budget",
                }
            )
        phase_output.write_json(
            "pagegen",
            "debug",
            "pagegen_input_budget_report.json",
            {
                "format": "pagegen_input_budget_report",
                "schema_revision": 1,
                "token_estimate_kind": "rough_chars_div_4_plus_multimodal_proxy",
                "api_call_budget_target": "each real PageGen API call should stay <= thresholds.estimated_input_tokens unless an explicit stage contract overrides it",
                "budget_policy": "complete PageGen authority is checked against the 40k default without semantic-field compaction; visual cascade remains screenshot-count-driven; every real call has preflight and provider-reported hard gates",
                "pages": budget_pages,
            },
            producer="frontend.html_generator",
        )

    @staticmethod
    def _frontend_pages_output_dir(output_dir: str) -> str:
        if not output_dir:
            return ""
        normalized = os.path.normpath(output_dir)
        if normalized.endswith(os.path.join("phase_pagegen", "pages")):
            return output_dir
        return str(PhaseOutputManager(output_dir).formal("pagegen", "pages"))

    @staticmethod
    def _rewrite_page_paths_for_frontend_pages(html: str) -> str:
        if not html:
            return html

        html = re.sub(r'((?:src|href)=["\'])images/', r'\1../images/', html)
        html = re.sub(r'((?:src|href)=["\'])\./images/', r'\1../images/', html)
        html = re.sub(r'(?P<quote>["\'])images/', r'\g<quote>../images/', html)
        html = re.sub(r'(?P<quote>["\'])\./images/', r'\g<quote>../images/', html)
        for js_file in ("business_logic", "data_layer", "seed_data", "route_manifest", "route_runtime"):
            html = re.sub(
                r'((?:src|href)=["\'])(?:\./)?(?:\.\./\.\./phase_sdk_generation/runtime/)?' + js_file + r'\.js(?P<query>\?[^"\']*)?(["\'])',
                r'\1../../phase_sdk_generation/runtime/' + js_file + r'.js\g<query>\3',
                html,
            )
        html = html.replace("url('images/", "url('../images/")
        html = html.replace('url("images/', 'url("../images/')
        html = html.replace("url(images/", "url(../images/")
        return html

    @staticmethod
    def _rewrite_css_paths_for_frontend_pages(css: str) -> str:
        if not css:
            return css
        css = css.replace("url('images/", "url('../images/")
        css = css.replace('url("images/', 'url("../images/')
        css = css.replace("url(images/", "url(../images/")
        return css

    @staticmethod
    def _page_fix_debug_filename(filename: str) -> str:
        name = (filename or "unknown_page").replace("\\", "/").split("/")[-1]
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".json"

    def _write_page_fix_debug(self, output_dir: str, page: GeneratedPage) -> None:
        if not output_dir or not page:
            return
        fix_rounds = int(getattr(page, "fix_rounds", 0) or 0)
        remaining_issues = list(getattr(page, "fix_issues", []) or [])
        if not fix_rounds and not remaining_issues:
            return
        payload = {
            "format": "pagegen_page_fix_issues",
            "schema_revision": 1,
            "filename": getattr(page, "filename", ""),
            "fix_rounds": fix_rounds,
            "max_fix_rounds": self.page_fix_max_rounds,
            "fixloop_enabled": self.page_fixloop_enabled,
            "mode": (
                "repair"
                if self.page_fixloop_enabled
                else "audit_only"
            ),
            "remaining_issue_count": len(remaining_issues),
            "remaining_issues": remaining_issues,
        }
        PhaseOutputManager(output_dir).write_json(
            "pagegen",
            "debug",
            f"page_fix_issues/{self._page_fix_debug_filename(getattr(page, 'filename', ''))}",
            payload,
            producer="frontend.html_generator",
        )

    @staticmethod
    def _blocking_page_contract_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [issue for issue in issues or [] if isinstance(issue, dict)]

    @classmethod
    def _raise_for_blocking_page_contract_issues(
        cls,
        filename: str,
        issues: List[Dict[str, Any]],
        candidate_page: Optional[GeneratedPage] = None,
    ) -> None:
        blocking = cls._blocking_page_contract_issues(issues)
        if not blocking:
            return
        if candidate_page is None:
            candidate_page = GeneratedPage(
                filename=filename,
                html_content="",
                css_content="",
                fix_issues=list(issues or []),
            )
        raise PageGenContractValidationError(
            filename=filename,
            issues=blocking,
            candidate_page=candidate_page,
        )

    async def generate_pages_async(self,
                                  page_designs: List[Dict[str, Any]],
                                  website_type: str,
                                  entity_schema: Dict[str, Any],
                                  interfaces: Dict[str, List[Dict[str, Any]]],
                                  page_layouts: Dict[str, Any],
                                  page_framework: Dict[str, Any],
                                  design_analysis: Dict[str, Any] = None,
                                  architecture_pages: Dict[str, Any] = None,
                                  page_evidence_map: Dict[str, Any] = None,
                                  site_model=None,
                                  extra_page_data: Dict[str, Dict] = None,
                                  output_dir: str = None,
                                  task_elements: Dict[str, Any] = None,
                                  content_samples: Dict[str, Any] = None,
                                  pagegen_logic_flow: str = "standard",
                                  cascade_page_inputs: Dict[str, Dict[str, Any]] = None,
                                  pagegen_runtime_config: Dict[str, Any] = None,
                                  page_postprocess: Optional[Callable[[str, str], str]] = None) -> List[GeneratedPage]:
        if site_model is not None:
            page_evidence_map = self._build_page_evidence_map(site_model, architecture_pages)
        self._active_output_dir = output_dir
        self.logger.start_stage("Generate Pages")
        self.logger.log_info(f"📄 Generating {len(page_designs)} pages in parallel...")


        jobs = []
        for input_index, page_design in enumerate(page_designs):

            if hasattr(page_design, 'filename'):
                filename = page_design.filename
            else:
                filename = page_design.get('filename', '')


            page_interfaces = interfaces.get(filename, [])

            page_architecture = architecture_pages.get(filename, {}) if architecture_pages else {}

            page_evidence = (page_evidence_map or {}).get(filename, {})
            page_layout = (page_layouts or {}).get(filename)

            if extra_page_data and filename in extra_page_data:
                page_evidence = dict(page_evidence)
                page_evidence.update(extra_page_data[filename])

            page_entity_schema = self._filter_entity_schema_for_page(entity_schema, page_interfaces, page_evidence)

            page_extracted = (page_evidence or {}).get("extracted_items", {})
            if page_extracted and any(k != '_metadata' for k in page_extracted):
                page_content_samples = {k: v for k, v in page_extracted.items() if k != '_metadata'}
            else:
                page_content_samples = None

            task_elements_for_page = (task_elements or {}).get(filename, None)
            cascade_page_input = self._normalize_cascade_page_input(
                filename,
                (cascade_page_inputs or {}).get(filename),
                page_evidence,
            )
            page_evidence = self._merge_cascade_scope_into_page_evidence(
                page_evidence,
                cascade_page_input,
            )
            extra_page = (extra_page_data or {}).get(filename, {})
            schedule = self._compute_pagegen_scheduling_weight(
                filename,
                page_design,
                page_interfaces,
                page_evidence,
                task_elements_for_page,
                page_content_samples,
                page_layout,
                extra_page,
            )
            pagegen_decision = self._build_pagegen_budget_decision(
                requested_logic_flow=pagegen_logic_flow,
                cascade_page_input=cascade_page_input,
                schedule=schedule,
                page_evidence=page_evidence,
                page_resources=extra_page.get("page_resources", []),
                runtime_config=pagegen_runtime_config or {},
            )
            schedule["pagegen_decision"] = pagegen_decision
            generation_page_evidence = page_evidence
            generation_page_resources = extra_page.get("page_resources", [])
            pagegen_execution_flow = self._resolve_pagegen_execution_flow(pagegen_decision)
            if pagegen_execution_flow != "cascade":
                dedup_resources = extra_page.get("dedup_page_resources")
                if isinstance(dedup_resources, list):
                    generation_page_evidence = dict(page_evidence)
                    generation_page_evidence["page_resources"] = dedup_resources
                    generation_page_evidence["dedup_page_resources"] = dedup_resources
                    generation_page_evidence["dedup_resource_count"] = len(dedup_resources)
                    generation_page_resources = dedup_resources
                    if extra_page.get("dedup_resource_list_text") is not None:
                        generation_page_evidence["resource_list_text"] = extra_page.get("dedup_resource_list_text")
                    if isinstance(generation_page_evidence.get("pagegen_input_scope"), dict):
                        scoped = dict(generation_page_evidence["pagegen_input_scope"])
                        scoped["page_resources"] = dedup_resources
                        scoped["dedup_resource_count"] = len(dedup_resources)
                        generation_page_evidence["pagegen_input_scope"] = scoped

            jobs.append({
                "input_index": input_index,
                "filename": filename,
                "page_design": page_design,
                "page_entity_schema": page_entity_schema,
                "page_interfaces": page_interfaces,
                "page_architecture": page_architecture,
                "page_evidence": generation_page_evidence,
                "task_elements_for_page": task_elements_for_page,
                "page_content_samples": page_content_samples,
                "page_layout": page_layout,
                "cascade_page_input": cascade_page_input,
                "page_resources": generation_page_resources,
                "pagegen_decision": pagegen_decision,
                "schedule": schedule,
            })

        jobs = self._sort_pagegen_jobs(jobs)
        schedule_debug = []
        for rank, job in enumerate(jobs, start=1):
            entry = dict(job["schedule"])
            entry["rank"] = rank
            entry["input_index"] = job["input_index"]
            job["schedule"]["rank"] = rank
            schedule_debug.append(entry)
        self._write_pagegen_schedule_debug(output_dir, schedule_debug)
        if output_dir:
            from trace2world_pipeline.page_realizer.pagegen_prompt_boundary import validate_pagegen_prompt_boundary
            boundary_violations = []
            for job in jobs:
                ev = job.get("page_evidence") if isinstance(job.get("page_evidence"), dict) else {}
                scope = ev.get("pagegen_input_scope") if isinstance(ev.get("pagegen_input_scope"), dict) else {}
                payload = {
                    "page_environment_spec": ev.get("page_environment_projection") or ev.get("page_environment_spec") or {},
                    "pagegen_resource_projection": ev.get("pagegen_resource_projection") or {},
                    "interface_requirement_projection": ev.get("interface_requirement_projection") or {},
                    "route_affordances_final": ev.get("route_affordances_final") or {},
                    "route_action_slots": scope.get("route_action_slots", []),
                    "action_slot_route_contract": scope.get("action_slot_route_contract", {}),
                    "spec_execution_view": scope.get("spec_execution_view", {}),
                    "render_context_ref": scope.get("render_context_ref", {}),
                    "environment_prompt_policy": scope.get("environment_prompt_policy", {}),
                }
                report = validate_pagegen_prompt_boundary(payload)
                for violation in report.get("violations", []) or []:
                    if isinstance(violation, dict):
                        item = dict(violation)
                        item["filename"] = job.get("filename", "")
                        boundary_violations.append(item)
            boundary_report = {
                "format": "pagegen_prompt_boundary_validation",
                "schema_revision": 1,
                "page_count": len(jobs),
                "blocking_error_count": len(boundary_violations),
                "violations": boundary_violations,
            }
            PhaseOutputManager(output_dir).write_json(
                "pagegen",
                "debug",
                "pagegen_prompt_boundary_validation.json",
                boundary_report,
                producer="frontend.html_generator",
            )
            if boundary_violations:
                raise ValueError(
                    "PageGen prompt boundary validation failed: "
                    f"{len(boundary_violations)} debug/legacy input leak(s)"
                )
        if schedule_debug:
            top = ", ".join("%s:%s" % (item["filename"], item.get("scheduling_weight", 0)) for item in schedule_debug[:8])
            self.logger.log_info(f"Pagegen schedule reranked by scheduling weight; top pages: {top}")

        generated_pages = []
        generated_filenames = set()

        def _write_page_artifact(page: GeneratedPage) -> str:
            if not output_dir or not page or not page.html_content:
                return ""
            page.html_content = self._normalize_page_html_content(page.html_content)
            if page_postprocess:
                page.html_content = page_postprocess(page.filename, page.html_content)
            pages_output_dir = self._frontend_pages_output_dir(output_dir)
            os.makedirs(pages_output_dir, exist_ok=True)
            html_path = os.path.join(pages_output_dir, page.filename)
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(self._rewrite_page_paths_for_frontend_pages(page.html_content))
            if page.css_content:
                css_path = os.path.join(pages_output_dir, page.filename.replace('.html', '.css'))
                with open(css_path, 'w', encoding='utf-8') as f:
                    f.write(self._rewrite_css_paths_for_frontend_pages(page.css_content))
            self._write_page_fix_debug(output_dir, page)
            return html_path

        async def _save_generated_page(page: GeneratedPage) -> None:
            if not page or not page.filename:
                raise RuntimeError("PageGen worker returned an empty page artifact")
            if page.filename in generated_filenames:
                raise RuntimeError(f"PageGen produced duplicate page artifact: {page.filename}")
            generated_filenames.add(page.filename)
            generated_pages.append(page)

            if output_dir and page and page.html_content:
                _write_page_artifact(page)
                self.logger.log_info(f"💾 Saved {page.filename} to disk ({len(generated_pages)}/{len(jobs)})")

        async def _pagegen_worker(
            worker_index: int,
            attempt: int,
            job_queue: asyncio.Queue,
            failed_jobs: List[Dict[str, Any]],
            failure_reasons: Dict[str, str],
        ) -> None:
            while True:
                try:
                    job = job_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                filename = job.get("filename", "")
                flow = self._resolve_pagegen_execution_flow(
                    job.get("pagegen_decision"),
                    pagegen_logic_flow=pagegen_logic_flow,
                )
                rank = job.get("schedule", {}).get("rank")
                if self.logger:
                    self.logger.log_info(
                        f"Pagegen shared queue dispatch worker={worker_index} "
                        f"attempt={attempt} rank={rank} flow={flow} page={filename}"
                    )
                try:
                    page = await self._generate_single_page_by_logic_flow_async(
                        job["page_design"],
                        website_type,
                        job["page_entity_schema"],
                        job["page_interfaces"],
                        job["page_architecture"],
                        job["page_evidence"],
                        job["task_elements_for_page"],
                        job["page_content_samples"],
                        job["page_layout"],
                        pagegen_logic_flow=pagegen_logic_flow,
                        cascade_page_input=job["cascade_page_input"],
                        page_resources=job["page_resources"],
                        output_dir=output_dir,
                        pagegen_runtime_config=pagegen_runtime_config,
                        pagegen_decision=job["pagegen_decision"],
                    )
                    await _save_generated_page(page)
                except PageGenContractValidationError as exc:
                    failure_reasons[filename] = str(exc)
                    failed_jobs.append(job)
                    if self.logger:
                        self.logger.log_error(
                            f"Pagegen attempt {attempt} rejected {filename}: {exc}; "
                            "fix the PageGen source/prompt or real visible control"
                        )
                except Exception as exc:
                    failure_reasons[filename] = str(exc)
                    failed_jobs.append(job)
                    if self.logger:
                        self.logger.log_error(
                            f"Pagegen attempt {attempt} failed for {filename}: {exc}"
                        )
                finally:
                    job_queue.task_done()

        pending_jobs = list(jobs)
        max_attempts = self._resolve_pagegen_max_attempts()
        retry_backoff_seconds = self._resolve_pagegen_retry_backoff_seconds()
        failure_reasons: Dict[str, str] = {}
        for attempt in range(1, max_attempts + 1):
            if not pending_jobs:
                break
            job_queue: asyncio.Queue = asyncio.Queue()
            for job in pending_jobs:
                if job.get("filename") not in generated_filenames:
                    job_queue.put_nowait(job)
            pending_count = job_queue.qsize()
            if pending_count <= 0:
                pending_jobs = []
                break
            worker_count = self._resolve_pagegen_worker_count(pending_count)
            if self.logger:
                self.logger.log_info(
                    f"Pagegen attempt {attempt}/{max_attempts}: "
                    f"pending_pages={pending_count}, workers={worker_count}, "
                    f"global_max_concurrent={self.max_concurrent}"
                )
            failed_jobs: List[Dict[str, Any]] = []
            tasks = [
                asyncio.create_task(
                    _pagegen_worker(index + 1, attempt, job_queue, failed_jobs, failure_reasons)
                )
                for index in range(worker_count)
            ]
            worker_results = await asyncio.gather(*tasks, return_exceptions=True)
            worker_errors = [result for result in worker_results if isinstance(result, Exception)]
            if worker_errors:
                raise RuntimeError(
                    "PageGen worker loop failed before recording page-level failures: "
                    + "; ".join(str(error) for error in worker_errors[:3])
                )
            pending_jobs = [
                job for job in failed_jobs
                if job.get("filename") not in generated_filenames
            ]
            if pending_jobs and attempt < max_attempts:
                missing = ", ".join(str(job.get("filename")) for job in pending_jobs[:8])
                if self.logger:
                    self.logger.log_warning(
                        f"Pagegen will retry {len(pending_jobs)} failed page(s) after "
                        f"{retry_backoff_seconds:.1f}s: {missing}"
                    )
                if retry_backoff_seconds > 0:
                    await asyncio.sleep(retry_backoff_seconds)

        missing_jobs = [
            job for job in jobs
            if job.get("filename") not in generated_filenames
        ]
        if missing_jobs:
            details = [
                {
                    "filename": job.get("filename"),
                    "last_error": failure_reasons.get(str(job.get("filename")), "unknown"),
                }
                for job in missing_jobs
            ]
            raise RuntimeError(
                "PageGen incomplete after bounded retry rounds; missing_pages="
                + json.dumps(details, ensure_ascii=False)
            )

        self.logger.log_info(f"✅ Successfully generated {len(generated_pages)} pages")
        self.logger.end_stage("Generate Pages")

        return generated_pages

    @staticmethod
    def _is_transient_component(c) -> bool:
        if isinstance(c, dict):
            return bool(c.get("is_transient", False))
        return bool(getattr(c, "is_transient", False))

    def _build_sprite_reference_labels(
        self,
        icon_sprite: str = "",
        photo_sprite: str = "",
        asset_sprite: str = "",
    ) -> List[str]:
        labels: List[str] = []
        has_icon = bool(icon_sprite and os.path.exists(icon_sprite))
        has_photo = bool(photo_sprite and os.path.exists(photo_sprite))

        if has_icon:
            labels.append("Icon sprite sheet — semantic sprite_group=icon resources, each cell labeled [index] matching the resource list")
        if has_photo:
            labels.append("Photo sprite sheet — semantic sprite_group=photo resources, each cell labeled [index]")
        return labels

    @staticmethod
    def _pagegen_resource_text_has_exact_image_paths(page_evidence: Dict[str, Any] | None) -> bool:
        if not isinstance(page_evidence, dict):
            return False
        resource_text_candidates = [
            page_evidence.get("resource_list_text", ""),
            page_evidence.get("dedup_resource_list_text", ""),
        ]
        projection = page_evidence.get("pagegen_resource_projection")
        if isinstance(projection, dict):
            resource_text_candidates.append(projection.get("resource_list_text", ""))

        resource_text = "\n".join(str(item or "") for item in resource_text_candidates)
        return bool(
            re.search(
                r"(?<![A-Za-z0-9_./-])(?:\.\./)?images/[A-Za-z0-9][A-Za-z0-9_.@-]*\.(?:png|jpe?g|webp|gif|svg)\b",
                resource_text,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _build_sprite_reference_text(
        cls,
        sprite_labels: List[str],
        page_evidence: Dict[str, Any] | None,
    ) -> str:
        if not sprite_labels:
            return ""
        if not cls._pagegen_resource_text_has_exact_image_paths(page_evidence):
            return (
                "\n\n## Visual Resource Reference Boundary\n"
                "No exact local resource image paths are authorized in the Resources section. "
                "Attached resource sprite indices are visual reference only and are not valid HTML paths. "
                "Do not use `images/[0]`, `../images/[0]`, or any bracket-only sprite label as an img src/href. "
                "For omitted symbols, controls, badges, and decorative icons, render with Lucide/Tailwind/CSS/text."
            )
        return (
            "\n\n## Visual Resource Reference\n"
            "The attached images are:\n"
            "- Images 1-N: Reference screenshots of the actual page (different scroll positions)\n"
            + "\n".join(f"- {label}" for label in sprite_labels)
            + "\nUse sprite sheets to see what each resource looks like. Match [index] to the exact resource path listed in the prompt."
        )

    def _build_page_evidence_map(self, site_model, architecture_pages=None) -> Dict[str, Any]:
        evidence_map: Dict[str, Any] = {}


        page_behaviors = site_model.structure.page_behaviors or {}


        old_to_new: Dict[str, str] = {}
        if architecture_pages:
            new_fns = set(architecture_pages.keys())
            for proto in site_model.structure.page_prototypes:
                if not proto.filename:
                    continue
                old_fn = proto.filename
                if old_fn in new_fns:
                    old_to_new[old_fn] = old_fn
                else:
                    for new_fn in new_fns:
                        if new_fn.endswith(old_fn):
                            old_to_new[old_fn] = new_fn
                            break

        final_page_inputs = site_model.get_final_page_inputs()


        page_id_to_filename: Dict[str, str] = {}
        for page_input in final_page_inputs:
            page_id = page_input.get("page_id", "")
            filename = page_input.get("filename", "")
            if page_id and filename:
                page_id_to_filename[page_id] = old_to_new.get(filename, filename)


        outbound_edges: Dict[str, List[Dict]] = {}
        if not page_behaviors:
            for e in site_model.structure.site_graph.edges:
                pid = e.from_page_id
                if pid not in outbound_edges:
                    outbound_edges[pid] = []
                outbound_edges[pid].append({
                    "to_page": e.to_page_name or e.to_page_id,
                    "to_filename": page_id_to_filename.get(e.to_page_id, ""),
                    "trigger": e.trigger,
                    "to_route": e.to_route_path,
                })

        page_inputs_by_id = {
            str(page_input.get("page_id") or ""): page_input
            for page_input in final_page_inputs
            if isinstance(page_input, dict) and page_input.get("page_id")
        }
        page_inputs_by_filename = {
            str(page_input.get("filename") or ""): page_input
            for page_input in final_page_inputs
            if isinstance(page_input, dict) and page_input.get("filename")
        }

        for proto in site_model.structure.page_prototypes:
            if not proto.filename:
                continue
            page_input = page_inputs_by_id.get(str(proto.page_id or "")) or page_inputs_by_filename.get(str(proto.filename or ""))
            if not page_input:
                continue
            comps = []
            for idx, c in enumerate(proto.components or []):

                if self._is_transient_component(c):
                    continue
                if isinstance(c, dict):
                    comp = dict(c)
                    if not comp.get("component_id"):
                        comp["component_id"] = str(comp.get("id") or comp.get("name") or f"component_{idx}").lower().replace(" ", "_")
                elif isinstance(c, str) and c.strip():
                    text = c.strip()
                    comp = {"component_id": text.lower().replace(" ", "_"), "name": text, "type": "unknown", "description": text}
                else:
                    continue
                comp["locked"] = True
                comps.append(comp)

            prompt_screenshot_refs = page_input.get("prompt_screenshot_refs", proto.screenshot_refs)
            page_screenshot_refs = page_input.get("page_screenshot_refs", prompt_screenshot_refs)
            page_ev: Dict[str, Any] = {
                "page_id": page_input.get("page_id", proto.page_id),
                "page_name": page_input.get("page_name", proto.page_name),
                "page_type": page_input.get("page_type", proto.page_type),
                "filename": page_input.get("filename", proto.filename),
                "page_screenshot_refs": page_screenshot_refs,
                "prompt_screenshot_refs": prompt_screenshot_refs,
                "initial_layout_screenshots": page_input.get("initial_layout_screenshots", []),
                "state_transition_screenshots": page_input.get("state_transition_screenshots", []),
                "runtime_status_screenshots": page_input.get("runtime_status_screenshots", []),
                "unknown_render_role_screenshots": page_input.get("unknown_render_role_screenshots", []),
                "initial_visual_authority": page_input.get("initial_visual_authority", {}),
                "non_prompt_page_screenshot_refs": page_input.get("non_prompt_page_screenshot_refs", []),
                "unique_prompt_screenshot_refs": page_input.get("unique_prompt_screenshot_refs", prompt_screenshot_refs),
                "found": True,
            }
            if comps:
                page_ev["components"] = comps
            if proto.description:
                page_ev["description"] = proto.description
            if proto.observed_urls:
                page_ev["observed_urls"] = proto.observed_urls
            cd = proto.content_data or {}
            extracted = cd.get("extracted_items", {})
            if extracted:
                page_ev["extracted_items"] = extracted
            entity_component_mapping = cd.get("entity_component_mapping", [])
            if entity_component_mapping:
                page_ev["entity_component_mapping"] = entity_component_mapping
            entity_task_relevance = cd.get("entity_task_relevance", {})
            if entity_task_relevance:
                page_ev["entity_task_relevance"] = entity_task_relevance


            sd = proto.structure_data if hasattr(proto, "structure_data") else None
            page_ev["visual_layout_text"] = sd.get("visual_layout_text", "") if isinstance(sd, dict) else ""
            if isinstance(sd, dict) and isinstance(sd.get("external_ui_regions"), list):
                page_ev["external_ui_regions"] = sd.get("external_ui_regions", [])


            behavior = page_behaviors.get(proto.page_id)
            if behavior and isinstance(behavior, dict):
                nav_out = behavior.get("navigation_out", [])
                if nav_out:

                    if old_to_new:
                        nav_out = [
                            {**n, "to_filename": old_to_new.get(n.get("to_filename", ""), n.get("to_filename", ""))}
                            if isinstance(n, dict) and "to_filename" in n else n
                            for n in nav_out
                        ]
                    page_ev["outbound_edges"] = nav_out
                nav_in = behavior.get("navigation_in", [])
                if nav_in:
                    if old_to_new:
                        nav_in = [
                            {**n, "from_filename": old_to_new.get(n.get("from_filename", ""), n.get("from_filename", ""))}
                            if isinstance(n, dict) and "from_filename" in n else n
                            for n in nav_in
                        ]
                    page_ev["inbound_edges"] = nav_in
                route_params = behavior.get("route_params", [])
                if route_params:
                    page_ev["route_params"] = route_params
                page_actions = behavior.get("page_actions", [])
                if page_actions:
                    page_ev["page_actions"] = page_actions
                task_continuations = behavior.get("task_continuations", [])
                if task_continuations:
                    page_ev["task_continuations"] = task_continuations
                interaction_policy = behavior.get("interaction_policy", {})
                if interaction_policy:
                    page_ev["interaction_policy"] = interaction_policy
                component_contracts = behavior.get("component_contracts", [])
                if component_contracts:
                    page_ev["component_contracts"] = component_contracts
                floating_component = behavior.get("floating_component", {})
                if floating_component:
                    page_ev["floating_component"] = floating_component

                global_nav = site_model.structure.global_navigation or {}
                if global_nav:
                    page_ev["global_nav"] = global_nav
            else:
                edges = outbound_edges.get(proto.page_id, [])
                if edges:
                    page_ev["outbound_edges"] = edges

            if page_ev.get("components") or page_ev.get("extracted_items") or page_ev.get("outbound_edges"):

                map_key = old_to_new.get(proto.filename, proto.filename)
                page_ev["filename"] = map_key
                page_ev["site_pages"] = [
                    {
                        "page_id": p.page_id,
                        "page_name": p.page_name,
                        "filename": old_to_new.get(p.filename, p.filename),
                    }
                    for p in site_model.structure.page_prototypes
                    if p.filename
                ]
                evidence_map[map_key] = page_ev
        return evidence_map

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    def _collect_identifier_param_aliases(self, page_interfaces: List[Dict[str, Any]]) -> List[str]:
        aliases = set()
        for iface in page_interfaces or []:
            params = iface.get("parameters", [])
            if not isinstance(params, list):
                continue
            for param in params:
                if not isinstance(param, dict):
                    continue
                name = str(param.get("name", "") or "").strip()
                if not name:
                    continue
                has_entity_ref = isinstance(param.get("entityReference"), dict)
                is_identifier_name = name == "id" or name.endswith("Id") or name.endswith("_id")
                if not (has_entity_ref or is_identifier_name):
                    continue
                aliases.add(name)
                aliases.add(self._camel_to_snake(name))
                if name != "mode":
                    aliases.add("id")
        return sorted(alias for alias in aliases if alias)

    def _page_task_interaction_contract_payload(self, page_evidence: Dict[str, Any]) -> Dict[str, Any]:
        contracts = []
        for item in (page_evidence or {}).get("task_continuations", []) or []:
            if not isinstance(item, dict):
                continue
            contract = item.get("interaction_contract", {})
            if not isinstance(contract, dict):
                continue
            payload = dict(contract)
            if not payload.get("contract_id") and item.get("candidate_id"):
                payload["contract_id"] = item.get("candidate_id")
            contracts.append(payload)
        return {"contracts": contracts}

    def _filter_entity_schema_for_page(self, entity_schema: Dict[str, Any],
                                    page_interfaces: List[Dict[str, Any]],
                                    page_evidence: Dict[str, Any] = None) -> Dict[str, Any]:
        if not entity_schema:
            return entity_schema or {}


        entities_list = entity_schema.get("entities", None)
        is_list_format = isinstance(entities_list, list)


        entity_entries: Dict[str, Any] = {}
        key_lookup: Dict[str, str] = {}

        if is_list_format:
            for entity_def in entities_list:
                if not isinstance(entity_def, dict):
                    continue
                name = entity_def.get("name", "")
                storage_key = entity_def.get("storage_key", "")
                if not name:
                    continue
                canonical = name
                entity_entries[canonical] = entity_def
                for variant in (name, name.lower(), self._camel_to_snake(name),
                                storage_key, storage_key.lower()):
                    if variant:
                        key_lookup[variant] = canonical
        else:
            for key in entity_schema:
                if key.startswith("_"):
                    continue
                entity_entries[key] = entity_schema[key]
                for variant in (key, key.lower(), self._camel_to_snake(key)):
                    key_lookup[variant] = key

        total_entities = len(entity_entries)
        matched_keys = set()


        if page_interfaces:
            for iface in page_interfaces:
                iface_str = json.dumps(iface, ensure_ascii=False)
                for canonical in entity_entries:
                    if canonical in matched_keys:
                        continue

                    if re.search(r'(?<![a-zA-Z])' + re.escape(canonical) + r'(?![a-z])', iface_str):
                        matched_keys.add(canonical)
                        continue

                    snake = self._camel_to_snake(canonical)
                    if snake and snake in iface_str.lower():
                        matched_keys.add(canonical)


        if page_evidence:
            extracted_items = page_evidence.get("extracted_items", {})
            for type_name in extracted_items.keys():
                if type_name == '_metadata':
                    continue
                for variant in (type_name, self._camel_to_snake(type_name), type_name.lower()):
                    canonical = key_lookup.get(variant)
                    if canonical:
                        matched_keys.add(canonical)
                        break

        if matched_keys:
            self.logger.log_info(f"    Entity schema filtered: {total_entities} → {len(matched_keys)} entities for page")
            return self._rebuild_entity_schema(entity_schema, is_list_format, entity_entries, matched_keys)


        return entity_schema

    @staticmethod
    def _rebuild_entity_schema(entity_schema: Dict[str, Any], is_list_format: bool,
                               entity_entries: Dict[str, Any], matched_keys: set) -> Dict[str, Any]:
        if is_list_format:
            filtered_entities = [entity_entries[k] for k in matched_keys if k in entity_entries]
            result = {"entities": filtered_entities}
            if "relationships" in entity_schema:
                result["relationships"] = entity_schema["relationships"]
            if "metadata" in entity_schema:
                result["metadata"] = entity_schema["metadata"]
            return result
        else:
            filtered = {k: entity_entries[k] for k in matched_keys if k in entity_entries}
            if '_metadata' in entity_schema:
                filtered['_metadata'] = entity_schema['_metadata']
            return filtered


    async def _generate_single_page_async(self,
                                         page_design: Any,
                                         website_type: str,
                                         entity_schema: Dict[str, Any],
                                         page_interfaces: List[Dict[str, Any]],
                                         page_architecture: Dict[str, Any],
                                         page_evidence: Dict[str, Any],
                                         task_elements_for_page=None,
                                         content_samples: Dict[str, Any] = None,
                                         page_layout: Dict[str, Any] = None) -> GeneratedPage:
        return await self._generate_single_page_inner_async(
            page_design,
            website_type,
            entity_schema,
            page_interfaces,
            page_architecture,
            page_evidence,
            task_elements_for_page,
            content_samples,
            page_layout,
        )

    async def _generate_single_page_by_logic_flow_async(self,
                                                         page_design: Any,
                                                         website_type: str,
                                                         entity_schema: Dict[str, Any],
                                                         page_interfaces: List[Dict[str, Any]],
                                                         page_architecture: Dict[str, Any],
                                                         page_evidence: Dict[str, Any],
                                                         task_elements_for_page=None,
                                                         content_samples: Dict[str, Any] = None,
                                                         page_layout: Dict[str, Any] = None,
                                                         pagegen_logic_flow: str = "standard",
                                                         cascade_page_input: Dict[str, Any] = None,
                                                         page_resources: List[Dict[str, Any]] = None,
                                                         output_dir: str = None,
                                                         pagegen_runtime_config: Dict[str, Any] = None,
                                                         pagegen_decision: Dict[str, Any] = None,
                                                         **_unused_kwargs) -> GeneratedPage:
        pagegen_execution_flow = self._resolve_pagegen_execution_flow(
            pagegen_decision,
            pagegen_logic_flow=pagegen_logic_flow,
        )
        if pagegen_decision:
            page_evidence = dict(page_evidence or {})
            page_evidence["pagegen_decision"] = pagegen_decision
        if pagegen_execution_flow == "cascade" and cascade_page_input:
            return await self._generate_single_page_cascade_async(
                page_design,
                website_type,
                entity_schema,
                page_interfaces,
                page_architecture,
                page_evidence,
                task_elements_for_page,
                content_samples,
                page_layout,
                cascade_page_input,
                page_resources or [],
                output_dir,
                pagegen_runtime_config,
            )
        return await self._generate_single_page_inner_async(
            page_design,
            website_type,
            entity_schema,
            page_interfaces,
            page_architecture,
            page_evidence,
            task_elements_for_page,
            content_samples,
            page_layout,
        )

    async def _generate_single_page_inner_async(self,
                                               page_design: Any,
                                               website_type: str,
                                               entity_schema: Dict[str, Any],
                                               page_interfaces: List[Dict[str, Any]],
                                               page_architecture: Dict[str, Any],
                                               page_evidence: Dict[str, Any],
                                               task_elements_for_page=None,
                                               content_samples: Dict[str, Any] = None,
                                               page_layout: Dict[str, Any] = None) -> GeneratedPage:
        if hasattr(page_design, 'filename'):
            filename = page_design.filename
        else:
            filename = page_design.get('filename', 'page.html')
        self.logger.log_info(f"  Generating page: {filename}")


        prompt = self._build_html_prompt_tailwind(
            page_design,
            website_type,
            entity_schema,
            page_interfaces,
            page_architecture,
            page_evidence,
            task_elements_for_page,
            content_samples,
            page_layout
        )
        html_content = await self._generate_html_async(
            page_design,
            prompt,
            page_evidence,
        )

        fix_rounds = 0
        fix_issues: List[Dict[str, Any]] = []
        html_content, fix_issues, fix_rounds = await self._audit_or_repair_page_contracts(
            filename=filename,
            prompt=prompt,
            html_content=html_content,
            page_evidence=page_evidence,
            page_architecture=page_architecture,
            page_interfaces=page_interfaces,
            task_elements_for_page=task_elements_for_page,
            page_name=filename,
        )
        html_content = self._normalize_page_html_content(html_content)
        candidate_page = GeneratedPage(
            filename=filename,
            html_content=html_content,
            css_content="",
            fix_issues=fix_issues,
            fix_rounds=fix_rounds,
        )
        self._raise_for_blocking_page_contract_issues(filename, fix_issues, candidate_page)


        entity_keys = []
        if page_evidence:
            extracted = page_evidence.get('extracted_items', {})
            if extracted:
                entity_keys.extend(extracted.keys())
        if not entity_keys and page_interfaces:
            for iface in page_interfaces:
                iface_str = json.dumps(iface.get('returns', {}), ensure_ascii=False)
                iface_str += json.dumps(iface.get('parameters', []), ensure_ascii=False)
                for match in re.findall(r'"items":\s*"(\w+)"', iface_str):
                    if match not in entity_keys:
                        entity_keys.append(match)
                for match in re.findall(r'"type":\s*"(\w+)"', iface_str):
                    if match not in ('string', 'number', 'boolean', 'array', 'object', 'integer', 'null') and match not in entity_keys:
                        entity_keys.append(match)

        return GeneratedPage(
            filename=filename,
            html_content=html_content,
            css_content="",
            entity_keys=entity_keys,
            fix_issues=fix_issues,
            fix_rounds=fix_rounds,
        )

    async def _generate_single_page_cascade_async(self,
                                                  page_design: Any,
                                                  website_type: str,
                                                  entity_schema: Dict[str, Any],
                                                  page_interfaces: List[Dict[str, Any]],
                                                  page_architecture: Dict[str, Any],
                                                  page_evidence: Dict[str, Any],
                                                  task_elements_for_page=None,
                                                  content_samples: Dict[str, Any] = None,
                                                  page_layout: Dict[str, Any] = None,
                                                  cascade_page_input: Dict[str, Any] = None,
                                                  page_resources: List[Dict[str, Any]] = None,
                                                  output_dir: str = None,
                                                  pagegen_runtime_config: Dict[str, Any] = None) -> GeneratedPage:
        if hasattr(page_design, 'filename'):
            filename = page_design.filename
        else:
            filename = page_design.get('filename', 'page.html')
        self.logger.log_info(f"  Generating page via cascade: {filename}")

        runtime_config = CascadeRuntimeConfig(**(pagegen_runtime_config or {}))

        def _round1_prompt_builder(round_page_evidence: Dict[str, Any]) -> str:
            page_entity_schema = self._filter_entity_schema_for_page(entity_schema, page_interfaces, round_page_evidence)
            page_extracted = (round_page_evidence or {}).get("extracted_items", {})
            page_content_samples = {k: v for k, v in page_extracted.items() if k != '_metadata'} if page_extracted and any(k != '_metadata' for k in page_extracted) else None
            return self._build_html_prompt_tailwind(
                page_design,
                website_type,
                page_entity_schema,
                page_interfaces,
                page_architecture,
                round_page_evidence,
                task_elements_for_page,
                page_content_samples,
                page_layout,
            )

        html_content, cascade_report = await generate_page_cascade_async(
            page_design=page_design,
            page_evidence=page_evidence,
            cascade_page_input=cascade_page_input or {},
            page_resources=page_resources or [],
            output_dir=output_dir,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            round1_prompt_builder=_round1_prompt_builder,
            logger=self.logger,
            runtime_config=runtime_config,
            prompt_policy=self.repair_prompt_policy,
        )

        fix_rounds = 0
        fix_issues: List[Dict[str, Any]] = []
        prompt = _round1_prompt_builder(page_evidence)
        html_content, fix_issues, fix_rounds = await self._audit_or_repair_page_contracts(
            filename=filename,
            prompt=prompt,
            html_content=html_content,
            page_evidence=page_evidence,
            page_architecture=page_architecture,
            page_interfaces=page_interfaces,
            task_elements_for_page=task_elements_for_page,
            page_name=filename,
        )
        html_content = self._normalize_page_html_content(html_content)
        candidate_page = GeneratedPage(
            filename=filename,
            html_content=html_content,
            css_content="",
            fix_issues=fix_issues,
            fix_rounds=fix_rounds,
            metadata={"cascade_report": cascade_report},
        )
        self._raise_for_blocking_page_contract_issues(filename, fix_issues, candidate_page)

        entity_keys = []
        if page_evidence:
            extracted = page_evidence.get('extracted_items', {})
            if extracted:
                entity_keys.extend(extracted.keys())
        if not entity_keys and page_interfaces:
            for iface in page_interfaces:
                iface_str = json.dumps(iface.get('returns', {}), ensure_ascii=False)
                iface_str += json.dumps(iface.get('parameters', []), ensure_ascii=False)
                for match in re.findall(r'"items":\s*"(\w+)"', iface_str):
                    if match not in entity_keys:
                        entity_keys.append(match)
                for match in re.findall(r'"type":\s*"(\w+)"', iface_str):
                    if match not in ('string', 'number', 'boolean', 'array', 'object', 'integer', 'null') and match not in entity_keys:
                        entity_keys.append(match)
        if cascade_report and self.logger:
            self.logger.log_info(f"  Cascade rounds for {filename}: {len(cascade_report.get('rounds', []))}")

        return GeneratedPage(
            filename=filename,
            html_content=html_content,
            css_content="",
            entity_keys=entity_keys,
            fix_issues=fix_issues,
            fix_rounds=fix_rounds,
            metadata={"cascade_report": cascade_report or {}},
        )

    @staticmethod
    def _html_generation_stability_fewshot() -> str:
        return """

## HTML Generation Stability Fewshot
These examples prevent recurring syntax and contract failures. Follow the GOOD patterns.

BAD mixed quote HTML:
button.innerHTML =
  `<span class="icon"></span>' +
  '<span>' + escapeHtml(text) + '</span>';
suggestionList.innerHTML = '`;

GOOD string concat:
button.innerHTML =
  '<span class="icon"></span>' +
  '<span>' + escapeHtml(text) + '</span>';

GOOD empty clear:
suggestionList.innerHTML = '';

GOOD multiline HTML:
panel.innerHTML = `
  <div class="row">
    <span>${escapeHtml(label)}</span>
  </div>
`;

Query-state rule:
If a query-state runtime contract or page_environment_spec commits query="肯德基", any visible search input value and JavaScript fallback must use that committed query. Do not keep stale observed values such as value="黄焖鸡米饭" on the target result page.

Collection rule:
Autocomplete, suggestions, and results containers may redraw their own derived rows with innerHTML = ''. Do not clear or replace preserved stateful collections from page_environment_spec unless the spec declares a clear/delete transition for that exact state_ref/component.

Resource rule:
If the Resources section lists only `images/asset_0306.jpg`, then `images/asset_0306.jpg` is the only valid local raster path. Do not reference remembered or nearby names such as `images/asset_0060.jpg` or `images/asset_0061.jpg`. Draw generic add/edit/check/arrow symbols with Lucide, Tailwind, CSS, or text unless the exact raster path is listed in Resources.

Return rule:
Return one complete HTML document only.
"""

    @classmethod
    def _append_html_generation_stability_fewshot(cls, prompt: str) -> str:
        prompt = prompt or ""
        marker = "## HTML Generation Stability Fewshot"
        if marker in prompt:
            return prompt
        return prompt + cls._html_generation_stability_fewshot()

    def _write_actual_prompt_manifest(
        self,
        *,
        page_name: str,
        prompt: str,
        messages: List[Dict[str, Any]],
        usage_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        output_dir = str(getattr(self, "_active_output_dir", "") or "")
        if not output_dir:
            return
        request_text_parts: List[str] = []
        image_count = 0
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                request_text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "input_text":
                        request_text_parts.append(str(block.get("text") or ""))
                    elif block.get("type") == "input_image":
                        image_count += 1
        request_text = "\n".join(request_text_parts)
        provider_prompt_tokens = None
        if isinstance(usage_info, dict):
            provider_prompt_tokens = usage_info.get("prompt_tokens") or usage_info.get("input_tokens")
        token_estimate = estimate_pagegen_message_input_tokens(messages)
        manifest = {
            "format": "actual_pagegen_prompt_manifest",
            "schema_revision": 1,
            "filename": page_name,
            "platform": self.environment_profile["platform"],
            "environment_profile_hash": self.environment_profile["profile_hash"],
            "environment_prompt_policy": prompt_policy_manifest(
                self.environment_profile,
                "pagegen_generation",
            ),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "request_text_sha256": hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
            "request_text_char_count": len(request_text),
            "input_image_count": image_count,
            "request_text_token_count": token_estimate.text_tokens,
            "text_tokenizer": PAGEGEN_TEXT_TOKENIZER,
            "estimated_input_tokens": token_estimate.estimated_input_tokens,
            "token_estimate_method": "tiktoken_text_plus_configured_image_and_request_overhead_proxies",
            "image_token_proxy": DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
            "request_overhead_token_proxy": DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
            "provider_prompt_tokens": provider_prompt_tokens,
            "platform_markers": {
                "phone_frame": "Phone Frame" in request_text,
                "mobile_viewport": "390x844" in request_text or "390×844" in request_text,
                "desktop_viewport": "1920x1080" in request_text or "1920×1080" in request_text,
                "desktop_mouse_keyboard": "mouse and keyboard" in request_text,
            },
        }
        target = (
            Path(output_dir)
            / "phase_pagegen"
            / "page_inputs"
            / Path(page_name).stem
            / "actual_pagegen_prompt_manifest.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    async def _generate_html_async(self,
                                  page_design: Any,
                                  prompt: str,
                                  page_evidence: Dict[str, Any] = None) -> str:
        prompt = prompt or ""
        prompt_with_stability_fewshot = self._append_html_generation_stability_fewshot(prompt)


        if hasattr(page_design, 'filename'):
            page_name = page_design.filename
        elif isinstance(page_design, dict):
            page_name = page_design.get('filename', 'unknown_page')
        else:
            page_name = 'unknown_page'


        call_id = None
        if self.logger:
            call_id = self.logger.log_api_call(
                f"Generate HTML - {page_name}",
                prompt_with_stability_fewshot,
                additional_args={"page": page_name},
                stage="Generate Pages"
            )


        evidence = page_evidence or {}
        scope = evidence.get("pagegen_input_scope") if isinstance(evidence.get("pagegen_input_scope"), dict) else {}
        prompt_refs = evidence.get("prompt_screenshot_refs") or evidence.get("screenshot_refs", [])
        source_host_entries = (
            evidence.get("source_host_visual_authority_screenshots")
            or scope.get("source_host_visual_authority_screenshots")
            or []
        )
        source_host_refs = []
        if isinstance(source_host_entries, list):
            for item in source_host_entries:
                if isinstance(item, dict):
                    ref = str(item.get("screenshot_ref") or "").strip()
                else:
                    ref = str(item or "").strip()
                if ref:
                    source_host_refs.append(ref)
        screenshot_refs = []
        seen_screenshot_refs = set()
        for ref in list(prompt_refs or []) + source_host_refs:
            ref = str(ref or "").strip()
            if ref and ref not in seen_screenshot_refs:
                seen_screenshot_refs.add(ref)
                screenshot_refs.append(ref)
        valid_screenshots = [p for p in screenshot_refs if os.path.exists(p)]
        if valid_screenshots:
            from trace2world_pipeline.core.llm_caller import encode_image, encode_image_downsampled
            from PIL import Image
            import io, base64

            user_content = []


            selected_screenshots = valid_screenshots
            screenshot_max_width = 800
            screenshot_max_height = None
            screenshot_quality = 75


            for idx, spath in enumerate(selected_screenshots):
                try:
                    img = Image.open(spath)
                    ratio = 1.0
                    if img.width > screenshot_max_width:
                        ratio = min(ratio, screenshot_max_width / img.width)
                    if screenshot_max_height and img.height > screenshot_max_height:
                        ratio = min(ratio, screenshot_max_height / img.height)
                    if ratio < 1.0:
                        img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=screenshot_quality, optimize=True)
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    image_url = f"data:image/jpeg;base64,{b64}"
                    user_content.append({"type": "input_image", "image_url": image_url})
                except Exception:
                    user_content.append({"type": "input_image", "image_url": encode_image(spath)})

            self.logger.log_info(
                f"  Sending {len(user_content)} screenshots for {page_name} "
                f"(available={len(valid_screenshots)}, "
                f"source_host_authority={len(source_host_refs)}, max_width={screenshot_max_width})"
            )


            icon_sprite = (page_evidence or {}).get("icon_sprite_path")
            photo_sprite = (page_evidence or {}).get("photo_sprite_path")
            asset_sprite = (page_evidence or {}).get("asset_sprite_path")
            sprite_labels = []
            attach_resource_sprites = self._pagegen_resource_text_has_exact_image_paths(page_evidence)
            sprite_max_size = 1280
            sprite_quality = 80
            if icon_sprite and os.path.exists(icon_sprite) and attach_resource_sprites:
                user_content.append({"type": "input_image", "image_url": encode_image_downsampled(icon_sprite, max_width=sprite_max_size, max_height=sprite_max_size, jpeg_quality=sprite_quality)})
                self.logger.log_info(f"  Sending icon sprite for {page_name}")
            if photo_sprite and os.path.exists(photo_sprite) and attach_resource_sprites:
                user_content.append({"type": "input_image", "image_url": encode_image_downsampled(photo_sprite, max_width=sprite_max_size, max_height=sprite_max_size, jpeg_quality=sprite_quality)})
                self.logger.log_info(
                    f"  Sending photo sprite for {page_name} "
                    f"(max_size={sprite_max_size})"
                )
            if asset_sprite and os.path.exists(asset_sprite) and attach_resource_sprites:
                self.logger.log_info(
                    f"  Skipping asset sprite for {page_name}: auxiliary resource sheet is not a PageGen visual input"
                )
            elif not attach_resource_sprites and any(
                sprite and os.path.exists(sprite)
                for sprite in (icon_sprite, photo_sprite, asset_sprite)
            ):
                self.logger.log_info(
                    f"  Skipping resource sprite for {page_name}: no exact image paths are authorized"
                )
            raw_sprite_labels = (
                self._build_sprite_reference_labels(icon_sprite, photo_sprite, asset_sprite)
                if attach_resource_sprites
                else []
            )
            if raw_sprite_labels:
                first_sprite_index = len(selected_screenshots) + 1
                sprite_labels = [
                    f"Image {first_sprite_index + idx}: {label}"
                    for idx, label in enumerate(raw_sprite_labels)
                ]


            sprite_ref = self._build_sprite_reference_text(sprite_labels, page_evidence)

            spec_screenshot_order_rule = ""
            if "Page Environment Spec (PAGEGEN INPUT)" in prompt:
                spec_screenshot_order_rule = (
                    "\n- Spec-authoritative mode: Do not choose a state from screenshot order. "
                    "Use page_environment_spec.screenshot_lifecycle_inputs.initial_layout_screenshots "
                    "and initial_visual_authority as the initial DOM authority; screenshots outside "
                    "initial_layout_screenshots describe hidden/runtime/post-action states only."
                )

            user_content.append({
                "type": "input_text",
                "text": self._append_html_generation_stability_fewshot(prompt + sprite_ref + f"""

## Screenshot Interpretation Rules
- Reproduce the visual design faithfully from the attached screenshots using Tailwind CSS classes.
{spec_screenshot_order_rule}
- Resource rule: only use local raster assets whose exact `images/<asset_name>` or `../images/<asset_name>` path is listed in the Resources section. Never use bracket-only sprite labels such as `images/[0]` or `../images/[0]` as HTML paths; draw omitted symbols with Lucide/Tailwind/CSS/text.
- If multiple screenshots show DIFFERENT layouts/forms/state-specific chrome for the same page (e.g., different business verticals or selected-state variants), render the FIRST screenshot as the default view. Use URL query params or existing state handles to switch variants — do NOT merge them into one page with toggle buttons.
- Source-host visual authority screenshots are scoped evidence for their listed source host label/affordance only. Do not treat them as page-global initial DOM authority unless they also appear in initial_layout_screenshots.
- Every target_element listed in Task Actions MUST be a clickable/interactive element in the generated HTML with the EXACT data-element-id. If an action says "click btn_X" or "tap item_Y", that element MUST exist and respond to clicks. Missing interactive elements = task failure.
- For spec-authoritative pages, only visible_text_contract.exact_text with observed_host_text/visual_slot_observed/exact_visible_label_required authority is a visible body-text obligation. accessible_label_must_include may be rendered as aria-label/data metadata only, and context_text_must_include belongs to nearby/ancestor context, not the host body. Semantic-only labels such as "top search box" must not replace visible text.
- trace_hit_points are approximate reference tap points for disambiguating the host inside the screenshot; do not render coordinates as text.
- Every visible button in the screenshot (back, close, submit, etc.) MUST be interactive in the HTML.""")
            })
            messages = [{"role": "user", "content": user_content}]
        else:
            messages = [{"role": "user", "content": prompt_with_stability_fewshot}]

        pagegen_decision = (
            evidence.get("pagegen_decision")
            if isinstance(evidence.get("pagegen_decision"), dict)
            else {}
        )
        input_budget_gate = (
            pagegen_decision.get("input_budget_gate")
            if isinstance(pagegen_decision.get("input_budget_gate"), dict)
            else {}
        )
        budget_thresholds = (
            input_budget_gate.get("thresholds")
            if isinstance(input_budget_gate.get("thresholds"), dict)
            else {}
        )
        input_token_budget = normalize_pagegen_input_token_budget(
            self._as_int(
                budget_thresholds.get("estimated_input_tokens"),
                DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET,
            ),
        )
        image_token_proxy = max(
            0,
            self._as_int(
                budget_thresholds.get("image_token_proxy"),
                DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
            ),
        )
        overhead_token_proxy = max(
            0,
            self._as_int(
                budget_thresholds.get("overhead_token_proxy"),
                DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
            ),
        )
        input_budget_disabled = os.getenv("T2W_PAGEGEN_DISABLE_INPUT_BUDGET") == "1"
        if not input_budget_disabled:
            enforce_pagegen_preflight_budget(
                messages,
                stage=f"generate_html:{page_name}",
                input_token_budget=input_token_budget,
                image_token_proxy=image_token_proxy,
                overhead_token_proxy=overhead_token_proxy,
            )

        self._write_actual_prompt_manifest(
            page_name=page_name,
            prompt=prompt,
            messages=messages,
        )

        def _record_and_enforce_provider_budget(
            usage: Optional[Dict[str, Any]],
            *,
            call_messages: List[Dict[str, Any]],
            stage: str,
        ) -> None:
            self._write_actual_prompt_manifest(
                page_name=page_name,
                prompt=prompt,
                messages=call_messages,
                usage_info=usage,
            )
            if not input_budget_disabled:
                enforce_pagegen_provider_budget(
                    usage,
                    stage=stage,
                    input_token_budget=input_token_budget,
                )

        try:
            result, usage_info = await call_openai_api_json_async(
                messages,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="generate_html",
            )
            _record_and_enforce_provider_budget(
                usage_info,
                call_messages=messages,
                stage=f"generate_html:{page_name}",
            )


            if self.logger:
                self.logger.log_api_response(
                    f"Generate HTML - {page_name}",
                    success=True,
                    response=result,
                    usage_info=usage_info,
                    stage="Generate Pages",
                    call_id=call_id
                )
        except Exception as e:
            try:
                from trace2world_pipeline.core.llm_caller import LLMJsonResponseError
            except Exception:
                LLMJsonResponseError = ()
            is_json_error = isinstance(e, json.JSONDecodeError) or (
                bool(LLMJsonResponseError) and isinstance(e, LLMJsonResponseError)
            )
            if self.logger:
                self.logger.log_error(f"Failed to generate HTML for {page_name}: {str(e)}")
                self.logger.log_api_response(
                    f"Generate HTML - {page_name}",
                    success=False,
                    error=str(e),
                    stage="Generate Pages",
                    call_id=call_id
                )
            if is_json_error:
                raise RuntimeError(
                    f"PageGen failed for {page_name}: JSON generation failed; "
                    "lossy text-only retry is disabled"
                ) from e
            else:
                raise

        if result is None:

            if self.logger:
                self.logger.log_warning(f"LLM returned None for {page_name}, retrying once...")
            try:
                if not input_budget_disabled:
                    enforce_pagegen_preflight_budget(
                        messages,
                        stage=f"generate_html_retry_none:{page_name}",
                        input_token_budget=input_token_budget,
                        image_token_proxy=image_token_proxy,
                        overhead_token_proxy=overhead_token_proxy,
                    )
                result, usage_info = await call_openai_api_json_async(
                    messages, model=self.model, reasoning_effort=self.reasoning_effort,
                    stage="generate_html_retry_none",
                )
                _record_and_enforce_provider_budget(
                    usage_info,
                    call_messages=messages,
                    stage=f"generate_html_retry_none:{page_name}",
                )
            except Exception:
                result = None
            if result is None:
                if self.logger:
                    self.logger.log_error(
                        f"LLM returned None for {page_name} after retry; PageGen cannot emit fallback HTML."
                    )
                raise RuntimeError(f"PageGen returned empty result for {page_name}")

        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                if self.logger:
                    self.logger.log_error(
                        f"JSON parse failed for {page_name}; raw model output is not a valid PageGen artifact."
                    )
                raise RuntimeError(f"PageGen returned non-JSON result for {page_name}")

        result_to_process = self._normalize_page_html_content(result.get("html_content", ""))


        result_to_process = result_to_process.replace('<link rel="stylesheet" href="styles.css">', '')

        return result_to_process

    def _collect_valid_filenames(self, filename: str, page_evidence: Dict[str, Any], page_architecture: Dict[str, Any]) -> List[str]:
        valid_fns = set()
        for e in (page_evidence or {}).get("outbound_edges", []):
            fn = e.get("to_filename", "") if isinstance(e, dict) else ""
            if fn and fn.endswith(".html"):
                valid_fns.add(fn)
        for page in (page_evidence or {}).get("site_pages", []):
            fn = page.get("filename", "") if isinstance(page, dict) else ""
            if fn and fn.endswith(".html"):
                valid_fns.add(fn)
        gnav = (page_evidence or {}).get("global_nav", {})
        for link in gnav.get("header_links", []) + gnav.get("footer_links", []):
            u = link.get("url", "")
            if u.endswith(".html"):
                valid_fns.add(u)
        if page_architecture:
            for conn in page_architecture.get("outgoing_connections", []):
                fn = conn.get("target", conn.get("filename", ""))
                if fn and fn.endswith(".html"):
                    valid_fns.add(fn)
        if filename:
            valid_fns.add(filename)
        return sorted(valid_fns)

    @staticmethod
    def _collect_render_view_valid_filenames(
        filename: str,
        render_view: Dict[str, Any],
    ) -> List[str]:
        valid_fns = {filename} if filename else set()
        if not isinstance(render_view, dict):
            return sorted(valid_fns)
        for section in ("action_obligations", "route_slots"):
            rows = render_view.get(section, [])
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                target = str(
                    row.get("target_filename")
                    or row.get("expected_filename")
                    or ""
                ).strip()
                if target.endswith(".html"):
                    valid_fns.add(target)
        return sorted(valid_fns)

    @staticmethod
    def _compact_interaction_contract_for_prompt(contract: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(contract, dict):
            return {}
        result: Dict[str, Any] = {}
        for key in (
            "contract_id",
            "action_semantic",
            "state_scope",
            "state_provenance",
            "source_lifecycle",
            "state_ref",
            "entity_ref",
            "runtime_state_observation_ids",
            "runtime_state_obligation_ids",
            "terminal_constraints",
        ):
            value = contract.get(key)
            if value not in (None, "", [], {}):
                result[key] = value
        for key in ("route_binding", "sdk_effect", "field_contract"):
            value = contract.get(key)
            if isinstance(value, dict) and any(v not in (None, "", [], {}, False) for v in value.values()):
                result[key] = value
        hydration = contract.get("hydration")
        if isinstance(hydration, dict):
            compact_hydration = {
                key: hydration.get(key)
                for key in ("required", "mode", "state_ref", "fallback", "bind_fields_from_state")
                if hydration.get(key) not in (None, "", [], {})
            }
            if compact_hydration:
                result["hydration"] = compact_hydration
        return result

    @staticmethod
    def _compact_task_action_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        contract = item.get("interaction_contract", {}) if isinstance(item.get("interaction_contract"), dict) else {}
        operation_binding = item.get("operation_binding", {}) if isinstance(item.get("operation_binding"), dict) else {}
        action: Dict[str, Any] = {
            "candidate_id": item.get("candidate_id") or contract.get("candidate_id") or contract.get("contract_id"),
            "continuation_kind": item.get("continuation_kind"),
            "target_filename": item.get("target_filename"),
        }
        operation = operation_binding.get("operation") or item.get("operation") or item.get("action")
        if operation not in (None, "", [], {}):
            action["operation"] = operation
        for key in ("step", "trace"):
            value = operation_binding.get(key) if key in operation_binding else item.get(key)
            if value not in (None, "", [], {}):
                action[key] = value
        source_kind = operation_binding.get("source_kind")
        if source_kind not in (None, "", [], {}):
            action["source_kind"] = source_kind
        return {k: v for k, v in action.items() if v not in (None, "", [], {})}

    @staticmethod
    def _compact_visible_label_for_prompt(value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        return text

    @classmethod
    def _looks_like_visible_ui_label_for_prompt(cls, value: Any) -> bool:
        text = cls._compact_visible_label_for_prompt(value)
        if not text or len(text) > 32:
            return False
        if re.search(r"[。！？!?；;]", text):
            return False
        if re.search(r"(点击|进入|选择|填写|以便|现在要|我要|click|tap|select|enter|open)", text, re.IGNORECASE):
            return False
        return True

    @classmethod
    def _compact_runtime_contract_for_prompt(cls, value: Any) -> Any:
        if isinstance(value, dict):
            compact: Dict[str, Any] = {}
            for key, child in value.items():
                if key in {"route_params", "route_requires_params"} and child in (None, "", [], {}, False):
                    continue
                compact[key] = cls._compact_runtime_contract_for_prompt(child)
            return compact
        if isinstance(value, list):
            return [cls._compact_runtime_contract_for_prompt(item) for item in value]
        return value

    @staticmethod
    def _compact_page_execution_contract_for_prompt(contract: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(contract, dict):
            return {}
        from trace2world_pipeline.app_spec.page_execution_contracts import compact_page_execution_contract

        return compact_page_execution_contract(contract)

    @staticmethod
    def _compact_page_execution_obligations_for_prompt(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        obligations = payload.get("obligations", [])
        result: List[Dict[str, Any]] = []
        for item in obligations if isinstance(obligations, list) else []:
            if not isinstance(item, dict):
                continue
            compact = {
                key: item.get(key)
                for key in (
                    "trace_id",
                    "step_index",
                    "operation",
                    "command",
                    "input_text",
                    "gesture",
                    "validation_viewport",
                    "expected_filename",
                    "hard",
                )
                if item.get(key) not in (None, "", [], {})
            }
            if item.get("intent") not in (None, "", [], {}):
                compact["intent_provenance"] = item.get("intent_provenance") or "trace_action"
                compact["intent_visible_text_allowed"] = False
            for key in ("operation_intent", "intent_provenance", "intent_visible_text_allowed"):
                value = item.get(key)
                if value not in (None, "", [], {}):
                    compact[key] = value
            if compact:
                result.append(compact)
        return result

    @classmethod
    def _compact_page_action_for_prompt(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        result: Dict[str, Any] = {}
        for key in ("trace", "step", "action"):
            value = item.get(key)
            if value not in (None, "", [], {}):
                result[key] = value
        if (
            item.get("target_visible_text_allowed") is True
            or item.get("visible_text_allowed") is True
            or cls._looks_like_visible_ui_label_for_prompt(item.get("target_element"))
        ):
            target_label = cls._compact_visible_label_for_prompt(item.get("target_element"))
            if target_label:
                result["target_element"] = target_label
        description = str(item.get("description") or "")
        option_match = re.search(r"(?:选择|select|choose)\s*([^，,。.!?；;]{1,24})", description, re.IGNORECASE)
        if option_match:
            option_label = cls._compact_visible_label_for_prompt(option_match.group(1))
            if option_label:
                result["operation_visible_option"] = option_label
        return result

    @classmethod
    def _compact_navigation_edge_for_prompt(cls, edge: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(edge, dict):
            return {}
        result = {
            "to_page": edge.get("to_page") or edge.get("target_page_name") or edge.get("to_page_id"),
            "to_filename": edge.get("to_filename") or edge.get("target_filename"),
        }
        if edge.get("trigger_visible_text_allowed") is True:
            trigger = cls._compact_visible_label_for_prompt(edge.get("trigger"))
            if trigger:
                result["trigger_label"] = trigger
        return {k: v for k, v in result.items() if v not in (None, "", [], {})}

    @classmethod
    def _compact_inbound_navigation_edge_for_prompt(cls, edge: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(edge, dict):
            return {}
        result = {
            "from_page": edge.get("from_page") or edge.get("source_page_name") or edge.get("from_page_id"),
            "from_filename": edge.get("from_filename") or edge.get("source_filename"),
        }
        if edge.get("trigger_visible_text_allowed") is True:
            trigger = cls._compact_visible_label_for_prompt(edge.get("trigger") or edge.get("trigger_text"))
            if trigger:
                result["trigger_label"] = trigger
        return {k: v for k, v in result.items() if v not in (None, "", [], {})}

    @staticmethod
    def _route_action_binding_labels_for_prompt(page_execution_contract: Dict[str, Any]) -> set[str]:
        if not isinstance(page_execution_contract, dict):
            return set()
        route_contract = page_execution_contract.get("route_execution_contract", {})
        if not isinstance(route_contract, dict):
            return set()
        labels: set[str] = set()
        for key in ("actions", "navigation_actions"):
            actions = route_contract.get(key, [])
            if not isinstance(actions, list):
                continue
            for action in actions:
                if not isinstance(action, dict):
                    continue
                binding = action.get("binding", {}) if isinstance(action.get("binding"), dict) else {}
                raw_labels = binding.get("labels", [])
                for label in raw_labels if isinstance(raw_labels, list) else []:
                    text = re.sub(r"\s+", " ", str(label or "").strip())
                    if text:
                        labels.add(text)
        return labels

    @staticmethod
    def _label_conflicts_with_route_action(label: str, route_action_labels: set[str]) -> bool:
        text = re.sub(r"\s+", " ", str(label or "").strip())
        if not text:
            return False
        return any(text == route_label for route_label in route_action_labels)

    @classmethod
    def _compact_interaction_policy_for_prompt(
        cls,
        policy: Dict[str, Any],
        route_action_labels: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(policy, dict):
            return {}
        route_action_labels = route_action_labels or set()
        result: Dict[str, Any] = {}
        default_policy = policy.get("default_component_policy")
        if default_policy:
            result["default_component_policy"] = default_policy
        for key, entry_key in (
            ("navigable_texts", "navigable_entries"),
            ("non_navigable_texts", "non_navigable_entries"),
        ):
            entries = policy.get(entry_key)
            if isinstance(entries, list) and entries:
                kept = [
                    str(item.get("text") or "").strip()
                    for item in entries
                    if (
                        isinstance(item, dict)
                        and item.get("visible_text_allowed") is True
                        and str(item.get("source_kind") or "") in {"visible_binding", "content_text", "l0_required_text", "task_element"}
                        and str(item.get("text") or "").strip()
                        and not (
                            key == "non_navigable_texts"
                            and Trace2WorldHtmlGenerator._label_conflicts_with_route_action(
                                str(item.get("text") or ""),
                                route_action_labels,
                            )
                        )
                    )
                ]
                if kept:
                    result[key] = kept
                omitted = len(entries) - len(kept)
                if omitted > 0:
                    result[f"{key}_non_visual_omitted"] = omitted
                continue
            values = policy.get(key)
            if isinstance(values, list) and values:
                kept = [
                    cls._compact_visible_label_for_prompt(item)
                    for item in values
                    if cls._looks_like_visible_ui_label_for_prompt(item)
                    and not (
                        key == "non_navigable_texts"
                        and Trace2WorldHtmlGenerator._label_conflicts_with_route_action(str(item or ""), route_action_labels)
                    )
                ]
                if kept:
                    result[key] = kept[:80]
                omitted = len(values) - len(kept)
                if omitted > 0:
                    result[f"{key}_legacy_unprovenanced_count"] = omitted
        inline_only = policy.get("inline_only_texts")
        if isinstance(inline_only, list) and inline_only:
            result["inline_only_texts_omitted"] = len(inline_only)
            result["inline_only_texts_note"] = (
                "Raw trace action/reasoning texts are intentionally omitted from PageGen prompt; "
                "they are not visible UI content."
            )
        strict_navigation = policy.get("strict_navigation")
        if strict_navigation is not None:
            result["strict_navigation"] = bool(strict_navigation)
        return result

    @classmethod
    def _summarize_json_schema_for_prompt(cls, schema: Any, depth: int = 0) -> Any:
        if not isinstance(schema, dict):
            return schema
        result: Dict[str, Any] = {}
        schema_type = schema.get("type")
        if schema_type:
            result["type"] = schema_type
        enum_values = schema.get("enum")
        if isinstance(enum_values, list) and enum_values:
            result["enum"] = enum_values[:8]
        description = str(schema.get("description") or "").strip()
        if description and depth <= 2:
            result["description"] = description[:160]
        if depth >= 3:
            props = schema.get("properties")
            if isinstance(props, dict):
                result["keys"] = sorted(str(key) for key in props.keys())[:24]
            return result
        props = schema.get("properties")
        if isinstance(props, dict):
            result["properties"] = {
                str(key): cls._summarize_json_schema_for_prompt(value, depth + 1)
                for key, value in list(props.items())[:32]
                if isinstance(value, dict)
            }
        items = schema.get("items")
        if isinstance(items, dict):
            result["items"] = cls._summarize_json_schema_for_prompt(items, depth + 1)
        return result

    @classmethod
    def _compact_sdk_interfaces_for_prompt(cls, page_interfaces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compact: List[Dict[str, Any]] = []
        for iface in page_interfaces or []:
            if not isinstance(iface, dict):
                continue
            name = iface.get("name") or iface.get("function_name") or ""
            if not name:
                continue
            params = []
            raw_params = iface.get("parameters", [])
            for param in raw_params if isinstance(raw_params, list) else []:
                if not isinstance(param, dict):
                    continue
                params.append(
                    {
                        key: param.get(key)
                        for key in ("name", "type", "required")
                        if param.get(key) not in (None, "", [], {})
                    }
                )
            compact.append(
                {
                    "method": name,
                    "parameters": params,
                    "operation": iface.get("operation", ""),
                    "entity": iface.get("entity", ""),
                    "returns": cls._summarize_json_schema_for_prompt(iface.get("returns", {})),
                }
            )
        return compact

    @staticmethod
    def _resource_prompt_line_priority(line: str) -> int:
        text = str(line or "")
        if "asset_kind=cover_image" in text or "asset_kind=banner" in text or "asset_kind=photo" in text:
            return 0
        if "sprite_group=content_visual" in text:
            return 1
        if "asset_kind=icon" in text or "asset_kind=badge" in text:
            return 3
        return 2

    @classmethod
    def _compact_resource_list_text_for_prompt(cls, resource_list_text: str, max_lines: int = 48) -> str:
        text = str(resource_list_text or "").strip()
        if not text:
            return ""
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if len(lines) <= max_lines:
            return text
        intro = [line for line in lines if not line.lstrip().startswith("[")]
        resource_lines = [line for line in lines if line.lstrip().startswith("[")]
        ranked = sorted(
            enumerate(resource_lines),
            key=lambda item: (cls._resource_prompt_line_priority(item[1]), item[0]),
        )
        kept = [line for _idx, line in ranked[:max_lines]]
        kept.sort(key=lambda line: int(re.match(r"\[(\d+)\]", line.strip()).group(1)) if re.match(r"\[(\d+)\]", line.strip()) else 10**9)
        omitted = max(0, len(resource_lines) - len(kept))
        prefix = intro[:2] if intro else ["Use these resource images for this page. Reference by exact path."]
        if omitted:
            prefix.append(
                f"Prompt projection keeps {len(kept)} semantically prioritized resources and omits {omitted} debug/provenance-only resource rows."
            )
        return "\n".join(prefix + kept + ["Use real images, NOT emoji/text placeholders."])

    @staticmethod
    def _summarize_page_resources_for_prompt(page_resources: List[Dict[str, Any]], limit: int = 24) -> Dict[str, Any]:
        if not isinstance(page_resources, list):
            return {}
        counts: Dict[str, int] = {}
        candidates: List[Dict[str, Any]] = []
        for resource in page_resources:
            if not isinstance(resource, dict):
                continue
            annotation = resource.get("pagegen_semantic_annotation")
            annotation = annotation if isinstance(annotation, dict) else {}
            if not annotation:
                annotation = {
                    key: resource.get(key)
                    for key in (
                        "semantic_label",
                        "asset_kind",
                        "sprite_group",
                        "visual_role",
                    )
                    if resource.get(key) not in (None, "", [], {})
                }
            sprite_group = str(annotation.get("sprite_group") or resource.get("original_category") or "unknown")
            counts[sprite_group] = counts.get(sprite_group, 0) + 1
            omission_reason = pagegen_resource_prompt_omission_reason(resource)
            if omission_reason:
                key = f"{omission_reason}_omitted"
                counts[key] = counts.get(key, 0) + 1
                continue
            asset_name = resource.get("asset_name")
            asset_path = resource.get("asset_path") or resource.get("path")
            item = {
                "asset_name": asset_name,
                "path": asset_path or (f"images/{asset_name}" if asset_name else ""),
                "resource_ref": resource.get("resource_ref"),
                "source_screenshot_ref": resource.get("source_screenshot_ref"),
                "source_step_key": resource.get("source_step_key"),
                "semantic_label": annotation.get("semantic_label"),
                "asset_kind": annotation.get("asset_kind"),
                "sprite_group": annotation.get("sprite_group"),
                "visual_role": annotation.get("visual_role"),
                "render_width": resource.get("render_width"),
                "render_height": resource.get("render_height"),
                "scaled_width": resource.get("scaled_width"),
                "scaled_height": resource.get("scaled_height"),
            }
            candidates.append({k: v for k, v in item.items() if v not in (None, "", [], {})})
        return {
            "resource_count": len(page_resources),
            "semantic_group_counts": counts,
            "prioritized_candidates": candidates[:limit],
            "prioritized_candidate_count": len(candidates),
        }

    @staticmethod
    def _strip_empty_recursive(value):
        def _strip(v):
            if isinstance(v, dict):
                cleaned = {k: _strip(child) for k, child in v.items()}
                return {k: child for k, child in cleaned.items() if child not in (None, "", [], {})}
            if isinstance(v, list):
                stripped = [_strip(i) for i in v]
                return [s for s in stripped if s not in (None, "", [], {})]
            return v
        return _strip(value)

    @staticmethod
    def _compact_external_regions_for_prompt(regions: List[Dict[str, Any]], *, budgeted: bool = False) -> Dict[str, Any]:
        if not isinstance(regions, list):
            return {}
        seen: set[Tuple[str, str]] = set()
        compact: List[Dict[str, Any]] = []
        duplicate_count = 0
        limit = 6 if budgeted else 10
        for region in regions:
            if not isinstance(region, dict):
                continue
            region_type = str(region.get("region_type") or "other").strip() or "other"
            location = str(region.get("location") or "").strip() or "unknown"
            key = (region_type, location)
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            item = {
                "region_type": region_type,
                "location": location,
                "ignore_for_app_model": True,
            }
            description = str(region.get("description") or "").strip()
            if description and not budgeted:
                item["description"] = description[:120]
            compact.append(item)
            if len(compact) >= limit:
                remaining_unique = max(0, len(seen) - len(compact))
                duplicate_count += remaining_unique
                break
        if not compact and duplicate_count <= 0:
            return {}
        return {
            "summary": compact,
            "dedupe_key": ["region_type", "location"],
            "omitted_duplicate_or_over_limit_count": duplicate_count,
            "contract": "negative evidence only; never render these regions, create actions for them, or treat their text as app content",
        }

    @classmethod
    def _compact_required_interaction_surfaces_for_prompt(
        cls,
        surfaces: List[Dict[str, Any]],
        *,
        budgeted: bool = False,
    ) -> List[Dict[str, Any]]:
        if not isinstance(surfaces, list):
            return []
        if not budgeted:
            return surfaces

        compacted: List[Dict[str, Any]] = []
        for surface in surfaces:
            if not isinstance(surface, dict):
                continue
            item: Dict[str, Any] = {}
            for key in (
                "surface_id",
                "surface_role",
                "required_data_element_id",
                "required_display_label",
                "control_label",
                "surface",
                "surface_family",
                "interaction_kind",
                "opened_surface_id",
                "opened_surface_option_id",
                "required_in_state",
                "render_phase",
                "host_availability_phase",
                "initial_dom_eligible",
                "initial_visibility_policy",
                "runtime_visibility_policy",
                "action_kinds",
                "required_data_element_aliases",
                "option_aliases",
                "contract_status",
            ):
                value = surface.get(key)
                if value not in (None, "", [], {}):
                    item[key] = value

            dom_contract = surface.get("dom_contract")
            if isinstance(dom_contract, dict):
                item["dom_contract"] = {
                    key: dom_contract.get(key)
                    for key in (
                        "required_behavior",
                        "forbid_noninteractive_host",
                        "forbid_initial_visible",
                        "click_surface",
                    )
                    if dom_contract.get(key) not in (None, "", [], {})
                }

            expected_effects = surface.get("expected_effects")
            if isinstance(expected_effects, list) and expected_effects:
                item["expected_effects"] = [
                    {
                        key: effect.get(key)
                        for key in ("type", "reducer", "state_ref", "target_filename")
                        if isinstance(effect, dict) and effect.get(key) not in (None, "", [], {})
                    }
                    for effect in expected_effects
                    if isinstance(effect, dict)
                ]

            evidence_steps = surface.get("evidence_steps")
            if isinstance(evidence_steps, list):
                step_keys = []
                for evidence in evidence_steps:
                    if not isinstance(evidence, dict):
                        continue
                    step_key = str(evidence.get("step_key") or "").strip()
                    if step_key and step_key not in step_keys:
                        step_keys.append(step_key)
                if step_keys:
                    item["evidence_step_keys"] = step_keys

            requirements = [
                key.replace("_required", "")
                for key in ("visibility_required", "topmost_required", "interactive_required")
                if surface.get(key) is True
            ]
            if requirements:
                item["requirements"] = requirements

            state_surface_contract = surface.get("state_surface_contract")
            if isinstance(state_surface_contract, dict):
                item["state_surface_contract"] = cls._compact_state_surface_contract_for_prompt(
                    state_surface_contract
                )

            compacted.append(cls._strip_empty_recursive(item))
        return compacted

    @staticmethod
    def _pagegen_needs_seat_map_contract(
        filename: str,
        page_name: str,
        description: str,
        pagegen_render_view: Dict[str, Any],
        required_interaction_surfaces: List[Dict[str, Any]],
    ) -> bool:
        snippets: List[str] = [filename, page_name, description]
        for surface in required_interaction_surfaces:
            if not isinstance(surface, dict):
                continue
            snippets.extend(
                str(surface.get(key) or "")
                for key in (
                    "required_display_label",
                    "control_label",
                    "operation_intent",
                    "surface_role",
                    "surface_family",
                    "required_data_element_id",
                    "surface_id",
                )
            )
        if isinstance(pagegen_render_view, dict):
            snippets.append(json.dumps(pagegen_render_view, ensure_ascii=False, default=str)[:6000])
        haystack = "\n".join(snippets)
        lowered = haystack.lower()
        if any(token in lowered for token in ("seat-map", "seat map", "seatmap")):
            return True
        return (
            "座位图" in haystack
            or "可选座" in haystack
            or ("座位" in haystack and any(token in haystack for token in ("选座", "座区", "区域")))
        )

    @staticmethod
    def _seat_map_generation_contract_prompt() -> str:
        return (
            "\n## Seat Map Visual Actionability Contract\n"
            "For seat-map pages, dense reference seats may be simplified, but the simplification "
            "must preserve every visible floor/section/block and the approximate relative layout. "
            "Represent dense seats as sparse dot grids or grouped blocks rather than omitting sections.\n"
            "- Do not mix multiple non-neutral price/status colors inside one seat section/block. "
            "If the reference has different colors in the same broad area, split that area into "
            "separate visible sub-zones with a gap/border and give each sub-zone one uniform color.\n"
            "- Keep decorative unavailable/empty seats neutral. Use price/status colors only for "
            "intentional selectable or categorized zones.\n"
            "- Task-relevant seat/zone action hosts must have a real tappable box of at least "
            "44px by 44px. If individual dots are visually smaller, wrap them in a visible zone "
            "button or add padding to the button itself; do not rely on a tiny dot or invisible "
            "coordinate hotspot.\n"
            "- Non-target decorative seat dots must not be independently interactive. Bind "
            "`data-element-id` and `data-action-id` to the visible section/zone button that the "
            "agent should click."
        )

    @classmethod
    def _build_pagegen_dom_hard_gate_checklist(
        cls,
        surfaces: List[Dict[str, Any]],
        *,
        budgeted: bool = False,
    ) -> Dict[str, Any]:
        if not isinstance(surfaces, list):
            surfaces = []
        rows: List[Dict[str, Any]] = []
        visible_count = 0
        hidden_count = 0
        for surface in surfaces:
            if not isinstance(surface, dict):
                continue
            required_id = str(surface.get("required_data_element_id") or surface.get("surface_id") or "").strip()
            if not required_id:
                continue
            policy = str(surface.get("initial_visibility_policy") or "").strip() or "visible_initial"
            action_kinds = [
                str(kind).strip()
                for kind in (surface.get("action_kinds") if isinstance(surface.get("action_kinds"), list) else [])
                if str(kind).strip()
            ]
            aliases = [
                str(alias).strip()
                for alias in (
                    surface.get("required_data_element_aliases")
                    if isinstance(surface.get("required_data_element_aliases"), list)
                    else []
                )
                if str(alias).strip() and str(alias).strip() != required_id
            ]
            if policy == "hidden_until_transition" or surface.get("initial_dom_eligible") is False:
                hidden_count += 1
                initial_requirement = "hidden_same_element"
            else:
                visible_count += 1
                initial_requirement = "visible_interactive_initial"
            row: Dict[str, Any] = {
                "id": required_id,
                "label": surface.get("required_display_label") or surface.get("control_label"),
                "init": policy,
                "must": initial_requirement,
            }
            if aliases:
                row["alias_count"] = len(aliases)
            if action_kinds:
                row["act"] = action_kinds[:3]
            rows.append(cls._strip_empty_recursive(row))
        if not rows:
            return {}
        return {
            "contract": "pre-submit hard gate for first-pass PageGen; every row must be satisfied before final HTML",
            "required_host_count": len(rows),
            "visible_initial_count": visible_count,
            "hidden_until_transition_count": hidden_count,
            "global_rules": [
                "Every id must appear exactly once as data-element-id or as a whitespace-separated data-element-aliases token on the same physical host.",
                "Visible-initial hosts must be real interactive controls, not static div/span, tiny hotspots, transparent overlays, or noninteractive ancestors.",
                "Hidden-until-transition hosts may exist in initial DOM only if the same element carrying the id/alias is hidden; reveal it through the declared state transition.",
                "For fill_input rows, render a blank/focusable input initially; label is the replay-entered value, not an initial value.",
                "Do not comma-separate data-element-aliases; CSS/runtime uses token matching.",
            ],
            "required_hosts": rows,
        }

    @classmethod
    def _compact_state_surface_contract_for_prompt(cls, contract: Dict[str, Any]) -> Dict[str, Any]:
        compact: Dict[str, Any] = {
            key: contract.get(key)
            for key in (
                "surface_id",
                "open_state_id",
                "surface_family",
                "initial_visibility_policy",
                "runtime_visibility_policy",
            )
            if contract.get(key) not in (None, "", [], {})
        }
        options = []
        raw_options = contract.get("options")
        for option in raw_options if isinstance(raw_options, list) else []:
            if not isinstance(option, dict):
                continue
            compact_option = {
                key: option.get(key)
                for key in (
                    "option_id",
                    "option_key",
                    "label",
                    "aliases",
                    "component_ids",
                    "source_step_keys",
                    "action_kind",
                    "interactive_required",
                )
                if option.get(key) not in (None, "", [], {})
            }
            expected = option.get("expected_effect")
            if isinstance(expected, dict):
                compact_option["expected_effect"] = {
                    key: expected.get(key)
                    for key in ("type", "reducer", "state_ref", "target_filename")
                    if expected.get(key) not in (None, "", [], {})
                }
            options.append(compact_option)
        if options:
            compact["options"] = options
        return cls._strip_empty_recursive(compact)

    @classmethod
    def _compact_asset_inventory_for_spec_prompt(
        cls,
        resource_list_text: str,
        page_resources: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "resource_list_text": cls._compact_resource_list_text_for_prompt(resource_list_text),
            "semantic_resource_summary": cls._summarize_page_resources_for_prompt(page_resources),
            "contract": "asset inventory only; it does not authorize extra content, records, sections, state, or navigation beyond page_environment_spec",
        }

    @classmethod
    def _collect_spec_method_refs(cls, value: Any) -> set[str]:
        refs: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"sdk_method", "method", "api_method", "website_sdk_method"} and isinstance(child, str) and child.strip():
                    refs.add(child.strip())
                refs.update(cls._collect_spec_method_refs(child))
        elif isinstance(value, list):
            for item in value:
                refs.update(cls._collect_spec_method_refs(item))
        return refs

    @classmethod
    def _compact_sdk_api_surface_for_spec_prompt(
        cls,
        page_interfaces: List[Dict[str, Any]],
        page_environment_spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        method_refs = cls._collect_spec_method_refs(page_environment_spec)
        compact: List[Dict[str, Any]] = []
        allowed_names = sorted(cls._page_interface_method_names(page_interfaces))
        for iface in page_interfaces or []:
            if not isinstance(iface, dict):
                continue
            name = str(iface.get("name") or iface.get("function_name") or "").strip()
            if not name:
                continue
            if method_refs and name not in method_refs:
                continue
            params = []
            raw_params = iface.get("parameters", [])
            for param in raw_params if isinstance(raw_params, list) else []:
                if not isinstance(param, dict):
                    continue
                params.append({
                    key: param.get(key)
                    for key in ("name", "type", "required")
                    if param.get(key) not in (None, "", [], {})
                })
            compact.append({
                "method": name,
                "parameters": params,
                "operation": iface.get("operation", ""),
                "entity": iface.get("entity", ""),
            })
        return {
            "allowed_website_sdk_methods": [item["method"] for item in compact] if method_refs else allowed_names,
            "sdk_interfaces": compact if method_refs else [
                {
                    "method": item.get("method"),
                    "parameters": item.get("parameters", []),
                    "operation": item.get("operation", ""),
                    "entity": item.get("entity", ""),
                }
                for item in cls._compact_sdk_interfaces_for_prompt(page_interfaces or [])
            ],
            "contract": "callable API surface only; state semantics and reducer obligations come from page_environment_spec",
        }

    @classmethod
    def _compact_page_environment_spec_for_pagegen_prompt(cls, spec: Dict[str, Any], budgeted: bool = False) -> Dict[str, Any]:
        if not isinstance(spec, dict):
            return {}

        top_level_keys = (
            "page_id",
            "filename",
            "initial_state",
            "state_schema",
            "content_seed",
            "components",
            "render_projections",
            "transitions",
            "query_projections",
            "parameterized_inputs",
            "interface_requirements",
            "oracles",
            "conflict_resolution",
            "conflict_resolutions",
            "semantic_enrichment",
        )
        skip_substrings = (
            "debug",
            "cache",
            "artifact",
            "raw_",
            "prompt",
            "screenshot",
            "resource",
        )

        def prioritize_prompt_items(items: List[Any], limit: int) -> List[Any]:
            if not budgeted or len(items) <= limit:
                return items[:limit]

            def score(value: Any) -> Tuple[int, int]:
                if not isinstance(value, dict):
                    return (0, 0)
                text = json.dumps(value, ensure_ascii=False, sort_keys=True)
                priority = 0
                if value.get("interaction_required") is True:
                    priority += 20
                if value.get("source_step_keys"):
                    priority += 18
                if value.get("target_filename") or value.get("expected_filename"):
                    priority += 14
                if value.get("dom_contract"):
                    priority += 12
                if value.get("suggestion_contract") or value.get("overlay_contract") or value.get("selection_contract"):
                    priority += 10
                if value.get("reads_state") or value.get("state_ref") or value.get("state_refs"):
                    priority += 8
                if value.get("component_role") == "l0_execution_anchor":
                    priority += 8
                return (priority, -len(text))

            indexed = list(enumerate(items))
            selected = sorted(indexed, key=lambda pair: (-score(pair[1])[0], score(pair[1])[1], pair[0]))[:limit]
            return [value for _, value in sorted(selected, key=lambda pair: pair[0])]

        def compact_display_value(value: Any) -> Any:
            limit = 80 if budgeted else 220
            if isinstance(value, str):
                text = re.sub(r"\s+", " ", value.strip())
                return text[:limit] + ("…" if len(text) > limit else "")
            if isinstance(value, list):
                compacted = []
                for item in value[:8 if budgeted else 16]:
                    compacted_item = compact_display_value(item)
                    if compacted_item not in (None, "", [], {}):
                        compacted.append(compacted_item)
                if len(value) > len(compacted):
                    compacted.append({"omitted_count": len(value) - len(compacted), "omitted_reason": "prompt_label_size_limit"})
                return compacted
            return value

        def compact_dom_contract(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            keep = (
                "required_data_element_id",
                "required_data_element_aliases",
                "required_behavior",
                "click_surface",
                "surface_role",
                "required_display_label",
                "control_label",
                "initial_visibility_policy",
            )
            compact = {
                key: compact_display_value(value.get(key)) if key in {"required_display_label", "control_label"} else value.get(key)
                for key in keep
                if value.get(key) not in (None, "", [], {})
            }
            if value.get("forbid_noninteractive_host") is True:
                compact["forbid_noninteractive_host"] = True
            return compact

        def compact_state_effect(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            compact = {
                key: value.get(key)
                for key in (
                    "effect",
                    "draft_ref",
                    "state_ref",
                    "target_filename",
                    "field_role",
                    "value",
                    "mutation_policy",
                    "query_transition_kind",
                    "query_state_owner",
                    "target_identity_status",
                    "visible_initial_prefill_allowed",
                    "effect_admission_status",
                )
                if value.get(key) not in (None, "", [], {})
            }
            for diff_key in ("expected_state_diff", "state_diff"):
                diff = value.get(diff_key)
                if isinstance(diff, dict) and diff:
                    compact[f"{diff_key}_keys"] = list(diff.keys())[:4]
            committed_fields = value.get("committed_fields")
            if isinstance(committed_fields, list) and committed_fields:
                compact["committed_fields"] = [
                    {
                        key: field.get(key)
                        for key in ("field_role", "value", "step_key")
                        if isinstance(field, dict) and field.get(key) not in (None, "", [], {})
                    }
                    for field in committed_fields[:3]
                    if isinstance(field, dict)
                ]
            hydration = value.get("selected_record_hydration")
            if isinstance(hydration, dict) and hydration:
                compact["selected_record_hydration"] = {
                    key: compact_display_value(hydration.get(key)) if key in {"title", "recipient_title"} else hydration.get(key)
                    for key in ("required", "source_filename", "target_filename", "policy", "title", "recipient_title")
                    if hydration.get(key) not in (None, "", [], {})
                }
            return {k: v for k, v in compact.items() if v not in (None, "", [], {})}

        def compact_state_schema(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            compact: Dict[str, Any] = {}
            drafts = value.get("drafts")
            if isinstance(drafts, list) and drafts:
                compact["drafts"] = [
                    {
                        key: draft.get(key)
                        for key in ("draft_ref", "source_filename", "target_filename")
                        if isinstance(draft, dict) and draft.get(key) not in (None, "", [], {})
                    }
                    | {
                        "field_roles": [
                            field.get("field_role")
                            for field in (draft.get("fields") or [])[:3]
                            if isinstance(field, dict) and field.get("field_role")
                        ]
                    }
                    for draft in drafts[:6]
                    if isinstance(draft, dict)
                ]
                if len(drafts) > 6:
                    compact["drafts"].append({"omitted_count": len(drafts) - 6, "omitted_reason": "prompt_state_schema_size_limit"})
            collections = value.get("collections")
            if isinstance(collections, list) and collections:
                compact["collections"] = [
                    {
                        key: collection.get(key)
                        for key in ("state_ref", "state_scope", "merge_policy")
                        if isinstance(collection, dict) and collection.get(key) not in (None, "", [], {})
                    }
                    for collection in collections[:8]
                    if isinstance(collection, dict)
                ]
                if len(collections) > 8:
                    compact["collections"].append({"omitted_count": len(collections) - 8, "omitted_reason": "prompt_state_schema_size_limit"})
            parameters = value.get("parameters")
            if isinstance(parameters, list) and parameters:
                compact["parameters"] = [
                    {
                        key: parameter.get(key)
                        for key in ("parameter_id", "field_role")
                        if isinstance(parameter, dict) and parameter.get(key) not in (None, "", [], {})
                    }
                    for parameter in parameters[:6]
                    if isinstance(parameter, dict)
                ]
            runtime_refs = value.get("runtime_state_refs")
            if isinstance(runtime_refs, list) and runtime_refs:
                compact["runtime_state_refs"] = [
                    {
                        key: ref.get(key)
                        for key in ("state_ref", "state_scope")
                        if isinstance(ref, dict) and ref.get(key) not in (None, "", [], {})
                    }
                    for ref in runtime_refs[:10]
                    if isinstance(ref, dict)
                ]
                if len(runtime_refs) > 10:
                    compact["runtime_state_refs"].append({"omitted_count": len(runtime_refs) - 10, "omitted_reason": "prompt_state_schema_size_limit"})
            return compact

        def compact_state_seed(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            compact: Dict[str, Any] = {}
            if value.get("seed_policy"):
                compact["seed_policy"] = value.get("seed_policy")
            collections = value.get("collections")
            if isinstance(collections, dict):
                items = list(collections.items())
                compact["collections"] = [
                    {
                        "state_ref": state_ref,
                        "state_scope": details.get("state_scope") if isinstance(details, dict) else None,
                        "min_initial_count": details.get("min_initial_count") if isinstance(details, dict) else None,
                        "seed_policy": details.get("seed_policy") if isinstance(details, dict) else None,
                        "evidence_steps": (details.get("evidence_steps") or [])[:1] if isinstance(details, dict) else [],
                    }
                    for state_ref, details in items[:8]
                ]
                if len(items) > 8:
                    compact["collections"].append({"omitted_count": len(items) - 8, "omitted_reason": "prompt_state_seed_size_limit"})
            elif isinstance(collections, list):
                compact["collections"] = [
                    {
                        key: item.get(key)
                        for key in ("state_ref", "state_scope", "min_initial_count", "seed_policy")
                        if isinstance(item, dict) and item.get(key) not in (None, "", [], {})
                    }
                    | (
                        {"evidence_steps": (item.get("evidence_steps") or [])[:1]}
                        if isinstance(item, dict) and item.get("evidence_steps")
                        else {}
                    )
                    for item in collections[:8]
                    if isinstance(item, dict)
                ]
                if len(collections) > 8:
                    compact["collections"].append({"omitted_count": len(collections) - 8, "omitted_reason": "prompt_state_seed_size_limit"})
            return compact

        def compact_component(item: Dict[str, Any]) -> Dict[str, Any]:
            if str(item.get("component_role") or "").lower() == "l0_execution_anchor":
                compact_anchor = {
                    "component_id": item.get("component_id"),
                    "component_role": "l0_execution_anchor",
                    "render_policy": "alias_only_do_not_render_separate_dom",
                    "dom_binding_policy": (
                        "Bind the trace step through the canonical visible component selected by "
                        "step_component_bindings; do not create hidden/tiny fallback DOM for this anchor."
                    ),
                    "reads_state": item.get("reads_state"),
                    "render_phase": item.get("render_phase"),
                    "host_availability_phase": item.get("host_availability_phase"),
                    "interaction_required": item.get("interaction_required"),
                    "dom_contract": item.get("dom_contract"),
                    "source_step_keys": item.get("source_step_keys"),
                    "initial_dom_eligible": item.get("initial_dom_eligible"),
                    "initial_visibility_policy": item.get("initial_visibility_policy"),
                    "runtime_visibility_policy": item.get("runtime_visibility_policy"),
                }
                if budgeted:
                    compact_anchor.pop("render_phase", None)
                    compact_anchor.pop("host_availability_phase", None)
                    compact_anchor.pop("dom_binding_policy", None)
                    compact_anchor["dom_contract"] = compact_dom_contract(compact_anchor.get("dom_contract"))
                return {
                    key: value
                    for key, value in compact_anchor.items()
                    if value not in (None, "", [], {})
                }
            contract = item.get("interaction_contract") if isinstance(item.get("interaction_contract"), dict) else {}
            compact = {
                "component_id": item.get("component_id"),
                "component_kind": item.get("component_kind"),
                "component_role": item.get("component_role"),
                "reads_state": item.get("reads_state"),
                "render_phase": item.get("render_phase"),
                "layout_policy": item.get("layout_policy"),
                "host_availability_phase": item.get("host_availability_phase"),
                "state_value_phase": item.get("state_value_phase"),
                "content_projection_phase": item.get("content_projection_phase"),
                "initial_value_source": item.get("initial_value_source"),
                "post_action_value": item.get("post_action_value"),
                "forbid_initial_value_from_input_text": item.get("forbid_initial_value_from_input_text"),
                "interaction_required": item.get("interaction_required"),
                "display_label": compact_display_value(item.get("display_label")),
                "display_labels": compact_display_value(item.get("display_labels")),
                "target_filename": item.get("target_filename"),
                "dom_contract": compact_dom_contract(item.get("dom_contract")) if budgeted else item.get("dom_contract"),
            }
            if budgeted:
                compact = {
                    key: compact.get(key)
                    for key in (
                        "component_id",
                        "component_kind",
                        "component_role",
                        "reads_state",
                        "interaction_required",
                        "display_label",
                        "display_labels",
                        "target_filename",
                        "dom_contract",
                    )
                }
            if contract:
                compact["interaction"] = {
                    key: contract.get(key)
                    for key in (
                        "operation",
                        "expected_filename",
                        "input_text",
                        "display_label",
                        "display_labels",
                        "reducer",
                        "state_effect",
                        "dom_contract",
                    )
                    if contract.get(key) not in (None, "", [], {})
                }
                if budgeted and "state_effect" in compact["interaction"]:
                    compact["interaction"]["state_effect"] = compact_state_effect(compact["interaction"]["state_effect"])
                if budgeted and "dom_contract" in compact["interaction"]:
                    compact["interaction"]["dom_contract"] = compact_dom_contract(compact["interaction"]["dom_contract"])
                if "display_label" in compact["interaction"]:
                    compact["interaction"]["display_label"] = compact_display_value(compact["interaction"]["display_label"])
                if "display_labels" in compact["interaction"]:
                    compact["interaction"]["display_labels"] = compact_display_value(compact["interaction"]["display_labels"])
                if not budgeted and contract.get("initial_value_binding") not in (None, "", [], {}):
                    compact["interaction"]["initial_value_binding"] = contract.get("initial_value_binding")
                intent = contract.get("operation_intent")
                if intent:
                    compact["interaction"]["operation_intent"] = str(intent)[:120 if budgeted else 220]
            steps = item.get("source_step_keys")
            if isinstance(steps, list) and steps:
                compact["source_step_keys"] = steps[:3 if budgeted else 4]
            return {k: v for k, v in compact.items() if v not in (None, "", [], {})}

        def compact_projection(item: Dict[str, Any]) -> Dict[str, Any]:
            expected = item.get("expected_interaction") if isinstance(item.get("expected_interaction"), dict) else {}
            compact = {
                "projection_id": item.get("projection_id"),
                "component_ref": item.get("component_ref"),
                "reads_state": item.get("reads_state"),
                "render_phase": item.get("render_phase"),
                "layout_policy": item.get("layout_policy"),
                "host_availability_phase": item.get("host_availability_phase"),
                "state_value_phase": item.get("state_value_phase"),
                "content_projection_phase": item.get("content_projection_phase"),
                "initial_value_source": item.get("initial_value_source"),
                "post_action_value": item.get("post_action_value"),
                "forbid_initial_value_from_input_text": item.get("forbid_initial_value_from_input_text"),
                "interaction_required": item.get("interaction_required"),
                "lifecycle": item.get("lifecycle"),
                "dom_contract": compact_dom_contract(item.get("dom_contract")) if budgeted else item.get("dom_contract"),
                "suggestion_contract": item.get("suggestion_contract"),
                "overlay_contract": item.get("overlay_contract"),
                "selection_contract": item.get("selection_contract"),
            }
            if budgeted:
                compact = {
                    key: compact.get(key)
                    for key in (
                        "projection_id",
                        "component_ref",
                        "reads_state",
                        "interaction_required",
                        "dom_contract",
                        "suggestion_contract",
                        "overlay_contract",
                        "selection_contract",
                    )
                }
            if expected:
                compact["expected_interaction"] = {
                    key: expected.get(key)
                    for key in (
                        "operation",
                        "expected_filename",
                        "input_text",
                        "display_label",
                        "display_labels",
                        "reducer",
                        "state_effect",
                        "dom_contract",
                    )
                    if expected.get(key) not in (None, "", [], {})
                }
                if budgeted and "state_effect" in compact["expected_interaction"]:
                    compact["expected_interaction"]["state_effect"] = compact_state_effect(compact["expected_interaction"]["state_effect"])
                if budgeted and "dom_contract" in compact["expected_interaction"]:
                    compact["expected_interaction"]["dom_contract"] = compact_dom_contract(compact["expected_interaction"]["dom_contract"])
                if "display_label" in compact["expected_interaction"]:
                    compact["expected_interaction"]["display_label"] = compact_display_value(compact["expected_interaction"]["display_label"])
                if "display_labels" in compact["expected_interaction"]:
                    compact["expected_interaction"]["display_labels"] = compact_display_value(compact["expected_interaction"]["display_labels"])
                if not budgeted and expected.get("initial_value_binding") not in (None, "", [], {}):
                    compact["expected_interaction"]["initial_value_binding"] = expected.get("initial_value_binding")
                intent = expected.get("operation_intent")
                if intent:
                    compact["expected_interaction"]["operation_intent"] = str(intent)[:120 if budgeted else 220]
            steps = item.get("source_step_keys")
            if isinstance(steps, list) and steps:
                compact["source_step_keys"] = steps[:3 if budgeted else 4]
            return {k: v for k, v in compact.items() if v not in (None, "", [], {})}

        def compact_transition(item: Dict[str, Any]) -> Dict[str, Any]:
            compact = {
                key: item.get(key)
                for key in (
                    "transition_id",
                    "step_key",
                    "source_filename",
                    "target_filename",
                    "operation",
                    "reducer",
                    "input_text",
                    "direction",
                    "l0_role",
                )
                if item.get(key) not in (None, "", [], {})
            }
            state_effect = item.get("state_effect")
            if state_effect not in (None, "", [], {}):
                compact["state_effect"] = compact_state_effect(state_effect) if budgeted else state_effect
            intent = item.get("operation_intent")
            if intent:
                compact["operation_intent"] = str(intent)[:120 if budgeted else 220]
            return compact

        def compact_oracle(item: Dict[str, Any]) -> Dict[str, Any]:
            compact = {
                key: item.get(key)
                for key in ("oracle_type", "step_key", "component_ref", "target_filename", "expected_filename", "state_ref", "dom_contract", "display_label")
                if item.get(key) not in (None, "", [], {})
            }
            expected = item.get("expected")
            if expected:
                compact["expected"] = str(expected)[:120 if budgeted else 220]
            return compact

        def compact_value(value: Any, depth: int = 0, parent_key: str = "") -> Any:
            if isinstance(value, dict):
                if budgeted and parent_key == "state_schema":
                    return compact_state_schema(value)
                if budgeted and parent_key in {"initial_state", "content_seed"}:
                    return compact_state_seed(value)
                result: Dict[str, Any] = {}
                for key, child in value.items():
                    key_text = str(key)
                    if any(token in key_text for token in skip_substrings):
                        continue
                    if depth >= 5:
                        if isinstance(child, (dict, list)):
                            continue
                    compacted = compact_value(child, depth + 1, key_text)
                    if compacted not in (None, "", [], {}):
                        result[key_text] = compacted
                return result
            if isinstance(value, list):
                if parent_key == "components":
                    limit = 10 if budgeted else 48
                    selected = prioritize_prompt_items(value, limit)
                    items = [compact_component(item) for item in selected if isinstance(item, dict)]
                    if len(value) > limit:
                        items.append({"omitted_count": len(value) - limit, "omitted_reason": "prompt_projection_size_limit"})
                    return items
                if parent_key == "render_projections":
                    limit = 10 if budgeted else 48
                    selected = prioritize_prompt_items(value, limit)
                    items = [compact_projection(item) for item in selected if isinstance(item, dict)]
                    if len(value) > limit:
                        items.append({"omitted_count": len(value) - limit, "omitted_reason": "prompt_projection_size_limit"})
                    return items
                if parent_key == "transitions":
                    limit = 14 if budgeted else 64
                    selected = prioritize_prompt_items(value, limit)
                    items = [compact_transition(item) for item in selected if isinstance(item, dict)]
                    if len(value) > limit:
                        items.append({"omitted_count": len(value) - limit, "omitted_reason": "prompt_projection_size_limit"})
                    return items
                if parent_key == "query_projections":
                    limit = 6 if budgeted else 32
                    selected = prioritize_prompt_items(value, limit)
                    items = [compact_value(item, depth + 1, parent_key) for item in selected if isinstance(item, dict)]
                    items = [item for item in items if item not in (None, "", [], {})]
                    if len(value) > limit:
                        items.append({"omitted_count": len(value) - limit, "omitted_reason": "prompt_projection_size_limit"})
                    return items
                if parent_key == "oracles":
                    limit = 14 if budgeted else 64
                    selected = prioritize_prompt_items(value, limit)
                    items = [compact_oracle(item) for item in selected if isinstance(item, dict)]
                    if len(value) > limit:
                        items.append({"omitted_count": len(value) - limit, "omitted_reason": "prompt_projection_size_limit"})
                    return items
                limits = (
                    {
                        "components": 10,
                        "render_projections": 10,
                        "transitions": 14,
                        "parameterized_inputs": 6,
                        "oracles": 14,
                        "content_seed": 6,
                        "records": 4,
                        "items": 4,
                        "values": 8,
                    }
                    if budgeted
                    else {
                        "components": 48,
                        "render_projections": 64,
                        "transitions": 80,
                        "parameterized_inputs": 48,
                        "oracles": 80,
                        "content_seed": 24,
                        "records": 16,
                        "items": 16,
                        "values": 24,
                    }
                )
                limit = limits.get(parent_key, 16 if (budgeted and depth >= 3) else (24 if depth >= 3 else 64))
                selected = prioritize_prompt_items(value, limit)
                items = [compact_value(item, depth + 1, parent_key) for item in selected]
                items = [item for item in items if item not in (None, "", [], {})]
                if len(value) > limit:
                    items.append({"omitted_count": len(value) - limit, "omitted_reason": "prompt_projection_size_limit"})
                return items
            if isinstance(value, str):
                text = re.sub(r"\s+", " ", value.strip())
                limit = 120 if budgeted else 600
                return text[:limit] + ("…" if len(text) > limit else "")
            return value

        compact: Dict[str, Any] = {}
        for key in top_level_keys:
            if key not in spec:
                continue
            compacted = compact_value(spec.get(key), 0, key)
            if key == "initial_state":
                compact[key] = spec.get(key) if compacted == {} else compacted
            elif compacted not in (None, "", [], {}):
                compact[key] = compacted
        compact["prompt_projection"] = {
            "source": "page_environment_spec",
            "mode": "compact_authoritative_pagegen_projection",
            "budgeted": bool(budgeted),
            "omits": [
                "debug/provenance/cache fields",
                "raw screenshots/resources",
                "full legacy contract mirrors",
            ],
        }
        return compact

    @classmethod
    def _compact_entity_task_relevance_for_prompt(cls, relevance: Any) -> Dict[str, Any]:
        if not isinstance(relevance, dict):
            return {}
        result: Dict[str, Any] = {}
        for entity, details in relevance.items():
            if not isinstance(details, dict):
                continue
            item: Dict[str, Any] = {}
            for key in ("task_related", "role", "source", "confidence"):
                value = details.get(key)
                if value not in (None, "", [], {}):
                    item[key] = value
            reason = details.get("reason")
            if reason:
                item["reason_omitted"] = True
            if item:
                result[str(entity)] = item
        return result

    def _extract_overlay_root_classes(self, html_content: str) -> str:
        match = re.search(r'<[^>]+id=["\']moreOverlay["\'][^>]*class=["\']([^"\']*)["\']', html_content or "", re.IGNORECASE)
        return match.group(1) if match else ""

    def _extract_overlay_snippet(self, html_content: str) -> str:
        snippet = html_content or ""
        if "moreOverlay" in snippet:
            start = snippet.find("moreOverlay")
            return snippet[max(0, start - 200): start + 5000]
        return snippet

    def _extract_overlay_navigation_targets(self, html_content: str) -> List[str]:
        targets: List[str] = []
        snippet = self._extract_overlay_snippet(html_content)
        for match in re.finditer(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", snippet):
            targets.append(match.group(1))
        for match in re.finditer(r"href=['\"]([^'\"]+)['\"]", snippet):
            targets.append(match.group(1))
        return targets

    def _extract_overlay_descendant_class_blob(self, html_content: str) -> str:
        snippet = self._extract_overlay_snippet(html_content)
        classes = []
        for match in re.finditer(r'class=["\']([^"\']*)["\']', snippet, re.IGNORECASE):
            classes.append(match.group(1))
        return " ".join(classes)

    def _detect_html_validity_issues(self, html_content: str) -> List[Dict[str, Any]]:
        interactive_tags = set(["button", "a", "label", "select", "input", "textarea", "summary"])
        void_tags = set([
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        ])

        class NestedInteractiveParser(HTMLParser):
            def __init__(self):
                HTMLParser.__init__(self)
                self.stack = []
                self.issues = []

            def handle_starttag(self, tag, attrs):
                tag = tag.lower()
                if tag in interactive_tags:
                    ancestor = None
                    for open_tag in reversed(self.stack):
                        if open_tag in interactive_tags:
                            if open_tag == "label" and tag in {"input", "select", "textarea"}:
                                continue
                            ancestor = open_tag
                            break
                    if ancestor:
                        self.issues.append({
                            "code": "html_validity.nested_interactive",
                            "severity": "critical",
                            "message": "Nested interactive elements are invalid HTML and can be reparented by the browser parser.",
                            "selector_hint": tag,
                            "evidence": "{} inside {}".format(tag, ancestor),
                            "expected_contract": "interactive elements must be siblings or use non-interactive wrappers; never nest button/a/label/select/input/textarea/summary",
                        })
                if tag not in void_tags:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                tag = tag.lower()
                for idx in range(len(self.stack) - 1, -1, -1):
                    if self.stack[idx] == tag:
                        del self.stack[idx:]
                        break

        parser = NestedInteractiveParser()
        try:
            parser.feed(html_content or "")
            parser.close()
        except Exception:
            return []
        return parser.issues

    _JS_UNESCAPED_ATTR_RE = re.compile(
        r"""(?P<open>["'])"""
        r"""[^"']*"""
        r"""data-ghostweb-click-contract-kind="""
        r"""(?P=open)"""
        r"""noninteractive"""
        r"""(?P=open)""",
    )

    def _detect_js_syntax_issues(self, html_content: str) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        script_re = re.compile(
            r"<script\b[^>]*>(?P<body>.*?)</script>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in script_re.finditer(html_content or ""):
            body = match.group("body").strip()
            if not body:
                continue
            if match.group(0).startswith("<script") and "src=" in match.group(0)[:200]:
                continue
            if self._JS_UNESCAPED_ATTR_RE.search(body):
                issues.append({
                    "code": "js_syntax.unescaped_html_attr_in_string",
                    "severity": "critical",
                    "message": (
                        "JS string contains unescaped HTML attribute quotes "
                        "(data-ghostweb-click-contract-kind=\"noninteractive\" "
                        "inside a JS string). Escape the inner quotes with "
                        "backslash or switch to single-quoted JS strings."
                    ),
                    "selector_hint": "<script>",
                    "evidence": "data-ghostweb-click-contract-kind=\"noninteractive\" inside JS string literal",
                })
        return issues

    def _detect_stateful_continuation_contract_issues(self,
                                                      html_content: str,
                                                      page_interfaces: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        identifier_aliases = self._collect_identifier_param_aliases(page_interfaces or [])
        if not identifier_aliases:
            return []

        interface_names = [
            str(iface.get("name", iface.get("function_name", "")) or "")
            for iface in page_interfaces or []
            if isinstance(iface, dict)
        ]
        has_form_contract = any("FormPageModel" in name for name in interface_names)
        has_update_contract = any(name.startswith("update") or name.startswith("save") for name in interface_names)
        if not (has_form_contract or has_update_contract):
            return []

        issues: List[Dict[str, Any]] = []
        missing_aliases = [alias for alias in identifier_aliases if alias not in (html_content or "")]
        if missing_aliases:
            issues.append({
                "code": "runtime_contract.missing_identifier_param_aliases",
                "severity": "critical",
                "message": "Stateful target page does not read every concrete selected-state query parameter alias.",
                "selector_hint": "script",
                "evidence": "missing aliases: {}".format(", ".join(missing_aliases)),
                "expected_contract": "Read every listed state-handle alias before deciding target variant or hydration state: {}".format(", ".join(identifier_aliases)),
            })

        variant_tokens = set(identifier_aliases)
        variant_tokens.update(["mode", "isEdit", "isEditMode", "selectedState", "selectedRecord"])
        token_pattern = "|".join(re.escape(token) for token in sorted(variant_tokens, key=len, reverse=True) if token)
        mutation_pattern = (
            r"(?:textContent|innerText|innerHTML|classList|hidden|"
            r"style\.[A-Za-z]+|setAttribute|removeAttribute|disabled)"
        )
        has_variant_binding = False
        if token_pattern:
            stateful_condition = r"(?:if\s*\([^)]*(?:{}).*?\)|(?:{})[^;]{{0,120}}\?)".format(
                token_pattern,
                token_pattern,
            )
            conditional_blocks = re.findall(
                stateful_condition + r"(?P<body>.{0,800})",
                html_content or "",
                flags=re.IGNORECASE | re.DOTALL,
            )
            has_variant_binding = any(
                re.search(mutation_pattern, block, flags=re.IGNORECASE)
                for block in conditional_blocks
            )
            has_variant_binding = has_variant_binding or bool(
                re.search(
                    mutation_pattern + r"[^;]{{0,240}}(?:{}|\?)".format(token_pattern),
                    html_content or "",
                    flags=re.IGNORECASE | re.DOTALL,
                )
            )
            has_variant_binding = has_variant_binding or bool(
                re.search(
                    r"(?:{}|\?)[^;]{{0,240}}".format(token_pattern) + mutation_pattern,
                    html_content or "",
                    flags=re.IGNORECASE | re.DOTALL,
                )
            )

        if has_form_contract and not has_variant_binding:
            issues.append({
                "code": "runtime_contract.stateful_variant_visual_mode",
                "severity": "major",
                "message": "Stateful target page supports selected-state identifiers but does not bind visible UI variant to the selected state handle.",
                "selector_hint": "script",
                "evidence": "no visible UI mutation driven by state-handle aliases or mode",
                "expected_contract": "When any selected-state handle alias is present, use the state handle or derived mode/isEdit value to switch visible chrome/actions/classes and hydrate from WebsiteSDK.",
            })

        return issues

    def _detect_overlay_contract_issues(self,
                                        html_content: str,
                                        page_evidence: Dict[str, Any],
                                        valid_filenames: List[str]) -> List[Dict[str, Any]]:
        component_contracts = (page_evidence or {}).get("component_contracts", []) if isinstance((page_evidence or {}).get("component_contracts", []), list) else []
        floating_component = next((item for item in component_contracts if isinstance(item, dict) and str(item.get("component_type", "")).strip() == "floating_component"), {})
        floating_component = floating_component or (page_evidence or {}).get("floating_component", {}) or (page_evidence or {}).get("overlay_policy", {}) or {}
        if not floating_component:
            return []

        issues: List[Dict[str, Any]] = []
        anchor = str(floating_component.get("trigger_element_id") or floating_component.get("overlay_anchor", "")).strip() or "btn_更多"
        html = html_content or ""
        lower_html = html.lower()
        overlay_classes = self._extract_overlay_root_classes(html)
        overlay_classes_lower = overlay_classes.lower()
        overlay_descendant_classes = self._extract_overlay_descendant_class_blob(html)
        overlay_descendant_classes_lower = overlay_descendant_classes.lower()
        floating_classes_lower = f"{overlay_classes_lower} {overlay_descendant_classes_lower}".strip()
        nav_targets = self._extract_overlay_navigation_targets(html)
        overlay_items = [str(item).strip() for item in floating_component.get("items", []) if str(item).strip()]

        if f'data-element-id="{anchor}"' not in html and f"data-element-id='{anchor}'" not in html:
            issues.append({
                "code": "overlay.anchor_missing",
                "severity": "critical",
                "message": f"Missing overlay anchor data-element-id={anchor}.",
                "selector_hint": f'[data-element-id="{anchor}"]',
                "evidence": "anchor element not found",
                "expected_contract": "overlay anchor must exist so the overlay can attach to the correct footer/button region",
            })

        if 'id="moreOverlay"' not in html and "id='moreOverlay'" not in html:
            issues.append({
                "code": "overlay.root_missing",
                "severity": "critical",
                "message": "Missing #moreOverlay root element.",
                "selector_hint": "#moreOverlay",
                "evidence": "overlay root not found",
                "expected_contract": "overlay root element must exist and be toggled from the more button",
            })
            return issues

        if not any(token in floating_classes_lower for token in ("absolute", "fixed")):
            issues.append({
                "code": "overlay.in_flow",
                "severity": "critical",
                "message": "Overlay root/panel is still an in-flow block instead of a floating layer.",
                "selector_hint": "#moreOverlay",
                "evidence": overlay_descendant_classes or overlay_classes or "missing absolute/fixed class",
                "expected_contract": "overlay root or panel must use absolute/fixed positioning so it does not reflow main/footer",
            })

        anchor_idx = lower_html.find(anchor.lower())
        window_start = max(0, anchor_idx - 400) if anchor_idx != -1 else 0
        window_end = anchor_idx + 400 if anchor_idx != -1 else 0
        anchor_window = lower_html[window_start:window_end] if anchor_idx != -1 else ""
        if anchor_window and "relative" not in anchor_window:
            issues.append({
                "code": "overlay.anchor_container_missing",
                "severity": "critical",
                "message": "Anchor container near btn_更多 is missing relative positioning.",
                "selector_hint": f'[data-element-id="{anchor}"]',
                "evidence": anchor_window[:200],
                "expected_contract": "anchor container must be relative so the overlay can anchor above footer right",
            })

        if "scrollintoview(" in lower_html:
            issues.append({
                "code": "overlay.scroll_into_view",
                "severity": "critical",
                "message": "Overlay uses scrollIntoView which indicates a fake in-flow overlay implementation.",
                "selector_hint": "#moreOverlay",
                "evidence": "scrollIntoView(",
                "expected_contract": "overlay must open in place without scrolling the viewport",
            })

        if floating_component.get("overlay_position") == "above_footer_right":
            has_bottom = any(token in floating_classes_lower for token in ("bottom-full", "bottom-[", "bottom-0"))
            has_right = any(token in floating_classes_lower for token in ("right-0", "right-["))
            if not (has_bottom and has_right):
                issues.append({
                    "code": "overlay.position_mismatch",
                    "severity": "critical",
                    "message": "Overlay is not anchored above the footer right region as required.",
                    "selector_hint": "#moreOverlay",
                    "evidence": overlay_descendant_classes or overlay_classes or "missing position classes",
                    "expected_contract": "overlay should use an anchored structure like bottom-full right-0 mb-2 z-20",
                })

        main_start = lower_html.find("<main")
        footer_start = lower_html.find("<footer")
        footer_end = lower_html.find("</footer>", footer_start) if footer_start != -1 else -1
        footer_html = lower_html[footer_start:footer_end + 9] if footer_start != -1 and footer_end != -1 else ""
        overlay_pos = lower_html.find("moreoverlay")
        if main_start != -1 and footer_start != -1 and overlay_pos != -1 and main_start < overlay_pos < footer_start and not any(token in floating_classes_lower for token in ("absolute", "fixed")):
            issues.append({
                "code": "overlay.reflow_risk",
                "severity": "critical",
                "message": "Overlay is embedded inside main flow and is likely to change layout height when shown.",
                "selector_hint": "#moreOverlay",
                "evidence": overlay_descendant_classes or overlay_classes or "overlay appears before footer without floating positioning",
                "expected_contract": "overlay must float above footer/content without affecting main/footer layout",
            })
        if floating_component.get("overlay_position") == "above_footer_right" and footer_html:
            footer_has_more = 'aria-label="更多"' in footer_html or '>更多<' in footer_html
            footer_has_anchor = f'data-element-id="{anchor.lower()}"' in footer_html or f"data-element-id='{anchor.lower()}'" in footer_html
            if footer_has_more and not footer_has_anchor:
                issues.append({
                    "code": "overlay.footer_anchor_missing",
                    "severity": "critical",
                    "message": "Footer has a visible 更多 button, but it is not the overlay anchor.",
                    "selector_hint": "footer",
                    "evidence": footer_html[:220],
                    "expected_contract": "for above_footer_right overlays, the visible footer 更多 button should be the anchor and trigger the overlay",
                })
            anchor_pos = lower_html.find(anchor.lower())
            if footer_has_more and anchor_pos != -1 and main_start != -1 and anchor_pos < main_start:
                issues.append({
                    "code": "overlay.anchor_region_mismatch",
                    "severity": "critical",
                    "message": "Overlay anchor is in the header/body instead of the footer more button region.",
                    "selector_hint": f'[data-element-id="{anchor}"]',
                    "evidence": html[max(0, anchor_pos - 120):anchor_pos + 220],
                    "expected_contract": "for above_footer_right overlays, anchor should live in the footer/button region rather than header chrome",
                })

        nav_by_trigger = {}
        for item in (page_evidence or {}).get("outbound_edges", []):
            if not isinstance(item, dict):
                continue
            trigger = str(item.get("trigger", "")).strip()
            to_filename = str(item.get("to_filename", "")).strip()
            if not to_filename:
                continue
            trigger_tail = trigger.split("->")[-1].strip()
            if trigger_tail.startswith("点击"):
                trigger_tail = trigger_tail.replace("点击", "", 1).strip()
            elif trigger.startswith("点击"):
                trigger_tail = trigger.replace("点击", "", 1).strip()
            if trigger_tail:
                nav_by_trigger[trigger_tail] = to_filename
        for item_name in overlay_items:
            expected = nav_by_trigger.get(item_name)
            if expected and expected not in nav_targets:
                issues.append({
                    "code": "overlay.navigation_mismatch",
                    "severity": "critical",
                    "message": f"Overlay item '{item_name}' is missing navigation to {expected}.",
                    "selector_hint": "#moreOverlay",
                    "evidence": ", ".join(nav_targets) or "no navigation targets found",
                    "expected_contract": f"overlay item '{item_name}' should navigate to {expected}",
                })
        invalid_targets = sorted(target for target in nav_targets if target.endswith(".html") and target not in set(valid_filenames))
        if invalid_targets:
            issues.append({
                "code": "overlay.navigation_invalid_target",
                "severity": "critical",
                "message": "Overlay contains navigation targets outside the valid filename set.",
                "selector_hint": "#moreOverlay",
                "evidence": ", ".join(invalid_targets),
                "expected_contract": f"overlay must use only valid filenames: {', '.join(valid_filenames)}",
            })
        return issues

    def _format_fix_issues(self, issues: List[Dict[str, Any]]) -> str:
        return "\n".join(
            f"{idx}. [{issue.get('code', 'issue')}] {issue.get('message', '')} (selector: {issue.get('selector_hint', '')}; evidence: {issue.get('evidence', '')}; expected: {issue.get('expected_contract', '')})"
            for idx, issue in enumerate(issues, 1)
        )

    @staticmethod
    def _page_interface_method_names(page_interfaces: List[Dict[str, Any]] = None) -> set:
        methods = set()
        for interface in page_interfaces or []:
            if isinstance(interface, str):
                if interface.strip():
                    methods.add(interface.strip())
                continue
            if not isinstance(interface, dict):
                continue
            for key in ("name", "method", "method_name", "function_name"):
                value = interface.get(key)
                if isinstance(value, str) and value.strip():
                    methods.add(value.strip())
        return methods

    @staticmethod
    def _html_websitesdk_calls(html_content: str) -> set:
        return set(re.findall(r"\b(?:window\.)?WebsiteSDK\.([A-Za-z_$][\w$]*)\s*\(", html_content or ""))

    @staticmethod
    def _action_slot_sdk_side_effect_methods(page_evidence: Dict[str, Any]) -> set:
        scope = (page_evidence or {}).get("pagegen_input_scope")
        slots = scope.get("route_action_slots") if isinstance(scope, dict) else []
        if not isinstance(slots, list):
            return set()
        methods = set()
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            side_effect = str(slot.get("sdk_side_effect") or "")
            match = re.match(r"WebsiteSDK\.([A-Za-z_$][\w$]*)$", side_effect)
            if match:
                methods.add(match.group(1))
        return methods

    def _detect_pagegen_dom_contract_issues(
        self,
        filename: str,
        html_content: str,
        page_evidence: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        spec = (
            (page_evidence or {}).get("page_environment_projection")
            or (page_evidence or {}).get("page_environment_spec")
        )
        if not isinstance(spec, dict):
            return []
        scope = (page_evidence or {}).get("pagegen_input_scope")
        surfaces = (
            scope.get("required_interaction_surfaces")
            if isinstance(scope, dict)
            else []
        )
        if not isinstance(surfaces, list):
            surfaces = []
        required: List[Dict[str, Any]] = []
        for s in surfaces:
            if not isinstance(s, dict):
                continue
            cid = str(s.get("required_data_element_id") or s.get("surface_id") or "").strip()
            if not cid:
                continue
            policy = str(s.get("initial_visibility_policy") or "").strip().lower()
            required.append(
                {
                    "id": cid,
                    "initial_hidden": (
                        policy in {"hidden_until_transition", "runtime_only", "blocked_unknown"}
                        or s.get("initial_dom_eligible") is False
                    ),
                    "policy": policy,
                    "interactive_required": s.get("interactive_required") is not False,
                    "action_ids": self._surface_action_keys_for_component_ref(s),
                }
            )
        if not required:
            components = spec.get("components", [])
            if not isinstance(components, list):
                return []
            required = [
                {
                    "id": str(c.get("component_id") or ""),
                    "initial_hidden": False,
                    "policy": "visible_initial",
                    "interactive_required": True,
                    "action_ids": [
                        str(step_key)
                        for step_key in (c.get("source_step_keys") or [])
                        if str(step_key or "").strip()
                    ],
                }
                for c in components
                if (
                    isinstance(c, dict)
                    and str(c.get("component_role") or "").lower() != "l0_execution_anchor"
                    and c.get("interaction_required")
                    and c.get("component_id")
                )
            ]
        if not required:
            return []
        try:
            from trace2world_pipeline.page_realizer.pagegen_dom_contract_validator import (
                _looks_hidden_or_tiny,
                _looks_interactive,
                _static_dom_scan_html,
                _tag_records_by_data_element_id,
            )
        except Exception:
            return []
        tag_records = _tag_records_by_data_element_id(html_content or "")
        counts: Dict[str, int] = {
            fid: len(records)
            for fid, records in tag_records.items()
        }
        issues: List[Dict[str, Any]] = []
        for item in required:
            cid = str(item.get("id") or "").strip()
            if not cid:
                continue
            initial_hidden = bool(item.get("initial_hidden"))
            n = counts.get(cid, 0)
            if n == 0:
                expected_action_ids = {
                    str(action_id or "").strip()
                    for action_id in (item.get("action_ids") or [])
                    if str(action_id or "").strip()
                }
                if (
                    initial_hidden
                    and bool(item.get("interactive_required", True))
                    and self._has_state_dependent_dynamic_host_binding(
                        html_content,
                        component_id=cid,
                        action_ids=expected_action_ids,
                    )
                ):
                    continue
                if initial_hidden:
                    message = (
                        f'Add data-element-id="{cid}" to the initially hidden state '
                        "surface that corresponds to this PES component. Use the EXACT "
                        "opaque component_id string. The element may be hidden in initial "
                        "DOM, but it must be the same stable state surface that becomes "
                        "available when the required runtime state is active. If that real "
                        "host already carries data-element-id for an equivalent required "
                        "surface, preserve its primary id and add this exact id to the same "
                        "host's whitespace-separated data-element-aliases; do not replace "
                        "one required identity with another."
                    )
                    selector_hint = "initially hidden state surface matching the component intent"
                    expected_contract = (
                        f'exactly one initially hidden element with data-element-id="{cid}"'
                    )
                else:
                    message = (
                        f'Add data-element-id="{cid}" to the visible interactive element '
                        "that corresponds to this PES component. Use the EXACT opaque "
                        "component_id string — no renaming, truncation, or suffix. "
                        "If that visible element already has a data-element-id for an "
                        "equivalent required surface, keep one primary data-element-id "
                        "and put this exact id in whitespace-separated data-element-aliases; "
                        "never add a second data-element-id attribute to the same element."
                    )
                    selector_hint = "interactive element matching the component's intent and screenshot position"
                    expected_contract = f'exactly one element with data-element-id="{cid}"'
                issues.append({
                    "code": "pagegen_dom_contract.missing_data_element_id",
                    "message": message,
                    "selector_hint": selector_hint,
                    "evidence": f'data-element-id="{cid}" not found in HTML',
                    "expected_contract": expected_contract,
                })
            elif n > 1:
                issues.append({
                    "code": "pagegen_dom_contract.ambiguous_data_element_id",
                    "message": (
                        f'data-element-id="{cid}" appears {n} times; the contract requires '
                        "exactly one occurrence. Keep it only on the canonical visible "
                        "element and remove all duplicates."
                    ),
                    "selector_hint": f'[data-element-id="{cid}"]',
                    "evidence": f"{n} occurrences in HTML",
                    "expected_contract": f'exactly one element with data-element-id="{cid}"',
                })
            else:
                records = tag_records.get(cid, [])
                tag_record = records[0] if records else {}
                interactive_required = bool(item.get("interactive_required", True))
                expected_action_ids = {
                    str(action_id or "").strip()
                    for action_id in (item.get("action_ids") or [])
                    if str(action_id or "").strip()
                }
                actual_action_ids = {
                    str(tag_record.get("data_action_id") or "").strip(),
                    *str(tag_record.get("data_action_aliases") or "").split(),
                }
                actual_action_ids.discard("")
                if initial_hidden and interactive_required and not _looks_interactive(tag_record):
                    issues.append({
                        "code": "pagegen_dom_contract.noninteractive_interaction_host",
                        "message": (
                            f'data-element-id="{cid}" is attached to a detached/static node. '
                            "Attach it to the real actionable control, including when the "
                            "control is initially hidden; an empty hidden stub is not a host."
                        ),
                        "selector_hint": f'[data-element-id="{cid}"]',
                        "evidence": json.dumps(
                            {
                                "tag": tag_record.get("tag", ""),
                                "class": tag_record.get("class", ""),
                                "role": tag_record.get("role", ""),
                                "initial_hidden": True,
                            },
                            ensure_ascii=False,
                        ),
                        "expected_contract": (
                            "The stable lifecycle host must itself be actionable; detached "
                            "metadata stubs cannot satisfy a DOM/action obligation."
                        ),
                    })
                if (
                    interactive_required
                    and expected_action_ids
                    and not expected_action_ids.intersection(actual_action_ids)
                ):
                    issues.append({
                        "code": "pagegen_dom_contract.missing_colocated_action_id",
                        "message": (
                            f'data-element-id="{cid}" is not co-located with its exact '
                            "data-action-id/data-action-aliases on the same real host."
                        ),
                        "selector_hint": f'[data-element-id="{cid}"]',
                        "evidence": json.dumps(
                            {
                                "expected_action_ids": sorted(expected_action_ids),
                                "actual_action_ids": sorted(actual_action_ids),
                                "initial_hidden": initial_hidden,
                            },
                            ensure_ascii=False,
                        ),
                        "expected_contract": (
                            "The component identity and action identity form one relational "
                            "host binding; neither may live on a detached sibling/stub."
                        ),
                    })
                if initial_hidden:
                    continue
                hidden_reason = _looks_hidden_or_tiny(tag_record)
                if hidden_reason:
                    issues.append({
                        "code": "pagegen_dom_contract.hidden_or_tiny_interaction_host",
                        "message": (
                            f'data-element-id="{cid}" is attached to a hidden, transparent, '
                            "or tiny element. Move the exact data-element-id onto the real "
                            "visible clickable/input host for this component, and remove the "
                            "hidden duplicate/hotspot."
                        ),
                        "selector_hint": f'[data-element-id="{cid}"]',
                        "evidence": json.dumps(
                            {
                                "reason": hidden_reason,
                                "tag": tag_record.get("tag", ""),
                                "class": tag_record.get("class", ""),
                                "style": tag_record.get("style", ""),
                            },
                            ensure_ascii=False,
                        ),
                        "expected_contract": (
                            "The unique data-element-id must be on a visible, normal-size "
                            "interactive surface. Do not satisfy L0 with opacity-0, hidden, "
                            "sr-only, w-0/h-0, or 1px hotspot nodes."
                        ),
                    })
                elif interactive_required and not _looks_interactive(tag_record):
                    issues.append({
                        "code": "pagegen_dom_contract.noninteractive_interaction_host",
                        "message": (
                            f'data-element-id="{cid}" is attached to a static DOM node. '
                            "Move the exact data-element-id to the visible actionable host "
                            "or convert that host to a button/link/input with the existing "
                            "navigation or reducer behavior."
                        ),
                        "selector_hint": f'[data-element-id="{cid}"]',
                        "evidence": json.dumps(
                            {
                                "tag": tag_record.get("tag", ""),
                                "class": tag_record.get("class", ""),
                                "role": tag_record.get("role", ""),
                            },
                            ensure_ascii=False,
                        ),
                        "expected_contract": (
                            "The host must be a real visible interactive element: button, "
                            "a[href], input/select/textarea, or an element with role/tabindex/"
                            "onclick/data-route behavior."
                        ),
                    })
        return issues

    @staticmethod
    def _detect_hydration_dom_contract_issues(
        filename: str,
        html_content: str,
        page_evidence: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        scope = (page_evidence or {}).get("pagegen_input_scope")
        render_view = scope.get("pagegen_render_view") if isinstance(scope, dict) else None
        contract = render_view.get("hydration_dom_contract") if isinstance(render_view, dict) else None
        if not isinstance(contract, dict) or not contract.get("field_bindings"):
            return []

        required: List[Tuple[str, str, str]] = []
        collection_bindings: List[Dict[str, Any]] = []
        root = contract.get("root_attribute")
        if isinstance(root, dict) and root.get("name") and root.get("value"):
            required.append((str(root["name"]), str(root["value"]), "page hydration root"))
        scalar_attribute = str(contract.get("scalar_attribute") or "data-ghostweb-field")
        collection_attribute = str(contract.get("collection_attribute") or "data-ghostweb-collection")
        template_attribute = str(
            contract.get("collection_template_attribute") or "data-ghostweb-collection-template"
        )
        for binding in contract.get("field_bindings") or []:
            if not isinstance(binding, dict) or not str(binding.get("path") or "").strip():
                continue
            path = str(binding["path"])
            if str(binding.get("kind") or "") == "collection":
                required.append((collection_attribute, path, "visible collection host"))
                collection_bindings.append(binding)
            else:
                required.append((scalar_attribute, path, "visible value host"))

        issues: List[Dict[str, Any]] = []
        for attribute, value, host_kind in required:
            pattern = rf"\b{re.escape(attribute)}\s*=\s*(['\"]){re.escape(value)}\1"
            if re.search(pattern, html_content or ""):
                continue
            exact = f'{attribute}="{value}"'
            issues.append(
                {
                    "code": "hydration_dom.missing_binding",
                    "severity": "critical",
                    "message": (
                        f"Add the exact {exact} binding to a {host_kind} in {filename}; "
                        "preserve every existing hydration binding."
                    ),
                    "selector_hint": host_kind,
                    "evidence": f"exact attribute {exact} is absent",
                    "expected_contract": (
                        f"one visible owning host contains exact {exact}; do not rename the path"
                    ),
                }
            )
        for binding in collection_bindings:
            path = str(binding["path"])
            template_pattern = re.compile(
                rf"<template\b[^>]*\b{re.escape(template_attribute)}\s*=\s*"
                rf"(['\"]){re.escape(path)}\1[^>]*>(?P<body>[\s\S]*?)</template\s*>",
                re.IGNORECASE,
            )
            template_match = template_pattern.search(html_content or "")
            if not template_match:
                exact = f'<template {template_attribute}="{path}">'
                issues.append(
                    {
                        "code": "hydration_dom.missing_binding",
                        "severity": "critical",
                        "message": (
                            f"Add one real HTML template element beginning with exact {exact} "
                            f"inside the {path} collection host in {filename}; a div carrying "
                            "the attribute does not satisfy the runtime contract."
                        ),
                        "selector_hint": f"template for collection {path}",
                        "evidence": f"real template tag for {path} is absent",
                        "expected_contract": exact,
                    }
                )
                continue
            template_body = template_match.group("body")
            item_attribute = str(
                contract.get("collection_item_attribute") or "data-ghostweb-item-field"
            )
            duplicate_binding = False
            for opening_tag in re.findall(r"<[^!/][^>]*>", template_body):
                if len(re.findall(rf"\b{re.escape(item_attribute)}\s*=", opening_tag)) > 1:
                    duplicate_binding = True
                    break
            if duplicate_binding:
                issues.append(
                    {
                        "code": "hydration_dom.missing_binding",
                        "severity": "critical",
                        "message": (
                            f"The real {path} template in {filename} repeats {item_attribute} "
                            "on one HTML element. Browsers keep only one value. Put every "
                            "declared item field on its own visible descendant host."
                        ),
                        "selector_hint": f"template for collection {path}",
                        "evidence": f"duplicate {item_attribute} attributes occur on one tag",
                        "expected_contract": "one distinct DOM host per declared collection item field",
                    }
                )
            for item_field in binding.get("item_fields") or []:
                item_pattern = rf"\b{re.escape(item_attribute)}\s*=\s*(['\"]){re.escape(str(item_field))}\1"
                if re.search(item_pattern, template_body):
                    continue
                exact = f'{item_attribute}="{item_field}"'
                issues.append(
                    {
                        "code": "hydration_dom.missing_binding",
                        "severity": "critical",
                        "message": (
                            f"Add exact {exact} to a visible repeated-item value host inside "
                            f"the real {path} template in {filename}."
                        ),
                        "selector_hint": f"template for collection {path}",
                        "evidence": f"exact repeated-item binding {exact} is absent",
                        "expected_contract": exact,
                    }
                )
            for action_host_id in binding.get("required_action_host_authority_ids") or []:
                action_pattern = rf'\bdata-action-id\s*=\s*(["\']){re.escape(str(action_host_id))}\1'
                if re.search(action_pattern, template_body):
                    continue
                exact = f'data-action-id="{action_host_id}"'
                issues.append(
                    {
                        "code": "hydration_dom.missing_binding",
                        "severity": "critical",
                        "message": (
                            f"Add exact {exact} to the visible interactive repeated-row host "
                            f"inside the real {path} template in {filename}."
                        ),
                        "selector_hint": f"template for collection {path}",
                        "evidence": f"collection navigation action host {exact} is absent",
                        "expected_contract": exact,
                    }
                )
        return issues

    @staticmethod
    def _has_state_dependent_dynamic_host_binding(
        html_content: str,
        *,
        component_id: str,
        action_ids: set[str],
    ) -> bool:
        html = html_content or ""
        cid = str(component_id or "").strip()
        if not cid or not action_ids:
            return False
        from trace2world_pipeline.page_realizer.pagegen_dom_contract_validator import _static_dom_scan_html

        for match in re.finditer(
            r"(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*"
            r"document\.getElementById\(\s*(['\"])(?P<id>[^'\"]+)\2\s*\)",
            html,
        ):
            var_name = match.group("var")
            host_id = match.group("id")
            if not re.search(
                rf"<[^>]+\bid\s*=\s*(['\"]){re.escape(host_id)}\1",
                _static_dom_scan_html(html),
                re.IGNORECASE,
            ):
                continue
            component_pattern = re.compile(
                rf"\b{re.escape(var_name)}\.setAttribute\(\s*(['\"])data-element-(?:id|aliases)\1\s*,\s*(['\"]){re.escape(cid)}\2\s*\)",
                re.DOTALL,
            )
            if not component_pattern.search(html):
                continue
            for action_id in action_ids:
                action_pattern = re.compile(
                    rf"\b{re.escape(var_name)}\.setAttribute\(\s*(['\"])data-action-(?:id|aliases)\1\s*,\s*(['\"]){re.escape(action_id)}\2\s*\)",
                    re.DOTALL,
                )
                if action_pattern.search(html):
                    return True
        return False

    @staticmethod
    def _route_target_pattern(target_filename: str) -> str:
        escaped = re.escape(str(target_filename or "").strip())
        return rf"{escaped}(?:[?#][^'\"\s<>)]*)?"

    @staticmethod
    def _strip_html_text(value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value or "")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _extract_footer_nav_targets_for_boundary(
        cls,
        filename: str,
        html_content: str,
    ) -> List[Dict[str, Any]]:
        targets: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, str]] = set()
        block_pattern = re.compile(
            r"<(?P<surface>footer|nav)\b[^>]*>.*?</(?P=surface)>",
            re.IGNORECASE | re.DOTALL,
        )
        target_patterns = [
            re.compile(
                r"<a\b(?P<attrs>[^>]*\bhref\s*=\s*['\"](?P<target>[^'\"]+?\.html(?:[?#][^'\"]*)?)['\"][^>]*)>(?P<label>.*?)</a>",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"<(?P<tag>[A-Za-z][\w:-]*)\b(?P<attrs>[^>]*\bdata-ghostweb-target\s*=\s*['\"](?P<target>[^'\"]+?\.html(?:[?#][^'\"]*)?)['\"][^>]*)>(?P<label>.*?)</(?P=tag)>",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"GhostwebRouteExecution\.navigate\(\s*['\"](?P<target>[^'\"]+?\.html(?:[?#][^'\"]*)?)['\"]\s*\)",
                re.IGNORECASE,
            ),
            re.compile(
                r"window\.location\.href\s*=\s*['\"](?P<target>[^'\"]+?\.html(?:[?#][^'\"]*)?)['\"]",
                re.IGNORECASE,
            ),
        ]
        for block_match in block_pattern.finditer(html_content or ""):
            surface = block_match.group("surface").lower()
            block = block_match.group(0)
            for pattern in target_patterns:
                for match in pattern.finditer(block):
                    target = str(match.group("target") or "").strip()
                    label = cls._strip_html_text(match.groupdict().get("label", ""))
                    key = (surface, target, label)
                    if key in seen:
                        continue
                    seen.add(key)
                    targets.append(
                        {
                            "source_filename": filename,
                            "surface": surface,
                            "target_filename": target,
                            "label": label,
                        }
                    )
        return targets

    _HIDDEN_ATTR_INDICATORS = re.compile(
        r"""\b(?:hidden|display\s*:\s*none|aria-hidden\s*=\s*["']true["']|class\s*=\s*["'][^"']*\bhidden\b)""",
        re.IGNORECASE,
    )
    _HTML_TAG_BOUNDARY_RE = re.compile(
        r"<(?P<closing>/)?(?P<tag>[A-Za-z][\w:-]*)\b(?P<attrs>[^>]*)>",
        re.IGNORECASE | re.DOTALL,
    )
    _VOID_HTML_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    @classmethod
    def _record_has_initial_hidden_self_or_ancestor(
        cls,
        scan_html: str,
        record: Dict[str, Any],
    ) -> bool:
        attrs = str(record.get("attrs") or "")
        if cls._HIDDEN_ATTR_INDICATORS.search(attrs):
            return True
        try:
            start = int(record.get("start") or 0)
        except (TypeError, ValueError):
            start = 0
        stack: List[Tuple[str, bool]] = []
        for match in cls._HTML_TAG_BOUNDARY_RE.finditer((scan_html or "")[:max(start, 0)]):
            tag = str(match.group("tag") or "").lower()
            if not tag:
                continue
            if match.group("closing"):
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index][0] == tag:
                        del stack[index:]
                        break
                continue
            raw_attrs = str(match.group("attrs") or "")
            hidden = bool(cls._HIDDEN_ATTR_INDICATORS.search(raw_attrs))
            self_closing = raw_attrs.rstrip().endswith("/") or tag in cls._VOID_HTML_TAGS
            if not self_closing:
                stack.append((tag, hidden))
        return any(hidden for _tag, hidden in stack)

    def _detect_initial_hidden_surface_leak_issues(
        self,
        filename: str,
        html_content: str,
        page_evidence: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        scope = (page_evidence or {}).get("pagegen_input_scope")
        surfaces = (
            scope.get("required_interaction_surfaces")
            if isinstance(scope, dict)
            else []
        )
        if not isinstance(surfaces, list):
            return []
        hidden_ids: List[str] = []
        for s in surfaces:
            if not isinstance(s, dict):
                continue
            policy = str(s.get("initial_visibility_policy") or "")
            eligible = s.get("initial_dom_eligible")
            if policy == "hidden_until_transition" or eligible is False:
                sid = str(s.get("required_data_element_id") or s.get("surface_id") or "")
                if sid:
                    hidden_ids.append(sid)
        if not hidden_ids:
            return []
        try:
            from trace2world_pipeline.page_realizer.pagegen_dom_contract_validator import (
                _static_dom_scan_html,
                _tag_records_by_data_element_id,
            )
        except Exception:
            return []
        scan_html = _static_dom_scan_html(html_content or "")
        tag_records = _tag_records_by_data_element_id(html_content or "")
        issues: List[Dict[str, Any]] = []
        for cid in hidden_ids:
            records = tag_records.get(cid, [])
            if not records:
                continue
            for record in records[:1]:
                if not self._record_has_initial_hidden_self_or_ancestor(scan_html, record):
                    attrs = str(record.get("attrs") or "")
                    issues.append({
                        "code": "initial_hidden_surface.visible_in_initial_dom",
                        "severity": "critical",
                        "message": (
                            f"Surface {cid} has initial_visibility_policy=hidden_until_transition "
                            f"but is visible in initial DOM. Add hidden/display:none/class=hidden "
                            f"to the element or its container."
                        ),
                        "selector_hint": f'[data-element-id="{cid}"]',
                        "evidence": json.dumps({"component_id": cid, "attrs": attrs[:300]}, ensure_ascii=False),
                    })
        return issues

    def _detect_route_authority_boundary_issues(
        self,
        filename: str,
        html_content: str,
        page_evidence: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        generated_targets = self._extract_footer_nav_targets_for_boundary(filename, html_content)
        if not generated_targets:
            return []
        scope = (page_evidence or {}).get("pagegen_input_scope")
        scope = scope if isinstance(scope, dict) else {}
        try:
            from trace2world_pipeline.page_realizer.pagegen_prompt_boundary import validate_pagegen_prompt_boundary
        except Exception:
            return []
        report = validate_pagegen_prompt_boundary(
            {
                "page_environment_spec": (
                    (page_evidence or {}).get("page_environment_projection")
                    or (page_evidence or {}).get("page_environment_spec")
                    or {}
                ),
                "global_navigation": (page_evidence or {}).get("global_navigation")
                or (page_evidence or {}).get("global_nav")
                or {},
                "route_affordances_final": (page_evidence or {}).get("route_affordances_final") or {},
                "route_action_slots": scope.get("route_action_slots", []),
                "action_slot_route_contract": scope.get("action_slot_route_contract", {}),
                "generated_navigation_targets": generated_targets,
            }
        )
        issues: List[Dict[str, Any]] = []
        for violation in report.get("violations", []) or []:
            if not isinstance(violation, dict):
                continue
            if violation.get("code") != "navigation_target.missing_route_authority":
                continue
            label = violation.get("label") or "visible footer/nav control"
            target = violation.get("target_filename") or ""
            issues.append(
                {
                    "code": "navigation_target.missing_route_authority",
                    "message": (
                        f"Footer/nav control '{label}' routes to {target} without explicit route authority. "
                        "Keep it visual-only/current-page inactive or bind it to a route_action_slot, "
                        "action_slot_route_contract, route_affordances_final, or page_environment_spec route."
                    ),
                    "selector_hint": "footer, nav",
                    "evidence": json.dumps(violation, ensure_ascii=False),
                    "expected_contract": (
                        "global_navigation empty is no navigation authority; do not infer footer/nav "
                        "targets from labels, page titles, site_pages, or filenames."
                    ),
                }
            )
        return issues

    def _detect_action_slot_route_executor_issues(
        self,
        filename: str,
        html_content: str,
        page_evidence: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        scope = (page_evidence or {}).get("pagegen_input_scope")
        route_slots = (
            scope.get("route_action_slots")
            if isinstance(scope, dict)
            else []
        )
        if not isinstance(route_slots, list) or not route_slots:
            return []
        try:
            from trace2world_pipeline.page_realizer.pagegen_dom_contract_validator import (
                _attr_value,
                _tag_records_by_data_element_id,
            )
        except Exception:
            return []

        tag_records = _tag_records_by_data_element_id(html_content or "")
        issues: List[Dict[str, Any]] = []
        for slot in route_slots:
            if not isinstance(slot, dict):
                continue
            component_id = str(slot.get("component_id") or "").strip()
            target_filename = str(slot.get("target_filename") or "").strip()
            if not component_id or not target_filename:
                continue
            records = tag_records.get(component_id, [])
            if not records:
                issues.append(
                    {
                        "code": "action_slot_route.missing_route_host",
                        "message": (
                            "Render the visible action slot route host with the exact "
                            "component_id as data-element-id or data-element-aliases, "
                            "then bind it through GhostwebRouteExecution.navigate(target_filename)."
                        ),
                        "selector_hint": f'[data-element-id="{component_id}"]',
                        "evidence": json.dumps(
                            {
                                "filename": filename,
                                "component_id": component_id,
                                "target_filename": target_filename,
                                "control_label": slot.get("control_label") or slot.get("label"),
                                "action_slot_id": slot.get("action_slot_id"),
                            },
                            ensure_ascii=False,
                        ),
                        "expected_contract": json.dumps(
                            {
                                "action_slot_id": slot.get("action_slot_id"),
                                "component_id": component_id,
                                "target_filename": target_filename,
                                "control_label": slot.get("control_label") or slot.get("label"),
                                "executor": "GhostwebRouteExecution.navigate",
                                "sdk_side_effect": "WebsiteSDK.navigateRoute",
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                continue
        return issues

    def _detect_raw_back_navigation_bypass_issues(
        self,
        html_content: str,
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        patterns = [
            (r"history\.back\s*\(\s*\)", "history.back()"),
            (r"history\.go\s*\(\s*-\d+\s*\)", "history.go(-N)"),
        ]
        for pattern, label in patterns:
            if re.search(pattern, html_content or ""):
                wrapped = re.search(
                    r"GhostwebRouteExecution\.returnBack",
                    html_content or "",
                )
                if not wrapped:
                    issues.append({
                        "code": "pagegen_raw_back_runtime_bypass",
                        "message": (
                            f"Raw {label} detected without GhostwebRouteExecution.returnBack() wrapper. "
                            "Back navigation must use GhostwebRouteExecution.returnBack(action_id)."
                        ),
                        "selector_hint": "script",
                        "evidence": label,
                    })
        return issues

    def _detect_undeclared_sdk_call_issues(
        self,
        html_content: str,
        page_interfaces: List[Dict[str, Any]] = None,
        extra_allowed_methods: Iterable[str] = (),
    ) -> List[Dict[str, Any]]:
        allowed_methods = self._page_interface_method_names(page_interfaces)
        allowed_methods.update(str(item) for item in (extra_allowed_methods or []) if item)
        calls = self._html_websitesdk_calls(html_content)
        undeclared = sorted(calls - allowed_methods)
        if not undeclared:
            return []
        return [
            {
                "code": "sdk_call_surface.undeclared_website_sdk_call",
                "message": "Remove or replace WebsiteSDK calls that are not declared by the current page's SDK Interfaces.",
                "selector_hint": "script",
                "evidence": ", ".join(undeclared[:24]),
                "expected_contract": json.dumps(
                    {
                        "allowed_website_sdk_methods": sorted(allowed_methods),
                        "undeclared_website_sdk_methods": undeclared,
                    },
                    ensure_ascii=False,
                ),
            }
        ]

    def _detect_page_execution_contract_issues(
        self,
        filename: str,
        html_content: str,
        page_evidence: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        page_environment_projection = (
            (page_evidence or {}).get("page_environment_projection")
            or (page_evidence or {}).get("page_environment_spec")
        )
        if isinstance(page_environment_projection, dict) and page_environment_projection:
            return []
        page_contract = (page_evidence or {}).get("page_execution_contract", {})
        if not isinstance(page_contract, dict) or not page_contract:
            return []
        contract = dict(page_contract)
        contract_filename = str(contract.get("filename") or filename or "").strip()
        if not contract_filename:
            return []
        contract["filename"] = contract_filename
        try:
            from trace2world_pipeline.app_spec.page_execution_contracts import validate_page_execution_contracts

            validation = validate_page_execution_contracts(
                {"format": "page_execution_contracts", "pages": [contract]},
                {
                    filename: html_content,
                    contract_filename: html_content,
                },
            )
        except Exception as exc:
            return [
                {
                    "code": "page_execution_contract.validation_failed",
                    "message": f"Failed to validate page execution contract: {exc}",
                    "selector_hint": "document",
                    "evidence": contract_filename,
                    "expected_contract": "page_execution_contract must be parseable before page repair",
                }
            ]
        page_report = (validation.get("pages", {}) or {}).get(contract_filename, {})
        if not isinstance(page_report, dict) or page_report.get("ok", True):
            return []

        issues: List[Dict[str, Any]] = []
        missing_text = page_report.get("missing_required_text", [])
        if isinstance(missing_text, list) and missing_text:
            issues.append(
                {
                    "code": "page_execution_contract.missing_required_text",
                    "message": "Render all L0 required_text values from the page execution contract in visible DOM or an explicitly visible same-page state.",
                    "selector_hint": "body",
                    "evidence": ", ".join(str(item) for item in missing_text[:24]),
                    "expected_contract": json.dumps(
                        {
                            "filename": contract_filename,
                            "missing_required_text": missing_text,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        missing_targets = page_report.get("missing_access_target", [])
        if isinstance(missing_targets, list) and missing_targets:
            issues.append(
                {
                    "code": "page_execution_contract.missing_access_target",
                    "message": "Expose the required L0 access target through an observed visible control or item.",
                    "selector_hint": "body",
                    "evidence": ", ".join(str(item) for item in missing_targets[:24]),
                    "expected_contract": json.dumps(
                        {
                            "filename": contract_filename,
                            "missing_access_target": missing_targets,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        navigation_errors = page_report.get("navigation_policy_errors", [])
        if isinstance(navigation_errors, list) and navigation_errors:
            issues.append(
                {
                    "code": "page_execution_contract.navigation_policy",
                    "message": "Follow the route execution and monotonic stack navigation policy from the page execution contract.",
                    "selector_hint": "navigation",
                    "evidence": "; ".join(str(item) for item in navigation_errors[:12]),
                    "expected_contract": json.dumps(
                        {
                            "filename": contract_filename,
                            "navigation_policy_errors": navigation_errors,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        html_artifacts = page_report.get("html_artifacts", [])
        if isinstance(html_artifacts, list) and html_artifacts:
            issues.append(
                {
                    "code": "page_execution_contract.html_artifact",
                    "message": "Remove stale or unauthorized HTML artifacts reported by the page execution contract validator.",
                    "selector_hint": "body",
                    "evidence": json.dumps(html_artifacts[:8], ensure_ascii=False),
                    "expected_contract": "No hidden task-only surfaces, legacy repair nodes, or unauthorized option groups.",
                }
            )
        return issues

    def _build_lossless_repair_context(
        self,
        filename: str,
        issues: List[Dict[str, Any]],
        page_evidence: Dict[str, Any],
        page_architecture: Dict[str, Any],
        page_interfaces: List[Dict[str, Any]] = None,
        task_elements_for_page=None,
    ) -> str:
        context = {
            "format": "lossless_page_repair_context",
            "filename": filename,
            "issues": issues,
            "valid_filenames": self._collect_valid_filenames(
                filename,
                page_evidence or {},
                page_architecture or {},
            ),
            "page_evidence": page_evidence or {},
            "page_architecture": page_architecture or {},
            "page_interfaces": page_interfaces or [],
            "task_elements_for_page": task_elements_for_page or [],
            "contract": "All supplied repair authority is included verbatim; no field or list is issue-scoped or size-clipped.",
        }
        return (
            "## Lossless Page Repair Context\n"
            "```json\n"
            + json.dumps(context, ensure_ascii=False, indent=2, default=str)
            + "\n```"
        )

    def _build_lossless_repair_visual_blocks(
        self,
        page_evidence: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], str]:
        from trace2world_pipeline.core.llm_caller import encode_image, encode_image_downsampled
        from PIL import Image
        import base64
        import io

        evidence = page_evidence or {}
        scope = evidence.get("pagegen_input_scope") if isinstance(evidence.get("pagegen_input_scope"), dict) else {}
        prompt_refs = evidence.get("prompt_screenshot_refs") or evidence.get("screenshot_refs") or []
        source_host_entries = (
            evidence.get("source_host_visual_authority_screenshots")
            or scope.get("source_host_visual_authority_screenshots")
            or []
        )
        source_host_refs: List[str] = []
        for item in source_host_entries if isinstance(source_host_entries, list) else []:
            ref = str(item.get("screenshot_ref") or "") if isinstance(item, dict) else str(item or "")
            if ref.strip():
                source_host_refs.append(ref.strip())

        refs: List[str] = []
        for raw_ref in list(prompt_refs or []) + source_host_refs:
            ref = str(raw_ref or "").strip()
            if ref and ref not in refs and os.path.exists(ref):
                refs.append(ref)

        blocks: List[Dict[str, Any]] = []
        for path in refs:
            try:
                image = Image.open(path)
                if image.width > 800:
                    ratio = 800 / image.width
                    image = image.resize(
                        (800, max(1, int(image.height * ratio))),
                        Image.LANCZOS,
                    )
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=75, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode()
                blocks.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"})
            except Exception:
                blocks.append({"type": "input_image", "image_url": encode_image(path)})

        attach_resource_sprites = self._pagegen_resource_text_has_exact_image_paths(evidence)
        sprite_paths: List[str] = []
        if attach_resource_sprites:
            for key in ("icon_sprite_path", "photo_sprite_path"):
                path = str(evidence.get(key) or "").strip()
                if path and os.path.exists(path):
                    blocks.append({
                        "type": "input_image",
                        "image_url": encode_image_downsampled(
                            path,
                            max_width=1280,
                            max_height=1280,
                            jpeg_quality=80,
                        ),
                    })
                    sprite_paths.append(path)
        sprite_labels = [
            f"Image {len(refs) + 1 + index}: {label}"
            for index, label in enumerate(
                self._build_sprite_reference_labels(
                    evidence.get("icon_sprite_path") if sprite_paths else None,
                    evidence.get("photo_sprite_path") if sprite_paths else None,
                    None,
                )
            )
        ]
        return blocks, self._build_sprite_reference_text(sprite_labels, evidence)

    async def _repair_page_html(self,
                                filename: str,
                                prompt: str,
                                html_content: str,
                                issues: List[Dict[str, Any]],
                                page_evidence: Dict[str, Any],
                                page_architecture: Dict[str, Any],
                                page_interfaces: List[Dict[str, Any]] = None,
                                task_elements_for_page=None,
                                stage: str = "generate_html_fix_1") -> str:
        valid_filenames = self._collect_valid_filenames(filename, page_evidence, page_architecture)
        evidence = page_evidence or {}
        hydration_only = bool(issues) and all(
            str(issue.get("code") or "") == "hydration_dom.missing_binding"
            for issue in issues
            if isinstance(issue, dict)
        )
        action_host_only = bool(issues) and all(
            str(issue.get("code") or "").startswith("action_host.")
            for issue in issues
            if isinstance(issue, dict)
        )
        compact_contract_prefixes = (
            "hydration_dom.",
            "action_host.",
            "pagegen_dom_contract.",
            "navigation_target.",
            "action_slot_route.",
            "pagegen_raw_back_runtime_bypass",
        )
        formal_contract_only = bool(issues) and all(
            str(issue.get("code") or "").startswith(compact_contract_prefixes)
            for issue in issues
            if isinstance(issue, dict)
        )
        if hydration_only or action_host_only or formal_contract_only:
            scope = evidence.get("pagegen_input_scope") if isinstance(evidence, dict) else None
            render_view = scope.get("pagegen_render_view") if isinstance(scope, dict) else None
            page_environment = (
                evidence.get("page_environment_projection")
                if isinstance(evidence.get("page_environment_projection"), dict)
                else {}
            )
            if hydration_only and isinstance(render_view, dict):
                compact_contract = {
                    "hydration_dom_contract": render_view.get("hydration_dom_contract") or {},
                    "identity_continuity_contract": render_view.get("identity_continuity_contract") or {},
                }
            elif action_host_only and isinstance(render_view, dict):
                compact_contract = {
                    "operations": render_view.get("operations") or [],
                    "affordance_materialization_contract": render_view.get("affordance_materialization_contract") or [],
                    "identity_continuity_contract": render_view.get("identity_continuity_contract") or {},
                }
            else:
                compact_contract = {
                    "hydration_dom_contract": render_view.get("hydration_dom_contract") or {} if isinstance(render_view, dict) else {},
                    "search_dom_contract": render_view.get("search_dom_contract") or {} if isinstance(render_view, dict) else {},
                    "identity_continuity_contract": render_view.get("identity_continuity_contract") or {} if isinstance(render_view, dict) else {},
                    "operations": render_view.get("operations") or [] if isinstance(render_view, dict) else [],
                    "route_bindings": render_view.get("route_bindings") or [] if isinstance(render_view, dict) else [],
                    "affordance_materialization_contract": render_view.get("affordance_materialization_contract") or [] if isinstance(render_view, dict) else [],
                    "dom_obligations": page_environment.get("dom_obligations") or [],
                    "action_obligations": page_environment.get("action_obligations") or [],
                    "route_slots": page_environment.get("route_slots") or [],
                }
            visual_blocks: List[Dict[str, Any]] = []
            repair_text = (
                "## Contract-only repair boundary\n"
                "This repair is intentionally compact. The complete current HTML is the visual and "
                "content authority. Change only the machine-reported contract bindings, preserve all "
                "other bytes where possible, and return a minimal JSON text patch.\n\n"
                "## Complete relevant contract\n"
                "```json\n"
                f"{json.dumps(compact_contract, ensure_ascii=False, indent=2)}\n"
                "```\n\n"
                "## Full Previous HTML\n"
                "```html\n"
                f"{html_content}\n"
                "```\n\n"
                + PAGE_HTML_FIX_PROMPT.format(
                    filename=filename,
                    issues=self._format_fix_issues(issues),
                    valid_filenames=", ".join(valid_filenames),
                    floating_component="{}",
                    interaction_policy="{}",
                    navigation_out="[]",
                    task_elements="[]",
                )
            )
        else:
            lossless_context = self._build_lossless_repair_context(
                filename=filename,
                issues=issues,
                page_evidence=page_evidence,
                page_architecture=page_architecture,
                page_interfaces=page_interfaces,
                task_elements_for_page=task_elements_for_page,
            )
            visual_blocks, sprite_ref = self._build_lossless_repair_visual_blocks(evidence)
            repair_text = (
                "## Complete Original PageGen Prompt\n"
                "```text\n"
                f"{prompt or ''}\n"
                "```\n\n"
                f"{lossless_context}\n\n"
                "## Full Previous HTML\n"
                "```html\n"
                f"{html_content}\n"
                "```\n\n"
                + sprite_ref
                + (
                    self.repair_prompt_policy["rendered_section"] + "\n\n"
                    if self.repair_prompt_policy["rendered_section"]
                    else ""
                )
                + PAGE_HTML_FIX_PROMPT.format(
                    filename=filename,
                    issues=self._format_fix_issues(issues),
                    valid_filenames=", ".join(valid_filenames),
                    floating_component=json.dumps(evidence.get("floating_component", {}), ensure_ascii=False),
                    interaction_policy=json.dumps(evidence.get("interaction_policy", {}), ensure_ascii=False),
                    navigation_out=json.dumps(evidence.get("outbound_edges", []), ensure_ascii=False),
                    task_elements=json.dumps(task_elements_for_page or [], ensure_ascii=False, default=str),
                )
            )
        visual_blocks.append({"type": "input_text", "text": repair_text})
        fix_messages = [{"role": "user", "content": visual_blocks}]

        pagegen_decision = evidence.get("pagegen_decision") if isinstance(evidence.get("pagegen_decision"), dict) else {}
        input_budget_gate = pagegen_decision.get("input_budget_gate") if isinstance(pagegen_decision.get("input_budget_gate"), dict) else {}
        thresholds = input_budget_gate.get("thresholds") if isinstance(input_budget_gate.get("thresholds"), dict) else {}
        input_token_budget = normalize_pagegen_input_token_budget(
            thresholds.get("estimated_input_tokens", DEFAULT_PAGEGEN_INPUT_TOKEN_BUDGET)
        )
        if hydration_only or action_host_only or formal_contract_only:
            input_token_budget = min(input_token_budget, 24_000)
        image_token_proxy = self._as_int(
            thresholds.get("image_token_proxy"),
            DEFAULT_PAGEGEN_IMAGE_TOKEN_PROXY,
        )
        overhead_token_proxy = self._as_int(
            thresholds.get("overhead_token_proxy"),
            DEFAULT_PAGEGEN_REQUEST_OVERHEAD_TOKEN_PROXY,
        )
        input_budget_disabled = os.getenv("T2W_PAGEGEN_DISABLE_INPUT_BUDGET") == "1"
        if not input_budget_disabled:
            enforce_pagegen_preflight_budget(
                fix_messages,
                stage=f"{stage}:{filename}",
                input_token_budget=input_token_budget,
                image_token_proxy=image_token_proxy,
                overhead_token_proxy=overhead_token_proxy,
            )
        try:
            result, usage = await call_openai_api_json_async(
                fix_messages,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage=stage,
            )
            if not input_budget_disabled:
                enforce_pagegen_provider_budget(
                    usage,
                    stage=f"{stage}:{filename}",
                    input_token_budget=input_token_budget,
                )
        except Exception as exc:
            try:
                from trace2world_pipeline.core.llm_caller import LLMJsonResponseError
            except Exception:
                LLMJsonResponseError = ()
            is_json_error = isinstance(exc, json.JSONDecodeError) or (
                bool(LLMJsonResponseError) and isinstance(exc, LLMJsonResponseError)
            )
            if is_json_error:
                if self.logger:
                    log_warning = getattr(self.logger, "log_warning", None)
                    log_error = getattr(self.logger, "log_error", None)
                    log_fn = log_warning or log_error
                    if log_fn:
                        log_fn(
                            f"  Page fix for {filename} returned invalid JSON at {stage}; "
                            "PageGen repair cannot keep pre-fix HTML."
                        )
                raise RuntimeError(
                    f"PageGen repair returned invalid JSON for {filename} at {stage}"
                ) from exc
            raise
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                if self.logger:
                    self.logger.log_error(
                        f"  Page fix for {filename} returned non-JSON string at {stage}; "
                        "PageGen repair cannot keep pre-fix HTML."
                    )
                raise RuntimeError(f"PageGen repair returned non-JSON string for {filename} at {stage}")
        if not isinstance(result, dict):
            raise RuntimeError(f"PageGen repair returned {type(result).__name__} for {filename} at {stage}")
        if "html_content" in result or "html" in result:
            if self.logger:
                self.logger.log_error(
                    f"  Page fix for {filename} returned full HTML at {stage}; "
                    "ignoring it because PageGen repair requires patch output"
                )
            return html_content
        updated_html, apply_report = apply_text_patch_edits(html_content, result, target_id=filename)
        if not apply_report.get("success"):
            if self.logger:
                self.logger.log_error(
                    f"  Page fix patch for {filename} applied no edits: {json.dumps(apply_report.get('failed', []), ensure_ascii=False)[:500]}"
                )
            raise RuntimeError(f"PageGen repair patch failed for {filename} at {stage}")
        return updated_html.replace('<link rel="stylesheet" href="styles.css">', '')

    def _audit_page_contract_issues(self,
                                    filename: str,
                                    html_content: str,
                                    page_evidence: Dict[str, Any],
                                    page_architecture: Dict[str, Any],
                                    page_interfaces: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        sdk_side_effect_methods = self._action_slot_sdk_side_effect_methods(page_evidence)
        issues = self._detect_html_validity_issues(html_content)
        issues.extend(self._detect_js_syntax_issues(html_content))
        issues.extend(self._detect_stateful_continuation_contract_issues(html_content, page_interfaces))
        issues.extend(self._detect_page_execution_contract_issues(filename, html_content, page_evidence))
        issues.extend(
            self._detect_undeclared_sdk_call_issues(
                html_content,
                page_interfaces,
                extra_allowed_methods=sdk_side_effect_methods,
            )
        )
        issues.extend(self._detect_pagegen_dom_contract_issues(filename, html_content, page_evidence))
        issues.extend(self._detect_hydration_dom_contract_issues(filename, html_content, page_evidence))
        issues.extend(self._detect_initial_hidden_surface_leak_issues(filename, html_content, page_evidence))
        issues.extend(self._detect_route_authority_boundary_issues(filename, html_content, page_evidence))
        issues.extend(self._detect_action_slot_route_executor_issues(filename, html_content, page_evidence))
        issues.extend(self._detect_raw_back_navigation_bypass_issues(html_content))
        if (page_evidence or {}).get("floating_component"):
            valid_filenames = self._collect_valid_filenames(filename, page_evidence, page_architecture)
            issues.extend(self._detect_overlay_contract_issues(html_content, page_evidence, valid_filenames))
        return issues

    @staticmethod
    def _surface_action_keys_for_component_ref(surface: Dict[str, Any]) -> List[str]:
        if not isinstance(surface, dict):
            return []
        keys: List[str] = []
        for item in surface.get("evidence_steps") or []:
            if not isinstance(item, dict):
                continue
            step_key = str(item.get("step_key") or "").strip()
            if step_key:
                keys.append(step_key)
                continue
            trace_id = str(item.get("trace_id") or "").strip()
            step_index = item.get("step_index")
            if trace_id and step_index is not None:
                try:
                    keys.append(f"{trace_id}:step{int(step_index)}")
                except (TypeError, ValueError):
                    pass
        for field in ("source_step_keys", "action_ids", "action_id"):
            value = surface.get(field)
            if isinstance(value, list):
                keys.extend(str(item).strip() for item in value if str(item).strip())
            elif value not in (None, "", [], {}):
                keys.append(str(value).strip())
        for text in (
            str(surface.get("surface_id") or ""),
            str(surface.get("required_data_element_id") or ""),
        ):
            for match in re.finditer(r"::([^:]+:step\d+)::", text):
                keys.append(match.group(1))
        seen: set[str] = set()
        result: List[str] = []
        for key in keys:
            if key and key not in seen:
                seen.add(key)
                result.append(key)
        return result

    @classmethod
    def _enrich_dom_bindings_with_surface_component_refs(
        cls,
        bindings_payload: Any,
        surfaces: List[Dict[str, Any]],
    ) -> Any:
        if not isinstance(bindings_payload, dict) or not isinstance(bindings_payload.get("bindings"), list):
            return bindings_payload
        component_by_action: Dict[str, str] = {}
        for surface in surfaces or []:
            if not isinstance(surface, dict):
                continue
            component_id = str(surface.get("required_data_element_id") or surface.get("surface_id") or "").strip()
            if not component_id:
                continue
            for action_key in cls._surface_action_keys_for_component_ref(surface):
                component_by_action.setdefault(action_key, component_id)
        if not component_by_action:
            return bindings_payload

        updated = dict(bindings_payload)
        updated_bindings: List[Dict[str, Any]] = []
        for binding in bindings_payload.get("bindings") or []:
            if not isinstance(binding, dict):
                updated_bindings.append(binding)
                continue
            action_id = str(binding.get("action_id") or "").strip()
            if not action_id:
                interaction = binding.get("interaction") if isinstance(binding.get("interaction"), dict) else {}
                payload = interaction.get("payload") if isinstance(interaction.get("payload"), dict) else {}
                action_id = str(payload.get("action_id") or "").strip()
            component_ref = component_by_action.get(action_id)
            if not component_ref:
                updated_bindings.append(binding)
                continue
            item = dict(binding)
            identity = dict(item.get("element_identity") or {})
            if not identity.get("component_ref"):
                identity["component_ref"] = component_ref
            item["element_identity"] = identity
            updated_bindings.append(item)
        updated["bindings"] = updated_bindings
        return updated

    async def _audit_or_repair_page_contracts(self,
                                              filename: str,
                                              prompt: str,
                                              html_content: str,
                                              page_evidence: Dict[str, Any],
                                              page_architecture: Dict[str, Any],
                                              page_interfaces: List[Dict[str, Any]] = None,
                                              task_elements_for_page=None,
                                              page_name: str = "",
        max_fix_rounds: Optional[int] = None) -> Tuple[str, List[Dict[str, Any]], int]:
        html_content = self._normalize_page_html_content(html_content)
        effective_max_fix_rounds = max(0, int(max_fix_rounds if max_fix_rounds is not None else self.page_fix_max_rounds))
        if not self.page_fixloop_enabled or effective_max_fix_rounds <= 0:
            issues = self._audit_page_contract_issues(
                filename=filename,
                html_content=html_content,
                page_evidence=page_evidence,
                page_architecture=page_architecture,
                page_interfaces=page_interfaces,
            )
            if issues and self.logger:
                self.logger.log_warning(
                    f"  Page contract audit for {page_name or filename}: found {len(issues)} page contract issues; PageGen repair disabled"
                )
            return html_content, issues, 0
        return await self._run_page_fix_loop(
            filename=filename,
            prompt=prompt,
            html_content=html_content,
            page_evidence=page_evidence,
            page_architecture=page_architecture,
            page_interfaces=page_interfaces,
            task_elements_for_page=task_elements_for_page,
            page_name=page_name,
            max_fix_rounds=effective_max_fix_rounds,
        )

    async def _run_page_fix_loop(self,
                                 filename: str,
                                 prompt: str,
                                 html_content: str,
                                 page_evidence: Dict[str, Any],
                                 page_architecture: Dict[str, Any],
                                 page_interfaces: List[Dict[str, Any]] = None,
                                 task_elements_for_page=None,
                                 page_name: str = "",
                                 max_fix_rounds: Optional[int] = None) -> Tuple[str, List[Dict[str, Any]], int]:
        issues = self._audit_page_contract_issues(
            filename=filename,
            html_content=html_content,
            page_evidence=page_evidence,
            page_architecture=page_architecture,
            page_interfaces=page_interfaces,
        )
        if not issues:
            return html_content, [], 0
        current_html = html_content
        effective_max_fix_rounds = max(0, int(max_fix_rounds if max_fix_rounds is not None else self.page_fix_max_rounds))
        if not self.page_fixloop_enabled or effective_max_fix_rounds <= 0:
            if issues and self.logger:
                self.logger.log_warning(
                    f"  Page contract audit for {page_name or filename}: found {len(issues)} page contract issues; PageGen fixloop disabled"
                )
            return current_html, issues, 0

        rounds_used = 0
        while issues and rounds_used < effective_max_fix_rounds:
            rounds_used += 1
            if self.logger:
                self.logger.log_warning(
                    f"  Page fix round {rounds_used} for {page_name or filename}: found {len(issues)} page contract issues"
                )
            current_html = await self._repair_page_html(
                filename=filename,
                prompt=prompt,
                html_content=current_html,
                issues=issues,
                page_evidence=page_evidence,
                page_architecture=page_architecture,
                page_interfaces=page_interfaces,
                task_elements_for_page=task_elements_for_page,
                stage=f"generate_html_fix_{rounds_used}",
            )
            issues = self._audit_page_contract_issues(
                filename=filename,
                html_content=current_html,
                page_evidence=page_evidence,
                page_architecture=page_architecture,
                page_interfaces=page_interfaces,
            )
        return current_html, issues, rounds_used

    def _build_html_prompt_tailwind(self,
                                    page_design: Any,
                                    website_type: str,
                                    entity_schema: Dict[str, Any],
                                    page_interfaces: List[Dict[str, Any]],
                                    page_architecture: Dict[str, Any],
                                    page_evidence: Dict[str, Any],
                                    task_elements_for_page=None,
                                    content_samples: Dict[str, Any] = None,
                                    page_layout: Dict[str, Any] = None) -> str:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(page_design):
            page_design_dict = asdict(page_design)
            filename = page_design.filename
        else:
            page_design_dict = page_design
            filename = page_design.get('filename', 'index.html')

        ev = page_evidence or {}

        chinese = False
        for source in [ev, entity_schema]:
            if not source:
                continue
            sample = json.dumps(source if source is ev else list(source.values())[:3],
                                ensure_ascii=False, default=str)[:2000]
            if sum(1 for c in sample if '\u4e00' <= c <= '\u9fff') > 10:
                chinese = True
                break
        lang_line = "\n5. ALL visible text MUST be Chinese (简体中文). English only for code/attributes." if chinese else ""


        if self.render_policy["platform"] == "mobile":
            parts = [f"""You are a senior frontend developer. Generate a complete, production-quality HTML page that faithfully reproduces the mobile app page shown in the reference screenshot(s).

Requirements:
1. Tailwind CDN: <script src="https://cdn.tailwindcss.com"></script>
2. Phone frame: handled by the <style> block below — do NOT add layout classes to <body>
3. App shell: wrap content in div.app-shell (flex flex-col, 36px top safe area). Only add header/footer if they are VISIBLE in the screenshot — modal/overlay pages may not have them
4. Full HTML5: <!DOCTYPE html><html><head><title>...</title></head><body>...</body></html>{lang_line}
5. NO separate CSS file — Tailwind utilities + one <style> block for phone frame only
6. No loading placeholders, no auto-popups, no chatbot widgets
7. Lucide icons: <script src="https://unpkg.com/lucide@latest"></script>, init with if (window.lucide && typeof window.lucide.createIcons === 'function') window.lucide.createIcons() at body end so an unavailable optional icon CDN cannot break page execution
8. SDK (3 scripts in order): <script src="../../phase_sdk_generation/runtime/seed_data.js"></script><script src="../../phase_sdk_generation/runtime/data_layer.js"></script><script src="../../phase_sdk_generation/runtime/business_logic.js"></script>
9. Screenshot fidelity: screenshots are LAYOUT, STYLE, and CONTENT specs — reproduce the exact visual hierarchy, section proportions, spacing, grid layouts, background colors, card layouts, component positioning, and text hierarchy. Do NOT redesign or reinterpret any layout. Screenshots are the PRIMARY content source — reproduce visible text (list item titles, names, labels, counts, timestamps) directly from the screenshots. However, Page Environment Spec task-critical overrides take precedence: when PES content_seed.task_critical_overrides specifies a required value for a specific component or list position, use the PES value even if the screenshot shows different content at that position. PES overrides ensure trace replay executability.
10. Resource images: use ONLY real images from the Resources section — NEVER use emoji, text placeholders, or colored blocks
11. Every task-relevant visible button in the screenshot MUST be interactive — buttons with explicit route/navigation targets link to that target page
12. Do NOT use viewport-scoped fixed positioning. Do not use Tailwind `fixed` or inline `position: fixed`; use `absolute` inside `.app-shell`/`body` or flex layout so all in-app components remain inside the phone frame even on wide desktop viewports."""]
        else:
            desktop_viewport = self.environment_profile["viewport"]
            parts = [f"""You are a senior frontend developer. Generate a complete, production-quality desktop web application page that faithfully reproduces the reference screenshot(s).

Requirements:
1. Tailwind CDN: <script src="https://cdn.tailwindcss.com"></script>
2. Desktop canvas: target {desktop_viewport['width']}x{desktop_viewport['height']} CSS pixels; do NOT add a phone frame, device notch, home indicator, rounded handset border, or artificial outer margin
3. App shell: wrap the complete desktop surface in div.app-shell. Preserve visible browser-app regions such as top navigation, sidebars, toolbars, content panes, tables, dialogs, and footers only when evidenced by screenshots/contracts
4. Full HTML5: <!DOCTYPE html><html><head><title>...</title></head><body>...</body></html>{lang_line}
5. NO separate CSS file — Tailwind utilities + one <style> block for desktop canvas fundamentals only
6. No loading placeholders, no auto-popups, no chatbot widgets
7. Lucide icons: <script src="https://unpkg.com/lucide@latest"></script>, init with if (window.lucide && typeof window.lucide.createIcons === 'function') window.lucide.createIcons() at body end so an unavailable optional icon CDN cannot break page execution
8. SDK (3 scripts in order): <script src="../../phase_sdk_generation/runtime/seed_data.js"></script><script src="../../phase_sdk_generation/runtime/data_layer.js"></script><script src="../../phase_sdk_generation/runtime/business_logic.js"></script>
9. Screenshot fidelity: screenshots are LAYOUT, STYLE, and CONTENT specs — reproduce the exact visual hierarchy, section proportions, spacing, grid layouts, background colors, card layouts, component positioning, and text hierarchy. Do NOT redesign or reinterpret any layout. Screenshots are the PRIMARY content source — reproduce visible text directly from the screenshots. Page Environment Spec task-critical overrides take precedence when present.
10. Resource images: use ONLY real images from the Resources section — NEVER use emoji, text placeholders, or colored blocks
11. Every task-relevant visible control in the screenshot MUST be interactive when route/action authority exists. Support mouse, keyboard focus, wheel scrolling, and desktop-sized hit targets
12. Viewport-fixed positioning is allowed only for screenshot/contract-evidenced desktop overlays, dialogs, sticky headers, or persistent toolbars. Do not convert desktop fixed/sticky behavior into phone-scoped absolute positioning."""]

        parts.append(
            "\n## Hit Target Integrity\n"
            "Interactive controls must not be covered by decorative or centering layers. "
            "Header titles, badges, overlays, and spacer elements may not intercept pointer "
            "events over back/share/action buttons. Use non-overlapping grid/flex layout or "
            "`pointer-events-none` on non-interactive centering text when needed."
        )
        parts.append(
            "\n## Page-Local JavaScript Binding Safety\n"
            "When page-local JavaScript binds handlers to task actions, select the visible "
            "host by its actual `data-action-id`, stable `id`, or exact rendered "
            "`data-element-id`, and always guard nullable DOM lookups before calling "
            "`addEventListener`, `classList`, or style mutation. Never let an optional "
            "overlay/button selector throw during page load; missing optional hosts must "
            "degrade to a no-op while the visible page remains replayable."
        )
        parts.append(
            "\n## Modal / Dialog Action Button Layout\n"
            "Confirmation dialogs with two action buttons (e.g. cancel + confirm) must render "
            "both buttons on the SAME row. Do NOT insert a standalone divider `<div>` as a "
            "sibling grid/flex child between them — it creates a third child that breaks "
            "`grid-cols-2` layout. Use `border-r` on the first button for a vertical separator, "
            "or use `grid-cols-[1fr_1px_1fr]` if a divider element is needed."
        )

        external_regions = ev.get("external_ui_regions", [])
        if not isinstance(external_regions, list):
            external_regions = []
        parts.append(
            "\n## External/System UI Exclusion\n"
            "Reference screenshots may contain OS or non-target-app surfaces such as "
            "verification SMS previews, OS notification banners, incoming call bars, "
            "system permission sheets, app switcher surfaces, keyboard/IME chrome, "
            "or status-bar-only system UI. Do NOT reproduce these regions in HTML, "
            "do not create DOM components, buttons, SDK calls, task bindings, routes, "
            "or localStorage data for them, and do not treat their text as app content."
        )
        parts.append(
            "\n## Search Entry Dynamic State\n"
            "For search-entry or suggestion-entry pages within the same stable "
            "search scope, search input text, search history, recommendation chips, "
            "recommended queries, and autocomplete / typeahead rows are dynamic "
            "state within the same page instance. Do NOT render different observed "
            "query strings as separate static pages or duplicate sections. If the "
            "page shows a visible scope anchor that changes query interpretation or "
            "result/action applicability, preserve that anchor as fixed context for "
            "this page instance and do not hide it behind input-state JavaScript. "
            "Use JavaScript input event handlers to switch the visible state inside "
            "the same DOM: empty input shows history and recommendation content; "
            "non-empty input shows filtered autocomplete / typeahead suggestions; "
            "clearing the input restores the initial state. Clicking a "
            "recommendation or autocomplete row should fill the input and optionally "
            "submit/navigate through the assigned SDK/navigation flow.\n\n"
            "The initial DOM must leave search/searchbox inputs empty; observed query "
            "text is post-fill state and must only appear after the user types or "
            "selects a suggestion.\n\n"
            "IMPORTANT: If `autocomplete_contracts` is present in the page scope, "
            "you MUST use its `candidate_rows` as the autocomplete data source. "
            "Do NOT fabricate fallback autocomplete data or create fewer rows than "
            "`expected_candidate_count`. Each candidate row's `title` must appear "
            "as a visible suggestion item. If `candidate_categories` is specified, "
            "rendered suggestions must cover all listed categories.\n\n"
            "IMPORTANT: If `initial_control_state_contracts` or `initial_dom_contract` "
            "is present, you MUST render checkboxes/radios/toggles/count badges "
            "according to their `expected_checked`, `expected_value`, and "
            "`expected_visible` fields. Do NOT pre-select checkboxes based on "
            "common sense or infer checked state from group counts."
        )
        parts.append(
            "\n## HTML Validity\n"
            "- Do NOT nest interactive elements. `button`, `a`, `label`, `select`, "
            "`input`, `textarea`, and `summary` must not contain each other.\n"
            "- For clickable cards with inner action controls, make the card a non-interactive "
            "`div`/`article` wrapper and place sibling `button`/`a` controls inside it; do not "
            "put a `button` inside another `button` or an `a` inside another `a`.\n"
            "- Invalid nested interactive markup is a browser parsing contract violation: "
            "Chromium may auto-close/reparent nodes and move later sections outside `.app-shell main`."
        )
        if external_regions:
            compact_external_regions = self._compact_external_regions_for_prompt(
                external_regions,
                budgeted=False,
            )
            parts.append(
                "\nIgnored external/system regions from AppSpec:\n```json\n"
                + json.dumps(compact_external_regions, ensure_ascii=False, indent=2)
                + "\n```"
            )

        if self.render_policy["platform"] == "mobile":
            parts.append("""
## Mobile Typography
- Preferred semantic sizes: text-xs, text-sm, text-base, text-lg, text-xl.
- Prefer Tailwind semantic text sizes for mobile UI: `text-xs`, `text-sm`, `text-base`, `text-lg`, `text-xl`.
- Do not use custom pixel font sizes by default. Only use `text-[Npx]` for truly exceptional cases like tiny badges/counters or a screenshot-proven oversized title that cannot be represented well by the semantic scale.
- Most mobile UI text should stay in the 12-16px range. Use larger sizes sparingly:
  - `text-xs` / 12px: timestamps, counts, tab labels, helper/meta text
  - `text-sm` / 14px: secondary labels, previews, small buttons, chips
  - `text-base` / 16px: primary body text, standard list titles, input text
  - `text-lg` / 18px: app bar titles, emphasized row titles
  - `text-xl` / 20px: large detail-page titles only
- For standard mobile feeds, inboxes, tab pages, and tool pages, avoid `18px+` for ordinary list rows or preview text.
- Keep the number of distinct font sizes per page small: usually 3-5 sizes is enough.
- If unsure between two sizes, choose the smaller one and use weight/spacing/contrast for emphasis instead of increasing font size.""")
        else:
            parts.append("""
## Desktop Typography and Density
- Use a restrained desktop type scale: `text-xs` and `text-sm` for dense metadata, tables, sidebars, and controls; `text-base` for normal body copy; `text-lg` through `text-3xl` only for evidenced headings.
- Preserve screenshot-evidenced information density. Do not inflate mobile-style cards, buttons, gaps, or typography across a desktop canvas.
- Keep keyboard focus states visible, labels aligned with their controls, and long content readable without forcing a single narrow phone-like column.
- Use responsive grid/flex sizing and bounded content widths only when the screenshot shows them; do not center the entire application inside a handset-sized container.""")


        page_name = ev.get("page_name", filename)
        desc = ev.get("description", "")
        page_type = ev.get("page_type", "")
        page_environment_projection = ev.get("page_environment_projection") or ev.get("page_environment_spec")
        pagegen_scope = ev.get("pagegen_input_scope") if isinstance(ev.get("pagegen_input_scope"), dict) else {}
        authoritative_pagegen_render_view = (
            pagegen_scope.get("pagegen_render_view")
            if isinstance(pagegen_scope.get("pagegen_render_view"), dict)
            else {}
        )
        spec_authoritative_mode = isinstance(page_environment_projection, dict) and bool(
            page_environment_projection
        )
        if authoritative_pagegen_render_view and not spec_authoritative_mode:
            raise ValueError(
                f"PageGen render view for {filename} has no matching Page Environment projection; "
                "regenerate phase_page_environment from the current AppSpec instead of falling "
                "back to legacy parallel prompt carriers."
            )
        pagegen_render_view = authoritative_pagegen_render_view
        required_interaction_surfaces = (
            pagegen_scope.get("required_interaction_surfaces")
            if isinstance(pagegen_scope, dict)
            else []
        )
        if not isinstance(required_interaction_surfaces, list):
            required_interaction_surfaces = []
        required_interaction_surfaces_for_prompt = self._compact_required_interaction_surfaces_for_prompt(
            required_interaction_surfaces,
            budgeted=False,
        )
        pagegen_dom_hard_gate_checklist = self._build_pagegen_dom_hard_gate_checklist(
            required_interaction_surfaces,
            budgeted=False,
        )
        if self._pagegen_needs_seat_map_contract(
            filename,
            page_name,
            desc,
            pagegen_render_view,
            required_interaction_surfaces,
        ):
            parts.append(self._seat_map_generation_contract_prompt())
        route_action_slots = (
            pagegen_scope.get("route_action_slots")
            if isinstance(pagegen_scope, dict)
            else []
        )
        if not isinstance(route_action_slots, list):
            route_action_slots = []

        if not spec_authoritative_mode:
            parts.append(f"""
## Page: {page_name} ({filename})
Type: {page_type} | Website: {website_type}
Description: {desc}
Design spec: {json.dumps(page_design_dict, ensure_ascii=False)}""")


        components = ev.get("components", [])
        if components:
            comp_lines = []

            layout_sizing = {}
            if page_layout and isinstance(page_layout, dict):
                for cl in page_layout.get("component_layouts", []):
                    if isinstance(cl, dict) and cl.get("id") and cl.get("css_sizing"):
                        layout_sizing[cl["id"]] = cl["css_sizing"]

            for c in components:
                if not isinstance(c, dict):
                    continue
                cid = c.get("component_id", "")
                if not cid:
                    continue
                bounds = c.get("bounds_pct", {})
                b_str = ""
                if isinstance(bounds, dict) and bounds.get("y") is not None:
                    b_str = f" [y={bounds.get('y','')}% h={bounds.get('h','')}%]"

                sizing = layout_sizing.get(cid)
                s_str = ""
                if sizing:
                    w = sizing.get("width", "")
                    ctx = sizing.get("display_context", "")
                    flex = sizing.get("flex_or_grid_hint", "")
                    parts_list = [p for p in [w, ctx, flex] if p]
                    if parts_list:
                        s_str = f" | sizing: {', '.join(parts_list)}"
                comp_lines.append(f"  - {cid}{b_str}{s_str}")
            if comp_lines and not spec_authoritative_mode:
                parts.append(
                    "\n**Components (MUST appear in DOM with data-component or id):**\n"
                    + "\n".join(comp_lines)
                )


        layout_rules = self._build_layout_constraints(page_layout, page_evidence)
        if layout_rules and not spec_authoritative_mode:
            parts.append(layout_rules)


        vlt = ev.get("visual_layout_text", "")
        if vlt and not spec_authoritative_mode:
            parts.append(f"\n## Visual Layout\n```\n{vlt}\n```")


        structure_data = ev.get("structure_data", {})
        design_system = structure_data.get("design_system", {}) if isinstance(structure_data, dict) else {}
        if not design_system:

            design_system = ev.get("design_system", {})
        if design_system and not spec_authoritative_mode:
            parts.append(f"\n## Design System\n```json\n{json.dumps(design_system, indent=2, ensure_ascii=False)}\n```")


        rmt = ev.get("resource_list_text", "")
        page_resources = ev.get("page_resources", [])
        pagegen_resource_projection = (
            ev.get("pagegen_resource_projection")
            if isinstance(ev.get("pagegen_resource_projection"), dict)
            else {}
        )
        if isinstance(pagegen_resource_projection.get("resources"), list):
            page_resources = pagegen_resource_projection.get("resources") or []
        render_scope_contract = ev.get("render_scope_contract")
        complete_render_scope_contract = (
            deepcopy(render_scope_contract)
            if isinstance(render_scope_contract, dict) and render_scope_contract
            else {}
        )

        page_execution_obligations = ev.get("page_execution_obligations", {})
        compact_execution_obligations = self._compact_page_execution_obligations_for_prompt(page_execution_obligations)
        page_execution_contract = ev.get("page_execution_contract", {})
        compact_page_execution_contract = self._compact_page_execution_contract_for_prompt(
            page_execution_contract
        )
        route_action_labels = self._route_action_binding_labels_for_prompt(compact_page_execution_contract)
        interaction_policy = ev.get("interaction_policy", {})
        prompt_interaction_policy = (
            self._compact_interaction_policy_for_prompt(
                interaction_policy,
                route_action_labels,
            )
            if interaction_policy
            else {}
        )
        component_contracts = ev.get("component_contracts", []) if isinstance(ev.get("component_contracts", []), list) else []
        floating_component = next((item for item in component_contracts if isinstance(item, dict) and str(item.get("component_type", "")).strip() == "floating_component"), {})
        floating_component = floating_component or ev.get("floating_component", {}) or ev.get("overlay_policy", {})

        page_runtime_contract = {}
        if not spec_authoritative_mode:
            try:
                from trace2world_pipeline.runtime_backend.runtime_contract import build_runtime_contract
                page_runtime_contract = build_runtime_contract(
                    entity_schema or {},
                    {"interfaces": page_interfaces or []},
                    task_interaction_contracts=self._page_task_interaction_contract_payload(ev),
                )
                page_runtime_contract = self._compact_runtime_contract_for_prompt(page_runtime_contract)
            except Exception:
                page_runtime_contract = {}
        sdk_interface_contracts = self._compact_sdk_interfaces_for_prompt(page_interfaces or [])

        extracted_items = ev.get("extracted_items", {})
        entity_component_mapping = ev.get("entity_component_mapping", [])
        entity_task_relevance = ev.get("entity_task_relevance", {})

        if spec_authoritative_mode:
            if not authoritative_pagegen_render_view:
                raise ValueError(
                    f"PageGen render view is required for spec-authoritative page {filename}; "
                    "regenerate pagegen_input_scope from compiled spec execution views."
                )
            conflict_report = (
                authoritative_pagegen_render_view.get("validation_contract", {}).get("conflict_report", {})
                if isinstance(authoritative_pagegen_render_view.get("validation_contract"), dict)
                else {}
            )
            if isinstance(conflict_report, dict) and int(conflict_report.get("conflict_count") or 0) > 0:
                raise ValueError(
                    f"PageGen render view conflict for {filename}: "
                    f"{conflict_report.get('conflict_count')} conflict(s)"
                )
            visual_components = deepcopy(components) if isinstance(components, list) else []
            screenshot_lifecycle_inputs = {
                "initial_layout_screenshots": pagegen_scope.get("initial_layout_screenshots", []),
                "state_transition_screenshots": pagegen_scope.get("state_transition_screenshots", []),
                "runtime_status_screenshots": pagegen_scope.get("runtime_status_screenshots", []),
                "unknown_render_role_screenshots": pagegen_scope.get("unknown_render_role_screenshots", []),
                "source_host_visual_authority_screenshots": pagegen_scope.get("source_host_visual_authority_screenshots", []),
                "initial_visual_authority": pagegen_scope.get("initial_visual_authority", {}),
            }
            detailed_pagegen_rules = [
                "Screenshots are visual/content authority; pagegen_render_view is the only semantic authority for DOM hosts, actions, state transitions, route slots, runtime bindings, and asset slots.",
                "Render each dom_obligations item as exactly one real host carrying its obligation_id as data-element-id, or as a whitespace-separated data-element-aliases token when coalesced onto another real visible host.",
                "No deterministic postprocessor will add, move, guess, or repair missing data-element-id/data-element-aliases for you; the returned HTML itself must be binding-complete.",
                "If a host also carries data-action-id, keep the exact data-element-id/data-element-aliases from dom_obligations on the same visible actionable host; data-action-id never satisfies a DOM obligation by itself.",
                "Visibility follows each obligation's visibility_policy: visible_initial hosts are visible/actionable initially; scroll_reachable_initial hosts must exist in the initial DOM inside the real scroll container and become visible through the declared scroll without display:none; hidden_until_transition and runtime-only hosts must not be initial-visible.",
                "Bind actions only from action_obligations and route_slots. Do not infer navigation or handlers from labels, page titles, filenames, or global fallback maps.",
                "Use action_obligations, state_obligations, and runtime_bindings for reducers/state effects. Do not create page-local fallback scripts that clear, replace, or fabricate state outside those contracts.",
                "Inputs, autocomplete, suggestions, selections, overlays, sheets, carts, and saved records must follow their declared reducer/state transition. Observed input text is post-action state unless the initial screenshot visibly shows it.",
                "Only visible_text_contract.exact_text with visible authority may become user-visible body text. Keep semantic-only labels in aria-label/data metadata and context_text_must_include in nearby/ancestor context.",
                "Use source_host_visual_authority_screenshots only to identify the listed source host label/affordance and nearby shape; they are not page-global initial DOM authority unless also listed under initial_layout_screenshots.",
                "Use trace_hit_points in dom_obligations as approximate screenshot tap-point hints for disambiguation only; never render coordinate values.",
                "Use pagegen_render_view.asset_slots only for screenshot-backed visual media. Render generic controls, badges, prices, ratings, status markers, and text-only labels with HTML/CSS/Lucide.",
                "Only initial_layout_screenshots define initial visible DOM. State/runtime/source-host screenshots define hidden or runtime-active state and JS transitions only.",
                "When pagegen_render_view.hydration_dom_contract has field_bindings, put its exact root attribute on the single owning page shell. Every field_bindings row is mandatory even when its sample value is null or visually secondary: for every scalar, rich_text, or boolean_state row, put data-ghostweb-field with the exact unrenamed path on the visible value host. For every collection row, put data-ghostweb-collection with the exact path on the visible list container and include one real HTML <template> element whose data-ghostweb-collection-template value is that same exact path, not a div or custom component name; inside that template, include a visible repeated-item host with data-ghostweb-item-field for every exact item_fields entry. Before returning HTML, enumerate the contract rows and verify each exact attribute and required template tag exists. Runtime hydration replaces the children of scalar, rich_text, boolean_state, and repeated-item value hosts: those binding hosts must therefore be leaf hosts relative to every other formal binding. Never put data-ghostweb-field on a data-ghostweb-collection host or on an ancestor of another data-ghostweb-field, data-ghostweb-collection, data-ghostweb-item-field, data-action-id, or search binding. A repeated-row action wrapper may contain child data-ghostweb-item-field value hosts, but the wrapper itself must not carry data-ghostweb-item-field. Do not add data-ghostweb-collection attributes for decorative/static lists that are absent from the hydration contract, and do not replace required bindings with static sample-only text.",
                "If hydration_dom_contract.retry_required_exact_attributes is present, every listed name=value pair is a machine-reported failure from the prior provider attempt and must be corrected in the returned complete HTML. Re-emit every field_bindings row as an exact attribute checklist, including rows that passed previously; fixing one missing binding must never remove or rename another required binding. If no existing visible value host fits, add a visible metadata/stat row for the required value rather than omitting it. Do not omit a binding merely because another nearby element displays a related value.",
                "When pagegen_render_view.search_dom_contract.required is true, the single visible search combobox inside the admitted search overlay must carry the exact input_attribute name=value and data-action-id=query_action_host_authority_id, and the same overlay must contain exactly one visible-capable results container carrying the exact results_attribute name=value immediately below the input. Keep that container empty initially; the shared SDK materializes Data Lake-backed result rows and binds the declared result action host. Do not add a page-local search dataset, query handler, result ranking, or independent search-results route.",
                "Every operation whose action_host.visible_affordance_required is true must have a visible interactive host carrying its exact action_host_authority_id as data-action-id, except hosts explicitly materialized by the shared runtime. A close action host must also have a stable accessible name such as aria-label and a non-zero clickable box; an icon alone is insufficient.",
                "For a composite visual action such as an icon-and-label card, grid tile, or menu entry, put the data-element-id/data-element-aliases and exact data-action-id on the single outer interactive card/tile that owns the complete visible hit area. Do not bind only a nested text span, icon, image, or decorative child: tapping anywhere on the displayed card, including its image region, must invoke the same contract-owned action. Keep child media and text noninteractive unless the contract explicitly declares a separate action.",
                "When pagegen_render_view.affordance_materialization_contract is present, it is the total interactive allowlist for this page. Materialize every canonical_operation row as the exact data-action-id host, every internal_navigation row as its exact data-action-id host or authority-backed route link, and every authorized_boundary row as a real link to its declared boundary target. Do not render any other button, input, select, textarea, summary, role=button/link/tab, or anchor with href. Visual chrome absent from the allowlist must be noninteractive text/container markup, never an inert clickable placeholder.",
                "When pagegen_render_view.identity_continuity_contract is present, all subject-dependent visible text, counts, collections, rich content, and the browser document title must live only in its declared hydration hosts. Materialize document_title_binding as exactly one <title> host with its exact attribute and path, no static title payload, and exact normalized runtime projection value. Body identity hosts and the document title are separate obligations: validate every declared host and never let one correct first match mask a stale duplicate or stale title. Preview values and screenshot text are layout examples, not static fallback content. Do not leave a second hardcoded owner, repository, commit, pull request, file, count, description, README identity, or document title outside the exact hydration hosts.",
                "Parameterized and enumerated PageInstances must hydrate from the SDK's hydrateCurrentRoute through the shared runtime. Bounded preview rows are layout/content examples only, never the complete population or route identity authority. Do not create page-local entity arrays, fallback identities, or route parsers.",
                "If a required obligation cannot be represented from pagegen_render_view, expose the missing authority; do not fabricate fallback behavior or synthetic content.",
            ]
            pagegen_environment_input = {
                "pagegen_render_view": pagegen_render_view,
                "page_identity": {
                    "filename": filename,
                    "page_name": page_name,
                    "page_type": page_type,
                    "description": desc,
                    "website_type": website_type,
                },
                "visual_implementation_context": {
                    "components": visual_components,
                    "layout_constraints": layout_rules,
                    "visual_layout_text": vlt,
                    "design_system": design_system,
                },
                "screenshot_lifecycle_inputs": screenshot_lifecycle_inputs,
                "pagegen_contract": {
                    "single_authoritative_semantic_input": True,
                    "screenshot_ocr_content_primary": True,
                    "semantic_input_field": "pagegen_render_view",
                    "legacy_parallel_sections_suppressed": True,
                    "task_critical_overrides_via_pes": True,
                    "rules": detailed_pagegen_rules,
                },
            }
            if complete_render_scope_contract:
                pagegen_environment_input["render_scope_debug"] = complete_render_scope_contract
            wheel_template_ids = {"wheel_time_picker", "wheel_picker"}
            wheel_projection_sources = [
                page_evidence.get("render_projections") if isinstance(page_evidence, dict) else [],
                page_environment_projection.get("render_projections")
                if isinstance(page_environment_projection, dict)
                else [],
                pagegen_render_view.get("render_projections")
                if isinstance(pagegen_render_view, dict)
                else [],
            ]
            _has_wheel_picker = any(
                isinstance(item, dict)
                and isinstance(item.get("overlay_contract"), dict)
                and str(item["overlay_contract"].get("template_id", "")).strip() in wheel_template_ids
                for source in wheel_projection_sources
                for item in (source or [])
                if isinstance(item, dict)
            )
            if _has_wheel_picker:
                pagegen_environment_input["pagegen_contract"]["rules"].append(
                    "For render_projection overlay_contract.template_id=wheel_time_picker or wheel_picker, PageGen owns the visible layout, color, style, column count/grouping, and copy of the overlay. Render the screenshot-faithful host, but keep the generic runtime contract: the root has data-ghostweb-template set to the overlay_contract.template_id and data-ghostweb-wheel-trigger; the root must be an app-shell-scoped absolute overlay, not nested as normal content inside a scrollable main/list; the visible opener either uses that trigger id or data-ghostweb-opens-template matching the template id / data-wheel-open; each visible wheel column host has data-wheel-column-host set to the matching overlay_contract.columns[].id, role=\"spinbutton\", and tabindex=\"0\"; use hour for an hour/immediate-departure time column even if the visible label says time/出发时间. data-wheel-track must be attached to the visible movable list itself, not to an empty placeholder; each visible option inside that track has data-wheel-option, data-wheel-column, data-wheel-index, and data-wheel-value. Generic wheel_picker columns such as year/month must preserve the declared column ids and must not be rewritten as date/hour/minute. Prefer screenshot-faithful visible option labels and active defaults; the injected runtime can preserve those defaults and augment missing overlay_contract values for later interaction. If the visual design uses a separate centered selected-value display outside data-wheel-track, mark it data-wheel-selected-display=\"<column_id>\" so the runtime can keep it synchronized. Close controls use data-wheel-close; confirm controls use data-wheel-confirm. Do not write page-local wheel-picker JavaScript; the injected runtime supplies the wheel drag, wheel, keyboard, state commit, and navigation mechanism. CRITICAL alignment rule: every wheel column must have exactly 2 empty spacer divs (data-wheel-option with empty data-wheel-value) BEFORE the initially-selected option and at least 2 option divs AFTER it, so that all columns' selected items align on the same row and sit inside the centered highlight bar. Without these spacers the selected row drifts out of the highlight band and columns appear misaligned."
                )
            pagegen_environment_input = self._strip_empty_recursive(pagegen_environment_input)
            from trace2world_pipeline.page_realizer.pagegen_prompt_boundary import validate_pagegen_prompt_boundary
            boundary_report = validate_pagegen_prompt_boundary(pagegen_environment_input)
            if boundary_report.get("blocking_error_count"):
                boundary_paths = [
                    str(item.get("path") or item.get("key") or item.get("code") or "")
                    for item in (boundary_report.get("violations") or [])
                    if isinstance(item, dict)
                ]
                raise ValueError(
                    f"PageGen prompt boundary violation for {filename}: "
                    f"{boundary_report.get('blocking_error_count')} debug/legacy input leak(s); "
                    f"paths={boundary_paths[:8]}"
                )
            parts.append(
                "\n## PageGen Render View (PAGEGEN INPUT)\n"
                "Screenshots are the primary content source — reproduce visible text and items from screenshots. "
                "`pagegen_render_view` is the single semantic authority compiled from spec, surfaces, actions, routes, runtime bindings, and resources.\n"
                "```json\n"
                + json.dumps(pagegen_environment_input, ensure_ascii=False, separators=(",", ":"))
                + "\n```"
            )

        if (not spec_authoritative_mode) and isinstance(render_scope_contract, dict) and render_scope_contract:
            parts.append(
                "\n## L0 Render Scope Contract\n"
                "This page is generated in L0-fidelity mode. Use the selected screenshots and resources only for the L0 replay surface.\n"
                "- Unique/layout screenshots define the default layout template.\n"
                "- Task-extra screenshots provide only required component instances, interaction targets, or runtime states.\n"
                "- Do not merge unrelated scroll/search/filter/state variants into one flat content pool.\n"
                "- Fixed-capacity component slots are exclusive by slot identity: when multiple candidates map to the same slot/conflict key, render the selected default candidate in the default state and keep other candidates behind their explicit state handle.\n"
                "- Do not synthesize fallback records or append text-only rows when no selected screenshot/resource supports them.\n"
                "```json\n"
                + json.dumps(complete_render_scope_contract, ensure_ascii=False, indent=2)
                + "\n```"
            )

        if (not spec_authoritative_mode) and isinstance(page_environment_projection, dict) and page_environment_projection:
            parts.append(
                "\n## Page Environment Projection (AUTHORITATIVE)\n"
                "This is the deterministic, parameterized environment contract for this materialized page. "
                "Use it before legacy content hints when deciding what exists initially, what appears only after runtime transitions, and how state mutates.\n"
                "- `initial_state` and `content_seed` define the default render state. Do not place runtime-created records, submitted queries, or committed draft values into the initial DOM unless the spec lists them as initial state.\n"
                "- `parameterized_inputs` define free-form inputs. Inputs must accept matching runtime values; do not prefill them from observed trace values unless the selected screenshot itself shows that value.\n"
                "- `transitions` are reducer obligations: fill/type updates draft/query state; commit/submit preserves existing collections and appends or upserts the committed record; navigation reaches the target filename.\n"
                "- `render_projections` bind components to state. Fixed-capacity projections render one default candidate per slot; appendable lists reuse one card/list template; runtime-only items appear only after their transition.\n"
                "- `oracles` are executable validation targets. Do not satisfy them with hidden controls, orphan required-text blocks, or text-only fallback rows.\n"
                "```json\n"
                + json.dumps(page_environment_projection, ensure_ascii=False, indent=2)
                + "\n```"
            )

        if (not spec_authoritative_mode) and rmt:
            parts.append(f"\n## Resources\n{rmt}")
        elif (not spec_authoritative_mode) and page_resources:
            raise ValueError(
                f"Page '{ev.get('filename', '?')}' has page_resources but no resource_list_text. "
                "All pages must use deterministic asset_N naming via resource_list_text."
            )


        outbound = ev.get("outbound_edges", [])
        nav_rows = []
        for e in outbound:
            if isinstance(e, dict):
                compact_edge = self._compact_navigation_edge_for_prompt(e)
                nav_rows.append(
                    f"| {compact_edge.get('to_page', '')} | {compact_edge.get('trigger_label', '')} | {compact_edge.get('to_filename', '')} |"
                )
        if (not spec_authoritative_mode) and nav_rows:
            parts.append(
                "\n## Navigation Out\n| Target | Trigger | Filename |\n|---|---|---|\n"
                + "\n".join(nav_rows)
            )

        site_pages = ev.get("site_pages", [])
        if site_pages:
            site_nav_rows = []
            for page in site_pages:
                if isinstance(page, dict):
                    site_nav_rows.append(
                        f"| {page.get('page_name', '')} | {page.get('filename', '')} |"
                    )
            if (not spec_authoritative_mode) and site_nav_rows:
                parts.append(
                    "\n## Site Navigation Targets\n| Page | Filename |\n|---|---|\n"
                    + "\n".join(site_nav_rows)
                )

        inbound = ev.get("inbound_edges", [])
        if inbound:
            compact_inbound = [
                self._compact_inbound_navigation_edge_for_prompt(item)
                for item in inbound
                if isinstance(item, dict)
            ]
            compact_inbound = [item for item in compact_inbound if item]
            if (not spec_authoritative_mode) and compact_inbound:
                parts.append(f"\nInbound: {json.dumps(compact_inbound, ensure_ascii=False)}")

        rp = ev.get("route_params", [])
        if (not spec_authoritative_mode) and rp:
            parts.append(f"\nRoute params: {json.dumps(rp)}")

        pa = ev.get("page_actions", [])
        if (not spec_authoritative_mode) and pa:
            compact_actions = [
                self._compact_page_action_for_prompt(item)
                for item in pa
                if isinstance(item, dict)
            ]
            compact_actions = [item for item in compact_actions if item]
            parts.append(
                "\n## Trace Action Evidence\n"
                "Trace actions identify interaction order only. Long natural-language task sentences are intentionally omitted because they often mention future/runtime values rather than current visible DOM labels. "
                "A trace action may identify an intended click/type/scroll, but it does not authorize new visible UI options, hidden controls, or synthetic dropdowns unless those are also observed in screenshots/content or explicitly listed in the page execution contract.\n"
                f"Task Actions: {json.dumps(compact_actions, ensure_ascii=False)}"
            )

        if (not spec_authoritative_mode) and compact_execution_obligations:
            parts.append(
                "\n## Trace-Critical Page Execution Obligations\n"
                "These are hard browser-execution obligations for the current HTML filename only. "
                "Each item means a reference trace starts from this page, performs the listed operation, and must reach `expected_filename` or the matching state effect. "
                "Bind the visible control/card/input that corresponds to the intent and screenshot position; do not add hidden task-only buttons, invisible hotspots, duplicate links, or synthetic route-index rows. "
                f"The validation viewport is `validation_viewport` CSS pixels, normally {self.environment_profile['viewport']['width']}x{self.environment_profile['viewport']['height']}. "
                "For tap/click obligations with `gesture.type == \"tap\"`, the visible actionable element for the intent MUST cover `gesture.hit_point` in that viewport, so the reference tap lands on the actual button/card/input rather than a surrounding inert container. "
                "If the semantic component is visually present but offset, adjust the real layout or tappable box to include that point; do not solve it with an invisible overlay. "
                "Hard obligation targets override the default passive/static visual-only policy: the relevant visible component must be actionable. "
                "For navigation obligations, use `GhostwebRouteExecution.navigate(expected_filename)` or the provided route execution runtime instead of raw `history.back()` or uncontracted `window.location.href`. "
                "For same-page tap obligations where `expected_filename` equals the current filename, the hit surface may focus/toggle/open inline UI but must not navigate away. "
                "For scroll obligations, preserve a real scrollable region and do not block pointer/wheel/touch events with overlays. "
                "The `command` field is validation evidence from the reference viewport, not visible UI text.\n"
                "```json\n"
                + json.dumps(compact_execution_obligations, indent=2, ensure_ascii=False)
                + "\n```"
            )


        if (not spec_authoritative_mode) and task_elements_for_page:
            te_rows = []
            for elem in task_elements_for_page:
                eid = elem.get("element_name", elem.get("element_id", elem.get("data_element_id", "")))
                text = elem.get("display_text", elem.get("text", ""))
                etype = elem.get("element_type", elem.get("type", ""))
                if eid:
                    te_rows.append(f"| {eid} | {text} | {etype} |")
            if te_rows:
                parts.append(
                    "\n## Task Element Binding (REQUIRED — DO NOT RENAME)\n"
                    "These elements MUST have data-element-id attributes with the EXACT values below.\n"
                    "Do NOT invent your own IDs — use these EXACTLY as specified:\n"
                    "| data-element-id | Display Text | Element Type |\n|---|---|---|\n"
                    + "\n".join(te_rows)
                )

        task_continuations = ev.get("task_continuations", [])
        if (not spec_authoritative_mode) and task_continuations:
            rows = []
            for item in task_continuations:
                contract = item.get("interaction_contract", {}) if isinstance(item.get("interaction_contract", {}), dict) else {}
                contract_json = json.dumps(self._compact_interaction_contract_for_prompt(contract), ensure_ascii=False)
                action_evidence = json.dumps(self._compact_task_action_evidence(item), ensure_ascii=False)
                rows.append(
                    f"| `{action_evidence}` | {item.get('continuation_kind', '')} | "
                    f"{item.get('target_filename', '')} | {item.get('reason', '')} | "
                    f"`{contract_json}` |"
                )
            parts.append(
                "\n## Task Continuations\n"
                "These are task-action continuation hints derived from reference traces. "
                "`element_name` is a task-action description, not a DOM label and not a required `data-element-id`. "
                "Do not render raw task sentences as hidden alias buttons or visible text. "
                "Bind each continuation to an observed visible control/item from the screenshots or L0 Page Execution Contract. "
                "If several continuation hints refer to the same visible control, render one visible control only; do not create duplicate hidden controls. "
                "If task wording mentions a future value, option, dropdown, or target state that is not visible in screenshots and not explicitly authorized by the L0 Page Execution Contract or interaction_contract, do not synthesize that extra UI.\n"
                "Trace action fields are higher-confidence evidence for the actually clicked/typed visible target; Reasoning fields are noisy context and must not override observed action targets or explicit L0 obligations.\n"
                "The JSON contract fields remain semantic evidence for route/state/sdk behavior. Use interaction_contract.route_binding, interaction_contract.hydration, and interaction_contract.sdk_effect; do not derive separate legacy route, hydration, or state-effect views.\n"
                "| Trace Action Evidence | Continuation Kind | Target Filename | Reason | interaction_contract |\n|---|---|---|---|---|\n"
                + "\n".join(rows)
            )
            parts.append(
                "\n## Task State Lifecycle Boundary\n"
                "- Task-created records are future runtime state: values from `create_record_draft`, "
                "`update_current_draft`, `commit_transaction`, `field_contract.typed_fields`, "
                "or downstream task descriptions must not be treated as initial collection seed data.\n"
                "- If a collection/list page has both open/read inbound navigation and save/commit inbound navigation, "
                "render the pre-mutation collection as the default initial view. Show task-created records only after "
                "the matching SDK write/submit flow returns to this page.\n"
                "- For collection/list pages, initial rows must come from screenshot/content evidence for the "
                "pre-mutation entry path or from existing collection data. Do not promote a later created record "
                "into the initial list just because it appears in a post-save screenshot or task action text."
            )

        if (not spec_authoritative_mode) and page_execution_contract:
            parts.append(
                "\n## L0 Page Execution Contract\n"
                "This contract is mandatory for reference-trace executability. "
                "For `obligation_type=visible_item`, every `required_text` item must be visible in the generated DOM or in a visible same-page interactive state. "
                "For `obligation_type=visible_control`, render each `required_text` item as a visible same-page control/chip/tab in the relevant feed/search/list control region; do not replace it with a link to another page. "
                "Visible controls are an observed control set, not permission to invent extra hidden dropdowns, alias buttons, synthetic option lists, or future target values mentioned only in task wording. "
                "If a task says a user wants a state such as a date range/filter value but no selected screenshot, content item, or explicit contract lists that option as visible, do not render that option. "
                "Do not create hidden task-only buttons that duplicate visible controls; the visible control itself must carry the interaction target. "
                "For visible filter/tab/chip controls, implement an actual same-page state transition: clicking a control must update active styling and the relevant list/feed/detail content from observed screenshots or available extracted content, not just toggle a class. "
                "`reachable_materialized_page` obligations are intentionally omitted from this compact prompt contract. "
                "Do not synthesize 'more entry', sibling-page, or route-index components for sibling Materialized Pages. "
                "Only wire navigation when a visible control/item already exists in the selected screenshots or explicit page actions. "
                "Route navigation is provided by shared runtime files. Include `<script src=\"../../phase_sdk_generation/runtime/route_manifest.js\"></script>` and `<script src=\"../../phase_sdk_generation/runtime/route_runtime.js\"></script>` before </body>. Do NOT write inline GhostwebRouteExecution implementation code — the shared runtime provides navigate(), goBack(), openOverlay(), closeOverlay(). Back controls must call `GhostwebRouteExecution.goBack()` or use `data-ghostweb-route-action=\"default_back\"`; ordinary page navigation calls `GhostwebRouteExecution.navigate(target_filename)`. For overlays/modals, use `data-ghostweb-overlay=\"overlay_id\"` on the overlay container and trigger open via `GhostwebRouteExecution.openOverlay('overlay_id')`, close via `GhostwebRouteExecution.closeOverlay()`. The runtime syncs overlay visibility from URL hash automatically. "
                "If `query_state_contract` is present, search inputs must hydrate from URL query aliases such as `q`/`query`, display the matching query's suggestions or related items while the query is active, and submit to the matching query-specific target instead of a generic search page. "
                "If `query_state_contract.hydration.render_preview_panel` is false, do not render an extra preview/suggestion panel outside the observed page layout; use only the visible result/list surfaces required by screenshots, content, or L0 obligations. "
                "If `draft_state_contract` is present instead of `query_state_contract`, this is a compose/draft/form page, not a search page. Do not hydrate from URL query aliases; do not render search suggestion panels; do not treat the text input as a search query. Use the `draft_ref` to manage draft state through WebsiteSDK, and navigate to `submit_targets[].target_filename` on submit/next-step. "
                "For `dynamic_surface_contract.state_display_bindings`, after the matching state commit, the target page's visible display surface must show `required_display_text` and be driven by the named `state_ref`; do not leave stale default labels on city/filter/selected-record indicators. "
                "Do not implement route backs, forward navigation, or commit/save navigation with raw `history.back()`, `window.location.href`, or uncontracted targets when a route execution action/runtime is provided. "
                "For current-page `layout_policy=appendable_feed`, render visible items in the scrollable feed/list/search-results region and do not collapse the region to a single representative card. "
                "When required appendable items come from non-prompt screenshots or dynamic data, reuse the repeating card/list-item layout visible in the prompt screenshots; do not satisfy them as standalone text chips or one-line fallback rows. "
                "For `layout_policy=fixed_capacity`, keep one coherent "
                "representative set and do not concatenate unrelated alternatives beyond the observed slot capacity. "
                "If `content_selection_policy.fixed_slot_conflicts` is present, render only each `default_candidate` in the initial DOM for that `slot_conflict_key`; `state_variant_candidates` must require the corresponding state/query/input before becoming visible.\n"
                "```json\n"
                + json.dumps(compact_page_execution_contract, indent=2, ensure_ascii=False)
                + "\n```"
            )

        if (not spec_authoritative_mode) and interaction_policy:
            prompt_policy = prompt_interaction_policy
            parts.append(
                "\n## Interaction Policy\n"
                f"Default component policy: {prompt_policy.get('default_component_policy', 'non_navigable')}\n"
                f"\nNavigable texts: {json.dumps(prompt_policy.get('navigable_texts', []), ensure_ascii=False)}\n"
                f"\nNon-navigable texts: {json.dumps(prompt_policy.get('non_navigable_texts', []), ensure_ascii=False)}\n"
                f"\nOmitted inline-only trace texts: {prompt_policy.get('inline_only_texts_omitted', 0)}\n"
                "Inline-only trace action/reasoning strings are not visible UI content and must not be rendered as page text or hidden controls.\n"
            )

        if (not spec_authoritative_mode) and floating_component:
            payload = floating_component.get("payload", {}) if isinstance(floating_component.get("payload", {}), dict) else {}
            items = payload.get("items", []) if isinstance(payload.get("items", []), list) else floating_component.get("items", [])
            parts.append(
                "\n## Floating Component Contract\n"
                f"Component type: {floating_component.get('component_type', 'floating_component')}\n"
                f"\nCapability id: {floating_component.get('capability_id', '')}\n"
                f"\nComponent id: {floating_component.get('component_id', '')}\n"
                f"\nHost type: {floating_component.get('host_type', '')}\n"
                f"\nTrigger element id: {floating_component.get('trigger_element_id', floating_component.get('overlay_anchor', ''))}\n"
                f"\nPanel alignment: {floating_component.get('panel_alignment', floating_component.get('overlay_position', ''))}\n"
                f"\nOffset: {json.dumps(floating_component.get('offset', {}), ensure_ascii=False)}\n"
                f"\nBackdrop: {json.dumps(floating_component.get('backdrop', False), ensure_ascii=False)}\n"
                f"\nTemplate id: {floating_component.get('template_id', '')}\n"
                f"\nVariant id: {floating_component.get('variant_id', '')}\n"
                f"\nPayload: {json.dumps(payload, ensure_ascii=False)}\n"
                f"\nItems: {json.dumps(items, ensure_ascii=False)}\n"
            )
            parts.append(
                "\n## Floating Component Template (STRICT)\n"
                "Render this as a floating component, not as a normal block in document flow.\n"
                "- Use a fullscreen overlay host (`absolute inset-0 z-*`) when host_type is `fullscreen_overlay`.\n"
                "- If backdrop=true, render a full-screen backdrop layer that closes the component when clicked.\n"
                "- Render the floating panel as an `absolute` positioned child aligned by panel_alignment and offset.\n"
                "- Opening the component MUST NOT push `main`, `footer`, or other content. No reflow hacks, negative margins, or scrollIntoView.\n"
                "- The trigger element id MUST open/close this floating component.\n"
                "- Items listed here that also appear in Navigation Out / Interaction Policy navigable texts MUST navigate to their exact target pages.\n"
                "- Follow the template structure exactly: host layer + optional backdrop + floating panel + deterministic toggle/close behavior.\n"
            )


        if (not spec_authoritative_mode) and (entity_schema or extracted_items or content_samples):
            parts.append("\n## Data")
            if entity_component_mapping or entity_task_relevance:
                compact_entity_task_relevance = self._compact_entity_task_relevance_for_prompt(entity_task_relevance)
                parts.append(f"""
### Content/Layout Metadata
```json
{json.dumps({
    "entity_component_mapping": entity_component_mapping,
    "entity_task_relevance": compact_entity_task_relevance,
}, indent=2, ensure_ascii=False)}
```
Use this metadata to decide which entity arrays belong to fixed-capacity components and which entities are task-related.""")
            if content_samples:

                parts.append(f"""
### Dynamic Data — Actual Items (pre-loaded in localStorage by business_logic.js)
```json
{json.dumps(content_samples, indent=2, ensure_ascii=False)}
```
These are the REAL data items in localStorage. Use these EXACT values when hardcoding content into the DOM.
- **data-* attributes**: Use the item `id` field (if present) or `name` field as the identifier in data attributes (e.g., data-venue-id="xxx"). NEVER invent IDs.
- **Display text**: Use the exact `name`, `title`, `price`, etc. from the items above.
- **Lifecycle caveat**: On collection/list pages, these items may include observations from multiple trace states. Values created by task actions (`create_record_draft`, `update_current_draft`, `commit_transaction`, or typed_fields) are future runtime state, not initial rows.

Schema reference (field types and relationships):
```json
{json.dumps(entity_schema, indent=2, ensure_ascii=False)}
```""")
            elif entity_schema:
                parts.append(f"""
### Dynamic Data (task-related — already in localStorage, read via SDK)
```json
{json.dumps(entity_schema, indent=2, ensure_ascii=False)}
```
This data is pre-loaded into localStorage by business_logic.js.""")

            if entity_schema or content_samples:
                parts.append("""
**CRITICAL — Runtime State vs Static Rendering**:
- **Static default**: Render initial page content as STATIC HTML when the page is read-only and the displayed collection cannot be changed by task interactions.
- **Mutable collection exception**: If this page displays an entity collection affected by `interaction_contract.sdk_effect` (create/update/select/apply/append/submit), render the list from WebsiteSDK on load and after returning to the page. Static HTML may be a fallback skeleton, but the visible rows must reflect current SDK/localStorage state.
- **Use JavaScript for**: (1) event handlers (click, submit, tab switching), (2) navigation, (3) SDK WRITE operations (addToCart, createOrder), (4) filtering/switching content in response to user interaction (e.g., clicking a category tab to show different items).
- **Do NOT use JavaScript for**: read-only initial page rendering. Do not call SDK getter methods on DOMContentLoaded just to populate content that cannot change during the task.
- **Tab/filter interaction pattern**: Render ALL tab content as hidden sections in static HTML. Use JS only to show/hide sections when user clicks a tab. Do NOT re-fetch data from SDK on tab click.
- **Business constraint enforcement**: If the data contains selection limits (max_select_*, selection_rule_*), the UI MUST enforce them — disable add buttons when limit is reached, show count indicators, prevent duplicate selections. Call SDK methods for state changes and check the return value for constraint violations.
- **Content/layout fit**: Render every item only when the target component is appendable/open-ended (feed, scroll list, search results, menu list) or expansion preserves the observed component structure. For appendable repeating components, apply the same visible item/card template to all task-related items, including items observed only in non-prompt screenshots. For fixed-capacity components (2/3/4-slot grids, fixed cards, banners, rank modules), render at most the observed slot capacity. Use Content/Layout Metadata to bind entity arrays to source components and keep task-related entities complete within the chosen page instance.""")


            static_items = {}

            entity_names_lower = set()
            if entity_schema:
                _elist = entity_schema.get("entities", None)
                if isinstance(_elist, list):
                    for _e in _elist:
                        if isinstance(_e, dict) and _e.get("name"):
                            entity_names_lower.add(_e["name"].lower())
                else:
                    entity_names_lower = {k.lower() for k in entity_schema if not k.startswith("_")}

            if content_samples:
                entity_names_lower |= {k.lower() for k in content_samples}
            for entity_type, items in extracted_items.items():
                if entity_type.lower() not in entity_names_lower and entity_type != '_metadata':
                    static_items[entity_type] = items
            if static_items:
                parts.append(f"""
### Static Content (visual-only — hardcode directly into HTML)
```json
{json.dumps(static_items, indent=2, ensure_ascii=False)}
```
Hardcode this content directly into the HTML. It is decorative/informational and does NOT need SDK or localStorage.
**Content/layout fit**: Render every item only when the target component is appendable/open-ended or expansion preserves the observed component structure. For fixed-capacity components, render one coherent representative set that fits the observed slot capacity instead of concatenating alternatives from multiple screenshots. When a slot conflict key groups multiple candidates, the initial view renders the default candidate only; variants belong to explicit state changes, not adjacent default DOM.""")


        if (not spec_authoritative_mode) and page_runtime_contract:
            parts.append(f"""
## Runtime Contract
```json
{json.dumps(page_runtime_contract, indent=2, ensure_ascii=False)}
```
This contract is authoritative for runtime state:
- SDK methods listed in `methods_by_entity` are the only state APIs this page may call.
- `operation=create/update/save/submit/select/apply` mutates SDK/localStorage state.
- `operation=list/list_page_model/get/form_page_model` reads SDK/localStorage state.
- If this page renders an entity collection that can be mutated by any listed method, render visible rows from the matching SDK list/page-model method instead of relying only on static DOM.""")

        if (not spec_authoritative_mode) and sdk_interface_contracts:
            parts.append(
                "\n## SDK Interfaces\n"
                "These are the exact WebsiteSDK methods available to this page, including nested return shapes. "
                "Call each method with positional arguments exactly as listed in `parameters`; do not wrap scalar parameters in an object unless the declared parameter itself is an object payload. "
                "When rendering records returned by a SDK getter/list method, consume fields from the returned item schema, not raw extraction-only fields. "
                "When rendering SDK records, reference concrete fields declared in the SDK item schema or entity_schema.fields[].name. "
                "Do not invent or depend on SDK-injected display abstractions such as displayName, defaultBadge, defaultBadgeText, titleName, titleName aliases, identifierLine, or taxIdMasked unless that exact field is explicitly declared in the returned item schema. "
                "Pagegen owns visual placement: choose which declared field appears in title/subtitle/badge slots from screenshots and page evidence, and bind row data attributes from stable identifier fields such as `id` or route_binding params.\n"
                "```json\n"
                + json.dumps(sdk_interface_contracts, indent=2, ensure_ascii=False)
                + "\n```"
            )
        elif not spec_authoritative_mode:
            parts.append("\n## SDK Interfaces\nNo SDK interfaces for this page.")


        valid_fns = (
            self._collect_render_view_valid_filenames(
                filename,
                authoritative_pagegen_render_view,
            )
            if spec_authoritative_mode
            else self._collect_valid_filenames(filename, ev, page_architecture)
        )
        stateful_contexts = (
            page_runtime_contract.get("stateful_continuation_contexts", [])
            if (not spec_authoritative_mode) and isinstance(page_runtime_contract, dict)
            else []
        )
        if (not spec_authoritative_mode) and stateful_contexts:
            parts.append(
                "\n## Stateful Continuation Contexts\n"
                "These contexts are the canonical bridge from interaction to target state materialization:\n"
                "```json\n"
                + json.dumps(stateful_contexts, indent=2, ensure_ascii=False)
                + "\n```\n"
                "- URL params and DOM data attributes are carriers, not semantics. The `state_handle` and `operation_intent` define the meaning.\n"
                "- Read every `state_handle.identifier_aliases` entry before deciding target variant or hydration state; do not read only one casing.\n"
                "- `operation_intent` chooses the target UI variant: create/draft stays blank, edit/configure/update/select hydrates existing state, accumulator/transaction uses current runtime state.\n"
                "- Preserve the selected state handle when navigating between pages that continue the same record, draft, cart line, option, accumulator, or transaction.\n"
                "- Hydrate visible fields/content through WebsiteSDK methods declared in the context; do not hardcode runtime-created values as initial DOM."
            )

        if spec_authoritative_mode:
            parameter_fallback_rule = (
                "- Parameter fallback: use `pagegen_render_view` state, projection, transition, and runtime "
                "authority; never infer create/edit/select semantics from URL params alone."
            )
            stateful_navigation_rules = "\n".join([
                "- pagegen_render_view is authoritative for selected records, drafts, cart lines, options, accumulators, and transactions.",
                "- URL params and DOM dataset values are carriers for render-view-declared state handles; they do not define create/edit/configure/select semantics by themselves.",
                "- Use declared reducers and projections to choose page variants and SDK hydration paths.",
            ])
        else:
            parameter_fallback_rule = (
                "- Parameter fallback: no URL params → use the explicit Stateful Continuation Context fallback if present; otherwise render a blank neutral state. Never choose the first record as an implicit semantic default."
            )
            stateful_navigation_rules = "\n".join([
                "- Stateful Continuation Contexts are authoritative for selected records, drafts, cart lines, options, accumulators, and transactions.",
                "- URL params and DOM dataset values are carriers for `state_handle`; they do not define create/edit/configure/select semantics by themselves.",
                "- Use `operation_intent` plus `state_handle.kind` to choose the page variant and SDK hydration path. For selected existing state, never render a blank create surface.",
            ])

        parts.append(f"""
## SDK & Navigation Rules
- WebsiteSDK methods are SYNCHRONOUS — use for WRITE operations and interactive filtering
- WebsiteSDK call signatures are positional. Use the exact parameter order from SDK Interfaces; do not pass `{...}` as a single object unless that exact method declares one object parameter.
- Navigation: relative .html paths with query params (detail.html?id=123)
{parameter_fallback_rule}
- Back/close/up-level controls MUST use `GhostwebRouteExecution.returnBack(action_id)` where action_id is the return_operation action_id from the navigation contract. If a return_operation is declared in page_environment_spec or route_action_slots for this back surface, bind the visible host with `data-ghostweb-action="returnBack"` and `data-ghostweb-action-id="<return_action_id>"`. Do NOT use raw `history.back()`, ad-hoc `goBack()` target guessing, or page-local `window.location.href` fallback for back/close controls. The runtime stack owner (GhostwebRouteExecution) resolves the correct target.
- If no return_operation contract exists for a back surface, use `GhostwebRouteExecution.returnBack('fallback_back')` which triggers cold-start fallback resolution.
- Explicit forward navigation targets from task continuations, site navigation targets, or named route actions still beat return operations when the action is not a generic back/close action.
- Loop event binding: store item id in data attribute, read via event.currentTarget.dataset.id
- VALID filenames: {', '.join(sorted(valid_fns))}
- href to unlisted filename = BUG. Use javascript:void(0) if no valid target
- Only create navigation handlers for controls present in route_action_slots, action_slot_route_contract, route_affordances_final, explicit Navigation Out, task continuations with page_navigation, or page_environment_spec route/transitions authority.
- A visible nav/control without a route contract must remain visual-only, current-page inactive, and passive/static. Do not emit `data-ghostweb-click-contract-kind="noninteractive"` from PageGen; runtime actionability infers unsupported visual affordances by reverse selection.
- Never infer a target by matching visible label text to a filename, page title, or site_pages entry.
- If global_navigation is empty, do not synthesize fallback global navigation from visible footer/header labels.
- Preserve visible persistent footer/header nav from screenshots as layout, but wire only destinations with explicit route authority.
- Only elements explicitly listed as navigable may navigate. Do NOT infer navigation from generic page descriptions, repeated content cards, notification entries, or visually clickable chrome.
- A single rendered component instance/card/media region must have at most one primary navigation target. If multiple route actions mention similar text but different target filenames, do not stack invisible absolute anchors or split one image/card into multiple hidden hotspots. Use separate visible controls only when the screenshots show distinct visible controls; otherwise choose the single matching action and keep the other target unrendered.
- Components/texts listed as non-navigable in Interaction Policy MUST remain static or inline-only. Do not add hrefs, click handlers, or `data-ghostweb-click-contract-kind="noninteractive"` markers. Runtime actionability tells the agent that unsupported visual affordances are not interactive by reverse selection.
- Runtime/post-commit task values must not be baked into initial DOM labels, fallback arrays, or data-element-id strings. For mutable collection rows, bind generic row actions to the row id from WebsiteSDK data; use the record's current display value only as visible row content.
- Cart/product quantity values from add/remove/update transitions are post-click projections. Initial product rows must not show quantity badges, decrement controls, or nonzero cart counts unless `page_environment_spec.initial_state` explicitly declares them.
- Detail pages must hydrate visible fields from the selected WebsiteSDK record (support both flat records and wrapper shapes such as record/item/<entity>). If the record is missing, use the explicit contract fallback or blank UI text; never choose another SDK record or hardcode a task-created/post-commit record as the default detail content.
{stateful_navigation_rules}
- If Floating Component Contract is present, render the component using the specified host_type, trigger_element_id, panel_alignment, offset, and backdrop behavior.
- For `fullscreen_overlay`, use a fullscreen host layer plus an absolute floating panel; do not render it as a normal section inside `main`.
- For `bottom_right`, place the panel near the lower-right anchor region using absolute positioning and the provided offsets.
- For REQUIRED action buttons without a valid target page filename in VALID filenames: Implement the next UI state inline on the current page (sheet, modal, drawer, expanded panel, or tab switch) based on the screenshot/task. NEVER navigate to an invented filename.
- Continuation kinds: `page_navigation` => navigate to the listed target filename; `inline_state` => stay on the same page and open/toggle the next UI state; `stateful_action` => call the SDK write/action and remain on the current page unless a valid target filename is explicitly provided.
- Task Continuations are authoritative only through `interaction_contract`.
- If `interaction_contract.route_binding.requires_params=true`, preserve the clicked/current item id in the destination URL according to `route_binding.params` and `route_binding.source`.
- Only call WebsiteSDK methods explicitly listed in **SDK Interfaces**. `interaction_contract.sdk_effect.method` is semantic evidence, not a callable API, unless the same method name is declared in **SDK Interfaces**.
- Always call the runtime instance as `window.WebsiteSDK.methodName(...)`; bare `WebsiteSDK.methodName(...)` calls are invalid and will reject the PageGen output.
- If `interaction_contract.sdk_effect.effect` is create/update/select/apply/append/submit and no declared WebsiteSDK method matches, implement only the visible inline UI state or navigate to a valid filename; do not invent SDK methods.
- If `interaction_contract.hydration.required=true`, prefill detail/edit/select UI from `hydration.state_ref` / `hydration.source_ref` using SDK getters.
- If `interaction_contract.action_semantic=edit_existing_record`, render edit mode for the referenced object, not a blank create form.
- If `interaction_contract.action_semantic=update_current_draft`, mutate the current visible form/draft only; do not navigate or create a separate record until an explicit submit/save contract appears.
- Inline JavaScript must not call querySelector/querySelectorAll with raw Tailwind arbitrary-value class selectors such as `.w-[52px]`, `.bg-[#ffd100]`, or `.translate-x-[20px]`; use data attributes or quoted `[class~="..."]` selectors instead.
- **Do NOT generate localStorage.setItem() init scripts** — business_logic.js constructor handles all data initialization

## Inline JavaScript Rules (STRICT)
Inline JS is RESTRICTED to these categories ONLY:
1. **Event wiring**: addEventListener / onclick that call WebsiteSDK.method(), GhostwebRouteExecution.navigate(), or GhostwebRouteExecution.returnBack()
2. **Navigation**: GhostwebRouteExecution.navigate(target_filename) for forward navigation, GhostwebRouteExecution.returnBack(action_id) for back/close/up-level, URL parameter reading
3. **UI state toggle**: classList.toggle/add/remove, show/hide elements, tab switching
4. **State hydration for interaction contracts**: SDK getter calls needed to prefill fields or detail text only when `interaction_contract.hydration.required=true`

FORBIDDEN inline — must use WebsiteSDK methods or GhostwebRouteExecution instead:
- Raw `history.back()` — use GhostwebRouteExecution.returnBack() instead
- Raw `window.location.href` for back/close navigation — use GhostwebRouteExecution.returnBack() instead
- Ad-hoc `goBack()` functions that guess back targets from filenames or breadcrumbs
- Price calculation, total computation
- Form validation, constraint checking
- Cart/order/booking operations
- Direct localStorage reads/writes
- Any logic that duplicates a WebsiteSDK method

If you need business logic, call WebsiteSDK.methodName(). Never reimplement it inline.""")


        if self.render_policy["platform"] == "mobile":
            parts.append(f"""
## Phone Frame <style> (the ONLY inline CSS block needed — copy EXACTLY, do NOT add classes to <body>)
```css
html {{ background: #1a1a2e; height: 100%; overflow: hidden; }}
body {{ max-width: {MOBILE_VIEWPORT_WIDTH}px !important; height: {MOBILE_VIEWPORT_HEIGHT}px !important; margin: 20px auto !important; border-radius: 44px; overflow: hidden; background: #fff; position: relative; box-shadow: 0 0 0 10px #1a1a2e, 0 0 0 13px #333; }}
body::before {{ content:''; position:absolute; top:0; left:50%; transform:translateX(-50%); width:120px; height:28px; background:#1a1a2e; border-radius:0 0 18px 18px; z-index:100; }}
body::after {{ content:''; position:absolute; bottom:8px; left:50%; transform:translateX(-50%); width:134px; height:5px; background:rgba(0,0,0,.3); border-radius:3px; }}
.app-shell {{ display:flex; flex-direction:column; height:100%; padding-top:36px; padding-bottom:13px; position:relative; overflow:hidden; }}
.app-shell main {{ flex:1; overflow-y:auto; -webkit-overflow-scrolling:touch; }}
a, a:visited {{ text-decoration: none; color: inherit; }}
```

Return JSON:
{{"html_content": "Complete HTML with embedded <style> for phone frame, Tailwind classes, Lucide icons, SDK integration, and UI JavaScript"}}""")
        else:
            desktop_viewport = self.environment_profile["viewport"]
            parts.append(f"""
## Desktop Canvas <style> (the ONLY inline CSS block needed for canvas fundamentals)
```css
html {{ width: 100%; min-height: 100%; background: #fff; }}
body {{ width: 100%; min-width: 1024px; min-height: {desktop_viewport['height']}px; margin: 0; overflow: auto; background: #fff; color: #111827; }}
.app-shell {{ width: 100%; min-height: {desktop_viewport['height']}px; position: relative; }}
a, a:visited {{ text-decoration: none; color: inherit; }}
```

Return JSON:
{{"html_content": "Complete HTML with embedded <style> for a {desktop_viewport['width']}x{desktop_viewport['height']} desktop canvas, Tailwind classes, Lucide icons, SDK integration, and UI JavaScript"}}""")

        return "\n".join(parts)

    def _build_layout_constraints(self, page_layout: Dict[str, Any], page_evidence: Dict[str, Any]) -> str:
        ev = page_evidence or {}
        layout = page_layout or {}
        rules = []


        components = ev.get("components", [])
        side_by_side_pairs = []
        full_width_components = []
        for c in components:
            if not isinstance(c, dict):
                continue
            bounds = c.get("bounds_pct", {})
            if not isinstance(bounds, dict):
                continue
            x = bounds.get("x_pct", 0)
            w = bounds.get("width_pct", 100)
            y = bounds.get("y_pct", 0)
            cid = c.get("component_id", "")
            if not cid:
                continue
            if w > 90:
                full_width_components.append(cid)
            elif x > 2 and x < 50:

                for c2 in components:
                    if c2 is c or not isinstance(c2, dict):
                        continue
                    b2 = c2.get("bounds_pct", {})
                    if not isinstance(b2, dict):
                        continue
                    x2 = b2.get("x_pct", 0)
                    y2 = b2.get("y_pct", 0)
                    if x2 > 45 and abs(y2 - y) < 5:
                        side_by_side_pairs.append((cid, c2.get("component_id", "")))


        spacing = layout.get("spacing_rules", {})
        section_pad = spacing.get("section_padding", "16px")
        comp_gap = spacing.get("component_gap", "12px")
        content_marg = spacing.get("content_margin", "12px")


        parts = ["\n## Layout Constraints (from trace screenshot analysis)"]


        parts.append(f"""
1. Equal-height grid cards: when multiple cards repeat in a row/column, the card root MUST use
   `h-full flex flex-col`. Place `mt-auto` on the bottom section (price/action/description) so it
   always aligns to the card bottom regardless of variable content height above it.""")


        if side_by_side_pairs:
            pair_str = ", ".join(f"{a}+{b}" for a, b in side_by_side_pairs)
            parts.append(f"""
2. Side-by-side components detected: {pair_str}. These MUST use flex-row (`flex flex-row`) or CSS grid
   (`grid grid-cols-2`), NOT stack vertically. Use `gap-{int(comp_gap.replace('px',''))//4}` between them.""")
        else:
            parts.append("""
2. Side-by-side components: if two components share a similar y% but have different x% ranges,
   they are side-by-side — use `flex flex-row` or `grid`, NOT vertical stacking.""")


        if full_width_components:
            fw_str = ", ".join(full_width_components[:5])
            parts.append(f"""
3. Full-width components (width_pct > 90%): {fw_str}. These span the entire container width —
   use `w-full` or `w-[100%]`, do NOT put them in a multi-column grid.""")


        parts.append(f"""
4. Component internal structure: cards and list items should use `w-full` for container-dependent
   elements (images, wrappers). Do NOT add fixed widths (`w-[Npx]`), `shrink-0`, or external margins
   to individual components — the parent container decides their width.""")


        parts.append("""
5. Bottom-row overflow check: if a row of inline elements (buttons, tags, price+action) might exceed
   the available width, wrap them in `flex-wrap` or switch to `flex-col` to prevent horizontal overflow.
   In doubt, stack vertically.""")


        if self.render_policy["platform"] == "mobile":
            parts.append(f"""
6. Outer container: the page root MUST use `w-full`. NEVER set a fixed `max-width` on inner
   containers — the phone frame ({MOBILE_VIEWPORT_WIDTH}px) already constrains the viewport. All inner widths should
   use relative units (%, `w-full`, `w-1/2`, etc.).""")

            parts.append(f"""
7. Mobile spacing rules: use compact spacing — section padding ~{section_pad}, component gaps
   ~{comp_gap}, content margins ~{content_marg}. NEVER use desktop-scale 32-64px margins.
   Touch targets MUST be minimum 44px (min-h-[44px] for buttons/tap targets).""")
        else:
            desktop_width = self.render_policy["viewport"]["width"]
            parts.append(f"""
6. Outer container: use the full {desktop_width}px desktop canvas when evidenced. Do not add a phone-width
   `max-width`, handset shell, notch, home indicator, or artificial device margin. Use responsive grid/flex
   sizing and preserve screenshot-evidenced sidebars, panes, tables, and multi-column regions.""")
            parts.append(f"""
7. Desktop spacing rules: preserve the observed desktop information density with section padding ~{section_pad},
   component gaps ~{comp_gap}, and content margins ~{content_marg}. Support mouse, keyboard focus, and wheel
   scrolling; do not inflate every control to a touch-only 44px minimum.""")


        parts.append("""
8. Aspect ratio for images/wrappers: use `aspect-square` or `aspect-[W/H]` for container-dependent
   images to maintain consistent proportions. Do NOT use fixed pixel heights.""")

        return "\n".join(parts)

    def generate_pages(self,
                      page_designs: List[Dict[str, Any]],
                      website_type: str,
                      entity_schema: Dict[str, Any],
                      interfaces: Dict[str, List[Dict[str, Any]]],
                      page_layouts: Dict[str, Any],
                      page_framework: Dict[str, Any],
                      design_analysis: Dict[str, Any] = None,
                      architecture_pages: Dict[str, Any] = None,
                      page_evidence_map: Dict[str, Any] = None,
                      extra_page_data: Dict[str, Dict] = None,
                      task_elements: Dict[str, Any] = None,
                      content_samples: Dict[str, Any] = None) -> List[GeneratedPage]:
        return asyncio.run(self.generate_pages_async(
            page_designs,
            website_type,
            entity_schema,
            interfaces,
            page_layouts,
            page_framework,
            design_analysis,
            architecture_pages,
            page_evidence_map,
            extra_page_data=extra_page_data,
            task_elements=task_elements,
            content_samples=content_samples
        ))
