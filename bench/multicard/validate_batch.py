"""Batch kernel on hardware against Stim: Tier 4 (detector and observable firing rates) and Tier 5 (logical error rate
through PyMatching), as in README "Validation strategy" for the first kernel.

Author: Nasir Ali, C-DAC Noida.

Samples are taken on one card with all of its kernel instances (build/batch/xrt_batch, dump mode), and compared with
CPU Stim samples of the same circuit:
  tier4: the three benchmark circuits, 10**7 shots per side; per output two proportion z score, pass if max |z| < 5.
  tier5: rotated surface code memory Z, d = 3, 5, 7 (rounds = d), p = 0.001, 0.003, 0.01, 10**6 shots per point per
         side; both sides decoded by the same PyMatching matcher from the same detector error model; pass if the
         logical error rates agree within |z| < 5 at every point.
usage: validate_batch.py --xclbin build/batch/hw/stim_batch_sampler.xclbin [--device 0] [--tiers 4,5] [--out f.json]
"""
import argparse, json, math, re, subprocess, sys, tempfile, time
from pathlib import Path

import numpy as np
import pymatching
import stim

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "python"), str(ROOT / "kernel"), str(ROOT / "bench")]
from isa_batch import LANES, compile_batch  # noqa: E402
from run_benchmark import _CIRCUITS as CIRCUITS  # noqa: E402

HOST = ROOT / "build" / "batch" / "xrt_batch"
BATCHES = 2048


def fpga_sample(xclbin, bp, shots, device, seed_hi, d):
    """Returns the kernel output as (batches, num_out) uint64 words (LANES = 64), at least `shots` shots."""
    ins, fold, dump = Path(d) / "i.bin", Path(d) / "f.bin", Path(d) / "dump.bin"
    ins.write_bytes(bp.instructions.astype("<u8").tobytes())
    fold.write_bytes(bp.fold.astype("<u4").tobytes())
    probe = subprocess.run([str(HOST), str(xclbin), str(ins), str(fold), str(bp.num_meas), str(bp.num_out), "1", "1",
                            str(device), "1", str(seed_hi)], capture_output=True, text=True, check=True)
    n_inst = int(re.search(r"instances (\d+)", probe.stderr).group(1))
    launches = math.ceil(shots / (BATCHES * LANES * n_inst))
    subprocess.run([str(HOST), str(xclbin), str(ins), str(fold), str(bp.num_meas), str(bp.num_out), str(BATCHES),
                    str(launches), str(device), "20261013", str(seed_hi), str(dump)], capture_output=True, text=True,
                   check=True)
    raw = np.frombuffer(dump.read_bytes(), dtype="<u8")
    assert LANES == 64
    return raw.reshape(-1, bp.num_out)


def popcounts(words):
    return np.bitwise_count(words).sum(axis=0, dtype=np.int64)


def shot_major(words):
    """(batches, num_out) uint64 -> (batches * 64, num_out) bool, shot b * 64 + l."""
    bits = (words[:, :, None] >> np.arange(64, dtype=np.uint64)) & np.uint64(1)
    return bits.transpose(0, 2, 1).reshape(-1, words.shape[1]).astype(bool)


def z2(k1, n1, k2, n2):
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return 0.0 if se == 0 else (k1 / n1 - k2 / n2) / se


def tier4(xclbin, device, d, rec):
    ok_all = True
    for name, make in CIRCUITS.items():
        c = make()
        bp = compile_batch(c)
        words = fpga_sample(xclbin, bp, 10_000_000, device, 41, d)
        n_f = words.shape[0] * 64
        k_f = popcounts(words)
        sampler = c.compile_detector_sampler(seed=20261013)
        k_s = np.zeros(bp.num_out, dtype=np.int64)
        n_s = 0
        while n_s < 10_000_000:
            det, obs = sampler.sample(1_000_000, separate_observables=True)
            k_s += np.concatenate([det.sum(axis=0), obs.sum(axis=0)])
            n_s += 1_000_000
        zs = [z2(int(a), n_f, int(b), n_s) for a, b in zip(k_f, k_s)]
        zmax = max(abs(z) for z in zs)
        ok = zmax < 5.0
        ok_all &= ok
        rec["tier4"].append(dict(circuit=name, fpga_shots=n_f, stim_shots=n_s, max_abs_z=zmax, passed=ok))
        print(f"tier4 {name:20s} FPGA {n_f:,} shots, Stim {n_s:,}: max|z| {zmax:.2f} {'PASS' if ok else 'FAIL'}",
              flush=True)
    return ok_all


def tier5(xclbin, device, d, rec):
    ok_all = True
    for dist in (3, 5, 7):
        for p in (0.001, 0.003, 0.01):
            c = stim.Circuit.generated("surface_code:rotated_memory_z", rounds=dist, distance=dist,
                                       before_round_data_depolarization=p, before_measure_flip_probability=p,
                                       after_clifford_depolarization=p, after_reset_flip_probability=p)
            matcher = pymatching.Matching.from_detector_error_model(c.detector_error_model(decompose_errors=True))
            bp = compile_batch(c)
            words = fpga_sample(xclbin, bp, 1_000_000, device, 51, d)
            bits = shot_major(words)
            det, obs = bits[:, :bp.num_detectors], bits[:, bp.num_detectors:]
            k_f = int(np.any(matcher.decode_batch(det) != obs, axis=1).sum())
            n_f = bits.shape[0]
            sdet, sobs = c.compile_detector_sampler(seed=7).sample(n_f, separate_observables=True)
            k_s = int(np.any(matcher.decode_batch(sdet) != sobs, axis=1).sum())
            z = z2(k_f, n_f, k_s, n_f)
            ok = abs(z) < 5.0
            ok_all &= ok
            rec["tier5"].append(dict(distance=dist, p=p, shots=n_f, fpga_logical_errors=k_f, stim_logical_errors=k_s,
                                     z=z, passed=ok))
            print(f"tier5 d={dist} p={p}: FPGA {k_f}/{n_f} = {k_f / n_f:.2e}, Stim {k_s}/{n_f} = {k_s / n_f:.2e}, "
                  f"z {z:+.2f} {'PASS' if ok else 'FAIL'}", flush=True)
    return ok_all


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xclbin", required=True)
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--tiers", default="4,5")
    ap.add_argument("--out", default=str(ROOT / "bench" / "multicard" / f"validate_{time.strftime('%Y%m%d_%H%M')}.json"))
    a = ap.parse_args()
    xclbin = Path(a.xclbin).resolve()
    sha = subprocess.run(["sha256sum", str(xclbin)], capture_output=True, text=True).stdout.split()[0]
    rec = dict(start=time.strftime("%Y-%m-%d %H:%M:%S"), stim=stim.__version__, pymatching=pymatching.__version__,
               xclbin_sha256=sha, device=a.device, tier4=[], tier5=[])
    ok = True
    with tempfile.TemporaryDirectory() as d:
        if "4" in a.tiers:
            ok &= tier4(xclbin, a.device, d, rec)
        if "5" in a.tiers:
            ok &= tier5(xclbin, a.device, d, rec)
    rec["end"], rec["passed"] = time.strftime("%Y-%m-%d %H:%M:%S"), bool(ok)
    json.dump(rec, open(a.out, "w"), indent=1)
    print(f"{'PASS' if ok else 'FAIL'}; wrote {a.out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
