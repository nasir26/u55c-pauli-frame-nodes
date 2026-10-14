"""Compiles and runs the per-module C-sim testbenches in
kernel/hls_testbench/ (plain g++, no Vitis needed -- see
kernel/ap_uint_shim.hpp) so they're part of the one-command test suite
alongside the Python-side tiers, not a separate manual step.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_KERNEL_DIR = Path(__file__).resolve().parent.parent / "kernel"
_TESTBENCH_DIR = _KERNEL_DIR / "hls_testbench"

_UNIT_TESTBENCHES = ["tb_prng", "tb_gate_ops"]


@pytest.mark.parametrize("name", _UNIT_TESTBENCHES)
def test_kernel_unit_testbench(name, tmp_path):
    binary = tmp_path / name
    subprocess.run(
        [
            "g++", "-std=c++17", "-O2",
            "-Wno-unknown-pragmas", "-Wno-unused-label",
            str(_TESTBENCH_DIR / f"{name}.cpp"),
            "-I", str(_KERNEL_DIR),
            "-o", str(binary),
        ],
        check=True,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, f"{name} failed:\n{result.stdout}\n{result.stderr}"
