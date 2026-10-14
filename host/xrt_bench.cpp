// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Mode A throughput benchmark: loads the device/xclbin/instruction
// stream once (unlike host/xrt_runner.cpp, which is a single-shot
// correctness-checking tool and reloads everything every process
// invocation -- fine for validation, useless for measuring throughput
// since xclbin load time would dominate), then launches the kernel
// `--repeat N` times back to back with a fresh seed each time, with (per
// the project brief's Mode A description) at least 2 xrt::run objects in
// flight -- while run A executes on the card, the host sets up and
// launches run B, so host-side overhead (buffer sync, argument
// marshaling) overlaps kernel execution instead of serializing after it.
//
// Reports shots/sec = (SHOTS * repeats) / elapsed wall-clock seconds,
// timed around the launch/wait loop only (device+xclbin load and the
// one-time instruction upload are excluded, matching how a real
// benchmark should report -- see bench/README.md).
//
// Multi card use (bench/multicard/run_multicard.py): optional arguments
// <device_index> <seed_stream> pick the card and put the stream number in
// seed_hi, the high word of the Philox key, so concurrent cards draw
// independent noise (seed_lo is still the run index). Defaults 0 and 0 give
// exactly the single card behaviour above. If STIM_GO_FILE is set, the
// timed loop starts only once that file exists, so several processes can
// load their cards first and then start together; the wall clock window of
// the timed loop is reported on stderr for aggregating across processes.
#include "../kernel/instruction.hpp"
#include <chrono>
#include <thread>
#include <sys/stat.h>
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

}  // namespace

int main(int argc, char **argv) {
    if (argc != 5 && argc != 7) {
        std::fprintf(stderr, "usage: %s <xclbin> <instructions.bin> <layer_offsets.bin> <repeat_count> "
                             "[<device_index> <seed_stream>]\n", argv[0]);
        return 2;
    }
    const char *xclbin_path = argv[1];
    const char *instructions_path = argv[2];
    const char *layer_offsets_path = argv[3];
    const long repeat_count = std::strtol(argv[4], nullptr, 10);
    if (repeat_count < 2) fail("repeat_count must be >= 2 (need at least 2 runs to pipeline)");
    const unsigned device_index = argc == 7 ? static_cast<unsigned>(std::strtoul(argv[5], nullptr, 10)) : 0;
    const uint32_t seed_stream = argc == 7 ? static_cast<uint32_t>(std::strtoul(argv[6], nullptr, 10)) : 0;

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

    std::fprintf(stderr, "loaded %d instructions across %d layers; benchmarking %ld runs (%ld shots)\n",
                 num_instructions, num_layers, repeat_count, repeat_count * SHOTS);

    xrt::device device(device_index);
    xrt::uuid uuid = device.load_xclbin(xclbin_path);
    xrt::kernel kernel(device, uuid, "stim_frame_sampler");

    xrt::bo bo_instructions(device, instructions.size() * sizeof(Instruction), kernel.group_id(0));
    xrt::bo bo_layer_offsets(device, layer_offsets.size() * sizeof(uint32_t), kernel.group_id(2));
    std::memcpy(bo_instructions.map<Instruction *>(), instructions.data(), instructions.size() * sizeof(Instruction));
    std::memcpy(bo_layer_offsets.map<uint32_t *>(), layer_offsets.data(), layer_offsets.size() * sizeof(uint32_t));
    bo_instructions.sync(XCL_BO_SYNC_BO_TO_DEVICE);
    bo_layer_offsets.sync(XCL_BO_SYNC_BO_TO_DEVICE);

    // Two independent output-buffer/run slots, ping-ponged: while slot A's
    // run executes, slot B's run is created and started, so the two
    // overlap rather than the host waiting idle between them.
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

    std::fprintf(stderr, "ready\n");
    std::fflush(stderr);
    if (const char *go = std::getenv("STIM_GO_FILE")) {
        struct stat st;
        while (stat(go, &st) != 0) std::this_thread::sleep_for(std::chrono::microseconds(200));
    }
    const auto wall_start = std::chrono::system_clock::now();
    const auto start = std::chrono::steady_clock::now();

    launch(0, 0, seed_stream);
    long completed = 0;
    for (long i = 1; i < repeat_count; i++) {
        launch(static_cast<int>(i % 2), static_cast<uint32_t>(i), seed_stream);  // start run i while run i-1 is still in flight
        slots[(i - 1) % 2].run.wait();
        slots[(i - 1) % 2].detector_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);  // realistic: a real caller reads this back
        completed++;
    }
    slots[(repeat_count - 1) % 2].run.wait();
    slots[(repeat_count - 1) % 2].detector_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
    completed++;

    const auto end = std::chrono::steady_clock::now();
    const auto wall_end = std::chrono::system_clock::now();
    const double elapsed_s = std::chrono::duration<double>(end - start).count();
    const double shots_per_sec = static_cast<double>(completed) * SHOTS / elapsed_s;

    std::fprintf(stderr, "completed %ld runs in %.6f s\n", completed, elapsed_s);
    std::fprintf(stderr, "window_ns %lld %lld device %u stream %u bdf %s\n",
                 static_cast<long long>(std::chrono::duration_cast<std::chrono::nanoseconds>(wall_start.time_since_epoch()).count()),
                 static_cast<long long>(std::chrono::duration_cast<std::chrono::nanoseconds>(wall_end.time_since_epoch()).count()),
                 device_index, seed_stream, device.get_info<xrt::info::device::bdf>().c_str());
    std::printf("%.3f\n", shots_per_sec);  // stdout: just the number, for scripting
    return 0;
}
