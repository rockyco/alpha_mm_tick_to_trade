// m0_multi_symbol.sv -- multi-symbol HFT M0 public book.
//
// Pure-instantiation wrapper per FP-FILE-DECOMPOSITION:
//   - Combinational ITCH-to-ROI-offset adapter (just a subtractor + range
//     check; no separate sub-module needed at this scope).
//   - 2x priority_array_k_packed:
//       u_bid: descending priority (highest tick wins)
//       u_ask: ascending priority  (lowest tick wins)
//     Both packed to N_SYMBOLS independent books in one BRAM each.
//
// Per-cycle event semantics: one event per cycle, addressed to a single
// (sym_id, side). The other side gets a NOP for that cycle, holding state.
//
// Output: best_bid / best_ask of the symbol that JUST had an event,
// observable LATENCY_CYCLES=2 cycles after event arrival.

module m0_multi_symbol #(
    parameter int N_SYMBOLS  = 32,
    parameter int ROI_LB     = 19744,    // shared ROI base (single-asset default)
    parameter int ROI_SIZE   = 256,      // # ticks per side per symbol (SHARED, easy memory control)
    parameter int VAL_BITS   = 32,       // qty width
    parameter int PRICE_BITS = 32,       // ITCH tick width
    parameter int K_LEVELS   = 2,
    // v1.28.42 per-symbol ROI_LB. Set ROI_LB_USE_PER_SYM=1 and pack one
    // PRICE_BITS-wide ROI base per symbol into ROI_LB_PACKED. Each symbol's
    // ROI window is [ROI_LB_PER_SYM[s], ROI_LB_PER_SYM[s] + ROI_SIZE).
    // The ROI_SIZE is shared across symbols (same BRAM depth per symbol)
    // for predictable memory sizing. When ROI_LB_USE_PER_SYM=0 (default),
    // the legacy scalar `ROI_LB` is used for every symbol.
    parameter int ROI_LB_USE_PER_SYM = 0,
    parameter logic [N_SYMBOLS*PRICE_BITS-1:0] ROI_LB_PACKED = '0
)(
    input  logic                       clk,
    input  logic                       rst_n,
    // Inbound MD event
    input  logic                       valid_i,
    input  logic [$clog2(N_SYMBOLS)-1:0] sym_i,
    input  logic [1:0]                 op_i,         // 0=NOP, 1=ADD/UPD, 2=DEL
    input  logic                       side_i,       // 0=BID, 1=ASK
    input  logic [PRICE_BITS-1:0]      price_tick_i,
    input  logic [VAL_BITS-1:0]        new_abs_qty_i,
    // Top-of-book outputs (for the symbol of the latest event)
    output logic [$clog2(N_SYMBOLS)-1:0]  sym_o,
    output logic [PRICE_BITS-1:0]      best_bid_tick_o,
    output logic [VAL_BITS-1:0]        best_bid_qty_o,
    output logic                       best_bid_valid_o,
    output logic [PRICE_BITS-1:0]      best_ask_tick_o,
    output logic [VAL_BITS-1:0]        best_ask_qty_o,
    output logic                       best_ask_valid_o
);

    localparam int KEY_BITS = $clog2(ROI_SIZE);
    localparam int SYM_BITS = $clog2(N_SYMBOLS) > 0 ? $clog2(N_SYMBOLS) : 1;

    // ===== ITCH adapter: price_tick -> ROI offset + range check =====
    // v1.28.42: per-symbol ROI_LB lookup. The packed parameter array is
    // sliced by sym_i. Combinational mux at input + identical mux at
    // output (output uses the symbol of the kbest read).
    logic [KEY_BITS-1:0] roi_offset_w;
    logic                in_range_w;
    logic signed [PRICE_BITS:0] offset_signed_w;
    logic [PRICE_BITS-1:0] roi_lb_for_sym_w;

    generate
        if (ROI_LB_USE_PER_SYM != 0) begin : g_per_sym_roi_in
            assign roi_lb_for_sym_w =
                ROI_LB_PACKED[sym_i*PRICE_BITS +: PRICE_BITS];
        end else begin : g_shared_roi_in
            assign roi_lb_for_sym_w = ROI_LB[PRICE_BITS-1:0];
        end
    endgenerate

    assign offset_signed_w = $signed({1'b0, price_tick_i})
                           - $signed({1'b0, roi_lb_for_sym_w});
    assign in_range_w = (offset_signed_w >= 0)
                     && (offset_signed_w < $signed({1'b0, ROI_SIZE[PRICE_BITS-1:0]}));
    assign roi_offset_w = offset_signed_w[KEY_BITS-1:0];

    // ===== Per-side op gating =====
    logic [1:0] bid_op_w, ask_op_w;
    logic       evt_valid_w;
    assign evt_valid_w = valid_i && in_range_w && (op_i == 2'd1 || op_i == 2'd2);
    assign bid_op_w = (evt_valid_w && (side_i == 1'b0)) ? op_i : 2'd0;
    assign ask_op_w = (evt_valid_w && (side_i == 1'b1)) ? op_i : 2'd0;

    // ===== priority_array_k_packed bid (DESCENDING) =====
    logic [SYM_BITS-1:0] bid_top_sym_w;
    logic [KEY_BITS-1:0] bid_top_key_w;
    logic [VAL_BITS-1:0] bid_top_val_w;
    logic                bid_top_valid_w;
    logic [KEY_BITS:0]   bid_size_w;

    priority_array_k_packed #(
        .N_SYMBOLS  (N_SYMBOLS),
        .KEY_BITS   (KEY_BITS),
        .VAL_BITS   (VAL_BITS),
        .DESCENDING (1),
        .K_LEVELS   (K_LEVELS)
    ) u_bid (
        .clk         (clk),
        .rst_n       (rst_n),
        .ready_o     (),
        .sym_i       (sym_i),
        .op_i        (bid_op_w),
        .key_i       (roi_offset_w),
        .val_i       (new_abs_qty_i),
        .top_sym_o   (bid_top_sym_w),
        .top_key_o   (bid_top_key_w),
        .top_val_o   (bid_top_val_w),
        .top_valid_o (bid_top_valid_w),
        .size_o      (bid_size_w),
        // canonical (v1.28.23) debug read port; tied to constant 0 here
        .read_sym_i   ('0),
        .read_key_i   ('0),
        .read_val_o   (),
        .read_valid_o ()
    );

    // ===== priority_array_k_packed ask (ASCENDING) =====
    logic [SYM_BITS-1:0] ask_top_sym_w;
    logic [KEY_BITS-1:0] ask_top_key_w;
    logic [VAL_BITS-1:0] ask_top_val_w;
    logic                ask_top_valid_w;
    logic [KEY_BITS:0]   ask_size_w;

    priority_array_k_packed #(
        .N_SYMBOLS  (N_SYMBOLS),
        .KEY_BITS   (KEY_BITS),
        .VAL_BITS   (VAL_BITS),
        .DESCENDING (0),
        .K_LEVELS   (K_LEVELS)
    ) u_ask (
        .clk         (clk),
        .rst_n       (rst_n),
        .ready_o     (),
        .sym_i       (sym_i),
        .op_i        (ask_op_w),
        .key_i       (roi_offset_w),
        .val_i       (new_abs_qty_i),
        .top_sym_o   (ask_top_sym_w),
        .top_key_o   (ask_top_key_w),
        .top_val_o   (ask_top_val_w),
        .top_valid_o (ask_top_valid_w),
        .size_o      (ask_size_w),
        // canonical (v1.28.23) debug read port; tied to constant 0 here
        .read_sym_i   ('0),
        .read_key_i   ('0),
        .read_val_o   (),
        .read_valid_o ()
    );

    // ===== Output: convert ROI offset back to absolute tick =====
    // top_sym_o is the symbol of the most recent event (the one that just
    // updated the addressed instance). bid and ask agree on this since
    // both packed instances are indexed by the same sym_i.
    // v1.28.40: register sym_o once more so its latency matches the
    // book outputs (2 cycles). Without this, sym_o leads best_*_o by
    // 1 cycle and a multi-symbol cocotb cross-val cannot align per-event
    // captures across the temporal mismatch.
    logic [SYM_BITS-1:0] sym_o_q;
    always_ff @(posedge clk) begin
        if (!rst_n) sym_o_q <= '0;
        else        sym_o_q <= bid_top_sym_w;
    end
    assign sym_o = sym_o_q;
    // v1.28.42: per-symbol ROI_LB lookup at output. Use bid_top_sym_w
    // (the symbol of the kbest read) to pick the correct anchor.
    // Bid and ask both observe the same sym at any cycle since both
    // packed primitives are addressed by the same sym_i.
    logic [PRICE_BITS-1:0] roi_lb_for_out_bid_w, roi_lb_for_out_ask_w;
    generate
        if (ROI_LB_USE_PER_SYM != 0) begin : g_per_sym_roi_out
            assign roi_lb_for_out_bid_w =
                ROI_LB_PACKED[bid_top_sym_w*PRICE_BITS +: PRICE_BITS];
            assign roi_lb_for_out_ask_w =
                ROI_LB_PACKED[ask_top_sym_w*PRICE_BITS +: PRICE_BITS];
        end else begin : g_shared_roi_out
            assign roi_lb_for_out_bid_w = ROI_LB[PRICE_BITS-1:0];
            assign roi_lb_for_out_ask_w = ROI_LB[PRICE_BITS-1:0];
        end
    endgenerate

    assign best_bid_tick_o = bid_top_valid_w
                           ? (roi_lb_for_out_bid_w
                              + {{(PRICE_BITS-KEY_BITS){1'b0}}, bid_top_key_w})
                           : '0;
    assign best_bid_qty_o   = bid_top_val_w;
    assign best_bid_valid_o = bid_top_valid_w;

    assign best_ask_tick_o = ask_top_valid_w
                           ? (roi_lb_for_out_ask_w
                              + {{(PRICE_BITS-KEY_BITS){1'b0}}, ask_top_key_w})
                           : '0;
    assign best_ask_qty_o   = ask_top_val_w;
    assign best_ask_valid_o = ask_top_valid_w;

endmodule
