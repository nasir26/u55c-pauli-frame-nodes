# docs/

`utilization.md` — kernel resource/timing numbers, appended to (not
overwritten) after each `vitis_hls` synthesis run or `v++ hw` build, so
regressions are visible in git history. As of Phase 3 these are HLS
C-synthesis estimates (`kernel/hls/run_hls.tcl`); real post-place-and-route
numbers come from a full `v++ hw` build (Phase 4, needs `host/`).
