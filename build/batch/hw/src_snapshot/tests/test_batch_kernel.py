"""Batch kernel (kernel/batch/stim_batch_sampler.cpp): C simulation bit exact against softmodel/batch_replay.py,
and the batch model itself against the first kernel's model and against Stim.

Author: Nasir Ali, C-DAC Noida.

The C simulation needs the Vitis HLS headers (ap_int.h, hls_stream.h); it is skipped where they are not installed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
import stim

from isa_batch import CFG, LANES, compile_batch
from softmodel.batch_replay import run_batches

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BATCH_DIR = _REPO_ROOT / "kernel" / "batch"
_HLS_INCLUDE = Path(os.environ.get("XILINX_HLS", "/tools/Xilinx/Vitis_HLS/2023.2")) / "include"

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
    "surface_code_x_d3": lambda: stim.Circuit.generated(
        "surface_code:rotated_memory_x", rounds=3, distance=3,
        before_round_data_depolarization=0.03, before_measure_flip_probability=0.03,
        after_clifford_depolarization=0.03, after_reset_flip_probability=0.03,
    ),
}


def test_config_matches_python():
    assert LANES % 64 == 0 and CFG["PAIRS"] * 2 == LANES


@pytest.fixture(scope="module")
def tb_binary(tmp_path_factory) -> Path:
    if not (_HLS_INCLUDE / "ap_int.h").exists():
        pytest.skip("Vitis HLS headers not installed")
    binary = tmp_path_factory.mktemp("batch_build") / "tb_stim_batch_sampler"
    subprocess.run(
        ["g++", "-std=c++17", "-O2", "-Wno-unknown-pragmas", "-Wno-unused-label", f"-I{_HLS_INCLUDE}",
         str(_BATCH_DIR / "stim_batch_sampler.cpp"), str(_BATCH_DIR / "testbench" / "tb_stim_batch_sampler.cpp"),
         "-o", str(binary)],
        check=True, capture_output=True,
    )
    return binary


def run_csim(binary: Path, bp, num_batches: int, batch_base: int, seed_lo: int, seed_hi: int, d: Path) -> np.ndarray:
    (d / "instructions.bin").write_bytes(bp.instructions.astype("<u8").tobytes())
    (d / "fold.bin").write_bytes(bp.fold.astype("<u4").tobytes())
    subprocess.run(
        [str(binary), str(d / "instructions.bin"), str(d / "fold.bin"), str(bp.num_meas), str(bp.num_out),
         str(num_batches), str(batch_base), str(seed_lo), str(seed_hi), str(d / "output.bin")],
        check=True, capture_output=True,
    )
    raw = np.frombuffer((d / "output.bin").read_bytes(), dtype="<u8")
    return raw.reshape(num_batches, bp.num_out, LANES // 64)


@pytest.mark.parametrize("name", list(_CIRCUITS))
def test_csim_bit_exact(tb_binary, tmp_path, name):
    bp = compile_batch(_CIRCUITS[name]())
    for num_batches, batch_base, seed_lo, seed_hi in [(3, 0, 305419896, 2271560481), (2, 4000000000, 7, 1)]:
        got = run_csim(tb_binary, bp, num_batches, batch_base, seed_lo, seed_hi, tmp_path)
        _, _, want = run_batches(bp, num_batches=num_batches, batch_base=batch_base, seed_lo=seed_lo, seed_hi=seed_hi)
        assert np.array_equal(got, want), f"{name}: batch kernel C simulation differs from softmodel/batch_replay.py"
        assert got.any(), "all outputs zero: the noise path is not exercised"


@pytest.mark.parametrize("name", list(_CIRCUITS))
def test_noiseless_all_zero(tb_binary, tmp_path, name):
    bp = compile_batch(_CIRCUITS[name]().without_noise())
    got = run_csim(tb_binary, bp, 2, 0, 1, 2, tmp_path)
    assert not got.any()


@pytest.mark.parametrize("name", ["repetition_code_d3", "surface_code_d3"])
def test_rates_against_stim(name):
    """Per output firing rates of the batch model against Stim over 2**16 shots (5 sigma, independent binomials)."""
    c = _CIRCUITS[name]()
    bp = compile_batch(c)
    nb = (1 << 16) // LANES
    det, obs, _ = run_batches(bp, num_batches=nb, batch_base=11, seed_lo=99, seed_hi=3)
    ref_det, ref_obs = c.compile_detector_sampler(seed=5).sample(nb * LANES, separate_observables=True)
    for ours, ref in ((det, ref_det), (obs, ref_obs)):
        p1, p2 = ours.mean(axis=0), ref.mean(axis=0)
        pooled = (p1 + p2) / 2
        se = np.sqrt(np.maximum(pooled * (1 - pooled), 1e-12) * 2 / len(ours))
        assert np.max(np.abs(p1 - p2) / se) < 5.0


@pytest.mark.parametrize("name", list(_CIRCUITS))
def test_schedule_hazard_distance(name):
    """Two instructions on one qubit are HAZARD_DISTANCE apart; the program starts and ends with that many NOPs; and
    every qubit sees its instructions in compiler order (the frame semantics rest on these three)."""
    from isa import NO_QUBIT, Opcode, encode_circuit

    dist = CFG["HAZARD_DISTANCE"]
    bp = compile_batch(_CIRCUITS[name]())
    ins = bp.program.instructions
    assert all(i.opcode == Opcode.NOP for i in ins[:dist] + ins[-dist:])
    last = {}
    for k, i in enumerate(ins):
        for q in (i.qubit_a, i.qubit_b):
            if q != NO_QUBIT:
                assert k - last.get(q, -dist) >= dist
                last[q] = k
    ref = [i for i in encode_circuit(_CIRCUITS[name](), max_qubits=NO_QUBIT, max_detectors=CFG["OUT_MAX"]).instructions
           if i.opcode not in (Opcode.END_OF_PROGRAM, Opcode.NOP)]
    got = [i for i in ins if i.opcode != Opcode.NOP]
    assert len(ref) == len(got)
    dense = {}
    for i in ref:
        for q in (i.qubit_a, i.qubit_b):
            if q != NO_QUBIT:
                dense.setdefault(q, len(dense))

    def per_qubit(seq, remap):
        d = {}
        for i in seq:
            for q in (i.qubit_a, i.qubit_b):
                if q != NO_QUBIT:
                    d.setdefault(remap(q), []).append((int(i.opcode), i.prob_threshold))
        return d

    assert per_qubit(ref, dense.__getitem__) == per_qubit(got, lambda q: q)


def test_csim_detects_hazards(tb_binary, tmp_path, monkeypatch):
    """Negative control: scheduled with distance 1 the C simulation must differ from the model, which shows that it
    runs the frame stage's delayed write back and that the bit exact tests above would catch a schedule violation."""
    import isa_batch

    # one qubit, every instruction depends on the one before it
    c = stim.Circuit("""
        R 0
        X_ERROR(0.4) 0
        H 0
        Z_ERROR(0.3) 0
        H 0
        M 0
        DETECTOR rec[-1]
        DEPOLARIZE1(0.5) 0
        MR 0
        DETECTOR rec[-1]
    """)
    bp_ok = isa_batch.compile_batch(c)
    got = run_csim(tb_binary, bp_ok, 2, 0, 1, 2, tmp_path)
    _, _, want = run_batches(bp_ok, num_batches=2, batch_base=0, seed_lo=1, seed_hi=2)
    assert np.array_equal(got, want)
    monkeypatch.setitem(isa_batch.CFG, "HAZARD_DISTANCE", 1)
    bp = isa_batch.compile_batch(c)
    got = run_csim(tb_binary, bp, 2, 0, 1, 2, tmp_path)
    _, _, want = run_batches(bp, num_batches=2, batch_base=0, seed_lo=1, seed_hi=2)
    assert not np.array_equal(got, want)
