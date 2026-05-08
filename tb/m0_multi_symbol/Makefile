TOPLEVEL_LANG = verilog
SIM ?= icarus
COMPILE_ARGS += -g2012
PYTHON_BIN = /usr/bin/python3
ROOT := $(abspath $(PWD)/../..)
export PYTHONPATH := $(ROOT):$(PYTHONPATH)

VERILOG_SOURCES = \
    $(ROOT)/rtl/priority_array_k_packed.sv \
    $(ROOT)/rtl/m0_multi_symbol.sv

TOPLEVEL = m0_multi_symbol
MODULE = test_m0_multi_symbol
COCOTB_TEST_MODULES = test_m0_multi_symbol

COMPILE_ARGS += -Pm0_multi_symbol.N_SYMBOLS=8
COMPILE_ARGS += -Pm0_multi_symbol.ROI_LB=19744
COMPILE_ARGS += -Pm0_multi_symbol.ROI_SIZE=64
COMPILE_ARGS += -Pm0_multi_symbol.VAL_BITS=24
COMPILE_ARGS += -Pm0_multi_symbol.PRICE_BITS=32
COMPILE_ARGS += -Pm0_multi_symbol.K_LEVELS=2

include $(shell cocotb-config --makefiles)/Makefile.sim
