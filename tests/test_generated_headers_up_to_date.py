"""The kernel and the host/soft model can't disagree about an opcode
value or a config constant -- that's the whole point of generating
kernel/config.hpp, kernel/opcodes.hpp, and kernel/depolarize2_table.hpp
from Python sources (see kernel/generate_headers.py). This test is what
actually enforces that: it fails if someone edits python/stim_u55c/config.py
or kernel/isa.py without re-running the generator and committing the result.
"""

from __future__ import annotations

from pathlib import Path

import generate_headers

_KERNEL_DIR = Path(__file__).resolve().parent.parent / "kernel"


def test_generated_headers_match_source_of_truth():
    checks = {
        "config.hpp": generate_headers.generate_config_header(),
        "opcodes.hpp": generate_headers.isa.generate_opcodes_header(),
        "depolarize2_table.hpp": generate_headers.generate_depolarize2_table_header(),
    }
    stale = [name for name, expected in checks.items() if (_KERNEL_DIR / name).read_text() != expected]
    assert not stale, (
        f"{stale} out of date -- run `python3 kernel/generate_headers.py` and commit the result"
    )
