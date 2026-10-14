# bench/

Benchmark scripts and hardware-validation harnesses, all against real
Alveo U55C hardware (need a built, timing-closed `build/hw/` — see
`../build/README.md`).

- `run_benchmark.py` — Mode A shots/sec, FPGA (`host/xrt_bench.cpp`) vs.
  CPU Stim, same circuits.
- `hw_tier3.py` — Tier 3 (single-fault injection vs. the DEM) run through
  the real kernel on real hardware, not the soft model.
- `hw_tier4.py` — Tier 4 (statistical equivalence, 10^7 shots) run
  through the real kernel on real hardware (`host/xrt_tier4.cpp`), vs.
  CPU Stim.
- `hw_tier5.py` — Tier 5 (logical error rate via PyMatching), surface
  code d=3/d=5, decoding real syndromes from hardware
  (`host/xrt_tier5.cpp`) and CPU-Stim-generated syndromes with the same
  matcher, comparing logical error rates.
- `results/` — committed output of the above, each tagged with machine,
  clock, batch size, and whether host setup time is included — see the
  project brief's "Working rules" for why that discipline matters.
