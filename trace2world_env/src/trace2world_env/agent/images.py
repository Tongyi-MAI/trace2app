"""Screenshot facts the agent needs: its pixel size and the model's pixel budget.

Screenshots are sent unchanged; the model server resizes them to the pixel budget carried in
every request (see :class:`~trace2world_env.agent.chat.ChatClient`). The original size is kept
to map the model's 0-999 coordinates back to screenshot pixels.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

logger = logging.getLogger("trace2world_env.agent.images")


@dataclass(frozen=True)
class PixelBudget:
    """Bounds on the pixel count of one screenshot as seen by the model."""

    min_pixels: int
    max_pixels: int


RELEASE_PIXEL_BUDGET = PixelBudget(min_pixels=4096, max_pixels=2073600)
"""Pixel budget the released policy was trained with."""


def image_size(b64_image: str) -> tuple[int, int] | None:
    """Return ``(width, height)`` of a base64 image, or None when it cannot be decoded."""
    if not b64_image:
        return None
    try:
        with Image.open(BytesIO(base64.b64decode(b64_image))) as image:
            return image.size
    except Exception as exc:
        logger.warning("failed to decode screenshot size: %s", exc)
        return None
