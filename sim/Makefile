# =============================================================================
# sim/ — pre-silicon simulation dispatcher.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
# =============================================================================
# Invoked by the top-level Makefile as `$(MAKE) -C sim`.
#
#   make all       every simulation gate (today: het-pair elaboration)
#   make het-pair  the heterogeneous pair: nanosoc_eth_chiplet +
#                  nanosoc_compute_chiplet back to back, D2D pins tied
#   make sim       the cocotb heterogeneous test (long; needs a VCS licence)
#   make clean
#
# `all` deliberately runs ELABORATION, not the cocotb tests. Elaboration is the
# gate — if the two chiplet tops cannot co-elaborate, nothing downstream means
# anything — and it is minutes rather than tens of minutes. `make sim` is opt-in.
#
# Both chiplet checkouts are taken from the repo's submodules by default;
# override to iterate against a working tree:
#
#   make -C sim het-pair \
#        ETH_CHIPLET_HOME=/path/to/nanosoc-ethernet-chiplet \
#        COMPUTE_CHIPLET_HOME=/path/to/NanoSoC-Compute-Chiplet
#
# Everything under sim/*/build/ is a generated artifact and should be
# gitignored (that file is owned by the Flows area):
#   sim/*/build/
#   sim/**/__pycache__/
#   sim/**/results.xml
#   sim/**/ucli.key
#   sim/**/novas.* sim/**/verdi*
# =============================================================================
SHELL := /bin/bash

SIM_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# Forwarded to the per-testbench Makefiles. Exported rather than passed
# positionally so a caller can set them in the environment too.
export ETH_CHIPLET_HOME
export COMPUTE_CHIPLET_HOME

.PHONY: all het-pair sim clean

all: het-pair

## het-pair: library-partitioned structural elaboration of the heterogeneous pair.
het-pair:
	@$(MAKE) -C "$(SIM_DIR)/het_pair" elab

## sim: the cocotb heterogeneous test. Long-running; attended use.
sim:
	@$(MAKE) -C "$(SIM_DIR)/het_pair" sim

clean:
	@$(MAKE) -C "$(SIM_DIR)/het_pair" clean
