#!/usr/bin/env bash
# stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
# Author: Nasir Ali, C-DAC Noida
#
# Builds the kernel .xo -> .xclbin and the host runtime for one v++
# target (sw_emu, hw_emu, or hw), plus emconfig.json for the emulation
# targets. Called from build/Makefile's sw_emu/hw_emu/hw targets, not
# meant to be the only way to invoke this -- run directly for more
# visibility into which step is slow/failing:
#   ./vpp_build.sh sw_emu
#
# Assumes Vitis/XRT are already sourced (this repo doesn't assume a
# specific install layout beyond what Phase 0's environment survey found
# on the build machine -- see ../README.md "Environment").
set -euo pipefail

MODE="${1:?usage: vpp_build.sh <sw_emu|hw_emu|hw>}"
case "$MODE" in
    sw_emu|hw_emu|hw) ;;
    *) echo "error: unknown mode '$MODE' (want sw_emu, hw_emu, or hw)" >&2; exit 1 ;;
esac

PLATFORM="xilinx_u55c_gen3x16_xdma_3_202210_1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
KERNEL_DIR="$REPO_ROOT/kernel"
HOST_DIR="$REPO_ROOT/host"
OUT_DIR="$SCRIPT_DIR/$MODE"
mkdir -p "$OUT_DIR"

echo "== [$MODE] regenerating connectivity.cfg =="
python3 "$SCRIPT_DIR/generate_connectivity.py"

echo "== [$MODE] v++ compile (kernel -> .xo) =="
# 250 MHz, not the 300 MHz originally targeted: a real `hw` build at 300
# missed timing closure by -0.688ns (WNS), ~249 MHz achievable -- see
# ../docs/utilization.md's 2026-08-31 entry for the diagnosed root cause
# (a routing-dominated reset-fanout path, not a logic/resource problem).
# 250 MHz is this project's own stated floor, not an arbitrary retreat,
# and gives Vivado 20% more slack than the 300 MHz attempt had to find a
# route that actually closes. Must match generate_connectivity.py's
# _CLOCK_HZ, which feeds the [hls] clock= line v++ -l reads.
v++ -c -t "$MODE" --platform "$PLATFORM" \
    -k stim_frame_sampler \
    -I"$KERNEL_DIR" -D STIM_U55C_USE_XILINX_AP_INT \
    --kernel_frequency 250 \
    -o "$OUT_DIR/stim_frame_sampler.xo" \
    "$KERNEL_DIR/stim_frame_sampler.cpp"

echo "== [$MODE] v++ link (.xo -> .xclbin) =="
# --kernel_frequency here too, not just on -c above: it's documented as
# an "[impl]" flag, i.e. it's the link/implementation stage that actually
# configures the platform's clock wizard for the kernel's clock domain.
# The -c copy alone only affects HLS's own internal synthesis constraint
# during kernel compilation -- discovered the hard way when a real hw
# build "retargeted" to 250 MHz still showed a 3.333ns (300 MHz) period
# on clk_kernel_00_unbuffered_net in the routed timing report; the
# connectivity.cfg [hls] clock= line alone wasn't sufficient either.
v++ -l -t "$MODE" --platform "$PLATFORM" \
    --config "$SCRIPT_DIR/connectivity.cfg" \
    --kernel_frequency 250 \
    -o "$OUT_DIR/stim_frame_sampler.xclbin" \
    "$OUT_DIR/stim_frame_sampler.xo"

echo "== [$MODE] host runtime (g++) =="
# Source file before -l flags: GNU ld only pulls symbols from a library
# for undefined references seen *before* it on the command line, so
# -lxrt_coreutil has to come after xrt_runner.cpp, not before.
g++ -std=c++17 -O2 \
    -I"$KERNEL_DIR" -I"$XILINX_XRT/include" \
    -o "$OUT_DIR/xrt_runner" \
    "$HOST_DIR/xrt_runner.cpp" \
    -L"$XILINX_XRT/lib" -lxrt_coreutil -luuid

if [ "$MODE" != "hw" ]; then
    echo "== [$MODE] emconfig.json =="
    emconfigutil --platform "$PLATFORM" --nd 1 --od "$OUT_DIR"
fi

echo "== [$MODE] build complete: $OUT_DIR =="
