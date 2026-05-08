// hft_alpha_pipeline.sv -- alpha-driven HFT tick-to-trade pipeline (v1.28.48).
//
// Drop-in replacement for hft_full_pipeline that swaps Stoikov MM
// (microprice + seq_div_lutmult, ~18 cyc) with alpha_mm
// (TOB imbalance + OFI + inv_skew, 2 cyc).
//
// End-to-end steady-state latency target: 8 cycles
//   M0       (3) + alpha_mm (2) + qe (1) + risk (1) + encoder (1) = 8 cyc
//   @ 333 MHz: 24 ns tick-to-trade.

module hft_alpha_pipeline #(
    parameter int N_SYMBOLS    = 1,
    parameter int ROI_LB       = 533504,
    parameter int ROI_SIZE     = 1024,
    parameter int PRICE_BITS   = 32,
    parameter int VAL_BITS     = 24,
    parameter int PX_BITS      = 16,
    parameter int QTY_BITS     = 16,
    parameter int POS_BITS     = 32,
    parameter int OFI_BITS     = 32,
    parameter int K_LEVELS     = 2,
    parameter int HALF_SPREAD  = 2,
    parameter int DEFAULT_QTY  = 10,
    parameter int ALPHA_IMB_SHIFT     = 6,
    parameter int ALPHA_OFI_SHIFT     = 4,
    parameter int GAMMA_SHIFT         = 7,
    parameter int SPREAD_ALPHA_SHIFT  = 4,
    parameter int CONFIDENT_THRESHOLD = 4,
    parameter int MAX_PX_DEV   = 10000,
    parameter int MAX_QTY      = 10000,
    parameter int MAX_POSITION = 1000000
)(
    input  logic                              clk,
    input  logic                              rst_n,
    input  logic                              valid_i,
    input  logic [$clog2(N_SYMBOLS)-1:0]      sym_i,
    input  logic [1:0]                        op_i,
    input  logic                              side_i,
    input  logic [PRICE_BITS-1:0]             price_tick_i,
    input  logic [VAL_BITS-1:0]               new_abs_qty_i,
    input  logic                              fill_valid_i,
    input  logic [$clog2(N_SYMBOLS)-1:0]      fill_sym_i,
    input  logic signed [POS_BITS-1:0]        fill_delta_i,
    input  logic signed [OFI_BITS-1:0]        ofi_signal_i,
    input  logic                              kill_switch_i,
    output logic                              frame_valid_o,
    output logic [95:0]                       frame_o
);

    // ===== M0 =====
    logic [$clog2(N_SYMBOLS)-1:0] m0_sym_w;
    logic [PRICE_BITS-1:0]        m0_bid_tick_w, m0_ask_tick_w;
    logic [VAL_BITS-1:0]          m0_bid_qty_w, m0_ask_qty_w;
    logic                         m0_bid_valid_w, m0_ask_valid_w;

    m0_multi_symbol #(
        .N_SYMBOLS  (N_SYMBOLS),
        .ROI_LB     (ROI_LB),
        .ROI_SIZE   (ROI_SIZE),
        .VAL_BITS   (VAL_BITS),
        .PRICE_BITS (PRICE_BITS),
        .K_LEVELS   (K_LEVELS)
    ) u_m0 (
        .clk              (clk),
        .rst_n            (rst_n),
        .valid_i          (valid_i),
        .sym_i            (sym_i),
        .op_i             (op_i),
        .side_i           (side_i),
        .price_tick_i     (price_tick_i),
        .new_abs_qty_i    (new_abs_qty_i),
        .sym_o            (m0_sym_w),
        .best_bid_tick_o  (m0_bid_tick_w),
        .best_bid_qty_o   (m0_bid_qty_w),
        .best_bid_valid_o (m0_bid_valid_w),
        .best_ask_tick_o  (m0_ask_tick_w),
        .best_ask_qty_o   (m0_ask_qty_w),
        .best_ask_valid_o (m0_ask_valid_w)
    );

    // ===== Position table =====
    logic signed [POS_BITS-1:0] pos_value_w;
    position_table #(
        .N_SYMBOLS (N_SYMBOLS),
        .POS_BITS  (POS_BITS)
    ) u_pos (
        .clk          (clk),
        .rst_n        (rst_n),
        .fill_valid_i (fill_valid_i),
        .fill_sym_i   (fill_sym_i),
        .fill_delta_i (fill_delta_i),
        .query_sym_i  (m0_sym_w),
        .query_pos_o  (pos_value_w)
    );

    // ===== alpha_mm strategy (2 cycles) =====
    logic                       strat_valid_w;
    logic signed [PX_BITS-1:0]  strat_bid_px_w, strat_ask_px_w;
    logic [QTY_BITS-1:0]        strat_bid_qty_w, strat_ask_qty_w;
    logic                       strat_valid_in_w;

    assign strat_valid_in_w = m0_bid_valid_w & m0_ask_valid_w;

    alpha_mm #(
        .PX_BITS              (PX_BITS),
        .QTY_BITS             (QTY_BITS),
        .POS_BITS             (POS_BITS),
        .OFI_BITS             (OFI_BITS),
        .HALF_SPREAD          (HALF_SPREAD),
        .DEFAULT_QTY          (DEFAULT_QTY),
        .ALPHA_IMB_SHIFT      (ALPHA_IMB_SHIFT),
        .ALPHA_OFI_SHIFT      (ALPHA_OFI_SHIFT),
        .GAMMA_SHIFT          (GAMMA_SHIFT),
        .SPREAD_ALPHA_SHIFT   (SPREAD_ALPHA_SHIFT),
        .CONFIDENT_THRESHOLD  (CONFIDENT_THRESHOLD)
    ) u_strat (
        .clk          (clk),
        .rst_n        (rst_n),
        .valid_i      (strat_valid_in_w),
        .bid_px_i     (m0_bid_tick_w[PX_BITS-1:0]),
        .bid_qty_i    (m0_bid_qty_w[QTY_BITS-1:0]),
        .ask_px_i     (m0_ask_tick_w[PX_BITS-1:0]),
        .ask_qty_i    (m0_ask_qty_w[QTY_BITS-1:0]),
        .position_i   (pos_value_w),
        .ofi_signal_i (ofi_signal_i),
        .valid_o      (strat_valid_w),
        .bid_price_o  (strat_bid_px_w),
        .bid_qty_o    (strat_bid_qty_w),
        .ask_price_o  (strat_ask_px_w),
        .ask_qty_o    (strat_ask_qty_w)
    );

    // ===== quote_emitter =====
    logic                       qe_valid_w;
    logic [7:0]                 qe_op_w;
    logic                       qe_side_w;
    logic signed [PX_BITS-1:0]  qe_price_w;
    logic        [QTY_BITS-1:0] qe_qty_w;
    quote_emitter #(
        .PX_BITS  (PX_BITS),
        .QTY_BITS (QTY_BITS)
    ) u_emit (
        .clk         (clk),
        .rst_n       (rst_n),
        .valid_i     (strat_valid_w),
        .bid_price_i (strat_bid_px_w),
        .bid_qty_i   (strat_bid_qty_w),
        .ask_price_i (strat_ask_px_w),
        .ask_qty_i   (strat_ask_qty_w),
        .valid_o     (qe_valid_w),
        .op_o        (qe_op_w),
        .side_o      (qe_side_w),
        .price_o     (qe_price_w),
        .qty_o       (qe_qty_w)
    );

    // Reference price for risk: a microprice approximation = mid
    // (single-cycle, no division). Use latest M0 best bid+ask.
    logic [PX_BITS-1:0] ref_px_w;
    assign ref_px_w = (m0_bid_tick_w[PX_BITS-1:0] + m0_ask_tick_w[PX_BITS-1:0]) >> 1;

    // ===== risk_gateway =====
    logic                       rg_valid_w;
    logic [2:0]                 rg_decision_w;
    logic signed [POS_BITS-1:0] rg_new_pos_w;
    logic [PX_BITS-1:0]         rg_passed_px_w;
    logic [QTY_BITS-1:0]        rg_passed_qty_w;
    logic                       rg_passed_side_w;
    adapter_risk_gateway #(
        .PX_BITS  (PX_BITS),
        .QTY_BITS (QTY_BITS),
        .POS_BITS (POS_BITS)
    ) u_risk (
        .clk                (clk),
        .rst_n              (rst_n),
        .max_px_dev_i       (MAX_PX_DEV[PX_BITS-1:0]),
        .max_qty_i          (MAX_QTY[QTY_BITS-1:0]),
        .max_position_i     (MAX_POSITION[POS_BITS-1:0]),
        .kill_switch_i      (kill_switch_i),
        .reference_px_i     (ref_px_w),
        .valid_i            (qe_valid_w),
        .proposed_px_i      (qe_price_w[PX_BITS-1:0]),
        .proposed_qty_i     (qe_qty_w),
        .proposed_side_i    (qe_side_w),
        .current_position_i (pos_value_w),
        .valid_o            (rg_valid_w),
        .decision_o         (rg_decision_w),
        .new_position_o     (rg_new_pos_w),
        .passed_px_o        (rg_passed_px_w),
        .passed_qty_o       (rg_passed_qty_w),
        .passed_side_o      (rg_passed_side_w)
    );

    // op tag pipelined to encoder
    logic [7:0] op_q1;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) op_q1 <= '0;
        else        op_q1 <= qe_op_w;
    end

    // ===== order_encoder =====
    logic enc_gate_w;
    assign enc_gate_w = rg_valid_w & (rg_decision_w == 3'd0);
    adapter_order_encoder #(
        .PX_BITS  (PX_BITS),
        .QTY_BITS (QTY_BITS)
    ) u_enc (
        .clk     (clk),
        .rst_n   (rst_n),
        .valid_i (enc_gate_w),
        .op_i    (op_q1),
        .side_i  (rg_passed_side_w),
        .price_i (rg_passed_px_w),
        .qty_i   (rg_passed_qty_w),
        .valid_o (frame_valid_o),
        .frame_o (frame_o)
    );

endmodule
