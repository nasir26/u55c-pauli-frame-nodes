"""softmodel/threefry.py against the Random123 known answer and against the ThreeFry generator of randomgen.

Author: Nasir Ali, C-DAC Noida.
"""

from __future__ import annotations

import numpy as np
import pytest

from softmodel.threefry import threefry4x32_20


def test_known_answer_zero():
    """Random123 known answer test vector of threefry4x32_20 for counter 0 and key 0."""
    out = threefry4x32_20(np.zeros((4, 1), np.uint32), np.zeros(4, np.uint32))[:, 0]
    assert [int(v) for v in out] == [0x9C6CA96A, 0xE17EAE66, 0xFC10ECD4, 0x5256A7D8]


def test_against_randomgen():
    randomgen = pytest.importorskip("randomgen")
    rng = np.random.default_rng(2026)

    def to_int(words):
        return sum(int(v) << (32 * i) for i, v in enumerate(words))

    for _ in range(100):
        key = rng.integers(0, 2**32, 4, dtype=np.uint64).astype(np.uint32)
        ctr = rng.integers(0, 2**32, 4, dtype=np.uint64).astype(np.uint32)
        g = randomgen.ThreeFry(number=4, width=32, key=to_int(key), counter=to_int(ctr))
        want = [int(v) for v in g.random_raw(4)]
        nxt = (to_int(ctr) + 1) % 2**128  # randomgen advances the counter before producing a block
        nxt_words = np.array([(nxt >> (32 * i)) & 0xFFFFFFFF for i in range(4)], np.uint32).reshape(4, 1)
        assert [int(v) for v in threefry4x32_20(nxt_words, key)[:, 0]] == want
