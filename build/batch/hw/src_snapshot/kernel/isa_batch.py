"""Program format of the batch kernel (kernel/batch/stim_batch_sampler.cpp).

Author: Nasir Ali, C-DAC Noida.

The circuit is compiled by kernel/isa.py:encode_circuit exactly as for the
first kernel (same un-broadcasting, same detector and observable resolution,
same instruction order), then turned
into the batch kernel's three inputs:

  - instructions: one 64 bit record each (layout in batch_config.hpp):
    opcode, qubit a, qubit b, a flag for two qubit instructions, the
    measurement slot of a measurement, and the 32 bit noise threshold.
    Qubits are renumbered densely in order of first use, so a circuit with
    sparse Stim qubit ids (the generated surface codes) needs only as many
    frame registers as qubits it actually uses.
  - fold entries: one 32 bit entry per (output, measurement) pair, grouped by
    output (detectors in order, then observables), the last entry of each
    output flagged; an output without any measurement gets one entry flagged
    as contributing nothing, so every output is emitted.
  - the counts the kernel takes as scalars.

The END_OF_PROGRAM marker of the first kernel is dropped (the batch kernel
takes a count). The instructions are then rescheduled for the frame stage,
which does not check hazards (see stim_batch_sampler.cpp): two instructions
on the same qubit must be at least HAZARD_DISTANCE apart, and the program
starts and ends with HAZARD_DISTANCE NOPs. The scheduler keeps the order of
the instructions on every qubit, which is all the frame semantics depend on
(instructions on disjoint qubits commute), and fills each slot with the
earliest instruction in compiler order that may issue, or a NOP. Finally the
instructions are renumbered by position, which is the instruction component
of the Philox counter in this kernel.

Measurement slots number the measurement instructions in stream order; any
numbering works as long as it is the same for the kernel and for the fold.
"""

from __future__ import annotations

import copy
import heapq
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import stim

from isa import MEASURE_OPCODES, NO_QUBIT, Instruction, Opcode, Program, encode_circuit

_CONFIG = Path(__file__).resolve().parent / "batch" / "batch_config.hpp"


def _read_config() -> dict[str, int]:
    vals: dict[str, int] = {}
    for name, expr in re.findall(r"constexpr int (\w+) = ([^;]+);", _CONFIG.read_text()):
        vals[name] = int(eval(expr, {}, vals))  # noqa: S307 (constant expressions of this repository's own header)
    return vals


CFG = _read_config()
LANES = CFG["LANES"]
_TWO_QUBIT = {Opcode.GATE_CX, Opcode.GATE_CZ, Opcode.GATE_SWAP, Opcode.NOISE_DEPOLARIZE2}


@dataclass
class BatchProgram:
    program: Program            # the encode_circuit program with qubits renumbered densely
    instructions: np.ndarray    # uint64 records
    fold: np.ndarray            # uint32 entries
    num_meas: int
    num_detectors: int
    num_observables: int
    num_qubits: int

    @property
    def num_out(self) -> int:
        return self.num_detectors + self.num_observables


def _qubits(ins: Instruction) -> tuple[int, ...]:
    return tuple(q for q in (ins.qubit_a, ins.qubit_b) if q != NO_QUBIT)


def schedule(instructions: list[Instruction], distance: int) -> list[Instruction]:
    """Hazard distance list scheduling (see the module docstring). Returns new list, NOPs included."""
    queues: dict[int, deque[int]] = {}
    for k, ins in enumerate(instructions):
        for q in _qubits(ins):
            queues.setdefault(q, deque()).append(k)
    last: dict[int, int] = {}            # qubit -> slot of its latest scheduled instruction
    pending = set(range(len(instructions)))
    out = [Instruction(index=0, opcode=Opcode.NOP) for _ in range(distance)]
    heads: list[int] = []                # instructions at the head of the queue of every qubit they touch

    def refresh(k: int) -> None:
        if all(queues[q][0] == k for q in _qubits(instructions[k])):
            heapq.heappush(heads, k)

    for k, ins in enumerate(instructions):
        if not _qubits(ins):
            pass
        elif all(queues[q][0] == k for q in _qubits(ins)):
            heapq.heappush(heads, k)
    free = [k for k, ins in enumerate(instructions) if not _qubits(ins)]  # no qubit (NOP): issue any time
    while pending:
        slot = len(out)
        waiting, chosen = [], None
        while heads:
            k = heapq.heappop(heads)
            if all(slot - last.get(q, -distance) >= distance for q in _qubits(instructions[k])):
                chosen = k
                break
            waiting.append(k)
        for k in waiting:
            heapq.heappush(heads, k)
        if chosen is None and free:
            chosen = free.pop(0)
        if chosen is None:
            out.append(Instruction(index=0, opcode=Opcode.NOP))
            continue
        out.append(instructions[chosen])
        pending.discard(chosen)
        for q in _qubits(instructions[chosen]):
            last[q] = slot
            queues[q].popleft()
            if queues[q]:
                refresh(queues[q][0])
    out += [Instruction(index=0, opcode=Opcode.NOP) for _ in range(distance)]
    return out


def compile_batch(circuit: stim.Circuit) -> BatchProgram:
    prog = copy.deepcopy(encode_circuit(circuit, max_qubits=NO_QUBIT, max_detectors=CFG["OUT_MAX"]))
    prog.instructions = schedule([ins for ins in prog.instructions if ins.opcode != Opcode.END_OF_PROGRAM],
                                 CFG["HAZARD_DISTANCE"])
    for k, ins in enumerate(prog.instructions):
        ins.index = k

    dense: dict[int, int] = {}
    for ins in prog.instructions:
        for attr in ("qubit_a", "qubit_b"):
            q = getattr(ins, attr)
            if q != NO_QUBIT:
                setattr(ins, attr, dense.setdefault(q, len(dense)))
    if len(dense) > CFG["QUBITS_MAX"]:
        raise ValueError(f"circuit uses {len(dense)} qubits, the batch kernel holds {CFG['QUBITS_MAX']}")
    if len(prog.instructions) > CFG["PROG_MAX"]:
        raise ValueError(f"{len(prog.instructions)} instructions, the batch kernel holds {CFG['PROG_MAX']}")

    num_det, num_obs = prog.num_detectors, prog.num_observables
    det_lists: list[list[int]] = [[] for _ in range(num_det)]
    obs_lists: list[list[int]] = [[] for _ in range(num_obs)]
    words = np.zeros(len(prog.instructions), dtype=np.uint64)
    slot = 0
    for k, ins in enumerate(prog.instructions):
        w = int(ins.opcode) << CFG["OP_LSB"]
        if ins.qubit_a != NO_QUBIT:
            w |= ins.qubit_a << CFG["QA_LSB"]
        if ins.opcode in _TWO_QUBIT:
            w |= ins.qubit_b << CFG["QB_LSB"]
            w |= 1 << CFG["B_VALID_BIT"]
        if ins.opcode in MEASURE_OPCODES:
            w |= slot << CFG["SLOT_LSB"]
            for d in range(ins.detector_mask.bit_length()):
                if ins.detector_mask >> d & 1:
                    det_lists[d].append(slot)
            for o in range(ins.observable_mask.bit_length()):
                if ins.observable_mask >> o & 1:
                    obs_lists[o].append(slot)
            slot += 1
        w |= ins.prob_threshold << CFG["THR_LSB"]
        words[k] = w
    if slot > CFG["MEAS_MAX"] or slot >= 1 << CFG["FOLD_MEAS_BITS"]:
        raise ValueError(f"{slot} measurements, the batch kernel holds {CFG['MEAS_MAX']}")
    if num_det + num_obs > CFG["OUT_MAX"]:
        raise ValueError(f"{num_det + num_obs} outputs, the batch kernel holds {CFG['OUT_MAX']}")

    entries: list[int] = []
    for lst in det_lists + obs_lists:
        if not lst:
            entries.append(1 << CFG["FOLD_NONE_BIT"] | 1 << CFG["FOLD_LAST_BIT"])
            continue
        for j, m in enumerate(lst):
            entries.append(m | ((1 << CFG["FOLD_LAST_BIT"]) if j == len(lst) - 1 else 0))
    if len(entries) > CFG["FOLD_MAX"]:
        raise ValueError(f"{len(entries)} fold entries, the batch kernel holds {CFG['FOLD_MAX']}")

    return BatchProgram(program=prog, instructions=words, fold=np.array(entries, dtype=np.uint32), num_meas=slot,
                        num_detectors=num_det, num_observables=num_obs, num_qubits=len(dense))
