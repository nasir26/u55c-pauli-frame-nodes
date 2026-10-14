open_checkpoint <REPO>/build/batch/hw/_x/link/vivado/vpl/prj/prj.runs/impl_1/level0_wrapper_routed_error.dcp
report_design_analysis -congestion -file <REPO>/build/batch/hw/congestion/congestion.rpt
report_design_analysis -complexity -hierarchical_depth 4 -file <REPO>/build/batch/hw/congestion/complexity.rpt
report_utilization -hierarchical -hierarchical_depth 5 -file <REPO>/build/batch/hw/congestion/util_hier.rpt
exit
