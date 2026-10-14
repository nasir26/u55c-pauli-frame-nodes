// GENERATED FILE -- do not edit by hand. Regenerate with:
//   python3 kernel/generate_headers.py
// Source of truth: kernel/isa.py:Opcode
#pragma once
#include <cstdint>

namespace stim_u55c {

enum Opcode : uint8_t {
    OPCODE_NOP = 0,
    OPCODE_RESET_Z = 1,
    OPCODE_RESET_X = 2,
    OPCODE_RESET_Y = 3,
    OPCODE_MEASURE_Z = 4,
    OPCODE_MEASURE_X = 5,
    OPCODE_MEASURE_Y = 6,
    OPCODE_MEASURE_RESET_Z = 7,
    OPCODE_MEASURE_RESET_X = 8,
    OPCODE_MEASURE_RESET_Y = 9,
    OPCODE_GATE_H = 10,
    OPCODE_GATE_S = 11,
    OPCODE_GATE_CX = 12,
    OPCODE_GATE_CZ = 13,
    OPCODE_GATE_SWAP = 14,
    OPCODE_NOISE_X = 15,
    OPCODE_NOISE_Y = 16,
    OPCODE_NOISE_Z = 17,
    OPCODE_NOISE_DEPOLARIZE1 = 18,
    OPCODE_NOISE_DEPOLARIZE2 = 19,
    OPCODE_END_OF_PROGRAM = 255,
};

}  // namespace stim_u55c
