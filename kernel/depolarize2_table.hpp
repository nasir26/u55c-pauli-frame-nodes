// GENERATED FILE -- do not edit by hand. Regenerate with:
//   python3 kernel/generate_headers.py
// Source of truth: kernel/isa.py:DEPOLARIZE2_COMBOS
//
// DEPOLARIZE2's 15 nontrivial two-qubit Pauli combinations, indexed
// by word1 % 15 (see stim_frame_sampler.cpp's depolarize2_combo()).
// This used to be a hand-transcribed table that silently disagreed
// with the Python side's actual itertools.product order -- Tier 2
// caught it. Generating both from one list is what rules that class
// of bug out for good.
#pragma once
#include <cstdint>

namespace stim_u55c {

constexpr uint8_t kDepolarize2AHasX[15] = {0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0};
constexpr uint8_t kDepolarize2AHasZ[15] = {0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1};
constexpr uint8_t kDepolarize2BHasX[15] = {1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0};
constexpr uint8_t kDepolarize2BHasZ[15] = {0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1};

}  // namespace stim_u55c
