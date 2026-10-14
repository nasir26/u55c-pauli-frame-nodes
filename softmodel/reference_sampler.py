"""Bit-exact Python reference model of the stim-u55c kernel.

This module is the Tier 2 oracle (see top-level README.md, "Validation
strategy"): the FPGA kernel's output must match this module's output
bit-for-bit given the same instruction stream and PRNG seed (Phase 2+).
Tiers 1, 3, and 4 -- the ones this module has to pass on its own, in
software, before any kernel code is written -- are implemented and
exercised in tests/test_softmodel_validation.py.

Architecture (mirrors Stim section 3 / 5.6, see top-level README.md):

  - A "frame" is one Pauli error per qubit, tracked as an (x, z) bit pair
    per qubit per shot: (0,0)=I (0,1)=Z (1,0)=X (1,1)=Y. Clifford gates
    conjugate the frame by permuting/XORing these bits; Pauli gates (X, Y,
    Z, as *circuit* instructions rather than noise) are no-ops on the
    frame, because global phase doesn't affect which detectors fire.
  - Noise instructions (X_ERROR, Y_ERROR, Z_ERROR, DEPOLARIZE1,
    DEPOLARIZE2) flip frame bits with the stated probability, drawn from a
    counter-based PRNG (numpy's Philox), independently per shot.
  - Measurement reads out whichever frame component anticommutes with the
    measurement basis (x for Z-basis, z for X-basis, x^z for Y-basis),
    XORed against a *reference* bit.
  - The reference is computed once, deterministically, by running the
    circuit with all noise stripped through upstream Stim's own
    TableauSimulator (this is exactly the tableau-simulation-stays-on-CPU
    split described in the top-level README -- Stim is the reference
    oracle here, not reimplemented).
  - Detectors and observables are XORs of (actual XOR reference) over
    their listed measurement-record targets, computed generally rather
    than assuming the reference contribution is always zero, so genuinely
    gauge/non-deterministic detectors are still handled correctly.

This is deliberately the "straightforward, slow, obviously correct"
implementation the project brief calls for: it walks the circuit
instruction by instruction (recursing into REPEAT blocks rather than
flattening them away), vectorized only across the shot axis via numpy.
It is not written for speed -- that's the kernel's job.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import stim

# ---------------------------------------------------------------------------
# Gate tables
# ---------------------------------------------------------------------------

# Gates whose frame effect is a pure permutation/XOR of (x, z) bits, with no
# randomness. Pauli gates (X, Y, Z) are intentionally absent: conjugating a
# Pauli frame by a Pauli gate never changes which error type (I/X/Y/Z) is
# present, only its sign, and sign doesn't affect which detectors fire.
_SINGLE_QUBIT_DETERMINISTIC = frozenset({"H", "S", "S_DAG", "SQRT_Z", "SQRT_Z_DAG", "I"})
_TWO_QUBIT_DETERMINISTIC = frozenset({"CX", "CNOT", "CZ", "SWAP"})

_RESET = {"R": "Z", "RX": "X", "RY": "Y"}
_MEASURE = {"M": "Z", "MX": "X", "MY": "Y", "MZ": "Z"}
_MEASURE_RESET = {"MR": "Z", "MRX": "X", "MRY": "Y", "MZR": "Z"}

_NOISE_1Q = frozenset({"X_ERROR", "Y_ERROR", "Z_ERROR"})
_NOISE_DEPOLARIZE1 = "DEPOLARIZE1"
_NOISE_DEPOLARIZE2 = "DEPOLARIZE2"
_ALL_NOISE = _NOISE_1Q | {_NOISE_DEPOLARIZE1, _NOISE_DEPOLARIZE2}

_IGNORED = frozenset({"QUBIT_COORDS", "SHIFT_COORDS"})

# The 15 nontrivial two-qubit Pauli combinations DEPOLARIZE2 chooses among,
# each with probability p/15. Index -> (pauli_on_first_qubit, pauli_on_second_qubit).
_DEPOLARIZE2_COMBOS = [
    (a, b) for a, b in itertools.product("IXYZ", repeat=2) if (a, b) != ("I", "I")
]
_PAULI_HAS_X = {"I": False, "X": True, "Y": True, "Z": False}
_PAULI_HAS_Z = {"I": False, "X": False, "Y": True, "Z": True}
_DEPOLARIZE2_A_X = np.array([_PAULI_HAS_X[a] for a, _ in _DEPOLARIZE2_COMBOS])
_DEPOLARIZE2_A_Z = np.array([_PAULI_HAS_Z[a] for a, _ in _DEPOLARIZE2_COMBOS])
_DEPOLARIZE2_B_X = np.array([_PAULI_HAS_X[b] for _, b in _DEPOLARIZE2_COMBOS])
_DEPOLARIZE2_B_Z = np.array([_PAULI_HAS_Z[b] for _, b in _DEPOLARIZE2_COMBOS])


# ---------------------------------------------------------------------------
# Reference sample (noiseless, deterministic -- delegates to upstream Stim's
# TableauSimulator, per the CPU/FPGA split in the top-level README)
# ---------------------------------------------------------------------------


def _strip_noise(circuit: stim.Circuit) -> stim.Circuit:
    """Copy of `circuit` with every noise instruction removed, REPEAT blocks preserved."""
    out = stim.Circuit()
    for instruction in circuit:
        if isinstance(instruction, stim.CircuitRepeatBlock):
            out += _strip_noise(instruction.body_copy()) * instruction.repeat_count
        elif instruction.name in _ALL_NOISE:
            continue
        else:
            out.append(instruction)
    return out


def compute_reference_measurements(circuit: stim.Circuit, *, seed: int) -> list[bool]:
    """The measurement record of one deterministic, noiseless run of `circuit`.

    Every shot's actual measurement is this reference XORed with whatever
    the accumulated Pauli frame did to that measurement's qubit -- see
    module docstring. `seed` only matters for circuits containing a
    measurement that isn't deterministic even without noise (rare for
    well-formed QEC memory circuits); it makes that corner case
    reproducible rather than silently re-randomizing the reference on
    every call.
    """
    sim = stim.TableauSimulator(seed=seed)
    sim.do(_strip_noise(circuit))
    return list(sim.current_measurement_record())


# ---------------------------------------------------------------------------
# Frame state
# ---------------------------------------------------------------------------


@dataclass
class _Frame:
    x: np.ndarray  # shape (num_qubits, shots), bool
    z: np.ndarray

    @classmethod
    def zeros(cls, num_qubits: int, shots: int) -> "_Frame":
        return cls(
            x=np.zeros((num_qubits, shots), dtype=bool),
            z=np.zeros((num_qubits, shots), dtype=bool),
        )


@dataclass
class _FaultInjection:
    """A single deterministic fault to force instead of drawing noise, for Tier 3.

    `path` matches stim's CircuitErrorLocation.stack_frames: a tuple of
    (instruction_offset, iteration_index) pairs locating the exact noise
    instruction occurrence (including which REPEAT iteration) that the DEM
    error corresponds to. `qubit_paulis` is the (qubit, pauli-char) pairs
    to force-flip when that exact occurrence is reached; every other noise
    instruction in the circuit is suppressed (see `_run`).
    """

    path: tuple[tuple[int, int], ...]
    qubit_paulis: tuple[tuple[int, str], ...]
    applied: bool = field(default=False)


@dataclass
class _RunState:
    frame: _Frame
    shots: int
    rng: np.random.Generator | None
    meas_record: list[np.ndarray] = field(default_factory=list)
    reference: list[bool] = field(default_factory=list)
    detectors: list[np.ndarray] = field(default_factory=list)
    observables: dict[int, np.ndarray] = field(default_factory=dict)
    inject: _FaultInjection | None = None

    def rec_actual_xor_reference(self, targets) -> np.ndarray:
        acc = np.zeros(self.shots, dtype=bool)
        # rec[-k] is a lookback relative to how many measurements have
        # happened *so far* (Python negative indexing on the live,
        # still-growing meas_record handles that correctly on its own).
        # `self.reference` is the *complete* reference list computed up
        # front, so the same -k must be resolved to an absolute index
        # here rather than indexed from its end, or every detector but
        # the very last one reads the wrong reference bit.
        now = len(self.meas_record)
        for t in targets:
            if not t.is_measurement_record_target:
                continue
            idx = t.value  # negative lookback, e.g. -2
            acc ^= self.meas_record[idx] ^ bool(self.reference[now + idx])
        return acc


def _apply_pauli(state: _RunState, qubit: int, pauli: str, mask: np.ndarray) -> None:
    if pauli in ("X", "Y"):
        state.frame.x[qubit] ^= mask
    if pauli in ("Z", "Y"):
        state.frame.z[qubit] ^= mask


def _measure_outcome(state: _RunState, qubit: int, basis: str) -> np.ndarray:
    if basis == "Z":
        effect = state.frame.x[qubit]
    elif basis == "X":
        effect = state.frame.z[qubit]
    else:  # Y
        effect = state.frame.x[qubit] ^ state.frame.z[qubit]
    ref = bool(state.reference[len(state.meas_record)]) if state.reference else False
    return effect ^ ref


def _run(
    body: stim.Circuit,
    state: _RunState,
    *,
    path: tuple[tuple[int, int], ...],
    tick: list[int],
    pending_iteration: int = 0,
) -> None:
    # `pending_iteration` is which pass of the loop that directly contains
    # `body` we're currently on (0 if `body` isn't a REPEAT body at all).
    # It's attached to the *next* frame constructed below -- matching
    # stim's CircuitErrorLocation.stack_frames convention, where a frame's
    # iteration_index describes the loop containing that frame's own
    # instruction, not a loop the instruction itself introduces. See the
    # docstring on _FaultInjection for how this is used (Tier 3).
    for offset, instruction in enumerate(body):
        if isinstance(instruction, stim.CircuitRepeatBlock):
            frame = (offset, pending_iteration)
            for it in range(instruction.repeat_count):
                _run(
                    instruction.body_copy(),
                    state,
                    path=path + (frame,),
                    tick=tick,
                    pending_iteration=it,
                )
            continue

        name = instruction.name
        targets = instruction.targets_copy()
        args = instruction.gate_args_copy()
        here = path + ((offset, pending_iteration),)

        if name == "TICK":
            tick[0] += 1
        elif name in _IGNORED:
            pass
        elif name in _SINGLE_QUBIT_DETERMINISTIC:
            for t in targets:
                q = t.value
                if name == "H":
                    state.frame.x[q], state.frame.z[q] = (
                        state.frame.z[q].copy(),
                        state.frame.x[q].copy(),
                    )
                elif name != "I":  # S / S_DAG / SQRT_Z / SQRT_Z_DAG
                    state.frame.z[q] ^= state.frame.x[q]
        elif name in _TWO_QUBIT_DETERMINISTIC:
            pairs = [(targets[i].value, targets[i + 1].value) for i in range(0, len(targets), 2)]
            if name in ("CX", "CNOT"):
                for c, t in pairs:
                    state.frame.x[t] ^= state.frame.x[c]
                    state.frame.z[c] ^= state.frame.z[t]
            elif name == "CZ":
                for a, b in pairs:
                    xa = state.frame.x[a].copy()
                    xb = state.frame.x[b].copy()
                    state.frame.z[b] ^= xa
                    state.frame.z[a] ^= xb
            else:  # SWAP
                for a, b in pairs:
                    state.frame.x[a], state.frame.x[b] = state.frame.x[b].copy(), state.frame.x[a].copy()
                    state.frame.z[a], state.frame.z[b] = state.frame.z[b].copy(), state.frame.z[a].copy()
        elif name in _RESET:
            for t in targets:
                q = t.value
                state.frame.x[q][:] = False
                state.frame.z[q][:] = False
        elif name in _MEASURE:
            basis = _MEASURE[name]
            for t in targets:
                state.meas_record.append(_measure_outcome(state, t.value, basis))
        elif name in _MEASURE_RESET:
            basis = _MEASURE_RESET[name]
            for t in targets:
                q = t.value
                state.meas_record.append(_measure_outcome(state, q, basis))
                state.frame.x[q][:] = False
                state.frame.z[q][:] = False
        elif name in _ALL_NOISE:
            _apply_noise(state, name, targets, args, here=here, tick=tick[0])
        elif name == "DETECTOR":
            state.detectors.append(state.rec_actual_xor_reference(targets))
        elif name == "OBSERVABLE_INCLUDE":
            idx = int(args[0])
            bit = state.rec_actual_xor_reference(targets)
            if idx in state.observables:
                state.observables[idx] ^= bit
            else:
                state.observables[idx] = bit
        else:
            raise NotImplementedError(f"softmodel does not yet handle instruction {name!r}")


def _apply_noise(state: _RunState, name: str, targets, args, *, here, tick: int) -> None:
    inject = state.inject
    if inject is not None:
        # Fault-injection mode: every stochastic noise instruction is
        # suppressed except the one exact occurrence the fault targets.
        if here != inject.path:
            return
        for qubit, pauli in inject.qubit_paulis:
            _apply_pauli(state, qubit, pauli, np.ones(state.shots, dtype=bool))
        inject.applied = True
        return

    rng = state.rng
    p = args[0]
    if name in _NOISE_1Q:
        pauli = name[0]  # "X_ERROR" -> "X", etc.
        for t in targets:
            mask = rng.random(state.shots) < p
            _apply_pauli(state, t.value, pauli, mask)
    elif name == _NOISE_DEPOLARIZE1:
        for t in targets:
            active = rng.random(state.shots) < p
            which = rng.integers(0, 3, size=state.shots)  # 0=X 1=Y 2=Z
            _apply_pauli(state, t.value, "X", active & (which == 0))
            _apply_pauli(state, t.value, "Y", active & (which == 1))
            _apply_pauli(state, t.value, "Z", active & (which == 2))
    elif name == _NOISE_DEPOLARIZE2:
        for i in range(0, len(targets), 2):
            a, b = targets[i].value, targets[i + 1].value
            active = rng.random(state.shots) < p
            combo = rng.integers(0, 15, size=state.shots)
            state.frame.x[a] ^= active & _DEPOLARIZE2_A_X[combo]
            state.frame.z[a] ^= active & _DEPOLARIZE2_A_Z[combo]
            state.frame.x[b] ^= active & _DEPOLARIZE2_B_X[combo]
            state.frame.z[b] ^= active & _DEPOLARIZE2_B_Z[combo]


@dataclass
class DetectorSampleResult:
    detectors: np.ndarray  # shape (shots, num_detectors), bool
    observables: np.ndarray  # shape (shots, num_observables), bool


def sample_detectors(
    circuit: stim.Circuit,
    *,
    shots: int,
    seed: int,
    reference_seed: int | None = None,
) -> DetectorSampleResult:
    """The Tier-2/Tier-4 entry point: sample `shots` shots of detector/observable data.

    `seed` drives the noise PRNG (numpy Philox, counter-based per the
    project brief); `reference_seed` (defaults to `seed`) drives the
    one-off deterministic reference run. Different seeds are accepted
    separately because the reference is computed once and reused across
    every shot, so reusing `seed` for it is a choice, not a requirement.
    """
    num_qubits = circuit.num_qubits
    reference = compute_reference_measurements(circuit, seed=seed if reference_seed is None else reference_seed)
    state = _RunState(
        frame=_Frame.zeros(num_qubits, shots),
        shots=shots,
        rng=np.random.Generator(np.random.Philox(seed=seed)),
        reference=reference,
    )
    _run(circuit, state, path=(), tick=[0])
    num_obs = (max(state.observables) + 1) if state.observables else 0
    obs_cols = [state.observables.get(i, np.zeros(shots, dtype=bool)) for i in range(num_obs)]
    return DetectorSampleResult(
        detectors=np.stack(state.detectors, axis=1) if state.detectors else np.zeros((shots, 0), dtype=bool),
        observables=np.stack(obs_cols, axis=1) if obs_cols else np.zeros((shots, 0), dtype=bool),
    )


def sample_single_fault(
    circuit: stim.Circuit,
    *,
    path: tuple[tuple[int, int], ...],
    qubit_paulis: tuple[tuple[int, str], ...],
    seed: int = 0,
) -> DetectorSampleResult:
    """Tier 3: force exactly one Pauli fault at a specific circuit location, no other noise.

    `path` and `qubit_paulis` come straight from a
    `stim.CircuitErrorLocation` (`.stack_frames` and `.flipped_pauli_product`
    respectively) as produced by
    `circuit.explain_detector_error_model_errors(...)` -- see
    tests/test_softmodel_validation.py for the full Tier-3 harness that
    enumerates every DEM error mechanism and calls this once per mechanism.
    """
    num_qubits = circuit.num_qubits
    reference = compute_reference_measurements(circuit, seed=seed)
    inject = _FaultInjection(path=path, qubit_paulis=qubit_paulis)
    state = _RunState(
        frame=_Frame.zeros(num_qubits, 1),
        shots=1,
        rng=None,
        reference=reference,
        inject=inject,
    )
    _run(circuit, state, path=(), tick=[0])
    if not inject.applied:
        raise AssertionError(f"fault injection path {path} was never reached while walking the circuit")
    num_obs = (max(state.observables) + 1) if state.observables else 0
    obs_cols = [state.observables.get(i, np.zeros(1, dtype=bool)) for i in range(num_obs)]
    return DetectorSampleResult(
        detectors=np.stack(state.detectors, axis=1) if state.detectors else np.zeros((1, 0), dtype=bool),
        observables=np.stack(obs_cols, axis=1) if obs_cols else np.zeros((1, 0), dtype=bool),
    )


def build_single_fault_circuit(
    circuit: stim.Circuit,
    *,
    path: tuple[tuple[int, int], ...],
    qubit_paulis: tuple[tuple[int, str], ...],
) -> stim.Circuit:
    """Tier 3 on real hardware: same fault-location semantics as
    sample_single_fault (path/qubit_paulis come from the same
    stim.CircuitErrorLocation fields -- see that function's docstring),
    but builds an actual stim.Circuit with the fault as an explicit,
    forced (p=1) noise instruction, instead of interpreting it directly.
    Feed the result through kernel.isa.encode_circuit unmodified and run
    it on the kernel (C-sim or real hardware) -- since the kernel has no
    separate "fault injection mode", this is what lets Tier 3 exercise
    the real kernel/hardware instead of only the soft model.

    REPEAT blocks are unrolled into the output (matching
    kernel.isa.encode_circuit's own convention) rather than preserved --
    the output only needs the same *instruction order* as the original,
    not the same syntactic structure.
    """
    out = stim.Circuit()
    applied = False

    def walk(body: stim.Circuit, current_path: tuple[tuple[int, int], ...], pending_iteration: int) -> None:
        nonlocal applied
        for offset, instruction in enumerate(body):
            if isinstance(instruction, stim.CircuitRepeatBlock):
                frame = (offset, pending_iteration)
                for it in range(instruction.repeat_count):
                    walk(instruction.body_copy(), current_path + (frame,), it)
                continue

            name = instruction.name
            here = current_path + ((offset, pending_iteration),)

            if name in _ALL_NOISE:
                if here == path:
                    for qubit, pauli in qubit_paulis:
                        out.append(f"{pauli}_ERROR", [qubit], [1.0])
                    applied = True
                continue  # every other noise instruction is stripped, matching _strip_noise
            out.append(instruction)

    walk(circuit, (), 0)
    if not applied:
        raise AssertionError(f"fault injection path {path} was never reached while walking the circuit")
    return out


def noiseless_detectors_are_all_zero(circuit: stim.Circuit, *, shots: int = 256, seed: int = 0) -> bool:
    """Tier 1: with all noise stripped, every detector must read zero on every shot.

    Runs *this* soft model (not upstream Stim) on the noise-stripped
    circuit, so it directly exercises the frame/detector-fold logic above
    rather than merely restating a fact about Stim's own sampler.
    """
    result = sample_detectors(_strip_noise(circuit), shots=shots, seed=seed)
    return not result.detectors.any()
