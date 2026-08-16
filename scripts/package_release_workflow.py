"""Create portable release workflows from user-authored ComfyUI canvases."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


INPUT_NAMES = {
    1945: "character_front.png",
    1946: "character_side.png",
    1963: "character_back.png",
    1964: "character_face_closeup.png",
    1950: "source_video.mp4",
}
TURBO_NODE_ID = 1972
SAGE_NODE_ID = 1970
ATTENTION_NODE_ID = 1941

USER_PROFILES = {
    "normal": {
        "variant": "user-no-audio-compatible-124f",
        "frame_cap": 124,
        "scene_lengths": [124, 124, 124, 124, 124, 107],
        "run_suffix": "user_no_audio_124f",
        "label": "124-FRAME USER PROFILE",
    },
    "low-vram": {
        "variant": "user-no-audio-compatible-low-vram-107f",
        "frame_cap": 107,
        "scene_lengths": [107, 107, 107, 107, 107, 107, 107],
        "run_suffix": "user_no_audio_low_vram_107f",
        "label": "107-FRAME LOW-VRAM USER PROFILE",
    },
}


def _node(workflow: dict[str, Any], node_id: int) -> dict[str, Any]:
    try:
        return next(
            node for node in workflow["nodes"] if int(node["id"]) == node_id
        )
    except StopIteration as exc:
        raise ValueError(f"workflow is missing required node {node_id}") from exc


def _input_index(node: dict[str, Any], name: str) -> int:
    try:
        return next(
            index
            for index, value in enumerate(node.get("inputs", []))
            if value["name"] == name
        )
    except StopIteration as exc:
        raise ValueError(f"node {node['id']} has no input named {name!r}") from exc


def _rebuild_link_fields(workflow: dict[str, Any]) -> None:
    nodes = {int(node["id"]): node for node in workflow["nodes"]}
    for node in nodes.values():
        for value in node.get("inputs", []):
            value["link"] = None
        for value in node.get("outputs", []):
            value["links"] = None

    output_links: dict[tuple[int, int], list[int]] = {}
    for link in workflow["links"]:
        link_id, source_id, source_slot, target_id, target_slot, _ = link
        nodes[int(target_id)]["inputs"][int(target_slot)]["link"] = int(link_id)
        output_links.setdefault((int(source_id), int(source_slot)), []).append(
            int(link_id)
        )
    for (source_id, source_slot), link_ids in output_links.items():
        nodes[source_id]["outputs"][source_slot]["links"] = link_ids
    workflow["last_link_id"] = max(
        (int(link[0]) for link in workflow["links"]), default=0
    )


def _genericize(workflow: dict[str, Any]) -> None:
    for node_id, filename in INPUT_NAMES.items():
        values = list(_node(workflow, node_id).get("widgets_values") or [])
        if not values:
            raise ValueError(f"input node {node_id} has no filename widget")
        values[0] = filename
        _node(workflow, node_id)["widgets_values"] = values

    prepare = _node(workflow, 1960)
    _node(workflow, 1950)["title"] = "INPUT ORIGINAL LONG VIDEO - AUDIO OPTIONAL"
    prepare["title"] = (
        "STREAM METADATA + AUTO PLAN + AUDIO FALLBACK - NO FULL FRAME BATCH"
    )
    prompt = str(prepare["widgets_values"][0])
    raw_lengths = [124, 124, 124, 124, 124, 107]
    placeholder_plan = {
        "defaults": {"steps": 20},
        "shots": [
            {
                "id": f"source_{index:02d}",
                "prompt": prompt,
                "length": length,
                "seed": str(730000 + index - 1),
            }
            for index, length in enumerate(raw_lengths, start=1)
        ],
    }
    plan = _node(workflow, 1700)
    plan_values = list(plan["widgets_values"])
    plan_values[0] = json.dumps(
        placeholder_plan, ensure_ascii=False, separators=(",", ":")
    )
    plan_values[1] = "h3_native_loop_streamed_release"
    plan["widgets_values"] = plan_values
    plan["title"] = "AUTO PLAN - DYNAMIC SOURCE FRAME COUNT"

    _node(workflow, 110)["widgets_values"] = [
        prompt,
        int(plan_values[3]),
        int(plan_values[4]),
        124,
        "match",
        "strict",
    ]
    _node(workflow, 1706)["widgets_values"] = [
        "plan",
        "character_swap_assembled_streamed",
        256,
    ]
    _node(workflow, 1708)["widgets_values"] = [
        "plan",
        "character_swap_recovered",
        256,
    ]
    final = _node(workflow, 1961)
    final["title"] = (
        "FINAL PLAYABLE PREVIEW + EXACT SOURCE-FRAME TRIM + AUDIO FALLBACK"
    )
    final["widgets_values"] = ["character_swap_full_exact_streamed", 24.0, 256]

    note_text = {
        1932: (
            "Queue once. Loop End recursively clones the visible sampling body "
            "for every auto-planned scene. The green Exact Trim node is the "
            "actual source-length delivery with original audio or silence fallback."
        ),
        1968: (
            "Only source metadata and audio are prepared globally. Every "
            "recursive scene decodes its exact 24 fps source window on demand, "
            "so full source frames remain on disk. H3-valid lengths and "
            "inference-only tail padding are computed automatically. Final "
            "Exact Trim restores original audio or same-duration silence."
        ),
    }
    for node_id, text in note_text.items():
        _node(workflow, node_id)["widgets_values"] = [text]
    for group in workflow.get("groups", []):
        if int(group["id"]) == 1:
            group["title"] = (
                "H3 MODEL STACK - 20 STEPS / TURBO LORAS DISABLED"
            )
        elif int(group["id"]) == 5:
            group["title"] = "PLAN EDITOR - DYNAMIC JSON OVERRIDES EXAMPLE"

    workflow.setdefault("extra", {})["release"] = {
        "input_media_included": False,
        "model_weights_included": False,
        "source_workflow": "user final canvas",
        "missing_audio": "same-duration 44.1 kHz mono silence fallback",
        "source_loading": "streamed current-scene windows",
    }


def _stable_variant(source: dict[str, Any]) -> dict[str, Any]:
    workflow = copy.deepcopy(source)
    _genericize(workflow)
    turbo = _node(workflow, TURBO_NODE_ID)
    turbo["mode"] = 2
    turbo["title"] = (
        "DISABLED - Turbo 4-step LoRA requires a compatible non-pruned base"
    )
    turbo["color"] = "#6b3030"
    turbo["bgcolor"] = "rgba(24,24,27,.9)"

    workflow["links"] = [
        link
        for link in workflow["links"]
        if int(link[1]) != TURBO_NODE_ID and int(link[3]) != TURBO_NODE_ID
    ]
    target = _node(workflow, ATTENTION_NODE_ID)
    target_slot = _input_index(target, "model")
    workflow["links"] = [
        link
        for link in workflow["links"]
        if not (
            int(link[3]) == ATTENTION_NODE_ID and int(link[4]) == target_slot
        )
    ]
    link_id = max((int(link[0]) for link in workflow["links"]), default=0) + 1
    workflow["links"].append(
        [link_id, SAGE_NODE_ID, 0, ATTENTION_NODE_ID, target_slot, "MODEL"]
    )
    workflow["extra"]["release"].update(
        {
            "variant": "stable",
            "turbo_lora": "disabled and disconnected",
        }
    )
    _rebuild_link_fields(workflow)
    return workflow


def _experimental_variant(source: dict[str, Any]) -> dict[str, Any]:
    workflow = copy.deepcopy(source)
    _genericize(workflow)
    turbo = _node(workflow, TURBO_NODE_ID)
    turbo["title"] = (
        "EXPERIMENTAL - Turbo 4-step LoRA; replace the pruned base first"
    )
    workflow["extra"]["release"].update(
        {
            "variant": "turbo-experimental",
            "warning": (
                "The enabled Turbo route is incompatible with the default "
                "pruned_int8_convrot base and is not the supported default."
            ),
        }
    )
    _rebuild_link_fields(workflow)
    return workflow


def _user_profile_variant(
    source: dict[str, Any], profile_name: str
) -> dict[str, Any]:
    """Make a portable copy while preserving the user's active model route."""
    try:
        profile = USER_PROFILES[profile_name]
    except KeyError as exc:
        raise ValueError(f"unknown user profile {profile_name!r}") from exc

    workflow = copy.deepcopy(source)
    for node_id, filename in INPUT_NAMES.items():
        values = list(_node(workflow, node_id).get("widgets_values") or [])
        if not values:
            raise ValueError(f"input node {node_id} has no filename widget")
        values[0] = filename
        _node(workflow, node_id)["widgets_values"] = values

    prepare = _node(workflow, 1960)
    prepare_values = list(prepare.get("widgets_values") or [])
    frame_cap = int(profile["frame_cap"])
    if len(prepare_values) < 5 or int(prepare_values[1]) != frame_cap:
        raise ValueError(
            f"{profile_name} source must use a {frame_cap}-frame prepare cap"
        )
    prompt = str(prepare_values[0])
    base_seed = int(prepare_values[4])

    plan_node = _node(workflow, 1700)
    plan_values = list(plan_node.get("widgets_values") or [])
    if len(plan_values) < 2:
        raise ValueError("plan node 1700 is missing its run-name widget")
    plan_values[0] = json.dumps(
        {
            "defaults": {"steps": int(prepare_values[3])},
            "shots": [
                {
                    "id": f"source_{index:02d}",
                    "prompt": prompt,
                    "length": int(length),
                    "seed": str(base_seed + index - 1),
                }
                for index, length in enumerate(profile["scene_lengths"], start=1)
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    suffix = str(profile["run_suffix"])
    plan_values[1] = f"h3_native_loop_{suffix}"
    plan_node["widgets_values"] = plan_values
    plan_node["title"] = f"AUTO PLAN - DYNAMIC SOURCE COUNT / {profile['label']}"

    ref2va_values = list(_node(workflow, 110).get("widgets_values") or [])
    if len(ref2va_values) > 3:
        ref2va_values[3] = frame_cap
        _node(workflow, 110)["widgets_values"] = ref2va_values

    _node(workflow, 1950)["title"] = "INPUT ORIGINAL LONG VIDEO - AUDIO OPTIONAL"
    prepare["title"] = (
        f"STREAM METADATA + AUTO PLAN + AUDIO FALLBACK - {profile['label']}"
    )
    _node(workflow, 1706)["widgets_values"][1] = (
        f"character_swap_assembled_{suffix}"
    )
    _node(workflow, 1708)["widgets_values"][1] = (
        f"character_swap_recovered_{suffix}"
    )
    final = _node(workflow, 1961)
    final["title"] = (
        "FINAL PLAYABLE PREVIEW + EXACT SOURCE-FRAME TRIM + AUDIO FALLBACK"
    )
    final["widgets_values"][0] = f"character_swap_full_exact_{suffix}"

    turbo = _node(workflow, TURBO_NODE_ID)
    if int(turbo.get("mode", 0)) != 0:
        raise ValueError("user profile requires active LightX2V LoRA node 1972")
    turbo["title"] = "ENABLED - LightX2V Turbo LoRA / 20-step user profile"
    connected_links = [
        link
        for link in workflow["links"]
        if int(link[1]) == TURBO_NODE_ID or int(link[3]) == TURBO_NODE_ID
    ]
    if not any(int(link[3]) == TURBO_NODE_ID for link in connected_links) or not any(
        int(link[1]) == TURBO_NODE_ID for link in connected_links
    ):
        raise ValueError("user profile requires node 1972 to stay connected")

    for group in workflow.get("groups", []):
        if int(group["id"]) == 1:
            group["title"] = (
                "H3 MODEL STACK - LIGHTX2V TURBO LORA ENABLED / "
                "20-STEP USER PROFILE"
            )
        elif int(group["id"]) == 5:
            group["title"] = "PLAN EDITOR - DYNAMIC JSON OVERRIDES EXAMPLE"

    workflow.setdefault("extra", {})["release"] = {
        "input_media_included": False,
        "model_weights_included": False,
        "source_workflow": "user-modified final canvas",
        "variant": profile["variant"],
        "scene_frame_cap": frame_cap,
        "missing_audio": "same-duration 44.1 kHz mono silence fallback",
        "source_loading": "streamed current-scene windows",
        "final_preview": "playable video and saved path on canvas",
        "turbo_lora": "enabled and connected user profile",
        "turbo_lora_model": str(turbo["widgets_values"][0]),
        "sampling": "res_multistep / beta / 20 steps",
    }
    _rebuild_link_fields(workflow)
    return workflow


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("stable_output", type=Path)
    parser.add_argument("experimental_output", type=Path, nargs="?")
    parser.add_argument(
        "--user-profile",
        choices=sorted(USER_PROFILES),
        help="write one portable user profile to stable_output",
    )
    args = parser.parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8"))
    if args.user_profile:
        if args.experimental_output is not None:
            parser.error("experimental_output is not used with --user-profile")
        _write(
            args.stable_output,
            _user_profile_variant(source, args.user_profile),
        )
        print(args.stable_output.resolve())
        return 0
    if args.experimental_output is None:
        parser.error("experimental_output is required unless --user-profile is used")
    _write(args.stable_output, _stable_variant(source))
    _write(args.experimental_output, _experimental_variant(source))
    print(args.stable_output.resolve())
    print(args.experimental_output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
