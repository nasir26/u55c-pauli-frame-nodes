#!/usr/bin/env python3
"""Tier 5 (logical error rate vs. Stim+PyMatching) run on real hardware,
for surface code d=3 and d=5 -- the two distances the current kernel's
NUM_QUBITS_MAX=128 covers without a rebuild (d=7 needs more qubits; see
README.md's Phase 4 entry). Not a threshold estimate (that needs more
distances than these two to see the characteristic curve crossing) --
what this checks is the thing Tier 5 is actually for: does decoding
FPGA-generated syndromes give the same logical error rate, within
Monte Carlo error bars, as decoding CPU-Stim-generated syndromes for the
same circuit. Both sides use the identical PyMatching decoder built from
the identical DEM, so a real disagreement here would mean the FPGA's
syndromes are wrong in a way Tiers 1-4 didn't happen to catch -- not a
decoder difference.

Needs a built, timing-closed build/hw/ (see build/README.md) and a real
device. host/xrt_tier5.cpp does the sampling + on-host b8 transpose
(kernel output is detector-major, PyMatching wants shot-major -- see
that file's header comment); this script does the decoding and stats.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pymatching
import stim

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "kernel"))
sys.path.insert(0, str(_REPO_ROOT / "python"))
from isa import encode_circuit  # noqa: E402
from stim_u55c.config import NUM_DETECTOR_BYTES, NUM_OBSERVABLE_BYTES, SHOTS  # noqa: E402

_XCLBIN = _REPO_ROOT / "build" / "hw" / "stim_frame_sampler.xclbin"
_XRT_TIER5 = _REPO_ROOT / "build" / "hw" / "xrt_tier5"

_SHOTS_PER_POINT = 1_000_000
_P_VALUES = [0.001, 0.003, 0.01]
_DISTANCES = [3, 5]
_SIGMA_THRESHOLD = 5.0


def _surface_code(distance: int, p: float) -> stim.Circuit:
    return stim.Circuit.generated(
        "surface_code:rotated_memory_z", rounds=distance, distance=distance,
        before_round_data_depolarization=p, before_measure_flip_probability=p,
        after_clifford_depolarization=p, after_reset_flip_probability=p,
    )


def _two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> float:
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    return 0.0 if se == 0 else (p1 - p2) / se


def fpga_logical_error_rate(circuit: stim.Circuit, matcher: pymatching.Matching, num_detectors: int) -> tuple[int, int]:
    program = encode_circuit(circuit)
    instr_path = Path("/tmp/hw_tier5_instructions.bin")
    layers_path = Path("/tmp/hw_tier5_layer_offsets.bin")
    instr_path.write_bytes(program.serialize())
    layers_path.write_bytes(program.serialize_layer_offsets())

    repeat_count = _SHOTS_PER_POINT // SHOTS
    out_prefix = "/tmp/hw_tier5_out"
    subprocess.run(
        [str(_XRT_TIER5), str(_XCLBIN), str(instr_path), str(layers_path), str(repeat_count), out_prefix],
        check=True, capture_output=True,
    )
    total_shots = repeat_count * SHOTS

    det_bytes = np.fromfile(f"{out_prefix}_detectors.b8", dtype=np.uint8).reshape(total_shots, NUM_DETECTOR_BYTES)
    obs_bytes = np.fromfile(f"{out_prefix}_observables.b8", dtype=np.uint8).reshape(total_shots, NUM_OBSERVABLE_BYTES)
    det_bytes = np.ascontiguousarray(det_bytes[:, : (num_detectors + 7) // 8])

    predicted = matcher.decode_batch(det_bytes, bit_packed_shots=True, bit_packed_predictions=True)
    actual = np.unpackbits(obs_bytes, axis=1, bitorder="little")[:, :1]
    predicted_bits = np.unpackbits(predicted, axis=1, bitorder="little")[:, :1]
    logical_errors = int(np.count_nonzero(np.any(predicted_bits != actual, axis=1)))
    return logical_errors, total_shots


def cpu_logical_error_rate(circuit: stim.Circuit, matcher: pymatching.Matching, shots: int) -> tuple[int, int]:
    sampler = circuit.compile_detector_sampler(seed=67890)
    det, obs = sampler.sample(shots=shots, bit_packed=True, separate_observables=True)
    predicted = matcher.decode_batch(det, bit_packed_shots=True, bit_packed_predictions=True)
    predicted_bits = np.unpackbits(predicted, axis=1, bitorder="little")[:, :1]
    actual_bits = np.unpackbits(obs, axis=1, bitorder="little")[:, :1]
    logical_errors = int(np.count_nonzero(np.any(predicted_bits != actual_bits, axis=1)))
    return logical_errors, shots


def main() -> None:
    if not _XRT_TIER5.exists():
        raise SystemExit(f"{_XRT_TIER5} not built -- see build/README.md")

    print(f"{'d':>3} {'p':>7} {'FPGA LER':>12} {'CPU LER':>12} {'z':>6}  status")
    any_flagged = False
    for d in _DISTANCES:
        for p in _P_VALUES:
            circuit = _surface_code(d, p)
            dem = circuit.detector_error_model(decompose_errors=True)
            matcher = pymatching.Matching.from_detector_error_model(dem)
            program = encode_circuit(circuit)

            fpga_errors, fpga_shots = fpga_logical_error_rate(circuit, matcher, program.num_detectors)
            cpu_errors, cpu_shots = cpu_logical_error_rate(circuit, matcher, _SHOTS_PER_POINT)

            z = _two_proportion_z(fpga_errors, fpga_shots, cpu_errors, cpu_shots)
            flagged = abs(z) > _SIGMA_THRESHOLD
            any_flagged = any_flagged or flagged
            status = "FLAGGED" if flagged else "ok"
            print(f"{d:>3} {p:>7.4f} {fpga_errors / fpga_shots:>12.5f} {cpu_errors / cpu_shots:>12.5f} "
                  f"{z:>6.2f}  {status}")

    if any_flagged:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
