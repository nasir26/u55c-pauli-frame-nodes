// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Mode A (bulk throughput) host runtime: loads a compiled instruction
// stream onto the card via XRT, runs the real stim_frame_sampler kernel
// on hardware (or sw_emu/hw_emu, same code either way -- see
// build/Makefile), and writes the resulting detector/observable
// accumulators in the same raw format kernel/hls_testbench/
// tb_stim_frame_sampler.cpp does, so the exact same Python-side
// comparison against softmodel/kernel_replay.py validates both.
//
// IMPORTANT: the instructions.bin / layer_offsets.bin files
// (kernel/isa.py:Program.serialize()) are NOT DMA'd to the device
// as-is. They're a compact, host-side-only wire format (see
// kernel/instruction.hpp's comment on why it's parsed byte-by-byte
// rather than reinterpret-cast). The kernel's m_axi `instructions` port
// is typed `const Instruction*` and HLS lays that struct out on the bus
// using the compiler's own struct layout -- so what has to land in
// device memory is an array of natively-constructed `Instruction`
// structs (via instruction.hpp's read_program(), exactly as the C-sim
// testbench already does), not the packed file's bytes directly. Getting
// this backwards would be a silent correctness bug specific to real
// hardware -- sw_emu/hw_emu use the same host code, so it can't hide
// there either.
//
// This is Mode A only (deep queues, throughput-optimized): a single
// buffer, single run, wait-to-completion. Mode B (low-latency, polled
// xrt::run::state(), host-memory bridge) is a separate runtime -- Phase 5.
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

}  // namespace

int main(int argc, char **argv) {
    if (argc != 7 && argc != 8) {
        std::fprintf(stderr,
                      "usage: %s <xclbin> <instructions.bin> <layer_offsets.bin> <seed_lo> <seed_hi> <output.bin> [<device_index>]\n",
                      argv[0]);
        return 2;
    }
    const char *xclbin_path = argv[1];
    const char *instructions_path = argv[2];
    const char *layer_offsets_path = argv[3];
    const uint32_t seed_lo = static_cast<uint32_t>(std::strtoul(argv[4], nullptr, 10));
    const uint32_t seed_hi = static_cast<uint32_t>(std::strtoul(argv[5], nullptr, 10));
    const char *output_path = argv[6];
    const unsigned device_index = argc == 8 ? static_cast<unsigned>(std::strtoul(argv[7], nullptr, 10)) : 0;

    // -- Load the compiled instruction stream into host memory (same
    // parser the C-sim testbench uses -- see file header). --
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

    std::fprintf(stderr, "loaded %d instructions across %d layers\n", num_instructions, num_layers);

    // -- Device + kernel setup. --
    xrt::device device(device_index);
    xrt::uuid uuid = device.load_xclbin(xclbin_path);
    xrt::kernel kernel(device, uuid, "stim_frame_sampler");

    // -- Buffers, one per memory-mapped kernel argument, each in the
    // memory bank build/connectivity.cfg assigned that argument
    // (kernel.group_id looks that mapping up from the loaded xclbin, so
    // this stays correct even if connectivity.cfg's bank numbers change). --
    xrt::bo bo_instructions(device, instructions.size() * sizeof(Instruction), kernel.group_id(0));
    xrt::bo bo_layer_offsets(device, layer_offsets.size() * sizeof(uint32_t), kernel.group_id(2));
    xrt::bo bo_detector_out(device, NUM_DETECTORS_MAX * sizeof(uint64_t), kernel.group_id(6));
    xrt::bo bo_observable_out(device, NUM_OBSERVABLES_MAX * sizeof(uint64_t), kernel.group_id(7));

    std::memcpy(bo_instructions.map<Instruction *>(), instructions.data(), instructions.size() * sizeof(Instruction));
    std::memcpy(bo_layer_offsets.map<uint32_t *>(), layer_offsets.data(), layer_offsets.size() * sizeof(uint32_t));
    bo_instructions.sync(XCL_BO_SYNC_BO_TO_DEVICE);
    bo_layer_offsets.sync(XCL_BO_SYNC_BO_TO_DEVICE);

    // -- Run. Argument order must match stim_frame_sampler's signature
    // exactly (kernel/stim_frame_sampler.hpp). --
    xrt::run run = kernel(bo_instructions, num_instructions, bo_layer_offsets, num_layers, seed_lo, seed_hi,
                           bo_detector_out, bo_observable_out);
    run.wait();

    bo_detector_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);
    bo_observable_out.sync(XCL_BO_SYNC_BO_FROM_DEVICE);

    std::FILE *out = std::fopen(output_path, "wb");
    if (!out) fail("could not open output file for writing");
    std::fwrite(bo_detector_out.map<uint8_t *>(), 1, NUM_DETECTORS_MAX * sizeof(uint64_t), out);
    std::fwrite(bo_observable_out.map<uint8_t *>(), 1, NUM_OBSERVABLES_MAX * sizeof(uint64_t), out);
    std::fclose(out);

    std::fprintf(stderr, "done\n");
    return 0;
}
