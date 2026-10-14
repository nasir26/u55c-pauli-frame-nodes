#!/usr/bin/env bash
# stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
# Author: Nasir Ali, C-DAC Noida
#
# Runs a build produced by vpp_build.sh (sw_emu/hw_emu/hw) against the
# same fixed test vector kernel/hls/ cosimulation uses
# (kernel/hls/test_vectors/, regenerated here if missing -- see
# kernel/hls/generate_test_vector.py) and diffs the result against
# softmodel/kernel_replay.py bit-for-bit, the same comparison
# tests/test_kernel_tier2.py makes for the C-sim build. Same seed
# (0x12345678 / 0x87654321) as kernel/hls/run_hls.tcl's cosim step, so
# results are directly comparable across C-sim / RTL cosim / this.
set -euo pipefail

MODE="${1:?usage: run_and_validate.sh <sw_emu|hw_emu|hw>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$SCRIPT_DIR/$MODE"
VECTORS_DIR="$REPO_ROOT/kernel/hls/test_vectors"
SEED_LO=305419896
SEED_HI=2271560481

if [ ! -f "$OUT_DIR/stim_frame_sampler.xclbin" ] || [ ! -f "$OUT_DIR/xrt_runner" ]; then
    echo "error: $OUT_DIR is missing a built xclbin/xrt_runner -- run 'make $MODE' first" >&2
    exit 1
fi

if [ ! -f "$VECTORS_DIR/instructions.bin" ]; then
    echo "== generating test vector =="
    python3 "$REPO_ROOT/kernel/hls/generate_test_vector.py"
fi

echo "== [$MODE] running xrt_runner =="
if [ "$MODE" != "hw" ]; then
    export XCL_EMULATION_MODE="$MODE"
fi
( cd "$OUT_DIR" && ./xrt_runner stim_frame_sampler.xclbin "$VECTORS_DIR/instructions.bin" \
    "$VECTORS_DIR/layer_offsets.bin" "$SEED_LO" "$SEED_HI" "$OUT_DIR/output.bin" )

echo "== comparing against softmodel/kernel_replay.py =="
PYTHONPATH="$REPO_ROOT:$REPO_ROOT/python:$REPO_ROOT/kernel" python3 - "$MODE" "$OUT_DIR/output.bin" <<'EOF'
import struct
import sys
import numpy as np
import stim
from isa import encode_circuit
from softmodel.kernel_replay import run_program
from stim_u55c.config import NUM_DETECTORS_MAX, NUM_OBSERVABLES_MAX, SHOTS

mode, output_path = sys.argv[1], sys.argv[2]
circuit = stim.Circuit.generated(
    "repetition_code:memory", rounds=3, distance=3,
    before_round_data_depolarization=0.05, before_measure_flip_probability=0.05,
)
program = encode_circuit(circuit)
seed = (2271560481 << 32) | 305419896
expected = run_program(program, shots=SHOTS, seed=seed)


def pack(bits):
    n = bits.shape[1]
    words = np.zeros(n, dtype=np.uint64)
    for s in range(bits.shape[0]):
        words |= bits[s].astype(np.uint64) << np.uint64(s)
    return words


exp_det = np.pad(pack(expected.detectors), (0, NUM_DETECTORS_MAX - expected.detectors.shape[1]))
exp_obs = np.pad(pack(expected.observables), (0, NUM_OBSERVABLES_MAX - expected.observables.shape[1]))

raw = open(output_path, "rb").read()
words = struct.unpack(f"<{NUM_DETECTORS_MAX + NUM_OBSERVABLES_MAX}Q", raw)
got_det = np.array(words[:NUM_DETECTORS_MAX], dtype=np.uint64)
got_obs = np.array(words[NUM_DETECTORS_MAX:], dtype=np.uint64)

ok = np.array_equal(got_det, exp_det) and np.array_equal(got_obs, exp_obs)
print(f"[{mode}] bit-exact vs. softmodel.kernel_replay: {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
EOF
