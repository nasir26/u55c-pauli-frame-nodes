// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Sizing of the batch kernel (stim_batch_sampler.cpp). Mirrored in
// kernel/isa_batch.py, which checks a compiled program against these limits;
// tests/test_batch_kernel.py checks that the two files agree.
#pragma once

namespace stim_batch {

constexpr int LANES = 64;           // shots per batch: one bit per shot in every frame word
constexpr int PAIRS = LANES / 2;    // Philox units: each 128 bit output serves two lanes
constexpr int QUBITS_MAX = 128;     // after the compiler's dense renumbering of the qubits a circuit uses
constexpr int PROG_MAX = 8192;      // instructions held on chip (including the scheduler's NOPs)
// Minimum distance, in instructions, between two instructions that touch the same qubit, and number of NOPs at
// the start and at the end of every program (the frame is cleared in the first slot of each batch). The frame stage reads a qubit several pipeline stages before it
// writes it back and does not check for hazards; kernel/isa_batch.py schedules every program to keep this
// distance. It must exceed the write minus read distance of the FRAME pipeline (checked by RTL cosimulation).
constexpr int HAZARD_DISTANCE = 8;
constexpr int MEAS_MAX = 2048;      // measurements per shot
constexpr int FOLD_MAX = 8192;      // (output, measurement) pairs of the detector and observable fold
constexpr int OUT_MAX = 512;        // detectors plus observables
constexpr int OUT_DEPTH = 65536;    // output words declared to the m_axi interface (cosimulation buffer size only)

// 64 bit instruction record
constexpr int OP_LSB = 0;
constexpr int OP_BITS = 5;
constexpr int QA_LSB = 5;
constexpr int QB_LSB = 12;
constexpr int Q_BITS = 7;
constexpr int SLOT_LSB = 19;
constexpr int SLOT_BITS = 12;
constexpr int B_VALID_BIT = 31;     // set when qubit_b is used (two qubit instructions)
constexpr int THR_LSB = 32;

// 32 bit fold entry
constexpr int FOLD_MEAS_BITS = 12;
constexpr int FOLD_LAST_BIT = 12;   // last entry of an output: emit the accumulator
constexpr int FOLD_NONE_BIT = 13;   // entry contributes nothing (an output with no measurement)

}  // namespace stim_batch
