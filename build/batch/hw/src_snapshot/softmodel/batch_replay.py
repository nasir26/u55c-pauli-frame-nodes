"""Bit exact model of the batch kernel (kernel/batch/stim_batch_sampler.cpp).

Author: Nasir Ali, C-DAC Noida.

The frame semantics are those of softmodel/kernel_replay.py, reused as they
are; only the random stream differs. In the batch kernel lane l of batch g
(g counted from batch_base) at instruction i draws Philox4x32-10 with counter
(i, l // 2, g, 0) and key (seed_lo, seed_hi); even lanes use (w0, w1), odd
lanes (w2, w3). Shots are numbered batch after batch, lane within batch.

run_batches returns the kernel's raw output layout as well: for every batch,
num_out words of LANES bits (detectors, then observables).
"""

from __future__ import annotations

import numpy as np

from isa_batch import LANES, BatchProgram
from softmodel.kernel_replay import run_program
from softmodel.philox import philox4x32_10


def batch_draw(num_batches: int, batch_base: int, key: np.ndarray):
    pairs = LANES // 2

    def draw(instr_index: int):
        g = (batch_base + np.arange(num_batches, dtype=np.uint64)) & 0xFFFFFFFF
        counter = np.zeros((4, num_batches, pairs), dtype=np.uint32)
        counter[0] = np.uint32(instr_index)
        counter[1] = np.arange(pairs, dtype=np.uint32)[None, :]
        counter[2] = g.astype(np.uint32)[:, None]
        out = philox4x32_10(counter.reshape(4, -1), key).reshape(4, num_batches, pairs)
        first = np.empty((num_batches, LANES), dtype=np.uint32)
        second = np.empty((num_batches, LANES), dtype=np.uint32)
        first[:, 0::2], first[:, 1::2] = out[0], out[2]
        second[:, 0::2], second[:, 1::2] = out[1], out[3]
        return first.reshape(-1), second.reshape(-1)

    return draw


def run_batches(bp: BatchProgram, *, num_batches: int, batch_base: int, seed_lo: int, seed_hi: int):
    """Returns (detectors (shots, num_det) bool, observables (shots, num_obs) bool, raw (num_batches, num_out,
    LANES // 64) uint64 in the kernel's output layout)."""
    shots = num_batches * LANES
    key = np.array([seed_lo, seed_hi], dtype=np.uint32)
    res = run_program(bp.program, shots=shots, seed=(seed_hi << 32) | seed_lo,
                      draw=batch_draw(num_batches, batch_base, key))
    det = np.zeros((shots, bp.num_detectors), dtype=bool)
    obs = np.zeros((shots, bp.num_observables), dtype=bool)
    det[:, : res.detectors.shape[1]] = res.detectors
    obs[:, : res.observables.shape[1]] = res.observables
    cols = np.concatenate([det, obs], axis=1).reshape(num_batches, LANES, bp.num_out)  # batch, lane, output
    bits = cols.transpose(0, 2, 1).reshape(num_batches, bp.num_out, LANES // 64, 64).astype(np.uint64)
    raw = (bits << np.arange(64, dtype=np.uint64)).sum(axis=3, dtype=np.uint64)
    return det, obs, raw
