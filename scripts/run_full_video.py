#!/usr/bin/env python3
"""Run a complete source-video H3 character replacement from one controller.

This outer loop mirrors the useful semantics of a MieLoop workflow: derive the
iteration count from the source frame count, keep only clean delivered tail
frames as state, collect unique frames per iteration, and finalize one video.
The ComfyUI jobs are still submitted asynchronously from a controller process
so the graph which launched the controller cannot deadlock its own queue.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_chain as chain


class FullVideoError(chain.ChainError):
    pass


def plan_segments(total_frames: int, raw_frames: int = 124, context_frames: int = 22) -> list[dict[str, int | str]]:
    if total_frames < 1:
        raise FullVideoError("The source video contains no frames")
    if raw_frames <= context_frames or (raw_frames - 5) % 17 != 0:
        raise FullVideoError("raw_frames must exceed context_frames and follow H3's 17k + 5 grid")
    delivery_frames = raw_frames - context_frames
    count = 1 + math.ceil(max(0, total_frames - raw_frames) / delivery_frames)
    plan: list[dict[str, int | str]] = []
    for index in range(count):
        source_start = index * delivery_frames
        available = min(raw_frames, max(0, total_frames - source_start))
        unique_start = source_start if index == 0 else source_start + context_frames
        unique_frames = min(
            raw_frames if index == 0 else delivery_frames,
            max(0, total_frames - unique_start),
        )
        if unique_frames < 1:
            raise FullVideoError(f"Segment planning produced an empty delivery at index {index}")
        plan.append({
            "index": index,
            "name": f"seg{index + 1:02d}",
            "source_start": source_start,
            "source_available_frames": available,
            "inference_padding_frames": raw_frames - available,
            "unique_start": unique_start,
            "unique_frames": unique_frames,
        })
    if sum(int(item["unique_frames"]) for item in plan) != total_frames:
        raise FullVideoError("Segment plan does not preserve the source frame count")
    return plan


def normalize_source(source: Path, destination: Path, width: int, height: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    filters = (
        f"fps=24,scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )
    chain.run([
        "ffmpeg", "-y", "-v", "error", "-i", source, "-vf", filters,
        "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", destination,
    ], timeout=3600)
    chain.require_fps(destination)


def slice_with_inference_padding(
    source: Path,
    destination: Path,
    start_frame: int,
    available_frames: int,
    raw_frames: int,
) -> None:
    if available_frames < 1 or available_frames > raw_frames:
        raise FullVideoError("Invalid source slice frame count")
    filters = [
        f"trim=start_frame={start_frame}:end_frame={start_frame + available_frames}",
        "setpts=PTS-STARTPTS",
    ]
    padding = raw_frames - available_frames
    if padding:
        filters.append(f"tpad=stop_mode=clone:stop_duration={padding / 24 + 0.05:.6f}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    chain.run([
        "ffmpeg", "-y", "-v", "error", "-i", source, "-vf", ",".join(filters),
        "-frames:v", str(raw_frames), "-fps_mode", "cfr", "-r", "24",
        "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-an", destination,
    ])
    if chain.frame_count(destination) != raw_frames:
        raise FullVideoError(f"Source slice has the wrong frame count: {destination}")


def has_audio(path: Path) -> bool:
    result = chain.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", path,
    ])
    return bool(result.stdout.strip())


def concat_deliveries(deliveries: list[Path], destination: Path) -> None:
    if not deliveries:
        raise FullVideoError("No deliveries are available for assembly")
    listing = destination.with_suffix(".ffconcat")
    rows = ["ffconcat version 1.0"]
    for path in deliveries:
        escaped = str(path.resolve()).replace("'", "'\\''")
        rows.append(f"file '{escaped}'")
    listing.write_text("\n".join(rows) + "\n", encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    chain.run([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing,
        "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr", "-r", "24", destination,
    ], timeout=3600)


def mux_original_audio(video: Path, source: Path, destination: Path) -> None:
    if not has_audio(source):
        shutil.copy2(video, destination)
        return
    chain.run([
        "ffmpeg", "-y", "-v", "error", "-i", video, "-i", source,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
        "-af", "apad", "-shortest", destination,
    ], timeout=3600)


def require_config(config: dict[str, Any]) -> None:
    required = [
        "source_video", "initial_workflow", "continuation_workflow", "comfy_url",
        "comfy_input_dir", "comfy_output_dir", "run_dir", "reference_images",
    ]
    missing = [key for key in required if key not in config]
    if missing:
        raise FullVideoError(f"Missing required config keys: {', '.join(missing)}")


def normalize_reference_paths(value: Any, base: Path) -> Any:
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return [str(chain.resolve(base, item)) for item in value]
    if isinstance(value, dict) and value and all(isinstance(item, str) for item in value.values()):
        return {str(node_id): str(chain.resolve(base, item)) for node_id, item in value.items()}
    raise FullVideoError("reference_images must be a non-empty path list or node-id map")


def patch_initial_workflow(
    workflow: dict[str, Any],
    config: dict[str, Any],
    source_name: str,
    run_id: str,
) -> tuple[str, str]:
    nodes = config.get("initial_nodes", {})
    if not isinstance(nodes, dict):
        raise FullVideoError("initial_nodes must be an object")
    source_id = str(nodes.get("source_video", "43"))
    ref2va_id = str(nodes.get("ref2va", "20"))
    noise_id = str(nodes.get("noise", "12"))
    scheduler_id = str(nodes.get("scheduler", "14"))
    output_id = str(nodes.get("output", "19"))
    for node_id, label in (
        (source_id, "initial source"), (ref2va_id, "initial Ref2VA"),
        (noise_id, "initial noise"), (scheduler_id, "initial scheduler"),
        (output_id, "initial output"),
    ):
        node = workflow.get(node_id)
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise FullVideoError(f"Configured {label} node {node_id} is absent or malformed")
    workflow[source_id]["inputs"]["file"] = source_name
    ref_inputs = workflow[ref2va_id]["inputs"]
    ref_inputs["width"] = int(config.get("width", 576))
    ref_inputs["height"] = int(config.get("height", 1024))
    ref_inputs["length"] = int(config.get("raw_frames", 124))
    prompt = config.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        ref_inputs["prompt"] = prompt
    workflow[noise_id]["inputs"]["noise_seed"] = int(config.get("seed_base", 730000)) + 1
    workflow[scheduler_id]["inputs"]["steps"] = int(config.get("steps", 20))
    workflow[output_id]["inputs"]["filename_prefix"] = f"full-chain/{run_id}/seg01/delivery"
    return output_id, ref2va_id


def generate_initial(
    config: dict[str, Any],
    config_base: Path,
    state: dict[str, Any],
    source_slice: Path,
    unique_frames: int,
) -> tuple[Path, list[str], dict[str, Any]]:
    run_dir = Path(state["run_dir"])
    segment_dir = run_dir / "segments" / "seg01"
    segment_dir.mkdir(parents=True, exist_ok=True)
    workflow_path = chain.resolve(config_base, str(config["initial_workflow"]))
    workflow = chain.load_json(workflow_path)
    chain.validate_api_workflow(workflow, workflow_path)
    workflow = copy.deepcopy(workflow)
    input_dir = chain.resolve(config_base, str(config["comfy_input_dir"]))
    output_dir = chain.resolve(config_base, str(config["comfy_output_dir"]))
    run_id = str(state["run_id"])
    chain.prepare_static_inputs(config, config_base, input_dir, run_id, workflow)
    source_name = chain.copy_to_input(
        source_slice, input_dir, chain.unique_name(run_id, "seg01_source", source_slice)
    )
    output_id, _ = patch_initial_workflow(workflow, config, source_name, run_id)
    workflow_snapshot = segment_dir / "workflow.json"
    chain.write_json(workflow_snapshot, workflow)
    state["active_segment"] = {"index": 0, "name": "seg01", "started_at": chain.now()}
    state["status"] = "initial_generation"
    chain.write_json(run_dir / "STATE.json", state)
    response = chain.http_json(
        str(config["comfy_url"]), "/prompt",
        {"prompt": workflow, "client_id": f"h3-full-{run_id}-seg01"},
    )
    prompt_id = response.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise FullVideoError(f"ComfyUI rejected initial workflow: {json.dumps(response, ensure_ascii=False)}")
    (segment_dir / "submission.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    history = chain.wait_for_job(str(config["comfy_url"]), prompt_id, int(config.get("timeout_seconds", 5400)))
    (segment_dir / "history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    server_output = chain.first_history_video(history, prompt_id, output_id, output_dir)
    full_delivery = segment_dir / "delivery_full_inference.mp4"
    shutil.copy2(server_output, full_delivery)
    raw_frames = int(config.get("raw_frames", 124))
    if chain.frame_count(full_delivery) != raw_frames:
        raise FullVideoError("Initial H3 output has the wrong frame count")
    delivery = segment_dir / "delivery.mp4"
    if unique_frames == raw_frames:
        shutil.copy2(full_delivery, delivery)
    else:
        chain.trim_video_frames(full_delivery, delivery, unique_frames)
    chain.require_fps(delivery)

    phase_search = int(config.get("phase_search", 12))
    phase_segments = int(config.get("phase_segments", 3))
    phase_metrics, phase_report = chain.measure_phase(
        full_delivery, source_slice, 0, unique_frames, phase_search, phase_segments
    )
    (segment_dir / "phase_screen.txt").write_text(phase_report, encoding="utf-8")
    metrics = {
        **phase_metrics,
        "seam_diff": 0.0,
        "sharpness_ratio": round(chain.sharpness_ratio(delivery, source_slice, 0), 4),
        "source_rms_difference": round(chain.source_rms_difference(delivery, source_slice, 0), 3),
    }
    gates = config.get("gates", {})
    if not isinstance(gates, dict):
        raise FullVideoError("gates must be an object")
    failures = chain.evaluate(metrics, gates)
    record = {
        "index": 0,
        "name": "seg01",
        "seed": int(config.get("seed_base", 730000)) + 1,
        "prompt_id": prompt_id,
        "source": str(source_slice),
        "raw": str(full_delivery),
        "delivery": str(delivery),
        "unique_frames": unique_frames,
        "workflow": str(workflow_snapshot),
        "metrics": metrics,
        "gate_failures": failures,
        "finished_at": chain.now(),
    }
    chain.write_json(segment_dir / "qa.json", record)
    state["initial"] = record
    state["completed_segment_count"] = 1
    state.pop("active_segment", None)
    chain.write_json(run_dir / "STATE.json", state)
    return delivery, failures, record


def build_continuation_config(
    config: dict[str, Any],
    run_dir: Path,
    initial_delivery: Path,
    plan: list[dict[str, Any]],
    slice_paths: list[Path],
) -> Path:
    payload: dict[str, Any] = {
        "workflow": str(config["continuation_workflow"]),
        "comfy_url": config["comfy_url"],
        "comfy_input_dir": config["comfy_input_dir"],
        "comfy_output_dir": config["comfy_output_dir"],
        "run_dir": str(run_dir / "continuation"),
        "initial_delivery": str(initial_delivery),
        "reference_images": config["reference_images"],
        "raw_frames": int(config.get("raw_frames", 124)),
        "context_frames": int(config.get("context_frames", 22)),
        "steps": int(config.get("steps", 20)),
        "width": int(config.get("width", 576)),
        "height": int(config.get("height", 1024)),
        "seed_base": int(config.get("seed_base", 730000)),
        "timeout_seconds": int(config.get("timeout_seconds", 5400)),
        "phase_search": int(config.get("phase_search", 12)),
        "phase_segments": int(config.get("phase_segments", 3)),
        "taper": config.get("taper", {"alpha": 0.45, "alpha_end": 0.10, "ramp_frames": 3}),
        "gates": config.get("gates", {}),
        "nodes": config.get("continuation_nodes", {}),
        "segments": [],
    }
    prompt = config.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        payload["prompt"] = prompt
    for item, source in zip(plan[1:], slice_paths[1:]):
        index = int(item["index"])
        payload["segments"].append({
            "name": item["name"],
            "source": str(source),
            "seed": int(config.get("seed_base", 730000)) + index + 1,
            "unique_frames": int(item["unique_frames"]),
        })
    path = run_dir / "continuation-config.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != serialized:
        raise FullVideoError("Continuation config changed inside an existing run_dir")
    path.write_text(serialized, encoding="utf-8")
    return path


def assemble_final(config: dict[str, Any], state: dict[str, Any], initial_delivery: Path) -> Path:
    run_dir = Path(state["run_dir"])
    deliveries = [initial_delivery]
    continuation_state_path = run_dir / "continuation" / "STATE.json"
    if continuation_state_path.is_file():
        continuation_state = chain.load_json(continuation_state_path)
        completed = continuation_state.get("completed_segments", [])
        if not isinstance(completed, list):
            raise FullVideoError("Continuation STATE.json is malformed")
        deliveries.extend(Path(item["delivery"]) for item in completed)
    expected = int(state["source_total_frames"])
    actual = sum(chain.frame_count(path) for path in deliveries)
    if actual != expected:
        raise FullVideoError(f"Collected deliveries contain {actual} frames; expected {expected}")
    final_dir = run_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    silent = final_dir / "final_silent.mp4"
    concat_deliveries(deliveries, silent)
    if chain.frame_count(silent) != expected:
        raise FullVideoError("Final silent assembly changed the frame count")
    source = Path(state["source_video"])
    final = final_dir / "final.mp4"
    mux_original_audio(silent, source, final)
    if chain.frame_count(final) != expected:
        raise FullVideoError("Final audio mux changed the video frame count")
    return final


def run_full_video(config_path: Path, approve_latest: bool = False) -> int:
    config = chain.load_json(config_path)
    require_config(config)
    base = config_path.parent
    source = chain.resolve(base, str(config["source_video"]))
    initial_workflow = chain.resolve(base, str(config["initial_workflow"]))
    continuation_workflow = chain.resolve(base, str(config["continuation_workflow"]))
    input_dir = chain.resolve(base, str(config["comfy_input_dir"]))
    output_dir = chain.resolve(base, str(config["comfy_output_dir"]))
    run_dir = chain.resolve(base, str(config["run_dir"]))
    config["source_video"] = str(source)
    config["initial_workflow"] = str(initial_workflow)
    config["continuation_workflow"] = str(continuation_workflow)
    config["comfy_input_dir"] = str(input_dir)
    config["comfy_output_dir"] = str(output_dir)
    config["run_dir"] = str(run_dir)
    config["reference_images"] = normalize_reference_paths(config["reference_images"], base)
    for path, label in (
        (source, "source_video"), (initial_workflow, "initial_workflow"),
        (continuation_workflow, "continuation_workflow"),
    ):
        if not path.is_file():
            raise FullVideoError(f"{label} does not exist: {path}")
    for path, label in ((input_dir, "comfy_input_dir"), (output_dir, "comfy_output_dir")):
        if not path.is_dir():
            raise FullVideoError(f"{label} is not a directory: {path}")
    raw_frames = int(config.get("raw_frames", 124))
    context_frames = int(config.get("context_frames", 22))
    width = int(config.get("width", 576))
    height = int(config.get("height", 1024))
    if width < 32 or height < 32 or width % 32 or height % 32:
        raise FullVideoError("width and height must be positive multiples of 32")
    state_path = run_dir / "STATE.json"
    config_sha = chain.file_sha256(config_path)
    continuation_approve = False

    if state_path.exists():
        state = chain.load_json(state_path)
        if state.get("config_sha256") != config_sha:
            raise FullVideoError("Config changed after this full-video run started; use a new run_dir")
        if state.get("initial_workflow_sha256") != chain.file_sha256(initial_workflow):
            raise FullVideoError("Initial workflow changed after this full-video run started")
        if state.get("continuation_workflow_sha256") != chain.file_sha256(continuation_workflow):
            raise FullVideoError("Continuation workflow changed after this full-video run started")
        if state.get("status") == "completed":
            print(json.dumps({"status": "completed", "final_video": state.get("final_video")}, ensure_ascii=False))
            return 0
        if state.get("status") == "needs_agent_review":
            if not approve_latest:
                print(json.dumps({"status": "needs_agent_review", "halt": state.get("halt")}, ensure_ascii=False, indent=2))
                return 2
            halt = state.get("halt", {})
            if isinstance(halt, dict) and halt.get("phase") == "initial":
                state.setdefault("approvals", []).append({
                    "segment": "seg01", "approved_at": chain.now(), "approved_by": "--approve-latest",
                })
                state["status"] = "running"
                state.pop("halt", None)
                chain.write_json(state_path, state)
            elif isinstance(halt, dict) and halt.get("phase") == "continuation":
                continuation_approve = True
                state["status"] = "running"
                state.pop("halt", None)
                chain.write_json(state_path, state)
            else:
                raise FullVideoError("STATE.json has an unknown review phase")
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        state = {
            "version": 1,
            "mode": "full_video",
            "status": "preprocessing",
            "created_at": chain.now(),
            "run_id": run_id,
            "run_dir": str(run_dir),
            "config": str(config_path),
            "config_sha256": config_sha,
            "source_video": str(source),
            "initial_workflow_sha256": chain.file_sha256(initial_workflow),
            "continuation_workflow_sha256": chain.file_sha256(continuation_workflow),
            "approvals": [],
            "completed_segment_count": 0,
        }
        chain.write_json(state_path, state)

    work_dir = run_dir / "work"
    normalized = work_dir / "source_24fps.mp4"
    if not normalized.is_file():
        normalize_source(source, normalized, width, height)
    total_frames = chain.frame_count(normalized)
    plan = plan_segments(total_frames, raw_frames, context_frames)
    state["source_total_frames"] = total_frames
    state["segment_plan"] = plan
    state["total_segments"] = len(plan)
    state["normalized_source"] = str(normalized)
    chain.write_json(state_path, state)

    slice_dir = work_dir / "segments"
    slice_paths: list[Path] = []
    for item in plan:
        path = slice_dir / f"{item['name']}_source_{raw_frames}f.mp4"
        if not path.is_file():
            slice_with_inference_padding(
                normalized,
                path,
                int(item["source_start"]),
                int(item["source_available_frames"]),
                raw_frames,
            )
        slice_paths.append(path)

    initial_record = state.get("initial")
    if isinstance(initial_record, dict) and Path(str(initial_record.get("delivery", ""))).is_file():
        initial_delivery = Path(str(initial_record["delivery"]))
    else:
        initial_delivery, failures, record = generate_initial(
            config, base, state, slice_paths[0], int(plan[0]["unique_frames"])
        )
        if failures:
            state["status"] = "needs_agent_review"
            state["halt"] = {
                "phase": "initial",
                "segment": "seg01",
                "reason": "Initial segment failed numerical QA gates",
                "failures": failures,
                "qa": str(run_dir / "segments" / "seg01" / "qa.json"),
            }
            chain.write_json(state_path, state)
            print(json.dumps({"status": "needs_agent_review", "halt": state["halt"]}, ensure_ascii=False, indent=2))
            return 2
        state["initial"] = record

    if len(plan) > 1:
        continuation_config = build_continuation_config(
            config, run_dir, initial_delivery, plan, slice_paths
        )
        state["continuation_config"] = str(continuation_config)
        state["status"] = "continuation"
        chain.write_json(state_path, state)
        result = chain.run_chain(continuation_config, continuation_approve)
        continuation_state = chain.load_json(run_dir / "continuation" / "STATE.json")
        completed = continuation_state.get("completed_segments", [])
        state["completed_segment_count"] = 1 + (len(completed) if isinstance(completed, list) else 0)
        if result == 2:
            state["status"] = "needs_agent_review"
            state["halt"] = {
                "phase": "continuation",
                "segment": continuation_state.get("halt", {}).get("segment"),
                "reason": "Continuation segment failed numerical QA gates",
                "continuation_state": str(run_dir / "continuation" / "STATE.json"),
                "details": continuation_state.get("halt"),
            }
            chain.write_json(state_path, state)
            print(json.dumps({"status": "needs_agent_review", "halt": state["halt"]}, ensure_ascii=False, indent=2))
            return 2
        if result != 0:
            raise FullVideoError(f"Continuation runner returned exit code {result}")

    state["status"] = "assembling"
    chain.write_json(state_path, state)
    final = assemble_final(config, state, initial_delivery)
    state["status"] = "completed"
    state["completed_at"] = chain.now()
    state["completed_segment_count"] = len(plan)
    state["final_video"] = str(final)
    chain.write_json(state_path, state)
    print(json.dumps({
        "status": "completed",
        "final_video": str(final),
        "source_frames": total_frames,
        "segments": len(plan),
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--approve-latest", action="store_true")
    args = parser.parse_args()
    try:
        return run_full_video(args.config.resolve(), args.approve_latest)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed ({exc.returncode}): {' '.join(map(str, exc.cmd))}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1
    except (FullVideoError, chain.ChainError, FileNotFoundError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
