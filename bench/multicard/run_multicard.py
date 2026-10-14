"""Mode A on one, two and four Alveo U55C cards: per card correctness, then aggregate shots/sec.

Author: Nasir Ali, C-DAC Noida.

Shots are independent, so the cards share no data: card i runs the same instruction stream with seed_hi = i + 1, the
high word of the Philox key, which gives it its own noise stream (seed_lo is the run index, as on one card).

1. check: on every card and for every benchmark circuit, build/hw/xrt_runner output must equal
   softmodel/kernel_replay.py bit for bit for that card's seed. Two sanity checks on the streams: the same seed on two
   cards gives identical output (no card dependence), different streams give different output.
2. bench: for C = 1, 2, 4 cards, one build/hw/xrt_bench process per card. Every process loads its card and uploads the
   program, reports "ready", and waits for a go file; the timed loops then start together. Aggregate rate =
   (shots of all cards) / (latest end - earliest start) of the timed loops, so slow starts count against it. FPGA and
   HBM temperatures of every card are read before and after each configuration.

usage: run_multicard.py [--cards 0,1,2,3] [--counts 1,2,4] [--seconds 10] [--reps 3] [--skip-check] [--out file.json]
"""
import argparse, json, os, re, struct, subprocess, sys, tempfile, time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "python"), str(ROOT / "kernel"), str(ROOT / "bench")]
import stim  # noqa: E402
from isa import encode_circuit  # noqa: E402
from softmodel.kernel_replay import run_program  # noqa: E402
from stim_u55c.config import NUM_DETECTORS_MAX, NUM_OBSERVABLES_MAX, SHOTS  # noqa: E402
from run_benchmark import _CIRCUITS as CIRCUITS  # noqa: E402

HW = ROOT / "build" / "hw"
XCLBIN, RUNNER, BENCH = HW / "stim_frame_sampler.xclbin", HW / "xrt_runner", HW / "xrt_bench"
# single card rates of bench/results/2026-09-01-mode-a-throughput.md, used only to size the repeat counts
EST_RATE = {"repetition_code_d3": 865_117, "surface_code_d3": 160_104, "surface_code_d5": 33_587}


def write_program(name, d):
    prog = encode_circuit(CIRCUITS[name]())
    ins, lay = Path(d) / f"{name}_instructions.bin", Path(d) / f"{name}_layer_offsets.bin"
    ins.write_bytes(prog.serialize())
    lay.write_bytes(prog.serialize_layer_offsets())
    return prog, ins, lay


def pack(bits):
    words = np.zeros(bits.shape[1], dtype=np.uint64)
    for s in range(bits.shape[0]):
        words |= bits[s].astype(np.uint64) << np.uint64(s)
    return words


def expected_words(prog, seed_lo, seed_hi):
    e = run_program(prog, shots=SHOTS, seed=(seed_hi << 32) | seed_lo)
    det = np.pad(pack(e.detectors), (0, NUM_DETECTORS_MAX - e.detectors.shape[1]))
    obs = np.pad(pack(e.observables), (0, NUM_OBSERVABLES_MAX - e.observables.shape[1]))
    return np.concatenate([det, obs])


def run_runner(ins, lay, seed_lo, seed_hi, dev, out):
    subprocess.run([str(RUNNER), str(XCLBIN), str(ins), str(lay), str(seed_lo), str(seed_hi), str(out), str(dev)],
                   check=True, capture_output=True, text=True)
    raw = Path(out).read_bytes()
    return np.array(struct.unpack(f"<{NUM_DETECTORS_MAX + NUM_OBSERVABLES_MAX}Q", raw), dtype=np.uint64)


def temps():
    """FPGA and HBM temperature of every card, from xbutil (read only)."""
    out = {}
    lst = subprocess.run(["xbutil", "examine"], capture_output=True, text=True).stdout
    for bdf in re.findall(r"\[([0-9a-f:.]+)\]", lst):
        r = subprocess.run(["xbutil", "examine", "-d", bdf, "-r", "thermal"], capture_output=True, text=True).stdout
        f = re.search(r"^\s*FPGA\s+(\d+)", r, re.M)
        h = re.search(r"^\s*FPGA HBM\s+(\d+)", r, re.M)
        out[bdf] = dict(fpga_c=int(f.group(1)) if f else None, hbm_c=int(h.group(1)) if h else None)
    return out


def check(cards, progs, d, rec):
    for name, (prog, ins, lay) in progs.items():
        for dev in cards:
            seed_lo, seed_hi = 12345, dev + 1
            got = run_runner(ins, lay, seed_lo, seed_hi, dev, Path(d) / "out.bin")
            ok = bool(np.array_equal(got, expected_words(prog, seed_lo, seed_hi)))
            rec["check"].append(dict(circuit=name, device=dev, seed_lo=seed_lo, seed_hi=seed_hi, bit_exact=ok))
            print(f"check {name:20s} device {dev} seed_hi {seed_hi}: {'PASS' if ok else 'FAIL'}", flush=True)
        if len(cards) > 1:
            a = run_runner(ins, lay, 777, 9, cards[0], Path(d) / "a.bin")
            b = run_runner(ins, lay, 777, 9, cards[-1], Path(d) / "b.bin")
            c = run_runner(ins, lay, 777, 10, cards[-1], Path(d) / "c.bin")
            rec["streams"].append(dict(circuit=name, same_seed_identical=bool(np.array_equal(a, b)),
                                       different_stream_differs=not np.array_equal(b, c)))
            print(f"streams {name:20s} same seed identical across cards: {np.array_equal(a, b)}; "
                  f"different stream differs: {not np.array_equal(b, c)}", flush=True)


def bench_once(name, ins, lay, devs, repeat, d):
    go = Path(d) / "go"
    go.unlink(missing_ok=True)
    env = dict(os.environ, STIM_GO_FILE=str(go))
    ps = [subprocess.Popen([str(BENCH), str(XCLBIN), str(ins), str(lay), str(repeat), str(dev), str(dev + 1)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for dev in devs]
    for p in ps:  # wait until every card is loaded and holds the program
        for line in p.stderr:
            if line.strip() == "ready":
                break
        else:
            raise RuntimeError(f"bench process ended before ready: {p.wait()}")
    go.touch()
    rows = []
    for dev, p in zip(devs, ps):
        rest = p.stderr.read()
        rate = float(p.stdout.read().strip())
        if p.wait() != 0:
            raise RuntimeError(rest)
        m = re.search(r"window_ns (\d+) (\d+) device (\d+) stream (\d+) bdf (\S+)", rest)
        rows.append(dict(device=dev, bdf=m.group(5), stream=int(m.group(4)), start_ns=int(m.group(1)),
                         end_ns=int(m.group(2)), card_shots_per_sec=rate))
    t0, t1 = min(r["start_ns"] for r in rows), max(r["end_ns"] for r in rows)
    agg = len(devs) * repeat * SHOTS / ((t1 - t0) * 1e-9)
    return dict(circuit=name, cards=len(devs), repeat=repeat, shots=len(devs) * repeat * SHOTS,
                window_s=(t1 - t0) * 1e-9, aggregate_shots_per_sec=agg, per_card=rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", default="0,1,2,3")
    ap.add_argument("--counts", default="1,2,4")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--skip-check", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "bench" / "multicard" / f"multicard_{time.strftime('%Y%m%d_%H%M')}.json"))
    a = ap.parse_args()
    for f in (XCLBIN, RUNNER, BENCH):
        if not f.exists():
            sys.exit(f"missing {f}")
    cards = [int(x) for x in a.cards.split(",")]
    rec = dict(start=time.strftime("%Y-%m-%d %H:%M:%S"), stim=stim.__version__, shots_per_launch=SHOTS,
               xclbin_sha256=subprocess.run(["sha256sum", str(XCLBIN)], capture_output=True, text=True).stdout.split()[0],
               git=subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
               git_dirty=bool(subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True,
                                             text=True).stdout.strip()),
               cards=cards, check=[], streams=[], bench=[], temps=[])
    with tempfile.TemporaryDirectory() as d:
        progs = {n: write_program(n, d) for n in CIRCUITS}
        if not a.skip_check:
            check(cards, progs, d, rec)
            if not all(c["bit_exact"] for c in rec["check"]):
                json.dump(rec, open(a.out, "w"), indent=1)
                sys.exit("correctness check failed; no benchmark run")
        for name, (prog, ins, lay) in progs.items():
            repeat = max(2, int(EST_RATE[name] * a.seconds / SHOTS))
            for c in map(int, a.counts.split(",")):
                rec["temps"].append(dict(when="before", circuit=name, cards=c, t=time.time(), temps=temps()))
                for r in range(a.reps):
                    row = bench_once(name, ins, lay, cards[:c], repeat, d)
                    row["rep"] = r
                    rec["bench"].append(row)
                    per = ", ".join(f"{x['card_shots_per_sec']:,.0f}" for x in row["per_card"])
                    print(f"bench {name:20s} cards {c} rep {r}: {row['aggregate_shots_per_sec']:14,.0f} shots/s "
                          f"(per card {per})", flush=True)
                rec["temps"].append(dict(when="after", circuit=name, cards=c, t=time.time(), temps=temps()))
                json.dump(rec, open(a.out, "w"), indent=1)
    rec["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(rec, open(a.out, "w"), indent=1)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
