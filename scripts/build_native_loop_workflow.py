"""Build one visible native recursive H3 long-video character-swap workflow."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any


DEFAULT_SOURCE = "9b1c0f6cad781c86229e2782af56bf45.mp4"
DEFAULT_IMAGES = [
    "01a005ab-d726-70d4-9736-0fc1857e5a55.png",
    "01a005af-47e3-75bb-a388-b85a2192d475.png",
    "01a005b1-ef88-7469-858f-d69858d3e5c4.png",
    "01a005c1-2085-75eb-ba2a-aa4f51a61b34.png",
]

PROMPT = """subject_definitions:
@character_front, @character_side, and @character_back define the exact target character from the front, side, and back, including identity, face, hair, body proportions, skin, clothing, accessories, and silhouette.
@character_face is the high-resolution close-up identity reference for the target character's facial structure, eyes, hairline, and close-range details.
@motion is the current sequential slice of the original source video. It owns the source performer's pose sequence, action, speed, rhythm, screen position, body scale, framing, camera movement, shot timing, lighting, environment, and edit structure at every timestamp.
@motion_audio is the synchronized source-audio slice paired with @motion. Use it only to keep visual timing synchronized; the exact original source track is restored after final assembly.
<Subject 1> is the target character defined jointly by @character_front, @character_side, @character_back, and @character_face. <Subject 1> completely replaces the main source performer for the entire current source slice.
<Subject 2> is the environment, camera, lighting, background, props, and all non-performer content in @motion.

summary:
[video editing] Replace the main performer in every shot and after every visible cut in @motion with <Subject 1>. Preserve the original source timeline, actions, pacing, composition, camera work, environment, and edit timing one-to-one.

retention_analysis:
<Subject 1> (appears throughout every shot in the current source slice): attribute_transfer - use the complete target identity and outfit from @character_front, @character_side, @character_back, and @character_face while following the source performer's body position, pose sequence, movement direction, speed, timing, and rhythm from @motion one-to-one.
<Subject 2> (appears throughout every shot in the current source slice): fully_preserved - preserve environment geometry, lighting direction and exposure, camera path, lens behavior, framing, background motion, props, and shot structure from @motion.
@motion (complete target timeline and shot structure): fully_preserved - its temporal order, every cut, motion phase, pace, framing, and camera path define every physical output frame.
@motion_audio: fully_preserved in delivery - it is timing guidance during generation and the original full source waveform is copied back after the exact final trim.

detailed_description:
[Shot 1] <Subject 1> replaces the source performer throughout the current sequential @motion slice, including after any hard cut or new camera angle inside the slice. Keep the same body scale, ground position, framing, scene geometry, lighting, camera movement, and occlusion order as @motion. Every pose, transition, movement direction, speed, facial orientation, and rhythm follows @motion at the same timestamp. The 22-frame head overlap on continuation scenes reconstructs the incoming Motion Context and is removed before delivery. Motion must advance into new source timestamps without restarting frame zero, pausing, replaying, compressing time, skipping forward, or anticipating a segment boundary. Keep the background clean, stable, correctly exposed, and free of chromatic noise, color blotches, identity leakage, and compression artifacts.

overall_soundscape:
Preserve the original source soundtrack exactly in final delivery. Do not replace dialogue, music, ambience, or timing; @motion_audio is synchronized guidance only during Ref2VA generation.

non_diegetic_music:
N/A - do not generate replacement music; the original source track is muxed after assembly."""


def _node(workflow: dict[str, Any], node_id: int) -> dict[str, Any]:
    return next(node for node in workflow["nodes"] if int(node["id"]) == node_id)


def _input_index(node: dict[str, Any], name: str) -> int:
    return next(index for index, item in enumerate(node.get("inputs", [])) if item["name"] == name)


def _drop_links_touching(workflow: dict[str, Any], node_ids: set[int]) -> None:
    workflow["links"] = [
        link for link in workflow["links"]
        if int(link[1]) not in node_ids and int(link[3]) not in node_ids
    ]


def _connect(
    workflow: dict[str, Any],
    source_id: int,
    source_slot: int,
    target_id: int,
    target_name: str,
    link_type: str,
) -> None:
    target = _node(workflow, target_id)
    try:
        target_slot = _input_index(target, target_name)
    except StopIteration:
        target.setdefault("inputs", []).append({"name": target_name, "type": link_type, "link": None})
        target_slot = len(target["inputs"]) - 1
    workflow["links"] = [
        link for link in workflow["links"]
        if not (int(link[3]) == target_id and int(link[4]) == target_slot)
    ]
    link_id = max((int(link[0]) for link in workflow["links"]), default=0) + 1
    workflow["links"].append(
        [link_id, source_id, source_slot, target_id, target_slot, link_type]
    )
    target["inputs"][target_slot]["link"] = link_id


def _rebuild_link_fields(workflow: dict[str, Any]) -> None:
    nodes = {int(node["id"]): node for node in workflow["nodes"]}
    for node in nodes.values():
        for item in node.get("inputs", []):
            item["link"] = None
        for item in node.get("outputs", []):
            item["links"] = None
    output_links: dict[tuple[int, int], list[int]] = {}
    for link in workflow["links"]:
        link_id, source_id, source_slot, target_id, target_slot, _ = link
        nodes[int(target_id)]["inputs"][int(target_slot)]["link"] = int(link_id)
        output_links.setdefault((int(source_id), int(source_slot)), []).append(int(link_id))
    for (source_id, source_slot), link_ids in output_links.items():
        nodes[source_id]["outputs"][source_slot]["links"] = link_ids
    workflow["last_link_id"] = max((int(link[0]) for link in workflow["links"]), default=0)


def _load_image(node_id: int, x: int, filename: str, title: str, order: int) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "LoadImage",
        "pos": [x, -256],
        "size": [416, 448],
        "flags": {},
        "order": order,
        "mode": 0,
        "inputs": [],
        "outputs": [
            {"name": "IMAGE", "type": "IMAGE", "links": None},
            {"name": "MASK", "type": "MASK", "links": None},
        ],
        "title": title,
        "properties": {"Node name for S&R": "LoadImage"},
        "widgets_values": [filename, "image"],
        "color": "#744c8c",
        "bgcolor": "rgba(24,24,27,.9)",
    }


def _tagged_picture(
    node_id: int, x: int, tag: str, title: str, order: int, has_previous: bool
) -> dict[str, Any]:
    inputs = [{"name": "image", "type": "IMAGE", "link": None}]
    if has_previous:
        inputs.append({"name": "previous", "shape": 7, "type": "H3_TAGGED_REFERENCES", "link": None})
    else:
        inputs.append({"name": "previous", "shape": 7, "type": "H3_TAGGED_REFERENCES", "link": None})
    return {
        "id": node_id,
        "type": "MiniMaxH3TaggedPictureReference",
        "pos": [x, 256],
        "size": [416, 192],
        "flags": {},
        "order": order,
        "mode": 0,
        "inputs": inputs,
        "outputs": [
            {"name": "references", "type": "H3_TAGGED_REFERENCES", "links": None},
            {"name": "reference_fingerprint", "type": "STRING", "links": None},
            {"name": "status", "type": "STRING", "links": None},
        ],
        "title": title,
        "properties": {"Node name for S&R": "MiniMaxH3TaggedPictureReference"},
        "widgets_values": [tag],
        "color": "#744c8c",
        "bgcolor": "rgba(24,24,27,.9)",
    }


def _note(node_id: int, pos: list[int], size: list[int], title: str, text: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "Note",
        "pos": pos,
        "size": size,
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": [],
        "outputs": [],
        "title": title,
        "properties": {"Node name for S&R": "Note"},
        "widgets_values": [text],
        "color": "#2d4b58",
        "bgcolor": "rgba(24,24,27,.92)",
    }


def build(base_workflow: Path, source_name: str, image_names: list[str]) -> dict[str, Any]:
    workflow = json.loads(base_workflow.read_text(encoding="utf-8"))
    replace_ids = {1945, 1946, 1947, 1948, 1950, 1951, 1952}
    _drop_links_touching(workflow, replace_ids | {1635})
    workflow["nodes"] = [
        node for node in workflow["nodes"] if int(node["id"]) not in replace_ids
    ]

    model = _node(workflow, 1)
    model["widgets_values"] = [
        "minimax\\minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "default",
    ]
    clip = _node(workflow, 2)
    clip["widgets_values"] = [
        "minimax\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "minimax",
        "default",
    ]
    _node(workflow, 3)["widgets_values"] = "minimax\\minimax_h3_video_vae_fp16.safetensors"
    _node(workflow, 4)["widgets_values"] = "minimax\\minimax_h3_audio_vae_fp32.safetensors"
    _node(workflow, 122)["widgets_values"] = "res_multistep"
    _node(workflow, 123)["widgets_values"] = ["beta", 20, 1]

    placeholder_plan = {
        "defaults": {"steps": 20},
        "shots": [
            {"id": f"source_{index:02d}", "prompt": PROMPT, "length": length, "seed": str(730000 + index - 1)}
            for index, length in enumerate([124, 124, 124, 124, 124, 107], start=1)
        ],
    }
    plan = _node(workflow, 1700)
    plan["title"] = "AUTO PLAN - SOURCE FRAMES -> 6 SCENES / 617 INFERENCE FRAMES"
    plan["widgets_values"] = [
        json.dumps(placeholder_plan, ensure_ascii=False, separators=(",", ":")),
        "h3_native_loop_9b1c0f6c_614f",
        "",
        576,
        1024,
        22,
        "video",
        "head",
        "disabled",
        "source_track",
        22,
        5,
        20,
        730000,
        18,
        0,
        "guide",
    ]
    plan["inputs"] = [
        {"name": "generation_fingerprint", "type": "STRING", "widget": {"name": "generation_fingerprint"}, "link": None},
        {"name": "plan_json_input", "type": "STRING", "link": None},
    ]

    review = _node(workflow, 1944)
    review["title"] = "VISIBLE REVIEW GATE - DISABLED FOR ONE-QUEUE AUTO RUN"
    review["widgets_values"] = [False, False, 0, False, True, "checkpointed"]
    _node(workflow, 1706)["widgets_values"] = ["plan", "character_swap_assembled_617f", 256]
    _node(workflow, 1706)["title"] = "ASSEMBLE ALL LOOP SEGMENTS + PADDED SOURCE AUDIO"
    _node(workflow, 1708)["widgets_values"] = ["plan", "character_swap_recovered_617f", 256]
    _node(workflow, 1701)["title"] = "LOOP START - RUN ALL AUTO-PLANNED SCENES"
    _node(workflow, 1702)["title"] = "CURRENT SOURCE SLICE - SEQUENTIAL, NEVER RESTART FRAME 0"
    _node(workflow, 1703)["title"] = "MOTION CONTEXT - 22 FRAME HEAD OVERLAP"
    _node(workflow, 1704)["title"] = "SAVE CLEAN DELIVERED SEGMENT + CHECKPOINT"
    _node(workflow, 1705)["title"] = "LOOP END - RECURSIVELY CLONE BODY UNTIL FINAL SCENE"
    _node(workflow, 132)["title"] = "TRIM REPEATED 22-FRAME CONTEXT"
    _node(workflow, 132)["widgets_values"] = [0, 24, True, 0]
    _node(workflow, 110)["title"] = "TAGGED REF2VA - FOUR IDENTITY IMAGES + CURRENT SOURCE SLICE"
    _node(workflow, 110)["widgets_values"] = [PROMPT, 576, 1024, 124, "match", "strict"]

    disabled_lora = _node(workflow, 1635)
    disabled_lora.update({
        "pos": [-1360, -1050],
        "mode": 2,
        "inputs": [{"name": "model", "type": "MODEL", "link": None}],
        "title": "DISABLED - Turbo 4 STEPS / INCOMPATIBLE WITH PRUNED BASE",
        "widgets_values": ["minimax\\minimax_h3_turbo_4STEPS_comfyui.safetensors", 1.0],
        "color": "#6b3030",
        "bgcolor": "rgba(24,24,27,.9)",
    })

    new_nodes = [
        _load_image(1945, -4864, image_names[0], "1 - INPUT @character_front", 38),
        _load_image(1946, -4416, image_names[1], "2 - INPUT @character_side", 39),
        _load_image(1963, -3968, image_names[2], "3 - INPUT @character_back", 40),
        _load_image(1964, -3520, image_names[3], "4 - INPUT @character_face / CLOSE-UP", 41),
        _tagged_picture(1947, -4864, "character_front", "@character_front", 42, False),
        _tagged_picture(1948, -4416, "character_side", "@character_side", 43, True),
        _tagged_picture(1965, -3968, "character_back", "@character_back", 44, True),
        _tagged_picture(1966, -3520, "character_face", "@character_face", 45, True),
        {
            "id": 1950,
            "type": "LoadVideo",
            "pos": [-3008, 608],
            "size": [576, 416],
            "flags": {},
            "order": 46,
            "mode": 0,
            "inputs": [],
            "outputs": [{"name": "VIDEO", "type": "VIDEO", "links": None}],
            "title": "INPUT ORIGINAL LONG VIDEO - AUDIO OPTIONAL",
            "properties": {"Node name for S&R": "LoadVideo"},
            "widgets_values": [source_name, "image"],
        },
        {
            "id": 1960,
            "type": "H3NativeLongVideoPrepare",
            "pos": [-2368, 544],
            "size": [832, 896],
            "flags": {},
            "order": 47,
            "mode": 0,
            "inputs": [{"name": "source_video", "type": "VIDEO", "link": None}],
            "outputs": [
                {"name": "frames_24fps", "type": "IMAGE", "links": None},
                {"name": "inference_audio", "type": "AUDIO", "links": None},
                {"name": "source_audio", "type": "AUDIO", "links": None},
                {"name": "plan_json", "type": "STRING", "links": None},
                {"name": "source_frame_count", "type": "INT", "links": None},
                {"name": "inference_frame_count", "type": "INT", "links": None},
                {"name": "segment_count", "type": "INT", "links": None},
                {"name": "status", "type": "STRING", "links": None},
            ],
            "title": "AUTO 24 FPS + PLAN + PAD + SILENCE FALLBACK",
            "properties": {"Node name for S&R": "H3NativeLongVideoPrepare"},
            "widgets_values": [PROMPT, 124, 22, 20, 730000],
            "color": "#215c55",
            "bgcolor": "rgba(24,24,27,.95)",
        },
        {
            "id": 1951,
            "type": "MiniMaxH3ReferenceVideoPrepare",
            "pos": [-1472, 544],
            "size": [448, 256],
            "flags": {},
            "order": 48,
            "mode": 0,
            "inputs": [
                {"name": "length", "type": "INT", "widget": {"name": "length"}, "link": None},
                {"name": "source_frames", "shape": 7, "type": "IMAGE", "link": None},
                {"name": "source_audio", "shape": 7, "type": "AUDIO", "link": None},
            ],
            "outputs": [
                {"name": "ref_video", "type": "IMAGE", "links": None},
                {"name": "source_audio", "type": "AUDIO", "links": None},
                {"name": "length", "type": "INT", "links": None},
                {"name": "status", "type": "STRING", "links": None},
            ],
            "title": "FULL PADDED 24 FPS REFERENCE TIMELINE",
            "properties": {"Node name for S&R": "MiniMaxH3ReferenceVideoPrepare"},
            "widgets_values": [617, 24.0],
            "color": "#1f1f48",
            "bgcolor": "rgba(24,24,27,.9)",
        },
        {
            "id": 1952,
            "type": "MiniMaxH3TaggedVideoReference",
            "pos": [-1472, 832],
            "size": [480, 240],
            "flags": {},
            "order": 49,
            "mode": 0,
            "inputs": [
                {"name": "video", "type": "IMAGE", "link": None},
                {"name": "audio", "shape": 7, "type": "AUDIO", "link": None},
                {"name": "previous", "shape": 7, "type": "H3_TAGGED_REFERENCES", "link": None},
            ],
            "outputs": [
                {"name": "references", "type": "H3_TAGGED_REFERENCES", "links": None},
                {"name": "reference_fingerprint", "type": "STRING", "links": None},
                {"name": "status", "type": "STRING", "links": None},
            ],
            "title": "@motion + @motion_audio - SEQUENTIAL SOURCE TIMELINE",
            "properties": {"Node name for S&R": "MiniMaxH3TaggedVideoReference"},
            "widgets_values": ["motion", "motion_audio", "sequential"],
            "color": "#744c8c",
            "bgcolor": "rgba(24,24,27,.9)",
        },
        {
            "id": 1961,
            "type": "H3FinalTrimToSource",
            "pos": [5792, -256],
            "size": [512, 288],
            "flags": {},
            "order": 50,
            "mode": 0,
            "inputs": [
                {"name": "video_path", "type": "STRING", "link": None},
                {"name": "source_audio", "type": "AUDIO", "link": None},
                {"name": "source_frame_count", "type": "INT", "link": None},
            ],
            "outputs": [
                {"name": "final_video", "type": "STRING", "links": None},
                {"name": "status", "type": "STRING", "links": None},
            ],
            "title": "FINAL PLAYABLE PREVIEW + EXACT SOURCE-FRAME TRIM + AUDIO FALLBACK",
            "properties": {"Node name for S&R": "H3FinalTrimToSource"},
            "widgets_values": ["character_swap_full_exact_614f", 24.0, 256],
            "color": "#24533a",
            "bgcolor": "rgba(24,24,27,.96)",
        },
        {
            "id": 1962,
            "type": "LoraLoaderModelOnly",
            "pos": [-1008, -1050],
            "size": [320, 112],
            "flags": {},
            "order": 51,
            "mode": 2,
            "inputs": [{"name": "model", "type": "MODEL", "link": None}],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": None}],
            "title": "DISABLED - Turbo v4 / INCOMPATIBLE WITH PRUNED BASE",
            "properties": {"Node name for S&R": "LoraLoaderModelOnly"},
            "widgets_values": ["minimax\\minimax_h3_turbo_v4_step600_ema.safetensors", 1.0],
            "color": "#6b3030",
            "bgcolor": "rgba(24,24,27,.9)",
        },
        _note(
            1968,
            [-3008, 1480],
            [1472, 320],
            "DYNAMIC SOURCE PLAN",
            "The source is decoded once, resampled to 24 fps, and counted on the canvas. For the selected 25.6s source: 614 unique frames become 617 inference frames; only three cloned tail frames exist inside Ref2VA input. The recursive plan is 124,124,124,124,124,107 raw frames. Final Exact Trim removes those three inference-only frames and restores the original source audio, or same-duration silence when no audio track exists.",
        ),
        _note(
            1969,
            [3840, 1160],
            [960, 304],
            "REVIEW MODE",
            "Default is one-queue automatic execution, so Review Gate is visible but disabled. To inspect each 5s scene before the loop advances, enable Review Gate. The output is not complete until Loop End finishes every planned scene, Assemble joins them, and Final Exact Trim produces the final path.",
        ),
    ]
    workflow["nodes"].extend(new_nodes)

    _connect(workflow, 1941, 0, 5, "model", "MODEL")
    _connect(workflow, 1945, 0, 1947, "image", "IMAGE")
    _connect(workflow, 1946, 0, 1948, "image", "IMAGE")
    _connect(workflow, 1947, 0, 1948, "previous", "H3_TAGGED_REFERENCES")
    _connect(workflow, 1963, 0, 1965, "image", "IMAGE")
    _connect(workflow, 1948, 0, 1965, "previous", "H3_TAGGED_REFERENCES")
    _connect(workflow, 1964, 0, 1966, "image", "IMAGE")
    _connect(workflow, 1965, 0, 1966, "previous", "H3_TAGGED_REFERENCES")
    _connect(workflow, 1950, 0, 1960, "source_video", "VIDEO")
    _connect(workflow, 1960, 5, 1951, "length", "INT")
    _connect(workflow, 1960, 0, 1951, "source_frames", "IMAGE")
    _connect(workflow, 1960, 1, 1951, "source_audio", "AUDIO")
    _connect(workflow, 1951, 0, 1952, "video", "IMAGE")
    _connect(workflow, 1951, 1, 1952, "audio", "AUDIO")
    _connect(workflow, 1966, 0, 1952, "previous", "H3_TAGGED_REFERENCES")
    _connect(workflow, 1952, 0, 110, "references", "H3_TAGGED_REFERENCES")
    _connect(workflow, 1952, 1, 1700, "generation_fingerprint", "STRING")
    _connect(workflow, 1960, 3, 1700, "plan_json_input", "STRING")
    for target_id in (1701, 1702, 1706, 1707, 1708, 1944):
        _connect(workflow, 1960, 1, target_id, "source_audio", "AUDIO")
    _connect(workflow, 1706, 0, 1961, "video_path", "STRING")
    _connect(workflow, 1960, 2, 1961, "source_audio", "AUDIO")
    _connect(workflow, 1960, 4, 1961, "source_frame_count", "INT")

    for note_id, title, text in [
        (1902, "CHARACTER REPLACEMENT INPUTS", "Four identity images own the replacement character. The source video owns motion, timing, camera, environment, cuts, and the final soundtrack. Missing source audio is replaced with same-duration silence."),
        (1906, "NATIVE RECURSIVE LONG-VIDEO LOOP", "Plan -> Loop Start -> Current -> Tagged Ref2VA -> Motion Context -> Sampler -> Trim -> Segment Save -> Review -> Loop End -> Assemble -> Exact Trim. All expensive generation nodes are visible on this canvas."),
        (1932, "QUEUE BEHAVIOR", "Queue once. Loop End recursively clones the visible sampling body for every auto-planned scene. The first assembled path is 617 inference frames; the green Exact Trim node is the actual 614-frame delivery with original audio or silence fallback."),
    ]:
        node = _node(workflow, note_id)
        node["title"] = title
        node["widgets_values"] = [text]
    _node(workflow, 1902)["pos"] = [-2112, -1536]

    workflow["groups"] = [
        {"id": 1, "title": "H3 MODEL STACK - 20 STEPS / TURBO LORAS DISABLED", "bounding": [-2112, -1152, 2200, 800], "color": "#3f789e", "flags": {}},
        {"id": 2, "title": "FOUR CHARACTER IDENTITY INPUTS", "bounding": [-4928, -352, 1888, 896], "color": "#744c8c", "flags": {}},
        {"id": 3, "title": "SOURCE VIDEO -> AUTO 24 FPS / AUTO PLAN / SEQUENTIAL REFERENCE", "bounding": [-3072, 448, 2656, 1392], "color": "#2d7d66", "flags": {}},
        {"id": 4, "title": "VISIBLE RECURSIVE BODY - REF2VA / CONTEXT / SAMPLE / TRIM / SAVE / REVIEW / LOOP", "bounding": [-704, -352, 5728, 1856], "color": "#744c8c", "flags": {}},
        {"id": 5, "title": "PLAN EDITOR - DYNAMIC JSON OVERRIDES THE 614-FRAME EXAMPLE", "bounding": [-704, 480, 3216, 1280], "color": "#3f789e", "flags": {}},
        {"id": 6, "title": "FINAL ASSEMBLY + EXACT SOURCE-LENGTH DELIVERY", "bounding": [5056, -352, 1312, 704], "color": "#2f6b3f", "flags": {}},
        {"id": 7, "title": "DISABLED RECOVERY PATH", "bounding": [5056, 320, 1056, 736], "color": "#8a5a2b", "flags": {}},
    ]
    workflow["id"] = str(uuid.uuid4())
    workflow["revision"] = 0
    workflow["last_node_id"] = max(int(node["id"]) for node in workflow["nodes"])
    workflow.setdefault("extra", {})["ds"] = {"scale": 0.26, "offset": [4928, 1500]}
    workflow["extra"]["h3_native_loop"] = {
        "source_fps": 24,
        "source_frames": 614,
        "inference_frames": 617,
        "inference_padding_frames": 3,
        "raw_scene_lengths": [124, 124, 124, 124, 124, 107],
        "context_frames": 22,
        "final_exact_trim": True,
    }
    _rebuild_link_fields(workflow)
    return workflow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-workflow", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-name", default=DEFAULT_SOURCE)
    parser.add_argument("--front-name", default=DEFAULT_IMAGES[0])
    parser.add_argument("--side-name", default=DEFAULT_IMAGES[1])
    parser.add_argument("--back-name", default=DEFAULT_IMAGES[2])
    parser.add_argument("--face-name", default=DEFAULT_IMAGES[3])
    args = parser.parse_args()
    workflow = build(
        args.base_workflow,
        args.source_name,
        [args.front_name, args.side_name, args.back_name, args.face_name],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
