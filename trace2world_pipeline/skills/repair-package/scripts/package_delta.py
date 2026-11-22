#!/usr/bin/env python3
"""Record the edited dependency set Delta of one repair, and prove the frozen inputs did not move.

Repair edits only the package. Take a snapshot before the first edit and diff after the last:

    python package_delta.py snapshot --package "$RESULT/package" \
        --frozen "$RESULT/phase_consolidation/site_model" --frozen "$CHECKLISTS" \
        --output "$REPAIR/before.json"
    # ... edit the package ...
    python package_delta.py diff --before "$REPAIR/before.json" --output "$REPAIR/delta.json"

``diff`` re-hashes the same roots, lists every added, removed, or changed file (sorted) as
``delta``, and reports the package revision before and after. Package files are listed relative to
the package root. A repair that must also keep a formal source in sync with its packaged mirror
(for example ``$RESULT/phase_post_processing`` for checkpoints and evaluators) declares that root
with ``--editable``; its files are listed as ``<root name>:<relative path>``. It exits 1 when a
frozen root changed (the specification, checklists, or any other declared input may not be edited
inside the loop) or when nothing changed at all, and 0 otherwise.

The package revision is the SHA-256 over the sorted ``path\\tsha256`` lines of every package file,
so two packages with identical files have identical revisions. Execution, review, and probe
packets bind to this value; print it with ``package_delta.py revision --package <dir>``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_SCHEMA = "trace2world.package_snapshot.v1"
DELTA_SCHEMA = "trace2world.repair_delta.v1"
SKIP_DIRS = {"__pycache__", ".git"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: Path) -> dict[str, str]:
    root = root.resolve()
    if root.is_file():
        return {root.name: file_sha256(root)}
    if not root.is_dir():
        raise SystemExit(f"not a file or directory: {root}")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts):
            files[path.relative_to(root).as_posix()] = file_sha256(path)
    return files


def revision(files: dict[str, str]) -> str:
    lines = "".join(f"{name}\t{sha}\n" for name, sha in sorted(files.items()))
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


def editable_files(roots: dict[str, str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for name, root in roots.items():
        for rel, sha in hash_tree(Path(root)).items():
            files[f"{name}:{rel}"] = sha
    return files


def snapshot(args: argparse.Namespace) -> int:
    package = args.package.resolve()
    files = hash_tree(package)
    editable = {}
    for root in args.editable or []:
        name = root.resolve().name
        if name in editable:
            raise SystemExit(f"two editable roots share the name {name}")
        editable[name] = str(root.resolve())
    frozen = {}
    for root in args.frozen or []:
        tree = hash_tree(root)
        frozen[str(root.resolve())] = {"revision": revision(tree), "files": len(tree)}
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "package_root": str(package),
        "package_revision": revision(files),
        "files": files,
        "editable": editable,
        "editable_files": editable_files(editable),
        "frozen": frozen,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"snapshot": str(args.output), "package_revision": payload["package_revision"],
                      "files": len(files), "frozen_roots": len(frozen)}))
    return 0


def diff(args: argparse.Namespace) -> int:
    before = json.loads(args.before.read_text(encoding="utf-8"))
    if before.get("schema") != SNAPSHOT_SCHEMA:
        raise SystemExit(f"{args.before} is not a {SNAPSHOT_SCHEMA} snapshot")
    package_new = hash_tree(Path(before["package_root"]))
    old = {**before["files"], **before.get("editable_files", {})}
    new = {**package_new, **editable_files(before.get("editable", {}))}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(name for name in set(old) & set(new) if old[name] != new[name])
    frozen_changed = []
    for root, record in before.get("frozen", {}).items():
        now = revision(hash_tree(Path(root)))
        if now != record["revision"]:
            frozen_changed.append({"root": root, "before": record["revision"], "after": now})
    delta = sorted(added + removed + changed)
    errors = []
    if frozen_changed:
        errors.append("a frozen input changed during repair")
    if not delta:
        errors.append("nothing changed; a repair must return a non-empty delta")
    payload = {
        "schema": DELTA_SCHEMA,
        "package_root": before["package_root"],
        "package_revision_before": before["package_revision"],
        "package_revision_after": revision(package_new),
        "editable_roots": before.get("editable", {}),
        "delta": delta,
        "added": added,
        "removed": removed,
        "changed": changed,
        "frozen_changed": frozen_changed,
        "status": "fail" if errors else "pass",
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"delta": str(args.output), "status": payload["status"], "files": len(delta),
                      "errors": errors}))
    return 1 if errors else 0


def print_revision(args: argparse.Namespace) -> int:
    files = hash_tree(args.package)
    print(json.dumps({"package_root": str(args.package.resolve()), "package_revision": revision(files),
                      "files": len(files)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="hash the package and the frozen roots before editing")
    snap.add_argument("--package", required=True, type=Path)
    snap.add_argument("--editable", action="append", type=Path,
                      help="formal source kept in sync with its packaged mirror; repeatable")
    snap.add_argument("--frozen", action="append", type=Path, help="input that must not change; repeatable")
    snap.add_argument("--output", required=True, type=Path)
    dif = sub.add_parser("diff", help="compute Delta after editing")
    dif.add_argument("--before", required=True, type=Path)
    dif.add_argument("--output", required=True, type=Path)
    rev = sub.add_parser("revision", help="print the package revision")
    rev.add_argument("--package", required=True, type=Path)
    args = parser.parse_args()
    return {"snapshot": snapshot, "diff": diff, "revision": print_revision}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
