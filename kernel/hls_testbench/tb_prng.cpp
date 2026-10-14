// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// C-sim testbench for prng.hpp. Checks determinism, sensitivity to each
// input, and pins down a regression value cross-checked against
// softmodel/philox.py at the time this was written -- see that module's
// docstring for why Python and C++ have to agree bit-for-bit.
#include "../prng.hpp"
#include <cassert>
#include <cstdio>

using namespace stim_u55c;

int main() {
    int failures = 0;

    // Regression value: cross-checked equal to
    // softmodel.philox.philox4x32_10 for the same counter/key at the
    // time this test was written.
    auto r = philox4x32_10(0, 0, 0, 0, 12345, 6789);
    if (!(r.w0 == 2868901202u && r.w1 == 1491490592u && r.w2 == 3447344351u && r.w3 == 856391381u)) {
        std::printf("FAIL: regression value mismatch: got %u %u %u %u\n", r.w0, r.w1, r.w2, r.w3);
        failures++;
    }

    auto r2 = philox4x32_10(0, 0, 0, 0, 12345, 6789);
    if (!(r.w0 == r2.w0 && r.w1 == r2.w1 && r.w2 == r2.w2 && r.w3 == r2.w3)) {
        std::printf("FAIL: not deterministic for identical inputs\n");
        failures++;
    }

    // Every input word should perturb the output (a cheap, not
    // exhaustive, avalanche sanity check).
    struct Variant {
        uint32_t c0, c1, c2, c3, k0, k1;
        const char *label;
    };
    Variant variants[] = {
        {1, 0, 0, 0, 12345, 6789, "c0"}, {0, 1, 0, 0, 12345, 6789, "c1"},
        {0, 0, 1, 0, 12345, 6789, "c2"}, {0, 0, 0, 1, 12345, 6789, "c3"},
        {0, 0, 0, 0, 12346, 6789, "k0"}, {0, 0, 0, 0, 12345, 6790, "k1"},
    };
    for (const auto &v : variants) {
        auto rv = philox4x32_10(v.c0, v.c1, v.c2, v.c3, v.k0, v.k1);
        if (rv.w0 == r.w0 && rv.w1 == r.w1 && rv.w2 == r.w2 && rv.w3 == r.w3) {
            std::printf("FAIL: perturbing %s did not change the output\n", v.label);
            failures++;
        }
    }

    if (failures == 0) {
        std::printf("tb_prng: PASS\n");
        return 0;
    }
    std::printf("tb_prng: FAIL (%d failure(s))\n", failures);
    return 1;
}
