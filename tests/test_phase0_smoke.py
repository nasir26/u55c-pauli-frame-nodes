"""Phase 0 smoke tests: dependencies import, and the CI harness runs end to end.

Not a validation tier by itself -- Tiers 1-5 (see top-level README.md)
start in Phase 1 once softmodel/reference_sampler.py has an actual
sampler to validate. This file exists so Phase 0's gate ("CI green") has
something real to be green about.
"""

import stim
import pymatching

from softmodel.reference_sampler import noiseless_detectors_are_all_zero


def test_dependencies_importable():
    assert stim.__version__
    assert pymatching.__version__


def test_noiseless_repetition_code_has_zero_detectors():
    circuit = stim.Circuit.generated(
        "repetition_code:memory",
        rounds=5,
        distance=3,
    )
    assert noiseless_detectors_are_all_zero(circuit)


def test_noiseless_surface_code_has_zero_detectors():
    circuit = stim.Circuit.generated(
        "surface_code:rotated_memory_z",
        rounds=5,
        distance=3,
    )
    assert noiseless_detectors_are_all_zero(circuit)
