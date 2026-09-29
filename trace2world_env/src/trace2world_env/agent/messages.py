"""Chat-message construction and history management for the agent."""

from __future__ import annotations

import copy
from typing import Any

from .prompt import FEEDBACK_TURN_PREFIX

Message = dict[str, Any]

MAX_FEEDBACK_CHARS = 4000
_IGNORED_FEEDBACK = frozenset({"", "ok", "true", "success", "none", "null"})


def system_message(text: str) -> Message:
    """Return the system message."""
    return {"role": "system", "content": text}


def text_message(text: str) -> Message:
    """Return a user message with one text part."""
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def image_message(b64_png: str) -> Message:
    """Return a user message carrying one screenshot as a PNG data URL."""
    return {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}}
        ],
    }


def assistant_message(text: str) -> Message:
    """Return an assistant message with the raw model response."""
    return {"role": "assistant", "content": text}


def feedback_text(feedback: str) -> str | None:
    """Normalise environment feedback for the model; None when it carries no information.

    Whitespace is stripped, trivial acknowledgements (``ok``, ``success``, ...) are dropped, and
    text longer than ``MAX_FEEDBACK_CHARS`` is truncated.
    """
    text = feedback.strip()
    if text.lower() in _IGNORED_FEEDBACK:
        return None
    if len(text) > MAX_FEEDBACK_CHARS:
        text = text[:MAX_FEEDBACK_CHARS] + "...[truncated]"
    return text


def feedback_message(text: str) -> Message:
    """Return the user turn that reports normalised environment feedback."""
    return text_message(FEEDBACK_TURN_PREFIX + text)


def _is_image_turn(message: Message) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and isinstance(content[0], dict)
        and content[0].get("type") == "image_url"
    )


def keep_recent_images(messages: list[Message], retention: int) -> list[Message]:
    """Return a copy of ``messages`` without all but the latest ``retention`` screenshot turns."""
    image_turns = [i for i in range(len(messages) - 1, -1, -1) if _is_image_turn(messages[i])]
    dropped = set(image_turns[retention:])
    return [copy.deepcopy(m) for i, m in enumerate(messages) if i not in dropped]


def elide_images(messages: list[Message]) -> list[Message]:
    """Return a copy of ``messages`` with image data replaced by a length marker (for logs)."""
    out = copy.deepcopy(messages)
    for message in out:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                raw = part.get("image_url")
                url = raw["url"] if isinstance(raw, dict) else (raw or "")
                part["image_url"] = f"<elided len={len(url)}>"
    return out
