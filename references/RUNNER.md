# Guarded chain runner

`scripts/run_chain.py` automates one deterministic continuation chain through ComfyUI's native `/prompt` and `/history/<prompt_id>` endpoints.

It does not use `pi-comfyui-paint` directly because it needs to create a new context input from the previous output after every job. It can coexist with `pi-comfyui-paint`: both submit ordinary API-format workflows to the same ComfyUI server, but do not queue both onto the same GPU without considering RAM.

## What the runner does

For each entry in `segments`:

1. Extracts the final `context_frames` from the previous **clean delivery**.
2. Produces an injected temporary context using the configured taper.
3. Copies that temporary context, the source segment, and identity images into the configured local `ComfyUI/input` directory under a run-unique name.
4. Patches a copy of the workflow: input filenames, plan seed/length, run name, and separate raw/delivery SaveVideo prefixes.
5. Submits the workflow to ComfyUI and waits for its native history record.
6. Archives raw output, trim output, workflow snapshot, submission, history, context files, phase report, and `qa.json` under `run_dir/segments/<name>/`.
7. Measures phase-screen offsets/NCC, normalized first-frame seam difference, resolution-matched Laplacian sharpness ratio, and a three-frame RGB source-difference screen for the known `output == source` failure.

It does not perform seed rerolls, visual QA, content selection, output upscaling, audio muxing, or final assembly.

## Preconditions

- `initial_delivery` is an already generated clean first segment. It must contain at least `context_frames` frames. Use `assets/workflows/t3-ref2va-initial-template.json` to produce it: same resolution, fps, steps, prompt, and identity references as the chain, and review it visually before chaining — continuations inherit its content.
- `initial_delivery` and every `segments[].source` must be **24 fps**. The runner rejects other frame rates before submitting an expensive job.
- Every `segments[].source` file is a pre-cut source slice with exactly `raw_frames` frames. Its first `context_frames` must overlap the source time interval that the continuation expects.
- The workflow must have the configured LoadVideo, plan, raw SaveVideo, and trimmed SaveVideo node IDs. The supplied template defaults are `43`, `101`, `100`, `19`, and `108`.
- `comfy_input_dir` and `comfy_output_dir` must point to the local or mounted folders used by the same ComfyUI server at `comfy_url`. **Verify these — ComfyUI is often launched with `--output-directory` (and sometimes `--input-directory`) pointing outside `ComfyUI/`.** The runner only checks that the paths are existing directories; a wrong `comfy_output_dir` fails later as "no local video output found" after a successful generation. Check the ComfyUI launch command or `/object_info` for the real output directory.
- The compatible H3 Motion Context custom nodes must already be installed on that server.

## Reference images

Two configuration forms are accepted:

- **Dynamic (recommended):** `"reference_images": ["front.png", "side.png", "back.png", "face.png"]`. The runner locates the workflow's `MiniMaxH3ReferenceToVideo` node, removes its existing reference wiring, creates one `LoadImage` node per image, and wires them as `ref_image_0..N-1`. The node accepts up to 9 images. Prompt tags (`<Picture 1>` …) follow this list order. If the count differs from the template's four images, provide a matching `prompt`; the runner does not rewrite picture references inside prose.
- **Explicit:** `"reference_images": {"40": "front.png", ...}` maps image paths onto existing `LoadImage` node ids, for workflows whose wiring should stay untouched.

## Optional source-audio mux

Deliveries are silent video. For character replacement the original soundtrack is usually the deliverable audio, so each segment may carry:

```json
{"name": "seg02", "source": "...", "audio": {"file": "/path/to/master_audio.wav", "offset_seconds": 4.250}}
```

`offset_seconds` is where this segment's source slice starts on the master audio timeline. The runner muxes exactly `delivered_frames / fps` seconds beginning at `offset_seconds + context_frames / fps`, because the delivery's first frame corresponds to the source slice's first post-context frame. The silent `delivery.mp4` stays untouched as the lineage/context artifact; the muxed copy is written as `delivery_with_audio.mp4` and recorded in `qa.json`.

H3's natively generated audio (`generated_audio` / `source_plus_timeline` chain modes) is a workflow-level feature and is not wired in the supplied template; the runner does not extract it.

## Generation settings

| Config key | Meaning |
|---|---|
| `prompt` | Shot prompt written into the chain plan. Top-level sets the default; a segment may override with its own `prompt`. If omitted, the template's built-in generic prompt is used. |
| `width` / `height` | Output dimensions (positive multiples of 32). Overrides the template's 576×1024. Chain-wide only — do not change mid-chain. |
| `raw_frames` | Frames per generation (default `124`). Must follow H3's `17k+5` grid (90, 107, 124, 141, …) and exceed `context_frames`. Each continuation delivers `raw_frames − context_frames` frames. |
| `context_frames` | Carried context length (default `22`). The Motion Context plugin only accepts `1/5/22/39`; `22` is the validated value. |
| `steps` | Sampling steps; omit to keep the template value. |
| `segments[].noise_seed` | Optional deterministic chroma-noise pattern seed. Defaults to a stable value derived from the segment generation seed. |

## Numerical gates

All gate fields are optional. Omit one to observe but not halt on that metric.

| Config key | Meaning | Typical documented value |
|---|---|---:|
| `max_abs_phase_offset` | Largest absolute phase-screen offset across subranges | `2` |
| `min_phase_ncc` | Lowest phase-screen NCC | `0.3` (collapse detector; see note) |
| `max_seam_diff` | Brightness-normalized difference between prior delivery tail and new delivery head | `0.04` |
| `min_sharpness_ratio` | Resolution-matched Laplacian variance of delivery divided by matching source region | task-specific; example `0.75` |
| `min_source_rms_difference` | Mean RGB RMS difference at three aligned frames; screens `output == source` | task-specific; example `8.0` |

A phase screen is a screening metric, not pose certification. `min_source_rms_difference` is also only a screen: similar-looking target/source identities or empty frames can false-halt, while a changed background can hide a failed face replacement. Passing all gates is permission to continue automatically, not a declaration that a clip is production-ready.

> **Gate values are baseline-specific.** The documented defaults were measured on a 576×1024 portrait, 4-image POC. Landscape output, a single reference image, or low-texture (e.g. chibi) subjects shift `min_phase_ncc` and `max_seam_diff` substantially — a real, human-accepted candidate can show `min_phase_ncc` ≈ 0.15 and `seam_diff` ≈ 0.22. Retune or disable gates for non-POC setups rather than trusting the defaults.

> **Note on `min_phase_ncc`:** the motion-energy NCC is noisy on small-figure or low-texture subranges — real candidates that passed human review have shown `0.3–0.5` on one subrange (typically the tail). A strict value like `0.78` will false-halt on genuinely good generations. Treat this gate as a collapse detector, and let `max_abs_phase_offset` carry the phase judgement.

## Halt contract

When a gate fails, the runner:

- keeps every output and measurement;
- appends the candidate to `completed_segments` so its exact lineage is recorded;
- writes `status: "needs_agent_review"` plus a detailed `halt` object to `STATE.json`;
- exits with status `2`;
- makes a plain rerun stop again without submitting a new job.

An agent should inspect at least:

```text
run_dir/STATE.json
run_dir/segments/<failed>/qa.json
run_dir/segments/<failed>/phase_screen.txt
run_dir/segments/<failed>/raw.mp4
run_dir/segments/<failed>/delivery.mp4
```

If the failure is acceptable after dynamic visual review, `--approve-latest` records explicit approval both in `STATE.json` and the segment's `qa.json`, then continues with that delivery as the next context source. If it is not acceptable, reroll or replace that segment explicitly; do not use `--approve-latest` as a magic “ignore QA” button.

The runner stores SHA-256 fingerprints of the config and workflow when a run starts. It refuses to resume if either file changes, preventing accidental mixed lineage. Start a new `run_dir` for a changed configuration. State files created by runner version 1 have no fingerprints and must be deliberately migrated or restarted.

> **There is no built-in reroll command.** To reroll a segment, submit the same workflow with a new seed (outside the runner, e.g. a small script that patches `plan_json.shots[0].seed` and the SaveVideo prefixes), archive the result under the segment directory, and continue from the replacement delivery. See [field notes §4](GOTCHAS.md) for the seed-lottery pattern.

## Example

```bash
mkdir -p runs
cp examples/chain-config.example.json runs/my-chain.json
# Edit paths and source slices. Keep this file outside git if it contains local paths.
python3 scripts/run_chain.py runs/my-chain.json

# Only after review of a halted candidate:
python3 scripts/run_chain.py runs/my-chain.json --approve-latest
```

The generated run directory is ignored by `.gitignore` if placed under `runs/`.
