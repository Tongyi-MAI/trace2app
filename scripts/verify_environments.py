#!/usr/bin/env python3
"""Verify a Trace2World environment release directory against its manifest.

Checks that every file listed in `environments_manifest.json` exists with the
recorded SHA-256 and size, that no unlisted files are present under
`environments/`, and that the per-environment task counts match `tasks.json`.

Usage:
    python scripts/verify_environments.py --environments-dir data/trace2world
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_FORMAT = "trace2world.environment_manifest.v1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root: Path) -> int:
    root = root.expanduser().resolve()
    manifest_path = root / "environments_manifest.json"
    if not manifest_path.is_file():
        print(f"missing manifest: {manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != MANIFEST_FORMAT:
        print(f"unexpected manifest format: {manifest.get('format')!r}", file=sys.stderr)
        return 1

    listed = {row["path"]: row for row in manifest["payload_files"]}
    missing: list[str] = []
    mismatched: list[str] = []
    for rel, row in listed.items():
        path = root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        if path.stat().st_size != row["size_bytes"] or sha256(path) != row["sha256"]:
            mismatched.append(rel)

    present = {
        p.relative_to(root).as_posix()
        for p in (root / "environments").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    extra = sorted(present - set(listed))

    task_errors: list[str] = []
    for env in manifest["environments"]:
        tasks_file = root / env["path"] / "tasks.json"
        if not tasks_file.is_file():
            task_errors.append(f"{env['id']}: tasks.json missing")
            continue
        count = len(json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"])
        if count != env["task_count"]:
            task_errors.append(f"{env['id']}: {count} tasks on disk, manifest says {env['task_count']}")

    print(
        f"environments: {len(manifest['environments'])}  tasks: {manifest['task_count']}  "
        f"files: {len(listed)}  missing: {len(missing)}  mismatched: {len(mismatched)}  "
        f"extra: {len(extra)}  task-count errors: {len(task_errors)}"
    )
    for label, items in (("missing", missing), ("mismatched", mismatched), ("extra", extra), ("task", task_errors)):
        for item in items[:10]:
            print(f"  {label}: {item}")
    ok = not (missing or mismatched or extra or task_errors)
    print("status:", "pass" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--environments-dir", type=Path, required=True)
    args = parser.parse_args()
    return verify(args.environments_dir)


if __name__ == "__main__":
    sys.exit(main())
