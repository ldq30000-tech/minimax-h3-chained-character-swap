from __future__ import annotations

import json
import importlib.util
import math
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nodes  # noqa: E402


class ComfyNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="h3_comfy_node_")
        self.base = Path(self.temp.name)
        self.input_dir = self.base / "input"
        self.output_dir = self.base / "output"
        self.input_dir.mkdir()
        self.output_dir.mkdir()
        self.workflow = self.base / "workflow.json"
        self.workflow.write_text("{}\n", encoding="utf-8")
        self.initial = self.base / "initial.mp4"
        self.initial.touch()
        self.reference = self.base / "reference.png"
        self.reference.touch()
        self.source = self.base / "source.mp4"
        self.source.touch()
        self.run_dir = self.base / "run"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build_config(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "workflow_path": str(self.workflow),
            "comfy_url": "http://127.0.0.1:8188",
            "comfy_input_dir": str(self.input_dir),
            "comfy_output_dir": str(self.output_dir),
            "run_dir": str(self.run_dir),
            "initial_delivery": str(self.initial),
            "reference_images_json": json.dumps([str(self.reference)]),
            "segments_json": json.dumps([{"name": "seg02", "source": str(self.source)}]),
            "nodes_json": '{"source_video":"43","context_video":"101","plan":"100","raw_output":"19","delivery_output":"108"}',
            "gates_json": "{}",
            "raw_frames": 124,
            "context_frames": 22,
            "width": 576,
            "height": 1024,
            "steps": 20,
            "seed_base": 730000,
            "timeout_seconds": 5400,
            "taper_alpha": 0.45,
            "taper_alpha_end": 0.10,
            "taper_ramp_frames": 3,
        }
        values.update(overrides)
        return nodes.H3ChainConfig().build(**values)[0]

    def test_config_is_repeatable_and_preserves_payload(self) -> None:
        first = Path(self.build_config())
        second = Path(self.build_config())
        self.assertEqual(first, second)
        config = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(config["context_frames"], 22)
        self.assertEqual(config["segments"][0]["name"], "seg02")

    def test_extension_entrypoint_registers_all_nodes(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "h3_chain_test_extension",
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(
            set(module.NODE_CLASS_MAPPINGS),
            {
                "H3ChainConfig", "H3ChainLaunch", "H3ChainStatus",
                "H3NativeGenerationFingerprint", "H3NativeLongVideoPrepare",
                "H3NativeLongVideoScene", "H3FinalTrimToSource",
                "H3FullVideoInputs",
                "H3FullVideoConfig", "H3FullVideoLaunch", "H3FullVideoStatus",
                "H3FullVideoOneClick",
                "H3FullVideoDiagnostics",
            },
        )

    def test_native_loop_plans_full_source_with_inference_only_padding(self) -> None:
        plan, padding = nodes._native_loop_plan(
            source_frames=614,
            prompt="subject_definitions:\n@motion owns motion.",
            raw_frames=124,
            context_frames=22,
            steps=20,
            base_seed=730000,
        )
        lengths = [shot["length"] for shot in plan["shots"]]
        self.assertEqual(lengths, [124, 124, 124, 124, 124, 107])
        self.assertEqual(padding, 3)
        delivered = lengths[0] + sum(length - 22 for length in lengths[1:])
        self.assertEqual(delivered, 617)
        self.assertEqual([shot["seed"] for shot in plan["shots"][:2]], ["730000", "730001"])

    def test_native_loop_avoids_undersized_final_scene(self) -> None:
        lengths, padding = nodes._native_loop_lengths(532, 124, 22)
        self.assertGreaterEqual(lengths[-1], 90)
        self.assertTrue(all((length - 5) % 17 == 0 for length in lengths))
        delivered = lengths[0] + sum(length - 22 for length in lengths[1:])
        self.assertEqual(delivered, 532 + padding)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "requires PyTorch")
    def test_native_loop_synthesizes_silence_when_source_has_no_audio(self) -> None:
        import torch

        source_video = mock.Mock()
        source_video.get_frame_count.return_value = 30
        source_video.get_frame_rate.return_value = 30.0
        source_video.get_duration.return_value = 1.0
        with (
            mock.patch.object(nodes, "_native_video_fingerprint", return_value="source"),
            mock.patch.object(nodes, "_decode_native_audio", return_value=None),
        ):
            result = nodes.H3NativeLongVideoPrepare().prepare(
                source_video=source_video,
                prompt="subject_definitions:\n@motion owns motion.",
                raw_frames=90,
                context_frames=22,
                steps=20,
                base_seed=730000,
            )

        timeline, inference_audio, source_audio = result[:3]
        source_frame_count, inference_frame_count = result[4:6]
        self.assertEqual((source_frame_count, inference_frame_count), (24, 39))
        self.assertEqual(timeline["format"], "h3_native_video_timeline_v2")
        self.assertIs(timeline["video"], source_video)
        self.assertNotIn("frames", timeline)
        self.assertEqual(source_audio["sample_rate"], 44100)
        self.assertEqual(source_audio["waveform"].shape[-1], 44100)
        self.assertEqual(source_audio["h3_audio_source"], "silence_fallback")
        self.assertEqual(inference_audio["waveform"].shape[-1], 71662)
        self.assertEqual(torch.count_nonzero(source_audio["waveform"]).item(), 0)
        self.assertIn("audio=missing -> 44.1 kHz mono silence", result[7])
        source_video.get_components.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("torch"), "requires PyTorch")
    def test_native_scene_decodes_only_the_exact_current_window(self) -> None:
        import torch

        source_video = mock.Mock()
        trimmed_video = mock.Mock()
        source_start = 68
        length = 90
        source_fps = 30.0
        source_indices = (
            torch.arange(source_start, source_start + length, dtype=torch.float64)
            * (source_fps / 24.0)
        ).floor().to(dtype=torch.long)
        first_source = int(source_indices[0])
        local_indices = source_indices - first_source
        decoded_count = int(local_indices.max()) + 1
        decoded = torch.arange(decoded_count, dtype=torch.float32).reshape(
            decoded_count, 1, 1, 1
        ).repeat(1, 2, 2, 3)
        trimmed_video.get_components.return_value = SimpleNamespace(images=decoded)
        source_video.as_trimmed.return_value = trimmed_video
        timeline = {
            "format": "h3_native_video_timeline_v2",
            "video": source_video,
            "source_fps": source_fps,
            "decoded_frame_count": 300,
            "source_frame_count": 240,
            "inference_frame_count": 243,
        }
        state = {
            "index": 2,
            "plan": {
                "shots": [
                    {"generation_start_frame": 0, "raw_frames": 90},
                    {
                        "generation_start_frame": source_start,
                        "raw_frames": length,
                    },
                ]
            },
        }
        inference_audio = {
            "waveform": torch.zeros((1, 1, 500000), dtype=torch.float32),
            "sample_rate": 44100,
        }

        frames, scene_audio, actual_start, status = (
            nodes.H3NativeLongVideoScene().decode(
                timeline, state, inference_audio
            )
        )

        self.assertEqual(actual_start, source_start)
        self.assertEqual(tuple(frames.shape), (length, 2, 2, 3))
        self.assertTrue(
            torch.equal(frames[:, 0, 0, 0], local_indices.to(torch.float32))
        )
        sample_start = round(source_start / 24.0 * 44100)
        sample_end = round((source_start + length) / 24.0 * 44100)
        self.assertEqual(scene_audio["waveform"].shape[-1], sample_end - sample_start)
        self.assertIn("scene 2/2", status)
        source_video.get_components.assert_not_called()
        source_video.as_trimmed.assert_called_once()

    def test_native_generation_fingerprint_combines_identity_and_source(self) -> None:
        first = nodes.H3NativeGenerationFingerprint().combine("identity", "source")
        second = nodes.H3NativeGenerationFingerprint().combine("identity", "source")
        changed = nodes.H3NativeGenerationFingerprint().combine("identity", "other")
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[0], changed[0])

    def test_final_video_preview_uses_comfy_output_relative_path(self) -> None:
        final = self.output_dir / "h3_chains" / "run" / "final" / "result.mp4"
        final.parent.mkdir(parents=True)
        final.touch()
        fake_folder_paths = mock.Mock()
        fake_folder_paths.get_output_directory.return_value = str(self.output_dir)
        with mock.patch.object(nodes, "folder_paths", fake_folder_paths):
            preview = nodes._video_preview_item(final)
        self.assertEqual(
            preview,
            {
                "filename": "result.mp4",
                "subfolder": "h3_chains/run/final",
                "type": "output",
            },
        )

    def test_full_video_inputs_builds_absolute_media_payload(self) -> None:
        source, references_json = nodes.H3FullVideoInputs().build(
            str(self.source),
            str(self.reference),
            str(self.reference),
            str(self.reference),
            str(self.reference),
        )
        self.assertEqual(source, str(self.source.resolve()))
        self.assertEqual(
            json.loads(references_json),
            [str(self.reference.resolve())] * 4,
        )

    def test_full_video_config_uses_source_instead_of_manual_segments(self) -> None:
        config_path = nodes.H3FullVideoConfig().build(
            source_video=str(self.source),
            initial_workflow_path=str(self.workflow),
            continuation_workflow_path=str(self.workflow),
            comfy_url="http://127.0.0.1:8188",
            comfy_input_dir=str(self.input_dir),
            comfy_output_dir=str(self.output_dir),
            run_dir=str(self.run_dir),
            reference_images_json=json.dumps([str(self.reference)]),
            prompt="",
            initial_nodes_json='{"source_video":"43","ref2va":"20","noise":"12","scheduler":"14","output":"19"}',
            continuation_nodes_json='{"source_video":"43","context_video":"101","plan":"100","raw_output":"19","delivery_output":"108"}',
            gates_json="{}",
            raw_frames=124,
            context_frames=22,
            width=576,
            height=1024,
            steps=20,
            seed_base=730000,
            timeout_seconds=5400,
            taper_alpha=0.45,
            taper_alpha_end=0.10,
            taper_ramp_frames=3,
        )[0]
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "full_video")
        self.assertEqual(payload["source_video"], str(self.source.resolve()))
        self.assertNotIn("segments", payload)

    def test_full_video_config_can_materialize_embedded_workflows(self) -> None:
        embedded = {
            "workflow": {
                "extra": {
                    "h3_embedded_workflows": {
                        "initial": {"1": {"class_type": "TestInitial", "inputs": {}}},
                        "continuation": {"2": {"class_type": "TestContinuation", "inputs": {}}},
                    }
                }
            }
        }
        config_path = nodes.H3FullVideoConfig().build(
            source_video=str(self.source),
            initial_workflow_path=str(self.base / "missing-initial.json"),
            continuation_workflow_path=str(self.base / "missing-continuation.json"),
            comfy_url="http://127.0.0.1:8188",
            comfy_input_dir=str(self.input_dir),
            comfy_output_dir=str(self.output_dir),
            run_dir=str(self.run_dir),
            reference_images_json=json.dumps([str(self.reference)]),
            prompt="",
            initial_nodes_json='{"source_video":"43","ref2va":"20","noise":"12","scheduler":"14","output":"19"}',
            continuation_nodes_json='{"source_video":"43","context_video":"101","plan":"100","raw_output":"19","delivery_output":"108"}',
            gates_json="{}",
            raw_frames=124,
            context_frames=22,
            width=576,
            height=1024,
            steps=20,
            seed_base=730000,
            timeout_seconds=5400,
            taper_alpha=0.45,
            taper_alpha_end=0.10,
            taper_ramp_frames=3,
            extra_pnginfo=embedded,
        )[0]
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.assertEqual(Path(config["initial_workflow"]).name, "initial.json")
        self.assertEqual(Path(config["continuation_workflow"]).name, "continuation.json")
        self.assertTrue(Path(config["initial_workflow"]).is_file())

    def test_changed_config_is_rejected_after_state_exists(self) -> None:
        self.build_config()
        (self.run_dir / "STATE.json").write_text('{"status":"running"}\n', encoding="utf-8")
        with self.assertRaises(nodes.H3ChainNodeError):
            self.build_config(width=1024)

    def test_launch_reports_existing_lock_without_spawning(self) -> None:
        config_path = self.build_config()
        self.run_dir.mkdir(exist_ok=True)
        (self.run_dir / ".h3_chain_controller.lock").write_text("{}\n", encoding="utf-8")
        result = nodes.H3ChainLaunch().launch(config_path, "start")
        self.assertEqual(result[1], "already_running")

    def test_launch_nodes_are_never_cached(self) -> None:
        self.assertTrue(math.isnan(nodes.H3ChainLaunch.IS_CHANGED()))
        self.assertTrue(math.isnan(nodes.H3FullVideoLaunch.IS_CHANGED()))
        self.assertTrue(math.isnan(nodes.H3FullVideoOneClick.IS_CHANGED()))

    def test_one_click_builds_and_launches_full_video_config(self) -> None:
        fake_folder_paths = mock.Mock()
        fake_folder_paths.get_input_directory.return_value = str(self.input_dir)
        fake_folder_paths.get_output_directory.return_value = str(self.output_dir)
        expected = (str(self.run_dir), "started", str(self.run_dir / "STATE.json"))
        with (
            mock.patch.object(nodes, "folder_paths", fake_folder_paths),
            mock.patch.object(nodes, "_launch_controller", return_value=expected) as launch,
        ):
            result = nodes.H3FullVideoOneClick().run(
                source_video=str(self.source),
                character_front=str(self.reference),
                character_side=str(self.reference),
                character_back=str(self.reference),
                character_face_closeup=str(self.reference),
                prompt="",
                comfy_url="http://127.0.0.1:8188",
                run_dir=str(self.run_dir),
                action="start",
                width=576,
                height=1024,
                steps=20,
                seed_base=730000,
            )
        self.assertEqual(result, expected)
        launch.assert_called_once()
        config = json.loads((self.run_dir / "full-video-config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["source_video"], str(self.source.resolve()))
        self.assertEqual(len(config["reference_images"]), 4)
        self.assertEqual(config["context_frames"], 22)

    def test_launch_starts_worker_without_blocking_the_graph(self) -> None:
        fake_runner = self.base / "fake_runner.py"
        fake_runner.write_text("raise SystemExit(0)\n", encoding="utf-8")
        config_path = self.build_config()
        with mock.patch.object(nodes, "RUNNER", fake_runner):
            result = nodes.H3ChainLaunch().launch(config_path, "start")
        self.assertEqual(result[1], "started")
        result_path = self.run_dir / "controller-result.json"
        for _ in range(40):
            if result_path.exists():
                break
            time.sleep(0.05)
        self.assertTrue(result_path.exists())
        for _ in range(40):
            nodes._reap_controllers()
            if not nodes.CONTROLLERS:
                break
            time.sleep(0.05)
        self.assertFalse((self.run_dir / ".h3_chain_controller.lock").exists())
        self.assertFalse(nodes.CONTROLLERS)
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8"))["meaning"], "completed")

    def test_status_reads_halted_state(self) -> None:
        state = self.base / "STATE.json"
        state.write_text(
            json.dumps({"status": "needs_agent_review", "completed_segments": [{"name": "seg02"}], "halt": {"segment": "seg02"}}),
            encoding="utf-8",
        )
        status, count, halt = nodes.H3ChainStatus().read(str(state))
        self.assertEqual(status, "needs_agent_review")
        self.assertEqual(count, 1)
        self.assertIn("seg02", halt)

    def test_full_video_status_returns_final_path(self) -> None:
        state = self.base / "FULL_STATE.json"
        final = self.base / "final.mp4"
        state.write_text(
            json.dumps({
                "status": "completed",
                "completed_segment_count": 4,
                "total_segments": 4,
                "final_video": str(final),
            }),
            encoding="utf-8",
        )
        status, completed, total, final_path, halt = nodes.H3FullVideoStatus().read(str(state))
        self.assertEqual((status, completed, total), ("completed", 4, 4))
        self.assertEqual(final_path, str(final))
        self.assertEqual(halt, "")

    def test_diagnostics_surfaces_latest_background_error(self) -> None:
        state = self.run_dir / "STATE.json"
        self.run_dir.mkdir()
        state.write_text(
            json.dumps({"status": "continuation", "completed_segment_count": 1, "total_segments": 6}),
            encoding="utf-8",
        )
        (self.run_dir / "controller-result.json").write_text(
            json.dumps({"exit_code": 1, "meaning": "failed"}), encoding="utf-8"
        )
        (self.run_dir / "controller.log").write_text(
            "old line\nERROR: Required input is missing: continuation_mode\n", encoding="utf-8"
        )
        stage, active, completed, total, problem, final = nodes.H3FullVideoDiagnostics().read(str(state))
        self.assertEqual((stage, active, completed, total, final), ("failed", "", 1, 6, ""))
        self.assertIn("continuation_mode", problem)

    def test_worker_releases_lock_and_records_review_exit(self) -> None:
        runner = self.base / "fake_runner.py"
        runner.write_text("raise SystemExit(2)\n", encoding="utf-8")
        config = self.base / "config.json"
        config.write_text("{}\n", encoding="utf-8")
        lock = self.base / ".h3_chain_controller.lock"
        lock.write_text("{}\n", encoding="utf-8")
        log = self.base / "controller.log"
        worker = subprocess.run(
            [
                sys.executable,
                str(ROOT / "comfy_worker.py"),
                "--runner", str(runner),
                "--config", str(config),
                "--lock", str(lock),
                "--log", str(log),
            ],
            check=False,
        )
        self.assertEqual(worker.returncode, 2)
        self.assertFalse(lock.exists())
        result = json.loads((self.base / "controller-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["meaning"], "needs_agent_review")
