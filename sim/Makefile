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
#
# They MUST be given a value here. A bare `export VAR` on an undefined variable
# exports it as EMPTY, and an empty-but-exported variable still counts as "set",
# so it beats het_pair/Makefile's own `?=` default. The child then preflights
# with a blank path and reports
#     het_pair: SKIP — chiplet checkout missing:
# with nothing after the colon. That broke every target routed through this
# dispatcher (`make sim`, `make sim-het-pair`, `make sim-het-manual`) while
# running `make -C sim/het_pair` directly worked fine — which is a confusing
# way to lose an afternoon.
ETH_CHIPLET_HOME     ?= $(abspath $(SIM_DIR)/../deps/eth-chiplet)
COMPUTE_CHIPLET_HOME ?= $(abspath $(SIM_DIR)/../deps/compute-chiplet)
export ETH_CHIPLET_HOME
export COMPUTE_CHIPLET_HOME

.PHONY: all het-pair sim manual decode clean

all: het-pair decode

## het-pair: library-partitioned structural elaboration of the heterogeneous pair.
het-pair:
	@$(MAKE) -C "$(SIM_DIR)/het_pair" elab

## sim: the cocotb heterogeneous test, DEFAULT (autoneg) posture. Long-running.
##      Expect 1 PASS / 6 FAIL until F6 is fixed — see docs/F6_ATTRIBUTION.md.
sim:
	@$(MAKE) -C "$(SIM_DIR)/het_pair" sim

## manual: the MANUAL-posture cross-die data-plane suite — the one that passes.
##         Steps over F6 testbench-side to verify the data plane and address
##         maps. Does NOT prove autonomous bring-up. See docs/SIM_PLAN.md 9a.
manual:
	@$(MAKE) -C "$(SIM_DIR)/het_pair" manual

## decode: L0-SIM-15 — chiplet_d2d_decode at both window placements. ~10 s,
##         no SoC, no link. Settles the compute 0x40-vs-0x41 peer aperture and
##         the 224 MB window alias.
decode:
	@$(MAKE) -C "$(SIM_DIR)/d2d_decode" sim

clean:
	@$(MAKE) -C "$(SIM_DIR)/het_pair" clean
	@$(MAKE) -C "$(SIM_DIR)/d2d_decode" clean
