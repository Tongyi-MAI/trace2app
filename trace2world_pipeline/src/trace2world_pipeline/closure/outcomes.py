
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .owners import Failure


@dataclass(frozen=True)
class ClosureOutcome:
    slice_id: str
    waves: tuple[dict[str, Any], ...]
    repairs_consumed: int

    @property
    def status(self) -> str:
        raise NotImplementedError

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "slice_id": self.slice_id,
            "waves": [dict(w) for w in self.waves],
            "repairs_consumed": self.repairs_consumed,
        }


@dataclass(frozen=True)
class Verified(ClosureOutcome):
    environment: Any
    evidence: Any

    @property
    def status(self) -> str:
        return "verified"

    def as_dict(self) -> dict[str, Any]:
        payload = super().as_dict()
        payload["closed_at_wave"] = self.waves[-1]["wave"] if self.waves else None
        return payload


@dataclass(frozen=True)
class UpstreamReferral(ClosureOutcome):
    failure: Failure

    @property
    def status(self) -> str:
        return "upstream"

    def as_dict(self) -> dict[str, Any]:
        payload = super().as_dict()
        payload["failure"] = self.failure.as_dict()
        return payload


@dataclass(frozen=True)
class DroppedSlice(ClosureOutcome):
    environment: Any
    failure: Failure
    at_cap: bool

    @property
    def status(self) -> str:
        return "dropped"

    def as_dict(self) -> dict[str, Any]:
        payload = super().as_dict()
        payload["failure"] = self.failure.as_dict()
        payload["owner"] = self.failure.owner.value


        payload["exit"] = "cap" if self.at_cap else "not_repairable"
        return payload
