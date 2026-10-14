// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// The in-memory instruction record, and a reader for the binary stream
// kernel/isa.py:Program.serialize() produces. Field order and widths
// here must match isa.py's `_STRUCT` format ("<BBBxI{n}s{m}s") exactly --
// deliberately read byte-by-byte rather than reinterpret-cast a raw
// buffer onto this struct, so struct padding/alignment can never cause a
// silent mismatch between the two.
//
// An instruction's PRNG counter component is its *position* in the
// stream (i.e. the loop index a caller iterates instructions with), not
// a separately stored field: kernel/isa.py assigns `Instruction.index`
// as exactly that position at encode time and never reorders or drops
// instructions when serializing, so the invariant holds by construction.
// softmodel/kernel_replay.py relies on the same invariant.
#pragma once

#include "config.hpp"
#include <cstdint>
#include <cstdio>

namespace stim_u55c {

struct Instruction {
    uint8_t opcode;
    uint8_t qubit_a;
    uint8_t qubit_b;
    uint32_t prob_threshold;
    uint8_t detector_mask[NUM_DETECTOR_BYTES];
    uint8_t observable_mask[NUM_OBSERVABLE_BYTES];
};

inline bool mask_bit(const uint8_t *mask, int bit) {
    return (mask[bit / 8] >> (bit % 8)) & 1;
}

// Reads up to `max_instructions` records from `f` (as produced by
// Program.serialize()) into `out`. Returns the number read.
inline int read_program(std::FILE *f, Instruction *out, int max_instructions) {
    int count = 0;
    uint8_t record[RECORD_SIZE];
    while (count < max_instructions && std::fread(record, 1, RECORD_SIZE, f) == static_cast<size_t>(RECORD_SIZE)) {
        Instruction &instr = out[count];
        instr.opcode = record[0];
        instr.qubit_a = record[1];
        instr.qubit_b = record[2];
        // record[3] is the struct-pack alignment pad byte ('x' in "<BBBxI...").
        instr.prob_threshold = static_cast<uint32_t>(record[4]) | (static_cast<uint32_t>(record[5]) << 8) |
                                (static_cast<uint32_t>(record[6]) << 16) | (static_cast<uint32_t>(record[7]) << 24);
        for (int i = 0; i < NUM_DETECTOR_BYTES; i++) {
            instr.detector_mask[i] = record[8 + i];
        }
        for (int i = 0; i < NUM_OBSERVABLE_BYTES; i++) {
            instr.observable_mask[i] = record[8 + NUM_DETECTOR_BYTES + i];
        }
        count++;
    }
    return count;
}

// Reads up to `max_layers + 1` little-endian uint32 offsets from `f` (as
// produced by Program.serialize_layer_offsets()) into `out`. Returns the
// number read (num_layers + 1, including the trailing sentinel -- see
// kernel/isa.py:Program.layer_offsets).
inline int read_layer_offsets(std::FILE *f, uint32_t *out, int max_layers) {
    int count = 0;
    uint8_t word[4];
    while (count < max_layers + 1 && std::fread(word, 1, 4, f) == 4) {
        out[count] = static_cast<uint32_t>(word[0]) | (static_cast<uint32_t>(word[1]) << 8) |
                     (static_cast<uint32_t>(word[2]) << 16) | (static_cast<uint32_t>(word[3]) << 24);
        count++;
    }
    return count;
}

}  // namespace stim_u55c
