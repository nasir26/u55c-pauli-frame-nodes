// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Measurement -> detection event reduction (Stim section 5.6), done as a
// single streaming pass with no measurement-history buffer: every
// rec[-k] lookback a DETECTOR or OBSERVABLE_INCLUDE instruction could
// reference was already resolved at compile time by kernel/isa.py, which
// patched the *originating* MEASURE-type instruction's detector_mask /
// observable_mask instead. So by the time execution reaches a
// measurement, folding is just "which accumulators does this bit belong
// to" -- answered by the instruction's own mask fields -- with no lookup
// into anything the kernel measured earlier.
#pragma once

#include "ap_uint.hpp"
#include "config.hpp"
#include "instruction.hpp"

namespace stim_u55c {

struct DetectorFold {
    ap_uint<SHOTS> detectors[NUM_DETECTORS_MAX];
    ap_uint<SHOTS> observables[NUM_OBSERVABLES_MAX];

    void reset_all() {
    RESET_DETECTORS:
        for (int d = 0; d < NUM_DETECTORS_MAX; d++) {
#pragma HLS pipeline II = 1
            detectors[d] = 0;
        }
    RESET_OBSERVABLES:
        for (int o = 0; o < NUM_OBSERVABLES_MAX; o++) {
#pragma HLS pipeline II = 1
            observables[o] = 0;
        }
    }

    // `effect` is the frame-induced deviation for one measurement
    // (actual XOR reference; see softmodel/reference_sampler.py's
    // rec_actual_xor_reference docstring for why that's the right
    // quantity to fold, rather than the raw measurement bit).
    void fold(const Instruction &instr, ap_uint<SHOTS> effect) {
    FOLD_DETECTORS:
        for (int d = 0; d < NUM_DETECTORS_MAX; d++) {
#pragma HLS pipeline II = 1
            if (mask_bit(instr.detector_mask, d)) {
                detectors[d] ^= effect;
            }
        }
    FOLD_OBSERVABLES:
        for (int o = 0; o < NUM_OBSERVABLES_MAX; o++) {
#pragma HLS pipeline II = 1
            if (mask_bit(instr.observable_mask, o)) {
                observables[o] ^= effect;
            }
        }
    }
};

}  // namespace stim_u55c
