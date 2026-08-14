from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_motion_phase_screen as phase  # noqa: E402
import inject_tail_taper as injector  # noqa: E402
import run_chain  # noqa: E402


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
        before = set(Path(tempfile.gettempdir()).glob("injtaper_*"))
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
            injector.inject(source, first, 22, 0.45, 0.10, 3, seed=42)
            injector.inject(source, second, 22, 0.45, 0.10, 3, seed=42)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
        after = set(Path(tempfile.gettempdir()).glob("injtaper_*"))
        self.assertEqual(after, before)


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
    def test_api_workflows_only_contain_nodes(self) -> None:
        workflows = [
            ROOT / "assets/workflows/t3-ref2va-initial-template.json",
            ROOT / "assets/workflows/t3-ref2va-context-taper-template.json",
            ROOT / "examples/zhu-yuan-poc-workflow.json",
        ]
        for path in workflows:
            with self.subTest(path=path.name):
                workflow = json.loads(path.read_text())
                for node_id, node in workflow.items():
                    self.assertIsInstance(node, dict, node_id)
                    self.assertIsInstance(node.get("class_type"), str, node_id)
                    self.assertIsInstance(node.get("inputs"), dict, node_id)


if __name__ == "__main__":
    unittest.main()
