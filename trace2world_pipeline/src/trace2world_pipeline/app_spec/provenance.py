
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable, Mapping, Optional, Tuple


PROVENANCE_KEY: str = "provenance"
"""Standard dict key used to attach a Provenance payload to a contract entry."""

REQUIRED_FIELDS: Tuple[str, ...] = ("mid", "source_step")
"""Fields whose absence raises MissingProvenance."""

OPTIONAL_FIELDS: Tuple[str, ...] = ("slot",)


class MissingProvenance(ValueError):

    def __init__(self, consumer: str, missing: Iterable[str]) -> None:
        self.consumer = str(consumer or "<unknown>")
        self.missing = tuple(str(field) for field in missing if field)
        message = (
            f"{self.consumer}: missing required provenance field(s) "
            f"{', '.join(self.missing) or '<unspecified>'}"
        )
        super().__init__(message)


@dataclass(frozen=True)
class Provenance:

    mid: str
    source_step: str
    slot: Optional[str] = None

    def __post_init__(self) -> None:


        missing = []
        if not isinstance(self.mid, str) or not self.mid.strip():
            missing.append("mid")
        if not isinstance(self.source_step, str) or not self.source_step.strip():
            missing.append("source_step")
        if missing:
            raise MissingProvenance("Provenance", missing)
        for field_name in OPTIONAL_FIELDS:
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise MissingProvenance(
                    "Provenance",
                    [f"{field_name} (must be non-empty str or None)"],
                )

    def to_dict(self) -> dict:

        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        consumer: str = "Provenance.from_mapping",
    ) -> "Provenance":

        if not isinstance(data, Mapping):
            raise MissingProvenance(consumer, REQUIRED_FIELDS)
        kwargs = {field: data.get(field) for field in REQUIRED_FIELDS + OPTIONAL_FIELDS}
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise MissingProvenance(consumer, REQUIRED_FIELDS) from exc

    def with_slot(self, slot: Optional[str]) -> "Provenance":

        return Provenance(
            mid=self.mid,
            source_step=self.source_step,
            slot=slot if slot is not None else self.slot,
        )


def provenance_from_mapping(
    data: Mapping[str, Any],
    *,
    consumer: str = "provenance_from_mapping",
) -> Provenance:

    return Provenance.from_mapping(data, consumer=consumer)


def attach_provenance(
    payload: dict,
    provenance: Provenance,
    *,
    key: str = PROVENANCE_KEY,
) -> dict:

    if not isinstance(payload, dict):
        raise TypeError(
            "attach_provenance: payload must be a dict, got "
            f"{type(payload).__name__}"
        )
    payload[key] = provenance.to_dict()
    return payload


def require_provenance(
    payload: Mapping[str, Any],
    *,
    consumer: str,
    key: str = PROVENANCE_KEY,
) -> Provenance:

    if not isinstance(payload, Mapping):
        raise MissingProvenance(consumer, REQUIRED_FIELDS)
    raw = payload.get(key)
    if not isinstance(raw, Mapping):
        raise MissingProvenance(consumer, REQUIRED_FIELDS)
    return Provenance.from_mapping(raw, consumer=consumer)
