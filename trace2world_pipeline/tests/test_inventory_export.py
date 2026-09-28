from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trace2world_pipeline.compiler.delivery_contract import sha256_file
from trace2world_pipeline.compiler.export.inventory_export import export_visible_structure_inventory


class InventoryExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.result = self.root / "result"
        self.package = self.root / "package"
        self.selection = self.result / "phase_l0_fidelity/l0_visual_evidence_selection.json"
        self.inventory = self.result / "phase_l0_fidelity/visible_structure_inventory.json"
        self.selection.parent.mkdir(parents=True)
        self.selection.write_text('{"format":"l0_visual_evidence_selection"}', encoding="utf-8")

    def test_missing_inventory_blocks_l0_export(self):
        with self.assertRaisesRegex(FileNotFoundError, "post-selection"):
            export_visible_structure_inventory(self.result, self.package)

    def test_export_binds_exact_selection_and_digest(self):
        self.inventory.write_text(json.dumps({
            "format": "trace2world.visible_structure_inventory.v1",
            "coverage_status": "complete_for_selected_screenshots",
            "source_l0_selection_sha256": sha256_file(self.selection),
            "inventory_scope": "selected_l0_instance_pages",
        }), encoding="utf-8")
        binding = export_visible_structure_inventory(self.result, self.package)
        self.assertEqual(binding["sha256"], sha256_file(self.package / binding["path"]))
        self.assertEqual(binding["source_l0_selection_sha256"], sha256_file(self.selection))

        self.selection.write_text('{"format":"l0_visual_evidence_selection","revision":2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            export_visible_structure_inventory(self.result, self.package)


if __name__ == "__main__":
    unittest.main()
