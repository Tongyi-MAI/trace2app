from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Iterable


FORMAL_APP_EXPORTS: tuple[tuple[str, bool], ...] = (
    ("phase_pagegen/pages", True),
    ("phase_pagegen/images", False),
    ("phase_pagegen/assets", False),
    ("phase_pagegen/action_host_manifest.json", False),
    ("phase_pagegen/action_host_manifest_validation.json", False),


    ("phase_pagegen/vendor", False),
    ("phase_sdk_generation/runtime", True),
    ("phase_post_processing/tasks", True),
    ("phase_post_processing/evaluators", True),
    ("phase_post_processing/instrumentation", False),
    ("phase_post_processing/action_authority", False),
    ("phase_post_processing/terminal_policy", False),
)

EXCLUDED_SCAFFOLD: tuple[str, ...] = (
    "phase_pagegen/debug",
    "phase_pagegen/page_inputs",
    "phase_pagegen/sprites",
    "phase_pagegen/projections",
    "phase_pagegen/validation",
    "phase_sdk_generation/projections",
    "phase_sdk_generation/validation",
    "phase_post_processing/debug",
)

STATE_KERNEL_REQUIRED: tuple[str, ...] = (
    "phase_sdk_generation/runtime/environment_state_client.js",
    "phase_sdk_generation/runtime/backend/service_manifest.json",
    "phase_sdk_generation/runtime/backend/prepare_session.py",
    "phase_sdk_generation/runtime/backend/run_state_service.py",
    "phase_sdk_generation/runtime/backend/world.sqlite",
    "phase_sdk_generation/runtime/backend/state.sqlite.template",
    "phase_sdk_generation/runtime/backend/materialization_report.json",
    "phase_sdk_generation/runtime/backend/contracts/storage_plan.json",
    "phase_sdk_generation/runtime/backend/contracts/runtime_state_registry.json",
    "phase_sdk_generation/runtime/backend/contracts/transaction_manifest.json",
    "phase_sdk_generation/runtime/backend/contracts/seed_manifest.json",
    "phase_sdk_generation/runtime/backend/contracts/evaluator_state_view.json",
    "phase_sdk_generation/runtime/backend/contracts/state_api_contract.json",
    "phase_post_processing/instrumentation/instrumentation_state_bindings.json",
    "phase_post_processing/evaluators/evaluator_state_bindings.json",
)

INSTRUMENTATION_REQUIRED: tuple[str, ...] = (
    "phase_pagegen/action_host_manifest.json",
    "phase_post_processing/action_authority/action_execution_contracts.json",
    "phase_post_processing/instrumentation/checkpoint_matrix.json",
    "phase_post_processing/instrumentation/runtime_event_bindings.json",
    "phase_post_processing/instrumentation/runtime_state_schema.json",
    "phase_post_processing/evaluators/evaluators.json",
    "phase_post_processing/evaluators/executable_evaluator_manifest.json",
)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_formal_app(
    source_root: Path,
    app_root: Path,
    *,
    require_evaluators: bool = True,
) -> None:
    for relative_path, required in FORMAL_APP_EXPORTS:
        if relative_path == "phase_post_processing/evaluators" and not require_evaluators:
            required = False
        source = source_root / relative_path
        target = app_root / relative_path
        if not source.exists():
            if required:
                raise FileNotFoundError(f"Missing required formal package export: {source}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        else:
            shutil.copy2(source, target)


def _dynamic_runtime_paths(source_root: Path) -> list[str]:
    runtime_root = source_root / "phase_sdk_generation/runtime"
    paths = [
        str(path.relative_to(source_root))
        for path in sorted(runtime_root.glob("*"))
        if path.is_file() and path.suffix in {".js", ".json"}
    ]
    python_root = runtime_root / "backend/python"
    paths.extend(
        str(path.relative_to(source_root))
        for path in sorted(python_root.rglob("*.py"))
        if path.is_file()
    )
    return paths


def _exported_formal_paths(source_root: Path) -> list[str]:
    paths: list[str] = []
    for relative_path, _required in FORMAL_APP_EXPORTS:
        source = source_root / relative_path
        if source.is_file():
            paths.append(relative_path)
            continue
        if not source.is_dir():
            continue
        paths.extend(
            str(path.relative_to(source_root))
            for path in sorted(source.rglob("*"))
            if path.is_file()
            and path.suffix != ".pyc"
            and "__pycache__" not in path.parts
            and ".pytest_cache" not in path.parts
        )
    return paths


def required_formal_paths(
    source_root: Path,
    *,
    require_action_authority: bool = True,
    require_evaluators: bool = True,
) -> tuple[list[str], bool, bool]:
    state_kernel_enabled = (
        source_root / "phase_sdk_generation/runtime/backend/service_manifest.json"
    ).is_file()
    instrumentation_enabled = (
        source_root / "phase_post_processing/instrumentation/checkpoint_matrix.json"
    ).is_file()
    paths = ["phase_post_processing/tasks/tasks.json"]


    vendor_root = source_root / "phase_pagegen/vendor"
    if vendor_root.is_dir():
        paths.extend(
            str(path.relative_to(source_root))
            for path in sorted(vendor_root.rglob("*"))
            if path.is_file()
        )
    if require_evaluators:
        paths.append("phase_post_processing/evaluators/evaluators.json")
    if state_kernel_enabled:
        paths.extend(STATE_KERNEL_REQUIRED)
    if instrumentation_enabled:
        excluded_optional_paths = set()
        if not require_action_authority:
            excluded_optional_paths.add(
                "phase_post_processing/action_authority/action_execution_contracts.json"
            )
        if not require_evaluators:
            excluded_optional_paths.update(
                {
                    "phase_post_processing/evaluators/evaluators.json",
                    "phase_post_processing/evaluators/executable_evaluator_manifest.json",
                }
            )
        paths.extend(path for path in INSTRUMENTATION_REQUIRED if path not in excluded_optional_paths)
    paths.extend(_dynamic_runtime_paths(source_root))


    paths.extend(_exported_formal_paths(source_root))
    return sorted(set(paths)), state_kernel_enabled, instrumentation_enabled


def build_formal_manifest(
    *,
    source_root: Path,
    app_root: Path,
    package_name: str,
    target: str,
    require_action_authority: bool = True,
    require_evaluators: bool = True,
) -> dict[str, Any]:
    paths, state_kernel_enabled, instrumentation_enabled = required_formal_paths(
        source_root,
        require_action_authority=require_action_authority,
        require_evaluators=require_evaluators,
    )
    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for relative_path in paths:
        source = source_root / relative_path
        packaged = app_root / relative_path
        source_exists = source.is_file()
        package_exists = packaged.is_file()
        source_hash = sha256_file(source) if source_exists else None
        package_hash = sha256_file(packaged) if package_exists else None
        record = {
            "path": relative_path,
            "source_exists": source_exists,
            "package_exists": package_exists,
            "source_sha256": source_hash,
            "package_sha256": package_hash,
            "hash_match": bool(source_exists and package_exists and source_hash == package_hash),
        }
        records.append(record)
        if not source_exists:
            issues.append({"code": "missing_source_formal_artifact", "path": relative_path})
        elif not package_exists:
            issues.append({"code": "missing_packaged_formal_artifact", "path": relative_path})
        elif source_hash != package_hash:
            issues.append({"code": "formal_artifact_hash_mismatch", "path": relative_path})

    authority_path = app_root / "phase_post_processing/action_authority/action_execution_contracts.json"
    authority = read_json(authority_path, {}) or {}
    authority_summary = {
        "required": instrumentation_enabled and require_action_authority,
        "path": "phase_post_processing/action_authority/action_execution_contracts.json",
        "action_count": int(authority.get("action_count") or 0),
        "checkpoint_count": int(authority.get("checkpoint_count") or 0),
        "missing_source_actions": authority.get("missing_source_actions") or [],
        "duplicate_action_ids": authority.get("duplicate_action_ids") or [],
    }
    if instrumentation_enabled and require_action_authority:
        if authority_summary["action_count"] <= 0:
            issues.append({"code": "empty_action_execution_contracts", "path": authority_summary["path"]})
        if authority_summary["missing_source_actions"]:
            issues.append({"code": "action_execution_contracts_missing_source_actions", "path": authority_summary["path"]})
        if authority_summary["duplicate_action_ids"]:
            issues.append({"code": "action_execution_contracts_duplicate_action_ids", "path": authority_summary["path"]})

    return {
        "schema": "ghostweb.formal_delivery_manifest.v1",
        "status": "pass" if not issues else "fail",
        "target": target,
        "package_name": package_name,
        "source_result_dir": str(source_root),
        "export_policy": "formal_allowlist_hash_verified",
        "export_groups": [path for path, _required in FORMAL_APP_EXPORTS],
        "excluded_scaffold": list(EXCLUDED_SCAFFOLD),
        "state_kernel": {
            "enabled": state_kernel_enabled,
            "backend": "sqlite" if state_kernel_enabled else None,
        },
        "instrumentation": {
            "enabled": instrumentation_enabled,
            "action_authority": authority_summary,
        },
        "required_artifacts": records,
        "issues": issues,
    }


def transient_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and (path.suffix == ".pyc" or "__pycache__" in path.parts)
    )


def leaked_scaffold(root: Path, excluded: Iterable[str] = EXCLUDED_SCAFFOLD) -> list[str]:
    return [relative for relative in excluded if (root / relative).exists()]
