TOPLEVEL_LANG = verilog
SIM ?= icarus
COMPILE_ARGS += -g2012
PYTHON_BIN = /usr/bin/python3
ROOT := $(abspath $(PWD)/../..)
export PYTHONPATH := $(ROOT):$(PYTHONPATH)

VERILOG_SOURCES = $(ROOT)/rtl/alpha_mm.sv

TOPLEVEL = alpha_mm
MODULE = test_alpha_mm
COCOTB_TEST_MODULES = test_alpha_mm

COMPILE_ARGS += -Palpha_mm.PX_BITS=16
COMPILE_ARGS += -Palpha_mm.QTY_BITS=16
COMPILE_ARGS += -Palpha_mm.POS_BITS=32
COMPILE_ARGS += -Palpha_mm.OFI_BITS=32
COMPILE_ARGS += -Palpha_mm.HALF_SPREAD=2
COMPILE_ARGS += -Palpha_mm.DEFAULT_QTY=10
COMPILE_ARGS += -Palpha_mm.ALPHA_IMB_SHIFT=6
COMPILE_ARGS += -Palpha_mm.ALPHA_OFI_SHIFT=4
COMPILE_ARGS += -Palpha_mm.GAMMA_SHIFT=7
COMPILE_ARGS += -Palpha_mm.SPREAD_ALPHA_SHIFT=4
COMPILE_ARGS += -Palpha_mm.CONFIDENT_THRESHOLD=4

include $(shell cocotb-config --makefiles)/Makefile.sim
