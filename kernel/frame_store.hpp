// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// URAM-resident Pauli frame state: one x bit and one z bit per qubit per
// shot, shot axis packed into a single SHOTS-wide word so a full row
// (all shots for one qubit) reads or writes in one cycle. x and z are
// separate arrays -- Gidney found interleaving them a mistake (Stim
// section 5.1): several gates need x XORed into z or vice versa, which
// interleaving would turn into shifts and masks instead of a plain XOR.
//
// #pragma HLS array_partition tuning (URAM bank spreading) is a Phase 3
// concern once there's a synthesis report to tune against -- see
// docs/README.md.
#pragma once

#include "ap_uint.hpp"
#include "config.hpp"

namespace stim_u55c {

struct FrameStore {
    ap_uint<SHOTS> x[NUM_QUBITS_MAX];
    ap_uint<SHOTS> z[NUM_QUBITS_MAX];

    void reset_all() {
    RESET_ALL_QUBITS:
        for (int q = 0; q < NUM_QUBITS_MAX; q++) {
#pragma HLS pipeline II = 1
            x[q] = 0;
            z[q] = 0;
        }
    }
};

}  // namespace stim_u55c
