"""Threefry4x32-20, the counter based generator of the batch kernel (kernel/batch/stim_batch_sampler.cpp).

Author: Nasir Ali, C-DAC Noida.

Algorithm and constants as in Salmon, Moraes, Dror, Shaw (2011), "Parallel Random Numbers: As Easy as 1, 2, 3",
and the Random123 library: 20 rounds of add, rotate and xor on four 32 bit words, the key injected every four rounds
with the parity word 0x1BD11BDA. Unlike Philox it needs no multiplier, which matters on the FPGA (see the kernel).
tests/test_threefry.py checks this function against the ThreeFry generator of the randomgen package.
"""

from __future__ import annotations

import numpy as np

ROTATIONS = [(10, 26), (11, 21), (13, 27), (23, 5), (6, 20), (17, 11), (25, 10), (18, 20)]
PARITY = 0x1BD11BDA
_M = np.uint32(0xFFFFFFFF)


def _rotl(x: np.ndarray, r: int) -> np.ndarray:
    return (x << np.uint32(r)) | (x >> np.uint32(32 - r))


def threefry4x32_20(counter: np.ndarray, key: np.ndarray) -> np.ndarray:
    """counter: shape (4, n) uint32; key: shape (4,) or (4, n) uint32. Returns shape (4, n) uint32."""
    n = counter.shape[1]
    k = [np.broadcast_to(np.asarray(key[i], dtype=np.uint32), (n,)).astype(np.uint32) for i in range(4)]
    ks = k + [np.uint32(PARITY) ^ k[0] ^ k[1] ^ k[2] ^ k[3]]
    with np.errstate(over="ignore"):
        x = [counter[i].astype(np.uint32) + ks[i] for i in range(4)]
        for r in range(20):
            ra, rb = ROTATIONS[r % 8]
            if r % 2 == 0:
                x[0] = x[0] + x[1]; x[1] = _rotl(x[1], ra) ^ x[0]
                x[2] = x[2] + x[3]; x[3] = _rotl(x[3], rb) ^ x[2]
            else:
                x[0] = x[0] + x[3]; x[3] = _rotl(x[3], ra) ^ x[0]
                x[2] = x[2] + x[1]; x[1] = _rotl(x[1], rb) ^ x[2]
            if r % 4 == 3:
                s = (r + 1) // 4
                x[0] = x[0] + ks[s % 5]
                x[1] = x[1] + ks[(s + 1) % 5]
                x[2] = x[2] + ks[(s + 2) % 5]
                x[3] = x[3] + ks[(s + 3) % 5] + np.uint32(s)
    return np.stack(x).astype(np.uint32)
