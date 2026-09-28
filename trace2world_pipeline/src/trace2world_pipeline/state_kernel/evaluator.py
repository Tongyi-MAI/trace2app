from __future__ import annotations

from typing import Any

from trace2world_pipeline.state_kernel.contracts import ValidationError


class EvaluatorStateReader:

    def __init__(self, backend: Any, evaluator_state_view: dict[str, Any]):
        self.backend = backend
        self.view = evaluator_state_view

    def read(self, handle: dict[str, Any]) -> dict[str, Any]:
        required = {"sid", "episode_epoch", "revision", "snapshot_id", "state_hash"}
        missing = sorted(required - set(handle))
        if missing:
            raise ValidationError("invalid evaluation handle", details={"missing": missing})
        snapshot = self.backend.finalize(handle["sid"], expected_revision=int(handle["revision"]))
        actual = snapshot.get("evaluation_handle", {})
        for key in required:
            if actual.get(key) != handle.get(key):
                raise ValidationError("evaluation handle does not match finalized state", details={"field": key})
        return {
            "evaluation_handle": actual,
            "state": snapshot["state"],
            "canonical_paths": self.view.get("canonical_paths", []),
            "events": self.backend.events(handle["sid"], from_revision=0),
        }
