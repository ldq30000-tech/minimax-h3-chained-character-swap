#!/usr/bin/env python3
"""Audit stitched videos without assuming output-frame == source-frame.

Reports brightness-normalized structural frame differences at the supplied actual
seam positions and near-duplicate transitions introduced elsewhere in the video.

Usage:
  audit_stitch.py video.mp4 --seams 123,247,371,495 [--freeze 0.001]

Frames are downscaled with their aspect ratio preserved (long edge 170 px).
"""
import argparse
import glob
import os
import shutil
import subprocess
import tempfile
from statistics import median
from PIL import Image, ImageChops, ImageStat


def extract(video: str, outdir: str) -> list[str]:
    os.makedirs(outdir)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video,
         "-vf", "scale=170:170:force_original_aspect_ratio=decrease",
         os.path.join(outdir, "f_%05d.png")],
        check=True,
    )
    return sorted(glob.glob(os.path.join(outdir, "f_*.png")))


def ndiff(a: str, b: str) -> float:
    ia = Image.open(a).convert("L")
    ib = Image.open(b).convert("L")
    ma = ImageStat.Stat(ia).mean[0]
    mb = ImageStat.Stat(ib).mean[0]
    ia = ia.point(lambda v: min(255, max(0, int(v * 128 / max(ma, 1)))))
    ib = ib.point(lambda v: min(255, max(0, int(v * 128 / max(mb, 1)))))
    hist = ImageChops.difference(ia, ib).histogram()
    count = sum(hist)
    return sum(value * n for value, n in enumerate(hist)) / (count * 255) if count else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--seams", required=True, help="Actual last-frame indices before each seam")
    parser.add_argument("--freeze", type=float, default=0.001)
    args = parser.parse_args()

    seams = [int(v) for v in args.seams.split(",") if v]
    work = tempfile.mkdtemp(prefix="h3_stitch_audit_")
    try:
        frames = extract(args.video, os.path.join(work, "frames"))
        if len(frames) < 2:
            raise SystemExit("video must contain at least two frames")
        diffs = [ndiff(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
        print(f"video={args.video}")
        print(f"frames={len(frames)} seams={seams}")
        print("seam  diff     local_median  ratio")
        for seam in seams:
            if seam < 0 or seam >= len(diffs):
                raise SystemExit(
                    f"invalid seam index {seam}; video has {len(frames)} frames "
                    f"and valid seam indices are 0..{max(len(diffs) - 1, 0)}"
                )
            neighbors = [
                diffs[i] for i in range(max(0, seam - 6), min(len(diffs), seam + 7))
                if i != seam
            ]
            local = median(neighbors) if neighbors else 0.0
            ratio = diffs[seam] / local if local else 0.0
            print(f"{seam:4d}  {diffs[seam]:.4f}   {local:.4f}       {ratio:.2f}x")
        freezes = [(i, value) for i, value in enumerate(diffs) if value < args.freeze]
        print(f"near_duplicate_transitions(<{args.freeze:.4f})={len(freezes)}")
        if freezes:
            print("positions=" + ",".join(f"{i}({value:.4f})" for i, value in freezes))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
