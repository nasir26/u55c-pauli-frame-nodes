// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Host runtime of the batch kernel (kernel/batch/stim_batch_sampler.cpp).
// Loads the card, the program and the fold list once, then launches every
// kernel instance (compute unit) of the bitstream `launches` times with
// `batches` batches of LANES shots each, two runs per instance in flight so
// that the host side of one launch overlaps the kernel of the other. Launch
// k of instance c samples batches c * 2^28 + k * batches onwards, so no two
// launches and no two instances reuse a Philox counter (up to 2^28 batches
// per instance and run). Every launch's output is copied
// back to host memory: shots/sec counts shots whose detector and observable
// bits have reached the host, which is what a caller of the sampler gets
// (and what CPU Stim's sample() returns).
//
// usage: xrt_batch <xclbin> <instructions.bin> <fold.bin> <num_meas> <num_out> <batches> <launches>
//                  <device_index> <seed_lo> <seed_hi> [<dump.bin>]
// With dump.bin, the output of every launch is appended to it, instance after
// instance within a launch (kernel layout, see tb_stim_batch_sampler.cpp), for
// checking against softmodel/batch_replay.py.
// As in xrt_bench.cpp: "ready" on stderr after setup, optional STIM_GO_FILE
// start barrier, the wall clock window of the timed loop on stderr, and
// shots/sec on stdout.
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>
#include <sys/stat.h>

#include <experimental/xrt_bo.h>
#include <experimental/xrt_device.h>
#include <experimental/xrt_kernel.h>
#include <experimental/xrt_xclbin.h>
#include <string>

#include "../kernel/batch/batch_config.hpp"

using namespace stim_batch;

namespace {

[[noreturn]] void fail(const char *msg) {
    std::fprintf(stderr, "error: %s\n", msg);
    std::exit(1);
}

template <typename T>
std::vector<T> read_all(const char *path) {
    std::FILE *f = std::fopen(path, "rb");
    if (!f) fail("could not open an input file");
    std::vector<T> v;
    T x;
    while (std::fread(&x, sizeof(T), 1, f) == 1) v.push_back(x);
    std::fclose(f);
    return v;
}

long long epoch_ns(std::chrono::system_clock::time_point t) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(t.time_since_epoch()).count();
}

}  // namespace

int main(int argc, char **argv) {
    if (argc != 11 && argc != 12) {
        std::fprintf(stderr, "usage: %s <xclbin> <instructions.bin> <fold.bin> <num_meas> <num_out> <batches> "
                             "<launches> <device_index> <seed_lo> <seed_hi> [<dump.bin>]\n", argv[0]);
        return 2;
    }
    const char *xclbin_path = argv[1];
    std::vector<uint64_t> prog = read_all<uint64_t>(argv[2]);
    std::vector<uint32_t> fold = read_all<uint32_t>(argv[3]);
    const int num_meas = std::atoi(argv[4]), num_out = std::atoi(argv[5]);
    const int batches = std::atoi(argv[6]);
    const long launches = std::atol(argv[7]);
    const unsigned device_index = std::strtoul(argv[8], nullptr, 10);
    const uint32_t seed_lo = std::strtoul(argv[9], nullptr, 10), seed_hi = std::strtoul(argv[10], nullptr, 10);
    std::FILE *dump = argc == 12 ? std::fopen(argv[11], "wb") : nullptr;
    if (argc == 12 && !dump) fail("could not open the dump file");
    if (launches < 1 || batches < 1) fail("batches and launches must be positive");
    if (prog.size() > (size_t)PROG_MAX || fold.size() > (size_t)FOLD_MAX) fail("program exceeds the kernel limits");
    if (fold.empty()) fold.push_back(0);

    xrt::device device(device_index);
    xrt::uuid uuid = device.load_xclbin(xclbin_path);
    // instances are named batch_1, batch_2, ... by build/batch_build.sh; the xclbin gives their number (XRT
    // 2.16 returns an empty name for some instances, so the names are not taken from it)
    std::vector<std::string> cu_names;
    const size_t n_cu = xrt::xclbin(xclbin_path).get_kernel("stim_batch_sampler").get_cus().size();
    for (size_t c = 1; c <= n_cu; c++) cu_names.push_back("stim_batch_sampler:{batch_" + std::to_string(c) + "}");
    if (cu_names.empty()) fail("no stim_batch_sampler instance in the xclbin");
    const size_t out_bytes = (size_t)num_out * batches * (LANES / 8);

    struct Instance {
        xrt::kernel kernel;
        xrt::bo prog, fold, out[2];
        xrt::run runs[2];
    };
    std::vector<Instance> inst;
    for (const std::string &name : cu_names) {
        xrt::kernel k(device, uuid, name);
        Instance in{k, xrt::bo(device, prog.size() * 8, k.group_id(0)), xrt::bo(device, fold.size() * 4, k.group_id(2)),
                    {xrt::bo(device, out_bytes, k.group_id(10)), xrt::bo(device, out_bytes, k.group_id(10))},
                    {xrt::run(k), xrt::run(k)}};
        std::memcpy(in.prog.map<uint64_t *>(), prog.data(), prog.size() * 8);
        std::memcpy(in.fold.map<uint32_t *>(), fold.data(), fold.size() * 4);
        in.prog.sync(XCL_BO_SYNC_BO_TO_DEVICE);
        in.fold.sync(XCL_BO_SYNC_BO_TO_DEVICE);
        inst.push_back(in);
    }

    auto launch = [&](long k) {
        for (size_t c = 0; c < inst.size(); c++) {
            Instance &in = inst[c];
            in.runs[k % 2] = in.kernel(in.prog, (int)prog.size(), in.fold, (int)fold.size(), num_meas, num_out, batches,
                                       (uint32_t)((c << 28) + k * batches), seed_lo, seed_hi, in.out[k % 2]);
        }
    };
    auto finish = [&](long k) {
        for (Instance &in : inst) {
            in.runs[k % 2].wait();
            in.out[k % 2].sync(XCL_BO_SYNC_BO_FROM_DEVICE);
            if (dump) std::fwrite(in.out[k % 2].map<const char *>(), 1, out_bytes, dump);
        }
    };

    std::fprintf(stderr, "loaded %zu instructions, %zu fold entries; %zu instances, %ld launches of %d batches "
                 "(%d lanes)\n", prog.size(), fold.size(), inst.size(), launches, batches, LANES);
    std::fprintf(stderr, "instances %zu\n", inst.size());
    std::fprintf(stderr, "ready\n");
    std::fflush(stderr);
    if (const char *go = std::getenv("STIM_GO_FILE")) {
        struct stat st;
        while (stat(go, &st) != 0) std::this_thread::sleep_for(std::chrono::microseconds(200));
    }

    const auto wall_start = std::chrono::system_clock::now();
    const auto start = std::chrono::steady_clock::now();
    launch(0);
    for (long k = 1; k < launches; k++) {
        launch(k);      // run k starts while run k - 1 is still on the card
        finish(k - 1);
    }
    finish(launches - 1);
    const auto end = std::chrono::steady_clock::now();
    const auto wall_end = std::chrono::system_clock::now();
    if (dump) std::fclose(dump);

    const double elapsed = std::chrono::duration<double>(end - start).count();
    const double shots = (double)launches * batches * LANES * inst.size();
    std::fprintf(stderr, "completed %ld launches, %.0f shots in %.6f s\n", launches, shots, elapsed);
    std::fprintf(stderr, "window_ns %lld %lld device %u stream %u bdf %s\n", epoch_ns(wall_start), epoch_ns(wall_end),
                 device_index, seed_hi, device.get_info<xrt::info::device::bdf>().c_str());
    std::printf("%.3f\n", shots / elapsed);
    return 0;
}
