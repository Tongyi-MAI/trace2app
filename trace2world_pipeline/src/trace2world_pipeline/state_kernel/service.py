from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from trace2world_pipeline.state_kernel.backends.sqlite import SQLiteStateBackend
from trace2world_pipeline.state_kernel.contracts import ConflictError


class EnvironmentStateService:
    def __init__(self, runtime_root: str | Path, *, state_db_path: str | Path | None = None):
        self.runtime_root = Path(runtime_root)
        artifacts_dir = self.runtime_root / "contracts"
        names = (
            "storage_plan",
            "runtime_state_registry",
            "transaction_manifest",
            "seed_manifest",
            "evaluator_state_view",
            "state_api_contract",
        )
        self.artifacts = {
            name: json.loads((artifacts_dir / f"{name}.json").read_text(encoding="utf-8"))
            for name in names
        }
        state_path = Path(state_db_path) if state_db_path else self.runtime_root / "state.sqlite"
        self.backend = SQLiteStateBackend(
            artifacts=self.artifacts,
            world_db_path=self.runtime_root / "world.sqlite",
            state_db_path=state_path,
            state_template_path=self.runtime_root / "state.sqlite.template",
        )
        max_writers = int(self.artifacts["storage_plan"].get("sqlite", {}).get("max_concurrent_writers", 32) or 32)
        self._write_slots = threading.BoundedSemaphore(max(1, max_writers))

    def bootstrap(self, sid: str | None = None) -> dict[str, Any]:
        sid = sid or f"sid_{uuid.uuid4().hex}"
        try:
            with self._write_slots:
                created = self.backend.create_session(sid)
                initial = self.backend.seal_initial(sid)
        except ConflictError:


            if sid:
                snapshot = self.backend.snapshot(sid, kind="bootstrap_read")
                return {
                    "sid": sid,
                    "state_version": snapshot["state_version"],
                    "initial_snapshot_id": snapshot["snapshot_id"],
                    "state": {
                        "runtime_fields": snapshot["state"].get("runtime_fields", {}),
                    },
                }
            raise
        return {
            "sid": sid,
            "state_version": created["state_version"],
            "initial_snapshot_id": initial["snapshot_id"],
            "state": {
                "runtime_fields": initial["state"].get("runtime_fields", {}),
            },
        }

    def create(self, sid: str | None = None) -> dict[str, Any]:
        sid = sid or f"sid_{uuid.uuid4().hex}"
        with self._write_slots:
            result = self.backend.create_session(sid)
        result["sid"] = sid
        return result

    def read_collection(self, sid: str, storage_key: str) -> dict[str, Any]:
        records = self.backend.read_collection(sid, storage_key)
        snapshot = self.backend.snapshot(sid, kind="read_view")
        return {"sid": sid, "storage_key": storage_key, "records": records, "state_version": snapshot["state_version"]}

    def read_storage(self, sid: str, storage_key: str, default: Any = None) -> dict[str, Any]:
        value = self.backend.read_storage(sid, storage_key, default)
        snapshot = self.backend.snapshot(sid, kind="read_view")
        return {"sid": sid, "storage_key": storage_key, "value": value, "state_version": snapshot["state_version"]}

    def execute(self, sid: str, transaction_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._write_slots:
            return self.backend.execute_transaction(
                sid,
                transaction_id,
                payload.get("payload", payload),
                expected_revision=payload.get("expected_revision"),
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )

    def record_action_receipt(self, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._write_slots:
            return self.backend.record_action_receipt(
                sid,
                str(payload.get("action_id") or ""),
                receipt_event=str(payload.get("receipt_event") or "action_effect_observed"),
                payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )

    def dispatch(self, operation: str, *, sid: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if operation == "bootstrap":
            return self.bootstrap(sid or payload.get("sid"))
        if operation == "create":
            return self.create(sid or payload.get("sid"))
        if operation == "inject":
            with self._write_slots:
                return self.backend.inject(sid, payload)
        if operation == "seal_initial":
            with self._write_slots:
                return self.backend.seal_initial(sid)
        if operation == "reset":
            with self._write_slots:
                result = self.backend.reset(sid)
            result["state"] = {
                "runtime_fields": (result.get("state") or {}).get("runtime_fields", {}),
            }
            return result
        if operation == "clone":
            with self._write_slots:
                return self.backend.clone(sid, str(payload.get("new_sid") or f"sid_{uuid.uuid4().hex}"), snapshot_id=payload.get("snapshot_id"))
        if operation == "snapshot":
            return self.backend.snapshot(sid, kind=str(payload.get("kind") or "manual"))
        if operation == "diff":
            return self.backend.diff(sid, str(payload.get("from_snapshot_id") or ""), str(payload.get("to_snapshot_id") or ""))
        if operation == "finalize":
            with self._write_slots:
                return self.backend.finalize(sid, expected_revision=payload.get("expected_revision"))
        if operation == "delete":
            with self._write_slots:
                return self.backend.delete(sid)
        if operation == "events":
            return {"sid": sid, "events": self.backend.events(sid, from_revision=int(payload.get("from_revision", 0)))}
        if operation == "record_action_receipt":
            return self.record_action_receipt(sid, payload)
        raise KeyError(f"unknown operation: {operation}")
