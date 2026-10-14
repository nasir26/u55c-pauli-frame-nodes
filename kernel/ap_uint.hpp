// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Selects which ap_uint<N> the rest of kernel/ builds against: real
// Xilinx ap_int.h when synthesizing (define STIM_U55C_USE_XILINX_AP_INT,
// e.g. via vitis_hls's -D flag), the portable shim otherwise so
// hls_testbench/ keeps compiling with plain g++ and no Vitis install.
// See ap_uint_shim.hpp for why the two aren't equivalent for synthesis.
//
// Xilinx's ap_uint<N> is declared in the global namespace; the shim
// declares stim_u55c::ap_uint<N>. Everything under kernel/ uses the
// unqualified name ap_uint<N> from inside `namespace stim_u55c { ... }`,
// so ordinary C++ unqualified lookup finds whichever one is in scope --
// no alias needed, and no kernel source changes between the two modes.
#pragma once

#ifdef STIM_U55C_USE_XILINX_AP_INT
#include <ap_int.h>
#else
#include "ap_uint_shim.hpp"
// Real ap_int.h puts ap_uint<N> at global scope; the shim puts it in
// stim_u55c to avoid polluting the global namespace when it isn't
// standing in for a real Xilinx header. stim_frame_sampler.hpp's top
// function is declared at global scope (see its own comment for why),
// so it needs ap_uint<N> visible there in shim mode too.
using stim_u55c::ap_uint;
#endif
