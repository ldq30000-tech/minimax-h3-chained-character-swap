"""Non-blocking ComfyUI nodes for the guarded H3 continuation runner.

The runner submits continuation workflows back to ComfyUI. Running it inline in
the current graph would deadlock a single-worker queue, so the launch node
starts a separate controller process and returns immediately.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

try:
    import folder_paths
except ImportError:  # Allows the controller helpers to be tested outside ComfyUI.
    folder_paths = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "scripts" / "run_chain.py"
FULL_VIDEO_RUNNER = ROOT / "scripts" / "run_full_video.py"
WORKER = ROOT / "comfy_worker.py"
CONTROLLERS: dict[int, subprocess.Popen[Any]] = {}


class H3ChainNodeError(RuntimeError):
    """A configuration error that should be shown directly in the ComfyUI UI."""


def _reap_controllers() -> None:
    """Keep active launch handles alive and discard handles whose worker exited."""
    for pid, process in list(CONTROLLERS.items()):
        if process.poll() is not None:
            CONTROLLERS.pop(pid, None)


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise H3ChainNodeError(f"Cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise H3ChainNodeError(f"Cannot parse {label} as JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise H3ChainNodeError(f"{label} must contain a JSON object: {path}")
    return value


def _parse_json(value: str, label: str, expected: type | tuple[type, ...]) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise H3ChainNodeError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, expected):
        names = (expected,) if isinstance(expected, type) else expected
        expected_name = " or ".join(item.__name__ for item in names)
        raise H3ChainNodeError(f"{label} must be a JSON {expected_name}")
    return parsed


def _require_file(value: str, label: str) -> Path:
    path = _path(value)
    if not path.is_file():
        raise H3ChainNodeError(f"{label} does not exist or is not a file: {path}")
    return path


def _require_directory(value: str, label: str) -> Path:
    path = _path(value)
    if not path.is_dir():
        raise H3ChainNodeError(f"{label} does not exist or is not a directory: {path}")
    return path


def _write_text_if_safe(path: Path, text: str) -> None:
    """Do not mutate a run configuration once it has an established lineage."""
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except OSError as exc:
            raise H3ChainNodeError(f"Cannot read existing config: {path}: {exc}") from exc
        if (path.parent / "STATE.json").exists():
            raise H3ChainNodeError(
                "This run already has STATE.json and the generated config changed. "
                "Use a new run_dir instead of mixing chain lineages."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _validate_reference_images(value: Any) -> Any:
    if isinstance(value, list):
        if not value or len(value) > 9 or not all(isinstance(item, str) for item in value):
            raise H3ChainNodeError("reference_images_json must be a list of 1 to 9 image paths")
        for index, item in enumerate(value, start=1):
            _require_file(item, f"reference image {index}")
        return value
    if isinstance(value, dict):
        if not value or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
            raise H3ChainNodeError("reference_images_json object must map LoadImage node ids to image paths")
        for node_id, item in value.items():
            _require_file(item, f"reference image for node {node_id}")
        return value
    raise H3ChainNodeError("reference_images_json must be a JSON list or object")


def _validate_segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise H3ChainNodeError("segments_json must be a non-empty JSON list")
    for index, segment in enumerate(value, start=1):
        if not isinstance(segment, dict) or not isinstance(segment.get("source"), str):
            raise H3ChainNodeError(f"segments_json item {index} needs a source path")
        _require_file(segment["source"], f"source segment {index}")
    return value


def _validate_h3_settings(raw_frames: int, context_frames: int, width: int, height: int, steps: int) -> None:
    if context_frames not in {1, 5, 22, 39}:
        raise H3ChainNodeError("context_frames must be one of 1, 5, 22, or 39")
    if raw_frames <= context_frames or (raw_frames - 5) % 17 != 0:
        raise H3ChainNodeError("raw_frames must exceed context_frames and follow H3's 17k + 5 grid")
    if width < 32 or height < 32 or width % 32 or height % 32:
        raise H3ChainNodeError("width and height must be positive multiples of 32")
    if steps < 1:
        raise H3ChainNodeError("steps must be positive")


def _native_loop_lengths(
    source_frames: int,
    raw_frames: int = 124,
    context_frames: int = 22,
) -> tuple[list[int], int]:
    """Plan H3-valid raw scene lengths and inference-only end padding."""
    _validate_h3_settings(raw_frames, context_frames, 32, 32, 1)
    if source_frames < 1:
        raise H3ChainNodeError("source video must contain at least one 24 fps frame")

    inference_frames = source_frames + ((5 - source_frames) % 17)
    if inference_frames <= raw_frames:
        return [max(5, inference_frames)], inference_frames - source_frames

    delivered_per_continuation = raw_frames - context_frames
    remaining = inference_frames - raw_frames
    lengths = [raw_frames]
    while remaining > delivered_per_continuation:
        lengths.append(raw_frames)
        remaining -= delivered_per_continuation

    final_raw = remaining + context_frames
    # Very short final scenes are outside the documented H3 training range.
    # Fold one preceding 102-frame delivery into the final scene when needed.
    if final_raw < 90 and len(lengths) > 1:
        lengths.pop()
        final_raw += delivered_per_continuation
    lengths.append(final_raw)
    for index, length in enumerate(lengths, start=1):
        if (length - 5) % 17 or length <= (context_frames if index > 1 else 0):
            raise H3ChainNodeError(f"internal scene plan produced invalid H3 length {length}")
    return lengths, inference_frames - source_frames


def _native_loop_plan(
    source_frames: int,
    prompt: str,
    raw_frames: int,
    context_frames: int,
    steps: int,
    base_seed: int,
) -> tuple[dict[str, Any], int]:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise H3ChainNodeError("scene prompt cannot be empty")
    lengths, padding = _native_loop_lengths(source_frames, raw_frames, context_frames)
    shots = [
        {
            "id": f"source_{index:02d}",
            "prompt": prompt,
            "length": length,
            "steps": steps,
            "seed": str(base_seed + index - 1),
        }
        for index, length in enumerate(lengths, start=1)
    ]
    return {"defaults": {"steps": steps}, "shots": shots}, padding


def _audio_waveform(audio: Any, label: str) -> tuple[Any, int]:
    if not isinstance(audio, dict):
        raise H3ChainNodeError(f"{label} is missing")
    waveform = audio.get("waveform")
    sample_rate = audio.get("sample_rate")
    if waveform is None or sample_rate is None:
        raise H3ChainNodeError(f"{label} must contain waveform and sample_rate")
    if getattr(waveform, "ndim", None) == 1:
        waveform = waveform.unsqueeze(0).unsqueeze(0)
    elif getattr(waveform, "ndim", None) == 2:
        waveform = waveform.unsqueeze(0)
    if getattr(waveform, "ndim", None) != 3:
        raise H3ChainNodeError(f"{label} waveform must be [batch, channels, samples]")
    return waveform, int(sample_rate)


def _video_preview_item(path: Path) -> dict[str, str] | None:
    """Build a ComfyUI video preview item for files inside the output root."""
    if folder_paths is None:
        return None
    output_root = Path(folder_paths.get_output_directory()).resolve()
    try:
        relative = path.resolve().relative_to(output_root)
    except ValueError:
        return None
    return {
        "filename": relative.name,
        "subfolder": relative.parent.as_posix() if relative.parent != Path(".") else "",
        "type": "output",
    }


class H3NativeLongVideoPrepare:
    """Normalize one native video and build the visible recursive scene plan."""

    CATEGORY = "H3 Chain/Native Loop"
    RETURN_TYPES = ("IMAGE", "AUDIO", "AUDIO", "STRING", "INT", "INT", "INT", "STRING")
    RETURN_NAMES = (
        "frames_24fps",
        "inference_audio",
        "source_audio",
        "plan_json",
        "source_frame_count",
        "inference_frame_count",
        "segment_count",
        "status",
    )
    FUNCTION = "prepare"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "source_video": ("VIDEO",),
                "prompt": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False}),
                "raw_frames": ("INT", {"default": 124, "min": 90, "max": 362, "step": 17}),
                "context_frames": ([22], {"default": 22}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "base_seed": ("INT", {"default": 730000, "min": 0, "max": 18446744073709551615}),
            }
        }

    def prepare(
        self,
        source_video: Any,
        prompt: str,
        raw_frames: int,
        context_frames: int,
        steps: int,
        base_seed: int,
    ) -> tuple[Any, dict[str, Any], dict[str, Any], str, int, int, int, str]:
        get_components = getattr(source_video, "get_components", None)
        if not callable(get_components):
            raise H3ChainNodeError("source_video must come from ComfyUI's native Load Video node")
        try:
            components = get_components()
        except Exception as exc:
            raise H3ChainNodeError(f"cannot decode source video: {exc}") from exc
        frames = getattr(components, "images", None)
        source_audio = getattr(components, "audio", None)
        try:
            source_fps = float(getattr(components, "frame_rate"))
        except (TypeError, ValueError) as exc:
            raise H3ChainNodeError("source video has no valid frame rate") from exc
        if getattr(frames, "ndim", None) != 4 or int(frames.shape[0]) < 1:
            raise H3ChainNodeError("decoded source video did not return an IMAGE frame batch")
        if not math.isfinite(source_fps) or source_fps <= 0:
            raise H3ChainNodeError("source video frame rate must be positive")

        try:
            import torch
        except ImportError as exc:
            raise H3ChainNodeError("PyTorch is required to prepare the H3 source timeline") from exc

        source_frame_count = max(1, int(round(int(frames.shape[0]) / source_fps * 24.0)))
        indices = (
            torch.arange(source_frame_count, dtype=torch.float64)
            * (source_fps / 24.0)
        ).floor().to(dtype=torch.long).clamp(min=0, max=int(frames.shape[0]) - 1)
        normalized = frames.index_select(0, indices.to(device=frames.device))

        plan, padding = _native_loop_plan(
            source_frame_count, prompt, raw_frames, context_frames, steps, base_seed
        )
        inference_frame_count = source_frame_count + padding
        if padding:
            normalized = torch.cat(
                [normalized, normalized[-1:].repeat((padding, 1, 1, 1))], dim=0
            )

        if source_audio is None:
            sample_rate = 44100
            source_samples = max(
                1, int(round(source_frame_count / 24.0 * sample_rate))
            )
            source_waveform = torch.zeros(
                (1, 1, source_samples), dtype=torch.float32
            )
            audio_status = "audio=missing -> 44.1 kHz mono silence"
        else:
            source_waveform, sample_rate = _audio_waveform(
                source_audio, "source video audio"
            )
            audio_status = f"audio=source {sample_rate} Hz"

        waveform = source_waveform
        required_samples = int(round(inference_frame_count / 24.0 * sample_rate))
        available_samples = int(waveform.shape[-1])
        if available_samples < required_samples:
            waveform = torch.nn.functional.pad(waveform, (0, required_samples - available_samples))
        else:
            waveform = waveform[..., :required_samples]
        inference_audio = {"waveform": waveform.clone(), "sample_rate": sample_rate}
        original_audio = {
            "waveform": source_waveform.clone(),
            "sample_rate": sample_rate,
            "h3_audio_source": "silence_fallback" if source_audio is None else "source",
        }
        lengths = [shot["length"] for shot in plan["shots"]]
        status = (
            f"{int(frames.shape[0])} frames at {source_fps:.6g} fps -> "
            f"{source_frame_count} unique frames at 24 fps; scenes={lengths}; "
            f"inference={inference_frame_count} frames; end padding={padding} frames; "
            f"{audio_status}"
        )
        return (
            normalized,
            inference_audio,
            original_audio,
            json.dumps(plan, ensure_ascii=False, indent=2),
            source_frame_count,
            inference_frame_count,
            len(lengths),
            status,
        )


class H3FinalTrimToSource:
    """Remove inference-only end padding and mux the original source audio."""

    CATEGORY = "H3 Chain/Native Loop"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("final_video", "status")
    FUNCTION = "finalize"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "video_path": ("STRING", {"forceInput": True}),
                "source_audio": ("AUDIO", {"forceInput": True}),
                "source_frame_count": ("INT", {"forceInput": True}),
                "filename": ("STRING", {"default": "character_swap_full_exact"}),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001}),
                "audio_bitrate": ("INT", {"default": 256, "min": 64, "max": 512}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, *args: Any, **kwargs: Any) -> float:
        return float("nan")

    def finalize(
        self,
        video_path: str,
        source_audio: Any,
        source_frame_count: int,
        filename: str,
        fps: float,
        audio_bitrate: int,
    ) -> Any:
        source = _require_file(video_path, "assembled H3 video")
        if source_frame_count < 1 or not math.isfinite(float(fps)) or fps <= 0:
            raise H3ChainNodeError("source_frame_count and fps must be positive")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise H3ChainNodeError("ffmpeg was not found on PATH")
        waveform, sample_rate = _audio_waveform(source_audio, "original source audio")

        safe_name = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in str(filename or "character_swap_full_exact")
        ).strip("._") or "character_swap_full_exact"
        destination = source.parent / f"{safe_name}.mp4"
        suffix = 1
        while destination.exists():
            destination = source.parent / f"{safe_name}_{suffix:03d}.mp4"
            suffix += 1
        temporary_output = destination.with_name(destination.stem + ".tmp.mp4")
        duration = source_frame_count / float(fps)

        with tempfile.TemporaryDirectory(prefix="h3_exact_trim_") as temp_dir:
            wav_path = Path(temp_dir) / "source.wav"
            try:
                import torch
            except ImportError as exc:
                raise H3ChainNodeError("PyTorch is required to write the source soundtrack") from exc
            pcm = (
                waveform[0].detach().cpu().clamp(-1.0, 1.0).transpose(0, 1)
                * 32767.0
            ).round().to(dtype=torch.int16).numpy()
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(int(pcm.shape[1]))
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(pcm.tobytes())
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-i",
                str(wav_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-frames:v",
                str(source_frame_count),
                "-t",
                f"{duration:.9f}",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_bitrate}k",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode:
                temporary_output.unlink(missing_ok=True)
                detail = completed.stderr.strip() or completed.stdout.strip() or "unknown ffmpeg error"
                raise H3ChainNodeError(f"final exact trim failed: {detail}")
        temporary_output.replace(destination)
        audio_label = (
            "silence fallback"
            if source_audio.get("h3_audio_source") == "silence_fallback"
            else "original audio"
        )
        status = (
            f"trimmed to {source_frame_count} frames at {fps:.6g} fps "
            f"({duration:.3f}s) and muxed {sample_rate} Hz {audio_label}"
        )
        result = (str(destination.resolve()), status)
        preview = _video_preview_item(destination)
        if preview is None:
            return result
        return {
            "ui": {"text": [status], "videos": [preview]},
            "result": result,
        }


def _uploaded_files(content_type: str) -> list[str]:
    if folder_paths is None:
        return []
    input_dir = folder_paths.get_input_directory()
    files = [item.name for item in Path(input_dir).iterdir() if item.is_file()]
    return sorted(folder_paths.filter_files_content_types(files, [content_type]))


def _uploaded_path(value: str, label: str) -> Path:
    direct = Path(value).expanduser()
    if direct.is_file():
        return direct.resolve()
    if folder_paths is None:
        raise H3ChainNodeError(f"{label} does not exist: {direct}")
    path = Path(folder_paths.get_annotated_filepath(value)).resolve()
    if not path.is_file():
        raise H3ChainNodeError(f"{label} does not exist in ComfyUI input: {value}")
    return path


class H3FullVideoInputs:
    """Select or upload the source video and four identity reference images."""

    CATEGORY = "H3 Chain"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("source_video", "reference_images_json")
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        videos = _uploaded_files("video")
        images = _uploaded_files("image")
        return {
            "required": {
                "source_video": (videos, {"video_upload": True}),
                "character_front": (images, {"image_upload": True}),
                "character_side": (images, {"image_upload": True}),
                "character_back": (images, {"image_upload": True}),
                "character_face_closeup": (images, {"image_upload": True}),
            }
        }

    def build(
        self,
        source_video: str,
        character_front: str,
        character_side: str,
        character_back: str,
        character_face_closeup: str,
    ) -> tuple[str, str]:
        source = _uploaded_path(source_video, "source video")
        references = [
            _uploaded_path(character_front, "front reference image"),
            _uploaded_path(character_side, "side reference image"),
            _uploaded_path(character_back, "back reference image"),
            _uploaded_path(character_face_closeup, "face closeup reference image"),
        ]
        return str(source), json.dumps([str(path) for path in references], ensure_ascii=False)


class H3ChainConfig:
    """Create an immutable runner configuration from a ComfyUI graph."""

    CATEGORY = "H3 Chain"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("config_path",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "workflow_path": ("STRING", {"default": str(ROOT / "assets" / "workflows" / "t3-ref2va-context-taper-template.json")}),
                "comfy_url": ("STRING", {"default": "http://127.0.0.1:8188"}),
                "comfy_input_dir": ("STRING", {"default": ""}),
                "comfy_output_dir": ("STRING", {"default": ""}),
                "run_dir": ("STRING", {"default": str(ROOT / "runs" / "comfy-chain")}),
                "initial_delivery": ("STRING", {"default": ""}),
                "reference_images_json": ("STRING", {"default": "[]", "multiline": True}),
                "segments_json": ("STRING", {"default": "[]", "multiline": True}),
                "nodes_json": ("STRING", {"default": '{"source_video":"43","context_video":"101","plan":"100","raw_output":"19","delivery_output":"108"}', "multiline": True}),
                "gates_json": ("STRING", {"default": '{"max_abs_phase_offset":2,"min_phase_ncc":0.3,"max_seam_diff":0.04,"min_sharpness_ratio":0.75,"min_source_rms_difference":8.0}', "multiline": True}),
                "raw_frames": ("INT", {"default": 124, "min": 22, "max": 362, "step": 17}),
                "context_frames": ([1, 5, 22, 39], {"default": 22}),
                "width": ("INT", {"default": 576, "min": 32, "max": 4096, "step": 32}),
                "height": ("INT", {"default": 1024, "min": 32, "max": 4096, "step": 32}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "seed_base": ("INT", {"default": 730000, "min": 1, "max": 2147483647}),
                "timeout_seconds": ("INT", {"default": 5400, "min": 60, "max": 86400}),
                "taper_alpha": ("FLOAT", {"default": 0.45, "min": 0.0, "max": 1.0, "step": 0.01}),
                "taper_alpha_end": ("FLOAT", {"default": 0.10, "min": 0.0, "max": 1.0, "step": 0.01}),
                "taper_ramp_frames": ("INT", {"default": 3, "min": 1, "max": 39}),
            }
        }

    def build(
        self,
        workflow_path: str,
        comfy_url: str,
        comfy_input_dir: str,
        comfy_output_dir: str,
        run_dir: str,
        initial_delivery: str,
        reference_images_json: str,
        segments_json: str,
        nodes_json: str,
        gates_json: str,
        raw_frames: int,
        context_frames: int,
        width: int,
        height: int,
        steps: int,
        seed_base: int,
        timeout_seconds: int,
        taper_alpha: float,
        taper_alpha_end: float,
        taper_ramp_frames: int,
    ) -> tuple[str]:
        workflow = _require_file(workflow_path, "workflow")
        input_dir = _require_directory(comfy_input_dir, "ComfyUI input directory")
        output_dir = _require_directory(comfy_output_dir, "ComfyUI output directory")
        initial = _require_file(initial_delivery, "initial delivery")
        if not comfy_url.startswith(("http://", "https://")):
            raise H3ChainNodeError("comfy_url must begin with http:// or https://")
        _validate_h3_settings(raw_frames, context_frames, width, height, steps)
        if not 0.0 <= taper_alpha_end <= taper_alpha <= 1.0:
            raise H3ChainNodeError("taper values must satisfy 0 <= alpha_end <= alpha <= 1")
        if taper_ramp_frames > context_frames:
            raise H3ChainNodeError("taper_ramp_frames cannot exceed context_frames")

        references = _validate_reference_images(_parse_json(reference_images_json, "reference_images_json", (list, dict)))
        segments = _validate_segments(_parse_json(segments_json, "segments_json", list))
        nodes = _parse_json(nodes_json, "nodes_json", dict)
        gates = _parse_json(gates_json, "gates_json", dict)
        run_root = _path(run_dir)
        payload = {
            "workflow": str(workflow),
            "comfy_url": comfy_url.rstrip("/"),
            "comfy_input_dir": str(input_dir),
            "comfy_output_dir": str(output_dir),
            "run_dir": str(run_root),
            "initial_delivery": str(initial),
            "reference_images": references,
            "raw_frames": raw_frames,
            "context_frames": context_frames,
            "steps": steps,
            "width": width,
            "height": height,
            "seed_base": seed_base,
            "timeout_seconds": timeout_seconds,
            "taper": {
                "alpha": taper_alpha,
                "alpha_end": taper_alpha_end,
                "ramp_frames": taper_ramp_frames,
            },
            "gates": gates,
            "nodes": nodes,
            "segments": segments,
        }
        config_path = run_root / "chain-config.json"
        _write_text_if_safe(config_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return (str(config_path),)


class H3FullVideoConfig:
    """Build a full-video config which derives source slices automatically."""

    CATEGORY = "H3 Chain"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("config_path",)
    FUNCTION = "build"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "source_video": ("STRING", {"forceInput": True}),
                "initial_workflow_path": ("STRING", {"default": str(ROOT / "assets" / "workflows" / "t3-ref2va-initial-template.json")}),
                "continuation_workflow_path": ("STRING", {"default": str(ROOT / "assets" / "workflows" / "t3-ref2va-context-taper-template.json")}),
                "comfy_url": ("STRING", {"default": "http://127.0.0.1:8188"}),
                "comfy_input_dir": ("STRING", {"default": ""}),
                "comfy_output_dir": ("STRING", {"default": ""}),
                "run_dir": ("STRING", {"default": str(ROOT / "runs" / "full-video")}),
                "reference_images_json": ("STRING", {"forceInput": True}),
                "prompt": ("STRING", {"default": "", "multiline": True}),
                "initial_nodes_json": ("STRING", {"default": '{"source_video":"43","ref2va":"20","noise":"12","scheduler":"14","output":"19"}', "multiline": True}),
                "continuation_nodes_json": ("STRING", {"default": '{"source_video":"43","context_video":"101","plan":"100","raw_output":"19","delivery_output":"108"}', "multiline": True}),
                "gates_json": ("STRING", {"default": '{"max_abs_phase_offset":2,"min_phase_ncc":0.3,"max_seam_diff":0.04,"min_sharpness_ratio":0.75,"min_source_rms_difference":8.0}', "multiline": True}),
                "raw_frames": ("INT", {"default": 124, "min": 22, "max": 362, "step": 17}),
                "context_frames": ([1, 5, 22, 39], {"default": 22}),
                "width": ("INT", {"default": 576, "min": 32, "max": 4096, "step": 32}),
                "height": ("INT", {"default": 1024, "min": 32, "max": 4096, "step": 32}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "seed_base": ("INT", {"default": 730000, "min": 1, "max": 2147483647}),
                "timeout_seconds": ("INT", {"default": 5400, "min": 60, "max": 86400}),
                "taper_alpha": ("FLOAT", {"default": 0.45, "min": 0.0, "max": 1.0, "step": 0.01}),
                "taper_alpha_end": ("FLOAT", {"default": 0.10, "min": 0.0, "max": 1.0, "step": 0.01}),
                "taper_ramp_frames": ("INT", {"default": 3, "min": 1, "max": 39}),
            },
            "hidden": {"extra_pnginfo": "EXTRA_PNGINFO"},
        }

    def build(
        self,
        source_video: str,
        initial_workflow_path: str,
        continuation_workflow_path: str,
        comfy_url: str,
        comfy_input_dir: str,
        comfy_output_dir: str,
        run_dir: str,
        reference_images_json: str,
        prompt: str,
        initial_nodes_json: str,
        continuation_nodes_json: str,
        gates_json: str,
        raw_frames: int,
        context_frames: int,
        width: int,
        height: int,
        steps: int,
        seed_base: int,
        timeout_seconds: int,
        taper_alpha: float,
        taper_alpha_end: float,
        taper_ramp_frames: int,
        extra_pnginfo: dict[str, Any] | list[Any] | None = None,
    ) -> tuple[str]:
        source = _require_file(source_video, "source video")
        run_root = _path(run_dir)
        embedded = self._embedded_workflows(extra_pnginfo)
        if embedded is None:
            initial_workflow = _require_file(initial_workflow_path, "initial workflow")
            continuation_workflow = _require_file(continuation_workflow_path, "continuation workflow")
        else:
            embedded_dir = run_root / "embedded-workflows"
            initial_workflow = self._write_embedded_workflow(
                embedded_dir / "initial.json", embedded.get("initial"), "embedded initial workflow"
            )
            continuation_workflow = self._write_embedded_workflow(
                embedded_dir / "continuation.json",
                embedded.get("continuation"),
                "embedded continuation workflow",
            )
        input_dir = _require_directory(comfy_input_dir, "ComfyUI input directory")
        output_dir = _require_directory(comfy_output_dir, "ComfyUI output directory")
        if not comfy_url.startswith(("http://", "https://")):
            raise H3ChainNodeError("comfy_url must begin with http:// or https://")
        _validate_h3_settings(raw_frames, context_frames, width, height, steps)
        if not 0.0 <= taper_alpha_end <= taper_alpha <= 1.0:
            raise H3ChainNodeError("taper values must satisfy 0 <= alpha_end <= alpha <= 1")
        if taper_ramp_frames > context_frames:
            raise H3ChainNodeError("taper_ramp_frames cannot exceed context_frames")
        references = _validate_reference_images(
            _parse_json(reference_images_json, "reference_images_json", (list, dict))
        )
        initial_nodes = _parse_json(initial_nodes_json, "initial_nodes_json", dict)
        continuation_nodes = _parse_json(continuation_nodes_json, "continuation_nodes_json", dict)
        gates = _parse_json(gates_json, "gates_json", dict)
        payload = {
            "mode": "full_video",
            "source_video": str(source),
            "initial_workflow": str(initial_workflow),
            "continuation_workflow": str(continuation_workflow),
            "comfy_url": comfy_url.rstrip("/"),
            "comfy_input_dir": str(input_dir),
            "comfy_output_dir": str(output_dir),
            "run_dir": str(run_root),
            "reference_images": references,
            "prompt": prompt,
            "initial_nodes": initial_nodes,
            "continuation_nodes": continuation_nodes,
            "raw_frames": raw_frames,
            "context_frames": context_frames,
            "steps": steps,
            "width": width,
            "height": height,
            "seed_base": seed_base,
            "timeout_seconds": timeout_seconds,
            "taper": {
                "alpha": taper_alpha,
                "alpha_end": taper_alpha_end,
                "ramp_frames": taper_ramp_frames,
            },
            "gates": gates,
        }
        config_path = run_root / "full-video-config.json"
        _write_text_if_safe(config_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return (str(config_path),)

    @staticmethod
    def _embedded_workflows(extra_pnginfo: dict[str, Any] | list[Any] | None) -> dict[str, Any] | None:
        metadata: Any = extra_pnginfo
        if isinstance(metadata, list) and metadata:
            metadata = metadata[0]
        if not isinstance(metadata, dict):
            return None
        workflow = metadata.get("workflow")
        if not isinstance(workflow, dict):
            return None
        extra = workflow.get("extra")
        if not isinstance(extra, dict):
            return None
        embedded = extra.get("h3_embedded_workflows")
        return embedded if isinstance(embedded, dict) else None

    @staticmethod
    def _write_embedded_workflow(path: Path, value: Any, label: str) -> Path:
        if not isinstance(value, dict) or not value:
            raise H3ChainNodeError(f"{label} must be a non-empty API workflow object")
        for node_id, node in value.items():
            if not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
                raise H3ChainNodeError(f"{label} node {node_id!r} is malformed")
            if not isinstance(node.get("inputs"), dict):
                raise H3ChainNodeError(f"{label} node {node_id!r} has no inputs object")
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        _write_text_if_safe(path, text)
        return path.resolve()


class H3FullVideoOneClick:
    """Upload media, build the guarded full-video config, and launch it from one node."""

    CATEGORY = "H3 Chain"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("run_dir", "launch_status", "state_path")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        media = H3FullVideoInputs.INPUT_TYPES()["required"]
        return {
            "required": {
                **media,
                "prompt": ("STRING", {"default": "", "multiline": True}),
                "comfy_url": ("STRING", {"default": "http://127.0.0.1:8188"}),
                "run_dir": ("STRING", {"default": str(ROOT / "runs" / "full-video-one-click")}),
                "action": (["start", "approve_latest"], {"default": "start"}),
                "width": ("INT", {"default": 576, "min": 32, "max": 4096, "step": 32}),
                "height": ("INT", {"default": 1024, "min": 32, "max": 4096, "step": 32}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "seed_base": ("INT", {"default": 730000, "min": 1, "max": 2147483647}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs: Any) -> float:
        return float("nan")

    def run(
        self,
        source_video: str,
        character_front: str,
        character_side: str,
        character_back: str,
        character_face_closeup: str,
        prompt: str,
        comfy_url: str,
        run_dir: str,
        action: str,
        width: int,
        height: int,
        steps: int,
        seed_base: int,
    ) -> tuple[str, str, str]:
        if folder_paths is None:
            raise H3ChainNodeError("H3 Full Video One Click must run inside ComfyUI")
        source, references_json = H3FullVideoInputs().build(
            source_video,
            character_front,
            character_side,
            character_back,
            character_face_closeup,
        )
        config_path = H3FullVideoConfig().build(
            source_video=source,
            initial_workflow_path=str(ROOT / "assets" / "workflows" / "t3-ref2va-initial-template.json"),
            continuation_workflow_path=str(ROOT / "assets" / "workflows" / "t3-ref2va-context-taper-template.json"),
            comfy_url=comfy_url,
            comfy_input_dir=folder_paths.get_input_directory(),
            comfy_output_dir=folder_paths.get_output_directory(),
            run_dir=run_dir,
            reference_images_json=references_json,
            prompt=prompt,
            initial_nodes_json='{"source_video":"43","ref2va":"20","noise":"12","scheduler":"14","output":"19"}',
            continuation_nodes_json='{"source_video":"43","context_video":"101","plan":"100","raw_output":"19","delivery_output":"108"}',
            gates_json='{"max_abs_phase_offset":2,"min_phase_ncc":0.3,"max_seam_diff":0.04,"min_sharpness_ratio":0.75,"min_source_rms_difference":8.0}',
            raw_frames=124,
            context_frames=22,
            width=width,
            height=height,
            steps=steps,
            seed_base=seed_base,
            timeout_seconds=5400,
            taper_alpha=0.45,
            taper_alpha_end=0.10,
            taper_ramp_frames=3,
        )[0]
        return _launch_controller(config_path, action, FULL_VIDEO_RUNNER, "full-video config")


def _launch_controller(
    config_path: str,
    action: str,
    runner: Path,
    config_label: str,
) -> tuple[str, str, str]:
    _reap_controllers()
    if not runner.is_file() or not WORKER.is_file():
        raise H3ChainNodeError("The H3 controller is incomplete; reinstall this extension")
    config = _read_json(_require_file(config_path, config_label), config_label)
    run_dir_value = config.get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        raise H3ChainNodeError(f"{config_label} is missing run_dir")
    run_dir = _path(run_dir_value)
    state_path = run_dir / "STATE.json"
    lock_path = run_dir / ".h3_chain_controller.lock"
    run_dir.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        return (str(run_dir), "already_running", str(state_path))
    if action == "approve_latest":
        state = _read_json(state_path, "STATE.json")
        if state.get("status") != "needs_agent_review":
            raise H3ChainNodeError("approve_latest is only allowed after STATE.json says needs_agent_review")
    lock = {"config": str(_path(config_path)), "action": action, "runner": str(runner)}
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return (str(run_dir), "already_running", str(state_path))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(lock, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        command = [
            sys.executable, str(WORKER), "--runner", str(runner),
            "--config", str(_path(config_path)), "--lock", str(lock_path),
            "--log", str(run_dir / "controller.log"),
        ]
        if action == "approve_latest":
            command.append("--approve-latest")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        CONTROLLERS[process.pid] = process
        lock["pid"] = process.pid
        lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise
    return (str(run_dir), "started", str(state_path))


class H3ChainLaunch:
    """Start the guarded runner without blocking the active ComfyUI queue worker."""

    CATEGORY = "H3 Chain"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("run_dir", "launch_status", "state_path")
    FUNCTION = "launch"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "config_path": ("STRING", {"forceInput": True}),
                "action": (["start", "approve_latest"], {"default": "start"}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs: Any) -> float:
        return float("nan")

    def launch(self, config_path: str, action: str) -> tuple[str, str, str]:
        return _launch_controller(config_path, action, RUNNER, "chain config")


class H3FullVideoLaunch:
    """Launch source normalization, automatic segmentation, chaining, and assembly."""

    CATEGORY = "H3 Chain"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("run_dir", "launch_status", "state_path")
    FUNCTION = "launch"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "config_path": ("STRING", {"forceInput": True}),
                "action": (["start", "approve_latest"], {"default": "start"}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs: Any) -> float:
        return float("nan")

    def launch(self, config_path: str, action: str) -> tuple[str, str, str]:
        return _launch_controller(config_path, action, FULL_VIDEO_RUNNER, "full-video config")


class H3ChainStatus:
    """Read a chain state without accepting, rerolling, or mutating its lineage."""

    CATEGORY = "H3 Chain"
    RETURN_TYPES = ("STRING", "INT", "STRING")
    RETURN_NAMES = ("status", "completed_segments", "halt")
    FUNCTION = "read"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {"required": {"state_path": ("STRING", {"forceInput": True})}}

    def read(self, state_path: str) -> tuple[str, int, str]:
        path = _path(state_path)
        if not path.is_file():
            return ("not_started", 0, "STATE.json has not been written yet")
        state = _read_json(path, "STATE.json")
        completed = state.get("completed_segments", [])
        count = len(completed) if isinstance(completed, list) else 0
        halt = state.get("halt")
        halt_text = json.dumps(halt, ensure_ascii=False) if halt else ""
        return (str(state.get("status", "unknown")), count, halt_text)


class H3FullVideoStatus:
    """Read full-video progress and the final assembled output path."""

    CATEGORY = "H3 Chain"
    RETURN_TYPES = ("STRING", "INT", "INT", "STRING", "STRING")
    RETURN_NAMES = ("status", "completed_segments", "total_segments", "final_video", "halt")
    FUNCTION = "read"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {"required": {"state_path": ("STRING", {"forceInput": True})}}

    def read(self, state_path: str) -> tuple[str, int, int, str, str]:
        path = _path(state_path)
        if not path.is_file():
            return ("not_started", 0, 0, "", "STATE.json has not been written yet")
        state = _read_json(path, "STATE.json")
        halt = state.get("halt")
        halt_text = json.dumps(halt, ensure_ascii=False) if halt else ""
        return (
            str(state.get("status", "unknown")),
            int(state.get("completed_segment_count", 0)),
            int(state.get("total_segments", 0)),
            str(state.get("final_video", "")),
            halt_text,
        )


class H3FullVideoDiagnostics:
    """Expose the active stage and the latest background-controller failure."""

    CATEGORY = "H3 Chain"
    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "STRING", "STRING")
    RETURN_NAMES = ("stage", "active_segment", "completed_segments", "total_segments", "problem", "final_video")
    FUNCTION = "read"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {"required": {"state_path": ("STRING", {"forceInput": True})}}

    @classmethod
    def IS_CHANGED(cls, **kwargs: Any) -> float:
        return float("nan")

    def read(self, state_path: str) -> tuple[str, str, int, int, str, str]:
        path = _path(state_path)
        if not path.is_file():
            return ("not_started", "", 0, 0, "STATE.json has not been written yet", "")
        state = _read_json(path, "STATE.json")
        run_dir = path.parent
        stage = str(state.get("status", "unknown"))
        completed = int(state.get("completed_segment_count", 0))
        total = int(state.get("total_segments", 0))
        final_video = str(state.get("final_video", ""))
        active = state.get("active_segment")
        active_name = str(active.get("name", "")) if isinstance(active, dict) else ""
        problem = ""

        continuation_path = run_dir / "continuation" / "STATE.json"
        if continuation_path.is_file():
            continuation = _read_json(continuation_path, "continuation STATE.json")
            continuation_active = continuation.get("active_segment")
            if isinstance(continuation_active, dict):
                active_name = str(continuation_active.get("name", active_name))
            continuation_completed = continuation.get("completed_segments")
            if isinstance(continuation_completed, list):
                completed = max(completed, 1 + len(continuation_completed))

        halt = state.get("halt")
        if isinstance(halt, dict):
            problem = json.dumps(halt, ensure_ascii=False)

        lock_exists = (run_dir / ".h3_chain_controller.lock").exists()
        result_path = run_dir / "controller-result.json"
        if not lock_exists and result_path.is_file() and stage not in {"completed", "needs_agent_review"}:
            result = _read_json(result_path, "controller-result.json")
            if int(result.get("exit_code", 0)) not in {0, 2}:
                stage = "failed"
                log_path = run_dir / "controller.log"
                if log_path.is_file():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    errors = [line for line in lines if line.startswith("ERROR:")]
                    problem = errors[-1] if errors else "Background controller exited with an error"
        return (stage, active_name, completed, total, problem, final_video)
