"""Build the expanded all-in-one ComfyUI controller workflow.

The expensive initial and continuation graphs remain API-format payloads in
workflow metadata. The visible canvas focuses on controller inputs, stages,
live diagnostics, and deliberately disabled LoRA compatibility slots.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INITIAL = ROOT / "assets" / "workflows" / "t3-ref2va-initial-template.json"
CONTINUATION = ROOT / "assets" / "workflows" / "t3-ref2va-context-taper-template.json"


def note(node_id: int, pos: list[int], size: list[int], text: str, order: int) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "Note",
        "pos": pos,
        "size": size,
        "flags": {},
        "order": order,
        "mode": 0,
        "inputs": [],
        "outputs": [],
        "properties": {"text": ""},
        "widgets_values": [text],
        "color": "#243342",
        "bgcolor": "#18232d",
    }


def display(node_id: int, pos: list[int], title: str, link_id: int, order: int) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "Display Any (rgthree)",
        "pos": pos,
        "size": [360, 150],
        "flags": {},
        "order": order,
        "mode": 0,
        "inputs": [{"name": "source", "type": "*", "link": link_id}],
        "outputs": [],
        "title": title,
        "properties": {"Node name for S&R": "Display Any (rgthree)"},
        "widgets_values": [],
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    initial = json.loads(INITIAL.read_text(encoding="utf-8"))
    continuation = json.loads(CONTINUATION.read_text(encoding="utf-8"))
    comfy_root = Path(args.comfy_root)
    extension_root = comfy_root / "custom_nodes" / "minimax-h3-chained-character-swap"

    nodes: list[dict[str, Any]] = [
        {
            "id": 1,
            "type": "H3FullVideoInputs",
            "pos": [100, 170],
            "size": [470, 330],
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [],
            "outputs": [
                {"name": "source_video", "type": "STRING", "links": [1], "slot_index": 0},
                {"name": "reference_images_json", "type": "STRING", "links": [2], "slot_index": 1},
            ],
            "properties": {"Node name for S&R": "H3FullVideoInputs"},
            "widgets_values": [
                args.source_name,
                args.front_name,
                args.side_name,
                args.back_name,
                args.face_name,
            ],
        },
        {
            "id": 2,
            "type": "H3FullVideoConfig",
            "pos": [720, 100],
            "size": [720, 850],
            "flags": {},
            "order": 1,
            "mode": 0,
            "inputs": [
                {"name": "source_video", "type": "STRING", "link": 1},
                {"name": "reference_images_json", "type": "STRING", "link": 2},
            ],
            "outputs": [
                {"name": "config_path", "type": "STRING", "links": [3], "slot_index": 0}
            ],
            "properties": {"Node name for S&R": "H3FullVideoConfig"},
            "widgets_values": [
                str(extension_root / "assets" / "workflows" / INITIAL.name),
                str(extension_root / "assets" / "workflows" / CONTINUATION.name),
                args.comfy_url,
                args.input_dir,
                args.output_dir,
                args.run_dir,
                "",
                '{"source_video":"43","ref2va":"20","noise":"12","scheduler":"14","output":"19"}',
                '{"source_video":"43","context_video":"101","plan":"100","raw_output":"19","delivery_output":"108"}',
                '{"max_abs_phase_offset":2,"min_phase_ncc":0.3,"max_seam_diff":0.04,"min_sharpness_ratio":0.75,"min_source_rms_difference":8.0}',
                124,
                22,
                576,
                1024,
                20,
                730000,
                5400,
                0.45,
                0.10,
                3,
            ],
        },
        {
            "id": 3,
            "type": "H3FullVideoLaunch",
            "pos": [1570, 170],
            "size": [380, 180],
            "flags": {},
            "order": 2,
            "mode": 0,
            "inputs": [{"name": "config_path", "type": "STRING", "link": 3}],
            "outputs": [
                {"name": "run_dir", "type": "STRING", "links": None, "slot_index": 0},
                {"name": "launch_status", "type": "STRING", "links": None, "slot_index": 1},
                {"name": "state_path", "type": "STRING", "links": [4], "slot_index": 2},
            ],
            "properties": {"Node name for S&R": "H3FullVideoLaunch"},
            "widgets_values": ["start"],
        },
        {
            "id": 4,
            "type": "H3FullVideoDiagnostics",
            "pos": [2110, 120],
            "size": [430, 250],
            "flags": {},
            "order": 3,
            "mode": 0,
            "inputs": [{"name": "state_path", "type": "STRING", "link": 4}],
            "outputs": [
                {"name": "stage", "type": "STRING", "links": [5], "slot_index": 0},
                {"name": "active_segment", "type": "STRING", "links": [6], "slot_index": 1},
                {"name": "completed_segments", "type": "INT", "links": [7], "slot_index": 2},
                {"name": "total_segments", "type": "INT", "links": [8], "slot_index": 3},
                {"name": "problem", "type": "STRING", "links": [9], "slot_index": 4},
                {"name": "final_video", "type": "STRING", "links": [10], "slot_index": 5},
            ],
            "properties": {"Node name for S&R": "H3FullVideoDiagnostics"},
            "widgets_values": [],
        },
        display(5, [2650, 100], "当前阶段 / stage", 5, 4),
        display(6, [3050, 100], "当前分段 / active segment", 6, 5),
        display(7, [2650, 310], "已完成分段 / completed", 7, 6),
        display(8, [3050, 310], "总分段数 / total", 8, 7),
        display(9, [2650, 520], "错误或 QA 停止原因 / problem", 9, 8),
        display(10, [3050, 520], "最终视频 / final video", 10, 9),
        note(
            11,
            [100, 720],
            [500, 500],
            "自动链路（控制器内部执行）\n\n"
            "1. 原视频归一化到 24 fps，并按输出尺寸缩放\n"
            "2. 根据总帧数自动规划 124 帧分段\n"
            "3. seg01 使用无上下文 H3 Ref2VA\n"
            "4. 后续段从上一段干净交付尾部取 22 帧\n"
            "5. 临时上下文：19 帧 0.45 注噪，末 3 帧降到 0.10\n"
            "6. 每段生成 124 帧，去掉重复的 22 帧上下文\n"
            "7. phase / seam / sharpness / source-difference QA\n"
            "8. QA 失败停在 needs_agent_review，不会静默继续\n"
            "9. 全部分段拼接后才恢复原视频声音",
            10,
        ),
        note(
            12,
            [1570, 420],
            [420, 300],
            "使用方式\n\n"
            "选择原视频和四张人物参考图后排队一次。控制节点立即返回，后台开始分段生成。"
            "再次排队只用于刷新右侧诊断；运行中不会重复启动。\n\n"
            "只有检查过失败片段并确认可接受时，才把 action 改为 approve_latest。",
            11,
        ),
        {
            "id": 13,
            "type": "UNETLoader",
            "pos": [760, 1190],
            "size": [520, 120],
            "flags": {},
            "order": 12,
            "mode": 2,
            "inputs": [],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [11]}],
            "title": "DISABLED - current pruned H3 base",
            "properties": {"Node name for S&R": "UNETLoader"},
            "widgets_values": ["minimax\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"],
            "color": "#5b2c2c",
            "bgcolor": "#351b1b",
        },
        {
            "id": 14,
            "type": "LoraLoaderModelOnly",
            "pos": [1340, 1190],
            "size": [520, 140],
            "flags": {},
            "order": 13,
            "mode": 2,
            "inputs": [{"name": "model", "type": "MODEL", "link": 11}],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [12]}],
            "title": "DISABLED - Turbo 4 steps (needs non-pruned base)",
            "properties": {"Node name for S&R": "LoraLoaderModelOnly"},
            "widgets_values": ["minimax\\minimax_h3_turbo_4STEPS_comfyui.safetensors", 1.0],
            "color": "#5b2c2c",
            "bgcolor": "#351b1b",
        },
        {
            "id": 15,
            "type": "LoraLoaderModelOnly",
            "pos": [1920, 1190],
            "size": [520, 140],
            "flags": {},
            "order": 14,
            "mode": 2,
            "inputs": [{"name": "model", "type": "MODEL", "link": 12}],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": None}],
            "title": "DISABLED - Turbo v4 (needs non-pruned base)",
            "properties": {"Node name for S&R": "LoraLoaderModelOnly"},
            "widgets_values": ["minimax\\minimax_h3_turbo_v4_step600_ema.safetensors", 1.0],
            "color": "#5b2c2c",
            "bgcolor": "#351b1b",
        },
        note(
            16,
            [2500, 1160],
            [660, 240],
            "LoRA 兼容性保护\n\n"
            "当前基础模型是 pruned_int8_convrot；已安装的两只 H3 Turbo LoRA 声明只兼容 non-pruned bf16 / int8_convrot。"
            "pruned 模型的 AdaLN 输入维度不同，直接连接会报 shape mismatch。\n\n"
            "因此这些插槽是可见的未来接线位，但保持 Disabled，当前控制器仍以 20 steps 运行。",
            15,
        ),
    ]

    links = [
        [1, 1, 0, 2, 0, "STRING"],
        [2, 1, 1, 2, 1, "STRING"],
        [3, 2, 0, 3, 0, "STRING"],
        [4, 3, 2, 4, 0, "STRING"],
        [5, 4, 0, 5, 0, "*"],
        [6, 4, 1, 6, 0, "*"],
        [7, 4, 2, 7, 0, "*"],
        [8, 4, 3, 8, 0, "*"],
        [9, 4, 4, 9, 0, "*"],
        [10, 4, 5, 10, 0, "*"],
        [11, 13, 0, 14, 0, "MODEL"],
        [12, 14, 0, 15, 0, "MODEL"],
    ]

    return {
        "id": "ecfa23b1-b8a2-4ed2-82d8-3daeddd87f6f",
        "revision": 0,
        "last_node_id": 16,
        "last_link_id": 12,
        "nodes": nodes,
        "links": links,
        "groups": [
            {"id": 1, "title": "1. 输入视频与人物参考图", "bounding": [60, 60, 570, 540], "color": "#2d7d66", "font_size": 24, "flags": {}},
            {"id": 2, "title": "2. 自动分段与链路配置（内嵌两份 API 工作流）", "bounding": [680, 60, 810, 940], "color": "#426b8a", "font_size": 24, "flags": {}},
            {"id": 3, "title": "3. 非阻塞控制器", "bounding": [1530, 60, 510, 720], "color": "#775f38", "font_size": 24, "flags": {}},
            {"id": 4, "title": "4. 实时诊断：重新排队刷新", "bounding": [2070, 60, 1390, 700], "color": "#7b4c61", "font_size": 24, "flags": {}},
            {"id": 5, "title": "5. 内部阶段说明", "bounding": [60, 680, 570, 590], "color": "#526c45", "font_size": 24, "flags": {}},
            {"id": 6, "title": "6. 加速 LoRA 插槽（兼容保护：默认禁用）", "bounding": [680, 1080, 2540, 390], "color": "#8a3f3f", "font_size": 24, "flags": {}},
        ],
        "config": {},
        "extra": {
            "ds": {"scale": 0.55, "offset": [80, 80]},
            "frontendVersion": "1.48.7",
            "h3_embedded_workflows": {
                "initial": initial,
                "continuation": continuation,
            },
        },
        "version": 0.4,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "assets" / "workflows" / "h3-full-video-diagnostic-all-in-one-ui.json",
    )
    parser.add_argument("--comfy-root", default="C:/ComfyUI")
    parser.add_argument("--input-dir", default="C:/ComfyUI/input")
    parser.add_argument("--output-dir", default="C:/ComfyUI/output")
    parser.add_argument("--run-dir", default="D:/project/runs/h3-full-video-diagnostic")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    parser.add_argument("--source-name", default="source_long_video.mp4")
    parser.add_argument("--front-name", default="character_front.png")
    parser.add_argument("--side-name", default="character_side.png")
    parser.add_argument("--back-name", default="character_back.png")
    parser.add_argument("--face-name", default="character_face_closeup.png")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build(args), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
