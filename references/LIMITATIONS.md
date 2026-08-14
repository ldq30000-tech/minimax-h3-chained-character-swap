# Limitations and acceptance rules

## What this workflow can do

- Chain slow-to-medium-speed H3 Ref2VA character-replacement clips.
- Keep a repeated context region out of the delivered timeline via trimming.
- Improve measured delivery sharpness in the documented 576×1024 dance task.
- Provide reproducible candidate screens for phase and seam review.

## What it does not prove or guarantee

- It does not provide hard pose/keypoint control.
- It does not guarantee every generated frame is source-faithful.
- It does not make fast combat, sudden direction changes, or complex action reliable.
- It does not prove a specific internal “repair mode” exists in MiniMax H3.
- It does not replace human review with a scalar metric.

## Do not use this as the primary route when

- The source has fast combat, abrupt speed changes, or **frequent** cuts. A handful of hard cuts is workable — write one `[Shot N] At MM:SS.mmm` per cut, see [field notes §2](GOTCHAS.md).
- Strict per-frame choreography or timing is a delivery requirement.
- The image identity must be exact at extreme close range **and you have no face-closeup reference image** — close-ups fail with only a full-body/sheet reference, see [field notes §3](GOTCHAS.md).

For fast motion, prefer independent source-aligned blocks plus a generated tail-frame anchor, then use multiple seeds and dynamic human review.

## Seed lottery

The same workflow, same prompt, different seed can produce **"output == source"** (no replacement at all) versus a correct replacement. This is a real, observed failure mode, not a workflow error. Plan for it: run several seeds per segment and pick a winner. The runner's optional RGB source-difference gate catches many literal copies but is not identity recognition — always eyeball the first and post-cut frames. See [field notes §4](GOTCHAS.md) for diagnosis (bad seed vs. no performer in the source).

## Acceptance checklist

1. **Timeline:** every delivered frame maps to a unique source timestamp; no duplicate padding.
2. **Context exit:** inspect raw frames 22–23; reject visible chroma-noise leakage.
3. **Motion:** screen candidates, then review pose phase and velocity in motion.
4. **Identity:** inspect face, hair, outfit, and hands across the whole segment — especially **close-up shots**, which need a face-closeup reference image.
5. **Background:** check brightness, scene identity, noise, and zoom drift — especially **after hard cuts**, where the background can collapse or overexpose.
6. **Seams:** use seam diff as an aid, then watch seams at real speed.
7. **Candidate selection:** phase score alone is insufficient; include image-content gates.

## Reference POC result

On one 576×1024 dance task, clean context produced a sharpness ratio of `0.792`. Constant 0.45 injection gave `0.865` but leaked at the boundary. The 3-frame taper produced `0.855`, with a seam diff of `0.0198` versus `0.0175` clean, identical phase-screen offsets, and no observed f22–23 residue. This is task-specific evidence, not a general benchmark.
