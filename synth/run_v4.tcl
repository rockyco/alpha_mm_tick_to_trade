## V4 P&R for hft_alpha_pipeline (v1.28.50).
## Verifies that the integrated alpha-driven HFT pipeline (M0 + alpha_mm
## + position_table + quote_emitter + risk_gateway + order_encoder)
## meets the 333 MHz Fmax claim that the 15-ns tick-to-trade depends on.
##
## Usage: vivado -mode batch -source run_v4.tcl

set TARGET_NS  3.0
set FPGA_PART  "xcu50-fsvh2104-2-e"
set RTL_DIR    "../rtl"
set ADAPT_DIR  "../rtl"
set OUTDIR     "."

create_project -in_memory -part $FPGA_PART

read_verilog -sv "${RTL_DIR}/alpha_mm.sv"
read_verilog -sv "${RTL_DIR}/priority_array_k_packed.sv"
read_verilog -sv "${RTL_DIR}/m0_multi_symbol.sv"
read_verilog -sv "${RTL_DIR}/position_table.sv"
read_verilog -sv "${RTL_DIR}/quote_emitter.sv"
read_verilog -sv "${ADAPT_DIR}/adapter_risk_gateway.sv"
read_verilog -sv "${ADAPT_DIR}/adapter_order_encoder.sv"
read_verilog -sv "${RTL_DIR}/hft_alpha_pipeline.sv"

set TOP "hft_alpha_pipeline"

# Match Makefile params for tb/hft_alpha_perf
set generics ""
append generics "N_SYMBOLS=1 "
append generics "ROI_LB=533504 "
append generics "ROI_SIZE=1024 "
append generics "PRICE_BITS=32 "
append generics "VAL_BITS=24 "
append generics "PX_BITS=24 "
append generics "QTY_BITS=16 "
append generics "POS_BITS=32 "
append generics "OFI_BITS=32 "
append generics "K_LEVELS=2 "
append generics "HALF_SPREAD=2 "
append generics "DEFAULT_QTY=10 "
append generics "MAX_PX_DEV=10000 "
append generics "MAX_QTY=10000 "
append generics "MAX_POSITION=1000000"

synth_design -top $TOP -part $FPGA_PART -mode out_of_context -generic $generics

create_clock -name clk -period $TARGET_NS [get_ports clk]
set_input_delay  0 -clock clk [all_inputs]
set_output_delay 0 -clock clk [all_outputs]

opt_design
place_design
route_design

report_utilization    -file ${OUTDIR}/hft_alpha_util.rpt
report_timing_summary -file ${OUTDIR}/hft_alpha_timing.rpt -warn_on_violation
report_timing -nworst 5 -setup -file ${OUTDIR}/hft_alpha_timing_paths.rpt

# Parse + write summary
set util_str   [exec grep -E "CLB LUTs|CLB Registers|RAMB36/FIFO|RAMB18 |Block RAM Tile|URAM|LUT as Distributed RAM|DSPs" ${OUTDIR}/hft_alpha_util.rpt]
set timing_str [exec grep -E "Worst Slack|Total Violation" ${OUTDIR}/hft_alpha_timing.rpt]

set fp [open ${OUTDIR}/hft_alpha_summary.txt w]
puts $fp "hft_alpha_pipeline V4 P&R (v1.28.50)"
puts $fp "======================================"
puts $fp "Part:           $FPGA_PART"
puts $fp "Target period:  $TARGET_NS ns (333.33 MHz)"
puts $fp "Top:            $TOP"
puts $fp "Generics:       $generics"
puts $fp ""
puts $fp "Resource utilization:"
puts $fp $util_str
puts $fp ""
puts $fp "Timing summary:"
puts $fp $timing_str
close $fp

puts "hft_alpha_pipeline V4 done"
