#!/usr/bin/env python3
"""Figures of the manuscript (figures/*.pdf).

Author: Nasir Ali, C-DAC Noida.

fig_nodes, fig_kernel: block diagrams. fig_throughput: measured rates from the latest complete
bench/multicard/batch_*.json and bench/multicard/cpu_stim_parallel.json (placeholder text if the card data is
missing; the final build refuses placeholders through the PENDING check).
usage: make_figures.py
"""
import glob, json, os, statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(PAPER, ".."))
FIG = os.path.join(PAPER, "figures")
plt.rcParams.update({"font.size": 9, "font.family": "serif", "pdf.fonttype": 42})


def box(ax, x, y, w, h, text, fc="#e8eef7", ec="#2b4a7a", fs=8, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08", fc=fc, ec=ec, lw=1))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, weight="bold" if bold else "normal")


def arrow(ax, x0, y0, x1, y1, text=None, both=False):
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                arrowprops=dict(arrowstyle="<->" if both else "->", lw=0.9, color="#333333"))
    if text:
        ax.text((x0 + x1) / 2 + 0.05, (y0 + y1) / 2, text, fontsize=7, ha="left", va="center", color="#333333")


def fig_nodes(instances):
    fig, ax = plt.subplots(figsize=(7.0, 3.2))
    ax.set_xlim(0, 10)
    ax.set_ylim(-0.4, 4.6)
    ax.axis("off")
    box(ax, 0.3, 3.4, 9.4, 1.0, "Host (2 CPU sockets, DDR4)\ncompile circuit once  |  load program  |  collect "
        "detector and observable bits", fc="#f3f3f3", ec="#555555", fs=8)
    for c in range(4):
        x = 0.3 + c * 2.4
        box(ax, x, 0.2, 2.2, 2.4, "", fc="#ffffff", ec="#2b4a7a")
        ax.text(x + 1.1, 2.4, f"Node {c}", ha="center", va="center", fontsize=8, weight="bold")
        ax.text(x + 1.1, 2.4 - 0.0, "", fontsize=1)
        ax.text(x + 1.1, 2.12, f"Alveo U55C, key (seed, {c + 1})", ha="center", va="center", fontsize=6.5,
                style="italic")
        h = 1.6 / instances
        for j in range(instances):
            box(ax, x + 0.15, 0.3 + j * h, 1.9, h - 0.12, f"SLR{j + 1}: instance {j}\nbatches from {j}" + r"$\cdot 2^{28}$",
                fc="#e8eef7", fs=6.5)
        arrow(ax, x + 1.1, 3.38, x + 1.1, 2.62, "PCIe", both=True)
    ax.text(5.0, -0.2, "the nodes exchange no data; each returns its own detector and observable bits to the host",
            ha="center", fontsize=7.5, color="#8a1c1c")
    fig.savefig(os.path.join(FIG, "fig_nodes.pdf"), bbox_inches="tight")
    plt.close(fig)


def fig_kernel(lanes):
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 4.4)
    ax.axis("off")
    box(ax, 0.1, 3.3, 3.0, 0.8, "HBM: program, fold list\n(copied on chip once per launch)", fc="#f3f3f3",
        ec="#555555", fs=7)
    box(ax, 8.9, 3.3, 3.0, 0.8, "HBM: outputs\n(one word per detector per batch)", fc="#f3f3f3", ec="#555555", fs=7)
    stages = [("Noise stage", f"{lanes // 2} Threefry4x32-20 units\n{lanes} lane masks per cycle"),
              ("Frame stage", "frame in LUT RAM\n1 read + 1 write per cycle"),
              ("Fold stage", "measurement record\nXOR of listed slots"),
              ("Store stage", "bursts to HBM")]
    for k, (title, sub) in enumerate(stages):
        x = 0.1 + k * 3.0
        box(ax, x, 1.0, 2.6, 1.6, "", fc="#e8eef7")
        ax.text(x + 1.3, 2.25, title, ha="center", va="center", fontsize=8.5, weight="bold")
        ax.text(x + 1.3, 1.55, sub, ha="center", va="center", fontsize=7)
        if k < 3:
            arrow(ax, x + 2.62, 1.8, x + 2.98, 1.8)
    ax.text(3.0, 0.6, "masks", fontsize=7, ha="center")
    ax.text(6.0, 0.6, "measurement flips", fontsize=7, ha="center")
    ax.text(9.0, 0.6, "outputs", fontsize=7, ha="center")
    arrow(ax, 1.4, 3.28, 1.4, 2.62)
    arrow(ax, 1.9, 3.28, 7.4, 2.62)
    arrow(ax, 10.4, 2.62, 10.4, 3.28)
    ax.text(6.0, 0.15, "all loops pipelined at one iteration per cycle; stages connected by FIFO streams",
            ha="center", fontsize=7, style="italic")
    fig.savefig(os.path.join(FIG, "fig_kernel.pdf"), bbox_inches="tight")
    plt.close(fig)


def fig_throughput():
    names = {"repetition_code_d3": "Repetition $d=3$", "surface_code_d3": "Surface $d=3$",
             "surface_code_d5": "Surface $d=5$"}
    cpu = json.load(open(os.path.join(REPO, "bench", "multicard", "cpu_stim_parallel.json")))
    reps = {}
    for r in cpu["rows"]:
        reps.setdefault(r["circuit"], {}).setdefault(r["procs"], []).append(r["shots_per_sec"])
    rows = {c: {p: statistics.median(v) for p, v in ps.items()} for c, ps in reps.items()}
    pmax = max(p for v in rows.values() for p in v)
    files = sorted(glob.glob(os.path.join(REPO, "bench", "multicard", "batch_*.json")), key=os.path.getmtime,
                   reverse=True)
    bench = None
    for f in files:
        d = json.load(open(f))
        if d.get("end") and d.get("bench"):
            bench = d
            break
    fig, ax = plt.subplots(figsize=(6.6, 3.1))
    series = [("Stim, 1 process", lambda c: rows[c][1], "#b0b0b0"),
              (f"Stim, {pmax} processes", lambda c: rows[c][pmax], "#606060")]
    if bench:
        med = {}
        for r in bench["bench"]:
            med.setdefault((r["circuit"], r["cards"]), []).append(r["aggregate_shots_per_sec"])
        med = {k: statistics.median(v) for k, v in med.items()}
        for cards, col in ((1, "#9ab8e0"), (2, "#4f7fbf"), (4, "#1f3f73")):
            series.append((f"FPGA, {cards} card{'s' if cards > 1 else ''}", lambda c, n=cards: med[(c, n)], col))
    width = 0.8 / len(series)
    for k, (label, fn, col) in enumerate(series):
        xs = [i + (k - (len(series) - 1) / 2) * width for i in range(len(names))]
        ax.bar(xs, [fn(c) for c in names], width, label=label, color=col, edgecolor="black", lw=0.4)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(list(names.values()))
    ax.set_yscale("log")
    ax.set_ylabel("shots per second")
    ax.grid(axis="y", which="major", lw=0.3, alpha=0.6)
    ax.legend(fontsize=7, ncol=3, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.12))
    if not bench:
        ax.text(0.5, 0.5, "FPGA data pending", transform=ax.transAxes, ha="center", color="red", fontsize=14)
    fig.savefig(os.path.join(FIG, "fig_throughput.pdf"), bbox_inches="tight")
    plt.close(fig)


def main():
    import re
    os.makedirs(FIG, exist_ok=True)
    cfg = open(os.path.join(REPO, "kernel", "batch", "batch_config.hpp")).read()
    lanes = int(re.search(r"constexpr int LANES = (\d+);", cfg).group(1))
    inst = 2
    files = sorted(glob.glob(os.path.join(REPO, "bench", "multicard", "batch_*.json")), key=os.path.getmtime)
    if files:
        d = json.load(open(files[-1]))
        if d.get("bench"):
            inst = d["bench"][0]["instances_per_card"][0]
    fig_nodes(inst)
    fig_kernel(lanes)
    fig_throughput()
    print("figures:", sorted(os.listdir(FIG)))


if __name__ == "__main__":
    main()
