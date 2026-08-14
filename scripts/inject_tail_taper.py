#!/usr/bin/env python3
"""Inject deterministic blocky chroma noise into a video's tail frames.

The last ``tail_frames`` are injected. Earlier tail frames use ``alpha`` and
the final ``ramp_frames`` taper linearly to ``alpha_end``. Frames before the
tail are visually unchanged apart from the output video's normal re-encode.

Example:
  inject_tail_taper.py in.mp4 out.mp4 22 0.45 0.10 3 --seed 730002

For 22 / 0.45 / 0.10 / 3, the final three alphas are approximately
0.33 / 0.22 / 0.10.
"""
from __future__ import annotations

import argparse
import random
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

PALETTE = [
    (185, 115, 215),
    (115, 195, 140),
    (150, 148, 162),
    (205, 150, 192),
    (138, 182, 148),
    (160, 120, 175),
]
# Validated POC grid. At 576x1024 this produces 16x16 pixel blocks.
NOISE_GRID = (36, 64)


def alpha_for(position: int, tail_frames: int, alpha: float, alpha_end: float, ramp_frames: int) -> float:
    """Return injection alpha for a zero-based position inside the tail."""
    if not 0 <= position < tail_frames:
        raise ValueError("position must be inside the injected tail")
    from_end = tail_frames - 1 - position
    if from_end >= ramp_frames:
        return alpha
    return alpha + (alpha_end - alpha) * (ramp_frames - from_end) / ramp_frames


def validate(tail_frames: int, alpha: float, alpha_end: float, ramp_frames: int) -> None:
    if tail_frames < 1:
        raise ValueError("tail_frames must be positive")
    if ramp_frames < 1 or ramp_frames > tail_frames:
        raise ValueError("ramp_frames must be between 1 and tail_frames")
    if not 0.0 <= alpha <= 1.0 or not 0.0 <= alpha_end <= 1.0:
        raise ValueError("alpha and alpha_end must be between 0 and 1")
    if alpha_end > alpha:
        raise ValueError("alpha_end must not exceed alpha for a taper")


def probe_fps(video: Path) -> str:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", video,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fps = result.stdout.strip()
    if not fps:
        raise RuntimeError(f"could not read frame rate: {video}")
    return fps


def inject(
    source: Path,
    destination: Path,
    tail_frames: int,
    alpha: float,
    alpha_end: float,
    ramp_frames: int,
    seed: int,
) -> None:
    validate(tail_frames, alpha, alpha_end, ramp_frames)
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    with tempfile.TemporaryDirectory(prefix="injtaper_") as temp:
        frames_dir = Path(temp) / "frames"
        frames_dir.mkdir()
        pattern = frames_dir / "f_%04d.png"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", source, pattern],
            check=True,
        )
        files = sorted(frames_dir.glob("f_*.png"))
        if not files:
            raise RuntimeError(f"no video frames decoded from {source}")
        if tail_frames > len(files):
            raise ValueError(f"tail_frames={tail_frames} exceeds decoded frame count {len(files)}")

        start = len(files) - tail_frames
        for index, frame_path in enumerate(files[start:], start=start):
            position = index - start
            amount = alpha_for(position, tail_frames, alpha, alpha_end, ramp_frames)
            with Image.open(frame_path) as opened:
                frame = opened.convert("RGB")
            small = Image.new("RGB", NOISE_GRID)
            pixels = small.load()
            for y in range(NOISE_GRID[1]):
                for x in range(NOISE_GRID[0]):
                    pixels[x, y] = rng.choice(PALETTE)
            noisy = small.resize(frame.size, Image.Resampling.NEAREST)
            Image.blend(frame, noisy, amount).save(frame_path)
            if position >= tail_frames - ramp_frames:
                print(f"  tail frame {position}: alpha={amount:.3f}")

        print(
            f"injected source frames {start}..{len(files) - 1} "
            f"({tail_frames}f), taper last {ramp_frames}f {alpha}->{alpha_end}, seed={seed}"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-framerate", probe_fps(source),
                "-i", pattern, "-c:v", "libx264", "-crf", "12",
                "-pix_fmt", "yuv420p", "-an", destination,
            ],
            check=True,
        )
    print(f"output: {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("tail_frames", type=int)
    parser.add_argument("alpha", type=float)
    parser.add_argument("alpha_end", type=float)
    parser.add_argument("ramp_frames", type=int)
    parser.add_argument(
        "--seed", type=int, default=0,
        help="deterministic noise-pattern seed (default: 0)",
    )
    args = parser.parse_args()
    try:
        inject(
            args.source,
            args.destination,
            args.tail_frames,
            args.alpha,
            args.alpha_end,
            args.ramp_frames,
            args.seed,
        )
    except (FileNotFoundError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
