#!/usr/bin/env python3
"""Regenerates build/connectivity.cfg -- HBM bank assignment for the
stim_frame_sampler kernel's memory-mapped arguments. Per the project
brief, this is generated rather than handwritten so bank counts stay one
variable to tune, not four scattered edits. Run after changing the
kernel's argument list or INTERFACE pragmas (kernel/stim_frame_sampler.cpp).

Starts at 4 of the U55C's 32 HBM pseudo-channels, well under the "at most
8 banks" ceiling the project brief sets from prior experience with
Vivado level-7 routing congestion on this card at 16 banks -- expand only
after timing closes, per that same guidance.
"""

from pathlib import Path

# argument name -> HBM bank. One argument per bank even though
# instructions/layer_offsets share an m_axi bundle (gmem0) and
# detector_out/observable_out share another (gmem1) in the kernel's
# INTERFACE pragmas -- v++ assigns banks per argument, not per bundle.
_KERNEL_NAME = "stim_frame_sampler"
_INSTANCE_NAME = "sampler_1"
_ARGUMENT_BANKS = {
    "instructions": 0,
    "layer_offsets": 1,
    "detector_out": 2,
    "observable_out": 3,
}
# 250 MHz, not the originally-targeted 300: a real `hw` build at 300 MHz
# missed timing closure (WNS -0.688ns, ~249 MHz achievable) on a
# routing-dominated path -- see ../docs/utilization.md's 2026-08-31
# entry. 250 MHz is this project's own stated floor, giving Vivado 20%
# more slack than the 300 MHz attempt had. Must match vpp_build.sh's
# --kernel_frequency.
_CLOCK_HZ = 250_000_000


def generate() -> str:
    lines = [
        "# GENERATED FILE -- do not edit by hand. Regenerate with:",
        "#   python3 build/generate_connectivity.py",
        "# Source of truth: build/generate_connectivity.py",
        "",
        "[connectivity]",
        f"nk={_KERNEL_NAME}:1:{_INSTANCE_NAME}",
    ]
    for arg, bank in _ARGUMENT_BANKS.items():
        lines.append(f"sp={_INSTANCE_NAME}.{arg}:HBM[{bank}]")
    lines += [
        "",
        "[hls]",
        f"clock={_CLOCK_HZ}:{_KERNEL_NAME}",
        "",
        "[vivado]",
        "prop=run.impl_1.STEPS.OPT_DESIGN.ARGS.DIRECTIVE=Explore",
        # Three real hw builds (see ../docs/utilization.md) all failed
        # timing in the same place regardless of clock target: routing
        # delay within the Philox draw_noise logic (64x replicated),
        # cut from WNS -0.688ns to -0.179ns once the clock retarget was
        # actually applied correctly, but not closed. That's specifically
        # what more aggressive placement/routing effort and a post-route
        # physical-optimization pass are for -- closing a small residual
        # gap in a locally congested region, not exploring a fundamentally
        # different implementation. All standard Vivado directives, not a
        # hand-written constraint on instance names that change between
        # runs. That set of directives took WNS from -0.688ns to
        # -0.179ns (250 MHz genuinely applied) to -0.041ns (this attempt,
        # AggressiveExplore added on top) -- see ../docs/utilization.md.
        # ROUTE_DESIGN switched to NoTimingRelaxation for the next
        # attempt: it forces full timing effort on every net rather than
        # relaxing on non-critical ones (AggressiveExplore's own
        # tradeoff), aimed at closing what's now a very small residual
        # gap rather than exploring more broadly.
        "prop=run.impl_1.STEPS.PLACE_DESIGN.ARGS.DIRECTIVE=ExtraTimingOpt",
        "prop=run.impl_1.STEPS.PHYS_OPT_DESIGN.IS_ENABLED=true",
        "prop=run.impl_1.STEPS.PHYS_OPT_DESIGN.ARGS.DIRECTIVE=AggressiveExplore",
        "prop=run.impl_1.STEPS.ROUTE_DESIGN.ARGS.DIRECTIVE=NoTimingRelaxation",
        "prop=run.impl_1.STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED=true",
        "prop=run.impl_1.STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE=AggressiveExplore",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    out = Path(__file__).resolve().parent / "connectivity.cfg"
    out.write_text(generate())
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
