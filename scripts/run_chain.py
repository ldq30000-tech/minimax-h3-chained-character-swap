#!/usr/bin/env python3
"""Run a guarded MiniMax H3 Motion Context + T3 taper continuation chain.

This runner intentionally stops on a numeric QA gate and writes STATE.json for
an agent/human to inspect. It never silently rerolls or accepts a failed
segment. It communicates with a local/mounted ComfyUI instance through its
native HTTP API, so it requires access to that instance's input and output
folders.

Typical use:
  python3 scripts/run_chain.py examples/chain-config.example.json
  # inspect run/STATE.json and artifacts
  python3 scripts/run_chain.py examples/chain-config.example.json --approve-latest

The first clean segment must already exist. Every continuation source slice is
exactly one H3 raw segment (normally 124 frames) and includes the source-side
context overlap expected by the Motion Context workflow.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageStat

ROOT = Path(__file__).resolve().parent.parent
INJECT = ROOT / "scripts" / "inject_tail_taper.py"
PHASE_AUDIT = ROOT / "scripts" / "audit_motion_phase_screen.py"


class ChainError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except Exception as exc:
        raise ChainError(f"Cannot parse JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ChainError(f"Expected an object in {path}")
    return value


def validate_api_workflow(workflow: dict[str, Any], path: Path) -> None:
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            raise ChainError(f"Workflow {path} entry {node_id!r} is not a node object")
        if not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), dict):
            raise ChainError(f"Workflow {path} node {node_id!r} is missing class_type or inputs")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def resolve(base: Path, value: str) -> Path:
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else (base / path).resolve()


def run(cmd: list[str | Path], timeout: int = 900, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(x) for x in cmd], check=True, timeout=timeout, text=True,
                          capture_output=capture)


def frame_count(path: Path) -> int:
    result = run([
        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path,
    ])
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise ChainError(f"Cannot read video frame count: {path}") from exc


def video_size(path: Path) -> tuple[int, int]:
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height", "-of", "csv=p=0", path,
    ])
    try:
        width, height = result.stdout.strip().split(",")
        return int(width), int(height)
    except Exception as exc:
        raise ChainError(f"Cannot read video size: {path}") from exc


def get_fps(path: Path) -> str:
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=r_frame_rate", "-of", "csv=p=0", path,
    ])
    fps = result.stdout.strip()
    if not fps:
        raise ChainError(f"Cannot read video fps: {path}")
    return fps


def fps_value(path: Path) -> float:
    try:
        return float(Fraction(get_fps(path)))
    except (ValueError, ZeroDivisionError) as exc:
        raise ChainError(f"Cannot parse video fps: {path}") from exc


def require_fps(path: Path, expected: float = 24.0, tolerance: float = 0.001) -> None:
    actual = fps_value(path)
    if abs(actual - expected) > tolerance:
        raise ChainError(
            f"{path} is {actual:.6g} fps; this workflow requires {expected:g} fps. "
            f"Resample before slicing (for example: ffmpeg -i INPUT -vf fps=24 ...)."
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ffmpeg_tail(source: Path, destination: Path, frames: int) -> None:
    count = frame_count(source)
    if count < frames:
        raise ChainError(f"{source} has {count} frames; needs at least {frames} for context")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", source, "-vf",
        f"select='between(n\\,{count - frames}\\,{count - 1})',setpts=PTS-STARTPTS",
        "-fps_mode", "cfr", "-r", get_fps(source), "-c:v", "libx264", "-crf", "16",
        "-pix_fmt", "yuv420p", "-an", destination,
    ])
    if frame_count(destination) != frames:
        raise ChainError(f"Tail extraction produced wrong frame count: {destination}")


def image_at(video: Path, index: int, mode: str = "L", size: tuple[int, int] = (96, 170)) -> Image.Image:
    command = [
        "ffmpeg", "-v", "error", "-i", str(video), "-vf", f"select='eq(n\\,{index})'",
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE)
    import io
    with Image.open(io.BytesIO(result.stdout)) as opened:
        return opened.convert(mode).resize(size, Image.Resampling.LANCZOS)


def normalized_diff(left: Image.Image, right: Image.Image) -> float:
    lm = ImageStat.Stat(left).mean[0]
    rm = ImageStat.Stat(right).mean[0]
    left = left.point(lambda value: min(255, max(0, int(value * 128 / max(lm, 1)))))
    right = right.point(lambda value: min(255, max(0, int(value * 128 / max(rm, 1)))))
    hist = ImageChops.difference(left, right).histogram()
    count = sum(hist)
    return sum(value * n for value, n in enumerate(hist)) / (255 * count) if count else 0.0


def seam_diff(previous: Path, delivered: Path) -> float:
    return normalized_diff(image_at(previous, frame_count(previous) - 1), image_at(delivered, 0))


def laplacian_variance(
    video: Path,
    skip: int,
    every: int = 4,
    target_size: tuple[int, int] | None = None,
) -> float:
    width, height = target_size or video_size(video)
    frame_bytes = width * height
    lap = ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1)
    filters = "format=gray"
    if target_size is not None:
        filters = f"scale={width}:{height}:flags=lanczos,format=gray"
    process = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vf", filters, "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    values: list[float] = []
    index = 0
    try:
        while True:
            block = process.stdout.read(frame_bytes)
            if len(block) < frame_bytes:
                break
            if index >= skip and (index - skip) % every == 0:
                image = Image.frombytes("L", (width, height), block)
                values.append(ImageStat.Stat(image.filter(lap)).var[0])
            index += 1
    finally:
        process.stdout.close()
        process.wait(timeout=30)
    if not values:
        raise ChainError(f"No frames available for sharpness measurement: {video}")
    return sum(values) / len(values)


def sharpness_ratio(delivered: Path, source: Path, context_frames: int) -> float:
    # Delivered frame 0 corresponds to source frame context_frames in the overlapping source slice.
    # Laplacian variance is resolution-sensitive, so compare at the delivered resolution.
    size = video_size(delivered)
    denominator = laplacian_variance(source, context_frames, target_size=size)
    if denominator <= 0:
        raise ChainError(f"Source sharpness is zero; cannot compute ratio: {source}")
    return laplacian_variance(delivered, 0, target_size=size) / denominator


def source_rms_difference(delivered: Path, source: Path, context_frames: int, samples: int = 3) -> float:
    """Screen for the known failure where output is effectively the unchanged source."""
    delivered_count = frame_count(delivered)
    if delivered_count < 1:
        raise ChainError(f"No frames available for source-difference screen: {delivered}")
    sample_indices = sorted({round(i * (delivered_count - 1) / max(samples - 1, 1)) for i in range(samples)})
    values: list[float] = []
    for index in sample_indices:
        generated = image_at(delivered, index, mode="RGB")
        original = image_at(source, context_frames + index, mode="RGB")
        rms = ImageStat.Stat(ImageChops.difference(generated, original)).rms
        values.append(sum(rms) / len(rms))
    return sum(values) / len(values)


def http_json(url: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if payload is None:
            request = urllib.request.Request(f"{url.rstrip('/')}{endpoint}")
        else:
            request = urllib.request.Request(
                f"{url.rstrip('/')}{endpoint}", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
        with urllib.request.urlopen(request, timeout=60) as response:
            value = json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise ChainError(f"ComfyUI request {endpoint} failed: {exc}") from exc
    if not isinstance(value, dict):
        raise ChainError(f"ComfyUI request {endpoint} returned a non-object")
    return value


def output_item_path(output_root: Path, item: dict[str, Any]) -> Path:
    filename = item.get("filename")
    subfolder = item.get("subfolder", "")
    if not isinstance(filename, str) or not filename:
        raise ChainError(f"Invalid ComfyUI output item: {item}")
    candidate = (output_root / str(subfolder) / filename).resolve()
    root = output_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ChainError(f"Refusing output path outside configured output root: {candidate}")
    return candidate


def first_history_video(history: dict[str, Any], prompt_id: str, node_id: str, output_root: Path) -> Path:
    record = history.get(prompt_id)
    if not isinstance(record, dict):
        raise ChainError(f"ComfyUI history has no record for {prompt_id}")
    outputs = record.get("outputs", {})
    if not isinstance(outputs, dict) or not isinstance(outputs.get(node_id), dict):
        raise ChainError(f"ComfyUI history has no output for node {node_id}")
    node_outputs = outputs[node_id]
    for items in node_outputs.values():
        if not isinstance(items, list):
            continue
        for item in reversed(items):
            if isinstance(item, dict):
                path = output_item_path(output_root, item)
                if path.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv", ".avi"}:
                    for _ in range(12):
                        if path.exists() and path.stat().st_size > 0:
                            return path
                        time.sleep(2)
    raise ChainError(f"No local video output found for node {node_id}; configure comfy_output_dir correctly")


def wait_for_job(comfy_url: str, prompt_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        history = http_json(comfy_url, f"/history/{prompt_id}")
        record = history.get(prompt_id)
        if isinstance(record, dict):
            status = record.get("status", {})
            status_text = status.get("status_str") if isinstance(status, dict) else ""
            if status_text == "success":
                return history
            if status_text == "error":
                raise ChainError(f"ComfyUI job {prompt_id} failed: {json.dumps(status, ensure_ascii=False)}")
        time.sleep(15)
    raise ChainError(f"Timed out waiting for ComfyUI job {prompt_id}")


def parse_phase_output(output: str, expected_segments: int) -> tuple[list[int], list[float]]:
    rows = re.findall(r"^\d+:\d+\s+(-?(?:\d+(?:\.\d*)?|\.\d+))\s+([+-]\d+)", output, flags=re.M)
    if len(rows) != expected_segments:
        raise ChainError(
            f"Phase audit produced {len(rows)} parseable rows; expected {expected_segments}. "
            "Inspect phase_screen.txt/output for unavailable or NaN ranges."
        )
    return [int(offset) for _, offset in rows], [float(ncc) for ncc, _ in rows]


def parse_phase(raw: Path, source: Path, context_frames: int, raw_frames: int, search: int, segments: int) -> tuple[list[int], list[float], str]:
    result = run([
        sys.executable, PHASE_AUDIT, raw, source,
        "--start", str(context_frames), "--end", str(raw_frames - 1),
        "--search", str(search), "--segments", str(segments),
    ], timeout=900)
    offsets, nccs = parse_phase_output(result.stdout, segments)
    return offsets, nccs, result.stdout


def unique_name(run_id: str, label: str, original: Path) -> str:
    digest = hashlib.sha1(str(original.resolve()).encode()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", original.name)
    return f"{run_id}_{label}_{digest}_{safe}"


def copy_to_input(source: Path, input_dir: Path, target_name: str) -> str:
    if not source.is_file():
        raise ChainError(f"Required input file does not exist: {source}")
    target = input_dir / target_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target.name


def patch_plan(workflow: dict[str, Any], node_id: str, *, segment_name: str, seed: int, raw_frames: int, steps: int | None, context_frames: int | None = None, prompt: str | None = None, width: int | None = None, height: int | None = None) -> None:
    node = workflow.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise ChainError(f"plan_node {node_id} not found or has no inputs")
    inputs = node["inputs"]
    encoded = inputs.get("plan_json")
    if not isinstance(encoded, str):
        raise ChainError(f"plan_node {node_id} has no string plan_json")
    plan = json.loads(encoded)
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots or not isinstance(shots[0], dict):
        raise ChainError("plan_json must contain a non-empty shots list")
    shot = shots[0]
    shot["id"] = segment_name
    shot["seed"] = seed
    shot["length"] = raw_frames
    if prompt is not None:
        shot["prompt"] = prompt
    if steps is not None:
        shot["steps"] = steps
    inputs["plan_json"] = json.dumps(plan, ensure_ascii=False)
    inputs["run_name"] = segment_name
    inputs["generation_fingerprint"] = f"h3-context-taper-runner|{segment_name}|seed={seed}"
    if width is not None:
        inputs["width"] = width
    if height is not None:
        inputs["height"] = height
    if context_frames is not None:
        # Keep the workflow's context_length in sync with the configured value;
        # otherwise trim/phase accounting silently diverges from the chain plan.
        inputs["context_length"] = context_frames


def evaluate(metrics: dict[str, Any], gates: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    max_offset = gates.get("max_abs_phase_offset")
    if max_offset is not None and max(abs(value) for value in metrics["phase_offsets"]) > int(max_offset):
        failures.append(f"max_abs_phase_offset>{max_offset}: {metrics['phase_offsets']}")
    min_ncc = gates.get("min_phase_ncc")
    if min_ncc is not None and min(metrics["phase_ncc"]) < float(min_ncc):
        failures.append(f"min_phase_ncc<{min_ncc}: {metrics['phase_ncc']}")
    max_seam = gates.get("max_seam_diff")
    if max_seam is not None and metrics["seam_diff"] > float(max_seam):
        failures.append(f"seam_diff>{max_seam}: {metrics['seam_diff']:.4f}")
    min_sharpness = gates.get("min_sharpness_ratio")
    if min_sharpness is not None and metrics["sharpness_ratio"] < float(min_sharpness):
        failures.append(f"sharpness_ratio<{min_sharpness}: {metrics['sharpness_ratio']:.3f}")
    min_source_difference = gates.get("min_source_rms_difference")
    if min_source_difference is not None and metrics["source_rms_difference"] < float(min_source_difference):
        failures.append(
            f"source_rms_difference<{min_source_difference}: "
            f"{metrics['source_rms_difference']:.3f} (possible output==source / failed identity replacement)"
        )
    return failures


def ensure_number(value: Any, label: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise ChainError(f"{label} must be a positive integer")
    return value


def prepare_static_inputs(config: dict[str, Any], base: Path, input_dir: Path, run_id: str, workflow: dict[str, Any]) -> None:
    references = config.get("reference_images", {})
    if isinstance(references, dict):
        # Explicit form: map existing LoadImage node ids to image paths.
        for node_id, value in references.items():
            node = workflow.get(str(node_id))
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ChainError(f"reference LoadImage node {node_id} not found")
            image = resolve(base, str(value))
            node["inputs"]["image"] = copy_to_input(image, input_dir, unique_name(run_id, f"ref_{node_id}", image))
        return
    if not isinstance(references, list) or not references:
        raise ChainError("reference_images must be a node-id map or a non-empty list of image paths")
    if len(references) > 9:
        raise ChainError("MiniMaxH3ReferenceToVideo accepts at most 9 reference images")
    # Dynamic form: locate the single Ref2VA node, drop its current ref_image
    # wiring (and orphaned LoadImage nodes), then create and wire one LoadImage
    # node per configured image.
    targets = [nid for nid, node in workflow.items()
               if isinstance(node, dict) and node.get("class_type") == "MiniMaxH3ReferenceToVideo"]
    if len(targets) != 1:
        raise ChainError(f"Workflow must contain exactly one MiniMaxH3ReferenceToVideo node, found {len(targets)}")
    inputs = workflow[targets[0]].setdefault("inputs", {})
    if not isinstance(inputs, dict):
        raise ChainError("MiniMaxH3ReferenceToVideo node has malformed inputs")
    orphaned: list[str] = []
    for key in [k for k in inputs if k.startswith("ref_images.ref_image_")]:
        link = inputs.pop(key)
        if isinstance(link, list) and link:
            orphaned.append(str(link[0]))
    for nid in orphaned:
        node = workflow.get(nid)
        if isinstance(node, dict) and node.get("class_type") == "LoadImage":
            workflow.pop(nid)
    next_id = max([int(k) for k in workflow if str(k).isdigit()] + [0]) + 1
    for position, value in enumerate(references):
        image = resolve(base, str(value))
        name = copy_to_input(image, input_dir, unique_name(run_id, f"ref_{position}", image))
        nid = str(next_id + position)
        workflow[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        inputs[f"ref_images.ref_image_{position}"] = [nid, 0]


def mux_audio(delivery: Path, audio_source: Path, offset_seconds: float, duration_seconds: float, destination: Path) -> None:
    run([
        "ffmpeg", "-y", "-v", "error",
        "-i", delivery,
        "-ss", f"{offset_seconds:.6f}", "-i", audio_source,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration_seconds:.6f}", "-movflags", "+faststart", destination,
    ])


def run_chain(config_path: Path, approve_latest: bool) -> int:
    base = config_path.parent.resolve()
    config = load_json(config_path)
    required = ["workflow", "comfy_url", "comfy_input_dir", "comfy_output_dir", "initial_delivery", "segments"]
    for key in required:
        if key not in config:
            raise ChainError(f"Missing required config key: {key}")

    run_dir = resolve(base, str(config.get("run_dir", "run")))
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "STATE.json"
    artifacts = run_dir / "segments"
    artifacts.mkdir(exist_ok=True)
    workflow_template = resolve(base, str(config["workflow"]))
    input_dir = resolve(base, str(config["comfy_input_dir"]))
    output_dir = resolve(base, str(config["comfy_output_dir"]))
    initial_delivery = resolve(base, str(config["initial_delivery"]))
    if not workflow_template.is_file():
        raise ChainError(f"workflow does not exist: {workflow_template}")
    workflow_template_data = load_json(workflow_template)
    validate_api_workflow(workflow_template_data, workflow_template)
    if not initial_delivery.is_file():
        raise ChainError(f"initial_delivery does not exist: {initial_delivery}")
    require_fps(initial_delivery)
    if not input_dir.is_dir() or not output_dir.is_dir():
        raise ChainError("comfy_input_dir and comfy_output_dir must be existing local directories")

    config_sha256 = file_sha256(config_path)
    workflow_sha256 = file_sha256(workflow_template)
    chain = config["segments"]
    if not isinstance(chain, list) or not chain:
        raise ChainError("segments must be a non-empty list of continuation source slices")
    segment_names: list[str] = []
    for index, item in enumerate(chain):
        if not isinstance(item, dict) or "source" not in item:
            raise ChainError(f"segments[{index}] must be an object containing source")
        name = str(item.get("name", f"seg{index + 2:02d}"))
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ChainError(f"segments[{index}].name must be a single safe path component; got {name!r}")
        segment_names.append(name)
    if len(set(segment_names)) != len(segment_names):
        raise ChainError(f"segment names must be unique: {segment_names}")
    raw_frames = ensure_number(config.get("raw_frames", 124), "raw_frames")
    context_frames = ensure_number(config.get("context_frames", 22), "context_frames")
    if context_frames not in {1, 5, 22, 39}:
        raise ChainError("context_frames must be one of the Motion Context plugin values: 1, 5, 22, 39")
    if (raw_frames - 5) % 17 != 0:
        raise ChainError(f"raw_frames must follow H3's 17k+5 grid (5, 22, 39, ..., 90, 107, 124, ...); got {raw_frames}")
    if raw_frames <= context_frames:
        raise ChainError("raw_frames must exceed context_frames")
    if frame_count(initial_delivery) < context_frames:
        raise ChainError(
            f"initial_delivery has fewer than context_frames={context_frames} frames: {initial_delivery}"
        )
    seed_base = ensure_number(config.get("seed_base", 730000), "seed_base")
    timeout = ensure_number(config.get("timeout_seconds", 5400), "timeout_seconds")
    phase_search = ensure_number(config.get("phase_search", 12), "phase_search")
    phase_segments = ensure_number(config.get("phase_segments", 3), "phase_segments")
    steps = config.get("steps")
    if steps is not None:
        steps = ensure_number(steps, "steps")
    width = config.get("width")
    height = config.get("height")
    for label, dim in (("width", width), ("height", height)):
        if dim is not None and (not isinstance(dim, int) or dim < 32 or dim % 32 != 0):
            raise ChainError(f"{label} must be a positive multiple of 32")
    base_prompt = config.get("prompt")
    if base_prompt is not None and not isinstance(base_prompt, str):
        raise ChainError("prompt must be a string")
    taper = config.get("taper", {})
    if not isinstance(taper, dict):
        raise ChainError("taper must be an object")
    alpha = float(taper.get("alpha", 0.45))
    alpha_end = float(taper.get("alpha_end", 0.10))
    ramp = ensure_number(taper.get("ramp_frames", 3), "taper.ramp_frames")
    if not 0.0 <= alpha_end <= alpha <= 1.0:
        raise ChainError("taper must satisfy 0 <= alpha_end <= alpha <= 1")
    if ramp > context_frames:
        raise ChainError("taper.ramp_frames must not exceed context_frames")
    gates = config.get("gates", {})
    if not isinstance(gates, dict):
        raise ChainError("gates must be an object")
    nodes = config.get("nodes", {})
    if not isinstance(nodes, dict):
        raise ChainError("nodes must be an object")
    node_source = str(nodes.get("source_video", "43"))
    node_context = str(nodes.get("context_video", "101"))
    node_plan = str(nodes.get("plan", "100"))
    node_raw = str(nodes.get("raw_output", "19"))
    node_delivery = str(nodes.get("delivery_output", "108"))

    if state_path.exists():
        state = load_json(state_path)
        if state.get("config_sha256") != config_sha256 or state.get("workflow_sha256") != workflow_sha256:
            raise ChainError(
                "Config or workflow changed after this run started. Refusing to mix lineages. "
                "Start a new run_dir, or deliberately migrate STATE.json after reviewing every completed artifact."
            )
        status = state.get("status")
        if status == "needs_agent_review":
            if not approve_latest:
                print(json.dumps({
                    "status": status,
                    "message": "QA gate stopped the chain. Inspect STATE.json and artifacts; rerun with --approve-latest only to explicitly accept this candidate.",
                    "state": str(state_path),
                    "halt": state.get("halt"),
                }, ensure_ascii=False, indent=2))
                return 2
            completed_for_approval = state.get("completed_segments")
            if not isinstance(completed_for_approval, list) or not completed_for_approval:
                raise ChainError("Cannot approve: STATE.json has no completed halted candidate")
            approved_at = now()
            approval = {
                "segment": completed_for_approval[-1].get("name"),
                "approved_at": approved_at,
                "approved_by": "--approve-latest",
                "gate_failures": completed_for_approval[-1].get("gate_failures", []),
            }
            completed_for_approval[-1]["approval"] = approval
            approvals = state.setdefault("approvals", [])
            if not isinstance(approvals, list):
                raise ChainError("STATE.json approvals is malformed")
            approvals.append(approval)
            state["status"] = "running"
            state.pop("halt", None)
            write_json(state_path, state)
            qa_path = artifacts / str(approval["segment"]) / "qa.json"
            if qa_path.is_file():
                qa_record = load_json(qa_path)
                qa_record["approval"] = approval
                write_json(qa_path, qa_record)
        elif status == "completed":
            print(json.dumps({"status": "completed", "state": str(state_path)}, ensure_ascii=False))
            return 0
        elif status != "running":
            raise ChainError(f"Unknown STATE.json status: {status}")
    else:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        state = {
            "version": 2,
            "status": "running",
            "created_at": now(),
            "run_id": run_id,
            "config": str(config_path),
            "config_sha256": config_sha256,
            "workflow": str(workflow_template),
            "workflow_sha256": workflow_sha256,
            "initial_delivery": str(initial_delivery),
            "context_frames": context_frames,
            "raw_frames": raw_frames,
            "deliver_frames": raw_frames - context_frames,
            "completed_segments": [],
            "approvals": [],
        }
        write_json(state_path, state)

    run_id = str(state["run_id"])
    completed = state.get("completed_segments", [])
    if not isinstance(completed, list):
        raise ChainError("STATE.json completed_segments is malformed")
    previous = Path(completed[-1]["delivery"]) if completed else initial_delivery
    start_index = len(completed)

    for index in range(start_index, len(chain)):
        segment_cfg = chain[index]
        if not isinstance(segment_cfg, dict) or "source" not in segment_cfg:
            raise ChainError(f"segments[{index}] must be an object containing source")
        label = segment_names[index]
        source = resolve(base, str(segment_cfg["source"]))
        if not source.is_file():
            raise ChainError(f"Source slice missing for {label}: {source}")
        require_fps(source)
        if frame_count(source) != raw_frames:
            raise ChainError(f"Source slice {source} must have exactly {raw_frames} frames")
        try:
            seed = int(segment_cfg.get("seed", seed_base + index + 2))
            noise_seed = int(segment_cfg.get("noise_seed", seed ^ 0x5A17))
        except (TypeError, ValueError) as exc:
            raise ChainError(f"Invalid seed/noise_seed for {label}") from exc
        segment_dir = artifacts / label
        segment_dir.mkdir(parents=True, exist_ok=True)

        clean_context = segment_dir / f"context_clean_tail{context_frames}.mp4"
        injected_context = segment_dir / f"context_injected_tail{context_frames}.mp4"
        ffmpeg_tail(previous, clean_context, context_frames)
        run([
            sys.executable, INJECT, clean_context, injected_context,
            context_frames, alpha, alpha_end, ramp, "--seed", noise_seed,
        ], timeout=600)

        workflow = copy.deepcopy(workflow_template_data)
        prepare_static_inputs(config, base, input_dir, run_id, workflow)
        source_name = copy_to_input(source, input_dir, unique_name(run_id, f"{label}_source", source))
        context_name = copy_to_input(injected_context, input_dir, unique_name(run_id, f"{label}_context", injected_context))
        for node_id, role in (
            (node_source, "source video"),
            (node_context, "context video"),
            (node_plan, "chain plan"),
            (node_raw, "raw output"),
            (node_delivery, "delivery output"),
        ):
            node = workflow.get(node_id)
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ChainError(f"Configured {role} node {node_id} is absent or malformed")
        workflow[node_source]["inputs"]["file"] = source_name
        workflow[node_context]["inputs"]["file"] = context_name
        patch_plan(workflow, node_plan, segment_name=f"{run_id}_{label}_s{seed}", seed=seed,
                   raw_frames=raw_frames, steps=steps, context_frames=context_frames,
                   prompt=segment_cfg.get("prompt", base_prompt), width=width, height=height)
        workflow[node_raw]["inputs"]["filename_prefix"] = f"chain/{run_id}/{label}/raw"
        workflow[node_delivery]["inputs"]["filename_prefix"] = f"chain/{run_id}/{label}/delivery"
        workflow_path = segment_dir / "workflow.json"
        write_json(workflow_path, workflow)

        state["active_segment"] = {"index": index, "name": label, "seed": seed, "started_at": now()}
        write_json(state_path, state)
        response = http_json(str(config["comfy_url"]), "/prompt", {"prompt": workflow, "client_id": f"h3-chain-{run_id}-{label}"})
        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ChainError(f"ComfyUI rejected workflow: {json.dumps(response, ensure_ascii=False)}")
        (segment_dir / "submission.json").write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n")
        history = wait_for_job(str(config["comfy_url"]), prompt_id, timeout)
        (segment_dir / "history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n")

        raw_server = first_history_video(history, prompt_id, node_raw, output_dir)
        delivered_server = first_history_video(history, prompt_id, node_delivery, output_dir)
        raw = segment_dir / "raw.mp4"
        delivery = segment_dir / "delivery.mp4"
        shutil.copy2(raw_server, raw)
        shutil.copy2(delivered_server, delivery)
        if frame_count(raw) != raw_frames:
            raise ChainError(f"Raw output for {label} has wrong frame count")
        if frame_count(delivery) != raw_frames - context_frames:
            raise ChainError(f"Trimmed delivery for {label} has wrong frame count")
        require_fps(raw)
        require_fps(delivery)

        offsets, nccs, phase_report = parse_phase(raw, source, context_frames, raw_frames, phase_search, phase_segments)
        (segment_dir / "phase_screen.txt").write_text(phase_report)
        metrics = {
            "phase_offsets": offsets,
            "phase_ncc": [round(value, 4) for value in nccs],
            "seam_diff": round(seam_diff(previous, delivery), 5),
            "sharpness_ratio": round(sharpness_ratio(delivery, source, context_frames), 4),
            "source_rms_difference": round(source_rms_difference(delivery, source, context_frames), 3),
        }
        failures = evaluate(metrics, gates)
        record = {
            "index": index,
            "name": label,
            "seed": seed,
            "noise_seed": noise_seed,
            "prompt_id": prompt_id,
            "source": str(source),
            "previous_delivery": str(previous),
            "context_clean": str(clean_context),
            "context_injected": str(injected_context),
            "raw": str(raw),
            "delivery": str(delivery),
            "workflow": str(workflow_path),
            "metrics": metrics,
            "gate_failures": failures,
            "finished_at": now(),
        }
        # Optional source-audio mux. The silent delivery stays untouched as the
        # lineage/context artifact; the muxed copy is for review and assembly.
        audio_cfg = segment_cfg.get("audio")
        if audio_cfg is not None:
            if not isinstance(audio_cfg, dict) or "file" not in audio_cfg:
                raise ChainError(f"segments[{index}].audio must be an object containing file")
            audio_source = resolve(base, str(audio_cfg["file"]))
            if not audio_source.is_file():
                raise ChainError(f"Audio source missing for {label}: {audio_source}")
            audio_offset = float(audio_cfg.get("offset_seconds", 0.0))
            if audio_offset < 0:
                raise ChainError(f"Audio offset_seconds must be non-negative for {label}")
            delivery_fps = fps_value(delivery)
            # offset_seconds marks where the source slice starts on the master
            # audio timeline; the delivery begins context_frames later.
            delivery_offset = audio_offset + context_frames / delivery_fps
            muxed = segment_dir / "delivery_with_audio.mp4"
            mux_audio(delivery, audio_source, delivery_offset, (raw_frames - context_frames) / delivery_fps, muxed)
            record["delivery_with_audio"] = str(muxed)
        write_json(segment_dir / "qa.json", record)
        completed.append(record)
        state["completed_segments"] = completed
        state.pop("active_segment", None)

        if failures:
            state["status"] = "needs_agent_review"
            state["halt"] = {
                "segment": label,
                "reason": "numeric QA gate failed; candidate is preserved but was not auto-accepted",
                "failures": failures,
                "qa": str(segment_dir / "qa.json"),
                "next_action": "Inspect source/raw/delivery and QA. Rerun with --approve-latest only if you deliberately accept this candidate; otherwise replace or reroll it manually.",
            }
            write_json(state_path, state)
            print(json.dumps({"status": state["status"], "state": str(state_path), "halt": state["halt"]}, ensure_ascii=False, indent=2))
            return 2

        state["status"] = "running"
        write_json(state_path, state)
        previous = delivery

    state["status"] = "completed"
    state["completed_at"] = now()
    state.pop("active_segment", None)
    write_json(state_path, state)
    print(json.dumps({"status": "completed", "state": str(state_path), "segments": len(completed)}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="JSON config; see examples/chain-config.example.json")
    parser.add_argument("--approve-latest", action="store_true",
                        help="Explicitly continue after a previous numeric gate halt. Does not reroll or erase evidence.")
    args = parser.parse_args()
    try:
        return run_chain(args.config.resolve(), args.approve_latest)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed ({exc.returncode}): {' '.join(map(str, exc.cmd))}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1
    except (ChainError, FileNotFoundError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
