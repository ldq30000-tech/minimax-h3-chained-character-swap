from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "assets" / "workflows"
STABLE = WORKFLOWS / "h3-native-loop-final-stable-ui.json"
EXPERIMENTAL = WORKFLOWS / "h3-native-loop-final-turbo-experimental-ui.json"


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stable = json.loads(STABLE.read_text(encoding="utf-8"))
        cls.experimental = json.loads(EXPERIMENTAL.read_text(encoding="utf-8"))

    @staticmethod
    def nodes(workflow: dict) -> dict[int, dict]:
        return {int(node["id"]): node for node in workflow["nodes"]}

    def test_stable_release_keeps_final_canvas_acceleration_nodes(self) -> None:
        nodes = self.nodes(self.stable)
        self.assertEqual(nodes[1970]["type"], "MiniMaxH3MemoryEfficientSageAttentionPatch")
        self.assertEqual(nodes[1971]["type"], "ReservedVRAMSetter")
        self.assertEqual(nodes[1972]["type"], "LoraLoaderModelOnly")

    def test_stable_release_disconnects_every_turbo_lora(self) -> None:
        nodes = self.nodes(self.stable)
        lora_ids = {
            node_id
            for node_id, node in nodes.items()
            if node["type"] == "LoraLoaderModelOnly"
        }
        self.assertEqual(lora_ids, {1962, 1972})
        self.assertTrue(all(nodes[node_id]["mode"] == 2 for node_id in lora_ids))
        self.assertFalse(
            any(
                int(link[1]) in lora_ids or int(link[3]) in lora_ids
                for link in self.stable["links"]
            )
        )
        self.assertTrue(
            any(
                int(link[1]) == 1970 and int(link[3]) == 1941
                for link in self.stable["links"]
            )
        )

    def test_stable_release_has_portable_inputs_and_valid_placeholder_plan(self) -> None:
        nodes = self.nodes(self.stable)
        self.assertEqual(nodes[1950]["widgets_values"][0], "source_video.mp4")
        self.assertEqual(
            [nodes[node_id]["widgets_values"][0] for node_id in (1945, 1946, 1963, 1964)],
            [
                "character_front.png",
                "character_side.png",
                "character_back.png",
                "character_face_closeup.png",
            ],
        )
        plan = json.loads(nodes[1700]["widgets_values"][0])
        self.assertTrue(all(isinstance(shot["prompt"], str) for shot in plan["shots"]))
        self.assertEqual(nodes[1700]["widgets_values"][1], "h3_native_loop_release")

    def test_dynamic_final_trim_and_original_audio_are_connected(self) -> None:
        nodes = self.nodes(self.stable)
        final = nodes[1961]
        inputs = {value["name"]: value for value in final["inputs"]}
        self.assertIsNotNone(inputs["source_frame_count"]["link"])
        self.assertIsNotNone(inputs["source_audio"]["link"])
        self.assertIn("SOURCE-FRAME", final["title"])

    def test_experimental_variant_preserves_enabled_turbo_route_and_warning(self) -> None:
        nodes = self.nodes(self.experimental)
        self.assertEqual(nodes[1972]["mode"], 0)
        self.assertTrue(
            any(
                int(link[1]) == 1972 or int(link[3]) == 1972
                for link in self.experimental["links"]
            )
        )
        self.assertIn("warning", self.experimental["extra"]["release"])


if __name__ == "__main__":
    unittest.main()
