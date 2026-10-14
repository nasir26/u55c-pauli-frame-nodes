# kernel/hls_testbench/

C-sim testbenches, compiled and run with plain g++ (see
`kernel/ap_uint_shim.hpp`), wired into `pytest` rather than run manually:

- `tb_prng.cpp` — Philox4x32-10 determinism, sensitivity, and a
  regression value cross-checked against `softmodel/philox.py`.
  Run via `tests/test_kernel_unit_testbenches.py`.
- `tb_gate_ops.cpp` — every gate against its hand-derived symplectic
  transform. Run via `tests/test_kernel_unit_testbenches.py`.
- `tb_stim_frame_sampler.cpp` — the Tier 2 end-to-end harness: runs a
  compiled instruction stream through the real kernel and writes raw
  detector/observable output for `tests/test_kernel_tier2.py` to diff
  against `softmodel/kernel_replay.py`.
