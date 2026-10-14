# Pauli frame sampling on four FPGA nodes (Alveo U55C)

[![tests](https://github.com/nasir26/u55c-pauli-frame-nodes/actions/workflows/ci.yml/badge.svg)](https://github.com/nasir26/u55c-pauli-frame-nodes/actions/workflows/ci.yml)

A sampler of detector and observable outcomes of noisy stabilizer circuits, the workload behind threshold estimates
and decoder benchmarks in quantum error correction, implemented as a Vitis HLS kernel for the AMD Alveo U55C and run
on four cards in one host that are treated as independent nodes. It produces the same quantity as the detector
sampler of [Stim](https://github.com/quantumlib/Stim) (Gidney, Quantum 5, 497, 2021) and is validated against it.

The repository holds two kernels:

| Kernel | Where | Role |
|---|---|---|
| batch kernel | `kernel/batch/` | the design of the manuscript: one instruction slot per clock cycle, 64 shots per batch, thousands of batches per launch, two instances per card |
| first kernel | `kernel/stim_frame_sampler.cpp` | the earlier single card design (one instruction at a time, 64 shots per launch); measured baseline |

## Results

Measured on four Alveo U55C cards in one host with two AMD EPYC 7742 processors (256 hardware threads), median of
three runs, shots per second whose outputs have reached host memory (`bench/multicard/*.json`):

| Circuit | 1 card | 4 cards | Stim, 1 process | Stim, 256 processes |
|---|---:|---:|---:|---:|
| repetition code, d = 3 | 289 million | 1.16 billion | 6.0 million | 140 million |
| rotated surface code, d = 3 | 73.9 million | 296 million | 2.84 million | 124 million |
| rotated surface code, d = 5 | 15.3 million | 61.4 million | 0.72 million | 57.2 million |

- Every kernel instance on every card is bit exact against the software model `softmodel/batch_replay.py`.
- Detector and observable firing rates agree with Stim over 10 million shots per circuit (largest |z| 3.12), and
  decoded logical error rates (PyMatching) agree with Stim for surface codes of distance 3, 5 and 7 (largest |z| 1.25).
- Four cards give 3.99 to 4.00 times the rate of one card: the nodes exchange no data.

## How the batch kernel works

```
             HBM: program and fold list (copied on chip once per launch)
                     |
   noise stage  ->  frame stage  ->  fold stage  ->  store stage  ->  HBM: one word per detector per batch
   32 Threefry      Pauli frame of     measurement      bursts
   4x32-20 units    up to 128 qubits   record of the
   (64 lanes)       in LUT RAM, one    batch, XOR of
                    read and one       the listed
                    write per cycle    measurements
```

- **Noise stage.** Counter based random numbers (Threefry4x32-20, Salmon et al., SC 2011) for lane l of batch g at
  slot i, with counter (i, l / 2, g, 0) and key (seed, node + 1, 0, 0); each output serves two lanes. Noise decisions
  follow the first kernel: the channel fires if the first word is below the threshold, the second word mod 3 or mod
  15 picks the Pauli (computed with a hexadecimal digit sum, no divider).
- **Frame stage.** One read and one write of the frame per cycle; a two qubit instruction takes two consecutive
  slots. The result of slot t is written two cycles after its read, without hazard checks: the compiler
  (`kernel/isa_batch.py`) schedules every program so that two slots on the same qubit are at least 8 slots apart and
  pads it with 8 NOPs at both ends.
- **Fold stage.** Collects the measurement flips of a batch and computes every detector and observable from a list of
  (output, measurement) pairs, while the frame stage runs the next batch.
- **Nodes.** Card c uses the key (seed, c + 1, 0, 0); instance j of a card samples batches
  from j * 2^28. The host only launches work and collects results (`host/xrt_batch.cpp`).

### Implementation history

All five hardware builds (`build/batch/*/`, logs in `logs/`) used two instances at a 300 MHz target:

| Build | Frame | Generator | SLRs | Placement | Outcome |
|---|---|---|---|---|---|
| `hw` | registers | Philox4x32-10 | 0, 1 | ExtraTimingOpt | routing failed (conflicting nets) |
| `hw_mem` | LUT RAM | Philox4x32-10 | 0, 1 | ExtraTimingOpt | routing failed |
| `hw_mem_spread` | LUT RAM | Philox4x32-10 | 0, 1 | AltSpreadLogic_high | routing failed |
| `hw_tf` | LUT RAM | Threefry4x32-20 | 1, 2 | ExtraTimingOpt | **met timing at 300 MHz, used for all measurements** |
| `hw_tf_spread` | LUT RAM | Threefry4x32-20 | 1, 2 | AltSpreadLogic_high | stopped once `hw_tf` had succeeded |

The 32 Philox units of an instance needed 2,076 DSP blocks and packed the DSP columns of their region; the congestion
analysis of the first build is in `build/batch/hw/congestion/`. Threefry needs 12 DSP blocks per instance. Every build
directory keeps the sources it was built from (`src_snapshot/`), its link configuration, the bitstream hash and
metadata, and its utilisation and timing reports.

## Layout

| Path | What |
|---|---|
| `kernel/batch/` | batch kernel (`stim_batch_sampler.cpp`), sizing (`batch_config.hpp`), C testbench, HLS script, cosimulation vectors and reports (`hls/`) |
| `kernel/isa_batch.py` | slot encoding, dense qubit numbering, fold list, hazard distance schedule |
| `kernel/isa.py` | circuit compiler shared by both kernels (Stim circuit to instructions, detector resolution) |
| `kernel/*.hpp`, `kernel/stim_frame_sampler.cpp` | first kernel |
| `softmodel/` | bit exact models: `kernel_replay.py` (frame semantics), `batch_replay.py` (batch kernel), `threefry.py`, `philox.py`, `reference_sampler.py` |
| `host/` | XRT host programs: `xrt_batch.cpp` (batch kernel, all instances of a card), `xrt_runner.cpp`, `xrt_bench.cpp`, `xrt_tier4.cpp`, `xrt_tier5.cpp` (first kernel) |
| `tests/` | software and C simulation tests of both kernels (`pytest`) |
| `build/` | `batch_build.sh` (v++ build of the batch kernel), the first kernel's build flow, `build/batch/<build>/` reports |
| `bench/multicard/` | CPU Stim baseline, card check and 1/2/4 card benchmark (`run_batch.py`), validation against Stim (`validate_batch.py`), campaign script, raw JSON results |
| `bench/results/`, `hw_reports/`, `docs/` | measurements, reports and implementation notes of the first kernel |
| `logs/` | build and campaign logs |
| `paper/` | manuscript sources, generated tables and macros, figures, reference verification, compiled `manuscript.pdf` |

## Workflow: from a circuit to the paper

```
Stim circuit
   |  kernel/isa.py, kernel/isa_batch.py           compile, renumber qubits, schedule slots, build fold list
   v
program words + fold list
   |  tests/ (C simulation, schedule checks)       bit exact against softmodel/batch_replay.py
   |  kernel/batch/hls/run_hls.tcl                 HLS synthesis and RTL cosimulation, bit exact
   |  build/batch_build.sh                         bitstream (build/batch/<build>/)
   v
four cards
   |  bench/multicard/campaign_batch.sh           check every instance bit exact, benchmark 1/2/4 cards,
   |                                               validate against Stim, measure CPU Stim
   v
bench/multicard/*.json
   |  paper/scripts/make_numbers.py, make_figures.py
   v
paper/generated/ -> paper/main.tex -> paper/manuscript.pdf
```

No number in the manuscript is typed by hand: each one is a macro or table in `paper/generated/`, produced from the
JSON results, the HLS and cosimulation reports and the build reports. Every reference was checked against Crossref or
arXiv (`paper/refs/verification_report.md`).

## Requirements

- Alveo U55C, shell `xilinx_u55c_gen3x16_xdma_base_3` (platform `xilinx_u55c_gen3x16_xdma_3_202210_1`), XRT 2.16;
  Vitis and Vitis HLS 2023.2 to build the kernel or run the cosimulation.
- Python 3.11 with `requirements.txt` (Stim 1.13.0 was used for every comparison, PyMatching 2.4.0, NumPy); the
  Threefry test also uses the `randomgen` package if it is installed.

## Quick start (no FPGA needed)

```
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt "stim==1.13.0"
python -m pytest -q        # both kernels in C simulation, schedule checks, Threefry known answers, rates against Stim
```

The batch kernel's C simulation needs the Vitis HLS headers (`ap_int.h`, `hls_stream.h`); without them those tests
are skipped.

## Building and measuring

```
source /tools/Xilinx/Vitis/2023.2/settings64.sh; source /opt/xilinx/xrt/setup.sh
SLRS="1 2" build/batch_build.sh hw 300 2 hw_tf          # two instances in SLR1 and SLR2 at 300 MHz (about 2 hours)
bench/multicard/campaign_batch.sh hw_tf                  # check, benchmark, validation and CPU baseline
cd paper && STIM_BUILD=hw_tf python scripts/make_numbers.py && python scripts/make_figures.py
```

`make_numbers.py` reads the post route reports from the build directory (`build/batch/<build>/_x/`), which is not
stored here; the tables generated from it are in `paper/generated/`.

## Citation

N. Ali, "Pauli Frame Sampling on Four FPGA Nodes: A Pipelined Stabilizer Circuit Sampler for Quantum Error Correction
on Alveo U55C Accelerators", manuscript, 2026 (`paper/manuscript.pdf`). The cards as nodes model follows N. Ali,
"Multi-FPGA Quantum Circuit Simulation: A Distributed Statevector Architecture on Four Xilinx Alveo U55C
Accelerators", Research Square preprint, doi:10.21203/rs.3.rs-10128153/v1.

## Author

Nasir Ali, Embedded Systems Division, Centre for Development of Advanced Computing (C-DAC), Noida, India.

## License

Apache License 2.0, see `LICENSE` and `NOTICE`. No source of Stim or PyMatching is included; both are used, unmodified,
as references for validation.
