// stim-u55c: FPGA-accelerated stabilizer circuit sampling for Alveo U55C
// Author: Nasir Ali, C-DAC Noida
//
// Top-level kernel: interprets a flat instruction stream (kernel/isa.py)
// against a SHOTS-wide, URAM-resident Pauli frame (frame_store.hpp),
// applying gates (gate_ops.hpp), drawing noise from a per-shot Philox4x32
// bank (prng.hpp), and folding measurements into detector/observable
// accumulators as they happen (detector_fold.hpp) -- no separate
// buffering pass. This is Stim section 3 (Pauli frame propagation) and
// section 5.6 (detector sampling) as one straight-line, pipelinable loop
// over instructions.
//
// Layered/hazard-free instruction scheduling for II=1 (Stim-paper-brief
// section 3.1) is a Phase 3 concern: this loop is functionally correct
// regardless of instruction order (HLS will simply insert stalls on a
// same-qubit hazard rather than corrupt results), which is everything
// Phase 2's gate (sw_emu bit-exact vs. the soft model) requires. See
// docs/README.md.
#include "config.hpp"
#include "depolarize2_table.hpp"
#include "detector_fold.hpp"
#include "frame_store.hpp"
#include "gate_ops.hpp"
#include "instruction.hpp"
#include "opcodes.hpp"
#include "prng.hpp"
#include "stim_frame_sampler.hpp"

namespace stim_u55c {

namespace {

// One shared draw for every noise opcode -- X/Y/Z only need `active_mask`
// (word0 vs. threshold), DEPOLARIZE1/2 additionally need `which[]` (word1
// mod a category count) to pick which Pauli(s) apply, but it's the same
// per-lane Philox4x32-10 bank either way. This used to be two separate
// functions (draw_flip_mask, draw_categorical); synthesis showed that
// meant two full 64-lane/10-round-unrolled Philox banks in hardware --
// 84% of the kernel's total LUT count between them, over budget on a
// single SLR -- purely because they were textually different functions
// HLS had no basis to share hardware between. One function called from
// all three noise cases lets HLS's ordinary mutually-exclusive-branch
// resource sharing do that consolidation instead.
inline void draw_noise(int instruction_index, uint32_t threshold, uint32_t key0, uint32_t key1, uint32_t modulus,
                        ap_uint<SHOTS> &active_mask, uint8_t which[SHOTS]) {
    active_mask = 0;
DRAW_NOISE_LANES:
    for (int lane = 0; lane < SHOTS; lane++) {
#pragma HLS unroll
        Philox4x32Result r = philox4x32_10(static_cast<uint32_t>(instruction_index), static_cast<uint32_t>(lane), 0,
                                            0, key0, key1);
        if (r.w0 < threshold) {
            active_mask[lane] = 1;
        }
        which[lane] = static_cast<uint8_t>(r.w1 % modulus);
    }
}

// Index -> (pauli_on_a, pauli_on_b) for DEPOLARIZE2's 15 nontrivial
// two-qubit Pauli combinations. Table is generated (depolarize2_table.hpp)
// from the same Python list softmodel/kernel_replay.py uses -- see that
// header's comment for why this isn't hand-transcribed.
inline void depolarize2_combo(uint8_t index, bool &a_x, bool &a_z, bool &b_x, bool &b_z) {
    a_x = kDepolarize2AHasX[index];
    a_z = kDepolarize2AHasZ[index];
    b_x = kDepolarize2BHasX[index];
    b_z = kDepolarize2BHasZ[index];
}

}  // namespace

}  // namespace stim_u55c

// stim_frame_sampler itself is deliberately NOT inside namespace
// stim_u55c -- see stim_frame_sampler.hpp for why. `using namespace`
// brings every other kernel symbol (FrameStore, gate_h, Instruction, the
// OPCODE_* constants, ...) into scope here without having to qualify
// each one individually in the function body below.
using namespace stim_u55c;

// No reference/noiseless measurement sample is passed in, and none is
// needed: a detector or observable's fold value is `actual XOR
// reference`, i.e. the frame's own deviation, and the reference term
// cancels out of that XOR algebraically regardless of what it is (see
// softmodel/kernel_replay.py's run_program docstring). This kernel never
// computes or exposes a raw per-shot measurement value, only the
// detector/observable accumulators below.
extern "C" void stim_frame_sampler(const Instruction *instructions, int num_instructions,
                                    const uint32_t *layer_offsets, int num_layers, uint32_t seed_lo, uint32_t seed_hi,
                                    ap_uint<SHOTS> detector_out[NUM_DETECTORS_MAX],
                                    ap_uint<SHOTS> observable_out[NUM_OBSERVABLES_MAX]) {
    // Logical AXI grouping only -- actual HBM bank assignment is a
    // connectivity.cfg concern (host/, Phase 4), generated rather than
    // handwritten per the project brief. gmem0/gmem1 split mirrors the
    // brief's own connectivity.cfg example (instr_stream and
    // detector_out on separate HBM pseudo-channels).
#pragma HLS INTERFACE m_axi port = instructions offset = slave bundle = gmem0 depth = 1048576
#pragma HLS INTERFACE m_axi port = layer_offsets offset = slave bundle = gmem0 depth = 1025
#pragma HLS INTERFACE m_axi port = detector_out offset = slave bundle = gmem1 depth = NUM_DETECTORS_MAX
#pragma HLS INTERFACE m_axi port = observable_out offset = slave bundle = gmem1 depth = NUM_OBSERVABLES_MAX
    // No explicit bundle names on the s_axilite ports below: each m_axi
    // port above also gets an auto-generated s_axilite offset register,
    // and Vitis kernel mode requires every s_axilite port (ours and
    // those auto-generated ones) to land in the same control bundle --
    // naming one explicitly here just fights that default.
#pragma HLS INTERFACE s_axilite port = num_instructions
#pragma HLS INTERFACE s_axilite port = num_layers
#pragma HLS INTERFACE s_axilite port = seed_lo
#pragma HLS INTERFACE s_axilite port = seed_hi
#pragma HLS INTERFACE s_axilite port = return

    FrameStore fs;
    fs.reset_all();
    DetectorFold fold;
    fold.reset_all();

    // Every instruction within one layer touches a disjoint set of
    // qubits (kernel/isa.py:_layer_and_reorder) -- that's the section
    // 3.1 guarantee, and it's a guarantee about fs.x/fs.z specifically
    // (each instruction's effect depends only on its own qubit(s)'
    // frame state).
    //
    // A `#pragma HLS pipeline II=1` + `dependence variable=fs.x/fs.z
    // inter false` on INSTRUCTION_LOOP was tried and measured, not
    // adopted: HLS could only reach II=34 anyway (not 1), bottlenecked
    // by two things layering-by-qubit doesn't address -- see below and
    // docs/utilization.md for the full account -- and getting even that
    // far pushed SLR-relative LUT usage to 131%, over the 70% gate, for
    // a design that was otherwise comfortably under it. That's a real
    // area/throughput tradeoff, not a bug to paper over, so the loop is
    // left unpipelined here; layering and the layer_offsets plumbing
    // stay (they're correct and free), ready for whoever picks the
    // pipelining problem back up with more runway than this pass had.
    //
    // The two things that would need solving first: (1) the detector/
    // observable accumulators in fold -- two qubit-disjoint measurements
    // in the same layer can still both contribute to the *same*
    // detector (very common -- several ancillas' measurements often
    // feed one), a real read-modify-write hazard layering-by-qubit
    // doesn't eliminate, and HLS's own dependence checker correctly
    // refused to pipeline through it; (2) `instructions` is fetched from
    // external memory (m_axi) per iteration, and Instruction's 32-byte
    // detector_mask means each fetch is several AXI transactions, which
    // HLS reported as its own II-limiting factor independent of (1).
LAYER_LOOP:
    for (int layer = 0; layer < num_layers; layer++) {
        int layer_start = layer_offsets[layer];
        int layer_end = layer_offsets[layer + 1];
    INSTRUCTION_LOOP:
        for (int i = layer_start; i < layer_end; i++) {
        const Instruction &instr = instructions[i];
        const int a = instr.qubit_a;
        const int b = instr.qubit_b;

        switch (instr.opcode) {
            case OPCODE_NOP:
            case OPCODE_END_OF_PROGRAM:
                break;
            case OPCODE_GATE_H:
                gate_h(fs, a);
                break;
            case OPCODE_GATE_S:
                gate_s(fs, a);
                break;
            case OPCODE_GATE_CX:
                gate_cx(fs, a, b);
                break;
            case OPCODE_GATE_CZ:
                gate_cz(fs, a, b);
                break;
            case OPCODE_GATE_SWAP:
                gate_swap(fs, a, b);
                break;
            case OPCODE_RESET_Z:
            case OPCODE_RESET_X:
            case OPCODE_RESET_Y:
                reset_qubit(fs, a);
                break;
            case OPCODE_MEASURE_Z:
            case OPCODE_MEASURE_X:
            case OPCODE_MEASURE_Y:
            case OPCODE_MEASURE_RESET_Z:
            case OPCODE_MEASURE_RESET_X:
            case OPCODE_MEASURE_RESET_Y: {
                ap_uint<SHOTS> effect;
                if (instr.opcode == OPCODE_MEASURE_Z || instr.opcode == OPCODE_MEASURE_RESET_Z) {
                    effect = fs.x[a];
                } else if (instr.opcode == OPCODE_MEASURE_X || instr.opcode == OPCODE_MEASURE_RESET_X) {
                    effect = fs.z[a];
                } else {
                    effect = fs.x[a] ^ fs.z[a];
                }
                if (instr.opcode == OPCODE_MEASURE_RESET_Z || instr.opcode == OPCODE_MEASURE_RESET_X ||
                    instr.opcode == OPCODE_MEASURE_RESET_Y) {
                    reset_qubit(fs, a);
                }
                fold.fold(instr, effect);
                break;
            }
            case OPCODE_NOISE_X:
            case OPCODE_NOISE_Y:
            case OPCODE_NOISE_Z: {
                ap_uint<SHOTS> flip;
                uint8_t unused_which[SHOTS];
                draw_noise(i, instr.prob_threshold, seed_lo, seed_hi, 3, flip, unused_which);
                if (instr.opcode != OPCODE_NOISE_Z) {
                    fs.x[a] ^= flip;
                }
                if (instr.opcode != OPCODE_NOISE_X) {
                    fs.z[a] ^= flip;
                }
                break;
            }
            case OPCODE_NOISE_DEPOLARIZE1: {
                ap_uint<SHOTS> active;
                uint8_t which[SHOTS];
                draw_noise(i, instr.prob_threshold, seed_lo, seed_hi, 3, active, which);
                ap_uint<SHOTS> flip_x = 0, flip_z = 0;
            DEPOL1_LANES:
                for (int lane = 0; lane < SHOTS; lane++) {
#pragma HLS unroll
                    if (active[lane]) {
                        if (which[lane] == 0) flip_x[lane] = 1;             // X
                        if (which[lane] == 1) { flip_x[lane] = 1; flip_z[lane] = 1; }  // Y
                        if (which[lane] == 2) flip_z[lane] = 1;             // Z
                    }
                }
                fs.x[a] ^= flip_x;
                fs.z[a] ^= flip_z;
                break;
            }
            case OPCODE_NOISE_DEPOLARIZE2: {
                ap_uint<SHOTS> active;
                uint8_t which[SHOTS];
                draw_noise(i, instr.prob_threshold, seed_lo, seed_hi, 15, active, which);
                ap_uint<SHOTS> flip_ax = 0, flip_az = 0, flip_bx = 0, flip_bz = 0;
            DEPOL2_LANES:
                for (int lane = 0; lane < SHOTS; lane++) {
#pragma HLS unroll
                    if (active[lane]) {
                        bool a_x, a_z, b_x, b_z;
                        depolarize2_combo(which[lane], a_x, a_z, b_x, b_z);
                        flip_ax[lane] = a_x;
                        flip_az[lane] = a_z;
                        flip_bx[lane] = b_x;
                        flip_bz[lane] = b_z;
                    }
                }
                fs.x[a] ^= flip_ax;
                fs.z[a] ^= flip_az;
                fs.x[b] ^= flip_bx;
                fs.z[b] ^= flip_bz;
                break;
            }
            default:
                break;
        }
        }  // INSTRUCTION_LOOP
    }  // LAYER_LOOP

WRITE_DETECTORS:
    for (int d = 0; d < NUM_DETECTORS_MAX; d++) {
#pragma HLS pipeline II = 1
        detector_out[d] = fold.detectors[d];
    }
WRITE_OBSERVABLES:
    for (int o = 0; o < NUM_OBSERVABLES_MAX; o++) {
#pragma HLS pipeline II = 1
        observable_out[o] = fold.observables[o];
    }
}
