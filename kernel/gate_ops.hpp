// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Every Clifford gate as a read-XOR-write on the frame store. Pauli
// gates (X, Y, Z) applied as *circuit* instructions (as opposed to
// noise) are deliberately absent: conjugating a Pauli frame by a Pauli
// gate never changes which error type (I/X/Y/Z) is present, only its
// sign, and sign doesn't affect which detectors fire -- see
// softmodel/reference_sampler.py's module docstring for the full
// reasoning. Two-qubit gates take their inputs into local variables
// first so that swapping gates (CZ, SWAP) stay correct: writing through
// one qubit's row before reading the other's would corrupt the second
// read.
#pragma once

#include "frame_store.hpp"

namespace stim_u55c {

inline void gate_h(FrameStore &fs, int q) {
    ap_uint<SHOTS> old_x = fs.x[q];
    fs.x[q] = fs.z[q];
    fs.z[q] = old_x;
}

inline void gate_s(FrameStore &fs, int q) {
    fs.z[q] ^= fs.x[q];
}

inline void gate_cx(FrameStore &fs, int control, int target) {
    fs.x[target] ^= fs.x[control];
    fs.z[control] ^= fs.z[target];
}

inline void gate_cz(FrameStore &fs, int a, int b) {
    ap_uint<SHOTS> xa = fs.x[a];
    ap_uint<SHOTS> xb = fs.x[b];
    fs.z[b] ^= xa;
    fs.z[a] ^= xb;
}

inline void gate_swap(FrameStore &fs, int a, int b) {
    ap_uint<SHOTS> tx = fs.x[a];
    fs.x[a] = fs.x[b];
    fs.x[b] = tx;
    ap_uint<SHOTS> tz = fs.z[a];
    fs.z[a] = fs.z[b];
    fs.z[b] = tz;
}

inline void reset_qubit(FrameStore &fs, int q) {
    fs.x[q] = 0;
    fs.z[q] = 0;
}

}  // namespace stim_u55c
