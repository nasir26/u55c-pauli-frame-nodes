// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Tier 5 on real hardware: like xrt_tier4.cpp, loads the device/xclbin/
// instruction stream once and pipelines 2 xrt::run objects in flight,
// but writes full per-shot bit-packed syndrome data instead of
// aggregate counts -- Tier 4 only needed firing rates, Tier 5 needs
// actual syndromes to hand to PyMatching for decoding.
//
// The kernel's own output is detector-major: detector_out[d] is one
// 64-bit word, bit s of which is detector d's result in shot s. What
// PyMatching's decode_batch(bit_packed_shots=True) wants is shot-major:
// one row per shot, NUM_DETECTOR_BYTES bytes per row, bit i of byte j
// being detector (8*j+i)'s result -- Stim's own b8 convention. That's a
// transpose this kernel doesn't do on-chip (a from-scratch b8 staging
// design, the kind the project brief's own architecture notes describe,
// is future work -- see bench/README.md); done here on the host once
// per batch, which is cheap enough (64 shots x 256 detectors bits) not
// to matter next to the kernel launch itself.
#include "../kernel/instruction.hpp"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <experimental/xrt_bo.h>
#include <experimental/xrt_device.h>
#include <experimental/xrt_kernel.h>

using namespace stim_u55c;

namespace {

[[noreturn]] void fail(const char *msg) {
    std::fprintf(stderr, "error: %s\n", msg);
    std::exit(1);
}

struct InFlight {
    xrt::bo detector_out;
    xrt::bo observable_out;
    xrt::run run;
};

void write_transposed(std::FILE *out, const uint64_t *words, int num_words, int bytes_per_shot) {
    for (int s = 0; s < SHOTS; s++) {
        std::vector<uint8_t> row(bytes_per_shot, 0);
        for (int w = 0; w < num_words && w < bytes_per_shot * 8; w++) {
            if ((words[w] >> s) & 1ULL) {
                row[w / 8] |= static_cast<uint8_t>(1u << (w % 8));
            }
        }
        std::fwrite(row.data(), 1, bytes_per_shot, out);
    }
}

}  // namespace

int main(int argc, char **argv) {
    if (argc != 6) {
        std::fprintf(stderr,
                      "usage: %s <xclbin> <instructions.bin> <layer_offsets.bin> <repeat_count> <out_prefix>\n",
                      argv[0]);
        return 2;
    }
    const char *xclbin_path = argv[1];
    const char *instructions_path = argv[2];
    const char *layer_offsets_path = argv[3];
    const long repeat_count = std::strtol(argv[4], nullptr, 10);
    const std::string out_prefix = argv[5];
    if (repeat_count < 2) fail("repeat_count must be >= 2 (need at least 2 runs to pipeline)");

    std::FILE *instr_f = std::fopen(instructions_path, "rb");
    if (!instr_f) fail("could not open instructions file");
    static constexpr int kMaxInstructions = 1 << 20;
    std::vector<Instruction> instructions(kMaxInstructions);
    int num_instructions = read_program(instr_f, instructions.data(), kMaxInstructions);
    std::fclose(instr_f);
    instructions.resize(num_instructions);

    std::FILE *layers_f = std::fopen(layer_offsets_path, "rb");
    if (!layers_f) fail("could not open layer offsets file");
    std::vector<uint32_t> layer_offsets(NUM_LAYERS_MAX + 1);
    int num_offsets = read_layer_offsets(layers_f, layer_offsets.data(), NUM_LAYERS_MAX);
    std::fclose(layers_f);
    int num_layers = num_offsets - 1;
    layer_offsets.resize(num_offsets);

    std::fprintf(stderr, "loaded %d instructions across %d layers; sampling %ld runs (%ld shots)\n",
                 num_instructions, num_layers, repeat_count, repeat_count * SHOTS);

    xrt::device device(0);
    xrt::uuid uuid = device.load_xclbin(xclbin_path);
    xrt::kernel kernel(device, uuid, "stim_frame_sampler");

    xrt::bo bo_instructions(device, instructions.size() * sizeof(Instruction), kernel.group_id(0));
    xrt::bo bo_layer_offsets(device, layer_offsets.size() * sizeof(uint32_t), kernel.group_id(2));
    std::memcpy(bo_instructions.map<Instruction *>(), instructions.data(), instructions.size() * sizeof(Instruction));
    std::memcpy(bo_layer_offsets.map<uint32_t *>(), layer_offsets.data(), layer_offsets.size() * sizeof(uint32_t));
    bo_instructions.sync(XCL_BO_SYNC_BO_TO_DEVICE);
    bo_layer_offsets.sync(XCL_BO_SYNC_BO_TO_DEVICE);

    InFlight slots[2] = {
        {xrt::bo(device, NUM_DETECTORS_MAX * sizeof(uint64_t), kernel.group_id(6)),
         xrt::bo(device, NUM_OBSERVABLES_MAX * sizeof(uint64_t), kernel.group_id(7)), xrt::run(kernel)},
        {xrt::bo(device, NUM_DETECTORS_MAX * sizeof(uint64_t), kernel.group_id(6)),
         xrt::bo(device, NUM_OBSERVABLES_MAX * sizeof(uint64_t), kernel.group_id(7)), xrt::run(kernel)},
    };

    auto launch = [&](int slot, uint32_t seed_lo, uint32_t seed_hi) {
        InFlight &s = slots[slot];
        s.run = kernel(bo_instructions, num_instructions, bo_layer_offsets, num_layers, seed_lo, seed_hi,
                        s.detector_out, s.observable_out);
    };

    std::FILE *det_out = std::fopen((out_prefix + "_detectors.b8").c_str(), "wb");
    std::FILE *obs_out = std::fopen((out_prefix + "_observables.b8").c_str(), "wb");
    if (!det_out || !obs_out) fail("could not open output files for writing");

    auto accumulate = [&](int slot) {
        InFlight &s = slots[slot];
        s.run.wait();
        s.detector_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
        s.observable_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
        write_transposed(det_out, s.detector_out.map<const uint64_t *>(), NUM_DETECTORS_MAX, NUM_DETECTOR_BYTES);
        write_transposed(obs_out, s.observable_out.map<const uint64_t *>(), NUM_OBSERVABLES_MAX, NUM_OBSERVABLE_BYTES);
    };

    launch(0, 0, 0);
    for (long i = 1; i < repeat_count; i++) {
        launch(static_cast<int>(i % 2), static_cast<uint32_t>(i), 0);
        accumulate(static_cast<int>((i - 1) % 2));
    }
    accumulate(static_cast<int>((repeat_count - 1) % 2));

    std::fclose(det_out);
    std::fclose(obs_out);
    std::fprintf(stderr, "wrote %ld shots to %s_detectors.b8 / %s_observables.b8\n", repeat_count * SHOTS,
                 out_prefix.c_str(), out_prefix.c_str());
    return 0;
}
