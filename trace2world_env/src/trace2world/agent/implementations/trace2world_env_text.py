
from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from trace2world.agent._base import AgentVariant
from trace2world.agent._types import GenerateResult
from trace2world.agent.prompts import render
from trace2world.agent.tool_parser import ToolParser
from trace2world.agent.variants import register_variant

from .qwen_mobile_use import QwenMobileUsePolicy, QwenToolParser

# Server-side resize bounds of the release policy (pixels per screenshot).
RELEASE_MAX_PIXELS = 2073600
RELEASE_MIN_PIXELS = 4096

logger = logging.getLogger(__name__)

_MAX_FEEDBACK_CHARS = 4000


T2W_IN_APP_ACTIONS = frozenset({
    "click",
    "double_click",
    "long_press",
    "type",
    "drag",
    "swipe",
    "scroll",
    "wait",
    "terminate",
})


@ToolParser.register("trace2world_in_app")
class QwenT2WInAppToolParser(QwenToolParser):

    VALID_ACTIONS = T2W_IN_APP_ACTIONS

    def _convert_to_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action_type", arguments.get("action", ""))
        if action in {"swipe", "scroll"}:
            arguments = {**arguments, "action_type": "drag"}
        return super()._convert_to_action(arguments)

_FEEDBACK_KEYS = (
    "env_text",
    "actionability_feedback",
    "environment_text",
    "textual_feedback",
    "last_action_feedback",
    "feedback",
    "notice",
    "warning",
    "message",
    "error",
    "text",
)

_NESTED_FEEDBACK_KEYS = (
    "info",
    "metadata",
    "env_info",
    "extra",
    "result",
    "step_result",
    "action_result",
    "last_action_result",
)

_BINARY_OR_LARGE_KEYS = {
    "image",
    "screenshot",
    "b64_png",
    "png",
    "bytes",
    "accessibility_tree",
}

_IGNORED_FEEDBACK_VALUES = {"", "ok", "true", "success", "none", "null"}

_ENV_TEXT_INSTRUCTION = (
    "When the environment provides textual feedback after an action, treat it "
    "as the authoritative result of that action. ERROR[FORMAT_INVALID], "
    "ERROR[INVALID_ACTION], ERROR[NOT_INTERACTIVE], and "
    "ERROR[NO_EDITABLE_FOCUS] mean that the previous action was rejected and "
    "no page state changed. The feedback describes only the previous action "
    "outcome; use the next screenshot or feedback to decide the next action."
)

_FORMAT_REPAIR_INSTRUCTION = (
    "Your previous response could not be parsed as exactly one valid "
    "mobile_use tool call. Re-emit the same intended next action exactly once "
    "using the required <think>...</think> and "
    "<tool_call>{\"name\":\"mobile_use\",\"arguments\":{...}}</tool_call> "
    "format. Do not change the intended action, add task facts, or write any "
    "text outside those tags."
)

_MALFORMED_JSON_ERROR_PREFIXES = (
    "Expecting property name enclosed in double quotes",
    "Expecting ',' delimiter",
    "Expecting ':' delimiter",
    "Expecting value",
    "Unterminated string",
    "Invalid \\escape",
    "Extra data",
)


def _format_repair_trigger(error: str) -> str | None:
    if error == "No <tool_call> block found":
        return "missing_tool_call_block"
    if any(error.startswith(prefix) for prefix in _MALFORMED_JSON_ERROR_PREFIXES):
        return "malformed_tool_call_json"
    return None


def _render_system_prompt(apps: list[str] | None = None) -> str:


    del apps
    return render("trace2world_env_text_system.j2").strip() + "\n\n" + _ENV_TEXT_INSTRUCTION


_SYSTEM_PROMPT_WITH_ENV_TEXT = _render_system_prompt()


def _stringify_feedback(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return _stringify_feedback(message)
        compact = {
            str(k): v
            for k, v in value.items()
            if str(k) not in _BINARY_OR_LARGE_KEYS and v is not None
        }
        if not compact:
            return None
        text = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, (list, tuple)):
        if not value:
            return None
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value).strip()

    if text.lower() in _IGNORED_FEEDBACK_VALUES:
        return None
    if len(text) > _MAX_FEEDBACK_CHARS:
        text = text[:_MAX_FEEDBACK_CHARS] + "...[truncated]"
    return text


def _extract_textual_feedback(observation: dict[str, Any]) -> str | None:
    for key in _FEEDBACK_KEYS:
        if key in observation:
            text = _stringify_feedback(observation.get(key))
            if text:
                return text

    for outer_key in _NESTED_FEEDBACK_KEYS:
        nested = observation.get(outer_key)
        if not isinstance(nested, dict):
            continue
        for key in _FEEDBACK_KEYS:
            if key in nested:
                text = _stringify_feedback(nested.get(key))
                if text:
                    return text
        nested_text = _stringify_feedback(nested)
        if nested_text:
            return nested_text

    return None


class QwenT2WReleaseEnvTextPolicy(QwenMobileUsePolicy):

    SUPPORTED_ACTIONS = T2W_IN_APP_ACTIONS

    async def select_action(self, observation: dict[str, Any]) -> dict[str, Any]:
        feedback = _extract_textual_feedback(observation)
        text_only_feedback = bool(feedback) and (
            observation.get("observation_kind") == "text"
            or not bool(observation.get("image"))
        )
        if feedback and text_only_feedback:
            feedback_body = feedback
            feedback_turn = (
                "Environment feedback after the previous action:\n"
                f"{feedback_body}"
            )


            self._messages.append({
                "role": "user",
                "content": [{"type": "text", "text": feedback_turn}],
            })
            observation = {
                **observation,
                "observation_kind": "text",
                "_prompt_observation_kind": "text_feedback",
                "_prompt_observation_text": feedback_turn,
            }
            logger.debug("[QwenT2WEnvTextPolicy] appended env feedback event turn")

        return await super().select_action(observation)

    async def _repair_generation_result(
        self,
        *,
        result: GenerateResult,
        messages: list[dict[str, Any]],
        tools: list[dict] | None,
    ) -> tuple[GenerateResult, dict[str, Any] | None]:
        original_action = result.info.get("action_dict", {})
        original_error = str(original_action.get("error") or "")
        repair_trigger = _format_repair_trigger(original_error)
        if (
            original_action.get("action_type") != "unknown"
            or repair_trigger is None
        ):
            return result, None

        repair_messages = [
            *messages,
            {"role": "assistant", "content": result.content},
            {
                "role": "user",
                "content": [{"type": "text", "text": _FORMAT_REPAIR_INSTRUCTION}],
            },
        ]
        repaired = await self.chat_model.generate(
            repair_messages,
            self.sampling_params,
            tools=tools,
            original_image_width=self.original_image_width,
            original_image_height=self.original_image_height,
            timing=self.timing,
        )
        repaired_action = repaired.info.get("action_dict", {})
        repaired_action_type = repaired_action.get("action_type")
        repair_succeeded = (
            repaired_action_type not in {None, "", "unknown", "finished"}
            and float(repaired.info.get("format_reward", 0.0)) >= 1.0
        )
        provenance = {
            "attempted": True,
            "trigger": repair_trigger,
            "model_call_count": 2,
            "same_model_sampling_and_deployment": True,
            "format_only_instruction": _FORMAT_REPAIR_INSTRUCTION,
            "original_response_text": result.content,
            "original_action_dict": original_action,
            "original_finish_reason": result.finish_reason,
            "retry_response_text": repaired.content,
            "retry_action_dict": repaired_action,
            "retry_finish_reason": repaired.finish_reason,
            "succeeded": repair_succeeded,
        }
        if repair_succeeded:
            return repaired, provenance

        failed_info = {
            **repaired.info,
            "action_dict": {
                "action_type": "unknown",
                "error": "format_repair_failed",
                "text": (
                    "The repaired response was still not a valid mobile_use "
                    "tool call; no task action was executed."
                ),
                "error_class": "parse_or_schema_error",
                "format_valid": False,
                "business_failure": False,
            },
            "format_reward": 0.0,
            "is_done": False,
            "failure_attribution": {
                "owner": "runner_parser",
                "error_class": "parse_or_schema_error",
                "business_failure": False,
                "recoverable_via_env_text": True,
            },
        }


        return replace(repaired, info=failed_info), provenance


register_variant(AgentVariant(
    name="trace2world_env_text",
    policy_cls=QwenT2WReleaseEnvTextPolicy,
    parser_name="trace2world_in_app",
    system_prompt=_SYSTEM_PROMPT_WITH_ENV_TEXT,
    system_prompt_renderer=_render_system_prompt,
    default_image_retention=3,
    preserve_thinking=True,
    description=(
        "Qwen release-agent parity plus WebEnv textual actionability "
        "feedback turns"
    ),
    extra={
        "release_server_pixels": True,
        "max_pixels": RELEASE_MAX_PIXELS,
        "min_pixels": RELEASE_MIN_PIXELS,
        "enable_thinking": True,
        "accepts_env_text_feedback": True,
        "model_visible_actions": tuple(sorted(T2W_IN_APP_ACTIONS)),
    },
))
