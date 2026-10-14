# stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
# Author: Nasir Ali, C-DAC Noida
#
# Phase 3: real vitis_hls C-synthesis (and, when STIM_U55C_HLS_COSIM=1
# is set, C/RTL cosimulation) of the top-level kernel. Run from
# kernel/hls/ (e.g. via `make hls-synth` / `make hls-cosim` in build/):
#   cd kernel/hls && vitis_hls -f run_hls.tcl
# Part and clock target come from the Phase 0 environment survey /
# README.md, not hardcoded elsewhere -- if the target part changes,
# change it here only.

set PART "xcu55c-fsvh2892-2L-e"
set CLOCK_PERIOD_NS 3.33 ;# 300 MHz target, per README.md / project brief

# cosim's testbench process runs from deep inside the HLS project's own
# sim directory, not from here -- the -argv paths below have to be
# absolute or the testbench can't find its input file.
set INVOCATION_DIR [pwd]

open_project -reset stim_frame_sampler_hls
set_top stim_frame_sampler

add_files ../stim_frame_sampler.cpp -cflags "-std=c++17 -DSTIM_U55C_USE_XILINX_AP_INT -I.."

if {[info exists ::env(STIM_U55C_HLS_COSIM)]} {
    add_files -tb ../hls_testbench/tb_stim_frame_sampler.cpp -cflags "-std=c++17 -DSTIM_U55C_USE_XILINX_AP_INT -I.."
}

open_solution -reset "solution1" -flow_target vitis
set_part $PART
create_clock -period $CLOCK_PERIOD_NS -name default

csynth_design

if {[info exists ::env(STIM_U55C_HLS_COSIM)]} {
    cosim_design -trace_level none -argv "$INVOCATION_DIR/test_vectors/instructions.bin $INVOCATION_DIR/test_vectors/layer_offsets.bin 305419896 2271560481 $INVOCATION_DIR/test_vectors/output.bin"
}

exit
