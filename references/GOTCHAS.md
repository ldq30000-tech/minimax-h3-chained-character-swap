# Field notes: hard cuts, multi-shot prompts, and gotchas

This page records issues that surface when you apply the POC recipe to real source footage rather than the validated 4-image portrait dance clip. Read it before chaining anything that is not an exact copy of the POC setup.

---

## 1. The prompt is six fixed sections — do not append text to the end

The template prompt is structured, in this exact order:

1. `subject_definitions`
2. `summary`
3. `retention_analysis`
4. `detailed_description`
5. `overall_soundscape`
6. `non_diegetic_music`

Shot paragraphs live **inside `detailed_description`**. When you add a new `[Shot N]`, it must go after the previous shot paragraph and **before** `overall_soundscape`.

> **Bug we hit:** naively doing `prompt = base_prompt + shot2_text` appends the new shot after `non_diegetic_music: N/A`, which is the wrong section. The model then does not parse the shot description as part of `detailed_description`, and the cut shot silently fails to replace. Insert shot text at the end of the last shot paragraph (anchor on `compression artifacts.`), not at the end of the file.

---

## 2. Hard cuts in the source video

The recipe assumes one continuous slow/medium shot. Real footage (dance edits, music videos) often has hard cuts. A single `[Shot 1]` prompt does **not** tell the model to keep replacing after a cut, so the post-cut shot degrades: identity reverts to the source performer, or the background collapses.

### 2.1 Detect cuts first

Frame-difference scan finds the cut frame cheaply:

```bash
ffmpeg -i source_slice.mp4 -vf "scale=64:36" tmp/f_%03d.png
```

```python
from PIL import Image, ImageChops, ImageStat
import glob
files = sorted(glob.glob("tmp/f_*.png"))
diffs = []
for i in range(len(files) - 1):
    a = Image.open(files[i]).convert("L"); b = Image.open(files[i+1]).convert("L")
    diffs.append((round(ImageStat.Stat(ImageChops.difference(a, b)).mean[0], 1), i))
for d, i in sorted(diffs, reverse=True)[:8]:
    print(f"frame {i} -> {i+1}: diff={d}")
```

A hard cut shows a diff spike of roughly 30–80 on a 64×36 frame; ordinary motion stays under ~15. Multiple spikes = multiple cuts.

### 2.2 Write one shot per cut, with a timestamp

`[Shot 1]` carries no timestamp. Every later shot carries `At MM:SS.mmm` where `MM:SS.mmm = cut_frame / fps`. Example for a cut at source frame 84 at 24 fps (3.500 s):

```text
detailed_description:
[Shot 1] <Subject 1> replaces the source performer throughout the shot. ... compression artifacts.
[Shot 2] At 00:03.500, the shot cuts to a new camera angle. <Subject 1> replaces the source performer throughout this new shot. Keep the same body scale, ground position, framing, scene geometry, lighting, and camera movement as <Video 1>. Every pose, transition, movement direction, speed, and rhythm follows <Video 1> at the same timestamp. Motion continues naturally without pausing, replaying, compressing time, skipping forward, or anticipating the clip boundary. Keep the background clean, stable, and correctly exposed without chromatic noise, color blotches, or compression artifacts.
```

Also update the shot lists elsewhere in the prompt:

- `summary`: `Replace the main performer in <Video 1> with <Subject 1> in every shot.`
- `retention_analysis`: `(appears in [Shot 1], [Shot 2])` instead of `(appears throughout [Shot 1])`.

**Do not write "a different environment"** for a same-location cut. Only change framing/angle language. If the cut genuinely moves to a different location, say so explicitly, but verify first — see §7.

---

## 3. Close-up shots need a face-closeup reference image

The POC uses 4 identity images: front, side, back, and **face closeup**. The face-closeup exists precisely for close-range shots.

With a single turnaround sheet (or any single image), a **close-up of the face** almost always fails: the model has no high-res facial detail to reconstruct, so it keeps the source performer's face. This is not a seed problem and not a prompt problem — it is a missing reference problem.

Fix: add a dedicated face-closeup image as `<Picture 2>` and mention it in `subject_definitions`:

```text
<Subject 1> is the target character. Their complete identity, face, hair, body, and outfit come from <Picture 1> and <Picture 2>. <Subject 1> completely replaces the main performer in <Video 1>.
<Picture 2> is a close-up of <Subject 1>'s face, providing the exact facial features, eye color, and hair details for close-range shots.
```

The runner's dynamic `reference_images` list form accepts up to 9 images and wires them as `ref_image_0..N-1`; prompt tags `<Picture N>` follow list order.

---

## 4. Seed lottery: some seeds simply do not replace

The same workflow, same prompt, different seed can produce **"output == source"** (no replacement at all) versus a correct replacement. This is a real observed failure mode, not a workflow error. Plan for it: run multiple seeds per segment and pick a winning one.

### Diagnosing "output == source"

Low pixel difference (rms < ~8 on 0–255 RGB) between the delivery frame and the corresponding source frame means the segment probably did **not** replace. A successful replacement on a mid/wide shot usually shows rms ~30+. **But** this heuristic fails on very dark or very close shots — treat it as a screen, and confirm visually.

Two distinct causes produce "output == source", and the fix differs:

- **Bad seed** → reroll with a different seed.
- **Source has no performer there** (empty frame, cut to scenery) → there is nothing to replace; skip or re-cut that region.

The runner reports a three-frame `source_rms_difference` screen and can halt on `min_source_rms_difference` (the example uses `8.0`). This catches many literal `output == source` failures, but it is **not identity recognition**: a changed background can hide a failed face replacement, and similar-looking source/target subjects can false-halt. Always eyeball the first and post-cut frames.

---

## 5. Source fps mismatch

The workflow and the Motion Context plugin are built around **24 fps**. If the source is 30 fps and you feed it as-is, the output runs 30/24 = 1.25× slow. Resample the source to 24 fps **before** slicing:

```bash
ffmpeg -i source.mp4 -vf "fps=24" -c:v libx264 -crf 18 -an source_24fps.mp4
ffmpeg -i source.mp4 -vn -acodec pcm_s16le master_audio.wav   # keep audio separately
```

Duration is preserved; the frame count becomes `round(duration × 24)`.

---

## 6. Source length may not divide evenly

The chain delivers `raw_frames − context_frames` new frames per continuation (e.g. 102). If `total_frames ≠ raw_frames + k × (raw_frames − context_frames)`, you cannot fill the last segment exactly.

Options:

- **Trim** the source to the exact total (drop the tail). This preserves the unique-timestamp acceptance rule but loses the ending.
- **Pad** the source to the exact total (clone the last frame) only as an explicitly disclosed end hold. This no longer qualifies for a “no duplicate padding” claim, even if the hold is visually harmless. Record the number and duration of padded frames.
- **Change segment length** to make it divide — but `raw_frames` is chain-wide in the runner, so this only works if you start a separate run or hand-roll the final segment.

Decide explicitly and record it. Do not silently mix padded and unpadded slices, and never claim a fully unique timeline after padding.

---

## 7. Video 1 is resized by ComfyUI, not by you

`MiniMaxH3ReferenceToVideo` resizes `Video 1` through an internal `adapt_canvas` (768 short edge, 768×1344 area cap, per-axis 32-rounding). For 16:9 footage that is **1344×768**, regardless of what you feed in.

- Feeding 1920×1080: it is downscaled to 1344×768 (wasted decode time).
- Feeding 1024×576 (≤ canvas): it is **not** upscaled — the reference stays at 1024×576 and the latent is smaller.

Practical consequence: **pre-scale the source slice to the output resolution** (e.g. 1024×576) before slicing. In our run this cut per-segment wall time from ~31 min to ~18 min with no quality loss. The reference-image path (`ref_image_size="match"`) is separate and scales to the output pixel area.

---

## 8. Single reference image boundaries

A single image can work for full/mid shots (our single turnaround sheet replaced correctly at full/mid range), but:

- It is not enough for **face close-ups** (§3).
- It does not invalidate the chain per se; "output == source" is usually seed lottery (§4) or a missing close-up ref (§3), not an inherent single-image failure.

If you must use one image, budget a face-closeup crop as a second reference the moment any segment contains a close-up.
