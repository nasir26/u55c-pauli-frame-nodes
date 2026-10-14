# Vitis HLS C synthesis (and optional cosimulation) of the batch kernel.
# Author: Nasir Ali, C-DAC Noida.
# usage: vitis_hls -f run_hls.tcl              (from kernel/batch/hls)
#        STIM_BATCH_COSIM=1 vitis_hls -f run_hls.tcl   (needs test_vectors/, see tests/test_batch_kernel.py)
set PART "xcu55c-fsvh2892-2L-e"
set CLOCK_PERIOD_NS [expr {[info exists ::env(STIM_BATCH_CLOCK_NS)] ? $::env(STIM_BATCH_CLOCK_NS) : 3.33}]
set INVOCATION_DIR [pwd]

open_project -reset stim_batch_sampler_hls
set_top stim_batch_sampler
add_files ../stim_batch_sampler.cpp -cflags "-std=c++17 -I.."
if {[info exists ::env(STIM_BATCH_COSIM)]} {
    add_files -tb ../testbench/tb_stim_batch_sampler.cpp -cflags "-std=c++17 -I.."
}
open_solution -reset "solution1" -flow_target vitis
set_part $PART
create_clock -period $CLOCK_PERIOD_NS -name default
csynth_design
if {[info exists ::env(STIM_BATCH_COSIM)]} {
    set V $INVOCATION_DIR/test_vectors
    cosim_design -trace_level none -argv "$V/instructions.bin $V/fold.bin [exec cat $V/args.txt] $V/output.bin"
}
exit
