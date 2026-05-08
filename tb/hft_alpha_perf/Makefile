TOPLEVEL_LANG = verilog
SIM ?= icarus
COMPILE_ARGS += -g2012
PYTHON_BIN = /usr/bin/python3
ROOT := $(abspath $(PWD)/../..)
export PYTHONPATH := $(ROOT):$(PYTHONPATH)

VERILOG_SOURCES = \
    $(ROOT)/rtl/alpha_mm.sv \
    $(ROOT)/rtl/priority_array_k_packed.sv \
    $(ROOT)/rtl/m0_multi_symbol.sv \
    $(ROOT)/rtl/position_table.sv \
    $(ROOT)/rtl/quote_emitter.sv \
    $(ROOT)/rtl/adapter_risk_gateway.sv \
    $(ROOT)/rtl/adapter_order_encoder.sv \
    $(ROOT)/rtl/hft_alpha_pipeline.sv

TOPLEVEL = hft_alpha_pipeline
MODULE = test_hft_alpha_perf
COCOTB_TEST_MODULES = test_hft_alpha_perf

COMPILE_ARGS += -Phft_alpha_pipeline.N_SYMBOLS=1
COMPILE_ARGS += -Phft_alpha_pipeline.ROI_LB=533504
COMPILE_ARGS += -Phft_alpha_pipeline.ROI_SIZE=1024
COMPILE_ARGS += -Phft_alpha_pipeline.PRICE_BITS=32
COMPILE_ARGS += -Phft_alpha_pipeline.VAL_BITS=24
COMPILE_ARGS += -Phft_alpha_pipeline.PX_BITS=24
COMPILE_ARGS += -Phft_alpha_pipeline.QTY_BITS=16
COMPILE_ARGS += -Phft_alpha_pipeline.POS_BITS=32
COMPILE_ARGS += -Phft_alpha_pipeline.OFI_BITS=32
COMPILE_ARGS += -Phft_alpha_pipeline.K_LEVELS=2
COMPILE_ARGS += -Phft_alpha_pipeline.HALF_SPREAD=2
COMPILE_ARGS += -Phft_alpha_pipeline.DEFAULT_QTY=10
COMPILE_ARGS += -Phft_alpha_pipeline.MAX_PX_DEV=10000
COMPILE_ARGS += -Phft_alpha_pipeline.MAX_QTY=10000
COMPILE_ARGS += -Phft_alpha_pipeline.MAX_POSITION=1000000

include $(shell cocotb-config --makefiles)/Makefile.sim
