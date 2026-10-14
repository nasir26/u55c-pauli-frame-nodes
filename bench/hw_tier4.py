#!/usr/bin/env python3
"""Tier 4 (statistical equivalence with Stim, >= 10^7 shots) run on real
hardware. The software form has passed since Phase 1
(tests/test_softmodel_validation.py, soft model vs. CPU Stim); this
compares the real kernel on real silicon directly against CPU Stim,
using the same two-proportion z-test and 5-sigma bar. host/xrt_tier4.cpp
does the actual sampling (double-buffered, accumulating per-detector
fired counts rather than raw per-shot bits -- unwieldy at this shot
count) so this script only needs to run it and do the statistics.

Needs a built, timing-closed build/hw/ (see build/README.md) and a real
device. Shot counts here make each circuit take on the order of a
minute or a few (see bench/results/ for the per-circuit throughput this
scales from).
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import stim

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "kernel"))
sys.path.insert(0, str(_REPO_ROOT / "python"))
from isa import encode_circuit  # noqa: E402
from stim_u55c.config import SHOTS  # noqa: E402

_XCLBIN = _REPO_ROOT / "build" / "hw" / "stim_frame_sampler.xclbin"
_XRT_TIER4 = _REPO_ROOT / "build" / "hw" / "xrt_tier4"

_TARGET_SHOTS = 10_000_000
_SIGMA_THRESHOLD = 5.0

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


def _two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> float:
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    return 0.0 if se == 0 else (p1 - p2) / se


def run_hw_tier4(name: str, circuit: stim.Circuit) -> None:
    program = encode_circuit(circuit)
    instr_path = Path(f"/tmp/hw_tier4_{name}_instructions.bin")
    layers_path = Path(f"/tmp/hw_tier4_{name}_layer_offsets.bin")
    instr_path.write_bytes(program.serialize())
    layers_path.write_bytes(program.serialize_layer_offsets())

    repeat_count = _TARGET_SHOTS // SHOTS
    result = subprocess.run(
        [str(_XRT_TIER4), str(_XCLBIN), str(instr_path), str(layers_path), str(repeat_count)],
        check=True, capture_output=True, text=True,
    )
    detector_fired = {}
    total_shots = None
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts[0] == "D":
            detector_fired[int(parts[1])] = int(parts[2])
        elif parts[0] == "TOTAL":
            total_shots = int(parts[1])
    assert total_shots is not None

    theirs = circuit.compile_detector_sampler(seed=67890).sample(shots=total_shots)
    num_detectors = program.num_detectors
    k_theirs = theirs.sum(axis=0)

    worst_z = 0.0
    flagged = []
    for d in range(num_detectors):
        z = _two_proportion_z(detector_fired[d], total_shots, int(k_theirs[d]), total_shots)
        worst_z = max(worst_z, abs(z))
        if abs(z) > _SIGMA_THRESHOLD:
            flagged.append((d, z))

    status = "PASS" if not flagged else "FAIL"
    print(f"{name}: {num_detectors} detectors, {total_shots} shots, max|z|={worst_z:.2f} -- {status}")
    if flagged:
        print(f"  flagged: {flagged[:10]}")


def main() -> None:
    if not _XRT_TIER4.exists():
        raise SystemExit(f"{_XRT_TIER4} not built -- see build/README.md")
    for name in _CIRCUITS:
        run_hw_tier4(name, _CIRCUITS[name]())


if __name__ == "__main__":
    main()
