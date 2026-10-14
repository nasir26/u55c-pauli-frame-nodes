"""Kernel ISA: opcode numbering, instruction encoding, and the
stim.Circuit -> flat instruction stream compiler.

This is the single source of truth for what an opcode value means --
kernel/opcodes.hpp is generated from Opcode below (see
kernel/generate_headers.py) specifically so the kernel and the host/
softmodel side can never disagree about an opcode's numeric value. A
disagreement here would show up as a Tier 2 failure with no obvious cause,
which is exactly the failure mode this file exists to rule out.

Design notes:

  - Every stim broadcast instruction (e.g. "CX 0 1 2 3", two independent
    CNOTs) is un-broadcast at compile time into one Instruction record per
    individual 1- or 2-qubit operation. Uniform, fixed-width records are
    what makes a pipelined HLS loop over them straightforward.

  - Detector/observable folding is resolved entirely here, at compile
    time, not at kernel runtime. Every rec[-k] target on a DETECTOR or
    OBSERVABLE_INCLUDE instruction is resolved to the absolute measurement
    index it refers to (same resolution softmodel/reference_sampler.py
    does at interpretation time -- see that module's docstring), and the
    corresponding *earlier* Instruction record (the MEASURE-type
    instruction for that measurement) is patched in place to carry the
    detector/observable bit(s) it must fold into. The kernel therefore
    never needs a measurement-history buffer: it folds each measurement's
    outcome into its accumulators the instant the outcome is known, and
    moves on. This is Stim section 5.6 as a single streaming pass, with
    the compile-time-vs-runtime split doing the work that a runtime
    lookback ring buffer would otherwise have to do.

  - REPEAT blocks are unrolled here (not left for the kernel to loop over)
    for the same reason broadcasts are un-broadcast: a flat instruction
    stream is what a straightforward pipelined kernel loop wants. This
    does mean instruction-stream size scales with round count; that's a
    deliberate simplicity-over-density tradeoff for Phase 2, revisited if
    Phase 3/4 profiling says it matters.
"""

from __future__ import annotations

import itertools
import struct
from dataclasses import dataclass, field
from enum import IntEnum

import stim

from stim_u55c.config import (
    NUM_DETECTOR_BYTES,
    NUM_DETECTORS_MAX,
    NUM_LAYERS_MAX,
    NUM_OBSERVABLE_BYTES,
    NUM_OBSERVABLES_MAX,
    NUM_QUBITS_MAX,
)


class Opcode(IntEnum):
    NOP = 0
    RESET_Z = 1
    RESET_X = 2
    RESET_Y = 3
    MEASURE_Z = 4
    MEASURE_X = 5
    MEASURE_Y = 6
    MEASURE_RESET_Z = 7
    MEASURE_RESET_X = 8
    MEASURE_RESET_Y = 9
    GATE_H = 10
    GATE_S = 11
    GATE_CX = 12
    GATE_CZ = 13
    GATE_SWAP = 14
    NOISE_X = 15
    NOISE_Y = 16
    NOISE_Z = 17
    NOISE_DEPOLARIZE1 = 18
    NOISE_DEPOLARIZE2 = 19
    END_OF_PROGRAM = 255


MEASURE_OPCODES = {
    Opcode.MEASURE_Z, Opcode.MEASURE_X, Opcode.MEASURE_Y,
    Opcode.MEASURE_RESET_Z, Opcode.MEASURE_RESET_X, Opcode.MEASURE_RESET_Y,
}
NOISE_OPCODES = {
    Opcode.NOISE_X, Opcode.NOISE_Y, Opcode.NOISE_Z,
    Opcode.NOISE_DEPOLARIZE1, Opcode.NOISE_DEPOLARIZE2,
}

_SINGLE_QUBIT_DETERMINISTIC = {
    "H": Opcode.GATE_H,
    "S": Opcode.GATE_S, "S_DAG": Opcode.GATE_S,
    "SQRT_Z": Opcode.GATE_S, "SQRT_Z_DAG": Opcode.GATE_S,
}
_TWO_QUBIT_DETERMINISTIC = {
    "CX": Opcode.GATE_CX, "CNOT": Opcode.GATE_CX,
    "CZ": Opcode.GATE_CZ,
    "SWAP": Opcode.GATE_SWAP,
}
_RESET = {"R": Opcode.RESET_Z, "RX": Opcode.RESET_X, "RY": Opcode.RESET_Y}
_MEASURE = {"M": Opcode.MEASURE_Z, "MZ": Opcode.MEASURE_Z, "MX": Opcode.MEASURE_X, "MY": Opcode.MEASURE_Y}
_MEASURE_RESET = {
    "MR": Opcode.MEASURE_RESET_Z, "MZR": Opcode.MEASURE_RESET_Z,
    "MRX": Opcode.MEASURE_RESET_X, "MRY": Opcode.MEASURE_RESET_Y,
}
_NOISE_1Q = {"X_ERROR": Opcode.NOISE_X, "Y_ERROR": Opcode.NOISE_Y, "Z_ERROR": Opcode.NOISE_Z}
_ALL_NOISE_NAMES = set(_NOISE_1Q) | {"DEPOLARIZE1", "DEPOLARIZE2"}
_IGNORED = {"QUBIT_COORDS", "SHIFT_COORDS", "TICK"}

NO_QUBIT = 0xFF
PROB_SCALE = 2**32  # prob_threshold = round(p * PROB_SCALE); kernel/replay compare a uint32 draw against this.

# The 15 nontrivial two-qubit Pauli combinations DEPOLARIZE2 chooses
# among (each with probability p/15), indexed by word1 % 15. Single
# source of truth for both softmodel/kernel_replay.py and the C++
# kernel's depolarize2_table.hpp (generated from this list by
# generate_headers.py) -- these *must* agree on the index -> combo
# mapping, and a hand-transcribed C++ table previously disagreed with
# this one silently until Tier 2 caught it.
DEPOLARIZE2_COMBOS = [(a, b) for a, b in itertools.product("IXYZ", repeat=2) if (a, b) != ("I", "I")]

# opcode, qubit_a, qubit_b, reserved, prob_threshold, detector_mask[NUM_DETECTOR_BYTES], observable_mask[NUM_OBSERVABLE_BYTES]
_STRUCT = struct.Struct(f"<BBBxI{NUM_DETECTOR_BYTES}s{NUM_OBSERVABLE_BYTES}s")
RECORD_SIZE = _STRUCT.size


@dataclass
class Instruction:
    index: int  # position in the final flat stream; also the PRNG counter's instruction component
    opcode: Opcode
    qubit_a: int = NO_QUBIT
    qubit_b: int = NO_QUBIT
    prob_threshold: int = 0
    detector_mask: int = 0
    observable_mask: int = 0
    layer: int = 0  # scheduling metadata only (see _layer_and_reorder); not serialized

    def pack(self) -> bytes:
        return _STRUCT.pack(
            int(self.opcode),
            self.qubit_a,
            self.qubit_b,
            self.prob_threshold,
            self.detector_mask.to_bytes(NUM_DETECTOR_BYTES, "little"),
            self.observable_mask.to_bytes(NUM_OBSERVABLE_BYTES, "little"),
        )


_LAYER_OFFSET_STRUCT = struct.Struct("<I")


@dataclass
class Program:
    instructions: list[Instruction] = field(default_factory=list)
    num_detectors: int = 0
    num_observables: int = 0
    # layer_offsets[L] is the start index (into `instructions`) of layer L;
    # layer_offsets[-1] == len(instructions), a sentinel so the kernel's
    # outer loop can compute every layer's length as offsets[L+1]-offsets[L]
    # uniformly. See _layer_and_reorder for how these are computed.
    layer_offsets: list[int] = field(default_factory=lambda: [0])

    @property
    def num_layers(self) -> int:
        return len(self.layer_offsets) - 1

    def serialize(self) -> bytes:
        return b"".join(instr.pack() for instr in self.instructions)

    def serialize_layer_offsets(self) -> bytes:
        return b"".join(_LAYER_OFFSET_STRUCT.pack(o) for o in self.layer_offsets)


def _prob_threshold(p: float) -> int:
    t = round(p * PROB_SCALE)
    return max(0, min(PROB_SCALE - 1, t))


def encode_circuit(
    circuit: stim.Circuit, *, max_qubits: int = NUM_QUBITS_MAX, max_detectors: int = NUM_DETECTORS_MAX
) -> Program:
    """`max_qubits` / `max_detectors` default to this kernel's limits; the
    batch kernel (kernel/isa_batch.py) renumbers qubits densely afterwards and
    checks its own limits, so it passes larger values here (qubit ids must
    still fit the 8 bit record field, i.e. stay below NO_QUBIT)."""
    if circuit.num_qubits > max_qubits:
        raise ValueError(f"circuit uses {circuit.num_qubits} qubits, exceeds NUM_QUBITS_MAX")

    program = Program()
    meas_index_to_instruction: dict[int, Instruction] = {}
    state = {"num_meas": 0, "num_detectors": 0}

    def emit(opcode, qubit_a=NO_QUBIT, qubit_b=NO_QUBIT, prob_threshold=0) -> Instruction:
        instr = Instruction(
            index=len(program.instructions),
            opcode=opcode,
            qubit_a=qubit_a,
            qubit_b=qubit_b,
            prob_threshold=prob_threshold,
        )
        program.instructions.append(instr)
        return instr

    def fold_measurement_rec_targets(targets, kind: str, ident: int) -> None:
        now = state["num_meas"]
        for t in targets:
            if not t.is_measurement_record_target:
                continue
            abs_idx = now + t.value  # t.value is a negative lookback, e.g. -2
            instr = meas_index_to_instruction[abs_idx]
            if kind == "detector":
                instr.detector_mask |= 1 << ident
            else:
                instr.observable_mask |= 1 << ident

    def walk(body: stim.Circuit) -> None:
        for instruction in body:
            if isinstance(instruction, stim.CircuitRepeatBlock):
                for _ in range(instruction.repeat_count):
                    walk(instruction.body_copy())
                continue

            name = instruction.name
            targets = instruction.targets_copy()
            args = instruction.gate_args_copy()

            if name in _IGNORED:
                continue
            if name in _SINGLE_QUBIT_DETERMINISTIC:
                op = _SINGLE_QUBIT_DETERMINISTIC[name]
                for t in targets:
                    emit(op, qubit_a=t.value)
            elif name in _TWO_QUBIT_DETERMINISTIC:
                op = _TWO_QUBIT_DETERMINISTIC[name]
                for i in range(0, len(targets), 2):
                    emit(op, qubit_a=targets[i].value, qubit_b=targets[i + 1].value)
            elif name in _RESET:
                op = _RESET[name]
                for t in targets:
                    emit(op, qubit_a=t.value)
            elif name in _MEASURE:
                op = _MEASURE[name]
                for t in targets:
                    instr = emit(op, qubit_a=t.value)
                    meas_index_to_instruction[state["num_meas"]] = instr
                    state["num_meas"] += 1
            elif name in _MEASURE_RESET:
                op = _MEASURE_RESET[name]
                for t in targets:
                    instr = emit(op, qubit_a=t.value)
                    meas_index_to_instruction[state["num_meas"]] = instr
                    state["num_meas"] += 1
            elif name in _NOISE_1Q:
                op = _NOISE_1Q[name]
                thr = _prob_threshold(args[0])
                for t in targets:
                    emit(op, qubit_a=t.value, prob_threshold=thr)
            elif name == "DEPOLARIZE1":
                thr = _prob_threshold(args[0])
                for t in targets:
                    emit(Opcode.NOISE_DEPOLARIZE1, qubit_a=t.value, prob_threshold=thr)
            elif name == "DEPOLARIZE2":
                thr = _prob_threshold(args[0])
                for i in range(0, len(targets), 2):
                    emit(
                        Opcode.NOISE_DEPOLARIZE2,
                        qubit_a=targets[i].value,
                        qubit_b=targets[i + 1].value,
                        prob_threshold=thr,
                    )
            elif name == "DETECTOR":
                det_id = state["num_detectors"]
                state["num_detectors"] += 1
                if det_id >= max_detectors:
                    raise ValueError(f"circuit has >= {NUM_DETECTORS_MAX} detectors, exceeds NUM_DETECTORS_MAX")
                fold_measurement_rec_targets(targets, "detector", det_id)
            elif name == "OBSERVABLE_INCLUDE":
                obs_id = int(args[0])
                if obs_id >= NUM_OBSERVABLES_MAX:
                    raise ValueError(f"observable index {obs_id} exceeds NUM_OBSERVABLES_MAX")
                program.num_observables = max(program.num_observables, obs_id + 1)
                fold_measurement_rec_targets(targets, "observable", obs_id)
            else:
                raise NotImplementedError(f"isa encoder does not yet handle instruction {name!r}")

    walk(circuit)
    emit(Opcode.END_OF_PROGRAM)
    program.num_detectors = state["num_detectors"]
    _layer_and_reorder(program)
    return program


def _layer_and_reorder(program: Program) -> None:
    """Project brief section 3.1's "hard problem": partition the
    instruction stream into layers of mutually qubit-disjoint
    instructions, so the kernel can pipeline at II=1 within a layer with
    no runtime hazard checking, draining only between layers.

    Greedy, single pass over the program in its existing (compiler)
    order: each instruction goes in the earliest layer after every layer
    already used by any qubit it touches. This is always correct for
    this kernel's dependency structure specifically, because an
    instruction's effect here depends only on the frame state of its own
    qubit(s) (see gate_ops.hpp / prng.hpp) -- there is no dependency to
    preserve *except* same-qubit relative order, and tracking
    last-layer-used per qubit is exactly what preserves that. A stable
    sort by layer then reorders `program.instructions` in place; because
    it's stable and per-qubit ordering is never violated across layers,
    every qubit's own instruction sequence is untouched by the reorder,
    which is what makes the reorder semantically invisible in the
    output (softmodel/kernel_replay.py needs no changes to replay it
    correctly -- it just iterates program.instructions in whichever
    order they end up in).

    This is a Python prototype of what host/scheduler.cpp will eventually
    do for the real XRT runtime (Phase 4); the algorithm doesn't change,
    only the language it's implemented in.
    """
    last_layer_used: dict[int, int] = {}
    for instr in program.instructions:
        touched = [q for q in (instr.qubit_a, instr.qubit_b) if q != NO_QUBIT]
        if not touched:
            instr.layer = 0
            continue
        layer = max((last_layer_used.get(q, -1) for q in touched), default=-1) + 1
        instr.layer = layer
        for q in touched:
            last_layer_used[q] = layer

    program.instructions.sort(key=lambda instr: instr.layer)  # stable: ties keep relative order

    num_layers = (program.instructions[-1].layer + 1) if program.instructions else 0
    if num_layers > NUM_LAYERS_MAX:
        raise ValueError(f"program needs {num_layers} layers, exceeds NUM_LAYERS_MAX")

    offsets = [0] * (num_layers + 1)
    for position, instr in enumerate(program.instructions):
        instr.index = position  # PRNG counter component: must match final position, see Instruction docstring
        offsets[instr.layer + 1] = position + 1  # running "one past the last instruction of this layer so far"
    program.layer_offsets = offsets


def generate_opcodes_header() -> str:
    lines = [
        "// GENERATED FILE -- do not edit by hand. Regenerate with:",
        "//   python3 kernel/generate_headers.py",
        "// Source of truth: kernel/isa.py:Opcode",
        "#pragma once",
        "#include <cstdint>",
        "",
        "namespace stim_u55c {",
        "",
        "enum Opcode : uint8_t {",
    ]
    for member in Opcode:
        lines.append(f"    OPCODE_{member.name} = {int(member)},")
    lines.append("};")
    lines.append("")
    lines.append("}  // namespace stim_u55c")
    lines.append("")
    return "\n".join(lines)
