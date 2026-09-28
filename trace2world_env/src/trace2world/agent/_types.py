
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class FunctionCall(BaseModel):

    arguments: str
    name: str


@dataclass
class GenerateResult:

    content: str
    function_calls: list[FunctionCall]
    info: dict[str, Any]
    finish_reason: str


@runtime_checkable
class ChatModel(Protocol):

    def reset(self) -> None: ...

    async def generate(
        self,
        messages: list[dict[str, Any]],
        sampling_params: dict[str, Any],
        *,
        tools: list[dict] | None = None,
        original_image_width: int | None = None,
        original_image_height: int | None = None,
        timing: Any | None = None,
    ) -> GenerateResult: ...
