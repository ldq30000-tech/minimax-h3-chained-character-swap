"""Non-blocking ComfyUI nodes for the guarded H3 continuation runner.

The runner submits continuation workflows back to ComfyUI. Running it inline in
the current graph would deadlock a single-worker queue, so the launch node
starts a separate controller process and returns immediately.
"""

from __future__ import annotations

import json
import hashlib
import io
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
SOURCE_FINGERPRINT_CACHE: dict[tuple[str, int, int, float, float], str] = {}
SOURCE_AUDIO_CACHE: dict[str, tuple[Any, int] | None] = {}


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


def _bounded_native_scene_lengths(
    inference_frames: int,
    raw_frames: int,
    context_frames: int,
) -> list[int] | None:
    """Return the fewest >=90-frame scenes without exceeding raw_frames."""
    valid_lengths = list(range(90, raw_frames + 1, 17))
    max_remaining = inference_frames - min(valid_lengths)
    if max_remaining < 0:
        return None
    continuation_options = [
        (length, length - context_frames) for length in reversed(valid_lengths)
    ]
    min_tail_scenes: list[int | None] = [None] * (max_remaining + 1)
    min_tail_scenes[0] = 0
    for delivered in range(1, max_remaining + 1):
        counts = [
            min_tail_scenes[delivered - contribution]
            for _, contribution in continuation_options
            if delivered >= contribution
            and min_tail_scenes[delivered - contribution] is not None
        ]
        if counts:
            min_tail_scenes[delivered] = min(counts) + 1

    best: list[int] | None = None
    for first in reversed(valid_lengths):
        remaining = inference_frames - first
        if remaining < 0 or min_tail_scenes[remaining] is None:
            continue
        tail: list[int] = []
        while remaining:
            count = min_tail_scenes[remaining]
            for length, contribution in continuation_options:
                previous = remaining - contribution
                if (
                    previous >= 0
                    and min_tail_scenes[previous] is not None
                    and min_tail_scenes[previous] == count - 1
                ):
                    tail.append(length)
                    remaining = previous
                    break
            else:  # pragma: no cover - guarded by the dynamic program above
                raise H3ChainNodeError("cannot reconstruct bounded H3 scene plan")
        candidate = [first, *tail]
        if best is None or (
            len(candidate), tuple(-value for value in candidate)
        ) < (
            len(best), tuple(-value for value in best)
        ):
            best = candidate

    return best


def _native_loop_lengths(
    source_frames: int,
    raw_frames: int = 124,
    context_frames: int = 22,
) -> tuple[list[int], int]:
    """Plan bounded H3-valid scene lengths and inference-only end padding."""
    _validate_h3_settings(raw_frames, context_frames, 32, 32, 1)
    if source_frames < 1:
        raise H3ChainNodeError("source video must contain at least one 24 fps frame")

    inference_frames = source_frames + ((5 - source_frames) % 17)
    if inference_frames <= raw_frames:
        return [max(5, inference_frames)], inference_frames - source_frames

    # The old greedy tail fold could turn [124, 124, 124, 56] into
    # [124, 124, 158], crossing the memory cliff on 12 GB GPUs. Search a few
    # H3-grid padding increments for a fully bounded plan; Exact Trim removes
    # those cloned end frames from delivery.
    lengths = None
    if context_frames != 1:
        for _ in range(17):
            lengths = _bounded_native_scene_lengths(
                inference_frames, raw_frames, context_frames
            )
            if lengths is not None:
                break
            inference_frames += 17

    if lengths is None:
        # Context length 1 has a sparse modular grid. Retain the valid legacy
        # fallback instead of adding a potentially very long inference tail.
        delivered_per_continuation = raw_frames - context_frames
        remaining = inference_frames - raw_frames
        lengths = [raw_frames]
        while remaining > delivered_per_continuation:
            lengths.append(raw_frames)
            remaining -= delivered_per_continuation
        final_raw = remaining + context_frames
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


def _native_video_metadata(source_video: Any) -> tuple[int, float, float]:
    """Read timing metadata without materializing the source frame tensor."""
    getters = {
        "frame count": getattr(source_video, "get_frame_count", None),
        "frame rate": getattr(source_video, "get_frame_rate", None),
        "duration": getattr(source_video, "get_duration", None),
    }
    missing = [name for name, value in getters.items() if not callable(value)]
    if missing:
        raise H3ChainNodeError(
            "source VIDEO does not support streamed metadata (%s); update "
            "ComfyUI before running the low-memory workflow" % ", ".join(missing)
        )
    try:
        frame_count = int(getters["frame count"]())
        frame_rate = float(getters["frame rate"]())
        duration = float(getters["duration"]())
    except Exception as exc:
        raise H3ChainNodeError(f"cannot read source video metadata: {exc}") from exc
    if frame_count < 1:
        raise H3ChainNodeError("source video contains no frames")
    if not math.isfinite(frame_rate) or frame_rate <= 0:
        raise H3ChainNodeError("source video frame rate must be positive")
    if not math.isfinite(duration) or duration <= 0:
        raise H3ChainNodeError("source video duration must be positive")
    return frame_count, frame_rate, duration


def _native_video_stream(source_video: Any) -> Any:
    getter = getattr(source_video, "get_stream_source", None)
    if not callable(getter):
        raise H3ChainNodeError(
            "source VIDEO does not expose a stream source; update ComfyUI before "
            "running the low-memory workflow"
        )
    try:
        source = getter()
    except Exception as exc:
        raise H3ChainNodeError(f"cannot open source video stream: {exc}") from exc
    if isinstance(source, (str, os.PathLike)):
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise H3ChainNodeError(f"source video stream does not exist: {path}")
        return path
    if not callable(getattr(source, "read", None)):
        raise H3ChainNodeError("source video stream must be a file path or byte stream")
    return source


def _native_video_fingerprint(source_video: Any) -> str:
    """Hash the encoded source incrementally so checkpoint lineage stays stable."""
    source = _native_video_stream(source_video)
    trim_getter = getattr(source_video, "get_active_trim_window", None)
    start_time, duration = (0.0, 0.0)
    if callable(trim_getter):
        start_time, duration = (float(value) for value in trim_getter())
    digest = hashlib.sha256()
    digest.update(f"trim:{start_time:.9f}:{duration:.9f}\n".encode("ascii"))
    if isinstance(source, Path):
        stat = source.stat()
        key = (str(source), stat.st_size, stat.st_mtime_ns, start_time, duration)
        cached = SOURCE_FINGERPRINT_CACHE.get(key)
        if cached is not None:
            return cached
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        fingerprint = digest.hexdigest()
        SOURCE_FINGERPRINT_CACHE[key] = fingerprint
        return fingerprint

    position = source.tell() if callable(getattr(source, "tell", None)) else None
    try:
        if callable(getattr(source, "seek", None)):
            source.seek(0)
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    finally:
        if position is not None and callable(getattr(source, "seek", None)):
            source.seek(position)
    return digest.hexdigest()


def _decode_native_audio(source_video: Any, fingerprint: str) -> tuple[Any, int] | None:
    """Decode only audio packets; video frames remain on disk."""
    if fingerprint in SOURCE_AUDIO_CACHE:
        return SOURCE_AUDIO_CACHE[fingerprint]
    try:
        import av
        import numpy as np
        import torch
    except ImportError as exc:
        raise H3ChainNodeError(
            "PyAV, NumPy, and PyTorch are required for streamed source audio"
        ) from exc

    source = _native_video_stream(source_video)
    if isinstance(source, Path):
        source = str(source)
    elif isinstance(source, io.BytesIO):
        source.seek(0)
    try:
        with av.open(source, mode="r") as container:
            if not container.streams.audio:
                SOURCE_AUDIO_CACHE[fingerprint] = None
                return None
            audio_stream = container.streams.audio[0]
            resampler = av.audio.resampler.AudioResampler(format="fltp")
            chunks = []
            sample_rate = int(audio_stream.rate or 0)
            for packet in container.demux(audio_stream):
                for decoded in packet.decode():
                    for frame in resampler.resample(decoded):
                        sample_rate = int(frame.sample_rate or sample_rate)
                        chunks.append(np.asarray(frame.to_ndarray()))
            for frame in resampler.resample(None):
                sample_rate = int(frame.sample_rate or sample_rate)
                chunks.append(np.asarray(frame.to_ndarray()))
    except Exception as exc:
        raise H3ChainNodeError(f"cannot decode source audio stream: {exc}") from exc
    if not chunks or sample_rate < 1:
        SOURCE_AUDIO_CACHE[fingerprint] = None
        return None
    waveform = torch.from_numpy(np.concatenate(chunks, axis=-1)).unsqueeze(0).float()
    trim_getter = getattr(source_video, "get_active_trim_window", None)
    start_time, duration = (0.0, 0.0)
    if callable(trim_getter):
        start_time, duration = (float(value) for value in trim_getter())
    sample_start = max(0, int(round(start_time * sample_rate)))
    sample_end = int(waveform.shape[-1])
    if duration > 0:
        sample_end = min(sample_end, sample_start + int(round(duration * sample_rate)))
    waveform = waveform[..., sample_start:sample_end].contiguous()
    decoded_audio = (waveform, sample_rate)
    SOURCE_AUDIO_CACHE[fingerprint] = decoded_audio
    return decoded_audio


def _slice_timeline_audio(audio: Any, start_frame: int, length: int) -> dict[str, Any]:
    waveform, sample_rate = _audio_waveform(audio, "streamed source audio")
    sample_start = int(round(start_frame / 24.0 * sample_rate))
    sample_end = int(round((start_frame + length) / 24.0 * sample_rate))
    if sample_start < 0 or sample_end > int(waveform.shape[-1]):
        raise H3ChainNodeError(
            f"streamed source audio window {sample_start}:{sample_end} exceeds "
            f"{int(waveform.shape[-1])} samples"
        )
    return {
        "waveform": waveform[..., sample_start:sample_end],
        "sample_rate": sample_rate,
    }


class H3NativeLongVideoPrepare:
    """Build a long-video plan without materializing the full frame timeline."""

    CATEGORY = "H3 Chain/Native Loop"
    RETURN_TYPES = (
        "H3_NATIVE_VIDEO_TIMELINE", "AUDIO", "AUDIO", "STRING", "INT",
        "INT", "INT", "STRING", "STRING",
    )
    RETURN_NAMES = (
        "source_timeline",
        "inference_audio",
        "source_audio",
        "plan_json",
        "source_frame_count",
        "inference_frame_count",
        "segment_count",
        "status",
        "source_fingerprint",
    )
    FUNCTION = "prepare"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "source_video": ("VIDEO",),
                "prompt": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False}),
                "raw_frames": ("INT", {"default": 107, "min": 90, "max": 362, "step": 17}),
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
    ) -> tuple[Any, dict[str, Any], dict[str, Any], str, int, int, int, str, str]:
        try:
            import torch
        except ImportError as exc:
            raise H3ChainNodeError("PyTorch is required to prepare the H3 source timeline") from exc

        decoded_frame_count, source_fps, source_duration = _native_video_metadata(
            source_video
        )
        source_frame_count = max(
            1, int(round(decoded_frame_count / source_fps * 24.0))
        )
        source_fingerprint = _native_video_fingerprint(source_video)

        plan, padding = _native_loop_plan(
            source_frame_count, prompt, raw_frames, context_frames, steps, base_seed
        )
        inference_frame_count = source_frame_count + padding

        decoded_audio = _decode_native_audio(source_video, source_fingerprint)
        if decoded_audio is None:
            sample_rate = 44100
            source_samples = max(
                1, int(round(source_frame_count / 24.0 * sample_rate))
            )
            source_waveform = torch.zeros(
                (1, 1, source_samples), dtype=torch.float32
            )
            audio_status = "audio=missing -> 44.1 kHz mono silence"
        else:
            source_waveform, sample_rate = decoded_audio
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
            "h3_audio_source": "silence_fallback" if decoded_audio is None else "source",
        }
        timeline = {
            "format": "h3_native_video_timeline_v2",
            "video": source_video,
            "source_fps": source_fps,
            "source_duration": source_duration,
            "decoded_frame_count": decoded_frame_count,
            "source_frame_count": source_frame_count,
            "inference_frame_count": inference_frame_count,
            "padding_frames": padding,
            "fingerprint": source_fingerprint,
        }
        lengths = [shot["length"] for shot in plan["shots"]]
        status = (
            f"streamed metadata: {decoded_frame_count} frames at {source_fps:.6g} fps -> "
            f"{source_frame_count} unique frames at 24 fps; scenes={lengths}; "
            f"inference={inference_frame_count} frames; end padding={padding} frames; "
            f"{audio_status}; full source frames remain on disk"
        )
        return (
            timeline,
            inference_audio,
            original_audio,
            json.dumps(plan, ensure_ascii=False, indent=2),
            source_frame_count,
            inference_frame_count,
            len(lengths),
            status,
            source_fingerprint,
        )


class H3NativeLongVideoScene:
    """Decode only the current Plan scene from a native source timeline."""

    CATEGORY = "H3 Chain/Native Loop"
    RETURN_TYPES = ("IMAGE", "AUDIO", "INT", "STRING")
    RETURN_NAMES = ("scene_frames", "scene_audio", "source_start_frame", "status")
    FUNCTION = "decode"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "source_timeline": ("H3_NATIVE_VIDEO_TIMELINE",),
                "state": ("H3_CHAIN_STATE",),
                "inference_audio": ("AUDIO",),
            }
        }

    def decode(
        self,
        source_timeline: Any,
        state: Any,
        inference_audio: Any,
    ) -> tuple[Any, dict[str, Any], int, str]:
        if not isinstance(source_timeline, dict) or source_timeline.get(
            "format"
        ) != "h3_native_video_timeline_v2":
            raise H3ChainNodeError("invalid streamed H3 source timeline")
        if not isinstance(state, dict) or not isinstance(state.get("plan"), dict):
            raise H3ChainNodeError("streamed scene requires H3 Chain Current state")
        try:
            import torch
        except ImportError as exc:
            raise H3ChainNodeError("PyTorch is required to decode an H3 source scene") from exc

        index = int(state.get("index", 0))
        shots = state["plan"].get("shots")
        if not isinstance(shots, list) or index < 1 or index > len(shots):
            raise H3ChainNodeError(f"invalid H3 scene index {index}")
        shot = shots[index - 1]
        source_start = int(shot.get("generation_start_frame", -1))
        length = int(shot.get("raw_frames", -1))
        source_frame_count = int(source_timeline["source_frame_count"])
        inference_frame_count = int(source_timeline["inference_frame_count"])
        decoded_frame_count = int(source_timeline["decoded_frame_count"])
        source_fps = float(source_timeline["source_fps"])
        if source_start < 0 or length < 1 or source_start + length > inference_frame_count:
            raise H3ChainNodeError(
                f"scene {index} source window {source_start}:{source_start + length} "
                f"exceeds inference timeline {inference_frame_count}"
            )

        normalized_indices = torch.arange(
            source_start, source_start + length, dtype=torch.float64
        ).clamp(max=source_frame_count - 1)
        source_indices = (
            normalized_indices * (source_fps / 24.0)
        ).floor().to(dtype=torch.long).clamp(max=decoded_frame_count - 1)
        first_source = int(source_indices[0])
        last_source = int(source_indices[-1])
        relative_indices = source_indices - first_source
        video = source_timeline["video"]
        trimmer = getattr(video, "as_trimmed", None)
        if not callable(trimmer):
            raise H3ChainNodeError(
                "source VIDEO does not support streamed trimming; update ComfyUI"
            )
        duration = (last_source - first_source + 1) / source_fps
        try:
            trimmed = trimmer(first_source / source_fps, duration, False)
            if trimmed is None:
                raise H3ChainNodeError(
                    f"cannot trim source frames {first_source}:{last_source + 1}"
                )
            components = trimmed.get_components()
        except H3ChainNodeError:
            raise
        except Exception as exc:
            raise H3ChainNodeError(
                f"cannot decode streamed source scene {index}: {exc}"
            ) from exc
        frames = getattr(components, "images", None)
        if getattr(frames, "ndim", None) != 4 or int(frames.shape[0]) < 1:
            raise H3ChainNodeError(
                f"streamed source scene {index} returned no IMAGE frames"
            )
        required_local = int(relative_indices.max()) + 1
        if int(frames.shape[0]) < required_local:
            raise H3ChainNodeError(
                f"streamed source scene {index} decoded {int(frames.shape[0])} "
                f"frames but exact source indices require {required_local}; "
                "unique source timestamps will not be duplicated"
            )
        selected = frames.index_select(
            0, relative_indices.to(device=frames.device)
        )
        scene_audio = _slice_timeline_audio(inference_audio, source_start, length)
        mib = int(selected.numel()) * int(selected.element_size()) / (1024 * 1024)
        status = (
            f"scene {index}/{len(shots)}: timeline {source_start}:{source_start + length}; "
            f"decoded source {first_source}:{last_source + 1}; "
            f"resident scene batch={mib:.1f} MiB"
        )
        return selected, scene_audio, source_start, status


class H3NativeGenerationFingerprint:
    """Combine static identity and encoded source fingerprints for checkpoints."""

    CATEGORY = "H3 Chain/Native Loop"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("generation_fingerprint", "status")
    FUNCTION = "combine"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, Any]]:
        return {
            "required": {
                "identity_fingerprint": ("STRING", {"forceInput": True}),
                "source_fingerprint": ("STRING", {"forceInput": True}),
            }
        }

    def combine(
        self, identity_fingerprint: str, source_fingerprint: str
    ) -> tuple[str, str]:
        identity = str(identity_fingerprint or "").strip()
        source = str(source_fingerprint or "").strip()
        if not identity or not source:
            raise H3ChainNodeError("identity and source fingerprints are required")
        value = hashlib.sha256(
            json.dumps(
                {"identity": identity, "source_video": source},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return value, f"identity + streamed source: {value[:12]}"


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
