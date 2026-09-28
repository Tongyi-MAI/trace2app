"""Package the source-linked structure inventory without changing its authority."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from trace2world_pipeline.compiler.delivery_contract import read_json, sha256_file


def export_visible_structure_inventory(result_dir: Path, package_dir: Path) -> dict[str, Any] | None:
    selection_source = result_dir / "phase_l0_fidelity/l0_visual_evidence_selection.json"
    inventory_source = result_dir / "phase_l0_fidelity/visible_structure_inventory.json"
    if selection_source.is_file() and not inventory_source.is_file():
        raise FileNotFoundError(
            "L0 export requires the post-selection visible structure inventory: "
            f"{inventory_source}"
        )
    if not inventory_source.is_file():
        return None
    if not selection_source.is_file():
        raise FileNotFoundError(f"Inventory has no L0 selection manifest: {selection_source}")
    payload = read_json(inventory_source, {}) or {}
    if payload.get("format") != "trace2world.visible_structure_inventory.v1":
        raise ValueError(f"Unsupported visible structure inventory: {inventory_source}")
    if payload.get("coverage_status") != "complete_for_selected_screenshots":
        raise ValueError(f"Incomplete visible structure inventory: {inventory_source}")
    if payload.get("source_l0_selection_sha256") != sha256_file(selection_source):
        raise ValueError(f"Visible structure inventory is stale against L0 selection: {inventory_source}")
    target = package_dir / "data/visible_structure_inventory.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(inventory_source, target)
    return {
        "path": "data/visible_structure_inventory.json",
        "sha256": sha256_file(target),
        "source_l0_selection_sha256": payload["source_l0_selection_sha256"],
        "scope": payload["inventory_scope"],
    }
