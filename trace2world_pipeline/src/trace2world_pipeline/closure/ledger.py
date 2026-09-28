
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


class RepairLedger:

    FORMAT = "trace2world.repair_ledger.v1"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._counts: dict[str, int] = {}
        self._outcomes: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("format") != self.FORMAT:
            raise ValueError(f"{self.path} is not a {self.FORMAT} ledger")
        counts = payload.get("repair_counts") or {}
        outcomes = payload.get("outcomes") or {}
        self._counts = {str(k): int(v) for k, v in counts.items()}
        self._outcomes = {str(k): str(v) for k, v in outcomes.items()}

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "format": self.FORMAT,
                    "repair_counts": dict(sorted(self._counts.items())),
                    "outcomes": dict(sorted(self._outcomes.items())),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def consumed(self, slice_id: str) -> int:
        return self._counts.get(slice_id, 0)

    def remaining(self, slice_id: str, budget: int) -> int:
        return max(0, int(budget) - self.consumed(slice_id))

    def record_repair(self, slice_id: str) -> int:
        self._counts[slice_id] = self.consumed(slice_id) + 1
        self.flush()
        return self._counts[slice_id]

    def record_outcome(self, slice_id: str, outcome: str) -> None:
        self._outcomes[slice_id] = outcome
        self.flush()

    def outcome(self, slice_id: str) -> str | None:
        return self._outcomes.get(slice_id)

    def slices(self) -> Iterator[str]:
        return iter(sorted(set(self._counts) | set(self._outcomes)))

    def as_dict(self) -> dict[str, object]:
        return {
            "format": self.FORMAT,
            "repair_counts": dict(sorted(self._counts.items())),
            "outcomes": dict(sorted(self._outcomes.items())),
        }
