"""CPU Stim baseline for the multi card study: detector sampling throughput with 1 to N worker processes.

Author: Nasir Ali, C-DAC Noida.

Same circuits and the same timing cut as bench/run_benchmark.py (timer starts after compile_detector_sampler()).
Each worker samples its share of the shots with its own seed; the aggregate rate is total shots / wall time of the
slowest worker, all workers started together behind a barrier.
Each point is repeated --reps times; the rows keep every repetition and the median is used by the paper.
usage: cpu_stim_parallel.py [--shots N] [--procs 1,16,64,256] [--reps 3] [--out results.json]
"""
import argparse, json, multiprocessing as mp, os, platform, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import stim
from run_benchmark import _CIRCUITS as CIRCUITS


def worker(name, shots, seed, barrier, q):
    sampler = CIRCUITS[name]().compile_detector_sampler(seed=seed)
    barrier.wait()
    t0 = time.perf_counter()
    sampler.sample(shots=shots, bit_packed=True)
    q.put(time.perf_counter() - t0)


def run(name, shots, procs):
    per = shots // procs
    barrier, q = mp.Barrier(procs), mp.Queue()
    ps = [mp.Process(target=worker, args=(name, per, 1000 + i, barrier, q)) for i in range(procs)]
    for p in ps:
        p.start()
    times = [q.get() for _ in ps]
    for p in ps:
        p.join()
    return per * procs / max(times)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=int, default=2_000_000)
    ap.add_argument("--procs", default="1,16,64,128,256")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rec = dict(stim=stim.__version__, host_cpus=os.cpu_count(), python=platform.python_version(), shots=a.shots,
               reps=a.reps, start=time.strftime("%Y-%m-%d %H:%M:%S"), rows=[])
    for name in CIRCUITS:
        for p in map(int, a.procs.split(",")):
            for k in range(a.reps):
                r = run(name, a.shots * p if p > 1 else a.shots, p)
                rec["rows"].append(dict(circuit=name, procs=p, rep=k, shots_per_sec=r))
                print(f"{name:22s} procs={p:4d} rep {k} {r:16,.0f} shots/s", flush=True)
    rec["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if a.out:
        json.dump(rec, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
