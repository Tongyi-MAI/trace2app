

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from trace2world_pipeline.state_kernel.materializer import materialize_environment_state_runtime
from trace2world_pipeline.state_kernel.read_model import compiled_read_model_to_init_data
from trace2world_pipeline.app_spec.entity_seed import entity_seed_to_init_data


STATE_ARTIFACTS = (
    "storage_plan",
    "runtime_state_registry",
    "transaction_manifest",
    "seed_manifest",
    "evaluator_state_view",
    "state_api_contract",
)


def _read(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"required producer artifact is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def materialize(result_dir: Path) -> dict[str, Any]:
    compiled = result_dir / "phase_page_environment/semantic_api/08_spec_compiled_execution_views"
    artifacts = {name: _read(compiled / f"{name}.json") for name in STATE_ARTIFACTS}
    init_data = entity_seed_to_init_data(
        _read(result_dir / "phase_consolidation/site_model/entity_seed.json")
    )
    read_model_collections, read_model_manifest = compiled_read_model_to_init_data(result_dir)
    init_data.update(read_model_collections)
    world: dict[str, list[Any]] = {}
    entities_by_storage_key = {
        str(entity.get("storage_key") or entity.get("entity_type") or ""): entity
        for entity in artifacts["storage_plan"].get("entity_types", [])
        if isinstance(entity, dict)
    }
    for storage_key in (artifacts["seed_manifest"].get("collections") or {}):
        entity = entities_by_storage_key.get(str(storage_key), {})
        entity_name = str(entity.get("entity_type") or storage_key)
        candidate = init_data.get(entity_name, init_data.get(storage_key, []))
        if not isinstance(candidate, list):
            raise ValueError(f"world seed for {storage_key} is not a list")
        world[str(storage_key)] = candidate
    result = materialize_environment_state_runtime(
        output_dir=result_dir,
        artifacts=artifacts,
        world=world,
    )
    migration_manifest_path = (
        result_dir
        / "phase_sdk_generation/runtime/backend/contracts/read_model_migration_manifest.json"
    )
    read_model_manifest = {
        **read_model_manifest,
        "world_hash": artifacts["storage_plan"]["world_hash"],
        "status": "materialized_in_world_sqlite",
    }
    migration_manifest_path.write_text(
        json.dumps(read_model_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = {
        "format": "trace2world.environment_state_runtime_generation.v1",
        "result_dir": str(result_dir),
        "runtime_dir": result["runtime_dir"],
        "manifest": result["manifest"],
        "materialization_report": result["materialization_report"],
    }
    path = result_dir / "run_management/environment_state_runtime_generation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(materialize(args.result_dir.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
