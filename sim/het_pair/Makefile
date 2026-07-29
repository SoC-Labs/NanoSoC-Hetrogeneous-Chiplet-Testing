# =============================================================================
# het_pair — the HETEROGENEOUS pair: one real `nanosoc_eth_chiplet` die and one
# real `nanosoc_compute_chiplet` die, cross-wired through the TideLink PHY pads.
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
# =============================================================================
# TARGETS
#
#   make            (== make elab)  library-partitioned structural elaboration
#                                   of tb_het_pair under VCS. This is the gate:
#                                   if the two tops cannot co-elaborate, nothing
#                                   downstream is worth running.
#   make elab-naive                 the SAME elaboration with both flists simply
#                                   concatenated into one namespace. It is
#                                   EXPECTED TO FAIL, and it is kept because the
#                                   failure is the evidence for why `elab`
#                                   partitions (see docs/SIM_PLAN.md). Never
#                                   make this the default.
#   make sim                        the cocotb heterogeneous test.
#   make flists                     prepare the four component flists only.
#   make clean
#
# ENVIRONMENT
#
# The two chiplets come from the repo's submodules by default. Point these at a
# working checkout to iterate without touching the pins:
#
#   make ETH_CHIPLET_HOME=/path/to/nanosoc-ethernet-chiplet \
#        COMPUTE_CHIPLET_HOME=/path/to/NanoSoC-Compute-Chiplet elab
#
# Each chiplet repo owns its own environment and refuses to source its
# submodules' set_env.sh (three of them in sequence produce an environment nobody
# can reason about). So, exactly as both repos' own `make elab` recipes do, the
# environment is assembled HERE, in the recipe, per chiplet, in dependency order.
# The two are assembled in SEPARATE sub-shells because they both export
# TIDELINK_HOME / TIDECHART_HOME / SOCLABS_* to DIFFERENT values — sourcing both
# into one shell silently gives one chiplet the other's components.
# =============================================================================
SHELL := /bin/bash

SIM_DIR      := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
REPO_ROOT    := $(abspath $(SIM_DIR)/../..)

ETH_CHIPLET_HOME     ?= $(REPO_ROOT)/deps/eth-chiplet
COMPUTE_CHIPLET_HOME ?= $(REPO_ROOT)/deps/compute-chiplet

BUILD ?= $(SIM_DIR)/build

TOPLEVEL := tb_het_pair
MODULE   ?= test_het_pair

# Prepared, tool-readable component flists (see the `flists` recipe).
ETH_SOC_F  := $(BUILD)/eth_soc.f
ETH_TL_F   := $(BUILD)/eth_tidelink.f
CMP_SOC_F  := $(BUILD)/cmp_soc.f
CMP_TL_F   := $(BUILD)/cmp_tidelink.f
LIBMAP     := $(BUILD)/het_pair.map
LIBCONFIG  := $(BUILD)/het_pair_config.sv
SWITCHES   := $(BUILD)/switches.f
SOURCES    := $(BUILD)/sources.f

# `pad_skid` is TideLink's own, unmodified. Both dies' ribbons pass through it;
# it is die-agnostic so it is taken from the ethernet checkout.
PAD_SKID   := $(ETH_CHIPLET_HOME)/tidelink/cocotb/tidelink_top_pair/pad_skid.sv
# QSPI flash VIP. Byte-identical in both repos' ahb_qspi; one copy serves both.
FLASH_VIP    := $(ETH_CHIPLET_HOME)/nanosoc-multicore-system/ahb_qspi/verif/VIP/SST26VF064B.v
FLASH_INCDIR := $(ETH_CHIPLET_HOME)/nanosoc-multicore-system/ahb_qspi/verif/VIP

SKID_BITS ?= 0
export SKID_BITS

VCS_COMMON := -full64 -sverilog -timescale=1ns/1ps

# =============================================================================
# Tool / dependency preflight. Degrade with a clear message rather than an
# inscrutable failure 200 lines into a recipe.
# =============================================================================
.PHONY: check-tools
check-tools:
	@missing=0; \
	if ! command -v vcs >/dev/null 2>&1; then \
	  echo "het_pair: SKIP — no 'vcs' on PATH."; \
	  echo "          This testbench needs a SystemVerilog simulator that supports"; \
	  echo "          Verilog library maps + config blocks (VCS -libmap, or Xcelium"; \
	  echo "          -makelib). Verilator cannot build it: it rejects duplicate"; \
	  echo "          module definitions outright, which is the whole problem here."; \
	  missing=1; fi; \
	if ! command -v cocotb-config >/dev/null 2>&1; then \
	  echo "het_pair: NOTE — cocotb not found; 'make sim' unavailable, 'make elab' still works."; \
	fi; \
	for d in "$(ETH_CHIPLET_HOME)" "$(COMPUTE_CHIPLET_HOME)"; do \
	  if [ ! -d "$$d" ]; then echo "het_pair: SKIP — chiplet checkout missing: $$d"; missing=1; fi; \
	done; \
	if [ -d "$(ETH_CHIPLET_HOME)" ] && [ ! -d "$(ETH_CHIPLET_HOME)/tidelink/src" ]; then \
	  echo "het_pair: SKIP — $(ETH_CHIPLET_HOME) has uninitialised submodules."; \
	  echo "          Run the repo bootstrap (scripts/bootstrap.sh) first."; \
	  missing=1; fi; \
	if [ -d "$(COMPUTE_CHIPLET_HOME)" ] && [ ! -d "$(COMPUTE_CHIPLET_HOME)/tidelink/src" ]; then \
	  echo "het_pair: SKIP — $(COMPUTE_CHIPLET_HOME) has uninitialised submodules."; \
	  echo "          Run the repo bootstrap (scripts/bootstrap.sh) first."; \
	  missing=1; fi; \
	for d in "$(ETH_CHIPLET_HOME)" "$(COMPUTE_CHIPLET_HOME)"; do \
	  for dep in axi-chiplet-controller tidelink-phy tidelink-gpio-phy; do \
	    if [ -d "$$d/tidelink/deps/$$dep" ] && [ -z "$$(ls -A "$$d/tidelink/deps/$$dep" 2>/dev/null)" ]; then \
	      echo "het_pair: SKIP — $$d/tidelink/deps/$$dep is an EMPTY submodule."; \
	      echo "          The flists reference RTL inside it (e.g. WavResetSync.v), so"; \
	      echo "          elaboration fails late with 'Cannot find cell in liblist'"; \
	      echo "          rather than here. These are 4th-level submodules"; \
	      echo "          (repo -> deps/<chiplet> -> tidelink -> deps/<phy>) and the"; \
	      echo "          default 'make deps' deliberately stops short of them."; \
	      echo "          Run:  make deps-full"; \
	      missing=1; \
	    fi; \
	  done; \
	done; \
	if [ -d "$(ETH_CHIPLET_HOME)/tidelink/deps/tidelink-gpio-phy" ] && \
	   [ -z "$$(ls -A "$(ETH_CHIPLET_HOME)/tidelink/deps/tidelink-gpio-phy" 2>/dev/null)" ]; then \
	  echo "          NOTE: eth-chiplet's tidelink pins a tidelink-gpio-phy commit that"; \
	  echo "          its GitHub remote does NOT have; only the GitLab mirror carries it."; \
	  echo "          See docs/SIM_PLAN.md § Bootstrap hazards."; \
	fi; \
	if [ "$$missing" = "1" ]; then echo "het_pair: preflight failed — not building."; exit 1; fi; \
	echo "het_pair: preflight ok (vcs, chiplet checkouts + nested tidelink deps present)"

# =============================================================================
# Flist preparation. ONE &&-joined shell per chiplet so the sourced environment
# persists across the flatten/resolve steps AND so the two chiplets' conflicting
# exports never meet.
# =============================================================================
.PHONY: flists
flists: check-tools
	@mkdir -p "$(BUILD)"
	@echo "== preparing ethernet-chiplet flists =="
	@set -e; \
	source "$(ETH_CHIPLET_HOME)/set_env.sh" >/dev/null && \
	source "$(ETH_CHIPLET_HOME)/nanosoc-multicore-system/set_env.sh" >/dev/null && \
	source "$(ETH_CHIPLET_HOME)/tidelink/set_env.sh" >/dev/null && \
	python3 "$(ETH_CHIPLET_HOME)/flist/flatten_soc_flist.py" \
	    "$${NANOSOC_MULTICORE_HOME}/flist/nanosoc_multicore.flist" > "$(ETH_SOC_F)" && \
	python3 "$(ETH_CHIPLET_HOME)/flist/resolve_tidelink_flist.py" \
	    "$${TIDELINK_HOME}/flists/tidelink_fpga.flist" > "$(ETH_TL_F)"
	@echo "== preparing compute-chiplet flists =="
	@set -e; \
	source "$(COMPUTE_CHIPLET_HOME)/set_env.sh" >/dev/null && \
	source "$(COMPUTE_CHIPLET_HOME)/nanosoc-compute-system/set_env.sh" >/dev/null && \
	source "$(COMPUTE_CHIPLET_HOME)/tidelink/set_env.sh" >/dev/null && \
	export SOCLABS_NANOSOC_SOC_DIR="$(COMPUTE_CHIPLET_HOME)/nanosoc-compute-system/build/compute_soc" && \
	if [ ! -f "$${SOCLABS_NANOSOC_SOC_DIR}/compute_soc.flist" ]; then \
	    echo "== generating compute SoC into $${SOCLABS_NANOSOC_SOC_DIR} =="; \
	    ( cd "$(COMPUTE_CHIPLET_HOME)/nanosoc-compute-system" && source ./set_env.sh && \
	      bash compute-subsystem/sim/gen_compute_soc.sh "$${SOCLABS_NANOSOC_SOC_DIR}" ); \
	fi && \
	python3 "$(COMPUTE_CHIPLET_HOME)/flist/flatten_soc_flist.py" \
	    "$${SOCLABS_NANOSOC_SOC_DIR}/compute_soc.flist" > "$(CMP_SOC_F)" && \
	python3 "$(COMPUTE_CHIPLET_HOME)/flist/resolve_tidelink_flist.py" \
	    "$${TIDELINK_HOME}/flists/tidelink_fpga.flist" > "$(CMP_TL_F)"
	@echo "flists: prepared $(ETH_SOC_F) $(ETH_TL_F) $(CMP_SOC_F) $(CMP_TL_F)"

# =============================================================================
# Library map + config. See gen_libmap.py for why this is not optional.
# =============================================================================
.PHONY: libmap
libmap: flists
	@python3 "$(SIM_DIR)/gen_libmap.py" \
	    --eth-soc "$(ETH_SOC_F)" --eth-tl "$(ETH_TL_F)" \
	    --eth-tidechart "$(ETH_CHIPLET_HOME)/tidechart/flist/tidechart.flist" \
	    --eth-rtl "$(ETH_CHIPLET_HOME)/src/rtl" \
	    --cmp-soc "$(CMP_SOC_F)" --cmp-tl "$(CMP_TL_F)" \
	    --cmp-tidechart "$(COMPUTE_CHIPLET_HOME)/tidechart/flist/tidechart.flist" \
	    --cmp-rtl "$(COMPUTE_CHIPLET_HOME)/src/rtl" \
	    --eth-home "$(ETH_CHIPLET_HOME)" --cmp-home "$(COMPUTE_CHIPLET_HOME)" \
	    --out-libmap "$(LIBMAP)" --out-config "$(LIBCONFIG)" \
	    --out-switches "$(SWITCHES)" --out-sources "$(SOURCES)"

# =============================================================================
# The gate: library-partitioned structural elaboration.
# =============================================================================
.PHONY: elab
elab: libmap
	@mkdir -p "$(BUILD)/elab"
	@echo "== vcs -libmap $(LIBMAP) -top $(TOPLEVEL) =="
	cd "$(BUILD)/elab" && \
	vcs $(VCS_COMMON) \
	    -libmap "$(LIBMAP)" \
	    -f "$(SWITCHES)" \
	    -f "$(SOURCES)" \
	    +incdir+$(FLASH_INCDIR) \
	    +incdir+$(ETH_CHIPLET_HOME)/src/rtl \
	    +incdir+$(COMPUTE_CHIPLET_HOME)/src/rtl \
	    "$(FLASH_VIP)" \
	    "$(PAD_SKID)" \
	    "$(SIM_DIR)/$(TOPLEVEL).sv" \
	    "$(LIBCONFIG)" \
	    -top cfg_het_pair \
	    -o simv_het \
	    -l "$(BUILD)/elab/elab.log"

# =============================================================================
# The counter-experiment. A CONTROLLED comparison against `elab`: byte-for-byte
# the same sources (-f $(SOURCES)) and the same switches (-f $(SWITCHES)); the
# ONLY variable removed is the library partition (-libmap + the config's per-die
# liblists). So any failure here is attributable to the shared namespace and to
# nothing else.
#
# EXPECTED TO FAIL. Kept so the claim in docs/SIM_PLAN.md ("the two SoCs cannot
# share a module namespace") is reproducible rather than asserted. Do not "fix"
# it by reordering the flists — that ordering is what would decide the netlist,
# and that is precisely the defect being demonstrated.
# =============================================================================
.PHONY: elab-naive
elab-naive: libmap
	@mkdir -p "$(BUILD)/elab_naive"
	@echo "== NAIVE (single-namespace) elaboration — expected to FAIL =="
	cd "$(BUILD)/elab_naive" && \
	vcs $(VCS_COMMON) \
	    -f "$(SWITCHES)" \
	    -f "$(SOURCES)" \
	    +incdir+$(FLASH_INCDIR) \
	    +incdir+$(ETH_CHIPLET_HOME)/src/rtl \
	    +incdir+$(COMPUTE_CHIPLET_HOME)/src/rtl \
	    "$(FLASH_VIP)" "$(PAD_SKID)" "$(SIM_DIR)/$(TOPLEVEL).sv" \
	    -top $(TOPLEVEL) -o simv_naive \
	    -l "$(BUILD)/elab_naive/elab.log" \
	  || { echo; echo "== elab-naive FAILED, as documented. Collisions: =="; \
	       grep -E "Error-\[(UPIMI|MPD|OPD)" "$(BUILD)/elab_naive/elab.log" | head -20; \
	       exit 0; }

# =============================================================================
# cocotb run.
# =============================================================================
SIM           = vcs
TOPLEVEL_LANG = verilog

VERILOG_SOURCES = $(SIM_DIR)/$(TOPLEVEL).sv $(PAD_SKID) $(FLASH_VIP) $(LIBCONFIG)

COMPILE_ARGS += $(VCS_COMMON) -debug_access+all
COMPILE_ARGS += -libmap $(LIBMAP)
COMPILE_ARGS += -f $(SWITCHES)
COMPILE_ARGS += -f $(SOURCES)
COMPILE_ARGS += +incdir+$(FLASH_INCDIR)
COMPILE_ARGS += +incdir+$(ETH_CHIPLET_HOME)/src/rtl
COMPILE_ARGS += +incdir+$(COMPUTE_CHIPLET_HOME)/src/rtl
COMPILE_ARGS += +define+TB_TOP_SKID_BITS=$(SKID_BITS)

# The OpenCores MAC wishbone slave in the ethernet die drives X on hrdata during
# unrelated APB writes; the same workaround g2_soc_pair uses. It masks nothing
# this env asserts on — every data-plane observation is an explicit resolvable
# check.
export COCOTB_RESOLVE_X ?= ZEROS

WAVES ?= 0
ifeq ($(WAVES),1)
COMPILE_ARGS += +define+DUMP_FSDB
endif

SIM_BUILD ?= $(BUILD)/sim

# Keep `elab` the default so a bare `make` runs the gate, not the long cocotb run.
.DEFAULT_GOAL := elab

-include $(shell cocotb-config --makefiles 2>/dev/null)/Makefile.sim

sim: | libmap

clean::
	rm -rf "$(BUILD)" __pycache__ results.xml ucli.key novas.* verdi*
