
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from trace2world_pipeline.core.phase_output import PhaseOutputManager
from trace2world_pipeline.app_spec.field_canonicalization import (
    build_field_canonicalization_contract,
    canonicalize_content_samples,
)


DISPLAY_FIELDS = (
    "title_name",
    "name",
    "title",
    "display_name",
    "label",
    "merchant_name",
    "product_name",
    "deal_name",
    "item_name",
)

IDENTITY_FIELDS = (
    "tax_id",
    "tax_number",
    "phone",
    "phone_number",
    "mobile",
    "email",
    "code",
    "sku",
    "item_id",
)


def _snake_case(name: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name or "item")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return value or "item"


def _stable_hash(value: Any, length: int = 12) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _full_stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(record or {})
    for key, value in list(normalized.items()):
        snake_key = _snake_case(str(key))
        if snake_key and snake_key != key and value not in (None, ""):
            normalized.setdefault(snake_key, value)
    if normalized.get("title_name") and not normalized.get("name"):
        normalized["name"] = normalized.get("title_name")
    if normalized.get("name") and not normalized.get("title_name"):
        normalized["title_name"] = normalized.get("name")
    return normalized


def _meaningful_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in (record or {}).items():
        if key == "id":
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        result[key] = value
    return result


def _display_value(record: Dict[str, Any]) -> str:
    for field in DISPLAY_FIELDS:
        value = str((record or {}).get(field, "") or "").strip()
        if value:
            return value
    return ""


def _identity_value(record: Dict[str, Any]) -> str:
    for field in IDENTITY_FIELDS:
        value = str((record or {}).get(field, "") or "").strip()
        if value:
            return value
    return ""


def _record_text_values(record: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for value in (record or {}).values():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            text = str(value)
        text = text.strip()
        if text:
            values.append(text)
    return values


def _dedupe_key(record: Dict[str, Any]) -> str:
    display = _display_value(record)
    identity = _identity_value(record)
    if display and identity:
        return f"display:{display}|identity:{identity}"
    if display:
        return f"display:{display}"
    return ""


def _compatible(base: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    for key in set(base.keys()) & set(incoming.keys()):
        if key == "id":
            continue
        left = base.get(key)
        right = incoming.get(key)
        if left in (None, "") or right in (None, ""):
            continue
        if left == right:
            continue
        left_text = str(left).strip()
        right_text = str(right).strip()
        if left_text and right_text and (left_text in right_text or right_text in left_text):
            continue
        return False
    return True


def _merge_records(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(incoming or {})
    merged.update(base or {})
    for key, value in (incoming or {}).items():
        if merged.get(key) in (None, "") and value not in (None, ""):
            merged[key] = value
    return merged


def _entity_specs(entity_schemas: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    specs: Dict[str, Dict[str, Any]] = {}
    for spec in entity_schemas or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name", "") or "").strip()
        if not name:
            continue
        fields = [
            str(field.get("name", "") or "").strip()
            for field in spec.get("fields", [])
            if isinstance(field, dict) and str(field.get("name", "") or "").strip()
        ]
        specs[name] = {
            "name": name,
            "storage_key": str(spec.get("storage_key", "") or _snake_case(name) + "s"),
            "field_names": set(fields),
            "schema_role": spec.get("schema_role", ""),
            "data_pre_generation_num": spec.get("data_pre_generation_num", ""),
        }
    return specs


def _excluded_value_hints(
    runtime_state_observations: Dict[str, Any],
) -> List[Tuple[str, Set[str]]]:
    hints: Dict[str, Set[str]] = {}

    def add_hint(value: Any, cluster_id: str) -> None:
        text = str(value or "").strip()
        if text:
            hints.setdefault(text, set()).add(cluster_id)

    if not isinstance(runtime_state_observations, dict):
        return []
    lifecycle_steps = (
        runtime_state_observations.get("derived", {})
        .get("content_lifecycle_plan", {})
        .get("content_lifecycle_steps", [])
    )
    lifecycle_plan = runtime_state_observations.get("derived", {}).get("content_lifecycle_plan", {})
    clusters = lifecycle_plan.get("clusters", {}) if isinstance(lifecycle_plan, dict) else {}
    if isinstance(clusters, dict):
        for cluster_key, cluster in clusters.items():
            if not isinstance(cluster, dict):
                continue
            for value in cluster.get("exclude_from_initial_value_hints", []) or []:
                add_hint(value, cluster_key)
            for step in cluster.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                phase = str(step.get("lifecycle_phase", "") or "")
                if phase and not phase.startswith("post_"):
                    continue
                for value in step.get("exclude_from_initial_value_hints", []) or []:
                    add_hint(value, cluster_key)
    if not isinstance(lifecycle_steps, list):
        lifecycle_steps = runtime_state_observations.get("content_lifecycle_steps", [])
    for step in lifecycle_steps if isinstance(lifecycle_steps, list) else []:
        if not isinstance(step, dict):
            continue
        phase = str(step.get("lifecycle_phase", "") or "")
        if phase and not phase.startswith("post_"):
            continue
        for value in step.get("exclude_from_initial_value_hints", []) or []:
            add_hint(value, "_global")
    return [(text, cids) for text, cids in hints.items()]


class EntitySeedMaterializer:

    def __init__(
        self,
        entity_schemas: List[Dict[str, Any]],
        content_samples: Dict[str, List[Dict[str, Any]]],
        runtime_state_observations: Dict[str, Any] | None = None,
        field_canonicalization_contract: Dict[str, Any] | None = None,
    ):
        self.entity_schemas = entity_schemas or []
        self.raw_content_samples = content_samples or {}
        self.runtime_state_observations = runtime_state_observations or {}
        self.specs = _entity_specs(self.entity_schemas)
        self.exclude_hints = _excluded_value_hints(self.runtime_state_observations)
        self.field_canonicalization_contract = (
            field_canonicalization_contract
            if isinstance(field_canonicalization_contract, dict)
            else build_field_canonicalization_contract(self.entity_schemas, self.raw_content_samples)
        )
        self.content_samples, self.field_canonicalization_decisions = canonicalize_content_samples(
            self.raw_content_samples,
            self.field_canonicalization_contract,
        )

    def build_candidates(self) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        by_entity: Dict[str, List[str]] = defaultdict(list)
        for entity_name, records in sorted((self.content_samples or {}).items()):
            if not isinstance(records, list):
                continue
            spec = self.specs.get(entity_name, {})
            storage_key = str(spec.get("storage_key") or _snake_case(entity_name) + "s")
            for index, raw_record in enumerate(records):
                if not isinstance(raw_record, dict):
                    continue
                source_cluster_id = raw_record.get("_source_cluster_id") or raw_record.get("source_cluster_id")
                normalized = _normalize_record(raw_record)
                normalized.pop("_source_cluster_id", None)
                normalized.pop("source_cluster_id", None)
                candidate_id = f"{entity_name}:cand_{index:04d}_{_stable_hash(normalized, 8)}"
                candidate = {
                    "candidate_id": candidate_id,
                    "entity_name": entity_name,
                    "storage_key": storage_key,
                    "normalized_fields": normalized,
                    "field_presence": sorted(k for k, v in normalized.items() if v not in (None, "")),
                    "source_steps": [],
                    "source_pages": [],
                    "source_clusters": [source_cluster_id] if source_cluster_id else [],
                    "source_roles": ["content_sample"],
                    "nearby_runtime_observations": [],
                    "local_signature": _stable_hash(normalized),
                }
                candidates.append(candidate)
                by_entity[entity_name].append(candidate_id)
        return {
            "format": "entity_seed_candidates",
            "schema_revision": 1,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "candidate_ids_by_entity": dict(by_entity),
            "exclude_value_hints": [
                {"hint": h, "clusters": sorted(c)} for h, c in self.exclude_hints
            ],
        }

    def _classify_candidate(self, candidate: Dict[str, Any]) -> Tuple[str, str]:
        fields = candidate.get("normalized_fields", {})
        meaningful = _meaningful_fields(fields)
        if not meaningful:
            return "empty_placeholder", "record has no non-empty fields except id"
        values = _record_text_values(fields)
        candidate_clusters = set(candidate.get("source_clusters") or [])
        for hint_text, hint_clusters in self.exclude_hints:
            if not hint_text:
                continue
            if candidate_clusters and hint_clusters and "_global" not in hint_clusters:
                if not candidate_clusters & hint_clusters:
                    continue
            if any(hint_text in value or (len(value) >= 6 and value in hint_text) for value in values):
                return "runtime_created", f"matches post-commit exclude hint: {hint_text}"
        if not _display_value(fields) and not _identity_value(fields):
            return "ambiguous", "record lacks display and identity fields"
        return "observed_initial_state", "content evidence is usable as initial seed"

    def materialize(self) -> Dict[str, Any]:
        candidates_artifact = self.build_candidates()
        entities: Dict[str, Dict[str, Any]] = {}
        validation = {
            "format": "entity_seed_validation",
            "schema_revision": 1,
            "errors": [],
            "warnings": [],
            "summary": {},
        }

        by_entity: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for candidate in candidates_artifact["candidates"]:
            by_entity[candidate["entity_name"]].append(candidate)

        for entity_name, candidates in sorted(by_entity.items()):
            spec = self.specs.get(entity_name, {})
            storage_key = str(spec.get("storage_key") or _snake_case(entity_name) + "s")
            merged_records: List[Dict[str, Any]] = []
            key_to_indices: Dict[str, List[int]] = defaultdict(list)
            excluded: List[Dict[str, Any]] = []
            merge_decisions: List[Dict[str, Any]] = []

            for candidate in candidates:
                classification, reason = self._classify_candidate(candidate)
                fields = dict(candidate.get("normalized_fields", {}) or {})
                if classification != "observed_initial_state":
                    excluded.append({
                        "candidate_id": candidate["candidate_id"],
                        "classification": classification,
                        "reason": reason,
                        "source_steps": candidate.get("source_steps", []),
                    })
                    continue

                key = _dedupe_key(fields)
                if key:
                    for index in key_to_indices.get(key, []):
                        if _compatible(merged_records[index]["fields"], fields):
                            merged_records[index]["fields"] = _merge_records(merged_records[index]["fields"], fields)
                            merged_records[index]["candidate_ids"].append(candidate["candidate_id"])
                            merge_decisions.append({
                                "target_record_index": index,
                                "candidate_id": candidate["candidate_id"],
                                "dedupe_key": key,
                                "reason": "same display/identity signature",
                            })
                            break
                    else:
                        key_to_indices[key].append(len(merged_records))
                        merged_records.append({
                            "fields": fields,
                            "candidate_ids": [candidate["candidate_id"]],
                            "source_steps": candidate.get("source_steps", []),
                        })
                else:
                    validation["warnings"].append({
                        "entity_name": entity_name,
                        "candidate_id": candidate["candidate_id"],
                        "reason": "included candidate lacks dedupe key",
                    })
                    merged_records.append({
                        "fields": fields,
                        "candidate_ids": [candidate["candidate_id"]],
                        "source_steps": candidate.get("source_steps", []),
                    })

            records = []
            for index, record in enumerate(merged_records):
                fields = dict(record["fields"])
                fields.pop("id", None)
                records.append({
                    "id": f"{storage_key}_{index}",
                    "fields": fields,
                    "seed_origin": "observed_initial_state",
                    "source_steps": sorted(set(record.get("source_steps", []))),
                    "candidate_ids": record.get("candidate_ids", []),
                    "confidence": "medium" if not record.get("source_steps") else "high",
                })

            entities[entity_name] = {
                "storage_key": storage_key,
                "schema_role": spec.get("schema_role", ""),
                "records": records,
                "excluded_candidates": excluded,
                "merge_decisions": merge_decisions,
            }

        validation["summary"] = {
            "entity_count": len(entities),
            "record_count": sum(len(item.get("records", [])) for item in entities.values()),
            "excluded_candidate_count": sum(len(item.get("excluded_candidates", [])) for item in entities.values()),
            "exclude_hint_count": len(self.exclude_hints),
        }
        seed = {
            "format": "entity_seed",
            "schema_revision": 1,
            "entities": entities,
            "validation": validation,
            "candidate_summary": {
                "candidate_count": candidates_artifact["candidate_count"],
                "candidate_ids_by_entity": candidates_artifact["candidate_ids_by_entity"],
            },
        }
        return {
            "seed": seed,
            "candidates": candidates_artifact,
            "validation": validation,
            "budget_report": self._budget_report(candidates_artifact, seed),
            "field_canonicalization_contract": self.field_canonicalization_contract,
            "field_canonicalization_decisions": self.field_canonicalization_decisions,
            "canonical_content_samples": self.content_samples,
        }

    def _budget_report(self, candidates_artifact: Dict[str, Any], seed: Dict[str, Any]) -> Dict[str, Any]:
        shards = []
        for entity_name, ids in sorted(candidates_artifact.get("candidate_ids_by_entity", {}).items()):
            entity_seed = seed.get("entities", {}).get(entity_name, {})
            payload = {
                "candidate_ids": ids,
                "records": entity_seed.get("records", []),
                "excluded_candidates": entity_seed.get("excluded_candidates", []),
            }
            approx_chars = len(json.dumps(payload, ensure_ascii=False, default=str))
            shards.append({
                "entity_name": entity_name,
                "candidate_count": len(ids),
                "seed_record_count": len(entity_seed.get("records", [])),
                "excluded_candidate_count": len(entity_seed.get("excluded_candidates", [])),
                "approx_chars": approx_chars,
                "approx_tokens": max(1, round(approx_chars / 4)),
            })
        return {
            "format": "entity_seed_budget_report",
            "schema_revision": 1,
            "shards": shards,
            "max_shard_tokens": max((item["approx_tokens"] for item in shards), default=0),
            "total_candidate_count": candidates_artifact.get("candidate_count", 0),
        }


def _seed_record_to_init_record(item: Dict[str, Any]) -> Dict[str, Any]:
    raw_fields = item.get("fields")
    raw_data = item.get("data")
    if isinstance(raw_data, dict):
        record = dict(raw_data)
        for key, value in item.items():
            if key in {"data", "fields"}:
                continue
            record.setdefault(str(key), value)
    elif isinstance(raw_fields, dict):
        record = dict(raw_fields)
        if item.get("id") is not None:
            record.setdefault("id", item.get("id"))
    else:


        record = dict(item)

    exact_id = next(
        (
            value
            for value in (
                record.get("id"),
                item.get("id"),
                item.get("entity_key"),
                item.get("resource_key"),
                item.get("resource_id"),
                item.get("pool_row_id"),
            )
            if value not in (None, "")
        ),
        None,
    )
    if exact_id is not None:
        record.setdefault("id", str(exact_id))
    return record


def entity_seed_to_init_data(entity_seed: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    init_data: Dict[str, List[Dict[str, Any]]] = {}
    if not isinstance(entity_seed, dict):
        return init_data
    entities = entity_seed.get("entities", {})
    if not isinstance(entities, dict):
        return init_data
    for entity_name, payload in entities.items():
        if not isinstance(payload, dict):
            continue
        records = []
        for item in payload.get("records", []) or []:
            if not isinstance(item, dict):
                continue
            records.append(_seed_record_to_init_record(item))
        init_data[str(entity_name)] = records

    resources = entity_seed.get("resources")
    if isinstance(resources, dict):
        resource_records = [
            _seed_record_to_init_record(item)
            for item in resources.get("records", []) or []
            if isinstance(item, dict)
        ]
        storage_key = str(resources.get("storage_key") or "Resource")
        init_data.setdefault(storage_key, []).extend(resource_records)
    return init_data


def materialize_entity_seed(
    entity_schemas: List[Dict[str, Any]],
    content_samples: Dict[str, List[Dict[str, Any]]],
    runtime_state_observations: Dict[str, Any] | None = None,
    field_canonicalization_contract: Dict[str, Any] | None = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    artifact = EntitySeedMaterializer(
        entity_schemas=entity_schemas,
        content_samples=content_samples,
        runtime_state_observations=runtime_state_observations or {},
        field_canonicalization_contract=field_canonicalization_contract,
    ).materialize()
    if output_dir:
        pom = PhaseOutputManager(output_dir)
        site_model_dir = str(pom.site_model_dir())
        debug_dir = str(pom.debug("consolidation"))
        shard_dir = os.path.join(debug_dir, "entity_seed_shards")
        os.makedirs(site_model_dir, exist_ok=True)
        os.makedirs(debug_dir, exist_ok=True)
        os.makedirs(shard_dir, exist_ok=True)
        with open(os.path.join(site_model_dir, "entity_seed.json"), "w", encoding="utf-8") as f:
            json.dump(artifact["seed"], f, indent=2, ensure_ascii=False)
        with open(os.path.join(debug_dir, "entity_seed_candidates.json"), "w", encoding="utf-8") as f:
            json.dump(artifact["candidates"], f, indent=2, ensure_ascii=False)
        with open(os.path.join(debug_dir, "entity_seed_validation.json"), "w", encoding="utf-8") as f:
            json.dump(artifact["validation"], f, indent=2, ensure_ascii=False)
        with open(os.path.join(debug_dir, "entity_seed_budget_report.json"), "w", encoding="utf-8") as f:
            json.dump(artifact["budget_report"], f, indent=2, ensure_ascii=False)
        with open(os.path.join(debug_dir, "field_canonicalization_contract.json"), "w", encoding="utf-8") as f:
            json.dump(artifact["field_canonicalization_contract"], f, indent=2, ensure_ascii=False)
        with open(os.path.join(debug_dir, "field_canonicalization_decisions.json"), "w", encoding="utf-8") as f:
            json.dump(artifact["field_canonicalization_decisions"], f, indent=2, ensure_ascii=False)
        with open(os.path.join(debug_dir, "canonical_content_samples.json"), "w", encoding="utf-8") as f:
            json.dump(artifact["canonical_content_samples"], f, indent=2, ensure_ascii=False)
        for shard in artifact["budget_report"].get("shards", []):
            entity_name = shard["entity_name"]
            entity_payload = artifact["seed"].get("entities", {}).get(entity_name, {})
            shard_payload = {
                "format": "entity_seed_shard",
                "schema_revision": 1,
                "entity_name": entity_name,
                "budget": shard,
                "seed": entity_payload,
            }
            shard_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", entity_name).strip("_") or "entity"
            with open(os.path.join(shard_dir, f"{shard_name}.json"), "w", encoding="utf-8") as f:
                json.dump(shard_payload, f, indent=2, ensure_ascii=False)
    return artifact
