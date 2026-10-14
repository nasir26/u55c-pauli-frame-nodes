# Kernel utilization and timing

The resource/Fmax numbers below are `vitis_hls` C-synthesis
(`csynth_design`) estimates for `stim_frame_sampler`, not a
post-place-and-route Vivado implementation report -- that comes from a
real `hw` build, not yet attempted (see the bottom of this file for why).
Regenerate the synthesis numbers with:

```
vitis_hls -f kernel/hls/run_hls.tcl
```

from the repo root. Part and clock target live in
`kernel/hls/run_hls.tcl` (`xcu55c-fsvh2892-2L-e`, 300 MHz), not
duplicated here.

## 2026-08-27 — Phase 3 baseline

| | |
|---|---|
| Vitis HLS | 2023.2 |
| Target clock | 3.33 ns (300 MHz) |
| Estimated clock | 2.615 ns |
| **Estimated Fmax** | **382.44 MHz** |
| Loop constraints | all satisfied |

Resource utilization, against one SLR (the binding constraint — the
kernel isn't yet partitioned across the part's 3 SLRs) and against the
whole part:

| Resource | Used | SLR budget | SLR % | Part budget | Part % |
|---|---:|---:|---:|---:|---:|
| LUT | 217,052 | 434,560 | 49% | 1,303,680 | 16% |
| FF | 174,517 | 869,120 | 20% | 2,607,360 | 6% |
| DSP | 960 | 3,008 | 31% | 9,024 | 10% |
| BRAM_18K | 66 | 1,344 | 4% | 4,032 | 1% |
| URAM | 0 | 320 | 0% | 960 | 0% |

Both Fmax and every resource class clear the Phase 3 gate (>= 250 MHz,
< 70% of any single class) — with the numbers above the SLR-relative
figures, the tighter of the two ways to read the gate.

**One real finding from this run, not from review:** the first synthesis
attempt put SLR-relative LUT at 83% — over the gate. `draw_flip_mask`
(used by X/Y/Z noise) and `draw_categorical` (used by DEPOLARIZE1/2) were
two separate C++ functions, each fully unrolling its own 64-lane,
10-round Philox4x32 bank; being textually different functions gave HLS
no basis to share hardware between them even though the noise opcodes
that call them are mutually exclusive within one instruction. Merging
them into a single `draw_noise` (kernel/stim_frame_sampler.cpp) let HLS's
ordinary mutually-exclusive-branch resource sharing consolidate the two
banks into one: DSP dropped 1920 -> 960, LUT 361,185 -> 217,052, SLR LUT
83% -> 49%, with the numbers in the table above being the result. No
change to the frame/gate/detector-fold logic these numbers describe.

**C/RTL cosimulation: PASS.** `vitis_hls`'s `cosim_design` runs the actual
synthesized RTL (via XSIM) against the same C testbench used for Tier 2
(`kernel/hls_testbench/tb_stim_frame_sampler.cpp`, fed
`kernel/hls/test_vectors/instructions.bin` — regenerate with
`kernel/hls/generate_test_vector.py`) and checks RTL and C-level behavior
agree bit-for-bit. It does; independently re-checking the RTL's actual
output file against `softmodel/kernel_replay.py` (not just trusting
cosim's own PASS verdict) confirms the same thing: detector and
observable words match exactly. Chained with Tier 2 (C-sim matches the
soft model), this means soft model == C-sim == synthesized RTL,
transitively — as close to "hw_emu bit-exact" as achievable without a
full `v++ -t hw_emu` build, which needs `host/` (XRT buffers, AXI/HBM DMA
simulation) and doesn't exist until Phase 4. Reproduce with
`make hls-cosim` (`build/Makefile`).

## 2026-08-27 — instruction layering: implemented, verified, not yet exploited

Project brief section 3.1's "hard problem" -- partition the instruction
stream into layers of mutually qubit-disjoint instructions -- is now
implemented (`kernel/isa.py:_layer_and_reorder`), runs as the last step
of `encode_circuit`, and is verified: a Python-side check that no two
instructions in the same layer share a qubit passes for all three test
circuits, and `tests/test_kernel_tier2.py` confirms the reorder is
bit-exact (reordering only ever changes each instruction's own PRNG
counter value, never which qubit sees which effect in what relative
order -- see that function's docstring for the full argument). Average
instructions per layer, for the same circuits Phase 1/2 used:

| Circuit | Instructions | Layers | Avg instrs/layer |
|---|---:|---:|---:|
| repetition_code d3 | 45 | 14 | 3.2 |
| surface_code d3 | 344 | 47 | 7.3 |
| surface_code d5 | 1,674 | 77 | 21.7 |

(d=11/d=21, which the project brief also asks for here, need
`NUM_QUBITS_MAX` raised past its current 128 -- d=11 alone needs 274
qubits. That's a config change, not a redesign, but wasn't done this
pass since it wasn't needed for the d=3/d=5 target this phase has been
scoped to.) Parallelism improves with qubit count, as expected -- more
qubits means more mutually-disjoint work available per layer.

The kernel's top-level signature now takes `layer_offsets`/`num_layers`
and processes instructions in a `LAYER_LOOP` / `INSTRUCTION_LOOP` nest
matching that structure, but **the inner loop is not pipelined** in the
version this repository ships. That was tried, measured, and reverted:

**The experiment.** `#pragma HLS pipeline II=1` plus
`#pragma HLS dependence variable=fs.x/fs.z inter false` on
`INSTRUCTION_LOOP` (the dependence override is sound -- it's exactly the
frame-store guarantee layering provides). Result: HLS could only reach
**II=34**, not 1, and reaching even that pushed SLR-relative LUT usage to
**131%** -- over the 70% gate -- while Fmax *dropped* slightly (382.44 ->
351.25 MHz). A worse design on every axis that mattered for this phase's
gate, despite being a genuine attempt at the thing the gate asks for.
Per the project's own working rule ("if a fix requires an architectural
change, stop and explain the tradeoff rather than working around it"),
this was reverted rather than adopted or quietly left over-budget.

**Why II=34, not 1 -- two causes, both reported by HLS itself, not
guessed at:**
1. The detector/observable accumulators in `DetectorFold` are a real
   read-modify-write hazard layering-by-qubit doesn't touch: two
   qubit-disjoint measurements in the same layer can still both
   contribute to the *same* detector (common -- several ancillas' results
   often feed one detector), so consecutive fold() calls can have a
   genuine loop-carried dependency on the same accumulator words. HLS's
   own dependence checker correctly refused to pipeline through this
   (`HLS 200-880`, citing `detector_fold.hpp:47`) -- overriding it would
   have been unsound, not just conservative, so it wasn't.
2. `instructions` is fetched from external memory (m_axi) once per loop
   iteration, and `Instruction`'s 32-byte `detector_mask` means each
   fetch is several separate AXI transactions -- HLS reported this as its
   own, independent II-limiting factor (`HLS 200-885`, "limited memory
   ports").

Fixing (1) needs a different detector-fold structure (e.g. splitting
accumulators so same-layer measurements can't collide, or deferring the
fold to a separate pass); fixing (2) needs the instruction stream staged
on-chip (BRAM/URAM) rather than re-fetched from external memory every
iteration, or a narrower per-iteration record. Both are real, scoped
follow-up work, not attempted here.

**What shipped instead:** the layering algorithm, the layer-aware kernel
API, and the `LAYER_LOOP`/`INSTRUCTION_LOOP` structure, all with zero
pipelining directives on the inner loop -- functionally and resource-wise
equivalent to the pre-layering baseline above (Fmax unchanged at 382.44
MHz; SLR LUT 50% vs. the baseline's 49%, FF 18% vs 20%, DSP 23% vs 31%,
the small deltas being the extra loop nesting and the now-unused
`layer_offsets` array, not pipelining cost). Nothing here makes the
kernel faster yet, but the layering computation itself is done, correct,
and ready for whoever picks up the pipelining problem with (1) and (2)
solved first.

## 2026-08-31 — Phase 4: sw_emu and hw_emu both passing end-to-end

Both driven through the real stack: `v++ -c`/`-l` producing an actual
`.xclbin`, `host/xrt_runner.cpp` (real XRT C++ API) loading it and
running the kernel, output diffed against `softmodel/kernel_replay.py`
bit-for-bit -- reproduce with `make sw_emu` / `make hw_emu` then
`./run_and_validate.sh <mode>` in `build/`.

| | sw_emu | hw_emu |
|---|---|---|
| `v++ -c` (compile) | a few seconds | 2m 9s |
| `v++ -l` (link) | 13s | 7m 2s |
| Run (bit-exact vs. soft model) | **PASS** | **PASS** |

`hw_emu` is the meaningfully bigger step of the two: it simulates the
*actual synthesized kernel RTL* inside the *full platform shell*
(PCIe/XDMA/HBM controllers), via XSIM, driven by the real XRT runtime --
not an approximation of the kernel the way sw_emu's functional
simulation is. This is the closest validation to real hardware short of
the physical card, and it passed on the first attempt. The RTL
simulation itself was fast (16s of simulated time for the 45-instruction
test vector) — the ~9 minutes is `v++`'s own compile/link, not the
simulator being slow.

**Not attempted this pass: `hw`.** See below — it has been attempted
since, with a real, honest result to report.

## 2026-08-31 — `hw`: builds, real utilization excellent, timing NOT closed

`make hw` (real Vivado synthesis + implementation against
`xcu55c-fsvh2892-2L-e`) ran to completion: **3h 52m 59s**, confirming the
project brief's "potentially hours long" warning exactly. It produced a
valid `.xclbin`. Two real, opposite-direction surprises came out of
actually measuring post-route numbers instead of trusting HLS's own
estimates:

**Resource usage: real numbers beat the HLS estimate, not just cleared
it.** Post-route, `stim_frame_sampler` alone (not the platform shell)
uses, against the fabric budget left after the shell's own reservation:

| Resource | Used | Budget | % |
|---|---:|---:|---:|
| LUT | 83,843 | 1,181,758 | 7.09% |
| REG | 101,402 | 2,441,808 | 4.15% |
| DSP | 708 | 9,020 | 7.85% |
| BRAM | 21 | 1,818 | 1.16% |
| URAM | 0 | 960 | 0% |

That LUT figure is ~2.6x *lower* than the 217,052 HLS's own
`csynth_design` estimated back in the Phase 3 entry above. HLS's
estimate is a conservative upper bound from its own RTL generation, not
a substitute for what Vivado's real technology mapping and logic
optimization actually produce — worth remembering before treating any
`csynth` number as final. Whole-device numbers (platform shell included)
are similarly comfortable: 15.78% LUT of the entire part.

**Timing: NOT closed, and specifically enough to say why.** WNS
(worst negative slack) = **-0.688ns** against the 3.333ns (300 MHz)
requirement — 26,495 of 662,462 endpoints fail. Effective achievable
clock is therefore ~1/(3.333+0.688)ns ≈ **249 MHz**, just under this
project's own 250 MHz floor, not just short of the 300 MHz target. This
is a real violation on real silicon, not an estimate falling short.

The failing path is specific and worth recording exactly: source is
`ap_rst_n_inv_reg_replica_5` (one of Vivado's automatic reset-fanout
replicas), destination is deep inside `draw_noise`'s Philox instance —
specifically the modulo-divider hardware `w1 % modulus` compiles to
(`urem_32ns_4ns_8_36_seq_1`, a sequential divider). **97.8% of the 3.714ns
data path delay is routing, not logic** (0.081ns of actual logic delay).
This isn't a "too much logic" problem -- the resource table above rules
that out -- it's a physical-placement problem: this one reset-replica's
route to that specific divider instance is long enough to blow the
budget on its own, most likely because the divider ended up placed far
from where that reset replica landed.

**Not run on real hardware.** A design with unclosed timing can behave
unreliably on physical silicon in ways simulation can't show (metastability,
intermittent failures) — running this bitstream on the card would not be
a meaningful correctness check, so it wasn't attempted. Fixing this is
scoped, not open-ended (a placement constraint on the reset-fanout
network, or a lower, safely-closing clock target, are the two obvious
next moves), but each attempt costs another multi-hour `hw` build, so
it's flagged here for a deliberate decision on how to spend that time
rather than re-run speculatively.

## 2026-08-31 (later) — retargeting to 250 MHz didn't apply, and why

Second `hw` attempt, intending to retarget the build to 250 MHz (this
project's own floor) for more timing slack: 3h 17m 58s, WNS **-1.092ns**
(worse than the 300 MHz attempt's -0.688ns), 40,302 failing endpoints
(vs. 26,495). Looked like relaxing the clock target made timing *worse*
-- until the routed timing report was checked directly: the failing
path's clock still showed `period=3.333ns` (300 MHz), not the intended
4.0ns (250 MHz). The retarget never actually applied.

Root cause: `--kernel_frequency` had only been passed to `v++ -c`
(kernel compile). That flag is documented as `[impl]` -- it's the
link/implementation stage that configures the platform's clock wizard
for the kernel's actual clock domain; the `-c` copy only affects HLS's
own internal synthesis constraint during kernel compilation, and
`connectivity.cfg`'s `[hls] clock=` line wasn't sufficient on its own
either. Both `hw` attempts so far therefore targeted the *same* real
300 MHz constraint regardless of what was intended for the second one --
the WNS difference between them (-0.688 vs -1.092ns) is run-to-run
placement variability on an unchanged target, not evidence that a lower
target hurts. `vpp_build.sh` now passes `--kernel_frequency 250` to both
`-c` and `-l`; a third `hw` attempt with the fix actually applied is the
next real data point, not yet built as of this entry.

## 2026-08-31 (final) — 250 MHz actually applied: much closer, still not closed

Third `hw` attempt, 3h 14m 6s, with `--kernel_frequency 250` genuinely
reaching the implementation stage this time -- confirmed directly in the
routed report, `period=4.000ns` on the failing paths' clock, not the
3.333ns both earlier attempts silently kept. Real result, not a wash:

| | 300 MHz (1st) | 250 MHz, unapplied (2nd) | 250 MHz, applied (3rd) |
|---|---:|---:|---:|
| WNS | -0.688ns | -1.092ns | **-0.179ns** |
| Failing endpoints | 26,495 | 40,302 | **10,707** |
| Effective achievable | ~249 MHz | ~196 MHz | **~239 MHz** |

Cutting the violation from -0.688ns to -0.179ns (a ~74% reduction) is
genuine evidence the retarget helped, once it was actually wired up
correctly. Resource usage is essentially unchanged from the first `hw`
attempt (LUT 7.03% of the kernel's fabric budget, DSP 7.85%, REG 3.31%,
BRAM 1.16%, URAM 0% -- comfortable, not the constraint).

**Still not closed at 250 MHz**, and the failing path again points at
the same region as both earlier attempts: the `draw_noise` Philox
instance (this run: `mul_32ns_33ns_64_2_1_U5`, a multiplier; the first
attempt's was a divider in the same block). Three attempts, three
different specific nets, one consistent culprit -- the Philox draw
logic, which is heavily replicated (64 shot lanes) and evidently dense
enough locally to be the recurring routing bottleneck, independent of
the exact clock target.

**Where this stands:** not attempted further this pass. ~10 hours of
real `hw` build time have gone into three attempts; the remaining gap is
small (-0.179ns) but not proven to close with another parameter guess,
and the next productive move is a more targeted one -- likely a
placement constraint or extra `phys_opt_design` pass focused specifically
on the Philox block, rather than another blind clock-target change. Not
run on physical hardware, for the same reason as the first attempt: an
unclosed-timing bitstream isn't a meaningful correctness check.

## 2026-09-01 — fourth attempt: aggressive directives, WNS -0.041ns

`ExtraTimingOpt` placement, `AggressiveExplore` phys_opt_design and
route_design, plus an added post-route `phys_opt_design` pass (also
`AggressiveExplore`) -- targeted specifically at the recurring Philox
`draw_noise` bottleneck rather than another clock-target guess. 5h 32m
46s (longer than the ~3h prior attempts, as expected -- "Aggressive"
directives trade runtime for more exhaustive exploration).

**WNS: -0.041ns**, down from -0.179ns -- another ~77% cut, and a ~94%
cut from the first attempt's -0.688ns. Failing endpoints: 1,211 (down
from 10,707). Effective achievable clock ~247.4 MHz, now within a
fraction of a percent of the 250 MHz target. Resource usage: unchanged,
still comfortable.

Genuinely converging, not closed. Fifth attempt: `ROUTE_DESIGN`
directive switched from `AggressiveExplore` to `NoTimingRelaxation`
(forces full timing effort on every net rather than `AggressiveExplore`'s
own relaxation tradeoff on non-critical paths), keeping everything else
that worked. Not run on physical hardware.

## 2026-09-01 (later) — fifth attempt: timing CLOSED, and run on real hardware

Fifth `hw` build, `NoTimingRelaxation` swapped in for `route_design`,
4h 18m 57s. Result:

```
WNS(ns)  TNS(ns)  TNS Failing Endpoints  ...
  0.000    0.000                      0  ...
```

`impl_1_hw_bb_locked_timing_summary_routed.rpt`: **"All user specified
timing constraints are met."** Zero failing endpoints, WNS exactly
0.000ns. Five attempts, ~24 hours of cumulative real Vivado build time,
converging from -0.688ns -> (-1.092ns, a build bug) -> -0.179ns ->
-0.041ns -> **0.000ns**. Resource usage held steady throughout at ~7%
LUT of the kernel's fabric budget.

**This bitstream was run on the physical card.** With timing genuinely
closed, running it is a meaningful correctness check for the first time
across all five `hw` attempts. `./run_and_validate.sh hw` against the
same fixed test vector (`repetition_code:memory`, d=3) used throughout
this document: **PASS**, bit-exact against `softmodel/kernel_replay.py`.
Extended to the other two Tier 2 test circuits by hand (same seed,
against the same built `.xclbin`, no rebuild needed since only the *run*
differs): `surface_code:rotated_memory_z` d=3 (344 instructions, 24
detectors) and d=5 (1,674 instructions, 120 detectors) — **both PASS**,
bit-exact, on the real card.

This closes the loop this project set out to test: soft model == C-sim
(Tier 2) == HLS RTL cosimulation == `sw_emu` == `hw_emu` == **real
Alveo U55C silicon**, for all three circuits Tier 2 has used since
Phase 2. Not yet done: Tiers 1/3/4/5 specifically re-run *on hardware*
(as opposed to the software-only forms already passing since Phase 1)
and the shots/sec benchmark against CPU Stim that Phase 4's gate also
asks for — real remaining scope, not claimed here.
