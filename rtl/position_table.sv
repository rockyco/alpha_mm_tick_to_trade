// position_table.sv -- per-symbol signed position counter (LUTRAM-backed).
//
// Update interface: fill events from exchange ACK/FILL stream
//   (fill_valid_i, fill_sym_i, fill_delta_i).
//   delta is signed: + for buy fills, - for sell fills.
//   Saturating add to prevent silent wraparound on adversarial streams.
//
// Query interface: combinational read by query_sym_i.
//
// Storage: distributed RAM (LUTRAM) addressed by sym_id; FP-INDEXED-STORAGE.
//
// LATENCY: 0 cycles for query (combinational). Fill commits at next edge,
// so a query 1 cycle after a fill to the same sym sees the new value.
//
// Universal beyond HFT: per-key running counter for any keyed-event
// stream (network flow stats, per-host counters, per-task accounting).

module position_table #(
    parameter int N_SYMBOLS = 32,
    parameter int POS_BITS  = 32
)(
    input  logic                         clk,
    input  logic                         rst_n,
    // Fill update port
    input  logic                         fill_valid_i,
    input  logic [$clog2(N_SYMBOLS)-1:0] fill_sym_i,
    input  logic signed [POS_BITS-1:0]   fill_delta_i,
    // Combinational query port
    input  logic [$clog2(N_SYMBOLS)-1:0] query_sym_i,
    output logic signed [POS_BITS-1:0]   query_pos_o
);

    // FP-INDEXED-STORAGE-AS-MEMORY: explicit LUTRAM, no async reset on the
    // array (initial block + bitstream INIT zeros it on configuration).
    (* ram_style = "distributed" *)
    logic signed [POS_BITS-1:0] pos_mem_q [0:N_SYMBOLS-1];

    integer init_i;
    initial begin
        for (init_i = 0; init_i < N_SYMBOLS; init_i = init_i + 1)
            pos_mem_q[init_i] = '0;
    end

    // Combinational query
    assign query_pos_o = pos_mem_q[query_sym_i];

    // Saturating add (signed)
    localparam logic signed [POS_BITS-1:0] HI = {1'b0, {(POS_BITS-1){1'b1}}};
    localparam logic signed [POS_BITS-1:0] LO = {1'b1, {(POS_BITS-1){1'b0}}};

    logic signed [POS_BITS:0] sum_w;
    logic signed [POS_BITS-1:0] cur_w;
    logic signed [POS_BITS-1:0] sat_w;

    assign cur_w = pos_mem_q[fill_sym_i];
    assign sum_w = $signed({cur_w[POS_BITS-1], cur_w}) + $signed({fill_delta_i[POS_BITS-1], fill_delta_i});
    assign sat_w = (sum_w >  $signed({1'b0, HI})) ? HI :
                   (sum_w <  $signed({1'b1, LO})) ? LO :
                   sum_w[POS_BITS-1:0];

    // Sync write (only the addressed entry on fill_valid_i)
    always_ff @(posedge clk) begin
        if (fill_valid_i) begin
            pos_mem_q[fill_sym_i] <= sat_w;
        end
    end

endmodule
