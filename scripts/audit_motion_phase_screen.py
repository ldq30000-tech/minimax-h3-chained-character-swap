#!/usr/bin/env python3
"""Screen temporal phase drift from motion-energy signatures.

This is a candidate/reject screen, NOT a pose-certification metric: it compares
per-frame grayscale motion energy in a center person crop, so it is relatively
insensitive to character identity but cannot distinguish all choreography
aliases. Use it to flag candidates for human pose review; do not auto-accept a
candidate solely from this score.

Usage:
  audit_motion_phase_screen.py GEN.mp4 SOURCE.mp4 --start 22 --end 124 \
      --search 12 --segments 3

GEN[i] is nominally SOURCE[i]. A reported offset d compares GEN transitions
in [a,b) against SOURCE[a+d,b+d). Therefore d<0 means the generation is behind
(lagging) relative to the source, while d>0 means it is ahead. The source video
must cover the same nominal frame range as GEN.
"""
import argparse
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


def extract(video: str, directory: Path) -> list[Image.Image]:
    directory.mkdir(parents=True)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", video,
        "-vf", "scale=170:170:force_original_aspect_ratio=decrease",
        str(directory / "f_%05d.png"),
    ], check=True)
    images = []
    for path in sorted(directory.glob("f_*.png")):
        with Image.open(path) as opened:
            image = opened.convert("L")
        # Relative center crop preserves the original aspect ratio and works for
        # portrait or landscape, but still assumes the subject is near center.
        width, height = image.size
        images.append(image.crop((round(width * 0.18), round(height * 0.06),
                                  round(width * 0.82), round(height * 0.965))))
    return images


def normalized(image: Image.Image) -> Image.Image:
    mean = ImageStat.Stat(image).mean[0]
    return image.point(lambda value: min(255, max(0, int(value * 128 / max(mean, 1)))))


def signature(images: list[Image.Image]) -> list[float]:
    frames = [normalized(image) for image in images]
    values = []
    for left, right in zip(frames, frames[1:]):
        histogram = ImageChops.difference(left, right).histogram()
        count = sum(histogram)
        values.append(sum(value * number for value, number in enumerate(histogram)) / (255 * count))
    return values


def ncc(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 4:
        return float("nan")
    lm = sum(left) / len(left)
    rm = sum(right) / len(right)
    numerator = sum((a - lm) * (b - rm) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a - lm) ** 2 for a in left) * sum((b - rm) ** 2 for b in right))
    return numerator / denominator if denominator else float("nan")


def best_offset(gen: list[float], source: list[float], start: int, end: int, search: int) -> tuple[float, int | None, float]:
    candidates = []
    for offset in range(-search, search + 1):
        source_start = start + offset
        source_end = end + offset
        if source_start < 0 or source_end > len(source):
            continue
        score = ncc(gen[start:end], source[source_start:source_end])
        if not math.isnan(score):
            candidates.append((score, offset))
    candidates.sort(reverse=True)
    if not candidates:
        return float("nan"), None, float("nan")
    best_score, offset = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else float("nan")
    return best_score, offset, best_score - runner_up


def interpretation(offset: int) -> str:
    if offset == 0:
        return "nominal"
    return "generation lags source" if offset < 0 else "generation leads source"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("gen")
    parser.add_argument("source")
    parser.add_argument("--start", type=int, default=0, help="first generated frame to evaluate")
    parser.add_argument("--end", type=int, required=True, help="exclusive generated frame end")
    parser.add_argument(
        "--source-end",
        type=int,
        help="exclusive source transition end; excludes inference-only tail padding",
    )
    parser.add_argument("--search", type=int, default=12)
    parser.add_argument("--segments", type=int, default=3)
    args = parser.parse_args()
    if args.search < 0:
        parser.error("--search must be non-negative")
    if args.segments < 1:
        parser.error("--segments must be positive")
    work = Path(tempfile.mkdtemp(prefix="h3_motion_phase_"))
    try:
        generated = extract(args.gen, work / "generated")
        source = extract(args.source, work / "source")
        gen_sig, source_sig = signature(generated), signature(source)
        total_source_transitions = len(source_sig)
        if args.start < 0 or args.end > len(gen_sig) or args.end <= args.start:
            raise SystemExit(f"invalid transition range [{args.start}, {args.end}); generated has {len(gen_sig)} transitions")
        if args.source_end is not None:
            if args.source_end < 1 or args.source_end > len(source_sig):
                raise SystemExit(
                    f"invalid source transition end {args.source_end}; source has {len(source_sig)} transitions"
                )
            source_sig = source_sig[:args.source_end]
        print(f"generated_frames={len(generated)} source_frames={len(source)}")
        print(
            f"source_transitions_used={len(source_sig)} "
            f"source_transitions_total={total_source_transitions}"
        )
        print("metric=motion-energy NCC in center crop; screening only, not pose certification")
        print("segment      best_NCC  offset  margin_to_runner_up  interpretation")
        width = args.end - args.start
        for index in range(args.segments):
            begin = args.start + index * width // args.segments
            finish = args.start + (index + 1) * width // args.segments
            score, offset, margin = best_offset(gen_sig, source_sig, begin, finish, args.search)
            if offset is None:
                print(f"{begin:03d}:{finish:03d}  unavailable")
                continue
            print(
                f"{begin:03d}:{finish:03d}  {score:8.3f}  {offset:+6d}  "
                f"{margin:19.3f}  {interpretation(offset)}"
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
