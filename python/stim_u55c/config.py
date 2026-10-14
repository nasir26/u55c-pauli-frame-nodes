"""Single source of truth for kernel sizing constants.

kernel/config.hpp is generated from this file (see
kernel/generate_headers.py) so the host-side instruction encoder and the
HLS kernel can never disagree about a buffer size. Per the project brief:
no qubit maxima, detector maxima, or shot batch sizes hardcoded anywhere
else.

SHOTS=64 is a Phase 2 functional-correctness value, not a throughput
target -- raising it is the most direct throughput lever once there are
real part utilization figures to size against (Phase 3+). It has no
bearing on correctness: the frame store is just SHOTS bits wide per
qubit either way.
"""

SHOTS = 64
NUM_QUBITS_MAX = 128
NUM_DETECTORS_MAX = 256
NUM_OBSERVABLES_MAX = 8
NUM_LAYERS_MAX = 1024  # see kernel/isa.py:_layer_and_reorder (project brief section 3.1)

NUM_DETECTOR_BYTES = NUM_DETECTORS_MAX // 8
NUM_OBSERVABLE_BYTES = NUM_OBSERVABLES_MAX // 8

assert NUM_DETECTORS_MAX % 8 == 0
assert NUM_OBSERVABLES_MAX % 8 == 0
