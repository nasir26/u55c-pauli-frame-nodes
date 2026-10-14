// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Batch kernel: the same Pauli frame semantics as stim_frame_sampler.cpp
// (softmodel/kernel_replay.py is the oracle for both), restructured so that
// one instruction issues per clock cycle and one launch samples many
// batches of LANES shots. The four costs of the first kernel and what
// replaces them:
//
//   1. Unpipelined instruction loop. Here the frame of all qubits is held in
//      registers and the loop runs at II=1. Reading a qubit, updating it and
//      writing it back are three pipeline steps, so the frame stage does not
//      resolve hazards itself: kernel/isa_batch.py schedules every
//      program so that two instructions on the same qubit are at least
//      HAZARD_DISTANCE apart (inserting NOPs where the circuit has too little
//      parallelism). Every program also starts and ends with HAZARD_DISTANCE
//      NOPs, which write nothing: the frame is cleared in the first slot of
//      a batch, and the NOPs keep the last writes of the previous batch and
//      the first reads of this one clear of that clear.
//   2. Philox evaluated inside that loop. Noise masks depend only on the
//      instruction, the lane and the batch, never on the frame, so a separate
//      dataflow stage computes them ahead of use. Each Philox4x32-10 output
//      (128 bits) serves two lanes, (w0, w1) and (w2, w3), so PAIRS units feed
//      LANES lanes. One instance (64 lanes, 32 Philox units) fits the DSPs of
//      one SLR; a card runs one instance per SLR, each on its own batches.
//   3. A 41 byte instruction fetched from HBM per step. The program is copied
//      once per launch into on chip memory as 64 bit records.
//   4. A 256 step detector fold for every measurement. Measurements are
//      stored per batch, and a third stage XORs only the listed (output,
//      measurement) pairs while the frame stage already runs the next batch.
//
// Random stream: lane l of batch g (g counted from batch_base) at instruction
// i uses Philox4x32-10 with counter (i, l / 2, g, 0) and key (seed_lo,
// seed_hi); even lanes take (w0, w1), odd lanes (w2, w3). Noise decisions are
// those of the first kernel: active = first word < threshold, category =
// second word mod 3 (DEPOLARIZE1) or mod 15 (DEPOLARIZE2).
//
// Output: for every batch, num_out words of LANES bits (detectors, then
// observables), batch after batch, bit l of a word = shot l of the batch.
#include <ap_int.h>
#include <hls_stream.h>
#include <stdint.h>

#include "../opcodes.hpp"
#include "batch_config.hpp"

using namespace stim_batch;
using stim_u55c::Opcode;

typedef ap_uint<LANES> lanes_t;

namespace {

struct NoiseOp {
    ap_uint<64> word;
    lanes_t fxa, fza, fxb, fzb;
};

struct Effect {
    ap_uint<SLOT_BITS> slot;
    lanes_t value;
};

// DEPOLARIZE2 table of kernel/isa.py:DEPOLARIZE2_COMBOS as 15 bit masks: bit c
// is set when combination c has that Pauli component (same values as
// kernel/depolarize2_table.hpp).
constexpr unsigned kD2AX = 0x07F8;  // 0,0,0,1,1,1,1,1,1,1,1,0,0,0,0
constexpr unsigned kD2AZ = 0x7F80;  // 0,0,0,0,0,0,0,1,1,1,1,1,1,1,1
constexpr unsigned kD2BX = 0x3333;  // 1,1,0,0,1,1,0,0,1,1,0,0,1,1,0
constexpr unsigned kD2BZ = 0x6666;  // 0,1,1,0,0,1,1,0,0,1,1,0,0,1,1

inline void philox4x32_10(uint32_t c0, uint32_t c1, uint32_t c2, uint32_t c3, uint32_t k0, uint32_t k1,
                          uint32_t &w0, uint32_t &w1, uint32_t &w2, uint32_t &w3) {
#pragma HLS inline
ROUNDS:
    for (int r = 0; r < 10; r++) {
#pragma HLS unroll
        ap_uint<64> p0 = ap_uint<64>(0xD2511F53u) * ap_uint<64>(c0);
        ap_uint<64> p1 = ap_uint<64>(0xCD9E8D57u) * ap_uint<64>(c2);
        uint32_t hi0 = p0.range(63, 32), lo0 = p0.range(31, 0);
        uint32_t hi1 = p1.range(63, 32), lo1 = p1.range(31, 0);
        uint32_t n0 = hi1 ^ c1 ^ k0, n2 = hi0 ^ c3 ^ k1;
        c0 = n0;
        c1 = lo1;
        c2 = n2;
        c3 = lo0;
        k0 += 0x9E3779B9u;
        k1 += 0xBB67AE85u;
    }
    w0 = c0;
    w1 = c1;
    w2 = c2;
    w3 = c3;
}

// x mod 15 without a divider: 16 = 1 (mod 15), so x and the sum of its eight
// hex digits agree mod 15; the sum is at most 120 and one more fold brings it
// to 0..22.
inline ap_uint<4> mod15(uint32_t x) {
#pragma HLS inline
    ap_uint<7> s = 0;
    for (int d = 0; d < 8; d++) {
#pragma HLS unroll
        s += ap_uint<4>((x >> (4 * d)) & 0xF);
    }
    ap_uint<5> t = ap_uint<5>(s.range(3, 0)) + ap_uint<5>(s.range(6, 4));
    return t >= 15 ? ap_uint<4>(t - 15) : ap_uint<4>(t);
}

void load(const ap_uint<64> *prog, int num_instr, const ap_uint<32> *fold, int num_fold, ap_uint<64> P[PROG_MAX],
          ap_uint<32> F[FOLD_MAX]) {
LOAD_PROG:
    for (int i = 0; i < num_instr; i++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 8192
        P[i] = prog[i];
    }
LOAD_FOLD:
    for (int k = 0; k < num_fold; k++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 8192
        F[k] = fold[k];
    }
}

// Stage 1: noise masks for every instruction of every batch.
void gen_noise(const ap_uint<64> P[PROG_MAX], int num_instr, int num_batches, uint32_t batch_base, uint32_t seed_lo,
               uint32_t seed_hi, hls::stream<NoiseOp> &out) {
    int i = 0;
    uint32_t g = batch_base;
    const long total = (long)num_instr * num_batches;
NOISE:
    for (long t = 0; t < total; t++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 1000000
        ap_uint<64> w = P[i];
        unsigned op = w.range(OP_LSB + OP_BITS - 1, OP_LSB);
        uint32_t thr = w.range(63, THR_LSB);
        NoiseOp n;
        n.word = w;
    LANE_PAIRS:
        for (int j = 0; j < PAIRS; j++) {
#pragma HLS unroll
            uint32_t r[4];
            philox4x32_10(i, j, g, 0, seed_lo, seed_hi, r[0], r[1], r[2], r[3]);
            for (int h = 0; h < 2; h++) {
#pragma HLS unroll
                const int lane = 2 * j + h;
                const bool active = r[2 * h] < thr;
                const ap_uint<4> c15 = mod15(r[2 * h + 1]);
                const ap_uint<2> c3 = c15 % 3;
                bool xa = false, za = false, xb = false, zb = false;
                if (op == stim_u55c::OPCODE_NOISE_X) {
                    xa = active;
                } else if (op == stim_u55c::OPCODE_NOISE_Y) {
                    xa = active;
                    za = active;
                } else if (op == stim_u55c::OPCODE_NOISE_Z) {
                    za = active;
                } else if (op == stim_u55c::OPCODE_NOISE_DEPOLARIZE1) {
                    xa = active && (c3 == 0 || c3 == 1);
                    za = active && (c3 == 1 || c3 == 2);
                } else if (op == stim_u55c::OPCODE_NOISE_DEPOLARIZE2) {
                    xa = active && ((kD2AX >> c15) & 1);
                    za = active && ((kD2AZ >> c15) & 1);
                    xb = active && ((kD2BX >> c15) & 1);
                    zb = active && ((kD2BZ >> c15) & 1);
                }
                n.fxa[lane] = xa;
                n.fza[lane] = za;
                n.fxb[lane] = xb;
                n.fzb[lane] = zb;
            }
        }
        out.write(n);
        if (i == num_instr - 1) {
            i = 0;
            g++;
        } else {
            i++;
        }
    }
}

// Stage 2: the Pauli frame, all qubits in registers, as an explicit three step
// pipeline so that no cycle chains the 128 way register read, the gate logic
// and the write back: in iteration t the operands of instruction t are read,
// the new values of instruction t - 1 are computed, and those of instruction
// t - 2 are written. The read comes first in the body, so it sees the
// registers as they were at the start of the cycle. An instruction therefore
// sees the writes of instructions at least 3 slots before it, which the
// schedule guarantees (HAZARD_DISTANCE, see the top of this file); C
// simulation runs these same delayed semantics.
struct Operands {
    ap_uint<64> word;
    lanes_t xa, za, xb, zb, fxa, fza, fxb, fzb;
};

struct Update {
    ap_uint<QUBITS_MAX> sel_a, sel_b;  // one hot write enables, decoded in the compute step
    lanes_t xa, za, xb, zb;
};

void frame(int num_instr, int num_batches, hls::stream<NoiseOp> &in, hls::stream<Effect> &effects) {
    static_assert(HAZARD_DISTANCE >= 3, "the frame pipeline writes back two slots after the read");
    lanes_t X[QUBITS_MAX], Z[QUBITS_MAX];
#pragma HLS array_partition variable = X complete
#pragma HLS array_partition variable = Z complete
    for (int q = 0; q < QUBITS_MAX; q++) {
#pragma HLS unroll
        X[q] = 0;
        Z[q] = 0;
    }
    Operands rd;          // read in the previous iteration
    rd.word = 0;          // NOP
    rd.xa = rd.za = rd.xb = rd.zb = rd.fxa = rd.fza = rd.fxb = rd.fzb = 0;
    Update up;            // computed in the previous iteration
    up.sel_a = up.sel_b = 0;
    up.xa = up.za = up.xb = up.zb = 0;
    bool clear = false;   // registered: this iteration is the first slot of a batch
    int i = 0;
    const long total = (long)num_instr * num_batches;
FRAME:
    for (long t = 0; t < total; t++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 1000000
        // read: operands of instruction t
        NoiseOp n = in.read();
        Operands nrd;
        nrd.word = n.word;
        const ap_uint<Q_BITS> ra = n.word.range(QA_LSB + Q_BITS - 1, QA_LSB);
        const ap_uint<Q_BITS> rb = n.word.range(QB_LSB + Q_BITS - 1, QB_LSB);
        nrd.xa = X[ra];
        nrd.za = Z[ra];
        nrd.xb = X[rb];
        nrd.zb = Z[rb];
        nrd.fxa = n.fxa;
        nrd.fza = n.fza;
        nrd.fxb = n.fxb;
        nrd.fzb = n.fzb;

        // compute: new values of instruction t - 1
        const ap_uint<64> w = rd.word;
        const unsigned op = w.range(OP_LSB + OP_BITS - 1, OP_LSB);
        const lanes_t xa = rd.xa, za = rd.za, xb = rd.xb, zb = rd.zb;
        lanes_t nxa = xa ^ rd.fxa, nza = za ^ rd.fza, nxb = xb ^ rd.fxb, nzb = zb ^ rd.fzb;
        bool meas = false;
        lanes_t eff = 0;
        switch (op) {
            case stim_u55c::OPCODE_GATE_H:
                nxa = za;
                nza = xa;
                break;
            case stim_u55c::OPCODE_GATE_S:
                nza = za ^ xa;
                break;
            case stim_u55c::OPCODE_GATE_CX:
                nxb = xb ^ xa;
                nza = za ^ zb;
                break;
            case stim_u55c::OPCODE_GATE_CZ:
                nzb = zb ^ xa;
                nza = za ^ xb;
                break;
            case stim_u55c::OPCODE_GATE_SWAP:
                nxa = xb;
                nxb = xa;
                nza = zb;
                nzb = za;
                break;
            case stim_u55c::OPCODE_RESET_Z:
            case stim_u55c::OPCODE_RESET_X:
            case stim_u55c::OPCODE_RESET_Y:
                nxa = 0;
                nza = 0;
                break;
            case stim_u55c::OPCODE_MEASURE_Z:
            case stim_u55c::OPCODE_MEASURE_RESET_Z:
                eff = xa;
                meas = true;
                break;
            case stim_u55c::OPCODE_MEASURE_X:
            case stim_u55c::OPCODE_MEASURE_RESET_X:
                eff = za;
                meas = true;
                break;
            case stim_u55c::OPCODE_MEASURE_Y:
            case stim_u55c::OPCODE_MEASURE_RESET_Y:
                eff = xa ^ za;
                meas = true;
                break;
            default:
                break;  // noise and NOP: the masks above (zero for NOP)
        }
        if (op == stim_u55c::OPCODE_MEASURE_RESET_Z || op == stim_u55c::OPCODE_MEASURE_RESET_X ||
            op == stim_u55c::OPCODE_MEASURE_RESET_Y) {
            nxa = 0;
            nza = 0;
        }
        if (meas) {
            Effect e;
            e.slot = w.range(SLOT_LSB + SLOT_BITS - 1, SLOT_LSB);
            e.value = eff;
            effects.write(e);
        }
        Update nup;
        const ap_uint<Q_BITS> ua = w.range(QA_LSB + Q_BITS - 1, QA_LSB);
        const ap_uint<Q_BITS> ub = w.range(QB_LSB + Q_BITS - 1, QB_LSB);
        nup.sel_a = op != stim_u55c::OPCODE_NOP ? ap_uint<QUBITS_MAX>(ap_uint<QUBITS_MAX>(1) << ua) : ap_uint<QUBITS_MAX>(0);
        nup.sel_b = w[B_VALID_BIT] ? ap_uint<QUBITS_MAX>(ap_uint<QUBITS_MAX>(1) << ub) : ap_uint<QUBITS_MAX>(0);
        nup.xa = nxa;
        nup.za = nza;
        nup.xb = nxb;
        nup.zb = nzb;

        // write: instruction t - 2; at the first slot of a batch the frame is cleared instead (the program starts
        // and ends with NOPs, so nothing is pending across the boundary)
        for (int q = 0; q < QUBITS_MAX; q++) {
#pragma HLS unroll
            if (clear) {
                X[q] = 0;
                Z[q] = 0;
            } else if (up.sel_a[q]) {
                X[q] = up.xa;
                Z[q] = up.za;
            } else if (up.sel_b[q]) {
                X[q] = up.xb;
                Z[q] = up.zb;
            }
        }
        rd = nrd;
        up = nup;
        const bool last = i == num_instr - 1;
        clear = last;
        i = last ? 0 : i + 1;
    }
}

// Stage 3: per batch, collect the measurement effects, then XOR the listed
// measurements of every output.
void fold_outputs(const ap_uint<32> F[FOLD_MAX], int num_fold, int num_meas, int num_batches,
                  hls::stream<Effect> &effects, hls::stream<lanes_t> &outs) {
    lanes_t R[MEAS_MAX];
BATCHES:
    for (int g = 0; g < num_batches; g++) {
#pragma HLS loop_tripcount max = 100000
    COLLECT:
        for (int m = 0; m < num_meas; m++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 2048
            Effect e = effects.read();
            R[e.slot] = e.value;
        }
        lanes_t acc = 0;
    FOLD:
        for (int k = 0; k < num_fold; k++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 8192
            ap_uint<32> f = F[k];
            ap_uint<FOLD_MEAS_BITS> m = f.range(FOLD_MEAS_BITS - 1, 0);
            lanes_t v = f[FOLD_NONE_BIT] ? lanes_t(0) : R[m];
            lanes_t s = acc ^ v;
            if (f[FOLD_LAST_BIT]) {
                outs.write(s);
                acc = 0;
            } else {
                acc = s;
            }
        }
    }
}

// Stage 4: stream the outputs to HBM, batch after batch.
void store(int num_out, int num_batches, hls::stream<lanes_t> &outs, lanes_t *out) {
    const long total = (long)num_out * num_batches;
STORE:
    for (long t = 0; t < total; t++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 1000000
        out[t] = outs.read();
    }
}

void run(const ap_uint<64> P[PROG_MAX], int num_instr, const ap_uint<32> F[FOLD_MAX], int num_fold, int num_meas,
         int num_out, int num_batches, uint32_t batch_base, uint32_t seed_lo, uint32_t seed_hi, lanes_t *out) {
#pragma HLS dataflow
    hls::stream<NoiseOp, 8> noise;
    hls::stream<Effect, 1024> effects;
    hls::stream<lanes_t, 512> outs;
    gen_noise(P, num_instr, num_batches, batch_base, seed_lo, seed_hi, noise);
    frame(num_instr, num_batches, noise, effects);
    fold_outputs(F, num_fold, num_meas, num_batches, effects, outs);
    store(num_out, num_batches, outs, out);
}

}  // namespace

extern "C" void stim_batch_sampler(const ap_uint<64> *prog, int num_instr, const ap_uint<32> *fold, int num_fold,
                                   int num_meas, int num_out, int num_batches, uint32_t batch_base, uint32_t seed_lo,
                                   uint32_t seed_hi, lanes_t *out) {
#pragma HLS INTERFACE m_axi port = prog offset = slave bundle = gmem0 depth = PROG_MAX max_read_burst_length = 64
#pragma HLS INTERFACE m_axi port = fold offset = slave bundle = gmem0 depth = FOLD_MAX max_read_burst_length = 64
#pragma HLS INTERFACE m_axi port = out offset = slave bundle = gmem1 depth = OUT_DEPTH max_write_burst_length = 64
#pragma HLS INTERFACE s_axilite port = num_instr
#pragma HLS INTERFACE s_axilite port = num_fold
#pragma HLS INTERFACE s_axilite port = num_meas
#pragma HLS INTERFACE s_axilite port = num_out
#pragma HLS INTERFACE s_axilite port = num_batches
#pragma HLS INTERFACE s_axilite port = batch_base
#pragma HLS INTERFACE s_axilite port = seed_lo
#pragma HLS INTERFACE s_axilite port = seed_hi
#pragma HLS INTERFACE s_axilite port = return
    static ap_uint<64> P[PROG_MAX];
    static ap_uint<32> F[FOLD_MAX];
    load(prog, num_instr, fold, num_fold, P, F);
    run(P, num_instr, F, num_fold, num_meas, num_out, num_batches, batch_base, seed_lo, seed_hi, out);
}
