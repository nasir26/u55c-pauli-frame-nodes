// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Tier 2 end-to-end C-sim testbench: reads a compiled instruction stream
// (kernel/isa.py:Program.serialize()) from argv[1] and its layer offsets
// (Program.serialize_layer_offsets()) from argv[2], runs them through the
// real kernel top-level function, and writes the resulting
// detector/observable accumulators as raw little-endian uint64 words to
// argv[5] -- one word per detector (NUM_DETECTORS_MAX of them), then one
// per observable (NUM_OBSERVABLES_MAX). SHOTS <= 64 (see config.hpp) so
// each accumulator fits in one uint64 exactly.
//
// The Python side (tests/test_kernel_tier2.py) computes the same
// program's expected output via softmodel/kernel_replay.py and diffs
// this file against it byte-for-byte -- that comparison *is* Tier 2.
#include "../instruction.hpp"
#include "../stim_frame_sampler.hpp"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

using namespace stim_u55c;

int main(int argc, char **argv) {
    if (argc != 6) {
        std::fprintf(stderr, "usage: %s <instructions.bin> <layer_offsets.bin> <seed_lo> <seed_hi> <output.bin>\n",
                      argv[0]);
        return 2;
    }

    std::FILE *in = std::fopen(argv[1], "rb");
    if (!in) {
        std::fprintf(stderr, "could not open %s\n", argv[1]);
        return 2;
    }
    static constexpr int kMaxInstructions = 1 << 20;
    std::vector<Instruction> instructions(kMaxInstructions);
    int count = read_program(in, instructions.data(), kMaxInstructions);
    std::fclose(in);

    std::FILE *layers_f = std::fopen(argv[2], "rb");
    if (!layers_f) {
        std::fprintf(stderr, "could not open %s\n", argv[2]);
        return 2;
    }
    std::vector<uint32_t> layer_offsets(NUM_LAYERS_MAX + 1);
    int num_offsets = read_layer_offsets(layers_f, layer_offsets.data(), NUM_LAYERS_MAX);
    std::fclose(layers_f);
    int num_layers = num_offsets - 1;

    uint32_t seed_lo = static_cast<uint32_t>(std::strtoul(argv[3], nullptr, 10));
    uint32_t seed_hi = static_cast<uint32_t>(std::strtoul(argv[4], nullptr, 10));

    ap_uint<SHOTS> detector_out[NUM_DETECTORS_MAX];
    ap_uint<SHOTS> observable_out[NUM_OBSERVABLES_MAX];
    stim_frame_sampler(instructions.data(), count, layer_offsets.data(), num_layers, seed_lo, seed_hi, detector_out,
                        observable_out);

    std::FILE *out = std::fopen(argv[5], "wb");
    if (!out) {
        std::fprintf(stderr, "could not open %s for writing\n", argv[5]);
        return 2;
    }
    for (int d = 0; d < NUM_DETECTORS_MAX; d++) {
        uint64_t word = static_cast<uint64_t>(detector_out[d]);
        std::fwrite(&word, sizeof(word), 1, out);
    }
    for (int o = 0; o < NUM_OBSERVABLES_MAX; o++) {
        uint64_t word = static_cast<uint64_t>(observable_out[o]);
        std::fwrite(&word, sizeof(word), 1, out);
    }
    std::fclose(out);

    std::fprintf(stderr, "ran %d instructions across %d layers\n", count, num_layers);
    return 0;
}
