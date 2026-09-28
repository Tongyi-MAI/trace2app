
from __future__ import annotations

from enum import Enum


class Owner(str, Enum):

    SEMANTICS = "semantics"
    IMPLEMENTATION = "implementation"
    SUPERVISION = "supervision"
    MODEL = "model"
    PROVIDER = "provider"


REPAIRABLE_OWNERS = frozenset({Owner.IMPLEMENTATION, Owner.SUPERVISION})


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class Failure:

    __slots__ = ("code", "owner", "stage", "slice_id", "dependencies", "detail")

    def __init__(
        self,
        *,
        code: str,
        owner: Owner,
        stage: str,
        slice_id: str,
        dependencies: frozenset[str] = frozenset(),
        detail: str = "",
    ) -> None:
        if not code:
            raise ValueError("a failure must carry a code")
        if not slice_id:
            raise ValueError("a failure must name the slice it belongs to")
        self.code = code
        self.owner = Owner(owner)
        self.stage = stage
        self.slice_id = slice_id
        self.dependencies = frozenset(dependencies)
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "owner": self.owner.value,
            "stage": self.stage,
            "slice_id": self.slice_id,
            "dependencies": sorted(self.dependencies),
            "detail": self.detail,
        }

    def __repr__(self) -> str:
        return f"Failure({self.code!r}, owner={self.owner.value!r}, slice={self.slice_id!r})"


def owner_of(failure: Failure) -> Owner:
    return failure.owner


def is_repairable(failure: Failure) -> bool:
    return failure.owner in REPAIRABLE_OWNERS
