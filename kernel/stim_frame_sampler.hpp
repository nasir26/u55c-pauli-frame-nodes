// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
#pragma once

#include "ap_uint.hpp"
#include "config.hpp"
#include "frame_store.hpp"
#include "instruction.hpp"

// Deliberately at global scope, not inside namespace stim_u55c (unlike
// everything else in kernel/), and deliberately `extern "C"`. Two
// separate Vitis mechanisms both need this, found the hard way:
//   - C/RTL cosimulation generates a top-level "hw_stub" wrapper keyed
//     off the top function's linkage; a namespace-qualified top function
//     tripped that up (cosim failed to link against
//     `stim_frame_sampler_hw_stub` even though csynth itself was fine).
//     Global scope fixed this.
//   - sw_emu loads the compiled kernel as a shared library and looks up
//     the literal string "stim_frame_sampler" (the `-k` name passed to
//     `v++ -c`) via dlopen/dlsym -- which only finds it if the symbol
//     isn't C++ name-mangled. Global scope alone still mangles the name;
//     `extern "C"` is what actually gives it the plain symbol dlsym needs.
// This is the one function in kernel/ that v++ actually instantiates as
// the kernel, so it's also the one with a reason to deviate from the
// namespace the rest of the kernel uses -- see stim_frame_sampler.cpp.
// `extern "C"` only affects name mangling, not the type system, so the
// C++-typed parameters below (references to stim_u55c:: types, the
// ap_uint<N> template) are unaffected. ap_uint<N> resolves here in both
// real-ap_int.h and shim builds -- see ap_uint.hpp.
// `layer_offsets` has num_layers+1 entries (kernel/isa.py:Program.layer_offsets):
// layer L is instructions[layer_offsets[L] .. layer_offsets[L+1]). Every
// instruction within a layer is guaranteed qubit-disjoint from every
// other instruction in that same layer (kernel/isa.py:_layer_and_reorder)
// -- see stim_frame_sampler.cpp for what that guarantee is (and isn't)
// used for.
extern "C" void stim_frame_sampler(const stim_u55c::Instruction *instructions, int num_instructions,
                                    const uint32_t *layer_offsets, int num_layers, uint32_t seed_lo,
                                    uint32_t seed_hi, ap_uint<stim_u55c::SHOTS> detector_out[stim_u55c::NUM_DETECTORS_MAX],
                                    ap_uint<stim_u55c::SHOTS> observable_out[stim_u55c::NUM_OBSERVABLES_MAX]);
