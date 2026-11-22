#!/usr/bin/env python3
"""Reduce per-trace checklist status into the checklist-set manifest and index."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checklist-dir", required=True, type=Path)
    args = parser.parse_args()

    manifest_path = args.checklist_dir / "manifest.json"
    index_path = args.checklist_dir / "trace_index.json"
    manifest = load_json(manifest_path)
    index = load_json(index_path)
    entries = index.get("traces")
    if not isinstance(entries, list):
        raise ValueError(f"{index_path}: traces must be a list")

    all_ready = True
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("trace_id"):
            raise ValueError(f"{index_path}: invalid trace entry")
        trace_id = str(entry["trace_id"])
        checklist = load_json(args.checklist_dir / "traces" / trace_id / "stepwise_checklist.json")
        evidence = load_json(args.checklist_dir / "traces" / trace_id / "evidence.json")
        blockers = list(checklist.get("blockers") or []) + list(evidence.get("blockers") or [])
        row_blockers = [
            {"step_index": row.get("step_index"), "blockers": row.get("blockers")}
            for row in checklist.get("rows") or []
            if isinstance(row, dict) and row.get("blockers")
        ]
        if row_blockers:
            blockers.append({"row_blockers": row_blockers})
        ready = (
            checklist.get("status") == "ready"
            and checklist.get("sprite_opened_and_read") is True
            and evidence.get("sprite_opened_and_read") is True
            and not blockers
        )
        entry["status"] = "ready" if ready else ("blocked" if checklist.get("status") == "blocked" else "pending")
        entry["blockers"] = blockers
        all_ready = all_ready and ready

    manifest["trace_count"] = len(entries)
    manifest["trace_ids"] = sorted(str(entry["trace_id"]) for entry in entries)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["status"] = "ready" if all_ready else "blocked" if any(
        entry.get("status") == "blocked" for entry in entries
    ) else "pending"
    write_json(index_path, index)
    write_json(manifest_path, manifest)
    print(json.dumps({"checklist_dir": str(args.checklist_dir), "status": manifest["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
