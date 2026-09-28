from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SessionStatus(str, Enum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    RUNNING = "RUNNING"
    FINALIZED = "FINALIZED"


class EnvironmentStateError(RuntimeError):
    code = "environment_state_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class NotFoundError(EnvironmentStateError):
    code = "not_found"
    http_status = 404


class ConflictError(EnvironmentStateError):
    code = "conflict"
    http_status = 409


class LifecycleError(EnvironmentStateError):
    code = "lifecycle_error"
    http_status = 410


class ValidationError(EnvironmentStateError):
    code = "validation_error"
    http_status = 422


class AuthorizationError(EnvironmentStateError):
    code = "forbidden"
    http_status = 403


@dataclass(frozen=True)
class StateVersion:
    sid: str
    episode_epoch: int
    revision: int
    schema_hash: str
    world_hash: str
    bundle_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "episode_epoch": self.episode_epoch,
            "revision": self.revision,
            "schema_hash": self.schema_hash,
            "world_hash": self.world_hash,
            "bundle_hash": self.bundle_hash,
        }


@dataclass(frozen=True)
class EvaluationHandle:
    sid: str
    episode_epoch: int
    revision: int
    snapshot_id: str
    state_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "episode_epoch": self.episode_epoch,
            "revision": self.revision,
            "snapshot_id": self.snapshot_id,
            "state_hash": self.state_hash,
        }
