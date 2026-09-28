
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, eq=False)
class EnvironmentEvaluation:

    raw_reward: float | None
    reason: str | None = None
    terminal_success: bool | None = None
    checkpoint_reward_vector: list[dict[str, Any]] = field(default_factory=list)
    ability_reward_vector: dict[str, float] = field(default_factory=dict)
    nonzero_checkpoint_reached: bool | None = None
    env_valid: bool | None = None
    format_valid: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> float | None:
        return self.raw_reward

    def __iter__(self) -> Iterator[float | str | None]:
        yield self.raw_reward
        yield self.reason

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> float | str | None:
        return (self.raw_reward, self.reason)[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EnvironmentEvaluation):
            return self.__dict__ == other.__dict__
        if isinstance(other, tuple) and len(other) == 2:
            return (self.raw_reward, self.reason) == other
        return NotImplemented

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EnvironmentEvaluation:
        raw = payload.get("raw_reward", payload.get("score", payload.get("reward")))
        try:
            raw_reward = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            raw_reward = None
        details = payload.get("evaluation")
        if not isinstance(details, dict):


            details = {
                key: value
                for key, value in payload.items()
                if key not in {"score", "reward"}
            }

        def _pick(name: str, default: Any = None) -> Any:
            return payload[name] if name in payload else details.get(name, default)

        checkpoint_vector = _pick("checkpoint_reward_vector", [])
        ability_vector = _pick("ability_reward_vector", {})
        return cls(
            raw_reward=raw_reward,
            reason=payload.get("reason"),
            terminal_success=_optional_bool(_pick("terminal_success")),
            checkpoint_reward_vector=(
                checkpoint_vector if isinstance(checkpoint_vector, list) else []
            ),
            ability_reward_vector=(
                ability_vector if isinstance(ability_vector, dict) else {}
            ),
            nonzero_checkpoint_reached=_optional_bool(
                _pick("nonzero_checkpoint_reached")
            ),
            env_valid=_optional_bool(_pick("env_valid")),
            format_valid=_optional_bool(_pick("format_valid")),
            details=details,
        )

    def to_payload(self, *, raw_reward: float | None = None) -> dict[str, Any]:
        value = self.raw_reward if raw_reward is None else raw_reward
        details = dict(self.details)
        details.update(
            {
                "raw_reward": value,
                "reason": self.reason,
                "terminal_success": self.terminal_success,
                "checkpoint_reward_vector": self.checkpoint_reward_vector,
                "ability_reward_vector": self.ability_reward_vector,
                "nonzero_checkpoint_reached": self.nonzero_checkpoint_reached,
                "env_valid": self.env_valid,
                "format_valid": self.format_valid,
            }
        )
        payload = {
            "raw_reward": value,
            "reason": self.reason,
            "terminal_success": self.terminal_success,
            "checkpoint_reward_vector": self.checkpoint_reward_vector,
            "ability_reward_vector": self.ability_reward_vector,
            "nonzero_checkpoint_reached": self.nonzero_checkpoint_reached,
            "env_valid": self.env_valid,
            "format_valid": self.format_valid,
            "evaluation": details,
        }
        evaluation_metadata = details.get("evaluation_metadata")
        if isinstance(evaluation_metadata, dict):
            payload["evaluation_metadata"] = evaluation_metadata
        return payload


def coerce_environment_evaluation(value: Any) -> EnvironmentEvaluation:
    if isinstance(value, EnvironmentEvaluation):
        return value
    if isinstance(value, dict):
        return EnvironmentEvaluation.from_payload(value)
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        raw, reason = value[0], value[1]
        return EnvironmentEvaluation(
            raw_reward=float(raw) if raw is not None else None,
            reason=str(reason) if reason is not None else None,
        )
    if value is None:
        return EnvironmentEvaluation(raw_reward=None)
    return EnvironmentEvaluation(raw_reward=float(value))


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
