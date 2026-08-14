# MiniMax H3 Chained Character Swap

A portable [Agent Skill](https://agentskills.io) for chaining MiniMax H3 Ref2VA character-replacement clips with **Motion Context** and a **tapered chroma-noise context injection**.

> Experimental workflow, not a claim of hard frame-by-frame temporal control. It is intended for slow-to-medium-speed motion. Fast combat, abrupt direction changes, and strict source-faithful choreography still need separate handling and human review.

## What this contains

- `SKILL.md` — Pi / Codex / Claude-compatible skill instructions.
- `assets/workflows/` — a generic, editable T3 workflow template.
- `scripts/` — context taper injection, motion-phase/seam auditing, and a guarded native-ComfyUI chain runner.
- `references/` — recipe, experiment notes, limitations, and runner documentation.
- `examples/` — the original Zhu Yuan POC workflow, preserved as a reference only. It expects files that are **not** included here.

No model weights, reference images, source videos, or rendered footage are included.

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

## Install as a skill

Clone this repository, then expose the repository folder as a skill directory.

### Shared project skill — Pi

```bash
mkdir -p .pi/skills
git clone https://github.com/MacroSony/minimax-h3-chained-character-swap.git \
  .pi/skills/minimax-h3-chained-character-swap
```

### Shared project skill — Codex

```bash
mkdir -p .agents/skills
git clone https://github.com/MacroSony/minimax-h3-chained-character-swap.git \
  .agents/skills/minimax-h3-chained-character-swap
```

### Pi global skill

```bash
mkdir -p ~/.pi/agent/skills
git clone https://github.com/MacroSony/minimax-h3-chained-character-swap.git \
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
