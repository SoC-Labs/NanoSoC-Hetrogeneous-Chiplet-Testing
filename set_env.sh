#!/usr/bin/env bash
# shellcheck shell=bash
#-----------------------------------------------------------------------------
# set_env.sh — environment for the NanoSoC heterogeneous chiplet test framework
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access license.
#
# Copyright 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------
# Source this, do not execute it:   source set_env.sh
#
# Every variable below is set with `:=` (assign-if-unset) so re-sourcing is a
# no-op and an operator's existing value always wins. Nothing here is exported
# over the top of something you already chose.
#
# This wrapper does NOT re-source the chiplet submodules' set_env.sh scripts.
# Each of them mutates PATH and points vendor-IP vars at the shared read-only
# lab tree (/research/AAA/**), and sourcing several in sequence produces an
# environment nobody can reason about. The RTL/FPGA flows that genuinely need
# those variables are delegated to the submodule's own Makefile, which sources
# its own environment. See docs/CI.md.
#
# Site-specific overrides (board addresses on a different rig, an ssh proxy, a
# password) belong in site.local.sh — gitignored, sourced first, so its values
# win the `:=` below. Copy site.local.sh.example to start.
#-----------------------------------------------------------------------------

# Resolve this file's directory whether sourced from bash or zsh.
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    _HETSOC_SETENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    _HETSOC_SETENV_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

export HETSOC_ROOT="${_HETSOC_SETENV_DIR}"

# --- Site-local overrides (gitignored; sourced FIRST so it wins) -------------
if [ -f "${HETSOC_ROOT}/site.local.sh" ]; then
    # shellcheck source=/dev/null
    . "${HETSOC_ROOT}/site.local.sh"
fi

# --- Chiplet designs (submodules; see .gitmodules + docs/CI.md) --------------
: "${HETSOC_ETH_CHIPLET:=${HETSOC_ROOT}/deps/eth-chiplet}"
: "${HETSOC_COMPUTE_CHIPLET:=${HETSOC_ROOT}/deps/compute-chiplet}"
export HETSOC_ETH_CHIPLET HETSOC_COMPUTE_CHIPLET

# TideLink and TideChart are reached THROUGH the eth-chiplet submodule, never
# cloned standalone. A standalone clone drifts to a different commit than the
# one the bitstream was built from, and you end up debugging RTL that is not on
# the board (the lab's "two-checkouts trap"). The eth-chiplet pin is the
# reference because it is the die whose D2D link has been proven on silicon.
: "${TIDELINK_HOME:=${HETSOC_ETH_CHIPLET}/tidelink}"
: "${TIDECHART_HOME:=${HETSOC_ETH_CHIPLET}/tidechart}"
export TIDELINK_HOME TIDECHART_HOME

# The bench tooling the flows shell out to (kr260_eth_run.sh, kr260_deploy.sh,
# deploy_pair_role) lives inside TideLink, staged onto the boards by deploy.
: "${HETSOC_TL_FPGA_DIR:=${TIDELINK_HOME}/fpga}"
: "${HETSOC_TL_SCRIPTS:=${TIDELINK_HOME}/pynq_host/scripts}"
export HETSOC_TL_FPGA_DIR HETSOC_TL_SCRIPTS

# --- Python ------------------------------------------------------------------
# Pick the newest usable interpreter. tomllib (config loading) is 3.11+, so
# prefer that; 3.9/3.10 work if the package vendors tomli. The system default
# python3 on this host is 3.8, which is too old — hence the explicit search.
if [ -z "${HETSOC_PYTHON:-}" ]; then
    _hetsoc_py=""
    _hetsoc_py_fallback=""
    for _c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        command -v "${_c}" >/dev/null 2>&1 || continue
        _mm="$("${_c}" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null)" || continue
        [ -n "${_mm}" ] || continue
        if [ "${_mm}" -ge 311 ] && [ -z "${_hetsoc_py}" ]; then
            _hetsoc_py="${_c}"
        elif [ "${_mm}" -ge 309 ] && [ -z "${_hetsoc_py_fallback}" ]; then
            _hetsoc_py_fallback="${_c}"
        fi
    done
    [ -n "${_hetsoc_py}" ] || _hetsoc_py="${_hetsoc_py_fallback}"
    [ -n "${_hetsoc_py}" ] || _hetsoc_py="python3"
    HETSOC_PYTHON="${_hetsoc_py}"
    unset _c _mm _hetsoc_py _hetsoc_py_fallback
fi
export HETSOC_PYTHON

: "${HETSOC_VENV:=${HETSOC_ROOT}/.venv}"
export HETSOC_VENV

# Put the venv on PATH when it exists, without stacking duplicate entries on a
# re-source and without the full `activate` ceremony (which mangles PS1 and
# cannot be undone idempotently).
if [ -x "${HETSOC_VENV}/bin/python" ]; then
    case ":${PATH}:" in
        *":${HETSOC_VENV}/bin:"*) ;;
        *) PATH="${HETSOC_VENV}/bin:${PATH}"; export PATH ;;
    esac
    export VIRTUAL_ENV="${HETSOC_VENV}"
fi

# `pip install -e host/` is the supported path; PYTHONPATH is the belt-and-
# braces fallback so `python -c 'import hetsoc'` works in a bare checkout too.
case ":${PYTHONPATH:-}:" in
    *":${HETSOC_ROOT}/host:"*) ;;
    *) PYTHONPATH="${HETSOC_ROOT}/host${PYTHONPATH:+:${PYTHONPATH}}"; export PYTHONPATH ;;
esac

# --- Bench ------------------------------------------------------------------
# Defaults verified against `fpgahub status --json` on 2026-07-29:
#   kr260_01 host_ssh=ubuntu@10.22.24.159   kr260_02 host_ssh=ubuntu@10.22.24.153
# Both report host_dev_host=mapstone-dev — the host where fpgahub's PER-BOARD
# endpoints (board reset / board show / actions run) actually resolve. They 404
# from this dev box; see flows/recover.sh and docs/CI.md.
: "${HETSOC_BOARD_A:=kr260_01}"
: "${HETSOC_BOARD_B:=kr260_02}"
: "${HETSOC_BOARD_A_HOST:=ubuntu@10.22.24.159}"
: "${HETSOC_BOARD_B_HOST:=ubuntu@10.22.24.153}"
: "${HETSOC_FPGAHUB_DEV_HOST:=mapstone-dev}"
: "${HETSOC_SSH_TIMEOUT:=15}"
export HETSOC_BOARD_A HETSOC_BOARD_B HETSOC_BOARD_A_HOST HETSOC_BOARD_B_HOST
export HETSOC_FPGAHUB_DEV_HOST HETSOC_SSH_TIMEOUT

# Role assignment for the heterogeneous pair. die_a is the straight J21 pinout,
# die_b the mirrored one — a role/board swap needs a matching bitstream swap,
# so this is a pair of variables, not a single flag.
: "${HETSOC_ROLE_A:=die_a}"
: "${HETSOC_ROLE_B:=die_b}"
export HETSOC_ROLE_A HETSOC_ROLE_B

# hetsoc.config.load() searches $HETSOC_CONFIG, ./hetsoc.toml, ~/.config/hetsoc.toml.
# Only point at a file that exists — an env var naming a missing file is worse
# than no env var, because it masks the two fallbacks.
if [ -z "${HETSOC_CONFIG:-}" ] && [ -f "${HETSOC_ROOT}/hetsoc.toml" ]; then
    HETSOC_CONFIG="${HETSOC_ROOT}/hetsoc.toml"
    export HETSOC_CONFIG
fi

# --- Build/results layout ----------------------------------------------------
: "${HETSOC_BUILD:=${HETSOC_ROOT}/build}"
: "${HETSOC_RESULTS:=${HETSOC_BUILD}/results}"
export HETSOC_BUILD HETSOC_RESULTS

# --- Vendor IP (READ-ONLY shared lab trees; NEVER write here) ----------------
# Defaulted, not forced, so a submodule's own set_env.sh can still correct it.
: "${ARM_IP_LIBRARY_PATH:=/research/AAA/ip_library}"
export ARM_IP_LIBRARY_PATH

# --- Sanity (advisory only — never fail an interactive shell) ----------------
_hetsoc_missing=0
for _d in "${HETSOC_ETH_CHIPLET}" "${HETSOC_COMPUTE_CHIPLET}"; do
    if [ ! -d "${_d}/.git" ] && [ ! -f "${_d}/.git" ]; then
        echo "set_env.sh: MISSING submodule ${_d}" >&2
        _hetsoc_missing=1
    fi
done
if [ "${_hetsoc_missing}" = "1" ]; then
    echo "set_env.sh: run 'make deps' to populate deps/" >&2
fi
unset _d _hetsoc_missing _HETSOC_SETENV_DIR

echo "hetsoc: HETSOC_ROOT=${HETSOC_ROOT}"
echo "hetsoc: python=${HETSOC_PYTHON}  venv=${HETSOC_VENV}$([ -x "${HETSOC_VENV}/bin/python" ] || echo ' (not created — make venv)')"
echo "hetsoc: boards ${HETSOC_BOARD_A}=${HETSOC_BOARD_A_HOST} (${HETSOC_ROLE_A})  ${HETSOC_BOARD_B}=${HETSOC_BOARD_B_HOST} (${HETSOC_ROLE_B})"
