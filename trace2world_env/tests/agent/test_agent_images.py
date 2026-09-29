"""Unit tests for screenshot size, the pixel budget, and conversation bookkeeping."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO

from PIL import Image

from trace2world_env.agent.chat import ChatClient
from trace2world_env.agent.images import RELEASE_PIXEL_BUDGET, PixelBudget, image_size
from trace2world_env.agent.messages import (
    MAX_FEEDBACK_CHARS,
    feedback_text,
    image_message,
    keep_recent_images,
    text_message,
)


def png(width: int, height: int) -> str:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_release_pixel_budget_is_sent_with_every_request() -> None:
    assert RELEASE_PIXEL_BUDGET == PixelBudget(min_pixels=4096, max_pixels=2073600)

    async def body() -> dict:
        chat = ChatClient("http://127.0.0.1:1/v1/", "m")
        try:
            return chat.request_body([])
        finally:
            await chat.aclose()

    request = asyncio.run(body())
    assert (request["max_pixels"], request["min_pixels"]) == (2073600, 4096)
    assert list(request) == [
        "model", "messages", "max_tokens", "temperature", "top_p",
        "enable_thinking", "chat_template_kwargs", "max_pixels", "min_pixels",
    ]


def test_image_size() -> None:
    assert image_size(png(390, 844)) == (390, 844)
    assert image_size("") is None
    assert image_size(base64.b64encode(b"nope").decode()) is None


def test_feedback_text_normalisation() -> None:
    assert feedback_text("  ERROR[X] rejected.\n") == "ERROR[X] rejected."
    for trivial in ("", "  ", "OK", "success", "None", "null", "True"):
        assert feedback_text(trivial) is None
    long = "y" * (MAX_FEEDBACK_CHARS + 10)
    assert feedback_text(long) == "y" * MAX_FEEDBACK_CHARS + "...[truncated]"


def test_keep_recent_images_drops_oldest_screenshot_turns() -> None:
    messages = [{"role": "system", "content": "s"}, text_message("task")]
    for index in range(5):
        messages.append(image_message(f"img{index}"))
        messages.append({"role": "assistant", "content": f"a{index}"})
    kept = keep_recent_images(messages, 3)
    urls = [m["content"][0]["image_url"]["url"] for m in kept if m["role"] == "user" and m["content"][0]["type"] == "image_url"]
    assert urls == [f"data:image/png;base64,img{i}" for i in (2, 3, 4)]
    assert [m["content"] for m in kept if m["role"] == "assistant"] == [f"a{i}" for i in range(5)]
    assert kept is not messages and kept[0] is not messages[0]
