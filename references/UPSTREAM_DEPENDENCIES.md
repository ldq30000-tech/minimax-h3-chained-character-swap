# Upstream dependencies and attribution

This repository does **not** include MiniMax H3 weights or Motion Context node code.

## Required upstream components

- [ComfyUI](https://github.com/Comfy-Org/ComfyUI) with native MiniMax H3 / Ref2VA support.
- A compatible H3 Motion Context implementation that provides nodes equivalent to the workflow's `MiniMaxH3Chain*` and trim nodes.

The reference workflow was built with the Motion Context ecosystem originating from:

- [NikoDemon80/ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context)
- [ethanfel/ComfyUI-MiniMaxH3-Contex-Loop](https://github.com/ethanfel/ComfyUI-MiniMaxH3-Contex-Loop)

These projects are GPL-3.0 and have their own installation instructions, licensing, and compatibility constraints. This repository only supplies an external workflow configuration and independent helper scripts; it does not redistribute or modify their code.

## Model and media licensing

MiniMax H3 weights, ComfyUI, node packs, target-character references, source performances, and generated outputs all have independent licenses or rights considerations. Confirm the relevant terms before using this workflow or publishing results.

## This repository

Unless a file says otherwise, the original scripts, documentation, and generic workflow configuration here are MIT licensed. See `LICENSE`.
