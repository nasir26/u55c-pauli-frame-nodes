"""Philox4x32-10, the counter-based PRNG the kernel uses (see kernel/prng.hpp
for the bit-identical C++ implementation this must match for Tier 2).

Counter-based per the project brief: seedable and reproducible per
(instruction_index, shot_index) with no shared mutable state between
lanes, which is what makes it possible to compute S independent random
values per instruction per cycle in hardware, and to reproduce any single
lane's draw in isolation here in Python for validation.

Algorithm and constants are Salmon, Moraes, Pangali, Shaw (2011),
"Parallel Random Numbers: As Easy as 1, 2, 3" -- the standard Random123
Philox4x32-10. `key` is derived from the run seed; `counter` is
[instruction_index, shot_index, 0, 0] (see softmodel/kernel_replay.py and
kernel/prng.hpp for how the two 32-bit words are packed).
"""

from __future__ import annotations

import numpy as np

_MUL0 = np.uint64(0xD2511F53)
_MUL1 = np.uint64(0xCD9E8D57)
_BUMP0 = np.uint32(0x9E3779B9)
_BUMP1 = np.uint32(0xBB67AE85)
_MASK32 = np.uint64(0xFFFFFFFF)


def philox4x32_10(counter: np.ndarray, key: np.ndarray) -> np.ndarray:
    """Vectorized across shots.

    counter: shape (4, shots), uint32 -- (c0, c1, c2, c3) per shot.
    key: shape (2,) or (2, shots), uint32 -- (k0, k1).
    Returns shape (4, shots), uint32.
    """
    c0, c1, c2, c3 = (counter[i].astype(np.uint64) for i in range(4))
    if key.ndim == 1:
        k0 = np.full(counter.shape[1], key[0], dtype=np.uint64)
        k1 = np.full(counter.shape[1], key[1], dtype=np.uint64)
    else:
        k0, k1 = key[0].astype(np.uint64), key[1].astype(np.uint64)

    for _ in range(10):
        p0 = c0 * _MUL0
        hi0, lo0 = (p0 >> np.uint64(32)) & _MASK32, p0 & _MASK32
        p1 = c2 * _MUL1
        hi1, lo1 = (p1 >> np.uint64(32)) & _MASK32, p1 & _MASK32

        c0, c1, c2, c3 = (hi1 ^ c1 ^ k0) & _MASK32, lo1, (hi0 ^ c3 ^ k1) & _MASK32, lo0
        k0 = (k0 + _BUMP0) & _MASK32
        k1 = (k1 + _BUMP1) & _MASK32

    return np.stack([c0, c1, c2, c3]).astype(np.uint32)
