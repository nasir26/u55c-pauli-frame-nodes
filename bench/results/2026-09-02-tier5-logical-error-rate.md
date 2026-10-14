# Tier 5: logical error rate, FPGA vs. CPU Stim (both via PyMatching)

Reproduce with `python3 bench/hw_tier5.py` (needs a built, timing-closed
`build/hw/` — see `build/README.md`). Surface code, rotated memory Z,
d = 3 and d = 5 only — the two distances the current kernel's
`NUM_QUBITS_MAX = 128` covers (d=7 needs more; see below). 1,000,000
shots per (d, p) point, both sides decoded with the identical PyMatching
2.4.0 matcher built from the identical DEM, so this isolates whether the
FPGA's *syndromes* are right, not whether the decoder agrees with itself.

## Results

| d | p | FPGA logical error rate | CPU Stim logical error rate | z |
|---:|---:|---:|---:|---:|
| 3 | 0.0010 | 0.00074 | 0.00079 | -1.38 |
| 3 | 0.0030 | 0.00651 | 0.00662 | -0.93 |
| 3 | 0.0100 | 0.05906 | 0.05919 | -0.40 |
| 5 | 0.0010 | 0.00013 | 0.00016 | -1.59 |
| 5 | 0.0030 | 0.00331 | 0.00332 | -0.06 |
| 5 | 0.0100 | 0.08412 | 0.08395 | 0.42 |

All six points agree within a 5σ bar (max |z| = 1.59) — **PASS**.

The rates themselves show the behavior a correctly working surface code
should: below the apparent threshold (p = 0.001, 0.003), d=5 has a
*lower* logical error rate than d=3 (error suppression with distance);
at p = 0.01, d=5 does *worse* than d=3, the classic above-threshold
signature where a bigger code has more opportunities to fail. This
wasn't targeted or tuned for — it falls out of real syndromes correctly
decoded, which is exactly what Tier 5 is checking for.

## Methodology note: the b8 transpose

The kernel's output is detector-major (`detector_out[d]` is one 64-bit
word, bit *s* = detector *d*'s result in shot *s*); PyMatching's
`decode_batch(bit_packed_shots=True)` wants shot-major (Stim's own b8
convention: one row per shot, bit *i* of byte *j* = detector `8j+i`).
`host/xrt_tier5.cpp` transposes this on the host after each batch —
cheap next to the kernel launch itself (64 shots × 256 detector bits).
Before trusting it for real hardware time, it was cross-checked against
the independently-validated `xrt_tier4.cpp` (aggregate popcounts, not
transposed) on the same instruction stream and seed sequence: 0
mismatches across all 8 real detectors of the repetition code test
circuit.

An on-chip b8 staging design — filling output byte columns directly as
detectors are folded, rather than transposing after the fact — is
future work, not attempted here; see the project's own architecture
notes on why that's "free" once designed for, referenced in earlier
project history.

## Not attempted: d = 7, 9, 11

Full Tier 5 per the project brief covers d = 3, 5, 7, 9, 11. d=7 alone
needs more qubits than the current kernel's `NUM_QUBITS_MAX = 128`
supports (d=11 needs 274). Extending requires a config change, a fresh
`v++` build, and — per `docs/utilization.md`'s five-attempt, ~24-hour
experience closing timing at the *current*, smaller qubit count — a real
possibility of another multi-attempt timing-closure process at a larger,
likely more resource-hungry design. Genuine, scoped, separate work, not
something to start without that time cost being explicit first.
