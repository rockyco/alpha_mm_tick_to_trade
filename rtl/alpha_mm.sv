// alpha_mm.sv -- single-cycle alpha-driven MM strategy (v1.28.48).
//
// Replaces the 12-stage seq_div_lutmult divider in the Stoikov MM
// microprice with single-cycle alpha arithmetic (shift + add only).
//
// Alpha sources:
//   imb_alpha  = (bid_qty_i - ask_qty_i) >>> ALPHA_IMB_SHIFT
//   flow_alpha = ofi_signal_i           >>> ALPHA_OFI_SHIFT
//   alpha      = imb_alpha + flow_alpha
// Reservation:
//   reservation = mid + alpha - (position >>> GAMMA_SHIFT)
// Adaptive spread:
//   tighter when |alpha| > CONFIDENT_THRESHOLD; wider otherwise
//
// Pipeline (2 stages, LATENCY_CYCLES = 2):
//   S0: alpha components + mid + inv_skew + abs(alpha)
//   S1: reservation + half_spread mux + quote outputs
//
// All ops are 1-cycle arithmetic. NO multiplier/divider DSP usage.

module alpha_mm #(
    parameter int PX_BITS              = 16,
    parameter int QTY_BITS             = 16,
    parameter int POS_BITS             = 32,
    parameter int OFI_BITS             = 32,
    parameter int HALF_SPREAD          = 2,
    parameter int DEFAULT_QTY          = 10,
    parameter int ALPHA_IMB_SHIFT      = 6,
    parameter int ALPHA_OFI_SHIFT      = 4,
    parameter int GAMMA_SHIFT          = 7,
    parameter int SPREAD_ALPHA_SHIFT   = 4,
    parameter int CONFIDENT_THRESHOLD  = 4
)(
    input  logic                              clk,
    input  logic                              rst_n,
    input  logic                              valid_i,
    input  logic signed [PX_BITS-1:0]         bid_px_i,
    input  logic        [QTY_BITS-1:0]        bid_qty_i,
    input  logic signed [PX_BITS-1:0]         ask_px_i,
    input  logic        [QTY_BITS-1:0]        ask_qty_i,
    input  logic signed [POS_BITS-1:0]        position_i,
    input  logic signed [OFI_BITS-1:0]        ofi_signal_i,
    output logic                              valid_o,
    output logic signed [PX_BITS-1:0]         bid_price_o,
    output logic        [QTY_BITS-1:0]        bid_qty_o,
    output logic signed [PX_BITS-1:0]         ask_price_o,
    output logic        [QTY_BITS-1:0]        ask_qty_o
);

    localparam int W = POS_BITS;                  // wide internal signed width

    // ===== S0 combinational =====
    logic signed [W-1:0] mid_w, alpha_w, inv_skew_w, abs_alpha_w;
    logic signed [W-1:0] imb_diff_w, imb_alpha_w, flow_alpha_w;

    assign mid_w        = ($signed({{(W-PX_BITS){bid_px_i[PX_BITS-1]}}, bid_px_i})
                         + $signed({{(W-PX_BITS){ask_px_i[PX_BITS-1]}}, ask_px_i})) >>> 1;
    assign imb_diff_w   = $signed({{(W-QTY_BITS){1'b0}}, bid_qty_i})
                        - $signed({{(W-QTY_BITS){1'b0}}, ask_qty_i});
    assign imb_alpha_w  = imb_diff_w >>> ALPHA_IMB_SHIFT;
    assign flow_alpha_w = $signed({{(W-OFI_BITS){ofi_signal_i[OFI_BITS-1]}}, ofi_signal_i})
                          >>> ALPHA_OFI_SHIFT;
    assign alpha_w      = imb_alpha_w + flow_alpha_w;
    assign inv_skew_w   = $signed(position_i) >>> GAMMA_SHIFT;
    assign abs_alpha_w  = (alpha_w[W-1]) ? -alpha_w : alpha_w;

    // ===== S0 registers =====
    logic                s0_valid_q;
    logic signed [W-1:0] s0_mid_q, s0_alpha_q, s0_inv_skew_q, s0_abs_alpha_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s0_valid_q     <= 1'b0;
            s0_mid_q       <= '0;
            s0_alpha_q     <= '0;
            s0_inv_skew_q  <= '0;
            s0_abs_alpha_q <= '0;
        end else begin
            s0_valid_q     <= valid_i;
            s0_mid_q       <= mid_w;
            s0_alpha_q     <= alpha_w;
            s0_inv_skew_q  <= inv_skew_w;
            s0_abs_alpha_q <= abs_alpha_w;
        end
    end

    // ===== S1 combinational =====
    logic signed [W-1:0] reservation_w, spread_bump_w, half_spread_w;
    logic                confident_w;
    logic signed [W-1:0] bid_quote_w, ask_quote_w;

    assign reservation_w = s0_mid_q + s0_alpha_q - s0_inv_skew_q;
    assign confident_w   = (s0_abs_alpha_q > CONFIDENT_THRESHOLD);
    assign spread_bump_w = s0_abs_alpha_q >>> SPREAD_ALPHA_SHIFT;
    assign half_spread_w = confident_w
                         ? ((HALF_SPREAD > 1) ? (HALF_SPREAD - 1) : 1)
                         : (HALF_SPREAD + spread_bump_w);
    assign bid_quote_w   = reservation_w - half_spread_w;
    assign ask_quote_w   = reservation_w + half_spread_w;

    // ===== S1 registers (outputs) =====
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_o     <= 1'b0;
            bid_price_o <= '0;
            bid_qty_o   <= '0;
            ask_price_o <= '0;
            ask_qty_o   <= '0;
        end else begin
            valid_o     <= s0_valid_q;
            bid_price_o <= bid_quote_w[PX_BITS-1:0];
            bid_qty_o   <= DEFAULT_QTY[QTY_BITS-1:0];
            ask_price_o <= ask_quote_w[PX_BITS-1:0];
            ask_qty_o   <= DEFAULT_QTY[QTY_BITS-1:0];
        end
    end

endmodule
