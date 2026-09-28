from __future__ import annotations

from typing import Any, Iterable, Mapping

from trace2world_pipeline.state_kernel.canonical_json import canonical_hash


def _entity_rows(entity_schemas: Any) -> list[dict[str, Any]]:
    if isinstance(entity_schemas, Mapping):
        raw = entity_schemas.get("entities", entity_schemas.get("entity_schemas", []))
        if isinstance(raw, Mapping):
            return [dict(value, name=name) for name, value in raw.items() if isinstance(value, Mapping)]
    else:
        raw = entity_schemas
    return [dict(item) for item in raw or [] if isinstance(item, Mapping)]


def _transaction_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("transactions")
    if isinstance(rows, list):
        return [dict(item) for item in rows if isinstance(item, Mapping)]
    by_id = payload.get("transactions_by_id")
    if isinstance(by_id, Mapping):
        return [dict(item) for item in by_id.values() if isinstance(item, Mapping)]
    return []


def _seed_collections(
    seed_data: Mapping[str, Any] | None,
    entities: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, list[Any]], dict[str, str]]:
    name_to_key: dict[str, str] = {}
    for entity in entities:
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        name_to_key[name] = str(entity.get("storage_key") or entity.get("storageKey") or name).strip()
    collections: dict[str, list[Any]] = {}
    for raw_key, raw_value in (seed_data or {}).items():
        if str(raw_key).startswith("_") or not isinstance(raw_value, list):
            continue
        storage_key = name_to_key.get(str(raw_key), str(raw_key))
        collections[storage_key] = list(raw_value)
    return collections, name_to_key


def compile_environment_state_artifacts(
    *,
    global_execution_schema: Mapping[str, Any] | None,
    entity_schemas: Any,
    runtime_transaction_contracts: Mapping[str, Any] | None,
    seed_data: Mapping[str, Any] | None,
    source_provenance: Mapping[str, Any] | None = None,
    mutable_collection_storage_keys: Iterable[str] | None = None,
    compatibility_storage_replace: bool = True,
) -> dict[str, dict[str, Any]]:
    schema = dict(global_execution_schema or {})
    entities = _entity_rows(entity_schemas)
    collections, name_to_key = _seed_collections(seed_data, entities)
    transactions = _transaction_rows(runtime_transaction_contracts)

    schema_hash = canonical_hash({
        "global_execution_schema": schema,
        "entity_schemas": entities,
    })
    world_hash = canonical_hash(collections)

    entity_types: list[dict[str, Any]] = []
    for entity in sorted(entities, key=lambda item: str(item.get("name") or "")):
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        storage_key = name_to_key.get(name, str(entity.get("storage_key") or name))
        fields = [dict(field) for field in entity.get("fields", []) if isinstance(field, Mapping)]
        entity_types.append({
            "entity_type": name,
            "storage_key": storage_key,
            "id_field": str(entity.get("id_field") or "id"),
            "fields": fields,
            "world_record_count": len(collections.get(storage_key, [])),
            "overlay_policy": "entity_replace_or_tombstone",
        })
    covered_storage_keys = {item["storage_key"] for item in entity_types}
    for storage_key in sorted(set(collections) - covered_storage_keys):
        sample_fields: list[dict[str, Any]] = []
        sample = next((item for item in collections[storage_key] if isinstance(item, Mapping)), None)
        if isinstance(sample, Mapping):
            sample_fields = [
                {"name": str(key), "type": "json"}
                for key in sorted(sample)
            ]
        entity_types.append({
            "entity_type": storage_key,
            "storage_key": storage_key,
            "id_field": "id",
            "fields": sample_fields,
            "world_record_count": len(collections[storage_key]),
            "overlay_policy": "entity_replace_or_tombstone",
            "schema_authority": "seed_only_evidence_entity",
        })

    global_fields = []
    for field in schema.get("global_fields", []) if isinstance(schema.get("global_fields"), list) else []:
        if not isinstance(field, Mapping) or not field.get("field_path"):
            continue
        global_fields.append({
            "canonical_path": str(field["field_path"]),
            "value_type": str(field.get("field_type") or "json"),
            "namespace": "business",
            "scope_kind": "session",
            "lifecycle": str(field.get("lifecycle") or "runtime"),
            "snapshot_policy": "included",
            "persistence": "sqlite_overlay",
            "evaluator_policy": "judge_visible" if field.get("consumers") else "observable",
            "source": "global_execution_schema",
        })

    state_refs: set[str] = set()
    normalized_transactions: list[dict[str, Any]] = []
    for transaction in transactions:
        transaction_id = str(transaction.get("transaction_id") or "").strip()
        if not transaction_id:
            continue
        reads = sorted({str(item) for item in transaction.get("state_reads", []) if item})
        writes = sorted({str(item) for item in transaction.get("state_writes", []) if item})
        state_refs.update(reads)
        state_refs.update(writes)
        normalized_transactions.append({
            **transaction,
            "transaction_id": transaction_id,
            "input_schema": transaction.get("input_schema") or {"type": "object", "additionalProperties": True},
            "state_reads": reads,
            "state_writes": writes,
            "write_policy": "declared_paths_only",
            "idempotency": "required",
            "revision_policy": "expected_revision_cas",
        })

    mutable_collections = (
        set(collections)
        if mutable_collection_storage_keys is None
        else {str(value) for value in mutable_collection_storage_keys}
    )
    unknown_mutable_collections = mutable_collections - set(collections)
    if unknown_mutable_collections:
        raise ValueError(
            "mutable collection storage keys are not present in the world seed: "
            + ", ".join(sorted(unknown_mutable_collections))
        )
    for storage_key in sorted(mutable_collections):
        normalized_transactions.append({
            "transaction_id": f"collection::{storage_key}::replace",
            "transaction_kind": "collection_replace",
            "storage_key": storage_key,
            "input_schema": {"type": "object", "required": ["records"]},
            "state_reads": [f"collection::{storage_key}"],
            "state_writes": [f"collection::{storage_key}"],
            "write_policy": "entity_diff_replace",
            "idempotency": "required",
            "revision_policy": "expected_revision_cas",
        })
    if compatibility_storage_replace:
        normalized_transactions.append({
            "transaction_id": "compat::storage_replace",
            "transaction_kind": "compat_storage_replace",
            "input_schema": {"type": "object", "required": ["storage_key", "value"]},
            "allowed_storage_keys": sorted([*collections, "idCounter", "__ghostweb_seed_version"]),
            "allowed_storage_prefixes": ["__gw_drafts__", "__gw_passenger_form_draft__", "__ghostweb_route_events"],
            "state_reads": ["compat::storage::<payload.storage_key>"],
            "state_writes": ["compat::storage::<payload.storage_key>"],
            "write_policy": "compiled_storage_key_or_prefix_only",
            "idempotency": "required",
            "revision_policy": "expected_revision_cas",
        })

    extension_fields = [
        {
            "canonical_path": ref,
            "value_type": "json",
            "namespace": "ui_judged" if ref.startswith("state::") else "business",
            "scope_kind": "session",
            "lifecycle": "runtime",
            "snapshot_policy": "included",
            "persistence": "sqlite_overlay",
            "evaluator_policy": "observable",
            "source": "runtime_transaction_contracts",
        }
        for ref in sorted(state_refs)
        if ref not in {item["canonical_path"] for item in global_fields}
    ]

    storage_plan = {
        "format": "ghostweb_environment_storage_plan",
        "schema_revision": 1,
        "backend": "sqlite",
        "schema_hash": schema_hash,
        "world_hash": world_hash,
        "world_database": "world.sqlite",
        "state_database_template": "state.sqlite.template",
        "entity_types": entity_types,
        "indexes": [
            {"storage_key": row["storage_key"], "fields": [row["id_field"]]}
            for row in entity_types
        ],
        "sqlite": {
            "journal_mode": "WAL",
            "foreign_keys": True,
            "busy_timeout_ms": 30000,
            "max_concurrent_writers": 32,
            "transaction_mode": "BEGIN IMMEDIATE",
        },
        "source_provenance": dict(source_provenance or {}),
    }
    runtime_state_registry = {
        "format": "ghostweb_runtime_state_registry",
        "schema_revision": 1,
        "schema_hash": schema_hash,
        "world_hash": world_hash,
        "fields": sorted(global_fields + extension_fields, key=lambda item: item["canonical_path"]),
        "namespaces": ["business", "navigation", "environment", "ui_judged"],
        "authority": "environment_state_kernel",
    }
    transaction_manifest = {
        "format": "ghostweb_transaction_manifest",
        "schema_revision": 1,
        "schema_hash": schema_hash,
        "world_hash": world_hash,
        "transactions": normalized_transactions,
        "transactions_by_id": {
            item["transaction_id"]: item for item in normalized_transactions
        },
    }
    seed_manifest = {
        "format": "ghostweb_environment_seed_manifest",
        "schema_revision": 1,
        "schema_hash": schema_hash,
        "world_hash": world_hash,
        "collections": {
            key: {"record_count": len(value), "content_hash": canonical_hash(value)}
            for key, value in sorted(collections.items())
        },
        "default_overlay": {},
        "injection_policy": "controller_initializing_only",
    }
    evaluator_state_view = {
        "format": "ghostweb_evaluator_state_view",
        "schema_revision": 1,
        "schema_hash": schema_hash,
        "world_hash": world_hash,
        "read_contract": "finalized_snapshot_only",
        "canonical_paths": [item["canonical_path"] for item in runtime_state_registry["fields"]],
        "transaction_ids": [item["transaction_id"] for item in normalized_transactions],
        "unknown_path_policy": "fail_closed",
        "dom_evidence_role": "visible_projection_only",
    }
    state_api_contract = {
        "format": "ghostweb_state_api_contract",
        "schema_revision": 1,
        "schema_hash": schema_hash,
        "world_hash": world_hash,
        "runtime_profile": "state_kernel_sqlite",
        "roles": {
            "page_runtime": ["bootstrap", "read_collection", "read_view", "execute_transaction"],
            "episode_controller": ["create", "inject", "seal_initial", "reset", "clone", "finalize", "delete"],
            "evaluator_reader": ["snapshot", "diff", "events", "evaluator_view"],
        },
        "state_version": ["sid", "episode_epoch", "revision", "schema_hash", "world_hash", "bundle_hash"],
        "errors": {"validation": 422, "conflict": 409, "finalized": 410, "not_found": 404},
    }
    artifacts = {
        "storage_plan": storage_plan,
        "runtime_state_registry": runtime_state_registry,
        "transaction_manifest": transaction_manifest,
        "seed_manifest": seed_manifest,
        "evaluator_state_view": evaluator_state_view,
        "state_api_contract": state_api_contract,
    }
    bundle_hash = canonical_hash({name: canonical_hash(value) for name, value in artifacts.items()})
    for payload in artifacts.values():
        payload["bundle_hash"] = bundle_hash
    return artifacts
