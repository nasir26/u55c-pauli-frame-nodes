// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Philox4x32-10, the counter-based PRNG bank. Must stay bit-identical to
// softmodel/philox.py -- that's what makes Tier 2 (bit-exact vs. the soft
// model) meaningful. Algorithm and constants: Salmon, Moraes, Pangali,
// Shaw (2011), "Parallel Random Numbers: As Easy as 1, 2, 3"
// (Philox4x32-10).
//
// Counter-based: seedable and reproducible per (instruction_index,
// shot_index), with no state shared between shot lanes -- see
// frame_store.hpp / stim_frame_sampler.cpp for how a full SHOTS-wide bank
// of these is evaluated (conceptually) in parallel, one call per lane,
// per noise instruction.
#pragma once

#include <cstdint>

namespace stim_u55c {

struct Philox4x32Result {
    uint32_t w0, w1, w2, w3;
};

inline void mulhilo32(uint32_t a, uint32_t b, uint32_t &hi, uint32_t &lo) {
    uint64_t product = static_cast<uint64_t>(a) * static_cast<uint64_t>(b);
    hi = static_cast<uint32_t>(product >> 32);
    lo = static_cast<uint32_t>(product);
}

// counter = (instruction_index, shot_index, 0, 0); key = (seed_lo, seed_hi).
inline Philox4x32Result philox4x32_10(uint32_t c0, uint32_t c1, uint32_t c2, uint32_t c3,
                                       uint32_t k0, uint32_t k1) {
    static constexpr uint32_t MUL0 = 0xD2511F53u;
    static constexpr uint32_t MUL1 = 0xCD9E8D57u;
    static constexpr uint32_t BUMP0 = 0x9E3779B9u;
    static constexpr uint32_t BUMP1 = 0xBB67AE85u;

PHILOX_ROUNDS:
    for (int round = 0; round < 10; round++) {
#pragma HLS unroll
        uint32_t hi0, lo0, hi1, lo1;
        mulhilo32(MUL0, c0, hi0, lo0);
        mulhilo32(MUL1, c2, hi1, lo1);

        uint32_t new_c0 = hi1 ^ c1 ^ k0;
        uint32_t new_c1 = lo1;
        uint32_t new_c2 = hi0 ^ c3 ^ k1;
        uint32_t new_c3 = lo0;
        c0 = new_c0;
        c1 = new_c1;
        c2 = new_c2;
        c3 = new_c3;

        k0 = k0 + BUMP0;
        k1 = k1 + BUMP1;
    }

    Philox4x32Result result;
    result.w0 = c0;
    result.w1 = c1;
    result.w2 = c2;
    result.w3 = c3;
    return result;
}

}  // namespace stim_u55c
