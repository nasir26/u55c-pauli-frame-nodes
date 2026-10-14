# Validation tiers on real hardware

Everything below runs against the timing-closed `build/hw/stim_frame_sampler.xclbin`
(`docs/utilization.md`'s fifth `hw` attempt, WNS 0.000ns) on a physical
Alveo U55C, not emulation. Same three circuits used throughout this
project's validation since Phase 1/2 (repetition code d=3, surface code
d=3 and d=5, plus surface code d=3 in the X basis for Tier 3).

Reproduce: `bench/hw_tier3.py`, `bench/hw_tier4.py`, `bench/hw_tier5.py`
(need a built `build/hw/` — see `build/README.md`). Tier 1/2 were run ad
hoc via `build/hw/xrt_runner` on noiseless and normal instruction streams
respectively (see commit history around 2026-09-01 for the exact
commands); not (yet) wrapped into a committed script the way Tiers 3/4/5
are.

## Results

| Tier | What | Circuits | Result |
|---|---|---|---|
| 1 | Noiseless determinism | rep d3, surface d3, surface d5 | **PASS** — all detector/observable words exactly zero, all 64 shot lanes |
| 2 | Bit-exact vs. soft model | rep d3, surface d3, surface d5 | **PASS** — `docs/utilization.md`'s fifth-attempt entry |
| 3 | Single-fault injection vs. DEM | rep d3, surface z d3, surface x d3 | **PASS** — 458/458 DEM error mechanisms, ~92s |
| 4 | Statistical equivalence vs. CPU Stim | rep d3, surface d3, surface d5 | **PASS** — 10,000,000 shots/circuit, max\|z\| 1.64 / 2.91 / 2.64 (5σ bar), ~6m 42s total |
| 5 | Logical error rate vs. Stim+PyMatching | surface d3, surface d5 | **PASS** — 6 points (2 distances × 3 error rates), max\|z\| 1.59; d=7/9/11 not attempted (see `bench/results/2026-09-02-tier5-logical-error-rate.md`) |

Tiers 1-4 above are the real-hardware counterparts of validation that
already passed in software since Phase 1 (Tiers 1/3/4,
`tests/test_softmodel_validation.py`) and Phase 2/3 (Tier 2, C-sim and
RTL cosimulation). What's new here is running the *same* checks through
the actual synthesized kernel on physical silicon rather than a model of
it.

## Tier 3 methodology note

The kernel has no "fault injection mode" — Tier 3 needs an actual
instruction stream with a forced, deterministic single Pauli error and
nothing else. `softmodel/reference_sampler.py:build_single_fault_circuit`
builds one: given a `stim.CircuitErrorLocation`'s `stack_frames` and
`flipped_pauli_product` (same location format Tier 3 has used since
Phase 1), it walks the original circuit with the same path-matching logic
`sample_single_fault` already uses to *interpret* a fault, but emits an
actual `stim.Circuit` instead — noise stripped everywhere except an
explicit `p=1` `X_ERROR`/`Y_ERROR`/`Z_ERROR` at the matched location.
That circuit goes through the ordinary, unmodified `kernel.isa.encode_circuit`
and runs on the ordinary, unmodified kernel — no isa.py or kernel changes
needed for Tier 3 to reach real hardware.

Before trusting this on ~92 seconds of real hardware time, it was
cross-checked against `sample_single_fault` (the already-validated
Phase 1 interpreter) for the same 458 DEM mechanisms: 0 disagreements.

## Tier 5

Done for d=3 and d=5 (the distances `NUM_QUBITS_MAX=128` supports without
a rebuild): 6 points (2 distances × 3 physical error rates), all within
a 5σ bar against CPU Stim+PyMatching, decoded with the identical
matcher on both sides so this isolates syndrome correctness rather than
decoder agreement. Full account, including the on-host b8 transpose this
needed (kernel output is detector-major, PyMatching wants shot-major)
and its own cross-check before trusting it on real hardware, in
`bench/results/2026-09-02-tier5-logical-error-rate.md`.

d = 7, 9, 11 not attempted: d=7 alone needs more qubits than
`NUM_QUBITS_MAX = 128` supports (d=11 needs 274), so reaching them needs
a config change and a fresh `v++` build — and per this document's own
five-attempt, ~24-hour experience closing timing at the *current*,
smaller qubit count, a real possibility of another multi-attempt timing
closure at a larger, likely more resource-hungry design. Genuine, scoped,
separate work.
