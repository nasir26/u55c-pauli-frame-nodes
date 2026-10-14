// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// C simulation testbench of the batch kernel (also its cosimulation
// testbench). usage:
//   tb_stim_batch_sampler <instructions.bin> <fold.bin> <num_meas> <num_out> <num_batches> <batch_base>
//                         <seed_lo> <seed_hi> <output.bin>
// instructions.bin: uint64 records, fold.bin: uint32 entries (kernel/isa_batch.py). output.bin: for every batch,
// num_out words of LANES bits, each as LANES / 64 little endian uint64 (lane 0 in bit 0 of the first).
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../batch_config.hpp"
#include <ap_int.h>

using namespace stim_batch;

extern "C" void stim_batch_sampler(const ap_uint<64> *prog, int num_instr, const ap_uint<32> *fold, int num_fold,
                                   int num_meas, int num_out, int num_batches, uint32_t batch_base, uint32_t seed_lo,
                                   uint32_t seed_hi, ap_uint<LANES> *out);

template <typename T>
static std::vector<T> read_all(const char *path) {
    std::FILE *f = std::fopen(path, "rb");
    if (!f) {
        std::fprintf(stderr, "cannot open %s\n", path);
        std::exit(1);
    }
    std::vector<T> v;
    T x;
    while (std::fread(&x, sizeof(T), 1, f) == 1) v.push_back(x);
    std::fclose(f);
    return v;
}

int main(int argc, char **argv) {
    if (argc != 10) {
        std::fprintf(stderr, "usage: %s <instructions.bin> <fold.bin> <num_meas> <num_out> <num_batches> <batch_base> "
                             "<seed_lo> <seed_hi> <output.bin>\n", argv[0]);
        return 2;
    }
    std::vector<uint64_t> p = read_all<uint64_t>(argv[1]);
    std::vector<uint32_t> f = read_all<uint32_t>(argv[2]);
    const int num_meas = std::atoi(argv[3]), num_out = std::atoi(argv[4]), num_batches = std::atoi(argv[5]);
    const uint32_t batch_base = std::strtoul(argv[6], nullptr, 10);
    const uint32_t seed_lo = std::strtoul(argv[7], nullptr, 10), seed_hi = std::strtoul(argv[8], nullptr, 10);

    // buffers at least as deep as the interface depths: the cosimulation wrapper copies that many words
    if (p.size() > (size_t)PROG_MAX || f.size() > (size_t)FOLD_MAX) {
        std::fprintf(stderr, "program exceeds the kernel limits\n");
        return 1;
    }
    std::vector<ap_uint<64>> prog(PROG_MAX, 0);
    std::vector<ap_uint<32>> fold(FOLD_MAX, 0);
    std::copy(p.begin(), p.end(), prog.begin());
    std::copy(f.begin(), f.end(), fold.begin());
    const size_t out_words = (size_t)num_out * num_batches;
    std::vector<ap_uint<LANES>> out(std::max(out_words, (size_t)OUT_DEPTH), 0);
    stim_batch_sampler(prog.data(), (int)p.size(), fold.data(), (int)f.size(), num_meas, num_out, num_batches,
                       batch_base, seed_lo, seed_hi, out.data());

    std::FILE *o = std::fopen(argv[9], "wb");
    for (size_t j = 0; j < out_words; j++) {
        const ap_uint<LANES> &w = out[j];
        for (int k = 0; k < LANES / 64; k++) {
            uint64_t word = w.range(64 * k + 63, 64 * k);
            std::fwrite(&word, 8, 1, o);
        }
    }
    std::fclose(o);
    return 0;
}
