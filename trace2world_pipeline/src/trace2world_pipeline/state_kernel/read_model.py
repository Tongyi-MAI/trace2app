from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"compiled read-model artifact is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"compiled read-model artifact must be an object: {path}")
    return value


def _with_exact_ids(
    rows: Any,
    *,
    identity_field: str,
    collection: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError(f"{collection} rows must be a list")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{collection} contains a non-object row")
        row = dict(raw)
        identity = row.get(identity_field)
        if identity in (None, ""):
            raise ValueError(f"{collection} row lacks exact {identity_field}")
        exact_id = str(identity)
        if exact_id in seen:
            raise ValueError(f"{collection} contains duplicate identity: {exact_id}")
        seen.add(exact_id)
        row.setdefault("id", exact_id)
        output.append(row)
    return output


def compiled_read_model_to_init_data(
    result_dir: str | Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    root = Path(result_dir)
    read_model_root = root / "phase_read_model"
    route_path = read_model_root / "route_resolver_index.json"
    search_path = read_model_root / "search_projection_index.json"
    route_payload = _read(route_path)
    search_payload = _read(search_path)
    routes = _with_exact_ids(
        route_payload.get("routes"),
        identity_field="route",
        collection="route_records",
    )
    candidates = _with_exact_ids(
        search_payload.get("candidates"),
        identity_field="candidate_id",
        collection="search_candidates",
    )
    expected_route_count = int(route_payload.get("route_count") or 0)
    expected_candidate_count = int(search_payload.get("candidate_count") or 0)
    if expected_route_count != len(routes):
        raise ValueError("route read-model count mismatch")
    if expected_candidate_count != len(candidates):
        raise ValueError("search read-model count mismatch")
    manifest = {
        "format": "ghostweb.read_model_sqlite_migration_manifest.v1",
        "schema_revision": 1,
        "runtime_authority": "world.sqlite",
        "parallel_json_runtime_allowed": False,
        "collections": {
            "route_records": {
                "source_path": str(route_path.relative_to(root)),
                "source_sha256": _sha256(route_path),
                "record_count": len(routes),
                "identity_field": "route",
            },
            "search_candidates": {
                "source_path": str(search_path.relative_to(root)),
                "source_sha256": _sha256(search_path),
                "record_count": len(candidates),
                "identity_field": "candidate_id",
            },
        },
        "status": "ready_for_sqlite_materialization",
    }
    return {"route_records": routes, "search_candidates": candidates}, manifest
