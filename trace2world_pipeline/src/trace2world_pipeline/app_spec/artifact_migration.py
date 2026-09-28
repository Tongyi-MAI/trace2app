
from __future__ import annotations

import argparse
import json
import shutil
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from trace2world_pipeline.core.phase_output import PHASE_ORDER, PHASE_ARTIFACTS, phase_index
from trace2world_pipeline.app_spec.phase_dependencies import cleanup_plan_for_changed_phase
from trace2world_pipeline.app_spec.artifact_index import rebuild_artifact_index


LIFECYCLE_CLEANUP_REPORT_RELATIVE_PATH = "run_management/artifact_lifecycle/cleanup_report.json"
LIFECYCLE_MIGRATION_REPORT_RELATIVE_PATH = "run_management/artifact_lifecycle/migration_report.json"

IGNORED_PATTERNS = (
    "artifact_index.json",
    LIFECYCLE_CLEANUP_REPORT_RELATIVE_PATH,
    "logs/**",
    "deprecated/**",
    ".DS_Store",
)


def _normalize_relative_path(path: str | Path) -> str:
    return Path(path).as_posix().strip("/")


def _matches(pattern: str, relative_path: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return relative_path == prefix or relative_path.startswith(prefix + "/")
    return fnmatch(relative_path, pattern)


def _is_ignored(relative_path: str) -> bool:
    parts = relative_path.split("/")
    return "deprecated" in parts or any(_matches(pattern, relative_path) for pattern in IGNORED_PATTERNS)


def _iter_files(root: Path):
    if not root.is_dir():
        return ()
    return (path for path in sorted(root.rglob("*")) if path.is_file())


def _expand_artifact_pattern(root: Path, pattern: str) -> list[Path]:
    if pattern.endswith("/**"):
        prefix = root / pattern[:-3]
        matches = [prefix] if prefix.exists() else []
        if prefix.is_dir():
            matches.extend(sorted(prefix.rglob("*")))
        return matches
    return sorted(root.glob(pattern))


def _delete_path(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _write_lifecycle_report(result_dir: Path, relative_path: str, payload: dict[str, Any]) -> Path:
    report_path = result_dir / relative_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def _artifact_index_artifact_count(index_path: Path | None) -> int | None:
    if not index_path or not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    return len(artifacts) if isinstance(artifacts, list) else None


def invalidate_from_phase(
    result_dir: str | Path,
    phase: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(result_dir)
    start_index = phase_index(phase)
    invalidated_phases = list(PHASE_ORDER[start_index:])
    matches: dict[str, str] = {}
    for invalidated_phase in invalidated_phases:
        for pattern in PHASE_ARTIFACTS.get(invalidated_phase, ()):
            for path in _expand_artifact_pattern(root, pattern):
                if path == root or not (path.exists() or path.is_symlink()):
                    continue
                matches[path.relative_to(root).as_posix()] = invalidated_phase

    deleted: list[dict[str, str]] = []
    for rel, invalidated_phase in sorted(matches.items(), key=lambda item: len(item[0]), reverse=True):
        path = root / rel
        if dry_run:
            deleted.append({"relative_path": rel, "phase": invalidated_phase})
            continue
        if _delete_path(path):
            deleted.append({"relative_path": rel, "phase": invalidated_phase})

    index_path: Path | None = None
    if not dry_run:
        index_path = rebuild_artifact_index(root, preserve_existing=True)
    payload = {
        "format": "ghostweb_artifact_lifecycle_cleanup_report",
        "version": 2,
        "result_dir": str(root),
        "from_phase": phase,
        "invalidated_phases": invalidated_phases,
        "dry_run": dry_run,
        "deleted_artifacts": deleted,
        "summary": {
            "deleted_count": len(deleted),
            "artifact_index_count": _artifact_index_artifact_count(index_path),
        },
    }
    if not dry_run:
        _write_lifecycle_report(root, LIFECYCLE_CLEANUP_REPORT_RELATIVE_PATH, payload)
    return payload


def invalidate_from_changed_phase(
    result_dir: str | Path,
    changed_phase: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    plan = cleanup_plan_for_changed_phase(changed_phase)
    report = invalidate_from_phase(
        result_dir,
        str(plan["earliest_stale_phase"]),
        dry_run=dry_run,
    )
    report["changed_phase"] = changed_phase
    report["dependency_cleanup_plan"] = plan
    return report


def migrate_result_artifacts(
    source_dir: str | Path,
    target_dir: str | Path,
    *,
    through_phase: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    source = Path(source_dir)
    target = Path(target_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"source result dir does not exist: {source}")

    max_idx = phase_index(through_phase)
    phases_to_copy = list(PHASE_ORDER[: max_idx + 1])

    copied: list[dict[str, str]] = []
    for copy_phase in phases_to_copy:
        for pattern in PHASE_ARTIFACTS.get(copy_phase, ()):
            for path in _expand_artifact_pattern(source, pattern):
                if not path.is_file():
                    continue
                rel = path.relative_to(source).as_posix()
                if _is_ignored(rel):
                    continue
                if dry_run:
                    copied.append({"relative_path": rel, "phase": copy_phase})
                    continue
                dst = target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst)
                copied.append({"relative_path": rel, "phase": copy_phase})

    payload = {
        "format": "ghostweb_artifact_migration",
        "version": 2,
        "source_result_dir": str(source),
        "target_result_dir": str(target),
        "through_phase": through_phase,
        "phase_order": list(PHASE_ORDER),
        "dry_run": dry_run,
        "copied_artifacts": copied,
        "summary": {
            "copied_count": len(copied),
            "phases_copied": phases_to_copy,
        },
    }
    if not dry_run:
        _write_lifecycle_report(target, LIFECYCLE_MIGRATION_REPORT_RELATIVE_PATH, payload)
    return payload


def assert_output_artifacts_registered(result_dir: str | Path) -> dict[str, Any]:
    root = Path(result_dir)
    index_path = root / "artifact_index.json"
    if not index_path.is_file():
        return {"status": "passed", "checked_artifact_count": 0, "unknown_artifacts": []}

    known_paths: set[str] = set()
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    for item in payload.get("artifacts", []) if isinstance(payload, dict) else []:
        if isinstance(item, dict) and item.get("path"):
            known_paths.add(_normalize_relative_path(item["path"]))
    known_paths.add("artifact_index.json")

    files = [
        path.relative_to(root).as_posix()
        for path in _iter_files(root)
        if not _is_ignored(path.relative_to(root).as_posix())
    ]
    unknown = [rel for rel in files if rel not in known_paths]

    return {
        "status": "failed" if unknown else "passed",
        "checked_artifact_count": len(files),
        "unknown_artifacts": unknown,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trace2World artifact lifecycle management.")
    parser.add_argument("--invalidate-result", help="Result directory to clean from a lifecycle phase.")
    parser.add_argument("--from-phase", choices=PHASE_ORDER, help="Phase to invalidate from.")
    parser.add_argument("--source", help="Source result directory for migration.")
    parser.add_argument("--target", help="Target result directory for migration.")
    parser.add_argument("--through-phase", choices=PHASE_ORDER, help="Highest phase to copy.")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without executing.")
    args = parser.parse_args(argv)

    if args.invalidate_result:
        if not args.from_phase:
            parser.error("--invalidate-result requires --from-phase")
        report = invalidate_from_phase(args.invalidate_result, args.from_phase, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.source and args.target and args.through_phase:
        report = migrate_result_artifacts(
            args.source, args.target, through_phase=args.through_phase, dry_run=args.dry_run,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    parser.error("Use --invalidate-result + --from-phase, or --source + --target + --through-phase")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
