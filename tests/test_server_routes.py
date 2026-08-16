from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server_routes


class _FolderPaths:
    def __init__(self, output: Path) -> None:
        self.output = output

    def get_output_directory(self) -> str:
        return str(self.output)


class FinalPreviewRouteTests(unittest.TestCase):
    def test_route_registration_skips_uninitialized_prompt_server(self) -> None:
        prompt_server = type("PromptServer", (), {})
        with (
            mock.patch.object(server_routes, "PromptServer", prompt_server),
            mock.patch.object(server_routes, "web", object()),
            mock.patch.object(server_routes, "_ROUTES_REGISTERED", False),
        ):
            server_routes.register_routes()

        self.assertFalse(server_routes._ROUTES_REGISTERED)

    def test_latest_final_video_uses_prefix_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            first = output / "h3_chains" / "run_a" / "final" / "result_001.mp4"
            latest = output / "h3_chains" / "run_b" / "final" / "result_002.mp4"
            ignored = output / "h3_chains" / "run_b" / "final" / "other.mp4"
            for path in (first, latest, ignored):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
            os.utime(first, ns=(1_000_000_000, 1_000_000_000))
            os.utime(latest, ns=(2_000_000_000, 2_000_000_000))
            os.utime(ignored, ns=(3_000_000_000, 3_000_000_000))

            with mock.patch.object(
                server_routes, "folder_paths", _FolderPaths(output)
            ):
                found = server_routes._latest_final_video("result_")

            self.assertIsNotNone(found)
            self.assertEqual(found[0], latest)
            self.assertEqual(found[1], output.resolve())

    def test_preview_item_is_relative_to_comfy_output(self) -> None:
        output = Path("C:/ComfyUI/output")
        video = output / "h3_chains" / "run" / "final" / "final.mp4"
        self.assertEqual(
            server_routes._preview_item(video, output),
            {
                "filename": "final.mp4",
                "subfolder": "h3_chains/run/final",
                "type": "output",
            },
        )


if __name__ == "__main__":
    unittest.main()
