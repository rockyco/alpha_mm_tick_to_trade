// quote_emitter.sv -- BID/ASK quote pair -> alternating one-order-per-cycle.
//
// Stoikov-style strategies produce a (bid_price, ask_price) PAIR per
// strategy event. Downstream order encoders take ONE order per cycle.
// quote_emitter bridges by:
//   - cycle T   : emit BID order (op=NEW, side=0, price=bid, qty=bid_qty)
//   - cycle T+1 : emit ASK order (op=NEW, side=1, price=ask, qty=ask_qty)
//
// LATENCY = 1 cycle (single output register stage).
//
// Throughput: 1 order per cycle at output, so a strategy producing 1 pair
// per cycle saturates this emitter at 50% input rate. For typical HFT MM
// where strategy events are sparser than the clock, this is fine.
//
// Universal beyond HFT: any "two-channel proposal -> one-channel output"
// pattern (dual-rail signal serializer, dual-bank memory output mux).

module quote_emitter #(
    parameter int PX_BITS  = 16,
    parameter int QTY_BITS = 16,
    parameter logic [7:0] OP_NEW = 8'h4F  // 'O'
)(
    input  logic                       clk,
    input  logic                       rst_n,
    input  logic                       valid_i,
    input  logic signed [PX_BITS-1:0]  bid_price_i,
    input  logic        [QTY_BITS-1:0] bid_qty_i,
    input  logic signed [PX_BITS-1:0]  ask_price_i,
    input  logic        [QTY_BITS-1:0] ask_qty_i,
    output logic                       valid_o,
    output logic [7:0]                 op_o,
    output logic                       side_o,         // 0=BID, 1=ASK
    output logic signed [PX_BITS-1:0]  price_o,
    output logic        [QTY_BITS-1:0] qty_o
);

    // Side alternation: toggle each cycle a valid_i is high.
    logic next_side_q;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            next_side_q <= 1'b0;  // emit BID first
            valid_o     <= 1'b0;
            op_o        <= '0;
            side_o      <= 1'b0;
            price_o     <= '0;
            qty_o       <= '0;
        end else begin
            valid_o <= valid_i;
            if (valid_i) begin
                op_o    <= OP_NEW;
                side_o  <= next_side_q;
                price_o <= next_side_q ? ask_price_i : bid_price_i;
                qty_o   <= next_side_q ? ask_qty_i   : bid_qty_i;
                next_side_q <= ~next_side_q;
            end
        end
    end

endmodule
