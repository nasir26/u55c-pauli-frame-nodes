#!/usr/bin/env python3
"""Tier 3 (single-fault injection vs. the DEM) run on real hardware --
the software form of this has passed since Phase 1
(tests/test_softmodel_validation.py); this exercises the same 458 DEM
error mechanisms across the same three circuits, but through the actual
synthesized kernel on the physical Alveo U55C via host/xrt_runner.cpp,
not the soft model.

Needs a built, timing-closed build/hw/ with xrt_runner (see
build/README.md) and a real device.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import stim

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "kernel"))
sys.path.insert(0, str(_REPO_ROOT / "python"))
from isa import encode_circuit  # noqa: E402
from softmodel.reference_sampler import build_single_fault_circuit  # noqa: E402
from stim_u55c.config import NUM_DETECTORS_MAX, NUM_OBSERVABLES_MAX  # noqa: E402

_XCLBIN = _REPO_ROOT / "build" / "hw" / "stim_frame_sampler.xclbin"
_XRT_RUNNER = _REPO_ROOT / "build" / "hw" / "xrt_runner"

_CIRCUITS = {
    "repetition_code_d3": stim.Circuit.generated(
        "repetition_code:memory", rounds=3, distance=3,
        before_round_data_depolarization=0.05, before_measure_flip_probability=0.05,
    ),
    "surface_code_z_d3": stim.Circuit.generated(
        "surface_code:rotated_memory_z", rounds=3, distance=3,
        before_round_data_depolarization=0.02, before_measure_flip_probability=0.02,
        after_clifford_depolarization=0.02, after_reset_flip_probability=0.02,
    ),
    "surface_code_x_d3": stim.Circuit.generated(
        "surface_code:rotated_memory_x", rounds=3, distance=3,
        before_round_data_depolarization=0.02, before_measure_flip_probability=0.02,
        after_clifford_depolarization=0.02, after_reset_flip_probability=0.02,
    ),
}


def run_one_fault(tmpdir: Path, circuit: stim.Circuit, path, qubit_paulis) -> tuple[set[int], set[int]]:
    fault_circuit = build_single_fault_circuit(circuit, path=path, qubit_paulis=qubit_paulis)
    program = encode_circuit(fault_circuit)

    instr_path = tmpdir / "instructions.bin"
    layers_path = tmpdir / "layer_offsets.bin"
    out_path = tmpdir / "output.bin"
    instr_path.write_bytes(program.serialize())
    layers_path.write_bytes(program.serialize_layer_offsets())

    subprocess.run(
        [str(_XRT_RUNNER), str(_XCLBIN), str(instr_path), str(layers_path), "1", "0", str(out_path)],
        check=True, capture_output=True,
    )
    raw = out_path.read_bytes()
    words = struct.unpack(f"<{NUM_DETECTORS_MAX + NUM_OBSERVABLES_MAX}Q", raw)
    detector_words = words[:NUM_DETECTORS_MAX]
    observable_words = words[NUM_DETECTORS_MAX:]
    # shot 0 is bit 0 of each word (single-shot run: instructions.bin encodes shots=1
    # semantically, but the kernel always fills SHOTS lanes -- only lane 0 is meaningful here).
    fired_detectors = {i for i, w in enumerate(detector_words) if w & 1}
    fired_observables = {i for i, w in enumerate(observable_words) if w & 1}
    return fired_detectors, fired_observables


def main() -> None:
    if not _XRT_RUNNER.exists():
        raise SystemExit(f"{_XRT_RUNNER} not built -- see build/README.md")

    total = 0
    total_mismatches = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for name, circuit in _CIRCUITS.items():
            dem = circuit.detector_error_model(decompose_errors=True)
            explained = circuit.explain_detector_error_model_errors(
                dem_filter=dem, reduce_to_one_representative_error=True
            )
            mismatches = 0
            for e in explained:
                loc = e.circuit_error_locations[0]
                path = tuple((f.instruction_offset, f.iteration_index) for f in loc.stack_frames)
                qp = tuple((g.gate_target.value, g.gate_target.pauli_type) for g in loc.flipped_pauli_product)

                fired_det, fired_obs = run_one_fault(tmpdir, circuit, path, qp)
                expected_det = {t.dem_target.val for t in e.dem_error_terms if t.dem_target.is_relative_detector_id()}
                expected_obs = {
                    t.dem_target.val for t in e.dem_error_terms if t.dem_target.is_logical_observable_id()
                }
                if fired_det != expected_det or fired_obs != expected_obs:
                    mismatches += 1
                    print(f"  MISMATCH {name}: fault {qp} -> got det={fired_det} obs={fired_obs}, "
                          f"expected det={expected_det} obs={expected_obs}")

            print(f"{name}: {len(explained) - mismatches}/{len(explained)} DEM mechanisms PASS on real hardware")
            total += len(explained)
            total_mismatches += mismatches

    print(f"\nTotal: {total - total_mismatches}/{total} PASS")
    if total_mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
