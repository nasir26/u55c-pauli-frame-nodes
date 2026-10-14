"""Test vectors for RTL cosimulation of the batch kernel (run_hls.tcl with STIM_BATCH_COSIM=1).

Author: Nasir Ali, C-DAC Noida.

Writes test_vectors/{instructions.bin, fold.bin, args.txt, expected.bin}; after cosimulation, check with
`python3 make_test_vectors.py --check` (compares test_vectors/output.bin with expected.bin).
usage: make_test_vectors.py [--circuit surface_code_d3] [--batches 3] [--check]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "python"), str(ROOT / "kernel"), str(ROOT / "bench")]
from isa_batch import compile_batch  # noqa: E402
from softmodel.batch_replay import run_batches  # noqa: E402
from run_benchmark import _CIRCUITS  # noqa: E402

V = Path(__file__).resolve().parent / "test_vectors"
SEED_LO, SEED_HI, BATCH_BASE = 305419896, 2271560481, 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--circuit", default="surface_code_d3")
    ap.add_argument("--batches", type=int, default=3)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        got, want = (V / "output.bin").read_bytes(), (V / "expected.bin").read_bytes()
        ok = got == want
        print(f"cosimulation output {'matches' if ok else 'DIFFERS FROM'} softmodel/batch_replay.py "
              f"({len(got)} bytes)")
        sys.exit(0 if ok else 1)
    V.mkdir(exist_ok=True)
    bp = compile_batch(_CIRCUITS[a.circuit]())
    (V / "instructions.bin").write_bytes(bp.instructions.astype("<u8").tobytes())
    (V / "fold.bin").write_bytes(bp.fold.astype("<u4").tobytes())
    (V / "args.txt").write_text(f"{bp.num_meas} {bp.num_out} {a.batches} {BATCH_BASE} {SEED_LO} {SEED_HI}\n")
    _, _, raw = run_batches(bp, num_batches=a.batches, batch_base=BATCH_BASE, seed_lo=SEED_LO, seed_hi=SEED_HI)
    (V / "expected.bin").write_bytes(raw.astype("<u8").tobytes())
    print(f"wrote {V}: {a.circuit}, {len(bp.instructions)} instructions, {a.batches} batches")


if __name__ == "__main__":
    main()
