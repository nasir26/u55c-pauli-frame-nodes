#!/bin/bash
# v++ build of the batch kernel (kernel/batch/stim_batch_sampler.cpp) and its host runtime (host/xrt_batch.cpp).
# Author: Nasir Ali, C-DAC Noida.
# usage: batch_build.sh <hw_emu|hw> [clock_mhz] [instances] [out_name]     output in build/batch/<out_name>/
#        (out_name defaults to the mode; a separate name lets two builds run side by side)
# instances (1 to 3, default 2): kernel instances, instance n in SLR n - 1 with its own two HBM pseudo channels.
# PLACE_DIRECTIVE (environment, default ExtraTimingOpt): Vivado placement directive, e.g. AltSpreadLogic_high
# against routing congestion. SLRS (environment, default "0 1 2"): the SLR of instance 1, 2, 3 in order; SLR0 also
# holds the platform's HBM and DMA logic, so "1 2" keeps two instances out of it.
# A hw build is a multi hour Vivado run: start it deliberately (see README "Phased plan").
set -euo pipefail
MODE="${1:?usage: batch_build.sh <hw_emu|hw> [clock_mhz]}"
MHZ="${2:-300}"
NCU="${3:-2}"
case "$NCU" in 1|2|3) ;; *) echo "error: instances must be 1, 2 or 3" >&2; exit 1 ;; esac
case "$MODE" in hw_emu|hw) ;; *) echo "error: mode must be hw_emu or hw" >&2; exit 1 ;; esac
PLATFORM="xilinx_u55c_gen3x16_xdma_3_202210_1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$SCRIPT_DIR/batch/${4:-$MODE}"
mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

# per instance: program and fold list on one HBM pseudo channel (read once per launch), the output on the next
{
  echo "[connectivity]"
  echo "nk=stim_batch_sampler:$NCU:$(seq -s . -f 'batch_%g' 1 $NCU)"
  for n in $(seq 1 $NCU); do
    echo "sp=batch_$n.prog:HBM[$((2 * n - 2))]"
    echo "sp=batch_$n.fold:HBM[$((2 * n - 2))]"
    echo "sp=batch_$n.out:HBM[$((2 * n - 1))]"
    echo "slr=batch_$n:SLR$(echo ${SLRS:-0 1 2} | cut -d' ' -f$n)"
  done
  echo
} > connectivity.cfg
cat >> connectivity.cfg <<CFG
[vivado]
prop=run.impl_1.STEPS.OPT_DESIGN.ARGS.DIRECTIVE=Explore
prop=run.impl_1.STEPS.PLACE_DESIGN.ARGS.DIRECTIVE=${PLACE_DIRECTIVE:-ExtraTimingOpt}
prop=run.impl_1.STEPS.PHYS_OPT_DESIGN.IS_ENABLED=true
prop=run.impl_1.STEPS.PHYS_OPT_DESIGN.ARGS.DIRECTIVE=AggressiveExplore
prop=run.impl_1.STEPS.ROUTE_DESIGN.ARGS.DIRECTIVE=NoTimingRelaxation
prop=run.impl_1.STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED=true
prop=run.impl_1.STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE=AggressiveExplore
CFG

echo "== [$MODE] v++ compile at $MHZ MHz =="
# --kernel_frequency on both -c and -l: on -c it only constrains HLS; the link sets the kernel clock (vpp_build.sh)
v++ -c -t "$MODE" --platform "$PLATFORM" -k stim_batch_sampler -I"$REPO_ROOT/kernel/batch" \
    --kernel_frequency "$MHZ" -o stim_batch_sampler.xo "$REPO_ROOT/kernel/batch/stim_batch_sampler.cpp"
echo "== [$MODE] v++ link =="
v++ -l -t "$MODE" --platform "$PLATFORM" --config connectivity.cfg --kernel_frequency "$MHZ" \
    -o stim_batch_sampler.xclbin stim_batch_sampler.xo
sha256sum stim_batch_sampler.xclbin > stim_batch_sampler.xclbin.sha256

echo "== host runtime =="
g++ -std=c++17 -O2 -I"$XILINX_XRT/include" -o "$SCRIPT_DIR/batch/xrt_batch" "$REPO_ROOT/host/xrt_batch.cpp" \
    -L"$XILINX_XRT/lib" -lxrt_coreutil -luuid -pthread
if [ "$MODE" = hw_emu ]; then emconfigutil --platform "$PLATFORM" --nd 1 --od "$OUT_DIR"; fi
echo "== [$MODE] build complete: $OUT_DIR =="
