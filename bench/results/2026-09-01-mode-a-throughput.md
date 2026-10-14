# Mode A throughput: FPGA vs. CPU Stim, real hardware

Reproduce with `python3 bench/run_benchmark.py` (needs a built
`build/hw/` with a timing-closed `.xclbin` loaded on a real Alveo U55C —
see `build/README.md`).

## Methodology (per the project brief's own discipline)

| | |
|---|---|
| Machine | AMD EPYC 7742, 64 cores (same machine as all synthesis/build numbers in `docs/utilization.md`) |
| FPGA | Alveo U55C, kernel clock **250 MHz** (real, timing-closed — see `docs/utilization.md`'s fifth `hw` attempt) |
| FPGA batch size | `SHOTS = 64` per kernel launch (`python/stim_u55c/config.py`) |
| FPGA timing | Excludes device/xclbin load and the one-time instruction upload; includes per-run kernel launch, execution, and output DMA sync, double-buffered (2 `xrt::run` in flight, `host/xrt_bench.cpp`) so host-side setup for run *i+1* overlaps run *i*'s execution — steady-state throughput, not one-shot latency |
| FPGA shots/run | 5,000 kernel launches per circuit (320,000 shots total) |
| CPU Stim | Stim 1.13.0, `compile_detector_sampler().sample(shots=2_000_000, bit_packed=True)`; timer starts after `compile_detector_sampler()`, so DEM/circuit compilation is excluded, matching the FPGA side's "steady-state" cut |

## Results

| Circuit | FPGA shots/sec | CPU Stim shots/sec | CPU / FPGA |
|---|---:|---:|---:|
| repetition_code d=3 | 865,117 | 4,867,590 | 5.6x |
| surface_code d=3 | 160,104 | 2,260,033 | 14.1x |
| surface_code d=5 | 33,587 | 512,610 | 15.3x |

## Honest reading

**CPU Stim is faster than this kernel, on these circuits, today.** This
is not a favorable number, and it isn't dressed up as one — the project
brief's own rule is not to claim a speedup without stating exactly what
was compared and how, and the flip side of that rule is not hiding a
result that goes the other way either.

Two concrete, already-known reasons, not new findings:

1. **`INSTRUCTION_LOOP` is unpipelined** (`docs/utilization.md`, Phase 3):
   each instruction costs 75-496 cycles depending on opcode, so a
   1,674-instruction circuit (surface d=5) costs on the order of
   hundreds of microseconds of kernel execution *before* any host
   overhead, every single launch. Achieving II=1 within instruction
   layers was attempted and reverted in Phase 3 (measured over the SLR
   resource budget for the throughput it bought) — this benchmark is
   direct evidence of the cost of not having solved that yet.
2. **`SHOTS = 64` is a small batch**, chosen in Phase 2 for C-sim/synthesis
   convenience, not throughput — `python/stim_u55c/config.py` says as
   much ("the most direct throughput lever once there are real part
   utilization figures to size against"). Every kernel launch pays fixed
   per-run host/PCIe overhead regardless of batch size; a larger `SHOTS`
   amortizes that overhead over more shots per launch, directly raising
   shots/sec without touching the kernel's per-instruction cost at all.

CPU Stim, for comparison, is Gidney's own hand-vectorized SIMD frame
simulator — a mature, heavily optimized competitor, not a strawman. Both
of the above are real, scoped, already-identified levers (not open
questions), and neither has been pulled yet. This number is the honest
baseline they'd be measured against.
