#!/usr/bin/env python3
"""Regenerates kernel/config.hpp and kernel/opcodes.hpp from their single
sources of truth (python/stim_u55c/config.py and kernel/isa.py:Opcode).

Run after changing either source. CI checks both generated files are
up to date (see .github/workflows/ci.yml) so the kernel and host/soft
model can never silently disagree about a constant or an opcode value.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "python"))
sys.path.insert(0, str(_REPO_ROOT / "kernel"))

import isa  # noqa: E402
from stim_u55c import config  # noqa: E402


def generate_config_header() -> str:
    return "\n".join(
        [
            "// GENERATED FILE -- do not edit by hand. Regenerate with:",
            "//   python3 kernel/generate_headers.py",
            "// Source of truth: python/stim_u55c/config.py",
            "#pragma once",
            "",
            "namespace stim_u55c {",
            "",
            f"constexpr int SHOTS = {config.SHOTS};",
            f"constexpr int NUM_QUBITS_MAX = {config.NUM_QUBITS_MAX};",
            f"constexpr int NUM_DETECTORS_MAX = {config.NUM_DETECTORS_MAX};",
            f"constexpr int NUM_OBSERVABLES_MAX = {config.NUM_OBSERVABLES_MAX};",
            f"constexpr int NUM_LAYERS_MAX = {config.NUM_LAYERS_MAX};",
            f"constexpr int NUM_DETECTOR_BYTES = {config.NUM_DETECTOR_BYTES};",
            f"constexpr int NUM_OBSERVABLE_BYTES = {config.NUM_OBSERVABLE_BYTES};",
            f"constexpr int RECORD_SIZE = {isa.RECORD_SIZE};",
            f"constexpr unsigned char NO_QUBIT = {isa.NO_QUBIT};",
            "",
            "}  // namespace stim_u55c",
            "",
        ]
    )


def generate_depolarize2_table_header() -> str:
    has_x = {"I": 0, "X": 1, "Y": 1, "Z": 0}
    has_z = {"I": 0, "X": 0, "Y": 1, "Z": 1}
    combos = isa.DEPOLARIZE2_COMBOS

    def row(fn):
        return ", ".join(str(fn(a, b)) for a, b in combos)

    return "\n".join(
        [
            "// GENERATED FILE -- do not edit by hand. Regenerate with:",
            "//   python3 kernel/generate_headers.py",
            "// Source of truth: kernel/isa.py:DEPOLARIZE2_COMBOS",
            "//",
            "// DEPOLARIZE2's 15 nontrivial two-qubit Pauli combinations, indexed",
            "// by word1 % 15 (see stim_frame_sampler.cpp's depolarize2_combo()).",
            "// This used to be a hand-transcribed table that silently disagreed",
            "// with the Python side's actual itertools.product order -- Tier 2",
            "// caught it. Generating both from one list is what rules that class",
            "// of bug out for good.",
            "#pragma once",
            "#include <cstdint>",
            "",
            "namespace stim_u55c {",
            "",
            f"constexpr uint8_t kDepolarize2AHasX[15] = {{{row(lambda a, b: has_x[a])}}};",
            f"constexpr uint8_t kDepolarize2AHasZ[15] = {{{row(lambda a, b: has_z[a])}}};",
            f"constexpr uint8_t kDepolarize2BHasX[15] = {{{row(lambda a, b: has_x[b])}}};",
            f"constexpr uint8_t kDepolarize2BHasZ[15] = {{{row(lambda a, b: has_z[b])}}};",
            "",
            "}  // namespace stim_u55c",
            "",
        ]
    )


def main() -> None:
    kernel_dir = _REPO_ROOT / "kernel"
    (kernel_dir / "config.hpp").write_text(generate_config_header())
    (kernel_dir / "opcodes.hpp").write_text(isa.generate_opcodes_header())
    (kernel_dir / "depolarize2_table.hpp").write_text(generate_depolarize2_table_header())
    print("wrote kernel/config.hpp, kernel/opcodes.hpp, kernel/depolarize2_table.hpp")


if __name__ == "__main__":
    main()
