# T3 recipe: Motion Context + tapered chroma-noise injection

## Scope

Use this recipe to extend a MiniMax H3 Ref2VA character-replacement shot when the source performance is slow or medium paced and continuity matters more than strict frame-perfect motion transfer.

## Validated reference configuration (POC baseline)

| Item | Value |
|---|---|
| Output | 576×1024, 24 fps |
| H3 sampling | beta scheduler, 20 steps |
| Segment length | 124 frames (adjustable — see below) |
| Context | 22 frames, video/head mode |
| New delivery per continuation | 102 frames after trim |
| Identity references | 4 images in the supplied template |
| Taper recipe | 22f tail; 19f @ 0.45; final 3f taper to 0.10 |

H3 lengths must follow the model's `17k + 5` grid. Dimensions must be multiples of 32.

**Segment length is configurable.** Shorter (e.g. 90f) or longer (e.g. 141/158f) segments are valid as long as they stay on the grid and exceed `context_frames`. Each continuation delivers `raw_frames − context_frames` new frames, so shorter segments mean more seams and more context overhead per delivered minute; longer segments accumulate more within-segment drift per generation. The model's trained range is ~124–362f — treat values outside it as experimental.

**Resolution is a validated baseline, not a requirement.** 576×1024 is simply where the taper recipe and the gate values were measured. Match the output aspect ratio to your source (e.g. 960×544 for 16:9 footage) instead of center-cropping landscape footage into portrait. If you change resolution, treat the documented gate values as starting points, not guarantees.

## Input roles

- **Pictures 1–4:** target identity and outfit.
- **Video 1:** source performance; owns every output timestamp's motion, framing, and environment.
- **Motion Context external video:** the previous generated delivery's 22-frame tail, injected only in a temporary copy.

Do not make the prompt re-describe every dance move. Let Video 1 own the movement; let the pictures own the identity.

## Per-segment procedure

1. Generate segment 1 without context — use `assets/workflows/t3-ref2va-initial-template.json` (a field-validated no-context variant of the chain template). Keep resolution, fps, steps, prompt, and identity references identical to what the chain will use; review it visually for **both identity and motion** before chaining from it, because every continuation inherits its content.
2. Save segment 1's clean delivered MP4.
3. For segment N, extract the final 22 frames from segment N-1's **clean** delivered MP4.
4. Inject the temporary context copy:

   ```bash
   python3 scripts/inject_tail_taper.py clean.mp4 injected.mp4 22 0.45 0.10 3
   ```

5. Load `injected.mp4` in the workflow's `MiniMaxH3ChainExternalVideo` branch.
6. Load the next source slice into Video 1. Preserve the 22-frame source overlap required by the chain's timing plan.
7. Generate the 124-frame raw output. Its first 22 frames reconstruct the carried context.
8. Run `MiniMaxH3LoopTrim` with 22 frames. Deliver only raw frames 22–123.
9. Audit and select candidates. The next segment's context must be extracted from the selected **clean trimmed delivery**, never from the injected context or untrimmed raw output.

## Taper rationale

A constant 0.45 injection can increase sharpness but leaves chroma residue immediately after the trimmed boundary. A low 0.20 injection did not produce the sharpness benefit in the reference POC. The three-frame taper leaves the earlier context strongly corrupted while softening the state nearest to the seam.

This is an empirical recipe, not a verified description of H3 internals.

## Workflow template

Open `assets/workflows/t3-ref2va-context-taper-template.json` in ComfyUI and replace these files in `ComfyUI/input`:

```text
character_front.png
character_side.png
character_back.png
character_face_closeup.png
source_segment_124f.mp4
context_tail22_taper.mp4
```

Also replace the generic prompt with the target character, source-performer role, and environment. Keep the role allocation intact.
