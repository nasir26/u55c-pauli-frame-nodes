# kernel/

HLS C++ kernel sources and the ISA that drives them.

- `isa.py` / `generate_headers.py` — single source of truth for opcode
  numbering and sizing constants; generates `opcodes.hpp`, `config.hpp`,
  `depolarize2_table.hpp`. Run `python3 kernel/generate_headers.py` after
  editing `isa.py` or `python/stim_u55c/config.py` (CI checks staleness —
  `tests/test_generated_headers_up_to_date.py`).
- `frame_store.hpp` — URAM-resident, shot-axis-partitioned Pauli frame.
- `gate_ops.hpp` — every Clifford gate as a read-XOR-write.
- `prng.hpp` — Philox4x32-10, counter-based, bit-identical to
  `softmodel/philox.py`.
- `detector_fold.hpp` — measurement → detection-event reduction, folded
  compile-time (no runtime measurement-history buffer needed — see its
  docstring).
- `instruction.hpp` — the in-memory instruction record and binary reader.
- `stim_frame_sampler.cpp` / `.hpp` — the top-level kernel function.
  Deliberately at global scope rather than inside `namespace stim_u55c`
  — see `stim_frame_sampler.hpp`'s comment for why (a Vitis HLS
  cosimulation quirk with namespaced top functions).
- `ap_uint.hpp` — selects real `<ap_int.h>` (real HLS synthesis,
  `STIM_U55C_USE_XILINX_AP_INT`) or the portable `ap_uint_shim.hpp`
  (plain g++, no Vitis install needed — Tier 1/2 CI and local testing).
  Both expose the same unqualified `ap_uint<N>` surface, so no kernel
  source changes between the two.
- `hls/` — real `vitis_hls` C-synthesis and C/RTL cosimulation
  (`run_hls.tcl`); results in `../docs/utilization.md`. Local-only, not
  in CI (needs a Vitis install) — run via `make hls-synth` /
  `make hls-cosim` in `../build/`.
- `hls_testbench/` — one C-sim testbench per module, plus the end-to-end
  Tier 2 comparison (`tb_stim_frame_sampler.cpp`, reused by `hls/` for
  cosimulation).

Layered/hazard-free instruction scheduling (the project brief's section
3.1) is implemented and verified: `isa.py:_layer_and_reorder` partitions
the compiled instruction stream into layers of mutually qubit-disjoint
instructions, and the kernel processes them via a `LAYER_LOOP` /
`INSTRUCTION_LOOP` nest matching that structure. What it doesn't do yet
is exploit that structure for pipelining — `INSTRUCTION_LOOP` carries no
`#pragma HLS pipeline`. That was tried (`II=1` plus a `dependence`
override on the frame store, which the layering guarantee makes sound)
and reverted: HLS could only reach II=34 anyway, gated by two things
layering-by-qubit doesn't address — a real hazard on `DetectorFold`'s
accumulators, and per-instruction AXI fetch cost from `Instruction`'s
32-byte `detector_mask` — and getting even that far pushed SLR LUT usage
to 131%, over budget, for a worse Fmax too. Full account, including what
solving those two would take, in `../docs/utilization.md`. The kernel is
functionally correct either way — a same-qubit hazard just costs cycles,
not correctness — which is what let Phase 2's Tier 2 gate and every
synthesis/cosimulation run so far proceed without any of this.
