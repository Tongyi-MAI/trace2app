

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trace2world_pipeline.state_kernel.compiler import compile_environment_state_artifacts
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


def _load(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"required producer artifact is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize(result_dir: Path, *, replace_existing: bool = False) -> dict[str, Any]:
    site_model_dir = result_dir / "phase_consolidation" / "site_model"
    schema_path = result_dir / "phase_consolidation" / "projections" / "schema" / "global_execution_schema.json"
    compiled_dir = result_dir / "phase_page_environment" / "semantic_api" / "08_spec_compiled_execution_views"
    runtime_transactions_path = compiled_dir / "runtime_transaction_contracts.json"
    content_path = site_model_dir / "content.json"
    entity_seed_path = site_model_dir / "entity_seed.json"
    existing_files = sorted(
        path for path in compiled_dir.glob("*.json") if path.stem not in STATE_ARTIFACTS
    )
    before = {str(path.relative_to(result_dir)): _sha256(path) for path in existing_files}

    entity_seed = _load(entity_seed_path)
    seed_data = entity_seed_to_init_data(entity_seed)
    read_model_collections, read_model_manifest = compiled_read_model_to_init_data(result_dir)
    seed_data.update(read_model_collections)
    artifacts = compile_environment_state_artifacts(
        global_execution_schema=_load(schema_path),
        entity_schemas=_load(content_path).get("entity_schemas", []),
        runtime_transaction_contracts=_load(runtime_transactions_path),
        seed_data=seed_data,
        source_provenance={
            "global_schema": str(schema_path.relative_to(result_dir)),
            "runtime_transactions": str(runtime_transactions_path.relative_to(result_dir)),
            "entity_seed": str(entity_seed_path.relative_to(result_dir)),
            "read_model_migration": read_model_manifest,
        },
    )
    replaced: list[str] = []
    if replace_existing:
        deprecated_root = compiled_dir / "deprecated"
        deprecated = deprecated_root / "state_kernel_pre_outcome_transaction_extension"
        if deprecated.exists() and any(deprecated.iterdir()):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            deprecated = deprecated_root / f"state_kernel_pre_replace_{stamp}"
        deprecated.mkdir(parents=True, exist_ok=True)
        for name in STATE_ARTIFACTS:
            target = compiled_dir / f"{name}.json"
            if target.exists():
                archived = deprecated / target.name
                if archived.exists():
                    raise FileExistsError(f"state-kernel replacement archive already exists: {archived}")
                shutil.move(str(target), str(archived))
                replaced.append(str(target.relative_to(result_dir)))
    for name in STATE_ARTIFACTS:
        target = compiled_dir / f"{name}.json"
        if target.exists():
            raise FileExistsError(
                f"refusing to overwrite existing state-kernel contract: {target}"
            )
        target.write_text(
            json.dumps(artifacts[name], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    after = {str(path.relative_to(result_dir)): _sha256(path) for path in existing_files}
    if before != after:
        raise RuntimeError("pre-existing compiled execution views changed during materialization")
    report = {
        "format": "ghostweb_environment_state_kernel_materialization_report",
        "schema_revision": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result_dir": str(result_dir),
        "earliest_affected_phase": "phase_page_environment/semantic_api/08_spec_compiled_execution_views",
        "preserved_existing_compiled_view_hashes": before,
        "created_artifacts": [
            str((compiled_dir / f"{name}.json").relative_to(result_dir))
            for name in STATE_ARTIFACTS
        ],
        "replaced_artifacts": replaced,
        "schema_hash": artifacts["storage_plan"]["schema_hash"],
        "world_hash": artifacts["storage_plan"]["world_hash"],
        "transaction_count": len(artifacts["transaction_manifest"]["transactions"]),
        "runtime_field_count": len(artifacts["runtime_state_registry"]["fields"]),
    }
    report_path = result_dir / "run_management" / "environment_state_kernel_materialization.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    print(json.dumps(
        materialize(args.result_dir.resolve(), replace_existing=args.replace_existing),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
