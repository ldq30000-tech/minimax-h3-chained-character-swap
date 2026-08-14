---
name: minimax-h3-chained-character-swap
description: Chain MiniMax H3 Ref2VA character-replacement clips with Motion Context and tapered chroma-noise injection. Use when creating long-form H3 video-to-video character swaps, preserving motion across generated clips, reducing chained sharpness loss, preparing a 22-frame context taper, or auditing temporal phase and seams.
license: MIT
compatibility: Requires ComfyUI with MiniMax H3 Ref2VA and a compatible H3 Motion Context node pack, plus Python 3, FFmpeg, and Pillow.
metadata:
  author: MacroSony
  version: "0.1.0"
---

# MiniMax H3 Chained Character Swap

Use this skill for **slow-to-medium-speed** MiniMax H3 Ref2VA character replacement across multiple clips. It combines Motion Context for continuity with a tapered chroma-noise injection on the temporary context file to reduce chained sharpness loss.

## Read first

- Use this for character replacement where reference images define identity and `Video 1` defines motion, timing, composition, camera work, and environment.
- It is **not** hard temporal control. Do not claim frame-exact source motion from the phase screen alone.
- Do not use it as the primary continuation method for fast combat or abrupt motion. Use independent blocks, a generated tail-frame anchor, and human dynamic review instead.
- The injection is applied only to a temporary context input. Never feed an already injected clip into the next generation.

## Required setup

1. Install a compatible Motion Context implementation for H3 Ref2VA. This repository ships no node code; see [upstream dependencies](references/UPSTREAM_DEPENDENCIES.md).
2. Put the model weights and this skill's input media in your ComfyUI setup.
3. Ensure `python3`, `ffmpeg`, and Pillow are available:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

4. Generate the first segment (no context) with `assets/workflows/t3-ref2va-initial-template.json`; then start chains from `assets/workflows/t3-ref2va-context-taper-template.json`. Replace all placeholder image/video filenames and review the prompt before queueing anything.
5. For a guarded multi-segment run, copy `examples/chain-config.example.json`, fill every path and gate, then run `scripts/run_chain.py`. The runner talks to ComfyUI's native API and therefore needs local/mounted access to both `ComfyUI/input` and `ComfyUI/output`.

## T3 context recipe

For every continuation segment after the first:

1. Keep the previous segment's **clean delivered** MP4.
2. Extract its final 22 frames as a temporary context video.
3. Create a separate injected copy:

   ```bash
   python3 scripts/inject_tail_taper.py \
     context_clean_tail22.mp4 context_tail22_taper.mp4 \
     22 0.45 0.10 3
   ```

   This injects chroma blocks into the 22-frame context: the first 19 frames at `0.45`; the final 3 frames taper toward `0.10`.

4. In the workflow, use the injected file only at the Motion Context external-video input; use the clean prior delivery as the source for the next context extraction.
5. Generate the raw clip, then trim the repeated 22-frame context region. Only the trimmed delivery enters the final timeline.
6. Keep source timestamps unique. Do not delete unique frames and fill the duration with duplicates.

See [the full recipe](references/RECIPE.md) and [limitations](references/LIMITATIONS.md).

## Guarded automatic loop

The generic runner automates the repetitive safe part of the chain but deliberately leaves ambiguous decisions to the agent:

```bash
mkdir -p runs
cp examples/chain-config.example.json runs/my-chain.json
# Edit all paths, source slices, identity images, node ids, and numerical gates.
python3 scripts/run_chain.py runs/my-chain.json
```

For each continuation it extracts the previous **clean** delivery tail, builds an injected temporary context, submits the workflow, archives raw and trimmed outputs, measures phase / seam / sharpness, and writes `run_dir/STATE.json`.

If any configured numerical gate fails, the process exits with code `2` and changes `STATE.json` to `needs_agent_review`. It does **not** reroll, discard the candidate, or silently continue. Inspect that segment's `qa.json`, source, raw, and delivery; only if an agent deliberately accepts the tradeoff should it resume:

```bash
python3 scripts/run_chain.py runs/my-chain.json --approve-latest
```

`--approve-latest` accepts only the already archived candidate and starts the next segment. To reroll or replace a failed segment, do that explicitly, update the run state/config after review, and retain the failed artifacts.

## QA

Run candidate screens, then inspect the video yourself:

```bash
python3 scripts/audit_motion_phase_screen.py raw.mp4 source_segment.mp4 \
  --start 22 --end 123 --search 12 --segments 3

python3 scripts/audit_stitch.py final.mp4 --seams 106,208
```

Seam indices are zero-based positions in the **delivered** timeline. The example `106,208` fits the POC chain (107-frame first segment, then 102-frame deliveries). Compute your own seams from the chain's timing plan, not from this example.

- Phase screen is a **candidate-ranking screen**, not pose certification.
- Review the repeated-context exit (typically raw frames 22–23) for chroma-noise leakage.
- Check source motion phase, velocity, identity, background content/brightness, sharpness, and seams. Do not select a candidate on a single scalar score.

## Resources

- [Runner configuration and gate behavior](references/RUNNER.md)
- [Recipe and workflow wiring](references/RECIPE.md)
- [Experiment results and measurements](references/EXPERIMENT_RESULTS_CN.md)
- [Limitations and acceptance rules](references/LIMITATIONS.md)
- [Upstream dependencies and attribution](references/UPSTREAM_DEPENDENCIES.md)
