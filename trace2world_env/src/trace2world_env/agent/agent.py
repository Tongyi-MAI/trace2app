"""The bundled feedback agent: one model call per environment step.

Conversation layout: system prompt, the task instruction, then per step either a screenshot
turn or an environment-feedback text turn, followed by the model's raw response. Only the
latest ``image_retention`` screenshot turns are sent. A response without a parsable tool call
gets one format-repair retry with the same history.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .chat import ChatClient
from .images import image_size
from .messages import (
    Message,
    assistant_message,
    elide_images,
    feedback_message,
    feedback_text,
    image_message,
    keep_recent_images,
    system_message,
    text_message,
)
from .parser import parse_response
from .prompt import FORMAT_REPAIR_INSTRUCTION, SYSTEM_PROMPT

logger = logging.getLogger("trace2world_env.agent")

DEFAULT_IMAGE_RETENTION = 3


@dataclass(frozen=True)
class Screenshot:
    """A fresh screenshot (base64 PNG) of the environment."""

    image: str


@dataclass(frozen=True)
class Feedback:
    """Environment feedback about the previous action, shown instead of a screenshot."""

    text: str


AgentInput = Screenshot | Feedback


@dataclass(frozen=True)
class FormatRepair:
    """Record of a format-repair retry."""

    trigger: str
    original_response: str
    original_action: dict[str, Any]
    retry_response: str
    retry_action: dict[str, Any]
    succeeded: bool


@dataclass(frozen=True)
class AgentStep:
    """The action chosen for one environment step and the response it came from.

    ``response`` is the text kept in the conversation (the retry's text after a repair);
    it is empty when no model call was made.
    """

    action: dict[str, Any]
    response: str
    finish_reason: str
    repair: FormatRepair | None = None


def _repair_failed_action() -> dict[str, Any]:
    return {
        "action_type": "unknown",
        "error": "format_repair_failed",
        "text": (
            "The repaired response was still not a valid mobile_use "
            "tool call; no task action was executed."
        ),
        "error_class": "parse_or_schema_error",
        "format_valid": False,
        "business_failure": False,
    }


class FeedbackAgent:
    """Screenshot/feedback-driven GUI agent for one episode at a time.

    Args:
        chat: model endpoint.
        image_retention: number of most recent screenshot turns kept in the prompt.
        log_dir: when set, every model step is written to ``step_NNNN.json`` in this directory.
    """

    def __init__(
        self,
        chat: ChatClient,
        *,
        image_retention: int = DEFAULT_IMAGE_RETENTION,
        log_dir: Path | None = None,
    ) -> None:
        self.chat = chat
        self.image_retention = image_retention
        self.log_dir = log_dir
        self._instruction = ""
        self._messages: list[Message] = []
        self._screenshot: str | None = None
        self._size: tuple[int | None, int | None] = (None, None)
        self._model_steps = 0

    def reset(self, instruction: str) -> None:
        """Start a new episode for ``instruction``."""
        self._instruction = instruction
        self._messages = [system_message(SYSTEM_PROMPT)]
        if instruction:
            self._messages.append(text_message(instruction))
        self._screenshot = None
        self._size = (None, None)
        self._model_steps = 0
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    async def act(self, observation: AgentInput) -> AgentStep:
        """Add ``observation`` to the conversation and return the next action.

        Before the first screenshot no model call is made and the action is ``wait``.
        An empty screenshot re-sends the previous one.
        """
        if isinstance(observation, Feedback):
            text = feedback_text(observation.text)
            if text is not None:
                self._messages.append(feedback_message(text))
            if self._screenshot is None:
                return AgentStep(action={"action_type": "wait"}, response="", finish_reason="")
        else:
            screenshot = observation.image or self._screenshot
            if not screenshot:
                logger.warning("first observation has no screenshot; emitting wait")
                return AgentStep(action={"action_type": "wait"}, response="", finish_reason="")
            self._add_screenshot(screenshot)
        return await self._generate()

    def _add_screenshot(self, screenshot: str) -> None:
        size = image_size(screenshot)
        if size is not None:
            self._size = size
        self._screenshot = screenshot
        self._messages.append(image_message(screenshot))

    async def _generate(self) -> AgentStep:
        width, height = self._size
        prompt = keep_recent_images(self._messages, self.image_retention)
        reply = await self.chat.complete(prompt)
        parsed = parse_response(reply.content, width, height)
        action = parsed.action
        repair: FormatRepair | None = None
        trigger = parsed.repair_trigger
        if trigger is not None:
            retry_prompt = [
                *prompt,
                assistant_message(reply.content),
                text_message(FORMAT_REPAIR_INSTRUCTION),
            ]
            retry = await self.chat.complete(retry_prompt)
            retried = parse_response(retry.content, width, height)
            repair = FormatRepair(
                trigger=trigger,
                original_response=reply.content,
                original_action=parsed.action,
                retry_response=retry.content,
                retry_action=retried.action,
                succeeded=retried.valid,
            )
            action = retried.action if retried.valid else _repair_failed_action()
            reply = retry
        self._messages.append(assistant_message(reply.content))
        step = AgentStep(
            action=action,
            response=reply.content,
            finish_reason=reply.finish_reason,
            repair=repair,
        )
        self._write_log(prompt, step)
        self._model_steps += 1
        return step

    def _write_log(self, prompt: list[Message], step: AgentStep) -> None:
        if self.log_dir is None:
            return
        record = {
            "step": self._model_steps,
            "instruction": self._instruction,
            "messages": elide_images(prompt),
            "response": step.response,
            "action": step.action,
            "finish_reason": step.finish_reason,
            "format_repair": asdict(step.repair) if step.repair is not None else None,
        }
        path = self.log_dir / f"step_{self._model_steps:04d}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
