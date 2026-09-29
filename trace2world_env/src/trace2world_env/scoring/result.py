"""The result record of a browser checkpoint evaluation and its wire payload."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class EvaluationResult:
    """Reward and diagnostics of one browser-evaluated episode.

    ``details`` is the full evaluator payload (reward vector, grounded checkpoint ledger,
    ``evaluation_metadata``); the typed fields mirror its headline values.
    """

    raw_reward: float | None
    reason: str | None = None
    terminal_success: bool | None = None
    checkpoint_reward_vector: list[dict[str, Any]] = field(default_factory=list)
    ability_reward_vector: dict[str, float] = field(default_factory=dict)
    nonzero_checkpoint_reached: bool | None = None
    env_valid: bool | None = None
    format_valid: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, reason: str, details: dict[str, Any] | None = None) -> EvaluationResult:
        """A zero-reward, environment-invalid result (evaluator missing or crashed)."""
        return cls(
            raw_reward=0.0,
            reason=reason,
            terminal_success=False,
            env_valid=False,
            format_valid=False,
            details=details or {},
        )

    @classmethod
    def from_details(
        cls, raw_reward: float, reason: str, details: dict[str, Any]
    ) -> EvaluationResult:
        """Build a result whose headline fields are read from ``details``."""
        checkpoint_vector = details.get("checkpoint_reward_vector", [])
        ability_vector = details.get("ability_reward_vector", {})
        return cls(
            raw_reward=float(raw_reward),
            reason=reason,
            terminal_success=_optional_bool(details.get("terminal_success")),
            checkpoint_reward_vector=(
                checkpoint_vector if isinstance(checkpoint_vector, list) else []
            ),
            ability_reward_vector=ability_vector if isinstance(ability_vector, dict) else {},
            nonzero_checkpoint_reached=_optional_bool(details.get("nonzero_checkpoint_reached")),
            env_valid=_optional_bool(details.get("env_valid")),
            format_valid=_optional_bool(details.get("format_valid")),
            details=details,
        )

    def with_details(self, details: dict[str, Any]) -> EvaluationResult:
        """Copy of this result with ``details`` replaced."""
        return replace(self, details=details)

    def to_payload(self) -> dict[str, Any]:
        """The evaluation payload: headline fields plus ``evaluation`` (= details)."""
        headline = {
            "raw_reward": self.raw_reward,
            "reason": self.reason,
            "terminal_success": self.terminal_success,
            "checkpoint_reward_vector": self.checkpoint_reward_vector,
            "ability_reward_vector": self.ability_reward_vector,
            "nonzero_checkpoint_reached": self.nonzero_checkpoint_reached,
            "env_valid": self.env_valid,
            "format_valid": self.format_valid,
        }
        details = {**self.details, **headline}
        payload: dict[str, Any] = {
            "score": self.raw_reward or 0.0,
            "reason": self.reason or "",
            **headline,
            "evaluation": details,
        }
        evaluation_metadata = details.get("evaluation_metadata")
        if isinstance(evaluation_metadata, dict):
            payload["evaluation_metadata"] = evaluation_metadata
        return payload
