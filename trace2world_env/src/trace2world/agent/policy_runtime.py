
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image


logger = logging.getLogger(__name__)


class MessagesLogger:
    def __init__(self, log_dir: Any = None) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else None

    @property
    def enabled(self) -> bool:
        return self.log_dir is not None

    def ensure_dir(self) -> None:
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def write_step(self, step_idx: int, payload: dict[str, Any]) -> None:
        if self.log_dir is None:
            return
        path = self.log_dir / f"step_{step_idx:04d}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _decode_png_size(b64_png: str) -> tuple[int, int] | None:
    if not b64_png:
        return None
    try:
        with Image.open(BytesIO(base64.b64decode(b64_png))) as image:
            return image.size
    except Exception as exc:
        logger.warning("failed to decode screenshot size: %s", exc)
        return None


@dataclass
class StepRecord:
    response_text: str
    observation_screenshot: str
    action_dict: dict[str, Any]
    format_reward: float
    is_terminal: bool
    rendered_messages: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    action_text: str = ""
    observation_in_prompt: bool = True
    observation_kind: str = ""
    observation_text: str = ""


