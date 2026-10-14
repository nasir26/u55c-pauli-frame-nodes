#!/usr/bin/env python3
"""Every measured or derived number of the manuscript, as LaTeX macros and tables in generated/.

Author: Nasir Ali, C-DAC Noida.

Sources (nothing is typed by hand):
  bench/multicard/cpu_stim_parallel.json      CPU Stim on the host, 1 to 256 processes
  kernel/isa_batch.py                         compiled program statistics of the benchmark circuits
  kernel/batch/hls/stim_batch_sampler_hls/    Vitis HLS synthesis and RTL cosimulation reports
  build/batch/hw/                             post route utilisation and timing reports, xclbin info
  bench/multicard/batch_*.json                one, two and four card throughput (latest complete file)
  bench/multicard/validate_*.json             tier 4 and tier 5 against Stim (latest complete file)
  /proc/cpuinfo, xbutil                       host platform
A missing source leaves \\PENDING{...} markers (refused by the final build).
usage: make_numbers.py
"""
import glob, json, os, re, statistics, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(PAPER, ".."))
GEN = os.path.join(PAPER, "generated")
sys.path[:0] = [REPO, os.path.join(REPO, "python"), os.path.join(REPO, "kernel"), os.path.join(REPO, "bench")]

M = {}
MISSING = []
BUILD = os.environ.get("STIM_BUILD", "hw_tf")   # build/batch/<BUILD>: the bitstream the measurements used
NAMES = {"repetition_code_d3": "Repetition code, $d=3$", "surface_code_d3": "Surface code, $d=3$",
         "surface_code_d5": "Surface code, $d=5$"}
SHORT = {"repetition_code_d3": "Rep", "surface_code_d3": "SurfThree", "surface_code_d5": "SurfFive"}


def pend(what):
    MISSING.append(what)
    return r"\PENDING{" + what + "}"


def fmt_int(x):
    return f"{int(round(x)):,}".replace(",", "{,}")


def fmt_rate(x):
    """shots/s with three significant digits and a unit word (thousand, million, billion)"""
    for div, unit in ((1e9, "billion"), (1e6, "million"), (1e3, "thousand")):
        if x >= div:
            v = x / div
            return f"{v:.3g}~{unit}"
    return f"{x:.3g}"


def fmt_x(x):
    return f"{x:.3g}" if x < 100 else f"{x:.0f}"


def latest(pattern, ok=lambda d: True):
    files = sorted(glob.glob(os.path.join(REPO, pattern)), key=os.path.getmtime, reverse=True)
    for f in files:
        try:
            d = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        if ok(d):
            return f, d
    return None, None


# ---------------------------------------------------------------- platform
def platform():
    cpu = open("/proc/cpuinfo").read()
    M["HostCpu"] = re.search(r"model name\s*:\s*(.+)", cpu).group(1).replace("64-Core Processor", "").strip()
    M["HostThreads"] = str(len(re.findall(r"^processor", cpu, re.M)))
    M["HostSockets"] = str(len(set(re.findall(r"physical id\s*:\s*(\d+)", cpu))))
    mem = int(re.search(r"MemTotal:\s*(\d+)", open("/proc/meminfo").read()).group(1))
    M["HostMemGiB"] = str(round(mem / 2**20))
    try:
        v = subprocess.run(["bash", "-lc", "source /opt/xilinx/xrt/setup.sh >/dev/null 2>&1; xbutil --version"],
                           capture_output=True, text=True).stdout
        M["XrtVersion"] = re.search(r"Version\s*:\s*(\S+)", v).group(1)
    except Exception:  # noqa: BLE001
        M["XrtVersion"] = pend("XRT version")


# ---------------------------------------------------------------- CPU Stim
def cpu():
    f = os.path.join(REPO, "bench", "multicard", "cpu_stim_parallel.json")
    if not os.path.exists(f):
        for n in SHORT.values():
            M[f"CpuOne{n}"] = M[f"CpuAll{n}"] = pend("CPU Stim")
        return {}
    d = json.load(open(f))
    M["StimVersion"] = d["stim"]
    reps = {}
    for r in d["rows"]:
        reps.setdefault(r["circuit"], {}).setdefault(r["procs"], []).append(r["shots_per_sec"])
    rows = {c: {p: statistics.median(v) for p, v in ps.items()} for c, ps in reps.items()}
    M["CpuReps"] = str(d.get("reps", 1))
    procs = sorted({p for v in rows.values() for p in v})
    M["CpuMaxProcs"] = str(max(procs))
    for c, n in SHORT.items():
        M[f"CpuOne{n}"] = fmt_rate(rows[c][1])
        M[f"CpuAll{n}"] = fmt_rate(rows[c][max(procs)])
    lines = [r"\begin{tabular}{@{}l" + "r" * len(procs) + "@{}}", r"\toprule",
             "Circuit & " + " & ".join(f"{p} proc." for p in procs) + r" \\", r"\midrule"]
    for c in SHORT:
        lines.append(NAMES[c] + " & " + " & ".join(fmt_int(rows[c][p]) for p in procs) + r" \\")
    lines += [r"\botrule", r"\end{tabular}"]
    write("tab_cpu.tex", lines)
    return rows


# ---------------------------------------------------------------- compiler
def compiler():
    from isa_batch import CFG, LANES, compile_batch
    from isa import Opcode
    from run_benchmark import _CIRCUITS
    M["Lanes"] = str(LANES)
    M["Pairs"] = str(CFG["PAIRS"])
    M["HazardDistance"] = str(CFG["HAZARD_DISTANCE"])
    M["QubitsMax"] = str(CFG["QUBITS_MAX"])
    M["ProgMax"] = fmt_int(CFG["PROG_MAX"])
    M["MeasMax"] = fmt_int(CFG["MEAS_MAX"])
    M["OutMax"] = str(CFG["OUT_MAX"])
    stats = {}
    from isa_batch import FIRST
    lines = [r"\begin{tabular}{@{}lrrrrrrrr@{}}", r"\toprule",
             r"Circuit & Qubits & Instr. & 2q & NOPs & Slots & Meas. & Out. & Fold \\",
             r"\midrule"]

    def row(name, bp):
        nops = sum(1 for i, _ in bp.slots if i.opcode == Opcode.NOP)
        twoq = sum(1 for _, k in bp.slots if k == FIRST)
        instr = sum(1 for i in bp.program.instructions if i.opcode != Opcode.NOP)
        lines.append(f"{name} & {bp.num_qubits} & {fmt_int(instr)} & {fmt_int(twoq)} & {fmt_int(nops)} & "
                     f"{fmt_int(len(bp.slots))} & {bp.num_meas} & {bp.num_out} & {len(bp.fold)} " + r"\\")
        return nops

    for c, n in SHORT.items():
        bp = compile_batch(_CIRCUITS[c]())
        nops = row(NAMES[c], bp)
        slots = len(bp.slots)
        stats[c] = dict(slots=slots, meas=bp.num_meas, fold=len(bp.fold), out=bp.num_out)
        M[f"Slots{n}"] = fmt_int(slots)
        M[f"Nops{n}"] = fmt_int(nops)
    # reuse distances of the repetition code program (the tightest schedule, used for RTL cosimulation)
    from isa_batch import SECOND
    from isa import NO_QUBIT
    bp = compile_batch(_CIRCUITS["repetition_code_d3"]())
    last, gaps = {}, []
    for k, (ins, kind) in enumerate(bp.slots):
        q = ins.qubit_b if kind == SECOND else ins.qubit_a
        if q != NO_QUBIT:
            if q in last:
                gaps.append(k - last[q])
            last[q] = k
    M["RepReuses"] = str(len(gaps))
    M["RepReusesAtD"] = str(sum(1 for g in gaps if g == CFG["HAZARD_DISTANCE"]))
    import stim
    c = stim.Circuit.generated("surface_code:rotated_memory_z", rounds=7, distance=7,
                               before_round_data_depolarization=0.001, before_measure_flip_probability=0.001,
                               after_clifford_depolarization=0.001, after_reset_flip_probability=0.001)
    bp = compile_batch(c)
    row("Surface code, $d=7$", bp)
    M["SlotsSevenQubits"] = str(bp.num_qubits)
    M["SlotsSeven"] = fmt_int(len(bp.slots))
    lines += [r"\botrule", r"\end{tabular}"]
    write("tab_programs.tex", lines)
    return stats


# ---------------------------------------------------------------- HLS and cosimulation
def hls():
    base = os.path.join(REPO, "kernel", "batch", "hls", "stim_batch_sampler_hls", "solution1")
    # the most recent synthesis of the current sources (csynth.log, or the synthesis step of a cosimulation run)
    logs = glob.glob(os.path.join(REPO, "kernel", "batch", "hls", "csynth.log")) + \
        glob.glob(os.path.join(REPO, "kernel", "batch", "hls", "cosim*.log"))
    logs = [f for f in logs if "Estimated Fmax" in open(f, errors="ignore").read()]
    log = max(logs, key=os.path.getmtime)
    try:
        s = open(log).read()
        M["HlsFmax"] = re.search(r"Estimated Fmax:\s*([\d.]+)", s).group(1).split(".")[0]
        loops = re.findall(r"Final II = (\d+), Depth = (\d+), loop '(\w+)'", s)
        d = {name: (int(ii), int(dep)) for ii, dep, name in loops}
        M["HlsAllIIOne"] = "1" if all(ii == 1 for ii, _ in d.values()) else pend("II")
        M["DepthNoise"] = str(d["NOISE"][1])
        M["DepthFrame"] = str(d["FRAME"][1])
        rpt = open(os.path.join(base, "syn", "report", "stim_batch_sampler_csynth.rpt")).read()
        tot = re.search(r"\|Total\s*\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|", rpt)
        slr = re.search(r"\|Utilization SLR \(%\)\s*\|\s*~?(\d+)\|\s*~?(\d+)\|\s*~?(\d+)\|\s*~?(\d+)\|", rpt)
        M["HlsDsp"], M["HlsLut"], M["HlsFf"] = fmt_int(int(tot.group(2))), fmt_int(int(tot.group(4))), fmt_int(int(tot.group(3)))
        M["HlsDspSlr"], M["HlsLutSlr"], M["HlsFfSlr"] = slr.group(2), slr.group(4), slr.group(3)
    except Exception as e:  # noqa: BLE001
        for k in ("HlsFmax", "HlsAllIIOne", "DepthNoise", "DepthFrame", "HlsDsp", "HlsLut", "HlsFf", "HlsDspSlr",
                  "HlsLutSlr", "HlsFfSlr"):
            M.setdefault(k, pend(f"HLS ({e})"))
    # RTL cosimulation of the distance 3 surface code with 4 and 12 batches (kernel/batch/hls/cosim_reports)
    cr = os.path.join(REPO, "kernel", "batch", "hls", "cosim_reports")
    try:
        cyc = {}
        for nb in (4, 12):
            rpt = open(os.path.join(cr, f"surface_code_d3_b{nb}.rpt")).read()
            cyc[nb] = int(re.search(r"Verilog\|\s*Pass\|\s*(\d+)", rpt).group(1))
        M["CosimBatchesA"], M["CosimBatchesB"] = "4", "12"
        M["CosimCyclesA"], M["CosimCyclesB"] = fmt_int(cyc[4]), fmt_int(cyc[12])
        M["CosimPerBatch"] = f"{(cyc[12] - cyc[4]) / 8:.1f}"
        from isa_batch import compile_batch
        from run_benchmark import _CIRCUITS
        M["CosimOverhead"] = f"{(cyc[12] - cyc[4]) / 8 - len(compile_batch(_CIRCUITS['surface_code_d3']()).slots):.1f}"
        M["CosimRepPass"] = "PASS" if "PASS" in open(os.path.join(cr, "repetition_code_d3_b6.txt")).read() else pend("cosim")
    except Exception:  # noqa: BLE001
        for k in ("CosimBatchesA", "CosimBatchesB", "CosimCyclesA", "CosimCyclesB", "CosimPerBatch"):
            M[k] = pend("cosimulation")


# ---------------------------------------------------------------- post route
def postroute():
    """Routed utilisation (kernel_util_routed: dynamic region and per compute unit), timing summary and kernel clock
    of the linked design in build/batch/hw."""
    hw = os.path.join(REPO, "build", "batch", BUILD)
    routed = {}
    try:
        util = sorted(glob.glob(os.path.join(hw, "_x", "**", "*kernel_util_routed.rpt"), recursive=True))[0]
        u = open(util).read()
        hdr = re.search(r"\|\s*Name\s*\|(.*)\|\s*\n", u).group(1)
        cols = [c.strip() for c in hdr.split("|")]
        def row(label):
            m = re.search(r"\|\s*" + re.escape(label) + r"\s*\|(.*)\|", u)
            return dict(zip(cols, [c.strip() for c in m.group(1).split("|")])) if m else None
        routed = dict(used=row("Used Resources"), one=row("batch_1"))
    except Exception:  # noqa: BLE001
        routed = {}
    try:
        t = sorted(glob.glob(os.path.join(hw, "_x", "**", "*timing_summary_routed.rpt"), recursive=True))[0]
        s = open(t).read()
        row = re.search(r"WNS\(ns\)\s+TNS\(ns\)\s+TNS Failing Endpoints.*?\n\s*-[-\s]+\n\s*([-\d.]+)\s+([-\d.]+)\s+(\d+)", s, re.S)
        M["Wns"], M["FailingEndpoints"] = row.group(1), row.group(3)
    except Exception:  # noqa: BLE001
        M["Wns"] = M["FailingEndpoints"] = pend("timing")
    try:
        info = open(glob.glob(os.path.join(hw, "*.xclbin.info"))[0]).read()
        mhz = re.findall(r"Name:\s*(\S+)\s*\n\s*Index:.*?\n\s*Type:\s*(\S+).*?\n\s*Frequency:\s*([\d.]+)", info)
        k = [f for n, typ, f in mhz if typ.upper() == "DATA"]
        M["KernelMhz"] = str(int(float(k[0]))) if k else pend("kernel clock")
    except Exception:  # noqa: BLE001
        M["KernelMhz"] = pend("kernel clock")
    lines = [r"\begin{tabular}{@{}lrrrr@{}}", r"\toprule",
             r"Resource & HLS, one instance & \% SLR & Routed, one instance & Routed, card (\% budget) \\",
             r"\midrule"]
    hls_vals = {"LUT": ("HlsLut", "HlsLutSlr"), "FF": ("HlsFf", "HlsFfSlr"), "DSP": ("HlsDsp", "HlsDspSlr")}
    rkey = {"LUT": "LUT", "FF": "REG", "DSP": "DSP"}

    def cell(r, res):
        m = re.match(r"(\d+)\s*\[\s*([\d.]+)%\]", r[rkey[res]])
        return fmt_int(int(m.group(1))), m.group(2)

    for res in ("LUT", "FF", "DSP"):
        h, hs = hls_vals[res]
        if routed.get("used") and routed.get("one"):
            one, _ = cell(routed["one"], res)
            card, pct = cell(routed["used"], res)
            r1, rc = one, f"{card} ({pct})"
            M[f"Routed{res.title()}Pct"] = pct
            M[f"Routed{res.title()}One"] = one
        else:
            r1 = rc = pend("routed utilisation")
        lines.append(f"{res} & {M[h] if h in M else pend('HLS')} & {M[hs] if hs in M else pend('HLS')} & {r1} & {rc} " + r"\\")
    lines += [r"\botrule", r"\end{tabular}"]
    write("tab_resources.tex", lines)


# ---------------------------------------------------------------- implementation attempts
BUILDS = [  # build directory, log name prefix, frame, generator, SLRs of the two instances, placement directive
    ("hw", "batch_build_hw_2026", "registers", "Philox", "0, 1", "timing"),
    ("hw_mem", "batch_build_hwmem_2026", "LUT RAM", "Philox", "0, 1", "timing"),
    ("hw_mem_spread", "batch_build_hwmem_spread_2026", "LUT RAM", "Philox", "0, 1", "spread"),
    ("hw_tf", "batch_build_hw_tf_2026", "LUT RAM", "Threefry", "1, 2", "timing"),
    ("hw_tf_spread", "batch_build_hw_tf_spread_2026", "LUT RAM", "Threefry", "1, 2", "spread"),
]


def builds():
    """One row per hardware build: configuration, DSP blocks per instance (HLS), implementation time and outcome,
    all read from the build directories and logs."""
    import datetime
    lines = [r"\begin{tabular}{@{}llllrrl@{}}", r"\toprule",
             r"Frame & Generator & SLRs & Placement & DSP & Time & Outcome \\", r"\midrule"]
    for d, prefix, frame, gen, slrs, place in BUILDS:
        base = os.path.join(REPO, "build", "batch", d)
        logs = sorted(glob.glob(os.path.join(REPO, "logs", prefix + "*.log")))
        if not os.path.isdir(base) or not logs:
            continue
        log = open(logs[-1], errors="ignore").read()
        try:
            rpt = open(os.path.join(base, "_x", "reports", "stim_batch_sampler", "hls_reports",
                                    "stim_batch_sampler_csynth.rpt")).read()
            dsp = fmt_int(int(re.search(r"\|Total\s*\|\s*\d+\|\s*(\d+)\|", rpt).group(1)))
        except Exception:  # noqa: BLE001
            dsp = pend("HLS report")
        t0 = re.search(r"\[(\d\d:\d\d:\d\d)\] Run vpl: Step impl: Started", log)
        t1 = re.search(r"\[(\d\d:\d\d:\d\d)\] Run vpl: FINISHED", log)
        if t0 and t1:
            a = datetime.datetime.strptime(t0.group(1), "%H:%M:%S")
            b = datetime.datetime.strptime(t1.group(1), "%H:%M:%S")
            secs = (b - a).total_seconds() % 86400
            impl = f"{int(secs // 3600)}~h {int(secs % 3600 // 60):02d}~min"
        else:
            impl = None
        if "BUILD_EXIT 0" in log:
            outcome = "bitstream"
        elif "Routing results verification failed" in log:
            outcome = "routing failed"
        elif "BUILD_EXIT" in log:
            outcome = "failed"
        else:
            running = subprocess.run(["pgrep", "-f", f"batch/{d}/"], capture_output=True, text=True).stdout.strip()
            outcome = pend("build running") if running else "stopped"
            if not running:
                impl = "stopped"
        if impl is None:
            impl = pend("implementation time")
        if d == "hw_tf" and "BUILD_EXIT 0" in log:
            M["TfImplTime"] = impl.replace("~", " ")
        lines.append(f"{frame} & {gen} & {slrs} & {place} & {dsp} & {impl} & {outcome} " + r"\\")
        if d == "hw_mem":
            pl = open(os.path.join(base, "_x", "link", "vivado", "vpl", "prj", "prj.runs", "impl_1",
                                   "kernel_util_placed.rpt")).read()
            used = re.search(r"Used Resources.*?\|\s*(\d+)\s*\[\s*([\d.]+)%\]\s*\|\s*$", pl, re.M)
            M["PhiloxDspCardPct"] = f"{float(used.group(2)):.0f}"
            M["PhiloxDsp"] = dsp
            M["PhiloxDspSlr"] = re.search(r"\|Utilization SLR \(%\)\s*\|\s*~?\d+\|\s*~?(\d+)\|", rpt).group(1)
    lines += [r"\botrule", r"\end{tabular}"]
    write("tab_builds.tex", lines)
    M["FailedBuilds"] = str(sum(1 for l in lines if "routing failed" in l))


# ---------------------------------------------------------------- hardware throughput
def bench(cpu_rows, stats, first):
    f, d = latest("bench/multicard/batch_*.json", lambda d: d.get("end") and d.get("bench"))
    keys = [f"Fpga{c}{n}" for c in ("One", "Two", "Four") for n in SHORT.values()]
    if not d:
        for k in keys:
            M[k] = pend("card benchmark")
        write("tab_throughput.tex", [r"\begin{tabular}{@{}l@{}}", r"\PENDING{card benchmark}", r"\end{tabular}"])
        return
    M["BenchFile"] = os.path.basename(f)
    agg = {}
    for r in d["bench"]:
        agg.setdefault((r["circuit"], r["cards"]), []).append(r["aggregate_shots_per_sec"])
    med = {k: statistics.median(v) for k, v in agg.items()}
    inst = d["bench"][0]["instances_per_card"][0]
    M["InstancesPerCard"] = str(inst)
    word = {1: "One", 2: "Two", 4: "Four"}
    for (c, cards), v in med.items():
        M[f"Fpga{word[cards]}{SHORT[c]}"] = fmt_rate(v)
    effs = []
    lines = [r"\begin{tabular}{@{}lrrrrrr@{}}", r"\toprule",
             r"Circuit & 1 card & 2 cards & 4 cards & $S_4$ & $R_1$ & $R_{\mathrm{all}}$ \\",
             r"\midrule"]
    for c, n in SHORT.items():
        one, four = med[(c, 1)], med[(c, 4)]
        eff = four / (4 * one)
        effs.append(eff)
        r1, rall = cpu_rows[c][1], cpu_rows[c][max(cpu_rows[c])]
        M[f"RatioOne{n}"] = fmt_x(four / r1)
        M[f"RatioAll{n}"] = fmt_x(four / rall)
        M[f"OverFirst{n}"] = fmt_x(one / first[n])
        # cycle model: LANES shots per scheduled slot at the kernel clock, per instance
        lines.append(f"{NAMES[c]} & {fmt_int(one)} & {fmt_int(med[(c, 2)])} & {fmt_int(four)} & "
                     f"{four / one:.2f}$\\times$ & {fmt_x(four / r1)}$\\times$ & {fmt_x(four / rall)}$\\times$ " + r"\\")
    lines += [r"\botrule", r"\end{tabular}"]
    write("tab_throughput.tex", lines)
    M["ScaleEffMin"] = f"{100 * min(effs):.0f}"
    M["ScaleEffMax"] = f"{100 * max(effs):.0f}"
    M["ScaleFactorMin"] = f"{4 * min(effs):.2f}"
    M["ScaleFactorMax"] = f"{4 * max(effs):.2f}"
    M["BenchReps"] = str(max(r["rep"] for r in d["bench"]) + 1)
    M["BenchCheckPassed"] = str(sum(1 for c in d["check"] if c["bit_exact"]))
    M["BenchCheckTotal"] = str(len(d["check"]))
    temps = [t["temps"] for t in d.get("temps", [])]
    fpga = [v["fpga_c"] for t in temps for v in t.values() if v.get("fpga_c") is not None]
    M["MaxFpgaTemp"] = str(max(fpga)) if fpga else pend("temperature")
    # model of one instance from the cycle counts: LANES shots per batch of `slots` cycles
    if "KernelMhz" in M and not M["KernelMhz"].startswith("\\PENDING"):
        mhz = float(M["KernelMhz"])
        lanes = int(M["Lanes"])
        for c, n in SHORT.items():
            model = lanes * mhz * 1e6 / max(stats[c]["slots"], stats[c]["meas"] + stats[c]["fold"], stats[c]["out"])
            M[f"ModelCard{n}"] = fmt_rate(model * inst)
            M[f"ModelFrac{n}"] = f"{100 * med[(c, 1)] / (model * inst):.0f}"


# ---------------------------------------------------------------- validation against Stim
def validation():
    f, d = latest("bench/multicard/validate_*.json", lambda d: "passed" in d)
    if not d:
        M["TierFourMaxZ"] = M["TierFiveMaxZ"] = M["ValidationPassed"] = pend("validation")
        for t in ("tab_ler.tex", "tab_tier4.tex"):
            write(t, [r"\begin{tabular}{@{}l@{}}", r"\PENDING{validation}", r"\end{tabular}"])
        return
    M["TierFourMaxZ"] = f"{max(r['max_abs_z'] for r in d['tier4']):.2f}" if d["tier4"] else pend("tier 4")
    M["TierFiveMaxZ"] = f"{max(abs(r['z']) for r in d['tier5']):.2f}" if d["tier5"] else pend("tier 5")
    M["TierFourShots"] = fmt_int(d["tier4"][0]["fpga_shots"]) if d["tier4"] else pend("tier 4")
    M["ValidationPassed"] = "all" if d["passed"] else pend("validation failed")
    lines = [r"\begin{tabular}{@{}rrrrrr@{}}", r"\toprule",
             r"$d$ & $p$ & Shots & FPGA LER & CPU Stim LER & $z$ \\", r"\midrule"]
    for r in d["tier5"]:
        lines.append(f"{r['distance']} & {r['p']:.3f} & {fmt_int(r['shots'])} & "
                     f"{r['fpga_logical_errors'] / r['shots']:.3e} & {r['stim_logical_errors'] / r['shots']:.3e} & "
                     f"${r['z']:+.2f}$ " + r"\\")
    lines += [r"\botrule", r"\end{tabular}"]
    write("tab_ler.tex", lines)
    lines = [r"\begin{tabular}{@{}lrrr@{}}", r"\toprule", r"Circuit & FPGA shots & Stim shots & max $|z|$ \\",
             r"\midrule"]
    for r in d["tier4"]:
        lines.append(f"{NAMES[r['circuit']]} & {fmt_int(r['fpga_shots'])} & {fmt_int(r['stim_shots'])} & "
                     f"{r['max_abs_z']:.2f} " + r"\\")
    lines += [r"\botrule", r"\end{tabular}"]
    write("tab_tier4.tex", lines)


# ---------------------------------------------------------------- first kernel (companion manuscript)
def first_kernel():
    """single card rates of the first kernel, from bench/results/2026-09-01-mode-a-throughput.md (the measurements
    reported in the companion manuscript)."""
    t = open(os.path.join(REPO, "bench", "results", "2026-09-01-mode-a-throughput.md")).read()
    keys = {"repetition_code d=3": "Rep", "surface_code d=3": "SurfThree", "surface_code d=5": "SurfFive"}
    out = {}
    for label, n in keys.items():
        m = re.search(r"\| " + re.escape(label) + r" \| ([\d,]+) \| ([\d,]+) \|", t)
        out[n] = int(m.group(1).replace(",", ""))
        M[f"FirstKernel{n}"] = fmt_int(out[n])
    v = open(os.path.join(REPO, "bench", "results", "2026-09-01-hardware-validation.md")).read()
    m = re.search(r"(\d+)/(\d+) DEM error mechanisms", v)
    M["FirstTierThree"], M["FirstTierThreeTotal"] = m.group(1), m.group(2)
    return out


def write(name, lines):
    os.makedirs(GEN, exist_ok=True)
    open(os.path.join(GEN, name), "w").write("% generated by scripts/make_numbers.py\n" + "\n".join(lines) + "\n")


def main():
    platform()
    rows = cpu()
    stats = compiler()
    hls()
    postroute()
    builds()
    first = first_kernel()
    bench(rows, stats, first)
    validation()
    # every \\Num macro the text uses must exist; the ones without a source yet become PENDING markers
    used = set()
    for f in glob.glob(os.path.join(PAPER, "sections", "*.tex")) + [os.path.join(PAPER, "main.tex")]:
        used |= set(re.findall(r"\\Num([A-Za-z]+)", open(f).read()))
    for k in sorted(used - set(M)):
        M[k] = pend(f"{k}")
    out = ["% generated by scripts/make_numbers.py; do not edit"]
    out += [f"\\newcommand{{\\Num{k}}}{{{v}}}" for k, v in sorted(M.items())]
    write("macros.tex", out)
    print(f"generated/macros.tex: {len(M)} macros, {len(MISSING)} pending")
    for m in sorted(set(MISSING)):
        print("  pending:", m)


if __name__ == "__main__":
    main()
