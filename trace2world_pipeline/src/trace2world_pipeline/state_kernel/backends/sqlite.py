from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping

from trace2world_pipeline.state_kernel.canonical_json import canonical_clone, canonical_hash, canonical_json
from trace2world_pipeline.state_kernel.contracts import ConflictError, LifecycleError, NotFoundError, SessionStatus, ValidationError
from trace2world_pipeline.state_kernel.diff import typed_diff


WORLD_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS world_meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS world_entity_types (
  entity_type TEXT PRIMARY KEY,
  storage_key TEXT NOT NULL UNIQUE,
  schema_json TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  id_field TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS world_entities (
  storage_key TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(storage_key, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_world_entities_order ON world_entities(storage_key, ordinal, entity_id);
"""


STATE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY,
  episode_epoch INTEGER NOT NULL,
  status TEXT NOT NULL,
  revision INTEGER NOT NULL,
  schema_hash TEXT NOT NULL,
  world_hash TEXT NOT NULL,
  bundle_hash TEXT NOT NULL,
  initial_snapshot_id TEXT,
  final_snapshot_id TEXT,
  clone_source_json TEXT
);
CREATE TABLE IF NOT EXISTS session_entity_overlays (
  sid TEXT NOT NULL,
  episode_epoch INTEGER NOT NULL,
  storage_key TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  overlay_kind TEXT NOT NULL,
  payload_json TEXT,
  row_revision INTEGER NOT NULL,
  PRIMARY KEY(sid, episode_epoch, storage_key, entity_id),
  FOREIGN KEY(sid) REFERENCES sessions(sid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_overlay_collection ON session_entity_overlays(sid, episode_epoch, storage_key, ordinal);
CREATE TABLE IF NOT EXISTS session_runtime_fields (
  sid TEXT NOT NULL,
  episode_epoch INTEGER NOT NULL,
  namespace TEXT NOT NULL,
  canonical_path TEXT NOT NULL,
  value_json TEXT NOT NULL,
  row_revision INTEGER NOT NULL,
  PRIMARY KEY(sid, episode_epoch, namespace, canonical_path),
  FOREIGN KEY(sid) REFERENCES sessions(sid) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS session_transactions (
  sid TEXT NOT NULL,
  episode_epoch INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  transaction_instance_id TEXT NOT NULL,
  transaction_contract_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  committed_at TEXT NOT NULL,
  PRIMARY KEY(sid, episode_epoch, revision),
  UNIQUE(sid, episode_epoch, idempotency_key),
  FOREIGN KEY(sid) REFERENCES sessions(sid) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS session_events (
  sid TEXT NOT NULL,
  episode_epoch INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  event_index INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  event_json TEXT NOT NULL,
  PRIMARY KEY(sid, episode_epoch, revision, event_index),
  FOREIGN KEY(sid) REFERENCES sessions(sid) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS session_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  sid TEXT NOT NULL,
  episode_epoch INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  snapshot_kind TEXT NOT NULL,
  state_json TEXT NOT NULL,
  state_hash TEXT NOT NULL,
  FOREIGN KEY(sid) REFERENCES sessions(sid) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS artifact_provenance (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, migration_hash TEXT NOT NULL);
"""


def _record_id(record: Any, ordinal: int, storage_key: str) -> str:
    if isinstance(record, dict) and record.get("id") not in (None, ""):
        return str(record["id"])
    return f"{storage_key}_{ordinal}"


def materialize_sqlite_databases(
    *,
    world_db_path: str | Path,
    state_template_path: str | Path,
    artifacts: Mapping[str, Any],
    world: Mapping[str, list[Any]],
) -> dict[str, Any]:
    world_path = Path(world_db_path)
    state_path = Path(state_template_path)
    world_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if world_path.exists():
        world_path.unlink()
    if state_path.exists():
        state_path.unlink()
    storage_plan = artifacts["storage_plan"]
    with sqlite3.connect(world_path) as conn:
        conn.executescript(WORLD_SCHEMA)
        meta = {
            "schema_hash": storage_plan["schema_hash"],
            "world_hash": storage_plan["world_hash"],
            "bundle_hash": storage_plan["bundle_hash"],
            "backend": "sqlite",
        }
        conn.executemany(
            "INSERT INTO world_meta(key,value_json) VALUES (?,?)",
            [(key, canonical_json(value)) for key, value in sorted(meta.items())],
        )
        entity_by_key = {item["storage_key"]: item for item in storage_plan.get("entity_types", [])}
        for storage_key, records in sorted(world.items()):
            entity = entity_by_key.get(storage_key, {"entity_type": storage_key, "id_field": "id", "fields": []})
            schema_payload = {"fields": entity.get("fields", [])}
            conn.execute(
                "INSERT INTO world_entity_types(entity_type,storage_key,schema_json,schema_hash,id_field) VALUES (?,?,?,?,?)",
                (entity["entity_type"], storage_key, canonical_json(schema_payload), canonical_hash(schema_payload), entity.get("id_field", "id")),
            )
            for ordinal, raw in enumerate(records):
                record = canonical_clone(raw)
                entity_id = _record_id(record, ordinal, storage_key)
                if isinstance(record, dict) and "id" not in record:
                    record["id"] = entity_id
                conn.execute(
                    "INSERT INTO world_entities(storage_key,entity_id,ordinal,payload_json,content_hash) VALUES (?,?,?,?,?)",
                    (storage_key, entity_id, ordinal, canonical_json(record), canonical_hash(record)),
                )
        conn.commit()
    with sqlite3.connect(state_path) as conn:
        conn.executescript(STATE_SCHEMA)
        for key in ("schema_hash", "world_hash", "bundle_hash"):
            conn.execute(
                "INSERT INTO artifact_provenance(key,value_json) VALUES (?,?)",
                (key, canonical_json(storage_plan[key])),
            )
        conn.execute("INSERT INTO schema_migrations(version,migration_hash) VALUES (?,?)", (1, canonical_hash(STATE_SCHEMA)))
        conn.commit()
    return {
        "format": "ghostweb_sqlite_materialization_report",
        "schema_revision": 1,
        "world_db": str(world_path),
        "state_template": str(state_path),
        "world_record_count": sum(len(value) for value in world.values()),
        "collection_count": len(world),
        "schema_hash": storage_plan["schema_hash"],
        "world_hash": storage_plan["world_hash"],
        "bundle_hash": storage_plan["bundle_hash"],
        "world_db_hash": canonical_hash(world_path.read_bytes().hex()),
        "state_template_hash": canonical_hash(state_path.read_bytes().hex()),
    }


class SQLiteStateBackend:
    def __init__(
        self,
        *,
        artifacts: Mapping[str, Any],
        world_db_path: str | Path,
        state_db_path: str | Path,
        state_template_path: str | Path | None = None,
    ):
        self.artifacts = dict(artifacts)
        self.world_db_path = Path(world_db_path)
        self.state_db_path = Path(state_db_path)
        if not self.state_db_path.exists() and state_template_path:
            self.state_db_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state_template_path, self.state_db_path)
        if not self.world_db_path.is_file() or not self.state_db_path.is_file():
            raise FileNotFoundError("world.sqlite and writable state.sqlite are required")
        with self._connect() as conn:
            conn.executescript(STATE_SCHEMA)
            conn.commit()
        self._validate_provenance()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.state_db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _world_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.world_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _hashes(self) -> tuple[str, str, str]:
        plan = self.artifacts["storage_plan"]
        return plan["schema_hash"], plan["world_hash"], plan["bundle_hash"]

    def _validate_provenance(self) -> None:
        expected = dict(zip(("schema_hash", "world_hash", "bundle_hash"), self._hashes()))
        with self._world_connect() as conn:
            actual = {row["key"]: json.loads(row["value_json"]) for row in conn.execute("SELECT key,value_json FROM world_meta")}
        for key, value in expected.items():
            if actual.get(key) != value:
                raise ConflictError(f"world database {key} mismatch")

    def _session_row(self, conn: sqlite3.Connection, sid: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
        if row is None:
            raise NotFoundError(f"unknown sid: {sid}")
        return row

    def _version(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "sid": row["sid"],
            "episode_epoch": int(row["episode_epoch"]),
            "revision": int(row["revision"]),
            "schema_hash": row["schema_hash"],
            "world_hash": row["world_hash"],
            "bundle_hash": row["bundle_hash"],
        }

    def create_session(self, sid: str) -> dict[str, Any]:
        if not sid:
            raise ValidationError("sid is required")
        schema_hash, world_hash, bundle_hash = self._hashes()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO sessions(sid,episode_epoch,status,revision,schema_hash,world_hash,bundle_hash) VALUES (?,?,?,?,?,?,?)",
                    (sid, 1, SessionStatus.INITIALIZING.value, 0, schema_hash, world_hash, bundle_hash),
                )
                row = self._session_row(conn, sid)
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"sid already exists: {sid}") from exc
        return {"state_version": self._version(row), "status": row["status"]}

    def _world_collection(self, storage_key: str) -> list[Any]:
        with self._world_connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM world_entities WHERE storage_key=? ORDER BY ordinal,entity_id",
                (storage_key,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _collection_in_conn(self, conn: sqlite3.Connection, sid: str, epoch: int, storage_key: str) -> list[Any]:
        world = self._world_collection(storage_key)
        by_id = {_record_id(item, index, storage_key): item for index, item in enumerate(world)}
        order = {_record_id(item, index, storage_key): index for index, item in enumerate(world)}
        rows = conn.execute(
            "SELECT entity_id,ordinal,overlay_kind,payload_json FROM session_entity_overlays WHERE sid=? AND episode_epoch=? AND storage_key=?",
            (sid, epoch, storage_key),
        ).fetchall()
        for row in rows:
            entity_id = row["entity_id"]
            if row["overlay_kind"] == "tombstone":
                by_id.pop(entity_id, None)
                order.pop(entity_id, None)
            else:
                by_id[entity_id] = json.loads(row["payload_json"])
                order[entity_id] = int(row["ordinal"])
        return [by_id[key] for key in sorted(by_id, key=lambda item: (order.get(item, 10**9), item))]

    def read_collection(self, sid: str, storage_key: str) -> list[Any]:
        with self._connect() as conn:
            row = self._session_row(conn, sid)
            return canonical_clone(self._collection_in_conn(conn, sid, int(row["episode_epoch"]), storage_key))

    def read_storage(self, sid: str, storage_key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = self._session_row(conn, sid)
            stored = conn.execute(
                "SELECT value_json FROM session_runtime_fields WHERE sid=? AND episode_epoch=? AND namespace='compat' AND canonical_path=?",
                (sid, int(row["episode_epoch"]), f"storage::{storage_key}"),
            ).fetchone()
            if stored:
                return json.loads(stored["value_json"])
            if storage_key in self._collection_keys():
                return canonical_clone(self._collection_in_conn(conn, sid, int(row["episode_epoch"]), storage_key))
        return canonical_clone(default)

    def _replace_collection(self, conn: sqlite3.Connection, row: Mapping[str, Any], storage_key: str, records: list[Any], revision: int) -> None:
        sid, epoch = row["sid"], int(row["episode_epoch"])
        world = self._world_collection(storage_key)
        world_by_id = {_record_id(item, index, storage_key): item for index, item in enumerate(world)}
        desired: dict[str, tuple[int, Any]] = {}
        for ordinal, raw in enumerate(records):
            record = canonical_clone(raw)
            entity_id = _record_id(record, ordinal, storage_key)
            if isinstance(record, dict) and "id" not in record:
                record["id"] = entity_id
            if entity_id in desired:
                raise ValidationError(f"duplicate entity id in {storage_key}: {entity_id}")
            desired[entity_id] = (ordinal, record)
        conn.execute(
            "DELETE FROM session_entity_overlays WHERE sid=? AND episode_epoch=? AND storage_key=?",
            (sid, epoch, storage_key),
        )
        for entity_id, world_record in world_by_id.items():
            if entity_id not in desired:
                conn.execute(
                    "INSERT INTO session_entity_overlays VALUES (?,?,?,?,?,?,?,?)",
                    (sid, epoch, storage_key, entity_id, 0, "tombstone", None, revision),
                )
        for entity_id, (ordinal, record) in desired.items():
            if world_by_id.get(entity_id) == record and ordinal < len(world):
                continue
            conn.execute(
                "INSERT INTO session_entity_overlays VALUES (?,?,?,?,?,?,?,?)",
                (sid, epoch, storage_key, entity_id, ordinal, "replace", canonical_json(record), revision),
            )

    def _runtime_fields(self, conn: sqlite3.Connection, sid: str, epoch: int) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT canonical_path,value_json FROM session_runtime_fields WHERE sid=? AND episode_epoch=? ORDER BY canonical_path",
            (sid, epoch),
        ).fetchall()
        return {row["canonical_path"]: json.loads(row["value_json"]) for row in rows}

    def _collection_keys(self) -> list[str]:
        return sorted(self.artifacts["seed_manifest"].get("collections", {}))

    def _state_in_conn(self, conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
        sid, epoch = row["sid"], int(row["episode_epoch"])
        overridden_keys = [
            str(item[0])
            for item in conn.execute(
                "SELECT DISTINCT storage_key FROM session_entity_overlays "
                "WHERE sid=? AND episode_epoch=? ORDER BY storage_key",
                (sid, epoch),
            ).fetchall()
        ]
        return {
            "world_reference": {
                "world_hash": self.artifacts["storage_plan"]["world_hash"],
                "schema_hash": self.artifacts["storage_plan"]["schema_hash"],
            },
            "collections": {
                key: self._collection_in_conn(conn, sid, epoch, key)
                for key in overridden_keys
            },
            "runtime_fields": self._runtime_fields(conn, sid, epoch),
        }

    def inject(self, sid: str, patch: dict[str, Any]) -> dict[str, Any]:
        collections = patch.get("collections", {}) if isinstance(patch, dict) else {}
        fields = patch.get("runtime_fields", {}) if isinstance(patch, dict) else {}
        if not isinstance(collections, dict) or not isinstance(fields, dict):
            raise ValidationError("inject patch must contain object collections/runtime_fields")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._session_row(conn, sid)
            if row["status"] != SessionStatus.INITIALIZING.value:
                raise LifecycleError("inject is allowed only while INITIALIZING")
            for key, records in collections.items():
                if not isinstance(records, list):
                    raise ValidationError(f"collection {key} must be a list")
                self._replace_collection(conn, row, str(key), records, 0)
            for path, value in fields.items():
                conn.execute(
                    "INSERT OR REPLACE INTO session_runtime_fields VALUES (?,?,?,?,?,?)",
                    (sid, int(row["episode_epoch"]), "business", str(path), canonical_json(value), 0),
                )
            conn.commit()
        return {"state_version": self._version(row), "status": row["status"]}

    def _snapshot_in_conn(self, conn: sqlite3.Connection, row: Mapping[str, Any], kind: str) -> dict[str, Any]:
        state = self._state_in_conn(conn, row)
        snapshot_id = f"snap_{uuid.uuid4().hex}"
        state_hash = canonical_hash(state)
        conn.execute(
            "INSERT INTO session_snapshots VALUES (?,?,?,?,?,?,?)",
            (snapshot_id, row["sid"], int(row["episode_epoch"]), int(row["revision"]), kind, canonical_json(state), state_hash),
        )
        return {"snapshot_id": snapshot_id, "snapshot_kind": kind, "state_version": self._version(row), "state": state, "state_hash": state_hash}

    def seal_initial(self, sid: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._session_row(conn, sid)
            if row["status"] != SessionStatus.INITIALIZING.value:
                raise LifecycleError("seal_initial is allowed only while INITIALIZING")
            conn.execute("UPDATE sessions SET status=? WHERE sid=?", (SessionStatus.READY.value, sid))
            row = self._session_row(conn, sid)
            snapshot = self._snapshot_in_conn(conn, row, "initial")
            conn.execute("UPDATE sessions SET initial_snapshot_id=? WHERE sid=?", (snapshot["snapshot_id"], sid))
            conn.commit()
        return snapshot

    def _transaction_contract(self, transaction_id: str) -> dict[str, Any]:
        tx = self.artifacts["transaction_manifest"]["transactions_by_id"].get(transaction_id)
        if not isinstance(tx, dict):
            raise NotFoundError(f"unknown transaction: {transaction_id}")
        return tx

    def execute_transaction(self, sid: str, transaction_id: str, payload: dict[str, Any], *, expected_revision: int | None, idempotency_key: str) -> dict[str, Any]:
        if not idempotency_key:
            raise ValidationError("idempotency_key is required")
        tx = self._transaction_contract(transaction_id)
        request_hash = canonical_hash({"transaction_id": transaction_id, "payload": payload})
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._session_row(conn, sid)
            if row["status"] == SessionStatus.FINALIZED.value:
                raise LifecycleError("session is finalized")
            if row["status"] not in {SessionStatus.READY.value, SessionStatus.RUNNING.value}:
                raise LifecycleError("session is not ready")
            if expected_revision is not None and int(expected_revision) != int(row["revision"]):
                raise ConflictError("stale expected_revision")
            previous = conn.execute(
                "SELECT request_hash,response_json FROM session_transactions WHERE sid=? AND episode_epoch=? AND idempotency_key=?",
                (sid, int(row["episode_epoch"]), idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ConflictError("idempotency key payload mismatch")
                conn.rollback()
                return json.loads(previous["response_json"])
            before = self._state_in_conn(conn, row)
            next_revision = int(row["revision"]) + 1
            if tx.get("transaction_kind") == "collection_replace":
                records = payload.get("records")
                if not isinstance(records, list):
                    raise ValidationError("collection replacement requires records list")
                self._replace_collection(conn, row, tx["storage_key"], records, next_revision)
            elif tx.get("transaction_kind") == "compat_storage_replace":
                storage_key = str(payload.get("storage_key") or "")
                allowed_keys = set(tx.get("allowed_storage_keys", []))
                allowed_prefixes = tuple(tx.get("allowed_storage_prefixes", []))
                if storage_key not in allowed_keys and not storage_key.startswith(allowed_prefixes):
                    raise ValidationError(f"storage key is not declared: {storage_key}")
                value = canonical_clone(payload.get("value"))
                if storage_key in allowed_keys and isinstance(value, list):
                    self._replace_collection(conn, row, storage_key, value, next_revision)
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO session_runtime_fields VALUES (?,?,?,?,?,?)",
                        (sid, int(row["episode_epoch"]), "compat", f"storage::{storage_key}", canonical_json(value), next_revision),
                    )
            else:
                writes = payload.get("writes", {})
                if not isinstance(writes, dict):
                    raise ValidationError("writes must be an object")
                allowed = set(tx.get("state_writes", []))
                undeclared = sorted(set(writes) - allowed)
                if undeclared:
                    raise ValidationError("transaction attempted undeclared writes", details={"paths": undeclared})
                for path, value in writes.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO session_runtime_fields VALUES (?,?,?,?,?,?)",
                        (sid, int(row["episode_epoch"]), "business", path, canonical_json(value), next_revision),
                    )
            conn.execute(
                "UPDATE sessions SET revision=?,status=? WHERE sid=?",
                (next_revision, SessionStatus.RUNNING.value, sid),
            )
            row = self._session_row(conn, sid)
            after = self._state_in_conn(conn, row)
            changes = typed_diff(before, after)
            event = {"revision": next_revision, "transaction_id": transaction_id, "state_diff": changes}
            response = {"state_version": self._version(row), "transaction_id": transaction_id, "state_diff": changes, "events": [event]}
            instance_id = f"txi_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO session_transactions VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, int(row["episode_epoch"]), next_revision, instance_id, transaction_id, idempotency_key, request_hash, canonical_json(response), "logical"),
            )
            conn.execute(
                "INSERT INTO session_events VALUES (?,?,?,?,?,?)",
                (sid, int(row["episode_epoch"]), next_revision, 0, "transaction_committed", canonical_json(event)),
            )
            conn.commit()
        return response

    def record_action_receipt(
        self,
        sid: str,
        action_id: str,
        *,
        receipt_event: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not action_id or not receipt_event or not idempotency_key:
            raise ValidationError("action_id, receipt_event, and idempotency_key are required")
        request = {
            "action_id": action_id,
            "receipt_event": receipt_event,
            "payload": payload,
        }
        request_hash = canonical_hash(request)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._session_row(conn, sid)
            if row["status"] not in {SessionStatus.READY.value, SessionStatus.RUNNING.value}:
                raise LifecycleError("session is not ready")
            previous = conn.execute(
                "SELECT request_hash,response_json FROM session_transactions "
                "WHERE sid=? AND episode_epoch=? AND idempotency_key=?",
                (sid, int(row["episode_epoch"]), idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ConflictError("idempotency key payload mismatch")
                conn.rollback()
                return json.loads(previous["response_json"])
            next_revision = int(row["revision"]) + 1
            event = {
                "revision": next_revision,
                "event_type": receipt_event,
                "action_id": action_id,
                "payload": canonical_clone(payload),
            }
            conn.execute(
                "UPDATE sessions SET revision=?,status=? WHERE sid=?",
                (next_revision, SessionStatus.RUNNING.value, sid),
            )
            row = self._session_row(conn, sid)
            response = {"state_version": self._version(row), "receipt": event, "events": [event]}
            conn.execute(
                "INSERT INTO session_transactions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    int(row["episode_epoch"]),
                    next_revision,
                    f"rxi_{uuid.uuid4().hex}",
                    f"receipt::{action_id}",
                    idempotency_key,
                    request_hash,
                    canonical_json(response),
                    "host_observation",
                ),
            )
            conn.execute(
                "INSERT INTO session_events VALUES (?,?,?,?,?,?)",
                (sid, int(row["episode_epoch"]), next_revision, 0, receipt_event, canonical_json(event)),
            )
            conn.commit()
        return response

    def snapshot(self, sid: str, *, kind: str = "manual") -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = self._session_row(conn, sid)
            snapshot = self._snapshot_in_conn(conn, row, kind)
            conn.commit()
        return snapshot

    def _snapshot_row(self, conn: sqlite3.Connection, snapshot_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM session_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"unknown snapshot: {snapshot_id}")
        return row

    def reset(self, sid: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._session_row(conn, sid)
            if not row["initial_snapshot_id"]:
                raise LifecycleError("session has no sealed initial snapshot")
            initial = json.loads(self._snapshot_row(conn, row["initial_snapshot_id"])["state_json"])
            new_epoch = int(row["episode_epoch"]) + 1
            conn.execute(
                "UPDATE sessions SET episode_epoch=?,revision=0,status=?,initial_snapshot_id=NULL,final_snapshot_id=NULL WHERE sid=?",
                (new_epoch, SessionStatus.READY.value, sid),
            )
            row = self._session_row(conn, sid)
            for key, records in initial.get("collections", {}).items():
                self._replace_collection(conn, row, key, records, 0)
            for path, value in initial.get("runtime_fields", {}).items():
                conn.execute(
                    "INSERT INTO session_runtime_fields VALUES (?,?,?,?,?,?)",
                    (sid, new_epoch, "business", path, canonical_json(value), 0),
                )
            snapshot = self._snapshot_in_conn(conn, row, "initial")
            conn.execute("UPDATE sessions SET initial_snapshot_id=? WHERE sid=?", (snapshot["snapshot_id"], sid))
            conn.commit()
        return snapshot

    def clone(self, sid: str, new_sid: str, *, snapshot_id: str | None = None) -> dict[str, Any]:
        source = self.snapshot(sid, kind="clone_source") if not snapshot_id else None
        if source is None:
            with self._connect() as conn:
                row = self._snapshot_row(conn, snapshot_id or "")
                source = {"snapshot_id": row["snapshot_id"], "state": json.loads(row["state_json"])}
        self.create_session(new_sid)
        self.inject(new_sid, source["state"])
        result = self.seal_initial(new_sid)
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET clone_source_json=? WHERE sid=?", (canonical_json({"sid": sid, "snapshot_id": source["snapshot_id"]}), new_sid))
            conn.commit()
        return result

    def diff(self, sid: str, from_snapshot_id: str, to_snapshot_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            self._session_row(conn, sid)
            before = self._snapshot_row(conn, from_snapshot_id)
            after = self._snapshot_row(conn, to_snapshot_id)
            if before["sid"] != sid or after["sid"] != sid:
                raise ValidationError("snapshot sid mismatch")
        return {"from_snapshot_id": from_snapshot_id, "to_snapshot_id": to_snapshot_id, "changes": typed_diff(json.loads(before["state_json"]), json.loads(after["state_json"]))}

    def finalize(self, sid: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._session_row(conn, sid)
            if expected_revision is not None and int(expected_revision) != int(row["revision"]):
                raise ConflictError("stale expected_revision")
            if row["status"] == SessionStatus.FINALIZED.value and row["final_snapshot_id"]:
                snap = self._snapshot_row(conn, row["final_snapshot_id"])
                snapshot = {"snapshot_id": snap["snapshot_id"], "snapshot_kind": "final", "state_version": self._version(row), "state": json.loads(snap["state_json"]), "state_hash": snap["state_hash"]}
            else:
                snapshot = self._snapshot_in_conn(conn, row, "final")
                conn.execute("UPDATE sessions SET status=?,final_snapshot_id=? WHERE sid=?", (SessionStatus.FINALIZED.value, snapshot["snapshot_id"], sid))
            conn.commit()
        snapshot["evaluation_handle"] = {"sid": sid, "episode_epoch": snapshot["state_version"]["episode_epoch"], "revision": snapshot["state_version"]["revision"], "snapshot_id": snapshot["snapshot_id"], "state_hash": snapshot["state_hash"]}
        return snapshot

    def delete(self, sid: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._session_row(conn, sid)
            conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            conn.commit()
        return {"sid": sid, "deleted": True}

    def events(self, sid: str, *, from_revision: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            row = self._session_row(conn, sid)
            rows = conn.execute(
                "SELECT event_json FROM session_events WHERE sid=? AND episode_epoch=? AND revision>=? ORDER BY revision,event_index",
                (sid, int(row["episode_epoch"]), int(from_revision)),
            ).fetchall()
        return [json.loads(item["event_json"]) for item in rows]
