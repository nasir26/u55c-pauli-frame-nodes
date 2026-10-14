"""Phase 1 gate: softmodel/reference_sampler.py against Tiers 1, 3, and 4.

Tier 2 (bit-exact vs. the FPGA kernel) doesn't exist until there's a
kernel to compare against -- Phase 2. Tier 5 (logical error rate through
PyMatching) is Phase 4+, once there's real hardware-generated data to
feed it. See top-level README.md "Validation strategy" for all five.

Two circuits, per the Phase 1 gate: a d=3 repetition code and a d=3
surface code (both memory bases for the surface code, since the two use
different reset/measurement bases and exercise different code paths).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import stim

from softmodel.reference_sampler import (
    noiseless_detectors_are_all_zero,
    sample_detectors,
    sample_single_fault,
)

_NOISE_KWARGS = dict(
    before_round_data_depolarization=0.01,
    before_measure_flip_probability=0.01,
    after_clifford_depolarization=0.01,
    after_reset_flip_probability=0.01,
)

_SIGMA_THRESHOLD = 5.0


def _repetition_code(rounds: int = 5, **noise) -> stim.Circuit:
    return stim.Circuit.generated("repetition_code:memory", rounds=rounds, distance=3, **noise)


def _surface_code(task: str, rounds: int = 3, **noise) -> stim.Circuit:
    return stim.Circuit.generated(task, rounds=rounds, distance=3, **noise)


_CIRCUITS = {
    "repetition_code": lambda: _repetition_code(rounds=5, before_round_data_depolarization=0.01, before_measure_flip_probability=0.01),
    "surface_code_z": lambda: _surface_code("surface_code:rotated_memory_z", rounds=3, **_NOISE_KWARGS),
    "surface_code_x": lambda: _surface_code("surface_code:rotated_memory_x", rounds=3, **_NOISE_KWARGS),
}


# ---------------------------------------------------------------------------
# Tier 1 -- noiseless determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _CIRCUITS)
def test_tier1_noiseless_determinism(name):
    circuit = _CIRCUITS[name]()
    assert noiseless_detectors_are_all_zero(circuit, shots=512)


# ---------------------------------------------------------------------------
# Tier 3 -- single-fault injection against the DEM
# ---------------------------------------------------------------------------


def _dem_targets(dem_error_terms, predicate):
    return {t.dem_target.val for t in dem_error_terms if predicate(t.dem_target)}


@pytest.mark.parametrize("name", _CIRCUITS)
def test_tier3_single_fault_injection_matches_dem(name):
    circuit = _CIRCUITS[name]()
    dem = circuit.detector_error_model(decompose_errors=True)
    explained = circuit.explain_detector_error_model_errors(
        dem_filter=dem, reduce_to_one_representative_error=True
    )
    assert len(explained) > 0, "circuit's DEM has no error mechanisms -- noise config is wrong"

    checked = 0
    for explained_error in explained:
        loc = explained_error.circuit_error_locations[0]
        path = tuple((f.instruction_offset, f.iteration_index) for f in loc.stack_frames)
        qubit_paulis = tuple((g.gate_target.value, g.gate_target.pauli_type) for g in loc.flipped_pauli_product)

        result = sample_single_fault(circuit, path=path, qubit_paulis=qubit_paulis)

        fired = set(int(i) for i in result.detectors[0].nonzero()[0])
        expected = _dem_targets(explained_error.dem_error_terms, lambda t: t.is_relative_detector_id())
        obs_fired = set(int(i) for i in result.observables[0].nonzero()[0]) if result.observables.shape[1] else set()
        expected_obs = _dem_targets(explained_error.dem_error_terms, lambda t: t.is_logical_observable_id())

        assert fired == expected, (
            f"{name}: fault at {qubit_paulis} fired detectors {fired}, DEM says {expected}"
        )
        assert obs_fired == expected_obs, (
            f"{name}: fault at {qubit_paulis} flipped observables {obs_fired}, DEM says {expected_obs}"
        )
        checked += 1

    assert checked == len(explained)


# ---------------------------------------------------------------------------
# Tier 4 -- statistical equivalence with Stim over >= 1e7 shots
# ---------------------------------------------------------------------------


def _two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> float:
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return (p1 - p2) / se


def _correlation_z(r1: float, r2: float, n: int) -> float:
    # Large-N normal approximation to the sampling distribution of a
    # Pearson correlation coefficient; adequate at n ~ 1e7 without pulling
    # in scipy as a dependency.
    se = math.sqrt(2.0 / max(n - 3, 1))
    if se == 0:
        return 0.0
    return (r1 - r2) / se


@pytest.mark.parametrize("name", _CIRCUITS)
def test_tier4_statistical_equivalence_with_stim(name):
    shots = 10_000_000
    circuit = _CIRCUITS[name]()

    ours = sample_detectors(circuit, shots=shots, seed=12345).detectors
    theirs = circuit.compile_detector_sampler(seed=67890).sample(shots=shots)

    num_detectors = ours.shape[1]
    assert theirs.shape[1] == num_detectors

    # Per-detector firing rate: two-proportion z-test + total variation
    # distance (== |p1 - p2| for a Bernoulli variable).
    worst_z, worst_tvd = 0.0, 0.0
    flagged = []
    k_ours = ours.sum(axis=0)
    k_theirs = theirs.sum(axis=0)
    for d in range(num_detectors):
        z = _two_proportion_z(int(k_ours[d]), shots, int(k_theirs[d]), shots)
        tvd = abs(k_ours[d] - k_theirs[d]) / shots
        worst_z = max(worst_z, abs(z))
        worst_tvd = max(worst_tvd, tvd)
        if abs(z) > _SIGMA_THRESHOLD:
            flagged.append(("firing_rate", d, z, tvd))

    # Pairwise detector correlation, both samplers, compared with the same
    # 5-sigma bar.
    ours_f = ours.astype(np.float64)
    theirs_f = theirs.astype(np.float64)
    corr_ours = np.corrcoef(ours_f, rowvar=False)
    corr_theirs = np.corrcoef(theirs_f, rowvar=False)
    for i in range(num_detectors):
        for j in range(i + 1, num_detectors):
            r1, r2 = corr_ours[i, j], corr_theirs[i, j]
            if np.isnan(r1) or np.isnan(r2):
                continue  # a detector that never fires has undefined correlation
            z = _correlation_z(r1, r2, shots)
            if abs(z) > _SIGMA_THRESHOLD:
                flagged.append(("pairwise_correlation", (i, j), z, abs(r1 - r2)))

    print(
        f"\n[{name}] {num_detectors} detectors, {shots} shots: "
        f"max |z| (firing rate) = {worst_z:.2f}, max TVD = {worst_tvd:.2e}, "
        f"{len(flagged)} comparisons flagged outside {_SIGMA_THRESHOLD} sigma"
    )
    assert not flagged, f"{name}: statistically inconsistent with Stim: {flagged[:10]}"
