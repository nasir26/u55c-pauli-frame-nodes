"""Executes a compiled kernel.isa.Program the same way the HLS kernel does.

This is the Tier 2 oracle specifically (see top-level README.md
"Validation strategy" and kernel/isa.py's module docstring): unlike
softmodel/reference_sampler.py (which interprets a stim.Circuit directly,
and is the Tier 1/3/4 oracle), this module interprets the *compiled
instruction stream* -- the same bytes the kernel testbench reads -- using
the same Philox4x32-10 protocol the kernel uses (see kernel/prng.hpp).
Bit-exactness between this module and the kernel is Tier 2.

Frame state here is still a plain (num_qubits, shots) boolean array rather
than the kernel's SHOTS-wide ap_uint packing -- that's a representation
difference only. What has to match bit-for-bit is the *values*: the same
PRNG draws, the same threshold, the same modulo split for categorical
noise.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from isa import DEPOLARIZE2_COMBOS, NO_QUBIT, Opcode, Program
from softmodel.philox import philox4x32_10
from stim_u55c.config import NUM_DETECTOR_BYTES, NUM_OBSERVABLE_BYTES, NUM_QUBITS_MAX

_PAULI_HAS_X = {"I": False, "X": True, "Y": True, "Z": False}
_PAULI_HAS_Z = {"I": False, "X": False, "Y": True, "Z": True}
_DEPOLARIZE2_A_X = np.array([_PAULI_HAS_X[a] for a, _ in DEPOLARIZE2_COMBOS])
_DEPOLARIZE2_A_Z = np.array([_PAULI_HAS_Z[a] for a, _ in DEPOLARIZE2_COMBOS])
_DEPOLARIZE2_B_X = np.array([_PAULI_HAS_X[b] for _, b in DEPOLARIZE2_COMBOS])
_DEPOLARIZE2_B_Z = np.array([_PAULI_HAS_Z[b] for _, b in DEPOLARIZE2_COMBOS])

_MEASURE_BASIS = {
    Opcode.MEASURE_Z: "Z", Opcode.MEASURE_X: "X", Opcode.MEASURE_Y: "Y",
    Opcode.MEASURE_RESET_Z: "Z", Opcode.MEASURE_RESET_X: "X", Opcode.MEASURE_RESET_Y: "Y",
}
_IS_MEASURE_RESET = {Opcode.MEASURE_RESET_Z, Opcode.MEASURE_RESET_X, Opcode.MEASURE_RESET_Y}
_RESET_BASIS = {Opcode.RESET_Z, Opcode.RESET_X, Opcode.RESET_Y}


def _draw(instr_index: int, shots: int, key: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    counter = np.zeros((4, shots), dtype=np.uint32)
    counter[0] = np.uint32(instr_index)
    counter[1] = np.arange(shots, dtype=np.uint32)
    out = philox4x32_10(counter, key)
    return out[0], out[1]


@dataclass
class KernelSampleResult:
    detectors: np.ndarray  # shape (shots, num_detectors), bool
    observables: np.ndarray  # shape (shots, num_observables), bool


def run_program(
    program: Program,
    *,
    shots: int,
    seed: int,
    draw=None,
) -> KernelSampleResult:
    """Replay `program` for `shots` shots, matching kernel semantics exactly.

    No reference/noiseless measurement sample is needed here (or in the
    kernel itself): a detector/observable's fold value is `actual XOR
    reference`, i.e. `effect`, the frame's own deviation -- the reference
    term cancels out of that XOR algebraically regardless of what it is
    (see softmodel/reference_sampler.py's rec_actual_xor_reference
    docstring). softmodel.reference_sampler.py still needs its own
    reference sample for Tiers 1/3/4, which compare against Stim's actual
    conventions; this module only has to match the kernel, which never
    computes or exposes a raw measurement value at all.
    """
    key = np.array([seed & 0xFFFFFFFF, (seed >> 32) & 0xFFFFFFFF], dtype=np.uint32)
    # `draw(instruction_index) -> (first word, second word)` per shot; the default is this kernel's Philox protocol.
    # softmodel/batch_replay.py passes the batch kernel's protocol; everything else is shared.
    if draw is None:
        def draw(instr_index):
            return _draw(instr_index, shots, key)

    x = np.zeros((NUM_QUBITS_MAX, shots), dtype=bool)
    z = np.zeros((NUM_QUBITS_MAX, shots), dtype=bool)
    detector_bits = {}  # detector_id -> (shots,) bool accumulator
    observable_bits = {}

    for instr in program.instructions:
        op = instr.opcode
        a, b = instr.qubit_a, instr.qubit_b

        if op == Opcode.NOP or op == Opcode.END_OF_PROGRAM:
            continue
        elif op == Opcode.GATE_H:
            x[a], z[a] = z[a].copy(), x[a].copy()
        elif op == Opcode.GATE_S:
            z[a] ^= x[a]
        elif op == Opcode.GATE_CX:
            x[b] ^= x[a]
            z[a] ^= z[b]
        elif op == Opcode.GATE_CZ:
            xa, xb = x[a].copy(), x[b].copy()
            z[b] ^= xa
            z[a] ^= xb
        elif op == Opcode.GATE_SWAP:
            x[a], x[b] = x[b].copy(), x[a].copy()
            z[a], z[b] = z[b].copy(), z[a].copy()
        elif op in _RESET_BASIS:
            x[a][:] = False
            z[a][:] = False
        elif op in _MEASURE_BASIS:
            basis = _MEASURE_BASIS[op]
            # .copy() matters: MEASURE_RESET zeroes x[a]/z[a] in place a
            # few lines down, and a bare `x[a]` here would be a view into
            # that same row, not a snapshot -- the reset would silently
            # clobber `effect` before the detector fold below reads it.
            if basis == "Z":
                effect = x[a].copy()
            elif basis == "X":
                effect = z[a].copy()
            else:
                effect = x[a] ^ z[a]
            if op in _IS_MEASURE_RESET:
                x[a][:] = False
                z[a][:] = False

            # Detector/observable fold, resolved at compile time into this
            # instruction's masks -- no runtime lookback needed. The fold
            # value is `actual XOR reference`, i.e. `effect`: see
            # softmodel/reference_sampler.py's rec_actual_xor_reference
            # docstring for why this is the right quantity (it's zero
            # whenever nothing anomalous happened, regardless of the
            # reference's own value).
            if instr.detector_mask:
                for d in range(instr.detector_mask.bit_length()):
                    if instr.detector_mask & (1 << d):
                        acc = detector_bits.setdefault(d, np.zeros(shots, dtype=bool))
                        acc ^= effect
            if instr.observable_mask:
                for o in range(instr.observable_mask.bit_length()):
                    if instr.observable_mask & (1 << o):
                        acc = observable_bits.setdefault(o, np.zeros(shots, dtype=bool))
                        acc ^= effect
        elif op == Opcode.NOISE_X or op == Opcode.NOISE_Y or op == Opcode.NOISE_Z:
            w0, _w1 = draw(instr.index)
            fires = w0 < np.uint32(instr.prob_threshold)
            if op != Opcode.NOISE_Z:
                x[a] ^= fires
            if op != Opcode.NOISE_X:
                z[a] ^= fires
        elif op == Opcode.NOISE_DEPOLARIZE1:
            w0, w1 = draw(instr.index)
            active = w0 < np.uint32(instr.prob_threshold)
            which = w1 % np.uint32(3)
            x[a] ^= active & (which == 0)  # X
            x[a] ^= active & (which == 1)  # Y
            z[a] ^= active & (which == 1)
            z[a] ^= active & (which == 2)  # Z
        elif op == Opcode.NOISE_DEPOLARIZE2:
            w0, w1 = draw(instr.index)
            active = w0 < np.uint32(instr.prob_threshold)
            combo = w1 % np.uint32(15)
            x[a] ^= active & _DEPOLARIZE2_A_X[combo]
            z[a] ^= active & _DEPOLARIZE2_A_Z[combo]
            x[b] ^= active & _DEPOLARIZE2_B_X[combo]
            z[b] ^= active & _DEPOLARIZE2_B_Z[combo]
        else:
            raise NotImplementedError(f"kernel_replay does not handle opcode {op!r}")

    num_det = (max(detector_bits) + 1) if detector_bits else 0
    num_obs = (max(observable_bits) + 1) if observable_bits else 0
    det_cols = [detector_bits.get(i, np.zeros(shots, dtype=bool)) for i in range(num_det)]
    obs_cols = [observable_bits.get(i, np.zeros(shots, dtype=bool)) for i in range(num_obs)]
    return KernelSampleResult(
        detectors=np.stack(det_cols, axis=1) if det_cols else np.zeros((shots, 0), dtype=bool),
        observables=np.stack(obs_cols, axis=1) if obs_cols else np.zeros((shots, 0), dtype=bool),
    )
