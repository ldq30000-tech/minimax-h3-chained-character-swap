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
- Source video is decoded, normalized to 24 fps, counted, and segmented automatically.
- H3-valid inference-only tail padding is removed from final delivery.
- Every continuation uses the previous clean delivery as Motion Context lineage.
- All segments are checkpointed and may be recovered without rerendering.
- The final MP4 is trimmed to the exact normalized source frame count and receives
  the original source soundtrack.
- `res_multistep`, `beta`, 20 steps, and denoise 1.0 are the stable defaults.

The local integration run used 614 source frames at 576x1024 and produced a
614-frame, 24 fps, 25.583333-second H.264/AAC final. The six-segment run completed
in about 2 hours 29 minutes on an RTX 5070 Ti Laptop GPU with 12 GB VRAM. This is
an environment-specific validation record, not a performance or minimum-memory
guarantee.
