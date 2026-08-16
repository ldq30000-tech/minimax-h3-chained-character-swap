"""Create stable and experimental release workflows from a ComfyUI canvas."""

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
    prepare["title"] = "AUTO 24 FPS + PLAN + PAD + SILENCE FALLBACK"
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
    plan_values[1] = "h3_native_loop_release"
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
        "character_swap_assembled",
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
    final["widgets_values"] = ["character_swap_full_exact", 24.0, 256]

    note_text = {
        1932: (
            "Queue once. Loop End recursively clones the visible sampling body "
            "for every auto-planned scene. The green Exact Trim node is the "
            "actual source-length delivery with original audio or silence fallback."
        ),
        1968: (
            "The source is decoded once, resampled to 24 fps, and counted on "
            "the canvas. H3-valid segment lengths and inference-only tail "
            "padding are computed automatically. Final Exact Trim removes "
            "inference-only frames and restores original source audio, or "
            "same-duration silence when no decodable audio track exists."
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


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("stable_output", type=Path)
    parser.add_argument("experimental_output", type=Path)
    args = parser.parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8"))
    _write(args.stable_output, _stable_variant(source))
    _write(args.experimental_output, _experimental_variant(source))
    print(args.stable_output.resolve())
    print(args.experimental_output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
