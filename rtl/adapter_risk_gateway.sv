// adapter_risk_gateway.sv -- pre-trade 3-bounds check pipeline (Phase 6d)
// Pure-ops file (no instantiations) per FP-FILE-DECOMPOSITION.
//
// Combinational decision + 1-stage output register. LATENCY = 1.
//
// Reject priority (matches math model):
//   1 KILL_SWITCH  > 2 PX_OUT_OF_BAND > 3 QTY_OVER_LIMIT > 4 POSITION_OVER_LIMIT

module adapter_risk_gateway #(
    parameter int PX_BITS    = 32,
    parameter int QTY_BITS   = 32,
    parameter int POS_BITS   = 32      // signed
)(
    input  logic                       clk,
    input  logic                       rst_n,
    // Recipe-configured limits (live as inputs so a host can update them)
    input  logic [PX_BITS-1:0]         max_px_dev_i,
    input  logic [QTY_BITS-1:0]        max_qty_i,
    input  logic [POS_BITS-1:0]        max_position_i,    // unsigned magnitude
    input  logic                       kill_switch_i,
    // Strategy-driven reference price
    input  logic [PX_BITS-1:0]         reference_px_i,
    // Proposed order
    input  logic                       valid_i,
    input  logic [PX_BITS-1:0]         proposed_px_i,
    input  logic [QTY_BITS-1:0]        proposed_qty_i,
    input  logic                       proposed_side_i,    // 0=BID, 1=ASK
    input  logic signed [POS_BITS-1:0] current_position_i,
    // Decision (registered)
    output logic                       valid_o,
    output logic [2:0]                 decision_o,         // 0=PASS, 1..4=REJECT_*
    output logic signed [POS_BITS-1:0] new_position_o,
    output logic [PX_BITS-1:0]         passed_px_o,
    output logic [QTY_BITS-1:0]        passed_qty_o,
    output logic                       passed_side_o
);

    localparam logic [2:0] DEC_PASS                  = 3'd0;
    localparam logic [2:0] DEC_REJ_KILL              = 3'd1;
    localparam logic [2:0] DEC_REJ_PX_OUT_OF_BAND    = 3'd2;
    localparam logic [2:0] DEC_REJ_QTY_OVER_LIMIT    = 3'd3;
    localparam logic [2:0] DEC_REJ_POSITION_OVER_LIMIT = 3'd4;

    // Combinational arithmetic
    logic [PX_BITS-1:0]         px_dev_w;
    logic signed [POS_BITS:0]   signed_delta_w;
    logic signed [POS_BITS:0]   new_pos_ext_w;
    logic signed [POS_BITS-1:0] new_pos_w;
    logic [POS_BITS-1:0]        new_pos_abs_w;

    assign px_dev_w = (proposed_px_i > reference_px_i)
                    ? (proposed_px_i - reference_px_i)
                    : (reference_px_i - proposed_px_i);

    // v1.28.21: parameterize qty -> position width bridge.
    // proposed_qty_i is QTY_BITS wide; sign-aware extension to POS_BITS
    // (qty is unsigned, so zero-extend).
    logic signed [POS_BITS-1:0] qty_as_pos_w;
    assign qty_as_pos_w = $signed({{(POS_BITS-QTY_BITS){1'b0}}, proposed_qty_i});

    assign signed_delta_w = proposed_side_i ? -qty_as_pos_w : qty_as_pos_w;

    assign new_pos_ext_w = $signed({current_position_i[POS_BITS-1], current_position_i})
                         + signed_delta_w;
    assign new_pos_w     = new_pos_ext_w[POS_BITS-1:0];
    assign new_pos_abs_w = new_pos_w[POS_BITS-1] ? -new_pos_w : new_pos_w;

    // Combinational decision
    logic [2:0] decision_w;
    always_comb begin
        if (kill_switch_i)                       decision_w = DEC_REJ_KILL;
        else if (px_dev_w > max_px_dev_i)        decision_w = DEC_REJ_PX_OUT_OF_BAND;
        else if (proposed_qty_i > max_qty_i)     decision_w = DEC_REJ_QTY_OVER_LIMIT;
        else if (new_pos_abs_w > max_position_i) decision_w = DEC_REJ_POSITION_OVER_LIMIT;
        else                                     decision_w = DEC_PASS;
    end

    // Output register
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_o        <= 1'b0;
            decision_o     <= DEC_PASS;
            new_position_o <= '0;
            passed_px_o    <= '0;
            passed_qty_o   <= '0;
            passed_side_o  <= 1'b0;
        end else begin
            valid_o        <= valid_i;
            decision_o     <= decision_w;
            new_position_o <= new_pos_w;
            passed_px_o    <= proposed_px_i;
            passed_qty_o   <= proposed_qty_i;
            passed_side_o  <= proposed_side_i;
        end
    end

endmodule
