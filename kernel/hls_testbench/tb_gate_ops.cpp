// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// C-sim testbench for gate_ops.hpp: each gate against its hand-derived
// symplectic transform on a single-qubit or two-qubit Pauli frame, using
// shot lane 0 only (frame state doesn't interact across shot lanes, so
// one lane is enough to check the bit logic; frame_store.hpp's width is
// what makes it apply to all SHOTS lanes at once in the real kernel).
#include "../frame_store.hpp"
#include "../gate_ops.hpp"
#include <cstdio>

using namespace stim_u55c;

namespace {
int failures = 0;

void check(bool cond, const char *label) {
    if (!cond) {
        std::printf("FAIL: %s\n", label);
        failures++;
    }
}

FrameStore fresh() {
    FrameStore fs;
    fs.reset_all();
    return fs;
}
}  // namespace

int main() {
    // H: X <-> Z.
    {
        FrameStore fs = fresh();
        fs.x[0][0] = 1;  // X error on qubit 0
        gate_h(fs, 0);
        check(fs.x[0][0] == 0 && fs.z[0][0] == 1, "H: X -> Z");
    }
    {
        FrameStore fs = fresh();
        fs.z[0][0] = 1;  // Z error
        gate_h(fs, 0);
        check(fs.x[0][0] == 1 && fs.z[0][0] == 0, "H: Z -> X");
    }

    // S: X -> Y -> X (z ^= x, applied twice returns to X since XOR is an involution).
    {
        FrameStore fs = fresh();
        fs.x[0][0] = 1;  // X
        gate_s(fs, 0);
        check(fs.x[0][0] == 1 && fs.z[0][0] == 1, "S: X -> Y");
        gate_s(fs, 0);
        check(fs.x[0][0] == 1 && fs.z[0][0] == 0, "S: Y -> X");
    }
    {
        FrameStore fs = fresh();
        fs.z[0][0] = 1;  // Z is a fixed point of S
        gate_s(fs, 0);
        check(fs.x[0][0] == 0 && fs.z[0][0] == 1, "S: Z -> Z");
    }

    // CX(control=0, target=1): X propagates control->target, Z propagates target->control.
    {
        FrameStore fs = fresh();
        fs.x[0][0] = 1;
        gate_cx(fs, 0, 1);
        check(fs.x[0][0] == 1 && fs.x[1][0] == 1 && fs.z[0][0] == 0 && fs.z[1][0] == 0,
              "CX: X on control propagates to target");
    }
    {
        FrameStore fs = fresh();
        fs.z[1][0] = 1;
        gate_cx(fs, 0, 1);
        check(fs.z[0][0] == 1 && fs.z[1][0] == 1 && fs.x[0][0] == 0 && fs.x[1][0] == 0,
              "CX: Z on target propagates to control");
    }

    // CZ(a=0, b=1): X on either qubit induces Z on the other; X components unchanged.
    {
        FrameStore fs = fresh();
        fs.x[0][0] = 1;
        gate_cz(fs, 0, 1);
        check(fs.x[0][0] == 1 && fs.x[1][0] == 0 && fs.z[0][0] == 0 && fs.z[1][0] == 1,
              "CZ: X on a induces Z on b");
    }

    // SWAP: full (x,z) pair exchanged.
    {
        FrameStore fs = fresh();
        fs.x[0][0] = 1;
        fs.z[1][0] = 1;
        gate_swap(fs, 0, 1);
        check(fs.x[0][0] == 0 && fs.z[0][0] == 1 && fs.x[1][0] == 1 && fs.z[1][0] == 0, "SWAP: exchanges (x,z) pairs");
    }

    // reset_qubit: zeroes both regardless of prior state.
    {
        FrameStore fs = fresh();
        fs.x[0][0] = 1;
        fs.z[0][0] = 1;
        reset_qubit(fs, 0);
        check(fs.x[0][0] == 0 && fs.z[0][0] == 0, "reset_qubit: zeroes x and z");
    }

    if (failures == 0) {
        std::printf("tb_gate_ops: PASS\n");
        return 0;
    }
    std::printf("tb_gate_ops: FAIL (%d failure(s))\n", failures);
    return 1;
}
