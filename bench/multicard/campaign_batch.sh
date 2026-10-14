#!/bin/bash
# Measurement campaign of the batch kernel on four cards, run inside tmux after a hw build.
# Author: Nasir Ali, C-DAC Noida.
#   1. routed timing must be met (worst negative slack >= 0), else stop
#   2. bit exact check of every instance of every card, then 1, 2 and 4 card throughput (run_batch.py)
#   3. tier 4 and tier 5 against Stim on card 0 (validate_batch.py)
#   4. CPU Stim baseline with the cards idle, 3 repetitions per point (cpu_stim_parallel.py)
# usage: campaign_batch.sh [build_name]   (build/batch/<build_name>, default hw_mem)
set -uo pipefail
R=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PY=$R/../envs/stim/bin/python
source /opt/xilinx/xrt/setup.sh > /dev/null
B=$R/build/batch/${1:-hw_mem}
X=$B/stim_batch_sampler.xclbin
T=$(find $B/_x -name "*timing_summary_routed.rpt" 2>/dev/null | head -1)
echo "== $(date '+%F %T') timing: $T"
WNS=$(awk '/WNS\(ns\)/{getline; getline; print $1; exit}' "$T")
echo "WNS $WNS ns"
MHZ=$(xclbinutil --info --input $X 2>/dev/null | awk '/Name:/{n=$2} /Type:/{t=$2} /Frequency:/{if (t=="DATA") {print $2; exit}}')
echo "kernel clock in the xclbin: $MHZ MHz"
# negative slack at the 300 MHz target is acceptable only if v++ scaled the kernel clock down to meet timing
awk -v w="$WNS" -v f="$MHZ" 'BEGIN{exit !(w+0 >= 0 || (f+0 > 0 && f+0 < 300))}' \
  || { echo "timing not met and clock not scaled; stopping"; exit 1; }
sha256sum $X
echo "== $(date '+%F %T') check and throughput"
$PY $R/bench/multicard/run_batch.py --xclbin $X || { echo "run_batch failed"; exit 1; }
echo "== $(date '+%F %T') validation against Stim"
$PY $R/bench/multicard/validate_batch.py --xclbin $X || echo "validation reported a failure"
echo "== $(date '+%F %T') CPU baseline (cards idle)"
$PY $R/bench/multicard/cpu_stim_parallel.py --procs 1,16,64,128,256 --reps 3 \
    --out $R/bench/multicard/cpu_stim_parallel.json
echo "== $(date '+%F %T') CAMPAIGN_DONE"
