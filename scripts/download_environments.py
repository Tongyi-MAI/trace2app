#!/usr/bin/env python3
"""Download the Trace2World environments from the project dataset on the Hugging Face Hub and
verify every file against the SHA-256 manifest shipped with it.

Usage:
    python scripts/download_environments.py                 # -> data/trace2world
    python scripts/download_environments.py --dest /path    # custom location
    python scripts/download_environments.py --verify-only   # re-check an existing download

The destination directory is what `t2w-serve --environments-dir` expects: it
contains `environments_manifest.json` and `environments/<id>/`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_REPO = "LEONW24/Trace2World"
DEFAULT_DEST = Path(__file__).resolve().parents[1] / "data" / "trace2world"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Hugging Face dataset repository id")
    parser.add_argument("--revision", default=None, help="Optional dataset revision (branch, tag, or commit)")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="Download destination")
    parser.add_argument("--verify-only", action="store_true", help="Skip the download and only verify --dest")
    args = parser.parse_args()

    if not args.verify_only:
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            print("huggingface_hub is required: pip install huggingface_hub", file=sys.stderr)
            return 2
        args.dest.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=args.repo,
            repo_type="dataset",
            revision=args.revision,
            local_dir=str(args.dest),
            allow_patterns=["README.md", "environments_manifest.json", "environments/**"],
        )
        print(f"downloaded {args.repo} -> {args.dest}")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verify_environments import verify

    return verify(args.dest)


if __name__ == "__main__":
    sys.exit(main())
