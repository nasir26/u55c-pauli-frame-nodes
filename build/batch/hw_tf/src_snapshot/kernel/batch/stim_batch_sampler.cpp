// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Batch kernel: the same Pauli frame semantics as stim_frame_sampler.cpp
// (softmodel/kernel_replay.py is the oracle for both), restructured so that
// one instruction issues per clock cycle and one launch samples many
// batches of LANES shots. The four costs of the first kernel and what
// replaces them:
//
//   1. Unpipelined instruction loop. Here the loop runs at II=1 over slots.
//      The frame is a small memory (LUTRAM) with one read and one write per
//      cycle, so a two qubit instruction occupies two consecutive slots,
//      reading qubit a and then qubit b; its new values are written in the
//      two following cycles. The result of slot t is written two cycles after
//      its read and the frame stage does not resolve hazards itself:
//      kernel/isa_batch.py schedules every program so that two slots on the
//      same qubit are at least HAZARD_DISTANCE apart (inserting NOPs where
//      the circuit has too little parallelism). Every program also starts and
//      ends with HAZARD_DISTANCE NOPs, which write nothing: the frame is
//      cleared in the first slot of a batch (a valid bit per qubit), and the
//      NOPs keep the last writes of the previous batch and the first reads of
//      this one clear of that clear. (An earlier variant held the frame in
//      registers with two reads and two writes per cycle; the register
//      crossbar congested routing.)
//   2. Philox evaluated inside that loop. Noise masks depend only on the
//      instruction, the lane and the batch, never on the frame, so a separate
//      dataflow stage computes them ahead of use, with a counter based
//      generator: each 128 bit output serves two lanes, (w0, w1) and
//      (w2, w3), so PAIRS units feed LANES lanes. The generator is
//      Threefry4x32-20 (add, rotate, xor) rather than the first kernel's
//      Philox4x32-10: 32 pipelined Philox units filled the DSP columns of
//      their region and congested routing, while Threefry needs no
//      multiplier and spreads over ordinary logic.
//   3. A 41 byte instruction fetched from HBM per step. The program is copied
//      once per launch into on chip memory as 64 bit records.
//   4. A 256 step detector fold for every measurement. Measurements are
//      stored per batch, and a third stage XORs only the listed (output,
//      measurement) pairs while the frame stage already runs the next batch.
//
// Random stream: lane l of batch g (g counted from batch_base) at slot i (the
// first slot of a two qubit instruction) uses Threefry4x32-20 with counter
// (i, l / 2, g, 0) and key (seed_lo, seed_hi, 0, 0); even lanes take
// (w0, w1), odd lanes (w2, w3) (softmodel/threefry.py, checked against the
// Random123 known answer and the randomgen implementation). Noise decisions are
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

// Threefry4x32-20 (Salmon et al. 2011, Random123): 20 rounds of add, rotate
// and xor, key injected every four rounds with the parity word 0x1BD11BDA.
inline uint32_t rotl32(uint32_t x, int r) {
#pragma HLS inline
    return (x << r) | (x >> (32 - r));
}

inline void threefry4x32_20(uint32_t c0, uint32_t c1, uint32_t c2, uint32_t c3, uint32_t k0, uint32_t k1,
                            uint32_t k2, uint32_t k3, uint32_t &w0, uint32_t &w1, uint32_t &w2, uint32_t &w3) {
#pragma HLS inline
    static const int R[8][2] = {{10, 26}, {11, 21}, {13, 27}, {23, 5}, {6, 20}, {17, 11}, {25, 10}, {18, 20}};
    const uint32_t ks[5] = {k0, k1, k2, k3, 0x1BD11BDAu ^ k0 ^ k1 ^ k2 ^ k3};
    uint32_t x0 = c0 + ks[0], x1 = c1 + ks[1], x2 = c2 + ks[2], x3 = c3 + ks[3];
ROUNDS:
    for (int r = 0; r < 20; r++) {
#pragma HLS unroll
        if (r % 2 == 0) {
            x0 += x1;
            x1 = rotl32(x1, R[r % 8][0]) ^ x0;
            x2 += x3;
            x3 = rotl32(x3, R[r % 8][1]) ^ x2;
        } else {
            x0 += x3;
            x3 = rotl32(x3, R[r % 8][0]) ^ x0;
            x2 += x1;
            x1 = rotl32(x1, R[r % 8][1]) ^ x2;
        }
        if (r % 4 == 3) {
            const int s = (r + 1) / 4;
            x0 += ks[s % 5];
            x1 += ks[(s + 1) % 5];
            x2 += ks[(s + 2) % 5];
            x3 += ks[(s + 3) % 5] + (uint32_t)s;
        }
    }
    w0 = x0;
    w1 = x1;
    w2 = x2;
    w3 = x3;
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
        // the second slot of a pair carries no masks: the frame stage uses those of the first slot
        unsigned op = w[PAIR_SECOND_BIT] ? 0u : (unsigned)w.range(OP_LSB + OP_BITS - 1, OP_LSB);
        uint32_t thr = w.range(63, THR_LSB);
        NoiseOp n;
        n.word = w;
    LANE_PAIRS:
        for (int j = 0; j < PAIRS; j++) {
#pragma HLS unroll
            uint32_t r[4];
            threefry4x32_20(i, j, g, 0, seed_lo, seed_hi, 0, 0, r[0], r[1], r[2], r[3]);
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

// Stage 2: the Pauli frame in a one read, one write memory. In iteration t
// the stage reads the qubit of slot t, then completes slot t - 2 from the
// operands read two and one iterations earlier: a single qubit slot is
// computed and written; the first slot of a pair computes both new values
// from the operands of a (slot t - 2) and of b (slot t - 1), writes a and
// keeps b, which the second slot writes one iteration later. The read comes
// first in the body, so slot t sees the writes of slots up to t - 3; the
// schedule (HAZARD_DISTANCE) makes that enough, and C simulation runs these
// same semantics.
struct Slot {
    NoiseOp n;
    lanes_t x, z;  // frame of the slot's qubit as read
};

void frame(int num_instr, int num_batches, hls::stream<NoiseOp> &in, hls::stream<Effect> &effects) {
    static_assert(HAZARD_DISTANCE >= 3, "the frame stage writes a slot two cycles after reading it");
    lanes_t X[QUBITS_MAX], Z[QUBITS_MAX];
#pragma HLS bind_storage variable = X type = ram_s2p impl = lutram
#pragma HLS bind_storage variable = Z type = ram_s2p impl = lutram
#pragma HLS dependence variable = X inter false
#pragma HLS dependence variable = Z inter false
    ap_uint<QUBITS_MAX> valid = 0;  // qubit written in this batch; an unwritten qubit reads as zero
    Slot s1, s0;                    // read two and one iterations ago
    s1.n.word = s0.n.word = 0;      // NOPs
    s1.x = s1.z = s0.x = s0.z = 0;
    s1.n.fxa = s1.n.fza = s1.n.fxb = s1.n.fzb = 0;
    s0.n.fxa = s0.n.fza = s0.n.fxb = s0.n.fzb = 0;
    ap_uint<Q_BITS> pend_q = 0;     // second write of a pair
    lanes_t pend_x = 0, pend_z = 0;
    bool clear = false;             // registered: this iteration is the first slot of a batch
    int i = 0;
    const long total = (long)num_instr * num_batches;
FRAME:
    for (long t = 0; t < total; t++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount max = 1000000
        // read: slot t
        Slot nr;
        nr.n = in.read();
        const ap_uint<Q_BITS> rq = nr.n.word.range(QA_LSB + Q_BITS - 1, QA_LSB);
        const bool rv = valid[rq];
        const lanes_t rx = X[rq], rz = Z[rq];
        nr.x = rv ? rx : lanes_t(0);
        nr.z = rv ? rz : lanes_t(0);

        // complete slot t - 2
        const ap_uint<64> w = s1.n.word;
        const unsigned op = w.range(OP_LSB + OP_BITS - 1, OP_LSB);
        const ap_uint<Q_BITS> q = w.range(QA_LSB + Q_BITS - 1, QA_LSB);
        const ap_uint<Q_BITS> qb = s0.n.word.range(QA_LSB + Q_BITS - 1, QA_LSB);
        bool wr = false;
        ap_uint<Q_BITS> wq = q;
        lanes_t wx = 0, wz = 0;
        bool meas = false;
        lanes_t eff = 0;
        if (w[PAIR_FIRST_BIT]) {
            // operands: a = s1, b = s0; masks of the pair in s1
            const lanes_t xa = s1.x, za = s1.z, xb = s0.x, zb = s0.z;
            lanes_t nxa = xa ^ s1.n.fxa, nza = za ^ s1.n.fza, nxb = xb ^ s1.n.fxb, nzb = zb ^ s1.n.fzb;
            if (op == stim_u55c::OPCODE_GATE_CX) {
                nxb = xb ^ xa;
                nza = za ^ zb;
            } else if (op == stim_u55c::OPCODE_GATE_CZ) {
                nzb = zb ^ xa;
                nza = za ^ xb;
            } else if (op == stim_u55c::OPCODE_GATE_SWAP) {
                nxa = xb;
                nxb = xa;
                nza = zb;
                nzb = za;
            }
            wr = true;
            wx = nxa;
            wz = nza;
            pend_q = qb;
            pend_x = nxb;
            pend_z = nzb;
        } else if (w[PAIR_SECOND_BIT]) {
            wr = true;
            wq = pend_q;
            wx = pend_x;
            wz = pend_z;
        } else if (op != stim_u55c::OPCODE_NOP) {
            const lanes_t xa = s1.x, za = s1.z;
            lanes_t nxa = xa ^ s1.n.fxa, nza = za ^ s1.n.fza;
            switch (op) {
                case stim_u55c::OPCODE_GATE_H:
                    nxa = za;
                    nza = xa;
                    break;
                case stim_u55c::OPCODE_GATE_S:
                    nza = za ^ xa;
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
                    break;  // single qubit noise: the masks above
            }
            if (op == stim_u55c::OPCODE_MEASURE_RESET_Z || op == stim_u55c::OPCODE_MEASURE_RESET_X ||
                op == stim_u55c::OPCODE_MEASURE_RESET_Y) {
                nxa = 0;
                nza = 0;
            }
            wr = true;
            wx = nxa;
            wz = nza;
        }
        if (meas) {
            Effect e;
            e.slot = w.range(SLOT_LSB + SLOT_BITS - 1, SLOT_LSB);
            e.value = eff;
            effects.write(e);
        }
        if (clear) {
            valid = 0;  // the slots completing now are the NOPs that end the previous batch
        } else if (wr) {
            X[wq] = wx;
            Z[wq] = wz;
            valid[wq] = 1;
        }
        s1 = s0;
        s0 = nr;
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
