// adapter_order_encoder.sv -- decision tuple -> 12-byte wire frame (Phase 6e)
// Pure-ops file (no instantiations) per FP-FILE-DECOMPOSITION.
//
// Frame layout (12 bytes / 96 bits, byte 0 in LSB position [7:0]):
//   byte 0  : SOH = 0x01
//   byte 1  : op_i (7-bit ASCII; e.g. 'O'=0x4F, 'X'=0x58)
//   byte 2  : side_i ? 'S' (0x53) : 'B' (0x42)
//   byte 3-6: price_i big-endian (byte 3 = MSB)
//   byte 7-10: qty_i big-endian
//   byte 11 : XOR checksum of bytes 0..10
//
// LATENCY = 1 cycle (combinational pack + 1-stage output register).

module adapter_order_encoder #(
    parameter int PX_BITS  = 32,
    parameter int QTY_BITS = 32
)(
    input  logic                  clk,
    input  logic                  rst_n,
    input  logic                  valid_i,         // gate: only encode passed orders
    input  logic [7:0]            op_i,
    input  logic                  side_i,          // 0=BID, 1=ASK
    input  logic [PX_BITS-1:0]    price_i,
    input  logic [QTY_BITS-1:0]   qty_i,
    output logic                  valid_o,
    output logic [95:0]           frame_o          // 12 bytes packed
);

    localparam logic [7:0] SOH_BYTE      = 8'h01;
    localparam logic [7:0] SIDE_BID_BYTE = 8'h42; // 'B'
    localparam logic [7:0] SIDE_ASK_BYTE = 8'h53; // 'S'

    // v1.28.21: parameterize wire-frame field widths.
    // The wire frame allocates 4 bytes for price and 4 bytes for qty
    // (fixed by spec). Pad PX_BITS/QTY_BITS to exactly 32 bits each by:
    //   - zero-extending if the signal is narrower than 32 bits
    //   - truncating MSBs if the signal is wider (compile-time choice;
    //     real deployment uses widths <= 32 for OUCH-style protocols).
    logic [31:0] price32_w;
    logic [31:0] qty32_w;
    generate
    if (PX_BITS >= 32) begin : g_px_trunc
        assign price32_w = price_i[31:0];
    end else begin : g_px_zext
        assign price32_w = {{(32-PX_BITS){1'b0}}, price_i};
    end
    if (QTY_BITS >= 32) begin : g_qty_trunc
        assign qty32_w = qty_i[31:0];
    end else begin : g_qty_zext
        assign qty32_w = {{(32-QTY_BITS){1'b0}}, qty_i};
    end
    endgenerate

    // Combinational pack
    logic [7:0] b0_w, b1_w, b2_w, b3_w, b4_w, b5_w, b6_w;
    logic [7:0] b7_w, b8_w, b9_w, b10_w, ck_w;

    assign b0_w  = SOH_BYTE;
    assign b1_w  = op_i;
    assign b2_w  = side_i ? SIDE_ASK_BYTE : SIDE_BID_BYTE;
    assign b3_w  = price32_w[31:24];
    assign b4_w  = price32_w[23:16];
    assign b5_w  = price32_w[15:8];
    assign b6_w  = price32_w[7:0];
    assign b7_w  = qty32_w[31:24];
    assign b8_w  = qty32_w[23:16];
    assign b9_w  = qty32_w[15:8];
    assign b10_w = qty32_w[7:0];
    assign ck_w  = b0_w ^ b1_w ^ b2_w ^ b3_w ^ b4_w ^ b5_w ^ b6_w
                 ^ b7_w ^ b8_w ^ b9_w ^ b10_w;

    // Pack into 96-bit frame: byte 0 in [7:0], byte 11 in [95:88]
    logic [95:0] frame_w;
    assign frame_w = {ck_w, b10_w, b9_w, b8_w, b7_w, b6_w, b5_w, b4_w,
                      b3_w, b2_w, b1_w, b0_w};

    // Output register
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_o <= 1'b0;
            frame_o <= '0;
        end else begin
            valid_o <= valid_i;
            frame_o <= valid_i ? frame_w : '0;
        end
    end

endmodule
