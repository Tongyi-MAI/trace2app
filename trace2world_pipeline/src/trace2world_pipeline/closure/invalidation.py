
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

EVIDENCE_KINDS = ("execution", "review", "probe", "checkpoint")


@dataclass(frozen=True)
class Evidence:

    evidence_id: str
    kind: str
    trace_id: str
    consumed: frozenset[str] = field(default_factory=frozenset)
    step: int = 0

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind {self.kind!r}; expected one of {EVIDENCE_KINDS}")


@dataclass(frozen=True)
class InvalidationResult:
    retained: tuple[Evidence, ...]
    removed: tuple[Evidence, ...]
    restarted_traces: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "retained": [e.evidence_id for e in self.retained],
            "removed": [e.evidence_id for e in self.removed],
            "restarted_traces": list(self.restarted_traces),
        }


def invalidate(evidence: Iterable[Evidence], delta: Iterable[str]) -> InvalidationResult:

    edited = frozenset(delta)
    retained: list[Evidence] = []
    removed: list[Evidence] = []
    for record in evidence:
        if record.consumed & edited:
            removed.append(record)
        else:
            retained.append(record)

    restarted = {record.trace_id for record in removed}
    retained = [replace(r, step=0) if r.trace_id in restarted else r for r in retained]
    return InvalidationResult(
        retained=tuple(retained),
        removed=tuple(removed),
        restarted_traces=tuple(sorted(restarted)),
    )
