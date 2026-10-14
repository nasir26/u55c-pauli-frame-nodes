// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Tier 4 on real hardware: samples `repeat_count * SHOTS` shots from the
// real kernel (double-buffered, same structure as host/xrt_bench.cpp --
// see that file for why loading once and pipelining matters for
// throughput at this scale) and accumulates, per detector and per
// observable, how many of those shots fired -- not the raw per-shot
// bits, which would be an unwieldy amount of data at the shot counts
// Tier 4 needs (Stim itself uses 10^7). Prints "<index> <fired_count>"
// lines for detectors then observables, total shot count last, for
// bench/hw_tier4.py to turn into the same firing-rate z-test
// tests/test_softmodel_validation.py already runs against the soft
// model -- here against CPU Stim, from real silicon.
#include "../kernel/instruction.hpp"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
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

inline int popcount64(uint64_t x) {
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_popcountll(x);
#else
    int c = 0;
    while (x) {
        c += x & 1;
        x >>= 1;
    }
    return c;
#endif
}

}  // namespace

int main(int argc, char **argv) {
    if (argc != 5) {
        std::fprintf(stderr, "usage: %s <xclbin> <instructions.bin> <layer_offsets.bin> <repeat_count>\n", argv[0]);
        return 2;
    }
    const char *xclbin_path = argv[1];
    const char *instructions_path = argv[2];
    const char *layer_offsets_path = argv[3];
    const long repeat_count = std::strtol(argv[4], nullptr, 10);
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

    std::vector<uint64_t> detector_fired(NUM_DETECTORS_MAX, 0);
    std::vector<uint64_t> observable_fired(NUM_OBSERVABLES_MAX, 0);

    auto accumulate = [&](int slot) {
        InFlight &s = slots[slot];
        s.run.wait();
        s.detector_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
        s.observable_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
        const uint64_t *det = s.detector_out.map<const uint64_t *>();
        const uint64_t *obs = s.observable_out.map<const uint64_t *>();
        for (int d = 0; d < NUM_DETECTORS_MAX; d++) detector_fired[d] += popcount64(det[d]);
        for (int o = 0; o < NUM_OBSERVABLES_MAX; o++) observable_fired[o] += popcount64(obs[o]);
    };

    // Seeds: each run's 64 lanes are independent shots (Philox counter
    // includes the lane index -- kernel/prng.hpp), so a distinct seed per
    // *run* is what makes different runs independent batches, not
    // repeats of the same 64 shots.
    launch(0, 0, 0);
    for (long i = 1; i < repeat_count; i++) {
        launch(static_cast<int>(i % 2), static_cast<uint32_t>(i), 0);
        accumulate(static_cast<int>((i - 1) % 2));
    }
    accumulate(static_cast<int>((repeat_count - 1) % 2));

    const uint64_t total_shots = static_cast<uint64_t>(repeat_count) * SHOTS;
    for (int d = 0; d < NUM_DETECTORS_MAX; d++) std::printf("D %d %lu\n", d, (unsigned long)detector_fired[d]);
    for (int o = 0; o < NUM_OBSERVABLES_MAX; o++) std::printf("O %d %lu\n", o, (unsigned long)observable_fired[o]);
    std::printf("TOTAL %lu\n", (unsigned long)total_shots);
    return 0;
}
