from __future__ import annotations

import uuid
from typing import Any, Mapping

from trace2world_pipeline.state_kernel.canonical_json import canonical_clone, canonical_hash
from trace2world_pipeline.state_kernel.contracts import ConflictError, LifecycleError, NotFoundError, SessionStatus, ValidationError
from trace2world_pipeline.state_kernel.diff import typed_diff


class MemoryStateBackend:

    def __init__(self, *, artifacts: Mapping[str, Any], world: Mapping[str, list[Any]]):
        self.artifacts = canonical_clone(dict(artifacts))
        self.world = canonical_clone(dict(world))
        self.sessions: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}

    def _session(self, sid: str) -> dict[str, Any]:
        if sid not in self.sessions:
            raise NotFoundError(f"unknown sid: {sid}")
        return self.sessions[sid]

    def _version(self, sid: str) -> dict[str, Any]:
        session = self._session(sid)
        return {
            "sid": sid,
            "episode_epoch": session["episode_epoch"],
            "revision": session["revision"],
            "schema_hash": self.artifacts["storage_plan"]["schema_hash"],
            "world_hash": self.artifacts["storage_plan"]["world_hash"],
            "bundle_hash": self.artifacts["storage_plan"]["bundle_hash"],
        }

    def create_session(self, sid: str) -> dict[str, Any]:
        if not sid or sid in self.sessions:
            raise ConflictError(f"sid already exists or invalid: {sid}")
        self.sessions[sid] = {
            "episode_epoch": 1,
            "revision": 0,
            "status": SessionStatus.INITIALIZING.value,
            "collections": {},
            "runtime_fields": {},
            "events": [],
            "idempotency": {},
            "initial_state": None,
            "final_snapshot_id": None,
        }
        return {"state_version": self._version(sid), "status": SessionStatus.INITIALIZING.value}

    def inject(self, sid: str, patch: dict[str, Any]) -> dict[str, Any]:
        session = self._session(sid)
        if session["status"] != SessionStatus.INITIALIZING.value:
            raise LifecycleError("inject is allowed only while INITIALIZING")
        collections = patch.get("collections", {}) if isinstance(patch, dict) else {}
        fields = patch.get("runtime_fields", {}) if isinstance(patch, dict) else {}
        if not isinstance(collections, dict) or not isinstance(fields, dict):
            raise ValidationError("inject patch must contain object collections/runtime_fields")
        for key, value in collections.items():
            if not isinstance(value, list):
                raise ValidationError(f"collection {key} must be a list")
            session["collections"][str(key)] = canonical_clone(value)
        session["runtime_fields"].update(canonical_clone(fields))
        return {"state_version": self._version(sid), "status": session["status"]}

    def seal_initial(self, sid: str) -> dict[str, Any]:
        session = self._session(sid)
        if session["status"] != SessionStatus.INITIALIZING.value:
            raise LifecycleError("seal_initial is allowed only while INITIALIZING")
        session["status"] = SessionStatus.READY.value
        snapshot = self.snapshot(sid, kind="initial")
        session["initial_state"] = canonical_clone(snapshot["state"])
        session["initial_snapshot_id"] = snapshot["snapshot_id"]
        return snapshot

    def read_collection(self, sid: str, storage_key: str) -> list[Any]:
        session = self._session(sid)
        value = session["collections"].get(storage_key, self.world.get(storage_key, []))
        return canonical_clone(value)

    def read_storage(self, sid: str, storage_key: str, default: Any = None) -> Any:
        session = self._session(sid)
        path = f"storage::{storage_key}"
        if path in session["runtime_fields"]:
            return canonical_clone(session["runtime_fields"][path])
        if storage_key in self.world or storage_key in session["collections"]:
            return self.read_collection(sid, storage_key)
        return canonical_clone(default)

    def _validate_tx(self, transaction_id: str) -> dict[str, Any]:
        tx = self.artifacts["transaction_manifest"]["transactions_by_id"].get(transaction_id)
        if not isinstance(tx, dict):
            raise NotFoundError(f"unknown transaction: {transaction_id}")
        return tx

    def execute_transaction(self, sid: str, transaction_id: str, payload: dict[str, Any], *, expected_revision: int | None, idempotency_key: str) -> dict[str, Any]:
        session = self._session(sid)
        if session["status"] == SessionStatus.FINALIZED.value:
            raise LifecycleError("session is finalized")
        if session["status"] not in {SessionStatus.READY.value, SessionStatus.RUNNING.value}:
            raise LifecycleError("session is not ready")
        if expected_revision is not None and int(expected_revision) != session["revision"]:
            raise ConflictError("stale expected_revision")
        if not idempotency_key:
            raise ValidationError("idempotency_key is required")
        request_hash = canonical_hash({"transaction_id": transaction_id, "payload": payload})
        previous = session["idempotency"].get(idempotency_key)
        if previous:
            if previous["request_hash"] != request_hash:
                raise ConflictError("idempotency key payload mismatch")
            return canonical_clone(previous["response"])
        tx = self._validate_tx(transaction_id)
        before = self._state(sid)
        if tx.get("transaction_kind") == "collection_replace":
            records = payload.get("records")
            if not isinstance(records, list):
                raise ValidationError("collection replacement requires records list")
            session["collections"][tx["storage_key"]] = canonical_clone(records)
        elif tx.get("transaction_kind") == "compat_storage_replace":
            storage_key = str(payload.get("storage_key") or "")
            allowed_keys = set(tx.get("allowed_storage_keys", []))
            allowed_prefixes = tuple(tx.get("allowed_storage_prefixes", []))
            if storage_key not in allowed_keys and not storage_key.startswith(allowed_prefixes):
                raise ValidationError(f"storage key is not declared: {storage_key}")
            value = canonical_clone(payload.get("value"))
            if storage_key in allowed_keys and isinstance(value, list):
                session["collections"][storage_key] = value
            else:
                session["runtime_fields"][f"storage::{storage_key}"] = value
        else:
            writes = payload.get("writes", {})
            if not isinstance(writes, dict):
                raise ValidationError("writes must be an object")
            allowed = set(tx.get("state_writes", []))
            undeclared = sorted(set(writes) - allowed)
            if undeclared:
                raise ValidationError("transaction attempted undeclared writes", details={"paths": undeclared})
            session["runtime_fields"].update(canonical_clone(writes))
        session["revision"] += 1
        session["status"] = SessionStatus.RUNNING.value
        after = self._state(sid)
        changes = typed_diff(before, after)
        event = {"revision": session["revision"], "transaction_id": transaction_id, "state_diff": changes}
        session["events"].append(event)
        response = {"state_version": self._version(sid), "transaction_id": transaction_id, "state_diff": changes, "events": [event]}
        session["idempotency"][idempotency_key] = {"request_hash": request_hash, "response": canonical_clone(response)}
        return response

    def _state(self, sid: str) -> dict[str, Any]:
        session = self._session(sid)


        return {
            "world_reference": {
                "world_hash": self.artifacts["storage_plan"]["world_hash"],
                "schema_hash": self.artifacts["storage_plan"]["schema_hash"],
            },
            "collections": canonical_clone(session["collections"]),
            "runtime_fields": canonical_clone(session["runtime_fields"]),
        }

    def snapshot(self, sid: str, *, kind: str = "manual") -> dict[str, Any]:
        state = self._state(sid)
        state_hash = canonical_hash(state)
        snapshot_id = f"snap_{uuid.uuid4().hex}"
        payload = {"snapshot_id": snapshot_id, "snapshot_kind": kind, "state_version": self._version(sid), "state": state, "state_hash": state_hash}
        self.snapshots[snapshot_id] = canonical_clone(payload)
        return payload

    def reset(self, sid: str) -> dict[str, Any]:
        session = self._session(sid)
        initial = session.get("initial_state")
        if not isinstance(initial, dict):
            raise LifecycleError("session has no sealed initial snapshot")
        session["episode_epoch"] += 1
        session["revision"] = 0
        session["status"] = SessionStatus.READY.value
        session["collections"] = canonical_clone(initial.get("collections", {}))
        session["runtime_fields"] = canonical_clone(initial.get("runtime_fields", {}))
        session["events"] = []
        session["idempotency"] = {}
        session["final_snapshot_id"] = None
        snap = self.snapshot(sid, kind="initial")
        session["initial_snapshot_id"] = snap["snapshot_id"]
        return snap

    def clone(self, sid: str, new_sid: str, *, snapshot_id: str | None = None) -> dict[str, Any]:
        source = self.snapshots.get(snapshot_id) if snapshot_id else self.snapshot(sid, kind="clone_source")
        if not source:
            raise NotFoundError(f"unknown snapshot: {snapshot_id}")
        self.create_session(new_sid)
        state = source["state"]
        self.inject(new_sid, {"collections": state.get("collections", {}), "runtime_fields": state.get("runtime_fields", {})})
        return self.seal_initial(new_sid)

    def diff(self, sid: str, from_snapshot_id: str, to_snapshot_id: str) -> dict[str, Any]:
        self._session(sid)
        before = self.snapshots.get(from_snapshot_id)
        after = self.snapshots.get(to_snapshot_id)
        if not before or not after:
            raise NotFoundError("snapshot not found")
        return {"from_snapshot_id": from_snapshot_id, "to_snapshot_id": to_snapshot_id, "changes": typed_diff(before["state"], after["state"])}

    def finalize(self, sid: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        session = self._session(sid)
        if expected_revision is not None and int(expected_revision) != session["revision"]:
            raise ConflictError("stale expected_revision")
        if session["status"] == SessionStatus.FINALIZED.value:
            snapshot = canonical_clone(self.snapshots[session["final_snapshot_id"]])
            snapshot["evaluation_handle"] = {
                "sid": sid,
                "episode_epoch": session["episode_epoch"],
                "revision": session["revision"],
                "snapshot_id": snapshot["snapshot_id"],
                "state_hash": snapshot["state_hash"],
            }
            return snapshot
        snapshot = self.snapshot(sid, kind="final")
        session["final_snapshot_id"] = snapshot["snapshot_id"]
        session["status"] = SessionStatus.FINALIZED.value
        snapshot["evaluation_handle"] = {
            "sid": sid,
            "episode_epoch": session["episode_epoch"],
            "revision": session["revision"],
            "snapshot_id": snapshot["snapshot_id"],
            "state_hash": snapshot["state_hash"],
        }
        self.snapshots[snapshot["snapshot_id"]] = canonical_clone(snapshot)
        return snapshot

    def delete(self, sid: str) -> dict[str, Any]:
        self._session(sid)
        del self.sessions[sid]
        for snapshot_id in [
            key for key, value in self.snapshots.items() if value["state_version"]["sid"] == sid
        ]:
            del self.snapshots[snapshot_id]
        return {"sid": sid, "deleted": True}

    def events(self, sid: str, *, from_revision: int = 0) -> list[dict[str, Any]]:
        return canonical_clone([item for item in self._session(sid)["events"] if item["revision"] >= from_revision])
