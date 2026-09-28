"""Provider-boundary tests for the post-L0 inventory producer."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


# The production caller is exercised through the injected provider boundary;
# these tests do not need an API credential or the optional provider SDK.
caller_stub = types.ModuleType("trace2world_pipeline.core.llm_caller")
caller_stub.call_openai_api_json_async = None
caller_stub.encode_image = lambda path: "data:image/png;base64,test"
sys.modules.setdefault("trace2world_pipeline.core.llm_caller", caller_stub)

from trace2world_pipeline.app_spec.visible_structure_inventory import (  # noqa: E402
    build_visible_structure_inventory,
)


class VisibleStructureInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.refs = []
        for trace_id in ("trace_a", "trace_b"):
            path = self.root / "traces" / trace_id / "screenshots" / "action_0001.png"
            path.parent.mkdir(parents=True)
            Image.new("RGB", (8, 8), "white").save(path)
            self.refs.append(str(path))
        self.selection_path = self.root / "phase_l0_fidelity/l0_visual_evidence_selection.json"
        self.selection_path.parent.mkdir(parents=True)
        self.selection_path.write_text(json.dumps({
            "format": "l0_visual_evidence_selection",
            "pages": {
                "city_xian.html": {
                    "selected_screenshot_refs": [self.refs[0]],
                    "screenshot_render_roles": {"trace_a:step1": "canonical_layout"},
                },
                "city_beijing.html": {
                    "selected_screenshot_refs": [self.refs[1]],
                    "screenshot_render_roles": {"trace_b:step1": "canonical_layout"},
                },
            },
        }), encoding="utf-8")
        prototypes = [
            SimpleNamespace(
                filename=name, page_id=name, page_name="Home",
                structure_data={"general_page_id": "home"},
            )
            for name in ("city_xian.html", "city_beijing.html")
        ]
        self.site_model = SimpleNamespace(
            structure=SimpleNamespace(page_prototypes=prototypes),
            trace_ids=["trace_a", "trace_b"],
        )

    def run_build(self, caller):
        return asyncio.run(build_visible_structure_inventory(
            selection_path=self.selection_path,
            site_model=self.site_model,
            output_dir=self.root,
            trace_root=self.root / "traces",
            model="mock-vision",
            max_concurrent=2,
            api_caller=caller,
        ))

    def test_selected_images_merge_under_general_page_and_resume_from_packets(self):
        calls = []

        async def provider(messages, **kwargs):
            calls.append((messages, kwargs))
            return json.dumps({
                "regions": [{
                    "component_key": "home.city_selector",
                    "component_type": "selector",
                    "visible_labels": ["城市"],
                    "visible_state_values": ["西安" if len(calls) == 1 else "北京"],
                    "action_host_visibility": "visible",
                }],
                "exclusion_reason": "",
            }), {"prompt_tokens": 10}

        output = self.run_build(provider)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 2)
        self.assertEqual({call[1]["stage"] for call in calls}, {"l0_visible_structure_extraction"})
        self.assertEqual(payload["selected_screenshot_count"], 2)
        self.assertEqual(payload["structure_row_count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["surface_id"], "home")
        self.assertEqual(set(row["evidence_refs"]), {"trace_a:step1", "trace_b:step1"})
        self.assertEqual(set(row["visible_state_values"]), {"西安", "北京"})
        self.assertEqual(len(list((self.root / "phase_l0_fidelity/instance_structure").glob("*.json"))), 2)
        artifact_index = json.loads((self.root / "artifact_index.json").read_text(encoding="utf-8"))
        self.assertIn("l0_fidelity.visible_structure_inventory", json.dumps(artifact_index))

        async def forbidden_provider(*args, **kwargs):
            raise AssertionError("unchanged selected image should reuse its API packet")

        self.run_build(forbidden_provider)

    def test_retry_calls_only_the_image_whose_packet_failed(self):
        attempts = []

        async def first_provider(messages, **kwargs):
            prompt = messages[0]["content"][0]["text"]
            attempts.append(prompt)
            if "trace_b:step1" in prompt:
                raise RuntimeError("provider failed on trace_b")
            return {"regions": [{
                "component_key": "home.city_selector", "component_type": "selector",
                "action_host_visibility": "visible",
            }]}, {}

        with self.assertRaisesRegex(RuntimeError, "trace_b"):
            self.run_build(first_provider)
        self.assertFalse((self.root / "phase_l0_fidelity/visible_structure_inventory.json").exists())

        async def retry_provider(messages, **kwargs):
            prompt = messages[0]["content"][0]["text"]
            self.assertIn("trace_b:step1", prompt)
            attempts.append(prompt)
            return {"regions": [{
                "component_key": "home.city_selector", "component_type": "selector",
                "action_host_visibility": "visible",
            }]}, {}

        self.run_build(retry_provider)
        self.assertEqual(len(attempts), 3)

    def test_invalid_response_cannot_publish_partial_inventory(self):
        async def provider(messages, **kwargs):
            return {"regions": [{"component_key": "bad key", "component_type": "button"}]}, {}

        with self.assertRaisesRegex(ValueError, "invalid component_key"):
            self.run_build(provider)
        self.assertFalse((self.root / "phase_l0_fidelity/visible_structure_inventory.json").exists())


if __name__ == "__main__":
    unittest.main()
