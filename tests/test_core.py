from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_motion_phase_screen as phase  # noqa: E402
import inject_tail_taper as injector  # noqa: E402
import run_chain  # noqa: E402
import run_full_video  # noqa: E402


class JsonEncodingTests(unittest.TestCase):
    def test_utf8_json_round_trip_supports_non_ascii_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3_json_encoding_") as directory:
            path = Path(directory) / "config.json"
            payload = {"prompt": "半身特写 — 继续跳舞"}
            run_chain.write_json(path, payload)
            self.assertEqual(run_chain.load_json(path), payload)
            self.assertIn(payload["prompt"], path.read_bytes().decode("utf-8"))

    def test_http_error_includes_comfy_validation_body(self) -> None:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8188/prompt",
            400,
            "Bad Request",
            None,
            io.BytesIO(b'{"error":"bad model path"}'),
        )
        with mock.patch.object(run_chain.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(run_chain.ChainError, "bad model path"):
                run_chain.http_json("http://127.0.0.1:8188", "/prompt", {"prompt": {}})


class TaperTests(unittest.TestCase):
    def test_validated_three_frame_ramp(self) -> None:
        values = [
            injector.alpha_for(position, 22, 0.45, 0.10, 3)
            for position in range(19, 22)
        ]
        self.assertAlmostEqual(values[0], 0.45 + (0.10 - 0.45) / 3)
        self.assertAlmostEqual(values[1], 0.45 + 2 * (0.10 - 0.45) / 3)
        self.assertAlmostEqual(values[2], 0.10)

    def test_invalid_ramp_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            injector.validate(22, 0.45, 0.10, 23)
        with self.assertRaises(ValueError):
            injector.validate(22, 1.1, 0.10, 3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_injection_is_deterministic_and_cleans_temp_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="taper_test_") as directory:
            root = Path(directory)
            source = root / "source.mp4"
            first = root / "first.mp4"
            second = root / "second.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=gray:s=64x96:r=24:d=1", "-frames:v", "24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", source,
                ],
                check=True,
            )
            real_temporary_directory = tempfile.TemporaryDirectory
            owned_work_dirs: list[Path] = []

            def tracked_temporary_directory(*args: object, **kwargs: object) -> tempfile.TemporaryDirectory[str]:
                work = real_temporary_directory(prefix="injtaper_test_", dir=root)
                owned_work_dirs.append(Path(work.name))
                return work

            with mock.patch.object(
                injector.tempfile, "TemporaryDirectory", side_effect=tracked_temporary_directory
            ):
                injector.inject(source, first, 22, 0.45, 0.10, 3, seed=42)
                injector.inject(source, second, 22, 0.45, 0.10, 3, seed=42)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            self.assertTrue(owned_work_dirs)
            self.assertFalse(any(path.exists() for path in owned_work_dirs))


class PhaseTests(unittest.TestCase):
    def test_offset_sign_interpretation(self) -> None:
        source = [0, 1, 4, 2, 8, 3, 7, 5, 9, 6, 10, 8, 12, 7, 13, 9, 14, 10, 15, 11]
        lagging = [99, 98] + source[:-2]  # generated[t] corresponds to source[t-2]
        _, lag_offset, _ = phase.best_offset(lagging, source, 4, 16, 4)
        self.assertEqual(lag_offset, -2)
        self.assertEqual(phase.interpretation(lag_offset), "generation lags source")

        leading = source[2:] + [99, 98]  # generated[t] corresponds to source[t+2]
        _, lead_offset, _ = phase.best_offset(leading, source, 2, 14, 4)
        self.assertEqual(lead_offset, 2)
        self.assertEqual(phase.interpretation(lead_offset), "generation leads source")

    def test_runner_parses_negative_ncc(self) -> None:
        output = """\
022:055    -0.125      -2                0.010  generation lags source
055:089     0.456      +1                0.020  generation leads source
"""
        offsets, nccs = run_chain.parse_phase_output(output, 2)
        self.assertEqual(offsets, [-2, 1])
        self.assertEqual(nccs, [-0.125, 0.456])

    def test_short_real_tail_skips_phase_screen_and_gates(self) -> None:
        with mock.patch.object(run_chain, "parse_phase") as parse_phase:
            metrics, report = run_chain.measure_phase(
                Path("raw.mp4"), Path("source.mp4"), 22, 1, 12, 3
            )
        parse_phase.assert_not_called()
        self.assertTrue(metrics["phase_skipped"])
        self.assertEqual(metrics["phase_offsets"], [])
        self.assertIn("Inference-only padding was excluded", metrics["phase_skip_reason"])
        self.assertIn("phase_skipped=true", report)
        failures = run_chain.evaluate(
            {
                **metrics,
                "seam_diff": 0.0,
                "sharpness_ratio": 1.0,
                "source_rms_difference": 20.0,
            },
            {"max_abs_phase_offset": 0, "min_phase_ncc": 1.0},
        )
        self.assertEqual(failures, [])

    def test_phase_screen_stops_at_last_real_transition(self) -> None:
        with mock.patch.object(
            run_chain, "parse_phase", return_value=([0, 0, 0], [0.5, 0.6, 0.7], "report")
        ) as parse_phase:
            metrics, _ = run_chain.measure_phase(
                Path("raw.mp4"), Path("source.mp4"), 22, 13, 12, 3
            )
        parse_phase.assert_called_once_with(
            Path("raw.mp4"), Path("source.mp4"), 22, 35, 12, 3
        )
        self.assertFalse(metrics["phase_skipped"])


class SharpnessTests(unittest.TestCase):
    def test_source_window_matches_real_delivery_length(self) -> None:
        with (
            mock.patch.object(run_chain, "video_size", return_value=(64, 96)),
            mock.patch.object(run_chain, "frame_count", return_value=5),
            mock.patch.object(run_chain, "laplacian_variance", side_effect=[10.0, 8.0]) as variance,
        ):
            ratio = run_chain.sharpness_ratio(
                Path("delivery.mp4"), Path("padded_source.mp4"), 22
            )
        self.assertEqual(ratio, 0.8)
        self.assertEqual(variance.call_args_list, [
            mock.call(Path("padded_source.mp4"), 22, target_size=(64, 96), max_frames=5),
            mock.call(Path("delivery.mp4"), 0, target_size=(64, 96), max_frames=5),
        ])


class GateTests(unittest.TestCase):
    def test_source_copy_screen_can_halt(self) -> None:
        metrics = {
            "phase_offsets": [0, 0, 0],
            "phase_ncc": [1.0, 1.0, 1.0],
            "seam_diff": 0.01,
            "sharpness_ratio": 1.0,
            "source_rms_difference": 2.0,
        }
        failures = run_chain.evaluate(metrics, {"min_source_rms_difference": 8.0})
        self.assertEqual(len(failures), 1)
        self.assertIn("possible output==source", failures[0])


class WorkflowTests(unittest.TestCase):
    def test_patch_plan_supplies_current_loop_required_defaults(self) -> None:
        workflow = {
            "100": {
                "inputs": {
                    "plan_json": json.dumps({"shots": [{"id": "old", "prompt": "keep moving"}]})
                }
            }
        }
        run_chain.patch_plan(
            workflow,
            "100",
            segment_name="seg02",
            seed=42,
            raw_frames=124,
            steps=20,
            context_frames=22,
        )
        inputs = workflow["100"]["inputs"]
        self.assertEqual(inputs["continuation_mode"], "guide")
        self.assertEqual(inputs["video_blend_frames"], 0)

    def test_h3_templates_use_installed_minimax_model_subdirectory(self) -> None:
        workflows = [
            ROOT / "assets/workflows/t3-ref2va-initial-template.json",
            ROOT / "assets/workflows/t3-ref2va-context-taper-template.json",
        ]
        for path in workflows:
            with self.subTest(path=path.name):
                workflow = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(workflow["1"]["inputs"]["unet_name"].startswith("minimax\\"))
                self.assertTrue(workflow["2"]["inputs"]["clip_name"].startswith("minimax\\"))
                self.assertTrue(workflow["3"]["inputs"]["vae_name"].startswith("minimax\\"))
                self.assertTrue(workflow["4"]["inputs"]["vae_name"].startswith("minimax\\"))

    def test_api_workflows_only_contain_nodes(self) -> None:
        workflows = [
            ROOT / "assets/workflows/t3-ref2va-initial-template.json",
            ROOT / "assets/workflows/t3-ref2va-context-taper-template.json",
            ROOT / "assets/workflows/h3-chain-controller-template.json",
            ROOT / "assets/workflows/h3-full-video-controller-template.json",
            ROOT / "examples/zhu-yuan-poc-workflow.json",
        ]
        for path in workflows:
            with self.subTest(path=path.name):
                workflow = json.loads(path.read_text())
                for node_id, node in workflow.items():
                    self.assertIsInstance(node, dict, node_id)
                    self.assertIsInstance(node.get("class_type"), str, node_id)
                    self.assertIsInstance(node.get("inputs"), dict, node_id)

    def test_full_video_ui_workflow_has_controller_link(self) -> None:
        path = ROOT / "assets/workflows/h3-full-video-controller-ui.json"
        workflow = json.loads(path.read_text())
        self.assertEqual(workflow["version"], 0.4)
        self.assertEqual([node["type"] for node in workflow["nodes"]], [
            "H3FullVideoInputs", "H3FullVideoConfig", "H3FullVideoLaunch",
        ])
        self.assertEqual(workflow["links"], [
            [1, 1, 0, 2, 0, "STRING"],
            [2, 1, 1, 2, 1, "STRING"],
            [3, 2, 0, 3, 0, "STRING"],
        ])
        self.assertEqual(len(workflow["nodes"][0]["widgets_values"]), 5)
        self.assertEqual(len(workflow["nodes"][1]["widgets_values"]), 20)

    def test_one_click_ui_workflow_is_a_single_self_contained_controller(self) -> None:
        path = ROOT / "assets/workflows/h3-full-video-one-click-ui.json"
        workflow = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([node["type"] for node in workflow["nodes"]], ["H3FullVideoOneClick"])
        self.assertEqual(workflow["links"], [])
        self.assertEqual(len(workflow["nodes"][0]["widgets_values"]), 13)

    def test_diagnostic_workflow_embeds_generation_graphs_and_disables_loras(self) -> None:
        path = ROOT / "assets/workflows/h3-full-video-diagnostic-all-in-one-ui.json"
        workflow = json.loads(path.read_text(encoding="utf-8"))
        node_types = [node["type"] for node in workflow["nodes"]]
        self.assertIn("H3FullVideoInputs", node_types)
        self.assertIn("H3FullVideoConfig", node_types)
        self.assertIn("H3FullVideoLaunch", node_types)
        self.assertIn("H3FullVideoDiagnostics", node_types)
        embedded = workflow["extra"]["h3_embedded_workflows"]
        self.assertEqual(
            embedded["initial"],
            json.loads((ROOT / "assets/workflows/t3-ref2va-initial-template.json").read_text(encoding="utf-8")),
        )
        self.assertEqual(
            embedded["continuation"],
            json.loads((ROOT / "assets/workflows/t3-ref2va-context-taper-template.json").read_text(encoding="utf-8")),
        )
        loras = [node for node in workflow["nodes"] if node["type"] == "LoraLoaderModelOnly"]
        self.assertEqual(len(loras), 2)
        self.assertTrue(all(node["mode"] == 2 for node in loras))
        self.assertTrue(all("DISABLED" in node["title"] for node in loras))


class FullVideoPlanTests(unittest.TestCase):
    def test_full_video_entrypoint_imports_sibling_in_isolated_python(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / "scripts" / "run_full_video.py"),
                "--help",
            ],
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_first_segment(self) -> None:
        plan = run_full_video.plan_segments(124)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["unique_frames"], 124)
        self.assertEqual(plan[0]["inference_padding_frames"], 0)

    def test_one_frame_remainder_uses_inference_only_padding(self) -> None:
        plan = run_full_video.plan_segments(125)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[1]["source_start"], 102)
        self.assertEqual(plan[1]["source_available_frames"], 23)
        self.assertEqual(plan[1]["unique_frames"], 1)
        self.assertEqual(sum(int(item["unique_frames"]) for item in plan), 125)

    def test_multiple_segments_preserve_every_source_frame(self) -> None:
        plan = run_full_video.plan_segments(1000)
        self.assertEqual(sum(int(item["unique_frames"]) for item in plan), 1000)
        for item in plan[1:]:
            self.assertEqual(int(item["unique_start"]), int(item["source_start"]) + 22)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_inference_padding_and_final_collection_preserve_125_frames(self) -> None:
        with tempfile.TemporaryDirectory(prefix="h3_full_video_") as directory:
            root = Path(directory)
            source = root / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=64x96:rate=24", "-frames:v", "125",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", source,
                ],
                check=True,
            )
            plan = run_full_video.plan_segments(125)
            slices: list[Path] = []
            for item in plan:
                segment = root / f"{item['name']}.mp4"
                run_full_video.slice_with_inference_padding(
                    source,
                    segment,
                    int(item["source_start"]),
                    int(item["source_available_frames"]),
                    124,
                )
                self.assertEqual(run_chain.frame_count(segment), 124)
                slices.append(segment)

            first = root / "delivery_01.mp4"
            run_chain.trim_video_frames(slices[0], first, 124)
            last = root / "delivery_02.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", slices[1], "-vf",
                    "trim=start_frame=22:end_frame=23,setpts=PTS-STARTPTS",
                    "-frames:v", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", last,
                ],
                check=True,
            )
            assembled = root / "assembled.mp4"
            run_full_video.concat_deliveries([first, last], assembled)
            self.assertEqual(run_chain.frame_count(assembled), 125)


if __name__ == "__main__":
    unittest.main()
