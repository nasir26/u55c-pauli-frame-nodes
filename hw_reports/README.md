# Archived synthesis and implementation reports

Raw Vitis/Vivado reports backing the hardware numbers in `bench/results/`.
Archived here because `build/` and `kernel/hls/*_hls/` are build directories
excluded by `.gitignore`, so these would be lost whenever the build tree is
cleaned, taking the provenance of the reported numbers with them.

| File | What it backs |
|---|---|
| `stim_frame_sampler_csynth.rpt` | Vitis HLS C-synthesis estimates (latency, II, resources) |
| `impl_1_kernel_util_routed.rpt` | Post-route utilisation scoped to the kernel, excluding the platform shell |
| `impl_1_hw_bb_locked_timing_summary_routed.rpt.gz` | Post-route timing summary (gzipped; ~16 MB raw) |

    gunzip -k impl_1_hw_bb_locked_timing_summary_routed.rpt.gz

The `hw` and `hw_emu` bitstreams are attached to the GitHub Release rather
than committed.
