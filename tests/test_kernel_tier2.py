"""Phase 2 gate: the HLS kernel's C-sim (sw_emu-equivalent) output is
bit-exact against the soft model, for a d=3 repetition code and a d=3 and
d=5 surface code.

This compiles kernel/stim_frame_sampler.cpp with plain g++ (see
kernel/ap_uint_shim.hpp's docstring for why that's possible without a
Vitis install), feeds it a compiled instruction stream
(kernel/isa.py:encode_circuit), and diffs its raw output against
softmodel/kernel_replay.py -- which is Tier 2's whole point: identical
instruction stream and PRNG seed must produce identical output, kernel
vs. soft model. See top-level README.md "Validation strategy".
"""

from __future__ import annotations

import struct
import subprocess
import zlib
from pathlib import Path

import numpy as np
import pytest
import stim

from isa import encode_circuit
from softmodel.kernel_replay import run_program
from stim_u55c.config import NUM_DETECTORS_MAX, NUM_OBSERVABLES_MAX, SHOTS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_KERNEL_DIR = _REPO_ROOT / "kernel"

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


@pytest.fixture(scope="module")
def kernel_binary(tmp_path_factory) -> Path:
    build_dir = tmp_path_factory.mktemp("kernel_build")
    binary = build_dir / "tb_stim_frame_sampler"
    subprocess.run(
        [
            "g++", "-std=c++17", "-O2",
            "-Wno-unknown-pragmas", "-Wno-unused-label",
            str(_KERNEL_DIR / "stim_frame_sampler.cpp"),
            str(_KERNEL_DIR / "hls_testbench" / "tb_stim_frame_sampler.cpp"),
            "-I", str(_KERNEL_DIR),
            "-o", str(binary),
        ],
        check=True,
    )
    return binary


def _run_kernel(
    binary: Path, instructions_path: Path, layer_offsets_path: Path, seed: int, tmp_path: Path
) -> tuple[np.ndarray, np.ndarray]:
    output_path = tmp_path / "output.bin"
    seed_lo = seed & 0xFFFFFFFF
    seed_hi = (seed >> 32) & 0xFFFFFFFF
    subprocess.run(
        [
            str(binary), str(instructions_path), str(layer_offsets_path),
            str(seed_lo), str(seed_hi), str(output_path),
        ],
        check=True,
        capture_output=True,
    )
    raw = output_path.read_bytes()
    expected_bytes = (NUM_DETECTORS_MAX + NUM_OBSERVABLES_MAX) * 8
    assert len(raw) == expected_bytes, f"kernel wrote {len(raw)} bytes, expected {expected_bytes}"
    words = struct.unpack(f"<{NUM_DETECTORS_MAX + NUM_OBSERVABLES_MAX}Q", raw)
    detector_words = np.array(words[:NUM_DETECTORS_MAX], dtype=np.uint64)
    observable_words = np.array(words[NUM_DETECTORS_MAX:], dtype=np.uint64)
    return detector_words, observable_words


def _pack_expected(bits: np.ndarray) -> np.ndarray:
    """(SHOTS, n) bool -> (n,) uint64, bit i of each word = shot i's value."""
    n = bits.shape[1]
    words = np.zeros(n, dtype=np.uint64)
    for shot in range(bits.shape[0]):
        words |= bits[shot].astype(np.uint64) << np.uint64(shot)
    return words


@pytest.mark.parametrize("name", _CIRCUITS)
def test_tier2_kernel_bit_exact_vs_softmodel(name, kernel_binary, tmp_path):
    circuit = _CIRCUITS[name]()
    program = encode_circuit(circuit)
    assert program.num_detectors > 0

    instructions_path = tmp_path / "instructions.bin"
    instructions_path.write_bytes(program.serialize())
    layer_offsets_path = tmp_path / "layer_offsets.bin"
    layer_offsets_path.write_bytes(program.serialize_layer_offsets())

    # zlib.crc32, not hash(): plain hash() on a str isn't reproducible
    # across processes (PYTHONHASHSEED), and while that wouldn't break
    # this test (the same seed is used for both sides within one run),
    # a fixed seed makes a failure reproducible when re-run standalone.
    seed = 0xC0FFEE ^ zlib.crc32(name.encode())

    expected = run_program(program, shots=SHOTS, seed=seed)
    expected_detector_words = _pack_expected(expected.detectors)
    expected_observable_words = _pack_expected(expected.observables)
    # Pad to the full fixed-size buffers the kernel always writes, so
    # unused accumulator slots are checked too (they must come back zero
    # on both sides).
    expected_detector_words = np.pad(expected_detector_words, (0, NUM_DETECTORS_MAX - len(expected_detector_words)))
    expected_observable_words = np.pad(
        expected_observable_words, (0, NUM_OBSERVABLES_MAX - len(expected_observable_words))
    )

    kernel_detector_words, kernel_observable_words = _run_kernel(
        kernel_binary, instructions_path, layer_offsets_path, seed, tmp_path
    )

    mismatched_detectors = np.nonzero(kernel_detector_words != expected_detector_words)[0]
    mismatched_observables = np.nonzero(kernel_observable_words != expected_observable_words)[0]

    assert len(mismatched_detectors) == 0, (
        f"{name}: kernel/soft-model detector mismatch at indices {mismatched_detectors[:10].tolist()}"
    )
    assert len(mismatched_observables) == 0, (
        f"{name}: kernel/soft-model observable mismatch at indices {mismatched_observables[:10].tolist()}"
    )
