#!/usr/bin/env python3
"""Download the Trace2World source traces from the project dataset on the Hugging Face Hub and
verify every file against the SHA-256 manifest shipped with them.

Usage:
    python scripts/download_reference_traces.py                       # -> data/trace2world
    python scripts/download_reference_traces.py --dest /path          # custom location
    python scripts/download_reference_traces.py --verify-only         # re-check an existing download
    python scripts/download_reference_traces.py --environments kfc meituan   # only some environments

The destination contains `reference_trace_manifest.json`,
`reference_trace_index.json` and `traces/<environment_id>/<task_id>/`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_REPO = "LEONW24/Trace2World"
DEFAULT_DEST = Path(__file__).resolve().parents[1] / "data" / "trace2world"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(dest: Path, environments: list[str] | None) -> int:
    manifest_path = dest / "reference_trace_manifest.json"
    if not manifest_path.is_file():
        print(f"missing manifest: {manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest["payload_files"]
    if environments:
        wanted = {f"traces/{e}/" for e in environments}
        rows = [r for r in rows if any(r["path"].startswith(w) for w in wanted)]
    missing = mismatched = 0
    for row in rows:
        path = dest / row["path"]
        if not path.is_file():
            missing += 1
            continue
        if path.stat().st_size != row["size_bytes"] or sha256_file(path) != row["sha256"]:
            mismatched += 1
    listed = {r["path"] for r in rows}
    scope = [dest / "traces" / e for e in environments] if environments else [dest / "traces"]
    extra = sum(
        1
        for root in scope
        if root.exists()
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(dest).as_posix() not in listed
    )
    print(f"files: {len(rows)} missing: {missing} mismatched: {mismatched} extra: {extra}")
    ok = missing == 0 and mismatched == 0 and extra == 0
    print("status:", "pass" if ok else "fail")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Hugging Face dataset repository id")
    parser.add_argument("--revision", default=None, help="Optional dataset revision (branch, tag, or commit)")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="Download destination")
    parser.add_argument("--environments", nargs="*", default=None, help="Environment ids to download (default: all 20)")
    parser.add_argument("--verify-only", action="store_true", help="Skip the download and only verify --dest")
    args = parser.parse_args()

    if not args.verify_only:
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            print("huggingface_hub is required: pip install huggingface_hub", file=sys.stderr)
            return 2
        args.dest.mkdir(parents=True, exist_ok=True)
        patterns = ["README.md", "reference_trace_manifest.json", "reference_trace_index.json"] + (
            [f"traces/{e}/**" for e in args.environments] if args.environments else ["traces/**"]
        )
        snapshot_download(
            repo_id=args.repo,
            repo_type="dataset",
            revision=args.revision,
            local_dir=str(args.dest),
            allow_patterns=patterns,
        )
        print(f"downloaded {args.repo} -> {args.dest}")

    return verify(args.dest, args.environments)


if __name__ == "__main__":
    sys.exit(main())
