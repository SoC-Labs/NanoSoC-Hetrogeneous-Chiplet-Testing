#-----------------------------------------------------------------------------
# Makefile — NanoSoC heterogeneous chiplet test framework
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
# One entry point for every flow in this repo. `make help` lists it all.
#
# THE ORGANISING RULE: targets are named after the TEST LEVEL they run, not
# after the tool they invoke, so the convention in docs/REPO_LAYOUT.md is the
# thing an operator types:
#
#   L0  test-offline    no boards        always safe, always in CI
#   L1  test-single     one board, RO    safe
#   L2  test-single     one board, cfg   safe
#   L3  test-pair       two boards       safe (control plane only)
#   L4  test-dataplane  two boards       *** WEDGES SILICON *** attended only
#   L5  test-soak       two boards       *** WEDGES SILICON *** attended only
#
# EVERY hardware target runs scripts/preflight.sh first, so invoking one with no
# boards on the bench fails in seconds with a diagnosis — it never hangs. That
# matters more than usual here: a wedged KR260 needs a JTAG POR issued from a
# DIFFERENT host to recover, so "fail fast" is a safety property, not a nicety.
#
# Recipes are silent (@ on the first line + .ONESHELL): the scripts below do
# their own structured logging and a doubled echo of a 5-line recipe helps
# nobody. Run `make -n <target>` to see what would execute.
#-----------------------------------------------------------------------------

SHELL         := /bin/bash
.SHELLFLAGS   := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

HETSOC_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
export HETSOC_ROOT

SCRIPTS := $(HETSOC_ROOT)/scripts
FLOWS   := $(HETSOC_ROOT)/flows
CI_DIR  := $(HETSOC_ROOT)/ci
BUILD   := $(HETSOC_ROOT)/build
RESULTS := $(BUILD)/results
VENV    := $(HETSOC_ROOT)/.venv

# Extra args forwarded to pytest:  make test-offline PYTEST_ARGS="-k targets -x"
PYTEST_ARGS ?=
# Extra args forwarded to scripts/regress.sh:  make regress ARGS=--data-plane
ARGS ?=
# Single board to act on, where a target takes one (bench-recover, lease).
BOARD ?=

# The two boards, by fpgahub name. Override here or in site.local.sh.
BOARD_A ?= kr260_01
BOARD_B ?= kr260_02
export BOARD_A BOARD_B

# L4/L5 opt-in gate. Anything other than 1 refuses to run. Also honoured by
# scripts/run_pytest.sh directly, so the guard cannot be bypassed by calling
# the script instead of the target.
I_ACCEPT_WEDGE_RISK ?= 0
export I_ACCEPT_WEDGE_RISK

.PHONY: help deps deps-full venv lint fmt test-id-map \
        test-offline test-single test-pair test-dataplane test-soak \
        sim sim-het-pair \
        preflight preflight-single preflight-pair \
        deploy-pair bench-status bench-bringup bench-recover regress \
        lease release junit dashboard clean distclean

#-----------------------------------------------------------------------------
# Self-documentation
#-----------------------------------------------------------------------------
## help: list every target (this).
help:
	@echo "NanoSoC heterogeneous chiplet testing — make targets"
	echo ""
	awk '/^## [a-zA-Z0-9_-]+: / { sub(/^## /,""); n=index($$0,": "); \
	     printf "  %-16s %s\n", substr($$0,1,n-1), substr($$0,n+2) }' $(MAKEFILE_LIST)
	echo ""
	echo "Levels (docs/REPO_LAYOUT.md): L0 offline | L1-L2 single | L3 pair | L4 data-plane | L5 soak"
	echo "L4/L5 wedge silicon. They are ATTENDED ONLY and need I_ACCEPT_WEDGE_RISK=1."
	echo ""
	echo "Boards: BOARD_A=$(BOARD_A) BOARD_B=$(BOARD_B)   (override on the command line or in site.local.sh)"
	echo "First run: source set_env.sh && make deps && make test-offline"

#-----------------------------------------------------------------------------
# Setup
#-----------------------------------------------------------------------------
## deps: fetch the chiplet submodules (+ TideLink/TideChart) and build the venv.
deps:
	@"$(SCRIPTS)/bootstrap.sh"

## deps-full: deps, plus every nested submodule of both chiplets (for RTL/FPGA flows).
deps-full:
	@"$(SCRIPTS)/bootstrap.sh" --full

## venv: create .venv and `pip install -e host/`. No submodules, no bench.
venv:
	@"$(SCRIPTS)/bootstrap.sh" --venv-only

#-----------------------------------------------------------------------------
# Static gates — no boards, no EDA licence, safe anywhere.
#-----------------------------------------------------------------------------
## lint: ruff (or flake8) over the python + shellcheck over every script.
lint:
	@"$(SCRIPTS)/lint.sh"

## fmt: apply ruff format + ruff --fix to the python. Shell is reported, not rewritten.
fmt:
	@"$(SCRIPTS)/lint.sh" --fix

## test-id-map: regenerate docs/TEST_ID_MAP.md (plan ids <-> pytest ids).
test-id-map:
	@"$(SCRIPTS)/py.sh" "$(SCRIPTS)/test_id_map.py"

#-----------------------------------------------------------------------------
# Test levels
#-----------------------------------------------------------------------------
## test-offline: L0 — pure host logic, no boards. The gate CI runs on every push.
test-offline:
	@"$(SCRIPTS)/run_pytest.sh" --level l0 -- $(PYTEST_ARGS)

## test-single: L1+L2 — one board: read-only probes, then config-plane writes.
test-single: preflight-single
	@"$(SCRIPTS)/run_pytest.sh" --level l1l2 -- $(PYTEST_ARGS)

## test-pair: L3 — two boards: link bring-up + cross-die CONTROL plane. No data.
test-pair: preflight-pair
	@"$(SCRIPTS)/run_pytest.sh" --level l3 -- $(PYTEST_ARGS)

## test-dataplane: L4 — cross-die DATA plane. WEDGES SILICON. Needs I_ACCEPT_WEDGE_RISK=1.
test-dataplane: preflight-pair
	@"$(SCRIPTS)/run_pytest.sh" --level l4 -- $(PYTEST_ARGS)

## test-soak: L5 — soak / stress / characterisation. WEDGES SILICON. Attended only.
test-soak: preflight-pair
	@"$(SCRIPTS)/run_pytest.sh" --level l5 -- $(PYTEST_ARGS)

# Preflight is split so `make test-single` does not demand two boards. A bare
# `make preflight` means "check the whole bench".
preflight:
	@"$(SCRIPTS)/preflight.sh" --pair

preflight-single:
	@"$(SCRIPTS)/preflight.sh" --single

preflight-pair:
	@"$(SCRIPTS)/preflight.sh" --pair

#-----------------------------------------------------------------------------
# Simulation — sim/ is owned by another area. Delegate; tolerate its absence so
# a fresh checkout's `make regress` is not blocked on it landing.
#-----------------------------------------------------------------------------
## sim: run the pre-silicon simulation suite in sim/ (skips cleanly if absent).
sim:
	@if [ ! -f "$(HETSOC_ROOT)/sim/Makefile" ]; then
	    echo "sim: no sim/Makefile yet — the sim area has not landed. Skipping."
	    exit 0
	fi
	$(MAKE) -C "$(HETSOC_ROOT)/sim"

## sim-het-pair: back-to-back eth+compute RTL pair sim (skips cleanly if absent).
sim-het-pair:
	@if [ ! -f "$(HETSOC_ROOT)/sim/Makefile" ]; then
	    echo "sim-het-pair: no sim/Makefile yet — the sim area has not landed. Skipping."
	    exit 0
	fi
	$(MAKE) -C "$(HETSOC_ROOT)/sim" het_pair

#-----------------------------------------------------------------------------
# Bench flows
#-----------------------------------------------------------------------------
## deploy-pair: reflash BOTH dies (eth on A, compute on B). Power-cycle first.
deploy-pair: preflight-pair
	@"$(FLOWS)/deploy_pair.sh"

## bench-bringup: bring the D2D link up on both dies CONCURRENTLY. Fresh dies only.
bench-bringup: preflight-pair
	@"$(FLOWS)/bringup_pair.sh"

## bench-status: L1 read-only probe of both boards. Cannot wedge. Start here.
bench-status:
	@"$(FLOWS)/bench_status.sh"

## bench-recover: JTAG POR a wedged board (BOARD=<name>, default both). Via mapstone-dev.
bench-recover:
	@"$(FLOWS)/recover.sh" $(if $(BOARD),--board $(BOARD),)

## regress: the full regression — offline, then single, then pair. L4/L5 opt-in.
regress:
	@"$(SCRIPTS)/regress.sh" $(ARGS)

#-----------------------------------------------------------------------------
# Board leasing (fpgahub)
#-----------------------------------------------------------------------------
## lease: acquire an fpgahub lease on both boards; records build/lease.env.
lease:
	@"$(FLOWS)/lease.sh" acquire

## release: release the fpgahub lease recorded in build/lease.env.
release:
	@"$(FLOWS)/lease.sh" release

#-----------------------------------------------------------------------------
# Result publishing
#-----------------------------------------------------------------------------
## junit: merge build/results/*.xml + *.json into one JUnit file for CI.
junit:
	@mkdir -p "$(RESULTS)"
	"$(SCRIPTS)/py.sh" "$(CI_DIR)/results_to_junit.py" "$(RESULTS)" -o "$(RESULTS)/junit.xml"

## dashboard: render build/results into a single-file HTML + markdown summary.
dashboard:
	@mkdir -p "$(RESULTS)"
	"$(SCRIPTS)/py.sh" "$(CI_DIR)/dashboard.py" "$(RESULTS)" -o "$(RESULTS)/dashboard.html"

#-----------------------------------------------------------------------------
# Cleaning
#-----------------------------------------------------------------------------
## clean: remove build artefacts and results. Keeps the venv and the submodules.
clean:
	@rm -rf "$(BUILD)" "$(HETSOC_ROOT)/.pytest_cache" "$(HETSOC_ROOT)/.ruff_cache"
	find "$(HETSOC_ROOT)" -name '__pycache__' -type d -prune \
	    -not -path '*/deps/*' -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null || true
	echo "clean: build/ and caches removed (venv + deps kept — distclean drops the venv)."

## distclean: clean, plus delete the venv. Submodules are left alone (git owns those).
distclean: clean
	@rm -rf "$(VENV)"
	echo "distclean: venv removed. Submodules untouched — 'git submodule deinit -f .' if you mean it."
