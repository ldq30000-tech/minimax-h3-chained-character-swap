# Final native-loop workflow release

## Main deliverable

Import `assets/workflows/h3-native-loop-final-stable-ui.json` into ComfyUI.
It is the supported release variant of the final single-canvas workflow.

The user-authored final canvas also contained an enabled Turbo 4-step LoRA route.
That route is preserved separately as
`assets/workflows/h3-native-loop-final-turbo-experimental-ui.json`, but it is not
the default because the included `pruned_int8_convrot` model and the Turbo LoRA
have incompatible AdaLN dimensions. The experimental graph requires a compatible
non-pruned base and the LoRA's documented sampling schedule.

## Validated behavior

- One native ComfyUI canvas exposes the complete recursive generation body.
- Source metadata is counted and segmented automatically at 24 fps without
  materializing the complete source as one float32 `IMAGE` tensor.
- Each recursive pass decodes only the exact current source-scene window. Real
  source timestamps remain unique; only inference-only final padding may repeat
  the last source frame.
- H3-valid inference-only tail padding is removed from final delivery.
- Every continuation uses the previous clean delivery as Motion Context lineage.
- All segments are checkpointed and may be recovered without rerendering.
- Checkpoint lineage combines the identity-reference fingerprint with the encoded
  source-video fingerprint, preventing accidental resume after replacing the source.
- The final MP4 is trimmed to the exact normalized source frame count and receives
  the original source soundtrack. Sources without a decodable audio track receive
  same-duration 44.1 kHz mono silence instead of failing preparation.
- The final exact-trim output publishes a playable video preview and its saved path
  directly on the workflow canvas.
- `res_multistep`, `beta`, 20 steps, and denoise 1.0 are the stable defaults.
- The stable low-VRAM profile caps scenes at 107 frames so 12 GB systems avoid
  the observed 141/158-frame paging cliff. The planner may add inference-only
  tail padding to keep every scene bounded; Exact Trim removes it from delivery.
- The low-VRAM profile retains 576x1024 output, 24 fps indexing, 20 steps, the
  existing seeds, and 22-frame Motion Context. It does add some segment boundaries,
  so seam review remains required and identical output quality is not guaranteed.

The historical local integration run used the earlier 124-frame scene profile with
614 source frames at 576x1024 and produced a 614-frame, 24 fps, 25.583333-second
H.264/AAC final. That six-segment run completed in about 2 hours 29 minutes on an
RTX 5070 Ti Laptop GPU with 12 GB VRAM. The current 107-frame profile targets lower
peak memory and has not been given the same full-duration benchmark. These records
are not a performance or minimum-memory guarantee.

The streamed-source update was additionally checked against the actual 768-frame,
720x1280, 30 fps, 25.6-second reference. Preparation reported 614 unique 24 fps
frames and six scenes. Scene 1 decoded normalized frames `0:124`; scene 2 decoded
the overlapped `102:226` window. Each returned frame tensor was 1.277 GiB, and the
test process returned to about 0.54 GiB after releasing each scene instead of
retaining a full-video frame batch. This decode-only check did not rerun the full
two-and-a-half-hour H3 sampling pass.
