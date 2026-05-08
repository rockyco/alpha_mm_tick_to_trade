TOPLEVEL_LANG = verilog
SIM ?= icarus
COMPILE_ARGS += -g2012
PYTHON_BIN = /usr/bin/python3
ROOT := $(abspath $(PWD)/../..)
export PYTHONPATH := $(ROOT):$(PYTHONPATH)

VERILOG_SOURCES = $(ROOT)/rtl/priority_array_k_packed.sv

TOPLEVEL = priority_array_k_packed
MODULE = test_priority_array_k_packed
COCOTB_TEST_MODULES = test_priority_array_k_packed

# Small N for fast simulation; same shape as the L1==L2 test
COMPILE_ARGS += -Ppriority_array_k_packed.N_SYMBOLS=4
COMPILE_ARGS += -Ppriority_array_k_packed.KEY_BITS=6
COMPILE_ARGS += -Ppriority_array_k_packed.VAL_BITS=16
COMPILE_ARGS += -Ppriority_array_k_packed.DESCENDING=1
COMPILE_ARGS += -Ppriority_array_k_packed.K_LEVELS=2
COMPILE_ARGS += -Ppriority_array_k_packed.DEBUG_READ_PORT=1

include $(shell cocotb-config --makefiles)/Makefile.sim
