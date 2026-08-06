# =============================================================================
# d2d_decode — L0-SIM-15. The REAL chiplet_d2d_decode at both window
# placements, driven directly. No SoC, no link, no bypass.
#
# Deliberately small and fast (~10 s): it answers the 0x40-vs-0x41 question and
# the 224 MB alias question, both of which have been open across three
# documents, and it does so without a two-SoC elaboration. See
# test_d2d_decode.py for why the existing tests could not settle them.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
# =============================================================================
SHELL := /bin/bash

SIM_DIR   := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
REPO_ROOT := $(abspath $(SIM_DIR)/../..)

# The decoder is byte-identical between the repos EXCEPT that only the compute
# copy carries the G4 WINDOW_BASE parameterisation, which is the thing under
# test — so it is taken from there.
COMPUTE_CHIPLET_HOME ?= $(REPO_ROOT)/deps/compute-chiplet
DECODE_SV := $(COMPUTE_CHIPLET_HOME)/src/rtl/chiplet_d2d_decode.sv

TOPLEVEL_LANG := verilog
TOPLEVEL      := tb_d2d_decode
MODULE        ?= test_d2d_decode
SIM           ?= vcs

VERILOG_SOURCES := $(DECODE_SV) $(SIM_DIR)/tb_d2d_decode.sv
COMPILE_ARGS    += -full64 -sverilog -timescale=1ns/1ps
SIM_BUILD       ?= $(SIM_DIR)/build

.PHONY: check
check:
	@if [ ! -f "$(DECODE_SV)" ]; then \
	    echo "d2d_decode: SKIP — decoder not checked out: $(DECODE_SV)"; \
	    echo "            run 'make deps' at the repo root."; exit 1; fi
	@if ! grep -q "WINDOW_BASE" "$(DECODE_SV)"; then \
	    echo "d2d_decode: SKIP — this decoder has no WINDOW_BASE parameter."; \
	    echo "            The pinned compute-chiplet predates G4; the 0x40-vs-0x41"; \
	    echo "            question cannot be asked of it. Bump the pin."; exit 1; fi
	@echo "d2d_decode: preflight ok (parameterised decoder present)"

sim: check

-include $(shell cocotb-config --makefiles 2>/dev/null)/Makefile.sim

clean::
	rm -rf "$(SIM_BUILD)" __pycache__ results.xml ucli.key novas.* verdi*
