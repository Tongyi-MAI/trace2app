
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


RESUME_SOURCES = {"memory", "reused_disk", "recomputed", "partial"}
CHANGE_TYPES = {"byte", "semantic", "prompt_boundary"}


def _normalize_tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, str):
        value = values.strip()
        return (value,) if value else ()
    return tuple(str(value) for value in values if str(value or "").strip())


@dataclass(frozen=True)
class LogicalArtifactChange:

    path: str
    logical_artifact: str = ""
    change_type: str = "byte"
    artifact_role: str = ""
    canonical_semantic_source: bool = False
    canonical_prompt_boundary: bool = False
    invalidates: tuple[str, ...] = field(default_factory=tuple)
    consumers: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    def __post_init__(self) -> None:
        normalized_type = str(self.change_type or "byte").strip()
        if normalized_type not in CHANGE_TYPES:
            raise ValueError(f"invalid change_type for {self.path}: {self.change_type}")
        object.__setattr__(self, "change_type", normalized_type)
        object.__setattr__(self, "invalidates", _normalize_tuple(self.invalidates))
        object.__setattr__(self, "consumers", _normalize_tuple(self.consumers))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LogicalArtifactChange":
        return cls(
            path=str(payload.get("path") or payload.get("artifact_id") or ""),
            logical_artifact=str(payload.get("logical_artifact") or payload.get("logical_owner") or ""),
            change_type=str(payload.get("change_type") or "byte"),
            artifact_role=str(payload.get("artifact_role") or payload.get("artifact_kind") or ""),
            canonical_semantic_source=bool(payload.get("canonical_semantic_source", False)),
            canonical_prompt_boundary=bool(payload.get("canonical_prompt_boundary", False)),
            invalidates=payload.get("invalidates") or (),
            consumers=payload.get("consumers") or payload.get("allowed_consumers") or (),
            reason=str(payload.get("reason") or payload.get("notes") or ""),
        )

    def invalidates_logical_artifact(self, logical_artifact: str) -> bool:
        target = str(logical_artifact or "").strip()
        if not target:
            return False
        if "*" in self.invalidates or target in self.invalidates:
            return True
        if str(self.logical_artifact or "").strip() != target:
            return False
        if self.change_type == "semantic":
            return self.canonical_semantic_source
        if self.change_type == "prompt_boundary":
            return self.canonical_prompt_boundary
        return False


@dataclass(frozen=True)
class LogicalArtifactDecision:
    logical_artifact: str
    source: str
    canonical_semantic_source: bool
    canonical_prompt_boundary: bool
    phase: str = ""
    artifact_path: str = ""
    invalidated_by: tuple[str, ...] = field(default_factory=tuple)
    consumers: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_source = str(self.source or "").strip()
        if normalized_source not in RESUME_SOURCES:
            raise ValueError(f"invalid resume source for {self.logical_artifact}: {self.source}")
        object.__setattr__(self, "source", normalized_source)
        object.__setattr__(self, "invalidated_by", _normalize_tuple(self.invalidated_by))
        object.__setattr__(self, "consumers", _normalize_tuple(self.consumers))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "logical_artifact": self.logical_artifact,
            "source": self.source,
            "canonical_semantic_source": self.canonical_semantic_source,
            "canonical_prompt_boundary": self.canonical_prompt_boundary,
            "invalidated_by": list(self.invalidated_by),
            "consumers": list(self.consumers),
            "reason": self.reason,
        }
        if self.phase:
            payload["phase"] = self.phase
        if self.artifact_path:
            payload["artifact_path"] = self.artifact_path
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class RunContext:
    decisions: tuple[LogicalArtifactDecision, ...]

    @classmethod
    def from_decisions(cls, decisions: Iterable[LogicalArtifactDecision]) -> "RunContext":
        return cls(tuple(decisions))

    def to_resume_plan(self) -> dict[str, Any]:
        return {
            "format": "ghostweb_resume_plan",
            "schema_revision": 1,
            "execution_effect": "metadata_only",
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


def resolve_logical_artifact_resume(
    *,
    logical_artifact: str,
    cached_on_disk: bool,
    canonical_semantic_source: bool,
    canonical_prompt_boundary: bool,
    consumers: Iterable[str] = (),
    changes: Iterable[LogicalArtifactChange | Mapping[str, Any]] = (),
    memory_available: bool = False,
) -> LogicalArtifactDecision:
    normalized_changes = tuple(
        change if isinstance(change, LogicalArtifactChange) else LogicalArtifactChange.from_mapping(change)
        for change in changes
    )
    invalidated_by = tuple(
        change.path
        for change in normalized_changes
        if change.invalidates_logical_artifact(logical_artifact)
    )
    if invalidated_by:
        return LogicalArtifactDecision(
            logical_artifact=logical_artifact,
            source="recomputed",
            canonical_semantic_source=canonical_semantic_source,
            canonical_prompt_boundary=canonical_prompt_boundary,
            invalidated_by=invalidated_by,
            consumers=tuple(consumers),
            reason="matched_invalidation",
        )
    if memory_available:
        return LogicalArtifactDecision(
            logical_artifact=logical_artifact,
            source="memory",
            canonical_semantic_source=canonical_semantic_source,
            canonical_prompt_boundary=canonical_prompt_boundary,
            consumers=tuple(consumers),
            reason="memory_context_available",
        )
    if cached_on_disk:
        return LogicalArtifactDecision(
            logical_artifact=logical_artifact,
            source="reused_disk",
            canonical_semantic_source=canonical_semantic_source,
            canonical_prompt_boundary=canonical_prompt_boundary,
            consumers=tuple(consumers),
            reason="cached_artifact_present_manual_invalidation",
        )
    return LogicalArtifactDecision(
        logical_artifact=logical_artifact,
        source="recomputed",
        canonical_semantic_source=canonical_semantic_source,
        canonical_prompt_boundary=canonical_prompt_boundary,
        consumers=tuple(consumers),
        reason="missing_cached_artifact",
    )
