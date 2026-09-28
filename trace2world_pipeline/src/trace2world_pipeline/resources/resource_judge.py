
import os
import json
import asyncio
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from trace2world_pipeline.core.logger import Trace2WorldLogger
from trace2world_pipeline.core.llm_caller import (
    call_openai_api_json_async,
    encode_image,
)
from trace2world_pipeline.resources.screenshot_resource_extractor import ExtractedResource
from trace2world_pipeline.app_spec.environment_profile import resolve_environment_profile
from trace2world_pipeline.app_spec.environment_prompt_policy import (
    build_environment_prompt_policy,
    prompt_policy_manifest,
)


@dataclass
class JudgmentResult:
    resource_path: str
    usable: bool
    confidence: float
    category: str
    reason: str
    suggested_usage: str = ""
    has_state_indicator: bool = False
    overlapping_component_count: int = 0
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ResourceJudge:

    def __init__(self, logger: Trace2WorldLogger = None,
                 model: str | None = None,
                 reasoning_effort: str = "low",
                 max_concurrent: int = 10,
                 environment_profile: Dict[str, Any] | None = None):
        self.logger = logger or Trace2WorldLogger()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_concurrent = max_concurrent
        self.environment_profile = resolve_environment_profile(environment_profile)
        self.prompt_policy = build_environment_prompt_policy(
            self.environment_profile,
            "resource_analysis",
        )

    async def judge_resource(self, image_path: str, context: Dict[str, Any] = None) -> JudgmentResult:
        if not os.path.exists(image_path):
            self.logger.log("resource_judge", f"File not found: {image_path}")
            return JudgmentResult(
                resource_path=image_path,
                usable=False,
                confidence=0.0,
                category="unusable",
                reason="File not found",
            )

        prompt = self._build_judge_prompt(context)

        try:
            image_url = encode_image(image_path)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ]
            reply, _usage = await call_openai_api_json_async(
                messages,
                max_retries=3,
                reasoning_effort=self.reasoning_effort,
                stage="resource_judge",
                model=self.model,
            )

            if reply is None:
                raise ValueError("LLM returned None")

            result = json.loads(reply) if isinstance(reply, str) else reply

            judgment = JudgmentResult(
                resource_path=image_path,
                usable=bool(result.get("usable", False)),
                confidence=float(result.get("confidence", 0.0)),
                category=result.get("category", "unusable"),
                reason=result.get("reason", ""),
                suggested_usage=result.get("suggested_usage", ""),
                has_state_indicator=bool(result.get("has_state_indicator", False)),
                overlapping_component_count=int(result.get("overlapping_component_count", 0)),
                metadata={
                    "environment_profile_hash": self.environment_profile["profile_hash"],
                    "environment_prompt_policy": prompt_policy_manifest(
                        self.environment_profile,
                        "resource_analysis",
                    ),
                },
            )
        except Exception as e:
            self.logger.log("resource_judge", f"Error judging {image_path}: {e}")
            judgment = JudgmentResult(
                resource_path=image_path,
                usable=False,
                confidence=0.0,
                category="unusable",
                reason=f"Judgment failed: {e}",
            )

        self.logger.log(
            "resource_judge",
            f"Judged {os.path.basename(image_path)}: "
            f"usable={judgment.usable}, confidence={judgment.confidence:.2f}, "
            f"category={judgment.category}",
        )
        return judgment

    async def batch_judge(self, resources: List[ExtractedResource],
                          context: Dict[str, Any] = None) -> List[JudgmentResult]:
        if not resources:
            return []

        self.logger.log("resource_judge", f"Batch judging {len(resources)} resources")

        judgments = await asyncio.gather(*[self.judge_resource(r.resource_path, context) for r in resources])


        usable_count = sum(1 for j in judgments if j.usable)
        category_dist: Dict[str, int] = {}
        for j in judgments:
            category_dist[j.category] = category_dist.get(j.category, 0) + 1

        self.logger.log(
            "resource_judge",
            f"Batch complete: {usable_count}/{len(judgments)} usable. "
            f"Categories: {category_dist}",
        )
        return list(judgments)

    def _build_judge_prompt(self, context: Dict[str, Any] = None) -> str:
        context = context or {}

        context_section = ""
        if context.get("website_type"):
            context_section += f"\nTarget website type: {context['website_type']}"
        if context.get("expected_role"):
            context_section += f"\nExpected role for this image: {context['expected_role']}"

        prompt = f"""You are an image resource evaluator. The attached image was extracted from a mobile app screenshot using an automated tool. Judge whether it is usable as a standalone resource for website generation.

USABILITY PRINCIPLE — an image is usable ONLY if BOTH conditions are met:
1. For icons: the icon must NOT carry any state indicator (no active/selected/disabled highlight, no notification badge, no unread count, no red dot, no numeric overlay).
2. For all images: the extracted crop must contain at most 1 other UI component besides the target resource itself. If 2+ unrelated components (other icons, text blocks, cards, buttons) are visible in the crop, the image is NOT usable.

USABLE examples:
- A single category icon illustration (e.g. a food delivery mascot, a hotel building) with no badge/count overlay
- A standalone food photo with at most a small price tag
- A clean brand logo
- A merchant storefront photo with no overlapping UI

NOT USABLE examples:
- An icon with a red notification dot or "99+" badge (state indicator)
- An icon shown in "selected/active" state (highlighted background, underline)
- A crop containing 2+ adjacent category icons packed together
- A banner composite mixing text + icon + background
- Tiny fragments, shadows, dividers, or extraction artifacts
{context_section}

Respond with JSON:
{{
  "usable": true or false,
  "confidence": 0.0 to 1.0,
  "category": "photo" | "icon" | "logo" | "background" | "decoration" | "ui_element" | "promotional" | "unusable",
  "has_state_indicator": true or false,
  "overlapping_component_count": <integer, how many OTHER components appear in the crop besides the target>,
  "reason": "brief explanation",
  "suggested_usage": "how this image could be used, e.g. product thumbnail, category icon, hero background"
}}"""
        if self.environment_profile["platform"] == "desktop":
            prompt = prompt.replace("mobile app screenshot", "desktop web/application screenshot", 1)
            prompt += "\n\nEnvironment contract:\n" + self.prompt_policy["rendered_section"]
        return prompt

    def filter_usable(self, judgments: List[JudgmentResult],
                      min_confidence: float = 0.5) -> List[JudgmentResult]:
        filtered = [
            j for j in judgments
            if j.usable and j.confidence >= min_confidence
        ]
        self.logger.log(
            "resource_judge",
            f"Filtered: {len(filtered)}/{len(judgments)} passed (min_confidence={min_confidence})",
        )
        return filtered
