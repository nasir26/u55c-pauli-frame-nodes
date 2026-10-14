// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// A minimal, uint64_t-backed stand-in for Xilinx's ap_uint<N>, covering
// only what this kernel actually uses (zero/int construction, ^, ^=,
// per-bit read/write via operator[], implicit conversion to uint64_t).
// This is what lets kernel/hls_testbench/ compile and run with plain
// g++ and no Vitis install -- "no toolchain, no card" for Tier 1-2 CI.
//
// It is explicitly NOT a substitute for real ap_int.h once Phase 3 needs
// actual HLS synthesis: Vitis HLS treats ap_uint<N> specially to
// generate bit-width-exact hardware, and a uint64_t wrapper gets none of
// that. Swapping this header for <ap_int.h> (both expose the same
// `ap_uint<N>` surface the rest of kernel/ uses) is the intended Phase 3
// transition, not a design this shim tries to preempt.
//
// SHOTS <= 64 only, matching the current config -- see
// python/stim_u55c/config.py. Extending past 64 shots means widening
// this to a multi-word representation, or switching to real ap_int.h
// early.
#pragma once

#include <cstdint>

namespace stim_u55c {

template <int N>
class ap_uint {
    static_assert(N > 0 && N <= 64, "ap_uint_shim supports 1..64 bits only");

    static constexpr uint64_t kMask = (N == 64) ? ~uint64_t(0) : ((uint64_t(1) << N) - 1);

    uint64_t value_ = 0;

  public:
    ap_uint() = default;
    ap_uint(uint64_t v) : value_(v & kMask) {}  // NOLINT(google-explicit-constructor)

    operator uint64_t() const { return value_; }  // NOLINT(google-explicit-constructor)

    ap_uint &operator=(uint64_t v) {
        value_ = v & kMask;
        return *this;
    }
    ap_uint &operator^=(const ap_uint &other) {
        value_ = (value_ ^ other.value_) & kMask;
        return *this;
    }
    ap_uint operator^(const ap_uint &other) const { return ap_uint((value_ ^ other.value_) & kMask); }

    class BitRef {
        ap_uint &parent_;
        int bit_;

      public:
        BitRef(ap_uint &parent, int bit) : parent_(parent), bit_(bit) {}
        BitRef &operator=(int v) {
            if (v) {
                parent_.value_ |= (uint64_t(1) << bit_);
            } else {
                parent_.value_ &= ~(uint64_t(1) << bit_);
            }
            return *this;
        }
        operator bool() const { return (parent_.value_ >> bit_) & 1ULL; }  // NOLINT(google-explicit-constructor)
    };

    BitRef operator[](int bit) { return BitRef(*this, bit); }
    bool operator[](int bit) const { return (value_ >> bit) & 1ULL; }
};

}  // namespace stim_u55c
