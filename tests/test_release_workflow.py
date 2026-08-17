from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "assets" / "workflows"
STABLE = WORKFLOWS / "h3-native-loop-final-stable-ui.json"
EXPERIMENTAL = WORKFLOWS / "h3-native-loop-final-turbo-experimental-ui.json"
USER_NORMAL = (
    WORKFLOWS / "h3-native-loop-user-final-no-audio-compatible-ui.json"
)
USER_LOW_VRAM = (
    WORKFLOWS
    / "h3-native-loop-user-final-no-audio-compatible-low-vram-ui.json"
)


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
        self.assertEqual(
            nodes[1700]["widgets_values"][1],
            "h3_native_loop_streamed_release",
        )

    def test_stable_release_streams_only_the_current_source_scene(self) -> None:
        nodes = self.nodes(self.stable)
        self.assertEqual(nodes[1951]["type"], "H3NativeLongVideoScene")
        self.assertEqual(
            nodes[1952]["widgets_values"],
            ["motion", "motion_audio", "restart_each_scene"],
        )
        self.assertEqual(nodes[1960]["outputs"][0]["type"], "H3_NATIVE_VIDEO_TIMELINE")
        self.assertFalse(
            any(output["type"] == "IMAGE" for output in nodes[1960]["outputs"])
        )
        self.assertEqual(nodes[1973]["type"], "H3NativeGenerationFingerprint")
        self.assertEqual(
            self.stable["extra"]["release"]["source_loading"],
            "streamed current-scene windows",
        )

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


class UserReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = {
            "normal": json.loads(USER_NORMAL.read_text(encoding="utf-8")),
            "low_vram": json.loads(USER_LOW_VRAM.read_text(encoding="utf-8")),
        }

    @staticmethod
    def nodes(workflow: dict) -> dict[int, dict]:
        return {int(node["id"]): node for node in workflow["nodes"]}

    def test_user_profiles_have_portable_inputs_and_no_private_run_names(self) -> None:
        expected = {
            1945: "character_front.png",
            1946: "character_side.png",
            1963: "character_back.png",
            1964: "character_face_closeup.png",
            1950: "source_video.mp4",
        }
        private_markers = ("01a005", "干什么", "9b1c0f6c", "_614f")
        for workflow in self.profiles.values():
            with self.subTest(variant=workflow["extra"]["release"]["variant"]):
                nodes = self.nodes(workflow)
                self.assertEqual(
                    {node_id: nodes[node_id]["widgets_values"][0] for node_id in expected},
                    expected,
                )
                serialized = json.dumps(workflow, ensure_ascii=False)
                self.assertFalse(any(marker in serialized for marker in private_markers))

    def test_user_profiles_preserve_active_lightx2v_route(self) -> None:
        for workflow in self.profiles.values():
            with self.subTest(variant=workflow["extra"]["release"]["variant"]):
                nodes = self.nodes(workflow)
                turbo = nodes[1972]
                self.assertEqual(turbo["mode"], 0)
                self.assertIn("ENABLED", turbo["title"])
                self.assertNotIn("DISABLED", turbo["title"])
                self.assertIn("lightx2v_turbo", turbo["widgets_values"][0])
                self.assertEqual(
                    nodes[1]["widgets_values"][0],
                    "minimax\\minimax_h3_ref2va_int8_convrot.safetensors",
                )
                self.assertTrue(
                    any(int(link[3]) == 1972 for link in workflow["links"])
                )
                self.assertTrue(
                    any(int(link[1]) == 1972 for link in workflow["links"])
                )
                self.assertEqual(
                    workflow["extra"]["release"]["turbo_lora"],
                    "enabled and connected user profile",
                )

    def test_user_profile_frame_caps_and_examples_match(self) -> None:
        expected = {
            "normal": (124, [124, 124, 124, 124, 124, 107]),
            "low_vram": (107, [107, 107, 107, 107, 107, 107, 107]),
        }
        for name, workflow in self.profiles.items():
            with self.subTest(profile=name):
                frame_cap, lengths = expected[name]
                nodes = self.nodes(workflow)
                plan = json.loads(nodes[1700]["widgets_values"][0])
                self.assertEqual(nodes[1960]["widgets_values"][1], frame_cap)
                self.assertEqual(nodes[110]["widgets_values"][3], frame_cap)
                self.assertEqual(
                    [int(shot["length"]) for shot in plan["shots"]], lengths
                )
                self.assertEqual(
                    workflow["extra"]["release"]["scene_frame_cap"], frame_cap
                )

    def test_user_profiles_keep_silent_audio_fallback_and_final_preview(self) -> None:
        for workflow in self.profiles.values():
            with self.subTest(variant=workflow["extra"]["release"]["variant"]):
                nodes = self.nodes(workflow)
                final = nodes[1961]
                inputs = {value["name"]: value for value in final["inputs"]}
                self.assertIsNotNone(inputs["source_frame_count"]["link"])
                self.assertIsNotNone(inputs["source_audio"]["link"])
                self.assertIn("PLAYABLE PREVIEW", final["title"])
                self.assertIn(
                    "silence fallback",
                    workflow["extra"]["release"]["missing_audio"],
                )


if __name__ == "__main__":
    unittest.main()
