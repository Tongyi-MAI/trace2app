
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ._types import FunctionCall

__all__ = ["FunctionCall", "ToolParser"]


class ToolParser(ABC):

    _registry: dict[str, type["ToolParser"]] = {}

    def __init__(self, tokenizer: Any) -> None:
        self.tokenizer = tokenizer

    @abstractmethod
    async def extract_tool_calls(
        self,
        responses_ids: list[int],
    ) -> tuple[dict[str, Any], list[FunctionCall]]:
        raise NotImplementedError

    @classmethod
    def get_tool_parser(
        cls,
        name: str,
        tokenizer: Any,
        **kwargs: Any,
    ) -> "ToolParser":
        parser = cls._registry.get(name)
        if parser is None:
            raise ValueError(
                f"Unknown tool parser: {name}. Available: {sorted(cls._registry)}"
            )
        filtered = {key: value for key, value in kwargs.items() if value is not None}
        return parser(tokenizer, **filtered)

    @classmethod
    def register(cls, name: str):
        def decorator(subclass: type["ToolParser"]) -> type["ToolParser"]:
            if name in cls._registry:
                raise ValueError(f"Tool parser {name!r} is already registered")
            cls._registry[name] = subclass
            return subclass

        return decorator
