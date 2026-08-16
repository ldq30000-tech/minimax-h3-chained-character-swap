from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "assets" / "workflows" / "h3-native-loop-long-video-character-swap-ui.json"


class NativeLoopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        cls.nodes = {int(node["id"]): node for node in cls.workflow["nodes"]}

    def links_to(self, node_id: int, input_name: str) -> list[list[object]]:
        node = self.nodes[node_id]
        input_index = next(
            index for index, value in enumerate(node.get("inputs", []))
            if value["name"] == input_name
        )
        return [
            link for link in self.workflow["links"]
            if int(link[3]) == node_id and int(link[4]) == input_index
        ]

    def test_real_recursive_body_is_visible(self) -> None:
        required = {
            "MiniMaxH3ChainPlan",
            "MiniMaxH3ChainLoopStart",
            "MiniMaxH3ChainCurrent",
            "MiniMaxH3TaggedReferenceToVideo",
            "MiniMaxH3ChainContext",
            "SamplerCustomAdvanced",
            "MiniMaxH3LoopTrim",
            "MiniMaxH3ChainSegmentSave",
            "MiniMaxH3ChainReview",
            "MiniMaxH3ChainLoopEnd",
            "MiniMaxH3ChainAssemble",
            "H3FinalTrimToSource",
        }
        self.assertTrue(required.issubset({node["type"] for node in self.nodes.values()}))

    def test_known_source_plan_delivers_617_then_trims_to_614(self) -> None:
        plan = json.loads(self.nodes[1700]["widgets_values"][0])
        lengths = [shot["length"] for shot in plan["shots"]]
        self.assertEqual(lengths, [124, 124, 124, 124, 124, 107])
        self.assertEqual(lengths[0] + sum(length - 22 for length in lengths[1:]), 617)
        self.assertEqual(self.workflow["extra"]["h3_native_loop"]["source_frames"], 614)
        self.assertTrue(self.links_to(1961, "source_frame_count"))

    def test_source_audio_drives_loop_and_assembly(self) -> None:
        for node_id in (1701, 1702, 1706, 1707, 1708, 1944):
            link = self.links_to(node_id, "source_audio")
            self.assertEqual(len(link), 1)
            self.assertEqual((int(link[0][1]), int(link[0][2])), (1960, 1))
        exact_audio = self.links_to(1961, "source_audio")
        self.assertEqual((int(exact_audio[0][1]), int(exact_audio[0][2])), (1960, 2))

    def test_inputs_and_sequential_reference_are_visible(self) -> None:
        images = [
            node for node in self.nodes.values()
            if node["type"] == "LoadImage" and "@character_" in node.get("title", "")
        ]
        self.assertEqual(len(images), 4)
        scene = self.nodes[1951]
        self.assertEqual(scene["type"], "H3NativeLongVideoScene")
        video_tag = self.nodes[1952]
        self.assertEqual(
            video_tag["widgets_values"],
            ["motion", "motion_audio", "restart_each_scene"],
        )
        self.assertTrue(self.links_to(1700, "plan_json_input"))

    def test_only_current_scene_is_decoded_from_source_timeline(self) -> None:
        prepare = self.nodes[1960]
        self.assertEqual(prepare["type"], "H3NativeLongVideoPrepare")
        self.assertEqual(prepare["outputs"][0]["type"], "H3_NATIVE_VIDEO_TIMELINE")
        scene_timeline = self.links_to(1951, "source_timeline")
        self.assertEqual(
            (int(scene_timeline[0][1]), int(scene_timeline[0][2])),
            (1960, 0),
        )
        scene_state = self.links_to(1951, "state")
        self.assertEqual((int(scene_state[0][1]), int(scene_state[0][2])), (1702, 0))
        self.assertFalse(
            any(
                output["type"] == "IMAGE"
                for output in prepare.get("outputs", [])
            )
        )

    def test_checkpoint_fingerprint_includes_identity_and_source_file(self) -> None:
        fingerprint = self.nodes[1973]
        self.assertEqual(fingerprint["type"], "H3NativeGenerationFingerprint")
        identity = self.links_to(1973, "identity_fingerprint")
        source = self.links_to(1973, "source_fingerprint")
        plan = self.links_to(1700, "generation_fingerprint")
        self.assertEqual((int(identity[0][1]), int(identity[0][2])), (1966, 1))
        self.assertEqual((int(source[0][1]), int(source[0][2])), (1960, 8))
        self.assertEqual((int(plan[0][1]), int(plan[0][2])), (1973, 0))

    def test_incompatible_loras_are_disconnected_and_disabled(self) -> None:
        lora_ids = {
            node_id for node_id, node in self.nodes.items()
            if node["type"] == "LoraLoaderModelOnly"
        }
        self.assertEqual(lora_ids, {1635, 1962})
        self.assertTrue(all(self.nodes[node_id]["mode"] == 2 for node_id in lora_ids))
        self.assertFalse(any(int(link[1]) in lora_ids or int(link[3]) in lora_ids for link in self.workflow["links"]))

    def test_review_is_visible_but_auto_run_is_default(self) -> None:
        self.assertEqual(self.nodes[1944]["type"], "MiniMaxH3ChainReview")
        self.assertFalse(self.nodes[1944]["widgets_values"][0])


if __name__ == "__main__":
    unittest.main()
