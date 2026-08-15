# Upstream dependencies and attribution

This repository does **not** include MiniMax H3 weights or Motion Context node code.

## Required upstream components

- [ComfyUI](https://github.com/Comfy-Org/ComfyUI) with native MiniMax H3 / Ref2VA support.
- [ethanfel/ComfyUI-MiniMaxH3-Contex-Loop](https://github.com/ethanfel/ComfyUI-MiniMaxH3-Contex-Loop)
  — provides the `MiniMaxH3ChainPlan`, `MiniMaxH3ChainExternalVideo`,
  `MiniMaxH3ChainLoopStart`, `MiniMaxH3ChainCurrent`, `MiniMaxH3ChainContext`,
  and `MiniMaxH3LoopTrim` nodes used by the reference workflow.

## Prior work / optional coexistence

- [NikoDemon80/ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context)
  — original Motion Context implementation. It does **not** provide the
  `MiniMaxH3Chain*` nodes used by this workflow; it can be installed alongside
  ethanfel's pack, but is not required for this skill.

These projects are GPL-3.0 and have their own installation instructions, licensing, and compatibility constraints. This repository only supplies an external workflow configuration and independent helper scripts; it does not redistribute or modify their code.

## Model and media licensing

MiniMax H3 weights, ComfyUI, node packs, target-character references, source performances, and generated outputs all have independent licenses or rights considerations. Confirm the relevant terms before using this workflow or publishing results.

## This repository

Unless a file says otherwise, the original scripts, documentation, and generic workflow configuration here are MIT licensed. See `LICENSE`.
