#!/usr/bin/env python3
"""Mode A shots/sec benchmark: FPGA (real hardware, via host/xrt_bench.cpp)
vs. CPU Stim, same circuits, same shot semantics. Reproduces the numbers
in bench/results/.

Requires: a built build/hw/ (make hw && g++ host/xrt_bench.cpp, see
build/README.md) and a real Alveo U55C with a timing-closed xclbin
loaded -- this runs actual hardware, not emulation.

Per the project brief's own discipline ("report the machine, the clock,
the batch size, and whether timing includes host setup"): the FPGA timer
(host/xrt_bench.cpp) starts after device/xclbin load and the one-time
instruction upload, and stops after the last run's output DMA sync -- it
measures steady-state per-run throughput, not one-shot latency. CPU
Stim's timer starts after compile_detector_sampler() (circuit/DEM
compilation excluded), matching the same "steady-state sampling" cut.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import stim

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kernel"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
from isa import encode_circuit  # noqa: E402
from stim_u55c.config import SHOTS  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_XCLBIN = _REPO_ROOT / "build" / "hw" / "stim_frame_sampler.xclbin"
_XRT_BENCH = _REPO_ROOT / "build" / "hw" / "xrt_bench"

_CIRCUITS = {
    "repetition_code_d3": lambda: stim.Circuit.generated(
        "repetition_code:memory", rounds=3, distance=3,
        before_round_data_depolarization=0.05, before_measure_flip_probability=0.05,
    ),
    "surface_code_d3": lambda: stim.Circuit.generated(
        "surface_code:rotated_memory_z", rounds=3, distance=3,
        before_round_data_depolarization=0.02, before_measure_flip_probability=0.02,
        after_clifford_depolarization=0.02, after_reset_flip_probability=0.02,
    ),
    "surface_code_d5": lambda: stim.Circuit.generated(
        "surface_code:rotated_memory_z", rounds=5, distance=5,
        before_round_data_depolarization=0.02, before_measure_flip_probability=0.02,
        after_clifford_depolarization=0.02, after_reset_flip_probability=0.02,
    ),
}

_FPGA_REPEATS = 5000  # -> 5000 * SHOTS shots per circuit
_CPU_SHOTS = 2_000_000


def cpu_stim_shots_per_sec(circuit: stim.Circuit, shots: int) -> float:
    sampler = circuit.compile_detector_sampler()
    t0 = time.perf_counter()
    sampler.sample(shots=shots, bit_packed=True)
    dt = time.perf_counter() - t0
    return shots / dt


def fpga_shots_per_sec(name: str) -> float:
    instructions_path = Path(f"/tmp/bench_{name}_instructions.bin")
    layer_offsets_path = Path(f"/tmp/bench_{name}_layer_offsets.bin")
    program = encode_circuit(_CIRCUITS[name]())
    instructions_path.write_bytes(program.serialize())
    layer_offsets_path.write_bytes(program.serialize_layer_offsets())

    result = subprocess.run(
        [str(_XRT_BENCH), str(_XCLBIN), str(instructions_path), str(layer_offsets_path), str(_FPGA_REPEATS)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def main() -> None:
    if not _XRT_BENCH.exists():
        raise SystemExit(f"{_XRT_BENCH} not built -- see build/README.md")

    print(f"{'circuit':<20} {'FPGA shots/s':>15} {'CPU Stim shots/s':>18} {'CPU/FPGA':>10}")
    for name in _CIRCUITS:
        fpga_rate = fpga_shots_per_sec(name)
        cpu_rate = cpu_stim_shots_per_sec(_CIRCUITS[name](), _CPU_SHOTS)
        print(f"{name:<20} {fpga_rate:>15,.0f} {cpu_rate:>18,.0f} {cpu_rate / fpga_rate:>9.1f}x")


if __name__ == "__main__":
    main()
