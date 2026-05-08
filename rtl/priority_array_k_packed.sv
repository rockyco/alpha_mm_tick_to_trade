// priority_array_k_packed.sv -- multi-symbol packed variant (canonical, v1.28.23).
//
// Phase-10 keystone primitive: N_SYMBOLS independent priority arrays sharing
// one BRAM addressed by {symbol_id, key}, plus per-symbol K-best register
// cache, plus a shared scan FSM that refills the cache from mem_q after
// eviction. Mirror of legacy priority_array_k.sv extended over the
// {sym_id, key} address space.
//
// Architecture (Rule 6.5 canonical):
//
//   +------------------------------+    +-----------------------------+
//   | mem_q[N_SYMBOLS * KEY_RANGE] |    | kbest_data_mem_q[N_SYMBOLS] |
//   | data = {valid, qty}          |    | (LUTRAM, K=2 cache per sym) |
//   | (BRAM, dual-port: cmd + scan)|    +-----------------------------+
//   +------------------------------+              ^      ^
//          ^                                      |      |
//          | cmd writes (insert/remove)           |      | scan-driven
//          | scan reads (refill)                  |      | refill of slot 0
//          | debug reads (any sym, any key)       |      |
//          v                                      |      |
//   +------------------------------+              |      |
//   | Shared scan FSM              | -------------+------+
//   | (round-robin over symbols    |
//   |  whose kbest depleted)       |
//   +------------------------------+
//
// Two BRAM read ports on mem_q (Vivado clones the BRAM once):
//   port-A: cmd path -> was_valid for size counter update
//   port-B: scan path -> {scan_sym_q, scan_key_q} during refill
//   port-C (debug): {read_sym_i, read_addr_i} -> read_val_o / read_valid_o
//
// LATENCY_CYCLES = 2 (cmd_q -> kbest commit; output combinational).
// Scan refill is async (1..KEY_RANGE cycles per cache deplete).
//
// Storage-budget sanity (Rule 6.5(5)):
//   declared_bits = N_SYMBOLS * KEY_RANGE * (VAL_BITS + 1)
//   expected_RAMB36 ~ ceil(declared_bits / 36864)
//   With debug read port + scan read port consuming all VAL_BITS, Vivado
//   retains the BRAM (no DCE). Storage-budget verified by V4.
//
// Universal across HFT M0 multi-symbol book, QoS scheduler, packet
// classifier, multi-stream cache.

module priority_array_k_packed #(
    parameter int N_SYMBOLS  = 8,
    parameter int KEY_BITS   = 8,
    parameter int VAL_BITS   = 32,
    parameter int DESCENDING = 1,
    parameter int K_LEVELS   = 2,
    // BANK_SYMS controls the storage partitioning. Tunable knob for timing
    // closure (v1.28.32 lesson): trade bank-mux width vs BRAM cascade depth.
    //   Smaller BANK_SYMS  -> more banks, wider mux (slow at huge N).
    //   Larger BANK_SYMS   -> fewer banks, deeper banks (BRAM cascade slow at huge N).
    // Default 16 is best at KEY_BITS=8 for N=128-256 (no BRAM cascade,
    // moderate mux). For N>=512 try BANK_SYMS=32 or 64 to narrow the mux.
    // BANK_SYMS=0 means auto-pick min(N_SYMBOLS, 16). User can override.
    parameter int BANK_SYMS_OVERRIDE = 0,
    // DEBUG_READ_PORT (v1.28.33): include the debug-read structure?
    //   0 = NO (production default). mem_q is 1W2R - one BRAM tile per
    //       bank, no clone. Saves ~50% BRAM vs 1W3R.
    //   1 = YES. Adds a third read port for direct (sym, key) value
    //       observation. Used by test_storage_liveness_via_debug_read
    //       and other RTL-level verification tests. Forces Vivado to
    //       clone the BRAM (~2x tiles per bank).
    // Indirect storage liveness (Rule 6.5(5)) is still verified at
    // DEBUG_READ_PORT=0 via the deep-eviction test: scan refill correctness
    // implies mem_q value bits are not DCE'd.
    parameter int DEBUG_READ_PORT = 0
)(
    input  logic                          clk,
    input  logic                          rst_n,
    output logic                          ready_o,
    input  logic [$clog2(N_SYMBOLS)-1:0]  sym_i,
    input  logic [1:0]                    op_i,    // 0=nop, 1=insert, 2=remove
    input  logic [KEY_BITS-1:0]           key_i,
    input  logic [VAL_BITS-1:0]           val_i,
    output logic [$clog2(N_SYMBOLS)-1:0]  top_sym_o,
    output logic [KEY_BITS-1:0]           top_key_o,
    output logic [VAL_BITS-1:0]           top_val_o,
    output logic                          top_valid_o,
    output logic [KEY_BITS:0]             size_o,
    // Debug read port (Rule 6.5(5) storage liveness verifier)
    input  logic [$clog2(N_SYMBOLS)-1:0]  read_sym_i,
    input  logic [KEY_BITS-1:0]           read_key_i,
    output logic [VAL_BITS-1:0]           read_val_o,
    output logic                          read_valid_o
);

    localparam int SYM_BITS  = ($clog2(N_SYMBOLS) > 0) ? $clog2(N_SYMBOLS) : 1;
    localparam int KEY_RANGE = 1 << KEY_BITS;
    localparam int CELL_W    = VAL_BITS + 1;
    localparam int MEM_DEPTH = N_SYMBOLS * KEY_RANGE;
    localparam int MEM_AW    = SYM_BITS + KEY_BITS;
    localparam logic [KEY_BITS-1:0] KEY_HIGH = {KEY_BITS{1'b1}};
    localparam logic [KEY_BITS-1:0] KEY_ZERO = '0;

    assign ready_o = 1'b1;

    // ==========================================================
    // Banked BRAM mem_q (v1.28.26: width-banked for huge-N timing)
    //
    // Prior versions used a single mem_q[N_SYMBOLS * KEY_RANGE] block. At
    // N >= 128 this becomes 32k+ deep, forcing Vivado to depth-cascade 7+
    // RAMB36E2 in series. Cascade delay (~0.6 ns/hop) blew through the
    // 333 MHz target (-0.88 ns WNS at N=128).
    //
    // Fix: split mem_q into MEM_BANKS parallel banks, each holding
    // (N_SYMBOLS / MEM_BANKS) symbols' worth of data. With banks scaled
    // to keep BANK_DEPTH ~= 8192 entries (one BRAM tile worth of address
    // space), no cascade is needed.
    //
    // Bank select = high bits of sym_id; bank-local offset = (low sym
    // bits, key). All banks read the SAME bank-local offset every cycle;
    // a 1-cycle-delayed bank-id selects the correct bank's data at the
    // output. Writes target only the selected bank.
    //
    // Sizing: BANK_SYMS = 32 keeps bank depth = 32 * KEY_RANGE = 8192 at
    // KEY_BITS=8, fitting cleanly in BRAM aspect ratios with depth-1
    // cascade. For KEY_BITS != 8, BANK_SYMS scales accordingly.
    //
    // Note: banking only helps at N >= 64. For smaller N, MEM_BANKS = 1
    // and the design degenerates to the single-array form.
    // ==========================================================
    // BANK_SYMS chosen to keep BANK_DEPTH within Vivado's preferred BRAM
    // aspect ratio (no depth cascade) when default. At KEY_BITS=8:
    // BANK_SYMS=16 gives BANK_DEPTH=4096, single RAMB36 layer in 9-bit-wide
    // mode (3 BRAMs in width, no cascade). BANK_SYMS=32 gives BANK_DEPTH=8192,
    // 3-deep BRAM cascade chain costing ~0.86 ns of delay
    // (verified at v1.28.27 N=256, WNS -0.215 ns dominated by cascade).
    //
    // For N>=512 the bank-MUX width itself becomes the bottleneck (32:1 at
    // BANK_SYMS=16). Increasing BANK_SYMS to 32 or 64 narrows the mux but
    // brings back BRAM cascade. Worked example v1.28.32 sweep below.
    //
    // BANK_SYMS_OVERRIDE = 0 -> use default; non-zero -> use override.
    //
    // Empirical sweet-spots from v1.28.32 V4 sweep (Alveo U50, 333 MHz):
    //   N <= 256  -> BS=16 (best WNS).
    //   N = 512   -> BS=8  (BS=16 -0.539 ns; BS=8 -0.427 ns; +112 ps).
    //   N >= 1024 -> BS=16 (BS=8 -1.519 ns; BS=16 -1.324 ns).
    //
    // The knee at N=512 is non-trivial: BANK_DEPTH=2048 fits BRAM 18x2K
    // mode (1 RAMB18 width × 2K deep) without cascade; the wider 64:1
    // mux is cheap because Vivado uses dense F7/F8 cells. At N=1024 the
    // 128:1 mux at BS=8 dominates the trade.
    //
    // Default formula chosen to track empirical sweet-spot:
    localparam int BANK_SYMS_DEFAULT =
        (N_SYMBOLS <=  16) ? N_SYMBOLS  :
        (N_SYMBOLS <= 256) ? 16         :
        (N_SYMBOLS <= 512) ? 8          :
        16;
    localparam int BANK_SYMS = (BANK_SYMS_OVERRIDE == 0)
                              ? BANK_SYMS_DEFAULT
                              : BANK_SYMS_OVERRIDE;
    localparam int LOG_BANKS_RAW = (N_SYMBOLS <= 32) ? 0
                                 : ($clog2(N_SYMBOLS) - $clog2(BANK_SYMS));
    localparam int LOG_BANKS    = (LOG_BANKS_RAW < 0) ? 0 : LOG_BANKS_RAW;
    localparam int MEM_BANKS    = 1 << LOG_BANKS;
    localparam int BANK_SYM_BITS = SYM_BITS - LOG_BANKS;
    localparam int BANK_OFFSET_BITS = (BANK_SYM_BITS <= 0)
                                   ? KEY_BITS
                                   : (BANK_SYM_BITS + KEY_BITS);
    localparam int BANK_DEPTH = 1 << BANK_OFFSET_BITS;
    // Width-1 minimum for bank-id register declarations
    localparam int LOG_BANKS_W = (LOG_BANKS == 0) ? 1 : LOG_BANKS;

    // ==========================================================
    // S0: latch cmd
    // ==========================================================
    logic [SYM_BITS-1:0]   s0_sym_q;
    logic [1:0]            s0_op_q;
    logic [KEY_BITS-1:0]   s0_key_q;
    logic [VAL_BITS-1:0]   s0_val_q;
    logic                  s0_is_insert_q;
    logic                  s0_is_remove_q;

    logic is_insert_w;
    logic is_remove_w;
    assign is_insert_w = (op_i == 2'd1) && (val_i != '0);
    assign is_remove_w = (op_i == 2'd2)
                       || ((op_i == 2'd1) && (val_i == '0));

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s0_sym_q       <= '0;
            s0_op_q        <= 2'd0;
            s0_key_q       <= '0;
            s0_val_q       <= '0;
            s0_is_insert_q <= 1'b0;
            s0_is_remove_q <= 1'b0;
        end else begin
            s0_sym_q       <= sym_i;
            s0_op_q        <= op_i;
            s0_key_q       <= key_i;
            s0_val_q       <= val_i;
            s0_is_insert_q <= is_insert_w;
            s0_is_remove_q <= is_remove_w;
        end
    end

    // ==========================================================
    // Banked BRAM ports
    //   - bank_id_w  = high bits of sym_id (selects bank at output mux)
    //   - offset_w   = (low sym bits, key) drives all banks in parallel
    //   - port-A: cmd write to addressed bank + read for was_valid
    //   - port-B: scan read of addressed bank
    //   - port-C: debug read of addressed bank
    // ==========================================================

    // Forward refs - declared early because they feed bank-decode below
    logic [SYM_BITS-1:0]  scan_sym_q;
    logic [KEY_BITS-1:0]  scan_key_q;
    logic [SYM_BITS-1:0]  scan_sym_d1_q;
    logic [KEY_BITS-1:0]  scan_key_d1_q;
    logic                 scan_active_d1_q;

    // Bank decode (combinational, per cycle)
    logic [LOG_BANKS_W-1:0]      cmd_bank_w, scan_bank_w, read_bank_w;
    logic [BANK_OFFSET_BITS-1:0] cmd_offset_w, scan_offset_w, read_offset_w;

    generate
    if (LOG_BANKS == 0) begin : g_no_bank_decode
        assign cmd_bank_w    = '0;
        assign scan_bank_w   = '0;
        assign read_bank_w   = '0;
        assign cmd_offset_w  = (BANK_SYM_BITS > 0)
                             ? {s0_sym_q[BANK_SYM_BITS-1:0], s0_key_q}
                             : s0_key_q;
        assign scan_offset_w = (BANK_SYM_BITS > 0)
                             ? {scan_sym_q[BANK_SYM_BITS-1:0], scan_key_q}
                             : scan_key_q;
        assign read_offset_w = (BANK_SYM_BITS > 0)
                             ? {read_sym_i[BANK_SYM_BITS-1:0], read_key_i}
                             : read_key_i;
    end else begin : g_bank_decode
        assign cmd_bank_w    = s0_sym_q[SYM_BITS-1 -: LOG_BANKS];
        assign scan_bank_w   = scan_sym_q[SYM_BITS-1 -: LOG_BANKS];
        assign read_bank_w   = read_sym_i[SYM_BITS-1 -: LOG_BANKS];
        assign cmd_offset_w  = {s0_sym_q[BANK_SYM_BITS-1:0], s0_key_q};
        assign scan_offset_w = {scan_sym_q[BANK_SYM_BITS-1:0], scan_key_q};
        assign read_offset_w = {read_sym_i[BANK_SYM_BITS-1:0], read_key_i};
    end
    endgenerate

    // Bank-id 1-cycle delay register (aligns with registered read data)
    logic [LOG_BANKS_W-1:0] cmd_bank_d1_q, scan_bank_d2_q, read_bank_d1_q;

    always_ff @(posedge clk) begin
        cmd_bank_d1_q  <= cmd_bank_w;
        read_bank_d1_q <= read_bank_w;
    end
    // Scan path: bank id needs to align with scan_data_q which is the read
    // result of scan_addr probed when scan_state == S_SCAN. scan_sym_q is
    // already registered, so scan_bank_w (combinational of scan_sym_q) at
    // cycle T drives the read at T; the data lands at cycle T+1; we need
    // the bank id at T+1 to mux. So one register stage.
    always_ff @(posedge clk) scan_bank_d2_q <= scan_bank_w;

    // Per-bank arrays (Level-3 storage architecture, v1.28.34):
    //
    // - mem_q (BRAM, 1W1R): stores VAL_BITS-wide value. Cmd writes value;
    //   scan reads value for kbest refill. NO cmd-was BRAM read (moved
    //   to LUTRAM valid_mem_q). Vivado fits each bank in 1 BRAM tile.
    //
    // - valid_mem_q (LUTRAM, 1W2R): stores 1-bit valid flag. Cmd writes
    //   valid bit synchronously with mem_q write. Cmd-was reads valid bit
    //   COMBINATIONALLY (no BRAM-read latency on the cmd path). Scan also
    //   reads valid_mem_q (registered to align with scan_data_q).
    //
    // The win vs Level-1 (debug-port off): cmd-was is no longer in the
    // BRAM-out -> bank-mux -> consumer combinational path. valid_mem_q
    // is 1-bit-wide so its bank-mux is much smaller than mem_q's. Total
    // critical-path delay drops by the BRAM CLK->DOUT delta (~0.3-0.5 ns).
    //
    // Coherence: cmd writes to mem_q and valid_mem_q at the same posedge.
    // Both are read at the same address. The two arrays are kept in sync
    // by construction.
    logic [VAL_BITS-1:0] bank_scan_data_q  [0:MEM_BANKS-1];
    logic                bank_scan_valid_q [0:MEM_BANKS-1];
    // v1.28.34 Option 2: register the cmd-was LUTRAM read so the downstream
    // chain has its own cycle (mimicking BRAM's natural CLKARDCLK→DOUTADOUT
    // register that L1 was implicitly relying on). Without this register,
    // the cmd-was → fifo_count_q chain is 12 LUT levels in one cycle.
    // With it, the chain splits into LUTRAM access + bank-mux (cycle T+1)
    // and was_valid → fifo logic (cycle T+2), each fitting comfortably.
    logic                bank_valid_cmd_q  [0:MEM_BANKS-1];  // registered
    logic [VAL_BITS-1:0] bank_read_data_q  [0:MEM_BANKS-1];
    logic                bank_read_valid_q [0:MEM_BANKS-1];

    genvar bi;
    generate
    for (bi = 0; bi < MEM_BANKS; bi = bi + 1) begin : g_mem_bank
        // mem_q now holds VALUE only (no valid bit), reducing width from
        // CELL_W to VAL_BITS. Saves 1 bit per entry of BRAM storage.
        (* ram_style = "block" *)
        logic [VAL_BITS-1:0] mem_q [0:BANK_DEPTH-1];

        // valid_mem_q holds 1-bit valid flag in LUTRAM. Combinational read.
        (* ram_style = "distributed" *)
        logic valid_mem_q [0:BANK_DEPTH-1];

        integer init_k;
        initial begin
            for (init_k = 0; init_k < BANK_DEPTH; init_k = init_k + 1) begin
                mem_q[init_k]       = '0;
                valid_mem_q[init_k] = 1'b0;
            end
        end

        // Bank-targeted write enable: only fires when cmd targets this bank
        logic this_bank_we_w;
        assign this_bank_we_w = (cmd_bank_w == bi[LOG_BANKS_W-1:0]);

        // mem_q write port (BRAM): value only. Reset-less always_ff per
        // F5.28 (LUTRAM/BRAM reset trap). Initial block sets bitstream INIT.
        always_ff @(posedge clk) begin
            if (s0_is_insert_q && this_bank_we_w)
                mem_q[cmd_offset_w] <= s0_val_q;
            else if (s0_is_remove_q && this_bank_we_w)
                mem_q[cmd_offset_w] <= '0;
        end

        // valid_mem_q write port (LUTRAM): 1 = insert, 0 = remove.
        // Same address + same posedge as mem_q write -> coherent.
        always_ff @(posedge clk) begin
            if (s0_is_insert_q && this_bank_we_w)
                valid_mem_q[cmd_offset_w] <= 1'b1;
            else if (s0_is_remove_q && this_bank_we_w)
                valid_mem_q[cmd_offset_w] <= 1'b0;
        end

        // Cmd-was read: valid_mem_q LUTRAM, REGISTERED (Option 2).
        // The register stage replaces BRAM's implicit CLKARDCLK→DOUTADOUT
        // register that L1 used. Net cmd-was timing matches L1.
        always_ff @(posedge clk) bank_valid_cmd_q[bi] <= valid_mem_q[cmd_offset_w];

        // Scan reads: BOTH mem_q (value, BRAM, registered) AND
        // valid_mem_q (valid, LUTRAM, registered to align).
        always_ff @(posedge clk) bank_scan_data_q [bi] <= mem_q       [scan_offset_w];
        always_ff @(posedge clk) bank_scan_valid_q[bi] <= valid_mem_q [scan_offset_w];

        // Optional debug read (forces both BRAM and LUTRAM clones)
        if (DEBUG_READ_PORT) begin : g_debug_read
            always_ff @(posedge clk) begin
                bank_read_data_q [bi] <= mem_q      [read_offset_w];
                bank_read_valid_q[bi] <= valid_mem_q[read_offset_w];
            end
        end else begin : g_no_debug_read
            assign bank_read_data_q [bi] = '0;
            assign bank_read_valid_q[bi] = 1'b0;
        end
    end
    endgenerate

    // Top-level read data: muxed by 1-cycle-delayed bank id
    // scan_data_q now holds VALUE only (scan valid is separate signal
    // from valid_mem_q LUTRAM). Width drops from CELL_W to VAL_BITS.
    logic [VAL_BITS-1:0] scan_data_q;
    logic                scan_valid_q;
    logic [VAL_BITS-1:0] read_data_q;
    logic                read_valid_q;

    assign scan_data_q  = bank_scan_data_q [scan_bank_d2_q];
    assign scan_valid_q = bank_scan_valid_q[scan_bank_d2_q];
    assign read_data_q  = bank_read_data_q [read_bank_d1_q];
    assign read_valid_q = bank_read_valid_q[read_bank_d1_q];

    // was_valid_w: combinational mux of REGISTERED bank_valid_cmd_q
    // outputs. Bank-id uses cmd_bank_d1_q (1-cycle delayed) to align with
    // the registered LUTRAM read. Same alignment trick as bank_was_data_q
    // had in L1 — the register lives in valid_mem_q's read side, not in
    // the BRAM (since LUTRAM doesn't have one natively).
    logic was_valid_w;
    assign was_valid_w = bank_valid_cmd_q[cmd_bank_d1_q];

    // Debug outputs: tied to 0 when DEBUG_READ_PORT=0.
    assign read_val_o   = (DEBUG_READ_PORT) ? read_data_q  : '0;
    assign read_valid_o = (DEBUG_READ_PORT) ? read_valid_q : 1'b0;

    // ==========================================================
    // K-best per-field cache (v1.28.25 split for LUTRAM inference)
    //
    // Prior version used a single packed kbest_data_mem_q [N_SYMBOLS]
    // that was 66 bits wide x N deep. Vivado bailed on LUTRAM inference
    // at N >= 128 (combinational mux tree + flop array, ~22 kLUT at N=128).
    //
    // Fix: split into 6 narrow per-field arrays (1 / KEY_BITS / VAL_BITS
    // wide). Each is LUTRAM-friendly at any N up to ~1024. Total bit
    // budget identical (= K_LEVELS * (KEY_BITS + VAL_BITS + 1) per sym).
    //
    // For N_SYMBOLS > 1024 we switch to BRAM (sync read) to avoid LUTRAM
    // depth blow-up; this requires a deeper pipeline (1-cycle read
    // latency on cmd path) and is currently TODO. The LUTRAM threshold
    // is parameterized so the recipe can override.
    //
    // Note: size_mem_q stays 9-bit-wide x N-deep LUTRAM (small).
    // ==========================================================
    localparam int ENTRY_W = K_LEVELS * (KEY_BITS + VAL_BITS + 1);
    localparam int KBEST_USE_BRAM = (N_SYMBOLS > 1024) ? 1 : 0;

    // synthesis attribute selector (BRAM path TODO; emits LUTRAM today)
    (* ram_style = "distributed" *)
    logic [KEY_BITS-1:0] kbest_k0_mem_q [0:N_SYMBOLS-1];
    (* ram_style = "distributed" *)
    logic [VAL_BITS-1:0] kbest_v0_mem_q [0:N_SYMBOLS-1];
    (* ram_style = "distributed" *)
    logic                kbest_d0_mem_q [0:N_SYMBOLS-1];
    (* ram_style = "distributed" *)
    logic [KEY_BITS-1:0] kbest_k1_mem_q [0:N_SYMBOLS-1];
    (* ram_style = "distributed" *)
    logic [VAL_BITS-1:0] kbest_v1_mem_q [0:N_SYMBOLS-1];
    (* ram_style = "distributed" *)
    logic                kbest_d1_mem_q [0:N_SYMBOLS-1];

    (* ram_style = "distributed" *)
    logic [KEY_BITS:0]   size_mem_q     [0:N_SYMBOLS-1];

    integer kinit_i;
    initial begin
        for (kinit_i = 0; kinit_i < N_SYMBOLS; kinit_i = kinit_i + 1) begin
            kbest_k0_mem_q[kinit_i] = '0;
            kbest_v0_mem_q[kinit_i] = '0;
            kbest_d0_mem_q[kinit_i] = 1'b0;
            kbest_k1_mem_q[kinit_i] = '0;
            kbest_v1_mem_q[kinit_i] = '0;
            kbest_d1_mem_q[kinit_i] = 1'b0;
            size_mem_q   [kinit_i] = '0;
        end
    end

    // Combinational addressed read of cmd-symbol's kbest fields
    logic [KEY_BITS-1:0] cur_k0_w, cur_k1_w;
    logic [VAL_BITS-1:0] cur_v0_w, cur_v1_w;
    logic                cur_d0_w, cur_d1_w;
    logic [KEY_BITS:0]   cur_size_w;

    assign cur_k0_w   = kbest_k0_mem_q[s0_sym_q];
    assign cur_v0_w   = kbest_v0_mem_q[s0_sym_q];
    assign cur_d0_w   = kbest_d0_mem_q[s0_sym_q];
    assign cur_k1_w   = (K_LEVELS > 1) ? kbest_k1_mem_q[s0_sym_q] : '0;
    assign cur_v1_w   = (K_LEVELS > 1) ? kbest_v1_mem_q[s0_sym_q] : '0;
    assign cur_d1_w   = (K_LEVELS > 1) ? kbest_d1_mem_q[s0_sym_q] : 1'b0;
    assign cur_size_w = size_mem_q    [s0_sym_q];

    logic match_0_w, match_1_w;
    logic better_0_w, better_1_w;
    assign match_0_w = cur_d0_w && (cur_k0_w == s0_key_q);
    assign match_1_w = (K_LEVELS > 1) && cur_d1_w && (cur_k1_w == s0_key_q);
    generate
    if (DESCENDING != 0) begin : g_desc_cmp
        assign better_0_w = (!cur_d0_w) || (s0_key_q > cur_k0_w);
        assign better_1_w = (K_LEVELS > 1) && ((!cur_d1_w) || (s0_key_q > cur_k1_w));
    end else begin : g_asc_cmp
        assign better_0_w = (!cur_d0_w) || (s0_key_q < cur_k0_w);
        assign better_1_w = (K_LEVELS > 1) && ((!cur_d1_w) || (s0_key_q < cur_k1_w));
    end
    endgenerate

    // Cmd-driven next state
    logic [KEY_BITS-1:0] cmd_new_k0_w, cmd_new_k1_w;
    logic [VAL_BITS-1:0] cmd_new_v0_w, cmd_new_v1_w;
    logic                cmd_new_d0_w, cmd_new_d1_w;

    always_comb begin
        cmd_new_k0_w = cur_k0_w;
        cmd_new_v0_w = cur_v0_w;
        cmd_new_d0_w = cur_d0_w;
        cmd_new_k1_w = cur_k1_w;
        cmd_new_v1_w = cur_v1_w;
        cmd_new_d1_w = cur_d1_w;

        if (s0_is_insert_q) begin
            if (match_0_w) begin
                cmd_new_v0_w = s0_val_q;
            end else if (better_0_w) begin
                if (K_LEVELS > 1) begin
                    cmd_new_k1_w = cur_k0_w;
                    cmd_new_v1_w = cur_v0_w;
                    cmd_new_d1_w = cur_d0_w;
                end
                cmd_new_k0_w = s0_key_q;
                cmd_new_v0_w = s0_val_q;
                cmd_new_d0_w = 1'b1;
            end else if ((K_LEVELS > 1) && match_1_w) begin
                cmd_new_v1_w = s0_val_q;
            end else if ((K_LEVELS > 1) && better_1_w) begin
                cmd_new_k1_w = s0_key_q;
                cmd_new_v1_w = s0_val_q;
                cmd_new_d1_w = 1'b1;
            end
        end else if (s0_is_remove_q) begin
            if (match_0_w) begin
                if (K_LEVELS > 1) begin
                    cmd_new_k0_w = cur_k1_w;
                    cmd_new_v0_w = cur_v1_w;
                    cmd_new_d0_w = cur_d1_w;
                    cmd_new_k1_w = '0;
                    cmd_new_v1_w = '0;
                    cmd_new_d1_w = 1'b0;
                end else begin
                    cmd_new_k0_w = '0;
                    cmd_new_v0_w = '0;
                    cmd_new_d0_w = 1'b0;
                end
            end else if ((K_LEVELS > 1) && match_1_w) begin
                cmd_new_k1_w = '0;
                cmd_new_v1_w = '0;
                cmd_new_d1_w = 1'b0;
            end
        end
    end

    logic cmd_we_w;
    assign cmd_we_w = s0_is_insert_q | s0_is_remove_q;

    logic size_inc_w, size_dec_w;
    assign size_inc_w = s0_is_insert_q & (!was_valid_w);
    assign size_dec_w = s0_is_remove_q &   was_valid_w;

    // ==========================================================
    // Refill arbitration: pending FIFO + dedup bitmap (v1.28.24)
    //
    // Replaces the prior O(N) priority encoder + wide-OR pattern. At huge
    // N (>= 256) the priority encoder over an N-wide bitmap becomes the
    // Fmax bottleneck. The FIFO arbiter is O(1) regardless of N.
    //
    // enqueued_q[N]   : 1-bit dedup flag per sym. Indexed by s0_sym_q on
    //                   enqueue and by scan_sym_q at scan_finish. Read is
    //                   single-bit indexed (LUTRAM-friendly), never reduced
    //                   combinationally across all bits.
    // pending_fifo    : circular FIFO of pending sym IDs, depth = N_SYMBOLS
    //                   (cannot overflow because dedup ensures each sym
    //                   appears at most once).
    //
    // Set / Clear of enqueued_q and FIFO state machine:
    //   enq_w := refill_set_w && !enqueued_q[s0_sym_q]
    //   deq_w := (scan_state_q == S_IDLE) && !fifo_empty_w
    //   on enq_w:    enqueued_q[s0_sym_q] := 1; push s0_sym_q to FIFO
    //   on scan_finish_w (transition S_SCAN -> S_IDLE; commit OR exhaust):
    //                enqueued_q[scan_sym_q] := 0
    //
    // Race handling: if cmd targets scan_sym_q during a scan-commit cycle
    // (write-write conflict on kbest_data_mem_q[scan_sym]), the FSM STALLS
    // in S_SCAN (does not transition to IDLE). enqueued_q stays set and
    // the scan re-attempts the commit next cycle. This guarantees
    // convergence per Rule 6.5(2).
    // ==========================================================
    logic [N_SYMBOLS-1:0] enqueued_q;
    logic                 refill_set_w;
    logic [KEY_BITS:0]    next_size_w;
    assign next_size_w = size_inc_w ? (cur_size_w + 1)
                       : size_dec_w ? (cur_size_w - 1)
                       : cur_size_w;
    assign refill_set_w = cmd_we_w
                        && (!cmd_new_d0_w)
                        && (next_size_w != '0);

    // Pending FIFO: depth = N_SYMBOLS guarantees no overflow under dedup.
    localparam int FIFO_DEPTH = N_SYMBOLS;
    localparam int FIFO_AW    = ($clog2(FIFO_DEPTH) > 0) ? $clog2(FIFO_DEPTH) : 1;
    localparam int FIFO_CW    = FIFO_AW + 1;

    (* ram_style = "distributed" *)
    logic [SYM_BITS-1:0] fifo_mem_q [0:FIFO_DEPTH-1];
    logic [FIFO_AW-1:0]  fifo_wptr_q;
    logic [FIFO_AW-1:0]  fifo_rptr_q;
    logic [FIFO_CW-1:0]  fifo_count_q;

    integer fi;
    initial begin
        for (fi = 0; fi < FIFO_DEPTH; fi = fi + 1)
            fifo_mem_q[fi] = '0;
    end

    logic fifo_empty_w;
    assign fifo_empty_w = (fifo_count_q == '0);

    logic [SYM_BITS-1:0] fifo_dout_w;
    assign fifo_dout_w = fifo_mem_q[fifo_rptr_q];

    typedef enum logic [0:0] {S_IDLE, S_SCAN} scan_state_e;
    scan_state_e scan_state_q;

    logic scan_exhausted_w;
    assign scan_exhausted_w = DESCENDING ? (scan_key_q == KEY_ZERO)
                                         : (scan_key_q == KEY_HIGH);

    // Forward refs (driven below; used in FSM combinational decisions)
    logic scan_hit_w;
    logic scan_collision_w;
    logic scan_commit_w;
    logic scan_finish_w;     // S_SCAN -> S_IDLE this cycle (commit OR exhaust)
    logic enq_w;
    logic deq_w;

    assign deq_w = (scan_state_q == S_IDLE) && !fifo_empty_w;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            scan_state_q     <= S_IDLE;
            scan_sym_q       <= '0;
            scan_key_q       <= DESCENDING ? KEY_HIGH : KEY_ZERO;
            scan_sym_d1_q    <= '0;
            scan_key_d1_q    <= '0;
            scan_active_d1_q <= 1'b0;
        end else begin
            scan_sym_d1_q    <= scan_sym_q;
            scan_key_d1_q    <= scan_key_q;
            scan_active_d1_q <= (scan_state_q == S_SCAN);

            case (scan_state_q)
                S_IDLE: begin
                    if (deq_w) begin
                        scan_state_q <= S_SCAN;
                        scan_sym_q   <= fifo_dout_w;
                        scan_key_q   <= DESCENDING ? KEY_HIGH : KEY_ZERO;
                    end
                end
                S_SCAN: begin
                    // Refill outcome (driven by scan_data_q from prev cycle).
                    // On hit: COMMIT and go IDLE (unless cmd-collision: STALL,
                    //         hold scan_state and scan_key_q, retry next cycle).
                    if (scan_hit_w) begin
                        if (!scan_collision_w)
                            scan_state_q <= S_IDLE;
                        // else: stall - scan_state_q remains S_SCAN
                        //       scan_key_q held (no advance below)
                    end else if (scan_exhausted_w) begin
                        scan_state_q <= S_IDLE;
                    end else begin
                        scan_key_q <= DESCENDING ? (scan_key_q - 1'b1)
                                                 : (scan_key_q + 1'b1);
                    end
                end
                default: scan_state_q <= S_IDLE;
            endcase
        end
    end

    assign scan_hit_w = (scan_state_q == S_SCAN)
                     && scan_active_d1_q
                     && scan_valid_q                       // v1.28.34: separate valid_mem_q
                     && (scan_sym_d1_q == scan_sym_q);

    // ==========================================================
    // Scan-driven refill: targets kbest_*_mem_q[scan_sym_d1_q].
    // Reads scan-symbol's CURRENT cache slot-1 to decide whether
    // slot-1 must be cleared (it would duplicate the new slot-0 key).
    // Slot-0 is overwritten with the scan hit unconditionally.
    // ==========================================================
    logic [KEY_BITS-1:0] scan_cur_k1_w;
    logic                scan_cur_d1_w;
    assign scan_cur_k1_w = (K_LEVELS > 1) ? kbest_k1_mem_q[scan_sym_d1_q] : '0;
    assign scan_cur_d1_w = (K_LEVELS > 1) ? kbest_d1_mem_q[scan_sym_d1_q] : 1'b0;

    logic [VAL_BITS-1:0] scan_hit_val_w;
    assign scan_hit_val_w = scan_data_q[VAL_BITS-1:0];

    // Suppress slot-1 keep when it would duplicate new slot-0 key
    logic scan_clear_slot1_w;
    assign scan_clear_slot1_w = (K_LEVELS > 1)
                              && scan_cur_d1_w
                              && (scan_cur_k1_w == scan_key_d1_q);

    // ==========================================================
    // Kbest writer: priority cmd-update > scan-refill (different sym)
    //   - cmd update writes kbest[s0_sym_q]
    //   - scan refill writes kbest[scan_sym_d1_q] when no cmd same-sym collision
    // ==========================================================
    // Cmd has priority over scan on the per-LUTRAM single write port.
    // Even when cmd targets a DIFFERENT sym than scan, scan write is dropped
    // so the LUTRAM's one write port goes to cmd. Scan retries next cycle
    // (FSM stalls in S_SCAN). This is broader than the prior same-sym-only
    // collision check; the broader rule is what allows LUTRAM inference.
    assign scan_collision_w = scan_hit_w && cmd_we_w;
    assign scan_commit_w    = scan_hit_w && !scan_collision_w;

    // scan_finish_w: this cycle the FSM transitions S_SCAN -> S_IDLE.
    // Two reasons: successful commit, or exhaust without finding a hit.
    // (A collision-suppressed hit STALLS in S_SCAN; not a finish.)
    logic scan_exhaust_finish_w;
    assign scan_exhaust_finish_w = (scan_state_q == S_SCAN)
                                && !scan_hit_w
                                && scan_exhausted_w;
    assign scan_finish_w = scan_commit_w || scan_exhaust_finish_w;

    // Single-write-port-per-LUTRAM via combinational write-MUX.
    // Cmd has priority over scan. When BOTH would write same cycle, scan
    // is dropped and the FSM stalls (scan_collision_w covers this; see
    // scan FSM block above). This single-write semantic is what Vivado's
    // distributed-RAM inference engine requires; using two `<=` writes
    // to the same array (even at different addresses) forces flop-array
    // synthesis with a wide combinational mux per bit (verified at v1.28.24
    // pre-fix, 22 kLUT @ N=128).
    logic                k0_we_w;
    logic [SYM_BITS-1:0] k0_waddr_w;
    logic [KEY_BITS-1:0] k0_wdata_w;
    logic                v0_we_w;
    logic [SYM_BITS-1:0] v0_waddr_w;
    logic [VAL_BITS-1:0] v0_wdata_w;
    logic                d0_we_w;
    logic [SYM_BITS-1:0] d0_waddr_w;
    logic                d0_wdata_w;

    always_comb begin
        if (cmd_we_w) begin
            k0_we_w    = 1'b1;
            k0_waddr_w = s0_sym_q;
            k0_wdata_w = cmd_new_k0_w;
            v0_we_w    = 1'b1;
            v0_waddr_w = s0_sym_q;
            v0_wdata_w = cmd_new_v0_w;
            d0_we_w    = 1'b1;
            d0_waddr_w = s0_sym_q;
            d0_wdata_w = cmd_new_d0_w;
        end else if (scan_commit_w) begin
            k0_we_w    = 1'b1;
            k0_waddr_w = scan_sym_d1_q;
            k0_wdata_w = scan_key_d1_q;
            v0_we_w    = 1'b1;
            v0_waddr_w = scan_sym_d1_q;
            v0_wdata_w = scan_hit_val_w;
            d0_we_w    = 1'b1;
            d0_waddr_w = scan_sym_d1_q;
            d0_wdata_w = 1'b1;
        end else begin
            k0_we_w    = 1'b0;
            k0_waddr_w = '0;
            k0_wdata_w = '0;
            v0_we_w    = 1'b0;
            v0_waddr_w = '0;
            v0_wdata_w = '0;
            d0_we_w    = 1'b0;
            d0_waddr_w = '0;
            d0_wdata_w = 1'b0;
        end
    end

    always_ff @(posedge clk) begin
        if (k0_we_w) kbest_k0_mem_q[k0_waddr_w] <= k0_wdata_w;
        if (v0_we_w) kbest_v0_mem_q[v0_waddr_w] <= v0_wdata_w;
        if (d0_we_w) kbest_d0_mem_q[d0_waddr_w] <= d0_wdata_w;

        // size counter (cmd-driven only - single writer trivially)
        if (size_inc_w)
            size_mem_q[s0_sym_q] <= cur_size_w + 1;
        else if (size_dec_w)
            size_mem_q[s0_sym_q] <= cur_size_w - 1;
    end

    // Slot 1 single-port write MUX
    generate
    if (K_LEVELS > 1) begin : g_slot1_writes
        logic                k1_we_w;
        logic [SYM_BITS-1:0] k1_waddr_w;
        logic [KEY_BITS-1:0] k1_wdata_w;
        logic                v1_we_w;
        logic [SYM_BITS-1:0] v1_waddr_w;
        logic [VAL_BITS-1:0] v1_wdata_w;
        logic                d1_we_w;
        logic [SYM_BITS-1:0] d1_waddr_w;
        logic                d1_wdata_w;

        always_comb begin
            if (cmd_we_w) begin
                k1_we_w    = 1'b1;
                k1_waddr_w = s0_sym_q;
                k1_wdata_w = cmd_new_k1_w;
                v1_we_w    = 1'b1;
                v1_waddr_w = s0_sym_q;
                v1_wdata_w = cmd_new_v1_w;
                d1_we_w    = 1'b1;
                d1_waddr_w = s0_sym_q;
                d1_wdata_w = cmd_new_d1_w;
            end else if (scan_commit_w && scan_clear_slot1_w) begin
                // Scan needs to clear slot 1 (it duplicated the new slot-0 key)
                k1_we_w    = 1'b0;
                k1_waddr_w = '0;
                k1_wdata_w = '0;
                v1_we_w    = 1'b0;
                v1_waddr_w = '0;
                v1_wdata_w = '0;
                d1_we_w    = 1'b1;
                d1_waddr_w = scan_sym_d1_q;
                d1_wdata_w = 1'b0;
            end else begin
                k1_we_w    = 1'b0;
                k1_waddr_w = '0;
                k1_wdata_w = '0;
                v1_we_w    = 1'b0;
                v1_waddr_w = '0;
                v1_wdata_w = '0;
                d1_we_w    = 1'b0;
                d1_waddr_w = '0;
                d1_wdata_w = 1'b0;
            end
        end

        always_ff @(posedge clk) begin
            if (k1_we_w) kbest_k1_mem_q[k1_waddr_w] <= k1_wdata_w;
            if (v1_we_w) kbest_v1_mem_q[v1_waddr_w] <= v1_wdata_w;
            if (d1_we_w) kbest_d1_mem_q[d1_waddr_w] <= d1_wdata_w;
        end
    end
    endgenerate

    // ==========================================================
    // FIFO + dedup-bit update
    //   enq_w = refill_set_w && !enqueued_q[s0_sym_q]
    //         (FIFO can never be full because dedup ensures each sym
    //          appears at most once and depth = N_SYMBOLS)
    //   deq_w = (scan_state_q == S_IDLE) && !fifo_empty_w  (declared above)
    //   enqueued_q[s0_sym_q] := 1 on enq
    //   enqueued_q[scan_sym_q] := 0 on scan_finish_w
    //
    // Same-cycle enq + deq for the SAME sym is impossible because:
    //   - deq sources from FIFO head; sym was written there >=1 cycle ago
    //   - enq guard requires enqueued_q[s0_sym_q]==0; cleared only at
    //     scan_finish, which transitions to IDLE this cycle - and S_IDLE
    //     entry is the only place deq fires. The deq this cycle pops the
    //     OTHER sym (or no sym, if FIFO became empty).
    // ==========================================================
    assign enq_w = refill_set_w && !enqueued_q[s0_sym_q];

    integer ei;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            enqueued_q <= '0;
        end else begin
            if (enq_w)
                enqueued_q[s0_sym_q] <= 1'b1;
            if (scan_finish_w)
                enqueued_q[scan_sym_q] <= 1'b0;
        end
    end

    // FIFO memory write port: NO reset. LUTRAM cells have no reset port,
    // so any always_ff with async reset disqualifies the array from
    // distributed-RAM inference; Vivado falls back to flop array with a
    // wide combinational write-enable demux (verified at v1.28.26 N=128:
    // fanout-96 net `fifo_mem_q[X]/CE` was the critical-path destination).
    // Initial block above zeros entries at power-on; subsequent contents
    // are entirely covered by enq_w writes during normal operation.
    always_ff @(posedge clk) begin
        if (enq_w) fifo_mem_q[fifo_wptr_q] <= s0_sym_q;
    end

    // FIFO pointers + count: WITH reset (small, flop-friendly)
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            fifo_wptr_q  <= '0;
            fifo_rptr_q  <= '0;
            fifo_count_q <= '0;
        end else begin
            if (enq_w) begin
                fifo_wptr_q <= (fifo_wptr_q == FIFO_DEPTH - 1)
                             ? '0 : (fifo_wptr_q + 1'b1);
            end
            if (deq_w) begin
                fifo_rptr_q <= (fifo_rptr_q == FIFO_DEPTH - 1)
                             ? '0 : (fifo_rptr_q + 1'b1);
            end
            if (enq_w && !deq_w)      fifo_count_q <= fifo_count_q + 1'b1;
            else if (deq_w && !enq_w) fifo_count_q <= fifo_count_q - 1'b1;
            // enq && deq same cycle: count unchanged
        end
    end

    // ==========================================================
    // Outputs (kbest cache slot-0 of cmd-symbol)
    // ==========================================================
    assign top_sym_o   = s0_sym_q;
    assign top_key_o   = cur_k0_w;
    assign top_val_o   = cur_v0_w;
    assign top_valid_o = cur_d0_w;
    assign size_o      = cur_size_w;

endmodule
