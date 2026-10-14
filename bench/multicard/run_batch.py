"""Batch kernel on one, two and four Alveo U55C cards: per card bit exact check, then aggregate shots/sec.

Author: Nasir Ali, C-DAC Noida.

Card i samples with seed_hi = i + 1 (its own Philox key); launch k of a card samples batches k * B ... (k + 1) * B - 1.

1. check: on every card and for every circuit, 2 launches of 4 batches per kernel instance are dumped by
   build/batch/xrt_batch and must equal softmodel/batch_replay.py bit for bit (batch offsets of launches and
   instances included).
2. bench: for C = 1, 2, 4 cards, one xrt_batch process per card, started together behind a go file. Aggregate rate =
   (shots of all cards, copied back to host memory) / (latest end - earliest start). Temperatures before and after.

usage: run_batch.py --xclbin build/batch/hw/stim_batch_sampler.xclbin [--cards 0,1,2,3] [--counts 1,2,4]
                    [--seconds 10] [--batches 4096] [--reps 3] [--skip-check] [--out file.json]
"""
import argparse, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "python"), str(ROOT / "kernel"), str(ROOT / "bench")]
import stim  # noqa: E402
from isa_batch import LANES, compile_batch  # noqa: E402
from softmodel.batch_replay import run_batches  # noqa: E402
from run_benchmark import _CIRCUITS as CIRCUITS  # noqa: E402
from run_multicard import temps  # noqa: E402

HOST = ROOT / "build" / "batch" / "xrt_batch"


def write_inputs(bp, d, name):
    ins, fold = Path(d) / f"{name}.instr.bin", Path(d) / f"{name}.fold.bin"
    ins.write_bytes(bp.instructions.astype("<u8").tobytes())
    fold.write_bytes(bp.fold.astype("<u4").tobytes())
    return ins, fold


def host_cmd(xclbin, bp, ins, fold, batches, launches, dev, seed_lo, seed_hi, dump=None):
    c = [str(HOST), str(xclbin), str(ins), str(fold), str(bp.num_meas), str(bp.num_out), str(batches), str(launches),
         str(dev), str(seed_lo), str(seed_hi)]
    return c + ([str(dump)] if dump else [])


def check(xclbin, cards, progs, d, rec):
    """2 launches of 4 batches on every instance of every card, against the model (instance c of launch k samples
    batches c * 2**28 + 4 k onwards)."""
    for name, (bp, ins, fold) in progs.items():
        for dev in cards:
            dump = Path(d) / "dump.bin"
            r = subprocess.run(host_cmd(xclbin, bp, ins, fold, 4, 2, dev, 4242, dev + 1, dump), check=True,
                               capture_output=True, text=True)
            n_inst = int(re.search(r"instances (\d+)", r.stderr).group(1))
            got = np.frombuffer(dump.read_bytes(), dtype="<u8").reshape(2, n_inst, 4, bp.num_out, LANES // 64)
            ok = True
            for k in range(2):
                for c in range(n_inst):
                    _, _, want = run_batches(bp, num_batches=4, batch_base=(c << 28) + 4 * k, seed_lo=4242,
                                             seed_hi=dev + 1)
                    ok &= bool(np.array_equal(got[k, c], want))
            rec["check"].append(dict(circuit=name, device=dev, seed_hi=dev + 1, instances=n_inst, batches=16 * n_inst,
                                     bit_exact=ok))
            print(f"check {name:20s} device {dev} ({n_inst} instances): {'PASS' if ok else 'FAIL'}", flush=True)


def bench_once(xclbin, bp, ins, fold, devs, batches, launches, d):
    go = Path(d) / "go"
    go.unlink(missing_ok=True)
    env = dict(os.environ, STIM_GO_FILE=str(go))
    ps = [subprocess.Popen(host_cmd(xclbin, bp, ins, fold, batches, launches, dev, 777, dev + 1),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for dev in devs]
    n_inst = []
    for p in ps:
        for line in p.stderr:
            m = re.match(r"instances (\d+)", line)
            if m:
                n_inst.append(int(m.group(1)))
            if line.strip() == "ready":
                break
        else:
            raise RuntimeError(f"host process ended before ready: {p.wait()}")
    go.touch()
    rows = []
    for dev, p in zip(devs, ps):
        rest, rate = p.stderr.read(), p.stdout.read().strip()
        if p.wait() != 0:
            raise RuntimeError(rest)
        m = re.search(r"window_ns (\d+) (\d+) device (\d+) stream (\d+) bdf (\S+)", rest)
        rows.append(dict(device=dev, bdf=m.group(5), start_ns=int(m.group(1)), end_ns=int(m.group(2)),
                         card_shots_per_sec=float(rate)))
    t0, t1 = min(r["start_ns"] for r in rows), max(r["end_ns"] for r in rows)
    shots = sum(n_inst) * launches * batches * LANES
    return dict(cards=len(devs), instances_per_card=n_inst, batches=batches, launches=launches, shots=shots, window_s=(t1 - t0) * 1e-9,
                aggregate_shots_per_sec=shots / ((t1 - t0) * 1e-9), per_card=rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xclbin", required=True)
    ap.add_argument("--cards", default="0,1,2,3")
    ap.add_argument("--counts", default="1,2,4")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--batches", type=int, default=4096)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--clock-mhz", type=float, default=300.0, help="only to size the runs")
    ap.add_argument("--skip-check", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "bench" / "multicard" / f"batch_{time.strftime('%Y%m%d_%H%M')}.json"))
    a = ap.parse_args()
    xclbin = Path(a.xclbin).resolve()
    cards = [int(x) for x in a.cards.split(",")]
    sha = subprocess.run(["sha256sum", str(xclbin)], capture_output=True, text=True).stdout.split()[0]
    git = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True,
                                text=True).stdout.strip())
    rec = dict(start=time.strftime("%Y-%m-%d %H:%M:%S"), stim=stim.__version__, lanes=LANES, xclbin_sha256=sha,
               git=git, git_dirty=dirty, cards=cards, check=[], bench=[], temps=[])
    with tempfile.TemporaryDirectory() as d:
        progs = {}
        for n in CIRCUITS:
            bp = compile_batch(CIRCUITS[n]())
            progs[n] = (bp, *write_inputs(bp, d, n))
        if not a.skip_check:
            check(xclbin, cards, progs, d, rec)
            if not all(c["bit_exact"] for c in rec["check"]):
                json.dump(rec, open(a.out, "w"), indent=1)
                sys.exit("bit exact check failed; no benchmark run")
        for name, (bp, ins, fold) in progs.items():
            per_batch_s = max(len(bp.instructions), bp.num_meas + len(bp.fold), bp.num_out) / (a.clock_mhz * 1e6)
            launches = max(4, int(a.seconds / (per_batch_s * a.batches)))
            for c in map(int, a.counts.split(",")):
                rec["temps"].append(dict(when="before", circuit=name, cards=c, t=time.time(), temps=temps()))
                for r in range(a.reps):
                    row = bench_once(xclbin, bp, ins, fold, cards[:c], a.batches, launches, d)
                    row.update(circuit=name, rep=r, instructions=len(bp.instructions), fold_entries=len(bp.fold),
                               measurements=bp.num_meas, outputs=bp.num_out)
                    rec["bench"].append(row)
                    per = ", ".join(f"{x['card_shots_per_sec']:,.0f}" for x in row["per_card"])
                    print(f"bench {name:20s} cards {c} rep {r}: {row['aggregate_shots_per_sec']:16,.0f} shots/s "
                          f"(per card {per})", flush=True)
                rec["temps"].append(dict(when="after", circuit=name, cards=c, t=time.time(), temps=temps()))
                json.dump(rec, open(a.out, "w"), indent=1)
    rec["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(rec, open(a.out, "w"), indent=1)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
