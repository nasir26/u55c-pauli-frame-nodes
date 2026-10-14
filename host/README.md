# host/

XRT host runtime.

- `xrt_runner.cpp` — Mode A, single-shot correctness checking: loads a
  compiled instruction stream (`kernel/isa.py:Program.serialize()`), runs
  it once on the real `stim_frame_sampler` kernel via XRT (works
  unchanged against sw_emu, hw_emu, or real hardware — see
  `../build/vpp_build.sh`), and writes raw detector/observable output in
  the same format `kernel/hls_testbench/tb_stim_frame_sampler.cpp` does,
  so the same Python-side comparison against `softmodel/kernel_replay.py`
  validates both. See its own header comment for why the packed
  instruction file gets parsed into native `Instruction` structs before
  it's DMA'd to the device, rather than copied as-is. Reloads the xclbin
  every process invocation, which is fine for a one-off check but
  dominates runtime completely for anything repeated -- see `xrt_bench.cpp`.
- `xrt_bench.cpp` — Mode A throughput: loads the device/xclbin/instruction
  stream *once*, then launches the kernel many times back to back with 2
  `xrt::run` objects in flight (per the project brief's own Mode A
  description) so host-side setup for run *i+1* overlaps run *i*'s
  execution. Reports shots/sec; see `../bench/run_benchmark.py` and
  `../bench/results/`.
- `xrt_tier4.cpp` — same loop and pipelining as `xrt_bench.cpp`, but
  accumulates per-detector/per-observable fired counts across all runs
  instead of discarding output, for Tier 4 (statistical equivalence) at
  real shot counts (10^7) without needing to move an unwieldy amount of
  raw per-shot data off the device. See `../bench/hw_tier4.py`.
- `xrt_tier5.cpp` — same loop and pipelining again, but writes full
  per-shot bit-packed syndrome data (not just counts), transposed on the
  host from the kernel's detector-major output into PyMatching's
  shot-major b8 convention -- Tier 4 only needed firing rates, Tier 5
  needs actual syndromes to decode. See `../bench/hw_tier5.py`.
- Mode B (low-latency, polled `xrt::run::state()`, host-memory bridge) is
  a separate runtime — Phase 5, not written yet.
- `scheduler.cpp` / `stim_bridge.cpp` from the original repo layout
  aren't separate files here: instruction-stream compiling and layer
  scheduling live in `kernel/isa.py` (Python), and Stim parsing/reference
  sampling in `softmodel/reference_sampler.py` — see those modules' own
  docstrings for why keeping them in Python was the right call rather
  than porting to C++ for its own sake.
