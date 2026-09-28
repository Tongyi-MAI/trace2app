
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class LifecyclePolicy(Protocol):
    NATIVE_ACTION_SPACE: Any

    def initialize(self, instruction: str) -> None: ...
    def done(self) -> None: ...
    async def select_action(self, observation: dict[str, Any]) -> dict[str, Any]: ...


class AgentPolicy(LifecyclePolicy, Protocol):
    step_records: list[Any]
    last_prediction: str | None


@dataclass(frozen=True)
class AgentVariant:
    name: str
    policy_cls: type
    parser_name: str
    system_prompt: str
    default_image_retention: int = 3
    system_prompt_renderer: Callable[[list[str] | None], str] | None = None
    description: str = ""
    preserve_thinking: bool = False
    stop_sequences: tuple[str, ...] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def render_system_prompt(self, available_apps: list[str] | None = None) -> str:
        if available_apps is None:
            return self.system_prompt
        if self.system_prompt_renderer is None:
            raise ValueError(
                f"Agent variant {self.name!r} does not support available_apps"
            )
        return self.system_prompt_renderer(available_apps)
