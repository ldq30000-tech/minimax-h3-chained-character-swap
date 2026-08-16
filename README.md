# MiniMax H3 Chained Character Swap

A portable [Agent Skill](https://agentskills.io) for chaining MiniMax H3 Ref2VA character-replacement clips with **Motion Context** and a **tapered chroma-noise context injection**.

[中文最终版说明](README_CN.md) | [Final release notes](RELEASE_NOTES.md) | [Third-party notices](THIRD_PARTY_NOTICES.md)

> Experimental workflow, not a claim of hard frame-by-frame temporal control. It is intended for slow-to-medium-speed motion. Fast combat, abrupt direction changes, and strict source-faithful choreography still need separate handling and human review.

## What this contains

- `SKILL.md` — Pi / Codex / Claude-compatible skill instructions.
- `assets/workflows/` — a generic, editable T3 workflow template.
- `scripts/` — context taper injection, motion-phase/seam auditing, and a guarded native-ComfyUI chain runner.
- `references/` — recipe, experiment notes, limitations, and runner documentation.
- `examples/` — the original Zhu Yuan POC workflow, preserved as a reference only. It expects files that are **not** included here.

No model weights, reference images, source videos, or rendered footage are stored
in the Git source history. User-supplied demonstration media is attached to the
GitHub Release only.

## Demo videos

- [Generated result (25.01 s, 1440x2560, 60 fps)](https://github.com/ldq30000-tech/minimax-h3-chained-character-swap/releases/download/v1.0.0/generated-character-swap-result.mp4)
- [Template/reference video (6.94 s, 720x1280, 30 fps)](https://github.com/ldq30000-tech/minimax-h3-chained-character-swap/releases/download/v1.0.0/template-reference-video.mp4)

These illustrate workflow input and output, but they have different durations and
are not an equal-length, frame-by-frame before/after pair. Release media is not
automatically covered by this repository's MIT license.

## The recipe

For each continuation:

```text
clean previous delivery
  └─ extract final 22 frames
      ├─ clean copy retained for the next generation
      └─ temporary injected copy: 19f @ 0.45, tail 3f → 0.10
           └─ Motion Context input → H3 raw output → trim repeated 22f → clean delivery
```

The injected context is disposable. Never recursively inject the already-injected version.

## Guarded automatic loop

`scripts/run_chain.py` automates the repeated continuation steps against a local/mounted ComfyUI server. It archives each raw/trimmed result and **halts with exit code 2** if a configured phase, seam, or sharpness threshold fails. It writes `STATE.json` for the agent to inspect; it never silently rerolls or continues through a failure.

```bash
mkdir -p runs
cp examples/chain-config.example.json runs/my-chain.json
# Fill local paths, source slices, identity images, node IDs, and gates.
python3 scripts/run_chain.py runs/my-chain.json
```

After human/agent inspection of a halted candidate, an explicit `--approve-latest` continues from that exact archived delivery. Details: [references/RUNNER.md](references/RUNNER.md).

## One-click ComfyUI entry

This repository can also be installed directly as a ComfyUI custom node. Clone
the whole repository into `ComfyUI/custom_nodes/minimax-h3-chained-character-swap`,
then restart ComfyUI. It exposes three nodes under **H3 Chain**:

- **H3 Chain Config** validates the source slices, references, paths, H3 frame
  settings, gates, and template node ids, then writes an immutable config in
  `run_dir`.
- **H3 Chain Launch** starts the guarded runner as a separate controller
  process. It returns immediately so the active ComfyUI queue worker can finish;
  the controller then submits each continuation back through the native API.
- **H3 Chain Status** reads `STATE.json` without mutating it.

Connect Config's `config_path` output to Launch. Use `start` after a manually
generated and reviewed first delivery. When Status reports `needs_agent_review`,
inspect the archived output and choose Launch's `approve_latest` action only to
explicitly accept that exact candidate. The node never auto-approves failures,
rerolls, or feeds an injected temporary context into a later segment.

An API-format starter graph is available at
`assets/workflows/h3-chain-controller-template.json`. Replace every absolute
path before queueing it. `reference_images_json` accepts either an ordered list
of image paths or a map of existing `LoadImage` node ids to paths.
`segments_json` is an ordered list of source slices, each exactly `raw_frames`
long at 24 fps, for example:

```json
[
  {"name": "seg02", "seed": 730002, "source": "D:/project/source_seg02_124f.mp4"},
  {"name": "seg03", "seed": 730003, "source": "D:/project/source_seg03_124f.mp4"}
]
```

## Full source-video loop

For the final visible native recursive canvas, import
`assets/workflows/h3-native-loop-final-stable-ui.json`. It automatically counts
and segments a complete source video, keeps every expensive H3 node visible,
checkpoints each clean segment, assembles the chain, trims inference-only tail
padding, and restores the original source soundtrack. The Stable graph keeps
ReservedVRAM and the KJNodes memory-efficient SageAttention patch but leaves all
Turbo LoRAs disabled and disconnected from the pruned INT8 base.

The neighboring `h3-native-loop-final-turbo-experimental-ui.json` preserves the
enabled Turbo route from the supplied final canvas for inspection only. It is
not compatible with the default `pruned_int8_convrot` base and requires a
compatible non-pruned model plus the LoRA's documented schedule.

`assets/workflows/h3-full-video-controller-ui.json` is the standard ComfyUI UI
workflow to import for a complete source video. The neighboring
`h3-full-video-controller-template.json` is its API-format equivalent. They use
**H3 Full Video Inputs**, **H3 Full Video Config**, **H3 Full Video Launch**,
and **H3 Full Video Status**. The Inputs node provides native ComfyUI
upload/select widgets for one source video plus front, side, back, and face
closeup identity images; it passes resolved input paths to the controller.
The controller follows the same useful state/collect/finalize pattern as a
MieLoop graph while submitting the expensive H3 jobs only after the launching
graph has released ComfyUI's queue worker.

For a single-node import, use
`assets/workflows/h3-full-video-one-click-ui.json`. **H3 Full Video One Click**
keeps the media upload widgets and stable controller settings in one node; the
initial and continuation API workflows remain internal implementation details
and do not need to be loaded manually.

For troubleshooting on one canvas, use
`assets/workflows/h3-full-video-diagnostic-all-in-one-ui.json`. It keeps the
media inputs, full-video configuration, non-blocking launcher, live stage/error
diagnostics, and a readable stage map together. Both generation API workflows
are embedded in the workflow metadata, so the imported graph does not depend on
separate JSON paths. The visible MiniMax H3 Turbo LoRA slots are deliberately
disabled: the bundled pruned Ref2VA base has incompatible AdaLN dimensions.
Enable and wire those slots only after replacing it with a compatible
non-pruned MiniMax H3 base, and change sampling to the LoRA's documented steps.

The full-video runner automatically:

1. resamples and letterboxes the source to 24 fps and the requested dimensions;
2. computes `1 + ceil(max(0, total_frames - raw_frames) / (raw_frames - context_frames))`;
3. creates overlapping source slices and inference-only padding for the final slice;
4. generates the first segment with the no-context template;
5. chains later segments from the previous clean delivery's 22-frame tail;
6. trims duplicated context and final inference padding;
7. concatenates exactly the normalized source frame count; and
8. muxes the original source audio into `run_dir/final/final.mp4`.

Final-segment QA excludes inference-only duplicate padding. Sharpness compares
only the real delivered frame range. If that range is too short to provide four
motion transitions per configured phase subrange, `qa.json` records
`phase_skipped` and its reason, and phase gates are skipped for that segment.

Set a new `run_dir` when changing any input or setting. A numerical QA failure
changes `STATE.json` to `needs_agent_review`; inspect the archived segment and
queue **H3 Full Video Launch** with `approve_latest` only to deliberately accept
that exact candidate.

## Install as a skill

Clone this repository, then expose the repository folder as a skill directory.

### Shared project skill — Pi

```bash
mkdir -p .pi/skills
git clone https://github.com/ldq30000-tech/minimax-h3-chained-character-swap.git \
  .pi/skills/minimax-h3-chained-character-swap
```

### Shared project skill — Codex

```bash
mkdir -p .agents/skills
git clone https://github.com/ldq30000-tech/minimax-h3-chained-character-swap.git \
  .agents/skills/minimax-h3-chained-character-swap
```

### Pi global skill

```bash
mkdir -p ~/.pi/agent/skills
git clone https://github.com/ldq30000-tech/minimax-h3-chained-character-swap.git \
  ~/.pi/agent/skills/minimax-h3-chained-character-swap
```

Then use `/skill:minimax-h3-chained-character-swap` or ask Pi naturally. Reload/restart if the new skill does not appear.

### Claude Code

```bash
mkdir -p ~/.claude/skills
ln -s /path/to/minimax-h3-chained-character-swap \
  ~/.claude/skills/minimax-h3-chained-character-swap
```

### Codex global skill

```bash
mkdir -p ~/.codex/skills
ln -s /path/to/minimax-h3-chained-character-swap \
  ~/.codex/skills/minimax-h3-chained-character-swap
```

## Prerequisites

- ComfyUI with MiniMax H3 Ref2VA support.
- A compatible Motion Context node pack.
- Python 3.10+, FFmpeg, and Pillow (`python3 -m pip install -r requirements.txt`).
- 24 fps source slices. Resample other frame rates before slicing; the runner rejects non-24-fps inputs.
- For the automatic runner: local or mounted access to the selected ComfyUI server's `input/` and `output/` directories.
- Your own legal source video, target-character references, and model weights.

The supplied workflows reference these loader filenames and must be adjusted if your installation uses different names:

```text
minimax_h3_ref2va_pruned_int8_convrot.safetensors
qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
minimax_h3_video_vae_fp16.safetensors
minimax_h3_audio_vae_fp32.safetensors
```

See [upstream dependencies and licensing](references/UPSTREAM_DEPENDENCIES.md) before use.

## Development checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

## License

MIT for this repository's original scripts, documentation, and generic workflow configuration. MiniMax H3 weights, ComfyUI, and Motion Context node packs have their own licenses and usage restrictions.
